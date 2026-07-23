"""创作中心笔记删除的进程级内存台账:后台起浏览器按标题删笔记。

对齐 note_export 的 ephemeral 台账设计(进程内存 registry + 后台 asyncio 任务 →
asyncio.to_thread 跑同步浏览器 + AccountLocks 串行 + TTL 驱逐),细节注释见
``note_export``,此处不重复。差异点:

- 删除是**不可逆**破坏性操作:浏览器层(app.browser.note_delete)有确认弹窗文案
  必须含「删除」的防误点闸,服务层不再重复校验;
- 结果字段为 ``deleted``(实际删除数)与 ``remaining``(剩余同题卡数),
  同题多篇(重复发布)用 ``count`` 一次会话删多篇。
"""

import asyncio
import uuid
from datetime import datetime, timedelta

from loguru import logger

from app.browser.account_locks import account_locks
from app.browser.browser_gate import browser_slot
from app.browser.note_delete import NoteDeleteError, delete_notes_by_title
from app.browser.sync_client import SyncClient

_TERMINAL_STATUSES = ("done", "error")
_ENTRY_TTL = timedelta(hours=1)

# deletion_id -> {"status","account_id","title","deleted","remaining","reason","created_at"}
_registry: dict[str, dict] = {}
_tasks: set[asyncio.Task] = set()


def _evict_stale() -> None:
    """驱逐超龄终态条目(running 不动),防 _registry 无界增长。"""
    cutoff = datetime.utcnow() - _ENTRY_TTL
    stale = [
        deletion_id
        for deletion_id, entry in _registry.items()
        if entry["status"] in _TERMINAL_STATUSES and entry["created_at"] <= cutoff
    ]
    for deletion_id in stale:
        _registry.pop(deletion_id, None)


def start_delete(
    account_id: int, cookies: list[dict], title: str, count: int = 1
) -> str:
    """登记 running 台账并起后台删除任务,立即返回 deletion_id。"""
    _evict_stale()
    deletion_id = uuid.uuid4().hex
    _registry[deletion_id] = {
        "status": "running",
        "account_id": account_id,
        "title": title,
        "deleted": 0,
        "remaining": None,
        "reason": None,
        "created_at": datetime.utcnow(),
    }
    task = asyncio.create_task(_run_delete(deletion_id, account_id, cookies, title, count))
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
    return deletion_id


def get_delete(deletion_id: str) -> dict | None:
    """按 deletion_id 取台账条目;不存在返回 None。"""
    _evict_stale()
    return _registry.get(deletion_id)


def _delete_sync(account_id: int, cookies: list[dict], title: str, count: int) -> dict:
    """同一线程内:建 SyncClient → start → 按标题删除 → stop 收尾(finally 防泄漏)。"""
    # 删除要在笔记管理页悬停/点击真实卡片,保留图片渲染(block_images 会缺封面影响布局)
    client = SyncClient(account_id, cookies)
    try:
        start = client.start()
        if not start.get("success"):
            raise NoteDeleteError(f"browser_start_failed: {start.get('error')}")
        return delete_notes_by_title(client.page, account_id, title, count)
    finally:
        client.stop()


async def _run_delete(
    deletion_id: str, account_id: int, cookies: list[dict], title: str, count: int
) -> None:
    """后台删除:持号锁串行 → 线程内跑浏览器删除 → 更新台账;异常落 error 不上抛。"""
    try:
        async with account_locks.get(account_id):
            async with browser_slot():
                result = await asyncio.to_thread(
                    _delete_sync, account_id, cookies, title, count
                )
        _update_entry(deletion_id, "done",
                      deleted=result["deleted"], remaining=result["remaining"],
                      reason=None)
    except NoteDeleteError as exc:
        logger.warning(
            f"笔记删除失败 deletion_id={deletion_id} account_id={account_id} "
            f"title={title!r} reason={exc.reason}"
        )
        _update_entry(deletion_id, "error", deleted=0, remaining=None, reason=exc.reason)
    except Exception as exc:  # 兜底:任务异常也要落终态,别让台账永远 running
        logger.exception(
            f"笔记删除任务异常 deletion_id={deletion_id} account_id={account_id}"
        )
        _update_entry(deletion_id, "error", deleted=0, remaining=None,
                      reason=f"删除任务异常:{exc}")


def _update_entry(
    deletion_id: str, status: str, deleted: int, remaining: int | None,
    reason: str | None,
) -> None:
    """把删除结果更新进台账条目(条目已被同步移除时静默跳过)。"""
    entry = _registry.get(deletion_id)
    if entry is not None:
        entry["status"] = status
        entry["deleted"] = deleted
        entry["remaining"] = remaining
        entry["reason"] = reason
