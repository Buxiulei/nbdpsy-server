"""术语表匹配与回写（平移自 test_glossary.py）。

源用同步 db_session；宿主 AsyncSession + async match/upsert，seed 走 conftest 的 db fixture
（每测试独立临时 sqlite，含 psych_glossary 表，自动清理）。
"""
import pytest_asyncio
from sqlalchemy import select

from app.models.psych_glossary import PsychGlossary
from app.video.pipeline.glossary import (
    match_terms,
    normalize_term,
    upsert_auto_term,
)


@pytest_asyncio.fixture
async def seeded(db):
    """种子术语：arfid(seed) + attachment theory(manual/approved 带 alias)。"""
    db.add_all([
        PsychGlossary(en_term="arfid", zh_term="回避性/限制性摄食障碍",
                      source="seed", confidence=0.8),
        PsychGlossary(en_term="attachment theory", zh_term="依恋理论",
                      source="manual", approved=True,
                      aliases=["attachment styles theory"]),
    ])
    await db.commit()
    return db


class TestGlossary:
    def test_normalize(self):
        assert normalize_term("  Attachment   Theory. ") == "attachment theory"

    async def test_match_exact_and_alias(self, seeded):
        hits = await match_terms(seeded, ["ARFID", "Attachment Styles Theory", "unknown term"])
        assert hits["ARFID"]["zh"] == "回避性/限制性摄食障碍"
        assert hits["Attachment Styles Theory"]["zh"] == "依恋理论"
        assert hits["Attachment Styles Theory"]["approved"] is True
        assert "unknown term" not in hits

    async def test_upsert_auto_does_not_override(self, seeded):
        await upsert_auto_term(seeded, "CBT", "认知行为疗法", evidence="entity:123")
        await upsert_auto_term(seeded, "ARFID", "错误译名不应覆盖")
        row = (await seeded.execute(
            select(PsychGlossary).filter_by(en_term="cbt"))).scalars().first()
        assert row.source == "auto" and row.confidence == 0.4
        row2 = (await seeded.execute(
            select(PsychGlossary).filter_by(en_term="arfid"))).scalars().first()
        assert row2.zh_term == "回避性/限制性摄食障碍"
