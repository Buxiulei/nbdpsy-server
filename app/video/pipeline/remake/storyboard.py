"""分镜脚本：schema 校验 + 原片事实(facts)→我们的分镜脚本 生成（spec §5）。

storyboard.json 是管线核心中间产物，落盘可查可手改。
卡片文案本地化：免责声明类关键词命中 → 固定使用须知文案；其余卡片文字送 LLM 汉化。
"""
import json
import logging
import math
import statistics

from app.video.pipeline.remake import style, timeline
from app.video.providers import llm_chat

logger = logging.getLogger(__name__)

_T_EPS = 0.05                      # 相邻场景衔接允许的浮点误差（秒）

# 分镜互斥节奏两处修正（用户第四轮验收反馈，job5 实证）——carve 只把语音窗本身转静止，
# 留下两类节奏缺陷，故在场景组装后统一做两遍普适修正（弹性时间轴模式）：
#  A. 问答块间隙漏动：相邻两句配音之间几秒的运动间隙里球又晃起来（投诉 5:38/12:28/13:16/
#     14:10/23:04/24:45 同一模式）。→ 语音窗桥接：两侧都锚着语音的静止块之间、运动间隙
#     ≤ 本阈值 → 间隙并入静止，整个问答块一停到底；> 本阈值（下一句在很远处）不并，运动照常恢复。
#  B. 指令后死区：静止块内最后一句说完后，静止还拖好几秒才恢复运动（25:50 拖 6s 空白）。
#     → 静止尾巴裁剪：静止块末端距块内最后一句语音结束点 > 本阈值 → 裁到 语音结束+本阈值，
#     裁掉的部分并入后续运动段（球早点动起来）。
# 两遍都只作用于「锚着语音的静止块」；纯组间休息 run（原片功能性留白，无任何语音锚）不受影响。
_SPEECH_BRIDGE_GAP_S = 6.0         # 语音窗桥接上限（秒）：两语音静止块间运动间隙 ≤ 此值即并入静止
_POST_SPEECH_MOTION_DELAY_S = 2.0  # 指令后恢复运动延迟（秒）：静止块最后一句说完至多再停此值就恢复运动

IMPLEMENTED_RENDERERS = {"programmatic", "still_image"}
KNOWN_RENDERERS = IMPLEMENTED_RENDERERS | {"seedance"}

USAGE_NOTICE_TITLE = "使用须知"
USAGE_NOTICE_BODY = (
    "本视频由 NBDpsy 心理咨询工作室制作，练习设计参考国际公开 EMDR 自助资料。"
    "内容不构成医疗建议，不能替代专业诊断与治疗；练习中如出现强烈不适请立即停止，"
    "并咨询专业心理或医疗人员。"
)

# 免责声明类卡片关键词（中英），命中即替换为标准使用须知
_DISCLAIMER_KEYWORDS = ("disclaimer", "liability", "medical advice", "免责", "医疗建议")


class StoryboardError(Exception):
    pass


def _quantize_t(t: float) -> float:
    """时间戳量化到 1/FPS 帧栅格。

    lavfi color=...:d= 与 -t 会把段时长向上取整到帧边界，逐场景累积会让 tones/字幕
    绝对轴与 concat 后的实际轴渐行渐远。生成期先把每段 t0/t1 对齐到 1/30 栅格，
    相邻段量化后仍相等（衔接不断），从源头消除漂移。
    """
    return round(float(t) * style.FPS) / style.FPS


def _scene_hits_span(sc: dict, spans: list) -> bool:
    """facts 球场景源区间 [t0,t1] 是否与任一强制静止源窗相交（revision scene_edit，第三轮扩展）。

    spans 为端点溯源出的 facts 源时间窗列表 [[s0,s1],...]（见 revision.resolve_scene_edit_spans）；
    命中的运动球段在 build_storyboard 里按静止处理。spans 空 → 恒 False（保真：无 override 时行为不变）。
    """
    if not spans:
        return False
    st0, st1 = float(sc.get("t0", 0.0)), float(sc.get("t1", 0.0))
    return any(min(st1, float(b)) - max(st0, float(a)) > 1e-6 for a, b in spans)


def _split_motion_by_periods(m0: float, m1: float, *, run_ref: float,
                             span: float) -> list[tuple[float, float]]:
    """把运动子段 [m0,m1] 按 run 起点起算的 span(=N·T) 栅格切成连续子段（含末尾余段），供轮色。

    切点 = run_ref + k*span 落 (m0,m1) 内者，量化到 1/FPS 帧栅格；相邻子段共用同一量化边界值
    （铺满/衔接不破）。span<=0 或无内部切点 → 原样返回单段。切点均 motion↔motion——全局相位公式
    sin(2π(t+t0)/T) 保证球位在切点连续（渲染层零改动），且不触发 F-B 栅格不变量（只管 motion↔static）。
    整周期倍数（N·T）切分令切点恰在球回到同一相位处，视觉上「每晃一组换色」自然。

    最小子场景护栏（job3 碎片病灶根治）：facts 相位边界/carve 语音窗切出的运动子段起止极少
    恰落 N·T 栅格，其头/尾余量常残成 1~14 帧的独立子场景——每片一个颜色，肉眼是闪烁。护栏保证
    「切分产物绝不短于半个轮色周期（0.5·span）」：
      ① 整段本就短于一个轮色周期（m1-m0 < span）→ 不切（一切必产 <0.5·span 碎片；单段透传是
         carve 的短片本身，非切分制造的碎片）。
      ② 头/尾栅格余量 < 0.5·span 时并入相邻满周期子段：尾片并入前一子段（沿用前段颜色，符合
         「尾巴融进上一晃」的直觉）、头片并入后一子段。整段 ≥ span 时中间子段恒 = span，仅头尾
         两端可能短，各归并一次后每段必 ≥ 0.5·span（唯一内部切点时头尾之和 ≥ span，至多一侧短）。
    只删内部切点、不移动 m0/m1，铺满/衔接不变量天然维持。
    """
    if span <= 0 or (m1 - m0) < span:           # ① 整段短于一个轮色周期：不切，原样透传
        return [(m0, m1)]
    pts: list[float] = []
    k = math.ceil((m0 - run_ref) / span - 1e-9)
    while True:
        p = _quantize_t(run_ref + k * span)
        if p >= m1 - 1e-9:
            break
        if p > m0 + 1e-9:
            pts.append(p)
        k += 1
    bounds = [m0] + pts + [m1]
    # ② 头/尾短余量并入相邻满周期子段（先尾后头；各判 len>2 防把整段并没）
    half = 0.5 * span
    if len(bounds) > 2 and bounds[-1] - bounds[-2] < half:   # 尾片并入前一子段（沿用前段颜色）
        del bounds[-2]
    if len(bounds) > 2 and bounds[1] - bounds[0] < half:     # 头片并入后一子段
        del bounds[1]
    return [(bounds[idx], bounds[idx + 1]) for idx in range(len(bounds) - 1)]


def validate_storyboard(sb: dict) -> None:
    """校验分镜脚本：结构完整、渲染器已实现、时间轴铺满无重叠、球参数合法、首尾锚定。"""
    scenes = sb.get("scenes") or []
    if not scenes:
        raise StoryboardError("scenes 为空")
    prev_t1 = None
    for sc in scenes:
        rid = sc.get("id")
        renderer = sc.get("renderer")
        if renderer not in KNOWN_RENDERERS:
            raise StoryboardError(f"场景 {rid}: 未知渲染器 {renderer}")
        if renderer not in IMPLEMENTED_RENDERERS:
            raise StoryboardError(f"场景 {rid}: 渲染器 {renderer} 未实现")
        t0, t1 = float(sc.get("t0", -1)), float(sc.get("t1", -1))
        if not (t0 >= 0 and t1 > t0):
            raise StoryboardError(f"场景 {rid}: 非法时间区间 [{t0},{t1}]")
        if prev_t1 is not None and abs(t0 - prev_t1) > _T_EPS:
            raise StoryboardError(f"场景 {rid}: 时间轴不连续（前 t1={prev_t1} 本 t0={t0}）")
        prev_t1 = t1
        if sc.get("type") == "ball_exercise":
            # 静止休息球（params.static）同样带 global_period（>0）故放行；渲染忽略该值
            period = float((sc.get("params") or {}).get("period_s", 0))
            if period <= 0:
                raise StoryboardError(f"场景 {rid}: period_s 必须为正")
    # 首尾锚定：只验相邻衔接会漏掉"手改删首/末场景致全片错位仍绿灯"——补首 t0==0、
    # 末 t1==source.duration_s（duration 缺失/<=0 时跳过末尾校验，无参照系）。
    first_t0 = float(scenes[0].get("t0", -1))
    if abs(first_t0) > _T_EPS:
        raise StoryboardError(f"首场景 t0 必须为 0（实际 {first_t0}）")
    dur = float((sb.get("source") or {}).get("duration_s") or 0)
    if dur > 0:
        last_t1 = float(scenes[-1].get("t1", -1))
        if abs(last_t1 - dur) > _T_EPS:
            raise StoryboardError(
                f"末场景 t1={last_t1} 未达原片时长 duration_s={dur}")
    _validate_ball_phase_grid(sb, scenes)


def _validate_ball_phase_grid(sb: dict, scenes: list[dict]) -> None:
    """F-B 栅格不变量（管线 fail-fast）：所有「运动↔静止」邻接边界必须落「球过中点」k*(T/2)
    栅格——停/起球恰在中心才零跳变。漂离 = |boundary - 最近 k*(T/2)|；> 1/FPS 即球停不在中心
    → 瞬移跳变（job15 漏网即 2 处 5.00s 整相位边界漂 115/258ms 未被终校吸附）。周期从球场景
    params.period_s 取（全片统一），无球邻接边界跳过。

    仅弹性时间轴模式校验：终校吸附 pass 只在弹性模式跑（见 build_storyboard），非弹性原轴模式
    的组间静止边界本就不吸附（既有 A4 色序测试用原轴，边界必然漂栅格但无终校契约）。弹性模式
    的标记是 retimed_segments 键——handler 在 validate 之后才 pop（见 app/video/stages.py），
    故生产校验时该键在场；非弹性模式无此键，跳过。
    """
    if "retimed_segments" not in sb:
        return

    def _moving(sc: dict) -> bool:
        return sc.get("type") == "ball_exercise" and not (sc.get("params") or {}).get("static")

    def _resting(sc: dict) -> bool:
        return sc.get("type") == "ball_exercise" and bool((sc.get("params") or {}).get("static"))

    for a, b in zip(scenes, scenes[1:]):
        if not ((_moving(a) and _resting(b)) or (_resting(a) and _moving(b))):
            continue                                          # 无球 / motion↔motion / static↔static 跳过
        period = float((a.get("params") or {}).get("period_s")
                       or (b.get("params") or {}).get("period_s") or 0)
        if period <= 0:
            continue
        h = period / 2.0
        boundary = float(a["t1"])                             # == b["t0"]（铺满不变量）
        drift = abs(boundary - round(boundary / h) * h)
        if drift > 1.0 / style.FPS + 1e-6:
            raise StoryboardError(
                f"场景 {a.get('id')}→{b.get('id')}: motion↔static 边界 {boundary:.4f} "
                f"漂离 k*(T/2)={h:.4f} 栅格 {drift * 1000:.1f}ms（停/起球不在中心→跳变）")


def _nearest_orig_color(hex_color: str) -> str:
    """原片球色 hex → 最近的参考色名（white/green/red），用于品牌色映射。"""
    h = hex_color.lstrip("#")
    rgb = tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))
    return min(style.ORIG_BALL_REFS,
               key=lambda name: sum((a - b) ** 2 for a, b in
                                    zip(rgb, style.ORIG_BALL_REFS[name])))


def _is_disclaimer(text: str) -> bool:
    low = (text or "").lower()
    return any(k in low for k in _DISCLAIMER_KEYWORDS)


async def _chat_localize(cards: list[str]) -> dict[str, str]:
    """LLM 汉化卡片文字：{原文: 中文}。失败返回空 dict（调用方兜底原文）。"""
    if not cards:
        return {}
    prompt = (
        "把以下视频章节卡/文字卡的英文文字翻译成简体中文，风格简洁专业（心理科普语境），"
        "标题类不超过 8 个字。只输出 JSON 对象 {原文: 中文}，不要其他内容。\n"
        + json.dumps(cards, ensure_ascii=False))
    try:
        # 换 import 面：源 get_llm(_LLM_KEY).chat(...).content → 薄 provider llm_chat 直返字符串。
        content = await llm_chat(messages=[{"role": "user", "content": prompt}],
                                 temperature=0.2) or ""
        start, end = content.find("{"), content.rfind("}")
        return json.loads(content[start:end + 1]) if start >= 0 else {}
    except Exception as exc:
        logger.warning("卡片文案本地化失败，保留原文: %s", exc)
        return {}


def _global_period(facts_scenes: list[dict]) -> float:
    """全片统一球周期 = 全部实测周期（period_estimated==True）的中位数。

    wave2 问题②：各运动段实测周期不一致（2.47~2.72s）会让段边界球瞬移。取中位数
    统一为一个全局周期，相位连续在全片成立。无任何实测则回退 DEFAULT_PERIOD_S。
    """
    measured = [float(sc["period_s"]) for sc in facts_scenes
                if sc.get("kind") == "ball_exercise"
                and sc.get("period_estimated") and sc.get("period_s")]
    return statistics.median(measured) if measured else style.DEFAULT_PERIOD_S


def _phase_touches_static(scenes: list[dict], k: int,
                          run_start: int, run_end: int) -> bool:
    """运动相位 k（属运动 run [run_start, run_end)）是否紧邻静止休息球。

    A4 顺延判定用：只有 run 首/尾相位可能与静止球相邻（run 内部相位两侧都是运动）。
    """
    if k == run_start and run_start > 0:
        prev = scenes[run_start - 1]
        if prev.get("kind") == "ball_exercise" and prev.get("static"):
            return True
    if k == run_end - 1 and run_end < len(scenes):
        nxt = scenes[run_end]
        if nxt.get("kind") == "ball_exercise" and nxt.get("static"):
            return True
    return False


def _card_scene(sc: dict, zh_map: dict[str, str], warnings: list[str],
                *, t0: float, t1: float) -> dict:
    """卡片类 facts 场景 → still_image 分镜场景（含 other 降级品牌底卡）。

    t0/t1 由调用方按轴给定（普通模式=量化后原轴；弹性时间轴模式=重排后新轴）。
    """
    kind = sc.get("kind")
    text = sc.get("text", "") or ""
    if kind in ("title_card", "text_card") and _is_disclaimer(text):
        content = {"title": USAGE_NOTICE_TITLE, "body": USAGE_NOTICE_BODY}
    elif kind == "title_card":
        content = {"title": zh_map.get(text, text)}
    elif kind == "text_card":
        content = {"title": "", "body": zh_map.get(text, text)}
    else:                                                       # other：降级品牌底卡
        warnings.append(f"场景[{t0:.0f}s,{t1:.0f}s] kind={kind} 降级为品牌底卡")
        content = {"title": "", "body": ""}
    return {"t0": t0, "t1": t1,
            "type": kind if kind in ("title_card", "text_card") else "text_card",
            "renderer": "still_image", "content": content, "transition": "fade"}


def _is_motion_ball(sc: dict) -> bool:
    return sc.get("type") == "ball_exercise" and not (sc.get("params") or {}).get("static")


def _is_static_ball(sc: dict) -> bool:
    return sc.get("type") == "ball_exercise" and bool((sc.get("params") or {}).get("static"))


def _static_touches_speech(sc: dict, speech_windows: list) -> bool:
    """静止球场景 [t0,t1] 是否锚着语音（与任一语音窗真相交）。

    speech_windows = [(start, end+尾延展), ...]（新轴）。用于区分「锚着语音的静止块」
    与「纯组间休息 run（原片功能性留白，无语音）」——两处节奏修正只作用前者。
    """
    return any(float(w0) < sc["t1"] - 1e-6 and float(w1) > sc["t0"] + 1e-6
               for w0, w1 in speech_windows)


def _merge_adjacent_statics(scenes: list[dict]) -> None:
    """把连续的静止球场景合并成单个场景（原地改 scenes）。

    carve 逐相位 / 逐语音窗切分、组间休息 run、桥接并入的间隙，都可能产出多个紧邻的米白
    静止子场景（渲染完全一致——同米白、同居中、同参数）。合并成一个既让「问答块一停到底」
    在 storyboard.json 里显式成一段，也让尾巴裁剪按整块处理（无需跨多子场景）。
    合并只并同为静止的球场景，保留首段 params（全片静止段参数一致：米白 + 全局周期）。
    """
    out: list[dict] = []
    for sc in scenes:
        if out and _is_static_ball(out[-1]) and _is_static_ball(sc):
            out[-1]["t1"] = sc["t1"]                        # 紧邻静止：延展前段末端，丢弃本段
        else:
            out.append(sc)
    scenes[:] = out


def _bridge_speech_gaps(scenes: list[dict], speech_windows: list) -> None:
    """语音窗桥接（第四轮反馈 A：问答块间隙漏动）——原地改 scenes。

    扫场景序列，凡「静止块 → 运动间隙 → 静止块」且两侧静止块都锚着语音、间隙总时长
    ≤ _SPEECH_BRIDGE_GAP_S 者，把整段运动间隙逐场景改成米白静止（沿用相邻静止参数模板），
    使整个问答块一停到底。间隙可含多个运动子场景（color_cycle 逐 N·T 轮色会把间隙切成数段），
    故按「静止到下一个静止之间的连续运动段」整体判定与并入。
    间隙 > 阈值（下一句语音在很远处）不并——运动照常恢复（保 5:38 那类块末长间隙的正确恢复）。
    speech_windows 空（非弹性模式）→ 直接返回，行为不变。
    """
    if not speech_windows:
        return
    i = 0
    while i < len(scenes):
        if not _is_static_ball(scenes[i]):
            i += 1
            continue
        j = i + 1                                           # 收集本静止块之后的连续运动段 [i+1, j)
        while j < len(scenes) and _is_motion_ball(scenes[j]):
            j += 1
        if (j < len(scenes) and j > i + 1 and _is_static_ball(scenes[j])
                and scenes[j]["t0"] - scenes[i + 1]["t0"] <= _SPEECH_BRIDGE_GAP_S + 1e-9
                and _static_touches_speech(scenes[i], speech_windows)
                and _static_touches_speech(scenes[j], speech_windows)):
            tmpl = dict(scenes[i]["params"])               # 米白静止参数模板（含 static=True）
            for m in range(i + 1, j):
                scenes[m]["params"] = dict(tmpl)
            i = j                                           # [i..j] 已全静止，从右静止续扫（可再链桥）
            continue
        i = j                                               # 无桥接：跳过这段运动到下一个可能的静止


def _trim_static_speech_tails(scenes: list[dict], speech_windows: list,
                              *, period: float, fps: int) -> None:
    """静止尾巴裁剪（第四轮反馈 B：指令后死区）——原地改 scenes。

    对每个「锚着语音、其后紧邻运动段」的静止块：取块内最后一句语音结束点 last_end，若静止块
    末端距 last_end > _POST_SPEECH_MOTION_DELAY_S，则把末端裁到 last_end + 该延迟，裁掉的部分
    并入后续运动段（后续运动 t0 左移到新末端）——总时长不变，只是球早点动起来。
    作为管线最后一步（F3 栅格终校之后）运行：新末端落既有「球过中点」栅格（phase_floor，
    ≤ 语音结束+延迟），停/起球恰在中心、漂移 ≤0.5/fps 满足 F-B 不变量，且无后续 pass 再动它
    （若放 _snap 之前，其 static→motion 的 phase_ceil 会把边界向后顶回去）。
    只裁锚着语音的静止块（含「原片休息 run 但块内有指令语音」这类，如 25:50）；纯无语音的
    组间休息 run（last_end 不存在）跳过——绝不动原片功能性留白。speech_windows 空 → 直接返回。
    合并已保证静止块=单场景，故这里假定连续静止已并作一段（scenes[i] 即整块）。
    """
    if not speech_windows:
        return
    tail = timeline._SPEECH_WINDOW_TAIL
    for i in range(len(scenes) - 1):
        sc, nxt = scenes[i], scenes[i + 1]
        if not (_is_static_ball(sc) and _is_motion_ball(nxt)):
            continue
        ends = [float(w1) - tail for w0, w1 in speech_windows
                if float(w0) < sc["t1"] - 1e-6 and float(w1) > sc["t0"] + 1e-6]
        if not ends:                                        # 无语音锚（纯组间休息 run）→ 不裁
            continue
        last_end = max(ends)
        if sc["t1"] - last_end <= _POST_SPEECH_MOTION_DELAY_S + 1e-9:
            continue
        new_end = timeline.phase_floor(last_end + _POST_SPEECH_MOTION_DELAY_S,
                                       period=period, fps=fps)
        new_end = min(max(new_end, sc["t0"]), sc["t1"])     # 夹回本静止块内（不越 run 起止）
        if new_end < sc["t1"] - 1e-9:
            sc["t1"] = new_end                              # 静止块末端裁短
            nxt["t0"] = new_end                             # 裁掉部分并入后续运动段（衔接不断）


def _snap_ball_boundaries(scenes: list[dict], *, period: float, fps: int) -> None:
    """F3/F-B 停球零跳变最终归一化 pass：所有边界操作（carve/量化/最短窗/合并/组装）完成后、
    validate 之前扫一遍场景序列，把每个「运动↔静止」邻接边界吸附到「球过中点」栅格 k*(T/2)，
    原地改 scenes。

    根因：运动段末帧球停在任意 x、下一帧静止球突现中心 → 跳帧瞬移。吸附后运动恰在球滑到中心
    那一帧结束/起步，静止中心球接得上，全程零跳变。契约：任何 motion→static / static→motion
    邻接边界落 k*(T/2) 栅格（误差 ≤ 1/fps）。

    覆盖两类同根因边界：① 语音窗切出的静止子段与前后运动子段的边界（carve 已吸附，此处幂等）；
    ② 组间天然静止休息 run 与相邻运动 run 的边界（carve 看不到，此处补吸附）。
    向静止侧生长（吞掉运动侧一小段）只缩短运动、不碰语音窗，A2 语音窗覆盖不变量不破；
    motion↔motion（相位连续无跳变）/ 任一侧为卡片（无球）的边界不动。

    生长冲突处理（F-B 根治点）：旧实现遇短 motion 子场景（两侧静止相向生长挤压）时把边界
    clamp 到 ±1 帧的 off-grid 值——job14 实测 3 处 motion↔static 边界因此漂离栅格 115~540ms
    （315ms≈球瞬移七成振幅，用户投诉的跳变）。此处改为：生长至多把 motion 挤成零长（clamp 到
    该 motion 的另一侧边界，而非 ±1 帧），随后把被吞成非正长的 motion 子场景整段丢弃——其两侧
    边界此时已相等，铺满/衔接自动维持（相邻若同为静止即两段米白球无缝续，无跳变）。

    循环到不动点（F-B job15 漏网根治点）：单遍吸附+丢弃不够——短 motion 末相位塌缩丢弃后，
    原本被它「遮蔽」的前一整相位会直接贴上静止 rest，其边界在上一遍是 motion↔motion（相位连续
    被跳过）从未吸附，漂离栅格（job15 实测 2 处 5.00s 整相位边界漂 115/258ms → 球瞬移 232/490px）。
    故改为循环：吸附→丢弃塌缩 motion→若有丢弃则暴露了新的 motion↔static 邻接，再吸附。收敛
    保证：每轮要么删除 ≥1 场景（场景数严格下降、有下界 0），要么无删除即到不动点退出；对已落
    栅格的边界 phase_floor/phase_ceil 幂等，重扫不扰动既有结果。
    """
    def _is_motion(s: dict) -> bool:
        return s.get("type") == "ball_exercise" and not (s.get("params") or {}).get("static")

    def _is_static(s: dict) -> bool:
        return s.get("type") == "ball_exercise" and bool((s.get("params") or {}).get("static"))

    def _snap_pass() -> None:
        for a, b in zip(scenes, scenes[1:]):
            boundary = a["t1"]                       # == b["t0"]（铺满不变量）
            if _is_motion(a) and _is_static(b):      # 运动→静止：静止向左生长到 ≤boundary 的过中点
                snapped = max(timeline.phase_floor(boundary, period=period, fps=fps), a["t0"])
            elif _is_static(a) and _is_motion(b):    # 静止→运动：静止向右生长到 ≥boundary 的过中点
                snapped = min(timeline.phase_ceil(boundary, period=period, fps=fps), b["t1"])
            else:
                continue
            a["t1"] = b["t0"] = snapped

    while True:
        _snap_pass()
        before = len(scenes)
        # 被静止吞成非正长的 motion 子场景整段丢弃（两侧边界已相等，铺满不变量自动维持）
        scenes[:] = [s for s in scenes
                     if not (_is_motion(s) and s["t1"] <= s["t0"] + 1e-9)]
        if len(scenes) == before:                    # 无场景被丢弃 → 无新暴露边界，到不动点
            break


def _emit_cycle_motion_run(scenes: list, src_scenes: list, i: int, j: int,
                           intervals: dict, speech_windows: list, *,
                           base_params: dict, ball_palette: list, global_period: float,
                           span: float, phase_idx: int, is_static_facts) -> int:
    """color_cycle_periods 运动 run：按 A2 语音窗 carve + N·T 切分出段序列，逐 motion 段轮色。

    两遍法（第二遍才知每 motion 段的静止邻居，顺延跳槽判定才干净）：
      1) 逐相位过 carve_motion_for_speech（复用 A2 静止子段 + F3 栅格吸附）；motion 子段再按 run
         起点起算的 N·T 栅格切 chunk（_split_motion_by_periods），静止子段原样入列。
      2) 逐 motion chunk 沿调色板顺序取色（phase_idx 跨全片连续）；轮到米白且紧邻静止（内部 carve
         静止 / run 端邻组间静止或 scene_edit 强制静止段）→ 跳槽取下一色（复用顺延跳槽语义）。
    渲染层零改动：chunk 边界 motion↔motion，全局相位保证球位连续；F-B 栅格不变量只管 motion↔static，
    chunk 内边界豁免，run 与静止的外边界仍由 _snap_ball_boundaries 终校吸附。返回推进后的 phase_idx。
    """
    run_ref = intervals[i][0]
    run_segs: list[dict] = []
    for k in range(i, j):
        ph_t0, ph_t1 = intervals[k]
        for kind, st0, st1 in timeline.carve_motion_for_speech(
                ph_t0, ph_t1, speech_windows, fps=style.FPS, period=global_period):
            if kind == "static":
                run_segs.append({"t0": st0, "t1": st1, "type": "ball_exercise",
                                 "renderer": "programmatic",
                                 "params": dict(base_params, ball_color=style.CREAM,
                                                static=True)})
            else:
                for c0, c1 in _split_motion_by_periods(st0, st1, run_ref=run_ref, span=span):
                    run_segs.append({"t0": c0, "t1": c1, "type": "ball_exercise",
                                     "renderer": "programmatic",
                                     "params": dict(base_params)})       # 色第二遍填
    # run 两端是否紧邻静止（组间休息 run / scene_edit 强制静止段）——顺延跳槽判定用
    prev_static = (i > 0 and src_scenes[i - 1].get("kind") == "ball_exercise"
                   and is_static_facts(src_scenes[i - 1]))
    next_static = (j < len(src_scenes) and src_scenes[j].get("kind") == "ball_exercise"
                   and is_static_facts(src_scenes[j]))
    m = len(run_segs)
    for pos, seg in enumerate(run_segs):
        if seg["params"].get("static"):
            continue
        touch = ((pos > 0 and run_segs[pos - 1]["params"].get("static"))
                 or (pos == 0 and prev_static)
                 or (pos < m - 1 and run_segs[pos + 1]["params"].get("static"))
                 or (pos == m - 1 and next_static))
        color = ball_palette[phase_idx % len(ball_palette)]
        if color == style.CREAM and touch:      # 米白紧邻静止 → 跳槽（避免运动米白球看着像静止）
            phase_idx += 1
            color = ball_palette[phase_idx % len(ball_palette)]
        phase_idx += 1
        seg["params"]["ball_color"] = color
    scenes.extend(run_segs)
    return phase_idx


# ==================== 摆动组临床剂量重编排（用户第五轮反馈：EMDR 每组 24-40 轮）====================
# 诊断（job6 实证）：「说话停球」互斥把原片连续双侧刺激按每个检查点问句切成 ~14 轮的碎组，低于临床
# 剂量（每组 24-40 轮不间断，一左一右=1 轮=一个周期 T≈2.486s）。逐组硬拉长会得荒谬总时长；正解是
# 「重编排聚合」——以检查点语音块为组边界，每组摆动轮数 clamp 到 [min,max] 且组内不间断，零散旁白
# 静止块前移堆进本组开头静止窗（真正达成「组内语音=零」的互斥）。仅弹性模式 + 给出 set_rounds_range 启用。
_REORCH_REST_MIN_S = 8.0        # 无语音静止 ≥ 此秒 = 功能性留白 rest = 天然组边界（保留原时长）
# 检查点（组边界）语义：语音块含疑问句（？/?）或含以下检查语义关键词即为组边界；其余零散旁白
# （指令/鼓励，如「继续保持专注」「看着球说颜色」）不是边界，前移进本组开头静止窗。
_REORCH_CHECK_KEYWORDS = ("打分", "打个分", "感受", "留意到", "想法")


def _reorch_seg_text(seg: dict) -> str:
    return str(seg.get("zh") or "")


def _is_boundary_speech(texts: list[str]) -> bool:
    """静止块语音是否构成「检查点语音块」（=组边界）：任一句含疑问句或检查语义关键词。"""
    for t in texts:
        if "？" in t or "?" in t or any(k in t for k in _REORCH_CHECK_KEYWORDS):
            return True
    return False


def _clamp_rounds(x: float, lo: int, hi: int) -> int:
    """组摆动轮数 = clamp(原有总运动轮数, min, max) 四舍五入到整轮。lo/hi 均正整数故结果 ∈[lo,hi]。"""
    return int(round(min(max(x, lo), hi)))


def _assign_segments_to_scenes(scenes: list[dict], retimed: list[dict]) -> dict:
    """每条台词句按其**起点** start 归到对应场景下标（新轴）：语音从哪个场景区间开始即归该场景，
    无命中归起点最近场景。用起点而非中点/最大重叠——静止块末句配音常向后越过块末探入相邻卡片
    （句尾 +0.5 尾延展 + 自然时长），按中点/重叠会把该句误判归卡片、平移后落到前一段运动上
    （job6 seg114「慢慢睁开眼睛」实证）；起点恰是「carve 把该句切进哪个静止窗」的稳定归属。
    台词句（非 no_dub）若归到运动段则防御式改归起点最近静止段（A2：台词必落静止窗）。
    返回 {scene_idx: [seg_idx,...]}。"""
    static_idxs = [i for i, sc in enumerate(scenes) if _is_static_ball(sc)]
    spans = [(float(sc.get("t0", 0.0)), float(sc.get("t1", 0.0))) for sc in scenes]

    def _center(idx: int) -> float:
        return (spans[idx][0] + spans[idx][1]) / 2.0

    out: dict[int, list[int]] = {}
    for si, seg in enumerate(retimed):
        if seg.get("start") is None:
            continue
        start = float(seg["start"])
        best = next((idx for idx, (t0, t1) in enumerate(spans) if t0 <= start < t1), None)
        if best is None:                                # 起点不落任何区间（越界）→ 起点最近场景
            best = min(range(len(spans)), key=lambda k: abs(_center(k) - start))
        if (not seg.get("no_dub")) and _is_motion_ball(scenes[best]) and static_idxs:
            best = min(static_idxs, key=lambda k: abs(_center(k) - start))
        out.setdefault(best, []).append(si)
    return out


def _emit_static_window(new_scenes: list, retimed: list, seg_idxs: list[int],
                        cursor: float, *, period: float, fps: int, base_params: dict) -> float:
    """组开头旁白窗 / 检查点边界窗：把 seg_idxs（台词先后）在静止窗内按 LEAD 起、句间 GAP(1s)、
    尾 TAIL 顺排落位（原地改 retimed 的 start/end），窗末端吸附到「球过中点」栅格 k*(T/2)（停/起球
    在中心零跳变），发一个米白居中静止场景。返回窗末端（下一段起点）。"""
    durs = [float(retimed[i].get("end", 0.0)) - float(retimed[i].get("start", 0.0))
            for i in seg_idxs]
    window_dur = max(timeline.min_card_duration(durs), timeline._MIN_STATIC_S)
    end = timeline.phase_ceil(cursor + window_dur, period=period, fps=fps)
    t = cursor + timeline._LEAD                          # 首句提前量
    for i, d in zip(seg_idxs, durs):
        retimed[i]["start"] = round(t, 3)
        retimed[i]["end"] = round(t + d, 3)
        t += d + timeline._GAP                           # 句间 1s 顺排
    new_scenes.append({"t0": cursor, "t1": end, "type": "ball_exercise",
                       "renderer": "programmatic",
                       "params": dict(base_params, ball_color=style.CREAM, static=True)})
    return end


def _emit_rest_window(new_scenes: list, cursor: float, rest_dur: float, *,
                      period: float, fps: int, base_params: dict) -> float:
    """功能性留白 rest 边界：保留原时长（末端吸附栅格），无语音的米白居中静止球。返回窗末端。"""
    end = timeline.phase_ceil(cursor + rest_dur, period=period, fps=fps)
    new_scenes.append({"t0": cursor, "t1": end, "type": "ball_exercise",
                       "renderer": "programmatic",
                       "params": dict(base_params, ball_color=style.CREAM, static=True)})
    return end


def _emit_reorch_motion(new_scenes: list, t0: float, t1: float, phase_idx: int, *,
                        base_params: dict, ball_palette: list, color_mode: str | None,
                        span: float, prev_static: bool, next_static: bool) -> int:
    """组内不间断摆动：[t0,t1] 按 color_cycle 轮色（span=N·T）切成 motion↔motion 色块逐段上色。
    色块内边界 motion↔motion（全局相位保球位连续、栅格豁免）；首/末色块紧邻静止且轮到米白则跳槽
    （避免运动米白球看着像静止）。color_mode=single 时全程取调色板首色。返回推进后的 phase_idx。"""
    chunks = _split_motion_by_periods(t0, t1, run_ref=t0, span=span)
    m = len(chunks)
    for pos, (c0, c1) in enumerate(chunks):
        if color_mode == "single":
            color = ball_palette[0]
            phase_idx += 1
        else:
            touch = (pos == 0 and prev_static) or (pos == m - 1 and next_static)
            color = ball_palette[phase_idx % len(ball_palette)]
            if color == style.CREAM and touch:
                phase_idx += 1
                color = ball_palette[phase_idx % len(ball_palette)]
            phase_idx += 1
        new_scenes.append({"t0": c0, "t1": c1, "type": "ball_exercise",
                           "renderer": "programmatic",
                           "params": dict(base_params, ball_color=color)})
    return phase_idx


def _reorch_build_groups(zone_idxs: list[int], scenes: list[dict], retimed: list[dict],
                         scene_segs: dict) -> list[dict]:
    """把球练习区场景序列按检查点语音块 / 功能性留白 rest 分组。每组累计：前移旁白句 narr + 组内
    总运动时长 motion_dur + 组尾边界 bound（("speech", segs) / ("rest", 原时长) / None=末组无边界）。"""
    groups: list[dict] = []
    cur: dict = {"narr": [], "motion_dur": 0.0}
    for si in zone_idxs:
        sc = scenes[si]
        if _is_motion_ball(sc):
            cur["motion_dur"] += float(sc["t1"]) - float(sc["t0"])
        elif _is_static_ball(sc):
            segs = sorted(scene_segs.get(si, []))
            texts = [_reorch_seg_text(retimed[i]) for i in segs]
            dur = float(sc["t1"]) - float(sc["t0"])
            if (not segs) and dur >= _REORCH_REST_MIN_S:        # 功能性留白 rest = 天然边界
                cur["bound"] = ("rest", dur)
                groups.append(cur); cur = {"narr": [], "motion_dur": 0.0}
            elif segs and _is_boundary_speech(texts):           # 检查点语音块 = 组边界
                cur["bound"] = ("speech", segs)
                groups.append(cur); cur = {"narr": [], "motion_dur": 0.0}
            else:                                               # 零散旁白（或 <8s 无语音静止）→ 前移进组开头
                cur["narr"].extend(segs)
    if cur["narr"] or cur["motion_dur"] > 1e-6:                 # 末组：无组尾边界
        cur["bound"] = None
        groups.append(cur)
    return groups


def _reorchestrate_rounds(scenes: list[dict], retimed: list[dict], *,
                          rounds_min: int, rounds_max: int, period: float, fps: int,
                          color_cycle_periods: int | None, ball_palette: list,
                          color_mode: str | None, base_params: dict) -> float:
    """摆动组临床剂量重编排（见本节顶部说明）。原地重排 scenes[:] 与 retimed（各句 start/end 重
    落位），返回重排后新总时长。前置：scenes 是弹性组装完成态（carve/桥接/合并后），retimed 为
    各句新轴 start/end。卡片段保时长仅整体平移；球练习区按组重建（组开头旁白窗 + 组内不间断摆动
    clamp 到 [min,max] + 组尾检查点/rest 静止窗）。所有 motion↔static 边界由构造落 k*(T/2) 栅格。"""
    scene_segs = _assign_segments_to_scenes(scenes, retimed)
    span = float(color_cycle_periods or 1) * period
    h = period / 2.0
    # 分单元：卡片各自成单元；连续球场景聚成「球练习区」单元
    units: list[tuple] = []
    i, n = 0, len(scenes)
    while i < n:
        if scenes[i].get("type") == "ball_exercise":
            j = i
            while j < n and scenes[j].get("type") == "ball_exercise":
                j += 1
            units.append(("zone", list(range(i, j)))); i = j
        else:
            units.append(("card", i)); i += 1

    new_scenes: list[dict] = []
    cursor, phase_idx = 0.0, 0
    for kind, payload in units:
        if kind == "card":                                  # 卡片：保时长整体平移（含内部台词句同移）
            sc = scenes[payload]
            dur = float(sc["t1"]) - float(sc["t0"])
            delta = cursor - float(sc["t0"])
            for seg_i in scene_segs.get(payload, []):
                if retimed[seg_i].get("start") is None:
                    continue
                retimed[seg_i]["start"] = round(float(retimed[seg_i]["start"]) + delta, 3)
                retimed[seg_i]["end"] = round(float(retimed[seg_i]["end"]) + delta, 3)
            nsc = dict(sc); nsc["t0"], nsc["t1"] = cursor, _quantize_t(cursor + dur)
            new_scenes.append(nsc); cursor = nsc["t1"]
            continue
        # 球练习区：按组边界重建
        for g in _reorch_build_groups(payload, scenes, retimed, scene_segs):
            if g["narr"]:                                   # 组开头旁白窗（前移的零散旁白）
                cursor = _emit_static_window(new_scenes, retimed, g["narr"], cursor,
                                             period=period, fps=fps, base_params=base_params)
            if g["motion_dur"] > 1e-6:                       # 组内不间断摆动，轮数 clamp 到 [min,max]
                rounds = _clamp_rounds(g["motion_dur"] / period, rounds_min, rounds_max)
                k0 = round(cursor / h)                       # cursor 落栅格（静止窗末端 phase_ceil）→ 复原相位序
                m_t1 = _quantize_t((k0 + 2 * rounds) * h)    # 整 rounds 轮 = 2·rounds 个半周期 → 末端落栅格
                prev_static = bool(new_scenes) and _is_static_ball(new_scenes[-1])
                phase_idx = _emit_reorch_motion(
                    new_scenes, cursor, m_t1, phase_idx, base_params=base_params,
                    ball_palette=ball_palette, color_mode=color_mode, span=span,
                    prev_static=prev_static, next_static=g["bound"] is not None)
                cursor = m_t1
            bound = g["bound"]
            if bound is None:
                continue
            if bound[0] == "rest":                           # 功能性留白 rest 边界（保原时长）
                cursor = _emit_rest_window(new_scenes, cursor, bound[1],
                                           period=period, fps=fps, base_params=base_params)
            else:                                            # 检查点语音边界窗
                cursor = _emit_static_window(new_scenes, retimed, bound[1], cursor,
                                             period=period, fps=fps, base_params=base_params)

    _merge_adjacent_statics(new_scenes)                      # 零运动组致的相邻静止合一
    # 不跑 _snap_ball_boundaries：所有 motion↔static 边界由构造已落 k*(T/2) 栅格，而 phase_ceil
    # 对「量化后略高于理想 k·h 的栅格点」非幂等（会把已对齐边界再向前顶半周期 → 每组少半轮），
    # 反而破坏已正确的边界。终校仅供 carve/量化出的非栅格边界，此处无此类边界。
    for idx, sc in enumerate(new_scenes, start=1):
        sc["id"] = idx
    scenes[:] = new_scenes
    return new_scenes[-1]["t1"] if new_scenes else 0.0


async def build_storyboard(facts: dict, *, duration: float,
                           segments: list[dict] | None = None,
                           clip_durations: list[float] | None = None,
                           y_ratio: float | None = None,
                           palette: list[str] | None = None,
                           period_s: float | None = None,
                           color_mode: str | None = None,
                           color_cycle_periods: int | None = None,
                           static_source_spans: list | None = None,
                           sentence_gap: float | None = None,
                           card_duration_overrides: list | None = None,
                           set_rounds_range: list | None = None) -> dict:
    """原片事实 → nbdpsy_v1 分镜脚本。

    revision 参数覆盖（B4，均 None 时行为不变，显式传参不 monkeypatch 全局常量）：
    y_ratio 球心竖直位置（写入球段 params 供渲染器读）；palette 覆盖 BALL_PALETTE 循环色；
    period_s 覆盖全片统一摆动周期；color_mode="single" 时运动球全程单色（取 palette[0]），
    默认/"cycle" 按相位轮播；sentence_gap 覆盖 relayout 的 card 块句间停顿。

    第三轮验收扩展（None/空时行为逐字节不变，用现有生成测试保证）：
    color_cycle_periods=N 把连续运动球段按每 N 个摆动周期（N·T）切成子场景逐段轮色（复用相位轮转
    + 顺延跳槽语义，用户「每晃一组变色」= N=1）——切点 motion↔motion、全局相位保证球位连续、渲染零改动；
    static_source_spans=[[s0,s1],...] 把源区间命中的运动球段强制转静止（米白居中、无提示音、栅格吸附
    终校照走——与既有静止段语义一字不差），供 scene_edit（把孤立摆动球段停成静止）落地。
    card_duration_overrides=[[src_t0,src_t1,new_dur],...] 把源区间命中的卡片场景目标时长强制成
    new_dur（缩短/延长页面停留），后续场景整体前移、全局顺序护栏 + 栅格终校照走——供 card_edit
    的 duration_s 落地（弹性时间轴 relayout 消费；非弹性模式无 relayout，该覆盖不生效）。

    第五轮验收扩展（None/空时行为逐字节不变，恒等锚测试保证）：
    set_rounds_range=[min,max] 把球练习区按检查点语音块为边界重编排——每组摆动轮数 clamp 到
    [min,max] 且组内不间断、零散旁白前移进组开头静止窗（组内语音=零），临床剂量落地（见
    _reorchestrate_rounds）。仅弹性模式生效（非弹性无 retimed，该覆盖不消费）。

    球段（wave2 + A4）：连续微段按「运动 run / 静止 run」聚合（run 仅判运动/静止与保时长）。
    运动 run 用全片统一中位周期，颜色恢复 per 相位粒度——每相位按相位序循环取 BALL_PALETTE
    （紧邻静止段的米白相位顺延下一色）；静止 run 为米白居中休息球（供 schema 校验带周期，
    渲染不使用）。卡片段：免责声明卡→标准使用须知，title/text 汉化，other 降级底卡。

    弹性时间轴（wave5）：segments + clip_durations 都给出时调 timeline.relayout 按语音
    自然时长重排——场景 t0/t1 用新轴、source.duration_s = 重排后新总时长，重排后的台词句
    （retimed_segments，带新轴 start/end + orig_*）随返回 dict 携出，供 handler 覆写回台词
    文件后 pop 掉（不落进 storyboard.json）；未给出时行为不变（场景用量化后原轴）。
    """
    warnings = list(facts.get("warnings") or [])
    src_scenes = facts.get("scenes", [])
    to_localize = [sc.get("text", "") for sc in src_scenes
                   if sc.get("kind") in ("title_card", "text_card")
                   and sc.get("text") and not _is_disclaimer(sc.get("text", ""))]
    zh_map = await _chat_localize(to_localize)
    if to_localize and not zh_map:              # 本地化整体失败：保留原文，但记入 warnings 可见
        warnings.append("卡片本地化失败，保留原文")

    # 时间轴：弹性重排（语音优先）或原轴量化。intervals[facts下标] = (t0, t1)
    retimed = None
    new_total = None
    if segments is not None and clip_durations is not None:
        block_time_map, retimed, tl_warnings = timeline.relayout(
            src_scenes, segments, clip_durations, fps=style.FPS, gap=sentence_gap,
            card_duration_overrides=card_duration_overrides)
        warnings.extend(tl_warnings)
        intervals = block_time_map
        new_total = block_time_map[len(src_scenes) - 1][1] if src_scenes else 0.0
    else:
        intervals = {idx: (_quantize_t(sc["t0"]), _quantize_t(sc["t1"]))
                     for idx, sc in enumerate(src_scenes)}

    # period_s 覆盖全片统一周期（revision B4），未给沿用实测中位周期
    global_period = float(period_s) if period_s else _global_period(src_scenes)
    ball_palette = palette or style.BALL_PALETTE     # palette 覆盖循环调色板（revision B4）
    # A2 说话时球停：球块内句子的语音窗 [start, end+尾延展]，落到运动 run 内需切静止子场景。
    # 非弹性时间轴（无 retimed）时为空 → 运动 run 不切分，行为不变。
    # A3：no_dub 句不朗读、不占时间轴（relayout 未重排其 start，仍是原片轴），不得混进
    # 语音窗——否则会用原片轴时间误切静止子场景，故一并排除。
    speech_windows = ([(float(s["start"]), float(s["end"]) + timeline._SPEECH_WINDOW_TAIL)
                       for s in retimed
                       if s.get("start") is not None and not s.get("no_dub")]
                      if retimed else [])
    # scene_edit 强制静止源窗（第三轮扩展）：命中的运动球段按静止处理——run 聚合/渲染/无 cue/栅格
    # 吸附全走既有静止段路径。static_spans 空 → _is_static_facts 恒等于 bool(sc.static)，逐字节保真。
    static_spans = list(static_source_spans or [])

    def _is_static_facts(sc: dict) -> bool:
        return bool(sc.get("static")) or _scene_hits_span(sc, static_spans)

    scenes: list[dict] = []
    phase_idx = 0                               # 运动相位序（A4：跨全片连续，静止相位不占序）
    i, n = 0, len(src_scenes)
    while i < n:
        sc = src_scenes[i]
        if sc.get("kind") != "ball_exercise":
            t0, t1 = intervals[i]
            scenes.append(_card_scene(sc, zh_map, warnings, t0=t0, t1=t1))
            i += 1
            continue
        # 聚合连续同类（运动/静止）球微段为一个 run——run 仅用于运动/静止判定与时长保真；
        # 运动 run 的颜色恢复 per 相位粒度（A4），静止 run 整段一个米白休息球。
        # scene_edit 命中的运动球段视同静止（_is_static_facts），故与相邻静止段聚合为一个米白休息球。
        is_static = _is_static_facts(sc)
        j = i
        while (j < n and src_scenes[j].get("kind") == "ball_exercise"
               and _is_static_facts(src_scenes[j]) == is_static):
            j += 1
        base_params = {"bg_color": style.DARK_BG, "period_s": global_period,
                       "amplitude_ratio": style.BALL_AMPLITUDE_RATIO,
                       "audio_cue": "alternating_tone"}
        if y_ratio is not None:                 # 覆盖时才写 params.y_ratio，渲染器缺省读 style 常量
            base_params["y_ratio"] = float(y_ratio)
        if is_static:                           # 组间休息：米白居中静止球，无双侧提示音（整 run 一场景）
            run_t0, run_t1 = intervals[i][0], intervals[j - 1][1]
            scenes.append({"t0": run_t0, "t1": run_t1, "type": "ball_exercise",
                           "renderer": "programmatic",
                           "params": dict(base_params, ball_color=style.CREAM,
                                          static=True)})
        elif color_cycle_periods:               # revision：逐 N·T 段轮色（第三轮扩展，取代逐相位色）
            phase_idx = _emit_cycle_motion_run(
                scenes, src_scenes, i, j, intervals, speech_windows,
                base_params=base_params, ball_palette=ball_palette,
                global_period=global_period, span=float(color_cycle_periods) * global_period,
                phase_idx=phase_idx, is_static_facts=_is_static_facts)
        else:                                   # 运动 run：逐相位铺循环色（A4），各相位再过 A2 语音窗切分
            for k in range(i, j):
                if color_mode == "single":      # revision：全程单色取调色板首色，不轮播
                    color = ball_palette[0]
                    phase_idx += 1
                else:
                    color = ball_palette[phase_idx % len(ball_palette)]
                    # 循环色轮到米白且相位紧邻静止休息球 → 跳过米白槽位取下一色（phase_idx 额外 +1）。
                    # 既避免与静止米白球视觉「没变」，也避免与本 run 次相位天然的深金相邻同色
                    # （纯替换会让顺延深金与次相位深金相邻）。
                    if color == style.CREAM and _phase_touches_static(src_scenes, k, i, j):
                        phase_idx += 1
                        color = ball_palette[phase_idx % len(ball_palette)]
                    phase_idx += 1
                ph_t0, ph_t1 = intervals[k]
                motion_params = dict(base_params, ball_color=color)
                # A2：落入本相位的语音窗切成静止子场景（球停/米白），窗前后仍本相位循环色
                # F3：传 global_period，静止子段边界吸附「球过中点」栅格，停/起球恰在中心零跳变
                for kind, st0, st1 in timeline.carve_motion_for_speech(
                        ph_t0, ph_t1, speech_windows, fps=style.FPS,
                        period=global_period):
                    sub = (dict(base_params, ball_color=style.CREAM, static=True)
                           if kind == "static" else dict(motion_params))
                    scenes.append({"t0": st0, "t1": st1, "type": "ball_exercise",
                                   "renderer": "programmatic", "params": sub})
        i = j

    # 分镜互斥节奏两处修正（第四轮反馈，弹性模式）。顺序有讲究：
    #  1) 桥接问答块内运动间隙（产出新静止）→ 2) 合并连续静止成整块 → 3) F3 栅格终校对齐所有
    #     「运动↔静止」边界（含桥接/合并后新暴露的边界）→ 4) 裁静止块指令后死区尾巴。
    # 尾巴裁剪必须放在栅格终校**之后**：_snap 的 static→motion 规则用 phase_ceil 向后生长静止
    # （保停/起球在中心），会把裁短的边界又顶回去（且 120fps 量化下 phase_floor/ceil 非互幂等，
    # 生长后不再回落）；故裁剪作为最后一步落 phase_floor 栅格点（≤ 语音结束+延迟，且漂移
    # ≤0.5/fps 满足 F-B 不变量），无后续 pass 再动它。
    # F3：弹性时间轴模式下，把所有「运动↔静止」球场景边界吸附到「球过中点」栅格（含组间天然
    # 静止 run 的边界）。非弹性模式保持原轴量化的既有行为（生产恒走弹性模式，见 handler）。
    if retimed is not None:
        _bridge_speech_gaps(scenes, speech_windows)
        _merge_adjacent_statics(scenes)
        if set_rounds_range:
            # 摆动组临床剂量重编排（第五轮反馈）：以桥接/合并后的干净静止块为分组依据，重建整个
            # 球练习区（组内不间断摆动 clamp 到 [min,max] + 旁白前移）。重编排自带栅格终校与合并，
            # 故不再跑原轴的 _snap/_trim（其产物会被整段重建丢弃）。new_total 改为重排后新总时长。
            reorch_base = {"bg_color": style.DARK_BG, "period_s": global_period,
                           "amplitude_ratio": style.BALL_AMPLITUDE_RATIO,
                           "audio_cue": "alternating_tone"}
            if y_ratio is not None:
                reorch_base["y_ratio"] = float(y_ratio)
            new_total = _reorchestrate_rounds(
                scenes, retimed, rounds_min=int(set_rounds_range[0]),
                rounds_max=int(set_rounds_range[1]), period=global_period, fps=style.FPS,
                color_cycle_periods=color_cycle_periods, ball_palette=ball_palette,
                color_mode=color_mode, base_params=reorch_base)
        else:
            _snap_ball_boundaries(scenes, period=global_period, fps=style.FPS)
            _trim_static_speech_tails(scenes, speech_windows,
                                      period=global_period, fps=style.FPS)

    for idx, sc in enumerate(scenes, start=1):  # 聚合后重排连续 id
        sc["id"] = idx
    src_duration = new_total if new_total is not None else \
        _quantize_t(duration or (scenes[-1]["t1"] if scenes else 0))
    sb = {"version": 1, "style": "nbdpsy_v1",
          "source": {"duration_s": src_duration},
          "scenes": scenes, "warnings": warnings}
    if retimed is not None:
        sb["retimed_segments"] = retimed
    return sb
