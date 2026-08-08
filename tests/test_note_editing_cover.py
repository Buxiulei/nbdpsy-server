"""已发布笔记改封面(更新页「设置封面」**弹窗**链)的浏览器层 + REST 契约测试。

真号取证(账号2 视频笔记 6a1e76f9…,2026-08-08,data/scene_captures/edit_cover/)锁定的
操作面与本文件断言一一对应:

- 更新页封面区 ``.publish-page-content-cover``,缩略图 ``.cover .default.column``,
  悬停才显的浮层 ``.operator``(class 带 ``noCover`` = 当前用的是平台默认首帧);
- 入口是浮层里的 ``.operator .text``「修改封面」,**按实测矩形中心坐标点**(它与
  「遇到问题?」矩形重叠,element.click() 会撞 tooltip 覆盖区);
- 点开后是 ``.d-modal``(标题「设置封面」),默认停在「截取封面」tab —— **图片 file
  input 懒挂载在「上传封面」tab 里**,不切过去就永远报"没有 file input"(phase-1 就
  卡在这);切过去后 ``input.upload-input[type=file]`` 才存在,``.btn-confirm``「确定」
  选图前带 disabled。

覆盖:

- 幂等:已有自定义封面(``.operator`` 没有 noCover)→ ``skipped`` **零点击**;这次只请求
  改封面时**一次发布都不点**;
- 红线:全程不点「智能推荐封面」/「PK封面」/「优质封面示例」/「遇到问题?」,
  也不动标题/正文/话题/组件/可见性;
- 上传避原生框:走 ``set_input_files``,**绝不点**「上传图片」按钮;
- 弹窗必收尾:每条失败路径都要把弹窗关掉(2026-08-02 引用弹窗盖住发布按钮同型事故);
- 回读判据是**封面区真变了**(背景图指纹变化 / noCover 消失),不是"点了就成功";
- REST:``cover`` 只对视频笔记有效(图文 → 422)、扩展名 + 存在性校验、单独给它也构成
  一次有效请求、payload 带得出去、manifest 字段级不漂移。
"""

import json
from datetime import datetime
from pathlib import Path

import pytest
from sqlalchemy import func, select

import app.browser.note_components as bnc
import app.core.db as db_module
from app.models.browser_job import BrowserJob
from app.models.published_note import PublishedNote
from app.services import note_components
from tests.rest_helpers import ADMIN_KEY, bearer, rest_client, seed_account
from tests.test_note_components import (  # noqa: F401 — wired 是跨模块复用的 fixture
    Editor,
    _wire,
    wired,
)

_COOKIES = [{"name": "a1", "value": "x", "domain": ".xiaohongshu.com"}]
_NOTE = "6a1e76f90000000008025984"
# 红线控件:出现在任何一次点击 reason / 文案里就是事故
_FORBIDDEN_TEXTS = ("智能推荐封面", "PK封面", "优质封面示例", "遇到问题")


# ---------------- 测试替身:只建模封面那一块的假更新页 ----------------


class _El:
    """假元素:被测代码真用到的能力(文案 / class / 矩形 / 子查询 / 点击 / 灌文件)。"""

    def __init__(self, text="", *, cls=None, disabled=False, on_click=None,
                 children=None, on_files=None, box=None):
        self._text = text
        self._cls = cls
        self._disabled = disabled
        self.on_click = on_click
        self.on_files = on_files
        self._children = children or {}
        self._box = box or {"x": 400.0, "y": 420.0, "width": 56.0, "height": 22.0}

    def inner_text(self):
        return self._text

    def is_visible(self):
        return True

    def get_attribute(self, name):
        if name == "class":
            return self._cls
        if name == "disabled":
            return "" if self._disabled else None
        return None

    def bounding_box(self):
        return dict(self._box)

    def evaluate(self, _js):
        return "<div class='publish-page-content-cover'>假封面区</div>"

    def query_selector(self, sel):
        hits = self._children.get(sel) or []
        return hits[0] if hits else None

    def query_selector_all(self, sel):
        return list(self._children.get(sel) or [])

    def set_input_files(self, paths):
        if self.on_files:
            self.on_files(paths)


class _Human:
    """假拟人层:记录每一次点击/悬停(供"不该点的一次都没点"断言),并触发副作用。"""

    def __init__(self, _page=None):
        self.clicks = []
        self.hovers = []

    def wait(self, *_a, **_kw):
        pass

    def hover(self, target, *, reason="", **_kw):
        self.hovers.append(reason)

    def click(self, target, *, reason="", **_kw):
        text = target.inner_text() if hasattr(target, "inner_text") else str(target)
        self.clicks.append((reason, text))
        if hasattr(target, "on_click") and target.on_click:
            target.on_click()


class CoverPage:
    """假更新页(只管封面):缩略图 → 浮层 → 设置封面弹窗 → 上传 tab → 确定。

    每个可配的坏行为都对应一条真实风险,不是凑数:``operator_absent``=浮层没唤出;
    ``upload_tab_absent``=平台改版删了 tab;``file_input_absent``=切了 tab 仍没挂载;
    ``confirm_never_enables``=图片没被平台接受(确定一直禁用);``modal_never_closes``=
    点了确定弹窗不走(它会盖住发布按钮);``preview_unchanged``=弹窗关了但封面没换。
    """

    def __init__(self, *, no_cover=True, fingerprint="cdn/frame-0",
                 section_absent=False, state_unreadable=False, thumb_absent=False,
                 operator_absent=False, modal_never_opens=False,
                 upload_tab_absent=False, file_input_absent=False,
                 confirm_never_enables=False, modal_never_closes=False,
                 preview_unchanged=False):
        self.no_cover = no_cover
        self.fingerprint = fingerprint
        self.section_absent = section_absent
        self.state_unreadable = state_unreadable
        self.thumb_absent = thumb_absent
        self.operator_absent = operator_absent
        self.modal_never_opens = modal_never_opens
        self.upload_tab_absent = upload_tab_absent
        self.file_input_absent = file_input_absent
        self.confirm_never_enables = confirm_never_enables
        self.modal_never_closes = modal_never_closes
        self.preview_unchanged = preview_unchanged

        self.modal_open = False
        self.upload_tab_active = False
        self.chosen_files = []
        self.cancelled = 0

    # ---- 点击副作用 ----

    def _open_modal(self):
        if not self.modal_never_opens:
            self.modal_open = True
            self.upload_tab_active = False

    def _switch_upload_tab(self):
        self.upload_tab_active = True

    def _choose_files(self, paths):
        self.chosen_files = list(paths)

    def _confirm(self):
        if self.confirm_never_enables or not self.chosen_files:
            return          # 禁用态的按钮点了也没用
        if self.modal_never_closes:
            return
        self.modal_open = False
        if not self.preview_unchanged:
            self.no_cover = False
            self.fingerprint = "cdn/custom-1"

    def _cancel(self):
        self.cancelled += 1
        self.modal_open = False

    def _boom(self, label):
        raise AssertionError(f"点到了封面红线控件「{label}」——绝对禁止")

    # ---- page 接口 ----

    def wait_for_timeout(self, _ms):
        pass

    def evaluate(self, js, _arg=None):
        if "noCover" in js:
            if self.section_absent or self.state_unreadable:
                return None
            return {"no_cover": self.no_cover, "fingerprint": self.fingerprint}
        return None

    def _modal_el(self):
        confirm = _El(
            "确定", cls="d-button btn-confirm" + ("" if self.chosen_files else " disabled"),
            disabled=not self.chosen_files or self.confirm_never_enables,
            on_click=self._confirm,
        )
        tabs = [_El("截取封面", on_click=lambda: None)]
        if not self.upload_tab_absent:
            tabs.append(_El("上传封面", on_click=self._switch_upload_tab))
        children = {
            bnc._COVER_MODAL_TAB: tabs,
            bnc._COVER_MODAL_CONFIRM: [confirm],
            bnc._COVER_MODAL_CANCEL: [_El("取消", cls="cancelBtn", on_click=self._cancel)],
            bnc._COVER_MODAL_FILE_INPUT: (
                [] if (self.file_input_absent or not self.upload_tab_active)
                else [_El("", cls="upload-input", on_files=self._choose_files)]
            ),
        }
        return _El("设置封面 截取封面 上传封面 取消 确定", children=children)

    def query_selector(self, sel):
        hits = self.query_selector_all(sel)
        return hits[0] if hits else None

    def query_selector_all(self, sel):
        if sel == bnc._COVER_SECTION:
            return [] if self.section_absent else [_El("设置封面 智能推荐封面")]
        if sel in bnc._COVER_THUMB_CANDIDATES:
            if self.section_absent or self.thumb_absent:
                return []
            return [_El("", box={"x": 400.0, "y": 300.0, "width": 112.0, "height": 150.0})]
        if sel == bnc._COVER_OPERATOR_TEXT:
            if self.section_absent or self.operator_absent:
                return []
            return [_El("修改封面", on_click=self._open_modal)]
        if sel == bnc._COVER_MODAL:
            return [self._modal_el()] if self.modal_open else []
        if sel in ("智能推荐封面", "PK封面"):
            return [_El(sel, on_click=lambda s=sel: self._boom(s))]
        return []


@pytest.fixture(autouse=True)
def _fast_cover_windows(monkeypatch):
    """替身的 ``wait_for_timeout`` 是空转,轮询窗口按真实秒走会白烧 CPU;测里压到毫秒级。

    逻辑一个字不变,只缩窗口 —— 三条超时用例(确定不解禁 / 弹窗不关 / 预览不变)本来
    要空转 50 秒。
    """
    for name in (
        "_COVER_OPERATOR_TIMEOUT_S", "_COVER_MODAL_OPEN_TIMEOUT_S",
        "_COVER_INPUT_TIMEOUT_S", "_COVER_CONFIRM_TIMEOUT_S",
        "_COVER_MODAL_CLOSE_TIMEOUT_S", "_COVER_PREVIEW_TIMEOUT_S",
    ):
        monkeypatch.setattr(bnc, name, 0.4)


def _apply(page, **_kw):
    human = _Human()
    outcome = bnc.apply_cover_change(page, human, "/tmp/cover.png")
    return outcome, human


def _assert_no_forbidden_click(human):
    blob = " ".join(f"{r} {t}" for r, t in human.clicks)
    for word in _FORBIDDEN_TEXTS:
        assert word not in blob, f"点到了红线控件「{word}」:{human.clicks}"


# ---------------- 幂等:已有自定义封面 → skipped 零点击 ----------------


def test_existing_custom_cover_is_skipped_with_zero_clicks():
    """``.operator`` 没有 noCover = 已经是自定义封面 → skipped,**一次点击都不发**。

    改封面是覆盖性动作,已达标还去点一遍等于白白覆盖一次(且要多付一次全量提交)。
    """
    page = CoverPage(no_cover=False, fingerprint="cdn/custom-0")
    outcome, human = _apply(page)

    assert outcome["status"] == "skipped", outcome
    assert human.clicks == []
    assert human.hovers == []
    assert page.modal_open is False


def test_cover_state_unreadable_is_fail_loud_not_optimistic():
    """读不出封面区状态 → error 且零点击:判不出现状就不敢覆盖(绝不乐观当"没封面")。"""
    page = CoverPage(state_unreadable=True)
    outcome, human = _apply(page)

    assert outcome["status"] == "error"
    assert "cover_state_unreadable" in outcome["reason"]
    assert human.clicks == []


def test_cover_section_missing_hands_back_forensics():
    """封面区不在 → error,且把当场取证(封面区 HTML)一起交出去,不是只丢一句"没找到"。"""
    page = CoverPage(section_absent=True)
    outcome, human = _apply(page)

    assert outcome["status"] == "error"
    assert "cover_section_not_found" in outcome["reason"]
    assert "cover_section_html" in (outcome.get("observed") or {})
    assert human.clicks == []


# ---------------- 正常路径:悬停 → 按坐标点入口 → 切 tab → 灌文件 → 确定 ----------------


def test_happy_path_switches_upload_tab_and_sets_input_files():
    """全链路:悬停缩略图 → 点「修改封面」→ **切「上传封面」tab** → set_input_files → 确定。"""
    page = CoverPage()
    outcome, human = _apply(page)

    assert outcome["status"] == "done", outcome
    assert page.chosen_files == ["/tmp/cover.png"]
    assert page.upload_tab_active is True, "没切「上传封面」tab —— 图片 input 是懒挂载的"
    assert page.modal_open is False
    assert outcome["fingerprint_before"] == "cdn/frame-0"
    # 入口是按矩形中心坐标点的(避开 tooltip 覆盖区),不是 element.click()
    assert any("修改封面" in reason for reason, _t in human.clicks)
    assert human.hovers, "没悬停缩略图,浮层根本不会出现"
    _assert_no_forbidden_click(human)


def test_never_clicks_the_upload_button_only_feeds_the_input():
    """**绝不点**「上传图片」按钮:真桌面上它会弹原生 GTK 文件框卡死整条流程。"""
    page = CoverPage()
    _outcome, human = _apply(page)

    assert all("上传图片" not in t for _r, t in human.clicks), human.clicks


def test_upload_tab_missing_reports_it_and_closes_modal():
    """平台把「上传封面」tab 拿掉了 → 如实报 + 关弹窗(不能留着盖住发布按钮)。"""
    page = CoverPage(upload_tab_absent=True)
    outcome, human = _apply(page)

    assert outcome["status"] == "error"
    assert "cover_upload_tab_not_found" in outcome["reason"]
    assert page.modal_open is False and page.cancelled == 1
    assert page.chosen_files == []


def test_file_input_missing_after_tab_switch_is_error():
    """切了 tab 仍没挂出 file input → error(而不是退回去点上传按钮)。"""
    page = CoverPage(file_input_absent=True)
    outcome, _human = _apply(page)

    assert outcome["status"] == "error"
    assert "cover_file_input_not_found" in outcome["reason"]
    assert page.modal_open is False


def test_confirm_never_enabled_is_error_and_modal_closed():
    """「确定」一直禁用(平台没接受这张图)→ error;弹窗必须关掉。"""
    page = CoverPage(confirm_never_enables=True)
    outcome, _human = _apply(page)

    assert outcome["status"] == "error"
    assert "cover_confirm_not_enabled" in outcome["reason"]
    assert page.modal_open is False and page.cancelled == 1


def test_modal_not_closing_after_confirm_is_error_and_forced_closed():
    """点了确定弹窗不走 → error,并强行点取消关掉(它会盖住发布按钮)。"""
    page = CoverPage(modal_never_closes=True)
    outcome, _human = _apply(page)

    assert outcome["status"] == "error"
    assert "cover_modal_not_closed" in outcome["reason"]
    assert page.modal_open is False and page.cancelled == 1


def test_preview_unchanged_after_confirm_is_error_not_silent_success():
    """弹窗关了但封面区没变 → error:判据是**封面真变了**,不是"点了就成功"。"""
    page = CoverPage(preview_unchanged=True)
    outcome, _human = _apply(page)

    assert outcome["status"] == "error"
    assert "cover_preview_unchanged" in outcome["reason"]


def test_entry_missing_after_hover_is_error_with_html():
    """悬停后仍没量到「修改封面」矩形 → error + 封面区 HTML(下一次一击定位的凭据)。"""
    page = CoverPage(operator_absent=True)
    outcome, _human = _apply(page)

    assert outcome["status"] == "error"
    assert "cover_entry_not_found" in outcome["reason"]
    assert "cover_section_html" in (outcome.get("observed") or {})


def test_modal_not_opened_is_error_and_nothing_uploaded():
    """点了入口没弹窗 → error,且一个文件都没灌(不猜别的路径)。"""
    page = CoverPage(modal_never_opens=True)
    outcome, _human = _apply(page)

    assert outcome["status"] == "error"
    assert "cover_modal_not_opened" in outcome["reason"]
    assert page.chosen_files == []


# ---------------- 整条编辑链:提交决策 / 回读 / 不误伤 ----------------


def _run(editor, **kw):
    return bnc.set_note_components(editor.page, 1, "n-target", **kw)


def test_cover_only_already_custom_does_not_publish(monkeypatch, wired):
    """只请求改封面而它已经是自定义封面 → 幂等 skipped,**一次发布都不点**。

    批量清理会对上百篇已达标的笔记跑这条路,每篇白提交一次就是上百次真发布。
    """
    editor = Editor(cover_no_cover=False)
    _wire(monkeypatch, editor, wired, publish=False)

    out = _run(editor, cover_path="/tmp/cover.png")

    assert out["applied"]["cover"] is True
    assert out["submitted"] is False
    assert editor.submitted == 0
    assert out["components"]["cover"]["status"] == "skipped"


def test_cover_change_publishes_once_and_verifies_by_readback(monkeypatch, wired):
    """真换封面:走一次提交,回读靠封面区指纹变化确认生效。"""
    editor = Editor(cover_no_cover=True)
    _wire(monkeypatch, editor, wired)

    out = _run(editor, cover_path="/tmp/cover.png")

    assert out["applied"]["cover"] is True, out
    assert out["submitted"] is True
    assert editor.submitted == 1
    assert editor.cover_files == ["/tmp/cover.png"]


def test_cover_change_touches_nothing_else(monkeypatch, wired):
    """验收判据:改封面**绝不动**标题/正文/话题/组件/可见性。"""
    editor = Editor(cover_no_cover=True, collection=None, body="原有正文", title="目标笔记")
    _wire(monkeypatch, editor, wired)

    out = _run(editor, cover_path="/tmp/cover.png")

    assert out["applied"] == {"cover": True}
    assert editor.collection is None
    assert editor.body == "原有正文"
    assert editor.title == "目标笔记"
    assert editor.quote_text == "引用笔记"
    assert all(not a["linked"] for a in editor.activities)
    assert out["permission_preserved"] is True


def test_cover_not_changed_on_readback_is_false_not_true(monkeypatch, wired):
    """提交后回读封面区没变 → applied.cover=False(静默失败必须报出来)。"""
    editor = Editor(cover_no_cover=True, cover_persists_on_readback=False)
    _wire(monkeypatch, editor, wired)

    out = _run(editor, cover_path="/tmp/cover.png")

    assert out["applied"]["cover"] is False
    assert out["status"] == "failed"
    assert "note_components_all_failed" in out["error"]


def test_cover_step_failure_does_not_block_other_components(monkeypatch, wired):
    """封面步失败不阻断其余组件(与破坏性编辑步的"整单弃提交"语义刻意不同)。"""
    editor = Editor(cover_no_cover=True, cover_entry_absent=True)
    _wire(monkeypatch, editor, wired)

    out = _run(editor, cover_path="/tmp/cover.png", collection_id="c1",
               collection_name="咨询师简介")

    assert out["applied"]["cover"] is not True
    assert out["applied"]["collection"] is True
    assert out["status"] == "partially_applied"


def test_aborted_edit_marks_cover_as_not_executed(monkeypatch, wired):
    """破坏性编辑步失败弃提交时,请求过的封面要记「因前序失败未执行」,不能假装设上了。"""
    skipped = bnc._skipped_components(None, None, None, cover_path="/tmp/cover.png")
    assert skipped["cover"]["status"] == "error"
    assert "前序破坏性编辑步失败" in skipped["cover"]["reason"]


# ---------------- REST 契约 ----------------


def _api_role(monkeypatch) -> None:
    monkeypatch.setenv("NBDPSY_ROLE", "api")


async def _seed_note(account_id: int, note_type="video", note_id: str = _NOTE) -> None:
    now = datetime(2026, 8, 1)
    async with db_module.async_session() as s:
        s.add(
            PublishedNote(
                account_id=account_id, note_id=note_id, title="旧标题",
                note_type=note_type, published_at=now, sync_status="linked",
                first_seen_at=now, last_synced_at=now,
            )
        )
        await s.commit()


async def _job_count() -> int:
    async with db_module.async_session() as s:
        return await s.scalar(select(func.count()).select_from(BrowserJob))


async def _payload_of(job_id: str) -> dict:
    async with db_module.async_session() as s:
        return json.loads((await s.get(BrowserJob, job_id)).payload)


def _cover_file(tmp_path: Path, name="cover.png") -> str:
    path = tmp_path / name
    path.write_bytes(b"\x89PNG\r\n\x1a\n")
    return str(path)


async def test_cover_alone_is_a_valid_request_on_video_note(tmp_path, monkeypatch):
    """只给 cover 也构成一次有效请求,payload 带得出去。"""
    _api_role(monkeypatch)
    async with rest_client(tmp_path, monkeypatch) as c:
        acc = await seed_account("改封面号", "uCover1", _COOKIES)
        await _seed_note(acc)
        cover = _cover_file(tmp_path)

        r = await c.post(
            f"/api/accounts/{acc}/note-components",
            json={"note_id": _NOTE, "cover": cover}, headers=bearer(ADMIN_KEY),
        )
        assert r.status_code == 202, r.text
        assert (await _payload_of(r.json()["job_id"]))["cover"] == cover


async def test_cover_on_image_note_is_422(tmp_path, monkeypatch):
    """图文笔记传 cover → 422(图文的封面就是第一张图,与发布端点同口径),且不建 job。"""
    _api_role(monkeypatch)
    async with rest_client(tmp_path, monkeypatch) as c:
        acc = await seed_account("图文号", "uCover2", _COOKIES)
        await _seed_note(acc, note_type="normal")

        r = await c.post(
            f"/api/accounts/{acc}/note-components",
            json={"note_id": _NOTE, "cover": _cover_file(tmp_path)},
            headers=bearer(ADMIN_KEY),
        )
        assert r.status_code == 422, r.text
        assert "第一张图" in r.text
        assert await _job_count() == 0


async def test_cover_on_unknown_note_type_is_422(tmp_path, monkeypatch):
    """台账 note_type 为 NULL(类型未知)→ 422:类型不可确认就不做全量覆盖提交。"""
    _api_role(monkeypatch)
    async with rest_client(tmp_path, monkeypatch) as c:
        acc = await seed_account("未知类型号", "uCover3", _COOKIES)
        await _seed_note(acc, note_type=None)

        r = await c.post(
            f"/api/accounts/{acc}/note-components",
            json={"note_id": _NOTE, "cover": _cover_file(tmp_path)},
            headers=bearer(ADMIN_KEY),
        )
        assert r.status_code == 422, r.text
        assert await _job_count() == 0


async def test_cover_unknown_note_is_404(tmp_path, monkeypatch):
    """台账里查无此 note_id → 404(认不出类型就不改封面)。"""
    _api_role(monkeypatch)
    async with rest_client(tmp_path, monkeypatch) as c:
        acc = await seed_account("无台账号", "uCover4", _COOKIES)

        r = await c.post(
            f"/api/accounts/{acc}/note-components",
            json={"note_id": _NOTE, "cover": _cover_file(tmp_path)},
            headers=bearer(ADMIN_KEY),
        )
        assert r.status_code == 404, r.text
        assert await _job_count() == 0


async def test_cover_bad_extension_is_422(tmp_path, monkeypatch):
    """扩展名不在白名单 → 422,一步都不做。"""
    _api_role(monkeypatch)
    async with rest_client(tmp_path, monkeypatch) as c:
        acc = await seed_account("坏扩展号", "uCover5", _COOKIES)
        await _seed_note(acc)

        r = await c.post(
            f"/api/accounts/{acc}/note-components",
            json={"note_id": _NOTE, "cover": _cover_file(tmp_path, "cover.gif")},
            headers=bearer(ADMIN_KEY),
        )
        assert r.status_code == 422, r.text
        assert await _job_count() == 0


async def test_cover_missing_file_is_422(tmp_path, monkeypatch):
    """文件不存在 → 422(别让它排队两分钟后才在浏览器层撞墙)。"""
    _api_role(monkeypatch)
    async with rest_client(tmp_path, monkeypatch) as c:
        acc = await seed_account("缺文件号", "uCover6", _COOKIES)
        await _seed_note(acc)

        r = await c.post(
            f"/api/accounts/{acc}/note-components",
            json={"note_id": _NOTE, "cover": str(tmp_path / "nope.png")},
            headers=bearer(ADMIN_KEY),
        )
        assert r.status_code == 422, r.text
        assert await _job_count() == 0


async def test_service_passes_cover_to_browser_layer(monkeypatch):
    """服务层把 payload 里的 cover 同名直传浏览器层(链路不能断在中间)。"""
    seen = {}

    async def fake_load(_aid):
        return _COOKIES

    def fake_apply(_aid, _cookies, _note, _components, edits=None, collection_name=None,
                   remove_collection_name=None, set_original_declaration=False,
                   cover_path=None):
        seen["cover_path"] = cover_path
        return {"status": "done", "applied": {"cover": True}}

    monkeypatch.setattr(note_components, "load_account_cookies", fake_load)
    monkeypatch.setattr(note_components, "_apply_sync", fake_apply)

    out = await note_components.execute(1, {"note_id": "n1", "cover": "/tmp/c.png"})

    assert "error" not in out, out
    assert seen["cover_path"] == "/tmp/c.png"


def test_manifest_states_the_cover_contract():
    """manifest 必须写清改封面的四条:仅视频有效 / 幂等 skipped / 回读判据 / 未取证提醒。"""
    from app.http.note_components_rest import MANIFEST_ENTRIES

    entry = next(
        e for e in MANIFEST_ENTRIES
        if e["method"] == "POST" and e["path"].endswith("/note-components")
    )
    text = entry["params"]["cover"] + entry["notes"] + entry["errors"]
    assert "视频" in text and "图文" in text
    assert "skipped" in text and "零点击" in text
    assert "422" in entry["errors"] and "cover" in entry["errors"]


def test_polling_manifest_lists_cover_in_applied():
    """轮询端点的 returns 要列出 applied.cover,否则调用方不知道去读它。"""
    from app.http.note_components_rest import MANIFEST_ENTRIES

    entry = next(
        e for e in MANIFEST_ENTRIES
        if e["method"] == "GET" and "{job_id}" in e["path"]
    )
    assert "cover" in entry["returns"]


def test_changelog_records_the_cover_feature():
    """guide 变更记录里必须有这条(调用方是照它决定要不要改代码的)。"""
    from app.http.guide import CHANGELOG_ENTRIES

    hits = [e for e in CHANGELOG_ENTRIES if "封面" in e["title"]]
    assert hits, "CHANGELOG_ENTRIES 里没有改封面这条"
    assert any(
        "/api/accounts/{account_id}/note-components" in e["endpoints"] for e in hits
    )
