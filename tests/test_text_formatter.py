"""text_formatter 纯逻辑单测(不起真浏览器)。

覆盖:
- get_display_length:小红书字符计数 = len(text) + 可见 emoji 序列个数
  (每字符含中文按 1,每个可见 emoji 额外 +1;经旧仓生产验证)
- truncate_by_display:按该度量截断,结果 ≤ max_width 且不截半个字符 / emoji 序列
- format_for_xiaohongshu:清除 Markdown 标记(加粗 / 标题 / 链接等)

顺带对 login_detector / sync_human_actions 做 import 冒烟
(无账号不起浏览器,真交互由 e2e 覆盖)。
"""
import pytest

from app.browser import text_formatter


# ── get_display_length(len + 可见 emoji 序列个数,中文按 1)──
def test_display_length_ascii():
    """纯半角字符按 1 计。"""
    assert text_formatter.get_display_length("abc") == 3


def test_display_length_emoji():
    """a=1 + 😀(len1 + 可见 emoji1 = 2) = 3。"""
    assert text_formatter.get_display_length("a😀") == 3


def test_display_length_cjk_counts_one():
    """中文每字计 1(旧仓生产语义:20 个中文标题 = 20,符合小红书 20 上限)。"""
    assert text_formatter.get_display_length("中") == 1
    assert text_formatter.get_display_length("中文标题") == 4


def test_display_length_cjk_with_emoji():
    """2 个中文(2) + 😀(len1 + 可见 emoji1 = 2) = 4。"""
    assert text_formatter.get_display_length("中文😀") == 4


def test_display_length_mixed():
    """中英混排:2 个中文(2) + 3 个半角(3) = 5。"""
    assert text_formatter.get_display_length("你好abc") == 5


def test_display_length_zwj_emoji():
    """ZWJ 复合 emoji 🏃‍♀️ 整体计 1 个可见 emoji:len(4) + 1 = 5。"""
    text = "🏃‍♀️"
    assert text_formatter.get_display_length(text) == len(text) + 1
    assert text_formatter.get_display_length(text) == 5


def test_display_length_empty():
    """空串长度为 0。"""
    assert text_formatter.get_display_length("") == 0


# ── truncate_by_display(按 get_display_length 度量截断)──
def test_truncate_20_cjk_unchanged():
    """20 个中文 + max_width=20 → 恰好不截断(每字计 1,总长 20 ≤ 20)。"""
    title = "标" * 20
    out = text_formatter.truncate_by_display(title, 20)
    assert out == title
    assert text_formatter.get_display_length(out) == 20


def test_truncate_21_cjk_to_20():
    """21 个中文 + max_width=20 → 截到 20 个中文。"""
    out = text_formatter.truncate_by_display("标" * 21, 20)
    assert out == "标" * 20
    assert text_formatter.get_display_length(out) == 20


def test_truncate_emoji_not_split():
    """含 emoji 时不把 emoji 序列截半:边界处放不下整段 emoji 就整段丢弃。

    "中中中🏃‍♀️":前 3 个中文宽 3,🏃‍♀️ 宽 5(len4 + 可见 1),总宽 8。
    截到 5 时只容得下 3 个中文(宽 3),加 emoji 会到 8 > 5 → 丢弃整段 emoji,
    结果 "中中中" 宽 3 ≤ 5,不出现半个 emoji。
    """
    out = text_formatter.truncate_by_display("中中中🏃‍♀️", 5)
    assert out == "中中中"
    assert text_formatter.get_display_length(out) <= 5


def test_truncate_consecutive_independent_emoji():
    """连续但无 ZWJ 连接的独立 emoji 可逐个保留、用满预算,不被整串丢弃。

    "a😀😀😀" + max_width=3:a(1) + 😀(len1+可见1=2)= 3 恰好用满;加第二个 😀
    → get_display_length("a😀😀")=len3+可见1=4 > 3 → 停。结果 "a😀"。
    (旧实现把 😀😀😀 当一个不可分单元,边界落其中 → 只剩 "a"。)
    """
    assert text_formatter.truncate_by_display("a😀😀😀", 3) == "a😀"


def test_truncate_consecutive_emoji_uses_full_budget():
    """"新品🎉🎉🎉" + max_width=4:新品(2) + 🎉(len1+可见1) → 4 恰好;加第二个 🎉=5 超。"""
    assert text_formatter.truncate_by_display("新品🎉🎉🎉", 4) == "新品🎉"


def test_truncate_zwj_emoji_kept_whole_when_fits():
    """ZWJ 复合 emoji 放得下时整体保留(原子单元不被拆):中中中🏃‍♀️ 宽 8 @ 8 全留。"""
    text = "中中中🏃‍♀️"
    assert text_formatter.get_display_length(text) == 8
    assert text_formatter.truncate_by_display(text, 8) == text


def test_truncate_pure_emoji():
    """纯独立 emoji 串按预算逐个保留:😀😀😀 @ 2 → 😀(len1+可见1=2 用满,第二个超)。"""
    assert text_formatter.truncate_by_display("😀😀😀", 2) == "😀"


def test_truncate_max_width_zero():
    """max_width=0 → 空串(放不下任何单元)。"""
    assert text_formatter.truncate_by_display("abc", 0) == ""


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
