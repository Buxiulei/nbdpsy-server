"""已发布笔记补挂话题(追加语义)单测:纯逻辑差集 + 浏览器补话题步骤。

需求 docs `2026-08-08 server需求-已发布笔记补挂话题`。分工:

- ``plan_topic_appends`` / ``normalize_topic_names``:纯逻辑,钉死"追加去重截断"这三条
  语义(验收表的追加/上限两项);
- ``append_topics``:浏览器步骤,钉死"逐个独立成败、失败回删不留残缺、不连坐其余话题"
  (验收表的失败可诊断),用一个只支持这条流程的轻量假页面驱动,一个真浏览器动作都不发生。

判据全部来自需求验收表,断言有牙(既断值也断"不该发生的没发生":回删按了、别的话题没受
连坐、真话题实体真挂上)。
"""

import pytest

from app.browser import note_editing as ne


# ==================== 纯逻辑:追加去重截断 ====================


def test_normalize_strips_hash_dedups_preserves_order():
    """去 # / 去空白 / 丢空串 / 按首次出现去重(保序)。"""
    got = ne.normalize_topic_names(["#心理科普", " 心理科普 ", "创伤", "#创伤", "", "  "])
    assert got == ["心理科普", "创伤"]


def test_plan_appends_diff_keeps_existing_and_only_adds_missing():
    """验收「追加语义」:已有 1 个,补 4 个(其中 0 个重复)→ to_add=4,原话题保留。

    job278 场景:已上「过度寻求保证」1 个,按追加补齐其余 4 个,结果应是 5 个。
    """
    plan = ne.plan_topic_appends(
        existing=["过度寻求保证"],
        requested=["投射性认同", "焦虑型依恋", "亲密关系", "心理科普"],
    )
    assert plan["existing"] == ["过度寻求保证"]
    assert plan["to_add"] == ["投射性认同", "焦虑型依恋", "亲密关系", "心理科普"]
    assert plan["truncated"] == []
    assert plan["already"] == []
    # 补完后的总数 = 现有 + 补 = 5(原 1 个保留)
    assert len(plan["existing"]) + len(plan["to_add"]) == 5


def test_plan_appends_drops_topics_already_present():
    """请求里与现有重复的进 already、不进 to_add(不重复挂,追加语义的核心)。"""
    plan = ne.plan_topic_appends(
        existing=["复杂性创伤", "CPTSD"],
        requested=["#复杂性创伤", "创伤应激", "CPTSD"],
    )
    assert plan["to_add"] == ["创伤应激"]
    assert set(plan["already"]) == {"复杂性创伤", "CPTSD"}
    assert plan["truncated"] == []


def test_plan_appends_truncates_over_ten_total():
    """验收「上限截断」:现有 + 新增 > 10 → 截断,truncated 如实说明留了哪些。"""
    existing = [f"旧{i}" for i in range(8)]  # 已占 8 个
    requested = ["新A", "新B", "新C", "新D"]   # 只剩 2 个名额
    plan = ne.plan_topic_appends(existing=existing, requested=requested)
    assert plan["to_add"] == ["新A", "新B"]         # 先来的补上
    assert plan["truncated"] == ["新C", "新D"]       # 靠后的截掉
    assert len(plan["existing"]) + len(plan["to_add"]) == 10


def test_plan_appends_all_present_is_noop():
    """请求的全都已挂 → to_add 空、already 全量(编排层据此零点击零提交)。"""
    plan = ne.plan_topic_appends(existing=["a", "b"], requested=["a", "#b"])
    assert plan["to_add"] == []
    assert plan["truncated"] == []
    assert set(plan["already"]) == {"a", "b"}


def test_plan_appends_existing_already_full_truncates_all():
    """现有已满 10 个 → 一个都补不下,全进 truncated。"""
    existing = [f"旧{i}" for i in range(10)]
    plan = ne.plan_topic_appends(existing=existing, requested=["新A", "新B"])
    assert plan["to_add"] == []
    assert plan["truncated"] == ["新A", "新B"]


# ==================== 浏览器步骤:补话题(逐个独立成败) ====================


class _Human:
    """假拟人层:记录 click/type/press,点击直接触发落点回调(下拉命中时用)。"""

    def __init__(self, page):
        self.page = page
        self.clicks = []
        self.typed = []
        self.keys = []

    def wait(self, *_a, **_kw):
        pass

    def scroll(self, *_a, **_kw):
        pass

    def click(self, target, *, reason="", **_kw):
        self.clicks.append((reason, target))
        # 话题下拉命中时 target 是坐标 (x, y):让页面按坐标兑现"点选了哪个话题"
        if isinstance(target, tuple):
            self.page.click_topic_at(target)

    def type_text(self, _target, text, **_kw):
        self.typed.append(text)
        self.page.type_into_body(text)

    def press_key(self, key, *, reason="", **_kw):
        self.keys.append(key)


class _Keyboard:
    def __init__(self, page):
        self.page = page

    def press(self, key):
        self.page.press(key)


class _TopicPage:
    """只为补话题流程服务的假页面:正文框有内容、话题下拉可命中/不命中可配。

    话题实体以 ``#名字[话题]#`` 记进 ``self.body``(与真页面 innerText 同形态,
    ``extract_topics`` 能读)。下拉命中集 ``self.available`` 控制哪些词能点选成功。
    """

    def __init__(self, *, body="原有正文", available=None, focus_ok=True):
        self.body = body
        self.available = set(available if available is not None else [])
        self.focus_ok = focus_ok
        self.keyboard = _Keyboard(self)
        self._pending = ""       # 刚打进去、还没转成实体的 #话题(已剥掉分隔)
        self._dropdown_suppressed = False  # 本次输入 # 粘连了前一个话题实体 → 浮层不弹
        self.backspaces = 0
        self.escapes = 0

    # ---- 被 note_editing 调用的页面接口 ----

    def evaluate(self, js, arg=None):
        if "primary_matched" in js:          # _FOCUS_FORENSICS_JS(聚焦失败取证)
            return {"primary_matched": True, "contenteditable_count": 1,
                    "primary_rect": {"x": 10, "y": 200, "w": 300, "h": 120},
                    "viewport_h": 900, "active_tag": "body"}
        if "activeElement" in js:            # _BODY_FOCUS_JS
            return self.focus_ok
        if "getComputedStyle" in js:         # COLLECT_LAYERS_JS:回放话题下拉浮层
            tag = arg
            if self._dropdown_suppressed:    # # 粘连前一个话题实体:编辑器没弹联想浮层
                return {"layers": []}
            if tag in self.available:
                # 一层"像下拉"的浮层,含精确匹配那一行(几何锚放行:x 落在正文框区间)
                return {"layers": [{
                    "cls": "topic-dropdown",
                    "rect": {"x": 20.0, "y": 300.0, "width": 200.0, "height": 120.0},
                    "has_tag": True,
                    "items": [
                        {"text": f"#{tag}", "x": 60.0, "y": 320.0},
                        {"text": f"#{tag}的近似词", "x": 60.0, "y": 350.0},
                    ],
                }]}
            return {"layers": []}            # 没这个词 → 空浮层 → topic_dropdown_not_shown
        if "getBoundingClientRect" in js and "sels" in js:  # _BOX_JS(_locate)
            return {"x": 10.0, "y": 200.0, "w": 300.0, "h": 120.0, "sel": arg[0],
                    "ih": 900.0}
        if "contenteditable" in js:          # 正文只读(read_body_value)
            return self.body
        return None

    def press(self, key):
        if key == "Escape":
            self.escapes += 1
            self._pending = ""               # 关下拉,刚打的 #话题 待回删
        elif key == "Backspace":
            self.backspaces += 1

    # ---- 测试辅助:回放"打字"与"点选下拉" ----

    def type_into_body(self, text):
        # 真实编辑器(RCA 2026-08-09):# 直接粘在前一个话题实体(chip,正文里形如
        # `#x[话题]#`)边界上**不弹**联想浮层;有分隔(空格/换行)或前面是普通正文才弹。
        body_ends_with_chip = self.body.rstrip().endswith("[话题]#")
        has_separator = text[:1] in (" ", "\n", "\t")
        self._dropdown_suppressed = body_ends_with_chip and not has_separator
        self._pending = text.strip()         # 剥掉分隔,只留 #话题,未选中不算实体

    def click_topic_at(self, _xy):
        # 点选下拉命中项:把待定的 #话题 转成正文里的真话题实体
        name = self._pending.lstrip("#").strip()
        if name:
            self.body = f"{self.body} #{name}[话题]#"
        self._pending = ""


def _run(page):
    human = _Human(page)
    # to_add 由编排层算好;这里直接喂差集,测步骤本身
    return human, page


def test_append_topics_all_hit_become_real_entities():
    """全部命中:逐个点选成真实体,body 里出现 #x[话题]#,回读得到全部。"""
    page = _TopicPage(available={"投射性认同", "焦虑型依恋"})
    human, page = _run(page)
    out = ne.append_topics(page, human, ["投射性认同", "焦虑型依恋"])
    assert out["status"] == "done"
    assert out["in_editor_added"] == ["投射性认同", "焦虑型依恋"]
    assert out["failed"] == []
    assert set(ne.extract_topics(page.body)) == {"投射性认同", "焦虑型依恋"}
    # 一次回删都没发生(全命中)
    assert page.backspaces == 0 and page.escapes == 0


def test_append_topics_one_miss_does_not_collateral_others():
    """验收「失败可诊断+不连坐」:中间一个平台没有 → 它进 failed 且被回删,其余照样挂上。"""
    page = _TopicPage(available={"投射性认同", "亲密关系"})  # 「查无此词」不在集里
    human, page = _run(page)
    out = ne.append_topics(page, human, ["投射性认同", "查无此词", "亲密关系"])
    assert out["status"] == "partially_applied"
    assert out["in_editor_added"] == ["投射性认同", "亲密关系"]   # 不连坐
    assert [f["tag"] for f in out["failed"]] == ["查无此词"]
    # 失败那条带当场证据(reason 来自正向判据):空浮层 = 浮层没弹,别换词(缺陷3)
    assert out["failed"][0]["reason"] == "topic_dropdown_not_shown"
    # 失败的词被 Escape + 逐键回删,绝不留残缺文本(残缺话题会撑爆 10 上限);
    # 回删长度**含开头分隔空格**(缺陷1:` #查无此词`,只删 #tag 会留空格残留)
    assert page.escapes == 1
    assert page.backspaces == len(" #查无此词")
    # body 里只有成功的两个实体,失败的词没落进正文
    assert set(ne.extract_topics(page.body)) == {"投射性认同", "亲密关系"}


def test_append_topics_focus_failed_marks_all_without_typing():
    """聚焦不上正文框 → 全体失败、一个字都不打(正文原样未动,可整体重试)。

    缺陷2:content_box_focus_failed 不再是黑箱,每条失败带聚焦取证(选择器是否命中/
    页面有几个 contenteditable/是否滚进过视口),下一次号6播客复现一眼可定性。
    """
    page = _TopicPage(available={"任意"}, focus_ok=False)
    human, page = _run(page)
    out = ne.append_topics(page, human, ["任意", "另一个"])
    assert out["status"] == "error"
    assert out["in_editor_added"] == []
    assert {f["tag"] for f in out["failed"]} == {"任意", "另一个"}
    assert all(f["reason"] == "content_box_focus_failed" for f in out["failed"])
    assert human.typed == []                 # 一个字都没打
    assert ne.extract_topics(page.body) == []
    # 每条失败都带上聚焦取证(缺陷2 的可诊断化):区分"选择器没命中"vs"命中但焦点没进"
    for f in out["failed"]:
        assert f["primary_matched"] is True
        assert f["contenteditable_count"] == 1
        assert f["scrolled_into_view"] is True
        assert "primary_rect" in f and "viewport_h" in f


# ==================== 缺陷1:追加场景(# 粘连话题实体)专项 ====================


def test_fake_models_glued_hash_suppresses_dropdown():
    """夹具忠实回放 RCA 2026-08-09:# 紧贴前一个话题实体(无分隔)编辑器不弹联想浮层;
    补一个空格分隔就弹。这条给下面的追加场景测试装上牙——没有它那些测试是橡皮图章。"""
    from app.browser.topic_dropdown import COLLECT_LAYERS_JS

    page = _TopicPage(body="正文 #复杂性创伤[话题]#", available={"失眠"})
    # 无分隔(旧代码 f"#{tag}"):# 粘连 chip → 浮层空(追加场景选不中的根因)
    page.type_into_body("#失眠")
    assert page.evaluate(COLLECT_LAYERS_JS, "失眠") == {"layers": []}
    # 有分隔(新代码 f" #{tag}"):浮层弹出、含精确匹配
    page.type_into_body(" #失眠")
    assert page.evaluate(COLLECT_LAYERS_JS, "失眠")["layers"], "补空格分隔就该弹浮层"


def test_append_after_existing_chip_selects_with_separator():
    """验收缺陷1:正文末尾是已有话题实体(追加场景),append_topics 先补一个空格分隔,浮层
    弹出、能选中成实体;原有话题实体无损。断言 typed 带头部分隔 = 修复确实生效(夹具在无
    分隔时会抑制浮层,能选中即证明代码打了分隔)。"""
    page = _TopicPage(body="原有正文 #复杂性创伤[话题]#", available={"失眠"})
    human, page = _run(page)
    out = ne.append_topics(page, human, ["失眠"])
    assert out["status"] == "done"
    assert out["in_editor_added"] == ["失眠"]
    assert out["failed"] == []
    assert set(ne.extract_topics(page.body)) == {"复杂性创伤", "失眠"}
    assert human.typed == [" #失眠"]         # 每个话题输入前先打分隔空格


def test_append_after_chip_failure_backspaces_include_separator():
    """追加场景里某词平台没有 → 回删把开头分隔空格一起删净,不留残缺(残缺撑爆 10 上限)。"""
    page = _TopicPage(body="原有正文 #复杂性创伤[话题]#", available=set())
    human, page = _run(page)
    out = ne.append_topics(page, human, ["查无此词"])
    assert out["status"] == "error"
    assert [f["tag"] for f in out["failed"]] == ["查无此词"]
    assert out["failed"][0]["reason"] == "topic_dropdown_not_shown"
    assert page.escapes == 1
    assert page.backspaces == len(" #查无此词")   # 含开头分隔空格,删干净


def test_append_topics_empty_to_add_is_skipped():
    """差集为空(全已挂)→ skipped、零动作(编排层据此不点发布)。"""
    page = _TopicPage()
    human, page = _run(page)
    out = ne.append_topics(page, human, [])
    assert out["status"] == "skipped"
    assert out["in_editor_added"] == [] and out["failed"] == []
    assert human.clicks == [] and human.typed == []
