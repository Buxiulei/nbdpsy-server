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

from loguru import logger

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
                # Ctrl+A → Backspace:E5 实证能把旧内容清干净
                human.press_key("Control+a", reason=f"全选{intent}原内容")
                human.press_key("Backspace", reason=f"清空{intent}")
                human.wait(0.2, 0.5, context="清空后停顿")
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

    ok, err = _type_into(page, human, [_TITLE_INPUT], new_title, intent="标题")
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

    ok, err = _type_into(page, human, [_BODY_EDITOR], new_content, intent="正文")
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
                f"content_readback_mismatch: 读回 {(read_back or '')[:60]!r} "
                f"!= 目标 {new_content[:60]!r}"
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
