"""NoteMetricsScheduler 行为测试(不起真浏览器,只验「今天该不该采」决策 + enqueue)。

隔离手法与 test_cookie_checker 一致:tmp sqlite 引擎 + async_sessionmaker;
browser_jobs_repo.enqueue 走 db_module.async_session,monkeypatch 指到同一 tmp 库。

覆盖:
- scan_once:valid 且今天没快照没尝试 → enqueue 一条 note_export(operator_id=0);
- 今天已有 note_metrics_daily 快照的号跳过;
- 今天已有任意状态 note_export 台账行的号跳过(每天每号最多自动 1 次);
- invalid 号跳过;
- 幂等:连跑两轮不重复 enqueue。
"""
from datetime import datetime, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

import app.core.db as db_module
from app.models.browser_job import BrowserJob
from app.models.note_metric import NoteMetricDaily
from app.models.xhs_account import XhsAccount
from app.services.note_metrics_scheduler import NoteMetricsScheduler


@pytest.fixture
async def smk(tmp_path, monkeypatch):
    """独立 tmp sqlite 会话工厂 + 建表;并把 repo.enqueue 用的全局会话指到同一库。"""
    from app.core.db import Base

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/t.db", future=True)
    import app.models  # noqa: F401  触发模型注册

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr(db_module, "async_session", factory)
    try:
        yield factory
    finally:
        await engine.dispose()


async def _add_account(factory, name, cookie_status) -> int:
    async with factory() as s:
        acc = XhsAccount(name=name, cookie_status=cookie_status)
        s.add(acc)
        await s.commit()
        return acc.id


async def _list_export_jobs(factory) -> list[BrowserJob]:
    async with factory() as s:
        return list((await s.execute(
            select(BrowserJob).where(BrowserJob.kind == "note_export")
        )).scalars().all())


async def test_scan_enqueues_for_valid_without_today_snapshot(smk):
    """valid 且今天没采过 → enqueue;invalid 跳过;operator_id=0(系统直调约定)。"""
    valid_id = await _add_account(smk, "有效号", "valid")
    await _add_account(smk, "失效号", "invalid")

    sched = NoteMetricsScheduler(smk, interval=999)
    enqueued = await sched.scan_once()

    assert enqueued == 1
    jobs = await _list_export_jobs(smk)
    assert len(jobs) == 1
    assert jobs[0].account_id == valid_id
    assert jobs[0].operator_id == 0
    assert jobs[0].status == "queued"


async def test_scan_skips_account_with_today_snapshot(smk):
    """今天已有 note_metrics_daily 快照 → 不再 enqueue。"""
    acc_id = await _add_account(smk, "有效号", "valid")
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    async with smk() as s:
        s.add(NoteMetricDaily(
            account_id=acc_id, title="t", publish_time="p", snapshot_date=today,
        ))
        await s.commit()

    sched = NoteMetricsScheduler(smk, interval=999)
    assert await sched.scan_once() == 0
    assert await _list_export_jobs(smk) == []


async def test_scan_skips_account_attempted_today_even_if_failed(smk):
    """今天已有任意状态的 note_export 台账行(含 error)→ 不再自动采(每天最多 1 次)。"""
    acc_id = await _add_account(smk, "有效号", "valid")
    async with smk() as s:
        s.add(BrowserJob(
            id="j1", kind="note_export", account_id=acc_id, operator_id=3,
            payload="{}", status="error",
        ))
        await s.commit()

    sched = NoteMetricsScheduler(smk, interval=999)
    assert await sched.scan_once() == 0
    assert len(await _list_export_jobs(smk)) == 1  # 只有预置那条,没新增


async def test_scan_idempotent_across_rounds(smk):
    """连跑两轮:第一轮 enqueue 后,第二轮因「今天已尝试」跳过,不重复。"""
    await _add_account(smk, "有效号", "valid")

    sched = NoteMetricsScheduler(smk, interval=999)
    assert await sched.scan_once() == 1
    assert await sched.scan_once() == 0
    assert len(await _list_export_jobs(smk)) == 1
