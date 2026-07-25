"""account_trends 分析包测试:日汇总/增量/率值/断档间隔/排序/RBAC。

隔离:tmp sqlite + async_sessionmaker,admin operator(assert_account_access 放行)。
"""
from datetime import datetime

import pytest
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.models.note_metric import NoteMetric, NoteMetricDaily
from app.models.operator import Operator
from app.models.xhs_account import XhsAccount
from app.services.note_metrics_service import account_trends


@pytest.fixture
async def smk(tmp_path):
    from app.core.db import Base

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/t.db", future=True)
    import app.models  # noqa: F401

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    try:
        yield factory
    finally:
        await engine.dispose()


def _admin() -> Operator:
    return Operator(id=1, name="root", apikey_hash="x", role="admin", enabled=True)


def _daily(acc, title, pub, date, **m):
    return NoteMetricDaily(
        account_id=acc, title=title, publish_time=pub, snapshot_date=date, **m)


async def _seed(smk) -> int:
    """1 账号 2 笔记 × 2 快照日(隔 2 天,模拟断档);A 火 B 冷。"""
    async with smk() as s:
        acc = XhsAccount(name="号A", cookie_status="valid")
        s.add(acc)
        await s.commit()
        acc_id = acc.id
        pub = "2026年07月01日10时00分00秒"
        # 快照日 07-23:A(曝光100/观看50/赞5/藏2/评1) B(曝光10/观看0)
        s.add(_daily(acc_id, "A", pub, "2026-07-23",
                     exposure=100, views=50, likes=5, collects=2, comments=1, follows=1))
        s.add(_daily(acc_id, "B", pub, "2026-07-23", exposure=10, views=0))
        # 快照日 07-25(隔 2 天):A 涨到 200/80/9/4/2;B 仍 0 观看
        s.add(_daily(acc_id, "A", pub, "2026-07-25",
                     exposure=200, views=80, likes=9, collects=4, comments=2, follows=1))
        s.add(_daily(acc_id, "B", pub, "2026-07-25", exposure=12, views=0))
        # 最新快照表(latest 态)
        s.add(NoteMetric(account_id=acc_id, title="A", publish_time=pub,
                         exposure=200, views=80, likes=9, collects=4, comments=2,
                         follows=1, updated_at=datetime.utcnow()))
        s.add(NoteMetric(account_id=acc_id, title="B", publish_time=pub,
                         exposure=12, views=0, updated_at=datetime.utcnow()))
        await s.commit()
        return acc_id


async def test_trends_full_package(smk):
    acc_id = await _seed(smk)
    async with smk() as s:
        pkg = await account_trends(s, _admin(), acc_id)

    # meta:快照覆盖
    assert pkg["meta"]["snapshot_dates"] == ["2026-07-23", "2026-07-25"]
    assert pkg["meta"]["latest_snapshot_date"] == "2026-07-25"
    assert pkg["meta"]["notes_tracked"] == 2
    assert pkg["account"]["id"] == acc_id

    # 账号级日汇总 + 增量(带断档间隔 days_between=2)
    d1, d2 = pkg["account_daily"]
    assert d1["snapshot_date"] == "2026-07-23" and d1["note_count"] == 2
    assert d1["exposure"] == 110 and d1["views"] == 50 and d1["delta"] is None
    assert d2["exposure"] == 212 and d2["views"] == 80
    assert d2["delta"]["exposure"] == 102 and d2["delta"]["views"] == 30
    assert d2["delta"]["days_between"] == 2

    # notes 按最新 views 降序:A 在前
    note_a, note_b = pkg["notes"]
    assert note_a["title"] == "A" and note_b["title"] == "B"
    # 发布距今天数已解析(中文日期格式)
    assert isinstance(note_a["days_since_publish"], int)
    # 率值:A like_rate=9/80;B views=0 → 全 None
    assert note_a["rates"]["like_rate"] == round(9 / 80, 4)
    assert note_a["rates"]["engage_rate"] == round((9 + 4 + 2) / 80, 4)
    assert note_b["rates"]["like_rate"] is None
    # 每篇序列增量
    assert note_a["series"][0]["delta"] is None
    assert note_a["series"][1]["delta"]["views"] == 30
    assert note_a["series"][1]["delta"]["days_between"] == 2


async def test_trends_rbac_denied(smk):
    """无授权 operator → AccessDenied。"""
    from app.auth.guards import AccessDenied

    acc_id = await _seed(smk)
    op = Operator(id=9, name="op", apikey_hash="x", role="operator", enabled=True)
    async with smk() as s:
        with pytest.raises(AccessDenied):
            await account_trends(s, op, acc_id)


async def test_trends_empty_account(smk):
    """无任何快照的账号:结构完整、列表为空,不炸。"""
    async with smk() as s:
        acc = XhsAccount(name="空号", cookie_status="valid")
        s.add(acc)
        await s.commit()
        pkg = await account_trends(s, _admin(), acc.id)
    assert pkg["account_daily"] == [] and pkg["notes"] == []
    assert pkg["meta"]["latest_snapshot_date"] is None
