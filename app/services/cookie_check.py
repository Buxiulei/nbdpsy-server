"""check_cookies 异步化的进程级内存台账:后台起浏览器检测 + 结果轮询,不加表。

check_cookies 工具不再同步阻塞 20-40s:调 start_check 登记一条 check 并起后台任务立即返回
check_id;调用方用 get_check(经 get_cookie_check 工具)轮询到终态。设计对齐 cookie_checker:
- 后台任务把阻塞的 sync 浏览器调用经 asyncio.to_thread 下沉线程,不卡事件循环;
- 写回沿用 check_cookies 语义:valid/invalid/captcha 写回 cookie_status/last_check_at + 回填
  user_info;error(基础设施失败)**不写回、保留原值**,仅落台账,避免把好号误标失效;
- 同号浏览器操作靠**共享 AccountLocks**(app.browser.account_locks 的进程级单例)与发布链
  串行:检测与发布用同一把 per-account 锁,同号只能一条链在跑。二者共用同一 profile 目录
  DATA_DIR/browser/account_{id},SyncClient.start() 的 kill_orphans 会按 argv 精确杀该
  profile 的所有 camoufox;若不串行,后到的检测/发布会误杀正在跑的另一条链。故不能各持独立锁。

台账为进程级内存 dict,进程重启即丢(check_id 失效)。终态条目按 _ENTRY_TTL 在读/写时驱逐,
防 _registry 无界增长(检测量低但长跑进程仍会累积)。
"""

import asyncio
import uuid
from datetime import datetime, timedelta

from loguru import logger

from app.browser import sync_client
from app.browser.account_locks import account_locks
from app.browser.browser_gate import browser_slot
from app.core.db import get_session
from app.models.xhs_account import XhsAccount

# check_login_once 返回 user_info 时回填到账号的字段(与 cookie_checker 一致的子集)
_USER_INFO_FIELDS = ("nickname", "user_id", "red_id", "avatar")

# 终态四态:落到这些状态的台账条目才可被超龄驱逐(checking 进行中不动)。
_TERMINAL_STATUSES = ("valid", "invalid", "captcha", "error")
# 终态台账条目最大留存;超此龄在读/写时驱逐,防 _registry 无界增长。
_ENTRY_TTL = timedelta(hours=1)

# check_id -> {"status","account_id","user_info","reason","created_at"} 的进程级台账。
_registry: dict[str, dict] = {}
# 后台任务强引用集合:防止未完成的 asyncio.Task 被 GC 提前回收。
_tasks: set[asyncio.Task] = set()


def _evict_stale() -> None:
    """驱逐超龄的**终态**台账条目(checking 进行中不动),防 _registry 无界增长。

    读(get_check)/写(start_check)时各调一次:即便调用方从不轮询,新检测也会顺带清掉旧终态。
    """
    cutoff = datetime.utcnow() - _ENTRY_TTL
    stale = [
        check_id
        for check_id, entry in _registry.items()
        if entry["status"] in _TERMINAL_STATUSES and entry["created_at"] <= cutoff
    ]
    for check_id in stale:
        _registry.pop(check_id, None)


def start_check(account_id: int, cookies: list[dict]) -> str:
    """登记一条 checking 台账并起后台检测任务,立即返回 check_id(不等检测完成)。"""
    _evict_stale()  # 顺带清掉超龄终态条目,防台账无界增长
    check_id = uuid.uuid4().hex
    _registry[check_id] = {
        "status": "checking",
        "account_id": account_id,
        "user_info": None,
        "reason": None,
        "created_at": datetime.utcnow(),
    }
    task = asyncio.create_task(_run_check(check_id, account_id, cookies))
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)  # 完成即从强引用集合移除,防泄漏
    return check_id


def get_check(check_id: str) -> dict | None:
    """按 check_id 取台账条目;不存在返回 None(交由工具层报"不存在或已过期")。"""
    _evict_stale()  # 读路径顺带清掉超龄终态条目
    return _registry.get(check_id)


async def _run_check(check_id: str, account_id: int, cookies: list[dict]) -> None:
    """后台检测:持号锁串行 → 线程内跑登录检测 → 写回账号(error 除外)→ 更新台账。

    任何意外都兜底为 error 台账,绝不让 check 卡在 checking 让轮询方死等。
    """
    try:
        # 与发布链共用同一把 per-account 锁:同号发布/检测串行,避免 kill_orphans 互杀。
        async with account_locks.get(account_id):
            # 全局浏览器并发闸:封顶总 camoufox 数,超出排队(仅罩浏览器段,不含写回)。
            async with browser_slot():
                result = await asyncio.to_thread(
                    sync_client.check_login_once, account_id, cookies
                )
            status = result.get("status", "invalid")
            user_info = result.get("user_info")
            reason = result.get("reason")

            # error:基础设施失败,不写回账号(保留原 cookie_status),仅落台账。
            if status != "error":
                await _write_back(account_id, status, user_info)
            _update_entry(check_id, status, user_info, reason)
    except Exception as exc:  # 兜底:检测任务异常也要落终态,别让台账永远 checking
        logger.exception(
            f"cookie 异步检测任务异常 check_id={check_id} account_id={account_id}"
        )
        _update_entry(check_id, "error", None, f"检测任务异常:{exc}")


async def _write_back(account_id: int, status: str, user_info: dict | None) -> None:
    """把 valid/invalid/captcha 写回 cookie_status/last_check_at,并回填非空 user_info。

    用 get_session()(读 db_module.async_session,测试对其 monkeypatch 生效),会话内重取
    账号避免操作 detached 实例。
    """
    async with get_session() as session:
        account = await session.get(XhsAccount, account_id)
        if account is not None:
            account.cookie_status = status
            account.last_check_at = datetime.utcnow()
            if user_info:
                for field in _USER_INFO_FIELDS:
                    value = user_info.get(field)
                    if value:
                        setattr(account, field, value)
            await session.commit()


def _update_entry(
    check_id: str, status: str, user_info: dict | None, reason: str | None
) -> None:
    """把检测结果更新进台账条目(条目已被同步移除时静默跳过)。"""
    entry = _registry.get(check_id)
    if entry is not None:
        entry["status"] = status
        entry["user_info"] = user_info
        entry["reason"] = reason
