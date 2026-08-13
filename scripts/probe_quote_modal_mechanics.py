"""引用弹窗**滚动力学 + 选中力学**真号只读取证(不改 app/,不提交任何东西)。

    用法: .venv/bin/python scripts/probe_quote_modal_mechanics.py <account_id> <note_id> [<quoted_note_id>] [--force]

带第三个参数 = **复刻生产失败单**:一直翻到目标出现(或真到底),再按生产同款
(标题唯一命中 → ``_bring_card_into_view`` → 点卡片中心)选它,**只到「确认引用」解禁为止**。

## 要回答的两问(0.24.3 上线后引用仍失败)

- **Q1 滚动力学**:弹窗里真正可滚的容器是谁?滚轮打在候选卡上被谁消费?哪个元素的
  ``scrollTop`` 会动?第 3 页由什么触发(滚动位置 / sentinel 进视口)?
- **Q2 选中力学**:候选卡哪个子元素才是选中热区?点中后 DOM 怎么变(卡片 class /
  新增子元素 / 「确认引用」何时解禁)?

## 纪律(与 capture_page_scene.py 一致)

- **全程拟人化** ``SyncHumanActions``,禁裸 click / 禁 JS 合成事件 / 禁 JS 写 scrollTop;
  注入的 JS 只有**只读求值**与**被动 wheel 监听器**(passive,不改变页面行为)。
- **只读**:开弹窗、滚动、点候选卡;**绝不点「确认引用」,绝不提交**,收尾点「取消」弃掉。
- 起浏览器前查 ``browser_jobs`` / ``publish_jobs`` 在飞任务(跨进程唯一共享真相)。
- xsec_token 一律抹掉。
"""

import json
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import asyncio  # noqa: E402

from app.browser.account_locks import account_locks  # noqa: E402
from app.browser.browser_gate import browser_slot  # noqa: E402
from app.browser import note_components as nc  # noqa: E402
from app.browser.sync_client import SyncClient  # noqa: E402
from app.browser.sync_human_actions import SyncHumanActions  # noqa: E402
from app.services.cookie_check import load_account_cookies  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = ROOT / "data" / "scene_captures"
SHOT_DIR = ROOT / "data" / "debug_screenshots"

SESSION_BUDGET_S = 12 * 60          # 自设 deadline:超了就直奔收尾
_TOKEN_RE = re.compile(r"(xsec_token=)[^&\"'\s]+")

# ─────────────────────────── 只读 JS 探针 ───────────────────────────

# 弹窗内**全部**纵向可滚容器(scrollHeight - clientHeight > 2),外加 document。
_SCROLLERS_JS = r"""
() => {
    const path = (el) => {
        const seg = [];
        let n = el;
        for (let i = 0; i < 4 && n && n.tagName; i++) {
            const cls = (n.className || '').toString().trim().split(/\s+/).filter(Boolean);
            seg.unshift(n.tagName.toLowerCase() + (cls.length ? '.' + cls.join('.') : ''));
            n = n.parentElement;
        }
        return seg.join(' > ');
    };
    const info = (el) => {
        const cs = getComputedStyle(el);
        const r = el.getBoundingClientRect();
        return {
            path: path(el),
            tag: el.tagName,
            cls: (el.className || '').toString().slice(0, 120),
            overflow_y: cs.overflowY,
            scroll_top: Math.round(el.scrollTop),
            scroll_height: Math.round(el.scrollHeight),
            client_height: Math.round(el.clientHeight),
            rect: {x: Math.round(r.x), y: Math.round(r.y),
                   w: Math.round(r.width), h: Math.round(r.height)},
        };
    };
    const modal = document.querySelector('.d-modal.select-note-modal');
    const out = [];
    if (modal) {
        for (const el of [modal, ...modal.querySelectorAll('*')]) {
            if (el.scrollHeight - el.clientHeight > 2) out.push(info(el));
        }
    }
    const de = document.scrollingElement || document.documentElement;
    out.push(Object.assign(info(de), {path: 'document.scrollingElement'}));
    return out;
}
"""

# 从候选网格往上走的祖先链(不管可不可滚都记):真滚动容器必在这条链上。
_GRID_CHAIN_JS = r"""
() => {
    const grid = document.querySelector('.select-note-modal__note-grid');
    if (!grid) return null;
    const chain = [];
    let el = grid;
    for (let i = 0; i < 8 && el && el.tagName; i++) {
        const cs = getComputedStyle(el);
        const r = el.getBoundingClientRect();
        chain.push({
            tag: el.tagName,
            cls: (el.className || '').toString().slice(0, 120),
            overflow_y: cs.overflowY,
            scroll_top: Math.round(el.scrollTop),
            scroll_height: Math.round(el.scrollHeight),
            client_height: Math.round(el.clientHeight),
            scrollable: el.scrollHeight - el.clientHeight > 2,
            rect: {x: Math.round(r.x), y: Math.round(r.y),
                   w: Math.round(r.width), h: Math.round(r.height)},
        });
        el = el.parentElement;
    }
    return chain;
}
"""

# 鼠标真实落点下面到底是谁 + 它的可滚祖先(答"滚轮打在哪儿"的直接证据)。
_POINT_JS = r"""
(pt) => {
    const top = document.elementFromPoint(pt[0], pt[1]);
    if (!top) return {point: pt, hit: null};
    const chain = [];
    let el = top;
    for (let i = 0; i < 8 && el && el.tagName; i++) {
        const cs = getComputedStyle(el);
        chain.push({
            tag: el.tagName,
            cls: (el.className || '').toString().slice(0, 100),
            overflow_y: cs.overflowY,
            scrollable: el.scrollHeight - el.clientHeight > 2,
            scroll_top: Math.round(el.scrollTop),
            scroll_height: Math.round(el.scrollHeight),
            client_height: Math.round(el.clientHeight),
        });
        el = el.parentElement;
    }
    const firstScrollable = chain.find((c) => c.scrollable) || null;
    return {point: pt, hit: chain[0], chain: chain, first_scrollable_ancestor: firstScrollable};
}
"""

# 被动 wheel 监听:capture 与 bubble 各一路。bubble 收不到 = 中途 stopPropagation;
# defaultPrevented = 有人接管了滚动。**passive,不改页面行为**。
_WHEEL_INSTALL_JS = r"""
() => {
    if (window.__probe_wheel) return 'already';
    window.__probe_wheel = [];
    const desc = (t) => (t && t.tagName)
        ? t.tagName + '.' + (t.className || '').toString().slice(0, 60) : String(t);
    const rec = (phase) => (e) => {
        if (window.__probe_wheel.length > 400) return;
        window.__probe_wheel.push({
            phase: phase,
            t: Math.round(performance.now()),
            delta_y: Math.round(e.deltaY),
            default_prevented: e.defaultPrevented,
            target: desc(e.target),
        });
    };
    window.addEventListener('wheel', rec('capture'), {capture: true, passive: true});
    window.addEventListener('wheel', rec('bubble'), {capture: false, passive: true});
    return 'installed';
}
"""

_WHEEL_DRAIN_JS = r"""
() => {
    const w = window.__probe_wheel || [];
    window.__probe_wheel = [];
    const cap = w.filter((x) => x.phase === 'capture');
    const bub = w.filter((x) => x.phase === 'bubble');
    return {
        capture_count: cap.length,
        bubble_count: bub.length,
        prevented_count: w.filter((x) => x.default_prevented).length,
        sample: cap.slice(0, 3).concat(bub.slice(0, 2)),
    };
}
"""

# 懒加载 sentinel 侦查:网格末尾子元素 + 网格之后的兄弟(loading / observer 靶子)。
_SENTINEL_JS = r"""
() => {
    const grid = document.querySelector('.select-note-modal__note-grid');
    if (!grid) return null;
    const brief = (el) => ({
        tag: el.tagName,
        cls: (el.className || '').toString().slice(0, 120),
        text: (el.innerText || '').replace(/\s+/g, ' ').trim().slice(0, 60),
        html: el.outerHTML.slice(0, 200),
    });
    const parent = grid.parentElement;
    return {
        children_total: grid.children.length,
        tail: [...grid.children].slice(-3).map(brief),
        siblings_after_grid: parent
            ? [...parent.children].filter((el) => el !== grid).map(brief) : [],
        parent_cls: parent ? (parent.className || '').toString().slice(0, 120) : null,
    };
}
"""

# 候选卡子树形态(class 全量 + 各子元素矩形):找选中热区用。
_CARD_DUMP_JS = r"""
(el) => {
    const dump = (n) => {
        const r = n.getBoundingClientRect();
        const attrs = {};
        for (const a of n.attributes) attrs[a.name] = a.value.slice(0, 120);
        return {
            tag: n.tagName,
            cls: (n.className || '').toString(),
            attrs: attrs,
            text: (n.innerText || '').replace(/\s+/g, ' ').trim().slice(0, 40),
            rect: {x: Math.round(r.x), y: Math.round(r.y),
                   w: Math.round(r.width), h: Math.round(r.height)},
        };
    };
    return {
        self: dump(el),
        html: el.outerHTML.slice(0, 1800),
        children: [...el.querySelectorAll('*')].map(dump),
    };
}
"""

# 点卡之后平台的**反馈浮层**(真号实测:非公开笔记会弹「非公开可见笔记,无法引用」)。
# 广谱扫:文案短且含「引用」/「公开」的可见元素,连同 message/toast/tip 类容器。
_TOAST_JS = r"""
() => {
    const out = [];
    const push = (el, why) => {
        const r = el.getBoundingClientRect();
        if (r.width <= 0 || r.height <= 0) return;
        const text = (el.innerText || '').replace(/\s+/g, ' ').trim();
        if (!text || text.length > 60) return;
        out.push({
            why: why,
            tag: el.tagName,
            cls: (el.className || '').toString().slice(0, 140),
            text: text,
            rect: {x: Math.round(r.x), y: Math.round(r.y),
                   w: Math.round(r.width), h: Math.round(r.height)},
            html: el.outerHTML.slice(0, 240),
        });
    };
    for (const el of document.querySelectorAll('body *')) {
        const t = (el.innerText || '').replace(/\s+/g, ' ').trim();
        if (t && t.length <= 60 && (t.includes('无法引用') || t.includes('非公开'))) push(el, 'text');
    }
    for (const el of document.querySelectorAll(
            "[class*='message'],[class*='toast'],[class*='notice'],[class*='tip']")) {
        push(el, 'container');
    }
    return out;
}
"""

# 弹窗里全部按钮/tab(找「加载更多」这类分页控件,以及确认钮当前形态)。
_BUTTONS_JS = r"""
() => [...document.querySelectorAll('.d-modal.select-note-modal button, .d-modal.select-note-modal [class*="btn"]')]
    .map((el) => {
        const r = el.getBoundingClientRect();
        return {
            tag: el.tagName,
            cls: (el.className || '').toString().slice(0, 140),
            text: (el.innerText || '').replace(/\s+/g, ' ').trim().slice(0, 30),
            disabled: el.hasAttribute('disabled'),
            rect: {x: Math.round(r.x), y: Math.round(r.y),
                   w: Math.round(r.width), h: Math.round(r.height)},
        };
    })
"""


def _scrub(value):
    """抹掉 xsec_token:会过期的凭据不该落盘。"""
    if isinstance(value, str):
        return _TOKEN_RE.sub(r"\1SCRUBBED", value)
    if isinstance(value, dict):
        return {k: ("SCRUBBED" if k in ("xsec_token", "token") else _scrub(v))
                for k, v in value.items()}
    if isinstance(value, list):
        return [_scrub(v) for v in value]
    return value


def _inflight_jobs(account_id: int) -> list:
    """只读查在飞任务(照抄 capture_page_scene 的跨进程撞车门禁)。"""
    import sqlite3

    from app.services import browser_jobs_repo

    con = sqlite3.connect(f"file:{browser_jobs_repo.current_db_path()}?mode=ro", uri=True)
    try:
        rows = con.execute(
            "SELECT id, kind, status FROM browser_jobs "
            "WHERE account_id = ? AND status IN ('running', 'queued')",
            (account_id,),
        ).fetchall()
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


def probe(account_id: int, note_id: str, cookies, quoted_note_id: str = "") -> dict:  # noqa: C901
    started = time.monotonic()
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    api: list = []
    shots: list = []
    seen_notes: list = []          # 累计候选 (id, 标题),顺序即到达顺序

    def on_resp(resp):
        try:
            url = resp.url or ""
        except Exception:  # noqa: BLE001
            return
        if nc._POSTED_API_MARK not in url:
            return
        try:
            body = resp.json()
        except Exception:  # noqa: BLE001
            return
        notes = ((body or {}).get("data") or {}).get("notes") or []
        known = {n[0] for n in seen_notes}
        for n in notes:
            nid = str((n or {}).get("id") or "").strip()
            if nid and nid not in known:
                known.add(nid)
                seen_notes.append((nid, str((n or {}).get("display_title") or "")))
        api.append({
            "url": url, "ts": round(time.monotonic() - started, 2),
            "count": len(notes),
            "has_more": ((body or {}).get("data") or {}).get("has_more"),
            "first_id": str((notes[0] or {}).get("id") or "") if notes else "",
        })

    def over_budget() -> bool:
        return time.monotonic() - started > SESSION_BUDGET_S

    client = SyncClient(account_id, cookies)
    result: dict = {"scroll_rounds": [], "select_ladder": []}
    try:
        start = client.start()
        if not start.get("success"):
            raise RuntimeError(f"浏览器启动失败: {start.get('error')}")
        page = client.page
        page.on("response", on_resp)
        human = SyncHumanActions(page)

        def shot(name: str) -> None:
            path = SHOT_DIR / f"quote_mech_{ts}_{name}.png"
            try:
                page.screenshot(path=str(path))
                shots.append(str(path))
            except Exception as exc:  # noqa: BLE001 — 截图失败不打断取证
                result.setdefault("warnings", []).append(f"截图 {name} 失败: {exc}")

        def scroll_state() -> dict:
            return {
                "scrollers": page.evaluate(_SCROLLERS_JS),
                "grid_chain": page.evaluate(_GRID_CHAIN_JS),
                "cards": len(page.query_selector_all(nc._QUOTE_NOTE_CARD)),
                "posted_responses": len(api),
                "notes_total": sum(a["count"] for a in api),
            }

        # ── 开编辑页 + 开引用弹窗 ─────────────────────────────
        nc.open_update_page(page, account_id, note_id)
        human.wait(1.0, 1.6, context="编辑页停留")
        entry = page.query_selector(nc._QUOTE_CONTAINER)
        if entry is None:
            raise RuntimeError("没找到引用笔记入口")
        human.click(entry, reason="打开引用笔记弹窗(取证)")
        human.wait(2.0, 3.0, context="等弹窗与首两页候选渲染")
        # 自动分页安静下来再开始量(与生产同口径:连续静默即收工)
        deadline = time.monotonic() + 8
        last, quiet = len(api), time.monotonic()
        while time.monotonic() < deadline:
            if len(api) > last:
                last, quiet = len(api), time.monotonic()
            elif time.monotonic() - quiet >= 1.5:
                break
            page.wait_for_timeout(300)
        page.evaluate(_WHEEL_INSTALL_JS)
        result["baseline"] = scroll_state()
        result["modal_buttons"] = page.evaluate(_BUTTONS_JS)
        result["sentinel"] = page.evaluate(_SENTINEL_JS)
        shot("01_modal_open")

        # ── Q1:滚动仪表 ───────────────────────────────────────
        def one_round(kind: str, target, wait_s=(1.4, 2.0)) -> dict:
            """hover 到 target(元素或坐标)→ 滚一屏 → 立刻读全部容器 scrollTop。"""
            before = scroll_state()
            page.evaluate(_WHEEL_DRAIN_JS)          # 清掉上一轮残留
            human.hover(target, reason=f"移到滚动落点({kind})")
            pos = list(human.last_mouse_pos or (0, 0))
            under = page.evaluate(_POINT_JS, pos)
            human.scroll("down")
            wheel = page.evaluate(_WHEEL_DRAIN_JS)
            after_immediate = scroll_state()
            human.wait(*wait_s, context="等懒加载下一页")
            after = scroll_state()
            moved = {
                s["path"]: [b["scroll_top"], s["scroll_top"]]
                for b, s in zip(before["scrollers"], after["scrollers"])
                if b["path"] == s["path"] and b["scroll_top"] != s["scroll_top"]
            }
            return {
                "kind": kind,
                "t": round(time.monotonic() - started, 1),
                "mouse": pos,
                "under_mouse": under,
                "wheel": wheel,
                "scroll_tops_before": {s["path"]: s["scroll_top"] for s in before["scrollers"]},
                "scroll_tops_after": {s["path"]: s["scroll_top"] for s in after["scrollers"]},
                "moved": moved,
                "cards": [before["cards"], after_immediate["cards"], after["cards"]],
                "posted_responses": [before["posted_responses"], after["posted_responses"]],
                "notes_total": [before["notes_total"], after["notes_total"]],
            }

        # 复刻模式:一直翻到目标 note_id 出现或真到底(每轮给足懒加载时间)
        rounds = 14 if quoted_note_id else 5
        for i in range(rounds):
            if over_budget():
                result.setdefault("warnings", []).append("预算耗尽,提前结束滚动仪表")
                break
            if quoted_note_id and any(n[0] == quoted_note_id for n in seen_notes):
                break
            cards = page.query_selector_all(nc._QUOTE_NOTE_CARD)
            if not cards:
                result.setdefault("warnings", []).append("弹窗里没有候选卡,滚动仪表跳过")
                break
            anchor = nc._pick_scroll_anchor(page, cards)   # 生产同款落点
            result["scroll_rounds"].append(one_round(
                f"prod_anchor_{i + 1}", anchor,
                wait_s=(3.0, 3.4) if quoted_note_id else (1.4, 2.0),
            ))

        if quoted_note_id:
            ids = [n[0] for n in seen_notes]
            result["target"] = {
                "note_id": quoted_note_id,
                "found": quoted_note_id in ids,
                "index": ids.index(quoted_note_id) if quoted_note_id in ids else None,
                "candidates_total": len(seen_notes),
                "title": next((t for i_, t in seen_notes if i_ == quoted_note_id), ""),
            }
            result["candidate_titles"] = [t for _, t in seen_notes]
        prod_moved = any(r["moved"] for r in result["scroll_rounds"])
        result["prod_anchor_moved_something"] = prod_moved
        shot("02_after_prod_scroll")

        # 生产落点零效果 → 试两个替代落点(各 2 轮)
        if not prod_moved and not over_budget():
            grid = page.query_selector(".select-note-modal__note-grid")
            box = grid.bounding_box() if grid is not None else None
            if box:
                center = (box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
                top_inside = (box["x"] + box["width"] / 2, box["y"] + 20)
                for i in range(2):
                    if over_budget():
                        break
                    result["scroll_rounds"].append(one_round(f"grid_center_{i + 1}", center))
                for i in range(2):
                    if over_budget():
                        break
                    result["scroll_rounds"].append(one_round(f"grid_top20_{i + 1}", top_inside))
            else:
                result.setdefault("warnings", []).append("读不出候选网格矩形,替代落点跳过")

        result["sentinel_after_scroll"] = page.evaluate(_SENTINEL_JS)
        result["scrollers_after_scroll"] = page.evaluate(_SCROLLERS_JS)

        # ── Q2:选中仪表 ───────────────────────────────────────
        cards = page.query_selector_all(nc._QUOTE_NOTE_CARD)
        card = cards[0] if cards else None
        if cards and quoted_note_id:
            # 生产同款认卡:平台标题在全部卡片里唯一命中那一张
            title = nc._norm((result.get("target") or {}).get("title") or "")
            hits = [c for c in cards if title and title in nc._norm(c.inner_text())]
            result["target_card_hits"] = {"title": title, "hits": len(hits), "cards": len(cards)}
            card = hits[0] if len(hits) == 1 else None
        if card is not None and not over_budget():
            result["card_before"] = page.evaluate(_CARD_DUMP_JS, card)
            result["confirm_before"] = nc._quote_confirm_state(page)

            def attempt(name: str, target) -> dict:
                in_view = nc._bring_card_into_view(page, human, target)   # 生产同款前置
                box = target.bounding_box() if target is not None else None
                point = [box["x"] + box["width"] / 2, box["y"] + box["height"] / 2] if box else None
                under = page.evaluate(_POINT_JS, point) if point else None
                human.click(target, random_offset=False, reason=f"选中候选卡({name})")
                human.wait(0.8, 1.3, context="等选中态生效")
                toast = page.evaluate(_TOAST_JS)      # 浮层会自己消失,必须最先读
                confirm = nc._quote_confirm_state(page)
                dump = page.evaluate(_CARD_DUMP_JS, card)
                return {
                    "hot_zone": name,
                    "brought_into_view": in_view,
                    "toast": toast,
                    "click_point": point,
                    "under_click_point": under,
                    "confirm_after": confirm,
                    "card_cls_after": dump["self"]["cls"],
                    "card_html_after": dump["html"],
                    "child_classes_after": [c["cls"] for c in dump["children"]],
                    "selected": bool(confirm.get("enabled")),
                }

            ladder = [("card_center", card)]
            cover = card.query_selector("img") or card.query_selector("[class*='cover']")
            if cover is not None:
                ladder.append(("cover_img", cover))
            title_el = card.query_selector("[class*='title']") or card.query_selector("span, p")
            if title_el is not None:
                ladder.append(("title_text", title_el))

            for name, target in ladder:
                if over_budget():
                    result.setdefault("warnings", []).append("预算耗尽,选中阶梯提前结束")
                    break
                try:
                    step = attempt(name, target)
                except Exception as exc:  # noqa: BLE001 — 单级失败不毁整轮取证
                    step = {"hot_zone": name, "error": str(exc)[:200], "selected": False}
                result["select_ladder"].append(step)
                if step.get("selected"):
                    break
            # 全没选上且是复刻模式:换一个**当场重查的**句柄再点一次
            # (验"卡片句柄在滚动/懒加载后失效"这条假设)
            if quoted_note_id and not any(s.get("selected") for s in result["select_ladder"]):
                title = nc._norm((result.get("target") or {}).get("title") or "")
                fresh = [c for c in page.query_selector_all(nc._QUOTE_NOTE_CARD)
                         if title and title in nc._norm(c.inner_text())]
                if len(fresh) == 1 and not over_budget():
                    try:
                        result["select_ladder"].append(attempt("fresh_handle", fresh[0]))
                    except Exception as exc:  # noqa: BLE001
                        result["select_ladder"].append(
                            {"hot_zone": "fresh_handle", "error": str(exc)[:200], "selected": False})
            shot("03_after_select")
            result["modal_buttons_after_select"] = page.evaluate(_BUTTONS_JS)

        result["url"] = page.url
        result["elapsed_s"] = round(time.monotonic() - started, 1)
        result["screenshots"] = shots
        return _scrub({
            "scene": "quote_modal_mechanics",
            "captured_at": datetime.now(timezone.utc).isoformat(),
            "account_id": account_id,
            "source_note_id": note_id,
            "api": api,
            "result": result,
            "note": "真号只读取证:只开弹窗/滚动/点候选卡,绝未点「确认引用」、未提交;"
                    "xsec_token 已抹除。",
        })
    finally:
        try:
            nc._close_quote_modal(page, human)   # 收尾必关:弹窗盖住发布按钮是老事故
        except Exception:  # noqa: BLE001
            pass
        client.stop()


async def main() -> None:
    if len(sys.argv) < 3:
        print(__doc__)
        sys.exit(2)
    account_id, note_id = int(sys.argv[1]), sys.argv[2]
    quoted = sys.argv[3] if len(sys.argv) > 3 and not sys.argv[3].startswith("--") else ""
    cookies = await load_account_cookies(account_id)
    if not cookies:
        print(f"账号 {account_id} 无可用 cookie")
        sys.exit(1)
    if "--force" not in sys.argv:
        blockers = _inflight_jobs(account_id)
        if blockers:
            print(
                f"账号 {account_id} 有在飞浏览器任务,取证会与其互杀会话,拒绝启动:\n  "
                + "\n  ".join(f"{j[0][:8]} {j[1]} ({j[2]})" for j in blockers)
            )
            sys.exit(3)
    async with account_locks.get(account_id):
        async with browser_slot():
            snapshot = await asyncio.to_thread(probe, account_id, note_id, cookies, quoted)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    SHOT_DIR.mkdir(parents=True, exist_ok=True)
    out = OUT_DIR / f"quote_modal_mechanics_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    out.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")
    r = snapshot["result"]
    print(f"已写 {out}")
    print(f"  滚动轮次 {len(r['scroll_rounds'])} / 生产落点有位移: {r.get('prod_anchor_moved_something')}")
    if r.get("target"):
        print(f"  目标: {r['target']}")
        print(f"  认卡: {r.get('target_card_hits')}")
    print(f"  选中阶梯 {[s.get('hot_zone') + ':' + str(s.get('selected')) for s in r['select_ladder']]}")
    print(f"  截图 {r.get('screenshots')}")


asyncio.run(main())
