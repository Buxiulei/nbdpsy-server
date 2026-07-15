"""创作中心笔记数据导出的进程级内存台账:后台起浏览器导出 Excel → 落库,不加表。

对齐 cookie_check 的 ephemeral 台账设计(进程内存 registry + 后台 asyncio 任务 →
asyncio.to_thread 跑同步浏览器 + AccountLocks 串行 + TTL 驱逐):

- start_export 登记一条 running 台账并起后台任务,立即返回 export_id;调用方经 get_export 轮询终态。
- 后台任务把阻塞的 sync 浏览器导出经 asyncio.to_thread 下沉线程,不卡事件循环;
- 同号浏览器操作靠**共享 AccountLocks**(app.browser.account_locks 的进程级单例)与发布/cookie
  检测串行:三条路径共用同一把 per-account 锁、同一 profile 目录,若不串行,后到者
  SyncClient.start() 的 kill_orphans 会误杀正在跑的另一条链;
- 导出结果经 note_metrics_service.upsert_notes 落两表(最新快照 + 当天趋势);
- CreatorExportError / 任何异常 → 台账 error + reason,**绝不抛出**崩后台 loop、**绝不写半截数据**。

台账为进程级内存 dict,进程重启即丢(export_id 失效)。终态条目按 _ENTRY_TTL 在读/写时驱逐,
防 _registry 无界增长(导出量低但长跑进程仍会累积)。
"""

import asyncio
import os
import uuid
from datetime import datetime, timedelta, timezone

from loguru import logger

from app.browser.account_locks import account_locks
from app.browser.browser_gate import browser_slot
from app.browser.creator_export import CreatorExportError, export_notes
from app.browser.sync_client import SyncClient
from app.core.config import settings
from app.core.db import get_session
from app.services.note_metrics_service import upsert_notes

# 终态两态:落到这些状态的台账条目才可被超龄驱逐(running 进行中不动)。
_TERMINAL_STATUSES = ("done", "error")
# 终态台账条目最大留存;超此龄在读/写时驱逐,防 _registry 无界增长。
_ENTRY_TTL = timedelta(hours=1)

# export_id -> {"status","account_id","note_count","reason","created_at"} 的进程级台账。
_registry: dict[str, dict] = {}
# 后台任务强引用集合:防止未完成的 asyncio.Task 被 GC 提前回收。
_tasks: set[asyncio.Task] = set()


def _evict_stale() -> None:
    """驱逐超龄的**终态**台账条目(running 进行中不动),防 _registry 无界增长。

    读(get_export)/写(start_export)时各调一次:即便调用方从不轮询,新导出也会顺带清掉旧终态。
    """
    cutoff = datetime.utcnow() - _ENTRY_TTL
    stale = [
        export_id
        for export_id, entry in _registry.items()
        if entry["status"] in _TERMINAL_STATUSES and entry["created_at"] <= cutoff
    ]
    for export_id in stale:
        _registry.pop(export_id, None)


def start_export(account_id: int, cookies: list[dict]) -> str:
    """登记一条 running 台账并起后台导出任务,立即返回 export_id(不等导出完成)。"""
    _evict_stale()  # 顺带清掉超龄终态条目,防台账无界增长
    export_id = uuid.uuid4().hex
    _registry[export_id] = {
        "status": "running",
        "account_id": account_id,
        "note_count": 0,
        "reason": None,
        "created_at": datetime.utcnow(),
    }
    task = asyncio.create_task(_run_export(export_id, account_id, cookies))
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)  # 完成即从强引用集合移除,防泄漏
    return export_id


def get_export(export_id: str) -> dict | None:
    """按 export_id 取台账条目;不存在返回 None(交由端点层报"不存在或已过期")。"""
    _evict_stale()  # 读路径顺带清掉超龄终态条目
    return _registry.get(export_id)


def _export_sync(
    account_id: int, cookies: list[dict], download_dir: str, ts: str
) -> list[dict]:
    """同一线程内:建 SyncClient → start 建登录态 page → export_notes 导出 → stop 收尾。

    纯 sync,由 _run_export 经 asyncio.to_thread 调用(严格单线程建 client→操作→stop)。
    start 失败或导出失败均抛 CreatorExportError(reason 说明),由上层收成 error 台账。
    stop 在 finally 收尾:即便导出抛异常也关闭浏览器,不泄漏 camoufox 进程。
    """
    client = SyncClient(account_id, cookies, block_images=True)  # 导出纯只读,拦图省内存
    try:
        start = client.start()
        if not start.get("success"):
            # 浏览器基础设施失败,统一收成 CreatorExportError 交上层落 error 台账。
            raise CreatorExportError(f"browser_start_failed: {start.get('error')}")
        return export_notes(client.page, account_id, download_dir, ts)
    finally:
        client.stop()


async def _run_export(export_id: str, account_id: int, cookies: list[dict]) -> None:
    """后台导出:持号锁串行 → 线程内跑浏览器导出 → 落库两表 → 更新台账。

    时间基准在此生成(service 层可用真实时间):snapshot_date / now / ts 均取
    datetime.now(timezone.utc)。CreatorExportError / 任何异常 → error 台账 + reason,
    **不抛出**(绝不崩后台 loop)、**不落库**(绝不写半截数据)。
    """
    now = datetime.now(timezone.utc)
    snapshot_date = now.strftime("%Y-%m-%d")
    ts = now.strftime("%Y%m%d-%H%M%S")
    download_dir = os.path.join(settings.DATA_DIR, "creator_exports", str(account_id))
    try:
        # 与发布/cookie 检测共用同一把 per-account 锁:同号浏览器操作串行,避免 kill_orphans 互杀。
        async with account_locks.get(account_id):
            # 全局浏览器并发闸:封顶总 camoufox 数,超出排队(仅罩浏览器段,不含落库)。
            async with browser_slot():
                rows = await asyncio.to_thread(
                    _export_sync, account_id, cookies, download_dir, ts
                )
            # 导出成功才落库:用 get_session()(测试对 async_session monkeypatch 生效)。
            async with get_session() as session:
                count = await upsert_notes(
                    session, account_id, rows, snapshot_date, now
                )
        _update_entry(export_id, "done", note_count=count, reason=None)
    except CreatorExportError as exc:
        # 导出器语义失败(如 need_manual_login):落 error 台账,不落库、不上抛。
        logger.warning(
            f"笔记导出失败 export_id={export_id} account_id={account_id} reason={exc.reason}"
        )
        _update_entry(export_id, "error", note_count=0, reason=exc.reason)
    except Exception as exc:  # 兜底:导出任务异常也要落终态,别让台账永远 running
        logger.exception(
            f"笔记导出任务异常 export_id={export_id} account_id={account_id}"
        )
        _update_entry(export_id, "error", note_count=0, reason=f"导出任务异常:{exc}")


def _update_entry(
    export_id: str, status: str, note_count: int, reason: str | None
) -> None:
    """把导出结果更新进台账条目(条目已被同步移除时静默跳过)。"""
    entry = _registry.get(export_id)
    if entry is not None:
        entry["status"] = status
        entry["note_count"] = note_count
        entry["reason"] = reason
