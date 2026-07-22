"""分镜脚本：schema 校验 + 原片事实(facts)→我们的分镜脚本 生成（spec §5）。

storyboard.json 是管线核心中间产物，落盘可查可手改。
卡片文案本地化：免责声明类关键词命中 → 固定使用须知文案；其余卡片文字送 LLM 汉化。
"""
import json
import logging
import statistics

from app.video.pipeline.remake import style, timeline
from app.video.providers import llm_chat

logger = logging.getLogger(__name__)

_T_EPS = 0.05                      # 相邻场景衔接允许的浮点误差（秒）

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


async def build_storyboard(facts: dict, *, duration: float,
                           segments: list[dict] | None = None,
                           clip_durations: list[float] | None = None,
                           y_ratio: float | None = None,
                           palette: list[str] | None = None,
                           period_s: float | None = None,
                           color_mode: str | None = None,
                           sentence_gap: float | None = None) -> dict:
    """原片事实 → nbdpsy_v1 分镜脚本。

    revision 参数覆盖（B4，均 None 时行为不变，显式传参不 monkeypatch 全局常量）：
    y_ratio 球心竖直位置（写入球段 params 供渲染器读）；palette 覆盖 BALL_PALETTE 循环色；
    period_s 覆盖全片统一摆动周期；color_mode="single" 时运动球全程单色（取 palette[0]），
    默认/"cycle" 按相位轮播；sentence_gap 覆盖 relayout 的 card 块句间停顿。

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
            src_scenes, segments, clip_durations, fps=style.FPS, gap=sentence_gap)
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
        is_static = bool(sc.get("static"))
        j = i
        while (j < n and src_scenes[j].get("kind") == "ball_exercise"
               and bool(src_scenes[j].get("static")) == is_static):
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

    # F3：弹性时间轴模式下，把所有「运动↔静止」球场景边界吸附到「球过中点」栅格（含组间天然
    # 静止 run 的边界）。非弹性模式保持原轴量化的既有行为（生产恒走弹性模式，见 handler）。
    if retimed is not None:
        _snap_ball_boundaries(scenes, period=global_period, fps=style.FPS)

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
