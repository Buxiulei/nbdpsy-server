"""text_formatter 纯逻辑单测(不起真浏览器)。

覆盖:
- get_display_length:半角 1 / 全角(CJK)2 / emoji 2
- truncate_by_display:按显示宽度截断,结果 ≤ max_width 且不截半个字符
- format_for_xiaohongshu:清除 Markdown 标记(加粗 / 标题 / 链接等)

顺带对 login_detector / sync_human_actions 做 import 冒烟
(无账号不起浏览器,真交互由 e2e 覆盖)。
"""
import pytest

from app.browser import text_formatter


# ── get_display_length ──
def test_display_width_ascii():
    """纯半角字符按 1 计。"""
    assert text_formatter.get_display_length("abc") == 3


def test_display_width_emoji():
    """半角 1 + emoji 2 = 3。"""
    assert text_formatter.get_display_length("a😀") == 3


def test_display_width_cjk_fullwidth():
    """中文全角每字计 2。"""
    assert text_formatter.get_display_length("中") == 2
    assert text_formatter.get_display_length("中文") == 4


def test_display_width_mixed():
    """中英混排:2 个中文(4) + 3 个半角(3) = 7。"""
    assert text_formatter.get_display_length("你好abc") == 7


def test_display_width_empty():
    """空串宽度为 0。"""
    assert text_formatter.get_display_length("") == 0


# ── truncate_by_display ──
def test_truncate_title_within_width():
    """按显示宽度截到 ≤ 20。"""
    long_title = "很长的标题" * 10  # 全角 50 字 → 显示宽 100
    out = text_formatter.truncate_by_display(long_title, 20)
    assert text_formatter.get_display_length(out) <= 20


def test_truncate_no_half_char():
    """不截半个字符:宽 2 的字符不会因边界被切成半个。

    "中中中" 显示宽 6,截到 5 时只能容纳 2 个中文(宽 4),第 3 个会溢出到 6 > 5,
    因此保留 2 个,宽度 4 ≤ 5,且不出现半个字符。
    """
    out = text_formatter.truncate_by_display("中中中", 5)
    assert out == "中中"
    assert text_formatter.get_display_length(out) == 4


def test_truncate_shorter_than_limit_unchanged():
    """未超限时原样返回。"""
    assert text_formatter.truncate_by_display("短", 20) == "短"


def test_truncate_empty():
    """空串截断仍为空。"""
    assert text_formatter.truncate_by_display("", 20) == ""


# ── format_for_xiaohongshu(清 Markdown)──
def test_format_removes_bold():
    """**加粗** → 纯文字。"""
    assert text_formatter.format_for_xiaohongshu("**重点**内容") == "重点内容"


def test_format_removes_heading():
    """# 标题 → 标题。"""
    assert text_formatter.format_for_xiaohongshu("# 大标题") == "大标题"


def test_format_removes_link():
    """[文字](url) → 文字。"""
    assert text_formatter.format_for_xiaohongshu("看这里[官网](https://a.com)") == "看这里官网"


def test_format_plain_text_unchanged():
    """无 Markdown 的纯文本原样返回(仅去首尾空白)。"""
    assert text_formatter.format_for_xiaohongshu("普通文案") == "普通文案"


# ── 冒烟:login_detector / sync_human_actions 可导入 ──
def test_login_detector_importable():
    """login_detector 可导入且 DETECT_LOGIN_JS 是非空 str。"""
    from app.browser import login_detector

    assert isinstance(login_detector.DETECT_LOGIN_JS, str)
    assert login_detector.DETECT_LOGIN_JS.strip()


def test_sync_human_actions_importable():
    """sync_human_actions 可导入且暴露 SyncHumanActions 类。"""
    from app.browser import sync_human_actions

    assert hasattr(sync_human_actions, "SyncHumanActions")
