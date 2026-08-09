"""播客浏览器层:tab 三件套 / 引导浮层 / 合集创建 / step2a / step3a(全部假 page)。

假 page 的粒度刻意做到"能表达取证里那几个真实坑":
- 「创建」按钮外层还有一个 innerText 同为「创建」的 div(真号取证实录,按纯文本找会抓错);
- 封面选完文件会弹「封面裁剪」,不点确定按钮就一直禁用(第二轮取证最贵的发现);
- 「播客合集上线啦」引导浮层压在「上传音频」按钮上。
"""

import app.browser.atomic_tasks as atomic_mod
import app.browser.podcast as podcast_mod
from app.browser.atomic_tasks import XHSPublishAtomicTasks, go_publish_enabled


# ---------------- 假件 ----------------


class _El:
    """最小元素替身:标签 + 文本 + class + 属性 + 子元素。"""

    def __init__(self, tag="div", text="", cls="", attrs=None, children=None):
        self.tag = tag
        self.text = text
        self.cls = cls
        # class 同时进 attrs:真 DOM 里 ``[class*='close']`` 就是按 class 属性匹配的,
        # 假件不这么做的话"按属性找关闭按钮"这类选择器在测试里会假阴性。
        self.attrs = {"class": cls, **dict(attrs or {})}
        self.children = list(children or [])
        self.files = None

    def inner_text(self):
        return self.text

    def set_input_files(self, paths):
        self.files = paths

    def scroll_into_view_if_needed(self, **k):
        return None

    def query_selector(self, selector):
        return next(iter(self.query_selector_all(selector)), None)

    def query_selector_all(self, selector):
        return [c for c in self.children if _matches(c, selector)]

    def evaluate(self, script, other=None):
        return self is other


def _matches(el: "_El", selector: str) -> bool:
    """够用的选择器匹配:标签 / .class / [attr] / [attr*='v'] 的组合,逗号为并集。"""
    for part in selector.split(","):
        part = part.strip()
        if not part:
            continue
        if _matches_one(el, part):
            return True
    return False


def _matches_one(el: "_El", part: str) -> bool:
    rest = part
    if rest and rest[0].isalpha():
        tag = ""
        while rest and (rest[0].isalnum() or rest[0] == "-"):
            tag += rest[0]
            rest = rest[1:]
        if tag.lower() != el.tag.lower():
            return False
    classes = set(el.cls.split())
    while rest:
        if rest.startswith("."):
            rest = rest[1:]
            name = ""
            while rest and (rest[0].isalnum() or rest[0] in "-_"):
                name += rest[0]
                rest = rest[1:]
            if name not in classes:
                return False
        elif rest.startswith("["):
            close = rest.index("]")
            body, rest = rest[1:close], rest[close + 1:]
            if "*=" in body:
                key, val = body.split("*=", 1)
                if val.strip("'\"") not in str(el.attrs.get(key, "")):
                    return False
            elif "=" in body:
                key, val = body.split("=", 1)
                if str(el.attrs.get(key, "")) != val.strip("'\""):
                    return False
            elif body not in el.attrs:
                return False
        else:
            return False
    return True


class _Human:
    def __init__(self):
        self.clicks = []
        self.typed = []
        self.keys = []

    def click(self, target, reason=""):
        self.clicks.append((target, reason))

    def type_text(self, target, text, **k):
        self.typed.append((target, text))
        if target is not None and target.tag in ("input", "textarea"):
            target.attrs["value"] = text

    def press_key(self, key, reason=""):
        self.keys.append(key)

    def wait(self, *a, **k):
        return None


class _Page:
    """按 ``elements`` 列表回答查询;``evaluate`` 由注入的 ``eval_fn`` 处理。"""

    url = "https://creator.xiaohongshu.com/publish/publish?source=official"

    def __init__(self, elements=None, eval_fn=None, body_text=""):
        self.elements = list(elements or [])
        self.eval_fn = eval_fn
        self.body_text = body_text
        self.screenshots = 0

    def query_selector(self, selector):
        return next(iter(self.query_selector_all(selector)), None)

    def query_selector_all(self, selector):
        return [e for e in self.elements if _matches(e, selector)]

    def evaluate(self, script, *args):
        return self.eval_fn(script, *args) if self.eval_fn else None

    def inner_text(self, _sel):
        return self.body_text

    def screenshot(self, **k):
        self.screenshots += 1
        return b""

    def wait_for_selector(self, *a, **k):
        raise Exception("no match")


def _tab_eval(*active_texts):
    """伪造 tab 判据那段只读 JS —— 返回**所有**带 active 的 tab 文案(复数)。

    复数是真值:2026-08-08 取证实测 DOM 里同时存在两个 ``.creator-tab.active``
    (一个残留在「上传视频」上没摘掉、一个正确挂在「发播客」上)。
    """
    def _fn(script, *a):
        if "creator-tab" in script and "active" in script:
            return list(active_texts)
        return None
    return _fn


# ---------------- tab 三件套 ----------------


def test_active_tab_is_read_from_dom_not_url():
    """激活判据只认 ``.creator-tab.active`` 的文本 —— URL 上的 from=tab_switch 不作数。

    真号取证:点「发播客」后 URL 只追加 ``&from=tab_switch``,那是一次性来源标记,
    切回别的 tab 它还在,拿它判"现在在哪个 tab"必然误判。
    """
    page = _Page(eval_fn=_tab_eval("上传视频"))
    page.url += "&from=tab_switch"
    assert podcast_mod.is_podcast_tab_active(page) is False
    assert podcast_mod.active_tab_text(page) == "上传视频"


def test_ensure_podcast_tab_clicks_the_creator_tab_div():
    """未激活时点的是 ``div.creator-tab`` 本体,不是内层 span.title。"""
    tab = _El("div", "发播客", cls="creator-tab")
    inner = _El("span", "发播客", cls="title")
    state = {"active": "上传视频"}

    def _fn(script, *a):
        if "creator-tab" in script and "active" in script:
            return [state["active"]]
        return None

    page = _Page(elements=[tab, inner], eval_fn=_fn)
    human = _Human()

    original = human.click

    def _click(target, reason=""):
        original(target, reason)
        state["active"] = "发播客"

    human.click = _click
    assert podcast_mod.ensure_podcast_tab(page, human) is True
    assert human.clicks[0][0] is tab


def test_ensure_podcast_tab_gives_up_without_url_fallback():
    """tab 压根不在 → 重试到头返回 False,**不做 URL 兜底**(那条路播客上不存在)。"""
    page = _Page(elements=[], eval_fn=_tab_eval("上传视频"))
    human = _Human()
    assert podcast_mod.ensure_podcast_tab(page, human, tries=2) is False
    assert human.clicks == []


# ---------------- 引导浮层 ----------------


def test_dismiss_tooltip_absent_is_noop():
    """浮层不在 → 什么都不做,也不报错。"""
    page = _Page(elements=[])
    human = _Human()
    out = podcast_mod.dismiss_guide_tooltip(page, human)
    assert out["present_before"] is False and human.clicks == []


def test_dismiss_tooltip_clicks_close_button_first():
    """浮层在 → 先点它容器内的关闭按钮;关掉了就不再试 Esc / 空白点击。"""
    close = _El("div", "", cls="close-btn")
    tooltip = _El("div", "播客合集上线啦 创建播客合集", children=[close])
    page = _Page(elements=[tooltip])
    human = _Human()

    def _click(target, reason=""):
        human.clicks.append((target, reason))
        page.elements = []  # 关掉了

    human.click = _click
    out = podcast_mod.dismiss_guide_tooltip(page, human)
    assert out["tried"] == ["close_button"] and out["present_after"] is False
    assert human.keys == [], "关掉了就不该再按 Esc"


def test_dismiss_tooltip_never_raises_when_stuck():
    """三种手段都关不掉 → **不抛错**,如实回报 present_after=True。

    它不挡下方的合集入口,而音频那条路会在 step2a 明确收口 —— 在这里抛错等于
    把一个「可能」的阻塞说成确定的失败。
    """
    tooltip = _El("div", "播客合集上线啦")
    page = _Page(elements=[tooltip])
    human = _Human()
    out = podcast_mod.dismiss_guide_tooltip(page, human)
    assert out["present_after"] is True
    assert out["tried"] == ["escape", "blank_click"]


# ---------------- 「创建」按钮禁用判据 ----------------


def _create_btn_eval(cls, disabled_attr, found=True):
    def _fn(script, *a):
        if "button.create-btn" in script:
            return {"found": found, "cls": cls, "disabled_attr": disabled_attr}
        return None
    return _fn


# 真号 7 单假绿里「创建」按钮 class 的**原文**(job fbb12cb4 等,2026-08-09):
# 禁用只体现在一个**裸 token** ``disabled`` 上 —— 没有 disabled 属性,也没有
# ``create-btn-disabled``。旧判据两条都不命中 → 判成"可点" → 点了颗禁用按钮无事发生。
_FAKE_GREEN_BTN_CLS = (
    "d-button d-button-large --size-icon-large --size-text-h6 disabled "
    "--color-static bold d-button-primary-loading --color-bg-primary "
    "--color-white create-btn"
)


def test_create_button_disabled_by_class_or_attr():
    """class 含 create-btn-disabled **或**有 disabled 属性 → 判不可点(取"或",防御)。"""
    both = _Page(eval_fn=_create_btn_eval("d-button create-btn create-btn-disabled", True))
    only_cls = _Page(eval_fn=_create_btn_eval("d-button create-btn create-btn-disabled", False))
    only_attr = _Page(eval_fn=_create_btn_eval("d-button create-btn", True))
    enabled = _Page(eval_fn=_create_btn_eval("d-button create-btn", False))
    assert podcast_mod.create_button_state(both)["enabled"] is False
    assert podcast_mod.create_button_state(only_cls)["enabled"] is False
    assert podcast_mod.create_button_state(only_attr)["enabled"] is False
    assert podcast_mod.create_button_state(enabled)["enabled"] is True


def test_create_button_bare_disabled_token_is_not_enabled():
    """真号假绿单的 class 原文 → 必须判**不可点**(裸 token ``disabled``,无属性无专用类)。

    这是 7 单假绿的第一个缺陷:旧判据只认 disabled 属性和 ``create-btn-disabled``,
    对这一形态全盲 → ``_wait_create_enabled`` 秒过 → 点了禁用按钮,合集一个都没建出来。
    """
    page = _Page(eval_fn=_create_btn_eval(_FAKE_GREEN_BTN_CLS, False))
    assert podcast_mod.create_button_state(page)["enabled"] is False
    # cls 原文要原样带出来,失败取证靠它
    assert podcast_mod.create_button_state(page)["cls"] == _FAKE_GREEN_BTN_CLS


def test_create_button_loading_token_is_not_enabled():
    """只剩 ``d-button-primary-loading``(封面还在上传/处理)→ 也判不可点:点了也白点。"""
    cls = "d-button d-button-large d-button-primary-loading --color-white create-btn"
    page = _Page(eval_fn=_create_btn_eval(cls, False))
    assert podcast_mod.create_button_state(page)["enabled"] is False


def test_create_button_bare_token_is_whole_word_not_substring():
    """裸 token 判定必须**整词**:粘连词不误伤,不然按钮永远"不可点"、流程直接死。"""
    for cls in (
        "d-button create-btn xdisabled",          # 前缀粘连
        "d-button create-btn disabled-x",         # 后缀粘连
        "d-button create-btn --color-static bold",  # 假绿单里同在的普通类名
        "d-button create-btn is-loading",         # 与 loading token 不同的类名
    ):
        page = _Page(eval_fn=_create_btn_eval(cls, False))
        assert podcast_mod.create_button_state(page)["enabled"] is True, cls


def test_create_button_missing_is_not_enabled():
    """按钮找不到 → ``{"found": False}``,调用方当**不可点**处理(找不到 ≠ 可以点了)。"""
    page = _Page(eval_fn=_create_btn_eval("", False, found=False))
    state = podcast_mod.create_button_state(page)
    assert state == {"found": False} and not state.get("enabled")


# ---------------- 合集创建全流程 ----------------


def _collection_page(*, crop_needed=True):
    """搭一个合集创建页的假 DOM,行为复刻真号取证到的两个坑。"""
    name = _El("input", "", cls="d-text", attrs={"placeholder": "请输入合集名称"})
    desc = _El("textarea", "", cls="d-text", attrs={"placeholder": "请输入合集简介"})
    cover = _El("input", "", cls="upload-input",
                attrs={"type": "file", "accept": ".jpg,.jpeg,.png,.webp"})
    # 真号取证:外层 div.footer-btn-area 的 innerText 也是「创建」——
    # 按纯文本找会抓到它(点不动、读不到 disabled)。
    wrapper = _El("div", "创建", cls="footer-btn-area")
    button = _El("button", "创建", cls="d-button create-btn create-btn-disabled",
                 attrs={"disabled": ""})
    confirm = _El("button", "确定", cls="crop-confirm")
    entry = _El("span", "新建播客合集", cls="drop-zone-text")

    state = {"stage": "list", "cropped": not crop_needed}
    page = _Page(elements=[entry], body_text="播客合集")

    def _btn_cls():
        filled = bool(name.attrs.get("value")) and cover.files and state["cropped"]
        return "d-button create-btn" + ("" if filled else " create-btn-disabled"), not filled

    def _fn(script, *a):
        if "creator-tab" in script and "active" in script:
            return ["发播客"]
        if "elementFromPoint" in script:   # 按钮落点链取证(失败路径才会读)
            if state["stage"] != "create":
                return {"found": False}
            cls, disabled_attr = _btn_cls()
            return {"found": True, "cls": cls, "disabled_attr": disabled_attr,
                    "point_element_chain": "button.create-btn < div.footer-btn-area < body",
                    "point_hits_button": True}
        if "button.create-btn" in script:
            if state["stage"] != "create":
                return {"found": False}
            cls, disabled_attr = _btn_cls()
            return {"found": True, "cls": cls, "disabled_attr": disabled_attr}
        return None

    page.eval_fn = _fn

    def _enter_create():
        state["stage"] = "create"
        page.elements = [name, desc, cover, wrapper, button]
        page.body_text = "新建播客合集 合集名称 合集简介 合集封面 创建"

    def _on_set_files(paths):
        cover.files = paths
        if crop_needed and confirm not in page.elements:
            page.elements = page.elements + [confirm]

    cover.set_input_files = _on_set_files

    def _confirm_crop():
        state["cropped"] = True
        if confirm in page.elements:
            page.elements = [e for e in page.elements if e is not confirm]

    def _submit():
        state["stage"] = "list"
        page.elements = [entry]
        page.body_text = f"播客合集 {name.attrs.get('value', '')}"

    handlers = {entry: _enter_create, confirm: _confirm_crop, button: _submit}
    return page, handlers, {"name": name, "cover": cover, "button": button}


def _wire(page, handlers):
    human = _Human()

    def _click(target, reason=""):
        human.clicks.append((target, reason))
        fn = handlers.get(target)
        if fn:
            fn()

    human.click = _click
    return human


def test_create_collection_happy_path(monkeypatch):
    """名称 + 简介 + 封面 + 裁剪确认 + 点创建 → done,且**点的是 <button> 不是外层 div**。"""
    monkeypatch.setattr(podcast_mod.time, "sleep", lambda *_: None)
    page, handlers, els = _collection_page()
    human = _wire(page, handlers)
    out = podcast_mod.create_collection(page, human, "心理急救包", "每周一集", "/tmp/c.png")
    assert out["status"] == "done", out
    # 成功只有一种凭据:表单收起 **且** 收起后的页面文本里出现了这个名字
    assert out["confirmed_by"] == "create_page_closed"
    assert out["name_shown_after_close"] is True
    assert out["name_preexisted"] is False
    assert els["cover"].files == ["/tmp/c.png"]
    assert any(t is els["button"] for t, _ in human.clicks), "必须点真正的 <button>"
    assert not any(getattr(t, "cls", "") == "footer-btn-area" for t, _ in human.clicks), \
        "绝不能点到 innerText 同为「创建」的外层容器"


def test_create_collection_requires_crop_confirm(monkeypatch):
    """不点「封面裁剪」的确定,「创建」就永远禁用 → 报 create_button_never_enabled。

    这正是第二轮真号取证里"三项都填了按钮仍禁用"的根因;把它钉成回归锁,
    以免以后有人把裁剪确认那一步当成可选的顺手删掉。
    """
    monkeypatch.setattr(podcast_mod.time, "sleep", lambda *_: None)
    monkeypatch.setattr(podcast_mod, "_CROP_MODAL_TIMEOUT_S", 0.1)
    monkeypatch.setattr(podcast_mod, "_CREATE_ENABLE_TIMEOUT_S", 0.1)
    page, handlers, _ = _collection_page()
    handlers.pop(list(handlers)[1])  # 拆掉裁剪确认的响应:点了也不生效
    human = _wire(page, handlers)
    out = podcast_mod.create_collection(page, human, "心理急救包", None, "/tmp/c.png")
    assert out["status"] == "error"
    assert out["reason"].startswith("create_button_never_enabled")
    assert out["observed"]["create_button"]["enabled"] is False


def test_create_collection_entry_missing_fails_loud(monkeypatch):
    """页面上没有「新建播客合集」入口 → error + 当场取证,绝不静默。"""
    monkeypatch.setattr(podcast_mod.time, "sleep", lambda *_: None)
    page = _Page(elements=[], eval_fn=_tab_eval("发播客"), body_text="发播客")
    out = podcast_mod.create_collection(page, _Human(), "X", None, "/tmp/c.png")
    assert out["status"] == "error"
    assert out["reason"].startswith("collection_entry_not_found")
    assert "observed" in out


def _submit_button(handlers):
    """从 handlers 里挑出真正的「创建」<button>(外层同名 div 不算)。"""
    return [k for k in handlers
            if getattr(k, "tag", "") == "button" and k.cls.startswith("d-button")][0]


def test_create_collection_form_still_open_is_error(monkeypatch):
    """点了创建但表单一直没收起 → error(**做没做成未知**,不谎报成功)。"""
    monkeypatch.setattr(podcast_mod.time, "sleep", lambda *_: None)
    monkeypatch.setattr(podcast_mod, "_CREATE_RESULT_TIMEOUT_S", 0.1)
    page, handlers, _ = _collection_page()
    handlers[_submit_button(handlers)] = lambda: None  # 点创建没反应
    human = _wire(page, handlers)
    out = podcast_mod.create_collection(page, human, "心理急救包", None, "/tmp/c.png")
    assert out["status"] == "error"
    assert out["reason"].startswith("create_form_still_open")


def test_create_collection_preview_card_name_is_not_success(monkeypatch):
    """**假绿回归锁**:表单还开着时,页面文本里出现合集名**不构成任何成功证据**。

    复刻真号 7 单假绿的第二个缺陷:创建表单右侧渲染一张**实时预览卡**,把刚打进去的
    合集名原样显示出来 —— 旧判据的信号②「name in page_text」于是拿自己打的字当证人,
    在表单根本没提交的情况下判 done。这里必须 error,且取证要能回答"下一单为什么还
    提交不出去"(按钮 cls 全文 + 落点链)。
    """
    monkeypatch.setattr(podcast_mod.time, "sleep", lambda *_: None)
    monkeypatch.setattr(podcast_mod, "_CREATE_RESULT_TIMEOUT_S", 0.1)
    page, handlers, _ = _collection_page()

    def _fake_green():
        # 点的是禁用按钮:表单不收起,但预览卡里有名字
        page.body_text = "创建播客合集 合集名称* 11/20 NBDpsy心理会客厅 播客 更新至0集 0人听过"

    handlers[_submit_button(handlers)] = _fake_green
    human = _wire(page, handlers)
    out = podcast_mod.create_collection(page, human, "NBDpsy心理会客厅", None, "/tmp/c.png")
    assert out["status"] == "error", "预览卡里的名字绝不能判成 done"
    assert out["reason"].startswith("create_form_still_open")
    assert out["observed"]["create_button"]["cls"], "取证要带按钮 cls 全文"
    assert out["create_button_forensics"]["point_element_chain"], "取证要带落点链"
    assert out["name_shown_after_close"] is False


def test_create_collection_closed_but_name_missing_is_error(monkeypatch):
    """表单收起了但合集区里没这个名字 → error(做没做成未知,别自动重建)。"""
    monkeypatch.setattr(podcast_mod.time, "sleep", lambda *_: None)
    monkeypatch.setattr(podcast_mod, "_CREATE_RESULT_TIMEOUT_S", 0.1)
    page, handlers, _ = _collection_page()

    def _close_without_name():
        page.elements = []
        page.body_text = "播客合集"   # 收起了,但列表里没有这个名字

    handlers[_submit_button(handlers)] = _close_without_name
    human = _wire(page, handlers)
    out = podcast_mod.create_collection(page, human, "心理急救包", None, "/tmp/c.png")
    assert out["status"] == "error"
    assert out["reason"].startswith("create_page_closed_name_missing")
    assert out["name_shown_after_close"] is False


def test_create_collection_preexisting_name_marks_confirmed_by(monkeypatch):
    """创建前列表里就有同名合集 → 成功也要在 confirmed_by 后缀提醒调用方核对。

    号1 的真实处境:「NBDpsy心理会客厅」创建前就在列表里,收起后的名字检查对它
    **不构成新建证据**(名字本来就在)。
    """
    monkeypatch.setattr(podcast_mod.time, "sleep", lambda *_: None)
    page, handlers, _ = _collection_page()
    page.body_text = "播客合集 NBDpsy心理会客厅"   # 进创建页之前就有同名
    human = _wire(page, handlers)
    out = podcast_mod.create_collection(page, human, "NBDpsy心理会客厅", None, "/tmp/c.png")
    assert out["status"] == "done", out
    assert out["name_preexisted"] is True
    assert out["confirmed_by"] == "create_page_closed_name_preexisted"


# ---------------- 「去发布」禁用判据 ----------------


def test_go_publish_enabled_requires_all_three_signals_clear():
    """三路判据取**与**:任一路表明禁用就不算可点(读不懂的形态一律当禁用)。"""
    assert go_publish_enabled({"disabled": False, "aria": None, "cls": "d-button"}) is True
    assert go_publish_enabled({"disabled": True, "aria": None, "cls": "d-button"}) is False
    assert go_publish_enabled({"disabled": False, "aria": "true", "cls": "d-button"}) is False
    assert go_publish_enabled(
        {"disabled": False, "aria": None, "cls": "d-button is-disabled"}) is False
    assert go_publish_enabled({}) is False, "读不到按钮 ≠ 可以点了"


# ---------------- step2a / step3a ----------------


def _tasks(page):
    t = XHSPublishAtomicTasks.__new__(XHSPublishAtomicTasks)
    t.page = page
    t.human = _Human()
    t.enable_debug = False
    t.screenshot_dir = "/tmp"
    t.job_tag = ""
    t.current_step = 0
    return t


def test_step2a_sets_input_files_and_never_opens_native_dialog(monkeypatch):
    """step2a 只点「上传音频」开弹窗,音频本身走 set_input_files(绝不点 file input)。"""
    button = _El("button", "上传音频", cls="d-button custom-button bg-red upload-button")
    audio_input = _El("input", "", cls="upload-input", attrs={"type": "file"})
    page = _Page(elements=[button, audio_input], eval_fn=_tab_eval("发播客"))
    tasks = _tasks(page)
    monkeypatch.setattr(
        XHSPublishAtomicTasks, "_find_element_with_retry",
        lambda self, *a, **k: audio_input,
    )
    r = tasks.step2a_upload_audio("/data/ep.mp3")
    assert r["success"] is True
    assert audio_input.files == ["/data/ep.mp3"]
    clicked = [t for t, _ in tasks.human.clicks]
    assert clicked == [button], "只该点那颗红按钮,file input 一次都不许点"


def test_step2a_fails_loud_when_modal_has_no_audio_input(monkeypatch):
    """弹窗里找不到音频 input → 明确失败并附当场取证(这正是真号取证卡住的那一步)。"""
    button = _El("button", "上传音频", cls="upload-button")
    page = _Page(elements=[button], eval_fn=_tab_eval("发播客"))
    tasks = _tasks(page)
    monkeypatch.setattr(
        XHSPublishAtomicTasks, "_find_element_with_retry", lambda self, *a, **k: None
    )
    r = tasks.step2a_upload_audio("/data/ep.mp3")
    assert r["success"] is False and "file input" in r["error"]
    assert "observed" in r


def test_step2a_refuses_to_reuse_audio_input_as_cover(monkeypatch):
    """封面 input 认不出来(候选就是音频那个)→ 不设封面,**绝不把封面灌进音频位**。"""
    button = _El("button", "上传音频", cls="upload-button")
    # 唯一的 file input 同时匹配音频与封面候选(accept 里既有 mp3 也有 jpg 的畸形页面)
    shared = _El("input", "", cls="upload-input",
                 attrs={"type": "file", "accept": ".mp3,.jpg"})
    page = _Page(elements=[button, shared], eval_fn=_tab_eval("发播客"))
    tasks = _tasks(page)
    monkeypatch.setattr(
        XHSPublishAtomicTasks, "_find_element_with_retry", lambda self, *a, **k: shared
    )
    r = tasks.step2a_upload_audio("/data/ep.mp3", "/data/c.png")
    assert r["success"] is True, "封面失败不阻断"
    assert shared.files == ["/data/ep.mp3"], "音频不许被封面顶掉"
    assert r["audio_cover"]["status"] == "error"


def test_step2a_aborts_when_tab_switch_fails(monkeypatch):
    """切不到发播客 tab → 整步失败(不在别的 tab 上乱传文件)。"""
    page = _Page(elements=[], eval_fn=_tab_eval("上传视频"))
    tasks = _tasks(page)
    r = tasks.step2a_upload_audio("/data/ep.mp3")
    assert r["success"] is False and "发播客" in r["error"]


def test_step3a_clicks_go_publish_once_enabled(monkeypatch):
    """「去发布」从禁用翻成可点 → 点它并放行到发布表单。"""
    target = _El("button", "去发布", cls="d-button")
    probes = [
        {"file_inputs": [], "go_publish": [{"cls": "d-button disabled", "disabled": True,
                                            "aria": "true"}], "page_text": ""},
        {"file_inputs": [], "go_publish": [{"cls": "d-button", "disabled": False,
                                            "aria": None}], "page_text": ""},
    ]

    def _fn(script, *a):
        return probes.pop(0) if len(probes) > 1 else probes[0]

    page = _Page(elements=[target], eval_fn=_fn)
    tasks = _tasks(page)
    monkeypatch.setattr(atomic_mod.time, "sleep", lambda *_: None)
    r = tasks.step3a_wait_for_audio_upload(max_wait=30)
    assert r["success"] is True
    assert any(t is target for t, _ in tasks.human.clicks)


def test_step3a_timeout_carries_evidence(monkeypatch):
    """一直禁用 → 超时失败,并带上最后一次读到的 file input / 按钮属性(取证)。"""
    probe = {"file_inputs": [{"cls": "upload-input", "accept": ".mp3"}],
             "go_publish": [{"cls": "d-button disabled", "disabled": True, "aria": "true"}],
             "page_text": "上传中"}
    page = _Page(elements=[], eval_fn=lambda *a: probe)
    tasks = _tasks(page)
    monkeypatch.setattr(atomic_mod.time, "sleep", lambda *_: None)
    r = tasks.step3a_wait_for_audio_upload(max_wait=1)
    assert r["success"] is False
    assert r["observed"]["file_inputs"][0]["accept"] == ".mp3"
    assert "去发布" in r["error"]


def test_step3a_default_timeout_scales_with_audio_size(tmp_path, monkeypatch):
    """不传 max_wait 时超时按**音频体积**伸缩(与 step3v 共用 media_timeout_s 公式)。"""
    big = tmp_path / "ep.mp3"
    big.write_bytes(b"x" * (200 * 1024 * 1024))
    seen = {}
    probe = {"file_inputs": [], "go_publish": [], "page_text": ""}
    page = _Page(elements=[], eval_fn=lambda *a: probe)
    tasks = _tasks(page)
    monkeypatch.setattr(atomic_mod.time, "sleep", lambda *_: None)

    real = atomic_mod.time.monotonic
    calls = {"n": 0}

    def _mono():
        calls["n"] += 1
        # 第一次(算 deadline)给 0,之后给一个巨大值直接超时收口 —— 只验预算算得对
        return 0.0 if calls["n"] <= 2 else 10 ** 9

    monkeypatch.setattr(atomic_mod.time, "monotonic", _mono)
    try:
        r = tasks.step3a_wait_for_audio_upload(audio_path=str(big))
    finally:
        monkeypatch.setattr(atomic_mod.time, "monotonic", real)
    seen["error"] = r["error"]
    # 200MB → 300 + 2*120 = 540s(默认配置),报错里会带这个预算
    assert "540" in seen["error"], seen["error"]


# =====================================================================
# 2026-08-08 真号取证(账号9·米之木木)落定的真值:命中路径锁
# fixtures: data/scene_captures/podcast_selectors/account9_podcast_publish_probe.json
# =====================================================================


# ---------------- tab 判据:两个 active 同时存在 ----------------


def test_podcast_tab_active_when_a_stale_active_tab_comes_first():
    """DOM 里同时有两个 ``.creator-tab.active`` → 只要其中一个是「发播客」就算激活。

    取证铁证(``all_creator_tabs_evidence``):「上传视频」残留 active 排在文档序更前,
    ``document.querySelector('.creator-tab.active')`` 抓到的正是那个错的
    —— 老判据在这里 100% 误判"没切过去",而页面内容其实早就是播客上传区。
    """
    page = _Page(eval_fn=_tab_eval("上传视频", "发播客"))
    assert podcast_mod.is_podcast_tab_active(page) is True


def test_podcast_tab_active_falls_back_to_content_marker():
    """class 判据全军覆没时,用**内容判据**兜底(上传区文案是发播客 tab 独有的)。"""
    page = _Page(eval_fn=_tab_eval("上传视频"),
                 body_text="草稿箱(0) 将音频文件拖拽到此,或点击上传音频 支持m4a、mp3")
    assert podcast_mod.is_podcast_tab_active(page) is True


def test_podcast_tab_inactive_when_neither_signal_holds():
    """两路判据都不命中 → False(内容判据只是兜底,不是永远为真的橡皮图章)。"""
    page = _Page(eval_fn=_tab_eval("上传视频"), body_text="拖拽视频到此处")
    assert podcast_mod.is_podcast_tab_active(page) is False


# ---------------- 引导浮层:关不掉,只能绕 ----------------


def test_exposed_click_point_reproduces_the_captured_sliver():
    """按真号 rect 算出的穿透点必须复现取证当场那一颗:(643.7, 342.0)。

    浮层压住「上传音频」按钮左侧约 81px,右侧留 39px 暴露缝——四种关闭手段实测
    全部无效(present_after 全 True、四张截图像素级无变化),这条缝是唯一走通的路。
    """
    btn = {"x": 543.3333129882812, "y": 322, "w": 120, "h": 40}
    tip = {"x": 262, "y": 268, "w": 360, "h": 116}
    point = podcast_mod.exposed_click_point(btn, tip)
    assert point is not None
    assert abs(point[0] - 643.7) < 0.5, point
    assert point[1] == 342.0, point
    # 点必须落在按钮内、且在浮层右边界之外——两个不变量各自钉死
    assert btn["x"] < point[0] < btn["x"] + btn["w"]
    assert point[0] > tip["x"] + tip["w"]


def test_exposed_click_point_refuses_a_too_narrow_sliver():
    """缝窄到不可靠(窗口尺寸一变就归零)→ 返回 None,由调用方退回直接点按钮。"""
    btn = {"x": 543.0, "y": 322, "w": 120, "h": 40}
    tip = {"x": 262, "y": 268, "w": 396, "h": 116}  # 右边界 658,只剩 5px
    assert podcast_mod.exposed_click_point(btn, tip) is None


def test_exposed_click_point_none_when_tooltip_does_not_overlap():
    """浮层压根没盖住按钮(垂直/水平任一不相交)→ None,不做多余的坐标点击。"""
    btn = {"x": 543.0, "y": 322, "w": 120, "h": 40}
    assert podcast_mod.exposed_click_point(
        btn, {"x": 262, "y": 600, "w": 360, "h": 116}) is None, "垂直不相交"
    assert podcast_mod.exposed_click_point(
        btn, {"x": 0, "y": 268, "w": 100, "h": 116}) is None, "水平不相交"


def test_exposed_click_point_tolerates_garbage_rect():
    """rect 读残了(缺键 / None)→ None,取证读数绝不制造异常。"""
    assert podcast_mod.exposed_click_point(None, None) is None
    assert podcast_mod.exposed_click_point({"x": 1}, {"x": 1, "y": 1, "w": 1, "h": 1}) is None


# ---------------- 浮层定位规则:三条件纯函数 + 缝宽上界 ----------------


def test_pick_tooltip_rect_takes_min_area_positioned_overlay():
    """从所有文案命中的候选里挑真浮层:position∈{absolute,fixed} + **面积最小**。

    这条规则就是取证脚本当场用的那条(文案 + 定位 + 面积最小);它取到的那颗真浮层
    rect 再喂 ``exposed_click_point`` 必须复现真号点中过的 (643.7, 342.0)。

    候选里刻意混进两个陷阱:一个**排在文档序最后**的大祖先容器(面积最大)、一个
    面积更小但 ``position: static`` 的普通块 —— 前者是"取最后一个"近似版的毒饵、
    后者是"不做 position 过滤"的毒饵,新规则两个都得躲开。
    """
    candidates = [
        # 真浮层:绝对定位、面积 360×116;排在文档序中间,既不是最后也不是面积最小。
        {"position": "fixed", "x": 262, "y": 268, "w": 360, "h": 116},
        # 毒饵①:面积更小(10×10)但 static,不该被选(否则 position 过滤没牙)。
        {"position": "static", "x": 0, "y": 0, "w": 10, "h": 10},
        # 毒饵②:大祖先容器(900×500),排在**最后**,近似版"取最后一个"会中招。
        {"position": "absolute", "x": 100, "y": 100, "w": 900, "h": 500},
    ]
    tip = podcast_mod._pick_tooltip_rect(candidates)
    assert tip == {"x": 262.0, "y": 268.0, "w": 360.0, "h": 116.0}, tip

    btn = {"x": 543.3333129882812, "y": 322, "w": 120, "h": 40}
    point = podcast_mod.exposed_click_point(btn, tip)
    assert point is not None
    assert abs(point[0] - 643.7) < 0.5, point
    assert point[1] == 342.0, point


def test_pick_tooltip_rect_ignores_non_positioned_and_zero_area():
    """position 非 absolute/fixed、或 w/h ≤ 0 的候选一律排除,即使它面积更小。"""
    candidates = [
        {"position": "static", "x": 0, "y": 0, "w": 1, "h": 1},        # 非定位
        {"position": "relative", "x": 0, "y": 0, "w": 2, "h": 2},      # 非定位
        {"position": "absolute", "x": 5, "y": 5, "w": 0, "h": 50},     # 零宽
        {"position": "fixed", "x": 6, "y": 6, "w": 40, "h": 0},        # 零高
        {"position": "absolute", "x": 262, "y": 268, "w": 360, "h": 116},  # 唯一合格
    ]
    assert podcast_mod._pick_tooltip_rect(candidates) == {
        "x": 262.0, "y": 268.0, "w": 360.0, "h": 116.0}


def test_pick_tooltip_rect_none_when_no_positioned_candidate():
    """没有任何定位候选(全 static / 空表 / None)→ None,让调用方退回直接点按钮。"""
    assert podcast_mod._pick_tooltip_rect(None) is None
    assert podcast_mod._pick_tooltip_rect([]) is None
    assert podcast_mod._pick_tooltip_rect(
        [{"position": "static", "x": 1, "y": 1, "w": 9, "h": 9}]) is None
    # 读残的候选(缺键 / 非数)不制造异常,只是被跳过
    assert podcast_mod._pick_tooltip_rect(
        [{"position": "absolute", "x": "oops"}]) is None


def test_exposed_click_point_refuses_a_too_wide_sliver():
    """缝宽超过按钮宽度一半 → 判定 tip 命中了气泡内层(右边界偏小),返回 None。

    危害链:命中内层 content div → tip 右边界偏小 → 算出的缝**变宽**、点位左移趋向
    浮层。``_MIN_EXPOSED_WIDTH_PX`` 是下界,对"缝被算宽"不设防;上界这道在此补齐,
    命中即退回点按钮元素中心(调用方对 None 的处理),而不是硬点一个可疑坐标。
    """
    btn = {"x": 543.3, "y": 322, "w": 120, "h": 40}  # 右边界 663.3
    # tip 命中气泡内层:仍从 x262 起、仍盖住按钮左侧(右边界 555 > 按钮左边 543.3,水平确有
    # 重叠),但只盖住约 12px → 缝 = 663.3-(555+2) ≈ 106px,是按钮宽的 0.88 倍,越过一半上界。
    tip = {"x": 262, "y": 268, "w": 293, "h": 116}
    point = podcast_mod.exposed_click_point(btn, tip)
    assert point is None, point


def test_dismiss_tooltip_records_that_no_method_works():
    """四招全试仍在 → present_after=True 且 tried 记全,**不抛错**(它不挡合集入口)。"""
    tooltip = _El("div", "播客合集上线啦", cls="guide", children=[_El("div", "", cls="close-btn")])
    page = _Page(elements=[tooltip])
    human = _Human()
    out = podcast_mod.dismiss_guide_tooltip(page, human)
    assert out["present_after"] is True
    assert out["tried"] == ["close_button", "escape", "blank_click"]


def test_dismiss_tooltip_scopes_close_button_to_the_container():
    """关闭按钮**只在浮层容器内**找 —— 页面别处的同名 ``.close-btn`` 一次都不许点。

    取证第一版就栽在这:``document.querySelector('.close-btn')`` 抓到了别处的按钮。
    """
    decoy = _El("div", "", cls="close-btn")           # 页面别处的同名按钮
    inner = _El("div", "", cls="close-btn")
    tooltip = _El("div", "播客合集上线啦", cls="guide", children=[inner])
    page = _Page(elements=[decoy, tooltip])
    human = _Human()
    podcast_mod.dismiss_guide_tooltip(page, human)
    clicked = [t for t, _ in human.clicks]
    assert inner in clicked, "该点的是容器内那颗"
    assert decoy not in clicked, "绝不能点到页面别处的同名 close-btn"


# ---------------- 音频弹窗内部:两个 input 靠 accept 分辨 ----------------


def _audio_modal_page():
    """复刻真号弹窗:``.audio-upload-modal`` 里两个同 class 的 file input。"""
    button = _El("button", "上传音频", cls="d-button custom-button upload-button")
    audio = _El("input", "", cls="upload-input",
                attrs={"type": "file", "accept": ".mp3,.wav,.aac,.flac,.m4a"})
    cover = _El("input", "", cls="upload-input",
                attrs={"type": "file", "accept": ".jpg,.jpeg,.png,.webp"})
    modal = _El("div", "上传音频 音频封面 取消 去发布",
                cls="d-modal d-modal-centered creator-modal-style audio-upload-modal")
    page = _Page(elements=[button, modal, audio, cover])

    def _fn(script, *a):
        if "creator-tab" in script and "active" in script:
            return ["发播客"]
        if "audio_modal_present" in script:
            return {
                "file_inputs": [{"cls": e.cls, "accept": e.attrs.get("accept", "")}
                                for e in page.elements if e.tag == "input"],
                "go_publish": [],
                "audio_modal_present": page.query_selector(".audio-upload-modal") is not None,
                "page_text": "",
            }
        return None

    page.eval_fn = _fn
    return page, button, audio, cover


def test_step2a_tells_audio_and_cover_inputs_apart_by_accept(monkeypatch):
    """命中路径:两个 ``input.upload-input`` 同 class,**唯一区分靠 accept** → 各就各位。

    这是取证 ②(``modal_file_inputs``)的直接落地:音频 accept 是音频扩展名、
    封面 accept 是图片扩展名,没有第二个可用特征。
    """
    monkeypatch.setattr(atomic_mod.settings, "SELFHEAL_ENABLED", False)
    page, button, audio, cover = _audio_modal_page()
    tasks = _tasks(page)
    r = tasks.step2a_upload_audio("/data/ep.mp3", "/data/cover.png")
    assert r["success"] is True, r
    assert audio.files == ["/data/ep.mp3"], "音频必须进音频位"
    assert cover.files == ["/data/cover.png"], "封面必须进封面位"
    assert r["audio_cover"]["status"] == "done"


def test_step2a_clicks_the_exposed_sliver_when_tooltip_cannot_be_closed(monkeypatch):
    """浮层关不掉 → 点**按钮右侧暴露缝的坐标**穿透它,而不是点按钮元素(会点在浮层上)。"""
    monkeypatch.setattr(atomic_mod.settings, "SELFHEAL_ENABLED", False)
    page, button, audio, cover = _audio_modal_page()
    tooltip = _El("div", "播客合集上线啦", cls="guide")
    page.elements = [tooltip] + page.elements
    monkeypatch.setattr(podcast_mod, "upload_audio_click_point", lambda _p: (643.7, 342.0))
    tasks = _tasks(page)
    r = tasks.step2a_upload_audio("/data/ep.mp3")
    assert r["success"] is True, r
    clicked = [t for t, _ in tasks.human.clicks]
    assert (643.7, 342.0) in clicked, "该点的是暴露缝坐标"
    assert button not in clicked, "浮层还在时点按钮元素等于点在浮层上"


def test_step2a_clicks_the_button_element_when_no_tooltip(monkeypatch):
    """浮层不在 → 老老实实点按钮元素(不为不存在的遮挡做坐标点击)。"""
    monkeypatch.setattr(atomic_mod.settings, "SELFHEAL_ENABLED", False)
    page, button, audio, _cover = _audio_modal_page()
    called = {"n": 0}

    def _boom(_p):
        called["n"] += 1
        return (1.0, 2.0)

    monkeypatch.setattr(podcast_mod, "upload_audio_click_point", _boom)
    tasks = _tasks(page)
    tasks.step2a_upload_audio("/data/ep.mp3")
    clicked = [t for t, _ in tasks.human.clicks]
    assert button in clicked
    assert called["n"] == 0, "没浮层就不该去算穿透点"


def test_audio_probe_reports_the_modal_presence():
    """当场取证要能回答"弹窗到底开没开" —— 定位失败时这是第一个要看的字段。"""
    page, _b, _a, _c = _audio_modal_page()
    tasks = _tasks(page)
    got = tasks._audio_probe()
    assert got["audio_modal_present"] is True


# ---------------- 发布表单 + 合集选择控件 ----------------


def _publish_form_page(*, with_options=False, name="心理急救包"):
    """复刻真号发布表单(URL target=audio)的关键结构。"""
    title = _El("input", "", cls="d-text",
                attrs={"type": "text", "placeholder": "填写标题会有更多赞哦"})
    body = _El("div", "", cls="tiptap ProseMirror", attrs={"contenteditable": "true"})
    card_title = _El("div", "加入播客合集", cls="collection-plugin-content-title")
    content = _El("div", "加入播客合集 汇集系列播客，有利于连续性收听",
                  cls="collection-plugin-content", children=[card_title])
    create_link = _El("div", "创建播客合集", cls="collection-plugin-create")
    wrapper = _El("div", "加入播客合集 汇集系列播客，有利于连续性收听 创建播客合集",
                  cls="collection-plugin-wrapper", children=[content, create_link])
    setting = _El("div", "加入播客合集 原创声明", cls="publish-page-content-setting-content",
                  children=[wrapper])
    elements = [title, body, setting, content, card_title, create_link, wrapper]
    page = _Page(elements=elements, body_text="加入播客合集 汇集系列播客")
    if with_options:
        option = _El("li", name, cls="option")
        dropdown = _El("div", name, cls="d-dropdown", children=[option])

        def _open():
            page.elements = elements + [dropdown]
            page.body_text = f"加入播客合集 {name}"

        return page, {content: _open, card_title: _open}, {"create_link": create_link,
                                                           "content": content,
                                                           "option": option}
    return page, {}, {"create_link": create_link, "content": content}


def test_select_collection_hits_the_real_card_and_picks_the_option(monkeypatch):
    """命中路径:按真值定位到合集卡 → 点开 → 选中同名候选 → 回读到名字 → done。"""
    monkeypatch.setattr(podcast_mod.time, "sleep", lambda *_: None)
    page, handlers, els = _publish_form_page(with_options=True)
    human = _wire(page, handlers)
    out = podcast_mod.select_podcast_collection(page, human, "心理急救包")
    assert out["status"] == "done", out
    assert any(t is els["option"] for t, _ in human.clicks), "得真点中候选项"


def test_select_collection_never_clicks_the_create_link(monkeypatch):
    """**绝不点「创建播客合集」** —— 它直达合集创建页,点下去等于把发布表单丢了。

    这条是真值带来的新风险:创建入口 ``.collection-plugin-create`` 就压在整卡容器
    ``.collection-plugin-wrapper`` 的右侧(x867 落在 wrapper 的 355~987 区间内),
    对着整卡做带随机偏移的拟人点击有实打实的概率撞上它 —— 故点击目标收窄到
    ``.collection-plugin-content``(右边界 859 < 867,与创建入口零重叠)。
    """
    monkeypatch.setattr(podcast_mod.time, "sleep", lambda *_: None)
    monkeypatch.setattr(podcast_mod, "_COLLECTION_SELECT_TIMEOUT_S", 0.1)
    page, handlers, els = _publish_form_page(with_options=True)
    human = _wire(page, handlers)
    podcast_mod.select_podcast_collection(page, human, "心理急救包")
    clicked = [t for t, _ in human.clicks]
    assert els["create_link"] not in clicked
    assert els["content"] in clicked, "该点的是不含创建入口的内容区"


def test_select_collection_fails_loud_when_options_never_render(monkeypatch):
    """点开了但候选列表始终不出现 → fail-loud(展开后的结构本轮**未取证**)。"""
    monkeypatch.setattr(podcast_mod.time, "sleep", lambda *_: None)
    monkeypatch.setattr(podcast_mod, "_COLLECTION_SELECT_TIMEOUT_S", 0.1)
    page, handlers, _ = _publish_form_page()
    human = _wire(page, handlers)
    out = podcast_mod.select_podcast_collection(page, human, "心理急救包")
    assert out["status"] == "error"
    assert out["reason"].startswith("podcast_collection_not_in_options")
    assert out["observed"]["collection_card_present"] is True, "取证要说清卡片是在的"


def test_select_collection_reports_a_missing_card_distinctly(monkeypatch):
    """整张合集卡都不在 → 另一种 reason(与"卡在、候选没出来"必须可区分)。"""
    monkeypatch.setattr(podcast_mod.time, "sleep", lambda *_: None)
    page = _Page(elements=[], body_text="发布笔记")
    out = podcast_mod.select_podcast_collection(page, _Human(), "心理急救包")
    assert out["status"] == "error"
    assert out["reason"].startswith("podcast_collection_field_not_found")
    assert out["observed"]["collection_card_present"] is False


def test_publish_form_probe_reads_the_captured_selectors():
    """发布表单取证读的是真值控件,不再只丢一段页面文本。"""
    page, _h, _e = _publish_form_page()
    got = podcast_mod._publish_form_probe(page)
    assert got["title_input_present"] is True
    assert got["body_editor_present"] is True
    assert got["setting_content_present"] is True
    assert got["collection_card_present"] is True
