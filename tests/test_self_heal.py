"""SelfHealLocator:快照精简 / 选择器派生 / 安全校验 / LLM ref 解析。"""

import pytest

from app.browser import self_heal


def _el(**kw):
    base = {"ref": 1, "tag": "button", "role": "", "text": "", "attrs": {},
            "bbox": {"x": 0, "y": 0, "width": 10, "height": 10}, "visible": True}
    base.update(kw)
    return base


def test_build_snapshot_text_format_and_truncation():
    els = [_el(ref=3, tag="button", text="发布", attrs={"id": "pub", "type": "submit"})]
    txt = self_heal.build_snapshot_text(els)
    assert "[3]" in txt and "button" in txt and "发布" in txt and "id=pub" in txt


def test_element_to_selector_priority():
    assert self_heal.element_to_selector(_el(attrs={"id": "x"})) == "#x"
    assert self_heal.element_to_selector(_el(attrs={"data-testid": "t"})) == '[data-testid="t"]'
    assert self_heal.element_to_selector(_el(attrs={"aria-label": "标题"})) == '[aria-label="标题"]'
    assert self_heal.element_to_selector(_el(tag="input", attrs={"name": "n"})) == 'input[name="n"]'
    assert self_heal.element_to_selector(_el(tag="button", attrs={"class": "a b"})) == "button.a"
    assert self_heal.element_to_selector(_el(tag="div", attrs={})) == "div"


def test_passes_safety_publish_button():
    # 发布按钮:必须 text/aria 含发布 且 button/a
    assert self_heal.passes_safety(_el(tag="button", text="发布"), "publish_button") is True
    assert self_heal.passes_safety(_el(tag="button", attrs={"aria-label": "发布笔记"}), "publish_button") is True
    assert self_heal.passes_safety(_el(tag="input", text="发布"), "publish_button") is False  # 非 button
    assert self_heal.passes_safety(_el(tag="button", text="取消"), "publish_button") is False  # 不含发布


def test_passes_safety_input_intents():
    assert self_heal.passes_safety(_el(tag="input"), "title_input") is True
    assert self_heal.passes_safety(_el(tag="textarea"), "content_input") is True
    assert self_heal.passes_safety(_el(tag="div", attrs={"contenteditable": "true"}), "content_input") is True
    assert self_heal.passes_safety(_el(tag="button"), "title_input") is False


def test_llm_locate_parses_ref(monkeypatch):
    class _Resp:
        def __init__(self, content):
            self.choices = [type("C", (), {"message": type("M", (), {"content": content})})]

    class _FakeClient:
        def __init__(self, *a, **k):
            self.chat = type("Chat", (), {"completions": type("Comp", (), {
                "create": staticmethod(lambda **k: _Resp("元素编号 3"))})()})()

    monkeypatch.setattr(self_heal, "OpenAI", _FakeClient)
    monkeypatch.setattr(self_heal.settings, "LLM_API_KEY", "k")
    assert self_heal.llm_locate("[3] button 发布", "发布按钮") == 3


def test_llm_locate_no_digit_returns_none(monkeypatch):
    class _Resp:
        def __init__(self, content):
            self.choices = [type("C", (), {"message": type("M", (), {"content": content})})]

    class _FakeClient:
        def __init__(self, *a, **k):
            self.chat = type("Chat", (), {"completions": type("Comp", (), {
                "create": staticmethod(lambda **k: _Resp("没有匹配"))})()})()

    monkeypatch.setattr(self_heal, "OpenAI", _FakeClient)
    monkeypatch.setattr(self_heal.settings, "LLM_API_KEY", "k")
    assert self_heal.llm_locate("...", "...") is None


def test_llm_locate_exception_returns_none(monkeypatch):
    class _Boom:
        def __init__(self, *a, **k):
            raise RuntimeError("net down")
    monkeypatch.setattr(self_heal, "OpenAI", _Boom)
    monkeypatch.setattr(self_heal.settings, "LLM_API_KEY", "k")
    assert self_heal.llm_locate("...", "...") is None
