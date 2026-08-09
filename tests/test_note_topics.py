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
        self.waits = []          # [(min_s, max_s, context)] —— 轮询节奏是否走拟人层的证据

    def wait(self, min_s=None, max_s=None, *, context="", **_kw):
        # 签名对齐 SyncHumanActions.wait(min_s, max_s, *, context)
        self.waits.append((min_s, max_s, context))

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

    ``RECT`` 是正文框几何(子类可整体换一套,如 ``_BottomBarPage``),**下拉浮层的坐标一律
    从它推**:真页面的联想浮层挂在正文框的光标上、必然与正文栏同列,``is_anchored_to_editor``
    正是照这条判的。写死一套浮层坐标的话,子类一换正文框位置浮层就被几何锚拒掉。
    """

    RECT = {"x": 10.0, "y": 200.0, "w": 300.0, "h": 120.0, "ih": 900.0}

    def __init__(self, *, body="原有正文", available=None, focus_ok=True):
        self.body = body
        self.available = set(available if available is not None else [])
        self.focus_ok = focus_ok
        self.keyboard = _Keyboard(self)
        self._pending = ""       # 刚打进去、还没转成实体的 #话题(已剥掉分隔)
        self._dropdown_suppressed = False  # 本次输入 # 粘连了前一个话题实体 → 浮层不弹
        self.backspaces = 0
        self.escapes = 0
        self.forensics_points = []   # 取证 JS 收到的落点参数(每次失败取证一条)

    # ---- 被 note_editing 调用的页面接口 ----

    def evaluate(self, js, arg=None):
        if "primary_matched" in js:          # _FOCUS_FORENSICS_JS(聚焦失败取证)
            self.forensics_points.append(arg)
            return {"primary_matched": True, "contenteditable_count": 1,
                    "primary_rect": {k: self.RECT[k] for k in ("x", "y", "w", "h")},
                    "viewport_h": self.RECT["ih"], "active_tag": "body",
                    # 落点反查:真 JS 拿 elementFromPoint 反查,没给落点就留空
                    "point_element_chain": None if arg is None else "div.foot < body",
                    "point_inside_editor": None if arg is None else False}
        if "activeElement" in js:            # _BODY_FOCUS_JS
            return self.focus_ok
        if "getComputedStyle" in js:         # COLLECT_LAYERS_JS:回放话题下拉浮层
            tag = arg
            if self._dropdown_suppressed:    # # 粘连前一个话题实体:编辑器没弹联想浮层
                return {"layers": []}
            if tag in self.available:
                # 一层"像下拉"的浮层,含精确匹配那一行;坐标挂在正文框那一列(几何锚放行)、
                # 纵向贴着框内上方(正文框可能探出视口,挂框底会把浮层放到视口外去)
                lx = self.RECT["x"] + 10.0
                ly = self.RECT["y"] + 40.0
                return {"layers": [{
                    "cls": "topic-dropdown",
                    "rect": {"x": lx, "y": ly, "width": 200.0, "height": 120.0},
                    "has_tag": True,
                    "items": [
                        {"text": f"#{tag}", "x": lx + 40.0, "y": ly + 20.0},
                        {"text": f"#{tag}的近似词", "x": lx + 40.0, "y": ly + 50.0},
                    ],
                }]}
            return {"layers": []}            # 没这个词 → 空浮层 → topic_dropdown_not_shown
        if "getBoundingClientRect" in js and "sels" in js:  # _BOX_JS(_locate)
            return {**self.RECT, "sel": arg[0]}
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
        # 「# 粘在前一个话题实体(chip)边界上就不弹浮层」是**已被真号复验推翻**的旧假设
        # (加空格后 tail 变 `[话题]# #失眠`、浮层照样不弹,RCA 2026-08-09)。这里保留它
        # **只作探针**:代码若不打分隔空格,下面追加场景的测试会红——即它现在的唯一职责是
        # 给「分隔空格这条卫生措施还在」装牙,不再声称这是生产真因。
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


class _LateDropdownPage(_TopicPage):
    """话题联想浮层**晚到**的假页面(RCA 2026-08-09 真因候选 a 的忠实回放)。

    真号实证:同一个词在第 2 位补得上、在第 5 位补不上 —— 位置败不是词败,说明浮层不是
    不来,是来得比那次定长等待(1.5~2.5s 单次快照)晚。这里把"晚"参数化:打完一个词之后
    头 ``late_ticks`` 次采集看到的是**空页面**(与生产回执 ``layers=[]`` 同形),之后浮层
    才挂上、内容与 ``_TopicPage`` 命中时完全一致。``late_ticks=None`` = 浮层永远不来
    (轮询把预算等满、超时那条路径)。

    计数每打一个新词归零 —— 逐个话题各等各的,不能让上一个词的等待白送给下一个。
    """

    def __init__(self, *, late_ticks, **kw):
        super().__init__(**kw)
        self.late_ticks = late_ticks
        self.collect_calls = 0        # 当前这个词已被采集过几次

    def type_into_body(self, text):
        super().type_into_body(text)
        self.collect_calls = 0

    def evaluate(self, js, arg=None):
        # 判别条件与父类同款(按 JS 正文识别 COLLECT_LAYERS_JS),不靠外部传进来的键名 ——
        # 真页面只认得 JS,替身也只该认 JS
        if "getComputedStyle" in js:
            self.collect_calls += 1
            if self.late_ticks is None or self.collect_calls <= self.late_ticks:
                return {"layers": []}     # 浮层还没挂上(或永远不挂)
        return super().evaluate(js, arg)


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


# ============ 聚焦落点:底部固定操作栏吞点击(RCA 2026-08-09 号6播客几何) ============


class _BottomBarPage(_TopicPage):
    """底部固定操作栏吞点击的假页面,几何照抄号6播客 note 6a75fa99 的失败取证。

    真号三次复现的当场数据:正文框 ``{x:442, y:592, w:632, h:260}``、``viewport_h=794``、
    选择器命中且滚进过视口,焦点仍留在 body。框底 852 探出视口,矩形死中心落点 (758, 722)
    离视口底只剩 72px —— 编辑更新页底部那条固定操作栏(更新/取消)压在最下面,落在它上面的
    点击进不了正文框。

    ``bar_h`` = 那条栏的高度。**真值没量过**(取证只给了"落点离底 72px 且被吞"),所以测试
    按合理区间扫一遍,不挑一个数字碰运气:``bar_h`` 只要 ≥72 旧死中心就必被吞,而新落点
    ``0.33`` 带内偏上离视口底 135px,``bar_h`` 到 130 仍安全。

    ``_Human.click`` 把每一次坐标点击都转给页面的 ``click_topic_at``,所以这里覆写它当
    「一次点击落到 (x, y)」的总入口:先按几何判焦点落不落,再交回父类原来的话题点选语义。
    """

    RECT = {"x": 442.0, "y": 592.0, "w": 632.0, "h": 260.0, "ih": 794.0}

    def __init__(self, *, bar_h, **kw):
        # 焦点起始不在编辑器:落不落进去完全由点击几何决定
        super().__init__(focus_ok=False, **kw)
        self.bar_h = bar_h
        self.clicked_points = []

    # ---- 几何判据:被点击与被取证共用一套,假件不能自说自话 ----

    def _swallowed(self, y):
        """落点是否压在底部固定操作栏上(视口底 bar_h 以内)。"""
        return y >= self.RECT["ih"] - self.bar_h

    def _in_editor(self, x, y):
        r = self.RECT
        return r["x"] <= x <= r["x"] + r["w"] and r["y"] <= y <= r["y"] + r["h"]

    def _point_probe(self, pt):
        """落点反查(真页面走 elementFromPoint,这里按同一套几何回放)。"""
        if pt is None:
            return {"point_element_chain": None, "point_inside_editor": None}
        x, y = pt
        if self._swallowed(y):
            return {"point_element_chain": "div.footer-actions < div.edit-footer < body",
                    "point_inside_editor": False}
        if self._in_editor(x, y):
            return {"point_element_chain": "p < div.tiptap.ProseMirror < div.editor < body",
                    "point_inside_editor": True}
        return {"point_element_chain": "div.page < body", "point_inside_editor": False}

    # ---- 页面接口(只把落点反查换成按几何算的,其余照父类) ----

    def evaluate(self, js, arg=None):
        got = super().evaluate(js, arg)
        if "primary_matched" in js:                          # _FOCUS_FORENSICS_JS
            got.update(self._point_probe(arg))
        return got

    def click_topic_at(self, xy):
        x, y = xy
        self.clicked_points.append((x, y))
        if self._in_editor(x, y) and not self._swallowed(y):
            self.focus_ok = True                             # 点进正文框可见区 → 焦点落进去
        super().click_topic_at(xy)


@pytest.mark.parametrize("bar_h", [72, 80, 100, 110, 130])
def test_focus_click_lands_above_bottom_action_bar(bar_h, monkeypatch):
    """核心修复:正文框探出视口时,聚焦点击落在可见区带内偏上,避开底部固定操作栏。

    同一几何先用**旧的死中心策略**跑一遍作对照(monkeypatch 回 ``_click_point``):它必须
    失败,否则这个假件没牙、后半段是橡皮图章。断言全是行为级的(补没补上话题),不断言
    坐标数字 —— 换个百分比只要点得进编辑器就该照样绿。
    """
    old_page = _BottomBarPage(bar_h=bar_h, available={"失眠"})
    monkeypatch.setattr(ne, "_focus_click_point", ne._click_point)
    out_old = ne.append_topics(old_page, _Human(old_page), ["失眠"])
    assert out_old["status"] == "error", "死中心落点被底栏吞掉,这条几何下旧策略必须失败"
    assert all(f["reason"] == "content_box_focus_failed" for f in out_old["failed"])
    assert ne.extract_topics(old_page.body) == []

    monkeypatch.undo()
    page = _BottomBarPage(bar_h=bar_h, available={"失眠"})
    out = ne.append_topics(page, _Human(page), ["失眠"])
    assert out["status"] == "done"
    assert out["in_editor_added"] == ["失眠"]
    assert out["failed"] == []
    assert set(ne.extract_topics(page.body)) == {"失眠"}


def test_focus_failure_forensics_carries_click_point_element_chain(monkeypatch):
    """聚焦失败取证带**落点反查链**:那一下到底点在了谁身上、是不是编辑器内部。

    这两个字段是临时诊断字段(底栏候选坐实/排除后就撤),牙口在"取证收到的落点必须等于
    最后一次真点下去的坐标"——传错坐标的取证比没有取证更坏。
    """
    page = _BottomBarPage(bar_h=100, available={"失眠"})
    monkeypatch.setattr(ne, "_focus_click_point", ne._click_point)   # 逼出被吞的死中心落点

    out = ne.append_topics(page, _Human(page), ["失眠"])

    assert out["status"] == "error"
    detail = out["failed"][0]
    assert detail["reason"] == "content_box_focus_failed"
    assert detail["point_inside_editor"] is False        # 点在底栏上,不在编辑器里
    chain = detail["point_element_chain"]
    assert chain and len(chain) <= 300                   # 非空、且守临时取证的长度上限
    # 取证拿到的落点 = 最后一次真点下去的坐标(不是重算的、也不是 None)
    assert page.forensics_points[-1] == list(page.clicked_points[-1])


def test_focus_forensics_leaves_point_fields_empty_when_never_clicked(monkeypatch):
    """一次都没点成(正文框定位不到)→ 落点两字段留空,不编造一个坐标去反查。"""
    page = _TopicPage(available={"失眠"})
    monkeypatch.setattr(ne, "_locate", lambda *_a, **_kw: None)      # 全程定位不到正文框

    out = ne.append_topics(page, _Human(page), ["失眠"])

    assert out["status"] == "error"
    detail = out["failed"][0]
    assert detail["reason"] == "content_box_focus_failed"
    assert detail["scrolled_into_view"] is False
    assert detail["point_element_chain"] is None
    assert detail["point_inside_editor"] is None
    assert page.forensics_points == [None]


# ==================== 缺陷1:追加场景(# 粘连话题实体)专项 ====================


def test_fake_models_glued_hash_suppresses_dropdown():
    """夹具自检:本假页面在 # 粘连 chip 时抑制浮层、带空格分隔时弹出。注意这是**夹具的
    探针行为**,不是生产事实——「## 粘连是根因」已被真号复验推翻(空格修复上线后照样败,
    真因=浮层时序/锚定)。留着它只为给下面的分隔符测试装牙:没有它那些测试是橡皮图章。"""
    from app.browser.topic_dropdown import COLLECT_LAYERS_JS

    page = _TopicPage(body="正文 #复杂性创伤[话题]#", available={"失眠"})
    # 无分隔(旧代码 f"#{tag}"):夹具约定抑制浮层(探针行为,非生产根因)
    page.type_into_body("#失眠")
    assert page.evaluate(COLLECT_LAYERS_JS, "失眠") == {"layers": []}
    # 有分隔(新代码 f" #{tag}"):浮层弹出、含精确匹配
    page.type_into_body(" #失眠")
    assert page.evaluate(COLLECT_LAYERS_JS, "失眠")["layers"], "补空格分隔就该弹浮层"


def test_append_after_existing_chip_selects_with_separator():
    """追加场景卫生措施:正文末尾是已有话题实体时,append_topics 打头部空格分隔,浮层
    弹出、能选中成实体;原有话题实体无损。空格分隔是**保留的卫生措施不是根因修复**
    (真因=浮层时序,见条件轮询测试);此处只钉住"代码确实打了分隔"不回退。"""
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


# ==================== 浮层晚到:条件轮询(RCA 2026-08-09 复验) ====================


def test_append_topics_polls_until_late_dropdown_appears():
    """核心修复:浮层第 3 次采集才挂上,照样补得上 —— 定长单次快照会停在第 1 次就判败。

    牙口在 ``collect_calls == 3``:把轮询改回"等一次看一眼",这条必红(第 1 次看到的是
    空页面,词会被误判成浮层没弹并回删)。
    """
    page = _LateDropdownPage(late_ticks=2, available={"失眠"})
    human, page = _run(page)

    out = ne.append_topics(page, human, ["失眠"])

    assert out["status"] == "done"
    assert out["in_editor_added"] == ["失眠"]
    assert out["failed"] == []
    assert page.collect_calls == 3, "没轮到第 3 tick(单次快照会停在第 1 次)"
    assert set(ne.extract_topics(page.body)) == {"失眠"}
    # 命中即收手:没有多余的空转 tick,也没有多余的回删
    assert page.escapes == 0 and page.backspaces == 0


def test_append_topics_poll_waits_go_through_human_layer():
    """每 tick 的等待一律走拟人层(裸 sleep 是风控特征,本仓禁),且首 tick 等得更久。"""
    page = _LateDropdownPage(late_ticks=2, available={"失眠"})
    human, page = _run(page)

    ne.append_topics(page, human, ["失眠"])

    dropdown_waits = [w for w in human.waits if w[2] == "等待话题下拉"]
    assert len(dropdown_waits) == 3, "轮询的每一 tick 都该经 human.wait 拟人停顿"
    assert dropdown_waits[0][:2] == (1.4, 1.8)                      # 首 tick 从容一点
    assert all(w[:2] == (0.8, 1.2) for w in dropdown_waits[1:])      # 之后短间隔快速复查


def test_append_topics_records_poll_timeline_when_dropdown_never_shows():
    """浮层始终不来 → 失败回执带完整轮询时间线(多 tick),reason 是"浮层没弹"不是"没这词"。

    时间线是真因两候选之间的取证:全程 layers_seen=0 = 浮层真没挂;若哪天回执里
    layers_seen 稳定非 0 而 reason 恒为 not_found,那就是几何锚拒错了(候选 b)。
    """
    page = _LateDropdownPage(late_ticks=None, available={"失眠"})  # 词是有的,只是浮层不弹
    human, page = _run(page)

    out = ne.append_topics(page, human, ["失眠"])

    assert out["status"] == "error"
    detail = out["failed"][0]
    assert detail["reason"] == "topic_dropdown_not_shown"
    timeline = detail["poll_timeline"]
    assert len(timeline) > 1, "只看一眼就放弃 = 轮询没生效"
    assert len(timeline) <= 8, "轮询要有上限,不能无限等下去"
    assert [t["tick"] for t in timeline] == list(range(1, len(timeline) + 1))
    assert all(t["reason"] == "topic_dropdown_not_shown" for t in timeline)
    assert all(t["layers_seen"] == 0 for t in timeline)
    elapsed = [t["elapsed_s"] for t in timeline]
    assert all(isinstance(e, float) for e in elapsed)
    assert elapsed == sorted(elapsed), "elapsed_s 从打完字起算,必须单调不减"
    # 轮询不改回删语义:失败照样 Escape + 连分隔空格删净
    assert page.escapes == 1 and page.backspaces == len(" #失眠")
    assert ne.extract_topics(page.body) == []


def test_append_topics_poll_budget_is_per_topic_not_shared():
    """逐个话题各等各的:第一个词等满预算失败,第二个词照样有自己的完整轮询窗口。"""
    page = _LateDropdownPage(late_ticks=2, available={"亲密关系"})  # 「查无此词」不在集里
    human, page = _run(page)

    out = ne.append_topics(page, human, ["查无此词", "亲密关系"])

    assert out["status"] == "partially_applied"
    assert out["in_editor_added"] == ["亲密关系"]          # 前一个词等满预算没连坐它
    assert [f["tag"] for f in out["failed"]] == ["查无此词"]
    assert len(out["failed"][0]["poll_timeline"]) > 1


def test_append_topics_empty_to_add_is_skipped():
    """差集为空(全已挂)→ skipped、零动作(编排层据此不点发布)。"""
    page = _TopicPage()
    human, page = _run(page)
    out = ne.append_topics(page, human, [])
    assert out["status"] == "skipped"
    assert out["in_editor_added"] == [] and out["failed"] == []
    assert human.clicks == [] and human.typed == []
