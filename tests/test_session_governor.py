"""同号浏览器会话频次总闸(app/worker.py Supervisor 派发层)。

背景(2026-08-07 生产实证):小红书风控红线是**同一账号一小时 ≤4-5 次浏览器会话**,
8 月初实测一小时 5 次就把两个号弹上验证墙。但系统里每个子系统只守自己的闸
(补量有日配额和篇间抖动、cookie 巡检有号间隔、发布有冷却与日上限),**没有任何东西
守跨 kind 的同号总会话频次**——当天实测过去一小时 9 个号全部超线,最高 10 次/时
(发布钩子扇出 + 补量 + 互动 + 台账同步 + 草稿清理叠加,每一路单看都合规)。

闸开在派发层(唯一能看见全部 kind 的位置),语义:

- **计数含全部会话**(不论谁触发):browser_jobs 终态行(窗口内)+ running 行,
  外加 publish_jobs(发布链自起 camoufox 且不在 browser_jobs 留痕);
- **拦不拦按触发方分两层帽值**:系统自发任务(operator_id 非正)超 ``session_cap``(4)
  不派,留队列等下轮重估;运营触发任务(operator_id>0)吃更宽的
  ``operator_session_cap``(12)——人工意图优先,但同样有帽:2026-08-07 实证 skill 拿
  运营 apikey 批量回读组件,一小时 192 条全豁免直通,单号 51 次会话/时(红线 10 倍),
  运营配额闸限并发不限速率,拦不住;
- 被闸住打**去重**日志(同号连续多轮只吵一次,5s 一轮的循环刷屏就没人看了)。

各业务模块自有的节流一行不动,这里是第二层防御。
"""

import uuid
from datetime import datetime, timedelta

from app.models.browser_job import BrowserJob
from app.models.publish_job import PublishJob
from app.worker import Supervisor
from tests.test_worker_supervisor import (
    _FakeRepo,
    _cmd_account,
    _cmd_opt,
    _finish_all,
    _install_spawn_recorder,
)


# ---------------- 灌数据 ----------------


async def _seed_browser_sessions(
    db_factory,
    account_id: int,
    n: int,
    *,
    status: str = "done",
    minutes_ago: float = 5,
    kind: str = "cookie_check",
) -> None:
    """灌 n 条 browser_jobs 会话行(updated_at = minutes_ago 分钟前)。"""
    ts = datetime.utcnow() - timedelta(minutes=minutes_ago)
    async with db_factory() as s:
        for _ in range(n):
            s.add(
                BrowserJob(
                    id=uuid.uuid4().hex,
                    kind=kind,
                    account_id=account_id,
                    operator_id=0,
                    payload="{}",
                    status=status,
                    created_at=ts,
                    updated_at=ts,
                )
            )
        await s.commit()


async def _seed_published(
    db_factory, account_id: int, n: int, *, minutes_ago: float = 5
) -> None:
    """灌 n 条已发布 publish_jobs(started_at = minutes_ago 分钟前 = 会话发生时刻)。"""
    ts = datetime.utcnow() - timedelta(minutes=minutes_ago)
    async with db_factory() as s:
        for i in range(n):
            s.add(
                PublishJob(
                    account_id=account_id,
                    title=f"p{i}",
                    content="c",
                    images_json="[]",
                    topics_json="[]",
                    status="published",
                    started_at=ts,
                )
            )
        await s.commit()


def _queued_row(job_id: str, account_id: int, operator_id: int) -> dict:
    """一条 queued 的 browser_jobs 候选行(假 repo 的 list_dispatchable 产物)。"""
    return {
        "id": job_id,
        "kind": "note_ledger_sync",
        "account_id": account_id,
        "operator_id": operator_id,
        "payload": {},
        "status": "queued",
        "created_at": datetime.utcnow() - timedelta(minutes=1),
    }


def _capture_warnings():
    """挂 loguru sink 收 WARNING 文本(宿主用 loguru,caplog 抓不到)。"""
    from loguru import logger as _lg

    buf: list[str] = []
    sink_id = _lg.add(lambda m: buf.append(str(m)), level="WARNING")
    return buf, lambda: _lg.remove(sink_id)


# ---------------- 闸行为 ----------------


async def test_system_job_blocked_when_over_cap(db_factory, monkeypatch):
    """近一小时已达帽值:系统自发任务(operator_id=0)本轮不派,任务留在队列里。"""
    spawned = _install_spawn_recorder(monkeypatch)
    await _seed_browser_sessions(db_factory, 1, 4)
    repo = _FakeRepo(rows=[_queued_row("sys1", 1, 0)])
    sup = Supervisor(db_factory, repo=repo, session_cap=4)

    await sup.scan_once()

    assert spawned == [], "同号近一小时已 4 次会话,系统任务必须延后"
    await _finish_all(sup, spawned)


async def test_operator_job_dispatched_over_system_cap(db_factory, monkeypatch):
    """运营触发任务超**系统**帽不拦:人工意图优先,只要还没到运营帽就照派。"""
    spawned = _install_spawn_recorder(monkeypatch)
    await _seed_browser_sessions(db_factory, 1, 4)
    repo = _FakeRepo(rows=[_queued_row("op1", 1, 7)])
    sup = Supervisor(db_factory, repo=repo, session_cap=4, operator_session_cap=12)

    await sup.scan_once()

    assert [_cmd_account(s["cmd"]) for s in spawned] == [1]
    assert _cmd_opt(spawned[0]["cmd"], "--browser-job-ids") == "op1"
    await _finish_all(sup, spawned)


async def test_operator_job_blocked_at_operator_cap(db_factory, monkeypatch):
    """运营任务达运营帽也延后:批量刷运营端点不再是无限直通的后门。

    2026-08-07 生产实证:skill 用运营 apikey 逐篇回读组件,一小时 192 条全豁免,
    单号最高 51 次会话——风控红线的 10 倍。运营配额闸限的是并发未终态数不限速率。
    """
    spawned = _install_spawn_recorder(monkeypatch)
    await _seed_browser_sessions(db_factory, 1, 12)
    repo = _FakeRepo(rows=[_queued_row("op1", 1, 7)])
    sup = Supervisor(db_factory, repo=repo, session_cap=4, operator_session_cap=12)

    await sup.scan_once()

    assert spawned == [], "同号近一小时已 12 次会话,运营任务也必须延后"
    await _finish_all(sup, spawned)


async def test_operator_job_dispatched_below_operator_cap(db_factory, monkeypatch):
    """差一格就放行:11 次 + 帽值 12,运营任务照派(闸是限速不是禁用)。"""
    spawned = _install_spawn_recorder(monkeypatch)
    await _seed_browser_sessions(db_factory, 1, 11)
    repo = _FakeRepo(rows=[_queued_row("op1", 1, 7)])
    sup = Supervisor(db_factory, repo=repo, session_cap=4, operator_session_cap=12)

    await sup.scan_once()

    assert [_cmd_account(s["cmd"]) for s in spawned] == [1]
    assert _cmd_opt(spawned[0]["cmd"], "--browser-job-ids") == "op1"
    await _finish_all(sup, spawned)


async def test_operator_batch_limited_by_remaining_budget(db_factory, monkeypatch):
    """运营侧同样按剩余额度切批:已 11 次、帽值 12 → 一批只放行 1 个。

    与系统侧同理——只在"计数≥帽值"时才拦挡不住批量:11 次时把一批 3 个全放进去
    就是 14 次/时,正是本次事故的打法(批量端点一轮灌若干条)。
    """
    spawned = _install_spawn_recorder(monkeypatch)
    await _seed_browser_sessions(db_factory, 1, 11)
    repo = _FakeRepo(rows=[_queued_row(f"op{i}", 1, 7) for i in range(3)])
    sup = Supervisor(
        db_factory, repo=repo, session_cap=4, operator_session_cap=12, batch_per_account=3
    )

    await sup.scan_once()

    assert _cmd_opt(spawned[0]["cmd"], "--browser-job-ids") == "op0"
    await _finish_all(sup, spawned)


async def test_operator_cap_zero_disables_operator_governor(db_factory, monkeypatch):
    """运营帽 ≤0 = 只关运营那层(逃生口):超线也照派,系统那层不受影响。"""
    spawned = _install_spawn_recorder(monkeypatch)
    await _seed_browser_sessions(db_factory, 1, 50)
    repo = _FakeRepo(rows=[_queued_row("op1", 1, 7)])
    sup = Supervisor(db_factory, repo=repo, session_cap=4, operator_session_cap=0)

    await sup.scan_once()

    assert [_cmd_account(s["cmd"]) for s in spawned] == [1]
    assert _cmd_opt(spawned[0]["cmd"], "--browser-job-ids") == "op1"
    await _finish_all(sup, spawned)


async def test_system_cap_still_stricter_than_operator_cap(db_factory, monkeypatch):
    """双层各守各的:同一轮里 5 次会话下,运营任务(帽 12)派、系统任务(帽 4)不派。"""
    spawned = _install_spawn_recorder(monkeypatch)
    await _seed_browser_sessions(db_factory, 1, 5)
    repo = _FakeRepo(rows=[_queued_row("op1", 1, 7), _queued_row("sys1", 1, 0)])
    sup = Supervisor(
        db_factory, repo=repo, session_cap=4, operator_session_cap=12, batch_per_account=3
    )

    await sup.scan_once()

    assert _cmd_opt(spawned[0]["cmd"], "--browser-job-ids") == "op1"
    await _finish_all(sup, spawned)


async def test_window_rolls_off_restores_dispatch(db_factory, monkeypatch):
    """窗口滚动:同样 4 次会话但都在 90 分钟前(窗口外),系统任务恢复派发。"""
    spawned = _install_spawn_recorder(monkeypatch)
    await _seed_browser_sessions(db_factory, 1, 4, minutes_ago=90)
    repo = _FakeRepo(rows=[_queued_row("sys1", 1, 0)])
    sup = Supervisor(db_factory, repo=repo, session_cap=4)

    await sup.scan_once()

    assert [_cmd_account(s["cmd"]) for s in spawned] == [1]
    await _finish_all(sup, spawned)


async def test_running_sessions_counted(db_factory, monkeypatch):
    """在飞会话(running)照数:3 条终态 + 1 条 running = 4,达帽即拦。

    只数终态会漏掉正在跑的那次——恰恰是最该数的一次。
    """
    spawned = _install_spawn_recorder(monkeypatch)
    await _seed_browser_sessions(db_factory, 1, 3)
    # running 行的 updated_at 故意放到窗口外:在飞与否只看状态,不看时间
    await _seed_browser_sessions(db_factory, 1, 1, status="running", minutes_ago=200)
    repo = _FakeRepo(rows=[_queued_row("sys1", 1, 0)])
    sup = Supervisor(db_factory, repo=repo, session_cap=4)

    await sup.scan_once()

    assert spawned == [], "running 会话必须计入,否则在飞那次被漏数"
    await _finish_all(sup, spawned)


async def test_queued_sessions_not_counted(db_factory, monkeypatch):
    """queued 行不算会话:排着队还没起浏览器,数进去会自我锁死。"""
    spawned = _install_spawn_recorder(monkeypatch)
    await _seed_browser_sessions(db_factory, 1, 5, status="queued")
    repo = _FakeRepo(rows=[_queued_row("sys1", 1, 0)])
    sup = Supervisor(db_factory, repo=repo, session_cap=4)

    await sup.scan_once()

    assert [_cmd_account(s["cmd"]) for s in spawned] == [1]
    await _finish_all(sup, spawned)


async def test_publish_sessions_counted(db_factory, monkeypatch):
    """发布也是一次浏览器会话:publish_jobs 单独计入(它不在 browser_jobs 留痕)。"""
    spawned = _install_spawn_recorder(monkeypatch)
    await _seed_published(db_factory, 1, 4)
    repo = _FakeRepo(rows=[_queued_row("sys1", 1, 0)])
    sup = Supervisor(db_factory, repo=repo, session_cap=4)

    await sup.scan_once()

    assert spawned == [], "近一小时发了 4 篇 = 4 次会话,系统任务必须延后"
    await _finish_all(sup, spawned)


async def test_batch_limited_by_remaining_budget(db_factory, monkeypatch):
    """本轮派发量受剩余额度约束:已 3 次、帽值 4 → 一批只放行 1 个系统任务。

    只在"计数≥帽值"时才拦是不够的:批量派发一轮就能把额度冲穿(3 次时放 3 个进去
    就是 6 次/时)。额度按剩余量切批,才真的是一小时 ≤ 帽值。
    """
    spawned = _install_spawn_recorder(monkeypatch)
    await _seed_browser_sessions(db_factory, 1, 3)
    repo = _FakeRepo(
        rows=[_queued_row(f"sys{i}", 1, 0) for i in range(3)]
    )
    sup = Supervisor(db_factory, repo=repo, session_cap=4, batch_per_account=3)

    await sup.scan_once()

    assert _cmd_opt(spawned[0]["cmd"], "--browser-job-ids") == "sys0"
    await _finish_all(sup, spawned)


async def test_cap_zero_disables_governor(db_factory, monkeypatch):
    """帽值 ≤0 = 关闸(运维逃生口):超线也照派,与其它 0=关闭 的开关同款语义。"""
    spawned = _install_spawn_recorder(monkeypatch)
    await _seed_browser_sessions(db_factory, 1, 20)
    repo = _FakeRepo(rows=[_queued_row("sys1", 1, 0)])
    sup = Supervisor(db_factory, repo=repo, session_cap=0)

    await sup.scan_once()

    assert [_cmd_account(s["cmd"]) for s in spawned] == [1]
    await _finish_all(sup, spawned)


async def test_other_account_not_affected(db_factory, monkeypatch):
    """闸是按号算的:1 号超线不影响 2 号照派。"""
    spawned = _install_spawn_recorder(monkeypatch)
    await _seed_browser_sessions(db_factory, 1, 4)
    repo = _FakeRepo(rows=[_queued_row("sys1", 1, 0), _queued_row("sys2", 2, 0)])
    sup = Supervisor(db_factory, repo=repo, session_cap=4)

    await sup.scan_once()

    assert [_cmd_account(s["cmd"]) for s in spawned] == [2]
    await _finish_all(sup, spawned)


# ---------------- 日志去重 ----------------


async def test_block_log_deduped_per_account(db_factory, monkeypatch):
    """同号连续多轮被闸只吵一次;恢复后再超线才会再吵(5s 一轮刷屏没人看)。"""
    spawned = _install_spawn_recorder(monkeypatch)
    await _seed_browser_sessions(db_factory, 1, 4)
    repo = _FakeRepo(rows=[_queued_row("sys1", 1, 0)])
    sup = Supervisor(db_factory, repo=repo, session_cap=4)

    buf, done = _capture_warnings()
    try:
        await sup.scan_once()
        await sup.scan_once()
        await sup.scan_once()
    finally:
        done()

    hits = [line for line in buf if "会话总闸" in line]
    assert len(hits) == 1, f"同号被闸日志未去重: {hits}"
    assert "账号 1" in hits[0] and "系统任务延后" in hits[0]
    await _finish_all(sup, spawned)


async def test_operator_block_log_distinct_and_deduped(db_factory, monkeypatch):
    """运营被闸的日志与系统闸文案区分(得能一眼看出是谁该改打法),同样按号去重。"""
    spawned = _install_spawn_recorder(monkeypatch)
    await _seed_browser_sessions(db_factory, 1, 12)
    repo = _FakeRepo(rows=[_queued_row("op1", 1, 7)])
    sup = Supervisor(db_factory, repo=repo, session_cap=4, operator_session_cap=12)

    buf, done = _capture_warnings()
    try:
        await sup.scan_once()
        await sup.scan_once()
    finally:
        done()

    hits = [line for line in buf if "运营任务也已延后" in line]
    assert len(hits) == 1, f"运营被闸日志未去重: {hits}"
    assert "账号 1" in hits[0] and "12" in hits[0]
    assert "系统任务延后" not in hits[0], "运营闸不能复用系统闸文案"
    await _finish_all(sup, spawned)


async def test_both_layers_blocked_logs_separately(db_factory, monkeypatch):
    """同轮里系统与运营都被闸:两条独立日志,各自按号去重,互不吞没。"""
    spawned = _install_spawn_recorder(monkeypatch)
    await _seed_browser_sessions(db_factory, 1, 12)
    repo = _FakeRepo(rows=[_queued_row("op1", 1, 7), _queued_row("sys1", 1, 0)])
    sup = Supervisor(
        db_factory, repo=repo, session_cap=4, operator_session_cap=12, batch_per_account=3
    )

    buf, done = _capture_warnings()
    try:
        await sup.scan_once()
    finally:
        done()

    assert spawned == [], "两层都超帽,本轮不该派任何子进程"
    assert len([x for x in buf if "系统任务延后" in x]) == 1
    assert len([x for x in buf if "运营任务也已延后" in x]) == 1
    await _finish_all(sup, spawned)
