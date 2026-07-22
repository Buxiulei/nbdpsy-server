"""字幕翻译"信雅达"三步。

整篇搬运最核心也最复杂的一环，拆成三步保证术语一致 + 时长可配音：

1. extract_terms  —— 全篇一次 LLM 抽学术专名/疗法名/量表名/人名
2. resolve_terms  —— 术语表批量查（manual/seed/auto 优先级）；未命中 LLM 直译，结果回写术语表
3. translate_batches + reflect_and_fit
                  —— 逐句/分批翻译（术语强约束 + 上文衔接），再把超时间槽的句子送审校压缩

平移自 video_transport/translator.py。换 import 面（唯一改动类别）：
- qwen-mt 逐句翻译 → 薄 provider ``mt_translate(texts, term_sheet=, domains=, tm_list=)``：
  translation_options（source_lang/target_lang/terms[/domains/tm_list]）的构造与 model 绑定沉进
  provider，此处只喂 term_sheet/domains/tm_list 语义上下文；逐句 retry/None-fallback 保真。
- 抽术语 / 术语直译 / 分批兜底翻译 / 时长审校 → 薄 provider ``llm_chat(messages, temperature=)``：
  返回正文字符串，故去掉 ``_resp_content``/``urgent`` 兼容层。
- 术语表 match/upsert → ``glossary``（AsyncSession，改 await）。
- 分流开关从 ``settings.TRANSLATE_MODEL`` 换成宿主 ``settings.VIDEO_MT_MODEL``（同义：mt 档模型名）。
- **RAG 术语裁决层**：nbdpsy-server 未接入 RAG 知识库子系统，``_rag_lookup`` 恒返回 None（保留为
  seam，行为等价源「RAG 未命中→退回 LLM 直译」）；源 ``_query_rag``/``KnowledgeBase``/``_ADJUDICATE_PROMPT``
  在本宿主无落点，随之下线。批处理切分/tm_list 滑窗/reflect_and_fit/N 进 N 出命门逐行保真。
"""
import asyncio
import json
import re
import time

from loguru import logger

from app.core.config import settings
from app.video.pipeline.glossary import match_terms, upsert_auto_term
from app.video.providers import llm_chat, mt_translate

_BATCH = 30                                    # 每批翻译句数
_PREV_TAIL = 3                                 # 带给下一批的上文译文句数
_NEXT_PREVIEW = 3                              # 带给本批的下文预览原文句数（仅供理解语境，不翻译）
_MAX_WORDS = 8000                              # 抽术语单次喂词上限，超出截断分两次
_MT_CONCURRENCY = 8                            # qwen-mt 逐句翻译并发上限（Semaphore）
_TM_WINDOW = 5                                 # qwen-mt tm_list 滑窗：给每块共享的上文译对数

_EXTRACT_PROMPT = """从下面的英文视频字幕文本中抽取所有专有名词：学术理论名、心理疗法名、\
心理量表/问卷名、学者人名。只输出 JSON 字符串数组，不要解释、不要重复；找不到就输出 []。

字幕文本：
%s"""

_TERM_TRANSLATE_PROMPT = """把下面的英文心理学/学术术语翻成规范中文术语。
只输出中文译名本身，不要引号、不要解释、不要标点。
术语：{term}"""

_TRANSLATE_PROMPT = """你是心理学科普视频的字幕翻译。把编号英文句子翻成中文。

最高优先级（先做这一步再动笔）：先通读本批全部句子，理解这段整体在讲什么、逻辑怎么推进；\
再逐句翻译——整批译文要作为连贯的一段口语中文读起来顺（像讲给朋友听，不是写论文），\
不是一堆孤立句子的堆砌。术语前后统一，指代清晰，该合并语序的合并、该拆分的拆分；\
但"连贯"绝不等于"书面化"——口语第一，别为了通顺把话写成书面语。输出条数必须严格等于输入句数。

要求：
1. 信雅达：口语化科普语气，像中文母语者说话，严禁翻译腔
2. 中文语序重组（最重要）：先读懂整句意思，抛开英文句式，用中文母语者会说的话重写。
   - 英文长定语/从句 → 拆成两三个短句，先说主干再补细节
   - 时间/条件/原因状语 → 提到句子前面
   - 被动语态 → 转主动（"was developed by X" → "X 提出的"）
   - 消除 it / there be / one of 直译腔；代词能省则省
   - 自查标准：念出来像中国人日常说话，不像翻译稿
3. 术语强约束，以下术语必须使用给定译法：
{glossary}
4. 数字/单位本地化；人名首次出现"中文译名（原文）"
5. 上文衔接（前批末尾译文）：{prev_tail}
6. 下文预览【仅供理解语境，不要翻译、不要输出】：{next_preview}
7. 视频背景：{title}
8. 只输出 JSON 数组，第 i 项是第 i 句的译文，数量必须等于输入句数

输入：
{numbered_sentences}"""

_REFLECT_PROMPT = """以下中文配音句超出时间槽（观众听不完）。在不丢核心信息前提下压缩改写。
语序不像中文的句子按中文习惯语序重写（拆长句、状语前置、被动转主动、去直译腔）。
每句允许最大字数已给出。术语仍须遵守：{glossary}
只输出 JSON 对象 {{"句子下标": "压缩后译文"}}。
输入：{items}"""


# ── 通用小工具 ────────────────────────────────────────────────────

def _json_array(content: str) -> list:
    """从 LLM 输出里抠出 JSON 数组（容忍前后多余文字 / markdown 围栏）。失败返回 []。"""
    m = re.search(r"\[.*\]", content, re.DOTALL)
    if not m:
        return []
    try:
        v = json.loads(m.group(0))
        return v if isinstance(v, list) else []
    except (json.JSONDecodeError, ValueError):
        return []


def _json_object(content: str) -> dict:
    """从 LLM 输出里抠出 JSON 对象。失败返回 {}。"""
    m = re.search(r"\{.*\}", content, re.DOTALL)
    if not m:
        return {}
    try:
        v = json.loads(m.group(0))
        return v if isinstance(v, dict) else {}
    except (json.JSONDecodeError, ValueError):
        return {}


def _clean_term(text: str) -> str:
    """清掉 LLM 译名里的引号/句末标点/空白，只留术语本体。"""
    return (text or "").strip().strip("。.，,；;：:\"'“”「」").strip()


# ── Step 1: 抽术语 ────────────────────────────────────────────────

async def extract_terms(segments: list[dict]) -> list[str]:
    """全篇文本拼接后一次 LLM 抽专有名词，返回去重列表（大小写不敏感去重）。

    超 _MAX_WORDS 词时截断分两窗各调一次，避免超长上下文；顺序保留、忽略大小写重复。
    """
    if not segments:
        return []
    words = " ".join(s.get("text", "") for s in segments).split()
    if len(words) > _MAX_WORDS:
        windows = [" ".join(words[:_MAX_WORDS]),
                   " ".join(words[_MAX_WORDS:_MAX_WORDS * 2])]
    else:
        windows = [" ".join(words)]

    seen: list[str] = []
    lowered: set[str] = set()
    for window in windows:
        if not window.strip():
            continue
        content = await llm_chat(
            messages=[{"role": "user", "content": _EXTRACT_PROMPT % window}],
            temperature=0.0)
        for t in _json_array(content):
            if not isinstance(t, str):
                continue
            term = t.strip()
            if term and term.lower() not in lowered:
                seen.append(term)
                lowered.add(term.lower())
    return seen


# ── Step 2: 术语对齐 ──────────────────────────────────────────────

async def _rag_lookup(term: str) -> str | None:
    """RAG 术语裁决层——nbdpsy-server 未接入 RAG 知识库子系统，恒返回 None（RAG 未命中）。

    保留为函数 seam：resolve_terms 三级（术语表→RAG→LLM 直译）结构逐字保真；本宿主 RAG 层始终无
    候选，等价源「RAG 查不到」路径（退回 LLM 直译）。测试可 patch 本函数注入候选验证优先级。
    """
    return None


async def _llm_translate_term(term: str) -> str:
    """RAG 无候选时的兜底：LLM 直译单个术语。异常返回空串。"""
    try:
        content = await llm_chat(
            messages=[{"role": "user",
                       "content": _TERM_TRANSLATE_PROMPT.format(term=term)}],
            temperature=0.0)
    except Exception:
        return ""
    return _clean_term(content)


async def resolve_terms(db, candidates: list[str]) -> list[dict]:
    """把候选术语对齐到中文译名，返回 term_sheet=[{"en","zh","source"}]。

    优先术语表（保留其 source）；未命中先查 RAG 裁决，再退回 LLM 直译，
    结果以 source=auto 回写术语表（upsert 不覆盖已有条目）。zh 为空则丢弃该词。
    """
    ordered = [c for c in dict.fromkeys(candidates) if c and c.strip()]
    if not ordered:
        return []

    hits = await match_terms(db, ordered)
    sheet: list[dict] = []
    for cand in ordered:
        hit = hits.get(cand)
        if hit:
            sheet.append({"en": cand, "zh": hit["zh"],
                          "source": hit.get("source", "manual")})
            continue
        rag_zh = await _rag_lookup(cand)
        zh = rag_zh if rag_zh else await _llm_translate_term(cand)
        if not zh:
            continue
        await upsert_auto_term(db, cand, zh, evidence="rag" if rag_zh else "llm")
        sheet.append({"en": cand, "zh": zh, "source": "auto"})
    return sheet


# ── Step 3: 分批翻译 + 审校压缩 ───────────────────────────────────

def _glossary_block(term_sheet: list[dict]) -> str:
    """把 term_sheet 拼成 prompt 里的术语约束块。"""
    lines = [f"- {t['en']} → {t['zh']}" for t in term_sheet if t.get("en") and t.get("zh")]
    return "\n".join(lines) if lines else "(无)"


def _merge_translation(segments: list[dict], zh_list: list[str]) -> list[dict]:
    """按下标 zip 成 TranslatedSegment {start,end,en,zh}（取 segments 与 zh 较短者）。"""
    return [{"start": s["start"], "end": s["end"], "en": s["text"], "zh": zh}
            for s, zh in zip(segments, zh_list)]


async def _translate_one_batch(prompt: str, expected: int) -> list[str]:
    """翻一批：数量不等于输入句数重试一次，仍失败抛异常（宁可 job 失败也不错位）。"""
    arr: list[str] = []
    for _ in range(2):
        content = await llm_chat(
            messages=[{"role": "user", "content": prompt}], temperature=0.2)
        arr = [str(x) for x in _json_array(content)]
        if len(arr) == expected:
            return arr
    raise ValueError(f"翻译批次句数不符：期望 {expected} 得到 {len(arr)}（重试后仍失败）")


async def translate_batches(segments: list[dict], term_sheet: list[dict],
                            video_meta: dict, *, deadline: float | None = None) -> list[dict]:
    """字幕翻译总入口，按 settings.VIDEO_MT_MODEL 分流（命门：N 进 N 出恒成立）。

    - qwen-mt 前缀：走专用翻译模型逐句并发路径（terms/domains/tm_list 三件套，
      术语一致 + 中文语序 + 上下文连贯）。单句失败按下标回退批量路径兜底。
    - 其他：走通用 LLM 批量 prompt 路径（保留为降级兜底）。
    """
    if not segments:
        return []
    if (settings.VIDEO_MT_MODEL or "").startswith("qwen-mt"):
        return await _translate_batches_qwen_mt(
            segments, term_sheet, video_meta, deadline=deadline)
    return await _translate_batches_qwen3(
        segments, term_sheet, video_meta, deadline=deadline)


def _mt_domains(title: str) -> str:
    """按视频标题生成 qwen-mt 的 domains 领域+风格描述（DashScope 要求英文描述）。"""
    topic = (title or "").strip()
    style = ("Translate into warm, natural, colloquial Chinese as a caring psychology "
             "science-popularization narrator would speak to a friend, following Chinese "
             "expression habits; keep it fluent and spoken, never translationese.")
    if topic:
        return f'A Chinese psychology popular-science video about "{topic}". {style}'
    return f"A Chinese psychology popular-science video. {style}"


async def _mt_translate_one(sem, en: str, term_sheet: list[dict],
                            domains: str, tm_pairs: list[dict]) -> str | None:
    """单句 qwen-mt 翻译（term_sheet/domains/tm_list 三件套）。重试一次仍失败/空返回 None。

    返回 None 由上层收集后统一回退批量路径兜底，保证 N 进 N 出不丢句。translation_options 的
    构造与 model 绑定在 provider ``mt_translate`` 内（此处只喂语义上下文，逐句一元素批调用）。
    """
    async with sem:
        for _ in range(2):
            try:
                out = await mt_translate([en], term_sheet=term_sheet,
                                         domains=domains, tm_list=tm_pairs)
            except Exception as e:
                logger.warning("[translator] qwen-mt 单句异常，重试: {}", e)
                continue
            zh = (out[0] if out else "").strip()
            if zh:
                return zh
    return None


async def _translate_batches_qwen_mt(segments: list[dict], term_sheet: list[dict],
                                     video_meta: dict, *,
                                     deadline: float | None = None) -> list[dict]:
    """qwen-mt 逐句并发翻译：Semaphore(_MT_CONCURRENCY) 控并发，天然 N 句→N 句保时间轴。

    连贯性方案（并发 vs 上下文的折中）：按原顺序切 _MT_CONCURRENCY 大小的块，块内并发；
    每块共享的 tm_list = 该块之前「已定稿」译文的尾窗（_TM_WINDOW 句英↔中对），给模型上下文。
    块内句彼此不互为 tm（换取并发），但上一块尾窗已提供强连续性，边界连贯有保障。

    降级：单句 qwen-mt 失败（异常/空）记 None，收集后整体交批量路径按下标回填——单句抖动只回退
    该句、系统性失败（全 None）等价整片回退，job 不 fail。
    deadline 软预算耗尽：剩余句以英文原文占位保持句数对齐（与批量路径一致）。
    """
    domains = _mt_domains((video_meta or {}).get("title", ""))
    sem = asyncio.Semaphore(_MT_CONCURRENCY)

    n = len(segments)
    zh_all: list[str | None] = []
    idx = 0
    while idx < n:
        if deadline is not None and time.monotonic() > deadline:
            zh_all.extend(s["text"] for s in segments[idx:])   # 预算耗尽：原文占位不错位
            break
        chunk = segments[idx:idx + _MT_CONCURRENCY]
        # tm_list：本块之前已定稿译对的尾窗（跳过未定稿/兜底占位的 None）
        tm_pairs = [{"source": segments[j]["text"], "target": zh_all[j]}
                    for j in range(max(0, idx - _TM_WINDOW), idx)
                    if isinstance(zh_all[j], str)]
        results = await asyncio.gather(*[
            _mt_translate_one(sem, s["text"], term_sheet, domains, tm_pairs)
            for s in chunk])
        zh_all.extend(results)
        idx += _MT_CONCURRENCY

    # 失败句（None）统一回退批量路径，按原下标回填保 N 进 N 出
    fail_idx = [i for i, z in enumerate(zh_all) if z is None]
    if fail_idx:
        logger.warning("[translator] qwen-mt {}/{} 句失败，回退批量兜底",
                       len(fail_idx), n)
        fb_segs = [segments[i] for i in fail_idx]
        fb = await _translate_batches_qwen3(
            fb_segs, term_sheet, video_meta, deadline=deadline)
        for k, i in enumerate(fail_idx):
            zh_all[i] = fb[k]["zh"] if k < len(fb) else segments[i]["text"]

    return _merge_translation(
        segments, [z if isinstance(z, str) else segments[i]["text"]
                   for i, z in enumerate(zh_all)])


async def _translate_batches_qwen3(segments: list[dict], term_sheet: list[dict],
                                   video_meta: dict, *, deadline: float | None = None) -> list[dict]:
    """分批翻译整片字幕，返回 TranslatedSegment 列表（en 原文 + zh 译文）。

    每批 _BATCH 句编号进 prompt，带全片背景 + 前批末尾 _PREV_TAIL 句译文做上文衔接 +
    下一批开头 _NEXT_PREVIEW 句英文原文做下文预览（仅供理解语境、不翻译不输出，让批边界更连贯）+
    term_sheet 强约束。deadline 软预算耗尽时，剩余句降级为英文原文占位以保持句数对齐。
    """
    if not segments:
        return []
    glossary = _glossary_block(term_sheet)
    title = (video_meta or {}).get("title", "") or "(无)"

    zh_all: list[str] = []
    prev_tail = "(无)"
    for start in range(0, len(segments), _BATCH):
        if deadline is not None and time.monotonic() > deadline:
            zh_all.extend(s["text"] for s in segments[start:])   # 预算耗尽：原文占位不错位
            break
        batch = segments[start:start + _BATCH]
        numbered = "\n".join(f"{i + 1}. {s['text']}" for i, s in enumerate(batch))
        # 下文预览：下一批开头几句英文原文，让本批末尾句知道话题往哪走；末批无后文留"(无)"
        following = segments[start + _BATCH:start + _BATCH + _NEXT_PREVIEW]
        next_preview = " ".join(s["text"] for s in following) or "(无)"
        prompt = _TRANSLATE_PROMPT.format(
            glossary=glossary, prev_tail=prev_tail, next_preview=next_preview,
            title=title, numbered_sentences=numbered)
        zh_batch = await _translate_one_batch(prompt, len(batch))
        zh_all.extend(zh_batch)
        prev_tail = " ".join(zh_batch[-_PREV_TAIL:]) or "(无)"
    return _merge_translation(segments, zh_all)


def over_slot_indices(translated: list[dict], *, cps: float, max_ratio: float) -> list[int]:
    """返回译文字数超时间槽的下标集：len(zh) > (end-start) * cps * max_ratio。"""
    out: list[int] = []
    for i, t in enumerate(translated):
        slot = (t["end"] - t["start"]) * cps * max_ratio
        if len(t["zh"]) > slot:
            out.append(i)
    return out


async def reflect_and_fit(translated: list[dict], term_sheet: list[dict],
                          *, cps: float, max_ratio: float = 1.25,
                          deadline: float | None = None) -> list[dict]:
    """只把超时间槽的句子送审校 prompt 压缩改写并回填（审校是优化不是闸门）。

    空结果 / 解析失败 / 无超槽句 / deadline 耗尽都直接返回当前译文，不丢句。
    """
    result = [dict(t) for t in translated]
    over_idx = over_slot_indices(result, cps=cps, max_ratio=max_ratio)
    if not over_idx:
        return result
    if deadline is not None and time.monotonic() > deadline:
        return result

    items = [{"下标": i, "原译文": result[i]["zh"],
              "最大字数": int((result[i]["end"] - result[i]["start"]) * cps * max_ratio)}
             for i in over_idx]
    prompt = _REFLECT_PROMPT.format(
        glossary=_glossary_block(term_sheet),
        items=json.dumps(items, ensure_ascii=False))
    try:
        content = await llm_chat(
            messages=[{"role": "user", "content": prompt}], temperature=0.2)
        fixes = _json_object(content)
    except Exception:
        return result

    for key, val in fixes.items():
        try:
            i = int(key)
        except (ValueError, TypeError):
            continue
        if 0 <= i < len(result) and isinstance(val, str) and val.strip():
            result[i]["zh"] = val.strip()
    return result
