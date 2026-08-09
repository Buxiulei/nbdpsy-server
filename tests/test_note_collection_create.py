"""建笔记合集:浏览器层判据 + 全流程(假 page)+ 服务层契约 + 台账纪律。

假件按 2026-08-09 号8 三段只读探针的**实拍**造:

- 创建入口是弹层底栏 ``.collection-plugin-popover .popover-footer-content``,文案「创建合集」;
- 创建表单是个 modal,两个字段(合集名称 / 合集简介)+ [取消][**创建并加入**];
- 弹层列表接口是 ``note/collection/pc/list_v2``(它自己也含 ``collection`` 字样,
  创建 API 的宽松匹配必须把它排掉 —— 这条单独有测试)。

这一族测试的核心是**判据不能被自己打的字骗到**(播客合集 7 单假绿的教训),所以
"modal 收起"与"重进后干净列表里有这个名字"两个信号各自单独钉一条:任一缺失都必须
判失败,少钉一条就等于把假绿的门重新打开。
"""

import pytest

import app.browser.note_components as bnc
from app.services import browser_jobs_repo, note_collection_create


# ---------------- 假件 ----------------


class _El:
    """最小元素替身:标签 + 文本 + class + 属性 + 子元素。"""

    def __init__(self, tag="div", text="", cls="", attrs=None, children=None,
                 visible=True, html=""):
        self.detached = False   # 被下一次 render 换掉之后置 True(见 Scene.render)
        self.tag = tag
        self.text = text
        self.cls = cls
        self.attrs = {"class": cls, **dict(attrs or {})}
        self.children = list(children or [])
        self.visible = visible
        self.html = html
        self.typed = None

    # 读
    def inner_text(self):
        return self.text

    def inner_html(self):
        return self.html

    def is_visible(self):
        return self.visible

    def get_attribute(self, name):
        value = self.attrs.get(name)
        return None if value is None else str(value)

    def scroll_into_view_if_needed(self, **_k):
        return None

    # 查询(只在自己的子树里找)
    def query_selector(self, selector):
        return next(iter(self.query_selector_all(selector)), None)

    def query_selector_all(self, selector):
        out = []
        for child in self.children:
            if _matches(child, selector):
                out.append(child)
            out.extend(child.query_selector_all(selector))
        return out


def _matches(el: "_El", selector: str) -> bool:
    """够用的选择器匹配:标签 / .class / [attr] / [attr*='v'],逗号为并集;后代组合按最后一段。"""
    for part in selector.split(","):
        part = part.strip()
        if not part:
            continue
        # 后代组合(如 ".collection-plugin-popover .popover-footer-content"):
        # 假 DOM 的查询本来就只在子树里跑,判最后一段即可
        part = part.split()[-1]
        if _matches_one(el, part):
            return True
    return False


def _matches_one(el: "_El", part: str) -> bool:
    rest = part
    if rest == "*":
        return True
    if rest and (rest[0].isalpha()):
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
    """假拟人层:点击触发注册的副作用,输入写进元素 value。

    ``typed_detached`` 是这个替身最要紧的一格:往**已脱离文档**的节点里打字在真 DOM 上
    **不会报错**,字就这么没了、回执还是绿的(经典静默失败)。假件把它记下来,测试才拦得住。
    """

    def __init__(self, page=None):
        self.page = page
        self.clicks = []
        self.typed = []
        self.typed_detached = []
        self.handlers = {}

    def click(self, target, *, reason="", **_k):
        self.clicks.append((reason, getattr(target, "text", target)))
        fn = self.handlers.get(id(target)) if hasattr(target, "text") else None
        if fn:
            fn()

    def type_text(self, target, text, **_k):
        self.typed.append((target, text))
        if getattr(target, "detached", False):
            # 真 DOM 上这一下静默无效;这里如实记账,让测试能断言"没往死节点里打字"
            self.typed_detached.append((target, text))
        target.attrs["value"] = text
        target.typed = text

    def hover(self, *_a, **_k):
        return None

    def scroll(self, *_a, **_k):
        return None

    def wait(self, *_a, **_k):
        return None


class _Request:
    def __init__(self, method):
        self.method = method


class _Response:
    def __init__(self, url, body, *, method="GET", status=200, text=""):
        self.url = url
        self._body = body
        self.request = _Request(method)
        self.status = status
        self._text = text

    def json(self):
        if self._body is None:
            raise ValueError("no json")
        return self._body

    def text(self):
        return self._text


class _Page:
    """假 page:按 roots 回答查询,response 监听真回放,可注册"点了谁之后页面变成什么样"。"""

    def __init__(self, roots=None):
        self.roots = list(roots or [])
        self._listeners = []
        self.body_text = "内容设置 权限设置"
        self.waits = 0

    def on(self, event, fn):
        if event == "response":
            self._listeners.append(fn)

    def remove_listener(self, event, fn):
        if event == "response" and fn in self._listeners:
            self._listeners.remove(fn)

    def emit(self, response):
        for fn in list(self._listeners):
            fn(response)

    def inner_text(self, _sel):
        return self.body_text

    def query_selector(self, selector):
        return next(iter(self.query_selector_all(selector)), None)

    def query_selector_all(self, selector):
        out = []
        for root in self.roots:
            if _matches(root, selector):
                out.append(root)
            out.extend(root.query_selector_all(selector))
        return out

    def wait_for_timeout(self, _ms):
        self.waits += 1

    def evaluate(self, _js, _arg=None):
        return None


_LIST_URL = "https://creator.xiaohongshu.com/api/note/collection/pc/list_v2?page=1"


def _list_body(items):
    return {"data": {"collection_info_list": [
        {"id": cid, "name": nm, "desc": "", "note_num": num}
        for cid, nm, num in items
    ]}}


# ---------------- 场景搭建 ----------------


class Scene:
    """一个能被点出副作用的假编辑器页(合集入口 → 弹层 → 创建 modal)。

    ``collections`` 是平台侧"当前存在的合集";点「创建并加入」默认会把新合集加进去
    (即"创建即时落地"),各用例按需改这个行为来复刻不同的失败形态。
    """

    def __init__(self, collections=(), *, submit_disabled_cls="disabled",
                 name_placeholder="请输入合集名称", desc_placeholder="请输入合集简介",
                 fields=2):
        self.collections = list(collections)
        self.page = _Page()
        self.human = _Human(self.page)
        self.popover_open = False
        self.modal_open = False
        self.submit_clicked = 0
        self.create_persists = True     # 点了创建之后平台侧是否真落库
        self.modal_closes = True        # 点了创建之后表单是否收起
        # "并加入"是否**即时**把载体挂进去(未取证,两种可能各有一条用例):
        # False=合集建成但载体没自动挂上(重进后弹层还开得了,走列表回读那一支);
        # True=载体已在合集里(重进后入口不渲染,走 carrier chip 那一支)。
        self.join_carrier = False
        self.carrier_in_collection = None
        self.reloads = 0
        self._submit_disabled_cls = submit_disabled_cls
        self._name_placeholder = name_placeholder
        self._desc_placeholder = desc_placeholder
        self._fields = fields
        self.render()

    # -- DOM 渲染:每次状态变化重建一遍 roots(比在树上做增删更贴近 Vue 重渲染) --
    def render(self):
        # 上一轮的节点全部标记脱离文档:谁还攥着旧句柄,下一次读写就是在读一份死 DOM
        for root in self.page.roots:
            for el in [root] + root.query_selector_all("*"):
                el.detached = True
        roots = []
        if self.carrier_in_collection:
            roots.append(_El("div", self.carrier_in_collection,
                             cls="collection-plugin-choose"))
        else:
            roots.append(_El("div", "选择合集", cls="collection-plugin-button"))
        # 页面上还有笔记标题框:任何"退到 modal 之外找 input"的实现都会摸到它
        roots.append(_El("input", "", attrs={"placeholder": "填写标题", "value": ""}))
        if self.popover_open:
            items = [_El("div", name, cls="item") for _cid, name, _n in self.collections]
            footer = _El("div", "创建合集", cls="popover-footer-content")
            roots.append(_El(
                "div", "合集 " + " ".join(n for _c, n, _x in self.collections) + " 创建合集",
                cls="collection-plugin-popover",
                children=[_El("div", "", cls="collection-plugin-popover-content",
                              children=items), footer],
            ))
        if self.modal_open:
            fields = []
            if self._fields >= 1:
                fields.append(_El("input", "", cls="d-text",
                                  attrs={"placeholder": self._name_placeholder,
                                         "value": ""}))
            if self._fields >= 2:
                fields.append(_El("textarea", "", cls="d-text",
                                  attrs={"placeholder": self._desc_placeholder,
                                         "value": ""}))
            for extra in range(2, self._fields):
                fields.append(_El("input", "", cls="d-text",
                                  attrs={"placeholder": f"未知字段{extra}", "value": ""}))
            cancel = _El("button", "取消", cls="d-button")
            submit = _El("button", self._submit_text(), cls=self._submit_cls())
            # 外层还有个 innerText 同为「创建并加入」的容器:点它不生效(播客合集同型陷阱)
            wrapper = _El("div", self._submit_text(), cls="footer-btn-area",
                          children=[submit])
            modal = _El(
                "div",
                "创建合集 合集名称 0/20 合集简介 0/50 取消 " + self._submit_text(),
                cls="d-modal", children=fields + [cancel, wrapper],
                html="<div class='d-modal'>创建合集</div>",
            )
            roots.append(modal)
            self._submit_el = submit
            self._cancel_el = cancel
        self.page.roots = roots
        self._wire()

    def _submit_text(self):
        return "创建并加入"

    def _submit_cls(self):
        filled = bool(self._typed_name)
        return "d-button create-btn" + ("" if filled else f" {self._submit_disabled_cls}")

    _typed_name = ""

    def _wire(self):
        handlers = {}
        for root in self.page.roots:
            for el in [root] + root.query_selector_all("*"):
                if "collection-plugin-button" in el.cls:
                    handlers[id(el)] = self.open_popover
                elif "popover-footer-content" in el.cls:
                    handlers[id(el)] = self.open_modal
                elif el.tag == "button" and el.text == self._submit_text():
                    handlers[id(el)] = self.submit
        self.human.handlers = handlers
        # 输入要能改变按钮禁用态:包一层 type_text
        original = self.human.type_text

        def _type(target, text, **k):
            original(target, text, **k)
            if self._name_placeholder in (target.attrs.get("placeholder") or ""):
                self._typed_name = text
                self.render()

        self.human.type_text = _type

    # -- 副作用 --
    def open_popover(self):
        self.popover_open = True
        self.render()
        self.page.emit(_Response(_LIST_URL, _list_body(self.collections)))

    def open_modal(self):
        self.modal_open = True
        self.render()

    def submit(self):
        self.submit_clicked += 1
        name = self._typed_name
        self.page.emit(_Response(
            "https://creator.xiaohongshu.com/api/note/collection/pc/create",
            None, method="POST", status=200,
            text='{"success":true,"data":{"id":"newcid"}}',
        ))
        if self.create_persists:
            self.collections.append(("newcid", name, 1 if self.join_carrier else 0))
            if self.join_carrier:
                self.carrier_in_collection = name
        if self.modal_closes:
            self.modal_open = False
            self.popover_open = False
        self.render()

    def reload(self):
        """重进更新页:丢弃一切未提交的编辑器状态(弹层/表单都关掉)。"""
        self.reloads += 1
        self.popover_open = False
        self.modal_open = False
        self._typed_name = ""
        self.render()


@pytest.fixture()
def fast(monkeypatch):
    """把等待窗口压到刚够跑一两跳,并把拟人层/导航换成假的。"""
    for name, value in (
        ("_POPOVER_TIMEOUT_S", 0.4),
        ("_CREATE_MODAL_TIMEOUT_S", 0.4),
        ("_CREATE_ENABLE_TIMEOUT_S", 0.4),
        ("_CREATE_CLOSE_TIMEOUT_S", 0.4),
    ):
        monkeypatch.setattr(bnc, name, value)


def _run(monkeypatch, scene, name="读懂复杂性创伤", description=None,
         *, reload_raises=None):
    """把 SyncHumanActions / open_update_page 接到场景上,跑一次创建。"""
    monkeypatch.setattr(bnc, "SyncHumanActions", lambda _page: scene.human)

    def _open(_page, _account_id, _note_id):
        if reload_raises and scene.reloads >= 1:
            raise bnc.NoteComponentsError(reload_raises)
        scene.reload()

    monkeypatch.setattr(bnc, "open_update_page", _open)
    return bnc.create_note_collection(scene.page, 8, "n-carrier", name, description)


# ---------------- 纯函数:四路禁用判定(0.20.3 教训的同款回归锁) ----------------


def test_disabled_by_attribute():
    """有 disabled 属性 → 不可点(不看 class)。"""
    assert bnc.create_join_enabled("d-button", True) is False


def test_bare_disabled_token_is_not_enabled():
    """裸 token ``disabled``(播客 7 单假绿的真实形态)→ 不可点。"""
    cls = ("d-button d-button-large --size-icon-large disabled --color-static bold "
           "--color-bg-primary create-btn")
    assert bnc.create_join_enabled(cls, False) is False


def test_suffix_disabled_class_is_not_enabled():
    """``xxx-disabled`` 结尾的类名(同一套设计系统的常见形态)→ 不可点。"""
    assert bnc.create_join_enabled("d-button create-btn-disabled", False) is False
    assert bnc.create_join_enabled("d-button d-button-disabled", False) is False


def test_loading_token_is_not_enabled():
    """loading 态点了也白点 → 不可点。"""
    assert bnc.create_join_enabled("d-button d-button-primary-loading", False) is False


def test_whole_word_not_substring():
    """整词比较:粘连词不误伤 —— 否则按钮被**永久判死**(比假绿更难查的反向故障)。"""
    for cls in ("d-button xdisabled", "d-button disabledx", "d-button --color-static",
                "d-button is-loading", "d-button undisabled-thing"):
        assert bnc.create_join_enabled(cls, False) is True, cls


def test_empty_disabled_attribute_string_counts_as_disabled():
    """``<button disabled>`` 的属性读回来是**空串** —— 必须按 ``is not None`` 判。

    拿 ``bool("")`` 判会把一颗明确禁用的按钮判成可点,与 0.20.3 那条"禁用形态漏了一种"
    是同一类错误,只是换了个读法。
    """
    button = _El("button", "创建并加入", cls="d-button", attrs={"disabled": ""})
    assert bnc.read_create_join_state(button)["enabled"] is False


def test_button_missing_is_not_enabled():
    """按钮找不到 → ``{"found": False}``,调用方当不可点(找不到 ≠ 可以点了)。"""
    state = bnc.read_create_join_state(None)
    assert state == {"found": False} and not state.get("enabled")


# ---------------- 纯函数:创建 API 取证 ----------------


def test_create_api_capture_excludes_list_v2():
    """宽松匹配 ``collection`` **必须**把弹层列表接口 list_v2 排除掉。

    list_v2 的 URL 自己就含 ``collection`` 字样,只靠关键词匹配必然把它抓进来,
    取证里就全是列表响应、真正的创建响应会被条数上限挤掉。
    """
    capture = bnc._CreateApiCapture()
    capture.handle(_Response(_LIST_URL, None, method="POST", text="{}"))
    assert capture.entries == [], "list_v2 绝不能被当成创建 API"


def test_create_api_capture_only_takes_post():
    """只收 POST:写操作才可能是创建(与排除 list_v2 是两道独立过滤)。"""
    capture = bnc._CreateApiCapture()
    capture.handle(_Response("https://x/api/collection/detail", None,
                             method="GET", text="{}"))
    assert capture.entries == []
    capture.handle(_Response("https://x/api/collection/create", None,
                             method="POST", status=200, text='{"data":{"id":"c9"}}'))
    assert len(capture.entries) == 1
    assert capture.entries[0]["status"] == 200


def test_create_api_capture_ignores_unrelated_posts():
    """URL 里没有 collection 字样的 POST 一律不收(别把整个页面的请求都吞进回执)。"""
    capture = bnc._CreateApiCapture()
    capture.handle(_Response("https://x/api/note/update", None, method="POST", text="{}"))
    assert capture.entries == []


def test_parse_created_id_reads_data_id():
    """能从 ``data.id`` 抠出 id;抠不到给 None(**不做深度递归**,免得捞到别的 id)。"""
    assert bnc.parse_created_collection_id(
        [{"body": '{"success":true,"data":{"id":"cid-1"}}'}]) == "cid-1"
    assert bnc.parse_created_collection_id([{"body": "not json"}]) is None
    assert bnc.parse_created_collection_id(
        [{"body": '{"data":{"user_id":"u1"}}'}]) is None
    assert bnc.parse_created_collection_id([]) is None


# ---------------- 建前查重 ----------------


def test_duplicate_name_refuses_to_create(monkeypatch, fast):
    """列表里已有同名 → 立刻 error,**一次都不点创建**,并把现有那条的 id/note_num 回执。

    平台不去重同名(播客合集实证),重建只会多出一个空合集要人工删。
    """
    scene = Scene(collections=[("c1", "读懂复杂性创伤", 7)])
    out = _run(monkeypatch, scene)
    assert out["status"] == "error"
    assert out["reason"].startswith("collection_name_already_exists")
    assert out["collection_id"] == "c1" and out["note_num"] == 7
    assert scene.modal_open is False and scene.submit_clicked == 0
    assert not any("创建" in r for r, _t in scene.human.clicks if r), scene.human.clicks


def test_duplicate_check_is_exact_not_substring(monkeypatch, fast):
    """同族名(「读懂复杂性创伤」vs「读懂复杂性创伤2」)不算重复 —— 该建还得建。"""
    scene = Scene(collections=[("c1", "读懂复杂性创伤2", 3)])
    out = _run(monkeypatch, scene)
    assert out["status"] == "done", out
    assert scene.submit_clicked == 1


def test_catalog_unavailable_aborts_without_creating(monkeypatch, fast):
    """收不到 list_v2 → 查不了重,整单中止,**绝不硬着头皮建**。"""
    scene = Scene()
    scene.open_popover = lambda: (setattr(scene, "popover_open", True), scene.render())
    out = _run(monkeypatch, scene)
    assert out["status"] == "error"
    assert out["reason"].startswith("collection_catalog_unavailable")
    assert scene.submit_clicked == 0


# ---------------- 双信号判据 ----------------


def test_happy_path_is_done_with_both_signals(monkeypatch, fast):
    """表单收起 + 重进后干净列表里有这个名字 → done,id 取自列表。"""
    scene = Scene()
    out = _run(monkeypatch, scene, description="看懂复杂性创伤如何影响日常")
    assert out["status"] == "done", out
    assert out["confirmed_by"] == "modal_closed_and_in_fresh_list"
    assert out["collection_id"] == "newcid"
    assert out["name_preexisted"] is False
    # joined_carrier 就是重进后该合集的 note_num:本场景里「并加入」没随创建即时落地,
    # 于是它是 0 —— 这个字段的全部意义就是让首验能读出这件事,而不是替平台圆场。
    assert out["joined_carrier"] == 0
    # 点的是真 <button>,不是 innerText 同为「创建并加入」的外层容器
    assert scene.submit_clicked == 1
    assert not any(getattr(t, "cls", "") == "footer-btn-area"
                   for _r, t in scene.human.clicks if hasattr(t, "cls"))
    # 判定必须**重进过**更新页(干净列表回读),不是就地读页面文本
    assert scene.reloads >= 2


def test_modal_closed_but_absent_from_fresh_list_is_error(monkeypatch, fast):
    """表单收起了、但重进后的干净列表里没有 → **不算成功**(建没建成未知)。

    这一支正是"随笔记提交才生效"与"压根没建成"的共同形态,绝不能判 done。
    """
    scene = Scene()
    scene.create_persists = False
    out = _run(monkeypatch, scene)
    assert out["status"] == "error"
    assert out["reason"].startswith("collection_absent_from_fresh_list")
    assert out["modal_closed"] is True
    assert out["created_api_capture"], "创建 API 取证必须带出来,首验靠它分辨两种可能"


def test_list_has_name_but_modal_open_is_not_success(monkeypatch, fast):
    """列表里有了这个名字、但表单没收起 → **仍然不算成功**。

    单信号判据(只看列表)会在这里判绿。表单不收起说明提交没走通,平台侧列表里那条
    可能是别的来路 —— 双信号缺一不可,这条测试就是那把锁。
    """
    scene = Scene()
    scene.modal_closes = False          # 表单赖着不走
    out = _run(monkeypatch, scene)      # 但 create_persists=True,列表里会有这个名字
    assert out["status"] == "error"
    assert out["reason"].startswith("create_modal_still_open")
    assert out["modal_closed"] is False
    assert out["create_submit_state"]["found"] is True


def test_carrier_chip_after_reload_is_accepted(monkeypatch, fast):
    """重进后载体已在该合集里(入口因此不渲染)→ 算成功,id 退回创建 API 取证。

    未提交状态早被重进丢掉了还能读到,只可能是平台侧已独立落库 —— 这是比列表回读
    更强的证据,不能因为"弹层开不了"就判失败。
    """
    scene = Scene()
    scene.join_carrier = True           # 「并加入」即时落地:重进后载体就在合集里
    out = _run(monkeypatch, scene)
    assert out["status"] == "done", out
    assert out["confirmed_by"] == "modal_closed_and_carrier_chip"
    assert out["collection_id"] == "newcid"        # 取自创建 API 响应
    assert out["carrier_collection_label"] == "读懂复杂性创伤"


def test_reload_failure_is_unknown_not_success(monkeypatch, fast):
    """重进更新页失败 → 干净列表读不了 → 报"建没建成未知",绝不当成功。"""
    scene = Scene()
    out = _run(monkeypatch, scene, reload_raises="editor_not_ready: 页面没渲染出来")
    assert out["status"] == "error"
    assert out["reason"].startswith("verify_reload_failed")
    assert out["created_api_capture"], "已经点过创建了,取证必须留下"


# ---------------- 禁用按钮 / 表单结构 ----------------


def test_never_clicks_disabled_submit(monkeypatch, fast):
    """「创建并加入」始终禁用 → 报错,**一次都不点**。"""
    scene = Scene()
    scene._submit_cls = lambda: "d-button create-btn disabled"   # 填了也不解禁
    out = _run(monkeypatch, scene)
    assert out["status"] == "error"
    assert out["reason"].startswith("create_join_never_enabled")
    assert scene.submit_clicked == 0
    assert out["create_submit_state"]["enabled"] is False


def test_name_input_not_found_types_nothing(monkeypatch, fast):
    """认不出名称输入框 → fail-loud,**一个字都不填**(绝不退到 modal 之外找 input)。

    页面上就有笔记标题框,摸错一下就是对载体笔记的真实改动。
    """
    scene = Scene(name_placeholder="", desc_placeholder="", fields=3)
    out = _run(monkeypatch, scene)
    assert out["status"] == "error"
    assert out["reason"].startswith("collection_name_input_not_found")
    assert scene.human.typed == [], "认不出框就一个字都不能打"
    assert out["modal_html"], "结构未知时要留 HTML 取证(硬上限截断)"


def test_description_input_missing_is_error_not_silent_skip(monkeypatch, fast):
    """请求了简介却没有简介框 → 报错,不静默丢掉调用方给的字段。"""
    scene = Scene(fields=1)
    out = _run(monkeypatch, scene, description="一套自救练习")
    assert out["status"] == "error"
    assert out["reason"].startswith("collection_desc_input_not_found")
    assert scene.submit_clicked == 0


def test_description_is_typed_into_a_live_node(monkeypatch, fast):
    """填完名称会触发重渲染 → 简介必须**重新认领 modal 之后**再打,不能用旧句柄。

    往脱离文档的节点里打字在真 DOM 上**不报错**,简介就这么丢了、回执还是绿的 ——
    这条测试盯的就是那种静默丢字段。
    """
    scene = Scene()
    out = _run(monkeypatch, scene, description="一套自救练习")
    assert out["status"] == "done", out
    assert scene.human.typed_detached == [], (
        f"往已脱离文档的节点里打了字:{[t for _el, t in scene.human.typed_detached]}"
    )
    live_desc = [t for el, t in scene.human.typed
                 if "简介" in (el.attrs.get("placeholder") or "")]
    assert live_desc == ["一套自救练习"]


def test_name_input_found_by_placeholder_not_position(monkeypatch, fast):
    """字段顺序反过来时按 placeholder 认框,不按位置猜 —— 名字必须打进名称框。"""
    scene = Scene(name_placeholder="填写合集名称", desc_placeholder="填写合集简介")
    out = _run(monkeypatch, scene)
    assert out["status"] == "done", out
    typed = {(t.attrs.get("placeholder")): text for t, text in scene.human.typed}
    assert typed["填写合集名称"] == "读懂复杂性创伤"


def test_entry_missing_fails_loud(monkeypatch, fast):
    """载体笔记编辑页上没有「加入合集」入口 → 明确报错(多半是它已在别的合集里)。"""
    scene = Scene()
    scene.carrier_in_collection = "别的合集"
    scene.render()
    out = _run(monkeypatch, scene)
    assert out["status"] == "error"
    assert out["reason"].startswith("collection_entry_not_found")


def test_create_footer_missing_fails_loud(monkeypatch, fast):
    """弹层底栏没有「创建合集」 → 报错,不去别处乱点。"""
    scene = Scene()
    original = scene.open_popover

    def _open():
        original()
        scene.page.roots = [r for r in scene.page.roots
                            if "collection-plugin-popover" not in r.cls]

    scene.open_popover = _open
    scene._wire()
    out = _run(monkeypatch, scene)
    assert out["status"] == "error"
    assert out["reason"].startswith("collection_create_entry_not_found")


def test_empty_name_rejected_before_browser(monkeypatch, fast):
    """名称空白 → 直接拒,零点击。"""
    scene = Scene()
    out = _run(monkeypatch, scene, name="   ")
    assert out["status"] == "error" and out["reason"].startswith("collection_name_empty")
    assert scene.human.clicks == []


def test_empty_collection_list_is_not_an_error(monkeypatch, fast):
    """新号一个合集都没有 → 空列表照样往下建(拿"列表为空"当失败会让第一次永远建不成)。"""
    scene = Scene(collections=[])
    out = _run(monkeypatch, scene)
    assert out["status"] == "done", out


# ---------------- 服务层契约 ----------------


@pytest.fixture()
def no_browser(monkeypatch):
    """把 cookie 读取与同步执行体换成替身,返回一个可写的 holder。"""
    holder = {"result": {"status": "done", "name": "X", "collection_id": "c1"},
              "calls": []}

    async def _cookies(_account_id):
        return [{"name": "a", "value": "b"}]

    monkeypatch.setattr(note_collection_create, "load_account_cookies", _cookies)

    def _sync(account_id, _cookies_arg, name, description, carrier_note_id):
        holder["calls"].append((account_id, name, description, carrier_note_id))
        if isinstance(holder["result"], Exception):
            raise holder["result"]
        return holder["result"]

    monkeypatch.setattr(note_collection_create, "_create_sync", _sync)
    return holder


async def test_execute_success_passes_through(no_browser):
    """浏览器层 done → 原样返回。"""
    no_browser["result"] = {"status": "done", "name": "读懂复杂性创伤",
                            "collection_id": "c9",
                            "confirmed_by": "modal_closed_and_in_fresh_list"}
    out = await note_collection_create.execute(
        8, {"name": "读懂复杂性创伤", "description": "简介", "carrier_note_id": "n1"}
    )
    assert out["status"] == "done" and out["collection_id"] == "c9"
    assert no_browser["calls"] == [(8, "读懂复杂性创伤", "简介", "n1")]


async def test_browser_error_becomes_error_key(no_browser):
    """浏览器层 error → 翻译成 ``{"error": reason}``,**绝不以 done 收尾**。"""
    no_browser["result"] = {"status": "error",
                            "reason": "collection_name_already_exists: …",
                            "collection_id": "c1"}
    out = await note_collection_create.execute(
        8, {"name": "X", "carrier_note_id": "n1"}
    )
    assert out["error"].startswith("collection_name_already_exists")
    assert out["collection_id"] == "c1", "已有那条的 id 要透出来给调用方直接用"


async def test_exception_never_escapes(no_browser):
    """执行体抛异常 → 收敛成 error,不往外抛(抛了台账会悬挂在 running)。"""
    no_browser["result"] = RuntimeError("camoufox 挂了")
    out = await note_collection_create.execute(8, {"name": "X", "carrier_note_id": "n1"})
    assert "camoufox 挂了" in out["error"]


async def test_missing_name_or_carrier_rejected_before_browser(no_browser):
    """名称/载体笔记缺失 → 直接 error,**一次浏览器都不起**。"""
    assert "name" in (await note_collection_create.execute(
        8, {"carrier_note_id": "n1"}))["error"]
    assert "carrier_note_id" in (await note_collection_create.execute(
        8, {"name": "X"}))["error"]
    assert no_browser["calls"] == []


async def test_no_cookie_rejected(monkeypatch):
    """账号没 cookie → error(起了浏览器也进不去编辑器)。"""
    async def _cookies(_account_id):
        return []

    monkeypatch.setattr(note_collection_create, "load_account_cookies", _cookies)
    out = await note_collection_create.execute(8, {"name": "X", "carrier_note_id": "n1"})
    assert "cookie" in out["error"]


# ---------------- 台账 / 派发纪律 ----------------


def test_kind_is_not_idempotent():
    """``note_collection_create`` **不能**进幂等 kind 表:僵死自动重跑会建出重复合集。"""
    assert note_collection_create.KIND not in browser_jobs_repo._IDEMPOTENT_KINDS


def test_account_worker_resolves_execute():
    """account_worker 认得这个 kind —— 漏接线的话任务会永远躺在队列里。"""
    from app import account_worker

    assert account_worker._resolve_execute(note_collection_create.KIND) is not None


def test_preloaded_catalog_survives_render_only_popover(monkeypatch, fast):
    """列表随页面加载**预取**、点「加入合集」只渲染缓存不发新请求 → 回落到已捕获的
    预取响应,查重/创建照常走完。

    号8 图文载体首验实测形态(RCA 2026-08-09):等"点击后的新增响应"必然超时,当时整单
    误报 collection_catalog_unavailable;回落语义与 GET /collections 流程"先认预取"同源。
    """
    scene = Scene()
    orig_reload = scene.reload

    def reload_with_prefetch():
        orig_reload()
        scene.page.emit(_Response(_LIST_URL, _list_body(scene.collections)))

    scene.reload = reload_with_prefetch
    # 弹层只渲染,不发任何新请求(与真页面行为一致)
    scene.open_popover = lambda: (setattr(scene, "popover_open", True), scene.render())
    out = _run(monkeypatch, scene)
    assert out["status"] == "done", out
    assert scene.submit_clicked == 1
