"""小红书文本格式化工具(纯函数,不依赖浏览器)。

移植自旧仓 ``app/utils/text_formatter.py``,对外收敛为三个函数:

- ``get_display_length(text)``   —— 显示宽度:全角(CJK)/emoji 计 2,半角计 1
- ``truncate_by_display(text, max_width)`` —— 按显示宽度截断,不切半个字符
- ``format_for_xiaohongshu(text)`` —— 清除 Markdown 标记,返回纯文本

与旧仓的函数名映射(见 task-3.2-report):
- 旧 ``get_display_length`` 是"小红书字符计数"(len + emoji 个数,中文按 1),
  本仓按 brief 要求改成真正的**显示宽度**(全角/emoji=2、半角=1),语义不同;
- 旧 ``truncate_to_length`` 按 Python ``len`` 切片 → 本仓新增按显示宽度截断的
  ``truncate_by_display``;
- 旧 ``remove_markdown_formatting`` 的清洗逻辑 → 本仓即 ``format_for_xiaohongshu``
  (旧仓那个 ``format_for_xiaohongshu(title, content)`` 双参版本不移植)。
"""
import re
import unicodedata

from loguru import logger

# 零宽字符:ZWJ(U+200D)/ 变体选择符(U+FE0E、U+FE0F),拼接 emoji 序列时不占显示宽度
_ZERO_WIDTH = {"\u200d", "\ufe0e", "\ufe0f"}


def _char_width(ch: str) -> int:
    """单个字符的显示宽度。

    - 零宽字符(ZWJ / 变体选择符 / 组合记号):0
    - 全角 / 宽字符(CJK、大部分 emoji):2
    - 其余(半角 ASCII 等):1
    """
    if ch in _ZERO_WIDTH or unicodedata.combining(ch):
        return 0
    if unicodedata.east_asian_width(ch) in ("W", "F"):
        return 2
    return 1


def get_display_length(text: str) -> int:
    """计算文本显示宽度(全角/emoji=2,半角=1)。"""
    if not text:
        return 0
    return sum(_char_width(ch) for ch in text)


def truncate_by_display(text: str, max_width: int) -> str:
    """按显示宽度截断文本,保证结果宽度 ≤ ``max_width`` 且不切半个字符。

    逐字符累加显示宽度,一旦加入下个字符会超出 ``max_width`` 就停止 ——
    因此永远在字符边界处截断,不会切出半个宽字符。
    """
    if not text or max_width <= 0:
        return "" if max_width <= 0 else text

    width = 0
    result = []
    for ch in text:
        w = _char_width(ch)
        if width + w > max_width:
            break
        width += w
        result.append(ch)
    return "".join(result)


def format_for_xiaohongshu(text: str) -> str:
    """清除文本中的 Markdown 标记,返回适合小红书发布的纯文本。

    小红书不支持 Markdown,需要把标记转成纯文本(保留内容)。
    """
    if not text:
        return text

    original_text = text
    has_markdown = False

    # 1. 加粗 **文字** / __文字__
    if re.search(r"\*\*[^*]+\*\*|__[^_]+__", text):
        has_markdown = True
        text = re.sub(r"\*\*([^*]+)\*\*", r"\1", text)
        text = re.sub(r"__([^_]+)__", r"\1", text)

    # 2. 斜体 *文字*(单个下划线可能是正常文本,不动)
    if re.search(r"(?<!\*)\*(?!\*)([^*]+)\*(?!\*)", text):
        has_markdown = True
        text = re.sub(r"(?<!\*)\*(?!\*)([^*]+)\*(?!\*)", r"\1", text)

    # 3. 删除线 ~~文字~~
    if re.search(r"~~[^~]+~~", text):
        has_markdown = True
        text = re.sub(r"~~([^~]+)~~", r"\1", text)

    # 4. 标题 # 标题
    if re.search(r"^#{1,6}\s+", text, re.MULTILINE):
        has_markdown = True
        text = re.sub(r"^#{1,6}\s+", "", text, flags=re.MULTILINE)

    # 5. 代码块 ```代码``` / 行内 `代码`
    if "```" in text:
        has_markdown = True
        text = re.sub(r"```[\s\S]*?```", "", text)
        text = re.sub(r"`([^`]+)`", r"\1", text)

    # 6. 引用 > 引用
    if re.search(r"^>\s+", text, re.MULTILINE):
        has_markdown = True
        text = re.sub(r"^>\s+", "", text, flags=re.MULTILINE)

    # 7. 链接 [文字](链接) → 文字
    if re.search(r"\[([^\]]+)\]\([^)]+\)", text):
        has_markdown = True
        text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)

    # 8. 图片 ![alt](url) → alt
    if re.search(r"!\[([^\]]*)\]\([^)]+\)", text):
        has_markdown = True
        text = re.sub(r"!\[([^\]]*)\]\([^)]+\)", r"\1", text)

    # 9. 无序列表 → • 列表
    if re.search(r"^[\*\-\+]\s+", text, re.MULTILINE):
        has_markdown = True
        text = re.sub(r"^[\*\-\+]\s+", "• ", text, flags=re.MULTILINE)

    # 10. 有序列表 → 去掉序号
    if re.search(r"^\d+\.\s+", text, re.MULTILINE):
        has_markdown = True
        text = re.sub(r"^\d+\.\s+", "", text, flags=re.MULTILINE)

    # 11. 水平分割线
    if re.search(r"^[\*\-_]{3,}$", text, re.MULTILINE):
        has_markdown = True
        text = re.sub(r"^[\*\-_]{3,}$", "", text, flags=re.MULTILINE)

    # 12. 压缩多余空行(超过 2 个连续换行)
    text = re.sub(r"\n{3,}", "\n\n", text)

    # 13. 清理首尾空白
    text = text.strip()

    if has_markdown:
        logger.info("[文本格式化] 检测到并移除了 Markdown 格式")
        logger.debug(f"[文本格式化] 原始文本: {original_text[:100]}...")
        logger.debug(f"[文本格式化] 清理后: {text[:100]}...")

    return text
