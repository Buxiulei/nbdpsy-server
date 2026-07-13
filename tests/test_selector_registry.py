"""SelectorRegistry:JSON 持久化学到的选择器,success_count 降序。"""

import json

from app.browser.selector_registry import SelectorRegistry, get_default_registry


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


def test_get_default_registry_is_singleton():
    # M1:进程级单例 —— 两次调用返回同一对象(=> 共用同一实例锁,消除跨实例并发写竞争)。
    a = get_default_registry()
    b = get_default_registry()
    assert a is b
    assert a._lock is b._lock  # 同一把锁


def test_save_is_atomic_and_complete(tmp_path):
    # M1:原子写 —— 写后目标文件存在、内容完整可解析,且无临时文件残留。
    p = tmp_path / "reg.json"
    reg = SelectorRegistry(path=str(p))
    reg.learn("k", "sel", "d", "2026-07-13T00:00:00+00:00")
    assert p.is_file()
    data = json.loads(p.read_text(encoding="utf-8"))
    assert data["k"]["learned"][0]["selector"] == "sel"
    assert not list(tmp_path.glob("*.tmp"))  # 临时文件已被 os.replace 消费,无残留
