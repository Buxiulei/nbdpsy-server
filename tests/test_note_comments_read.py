"""评论区只读抓取:分页 / 上限 / 到底判据 / 撞墙 / 作者回复标记。

**选择器已在 2026-08-07 用账号 9(米之木木)真号只读复核过**(见被测模块 docstring):
``.parent-comment`` 10 个、``.comment-item`` 11 个、昵称/正文/点赞/子回复/作者徽标逐个取到真值。

假页面测的仍然只是**翻页与到底判定的逻辑**(假页面按代码的假设造,证不了选择器);
选择器的效力由那次真号复核背书。下面三个用例(``赞``=0 赞、hover 落点夹进视口、
标称总数含子回复)直接来自那次复核逮到的三个真缺陷,是回归锁。
"""

import pytest

from app.browser import note_comments_read as ncr


class _FakeElement:
    """假 DOM 元素:只提供被测代码真用到的那几个读操作。"""

    def __init__(self, data: dict):
        self._data = data

    def query_selector(self, sel: str):
        hits = self.query_selector_all(sel)
        return hits[0] if hits else None

    def query_selector_all(self, sel: str):
        return [_FakeElement(d) for d in (self._data.get("children") or {}).get(sel, [])]

    def inner_text(self) -> str:
        return self._data.get("text", "")

    def get_attribute(self, name: str):
        return (self._data.get("attrs") or {}).get(name)

    def bounding_box(self):
        # 真号实测:评论区矩形是 y≈2099 的**文档**坐标,远在 800 高的视口之外
        return {"x": 100.0, "y": 2099.0, "width": 600.0, "height": 80.0}


class _PagingPage:
    """滚动才加载下一页的假页面:每次 ``mark_scrolled`` 多放出一页评论。"""

    def __init__(self, pages: list[list[dict]], url: str = "https://www.xiaohongshu.com/explore/x"):
        self._pages = pages
        self._loaded = 1
        self.url = url
        self.scrolls = 0

    def mark_scrolled(self):
        self.scrolls += 1
        self._loaded = min(self._loaded + 1, len(self._pages))

    def _visible(self) -> list[dict]:
        out: list[dict] = []
        for page in self._pages[: self._loaded]:
            out.extend(page)
        return out

    def query_selector_all(self, sel: str):
        if sel == ncr.PARENT_COMMENT:
            return [_FakeElement(d) for d in self._visible()]
        return []

    def query_selector(self, sel: str):
        hits = self.query_selector_all(sel)
        return hits[0] if hits else None

    def inner_text(self, _sel: str = "body") -> str:
        return "评论区"

    @property
    def viewport_size(self):
        return {"width": 1280, "height": 800}


class _FakeHuman:
    """记录拟人动作次数的假 SyncHumanActions;滚动时驱动假页面加载下一页。"""

    def __init__(self, page):
        self.page = page
        self.hovers = 0
        self.hover_points: list = []
        self.scrolls = 0
        self.waits = 0

    def hover(self, target, *, reason: str = ""):
        self.hovers += 1
        self.hover_points.append(target)

    def scroll(self, direction: str = "down", distance: int = None):
        self.scrolls += 1
        self.page.mark_scrolled()

    def wait(self, *_a, **_kw):
        self.waits += 1


def _comment(cid: str, text: str, author: str = "读者", author_id: str = "u-reader",
             likes: str = "3", author_tag: bool = False, subs: list[dict] | None = None) -> dict:
    children = {
        ncr.COMMENT_ITEM: [{
            "attrs": {"id": f"comment-{cid}"},
            "children": {
                ncr.COMMENT_AUTHOR: [{"text": author, "attrs": {"data-user-id": author_id}}],
                ncr.COMMENT_TEXT: [{"text": text}],
                ncr.COMMENT_LIKE: [{"text": likes}],
                **({ncr.AUTHOR_BADGE: [{"text": "作者"}]} if author_tag else {}),
            },
        }],
        ncr.SUB_COMMENT: subs or [],
    }
    return {"children": children}


def _pages(total: int, per_page: int = 5) -> list[list[dict]]:
    items = [_comment(f"c{i}", f"第{i}条评论") for i in range(total)]
    return [items[i:i + per_page] for i in range(0, total, per_page)]


def _run(page, **kw):
    human = _FakeHuman(page)
    return ncr.read_note_comments(page, human, **kw), human


# ---------------- 分页 ----------------


def test_scrolls_until_max_count_reached():
    """上限判据:够 max_count 就停,不多滚一次(会话越短越安全)。"""
    page = _PagingPage(_pages(30))
    result, human = _run(page, max_count=12)
    assert len(result["comments"]) == 12
    assert result["complete"] is True
    assert result["stop_reason"] == "reached_limit"
    # 5 条/页 → 拿满 12 条需要滚到第 3 页,即 2 次滚动;绝不该滚到 6 页
    assert human.scrolls == 2


def test_first_screen_enough_never_scrolls():
    page = _PagingPage(_pages(5))
    result, human = _run(page, max_count=3)
    assert len(result["comments"]) == 3
    assert human.scrolls == 0


def test_hovers_before_first_scroll():
    """滚动前必须先把鼠标移到评论列表上 —— wheel 打在 (0,0) 的顶栏上是全空转。"""
    page = _PagingPage(_pages(20))
    _, human = _run(page, max_count=20)
    assert human.hovers >= 1


# ---------------- 到底判据 ----------------


def test_stops_when_no_new_comments_after_idle_rounds():
    """到底判据:连续 ``idle_rounds`` 轮滚动没有新增 → 判到底(complete=True)。"""
    page = _PagingPage(_pages(7))  # 只有 7 条,要 50 条也只能拿到 7
    result, human = _run(page, max_count=50, idle_rounds=2)
    assert len(result["comments"]) == 7
    assert result["complete"] is True
    assert result["stop_reason"] == "no_new_after_scroll"
    # 第 2 次滚动加载出第 2 页(2 条),之后连续 2 轮无新增才停
    assert human.scrolls == 3


def test_single_idle_round_is_not_enough():
    """一轮没新增不算到底 —— 实测创作中心列表就有过"滚一次没动,下一次才出"的情况。"""
    pages = _pages(5) + [[]] + [[_comment("late", "迟到的第 6 条")]]
    page = _PagingPage(pages)
    result, _ = _run(page, max_count=50, idle_rounds=2)
    texts = [c["text"] for c in result["comments"]]
    assert "迟到的第 6 条" in texts


def test_round_cap_stops_but_marks_incomplete():
    """轮数硬上限只防死循环:停了但**不能**声称抓到底。"""
    page = _PagingPage(_pages(500, per_page=2))
    result, human = _run(page, max_count=400, max_rounds=4)
    assert result["complete"] is False
    assert result["stop_reason"] == "round_cap"
    assert human.scrolls == 4


def test_expected_total_short_circuits():
    """已知平台评论总数(纯 HTTP 拿到的 interact.comment)时,抓够就判到底,不白滚。"""
    page = _PagingPage(_pages(10))
    result, human = _run(page, max_count=50, expected_total=10)
    assert len(result["comments"]) == 10
    assert result["complete"] is True
    assert result["stop_reason"] == "reached_expected_total"
    assert human.scrolls == 1


# ---------------- 解析 ----------------


def test_parses_text_likes_and_dedupes_by_id():
    """同一条评论在两轮里都会被扫到,按 comment_id 去重,不能重复计数。"""
    first = [_comment("a", "第一条", likes="12")]
    page = _PagingPage([first, first + [_comment("b", "第二条")]])
    result, _ = _run(page, max_count=50)
    assert [c["comment_id"] for c in result["comments"]] == ["a", "b"]
    assert result["comments"][0]["text"] == "第一条"
    assert result["comments"][0]["like_count"] == 12


def test_like_count_non_numeric_kept_raw():
    """点赞数可能是「1.2万」——转不成 int 就给 None + 保留原串,绝不瞎折算。"""
    page = _PagingPage([[_comment("a", "爆款评论", likes="1.2万")]])
    result, _ = _run(page, max_count=5)
    assert result["comments"][0]["like_count"] is None
    assert result["comments"][0]["like_count_raw"] == "1.2万"


def test_author_reply_flagged_by_user_id_first():
    """是否作者回复:user_id 可比时**以它为准且排他**,徽标不参与。

    c 那条挂着「作者」徽标但 user_id 对不上 —— 判 False。徽标选择器是旧仓先例
    (旧仓 ``like-active`` 判已赞就 100% 误判过),不该让它去推翻一个确定的 id 比对。
    """
    page = _PagingPage([[
        _comment("a", "读者说的", author_id="u-reader"),
        _comment("b", "作者回的", author="念头河", author_id="u-author"),
        _comment("c", "徽标那条", author_id="u-unknown", author_tag=True),
    ]])
    result, _ = _run(page, max_count=5, note_author_user_id="u-author")
    flags = {c["comment_id"]: c["is_author_reply"] for c in result["comments"]}
    assert flags == {"a": False, "b": True, "c": False}


def test_author_badge_is_fallback_when_user_id_unreadable():
    """读不到 user_id(DOM 变了/没这属性)时才退到徽标判据。"""
    page = _PagingPage([[
        _comment("a", "读者说的", author_id=None),
        _comment("b", "作者回的", author_id=None, author_tag=True),
    ]])
    result, _ = _run(page, max_count=5, note_author_user_id="u-author")
    flags = {c["comment_id"]: c["is_author_reply"] for c in result["comments"]}
    assert flags == {"a": False, "b": True}


def test_sub_comments_collected():
    subs = [{
        "attrs": {"id": "comment-s1"},
        "children": {
            ncr.COMMENT_AUTHOR: [{"text": "念头河", "attrs": {"data-user-id": "u-author"}}],
            ncr.COMMENT_TEXT: [{"text": "谢谢你"}],
            ncr.COMMENT_LIKE: [{"text": "1"}],
        },
    }]
    page = _PagingPage([[_comment("a", "主楼", subs=subs)]])
    result, _ = _run(page, max_count=5, note_author_user_id="u-author")
    top = result["comments"][0]
    assert len(top["sub_comments"]) == 1
    assert top["sub_comments"][0]["text"] == "谢谢你"
    assert top["sub_comments"][0]["is_author_reply"] is True


def test_comment_without_id_still_kept():
    """没有 id 的评论(DOM 变了或懒渲染)不丢弃 —— 内容才是运营要的东西。"""
    broken = {"children": {ncr.COMMENT_ITEM: [{
        "attrs": {},
        "children": {ncr.COMMENT_TEXT: [{"text": "没有 id 但有内容"}]},
    }]}}
    page = _PagingPage([[broken]])
    result, _ = _run(page, max_count=5)
    assert len(result["comments"]) == 1
    assert result["comments"][0]["comment_id"] is None


def test_empty_comment_area_returns_empty_not_error():
    page = _PagingPage([[]])
    result, human = _run(page, max_count=20)
    assert result["comments"] == []
    assert result["complete"] is True
    assert result["stop_reason"] == "empty"
    assert human.hovers == 0  # 没有评论可 hover,别乱动鼠标


def test_empty_while_platform_says_there_are_comments_is_not_complete():
    """选择器整体失配 ≠ 这篇没人评论 —— 平台标称数就在入参里,不许报 complete=True。

    真号那篇标称 17 条。假设某天平台改版让 ``.parent-comment`` 全失配(页面上一条都读
    不到),报 ``complete=True`` + ``stop_reason=empty`` 时,机器读起来就是"这篇没人评论"
    —— 运营据此得出的"对标笔记零互动"结论是彻头彻尾的假信息。
    """
    page = _PagingPage([[]])
    result, human = _run(page, max_count=20, expected_total=17)
    assert result["comments"] == []
    assert result["complete"] is False, "标称 17 条却一条没读到,绝不许声称抓完"
    assert result["stop_reason"] == "empty_but_expected"
    assert result["expected_total"] == 17, "得把标称数一起交出去,调用方才知道差多少"
    assert human.scrolls == 0  # 读不到就停手,别在失配的页面上白滚


def test_empty_and_platform_also_says_zero_is_genuinely_empty():
    """标称就是 0:那才是真的没人评论,不许报 empty_but_expected 吓唬调用方。

    这条路径在更早的 ``reached_expected_total`` 判据上就返回了(0 条 >= 标称 0),
    断言的是**结论**:抓完了、没有可疑。
    """
    page = _PagingPage([[]])
    result, _ = _run(page, max_count=20, expected_total=0)
    assert result["complete"] is True
    assert result["stop_reason"] != "empty_but_expected"


# ---------------- 撞墙 ----------------


def test_wall_url_aborts_immediately():
    """撞验证墙立即停手报告,**绝不**继续滚(继续只会把号推得更深)。"""
    page = _PagingPage(_pages(50), url="https://www.xiaohongshu.com/website-login/captcha?x=1")
    result, human = _run(page, max_count=20)
    assert result["error"] == "wall"
    assert result["comments"] == []
    assert human.scrolls == 0


def test_wall_detected_mid_scroll_stops():
    page = _PagingPage(_pages(50))
    human = _FakeHuman(page)
    original = human.scroll

    def _scroll_then_wall(*a, **kw):
        original(*a, **kw)
        page.url = "https://www.xiaohongshu.com/captcha?verifyType=124"

    human.scroll = _scroll_then_wall
    result = ncr.read_note_comments(page, human, max_count=50)
    assert result["error"] == "wall"
    # 撞墙前已经抓到的仍交出去(证据不丢)
    assert len(result["comments"]) == 5
    assert human.scrolls == 1


def test_max_count_zero_reads_nothing():
    page = _PagingPage(_pages(20))
    result, human = _run(page, max_count=0)
    assert result["comments"] == []
    assert human.scrolls == 0


# ---------------- 真号复核逮到的三个缺陷(回归锁) ----------------


def test_zero_like_label_is_zero_not_unknown():
    """0 赞时平台在计数位上写的是「赞」——按"转不成 int"处理会把它和"没读到"混成同一个 None。

    2026-08-07 账号 9 真号实测:有赞的那条读到 "1",没赞的读到 "赞"。
    """
    page = _PagingPage([[
        _comment("a", "有人赞", likes="1"),
        _comment("b", "没人赞", likes="赞"),
    ]])
    result, _ = _run(page, max_count=5)
    likes = {c["comment_id"]: c["like_count"] for c in result["comments"]}
    assert likes == {"a": 1, "b": 0}
    # 原串仍保留,便于事后核对平台文案有没有变
    assert result["comments"][1]["like_count_raw"] == "赞"


def test_hover_point_clamped_into_viewport():
    """评论区在 y≈2099 的文档坐标上,而视口只有 800 高——落点必须夹进视口才是真悬停。"""
    page = _PagingPage(_pages(20))
    _, human = _run(page, max_count=20)
    assert human.hover_points, "该 hover 却没 hover"
    x, y = human.hover_points[0]
    assert 0 < x < 1280 and 0 < y < 800, f"落点 {(x, y)} 掉在视口外了"


def test_expected_total_counts_sub_comments():
    """平台标称的 commentCount **含子回复**(实测标称 17 = 10 条一楼 + 子回复)。

    只拿一楼条数去比,这条判据永远够不着;算上子回复才对得上口径。
    """
    subs = [{
        "attrs": {"id": "comment-s1"},
        "children": {ncr.COMMENT_TEXT: [{"text": "子回复"}]},
    }]
    page = _PagingPage([[
        _comment("a", "一楼一", subs=subs),
        _comment("b", "一楼二", subs=subs),
    ]])
    result, human = _run(page, max_count=50, expected_total=4)
    # 2 条一楼 + 2 条子回复 = 4,已达标称总数 → 判到底,不再白滚
    assert result["stop_reason"] == "reached_expected_total"
    assert human.scrolls == 0
