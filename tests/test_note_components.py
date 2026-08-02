"""笔记三组件(合集 / 引用笔记 / 关联活动)单测(不起真浏览器)。

锁的是设计第二节那些**真号实测出来的静默失败**,以及为它们加的每一道闸:

- **静默失败必须被识别**:合集被服务端静默丢弃(``success:true`` 照返)、活动按钮点了
  不翻转 —— 两者都要落 ``partially_applied`` / ``failed``,**绝不报 done**;
- **权限保全**:权限读不出 → 一次都不点;提交前权限变了 → **不点发布**;提交后权限被改
  → 告警 + 尝试改回;
- **活动重试只重试同一个活动、有上限**:绝不"换个活动重试"(换活动会取消旧的,但旧活动
  注入正文的话题不回收,反复切换话题会单调累积并真发出去);
- **绝不点撤销类按钮**:「取消关联」「创建合集」在任何路径下都不许被点到;
- **定位优先 note_id**:引用候选按 note_id 映射并用标题交叉校验,对不上就拒绝;
  可见性切换先把 note_id 翻译成平台当前标题(台账 title 会过期);
- **活动筛选的假阳性排除**:默认词表不得命中「howto穿出自我」这类穿搭活动。

patch 纪律:打在被测模块的命名空间(顶层 import 的依赖),不是源模块。
"""

import pytest

from app.browser import matrix_interact as mi
from app.browser import note_components as bnc
from app.browser import note_visibility as bnv
from app.services import browser_jobs_repo as repo
from app.services import note_components as svc


# ---------------- 测试替身:一个能被点出副作用的假编辑器 ----------------


class _El:
    """假元素:只提供被测代码真用到的能力(读文本/读 value/取矩形/子查询/被点)。"""

    def __init__(self, text="", *, on_click=None, value="", children=None, href=None,
                 on_type=None):
        self._text = text
        self.on_click = on_click
        self.on_type = on_type   # 被 type_text 输入时的副作用(他人笔记检索框用)
        self._value = value
        self._children = children or {}
        self._href = href

    def inner_text(self):
        return self._text

    def input_value(self):
        return self._value

    def is_visible(self):
        return True

    def get_attribute(self, name):
        return self._href if name == "href" else None

    def bounding_box(self):
        return {"x": 10.0, "y": 20.0, "width": 100.0, "height": 40.0}

    def query_selector(self, sel):
        hits = self._children.get(sel) or []
        return hits[0] if hits else None

    def query_selector_all(self, sel):
        return list(self._children.get(sel) or [])


class _Human:
    """假拟人层:点击直接触发元素副作用,并记录 (reason, 文案) 供断言"不该点的没点"。"""

    def __init__(self, _page=None):
        self.clicks = []
        self.typed = []

    def wait(self, *_a, **_kw):
        pass

    def scroll(self, *_a, **_kw):
        pass

    def scroll_to_element(self, _el):
        pass

    def hover(self, *_a, **_kw):
        pass

    def navigate(self, *_a, **_kw):
        pass

    def click(self, target, *, reason="", **_kw):
        text = target.inner_text() if hasattr(target, "inner_text") else ""
        self.clicks.append((reason, text))
        if hasattr(target, "on_click") and target.on_click:
            target.on_click()

    def type_text(self, target, text, **_kw):
        self.typed.append(text)
        if hasattr(target, "on_type") and target.on_type:
            target.on_type(text)

    @property
    def texts(self):
        return [t for _r, t in self.clicks]


class Editor:
    """假编辑器页模型:三组件状态 + 点击副作用 + 接口响应回放。

    每个可配的"坏行为"都对应设计里一条实测结论:``drop_collection_on_submit``=服务端静默
    丢弃合集;``silent_activity_clicks``=活动首次点击静默失效;``permission_after_*``=
    权限被改动。
    """

    def __init__(
        self,
        *,
        permission="公开可见",
        collection=None,
        collections=(("c1", "咨询师简介"),),
        activities=(("43561", "身边的心理学", "心理科普活动"),),
        linked_activity=None,
        notes=(("n-quote", "心理咨询师-徐瑞恒"), ("n-other", "另一篇")),
        body="原有正文",
        title="目标笔记",
        drop_collection_on_submit=False,
        silent_activity_clicks=0,
        permission_before_submit=None,
        permission_after_submit=None,
        quote_card_titles=None,
        other_notes=(),
    ):
        self.permission = permission
        self.collection = collection
        self.collections = list(collections)
        self.activities = [
            {"id": i, "name": n, "desc": d, "linked": n == linked_activity}
            for i, n, d in activities
        ]
        self.notes = list(notes)
        self.quote_card_titles = (
            list(quote_card_titles) if quote_card_titles is not None
            else [t for _i, t in self.notes]
        )
        self.body = body
        self.title = title
        self.quote_text = "引用笔记"
        self.popover_open = False
        self.modal_open = False
        # 「他人笔记」tab:{note_id: 卡片文案};切过去后要检索才出候选(真号实测)
        self.other_notes = dict(other_notes)
        self.other_tab = False
        self.other_query = ""

        self.drop_collection_on_submit = drop_collection_on_submit
        self.silent_activity_clicks = silent_activity_clicks
        self.permission_before_submit = permission_before_submit
        self.permission_after_submit = permission_after_submit
        self.submitted = 0
        self.page = _FakePage(self)

    # ---- 接口响应回放(页面自己发,我们只被动读) ----

    def load(self):
        """一次页面加载:活动列表随页面返回(设计 2.9)。"""
        self.popover_open = False
        self.modal_open = False
        self.page.emit(
            "https://creator.xiaohongshu.com/api/galaxy/v2/creator/activity_center/list",
            {"data": {"list": [
                {"id": a["id"], "name": a["name"], "desc": a["desc"]}
                for a in self.activities
            ]}},
        )

    def _emit_collections(self):
        self.page.emit(
            "https://edith.xiaohongshu.com/api/sns/v1/note/collection/pc/list_v2",
            {"data": {"collection_info_list": [
                {"id": cid, "name": name, "desc": "", "note_num": 10}
                for cid, name in self.collections
            ]}},
        )

    def _emit_posted(self):
        self.page.emit(
            "https://creator.xiaohongshu.com/api/galaxy/v2/creator/note/user/posted?tab=1",
            {"data": {"notes": [
                {"id": nid, "display_title": t} for nid, t in self.notes
            ]}},
        )

    def submit(self):
        """点发布:服务端处理 —— 这里回放"合集被静默丢弃"与"权限被改"两种实测坏行为。"""
        self.submitted += 1
        if self.drop_collection_on_submit:
            self.collection = None
        if self.permission_after_submit is not None:
            self.permission = self.permission_after_submit
        self.page.emit(
            "https://edith.xiaohongshu.com/web_api/sns/capa/postgw/note/update",
            {"result": 0, "success": True, "msg": ""},
        )

    # ---- 点击副作用 ----

    def _open_popover(self):
        self.popover_open = True
        self._emit_collections()

    def _choose_collection(self, name):
        self.collection = name
        self.popover_open = False

    def _open_modal(self):
        self.modal_open = True
        self._emit_posted()

    def _confirm_quote(self):
        if self._selected_quote is None:
            return
        self.quote_text = f"引用了 {self._selected_quote}"
        self.modal_open = False

    def _cancel_quote(self):
        self.modal_open = False

    def _set_other_query(self, text):
        self.other_query = text
        # 真号实测:输入即触发 GET creator/search/others/note?note_link=<输入>,
        # 响应 data 带 note_id / display_title。逐字输入时中间态也会发,这里如实回放。
        hit = self.other_notes.get(text)
        self.page.emit(
            f"https://creator.xiaohongshu.com/api/galaxy/v2/creator/search/others/note?note_link={text}",
            {"data": {"note_id": text, "display_title": ""} if hit else None},
        )

    def _switch_other(self):
        self.other_tab = True   # 真号实测:切 tab 是纯前端,零网络请求

    def _switch_mine(self):
        self.other_tab = False

    _selected_quote = None

    def _select_quote(self, title):
        self._selected_quote = title

    def _click_activity(self, name):
        if self.silent_activity_clicks > 0:
            self.silent_activity_clicks -= 1
            return  # 静默失效:无 toast、零网络请求、按钮不翻转(设计 2.7④)
        for a in self.activities:
            a["linked"] = a["name"] == name  # 互斥单选

    # ---- 选择器 → 元素 ----

    def select(self, sel):
        if sel == bnc._COLLECTION_BUTTON:
            label = self.collection or "选择合集"
            return [_El(label, on_click=self._open_popover)]
        if sel == bnc._COLLECTION_CHOSEN:
            return [_El(self.collection)] if self.collection else []
        if sel == bnc._COLLECTION_POPOVER_ITEM:
            if not self.popover_open:
                return []
            items = [
                _El(name, on_click=(lambda n=name: self._choose_collection(n)))
                for _cid, name in self.collections
            ]
            # 弹层里还有一条「创建合集」:点到它会真的建一个新合集,断言里要求一次都没点
            items.append(_El("创建合集", on_click=self._boom_create))
            return items
        if sel == bnc._QUOTE_CONTAINER:
            return [_El(self.quote_text, on_click=self._open_modal)]
        if sel == bnc._QUOTE_NOTE_CARD:
            if not self.modal_open:
                return []
            if self.other_tab:
                # 他人笔记:检索前空,检索后按 note_id 命中才出卡
                hit = self.other_notes.get(self.other_query)
                return [_El(hit, on_click=(lambda x=hit: self._select_quote(x)))] if hit else []
            return [
                _El(f"{t} 封面", on_click=(lambda x=t: self._select_quote(x)))
                for t in self.quote_card_titles
            ]
        if sel == bnc._QUOTE_LINK_INPUT:
            if not (self.modal_open and self.other_tab):
                return []
            return [_El("", on_type=self._set_other_query)]
        if sel == bnc._QUOTE_MODAL:
            # 弹窗本体:_close_quote_modal 靠它判断"还开着吗"
            return [_El("选择笔记")] if self.modal_open else []
        if sel in (f"{bnc._QUOTE_MODAL} button", ".d-modal button", "button"):
            if not self.modal_open:
                return []
            # 真弹窗里「确认引用」旁边就是「取消」——收尾只能点它(Escape 关不掉)
            return [
                _El("我的笔记", on_click=self._switch_mine),
                _El("他人笔记", on_click=self._switch_other),
                _El("确认引用", on_click=self._confirm_quote),
                _El("取消", on_click=self._cancel_quote),
            ]
        if sel == bnc._ACTIVITY_CARD:
            cards = []
            for a in self.activities:
                text = bnc._ACTIVITY_LINKED_TEXT if a["linked"] else bnc._ACTIVITY_UNLINKED_TEXT
                cards.append(_El(a["name"], children={
                    bnc._ACTIVITY_NAME: [_El(a["name"])],
                    bnc._ACTIVITY_ACTION: [
                        _El(text, on_click=(lambda n=a["name"]: self._click_activity(n)))
                    ],
                }))
            return cards
        if sel == bnc._PERMISSION_DESC:
            return [_El(self.permission)] if self.permission else []
        if sel == bnc._TITLE_INPUT:
            return [_El(value=self.title)]
        return []

    @staticmethod
    def _boom_create():
        raise AssertionError("点到了「创建合集」——会凭空建一个新合集,绝对禁止")


class _FakePage:
    """假 page:选择器走 Editor,evaluate 按脚本特征分派,response 监听真回放。"""

    def __init__(self, editor):
        self.editor = editor
        self._listeners = []
        self.body_text = "内容设置 权限设置"

    # response 监听(与真 playwright 同接口)
    def on(self, event, fn):
        if event == "response":
            self._listeners.append(fn)

    def remove_listener(self, event, fn):
        if event == "response" and fn in self._listeners:
            self._listeners.remove(fn)

    def emit(self, url, body):
        for fn in list(self._listeners):
            fn(_FakeResponse(url, body))

    # 页面读写
    def inner_text(self, _sel):
        return self.body_text

    def query_selector(self, sel):
        hits = self.editor.select(sel)
        return hits[0] if hits else None

    def query_selector_all(self, sel):
        return self.editor.select(sel)

    def wait_for_timeout(self, _ms):
        pass

    def evaluate(self, js, _arg=None):
        if "contenteditable" in js:
            return self.editor.body
        if "elementFromPoint" in js:
            return "XHS-PUBLISH-BTN"
        if "xhs-publish-btn" in js:
            return {"x": 100.0, "y": 500.0, "w": 120.0, "h": 40.0, "ih": 900.0}
        if "innerWidth" in js:
            return {"iw": 1920, "ih": 900, "dpr": 1}
        return None


class _FakeResponse:
    def __init__(self, url, body):
        self.url = url
        self._body = body

    def json(self):
        return self._body


@pytest.fixture
def wired(monkeypatch):
    """把浏览器层的外部依赖换成假的:导航触发页面加载、拟人层可观测、点发布走假提交。"""
    humans = []

    def fake_human(page):
        human = _Human(page)
        humans.append(human)
        return human

    monkeypatch.setattr(bnc, "SyncHumanActions", fake_human)
    # 轮询窗口压到刚够跑一两跳:逻辑不变,只缩时间(假 page 的 wait_for_timeout 是 no-op)
    for name, value in (
        ("_EDITOR_READY_TIMEOUT_S", 0.5),
        ("_POPOVER_TIMEOUT_S", 0.4),
        ("_MODAL_TIMEOUT_S", 0.4),
        ("_CATALOG_TIMEOUT_S", 0.4),
        ("_ACTIVITY_FLIP_TIMEOUT_S", 0.4),
        ("_SUBMIT_TIMEOUT_S", 0.4),
    ):
        monkeypatch.setattr(bnc, name, value)
    return humans


def _wire(monkeypatch, editor, humans, *, publish=True):
    """把 ``_goto_creator`` 接到编辑器的页面加载,把点发布接到编辑器的提交。"""
    monkeypatch.setattr(bnc, "_goto_creator", lambda _page, _url: editor.load())
    if publish:
        monkeypatch.setattr(
            bnc, "click_publish",
            lambda page, human: (humans[-1].clicks.append(("发布", "")), editor.submit()),
        )
    else:
        def boom(*_a, **_kw):
            raise AssertionError("本用例不应点发布")

        monkeypatch.setattr(bnc, "click_publish", boom)


def _run(editor, **components):
    return bnc.set_note_components(editor.page, 1, "n-target", **components)


# ---------------- 全部生效:done ----------------


def test_all_three_applied_is_done(monkeypatch, wired):
    """三项都设上且提交后回读确认 → done,并如实报出正文被追加的话题。"""
    editor = Editor()
    _wire(monkeypatch, editor, wired)
    # 关联活动会往正文追加话题(追加不覆盖,且话题名 ≠ 活动名)
    original_click = editor._click_activity

    def click_with_topic(name):
        original_click(name)
        if any(a["linked"] for a in editor.activities):
            editor.body = f"{editor.body} #心理学小课堂[话题]#"

    editor._click_activity = click_with_topic

    result = _run(
        editor, collection_id="c1", quoted_note_id="n-quote", activity_id="43561"
    )

    assert result["status"] == "done"
    assert result["applied"] == {"collection": True, "quote": True, "activity": True}
    assert result["failed"] == []
    assert result["submitted"] is True
    assert result["permission_preserved"] is True
    assert result["topics_injected"] == ["心理学小课堂"]
    assert result["body_appended"] == "#心理学小课堂[话题]#"
    assert "创建合集" not in wired[0].texts


# ---------------- 静默失败:合集被服务端丢弃 ----------------


def test_collection_silently_dropped_is_partially_applied(monkeypatch, wired):
    """合集在编辑器里选上了、提交也 success:true,但回读没绑上 → partially_applied。

    这是设计 2.6 的真号实测:私密笔记的 noteCollectionBind 被服务端**静默丢弃**,
    零 toast 零错误码。**绝不能**因为"没报错"就报 done。
    """
    editor = Editor(drop_collection_on_submit=True)
    _wire(monkeypatch, editor, wired)

    result = _run(editor, collection_id="c1", activity_id="43561")

    assert result["status"] == "partially_applied"
    assert result["applied"] == {"collection": False, "activity": True}
    assert [f["component"] for f in result["failed"]] == ["collection"]
    assert "error" not in result  # 还有一项成了,不是整体失败


# ---------------- 静默失败:活动按钮不翻转 ----------------


def test_activity_first_click_silent_then_retried_same_activity(monkeypatch, wired):
    """首次点击静默失效 → 重试**同一个活动**并成功;绝不点到别的活动。"""
    editor = Editor(
        activities=(
            ("43561", "身边的心理学", "心理科普"),
            ("999", "明日方舟创作应援", "游戏"),
        ),
        silent_activity_clicks=1,
    )
    _wire(monkeypatch, editor, wired)

    result = _run(editor, activity_id="43561")

    assert result["status"] == "done"
    assert result["components"]["activity"]["clicks"] == 2
    reasons = [r for r, _t in wired[0].clicks]
    assert all("明日方舟" not in r for r in reasons), "绝不换活动重试(旧话题不回收)"


def test_activity_never_flips_hits_cap_and_does_not_submit(monkeypatch, wired):
    """按钮始终不翻转 → 点击次数封顶、整体 failed 带 error,且**一次发布都不点**。

    编辑器里一项都没设上时提交毫无意义,而每次提交都是一次全量覆盖,故不点。
    """
    editor = Editor(silent_activity_clicks=99)
    _wire(monkeypatch, editor, wired, publish=False)

    result = _run(editor, activity_id="43561")

    assert result["status"] == "failed"
    assert "note_components_all_failed" in result["error"]
    assert result["submitted"] is False
    assert result["applied"] == {"activity": None}
    clicks = [r for r, _t in wired[0].clicks if "关联活动" in r]
    assert len(clicks) == bnc._ACTIVITY_CLICK_ATTEMPTS
    assert "activity_not_linked" in result["failed"][0]["reason"]


def test_already_linked_activity_never_clicks_cancel(monkeypatch, wired):
    """活动本来就关联着 → skipped,**绝不点「取消关联」**。"""
    editor = Editor(linked_activity="身边的心理学")
    _wire(monkeypatch, editor, wired)

    result = _run(editor, activity_id="43561")

    assert result["components"]["activity"]["status"] == "skipped"
    assert bnc._ACTIVITY_LINKED_TEXT not in wired[0].texts
    assert result["status"] == "done"  # 回读时它确实是关联态


def test_activity_action_text_unexpected_clicks_nothing(monkeypatch, wired):
    """按钮文案既不是「关联」也不是「取消关联」→ 状态未知,一次都不点。"""
    editor = Editor()
    _wire(monkeypatch, editor, wired, publish=False)
    monkeypatch.setattr(
        bnc, "read_activity_action_text", lambda _p, _n: "已结束"
    )

    result = _run(editor, activity_id="43561")

    assert "activity_action_unexpected" in result["failed"][0]["reason"]
    assert wired[0].clicks == []


# ---------------- 权限保全 ----------------


def test_permission_unreadable_aborts_before_touching_anything(monkeypatch, wired):
    """权限档位读不出 → 抛错中止,一个组件都不设、发布不点(提交是全量覆盖语义)。"""
    editor = Editor(permission=None)
    _wire(monkeypatch, editor, wired, publish=False)

    with pytest.raises(bnc.NoteComponentsError) as exc:
        _run(editor, collection_id="c1")

    assert exc.value.reason.startswith("permission_unreadable")
    assert wired[0].clicks == []


def test_permission_changed_before_submit_aborts_without_publishing(monkeypatch, wired):
    """设完组件、点发布前复读发现权限变了 → **不点发布**,抛错中止(笔记原样未动)。"""
    editor = Editor()
    _wire(monkeypatch, editor, wired, publish=False)
    real_read = bnc.read_permission_label
    calls = {"n": 0}

    def read_twice(page):
        calls["n"] += 1
        # 第 1 次是编辑前留底,第 2 次是点发布前的复读 —— 让它"变了"
        return "公开可见" if calls["n"] == 1 else "仅自己可见"

    monkeypatch.setattr(bnc, "read_permission_label", read_twice)
    try:
        with pytest.raises(bnc.NoteComponentsError) as exc:
            _run(editor, collection_id="c1")
    finally:
        monkeypatch.setattr(bnc, "read_permission_label", real_read)

    assert exc.value.reason.startswith("permission_changed_before_submit")
    assert editor.submitted == 0


def test_permission_changed_after_submit_alarms_and_restores(monkeypatch, wired):
    """提交后回读发现权限被改 → permission_preserved=False + 自动改回(走权限弹窗)。"""
    editor = Editor(permission_after_submit="仅自己可见")
    _wire(monkeypatch, editor, wired)
    restored = {}

    def fake_set_visibility(_page, account_id, note_id, title, target):
        restored.update(
            {"account_id": account_id, "note_id": note_id, "title": title, "target": target}
        )
        return {"status": "done", "permission_code": target}

    monkeypatch.setattr(bnv, "set_note_visibility", fake_set_visibility)

    result = _run(editor, collection_id="c1")

    assert result["permission_preserved"] is False
    assert result["permission_restored"]["ok"] is True
    # 改回的是**原档位**(公开可见 → 0),且按编辑器里读到的平台当前标题定位
    assert restored["target"] == 0
    assert restored["title"] == "目标笔记"
    assert restored["note_id"] == "n-target"


# ---------------- 引用笔记:按 note_id 定位 + 标题交叉校验 ----------------


def test_quote_locates_by_note_id_not_by_order_guess(monkeypatch, wired):
    """引用按 note_id 在候选响应里定位,并用平台标题交叉校验对应那张卡。"""
    editor = Editor(
        notes=(("n-a", "第一篇"), ("n-quote", "心理咨询师-徐瑞恒"), ("n-c", "第三篇"))
    )
    _wire(monkeypatch, editor, wired)

    result = _run(editor, quoted_note_id="n-quote")

    assert result["status"] == "done"
    assert result["components"]["quote"]["title"] == "心理咨询师-徐瑞恒"
    assert "心理咨询师-徐瑞恒" in editor.quote_text


def test_quote_card_title_mismatch_refuses_to_guess(monkeypatch, wired):
    """卡片顺序与接口不一致(标题对不上)→ 拒绝引用,**不点「确认引用」**。"""
    editor = Editor(
        notes=(("n-a", "第一篇"), ("n-quote", "心理咨询师-徐瑞恒")),
        quote_card_titles=["完全不同的甲", "完全不同的乙"],
    )
    _wire(monkeypatch, editor, wired, publish=False)

    result = _run(editor, quoted_note_id="n-quote")

    assert "quote_card_title_mismatch" in result["failed"][0]["reason"]
    assert "确认引用" not in wired[0].texts


def test_quote_note_not_in_candidates(monkeypatch, wired):
    """要引用的笔记不在候选列表里 → 明确报错,不退而求其次选别的。"""
    editor = Editor(notes=(("n-a", "第一篇"),))
    _wire(monkeypatch, editor, wired, publish=False)

    result = _run(editor, quoted_note_id="n-missing")

    assert "quoted_note_not_in_candidates" in result["failed"][0]["reason"]
    assert "确认引用" not in wired[0].texts


# ---------------- 引用弹窗必须收尾(2026-08-02 发布连续超时事故) ----------------
#
# 事故经过:引用目标不在候选里 → 组件判 error 记「不阻断发布」→ **但弹窗留在页面上**。
# 弹窗是覆盖层,发布按钮点不到;而 step7 的全页兜底把弹窗里那个 disabled 的「确认引用」
# 当成发布按钮点了,什么都没发生,最后只报一句「发布超时(30秒),未检测到成功标志」。
# 好好生活号 job 132/133/134 连续三次全栽在这,运营换图片体积/换活动/压字数试了三轮,
# **没有一个变量与真正的原因有关**。
#
# 所以这组用例锁的是:**不管从哪条路径退出,弹窗都必须关掉**。


@pytest.mark.parametrize(
    "case,notes,card_titles,quoted_id",
    [
        # 目标不在候选里(正是线上那次)
        ("not_in_candidates", (("n-a", "第一篇"),), None, "n-missing"),
        # 卡片顺序与接口对不上,拒绝盲选
        ("title_mismatch", (("n-a", "第一篇"), ("n-quote", "徐瑞恒")),
         ["完全不同的甲", "完全不同的乙"], "n-quote"),
    ],
)
def test_quote_modal_closed_on_every_failure_path(
    monkeypatch, wired, case, notes, card_titles, quoted_id
):
    """引用失败后弹窗必须已关闭 —— 否则它会盖住发布按钮,让整篇笔记发不出去。"""
    editor = Editor(notes=notes, quote_card_titles=card_titles)
    _wire(monkeypatch, editor, wired, publish=False)

    result = _run(editor, quoted_note_id=quoted_id)

    assert result["failed"], f"{case} 本应判失败"
    assert editor.modal_open is False, f"{case}: 引用弹窗没关,发布按钮会被它盖住"
    assert "取消" in wired[0].texts, f"{case}: 没点「取消」收尾"


def test_quote_success_does_not_click_cancel(monkeypatch, wired):
    """成功路径上「确认引用」自己会关弹窗 —— 收尾是幂等的,**不能再点「取消」**。

    点了会怎样:取消掉刚设好的引用,组件白设。所以收尾必须先看弹窗还开不开着。
    """
    editor = Editor(notes=(("n-a", "第一篇"), ("n-quote", "心理咨询师-徐瑞恒")))
    _wire(monkeypatch, editor, wired)

    result = _run(editor, quoted_note_id="n-quote")

    assert result["status"] == "done"
    assert "心理咨询师-徐瑞恒" in editor.quote_text  # 引用还在,没被取消掉
    assert "取消" not in wired[0].texts
    assert editor.modal_open is False


# ---------------- 合集:id 映射 + 弹层条目 ----------------


def test_collection_id_not_in_catalog_refuses(monkeypatch, wired):
    """给的 collection_id 不在该号合集列表里 → 拒绝(绝不按文案瞎点一个)。"""
    editor = Editor()
    _wire(monkeypatch, editor, wired, publish=False)

    result = _run(editor, collection_id="不存在的id")

    assert "collection_not_found" in result["failed"][0]["reason"]
    assert wired[0].texts == ["选择合集"]  # 只点了入口,一个条目都没点


# ---------------- 发布按钮:落点复核 ----------------


def test_click_publish_requires_element_from_point_match(monkeypatch):
    """质心落点上不是 ``XHS-PUBLISH-BTN`` → 抛错**不点**(那一带上方就是正文与组件区)。"""
    editor = Editor()
    page = editor.page
    human = _Human(page)
    monkeypatch.setattr(bnc, "_red_centroid", lambda _p, _r: (160.0, 520.0))
    monkeypatch.setattr(bnc, "_element_tag_at", lambda _p, _x, _y: "DIV")

    with pytest.raises(bnc.NoteComponentsError) as exc:
        bnc.click_publish(page, human)

    assert exc.value.reason.startswith("publish_point_mismatch")
    assert human.clicks == []


def test_click_publish_refuses_when_no_red_pixels(monkeypatch):
    """红像素找不到 → 抛错,**不按比例猜坐标**(组件设置会改变页面高度顶动按钮)。"""
    editor = Editor()
    human = _Human(editor.page)
    monkeypatch.setattr(bnc, "_red_centroid", lambda _p, _r: None)

    with pytest.raises(bnc.NoteComponentsError) as exc:
        bnc.click_publish(editor.page, human)

    assert exc.value.reason.startswith("publish_button_not_located")
    assert human.clicks == []


def test_click_publish_happy_path(monkeypatch):
    """质心复核通过 → 拟人点坐标(不是点元素句柄,closed shadow 里根本没有句柄)。"""
    editor = Editor()
    human = _Human(editor.page)
    monkeypatch.setattr(bnc, "_red_centroid", lambda _p, _r: (160.0, 520.0))

    bnc.click_publish(editor.page, human)

    assert len(human.clicks) == 1 and "发布" in human.clicks[0][0]


# ---------------- 纯函数:响应解析 / 活动筛选 / 正文差 ----------------


def test_parse_collections_reads_platform_keys():
    body = {"data": {"collection_info_list": [
        {"id": "6a69e9e316fb000000000001", "name": "咨询师简介", "desc": "全员北大", "note_num": 10},
        {"name": "没有 id 的丢弃"},
    ]}}
    assert bnc.parse_collections(body) == [{
        "id": "6a69e9e316fb000000000001", "name": "咨询师简介",
        "desc": "全员北大", "note_num": 10,
    }]
    assert bnc.parse_collections(None) == []


def test_parse_activities_coerces_id_to_string():
    """id 统一转字符串:提交载荷里的 bizId 实测是字符串,两边比对必须同型。"""
    got = bnc.parse_activities({"data": {"list": [
        {"id": 43561, "name": "身边的心理学", "desc": "心理科普"},
        {"name": "没有 id 的丢弃"},
    ]}})
    assert got == [{"id": "43561", "name": "身边的心理学", "desc": "心理科普"}]


def test_activity_filter_excludes_the_measured_false_positive():
    """默认词表不得命中「howto穿出自我」(实测的假阳性:它其实是穿搭活动)。

    这条正是"只查 name + 用过宽的词"会踩的坑,故默认词表里刻意没有「自我」「成长」。
    """
    activities = [
        {"id": "1", "name": "身边的心理学", "desc": "聊聊情绪与生活"},
        {"id": "2", "name": "howto穿出自我", "desc": "穿搭灵感大赏,穿出自我风格"},
        {"id": "3", "name": "夏日好物", "desc": "记录你的焦虑缓解好物"},  # 简介命中
    ]
    got = bnc.filter_activities(activities)
    assert [a["id"] for a in got] == ["1", "3"]
    assert got[0]["matched_keywords"] == ["心理", "情绪"]
    assert got[1]["matched_keywords"] == ["焦虑"]  # name 没提,靠**简介**联合匹配到


def test_activity_filter_empty_keywords_means_no_filter():
    activities = [{"id": "1", "name": "穿搭", "desc": ""}]
    assert len(bnc.filter_activities(activities, ())) == 1


def test_body_append_and_topic_extraction():
    """关联活动是把话题**追加**到正文末尾(设计 2.7①:追加不覆盖)。"""
    before = "#身边的心理学[话题]#"
    after = "#身边的心理学[话题]# #明日方舟[话题]#"
    assert bnc.appended_part(before, after) == "#明日方舟[话题]#"
    assert bnc.extract_topics(after) == ["身边的心理学", "明日方舟"]
    # 不是纯追加(正文被改写)时如实交出整段,不假装"只加了一点"
    assert bnc.appended_part("原文", "完全换了") == "完全换了"


# ---------------- 服务层契约 ----------------


@pytest.mark.parametrize(
    "payload, mark",
    [
        ({"collection_id": "c1"}, "缺 note_id"),
        ({"note_id": "n1"}, "no_component_requested"),
        ({"note_id": "n1", "collection_id": "", "activity_id": None}, "no_component_requested"),
    ],
)
async def test_execute_rejects_bad_payload_without_browser(monkeypatch, payload, mark):
    """入参不合法一律收敛成 {"error"},**不起浏览器**(取 cookie 一被调就失败)。"""

    async def boom(_account_id):
        raise AssertionError("入参不合法时不应取 cookie / 起浏览器")

    monkeypatch.setattr(svc, "load_account_cookies", boom)

    result = await svc.execute(1, payload)

    assert mark in result["error"]


async def test_execute_wraps_browser_error(monkeypatch):
    """浏览器层抛 NoteComponentsError → {"error": reason},不抛出(台账才不会悬挂)。"""

    async def fake_load(_account_id):
        return [{"name": "a", "value": "b"}]

    def boom(*_a, **_kw):
        raise bnc.NoteComponentsError("permission_unreadable: 读不到权限档位")

    monkeypatch.setattr(svc, "load_account_cookies", fake_load)
    monkeypatch.setattr(svc, "_apply_sync", boom)

    result = await svc.execute(1, {"note_id": "n1", "collection_id": "c1"})

    assert result == {"error": "permission_unreadable: 读不到权限档位"}


async def test_execute_returns_error_without_cookies(monkeypatch):
    async def fake_load(_account_id):
        return []

    monkeypatch.setattr(svc, "load_account_cookies", fake_load)

    result = await svc.execute(1, {"note_id": "n1", "activity_id": "1"})

    assert "error" in result


async def test_execute_passes_partial_result_through(monkeypatch):
    """部分生效的结果原样透出(不含 error 键 → 台账 done,result_status 才是真相)。"""

    async def fake_load(_account_id):
        return [{"name": "a", "value": "b"}]

    monkeypatch.setattr(svc, "load_account_cookies", fake_load)
    monkeypatch.setattr(
        svc, "_apply_sync",
        lambda *_a, **_kw: {"status": "partially_applied", "applied": {"collection": False}},
    )

    result = await svc.execute(1, {"note_id": "n1", "collection_id": "c1"})

    assert result["status"] == "partially_applied" and "error" not in result


async def test_read_catalog_requires_note_id(monkeypatch):
    """目录查询没有 note_id 就没有落脚点 → 直接给可读错误,不起浏览器。"""

    async def boom(_account_id):
        raise AssertionError("不应起浏览器")

    monkeypatch.setattr(svc, "load_account_cookies", boom)

    assert "error" in await svc.read_catalog(1, "")


# ---------------- 台账纪律 ----------------


def test_note_components_is_not_idempotent_kind():
    """僵死绝不自动重跑:提交是全量覆盖,且关联活动会把话题**再追加一遍**。"""
    assert "note_components" not in repo._IDEMPOTENT_KINDS


def test_account_worker_resolves_note_components_execute():
    from app import account_worker

    assert account_worker._resolve_execute("note_components") is not None


# ---------------- 既有链路改成"定位优先 note_id" ----------------


def test_visibility_prefers_platform_title_over_stale_ledger_title(monkeypatch):
    """可见性切换:按 note_id 现拉平台标题定位,台账里的过期标题只作兜底。

    实测:平台显示「粤语咨询师-黄安麟」而台账里是「心理咨询师-黄安麟」——按台账标题
    精确匹配必然 note_not_locatable。
    """
    monkeypatch.setattr(
        bnv, "fetch_posted_notes",
        lambda *_a, **_kw: [{"id": "n1", "display_title": "粤语咨询师-黄安麟"}],
    )

    got = bnv._resolve_locating_title(None, 1, "n1", "心理咨询师-黄安麟")

    assert got == "粤语咨询师-黄安麟"


def test_visibility_falls_back_to_caller_title(monkeypatch):
    """平台列表里没这条 note_id → 回退调用方给的标题(不直接判失败)。"""
    monkeypatch.setattr(
        bnv, "fetch_posted_notes", lambda *_a, **_kw: [{"id": "other", "display_title": "别的"}]
    )

    assert bnv._resolve_locating_title(None, 1, "n1", "兜底标题") == "兜底标题"


def test_visibility_without_id_or_title_is_not_locatable(monkeypatch):
    """平台查不到 note_id 且调用方也没给 title → note_not_locatable,绝不猜。"""
    monkeypatch.setattr(bnv, "fetch_posted_notes", lambda *_a, **_kw: [{"id": "other"}])

    with pytest.raises(bnv.NoteVisibilityError) as exc:
        bnv._resolve_locating_title(None, 1, "n1", "")

    assert exc.value.reason.startswith("note_not_locatable")


def test_comment_card_matching_prefers_note_id():
    """评论定位:主页卡片链接 href 里带 note_id,按 id 命中优先于标题。"""
    card = _El("完全不同的标题", children={
        "a": [_El(href="/user/profile/u1/6a4ce556?xsec_token=x")]
    })
    assert mi._card_matches_note_id(card, "6a4ce556")
    assert not mi._card_matches_note_id(card, "6a4ce557")
    assert not mi._card_matches_note_id(card, "")


# ---------------- 跨账号引用:走「他人笔记」tab ----------------
#
# 业务上真实存在:每个账号背后是不同的运营、各有 KPI,所以咨询师推介笔记**只能引用自己
# 账号的**;唯一的例外是"小助手联系方式"那篇——它是二维码有违规风险,集中放在主号,
# 于是别的号引用它必然是**跨账号**的。
#
# 2026-08-02 真号只读观察确定(不是猜的):切「他人笔记」是纯前端零请求;输入框写着
# 「请粘贴笔记链接 http://...」,但**直接填 note_id 就能检索到**(不需要 xsec_token,
# 那玩意儿是账号绑定且短效的,拼完整 URL 反而更脆);检索前候选区空,检索后才出卡。


_QR_NOTE = "68d50838000000000e00c3b6"   # 主号那篇小助手联系方式(**空标题**)


def test_falls_back_to_other_tab_when_not_own_note(monkeypatch, wired):
    """目标不在本账号笔记里 → 自动切「他人笔记」按 note_id 检索并引用成功。"""
    editor = Editor(
        notes=(("n-a", "第一篇"),),                      # 本账号候选里没有目标
        other_notes=((_QR_NOTE, "小助手联系方式 封面"),),  # 他人笔记里有
    )
    _wire(monkeypatch, editor, wired)

    result = _run(editor, quoted_note_id=_QR_NOTE)

    assert result["status"] == "done"
    quote = result["components"]["quote"]
    assert quote["status"] == "done" and quote["via"] == "other_notes_tab"
    assert "小助手联系方式" in editor.quote_text
    assert "他人笔记" in wired[0].texts        # 确实切过 tab
    assert _QR_NOTE in wired[0].typed          # 确实按 note_id 检索


def test_other_tab_refuses_when_search_misses(monkeypatch, wired):
    """检索不到那篇 → 报错,**绝不点第一张凑数**。

    判据走**接口返回的 note_id**,不是"页面上有几张卡":逐字输入时页面会对中间态
    (note_link=68d508 这种半截 id)也发一次检索,只看"有没有结果"会认错。
    """
    editor = Editor(notes=(("n-a", "第一篇"),), other_notes=())
    _wire(monkeypatch, editor, wired, publish=False)

    result = _run(editor, quoted_note_id=_QR_NOTE)

    reason = result["failed"][0]["reason"]
    assert "quote_other_id_mismatch" in reason
    # 两条路的原因都要带上,否则排查时以为"本账号里也没有"这件事没发生过
    assert "quoted_note_not_in_candidates" in reason
    assert "确认引用" not in wired[0].texts


def test_no_fallback_on_uncertain_failures(monkeypatch, wired):
    """卡片顺序与接口对不上属于"状态不确定" → **不降级**,不拿不确定去赌另一条路。"""
    editor = Editor(
        notes=(("n-a", "第一篇"), ("n-quote", "徐瑞恒")),
        quote_card_titles=["完全不同的甲", "完全不同的乙"],
        other_notes=(("n-quote", "不该被用到"),),
    )
    _wire(monkeypatch, editor, wired, publish=False)

    result = _run(editor, quoted_note_id="n-quote")

    assert "quote_card_title_mismatch" in result["failed"][0]["reason"]
    assert "他人笔记" not in wired[0].texts   # 一次都没切过去


def test_other_tab_still_closes_modal_on_failure(monkeypatch, wired):
    """走他人笔记失败时,弹窗同样必须关掉 —— 否则又会盖住发布按钮(见上面那组用例)。"""
    editor = Editor(notes=(("n-a", "第一篇"),), other_notes=())
    _wire(monkeypatch, editor, wired, publish=False)

    _run(editor, quoted_note_id=_QR_NOTE)

    assert editor.modal_open is False
    assert "取消" in wired[0].texts


def test_other_tab_rejects_wrong_note_from_search(monkeypatch, wired):
    """检索接口返回的是**另一篇**的 note_id → 拒绝引用。

    这是逐字输入的真实副作用:输到一半时页面就会拿半截 id 去查,可能查出别的笔记。
    只要最终响应的 note_id 与目标不符,就一篇都不引用。
    """
    editor = Editor(notes=(("n-a", "第一篇"),), other_notes=(("别的笔记id", "别的笔记 封面"),))
    _wire(monkeypatch, editor, wired, publish=False)

    result = _run(editor, quoted_note_id=_QR_NOTE)

    assert "quote_other_id_mismatch" in result["failed"][0]["reason"]
    assert "确认引用" not in wired[0].texts
