"""笔记可见性切换服务:契约 execute() + 切换成功后的台账留痕。

设计 docs/design/2026-07-31-note-visibility-design.md 第 3.1 / 3.3 节。分层与
``note_delete`` / ``matrix_interact`` 一致(浏览器动作在 ``app.browser.note_visibility``,
本模块只管取数、并发闸与落库):

- ``execute()`` 为契约执行函数(account_worker 子进程消费):持号锁串行 → 浏览器闸 →
  线程内跑同步切换,**不碰 browser_jobs 台账**(claim/finish 由调用方);任何异常收敛成
  ``{"error": reason}``,**绝不抛出**。
- payload:``{"note_id","title","target_privacy","operator_id"?}``。``note_id`` 用于
  **回读校验**(列表 DOM 不暴露 note_id,定位只能靠 title),缺了就没法确认切换是否生效,
  故与 title 一样是必填。``operator_id`` 是 ``visibility_changed_by`` 的来源——execute
  的契约签名只有 ``(account_id, payload)``,拿不到 browser_jobs 行的 operator_id,
  只能由登记方写进 payload;缺失则留痕记 NULL(不阻断切换)。
- **本期只做 0=公开可见 / 1=仅自己可见两档**:另外三档与 ``user_ids`` 格式完全未验证,
  收到即拒(设计第四节)。
- ``note_visibility`` **非幂等**,不在 ``browser_jobs_repo._IDEMPOTENT_KINDS`` 里:
  重复设同一档位在平台侧无害,但僵死任务自动重跑有实际危险 —— 若期间运营手工把笔记改回
  公开,过期任务重跑会**再次把它藏起来**。与 ``note_delete`` / ``matrix_interact`` 同款纪律。
"""

import asyncio
from datetime import datetime

from loguru import logger
from sqlalchemy import select

from app.browser.account_locks import account_locks
from app.browser.browser_gate import browser_slot
from app.browser.note_visibility import NoteVisibilityError, set_note_visibility
from app.browser.sync_client import SyncClient
from app.core.db import get_session
from app.models.published_note import PublishedNote
from app.services import browser_jobs_repo
from app.services.cookie_check import load_account_cookies

# 本期支持的档位(设计第四节:其余三档与 user_ids 格式未验证,一律不做)
SUPPORTED_PRIVACY = (0, 1)


def start_change(
    account_id: int,
    note_id: str,
    title: str,
    target_privacy: int,
    operator_id: int | None,
) -> str:
    """REST 触发一次可见性切换;登记 browser_jobs 台账,返回轮询 id。

    ``operator_id`` 写进 payload 供 ``_record_change`` 落 ``visibility_changed_by``
    ——execute 的契约签名只有 ``(account_id, payload)``,拿不到请求上下文。
    档位合法性由入口(REST 请求体)与 ``execute`` 各自把关,本函数只负责登记。
    """
    payload = {
        "note_id": note_id,
        "title": title,
        "target_privacy": target_privacy,
        "operator_id": operator_id,
    }
    change_id = browser_jobs_repo.enqueue_from_request(
        "note_visibility", payload, account_id=account_id
    )
    browser_jobs_repo.spawn_inline(change_id, lambda: execute(account_id, payload))
    return change_id


async def execute(account_id: int, payload: dict) -> dict:
    """执行一次可见性切换(契约函数,不碰 browser_jobs 台账)。

    成功返回 ``{"status": "done"|"skipped", "permission_code", ...}``;
    入参不合法 / 切换失败 / 任何异常 → ``{"error": reason}``,**不抛出**。
    """
    payload = payload or {}
    note_id = str(payload.get("note_id") or "").strip()
    title = str(payload.get("title") or "").strip()
    target = payload.get("target_privacy")
    if not note_id:
        return {"error": "payload 缺 note_id,无法回读校验切换是否生效"}
    if not title:
        # 标题是唯一的定位手段(列表 DOM 不暴露 note_id),空标题的笔记本期定位不了
        return {"error": "note_not_locatable: payload 缺 title,无法定位笔记"}
    if isinstance(target, bool) or target not in SUPPORTED_PRIVACY:
        return {
            "error": f"unsupported_privacy: 本期只做 0=公开可见 / 1=仅自己可见,"
                     f"收到 target_privacy={target!r}"
        }

    try:
        cookies = await load_account_cookies(account_id)
        if not cookies:
            return {"error": "账号无可用 cookie,跳过可见性切换"}
        # 与发布/cookie 检测共用同一把 per-account 锁:同号浏览器操作串行,避免 kill_orphans 互杀。
        async with account_locks.get(account_id):
            # 全局浏览器并发闸:封顶总 camoufox 数,超出排队。
            async with browser_slot():
                result = await asyncio.to_thread(
                    _set_sync, account_id, cookies, note_id, title, target
                )
    except NoteVisibilityError as exc:
        logger.warning(
            f"可见性切换失败 account_id={account_id} note_id={note_id} reason={exc.reason}"
        )
        return {"error": exc.reason}
    except Exception as exc:  # 兜底:异常也要给终态结果,别让台账悬挂
        logger.exception(f"可见性切换任务异常 account_id={account_id} note_id={note_id}")
        return {"error": f"可见性切换任务异常:{exc}"}

    if result.get("status") == "done":
        # skipped 不留痕:什么都没改,visibility_changed_at 的语义是"我们主动切成功的时刻"
        await _record_change(account_id, note_id, result, payload.get("operator_id"))
    return result


def _set_sync(
    account_id: int,
    cookies: list[dict],
    note_id: str,
    title: str,
    target_privacy: int,
) -> dict:
    """同一线程内:建 SyncClient → start → 切可见性 → stop 收尾(finally 防泄漏)。

    headed 真屏沿用 SyncClient 默认(headless=False);要悬停真实卡片、点弹窗按钮,
    **不 block_images**(缺封面会影响卡片布局与坐标,同 note_delete)。
    """
    client = SyncClient(account_id, cookies)
    try:
        start = client.start()
        if not start.get("success"):
            raise NoteVisibilityError(f"browser_start_failed: {start.get('error')}")
        return set_note_visibility(
            client.page, account_id, note_id, title, target_privacy
        )
    finally:
        client.stop()


async def _record_change(
    account_id: int, note_id: str, result: dict, operator_id: int | None
) -> None:
    """把切换成功的结果落到台账行:平台原值 + 我们自己的操作留痕。

    台账里没有该 note_id 的行(还没同步到)时**只告警不建行**——建台账行是 note_ledger
    的职责,这里凭空造一行会绕过它那套 pending/linked/orphan 判定。落库失败也只告警:
    平台侧改动已经生效并回读确认过,为了一次留痕写失败把任务判成 error 是误报。
    """
    try:
        async with get_session() as session:
            row = await session.scalar(
                select(PublishedNote).where(
                    PublishedNote.account_id == account_id,
                    PublishedNote.note_id == note_id,
                )
            )
            if row is None:
                logger.warning(
                    f"[note_visibility] 台账无 note_id={note_id} 的行(尚未同步),"
                    f"可见性留痕跳过;下次台账同步会补上 permission_code"
                )
                return
            row.permission_code = result.get("permission_code")
            row.permission_msg = result.get("permission_msg")
            row.visibility_changed_at = datetime.utcnow()
            row.visibility_changed_by = operator_id
            await session.commit()
        logger.info(
            f"[note_visibility] 账号{account_id} 笔记 {note_id} 可见性留痕已落库"
            f"(permission_code={result.get('permission_code')}, by={operator_id})"
        )
    except Exception as exc:  # noqa: BLE001 — 留痕失败不改写已生效的平台结果
        logger.warning(
            f"[note_visibility] 可见性留痕落库失败 note_id={note_id}"
            f"(平台侧改动已生效,不影响任务终态): {exc}"
        )
