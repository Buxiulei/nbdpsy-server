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
from datetime import datetime

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

# 组件字段名(REST 请求体 / payload / publish_jobs 列名三处同名,别再各起一套)。
# ``remove_collection_id`` 是 2026-08-07 新增的**移出**合集(与加入对称,幂等),
# 它与 ``collection_id`` 在 REST 层互斥(同传 422),这里只负责同名直传。
COMPONENT_FIELDS = (
    "collection_id", "remove_collection_id", "quoted_note_id", "activity_id",
)
# 已发布笔记编辑字段名(设计 docs/design/2026-08-03-note-editing-design.md 3.1);
# payload 里有任一非空值即"这次请求要改内容,不只是挂组件"。
EDIT_FIELDS = (
    "title", "content", "add_images", "remove_image_indexes", "expected_image_count",
)


def start_components(
    account_id: int,
    note_id: str,
    collection_id: str | None,
    quoted_note_id: str | None,
    activity_id: str | None,
    related_counselor: str | None = None,
    edits: dict | None = None,
    collection_name: str | None = None,
    remove_collection_id: str | None = None,
    remove_collection_name: str | None = None,
    set_original_declaration: bool = False,
) -> str:
    """REST 触发一次三组件设置 / 笔记编辑;登记 browser_jobs 台账,返回轮询 id。

    ``quoted_note_id`` 到这里已经是**推导完的最终值**(REST 层按 counselor_quote 的规则
    定下来);``related_counselor`` 只随 payload 存个由来,执行链路不读它 —— 这是次
    非幂等的全量覆盖提交,事后查"当时为什么引了这篇"必须有据可查。

    ``edits`` 是 ``NoteComponentsRequest.note_edits()`` 的产物(``add_images`` 已在 REST
    层落成本地路径),没有编辑意图时为 ``None`` —— 此时 payload 与老版本逐字节一致。
    """
    payload = {
        "note_id": note_id,
        "collection_id": collection_id,
        "collection_name": collection_name,
        "remove_collection_id": remove_collection_id,
        "remove_collection_name": remove_collection_name,
        "quoted_note_id": quoted_note_id,
        "activity_id": activity_id,
        "related_counselor": related_counselor,
        "set_original_declaration": set_original_declaration,
    }
    if edits:
        payload.update(edits)
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

    payload 里的编辑字段(``EDIT_FIELDS``)按同名参数直传浏览器层;标题/正文回读确认
    生效后追加台账回写(见 ``_sync_ledger``)。
    """
    payload = payload or {}
    note_id = str(payload.get("note_id") or "").strip()
    if not note_id:
        return {"error": "payload 缺 note_id,无法定位要编辑的笔记"}
    components = {
        field: (str(payload.get(field)).strip() if payload.get(field) else None)
        for field in COMPONENT_FIELDS
    }
    # collection_name 是**辅助**字段(已选态下确认"已选的就是目标"用,见 _set_collection
    # 2026-08-04 注释),不进 COMPONENT_FIELDS —— 它单独出现不构成一次组件请求,
    # 也不参与"至少给一个"的判定。只在真的请求了 collection_id 时才透传。
    collection_name = (
        str(payload.get("collection_name")).strip()
        if payload.get("collection_name") and components.get("collection_id")
        else None
    )
    # 移出侧同款辅助字段,且**比加入侧更要紧**:名字比对不上时浏览器层会拒绝点 ×
    # (移出是破坏性操作,点错等于把笔记从正确的合集里摘出来)。
    remove_collection_name = (
        str(payload.get("remove_collection_name")).strip()
        if payload.get("remove_collection_name") and components.get("remove_collection_id")
        else None
    )
    # 编辑字段**同名直传**给浏览器层(``set_note_components`` 的可选参数与这里同名)。
    # 用 ``is not None`` 而不是真值判断:``title=""`` 是"清空标题"这一合法意图(设计 3.1),
    # 真值判断会把它当成"没请求"静默丢掉。纯组件 payload 不含这些键 → ``edits`` 为空,
    # 调用形态与老版本逐字节一致。
    edits = {
        field: payload[field]
        for field in EDIT_FIELDS
        if payload.get(field) is not None
    }
    # 原创声明补录:纯布尔开关(只开不关),不进 COMPONENT_FIELDS —— 那一族是 id 字符串。
    set_original_declaration = bool(payload.get("set_original_declaration"))
    if not any(components.values()) and not edits and not set_original_declaration:
        return {
            "error": "no_component_requested: collection_id / remove_collection_id / "
                     "quoted_note_id / activity_id / set_original_declaration / "
                     "编辑字段(title / content / 图片增删)"
                     "至少要给一个,否则这次编辑什么都不会改"
        }

    try:
        cookies = await load_account_cookies(account_id)
        if not cookies:
            return {"error": "账号无可用 cookie,跳过三组件设置"}
        # 与发布/cookie 检测共用同一把 per-account 锁:同号浏览器操作串行,避免 kill_orphans 互杀。
        async with account_locks.get(account_id):
            # 全局浏览器并发闸:封顶总 camoufox 数,超出排队。
            async with browser_slot():
                result = await asyncio.to_thread(
                    _apply_sync, account_id, cookies, note_id, components, edits,
                    collection_name, remove_collection_name,
                    set_original_declaration,
                )
        # 台账回写在**闸外**做:浏览器已经收工,这是一次纯 DB 写,没理由继续占着并发名额。
        return await _sync_ledger(account_id, note_id, result)
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
    account_id: int,
    cookies: list[dict],
    note_id: str,
    components: dict,
    edits: dict | None = None,
    collection_name: str | None = None,
    remove_collection_name: str | None = None,
    set_original_declaration: bool = False,
) -> dict:
    """同一线程内:建 SyncClient → start → 设置三组件/编辑 → stop 收尾(finally 防泄漏)。

    headed 真屏沿用 SyncClient 默认(headless=False);**不 block_images** —— 发布按钮靠
    截图找「小红书红」质心,拦图会改变页面布局与配色分布。
    """
    client = SyncClient(account_id, cookies)
    try:
        start = client.start()
        if not start.get("success"):
            raise NoteComponentsError(f"browser_start_failed: {start.get('error')}")
        return set_note_components(
            client.page, account_id, note_id, **components,
            collection_name=collection_name,
            remove_collection_name=remove_collection_name,
            set_original_declaration=set_original_declaration, **(edits or {})
        )
    finally:
        client.stop()


async def _sync_ledger(account_id: int, note_id: str, result: dict) -> dict:
    """标题/正文回读确认生效时把平台真值写回台账,结果里带 ``ledger_synced``(设计 3.3)。

    没有文本编辑项(纯组件 / 只改图片 / 文本没成)时**不开 session、不带该键** —— 与
    MANIFEST 口径一致:这个键出现就代表"这次确实有该写台账的东西"。

    回写失败不改变 job 终态(``write_back_ledger`` 自己已经把异常收敛成 ``False``),
    所以这里**不再包一层 try**:再吞一次只会把它的日志与返回值一起埋掉。
    """
    applied = (result or {}).get("applied") or {}
    if not (applied.get("title") is True or applied.get("content") is True):
        return result
    async with get_session() as session:
        result["ledger_synced"] = await write_back_ledger(
            session, account_id, note_id, applied, result.get("read_back") or {}
        )
    return result


# ---------------- 台账回写(标题 / 正文,设计 3.3) ----------------


async def write_back_ledger(
    session, account_id: int, note_id: str, applied: dict, read_back: dict
) -> bool:
    """标题/正文改成功后,把**回读到的平台真值**写回 published_notes;返回 ledger_synced。

    Args:
        session: 调用方给的 AsyncSession(本函数负责 commit)。
        account_id / note_id: 定位台账行(唯一键就是这两个)。
        applied: 浏览器层逐项回读结论,只认 ``applied["title"] is True`` /
            ``applied["content"] is True`` —— **三态里只有 True 算数**,False/None 一律
            不回写(台账写错比没写更坏:后续所有引用都会拿到假值)。
        read_back: 提交后回读的真值 ``{"title": ..., "content": ...}``。

    Returns:
        ``True`` = 该写的都写成了(**没什么可写时也是 True**:不存在"该写而没写成"的项);
        ``False`` = 有该写的却没写成(台账行不存在 / 回读值缺失 / 落库异常)。回写失败
        **不改变 job 结果**——平台侧改动已经生效并回读确认过,台账最终一致由 note 同步
        链路兜底,为一次留痕把任务判成 error 是误报(与 note_visibility._record_change 同款取舍)。

    图片增删不回写:台账不存图列表,没有可写的字段。
    """
    applied = applied or {}
    read_back = read_back or {}
    want_title = applied.get("title") is True
    want_content = applied.get("content") is True
    if not (want_title or want_content):
        return True

    # 回读值缺失说明浏览器层没把真值带回来,此时宁可不写:回写的意义就在于"用平台真值
    # 覆盖我们自己的旧认知",拿请求值凑数等于把台账写成我们**以为**的样子。
    if want_title and read_back.get("title") is None:
        logger.warning(
            f"[note_components] 台账回写跳过 note_id={note_id}:applied.title=True 但"
            f"回读值缺失,不拿请求值凑数"
        )
        return False
    if want_content and read_back.get("content") is None:
        logger.warning(
            f"[note_components] 台账回写跳过 note_id={note_id}:applied.content=True 但"
            f"回读值缺失,不拿请求值凑数"
        )
        return False

    try:
        row = await session.scalar(
            select(PublishedNote).where(
                PublishedNote.account_id == account_id,
                PublishedNote.note_id == note_id,
            )
        )
        if row is None:
            # 建台账行是 note_ledger 的职责,这里凭空造一行会绕过它那套
            # pending/linked/orphan 判定(与 note_visibility 留痕同款纪律)。
            logger.warning(
                f"[note_components] 台账无 note_id={note_id} 的行,标题/正文回写跳过;"
                f"下次台账同步会补上"
            )
            return False
        if want_title:
            row.title = read_back["title"]
        if want_content:
            row.content_text = read_back["content"]
            row.content_fetched_at = datetime.utcnow()
        await session.commit()
    except Exception as exc:  # noqa: BLE001 — 回写失败不吞:只记日志 + 返回 False
        logger.warning(
            f"[note_components] 台账回写失败 note_id={note_id}"
            f"(平台侧改动已生效,不影响任务终态): {exc}"
        )
        return False
    logger.info(
        f"[note_components] 台账回写完成 account_id={account_id} note_id={note_id} "
        f"title={'是' if want_title else '否'} content={'是' if want_content else '否'}"
    )
    return True


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
