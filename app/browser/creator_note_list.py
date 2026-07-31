"""创作中心笔记列表抓取器(纯只读,吃已登录 page)。

设计 docs/design/2026-07-31-published-note-ledger-design.md 第三节(真号实验结论)。

**机制**:笔记管理页 ``/new/note-manager`` 加载时前端会调::

    GET .../api/galaxy/v2/creator/note/user/posted?tab=0&page=N
    → {code, success, msg, data: {notes: [...], tags, page}}

我们**被动读**这些响应(``page.on("response")``)拿 ``notes[i].id`` 等字段。三条硬约定:

- **不构造、不直调接口**:只挂响应监听,请求由页面自己发。翻页靠拟人化滚动
  (``SyncHumanActions.scroll``)让前端自己去要下一页,不改 URL 参数、不发 XHR。
- **滚动前必须先把鼠标移到笔记列表上**(见 ``_hover_note_list``)。``page.mouse.wheel``
  把滚轮事件投在**鼠标当前位置**,而鼠标初始停在 (0,0) —— 真号实测那里是不滚动的顶栏
  ``div.d-topbar``(祖先 ``.main-page-container`` 是 ``overflow:hidden``),滚轮事件因此
  全部空转。列表真正的滚动容器是 ``div.content``(``overflow-y:scroll``),窗口本身根本
  不滚(``document.scrollHeight == clientHeight``),所以连"页面往下走了一点"都不会发生。
- **零 JS 注入**:全程不点击、不输入、不 ``page.evaluate``。笔记 DOM 里根本
  不暴露 note_id(真号实测:含 24 位 hex 的元素 0 个),这条路本就封死,只能读响应。
  定位滚动区也不用 evaluate —— 拿第一张 ``.note-card`` 的矩形即可,它就在滚动容器里。
- **等待必须用 ``page.wait_for_timeout``**:playwright 同步 API 的事件是在调用
  playwright 时才被派发的,``time.sleep`` 期间监听器一个都不会触发,循环会空转到超时。

翻页终止条件(三选一,任一命中即停,防死循环):
1. 某批响应的 ``notes`` 为空列表 —— 页码已超界;
2. 已抓够接口自报的总数,且滚动后 ``_NEXT_BATCH_TIMEOUT_S`` 内没有新的列表响应;
   总数未知或还没抓够时,要**连续** ``_EMPTY_SCROLL_RETRIES`` 次滚动都没响应才认定到底;
3. 累计批数达 ``max_pages`` —— 兜底硬上限,命中即告警停止。

**"滚了没响应"不等于到底**:真号实测中间就有一次滚动只挪了 260px 没触发请求,下一次滚动
才发出 ``page=3``。故除非已抓够期望总数,单次无响应一律重试。

**期望总数校验**:响应里 ``data.tags[0].notes_count`` 是该号笔记总数(真号实测 37)。
抓完仍不足即**告警**(``fetch_posted_notes`` 不为此抛错——半份列表照样能刷已有台账行,
但绝不静默当成"这号就这么多篇")。最后一页的 ``data.page`` 实测为 ``-1``(不足 10 条),
但该约定未见文档,故不拿它当终止条件,只作观察。
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

# 笔记卡片:只用来取滚轮落点的矩形(它在滚动容器 div.content 里),不做任何交互
_NOTE_CARD = ".note-card"

# 首批响应:先按 fast-path 短等一次(cookie 双域已登录时秒回),没等到才做 SSO 预热重进
_FIRST_BATCH_FAST_S = 8.0
_FIRST_BATCH_TIMEOUT_S = 20.0
# 滚动后等下一批列表响应的时长;等不到即视为没有更多分页
_NEXT_BATCH_TIMEOUT_S = 8.0
# 批数硬上限(实测单号 61 篇分若干页,60 批远超真实需要,纯防死循环)
MAX_PAGES = 60
# 还没抓够期望总数时,连续几次滚动都不触发新分页才认定到底(实测下拉偶发不触发请求)
_EMPTY_SCROLL_RETRIES = 3


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
        # 接口自报的笔记总数(data.tags[0].notes_count);读不到就是 None = 期望未知
        self.expected_total: Optional[int] = None

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
        self._take_expected_total(body)

    def _take_expected_total(self, body: dict) -> None:
        """从 ``data.tags[0].notes_count`` 记下期望总数(只取第一次读到的,读不到就留 None)。

        ``data.tags`` 实测只有一项「所有笔记」,不分可见性维度,故 ``tags[0]`` 就是全量口径。
        """
        if self.expected_total is not None:
            return
        tags = ((body or {}).get("data") or {}).get("tags")
        if not isinstance(tags, list) or not tags:
            return
        count = (tags[0] or {}).get("notes_count")
        if isinstance(count, int) and not isinstance(count, bool) and count >= 0:
            self.expected_total = count


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


def _hover_note_list(page, human: SyncHumanActions, account_id: int) -> bool:
    """把鼠标移到笔记列表上,让后续滚轮事件落进真正的滚动容器;移不过去返回 False。

    **这是翻页能不能继续的前提**:``page.mouse.wheel`` 投在鼠标当前位置,而鼠标从未移动过
    时停在 (0,0)——真号实测那里是不滚动的顶栏,滚轮全部空转,``div.content.scrollTop``
    三次滚动后仍是 0、一个分页请求都不发。悬停到列表上之后,同样的 ``human.scroll``
    立刻把 scrollTop 推到 450 → 710 → 1065,``page=2`` / ``page=3`` 应声而来。

    只取第一张 ``.note-card`` 的矩形当落点(它就在滚动容器 ``div.content`` 里),
    不用 ``page.evaluate`` 找容器 —— 保持本模块"零 JS 注入"的纪律。
    """
    try:
        card = page.query_selector(_NOTE_CARD)
        box = card.bounding_box() if card is not None else None
    except Exception as exc:  # noqa: BLE001 — 定位失败只降级,不打断抓取
        logger.warning(f"[creator_note_list] 账号{account_id}: 读笔记卡矩形失败: {exc}")
        box = None
    if not box:
        logger.warning(
            f"[creator_note_list] 账号{account_id}: 找不到笔记卡,鼠标无法移到列表上"
            f"——滚轮会打在不滚动的顶栏上,翻页多半停在首屏"
        )
        return False
    # 只悬停不点击(卡片悬停会显出操作图标,但我们一个都不碰)
    human.hover(
        (box["x"] + box["width"] * 0.5, box["y"] + box["height"] * 0.5),
        reason="移到笔记列表滚动区(滚轮落点)",
    )
    return True


def _merged_count(collector: _PostedCollector) -> int:
    """当前已抓到的去重后篇数(与最终返回的口径一致)。"""
    return len(_merge_batches(collector.batches))


def _reached_expected(collector: _PostedCollector) -> bool:
    """已抓够接口自报的总数 —— 只有这一种情况可以凭"滚动无响应"当场收工。

    期望未知时恒为 False:宁可多滚两次,也不再把中途的一次空滚当成到底。
    """
    expected = collector.expected_total
    return expected is not None and _merged_count(collector) >= expected


def permission_code_of(raw: Dict[str, Any]) -> Optional[int]:
    """列表项的 ``permission_code``(0=公开 / 1=仅自己可见,其余档位语义未验证)。

    **存平台原值,不自造 public/private 枚举**:只实测了 2 档,自造映射遇到第三态会丢
    信息或误判。读不到 / 不是整数 → None = **未知**,注意 None 不等于公开。
    """
    value = (raw or {}).get("permission_code")
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def permission_msg_of(raw: Dict[str, Any]) -> Optional[str]:
    """列表项的 ``permission_msg`` 平台原文案(公开笔记实测是空串);非字符串 → None。"""
    value = (raw or {}).get("permission_msg")
    return value if isinstance(value, str) else None


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

        # 滚动前必须先把鼠标移到列表上,否则滚轮打在不滚动的顶栏上,后面白滚(真号实测)
        _hover_note_list(page, human, account_id)

        empty_scrolls = 0
        while len(collector.batches) < max_pages:
            seen = len(collector.batches)
            human.wait(0.8, 2.0, context="笔记列表浏览")
            human.scroll("down")
            batch = _wait_for_new_batch(page, collector, seen, _NEXT_BATCH_TIMEOUT_S)
            if batch is None:
                # 没抓够(或压根不知道该有多少)就再滚几次:实测列表中段就有一次滚动只挪了
                # 260px 没触发请求,下一次才发出 page=3 —— 单次无响应判不了到底。
                empty_scrolls += 1
                if not _reached_expected(collector) and empty_scrolls < _EMPTY_SCROLL_RETRIES:
                    logger.info(
                        f"[creator_note_list] 账号{account_id}: 滚动后无新分页响应,"
                        f"已抓 {_merged_count(collector)}/{collector.expected_total} 篇,"
                        f"重试第 {empty_scrolls}/{_EMPTY_SCROLL_RETRIES - 1} 次"
                    )
                    continue
                logger.info(
                    f"[creator_note_list] 账号{account_id}: 滚动后无新分页响应,遍历结束"
                    f"(共 {len(collector.batches)} 批)"
                )
                break
            empty_scrolls = 0
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
        expected = collector.expected_total
        if expected is not None and len(notes) < expected:
            # 抓不满**绝不静默成功**:少掉的那些笔记会在台账里凭空消失(或被当成"已删"),
            # 今天就是因为只抓到 20/37 让 17 篇长期不在台账里。
            logger.warning(
                f"[creator_note_list] 账号{account_id}: 只抓到 {len(notes)} 篇,"
                f"少于接口自报的 {expected} 篇 —— 翻页提前终止,本次结果不完整"
            )
        return notes
    finally:
        # 监听器必须摘掉:同一个 page 会被后续任务复用,留着会继续吃响应体
        try:
            page.remove_listener("response", collector.handle)
        except Exception:  # noqa: BLE001 — 摘监听失败不影响已抓到的结果
            logger.warning("[creator_note_list] 摘除 response 监听失败(忽略)")
