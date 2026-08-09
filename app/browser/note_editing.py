"""已发布笔记编辑(标题 / 正文)的纯逻辑层 + 文本编辑的浏览器步骤。

设计 `docs/design/2026-08-03-note-editing-design.md`(3.2 判据 / 4.1 输入方式 / 4.2 顺序 /
4.4 失败语义)。

本文件两半:

- **纯函数**(下标计划、图数等式、话题提取、回读判据):零 DOM 假设,单测不需要页面;
- **文本编辑步骤**(`_type_into` / `apply_title_edit` / `apply_content_edit`,T4 补):
  HARD-GATE 的 E5(清空+逐字输入读回 100% 匹配)/ E7(未提交即恢复)已由 T2 受控写测试
  闭环(设计附录 C),故可实现。函数吃 `page` / `human` 入参、不持有状态,由 T6 在
  `set_note_components` 的 apply 阶段编排调用。

**图片增删(T5)不在本文件**,在 `note_editing_images.py` —— 分工按 worktree 并行拆的,
两边零交集,别往这边塞。

本文件仍不 import playwright:`page` / `human` 都按鸭子类型吃,单测用回放夹具 + 轻量
假页面就能覆盖。调用关系是 `note_components.set_note_components` 编排调用本文件(设计
第二节裁决的"结构性修正":单一提交路径不变,新逻辑不再堆进已 1541 行的
`note_components.py`)。**因此本文件绝不 import `note_components`** —— 那会成环。下面
`extract_topics` / `_TOPIC_PATTERN` / `_norm` 以及标题/正文的只读 JS,都是从
`note_components` 拷来的同款实现(出处见各自 docstring),拷贝的理由就是这个环,不是
没看见那边已有。两处将来要一起改。
"""

import re
import time

from loguru import logger

from app.browser.atomic_tasks import (
    XHS_MAX_TITLE_DISPLAY,
    XHS_MAX_TOPICS,
    topic_failure_detail,
)
from app.browser.text_formatter import get_display_length
from app.browser.topic_dropdown import COLLECT_LAYERS_JS, select_topic_option

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


def describe_diff(got: str, want: str) -> str:
    """定位两串的**首个差异点**并给两侧窗口——比截断前缀有诊断力。

    为什么必须有(2026-08-03 T7 实测):mismatch 只显示两边前 60 字时,"前缀相同、
    差异在后段"的失败(残留/截断/丢尾,恰是最常见形态)显示出来两边一模一样,
    零诊断力,真号连试两次都定位不了。首差异点 + 两侧窗口 + 双方长度一眼定性:
    got 比 want 长 = 残留;短 = 丢尾;同长不同字 = 字符转换。
    """
    if got == want:
        return "两串相等(不该走到这里)"
    n = min(len(got), len(want))
    i = next((k for k in range(n) if got[k] != want[k]), n)
    lo = max(0, i - 12)
    return (
        f"首差异@{i}(读回长{len(got)}/目标长{len(want)}):"
        f"读回[...{got[lo:i + 24]!r}...] vs 目标[...{want[lo:i + 24]!r}...]"
    )


def _norm(text: str | None) -> str:
    """空白归一(换行/多空格 → 单空格),便于回读值与目标值精确比对。

    与 ``note_components._norm`` 同款(同上,不 import 是为了不成环)。
    """
    return " ".join((text or "").split())


# ==================== 文本编辑的浏览器步骤(T4) ====================
#
# 选择器一律以真号夹具 ``tests/fixtures/pages/update_editor_images.json`` 为据
# (设计附录 B / T1 只读采集),回放测试 ``tests/test_note_editing_text.py`` 钉住
# "在真实快照上唯一命中";对不上只能重采夹具,不许改夹具迁就代码。

# 标题:更新页唯一命中、可见,**现值在 value 属性**(不是 innerText)。
_TITLE_INPUT = "input[placeholder*='标题']"
# 正文:tiptap/ProseMirror 富文本(不是 textarea)。比 ``_BODY_READ_JS`` 末尾那个裸
# ``div[contenteditable='true']`` 兜底更特异 —— **写入**必须用这个特异的:写错框
# (比如将来页面多出一个 contenteditable)就是把正文打进别的地方,而这是全量覆盖提交。
_BODY_EDITOR = "div.tiptap.ProseMirror[contenteditable='true']"

# 只读取证:按候选序列取第一个可见元素的矩形 + 视口高度。
#
# **不持 ElementHandle**(照抄 ``atomic_tasks._type_into_robust`` 的第一条实测纪律):
# 小红书创作页编辑器一聚焦即 React 重渲染,先前拿到的句柄指向的节点从 DOM 脱离,此后
# 无论 ``type_text`` 还是降级 ``fill`` 都抛 ``Element is not attached to the DOM``
# (历史 job2 / account1 实测复现均死在此)。所以每次尝试都重新 evaluate 取一次坐标。
#
# ``evaluate`` 在本模块只用于**读**(取矩形 / 取文本),绝不 JS 设值、绝不合成点击、
# 绝不 scrollIntoView —— JS 直填是"AI 托管"检测的典型信号,曾致账号被判违规禁发。
_BOX_JS = r"""(sels) => {
    for (const sel of sels) {
        const el = document.querySelector(sel);
        if (!el) continue;
        const r = el.getBoundingClientRect();
        if (r.width > 0 && r.height > 0) {
            return {x: r.x, y: r.y, w: r.width, h: r.height, sel: sel,
                    ih: window.innerHeight};
        }
    }
    return null;
}"""

# 只读回标题现值。与 ``note_components.read_note_title`` 同款语义(那边用
# ``query_selector().input_value()``,这里用只读 JS 少一次句柄往返),拷贝不 import 的
# 理由见模块 docstring 的防成环条款。找不到框返回 ``null`` —— 与"框在但值是空串"是两回事,
# 前者是定位失败,后者是标题真的空(``title=""`` 清空是合法路径)。
_TITLE_VALUE_JS = r"""(sel) => {
    const el = document.querySelector(sel);
    return el ? (el.value || '') : null;
}"""

# 只读回正文现值。候选序列与 ``note_components._BODY_TEXT_JS`` 同款(同上,拷贝不
# import),只在最前面多加了写入用的特异选择器 —— 读写用同一个元素才能保证"读回的就是
# 刚写的那个框"。
_BODY_READ_JS = r"""() => {
    const sels = ["div.tiptap.ProseMirror[contenteditable='true']",
                  "div[contenteditable='true'][data-placeholder*='正文']",
                  "div[contenteditable='true'][placeholder*='正文']",
                  "textarea[placeholder*='正文']",
                  "div[contenteditable='true']"];
    for (const s of sels) {
        const el = document.querySelector(s);
        if (el) return (el.innerText || el.value || '').trim();
    }
    return null;
}"""

# 定位/输入重试上限:与 ``_type_into_robust`` 同为 3。
# 清空重试上限:tiptap 多节点下一次 Ctrl+A 可能只选中部分,重清几次;仍不空转逐键退格
_CLEAR_TRIES = 3
# 逐键退格预算(清 atomic chip 用):话题 ≤10 个 + 少量残字,40 次绰绰有余;耗尽仍不空
# 说明结构异常(如嵌套只读块),那不是"再按几下"能解的,交回失败路径
_CHIP_BACKSPACE_BUDGET = 40
_TYPE_TRIES = 3
# 滚动进视口的尝试上限:与 ``note_components.click_publish`` 的滚动循环同为 3。
_SCROLL_TRIES = 3


def _click_point(box: dict) -> tuple[float, float]:
    """元素矩形 → 拟人点击落点(中心点)。"""
    return box["x"] + box["w"] * 0.5, box["y"] + box["h"] * 0.5


def _point_in_viewport(box: dict) -> bool:
    """**落点**(不是整块矩形)在视口内才算够得着。

    判的是落点而非整块:正文编辑器实拍 816x839,比视口还高,"整块进视口"的判据永远不
    成立,会白滚 3 次还把目标滚跑。我们只需要保证那一下点击落在视口里 —— 落点在外面时
    ``mouse.move`` 会被夹到视口边缘,点击静默落到别处(文字版发布"丢话题"同款陷阱)。
    """
    _, cy = _click_point(box)
    return 0 <= cy <= box["ih"]


def _locate(page, selectors: list[str]) -> dict | None:
    """只读取一次目标矩形;没命中返回 None。

    ``selectors`` 必须是 CSS(JS 侧走 ``querySelector``,XPath 传进来只会静默不命中)。
    """
    return page.evaluate(_BOX_JS, selectors)


def _scroll_into_view(page, human, selectors: list[str], *, intent: str) -> dict | None:
    """动手前把目标拟人滚进视口,返回**滚完之后重新取的**矩形;定位不到返回 None。

    设计附录 B / E8 是硬要求:实拍显示开一个弹窗就能把图片区顶到 ``rect.y = -815``
    (视口外上方)。也就是说**上一步拿到的坐标在下一步已经失效**,此时照旧坐标点击,
    落点是顶栏或页面空白,动作静默失败而代码看起来一切正常 —— 文字版发布丢话题/丢活动
    就是这么来的(02b44f6)。所以每个破坏性编辑步动手前都重新定位 + 重新滚。

    用 ``human.scroll`` 拟人滚(``page.evaluate`` 只读,不用 ``scrollIntoView``);方向按
    落点在视口上方还是下方决定 —— 只会向下滚的写法遇到"被顶到上方"这种 E8 实拍情形会
    越滚越远。

    滚满 ``_SCROLL_TRIES`` 次仍够不着就返回 None(**不硬着头皮点**)。这比
    ``click_publish`` 那条"滚不动也照点、靠 ``elementFromPoint`` 复核落点"更保守一档:
    文本步没有落点复核,而视口外坐标会被夹到边缘,点空之后紧跟着的 ``Ctrl+A`` +
    ``Backspace`` 就落到别的元素上了 —— 这一步宁可报"够不着"让编排层弃提交。
    """
    box = _locate(page, selectors)
    for _ in range(_SCROLL_TRIES):
        if box is None or _point_in_viewport(box):
            break
        _, cy = _click_point(box)
        human.scroll("up" if cy < 0 else "down")
        human.wait(0.3, 0.7, context=f"滚到{intent}")
        box = _locate(page, selectors)
    if box is None:
        logger.warning(f"[note_editing] 定位不到{intent}(选择器 {selectors})")
        return None
    if not _point_in_viewport(box):
        logger.warning(
            f"[note_editing] {intent}滚 {_SCROLL_TRIES} 次仍在视口外"
            f"(落点 y={_click_point(box)[1]:.0f}, 视口高 {box['ih']}),拒绝盲点"
        )
        return None
    return box


def _type_into(
    page,
    human,
    selectors: list[str],
    value: str,
    *,
    intent: str,
    clear_first: bool = True,
    read_current=None,
) -> tuple[bool, str | None]:
    """拟人化把 ``value`` 打进目标框。返回 ``(ok, 最后一次错误)``。

    仿写 ``atomic_tasks._type_into_robust``,**刻意不复用它**:那个方法绑死
    ``XHSPublishFlow`` 实例(``self.page / self.human / self._take_screenshot /
    self.current_step``),而且它是真号验证过的发布主路径 —— 与红质心"故意各写一份"
    同一取舍,不为了 DRY 去动它。**两份实现的坑注释互相引用,将来要改一起改。**

    照抄它的三条实测纪律:

    1. 每次尝试**重新定位取坐标、不持 ElementHandle**(React 聚焦重渲染会让旧句柄脱离,
       见 ``_BOX_JS`` 注释);
    2. 用 ``human.click(坐标)`` 拟人聚焦(贝塞尔移动 + 悬停 + 真实按压),不用
       ``element.click()`` / ``focus()``;
    3. ``human.type_text`` 逐字键盘输入(随机延迟 / 偶尔打错退格 / 标点稍慢)。

    与它唯一的语义差别在 ``clear_first``:发布页是空框,首次不清、只在重试时清残留;
    **编辑页框里是旧内容,首次就得清**(设计 4.1)。清空动作写成独立的两次 ``press_key``
    而不是借 ``type_text(clear_first=True)``,是因为 ``value=""``(清空标题,设计 3.1 的
    合法路径)时要"只清不输",拆开两步两条路径才共用得上,也才断言得了顺序。

    E5 受控写测试(设计附录 C)实证:清空 + 逐字输入后读回 100% 匹配,中文无异常。
    """
    last_err: str | None = None
    for attempt in range(1, _TYPE_TRIES + 1):
        try:
            box = _locate(page, selectors)
            if not box:
                last_err = f"未找到{intent}输入框"
                human.wait(0.4, 0.8, context="定位输入框重试")
                continue
            cx, cy = _click_point(box)
            human.click((cx, cy), reason=f"聚焦{intent}")
            human.wait(0.2, 0.5, context="聚焦后停顿")
            if clear_first:
                # Ctrl+A → Backspace 清空,**清完必须读回为空再输入**(2026-08-03 真号打脸):
                # E5 受控写只测了标题(input 单行,Ctrl+A 可靠),正文没测——tiptap 富文本的
                # Ctrl+A 在多节点(段落+话题 chip)下可能只选中部分,残留旧内容;此时继续
                # 输入,读回 = 新文本(前缀相同)+ 旧残留(后段不同),mismatch 只显示相同
                # 前缀毫无诊断力,T7 真号连挂两次才定位到这。清不干净就再清,清空封顶
                # _CLEAR_TRIES 次;仍不空 → 本次尝试失败(绝不在残留上输入)。
                cleared = False
                for _ in range(_CLEAR_TRIES):
                    human.press_key("Control+a", reason=f"全选{intent}原内容")
                    human.press_key("Backspace", reason=f"清空{intent}")
                    human.wait(0.2, 0.5, context="清空后停顿")
                    if read_current is None:
                        cleared = True  # 没给读法的框(理论不该有)保持旧行为
                        break
                    left = read_current(page)
                    if not _norm(left or ""):
                        cleared = True
                        break
                if not cleared and read_current is not None:
                    # 第二阶段:**逐键退格删 atomic 节点**(2026-08-03 真号确诊):
                    # tiptap 的话题 chip 是 atomic node,Ctrl+A 选不中它们——三轮全选删除
                    # 后残留一字不变('#身边的心理学[话题]# #明日方舟[话题]#')就是铁证。
                    # atomic 节点吃单独的 Backspace(一次删一个),所以光标移到文末逐键退格,
                    # 每几键读一次,读空即止。预算 _CHIP_BACKSPACE_BUDGET 封顶:残留是
                    # chip 时按 chip 个数消耗,预算耗尽仍不空 = 结构异常,交回失败路径。
                    human.press_key("Control+End", reason=f"光标移到{intent}末尾(清残留)")
                    for i in range(_CHIP_BACKSPACE_BUDGET):
                        human.press_key("Backspace", reason="")
                        if (i + 1) % 4 == 0:
                            human.wait(0.15, 0.35, context="退格间隔")
                            if not _norm(read_current(page) or ""):
                                cleared = True
                                break
                    if not cleared:
                        human.wait(0.2, 0.4, context="末次退格后读回")
                        cleared = not _norm(read_current(page) or "")
                if not cleared:
                    last_err = (
                        f"{intent}清空失败:{_CLEAR_TRIES} 次 Ctrl+A+Backspace 后仍残留 "
                        f"{_norm(read_current(page) or '')[:40]!r}"
                    )
                    continue
            if value:
                human.type_text(None, value, click_first=False, clear_first=False)
            logger.info(
                f"[note_editing] {intent}拟人输入完成 selector={box['sel']} "
                f"({len(value)}字, 第{attempt}次尝试)"
            )
            return True, None
        except Exception as exc:  # noqa: BLE001 — 定位/输入的失败原因原样上报给编排层
            last_err = str(exc)
            human.wait(0.5, 1.0, context="填入重试")
    return False, last_err


def read_title_value(page) -> str | None:
    """只读回标题框现值;**定位不到返回 None**(与"标题真的是空串"区分)。"""
    try:
        return page.evaluate(_TITLE_VALUE_JS, _TITLE_INPUT)
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[note_editing] 标题读取失败(当作读不到): {exc}")
        return None


def read_body_value(page) -> str | None:
    """只读回正文现值;定位不到 / 读失败返回 None。"""
    try:
        return page.evaluate(_BODY_READ_JS)
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[note_editing] 正文读取失败(当作读不到): {exc}")
        return None


def apply_title_edit(page, human, new_title: str) -> dict:
    """整体替换标题:滚进视口 → 留底 → 清空重填 → 当场读回全等校验。

    Args:
        page: 已停在更新页的同步 Playwright Page。
        human: ``SyncHumanActions``(全程拟人,红线)。
        new_title: 目标标题;``""`` 合法,表示清空标题(设计 3.1,与 ``None`` 不同,
            ``None`` 是"不改",由编排层决定压根不调本函数)。

    Returns:
        ``{"status": "done"|"error", "reason"?: str, "title_before": str|None,
        "title_read_back": str|None}``。

    读回不等一律 ``error``:此时编辑器里是**残缺态**(清了一半 / 打了一半),提交出去就是
    把残缺真发布。怎么处置(设计 4.4 的弃提交)是编排层 T6 的事,本函数只如实报状态,
    不点发布、不做补救。
    """
    title_before = read_title_value(page)
    box = _scroll_into_view(page, human, [_TITLE_INPUT], intent="标题框")
    if box is None:
        return {
            "status": "error",
            "reason": f"title_input_not_found: 选择器 {_TITLE_INPUT} 未命中或滚不进视口",
            "title_before": title_before,
            "title_read_back": None,
        }

    ok, err = _type_into(
        page, human, [_TITLE_INPUT], new_title, intent="标题",
        read_current=read_title_value,
    )
    if not ok:
        return {
            "status": "error",
            "reason": f"title_type_failed: {err}",
            "title_before": title_before,
            "title_read_back": read_title_value(page),
        }

    human.wait(0.3, 0.8, context="等标题渲染稳定")
    read_back = read_title_value(page)
    # **读回 None(定位失败/evaluate 异常)一律 error,不进全等比较**(fable 验收必修项):
    # 否则 title="" 清空路径下 _norm(None)=="" 与 _norm("") 相等,"根本没读到"会被误判成
    # "清空成功" —— 违反本模块"宁可漏报不可谎报"。content 因 min_length=1 天然免疫,
    # 只有清空标题这一条路径踩得到,恰恰它又是合法意图,必须堵死。
    if read_back is None:
        return {
            "status": "error",
            "reason": "title_readback_unavailable: 输入后读不回标题值,无法确认是否生效",
            "title_before": title_before,
            "title_read_back": None,
        }
    # 全等(空白归一后)才算成:标题没有"末尾会被追加东西"的情形,不存在正文那种前缀语义
    if _norm(read_back) != _norm(new_title):
        return {
            "status": "error",
            "reason": (
                f"title_readback_mismatch: 读回 {(read_back or '')[:40]!r} "
                f"!= 目标 {new_title[:40]!r}"
            ),
            "title_before": title_before,
            "title_read_back": read_back,
        }
    return {
        "status": "done",
        "title_before": title_before,
        "title_read_back": read_back,
    }


def apply_content_edit(page, human, new_content: str) -> dict:
    """整体替换正文:滚进视口 → 留底(含话题) → 清空重填 → 当场读回全等校验。

    Returns:
        ``{"status": "done"|"error", "reason"?: str, "body_before": str|None,
        "topics_dropped": list[str], "body_read_back": str|None}``。

    ``topics_dropped`` 取自**替换前**的旧正文:正文整体替换会把既有话题实体一并冲掉,
    本期明确不重建(设计 1.2),只如实上报丢了哪些(设计 3.2)。

    **判据是编辑器内全等,不是 ``content_prefix_ok``**,两条别混用:

    - 这里是**写完当场**在编辑器里读,活动步排在正文步之后(设计 4.2①),此刻还没有任何
      话题被注进正文末尾 —— 全等成立,用前缀会把"只写进去一半"放过去;
    - ``content_prefix_ok`` 是**提交后重进页面**的回读判据(设计 3.2),那时活动步已经把
      话题追加到末尾,回读必然比目标长一截,只能用前缀。

    读回不等也兜住了 E6 未闭环的那一半:若 ``Ctrl+A`` 没把旧话题 chip 选中删掉,残留会让
    全等失败 → error → 编排层弃提交,残缺态不会被发出去。
    """
    body_before = read_body_value(page)
    topics_dropped = extract_topics(body_before)
    box = _scroll_into_view(page, human, [_BODY_EDITOR], intent="正文框")
    if box is None:
        return {
            "status": "error",
            "reason": f"body_editor_not_found: 选择器 {_BODY_EDITOR} 未命中或滚不进视口",
            "body_before": body_before,
            "topics_dropped": topics_dropped,
            "body_read_back": None,
        }

    ok, err = _type_into(
        page, human, [_BODY_EDITOR], new_content, intent="正文",
        read_current=read_body_value,
    )
    if not ok:
        return {
            "status": "error",
            "reason": f"content_type_failed: {err}",
            "body_before": body_before,
            "topics_dropped": topics_dropped,
            "body_read_back": read_body_value(page),
        }

    human.wait(0.3, 0.8, context="等正文渲染稳定")
    read_back = read_body_value(page)
    if _norm(read_back) != _norm(new_content):
        return {
            "status": "error",
            "reason": (
                "content_readback_mismatch: "
                + describe_diff(_norm(read_back), _norm(new_content))
            ),
            "body_before": body_before,
            "topics_dropped": topics_dropped,
            "body_read_back": read_back,
        }
    return {
        "status": "done",
        "body_before": body_before,
        "topics_dropped": topics_dropped,
        "body_read_back": read_back,
    }


# ==================== 已发布笔记补挂话题(追加语义) ====================
#
# 需求 docs `2026-08-08 server需求-已发布笔记补挂话题`:存量视频笔记发布时话题 7/8 全空,
# 发布链路已修但存量丢的话题救不回来,需要编辑侧**补挂**能力。语义是**追加**不是替换:
# 传入话题追加到现有话题、去重、总数 >10 截断(全量替换在已有话题的笔记上太危险,一次
# 手滑清空别人的话题)。话题输入复用发布链那套「输 #话题名 → 浮层 → 正向判据选精确匹配」
# (``topic_dropdown.select_topic_option``,绝不走已定性有缺陷的旧「面积最小」近似)。


def normalize_topic_names(topics: list[str] | None) -> list[str]:
    """规整话题名:去前导 ``#``、去首尾空白、丢空串、按**首次出现**去重(保序)。

    ``extract_topics`` 出来的既有话题本就没有 ``#`` 前缀,调用方传的可能带可能不带,
    统一到"纯名字"这一种形态才比得了(追加语义的去重全靠它)。
    """
    out: list[str] = []
    seen: set[str] = set()
    for t in topics or []:
        name = (t or "").lstrip("#").strip()
        if name and name not in seen:
            seen.add(name)
            out.append(name)
    return out


def plan_topic_appends(
    existing: list[str] | None,
    requested: list[str] | None,
    cap: int = XHS_MAX_TOPICS,
) -> dict:
    """算出真正要补的话题**差集**(追加语义的纯逻辑,零 DOM 假设,单测直接覆盖)。

    追加语义三步:
    1. **去重**:``requested`` 规整成纯名字并去重;
    2. **剔除已在**:已经挂在笔记上的(``existing``)不再补——这就是"原话题保留、不重复挂"
       (验收:job278 已有 1 个,补 4 个后应是 5 个,原 1 个保留);
    3. **总数封顶 cap**:``existing`` 占掉的名额先扣掉,``requested`` 里的新话题只补到把
       总数填满 ``cap``(小红书单篇 ≤10)为止,补不下的进 ``truncated`` 如实回执。

    Returns:
        ``{"existing":[...规整后...], "to_add":[...要补且补得下的...],
        "truncated":[...因超上限没补的...], "already":[...请求里本就已挂的...]}``。
        ``to_add`` 保持 ``requested`` 的原始顺序(先来先补,截断截的是靠后的)。
    """
    existing_names = normalize_topic_names(existing)
    existing_set = set(existing_names)
    requested_names = normalize_topic_names(requested)
    already = [t for t in requested_names if t in existing_set]
    new = [t for t in requested_names if t not in existing_set]
    available = max(0, cap - len(existing_names))
    return {
        "existing": existing_names,
        "to_add": new[:available],
        "truncated": new[available:],
        "already": already,
    }


# 补话题:聚焦正文框的焦点判据(只读 ``document.activeElement``,与 atomic_tasks step6
# 同款——不验焦点就打字 = 把话题打进虚空还以为是"下拉没匹配")。
_BODY_FOCUS_JS = r"""() => {
    const ae = document.activeElement;
    if (!ae) return false;
    if (ae.getAttribute && ae.getAttribute('contenteditable') === 'true') return true;
    return !!(ae.closest && ae.closest("[contenteditable='true']"));
}"""

# 聚焦正文框的尝试次数(与 atomic_tasks step6 同为 2)
_FOCUS_TRIES = 2


# 聚焦失败取证(RCA 2026-08-09 号6播客):补话题第一步就报 ``content_box_focus_failed`` 时,
# 回执里只有一个原因码,分不清是**正文框选择器压根没命中**(该笔记编辑页结构异常/平台改版)
# 还是**命中了但点完焦点没落进去**(时序太早/被顶出视口)——两者处置天差地别。同期号1/号8
# 播客同选择器成功,故这不是播客页选择器普遍不同,是该笔记特异,不硬猜、只把当场证据带回:
# 主选择器是否命中、页面上有几个 contenteditable、命中框的矩形与视口高、以及是否滚进过视口。
#
# 首轮取证把范围收窄到"矩形命中了、也滚进了视口,焦点还是留在 body",于是加**落点反查**
# (``pt`` = 最后一次真点下去的坐标):``elementFromPoint`` 那一下究竟点在了谁身上、是不是
# 编辑器内部。这两个字段是**临时诊断字段**,底栏吞点击这个候选一旦被真号数据坐实/排除就该
# 撤掉(保质期挂在 guide 的 KNOWN_LIMITATIONS 里,别靠人记)。
# 链长度双闸(单层 ≤60 / 全链 ≤300)是临时取证的硬上限纪律:证据要够用,不许把整页 DOM
# 拖进回执。
_FOCUS_FORENSICS_JS = r"""(pt) => {
    const sel = "div.tiptap.ProseMirror[contenteditable='true']";
    const primary = document.querySelector(sel);
    const anyCe = document.querySelectorAll("[contenteditable='true']");
    const rect = primary ? primary.getBoundingClientRect() : null;
    const ae = document.activeElement;
    let chain = null;
    let inside = null;
    if (Array.isArray(pt) && pt.length === 2) {
        let el = document.elementFromPoint(pt[0], pt[1]);
        inside = !!(primary && el && primary.contains(el));
        const parts = [];
        for (let i = 0; i < 5 && el; i++) {   // 落点元素本身 + 最多 4 层祖先
            const cls = typeof el.className === 'string' ? el.className.slice(0, 40) : '';
            const tag = String(el.tagName || '').toLowerCase();
            parts.push((cls ? tag + '.' + cls : tag).slice(0, 60));
            el = el.parentElement;
        }
        // 落点在视口外时 elementFromPoint 返回 null:链留 null 而不是空串,
        // 好与"点上了元素但链读不出"区分开
        chain = parts.length ? parts.join(' < ').slice(0, 300) : null;
    }
    return {
        primary_matched: !!primary,
        contenteditable_count: anyCe.length,
        primary_rect: rect ? {x: Math.round(rect.x), y: Math.round(rect.y),
                              w: Math.round(rect.width), h: Math.round(rect.height)} : null,
        viewport_h: window.innerHeight,
        active_tag: ae ? String(ae.tagName || '').toLowerCase() : null,
        point_element_chain: chain,
        point_inside_editor: inside,
    };
}"""


def _focus_forensics(page, point: tuple[float, float] | None) -> dict:
    """聚焦失败时抓一份当场证据;读不到只回最小骨架(取证绝不制造新异常)。

    ``point`` 是**最后一次真点下去的坐标**,页面据它反查落点身上是谁
    (``point_element_chain`` / ``point_inside_editor``)——没有它,"矩形命中了、焦点却
    没进去"只能靠猜。一次都没点成(定位不到框)时传 ``None``,这两个字段留空。
    """
    try:
        data = page.evaluate(_FOCUS_FORENSICS_JS, list(point) if point is not None else None)
        return dict(data) if isinstance(data, dict) else {"probe_error": "non_dict"}
    except Exception as exc:  # noqa: BLE001
        return {"probe_error": str(exc)[:120]}


# 聚焦落点在正文框**可见区带**里的相对高度:0.33 = 偏上三分之一(见 ``_focus_click_point``)
_FOCUS_BAND_RATIO = 0.33


def _focus_click_point(box: dict) -> tuple[float, float]:
    """正文框矩形 → 聚焦点击落点(**可见区带内偏上**,不是矩形死中心)。

    RCA 2026-08-09(号6播客 note 6a75fa99,三次复现):取证 ``primary_matched=true``、
    ``primary_rect={x:442, y:592, w:632, h:260}``、``viewport_h=794``、滚进视口也为真 ——
    框底 852 探出视口,死中心落点 (758, 722) 离视口底只剩 72px,正好压在编辑更新页底部那条
    固定操作栏上,点击被它吞掉、焦点留在 body(与 ``note_components.click_publish`` 加
    ``elementFromPoint`` 复核时踩过的是同一片透明区)。笔记级绑定也对得上:该笔记正文全场
    最长(182 字符 / 9 行)把编辑器矩形撑得最靠下,而拟人滚动的判据是"落点一进视口就停",
    于是唯独这篇的中心停进了底栏区;其余 7 篇正文短、落点高,全绿。

    改法:落点收进**可见区带**(矩形与视口的交集)并取偏上 ``_FOCUS_BAND_RATIO`` 处,
    水平仍走中心。落点在框内哪个位置**没有语义** —— 聚焦成功后紧跟 ``Control+End`` 把光标
    移到正文末尾,点可见区内任意一处等效,所以这里可以自由挑一个几何上更安全的点。
    """
    visible_top = max(box["y"], 0)
    visible_bottom = min(box["y"] + box["h"], box["ih"])
    cy = visible_top + (visible_bottom - visible_top) * _FOCUS_BAND_RATIO
    return box["x"] + box["w"] * 0.5, cy


def _focus_body_end(page, human) -> tuple[bool, dict]:
    """拟人滚进视口 → 点击聚焦正文框 → **验焦点** → 光标移末尾。

    返回 ``(ok, forensic)``:``ok=True`` 时 ``forensic`` 为空 dict;失败时 ``forensic`` 带
    当场证据(见 ``_FOCUS_FORENSICS_JS``),供调用方塞进 ``content_box_focus_failed`` 回执,
    把这个黑箱失败变成下一次可诊断的事件(RCA 2026-08-09 号6播客两单全败无从下手)。

    与 atomic_tasks step6 同款纪律(2026-08-03 文字版事故):正文框可能被上传预览顶出
    视口,``getBoundingClientRect`` 中心落在顶栏,点击根本没进正文框,后续 ``#话题`` 打进
    虚空、下拉永不弹。所以每次都重新滚 + 重新取坐标 + 点完只读验 ``activeElement``。
    """
    scrolled_into_view = False
    last_click: tuple[float, float] | None = None
    for _ in range(_FOCUS_TRIES):
        box = _scroll_into_view(page, human, [_BODY_EDITOR], intent="正文框(补话题)")
        if box is None:
            continue
        scrolled_into_view = True
        cx, cy = _focus_click_point(box)
        last_click = (cx, cy)
        human.click((cx, cy), reason="聚焦正文框(补话题)")
        human.wait(0.2, 0.5, context="聚焦后停顿")
        try:
            if bool(page.evaluate(_BODY_FOCUS_JS)):
                human.press_key("Control+End", reason="光标移到正文末尾(补话题)")
                return True, {}
        except Exception:  # noqa: BLE001 — 读焦点失败当没聚焦上,下一轮重滚重点
            pass
        logger.warning("[note_editing] 聚焦正文框后焦点不在编辑区,重滚动重点")
    forensic = _focus_forensics(page, last_click)
    forensic["scrolled_into_view"] = scrolled_into_view
    logger.warning(
        f"[note_editing] 补话题聚焦正文框失败 | 选择器命中={forensic.get('primary_matched')} "
        f"contenteditable数={forensic.get('contenteditable_count')} "
        f"矩形={forensic.get('primary_rect')} 视口高={forensic.get('viewport_h')} "
        f"滚进视口={scrolled_into_view} 落点={last_click} "
        f"落点在编辑器内={forensic.get('point_inside_editor')} "
        f"落点元素链={forensic.get('point_element_chain')}"
    )
    return False, forensic


# ── 补话题:等联想浮层的条件轮询预算(RCA 2026-08-09)──
# 旧实现是"打完字 → 定长等 1.5~2.5s → 只看一眼"。真号复验实证这一眼会漏:同一个词在
# 第 2 位补得上、在第 5 位补不上(位置败,不是词败),说明浮层是**晚到**而不是不来。
# 定长单次快照对晚到的浮层零容错,轮询才是对症的。
# 预算 8s / 最多 8 tick 两道闸都在:预算防"页面卡住时干等",tick 数防"每 tick 都被
# 拟人 wait 抽成 0 秒时空转"(测试里 human.wait 是空操作,没有 tick 闸就是死循环)。
_TOPIC_POLL_BUDGET_S = 8.0
_TOPIC_POLL_MAX_TICKS = 8
# 首 tick 等久一点(浮层通常很快就到,先给它一次从容的机会),之后短间隔快速复查
_TOPIC_POLL_FIRST_WAIT = (1.4, 1.8)
_TOPIC_POLL_NEXT_WAIT = (0.8, 1.2)


def _poll_topic_dropdown(page, human, tag_name: str) -> tuple[dict, list[dict]]:
    """输完 ``#话题`` 后**条件轮询**等话题联想浮层,弹出并命中即返回,不再只看一眼。

    返回 ``(option, poll_timeline)``:

    - ``option`` 是 ``select_topic_option`` 的原样结果(命中时 ``success=True``,超预算时
      是**最后一 tick** 的失败判定,带它那一刻的取证字段);
    - ``poll_timeline`` 是每个失败 tick 的 ``{tick, elapsed_s, layers_seen, reason}``,
      失败时挂进回执 —— 它既是修复也是取证:真因两个候选(浮层异步渲染晚到 / chip 累积后
      光标几何变化把浮层挤出锚定区间)轮询对前者直接治愈,对后者能靠时间线钉死
      ("8 tick 全程 layers_seen 稳定非 0 但 reason 恒为 not_found" = 几何锚拒错了)。

    等待一律走 ``human.wait`` 保持拟人节奏(裸 sleep 是风控特征,本仓禁);每 tick 都
    **重新取**正文框矩形当几何锚,不持旧句柄(理由同 ``_focus_body_end``:重渲染会让旧
    句柄脱离)。
    """
    started = time.monotonic()
    deadline = started + _TOPIC_POLL_BUDGET_S
    timeline: list[dict] = []
    option: dict = {}
    for tick in range(1, _TOPIC_POLL_MAX_TICKS + 1):
        low, high = _TOPIC_POLL_FIRST_WAIT if tick == 1 else _TOPIC_POLL_NEXT_WAIT
        human.wait(low, high, context="等待话题下拉")
        box = _locate(page, [_BODY_EDITOR]) or {}
        editor_rect = (
            {"x": box.get("x"), "width": box.get("w")} if box.get("x") is not None else None
        )
        option = select_topic_option(
            page.evaluate(COLLECT_LAYERS_JS, tag_name), tag_name, editor_rect=editor_rect
        )
        if option and option.get("success"):
            return option, timeline
        timeline.append({
            "tick": tick,
            "elapsed_s": round(time.monotonic() - started, 1),
            "layers_seen": (option or {}).get("layers_seen", 0),
            "reason": (option or {}).get("reason", "error"),
        })
        if time.monotonic() >= deadline:
            break
    return option, timeline


def append_topics(page, human, to_add: list[str]) -> dict:
    """在已打开更新页的正文框**末尾追加**话题(逐个 输 #话题 → 等浮层 → 正向判据点选)。

    ``to_add`` 是调用方(编排层)已经用 ``plan_topic_appends`` 算好的**追加差集**——本函数
    不再去重不再截断,只负责把它们逐个真挂上去。逐个独立成败:

    - 命中精确匹配 → 拟人点选,生成**真话题实体**(蓝色 chip,参与分发),记进
      ``in_editor_added``;
    - 没命中(下拉没弹 / 词不存在 / 抓错容器)→ **Escape + 回删**刚打进去的 ``#话题``,
      **绝不留残缺文本**(残缺话题会撑爆 10 个上限拦发布,RCA 2026-05-18),失败进
      ``failed`` 带当场证据(``topic_failure_detail``),**不连坐其余话题**。

    话题是**追加**而非破坏性编辑:失败只是"这个没挂上",正文原内容无损,故本步失败**不弃
    提交**(与标题/正文/图片那类破坏性编辑步的语义差异见编辑设计 4.4)。

    Returns:
        ``{"status": "done"|"partially_applied"|"error"|"skipped",
        "in_editor_added": [...编辑器内点选成功的...], "failed": [{tag, reason, ...证据}]}``。
        ``in_editor_added`` 是**编辑器内**的乐观结果(点选上了),**不等于**平台真挂上——
        那要靠编排层提交后重进页面回读平台实况确认(这条产品线的失败是静默的)。
    """
    if not to_add:
        return {"status": "skipped", "in_editor_added": [], "failed": []}
    focused, focus_forensic = _focus_body_end(page, human)
    if not focused:
        # 聚焦不上 = 一个都打不进去,全体失败(但正文原样未动,可整体重试)。带聚焦取证,
        # 让 content_box_focus_failed 不再是黑箱(RCA 2026-08-09 号6播客)。
        return {
            "status": "error",
            "in_editor_added": [],
            "failed": [
                {"tag": t, "reason": "content_box_focus_failed", **focus_forensic}
                for t in to_add
            ],
        }
    added: list[str] = []
    failed: list[dict] = []
    for tag in to_add:
        tag_name = str(tag).lstrip("#").strip()
        # 每个话题输入前补一个空格分隔(``#`` 与前一个话题实体/正文分开)。这是**保留的
        # 卫生措施,不是根因修复**:它曾被当成追加场景失败的根因("``#`` 粘在 chip 边界上
        # 编辑器不弹浮层"),但 RCA 2026-08-09 真号复验推翻了——加空格后 tail 确实变成
        # ``[话题]# #失眠``,浮层照样不弹。空格本身无害(从零场景多一个空格小红书会吞),
        # 且让回删长度语义清晰,故留着。
        # 真因是**浮层时序/锚定**:同一个词第 2 位成功、第 5 位失败 = 位置败不是词败。
        # 下面的条件轮询既是对"浮层晚到"的修复,也是对"chip 累积后光标几何变化"这个
        # 另一候选的取证(poll_timeline 会把每 tick 看到的层数与判定摊开)。
        typed_text = f" #{tag_name}"
        human.type_text(None, typed_text, click_first=False)
        option, poll_timeline = _poll_topic_dropdown(page, human, tag_name)
        if option and option.get("success"):
            human.click(
                (option["x"], option["y"]),
                reason=f"点选话题: {option.get('matched', '')[:20]}",
            )
            logger.info(f"[note_editing] ✓ 补话题下拉点选成功: {option.get('matched')!r}")
            added.append(tag_name)
        else:
            # 回删**之前**先取证(回删后正文框就看不出打进去过什么了)
            detail = topic_failure_detail(tag_name, option, (read_body_value(page) or "")[-40:])
            # topic_failure_detail 是白名单式取键,轮询时间线得显式挂上去
            detail["poll_timeline"] = poll_timeline
            logger.info(
                f"[note_editing] 补话题未选中({detail['reason']}),等满 "
                f"{len(poll_timeline)} 轮仍未弹/未命中,回删不插入 | "
                f"候选 {detail.get('candidates')} | 正文末尾「{detail['editor_tail']}」"
            )
            failed.append(detail)
            try:
                page.keyboard.press("Escape")
                # 连分隔空格一起回删干净:只删 ``#tag`` 会留下空格残留,逐个失败累积会撑坏
                # 正文(``len(typed_text)`` 已含开头那个分隔空格)。
                for _ in range(len(typed_text)):
                    page.keyboard.press("Backspace")
            except Exception as exc:  # noqa: BLE001 — 回删失败只记一笔,不阻断其余话题
                logger.info(f"[note_editing] 回删话题异常: {exc}")
        human.wait(0.8, 1.5, context="话题处理")
    if added and not failed:
        status = "done"
    elif added:
        status = "partially_applied"
    else:
        status = "error"
    return {"status": status, "in_editor_added": added, "failed": failed}
