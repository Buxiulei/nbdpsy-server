"""cookie 检测服务(app.services.cookie_check)单测,不起真浏览器。

P1 台账落库后重写:旧版测的内存 _registry/_tasks/驱逐机制已整体移除(台账进
browser_jobs 表,持久化语义在 tests/test_browser_jobs_repo.py 锁定;REST 全链在
tests/test_cookie_checks_rest.py 锁定)。本文件保留并适配原三关注点中仍然成立的两个:

- 共享锁:检测执行(execute)与发布调度对同号用**同一把** per-account 锁
  (进程级单例)——断言同锁对象 + 同号检测在发布持锁时串行等待。
- 执行崩溃终态:进程内消费管线(_run_inline)在 execute 抛异常时把台账落成
  error 终态,不让轮询方死等。

新增第三个关注点(转正引导链):cookie 从非 valid 转 valid 时登记「台账同步 +
newcomer 补量」各一条,让新号自动融进矩阵互动体系;在途去重、登记失败绝不上抛。
"""

import asyncio
import json

from sqlalchemy import select

import app.core.db as db_module
from app.browser.account_locks import account_locks
from app.models.browser_job import BrowserJob
from app.models.xhs_account import XhsAccount
from app.publish.scheduler import PublishScheduler
from app.services import browser_jobs_repo, cookie_check


# 用不易与其它测试撞车的高位 account_id,避免共享单例里跨 event loop 复用同一把锁。
_ACC_IDENTITY = 90200
_ACC_SERIALIZE = 90201


async def _clean_locks():
    """清空共享锁单例,隔离各测试(跨测试/跨 event loop 会泄漏锁对象)。"""
    account_locks._locks.clear()


# ---------------- 共享 per-account 锁 ----------------


async def test_check_and_publish_share_same_lock_object(db_factory):
    """检测与发布调度器对同号 get 到**同一个** Lock 对象(共享进程级单例)。"""
    await _clean_locks()
    # 生产装配用共享单例,此处复刻该 wiring(PublishScheduler 类保留作语义参照)
    scheduler = PublishScheduler(db_factory, account_locks=account_locks)

    # cookie_check.execute 直接引用同一个模块级单例
    assert cookie_check.account_locks is account_locks
    assert scheduler._account_locks.get(_ACC_IDENTITY) is account_locks.get(_ACC_IDENTITY)
    assert account_locks.get(_ACC_IDENTITY) is not account_locks.get(_ACC_IDENTITY + 1)


async def test_execute_serializes_behind_publish_held_lock(monkeypatch):
    """发布侧持有该号锁时,同号 execute 在同一把锁上等待,不并发进入浏览器检测。"""
    await _clean_locks()
    entered = {"v": False}

    def fake_check_login_once(account_id, cookies, probe_user_id=None):
        entered["v"] = True
        # 返回 error 结果 → execute 走"不写回账号"分支,测试不碰生产库
        return {"status": "error", "user_info": None, "reason": "stub"}

    monkeypatch.setattr(
        cookie_check.sync_client, "check_login_once", fake_check_login_once
    )

    async def fake_load(account_id):
        return []  # 不读库解密,隔离测试

    monkeypatch.setattr(cookie_check, "load_account_cookies", fake_load)

    lock = account_locks.get(_ACC_SERIALIZE)  # 发布侧会用的同一把锁
    await lock.acquire()  # 模拟"发布正在进行,持有该号锁"
    task = asyncio.create_task(cookie_check.execute(_ACC_SERIALIZE, {}))
    try:
        await asyncio.sleep(0.05)
        # 被同一把锁挡住,未进入 check_login_once
        assert entered["v"] is False
    finally:
        lock.release()

    result = await task  # 释放后拿到锁继续,进入检测并返回结果
    assert entered["v"] is True
    assert result["status"] == "error" and result["reason"] == "stub"
    await _clean_locks()


# ---------------- 进程内消费管线的崩溃终态 ----------------


async def test_inline_pipeline_exception_lands_error(monkeypatch):
    """execute 抛异常时,_run_inline 兜底把台账落 error 终态(不卡 running)。"""
    finished = {}

    async def fake_claim(job_id, worker_tag):
        return {"id": job_id, "status": "running"}

    async def fake_finish(job_id, status, result, worker_tag=None):
        finished["args"] = (job_id, status, result)

    monkeypatch.setattr(browser_jobs_repo, "claim_job", fake_claim)
    monkeypatch.setattr(browser_jobs_repo, "finish_job", fake_finish)

    async def boom():
        raise RuntimeError("检测炸了")

    await browser_jobs_repo._run_inline("exc-check", boom)

    job_id, status, result = finished["args"]
    assert job_id == "exc-check"
    assert status == "error"  # 不卡 running
    assert "检测炸了" in result["error"]


# ---------------- 浏览器段看门狗(检测卡死强杀) ----------------
#
# 运营实测:--check-cookie 偶发连续 checking >90s 不返回。根因是 check_login_once
# 单个 playwright 操作各有 30s 超时,但整段没有墙钟上限(launch 卡死 / driver 僵死 /
# 多阶段累积都能无限拖)。修法不能裸 asyncio.wait_for:超时只取消 await,同步线程还
# 抱着浏览器活着,锁一放、下一个任务的 kill_orphans 会与它互杀会话。正确顺序:
# 先强杀该 profile 的浏览器(让阻塞的 sync 调用抛错、线程尽快返回)→ 限时 rejoin →
# 才把锁还回去;超时结果一律判 error(不采信被强杀线程的迟到结果,不写回账号)。


async def test_watchdog_fast_path_passes_result_through(monkeypatch):
    """未超时:结果原样透传,绝不触发强杀。"""
    killed = {"v": False}

    def fake_check(account_id, cookies, probe_user_id=None):
        return {"status": "valid", "user_info": {"nickname": "n"}}

    monkeypatch.setattr(cookie_check.sync_client, "check_login_once", fake_check)
    monkeypatch.setattr(
        cookie_check, "_kill_profile_browser",
        lambda account_id: killed.__setitem__("v", True),
    )

    result = await cookie_check._run_check_with_watchdog(90301, [], None)

    assert result["status"] == "valid"
    assert killed["v"] is False


async def test_watchdog_timeout_kills_then_rejoins_and_reports_error(monkeypatch):
    """超时:先杀浏览器解锁线程 → rejoin 成功 → 返回 error(超时),不采信迟到结果。"""
    import threading

    unblock = threading.Event()
    order: list[str] = []

    def fake_check(account_id, cookies, probe_user_id=None):
        order.append("enter")
        unblock.wait(timeout=5)  # 模拟僵死;被"杀浏览器"唤醒
        order.append("return")
        return {"status": "valid", "user_info": {"nickname": "迟到的假成功"}}

    def fake_kill(account_id):
        order.append("kill")
        unblock.set()  # 模拟强杀让阻塞的 sync 调用返回

    monkeypatch.setattr(cookie_check.sync_client, "check_login_once", fake_check)
    monkeypatch.setattr(cookie_check, "_kill_profile_browser", fake_kill)
    monkeypatch.setattr(cookie_check, "_WATCHDOG_S", 0.2)
    monkeypatch.setattr(cookie_check, "_REJOIN_GRACE_S", 2.0)

    result = await cookie_check._run_check_with_watchdog(90302, [], None)

    # 顺序:进入 → 超时触发杀 → 线程归队;结果判 error 且不采信迟到的 valid
    assert order == ["enter", "kill", "return"]
    assert result["status"] == "error"
    assert "超时" in result["reason"]


async def test_watchdog_rejoin_timeout_still_returns_error(monkeypatch):
    """强杀后线程仍不归队(极端僵死):限时放弃等待,照样返回 error 不死等。"""
    import threading

    never = threading.Event()

    def fake_check(account_id, cookies, probe_user_id=None):
        never.wait(timeout=5)  # 杀了也不返回(封顶 5s 免得拖住解释器退出)
        return {"status": "valid", "user_info": None}

    monkeypatch.setattr(cookie_check.sync_client, "check_login_once", fake_check)
    monkeypatch.setattr(cookie_check, "_kill_profile_browser", lambda account_id: None)
    monkeypatch.setattr(cookie_check, "_WATCHDOG_S", 0.2)
    monkeypatch.setattr(cookie_check, "_REJOIN_GRACE_S", 0.2)

    result = await cookie_check._run_check_with_watchdog(90303, [], None)

    assert result["status"] == "error"
    assert "超时" in result["reason"]


async def test_execute_timeout_does_not_write_back_account(monkeypatch):
    """execute 全链:看门狗超时 → status=error → 不写回账号(保留原 cookie_status)。"""
    wrote = {"v": False}

    async def fake_load(account_id):
        return []

    async def fake_probe(session_factory, account_id):
        return None

    async def fake_write_back(account_id, status, user_info):
        wrote["v"] = True

    async def fake_record_wall(session_factory, account_id, wall, source):
        return None

    def fake_check(account_id, cookies, probe_user_id=None):
        import time
        time.sleep(0.5)  # 超过看门狗但有限,测试不泄漏线程
        return {"status": "valid", "user_info": None}

    monkeypatch.setattr(cookie_check, "load_account_cookies", fake_load)
    monkeypatch.setattr(cookie_check, "pick_probe_user_id", fake_probe)
    monkeypatch.setattr(cookie_check, "_write_back", fake_write_back)
    monkeypatch.setattr(cookie_check.risk_events, "record_wall", fake_record_wall)
    monkeypatch.setattr(cookie_check.sync_client, "check_login_once", fake_check)
    monkeypatch.setattr(cookie_check, "_kill_profile_browser", lambda account_id: None)
    monkeypatch.setattr(cookie_check, "_WATCHDOG_S", 0.1)
    monkeypatch.setattr(cookie_check, "_REJOIN_GRACE_S", 2.0)

    result = await cookie_check.execute(90304, {})

    assert result["status"] == "error"
    assert wrote["v"] is False
    await _clean_locks()


# ---------------- 转正引导链(非 valid → valid) ----------------
#
# 缺口:新号插件推 cookie 落库时 cookie_status='unknown',检测转正后没有任何东西触发
# 「台账同步」与「newcomer 补量」——他既不会被别的号互动(台账里没有他的笔记),也不会
# 去互动别人。账号 10/11 加入后 published_notes 一行都没有正是这个空档。


async def _jobs_of(factory, kind: str) -> list[BrowserJob]:
    """取某 kind 的全部台账行(按 id 稳定排序)。"""
    async with factory() as s:
        rows = await s.execute(
            select(BrowserJob).where(BrowserJob.kind == kind).order_by(BrowserJob.id)
        )
        return list(rows.scalars().all())


async def _add_job(factory, kind: str, account_id: int, status: str) -> None:
    """造一条指定状态的台账行(去重断言用)。"""
    import uuid

    async with factory() as s:
        s.add(BrowserJob(
            id=uuid.uuid4().hex, kind=kind, account_id=account_id,
            operator_id=0, payload="{}", status=status,
        ))
        await s.commit()


async def test_kick_onboarding_chain_enqueues_both(db_factory):
    """登记两条 queued:note_ledger_sync + newcomer 补量,operator_id=0。"""
    job_ids = await cookie_check.kick_onboarding_chain(db_factory, 77)

    assert len(job_ids) == 2
    sync_jobs = await _jobs_of(db_factory, "note_ledger_sync")
    assert len(sync_jobs) == 1
    assert (sync_jobs[0].account_id, sync_jobs[0].operator_id) == (77, 0)
    assert sync_jobs[0].status == "queued"
    assert json.loads(sync_jobs[0].payload) == {}

    backfill_jobs = await _jobs_of(db_factory, "interaction_backfill")
    assert len(backfill_jobs) == 1
    assert (backfill_jobs[0].account_id, backfill_jobs[0].operator_id) == (77, 0)
    # newcomer 语义:互动方硬指定成本号,被互动的是其余所有号
    assert json.loads(backfill_jobs[0].payload) == {
        "scope": "newcomer",
        "target_account_id": None,
        "actor_account_id": 77,
        "limit": None,
    }


async def test_kick_onboarding_chain_skips_in_flight(db_factory):
    """在途(queued/running)同 kind 同号不重复登记;已终态(done)不算在途。"""
    await _add_job(db_factory, "note_ledger_sync", 77, "queued")
    await _add_job(db_factory, "interaction_backfill", 77, "running")
    await _add_job(db_factory, "note_ledger_sync", 88, "queued")  # 别的号不干扰

    assert await cookie_check.kick_onboarding_chain(db_factory, 77) == []
    assert len(await _jobs_of(db_factory, "note_ledger_sync")) == 2  # 未新增
    assert len(await _jobs_of(db_factory, "interaction_backfill")) == 1

    # done 是终态,不挡新一轮引导(重登老号时该补的还得补)
    await _add_job(db_factory, "note_ledger_sync", 99, "done")
    await _add_job(db_factory, "interaction_backfill", 99, "error")
    assert len(await cookie_check.kick_onboarding_chain(db_factory, 99)) == 2


async def test_kick_onboarding_chain_swallows_failure(monkeypatch):
    """登记炸了只告警不上抛:引导链是写回之后的副作用,绝不能拖累写回本身。"""

    def broken_factory():
        raise RuntimeError("库炸了")

    assert await cookie_check.kick_onboarding_chain(broken_factory, 77) == []


async def _seed_account(factory, cookie_status: str) -> int:
    """造一个指定 cookie_status 的账号,返回 id。"""
    async with factory() as s:
        acc = XhsAccount(name="号", cookie_status=cookie_status)
        s.add(acc)
        await s.commit()
        return acc.id


async def test_write_back_unknown_to_valid_kicks_chain(db_factory, monkeypatch):
    """_write_back:unknown → valid 触发引导链(手动检测/REST 路径也走这里)。"""
    monkeypatch.setattr(db_module, "async_session", db_factory)
    acc_id = await _seed_account(db_factory, "unknown")

    await cookie_check._write_back(acc_id, "valid", None)

    async with db_factory() as s:
        assert (await s.get(XhsAccount, acc_id)).cookie_status == "valid"
    assert len(await _jobs_of(db_factory, "note_ledger_sync")) == 1
    assert len(await _jobs_of(db_factory, "interaction_backfill")) == 1


async def test_write_back_valid_to_valid_skips_chain(db_factory, monkeypatch):
    """_write_back:valid → valid 不触发(老号每次检测都过这条路,不能次次叠一对)。"""
    monkeypatch.setattr(db_module, "async_session", db_factory)
    acc_id = await _seed_account(db_factory, "valid")

    await cookie_check._write_back(acc_id, "valid", None)

    assert await _jobs_of(db_factory, "note_ledger_sync") == []
    assert await _jobs_of(db_factory, "interaction_backfill") == []


async def test_write_back_valid_to_invalid_skips_chain(db_factory, monkeypatch):
    """_write_back:转成非 valid 的写回一律不触发引导链。"""
    monkeypatch.setattr(db_module, "async_session", db_factory)
    acc_id = await _seed_account(db_factory, "valid")

    await cookie_check._write_back(acc_id, "invalid", None)

    assert await _jobs_of(db_factory, "note_ledger_sync") == []
    assert await _jobs_of(db_factory, "interaction_backfill") == []
