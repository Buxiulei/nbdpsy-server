"""用 **quote_modal_lazyload 真号夹具**回放引用弹窗的两处 2026-08-13 缺陷。

夹具机械转换自探针快照 ``quote_modal_mechanics_20260813_171433.json``(号 6 只读采集),
提供了 ``quote_modal`` 那份夹具当初没采的两样东西:

- **真正的滚动容器** ``.select-note-modal__list-wrap``(``overflow-y:auto``,
  矩形 x=423 y=184 754×424,即可视区下沿 y=608);
- 点候选卡之后平台弹的 **toast** ``.d-new-toast`` → 「非公开可见笔记,无法引用」。

这两样正是两处缺陷的判据来源:

**缺陷 A(候选停在 22 篇)**:滚轮落点原先取"页脚上沿之上的候选卡"。页脚上沿 y=632,
而滚动容器可视区下沿 y=608 —— 中间 24px 是**被 overflow 裁掉、但矩形仍然读得到**的死带。
第一轮滚动之后落在死带里的卡就会被选成落点,鼠标于是停在滚动容器**外面**,
滚轮打给不可滚的 ``d-modal-content``,后两轮空转 → 判"到底了"。实测三轮响应
1→2→3、候选 12→22→40,平台压根没到底。

**缺陷 B(号 7 三单失败的真因)**:目标卡矩形 ``x=1037 y=727 w=136 h=182``,而滚动容器
可视区是 ``[184, 608]`` —— 卡在可视区**下方 119px**,点击落点(卡心 y≈818)落在弹窗之外,
**根本没点到卡**,确认钮当然不解禁。原实现的 ``_bring_card_into_view`` 滚不进也返回成功
("尽力而为"),于是照着区外坐标点了下去。

同一份快照里那条「非公开可见笔记,无法引用」的 toast **不是**平台对目标笔记的裁决:
2026-08-14 人工在同一目标(002c0c)上手动引用**成功**。它多半是点在卡外命中其它元素的
下游现象,何况 toast 根容器还会累加历史通知。所以本文件里它只被当作"出现了这条提示"
这个事实,断言也只锁"原文照抄 + 归因警告",不再锁"该换目标"。
"""

import pytest

from app.browser import note_components as nc
from tests.page_replay import ReplayPage, load_scene

_SCENE = "quote_modal_lazyload"


@pytest.fixture(autouse=True)
def _fast_settle(monkeypatch):
    """把纯等待窗压到近零:等的是真实时钟,回放里毫无意义。"""
    monkeypatch.setattr(nc, "_PAGE_SETTLE_S", 0.01)
    monkeypatch.setattr(nc, "_QUOTE_SELECT_SETTLE_S", 0.01)
    monkeypatch.setattr(nc, "_QUOTE_SCROLL_WAIT_S", 0.01)


@pytest.fixture
def scene():
    return load_scene(_SCENE)


class _Human:
    """假拟人层:只记录动作,**不注入任何状态迁移**(滚了也不动)。

    这正是"滚不动"的实况:目标卡永远进不了可视区。想让滚动真的生效用 ``_ScrollingHuman``。
    """

    def __init__(self):
        self.clicked = []
        self.hovers = []
        self.scrolls = 0
        self.directions = []
        self.distances = []

    def wait(self, *_a, **_kw):
        pass

    def scroll(self, direction="down", distance=None, *_a, **_kw):
        self.scrolls += 1
        self.directions.append(direction)
        self.distances.append(distance)

    def hover(self, target=None, *, reason="", **_kw):
        self.hovers.append(target)

    def click(self, target, *, reason="", random_offset=True, **_kw):
        self.clicked.append(
            target.inner_text() if hasattr(target, "inner_text") else str(target)
        )


class _ScrollingHuman(_Human):
    """滚动会真的把候选卡整体推走 —— 真页面上滚列表的可观察后果。

    容器 ``scrollHeight`` 1575 > 可视区 424(探针实测),所以"目标卡能被滚进可视区"
    在真页面上确实成立;不给这条迁移,凡是"先滚进可视区再点"的路径在回放里都走不通。
    """

    _STEP = 200.0

    def __init__(self, page):
        super().__init__()
        self.page = page

    def scroll(self, direction="down", distance=None, *_a, **_kw):
        super().scroll(direction, distance)
        self.page.shift_rects(
            nc._QUOTE_NOTE_CARD, -self._STEP if direction == "down" else self._STEP
        )


def _wrap_rect(scene):
    return scene["dom"][nc._QUOTE_LIST_WRAP][0]["rect"]


def _card_rect(page):
    return page.query_selector(nc._QUOTE_NOTE_CARD).bounding_box()


# ---------------- 缺陷 A:滚轮落点 ----------------


def test_fixture_exposes_the_dead_band_between_list_wrap_and_footer(scene):
    """夹具本身就是死带的证据 —— 缺陷 A 的判据全靠这 24px。

    这条红了说明夹具换了布局,滚轮落点的推理要重新做,别急着改代码去迁就它。
    """
    rect = _wrap_rect(scene)
    footer_top = next(
        b["rect"]["y"] for b in scene["dom"][f"{nc._QUOTE_MODAL} button"]
        if b["text"] == nc._QUOTE_CANCEL_TEXT
    )
    wrap_bottom = rect["y"] + rect["height"]

    assert wrap_bottom < footer_top, (
        f"滚动容器下沿 {wrap_bottom} 应在页脚上沿 {footer_top} 之上 —— 中间那段就是死带,"
        "死带宽度为零的话老判据根本不会出错,整条推理要重来"
    )


def test_scroll_anchor_lands_inside_the_real_scroll_container(scene):
    """落点取**滚动容器可见矩形中心**,而不是任何一张候选卡。

    容器中心永远落在 ``overflow-y:auto`` 那个矩形里面,滚轮必然被它消费
    —— 探针实测 ``prevented_count=0``,冒泡到 list-wrap 就是能滚。
    """
    page = ReplayPage(scene)
    cards = page.query_selector_all(nc._QUOTE_NOTE_CARD)
    rect = _wrap_rect(scene)

    anchor = nc._pick_scroll_anchor(page, cards)

    assert isinstance(anchor, tuple), "落点应是坐标(容器中心),不再是某张卡的元素"
    x, y = anchor
    assert rect["x"] < x < rect["x"] + rect["width"], "落点横坐标不在滚动容器里"
    assert rect["y"] < y < rect["y"] + rect["height"], "落点纵坐标不在滚动容器里"


def test_scroll_anchor_avoids_the_card_that_sits_in_the_dead_band(scene):
    """夹具里那张卡中心 y=818 —— 老判据会选中它,新落点必须离它远远的。

    818 既在页脚(632)之下、又在视口(794)之外:老实现把鼠标移到那儿去滚,
    自然一格都不动。
    """
    page = ReplayPage(scene)
    card_rect = page.query_selector(nc._QUOTE_NOTE_CARD).bounding_box()
    card_center_y = card_rect["y"] + card_rect["height"] / 2
    wrap = _wrap_rect(scene)

    _x, y = nc._pick_scroll_anchor(page, page.query_selector_all(nc._QUOTE_NOTE_CARD))

    assert card_center_y > wrap["y"] + wrap["height"], "夹具里这张卡本就在容器可视区外"
    assert y < wrap["y"] + wrap["height"], "落点仍落在容器可视区之外"


def test_scroll_anchor_is_clamped_into_the_viewport(scene, monkeypatch):
    """容器大半在视口外时落点要夹回视口 —— 移到视口外不算悬停,滚轮无从谈起。

    与 ``note_comments_read._clamp_to_viewport`` 同款手法(那边是 y≈2099 的评论区)。
    """
    page = ReplayPage(scene)
    page.snapshot = {**scene, "viewport": {"width": 400, "height": 300}}

    x, y = nc._pick_scroll_anchor(page, page.query_selector_all(nc._QUOTE_NOTE_CARD))

    assert 0 < x < 400 and 0 < y < 300, f"落点 ({x}, {y}) 没夹进 400x300 的视口"


def test_scroll_anchor_falls_back_to_cards_when_wrap_is_absent():
    """滚动容器读不出时退回原有的候选卡启发式 —— 不能因为改判据把老路也砍了。

    ``quote_modal`` 那份夹具采集时没记 list-wrap,正好当"容器缺失"的真实样本。
    """
    page = ReplayPage(load_scene("quote_modal"))
    cards = page.query_selector_all(nc._QUOTE_NOTE_CARD)
    assert page.query_selector(nc._QUOTE_LIST_WRAP) is None, "这份夹具本就没采 list-wrap"

    anchor = nc._pick_scroll_anchor(page, cards)

    assert anchor in cards, "退回路径应给出一张候选卡"


# ---------------- 缺陷 A:返回 False 的语义 ----------------


def _scene_without_cards(scene):
    """把候选卡从场景里摘掉(容器还在)——复刻"卡还没渲染出来"那一瞬。"""
    dom = {k: v for k, v in scene["dom"].items() if k != nc._QUOTE_NOTE_CARD}
    return {**scene, "dom": dom}


def test_scroll_still_happens_when_cards_are_absent_but_wrap_exists(scene):
    """列表里一张卡都没有、但滚动容器在 → **照滚不误**。

    卡没渲染出来只说明"这一瞬没东西可看",不说明"平台没有下一页"。
    """
    page = ReplayPage(_scene_without_cards(scene))
    human = _Human()

    assert nc._scroll_candidate_list(page, human) is True
    assert human.scrolls == 1
    assert human.hovers, "滚之前必须先把鼠标移进滚动容器"


def test_scroll_reports_nothing_to_scroll_only_when_wrap_and_cards_both_gone(scene):
    """容器和候选卡**双双**不在,才是真的没得可翻(唯一允许返回 False 的情形)。"""
    empty = {**scene, "dom": {}}
    human = _Human()

    assert nc._scroll_candidate_list(ReplayPage(empty), human) is False
    assert human.scrolls == 0


def test_absent_cards_no_longer_declare_candidates_exhausted(scene):
    """缺陷 A 的上层后果:选不出落点被当成"翻到底了",一轮都不肯再试。

    改后要按**正常停滞计数**走 —— 先滚满 ``_QUOTE_SCROLL_IDLE_ROUNDS`` 轮确认真没进展,
    才谈得上 exhausted。生产停在 22 篇正是"一轮都没再试"的后果。
    """
    page = ReplayPage(_scene_without_cards(scene))
    responses = nc.ComponentResponses()
    responses.attach(page)
    human = _Human()

    _notes, exhausted, rounds = nc._wait_all_candidate_notes(page, human, responses, 0)

    assert human.scrolls == nc._QUOTE_SCROLL_IDLE_ROUNDS, (
        "没卡就直接判到底 = 缺陷 A;必须真滚过、确认没进展才收工"
    )
    assert rounds == human.scrolls, "滚了几轮要如实报出来(候选覆盖面靠它)"
    assert exhausted is True, "确实滚过又确实没进展,这时判到底是对的"


# ---------------- 缺陷 B 之一:toast 只报事实,不下裁决 ----------------


def _pick_card(page):
    return page.query_selector(nc._QUOTE_NOTE_CARD)


def test_platform_toast_reports_the_prompt_as_a_fact_not_a_verdict(scene):
    """点卡后出现「无法引用」→ 报 ``quote_target_not_quotable`` 并带**平台原文**。

    但只报"出现了这条提示"这个**事实**:2026-08-14 人工在同一目标(002c0c)上手动引用
    **成功**,证伪了上一版"这是平台裁决、重试无用、该换目标"的说法。提示可能来自点击落在
    候选卡之外命中的其它元素,toast 容器还会累加历史通知 —— 文案里必须留下这条警告,
    否则运营照着"该换目标"去做,就会绕开真正的几何缺陷。
    """
    page = ReplayPage(scene)
    human = _ScrollingHuman(page)

    out = nc._select_quote_card(page, human, _pick_card(page), "刘琼")

    assert out["status"] == "error"
    assert "quote_target_not_quotable" in out["reason"]
    assert "非公开可见笔记，无法引用" in out["reason"], "必须原样带上平台文案"
    assert "不能据此断定该笔记不可被引用" in out["reason"], "必须给出归因警告"
    for verdict in ("重试无用", "平台侧的裁决", "请换一篇引用目标"):
        assert verdict not in out["reason"], f"「{verdict}」已被人工操作证伪,不该再出现"


def test_prompt_path_still_clicks_only_once(scene):
    """出现提示后**不重复点** —— 重点一次只是多一次真号动作,并不能改变什么。"""
    page = ReplayPage(scene)
    human = _ScrollingHuman(page)

    nc._select_quote_card(page, human, _pick_card(page), "刘琼")

    assert len(human.clicked) == 1, f"出现提示后不该重点,实际点了 {len(human.clicked)} 次"


# ---------------- 候选卡不在可视区(2026-08-13 号 7 三单真因)----------------


def test_fixture_target_card_sits_below_the_scroll_container_viewport(scene):
    """夹具本身就是真因的证据:目标卡在滚动容器可视区**下方**,点它等于点在弹窗外。

    这条红了说明夹具换了几何,整条推理要重做,别急着改代码去迁就它。
    """
    card = _card_rect(ReplayPage(scene))
    wrap = _wrap_rect(scene)

    assert card["y"] > wrap["y"] + wrap["height"], (
        f"卡片纵向 [{card['y']}, {card['y'] + card['height']}] 应整段落在容器可视区 "
        f"[{wrap['y']}, {wrap['y'] + wrap['height']}] 之下"
    )


def test_card_that_never_enters_the_viewport_is_never_clicked(scene):
    """滚不进可视区 → 报 ``quote_card_offscreen`` 且**一次都不点**。

    老实现在这里"尽力而为":滚满三轮仍在区外也照点,点到的是弹窗外的别的元素,
    失败于是以「平台拒绝 / 选中没生效」的面目出现在下游 —— 归因整整偏了一轮。
    """
    page = ReplayPage(scene)
    human = _Human()          # 滚了也不动:复刻"就是滚不进去"

    out = nc._select_quote_card(page, human, _pick_card(page), "刘琼")

    assert out["status"] == "error"
    assert "quote_card_offscreen" in out["reason"]
    assert human.clicked == [], "卡在区外还点,点到的就是别的元素"
    assert human.scrolls == nc._QUOTE_CARD_VIEW_TRIES, "判失败之前要把重试轮数用满"


def test_offscreen_detail_carries_both_rects(scene):
    """回执必须带**卡片矩形与可视区矩形两组数** —— 判据就是这两个纵向区间。

    下次再出同类失败,回执自己就能说明是不是同一堵墙,不必再开一次真号取证。
    """
    page = ReplayPage(scene)
    wrap = _wrap_rect(scene)

    out = nc._select_quote_card(page, _Human(), _pick_card(page), "刘琼")

    assert out["card_rect"]["y"] == 727 and out["card_rect"]["height"] == 182
    assert out["view_rect"]["y"] == wrap["y"] and out["view_rect"]["height"] == wrap["height"]
    # 结构化字段之外,文案里也要看得见(日志里只有 reason 那一行)
    assert "727" in out["reason"] and "909" in out["reason"], "文案缺卡片纵向区间"
    assert "184" in out["reason"] and "608" in out["reason"], "文案缺可视区纵向区间"


def test_card_is_clicked_once_it_has_been_scrolled_into_view(scene):
    """滚得进去就照常点 —— 修法不能把"能走通的路"一起堵死。"""
    page = ReplayPage(scene)
    human = _ScrollingHuman(page)

    nc._select_quote_card(page, human, _pick_card(page), "刘琼")

    assert human.scrolls >= 1, "卡在区外却一次都没滚"
    assert len(human.clicked) == 1, "滚进可视区之后必须真的点一次"
    card, wrap = _card_rect(page), _wrap_rect(scene)
    assert card["y"] >= wrap["y"], "点的时候卡片上沿仍在可视区之上"
    assert card["y"] + card["height"] <= wrap["y"] + wrap["height"], "卡片下沿仍在可视区之外"


def test_card_above_the_viewport_scrolls_back_up(scene):
    """卡被滚到列表**顶部之上**同样点不中 → 往回滚,而不是继续往下滚。

    上沿这条边界是新加的:老判据只看"在不在页脚之下",卡滚过头了照样判"在区里"。
    也不能指望 ``human.click`` 自带的 ``scroll_into_view_if_needed`` —— 本仓早有记录:
    它认的是"元素技术上可见",被祖先容器裁掉时它不滚。
    """
    page = ReplayPage(scene)
    page.shift_rects(nc._QUOTE_NOTE_CARD, -700.0)   # 727 → 27:整段跑到容器上沿之上
    human = _Human()

    out = nc._select_quote_card(page, human, _pick_card(page), "刘琼")

    assert human.directions == ["up"] * nc._QUOTE_CARD_VIEW_TRIES, (
        f"卡在可视区上方却往下滚:{human.directions}"
    )
    assert "quote_card_offscreen" in out["reason"], "滚不回来一样不许点"


def test_scroll_distance_matches_the_gap_instead_of_the_random_default(scene):
    """差多少滚多少(带增益),而不是默认的随机 300~800 一脚跨过去。

    容器只有 424 高、卡 182 高,随机步长跨过头再跨回来,三次重试全耗在震荡上 ——
    而现在"滚不进"是**判失败**的,震荡就成了假失败。
    """
    page = ReplayPage(scene)
    card, wrap = _card_rect(page), _wrap_rect(scene)
    gap = (card["y"] + card["height"]) - (wrap["y"] + wrap["height"])
    human = _Human()

    nc._select_quote_card(page, human, _pick_card(page), "刘琼")

    assert human.distances[0] == int(gap * nc._QUOTE_CARD_SCROLL_GAIN)
    assert human.distances[0] >= nc._QUOTE_CARD_SCROLL_MIN_PX


def test_scroll_distance_falls_back_to_default_without_container_geometry():
    """退回页脚判据时算不出差值 → ``distance=None``,交回默认随机距离(拟人层的事)。"""
    page = ReplayPage(load_scene("quote_modal"))
    state = nc._card_view_state(page, _pick_card(page))

    assert nc._card_scroll_plan(state) == ("down", None)


def test_falls_back_to_footer_when_the_scroll_container_is_absent():
    """滚动容器读不出 → 退回原来的页脚判据(带告警),不能因为改判据把老路砍了。

    ``quote_modal`` 那份夹具采集时没记 list-wrap,正好当"容器缺失"的真实样本。
    """
    page = ReplayPage(load_scene("quote_modal"))
    assert page.query_selector(nc._QUOTE_LIST_WRAP) is None, "这份夹具本就没采 list-wrap"

    state = nc._card_view_state(page, _pick_card(page))

    assert state["mode"] == "footer"
    assert state["footer_top"] == 632, "退回判据取的是页脚(「取消」)上沿"
    assert state["inside"] is True, "首行卡在页脚之上,退回判据下应判在区内"


def test_view_state_without_any_boundary_does_not_block_the_click():
    """卡片矩形/两条边界都读不出 → ``inside=None``,按"没判据不折腾"处理照常点。

    没有判据时硬判失败,等于把一条本来能走通的路堵死 —— 这类反向故障比假绿更难查。
    """
    page = ReplayPage({"dom": {}})
    human = _Human()

    assert nc._card_view_state(page, None)["inside"] is None
    assert nc._bring_card_into_view(page, human, None)["ok"] is True
    assert human.scrolls == 0


def test_no_toast_still_reports_select_not_applied():
    """点了、没 toast、确认钮也没解禁 → 仍报 ``quote_card_select_not_applied`` 并重试一次。

    这条守的是缺陷 B 的修法**没有**把老判据顶掉:toast 只是多一条更准的归因,
    "点击静默失效"那种故障还得照旧认出来。
    """
    page = ReplayPage(load_scene("quote_modal"))
    assert page.query_selector(nc._QUOTE_TOAST) is None, "这份夹具没有 toast(正是本例要的)"
    human = _Human()

    out = nc._select_quote_card(page, human, _pick_card(page), "无 toast")

    assert out["status"] == "error"
    assert "quote_card_select_not_applied" in out["reason"]
    assert len(human.clicked) == 2, "无 toast 的静默失效仍要重试一次(关随机偏移)"
    assert "落在滚动容器可视区外" in out["reason"], (
        "静默失效多半就是没点中卡,回执要把人先引向矩形数据"
    )


def test_error_codes_are_documented_without_the_falsified_verdict():
    """三个错误码的语义写在 docstring 里,且**不再断言平台裁决/重试无用**。

    那句断言 2026-08-14 被人工操作证伪:同一目标手动引用成功。留着它就会继续把排查
    引向"换个目标",而真正坏掉的是候选卡根本没被点到。
    """
    doc = nc._select_quote_card.__doc__ or ""

    for code in ("quote_card_offscreen", "quote_target_not_quotable",
                 "quote_card_select_not_applied"):
        assert code in doc
    assert "重试无用" not in doc and "平台侧的裁决" not in doc


# ---------------- 候选覆盖面(平台候选窗口那堵墙)----------------


def test_not_in_candidates_reports_the_coverage_it_actually_got():
    """"不在候选里"必须带上**候选覆盖面**,否则分不清两堵墙。

    2026-08-13 取证:平台给候选列表设了上限(号 7 翻到 49 篇≈上限 50,只覆盖到
    2026-02-13;2025-05-18 那篇**永远翻不到**),这和懒加载翻页是**两件独立的事**。
    只报"候选 N 篇里没有它",调用方没法判断是"目标不存在"还是"目标在窗口外"。
    三个数一起看就够判:翻满了还没有 = 窗口外,换目标;没翻满就停 = 翻页还有问题。
    """
    page = ReplayPage(load_scene("quote_modal"))
    responses = nc.ComponentResponses()
    responses.attach(page)
    page.emit_recorded(nc._POSTED_API_MARK)

    out = nc._set_quote_in_modal(page, _Human(), responses, "不存在的id", 0)

    assert "quoted_note_not_in_candidates" in out["reason"]
    assert out["candidates_count"] > 0, "翻到几篇要如实报"
    assert out["scroll_rounds"] == nc._QUOTE_SCROLL_IDLE_ROUNDS, "滚了几轮要如实报"
    # 夹具里候选带 time 字段(倒序,末尾那条最老)
    assert out["candidates_oldest"], "候选窗口的下边界(最老一篇)必须给出"
    assert out["candidates_oldest"] in out["reason"], "覆盖面也要写进报错文案(日志里要看得见)"


def test_coverage_oldest_falls_back_to_title_then_omits():
    """``time`` 取不到就退回标题;两者都没有则**省略**该字段,绝不编一个。"""
    assert nc._candidates_oldest([{"time": "2026-02-13 09:00"}]) == "2026-02-13 09:00"
    assert nc._candidates_oldest([{"display_title": "最老那篇"}]) == "最老那篇"
    assert nc._candidates_oldest([{}]) is None
    assert nc._candidates_oldest([]) is None


def test_coverage_oldest_takes_the_last_entry_because_the_api_is_newest_first():
    """接口按时间**倒序**返回,末尾那条才是翻到的最老一篇。"""
    notes = [{"time": "2026-08-01 10:39"}, {"time": "2026-02-13 09:00"}]

    assert nc._candidates_oldest(notes) == "2026-02-13 09:00"
