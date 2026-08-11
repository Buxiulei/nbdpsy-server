"""文本编辑步骤(标题 / 正文)的离线测试:选择器对真号夹具跑,交互顺序对假页面跑。

两半分工是刻意的,各治一种病:

- **回放夹具**(``update_editor_images``,真号实拍):治"手写假页面照抄代码的假设"。
  选择器唯一命中、标题现值在 ``value``、正文是 tiptap/ProseMirror —— 这些只有对着实拍
  才有意义,手写页面时人自然照着代码写,写错了测试也跟着错(见 ``page_replay`` docstring
  里引用弹窗同序假设那个例子)。夹具改不得,对不上只能重采。
- **轻量假页面**(记录调用序列):治"顺序/重试这类行为没人验"。``ReplayPage`` 不支持
  ``evaluate``(JS 求值没法离线回放)也不支持键盘/坐标,所以交互路径只能这么测。
  断言的是**动作序列**:清空先于输入、每次重试都重新定位、``value=""`` 只清不输、
  目标在视口外先滚再点、读回不等一律 error。

不测的:真正的键盘落字与 React 重渲染(``evaluate`` 边界,交 T7 真号验收)。
"""

import re

import pytest

from app.browser import note_editing as ne
from tests.page_replay import ReplayPage, load_scene

_SCENE = "update_editor_images"
# 夹具里正文框是按这个宽松选择器采的(复合类选择器不在夹具键里),
# 下面用它取出实拍元素,再对 ``_BODY_EDITOR`` 的谓词做同一份筛选
_BODY_CAPTURE_SELECTOR = "div[contenteditable='true']"


@pytest.fixture
def scene():
    return load_scene(_SCENE)


@pytest.fixture
def replay(scene):
    return ReplayPage(scene)


# ==================== 回放夹具:选择器假设 ====================


@pytest.mark.unit
def test_title_selector_hits_exactly_one_and_value_holds_current_title(replay):
    """标题框在实拍上唯一命中,且**现值在 value 属性**(不是 innerText)。

    唯一性是重点:多命中就得引入下标或额外限定,那正是引用弹窗踩过的坑。
    读法更是重点:读错地方会把"没改动"误判成"改动了"(innerText 恒为空,永远"不等")。
    """
    hits = replay.query_selector_all(ne._TITLE_INPUT)

    assert len(hits) == 1, f"标题框命中 {len(hits)} 个(要求恰好 1 个)"
    assert hits[0].get_attribute("value"), "标题框应带已发布笔记的现有标题(现值在 value)"
    assert hits[0].inner_text() == "", "input 的 innerText 恒空 —— 这就是不能拿它当现值的原因"


@pytest.mark.unit
def test_body_write_selector_predicate_matches_exactly_one_on_snapshot(replay):
    """正文**写入**选择器的复合谓词在实拍上唯一命中。

    ``_BODY_EDITOR`` 是 ``div.tiptap.ProseMirror[contenteditable='true']``,复合类选择器
    不在夹具键里(``ReplayPage`` 按采集时的选择器原样回答,绝不猜),所以这里把谓词拆开
    对实拍 attrs 做同一份筛选 —— 验的是"这个谓词在真页面上唯一命中",与 file input
    那条用例同一套路数。

    写入必须用这个特异的而不是裸 ``div[contenteditable='true']``:写错框 = 把正文打进
    别的地方,而提交是全量覆盖语义。
    """
    classes = re.findall(r"\.([A-Za-z][\w-]*)", ne._BODY_EDITOR)
    assert classes == ["tiptap", "ProseMirror"], (
        f"写入选择器的类谓词变了({classes}) —— 变了就得重新对夹具验一次唯一性"
    )

    matched = [
        el
        for el in replay.query_selector_all(_BODY_CAPTURE_SELECTOR)
        if all(cls in (el.get_attribute("class") or "").split() for cls in classes)
        and el.get_attribute("contenteditable") == "true"
    ]

    assert len(matched) == 1, (
        f"tiptap+ProseMirror+contenteditable 的谓词应恰好命中 1 个,实得 {len(matched)} 个"
    )
    assert matched[0].inner_text(), "正文框应带已发布笔记的现有正文(现值在 innerText)"


@pytest.mark.unit
def test_body_read_js_tries_the_write_selector_first():
    """读回的候选序列**第一个**就是写入用的那个选择器。

    读写必须落在同一个元素上,否则"读回全等"验的是另一个框,静默失败。裸
    ``div[contenteditable='true']`` 只当最后兜底(与 ``note_components._BODY_TEXT_JS``
    同款候选序列)。
    """
    candidates = re.findall(r'"([^"]+)"', ne._BODY_READ_JS)

    assert candidates[0] == ne._BODY_EDITOR
    assert candidates[-1] == _BODY_CAPTURE_SELECTOR, "裸 contenteditable 只能垫底兜底"


@pytest.mark.unit
def test_topics_dropped_source_matches_snapshot_body(replay):
    """``topics_dropped`` 的提取口径对实拍正文成立(E9:chip 的 innerText 就是纯文本形态)。"""
    body = replay.query_selector(_BODY_CAPTURE_SELECTOR).inner_text()

    # 实拍这篇正文里带话题就提得出、不带就是空列表 —— 两种都合法,这里锁的是"不炸、口径一致"
    assert ne.extract_topics(body) == re.findall(r"#([^#\[\]]+)\[话题\]#", body)


# ==================== 轻量假页面:交互顺序与重试 ====================


def _box(*, y=300.0, h=40.0, ih=900.0, x=700.0, w=600.0, sel="fake"):
    """造一个 ``_BOX_JS`` 形状的矩形。默认落在视口内(不触发滚动)。"""
    return {"x": x, "y": y, "w": w, "h": h, "sel": sel, "ih": ih}


def _next(seq):
    """按序取下一个;取完之后一直返回最后一个(模拟"页面不再变化")。"""
    if not seq:
        return None
    return seq.pop(0) if len(seq) > 1 else seq[0]


class FakeHuman:
    """只记调用序列的假拟人层 —— 断言的正是"以什么顺序做了什么"。"""

    def __init__(self, click_raises=None):
        self.calls = []
        self._click_raises = click_raises

    def click(self, target, *, reason="", **_kw):
        self.calls.append(("click", target))
        if self._click_raises:
            raise self._click_raises

    def type_text(self, target, text, *, clear_first=False, click_first=True):
        assert target is None and click_first is False, (
            "必须走「坐标聚焦 + 不持句柄」那条路(React 重渲染会让旧句柄脱离)"
        )
        assert clear_first is False, "清空要拆成独立的 press_key 两步(value='' 时只清不输)"
        self.calls.append(("type", text))

    def press_key(self, key, *, reason="", **_kw):
        self.calls.append(("key", key))

    def scroll(self, direction="down", distance=None):
        self.calls.append(("scroll", direction))

    def wait(self, *_a, **_kw):
        pass

    def kinds(self, kind):
        return [c for c in self.calls if c[0] == kind]


class FakePage:
    """按 JS 常量分派的假页面:只回放"读"(取矩形 / 取文本),并记录读的顺序。"""

    def __init__(self, *, boxes=None, titles=None, bodies=None):
        self._boxes = list(boxes or [])
        self._titles = list(titles or [])
        self._bodies = list(bodies or [])
        self.reads = []

    def evaluate(self, js, arg=None):
        if js is ne._BOX_JS:
            self.reads.append("box")
            return _next(self._boxes)
        if js is ne._TITLE_VALUE_JS:
            self.reads.append("title")
            return _next(self._titles)
        if js is ne._BODY_READ_JS:
            self.reads.append("body")
            return _next(self._bodies)
        raise AssertionError(f"未预期的 evaluate:{str(js)[:60]}")

    def locate_count(self):
        return self.reads.count("box")


# ---------------- _type_into ----------------


@pytest.mark.unit
def test_type_into_clears_before_typing():
    """顺序硬约束:聚焦 → Ctrl+A → Backspace → 逐字输入(E5 实证的那条路径)。"""
    page = FakePage(boxes=[_box()])
    human = FakeHuman()

    ok, err = ne._type_into(page, human, ["input"], "新标题", intent="标题")

    assert (ok, err) == (True, None)
    assert human.calls == [
        ("click", (1000.0, 320.0)),   # 矩形中心
        ("key", "Control+a"),
        ("key", "Backspace"),
        ("type", "新标题"),
    ]


@pytest.mark.unit
def test_type_into_without_clear_first_does_not_press_ctrl_a():
    page = FakePage(boxes=[_box()])
    human = FakeHuman()

    ne._type_into(page, human, ["input"], "追加文本", intent="标题", clear_first=False)

    assert human.kinds("key") == []
    assert human.kinds("type") == [("type", "追加文本")]


@pytest.mark.unit
def test_type_into_empty_value_clears_only():
    """``value=""`` 是清空标题的合法路径(设计 3.1):清了但**一个字都不输**。

    若这里退化成 ``type_text("")``,拟人层会白跑一趟 think_delay,更要紧的是语义混淆 ——
    "清空"和"输入空串"在读回判据上一样,但在动作序列上必须看得出区别。
    """
    page = FakePage(boxes=[_box()])
    human = FakeHuman()

    ok, _ = ne._type_into(page, human, ["input"], "", intent="标题")

    assert ok is True
    assert human.kinds("key") == [("key", "Control+a"), ("key", "Backspace")]
    assert human.kinds("type") == [], "空值不该有任何输入动作"


@pytest.mark.unit
def test_type_into_relocates_on_every_attempt():
    """每次重试都**重新定位**:前两次没命中,第三次命中即成功,共定位 3 次。

    不持 ElementHandle 是硬纪律(React 聚焦重渲染让旧句柄脱离,历史 job2 实测),
    所以"重试"必然伴随"重新取坐标" —— 这条用例就是防止将来有人把 box 提到循环外。
    """
    page = FakePage(boxes=[None, None, _box()])
    human = FakeHuman()

    ok, err = ne._type_into(page, human, ["input"], "文本", intent="标题")

    assert (ok, err) == (True, None)
    assert page.locate_count() == 3
    assert human.kinds("click") == [("click", (1000.0, 320.0))], "只有命中那次才动手"


@pytest.mark.unit
def test_type_into_gives_up_after_three_tries_with_last_error():
    page = FakePage(boxes=[None])
    human = FakeHuman()

    ok, err = ne._type_into(page, human, ["input"], "文本", intent="标题")

    assert ok is False
    assert "未找到标题输入框" in err
    assert page.locate_count() == ne._TYPE_TRIES == 3
    assert human.calls == [], "定位不到就一次都不点"


@pytest.mark.unit
def test_type_into_retries_on_click_exception_and_reports_it():
    """点击/输入抛异常(如句柄脱离)照样重试,最后如实上报原因,不吞。"""
    page = FakePage(boxes=[_box()])
    human = FakeHuman(click_raises=RuntimeError("Element is not attached to the DOM"))

    ok, err = ne._type_into(page, human, ["input"], "文本", intent="标题")

    assert ok is False
    assert "not attached" in err
    assert len(human.kinds("click")) == 3


# ---------------- 滚进视口(E8) ----------------


@pytest.mark.unit
def test_scroll_up_when_target_pushed_above_viewport():
    """E8 硬要求:目标被顶到视口上方(实拍 rect.y=-815)时先**向上**滚再重取坐标。

    弹层/滚动会把目标顶出视口,上一步的坐标在下一步已失效;照旧坐标点击会落到顶栏,
    动作静默失败而代码看起来一切正常(文字版发布丢话题同款陷阱,02b44f6)。
    只会向下滚的写法在这种情形下越滚越远,所以方向必须按落点算。
    """
    page = FakePage(boxes=[_box(y=-815.0, h=30.0), _box(y=300.0)], titles=["旧", "", "新标题"])
    human = FakeHuman()

    result = ne.apply_title_edit(page, human, "新标题")

    assert result["status"] == "done"
    assert human.kinds("scroll") == [("scroll", "up")]
    # 滚完重新取坐标,点的是**新**坐标而不是缓存的旧坐标
    assert human.kinds("click") == [("click", (1000.0, 320.0))]
    assert page.locate_count() >= 2, "滚动后必须重新取一次 rect"


@pytest.mark.unit
def test_scroll_down_when_target_below_viewport():
    page = FakePage(boxes=[_box(y=1200.0), _box(y=400.0)], titles=["旧", "", "新标题"])
    human = FakeHuman()

    ne.apply_title_edit(page, human, "新标题")

    assert human.kinds("scroll") == [("scroll", "down")]


@pytest.mark.unit
def test_no_scroll_when_already_in_viewport():
    """已经在视口里就别乱滚(多余动作也是风控面)。"""
    page = FakePage(boxes=[_box()], titles=["旧", "", "新标题"])
    human = FakeHuman()

    ne.apply_title_edit(page, human, "新标题")

    assert human.kinds("scroll") == []


@pytest.mark.unit
def test_tall_editor_counts_as_in_viewport_by_click_point():
    """正文编辑器实拍 816x839 比视口还高:判据是**落点**在视口内,不是整块矩形。

    用整块判据会永远不成立 → 白滚 3 次还把目标滚跑,最后判"够不着"直接 error。
    """
    page = FakePage(
        boxes=[_box(y=320.0, h=839.0, w=816.0, ih=900.0)],
        bodies=["旧正文", "", "新正文"],
    )
    human = FakeHuman()

    result = ne.apply_content_edit(page, human, "新正文")

    assert result["status"] == "done"
    assert human.kinds("scroll") == []


@pytest.mark.unit
def test_refuses_to_click_when_still_out_of_viewport_after_scrolls():
    """滚满 3 次仍够不着:报错退出,**绝不盲点**。

    视口外坐标会被夹到边缘,点空之后紧跟的 Ctrl+A / Backspace 就落到别的元素上了。
    宁可让编排层弃提交(设计 4.4),也不发一次落点不明的点击。
    """
    page = FakePage(boxes=[_box(y=-900.0, h=30.0)], titles=["旧标题"])
    human = FakeHuman()

    result = ne.apply_title_edit(page, human, "新标题")

    assert result["status"] == "error"
    assert result["reason"].startswith("title_input_not_found")
    assert human.kinds("click") == [] and human.kinds("key") == []
    assert len(human.kinds("scroll")) == ne._SCROLL_TRIES == 3


# ---------------- apply_title_edit ----------------


@pytest.mark.unit
def test_apply_title_edit_success_shape():
    page = FakePage(boxes=[_box()], titles=["旧标题", "", "新标题"])
    human = FakeHuman()

    result = ne.apply_title_edit(page, human, "新标题")

    assert result == {
        "status": "done",
        "title_before": "旧标题",
        "title_read_back": "新标题",
    }


@pytest.mark.unit
def test_apply_title_edit_readback_mismatch_is_error():
    """读回不等 → error。此刻编辑器里是残缺态,提交出去就是把残缺真发布(设计 4.4)。

    本函数只如实报状态,弃提交是编排层(T6)的事 —— 这里不点发布、不做补救。
    """
    page = FakePage(boxes=[_box()], titles=["旧标题", "", "新标"])
    human = FakeHuman()

    result = ne.apply_title_edit(page, human, "新标题")

    assert result["status"] == "error"
    assert result["reason"].startswith("title_readback_mismatch")
    assert result["title_before"] == "旧标题"
    assert result["title_read_back"] == "新标"


@pytest.mark.unit
def test_apply_title_edit_readback_normalizes_whitespace():
    """回读经 DOM 取值,换行/多空格与输入侧不保证逐字节一致 —— 归一后全等即算成。"""
    page = FakePage(boxes=[_box()], titles=["旧标题", "", " 新的  标题 "])
    human = FakeHuman()

    assert ne.apply_title_edit(page, human, "新的 标题")["status"] == "done"


@pytest.mark.unit
def test_apply_title_edit_clear_to_empty_is_done():
    """``title=""`` 清空标题:读回空串就是成功(不是"没读到")。"""
    page = FakePage(boxes=[_box()], titles=["旧标题", "", ""])
    human = FakeHuman()

    result = ne.apply_title_edit(page, human, "")

    assert result["status"] == "done"
    assert result["title_read_back"] == ""
    assert human.kinds("type") == [], "清空路径一个字都不输"


@pytest.mark.unit
def test_apply_title_edit_missing_input_reports_before_and_no_action():
    """定位不到标题框:error + 留底照报,且**一次输入动作都没发生**。"""
    page = FakePage(boxes=[None], titles=["旧标题"])
    human = FakeHuman()

    result = ne.apply_title_edit(page, human, "新标题")

    assert result["status"] == "error"
    assert result["reason"].startswith("title_input_not_found")
    assert result["title_before"] == "旧标题"
    assert result["title_read_back"] is None
    assert human.calls == []


@pytest.mark.unit
def test_apply_title_edit_type_failure_reports_reason():
    """输入阶段失败(定位一直不命中):reason 带 ``title_type_failed`` 前缀。"""
    page = FakePage(boxes=[_box(), None], titles=["旧标题"])
    human = FakeHuman()

    result = ne.apply_title_edit(page, human, "新标题")

    assert result["status"] == "error"
    assert result["reason"].startswith("title_type_failed")
    assert result["title_before"] == "旧标题"


@pytest.mark.unit
def test_apply_title_edit_unreadable_title_is_none_not_empty_string():
    """读不到标题框(返回 None)与"标题是空串"必须分得开 —— 后者是合法的清空态。"""
    page = FakePage(boxes=[_box()], titles=[None])
    human = FakeHuman()

    result = ne.apply_title_edit(page, human, "新标题")

    assert result["title_before"] is None
    assert result["status"] == "error", "读回 None 不等于目标,判失败"


# ---------------- apply_content_edit ----------------


@pytest.mark.unit
def test_apply_content_edit_success_shape_and_topics_dropped():
    """``topics_dropped`` 取自**替换前**的旧正文(替换即丢失,如实上报,设计 3.2)。"""
    old = "旧正文 #身边的心理学[话题]# 收尾 #情绪管理[话题]#"
    page = FakePage(boxes=[_box()], bodies=[old, "", "新的正文内容"])
    human = FakeHuman()

    result = ne.apply_content_edit(page, human, "新的正文内容")

    assert result == {
        "status": "done",
        "body_before": old,
        "topics_dropped": ["身边的心理学", "情绪管理"],
        "body_read_back": "新的正文内容",
    }


@pytest.mark.unit
def test_apply_content_edit_uses_exact_match_not_prefix():
    """编辑器内判据是**全等**,不是 ``content_prefix_ok``,两条判据别混用。

    正文步排在活动步之前(设计 4.2①),写完当场读时还没有任何话题被注进末尾,所以全等
    成立;拿前缀判会把"话题 chip 没被 Ctrl+A 清干净"(E6 未闭环那半)放过去,残缺态就
    被提交出去了。``content_prefix_ok`` 是提交后重进页面才用的那条。
    """
    page = FakePage(boxes=[_box()], bodies=["旧正文", "", "新的正文内容 #身边的心理学[话题]#"])
    human = FakeHuman()

    result = ne.apply_content_edit(page, human, "新的正文内容")

    assert result["status"] == "error", "编辑器内多出话题残留 = 没清干净,必须判失败"
    assert result["reason"].startswith("content_readback_mismatch")
    # 同一对值在提交后的判据下是"生效"—— 正好说明两条判据不能混用
    assert ne.content_prefix_ok(result["body_read_back"], "新的正文内容") is True


@pytest.mark.unit
def test_apply_content_edit_readback_mismatch_keeps_before_and_topics():
    """失败也要把留底与丢掉的话题报全 —— 编排层要拿它们上报/人工比对。"""
    old = "旧正文 #焦虑[话题]#"
    page = FakePage(boxes=[_box()], bodies=[old, "", "新的正"])
    human = FakeHuman()

    result = ne.apply_content_edit(page, human, "新的正文内容")

    assert result["status"] == "error"
    assert result["body_before"] == old
    assert result["topics_dropped"] == ["焦虑"]
    assert result["body_read_back"] == "新的正"


@pytest.mark.unit
def test_apply_content_edit_missing_editor_reports_before_and_no_action():
    page = FakePage(boxes=[None], bodies=["旧正文 #焦虑[话题]#"])
    human = FakeHuman()

    result = ne.apply_content_edit(page, human, "新正文")

    assert result["status"] == "error"
    assert result["reason"].startswith("body_editor_not_found")
    assert result["topics_dropped"] == ["焦虑"]
    assert result["body_read_back"] is None
    assert human.calls == []


@pytest.mark.unit
def test_apply_content_edit_type_failure_reports_reason():
    page = FakePage(boxes=[_box(), None], bodies=["旧正文"])
    human = FakeHuman()

    result = ne.apply_content_edit(page, human, "新正文")

    assert result["status"] == "error"
    assert result["reason"].startswith("content_type_failed")
    assert result["body_before"] == "旧正文"


@pytest.mark.unit
def test_apply_content_edit_clears_before_typing():
    """正文同样是"框里有旧内容":首次就得清(设计 4.1),顺序与标题一致。"""
    page = FakePage(boxes=[_box()], bodies=["旧正文", "", "新正文"])
    human = FakeHuman()

    ne.apply_content_edit(page, human, "新正文")

    assert [c[0] for c in human.calls] == ["click", "key", "key", "type"]
    assert human.kinds("key") == [("key", "Control+a"), ("key", "Backspace")]


@pytest.mark.unit
def test_apply_content_edit_tolerates_unreadable_body_before():
    """读不到旧正文(None):``topics_dropped`` 给空列表,不炸。"""
    page = FakePage(boxes=[_box()], bodies=[None, "", "新正文"])
    human = FakeHuman()

    result = ne.apply_content_edit(page, human, "新正文")

    assert result["status"] == "done"
    assert result["body_before"] is None
    assert result["topics_dropped"] == []


# ---------------- 返回形状(T6 按此接线) ----------------


@pytest.mark.unit
@pytest.mark.parametrize("status_case", ["done", "error"])
def test_title_result_keys_are_stable(status_case):
    titles = ["旧标题", "", "新标题" if status_case == "done" else "别的"]
    result = ne.apply_title_edit(FakePage(boxes=[_box()], titles=titles), FakeHuman(), "新标题")

    assert result["status"] == status_case
    assert set(result) == (
        {"status", "title_before", "title_read_back"}
        if status_case == "done"
        else {"status", "reason", "title_before", "title_read_back"}
    )


@pytest.mark.unit
@pytest.mark.parametrize("status_case", ["done", "error"])
def test_content_result_keys_are_stable(status_case):
    bodies = ["旧正文", "", "新正文" if status_case == "done" else "别的"]
    result = ne.apply_content_edit(FakePage(boxes=[_box()], bodies=bodies), FakeHuman(), "新正文")

    assert result["status"] == status_case
    assert set(result) == (
        {"status", "body_before", "topics_dropped", "body_read_back"}
        if status_case == "done"
        else {"status", "reason", "body_before", "topics_dropped", "body_read_back"}
    )


@pytest.mark.unit
def test_clear_title_with_unreadable_readback_is_error_not_done():
    """title="" 清空 + 读回 None → **error 而非 done**(fable 验收必修项)。

    读回 None 意味着"根本没读到",不是"读到了空"。若放进全等比较,
    _norm(None)=="" 与 _norm("") 相等,"定位失败"会被谎报成"清空成功"——
    而清空标题恰是合法意图(设计 3.1),这条路径真实存在,必须堵死。
    """
    page = FakePage(boxes=[_box()], titles=["旧标题", None])
    human = FakeHuman()

    result = ne.apply_title_edit(page, human, "")

    assert result["status"] == "error"
    assert result["reason"].startswith("title_readback_unavailable")
    assert result["title_read_back"] is None


@pytest.mark.unit
def test_clear_that_never_empties_fails_without_typing():
    """清空后读回始终有残留 → 重清 _CLEAR_TRIES 次仍不空即失败,**绝不在残留上输入**。

    2026-08-03 T7 真号实测:tiptap 富文本(多段落+话题 chip)下 Ctrl+A 可能只选中部分,
    残留旧内容;此时继续输入 = 新文本+旧残留,mismatch 又只显示相同前缀,连试两次都
    定位不了。清空验证是把这类失败从"打完才发现"提前到"打之前就拦住"。
    """
    page = FakePage(boxes=[_box()], bodies=["旧正文", "残留", "残留", "残留"])
    human = FakeHuman()

    result = ne.apply_content_edit(page, human, "新正文")

    assert result["status"] == "error"
    assert "清空失败" in result["reason"]
    assert human.kinds("type") == []   # 一个字都没输入


@pytest.mark.unit
def test_describe_diff_locates_first_divergence():
    """差异定位:前缀相同时必须给出首差异点与两侧窗口——截断前缀零诊断力。"""
    got = "共同前缀" * 20 + "然后是残留的旧内容"
    want = "共同前缀" * 20 + "然后是目标的新内容"

    msg = ne.describe_diff(got, want)

    assert "首差异@" in msg and "83" in msg      # 4*20+3=83 处首次分叉
    assert "残留" in msg and "目标" in msg        # 两侧窗口都在
    assert f"读回长{len(got)}" in msg


@pytest.mark.unit
def test_atomic_chips_cleared_by_per_key_backspace():
    """Ctrl+A 清不掉的 atomic chip → 第二阶段逐键退格删到空,然后正常输入。

    2026-08-03 真号确诊:tiptap 话题 chip 是 atomic node,三轮全选删除后残留一字不变
    ('#身边的心理学[话题]# #明日方舟[话题]#')。atomic 节点吃单独 Backspace,
    光标到文末逐键退格即可清掉。
    """
    chips = "#身边的心理学[话题]# #明日方舟[话题]#"
    # 读回序列:before → 3 次 Ctrl+A 阶段全残留 → 逐退格阶段第一次抽查即空 → 输入后读回
    page = FakePage(boxes=[_box()], bodies=["旧正文 " + chips, chips, chips, chips, "", "新正文"])
    human = FakeHuman()

    result = ne.apply_content_edit(page, human, "新正文")

    assert result["status"] == "done"
    assert result["topics_dropped"] == ["身边的心理学", "明日方舟"]
    # 第二阶段确实发生了:End 定位 + 若干退格
    keys = [c[1] for c in human.kinds("key")]
    assert any("End" in k for k in keys)
    assert keys.count("Backspace") >= 4    # 3 次全选阶段 + 逐退格阶段


@pytest.mark.unit
def test_backspace_budget_exhaustion_still_fails_closed():
    """逐键退格预算耗尽仍不空(结构异常)→ 失败,绝不在残留上输入。"""
    stuck = "#顽固[话题]#"
    page = FakePage(boxes=[_box()], bodies=["旧 " + stuck] + [stuck] * 60)
    human = FakeHuman()

    result = ne.apply_content_edit(page, human, "新正文")

    assert result["status"] == "error"
    assert "清空失败" in result["reason"]
    assert human.kinds("type") == []


# ==================== _backspace_budget:退格预算随残留长度(C3-③) ====================


@pytest.mark.unit
def test_backspace_budget_scales_with_residue_length():
    """退格预算 = 基线 + 剩余字数 + 换行数×2;长残留严格拿到更大预算。"""
    assert ne._backspace_budget("") == ne._CHIP_BACKSPACE_BUDGET           # 空残留 = 纯基线
    assert ne._backspace_budget("a" * 100) == ne._CHIP_BACKSPACE_BUDGET + 100
    # 换行按 2 算(tiptap 段落节点边界要多吃一次退格)
    assert ne._backspace_budget("a\nb\nc") == ne._CHIP_BACKSPACE_BUDGET + 5 + 2 * 2
    # 长残留严格大于短残留 —— 这正是进度制能清掉 900 字长文而不误报的底气
    assert ne._backspace_budget("x" * 500) > ne._backspace_budget("xx")


@pytest.mark.unit
def test_backspace_budget_capped_at_max():
    """再长也不过硬顶:到顶仍不空 = 结构异常,交回失败路径而非无限按。"""
    assert ne._backspace_budget("x" * 100000) == ne._BACKSPACE_BUDGET_MAX


# ==================== _clear_field:进度制清空(C3-①②④⑤) ====================


def _reader(seq):
    """诚实的假读法:第 n 次调用返回 ``seq[n]``,越界后固定返回 ``seq[-1]``。

    **按调用次数**吐残留(绝不按输入 dict-lookup 糊弄):清空是"读一次→按一次→再读"的
    递推,残留只跟"按了几次"有关、与 page 对象无关,所以 page 传谁都行。
    """
    calls = {"n": 0}

    def read(_page):
        i = calls["n"]
        calls["n"] += 1
        return seq[i] if i < len(seq) else seq[-1]

    return read


@pytest.mark.unit
def test_clear_field_keeps_going_while_shrinking_past_three_rounds():
    """残留逐轮变短就继续,**不再固定 3 轮放弃**:连缩 9 轮、第 10 轮读空 → ``(True, "")``。

    这条同时是防"进度制退回固定轮数"的变异哨兵:若把全选阶段改回固定 3 轮,3 轮后 prev_left
    仍很长会转逐键退格,而预算按那时残留算(小),抵达不了空索引 → ``(False, …)`` 变红。
    """
    seq = ["a" * n for n in range(9, 0, -1)] + [""]   # 9,8,…,1,"" 共 10 次读回
    human = FakeHuman()

    ok, diag = ne._clear_field(object(), human, _reader(seq), intent="正文")

    assert (ok, diag) == (True, "")
    # 全选阶段真跑了 10 轮(每轮一次 Control+a),证明没在第 3 轮放弃
    assert human.kinds("key").count(("key", "Control+a")) == 10


@pytest.mark.unit
def test_clear_field_switches_to_backspace_after_two_stalled_rounds():
    """连续 2 轮读回没变短 → 判定全选推不动、**转阶段二逐键退格**(Control+End 出现)。"""
    # 全选阶段:round1 "abc"(设 prev)→ round2 "abc"(停滞1)→ round3 "abc"(停滞2 break);
    # 阶段二:第 _BACKSPACE_READ_EVERY 次退格抽查读回 "" → 成功
    human = FakeHuman()

    ok, _diag = ne._clear_field(object(), human, _reader(["abc", "abc", "abc", ""]), intent="正文")

    assert ok is True
    assert human.kinds("key").count(("key", "Control+a")) == 3   # 停滞 2 轮后不再全选
    assert any("End" in k for _, k in human.kinds("key")), "应转入阶段二(Control+End 定位文末)"


@pytest.mark.unit
def test_clear_field_stops_as_soon_as_empty():
    """清空即止:全选阶段第一轮就读回空 → 立即返回,一次退格都不按,也不进阶段二(C3-④)。"""
    human = FakeHuman()

    ok, diag = ne._clear_field(object(), human, _reader([""]), intent="正文")

    assert (ok, diag) == (True, "")
    assert human.kinds("key") == [("key", "Control+a"), ("key", "Backspace")]   # 只清一轮
    assert not any("End" in k for _, k in human.kinds("key")), "读回即空不该进阶段二"


@pytest.mark.unit
def test_clear_field_exhausted_returns_false_with_diagnostic():
    """预算耗尽仍不空 → ``(False, 诊断串)``,绝不谎报清空(C3-⑤)。

    残留恒定非空:全选阶段停滞转退格,退格按满预算仍读回残留 → 失败;诊断串带轮数 +
    退格次数 + 残留样本,供上层弃提交时留痕。
    """
    human = FakeHuman()

    ok, diag = ne._clear_field(object(), human, _reader(["#顽固[话题]#"]), intent="正文")

    assert ok is False
    assert "残留" in diag and "逐键退格" in diag
    assert "#顽固[话题]#" in diag           # 残留样本如实带出(repr 里含原串)


# ==================== apply_content_edit 长度闸(C3 长度闸 + 变异哨兵②) ====================


@pytest.mark.unit
def test_content_edit_allows_shorter_writeback_to_oversized_note():
    """既存 937 字笔记写回 925 字(变短)→ 放行(判据 ``max(900, 原文长度)`` 的放宽档)。

    这条同时是防"把 ``max(900, L0)`` 改成死 900"的变异哨兵:925 > 900,退回死 900 会误拒 → 红。
    """
    before, after = "价" * 937, "价" * 925
    page = FakePage(boxes=[_box()], bodies=[before, "", after])
    human = FakeHuman()

    result = ne.apply_content_edit(page, human, after)

    assert result["status"] == "done", result.get("reason")
    assert result["body_before"] == before


@pytest.mark.unit
def test_content_edit_rejects_growing_beyond_original_with_zero_action():
    """新正文 > ``max(900, 原文长度)`` 且比原文更长 → ``content_too_long_for_note``,**零点击零清空**。

    937 字笔记想写成 950 字(变长)被判据挡下:判据先于破坏,笔记原样未动。
    """
    before = "价" * 937
    page = FakePage(boxes=[_box()], bodies=[before])
    human = FakeHuman()

    result = ne.apply_content_edit(page, human, "价" * 950)

    assert result["status"] == "error"
    assert result["reason"].startswith("content_too_long_for_note")
    assert human.calls == [], "判据不合格必须零动作(没点击、没清空、没输入)"
    assert page.reads.count("box") == 0, "连滚进视口的定位都不该发生"
    assert result["body_before"] == before
    assert result["body_read_back"] is None


@pytest.mark.unit
def test_content_edit_length_gate_fails_closed_when_before_unreadable():
    """``body_before`` 读不回(None)→ L0 记 0、闸退到 900 **fail-closed**:901 字被拒。

    读不到原文就没有放宽依据,宁可拒收让上层重试,也不拿猜的上限做全量覆盖提交。
    """
    page = FakePage(boxes=[_box()], bodies=[None])
    human = FakeHuman()

    result = ne.apply_content_edit(page, human, "字" * 901)

    assert result["status"] == "error"
    assert result["reason"].startswith("content_too_long_for_note")
    assert "原 0 字" in result["reason"]
    assert human.calls == []


# ==================== C2:话题分隔归一化在注入前生效 ====================


@pytest.mark.unit
def test_content_edit_normalizes_space_separated_topics_before_typing():
    """空格分隔的话题在**注入前**被归一成连写:真正打进编辑器的是连写文本,不是原始空格分隔。

    调用方拿台账 content_text(话题空格分隔)当新正文回写,不归一编辑器认不出 chip 会丢话题。
    """
    space_form = "正文内容 #A[话题]# #B[话题]#"
    joined_form = "正文内容 #A[话题]##B[话题]#"
    # 编辑器读回的是归一后的连写形态(注入什么读回什么)
    page = FakePage(boxes=[_box()], bodies=["旧正文", "", joined_form])
    human = FakeHuman()

    result = ne.apply_content_edit(page, human, space_form)

    assert result["status"] == "done", result.get("reason")
    assert human.kinds("type") == [("type", joined_form)]   # 打进去的是连写,不是空格分隔


# ==================== readback 比对两边归一(消除对编辑器读回话题分隔形态的依赖) ====================
#
# 注入前 new_content 已归一成连写,但读回值来自 tiptap DOM —— 编辑器把相邻话题 chip 读回成
# 连写还是空格分隔,静态不可证且可变。readback 比对若只对 new_content 单边归一,一旦编辑器
# 读回带空格就与连写的 new_content 判不等 → 带末尾话题的正文编辑一律假失败。修复:比对时两边
# 都过 normalize_topic_separators,只校验实质内容一致。下面第一条是本修复的核心哨兵。


@pytest.mark.unit
def test_content_edit_readback_with_spaced_topics_is_done():
    """核心哨兵:new_content 连写、编辑器读回**带空格分隔** → 判 done,不再 content_readback_mismatch。

    这正是对抗审查抓的真风险:read_back(带空格)≠ new_content(连写)会让带末尾话题的正文
    编辑一律 fail-safe。两边归一后只剩实质内容,分隔空格差异不再造成假失败。
    去掉 readback 比对的归一化(退回只 _norm),这条必转红 —— 变异哨兵。
    """
    joined_form = "正文内容 #A[话题]##B[话题]#"      # 注入的连写形态
    spaced_read_back = "正文内容 #A[话题]# #B[话题]#"  # 编辑器读回带空格(模拟 DOM 行为)
    page = FakePage(boxes=[_box()], bodies=["旧正文", "", spaced_read_back])
    human = FakeHuman()

    result = ne.apply_content_edit(page, human, joined_form)

    assert result["status"] == "done", result.get("reason")
    assert result["body_read_back"] == spaced_read_back  # 原始读回值如实留底,不被归一改写


@pytest.mark.unit
def test_content_edit_readback_joined_topics_still_done():
    """回归:new_content 连写、读回也连写 → done(归一是幂等的,不破坏原本就相等的通路)。"""
    joined_form = "正文内容 #A[话题]##B[话题]#"
    page = FakePage(boxes=[_box()], bodies=["旧正文", "", joined_form])
    human = FakeHuman()

    result = ne.apply_content_edit(page, human, joined_form)

    assert result["status"] == "done", result.get("reason")


@pytest.mark.unit
def test_content_edit_real_body_mismatch_still_errors():
    """归一只吞话题分隔空格,不吞真 mismatch:正文实质文字不同 → 仍判 content_readback_mismatch。

    话题 chip 部分完全相同(排除分隔差异),差异落在正文正文字上,确认归一没把真 mismatch 抹平。
    """
    new_content = "正文内容甲 #A[话题]##B[话题]#"
    real_diff_read_back = "正文内容乙 #A[话题]##B[话题]#"  # 话题相同,正文字不同
    page = FakePage(boxes=[_box()], bodies=["旧正文", "", real_diff_read_back])
    human = FakeHuman()

    result = ne.apply_content_edit(page, human, new_content)

    assert result["status"] == "error"
    assert result["reason"].startswith("content_readback_mismatch")
    assert result["body_read_back"] == real_diff_read_back
