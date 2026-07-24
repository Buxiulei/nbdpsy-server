"""创作中心笔记数据导出服务:browser_jobs 台账落库 + 契约 execute()(2026-07-24 架构升级 P1)。

原进程级内存台账收敛进 browser_jobs 表(kind=note_export),重启不丢任务/终态:

- start_export 登记一条 queued 台账行立即返回 export_id;NBDPSY_ROLE=all 时经
  spawn_inline 在本进程消费(claim→execute→finish 纪律),role=api 时纯 enqueue;
- execute() 为提炼出的契约执行函数(account_worker 子进程与进程内消费共用):
  持号锁串行 → 浏览器闸 → 线程内跑同步浏览器导出 → 经 note_metrics_service.upsert_notes
  落两表(最新快照 + 当天趋势),**不碰 browser_jobs 台账**(claim/finish 由调用方);
- 同号浏览器操作靠**共享 AccountLocks**与发布/cookie 检测串行:三条路径共用同一把
  per-account 锁、同一 profile 目录,不串行会被 SyncClient.start() 的 kill_orphans 互杀;
- CreatorExportError / 任何异常 → 返回 {"error": reason}(台账由调用方落 error),
  **绝不抛出**崩调用方 loop、**绝不写半截数据**;
- 安全:payload 不存明文 cookie,execute 时从账号行重新解密。
"""

import asyncio
import os
from datetime import datetime, timezone

from loguru import logger

from app.browser.account_locks import account_locks
from app.browser.browser_gate import browser_slot
from app.browser.creator_export import CreatorExportError, export_notes
from app.browser.sync_client import SyncClient
from app.core.config import settings
from app.core.db import get_session
from app.services import browser_jobs_repo
from app.services.cookie_check import load_account_cookies
from app.services.note_metrics_service import upsert_notes


def start_export(account_id: int, cookies: list[dict]) -> str:
    """登记一条 browser_jobs 台账并(all 模式)派进程内执行,立即返回 export_id。

    cookies 参数仅保签名兼容(REST 层解密后传入):执行时从账号行重新解密取最新值,
    不落明文进台账。
    """
    payload: dict = {}
    export_id = browser_jobs_repo.enqueue_from_request(
        "note_export", payload, account_id=account_id
    )
    browser_jobs_repo.spawn_inline(
        export_id, lambda: execute(account_id, payload)
    )
    return export_id


def get_export(export_id: str) -> dict | None:
    """按 export_id 读台账并映射回既有 entry 形状;不存在返回 None(REST 报 404)。

    status 映射:queued/running → running;done → done + note_count;error → error + reason。
    """
    row = browser_jobs_repo.get_job_sync(
        browser_jobs_repo.current_db_path(), export_id
    )
    if row is None or row["kind"] != "note_export":
        return None
    entry = {
        "status": "running",
        "account_id": row["account_id"],
        "note_count": 0,
        "reason": None,
    }
    result = row["result"] or {}
    if row["status"] == "done":
        entry["status"] = "done"
        entry["note_count"] = result.get("note_count", 0)
    elif row["status"] == "error":
        entry["status"] = "error"
        entry["reason"] = result.get("error")
    return entry


async def execute(account_id: int, payload: dict) -> dict:
    """执行一次创作中心笔记导出(契约函数,不碰 browser_jobs 台账)。

    时间基准在此生成:snapshot_date / now / ts 均取 datetime.now(timezone.utc)。
    成功返回 {"note_count": N};CreatorExportError / 任何异常 → {"error": reason},
    **不抛出**、**不落库半截数据**。
    """
    now = datetime.now(timezone.utc)
    snapshot_date = now.strftime("%Y-%m-%d")
    ts = now.strftime("%Y%m%d-%H%M%S")
    download_dir = os.path.join(settings.DATA_DIR, "creator_exports", str(account_id))
    try:
        cookies = await load_account_cookies(account_id)
        # 与发布/cookie 检测共用同一把 per-account 锁:同号浏览器操作串行,避免 kill_orphans 互杀。
        async with account_locks.get(account_id):
            # 全局浏览器并发闸:封顶总 camoufox 数,超出排队(仅罩浏览器段,不含落库)。
            async with browser_slot():
                rows = await asyncio.to_thread(
                    _export_sync, account_id, cookies, download_dir, ts
                )
            # 导出成功才落库:用 get_session()(测试对 async_session monkeypatch 生效)。
            async with get_session() as session:
                count = await upsert_notes(session, account_id, rows, snapshot_date, now)
        return {"note_count": count}
    except CreatorExportError as exc:
        # 导出器语义失败(如 need_manual_login):返回 error,不落库、不上抛。
        logger.warning(f"笔记导出失败 account_id={account_id} reason={exc.reason}")
        return {"error": exc.reason}
    except Exception as exc:  # 兜底:导出异常也要给终态结果,别让轮询方死等
        logger.exception(f"笔记导出任务异常 account_id={account_id}")
        return {"error": f"导出任务异常:{exc}"}


def _export_sync(
    account_id: int, cookies: list[dict], download_dir: str, ts: str
) -> list[dict]:
    """同一线程内:建 SyncClient → start 建登录态 page → export_notes 导出 → stop 收尾。

    纯 sync,由 execute 经 asyncio.to_thread 调用(严格单线程建 client→操作→stop)。
    start 失败或导出失败均抛 CreatorExportError(reason 说明),由上层收成 error 结果。
    stop 在 finally 收尾:即便导出抛异常也关闭浏览器,不泄漏 camoufox 进程。
    """
    client = SyncClient(account_id, cookies, block_images=True)  # 导出纯只读,拦图省内存
    try:
        start = client.start()
        if not start.get("success"):
            # 浏览器基础设施失败,统一收成 CreatorExportError 交上层落 error 结果。
            raise CreatorExportError(f"browser_start_failed: {start.get('error')}")
        return export_notes(client.page, account_id, download_dir, ts)
    finally:
        client.stop()
