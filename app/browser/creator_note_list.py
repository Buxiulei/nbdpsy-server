"""创作中心笔记列表抓取器(纯只读,吃已登录 page)。

设计 docs/design/2026-07-31-published-note-ledger-design.md 第三节(真号实验结论)。

**机制**:笔记管理页 ``/new/note-manager`` 加载时前端会调::

    GET .../api/galaxy/v2/creator/note/user/posted?tab=0&page=N
    → {code, success, msg, data: {notes: [...], tags, page}}

我们**被动读**这些响应(``page.on("response")``)拿 ``notes[i].id`` 等字段。三条硬约定:

- **不构造、不直调接口**:只挂响应监听,请求由页面自己发。翻页靠拟人化滚动
  (``SyncHumanActions.scroll``)让前端自己去要下一页,不改 URL 参数、不发 XHR。
- **零 JS 注入**:全程不点击、不输入、不 ``page.evaluate`` 改页面。笔记 DOM 里根本
  不暴露 note_id(真号实测:含 24 位 hex 的元素 0 个),这条路本就封死,只能读响应。
- **等待必须用 ``page.wait_for_timeout``**:playwright 同步 API 的事件是在调用
  playwright 时才被派发的,``time.sleep`` 期间监听器一个都不会触发,循环会空转到超时。

翻页终止条件(三选一,任一命中即停,防死循环):
1. 某批响应的 ``notes`` 为空列表 —— 页码已超界;
2. 滚动后 ``_NEXT_BATCH_TIMEOUT_S`` 内没有新的列表响应 —— 前端没有更多可加载;
3. 累计批数达 ``max_pages`` —— 兜底硬上限,命中即告警停止。
"""

import time
from typing import Any, Dict, List, Optional

from loguru import logger

# 复用导出器的 creator 域导航(容忍 SSO 重定向中断 + 等页面基本加载完成),
# 两处对 creator 域的预热语义必须一致,不另抄一份。
from app.browser.creator_export import _goto_creator
from app.browser.sync_human_actions import SyncHumanActions

_NOTE_MANAGER_URL = "https://creator.xiaohongshu.com/new/note-manager"
_PUBLISH_URL = "https://creator.xiaohongshu.com/publish/publish?source=official"

# 笔记列表接口的 URL 特征(只认这一个,其余响应一概不读)
_POSTED_API_MARK = "creator/note/user/posted"

# 首批响应:先按 fast-path 短等一次(cookie 双域已登录时秒回),没等到才做 SSO 预热重进
_FIRST_BATCH_FAST_S = 8.0
_FIRST_BATCH_TIMEOUT_S = 20.0
# 滚动后等下一批列表响应的时长;等不到即视为没有更多分页
_NEXT_BATCH_TIMEOUT_S = 8.0
# 批数硬上限(实测单号 61 篇分若干页,60 批远超真实需要,纯防死循环)
MAX_PAGES = 60


class CreatorNoteListError(Exception):
    """笔记列表抓取失败。``reason`` 携失败语义。"""

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


class _PostedCollector:
    """``page.on("response")`` 回调:只被动收笔记列表接口的响应体。

    响应体必须在回调里当场读:导航之后 body 就取不到了。任何解析异常只告警丢弃该批,
    绝不让监听器抛异常打断页面事件派发。
    """

    def __init__(self) -> None:
        self.batches: List[List[Dict[str, Any]]] = []

    def handle(self, response) -> None:
        try:
            url = response.url or ""
        except Exception:  # 响应对象已失效,读 url 都会炸
            return
        if _POSTED_API_MARK not in url:
            return
        try:
            body = response.json()
        except Exception as exc:  # noqa: BLE001 — 读不到 body 只丢这批,不打断抓取
            logger.warning(f"[creator_note_list] 列表响应体读取失败(忽略该批): {exc}")
            return
        notes = ((body or {}).get("data") or {}).get("notes")
        if not isinstance(notes, list):
            logger.warning(
                f"[creator_note_list] 列表响应无 data.notes 数组(忽略该批): "
                f"code={(body or {}).get('code')} msg={(body or {}).get('msg')}"
            )
            return
        self.batches.append(notes)


def _wait_for_new_batch(
    page, collector: _PostedCollector, seen: int, timeout_s: float
) -> Optional[List[Dict[str, Any]]]:
    """等到 batches 长度超过 seen,返回最新一批;超时返回 None。

    用 ``page.wait_for_timeout`` 而非 ``time.sleep``:同步 API 下只有调用 playwright
    才会派发 response 事件(见模块 docstring)。
    """
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if len(collector.batches) > seen:
            return collector.batches[-1]
        page.wait_for_timeout(300)
    # 超时后再查一次:响应可能正好在最后一次等待里到达,不能把已到手的一批判成超时
    return collector.batches[-1] if len(collector.batches) > seen else None


def _merge_batches(batches: List[List[Dict[str, Any]]]) -> List[Dict[str, Any]]:
    """按 note id 去重合并各批(保持首次出现顺序);无 id 的条目丢弃并告警。"""
    merged: List[Dict[str, Any]] = []
    seen_ids: set[str] = set()
    dropped = 0
    for batch in batches:
        for note in batch:
            note_id = ((note or {}).get("id") or "").strip()
            if not note_id:
                dropped += 1
                continue
            if note_id in seen_ids:
                continue
            seen_ids.add(note_id)
            merged.append(note)
    if dropped:
        logger.warning(f"[creator_note_list] {dropped} 条响应项无 id,已丢弃(无法入台账)")
    return merged


def fetch_posted_notes(
    page, account_id: int, max_pages: int = MAX_PAGES
) -> List[Dict[str, Any]]:
    """打开笔记管理页 → 拟人滚动翻页 → 返回全部笔记原始 dict(去重后)。

    Args:
        page: 已建好登录态的同步 Playwright Page(SyncClient.start 之后)。
        account_id: 账号 id(日志用)。
        max_pages: 批数硬上限,防前端异常导致无限翻页。

    Returns:
        接口返回的 notes 原始 dict 列表(字段不做改名/映射,解释权在服务层);
        该号一篇笔记都没有时返回 []。

    Raises:
        CreatorNoteListError: 始终没拿到列表接口响应(多半是 creator 域未登录)。
    """
    collector = _PostedCollector()
    page.on("response", collector.handle)
    try:
        human = SyncHumanActions(page)

        # Fast-path:cookie 双域已登录时直接进笔记管理页就能收到列表响应。
        _goto_creator(page, _NOTE_MANAGER_URL)
        first = _wait_for_new_batch(page, collector, 0, _FIRST_BATCH_FAST_S)
        if first is None:
            # 没响应多半是 creator 域 SSO 未建立(首访被重定向到登录页),预热后重进。
            logger.info(
                f"[creator_note_list] 账号{account_id}: 首访未收到列表响应,"
                f"走 publish_url 预热 SSO 后重进"
            )
            _goto_creator(page, _PUBLISH_URL)
            _goto_creator(page, _NOTE_MANAGER_URL)
            first = _wait_for_new_batch(
                page, collector, len(collector.batches), _FIRST_BATCH_TIMEOUT_S
            )
        if first is None:
            raise CreatorNoteListError(
                "no_posted_response: 笔记管理页始终未返回笔记列表接口响应"
                "(creator 域可能需重新扫码登录)"
            )
        if not first:
            logger.info(f"[creator_note_list] 账号{account_id}: 首批即空,该号无笔记")
            return []

        while len(collector.batches) < max_pages:
            seen = len(collector.batches)
            human.wait(0.8, 2.0, context="笔记列表浏览")
            human.scroll("down")
            batch = _wait_for_new_batch(page, collector, seen, _NEXT_BATCH_TIMEOUT_S)
            if batch is None:
                logger.info(
                    f"[creator_note_list] 账号{account_id}: 滚动后无新分页响应,遍历结束"
                    f"(共 {len(collector.batches)} 批)"
                )
                break
            if not batch:
                logger.info(
                    f"[creator_note_list] 账号{account_id}: 返回空列表,页码已超界,遍历结束"
                    f"(共 {len(collector.batches)} 批)"
                )
                break
        else:
            logger.warning(
                f"[creator_note_list] 账号{account_id}: 已达批数上限 {max_pages},"
                f"主动停止翻页(防死循环),结果可能不完整"
            )

        notes = _merge_batches(collector.batches)
        logger.info(
            f"[creator_note_list] 账号{account_id}: 抓到 {len(notes)} 篇笔记"
            f"({len(collector.batches)} 批响应)"
        )
        return notes
    finally:
        # 监听器必须摘掉:同一个 page 会被后续任务复用,留着会继续吃响应体
        try:
            page.remove_listener("response", collector.handle)
        except Exception:  # noqa: BLE001 — 摘监听失败不影响已抓到的结果
            logger.warning("[creator_note_list] 摘除 response 监听失败(忽略)")
