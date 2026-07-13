"""SelectorRegistry:JSON 持久化学到的选择器,success_count 降序。"""

from app.browser.selector_registry import SelectorRegistry


def test_learn_and_get_new_selector(tmp_path):
    reg = SelectorRegistry(path=str(tmp_path / "reg.json"))
    reg.learn("title_input", "input.title", "标题输入框", "2026-07-13T00:00:00+00:00")
    assert reg.get("title_input") == ["input.title"]


def test_learn_same_selector_bumps_count_not_duplicate(tmp_path):
    reg = SelectorRegistry(path=str(tmp_path / "reg.json"))
    reg.learn("title_input", "input.title", "标题", "2026-07-13T00:00:00+00:00")
    reg.learn("title_input", "input.title", "标题", "2026-07-13T01:00:00+00:00")
    assert reg.get("title_input") == ["input.title"]  # 不重复
    # count 累加体现在排序:再学一个更"稳"的应排在后学但 count 低的前面
    reg.learn("title_input", "input.title2", "标题", "2026-07-13T02:00:00+00:00")
    # input.title count=2 排在 input.title2 count=1 前
    assert reg.get("title_input") == ["input.title", "input.title2"]


def test_get_unknown_key_returns_empty(tmp_path):
    reg = SelectorRegistry(path=str(tmp_path / "reg.json"))
    assert reg.get("nope") == []


def test_persistence_across_instances(tmp_path):
    p = str(tmp_path / "reg.json")
    SelectorRegistry(path=p).learn("k", "sel", "d", "2026-07-13T00:00:00+00:00")
    assert SelectorRegistry(path=p).get("k") == ["sel"]
