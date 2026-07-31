"""矩阵互动器(纯同步,吃已登录 page):点赞 / 收藏 / 评论。

设计见 docs/design/2026-07-31-matrix-interact-design.md(真号实验结论),三条硬约定:

- **主页路径现场定位**:库里没有真实笔记链接(``publish_jobs.note_url`` 存的是 creator
  发布成功页、``note_id`` 全为空),故走发布者主页 → 按标题匹配笔记卡 → 拟人点进详情
  (URL 自动带上 xsec_token,由当前会话生成,无需预存)。**匹配不到即放弃,绝不默认
  取第一篇**——窗口内发布者可能发了多篇,取第一篇会点错笔记。
- **已赞/已藏只看 ``use[xlink:href]``**(#like/#liked、#collect/#collected)。旧仓
  ``already_liked = "like-active" in class`` 是错的:实测该 class 点赞前后常驻,
  照搬会 100% 误判为"已点赞"。
- **``.not-active.inner-when-not-active`` 是未激活的评论入口,不是遮罩**:拟人点它激活
  输入区,绝不用 JS 把它 display:none 隐藏(旧仓 comment_note 的做法是把入口当障碍物拆了)。

全程 ``SyncHumanActions``;``page.evaluate`` 只用于**只读取证**(读图标 href / 读按钮
class / 读命中元素),与 ``creator_export`` 读表格行数同性质,不做任何 JS 合成点击或 JS 设值。

任一动作失败不阻断其余动作,结果按动作粒度汇总(见 ``interact_with_note`` 返回值)。
"""

import time
from typing import Any, Dict, List, Optional

from loguru import logger

from app.browser.sync_human_actions import SyncHumanActions


class MatrixInteractError(Exception):
    """互动前置失败(定位不到笔记 / 详情打不开)。``reason`` 携失败语义。"""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


# 发布者主页 + 笔记卡片(主页路径的两个锚点)
_PROFILE_URL = "https://www.xiaohongshu.com/user/profile/{user_id}"
_NOTE_CARD = "section.note-item"

# 互动栏三按钮:优先 .engage-bar 内定位,失败退到裸 class(改版容错)
_ENGAGE_READY = ".interactions.engage-bar, .engage-bar"
_LIKE_SELECTORS = [".engage-bar .like-wrapper", ".like-wrapper"]
_COLLECT_SELECTORS = [".engage-bar .collect-wrapper", ".collect-wrapper"]
_COMMENT_ENTRY_SELECTORS = [
    ".engage-bar .not-active.inner-when-not-active",
    ".not-active.inner-when-not-active",
    ".engage-bar .inner",
]
_TEXTAREA = "#content-textarea"
_SUBMIT = "button.btn.submit"

# 读互动按钮内 <use> 的图标 href(只读取证):#like 未赞 / #liked 已赞,收藏同构。
_READ_ICON_HREF_JS = r"""
(sel) => {
    const el = document.querySelector(sel + ' use');
    if (!el) return null;
    return el.getAttribute('xlink:href') || el.getAttribute('href') || null;
}
"""

# 评论输入区是否真可交互(只读取证):未激活态下 #content-textarea 中心点被 SPAN 覆盖,
# elementFromPoint 命中的不是输入框自身 —— 这正是"点入口激活"是否生效的判据。
_TEXTAREA_READY_JS = r"""
() => {
    const ta = document.querySelector('#content-textarea');
    if (!ta) return {ready: false, reason: 'no_textarea'};
    const r = ta.getBoundingClientRect();
    if (r.width < 1 || r.height < 1) return {ready: false, reason: 'zero_rect'};
    const hit = document.elementFromPoint(r.x + r.width / 2, r.y + r.height / 2);
    const inside = !!hit && (hit === ta || ta.contains(hit) || hit.contains(ta));
    return {ready: inside, reason: inside ? 'ok' : 'covered'};
}
"""

# 发送按钮状态(只读取证):class 带 gray = 禁用(空文案 / 输入未被接收)。
_SUBMIT_STATE_JS = r"""
() => {
    const btn = document.querySelector('button.btn.submit');
    if (!btn) return {found: false, gray: true};
    const cls = btn.className || '';
    return {found: true, gray: /\bgray\b/.test(cls)};
}
"""

# 评论是否提交成功(只读取证):输入框已清空 + 文案出现在页面(评论列表)。
_COMMENT_POSTED_JS = r"""
(snippet) => {
    const ta = document.querySelector('#content-textarea');
    const taText = ta ? (ta.innerText || ta.value || '').trim() : '';
    const body = document.body ? (document.body.innerText || '') : '';
    return {cleared: taText === '', listed: body.indexOf(snippet) >= 0};
}
"""


def _norm(text: Optional[str]) -> str:
    """空白归一(卡片文本换行/多空格 → 单空格),便于标题比对。"""
    return " ".join((text or "").split())


def _title_matches(card_text: Optional[str], title: str) -> bool:
    """卡片文本是否命中目标标题(容忍卡片标题被截断成省略号)。

    命中判据(任一成立):卡片文本包含完整标题;或某行去掉省略号后是标题的前缀且
    ≥8 字(与 note_delete 同款容忍度,短前缀不认,避免误命中同前缀的另一篇)。
    """
    target = _norm(title)
    if not target:
        return False
    if target in _norm(card_text):
        return True
    for raw_line in (card_text or "").splitlines():
        line = _norm(raw_line)
        if not line:
            continue
        trimmed = line.rstrip("…").rstrip(".").strip()
        if len(trimmed) >= 8 and target.startswith(trimmed):
            return True
    return False


def _resolve_selector(page, candidates: List[str]) -> Optional[str]:
    """返回候选里第一个在页面上命中的选择器;都不命中返回 None。"""
    for sel in candidates:
        try:
            if page.query_selector(sel) is not None:
                return sel
        except Exception:
            continue
    return None


def _open_note_by_title(
    page, human: SyncHumanActions, publisher_user_id: str, title: str
) -> str:
    """拟人导航发布者主页 → 按标题匹配笔记卡 → 点进详情;返回详情页 URL。

    匹配不到标题即抛 ``MatrixInteractError``(绝不退而求其次点第一篇)。
    """
    url = _PROFILE_URL.format(user_id=publisher_user_id)
    human.navigate(url)
    try:
        page.locator(_NOTE_CARD).first.wait_for(state="visible", timeout=20000)
    except Exception:
        raise MatrixInteractError(
            f"profile_not_loaded: 发布者主页未渲染出笔记卡片({url})"
        )
    human.wait(1.2, 2.8, context="主页浏览")

    hit = None
    for card in page.query_selector_all(_NOTE_CARD):
        try:
            text = card.inner_text()
        except Exception:
            continue
        if _title_matches(text, title):
            hit = card
            break
    if hit is None:
        raise MatrixInteractError(
            f"note_not_found: 发布者主页未找到标题「{title}」的笔记卡"
        )

    human.scroll_to_element(hit)
    box = hit.bounding_box()
    if not box:
        raise MatrixInteractError("note_card_no_box: 命中卡片坐标不可得")
    # 点卡片上部(封面区):底部是作者/赞数行,点那里会跳作者页而非笔记详情
    human.click(
        (box["x"] + box["width"] * 0.5, box["y"] + box["height"] * 0.35),
        reason=f"进入笔记: {title[:15]}",
    )
    try:
        page.locator(_ENGAGE_READY).first.wait_for(state="visible", timeout=20000)
    except Exception:
        raise MatrixInteractError("note_open_failed: 点开笔记后互动栏未出现")
    logger.info(f"[matrix_interact] 已进入笔记详情: {page.url[:120]}")
    return page.url


def _browse_note(human: SyncHumanActions) -> None:
    """进笔记先浏览再互动:滚动看正文 + 随机停留(不得秒进秒赞)。"""
    human.wait(1.0, 2.5, context="笔记首屏停留")
    human.scroll("down")
    human.wait(1.5, 3.5, context="阅读正文")
    human.scroll("down")
    human.wait(1.0, 3.0, context="继续阅读")


def _icon_action(
    page,
    human: SyncHumanActions,
    name: str,
    selectors: List[str],
    off_href: str,
    on_href: str,
    verify_timeout_s: float = 8.0,
) -> Dict[str, Any]:
    """点赞/收藏同构动作:读图标 → 已激活则跳过 → 拟人点击 → 复核图标真的变了。

    返回 ``{"status": "done"|"skipped"|"error", "reason"?}``;已激活记 skipped 非 error。
    """
    sel = _resolve_selector(page, selectors)
    if sel is None:
        return {"status": "error", "reason": f"{name}_button_not_found"}
    href = page.evaluate(_READ_ICON_HREF_JS, sel)
    if href and href.endswith(on_href):
        return {"status": "skipped", "reason": f"已{name}"}
    if not href or not href.endswith(off_href):
        # 图标读不出来就不点:宁可不动,也不在状态未知时盲点(盲点可能取消已有互动)
        return {"status": "error", "reason": f"{name}_icon_unreadable: {href!r}"}

    element = page.query_selector(sel)
    if element is None:
        return {"status": "error", "reason": f"{name}_button_detached"}
    human.click(element, reason=f"{name}按钮")

    deadline = time.monotonic() + verify_timeout_s
    while time.monotonic() < deadline:
        time.sleep(0.5)
        now_href = page.evaluate(_READ_ICON_HREF_JS, sel)
        if now_href and now_href.endswith(on_href):
            logger.info(f"[matrix_interact] ✓ {name}生效: {href} → {now_href}")
            return {"status": "done"}
    return {
        "status": "error",
        "reason": f"{name}_not_effective: 点击后图标未变为 {on_href}",
    }


def _do_comment(page, human: SyncHumanActions, text: str) -> Dict[str, Any]:
    """评论:激活入口 → 等输入区可交互 → 逐字输入 → 等发送键可用 → 发送 → 复核。

    文案由调用方(payload)传入,为空即跳过评论(记 skipped 非 error)。
    """
    if not (text or "").strip():
        return {"status": "skipped", "reason": "无评论文案"}

    entry_sel = _resolve_selector(page, _COMMENT_ENTRY_SELECTORS)
    if entry_sel is None:
        return {"status": "error", "reason": "comment_entry_not_found"}
    entry = page.query_selector(entry_sel)
    if entry is None:
        return {"status": "error", "reason": "comment_entry_detached"}
    human.click(entry, reason="激活评论输入区")

    # 轮询等输入框真正可交互(未激活态中心点被 SPAN 盖住)
    ready = {}
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline:
        time.sleep(0.4)
        ready = page.evaluate(_TEXTAREA_READY_JS)
        if ready.get("ready"):
            break
    if not ready.get("ready"):
        return {
            "status": "error",
            "reason": f"comment_input_not_ready: {ready.get('reason')}",
        }

    textarea = page.query_selector(_TEXTAREA)
    if textarea is None:
        return {"status": "error", "reason": "comment_textarea_detached"}
    # type_text 默认 click_first=True:先拟人点击聚焦,再逐字输入(自带节奏与偶发退格)
    human.type_text(textarea, text)

    # 轮询等发送键去掉 gray(输入被前端接收的判据)
    state = {}
    deadline = time.monotonic() + 8.0
    while time.monotonic() < deadline:
        time.sleep(0.4)
        state = page.evaluate(_SUBMIT_STATE_JS)
        if state.get("found") and not state.get("gray"):
            break
    if not state.get("found") or state.get("gray"):
        return {"status": "error", "reason": "comment_submit_disabled: 发送键仍禁用"}

    submit = page.query_selector(_SUBMIT)
    if submit is None:
        return {"status": "error", "reason": "comment_submit_detached"}
    human.click(submit, reason="发送评论")

    # 复核:输入框清空 + 文案出现在页面(评论列表)
    snippet = (text or "").strip()[:12]
    posted = {}
    deadline = time.monotonic() + 12.0
    while time.monotonic() < deadline:
        time.sleep(0.6)
        posted = page.evaluate(_COMMENT_POSTED_JS, snippet)
        if posted.get("cleared") and posted.get("listed"):
            logger.info(f"[matrix_interact] ✓ 评论已发出: {snippet!r}")
            return {"status": "done"}
    return {
        "status": "error",
        "reason": (
            f"comment_unverified: 发送后未复核到评论"
            f"(cleared={posted.get('cleared')}, listed={posted.get('listed')})"
        ),
    }


def interact_with_note(
    page,
    account_id: int,
    publisher_user_id: str,
    title: str,
    comment_text: str = "",
) -> Dict[str, Any]:
    """对发布者某篇笔记执行点赞 + 收藏 + 评论(动作粒度汇总,互不阻断)。

    Args:
        page: 已建好登录态的同步 Playwright Page(SyncClient.start 之后)。
        account_id: 互动方账号 id(日志用)。
        publisher_user_id: 发布者的小红书 user_id(主页路径定位用)。
        title: 目标笔记标题(标题匹配,匹配不到即放弃)。
        comment_text: 评论文案(payload 入参);为空则跳过评论,只点赞收藏。

    Returns:
        ``{"note_url": str, "actions": {"like"/"collect"/"comment": {...}}}``;
        三个动作全部未成功(既无 done 也无 skipped)时额外带 ``"error"`` 键,
        让台账落 error 而非假 done。

    Raises:
        MatrixInteractError: 笔记定位/打开失败(此时一个动作都没做)。
    """
    human = SyncHumanActions(page)
    note_url = _open_note_by_title(page, human, publisher_user_id, title)
    _browse_note(human)

    actions: Dict[str, Dict[str, Any]] = {}
    steps = (
        ("like", lambda: _icon_action(
            page, human, "点赞", _LIKE_SELECTORS, "#like", "#liked")),
        ("collect", lambda: _icon_action(
            page, human, "收藏", _COLLECT_SELECTORS, "#collect", "#collected")),
        ("comment", lambda: _do_comment(page, human, comment_text)),
    )
    for i, (key, step) in enumerate(steps):
        if i:
            human.wait(1.5, 4.0, context="互动间隔")
        try:
            actions[key] = step()
        except Exception as exc:  # 单个动作异常不阻断其余动作
            logger.warning(f"[matrix_interact] 账号{account_id} {key} 动作异常: {exc}")
            actions[key] = {"status": "error", "reason": f"{key}_exception: {exc}"}
        logger.info(
            f"[matrix_interact] 账号{account_id} {key}: {actions[key]['status']}"
            f" {actions[key].get('reason', '')}"
        )

    result: Dict[str, Any] = {"note_url": note_url, "actions": actions}
    if not any(a["status"] in ("done", "skipped") for a in actions.values()):
        result["error"] = "全部互动动作失败"
    return result
