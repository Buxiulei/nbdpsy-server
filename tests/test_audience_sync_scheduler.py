"""受众采集调度器单测:锁"它推得动、又不会推过头"(套补量调度器的测法)。

三条纪律各对应一次真实代价:

1. **只插 queued 台账行,不 spawn_inline**:调度器活在 supervisor 进程里,inline 执行等于
   让 supervisor 自己起浏览器,违背 API/Worker 拆分("浏览器只在 account_worker 子进程");
2. **单 kind 在飞不叠**:一轮采集要开一次真号会话、滚好几分钟,扫描间隔比它短是常态。
   不挡一下队列里就会堆一串同号采集单 —— 而"队列里堆着一串同类任务"正是被平台看出
   自动化特征的样子;
3. **挑不出就跳过**:开一个注定空转的浏览器任务毫无意义,还白占号锁与浏览器闸,
   更白烧一次该号一小时只有 4 次的会话额度。
"""

import json
from datetime import datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.models.audience_sync_state import AudienceSyncState
from app.models.browser_job import BrowserJob
from app.models.xhs_account import XhsAccount
from app.services import audience_sync_scheduler as sched

_INTERVAL = 3600


@pytest_asyncio.fixture
async def session_factory(tmp_path):
    """临时文件库 + 会话工厂(调度器只吃 session_factory,不碰全局 engine)。"""
    from app.core.db import Base

    import app.models  # noqa: F401  触发模型注册

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/aud.db", future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    try:
        yield factory
    finally:
        await engine.dispose()


async def _seed_account(factory, account_id: int, *, managed=True, cookie="valid") -> None:
    async with factory() as s:
        s.add(XhsAccount(
            id=account_id, name=f"号{account_id}", user_id=f"u-{account_id}",
            cookie_status=cookie, login_cookies="enc", managed=managed,
        ))
        await s.commit()


async def _seed_state(factory, account_id: int, *, minutes_ago: int) -> None:
    """给某号写两条 channel 游标行,updated_at 设成 N 分钟前。"""
    when = datetime.utcnow() - timedelta(minutes=minutes_ago)
    async with factory() as s:
        for channel in ("likes", "connections"):
            s.add(AudienceSyncState(
                account_id=account_id, channel=channel,
                last_event_time=1_700_000_000, updated_at=when,
            ))
        await s.commit()


async def _jobs(factory) -> list[BrowserJob]:
    async with factory() as s:
        return list((await s.execute(
            select(BrowserJob).where(BrowserJob.kind == sched.JOB_KIND)
        )).scalars().all())


def _scheduler(factory):
    return sched.AudienceSyncScheduler(factory, _INTERVAL)


@pytest.mark.asyncio
async def test_first_time_account_gets_a_full_sync(session_factory):
    """从没采过的代管号:登记一条 **full** 采集单(历史一次性回采到底)。"""
    await _seed_account(session_factory, 1)

    assert await _scheduler(session_factory).scan_once() == 1

    jobs = await _jobs(session_factory)
    assert len(jobs) == 1
    job = jobs[0]
    assert job.status == "queued", "必须只插 queued 行,派子进程的事归 supervisor"
    assert job.account_id == 1
    payload = json.loads(job.payload)
    assert payload == {"account_id": 1, "full": True}


@pytest.mark.asyncio
async def test_due_account_gets_incremental_sync(session_factory):
    """已采过且到期的号:登记**增量**单(full=False),不再翻 47 页。"""
    await _seed_account(session_factory, 1)
    await _seed_state(session_factory, 1, minutes_ago=120)

    assert await _scheduler(session_factory).scan_once() == 1

    assert json.loads((await _jobs(session_factory))[0].payload)["full"] is False


@pytest.mark.asyncio
async def test_not_due_yet_is_skipped(session_factory):
    """刚采过(还没到 interval)就跳过:空转一轮的代价是一次真号会话额度。"""
    await _seed_account(session_factory, 1)
    await _seed_state(session_factory, 1, minutes_ago=5)

    assert await _scheduler(session_factory).scan_once() == 0
    assert await _jobs(session_factory) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["queued", "running"])
async def test_never_stacks_on_in_flight(session_factory, status):
    """已有在飞采集单时一条都不加(不分账号:队列里堆一串同类任务就是自动化特征)。"""
    await _seed_account(session_factory, 1)
    await _seed_account(session_factory, 2)
    async with session_factory() as s:
        s.add(BrowserJob(id="inflight", kind=sched.JOB_KIND, account_id=1,
                         operator_id=0, payload="{}", status=status))
        await s.commit()

    assert await _scheduler(session_factory).scan_once() == 0
    assert len(await _jobs(session_factory)) == 1


@pytest.mark.asyncio
async def test_finished_jobs_do_not_block(session_factory):
    """终态历史单不算在飞 —— 否则采完第一轮就再也不会有第二轮。"""
    await _seed_account(session_factory, 1)
    async with session_factory() as s:
        s.add(BrowserJob(id="d", kind=sched.JOB_KIND, account_id=1,
                         operator_id=0, payload="{}", status="done"))
        s.add(BrowserJob(id="e", kind=sched.JOB_KIND, account_id=1,
                         operator_id=0, payload="{}", status="error"))
        await s.commit()

    assert await _scheduler(session_factory).scan_once() == 1


@pytest.mark.asyncio
async def test_other_kinds_do_not_block(session_factory):
    """别的 kind 在飞不该挡采集:各类任务本就并行排队,挡了等于无谓地饿死受众库。"""
    await _seed_account(session_factory, 1)
    async with session_factory() as s:
        s.add(BrowserJob(id="x", kind="note_export", account_id=1,
                         operator_id=0, payload="{}", status="running"))
        await s.commit()

    assert await _scheduler(session_factory).scan_once() == 1


@pytest.mark.asyncio
async def test_only_managed_accounts(session_factory):
    """只采代管号(内容号)。水军号的通知流是它自己的社交噪音,不是我们的受众。"""
    await _seed_account(session_factory, 9, managed=False)

    assert await _scheduler(session_factory).scan_once() == 0


@pytest.mark.asyncio
async def test_invalid_cookie_account_skipped(session_factory):
    """cookie 已失效的号不派:浏览器起来也只会撞登录页,白烧一次会话额度。"""
    await _seed_account(session_factory, 1, cookie="invalid")

    assert await _scheduler(session_factory).scan_once() == 0


@pytest.mark.asyncio
async def test_one_job_per_round_and_oldest_first(session_factory):
    """一轮只派一个号,先派最久没采的那个(避免堆队列被平台看出特征)。"""
    await _seed_account(session_factory, 1)
    await _seed_account(session_factory, 2)
    await _seed_state(session_factory, 1, minutes_ago=90)
    await _seed_state(session_factory, 2, minutes_ago=600)

    assert await _scheduler(session_factory).scan_once() == 1

    jobs = await _jobs(session_factory)
    assert len(jobs) == 1 and jobs[0].account_id == 2


@pytest.mark.asyncio
async def test_never_synced_beats_stale(session_factory):
    """从没采过的号排在"很久没采"的号前面 —— 它一条数据都没有,缺口最大。"""
    await _seed_account(session_factory, 1)
    await _seed_account(session_factory, 2)
    await _seed_state(session_factory, 1, minutes_ago=99999)

    assert await _scheduler(session_factory).scan_once() == 1
    assert (await _jobs(session_factory))[0].account_id == 2


@pytest.mark.asyncio
async def test_partially_synced_account_uses_oldest_channel(session_factory):
    """两条 channel 只采了一条时按**最旧的那条**判到期,漏采的那条才不会永远排不上。"""
    await _seed_account(session_factory, 1)
    async with session_factory() as s:
        s.add(AudienceSyncState(account_id=1, channel="likes", last_event_time=1,
                                updated_at=datetime.utcnow()))
        s.add(AudienceSyncState(account_id=1, channel="connections", last_event_time=1,
                                updated_at=datetime.utcnow() - timedelta(hours=5)))
        await s.commit()

    assert await _scheduler(session_factory).scan_once() == 1
