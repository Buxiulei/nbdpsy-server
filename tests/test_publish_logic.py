"""发布落地层纯逻辑单测(不起浏览器)。

覆盖从 atomic_tasks / sync_client 抽出的可测纯函数:
- strip_trailing_hashtags:正文剥结尾 # 串(话题单一来源)
- truncate_title:标题按 text_formatter.get_display_length 硬截断 ≤20
- truncate_body:正文安全截断 900
- dedupe_topics:话题去重 + 截断 ≤10
- normalize_cookies_for_injection:cookie 双域注入 + domain/sameSite 规整
- step6 话题失败取证:浮层候选文案 / 条数 / 容器 class / 正文框回读(替身驱动,不起浏览器)
"""
from app.browser.atomic_tasks import (
    XHS_MAX_BODY_LENGTH,
    XHS_MAX_TITLE_DISPLAY,
    dedupe_topics,
    strip_trailing_hashtags,
    truncate_body,
    truncate_title,
)
from app.browser.sync_client import normalize_cookies_for_injection
from app.browser.text_formatter import get_display_length


# ── strip_trailing_hashtags ──

def test_strip_trailing_hashtags_basic():
    """剥掉结尾一串 #话题,保留正文主体。"""
    src = "今天分享一个减脂心得\n\n#减脂 #健身 #自律"
    assert strip_trailing_hashtags(src) == "今天分享一个减脂心得"


def test_strip_trailing_hashtags_fullwidth_space():
    """结尾话题串含全角空格/换行也要剥净。"""
    src = "正文内容　#话题一　#话题二　"
    assert strip_trailing_hashtags(src) == "正文内容"


def test_strip_trailing_hashtags_no_tags_unchanged():
    """正文中间的 # 不在结尾 → 不动(只剥结尾串)。"""
    src = "标题 #C语言 是一门语言\n继续正文"
    assert strip_trailing_hashtags(src) == src


def test_strip_trailing_hashtags_empty():
    assert strip_trailing_hashtags("") == ""


# ── truncate_title ──

def test_truncate_title_under_limit_unchanged():
    """20 个中文 = 显示长度 20,不截断。"""
    title = "一" * 20
    assert get_display_length(title) == 20
    assert truncate_title(title) == title


def test_truncate_title_over_limit_hard_cut():
    """25 个中文 → 硬截断到显示长度 ≤20。"""
    title = "一" * 25
    out = truncate_title(title)
    assert get_display_length(out) <= XHS_MAX_TITLE_DISPLAY
    assert out == "一" * 20


def test_truncate_title_emoji_not_split():
    """含 emoji 标题截断不切半个 emoji,且显示长度 ≤20。"""
    title = "🏃‍♀️" + "健身打卡每日坚持不放弃加油努力冲冲冲"
    out = truncate_title(title)
    assert get_display_length(out) <= XHS_MAX_TITLE_DISPLAY


# ── truncate_body ──

def test_truncate_body_under_limit_unchanged():
    body = "正文" * 100  # 200 字
    assert truncate_body(body) == body


def test_truncate_body_over_limit():
    body = "字" * 1000
    out = truncate_body(body)
    assert len(out) == XHS_MAX_BODY_LENGTH == 900


# ── dedupe_topics ──

def test_dedupe_topics_collapses_hash_variants():
    """'#a' 与 'a' 视为同一话题,去重后保留首次出现的原始写法。"""
    out = dedupe_topics(["#减脂", "减脂", "#健身"])
    assert out == ["#减脂", "#健身"]


def test_dedupe_topics_truncate_to_10():
    """超过 10 个截断到 10。"""
    tags = [f"话题{i}" for i in range(15)]
    out = dedupe_topics(tags)
    assert len(out) == 10
    assert out == [f"话题{i}" for i in range(10)]


def test_dedupe_topics_skips_empty():
    """空/纯 # 项跳过。"""
    out = dedupe_topics(["", "#", "  ", "#正常"])
    assert out == ["#正常"]


def test_dedupe_topics_none():
    assert dedupe_topics(None) == []


# ── normalize_cookies_for_injection ──

def test_normalize_cookies_dual_domain_injection():
    """.xiaohongshu.com cookie → 主站 1 条 + creator 子域 fallback 1 条(共 2 条)。"""
    out = normalize_cookies_for_injection(
        [{"name": "web_session", "value": "abc", "domain": ".xiaohongshu.com"}]
    )
    assert len(out) == 2
    main = out[0]
    creator = out[1]
    assert main["domain"] == ".xiaohongshu.com"
    assert "url" not in main
    assert creator["url"] == "https://creator.xiaohongshu.com/"
    assert "domain" not in creator
    assert creator["value"] == "abc"


def test_normalize_cookies_www_domain_normalized():
    """www.xiaohongshu.com → 归一为 .xiaohongshu.com,并触发双域注入。"""
    out = normalize_cookies_for_injection(
        [{"name": "a", "value": "1", "domain": "www.xiaohongshu.com"}]
    )
    assert out[0]["domain"] == ".xiaohongshu.com"
    assert len(out) == 2  # 归一后是主站域,补 creator fallback


def test_normalize_cookies_samesite_coerced():
    """sameSite 归一到 Strict/Lax/None(大小写/别名容错)。"""
    out = normalize_cookies_for_injection(
        [{"name": "a", "value": "1", "domain": ".xiaohongshu.com", "sameSite": "no_restriction"}]
    )
    # no_restriction 非 strict/none → 兜底 Lax
    assert out[0]["sameSite"] == "Lax"

    out2 = normalize_cookies_for_injection(
        [{"name": "a", "value": "1", "domain": ".xiaohongshu.com", "sameSite": "None"}]
    )
    assert out2[0]["sameSite"] == "None"


def test_normalize_cookies_expires_preserved():
    """有效 expires 保留,子域项也带上。"""
    out = normalize_cookies_for_injection(
        [{"name": "a", "value": "1", "domain": ".xiaohongshu.com", "expires": 9999999999}]
    )
    assert out[0]["expires"] == 9999999999
    assert out[1]["expires"] == 9999999999


def test_normalize_cookies_skips_malformed():
    """缺 name 或缺 value 的项跳过,不进注入列表。"""
    out = normalize_cookies_for_injection(
        [{"value": "no_name", "domain": ".xiaohongshu.com"}, {"name": "no_value"}]
    )
    assert out == []


def test_normalize_cookies_empty():
    assert normalize_cookies_for_injection([]) == []


def test_normalize_cookies_non_xhs_domain_no_creator_fallback():
    """非 .xiaohongshu.com 主域 cookie 不补 creator 子域项。"""
    out = normalize_cookies_for_injection(
        [{"name": "a", "value": "1", "domain": ".other.com"}]
    )
    assert len(out) == 1
    assert out[0]["domain"] == ".other.com"


# ── I1:step2 SSO 失败的 need_manual_login 透出(不被 publish_note 丢弃) ──

def test_publish_note_propagates_step2_need_manual_login(monkeypatch):
    """I1:step2 上传时 SSO 失败(need_manual_login=True)→ publish_note 透出该独立信号。

    过去 step2 分支只带 error 不带 need_manual_login,信号被丢弃 → 状态机当普通失败徒劳重试。
    """
    from app.browser import sync_client as sc

    class _FakeAtomic:
        def __init__(self, page, job_tag=None):
            self.page = page
            self.job_tag = job_tag  # 只用于给截图打标,不参与发布逻辑

        def step1_open_publish_page(self):
            return {"success": True, "url": "https://creator.xiaohongshu.com/publish"}

        def step2_upload_images(self, image_paths):
            return {
                "success": False,
                "error": "创作中心未登录,自动认证失败。请使用远程浏览器手动登录一次。",
                "need_manual_login": True,
            }

    monkeypatch.setattr(sc, "XHSPublishAtomicTasks", _FakeAtomic)

    client = sc.SyncClient(account_id=1, cookies=[])
    result = client.publish_note("标题", "正文", ["/tmp/a.png"], ["#心理"])
    assert result["success"] is False
    assert result["need_manual_login"] is True
    assert "创作中心未登录" in result["error"]


# ── import 冒烟(守 CI 不出 ImportError;真实发布留 P3.5/P5 e2e) ──

def test_publish_modules_import_and_public_surface():
    """sync_client / atomic_tasks / images 可导入且对外接口就位。"""
    from app.browser import atomic_tasks, images, sync_client

    assert callable(images.materialize_images)
    assert callable(sync_client.publish_once)
    assert callable(sync_client.check_login_once)
    # PublishResult 契约字段齐全
    r = sync_client.PublishResult(success=True, note_url="u")
    assert r.success is True and r.note_id == "" and r.need_manual_login is False
    # 原子步骤类 + step1-7 方法齐全
    for step in (
        "step1_open_publish_page",
        "step2_upload_images",
        "step3_wait_for_upload_processing",
        "step4_enter_edit_page",
        "step5_fill_content",
        "step6_set_publish_options",
        "step7_click_publish_and_wait",
    ):
        assert hasattr(atomic_tasks.XHSPublishAtomicTasks, step)


def test_normalize_cookies_dup_picks_later_expiry_live():
    """同名双份(RCA 2026-07-25 三路真验活铁证):归一 `.xiaohongshu.com` 后按
    **expires 更晚者胜**(最近写入=活凭据)。实测活 web_session 常在 host-only 那份且
    过期更晚——07-24 武断保留 `.域` 把活号注入成 invalid,本用例锁死正确方向。"""
    out = normalize_cookies_for_injection([
        {"name": "web_session", "value": "OLD", "domain": ".xiaohongshu.com",
         "expires": 1000},
        {"name": "web_session", "value": "LIVE", "domain": "xiaohongshu.com",
         "expires": 2000},  # 更晚过期 = 活凭据
    ])
    # 同名归一同键 → 只 1 条主站 + 1 条 creator,值取 LIVE
    dotted = [c for c in out if c.get("domain") == ".xiaohongshu.com"]
    creator = [c for c in out if "url" in c]
    assert len(dotted) == 1 and dotted[0]["value"] == "LIVE"
    assert len(creator) == 1 and creator[0]["value"] == "LIVE"
    assert all(c["value"] == "LIVE" for c in out)  # 旧值 OLD 绝不注入


def test_normalize_cookies_dup_tie_hostonly_wins():
    """expires 平局时 host-only 源胜(实测活值所在)。"""
    out = normalize_cookies_for_injection([
        {"name": "id_token", "value": "DOT", "domain": ".xiaohongshu.com", "expires": 500},
        {"name": "id_token", "value": "HOST", "domain": "xiaohongshu.com", "expires": 500},
    ])
    assert all(c["value"] == "HOST" for c in out)


def test_normalize_cookies_solitary_hostonly_still_dotted():
    """孤立 host-only(无同名 `.域` 份)加点归一为 `.xiaohongshu.com` 并触发 creator。"""
    out = normalize_cookies_for_injection(
        [{"name": "solo", "value": "x", "domain": "xiaohongshu.com"}]
    )
    assert out[0]["domain"] == ".xiaohongshu.com"
    assert any("url" in c for c in out)  # 加点后照常触发 creator fallback


# ---------------- 发布结果回显(2026-08-03 文字版丢话题事故) ----------------
#
# 事故:文字版超长竖图把正文框顶出视口,聚焦点击落在页面顶栏(实测 y=72,对照正常
# 轮播 y=788),#话题 打进虚空,6 个话题全报 no_floating_layer 静默丢光;运营删重发
# 验证后才确认,白损失一篇笔记的数据。两层修:①step6 先滚进视口+点击后验焦点;
# ②发布结果回显"实际应用了什么",丢弃当场可见。


def test_publish_result_carries_applied_echo(monkeypatch):
    """成功发布的 PublishResult.applied 带话题逐个成败 + 组件结果。"""
    from app.browser import sync_client as sc

    class _FakeClient:
        def __init__(self, *a, **k):
            pass

        def start(self):
            return {"success": True}

        def publish_note(self, *a, **k):
            return {
                "success": True, "note_id": "n1", "note_url": "u1",
                "components": {"activity": {"status": "done"}},
                "topics_applied": ["恋爱脑", "亲密关系"],
                "topics_failed": [{"tag": "融合渴望", "reason": "no_exact_match"}],
            }

        def stop(self):
            pass

    monkeypatch.setattr(sc, "SyncClient", _FakeClient)

    r = sc.publish_once(1, [], "标题", "正文", [], ["恋爱脑", "亲密关系", "融合渴望"])

    assert r.success is True
    assert r.applied["topics_requested"] == ["恋爱脑", "亲密关系", "融合渴望"]
    assert r.applied["topics_applied"] == ["恋爱脑", "亲密关系"]
    assert r.applied["topics_failed"][0]["tag"] == "融合渴望"
    assert r.applied["components"]["activity"]["status"] == "done"


def test_publish_failure_has_no_applied(monkeypatch):
    """失败的发布不带 applied(没发出去,谈不上"实际应用了什么")。"""
    from app.browser import sync_client as sc

    class _FakeClient:
        def __init__(self, *a, **k):
            pass

        def start(self):
            return {"success": True}

        def publish_note(self, *a, **k):
            return {"success": False, "error": "boom"}

        def stop(self):
            pass

    monkeypatch.setattr(sc, "SyncClient", _FakeClient)

    r = sc.publish_once(1, [], "标题", "正文", [], ["恋爱脑"])

    assert r.success is False and r.applied is None


# ── 话题失败取证(2026-08-07 视频 e2e 6/6 全 no_exact_match 后补) ──
#
# 同期图文近 30 条累计 applied=171 / failed=10,机制本身健康;视频那条却一个都没中,
# 连「心理科普」这种常见词都失败。旧回执里只有一个 reason 字符串,判不出是
# 「浮层里显示的是默认推荐话题(= 搜索压根没触发)」还是「这些词平台真没有」。
# 那一轮不猜着改逻辑,只把当场证据(浮层候选文案 / 条数 / 容器 class / 正文框回读)带出来 ——
# 隔天(08-08)这四个字段就把真因指了出来:抓到的是右侧预览面板的 base-info。
# 判据本身的单测在 tests/test_topic_dropdown.py,这里守的是**接线**:采集 JS → 判据 →
# 取证 → 回删 这条链路照旧走通。


class _TopicEl:
    """正文框替身:inner_text 回读当前内容,bounding_box 给个正常视口内的框。"""

    def __init__(self, page):
        self._page = page

    def inner_text(self):
        return self._page.body

    def bounding_box(self):
        return {"x": 100.0, "y": 700.0, "width": 600.0, "height": 200.0}


class _TopicPage:
    def __init__(self, collect):
        self._collect = collect
        self.body = "正文内容"
        self.keys = []
        self.content_el = _TopicEl(self)
        self.keyboard = self

    def query_selector(self, sel):
        return self.content_el if "contenteditable" in sel or "正文" in sel else None

    def evaluate(self, js, arg=None):
        if "activeElement" in js:
            return True          # 焦点验证:替身里一律认为聚焦成功
        return self._collect(arg)   # 采集 JS:返回浮层清单,判定由 select_topic_option 做

    def press(self, key):        # page.keyboard.press
        self.keys.append(key)


class _TopicHuman:
    def __init__(self, page):
        self.page = page
        self.clicks = []

    def scroll_to_element(self, _el):
        return None

    def click(self, target, reason="", **_k):
        self.clicks.append((target, reason))
        return None

    def press_key(self, _key, **_k):
        return None

    def type_text(self, _el, text, **_k):
        self.page.body += text   # 打进去的字要能被正文框回读看到

    def wait(self, *_a, **_k):
        return None


def _run_step6(collect, tags):
    from app.browser.atomic_tasks import XHSPublishAtomicTasks

    page = _TopicPage(collect)
    tasks = XHSPublishAtomicTasks.__new__(XHSPublishAtomicTasks)
    tasks.page = page
    tasks.human = _TopicHuman(page)
    tasks.current_step = 0
    tasks.enable_debug = False
    tasks.job_tag = ""
    tasks.screenshot_dir = "/tmp"
    return tasks.step6_set_publish_options(tags=tags), page, tasks.human


# 替身正文框 bounding_box 是 x=100 w=600(见 _TopicEl),下拉夹具就摆在这一列里
def _dropdown_layer(texts, x=300.0, y=920.0):
    return {"cls": "topic-container", "rect": {"x": x, "y": y, "width": 320.0, "height": 200.0},
            "items": [{"text": t, "x": x + 100, "y": y + 20 + 40 * i}
                      for i, t in enumerate(texts)]}


def test_topic_failure_carries_dropdown_candidates_and_editor_readback():
    """no_exact_match 必须带出浮层**实际**枚举到的候选 + 正文框回读,不能只丢一句 reason。"""
    seen = ["#生活美学 1.2万次浏览", "#日常文案 8千次浏览", "#人生的意义 3万次浏览"]

    out, _page, _human = _run_step6(lambda _t: {"layers": [_dropdown_layer(seen)]}, ["心理科普"])

    assert out["success"] is True and out["topics_applied"] == []
    detail = out["topics_failed"][0]
    assert detail["tag"] == "心理科普"
    # 下拉在、词不在 —— 这才是 no_exact_match
    assert detail["reason"] == "no_exact_match"
    # 有了这几条,下一次真跑一眼判定:候选是「默认推荐」→ 输入没进去;是搜索结果 → 词不存在
    assert detail["candidates"] == seen
    assert detail["item_count"] == 3
    assert detail["layer_class"] == "topic-container"
    assert detail["editor_tail"].endswith("#心理科普"), detail["editor_tail"]


def test_topic_evidence_is_read_before_the_backspace_cleanup():
    """取证必须发生在**回删之前** —— 回删完正文框就看不出打进去过什么了。"""
    def _collect(_tag):
        return {"layers": [_dropdown_layer(["#生活美学 1.2万次浏览", "#日常 3千次浏览"])]}

    out, page, _human = _run_step6(_collect, ["睡不着"])

    assert "#睡不着" in out["topics_failed"][0]["editor_tail"]
    assert page.keys and page.keys[0] == "Escape", "回删链路本身不能被取证改掉"


def test_topic_success_path_still_applies_without_evidence_noise():
    """匹配上的话题照旧进 topics_applied,坐标取自被点中的那一行。"""
    def _collect(tag):
        return {"layers": [_dropdown_layer([f"#{tag} 1.2万次浏览", "#别的话题 5千次浏览"])]}

    out, _page, human = _run_step6(_collect, ["心理科普", "情绪内耗"])

    assert out["topics_applied"] == ["心理科普", "情绪内耗"]
    assert out["topics_failed"] == []
    assert [c for c in human.clicks if "话题选项" in c[1]], "没走拟人点击"


def test_video_preview_layer_does_not_get_clicked_end_to_end():
    """视频页那种"只抓到右侧预览面板"的现场:整条链路要判 topic_dropdown_not_found,
    并且**一次话题点击都不许发生**(点上去等于在预览面板上乱点)。"""
    def _collect(tag):
        return {"layers": [{
            "cls": "base-info",
            "rect": {"x": 1145.0, "y": 490.0, "width": 225.0, "height": 55.0},
            "items": [{"text": "NBDpsy-亲密关系 关注", "x": 1250.0, "y": 500.0},
                      {"text": "关注", "x": 1340.0, "y": 500.0},
                      {"text": f"#{tag}", "x": 1250.0, "y": 530.0},
                      {"text": "编辑于 刚刚·公开可见", "x": 1240.0, "y": 552.0}],
        }]}

    out, _page, human = _run_step6(_collect, ["投射性认同"])

    detail = out["topics_failed"][0]
    assert detail["reason"] == "topic_dropdown_not_found"
    assert detail["layer_class"] == "base-info"
    assert detail["layers_seen"] == 1 and detail["rejected_classes"] == ["base-info"]
    assert [c for c in human.clicks if "话题选项" in c[1]] == [], "点到预览面板上去了"


def test_topic_failure_detail_caps_candidates_at_ten():
    """候选只留前 10 条:回执要塞进 job 台账,别让它无界膨胀。"""
    from app.browser.atomic_tasks import topic_failure_detail

    detail = topic_failure_detail(
        "睡不着",
        {"reason": "no_exact_match", "candidates": [f"词{i}" for i in range(30)],
         "item_count": 30, "layer_class": "c" * 200},
        "…#睡不着",
    )
    assert len(detail["candidates"]) == 10
    assert len(detail["layer_class"]) == 80


def test_editor_tail_is_silent_when_readback_blows_up():
    """回读正文框是取证,取证本身绝不制造新异常(读不到就交空串)。"""
    from app.browser.atomic_tasks import read_editor_tail

    class _Boom:
        def inner_text(self):
            raise RuntimeError("detached")

    assert read_editor_tail(_Boom()) == ""


def test_collect_js_only_enumerates_and_never_decides():
    """采集 JS 只**枚举**浮层:判据留在 Python 才测得动(JS 本身没法单测)。

    守两条:一、每层带回判据要用的 class / 几何 / 子项;二、别把"取面积最小的那层"
    这类判定塞回 JS —— 那正是视频页 6/6 全败的成因(RCA 2026-08-07)。
    """
    from app.browser.topic_dropdown import COLLECT_LAYERS_JS

    for key in ("cls:", "rect:", "items:", "has_tag:"):
        assert key in COLLECT_LAYERS_JS, key
    for decided in ("no_exact_match", "topic_dropdown_not_shown", "success:"):
        assert decided not in COLLECT_LAYERS_JS, f"判定回流到 JS 里了: {decided}"


def test_topic_failure_reasons_are_distinguishable():
    """三种失败必须分得开:没浮层 / 没找到下拉 / 下拉里没这词。"""
    from app.browser.topic_dropdown import select_topic_option

    editor = {"x": 100.0, "y": 700.0, "width": 600.0, "height": 200.0}
    preview = {"cls": "x", "rect": {"x": 1200.0, "y": 500.0, "width": 200.0, "height": 50.0},
               "items": [{"text": "#心理科普", "x": 1300.0, "y": 520.0}]}

    assert select_topic_option({"layers": []}, "心理科普", editor)["reason"] \
        == "topic_dropdown_not_shown"
    assert select_topic_option({"layers": [preview]}, "心理科普", editor)["reason"] \
        == "topic_dropdown_not_found"
    assert select_topic_option({"layers": [_dropdown_layer(["#别的 1万次浏览", "#词 2万次浏览"])]},
                               "心理科普", editor)["reason"] == "no_exact_match"
