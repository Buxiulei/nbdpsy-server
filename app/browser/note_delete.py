"""创作中心笔记删除器(纯同步,吃已登录 page)。

按**笔记标题**在创作中心「笔记管理」页删除笔记:
    creator warm-up(复用 creator_export 的 SSO 建会话)→ 侧边栏进「笔记管理」→
    按标题定位笔记卡片 → 拟人悬停(悬停才显出操作图标)→ 点垃圾桶图标 →
    确认弹窗(必须含「删除」字样,否则 Escape 拒点)→ 校验卡片数真的减少。

关键约定:
- 创作中心导出无 note_id,业务上以标题定位;同题多篇(重复发布)时删**第一张匹配卡**,
  ``count`` 参数支持一次会话删多篇(逐篇删+校验,省去反复起浏览器的会话开销)。
- 删除是**不可逆**操作:垃圾桶图标优先按 class/aria 含 delete/trash 匹配,匹配不到才
  退到「悬停图标区最右一个」(实测 UI 垃圾桶在最右);确认弹窗文案必须含「删除」,
  否则视为点错图标,Escape 收场并 fail-loud,绝不误确认。
- 全程拟人化(SyncHumanActions hover/click),禁 JS 合成点击。
"""

import time
from typing import Any, Dict

from loguru import logger

from app.browser.creator_export import _goto_creator
from app.browser.sync_human_actions import SyncHumanActions


class NoteDeleteError(Exception):
    """删除失败。``reason`` 携失败语义(如 need_manual_login / note_not_found)。"""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


# 笔记管理页就绪标志:状态 tab 栏(全部/已发布/审核中/未通过)。
_MANAGE_READY_SELECTOR = 'text=已发布'

# 定位标题卡片:叶子节点文本精确等于标题(或以标题开头,容忍截断省略号),
# 向上走到合理尺寸的卡片容器,返回卡片矩形与同题总数。
_FIND_CARD_JS = r"""
(title) => {
    const leaves = [...document.querySelectorAll('*')].filter(
        (e) => e.children.length === 0 && e.offsetParent !== null);
    const matches = [];
    for (const el of leaves) {
        const t = (el.textContent || '').trim();
        if (!t) continue;
        if (t === title || (t.length >= 8 && title.startsWith(t.replace(/\.{3}|…$/, '')))
            || t.startsWith(title)) {
            matches.push(el);
        }
    }
    if (!matches.length) return {found: false, count: 0};
    // 同一张卡内可能有多个命中叶子(标题元素/封面 alt),按卡片矩形去重,防计数虚增
    const cards = [];
    const seen = new Set();
    for (const el of matches) {
        let node = el;
        for (let i = 0; i < 8 && node; i++) {
            const r = node.getBoundingClientRect();
            if (r.width > 250 && r.width < 1000 && r.height > 60 && r.height < 400) {
                const key = Math.round(r.x) + ',' + Math.round(r.y);
                if (!seen.has(key)) {
                    seen.add(key);
                    cards.push({x: r.x, y: r.y, w: r.width, h: r.height});
                }
                break;
            }
            node = node.parentElement;
        }
    }
    if (!cards.length) return {found: false, count: matches.length};
    return {found: true, count: cards.length, card: cards[0]};
}
"""

# 悬停后在卡片矩形内找垃圾桶图标:优先 class/aria 含 delete/trash/del 的可点元素;
# 匹配不到退到卡片上部图标带的最右一个(实测垃圾桶在最右)。返回坐标+识别片段。
_FIND_TRASH_JS = r"""
(card) => {
    const inCard = (r) => r.x >= card.x - 8 && r.x + r.width <= card.x + card.w + 8
        && r.y >= card.y - 8 && r.y + r.height <= card.y + card.h + 8;
    const icons = [];
    for (const el of document.querySelectorAll('svg, button, [role=button], i, span, use')) {
        const r = el.getBoundingClientRect();
        if (r.width < 8 || r.width > 60 || r.height < 8 || r.height > 60) continue;
        if (!inCard(r)) continue;
        const idText = [el.className.baseVal || el.className || '',
                        el.getAttribute('aria-label') || '',
                        el.getAttribute('href') || '',
                        (el.querySelector && el.querySelector('use')
                            ? (el.querySelector('use').getAttribute('xlink:href')
                               || el.querySelector('use').getAttribute('href') || '')
                            : '')].join(' ');
        icons.push({x: r.x + r.width / 2, y: r.y + r.height / 2, id: idText});
    }
    if (!icons.length) return {found: false, iconCount: 0};
    const hit = icons.find((i) => /delete|trash|\bdel\b|shanchu/i.test(i.id));
    if (hit) return {found: true, x: hit.x, y: hit.y, via: 'class匹配', id: hit.id.slice(0, 80),
                     iconCount: icons.length};
    icons.sort((a, b) => b.x - a.x);
    return {found: true, x: icons[0].x, y: icons[0].y, via: '最右图标回退',
            id: icons[0].id.slice(0, 80), iconCount: icons.length};
}
"""

# 找确认弹窗:可见 dialog/modal 且文案含「删除」;返回确认按钮(非「取消」)坐标。
_FIND_CONFIRM_JS = r"""
() => {
    const dlgs = [...document.querySelectorAll(
        '[role=dialog],.d-modal,.modal,[class*=dialog],[class*=Modal],[class*=confirm]')]
        .filter((d) => d.offsetParent !== null
            && d.getBoundingClientRect().width > 150);
    for (const dlg of dlgs) {
        const text = (dlg.innerText || '').trim();
        if (!text) continue;
        const mentionsDelete = text.includes('删除');
        const btns = [...dlg.querySelectorAll('button, [role=button]')]
            .map((b) => ({b, t: (b.innerText || '').trim(),
                          r: b.getBoundingClientRect()}))
            .filter((x) => x.r.width > 30 && x.r.height > 18);
        const ok = btns.find((x) => /^(确定|确认|删除)$/.test(x.t));
        if (ok) return {found: true, mentionsDelete, dialogText: text.slice(0, 150),
                        x: ok.r.x + ok.r.width / 2, y: ok.r.y + ok.r.height / 2,
                        btnText: ok.t};
        if (mentionsDelete) return {found: true, mentionsDelete, noBtn: true,
                                    dialogText: text.slice(0, 150)};
    }
    return {found: false};
}
"""


def _open_note_manage(page, human: SyncHumanActions, account_id: int) -> None:
    """creator warm-up 建 SSO(goto publish 页)→ goto 笔记管理页 → 等状态 tab 就绪。"""
    for attempt in range(1, 4):
        _goto_creator(
            page, "https://creator.xiaohongshu.com/publish/publish?source=official")
        _goto_creator(page, "https://creator.xiaohongshu.com/new/note-manager")
        try:
            page.locator(_MANAGE_READY_SELECTOR).first.wait_for(
                state="visible", timeout=12000)
            logger.info(f"[note_delete] 账号{account_id}: 笔记管理页就绪(第 {attempt}/3 次)")
            return
        except Exception:
            # 新路径没就绪 → 试旧路径,再不行整轮重来
            _goto_creator(page, "https://creator.xiaohongshu.com/creator/notemanage")
            try:
                page.locator(_MANAGE_READY_SELECTOR).first.wait_for(
                    state="visible", timeout=8000)
                logger.info(f"[note_delete] 账号{account_id}: 笔记管理页(旧路径)就绪")
                return
            except Exception:
                logger.warning(
                    f"[note_delete] 账号{account_id}: 笔记管理页第 {attempt}/3 次未就绪,重试")
                time.sleep(2)
    raise NoteDeleteError("need_manual_login")


def _delete_first_match(page, human: SyncHumanActions, title: str) -> int:
    """删除第一张标题匹配卡,返回删除前同题卡数;全程校验,任一步失败抛 NoteDeleteError。"""
    found = page.evaluate(_FIND_CARD_JS, title)
    if not found.get("found"):
        raise NoteDeleteError(f"note_not_found: 笔记管理页未找到标题「{title}」的卡片")
    before = int(found["count"])
    card = found["card"]

    # 悬停卡片(悬停才显出操作图标),再定位垃圾桶
    human.hover((card["x"] + card["w"] * 0.5, card["y"] + card["h"] * 0.35),
                reason=f"悬停笔记卡: {title[:15]}")
    human.wait(0.5, 0.9, context="等操作图标显出")
    trash = page.evaluate(_FIND_TRASH_JS, card)
    if not trash.get("found"):
        raise NoteDeleteError(
            f"trash_icon_not_found: 悬停后未找到删除图标(iconCount="
            f"{trash.get('iconCount', 0)})")
    logger.info(f"[note_delete] 垃圾桶定位: via={trash['via']} id={trash.get('id', '')!r}")
    human.click((trash["x"], trash["y"]), reason="删除图标(垃圾桶)")

    # 等确认弹窗;文案必须含「删除」,否则视为点错图标 → Escape 收场 fail-loud
    confirm = None
    for _ in range(8):
        time.sleep(0.5)
        confirm = page.evaluate(_FIND_CONFIRM_JS)
        if confirm.get("found"):
            break
    if not confirm or not confirm.get("found"):
        raise NoteDeleteError("confirm_dialog_not_found: 点删除图标后未出现确认弹窗")
    if not confirm.get("mentionsDelete"):
        human.press_key("Escape", reason="弹窗不含删除字样,拒点收场")
        raise NoteDeleteError(
            f"wrong_dialog: 弹窗不含「删除」字样,疑似点错图标,已 Escape。"
            f"弹窗文案: {confirm.get('dialogText', '')!r}")
    if confirm.get("noBtn"):
        human.press_key("Escape", reason="确认按钮缺失,拒点收场")
        raise NoteDeleteError("confirm_button_not_found: 删除确认弹窗内未找到确认按钮")
    logger.info(f"[note_delete] 确认弹窗: {confirm['dialogText']!r} → 点「{confirm['btnText']}」")
    human.click((confirm["x"], confirm["y"]), reason=f"确认删除({confirm['btnText']})")

    # 校验同题卡数真的减少(最多等 10s)
    for _ in range(10):
        time.sleep(1.0)
        now = page.evaluate(_FIND_CARD_JS, title)
        remain = int(now.get("count", 0))
        if remain < before:
            logger.info(f"[note_delete] ✓ 删除生效: 同题卡 {before} → {remain}")
            return before
    raise NoteDeleteError(
        f"delete_not_effective: 确认后同题卡数未减少(仍 {before} 张)")


def delete_notes_by_title(
    page, account_id: int, title: str, count: int = 1
) -> Dict[str, Any]:
    """按标题删除最多 ``count`` 篇笔记(一次会话逐篇删,省反复起浏览器)。

    Args:
        page: 已建好登录态的同步 Playwright Page(SyncClient.start 之后)。
        account_id: 账号 ID(日志用)。
        title: 笔记标题(精确匹配,容忍卡片截断省略号)。
        count: 最多删除篇数(同题多篇时逐篇删,每篇独立校验)。

    Returns:
        {"deleted": 实际删除数, "remaining": 剩余同题卡数}

    Raises:
        NoteDeleteError: 任一步失败(reason 携语义);已删部分数量在异常前日志可见。
    """
    human = SyncHumanActions(page)
    _open_note_manage(page, human, account_id)
    human.wait(1.0, 2.0, context="笔记管理页浏览")

    deleted = 0
    for i in range(count):
        try:
            before = _delete_first_match(page, human, title)
        except NoteDeleteError as e:
            if deleted > 0 and e.reason.startswith("note_not_found"):
                break  # 已删完所有同题卡,自然收口
            raise
        deleted += 1
        remaining = before - 1
        logger.info(f"[note_delete] 账号{account_id}: 第 {i + 1}/{count} 篇已删,"
                    f"同题剩 {remaining}")
        if remaining <= 0:
            break
        human.wait(1.5, 3.0, context="删除间隔")

    final = page.evaluate(_FIND_CARD_JS, title)
    return {"deleted": deleted, "remaining": int(final.get("count", 0))}
