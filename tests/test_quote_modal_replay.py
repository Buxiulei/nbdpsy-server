"""用**真号实拍夹具**回放引用弹窗:钉死"下标同序"假设的证伪与现行修法的正确性。

这组用例与 ``test_note_components.py`` 里那些**不是重复**,区别恰恰是本文件存在的理由:

- 那边用**手写**的假页面 —— 手写时人自然照着代码的假设写,于是假设错了测试也跟着错。
  引用功能 100% 失败期间,1615 个单测全绿,就是这么来的;
- 这边的顺序、文案、数量来自 ``scripts/capture_page_scene.py`` 真号只读采集,
  **不是我想象出来的**。

## 夹具揭示的机制(采集之前谁都没想到)

接口返回 21 条,弹窗只渲染 20 张卡,且**从第 3 条起整体错位一格**。缺的那条正是
**当前正在编辑的这篇笔记** —— 弹窗会把它排除掉(不能自己引用自己),于是它之后的每张卡
下标全部偏移。

所以"响应第 i 条 ↔ 第 i 张卡"不是"偶尔不准",是**只要被编辑的笔记不在列表末尾就必然错**。
这也解释了为什么引用功能是 100% 失败而不是间歇失败。
"""

import pytest

from app.browser import note_components as nc
from tests.page_replay import ReplayPage, load_scene

_SCENE = "quote_modal"


@pytest.fixture(autouse=True)
def _fast_settle(monkeypatch):
    """把"分页到齐"的静默窗压到刚够跑一跳:逻辑不变,只缩时间。

    回放里响应是一次性喂完的,真等 1.5 秒纯属浪费(20 篇候选各等一次 = 30 秒)。
    """
    monkeypatch.setattr(nc, "_PAGE_SETTLE_S", 0.01)


@pytest.fixture
def scene():
    return load_scene(_SCENE)


def _cards(scene):
    return scene["dom"][nc._QUOTE_NOTE_CARD]


def _api_notes(scene):
    notes = []
    for rec in scene["api"]:
        if nc._POSTED_API_MARK in rec["url"]:
            notes.extend(((rec.get("body") or {}).get("data") or {}).get("notes") or [])
    return notes


def test_fixture_falsifies_index_assumption(scene):
    """夹具本身就是"同序假设不成立"的证据 —— 这条红了说明该重新采集夹具。"""
    cards, notes = _cards(scene), _api_notes(scene)
    assert cards and notes, "夹具没采到候选卡或接口响应"

    mismatched = 0
    for i in range(min(len(cards), len(notes))):
        title = nc._norm((notes[i] or {}).get("display_title"))
        if title and title not in nc._norm(cards[i]["text"]):
            mismatched += 1

    assert mismatched > 0, (
        "这份夹具里下标恰好对得上 —— 但线上确凿失败过。"
        "多半是采集时选的笔记正好排在列表末尾(它被排除后不影响其余下标),"
        "换一篇靠前的笔记重采。"
    )


def test_edited_note_is_excluded_from_candidates(scene):
    """机制:弹窗排除**当前正在编辑的那篇**,这正是下标错位的来源。

    钉住它是因为——只要还有人想"按下标取卡",这条会立刻提醒他为什么不行。
    """
    cards, notes = _cards(scene), _api_notes(scene)
    edited = scene["source_note_id"]

    assert any(str((n or {}).get("id")) == edited for n in notes), "接口里应含被编辑的那篇"
    edited_title = nc._norm(next(
        n.get("display_title") for n in notes if str((n or {}).get("id")) == edited
    ))
    rendered = [nc._norm(c["text"]) for c in cards]
    assert not any(edited_title in t for t in rendered), (
        f"被编辑的笔记「{edited_title}」不该出现在候选卡里"
    )
    assert len(cards) == len(notes) - 1, "候选卡应恰好比接口少一条(少的就是被编辑的那篇)"


class _ReplayHuman:
    """假拟人层:点击触发用例注入的状态迁移(见 ReplayPage.set_text 的分工说明)。

    这里复刻的两条迁移都来自夹具本身的证据,不是想象:

    - 夹具采于"什么都没选中"那一刻,「确认引用」是 **disabled**(属性 + class 里的裸
      token 双双在案)。所以"点候选卡 → 按钮解禁"是这个弹窗必然存在的一条迁移;
    - **禁用的按钮点了无事发生** —— 2026-08-13 号 7 连续三单 ``quote_not_applied``
      的真相。假拟人层如实照做,生产代码里的禁用闸才测得出来。
    """

    _BUTTON_LABELS = {
        nc._QUOTE_CONFIRM_TEXT, nc._QUOTE_CANCEL_TEXT, nc._QUOTE_TAB_OTHER_TEXT, "我的笔记",
    }
    # 解禁后的 class:摘掉 disabled 属性与裸 token(夹具禁用态原文的对照版)
    _ENABLED_CLS = ("d-button d-button-default d-button-with-content --color-static bold "
                    "--color-bg-fill --color-white custom-button bg-red confirm-width")

    def __init__(self, page):
        self.page = page
        self.clicked = []
        self.scrolls = 0
        self.hovers = []

    def wait(self, *_a, **_kw):
        pass

    def scroll(self, *_a, **_kw):
        self.scrolls += 1

    def hover(self, target=None, *, reason="", **_kw):
        self.hovers.append(
            target.inner_text() if hasattr(target, "inner_text") else str(target)
        )

    def click(self, target, *, reason="", random_offset=True, **_kw):
        text = target.inner_text() if hasattr(target, "inner_text") else str(target)
        self.clicked.append(text)
        if text not in self._BUTTON_LABELS:
            self._enable_confirm()      # 选中候选卡 → 「确认引用」解禁
            return
        # 点「确认引用」→ 引用区变成"引用了 <被选中那张卡的文案>"(真页面的可观察行为);
        # **禁用态下什么都不会发生**
        if text == nc._QUOTE_CONFIRM_TEXT and len(self.clicked) >= 2:
            if nc._quote_confirm_state(self.page).get("enabled"):
                self.page.set_text(nc._QUOTE_CONTAINER, f"引用了 {self.clicked[-2]}")

    def _enable_confirm(self):
        self.page.set_attrs(
            f"{nc._QUOTE_MODAL} button",
            {"disabled": None, "class": self._ENABLED_CLS},
            match_text=nc._QUOTE_CONFIRM_TEXT,
        )

    def type_text(self, target, text, **_kw):
        self.clicked.append(text)


def _drive_real_code(scene, quoted_note_id):
    """用真夹具驱动**生产函数** ``_set_quote_in_modal``,返回它的结果。"""
    page = ReplayPage(scene)
    responses = nc.ComponentResponses()
    responses.attach(page)
    page.emit_recorded(nc._POSTED_API_MARK)
    return _ReplayHuman(page), nc._set_quote_in_modal(
        page, _ReplayHuman(page) if False else _ReplayHuman(page),
        responses, quoted_note_id, 0,
    )


def test_real_code_quotes_every_candidate_on_real_layout(scene):
    """**调用生产函数本身**跑遍真实布局里每一篇可引用笔记,全部必须成功。

    与下面那条"复刻逻辑"的用例不同:这条走的是 ``_set_quote_in_modal`` 真身,
    生产代码一旦改动跑偏,这里会红 —— 复刻逻辑的测试是测不出偏离的。
    """
    edited = scene["source_note_id"]
    checked = 0
    for note in _api_notes(scene):
        note_id = str((note or {}).get("id"))
        if note_id == edited:
            continue
        page = ReplayPage(scene)
        human = _ReplayHuman(page)
        responses = nc.ComponentResponses()
        responses.attach(page)
        page.emit_recorded(nc._POSTED_API_MARK)

        out = nc._set_quote_in_modal(page, human, responses, note_id, 0)

        assert out["status"] == "done", f"{note_id} 失败: {out.get('reason')}"
        checked += 1
    assert checked >= 10, f"只跑了 {checked} 篇,样本太小"


def test_real_code_refuses_note_absent_from_candidates(scene):
    """不在候选里的 id → 明确报 quoted_note_not_in_candidates(交给他人笔记那条路)。"""
    page = ReplayPage(scene)
    responses = nc.ComponentResponses()
    responses.attach(page)
    page.emit_recorded(nc._POSTED_API_MARK)

    out = nc._set_quote_in_modal(page, _ReplayHuman(page), responses, "不存在的id", 0)

    assert out["status"] == "error"
    assert "quoted_note_not_in_candidates" in out["reason"]


def test_real_code_refuses_the_note_being_edited(scene):
    """引用"当前正在编辑的这篇"必然失败 —— 它被弹窗排除,候选里根本没有它。

    钉住它是因为:这正是下标错位的来源,也是运营最容易踩的一种用法。
    """
    page = ReplayPage(scene)
    responses = nc.ComponentResponses()
    responses.attach(page)
    page.emit_recorded(nc._POSTED_API_MARK)

    out = nc._set_quote_in_modal(
        page, _ReplayHuman(page), responses, scene["source_note_id"], 0
    )

    assert out["status"] == "error"


def test_title_matching_picks_the_right_card_on_real_layout(scene):
    """现行修法(按标题在全部卡里找)在真实布局上确实选中正确那张。

    直接复刻 ``_set_quote_in_modal`` 的选卡逻辑:对每一篇**可引用**的笔记都验一遍,
    每篇都必须唯一命中,且命中的卡文案确实含该标题。
    """
    cards, notes = _cards(scene), _api_notes(scene)
    edited = scene["source_note_id"]
    checked = 0

    for note in notes:
        if str((note or {}).get("id")) == edited:
            continue                      # 被编辑的那篇本就不在候选里
        title = nc._norm(note.get("display_title"))
        if not title:
            continue                      # 空标题走「他人笔记」按 id 检索,不归这条路
        hits = [c for c in cards if title in nc._norm(c["text"])]
        assert len(hits) == 1, f"「{title}」命中 {len(hits)} 张(要求恰好 1 张)"
        checked += 1

    assert checked >= 10, f"只验了 {checked} 篇,夹具样本太小说明不了问题"


def test_replay_page_serves_recorded_dom_and_api(scene):
    """回放层本身可用:选择器按实拍返回,接口响应能喂进被动监听那条通路。"""
    page = ReplayPage(scene)

    assert len(page.query_selector_all(nc._QUOTE_NOTE_CARD)) == len(_cards(scene))
    assert page.query_selector(nc._QUOTE_NOTE_CARD) is not None
    # 夹具里没有的选择器返回空 —— 绝不猜(猜就退回手写假页面那条老路)
    assert page.query_selector_all(".never-captured-selector") == []

    responses = nc.ComponentResponses()
    responses.attach(page)
    sent = page.emit_recorded(nc._POSTED_API_MARK)

    assert sent > 0
    assert responses.count(nc._POSTED_API_MARK) == sent


def test_fixture_has_no_live_credentials(scene):
    """夹具是长期资产,不许带会过期的凭据(采集时 xsec_token 已抹除)。"""
    import json

    blob = json.dumps(scene, ensure_ascii=False)
    assert "xsec_token=" not in blob or "xsec_token=SCRUBBED" in blob
    for rec in scene["api"]:
        for note in ((rec.get("body") or {}).get("data") or {}).get("notes") or []:
            assert note.get("xsec_token") in (None, "SCRUBBED")


def test_candidates_are_merged_across_pages(scene):
    """候选列表**分页**,必须把各页合起来 —— 只看最后一页会漏掉第 0 页的全部笔记。

    2026-08-03 回放实测:弹窗打开时连发 tab=1&page=0(11 条)与 page=1(10 条),
    两页一起渲染成 20 张卡。原实现取 latest 只见 10 条,于是第 0 页那 11 篇一律被报成
    「候选列表里没有」—— 生产日志里的 quoted_note_not_in_candidates 就是这么来的。
    """
    page = ReplayPage(scene)
    responses = nc.ComponentResponses()
    responses.attach(page)
    pages = page.emit_recorded(nc._POSTED_API_MARK)
    assert pages >= 2, "这份夹具只有一页,证明不了合并;换个笔记多的账号重采"

    merged, _exhausted, _rounds = nc._wait_all_candidate_notes(
        page, _ReplayHuman(page), responses, 0
    )

    per_page = [
        len(((r.get("body") or {}).get("data") or {}).get("notes") or [])
        for r in scene["api"] if nc._POSTED_API_MARK in r["url"]
    ]
    assert len(merged) == sum(per_page), f"合并后应有 {sum(per_page)} 条,实得 {len(merged)}"
    assert len(merged) > max(per_page), "合并后必须多于任何单页(否则等于没合并)"
    # 顺序是选卡算法的依据,去重不能打乱它
    first_page_first = ((scene["api"][0].get("body") or {}).get("data") or {})["notes"][0]
    assert str(merged[0]["id"]) == str(first_page_first["id"]), "应保持首次出现的顺序"


def test_fixture_confirm_button_starts_disabled(scene):
    """夹具本身就是「确认引用」**禁用态**的证据 —— 缺陷 B 的判据全靠它。

    这条红了说明夹具是在"已经选中某张卡"的时刻采的,禁用态的原文就丢了,
    ``_quote_confirm_state`` 的两路判据(disabled 属性 + class 裸 token)也就没了背书。
    """
    buttons = scene["dom"][f"{nc._QUOTE_MODAL} button"]
    confirm = next(b for b in buttons if b["text"] == nc._QUOTE_CONFIRM_TEXT)

    assert "disabled" in confirm["attrs"], "夹具里「确认引用」应带 disabled 属性"
    assert nc._QUOTE_DISABLED_TOKEN in (confirm["attrs"].get("class") or "").split(), (
        "夹具里「确认引用」的 class 应含**独立 token** disabled"
    )

    page = ReplayPage(scene)
    assert nc._quote_confirm_state(page) == {
        "found": True, "enabled": False, "cls": confirm["attrs"]["class"],
    }


def test_real_code_refuses_to_click_disabled_confirm(scene):
    """选中态一直没生效 → 报 ``quote_card_select_not_applied``,**绝不点那颗禁用按钮**。

    这是 2026-08-13 号 7 三单失败的病灶:老实现点完卡不回读就去点「确认引用」,
    而没选中时它本就是禁用的,点了无事发生,最后以 ``quote_not_applied`` 的面目出现
    —— 把排查引向"确认按钮/引用区",而真正坏掉的是**上一步**。
    """
    page = ReplayPage(scene)
    responses = nc.ComponentResponses()
    responses.attach(page)
    page.emit_recorded(nc._POSTED_API_MARK)
    human = _ReplayHuman(page)
    human._enable_confirm = lambda: None      # 点卡片不解禁:复刻"选中静默失效"
    target = next(
        n for n in _api_notes(scene)
        if str(n.get("id")) != scene["source_note_id"] and n.get("display_title")
    )

    out = nc._set_quote_in_modal(page, human, responses, str(target["id"]), 0)

    assert out["status"] == "error"
    assert "quote_card_select_not_applied" in out["reason"]
    assert nc._QUOTE_CONFIRM_TEXT not in human.clicked


def test_candidates_exhausted_when_scrolling_yields_nothing(scene):
    """夹具是**静态**的:滚了也不会有新页 → 判定"翻到底了"(exhausted=True)。

    ``exhausted`` 是降级门的依据:只有"确实把本账号笔记翻完了都没有它",
    才谈得上"这多半是别人的笔记"。
    """
    page = ReplayPage(scene)
    responses = nc.ComponentResponses()
    responses.attach(page)
    page.emit_recorded(nc._POSTED_API_MARK)
    human = _ReplayHuman(page)

    notes, exhausted, rounds = nc._wait_all_candidate_notes(page, human, responses, 0)

    assert exhausted is True
    assert rounds == human.scrolls, "滚了几轮要如实报出来(候选覆盖面靠它)"
    assert len(notes) == len(_api_notes(scene))
    assert human.scrolls == nc._QUOTE_SCROLL_IDLE_ROUNDS, "到底后不该继续空转"
    assert human.hovers, "滚之前必须先 hover 到候选卡上(落点打错就滚了别的容器)"


def test_scroll_anchor_stays_above_modal_footer(scene):
    """滚轮落点必须选**页脚之上**那张卡 —— 夹具里第 3、4 行卡已经在页脚之下了。

    页脚(「取消」)在 y=632,而卡片行分别在 y=188/386/584/782。拿末尾那张当落点
    等于把鼠标移到弹窗外面去滚,正是本仓"滚轮打在侧栏"的老毛病。
    """
    page = ReplayPage(scene)
    cards = page.query_selector_all(nc._QUOTE_NOTE_CARD)
    footer_top = page.query_selector_all(f"{nc._QUOTE_MODAL} button")
    limit = next(
        b.bounding_box()["y"] for b in footer_top
        if b.inner_text() == nc._QUOTE_CANCEL_TEXT
    )

    anchor = nc._pick_scroll_anchor(page, cards)
    box = anchor.bounding_box()

    assert box["y"] + box["height"] / 2 < limit, "落点卡落在弹窗页脚之下"
    assert anchor is not cards[-1], "夹具里最后一张卡在页脚之下,不该被选成落点"


def test_first_page_note_is_quotable(scene):
    """**第 0 页的笔记必须引用得了** —— 这是分页 bug 最直接的用户可见后果。"""
    first_page = ((scene["api"][0].get("body") or {}).get("data") or {})["notes"]
    target = next(
        n for n in first_page if str(n.get("id")) != scene["source_note_id"]
    )
    page = ReplayPage(scene)
    responses = nc.ComponentResponses()
    responses.attach(page)
    page.emit_recorded(nc._POSTED_API_MARK)

    out = nc._set_quote_in_modal(page, _ReplayHuman(page), responses, str(target["id"]), 0)

    assert out["status"] == "done", f"第 0 页的笔记引用失败: {out.get('reason')}"
