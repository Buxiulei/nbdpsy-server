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
  取第一篇**——窗口内发布者可能发了多篇,取第一篇会点错笔记。主页是懒加载的,首屏
  找不到要**滚动加载**再找(见 ``_scroll_until_found``)。
- **已赞/已藏只看 ``use[xlink:href]``**(#like/#liked、#collect/#collected)。旧仓
  ``already_liked = "like-active" in class`` 是错的:实测该 class 点赞前后常驻,
  照搬会 100% 误判为"已点赞"。
- **``.not-active.inner-when-not-active`` 是未激活的评论入口,不是遮罩**:拟人点它激活
  输入区,绝不用 JS 把它 display:none 隐藏(旧仓 comment_note 的做法是把入口当障碍物拆了)。

全程 ``SyncHumanActions``;``page.evaluate`` 只用于**只读取证**(读图标 href / 读按钮
class / 读命中元素 / 失败现场快照),与 ``creator_export`` 读表格行数同性质,不做任何
JS 合成点击或 JS 设值。

任一动作失败不阻断其余动作,结果按动作粒度汇总(见 ``interact_with_note`` 返回值)。

**失败当场留取证**(2026-08-02):动作判 error 时随结果带一个 ``forensics`` 键,记下
那一刻的 URL / 标题 / 正文前若干字 / 互动栏在不在 / 栏内有哪些 wrapper / 赞与藏两个
按钮的图标 href、矩形与计算样式(见 ``_FORENSICS_JS`` 与 ``collect_forensics``)。
三条纪律:**只在失败时抓**(成功路径不读页面)、**取证自身绝不抛异常**(抓不到就降级
成一句原因)、**只读**。事后靠复现去猜失败原因是行不通的 —— 复现不出来线索就断了。
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

# 主页懒加载翻找的两个闸(见 _scroll_until_found):
# 轮数硬上限 —— 纯防死循环。实测单号笔记数(几十篇)远小于 30 轮能加载出的量;取到底
# 也就多花约两分钟,且只发生在笔记真不在主页上的失败路径。
_MAX_SCROLL_ROUNDS = 30
# 连续这么多轮卡片数不再增长才认定"到底"。**单次无新增不算到底**:创作中心那边真号
# 实测出现过滚动只挪了一点没触发加载、下一轮才加载的情况(见 creator_note_list)。
_NO_GROWTH_ROUNDS = 3

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

# 失败现场取证(只读):互动动作失败的那一刻页面到底长什么样。
#
# 为什么要有这东西:排查「收藏按钮找不到」时绕了三大圈、20+ 次真号访问都没复现,根本
# 原因是失败当场什么现场证据都没留,只能事后靠复现去猜 —— 复现不出来线索就彻底断了。
# 补量一轮 5 篇失败 1 篇时错误信息只有「点赞与收藏均失败」一句,连是被踢到验证页、还是
# 互动栏改版、还是按钮被盖住都分不出来。
#
# 抓的东西一一对应排查时最先要问的几个问题:落在哪个 URL(是不是被踢到验证/登录页)、
# 页面到底是什么内容(标题 + 正文前若干字)、互动栏在不在、栏里有哪几个 wrapper
# (赞/藏/评论/分享齐不齐)、赞与藏两个按钮各自的图标 href / 矩形 / 计算样式。
#
# **必须与真正被点的那个元素说的是同一个**:``_icon_action`` 走 ``_resolve_selector``,
# 首选 ``.engage-bar .like-wrapper``(限定在互动栏内),取证要是用裸 ``.like-wrapper``
# 全文档找,``querySelector`` 返回的是**文档序第一个** —— 主页网格里的笔记卡自己就带
# 点赞图标,取证很可能描述的根本不是被点的那个按钮,那样的"证据"比没有还坏。故 probe
# 一律**限定在 bar 内**(bar 不在才退回全文档,并在 ``scope`` 里注明退过)。
#
# ``counts`` 是冲着一条具体怀疑去的:互动栏 / 赞 / 藏在整页各有几个。**大于 1 就说明
# 选择器有歧义**(例如上一篇的详情浮层没销毁、网格卡片也匹配),那正好解释"点了但图标
# 不翻、且赞与藏成对失败"—— 点的是另一个同名元素。等于 1 则这条怀疑当场出局。
#
# **只读**:与本模块其余 evaluate 同性质,不点击、不设值、不改 DOM。
# **JS 只负责读,裁剪一律在 Python 侧**(``_shrink_forensics``):裁剪规则是要落库的口径,
# 放在 Python 才测得到;读回来的正文再长也只是一次进程内传输,永远进不了库。
_FORENSICS_JS = r"""
() => {
    const bar = document.querySelector('.interactions.engage-bar')
        || document.querySelector('.engage-bar');
    const probe = (sel) => {
        // 与 _icon_action 的定位口径对齐:先在互动栏内找,栏不在才退回全文档
        const el = (bar || document).querySelector(sel);
        if (!el) return {present: false, scope: bar ? 'bar' : 'document'};
        const use = el.querySelector('use');
        const r = el.getBoundingClientRect();
        const cs = window.getComputedStyle(el);
        return {
            present: true,
            scope: bar ? 'bar' : 'document',
            icon_href: use
                ? (use.getAttribute('xlink:href') || use.getAttribute('href') || null)
                : null,
            rect: {
                x: Math.round(r.x), y: Math.round(r.y),
                w: Math.round(r.width), h: Math.round(r.height),
            },
            display: cs.display,
            visibility: cs.visibility,
            pointer_events: cs.pointerEvents,
        };
    };
    return {
        url: location.href,
        title: document.title,
        body: document.body ? document.body.innerText : '',
        engage_bar: !!bar,
        // class 用 getAttribute 读:SVG 元素的 className 是 SVGAnimatedString 不是字符串
        wrappers: bar
            ? Array.from(bar.querySelectorAll('[class*=wrapper]'))
                .map((el) => el.getAttribute('class'))
            : [],
        like: probe('.like-wrapper'),
        collect: probe('.collect-wrapper'),
        // 整页各有几个:>1 = 选择器有歧义,点到的很可能不是看到的那个(见上方注释)
        counts: {
            engage_bar: document.querySelectorAll('.engage-bar').length,
            like: document.querySelectorAll('.like-wrapper').length,
            collect: document.querySelectorAll('.collect-wrapper').length,
        },
    };
}
"""

# 取证各字段的落库上限。定这几个数的依据只有一条:**排查要的信息都在头部**——被踢到
# 验证页看 URL 就够了,页面变成什么看标题与正文头几十个字就够了,互动栏结构看前十几个
# wrapper 就够了。再多的量对排查没有增量价值,却要长期占着 browser_jobs.result 与
# note_interactions.detail。
_MAX_URL = 300
_MAX_TITLE = 200
_MAX_BODY = 200
_MAX_CLASS = 80
_MAX_WRAPPERS = 12


def _cut(text: Any, limit: int) -> str:
    """空白归一 + 截断(正文里的大段换行对排查没用,只会撑长度)。"""
    return " ".join(str(text if text is not None else "").split())[:limit]


def _shrink_forensics(data: Dict[str, Any]) -> Dict[str, Any]:
    """把 JS 读回来的原始快照裁到落库口径(正文只留头部,wrapper 列表只留前若干条)。

    ``body`` 读回来是整页正文,**改名 ``body_head`` 落库**是为了让读的人一眼知道这是
    截断过的头部,而不是以为"这页正文就这么点"。
    """
    shrunk = dict(data)
    shrunk["url"] = _cut(data.get("url"), _MAX_URL)
    shrunk["title"] = _cut(data.get("title"), _MAX_TITLE)
    shrunk["body_head"] = _cut(shrunk.pop("body", ""), _MAX_BODY)
    raw_wrappers = data.get("wrappers")
    shrunk["wrappers"] = [
        _cut(w, _MAX_CLASS)
        for w in (raw_wrappers if isinstance(raw_wrappers, list) else [])
    ][:_MAX_WRAPPERS]
    return shrunk


def collect_forensics(page) -> Dict[str, Any]:
    """抓一份失败现场(只读),**保证不抛异常**;抓不到就降级成一句原因。

    降级是硬要求:取证是排查用的附加信息,绝不能把一个"动作失败"升级成"任务崩溃"。
    故 ``page.url`` 单独先读一次:连 ``evaluate`` 都跑不通(页面已关 / 被导航走 /
    执行上下文销毁)时,至少还留得下"落在哪个 URL"—— 而那恰恰是最能一眼定性的一条
    (验证墙 / 登录页 / 404)。
    """
    snapshot: Dict[str, Any] = {}
    try:
        snapshot["url"] = _cut(page.url, _MAX_URL)
    except Exception as exc:  # noqa: BLE001 — 连 URL 都读不到也只记一句,不上抛
        snapshot["url_error"] = f"取证失败: 读 page.url {type(exc).__name__}: {exc}"[:200]
    try:
        data = page.evaluate(_FORENSICS_JS)
        if not isinstance(data, dict):
            raise TypeError(f"evaluate 返回 {type(data).__name__},不是对象")
        snapshot.update(_shrink_forensics(data))
    except Exception as exc:  # noqa: BLE001 — 取证失败必须降级,绝不上抛
        snapshot["error"] = f"取证失败: {type(exc).__name__}: {exc}"[:200]
    return snapshot


def _with_forensics(page, outcome: Dict[str, Any]) -> Dict[str, Any]:
    """失败结果补一份现场取证后原样返回;**done / skipped 一个字节都不多花**。

    成功路径天天在跑,这里只多一次 dict 取值就返回,不读页面、不进 JS。
    """
    if outcome.get("status") == "error":
        outcome["forensics"] = collect_forensics(page)
    return outcome


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


def _match_card(cards: List[Any], note_id: Optional[str], title: str):
    """在已加载的卡片里找目标:**note_id 优先**,没有对应卡片时回退标题匹配;都不中返回 None。

    ``note_id`` 优先(2026-08-01):主页卡片的链接 href 里带笔记 id,是稳定主键;而标题
    会变(实测平台上「粤语咨询师-黄安麟…」在台账里记的是「心理咨询师-…」)。
    """
    hit = next((c for c in cards if _card_matches_note_id(c, note_id or "")), None)
    if hit is not None:
        logger.info(f"[matrix_interact] 按 note_id={note_id} 命中笔记卡")
        return hit
    for card in cards:
        try:
            text = card.inner_text()
        except Exception:  # noqa: BLE001 — 单张卡读文本失败当不命中
            continue
        if _title_matches(text, title):
            return card
    return None


def _hover_card_list(human: SyncHumanActions, cards: List[Any]) -> None:
    """滚动前把鼠标移到笔记卡上,让滚轮事件落进真正的滚动容器;移不过去只告警。

    ``page.mouse.wheel`` 把滚轮事件投在**鼠标当前位置**,而鼠标从未移动过时停在 (0,0)。
    创作中心那边真号实测过:(0,0) 是不滚动的顶栏,滚轮全部空转,``scrollTop`` 三次滚动
    后仍是 0(见 ``creator_note_list._hover_note_list``)。主页的滚动容器结构未必和创作
    中心一样,但"**别假设 ``mouse.wheel`` 会滚到你想滚的地方**"这条教训通用,故一律先
    悬停到卡片上——卡片本身必在滚动区里,不需要 ``evaluate`` 去找容器。
    """
    box = None
    try:
        box = cards[0].bounding_box() if cards else None
    except Exception as exc:  # noqa: BLE001 — 定位失败只降级,不打断翻找
        logger.warning(f"[matrix_interact] 读笔记卡矩形失败: {exc}")
    if not box:
        logger.warning(
            "[matrix_interact] 取不到笔记卡矩形,鼠标无法移到列表上"
            "——滚轮可能打在不滚动的区域上,翻找多半停在首屏"
        )
        return
    # 只悬停不点击(卡片悬停不触发跳转)
    human.hover(
        (box["x"] + box["width"] * 0.5, box["y"] + box["height"] * 0.5),
        reason="移到笔记卡上(滚轮落点)",
    )


def _scroll_until_found(
    page,
    human: SyncHumanActions,
    cards: List[Any],
    note_id: Optional[str],
    title: str,
):
    """首屏没命中时滚动懒加载,**边滚边找,找到就停**;滚到底或到轮数上限仍没有返回 None。

    ``cards`` 是首屏已扫过的那批,只用来定滚轮落点和记初始卡片数。

    不一次滚到底再统一匹配:那会白白拉长会话、增加风控暴露。到底的判定要求**连续**
    ``_NO_GROWTH_ROUNDS`` 轮卡片数都不增长——单次没新增不算到底(理由见常量处注释)。
    """
    _hover_card_list(human, cards)
    loaded = len(cards)
    stale_rounds = 0
    for round_no in range(1, _MAX_SCROLL_ROUNDS + 1):
        human.wait(0.8, 2.0, context="主页翻找笔记")
        human.scroll("down")
        cards = page.query_selector_all(_NOTE_CARD)
        hit = _match_card(cards, note_id, title)
        if hit is not None:
            logger.info(
                f"[matrix_interact] 第 {round_no} 轮滚动后命中笔记卡"
                f"(已加载 {len(cards)} 张)"
            )
            return hit
        if len(cards) > loaded:
            loaded = len(cards)
            stale_rounds = 0
            continue
        stale_rounds += 1
        if stale_rounds >= _NO_GROWTH_ROUNDS:
            logger.info(
                f"[matrix_interact] 连续 {stale_rounds} 轮无新卡片(共 {loaded} 张),"
                f"主页已到底"
            )
            return None
    logger.warning(
        f"[matrix_interact] 已达滚动上限 {_MAX_SCROLL_ROUNDS} 轮(共 {loaded} 张卡),"
        f"主动停止翻找(防死循环)"
    )
    return None


def _open_note_by_title(
    page,
    human: SyncHumanActions,
    publisher_user_id: str,
    title: str,
    note_id: Optional[str] = None,
) -> str:
    """拟人导航发布者主页 → **优先按 note_id**、否则按标题匹配笔记卡 → 点进详情;返回 URL。

    主页是**懒加载**的,首屏只渲染约 10 张卡:发布后立刻互动时目标必排在最前面,首屏即中;
    但历史笔记补量要找的老笔记排位靠后,不滚动就永远找不到(2026-08-01 补量实跑 5 篇失败
    1 篇,笔记明明还在平台上,只是在该号 9 篇里排后面)。故**首屏找一次,没中才滚**——
    首屏命中这条路天天在跑,必须一次都不滚、行为完全不变。

    滚到底仍匹配不到即抛 ``MatrixInteractError``(绝不退而求其次点第一篇)。
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
    hit = _match_card(cards, note_id, title)
    if hit is None:
        logger.info(
            f"[matrix_interact] 首屏 {len(cards)} 张卡未命中 note_id={note_id!r} /"
            f" 标题「{title}」,滚动加载更多"
        )
        hit = _scroll_until_found(page, human, cards, note_id, title)
    if hit is None:
        raise MatrixInteractError(
            f"note_not_found: 发布者主页未找到 note_id={note_id!r} / 标题「{title}」的笔记卡"
            f"(已滚动加载 {len(page.query_selector_all(_NOTE_CARD))} 张卡)"
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
    """点赞/收藏同构动作;**失败时附一份现场取证**(``forensics`` 键)。

    返回 ``{"status": "done"|"skipped"|"error", "reason"?, "forensics"?}``;已激活记
    skipped 非 error。取证只在 error 分支抓(动作步骤全在 ``_icon_action_steps`` 里,
    这一层只负责在它判 error 时补现场)—— 成功与跳过路径完全不读页面。
    """
    return _with_forensics(
        page,
        _icon_action_steps(
            page, human, name, selectors, off_href, on_href, verify_timeout_s
        ),
    )


def _icon_action_steps(
    page,
    human: SyncHumanActions,
    name: str,
    selectors: List[str],
    off_href: str,
    on_href: str,
    verify_timeout_s: float,
) -> Dict[str, Any]:
    """点赞/收藏动作本体:读图标 → 已激活则跳过 → 拟人点击 → 复核图标真的变了。

    返回 ``{"status": "done"|"skipped"|"error", "reason"?}``(取证由 ``_icon_action`` 补)。
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
    """评论;**页面侧失败时附一份现场取证**(``forensics`` 键)。

    返回 ``{"status": "done"|"error", "reason"?, "cleared"?, "forensics"?}``;``cleared``
    是复核时输入框是否已清空,**仅供排查**不参与成败判定。文案由调用方传入且**必填**——
    评论自 2026-07-31 起是独立能力(``comment_on_note`` / REST ``note-comments``),不再是
    矩阵互动里那个"可以不传就跳过"的可选动作,故空文案是**入参错误**记 error。
    (历史上这里返回过 ``not_requested``,那是为了让"没要求评论"不被当成失败证据;
    评论独立后不存在"没要求"这回事,该状态一并取消,见 ``interact_with_note`` 的成败判定。)

    空文案那条**不取证**:它在碰页面之前就判掉了,现场跟失败原因毫无关系,抓一份只是
    白给台账塞垃圾。取证只服务"在页面上动手却没成"的那些失败。
    """
    if not (text or "").strip():
        return {"status": "error", "reason": "comment_text_empty: 未提供评论文案"}
    return _with_forensics(page, _do_comment_steps(page, human, text))


def _do_comment_steps(page, human: SyncHumanActions, text: str) -> Dict[str, Any]:
    """评论本体:激活入口 → 等输入区可交互 → 逐字输入 → 等发送键可用 → 发送 → 复核。"""
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
            # 异常路径同样留现场:抛异常时最需要知道页面当时是什么样(取证自带降级)
            actions[key] = _with_forensics(
                page, {"status": "error", "reason": f"{key}_exception: {exc}"}
            )
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
        # 把动作级取证顶到结果顶层:调用方(matrix_interact / interaction_backfill 服务层)
        # 读的是这一层的 error,现场证据得跟它在一起,否则又变成"只有一句失败没有原因"。
        # 这里**不重新抓一次**:每个失败动作在它失败的那一刻已经各抓过一份,当场那份比
        # 事后补抓的更贴近真相,顶层直接复用第一份即可(也顺带避免同一页抓三遍撑大结果)。
        forensics = next(
            (a["forensics"] for a in actions.values() if a.get("forensics")), None
        )
        if forensics is not None:
            result["forensics"] = forensics
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
        outcome = _with_forensics(
            page, {"status": "error", "reason": f"comment_exception: {exc}"}
        )
    logger.info(
        f"[note_comment] 账号{account_id} 评论: {outcome['status']}"
        f" {outcome.get('reason', '')}"
    )

    if outcome["status"] != "done":
        failed: Dict[str, Any] = {
            "note_url": note_url,
            "error": outcome.get("reason") or "comment_failed",
        }
        # 现场取证跟着 error 一起交出去(note_comment.execute 原样返回 → browser_jobs.result)
        if outcome.get("forensics"):
            failed["forensics"] = outcome["forensics"]
        return failed
    return {"note_url": note_url, "commented": True}
