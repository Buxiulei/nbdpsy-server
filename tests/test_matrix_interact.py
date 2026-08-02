"""矩阵互动单测(不起真浏览器),锁设计第五节验收 1 的四项 + 台账纪律。

- 矩阵选号:全部 cookie_status='valid' 排除发布者本人(失效/未知 cookie 号不派);
- 标题匹配定位:命中才点,匹配不到抛错放弃(**绝不默认取第一篇**);
- 已赞/已藏跳过分支:图标读到 #liked / #collected 记 skipped 且一次都不点;
- 成败判定:两个动作全失败必落 error(评论移走后 not_requested 状态一并取消,
  判据改为直接对全部动作取 any——回归锁死"永远落不下 error"的老缺陷不复发);
- 延时排期:payload 的 not_before 未到点则不派发(执行方不 sleep 等待);
- matrix_interact 非幂等,不得进 _IDEMPOTENT_KINDS(重复执行会取消已点的赞)。

patch 纪律:打在被测模块的命名空间(顶层 import 的依赖),不是源模块。
"""

import sqlite3
from datetime import datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

import app.core.db as db_module
from app.browser import matrix_interact as browser_mi
from app.services import browser_jobs_repo as repo
from app.services import matrix_interact as svc


# ---------------- 测试替身 ----------------


class _FakeLink:
    """假 <a>:只提供 get_attribute('href')(卡片的 note_id 就藏在这里)。"""

    def __init__(self, href: str):
        self._href = href

    def get_attribute(self, name: str):
        return self._href if name == "href" else None


class _FakeElement:
    """假元素:只提供定位/读文本/取矩形/读链接这几件被测代码真用到的能力。"""

    def __init__(self, text: str = "", box: dict | None = None, href: str | None = None):
        self._text = text
        self._box = box or {"x": 100.0, "y": 200.0, "width": 240.0, "height": 320.0}
        self._href = href

    def inner_text(self) -> str:
        return self._text

    def bounding_box(self) -> dict:
        return self._box

    def query_selector_all(self, _sel):
        return [_FakeLink(self._href)] if self._href else []


class _FakeLocator:
    def __init__(self, ok: bool = True):
        self._ok = ok

    @property
    def first(self):
        return self

    def wait_for(self, **_kw):
        if not self._ok:
            raise RuntimeError("等待超时")


class _FakePage:
    """假 page:query_selector(_all) / locator / evaluate / url 四件套。"""

    def __init__(self, cards=(), elements=None, evaluate=None, locator_ok=True):
        self._cards = list(cards)
        self._elements = elements or {}
        self._evaluate = evaluate or (lambda js, arg: None)
        self._locator_ok = locator_ok
        self.url = "https://www.xiaohongshu.com/explore/abc?xsec_token=T"

    def locator(self, _sel):
        return _FakeLocator(self._locator_ok)

    def query_selector_all(self, sel):
        # 互动按钮类选择器返回登记的那个元素;其余(笔记卡)仍返回卡片列表。
        # _icon_action 改成按下标取元素后,这里必须能按选择器答得上来。
        if sel in self._elements:
            return [self._elements[sel]]
        return self._cards

    def query_selector(self, sel):
        return self._elements.get(sel)

    def evaluate(self, js, arg=None):
        got = self._evaluate(js, arg)
        # 用例只关心"图标读出来是什么",不该知道内部把"挑元素"和"读图标"拆成了两段 JS。
        # 故这里做适配:用例给一个 href 字符串,就当成"整页只有一份、可点、图标是它"。
        if js is browser_mi._PICK_HITTABLE_JS and not isinstance(got, dict):
            return {"total": 1, "index": 0, "href": got, "skipped": []}
        return got

    def load_more(self) -> None:
        """滚动触发的懒加载(基类没有更多卡片,滚了也不变)。"""


class _LazyProfilePage(_FakePage):
    """懒加载假主页:每次滚动"渲染"出下一批卡片,query_selector_all 只看得见已渲染的。

    ``batches`` 是**累计**列表(第 i 项 = 滚了 i 次之后主页上的全部卡片),与真实懒加载
    一致 —— 卡片只增不减。
    """

    def __init__(self, batches):
        self._batches = [list(b) for b in batches]
        self._idx = 0
        super().__init__(cards=self._batches[0])

    def load_more(self) -> None:
        if self._idx + 1 < len(self._batches):
            self._idx += 1
            self._cards = list(self._batches[self._idx])


class _FakeHuman:
    """假拟人层:记录动作,断言"该点的点了 / 不该点的一次没点 / 不该滚的一次没滚"。"""

    def __init__(self, page=None):
        self.navigated = None
        self.clicks = []
        self.typed = []
        self.hovers = []
        self.scrolls = 0
        self._page = page

    def navigate(self, url, **_kw):
        self.navigated = url

    def wait(self, *_a, **_kw):
        pass

    def scroll(self, *_a, **_kw):
        self.scrolls += 1
        if self._page is not None:
            self._page.load_more()

    def scroll_to_element(self, _el):
        pass

    def hover(self, target, **_kw):
        self.hovers.append(target)

    def click(self, target, **_kw):
        self.clicks.append(target)

    def type_text(self, target, text, **_kw):
        self.typed.append((target, text))


# ---------------- 标题匹配定位 ----------------


def test_title_matches_exact_and_truncated():
    """完整包含命中;卡片截断成省略号时按 ≥8 字前缀命中;短前缀/异题不命中。"""
    title = "焦虑发作时的五个自救动作"
    assert browser_mi._title_matches("焦虑发作时的五个自救动作\n1.2万", title)
    assert browser_mi._title_matches("焦虑发作时的五个自...", title)
    assert not browser_mi._title_matches("焦虑发...", title)  # 前缀太短,不认
    assert not browser_mi._title_matches("拖延症的三个成因", title)
    assert not browser_mi._title_matches("焦虑发作时的五个自救动作", "")


def test_open_note_by_title_clicks_matched_card():
    """按标题匹配到第几张就点第几张(不是第一张),点的是卡片上部封面区。"""
    target = "边界感是练出来的"
    cards = [
        _FakeElement("别人的情绪不是你的责任\n860"),
        _FakeElement(f"{target}\n1203"),
    ]
    page = _FakePage(cards=cards)
    human = _FakeHuman()

    url = browser_mi._open_note_by_title(page, human, "u123", target)

    assert human.navigated.endswith("/user/profile/u123")
    assert url == page.url
    assert len(human.clicks) == 1
    box = cards[1].bounding_box()
    x, y = human.clicks[0]
    assert x == box["x"] + box["width"] * 0.5
    assert y == box["y"] + box["height"] * 0.35  # 上部封面区,不点底部作者行


def test_open_note_by_title_gives_up_when_no_match():
    """匹配不到标题 → 抛 note_not_found 放弃,绝不退而求其次点第一篇。"""
    cards = [_FakeElement("完全不相干的另一篇笔记标题"), _FakeElement("再来一篇也不相干")]
    page = _FakePage(cards=cards)
    human = _FakeHuman()

    with pytest.raises(browser_mi.MatrixInteractError) as exc:
        browser_mi._open_note_by_title(page, human, "u123", "目标笔记的标题在这里")

    assert exc.value.reason.startswith("note_not_found")
    assert human.clicks == []  # 一次都没点


# ---------------- 主页懒加载:滚动翻找 ----------------


def test_first_screen_hit_never_scrolls():
    """首屏就命中 → **一次都不滚、也不悬停**(发布后互动天天走这条路,不能被拖慢)。"""
    target = "边界感是练出来的"
    page = _LazyProfilePage([
        [_FakeElement("别人的情绪不是你的责任"), _FakeElement(f"{target}\n1203")],
        [_FakeElement("别人的情绪不是你的责任"), _FakeElement(f"{target}\n1203"),
         _FakeElement("不该被加载出来的第三篇")],
    ])
    human = _FakeHuman(page)

    browser_mi._open_note_by_title(page, human, "u123", target)

    assert human.scrolls == 0
    assert human.hovers == []
    assert len(human.clicks) == 1


def test_scrolls_until_target_loaded():
    """首屏没有、滚动加载后出现 → 命中并点那张卡(补量找老笔记的主场景)。"""
    target = "心理咨询师-徐瑞恒,陪你看清自我怀疑来处"
    old_card = _FakeElement(f"{target}\n88")
    page = _LazyProfilePage([
        [_FakeElement("新笔记一"), _FakeElement("新笔记二")],
        [_FakeElement("新笔记一"), _FakeElement("新笔记二"), _FakeElement("新笔记三")],
        [_FakeElement("新笔记一"), _FakeElement("新笔记二"), _FakeElement("新笔记三"),
         old_card],
    ])
    human = _FakeHuman(page)

    browser_mi._open_note_by_title(page, human, "u123", target)

    assert human.scrolls == 2
    box = old_card.bounding_box()
    assert human.clicks == [
        (box["x"] + box["width"] * 0.5, box["y"] + box["height"] * 0.35)
    ]


def test_hovers_note_card_before_scrolling():
    """滚动前必须先把鼠标移到笔记卡上 —— mouse.wheel 投在鼠标当前位置,(0,0) 处会空转。"""
    first = _FakeElement("无关笔记")
    page = _LazyProfilePage([[first]])
    human = _FakeHuman(page)

    with pytest.raises(browser_mi.MatrixInteractError):
        browser_mi._open_note_by_title(page, human, "u123", "找不到的标题在这里")

    box = first.bounding_box()
    assert human.hovers == [
        (box["x"] + box["width"] * 0.5, box["y"] + box["height"] * 0.5)
    ]


def test_gives_up_after_reaching_bottom():
    """滚到底(连续多轮无新卡)仍没有 → 抛 note_not_found,绝不退而求其次点第一篇。"""
    page = _LazyProfilePage([
        [_FakeElement("无关一")],
        [_FakeElement("无关一"), _FakeElement("无关二")],
    ])
    human = _FakeHuman(page)

    with pytest.raises(browser_mi.MatrixInteractError) as exc:
        browser_mi._open_note_by_title(page, human, "u123", "目标笔记的标题在这里")

    assert exc.value.reason.startswith("note_not_found")
    assert human.clicks == []
    # 1 轮加载出新卡 + 连续 _NO_GROWTH_ROUNDS 轮无新增才停;远没滚到轮数上限
    assert human.scrolls == 1 + browser_mi._NO_GROWTH_ROUNDS
    assert human.scrolls < browser_mi._MAX_SCROLL_ROUNDS


def test_single_empty_scroll_is_not_bottom():
    """单次滚动没加载出新卡**不算到底**:实测有"这次只挪一点、下一次才加载"的情况。"""
    target = "藏在第二轮的老笔记标题"
    page = _LazyProfilePage([
        [_FakeElement("无关一")],
        [_FakeElement("无关一")],                       # 第 1 轮:滚了但没新卡
        [_FakeElement("无关一"), _FakeElement(target)],  # 第 2 轮:才真的加载出来
    ])
    human = _FakeHuman(page)

    browser_mi._open_note_by_title(page, human, "u123", target)

    assert human.scrolls == 2
    assert len(human.clicks) == 1


def test_scroll_rounds_are_capped():
    """卡片一直在增长但永远没有目标 → 到轮数上限即停,不死循环。"""

    class _EndlessProfilePage(_FakePage):
        """每滚一次就多一张无关卡片的假主页(模拟无限流)。"""

        def __init__(self):
            super().__init__(cards=[_FakeElement("无关笔记 0")])

        def load_more(self) -> None:
            self._cards = self._cards + [_FakeElement(f"无关笔记 {len(self._cards)}")]

    page = _EndlessProfilePage()
    human = _FakeHuman(page)

    with pytest.raises(browser_mi.MatrixInteractError):
        browser_mi._open_note_by_title(page, human, "u123", "永远不会出现的标题")

    assert human.scrolls == browser_mi._MAX_SCROLL_ROUNDS


# ---------------- note_id 优先 / 标题回退(既有行为不变)----------------


def test_note_id_wins_over_title_match():
    """note_id 命中优先于标题命中(台账 title 会过期,链接里的 id 才是稳定主键)。"""
    note_id = "6a6b503e000000000600534a"
    title_card = _FakeElement("心理咨询师-徐瑞恒,陪你看清自我怀疑来处\n12")
    id_card = _FakeElement("平台上已改名的同一篇", href=f"/explore/{note_id}?xsec=1")
    page = _FakePage(cards=[title_card, id_card])
    human = _FakeHuman()

    browser_mi._open_note_by_title(
        page, human, "u123", "心理咨询师-徐瑞恒,陪你看清自我怀疑来处", note_id=note_id
    )

    box = id_card.bounding_box()
    assert human.clicks == [
        (box["x"] + box["width"] * 0.5, box["y"] + box["height"] * 0.35)
    ]
    assert human.scrolls == 0


def test_falls_back_to_title_when_note_id_absent():
    """给了 note_id 但页面上没这张卡 → 回退标题匹配,不直接判失败。"""
    target = "边界感是练出来的"
    cards = [_FakeElement("别的笔记"), _FakeElement(f"{target}\n1203")]
    page = _FakePage(cards=cards)
    human = _FakeHuman()

    browser_mi._open_note_by_title(page, human, "u123", target, note_id="ffffffff")

    box = cards[1].bounding_box()
    assert human.clicks == [
        (box["x"] + box["width"] * 0.5, box["y"] + box["height"] * 0.35)
    ]


# ---------------- 已赞 / 已藏跳过分支 ----------------


def test_like_skipped_when_already_liked():
    """图标是 #liked(不是看 class 里的 like-active)→ 记 skipped 且一次都不点。"""
    page = _FakePage(
        elements={".engage-bar .like-wrapper": _FakeElement()},
        evaluate=lambda js, arg: "#liked",
    )
    human = _FakeHuman()

    result = browser_mi._icon_action(
        page, human, "点赞", browser_mi._LIKE_SELECTORS, "#like", "#liked"
    )

    assert result["status"] == "skipped"
    assert human.clicks == []


def test_collect_skipped_when_already_collected():
    """收藏同构:#collected → skipped,不点。"""
    page = _FakePage(
        elements={".engage-bar .collect-wrapper": _FakeElement()},
        evaluate=lambda js, arg: "#collected",
    )
    human = _FakeHuman()

    result = browser_mi._icon_action(
        page, human, "收藏", browser_mi._COLLECT_SELECTORS, "#collect", "#collected"
    )

    assert result["status"] == "skipped"
    assert human.clicks == []


def test_like_clicks_and_verifies_icon_flip():
    """未赞(#like)→ 拟人点击 → 复核图标变 #liked 才算 done。"""
    element = _FakeElement()
    state = {"href": "#like"}

    def fake_evaluate(_js, _arg):
        href = state["href"]
        state["href"] = "#liked"  # 点击后下一次读到已赞
        return href

    page = _FakePage(
        elements={".engage-bar .like-wrapper": element}, evaluate=fake_evaluate
    )
    human = _FakeHuman()

    result = browser_mi._icon_action(
        page, human, "点赞", browser_mi._LIKE_SELECTORS, "#like", "#liked"
    )

    assert result["status"] == "done"
    assert human.clicks == [element]


def test_like_icon_unreadable_does_not_click():
    """图标读不出来(状态未知)就不点:盲点可能把已有的赞取消掉。"""
    page = _FakePage(
        elements={".engage-bar .like-wrapper": _FakeElement()},
        evaluate=lambda js, arg: None,
    )
    human = _FakeHuman()

    result = browser_mi._icon_action(
        page, human, "点赞", browser_mi._LIKE_SELECTORS, "#like", "#liked"
    )

    assert result["status"] == "error" and "unreadable" in result["reason"]
    assert human.clicks == []


# ---------------- 成败判定(评论移除后)----------------


def _patch_interact(monkeypatch, icon_result: dict) -> None:
    """把 interact_with_note 的定位/浏览/图标动作都换成替身,只留成败判定这一层。"""
    monkeypatch.setattr(browser_mi, "_open_note_by_title",
                        lambda *a, **k: "https://www.xiaohongshu.com/explore/x")
    monkeypatch.setattr(browser_mi, "_browse_note", lambda *a, **k: None)
    monkeypatch.setattr(browser_mi, "_icon_action", lambda *a, **k: icon_result)
    monkeypatch.setattr(browser_mi, "SyncHumanActions", lambda page: _FakeHuman())


def test_interact_has_no_comment_step(monkeypatch):
    """矩阵互动只剩点赞 + 收藏两个动作:actions 恒为这两条,不含 comment。

    评论是**结构上**移除的,不是靠传空文案绕过——所以 comment 这个键压根不该出现,
    也不该再有任何 not_requested 状态(它正是老成败判定漏洞的载体)。
    """
    _patch_interact(monkeypatch, {"status": "done"})

    result = browser_mi.interact_with_note(
        _FakePage(), account_id=9, publisher_user_id="u1", title="标题"
    )

    assert set(result["actions"]) == {"like", "collect"}
    assert all(
        a["status"] != "not_requested" for a in result["actions"].values()
    )
    assert "error" not in result


def test_both_actions_failed_falls_to_error(monkeypatch):
    """点赞收藏双双失败 → 必须落 error,绝不能显示 done。

    回归老缺陷:旧判定先剔掉 not_requested 再要求"剔剩的非空"才判失败,一旦所有动作
    都可缺席,error 就永远落不下来,错误上报被彻底架空。评论移走后两个动作无条件各跑
    一次,判据直接对全部动作取 any,不存在可剔空的集合。
    """
    _patch_interact(monkeypatch, {"status": "error", "reason": "点不动"})

    result = browser_mi.interact_with_note(
        _FakePage(), account_id=9, publisher_user_id="u1", title="标题"
    )

    assert result["actions"]["like"]["status"] == "error"
    assert result["actions"]["collect"]["status"] == "error"
    assert result.get("error") == "点赞与收藏均失败"


def test_action_exception_still_counts_as_failure(monkeypatch):
    """动作抛异常被 except 兜成 error 写回 actions,照样参与判定 → 整体 error。

    这条锁死"异常动作没进 actions 导致集合为空、于是不判失败"的另一条退路。
    """
    def _boom(*a, **k):
        raise RuntimeError("页面炸了")

    _patch_interact(monkeypatch, {"status": "done"})
    monkeypatch.setattr(browser_mi, "_icon_action", _boom)

    result = browser_mi.interact_with_note(
        _FakePage(), account_id=9, publisher_user_id="u1", title="标题"
    )

    assert set(result["actions"]) == {"like", "collect"}
    assert all(a["status"] == "error" for a in result["actions"].values())
    assert result.get("error") == "点赞与收藏均失败"


def test_one_action_succeeds_is_not_error(monkeypatch):
    """一个成功一个失败 → 不落 error(动作互不阻断,有成果就不算整体失败)。"""
    calls = {"n": 0}

    def _alternating(*a, **k):
        calls["n"] += 1
        return {"status": "done"} if calls["n"] == 1 else {"status": "error",
                                                           "reason": "点不动"}

    _patch_interact(monkeypatch, {"status": "done"})
    monkeypatch.setattr(browser_mi, "_icon_action", _alternating)

    result = browser_mi.interact_with_note(
        _FakePage(), account_id=9, publisher_user_id="u1", title="标题"
    )

    assert "error" not in result


def test_already_liked_and_collected_counts_as_success(monkeypatch):
    """已赞已藏(skipped)→ 目标本就达成,不得落 error。"""
    _patch_interact(monkeypatch, {"status": "skipped", "reason": "已激活"})

    result = browser_mi.interact_with_note(
        _FakePage(), account_id=9, publisher_user_id="u1", title="标题"
    )

    assert "error" not in result


# ---------------- 独立评论(comment_on_note)----------------


@pytest.mark.parametrize("text", ["", "   ", None])
def test_comment_empty_text_is_error_not_skip(text):
    """空文案 → error(评论独立后没有"这次没要求做"这回事),且不碰页面任何元素。"""
    page = _FakePage(elements={"boom": None})
    human = _FakeHuman()

    result = browser_mi._do_comment(page, human, text)

    assert result["status"] == "error"
    assert "comment_text_empty" in result["reason"]
    assert human.clicks == [] and human.typed == []


class _FakeClock:
    """假时钟:sleep 不真睡只推进虚拟时间,让 _do_comment 的轮询超时分支秒级跑完。"""

    def __init__(self):
        self._now = 0.0

    def monotonic(self) -> float:
        return self._now

    def sleep(self, seconds: float) -> None:
        self._now += seconds


def _comment_page(posted: dict) -> _FakePage:
    """造一个"评论链路全程顺畅"的假页面,只有最后的复核结果由 posted 决定。"""
    elements = {
        browser_mi._COMMENT_ENTRY_SELECTORS[0]: _FakeElement("评论"),
        browser_mi._TEXTAREA: _FakeElement(),
        browser_mi._SUBMIT: _FakeElement(),
    }

    def _evaluate(js, arg=None):
        if js is browser_mi._TEXTAREA_READY_JS:
            return {"ready": True, "reason": "ok"}
        if js is browser_mi._SUBMIT_STATE_JS:
            return {"found": True, "gray": False}
        if js is browser_mi._COMMENT_POSTED_JS:
            return dict(posted)
        raise AssertionError(f"未预期的 evaluate: {js[:40]!r}")

    return _FakePage(elements=elements, evaluate=_evaluate)


@pytest.mark.parametrize("cleared", [False, True])
def test_comment_listed_is_done_regardless_of_cleared(monkeypatch, cleared):
    """listed=True 即 done —— cleared 是前端表现(残留空白/清空延迟)不能当判据。

    cleared=False 那条是本次修复的核心用例:7 条真发出去的评论曾因此被记 error。
    """
    monkeypatch.setattr(browser_mi, "time", _FakeClock())
    page = _comment_page({"cleared": cleared, "listed": True})
    human = _FakeHuman()

    result = browser_mi._do_comment(page, human, "写得真好")

    assert result["status"] == "done"
    assert "reason" not in result
    # cleared 只作附加信息随结果带出,供日后排查前端清空行为
    assert result["cleared"] is cleared
    assert human.typed == [(page.query_selector(browser_mi._TEXTAREA), "写得真好")]


def test_comment_not_listed_is_error(monkeypatch):
    """listed=False → error(不能松:这条防的是"点了发送但根本没发出去")。"""
    monkeypatch.setattr(browser_mi, "time", _FakeClock())
    page = _comment_page({"cleared": True, "listed": False})

    result = browser_mi._do_comment(page, _FakeHuman(), "写得真好")

    assert result["status"] == "error"
    assert "comment_unverified" in result["reason"]
    assert result["cleared"] is True


def test_comment_on_note_success(monkeypatch):
    """评论发出并复核 → {note_url, commented:True},无 error 键(台账落 done)。"""
    monkeypatch.setattr(browser_mi, "_open_note_by_title",
                        lambda *a, **k: "https://www.xiaohongshu.com/explore/x")
    monkeypatch.setattr(browser_mi, "_browse_note", lambda *a, **k: None)
    monkeypatch.setattr(browser_mi, "_do_comment",
                        lambda *a, **k: {"status": "done"})
    monkeypatch.setattr(browser_mi, "SyncHumanActions", lambda page: _FakeHuman())

    result = browser_mi.comment_on_note(
        _FakePage(), account_id=9, publisher_user_id="u1", title="标题",
        comment_text="写得真好",
    )

    assert result == {
        "note_url": "https://www.xiaohongshu.com/explore/x", "commented": True
    }


def test_comment_on_note_failure_carries_error_and_url(monkeypatch):
    """评论没发出 → 带 error 键(台账落 error)且仍给 note_url 供人工核对。"""
    monkeypatch.setattr(browser_mi, "_open_note_by_title",
                        lambda *a, **k: "https://www.xiaohongshu.com/explore/x")
    monkeypatch.setattr(browser_mi, "_browse_note", lambda *a, **k: None)
    monkeypatch.setattr(
        browser_mi, "_do_comment",
        lambda *a, **k: {"status": "error", "reason": "comment_unverified: 没复核到"},
    )
    monkeypatch.setattr(browser_mi, "SyncHumanActions", lambda page: _FakeHuman())

    result = browser_mi.comment_on_note(
        _FakePage(), account_id=9, publisher_user_id="u1", title="标题",
        comment_text="写得真好",
    )

    assert "comment_unverified" in result["error"]
    # 非幂等链路,人工核对是重试前的必要步骤,所以失败也要把链接交出去
    assert result["note_url"] == "https://www.xiaohongshu.com/explore/x"
    assert "commented" not in result


# ---------------- 失败当场留取证(forensics) ----------------
#
# 背景:排查「收藏按钮找不到」时绕了三大圈、20+ 次真号访问都没复现,根因是失败当场没留
# 任何现场证据;补量一轮 5 篇失败 1 篇时错误信息也只有「点赞与收藏均失败」一句。这批用例
# 锁的就是"失败当场把页面状态抓下来、成功路径一点都不多花、取证自己炸了也不能牵连主流程"。


_RAW_FORENSICS = {
    "url": "https://www.xiaohongshu.com/explore/n1?xsec_token=T",
    "title": "边界感是练出来的 - 小红书",
    "body": "正文很长很长",
    "engage_bar": True,
    "wrappers": ["like-wrapper", "collect-wrapper", "chat-wrapper"],
    "like": {
        "present": True,
        "icon_href": "#like",
        "rect": {"x": 10, "y": 20, "w": 30, "h": 40},
        "display": "flex",
        "visibility": "visible",
        "pointer_events": "auto",
    },
    "collect": {"present": False},
    "counts": {"engage_bar": 1, "like": 1, "collect": 1},
}


class _ForensicsPage(_FakePage):
    """假页面:图标 href 按脚本逐次给出,取证 evaluate 单独记账(可注入抛异常)。

    ``evaluated`` 记下每次 evaluate 用的是哪段 JS —— "成功路径一次都没抓取证"这条断言
    只能靠它来证。
    """

    def __init__(self, icon_hrefs=(None,), raw=None, boom=False,
                 selector=".engage-bar .like-wrapper"):
        self._hrefs = list(icon_hrefs)
        self._raw = _RAW_FORENSICS if raw is None else raw
        self._boom = boom
        self.evaluated: list[str] = []
        super().__init__(elements={selector: _FakeElement()})

    def _next_href(self):
        return self._hrefs.pop(0) if len(self._hrefs) > 1 else self._hrefs[0]

    def evaluate(self, js, arg=None):
        self.evaluated.append(js)
        if js is browser_mi._FORENSICS_JS:
            if self._boom:
                raise RuntimeError("Execution context was destroyed")
            return dict(self._raw) if isinstance(self._raw, dict) else self._raw
        if js is browser_mi._PICK_HITTABLE_JS:
            # 默认单份、可命中:多份/被盖住的场景由 _AmbiguousPage 覆盖
            return {"total": 1, "index": 0, "href": self._next_href(), "skipped": []}
        if js is browser_mi._READ_ICON_HREF_AT_JS:
            return self._next_href()
        raise AssertionError(f"未预期的 evaluate: {js[:40]!r}")

    def forensics_calls(self) -> int:
        return sum(1 for js in self.evaluated if js is browser_mi._FORENSICS_JS)


class _AmbiguousPage(_FakePage):
    """整页有多份同名互动按钮的假页面(真号取证:engage_bar=2 / like=18)。

    ``hittable`` 指哪一个是没被盖住的;``-1`` 表示一个都点不到。``clicked_index``
    记下真正被点的是第几个 —— "点的和读的是不是同一份"这条断言只能靠它来证。
    """

    def __init__(self, total=2, hittable=1, hrefs=None):
        self._total = total
        self._hittable = hittable
        # 每一份的图标状态各自独立:残留浮层那份可能停在旧状态
        self._hrefs = list(hrefs) if hrefs else ["#liked"] * total
        self._hrefs[hittable] = "#like" if 0 <= hittable < total else "#like"
        self.clicked_index = None
        self.elements = [_FakeElement() for _ in range(total)]
        super().__init__(elements={".engage-bar .like-wrapper": self.elements[0]})

    def query_selector_all(self, sel):
        if sel == ".engage-bar .like-wrapper":
            return self.elements
        return []

    def evaluate(self, js, arg=None):
        if js is browser_mi._PICK_HITTABLE_JS:
            if self._hittable < 0:
                return {"total": self._total, "index": -1, "href": None,
                        "skipped": [[i, "covered"] for i in range(self._total)]}
            return {"total": self._total, "index": self._hittable,
                    "href": self._hrefs[self._hittable],
                    "skipped": [[i, "covered"] for i in range(self._hittable)]}
        if js is browser_mi._READ_ICON_HREF_AT_JS:
            _sel, index = arg
            # 点过的那一份才翻转;没点的保持原状
            if self.clicked_index == index:
                return "#liked"
            return self._hrefs[index]
        if js is browser_mi._FORENSICS_JS:
            return {"url": "u", "title": "t", "body": "", "engage_bar": True,
                    "wrappers": [], "like": {"present": True},
                    "collect": {"present": True},
                    "counts": {"engage_bar": 2, "like": self._total, "collect": 1}}
        raise AssertionError(f"未预期的 evaluate: {js[:40]!r}")


class _RecordingHuman(_FakeHuman):
    def __init__(self, page):
        super().__init__()
        self._page = page

    def click(self, target, **kw):
        super().click(target, **kw)
        if target in getattr(self._page, "elements", []):
            self._page.clicked_index = self._page.elements.index(target)


def _like_on(page) -> dict:
    return browser_mi._icon_action(
        page, _RecordingHuman(page), "点赞", browser_mi._LIKE_SELECTORS,
        "#like", "#liked",
    )


# ---------------- 同名元素歧义:_not_effective 的真正成因 ----------------
#
# 2026-08-02 真号取证:counts={"engage_bar":2,"like":18} —— 笔记详情是浮层,整页同时
# 存在多份同名按钮,身后信息流每张卡片也自带一个。此前"读状态"和"点击"都用
# document.querySelector,拿的是**文档序第一个**,而屏幕上显示的是另一份。
# 于是点了没反应、图标不翻,**且赞与藏成对失败**(同一份浮层上的两个按钮一起错)。
# 偏偏两个按钮都报 visible / pointer_events:auto、坐标也正常,单看属性根本看不出问题。


def test_clicks_the_hittable_one_not_the_first():
    """多份同名按钮时,点的必须是**能点到**的那一份,不是文档序第一个。"""
    page = _AmbiguousPage(total=3, hittable=2)

    result = _like_on(page)

    assert result["status"] == "done"
    assert page.clicked_index == 2, "点了被盖住的那一份(正是线上失败的样子)"


def test_reads_state_from_the_same_element_it_clicks():
    """读状态与点击必须锁同一份:残留浮层那份是 #liked,可点的那份是 #like。

    读错那份的后果是判 skipped「已点赞」,真正显示的笔记其实一次都没被点到 ——
    静默漏互动,比报错更难发现。
    """
    page = _AmbiguousPage(total=2, hittable=1, hrefs=["#liked", "#like"])

    result = _like_on(page)

    assert result["status"] == "done", "读了第一份的 #liked 就会误判成已点赞"
    assert page.clicked_index == 1


def test_refuses_to_click_when_nothing_is_hittable():
    """同名元素全被盖住 → 一次都不点并判 error(不知道会点到谁就不点)。"""
    page = _AmbiguousPage(total=4, hittable=-1)
    human = _RecordingHuman(page)

    result = browser_mi._icon_action(
        page, human, "点赞", browser_mi._LIKE_SELECTORS, "#like", "#liked"
    )

    assert result["status"] == "error"
    assert "no_hittable_button" in result["reason"]
    assert human.clicks == []
    assert page.clicked_index is None


def _like(page) -> dict:
    return browser_mi._icon_action(
        page, _FakeHuman(), "点赞", browser_mi._LIKE_SELECTORS, "#like", "#liked"
    )


def test_done_path_costs_nothing_extra():
    """点赞成功 → 结果里没有 forensics 键,且**一次取证都没抓**(成功路径零开销)。"""
    page = _ForensicsPage(icon_hrefs=["#like", "#liked"])

    result = _like(page)

    assert result["status"] == "done"
    assert "forensics" not in result
    assert page.forensics_calls() == 0


def test_skipped_path_costs_nothing_extra():
    """已赞(skipped)同样零开销:平台状态本就到位,没有"现场"可查。"""
    page = _ForensicsPage(icon_hrefs=["#liked"])

    result = _like(page)

    assert result["status"] == "skipped"
    assert "forensics" not in result
    assert page.forensics_calls() == 0


def test_icon_failure_carries_full_forensics():
    """图标读不出来判 error → 随结果带一份现场,排查要问的几件事都能答上。"""
    page = _ForensicsPage(icon_hrefs=[None])

    result = _like(page)

    assert result["status"] == "error" and "unreadable" in result["reason"]
    got = result["forensics"]
    # 当时在哪个页 / 页面是什么内容
    assert got["url"] == _RAW_FORENSICS["url"]
    assert got["title"] == _RAW_FORENSICS["title"]
    assert got["body_head"] == "正文很长很长"
    # 互动栏在不在 / 栏里有哪些 wrapper(赞藏评分享齐不齐)
    assert got["engage_bar"] is True
    assert got["wrappers"] == ["like-wrapper", "collect-wrapper", "chat-wrapper"]
    # 两个按钮各自的图标 href、矩形、计算样式
    assert got["like"]["icon_href"] == "#like"
    assert got["like"]["rect"] == {"x": 10, "y": 20, "w": 30, "h": 40}
    assert got["like"]["display"] == "flex"
    assert got["like"]["visibility"] == "visible"
    assert got["like"]["pointer_events"] == "auto"
    assert got["collect"] == {"present": False}
    # 同名元素整页各有几个 —— 判"点到的是不是另一个同名元素"全靠它,不能在裁剪时丢掉
    assert got["counts"] == {"engage_bar": 1, "like": 1, "collect": 1}
    assert page.forensics_calls() == 1


def test_duplicate_element_counts_survive_shrinking():
    """整页出现重复互动栏时,``counts`` 必须原样落到取证里。

    这是"点了但图标不翻、且赞与藏成对失败"最像的一种解释:上一篇的详情浮层没销毁 /
    网格卡片自带点赞图标 → 选择器命中的不是被看到的那个按钮。裁剪逻辑要是把 ``counts``
    顺手丢了,这条怀疑就永远查不实也证不伪。
    """
    raw = dict(_RAW_FORENSICS, counts={"engage_bar": 2, "like": 13, "collect": 2})
    page = _ForensicsPage(icon_hrefs=[None], raw=raw)

    got = _like(page)["forensics"]

    assert got["counts"] == {"engage_bar": 2, "like": 13, "collect": 2}


def test_button_not_found_carries_forensics():
    """按钮压根不在页面上(正是当初查不动的那个症状)也要留现场。"""
    page = _ForensicsPage(selector="别的选择器")

    result = _like(page)

    assert result["reason"] == "点赞_button_not_found"
    assert result["forensics"]["url"] == _RAW_FORENSICS["url"]


def test_forensics_truncates_body_and_wrappers():
    """正文只留头部、wrapper 列表与单条 class 都截断 —— 别把整页塞进库。"""
    page = _ForensicsPage(
        icon_hrefs=[None],
        raw={
            **_RAW_FORENSICS,
            "body": "长" * 5000,
            "wrappers": [f"w{i}-wrapper-" + "x" * 300 for i in range(40)],
        },
    )

    got = _like(page)["forensics"]

    assert len(got["body_head"]) == browser_mi._MAX_BODY
    assert len(got["wrappers"]) == browser_mi._MAX_WRAPPERS
    assert all(len(w) <= browser_mi._MAX_CLASS for w in got["wrappers"])


def test_forensics_normalizes_whitespace():
    """正文里的大段换行/空白归一成单空格:对排查没用,只会白占长度。"""
    page = _ForensicsPage(
        icon_hrefs=[None], raw={**_RAW_FORENSICS, "body": "第一行\n\n\n   第二行\t尾"}
    )

    assert _like(page)["forensics"]["body_head"] == "第一行 第二行 尾"


def test_forensics_exception_is_swallowed():
    """取证自己抛异常 → 被吞掉降级成一句原因,失败结果照常返回(绝不升级成任务崩溃)。"""
    page = _ForensicsPage(icon_hrefs=[None], boom=True)

    result = _like(page)

    assert result["status"] == "error" and "unreadable" in result["reason"]
    assert result["forensics"]["error"].startswith("取证失败: RuntimeError")
    # evaluate 挂了也还留得下 URL —— 被踢到验证页/登录页靠它一眼就能定性
    assert result["forensics"]["url"] == page.url


def test_forensics_tolerates_unexpected_evaluate_return():
    """evaluate 返回的不是对象(页面改版 / 注入被拦)也只降级,不炸。"""
    page = _ForensicsPage(icon_hrefs=[None], raw="不是对象")

    result = _like(page)

    assert result["status"] == "error"
    assert "取证失败" in result["forensics"]["error"]


def test_forensics_survives_unreadable_page_url():
    """连 page.url 都读不到(页没了)也只记一句,不影响失败结果本身。"""
    class _NoUrlPage(_ForensicsPage):
        @property
        def url(self):
            raise RuntimeError("Target page closed")

        @url.setter
        def url(self, _value):
            pass

    page = _NoUrlPage(icon_hrefs=[None], boom=True)

    result = _like(page)

    assert result["status"] == "error"
    assert "取证失败" in result["forensics"]["url_error"]


def test_aggregate_failure_reuses_action_forensics(monkeypatch):
    """两动作全败的汇总 error 也带现场,且**复用动作级那份**,不在同一页重抓第三次。"""
    monkeypatch.setattr(browser_mi, "_open_note_by_title",
                        lambda *a, **k: "https://www.xiaohongshu.com/explore/x")
    monkeypatch.setattr(browser_mi, "_browse_note", lambda *a, **k: None)
    monkeypatch.setattr(browser_mi, "SyncHumanActions", lambda page: _FakeHuman())
    page = _ForensicsPage(icon_hrefs=[None], selector=".like-wrapper")
    page._elements[".collect-wrapper"] = _FakeElement()

    result = browser_mi.interact_with_note(
        page, account_id=9, publisher_user_id="u1", title="标题"
    )

    assert result["error"] == "点赞与收藏均失败"
    assert result["forensics"] is result["actions"]["like"]["forensics"]
    # 两个动作各在自己失败那一刻抓一份,汇总层不再抓 → 恰好 2 次
    assert page.forensics_calls() == 2


def test_action_exception_also_leaves_forensics(monkeypatch):
    """动作抛异常被兜成 error 时同样留现场 —— 抛异常时最需要知道页面当时什么样。"""
    monkeypatch.setattr(browser_mi, "_open_note_by_title",
                        lambda *a, **k: "https://www.xiaohongshu.com/explore/x")
    monkeypatch.setattr(browser_mi, "_browse_note", lambda *a, **k: None)
    monkeypatch.setattr(browser_mi, "SyncHumanActions", lambda page: _FakeHuman())

    def _boom(*_a, **_k):
        raise RuntimeError("页面炸了")

    monkeypatch.setattr(browser_mi, "_icon_action", _boom)
    page = _ForensicsPage()

    result = browser_mi.interact_with_note(
        page, account_id=9, publisher_user_id="u1", title="标题"
    )

    assert result["actions"]["like"]["forensics"]["url"] == _RAW_FORENSICS["url"]
    assert result["forensics"] is result["actions"]["like"]["forensics"]


def test_comment_failure_carries_forensics():
    """评论入口找不到 → 失败结果带现场(评论链路与点赞收藏同款待遇)。"""
    page = _ForensicsPage(selector="别的选择器")

    result = browser_mi._do_comment(page, _FakeHuman(), "写得真好")

    assert result["reason"] == "comment_entry_not_found"
    assert result["forensics"]["url"] == _RAW_FORENSICS["url"]


def test_comment_empty_text_does_not_collect_forensics():
    """空文案是**入参错误**,在碰页面之前就判掉了 —— 现场与失败原因无关,不抓。"""
    page = _ForensicsPage()

    result = browser_mi._do_comment(page, _FakeHuman(), "  ")

    assert "comment_text_empty" in result["reason"]
    assert "forensics" not in result
    assert page.forensics_calls() == 0


def test_comment_on_note_hands_forensics_to_caller(monkeypatch):
    """comment_on_note 把动作级取证随 error 一起交出去(note_comment.execute 原样透传)。"""
    monkeypatch.setattr(browser_mi, "_open_note_by_title",
                        lambda *a, **k: "https://www.xiaohongshu.com/explore/x")
    monkeypatch.setattr(browser_mi, "_browse_note", lambda *a, **k: None)
    monkeypatch.setattr(browser_mi, "SyncHumanActions", lambda page: _FakeHuman())
    monkeypatch.setattr(
        browser_mi, "_do_comment",
        lambda *a, **k: {"status": "error", "reason": "comment_unverified: 没复核到",
                         "forensics": {"url": "https://x/captcha"}},
    )

    result = browser_mi.comment_on_note(
        _FakePage(), account_id=9, publisher_user_id="u1", title="标题",
        comment_text="写得真好",
    )

    assert result["forensics"] == {"url": "https://x/captcha"}


def test_comment_on_note_success_has_no_forensics(monkeypatch):
    """评论成功不带 forensics 键(与点赞收藏同口径:成功路径零开销)。"""
    monkeypatch.setattr(browser_mi, "_open_note_by_title",
                        lambda *a, **k: "https://www.xiaohongshu.com/explore/x")
    monkeypatch.setattr(browser_mi, "_browse_note", lambda *a, **k: None)
    monkeypatch.setattr(browser_mi, "SyncHumanActions", lambda page: _FakeHuman())
    monkeypatch.setattr(browser_mi, "_do_comment", lambda *a, **k: {"status": "done"})

    result = browser_mi.comment_on_note(
        _FakePage(), account_id=9, publisher_user_id="u1", title="标题",
        comment_text="写得真好",
    )

    assert "forensics" not in result


async def test_note_comment_execute_passes_forensics_through(monkeypatch):
    """服务层不加工:``comment_on_note`` 给什么就往 browser_jobs.result 里落什么。"""
    from app.services import note_comment

    failed = {
        "note_url": "https://www.xiaohongshu.com/explore/x",
        "error": "comment_entry_not_found",
        "forensics": {"url": "https://x/captcha", "engage_bar": False},
    }

    async def _cookies(_account_id):
        return [{"name": "a", "value": "b"}]

    monkeypatch.setattr(note_comment, "load_account_cookies", _cookies)
    monkeypatch.setattr(note_comment, "_comment_sync", lambda *a, **k: failed)

    result = await note_comment.execute(
        7, {"publisher_user_id": "u1", "title": "标题", "text": "写得真好"}
    )

    assert result["forensics"] == {"url": "https://x/captcha", "engage_bar": False}


# ---------------- 矩阵选号 + 登记(schedule_matrix_interact) ----------------


@pytest.fixture
def matrix_db(tmp_path):
    """建一个带全部表的临时 sqlite 文件库,返回路径(sync 侧直连用)。"""
    from sqlalchemy import create_engine

    import app.models  # noqa: F401  触发模型注册
    from app.core.db import Base

    db_path = str(tmp_path / "matrix.db")
    engine = create_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(engine)
    engine.dispose()
    return db_path


def _add_account(db_path: str, account_id: int, name: str, cookie_status: str,
                 user_id: str | None = None) -> None:
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO xhs_accounts (id, name, user_id, status, cookie_status, created_at)"
            " VALUES (?, ?, ?, 'unknown', ?, ?)",
            (account_id, name, user_id, cookie_status, datetime.utcnow().isoformat(sep=" ")),
        )
        conn.commit()


def _add_published_job(db_path: str, job_id: int, account_id: int, title: str) -> None:
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO publish_jobs (id, account_id, title, content, images_json,"
            " topics_json, status, retries, created_at)"
            " VALUES (?, ?, ?, '正文', '[]', '[]', 'published', 0, ?)",
            (job_id, account_id, title, datetime.utcnow().isoformat(sep=" ")),
        )
        conn.commit()


def _read_jobs(db_path: str) -> list[dict]:
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM browser_jobs WHERE kind='matrix_interact' ORDER BY account_id"
        ).fetchall()
    return [dict(r) for r in rows]


def test_schedule_selects_valid_accounts_excluding_publisher(matrix_db):
    """矩阵 = 全部 cookie_status='valid' 排除发布者本人;失效/未知 cookie 号不派。"""
    _add_account(matrix_db, 1, "发布者", "valid", user_id="pub-uid")
    _add_account(matrix_db, 2, "矩阵号A", "valid")
    _add_account(matrix_db, 3, "失效号", "invalid")
    _add_account(matrix_db, 4, "矩阵号B", "valid")
    _add_account(matrix_db, 5, "未检号", "unknown")
    _add_published_job(matrix_db, 77, 1, "边界感是练出来的")

    job_ids = svc.schedule_matrix_interact(matrix_db, 77)

    rows = _read_jobs(matrix_db)
    assert len(job_ids) == 2
    assert [r["account_id"] for r in rows] == [2, 4]  # 发布者 1 / 失效 3 / 未检 5 都不在
    assert all(r["status"] == "queued" and r["operator_id"] == 0 for r in rows)


def test_schedule_payload_carries_locator_and_window(matrix_db):
    """payload 带主页定位三件套 + 窗口内随机 not_before;**不再有 comment 字段**。"""
    _add_account(matrix_db, 1, "发布者", "valid", user_id="pub-uid")
    _add_account(matrix_db, 2, "矩阵号A", "valid")
    _add_published_job(matrix_db, 88, 1, "焦虑发作时的五个自救动作")

    before = datetime.utcnow()
    svc.schedule_matrix_interact(matrix_db, 88)

    payload = repo.get_job_sync(matrix_db, _read_jobs(matrix_db)[0]["id"])["payload"]
    assert payload["publisher_user_id"] == "pub-uid"
    assert payload["title"] == "焦虑发作时的五个自救动作"
    assert payload["source_publish_job_id"] == 88
    # 评论已从矩阵互动移除(独立走 note_comment),payload 里不该再有这个字段
    assert "comment" not in payload
    not_before = datetime.fromisoformat(payload["not_before"])
    assert before <= not_before <= before + timedelta(seconds=svc.WINDOW_SECONDS + 1)


def test_schedule_is_idempotent_per_publish_job(matrix_db):
    """同一发布重复调不重复登记(钩子幂等)。"""
    _add_account(matrix_db, 1, "发布者", "valid", user_id="pub-uid")
    _add_account(matrix_db, 2, "矩阵号A", "valid")
    _add_published_job(matrix_db, 99, 1, "拖延的三个成因")

    assert len(svc.schedule_matrix_interact(matrix_db, 99)) == 1
    assert svc.schedule_matrix_interact(matrix_db, 99) == []
    assert len(_read_jobs(matrix_db)) == 1


def test_schedule_skips_when_publisher_has_no_user_id(matrix_db):
    """发布者没有 user_id → 主页路径无从走起,直接放弃(不猜、不登记)。"""
    _add_account(matrix_db, 1, "发布者", "valid", user_id=None)
    _add_account(matrix_db, 2, "矩阵号A", "valid")
    _add_published_job(matrix_db, 66, 1, "标题在这里")

    assert svc.schedule_matrix_interact(matrix_db, 66) == []
    assert _read_jobs(matrix_db) == []


def test_schedule_never_raises_on_broken_db():
    """登记绝不抛错阻断发布终态:库路径都坏了也只返回空表。"""
    assert svc.schedule_matrix_interact("/nonexistent/dir/nope.db", 1) == []


# ---------------- execute 契约 ----------------


async def test_execute_returns_error_when_payload_incomplete():
    """payload 缺定位信息 → 收敛成 {"error": ...},不抛出、不起浏览器。"""
    result = await svc.execute(2, {"title": "只有标题没有 user_id"})
    assert "error" in result


async def test_execute_converges_locate_failure(monkeypatch):
    """定位类失败(MatrixInteractError)收敛成 {"error": reason},不上抛。"""

    async def fake_load(_account_id):
        return [{"name": "a1", "value": "x", "domain": ".xiaohongshu.com"}]

    def boom(*_args):
        raise browser_mi.MatrixInteractError("note_not_found: 没找到")

    monkeypatch.setattr(svc, "load_account_cookies", fake_load)
    monkeypatch.setattr(svc, "_interact_sync", boom)

    result = await svc.execute(2, {"publisher_user_id": "u1", "title": "标题"})
    assert result == {"error": "note_not_found: 没找到"}


async def test_execute_passes_forensics_through(monkeypatch):
    """发布后互动这条路的服务层不加工:取证原样落 ``browser_jobs.result``。

    这层直接 return 浏览器动作的返回值,取证"顺带就过去了"—— 正因为是顺带的,更要有
    用例钉住:哪天这里改成挑字段重组结果,现场证据会**悄无声息**地丢掉。
    """
    async def fake_load(_account_id):
        return [{"name": "a1", "value": "x", "domain": ".xiaohongshu.com"}]

    failed = {
        "note_url": "https://www.xiaohongshu.com/explore/x",
        "actions": {
            "like": {"status": "error", "reason": "点不动",
                     "forensics": {"url": "https://x/captcha", "engage_bar": False}},
            "collect": {"status": "error", "reason": "点不动"},
        },
        "error": "点赞与收藏均失败",
        "forensics": {"url": "https://x/captcha", "engage_bar": False},
    }
    monkeypatch.setattr(svc, "load_account_cookies", fake_load)
    monkeypatch.setattr(svc, "_interact_sync", lambda *a, **k: failed)

    result = await svc.execute(2, {"publisher_user_id": "u1", "title": "标题"})

    assert result["forensics"] == {"url": "https://x/captcha", "engage_bar": False}
    assert result["actions"]["like"]["forensics"]["engage_bar"] is False


# ---------------- 台账纪律:延时排期 + 非幂等 ----------------


@pytest_asyncio.fixture
async def jobs_db(tmp_path, monkeypatch):
    """临时 sqlite 文件库 + monkeypatch 全局 engine/async_session;yield 库文件路径。"""
    from app.core.db import Base

    import app.models  # noqa: F401

    db_path = str(tmp_path / "jobs.db")
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}", future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr(db_module, "engine", engine)
    monkeypatch.setattr(db_module, "async_session", session_factory)
    try:
        yield db_path
    finally:
        await engine.dispose()


async def test_list_dispatchable_holds_back_future_not_before(jobs_db):
    """未到点的延时任务不派发(执行方不许领了再 sleep 等,会占死浏览器闸)。"""
    future = (datetime.utcnow() + timedelta(seconds=300)).isoformat(sep=" ")
    past = (datetime.utcnow() - timedelta(seconds=5)).isoformat(sep=" ")
    later = await repo.enqueue(
        "matrix_interact", {"not_before": future}, operator_id=0, account_id=2)
    due = await repo.enqueue(
        "matrix_interact", {"not_before": past}, operator_id=0, account_id=3)
    plain = await repo.enqueue("note_export", {}, operator_id=1, account_id=4)

    ids = [r["id"] for r in await repo.list_dispatchable()]
    assert due in ids and plain in ids
    assert later not in ids


async def test_list_dispatchable_tolerates_broken_not_before(jobs_db):
    """not_before 值坏了按立即可派处理,不让任务永久卡死。"""
    jid = await repo.enqueue(
        "matrix_interact", {"not_before": "不是时间"}, operator_id=0, account_id=2)
    assert jid in [r["id"] for r in await repo.list_dispatchable()]


def test_matrix_interact_is_not_idempotent_kind():
    """matrix_interact 非幂等(重跑会取消已点的赞),不得进 _IDEMPOTENT_KINDS。"""
    assert "matrix_interact" not in repo._IDEMPOTENT_KINDS


def test_account_worker_resolves_matrix_interact_execute():
    """account_worker 按 kind 能解析到本服务的 execute(否则子进程会兜底置 error)。"""
    from app import account_worker

    assert account_worker._resolve_execute("matrix_interact") is not None
