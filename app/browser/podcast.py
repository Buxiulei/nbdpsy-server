"""播客(发播客 tab)页面的浏览器动作:tab 三件套 + 引导浮层 + 播客合集创建。

选择器的取证状态**分两档**,别混着读:

**已真号取证**(2026-08-07,账号9,``data/scene_captures/podcast/``):
- 顶部 4 个 tab 都是 ``div.creator-tab``,当前激活的额外带 ``active`` 类;
  ⚠️ 点「发播客」后 URL 只追加 ``&from=tab_switch``(一次性来源标记,**不是**可复用的
  mode 参数)——所以判据只认 DOM 的 ``.creator-tab.active`` 文本,**不做 URL 兜底**
  (与图文 tab 的 ``?type=normal`` 兜底刻意不同,那条路在播客上不存在);
- 「上传音频」红按钮 ``button.upload-button``;右侧 RSS 是 ``button.rss-button``;
- 「播客合集」区入口文案「新建播客合集」,**点击后是整页内容替换,不是 modal**
  ——所以它与"有没有传音频"完全解耦,可以独立触达;
- 合集创建页三个字段与「创建」按钮的禁用判据(见下方常量);
- 合集封面 accept 实测为 ``.jpg,.jpeg,.png,.webp`` —— **含 webp**,与设计文档
  「合集封面无 webp」的假设相反,以实测 DOM 为准。

**未取证(占位值,fail-loud)**:音频上传弹窗内部结构(见 ``app/browser/atomic_tasks.py``
的 step2a/step3a)。两轮真号取证都被下面这个引导浮层挡住,没能进到弹窗里。

**引导浮层**(本页最大的坑):「播客合集上线啦」的 popover **正压在「上传音频」按钮上**
(截图 ``02_upload_audio_modal_retry.png`` 铁证),不先关掉它,点上传音频点的是浮层。
``dismiss_guide_tooltip`` 尽最大努力关它,但**关不掉不算失败** —— 它不挡合集入口
(入口在页面下方),而音频那条路会在 step2a 以"找不到 file input + 当场取证"收口。
"""

import time
from typing import Any, Dict, Optional

from loguru import logger

from app.browser.sync_human_actions import SyncHumanActions

# ── 已取证:tab ──
PODCAST_TAB_TEXT = "发播客"
_CREATOR_TAB = "div.creator-tab"

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
# 选完封面文件会弹「封面裁剪」二次确认(重新上传 / 取消 / 确定),**不点确定封面就没提交**,
# 「创建」按钮会一直保持禁用 —— 这是第二轮取证里最贵的一个发现(填全了按钮仍禁用)。
_CROP_CONFIRM_TEXTS = ("确定", "确认")

# 引导浮层(未精确取证:第二轮点了 .close-btn 后截图像素级无变化,说明没点中它)
_GUIDE_TOOLTIP_TEXT = "播客合集上线啦"

# 各步等待窗口(秒)
_CREATE_PAGE_TIMEOUT_S = 15.0
_CROP_MODAL_TIMEOUT_S = 20.0
_CREATE_ENABLE_TIMEOUT_S = 20.0
_CREATE_RESULT_TIMEOUT_S = 30.0


def _norm(text: Optional[str]) -> str:
    """归一文本:去首尾空白,全角空格也算空白。"""
    return (text or "").replace("　", " ").strip()


# ────────────────────────── tab 三件套 ──────────────────────────


def active_tab_text(page) -> str:
    """当前激活 tab 的文案(读 ``.creator-tab.active``);读不到返回空串。

    只读 DOM 不看 URL:实测点 tab 只在 URL 追加 ``&from=tab_switch`` 这个一次性来源
    标记,刷新/再切换后它还在,拿它判"现在在哪个 tab"必然误判。
    """
    try:
        return _norm(page.evaluate(
            "() => { const el = document.querySelector('div.creator-tab.active');"
            " return el ? (el.innerText || '') : ''; }"
        ))
    except Exception:  # noqa: BLE001 — 读判据失败只当"这轮没读到"
        return ""


def is_podcast_tab_active(page) -> bool:
    """是否已切到「发播客」tab(判据 = ``.creator-tab.active`` 的文本)。"""
    return PODCAST_TAB_TEXT in active_tab_text(page)


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
    """确保停在「发播客」tab;未激活则点 tab 重试,直到 ``.creator-tab.active`` 认它。

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

    为什么它非关不可:实测这个 popover 正压在「上传音频」按钮上,不关就点不到按钮
    (两轮真号取证都卡在这)。为什么关不掉又不算失败:它不挡下方的合集入口,而音频
    那条路会在 step2a 以「找不到 file input + 当场取证」明确收口 —— 在这里抛错只会
    把一个**可能**的阻塞说成确定的失败。

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
    try:
        evidence["page_text"] = _norm(page.inner_text("body"))[:600]
    except Exception:  # noqa: BLE001
        evidence["page_text"] = ""
    return evidence


def create_button_state(page) -> Dict[str, Any]:
    """读「创建」按钮的禁用态。

    判据 = ``class`` 含 ``create-btn-disabled`` **或** 有 ``disabled`` 属性
    (真号夹具里两者同时出现;取"或"是防御——平台只留其一时不至于误判成可点)。
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
    disabled = bool(got.get("disabled_attr")) or _CREATE_DISABLED_CLASS in got.get("cls", "")
    return {"found": True, "enabled": not disabled, "cls": got.get("cls", "")}


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
                          "(封面裁剪没确认完 / 平台又加了必填项),不点禁用按钮",
                "observed": collection_probe(page), "cover_crop": crop}

    button = page.query_selector(_CREATE_BUTTON)
    if button is None:
        return {"status": "error", "reason": "create_button_vanished: 按钮刚才还在,现在没了",
                "observed": collection_probe(page)}
    human.click(button, reason="创建播客合集")

    return _read_create_result(page, name, tooltip, crop)


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


def _read_create_result(page, name: str, tooltip: dict, crop: dict) -> Dict[str, Any]:
    """回读创建结果:创建页收起 + 合集区出现该名称 = done。

    ⚠️ **成功判据未经真号验证**(E5:创建成功的页面反馈形态没抓到,两轮取证都没能
    真的点下「创建」)。所以这里取两个**互相独立**的信号,任一命中即算成功:
    ① 创建页的名称输入框消失(页面回到合集列表);② 页面文本里出现了这个合集名。
    两个都不命中就报 error 带当场取证 —— 宁可让调用方去核对,也不谎报成功。

    ``collection_id``:E4 未取证(平台侧 id 能不能回读不知道),这里只做一次
    **不抱期望**的 URL 抓取,抓不到给 None,不影响成功判定。
    """
    deadline = time.monotonic() + _CREATE_RESULT_TIMEOUT_S
    observed: Dict[str, Any] = {}
    while time.monotonic() < deadline:
        observed = collection_probe(page)
        page_gone = not observed.get("name_input_present")
        name_shown = name in (observed.get("page_text") or "")
        if page_gone or name_shown:
            return {
                "status": "done",
                "name": name,
                "collection_id": _extract_collection_id(page),
                "confirmed_by": "create_page_closed" if page_gone else "name_in_list",
                "tooltip": tooltip,
                "cover_crop": crop,
                "observed": observed,
            }
        time.sleep(0.8)
    return {
        "status": "error",
        "reason": "create_result_unconfirmed: 点了「创建」但既没回到列表、列表里也没有"
                  "这个合集名;**做没做成未知**,请人工到发播客页核对后再决定是否重建",
        "name": name,
        "observed": observed,
        "tooltip": tooltip,
        "cover_crop": crop,
    }


# ────────────────────── 发布表单里选播客合集(未取证,fail-loud) ──────────────────────
#
# ⚠️ **本段的选择器全部是占位值,一个都没有真号验证过**:「去发布」之后的发布表单
# 从未到达(E4)。控制流是确定的:找到合集选择控件 → 点开 → 按名称选中 → 回读确认。
# 每一步定位不到就带当场取证报 error,**绝不静默假装选上了**;调用方(sync_client)
# 对 error 的处理是告警不阻断 —— 笔记照发,只是不进合集。
_COLLECTION_FIELD_TEXTS = ("播客合集", "选择合集", "加入合集")
_COLLECTION_OPTION_SCOPE = ".d-dropdown, .d-popover, .d-modal, [class*='select']"
_COLLECTION_SELECT_TIMEOUT_S = 15.0


def select_podcast_collection(
    page, human: SyncHumanActions, name: str
) -> Dict[str, Any]:
    """在播客发布表单里按**名称**选中一个播客合集 → ``{"status": "done"|"error", ...}``。

    用名称不用 id:合集创建后能否回读到平台侧 id 未取证(E4/E5),而名称是实拍确认的
    必填项(≤20 字)。真号取证若发现下拉带 id 且名称可重复,把这里换成按 id 选即可。
    """
    field = _find_text(page, _COLLECTION_FIELD_TEXTS)
    if field is None:
        return {"status": "error",
                "reason": f"podcast_collection_field_not_found: 发布表单里没有"
                          f"{'/'.join(_COLLECTION_FIELD_TEXTS)} 这类合集控件"
                          f"(选择器待真号 fixtures 落定)",
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
    """播客发布表单的当场取证(表单结构未取证,先把页面原样交出去供人工判读)。"""
    evidence: Dict[str, Any] = {}
    try:
        evidence["page_text"] = _norm(page.inner_text("body"))[:1200]
    except Exception:  # noqa: BLE001 — 取证本身绝不制造新异常
        evidence["page_text"] = ""
    try:
        evidence["dropdown_present"] = (
            page.query_selector(_COLLECTION_OPTION_SCOPE) is not None
        )
    except Exception:  # noqa: BLE001
        evidence["dropdown_present"] = False
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
