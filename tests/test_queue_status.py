"""排队可见性(queue 段)与"与派发层口径一致"的钉子测试。

运营原话(2026-08-07):"任务排队的话,应该返回排队编号,并且显示当前执行的任务的编号,
这样就知道在排队,前面还有多少个任务。"当天全矩阵 9 个号满帽、11 条排队、running=0,
单条排 40 分钟以上,调用方只看得到 status=queued。

本文件钉三类事:

1. **位次与深度**:同账号 browser + publish 合成一条队列(派发层就是合着派的),
   排期未到点的不占位;
2. **三种 blocked_by 各自触发** + window_resets_at 算得对(取值不同的检验点:恰好满帽
   与超帽两格,答案必须落到不同的那次会话上);
3. **口径一致**:读侧数出来的 used 与派发层 ``_recent_session_counts`` 逐个账号相等,
   读侧判的 blocked 与派发层 ``_apply_session_cap`` 拦不拦同进同出。
"""

import json
import uuid
from datetime import datetime, timedelta

import pytest

import app.core.db as db_module
from app.models.browser_job import BrowserJob
from app.models.publish_job import PublishJob
from app.services import queue_status
from app.worker import Supervisor


# ---------------- 灌数据 ----------------


async def _browser(
    session,
    *,
    account_id=1,
    status="queued",
    kind="note_export",
    operator_id=0,
    created=None,
    updated=None,
    payload=None,
    job_id=None,
) -> str:
    jid = job_id or uuid.uuid4().hex
    now = datetime.utcnow()
    session.add(
        BrowserJob(
            id=jid,
            kind=kind,
            account_id=account_id,
            operator_id=operator_id,
            payload=json.dumps(payload or {}, ensure_ascii=False),
            status=status,
            created_at=created or now,
            updated_at=updated or created or now,
        )
    )
    await session.commit()
    return jid


async def _publish(
    session,
    *,
    account_id=1,
    status="pending",
    created=None,
    started=None,
    schedule_time=None,
    created_by=None,
) -> PublishJob:
    job = PublishJob(
        account_id=account_id,
        title="t",
        content="c",
        images_json="[]",
        topics_json="[]",
        status=status,
        created_at=created or datetime.utcnow(),
        started_at=started,
        schedule_time=schedule_time,
        created_by=created_by,
    )
    session.add(job)
    await session.commit()
    return job


async def _row(session, job_id: str) -> dict:
    """把台账行取成 for_browser_job 吃的 dict(payload 已反序列化)。"""
    job = await session.get(BrowserJob, job_id)
    return {
        "id": job.id,
        "status": job.status,
        "account_id": job.account_id,
        "operator_id": job.operator_id,
        "created_at": job.created_at,
        "payload": json.loads(job.payload),
    }


@pytest.fixture
def use_db(db_factory, monkeypatch):
    """把 get_session 指到测试库(queue_status 走 app.core.db.get_session)。"""
    monkeypatch.setattr(db_module, "async_session", db_factory)
    return db_factory


def _caps(monkeypatch, *, system=4, operator=12, max_procs=8):
    from app.core.config import settings

    monkeypatch.setattr(settings, "ACCOUNT_HOURLY_SESSION_CAP", system, raising=False)
    monkeypatch.setattr(
        settings, "ACCOUNT_HOURLY_OPERATOR_SESSION_CAP", operator, raising=False
    )
    monkeypatch.setattr(settings, "BROWSER_CONCURRENCY", max_procs, raising=False)


# ---------------- 位次 / 深度 ----------------


@pytest.mark.asyncio
async def test_position_counts_browser_and_publish_together(use_db, monkeypatch):
    """位次按 created_at 升序,且 browser 与 publish 合成**一条**队列。

    派发层把两者并进同一个 work[acc] 一起排序、一起吃这一轮名额,分开数会让运营看到
    "前面 0 个"却仍在等一条发布。
    """
    _caps(monkeypatch)
    base = datetime.utcnow() - timedelta(minutes=10)
    async with use_db() as s:
        await _browser(s, created=base)  # 第 1
        await _publish(s, created=base + timedelta(seconds=1))  # 第 2
        mine = await _browser(s, created=base + timedelta(seconds=2))  # 第 3
        await _browser(s, created=base + timedelta(seconds=3))  # 第 4
        await _browser(s, account_id=2, created=base)  # 别的号,不算
        row = await _row(s, mine)

    q = await queue_status.for_browser_job(row)
    assert q["position"] == 3, q
    assert q["ahead"] == 2
    assert q["account_queue_depth"] == 4


@pytest.mark.asyncio
async def test_future_not_before_excluded_from_queue(use_db, monkeypatch):
    """排期未到点的行不占队列位:派发层这一轮根本看不到它。"""
    _caps(monkeypatch)
    base = datetime.utcnow() - timedelta(minutes=10)
    future = (datetime.utcnow() + timedelta(hours=1)).isoformat()
    async with use_db() as s:
        await _browser(s, created=base, payload={"not_before": future})  # 不占位
        await _browser(s, created=base + timedelta(seconds=1))  # 第 1
        mine = await _browser(s, created=base + timedelta(seconds=2))  # 第 2
        row = await _row(s, mine)

    q = await queue_status.for_browser_job(row)
    assert q["position"] == 2, q
    assert q["account_queue_depth"] == 2


@pytest.mark.asyncio
async def test_self_not_due_gives_null_position_and_not_before(use_db, monkeypatch):
    """自己排期未到:位次 null,detail 给到点时刻,且不谎报被哪道闸拦住。"""
    _caps(monkeypatch)
    until = datetime.utcnow() + timedelta(minutes=30)
    async with use_db() as s:
        # 该号还有一条在跑:若判序错了就会被报成 account_busy
        await _browser(s, status="running", kind="note_ledger_sync")
        mine = await _browser(s, payload={"not_before": until.isoformat()})
        row = await _row(s, mine)

    q = await queue_status.for_browser_job(row)
    assert q["position"] is None and q["ahead"] is None
    assert q["blocked_by"] is None, q
    assert q["detail"]["not_before"].startswith(until.replace(microsecond=0).isoformat()[:16])


@pytest.mark.asyncio
async def test_terminal_and_running_have_no_queue(use_db, monkeypatch):
    """非排队态(running / done / error)queue 恒为 null。"""
    _caps(monkeypatch)
    async with use_db() as s:
        done = await _browser(s, status="done")
        running = await _browser(s, status="running")
        rows = [await _row(s, done), await _row(s, running)]

    for row in rows:
        assert await queue_status.for_browser_job(row) is None, row["status"]


@pytest.mark.asyncio
async def test_op_images_without_account_has_no_queue(use_db, monkeypatch):
    """无账号的 op_images 不排账号队列(supervisor 进程内直接执行),没有位次可言。"""
    _caps(monkeypatch)
    async with use_db() as s:
        jid = await _browser(s, account_id=None, kind="op_images")
        row = await _row(s, jid)

    assert await queue_status.for_browser_job(row) is None


# ---------------- running 段 ----------------


@pytest.mark.asyncio
async def test_running_section_reports_browser_job(use_db, monkeypatch):
    """running 给该号当前在执行的任务;browser 类没有开始时刻列,started_at 给 null。"""
    _caps(monkeypatch)
    beat = datetime.utcnow() - timedelta(minutes=2)
    async with use_db() as s:
        await _browser(s, status="running", kind="note_ledger_sync", job_id="run-1")
        async with db_module.async_session() as s2:
            job = await s2.get(BrowserJob, "run-1")
            job.heartbeat_at = beat
            await s2.commit()
        mine = await _browser(s)
        row = await _row(s, mine)

    q = await queue_status.for_browser_job(row)
    assert q["running"]["id"] == "run-1"
    assert q["running"]["kind"] == "note_ledger_sync"
    assert q["running"]["started_at"] is None
    assert q["running"]["heartbeat_at"].endswith("+00:00")


@pytest.mark.asyncio
async def test_running_section_reports_publish_job(use_db, monkeypatch):
    """该号在发布时,running 给发布任务(kind=publish)与真实 started_at。"""
    _caps(monkeypatch)
    started = datetime.utcnow() - timedelta(minutes=3)
    async with use_db() as s:
        pub = await _publish(s, status="publishing", started=started)
        mine = await _browser(s)
        row = await _row(s, mine)

    q = await queue_status.for_browser_job(row)
    assert q["running"]["id"] == pub.id
    assert q["running"]["kind"] == "publish"
    assert q["running"]["started_at"].startswith(started.replace(microsecond=0).isoformat()[:16])


@pytest.mark.asyncio
async def test_running_none_when_account_idle(use_db, monkeypatch):
    _caps(monkeypatch)
    async with use_db() as s:
        mine = await _browser(s)
        row = await _row(s, mine)

    q = await queue_status.for_browser_job(row)
    assert q["running"] is None
    assert q["blocked_by"] is None


# ---------------- blocked_by 三态 ----------------


@pytest.mark.asyncio
async def test_blocked_by_account_busy(use_db, monkeypatch):
    """同号已有任务在跑 → account_busy(同号严格串行,派发层第一道 continue)。"""
    _caps(monkeypatch)
    async with use_db() as s:
        await _browser(s, status="running", kind="cookie_check", job_id="busy-1")
        mine = await _browser(s)
        row = await _row(s, mine)

    q = await queue_status.for_browser_job(row)
    assert q["blocked_by"] == "account_busy"
    assert q["detail"]["running_id"] == "busy-1"


@pytest.mark.asyncio
async def test_blocked_by_global_concurrency(use_db, monkeypatch):
    """全局在跑账号数达 max_procs → global_concurrency(派发层第二道 break)。"""
    _caps(monkeypatch, max_procs=2)
    async with use_db() as s:
        await _browser(s, account_id=7, status="running")
        await _browser(s, account_id=8, status="running")
        mine = await _browser(s, account_id=1)
        row = await _row(s, mine)

    q = await queue_status.for_browser_job(row)
    assert q["blocked_by"] == "global_concurrency"
    assert q["detail"] == {"running_procs": 2, "max_procs": 2}


@pytest.mark.asyncio
async def test_blocked_by_session_cap_system_layer(use_db, monkeypatch):
    """系统自发任务(operator_id=0)超系统帽 → session_cap / kind_of_cap=system。"""
    _caps(monkeypatch, system=4, operator=12)
    ago = datetime.utcnow() - timedelta(minutes=5)
    async with use_db() as s:
        for _ in range(4):
            await _browser(s, status="done", updated=ago)
        mine = await _browser(s, operator_id=0)
        row = await _row(s, mine)

    q = await queue_status.for_browser_job(row)
    assert q["blocked_by"] == "session_cap"
    assert q["detail"]["used"] == 4
    assert q["detail"]["cap"] == 4
    assert q["detail"]["kind_of_cap"] == "system"


@pytest.mark.asyncio
async def test_operator_layer_passes_where_system_layer_blocks(use_db, monkeypatch):
    """同一份计数下,运营触发任务吃更宽的帽 —— 系统层拦住的它照样放行。"""
    _caps(monkeypatch, system=4, operator=12)
    ago = datetime.utcnow() - timedelta(minutes=5)
    async with use_db() as s:
        for _ in range(4):
            await _browser(s, status="done", updated=ago)
        mine = await _browser(s, operator_id=9)
        row = await _row(s, mine)

    q = await queue_status.for_browser_job(row)
    assert q["blocked_by"] is None, q


@pytest.mark.asyncio
async def test_session_cap_counts_publish_sessions_too(use_db, monkeypatch):
    """发布不在 browser_jobs 留痕,但照样占会话额度(与派发层计数口径一致)。"""
    _caps(monkeypatch, system=2)
    ago = datetime.utcnow() - timedelta(minutes=5)
    async with use_db() as s:
        await _browser(s, status="done", updated=ago)
        await _publish(s, status="published", started=ago)
        mine = await _browser(s, operator_id=0)
        row = await _row(s, mine)

    q = await queue_status.for_browser_job(row)
    assert q["blocked_by"] == "session_cap"
    assert q["detail"]["used"] == 2


@pytest.mark.asyncio
async def test_sessions_outside_window_do_not_count(use_db, monkeypatch):
    """滚出 60 分钟窗口的会话不再占额度。"""
    _caps(monkeypatch, system=2)
    old = datetime.utcnow() - timedelta(minutes=61)
    async with use_db() as s:
        for _ in range(5):
            await _browser(s, status="done", updated=old)
        mine = await _browser(s, operator_id=0)
        row = await _row(s, mine)

    q = await queue_status.for_browser_job(row)
    assert q["blocked_by"] is None, q


# ---------------- window_resets_at ----------------


def test_window_resets_at_picks_kth_oldest_not_min():
    """要滚出的是第 ``used-cap+1`` 早的那次,不是"最早那次"。

    检验点特意取两个**答案不同**的值:恰好满帽(4/4)落在最早那次,超帽两格(6/4)落在
    第 3 早那次。只测满帽的话,把公式写成 min(events) 也能过 —— 那正是这条要防的错。
    """
    t0 = datetime(2026, 8, 7, 10, 0, 0)
    events = [t0 + timedelta(minutes=i * 10) for i in range(6)]  # 10:00,10:10,...,10:50

    just_full = queue_status._window_resets_at(events[:4], used=4, cap=4)
    assert just_full == t0 + timedelta(hours=1)  # 第 1 早(10:00)滚出即够 → 11:00

    over_by_two = queue_status._window_resets_at(events, used=6, cap=4)
    assert over_by_two == t0 + timedelta(minutes=20, hours=1)  # 第 3 早(10:20)→ 11:20
    assert over_by_two != just_full  # 两个检验点答案必须不同


def test_window_resets_at_none_when_only_inflight_can_free_budget():
    """在飞会话没有到期时刻:光等时间解不开时给 null,不编一个假时刻。"""
    t0 = datetime(2026, 8, 7, 10, 0, 0)
    # 4 次会话里 3 次在飞(None),帽 4 → 要滚出 1 次,有时刻的只有 1 条,够
    assert queue_status._window_resets_at([t0, None, None, None], used=4, cap=4) == (
        t0 + timedelta(hours=1)
    )
    # 帽 2 → 要滚出 3 次,有时刻的只有 1 条,解不开
    assert queue_status._window_resets_at([t0, None, None, None], used=4, cap=2) is None


@pytest.mark.asyncio
async def test_window_resets_at_end_to_end(use_db, monkeypatch):
    """端到端:满帽时 window_resets_at = 最早那次会话 + 60 分钟(带 UTC 偏移)。"""
    _caps(monkeypatch, system=2)
    oldest = datetime.utcnow() - timedelta(minutes=50)
    async with use_db() as s:
        await _browser(s, status="done", updated=oldest)
        await _browser(s, status="done", updated=datetime.utcnow() - timedelta(minutes=5))
        mine = await _browser(s, operator_id=0)
        row = await _row(s, mine)

    q = await queue_status.for_browser_job(row)
    expected = (oldest + timedelta(hours=1)).replace(microsecond=0).isoformat()[:16]
    assert q["detail"]["window_resets_at"].startswith(expected)
    assert q["detail"]["window_resets_at"].endswith("+00:00")


# ---------------- publish_jobs 一路 ----------------


@pytest.mark.asyncio
async def test_publish_job_queue_position(use_db, monkeypatch):
    """发布任务自己也有位次(pending 同样排队)。"""
    _caps(monkeypatch)
    base = datetime.utcnow() - timedelta(minutes=10)
    async with use_db() as s:
        await _browser(s, created=base)
        job = await _publish(s, created=base + timedelta(seconds=1))
        q = await queue_status.for_publish_job(job, s)

    assert q["position"] == 2 and q["ahead"] == 1
    assert q["account_queue_depth"] == 2


@pytest.mark.asyncio
async def test_publish_job_scheduled_future_not_in_queue(use_db, monkeypatch):
    """未到点的定时稿:位次 null + detail.not_before,不是"卡死"。"""
    _caps(monkeypatch)
    later = datetime.utcnow() + timedelta(days=1)
    async with use_db() as s:
        job = await _publish(s, schedule_time=later)
        q = await queue_status.for_publish_job(job, s)

    assert q["position"] is None
    assert q["blocked_by"] is None
    assert q["detail"]["not_before"].startswith(later.replace(microsecond=0).isoformat()[:16])
    assert q["account_queue_depth"] == 0


@pytest.mark.asyncio
async def test_publish_job_terminal_has_no_queue(use_db, monkeypatch):
    _caps(monkeypatch)
    async with use_db() as s:
        job = await _publish(s, status="published", started=datetime.utcnow())
        assert await queue_status.for_publish_job(job, s) is None


@pytest.mark.asyncio
async def test_publish_job_layer_follows_created_by(use_db, monkeypatch):
    """发布任务的分层看 created_by:运营建的吃运营帽,系统建的吃系统帽。"""
    _caps(monkeypatch, system=1, operator=12)
    ago = datetime.utcnow() - timedelta(minutes=5)
    async with use_db() as s:
        await _browser(s, status="done", updated=ago)
        sys_job = await _publish(s, created_by=None)
        op_job = await _publish(s, created_by=5)
        sys_q = await queue_status.for_publish_job(sys_job, s)
        op_q = await queue_status.for_publish_job(op_job, s)

    assert sys_q["blocked_by"] == "session_cap"
    assert sys_q["detail"]["kind_of_cap"] == "system"
    assert op_q["blocked_by"] is None


# ---------------- 与派发层口径一致(本特性最容易腐化的地方) ----------------
#
# 判据同源是靠结构保证的(queue_status 是唯一真源,worker 只调不复刻),但"同源"这件事
# 本身也要有测试兜住:万一以后有人在 worker 里手写回一份判据,下面两条会立刻红。


@pytest.mark.asyncio
async def test_used_equals_dispatcher_session_count(db_factory, monkeypatch):
    """读侧 detail.used 与派发层 ``_recent_session_counts`` 数的是同一批行。

    行集合刻意混齐所有边界:窗口内终态(done/error)、在飞 running、发布的 published /
    publishing、滚出窗口的旧行(不算)、queued 行(不算,数进去会自锁)。

    这里直接比两侧的取数(而不是比对外的 detail.used):该号有 running 行时对外报的是
    account_busy,detail 里不带 used,比不到 —— 而"在飞会话也占额度"恰恰是必须覆盖的
    边界,不能为了好断言把它从数据里拿掉。
    """
    monkeypatch.setattr(db_module, "async_session", db_factory)
    _caps(monkeypatch, system=1)
    now = datetime.utcnow()
    recent = now - timedelta(minutes=5)
    old = now - timedelta(minutes=90)
    async with db_factory() as s:
        await _browser(s, status="done", updated=recent)
        await _browser(s, status="error", updated=recent)
        await _browser(s, status="running")
        await _browser(s, status="done", updated=old)  # 滚出窗口,不算
        await _browser(s, status="queued")  # 排队中,不算
        await _publish(s, status="published", started=recent)
        await _publish(s, status="published", started=old)  # 滚出窗口,不算
        await _publish(s, status="publishing", started=recent)

    sup = Supervisor(db_factory, repo=None, session_cap=1, operator_session_cap=12)
    counts = await sup._recent_session_counts([1])
    async with db_factory() as s:
        events = await queue_status._session_events(
            s, 1, queue_status.session_window_cutoff(now)
        )

    assert len(events) == counts[1] == 5, (events, counts)
    assert events.count(None) == 2, "两条在飞会话(running + publishing)没有到期时刻"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "seeded,operator_id,expect_blocked",
    [
        (3, 0, False),   # 系统层还差一格 → 派
        (4, 0, True),    # 系统层满帽 → 拦
        (4, 7, False),   # 运营层更宽 → 同一份计数下照派
        (12, 7, True),   # 运营层也满帽 → 拦
    ],
)
async def test_blocked_by_matches_dispatcher_verdict(
    db_factory, monkeypatch, seeded, operator_id, expect_blocked
):
    """派发层拦不拦,与读侧报不报 session_cap,必须同进同出。

    左边跑真 Supervisor.scan_once 看有没有派出子进程,右边读 queue 段——两边对同一批
    库行给出的结论一致才算数。判据只此一份时这条恒绿;哪天有人另写一套,它先红。
    """
    from tests.test_worker_supervisor import _FakeRepo, _finish_all, _install_spawn_recorder

    monkeypatch.setattr(db_module, "async_session", db_factory)
    _caps(monkeypatch, system=4, operator=12)
    spawned = _install_spawn_recorder(monkeypatch)
    ago = datetime.utcnow() - timedelta(minutes=5)
    async with db_factory() as s:
        for _ in range(seeded):
            await _browser(s, status="done", updated=ago)
        mine = await _browser(s, operator_id=operator_id)
        row = await _row(s, mine)

    q = await queue_status.for_browser_job(row)
    assert (q["blocked_by"] == "session_cap") is expect_blocked, q

    repo = _FakeRepo(rows=[dict(row, payload={})])
    sup = Supervisor(db_factory, repo=repo, session_cap=4, operator_session_cap=12)
    await sup.scan_once()
    assert (spawned == []) is expect_blocked, spawned
    await _finish_all(sup, spawned)


@pytest.mark.asyncio
async def test_tz_aware_not_before_normalized(use_db, monkeypatch):
    """带时区偏移的 not_before 归一成 naive UTC 再比较,不炸 TypeError 也不误判到点。

    matrix_interact / note_comment_task 写的是 naive UTC,但 payload 是自由字段,
    带偏移的值迟早会出现;老实现把比较放在 try 里,aware 值一撞 TypeError 就被当成
    "立即可派"静默放行 —— 排期直接失效。
    """
    from datetime import timezone as _tz

    _caps(monkeypatch)
    future_utc = datetime.utcnow() + timedelta(hours=2)
    aware = future_utc.replace(tzinfo=_tz.utc).astimezone(
        _tz(timedelta(hours=8))
    )  # 同一时刻,写成 +08:00
    async with use_db() as s:
        mine = await _browser(s, payload={"not_before": aware.isoformat()})
        row = await _row(s, mine)

    q = await queue_status.for_browser_job(row)
    assert q["position"] is None, "两小时后才到点,不该已经进队列"
    assert q["detail"]["not_before"].startswith(
        future_utc.replace(microsecond=0).isoformat()[:16]
    )
