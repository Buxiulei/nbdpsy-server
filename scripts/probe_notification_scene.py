"""主站通知页(赞和收藏 / 新增关注)+ 自己主页粉丝弹窗的真号**只读**取证。

    用法: .venv/bin/python scripts/probe_notification_scene.py <account_id> [会话预算分钟数]

## 要回答什么

「互动者身份追踪」能不能做,取决于平台到底把什么发给了前端。三问:

1. 「赞和收藏」每条通知能拿到什么字段(互动者 user_id/昵称/头像/red_id?赞还是收藏?
   目标笔记 note_id/标题?时间戳是精确 epoch 还是只有"3 天前"这类相对文案?)
2. 通知列表能翻多深(平台保留窗口),分页机制是什么(cursor?)
3. 「新增关注」通知与自己主页粉丝列表各能拿到什么字段?

这三问都只能问真页面 —— 代码里没有答案,文档里也没有。故本脚本不做任何判断,
只负责把**原始响应体**和**元素形态**原样落盘,结论留给读 dump 的人。

## 与 ``capture_page_scene.py`` 的关系

同源不同命:那个采的是**可回放的 CI 夹具**(进 tests/fixtures,要长期可读),
本脚本采的是**一次性取证 dump**(进 data/scene_captures,不进仓库)。故这里
接口过滤放得很宽(宁可多存)、DOM 选择器广谱撒网(选择器还没定下来,正是要找它)。

## 纪律(与本仓所有真号操作一致)

- **全程拟人化**:``SyncHumanActions``,禁裸 click / 禁 JS 设值;JS 只用于**只读查询**。
- **只读**:只做 goto、切 tab、拟人滚动、开自己主页粉丝弹窗、关弹窗。
  绝不点关注/点赞/进陌生人主页/任何提交类按钮。
- **跨进程门禁**:``account_locks`` 是进程内锁,拦不住 account_worker 子进程;起浏览器前
  必须查 ``browser_jobs`` + ``publish_jobs`` 台账(唯一跨进程共享真相)。本脚本**不提供
  --force**:取证没有紧急到值得赔上一次真发布的程度,撞上就等下一个窗口。
- **xsec_token 一律抹掉**:会过期的凭据不该留在 dump 里。
"""

import json
import re
import sys
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import asyncio  # noqa: E402

from app.browser.account_locks import account_locks  # noqa: E402
from app.browser.browser_gate import browser_slot  # noqa: E402
from app.browser.login_detector import (  # noqa: E402
    PAGE_TEXT_JS,
    classify_wall_text,
    is_wall_url,
)
from app.browser.sync_client import SyncClient  # noqa: E402
from app.browser.sync_human_actions import SyncHumanActions  # noqa: E402
from app.services.cookie_check import load_account_cookies  # noqa: E402

_ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = _ROOT / "data" / "scene_captures"
SHOT_DIR = _ROOT / "data" / "debug_screenshots"

NOTIFICATION_URL = "https://www.xiaohongshu.com/notification"

# 接口过滤**从宽**:通知/关注/粉丝相关的都存。这轮的目的就是发现端点,收窄等看完 dump 再说。
API_MARKS = ("sns/web/v1/you", "follower", "fans", "connections", "/you/")

# 整个浏览器会话预算(秒)。到点无论采到哪一步都收尾落盘 ——
# 真号会话开越久风控面越大,而"采了一半的证据"远好过"超时被杀什么都没有"。
SESSION_BUDGET_S = 15 * 60

# 深滚封顶轮数 / 连续无增长几轮就停。赞和收藏是主问题(要翻到平台保留窗口的底),
# 新增关注只需摸清分页机制与大致深度,给少一些轮数把预算让给前者。
MAX_SCROLL_ROUNDS = 40
FOLLOW_SCROLL_ROUNDS = 8
STALL_ROUNDS = 3

# 广谱 DOM 候选:选择器还没定下来,先撒网再从 dump 里挑。
# 前四个是**首采实测**确定的形态(2026-08-12 账号1):一条通知 = 一个 .interaction-hint
# (文案+相对时间)+ 一个 .user-avatar;整页容器是 .notification-page。
NOTIFY_ITEM_SELECTORS = [
    "[class*='interaction-hint']",
    "[class*='interaction-time']",
    "[class*='user-avatar']",
    "[class*='notification']",
    "[class*='interaction']",
    "[class*='container'] [class*='item']",
    "a[href*='/user/profile/']",
    "[class*='time']",
]

# 滚轮落点候选(按优先级)。**这是首采翻不动页的根因所在**:通知列表滚的是
# **document**(.notification-page 高 1626 > 视口 1266),页面上唯一的 overflow 滚动容器
# 是左侧 132px 宽的导航栏 UL.channel-list —— 按"面积最大的滚动容器"去挑必然挑中它,
# 滚轮全打在导航栏上,赞和收藏一页都没翻动(而新增关注只是"侥幸":导航栏那时已被滚到底,
# 滚轮才顺着 scroll chaining 冒泡到 document)。故落点改为**直接悬到通知行上**。
LIST_HOVER_SELECTORS = [
    "[class*='interaction-hint']",
    "[class*='user-avatar']",
    "[class*='notification-page']",
]
# 弹窗里的列表(粉丝/关注)另有一套容器,单独给一组落点候选
MODAL_HOVER_SELECTORS = [
    "[class*='user-list'] [class*='item']",
    "[class*='modal'] a[href*='/user/profile/']",
    "[class*='modal']",
]
# 挑"滚动容器"时的最小宽度:低于它多半是侧边导航之类的窄条,不是内容列表
MIN_SCROLLER_WIDTH = 400

# 到底标记:出现即认为翻到头(仍记进 timeline 供复核,不当唯一判据)
END_MARKERS = ("没有更多", "THE END", "已经到底", "暂无更多", "没有更多了", "- THE END -")


# ── 只读 JS(全部是查询,不点击/不设值/不改 DOM)──────────────────────────────

_DUMP_JS = r"""
([sel, limit]) => [...document.querySelectorAll(sel)].slice(0, limit).map((el) => {
    const r = el.getBoundingClientRect();
    const attrs = {};
    for (const a of el.attributes) attrs[a.name] = a.value;
    return {
        tag: el.tagName,
        text: (el.innerText || '').replace(/\s+/g, ' ').trim().slice(0, 200),
        attrs: attrs,
        visible: r.width > 0 && r.height > 0,
        rect: {x: Math.round(r.x), y: Math.round(r.y),
               width: Math.round(r.width), height: Math.round(r.height)},
    };
})
"""

# 找真正的滚动容器:``mouse.wheel`` 把事件投在**鼠标当前位置**,鼠标没动过时停在 (0,0),
# 那里通常是不滚动的顶栏 —— 滚轮全部空转,"翻两页就停"多半是这么来的(创作中心实测)。
# 先把容器矩形查出来,再 hover 到它上面,滚轮才落得进去。
_SCROLLERS_JS = r"""
() => {
    const out = [];
    const de = document.scrollingElement || document.documentElement;
    out.push({sel: '__document__', scrollHeight: de.scrollHeight, clientHeight: de.clientHeight,
              scrollTop: de.scrollTop, cls: '', rect: null});
    for (const el of document.querySelectorAll('body *')) {
        const st = getComputedStyle(el);
        if (!['auto', 'scroll', 'overlay'].includes(st.overflowY)) continue;
        if (el.scrollHeight <= el.clientHeight + 40) continue;
        const r = el.getBoundingClientRect();
        if (r.width < 100 || r.height < 100) continue;
        out.push({
            sel: el.tagName + '.' + (el.className || '').toString().trim().replace(/\s+/g, '.').slice(0, 60),
            cls: (el.className || '').toString().slice(0, 100),
            scrollHeight: el.scrollHeight, clientHeight: el.clientHeight, scrollTop: el.scrollTop,
            rect: {x: Math.round(r.x), y: Math.round(r.y),
                   width: Math.round(r.width), height: Math.round(r.height)},
        });
    }
    return out;
}
"""

_COUNTS_JS = r"""
(sels) => {
    const o = {};
    for (const s of sels) { try { o[s] = document.querySelectorAll(s).length; } catch (e) { o[s] = -1; } }
    const de = document.scrollingElement || document.documentElement;
    o.__doc_scrollTop = Math.round(de.scrollTop);
    o.__doc_scrollHeight = Math.round(de.scrollHeight);
    o.__body_text_len = (document.body.innerText || '').length;
    return o;
}
"""

_END_MARK_JS = r"""
(marks) => {
    const t = document.body.innerText || '';
    return marks.filter((m) => t.includes(m));
}
"""

_EDGE_TEXT_JS = r"""
([sel, n]) => {
    const els = [...document.querySelectorAll(sel)];
    if (!els.length) return null;
    const pick = (el) => (el.innerText || '').replace(/\s+/g, ' ').trim().slice(0, 300);
    return {count: els.length, first: pick(els[0]),
            last_n: els.slice(-n).map(pick)};
}
"""

# 取"最内层"的可见候选:堆叠浮层里点容器中点会命中错误子元素(本仓一天踩过三次),
# 故按面积升序取最小的那个,把点击收口到真正承载文案的元素上。
# 两轮匹配:先全等,再"包含且没长多少"(tab 上常挂未读数徽标,"赞和收藏 3" 全等会落空)。
_INNERMOST_JS = r"""
(texts) => {
    const vis = [...document.querySelectorAll('body *')].filter((el) => {
        const r = el.getBoundingClientRect();
        return r.width > 0 && r.height > 0;
    });
    const txt = (el) => (el.innerText || '').replace(/\s+/g, ' ').trim();
    const area = (el) => { const r = el.getBoundingClientRect(); return r.width * r.height; };
    let hits = vis.filter((el) => texts.includes(txt(el)));
    if (!hits.length) {
        hits = vis.filter((el) => {
            const t = txt(el);
            return texts.some((w) => t.includes(w) && t.length <= w.length + 6);
        });
    }
    if (!hits.length) return null;
    hits.sort((a, b) => area(a) - area(b));
    return hits[0];
}
"""

# 粉丝计数**整块**(形如 "93 粉丝"):首采只点了 "粉丝" 那个文字标签,没弹窗;
# 到底是"点错了元素"还是"web 端这个计数根本不可点",得把整块也点一次才分得清。
_FANS_BLOCK_JS = r"""
() => {
    const hits = [...document.querySelectorAll('body *')].filter((el) => {
        const t = (el.innerText || '').replace(/\s+/g, '').trim();
        if (!/^[\d.,万kK+]+粉丝$/.test(t)) return false;
        const r = el.getBoundingClientRect();
        return r.width > 0 && r.height > 0;
    });
    if (!hits.length) return null;
    hits.sort((a, b) => {
        const ra = a.getBoundingClientRect(), rb = b.getBoundingClientRect();
        return (ra.width * ra.height) - (rb.width * rb.height);
    });
    return hits[0];
}
"""

# 弹窗出现没有?只看**可见**的浮层容器,顺带把文案带回来当取证。
_MODAL_PRESENT_JS = r"""
() => [...document.querySelectorAll(
        "[class*='modal'],[class*='dialog'],[class*='popup'],[class*='user-list']")]
    .filter((el) => { const r = el.getBoundingClientRect(); return r.width > 100 && r.height > 100; })
    .map((el) => ({cls: (el.className || '').toString().slice(0, 90),
                   text: (el.innerText || '').replace(/\s+/g, ' ').trim().slice(0, 120)}))
"""

_LOGIN_JS = r"""
() => ({
    url: location.href,
    has_login_modal: !!document.querySelector(
        ".login-container, .login-modal, [class*='login-box'], [class*='qrcode']"),
    has_avatar: !!document.querySelector(
        ".user .avatar, [class*='side-bar'] img, .reds-avatar, img[class*='avatar']"),
    body_head: (document.body.innerText || '').replace(/\s+/g, ' ').trim().slice(0, 300),
})
"""

_TOKEN_RE = re.compile(r"(xsec_token=)[^&\"'\s]+")


def _scrub(value):
    """抹掉 xsec_token:会过期的凭据不该留在 dump 里。"""
    if isinstance(value, str):
        return _TOKEN_RE.sub(r"\1SCRUBBED", value)
    if isinstance(value, dict):
        return {k: ("SCRUBBED" if k in ("xsec_token", "token") else _scrub(v))
                for k, v in value.items()}
    if isinstance(value, list):
        return [_scrub(v) for v in value]
    return value


def _inflight_jobs(account_id: int) -> list:
    """只读查 browser_jobs:该账号 running/queued 的行(跨进程唯一共享真相)。

    照抄 ``capture_page_scene.py`` —— 这道门禁的判据必须与它逐字一致,两个探针各写一套
    迟早对不上口径。
    """
    import sqlite3

    from app.services import browser_jobs_repo

    con = sqlite3.connect(
        f"file:{browser_jobs_repo.current_db_path()}?mode=ro", uri=True
    )
    try:
        rows = con.execute(
            "SELECT id, kind, status FROM browser_jobs "
            "WHERE account_id = ? AND status IN ('running', 'queued')",
            (account_id,),
        ).fetchall()
        # 发布任务在独立台账:publishing 是全系统代价最高的撞车对象(图都传完了被杀还烧
        # 配额);pending 且已到期/15 分钟内到期 = 随时开跑,同样拦。远期定时的不算在飞。
        rows += [
            (str(r[0]), f"publish:{r[1]}", r[2])
            for r in con.execute(
                "SELECT id, title, status FROM publish_jobs "
                "WHERE account_id = ? AND (status = 'publishing' OR "
                "(status = 'pending' AND (schedule_time IS NULL "
                "OR schedule_time <= datetime('now', '+15 minutes'))))",
                (account_id,),
            ).fetchall()
        ]
        return rows
    finally:
        con.close()


def _account_user_id(account_id: int) -> str:
    """只读取本账号的 user_id(开自己主页用)。取不到返回空串。"""
    import sqlite3

    from app.services import browser_jobs_repo

    con = sqlite3.connect(
        f"file:{browser_jobs_repo.current_db_path()}?mode=ro", uri=True
    )
    try:
        row = con.execute(
            "SELECT user_id FROM xhs_accounts WHERE id = ?", (account_id,)
        ).fetchone()
        return (row[0] or "").strip() if row else ""
    finally:
        con.close()


# ── 页面小工具 ────────────────────────────────────────────────────────────────


def _shot(page, snap: dict, name: str) -> None:
    """存一张截图并把路径记进 snapshot;失败只记原因,绝不打断采集。"""
    path = SHOT_DIR / f"notification_probe_{name}_{int(time.time())}.png"
    try:
        SHOT_DIR.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(path))
        snap.setdefault("screenshots", []).append(str(path))
    except Exception as exc:  # noqa: BLE001
        snap.setdefault("screenshot_errors", []).append(f"{name}: {exc}")


def _innermost(page, texts: list):
    """返回文案完全等于 texts 之一的**最内层可见**元素(找不到返回 None)。"""
    try:
        return page.evaluate_handle(_INNERMOST_JS, texts).as_element()
    except Exception:  # noqa: BLE001
        return None


def _clamp_to_viewport(page, x: float, y: float) -> tuple:
    """把坐标夹进视口(留 10% 边距);读不到视口尺寸按 1280x800 兜底。

    列表矩形常常是**文档坐标**(y 可能上千),直接把鼠标"移"到视口外不是真的悬停,
    滚轮落点也就无从谈起。
    """
    try:
        size = page.viewport_size or {}
    except Exception:  # noqa: BLE001
        size = {}
    w = float(size.get("width") or 1280)
    h = float(size.get("height") or 800)
    return (min(max(x, w * 0.1), w * 0.9), min(max(y, h * 0.1), h * 0.9))


def _hover_list(page, human: SyncHumanActions, snap: dict, tag: str,
                hover_selectors=None) -> dict:
    """把鼠标悬到**列表内容**上(滚轮落点),返回本次落点信息。

    落点优先取 ``hover_selectors`` 里第一个命中的真实条目 —— 通知列表滚的是 document,
    悬到条目上,滚轮既能滚 document 也能滚它所在的任何内层容器,两种形态都覆盖。
    退路才是"够宽的 overflow 滚动容器";**窄于 ``MIN_SCROLLER_WIDTH`` 的一律不选**,
    那是侧边导航栏(首采就是栽在把 132px 宽的 UL.channel-list 当成了通知列表)。
    """
    info = {"tag": tag, "picked": None, "hovered": None, "scrollers": []}
    try:
        info["scrollers"] = page.evaluate(_SCROLLERS_JS) or []
    except Exception as exc:  # noqa: BLE001
        info["error"] = f"扫滚动容器失败: {exc}"

    box = None
    for sel in (hover_selectors or LIST_HOVER_SELECTORS):
        try:
            el = page.query_selector(sel)
            b = el.bounding_box() if el is not None else None
        except Exception:  # noqa: BLE001
            b = None
        if b and b["width"] >= 200 and b["height"] > 10:
            info["picked"] = {"sel": sel, "rect": b, "by": "item"}
            box = b
            break

    if box is None:
        wide = [s for s in info["scrollers"]
                if s.get("sel") != "__document__" and s.get("rect")
                and s["rect"]["width"] >= MIN_SCROLLER_WIDTH]
        wide.sort(key=lambda s: s["rect"]["width"] * s["rect"]["height"], reverse=True)
        if wide:
            info["picked"] = dict(wide[0], by="scroller")
            box = wide[0]["rect"]

    if not box:
        info["hovered"] = False
        snap.setdefault("warnings", []).append(
            f"{tag}: 找不到滚动容器也找不到通知条目,滚轮可能打在不滚动的顶栏上"
        )
        return info

    x, y = _clamp_to_viewport(page, box["x"] + box["width"] * 0.5,
                              box["y"] + box["height"] * 0.5)
    human.hover((x, y), reason=f"移到{tag}滚动区(滚轮落点)")
    info["hovered"] = [round(x), round(y)]
    return info


def _primary_item_selector(page) -> str:
    """挑一个"条目"选择器当进度/边缘文本的观测口径:命中数最多且 ≤200 的那个。

    只是**观测口径**,不参与任何判定 —— 真正的字段结论从 api 原始响应体里读。
    """
    best, best_n = NOTIFY_ITEM_SELECTORS[0], -1
    try:
        counts = page.evaluate(_COUNTS_JS, NOTIFY_ITEM_SELECTORS) or {}
    except Exception:  # noqa: BLE001
        return best
    for sel in NOTIFY_ITEM_SELECTORS:
        n = counts.get(sel, -1)
        if isinstance(n, int) and best_n < n <= 200:
            best, best_n = sel, n
    return best


def _deep_scroll(page, human: SyncHumanActions, snap: dict, api: list,
                 tag: str, max_rounds: int, deadline: float) -> dict:
    """拟人深滚采集;返回 ``{timeline, stopped_by, rounds}``。全程只滚不点。"""
    timeline = []
    item_sel = _primary_item_selector(page)
    stalled = 0
    prev_key = None
    stopped_by = "max_rounds"

    for rnd in range(1, max_rounds + 1):
        if time.monotonic() > deadline:
            stopped_by = "budget"
            break
        human.scroll("down")
        human.wait(0.8, 1.6, context=f"{tag}等新一屏渲染")
        try:
            counts = page.evaluate(_COUNTS_JS, NOTIFY_ITEM_SELECTORS) or {}
        except Exception as exc:  # noqa: BLE001
            counts = {"__error": str(exc)}
        try:
            ends = page.evaluate(_END_MARK_JS, list(END_MARKERS)) or []
        except Exception:  # noqa: BLE001
            ends = []
        row = {"round": rnd, "counts": counts, "api_responses": len(api),
               "end_markers": ends}
        # 每 5 轮记一次末尾条目文本(拿相对时间文案 —— 深滚到底后最老那条长什么样)
        if rnd % 5 == 0 or rnd == 1:
            try:
                row["edge_text"] = page.evaluate(_EDGE_TEXT_JS, [item_sel, 3])
            except Exception as exc:  # noqa: BLE001
                row["edge_text"] = {"error": str(exc)}
        timeline.append(row)

        if ends:
            stopped_by = f"end_marker:{ends}"
            break
        # 进度判据:条目数 + 接口响应数 + 文档高度/文本量 + **滚动位移**,任一动了就算有进展。
        # ``__doc_scrollTop`` 必须在里面:首采漏了它,于是"滚轮还在往下推、只是懒加载还没触发"
        # 的几轮被判成停滞,4 轮就收工,而接口自报 has_more=true —— 把"没翻到底"误报成了到底。
        key = (counts.get(item_sel), len(api), counts.get("__doc_scrollHeight"),
               counts.get("__body_text_len"), counts.get("__doc_scrollTop"))
        stalled = stalled + 1 if key == prev_key else 0
        prev_key = key
        if stalled >= STALL_ROUNDS:
            stopped_by = f"stalled_{STALL_ROUNDS}_rounds"
            break

    return {"timeline": timeline, "stopped_by": stopped_by,
            "rounds": len(timeline), "item_selector": item_sel}


def _dump_dom(page, limit: int = 12) -> dict:
    """广谱 DOM dump(每个选择器最多 limit 个,防 dump 撑爆)。"""
    out = {}
    for sel in NOTIFY_ITEM_SELECTORS:
        try:
            out[sel] = page.evaluate(_DUMP_JS, [sel, limit])
        except Exception as exc:  # noqa: BLE001
            out[sel] = {"error": str(exc)}
    return out


def _wall_check(page, snap: dict, tag: str) -> bool:
    """撞验证码/风控墙?撞了就记取证 + 截图并返回 True(调用方立即收尾)。"""
    try:
        url = page.url or ""
    except Exception:  # noqa: BLE001
        return False
    if not is_wall_url(url) and "captcha" not in url.lower():
        return False
    try:
        text = page.evaluate(PAGE_TEXT_JS) or ""
    except Exception:  # noqa: BLE001
        text = ""
    snap["wall"] = {"at": tag, "landed_url": url,
                    "wall_type": classify_wall_text(text), "page_text": text[:500]}
    _shot(page, snap, f"wall_{tag}")
    return True


# ── 采集主流程 ────────────────────────────────────────────────────────────────


def capture(account_id: int, cookies, budget_s: float = SESSION_BUDGET_S) -> dict:  # noqa: C901
    deadline = time.monotonic() + budget_s
    api: list = []
    snap: dict = {
        "probe": "notification_scene",
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "account_id": account_id,
        "tabs": {},
        "note": "真号只读取证 dump;xsec_token 已抹除。**取证不是夹具**:"
                "不要手改本文件,也不要把它塞进 tests/fixtures。",
    }

    def on_resp(resp):
        try:
            url = resp.url or ""
        except Exception:  # noqa: BLE001
            return
        if not any(m in url for m in API_MARKS):
            return
        try:
            body = resp.json()
        except Exception:  # noqa: BLE001 — 读不到就不记,绝不让监听器抛异常
            return
        api.append({"url": url, "status": getattr(resp, "status", 200),
                    "ts": round(time.monotonic(), 3), "body": body})

    client = SyncClient(account_id, cookies)
    try:
        start = client.start()
        snap["browser_start"] = {"success": start.get("success"),
                                 "logged_in": start.get("logged_in"),
                                 "error": start.get("error")}
        if not start.get("success"):
            snap["fatal"] = f"浏览器启动失败: {start.get('error')}"
            return snap
        page = client.page
        page.on("response", on_resp)
        human = SyncHumanActions(page)

        # ── 1. 进通知页 + 验登录态 ──────────────────────────────────────────
        human.navigate(NOTIFICATION_URL)
        human.wait(1.5, 2.5, context="等通知页渲染")
        snap["notification_url_landed"] = page.url
        if _wall_check(page, snap, "notification"):
            snap["fatal"] = "撞风控墙/验证码,立即收尾"
            return snap
        login = page.evaluate(_LOGIN_JS)
        snap["login_probe"] = login
        _shot(page, snap, "01_notification_landing")
        if login.get("has_login_modal") or "/login" in (login.get("url") or ""):
            snap["fatal"] = "未登录(弹登录框或被重定向到登录页)"
            snap["exit_code"] = 4
            return snap

        # ── 2. 找 tab ────────────────────────────────────────────────────────
        snap["tab_candidates"] = []
        try:
            snap["tab_candidates"] = page.evaluate(_DUMP_JS, ["[class*='tab']", 40])
        except Exception as exc:  # noqa: BLE001
            snap["tab_candidates"] = {"error": str(exc)}

        # ── 3+4+5. 「赞和收藏」:切 tab → 深滚 → DOM dump ────────────────────
        for tab_name, texts, rounds, shot in (
            ("赞和收藏", ["赞和收藏", "赞与收藏"], MAX_SCROLL_ROUNDS, True),
            ("新增关注", ["新增关注", "新增粉丝"], FOLLOW_SCROLL_ROUNDS, False),
        ):
            if time.monotonic() > deadline:
                snap.setdefault("warnings", []).append(f"预算耗尽,跳过 tab「{tab_name}」")
                break
            tab: dict = {"name": tab_name}
            snap["tabs"][tab_name] = tab
            api_before = len(api)
            el = _innermost(page, texts)
            if el is None:
                tab["error"] = "找不到该 tab(文案不匹配),跳过"
                continue
            try:
                human.click(el, reason=f"切到「{tab_name}」tab")
            except Exception as exc:  # noqa: BLE001
                tab["error"] = f"点 tab 失败: {exc}"
                continue
            human.wait(1.5, 2.5, context=f"等「{tab_name}」首屏渲染")
            tab["url_after_click"] = page.url
            if _wall_check(page, snap, tab_name):
                snap["fatal"] = "撞风控墙/验证码,立即收尾"
                return snap
            if shot:
                _shot(page, snap, "02_likes_first_screen")
            tab["first_screen_dom"] = _dump_dom(page)
            try:
                tab["first_screen_edge"] = page.evaluate(
                    _EDGE_TEXT_JS, [_primary_item_selector(page), 3])
            except Exception as exc:  # noqa: BLE001
                tab["first_screen_edge"] = {"error": str(exc)}

            tab["hover_target"] = _hover_list(page, human, snap, tab_name)
            tab["scroll"] = _deep_scroll(page, human, snap, api, tab_name, rounds, deadline)
            tab["api_responses_gained"] = len(api) - api_before
            tab["final_url"] = page.url
            try:
                tab["last_screen_edge"] = page.evaluate(
                    _EDGE_TEXT_JS, [tab["scroll"]["item_selector"], 5])
            except Exception as exc:  # noqa: BLE001
                tab["last_screen_edge"] = {"error": str(exc)}
            tab["last_screen_dom"] = _dump_dom(page, limit=6)
            if shot:
                _shot(page, snap, "03_likes_deep_scroll_bottom")

        # ── 7. 自己主页 → 粉丝弹窗 ──────────────────────────────────────────
        fans: dict = {}
        snap["fans_modal"] = fans
        user_id = _account_user_id(account_id)
        fans["user_id"] = user_id
        if not user_id:
            fans["error"] = "库里没有本号 user_id,跳过粉丝弹窗"
        elif time.monotonic() > deadline:
            fans["error"] = "预算耗尽,跳过粉丝弹窗"
        else:
            api_before = len(api)
            human.navigate(f"https://www.xiaohongshu.com/user/profile/{user_id}")
            human.wait(1.5, 2.5, context="等自己主页渲染")
            fans["profile_url"] = page.url
            if _wall_check(page, snap, "own_profile"):
                snap["fatal"] = "自己主页撞风控墙,立即收尾"
                return snap
            fans["profile_dom"] = _dump_dom(page, limit=8)
            try:
                # 阶梯点击:先点「粉丝」文字标签,没弹窗再点整块("93 粉丝")。
                # 两个落点都点不出弹窗,才有资格说"web 端这个计数不可点" ——
                # 一次点击落空就下结论,分不清是点错了元素还是平台真没这功能。
                ladder = []
                fans["ladder"] = ladder
                opened = False
                for rung, getter, desc in (
                    ("label", lambda: _innermost(page, ["粉丝"]), "「粉丝」文字标签"),
                    ("block", lambda: page.evaluate_handle(_FANS_BLOCK_JS).as_element(),
                     "粉丝计数整块(形如「93 粉丝」)"),
                ):
                    if opened:
                        break
                    try:
                        el = getter()
                    except Exception as exc:  # noqa: BLE001
                        ladder.append({"rung": rung, "error": f"定位失败: {exc}"})
                        continue
                    if el is None:
                        ladder.append({"rung": rung, "error": f"没找到{desc}"})
                        continue
                    human.click(el, reason=f"点{desc}(只读:期望弹出粉丝列表)")
                    human.wait(1.5, 2.5, context="等粉丝弹窗渲染")
                    modals = page.evaluate(_MODAL_PRESENT_JS) or []
                    ladder.append({"rung": rung, "target": desc,
                                   "visible_modals": modals, "url": page.url})
                    opened = bool(modals)

                fans["modal_opened"] = opened
                _shot(page, snap, "04_fans_modal")
                if opened:
                    fans["modal_dom"] = {
                        sel: page.evaluate(_DUMP_JS, [sel, 10])
                        for sel in ("[class*='modal']", "[class*='user-list']",
                                    "[class*='follow']", "a[href*='/user/profile/']")
                    }
                    fans["hover_target"] = _hover_list(
                        page, human, snap, "粉丝弹窗", MODAL_HOVER_SELECTORS)
                    fans["scroll"] = _deep_scroll(
                        page, human, snap, api, "粉丝弹窗", 3, deadline)
                else:
                    fans["conclusion"] = (
                        "两个落点(文字标签 + 计数整块)都点不出任何浮层 —— "
                        "web 端自己主页的粉丝计数疑似**不可点、没有粉丝列表弹窗**"
                    )
                fans["api_responses_gained"] = len(api) - api_before
            # 粉丝弹窗是三问里最独立的一问:它失败不该把前两问已采的证据判成 fatal
            except Exception as exc:  # noqa: BLE001
                fans["error"] = f"{type(exc).__name__}: {exc}"
                fans["traceback"] = traceback.format_exc()
            finally:
                # 弹窗必须关掉(开着的浮层会盖住页面 —— 本仓 2026-08-02 事故同源)。
                # 没弹出来时按 Escape 也无害,故不分支。
                try:
                    human.press_key("Escape", reason="关粉丝弹窗")
                    human.wait(0.5, 1.0, context="等弹窗关闭")
                    fans["closed_url"] = page.url
                except Exception as exc:  # noqa: BLE001
                    fans["close_error"] = str(exc)

        snap["final_url"] = page.url
        return snap

    except Exception as exc:  # noqa: BLE001 — 采到哪算哪,已采数据必须落盘
        snap["fatal"] = f"{type(exc).__name__}: {exc}"
        snap["traceback"] = traceback.format_exc()
        return snap
    finally:
        snap["api"] = api
        snap["api_count"] = len(api)
        snap["elapsed_s"] = round(budget_s - (deadline - time.monotonic()), 1)
        snap["budget_s"] = budget_s
        client.stop()


async def main() -> None:
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(2)
    account_id = int(sys.argv[1])
    # 会话预算可覆盖:调度窗口比默认 15 分钟窄时(见文件头「防撞时序」)必须调小,
    # 宁可少滚几轮,也不能让会话压到下一次 worker 扫描上去。
    budget_s = float(sys.argv[2]) * 60 if len(sys.argv) > 2 else SESSION_BUDGET_S

    cookies = await load_account_cookies(account_id)
    if not cookies:
        print(f"账号 {account_id} 无可用 cookie")
        sys.exit(1)

    # 跨进程撞车门禁(本仓已有三起探针与 worker 子进程互杀事故:SyncClient.start 的
    # kill_orphans 会把对方的 camoufox 杀掉)。account_locks 是**进程内**单例,拦不住别的
    # 进程 —— 唯一共享真相是 browser_jobs / publish_jobs 台账,起浏览器前必须查它。
    # 本脚本刻意不提供 --force:取证等得起,一次被杀掉的真发布等不起。
    blockers = _inflight_jobs(account_id)
    if blockers:
        print(
            f"账号 {account_id} 有在飞浏览器任务,采集会与其互杀会话,拒绝启动:\n  "
            + "\n  ".join(f"{j_id[:8]} {kind} ({status})" for j_id, kind, status in blockers)
            + "\n等它们终态后再跑。"
        )
        sys.exit(3)

    async with account_locks.get(account_id):
        async with browser_slot():
            snapshot = await asyncio.to_thread(capture, account_id, cookies, budget_s)

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out = OUT_DIR / f"notification_probe_account_{account_id}_{stamp}.json"
    out.write_text(json.dumps(_scrub(snapshot), ensure_ascii=False, indent=2),
                   encoding="utf-8")
    print(f"已写 {out}")
    print(f"  接口响应 {snapshot.get('api_count')} 条 | 耗时 {snapshot.get('elapsed_s')}s")
    for name, tab in (snapshot.get("tabs") or {}).items():
        sc = tab.get("scroll") or {}
        print(f"  tab「{name}」: 滚 {sc.get('rounds')} 轮 停因={sc.get('stopped_by')} "
              f"新增接口 {tab.get('api_responses_gained')} 条 {tab.get('error') or ''}")
    for path in snapshot.get("screenshots") or []:
        print(f"  截图 {path}")
    if snapshot.get("fatal"):
        print(f"  ⚠ 中止原因: {snapshot['fatal']}")
        sys.exit(int(snapshot.get("exit_code") or 5))


asyncio.run(main())
