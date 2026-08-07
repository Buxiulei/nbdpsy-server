"""他人笔记评论区只读抓取(纯同步,吃已登录 page):滚动加载 → 读 DOM → 到底判定。

**为什么这件事必须开浏览器**(2026-08-07 取证):详情页 SSR 的 ``__INITIAL_STATE__`` 里
``comments.list`` 是空数组、``firstRequestFinish=false`` —— 评论是纯客户端异步拉的,
服务端渲染阶段压根没下发。而直接打 ``edith.xiaohongshu.com`` 的评论接口,不带页面内
JS 现算的 ``x-s``/``x-t`` 签名头一律 406(带不带 cookie 都一样)。**唯一**剩下的路是
在真实浏览器里让页面自己去拉,我们只读它渲染出来的 DOM。

**选择器来源:旧仓移植 + 2026-08-07 账号 9(米之木木)真号只读复核通过**。
``.parent-comment``(10 个一楼)/ ``.comment-item``(11 = 10 一楼 + 1 子回复)/
``.author a.name``(昵称,``data-user-id`` 就挂在这个 a 上)/ ``.note-text`` /
``.like-wrapper .count`` / ``.comment-item-sub`` / ``.tag``(作者徽标,实测只在作者
那条上命中)—— 逐个在样例笔记 6a4f50d0…5535 上取到了真值。

复核当场逮到三个"照旧仓抄就会错"的地方,都已修在下面:

1. **0 赞时计数位的文案是「赞」不是「0」**——按"转不成 int 就给 None"处理会把"没人赞"
   和"我没读到"混成同一个值,故 ``_ZERO_LIKE_LABELS`` 显式把它认成 0;
2. **评论区矩形是 y≈2099 的文档坐标,视口只有一千出头**——直接 hover 到那个坐标不是
   真悬停,滚轮落点无从谈起,故 ``_clamp_to_viewport`` 把落点夹进视口;
3. **平台标称的 ``commentCount`` 含子回复**(实测标称 17,页面上是 10 条一楼 + 子回复),
   只拿一楼条数去比 ``expected_total`` 这条判据永远够不着,故按 ``_total_with_subs`` 比。

页面级同名选择器有污染(全页 ``.note-text`` 12 个 > 评论 11 条,笔记正文也用它;
``.tag`` 全页 11 个多来自正文话题),**所有读取都必须 scope 在单条 ``.comment-item``
之内**,不要在 page 级直接查 —— 本模块的 ``_parse_item`` 就是这么做的。

仍未验证:``.show-more``(展开更多回复)本模块**不点**,故子回复只取默认展开的那些;
到底判据在这条 17 评论的笔记上没能触发 ``reached_expected_total``(见上第 3 条修法),
真到底靠 ``no_new_after_scroll``。抓不到评论时返回 ``comments: []`` + ``stop_reason``,
调用方能一眼看出"没抓到"而不是"这篇没人评论"—— 平台给的 ``interact.comment`` 是第二只眼。

三条纪律与本仓其它只读模块一致:

1. **滚动前先 hover 到评论列表**:``page.mouse.wheel`` 把事件投在鼠标当前位置,鼠标
   没动过时停在 (0,0) —— 真号实测那里是不滚动的顶栏,滚轮全空转(见
   ``creator_note_list._hover_note_list`` 的实测记录)。
2. **零 JS 注入**:全程 ``query_selector`` 读,不 ``evaluate`` 合成任何东西。既是拟人化
   纪律,也让这条链路能被离线回放夹具证伪。
3. **撞墙立即停手**:URL 被重定向到 captcha / website-login 就中止,把已抓到的交出去
   并标 ``error="wall"``。继续滚只会把号推得更深。
"""

from typing import Any, Dict, List, Optional

from loguru import logger

from app.browser.login_detector import is_wall_url

# ---- 选择器(旧仓移植 + 2026-08-07 账号 9 真号复核通过;改动前请先采夹具)----
PARENT_COMMENT = ".parent-comment"      # 一楼(含其下所有子回复)
COMMENT_ITEM = ".comment-item"          # 楼内单条(主楼那条)
COMMENT_AUTHOR = ".author a.name"       # 评论者昵称锚(带 data-user-id)
COMMENT_TEXT = ".note-text"             # 评论正文
COMMENT_LIKE = ".like-wrapper .count"   # 评论点赞数
SUB_COMMENT = ".comment-item-sub"       # 子回复
AUTHOR_BADGE = ".tag"                   # 「作者」徽标(兜底判据,见 _is_author)

# 滚动轮数硬上限:纯防死循环。20 轮 × 每轮若干条,足够拿到常见爆款的前几百条评论;
# 到底判据正常都会先于它触发。
DEFAULT_MAX_ROUNDS = 20
# 连续几轮无新增判到底。**1 轮不够**:实测创作中心列表出现过"滚一次只挪了 260px 没触发
# 请求,下一次才出下一页"的情况,单轮无新增会把还没到底的列表判成到底。
DEFAULT_IDLE_ROUNDS = 2


def read_note_comments(
    page,
    human,
    *,
    max_count: int = 20,
    max_rounds: int = DEFAULT_MAX_ROUNDS,
    idle_rounds: int = DEFAULT_IDLE_ROUNDS,
    expected_total: Optional[int] = None,
    note_author_user_id: Optional[str] = None,
) -> Dict[str, Any]:
    """在已打开的笔记详情页上滚动读评论。

    Args:
        page: 已登录且**已停在目标笔记详情页**的同步 Page(导航由调用方负责)。
        human: ``SyncHumanActions``(滚动 / 悬停 / 停顿全走它)。
        max_count: 最多返回几条一楼评论(0 = 不抓)。
        max_rounds: 滚动轮数硬上限(防死循环)。
        idle_rounds: 连续几轮无新增判到底。
        expected_total: 平台给的评论总数(纯 HTTP 层的 ``interact.comment``);抓够即停。
        note_author_user_id: 笔记作者的 user_id,用于判"是否作者回复"。

    Returns:
        ``{"comments": [...], "complete": bool, "stop_reason": str, "rounds": int}``;
        撞墙时另带 ``error="wall"``(已抓到的评论仍原样交出)。
    """
    if max_count <= 0:
        return _result([], True, "not_requested", 0)

    if is_wall_url(getattr(page, "url", "")):
        logger.warning("[note_comments] 进页即撞验证墙,立即停手")
        return {**_result([], False, "wall", 0), "error": "wall"}

    collected: List[dict] = []
    seen: set[str] = set()
    idle = 0
    rounds = 0
    hovered = False

    while True:
        added = _collect_round(page, collected, seen, max_count, note_author_user_id)
        if len(collected) >= max_count:
            return _result(collected[:max_count], True, "reached_limit", rounds)
        if expected_total is not None and _total_with_subs(collected) >= expected_total:
            # 平台标称的 commentCount **含子回复**(真号实测:标称 17,页面上是 10 条
            # 一楼 + 若干子回复)。只拿一楼条数去比,这条判据永远够不着、形同虚设。
            return _result(collected, True, "reached_expected_total", rounds)

        idle = 0 if added else idle + 1
        if idle >= idle_rounds:
            # 连续多轮滚动都没有新评论 = 到底了(而不是"没抓到")
            return _result(collected, True, "no_new_after_scroll", rounds)
        if rounds >= max_rounds:
            # 轮数封顶只防死循环,**不能**声称抓到底
            logger.info(f"[note_comments] 滚动轮数达上限 {max_rounds},已抓 {len(collected)} 条")
            return _result(collected, False, "round_cap", rounds)

        if not collected and not _has_any_comment(page):
            # 一条都没有:这篇可能真没人评论(也可能选择器失配,靠 expected_total 交叉验证)
            return _result([], True, "empty", rounds)

        if not hovered:
            hovered = _hover_comment_list(page, human)
        human.wait(0.6, 1.6, context="评论区浏览")
        human.scroll("down")
        rounds += 1

        if is_wall_url(getattr(page, "url", "")):
            logger.warning(f"[note_comments] 滚动中撞验证墙,已抓 {len(collected)} 条后停手")
            return {**_result(collected, False, "wall", rounds), "error": "wall"}


def _total_with_subs(collected: List[dict]) -> int:
    """一楼 + 子回复的总条数(与平台 commentCount 同口径)。"""
    return sum(1 + len(c.get("sub_comments") or []) for c in collected)


def _result(comments: List[dict], complete: bool, stop_reason: str, rounds: int) -> Dict[str, Any]:
    return {
        "comments": comments,
        "complete": complete,
        "stop_reason": stop_reason,
        "rounds": rounds,
    }


def _has_any_comment(page) -> bool:
    try:
        return bool(page.query_selector_all(PARENT_COMMENT))
    except Exception:  # noqa: BLE001 — 读不到就当没有,不打断
        return False


def _collect_round(
    page, collected: List[dict], seen: set, max_count: int, note_author_user_id: Optional[str]
) -> int:
    """把当前 DOM 上的一楼评论并进 ``collected``(按 comment_id 去重),返回新增条数。

    没有 id 的条目**不丢弃**(内容才是运营要的),用其正文当去重键 —— 两条一模一样又
    都没 id 的评论会被并成一条,这个代价小于丢内容。
    """
    added = 0
    try:
        parents = page.query_selector_all(PARENT_COMMENT)
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[note_comments] 读评论列表失败: {exc}")
        return 0
    for parent in parents:
        item = _first(parent, COMMENT_ITEM)
        if item is None:
            continue
        parsed = _parse_item(item, note_author_user_id)
        key = parsed["comment_id"] or f"text:{parsed['text']}"
        if key in seen:
            continue
        seen.add(key)
        parsed["sub_comments"] = [
            _parse_item(sub, note_author_user_id) for sub in _all(parent, SUB_COMMENT)
        ]
        collected.append(parsed)
        added += 1
        if len(collected) >= max_count:
            break
    return added


def _parse_item(item, note_author_user_id: Optional[str]) -> dict:
    """单条评论 → 契约 dict。任何一格读不到都给 None,不让整条失败。"""
    raw_id = _attr(item, "id") or ""
    author_el = _first(item, COMMENT_AUTHOR)
    like_raw = _text(_first(item, COMMENT_LIKE))
    author_id = _attr(author_el, "data-user-id")
    return {
        "comment_id": raw_id.replace("comment-", "") or None,
        "author": _text(author_el) or None,
        "author_id": author_id,
        "text": _text(_first(item, COMMENT_TEXT)),
        "like_count": _to_int(like_raw),
        "like_count_raw": like_raw,
        "is_author_reply": _is_author(item, author_id, note_author_user_id),
        "sub_comments": [],
    }


def _is_author(item, author_id: Optional[str], note_author_user_id: Optional[str]) -> bool:
    """是否作者本人的回复。

    **优先按 user_id 比对**(语义判据,与 DOM 长相无关);拿不到 user_id 时才退到
    「作者」徽标。徽标 ``.tag`` 真号实测确实只在作者那条上命中,但全页有 11 个同名元素
    (正文话题也用它),所以它只能在 ``.comment-item`` 内作用域里当兜底,不该当第一判据。
    """
    if note_author_user_id and author_id:
        return author_id == note_author_user_id
    return _first(item, AUTHOR_BADGE) is not None


def _hover_comment_list(page, human) -> bool:
    """把鼠标移到评论列表上,让滚轮事件落进真正的滚动容器;移不过去返回 False。"""
    try:
        first = page.query_selector(PARENT_COMMENT)
        box = first.bounding_box() if first is not None else None
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[note_comments] 读评论矩形失败: {exc}")
        box = None
    if not box:
        logger.warning(
            "[note_comments] 定位不到评论列表,滚轮可能打在不滚动的区域上,翻页多半停在首屏"
        )
        return False
    # **必须夹进视口**:真号实测评论区矩形是 y≈2099 的文档坐标,而视口只有一千出头
    # —— 直接把鼠标"移"到视口外不是真的悬停,滚轮落点也就无从谈起。夹到视口内的同一
    # 竖直方向上,既保住"落在正文/评论这条滚动轴上"的本意,又保证坐标是真实可达的。
    x, y = _clamp_to_viewport(
        page, box["x"] + box["width"] * 0.5, box["y"] + box["height"] * 0.5
    )
    human.hover((x, y), reason="移到评论列表滚动区(滚轮落点)")
    return True


def _clamp_to_viewport(page, x: float, y: float) -> tuple[float, float]:
    """把坐标夹进当前视口(留 10% 边距);读不到视口尺寸就按常见 1280x800 兜底。"""
    try:
        size = page.viewport_size or {}
    except Exception:  # noqa: BLE001
        size = {}
    width = float(size.get("width") or 1280)
    height = float(size.get("height") or 800)
    return (
        min(max(x, width * 0.1), width * 0.9),
        min(max(y, height * 0.1), height * 0.9),
    )


# ---- 读元素的小工具(全部吞异常降级成 None:一格读不到不该炸整篇)----


def _first(scope, selector: str):
    try:
        return scope.query_selector(selector)
    except Exception:  # noqa: BLE001
        return None


def _all(scope, selector: str) -> List[Any]:
    try:
        return list(scope.query_selector_all(selector))
    except Exception:  # noqa: BLE001
        return []


def _text(element) -> str:
    if element is None:
        return ""
    try:
        return (element.inner_text() or "").strip()
    except Exception:  # noqa: BLE001
        return ""


def _attr(element, name: str) -> Optional[str]:
    if element is None:
        return None
    try:
        return element.get_attribute(name)
    except Exception:  # noqa: BLE001
        return None


# 0 赞时平台在计数位上显示的字面文案(真号实测:有赞的显示 "1",没赞的显示 "赞")。
# 把它当"读不到"会让"这条没人赞"和"我没读到"混成同一个 None —— 那正是取证要防的事。
_ZERO_LIKE_LABELS = ("赞", "点赞", "")


def _to_int(value: str) -> Optional[int]:
    """点赞数转 int。

    三种真实取值(2026-08-07 账号 9 真号实测):``"1"`` 这类数字、``"赞"``(0 赞时的
    占位文案)、以及理论上的 ``"1.2万"`` 简写。前两种给确定的 int,简写转不了才给
    None(原串保留在 ``like_count_raw`` 里,绝不瞎折算)。
    """
    text = (value or "").strip()
    if text in _ZERO_LIKE_LABELS:
        return 0
    try:
        return int(text)
    except (TypeError, ValueError):
        return None
