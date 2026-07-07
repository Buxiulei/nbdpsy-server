"""小红书文本格式化工具(纯函数,不依赖浏览器)。

移植自旧仓 ``app/utils/text_formatter.py``,对外收敛为三个函数:

- ``get_display_length(text)``   —— 小红书字符计数:``len(text) + 可见 emoji 序列个数``
  (每个字符含中文按 1,每个可见 emoji 序列额外 +1)
- ``truncate_by_display(text, max_width)`` —— 按上面这个度量截断,不切半个字符 / 半个 emoji 序列
- ``format_for_xiaohongshu(text)`` —— 清除 Markdown 标记,返回纯文本

与旧仓的函数名映射(见 task-3.2-report):
- 旧 ``get_display_length`` 是"小红书字符计数"(``len`` + 可见 emoji 个数,中文按 1),
  经生产验证:20 个中文标题 = 20,符合小红书 20 字上限 —— 本仓忠实还原该语义;
- 旧 ``truncate_to_length`` 按 Python ``len`` 切片 → 本仓换成按上述度量截断的
  ``truncate_by_display``(且保证 emoji 序列不被截半);
- 旧 ``remove_markdown_formatting`` 的清洗逻辑 → 本仓即 ``format_for_xiaohongshu``
  (旧仓那个 ``format_for_xiaohongshu(title, content)`` 双参版本不移植)。
"""
import re

from loguru import logger

# 可见 emoji 序列正则(自旧仓 ``app/utils/text_formatter.py`` 忠实移植)。
# 旧仓用第三方 ``regex`` 库,但这里只用到字符范围 + 交替 + 量词,stdlib ``re``
# 完全等价 —— 已实测两者在全部用例(含 ZWJ 复合 emoji / 国旗)上结果一致,故不引入新依赖。
# 一个匹配 = 一个可见 emoji(含 ZWJ 复合序列,如 🏃‍♀️ 整体计 1)。
_EMOJI_PATTERN = re.compile(
    r'(?:'
    r'[\U0001F1E6-\U0001F1FF]{2}'  # 国旗(两个区域指示符)
    r'|'
    r'[\U0001F300-\U0001F9FF\U0001FA70-\U0001FAFF\U00002600-\U000027BF]'  # emoji 基础字符
    r'[\U0001F3FB-\U0001F3FF️‍]*'  # 可选的肤色修饰符、变体选择器、ZWJ
    r'(?:[\U0001F300-\U0001F9FF\U0001FA70-\U0001FAFF\U00002600-\U000027BF][\U0001F3FB-\U0001F3FF️]*)*'  # 可选的后续 emoji
    r')'
)


def get_display_length(text: str) -> int:
    """计算文本的小红书显示长度:``len(text) + 可见 emoji 序列个数``。

    小红书字符计数规则(经生产验证):
    - 普通字符(中文、英文、数字):每个计 1
    - 每个可见的 emoji 序列(含 ZWJ 复合):在 ``len`` 基础上额外 +1

    例:'🏃‍♀️5分钟...' 的 ``len`` 已含 emoji 的码点数,再 +1 得小红书长度;
    20 个中文标题 = 20(符合小红书 20 字上限)。
    """
    if not text:
        return 0
    visible_emoji_count = len(_EMOJI_PATTERN.findall(text))
    return len(text) + visible_emoji_count


# 截断专用的原子单元正则。与计数用的 ``_EMOJI_PATTERN`` 关键区别:后续 emoji 基础
# 字符**必须由 ZWJ(U+200D)连接**才并入同一单元 —— 因此 ZWJ 复合序列(如 🏃‍♀️)
# 整体不可分,而**相邻但无 ZWJ 的独立 emoji**(😀😀)各自成独立单元,不被贪婪合并。
# (``_EMOJI_PATTERN`` 末尾那段无 ZWJ 也贪婪续接,截断时会把整串独立 emoji 当一个
#  不可分单元、边界落其中就整串丢弃 —— 那是计数语义,不适合切分。)
_ATOMIC_EMOJI_PATTERN = re.compile(
    r'(?:'
    r'[\U0001F1E6-\U0001F1FF]{2}'  # 国旗(两个区域指示符)
    r'|'
    r'[\U0001F300-\U0001F9FF\U0001FA70-\U0001FAFF\U00002600-\U000027BF]'  # emoji 基础字符
    r'[\U0001F3FB-\U0001F3FF️]*'  # 可选的肤色修饰符 / 变体选择器
    r'(?:‍[\U0001F300-\U0001F9FF\U0001FA70-\U0001FAFF\U00002600-\U000027BF][\U0001F3FB-\U0001F3FF️]*)*'  # 仅经 ZWJ 续接后续 emoji
    r')'
)


def _iter_atomic_units(text: str):
    """把文本切成不可再分的原子单元(截断边界只落在单元之间)。

    - ZWJ 复合 emoji 序列(如 🏃‍♀️ = emoji + ZWJ + emoji + 修饰符)整体为一个单元;
    - 相邻但无 ZWJ 连接的独立 emoji(😀😀)各自成独立单元;
    - 其余每个普通字符各为一个单元。

    宽度裁判交给 ``get_display_length``,本函数只负责"在哪切"。
    """
    spans = {m.start(): m.end() for m in _ATOMIC_EMOJI_PATTERN.finditer(text)}
    i, n = 0, len(text)
    while i < n:
        if i in spans:
            end = spans[i]
            yield text[i:end]
            i = end
        else:
            yield text[i]
            i += 1


def truncate_by_display(text: str, max_width: int) -> str:
    """按 ``get_display_length`` 度量截断文本,保证结果长度 ≤ ``max_width``。

    以 ``get_display_length`` 为唯一宽度裁判,逐原子单元累加:放得下就加上,放不下就
    停 —— 因此永远在字符 / emoji 序列边界处截断,不会切出半个 emoji;且相邻独立 emoji
    各自成单元,可逐个保留、用满预算(不因贪婪合并被整串丢弃)。ZWJ 复合序列仍整体不可分。
    """
    if not text or max_width <= 0:
        return "" if max_width <= 0 else text

    result = ""
    for unit in _iter_atomic_units(text):
        if get_display_length(result + unit) <= max_width:
            result += unit
        else:
            break
    return result


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
