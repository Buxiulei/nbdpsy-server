"""笔记三组件(合集 / 引用笔记 / 关联活动)服务:契约 execute() + 目录只读查询。

设计 docs/design/2026-08-01-note-components-design.md 第三节。分层与 ``note_visibility`` /
``note_comment`` 一致(浏览器动作在 ``app.browser.note_components``,本模块只管取数、
并发闸与任务登记):

- ``start_components``:REST 请求路径登记一条 ``browser_jobs``(kind=``note_components``),
  返回对外轮询 id;``NBDPSY_ROLE=all`` 时顺带派进程内执行。
- ``execute()``:契约执行函数(account_worker 子进程消费)——持号锁串行 → 浏览器闸 →
  线程内跑同步设置,**不碰 browser_jobs 台账**(claim/finish 由调用方);任何异常收敛成
  ``{"error": reason}``,**绝不抛出**。
- ``read_catalog()``:同步读该号可用的合集 / 活动列表(REST 的两个 GET 端点用)。
  它也要起一个真浏览器会话(这两个列表只有编辑器页会去要),故同样过号锁与浏览器闸。

``note_components`` **非幂等**,不在 ``browser_jobs_repo._IDEMPOTENT_KINDS`` 里:

- 提交是**全量覆盖语义**的一次真发布,重跑就是再覆盖一次;
- 关联活动会往正文**追加**话题(设计 2.7①),重跑会把同一个话题再拼一遍。

僵死任务置 error + 结果未知指引,绝不自动重跑;调用方看到失败必须先去笔记里核对当前
状态再决定,不能盲目重试。
"""

import asyncio

from loguru import logger
from sqlalchemy import select

from app.browser.account_locks import account_locks
from app.browser.browser_gate import browser_slot
from app.browser.note_components import (
    NoteComponentsError,
    read_catalog as browser_read_catalog,
    set_note_components,
)
from app.browser.sync_client import SyncClient
from app.core.db import get_session
from app.models.published_note import PublishedNote
from app.services import browser_jobs_repo
from app.services.cookie_check import load_account_cookies

# 三个组件字段名(REST 请求体 / payload / publish_jobs 列名三处同名,别再各起一套)
COMPONENT_FIELDS = ("collection_id", "quoted_note_id", "activity_id")


def start_components(
    account_id: int,
    note_id: str,
    collection_id: str | None,
    quoted_note_id: str | None,
    activity_id: str | None,
    related_counselor: str | None = None,
) -> str:
    """REST 触发一次三组件设置;登记 browser_jobs 台账,返回轮询 id。

    ``quoted_note_id`` 到这里已经是**推导完的最终值**(REST 层按 counselor_quote 的规则
    定下来);``related_counselor`` 只随 payload 存个由来,执行链路不读它 —— 这是次
    非幂等的全量覆盖提交,事后查"当时为什么引了这篇"必须有据可查。
    """
    payload = {
        "note_id": note_id,
        "collection_id": collection_id,
        "quoted_note_id": quoted_note_id,
        "activity_id": activity_id,
        "related_counselor": related_counselor,
    }
    job_id = browser_jobs_repo.enqueue_from_request(
        "note_components", payload, account_id=account_id
    )
    browser_jobs_repo.spawn_inline(job_id, lambda: execute(account_id, payload))
    return job_id


async def execute(account_id: int, payload: dict) -> dict:
    """执行一次三组件设置(契约函数,不碰 browser_jobs 台账)。

    成功返回 ``{"status": "done"|"partially_applied", "applied": {...}, ...}``
    (形状见 ``app.browser.note_components.set_note_components``);一项都没生效时结果里
    自带 ``error`` 键 → 台账落 error。入参不合法 / 前置失败 / 任何异常 →
    ``{"error": reason}``,**不抛出**。
    """
    payload = payload or {}
    note_id = str(payload.get("note_id") or "").strip()
    if not note_id:
        return {"error": "payload 缺 note_id,无法定位要编辑的笔记"}
    components = {
        field: (str(payload.get(field)).strip() if payload.get(field) else None)
        for field in COMPONENT_FIELDS
    }
    if not any(components.values()):
        return {
            "error": "no_component_requested: collection_id / quoted_note_id / "
                     "activity_id 至少要给一个,否则这次编辑什么都不会改"
        }

    try:
        cookies = await load_account_cookies(account_id)
        if not cookies:
            return {"error": "账号无可用 cookie,跳过三组件设置"}
        # 与发布/cookie 检测共用同一把 per-account 锁:同号浏览器操作串行,避免 kill_orphans 互杀。
        async with account_locks.get(account_id):
            # 全局浏览器并发闸:封顶总 camoufox 数,超出排队。
            async with browser_slot():
                return await asyncio.to_thread(
                    _apply_sync, account_id, cookies, note_id, components
                )
    except NoteComponentsError as exc:
        # 前置/硬失败(页面进不去 / 权限读不出 / 提交前权限已变):这几种都没点过发布
        logger.warning(
            f"三组件设置失败 account_id={account_id} note_id={note_id} reason={exc.reason}"
        )
        return {"error": exc.reason}
    except Exception as exc:  # 兜底:异常也要给终态结果,别让台账悬挂
        logger.exception(f"三组件设置任务异常 account_id={account_id} note_id={note_id}")
        return {"error": f"三组件设置任务异常:{exc}"}


def _apply_sync(
    account_id: int, cookies: list[dict], note_id: str, components: dict
) -> dict:
    """同一线程内:建 SyncClient → start → 设置三组件 → stop 收尾(finally 防泄漏)。

    headed 真屏沿用 SyncClient 默认(headless=False);**不 block_images** —— 发布按钮靠
    截图找「小红书红」质心,拦图会改变页面布局与配色分布。
    """
    client = SyncClient(account_id, cookies)
    try:
        start = client.start()
        if not start.get("success"):
            raise NoteComponentsError(f"browser_start_failed: {start.get('error')}")
        return set_note_components(client.page, account_id, note_id, **components)
    finally:
        client.stop()


# ---------------- 目录只读查询(合集 / 活动列表) ----------------


async def read_catalog(account_id: int, note_id: str) -> dict:
    """读该号可用的合集 + 活动列表(全程只读,不改任何东西)。

    ``note_id`` 是**打开哪篇笔记的编辑器**——这两个列表只有编辑器页会去要,没有独立的
    只读页面。打开更新页不提交就不落库,故对该笔记无副作用。

    成功返回 ``{"collections": [...], "activities": [...]}``;失败返回 ``{"error": reason}``
    (**不抛出**,REST 层据此给 502/400 之外的可读错误)。
    """
    note_id = str(note_id or "").strip()
    if not note_id:
        return {"error": "缺 note_id:合集/活动列表只有笔记编辑器页会返回,得先有一篇笔记"}
    try:
        cookies = await load_account_cookies(account_id)
        if not cookies:
            return {"error": "账号无可用 cookie,读不到合集/活动列表"}
        async with account_locks.get(account_id):
            async with browser_slot():
                return await asyncio.to_thread(
                    _catalog_sync, account_id, cookies, note_id
                )
    except NoteComponentsError as exc:
        logger.warning(f"目录读取失败 account_id={account_id} reason={exc.reason}")
        return {"error": exc.reason}
    except Exception as exc:  # noqa: BLE001 — 只读查询失败也不上抛,给可读错误
        logger.exception(f"目录读取异常 account_id={account_id}")
        return {"error": f"目录读取异常:{exc}"}


def _catalog_sync(account_id: int, cookies: list[dict], note_id: str) -> dict:
    """同一线程内:建 SyncClient → start → 只读收集目录 → stop 收尾。"""
    client = SyncClient(account_id, cookies)
    try:
        start = client.start()
        if not start.get("success"):
            raise NoteComponentsError(f"browser_start_failed: {start.get('error')}")
        return browser_read_catalog(client.page, account_id, note_id)
    finally:
        client.stop()


async def default_catalog_note_id(account_id: int) -> str | None:
    """挑一篇该号**最近发布且已有 note_id** 的笔记,当作打开编辑器的落脚点。

    调用方没指定 note_id 时用它兜底:目录查询本身与具体哪篇笔记无关(打开谁的编辑器,
    合集与活动列表都一样),但总得打开一篇。台账里 ``sync_status=pending_id`` 的行还没
    补到 note_id,天然被 ``is_not(None)`` 排除。
    """
    async with get_session() as session:
        return await session.scalar(
            select(PublishedNote.note_id)
            .where(
                PublishedNote.account_id == account_id,
                PublishedNote.note_id.is_not(None),
            )
            .order_by(PublishedNote.published_at.desc(), PublishedNote.id.desc())
            .limit(1)
        )
