"""发布落地层纯逻辑单测(不起浏览器)。

覆盖从 atomic_tasks / sync_client 抽出的可测纯函数:
- strip_trailing_hashtags:正文剥结尾 # 串(话题单一来源)
- truncate_title:标题按 text_formatter.get_display_length 硬截断 ≤20
- truncate_body:正文安全截断 900
- dedupe_topics:话题去重 + 截断 ≤10
- normalize_cookies_for_injection:cookie 双域注入 + domain/sameSite 规整
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
