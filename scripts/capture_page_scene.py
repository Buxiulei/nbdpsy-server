"""在真号上**只读采集**页面场景快照,存成 CI 可回放的夹具。

    用法: .venv/bin/python scripts/capture_page_scene.py quote_modal <account_id> <note_id>

## 为什么需要它

浏览器层的页面结构假设没法离线验证,验证要开真号、真号有风控成本,于是假设一写几个月
没人碰。``_set_quote`` 的"响应第 i 条 ↔ 第 i 张卡"就这么活到被证伪,期间引用功能
100% 失败而 1615 个单测全绿(因为那些测试用的是**手写的**假页面,照着代码的假设写)。

采一份真实快照进仓库,这类 bug 就能在 CI 里红。

## 纪律(与本仓所有真号操作一致)

- **全程拟人化**:``SyncHumanActions``,禁裸 click;
- **只读**:只做打开弹窗、切 tab 这类导航动作,**绝不点「确认引用」「发布」「删除」**;
- 走**编辑已发布笔记**那条路而不是发布页:不用传图,也就不存在误发;
- **必须挑空闲账号**:``account_locks`` 是进程内锁,拦不住 account_worker 子进程;
  跨进程串行全靠 supervisor"每账号只派一个子进程",本脚本在那之外,同时开会因
  ``SyncClient.start`` 的 kill_orphans 互杀。跑之前先确认该号没有在途任务。
- **xsec_token 一律抹掉**:它是会过期的凭据,不该进仓库。
"""

import json
import re
import sys
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

FIXTURE_DIR = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "pages"

# 每个场景声明:要抓哪些选择器 + **只留哪些接口**。
# 接口必须按场景过滤 —— 编辑页加载时会顺带返回活动列表(181 个活动,单独就 280KB+),
# 那与引用弹窗毫无关系,留着只会让夹具大到没人愿意 review。夹具的价值在于**能被看懂**。
_SCENES = {
    "quote_modal": {
        "dom": [
            nc._QUOTE_NOTE_CARD,
            f"{nc._QUOTE_MODAL} button",
            nc._QUOTE_CONTAINER,
        ],
        "api_marks": [nc._POSTED_API_MARK],
    },
}

# 只读抓元素形态的 JS(与本仓其它取证同性质:不点击、不设值、不改 DOM)
_DUMP_JS = r"""
(sel) => [...document.querySelectorAll(sel)].map((el) => {
    const r = el.getBoundingClientRect();
    const attrs = {};
    for (const a of el.attributes) attrs[a.name] = a.value;
    return {
        text: (el.innerText || '').replace(/\s+/g, ' ').trim().slice(0, 200),
        attrs: attrs,
        visible: r.width > 0 && r.height > 0,
        rect: {x: Math.round(r.x), y: Math.round(r.y),
               width: Math.round(r.width), height: Math.round(r.height)},
    };
})
"""

_TOKEN_RE = re.compile(r"(xsec_token=)[^&\"'\s]+")


def _scrub(value):
    """抹掉 xsec_token:会过期的凭据不该进仓库(夹具是长期资产)。"""
    if isinstance(value, str):
        return _TOKEN_RE.sub(r"\1SCRUBBED", value)
    if isinstance(value, dict):
        return {k: ("SCRUBBED" if k in ("xsec_token", "token") else _scrub(v))
                for k, v in value.items()}
    if isinstance(value, list):
        return [_scrub(v) for v in value]
    return value


def capture(scene: str, account_id: int, note_id: str, cookies) -> dict:  # noqa: C901
    api = []

    def on_resp(resp):
        try:
            url = resp.url or ""
        except Exception:  # noqa: BLE001
            return
        if not any(m in url for m in _SCENES[scene]["api_marks"]):
            return
        try:
            body = resp.json()
        except Exception:  # noqa: BLE001 — 读不到就不记,绝不让监听器抛异常
            return
        api.append({"url": url, "status": getattr(resp, "status", 200), "body": body})

    client = SyncClient(account_id, cookies)
    try:
        start = client.start()
        if not start.get("success"):
            raise RuntimeError(f"浏览器启动失败: {start.get('error')}")
        page = client.page
        page.on("response", on_resp)
        human = SyncHumanActions(page)

        nc.open_update_page(page, account_id, note_id)
        human.wait(1.0, 2.0, context="编辑页停留")

        entry = page.query_selector(nc._QUOTE_CONTAINER)
        if entry is None:
            raise RuntimeError("没找到引用笔记入口")
        human.click(entry, reason="打开引用笔记弹窗(只读采集)")
        human.wait(2.0, 3.0, context="等弹窗与候选渲染")

        dom = {sel: page.evaluate(_DUMP_JS, sel) for sel in _SCENES[scene]["dom"]}
        snapshot = {
            "scene": scene,
            "captured_at": datetime.now(timezone.utc).isoformat(),
            "account_id": account_id,
            "source_note_id": note_id,
            "url": page.url,
            "dom": dom,
            "api": api,
            "note": "真号只读采集;xsec_token 已抹除。**不要手改本文件** —— 它是证据,"
                    "改它去迁就代码等于自欺,要更新只能重新采集。",
        }
        # 采完就把弹窗关掉:开着会盖住发布按钮(2026-08-02 事故就是这么来的)
        nc._close_quote_modal(page, human)
        return _scrub(snapshot)
    finally:
        client.stop()


async def main() -> None:
    if len(sys.argv) < 4:
        print(__doc__)
        sys.exit(2)
    scene, account_id, note_id = sys.argv[1], int(sys.argv[2]), sys.argv[3]
    if scene not in _SCENES:
        print(f"未知场景 {scene};已支持: {list(_SCENES)}")
        sys.exit(2)
    cookies = await load_account_cookies(account_id)
    if not cookies:
        print(f"账号 {account_id} 无可用 cookie")
        sys.exit(1)
    async with account_locks.get(account_id):
        async with browser_slot():
            snapshot = await asyncio.to_thread(capture, scene, account_id, note_id, cookies)
    FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
    out = FIXTURE_DIR / f"{scene}.json"
    out.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")
    cards = len(snapshot["dom"].get(nc._QUOTE_NOTE_CARD, []))
    print(f"已写 {out}\n  候选卡 {cards} 张 / 接口响应 {len(snapshot['api'])} 条")


asyncio.run(main())
