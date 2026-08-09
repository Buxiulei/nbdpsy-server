"""**笔记合集**创建服务:契约 ``execute()``(browser_jobs kind=``note_collection_create``)。

与 ``podcast_collection`` 是**两套东西**,别混:那个建的是**播客合集**(发播客 tab 里,
封面必填 + 裁剪二次确认),这个建的是**笔记合集**——即 ``GET /api/accounts/{id}/collections``
读的那套 picker 系统,图文笔记挂载(``POST /note-components`` 的 ``collection_id``)只认它。

分层与 ``note_components_read`` / ``note_visibility`` 一致(浏览器动作在
``app.browser.note_components``,本模块只管取数、并发闸与登记):

- ``start_create()``:REST 触发,登记 browser_jobs 台账并返回轮询 id;
- ``execute()``:契约执行函数(account_worker 子进程消费),持号锁串行 → 浏览器闸 →
  线程内跑同步创建,**不碰 browser_jobs 台账**(claim/finish 由调用方);任何异常收敛成
  ``{"error": reason}``,**绝不抛出**。

**非幂等**,故意不进 ``browser_jobs_repo._IDEMPOTENT_KINDS``:平台**不去重同名合集**
(播客合集实证),僵死任务自动重跑会建出第二个同名合集,而"多一个空合集"要人工去平台删。
浏览器层已有建前查重挡在门口,但那挡的是"两次请求",挡不住"同一请求被系统重跑"——
所以台账这一层同样必须落 ``unknown`` 交给人核对,与 ``note_visibility`` / ``note_delete``
同款纪律。
"""

import asyncio
from typing import Any, Optional

from loguru import logger

from app.browser.account_locks import account_locks
from app.browser.browser_gate import browser_slot
from app.browser.note_components import NoteComponentsError, create_note_collection
from app.browser.sync_client import SyncClient
from app.services import browser_jobs_repo
from app.services.cookie_check import load_account_cookies

KIND = "note_collection_create"


def start_create(
    account_id: int, name: str, description: Optional[str], carrier_note_id: str
) -> str:
    """REST 触发一次笔记合集创建;登记 browser_jobs 台账,返回轮询 job_id。

    入参合法性(名称 ≤20 字 / 简介 ≤50 字 / 载体笔记非空 / cover 拒收)由 REST 层把关,
    本函数只负责登记 —— 与 ``podcast_collection.start_create`` 同款分工。
    """
    payload = {
        "name": name,
        "description": description,
        "carrier_note_id": carrier_note_id,
    }
    job_id = browser_jobs_repo.enqueue_from_request(KIND, payload, account_id=account_id)
    browser_jobs_repo.spawn_inline(job_id, lambda: execute(account_id, payload))
    return job_id


async def execute(account_id: int, payload: dict) -> dict:
    """执行一次笔记合集创建(契约函数,不碰 browser_jobs 台账)。

    成功返回 ``{"status":"done","collection_id",...}``;入参不合法 / 创建失败 / 任何异常
    → ``{"error": reason}``,**不抛出**。
    """
    payload = payload or {}
    name = str(payload.get("name") or "").strip()
    description = str(payload.get("description") or "").strip() or None
    carrier_note_id = str(payload.get("carrier_note_id") or "").strip()
    if not name:
        return {"error": "payload 缺 name,合集名称是平台必填项"}
    if not carrier_note_id:
        return {
            "error": "payload 缺 carrier_note_id:笔记合集的创建入口只在笔记编辑器的"
                     "「加入合集」弹层里,必须借一篇笔记打开编辑器"
        }

    try:
        cookies = await load_account_cookies(account_id)
        if not cookies:
            return {"error": "该账号未接入 cookie,无法打开笔记编辑器"}
        # 与发布/cookie 检测共用同一把 per-account 锁:同号浏览器操作串行,
        # 避免 profile_guard.kill_orphans 互杀。
        async with account_locks.get(account_id):
            async with browser_slot():
                result = await asyncio.to_thread(
                    _create_sync, account_id, cookies, name, description, carrier_note_id
                )
    except Exception as exc:  # noqa: BLE001 — 兜底也要给终态,别让台账悬挂
        logger.exception(f"笔记合集创建任务异常 account_id={account_id} name={name}")
        return {"error": f"笔记合集创建任务异常:{exc}"}

    if result.get("status") != "done":
        # 台账的终态判据是有没有 "error" 键:浏览器层的 error 要翻译过来,
        # 否则一次失败的创建会以 done 收尾(静默假成功)。
        return {
            "error": result.get("reason") or "笔记合集创建失败(浏览器层未给原因)",
            **{k: v for k, v in result.items() if k != "reason"},
        }
    logger.info(
        f"[note_collection] 账号{account_id} 笔记合集「{name}」已创建"
        f"(collection_id={result.get('collection_id')},"
        f"判据={result.get('confirmed_by')},载体={carrier_note_id})"
    )
    return result


def _create_sync(
    account_id: int,
    cookies: list[dict],
    name: str,
    description: Optional[str],
    carrier_note_id: str,
) -> dict[str, Any]:
    """同一线程内:建 SyncClient → start → 进载体笔记编辑器建合集 → stop(finally 防泄漏)。

    **不 block_images**:要在真实布局上点弹层与 modal 里的按钮,缺图会改变页面高度与坐标
    (同 note_components 的写路径)。载体笔记全程零提交。
    """
    client = SyncClient(account_id, cookies)
    try:
        start = client.start()
        if not start.get("success"):
            return {"status": "error",
                    "reason": f"browser_start_failed: {start.get('error')}"}
        return create_note_collection(
            client.page, account_id, carrier_note_id, name, description
        )
    except NoteComponentsError as exc:
        # 前置硬失败(载体笔记的更新页进不去):浏览器层用异常表达,这里转成结果 dict
        return {"status": "error", "reason": exc.reason}
    except Exception as exc:  # noqa: BLE001 — 转成结果 dict,由 execute 统一翻译
        return {"status": "error", "reason": f"note_collection_exception: {exc}"}
    finally:
        client.stop()
