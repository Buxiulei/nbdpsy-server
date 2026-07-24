"""cookie 检测服务(app.services.cookie_check)单测,不起真浏览器。

P1 台账落库后重写:旧版测的内存 _registry/_tasks/驱逐机制已整体移除(台账进
browser_jobs 表,持久化语义在 tests/test_browser_jobs_repo.py 锁定;REST 全链在
tests/test_cookie_checks_rest.py 锁定)。本文件保留并适配原三关注点中仍然成立的两个:

- 共享锁:检测执行(execute)与发布调度对同号用**同一把** per-account 锁
  (进程级单例)——断言同锁对象 + 同号检测在发布持锁时串行等待。
- 执行崩溃终态:进程内消费管线(_run_inline)在 execute 抛异常时把台账落成
  error 终态,不让轮询方死等。
"""

import asyncio

from app.browser.account_locks import account_locks
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

    def fake_check_login_once(account_id, cookies):
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
