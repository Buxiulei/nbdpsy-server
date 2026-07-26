"""NoteMetricsScheduler 行为测试(不起真浏览器,只验「现在该不该采」决策 + enqueue)。

隔离手法与 test_cookie_checker 一致:tmp sqlite 引擎 + async_sessionmaker;
browser_jobs_repo.enqueue 走 db_module.async_session,monkeypatch 指到同一 tmp 库。

覆盖(2026-07-25 语义:全部已接入 cookie 账号 + 每日最多 3 次 + 等比退避 1h/2h):
- scan_once:有 cookie 的账号(含 invalid)今天没采过 → enqueue(operator_id=0);
  无 cookie 的账号跳过;
- 今天已有快照 / 已有 done 台账行 → 跳过;
- 在途(queued)→ 跳过(连跑两轮不重复 enqueue);
- 失败退避:第 1 次 error 后未满 1h 跳过、满 1h 补采;第 2 次 error 后未满 2h 跳过;
- 当日 3 次用尽 → 跳过。
"""
from datetime import datetime, timedelta

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


async def _add_account(factory, name, cookie_status, with_cookies=True) -> int:
    async with factory() as s:
        acc = XhsAccount(name=name, cookie_status=cookie_status)
        if with_cookies:
            acc.login_cookies = "encrypted-blob"
        s.add(acc)
        await s.commit()
        return acc.id


async def _add_export_job(factory, acc_id, status, minutes_ago=0, job_id=None) -> None:
    async with factory() as s:
        s.add(BrowserJob(
            id=job_id or f"j-{acc_id}-{status}-{minutes_ago}",
            kind="note_export", account_id=acc_id, operator_id=3,
            payload="{}", status=status,
            created_at=datetime.utcnow() - timedelta(minutes=minutes_ago),
        ))
        await s.commit()


async def _export_jobs(factory) -> list[BrowserJob]:
    async with factory() as s:
        return list((await s.execute(
            select(BrowserJob).where(BrowserJob.kind == "note_export")
        )).scalars().all())


async def test_scan_covers_all_accounts_with_cookies(smk):
    """有 cookie 的账号全采(含 invalid,失败留痕);无 cookie 跳过;operator_id=0。"""
    valid_id = await _add_account(smk, "有效号", "valid")
    invalid_id = await _add_account(smk, "失效号", "invalid")
    await _add_account(smk, "未接入号", "unknown", with_cookies=False)

    sched = NoteMetricsScheduler(smk, interval=999)
    assert await sched.scan_once() == 2

    jobs = await _export_jobs(smk)
    assert sorted(j.account_id for j in jobs) == sorted([valid_id, invalid_id])
    assert all(j.operator_id == 0 and j.status == "queued" for j in jobs)


async def test_scan_skips_snapshot_done_and_inflight(smk):
    """今天有快照 / 已有 done 行 / 在途 queued → 三种都跳过;连跑两轮不重复。"""
    snap_id = await _add_account(smk, "已快照", "valid")
    done_id = await _add_account(smk, "已成功", "valid")
    flight_id = await _add_account(smk, "在途", "valid")
    from datetime import timezone
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    async with smk() as s:
        s.add(NoteMetricDaily(
            account_id=snap_id, title="t", publish_time="p", snapshot_date=today,
        ))
        await s.commit()
    await _add_export_job(smk, done_id, "done")
    await _add_export_job(smk, flight_id, "queued")

    sched = NoteMetricsScheduler(smk, interval=999)
    assert await sched.scan_once() == 0
    # 幂等:再跑一轮仍 0
    assert await sched.scan_once() == 0


async def test_retry_backoff_geometric(smk):
    """失败退避:第 1 次 error 未满 1h 不补;满 1h 补采;第 2 次 error 未满 2h 不补;满 2h 补。"""
    acc = await _add_account(smk, "失败号", "valid")
    sched = NoteMetricsScheduler(smk, interval=999)

    # 第 1 次失败 30 分钟前 → 未满 1h,不补
    await _add_export_job(smk, acc, "error", minutes_ago=30, job_id="e1")
    assert await sched.scan_once() == 0

    # 把这次失败改成 70 分钟前 → 满 1h,补采(第 2 次)
    async with smk() as s:
        j = await s.get(BrowserJob, "e1")
        j.created_at = datetime.utcnow() - timedelta(minutes=70)
        await s.commit()
    assert await sched.scan_once() == 1

    # 第 2 次(刚 enqueue 的)也置为 error:e1 挪到 200min 前当第 1 次,第 2 次 90min 前
    # → 两次失败,退避升到 2h,最近一次 90min 不够 → 不补
    async with smk() as s:
        jobs = list((await s.execute(
            select(BrowserJob).where(BrowserJob.kind == "note_export",
                                     BrowserJob.account_id == acc)
        )).scalars().all())
        # 先算 min/max 再 mutate(改 created_at 会影响 key,先改再算会二者选中同一对象)
        second = max(jobs, key=lambda j: j.created_at)  # 刚 enqueue 的第 2 次
        first = min(jobs, key=lambda j: j.created_at)   # e1
        second.status = "error"
        second.created_at = datetime.utcnow() - timedelta(minutes=90)
        first.created_at = datetime.utcnow() - timedelta(minutes=200)
        await s.commit()
    assert await sched.scan_once() == 0

    # 第 2 次挪到 130 分钟前 → 满 2h,补采(第 3 次)
    async with smk() as s:
        j = await s.get(BrowserJob, second.id)
        j.created_at = datetime.utcnow() - timedelta(minutes=130)
        await s.commit()
    assert await sched.scan_once() == 1


async def test_daily_attempts_capped_at_three(smk):
    """当日已 3 次(全 error 且退避早已过)→ 次数用尽,不再补采。"""
    acc = await _add_account(smk, "耗尽号", "valid")
    # ⚠️ 偏移必须小到「无论几点跑都还在今天(UTC)」:原写 600/500/400 分钟前,凌晨跑时
    # 这些落到昨天被今日窗口过滤掉 → 次数不足 3 → 测试变红(实测 UTC 04:25 红)。
    # 次数上限在 scan_once 里先于退避判定,故记录多久以前无关紧要,只要属于今天。
    now = datetime.utcnow()
    day_start = datetime(now.year, now.month, now.day)
    for i in range(3):
        ago = min(i + 1, max(0, int((now - day_start).total_seconds() // 60) - 1))
        await _add_export_job(smk, acc, "error", minutes_ago=ago, job_id=f"x{i}")

    sched = NoteMetricsScheduler(smk, interval=999)
    assert await sched.scan_once() == 0
    assert len(await _export_jobs(smk)) == 3


async def test_ensure_baseline_new_account_enqueues_once(smk):
    """新号(从未有快照)→ 基底 enqueue;重复调用/今天已有台账行 → 幂等不重发。"""
    from app.services.note_metrics_scheduler import ensure_baseline

    acc = await _add_account(smk, "新号", "valid")
    assert await ensure_baseline(smk, acc) is True
    jobs = await _export_jobs(smk)
    assert len(jobs) == 1 and jobs[0].account_id == acc and jobs[0].operator_id == 0
    # 幂等:今天已有台账行,不再发
    assert await ensure_baseline(smk, acc) is False
    assert len(await _export_jobs(smk)) == 1


async def test_ensure_baseline_skips_account_with_history(smk):
    """已有任意日快照(基底在)→ 不触发。"""
    from app.services.note_metrics_scheduler import ensure_baseline

    acc = await _add_account(smk, "老号", "valid")
    async with smk() as s:
        s.add(NoteMetricDaily(
            account_id=acc, title="t", publish_time="p", snapshot_date="2026-07-01",
        ))
        await s.commit()
    assert await ensure_baseline(smk, acc) is False
    assert await _export_jobs(smk) == []
