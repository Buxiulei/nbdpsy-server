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


def _tab_eval(active_text):
    """伪造 ``active_tab_text`` 用的那段只读 JS。"""
    def _fn(script, *a):
        if "creator-tab.active" in script:
            return active_text
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
        return state["active"] if "creator-tab.active" in script else None

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

    def _fn(script, *a):
        if "creator-tab.active" in script:
            return "发播客"
        if "button.create-btn" in script:
            if state["stage"] != "create":
                return {"found": False}
            filled = bool(name.attrs.get("value")) and cover.files and state["cropped"]
            return {
                "found": True,
                "cls": "d-button create-btn" + ("" if filled else " create-btn-disabled"),
                "disabled_attr": not filled,
            }
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


def test_create_collection_unconfirmed_result_is_error(monkeypatch):
    """点了创建但既没回列表、列表里也没这个名字 → error(**做没做成未知**,不谎报成功)。"""
    monkeypatch.setattr(podcast_mod.time, "sleep", lambda *_: None)
    monkeypatch.setattr(podcast_mod, "_CREATE_RESULT_TIMEOUT_S", 0.1)
    page, handlers, _ = _collection_page()
    handlers[[k for k in handlers if getattr(k, "tag", "") == "button"
              and k.cls.startswith("d-button")][0]] = lambda: None  # 点创建没反应
    human = _wire(page, handlers)
    out = podcast_mod.create_collection(page, human, "心理急救包", None, "/tmp/c.png")
    assert out["status"] == "error"
    assert out["reason"].startswith("create_result_unconfirmed")


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
