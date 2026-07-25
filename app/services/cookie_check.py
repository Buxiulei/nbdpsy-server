"""cookie 活性检测服务:browser_jobs 台账落库 + 契约 execute()(2026-07-24 架构升级 P1)。

原进程级内存台账(_registry)收敛进 browser_jobs 表(kind=cookie_check),重启不丢
任务/终态,check_id 永不因重启 404:

- start_check 登记一条 queued 台账行立即返回 check_id;NBDPSY_ROLE=all 时经
  spawn_inline 在本进程消费(claim→execute→finish 纪律),role=api 时纯 enqueue
  交 worker 进程执行;
- execute() 为提炼出的契约执行函数(account_worker 子进程与进程内消费共用):
  持号锁串行 → 浏览器闸 → 线程内跑登录检测 → 写回账号,**不碰 browser_jobs 台账**
  (claim/finish 由调用方负责);
- 写回沿用既有语义:valid/invalid/captcha 写回 cookie_status/last_check_at + 回填
  user_info;error(基础设施失败)**不写回、保留原值**,避免把好号误标失效;
- 同号浏览器操作靠**共享 AccountLocks**(app.browser.account_locks 的进程级单例)与
  发布链串行:检测与发布共用同一 profile 目录,SyncClient.start() 的 kill_orphans
  会按 argv 精确杀该 profile 的所有 camoufox,不串行会互杀,故不能各持独立锁;
- 安全:payload 不存明文 cookie,execute 时从账号行重新解密(login_cookies 落库
  本就是 Fernet 加密,台账不能开明文旁路)。
"""

import asyncio
import json
from datetime import datetime

from loguru import logger

from app.browser import sync_client
from app.browser.account_locks import account_locks
from app.browser.browser_gate import browser_slot
from app.core.db import get_session
from app.core.security import decrypt_cookies
from app.models.xhs_account import XhsAccount
from app.services import browser_jobs_repo

# check_login_once 返回 user_info 时回填到账号的字段(与 cookie_checker 一致的子集)
_USER_INFO_FIELDS = ("nickname", "user_id", "red_id", "avatar")


def start_check(account_id: int, cookies: list[dict]) -> str:
    """登记一条 browser_jobs 台账并(all 模式)派进程内执行,立即返回 check_id。

    cookies 参数仅保签名兼容(REST 层解密后传入):执行时从账号行重新解密取最新值,
    不落明文进台账。
    """
    payload: dict = {}
    check_id = browser_jobs_repo.enqueue_from_request(
        "cookie_check", payload, account_id=account_id
    )
    browser_jobs_repo.spawn_inline(
        check_id, lambda: execute(account_id, payload)
    )
    return check_id


def get_check(check_id: str) -> dict | None:
    """按 check_id 读台账并映射回既有 entry 形状;不存在返回 None(REST 报 404)。

    status 映射:queued/running → checking;done → 检测结果四态(valid/invalid/
    captcha/error);台账 error(执行崩溃)→ error + reason。
    """
    row = browser_jobs_repo.get_job_sync(
        browser_jobs_repo.current_db_path(), check_id
    )
    if row is None or row["kind"] != "cookie_check":
        return None
    entry = {
        "status": "checking",
        "account_id": row["account_id"],
        "user_info": None,
        "reason": None,
    }
    result = row["result"] or {}
    if row["status"] == "done":
        entry["status"] = result.get("status", "error")
        entry["user_info"] = result.get("user_info")
        entry["reason"] = result.get("reason")
    elif row["status"] == "error":
        entry["status"] = "error"
        entry["reason"] = result.get("error")
    return entry


async def execute(account_id: int, payload: dict) -> dict:
    """执行一次 cookie 活性检测(契约函数,不碰 browser_jobs 台账)。

    返回 {"status","user_info","reason"};任何意外兜底为 status=error,绝不抛出
    (error 属检测结果语义,台账仍记 done——与"执行崩溃"的台账 error 区分)。
    """
    try:
        cookies = await load_account_cookies(account_id)
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

        # error:基础设施失败,不写回账号(保留原 cookie_status),仅报结果。
        if status != "error":
            await _write_back(account_id, status, user_info)
        return {"status": status, "user_info": user_info, "reason": reason}
    except Exception as exc:  # 兜底:检测异常也要给终态结果,别让轮询方死等
        logger.exception(f"cookie 检测任务异常 account_id={account_id}")
        return {"status": "error", "user_info": None, "reason": f"检测任务异常:{exc}"}


async def load_account_cookies(account_id: int) -> list[dict]:
    """从账号行解密 login_cookies 回列表;无号/空 cookie → []。

    note_export / note_delete 的 execute 也复用(payload 不存明文 cookie 的统一取数口)。
    """
    async with get_session() as session:
        account = await session.get(XhsAccount, account_id)
    if account is None or not account.login_cookies:
        return []
    plaintext = decrypt_cookies(account.login_cookies)
    if not plaintext:
        return []
    return json.loads(plaintext)


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
                # 内部展示名跟随小红书昵称实时更新:运营在小红书 App 改了账号名后,
                # 一次检测即把 name 同步为最新昵称(前端/插件展示 name),名称随平台侧收敛。
                nickname = user_info.get("nickname")
                if nickname:
                    account.name = nickname
            await session.commit()
