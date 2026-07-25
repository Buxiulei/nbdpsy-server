"""创作中心草稿箱清理(浏览器层):删光全部四类 tab 的草稿,全程拟人。

背景(2026-07-25 全号清理实证):XHS 草稿存**浏览器本地**(即各账号 camoufox profile),
服务器无副本;本系统不用草稿功能,所有草稿都是垃圾——历史 draft-only 模式遗留 +
发布编辑器页被自动化打开时自动存的「暂无笔记标题」空草稿(每次发布类操作都可能新增)。

防误删闸(与 note_delete 同款纪律):确认弹窗文案必须含「删除」才点确认,否则 Escape
收场 fail-loud;每删一篇校验 tab 计数递减,不减即停。

会话注意:cookie_status=invalid 的号 creator 子域会话可能仍活(实测 acc5/6),
故不在此层拦登录态——找不到草稿箱入口才算不可达。
"""
import time

from loguru import logger

from app.browser.sync_human_actions import SyncHumanActions


class DraftCleanError(Exception):
    """草稿清理失败(reason 携语义,如 draftbox_not_found)。"""

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


# 草稿箱入口:发布页顶栏「草稿箱(N)」
_ENTRY_JS = r"""() => {
    for (const el of document.querySelectorAll('span,div')) {
        const t = (el.textContent || '').trim();
        const m = t.match(/^草稿箱\((\d+)\)$/);
        if (m) { const r = el.getBoundingClientRect();
            if (r.width > 0) return {n: +m[1], x: r.x + r.width/2, y: r.y + r.height/2}; }
    }
    return null;
}"""

# 四类 tab:「视频笔记(N)/图文笔记(N)/长文笔记(N)/播客笔记(N)」
_TABS_JS = r"""() => {
    const out = [];
    for (const el of document.querySelectorAll('div,span,li')) {
        const t = (el.textContent || '').trim();
        const m = t.match(/^(视频|图文|长文|播客)笔记\((\d+)\)$/);
        if (m) { const r = el.getBoundingClientRect();
            if (r.width > 0) out.push({kind: m[1], n: +m[2], x: r.x + r.width/2, y: r.y + r.height/2}); }
    }
    return out;
}"""

_TAB_COUNT_JS = r"""(kind) => {
    for (const el of document.querySelectorAll('div,span,li')) {
        const m = (el.textContent || '').trim().match(new RegExp('^' + kind + '笔记\\((\\d+)\\)$'));
        if (m) { const r = el.getBoundingClientRect();
            if (r.width > 0) return {n: +m[1], x: r.x + r.width/2, y: r.y + r.height/2}; }
    }
    return null;
}"""

# 当前 tab 下第一张草稿卡(限定列表区域、排除说明文案)
_CARDS_JS = r"""() => {
    const out = [];
    for (const el of document.querySelectorAll('[class*=draft] [class*=item],[class*=note-item],[class*=draft-item]')) {
        const r = el.getBoundingClientRect();
        const t = (el.innerText || '').trim();
        if (r.width > 200 && r.height > 40 && r.top > 120 && t)
            out.push({text: t.slice(0, 50), x: r.x, y: r.y, w: r.width, h: r.height});
    }
    if (out.length) return out;
    for (const el of document.querySelectorAll('div,li')) {
        const r = el.getBoundingClientRect();
        const t = (el.innerText || '').trim();
        if (r.width > 300 && r.height > 50 && r.height < 220 && r.top > 130 && t
            && t.length > 3 && t.length < 120 && el.querySelectorAll('*').length < 40
            && !t.includes('草稿存储') && !t.includes('拖拽')) {
            out.push({text: t.slice(0, 50), x: r.x, y: r.y, w: r.width, h: r.height});
            if (out.length >= 3) break;
        }
    }
    return out;
}"""

# 卡片矩形附近找「删除」操作位(精确文本/title 属性/删除类名图标)
_FIND_DELETE_JS = r"""(card) => {
    const inCard = (r) => r.left >= card.x - 30 && r.right <= card.x + card.w + 60
        && r.top >= card.y - 20 && r.bottom <= card.y + card.h + 30;
    for (const el of document.querySelectorAll('*')) {
        const r = el.getBoundingClientRect();
        if (r.width === 0 || r.height === 0 || !inCard(r)) continue;
        const own = Array.from(el.childNodes).filter(n => n.nodeType === 3)
            .map(n => n.nodeValue.trim()).join('');
        const title = el.getAttribute && (el.getAttribute('title') || el.getAttribute('aria-label')) || '';
        if (own === '删除' || title === '删除')
            return {x: r.x + r.width/2, y: r.y + r.height/2};
    }
    for (const el of document.querySelectorAll('[class*=delete],[class*=Delete],[class*=trash]')) {
        const r = el.getBoundingClientRect();
        if (r.width > 0 && inCard(r)) return {x: r.x + r.width/2, y: r.y + r.height/2};
    }
    return null;
}"""

# 确认弹窗:文案必须含「删除」;确认按钮=确定/确认/删除
_CONFIRM_JS = r"""() => {
    for (const dlg of document.querySelectorAll('[role=dialog],[class*=modal],[class*=Modal],[class*=dialog],[class*=Dialog],[class*=confirm]')) {
        const r = dlg.getBoundingClientRect();
        if (r.width === 0) continue;
        const text = (dlg.innerText || '').trim();
        if (!text) continue;
        for (const b of dlg.querySelectorAll('button,[class*=btn],[class*=Button]')) {
            const bt = (b.innerText || '').trim();
            const br = b.getBoundingClientRect();
            if (br.width > 0 && /^(确定|确认|删除|确认删除)$/.test(bt))
                return {found: true, mentionsDelete: /删除/.test(text),
                        x: br.x + br.width/2, y: br.y + br.height/2, btn: bt,
                        dialogText: text.slice(0, 120)};
        }
        return {found: true, mentionsDelete: /删除/.test(text), noBtn: true,
                dialogText: text.slice(0, 120)};
    }
    return {found: false};
}"""


def _clean_one_tab(page, human, kind: str) -> int:
    """删光某 tab(视频/图文/长文/播客)的草稿,返回删除数;异常纪律同 note_delete。"""
    tab = page.evaluate(_TAB_COUNT_JS, kind)
    if not tab or tab["n"] == 0:
        return 0
    total = tab["n"]
    human.click((tab["x"], tab["y"]), reason=f"{kind}笔记 tab")
    human.wait(1.5, 2.5, context=f"{kind}草稿列表加载")
    deleted = 0
    for _ in range(total + 3):  # 余量防计数漂移
        cur = page.evaluate(_TAB_COUNT_JS, kind)
        if not cur or cur["n"] == 0:
            break
        cards = page.evaluate(_CARDS_JS)
        if not cards:
            logger.warning(f"[draft_cleaner] {kind} tab 计数 {cur['n']} 但找不到卡片,停止本 tab")
            break
        card = cards[0]
        human.hover((card["x"] + card["w"] * 0.5, card["y"] + card["h"] * 0.5),
                    reason="悬停草稿卡")
        human.wait(0.5, 0.9, context="等操作位显出")
        btn = page.evaluate(_FIND_DELETE_JS, card)
        if not btn:
            logger.warning(f"[draft_cleaner] {kind} 悬停后未找到删除按钮,停止本 tab")
            break
        human.click((btn["x"], btn["y"]), reason="草稿删除按钮")
        confirm = None
        for _ in range(8):
            time.sleep(0.5)
            confirm = page.evaluate(_CONFIRM_JS)
            if confirm.get("found"):
                break
        if confirm and confirm.get("found"):
            if not confirm.get("mentionsDelete") or confirm.get("noBtn"):
                human.press_key("Escape", reason="弹窗异常拒点")
                logger.warning(f"[draft_cleaner] 确认弹窗异常拒点: {confirm.get('dialogText','')!r}")
                break
            human.click((confirm["x"], confirm["y"]), reason=f"确认({confirm['btn']})")
        ok = False
        for _ in range(10):
            time.sleep(0.8)
            after = page.evaluate(_TAB_COUNT_JS, kind)
            if after and after["n"] < cur["n"]:
                ok = True
                deleted += 1
                break
        if not ok:
            logger.warning(f"[draft_cleaner] {kind} 确认后计数未减,停止本 tab")
            break
        human.wait(1.0, 2.0, context="删除间隔")
    return deleted


def clean_drafts(page, account_id: int) -> dict:
    """清空该号草稿箱全部四类 tab;返回 {"deleted", "remaining"}。

    Args:
        page: 已 start 的同步 Playwright Page(登录态由 creator 实际可达性判定)。
    Raises:
        DraftCleanError: 草稿箱不可达(draftbox_not_found,常见=creator 会话真死)。
    """
    human = SyncHumanActions(page)
    page.goto("https://creator.xiaohongshu.com/publish/publish?source=official",
              wait_until="domcontentloaded", timeout=40000)
    human.wait(2.0, 3.0, context="发布页加载")
    entry = page.evaluate(_ENTRY_JS)
    if not entry:
        raise DraftCleanError("draftbox_not_found: 未找到草稿箱入口(creator 会话不可达?)")
    if entry["n"] == 0:
        logger.info(f"[draft_cleaner] 账号{account_id} 草稿箱为空,无需清理")
        return {"deleted": 0, "remaining": 0}
    logger.info(f"[draft_cleaner] 账号{account_id} 草稿箱共 {entry['n']} 篇,开始清理")
    human.click((entry["x"], entry["y"]), reason="草稿箱入口")
    human.wait(1.5, 2.5, context="草稿箱加载")

    deleted = 0
    for kind in ("视频", "图文", "长文", "播客"):
        deleted += _clean_one_tab(page, human, kind)
    final = page.evaluate(_ENTRY_JS)
    remaining = final["n"] if final else -1
    logger.info(f"[draft_cleaner] 账号{account_id} 清理完成: 删除 {deleted} 篇,剩余 {remaining}")
    return {"deleted": deleted, "remaining": remaining}
