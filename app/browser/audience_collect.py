"""主站通知页受众事件采集(纯同步,吃已登录 page):UI 驱动 + 被动监听接口响应。

设计 docs/design/2026-08-12-audience-behavior-library-design.md 第 3.2 节。
落库与调度在 ``app/services/audience_sync.py``,本模块只管"把页面上的事件读出来"。

## 为什么是 UI 驱动被动监听,而不是直调接口

``/you/likes`` 与 ``/you/connections`` 都在 ``edith.xiaohongshu.com``,不带页面内 JS 现算的
``x-s``/``x-t`` 签名头一律拒绝。要直调就得逆向签名 —— 那是脆弱(平台改一次算法全线失效)
且高风险(明确的自动化特征)的路,与本仓拟人化红线正面冲突。所以走真实页面:让页面自己
去拉,我们只在 ``page.on("response")`` 上被动截它的响应体。**页面看不到的东西我们不要。**

## 两个滚动坑(取证血泪,都在下面防死了)

1. **滚之前必须先 hover 到通知行**:``mouse.wheel`` 把事件投在**鼠标当前位置**,鼠标没动
   过时停在 (0,0),那里是不滚动的顶栏。取证首采就栽在这 —— 更坑的是"按面积最大的
   overflow 容器挑落点"也不对:通知页唯一的 overflow 滚动容器是左侧 132px 宽的导航栏,
   滚轮全打在导航栏上,赞和收藏一页都没翻动。落点必须是**通知行本身**。
2. **停滞判据必须含 ``document.scrollTop``**:通知页滚的是 document(``.notification-page``
   高 1626 > 视口 1266)。只看"条目数/响应数有没有涨"的话,"滚轮还在往下推、只是懒加载
   还没触发"的那几轮会被判成到底,而接口自报 ``has_more=true`` —— 把没翻到底误报成到底,
   增量库从此永远缺一段。

## 只读纪律(合规)

全程只做:导航、切 tab、hover、滚动。**没有**任何点赞/关注/进陌生人主页/提交类点击。
JS 只用于**只读查询**(读 scrollTop / 找 tab 元素),不设值、不改 DOM、不合成事件。
"""

import time
from typing import Any

from loguru import logger

from app.browser.login_detector import PAGE_TEXT_JS, classify_wall_text, is_wall_url

NOTIFICATION_URL = "https://www.xiaohongshu.com/notification"

CHANNEL_LIKES = "likes"
CHANNEL_CONNECTIONS = "connections"
CHANNELS = (CHANNEL_LIKES, CHANNEL_CONNECTIONS)

LIKES_MARK = "/api/sns/web/v1/you/likes"
CONNECTIONS_MARK = "/api/sns/web/v1/you/connections"

# 每条 channel 的 tab 文案(平台改文案时先在这里加候选)与接口特征串。
# ⚠️ 「评论和@」(``/you/mentions``)**刻意不采**:评论体系另账,见设计 §10。
CHANNEL_SPEC: dict[str, dict] = {
    CHANNEL_LIKES: {
        "tab_texts": ["赞和收藏", "赞与收藏"],
        "api_mark": LIKES_MARK,
        "label": "赞和收藏",
    },
    CHANNEL_CONNECTIONS: {
        "tab_texts": ["新增关注", "新增粉丝"],
        "api_mark": CONNECTIONS_MARK,
        "label": "新增关注",
    },
}

# 滚动轮数封顶。全量要翻到平台保留窗口的底(号1 实采 47 页 / 40 轮到底);
# 增量只需摸到已知区,给 5 轮 —— 一小时里新增几百条互动的号不存在,给多了只是给
# "增量退化成全量"留后门,而多滚一轮就是多一分风控暴露。
FULL_SCROLL_ROUNDS = 40
INCREMENTAL_SCROLL_ROUNDS = 5
# 连续几轮**四个进度指标全不动**才判停滞(见模块 docstring 坑②)
STALL_ROUNDS = 3

# 只读页面状态查询:滚动位移 + 文档高度 + 文本量 + 登录框。
# 这四个数合起来当"这一轮有没有进展"的判据,任一动了就算有进展。
STATE_JS = r"""
() => {
    const de = document.scrollingElement || document.documentElement;
    return {
        scroll_top: Math.round(de.scrollTop),
        scroll_height: Math.round(de.scrollHeight),
        body_text_len: (document.body ? document.body.innerText : '').length,
        has_login_modal: !!document.querySelector(
            ".login-container, .login-modal, [class*='login-box'], [class*='qrcode']"),
        url: location.href,
    };
}
"""

# 取文案完全等于给定文本的**最内层可见**元素(堆叠浮层里点容器中点会命中错误子元素,
# 本仓踩过三次)。两轮匹配:先全等,再"包含且没长多少" —— tab 上常挂未读数徽标,
# 「赞和收藏 3」用全等会落空。**纯查询,不改 DOM。**
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

# 滚轮落点候选:**通知行本身**优先(见模块 docstring 坑①)
_HOVER_SELECTORS = (
    "[class*='interaction-hint']",
    "[class*='user-avatar']",
    "[class*='notification-page']",
)


def take_until_known(messages: list[dict], last_event_time: int | None) -> tuple[list, bool]:
    """截到第一条已采过的事件为止。返回 ``(新事件, 是否已翻到已知区)``。

    通知流是**新事件在最前**的倒序流,所以一旦看见 ``event_time <= last_event_time``,
    后面全是上次采过的,当场停。时间戳**相等**算已知(游标语义是"采到这一刻为止"),
    否则边界那条每轮都要重采一次。

    ``last_event_time is None`` = 这条 channel 还没采过,全要,不停。
    """
    if last_event_time is None:
        return list(messages), False
    fresh = []
    for msg in messages:
        if int((msg or {}).get("time") or 0) <= last_event_time:
            return fresh, True
        fresh.append(msg)
    return fresh, False


def collect_audience(
    page, human, *, targets: dict[str, int | None], full: bool = False,
    deadline: float | None = None,
) -> dict:
    """在已登录的 page 上采两条通知流。

    Args:
        page: 同步 Page(尚未导航;本函数负责进通知页)。
        human: ``SyncHumanActions``,所有导航/点击/滚动都走它。
        targets: ``{channel: last_event_time | None}``,决定每条流采到哪儿停。
        full: True = 强制翻到底(忽略 targets 里的游标,只用它做去重)。
        deadline: ``time.monotonic()`` 口径的收尾时刻;到点无论采到哪都收工。

    Returns:
        ``{"channels": {channel: {"messages", "rounds", "stopped_by", "pages"}}}``;
        未登录 / 撞墙 → ``{"error": str}``(撞墙另带 ``wall`` 取证),**不返回半截数据**。
    """
    buffers: dict[str, list[dict]] = {ch: [] for ch in targets}
    marks = {CHANNEL_SPEC[ch]["api_mark"]: ch for ch in targets}

    def _on_response(resp) -> None:
        """被动监听:只认这两个接口的 JSON 体,读不到就当没发生(监听器绝不抛)。"""
        try:
            url = resp.url or ""
        except Exception:  # noqa: BLE001
            return
        channel = next((ch for mark, ch in marks.items() if mark in url), None)
        if channel is None:
            return
        try:
            body = resp.json()
        except Exception:  # noqa: BLE001
            return
        data = (body or {}).get("data") or {}
        buffers[channel].append({
            "messages": list(data.get("message_list") or []),
            "has_more": bool(data.get("has_more")),
        })

    page.on("response", _on_response)

    human.navigate(NOTIFICATION_URL)
    human.wait(1.5, 2.5, context="等通知页渲染")
    wall = _wall(page)
    if wall is not None:
        return {"error": f"wall_{wall['wall_type']}: 采集撞上风控验证墙,已停手", "wall": wall}

    state = _state(page)
    if state.get("has_login_modal") or "/login" in (state.get("url") or ""):
        # 未登录页上读到的东西不是我们的受众,一条都不入库
        return {"error": "账号未登录(通知页弹登录框或被重定向),本轮不采集"}

    channels: dict[str, dict] = {}
    for channel in CHANNELS:
        if channel not in targets:
            continue
        if deadline is not None and time.monotonic() > deadline:
            logger.warning(f"[audience_collect] 会话预算耗尽,跳过 channel={channel}")
            break
        spec = CHANNEL_SPEC[channel]
        tab = _find_tab(page, spec["tab_texts"])
        if tab is None:
            channels[channel] = _empty(f"tab_not_found:{spec['label']}")
            logger.warning(f"[audience_collect] 找不到 tab「{spec['label']}」,跳过该 channel")
            continue
        # **唯一的点击**:切 tab。除此之外全程只 hover 与滚动。
        human.click(tab, reason=f"切到「{spec['label']}」tab(只读浏览)")
        human.wait(1.5, 2.5, context=f"等「{spec['label']}」首屏渲染")
        wall = _wall(page)
        if wall is not None:
            return {"error": f"wall_{wall['wall_type']}: 采集撞上风控验证墙,已停手",
                    "wall": wall}
        channels[channel] = _drain_channel(
            page, human, channel,
            buffer=buffers[channel],
            last_event_time=None if full else targets.get(channel),
            max_rounds=FULL_SCROLL_ROUNDS if full else INCREMENTAL_SCROLL_ROUNDS,
            deadline=deadline,
        )
        if channels[channel].get("wall"):
            return {"error": "wall: 滚动中撞上风控验证墙,已停手",
                    "wall": channels[channel]["wall"]}

    return {"channels": channels}


def _drain_channel(
    page, human, channel: str, *, buffer: list[dict],
    last_event_time: int | None, max_rounds: int, deadline: float | None,
) -> dict:
    """滚一条 channel 直到停止条件命中;返回该条流的采集结果。

    四个停止条件,优先级即代码顺序:

    - ``reached_known`` 翻到已采过的事件(增量的正常出口);
    - ``exhausted`` 接口自报 ``has_more=false``(全量的正常出口);
    - ``stalled`` 连续 STALL_ROUNDS 轮四个进度指标全不动(页面真不动了);
    - ``round_cap`` / ``budget`` 轮数封顶或会话预算到点(**不声称采完**,下一轮接着来)。
    """
    collected: list[dict] = []
    seen: set[str] = set()
    consumed = 0
    rounds = 0
    stalled = 0
    prev_key: tuple | None = None
    hovered = False

    while True:
        consumed, reached, exhausted = _consume(
            buffer, consumed, collected, seen, last_event_time
        )
        if reached:
            return _channel_result(collected, rounds, "reached_known", consumed)
        if exhausted:
            return _channel_result(collected, rounds, "exhausted", consumed)
        if rounds >= max_rounds:
            # 封顶只防死循环,**不代表采完**:剩下的留给下一轮增量
            logger.info(
                f"[audience_collect] channel={channel} 滚动达上限 {max_rounds},"
                f"已采 {len(collected)} 条"
            )
            return _channel_result(collected, rounds, "round_cap", consumed)
        if deadline is not None and time.monotonic() > deadline:
            return _channel_result(collected, rounds, "budget", consumed)

        if not hovered:
            # 坑①:滚之前必须把鼠标移到通知行上,否则滚轮打在不滚动的顶栏
            hovered = _hover_list(page, human, channel)
        human.wait(0.6, 1.4, context=f"「{channel}」列表浏览")
        human.scroll("down")
        human.wait(0.8, 1.6, context="等懒加载新一屏")
        rounds += 1

        wall = _wall(page)
        if wall is not None:
            result = _channel_result(collected, rounds, "wall", consumed)
            result["wall"] = wall
            return result

        state = _state(page)
        # 坑②:进度四元组必须含 scroll_top —— 漏了它,"滚轮在动但懒加载没触发"的几轮
        # 会被误判成到底
        key = (len(buffer), state.get("scroll_top"), state.get("scroll_height"),
               state.get("body_text_len"))
        stalled = stalled + 1 if key == prev_key else 0
        prev_key = key
        if stalled >= STALL_ROUNDS:
            return _channel_result(collected, rounds, "stalled", consumed)


def _consume(
    buffer: list[dict], start: int, collected: list[dict], seen: set,
    last_event_time: int | None,
) -> tuple[int, bool, bool]:
    """把 ``buffer[start:]`` 里已到达的响应摊进 ``collected``。

    返回 ``(下一个起点, 是否翻到已知区, 接口是否自报到底)``。按平台事件 id 去重:
    平台真的会重复下发同一条(实采 922 条里只有 921 个唯一 id)。
    """
    reached = exhausted = False
    index = start
    while index < len(buffer):
        page_data = buffer[index]
        index += 1
        fresh, hit_known = take_until_known(page_data["messages"], last_event_time)
        for msg in fresh:
            event_id = str((msg or {}).get("id") or "")
            if not event_id or event_id in seen:
                continue
            seen.add(event_id)
            collected.append(msg)
        if hit_known:
            reached = True
            break
        if not page_data["has_more"]:
            exhausted = True
            break
    return index, reached, exhausted


def _channel_result(messages: list, rounds: int, stopped_by: str, pages: int) -> dict:
    return {"messages": messages, "rounds": rounds, "stopped_by": stopped_by,
            "pages": pages}


def _empty(stopped_by: str) -> dict:
    return _channel_result([], 0, stopped_by, 0)


def _find_tab(page, texts: list[str]):
    """找 tab 元素(最内层可见、文案匹配);找不到返回 None。"""
    try:
        return page.evaluate_handle(_INNERMOST_JS, list(texts)).as_element()
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[audience_collect] 定位 tab 失败: {exc}")
        return None


def _hover_list(page, human, channel: str) -> bool:
    """把鼠标悬到通知行上(滚轮落点);悬不上去返回 False 并告警。"""
    box = None
    for selector in _HOVER_SELECTORS:
        try:
            element = page.query_selector(selector)
            candidate = element.bounding_box() if element is not None else None
        except Exception:  # noqa: BLE001
            candidate = None
        if candidate and candidate["width"] >= 200 and candidate["height"] > 10:
            box = candidate
            break
    if not box:
        logger.warning(
            f"[audience_collect] channel={channel} 找不到通知行,滚轮可能打在不滚动的"
            f"顶栏上,本轮多半翻不动页"
        )
        return False
    x, y = _clamp_to_viewport(
        page, box["x"] + box["width"] * 0.5, box["y"] + box["height"] * 0.5
    )
    human.hover((x, y), reason="移到通知列表(滚轮落点)")
    return True


def _clamp_to_viewport(page, x: float, y: float) -> tuple[float, float]:
    """把坐标夹进视口(留 10% 边距)。

    列表矩形常常是**文档坐标**(y 可能上千),把鼠标"移"到视口外不是真的悬停,
    滚轮落点也就无从谈起。
    """
    try:
        size = page.viewport_size or {}
    except Exception:  # noqa: BLE001
        size = {}
    width = float(size.get("width") or 1280)
    height = float(size.get("height") or 800)
    return (min(max(x, width * 0.1), width * 0.9),
            min(max(y, height * 0.1), height * 0.9))


def _state(page) -> dict[str, Any]:
    """只读页面状态;读不到给空 dict(一次读失败不该打断整轮采集)。"""
    try:
        return page.evaluate(STATE_JS) or {}
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[audience_collect] 读页面状态失败: {exc}")
        return {}


def _wall(page) -> dict | None:
    """撞墙了吗?撞了返回取证 dict(URL 是硬判据,正文只用来分型)。"""
    try:
        url = page.url or ""
    except Exception:  # noqa: BLE001
        return None
    if not is_wall_url(url):
        return None
    try:
        text = page.evaluate(PAGE_TEXT_JS) or ""
    except Exception:  # noqa: BLE001 — 取证自身绝不抛
        text = ""
    return {"wall_type": classify_wall_text(text), "landed_url": url,
            "target_url": NOTIFICATION_URL, "page_text": text}
