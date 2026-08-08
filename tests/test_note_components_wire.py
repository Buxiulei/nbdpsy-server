"""编辑已发布笔记的**编排**单测(T6):步骤序列 / 弃提交 / 回读判据 / 台账回写接线。

设计 docs/design/2026-08-03-note-editing-design.md 第四节流程①-⑪ / 4.4 / 3.2 / 3.3。

分工:T4(`test_note_editing_text.py`)与 T5(`test_note_editing_images.py`)各自钉住
"那一步在页面上怎么做对";本文件只钉**编排**——谁先谁后、谁失败了之后还做不做、结果怎么
汇总。所以这里把 T4/T5 的步骤函数与所有真碰 DOM 的读写全换成可编程 stub,一个真浏览器
动作都不发生。

锁死的三条红线(每条都对应一次真金白银的事故风险):

- **全流程只有一次 `click_publish`**:提交是全量覆盖语义,提交次数就是风险次数。每个用例
  都数一遍 `calls.count("publish")`;
- **弃提交路径一次发布都不点**(设计 4.4):破坏性编辑步失败时编辑器里躺着残缺态,提交
  出去就是把残缺真发布;不提交则编辑器态不落库(附录 C / E4 实证),笔记原样未动;
- **纯组件请求行为不变**:不带编辑字段时不走任何编辑步,结果里也不多出编辑相关的键
  —— 老调用方(nbdpsy-skill)读的是同一份 JSON。

patch 纪律:打在被测模块 `app.browser.note_components` 的命名空间(它顶层 import 了
T4/T5 的函数),不是源模块 —— 打源模块的话被测模块早就绑好的引用不会变。
"""

import pytest

from app.browser import note_components as bnc
from app.services import note_components as svc

_NOTE = "6a6f18c4"

# 各破坏性编辑步的"成功"返回形状,与 T4/T5 的真实返回一致(键名照抄它们的 docstring)
_DEFAULT_STEPS = {
    "image_remove": {"status": "done", "removed": 1, "count_before": 4, "count_after": 3},
    "image_add": {"status": "done", "added": 2, "count_before": 3, "count_after": 5},
    "title": {"status": "done", "title_before": "旧标题", "title_read_back": "新标题"},
    "content": {
        "status": "done",
        "body_before": "旧正文 #身边的心理学[话题]#",
        "topics_dropped": ["身边的心理学"],
        "body_read_back": "新正文",
    },
}


class _Page:
    """假 page:本文件把每一个真读 DOM 的调用都换成了 stub,page 只需能挂/摘响应监听。

    唯一的例外是 ``evaluate``:原创声明的**提交后回读**直接在编排层读开关 checked
    (没有独立的读函数可 stub),故这里按 ``original_checked`` 如实吐回三态。
    """

    def __init__(self, original_checked=None):
        self.original_checked = original_checked

    def on(self, *_a, **_kw):
        pass

    def remove_listener(self, *_a, **_kw):
        pass

    def wait_for_timeout(self, *_a, **_kw):
        pass

    def evaluate(self, _js, _arg=None):
        return self.original_checked


class _Human:
    """假拟人层:编排层只用它 wait/scroll,真正的点与输入都在被 stub 掉的步骤函数里。"""

    def wait(self, *_a, **_kw):
        pass

    def scroll(self, *_a, **_kw):
        pass

    def hover(self, *_a, **_kw):
        pass

    def click(self, *_a, **_kw):
        raise AssertionError("编排层不该自己点东西——点击都在被 stub 的步骤函数里")


def _script(values):
    """按调用次序吐值,吐完之后一直给最后一个(免得每个用例都要凑够调用次数)。"""
    box = list(values)

    def _next(*_a, **_kw):
        return box.pop(0) if len(box) > 1 else box[0]

    return _next


def _wire(
    monkeypatch,
    calls,
    *,
    steps=None,
    components=None,
    gate_error=None,
    image_counts=(4, 5),
    title_read="新标题",
    body_read="新正文 #心理学小课堂[话题]#",
    body_texts=("旧正文", "新正文 #心理学小课堂[话题]#"),
    activity_linked=True,
    permission="公开可见",
):
    """把编排层之外的一切换成可编程 stub,并把调用序列记进 ``calls``。

    ``steps`` 覆盖单个编辑步的返回值(键同 ``_DEFAULT_STEPS``),``gate_error`` 非 None
    表示图片闸不过,``image_counts`` 是 ``count_images`` 依次吐的值(留底 / 提交后回读)。
    """
    steps = {**_DEFAULT_STEPS, **(steps or {})}
    components = components or {}

    monkeypatch.setattr(bnc, "SyncHumanActions", lambda _page: _Human())
    monkeypatch.setattr(
        bnc, "open_update_page", lambda *_a, **_kw: calls.append("open")
    )
    monkeypatch.setattr(bnc, "read_permission_label", lambda _page: permission)
    monkeypatch.setattr(bnc, "read_note_title", lambda _page: "平台标题")
    monkeypatch.setattr(bnc, "read_body_text", _script(body_texts))
    monkeypatch.setattr(bnc, "count_images", _script(image_counts))
    monkeypatch.setattr(
        bnc, "image_gate",
        lambda _page, expected: ({"status": "error", "reason": gate_error} if gate_error
                                 else {"status": "ok", "count": expected}),
    )

    def _step(key, arg_name):
        def run(_page, _human, value):
            calls.append(key)
            calls.append((arg_name, value))
            return steps[key]
        return run

    monkeypatch.setattr(bnc, "remove_images_step", _step("image_remove", "indexes"))
    monkeypatch.setattr(bnc, "add_images_step", _step("image_add", "paths"))
    monkeypatch.setattr(bnc, "apply_title_edit", _step("title", "title"))
    monkeypatch.setattr(bnc, "apply_content_edit", _step("content", "content"))

    def fake_apply_components(_page, _human, _responses, *, collection_id=None,
                              collection_name=None, remove_collection_id=None,
                              remove_collection_name=None, quoted_note_id=None,
                              activity_id=None):
        calls.append("components")
        return {
            key: components.get(key, {"status": "done", "name": "身边的心理学"})
            for key, value in (("collection_remove", remove_collection_id),
                               ("collection", collection_id),
                               ("quote", quoted_note_id),
                               ("activity", activity_id))
            if value
        }

    monkeypatch.setattr(bnc, "apply_components", fake_apply_components)
    monkeypatch.setattr(
        bnc, "click_publish", lambda *_a, **_kw: calls.append("publish")
    )
    monkeypatch.setattr(bnc, "_wait_submitted", lambda *_a, **_kw: {"success": True})

    # 提交后回读:标题/正文/活动各自的只读取证
    monkeypatch.setattr(bnc, "read_title_value", lambda _page: title_read)
    monkeypatch.setattr(bnc, "read_body_value", lambda _page: body_read)
    monkeypatch.setattr(bnc, "_activity_linked", lambda _page, _name: activity_linked)
    monkeypatch.setattr(bnc, "read_collection_label", lambda _page: "身边的心理学")
    monkeypatch.setattr(bnc, "read_quote_text", lambda _page: "引用 @某某 的笔记")
    return calls


def _run(page=None, **kwargs):
    return bnc.set_note_components(page or _Page(), 1, _NOTE, **kwargs)


_FULL_EDIT = {
    "title": "新标题",
    "content": "新正文",
    "add_images": ["/tmp/a.png", "/tmp/b.png"],
    "remove_image_indexes": [3, 1],
    "expected_image_count": 4,
}


# ---------------- 步骤序列(设计 4.2) ----------------


def test_edit_steps_run_in_designed_order(monkeypatch):
    """删图 → 加图 → 标题 → 正文 → 组件 → 发布,一步不许换位。

    顺序不是审美:①图片最可能失败(证据最少),放最前让弃提交尽早发生;②正文必须在活动
    之前 —— 活动把话题追加到正文末尾,反过来 ``Ctrl+A`` 会把刚注入的话题一并清掉。
    """
    calls = _wire(monkeypatch, [])

    result = _run(activity_id="43561", **_FULL_EDIT)

    assert [c for c in calls if isinstance(c, str)] == [
        "open", "image_remove", "image_add", "title", "content",
        "components", "publish", "open",   # 末尾那次 open 是提交后重进页面回读
    ]
    assert calls.count("publish") == 1
    # 参数原样直达各步(下标不在编排层排序 —— 降序是 T5 内部的事)
    assert ("indexes", [3, 1]) in calls
    assert ("paths", ["/tmp/a.png", "/tmp/b.png"]) in calls
    assert ("title", "新标题") in calls and ("content", "新正文") in calls
    assert result["aborted_before_submit"] is False


# ---------------- 弃提交:闸不过 / 任一破坏性步失败(设计 4.4) ----------------


def test_image_gate_failure_aborts_before_any_click(monkeypatch):
    """图数闸不过 → 后续编辑步、组件步、发布**零调用**,笔记原样未动。

    设计流程③原写的是"闸不过→图片落 error,文本/组件照常走";以 4.4 为准收严成整单弃提交:
    expected 对不上说明调用方对这篇的认知整体过期,替他提交剩下一半意图与静默丢改动同病。
    """
    calls = _wire(monkeypatch, [], gate_error="image_count_mismatch: 页面实数 6 ≠ expected 4")

    result = _run(activity_id="43561", **_FULL_EDIT)

    assert [c for c in calls if isinstance(c, str)] == ["open"]   # 只进了页面,什么都没做
    assert "publish" not in calls
    assert result["status"] == "failed"
    assert result["submitted"] is False
    assert result["aborted_before_submit"] is True
    assert "image_count_mismatch" in result["error"]
    # 请求的每一项都没提交没回读 → applied 全 None
    assert set(result["applied"]) == {
        "image_remove", "image_add", "title", "content", "activity"
    }
    assert all(v is None for v in result["applied"].values())
    # 因前序失败压根没执行的项要能与"执行了但失败"区分开
    reasons = {f["component"]: f["reason"] for f in result["failed"]}
    assert "image_count_mismatch" in reasons["image_remove"]
    assert "image_count_mismatch" in reasons["image_add"]
    for key in ("title", "content", "activity"):
        assert "skipped_due_to_abort" in reasons[key]
    # 已留底的部分如实带上
    assert result["images_before"] == 4 and result["images_after"] is None


@pytest.mark.parametrize(
    "failing, done_before",
    [
        ("image_remove", []),
        ("title", ["image_remove", "image_add"]),
        ("content", ["image_remove", "image_add", "title"]),
    ],
)
def test_any_destructive_step_failure_aborts_submit(monkeypatch, failing, done_before):
    """破坏性步任一 error → 立刻停手、绝不点发布(残缺态提交出去不可逆)。"""
    calls = _wire(
        monkeypatch, [],
        steps={failing: {"status": "error", "reason": f"{failing}_boom: 这一步没成",
                         "topics_dropped": ["身边的心理学"]}},
    )

    result = _run(activity_id="43561", **_FULL_EDIT)

    assert [c for c in calls if isinstance(c, str)] == ["open", *done_before, failing]
    assert "publish" not in calls and "components" not in calls
    assert result["aborted_before_submit"] is True and result["submitted"] is False
    assert result["status"] == "failed" and f"{failing}_boom" in result["error"]
    assert all(v is None for v in result["applied"].values())
    reasons = {f["component"]: f["reason"] for f in result["failed"]}
    assert f"{failing}_boom" in reasons[failing]
    assert "skipped_due_to_abort" in reasons["activity"]


def test_step_exception_also_aborts_instead_of_escaping(monkeypatch):
    """步骤函数抛异常也收敛成弃提交:异常穿出去就丢了「笔记原样未动、可安全重试」这条信息。"""
    calls = _wire(monkeypatch, [])

    def boom(*_a, **_kw):
        raise RuntimeError("句柄脱离 DOM")

    monkeypatch.setattr(bnc, "apply_title_edit", boom)

    result = _run(title="新标题")

    assert "publish" not in calls
    assert result["aborted_before_submit"] is True
    assert "title_exception" in result["error"]


# ---------------- 全部成功:一次提交 + 回读判据(设计 3.2) ----------------


def test_all_done_submits_once_and_verifies_each_criterion(monkeypatch):
    """全 done → 只提交一次;标题全等 / 正文前缀 / 图数等式吃**实删实增**。"""
    calls = _wire(
        monkeypatch, [],
        # 留底 4 张,删 1 加 2 → 回读该是 5 张
        image_counts=(4, 5),
        title_read="新标题",
        # 活动把话题追加在新正文末尾:全等必然假阴性,判据只能是前缀
        body_read="新正文 #心理学小课堂[话题]#",
    )

    result = _run(activity_id="43561", **_FULL_EDIT)

    assert calls.count("publish") == 1
    assert result["status"] == "done" and result["submitted"] is True
    assert result["applied"] == {
        "activity": True, "image_remove": True, "image_add": True,
        "title": True, "content": True,
    }
    assert result["failed"] == []
    assert result["images_before"] == 4 and result["images_after"] == 5
    assert result["topics_dropped"] == ["身边的心理学"]
    # 回读真值随结果带出(服务层台账回写只认它)
    assert result["read_back"] == {"title": "新标题", "content": "新正文 #心理学小课堂[话题]#"}
    # 活动追加了什么,拿**替换后**的正文做差 —— 拿旧正文做差会把新正文里自带的话题
    # 全算成"活动注入的",那是假报
    assert result["topics_injected"] == ["心理学小课堂"]
    assert result["permission_preserved"] is True


def test_readback_mismatch_is_false_not_done(monkeypatch):
    """回读对不上一律判 False:这条产品线的失败是静默的,"没报错"从来不是凭据。"""
    _wire(
        monkeypatch, [],
        image_counts=(4, 4),                 # 删了 1 加了 2 却还是 4 张 → 等式不成立
        title_read="平台改写过的标题",        # 与目标不全等
        body_read="完全不是我们写的正文",      # 不以目标正文开头
    )

    result = _run(**_FULL_EDIT)

    assert result["status"] == "failed"
    assert result["applied"] == {
        "image_remove": False, "image_add": False, "title": False, "content": False,
    }
    # 弃提交是另一种终态:这次是真提交了、只是没生效,调用方**不能**盲目重试
    assert result["aborted_before_submit"] is False and result["submitted"] is True
    # False 态兜底文案 = "确认没生效"(与 None 态"状态未知"处置不同,运营 §五-1)
    reasons = {f["component"]: f["reason"] for f in result["failed"]}
    assert "确认没生效" in reasons["title"]


def test_unreadable_readback_is_none_not_false(monkeypatch):
    """读不出 → None(未确认),绝不当成生效:``title=""`` 清空路径尤其踩得到。"""
    _wire(monkeypatch, [], image_counts=(4, None), title_read=None, body_read=None)

    result = _run(**_FULL_EDIT)

    assert result["applied"] == {
        "image_remove": None, "image_add": None, "title": None, "content": None,
    }
    assert result["read_back"] == {"title": None, "content": None}
    # None 态兜底文案 = "状态未知,先核对再决定"(绝不误导成"没生效")
    reasons = {f["component"]: f["reason"] for f in result["failed"]}
    assert "状态未知" in reasons["title"] and "别盲目重跑" in reasons["title"]


# ---------------- 纯编辑 / 纯组件 ----------------


def test_pure_edit_request_submits_without_components(monkeypatch):
    """没有任何组件的纯编辑请求照样提交 —— "至少一项成才提交"要认编辑步。"""
    calls = _wire(monkeypatch, [], body_texts=("旧正文", "新正文"), body_read="新正文")

    result = _run(title="新标题", content="新正文")

    assert calls.count("publish") == 1
    assert result["status"] == "done"
    assert result["applied"] == {"title": True, "content": True}
    # 没请求图片操作就不带图数键(也没跑过清点)
    assert "images_before" not in result


def test_pure_component_request_is_untouched(monkeypatch):
    """纯组件请求:不走任何编辑步,结果形状与接线前一致(只多一个 aborted_before_submit)。"""
    calls = _wire(monkeypatch, [])

    result = _run(collection_id="c1", activity_id="43561")

    assert [c for c in calls if isinstance(c, str)] == [
        "open", "components", "publish", "open"
    ]
    assert result["applied"] == {"collection": True, "activity": True}
    for key in ("topics_dropped", "images_before", "images_after", "read_back"):
        assert key not in result, f"纯组件请求不该多出 {key}"
    assert result["aborted_before_submit"] is False


def test_pure_component_all_failed_still_skips_publish(monkeypatch):
    """编辑器内一项都没设上仍然不点发布 —— 接线不许把这条老纪律冲掉。"""
    calls = _wire(
        monkeypatch, [],
        components={"collection": {"status": "error", "reason": "collection_not_applied"}},
    )

    result = _run(collection_id="c1")

    assert "publish" not in calls
    assert result["submitted"] is False and result["status"] == "failed"
    assert result["aborted_before_submit"] is False   # 不是弃提交,是"没东西可提交"


# ---------------- 混合请求 ----------------


def test_mixed_request_partially_applied(monkeypatch):
    """标题成了、活动没成 → partially_applied,failed 里只有活动。"""
    _wire(monkeypatch, [], activity_linked=False, body_texts=("旧正文", "旧正文"))

    result = _run(title="新标题", activity_id="43561")

    assert result["status"] == "partially_applied"
    assert result["applied"] == {"activity": False, "title": True}
    assert [f["component"] for f in result["failed"]] == ["activity"]


# ---------------- 补录原创声明的编排(运营 2026-08-08 来文) ----------------


def _wire_original(monkeypatch, calls, outcome):
    """把原创声明步换成可编程 stub,并把**它收到的 kwargs** 记进 ``calls``。

    记 kwargs 是这组用例的要害:运营特意问过"编辑页补声明是不是走同一段协议弹窗逻辑",
    答案必须由测试钉住,不能靠人读代码保证。
    """
    def fake_apply(_page, _human, **kw):
        calls.append(("original_declaration", kw))
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(bnc, "apply_original_declaration", fake_apply)


def test_original_declaration_reuses_the_publish_chain_function(monkeypatch):
    """补声明调的必须是**发布链那一个** ``apply_original_declaration``,且带协议弹窗链。

    这是运营来文里点名要确认的事:08-07 那个「拟人随机偏移 40% 概率撞上《原创声明须知》
    超链接」的修复在这个函数**内部**,编辑链只要共用它,修复就自动覆盖。要是哪天有人
    在编辑链里另写一份协议弹窗逻辑,这条会红。
    """
    calls = []
    _wire(monkeypatch, calls, body_texts=("旧正文", "旧正文"))
    _wire_original(monkeypatch, calls, {"status": "done", "observed": {"via": "consent_modal"}})

    result = _run(_Page(original_checked=True), set_original_declaration=True)

    hit = [kw for key, kw in [c for c in calls if isinstance(c, tuple)]
           if key == "original_declaration"]
    assert len(hit) == 1, f"补声明步应恰好跑一次: {calls}"
    assert hit[0] == {"handle_consent_modal": True}, (
        f"必须带 handle_consent_modal=True 走协议弹窗链,实收 {hit[0]}"
    )
    assert result["applied"] == {"original_declaration": True}
    assert result["status"] == "done"


def test_original_declaration_runs_after_components_and_before_publish(monkeypatch):
    """次序与发布链一致:三组件 → 原创声明 → 发布,且全流程仍只有一次提交。"""
    calls = []
    _wire(monkeypatch, calls, body_texts=("旧正文", "旧正文"))
    _wire_original(monkeypatch, calls, {"status": "done"})

    _run(_Page(original_checked=True), collection_id="c1", set_original_declaration=True)

    flat = [c[0] if isinstance(c, tuple) else c for c in calls]
    assert flat.index("components") < flat.index("original_declaration")
    assert flat.index("original_declaration") < flat.index("publish")
    assert flat.count("publish") == 1


def test_original_declaration_only_and_skipped_never_submits(monkeypatch):
    """只请求补声明、且本就是开态 → **一次发布都不点**,零改动不值得付一次全量覆盖提交。

    运营要拿它对 49 篇批量重跑,每篇白提交一次就是几十次真发布。
    """
    calls = []
    _wire(monkeypatch, calls, body_texts=("旧正文", "旧正文"))
    _wire_original(monkeypatch, calls, {"status": "skipped", "observed": "already_on"})

    result = _run(set_original_declaration=True)

    assert "publish" not in [c[0] if isinstance(c, tuple) else c for c in calls]
    assert result["submitted"] is False
    assert result["status"] == "done"
    assert result["applied"] == {"original_declaration": True}
    assert result["components"]["original_declaration"]["status"] == "skipped"


def test_skipped_declaration_still_submits_when_something_else_changed(monkeypatch):
    """本就是开态、但同一单里还挂了合集 → 照常提交(零改动豁免只对"整单零改动"生效)。"""
    calls = []
    _wire(monkeypatch, calls, body_texts=("旧正文", "旧正文"))
    _wire_original(monkeypatch, calls, {"status": "skipped", "observed": "already_on"})

    result = _run(_Page(original_checked=True),
                  collection_id="c1", set_original_declaration=True)

    assert [c[0] if isinstance(c, tuple) else c for c in calls].count("publish") == 1
    assert result["submitted"] is True
    assert result["applied"] == {"collection": True, "original_declaration": True}


def test_original_declaration_failure_does_not_block_other_components(monkeypatch):
    """补声明失败**不阻断**其余组件(与三组件同款:告警不阻断)。"""
    calls = []
    _wire(monkeypatch, calls, body_texts=("旧正文", "旧正文"))
    _wire_original(monkeypatch, calls, {
        "status": "error", "reason": "original_consent_not_ticked: 点了但回读仍未勾",
        "observed": {"consent_ticked": False},
    })

    result = _run(_Page(original_checked=False),
                  collection_id="c1", set_original_declaration=True)

    assert result["submitted"] is True, "合集是真改动,该提交还得提交"
    assert result["applied"] == {"collection": True, "original_declaration": False}
    assert result["status"] == "partially_applied"
    reason = next(f["reason"] for f in result["failed"]
                  if f["component"] == "original_declaration")
    assert reason.startswith("original_consent_not_ticked:"), reason


def test_original_declaration_only_failure_is_whole_job_failed(monkeypatch):
    """本单只有补声明这一件事,它失败 = 整单失败(没有"其余"可言),且不点发布。"""
    calls = []
    _wire(monkeypatch, calls, body_texts=("旧正文", "旧正文"))
    _wire_original(monkeypatch, calls, {
        "status": "error", "reason": "original_confirm_never_enabled: 勾了同意但按钮没解禁",
        "observed": {"confirm_enabled": False},
    })

    result = _run(set_original_declaration=True)

    assert "publish" not in [c[0] if isinstance(c, tuple) else c for c in calls]
    assert result["submitted"] is False
    assert result["status"] == "failed"
    assert "note_components_all_failed" in result["error"]
    assert result["components"]["original_declaration"]["observed"] == {
        "confirm_enabled": False}


def test_original_declaration_exception_is_caught_not_escaped(monkeypatch):
    """补声明抛异常也只落 error,绝不穿出去 —— 穿出去会丢掉其余组件的结果。"""
    calls = []
    _wire(monkeypatch, calls, body_texts=("旧正文", "旧正文"))
    _wire_original(monkeypatch, calls, RuntimeError("弹窗炸了"))

    result = _run(_Page(original_checked=False),
                  collection_id="c1", set_original_declaration=True)

    assert result["components"]["original_declaration"]["status"] == "error"
    assert "弹窗炸了" in result["components"]["original_declaration"]["reason"]
    assert result["applied"]["collection"] is True


@pytest.mark.parametrize(
    "checked, expect",
    [(True, True), (False, False), (None, None)],
)
def test_original_declaration_readback_is_three_state(monkeypatch, checked, expect):
    """提交后回读开关 checked 三态如实:true / false / 读不到=null(不乐观当成功)。"""
    calls = []
    _wire(monkeypatch, calls, body_texts=("旧正文", "旧正文"))
    _wire_original(monkeypatch, calls, {"status": "done"})

    result = _run(_Page(original_checked=checked), set_original_declaration=True)

    assert result["applied"]["original_declaration"] is expect


def test_original_declaration_not_executed_when_edit_step_aborts(monkeypatch):
    """前序破坏性编辑步失败弃提交时,补声明步如实记「因前序失败未执行」——它确实没跑。"""
    calls = []
    _wire(monkeypatch, calls, gate_error="图数对不上", body_texts=("旧正文", "旧正文"))
    _wire_original(monkeypatch, calls, {"status": "done"})

    result = _run(set_original_declaration=True, **_FULL_EDIT)

    assert result["aborted_before_submit"] is True
    assert "original_declaration" not in [
        c[0] for c in calls if isinstance(c, tuple)], "弃提交路径不该跑补声明"
    assert result["components"]["original_declaration"]["reason"] == bnc._SKIPPED_REASON


def test_pure_component_request_has_no_declaration_key(monkeypatch):
    """没请求补声明时,结果里**不许**多出 original_declaration 键(老调用方读同一份 JSON)。"""
    calls = []
    _wire(monkeypatch, calls, body_texts=("旧正文", "旧正文"))
    _wire_original(monkeypatch, calls, AssertionError("没请求就不该跑补声明"))

    result = _run(_Page(original_checked=False), collection_id="c1")

    assert "original_declaration" not in result["applied"]
    assert "original_declaration" not in result["components"]


# ---------------- 服务层:台账回写接线(设计 3.3) ----------------


class _FakeSession:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_a):
        return False


def _wire_service(monkeypatch, browser_result, written):
    """服务层接线用的替身:不起浏览器、不碰库,只看 write_back_ledger 被怎么调。"""

    async def fake_load(_account_id):
        return [{"name": "a", "value": "b"}]

    class _FakeClient:
        def __init__(self, *_a, **_kw):
            self.page = object()

        def start(self):
            return {"success": True}

        def stop(self):
            pass

    async def fake_write_back(_session, account_id, note_id, applied, read_back):
        written.append((account_id, note_id, applied, read_back))
        return True

    monkeypatch.setattr(svc, "load_account_cookies", fake_load)
    monkeypatch.setattr(svc, "SyncClient", _FakeClient)
    monkeypatch.setattr(svc, "set_note_components", lambda *_a, **_kw: browser_result)
    monkeypatch.setattr(svc, "get_session", lambda: _FakeSession())
    monkeypatch.setattr(svc, "write_back_ledger", fake_write_back)


async def test_ledger_written_when_text_applied(monkeypatch):
    """applied.title=True → 调 write_back_ledger(用**回读真值**),结果带 ledger_synced。"""
    written = []
    _wire_service(monkeypatch, {
        "status": "done",
        "applied": {"title": True, "content": None},
        "read_back": {"title": "平台改写过的标题", "content": None},
    }, written)

    result = await svc.execute(7, {"note_id": _NOTE, "title": "新标题"})

    assert result["ledger_synced"] is True
    assert len(written) == 1
    account_id, note_id, applied, read_back = written[0]
    assert (account_id, note_id) == (7, _NOTE)
    assert applied["title"] is True
    assert read_back == {"title": "平台改写过的标题", "content": None}


@pytest.mark.parametrize("applied", [
    {"title": False, "content": None},          # 回读确认没生效
    {"image_add": True},                        # 只改了图 —— 台账不存图列表,没什么可写
    {"collection": True},                       # 纯组件
])
async def test_ledger_not_touched_without_verified_text(monkeypatch, applied):
    """没有回读确认生效的标题/正文 → 不开 session、不调回写、**不带 ledger_synced 键**。"""
    written = []
    _wire_service(monkeypatch, {"status": "done", "applied": applied}, written)

    result = await svc.execute(7, {"note_id": _NOTE, "collection_id": "c1"})

    assert written == []
    assert "ledger_synced" not in result
