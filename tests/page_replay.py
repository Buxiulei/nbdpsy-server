"""真号页面快照的**离线回放**:让"UI 结构假设"能在 CI 里被证伪,而不是等生产暴露。

## 为什么要有它

本仓的浏览器层全靠 UI 驱动,代码里散着一堆**页面结构假设**。它们的共同问题不是写得
粗心,而是**没法验证** —— 验证要开真号,真号有风控成本,于是假设一写就是几个月没人碰。

代价是实打实的:``_set_quote`` 曾按"接口响应第 i 条 ↔ 弹窗第 i 张卡"取卡,那行代码的
注释当初就写明"这个同序假设没有实测背书"。它活到 2026-08-03 才被真号证伪(实测第 6 张
卡是「彭旱雨」,接口第 6 条是「黄安麟」),期间引用功能 **100% 失败**,而 1615 个单测
全绿 —— 因为那些测试用的是**手写的假页面**,手写时自然照着代码的假设写,
于是假设错了测试也跟着错,测不出任何东西。

**回放夹具换掉的正是这一点**:夹具里的顺序、文案、层级来自真号实拍,不是我按代码
想象出来的。同序假设这类 bug 一跑就红。

## 边界(故意划清)

- **只回放,不模拟**:夹具里没有的选择器返回空,绝不"猜一个合理的"——猜就退回手写假页面
  那条老路了;
- **不支持任意 ``evaluate``**:JS 求值没法离线回放。适用场景是靠 ``query_selector`` /
  接口响应驱动的那些(引用弹窗、活动卡、笔记列表),这些恰好是出过 bug 的地方;
- **夹具是证据不是设计**:改夹具去迁就代码等于自欺,要改只能重新采集。
"""

import json
from pathlib import Path
from typing import Any, Dict, List, Optional

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "pages"


class ReplayElement:
    """夹具里的一个元素:只提供被测代码真用到的那几个读操作。

    ``on_click`` 由用例按需挂载 —— 夹具只记"页面长什么样",不记"点了会怎样"
    (后者是行为断言的事,写死进夹具反而会把期望混进证据)。
    """

    def __init__(self, data: Dict[str, Any], on_click=None):
        self._data = data
        self.on_click = on_click
        self.clicked = 0

    def inner_text(self) -> str:
        return self._data.get("text", "")

    def get_attribute(self, name: str) -> Optional[str]:
        return (self._data.get("attrs") or {}).get(name)

    def is_visible(self) -> bool:
        return bool(self._data.get("visible", True))

    def bounding_box(self) -> Dict[str, float]:
        return self._data.get("rect") or {"x": 0.0, "y": 0.0, "width": 10.0, "height": 10.0}

    def query_selector_all(self, sel: str) -> List["ReplayElement"]:
        return [ReplayElement(d) for d in (self._data.get("children") or {}).get(sel, [])]

    def __repr__(self) -> str:  # 断言失败时能一眼看出是哪张卡
        return f"<ReplayElement {self.inner_text()[:24]!r}>"


class ReplayPage:
    """按快照回答 ``query_selector`` / ``query_selector_all`` 的假 page。

    接口响应通过 ``emit_recorded`` 回放给 ``ComponentResponses`` 那套被动监听 ——
    与真实链路同一条通路,不另开后门。
    """

    def __init__(self, snapshot: Dict[str, Any]):
        self.snapshot = snapshot
        self.url = snapshot.get("url", "")
        self._dom: Dict[str, List[Dict[str, Any]]] = snapshot.get("dom") or {}
        self._listeners: List[Any] = []
        self.clicks: List[str] = []

    # ---- 选择器 ----
    def query_selector_all(self, sel: str) -> List[ReplayElement]:
        # 夹具没有的选择器返回空:绝不猜(猜就退回手写假页面那条老路)
        return [ReplayElement(d) for d in self._dom.get(sel, [])]

    def query_selector(self, sel: str) -> Optional[ReplayElement]:
        hits = self.query_selector_all(sel)
        return hits[0] if hits else None

    def wait_for_timeout(self, _ms: int) -> None:
        pass

    def inner_text(self, _sel: str) -> str:
        return self.snapshot.get("body_text", "")

    # ---- 接口响应回放(与真实链路同一条被动监听通路)----
    def on(self, event: str, fn) -> None:
        if event == "response":
            self._listeners.append(fn)

    def remove_listener(self, event: str, fn) -> None:
        if event == "response" and fn in self._listeners:
            self._listeners.remove(fn)

    def emit_recorded(self, mark: str) -> int:
        """把夹具里某个接口的**全部实录响应**按采集顺序回放,返回回放了几条。

        按 mark 子串匹配(与生产侧 ``ComponentResponses._MARKS`` 同口径),
        这样夹具里存完整 URL、用例里写特征串,两边不必逐字对齐。
        """
        sent = 0
        for rec in self.snapshot.get("api") or []:
            if mark not in rec.get("url", ""):
                continue
            for fn in list(self._listeners):
                fn(_ReplayResponse(rec))
            sent += 1
        return sent

    def set_text(self, sel: str, text: str) -> None:
        """改写某选择器下第一个元素的文案,用于**由用例注入状态迁移**。

        分工是刻意的:**夹具只提供结构**(页面长什么样),**迁移由用例给**(点了之后
        变成什么样)。把"点确认后引用区会变"写进夹具就等于把期望混进证据 —— 那样夹具
        再也不能用来证伪代码了,而证伪正是它唯一的价值。
        """
        items = self._dom.get(sel)
        if items:
            items[0] = {**items[0], "text": text}

    def set_attrs(
        self, sel: str, attrs: Dict[str, Any], *, match_text: Optional[str] = None
    ) -> None:
        """改写某选择器下元素的属性(``None`` 值=删掉该属性),用于**由用例注入状态迁移**。

        与 ``set_text`` 同一分工:夹具只提供结构(按钮长什么样),迁移由用例给
        (点了卡片之后「确认引用」解禁)。夹具采集于"什么都没选中"那一刻,
        按钮天然是禁用的 —— 把"选中后会解禁"写进夹具就等于把期望混进证据。

        ``match_text``:只改文案含它的那些元素(一个选择器下有四颗按钮时要用)。
        """
        items = self._dom.get(sel) or []
        for i, item in enumerate(items):
            if match_text is not None and match_text not in (item.get("text") or ""):
                continue
            merged = {**(item.get("attrs") or {}), **attrs}
            items[i] = {
                **item,
                "attrs": {k: v for k, v in merged.items() if v is not None},
            }

    def evaluate(self, js, arg=None):  # noqa: D401 — 明确不支持
        raise NotImplementedError(
            "回放夹具不支持 evaluate:JS 求值没法离线重放。"
            "靠 evaluate 的路径请继续用手写假页面,或把要读的东西在采集时就存进 dom。"
        )


class _ReplayResponse:
    def __init__(self, rec: Dict[str, Any]):
        self.url = rec.get("url", "")
        self.status = rec.get("status", 200)
        self._body = rec.get("body")

    def json(self):
        return self._body


def load_scene(name: str) -> Dict[str, Any]:
    """读一个已采集的场景快照;缺文件直接报错并指明怎么采。"""
    path = FIXTURE_DIR / f"{name}.json"
    if not path.is_file():
        raise FileNotFoundError(
            f"没有场景夹具 {path}。用 scripts/capture_page_scene.py 在真号上采一份"
            f"(只读、拟人化、不点提交类按钮)。**不要手写夹具** —— 手写就会照着代码的"
            f"假设写,那正是这套东西要根治的问题。"
        )
    return json.loads(path.read_text(encoding="utf-8"))
