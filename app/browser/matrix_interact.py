"""笔记互动器(纯同步,吃已登录 page):矩阵互动(点赞 / 收藏)+ 独立评论。

两个对外入口,共用同一套主页定位与拟人浏览:

- ``interact_with_note``:矩阵互动,**只做点赞 + 收藏**(发布成功后自动触发);
- ``comment_on_note``:单篇评论(手工触发,走 REST ``note-comments``)。

评论 2026-07-31 从矩阵互动三件套里**结构上移除**——不是靠传空文案绕过,而是矩阵互动
压根不再有评论这一步;两件事的触发方式与幂等性都不同,合在一起只会让成败判定变形
(见 ``interact_with_note`` 末尾关于成败判定的注释)。

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

# 评论是否提交成功(只读取证):listed = 文案出现在页面(评论列表)= 判据;
# cleared = 输入框是否已清空 = 附加信息,不参与判定(理由见 _do_comment 复核段注释)。
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


def _card_matches_note_id(card, note_id: str) -> bool:
    """卡片的任一链接 href 里是否含该 note_id(主页卡片的封面链接带笔记 id)。

    读 href 用 ``get_attribute`` 直接读 DOM,不经 ``page.evaluate`` —— 与本模块"只读取证"
    的口径一致,连读都尽量不进 JS。
    """
    if not note_id:
        return False
    try:
        for link in card.query_selector_all("a"):
            href = link.get_attribute("href") or ""
            if note_id in href:
                return True
    except Exception:  # noqa: BLE001 — 单张卡读失败当不命中
        return False
    return False


def _open_note_by_title(
    page,
    human: SyncHumanActions,
    publisher_user_id: str,
    title: str,
    note_id: Optional[str] = None,
) -> str:
    """拟人导航发布者主页 → **优先按 note_id**、否则按标题匹配笔记卡 → 点进详情;返回 URL。

    ``note_id`` 优先(2026-08-01):主页卡片的链接 href 里带笔记 id,是稳定主键;而标题
    会变(实测平台上「粤语咨询师-黄安麟…」在台账里记的是「心理咨询师-…」)。给了 note_id
    但页面上没有对应卡片时**回退标题匹配**(可能是没滚到 / 卡片结构变了),不直接判失败。

    两种方式都匹配不到即抛 ``MatrixInteractError``(绝不退而求其次点第一篇)。
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

    cards = page.query_selector_all(_NOTE_CARD)
    hit = next((c for c in cards if _card_matches_note_id(c, note_id or "")), None)
    if hit is not None:
        logger.info(f"[matrix_interact] 按 note_id={note_id} 命中笔记卡")
    else:
        if note_id:
            logger.warning(
                f"[matrix_interact] 主页 {len(cards)} 张卡里没有 note_id={note_id} 的链接,"
                f"回退按标题「{title}」匹配"
            )
        for card in cards:
            try:
                text = card.inner_text()
            except Exception:
                continue
            if _title_matches(text, title):
                hit = card
                break
    if hit is None:
        raise MatrixInteractError(
            f"note_not_found: 发布者主页未找到 note_id={note_id!r} / 标题「{title}」的笔记卡"
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

    返回 ``{"status": "done"|"error", "reason"?, "cleared"?}``;``cleared`` 是复核时
    输入框是否已清空,**仅供排查**不参与成败判定。文案由调用方传入且**必填**——评论
    自 2026-07-31 起是独立能力(``comment_on_note`` / REST ``note-comments``),不再是
    矩阵互动里那个"可以不传就跳过"的可选动作,故空文案是**入参错误**记 error。
    (历史上这里返回过 ``not_requested``,那是为了让"没要求评论"不被当成失败证据;
    评论独立后不存在"没要求"这回事,该状态一并取消,见 ``interact_with_note`` 的成败判定。)
    """
    if not (text or "").strip():
        return {"status": "error", "reason": "comment_text_empty: 未提供评论文案"}

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

    # 复核:文案出现在评论列表(listed)即算发出,cleared 只作附加信息随结果带出。
    #
    # 为什么 cleared 不能当判据:它是**前端表现**——输入框残留空白字符、placeholder
    # 被读成内容、或清空比列表渲染慢一拍而我们读得太早,都会让 cleared=False。而
    # listed=True 意味着评论已经渲染进列表,是**服务端已接收**的铁证。曾把两者做成
    # "与",导致 7 条真发出去的评论被记 error(台账失真,且一旦有重试会重复发)。
    #
    # 为什么 listed 不能松:它防的是"点了发送但根本没发出去"——只看点击动作就判成功
    # 会把发送失败一律记成 done,这是当初设复核的原始初衷,必须保留。
    snippet = (text or "").strip()[:12]
    posted = {}
    deadline = time.monotonic() + 12.0
    while time.monotonic() < deadline:
        time.sleep(0.6)
        posted = page.evaluate(_COMMENT_POSTED_JS, snippet)
        if posted.get("listed"):
            logger.info(
                f"[matrix_interact] ✓ 评论已发出: {snippet!r}"
                f"(cleared={posted.get('cleared')})"
            )
            return {"status": "done", "cleared": bool(posted.get("cleared"))}
    return {
        "status": "error",
        "reason": (
            f"comment_unverified: 发送后评论未出现在列表"
            f"(cleared={posted.get('cleared')}, listed={posted.get('listed')})"
        ),
        "cleared": bool(posted.get("cleared")),
    }


def interact_with_note(
    page,
    account_id: int,
    publisher_user_id: str,
    title: str,
    note_id: Optional[str] = None,
) -> Dict[str, Any]:
    """对发布者某篇笔记执行点赞 + 收藏(动作粒度汇总,互不阻断)。

    2026-07-31 起**不含评论**:评论是独立能力,走 ``comment_on_note``。

    Args:
        page: 已建好登录态的同步 Playwright Page(SyncClient.start 之后)。
        account_id: 互动方账号 id(日志用)。
        publisher_user_id: 发布者的小红书 user_id(主页路径定位用)。
        title: 目标笔记标题(``note_id`` 给不出时的兜底匹配依据,匹配不到即放弃)。
        note_id: 目标笔记的平台 id,**定位优先用它**(与 ``comment_on_note`` 同款:
            主页卡片链接里带 id,比标题稳 —— 台账 title 会过期)。

    Returns:
        ``{"note_url": str, "actions": {"like"/"collect": {...}}}``;两个动作**全部**
        未成功(既无 done 也无 skipped)时额外带 ``"error"`` 键,让台账落 error 而非假 done。

    Raises:
        MatrixInteractError: 笔记定位/打开失败(此时一个动作都没做)。
    """
    human = SyncHumanActions(page)
    note_url = _open_note_by_title(page, human, publisher_user_id, title, note_id)
    _browse_note(human)

    actions: Dict[str, Dict[str, Any]] = {}
    steps = (
        ("like", lambda: _icon_action(
            page, human, "点赞", _LIKE_SELECTORS, "#like", "#liked")),
        ("collect", lambda: _icon_action(
            page, human, "收藏", _COLLECT_SELECTORS, "#collect", "#collected")),
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
    # 成败判定:两个动作都不是 done/skipped 才算整体失败。
    #
    # 这里**故意不再有**"先剔除某些状态、剔空则不判失败"那一层。评论还在三件套里时,它
    # 可以是 not_requested(没传文案 = 这次没要求做),必须先剔除、且剔空后不判失败,
    # 否则空文案会把真失败顶成 done;而那个"剔空不判失败"的兜底本身就是老缺陷的形状——
    # 一旦所有动作都可缺席,error 就永远落不下来。评论移走后 like/collect 由上面的循环
    # **无条件各跑一次**(异常也被 except 兜成 error 写回 actions),actions 恒为 2 条、
    # 恒无 not_requested,所以判据可以、也必须是直接对全部动作取 any:没有任何一个动作
    # 成功 = 失败。将来若要再加"可缺席"的动作,不能退回旧写法,而应让缺席动作压根不进
    # actions,判据保持不变。
    if not any(a["status"] in ("done", "skipped") for a in actions.values()):
        result["error"] = "点赞与收藏均失败"
    return result


def comment_on_note(
    page,
    account_id: int,
    publisher_user_id: str,
    title: str,
    comment_text: str,
    note_id: Optional[str] = None,
) -> Dict[str, Any]:
    """对发布者某篇笔记发一条评论(独立能力,不含点赞收藏)。

    定位与拟人浏览完全复用矩阵互动那套(主页 → 按标题匹配卡片 → 进详情 → 滚动阅读),
    评论动作复用真号验证过的 ``_do_comment``,**不重写**。

    Args:
        page: 已建好登录态的同步 Playwright Page(SyncClient.start 之后)。
        account_id: 评论方账号 id(日志用)。
        publisher_user_id: 发布者的小红书 user_id(主页路径定位用)。
        title: 目标笔记标题(``note_id`` 给不出时的兜底匹配依据,匹配不到即放弃)。
        comment_text: 评论文案,**必填**(空文案在 ``_do_comment`` 里记 error)。
        note_id: 目标笔记的平台 id,**定位优先用它**(主页卡片链接里带 id,比标题稳)。

    Returns:
        成功 ``{"note_url": str, "commented": True}``;评论未发出时带 ``"error"`` 键
        (调用方据此落台账 error)。

    Raises:
        MatrixInteractError: 笔记定位/打开失败(此时没评论出去)。
    """
    human = SyncHumanActions(page)
    note_url = _open_note_by_title(page, human, publisher_user_id, title, note_id)
    _browse_note(human)

    try:
        outcome = _do_comment(page, human, comment_text)
    except Exception as exc:  # 兜底:异常也要给结构化结果,别让上层拿不到 note_url
        logger.warning(f"[note_comment] 账号{account_id} 评论动作异常: {exc}")
        outcome = {"status": "error", "reason": f"comment_exception: {exc}"}
    logger.info(
        f"[note_comment] 账号{account_id} 评论: {outcome['status']}"
        f" {outcome.get('reason', '')}"
    )

    if outcome["status"] != "done":
        return {
            "note_url": note_url,
            "error": outcome.get("reason") or "comment_failed",
        }
    return {"note_url": note_url, "commented": True}
