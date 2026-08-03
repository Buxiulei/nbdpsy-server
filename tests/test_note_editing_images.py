"""更新页图片增删(`app/browser/note_editing_images.py`)单测。

两层测试,分工是刻意的:

1. **回放夹具层**(`ReplayPage` + `update_editor_images` 真号实拍):选择器假设对**真快照**
   跑 —— 图数怎么数、按序号认卡、图片上传通道怎么唯一命中。手写假页面测不出这些:手写时
   人自然照着代码的假设写(引用弹窗"同序假设"活了两个月、单测全绿而功能 100% 失败,就是
   这么来的,见 `tests/page_replay.py` 模块 docstring)。
2. **轻量假页面层**:交互**序列**与失败语义 —— 降序、每张前重新滚动+重新定位、hover 先于
   取删除钮、落点复核不过一次都不点、静默失效重试同张封顶后停手、意外弹窗不乱点。这些是
   "点了会怎样",夹具**故意不记**(写进夹具等于把期望混进证据)。

假页面的**几何直接取自真夹具**(6 张 82x82、删除钮 20x20 压在右上角、卡间距 90):这样
"点哪个坐标落在谁身上"不是我编的,而是实拍的。行为(点了会不会真删)才是用例注入的。

设计附录 C / E2 的钉子在 `test_remove_one_of_six_drops_count_by_one`:真号那次牺牲品只有
1 张图,删到 0 被平台「至少 1 张」拦下,**「-1」路径从未在真号验证过**,只能在这里用 6 图
模拟补验(真号补验在 T7)。
"""

import pytest

from app.browser import note_editing_images as nei
from tests.page_replay import ReplayPage, load_scene

_SCENE = "update_editor_images"


# ══════════════════════════════════════════════════════════════════
# 第一层:真号夹具回放(选择器假设对实拍跑)
# ══════════════════════════════════════════════════════════════════


@pytest.fixture
def scene():
    return load_scene(_SCENE)


@pytest.fixture
def replay(scene):
    return ReplayPage(scene)


def test_container_judgement_counts_six_and_ordinals_are_sequential(replay):
    """判据 A(容器计数)与序号连续性在实拍上成立:6 张,文案恰好 1..6。

    这两件事撑起后面所有图片操作:数得准才敢动手,序号即文案才能"按身份"而不是"按位置"
    去认要删的那张。
    """
    assert nei._container_count(replay) == 6
    assert nei._ordinals(replay) == ["1", "2", "3", "4", "5", "6"]
    assert nei._ordinals_are_sequential(replay, 6) is True
    # 数量对但序号不是 1..N 时必须判假 —— 删一张之后"数对了、序号没重绘"的半截态就长这样
    assert nei._ordinals_are_sequential(replay, 5) is False


def test_dual_judgement_agrees_on_the_real_snapshot(replay):
    """双判据在实拍上一致:容器数 6 == 图片区序号拼接「1 2 3 4 5 6」取 6。

    判据 B 的选择器是裁决换过的:设计原文写的是右侧预览列 `.image-preview` 的「1/N」页码,
    那个元素真实存在(8-03 泛化探针见过),但**没进 T1 采集清单、不在正式夹具里** ——
    没有夹具背书的选择器在 CI 里永远证伪不了,正是这套回放测试要根治的病。改用同为实拍、
    现在就能被这份夹具证伪的序号拼接串。真正独立的 `.image-preview` 待 T7 正式采证后升级
    (`count_images` docstring 里留了路标)。
    """
    assert replay.query_selector(nei._IMG_AREA).inner_text() == "1 2 3 4 5 6"
    assert nei._ordinal_sequence_total(replay) == 6
    assert nei.count_images(replay) == 6


def test_gate_passes_on_the_real_snapshot_and_rejects_a_stale_expectation(replay):
    """闸的"通过 / 不符"两态跑在实拍上:6 张实拍,expected=6 放行、expected=5 零点击拒绝。"""
    assert nei.image_gate(replay, 6) == {"status": "ok", "count": 6}

    stale = nei.image_gate(replay, 5)
    assert stale["status"] == "error"
    assert stale["count"] == 6
    assert stale["reason"].startswith("image_count_mismatch")


def test_gate_refuses_with_zero_clicks_when_ordinals_are_scrambled(scene):
    """闸的"读不出"态:序号拼接不是 1..N(渲染半截/序号错乱)→ 不可确认 + `count=None`。

    页面结构仍是实拍,只有图片区文案由**用例注入**(page_replay 的分工:夹具给结构、用例给
    迁移;把这种异常态写进夹具等于把期望混进证据)。与"权限读不出就不提交"同一条纪律:
    认知不确定时零动作是唯一安全解。
    """
    page = ReplayPage(scene)
    page.set_text(nei._IMG_AREA, "1 2 3 4 5 7")   # 第 6 张的序号渲染成了 7

    out = nei.image_gate(page, 6)

    assert nei.count_images(page) is None
    assert out["status"] == "error"
    assert out["count"] is None
    assert out["reason"].startswith("image_count_unconfirmable")


def test_pick_card_hits_exactly_one_by_ordinal_text(replay):
    """按序号文案认卡:实拍上每个序号唯一命中,且命中的就是那张。"""
    for ordinal in range(1, 7):
        picked = nei._pick_card(replay, ordinal)
        assert picked["status"] == "ok"
        assert picked["card"].inner_text() == str(ordinal)


def test_pick_card_refuses_unknown_ordinal(replay):
    """序号找不到 → error,**绝不退化成"按位置取第 k 个"**。

    位置退化正是引用弹窗栽过的那个坑(接口第 i 条 ↔ 弹窗第 i 张卡),在删图上等于删错图。
    """
    picked = nei._pick_card(replay, 7)

    assert picked["status"] == "error"
    assert picked["reason"].startswith("image_ordinal_not_found")


def test_image_input_anchor_uniquely_hits_the_jpg_multiple_channel(replay):
    """图片批量通道在实拍的 3 个 file input 里唯一命中,且 pdf 通道被排除。

    3 个 input 长得很像用途完全不同:一个 jpg+multiple(要的就是它)、一个同 accept 无
    multiple(疑似单张替换,多图一次灌入会丢图)、一个 accept 是 pdf/doc(灌进去等于往
    笔记里塞附件)。"取第一个"在今天这份实拍上碰巧也能对,但那是运气 —— DOM 顺序不是契约。
    """
    picked = nei._pick_image_input(replay)

    assert picked["status"] == "ok"
    chosen = picked["input"]
    assert chosen.get_attribute("accept") == ".jpg,.jpeg,.png,.webp"
    assert chosen.get_attribute("multiple") is not None
    assert nei._DOC_ACCEPT_MARK not in (chosen.get_attribute("accept") or "")

    # 另外两个确实还在页面上 —— 说明这个谓词是在"三选一",不是"页面上只有一个所以蒙对了"
    all_inputs = replay.query_selector_all(nei._FILE_INPUT)
    assert len(all_inputs) == 3
    assert sum(1 for el in all_inputs
               if nei._DOC_ACCEPT_MARK in (el.get_attribute("accept") or "")) == 1
    assert sum(1 for el in all_inputs
               if el.get_attribute("accept") == ".jpg,.jpeg,.png,.webp"
               and el.get_attribute("multiple") is None) == 1


def test_image_input_refuses_to_fall_back_when_multiple_channel_disappears(scene):
    """改版模拟:带 multiple 的那个没了 → error,**绝不回退**到剩下那两个中的任何一个。

    页面用的还是实拍元素(只是把 multiple 那个摘掉),不是手写的假 input。回退是致命的:
    退到单张通道 = 多图丢图,退到 pdf 通道 = 往笔记塞附件。
    """
    survivors = [d for d in scene["dom"][nei._FILE_INPUT]
                 if "multiple" not in (d["attrs"] or {})]
    assert len(survivors) == 2, "实拍里除 multiple 那个之外应还剩两个 file input"
    page = ReplayPage({**scene, "dom": {**scene["dom"], nei._FILE_INPUT: survivors}})

    picked = nei._pick_image_input(page)

    assert picked["status"] == "error"
    assert picked["reason"].startswith("image_input_not_found")


def test_close_btn_geometry_used_by_the_fake_page_comes_from_the_fixture(scene):
    """守住第二层测试的前提:假页面的几何确实抄自实拍,不是我随手编的坐标。

    这条一红就说明夹具重采过而假页面没跟上 —— 那时第二层的"点这个坐标落在谁身上"就不再
    可信,必须先同步几何再看别的用例。
    """
    cards = scene["dom"][nei._IMG_CONTAINER]
    buttons = scene["extra"]["close_btn_after_hover"]

    assert (cards[0]["rect"]["width"], cards[0]["rect"]["height"]) == (82, 82)
    assert (buttons[0]["rect"]["width"], buttons[0]["rect"]["height"]) == (20, 20)
    assert _CARD_STEP == cards[1]["rect"]["x"] - cards[0]["rect"]["x"]
    assert _BTN_DX == buttons[0]["rect"]["x"] - cards[0]["rect"]["x"]
    assert _BTN_DY == buttons[0]["rect"]["y"] - cards[0]["rect"]["y"]
    # 删除钮压在图的上边缘之上(实拍 y 更小)—— 视口判定要把这一圈算进去
    assert _BTN_DY < 0


# ══════════════════════════════════════════════════════════════════
# 第二层:轻量假页面(交互序列与失败语义)
# ══════════════════════════════════════════════════════════════════

# 几何常量取自真夹具(见上面那条守门用例)
_FIXTURE = load_scene(_SCENE)
_CARD0 = _FIXTURE["dom"][nei._IMG_CONTAINER][0]["rect"]
_CARD1 = _FIXTURE["dom"][nei._IMG_CONTAINER][1]["rect"]
_BTN0 = _FIXTURE["extra"]["close_btn_after_hover"][0]["rect"]
_CARD_W = _CARD0["width"]
_CARD_H = _CARD0["height"]
_CARD_STEP = _CARD1["x"] - _CARD0["x"]
_BTN_W = _BTN0["width"]
_BTN_H = _BTN0["height"]
_BTN_DX = _BTN0["x"] - _CARD0["x"]
_BTN_DY = _BTN0["y"] - _CARD0["y"]

_VIEWPORT_H = 900.0
_SCROLL_STEP = 500.0


class _El:
    """假元素:只给被测代码真用到的那几个读操作 + 文件灌入。"""

    def __init__(self, page, *, text="", attrs=None, rect=None, visible=True,
                 position=None, kind=""):
        self._page = page
        self._text = text
        self._attrs = attrs or {}
        self._rect = rect
        self._visible = visible
        self.position = position   # 卡片在当前图序里的 0-based 位置
        self.kind = kind

    def inner_text(self):
        return self._text

    def get_attribute(self, name):
        return self._attrs.get(name)

    def is_visible(self):
        return self._visible

    def bounding_box(self):
        return dict(self._rect) if self._rect else None

    def query_selector(self, sel):
        if sel == nei._CLOSE_BTN and self.kind == "card":
            # 记事件:用来断言"hover 之后才去取删除钮"
            self._page.events.append(("close_btn_lookup", self._text))
            return self._page._close_btn_el(self.position)
        return None

    def set_input_files(self, paths):
        self._page.uploaded.append((self._attrs.get("accept"), list(paths)))
        self._page.on_upload(list(paths))


class _Editor:
    """假更新页:图序 + 点击副作用 + 只读取证。每个"坏行为"都对应一条实测/未闭环风险。

    - ``silent_clicks``:窄按钮首点静默失效(活动「关联」28x68 真号实测,20x20 的 close-btn
      同族风险更高,附录 C / E2 的 -1 路径又从未真号验证);
    - ``dialog_text``:点删除后弹出确认框(E2 未闭环的那一支);
    - ``area_text``:图片区文案(判据 B)与各卡序号对不上的半截态;
    - ``renumber=False``:删掉了但序号没重绘的半截态;
    - ``upload_renders=False``:灌了文件但没渲染(E3 灌入行为从未实证);
    - ``viewport_readable=False``:读不出视口高度 → 不确定就不点。
    """

    def __init__(self, *, count=6, area_text=None, scroll_y=0.0,
                 silent_clicks=0, dialog_text=None, renumber=True,
                 upload_renders=True, viewport_readable=True, hit_override=None,
                 file_inputs=None, area=True):
        self.labels = [str(i) for i in range(1, count + 1)]
        # 图片区文案(判据 B)默认**跟着各卡序号走**(真页面就是各卡序号顺次拼接);
        # 显式给一个串 = 注入"判据 B 说了别的话"的半截态
        self._area_text = area_text
        self.scroll_y = scroll_y
        self.silent_clicks = silent_clicks
        self.dialog_text = dialog_text
        self.renumber = renumber
        self.upload_renders = upload_renders
        self.viewport_readable = viewport_readable
        self.hit_override = hit_override
        self.area = area
        self.dialogs = []
        self.events = []       # 交互流水:断言"顺序"用
        self.clicks = []       # 落点坐标
        self.clicked_labels = []
        self.uploaded = []     # (accept, paths) —— 断言"pdf 通道一次都没被灌"
        self.polls = 0
        self._inputs = file_inputs if file_inputs is not None else [
            {"accept": ".jpg,.jpeg,.png,.webp", "multiple": ""},
            {"accept": ".jpg,.jpeg,.png,.webp"},
            {"accept": ".pdf,.doc,.docx,.ppt,.pptx"},
        ]

    # ---- 几何(抄自实拍)----
    def _card_rect(self, position):
        return {"x": _CARD0["x"] + position * _CARD_STEP, "y": _CARD0["y"] + self.scroll_y,
                "width": _CARD_W, "height": _CARD_H}

    def _btn_rect(self, position):
        card = self._card_rect(position)
        return {"x": card["x"] + _BTN_DX, "y": card["y"] + _BTN_DY,
                "width": _BTN_W, "height": _BTN_H}

    def _close_btn_el(self, position):
        return _El(self, attrs={"class": "close-btn hoverShow"},
                   rect=self._btn_rect(position), position=position, kind="close")

    # ---- 选择器 ----
    def query_selector_all(self, sel):
        if sel == nei._IMG_CONTAINER:
            return [_El(self, text=label, rect=self._card_rect(i), position=i, kind="card")
                    for i, label in enumerate(self.labels)]
        if sel == nei._IMG_AREA:
            if not self.area:
                return []
            text = " ".join(self.labels) if self._area_text is None else self._area_text
            return [_El(self, text=text)]
        if sel == nei._FILE_INPUT:
            return [_El(self, attrs=dict(a), kind="input") for a in self._inputs]
        if sel in nei._DIALOG_SELECTORS:
            return [_El(self, text=t) for t in self.dialogs] if sel == ".d-modal" else []
        return []

    def query_selector(self, sel):
        hits = self.query_selector_all(sel)
        return hits[0] if hits else None

    def wait_for_timeout(self, _ms):
        self.polls += 1

    # ---- 只读取证 ----
    def evaluate(self, js, arg=None):
        if "innerHeight" in js:
            if not self.viewport_readable:
                raise RuntimeError("假页面:视口高度读不出")
            return _VIEWPORT_H
        if "elementFromPoint" in js:
            return self._hit_test(*arg)
        raise AssertionError(f"假页面收到未预期的 evaluate: {js[:60]!r}")

    def _hit_test(self, x, y):
        self.events.append(("hit_test", round(x), round(y)))
        if self.hit_override is not None:
            return dict(self.hit_override)
        position = self._btn_at(x, y)
        if position is None:
            return {"tag": "DIV", "cls": "", "on_close_btn": False, "card_text": None}
        return {"tag": "I", "cls": "close-btn hoverShow", "on_close_btn": True,
                "card_text": self.labels[position]}

    def _btn_at(self, x, y):
        for position in range(len(self.labels)):
            r = self._btn_rect(position)
            if r["x"] <= x <= r["x"] + r["width"] and r["y"] <= y <= r["y"] + r["height"]:
                return position
        return None

    # ---- 副作用 ----
    def scroll(self, direction):
        self.events.append(("scroll", direction))
        self.scroll_y += _SCROLL_STEP if direction == "up" else -_SCROLL_STEP

    def click_at(self, x, y):
        self.clicks.append((x, y))
        position = self._btn_at(x, y)
        assert position is not None, f"点在了 ({x},{y}),那里根本没有删除按钮"
        label = self.labels[position]
        self.clicked_labels.append(label)
        self.events.append(("click", label))
        if self.dialog_text:
            self.dialogs.append(self.dialog_text)   # 弹确认框,图不删
            return
        if self.silent_clicks > 0:
            self.silent_clicks -= 1                 # 窄按钮静默失效
            return
        del self.labels[position]
        if self.renumber:
            self.labels = [str(i + 1) for i in range(len(self.labels))]

    def on_upload(self, paths):
        if not self.upload_renders:
            return
        self.labels += [str(len(self.labels) + i + 1) for i in range(len(paths))]


class _Human:
    """假拟人层:记录 hover/scroll,点击只接受**已复核过的坐标**。"""

    def __init__(self, page):
        self.page = page
        self.hovers = []

    def wait(self, *_a, **_kw):
        pass

    def scroll(self, direction="down", distance=None):
        self.page.scroll(direction)

    def hover(self, target, *, reason=""):
        self.hovers.append(target.inner_text())
        self.page.events.append(("hover", target.inner_text()))

    def click(self, target, *, reason="", **_kw):
        assert isinstance(target, tuple), (
            "删除按钮必须点已经 elementFromPoint 复核过的坐标,不许直接把句柄丢给 click"
            "(句柄路径会走 scroll_into_view_if_needed,复核结论当场作废)"
        )
        self.page.click_at(*target)


@pytest.fixture
def fast_waits(monkeypatch):
    """把两处轮询超时压到毫秒级:假页面的 wait_for_timeout 不真等,超时路径不该跑满 90 秒。"""
    monkeypatch.setattr(nei, "_REMOVE_SETTLE_TIMEOUT_S", 0.02)
    monkeypatch.setattr(nei, "_UPLOAD_RENDER_TIMEOUT_S", 0.02)


def _run(page, indexes):
    human = _Human(page)
    return nei.remove_images(page, human, indexes), human


# ---------------- 图数与闸(通过 / 不符两态)----------------


def test_count_images_returns_count_when_both_judgements_agree():
    """双判据一致(容器 6 张 且 图片区文案「1 2 3 4 5 6」)→ 返回 6。"""
    assert nei.count_images(_Editor(count=6)) == 6


def test_count_images_returns_none_when_judgements_disagree():
    """容器 6 张但图片区文案只拼到 5 → 不可确认。差一张就是删错一张,不许放行。"""
    assert nei.count_images(_Editor(count=6, area_text="1 2 3 4 5")) is None


def test_count_images_returns_none_when_ordinals_are_not_sequential():
    """序号跳号/重号(1 2 3 4 5 7)→ 不可确认。

    数量对但序号不连续,恰恰是"按序号认卡"最危险的场景:数得过、认得错。
    """
    assert nei.count_images(_Editor(count=6, area_text="1 2 3 4 5 7")) is None


def test_count_images_returns_none_when_image_area_missing():
    """图片区整个不在(页面没进到编辑器/改版)→ 不可确认,**不是 0 张**。"""
    assert nei.count_images(_Editor(count=6, area=False)) is None


def test_gate_passes_when_expected_matches():
    page = _Editor(count=6)

    assert nei.image_gate(page, 6) == {"status": "ok", "count": 6}
    assert page.events == [], "闸只读,不该产生任何交互"


def test_gate_rejects_mismatch_with_zero_clicks():
    """expected 对不上实数 → error,一次点击都不发(台账认知过期时的零点击退出)。"""
    page = _Editor(count=6)

    out = nei.image_gate(page, 5)

    assert out["status"] == "error"
    assert out["count"] == 6
    assert out["reason"].startswith("image_count_mismatch")
    assert page.clicks == [] and page.events == []


# ---------------- 删除:顺序 / 前提 / 计数 ----------------


def test_remove_one_of_six_drops_count_by_one():
    """**附录 C / E2 的钉子**:6 图删 1 张 → 计数 -1、序号重排 1..5。

    真号只验到"删除按钮定位正确、点得中",牺牲品只有 1 张图,删到 0 被平台「至少 1 张」
    拦下 —— 「-1」这条路径至今没有真号背书。这里用 6 图把它补上(真号补验在 T7)。
    """
    page = _Editor(count=6)

    out, _human = _run(page, [3])

    assert out == {"status": "done", "removed": 1, "count_before": 6, "count_after": 5}
    assert page.clicked_labels == ["3"]
    assert page.labels == ["1", "2", "3", "4", "5"], "删完必须重排成 1..5"


def test_remove_goes_in_descending_order():
    """多张一起删按**降序**下手:删掉第 2 张后原第 5 张会左移成第 4 张,升序删就删错图。"""
    page = _Editor(count=6)

    out, _human = _run(page, [2, 5])

    assert out["status"] == "done" and out["removed"] == 2
    assert out["count_before"] == 6 and out["count_after"] == 4
    assert page.clicked_labels == ["5", "2"], "必须先删大序号"
    assert page.labels == ["1", "2", "3", "4"]


def test_remove_dedupes_repeated_indexes():
    """重复下标只删一次:同一张删两次 = 多删一张真图(REST 那层也拦,这层不靠上游守身)。"""
    page = _Editor(count=6)

    out, _human = _run(page, [4, 4])

    assert out["removed"] == 1 and out["count_after"] == 5
    assert page.clicked_labels == ["4"]


def test_remove_with_no_indexes_touches_nothing():
    page = _Editor(count=6)

    out, _human = _run(page, [])

    assert out == {"status": "done", "removed": 0, "count_before": 6, "count_after": 6}
    assert page.events == []


def test_each_image_is_rescrolled_and_relocated_before_touching_it():
    """附录 B / E8 硬要求:每张动手前**重新滚进视口 + 重新定位**,绝不复用上一步坐标。

    实拍里弹窗一开图片区就被顶到 y=-815(视口上方),此时按上一步的坐标点 = 点在顶栏上,
    动作静默失败而代码看起来一切正常(文字版丢话题同款陷阱)。所以:①滚动方向要能**向上**;
    ②滚完必须重新 query 拿新句柄新坐标。
    """
    page = _Editor(count=6, scroll_y=-950.0)   # 复刻 E8:图片区在视口上方

    out, human = _run(page, [2, 5])

    assert out["status"] == "done"
    assert ("scroll", "up") in page.events, "目标在视口上方时必须往上滚,只会下滚的循环回不来"
    # 每张图各自走完 [滚动…] → hover → 取删除钮 → 落点复核 → 点击
    per_image = _events_by_image(page.events)
    for label in ("5", "2"):
        kinds = [e[0] for e in per_image[label]]
        assert kinds == ["hover", "close_btn_lookup", "hit_test", "click"], (
            f"第 {label} 张的交互序列不对:{kinds}"
        )
    # 落点坐标随滚动位改变 —— 两张图不可能共用一个 y,共用就说明坐标被复用了
    assert len({round(y) for _x, y in page.clicks}) >= 1
    assert human.hovers == ["5", "2"]


def test_hover_precedes_close_button_lookup():
    """删除钮 `.close-btn.hoverShow` 悬停才显形(附录 B / E2):不 hover 就取矩形会拿到未显形态。"""
    page = _Editor(count=6)

    _run(page, [4])

    kinds = [e[0] for e in page.events if e[0] in ("hover", "close_btn_lookup")]
    assert kinds == ["hover", "close_btn_lookup"]


def test_no_click_when_hit_test_says_the_point_is_another_image():
    """落点复核不过 → **一次点击都不发**(相邻两张图的删除按钮长得一模一样,只有所属容器能区分)。"""
    page = _Editor(count=6, hit_override={
        "tag": "I", "cls": "close-btn hoverShow", "on_close_btn": True, "card_text": "5",
    })

    out, _human = _run(page, [2])

    assert out["status"] == "error"
    assert out["reason"].startswith("close_point_mismatch")
    assert page.clicks == [] and out["removed"] == 0
    assert page.labels == ["1", "2", "3", "4", "5", "6"], "笔记的图一张都不该少"


def test_no_click_when_hit_test_lands_on_something_that_is_not_a_close_button():
    page = _Editor(count=6, hit_override={
        "tag": "DIV", "cls": "img-container", "on_close_btn": False, "card_text": "2",
    })

    out, _human = _run(page, [2])

    assert out["status"] == "error"
    assert out["reason"].startswith("close_point_mismatch")
    assert page.clicks == []


def test_no_click_when_viewport_height_unreadable():
    """读不出视口高度就没法确认目标在视口内 → 不确定就不点。"""
    page = _Editor(count=6, viewport_readable=False)

    out, _human = _run(page, [2])

    assert out["status"] == "error"
    assert out["reason"].startswith("viewport_unreadable")
    assert page.clicks == []


def test_remove_refuses_when_count_unconfirmable_before_any_click():
    page = _Editor(count=6, area=False)

    out, _human = _run(page, [2])

    assert out["status"] == "error"
    assert out["reason"].startswith("image_count_unconfirmable")
    assert out["removed"] == 0 and out["count_before"] is None
    assert page.events == []


def test_remove_refuses_unknown_ordinal_without_clicking():
    """页面上没有那个序号(台账/请求与页面不同步)→ error,绝不按位置猜一张删。"""
    page = _Editor(count=6)

    out, _human = _run(page, [9])

    assert out["status"] == "error"
    assert out["reason"].startswith("image_ordinal_not_found")
    assert page.clicks == [] and page.labels == ["1", "2", "3", "4", "5", "6"]


# ---------------- 删除:静默失效 / 弹窗 / 半截态 ----------------


def test_retries_same_image_three_times_then_stops_without_touching_the_next(fast_waits):
    """静默失效 → **只重试同一张**,封顶 3 次后立刻停手,**不再删下一张**。

    出处:活动「关联」28x68 首点静默失效(无 toast、零网络请求、状态不翻转)的真号实测;
    close-btn 只有 20x20,同族风险更高。停手是关键 —— 删除不可逆,"这张卡住了那先删下一张"
    会在语义已经不可信的页面上继续制造不可逆动作。
    """
    page = _Editor(count=6, silent_clicks=99)

    out, human = _run(page, [2, 5])

    assert out["status"] == "error"
    assert out["reason"].startswith("image_remove_not_applied")
    assert out["removed"] == 0
    assert page.clicked_labels == ["5", "5", "5"], "只许重试同一张,且封顶 3 次"
    assert human.hovers == ["5", "5", "5"], "第 2 张连 hover 都不该发生"
    assert page.labels == ["1", "2", "3", "4", "5", "6"]


def test_first_click_silent_then_second_click_works(fast_waits):
    """首点静默失效、同样的点法再点一次就成 —— 与活动按钮的实测行为一致,不该被判失败。"""
    page = _Editor(count=6, silent_clicks=1)

    out, _human = _run(page, [3])

    assert out["status"] == "done" and out["removed"] == 1
    assert page.clicked_labels == ["3", "3"]
    assert page.labels == ["1", "2", "3", "4", "5"]


def test_partial_removal_then_failure_is_error_with_true_removed_count(fast_waits):
    """删成一张后第二张卡住 → 整单 **error**,但如实上报 removed=1。

    部分删成不是"部分成功":残缺态只存在于编辑器前端(附录 C / E4 实证纯前端态),编排层
    据此弃提交,笔记原样未动。谎报 done 才会让残缺态被真发布出去。
    """
    page = _Editor(count=6)
    human = _Human(page)
    original_click_at = page.click_at

    def click_at(x, y):
        """第一张删成之后开始静默失效。"""
        original_click_at(x, y)
        if len(page.clicked_labels) == 1:
            page.silent_clicks = 99

    page.click_at = click_at
    out = nei.remove_images(page, human, [2, 5])

    assert out["status"] == "error"
    assert out["removed"] == 1
    assert out["count_before"] == 6
    assert out["count_after"] == 5
    assert page.clicked_labels == ["5", "2", "2", "2"]


def test_unexpected_dialog_after_click_aborts_and_never_guesses_buttons(fast_waits):
    """点完冒出没见过的弹窗且图数没变 → 如实 error,把弹窗文案带出去交人工。

    E2 的"确认弹窗"分支从未在真号闭环。**绝不猜弹窗按钮乱点**:猜错要么删错图(不可逆),
    要么点到别的破坏性操作。引用弹窗盖住发布按钮致三连误诊,已经付过一次学费。
    """
    page = _Editor(count=6, dialog_text="确定删除这张图片吗?")

    out, human = _run(page, [3])

    assert out["status"] == "error"
    assert out["reason"].startswith("unexpected_dialog_after_close_click")
    assert "确定删除这张图片吗?" in out["reason"]
    assert page.clicked_labels == ["3"], "弹窗出现后不该再点任何东西(含弹窗按钮)"
    assert page.dialogs == ["确定删除这张图片吗?"], "弹窗原样留着交人工"
    assert page.labels == ["1", "2", "3", "4", "5", "6"]
    assert out["removed"] == 0


def test_pre_existing_dialog_is_not_mistaken_for_a_confirm_box(fast_waits):
    """页面本来就挂着的弹层(引用弹窗等)不算"点出来的确认框" —— 差集判定,不是"有没有"。"""
    page = _Editor(count=6, silent_clicks=99)
    page.dialogs = ["选择笔记"]

    out, _human = _run(page, [3])

    assert out["reason"].startswith("image_remove_not_applied"), (
        "本来就在的弹层不该把失败误判成 unexpected_dialog"
    )


def test_half_rendered_renumber_stops_immediately(fast_waits):
    """删掉了但序号没重绘(半截态)→ 判为未生效,下一次定位直接失败停手,**不会连删**。

    只数数量不看序号的话,这里会被判成功;而序号正是下一张的身份依据,认错就删错。
    """
    page = _Editor(count=6, renumber=False)

    out, _human = _run(page, [5])

    assert out["status"] == "error"
    assert page.clicked_labels == ["5"], "半截态下不许继续点第二次(那会删掉另一张真图)"
    assert len(page.labels) == 5


# ---------------- 追加 ----------------


def test_add_images_appends_to_the_tail():
    page = _Editor(count=6)
    human = _Human(page)

    out = nei.add_images(page, human, ["/tmp/a.jpg", "/tmp/b.jpg"])

    assert out == {"status": "done", "added": 2, "count_before": 6, "count_after": 8}
    assert page.labels == [str(i) for i in range(1, 9)], "新图拼在末尾且整体重排 1..8"
    assert page.uploaded == [(".jpg,.jpeg,.png,.webp", ["/tmp/a.jpg", "/tmp/b.jpg"])]


def test_add_images_errors_when_upload_does_not_render(fast_waits):
    """灌进去了但没渲染 → error,不做"大概成了"。

    E3(灌入行为)从未在真号验证过,第一次真验证在 T7 —— 未实证的路径必须 fail-safe。
    """
    page = _Editor(count=6, upload_renders=False)
    human = _Human(page)

    out = nei.add_images(page, human, ["/tmp/a.jpg"])

    assert out["status"] == "error"
    assert out["reason"].startswith("image_add_not_rendered")
    assert out["added"] == 0 and out["count_before"] == 6 and out["count_after"] == 6


def test_add_images_refuses_when_channel_not_unique_and_never_uploads():
    """通道不唯一(改版后两个 jpg+multiple)→ error,**一个文件都不灌**。"""
    page = _Editor(count=6, file_inputs=[
        {"accept": ".jpg,.jpeg,.png,.webp", "multiple": ""},
        {"accept": ".jpg,.png", "multiple": ""},
        {"accept": ".pdf,.doc"},
    ])
    human = _Human(page)

    out = nei.add_images(page, human, ["/tmp/a.jpg"])

    assert out["status"] == "error"
    assert out["reason"].startswith("image_input_ambiguous")
    assert page.uploaded == []
    assert out["added"] == 0 and out["count_after"] == 6


def test_add_images_never_falls_back_to_the_bare_or_pdf_input():
    """只剩单张通道 + pdf 通道 → error。**绝不**回退:退到单张 = 多图丢图,退到 pdf = 塞附件。"""
    page = _Editor(count=6, file_inputs=[
        {"accept": ".jpg,.jpeg,.png,.webp"},          # 无 multiple:单张替换
        {"accept": ".pdf,.doc,.docx,.ppt,.pptx"},     # 文档通道:绝不碰
    ])
    human = _Human(page)

    out = nei.add_images(page, human, ["/tmp/a.jpg", "/tmp/b.jpg"])

    assert out["status"] == "error"
    assert out["reason"].startswith("image_input_not_found")
    assert page.uploaded == [], "一个文件都不许灌 —— 尤其不许灌进 pdf 通道"


def test_add_images_refuses_when_count_unconfirmable():
    page = _Editor(count=6, area=False)
    human = _Human(page)

    out = nei.add_images(page, human, ["/tmp/a.jpg"])

    assert out["status"] == "error"
    assert out["reason"].startswith("image_count_unconfirmable")
    assert page.uploaded == []


def test_add_images_rejects_empty_list():
    page = _Editor(count=6)

    out = nei.add_images(page, _Human(page), [])

    assert out["status"] == "error"
    assert out["reason"].startswith("add_images_empty")
    assert page.uploaded == []


# ---------------- 返回形状(T6 按这个接线)----------------


def test_return_shapes_match_the_contract(fast_waits):
    """四个入口的返回 dict 形状固定 —— T6 编排按这个读,少一个键就是接线断。"""
    assert set(nei.image_gate(_Editor(count=6), 6)) == {"status", "count"}
    assert set(nei.image_gate(_Editor(count=6), 5)) == {"status", "count", "reason"}

    done, _h = _run(_Editor(count=6), [2])
    assert set(done) == {"status", "removed", "count_before", "count_after"}
    failed, _h = _run(_Editor(count=6, silent_clicks=99), [2])
    assert set(failed) == {"status", "reason", "removed", "count_before", "count_after"}

    page = _Editor(count=6)
    added = nei.add_images(page, _Human(page), ["/tmp/a.jpg"])
    assert set(added) == {"status", "added", "count_before", "count_after"}
    page2 = _Editor(count=6, upload_renders=False)
    add_failed = nei.add_images(page2, _Human(page2), ["/tmp/a.jpg"])
    assert set(add_failed) == {"status", "reason", "added", "count_before", "count_after"}


def _events_by_image(events):
    """把交互流水按"当前正在处理哪张图"分组(滚动事件不带图,归到下一张)。"""
    grouped = {}
    current = None
    for event in events:
        if event[0] in ("hover", "close_btn_lookup", "click"):
            current = event[1]
            grouped.setdefault(current, []).append(event)
        elif event[0] == "hit_test" and current is not None:
            grouped[current].append(event)
    return grouped
