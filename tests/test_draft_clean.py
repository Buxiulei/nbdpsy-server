"""DraftCleanScheduler 决策 + draft_clean kind 注册测试(不起真浏览器)。

覆盖:
- scan_once:有 cookie 且 7 天内无 draft_clean 台账行 → enqueue(operator_id=0);
- 7 天内已有任意状态行(done/error/queued)→ 跳过(失败也等下周期,不高频重试);
- 超过 7 天的旧行不算 → 重新 enqueue;无 cookie 账号跳过;
- account_worker._resolve_execute 认识 draft_clean kind。
"""
from datetime import datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.models.browser_job import BrowserJob
from app.models.xhs_account import XhsAccount
from app.services.draft_clean import DraftCleanScheduler


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


async def _add_account(factory, name, with_cookies=True) -> int:
    async with factory() as s:
        acc = XhsAccount(name=name, cookie_status="valid")
        if with_cookies:
            acc.login_cookies = "blob"
        s.add(acc)
        await s.commit()
        return acc.id


async def _add_clean_job(factory, acc_id, status, days_ago) -> None:
    async with factory() as s:
        s.add(BrowserJob(
            id=f"dc-{acc_id}-{days_ago}", kind="draft_clean", account_id=acc_id,
            operator_id=0, payload="{}", status=status,
            created_at=datetime.utcnow() - timedelta(days=days_ago),
        ))
        await s.commit()


async def _clean_jobs(factory) -> list[BrowserJob]:
    async with factory() as s:
        return list((await s.execute(
            select(BrowserJob).where(BrowserJob.kind == "draft_clean")
        )).scalars().all())


async def test_scan_enqueues_when_no_recent_clean(smk):
    """有 cookie 且 7 天内没清过 → enqueue;无 cookie 跳过。"""
    acc = await _add_account(smk, "号A")
    await _add_account(smk, "未接入", with_cookies=False)

    sched = DraftCleanScheduler(smk, interval=999)
    assert await sched.scan_once() == 1
    jobs = await _clean_jobs(smk)
    assert len(jobs) == 1 and jobs[0].account_id == acc
    assert jobs[0].operator_id == 0 and jobs[0].status == "queued"
    # 幂等:刚 enqueue 的行在 7 天窗口内 → 再跑不重复
    assert await sched.scan_once() == 0


async def test_scan_skips_recent_any_status(smk):
    """7 天内已有行(哪怕 error)→ 跳过——失败等下周期,不高频重试。"""
    acc = await _add_account(smk, "号A")
    await _add_clean_job(smk, acc, "error", days_ago=3)

    sched = DraftCleanScheduler(smk, interval=999)
    assert await sched.scan_once() == 0
    assert len(await _clean_jobs(smk)) == 1


async def test_scan_reenqueues_after_week(smk):
    """上次清理已超 7 天 → 重新 enqueue。"""
    acc = await _add_account(smk, "号A")
    await _add_clean_job(smk, acc, "done", days_ago=8)

    sched = DraftCleanScheduler(smk, interval=999)
    assert await sched.scan_once() == 1
    assert len(await _clean_jobs(smk)) == 2


def test_resolve_execute_knows_draft_clean():
    """account_worker 能解析 draft_clean kind(注册链在位)。"""
    from app.account_worker import _resolve_execute

    fn = _resolve_execute("draft_clean")
    assert callable(fn)
