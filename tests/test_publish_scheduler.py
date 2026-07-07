"""发布调度器 + 队列的纯 DB 状态机单测(不起浏览器)。

复用 conftest 的 db_factory fixture(每测试独立临时 sqlite 会话工厂)。核心断言:
- 状态机成功:pending → mark_publishing 占到(True)→ finish(success) → published + note 回填。
- 重试后失败:finish(fail) 连续 retry_delays 长度次 → retries 递增 / next_retry_at 排期 / 回
  pending;再一次 → failed + error。
- recover_stale:publishing 且 started_at 超时 → 复位 pending;未超时不动。
- 双重占用去重:同一 pending job 两次 mark_publishing 只一次返回 True。
- scan_once 选择:未到期 schedule_time / next_retry_at / 非 pending 不选;到期与空值选中。
- 队列 + 锁最小契约:AccountLocks 同号同锁;PublishQueue submit→worker→runner。
- 真实 runner 全流程:mark_publishing → per-account 锁 → to_thread(publish_once) → finish
  (monkeypatch publish_once,不起浏览器)。
- runner 兜底:publish_once 抛异常 → 占用后统一 finish(fail),job 不卡 publishing,
  按状态机排重试(pending + retries 递增 + error)或耗尽转 failed。
- 调度循环:start 每 poll 周期先 recover_stale 再 scan→submit,两条 job 均落 published;可 stop。
- 周期 recover:recover_stale 被每个 poll 周期调用(非仅启动一次)。
"""

import asyncio
import json
from datetime import datetime, timedelta

from app.browser.sync_client import PublishResult
from app.core.config import settings
from app.models.publish_job import PublishJob
from app.models.xhs_account import XhsAccount
from app.publish import scheduler as scheduler_mod
from app.publish.queue import AccountLocks, PublishQueue
from app.publish.scheduler import PublishScheduler, make_publish_runner


# ---------------- 建数据辅助 ----------------


async def _make_account(session_factory, name: str = "acc") -> int:
    """建一个账号,返回 id(满足 PublishJob.account_id 外键语义)。"""
    async with session_factory() as session:
        acc = XhsAccount(name=name)
        session.add(acc)
        await session.commit()
        return acc.id


async def _make_job(session_factory, account_id: int, **overrides) -> int:
    """建一条 PublishJob,返回 id;overrides 覆盖 status / schedule_time 等默认。"""
    defaults = dict(
        account_id=account_id,
        title="标题",
        content="正文",
        images_json="[]",
        topics_json="[]",
    )
    defaults.update(overrides)
    async with session_factory() as session:
        job = PublishJob(**defaults)
        session.add(job)
        await session.commit()
        return job.id


async def _get_job(session_factory, job_id: int) -> PublishJob:
    """回读一条 PublishJob 当前状态。"""
    async with session_factory() as session:
        return await session.get(PublishJob, job_id)


# ---------------- 状态机:成功 ----------------


async def test_state_machine_success(db_factory):
    """pending → mark_publishing 占到 → finish(success) → published + note 回填。"""
    account_id = await _make_account(db_factory)
    job_id = await _make_job(db_factory, account_id)
    scheduler = PublishScheduler(db_factory)

    assert await scheduler.mark_publishing(job_id) is True
    job = await _get_job(db_factory, job_id)
    assert job.status == "publishing"
    assert job.started_at is not None

    await scheduler.finish(
        job_id,
        PublishResult(success=True, note_id="abc123", note_url="https://xhs/9"),
    )
    job = await _get_job(db_factory, job_id)
    assert job.status == "published"
    assert job.note_id == "abc123"
    assert job.note_url == "https://xhs/9"
    assert job.error is None


# ---------------- 状态机:重试后失败 ----------------


async def test_retry_then_fail(db_factory):
    """连续 finish(fail):retry_delays 长度次内排期回 pending,再一次转 failed。"""
    account_id = await _make_account(db_factory)
    job_id = await _make_job(db_factory, account_id)
    scheduler = PublishScheduler(db_factory)
    delays = settings.retry_delays

    for i in range(len(delays)):
        before = datetime.utcnow()
        await scheduler.finish(job_id, PublishResult(success=False, error=f"boom{i}"))
        job = await _get_job(db_factory, job_id)
        assert job.status == "pending", f"第 {i} 次失败应回 pending"
        assert job.retries == i + 1
        assert job.error == f"boom{i}"
        assert job.started_at is None
        # next_retry_at 排到未来,且间隔约为该次的 retry_delays[i]
        assert job.next_retry_at is not None
        assert job.next_retry_at >= before + timedelta(seconds=delays[i] - 1)

    # 重试额度耗尽:再一次失败 → failed(终态)
    await scheduler.finish(job_id, PublishResult(success=False, error="final"))
    job = await _get_job(db_factory, job_id)
    assert job.status == "failed"
    assert job.error == "final"
    assert job.retries == len(delays)


# ---------------- recover_stale ----------------


async def test_recover_stale(db_factory):
    """publishing 且 started_at 超时 → 复位 pending;未超时的不动。"""
    account_id = await _make_account(db_factory)
    stale_started = datetime.utcnow() - timedelta(
        seconds=settings.PUBLISH_JOB_TIMEOUT + 60
    )
    fresh_started = datetime.utcnow()
    stale_id = await _make_job(
        db_factory, account_id, status="publishing", started_at=stale_started
    )
    fresh_id = await _make_job(
        db_factory, account_id, status="publishing", started_at=fresh_started
    )

    scheduler = PublishScheduler(db_factory)
    recovered = await scheduler.recover_stale()
    assert recovered == 1

    stale = await _get_job(db_factory, stale_id)
    assert stale.status == "pending"
    assert stale.started_at is None

    fresh = await _get_job(db_factory, fresh_id)
    assert fresh.status == "publishing"
    assert fresh.started_at is not None


# ---------------- 双重占用去重 ----------------


async def test_double_submit_dedup(db_factory):
    """同一 pending job 两次 mark_publishing:只一次返回 True。"""
    account_id = await _make_account(db_factory)
    job_id = await _make_job(db_factory, account_id)
    scheduler = PublishScheduler(db_factory)

    first = await scheduler.mark_publishing(job_id)
    second = await scheduler.mark_publishing(job_id)
    assert first is True
    assert second is False


# ---------------- scan_once 选择 ----------------


async def test_scan_once_selects_due_jobs(db_factory):
    """未到期 schedule_time / next_retry_at / 非 pending 不选;到期与空值选中。"""
    account_id = await _make_account(db_factory)
    now = datetime.utcnow()
    past = now - timedelta(hours=1)
    future = now + timedelta(hours=1)

    due_plain = await _make_job(db_factory, account_id)  # 两时间字段都空 → 立即可发
    due_scheduled = await _make_job(db_factory, account_id, schedule_time=past)
    due_retry = await _make_job(db_factory, account_id, next_retry_at=past)
    not_due_schedule = await _make_job(db_factory, account_id, schedule_time=future)
    not_due_retry = await _make_job(db_factory, account_id, next_retry_at=future)
    not_pending = await _make_job(db_factory, account_id, status="publishing")

    scheduler = PublishScheduler(db_factory)
    ids = await scheduler.scan_once()

    assert due_plain in ids
    assert due_scheduled in ids
    assert due_retry in ids
    assert not_due_schedule not in ids
    assert not_due_retry not in ids
    assert not_pending not in ids


# ---------------- 队列 + 锁最小契约 ----------------


async def test_account_locks_same_account_same_lock():
    """同一 account_id 返回同一把锁;不同 account_id 是不同锁。"""
    locks = AccountLocks()
    assert locks.get(1) is locks.get(1)
    assert locks.get(1) is not locks.get(2)


async def test_queue_submit_runs_runner():
    """submit 的 job_id 被 worker 取出并交给注入的 runner 处理。"""
    seen: list[int] = []

    async def fake_runner(job_id: int) -> None:
        seen.append(job_id)

    queue = PublishQueue(concurrency=2)
    queue.start(fake_runner)
    queue.submit(11)
    queue.submit(22)

    for _ in range(100):
        if len(seen) == 2:
            break
        await asyncio.sleep(0.01)
    await queue.stop()

    assert sorted(seen) == [11, 22]


# ---------------- 真实 runner 全流程(monkeypatch 不起浏览器)----------------


async def test_publish_runner_full_flow(db_factory, monkeypatch):
    """真实 runner:载参 → mark_publishing → 物料化 → 锁 → to_thread(publish_once) → finish=published。"""
    from pathlib import Path

    account_id = await _make_account(db_factory)
    job_id = await _make_job(
        db_factory,
        account_id,
        images_json=json.dumps(["https://cdn/a.png"]),
        topics_json=json.dumps(["#心理"]),
    )

    captured = {}

    # 物料化打桩:URL → 本地路径(不触真下载),断言 runner 用物料化后的本地路径调 publish_once
    def fake_materialize(images, workdir):
        captured["materialize"] = (list(images), str(workdir))
        return [Path("/local/a.png")]

    monkeypatch.setattr(scheduler_mod, "materialize_images", fake_materialize)

    def fake_publish_once(acc_id, cookies, title, content, image_paths, topics):
        captured["args"] = (acc_id, cookies, title, content, image_paths, topics)
        return PublishResult(success=True, note_id="nid", note_url="https://xhs/1")

    monkeypatch.setattr(scheduler_mod.sync_client, "publish_once", fake_publish_once)

    scheduler = PublishScheduler(db_factory)
    runner = make_publish_runner(db_factory, scheduler, AccountLocks())
    await runner(job_id)

    job = await _get_job(db_factory, job_id)
    assert job.status == "published"
    assert job.note_id == "nid"
    assert job.note_url == "https://xhs/1"
    # 物料化收到原始 URL 列表
    assert captured["materialize"][0] == ["https://cdn/a.png"]
    # 发布参数由 job 正确拆出(account 无 cookie → 空列表),image_paths 是物料化后的本地路径
    acc_id, cookies, title, content, image_paths, topics = captured["args"]
    assert acc_id == account_id
    assert cookies == []
    assert image_paths == ["/local/a.png"]
    assert topics == ["#心理"]


async def test_publish_runner_skips_when_not_pending(db_factory, monkeypatch):
    """非 pending(已被占用)的 job:mark_publishing 占不到 → 不触发 publish_once。"""
    account_id = await _make_account(db_factory)
    job_id = await _make_job(db_factory, account_id, status="publishing")

    called = {"n": 0}

    def fake_publish_once(*args, **kwargs):
        called["n"] += 1
        return PublishResult(success=True)

    monkeypatch.setattr(scheduler_mod.sync_client, "publish_once", fake_publish_once)

    scheduler = PublishScheduler(db_factory)
    runner = make_publish_runner(db_factory, scheduler, AccountLocks())
    await runner(job_id)

    assert called["n"] == 0


async def test_publish_runner_exception_does_not_stick(db_factory, monkeypatch):
    """publish_once 抛异常:runner 兜底 finish,job 不卡 publishing,按状态机排重试(pending)。"""
    account_id = await _make_account(db_factory)
    job_id = await _make_job(db_factory, account_id)

    def boom_publish_once(*args, **kwargs):
        raise RuntimeError("浏览器炸了")

    monkeypatch.setattr(scheduler_mod.sync_client, "publish_once", boom_publish_once)

    scheduler = PublishScheduler(db_factory)
    runner = make_publish_runner(db_factory, scheduler, AccountLocks())
    await runner(job_id)

    job = await _get_job(db_factory, job_id)
    # 不卡 publishing:占用后 publish_once 抛异常被兜底 finish(fail)→ 有重试额度回 pending
    assert job.status == "pending"
    assert job.retries == 1  # 重试计数递增
    assert job.next_retry_at is not None  # 排了下次重试
    assert job.started_at is None
    assert job.error is not None and "浏览器炸了" in job.error  # error 落库


async def test_publish_runner_exception_exhausts_to_failed(db_factory, monkeypatch):
    """publish_once 反复抛异常:重试耗尽后终态 failed,而非永久 publishing。"""
    account_id = await _make_account(db_factory)
    # 预置 retries=len(delays):再失败一次即耗尽转 failed
    delays = settings.retry_delays
    job_id = await _make_job(db_factory, account_id, retries=len(delays))

    def boom_publish_once(*args, **kwargs):
        raise RuntimeError("又炸了")

    monkeypatch.setattr(scheduler_mod.sync_client, "publish_once", boom_publish_once)

    scheduler = PublishScheduler(db_factory)
    runner = make_publish_runner(db_factory, scheduler, AccountLocks())
    await runner(job_id)

    job = await _get_job(db_factory, job_id)
    assert job.status == "failed"
    assert job.started_at is None
    assert job.error is not None and "又炸了" in job.error


# ---------------- 调度循环:恢复 + 扫表 + 发布 ----------------


async def test_scheduler_loop_recovers_and_publishes(db_factory, monkeypatch):
    """start:先 recover_stale(僵死 publishing 回 pending)再周期 scan→submit,两条均 published。"""
    account_id = await _make_account(db_factory)
    stale_id = await _make_job(
        db_factory,
        account_id,
        status="publishing",
        started_at=datetime.utcnow() - timedelta(seconds=settings.PUBLISH_JOB_TIMEOUT + 60),
    )
    pending_id = await _make_job(db_factory, account_id)

    def fake_publish_once(*args, **kwargs):
        return PublishResult(success=True, note_url="https://xhs/ok")

    monkeypatch.setattr(scheduler_mod.sync_client, "publish_once", fake_publish_once)

    scheduler = PublishScheduler(db_factory, poll_interval=0.02)
    scheduler.start()
    try:
        for _ in range(300):
            s = await _get_job(db_factory, stale_id)
            p = await _get_job(db_factory, pending_id)
            if s.status == "published" and p.status == "published":
                break
            await asyncio.sleep(0.01)
    finally:
        await scheduler.stop()

    assert (await _get_job(db_factory, stale_id)).status == "published"
    assert (await _get_job(db_factory, pending_id)).status == "published"


async def test_scheduler_loop_recovers_every_cycle(db_factory, monkeypatch):
    """周期 recover:recover_stale 被每个 poll 周期调用(而非仅启动一次)。"""
    scheduler = PublishScheduler(db_factory, poll_interval=0.02)

    calls = {"n": 0}
    orig_recover = scheduler.recover_stale

    async def counting_recover():
        calls["n"] += 1
        return await orig_recover()

    monkeypatch.setattr(scheduler, "recover_stale", counting_recover)

    scheduler.start()
    try:
        for _ in range(300):
            if calls["n"] >= 3:
                break
            await asyncio.sleep(0.01)
    finally:
        await scheduler.stop()

    # 仅启动一次调用则恒为 1;>=3 证明每轮 poll 都在跑 recover_stale
    assert calls["n"] >= 3, "recover_stale 应被每个 poll 周期调用,而非仅启动一次"
