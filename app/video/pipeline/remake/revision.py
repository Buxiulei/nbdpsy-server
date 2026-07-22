"""成片修订能力 v1 纯逻辑层（spec §B）：自然语言意见 → EditOp 清单 → 应用到台词/参数覆盖。

本模块只做纯逻辑，不碰 job_store / tasks / api（B4 接线由集成任务做）。三件产物：
  1. parse_instructions —— 走 llm_chat 把自然语言意见解析成 EditOp dict 列表；
  2. validate_edit_plan —— 校验 EditOp 清单（op type / index 越界 / scene_id / 未知键）；
  3. apply_edits（B2）—— 纯函数把 EditOp 作用到 rewritten 副本 + 写参数覆盖结构。

EditOp v1 类型（逐字，spec §B2）：
  - script_edit   {index, new_text}          改某句 zh
  - script_delete {index}                    删某句
  - script_insert {after_index, text}        在某句后插新句
  - card_edit     {scene_id, title?, body?}  改卡片文案
  - ball_style    {y_ratio?|palette?|period_s?|color_mode?}   全局球段参数
  - global_param  {sentence_gap?|disclaimer_text?|closing_line?}  全局参数
"""
import json
import logging

from app.video.providers import llm_chat

logger = logging.getLogger(__name__)

# op type → 该类型的字段契约。required=必填字段及类型；any_of=允许键集合（至少命中一个）。
# 校验层据此判「非法 op type」「缺必填字段」「未知键」；apply_edits 据此分发。
_SCRIPT_INDEX_FIELDS = {
    "script_edit": "index",
    "script_delete": "index",
    "script_insert": "after_index",
}
_BALL_STYLE_KEYS = ("y_ratio", "palette", "period_s", "color_mode")
_BALL_COLOR_MODES = ("cycle", "single")     # color_mode 允许值：相位轮播 / 全程单色
_GLOBAL_PARAM_KEYS = ("sentence_gap", "disclaimer_text", "closing_line")
_VALID_OP_TYPES = (
    "script_edit", "script_delete", "script_insert",
    "card_edit", "ball_style", "global_param",
)
_CARD_SCENE_TYPES = ("title_card", "text_card")     # card_edit 仅适用于卡片场景

# 解析 prompt 里给 LLM 的操作 schema 说明（逐字覆盖 v1 六种 EditOp）
_OP_SCHEMA = """可用编辑操作（EditOp）——每条是一个 JSON 对象，"type" 字段指明类型：
- {"type": "script_edit", "index": <台词下标>, "new_text": "<改写后的中文台词>"}  改某一句台词
- {"type": "script_delete", "index": <台词下标>}                                删除某一句台词
- {"type": "script_insert", "after_index": <台词下标>, "text": "<新增的中文台词>"}  在某句之后插入新台词
- {"type": "card_edit", "scene_id": <场景 id>, "title": "<新标题(可选)>", "body": "<新正文(可选)>"}  改卡片文案（title/body 至少给一个）
- {"type": "ball_style", "y_ratio": <0~1 球心竖直位置>, "palette": [...], "period_s": <摆动周期秒>, "color_mode": "cycle|single"}  调整全局球段参数（只给需要改的键；color_mode=cycle 按相位轮播调色板，single 全程单色）
- {"type": "global_param", "sentence_gap": <句间停顿秒>, "disclaimer_text": "<须知文案>", "closing_line": "<结语>"}  调整全局参数（只给需要改的键）"""


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
        "改球段/全局参数用 ball_style/global_param。\n"
        "- index / after_index 必须是上面台词列表里真实存在的下标；scene_id 必须是真实场景 id。\n"
        "- 若完全无法把意见对应到任何编辑操作，输出空数组 [] 并在数组前用一句话说明原因。"
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


async def parse_instructions(instructions: str, rewritten: list[dict],
                             storyboard: dict) -> list[dict]:
    """自然语言修改意见 → EditOp dict 列表（走 llm_chat）。

    LLM 输出一个 JSON 数组。解析失败（无数组/非法 JSON/非数组）或空清单 → raise EditPlanError，
    .detail 为 LLM 原始说明（API 层据此返 400，见 spec §B2）。本函数只负责解析结构，
    语义校验（index 越界 / scene_id 不存在 / 未知键）由 validate_edit_plan 负责。
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
            if op.get("title") is None and op.get("body") is None:
                raise EditPlanError(
                    f"第 {pos} 条编辑（card_edit）：title/body 至少给一个")

        elif op_type == "ball_style":
            _validate_param_keys(op, _BALL_STYLE_KEYS, "ball_style", pos)
            cm = op.get("color_mode")           # 值域校验：非 cycle|single 会在 storyboard 静默走轮播
            if cm is not None and cm not in _BALL_COLOR_MODES:
                raise EditPlanError(
                    f"第 {pos} 条编辑（ball_style）：color_mode={cm!r} 非法"
                    f"（允许 {list(_BALL_COLOR_MODES)}）")

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

    card_edit / ball_style / global_param 写进 param_overrides（B4 消费）：
      结构 {"cards": {scene_id: {title?, body?}}, "ball": {y_ratio?...}, "global": {sentence_gap?...}}。
      card_edit 覆盖对应场景 content；ball 覆盖球段 style 参数；global 覆盖 relayout/composer 参数。

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
            card = param_overrides["cards"].setdefault(str(op["scene_id"]), {})
            if op.get("title") is not None:
                card["title"] = op["title"]
            if op.get("body") is not None:
                card["body"] = op["body"]
        elif op_type == "ball_style":
            for k in _BALL_STYLE_KEYS:
                if k in op:
                    param_overrides["ball"][k] = op[k]
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
