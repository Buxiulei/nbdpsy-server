"""创作中心笔记可见性切换器(纯同步,吃已登录 page)。

设计 docs/design/2026-07-31-note-visibility-design.md 第 2.3 / 3.3 节(真号受控实验结论)。

按**笔记标题**在「笔记管理」页把一篇笔记切到目标可见性档位:
    进笔记管理页 → 滚动加载全部卡片 → 按标题精确定位(命中必须恰好 1 张)→
    拟人悬停显出操作图标 → 断言图标结构 → 点①权限设置 → **事后校验弹窗** →
    只读回读当前档位(已是目标即取消返回 skipped)→ 展开下拉选目标档 → 确定 →
    **重抓 posted 接口回读 permission_code 确认真的变了**。

安全约定(每一条都是真号实验踩出来的,不是理论):

- **绝不用坐标启发式定位图标**。上一轮调研拿"悬停后最右侧图标"当更多菜单,误命中删除
  图标弹出了删除确认框。这里只用 DOM 顺序 + 精确 class 断言:悬停后卡内
  ``.note-card__action-btn`` 必须**恰好 4 个**,``btns[0].class`` 必须**严格等于**
  ``note-card__action-btn``(不带任何修饰类),``btns[3]`` 必须含 ``--del``。
- **①权限设置 与 ③编辑 的 class 完全相同**,只能靠 DOM 顺序区分。因此点完 ``btns[0]``
  的**事后校验**(``.permission-modal`` 可见 **且** 所有可见弹窗文案都不含「删除」)是
  唯一挡住误点删除的东西,**绝不能省**。不满足立刻点弹窗内「取消」中止。
- **Escape 关不掉这条产品线的弹窗**(实测按了仍开着)。退出只能点弹窗内「取消」。
- **tooltip 定位不可用**(实测 tooltip/popper 选择器全返回空),别再试。
- 全程 ``SyncHumanActions``;``page.evaluate`` **只用于只读取证**(读弹窗可见性与文案),
  与 ``matrix_interact`` 读图标 href 同性质,不做任何 JS 合成点击或 JS 设值。
- **点了不算成功**:确定之后必须重抓一次 posted 接口,``permission_code`` 变成目标值
  才算 ``done``,否则抛错。

定位限制(如实记录,可接受的 v1 限制):靠标题定位,故**标题为空、或在该号下重复的
笔记无法定位**,一律 ``note_not_locatable`` —— 绝不猜。
"""

import time
from typing import Any, Dict, List, Optional

from loguru import logger

from app.browser.creator_export import _goto_creator
from app.browser.creator_note_list import (
    fetch_posted_notes,
    permission_code_of,
    permission_msg_of,
)
from app.browser.sync_human_actions import SyncHumanActions


class NoteVisibilityError(Exception):
    """可见性切换失败。``reason`` 携失败语义(如 note_not_locatable / verify_unchanged)。"""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


_NOTE_MANAGER_URL = "https://creator.xiaohongshu.com/new/note-manager"
_PUBLISH_URL = "https://creator.xiaohongshu.com/publish/publish?source=official"

_NOTE_CARD = ".note-card"
_ACTION_BTN = ".note-card__action-btn"
_PERM_SELECT = ".perm-select-wrapper"
_OPTION = ".d-options-wrapper .custom-option"

# 悬停后卡内操作图标的结构断言(设计 2.3 实测):恰好 4 个,①权限设置无修饰类、④删除带 --del
_EXPECTED_BTN_COUNT = 4
_PERMISSION_BTN_CLASS = "note-card__action-btn"
_DELETE_BTN_MARK = "--del"

# 平台档位文案。**本期只支持 0/1 两档**(另外三档与 user_ids 格式完全未验证,见设计第四节);
# _ALL_LABELS 用于只读识别当前档位——识别到未支持的档位不代表能切过去。
_PRIVACY_LABELS = {0: "公开可见", 1: "仅自己可见"}
_ALL_LABELS = (
    "公开可见",
    "仅自己可见",
    "仅互关好友可见",
    "部分人可见",
    "部分人不可见",
)

# 笔记管理页就绪等待 / 卡片懒加载滚动上限(37 篇实测分若干页,8 次滚动远超需要)
_MANAGER_READY_TIMEOUT_MS = 15000
_LOAD_MORE_SCROLLS = 8
# 点图标后等弹窗出现 / 展开下拉后等选项渲染(轮询上限,秒级)
_MODAL_TIMEOUT_S = 8.0
_OPTIONS_TIMEOUT_S = 6.0

# 只读取证:当前所有**可见**弹窗的状态。这是防误点删除的核心判据,故三件事一次读全:
# permission-modal 在不在、可见弹窗有几个、有没有任何一个提到「删除」。
# 可见性判定不用 offsetParent(弹窗是 position:fixed,fixed 元素 offsetParent 恒为 null),
# 改用矩形 + computedStyle。
_DIALOG_STATE_JS = r"""
() => {
    const visible = (el) => {
        const r = el.getBoundingClientRect();
        if (r.width < 1 || r.height < 1) return false;
        const s = getComputedStyle(el);
        return s.display !== 'none' && s.visibility !== 'hidden' && s.opacity !== '0';
    };
    const perm = [...document.querySelectorAll('.permission-modal')].filter(visible);
    const dialogs = [...document.querySelectorAll('.d-modal, [role=dialog]')]
        .filter(visible);
    return {
        permVisible: perm.length > 0,
        dialogCount: dialogs.length,
        mentionsDelete: dialogs.some((d) => (d.innerText || '').includes('删除')),
        texts: dialogs.map((d) => (d.innerText || '').trim().slice(0, 120)),
    };
}
"""


def _norm(text: Optional[str]) -> str:
    """空白归一(换行/多空格 → 单空格),便于文案精确比对。"""
    return " ".join((text or "").split())


def label_of(target_privacy: int) -> str:
    """目标档位 → 下拉文案;不支持的档位直接抛错(绝不猜一个文案去点)。"""
    label = _PRIVACY_LABELS.get(target_privacy)
    if label is None:
        raise NoteVisibilityError(
            f"unsupported_privacy: 本期只做 0=公开可见 / 1=仅自己可见,"
            f"收到 target_privacy={target_privacy!r}"
        )
    return label


# ---------------- 页面就绪与卡片定位 ----------------


def _open_note_manager(page, account_id: int) -> None:
    """进笔记管理页并等到笔记卡渲染出来;两次都不就绪按未登录处理。

    fast-path 与 ``creator_note_list`` 同款:cookie 双域已登录时直连即可;
    首访被重定向到登录页时用 publish 页预热 SSO 再重进。
    """
    for attempt in (1, 2):
        if attempt == 2:
            logger.info(
                f"[note_visibility] 账号{account_id}: 笔记管理页未就绪,"
                f"走 publish_url 预热 SSO 后重进"
            )
            _goto_creator(page, _PUBLISH_URL)
        _goto_creator(page, _NOTE_MANAGER_URL)
        try:
            page.locator(_NOTE_CARD).first.wait_for(
                state="visible", timeout=_MANAGER_READY_TIMEOUT_MS
            )
            logger.info(f"[note_visibility] 账号{account_id}: 笔记管理页就绪")
            return
        except Exception:
            continue
    raise NoteVisibilityError(
        "need_manual_login: 笔记管理页始终未渲染出笔记卡(creator 域可能需重新扫码登录)"
    )


def _load_all_cards(page, human: SyncHumanActions) -> List[Any]:
    """滚到卡片数不再增长,返回**全部**已加载的笔记卡。

    必须先加载全:命中数"恰好为 1"要在完整列表上判定,只看首屏会把翻页后的同题笔记
    漏掉,变成"以为唯一"的误定位。
    """
    cards = page.query_selector_all(_NOTE_CARD)
    for _ in range(_LOAD_MORE_SCROLLS):
        human.scroll("down")
        human.wait(0.8, 1.6, context="等更多笔记卡加载")
        more = page.query_selector_all(_NOTE_CARD)
        if len(more) <= len(cards):
            return more
        cards = more
    logger.warning(
        f"[note_visibility] 已达加载滚动上限 {_LOAD_MORE_SCROLLS},"
        f"卡片可能仍未加载全(当前 {len(cards)} 张),定位以已加载的为准"
    )
    return cards


def _title_hits(card, title: str) -> bool:
    """卡片是否命中标题:innerText 里**有一整行与标题精确相等**。

    不做 note_delete 那样的省略号前缀容忍:这里认错卡片的代价是把别人的笔记藏起来
    (或把用户刻意隐藏的笔记公开),宁可 note_not_locatable 也不放宽。
    """
    try:
        text = card.inner_text()
    except Exception:  # noqa: BLE001 — 卡片已失效,当不命中处理
        return False
    target = _norm(title)
    return any(_norm(line) == target for line in (text or "").splitlines())


def _locate_card(page, human: SyncHumanActions, title: str):
    """按标题定位唯一一张卡片;0 张或多张一律 ``note_not_locatable``(绝不猜)。"""
    if not _norm(title):
        raise NoteVisibilityError("note_not_locatable: 标题为空,无法定位笔记")
    cards = _load_all_cards(page, human)
    hits = [card for card in cards if _title_hits(card, title)]
    if len(hits) != 1:
        raise NoteVisibilityError(
            f"note_not_locatable: 标题「{title}」在 {len(cards)} 张卡片里命中 "
            f"{len(hits)} 张(要求恰好 1 张),拒绝操作"
        )
    return hits[0]


# ---------------- 图标结构断言 + 点权限设置 ----------------


def _action_buttons(page, human: SyncHumanActions, card, title: str) -> List[Any]:
    """悬停卡片显出操作图标,断言图标结构,返回 DOM 顺序的 4 个按钮。

    结构不符即抛错、**一个都不点** —— 顺序或数量一变,靠 DOM 顺序区分的"①权限设置"
    就可能是别的东西(③编辑与①class 完全相同,④是删除)。
    """
    human.scroll_to_element(card)
    box = card.bounding_box()
    if not box:
        raise NoteVisibilityError("card_no_box: 命中卡片坐标不可得,拒绝操作")
    # 悬停卡片上部(封面区)显出操作图标;这是**卡片**的坐标,不是靠坐标去猜图标
    human.hover(
        (box["x"] + box["width"] * 0.5, box["y"] + box["height"] * 0.35),
        reason=f"悬停笔记卡: {title[:15]}",
    )
    human.wait(0.5, 0.9, context="等操作图标显出")

    btns = card.query_selector_all(_ACTION_BTN)
    if len(btns) != _EXPECTED_BTN_COUNT:
        raise NoteVisibilityError(
            f"action_btns_unexpected: 悬停后卡内 {_ACTION_BTN} 有 {len(btns)} 个,"
            f"期望 {_EXPECTED_BTN_COUNT} 个;结构已变,拒绝按顺序点击"
        )
    classes = [_norm(btn.get_attribute("class")) for btn in btns]
    if classes[0] != _PERMISSION_BTN_CLASS:
        raise NoteVisibilityError(
            f"permission_btn_mismatch: 首个按钮 class={classes[0]!r},"
            f"期望严格等于 {_PERMISSION_BTN_CLASS!r};拒绝点击"
        )
    if _DELETE_BTN_MARK not in classes[3]:
        raise NoteVisibilityError(
            f"delete_btn_mismatch: 末个按钮 class={classes[3]!r} 不含 "
            f"{_DELETE_BTN_MARK!r},图标顺序与实测不符,拒绝点击"
        )
    logger.info(f"[note_visibility] 图标结构校验通过: {classes}")
    return btns


def _find_dialog_button(page, label: str):
    """在**可见弹窗**里按文案精确匹配按钮(「取消」「确定」);找不到返回 None。"""
    for sel in (
        ".d-modal .d-button",
        "[role=dialog] .d-button",
        ".d-modal button",
        "[role=dialog] button",
    ):
        for el in page.query_selector_all(sel):
            try:
                if _norm(el.inner_text()) == label and el.is_visible():
                    return el
            except Exception:  # noqa: BLE001 — 单个元素读失败只跳过它
                continue
    return None


def _dismiss(page, human: SyncHumanActions, reason: str) -> bool:
    """点弹窗内「取消」退出(**Escape 对这条产品线无效**,实测按了弹窗仍开着)。"""
    btn = _find_dialog_button(page, "取消")
    if btn is None:
        logger.error(
            f"[note_visibility] 未找到「取消」按钮,弹窗可能仍开着({reason});"
            f"Escape 对这条产品线无效,只能等会话结束随浏览器关闭"
        )
        return False
    human.click(btn, reason=f"取消({reason})")
    human.wait(0.4, 0.9, context="等弹窗关闭")
    return True


def _open_permission_modal(page, human: SyncHumanActions, btns: List[Any]) -> None:
    """点①权限设置,并做**事后校验**——这是唯一挡住误点删除的关卡,绝不能省。

    校验不过(不是权限弹窗 / 任何可见弹窗提到「删除」)立刻点「取消」中止。
    """
    human.click(btns[0], reason="权限设置(DOM 顺序第 1 个操作图标)")

    state: Dict[str, Any] = {}
    deadline = time.monotonic() + _MODAL_TIMEOUT_S
    while time.monotonic() < deadline:
        time.sleep(0.5)  # 等接口/动画,固定短轮询:这里等的是弹窗渲染不是拟人停顿
        state = page.evaluate(_DIALOG_STATE_JS)
        if state.get("dialogCount"):
            break

    if not state.get("dialogCount"):
        raise NoteVisibilityError("permission_modal_not_found: 点权限设置后没出现任何弹窗")
    if state.get("mentionsDelete") or not state.get("permVisible"):
        _dismiss(page, human, "弹窗不是权限设置")
        raise NoteVisibilityError(
            f"wrong_modal: 弹窗校验不通过(permVisible={state.get('permVisible')}, "
            f"mentionsDelete={state.get('mentionsDelete')}),疑似点到编辑/删除,已点取消中止。"
            f"弹窗文案: {state.get('texts')}"
        )
    logger.info(f"[note_visibility] 权限弹窗校验通过: {state.get('texts')}")


# ---------------- 档位读写 ----------------


def _current_label(page) -> Optional[str]:
    """只读回读当前档位文案(``.perm-select-wrapper`` 的 innerText 就是当前档位)。

    读不出或不是已知的五档 → None(当"未知",继续走正常提交流程,最终由回读校验兜底)。
    """
    el = page.query_selector(_PERM_SELECT)
    if el is None:
        return None
    text = _norm(el.inner_text())
    for label in _ALL_LABELS:
        if label in text:
            return label
    logger.warning(f"[note_visibility] 当前档位文案无法识别: {text!r}")
    return None


def _select_and_confirm(page, human: SyncHumanActions, target_label: str) -> None:
    """展开下拉 → 按文案精确匹配点选目标档 → 点「确定」提交。"""
    wrapper = page.query_selector(_PERM_SELECT)
    if wrapper is None:
        _dismiss(page, human, "档位下拉不存在")
        raise NoteVisibilityError("perm_select_not_found: 权限弹窗内没有档位下拉")
    human.click(wrapper, reason="展开可见性下拉")

    option = None
    deadline = time.monotonic() + _OPTIONS_TIMEOUT_S
    while time.monotonic() < deadline and option is None:
        time.sleep(0.4)  # 等下拉展开动画,固定短轮询
        for el in page.query_selector_all(_OPTION):
            try:
                if _norm(el.inner_text()) == target_label:
                    option = el
                    break
            except Exception:  # noqa: BLE001 — 单个选项读失败只跳过它
                continue
    if option is None:
        _dismiss(page, human, f"下拉里没有「{target_label}」")
        raise NoteVisibilityError(
            f"option_not_found: 下拉选项里没有文案精确等于「{target_label}」的档位"
        )
    human.click(option, reason=f"选择档位「{target_label}」")
    human.wait(0.4, 1.0, context="确认选中")

    confirm = _find_dialog_button(page, "确定")
    if confirm is None:
        _dismiss(page, human, "找不到确定按钮")
        raise NoteVisibilityError("confirm_button_not_found: 权限弹窗内没有「确定」按钮")
    human.click(confirm, reason=f"确定(切到「{target_label}」)")


# ---------------- 回读校验 ----------------


def _verify(page, account_id: int, note_id: str, target_privacy: int) -> Dict[str, Any]:
    """重抓 posted 接口回读该笔记的 permission_code;没变成目标值就抛错。

    **绝不"点了就当成功"**:提交那一步只有 UI 反馈,平台原值才是权威。
    """
    notes = fetch_posted_notes(page, account_id)
    raw = next((n for n in notes if (n.get("id") or "").strip() == note_id), None)
    if raw is None:
        raise NoteVisibilityError(
            f"verify_note_missing: 回读的 {len(notes)} 篇列表里找不到 note_id={note_id},"
            f"无法确认切换是否生效"
        )
    code = permission_code_of(raw)
    if code != target_privacy:
        raise NoteVisibilityError(
            f"verify_unchanged: 回读 permission_code={code!r},期望 {target_privacy};"
            f"切换未生效"
        )
    return {
        "permission_code": code,
        "permission_msg": permission_msg_of(raw),
    }


# ---------------- 对外入口 ----------------


def set_note_visibility(
    page, account_id: int, note_id: str, title: str, target_privacy: int
) -> Dict[str, Any]:
    """把某篇笔记切到目标可见性档位(全程驱动 UI,零 JS 注入)。

    Args:
        page: 已建好登录态的同步 Playwright Page(SyncClient.start 之后)。
        account_id: 账号 id(日志用)。
        note_id: 笔记 id(**回读校验用**,不是定位用——列表 DOM 里不暴露 note_id)。
        title: 笔记标题(定位用,精确匹配且必须唯一)。
        target_privacy: 目标档位,0=公开可见 / 1=仅自己可见(本期只做这两档)。

    Returns:
        ``{"status": "done", "permission_code", "permission_msg"}``——已回读确认生效;
        ``{"status": "skipped", "permission_code"}``——本就是目标档位,点取消未提交。

    Raises:
        NoteVisibilityError: 任一步失败(reason 携语义);失败时不会留下"改了一半"的状态
            ——要么没提交,要么提交了但回读没变(此时 reason 明说未生效)。
    """
    target_label = label_of(target_privacy)
    human = SyncHumanActions(page)
    _open_note_manager(page, account_id)
    human.wait(1.0, 2.0, context="笔记管理页浏览")

    card = _locate_card(page, human, title)
    btns = _action_buttons(page, human, card, title)
    _open_permission_modal(page, human, btns)

    current = _current_label(page)
    if current == target_label:
        logger.info(
            f"[note_visibility] 账号{account_id} 笔记「{title[:15]}」本就是"
            f"「{target_label}」,点取消不提交"
        )
        _dismiss(page, human, "已是目标档位")
        return {"status": "skipped", "permission_code": target_privacy}

    logger.info(
        f"[note_visibility] 账号{account_id} 笔记「{title[:15]}」: "
        f"{current!r} → 「{target_label}」"
    )
    _select_and_confirm(page, human, target_label)
    human.wait(1.5, 3.0, context="等权限提交落地")

    verified = _verify(page, account_id, note_id, target_privacy)
    logger.info(
        f"[note_visibility] ✓ 账号{account_id} 笔记 {note_id} 已切到 "
        f"permission_code={verified['permission_code']}"
    )
    return {"status": "done", **verified}
