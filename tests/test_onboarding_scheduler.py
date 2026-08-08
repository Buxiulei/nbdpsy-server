"""新号接入调度器单测:锁的是"它真的会推新号,又不会把额度推光"。

这个组件存在的理由是**转正引导链在生产从未触发过**:引导链只挂在手动 REST 检测与周期
巡检两条路上,而周期巡检 COOKIE_CHECK_INTERVAL 默认 0、生产也没开 —— 新号除非有人手动
戳一次,否则永远卡在 unknown。所以第一条要锁的就是"有 cookie 的 unknown 号真的会被登记"。

其余用例锁的是**不推过头**,每条对应一种真实后果:
- 选号错(选中 valid/invalid/无 cookie 的号)= 平白多烧浏览器会话,还可能把人工判过的
  失效号反复叫醒;
- 在途叠加 = 队列里堆一串同号检测,正是被平台看出批量特征的样子;**且在途判据不带
  时间窗** —— 撞会话总闸的任务会在 queued 里停很久,按时间窗判会把它当"过期"再叠一条,
  所以专门有一条用例把在途行的 created_at 推到退避窗之外验它仍去重;
- 无退避 = 一个检测不过去的号以扫描间隔无限重试,独自打满自己的会话额度。

退避那条用例刻意用 **error 结果**(而不是 invalid):invalid/captcha/restricted 都会被
_write_back 写回账号、自己从候选里掉出去,唯一会留在 unknown 里反复入选的就是 error
(基础设施失败按设计不写回)。用 invalid 构造等于测了一个到不了的分支。

patch 纪律:打在**被测模块的命名空间**(worker 装配用例 patch app.worker 里的名字),
不是源模块。
"""

from datetime import datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.models.browser_job import BrowserJob
from app.models.xhs_account import XhsAccount
from app.services.onboarding_scheduler import OnboardingScheduler

_RETRY_HOURS = 1


@pytest_asyncio.fixture
async def session_factory(tmp_path):
    """临时文件库 + 会话工厂(调度器只吃 session_factory,不碰全局 engine)。"""
    from app.core.db import Base

    import app.models  # noqa: F401  触发模型注册

    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path}/onboarding.db", future=True
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    try:
        yield factory
    finally:
        await engine.dispose()


async def _add_account(factory, account_id: int, status: str, cookies: str | None) -> None:
    async with factory() as s:
        s.add(XhsAccount(
            id=account_id, name=f"号{account_id}",
            cookie_status=status, login_cookies=cookies,
        ))
        await s.commit()


async def _add_job(factory, account_id: int, status: str, created_at: datetime,
                   result: str | None = None) -> str:
    """插一条 cookie_check 台账行(created_at 显式给,便于构造退避窗内外)。"""
    job_id = f"job-{account_id}-{created_at.timestamp()}-{status}"
    async with factory() as s:
        s.add(BrowserJob(
            id=job_id, kind="cookie_check", account_id=account_id,
            operator_id=0, payload="{}", status=status, result=result,
            created_at=created_at, updated_at=created_at,
        ))
        await s.commit()
    return job_id


async def _checks(factory) -> list[BrowserJob]:
    async with factory() as s:
        return list((await s.execute(
            select(BrowserJob).where(BrowserJob.kind == "cookie_check")
            .order_by(BrowserJob.created_at)
        )).scalars().all())


def _sched(factory) -> OnboardingScheduler:
    return OnboardingScheduler(factory, 300, _RETRY_HOURS)


@pytest.mark.asyncio
async def test_enqueues_check_for_unknown_account_with_cookies(session_factory):
    """有 cookie 的 unknown 号被登记一条 queued 系统检测 —— 这正是"没人戳也能转正"的全部依据。"""
    await _add_account(session_factory, 12, "unknown", "enc-cookie")

    enqueued = await _sched(session_factory).scan_once()

    assert enqueued == 1
    jobs = await _checks(session_factory)
    assert len(jobs) == 1
    job = jobs[0]
    assert job.kind == "cookie_check"
    assert job.account_id == 12
    assert job.status == "queued"
    # operator_id=0 = 系统自发任务:走系统层会话总闸(4 次/时)而非运营层(12 次/时)
    assert job.operator_id == 0
    assert job.payload == "{}"
    assert job.claimed_by is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("cookie_status", "cookies", "why"),
    [
        ("valid", "enc-cookie", "已转正的老号不该被自动复检——那是周期巡检的活,不是本组件的"),
        ("invalid", "enc-cookie", "失效号需人工重登,自动重试只会白烧会话"),
        ("captcha", "enc-cookie", "撞验证墙的号继续起浏览器只会把限流催得更狠"),
        ("restricted", "enc-cookie", "风控受限同上,需人工处置"),
        ("unknown", None, "没 cookie 可检,检了必然 invalid"),
        ("unknown", "", "空串 cookie 同上"),
    ],
)
async def test_skips_accounts_outside_candidate_set(
    session_factory, cookie_status, cookies, why
):
    """候选集只有「unknown + 有 cookie」;其余一律不登记。"""
    await _add_account(session_factory, 7, cookie_status, cookies)

    enqueued = await _sched(session_factory).scan_once()

    assert enqueued == 0, why
    assert await _checks(session_factory) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("in_flight_status", ["queued", "running"])
async def test_skips_when_check_in_flight(session_factory, in_flight_status):
    """同号已有在途检测就不叠 —— 且在途判据**不看年龄**。

    在途行刻意造得比退避窗还老:撞上会话总闸的任务会长期停在 queued(总闸的语义就是
    "留在队列里等下轮重估"),若在途也按时间窗判,这种行会被当成过期再叠一条,
    队列里就此堆出一串同号检测。
    """
    await _add_account(session_factory, 12, "unknown", "enc-cookie")
    stale = datetime.utcnow() - timedelta(hours=_RETRY_HOURS * 5)
    await _add_job(session_factory, 12, in_flight_status, stale)

    enqueued = await _sched(session_factory).scan_once()

    assert enqueued == 0
    assert len(await _checks(session_factory)) == 1  # 还是原来那条,没多出来


@pytest.mark.asyncio
async def test_backoff_blocks_retry_until_window_passes(session_factory):
    """检测以 error 收场(账号仍是 unknown)时,退避窗内不重试,窗外才重试。

    error 是唯一会把账号留在 unknown 的结果(基础设施失败按设计不写回账号状态),
    也就是这个退避真正治理的场景。
    """
    await _add_account(session_factory, 12, "unknown", "enc-cookie")
    sched = _sched(session_factory)

    assert await sched.scan_once() == 1
    jobs = await _checks(session_factory)
    assert len(jobs) == 1
    job_id = jobs[0].id

    # 第一轮那条走完:台账 done、结果 error;账号按设计保留 unknown,仍在候选集里
    async with session_factory() as s:
        row = await s.get(BrowserJob, job_id)
        row.status = "done"
        row.result = '{"status": "error", "reason": "检测超时"}'
        await s.commit()

    # 退避窗内:仍是候选、也没有在途,但不该重登 —— 否则一个坏号按扫描间隔无限重试
    assert await sched.scan_once() == 0
    assert len(await _checks(session_factory)) == 1

    # 把那条推到退避窗之外(模拟时间流逝),退避解除,允许再试一次
    async with session_factory() as s:
        row = await s.get(BrowserJob, job_id)
        row.created_at = datetime.utcnow() - timedelta(hours=_RETRY_HOURS, minutes=1)
        await s.commit()

    assert await sched.scan_once() == 1
    assert len(await _checks(session_factory)) == 2


@pytest.mark.asyncio
async def test_no_reenqueue_after_promotion(session_factory):
    """转正即出局:账号转 valid 后,即使退避窗早过了也不再登记。"""
    await _add_account(session_factory, 12, "unknown", "enc-cookie")
    sched = _sched(session_factory)
    assert await sched.scan_once() == 1

    # 检测成功 → _write_back 把账号写成 valid;台账行推到退避窗外排除退避的干扰
    async with session_factory() as s:
        account = await s.get(XhsAccount, 12)
        account.cookie_status = "valid"
        for row in (await s.execute(select(BrowserJob))).scalars().all():
            row.status = "done"
            row.created_at = datetime.utcnow() - timedelta(days=3)
        await s.commit()

    assert await sched.scan_once() == 0
    assert len(await _checks(session_factory)) == 1


@pytest.mark.asyncio
async def test_backoff_is_per_account(session_factory):
    """退避按号算:一个号在退避中,不该连累另一个刚接入的新号。"""
    await _add_account(session_factory, 12, "unknown", "enc-cookie")
    await _add_account(session_factory, 13, "unknown", "enc-cookie")
    await _add_job(session_factory, 12, "done", datetime.utcnow())

    enqueued = await _sched(session_factory).scan_once()

    assert enqueued == 1
    new_jobs = [j for j in await _checks(session_factory) if j.status == "queued"]
    assert [j.account_id for j in new_jobs] == [13]


# ── supervisor 装配:interval 语义与同族组件逐项一致(>0 构造并 start,=0 完全不构造)──


class _FakeScheduler:
    """替身:只记构造参数与 start/stop 是否被调用(不起真循环)。"""

    instances: list["_FakeScheduler"] = []

    def __init__(self, session_factory, interval, retry_hours):
        self.interval = interval
        self.retry_hours = retry_hours
        self.started = False
        self.stopped = False
        _FakeScheduler.instances.append(self)

    def start(self):
        self.started = True

    async def stop(self):
        self.stopped = True


async def _drive_components(tmp_path, monkeypatch, interval, retry_hours=3):
    import app.worker as worker_mod

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/w.db", future=True)
    smk = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr(worker_mod.settings, "ONBOARDING_CHECK_INTERVAL", interval)
    monkeypatch.setattr(worker_mod.settings, "ONBOARDING_CHECK_RETRY_HOURS", retry_hours)
    # 同族组件全部关掉,只留被测这一个:否则装配用例会连带起一堆真循环
    for field in ("COOKIE_CHECK_INTERVAL", "BROWSER_REAP_INTERVAL",
                  "PLACEHOLDER_REAP_INTERVAL", "ARCHIVE_REAP_INTERVAL",
                  "NOTE_METRICS_INTERVAL", "EGRESS_CHECK_INTERVAL",
                  "DRAFT_CLEAN_INTERVAL", "INTERACTION_BACKFILL_INTERVAL"):
        monkeypatch.setattr(worker_mod.settings, field, 0)
    _FakeScheduler.instances = []
    monkeypatch.setattr(worker_mod, "OnboardingScheduler", _FakeScheduler)

    sup = worker_mod.Supervisor(smk, include_video=False)
    try:
        await sup._start_components()
        await sup._stop_components()
    finally:
        await engine.dispose()
    return _FakeScheduler.instances


@pytest.mark.asyncio
async def test_supervisor_skips_scheduler_when_interval_zero(tmp_path, monkeypatch):
    """ONBOARDING_CHECK_INTERVAL=0:supervisor 完全不构造(与同族组件同款关停语义)。"""
    assert await _drive_components(tmp_path, monkeypatch, 0) == []


@pytest.mark.asyncio
async def test_supervisor_starts_and_stops_scheduler(tmp_path, monkeypatch):
    """interval>0:构造时把两个配置原样传进去,start 起、停机 stop。"""
    instances = await _drive_components(tmp_path, monkeypatch, 42, retry_hours=3)

    assert len(instances) == 1
    sched = instances[0]
    assert sched.interval == 42
    assert sched.retry_hours == 3
    assert sched.started is True
    assert sched.stopped is True
