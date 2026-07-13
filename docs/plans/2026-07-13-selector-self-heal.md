# 发布流程选择器自愈实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax.

**Goal:** 硬编码 CSS 选择器全失败时,LLM 看页面精简 DOM 指认正确元素并用它,学到的选择器持久化下次直接命中(自我维护)。默认关闭,配 key 才启用。

**Architecture:** 两个解耦新模块 `self_heal.py`(定位,不依赖 registry)+ `selector_registry.py`(JSON 持久化);挂载点 `_find_element_with_retry` 编排:learned 前置 → 轮询 → 全失败调自愈 → 拿到 (handle,selector) 后 registry.learn。发布流程同步跑,LLM 用同步 openai client。

**Tech Stack:** Python sync、Camoufox/Playwright sync page API、openai SDK(DashScope 兼容)、pytest。

**Spec:** `docs/design/2026-07-13-selector-self-heal-design.md`(接口/安全校验/数据流以 spec 为准)

## Global Constraints

- 解释器 `/home/roots/nbdpsy-server/.venv/bin/python`;测试 `source /home/roots/nbdpsy-server/.venv/bin/activate && python -m pytest tests/ -q`(cwd = 各自 worktree 根)。
- 注释/docstring/commit 全中文;禁 emoji;commit `type(scope): 描述`;**禁 `git add -A`,显式列文件**。
- 自愈**绝不抛异常打断发布链**:任一步失败(snapshot/LLM/解析/校验/取 handle)→ 返回 None,退化为现有"直接失败"。
- **默认关闭**:`SELFHEAL_ENABLED=False` 或 `LLM_API_KEY==""` → 自愈整条不触发,`_find_element_with_retry` 行为与现状逐字节一致。
- 脚本/模块内**不用** `datetime.now()` 之外无注入的时间——`selector_registry.learn` 的 `learned_at` 由调用方(self_heal 挂载点)用 `datetime.now(timezone.utc).isoformat()` 生成传入,便于测试注入固定值。
- 安全校验:`publish_button` intent 选中元素必须 text/aria-label 含"发布"或"publish" 且 tag∈{button,a}/role∈{button,link},否则 locate 返回 None(宁可失败不误发);输入类 intent 必须 tag∈{input,textarea} 或有 contenteditable。
- 每次自愈尝试+结果 loguru 记录(intent_key/元素数/ref/是否过校验/学到的选择器)。

---

### Task 1: 配置 + 依赖(foundation,先行)

**Files:**
- Modify: `app/core/config.py`、`backend/.env.example`(若无则 `.env.example`)、`requirements.txt`
- Test: `tests/test_config.py`(若已有则加用例)

**Interfaces(Produces):**
- `settings.SELFHEAL_ENABLED: bool`、`settings.LLM_API_KEY: str`、`settings.LLM_BASE_URL: str`、`settings.LLM_MODEL: str`、`settings.LLM_TIMEOUT: int`

- [ ] **Step 1: 写失败测试**

在 `tests/test_config.py` 加(若文件不存在则新建,参照现有 config 测试风格):

```python
def test_selfheal_config_defaults():
    """自愈配置默认值:默认关、key 空、DashScope 默认 base_url。"""
    from app.core.config import Settings
    s = Settings()
    assert s.SELFHEAL_ENABLED is False
    assert s.LLM_API_KEY == ""
    assert s.LLM_BASE_URL == "https://dashscope.aliyuncs.com/compatible-mode/v1"
    assert s.LLM_MODEL == "qwen-flash"
    assert s.LLM_TIMEOUT == 15
```

- [ ] **Step 2: 跑测试确认失败**

`source /home/roots/nbdpsy-server/.venv/bin/activate && python -m pytest tests/test_config.py::test_selfheal_config_defaults -q` → FAIL(AttributeError)。

- [ ] **Step 3: 实现**

`app/core/config.py` 的 `Settings` 类加(带默认值):

```python
    # ── 选择器自愈(SelfHealLocator)。默认关闭,配 LLM_API_KEY 且开 ENABLED 才生效。 ──
    SELFHEAL_ENABLED: bool = False
    LLM_API_KEY: str = ""
    LLM_BASE_URL: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    LLM_MODEL: str = "qwen-flash"
    LLM_TIMEOUT: int = 15
```

`requirements.txt` 追加一行 `openai>=1.0`。

`.env.example`(路径以仓库现有那份为准,搜 `PUBLIC_BASE_URL` 定位)追加:

```
# 选择器自愈(可选,默认关)。开启需 SELFHEAL_ENABLED=true 且填 LLM_API_KEY
SELFHEAL_ENABLED=false
LLM_API_KEY=
LLM_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
LLM_MODEL=qwen-flash
LLM_TIMEOUT=15
```

- [ ] **Step 4: 跑测试通过**

`python -m pytest tests/test_config.py -q` → PASS。若仓库有 `test_env_example.py`(校验 .env.example 覆盖所有 Settings 字段)也须绿。

- [ ] **Step 5: 提交**

```bash
git add app/core/config.py requirements.txt .env.example tests/test_config.py
git commit -m "feat(selfheal): 配置字段 + openai 依赖(默认关闭)"
```

（.env.example 实际路径若在 backend/ 下则相应替换;`git add` 显式列真实路径。）

---

### Task 2: selector_registry.py(并行,新文件)

**Files:**
- Create: `app/browser/selector_registry.py`、`tests/test_selector_registry.py`

**Interfaces:**
- Consumes:`settings.DATA_DIR`(已存在)
- Produces:
  ```python
  class SelectorRegistry:
      def __init__(self, path: str | None = None) -> None   # 默认 <DATA_DIR>/selector_registry.json
      def get(self, intent_key: str) -> list[str]           # learned 选择器,success_count 降序;无则 []
      def learn(self, intent_key: str, selector: str, desc: str, learned_at: str) -> None  # upsert,同 selector→count+1
  ```

- [ ] **Step 1: 写失败测试 `tests/test_selector_registry.py`**

```python
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
```

- [ ] **Step 2: 确认失败** `pytest tests/test_selector_registry.py -q` → FAIL(模块不存在)。

- [ ] **Step 3: 实现 `app/browser/selector_registry.py`**

```python
"""发布流程自愈学到的选择器持久化(JSON)。

结构:{intent_key: {"desc": str, "learned": [{"selector","source","learned_at","success_count"}]}}
learned 按 success_count 降序返回(学得越稳越先试)。success_count 语义见 design spec:
自愈重新学到该选择器的次数(稳定性代理),非 learned 前置路径的命中次数。
不用 SQLite——learned 选择器进 selectors 前列即等效缓存。
"""

import json
import threading
from pathlib import Path

from app.core.config import settings


class SelectorRegistry:
    def __init__(self, path: str | None = None) -> None:
        self._path = Path(path) if path else Path(settings.DATA_DIR) / "selector_registry.json"
        self._lock = threading.Lock()

    def _load(self) -> dict:
        if not self._path.is_file():
            return {}
        try:
            return json.loads(self._path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}

    def _save(self, data: dict) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def get(self, intent_key: str) -> list[str]:
        entry = self._load().get(intent_key)
        if not entry:
            return []
        learned = sorted(
            entry.get("learned", []),
            key=lambda x: x.get("success_count", 0),
            reverse=True,
        )
        return [x["selector"] for x in learned]

    def learn(self, intent_key: str, selector: str, desc: str, learned_at: str) -> None:
        with self._lock:
            data = self._load()
            entry = data.setdefault(intent_key, {"desc": desc, "learned": []})
            entry["desc"] = desc
            for item in entry["learned"]:
                if item["selector"] == selector:
                    item["success_count"] = item.get("success_count", 1) + 1
                    item["learned_at"] = learned_at
                    self._save(data)
                    return
            entry["learned"].append({
                "selector": selector, "source": "selfheal",
                "learned_at": learned_at, "success_count": 1,
            })
            self._save(data)
```

- [ ] **Step 4: 跑测试通过** `pytest tests/test_selector_registry.py -q` → PASS。

- [ ] **Step 5: 提交**

```bash
git add app/browser/selector_registry.py tests/test_selector_registry.py
git commit -m "feat(selfheal): SelectorRegistry JSON 持久化学到的选择器"
```

---

### Task 3: self_heal.py(并行,新文件)

**Files:**
- Create: `app/browser/self_heal.py`、`tests/test_self_heal.py`

**Interfaces:**
- Consumes:`settings.LLM_*`(Task 1);sync Playwright `page`(`page.evaluate`/`query_selector`/`evaluate_handle`)
- Produces:
  ```python
  def snapshot_interactive(page) -> list[dict]           # ≤100 可交互元素
  def build_snapshot_text(elements: list[dict]) -> str   # ≤2000 token 文本
  def llm_locate(snapshot_text: str, intent: str) -> int | None   # 同步 openai → ref
  def element_to_selector(el: dict) -> str | None        # 派生稳定 CSS
  def passes_safety(el: dict, intent_key: str) -> bool    # 安全校验
  class SelfHealLocator:
      def locate(self, page, intent_key: str, intent_desc: str) -> tuple | None  # (handle, selector|None)
  ```

- [ ] **Step 1: 写失败测试 `tests/test_self_heal.py`**

覆盖纯函数 + 安全校验 + llm_locate 解析(monkeypatch openai)。完整用例:

```python
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
```

- [ ] **Step 2: 确认失败** `pytest tests/test_self_heal.py -q` → FAIL(模块不存在)。

- [ ] **Step 3: 实现 `app/browser/self_heal.py`**

要点(完整实现,遵 spec §self_heal.py):
- 顶部 `from openai import OpenAI`(模块级,便于测试 monkeypatch `self_heal.OpenAI`)、`from app.core.config import settings`、`from loguru import logger`、`import re`。
- `snapshot_interactive(page)`:`page.evaluate(JS)`,JS 收集选择器集 `a,button,input,select,textarea,[role=button],[role=link],[role=tab],[role=menuitem],[onclick],[contenteditable],[tabindex]`,逐元素判可见(getBoundingClientRect 在视口内 + display/visibility)、ref 从 1 递增 >100 break、text 取 `aria-label||innerText||placeholder||title||value` 截 50、attrs 取 `id/class/name/type/placeholder/aria-label/data-testid` 各截 80、返回 list[dict]。异常 → 返回 []。
- `build_snapshot_text(elements)`:每行 `[{ref}] {tag} "{text}" id={id} type={type} placeholder={ph} name={name} data-testid={dt} aria-label={al} ({x},{y} {w}x{h})`,仅出现存在的属性。
- `llm_locate(snapshot_text, intent)`:`OpenAI(api_key=settings.LLM_API_KEY, base_url=settings.LLM_BASE_URL)`;`client.chat.completions.create(model=settings.LLM_MODEL, messages=[{"role":"user","content":prompt}], temperature=0, timeout=settings.LLM_TIMEOUT)`;prompt = "你是网页元素定位助手。\n## 页面快照\n{snapshot_text}\n## 用户意图\n{intent}\n只返回最匹配的元素编号(纯数字),例如:3"。取 `resp.choices[0].message.content`,`re.findall(r'\\d+')` 首个转 int;无/异常 → None(try/except 包全,logger.warning)。
- `element_to_selector(el)`:优先级 id→`#{id}`、data-testid→`[data-testid="{v}"]`、aria-label→`[aria-label="{v}"]`、name→`{tag}[name="{v}"]`、class(取首个非空)→`{tag}.{cls}`、否则 `{tag}`;都无 tag 兜底 None。
- `passes_safety(el, intent_key)`:见 Global Constraints 规则。
- `SelfHealLocator.locate(page, intent_key, intent_desc)`:snapshot → 空则 None;build_text;`ref = llm_locate(text, intent_desc)`;None 则 None;`el = 找 ref 对应元素`(遍历 snapshot);找不到 None;`passes_safety(el, intent_key)` 不过 → logger.warning + None;`sel = element_to_selector(el)`;`handle = page.query_selector(sel)` if sel else None;handle 为 None 时用 bbox 中心 `page.evaluate_handle("(p)=>document.elementFromPoint(p.x,p.y)", {x:cx,y:cy})` 的 `.as_element()`;handle 仍 None → None;logger.info 记录;返回 `(handle, sel)`。全程 try/except 兜底 → None。

- [ ] **Step 4: 跑测试通过** `pytest tests/test_self_heal.py -q` → PASS。

- [ ] **Step 5: 提交**

```bash
git add app/browser/self_heal.py tests/test_self_heal.py
git commit -m "feat(selfheal): SelfHealLocator 快照+LLM定位+选择器派生+安全校验(不依赖registry)"
```

---

### Task 4: 挂载点接入(串行,Task 1-3 合并后)

**Files:**
- Modify: `app/browser/atomic_tasks.py`(`_find_element_with_retry` + 6 调用点 + step7 fallback + `__init__` 注入 registry/locator)。**不动 sync_client.py**。
- Test: `tests/test_selfheal_integration.py`

**Interfaces:**
- Consumes:Task 1 `settings.SELFHEAL_*/LLM_*`;Task 2 `SelectorRegistry`;Task 3 `SelfHealLocator`
- 真实类:`class XHSPublishAtomicTasks`,`__init__(self, page, enable_debug=None, screenshot_dir=None)`,已持 `self.page`/`self.human`;构造处 sync_client.py:289 `XHSPublishAtomicTasks(self.page)`(不改)。

- [ ] **Step 1: 写失败集成测试 `tests/test_selfheal_integration.py`**

```python
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
```

- [ ] **Step 2: 确认失败** `pytest tests/test_selfheal_integration.py -q` → FAIL(签名无 intent_key / 无 _registry)。

- [ ] **Step 3: 实现**

3a. `_find_element_with_retry` 改签名加 `intent_key: str | None = None, intent_desc: str | None = None`;逻辑:learned 前置(`self._registry.get(intent_key)` 插 selectors 最前去重)→ 现有轮询 → 全失败+`settings.SELFHEAL_ENABLED`+`settings.LLM_API_KEY` → `found = self._locator.locate(self.page, intent_key, intent_desc or intent_key)`;`found` 则 `handle, sel = found`;`sel` 非空 `self._registry.learn(intent_key, sel, intent_desc or intent_key, datetime.now(timezone.utc).isoformat())` → 返回 handle。文件顶部补 `from datetime import datetime, timezone`、`from app.core.config import settings`(若未导)、`from app.browser.selector_registry import SelectorRegistry`、`from app.browser.self_heal import SelfHealLocator`。

3b. `XHSPublishAtomicTasks.__init__` 末尾加:`self._registry = SelectorRegistry()`、`self._locator = SelfHealLocator()`。

3c. 6 调用点补 intent(照抄以下映射):
- line ~568 & ~591 `upload_input_selectors` → `intent_key="upload_image_input", intent_desc="上传图片的 file input"`
- line ~647 `edit_indicators` → `intent_key="editor_ready", intent_desc="编辑器就绪的指示元素"`
- line ~873 `title_selectors` → `intent_key="title_input", intent_desc="笔记标题输入框"`
- line ~900 `content_selectors` → `intent_key="content_input", intent_desc="笔记正文输入框"`
- line ~961 `content_input_selectors` → `intent_key="content_input", intent_desc="笔记正文输入框"`

3d. step7:在现有全部发布按钮策略(light/open-shadow/closed-shadow 像素/global 候选)都未点成后、返回失败前,加一次 fallback:`found = self._locator.locate(self.page, "publish_button", "发布笔记的发布按钮")` if `settings.SELFHEAL_ENABLED and settings.LLM_API_KEY`;`found` 则 `handle, sel = found`;`sel` 非空 learn;`self.human.click(handle, reason="自愈发布按钮")` + `_published()` 验证。closed-shadow 情形 locate 自然返回 None(快照看不见),维持现有像素兜底。

- [ ] **Step 4: 跑测试通过**

`python -m pytest tests/test_selfheal_integration.py tests/test_self_heal.py tests/test_selector_registry.py tests/test_config.py -q` → PASS;全量 `python -m pytest tests/ -q` 不回归。

- [ ] **Step 5: 提交**

```bash
git add app/browser/atomic_tasks.py app/browser/sync_client.py tests/test_selfheal_integration.py
git commit -m "feat(selfheal): _find_element_with_retry 接入自愈 + 6 输入点 intent + step7 发布按钮兜底"
```

---

## 合并与验证(lead)

1. Task 1 先 merge(foundation)。
2. Task 2、3 从新 main 并行实施(互不相扰,各自新文件)→ 各自 review → merge。
3. Task 4 在 1-3 全并后串行实施 → review → merge。
4. 全并后:主仓已是生产目录,`python -m pytest tests/ -q` 全绿即可;**默认 SELFHEAL_ENABLED=False,不重启也不改变生产行为**。
5. 真机验证(可选,需用户配 key):`.env` 设 `SELFHEAL_ENABLED=true` + `LLM_API_KEY=<DashScope key>` → restart → 故意改错某步 selectors 触发一次真自愈 → 看 loguru 学到选择器 + `data/selector_registry.json` 落盘。
