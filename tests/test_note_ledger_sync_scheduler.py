"""台账同步保底调度器单测:锁"纯手工发布的号也会被同步到"(套受众/补量调度器的测法)。

这个组件补的是一个**真缺口**:台账同步原本只有两个触发点——发布任务完成后的钩子、
REST 手工触发。系统常发的号一天被同步好几次,而纯手工运营的号(老板定的禁发号)
十几天只被人手工戳过一次 —— 它手工发的笔记全都不在 published_notes 里,
互动补量选篇要求 note_id 非空,于是**自然流量最好的那个号反而永远补不到量**。

三条纪律照搬同族调度器(每条都是实测踩出来的):

1. **只插 queued 台账行,不 spawn_inline**:调度器活在 supervisor 进程里,inline 执行等于
   让 supervisor 自己起浏览器,违背 API/Worker 拆分("浏览器只在 account_worker 子进程");
2. **单 kind 在飞不叠**:发布钩子 / REST 手工触发插的单也算在飞 —— 保底调度给它们让路,
   队列里堆一串同类任务本身就是被平台看出自动化特征的样子;
3. **挑不出就跳过**:全都还新鲜时不空转,白开一次浏览器会话就白烧一次该号
   一小时只有 4 次的会话额度。
"""

import json
from datetime import datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.models.browser_job import BrowserJob
from app.models.xhs_account import XhsAccount
from app.services import note_ledger_sync_scheduler as sched

_SCAN_INTERVAL = 600
_MIN_INTERVAL = 43200  # 12 小时


@pytest_asyncio.fixture
async def session_factory(tmp_path):
    """临时文件库 + 会话工厂(调度器只吃 session_factory,不碰全局 engine)。"""
    from app.core.db import Base

    import app.models  # noqa: F401  触发模型注册

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/led.db", future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    try:
        yield factory
    finally:
        await engine.dispose()


async def _seed_account(
    factory, account_id: int, *, managed=True, cookie="valid", cookies="enc"
) -> None:
    async with factory() as s:
        s.add(XhsAccount(
            id=account_id, name=f"号{account_id}", user_id=f"u-{account_id}",
            cookie_status=cookie, login_cookies=cookies, managed=managed,
        ))
        await s.commit()


async def _seed_sync_job(
    factory, account_id: int, *, hours_ago: float, status="done", job_id=None
) -> None:
    """给某号写一条历史台账同步单,created_at 设成 N 小时前。"""
    when = datetime.utcnow() - timedelta(hours=hours_ago)
    async with factory() as s:
        s.add(BrowserJob(
            id=job_id or f"j-{account_id}-{hours_ago}-{status}",
            kind=sched.JOB_KIND, account_id=account_id, operator_id=0,
            payload="{}", status=status, created_at=when, updated_at=when,
        ))
        await s.commit()


async def _jobs(factory) -> list[BrowserJob]:
    async with factory() as s:
        return list((await s.execute(
            select(BrowserJob).where(BrowserJob.kind == sched.JOB_KIND)
        )).scalars().all())


def _scheduler(factory):
    return sched.NoteLedgerSyncScheduler(factory, _SCAN_INTERVAL, _MIN_INTERVAL)


@pytest.mark.asyncio
async def test_never_synced_account_gets_enqueued(session_factory):
    """从没同步过的号:登记一条同步单,字段与手工触发同构(payload 空、operator_id=0)。"""
    await _seed_account(session_factory, 1)

    assert await _scheduler(session_factory).scan_once() == 1

    jobs = await _jobs(session_factory)
    assert len(jobs) == 1
    job = jobs[0]
    assert job.kind == "note_ledger_sync"
    assert job.status == "queued", "必须只插 queued 行,派子进程的事归 supervisor"
    assert job.account_id == 1
    assert job.operator_id == 0, "非请求上下文的进程内直调,不占运营的未终态配额"
    assert json.loads(job.payload) == {}


@pytest.mark.asyncio
async def test_stale_account_gets_enqueued(session_factory):
    """超过 12 小时没同步的号照样排上队(保底就是为这种号存在的)。"""
    await _seed_account(session_factory, 1)
    await _seed_sync_job(session_factory, 1, hours_ago=20)

    assert await _scheduler(session_factory).scan_once() == 1
    assert len(await _jobs(session_factory)) == 2


@pytest.mark.asyncio
async def test_recently_synced_account_is_skipped(session_factory):
    """12 小时内同步过的号不选 —— 常发的号被发布钩子刷着,保底不该重复推。"""
    await _seed_account(session_factory, 1)
    await _seed_sync_job(session_factory, 1, hours_ago=2)

    assert await _scheduler(session_factory).scan_once() == 0
    assert len(await _jobs(session_factory)) == 1


@pytest.mark.asyncio
async def test_last_sync_counts_any_terminal_state(session_factory):
    """"最后一次同步"不分终态:失败单也算跑过一次,否则一直失败的号会被反复推。"""
    await _seed_account(session_factory, 1)
    await _seed_sync_job(session_factory, 1, hours_ago=1, status="error")

    assert await _scheduler(session_factory).scan_once() == 0


@pytest.mark.asyncio
async def test_oldest_first_one_per_round(session_factory):
    """一轮只派一个号,先派最久没同步的那个。"""
    await _seed_account(session_factory, 1)
    await _seed_account(session_factory, 2)
    await _seed_sync_job(session_factory, 1, hours_ago=15)
    await _seed_sync_job(session_factory, 2, hours_ago=40)

    assert await _scheduler(session_factory).scan_once() == 1

    new_jobs = [j for j in await _jobs(session_factory) if j.status == "queued"]
    assert len(new_jobs) == 1 and new_jobs[0].account_id == 2


@pytest.mark.asyncio
async def test_never_synced_beats_stale(session_factory):
    """从没同步过的号视为"无穷久",排在"很久没同步"的号前面 —— 它缺口最大。"""
    await _seed_account(session_factory, 1)
    await _seed_account(session_factory, 2)
    await _seed_sync_job(session_factory, 1, hours_ago=99999)

    assert await _scheduler(session_factory).scan_once() == 1

    new_jobs = [j for j in await _jobs(session_factory) if j.status == "queued"]
    assert len(new_jobs) == 1 and new_jobs[0].account_id == 2


@pytest.mark.asyncio
async def test_latest_sync_wins_over_older_ones(session_factory):
    """该号有多条历史单时按**最新那条**判到期,否则老单会让它每轮都被重复推。"""
    await _seed_account(session_factory, 1)
    await _seed_sync_job(session_factory, 1, hours_ago=99, job_id="old")
    await _seed_sync_job(session_factory, 1, hours_ago=1, job_id="fresh")

    assert await _scheduler(session_factory).scan_once() == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["queued", "running"])
async def test_never_stacks_on_in_flight(session_factory, status):
    """已有在飞同步单(发布钩子/手工触发插的也算)时一条都不加:保底给它们让路。"""
    await _seed_account(session_factory, 1)
    await _seed_account(session_factory, 2)
    async with session_factory() as s:
        s.add(BrowserJob(id="inflight", kind=sched.JOB_KIND, account_id=1,
                         operator_id=0, payload="{}", status=status))
        await s.commit()

    assert await _scheduler(session_factory).scan_once() == 0
    assert len(await _jobs(session_factory)) == 1


@pytest.mark.asyncio
async def test_other_kinds_do_not_block(session_factory):
    """别的 kind 在飞不该挡台账同步:各类任务本就并行排队。"""
    await _seed_account(session_factory, 1)
    async with session_factory() as s:
        s.add(BrowserJob(id="x", kind="note_export", account_id=1,
                         operator_id=0, payload="{}", status="running"))
        await s.commit()

    assert await _scheduler(session_factory).scan_once() == 1


@pytest.mark.asyncio
async def test_invalid_cookie_account_skipped(session_factory):
    """cookie 已失效的号不派:浏览器起来也只会撞登录页,白烧一次会话额度。"""
    await _seed_account(session_factory, 1, cookie="invalid")

    assert await _scheduler(session_factory).scan_once() == 0
    assert await _jobs(session_factory) == []


@pytest.mark.asyncio
async def test_account_without_cookies_skipped(session_factory):
    """从没登录过(没有 login_cookies)的号不派 —— 同理,只会撞登录页。"""
    await _seed_account(session_factory, 1, cookies=None)

    assert await _scheduler(session_factory).scan_once() == 0


@pytest.mark.asyncio
async def test_covers_unmanaged_accounts_too(session_factory):
    """水军号(managed=0)的台账也要新鲜 —— 孤儿行判定依赖它,故**不分代管**。"""
    await _seed_account(session_factory, 9, managed=False)

    assert await _scheduler(session_factory).scan_once() == 1
    assert (await _jobs(session_factory))[0].account_id == 9
