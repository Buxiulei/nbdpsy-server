"""笔记组件状态**只读查询**服务:契约 execute() + 登记入口。

为什么存在(2026-08-04 运营 P0-1,本清单最核心一条):引用与合集在正文里零痕迹,
调用方无法程序化自证"挂上了没有"——接口 applied:true 是**设置当时**的回读确认,
但事后任意时刻的当前状态没有任何查询手段;而台账 published_notes 压根没有这两个字段。
8 月计划 360 篇每篇都要挂,人工开 App 逐条看不现实。

本服务开更新页**只读**一份组件快照(引用/合集/话题/图数/权限/合集入口存在性),
分层纪律与 note_visibility 完全一致:execute 持号锁→浏览器闸→线程内跑同步读,
不碰 browser_jobs 台账,异常收敛 {"error"} 绝不抛出。

**幂等**(纯只读,重跑无任何副作用)→ kind 进 ``_IDEMPOTENT_KINDS``,僵死可自动重跑。
"""

import asyncio

from loguru import logger

from app.browser.account_locks import account_locks
from app.browser.browser_gate import browser_slot
from app.browser.note_components import read_components_snapshot
from app.browser.sync_client import SyncClient
from app.services import browser_jobs_repo
from app.services.cookie_check import load_account_cookies

JOB_KIND = "note_components_read"


def start_read(account_id: int, note_id: str) -> str:
    """REST 触发一次组件状态只读;登记 browser_jobs 台账,返回轮询 id。"""
    job_id = browser_jobs_repo.enqueue_from_request(
        JOB_KIND, {"note_id": note_id}, account_id=account_id
    )
    browser_jobs_repo.spawn_inline(
        job_id, lambda: execute(account_id, {"note_id": note_id})
    )
    return job_id


async def execute(account_id: int, payload: dict) -> dict:
    """契约执行:开更新页只读组件快照;任何异常收敛成 {"error"} 不抛出。"""
    note_id = (payload or {}).get("note_id") or ""
    if not note_id:
        return {"error": "note_id_missing: payload 里没有 note_id"}
    try:
        cookies = await load_account_cookies(account_id)
        if not cookies:
            return {"error": "账号无可用 cookie,跳过组件读取"}
        async with account_locks.get(account_id):
            async with browser_slot():
                return await asyncio.to_thread(_read_sync, account_id, note_id, cookies)
    except Exception as exc:  # noqa: BLE001 — 兜底给终态,别让轮询方死等
        logger.exception(f"组件状态读取任务异常 account_id={account_id}")
        return {"error": f"组件状态读取任务异常:{exc}"}


def _read_sync(account_id: int, note_id: str, cookies: list) -> dict:
    """同一线程内:建 SyncClient → start → 只读快照 → stop(finally 防泄漏)。"""
    client = SyncClient(account_id, cookies, block_images=True)
    try:
        start = client.start()
        if not start.get("success"):
            return {"error": f"browser_start_failed: {start.get('error')}"}
        return read_components_snapshot(client.page, account_id, note_id)
    finally:
        client.stop()
