"""台词口播本土化改写（spec §7，wave5 重构）：翻译产物 → NBDpsy 自然口播稿。

场景定调：这是 NBDpsy 自助 EMDR 干预的引导语音，听者闭眼跟随，语言的任何生硬都会造成
「出戏」，破坏干预效果。故改写目标不是「对齐原文字数」而是「表达含义、去翻译腔」。

两段式（弹性时间轴后无 slot 长度约束）：
  1. 初改：按口播本土化简报逐句改写（术语锁定 / 去人名 / 平台机制话术本地化 / 口头确认语温和化）；
  2. 反思环：对初改稿再过一次 LLM 自查——逐句挑翻译腔与出戏感（欧化句式/直译痕迹/质问语气/
     不自然书面语），发现即改、没问题保留。反思失败保留初改文本（优化不是闸门）。
"""
import json
import logging
import time

from app.video.providers import llm_chat

logger = logging.getLogger(__name__)

_BATCH = 20            # 每批句数（带下标 JSON，批间无上下文依赖）

_REWRITE_PROMPT = """你是 NBDpsy 心理咨询工作室的口播编辑。下面是一段自助 EMDR 干预引导语音的中文台词（逐句，带下标）。
听者会闭上眼睛、跟随你的声音做练习——语言里任何生硬、翻译腔、书面欧化都会把人从练习状态里拽出来。
请逐句改写成温和陪伴的中文口播稿：

场景与口吻：
- 咨询师面对面引导的口吻，温和、有陪伴感；不质问、不命令
- 表达含义，不要直译；中文口播节奏（短句、一句一意）
- 指令温和化（"请…""试着…"），去欧化句式（被动语态、代词堆叠、长定语从句）

硬规则（必须遵守）：
- 内容要点不增不减，不改变任何专业含义
- 术语表里的译法必须原样保留：
{glossary}
- 原视频作者的自我介绍与人名（如"我是XX""My name is..."）一律去除：改写为不含人名的中性表述（如"欢迎体验本次自助式眼动脱敏与再加工练习"），或直接删去自报姓名的部分；全文任何位置出现的原作者人名都不得保留
- 源平台机制话术（"描述中的链接""订阅/点赞/频道/章节链接"等）改写为适用本片的表述（如"请把进度条拖回'脱敏'练习部分"）或直接删除
- 英语口头确认语（Got it? / Okay? / Alright?）不得直译成质问式中文（"清楚了吗？"），改为温和表达（"好，我们继续"）或省略

改写示例（左=生硬直译，右=自然口播）：
- 「你已解释记忆在大脑的存储方式。」→「刚才，你讲述了这段记忆现在的样子。」
- 「使用风险需由您自行承担。」→「练习中的感受和风险，需要你自己留意和把握。」
- 「清楚了吗？」→「好，我们继续。」
- 「若仍感不适，请返回"脱敏"章节，或点击描述中的链接。」→「如果仍有明显不适，请把进度条拖回'脱敏'练习部分，重复练习后再继续。」

以口播导向的自然、简洁为准，不要为了对齐原文而堆砌字数。
只输出 JSON 对象 {{"下标": "改写后文本", ...}}，不要其他内容。
台词：
{items}"""

_REFLECT_PROMPT = """下面是一段自助 EMDR 引导语音的中文口播稿（逐句，带下标），听者闭眼跟随。
请逐句检查翻译腔与出戏感：欧化句式（被动语态、代词堆叠、长定语从句）、直译痕迹、质问语气、不自然的书面语——
发现即改写成温和自然的口播中文；没问题的句子原样保留。
只输出 JSON 对象 {{"下标": "改进后文本", ...}}，不要其他内容。
台词：
{items}"""

# A6 结语收束句（Global Constraints 逐字文案）：结语块末尾自动补一句温和收尾语。
CLOSING_LINE = "现在，慢慢把注意力带回当下的环境。本次练习到这里，感谢你的参与。"

# 仅拼到「最后一批」初改 prompt 尾部——让 LLM 判断是否需要补收束句（同义收尾已存在则不补，
# 避免重复）。用字符串拼接而非 .format 注入，避免规则里的引号/书名号与 format 占位冲突。
_CLOSING_RULE = (
    "\n\n结语补句规则（仅当这是全片最后一批台词时适用）：\n"
    "- 若这批台词的结尾还没有明确的收束/道别语（把注意力带回当下、结束本次练习、致谢一类的"
    "收尾表达），请在输出的 JSON 里额外加一个键 \"append\"，其值逐字为"
    "「" + CLOSING_LINE + "」。\n"
    "- 若结尾已经有同义的收束表达，则不要输出 \"append\" 键（不要重复收尾）。")


def _glossary_block(term_sheet: list[dict]) -> str:
    if not term_sheet:
        return "（无）"
    return "\n".join(f"- {t['en']} → {t['zh']}" for t in term_sheet)


async def _fetch_fixes(prompt: str, *, temperature: float) -> dict:
    """一次 LLM 调用取下标→文本的 JSON 修订；调用/解析失败返回空 dict（调用方兜底保留原文）。

    换 import 面：源 get_llm(_LLM_KEY).chat(...).content（带 urgent 兼容层）→ 薄 provider
    llm_chat(messages, temperature=) 直接返回正文字符串，去掉 _resp_content/urgent。
    """
    try:
        content = await llm_chat(messages=[{"role": "user", "content": prompt}],
                                 temperature=temperature) or ""
        start, end = content.find("{"), content.rfind("}")
        return json.loads(content[start:end + 1]) if start >= 0 else {}
    except Exception as exc:
        logger.warning("rewrite/reflect LLM 调用失败，保留当前文本: %s", exc)
        return {}


def _apply_fixes(result: list[dict], fixes: dict, base: int, batch_len: int) -> None:
    """把下标→文本回填 result[i]['zh']（越界下标 / 非串 / 空串忽略，防 LLM 幻觉越界）。"""
    for key, val in fixes.items():
        try:
            i = int(key)
        except (ValueError, TypeError):
            continue
        if base <= i < base + batch_len and isinstance(val, str) and val.strip():
            result[i]["zh"] = val.strip()


def _append_closing_line(result: list[dict], fixes: dict) -> None:
    """A6：最后一批 LLM 判定需补收束语时，把收束句作为新 segment 追加到结果末尾。

    契约：重写产物结语块末尾存在该收束句；同义收尾已存在时 LLM 不给 "append" 键（或给空值），
    不追加（不重复）。追加文本**逐字取常量 CLOSING_LINE**——"append" 键只作「是否追加」信号，
    LLM 回传的文本一律不采用，把逐字保证从 prompt 依赖收紧为代码保证（防 LLM 改标点/漏字）。
    新句继承末句时间锚点（start/orig_*），下游 relayout 据 orig_start 归入结语块、正常配音与
    排字幕——全链无需特判。收束句必须朗读，故清掉可能继承来的 no_dub 标记。
    """
    signal = fixes.get("append") if isinstance(fixes, dict) else None
    # "append" 键只是信号：真值（含变体文本 / True）即追加；缺省 / 空串 / False 不追加
    if not signal or not result:
        return
    tail = dict(result[-1])
    tail.pop("no_dub", None)
    tail["zh"] = CLOSING_LINE                # 逐字取常量，不采用 LLM 回传文本
    tail["en"] = ""                          # 收束句无英文源，置空避免双语 md 中英错配
    result.append(tail)


def _disclaimer_ranges(facts_scenes: list[dict]) -> list[tuple[float, float]]:
    """从 facts 场景取全部「免责/须知卡」原片时间区间（storyboard 同款判定，DRY 复用）。"""
    from app.video.pipeline.remake.storyboard import _is_disclaimer
    return [(float(sc["t0"]), float(sc["t1"])) for sc in facts_scenes
            if sc.get("kind") in ("title_card", "text_card")
            and _is_disclaimer(sc.get("text", "") or "")]


def mark_no_dub(segments: list[dict], facts_scenes: list[dict]) -> list[dict]:
    """A3：orig 时间落入免责/须知卡范围内的句子原地标 no_dub=True 并返回。

    通用规则（非硬编码句表）：免责/须知台词只在卡片画面上完整显示，不朗读、不进字幕、
    不占时间轴（下游 dub/字幕/relayout 据 no_dub 过滤）。orig 时间取 orig_start（优先，
    保证重入幂等），落入任一免责卡 [t0,t1)（左闭右开，与 timeline 归块半开区间一致）即标记。
    无免责卡时原样返回，其余台词不受影响。
    """
    ranges = _disclaimer_ranges(facts_scenes)
    if not ranges:
        return segments
    for seg in segments:
        os = float(seg.get("orig_start", seg.get("start", 0.0)))
        if any(t0 <= os < t1 for t0, t1 in ranges):
            seg["no_dub"] = True
    return segments


def anchor_closing_line(segments: list[dict], facts_scenes: list[dict]) -> list[dict]:
    """A6/F2/F-A：把追加的收束句锚定到收尾卡「卡内已有台词之后」，并保证其最终朗读（no_dub=False）。

    必须在 mark_no_dub 之后调用（现代码流：rewrite_segments 内追加收束句 → mark_no_dub →
    本函数）。两件事：
      1. 收束句无条件清 no_dub——收束句必须朗读，即便 mark_no_dub 因末句锚点落免责区间把它
         误标了（「末句锚点落免责区间致收束句被再标记」窄边界，根治点）。
      2. 若存在结语卡（最后一个 card 块且其后无球段），把收束句 orig_start/orig_end 锚到该收尾卡
         「卡内已有台词之后」——orig_start = max(卡区间内各句 orig_end, 卡t0) + 0.2，
         orig_end = orig_start + 0.5。使下游 relayout 据 orig_start 归入收尾卡块**并排到卡内
         最后一位**（成片最后一句配音）。EMDR 真实结构：收尾卡上常有 4-5 句原文旁白，旧行为锚
         t0+0.1（卡开头）会让 relayout 按 orig 排序把收束句排到卡内第一位 → 「感谢参与」先播、
         结尾提问后播（job13/14 连续复现的次序颠倒）。卡内无原文句时退化为 t0+0.1 保旧行为。
         无结语卡则保留继承锚点（收束句落末块）。

    末段不是收束句（未追加）时原样返回，不误伤普通末句。
    """
    from app.video.pipeline.remake.timeline import _closing_card_idx
    if not segments or segments[-1].get("zh") != CLOSING_LINE:
        return segments
    closing = segments[-1]
    closing.pop("no_dub", None)              # 收束句必须朗读
    idx = _closing_card_idx(facts_scenes)
    if idx is not None:
        t0 = float(facts_scenes[idx]["t0"])
        t1 = float(facts_scenes[idx]["t1"])
        # 收尾卡区间 [t0,t1) 内已有台词（原文旁白，不含收束句自身）的 orig_end 最大值
        inner_ends = [float(seg.get("orig_end", seg.get("end", 0.0)))
                      for seg in segments[:-1]
                      if t0 <= float(seg.get("orig_start", seg.get("start", 0.0))) < t1]
        if inner_ends:                        # 卡内有原文：锚到其后，保证成片里收束句最后播
            anchor = max([*inner_ends, t0]) + 0.2
            closing["orig_start"] = anchor
            closing["orig_end"] = anchor + 0.5
        else:                                 # 卡内无原文：退化为 t0+0.1 保旧行为
            closing["orig_start"] = t0 + 0.1
            closing["orig_end"] = t0 + 0.1
    return segments


async def rewrite_segments(translated: list[dict], term_sheet: list[dict], *,
                           deadline: float | None = None) -> list[dict]:
    """逐批口播本土化改写 zh 字段（初改 + 反思环），其余字段原样保留。"""
    result = [dict(t) for t in translated]
    if not result:
        return result
    n = len(result)
    for base in range(0, n, _BATCH):
        if deadline is not None and time.monotonic() > deadline:
            logger.warning("rewrite 预算耗尽，剩余 %d 句保留原译文", n - base)
            break
        batch = result[base:base + _BATCH]
        is_last = base + _BATCH >= n            # A6：仅最后一批追加结语补句规则
        # 初改
        items = json.dumps({str(base + i): seg["zh"] for i, seg in enumerate(batch)},
                           ensure_ascii=False)
        prompt = _REWRITE_PROMPT.format(glossary=_glossary_block(term_sheet), items=items)
        if is_last:
            prompt += _CLOSING_RULE
        fixes = await _fetch_fixes(prompt, temperature=0.4)
        _apply_fixes(result, fixes, base, len(batch))
        # 反思环：对初改后的同批再过一次自查（失败保留初改文本）
        reflect_items = json.dumps(
            {str(base + i): result[base + i]["zh"] for i in range(len(batch))},
            ensure_ascii=False)
        reflect_prompt = _REFLECT_PROMPT.format(items=reflect_items)
        _apply_fixes(result, await _fetch_fixes(reflect_prompt, temperature=0.3),
                     base, len(batch))
        if is_last:                             # A6：LLM 判定需补收束语则追加为新 segment
            _append_closing_line(result, fixes)
    return result
