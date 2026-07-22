"""psych_glossary 术语表读写（翻译强约束 + 自动回写）。

匹配优先级 manual/approved > seed > auto：翻译时优先采用人工终审过的译名，
其次种子词典，最后自动回写的候选译名。

平移自 video_transport/glossary.py：源用同步 ``Session.query``；宿主统一 AsyncSession，故
``match_terms``/``upsert_auto_term`` 改 async（``await db.execute(select(...))``），匹配/优先级/
去重/不覆盖等逻辑逐字保真。纯函数 ``normalize_term``/``_priority`` 保持同步。
"""
import re

from sqlalchemy import select

from app.models.psych_glossary import PsychGlossary

# 来源基础优先级；approved 再叠加权重，保证人工终审始终压过任何自动来源
_PRIORITY = {"manual": 3, "seed": 2, "auto": 1}


def normalize_term(term: str) -> str:
    """归一化术语：去首尾标点空白 + 多空格合一 + 转小写，作为匹配主键。"""
    cleaned = (term or "").strip().strip(".,;:!?\"'()[]")
    return re.sub(r"\s+", " ", cleaned).strip().lower()


def _priority(row: PsychGlossary) -> int:
    """行的匹配优先级：来源基分 + approved 加权。"""
    return _PRIORITY.get(row.source, 0) + (10 if row.approved else 0)


async def match_terms(db, candidates: list[str]) -> dict[str, dict]:
    """把候选术语匹配到术语表，返回 {原candidate: {"zh", "source", "approved"}}。

    en_term 与 aliases 双路匹配；同一归一词命中多来源时取优先级最高者。
    aliases 存 JSON 无法在 SQL 内检索，术语规模万级，全表载入后内存建索引即可。
    """
    norm_map = {c: normalize_term(c) for c in candidates if normalize_term(c)}
    if not norm_map:
        return {}

    # 归一词 -> 最高优先级的术语行
    index: dict[str, PsychGlossary] = {}
    rows = (await db.execute(select(PsychGlossary))).scalars().all()
    for row in rows:
        keys = [row.en_term] + [normalize_term(a) for a in (row.aliases or [])]
        for key in keys:
            if not key:
                continue
            current = index.get(key)
            if current is None or _priority(row) > _priority(current):
                index[key] = row

    out: dict[str, dict] = {}
    for cand, norm in norm_map.items():
        row = index.get(norm)
        if row:
            out[cand] = {"zh": row.zh_term, "source": row.source, "approved": row.approved}
    return out


async def upsert_auto_term(db, en: str, zh: str, *, evidence: str = "") -> None:
    """自动回写候选译名：已存在（任何 source）则不覆盖，不存在插入 source=auto/confidence=0.4。"""
    norm = normalize_term(en)
    if not norm or not (zh or "").strip():
        return
    existing = (await db.execute(
        select(PsychGlossary).filter_by(en_term=norm))).scalars().first()
    if existing:
        return  # 任何已有来源都不覆盖，避免自动译名污染人工/种子词典
    db.add(PsychGlossary(
        en_term=norm, zh_term=zh.strip(), source="auto",
        confidence=0.4, evidence=(evidence or "")[:500],
    ))
    await db.commit()
