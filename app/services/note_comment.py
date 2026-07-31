"""单篇评论服务:REST 触发登记 + 契约 execute()。

2026-07-31 评论从矩阵互动三件套里拆出来独立成一条能力。分层与 ``note_visibility`` /
``matrix_interact`` 一致(浏览器动作在 ``app.browser.matrix_interact.comment_on_note``,
本模块只管取数、并发闸与任务登记):

- ``start_comment``:REST 请求路径登记一条 ``browser_jobs``(kind=``note_comment``),
  返回对外轮询 id;``NBDPSY_ROLE=all`` 时顺带派进程内执行(与 ``note_delete`` 同款)。
- ``execute()`` 为契约执行函数(account_worker 子进程消费):持号锁串行 → 浏览器闸 →
  线程内跑同步评论,**不碰 browser_jobs 台账**(claim/finish 由调用方);任何异常收敛成
  ``{"error": reason}``,**绝不抛出**。
- payload:``{"publisher_user_id","title","text"}``,三者皆必填。``publisher_user_id``
  用于进发布者主页,``title`` 精确匹配定位笔记(与矩阵互动同一套定位),``text`` 是评论文案。

``note_comment`` **非幂等**:重复执行会**再发一条一模一样的评论**(不像点赞那样是开关,
评论是追加)。故不在 ``browser_jobs_repo._IDEMPOTENT_KINDS`` 里——僵死任务置 error +
结果未知指引,绝不自动重跑;调用方看到失败也必须先去笔记下核对评论到底发出去没有,
不能盲目重试,否则轻则刷屏重则触发平台风控。
"""

import asyncio

from loguru import logger

from app.browser.account_locks import account_locks
from app.browser.browser_gate import browser_slot
from app.browser.matrix_interact import MatrixInteractError, comment_on_note
from app.browser.sync_client import SyncClient
from app.services import browser_jobs_repo
from app.services.cookie_check import load_account_cookies


def start_comment(
    account_id: int, publisher_user_id: str, title: str, text: str
) -> str:
    """REST 触发一次单篇评论;登记 browser_jobs 台账,返回轮询 id。

    文案必填由入口(REST 请求体)把关,``execute`` 与 ``_do_comment`` 各自另有兜底校验。
    """
    payload = {
        "publisher_user_id": publisher_user_id,
        "title": title,
        "text": text,
    }
    comment_id = browser_jobs_repo.enqueue_from_request(
        "note_comment", payload, account_id=account_id
    )
    browser_jobs_repo.spawn_inline(comment_id, lambda: execute(account_id, payload))
    return comment_id


async def execute(account_id: int, payload: dict) -> dict:
    """执行一次单篇评论(契约函数,不碰 browser_jobs 台账)。

    成功返回 ``{"note_url","commented":True}``;入参不合法 / 定位失败 / 评论未发出 /
    任何异常 → ``{"error": reason}``,**不抛出**。
    """
    payload = payload or {}
    publisher_user_id = (payload.get("publisher_user_id") or "").strip()
    title = (payload.get("title") or "").strip()
    text = (payload.get("text") or "").strip()
    if not publisher_user_id or not title:
        return {"error": "payload 缺 publisher_user_id / title,无法定位目标笔记"}
    if not text:
        # 评论没文案就没有可执行的动作,直接判入参错误,不白起一次浏览器会话
        return {"error": "comment_text_empty: payload 缺 text,没有可发的评论文案"}
    try:
        cookies = await load_account_cookies(account_id)
        if not cookies:
            return {"error": "账号无可用 cookie,跳过评论"}
        # 与发布/cookie 检测共用同一把 per-account 锁:同号浏览器操作串行,避免 kill_orphans 互杀。
        async with account_locks.get(account_id):
            # 全局浏览器并发闸:封顶总 camoufox 数,超出排队。
            async with browser_slot():
                return await asyncio.to_thread(
                    _comment_sync, account_id, cookies, publisher_user_id, title, text
                )
    except MatrixInteractError as exc:
        # 定位类语义失败(笔记没找到 / 详情没打开):记 error,不重跑
        logger.warning(
            f"评论失败 account_id={account_id} title={title!r} reason={exc.reason}"
        )
        return {"error": exc.reason}
    except Exception as exc:  # 兜底:异常也要给终态结果,别让台账悬挂
        logger.exception(f"评论任务异常 account_id={account_id}")
        return {"error": f"评论任务异常:{exc}"}


def _comment_sync(
    account_id: int,
    cookies: list[dict],
    publisher_user_id: str,
    title: str,
    text: str,
) -> dict:
    """同一线程内:建 SyncClient → start → 评论 → stop 收尾(finally 防泄漏 camoufox)。

    headed 真屏沿用 SyncClient 默认(headless=False);要点真实卡片与评论入口,
    不 block_images(缺封面会影响卡片布局与坐标,同 matrix_interact)。
    """
    client = SyncClient(account_id, cookies)
    try:
        start = client.start()
        if not start.get("success"):
            raise MatrixInteractError(f"browser_start_failed: {start.get('error')}")
        return comment_on_note(client.page, account_id, publisher_user_id, title, text)
    finally:
        client.stop()
