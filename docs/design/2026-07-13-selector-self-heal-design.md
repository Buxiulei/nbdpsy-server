# 发布流程选择器自愈(SelfHealLocator)设计

**日期**:2026-07-13
**决策**:给 nbdpsy-server 的发布流程加回"选择器自愈"——硬编码 CSS 选择器全部失败时,
用 LLM 看页面精简 DOM 文本、指认正确元素、用它完成操作,并把学到的稳定选择器持久化,
下次硬编码路径直接命中,实现自我维护。**默认关闭**,配了 LLM key 才启用,零行为变更风险。

## 背景与关键前提

老仓(小红书运营工具)有 `smart_browser/` 一整套(ActionCache SQLite + PageUnderstanding
+ SelectorRegistry + execute_action 纠错 + schemas)。**但调研证实:它在发布流程里是哑的**——
发布路径调 `locate_sync`(注释明确"同步版永不调 LLM,只查缓存"),且 intent 是从 CSS 选择器
字符串机器拼的,冷缓存下直接返回 None。真正会调 LLM 的 `execute_action` 路径发布流程从未接。

因此本设计**不照搬**老仓那套(复杂且部分失效),而是建一个"最小但真能跑"的版本:只在选择器
全失败时同步触发一次 LLM 定位 + 持久化学习。

LLM 选型(用户定):**Qwen/DashScope 文本版**(qwen-flash,openai SDK 兼容接口,传精简 DOM
文本不用截图)。安全边界(用户定):**全部步骤含发布按钮**都可自愈,但发布按钮加防误点校验。

server 当前:`_find_element_with_retry(selectors, timeout, must_be_visible)` 是发布流程唯一的
元素定位收口点,6 处调用(上传框×2、编辑指示、标题、正文×2);发布按钮(step7)是特化的
closed Shadow DOM 像素定位,不走该收口点。发布流程在 worker 线程内**同步**执行
(sync Camoufox,经 `asyncio.to_thread` 下沉)。server 配置里当前**零 LLM 配置**。

## 架构

### 新增模块 1:`app/browser/self_heal.py`

纯定位逻辑,不碰持久化(持久化在 registry)。对外:

```python
def snapshot_interactive(page) -> list[dict]:
    """同步 page.evaluate 跑 JS,收集 ≤100 个可交互元素。移植老仓 JS 精简版。
    选择器集:a, button, input, select, textarea, [role=button/link/tab/menuitem],
    [onclick], [contenteditable], [tabindex]。每元素:
    {ref:int(1..N), tag, role, text(≤50), attrs:{id,class,name,type,placeholder,
     aria-label,data-testid}(各≤80), bbox:{x,y,width,height}, visible:bool}。
    仅收视口内可见元素;ref 从 1 递增,>100 即 break。"""

def build_snapshot_text(elements: list[dict]) -> str:
    """转给 LLM 的精简文本,每行 `[3] button "发布" id=xxx type=submit (230,450 120x40)`,
    仅展示 id/type/placeholder/name/data-testid/aria-label。目标 ≤2000 token。"""

def llm_locate(snapshot_text: str, intent: str) -> int | None:
    """同步 openai.OpenAI(base_url=LLM_BASE_URL)→ LLM_MODEL。
    prompt:'你是网页元素定位助手' + 快照 + 用户意图 + '只返回元素编号(纯数字)'。
    temperature=0, 超时 LLM_TIMEOUT。re.findall(r'\\d+') 取第一个数字为 ref;
    无数字/异常/超时 → None。"""

def element_to_selector(el: dict) -> str | None:
    """从选中元素派生稳定 CSS 选择器,优先级:id > data-testid > aria-label > name >
    tag.首个class > tag。无可用锚点 → None(退回坐标点击)。"""

class SelfHealLocator:
    def locate(self, page, intent_key: str, intent_desc: str) -> ElementHandle | None:
        """收口:snapshot → build_text → llm_locate(得 ref)→ 在快照里取该 ref 元素 →
        安全校验(见下,不过 → None)→ 派生选择器;能派生则 page.query_selector 拿 handle,
        不能则用 bbox 中心 elementFromPoint 拿 handle → 命中则 registry.learn(intent_key,
        selector, meta) → 返回 handle。任一步失败 → None(不抛异常)。"""
```

**安全校验**(`locate` 内,防 LLM 指错元素在发布链误点):

- 危险动作 `intent_key == "publish_button"`:选中元素必须 `text` 或 `aria-label` 含
  "发布"或"publish"(小写匹配)**且** tag ∈ {button, a} 或 role ∈ {button, link};
  不满足 → 返回 None(**宁可失败不误发**)。
- 输入类(`title_input`/`content_input`/`upload_image_input`/`editor_ready`):选中元素
  tag ∈ {input, textarea} 或有 `contenteditable`;不满足 → None。

### 新增模块 2:`app/browser/selector_registry.py`

持久化学到的选择器(自我维护),纯 JSON,不用 SQLite。

```python
# 文件:settings.DATA_DIR/selector_registry.json
# 结构:{intent_key: {"desc": str,
#         "learned": [{"selector": str, "source": "selfheal",
#                      "learned_at": "<ISO>", "success_count": int}]}}

class SelectorRegistry:
    def __init__(self, path: str | None = None): ...   # 默认 DATA_DIR/selector_registry.json
    def get(self, intent_key: str) -> list[str]:
        """返回该 key 的 learned 选择器,按 success_count 降序(学得越稳越先试)。"""
    def learn(self, intent_key: str, selector: str, desc: str) -> None:
        """upsert:同 selector 已存在则 success_count+1(否则追加,初始 1)。threading.Lock
        保护读改写(发布本就同号串行,冲突极小,锁只防同进程并发)。

        语义澄清:success_count 是"该选择器被自愈重新学到的次数"(稳定性代理),**不是**
        它经 learned 前置路径命中的次数——learned 选择器命中时选择器没失败、不触发自愈、
        不调 learn,故 count 保持不变。多个 learned 选择器时(页面多次改版)按此排序,
        单个时始终为 1,均满足"学过的先试"即可。"""
```

> **时间戳**:`learn` 的 `learned_at` 由 `self_heal` 在调用时用 `datetime.now(timezone.utc)`
> 生成传入(registry 不自取时间,便于测试注入固定值)。

### 挂载点改造:`_find_element_with_retry`

```python
def _find_element_with_retry(
    self, selectors: List[str], timeout: int = 10,
    must_be_visible: bool = True,
    intent_key: str | None = None, intent_desc: str | None = None,
) -> Optional[ElementHandle]:
    # 1. 自我维护:有 learned 选择器就插到 selectors 最前(通常直接命中,零 LLM)。
    effective = selectors
    if intent_key:
        learned = self._registry.get(intent_key)
        if learned:
            effective = learned + [s for s in selectors if s not in learned]
    # 2. 现有逻辑:timeout 内轮询 effective(wait_for_selector / query_selector_all 兜底)。
    ...
    # 3. 全失败 + 有 intent_key + 启用 → 自愈 fallback。
    if intent_key and settings.SELFHEAL_ENABLED and settings.LLM_API_KEY:
        handle = self._locator.locate(self.page, intent_key, intent_desc or intent_key)
        if handle:
            return handle
    return None
```

- `self._registry` / `self._locator` 在 SyncClient/发布 runner 构造时注入(单例,复用文件句柄)。
- 6 个输入类调用点补 `intent_key` + `intent_desc`:
  `upload_image_input`(上传图片的 file input)、`editor_ready`(编辑器就绪指示元素)、
  `title_input`(标题输入框)、`content_input`(正文输入框)。

### 发布按钮(step7)自愈

用户要发布按钮也自愈。**限制**:step7 的难例是 closed Shadow DOM(`<xhs-publish-btn>`),
按钮不在可访问 DOM,LLM 的 DOM 文本快照**看不见它**——自愈只能兜住"按钮在可访问 DOM
(light DOM / global 候选)但没匹配上"的情形。

改法:step7 现有全部策略(light / open-shadow / closed-shadow 像素质心 / global 候选)
**都失败后**,再 fallback 一次 `self._locator.locate(page, "publish_button", "发布笔记的发布按钮")`;
命中(且过发布按钮安全校验)则点击它。closed-shadow 情形自愈无能为力,维持现有像素兜底。

### 配置(`app/core/config.py` 新增,全默认值)

```python
SELFHEAL_ENABLED: bool = False           # 总开关,默认关(不改现有行为)
LLM_API_KEY: str = ""                     # 空则强制关,即使 ENABLED
LLM_BASE_URL: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
LLM_MODEL: str = "qwen-flash"
LLM_TIMEOUT: int = 15
```

`.env.example` 补对应行(真实值 placeholder)。改 config 须 `restart`(pydantic 锁字段)。
`requirements.txt` 加 `openai>=1.0`。

## 数据流

发布某步找元素 → `_find_element_with_retry(selectors, intent_key=...)` →
[learned 选择器前置 → 轮询 effective] 命中即返回(绝大多数,零 LLM)→
全失败且启用 → `SelfHealLocator.locate` → snapshot(同步 evaluate)→ LLM(同步 HTTP 1-3s)
→ ref → 安全校验 → 派生选择器/坐标 → handle → `registry.learn` 持久化 → 返回 handle。
下次同步骤:learned 选择器已在 selectors 前列 → 直接命中,不再调 LLM = 自我维护闭环。

## 错误处理

- 自愈全链任一步失败(snapshot 异常 / LLM 超时 / 解析失败 / 安全校验不过 / 派生+坐标都拿不到
  handle)→ `locate` 返回 None → `_find_element_with_retry` 返回 None → **退化为现有"直接失败"
  行为**,发布链照常按原逻辑处理(该步失败/重试),**自愈绝不抛异常打断发布**。
- 每次自愈尝试与结果 loguru 记录:`intent_key` / snapshot 元素数 / LLM 返回 ref / 是否过校验
  / 学到的选择器。便于人工复核 + 把稳定选择器提升进硬编码列表。
- LLM key 未配 / ENABLED=False → 自愈整条不触发,`_find_element_with_retry` 行为与现在**逐字节一致**。

## 测试策略

- **纯函数单测**:`build_snapshot_text`(喂假 elements 验行格式/截断)、`element_to_selector`
  (id/data-testid/aria-label/name/tag.class 各优先级 + 无锚点→None)。
- **registry 单测**:临时 JSON,`learn` upsert + success_count 累加 + `get` 降序;并发锁不测行为只测正确性。
- **llm_locate 单测**:monkeypatch `openai.OpenAI` client 返回 "编号 3" / "3" / "没有" → 验解析
  (取 3 / 取 3 / None);超时/异常 → None。
- **安全校验单测**:`publish_button` intent + LLM 指向非发布元素(如 input)→ locate 返回 None;
  指向 text="发布" 的 button → 通过。输入类 intent 指向 button → None。
- **挂载点集成**:
  - 硬编码全失败 + monkeypatch `SelfHealLocator.locate` 返回假 handle → 验 fallback 触发。
  - `SELFHEAL_ENABLED=False` 或 `LLM_API_KEY=""` → 自愈不触发,返回 None(与现状一致)。
  - learned 选择器存在 → 验其被前置进 effective selectors。
- **真机验证**:配 key,故意把某步 selectors 改错触发一次真自愈,看 loguru 学到选择器 +
  `selector_registry.json` 落盘 + 二次运行走 learned 路径不再调 LLM。

## 明确不做(YAGNI)

- 不移植 ActionCache(SQLite 页面结构 hash 缓存)——learned 选择器进 selectors 前列即等效,更简单。
- 不移植 `execute_action` 多轮 LLM 纠错循环——收口在 `_find_element_with_retry`,定位失败即失败,
  不做定位后的动作级重试。
- 不做 vision/截图路径(选 Qwen 文本版)。
- 不移植老仓 SelectorRegistry 的 `element_info`/`last_fix`/复杂 learned 结构,只存 selector + 计数 + 时间。
- 不改发布链的重试/退避/状态机(自愈是纯定位增强,对上层透明)。
