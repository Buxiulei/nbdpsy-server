"""播客合集创建服务:契约 ``execute()``(browser_jobs kind=``podcast_collection_create``)。

分层与 ``note_visibility`` / ``draft_clean`` 一致(浏览器动作在 ``app.browser.podcast``,
本模块只管取数、并发闸与登记):

- ``start_create()``:REST 触发,登记 browser_jobs 台账并返回轮询 id;
- ``execute()``:契约执行函数(account_worker 子进程消费),持号锁串行 → 浏览器闸 →
  线程内跑同步创建,**不碰 browser_jobs 台账**(claim/finish 由调用方);任何异常收敛成
  ``{"error": reason}``,**绝不抛出**。

**非幂等**,故意不进 ``browser_jobs_repo._IDEMPOTENT_KINDS``:合集创建是账号状态的写操作,
僵死任务自动重跑会建出第二个同名合集(平台侧会不会去重未取证),而"多一个空合集"要人工
去平台删。与 ``note_visibility`` / ``note_delete`` 同款纪律 —— 僵死后落 ``unknown``,由人
核对再决定。
"""

import asyncio
from typing import Any, Optional

from loguru import logger

from app.browser.account_locks import account_locks
from app.browser.browser_gate import browser_slot
from app.browser.podcast import create_collection
from app.browser.sync_client import SyncClient
from app.browser.sync_human_actions import SyncHumanActions
from app.services import browser_jobs_repo
from app.services.cookie_check import load_account_cookies

KIND = "podcast_collection_create"

# 创作中心发布页(合集入口在「发播客」tab 里,与"有没有传音频"完全解耦)
_PUBLISH_URL = "https://creator.xiaohongshu.com/publish/publish?source=official"


def start_create(
    account_id: int, name: str, description: Optional[str], cover_path: str
) -> str:
    """REST 触发一次播客合集创建;登记 browser_jobs 台账,返回轮询 job_id。

    入参合法性(名称 ≤20 字 / 简介 ≤100 字 / 封面格式与体积)由 REST 层把关,
    本函数只负责登记 —— 与 ``note_visibility.start_change`` 同款分工。
    """
    payload = {"name": name, "description": description, "cover": cover_path}
    job_id = browser_jobs_repo.enqueue_from_request(KIND, payload, account_id=account_id)
    browser_jobs_repo.spawn_inline(job_id, lambda: execute(account_id, payload))
    return job_id


async def execute(account_id: int, payload: dict) -> dict:
    """执行一次播客合集创建(契约函数,不碰 browser_jobs 台账)。

    成功返回 ``{"status":"done","name","collection_id":str|None, ...}``;
    入参不合法 / 创建失败 / 任何异常 → ``{"error": reason}``,**不抛出**。
    """
    payload = payload or {}
    name = str(payload.get("name") or "").strip()
    description = str(payload.get("description") or "").strip() or None
    cover_path = str(payload.get("cover") or "").strip()
    if not name:
        return {"error": "payload 缺 name,合集名称是平台必填项"}
    if not cover_path:
        return {"error": "payload 缺 cover,合集封面是平台必填项"}

    try:
        cookies = await load_account_cookies(account_id)
        if not cookies:
            return {"error": "该账号未接入 cookie,无法打开创作中心"}
        # 与发布/cookie 检测共用同一把 per-account 锁:同号浏览器操作串行,
        # 避免 profile_guard.kill_orphans 互杀。
        async with account_locks.get(account_id):
            async with browser_slot():
                result = await asyncio.to_thread(
                    _create_sync, account_id, cookies, name, description, cover_path
                )
    except Exception as exc:  # noqa: BLE001 — 兜底也要给终态,别让台账悬挂
        logger.exception(f"播客合集创建任务异常 account_id={account_id} name={name}")
        return {"error": f"播客合集创建任务异常:{exc}"}

    if result.get("status") != "done":
        # 台账的终态判据是有没有 "error" 键:浏览器层的 error 要翻译过来,
        # 否则一次失败的创建会以 done 收尾(静默假成功)。
        return {
            "error": result.get("reason") or "播客合集创建失败(浏览器层未给原因)",
            **{k: v for k, v in result.items() if k != "reason"},
        }
    logger.info(
        f"[podcast_collection] 账号{account_id} 合集「{name}」已创建"
        f"(collection_id={result.get('collection_id')},"
        f"判据={result.get('confirmed_by')})"
    )
    return result


def _create_sync(
    account_id: int,
    cookies: list[dict],
    name: str,
    description: Optional[str],
    cover_path: str,
) -> dict[str, Any]:
    """同一线程内:建 SyncClient → start → 开发布页 → 建合集 → stop 收尾(finally 防泄漏)。

    **不 block_images**:要点真实按钮、看封面裁剪弹窗,缺图会影响布局与坐标(同 note_delete)。
    也**不拦主站登录态**:creator 会话可能仍活,以合集入口的实际可达性为准(browser 层判)。
    """
    client = SyncClient(account_id, cookies)
    try:
        start = client.start()
        if not start.get("success"):
            return {"status": "error",
                    "reason": f"browser_start_failed: {start.get('error')}"}
        client.page.goto(_PUBLISH_URL, wait_until="domcontentloaded", timeout=60000)
        human = SyncHumanActions(client.page)
        human.wait(1.5, 2.5, context="等创作中心发布页渲染")
        return create_collection(client.page, human, name, description, cover_path)
    except Exception as exc:  # noqa: BLE001 — 转成结果 dict,由 execute 统一翻译
        return {"status": "error", "reason": f"podcast_collection_exception: {exc}"}
    finally:
        client.stop()
