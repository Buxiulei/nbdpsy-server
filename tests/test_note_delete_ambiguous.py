"""同名歧义硬闸:管理页同题卡 ≥2 张时拒绝不可逆删除(2026-08-13 李冠阳双篇事故)。

事故形态:同题两篇一死一活,`--count 1` 的实际语义是"删管理页 DOM 首张"=按发布时间
倒序的最新篇——恰好删掉要保留的健康篇。歧义下的不可逆操作必须拒绝执行,
确经人工确认的同题清理(删 N 留 1)显式带 allow_ambiguous 放行。
"""

import pytest

from app.browser.note_delete import NoteDeleteError, delete_notes_by_title


class _FakePage:
    """最小页面替身:管理页就绪,_FIND_CARD_JS 返回指定同题卡数;任何点击都记账。"""

    def __init__(self, card_count: int):
        self._count = card_count
        self.clicks = 0
        self.url = "https://creator.xiaohongshu.com/new/note-manager"

    def evaluate(self, js, *args):
        if "TreeWalker" in js or "createTreeWalker" in js:
            if self._count == 0:
                return {"found": False, "count": 0}
            return {"found": True, "count": self._count,
                    "card": {"x": 100, "y": 200, "w": 200, "h": 150}}
        return {}

    def query_selector(self, sel):
        return object()  # 管理页就绪探测直接过

    def mouse_click(self):
        self.clicks += 1


@pytest.fixture(autouse=True)
def _skip_page_setup(monkeypatch):
    """绕开真实管理页导航与拟人层:本测试只关心闸,不关心浏览器。"""
    monkeypatch.setattr("app.browser.note_delete._open_note_manage", lambda *a, **k: None)

    class _FakeHuman:
        def __init__(self, page): self._page = page
        def wait(self, *a, **k): pass
        def hover(self, *a, **k): pass
        def click(self, *a, **k): self._page.mouse_click()

    monkeypatch.setattr("app.browser.note_delete.SyncHumanActions", _FakeHuman)


def test_two_same_title_cards_refused_without_flag():
    """同题 2 张 + 未带 allow_ambiguous → ambiguous_title 拒绝,零点击。"""
    page = _FakePage(card_count=2)
    with pytest.raises(NoteDeleteError) as e:
        delete_notes_by_title(page, 1, "同名标题", count=1)
    assert e.value.reason.startswith("ambiguous_title")
    assert page.clicks == 0  # 闸在任何破坏性动作之前


def test_flag_allows_ambiguous_delete_path():
    """带 allow_ambiguous → 不再被闸拦(后续流程走到定位垃圾桶,替身返回空则报别的错)。"""
    page = _FakePage(card_count=2)
    with pytest.raises(NoteDeleteError) as e:
        delete_notes_by_title(page, 1, "同名标题", count=1, allow_ambiguous=True)
    assert not e.value.reason.startswith("ambiguous_title")


def test_single_card_unaffected():
    """同题仅 1 张:原路径不受影响(闸不介入)。"""
    page = _FakePage(card_count=1)
    with pytest.raises(NoteDeleteError) as e:
        delete_notes_by_title(page, 1, "唯一标题", count=1)
    assert not e.value.reason.startswith("ambiguous_title")
