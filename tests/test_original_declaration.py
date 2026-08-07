"""原创声明开关(apply_original_declaration)单测 + 发布链接线锁,不起真浏览器。

夹具证据 tests/fixtures/pages/content_settings.json(2026-08-05 真号采集):
- 开关 .original-wrapper .d-switch,状态在隐藏 input.checked(class 不翻转);
- 首次点开弹无底栏说明弹窗(d-modal-no-footer,仅右上 X,无确认按钮);
- X 关闭后 checked 是否保持**没有**直接证据 → 实现防御回读+重点,本文件把两种走向都锁住。
"""

from app.browser import note_components as bnc
from app.browser import sync_client as sc


class _El:
    def __init__(self, page, role):
        self.page, self.role = page, role

    def scroll_into_view_if_needed(self):
        pass


class _Human:
    """记录点击/等待;点击按元素角色驱动假页面状态机。"""

    def __init__(self, page):
        self.page = page
        self.clicks = []

    def click(self, el, reason=""):
        self.clicks.append(reason)
        self.page.on_click(el.role)

    def wait(self, *_a, **_kw):
        pass

    def hover(self, *_a, **_kw):
        pass


class _Page:
    """假发布页:原创声明行 + 开关 + 首开说明弹窗。"""

    def __init__(self, has_row=True, checked=False, modal_on_first=True,
                 close_reverts=False, dead_switch=False):
        self.has_row = has_row
        self.checked = checked
        self.modal_open = False
        self.toggles = 0
        self.modal_on_first = modal_on_first
        self.close_reverts = close_reverts
        self.dead_switch = dead_switch

    def query_selector(self, sel):
        if sel == bnc._ORIGINAL_ROW:
            return _El(self, "row") if self.has_row else None
        if sel == bnc._ORIGINAL_SWITCH:
            return _El(self, "toggle") if self.has_row else None
        if sel == bnc._ORIGINAL_MODAL_CLOSE:
            return _El(self, "close") if self.modal_open else None
        return None

    def evaluate(self, js, *_a):
        if "row-band-probe" in js:
            self.band_probes = getattr(self, "band_probes", 0) + 1
            return {"cx": 942, "cy": 500, "ih": 1266}   # 中带,不触发滚动
        if "original-wrapper" in js:
            return self.checked
        return None

    def on_click(self, role):
        if role == "toggle":
            self.toggles += 1
            if self.dead_switch:
                return
            self.checked = not self.checked
            if self.toggles == 1 and self.modal_on_first:
                self.modal_open = True
        elif role == "close":
            self.modal_open = False
            if self.close_reverts:
                self.checked = False


def _run(page):
    return bnc.apply_original_declaration(page, _Human(page))


def test_already_on_is_skipped_zero_clicks():
    """已是开态 → skipped,一次点击都不发生(幂等重跑安全)。"""
    page = _Page(checked=True)
    result = _run(page)
    assert result["status"] == "skipped"
    assert page.toggles == 0


def test_off_toggles_on_closes_modal_and_confirms():
    """关态 → 点开关 → 关说明弹窗 → 回读 checked=True → done。"""
    page = _Page()
    result = _run(page)
    assert result["status"] == "done"
    assert page.toggles == 1
    assert page.modal_open is False   # 弹窗必须关掉(不关会盖住发布按钮)
    assert getattr(page, "band_probes", 0) >= 1   # 点开关前做过防遮挡探测(接线锁)


def test_close_reverting_gets_second_toggle():
    """X 关弹窗把开关打回关态(证据缺口的防御走向)→ 再点一次 → done。"""
    page = _Page(close_reverts=True)
    result = _run(page)
    assert result["status"] == "done"
    assert page.toggles == 2


def test_dead_switch_reports_not_applied():
    """点了封顶轮数开关仍不是开态 → error(回读为准,"没报错"不算数)。"""
    page = _Page(dead_switch=True)
    result = _run(page)
    assert result["status"] == "error"
    assert "original_not_applied" in result["reason"]


def test_missing_row_reports_entry_not_found():
    """页面没有原创声明入口 → error,零点击。"""
    page = _Page(has_row=False)
    result = _run(page)
    assert result["status"] == "error"
    assert "original_entry_not_found" in result["reason"]
    assert page.toggles == 0


# ── 发布链接线锁:每次发布无条件走原创声明步,结果并进 components 回显 ──


class _FakeAtomicOK:
    def __init__(self, page, job_tag=None):
        self.page = page

    def step1_open_publish_page(self):
        return {"success": True}

    def step2_upload_images(self, image_paths):
        return {"success": True, "uploaded_count": len(image_paths)}

    def step3_wait_for_upload_processing(self, max_wait=30):
        return {"success": True, "edit_page_loaded": True}

    def step5_fill_content(self, title, content):
        return {"success": True}

    def step7_click_publish_and_wait(self, max_wait=30):
        return {"success": True, "note_url": "https://xhs/n1", "note_id": "n1"}


def test_publish_chain_always_applies_original_declaration(monkeypatch):
    """发布成功链无条件调 apply_original_declaration(运营裁定 2026-08-05:每次发布都开),
    结果并进返回值 components.original_declaration;不传组件也照走。"""
    seen = {}

    monkeypatch.setattr(sc, "XHSPublishAtomicTasks", _FakeAtomicOK)
    monkeypatch.setattr(sc, "SyncHumanActions", lambda page: object())
    def fake_apply(page, human, *, handle_consent_modal=False):
        # 图文任务现在**也**走协议弹窗链(老序列经探针+生产数据双实锤从未成功过)
        seen["called"] = True
        seen["consent_modal"] = handle_consent_modal
        return {"status": "done"}

    monkeypatch.setattr(sc, "apply_original_declaration", fake_apply)

    client = sc.SyncClient(account_id=1, cookies=[])
    result = client.publish_note("标题", "正文", ["/tmp/a.png"])

    assert result["success"] is True
    assert seen.get("called") is True
    assert seen["consent_modal"] is True, "图文路径现在也必须走完整协议链"
    assert result["components"]["original_declaration"] == {"status": "done"}


def test_original_declaration_failure_never_blocks_publish(monkeypatch):
    """原创声明步炸了 → 发布照常成功,components 里如实记 error(辅助步不阻断)。"""
    monkeypatch.setattr(sc, "XHSPublishAtomicTasks", _FakeAtomicOK)
    monkeypatch.setattr(sc, "SyncHumanActions", lambda page: object())

    def boom(page, human):
        raise RuntimeError("开关炸了")

    monkeypatch.setattr(sc, "apply_original_declaration", boom)

    client = sc.SyncClient(account_id=1, cookies=[])
    result = client.publish_note("标题", "正文", ["/tmp/a.png"])

    assert result["success"] is True
    assert "original_exception" in result["components"]["original_declaration"]["reason"]


# ---------------- 原创声明协议弹窗:勾同意 → 等解禁 → 点「声明原创」 ----------------
#
# 夹具铁证(tests/fixtures/pages/content_settings.json,2026-08-05 真号):
#   original_row.checkbox_checked      = False   ← 初始
#   after_toggle_on.checkbox_checked   = True    ← **点开关那一刻就 true,而弹窗还开着没确认**
# 也就是说隐藏 input.checked 是**乐观 UI 态**,不是"已声明"的证据。谁拿它当终判,
# 谁就会在关掉弹窗、什么都没声明的情况下报成功。

from app.browser import note_components as _bnc


class _FakeEl:
    """最小元素替身:能报文案/class,也能在子树里找文案(供 _find_text_in_section 用)。

    ``selector`` 记的是"这个替身是从哪个选择器查出来的",供点击 spy 断言**点了谁**。
    """

    def __init__(self, text="", children=(), selector=None):
        self._text = text
        self._children = list(children)
        self.selector = selector

    def inner_text(self):
        return self._text

    def get_attribute(self, name):
        return self._text if name == "class" else None

    def query_selector_all(self, _sel):
        return list(self._children)


class _ConsentModalPage:
    """会弹协议弹窗的假页面:必须勾同意 + 点「声明原创」才算真声明。"""

    def __init__(self, *, checkbox_found=True, button_ever_enables=True,
                 simulator_missing_first_n=0, consent_click_effective=True):
        self.modal_open = False
        self.consent_ticked = False
        self.declared = False          # 只有点了「声明原创」才 True
        self.optimistic_checked = False  # 模拟平台的乐观 UI 态
        self.saw_optimistic_true = False
        self.closed_by_x = False
        self._checkbox_found = checkbox_found
        self._button_ever_enables = button_ever_enables
        # simulator 方块查不到的前 N 次(模拟"渲染晚一拍"→ 走回退点宽容器)
        self._simulator_missing_first_n = simulator_missing_first_n
        self._simulator_queries = 0
        # 点了协议复选框会不会真勾上 —— False 复刻"落点撞《原创声明须知》链接、
        # 事件被链接吃掉没冒泡到 toggle"的生产失败态
        self.consent_click_effective = consent_click_effective

    # --- 被测代码会用到的 page API ---
    def query_selector(self, sel):
        if sel in (_bnc._ORIGINAL_ROW, _bnc._ORIGINAL_SWITCH):
            return f"EL:{sel}"
        if sel == _bnc._ORIGINAL_MODAL:
            if not self.modal_open:
                return None
            return _FakeEl("协议弹窗", [_FakeEl(_bnc._ORIGINAL_CONFIRM_TEXT)])
        if sel == _bnc._ORIGINAL_MODAL_CLOSE:
            return "EL:x" if self.modal_open else None
        if sel == _bnc._ORIGINAL_CONSENT_SIMULATOR:
            self._simulator_queries += 1
            if not self.modal_open:
                return None
            if self._simulator_queries <= self._simulator_missing_first_n:
                return None
            cls = "d-checkbox-simulator" + ("" if self.consent_ticked else " unchecked")
            return _FakeEl(cls, selector=sel)
        if sel == _bnc._ORIGINAL_CONFIRM_BUTTON:
            return _FakeEl(_bnc._ORIGINAL_CONFIRM_TEXT) if self.modal_open else None
        if "checkbox" in sel or "d-checkbox" in sel:
            if not (self.modal_open and self._checkbox_found):
                return None
            return _FakeEl("d-checkbox d-checkbox-main-label d-clickable", selector=sel)
        return None

    def query_selector_all(self, sel):
        el = self.query_selector(sel)
        return [el] if el else []

    def evaluate(self, script, *args):
        if "checked" in script and "original-wrapper" in script:
            return self.optimistic_checked
        if args and args[0] == _bnc._ORIGINAL_CONFIRM_TEXT:
            # 按钮可点 = 已勾同意(且本用例允许它解禁)
            return bool(self.consent_ticked and self._button_ever_enables)
        return None

    def wait_for_timeout(self, _ms):
        return None


class _ConsentHuman:
    def __init__(self, page):
        self.page = page
        self.clicks = []
        # 点击 spy:每次点击记 (被点元素的选择器, random_offset, reason)
        self.click_log = []

    def click(self, target, reason="", random_offset=True, **_k):
        self.clicks.append(reason)
        self.click_log.append(
            (getattr(target, "selector", target), random_offset, reason)
        )
        if "开关" in reason:
            self.page.modal_open = True
            self.page.optimistic_checked = True   # ← 乐观翻转,弹窗还没确认
            self.page.saw_optimistic_true = True  # 记账:之后关弹窗会把它打回 False
        elif "同意" in reason:
            if self.page.consent_click_effective:
                self.page.consent_ticked = True
        elif "声明原创" in reason:
            if self.page.consent_ticked:
                self.page.declared = True
                self.page.modal_open = False
                self.page.optimistic_checked = True   # 真声明 → 开关留在开态
        elif "关掉" in reason or "关闭" in reason:
            self.page.modal_open = False
            self.page.closed_by_x = True
            self.page.optimistic_checked = False  # 探针实测:X 关掉 → checked 重置 False

    def hover(self, *_a, **_k):
        return None

    def wait(self, *_a, **_k):
        return None

    def scroll(self, *_a, **_k):
        return None


def _run_consent(page):
    import app.browser.note_components as m

    human = _ConsentHuman(page)
    out = m.apply_original_declaration(page, human, handle_consent_modal=True)
    return out, human


def test_consent_modal_chain_actually_declares(monkeypatch):
    """走完整链:勾「我已阅读并同意」→ 等「声明原创」解禁 → 点它 → done。"""
    monkeypatch.setattr(_bnc, "_scroll_row_to_mid_viewport", lambda *a, **k: None)
    page = _ConsentModalPage()
    out, human = _run_consent(page)

    assert out["status"] == "done", out
    assert page.declared is True, "没点到「声明原创」就不算声明"
    assert page.closed_by_x is False, "走成了就不该用 X 关弹窗"
    # 顺序:先勾同意,再点声明原创
    tick = next(i for i, r in enumerate(human.clicks) if "同意" in r)
    confirm = next(i for i, r in enumerate(human.clicks) if "声明原创" in r)
    assert tick < confirm, human.clicks


def test_optimistic_checked_alone_is_not_accepted_as_done(monkeypatch):
    """**核心回归**:隐藏 input.checked 已经 true,但协议没确认 → 绝不许报 done。

    这正是夹具里 after_toggle_on.checkbox_checked=True 而弹窗仍开着的那一刻。
    拿 checked 当终判 = 关掉弹窗什么都没声明却报成功。
    """
    monkeypatch.setattr(_bnc, "_scroll_row_to_mid_viewport", lambda *a, **k: None)
    page = _ConsentModalPage(checkbox_found=False)  # 勾不上 → 链走不完
    out, _human = _run_consent(page)

    assert page.saw_optimistic_true is True, "夹具语义:点开关即乐观翻 true"
    assert out["status"] == "error", f"checked=true 但没声明,不许报 done: {out}"
    assert "consent" in out["reason"]


def test_modal_closed_when_consent_chain_cannot_complete(monkeypatch):
    """链走不完必须把弹窗关掉 —— 残留弹窗会盖住发布按钮(2026-08-02 事故同型)。"""
    monkeypatch.setattr(_bnc, "_scroll_row_to_mid_viewport", lambda *a, **k: None)
    page = _ConsentModalPage(button_ever_enables=False)
    out, _human = _run_consent(page)

    assert out["status"] == "error"
    assert page.modal_open is False, "弹窗必须关掉,否则盖住发布按钮"
    assert page.declared is False


def test_no_modal_falls_back_to_checked_readback(monkeypatch):
    """页面压根不弹协议弹窗(发布页可能如此)→ 退回读 checked 的老判据,不误报。"""
    monkeypatch.setattr(_bnc, "_scroll_row_to_mid_viewport", lambda *a, **k: None)

    class _NoModalPage(_ConsentModalPage):
        def query_selector(self, sel):
            if sel == _bnc._ORIGINAL_MODAL:
                return None          # 永不弹窗
            return super().query_selector(sel)

    page = _NoModalPage()
    out, human = _run_consent(page)
    assert out["status"] == "done", out
    assert all("同意" not in r for r in human.clicks), "没弹窗就不该去勾任何协议框"


# ---------------- 勾选的**点击目标**:16×16 的 simulator 方块,不是宽容器 ----------------
#
# 真号录屏实测(2026-08-07,账号2)三个矩形:
#   容器 .d-checkbox.d-checkbox-main-label : x=506 y=483 w=508 h=23  中心 (760,494)
#   simulator 方块 .d-checkbox-simulator   : x=506 y=486 w=16  h=16  中心 (514,494)
#   链接《原创声明须知》.custom-link        : x=636       w=107      → 页面 636~743
# human.click 默认 random_offset=True → 落点是容器宽度 30%~70% 的随机位置,对 w=508
# 就是页面 658~862,与链接区间 636~743 **重叠 658~743,约占随机区间 40%**。


def _consent_click(human):
    """从 spy 里取"勾同意"那一次点击 (selector, random_offset, reason)。"""
    hits = [c for c in human.click_log if "同意" in c[2]]
    assert len(hits) == 1, f"勾同意应恰好点一次: {human.click_log}"
    return hits[0]


def test_consent_click_targets_the_simulator_square(monkeypatch):
    """勾选点的必须是 simulator 方块,且**不加随机偏移**。

    点宽容器时随机偏移有约 40% 概率落进《原创声明须知》链接,链接吃掉事件不冒泡到
    父级 toggle → 勾不上。方块里不含链接,点它必然只触发 toggle。
    """
    monkeypatch.setattr(_bnc, "_scroll_row_to_mid_viewport", lambda *a, **k: None)
    page = _ConsentModalPage()
    out, human = _run_consent(page)

    selector, random_offset, _reason = _consent_click(human)
    assert selector == _bnc._ORIGINAL_CONSENT_SIMULATOR, (
        f"勾选必须点 simulator 方块,实际点了 {selector!r} —— 宽容器有撞链接风险"
    )
    assert random_offset is False, "16×16 的方块上随机偏移毫无拟人价值,还可能点出界"
    assert out["status"] == "done", out
    assert page.declared is True


def test_consent_falls_back_to_container_when_simulator_missing(monkeypatch):
    """simulator 方块定位不到(平台改版 / 渲染晚一拍)→ 回退点宽容器,不是干脆不点。"""
    monkeypatch.setattr(_bnc, "_scroll_row_to_mid_viewport", lambda *a, **k: None)
    # 第 1 次查 simulator(取点击目标)扑空,之后(回读勾选态)才查得到
    page = _ConsentModalPage(simulator_missing_first_n=1)
    out, human = _run_consent(page)

    selector, _random_offset, _reason = _consent_click(human)
    assert selector in _bnc._ORIGINAL_CONSENT_CANDIDATES, (
        f"simulator 缺失时应回退点容器,实际点了 {selector!r}"
    )
    assert out["status"] == "done", out
    assert page.declared is True


def test_consent_readback_unticked_stops_the_chain(monkeypatch):
    """点了但回读 simulator 仍是 unchecked(复刻撞链接)→ 立刻报错,不往下走。"""
    monkeypatch.setattr(_bnc, "_scroll_row_to_mid_viewport", lambda *a, **k: None)
    page = _ConsentModalPage(consent_click_effective=False)
    out, human = _run_consent(page)

    assert out["status"] == "error", f"没勾上就不许继续: {out}"
    assert "consent" in out["reason"], out["reason"]
    assert page.declared is False, "读态没确认就绝不能去点「声明原创」"
    assert all("声明原创" not in r for r in human.clicks), human.clicks
    assert page.modal_open is False, "走不完必须关弹窗,否则盖住发布按钮"


def test_legacy_close_modal_path_can_never_declare(monkeypatch):
    """回归留档:老路径(X 关弹窗)在真实语义下**永远声明不成功**。

    探针实测 X 关掉后 checked 重置 False,所以老序列重试到耗尽只会落 error ——
    这正是生产 08-05 以来 8/8 全 error 的成因。保留这条用例是为了钉住"老路径不可用"
    这个事实,防止有人日后把默认值改回去还以为没事。
    """
    monkeypatch.setattr(_bnc, "_scroll_row_to_mid_viewport", lambda *a, **k: None)
    page = _ConsentModalPage()
    human = _ConsentHuman(page)
    out = _bnc.apply_original_declaration(page, human, handle_consent_modal=False)

    assert page.closed_by_x is True, "老路径就是「弹窗出现就 X 关掉」"
    assert page.declared is False, "老路径根本没点过「声明原创」"
    assert out["status"] == "error", "X 关掉后 checked 重置 False → 只能落 error"


# ---------------- 发布页真号探针的回放锁(account10,2026-08-07) ----------------


def _publish_page_probe():
    import json
    import pathlib

    path = (pathlib.Path(__file__).parent / "fixtures" / "pages"
            / "original_modal_publish_page.json")
    return json.loads(path.read_text(encoding="utf-8"))


def test_probe_proves_publish_page_also_shows_consent_modal():
    """**发布页也弹协议弹窗** —— 图文生产路径确实会走到这里,不是只有编辑页。"""
    probe = _publish_page_probe()
    assert probe["modal_appeared"] is True


def test_probe_proves_checked_is_optimistic_but_reverts_on_close():
    """三个数定性了图文生产的真实失败模式:

    点 toggle → checked=True(乐观);X 关掉 → checked=**False**。
    所以老逻辑的回读**没有**把它报成成功,而是重试到耗尽后落 error ——
    是"一直没声明成功且如实报错",不是"静默假成功"。区别很大,别报错了性质。
    """
    probe = _publish_page_probe()
    assert probe["checked_immediately_after_click"]["checked"] is True
    assert probe["checked_after_close"]["checked"] is False


def test_probe_backs_the_consent_and_confirm_selectors():
    """选择器真值锁:协议框是可点的 .d-checkbox,「声明原创」初始为 disabled。

    平台改版把这两样任何一个改了,这条会红——比等真号发布失败再回溯便宜得多。
    """
    probe = _publish_page_probe()
    boxes = probe["modal_structure"]["checkboxes"]
    main_label = [b for b in boxes
                  if "d-checkbox" in b["attrs"].get("class", "")
                  and "d-checkbox-main-label" in b["attrs"].get("class", "")]
    assert main_label, f"没有 .d-checkbox-main-label 容器: {[b['attrs'] for b in boxes]}"
    assert _bnc._ORIGINAL_CONSENT_CANDIDATES[0].endswith(
        ".d-checkbox.d-checkbox-main-label")
    # 勾选态判据的真值来源:模拟器元素初始带 unchecked
    simulators = [b for b in boxes
                  if "d-checkbox-simulator" in b["attrs"].get("class", "")]
    assert simulators, "夹具里应有 .d-checkbox-simulator"
    assert _bnc.consent_ticked_from_simulator_class(
        simulators[0]["attrs"]["class"]) is False, "初始应为未勾选"

    button = probe["modal_structure"]["buttons"][0]
    assert button["text"] == _bnc._ORIGINAL_CONFIRM_TEXT
    # 初始禁用:原生 disabled 属性 + class 里也带 disabled(两处都读才不漏,同 step7 口径)
    assert "disabled" in button["attrs"]
    assert "disabled" in button["attrs"]["class"]


def test_consent_tick_read_from_simulator_class_not_hidden_input():
    """勾选态判据是**模拟器元素的 class 含不含 unchecked**,不是隐藏 input.checked。

    探针实测那个 input 的 rect 是 0×0(拿不到也点不着),与「原创声明」大开关同套路 ——
    这套组件库把真实状态放在模拟器元素的 class 上。读不到 class 一律算没勾上。
    """
    f = _bnc.consent_ticked_from_simulator_class
    assert f("d-checkbox-simulator --color-bg-white unchecked") is False
    assert f("d-checkbox-simulator --color-bg-white") is True
    assert f(None) is False and f("") is False


def test_declared_requires_modal_gone_AND_switch_on(monkeypatch):
    """成功终态判据 = 弹窗消失 **且** 开关行回读 checked 为真,两个都要。

    只看"弹窗消失"不够:点 X 关掉弹窗同样消失,而探针实测那条路 checked 会被重置成
    False。正是这个差别把"真声明了"和"只是把弹窗关掉了"区分开。
    """
    monkeypatch.setattr(_bnc, "_scroll_row_to_mid_viewport", lambda *a, **k: None)

    class _ConfirmButNotOn(_ConsentModalPage):
        """点了声明原创、弹窗也关了,但开关没留在开态(平台侧没接受)。"""

    page = _ConfirmButNotOn()
    human = _ConsentHuman(page)

    orig_click = human.click

    def click(target, reason="", **k):
        orig_click(target, reason=reason, **k)
        if "声明原创" in reason:
            page.optimistic_checked = False  # 弹窗关了但开关没开

    human.click = click
    out = _bnc.apply_original_declaration(page, human, handle_consent_modal=True)

    assert page.modal_open is False, "弹窗确实关了"
    assert out["status"] == "error", f"开关没开就不许报 done: {out}"
    assert "switch_not_on_after_confirm" in out["reason"]
