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
    def fake_apply(page, human):
        seen["called"] = True
        return {"status": "done"}

    monkeypatch.setattr(sc, "apply_original_declaration", fake_apply)

    client = sc.SyncClient(account_id=1, cookies=[])
    result = client.publish_note("标题", "正文", ["/tmp/a.png"])

    assert result["success"] is True
    assert seen.get("called") is True
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
