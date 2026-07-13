"""_find_element_with_retry 自愈集成:全失败→触发→learn;关则不触发;learned 前置。"""

import app.browser.atomic_tasks as atomic_mod
from app.browser.atomic_tasks import XHSPublishAtomicTasks
from app.browser.selector_registry import SelectorRegistry


class _FakePage:
    """恒不命中任何选择器;记录尝试过的选择器顺序(验 learned 前置)。"""
    url = "https://x"
    def __init__(self):
        self.tried = []
    def wait_for_selector(self, selector, *a, **k):
        self.tried.append(selector)
        raise Exception("no match")
    def query_selector_all(self, *a, **k):
        return []


def _make_tasks(tmp_path):
    # 跳过重构造(会 new SyncHumanActions/浏览器),直接装最小状态。
    tasks = XHSPublishAtomicTasks.__new__(XHSPublishAtomicTasks)
    tasks.page = _FakePage()
    tasks._registry = SelectorRegistry(path=str(tmp_path / "reg.json"))
    tasks._locator = atomic_mod.SelfHealLocator()
    tasks.human = type("H", (), {"wait": staticmethod(lambda *a, **k: None)})()
    return tasks


def test_selfheal_disabled_no_trigger(monkeypatch, tmp_path):
    monkeypatch.setattr(atomic_mod.settings, "SELFHEAL_ENABLED", False)
    monkeypatch.setattr(atomic_mod.settings, "LLM_API_KEY", "k")
    called = {"n": 0}
    monkeypatch.setattr(atomic_mod.SelfHealLocator, "locate",
                        lambda self, *a, **k: called.__setitem__("n", called["n"] + 1))
    tasks = _make_tasks(tmp_path)
    r = tasks._find_element_with_retry(["input.x"], timeout=1, intent_key="title_input")
    assert r is None and called["n"] == 0


def test_selfheal_enabled_triggers_and_learns(monkeypatch, tmp_path):
    monkeypatch.setattr(atomic_mod.settings, "SELFHEAL_ENABLED", True)
    monkeypatch.setattr(atomic_mod.settings, "LLM_API_KEY", "k")
    fake_handle = object()
    monkeypatch.setattr(atomic_mod.SelfHealLocator, "locate",
                        lambda self, page, ik, idesc: (fake_handle, "input.learned"))
    tasks = _make_tasks(tmp_path)
    r = tasks._find_element_with_retry(["input.x"], timeout=1,
                                       intent_key="title_input", intent_desc="标题")
    assert r is fake_handle
    assert tasks._registry.get("title_input") == ["input.learned"]


def test_learned_selector_prepended(monkeypatch, tmp_path):
    monkeypatch.setattr(atomic_mod.settings, "SELFHEAL_ENABLED", False)
    monkeypatch.setattr(atomic_mod.settings, "LLM_API_KEY", "")
    tasks = _make_tasks(tmp_path)
    tasks._registry.learn("title_input", "input.learned", "标题", "2026-07-13T00:00:00+00:00")
    tasks._find_element_with_retry(["input.hardcoded"], timeout=1, intent_key="title_input")
    assert tasks.page.tried[0] == "input.learned"  # learned 先试
    assert "input.hardcoded" in tasks.page.tried
