"""已发布笔记编辑(标题 / 正文 / 图片增删)的**纯逻辑层**。

设计 `docs/design/2026-08-03-note-editing-design.md`(3.2 判据 / 4.2 顺序 / 4.3 删除防护)。

**本文件当前只含纯函数**:浏览器交互步骤(`_type_into`、图片删除/追加、逐步回读)
依赖第五节 HARD-GATE 的受控写测试证据(E2/E3/E4/E5/E6/E7),证据未闭环前一行都不许写
—— 待 E-gate 闭环后由 T4(文本)/ T5(图片)补进本文件。这里先落**零 DOM 假设**的部分:
下标计划、图数等式、话题提取、回读判据。所以本文件不 import playwright,单测不需要页面。

调用关系是 `note_components.set_note_components` 在 apply 阶段编排调用本文件(设计第二节
裁决的"结构性修正":单一提交路径不变,新逻辑不再堆进已 1541 行的 `note_components.py`)。
**因此本文件绝不 import `note_components`** —— 那会成环。下面 `extract_topics` 与
`_TOPIC_PATTERN` 是从 `note_components` 拷来的同款实现(出处见各自 docstring),拷贝的
理由就是这个环,不是没看见那边已有。两处将来要一起改。
"""

import re

from app.browser.atomic_tasks import XHS_MAX_TITLE_DISPLAY
from app.browser.text_formatter import get_display_length

# 正文里的话题实体形如 ``#身边的心理学[话题]#``。
# 出处:``note_components._TOPIC_PATTERN``(私有名,不 import;拷贝理由见模块 docstring)。
# T1 只读采集(设计附录 B / E9)实证:正文里话题 chip 的 ``innerText`` 就是这个纯文本
# 形态,所以同一条正则既能提"提交前旧正文里有哪些话题"(``topics_dropped``),也能提
# 页面回读的正文。
_TOPIC_PATTERN = re.compile(r"#([^#\[\]]+)\[话题\]#")


def plan_image_removal(indexes: list[int]) -> list[int]:
    """删除下标 → 实际下手顺序:去重后**降序**。

    降序是硬要求(设计 4.2③ / 4.3):删掉第 k 张之后,原来第 k+1..n 张的序号全部左移 1,
    此时再按原下标去删就删错图 —— 而误删不可逆。从大到小删,每次动的都是尚未左移的
    那一段,前面小下标的语义始终不变。

    下标合法性(1-based、1..expected_image_count、剩余原图 ≥1)由 REST 层
    ``NoteComponentsRequest._validate_note_edits`` 一律 422 拦死,这里**不重复校验也不
    夹取** —— 重复项在 REST 是 422(写重了多半是调用方算错图序),但真走到这里还是先
    去重,因为同一张删两次的后果是多删一张真图。
    """
    return sorted(set(indexes), reverse=True)


def image_count_equation(before: int, removed: int, added: int) -> int:
    """图数等式(设计 3.2):提交后**应该**剩几张 = 留底图数 - 实删数 + 追加数。

    这是 ``image_add`` / ``image_remove`` 两个 applied 键共用的同一条判据 —— 回读图数
    等于本函数的返回值才算生效。两键分别上报只是为了让调用方定位是哪半失败,判据是同一条。

    参数是**计数**不是下标:``removed`` 要传"实际删成了几张"(逐张删各自当场核验过 -1),
    不是请求里给了几个下标。计划态的预测请用 ``surviving_count``。
    """
    return before - removed + added


def surviving_count(current: int, remove: list[int], add: list[int]) -> int:
    """计划态预测:按请求意图改完之后一共该有几张图。

    与 ``image_count_equation`` 的分工:本函数吃**请求里的下标/图列表**(去重口径与
    ``plan_image_removal`` 一致,免得重复下标把预测算大),等式本身复用前者。

    注意这是**总数**(含追加)。设计 1.2 / 3.1 的"删除后剩余 ≥1"红线口径是**原图**剩余
    (``current - 实删数``),不许拿新增图凑数 —— 追加可能失败,失败后就是删光。那条校验
    在 REST 层,**别拿本函数的返回值去判它**。
    """
    return image_count_equation(current, len(plan_image_removal(remove)), len(add))


def extract_topics(body: str | None) -> list[str]:
    """从正文里提取话题实体名(``#身边的心理学[话题]#`` → ``身边的心理学``)。

    出处:``note_components.extract_topics`` 同款实现(拷贝理由见模块 docstring)。

    用途是设计 3.2 的 ``topics_dropped``:正文整体替换会把既有话题实体一并冲掉(本期
    明确不做重建,见 1.2),替换**前**用本函数把旧正文里的话题记下来如实上报。
    """
    return [m.strip() for m in _TOPIC_PATTERN.findall(body or "") if m.strip()]


def content_prefix_ok(read_back: str | None, target: str) -> bool:
    """正文生效判据(设计 3.2):回读正文归一后**以目标正文开头**。

    不能用全等:关联活动会把话题追加到正文末尾(设计 2.7① / 4.2①,正文步排在活动步之前),
    混合请求下回读值必然比目标长出一截话题。

    空白归一后比,是因为回读经 DOM 取文本,换行/缩进与输入侧不保证逐字节一致。

    目标归一后为空(``""`` / 纯空白)一律判 False:此时 ``startswith`` 恒真,会把"什么都
    没写进去"谎报成生效 —— 这条判据宁可漏报不可谎报。REST 侧 ``content`` 有
    ``min_length=1``,正常不会走到,但判据函数不靠上游守身。
    """
    target_norm = _norm(target)
    if not target_norm:
        return False
    return _norm(read_back).startswith(target_norm)


def title_length_ok(title: str) -> bool:
    """标题显示长度是否在小红书上限内(≤ ``XHS_MAX_TITLE_DISPLAY``)。

    度量直接复用发布链路的 ``text_formatter.get_display_length``(``len`` + 可见 emoji
    序列个数),REST 层 ``_validate_note_edits`` 用的是同一个函数 —— 两边口径必须是同一
    份实现,不然入口放行的标题在浏览器层被判超长。

    编辑场景**只判不截**(``truncate_by_display`` 存在但这里刻意不用):调用方给的是精确
    意图,而这是一次全量覆盖提交,截断 = 替他改错内容。超长在 REST 就是 422。

    空串是合法的(设计 3.1:``title=""`` = 清空标题,与 ``None`` 语义不同),长度 0 通过。
    """
    return get_display_length(title) <= XHS_MAX_TITLE_DISPLAY


def _norm(text: str | None) -> str:
    """空白归一(换行/多空格 → 单空格),便于回读值与目标值精确比对。

    与 ``note_components._norm`` 同款(同上,不 import 是为了不成环)。
    """
    return " ".join((text or "").split())
