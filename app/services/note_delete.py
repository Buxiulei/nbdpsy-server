"""创作中心笔记删除服务:browser_jobs 台账落库 + 契约 execute()(2026-07-24 架构升级 P1)。

对齐 note_export 的落库改造,细节注释见 ``note_export``,此处不重复。差异点:

- 删除是**不可逆**破坏性操作:浏览器层(app.browser.note_delete)有确认弹窗文案
  必须含「删除」的防误点闸,服务层不再重复校验;僵死恢复对本 kind 置 error+unknown
  (绝不自动重跑),读方把 unknown 标记译成 status="unknown" 语义;
- 结果字段为 ``deleted``(实际删除数)与 ``remaining``(剩余同题卡数),
  同题多篇(重复发布)用 ``count`` 一次会话删多篇;
- **保留 note_deletions 表双写**(running/终态照旧,skills 侧 2026-07-23 反馈的
  可追溯语义):新台账 browser_jobs 为准,note_deletions 兼容旧轮询读一个版本
  (get_delete_persisted 的 DB 回退 + unknown 译义不变);deletion_id 经 payload
  传给 execute(execute 契约签名只有 account_id/payload,job id 由 enqueue 侧写入);
- ``_registry`` 保留为**进程内热路径兼容层**(REST 先读内存再回退 DB 的既有语义):
  新流程不再写它,仅供进行中任务的内存直读兼容与既有测试语义,无 TTL/后台任务。
"""

import asyncio
import uuid

from loguru import logger

from app.browser.account_locks import account_locks
from app.browser.browser_gate import browser_slot
from app.browser.note_delete import NoteDeleteError, delete_notes_by_title
from app.browser.sync_client import SyncClient
from app.core.db import get_session
from app.models.note_deletion import NoteDeletion
from app.services import browser_jobs_repo
from app.services.cookie_check import load_account_cookies

# deletion_id -> {"status","account_id","title","deleted","remaining","reason","created_at"}
# 进程内热路径兼容层(REST 内存命中语义):新流程不写入,命中即返回。
_registry: dict[str, dict] = {}


def start_delete(
    account_id: int, cookies: list[dict], title: str, count: int = 1,
    allow_ambiguous: bool = False,
) -> str:
    """登记一条 browser_jobs 台账并(all 模式)派进程内执行,立即返回 deletion_id。

    cookies 参数仅保签名兼容:执行时从账号行重新解密。deletion_id 同时写进 payload,
    供 execute 做 note_deletions 双写(execute 契约签名不含 job id)。
    """
    deletion_id = uuid.uuid4().hex
    payload = {"title": title, "count": count, "deletion_id": deletion_id,
               "allow_ambiguous": allow_ambiguous}
    browser_jobs_repo.enqueue_from_request(
        "note_delete", payload, account_id=account_id, job_id=deletion_id
    )
    browser_jobs_repo.spawn_inline(
        deletion_id, lambda: execute(account_id, payload)
    )
    return deletion_id


def get_delete(deletion_id: str) -> dict | None:
    """按 deletion_id 取台账条目;不存在返回 None(REST 再回退 get_delete_persisted)。

    读序:进程内热路径 _registry 命中即返回(既有内存语义)→ browser_jobs 台账映射。
    status 映射:queued/running → running;done → done + deleted/remaining;
    error → 带 unknown 标记(僵死恢复)译成 unknown,否则 error + reason。
    """
    entry = _registry.get(deletion_id)
    if entry is not None:
        return entry
    row = browser_jobs_repo.get_job_sync(
        browser_jobs_repo.current_db_path(), deletion_id
    )
    if row is None or row["kind"] != "note_delete":
        return None
    mapped = {
        "status": "running",
        "account_id": row["account_id"],
        "title": (row["payload"] or {}).get("title"),
        "deleted": 0,
        "remaining": None,
        "reason": None,
    }
    result = row["result"] or {}
    if row["status"] == "done":
        mapped["status"] = "done"
        mapped["deleted"] = result.get("deleted", 0)
        mapped["remaining"] = result.get("remaining")
    elif row["status"] == "error":
        # 僵死恢复(执行方死亡,删了没删未知)带 unknown 标记 → 译成 unknown 语义
        mapped["status"] = "unknown" if result.get("unknown") else "error"
        mapped["reason"] = result.get("error")
    return mapped


async def execute(account_id: int, payload: dict) -> dict:
    """执行一次按标题删除(契约函数,不碰 browser_jobs 台账;note_deletions 双写在此)。

    成功返回 {"deleted","remaining"};NoteDeleteError / 任何异常 → {"error": reason},
    不抛出。payload: {"title","count","deletion_id"}(deletion_id 供 note_deletions
    双写,缺失时跳过双写不拦主流程)。
    """
    title = (payload or {}).get("title", "")
    count = int((payload or {}).get("count", 1))
    deletion_id = (payload or {}).get("deletion_id")
    if deletion_id:
        try:
            await _db_insert_running(deletion_id, account_id, title)
        except Exception as exc:  # 旧台账写失败不拦删除主流程(新台账仍在)
            logger.warning(
                f"删除台账 DB 写入失败(不拦主流程) deletion_id={deletion_id}: {exc}"
            )
    try:
        cookies = await load_account_cookies(account_id)
        async with account_locks.get(account_id):
            async with browser_slot():
                result = await asyncio.to_thread(
                    _delete_sync, account_id, cookies, title, count
                )
        if deletion_id:
            await _finalize(
                deletion_id, "done",
                deleted=result["deleted"], remaining=result["remaining"], reason=None,
            )
        return {"deleted": result["deleted"], "remaining": result["remaining"]}
    except NoteDeleteError as exc:
        logger.warning(
            f"笔记删除失败 account_id={account_id} title={title!r} reason={exc.reason}"
        )
        if deletion_id:
            await _finalize(deletion_id, "error", deleted=0, remaining=None,
                            reason=exc.reason)
        return {"error": exc.reason}
    except Exception as exc:  # 兜底:任务异常也要给终态结果,别让轮询方死等
        logger.exception(f"笔记删除任务异常 account_id={account_id}")
        if deletion_id:
            await _finalize(deletion_id, "error", deleted=0, remaining=None,
                            reason=f"删除任务异常:{exc}")
        return {"error": f"删除任务异常:{exc}"}


def _delete_sync(account_id: int, cookies: list[dict], title: str, count: int) -> dict:
    """同一线程内:建 SyncClient → start → 按标题删除 → stop 收尾(finally 防泄漏)。"""
    # 删除要在笔记管理页悬停/点击真实卡片,保留图片渲染(block_images 会缺封面影响布局)
    client = SyncClient(account_id, cookies)
    try:
        start = client.start()
        if not start.get("success"):
            raise NoteDeleteError(f"browser_start_failed: {start.get('error')}")
        return delete_notes_by_title(client.page, account_id, title, count,
            allow_ambiguous=bool((payload or {}).get("allow_ambiguous")))
    finally:
        client.stop()


async def _db_insert_running(deletion_id: str, account_id: int, title: str) -> None:
    """向 note_deletions 旧台账落一条 running 行(execute 起步时,双写兼容)。"""
    async with get_session() as session:
        session.add(NoteDeletion(
            id=deletion_id, account_id=account_id, title=title,
            status="running", deleted=0,
        ))
        await session.commit()


async def _finalize(
    deletion_id: str, status: str, deleted: int, remaining: int | None,
    reason: str | None,
) -> None:
    """终态双写:内存兼容层 + note_deletions 旧台账(DB 失败只告警,不拦主流程)。"""
    _update_entry(deletion_id, status, deleted=deleted, remaining=remaining,
                  reason=reason)
    try:
        async with get_session() as session:
            row = await session.get(NoteDeletion, deletion_id)
            if row is not None:
                row.status = status
                row.deleted = deleted
                row.remaining = remaining
                row.reason = reason
                await session.commit()
    except Exception as exc:
        logger.warning(f"删除台账 DB 终态更新失败 deletion_id={deletion_id}: {exc}")


async def get_delete_persisted(deletion_id: str) -> dict | None:
    """browser_jobs / 内存台账都 miss 后的 note_deletions 回退读(REST 层用)。

    - 终态行(done/error)原样返回 —— 旧版本任务重启后"删了没删"仍可查(盲区闭合);
    - running 行且上游台账无此条 = 旧版本任务被重启打断,结果**未知**:译成
      status="unknown" + reason 说明,绝不冒充 running(它永远不会完成)。
    """
    async with get_session() as session:
        row = await session.get(NoteDeletion, deletion_id)
    if row is None:
        return None
    entry = {
        "status": row.status,
        "account_id": row.account_id,
        "title": row.title,
        "deleted": row.deleted,
        "remaining": row.remaining,
        "reason": row.reason,
    }
    if row.status == "running":
        # 上游台账没有而 DB 停在 running:server 重启打断,结果未知
        entry["status"] = "unknown"
        entry["reason"] = (
            "server 重启中断了删除任务,是否已删结果未知:请到创作中心笔记管理页"
            "人工核对该标题笔记数量后再决定是否重新发起"
        )
    return entry


def _update_entry(
    deletion_id: str, status: str, deleted: int, remaining: int | None,
    reason: str | None,
) -> None:
    """把删除结果更新进内存兼容层条目(条目不存在时静默跳过)。"""
    entry = _registry.get(deletion_id)
    if entry is not None:
        entry["status"] = status
        entry["deleted"] = deleted
        entry["remaining"] = remaining
        entry["reason"] = reason
