"""笔记可见性切换单测(不起真浏览器),锁设计第五节验收 1 的六项 + 防误点删除的三道闸。

- **目标档位已达成 → skipped 且一次都不提交**(点「取消」退出,不点「确定」、不回读);
- **回读未变 → error**,绝不"点了就当成功";回读列表里找不到该 note_id 同样 error;
- **标题为空 / 在该号下重复 → note_not_locatable**,绝不猜一张卡去点;
- ``note_visibility`` **不进** ``_IDEMPOTENT_KINDS``(僵死重跑可能把运营刚改回公开的笔记
  再次藏起来);
- ``permission_code`` / ``permission_msg`` 落库与 T2 同步覆盖(读不出时不覆盖已知值);
- 防误点删除三道闸:图标数量/class 断言不过**一个都不点**;弹窗事后校验不过立刻点
  「取消」中止(**Escape 对这条产品线无效**,故断言点的是取消按钮不是按键)。

patch 纪律:打在被测模块的命名空间(顶层 import 的依赖),不是源模块。
"""

from datetime import datetime

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import app.core.db as db_module
from app.browser import creator_note_list as cnl
from app.browser import note_visibility as bnv
from app.models.published_note import PublishedNote
from app.services import browser_jobs_repo as repo
from app.services import note_ledger as ledger
from app.services import note_visibility as svc


# ---------------- 测试替身 ----------------


class _FakeElement:
    """假元素:只提供被测代码真用到的能力(读文本 / 读 class / 取矩形 / 子查询)。"""

    def __init__(self, text="", cls="", box=None, children=None, visible=True):
        self._text = text
        self._cls = cls
        self._box = box or {"x": 100.0, "y": 200.0, "width": 240.0, "height": 320.0}
        self._children = children or {}
        self._visible = visible

    def inner_text(self):
        return self._text

    def get_attribute(self, name):
        return self._cls if name == "class" else None

    def bounding_box(self):
        return self._box

    def is_visible(self):
        return self._visible

    def query_selector_all(self, sel):
        return self._children.get(sel, [])


class _FakeLocator:
    def __init__(self, ok=True):
        self._ok = ok

    @property
    def first(self):
        return self

    def wait_for(self, **_kw):
        if not self._ok:
            raise RuntimeError("等待超时")


class _FakePage:
    """假 page:selectors 字典驱动 query_selector(_all),evaluate 走脚本队列。

    ``dialog_states`` 是 ``_DIALOG_STATE_JS`` 的返回值序列(最后一项会被重复返回)。
    """

    def __init__(self, selectors=None, dialog_states=None, locator_ok=True):
        self.selectors = dict(selectors or {})
        self._dialog_states = list(dialog_states or [])
        self._locator_ok = locator_ok
        self.evaluates = 0

    def locator(self, _sel):
        return _FakeLocator(self._locator_ok)

    def query_selector_all(self, sel):
        return list(self.selectors.get(sel, []))

    def query_selector(self, sel):
        hits = self.selectors.get(sel) or []
        return hits[0] if hits else None

    def evaluate(self, _js, _arg=None):
        self.evaluates += 1
        if not self._dialog_states:
            return {}
        if len(self._dialog_states) > 1:
            return self._dialog_states.pop(0)
        return self._dialog_states[0]


class _FakeHuman:
    """假拟人层:记录每次点击的 reason,断言"该点的点了 / 不该点的一次没点"。"""

    def __init__(self, _page=None):
        self.clicks = []
        self.hovers = []

    def wait(self, *_a, **_kw):
        pass

    def scroll(self, *_a, **_kw):
        pass

    def scroll_to_element(self, _el):
        pass

    def hover(self, target, *, reason="", **_kw):
        self.hovers.append(reason)

    def click(self, target, *, reason="", **_kw):
        self.clicks.append((target, reason))

    @property
    def click_texts(self):
        """被点元素的文案(元素是坐标元组时给空串),用于断言点了哪个按钮。"""
        out = []
        for target, _reason in self.clicks:
            out.append(target.inner_text() if hasattr(target, "inner_text") else "")
        return out


# 悬停后的合法图标结构(设计 2.3 真号实测):①权限设置 ②置顶 ③编辑 ④删除
_GOOD_BTN_CLASSES = [
    "note-card__action-btn",
    "note-card__action-btn note-card__action-btn--disabled",
    "note-card__action-btn",
    "note-card__action-btn note-card__action-btn--del",
]

_PERM_MODAL_OK = {
    "permVisible": True,
    "dialogCount": 1,
    "mentionsDelete": False,
    "texts": ["谁可以看 公开可见 取消 确定"],
}


def _card(title, btn_classes=None, extra_lines=("1.2万",)):
    """一张笔记卡:innerText 首行是标题,子查询给出悬停后的操作图标。"""
    classes = _GOOD_BTN_CLASSES if btn_classes is None else btn_classes
    btns = [_FakeElement(cls=cls) for cls in classes]
    text = "\n".join([title, *extra_lines])
    return _FakeElement(text=text, children={bnv._ACTION_BTN: btns})


def _page_with(card_titles, dialog_states, options=("公开可见", "仅自己可见"),
               current="公开可见", cards=None, buttons=("取消", "确定")):
    """组装一个走到底都不会 KeyError 的假页面。"""
    card_els = cards if cards is not None else [_card(t) for t in card_titles]
    return _FakePage(
        selectors={
            bnv._NOTE_CARD: card_els,
            bnv._PERM_SELECT: [_FakeElement(text=current)],
            bnv._OPTION: [_FakeElement(text=o) for o in options],
            ".d-modal .d-button": [_FakeElement(text=t) for t in buttons],
        },
        dialog_states=dialog_states,
    )


@pytest.fixture(autouse=True)
def _wire_browser_layer(monkeypatch):
    """把浏览器层的外部依赖换成假的:导航 no-op、拟人层可观测、轮询窗口压到一次。"""
    monkeypatch.setattr(bnv, "_goto_creator", lambda *_a, **_kw: None)
    monkeypatch.setattr(bnv, "SyncHumanActions", _FakeHuman)
    # 轮询节奏是 0.5s / 0.4s 一跳,窗口设成刚够跑一跳:逻辑不变,只缩时间
    monkeypatch.setattr(bnv, "_MODAL_TIMEOUT_S", 0.6)
    monkeypatch.setattr(bnv, "_OPTIONS_TIMEOUT_S", 0.5)


@pytest.fixture
def no_readback(monkeypatch):
    """默认让回读抓取炸掉:凡是"不该走到回读"的用例,一旦走到就立刻暴露。"""

    def boom(*_a, **_kw):
        raise AssertionError("本用例不应走到回读抓取")

    monkeypatch.setattr(bnv, "fetch_posted_notes", boom)


def _readback(monkeypatch, notes):
    """把回读抓取换成返回固定列表。"""
    monkeypatch.setattr(bnv, "fetch_posted_notes", lambda *_a, **_kw: list(notes))


def _raw(note_id, code, msg=""):
    """posted 接口的一条原始项(只含本模块用到的字段)。"""
    return {"id": note_id, "permission_code": code, "permission_msg": msg}


# ---------------- 定位:命中必须恰好 1 张,否则绝不猜 ----------------


def test_empty_title_is_not_locatable(no_readback):
    """标题为空 → note_not_locatable,且一次都没点/悬停(3 篇无标题私密笔记就属这类)。"""
    page = _page_with(["某篇笔记"], [_PERM_MODAL_OK])

    with pytest.raises(bnv.NoteVisibilityError) as exc:
        bnv.set_note_visibility(page, 1, "n1", "   ", 1)

    assert exc.value.reason.startswith("note_not_locatable")


def test_duplicate_title_is_not_locatable(no_readback):
    """同一号下标题重复 → 认不准是哪篇,拒绝操作(绝不取第一张)。"""
    page = _page_with(["重复的标题", "重复的标题", "别的笔记"], [_PERM_MODAL_OK])

    with pytest.raises(bnv.NoteVisibilityError) as exc:
        bnv.set_note_visibility(page, 1, "n1", "重复的标题", 1)

    assert "命中 2 张" in exc.value.reason


def test_missing_title_is_not_locatable(no_readback):
    """列表里没有这个标题 → note_not_locatable(不退而求其次)。"""
    page = _page_with(["另一篇"], [_PERM_MODAL_OK])

    with pytest.raises(bnv.NoteVisibilityError) as exc:
        bnv.set_note_visibility(page, 1, "n1", "找不到的标题", 1)

    assert "命中 0 张" in exc.value.reason


def test_title_match_is_exact_line_no_truncation_guess():
    """整行精确相等才算命中:截断的省略号标题不认(认错卡的代价是藏错笔记)。"""
    card = _card("边界感是练出来的")
    assert bnv._title_hits(card, "边界感是练出来的")
    assert not bnv._title_hits(card, "边界感是练出来")
    assert not bnv._title_hits(_card("边界感是练出来的..."), "边界感是练出来的")


# ---------------- 防误点删除闸一:图标结构断言 ----------------


@pytest.mark.parametrize(
    "classes, mark",
    [
        (_GOOD_BTN_CLASSES[:3], "有 3 个"),
        # ①带上修饰类 = 不是那个"class 完全裸"的权限设置按钮
        (["note-card__action-btn note-card__action-btn--x", *_GOOD_BTN_CLASSES[1:]],
         "permission_btn_mismatch"),
        # ④不是删除 = 图标顺序与实测不符,靠 DOM 顺序区分①③的前提已不成立
        ([*_GOOD_BTN_CLASSES[:3], "note-card__action-btn"], "delete_btn_mismatch"),
    ],
)
def test_bad_icon_structure_clicks_nothing(monkeypatch, no_readback, classes, mark):
    """图标数量/class 与实测不符 → 抛错且**一个都不点**(只允许悬停)。"""
    humans = []
    monkeypatch.setattr(
        bnv, "SyncHumanActions", lambda page: humans.append(_FakeHuman()) or humans[-1]
    )
    page = _page_with([], [_PERM_MODAL_OK], cards=[_card("目标笔记", btn_classes=classes)])

    with pytest.raises(bnv.NoteVisibilityError) as exc:
        bnv.set_note_visibility(page, 1, "n1", "目标笔记", 1)

    assert mark in exc.value.reason
    assert humans[0].clicks == []


# ---------------- 防误点删除闸二:弹窗事后校验 ----------------


@pytest.mark.parametrize(
    "state",
    [
        # 点出来的是删除确认框(①③class 相同,顺序一变就会点到别的)
        {"permVisible": False, "dialogCount": 1, "mentionsDelete": True,
         "texts": ["确定删除这篇笔记吗?"]},
        # 弹窗在,但不是权限弹窗(如编辑框)
        {"permVisible": False, "dialogCount": 1, "mentionsDelete": False,
         "texts": ["编辑笔记"]},
        # 权限弹窗在,但同屏还有个提到「删除」的弹窗 —— 状态可疑,一律中止
        {"permVisible": True, "dialogCount": 2, "mentionsDelete": True,
         "texts": ["谁可以看", "确定删除?"]},
    ],
)
def test_wrong_modal_cancels_and_aborts(monkeypatch, no_readback, state):
    """弹窗校验不过 → 点弹窗内「取消」中止,绝不点「确定」(Escape 关不掉这条产品线的弹窗)。"""
    humans = []
    monkeypatch.setattr(
        bnv, "SyncHumanActions", lambda page: humans.append(_FakeHuman()) or humans[-1]
    )
    page = _page_with(["目标笔记"], [state])

    with pytest.raises(bnv.NoteVisibilityError) as exc:
        bnv.set_note_visibility(page, 1, "n1", "目标笔记", 1)

    assert exc.value.reason.startswith("wrong_modal")
    texts = humans[0].click_texts
    assert "取消" in texts and "确定" not in texts


def test_no_dialog_at_all_aborts(monkeypatch, no_readback):
    """点完权限设置什么弹窗都没出现 → 抛错,不继续往下点。"""
    humans = []
    monkeypatch.setattr(
        bnv, "SyncHumanActions", lambda page: humans.append(_FakeHuman()) or humans[-1]
    )
    page = _page_with(
        ["目标笔记"],
        [{"permVisible": False, "dialogCount": 0, "mentionsDelete": False, "texts": []}],
    )

    with pytest.raises(bnv.NoteVisibilityError) as exc:
        bnv.set_note_visibility(page, 1, "n1", "目标笔记", 1)

    assert exc.value.reason.startswith("permission_modal_not_found")
    assert "确定" not in humans[0].click_texts


# ---------------- 已是目标档位:skipped 不提交 ----------------


def test_already_target_skips_without_submitting(monkeypatch, no_readback):
    """当前档位就是目标档 → 点「取消」返回 skipped,不展开下拉、不点确定、不回读。"""
    humans = []
    monkeypatch.setattr(
        bnv, "SyncHumanActions", lambda page: humans.append(_FakeHuman()) or humans[-1]
    )
    page = _page_with(["目标笔记"], [_PERM_MODAL_OK], current="仅自己可见")

    result = bnv.set_note_visibility(page, 1, "n1", "目标笔记", 1)

    assert result == {"status": "skipped", "permission_code": 1}
    texts = humans[0].click_texts
    assert "取消" in texts and "确定" not in texts and "仅自己可见" not in texts


# ---------------- 提交 + 回读校验 ----------------


def test_done_only_after_readback_confirms(monkeypatch):
    """选档 → 确定 → 回读到 permission_code=1 才算 done(并带回平台原文案)。"""
    humans = []
    monkeypatch.setattr(
        bnv, "SyncHumanActions", lambda page: humans.append(_FakeHuman()) or humans[-1]
    )
    _readback(monkeypatch, [_raw("n1", 1, "仅自己可见"), _raw("n2", 0, "")])
    page = _page_with(["目标笔记"], [_PERM_MODAL_OK], current="公开可见")

    result = bnv.set_note_visibility(page, 1, "n1", "目标笔记", 1)

    assert result == {
        "status": "done", "permission_code": 1, "permission_msg": "仅自己可见",
    }
    texts = humans[0].click_texts
    assert "仅自己可见" in texts and texts[-1] == "确定"


def test_readback_unchanged_is_error(monkeypatch):
    """点完确定但回读 permission_code 没变 → 抛错,**绝不"点了就当成功"**。"""
    _readback(monkeypatch, [_raw("n1", 0, "")])
    page = _page_with(["目标笔记"], [_PERM_MODAL_OK], current="公开可见")

    with pytest.raises(bnv.NoteVisibilityError) as exc:
        bnv.set_note_visibility(page, 1, "n1", "目标笔记", 1)

    assert exc.value.reason.startswith("verify_unchanged")


def test_readback_missing_note_is_error(monkeypatch):
    """回读列表里没有这个 note_id → 无法确认生效,判失败(不乐观放行)。"""
    _readback(monkeypatch, [_raw("n2", 1, "仅自己可见")])
    page = _page_with(["目标笔记"], [_PERM_MODAL_OK], current="公开可见")

    with pytest.raises(bnv.NoteVisibilityError) as exc:
        bnv.set_note_visibility(page, 1, "n1", "目标笔记", 1)

    assert exc.value.reason.startswith("verify_note_missing")


def test_option_not_found_cancels(monkeypatch, no_readback):
    """下拉里没有目标档位文案 → 点取消中止,不瞎点别的档位。"""
    humans = []
    monkeypatch.setattr(
        bnv, "SyncHumanActions", lambda page: humans.append(_FakeHuman()) or humans[-1]
    )
    page = _page_with(
        ["目标笔记"], [_PERM_MODAL_OK], options=("公开可见", "仅互关好友可见"),
        current="公开可见",
    )

    with pytest.raises(bnv.NoteVisibilityError) as exc:
        bnv.set_note_visibility(page, 1, "n1", "目标笔记", 1)

    assert exc.value.reason.startswith("option_not_found")
    assert "取消" in humans[0].click_texts


def test_unsupported_privacy_never_touches_page(no_readback):
    """本期只做 0/1 两档:其余档位在碰页面之前就拒(其语义与 user_ids 格式完全未验证)。"""
    page = _page_with(["目标笔记"], [_PERM_MODAL_OK])

    for target in (2, 3, 4):
        with pytest.raises(bnv.NoteVisibilityError) as exc:
            bnv.set_note_visibility(page, 1, "n1", "目标笔记", target)
        assert exc.value.reason.startswith("unsupported_privacy")
    assert page.evaluates == 0


# ---------------- 服务层契约 ----------------


@pytest.mark.parametrize(
    "payload, mark",
    [
        ({"title": "t", "target_privacy": 1}, "缺 note_id"),
        ({"note_id": "n1", "target_privacy": 1}, "note_not_locatable"),
        ({"note_id": "n1", "title": "t", "target_privacy": 2}, "unsupported_privacy"),
        ({"note_id": "n1", "title": "t", "target_privacy": "1"}, "unsupported_privacy"),
        ({"note_id": "n1", "title": "t"}, "unsupported_privacy"),
    ],
)
async def test_execute_rejects_bad_payload(monkeypatch, payload, mark):
    """入参不合法一律收敛成 {"error"},**不起浏览器**(load_account_cookies 一被调就失败)。"""

    async def boom(_account_id):
        raise AssertionError("入参不合法时不应取 cookie / 起浏览器")

    monkeypatch.setattr(svc, "load_account_cookies", boom)

    result = await svc.execute(1, payload)

    assert mark in result["error"]


async def test_execute_returns_error_without_cookies(monkeypatch):
    """没 cookie 直接收敛成 {"error"},不起浏览器、不抛出。"""

    async def fake_load(_account_id):
        return []

    monkeypatch.setattr(svc, "load_account_cookies", fake_load)

    result = await svc.execute(
        1, {"note_id": "n1", "title": "t", "target_privacy": 1}
    )
    assert "error" in result


async def test_execute_wraps_browser_error(monkeypatch):
    """浏览器层抛 NoteVisibilityError → {"error": reason},不抛出(台账才不会悬挂)。"""

    async def fake_load(_account_id):
        return [{"name": "a", "value": "b"}]

    def boom(*_a, **_kw):
        raise bnv.NoteVisibilityError("verify_unchanged: 切换未生效")

    monkeypatch.setattr(svc, "load_account_cookies", fake_load)
    monkeypatch.setattr(svc, "_set_sync", boom)

    result = await svc.execute(
        1, {"note_id": "n1", "title": "t", "target_privacy": 1}
    )
    assert result == {"error": "verify_unchanged: 切换未生效"}


# ---------------- 切换留痕落库 ----------------


@pytest_asyncio.fixture
async def wired_db(tmp_path, monkeypatch):
    """临时文件库 + monkeypatch 全局 engine/async_session(留痕走 get_session)。"""
    from app.core.db import Base

    import app.models  # noqa: F401

    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path}/visibility.db", future=True
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr(db_module, "engine", engine)
    monkeypatch.setattr(db_module, "async_session", factory)
    try:
        yield factory
    finally:
        await engine.dispose()


async def _seed_note(factory, note_id="n1", permission_code=0):
    async with factory() as session:
        session.add(
            PublishedNote(
                account_id=1, note_id=note_id, title="目标笔记",
                published_at=datetime(2026, 7, 1), sync_status="linked",
                first_seen_at=datetime(2026, 7, 1), last_synced_at=datetime(2026, 7, 1),
                permission_code=permission_code,
            )
        )
        await session.commit()


def _wire_success(monkeypatch, result):
    async def fake_load(_account_id):
        return [{"name": "a", "value": "b"}]

    monkeypatch.setattr(svc, "load_account_cookies", fake_load)
    monkeypatch.setattr(svc, "_set_sync", lambda *_a, **_kw: dict(result))


async def test_done_writes_visibility_trace(wired_db, monkeypatch):
    """切换成功 → 平台原值 + 留痕(改动时刻 / 发起人)一起落到台账行。"""
    await _seed_note(wired_db)
    _wire_success(
        monkeypatch,
        {"status": "done", "permission_code": 1, "permission_msg": "仅自己可见"},
    )

    result = await svc.execute(
        1,
        {"note_id": "n1", "title": "目标笔记", "target_privacy": 1, "operator_id": 7},
    )

    assert result["status"] == "done"
    async with wired_db() as session:
        row = await session.scalar(
            select(PublishedNote).where(PublishedNote.note_id == "n1")
        )
    assert row.permission_code == 1
    assert row.permission_msg == "仅自己可见"
    assert row.visibility_changed_by == 7
    assert row.visibility_changed_at is not None


async def test_skipped_leaves_trace_untouched(wired_db, monkeypatch):
    """skipped(本就是目标档)什么都没改 → 不写 visibility_changed_*(留痕语义是"我们改的")。"""
    await _seed_note(wired_db, permission_code=1)
    _wire_success(monkeypatch, {"status": "skipped", "permission_code": 1})

    result = await svc.execute(
        1,
        {"note_id": "n1", "title": "目标笔记", "target_privacy": 1, "operator_id": 7},
    )

    assert result["status"] == "skipped"
    async with wired_db() as session:
        row = await session.scalar(
            select(PublishedNote).where(PublishedNote.note_id == "n1")
        )
    assert row.visibility_changed_at is None and row.visibility_changed_by is None


async def test_missing_ledger_row_does_not_fail_the_job(wired_db, monkeypatch):
    """台账里还没有这条笔记(没同步到)→ 只告警不建行,任务仍是 done(平台侧已生效)。"""
    _wire_success(
        monkeypatch,
        {"status": "done", "permission_code": 1, "permission_msg": "仅自己可见"},
    )

    result = await svc.execute(
        1, {"note_id": "n404", "title": "目标笔记", "target_privacy": 1}
    )

    assert result["status"] == "done"
    async with wired_db() as session:
        rows = (await session.execute(select(PublishedNote))).scalars().all()
    assert rows == []


# ---------------- 平台字段解析 + T2 同步覆盖 ----------------


def test_permission_parsers_keep_platform_raw_values():
    """存平台原值:0/1 原样,公开笔记的空串 msg 原样;读不出 → None(=未知,不是公开)。"""
    assert cnl.permission_code_of({"permission_code": 0}) == 0
    assert cnl.permission_code_of({"permission_code": 1}) == 1
    # 未来出现第三态也原样落,不映射成 public/private
    assert cnl.permission_code_of({"permission_code": 4}) == 4
    assert cnl.permission_code_of({}) is None
    assert cnl.permission_code_of({"permission_code": "1"}) is None
    assert cnl.permission_msg_of({"permission_msg": ""}) == ""
    assert cnl.permission_msg_of({"permission_msg": "仅自己可见"}) == "仅自己可见"
    assert cnl.permission_msg_of({}) is None


def test_sync_applies_permission_fields():
    """T2 同步把平台可见性覆盖进台账行(运营在 APP 上手改也会被纠正回来)。"""
    row = PublishedNote(account_id=1, title="旧标题")
    now = datetime(2026, 7, 31)

    ledger._apply_platform_fields(
        row,
        {"id": "n1", "display_title": "标题", "permission_code": 1,
         "permission_msg": "仅自己可见"},
        now,
    )

    assert row.permission_code == 1 and row.permission_msg == "仅自己可见"


def test_sync_does_not_wipe_known_visibility():
    """平台这次没给 permission_code → 不拿 None 盖掉已知档位(与 title 同款纪律)。"""
    row = PublishedNote(account_id=1, title="旧标题")
    row.permission_code = 1
    row.permission_msg = "仅自己可见"

    ledger._apply_platform_fields(
        row, {"id": "n1", "display_title": "标题"}, datetime(2026, 7, 31)
    )

    assert row.permission_code == 1 and row.permission_msg == "仅自己可见"


def test_sync_records_public_note_as_zero():
    """公开笔记 permission_code=0 + 空 msg 也要落库(0 是事实,不是"没值")。"""
    row = PublishedNote(account_id=1, title="旧标题")
    row.permission_code = 1

    ledger._apply_platform_fields(
        row,
        {"id": "n1", "display_title": "标题", "permission_code": 0, "permission_msg": ""},
        datetime(2026, 7, 31),
    )

    assert row.permission_code == 0 and row.permission_msg == ""


# ---------------- 台账纪律 ----------------


def test_note_visibility_is_not_idempotent_kind():
    """僵死不自动重跑:期间运营可能手工改回公开,重跑会再次把它藏起来。"""
    assert "note_visibility" not in repo._IDEMPOTENT_KINDS


def test_account_worker_resolves_note_visibility_execute():
    """account_worker 按 kind 能解析到本服务的 execute(否则子进程会兜底置 error)。"""
    from app import account_worker

    assert account_worker._resolve_execute("note_visibility") is not None
