"""成片修订能力 v1 纯逻辑层（spec §B）：自然语言意见 → EditOp 清单 → 应用到台词/参数覆盖。

本模块只做纯逻辑，不碰 job_store / tasks / api（B4 接线由集成任务做）。三件产物：
  1. parse_instructions —— 走 llm_chat 把自然语言意见解析成 EditOp dict 列表；
  2. validate_edit_plan —— 校验 EditOp 清单（op type / index 越界 / scene_id / 未知键）；
  3. apply_edits（B2）—— 纯函数把 EditOp 作用到 rewritten 副本 + 写参数覆盖结构。

EditOp v1 类型（逐字，spec §B2）：
  - script_edit   {index, new_text}          改某句 zh
  - script_delete {index}                    删某句
  - script_insert {after_index, text}        在某句后插新句
  - card_edit     {scene_id, title?, body?, duration_s?}  改卡片文案 / 改卡片页面停留时长
  - ball_style    {y_ratio?|palette?|period_s?|color_mode?|color_cycle_periods?|set_rounds_range?}   全局球段参数
  - global_param  {sentence_gap?|disclaimer_text?|closing_line?}  全局参数
  - scene_edit    {scene_id, static: true}   把某个运动球段转静止（第三轮验收扩展）

第三轮验收扩展（用户反馈③④）：
  - scene_edit：把「喊开始前的孤立单摆动球段」转静止。端点解析时把 scene_id 溯源成 facts 源
    时间窗（scene_edit → static_source_spans，见 resolve_scene_edit_spans），落 param_overrides
    供 storyboard 强制转静止——绕开子 job storyboard 重建导致的场景 id 漂移。
  - ball_style.color_cycle_periods：长球段场景内恒色 → 每 N 个摆动周期沿调色板轮下一色
    （用户「每晃一组变色」= N=1），storyboard 消费。
  - card_edit.duration_s：改卡片页面停留时长（用户反馈②「须知卡停留太长」的真根因——卡片时长
    来自 facts 源区间，删句只是留白变多被弹性时间轴保住）。端点把 scene_id 溯源成 facts 卡片源
    时间窗（见 resolve_card_duration_spans），落 param_overrides.card_duration_overrides，relayout
    据源窗强制卡片目标时长、后续场景整体前移。校验保 duration_s ≥ 卡内台词自然排布最小需要。
"""
import json
import logging

from app.video.pipeline.remake import timeline
from app.video.providers import llm_chat

logger = logging.getLogger(__name__)

# op type → 该类型的字段契约。required=必填字段及类型；any_of=允许键集合（至少命中一个）。
# 校验层据此判「非法 op type」「缺必填字段」「未知键」；apply_edits 据此分发。
_SCRIPT_INDEX_FIELDS = {
    "script_edit": "index",
    "script_delete": "index",
    "script_insert": "after_index",
}
_BALL_STYLE_KEYS = ("y_ratio", "palette", "period_s", "color_mode",
                    "color_cycle_periods", "set_rounds_range")
_BALL_COLOR_MODES = ("cycle", "single")     # color_mode 允许值：相位轮播 / 全程单色
_GLOBAL_PARAM_KEYS = ("sentence_gap", "disclaimer_text", "closing_line")
_VALID_OP_TYPES = (
    "script_edit", "script_delete", "script_insert",
    "card_edit", "ball_style", "global_param", "scene_edit",
)
_CARD_SCENE_TYPES = ("title_card", "text_card")     # card_edit 仅适用于卡片场景
# scene_edit 允许键：type + scene_id + static（v1 只支持 static，留扩展位）+ static_source_spans
# （端点 resolve_scene_edit_spans baked 的溯源结果，允许其在场以便重校验不误伤）
_SCENE_EDIT_KEYS = ("type", "scene_id", "static", "static_source_spans")

# 解析 prompt 里给 LLM 的操作 schema 说明（逐字覆盖 v1 六种 EditOp）
_OP_SCHEMA = """可用编辑操作（EditOp）——每条是一个 JSON 对象，"type" 字段指明类型：
- {"type": "script_edit", "index": <台词下标>, "new_text": "<改写后的中文台词>"}  改某一句台词
- {"type": "script_delete", "index": <台词下标>}                                删除某一句台词
- {"type": "script_insert", "after_index": <台词下标>, "text": "<新增的中文台词>"}  在某句之后插入新台词
- {"type": "card_edit", "scene_id": <场景 id>, "title": "<新标题(可选)>", "body": "<新正文(可选)>", "duration_s": <正数秒(可选)>}  改卡片文案 / 改卡片页面停留时长（title/body/duration_s 至少给一个；用户说「这张卡/须知/结尾页停留太长、缩短到X秒、翻太快、多停一会」就用 duration_s 给目标秒数）
- {"type": "ball_style", "y_ratio": <0~1 球心竖直位置>, "palette": [...], "period_s": <摆动周期秒>, "color_mode": "cycle|single", "color_cycle_periods": <正整数N>, "set_rounds_range": [<最少轮数>, <最多轮数>]}  调整全局球段参数（只给需要改的键；color_mode=cycle 按相位轮播调色板，single 全程单色；color_cycle_periods=N 让运动球每晃 N 个周期就换调色板下一色，用户说「每晃一组变色」用 N=1、「变色快一点」用更小的 N、「变色慢一点」用更大的 N；set_rounds_range=[最少,最多] 把每一个晃动组的双侧刺激重编排到该轮数区间——一左一右算一轮（一个来回=一个周期），临床上每组常见 24-40 轮，用户说「每组/所有晃动组晃 24 到 40 轮」「每组轮数太少/每组太短/每组晃的次数不够」「一组要晃够多少个来回」这类要求剂量的说法时用 set_rounds_range=[24,40]）
- {"type": "global_param", "sentence_gap": <句间停顿秒>, "disclaimer_text": "<须知文案>", "closing_line": "<结语>"}  调整全局参数（只给需要改的键）
- {"type": "scene_edit", "scene_id": <运动球段场景 id>, "static": true}  把指定的某个运动球段（小球在晃动的那种）转成静止不动的居中球（用户说「这段球别晃 / 喊开始前的球先别动 / 开头这段静止 / 把某段小球停下来」时用，scene_id 必须选下面场景列表里「静止=False」的球段）"""


class EditPlanError(Exception):
    """意见解析 / 编辑清单校验失败。

    .detail 承载对外可展示的明细：解析失败时为 LLM 原始说明（API 层原样透传给用户，
    见 spec §B2「解析失败/空清单 → 400 带 LLM 原始说明」）；校验失败时为具体违规描述。
    """

    def __init__(self, detail: str):
        super().__init__(detail)
        self.detail = detail


def _format_script(rewritten: list[dict]) -> str:
    """台词全文（带下标与时间）——供 LLM 定位「第几句」。no_dub 句标注（不朗读仅显示）。"""
    lines = []
    for i, seg in enumerate(rewritten):
        start = float(seg.get("start", seg.get("orig_start", 0.0)) or 0.0)
        end = float(seg.get("end", seg.get("orig_end", 0.0)) or 0.0)
        tag = "（不配音）" if seg.get("no_dub") else ""
        lines.append(f"[{i}] ({start:.1f}-{end:.1f}s){tag} {seg.get('zh', '')}")
    return "\n".join(lines) if lines else "（无台词）"


def _format_scenes(storyboard: dict) -> str:
    """分镜场景摘要（id/类型/时间/参数或文案摘要）——供 LLM 定位 card_edit 的 scene_id。"""
    lines = []
    for sc in storyboard.get("scenes") or []:
        sid = sc.get("id")
        stype = sc.get("type")
        t0, t1 = float(sc.get("t0", 0.0)), float(sc.get("t1", 0.0))
        if stype in _CARD_SCENE_TYPES:
            content = sc.get("content") or {}
            summary = f"标题={content.get('title', '')!r} 正文={content.get('body', '')!r}"
        else:                                       # 球段等：摘参数
            params = sc.get("params") or {}
            summary = (f"球色={params.get('ball_color', '')} 周期={params.get('period_s', '')} "
                       f"静止={bool(params.get('static'))}")
        lines.append(f"场景 {sid} 类型={stype} [{t0:.1f}-{t1:.1f}s] {summary}")
    return "\n".join(lines) if lines else "（无场景）"


def _build_parse_prompt(instructions: str, rewritten: list[dict], storyboard: dict) -> str:
    """拼装解析 prompt：意见 + 台词全文（下标/时间） + 分镜场景摘要 + 操作 schema。"""
    return (
        "你是 NBDpsy 视频修订助手。用户对一支已生成的心理科普引导视频提出了修改意见，"
        "请把意见翻译成结构化的编辑操作清单（EditOp）。\n\n"
        f"用户的修改意见：\n{instructions}\n\n"
        f"当前台词（下标从 0 开始，附时间轴）：\n{_format_script(rewritten)}\n\n"
        f"当前分镜场景：\n{_format_scenes(storyboard)}\n\n"
        f"{_OP_SCHEMA}\n\n"
        "规则：\n"
        "- 只输出一个 JSON 数组，数组元素是上述 EditOp 对象，不要输出任何解释文字。\n"
        "- 意见涉及多处修改就输出多个 EditOp；改台词用 script_*，改卡片用 card_edit，"
        "改球段/全局参数用 ball_style/global_param；把某段晃动的球停成静止用 scene_edit；"
        "让球「每晃一组变色 / 变色快点慢点」用 ball_style.color_cycle_periods；"
        "让「每个晃动组晃够多少轮 / 每组太短太少 / 一左一右算一轮 / 每组 24 到 40 轮」用 "
        "ball_style.set_rounds_range；"
        "「某张卡片/页面停留太长太短、缩短到X秒、翻太快」用 card_edit.duration_s。\n"
        "- index / after_index 必须是上面台词列表里真实存在的下标；scene_id 必须是真实场景 id"
        "（scene_edit 的 scene_id 必须选「静止=False」的运动球段）。\n"
        "- 若完全无法把意见对应到任何编辑操作，输出空数组 [] 并在数组前用一句话说明原因。"
    )


def _build_reflection_prompt(instructions: str, first_ops: list[dict],
                             rewritten: list[dict], storyboard: dict) -> str:
    """拼装「覆盖度反思」prompt：原始意见全文 + 首轮 EditOp 清单 + 台词/场景上下文 + 操作 schema。

    为什么要第二轮：组合意见（多处独立要求写在一句话里）首轮解析常漏其一——实证两次整轮出片浪费：
    ①「使用须知改正文+缩短停留」丢 duration_s；②「结语改时长+全组 24-40 轮」丢 set_rounds_range。
    第二轮让 LLM 逐条核对「意见里每一处独立要求是否已有对应 EditOp」，只补被漏的项。
    下标/scene_id 的上下文与首轮完全一致（复用同一 _format_script / _format_scenes，不重新发明），
    LLM 补出的 op 定位口径与首轮一致，可直接并入首轮清单。
    """
    first_json = json.dumps(first_ops, ensure_ascii=False, indent=2)
    return (
        "你是 NBDpsy 视频修订助手的复核环节。上一轮已把用户意见初步解析成一份 EditOp 清单，"
        "但组合意见里常有某一处独立要求被漏掉。请你逐条核对：用户意见中的每一处独立要求，"
        "是否都已经有对应的 EditOp。\n\n"
        f"用户的原始修改意见：\n{instructions}\n\n"
        f"上一轮已解析出的 EditOp 清单：\n{first_json}\n\n"
        f"当前台词（下标从 0 开始，附时间轴）：\n{_format_script(rewritten)}\n\n"
        f"当前分镜场景：\n{_format_scenes(storyboard)}\n\n"
        f"{_OP_SCHEMA}\n\n"
        "规则：\n"
        "- 只针对**被遗漏**的要求补充 EditOp，不要重复上一轮已经覆盖的操作，也不要修改上一轮的操作。\n"
        "- 每一处遗漏对应一个补充 EditOp，index / after_index / scene_id 必须与上面的台词/场景列表一致。\n"
        "- 只输出一个 JSON 数组（补充的 EditOp）；若逐条核对后确认没有任何遗漏，输出空数组 []。"
        "不要输出任何解释文字。"
    )


def _extract_json_array(content: str):
    """从 LLM 输出里抠出 JSON 数组（首 [ 到末 ]，与 rewriter 的 {}/[] 风格一致）。

    返回 list（含空 list）或 None（无数组 / 解析失败 / 非数组）。
    """
    if not content:
        return None
    start, end = content.find("["), content.rfind("]")
    if start < 0 or end <= start:
        return None
    try:
        data = json.loads(content[start:end + 1])
    except (json.JSONDecodeError, ValueError):
        return None
    return data if isinstance(data, list) else None


def _op_dedup_key(op: dict):
    """反思补充项与首轮去重用的身份键（同 type + 定位键 → 判为重复，首轮优先）。

    定位键取「能区分不同编辑目标」的最小信息，避免把合法补充误判成重复而丢掉——尤其两个实证案例：
      - card_edit：内容编辑（title/body）与时长编辑（duration_s）是同一张卡片上两个正交子功能
        （apply_edits 也分开落 cards / card_duration_overrides），故键含子功能标签；否则「同卡片
        改正文 + 缩短停留」的 duration_s 补充项会被首轮 body 编辑挡掉（实证案例①）。
      - ball_style / global_param：全局 op 无位置锚，按其携带的参数键集合定位；首轮已覆盖的参数键
        不再补，缺的参数键才补——否则「结语改 + 全组 24-40 轮」里的 set_rounds_range 补充项会被
        首轮的另一个 ball_style（若有）整体挡掉（实证案例②）。
    其余按天然定位键：script_edit/script_delete 用 index，script_insert 用 (after_index, text)
    （同一锚点多插句各自独立），scene_edit 用 scene_id。
    """
    t = op.get("type")
    if t in ("script_edit", "script_delete"):
        return (t, op.get("index"))
    if t == "script_insert":
        return (t, op.get("after_index"), op.get("text"))
    if t == "card_edit":
        return (t, op.get("scene_id"),
                "duration" if op.get("duration_s") is not None else "content")
    if t == "scene_edit":
        return (t, op.get("scene_id"))
    if t in ("ball_style", "global_param"):
        return (t, frozenset(k for k in op if k != "type"))
    return (t,)


async def _reflect_coverage(instructions: str, first_ops: list[dict],
                            rewritten: list[dict], storyboard: dict) -> list[dict]:
    """覆盖度反思环：第二轮核对意见每处独立要求是否已有对应 EditOp，补出被漏项（已对首轮去重）。

    返回「补充 EditOp 列表」（已剔除与首轮重复的项）。三种退化路径均返回 []（不阻塞既有能力）：
      - 无遗漏：反思输出空数组 → 正常返回 []（首轮清单即完整）；
      - 反思 LLM 调用失败 / 超时（抛异常）→ 记 warning 后返回 []（降级为首轮结果）；
      - 反思输出不可解析（无 JSON 数组 / 非数组）→ 记 warning 后返回 []（降级为首轮结果）。
    补充项**不在此处做语义校验**：并入首轮清单后由调用方交既有 validate_edit_plan 统一校验
    （补充项非法 → 整体 EditPlanError，语义与首轮一致）。温度沿用首轮（0.0）。
    """
    try:
        prompt = _build_reflection_prompt(instructions, first_ops, rewritten, storyboard)
        content = (await llm_chat(messages=[{"role": "user", "content": prompt}],
                                 temperature=0.0) or "").strip()
    except Exception:                       # LLM 失败/超时 → 降级首轮（不阻塞既有能力）
        logger.warning("覆盖度反思环 LLM 调用失败，降级为首轮解析结果", exc_info=True)
        return []
    extra = _extract_json_array(content)
    if extra is None:                       # 输出不可解析 → 降级首轮
        logger.warning("覆盖度反思环输出不可解析，降级为首轮解析结果：%s", content[:200])
        return []
    if not extra:                           # 反思判定无遗漏
        return []
    # 对首轮去重（同 type + 定位键，首轮优先）；补充项内部也去重。非 dict 项原样保留，
    # 交下游 validate_edit_plan 报错（补充项非法整体报错，语义不变）。
    seen = {_op_dedup_key(op) for op in first_ops if isinstance(op, dict)}
    supplements: list[dict] = []
    for op in extra:
        if not isinstance(op, dict):
            supplements.append(op)
            continue
        key = _op_dedup_key(op)
        if key in seen:                     # 首轮已覆盖 → 丢弃反思重复项
            continue
        seen.add(key)
        supplements.append(op)
    return supplements


async def parse_instructions(instructions: str, rewritten: list[dict],
                             storyboard: dict) -> list[dict]:
    """自然语言修改意见 → EditOp dict 列表（走 llm_chat + 覆盖度反思环）。

    首轮 LLM 输出一个 JSON 数组。解析失败（无数组/非法 JSON/非数组）或空清单 → raise EditPlanError，
    .detail 为 LLM 原始说明（API 层据此返 400，见 spec §B2）。

    首轮出清单后加一轮「覆盖度反思」（_reflect_coverage）：让 LLM 逐条核对意见里每处独立要求是否
    已有对应 EditOp，补出被漏的项（组合意见丢项已三次浪费整轮出片）。补充项去重后 append 进清单，
    并整体交既有 validate_edit_plan 校验（补充项非法 → 整体 EditPlanError，语义与首轮一致）。
    反思环失败/超时/输出不可解析时降级为首轮结果，不阻塞既有能力（见 _reflect_coverage）。

    本函数只负责解析结构；首轮结果的语义校验（index 越界 / scene_id 不存在 / 未知键）仍由调用方的
    validate_edit_plan 负责（无补充项时本函数不重复校验，保持既有链路语义不变）。
    """
    prompt = _build_parse_prompt(instructions, rewritten, storyboard)
    # 换 import 面：源 get_llm(_LLM_KEY).chat(..., urgent=True).content → 薄 provider llm_chat
    # 直返字符串（provider 无 urgent 概念，去掉）。
    content = (await llm_chat(messages=[{"role": "user", "content": prompt}],
                             temperature=0.0) or "").strip()
    ops = _extract_json_array(content)
    if ops is None:
        raise EditPlanError(content or "LLM 未返回可解析的编辑清单")
    if not ops:
        raise EditPlanError(content or "未能从修改意见中识别出任何编辑操作")
    # 覆盖度反思环：核对遗漏并补充。补充项并入后走既有 validate_edit_plan（补充项非法即整体报错）；
    # 无补充项时按原链路直接返回首轮结果（不重复校验，语义不变）。
    supplements = await _reflect_coverage(instructions, ops, rewritten, storyboard)
    if supplements:
        merged = ops + supplements
        validate_edit_plan(merged, rewritten, storyboard)
        return merged
    return ops


def _require_int(value, field: str, pos: int) -> int:
    """取整型下标（拒绝 bool / 非整），越界前先保证类型正确。"""
    if isinstance(value, bool) or not isinstance(value, int):
        raise EditPlanError(f"第 {pos} 条编辑：{field} 必须是整数（实际 {value!r}）")
    return value


def validate_edit_plan(ops: list[dict], rewritten: list[dict],
                       storyboard: dict) -> None:
    """校验 EditOp 清单，非法即 raise EditPlanError 带明细（fail-fast，spec §B2/§错误处理）。

    检查：非法 op type / 缺必填字段 / index(after_index) 越界 / scene_id 不存在或非卡片场景 /
    ball_style|global_param 未知键或空。全部合法则返回 None。
    """
    n = len(rewritten)
    scene_by_id = {sc.get("id"): sc for sc in (storyboard.get("scenes") or [])}
    for pos, op in enumerate(ops):
        if not isinstance(op, dict):
            raise EditPlanError(f"第 {pos} 条编辑不是对象：{op!r}")
        op_type = op.get("type")
        if op_type not in _VALID_OP_TYPES:
            raise EditPlanError(f"第 {pos} 条编辑：非法操作类型 {op_type!r}")

        if op_type in _SCRIPT_INDEX_FIELDS:
            field = _SCRIPT_INDEX_FIELDS[op_type]
            if field not in op:
                raise EditPlanError(f"第 {pos} 条编辑（{op_type}）缺字段 {field}")
            idx = _require_int(op[field], field, pos)
            if not (0 <= idx < n):
                raise EditPlanError(
                    f"第 {pos} 条编辑（{op_type}）：{field}={idx} 越界（台词共 {n} 句）")
            if op_type == "script_edit" and not isinstance(op.get("new_text"), str):
                raise EditPlanError(f"第 {pos} 条编辑（script_edit）缺 new_text 或非字符串")
            if op_type == "script_insert" and not isinstance(op.get("text"), str):
                raise EditPlanError(f"第 {pos} 条编辑（script_insert）缺 text 或非字符串")

        elif op_type == "card_edit":
            if "scene_id" not in op:
                raise EditPlanError(f"第 {pos} 条编辑（card_edit）缺字段 scene_id")
            sid = op["scene_id"]
            if sid not in scene_by_id:
                raise EditPlanError(f"第 {pos} 条编辑（card_edit）：场景 id {sid!r} 不存在")
            if scene_by_id[sid].get("type") not in _CARD_SCENE_TYPES:
                raise EditPlanError(
                    f"第 {pos} 条编辑（card_edit）：场景 {sid} 不是卡片场景，无法改文案")
            if (op.get("title") is None and op.get("body") is None
                    and op.get("duration_s") is None):
                raise EditPlanError(
                    f"第 {pos} 条编辑（card_edit）：title/body/duration_s 至少给一个")
            ds = op.get("duration_s")               # 卡片页面停留目标时长（可选）
            if ds is not None:
                if isinstance(ds, bool) or not isinstance(ds, (int, float)) or ds <= 0:
                    raise EditPlanError(
                        f"第 {pos} 条编辑（card_edit）：duration_s={ds!r} 必须是正数（秒）")
                # 下限 = 卡内配音句紧排自然需要（LEAD + Σ时长 + 句间GAP + TAIL；纯读卡=LEAD+TAIL）。
                # 短于此会裁掉台词，fail-fast 并告知最小值（relayout 覆盖路径按同一紧排公式排布）。
                min_needed = timeline.min_card_duration(
                    _card_dub_durations(scene_by_id[sid], rewritten))
                if ds < min_needed - 1e-6:
                    raise EditPlanError(
                        f"第 {pos} 条编辑（card_edit）：duration_s={ds} 太短，"
                        f"卡内台词自然排布至少需要 {min_needed:.1f}s")

        elif op_type == "ball_style":
            _validate_param_keys(op, _BALL_STYLE_KEYS, "ball_style", pos)
            cm = op.get("color_mode")           # 值域校验：非 cycle|single 会在 storyboard 静默走轮播
            if cm is not None and cm not in _BALL_COLOR_MODES:
                raise EditPlanError(
                    f"第 {pos} 条编辑（ball_style）：color_mode={cm!r} 非法"
                    f"（允许 {list(_BALL_COLOR_MODES)}）")
            ccp = op.get("color_cycle_periods")  # 正整数校验：非正/非整会在 storyboard 切出零长段
            if ccp is not None and (isinstance(ccp, bool) or not isinstance(ccp, int)
                                    or ccp < 1):
                raise EditPlanError(
                    f"第 {pos} 条编辑（ball_style）：color_cycle_periods={ccp!r} 必须是正整数")
            srr = op.get("set_rounds_range")     # 摆动组临床剂量区间 [min,max]：长度 2 + 正整数 + min≤max
            if srr is not None:
                if (not isinstance(srr, (list, tuple)) or len(srr) != 2
                        or any(isinstance(x, bool) or not isinstance(x, int) or x < 1
                               for x in srr)):
                    raise EditPlanError(
                        f"第 {pos} 条编辑（ball_style）：set_rounds_range={srr!r} 必须是两个正整数"
                        f"[最少轮数, 最多轮数]")
                if srr[0] > srr[1]:
                    raise EditPlanError(
                        f"第 {pos} 条编辑（ball_style）：set_rounds_range 最少轮数 {srr[0]} "
                        f"不能大于最多轮数 {srr[1]}")

        elif op_type == "scene_edit":
            if "scene_id" not in op:
                raise EditPlanError(f"第 {pos} 条编辑（scene_edit）缺字段 scene_id")
            sid = op["scene_id"]
            if sid not in scene_by_id:
                raise EditPlanError(f"第 {pos} 条编辑（scene_edit）：场景 id {sid!r} 不存在")
            if scene_by_id[sid].get("type") != "ball_exercise":
                raise EditPlanError(
                    f"第 {pos} 条编辑（scene_edit）：场景 {sid} 不是球段，无法转静止")
            if op.get("static") is not True:            # v1 只支持 static=true（留扩展位）
                raise EditPlanError(
                    f"第 {pos} 条编辑（scene_edit）：v1 仅支持 static=true（实际 {op.get('static')!r}）")
            unknown = [k for k in op if k not in _SCENE_EDIT_KEYS]
            if unknown:
                raise EditPlanError(
                    f"第 {pos} 条编辑（scene_edit）：未知参数键 {unknown}"
                    f"（允许 {[k for k in _SCENE_EDIT_KEYS if k != 'type']}）")

        elif op_type == "global_param":
            _validate_param_keys(op, _GLOBAL_PARAM_KEYS, "global_param", pos)


def _validate_param_keys(op: dict, allowed: tuple, op_type: str, pos: int) -> None:
    """ball_style / global_param 键校验：无未知键、至少命中一个允许键。"""
    keys = [k for k in op if k != "type"]
    unknown = [k for k in keys if k not in allowed]
    if unknown:
        raise EditPlanError(
            f"第 {pos} 条编辑（{op_type}）：未知参数键 {unknown}（允许 {list(allowed)}）")
    if not keys:
        raise EditPlanError(f"第 {pos} 条编辑（{op_type}）：未给任何参数")


# -------- scene_edit 溯源：父分镜场景 id → facts 源时间窗（抗子 job 场景 id 漂移，第三轮扩展） --------

def _card_offset_anchors(storyboard: dict, facts: dict) -> list[tuple[float, float]]:
    """配对父分镜 still_image 场景 ↔ facts 非球场景（1:1 顺序），产出球区 retimed→source 反查锚点。

    弹性时间轴里卡片块会被重排（改时长），而球块保时长——故「同一连续球区内 source = retimed - offset」，
    offset 由该球区之前最近那张卡片的边界确定。锚点 = (卡片在父分镜时间轴的 t1, 该处 offset)，
    offset = 父分镜卡片 t1 - facts 源卡片 t1。build_storyboard 对每个非球 facts 场景恰出一个
    still_image 场景（含 other 降级卡），故两序列 1:1 顺序对齐。按时间序返回。
    """
    facts_cards = [sc for sc in (facts.get("scenes") or [])
                   if sc.get("kind") != "ball_exercise"]
    sb_cards = [sc for sc in (storyboard.get("scenes") or [])
                if sc.get("renderer") == "still_image"]
    return [(float(ssc.get("t1", 0.0)),
             float(ssc.get("t1", 0.0)) - float(fsc.get("t1", 0.0)))
            for fsc, ssc in zip(facts_cards, sb_cards)]


def _retimed_to_source(t: float, anchors: list[tuple[float, float]]) -> float:
    """父分镜时间轴时刻 t → facts 源时间：落到 t 之前最近的卡片锚点 offset，source = t - offset。"""
    offset = 0.0
    for rt1, off in anchors:
        if rt1 <= t + 1e-6:
            offset = off
        else:
            break
    return t - offset


def resolve_scene_edit_spans(ops: list[dict], storyboard: dict,
                             facts: dict | None) -> None:
    """把每条 scene_edit 的 scene_id 就地解析成 facts 源时间窗，baked 进 op["static_source_spans"]。

    为什么溯源到源时间窗而非直接传场景 id：子 revision job 的 storyboard 是重建的——台词增删 +
    语音窗 carve 变化会让场景 id 与父 storyboard 漂移，按 id / 父 retimed 时间都对不上。scene_facts
    全程继承不变，是唯一稳定锚。映射走卡片锚点 offset（见 _card_offset_anchors）：球区内
    source = retimed - offset，取场景中点的 offset 同时换算首尾，保证 span 落在同一球区不跨卡片边界。

    在端点解析（父 storyboard 已加载）后 bake 进不可变 edit_plan：子 rewrite 崩溃重入天然幂等（I1），
    无需子 worker 再载父分镜；解析结果随 edit_plan 落库、进 meta.revision 可查。facts 缺失（理论上
    不发生，父 remake 完成必有 scene_facts）时退化为恒等（retimed==source，非弹性父片精确）。
    """
    anchors = _card_offset_anchors(storyboard, facts) if facts else []
    scene_by_id = {sc.get("id"): sc for sc in (storyboard.get("scenes") or [])}
    for op in ops:
        if op.get("type") != "scene_edit":
            continue
        sc = scene_by_id.get(op.get("scene_id"))
        if sc is None:                          # 存在性由 validate_edit_plan 兜住，防御式跳过
            continue
        t0, t1 = float(sc.get("t0", 0.0)), float(sc.get("t1", 0.0))
        off = ((t0 + t1) / 2.0) - _retimed_to_source((t0 + t1) / 2.0, anchors)
        op["static_source_spans"] = [[t0 - off, t1 - off]]


# -------- card_edit.duration_s 溯源：卡片场景 id → facts 卡片源时间窗（改页面停留时长，反馈②） --------

def _card_dub_durations(card_scene: dict, rewritten: list[dict]) -> list[float]:
    """父分镜卡片场景 [t0,t1] 内的配音句自然时长序列（供 duration_s 下限校验）。

    落入卡片时间窗（父新轴 start ∈ [t0,t1)）的非 no_dub 句即属该卡；其 end-start = relayout 排下的
    自然配音时长（父层 rate=1.0 时长，与子重建一致）。no_dub 句（须知全文展示）不占时间轴、不计入。
    """
    t0, t1 = float(card_scene.get("t0", 0.0)), float(card_scene.get("t1", 0.0))
    out: list[float] = []
    for seg in rewritten:
        if seg.get("no_dub"):
            continue
        start = seg.get("start")
        if start is None:
            continue
        start = float(start)
        if t0 - 1e-6 <= start < t1:
            out.append(float(seg.get("end", start)) - start)
    return out


def resolve_card_duration_spans(ops: list[dict], storyboard: dict,
                                facts: dict | None) -> None:
    """把每条带 duration_s 的 card_edit 的 scene_id 就地溯源成 facts 卡片源时间窗，
    baked 进 op["duration_source_span"]（[src_t0, src_t1]）。

    与 scene_edit 同理由：子 revision job storyboard 重建后卡片场景 id 会随台词增删漂移，故落到
    稳定的 facts 卡片源窗——build_storyboard 对每个非球 facts 场景恰出一个 still_image 卡片，二者
    1:1 顺序对应，据序反查父分镜卡片 id → facts 卡片 [t0,t1]。relayout 按源窗匹配卡片块强制目标
    时长。facts 缺失（理论上不发生）时退化用父分镜卡片自身 [t0,t1]（非弹性父片精确）。
    """
    facts_cards = ([sc for sc in (facts.get("scenes") or [])
                    if sc.get("kind") != "ball_exercise"] if facts else [])
    sb_cards = [sc for sc in (storyboard.get("scenes") or [])
                if sc.get("renderer") == "still_image"]
    window_by_id: dict = {}
    for idx, ssc in enumerate(sb_cards):
        if idx < len(facts_cards):                  # facts 卡片源窗（稳定锚）
            fsc = facts_cards[idx]
            window_by_id[ssc.get("id")] = (float(fsc.get("t0", 0.0)),
                                           float(fsc.get("t1", 0.0)))
        else:                                       # 防御：facts 缺失/数量不齐 → 用分镜卡片自身区间
            window_by_id[ssc.get("id")] = (float(ssc.get("t0", 0.0)),
                                           float(ssc.get("t1", 0.0)))
    for op in ops:
        if op.get("type") != "card_edit" or op.get("duration_s") is None:
            continue
        win = window_by_id.get(op.get("scene_id"))
        if win is None:                             # 存在性由 validate_edit_plan 兜住，防御式跳过
            continue
        op["duration_source_span"] = [win[0], win[1]]


# ---------------- B2: apply_edits（EditOp → rewritten 副本 + 参数覆盖结构） ----------------

def _make_inserted(neighbor: dict, text: str) -> dict:
    """script_insert 新句：继承邻句全部字段（含 orig_* 时间锚点），覆写 zh/en/no_dub。

    与 rewriter._append_closing_line 追加句同款做法（dict(邻句) → 改 zh、清 en、强制配音）：
    orig_* 继承邻句供下游 relayout 归块；start/end 只是占位，storyboard.relayout 会据
    orig_start + clip_durations 重算新轴。新句必须朗读故 no_dub=False（不继承邻句的 no_dub）。
    """
    seg = dict(neighbor)
    seg["zh"] = text
    seg["en"] = ""                              # 新增句无英文源，置空避免双语 md 错配
    seg["no_dub"] = False                       # 新增句必须配音
    return seg


def _check_script_index(value, field: str, n: int) -> int:
    """apply 阶段的 index 兜底 fail-fast（正常已过 validate_edit_plan，此处防直接调用）。"""
    if isinstance(value, bool) or not isinstance(value, int) or not (0 <= value < n):
        raise EditPlanError(f"apply_edits：{field}={value!r} 越界（台词共 {n} 句）")
    return value


def apply_edits(ops: list[dict], rewritten: list[dict], *,
                param_overrides: dict) -> tuple[list[dict], dict]:
    """把 EditOp 清单作用到 rewritten 副本 + 写参数覆盖结构（spec §B2）。

    对 rewritten 是纯函数（返回全新列表，入参不被修改）；param_overrides 原地累积
    （setdefault 三键后就地写入并返回同一对象——同 scene_id 多 card_edit、多个 ball_style
    键都在同一 overrides 上累加，不覆盖已有键）。

    script_* 作用于 rewritten 副本：
      - script_edit 改 index 句 zh；script_delete 删 index 句；
      - script_insert 在 after_index 句后插新句（orig_* 继承邻句 + no_dub=False）。
      - 多 op 混合不错位：所有下标均按**原始列表**定位（预解析成 edits/deletes/inserts 映射），
        再单趟重建——delete 不移动其它 op 的下标语义，delete/insert 组合互不干扰。

    card_edit / ball_style / global_param / scene_edit 写进 param_overrides（B4 消费）：
      结构 {"cards": {scene_id: {title?, body?}}, "ball": {y_ratio?...}, "global": {sentence_gap?...}
      [, "card_duration_overrides": [[src_t0,src_t1,new_dur],...]]}。
      card_edit 覆盖对应场景 content（title/body），其 duration_s 另累积进 card_duration_overrides
      （relayout 强制卡片目标时长）；ball 覆盖球段 style 参数；global 覆盖 relayout/composer 参数；
      scene_edit 把端点 baked 的 static_source_spans 累积进 ball（storyboard 据此把命中源窗的运动球
      段强制转静止）。card_duration_overrides 键仅在有 duration_s 覆盖时才出现（无覆盖时结构不变）。

    非法 index → fail-fast EditPlanError（scene_id 合法性由 validate_edit_plan 兜住——本函数
    无 storyboard 上下文）。入参 rewritten 不被修改（先深拷贝各句）；param_overrides 原地更新并返回。
    """
    n = len(rewritten)
    param_overrides.setdefault("cards", {})
    param_overrides.setdefault("ball", {})
    param_overrides.setdefault("global", {})

    edits: dict[int, str] = {}                  # 原始下标 → 新 zh
    deletes: set[int] = set()                   # 原始下标
    inserts: dict[int, list[str]] = {}          # after_index → [新句文本...]（保序）
    for op in ops:
        op_type = op.get("type")
        if op_type == "script_edit":
            i = _check_script_index(op.get("index"), "index", n)
            edits[i] = op["new_text"]
        elif op_type == "script_delete":
            deletes.add(_check_script_index(op.get("index"), "index", n))
        elif op_type == "script_insert":
            i = _check_script_index(op.get("after_index"), "after_index", n)
            inserts.setdefault(i, []).append(op["text"])
        elif op_type == "card_edit":
            # 键归一化为 str：JSON 回读的继承种子键是 str、LLM 给 int——不归一化会键分裂，
            # 同 scene 跨层编辑的父层字段静默丢（消费侧 _apply_card_overrides 也按 str 匹配）。
            # 只在给了 title/body 时创建 cards 条目（纯 duration_s 编辑不留空 content 覆盖）。
            if op.get("title") is not None or op.get("body") is not None:
                card = param_overrides["cards"].setdefault(str(op["scene_id"]), {})
                if op.get("title") is not None:
                    card["title"] = op["title"]
                if op.get("body") is not None:
                    card["body"] = op["body"]
            # duration_s：端点 resolve_card_duration_spans 已 baked facts 卡片源窗；累积进
            # card_duration_overrides（relayout 据源窗定位卡片块强制目标时长，抗子 job 卡片 id 漂移）。
            # 仅在有 duration_s 时才创建该键，保持无覆盖时 overrides 三键结构不变。
            if op.get("duration_s") is not None and op.get("duration_source_span"):
                span = op["duration_source_span"]
                param_overrides.setdefault("card_duration_overrides", []).append(
                    [float(span[0]), float(span[1]), float(op["duration_s"])])
        elif op_type == "ball_style":
            for k in _BALL_STYLE_KEYS:
                if k in op:
                    param_overrides["ball"][k] = op[k]
        elif op_type == "scene_edit":
            # 端点 resolve_scene_edit_spans 已 baked 源时间窗；累积进 ball.static_source_spans
            # （多条 scene_edit 各自贡献一窗，同一 overrides 上追加不覆盖），storyboard 强制转静止。
            spans = op.get("static_source_spans") or []
            if spans:
                param_overrides["ball"].setdefault("static_source_spans", []).extend(
                    [[float(s[0]), float(s[1])] for s in spans])
        elif op_type == "global_param":
            for k in _GLOBAL_PARAM_KEYS:
                if k in op:
                    param_overrides["global"][k] = op[k]
        else:
            raise EditPlanError(f"apply_edits：非法操作类型 {op_type!r}")

    # 单趟按原始下标重建：非删句（含改写）保留，随后追加锚在该下标后的插入句。
    # after_index 指向的邻句即便被删，插入句仍落在该位置（orig_* 继承已删邻句的时间锚点仍有效）。
    new_segs: list[dict] = []
    for i, seg in enumerate(rewritten):
        if i not in deletes:
            s = dict(seg)
            if i in edits:
                s["zh"] = edits[i]
            new_segs.append(s)
        for text in inserts.get(i, []):
            new_segs.append(_make_inserted(seg, text))
    return new_segs, param_overrides
