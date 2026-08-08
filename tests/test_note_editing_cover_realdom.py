"""改封面弹窗选择器的**真 DOM** 回归(Playwright headless + set_content)。

为什么要这一条:``test_note_editing_cover.py`` 里的 ``_El`` 是 dict 查表替身
(``query_selector`` 按**选择器常量本身**取子节点),选择器写错也照样命中 —— 2026-08-08
的 P0(四个弹窗常量误带 ``.d-modal `` 前缀)带着 208 条绿灯进来,正因为那套替身对
选择器语义**没有牙**。本文件用真 chromium 灌真 DOM,直接对 ``ElementHandle`` 求值:

- ``modal.query_selector(".d-modal X")`` 在**根节点自己就是 .d-modal** 的元素上,
  后代组合子无从命中 → 返回 None(实测,见 ``test_prefixed_constants_would_break_*``);
- 相对形态 ``.d-tabs-header`` / ``input.upload-input[type=file]`` / ``.btn-confirm`` /
  ``.cancelBtn`` 才在弹窗子树内命中。

因此:**只要有人把 note_components 里那四个常量改回 ``.d-modal `` 前缀,本文件必红。**

DOM 取自真号取证(账号2 视频笔记 6a1e76f9…,2026-08-08,
``data/scene_captures/edit_cover/edit_cover_probe*.json``):弹窗两个 tab
「截取封面(active)」「上传封面」、``input.upload-input`` accept=image/*、
``.btn-confirm``「确定」选图前带 disable、``.cancelBtn``「取消」。
"""

import pytest

import app.browser.note_components as bnc

# ---- 真 DOM 片段:类名逐字取自 probe 的 dialog 结构与 file input hint ----

# 弹窗结构(phase-1 dialog_full_structure + phase-2 image_file_input_selector_hint):
# 两个 tab 都是 .d-tabs-header;file input 是 .upload-input;确定 .btn-confirm(disabled);
# 取消 .cancelBtn。刻意在 .d-modal 之外再放一个别的 .d-modal(合集移除确认)当诱饵,
# 验证 _find_cover_modal 是**按文案认领**而不是逮着第一个 .d-modal 就用。
_COVER_MODAL_HTML = """
<div class="d-modal creator-modal-style">
  <div class="d-modal-content">这是合集移除确认弹窗,和封面无关</div>
</div>
<div class="d-modal">
  <div class="d-modal-content">
    <div class="cover-plugin-title"><span>设置封面</span></div>
    <div class="d-tabs d-tabs-top">
      <div class="d-tabs-header d-clickable active">
        <div class="d-tabs-header-label">截取封面</div>
      </div>
      <div class="d-tabs-header d-clickable">
        <div class="d-tabs-header-label">上传封面</div>
      </div>
    </div>
    <input class="upload-input" type="file" accept="image/png, image/jpeg, image/*">
    <button type="button" class="custom-button bg-red btn-upload">上传图片</button>
    <button class="cancelBtn">取消</button>
    <button type="button" class="custom-button bg-red disabled btn-confirm" disabled>确定</button>
  </div>
</div>
"""


class _RecordingHuman:
    """记录点击并在真 ElementHandle 上执行 click,供「关弹窗点的是取消」断言。"""

    def __init__(self):
        self.clicked = []

    def click(self, target, *, reason="", **_kw):
        self.clicked.append(target.get_attribute("class") or "")
        target.click()


@pytest.fixture(scope="module")
def _browser():
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            yield browser
        finally:
            browser.close()


@pytest.fixture
def cover_page(_browser):
    page = _browser.new_page()
    page.set_content(f"<body>{_COVER_MODAL_HTML}</body>")
    try:
        yield page
    finally:
        page.close()


def test_full_chain_resolves_with_real_constants(cover_page):
    """开弹窗→切上传封面tab→拿到 file input→确定解禁→关弹窗,全链用真常量真 DOM 跑通。

    这就是有牙的那条:任一常量退回 ``.d-modal `` 前缀,下面每个元素级查询都会落空。
    """
    # ① 认领弹窗:两个 .d-modal 里只认带「设置封面」文案的那个
    modal = bnc._find_cover_modal(cover_page)
    assert modal is not None
    assert "设置封面" in modal.inner_text()

    # ② 切「上传封面」tab:_find_upload_tab 走 modal.query_selector_all(_COVER_MODAL_TAB)
    tab = bnc._find_upload_tab(modal)
    assert tab is not None
    assert bnc._norm(tab.inner_text()) == "上传封面"

    # ③ 拿到图片 file input:modal.query_selector(_COVER_MODAL_FILE_INPUT)
    file_input = bnc._cover_file_input(cover_page)
    assert file_input is not None
    assert "image/" in (file_input.get_attribute("accept") or "")

    # ④ 「确定」选图前是禁用态 → 拿不到;去掉 disabled 后才解禁
    assert bnc._enabled_cover_confirm(cover_page) is None
    cover_page.eval_on_selector(
        ".btn-confirm",
        "el => { el.removeAttribute('disabled'); el.classList.remove('disabled'); }",
    )
    confirm = bnc._enabled_cover_confirm(cover_page)
    assert confirm is not None
    assert "btn-confirm" in (confirm.get_attribute("class") or "")

    # ⑤ 关弹窗:必须点到 .cancelBtn(不能是禁用态的确定,2026-08-02 同型事故)
    human = _RecordingHuman()
    bnc._close_cover_modal(cover_page, human)
    assert len(human.clicked) == 1
    assert "cancelBtn" in human.clicked[0]
    assert "btn-confirm" not in human.clicked[0]


@pytest.mark.parametrize(
    "const_name, prefixed",
    [
        ("_COVER_MODAL_TAB", ".d-modal .d-tabs-header"),
        ("_COVER_MODAL_FILE_INPUT", ".d-modal input.upload-input[type='file']"),
        ("_COVER_MODAL_CONFIRM", ".d-modal .btn-confirm"),
        ("_COVER_MODAL_CANCEL", ".d-modal .cancelBtn"),
    ],
)
def test_prefixed_constants_would_break_scoped_lookup(
    cover_page, monkeypatch, const_name, prefixed
):
    """把任一常量改回 ``.d-modal `` 前缀,元素级查询立刻落空 —— 锁死 P0 的失败机理。

    这条把「前缀 = 命中 0」写成可执行契约:被测 helper 用元素级查询,根节点自己是
    .d-modal,后代组合子选不到自身的兄弟/子孙,四个入口全断。
    """
    monkeypatch.setattr(bnc, const_name, prefixed)

    if const_name == "_COVER_MODAL_TAB":
        modal = bnc._find_cover_modal(cover_page)
        assert bnc._find_upload_tab(modal) is None
    elif const_name == "_COVER_MODAL_FILE_INPUT":
        assert bnc._cover_file_input(cover_page) is None
    elif const_name == "_COVER_MODAL_CONFIRM":
        cover_page.eval_on_selector(
            ".btn-confirm",
            "el => { el.removeAttribute('disabled'); el.classList.remove('disabled'); }",
        )
        assert bnc._enabled_cover_confirm(cover_page) is None
    else:  # _COVER_MODAL_CANCEL
        human = _RecordingHuman()
        bnc._close_cover_modal(cover_page, human)
        assert human.clicked == []  # 找不到取消按钮 → 一次都不点(告警但不误点)
