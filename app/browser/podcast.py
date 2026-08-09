"""播客(发播客 tab)页面的浏览器动作:tab 三件套 + 引导浮层 + 播客合集创建。

选择器的取证状态**分两档**,别混着读:

**已真号取证**(2026-08-07 与 2026-08-08 两轮,账号9,
``data/scene_captures/podcast/`` 与 ``data/scene_captures/podcast_selectors/``):
- 顶部 4 个 tab 都是 ``div.creator-tab``,当前激活的额外带 ``active`` 类;
  ⚠️ 点「发播客」后 URL 只追加 ``&from=tab_switch``(一次性来源标记,**不是**可复用的
  mode 参数)——所以判据只认 DOM,**不做 URL 兜底**(与图文 tab 的 ``?type=normal``
  兜底刻意不同,那条路在播客上不存在);
  ⚠️⚠️ 08-08 取证推翻了"active 唯一"这个隐含假设:DOM 里**同时**存在两个
  ``.creator-tab.active`` —— 一个残留在「上传视频」上没被摘掉、一个正确挂在
  「发播客」上,而 ``document.querySelector`` 取文档序第一个、抓到的正是错的那个
  (那一轮 3 次会话 ``ensure_podcast_tab`` 全程判定失败,页面其实早就是播客上传区)。
  故判据改成:读**所有** active 的文案取并集,再叠一路独立的**内容判据**兜底;
- 「上传音频」红按钮 ``button.upload-button``;右侧 RSS 是 ``button.rss-button``;
- 「播客合集」区入口文案「新建播客合集」,**点击后是整页内容替换,不是 modal**
  ——所以它与"有没有传音频"完全解耦,可以独立触达;
- 合集创建页三个字段与「创建」按钮的禁用判据(见下方常量);
- 合集封面 accept 实测为 ``.jpg,.jpeg,.png,.webp`` —— **含 webp**,与设计文档
  「合集封面无 webp」的假设相反,以实测 DOM 为准;
- 发布表单(``publish/publish?...&target=audio``)的标题 / 正文 / 内容设置区,
  以及**播客合集卡的真实文案是「加入播客合集」**(旧占位猜的「播客合集 / 选择合集 /
  加入合集」三个全部落空)。

**08-09 补录(真号 7 单假绿的 observed 实录,判据据此重写、但修法尚未真号复验)**:
- 「创建」按钮的**真实禁用形态**是 class 里一个**裸 token** ``disabled``(常与
  ``d-button-primary-loading`` 同现),按钮**没有** disabled 属性、也**没有**
  ``create-btn-disabled`` —— 旧判据两条全落空,详见 ``create_button_state``;
- 合集创建**表单右侧渲染一张实时预览卡**,把刚打进输入框的名字原样回显
  (「<合集名> / 播客 / 更新至0集 / 0人听过」)。所以"页面文本里有这个名字"在表单
  开着时**是伪证**,详见 ``_read_create_result``;
- 失败时**表单不会自己关**:表单收没收起,是目前最硬的那条成败分水岭。

**仍未取证(占位值,fail-loud)**:
- 合集卡点开之后的候选结构(下拉?二级弹窗?)—— 为控制真号操作范围没有再点;
- 底部固定栏「暂存离开」+「发布」的精确选择器:取证只按 ``[class*=publish-btn]``
  查过、**未命中**,截图确认按钮在。注意这**不能**推出"播客页与图文/视频页不同款"
  —— 图文/视频用的是 ``<xhs-publish-btn>`` 自定义元素(closed shadow),本来就不会被
  一个 class 查询命中。播客页到底是不是同一个 host,**没验过**;
- 接近 2 小时上限的长音频有没有额外的转码等待(取证用的是 10 分 15 秒 / 2.35MB)。

**引导浮层**(本页最大的坑):「播客合集上线啦」的 popover **正压在「上传音频」按钮上**。
08-08 取证把四种关闭手段(容器内 ``.close-btn`` / Esc / 点空白 / 精确重定位再点)
**全部实测无效**——``present_after`` 四次全 True、四张截图像素级无变化。走通的是**绕过**
而不是关闭:浮层只压住 120px 宽按钮的左侧约 81px,右侧留一条约 39px 的暴露缝,精确点
这条缝能穿透浮层点中按钮、弹窗正常打开(见 ``exposed_click_point``)。这条缝的宽度是
**当次窗口尺寸下的实测值**,尺寸/文案一变随时可能归零 —— 所以算不出缝就退回直接点按钮,
而 ``dismiss_guide_tooltip`` 的四招一个不删(平台哪天修好了它就又能关掉)。
"""

import time
from typing import Any, Dict, List, Optional, Tuple

from loguru import logger

from app.browser.sync_human_actions import SyncHumanActions

# ── 已取证:tab ──
PODCAST_TAB_TEXT = "发播客"
_CREATOR_TAB = "div.creator-tab"
# 内容判据:发播客 tab 首屏上传区的文案,别的 tab 上不存在。
# 它是 class 判据之外**独立的一路**信号 —— 08-08 取证脚本正是靠它绕过了 active 误判。
_PODCAST_CONTENT_MARKER = "将音频文件拖拽到此"

# ── 已取证:发播客 tab 首屏 ──
UPLOAD_AUDIO_BUTTON = "button.upload-button"
_COLLECTION_ENTRY_TEXT = "新建播客合集"

# ── 已取证:合集创建页 ──
_NAME_INPUT = 'input.d-text[placeholder*="合集名称"]'
_DESC_INPUT = 'textarea.d-text[placeholder*="合集简介"]'
_COVER_INPUT = 'input.upload-input[type="file"]'
# 「创建」按钮:**必须限定 <button> 标签** —— 外层包裹 div.footer-btn-area 的 innerText
# 也是「创建」,按纯文本找会抓到那个容器(点它不生效,且读不到 disabled)。
_CREATE_BUTTON = "button.create-btn"
_CREATE_DISABLED_CLASS = "create-btn-disabled"
# 真号取到的**第二种**禁用形态(RCA 2026-08-09,7 单假绿):class 里挂一个**裸 token**
# ``disabled``,按钮既没有 disabled 属性、也没有 ``create-btn-disabled``。
# 必须按空白切分后**整词**比较:substring 判法会被 ``create-btn-disabled``(另有判据)、
# 以及将来任何 ``xxx-disabled`` / ``disabled-y`` 类名带偏,把按钮永久判死。
_CREATE_DISABLED_TOKEN = "disabled"
# loading 态(同一单实测与裸 disabled 同时出现):封面多半还在上传/处理,点了也白点。
_CREATE_LOADING_TOKEN = "d-button-primary-loading"
# 选完封面文件会弹「封面裁剪」二次确认(重新上传 / 取消 / 确定),**不点确定封面就没提交**,
# 「创建」按钮会一直保持禁用 —— 这是第二轮取证里最贵的一个发现(填全了按钮仍禁用)。
_CROP_CONFIRM_TEXTS = ("确定", "确认")

# 引导浮层:文案是它唯一稳定的抓手 —— 真容器是 create-podcast-collection 区块内一个
# 绝对/固定定位的悬浮子元素,**没有稳定 class 名可挂**(08-08 取证结论)。
_GUIDE_TOOLTIP_TEXT = "播客合集上线啦"
# 暴露缝的最小可用宽度(px):比这还窄就不信它,退回直接点按钮。
# 实测值 39.3px,取 12 是留足拟人点击的随机偏移余量,又不至于把实测那条缝判掉。
_MIN_EXPOSED_WIDTH_PX = 12.0
# 从浮层右边界再让开 2px 再点 —— 与取证脚本算点位的公式逐字一致(那一点是实打实点中过的)。
_SLIVER_MARGIN_PX = 2.0
# 暴露缝宽度相对按钮宽度的**上界**:超过这个比例就判定浮层 rect 抓小了(命中了气泡内层
# content div,右边界偏小 → 缝被算宽 → 点位左移趋向浮层正中央),不信这条缝、退回点按钮。
# 取 0.5 的依据:08-08 实测浮层盖住按钮左侧约 81px、右侧只留约 39px(39/120≈0.33),
# 即真浮层盖住按钮 2/3 强。缝要是宽过按钮的一半,等于浮层盖住不到一半 —— 与"盖住左侧 81px"
# 这条实测事实直接矛盾,只可能是 rect 抓错了。0.33 落在 0.5 以内留足余量,而"整颗按钮
# 都算成暴露"(1.0)、命中内层(0.88)这两种失效形态都被这道上界挡在门外。
_MAX_EXPOSED_WIDTH_RATIO = 0.5

# 各步等待窗口(秒)
_CREATE_PAGE_TIMEOUT_S = 15.0
_CROP_MODAL_TIMEOUT_S = 20.0
# 20 → 45:判据修对之前这一段其实**从没真等过**(裸 disabled token 判成可点、秒过),
# 所以旧值 20s 是没被真实用过的数。假绿单实测按钮同时挂 loading 态,而这一步等的正是
# 封面(≤5MB)上传完 + 平台处理完,45s 给它留够余量(RCA 2026-08-09)。
_CREATE_ENABLE_TIMEOUT_S = 45.0
_CREATE_RESULT_TIMEOUT_S = 30.0


def _norm(text: Optional[str]) -> str:
    """归一文本:去首尾空白,全角空格也算空白。"""
    return (text or "").replace("　", " ").strip()


# ────────────────────────── tab 三件套 ──────────────────────────


def active_tab_texts(page) -> List[str]:
    """**所有**带 active 的 ``.creator-tab`` 文案(去空、保持文档序);读不到返回空表。

    为什么是复数:08-08 真号取证实测 DOM 里同时挂着两个 ``.creator-tab.active``,
    ``document.querySelector`` 取文档序第一个 —— 抓到的恰是残留在「上传视频」上的
    那个陈旧 active,于是 ``ensure_podcast_tab`` 在三次会话里全程判定失败,而页面
    内容其实早就是播客上传区了。取并集就没有"抓到哪一个"这回事。

    只读 DOM 不看 URL:实测点 tab 只在 URL 追加 ``&from=tab_switch`` 这个一次性来源
    标记,刷新/再切换后它还在,拿它判"现在在哪个 tab"必然误判。
    """
    try:
        got = page.evaluate(
            "() => Array.from(document.querySelectorAll('div.creator-tab.active'))"
            ".map(el => el.innerText || '')"
        )
    except Exception:  # noqa: BLE001 — 读判据失败只当"这轮没读到"
        return []
    if not isinstance(got, (list, tuple)):
        return []
    return [t for t in (_norm(x) for x in got) if t]


def active_tab_text(page) -> str:
    """激活 tab 的文案,单值形态(只给日志/报错取证用);读不到返回空串。

    多个 active 并存时返回文档序第一个 —— 判定请用 ``is_podcast_tab_active``,
    别拿这个单值去判,它可能正是那个陈旧的残留 active。
    """
    texts = active_tab_texts(page)
    return texts[0] if texts else ""


def _podcast_content_present(page) -> bool:
    """内容判据:页面上有没有发播客 tab 独有的上传区文案。"""
    try:
        return _PODCAST_CONTENT_MARKER in (page.inner_text("body") or "")
    except Exception:  # noqa: BLE001
        return False


def is_podcast_tab_active(page) -> bool:
    """是否已切到「发播客」tab —— **两路独立判据取或**。

    ① 任一 ``.creator-tab.active`` 的文案是「发播客」;
    ② 页面上出现发播客独有的上传区文案(内容判据)。

    取"或"而不是"与":两路各自都会**漏报**(① 栽在陈旧 active 上、② 在平台改文案时
    失效),但都不会**误报** —— 别的 tab 既不会把自己标成「发播客」,也不会渲染
    「将音频文件拖拽到此」。漏报的代价是白点一次 tab,误报的代价是在别的 tab 上乱传文件。
    """
    if PODCAST_TAB_TEXT in active_tab_texts(page):
        return True
    return _podcast_content_present(page)


def _find_tab(page, text: str):
    """按文案在顶部 tab 条里找那个 ``div.creator-tab``;找不到返回 None。

    页面上同名文案有好几处(tab 自身 + 内层 ``span.title`` + 拟人层装的透明覆盖层),
    收口到 ``div.creator-tab`` 才不会点到内层 span 或那层 opacity≈0 的覆盖元素上。
    """
    try:
        for el in page.query_selector_all(_CREATOR_TAB):
            try:
                if _norm(el.inner_text()) == text:
                    return el
            except Exception:  # noqa: BLE001 — 单个元素读失败只跳过它
                continue
    except Exception:  # noqa: BLE001
        return None
    return None


def ensure_podcast_tab(page, human: SyncHumanActions, tries: int = 3) -> bool:
    """确保停在「发播客」tab;未激活则点 tab 重试,直到 ``is_podcast_tab_active`` 认它。

    判据是**两路取或**(见 ``is_podcast_tab_active``):任一 ``.creator-tab.active`` 文案
    是「发播客」,**或**页面出现发播客独有的上传区文案 —— 08-08 取证后不再单认那个会栽在
    陈旧 active 上的 class 判据。

    **没有 URL 兜底**(见模块头):这条路在播客上不存在,重试就只是再点一次 tab。
    """
    for attempt in range(1, tries + 1):
        if is_podcast_tab_active(page):
            if attempt > 1:
                logger.info(f"✓ 已进入发播客 tab(第 {attempt} 次校验)")
            return True
        tab = _find_tab(page, PODCAST_TAB_TEXT)
        if tab is None:
            logger.warning(
                f"⚠️ 顶部没有「{PODCAST_TAB_TEXT}」tab(第 {attempt}/{tries} 次);"
                f"当前激活的是 {active_tab_text(page)!r}"
            )
        else:
            human.click(tab, reason=f"{PODCAST_TAB_TEXT} tab")
        human.wait(1.0, 1.8, context="等播客 tab 渲染")
    return is_podcast_tab_active(page)


# ────────────────────────── 引导浮层 ──────────────────────────


def dismiss_guide_tooltip(page, human: SyncHumanActions) -> Dict[str, Any]:
    """尽最大努力关掉「播客合集上线啦」引导浮层;**关不掉不抛错**,如实回报做了什么。

    ⚠️ **现状是关不掉**:08-08 真号取证把这里的三招连同"精确重定位再点 close-btn"
    第四招一起试了个遍,``present_after`` 四次全 True、四张截图像素级无变化。三招
    一个不删,是因为它们零成本、且平台哪天修好了这个浮层就又能关掉;真正让音频那条路
    走通的是 ``exposed_click_point`` 那条**绕过**(点按钮右侧没被压住的暴露缝)。

    关闭按钮**必须 scope 到浮层容器内查**(下面就是这么做的):页面别处也有 class 含
    close 的按钮,``document.querySelector('.close-btn')`` 会抓到那个 —— 取证脚本
    第一版就栽在这里。

    为什么关不掉不算失败:它不挡下方的合集入口,而音频那条路会在 step2a 以「找不到
    file input + 当场取证」明确收口 —— 在这里抛错只会把一个**可能**的阻塞说成确定的失败。

    三段递进(前一段成了就不做后面的):浮层容器内的关闭按钮 → Esc → 点页面空白处。
    """
    outcome: Dict[str, Any] = {"present_before": False, "tried": [], "present_after": None}
    container = _find_guide_tooltip(page)
    outcome["present_before"] = container is not None
    if container is None:
        return outcome

    close_btn = None
    try:
        close_btn = container.query_selector(
            "[class*='close'], .d-icon-close, svg[class*='close']"
        )
    except Exception:  # noqa: BLE001
        close_btn = None
    if close_btn is not None:
        outcome["tried"].append("close_button")
        human.click(close_btn, reason="关闭播客引导浮层")
        human.wait(0.5, 1.0, context="等引导浮层消失")
        if _find_guide_tooltip(page) is None:
            outcome["present_after"] = False
            return outcome

    outcome["tried"].append("escape")
    try:
        human.press_key("Escape", reason="关闭播客引导浮层")
    except Exception:  # noqa: BLE001 — 兜底手段失败不额外制造异常
        pass
    human.wait(0.4, 0.9, context="等引导浮层消失")
    if _find_guide_tooltip(page) is None:
        outcome["present_after"] = False
        return outcome

    # 最后一招:点页面左上角空白区(侧边栏与内容区之间),靠失焦关掉 popover。
    outcome["tried"].append("blank_click")
    human.click((230, 620), reason="点空白处关闭播客引导浮层")
    human.wait(0.4, 0.9, context="等引导浮层消失")
    outcome["present_after"] = _find_guide_tooltip(page) is not None
    if outcome["present_after"]:
        logger.warning(
            "[podcast] 引导浮层「%s」三种手段都没关掉;它会压住「上传音频」按钮,"
            "音频那一步大概率失败(合集入口不受影响)", _GUIDE_TOOLTIP_TEXT
        )
    return outcome


def _find_guide_tooltip(page):
    """找引导浮层容器(按文案定位到最内层含它的元素);没有返回 None。"""
    try:
        for el in page.query_selector_all("div"):
            try:
                text = el.inner_text()
            except Exception:  # noqa: BLE001
                continue
            if _GUIDE_TOOLTIP_TEXT in (text or "") and len(text) < 120:
                return el
    except Exception:  # noqa: BLE001
        return None
    return None


def exposed_click_point(
    button_rect: Optional[dict],
    tooltip_rect: Optional[dict],
    *,
    min_width_px: float = _MIN_EXPOSED_WIDTH_PX,
    max_width_ratio: float = _MAX_EXPOSED_WIDTH_RATIO,
) -> Optional[Tuple[float, float]]:
    """算「上传音频」按钮上**没被引导浮层盖住**的那条缝的点击点;算不出返回 None。

    纯函数(只吃两个 rect,不碰 page),因为这条几何判断是本页最脆的一环,必须能脱离
    真页面钉死:实测浮层 x262 w360(右边界 622)压住按钮 x543.3 w120(右边界 663.3)
    的左侧约 81px,右侧剩约 39px —— 点 (643.7, 342.0) 穿透浮层点中了按钮、弹窗正常打开。

    只处理"浮层从左侧压住按钮、右侧留缝"这**一种实测到的形态**,别的一律返回 None
    让调用方退回直接点按钮:没观测过的遮挡形态下瞎算坐标,点出去的是哪儿谁也不知道。

    缝宽有**下界也有上界**,两头都是防"缝被算歪":
    - ``min_width_px``:缝比它还窄就不信 —— 39px 是那次窗口尺寸下的值,尺寸/文案一变
      随时可能归零,而拟人点击本身还带随机偏移,窄缝上偏一点就又点回浮层了;
    - ``max_width_ratio``:缝宽过按钮宽度的这个比例就不信 —— 浮层 rect 若抓成了气泡内层
      content div(右边界偏小),缝会被**算宽**、点位左移趋向浮层正中央,下界拦不住这种。
      实测真浮层盖住按钮 2/3 强、缝只占 1/3,缝要是宽过一半只可能是 rect 抓错了。
    """
    btn = _rect(button_rect)
    tip = _rect(tooltip_rect)
    if btn is None or tip is None:
        return None
    bx, by, bw, bh = btn
    tx, ty, tw, th = tip
    if bw <= 0 or bh <= 0:
        return None
    if ty + th <= by or by + bh <= ty:
        return None  # 垂直不相交:没挡住,不用绕
    left = tx + tw + _SLIVER_MARGIN_PX
    if left <= bx:
        return None  # 浮层右边界还在按钮左边:水平不相交,同上
    right = bx + bw
    sliver = right - left
    if sliver < min_width_px:
        return None  # 缝太窄,不可靠
    if sliver > bw * max_width_ratio:
        return None  # 缝被算宽:浮层 rect 抓小了(命中气泡内层),定位可疑,退回点按钮中心
    return ((left + right) / 2.0, by + bh / 2.0)


def _pick_tooltip_rect(candidates: Optional[list]) -> Optional[dict]:
    """从所有文案命中的浮层候选里挑出真正那颗的 rect;挑不出返回 None(纯函数)。

    **与取证脚本逐字同一条规则**(那颗真号点中过的 (643.7, 342.0) 就是它挑出来的 rect
    算出来的):文案已在 JS 侧筛过,这里再叠两条 —— ① ``position`` 必须是 ``absolute``
    或 ``fixed``(浮层是脱离文档流的悬浮元素);② 在合格者里取**面积最小**的一个。

    为什么非这条不可(取证脚本的血泪注释):近似版只按文案取(取最后一个 / 取第一个),
    会被大容器抢先命中,暴露区退化成整颗按钮宽度,首次真号就点在了浮层正中央。文案会
    在整块 ``create-podcast-collection`` 区(祖先容器)与气泡内层里同时出现,只有"定位 +
    面积最小"这两条一起才把祖先容器和内层块都排除、锁定那颗真浮层。

    ``w/h > 0`` 是兜底:面积为 0 的元素(未渲染 / display 塌陷)不参与,免得它以"面积
    最小"混进来。读残的候选(缺键 / 非数)直接跳过,取证读数绝不制造异常。
    """
    best: Optional[dict] = None
    best_area: Optional[float] = None
    for cand in candidates or []:
        if not isinstance(cand, dict):
            continue
        if str(cand.get("position") or "").lower() not in ("absolute", "fixed"):
            continue
        rect = _rect(cand)
        if rect is None:
            continue
        x, y, w, h = rect
        if w <= 0 or h <= 0:
            continue
        area = w * h
        if best_area is None or area < best_area:
            best_area = area
            best = {"x": x, "y": y, "w": w, "h": h}
    return best


def _rect(raw: Optional[dict]) -> Optional[Tuple[float, float, float, float]]:
    """把 ``{"x","y","w","h"}`` 读成四元组;缺键/非数一律 None(读数绝不制造异常)。"""
    if not isinstance(raw, dict):
        return None
    try:
        return (float(raw["x"]), float(raw["y"]), float(raw["w"]), float(raw["h"]))
    except (KeyError, TypeError, ValueError):
        return None


# 只读 dump:一次拿回「上传音频」按钮的 rect + **所有**文案命中的浮层候选(各带 position
# 与 rect)。**挑哪个交给 Python 侧的 ``_pick_tooltip_rect``**(定位 + 面积最小),JS 只负责
# 把候选连同 getComputedStyle().position 全捞回来 —— 别在 JS 里"取最后一个 / 取第一个",
# 那会被 create-podcast-collection 祖先容器(1060px 宽)或气泡内层块抢先命中,rect 一歪
# 整条缝就算错。文案 + len<120 只是粗筛,真正的三条件规则在纯函数里、有回归锁钉着。
_UPLOAD_BUTTON_RECT_JS = r"""() => {
    const btn = document.querySelector('button.upload-button');
    if (!btn) return null;
    const r = e => { const b = e.getBoundingClientRect();
                     return {x: b.x, y: b.y, w: b.width, h: b.height}; };
    const tips = [];
    for (const el of document.querySelectorAll('div')) {
        const t = el.innerText || '';
        if (t.includes('%s') && t.length < 120) {
            const box = r(el);
            tips.push({position: getComputedStyle(el).position,
                       x: box.x, y: box.y, w: box.w, h: box.h});
        }
    }
    return {btn: r(btn), tips: tips};
}""" % _GUIDE_TOOLTIP_TEXT


def upload_audio_click_point(page) -> Optional[Tuple[float, float]]:
    """读现场 rect,算出穿透引导浮层点中「上传音频」按钮的坐标;算不出返回 None。

    两段都可能"算不出"、都退回 None 让调用方直接点按钮:① ``_pick_tooltip_rect`` 在所有
    文案候选里按"定位 + 面积最小"挑不出真浮层;② ``exposed_click_point`` 判定缝太窄/太宽
    (rect 抓歪了)。哪一段都不瞎给坐标。
    """
    try:
        got = page.evaluate(_UPLOAD_BUTTON_RECT_JS)
    except Exception:  # noqa: BLE001 — 读数失败只当"这次绕不了"
        return None
    if not isinstance(got, dict):
        return None
    tip = _pick_tooltip_rect(got.get("tips"))
    return exposed_click_point(got.get("btn"), tip)


# ────────────────────────── 播客合集创建 ──────────────────────────


def collection_probe(page) -> Dict[str, Any]:
    """回读合集创建流程的当场证据(任何一步失败都随 error 一起交出去)。"""
    evidence: Dict[str, Any] = {}
    try:
        evidence["active_tab"] = active_tab_text(page)
    except Exception:  # noqa: BLE001 — 取证本身绝不制造新异常
        evidence["active_tab"] = ""
    for key, selector in (
        ("name_input_present", _NAME_INPUT),
        ("desc_input_present", _DESC_INPUT),
        ("cover_input_present", _COVER_INPUT),
        ("create_button_present", _CREATE_BUTTON),
    ):
        try:
            evidence[key] = page.query_selector(selector) is not None
        except Exception:  # noqa: BLE001
            evidence[key] = False
    evidence["create_button"] = create_button_state(page)
    evidence["page_text"] = _page_text(page)[:600]
    return evidence


def create_button_state(page) -> Dict[str, Any]:
    """读「创建」按钮的禁用态。

    判据**四路取或**,任一命中即不可点:① 有 ``disabled`` 属性;② ``class`` 含
    ``create-btn-disabled``;③ ``class`` 按空白切分后含**独立 token** ``disabled``;
    ④ 同法含 ``d-button-primary-loading``(loading 态点了也白点)。

    ③④ 是 RCA 2026-08-09 补的,依据是真号 7 单假绿里按钮 class 的**原文**::

        d-button d-button-large --size-icon-large --size-text-h6 disabled
        --color-static bold d-button-primary-loading --color-bg-primary
        --color-white create-btn

    禁用只体现在裸 token ``disabled`` 上 —— 按钮**没有** disabled 属性、class 里也
    **没有** ``create-btn-disabled``,①② 双双落空,``_wait_create_enabled`` 于是秒判
    "可点"、点下一颗禁用按钮无事发生,7 单全部假绿(平台侧一个合集都没建出来)。

    ③④ 坚持**整词**比较而不是 substring:``--color-static`` 这类类名里本来就不含
    ``disabled``,但 substring 判法会被将来任何 ``xxx-disabled`` 命中,把按钮永久
    判死(那是比假绿更难查的反向故障)。

    找不到按钮返回 ``{"found": False}``,调用方当作**不可点**处理:找不到是页面
    状态异常,不是"可以点了"。
    """
    try:
        got = page.evaluate(
            "() => { const b = document.querySelector('button.create-btn');"
            " if (!b) return {found: false};"
            " return {found: true, cls: b.className || '',"
            "   disabled_attr: b.hasAttribute('disabled')}; }"
        )
    except Exception:  # noqa: BLE001
        return {"found": False}
    if not isinstance(got, dict) or not got.get("found"):
        return {"found": False}
    cls = got.get("cls") or ""
    tokens = cls.split()
    disabled = (
        bool(got.get("disabled_attr"))
        or _CREATE_DISABLED_CLASS in cls
        or _CREATE_DISABLED_TOKEN in tokens
        or _CREATE_LOADING_TOKEN in tokens
    )
    return {"found": True, "enabled": not disabled, "cls": cls}


# 「创建」按钮**中心落点**的当场取证:cls 全文 + 矩形 + elementFromPoint 落点元素链。
# 与 note_editing._FOCUS_FORENSICS_JS 同款纪律(单层 ≤60 字符、全链 ≤300):失败回执要能
# 回答"下一单为什么还提交不出去"—— 按钮是禁用/loading,还是被别的层盖住了点不到。
_CREATE_BUTTON_FORENSICS_JS = r"""() => {
    const b = document.querySelector('button.create-btn');
    if (!b) return {found: false};
    const r = b.getBoundingClientRect();
    const cx = r.x + r.width / 2, cy = r.y + r.height / 2;
    let el = document.elementFromPoint(cx, cy);
    const hits = !!(el && (el === b || b.contains(el)));
    const parts = [];
    for (let i = 0; i < 5 && el; i++) {   // 落点元素本身 + 最多 4 层祖先
        const cls = typeof el.className === 'string' ? el.className.slice(0, 40) : '';
        const tag = String(el.tagName || '').toLowerCase();
        parts.push((cls ? tag + '.' + cls : tag).slice(0, 60));
        el = el.parentElement;
    }
    return {
        found: true,
        cls: b.className || '',
        disabled_attr: b.hasAttribute('disabled'),
        rect: {x: Math.round(r.x), y: Math.round(r.y),
               w: Math.round(r.width), h: Math.round(r.height)},
        point: [Math.round(cx), Math.round(cy)],
        // 落点在视口外时 elementFromPoint 返回 null:链留 null,好与"点上了但链读不出"区分
        point_element_chain: parts.length ? parts.join(' < ').slice(0, 300) : null,
        point_hits_button: hits,
    };
}"""


def _create_button_forensics(page) -> Dict[str, Any]:
    """按钮提交失败时抓一份当场证据;读不到只回最小骨架(取证绝不制造新异常)。"""
    try:
        got = page.evaluate(_CREATE_BUTTON_FORENSICS_JS)
    except Exception as exc:  # noqa: BLE001
        return {"probe_error": str(exc)[:120]}
    return dict(got) if isinstance(got, dict) else {"probe_error": "non_dict"}


def _page_text(page) -> str:
    """整页可见文本(归一后);读不到返回空串。

    与 ``collection_probe`` 里那份**刻意分开**:那份为了控制回执体积截到 600 字,
    而"合集名出现在列表里没有"这个判据必须看**全文** —— 合集多起来之后名字很容易
    落在 600 字以外,截断版会把成功读成失败。
    """
    try:
        return _norm(page.inner_text("body"))
    except Exception:  # noqa: BLE001
        return ""


def _wait_selector(page, selector: str, timeout_s: float):
    """轮询等某个选择器出现;超时返回 None(不抛)。"""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            el = page.query_selector(selector)
        except Exception:  # noqa: BLE001
            el = None
        if el is not None:
            return el
        time.sleep(0.4)
    return None


def _find_text(page, texts, *, scope: str = "button, div, span, a, li"):
    """在整页里找文案精确等于 ``texts`` 之一的可点元素;找不到返回 None。"""
    wanted = {_norm(t) for t in texts}
    try:
        for el in page.query_selector_all(scope):
            try:
                if _norm(el.inner_text()) in wanted:
                    return el
            except Exception:  # noqa: BLE001
                continue
    except Exception:  # noqa: BLE001
        return None
    return None


def create_collection(
    page,
    human: SyncHumanActions,
    name: str,
    description: Optional[str],
    cover_path: str,
) -> Dict[str, Any]:
    """在「发播客」tab 里新建一个播客合集 → ``{"status": "done"|"error", ...}``。

    流程(全部已真号取证,除结果回读那一步):切 tab → 关引导浮层(尽力)→ 点
    「新建播客合集」(**整页替换,不是弹窗**)→ 填名称/简介 → 封面 ``set_input_files``
    → **点「封面裁剪」弹窗的「确定」**(不点这一步封面不算提交,「创建」按钮永远禁用)
    → 等「创建」按钮翻转 → 点创建 → 回读。

    封面同样**绝不点上传按钮**:真桌面上会弹原生 GTK 文件框,Playwright 拦不住、
    模态卡死整条流程(与图片/视频上传同源纪律)。

    每一步失败都立刻返回 ``error`` 并附 ``observed`` 当场取证 —— 这条产品线的失败
    普遍是静默的,"没报错"从来不算数。``collection_id`` 抓得到就带,抓不到给 None
    (E4 未取证),**不阻断成功判定**。
    """
    if not ensure_podcast_tab(page, human):
        return {"status": "error", "reason": "podcast_tab_not_active: 切不到「发播客」tab",
                "observed": collection_probe(page)}
    tooltip = dismiss_guide_tooltip(page, human)

    entry = _find_text(page, (_COLLECTION_ENTRY_TEXT,))
    if entry is None:
        return {"status": "error",
                "reason": f"collection_entry_not_found: 页面上没有「{_COLLECTION_ENTRY_TEXT}」入口",
                "observed": collection_probe(page), "tooltip": tooltip}
    # 建前查重(P1 升级,2026-08-09):发播客页的合集区就是该号播客合集的完整列表,同名已在
    # → **一个字都不建**。平台不去重同名(号5 双会客厅实证),旧的"记录性 name_preexisted"
    # 在纯新建场景等于放行重复;语义对齐笔记合集 create 的 already_exists。
    if name in _page_text(page):
        return {"status": "error",
                "reason": f"collection_name_already_exists: 发播客页合集区已有「{name}」,"
                          "不重建(平台不去重同名,号5 双会客厅实证);要重建请先人工删除旧的",
                "observed": collection_probe(page), "tooltip": tooltip}
    # 查重挡在前面,走到这里必然没有同名(字段保留,回执形状不变)
    name_preexisted = False
    human.click(entry, reason="新建播客合集")

    name_input = _wait_selector(page, _NAME_INPUT, _CREATE_PAGE_TIMEOUT_S)
    if name_input is None:
        return {"status": "error",
                "reason": "collection_create_page_not_loaded: 点了新建但合集创建页没出来",
                "observed": collection_probe(page), "tooltip": tooltip}

    # type_text 自带 click_first 聚焦,不必另点一次(多点一次只是多一次可疑动作)
    human.type_text(name_input, name)
    if description:
        desc_input = page.query_selector(_DESC_INPUT)
        if desc_input is None:
            return {"status": "error",
                    "reason": "collection_desc_input_not_found: 传了简介但页面上没有简介框",
                    "observed": collection_probe(page)}
        human.type_text(desc_input, description)

    cover_input = page.query_selector(_COVER_INPUT)
    if cover_input is None:
        return {"status": "error",
                "reason": "collection_cover_input_not_found: 合集创建页没有封面 file input",
                "observed": collection_probe(page)}
    try:
        cover_input.set_input_files([cover_path])
    except Exception as exc:  # noqa: BLE001 — 灌文件失败如实报,不静默
        return {"status": "error", "reason": f"collection_cover_set_input_failed: {exc}",
                "observed": collection_probe(page)}

    crop = _confirm_cover_crop(page, human)
    if crop.get("status") == "error":
        return {**crop, "observed": collection_probe(page)}

    if not _wait_create_enabled(page):
        return {"status": "error",
                "reason": "create_button_never_enabled: 三项都填了但「创建」按钮始终禁用"
                          "(封面还在上传/处理 / 封面裁剪没确认完 / 平台又加了必填项),"
                          "不点禁用按钮",
                "observed": collection_probe(page), "cover_crop": crop,
                "create_button_forensics": _create_button_forensics(page)}

    button = page.query_selector(_CREATE_BUTTON)
    if button is None:
        return {"status": "error", "reason": "create_button_vanished: 按钮刚才还在,现在没了",
                "observed": collection_probe(page)}
    human.click(button, reason="创建播客合集")

    return _read_create_result(page, human, name, tooltip, crop, name_preexisted)


def _confirm_cover_crop(page, human: SyncHumanActions) -> Dict[str, Any]:
    """点掉封面上传后弹出的「封面裁剪」二次确认;没弹窗也算通过。

    ⚠️ 这一步是必需的而不是可选的:第二轮真号取证里名称/简介/封面三项都填了,
    「创建」按钮**仍然禁用**,根因就是裁剪没确认、封面根本没提交。

    弹窗没弹出来时返回 ``skipped`` 而不是报错:平台可能对某些图直接跳过裁剪,
    而真正的判据是下一步的「创建」按钮到底翻没翻转 —— 让那一步说话。
    """
    confirm = None
    deadline = time.monotonic() + _CROP_MODAL_TIMEOUT_S
    while time.monotonic() < deadline:
        confirm = _find_text(page, _CROP_CONFIRM_TEXTS, scope="button")
        if confirm is not None:
            break
        if create_button_state(page).get("enabled"):
            # 没弹裁剪窗、创建按钮已经可点 → 这张图不需要裁剪确认
            return {"status": "skipped", "reason": "no_crop_modal"}
        time.sleep(0.5)
    if confirm is None:
        return {"status": "skipped", "reason": "crop_confirm_not_found"}
    human.click(confirm, reason="确认封面裁剪")
    human.wait(0.8, 1.5, context="等裁剪弹窗关闭")
    return {"status": "done"}


def _wait_create_enabled(page) -> bool:
    """等「创建」按钮从禁用翻转成可点;超时返回 False(**绝不点禁用按钮**)。"""
    deadline = time.monotonic() + _CREATE_ENABLE_TIMEOUT_S
    while time.monotonic() < deadline:
        if create_button_state(page).get("enabled"):
            return True
        time.sleep(0.5)
    return False


def _read_create_result(
    page, human: SyncHumanActions, name: str, tooltip: dict, crop: dict,
    name_preexisted: bool
) -> Dict[str, Any]:
    """回读创建结果:**创建表单收起** ``且`` 收起后页面文本里出现该名称 = done。

    ⚠️ 判据是 RCA 2026-08-09 重写的,起因是真号 7 单假绿。旧实现取两个"互相独立"的
    信号**任一**命中即算成功,其中信号②「``name in page_text``」是伪证人:创建表单
    右侧渲染一张**实时预览卡**,把刚打进输入框的合集名原样显示出来(假绿单的 page_text
    实录:「创建播客合集 / 合集名称* / 11/20」表单文案与「NBDpsy心理会客厅 / 播客 /
    更新至0集 / 0人听过」预览卡并存)。于是表单根本没提交、按钮压根没点动的那 7 单,
    全部拿自己打的字当成"列表里有了"判成 done,平台侧一个合集都没建出来。

    **铁律:表单还开着时,页面文本里出现合集名不构成任何成功证据。**取证显示失败时
    表单不会自己关,所以"表单收起"才是那个便宜又硬的分水岭;名字检查退居第二道,
    在收起**之后**才有意义。

    三种终态:
    - 表单收起 + 名字出现 → ``done``,``confirmed_by=create_page_closed``;
    - 超时表单仍开着 → ``create_form_still_open``(带按钮 cls 全文 + 落点链 + 浮层现状,
      回答"下一单为什么还提交不出去");
    - 表单收起但名字没出现 → ``create_page_closed_name_missing``(做没做成未知,
      **请人工核对,别自动重建**)。

    ``name_preexisted``:创建**之前**列表里就有同名合集时(号1 的「NBDpsy心理会客厅」),
    收起后的名字检查对它不构成新建证据 —— 照样判 done,但 ``confirmed_by`` 后缀
    ``_name_preexisted``,提醒调用方核对合集数量/note_num 再认账。

    ``collection_id``:E4 未取证(平台侧 id 能不能回读不知道),这里只做一次
    **不抱期望**的 URL 抓取,抓不到给 None,不影响成功判定。
    """
    deadline = time.monotonic() + _CREATE_RESULT_TIMEOUT_S
    observed: Dict[str, Any] = {}
    form_open = True
    while time.monotonic() < deadline:
        observed = collection_probe(page)
        form_open = bool(observed.get("name_input_present"))
        # 名字检查只在表单收起**之后**做,而且读全文(collection_probe 的 page_text 截到 600 字)。
        # 收起后页面常落在「上传视频」tab(号6播客首验实拍:active_tab="上传视频"),而合集区
        # 在发播客 tab —— 不切回去就是在错的页面上找名字,真建成也只能报"未知"(P1 盲点修,
        # RCA 2026-08-09)。切换失败不抛错:留在原地让名字检查如实落空,走保守 error 分支。
        if not form_open and not is_podcast_tab_active(page):
            try:
                ensure_podcast_tab(page, human)
            except Exception:  # noqa: BLE001 — 切 tab 失败当没切成,判据保守方向不变
                pass
        if not form_open and name in _page_text(page):
            confirmed_by = "create_page_closed"
            if name_preexisted:
                confirmed_by += "_name_preexisted"
            return {
                "status": "done",
                "name": name,
                "collection_id": _extract_collection_id(page),
                "confirmed_by": confirmed_by,
                "name_shown_after_close": True,
                "name_preexisted": name_preexisted,
                "tooltip": tooltip,
                "cover_crop": crop,
                "observed": observed,
            }
        time.sleep(0.8)

    common = {
        "status": "error",
        "name": name,
        "name_shown_after_close": False,
        "name_preexisted": name_preexisted,
        "observed": observed,
        "tooltip": tooltip,
        "cover_crop": crop,
    }
    if form_open:
        return {
            **common,
            "reason": "create_form_still_open: 点了「创建」但创建表单一直没收起 —— 真号 7 单"
                      "假绿的形态正是它(按钮仍是禁用/loading 态,点下去无事发生)。"
                      "**大概率没建成**,但做没做成仍以人工核对为准,别自动重建",
            "create_button_forensics": _create_button_forensics(page),
            "guide_tooltip_present": _find_guide_tooltip(page) is not None,
        }
    return {
        **common,
        "reason": "create_page_closed_name_missing: 创建表单收起了,但合集区里找不到这个"
                  "合集名;**做没做成未知**,请人工到发播客页核对后再决定是否重建"
                  "(**不要自动重建**,平台会不会去重同名未验证)",
    }


# ────────────────────── 发布表单里选播客合集 ──────────────────────
#
# 取证状态(08-08,账号9,已走到发布表单):
# **已取证** —— 表单结构与合集卡本体。真实文案是**「加入播客合集」**;旧占位猜的
# 「播客合集 / 选择合集 / 加入合集」三个**全部落空**(``collection_field_text_hits``
# 三项全 false),留着只会在真正的控件旁边匹配到别的东西。
# **未取证** —— 卡片**点开之后**的候选形态(下拉?二级弹窗?):为控制真号操作范围
# 没有再点。故 ``_COLLECTION_OPTION_SCOPE`` 仍是占位值,找不到候选就 fail-loud。
#
# 控制流不变:找到合集控件 → 点开 → 按名称选中 → 回读确认。每一步定位不到就带当场
# 取证报 error,**绝不静默假装选上了**;调用方(sync_client)对 error 的处理是告警
# 不阻断 —— 笔记照发,只是不进合集。
_COLLECTION_CARD = ".collection-plugin-wrapper"
# 卡内标题文案(``.collection-plugin-content-title`` 的文本),按文案兜底时用它。
_COLLECTION_FIELD_TEXTS = ("加入播客合集",)
# 点击目标**刻意收窄到内容区**而不是整卡:整卡 ``.collection-plugin-wrapper`` 横跨
# x355~987,而「创建播客合集」直达创建页的入口 ``.collection-plugin-create`` 就压在
# x867~971 —— 在整卡上做带随机偏移的拟人点击有实打实的概率撞进它,一撞就离开发布表单、
# 整篇笔记白填。内容区 ``.collection-plugin-content`` 右边界 859 < 867,零重叠。
# (同款教训:原创声明勾选点位从宽容器收窄到 16×16 方块那次。)
_COLLECTION_CLICK_TARGET = ".collection-plugin-content"
# ⚠️ 占位值:点开之后的候选层结构未取证。
_COLLECTION_OPTION_SCOPE = ".d-dropdown, .d-popover, .d-modal, [class*='select']"
_COLLECTION_SELECT_TIMEOUT_S = 15.0

# ── 已取证:发布表单(publish/publish?...&target=audio)──
# 标题会自动预填音频文件名(去扩展名);正文与图文/视频同款 tiptap 编辑器。
_PUBLISH_TITLE_INPUT = 'input.d-text[placeholder*="填写标题"]'
_PUBLISH_BODY_EDITOR = 'div.tiptap[contenteditable="true"]'
_PUBLISH_SETTING_CONTENT = ".publish-page-content-setting-content"


def select_podcast_collection(
    page, human: SyncHumanActions, name: str
) -> Dict[str, Any]:
    """在播客发布表单里按**名称**选中一个播客合集 → ``{"status": "done"|"error", ...}``。

    用名称不用 id:合集创建后能否回读到平台侧 id 未取证(E4/E5),而名称是实拍确认的
    必填项(≤20 字)。真号取证若发现下拉带 id 且名称可重复,把这里换成按 id 选即可。
    """
    field = _find_collection_field(page)
    if field is None:
        return {"status": "error",
                "reason": f"podcast_collection_field_not_found: 发布表单里没有"
                          f"「{_COLLECTION_FIELD_TEXTS[0]}」这张合集卡"
                          f"({_COLLECTION_CARD} / {_COLLECTION_CLICK_TARGET} 均未命中)",
                "observed": _publish_form_probe(page)}
    human.click(field, reason="打开播客合集选择")
    human.wait(0.8, 1.5, context="等合集候选渲染")

    deadline = time.monotonic() + _COLLECTION_SELECT_TIMEOUT_S
    option = None
    while time.monotonic() < deadline:
        option = _find_option_by_name(page, name)
        if option is not None:
            break
        time.sleep(0.5)
    if option is None:
        return {"status": "error",
                "reason": f"podcast_collection_not_in_options: 候选里没有「{name}」"
                          f"(合集可能还没建,或建完没即时出现在候选里 —— E4 未取证)",
                "observed": _publish_form_probe(page)}
    human.click(option, reason=f"选中播客合集「{name}」")
    human.wait(0.8, 1.5, context="等合集选中回填")

    evidence = _publish_form_probe(page)
    if name not in (evidence.get("page_text") or ""):
        # 回读不到就**不算成功**:这条产品线的失败普遍是静默的,"没报错"从来不算数。
        return {"status": "error",
                "reason": f"podcast_collection_unverified: 点了「{name}」但表单里回读不到它",
                "observed": evidence}
    return {"status": "done", "name": name, "observed": evidence}


def _find_collection_field(page):
    """定位发布表单里那张播客合集卡的**可点区域**;找不到返回 None。

    两级:先按真值 ``.collection-plugin-content``(不含创建入口的内容区),
    再退回按文案「加入播客合集」找 —— 后者是平台改 class 时的兜底,不是猜测:
    文案本身是 08-08 实拍读到的。**两级都绕开** ``.collection-plugin-create``。
    """
    try:
        target = page.query_selector(_COLLECTION_CLICK_TARGET)
    except Exception:  # noqa: BLE001
        target = None
    if target is not None:
        return target
    return _find_text(page, _COLLECTION_FIELD_TEXTS)


def _find_option_by_name(page, name: str):
    """在下拉/弹层范围内按文案找候选项;找不到返回 None。"""
    target = _norm(name)
    try:
        for scope in page.query_selector_all(_COLLECTION_OPTION_SCOPE):
            try:
                for el in scope.query_selector_all("li, div, span"):
                    try:
                        if _norm(el.inner_text()) == target:
                            return el
                    except Exception:  # noqa: BLE001
                        continue
            except Exception:  # noqa: BLE001
                continue
    except Exception:  # noqa: BLE001
        return None
    return None


def _publish_form_probe(page) -> Dict[str, Any]:
    """播客发布表单的当场取证:逐个真值控件在不在 + 页面文本。

    为什么逐个报而不是只丢页面文本:合集选不上时,第一件要分清的事是"整张卡不在"
    (表单没渲染完 / 页型变了)还是"卡在但候选没出来"(点开之后的结构未取证)。
    """
    evidence: Dict[str, Any] = {}
    try:
        evidence["page_text"] = _norm(page.inner_text("body"))[:1200]
    except Exception:  # noqa: BLE001 — 取证本身绝不制造新异常
        evidence["page_text"] = ""
    for key, selector in (
        ("title_input_present", _PUBLISH_TITLE_INPUT),
        ("body_editor_present", _PUBLISH_BODY_EDITOR),
        ("setting_content_present", _PUBLISH_SETTING_CONTENT),
        ("collection_card_present", _COLLECTION_CARD),
        ("dropdown_present", _COLLECTION_OPTION_SCOPE),
    ):
        try:
            evidence[key] = page.query_selector(selector) is not None
        except Exception:  # noqa: BLE001
            evidence[key] = False
    return evidence


def _extract_collection_id(page) -> Optional[str]:
    """尽力从 URL 里抠出平台侧合集 id;抓不到返回 None(E4 未取证,不抱期望)。"""
    try:
        url = page.url or ""
    except Exception:  # noqa: BLE001
        return None
    for key in ("collection_id=", "collectionId=", "album_id="):
        if key in url:
            return url.split(key, 1)[1].split("&", 1)[0] or None
    return None
