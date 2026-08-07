"""他人笔记评论抓取服务:REST 触发登记 + 契约 execute()(kind=``note_extract_comments``)。

分层与 ``note_comment`` / ``note_media`` 一致:浏览器动作在
``app.browser.note_comments_read``,本模块只管取 cookie、并发闸、任务登记与结果落缓存。

**这是整条提取链路上唯一消耗浏览器会话的一步**。正文/图片/互动数据纯 HTTP 就能拿
(见 ``app.services.note_extract``),只有评论必须让真实浏览器去拉。所以:

- 端点侧默认 ``with_comments`` 不为 0 就要显式给 ``account_id``,让运营每次都意识到
  "这一次要烧一个会话额度";
- 抓回来的评论**并进那篇笔记的 24h 缓存件**,同一篇再问一次直接回缓存,不再开会话;
- 用**水军号**(浏览他人笔记是水军号的正常行为),不要用品牌号 —— 这条无法在代码里
  硬判(账号表没有号型字段),写在 manifest 与本 docstring 里靠人守。

**幂等**(纯只读 + 结果按 note_id 覆盖写缓存),进 ``_IDEMPOTENT_KINDS``:僵死可自动重跑。
"""

import asyncio

from loguru import logger

from app.browser.account_locks import account_locks
from app.browser.browser_gate import browser_slot
from app.browser.login_detector import PAGE_TEXT_JS, classify_wall_text, is_wall_url
from app.browser.note_comments_read import read_note_comments
from app.browser.sync_client import SyncClient
from app.browser.sync_human_actions import SyncHumanActions
from app.core.db import get_session
from app.services import browser_jobs_repo, note_extract, risk_events
from app.services.cookie_check import load_account_cookies

JOB_KIND = "note_extract_comments"


def start_comments(account_id: int, payload: dict) -> str:
    """登记一次评论抓取,返回轮询 id。payload 见 ``execute``。"""
    job_id = browser_jobs_repo.enqueue_from_request(JOB_KIND, payload, account_id=account_id)
    browser_jobs_repo.spawn_inline(job_id, lambda: execute(account_id, payload))
    return job_id


async def execute(account_id: int, payload: dict) -> dict:
    """抓某篇笔记的前 N 条评论并并进缓存(契约函数,不碰 browser_jobs 台账)。

    payload:``{"note_id","note_url","max_count","note_author_user_id"?,"expected_total"?}``。

    Returns:
        ``{"note_id","comments":[...],"complete":bool,"stop_reason":str,"count":int}``;
        无 cookie / 撞墙 / 任何异常 → ``{"error": reason}``,**不抛出**。
    """
    payload = payload or {}
    note_url = (payload.get("note_url") or "").strip()
    note_id = (payload.get("note_id") or "").strip()
    if not note_url or not note_id:
        return {"error": "payload 缺 note_url / note_id,无法定位笔记"}
    try:
        cookies = await load_account_cookies(account_id)
        if not cookies:
            return {"error": f"账号 {account_id} 无可用 cookie,不开会话"}
        async with account_locks.get(account_id):
            async with browser_slot():
                result = await asyncio.to_thread(_fetch_sync, account_id, cookies, payload)
        if result.get("wall"):
            # 撞墙留痕:与 cookie_check / interaction_backfill 同一张 risk_events 台账,
            # 运营看板才能把"这个号今天撞了几次墙"看全(record_wall 自己不上抛)。
            await risk_events.record_wall(get_session, account_id, result["wall"], JOB_KIND)
        if "error" in result:
            return result
        _merge_into_cache(note_id, result)
        return {
            "note_id": note_id,
            "comments": result["comments"],
            "count": len(result["comments"]),
            "complete": result["complete"],
            "stop_reason": result["stop_reason"],
        }
    except Exception as exc:  # noqa: BLE001 — 收敛成结果,绝不上抛
        logger.exception(f"评论抓取异常 account_id={account_id} note_id={note_id}")
        return {"error": f"评论抓取异常:{exc}"}


def _merge_into_cache(note_id: str, result: dict) -> None:
    """把评论并进那篇的 24h 缓存件;没有缓存件就跳过(下次提取会重建)。"""
    cached = note_extract.cache_load(note_id)
    if cached is None:
        return
    note_extract.merge_comments(cached, result["comments"], result["complete"])
    note_extract.cache_store(note_id, cached)


def _fetch_sync(account_id: int, cookies: list[dict], payload: dict) -> dict:
    """线程内:起浏览器 → 进详情页 → 只读滚动抓评论 → 关。基础设施失败收敛成 error。

    **不拦图**:评论区靠正常渲染出来,拦图省的那点流量不值得冒"页面布局塌掉导致滚动
    落点算错"的险(同 ``note_media`` 的取舍)。
    """
    client = SyncClient(account_id, cookies)
    try:
        start = client.start()
        if not start.get("success"):
            return {"error": f"浏览器启动失败:{start.get('error')}"}
        page = client.page
        human = SyncHumanActions(page)
        human.navigate(payload["note_url"])
        human.wait(1.5, 3.0, context="详情页首屏浏览")

        if is_wall_url(page.url):
            return _wall_result(page, account_id, payload["note_url"], [])

        result = read_note_comments(
            page,
            human,
            max_count=int(payload.get("max_count") or 20),
            expected_total=payload.get("expected_total"),
            note_author_user_id=payload.get("note_author_user_id"),
        )
        if result.get("error") == "wall":
            # 滚动中途撞墙:已抓到的评论一并交出去(证据不丢),但整单判 error
            return _wall_result(page, account_id, payload["note_url"], result["comments"])
        return result
    except Exception as exc:  # noqa: BLE001
        return {"error": f"浏览器异常:{exc}"}
    finally:
        client.stop()


def _wall_result(page, account_id: int, target_url: str, partial: list) -> dict:
    """撞墙时的统一结果:URL 是硬判据,正文只用来分型(扫码 / 限流 / 未知)。"""
    try:
        text = page.evaluate(PAGE_TEXT_JS)  # 只读取证,不做任何交互
    except Exception:  # noqa: BLE001 — 取证自身绝不抛
        text = ""
    kind = classify_wall_text(text)
    logger.warning(f"[note_extract_comments] 账号{account_id} 撞风控墙({kind}),已停手")
    return {
        "error": (
            f"wall_{kind}: 账号 {account_id} 撞上风控验证墙,本次未抓全评论,请先处理该号"
        ),
        "partial_comments": partial,
        "wall": {
            "wall_type": kind,
            "target_url": target_url,
            "landed_url": getattr(page, "url", None),
            "page_text": text,
        },
    }
