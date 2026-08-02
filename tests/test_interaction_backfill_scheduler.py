"""补量自动续跑调度器单测:锁的是"它推得动、又不会推过头"。

这个组件存在的理由是**补量原本完不成**:存量九百多个组合、日上限每号 20 篇,
不自动续跑就得有人连续六天每天手动 POST 一次。所以第一条要锁的就是"有存量时真的会
登记";而它每 interval 秒醒一次、补量一轮却要十几分钟,所以第二条必须锁"在途时不叠加"
—— 队列里堆一串同号补量任务,正是被平台看出补量特征的样子。

第三条锁的是**边界纪律**:所有风控闸(日上限 / 单轮上限 / 冷却 / 选篇优先级)都归
``plan_round`` 判,调度器一条都不许自己复制一份 —— 两套口径迟早对不上。因此
"挑不出 actor 就不登记"这条用例直接 patch ``plan_round`` 返回空来验,不去构造数据:
构造数据等于在测 plan_round,那是另一个文件的事。

patch 纪律:打在**被测模块的命名空间**(``sched.plan_round``),不是源模块。
"""

import json
from datetime import datetime

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.models.browser_job import BrowserJob
from app.models.published_note import PublishedNote
from app.models.xhs_account import XhsAccount
from app.services import interaction_backfill_scheduler as sched


@pytest_asyncio.fixture
async def session_factory(tmp_path):
    """临时文件库 + 会话工厂(调度器只吃 session_factory,不碰全局 engine)。"""
    from app.core.db import Base

    import app.models  # noqa: F401  触发模型注册

    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path}/sched.db", future=True
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    try:
        yield factory
    finally:
        await engine.dispose()


async def _seed_matrix(factory) -> None:
    """两个 valid 号 + 各一篇公开笔记:互相都有得补,plan_round 必挑得出 actor。"""
    async with factory() as s:
        for aid in (1, 2):
            s.add(XhsAccount(
                id=aid, name=f"号{aid}", user_id=f"u-{aid}",
                cookie_status="valid", login_cookies="enc",
            ))
            s.add(PublishedNote(
                account_id=aid, note_id=f"n{aid}", title=f"标题{aid}",
                published_at=datetime(2026, 7, 1),
                platform_published_at=datetime(2026, 7, 1),
                first_seen_at=datetime(2026, 7, 1),
                permission_code=0, sync_status="orphan",
            ))
        await s.commit()


async def _jobs(factory) -> list[BrowserJob]:
    async with factory() as s:
        return list((await s.execute(
            select(BrowserJob).where(BrowserJob.kind == "interaction_backfill")
        )).scalars().all())


@pytest.mark.asyncio
async def test_enqueues_when_backlog_exists(session_factory):
    """有存量就登记一条 queued 台账行 —— 这正是"没人手动戳也能补完"的全部依据。"""
    await _seed_matrix(session_factory)

    enqueued = await sched.InteractionBackfillScheduler(session_factory, 1).scan_once()

    assert enqueued == 1
    jobs = await _jobs(session_factory)
    assert len(jobs) == 1
    job = jobs[0]
    assert job.status == "queued"
    # account_id 必须是 plan_round 挑出的 actor:派发按账号分子进程、号锁与 profile 都靠它
    assert job.account_id in (1, 2)
    payload = json.loads(job.payload)
    assert payload["scope"] == "all"
    assert payload["actor_account_id"] == job.account_id
    # limit 留空:执行时会再挑一次篇,拿登记那刻的快照去做等于绕过日上限
    assert payload["limit"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["queued", "running"])
async def test_never_stacks_on_in_flight_round(session_factory, status):
    """已有在途补量任务时一条都不加。

    一轮补量要十几分钟(篇间刻意停 60-240 秒),扫描间隔比它短是常态。不挡这一下,
    队列里就会堆一串同号补量任务 —— 那正是平台眼里最典型的补量特征。
    """
    await _seed_matrix(session_factory)
    async with session_factory() as s:
        s.add(BrowserJob(
            id="inflight", kind="interaction_backfill", account_id=1,
            operator_id=0, payload="{}", status=status,
        ))
        await s.commit()

    enqueued = await sched.InteractionBackfillScheduler(session_factory, 1).scan_once()

    assert enqueued == 0
    assert len(await _jobs(session_factory)) == 1  # 还是那条在途的,没多出来


@pytest.mark.asyncio
async def test_finished_rounds_do_not_block_next(session_factory):
    """终态(done/error)的历史任务不算在途 —— 否则补完第一轮就再也不会有第二轮。"""
    await _seed_matrix(session_factory)
    async with session_factory() as s:
        s.add(BrowserJob(id="d", kind="interaction_backfill", account_id=1,
                         operator_id=0, payload="{}", status="done"))
        s.add(BrowserJob(id="e", kind="interaction_backfill", account_id=2,
                         operator_id=0, payload="{}", status="error"))
        await s.commit()

    assert await sched.InteractionBackfillScheduler(session_factory, 1).scan_once() == 1


@pytest.mark.asyncio
async def test_no_actor_means_no_job(session_factory, monkeypatch):
    """plan_round 挑不出 actor(都到日上限 / 补完了)时不登记。

    开一个注定空转的浏览器任务毫无意义,还白占号锁与浏览器闸。这里直接 patch
    plan_round 返回空:调度器**不自己判任何闸**,该由 plan_round 说了算。
    """
    await _seed_matrix(session_factory)

    async def _empty(*args, **kwargs):
        return {"actor_account_id": None, "targets": [], "reason": "都到日上限了"}

    monkeypatch.setattr(sched, "plan_round", _empty)

    assert await sched.InteractionBackfillScheduler(session_factory, 1).scan_once() == 0
    assert await _jobs(session_factory) == []


@pytest.mark.asyncio
async def test_other_kinds_do_not_count_as_in_flight(session_factory):
    """别的 kind 在途不该挡补量:各类任务本就并行排队,挡了等于无谓地饿死补量。"""
    await _seed_matrix(session_factory)
    async with session_factory() as s:
        s.add(BrowserJob(id="x", kind="note_export", account_id=1,
                         operator_id=0, payload="{}", status="running"))
        await s.commit()

    assert await sched.InteractionBackfillScheduler(session_factory, 1).scan_once() == 1
