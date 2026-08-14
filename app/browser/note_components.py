"""笔记三组件(合集 / 引用笔记 / 关联活动)设置器 + 编辑已发布笔记(纯同步,吃已登录 page)。

设计 docs/design/2026-08-01-note-components-design.md 第二 / 三节(真号受控写测试结论)。

两个对外入口:

- ``apply_components``:在**已经打开的编辑器页**上设置三组件(发布新笔记与编辑已发布
  笔记共用这一段);
- ``set_note_components``:编辑已发布笔记的完整流程 —— 深链进更新页 → 权限只读留底 →
  **破坏性编辑步(图片增删 / 标题 / 正文,任一失败即弃提交)** → 设置组件 → 点发布 →
  **重进更新页逐项回读**。文本与图片的实现在 ``note_editing*.py``,本文件只做编排:
  一次请求**只有一次提交**,提交次数就是风险次数(编辑设计
  ``docs/design/2026-08-03-note-editing-design.md`` 第二节裁决)。

这条产品线最反直觉的一点:**失败普遍是静默的**(设计 2.6 / 2.7④,均为真号实测):

- 私密笔记的合集绑定被服务端**静默丢弃**:``success:true`` 照返、零 toast、零错误码,
  回读时合集区仍是「选择合集」;
- 活动「关联」按钮**首次点击静默失效**:无 toast、**零网络请求**、按钮文案不翻转,
  只读诊断已排除禁用态,用完全相同的方式再点一次立刻成功。

所以本模块**任何一步都不拿"没报错"当成功凭据**:每一项设置完当场读回 DOM 确认,提交
之后再重进页面逐项回读,没生效的如实报 ``partially_applied`` 并列出哪项没成。

其余硬约束(每条都是实测踩出来的):

- **绝不自己构造那个 PUT**(设计 2.4):载荷含 ``metadata.history_id`` /
  ``capa_trace_info.contextJson`` / ``source`` / 图片 ``file_id`` 等编辑页会话态数据,
  拼不出来;且提交是**全量覆盖语义**,漏 ``privacy_info`` 可能把私密笔记变公开。
  正确姿势是走 UI 让前端自己序列化。
- **权限保全**(设计 3.4):编辑前只读留底权限档位,点发布前再读一次、不符就中止**不点**,
  提交后回读确认没变;变了立刻改回并大声告警。用户名下有 28 篇刻意隐藏的私密笔记。
- **发布按钮是 closed shadow DOM**(设计 2.5):``shadowRoot === null``,任何选择器都穿
  不透。只能取 host 矩形 → 像素带内按小红书红求质心 → ``elementFromPoint`` 复核 → 拟人
  点坐标。**不得写死坐标**:组件设置会改变页面高度顶动按钮,每次重算。
- **活动是互斥单选,但旧活动注入正文的话题不回收**(设计 2.7③):故重试只重试**同一个
  活动**、且有次数上限,**绝不做"换个活动重试"**——反复切换会让话题单调累积并真发出去。
- **绝不点删除、绝不点「取消关联」**:所有会撤销/删除的按钮在本模块里只被**读**,读到就当
  "已是目标态"跳过,永远不点。合集的 ``.close-icon``(移除合集)2026-08-07 起**开了一个
  受控例外**:只有 ``_remove_collection``(调用方显式请求移出时才跑的那一步)可以点它,
  且必须以 ``.collection-plugin-choose`` 为容器 scope + 名字比对通过 + 点完当场回读;
  ``_set_collection``(加入路径)对它的"绝不点"一字不改。
- 全程 ``SyncHumanActions``;``page.evaluate`` **只用于只读取证**,不做任何 JS 合成点击
  或 JS 设值。等待用 ``page.wait_for_timeout``(同步 API 下 ``time.sleep`` 期间 response
  监听器一个都不会触发,见 ``creator_note_list`` 模块 docstring)。
"""

import json
import os
import re
import time
from io import BytesIO
from typing import Any, Dict, List, Optional

from loguru import logger

from app.browser.atomic_tasks import XHS_MAX_TOPICS
from app.browser.creator_export import _goto_creator
# 已发布笔记编辑的两个实现模块(T4/T5)。**单向依赖**:它们绝不 import 本模块(会成环),
# 编排点在本文件 —— 这正是设计第二节那条"结构性修正"(新逻辑不再堆进本已 1541 行的文件,
# 但提交路径仍然只有这里一条)。``add_images`` / ``remove_images`` 改名 import:
# ``set_note_components`` 的同名入参会在函数体里把模块级函数遮住。
from app.browser.note_editing import (
    append_topics,
    apply_content_edit,
    apply_title_edit,
    content_prefix_ok,
    image_count_equation,
    plan_topic_appends,
    read_body_value,
    read_title_value,
)
from app.browser.note_editing_images import (
    add_images as add_images_step,
    count_images,
    image_gate,
    remove_images as remove_images_step,
)
from app.browser.sync_human_actions import SyncHumanActions

# 更新页深链(设计 2.1,三次独立验证成功)。**优先用它定位**,不走"笔记管理页悬停点第 3
# 个图标"那条路 —— 那条路上①权限设置与③编辑的 class 完全相同、④是删除,调研期间已真的
# 弹出过删除确认框。
_UPDATE_URL = "https://creator.xiaohongshu.com/publish/update?id={note_id}&noteType=normal"
_PUBLISH_URL = "https://creator.xiaohongshu.com/publish/publish?source=official"

# 编辑页是骨架屏,networkidle 不够,轮询等「内容设置」文案出现(实测 1.5-2s)
_EDITOR_READY_TEXT = "内容设置"
_EDITOR_READY_TIMEOUT_S = 25.0

# 组件选择器(设计 2.8,真号实测)
_COLLECTION_BUTTON = ".collection-plugin-button"
_COLLECTION_POPOVER_ITEM = (
    ".collection-plugin-popover .collection-plugin-popover-content > .item"
)
_COLLECTION_CHOSEN = ".collection-plugin-choose"
_COLLECTION_EMPTY_TEXT = "选择合集"
# 移出合集的 × (2026-08-07 运营需求 P0)。**必须以 .collection-plugin-choose 为容器 scope**:
# 裸 .close-icon 在页面上不唯一(引用区 / 图片卡都可能有同名类),而这是唯一一个会把笔记
# 从合集里摘出去的按钮。它**只在悬停 chip 之后才渲染**(用户 2026-08-07 编辑页实拍),
# 静态 dump 里查不到 —— 所以"查不到 ×"既是失败,也是"这篇不在合集里"的辅助反证。
_COLLECTION_CLOSE_ICON = f"{_COLLECTION_CHOSEN} .close-icon"
# 通用弹窗容器:点 × 之后**有没有确认弹窗是未验证点**(设计 docs/design/2026-08-07-collection-remove-design.md §6-2,取证轮未跑到),
# 故只用它做 fail-loud 探测,绝不按文案猜哪个按钮是"确认"。
_ANY_MODAL = ".d-modal"
_QUOTE_CONTAINER = ".quote-note-container"
_QUOTE_MODAL = ".d-modal.select-note-modal"
_QUOTE_NOTE_CARD = ".d-modal.select-note-modal .select-note-modal__note-grid > .note-card"
# 候选列表**真正的滚动容器**(quote_modal_lazyload 夹具实测:``overflow-y:auto``,
# 矩形 x=423 y=184 754×424,``scrollHeight`` 随懒加载 586→1575 增长)。滚轮落点必须打进
# 它的矩形里 —— 卡片、网格、``d-modal-content`` 全都不滚,只有它滚。
_QUOTE_LIST_WRAP = ".d-modal.select-note-modal .select-note-modal__list-wrap"
# 平台 toast:点候选卡被拒时弹出来的那条(夹具原文 class 含 ``d-toast-icon-danger``)。
# ``_QUOTE_TOAST`` 是根容器 —— 它会把多条通知**累加**进同一个节点,文案因此重复;
# 干净的单条文案在 ``_QUOTE_TOAST_TEXT`` 那个叶子上,故优先读叶子。
_QUOTE_TOAST = ".d-new-toast"
_QUOTE_TOAST_TEXT = ".d-new-toast .d-toast-description"
# 拒绝语义的判据只认这四个字。夹具实测原文是「非公开可见笔记,无法引用」,但把整句写死
# 会被平台换一个字就绕过;而「无法引用」四个字就是这条 toast 的语义本身。
_QUOTE_TOAST_REJECT_MARK = "无法引用"
_QUOTE_CONFIRM_TEXT = "确认引用"
# 「确认引用」的**禁用态判据**(quote_modal 夹具实测:未选中任何卡时按钮就是禁用的)。
# 夹具里那颗按钮的属性原文::
#
#     disabled=""
#     class="d-button d-button-default disabled d-button-with-content --color-static
#            bold --color-bg-fill --color-text-disabled custom-button bg-red disabled
#            confirm-width"
#
# 两路取或、任一命中即不可点:① 有 ``disabled`` 属性;② ``class`` 按空白切分后含
# **独立 token** ``disabled``。与 ``podcast.create_button_state`` 同款纪律 ——
# **整词**比较而不是 substring(substring 判法会被将来任何 ``xxx-disabled`` 类名命中,
# 把按钮永久判死,那是比假绿更难查的反向故障)。
#
# 故意**不**把 ``--color-text-disabled`` 算进判据:它是跟随禁用态的配色类,平台若在解禁
# 时忘了摘掉它,就会把一颗能点的按钮判死。上面两条已是直接证据,不需要第三条推测。
_QUOTE_DISABLED_TOKEN = "disabled"
# 引用区**未设置**时的占位文案(夹具实测)。与 _COLLECTION_EMPTY_TEXT 同性质:
# 判"到底有没有设上"要认空态,不能只靠"跟之前比变了没有"——重复设同一篇时前后一样,
# 拿变化当判据会把幂等重跑判成失败。
_QUOTE_EMPTY_TEXT = "引用笔记"
# 关弹窗只能点它:Escape 关不掉这条产品线的弹窗(实测),见 _close_quote_modal
_QUOTE_CANCEL_TEXT = "取消"
# 「他人笔记」tab(2026-08-02 真号只读观察实测):
# - 切 tab 是**纯前端**,零网络请求;
# - 输入框 placeholder 写「请粘贴笔记链接 http://...」,但**直接填 note_id 就能检索到**
#   —— 不需要 xsec_token、不需要拼完整 URL(token 是账号绑定且短效的,拼了反而更脆);
# - 检索前候选区是空的,检索后才渲染候选卡。
_QUOTE_TAB_OTHER_TEXT = "他人笔记"
_QUOTE_LINK_INPUT = ".d-modal.select-note-modal input.d-text"
# 他人笔记检索接口(真号实测):GET .../creator/search/others/note?note_link=<note_id>
# 响应 data 里带 note_id / display_title / author_nick_name —— **有它就能精确校验**
# "检索到的确实是目标那篇",不必退化成"只有一张卡就认了"。
_OTHERS_SEARCH_API_MARK = "creator/search/others/note"
_ACTIVITY_CARD = ".activity-card"
_ACTIVITY_NAME = ".activity-name"
_ACTIVITY_ACTION = ".activity-action"
_ACTIVITY_LINKED_TEXT = "取消关联"
_ACTIVITY_UNLINKED_TEXT = "关联"
# 活动区自己的「更多」入口(设计 2.8 记载的真实选择器)。**必须收口在活动区内** ——
# 页面上推荐话题区也有个「更多」,纯文本匹配会点错、展开的是话题面板(设计 2.8 同名陷阱)。
_ACTIVITY_MORE_ENTRY = ".activity-plugin-label .more"
# 活动区容器候选:用来判「这个页型到底有没有活动区」(与区标题文案一起构成双判据)
_ACTIVITY_SECTION_CONTAINERS = (".activity-plugin-label", "[class*='activity-plugin']")
_ACTIVITY_SECTION_TEXT = "关联活动"
# 「更多活动」面板是懒加载列表:开面板后最多滚这么多轮找目标卡
_ACTIVITY_PANEL_SCROLLS = 8
_PERMISSION_DESC = ".permission-card-wrapper .d-select-description"
_PUBLISH_HOST = "xhs-publish-btn"
_TITLE_INPUT = "input[placeholder*='标题']"

# 弹层里那条「创建合集」:选择器已用 ``> .item`` 排除 .popover-footer,这里再按文案兜一道
# —— 点到它会**真的建一个新合集**,是本模块唯一会凭空造实体的误点,必须双保险。
_COLLECTION_CREATE_TEXT = "创建合集"

# 被动读的接口特征(设计 2.9;**只挂响应监听,请求由页面自己发**)
_ACTIVITY_API_MARK = "creator/activity_center/list"
_COLLECTION_API_MARK = "note/collection/pc/list_v2"
_POSTED_API_MARK = "creator/note/user/posted"
_UPDATE_API_MARK = "capa/postgw/note/update"

# 各步等待窗口(秒)。轮询步长固定,等的是接口响应/弹层渲染,不是拟人停顿。
_POPOVER_TIMEOUT_S = 10.0
_MODAL_TIMEOUT_S = 12.0
# 候选列表分页到齐的判定:连续这么久没有新页就收工(见 _wait_all_candidate_notes)
_PAGE_SETTLE_S = 1.5

# ── 引用候选列表的**主动翻页**(2026-08-13 生产 RCA)──
# 弹窗「我的笔记」列表是**懒加载**的:打开时只自己发头一两页,后面的页要滚到底才发。
# 原实现只**被动等**响应,于是候选永远停在第一页(生产实录:49 篇的号只见 12 篇),
# 排在深位的笔记必然被判「候选列表里没有」,再被降级门当成"别人的笔记"送进死路。
# 故这里主动在列表里拟人滚动翻页,直到目标出现 / 翻不动了 / 用满封顶轮数。
_QUOTE_SCROLL_ROUNDS = 10          # 封顶轮数(49 篇约 4-5 页,给足余量也不至于空转太久)
_QUOTE_SCROLL_IDLE_ROUNDS = 2      # 连续这么多轮既无新页也无新卡 → 判定已到底
# 每次滚动之后等下一页的窗口。**必须比 _MODAL_TIMEOUT_S 短得多**:滚到底之后本来就
# 没有下一页,拿开弹窗那次的 12 秒来等,两轮空滚就是 24 秒纯等待,每一次引用都白付。
_QUOTE_SCROLL_WAIT_S = 4.0
# 目标卡滚进候选列表可视区的尝试轮数。用满仍在区外就**判失败**(报 quote_card_offscreen),
# 绝不"尽力而为"照点 —— 区外那个坐标落在弹窗之外,点下去命中的是别的元素(2026-08-13 事故)。
_QUOTE_CARD_VIEW_TRIES = 3
# 滚它进来时"差多少滚多少"的补偿与下限。默认的随机 300~800 对 424 高的容器太粗:
# 一脚跨过头、下一轮再跨回来,三次重试全耗在来回震荡上 —— 而现在滚不进是**判失败**的,
# 震荡就成了假失败。``SyncHumanActions.scroll`` 分段变速(speed=1-0.5t),实际位移约为
# 请求值的四分之三,故乘个增益补回来;下限免得几像素的差值发出一串等于没动的滚轮。
_QUOTE_CARD_SCROLL_GAIN = 1.4
_QUOTE_CARD_SCROLL_MIN_PX = 120
# 点完候选卡后等「确认引用」解禁的窗口:选中态是纯前端翻转,给足一秒足够
_QUOTE_SELECT_SETTLE_S = 1.5
_CATALOG_TIMEOUT_S = 15.0
_ACTIVITY_FLIP_TIMEOUT_S = 8.0
_SUBMIT_TIMEOUT_S = 25.0

# 活动「关联」按钮的点击上限:实测**首次点击静默失效**(零网络请求、按钮不翻转),
# 第二次用完全相同的方式立刻成功。给到 3 次封顶——**只重试同一个活动**,绝不换活动
# (换活动会取消旧的,但旧活动注入正文的话题不回收,反复切换话题会单调累积并真发出去)。
_ACTIVITY_CLICK_ATTEMPTS = 3

# 活动卡懒渲染:找不到时最多下滚几轮再判定(文字版超长图会把活动区顶得极深,
# 首屏 DOM 里没有活动卡;滚到即止,不白滚)
_ACTIVITY_REVEAL_SCROLLS = 6

# 发布按钮像素定位:小红书红筛(与 atomic_tasks step7 同款阈值)
_RED_MIN_PIXELS = 50

# 活动筛选默认关键词(设计 2.10)。**不要**加"自我""成长"这类过宽的词:实测
# 「howto穿出自我」因"自我"命中,实为穿搭活动。活动频繁上下线,**绝不做死名单**。
DEFAULT_ACTIVITY_KEYWORDS = (
    "心理", "情绪", "焦虑", "抑郁", "疗愈", "精神", "认知", "内耗",
)

# 正文里的话题实体形如 ``#身边的心理学[话题]#``(设计 2.7),从提交前后的正文差里提取
_TOPIC_PATTERN = re.compile(r"#([^#\[\]]+)\[话题\]#")

# 破坏性编辑步的 applied 键(编辑设计 3.2),**元组顺序就是执行顺序**(编辑设计 4.2:
# 图片在文本之前——最可能失败的先做,弃提交尽早发生;正文在活动之前——活动往正文末尾
# 追加话题,反过来 Ctrl+A 会把刚注入的话题一并清掉)。
_EDIT_STEP_KEYS = ("image_remove", "image_add", "title", "content")
_IMAGE_STEP_KEYS = ("image_remove", "image_add")
# 弃提交时"因前序失败压根没执行"的项(编辑设计 4.4):与"执行了但失败"必须能区分开,
# 否则调用方会以为我们真的动过它。
_SKIPPED_REASON = "skipped_due_to_abort: 前序破坏性编辑步失败,本步一次都没执行"


class NoteComponentsError(Exception):
    """三组件设置的**前置/硬**失败(页面进不去、权限读不出、权限被改等)。

    ``reason`` 携失败语义。单个组件设置失败**不走这里**——那属于"部分生效",
    收在结果的 ``failed`` 里如实上报,不打断其余组件。
    """

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def _norm(text: Optional[str]) -> str:
    """空白归一(换行/多空格 → 单空格),便于文案精确比对。"""
    return " ".join((text or "").split())


# ---------------- 被动响应收集(零构造请求) ----------------


class ComponentResponses:
    """``page.on("response")`` 回调:被动收三组件相关接口的响应体。

    与 ``creator_note_list._PostedCollector`` 同款纪律:响应体必须在回调里当场读
    (导航之后 body 就取不到了);任何解析异常只告警丢弃,绝不让监听器抛异常打断页面
    事件派发。**只读不发**——一个请求都不构造。
    """

    _MARKS = (
        _ACTIVITY_API_MARK,
        _COLLECTION_API_MARK,
        _POSTED_API_MARK,
        _UPDATE_API_MARK,
        _OTHERS_SEARCH_API_MARK,
    )

    def __init__(self) -> None:
        self.bodies: Dict[str, List[dict]] = {mark: [] for mark in self._MARKS}
        self._page = None

    def handle(self, response) -> None:
        try:
            url = response.url or ""
        except Exception:  # noqa: BLE001 — 响应对象已失效,读 url 都会炸
            return
        mark = next((m for m in self._MARKS if m in url), None)
        if mark is None:
            return
        try:
            body = response.json()
        except Exception as exc:  # noqa: BLE001 — 读不到 body 只丢这条
            logger.warning(f"[note_components] 响应体读取失败(忽略) {mark}: {exc}")
            return
        if isinstance(body, dict):
            self.bodies[mark].append(body)

    def attach(self, page) -> None:
        """挂监听(幂等:同一实例只挂一次,换 page 时先摘旧的)。"""
        if self._page is page:
            return
        self.detach()
        page.on("response", self.handle)
        self._page = page

    def detach(self) -> None:
        """摘监听:同一个 page 会被后续任务复用,留着会继续吃响应体。"""
        if self._page is None:
            return
        try:
            self._page.remove_listener("response", self.handle)
        except Exception:  # noqa: BLE001 — 摘监听失败不影响已收到的结果
            logger.warning("[note_components] 摘除 response 监听失败(忽略)")
        self._page = None

    def latest(self, mark: str) -> Optional[dict]:
        """该接口最后一次响应体;一次都没收到返回 None。"""
        got = self.bodies.get(mark) or []
        return got[-1] if got else None

    def count(self, mark: str) -> int:
        return len(self.bodies.get(mark) or [])


def _wait_body(
    page, responses: ComponentResponses, mark: str, timeout_s: float, seen: int
):
    """等该接口收到**比 seen 更多**的响应(用 ``page.wait_for_timeout`` 让事件真被派发)。

    ``seen`` 必须由调用方在**触发动作之前**取好。若在这里现取,响应恰好在"点击返回"与
    "开始等待"之间到达时,基线就已经把它算进去了,于是白等一整个超时窗口才返回 None
    —— 那正是"点了、其实成了、却报没响应"的静默误判。
    """
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if responses.count(mark) > seen:
            return responses.latest(mark)
        page.wait_for_timeout(300)
    return responses.latest(mark) if responses.count(mark) > seen else None


# ---------------- 接口响应解析(纯函数) ----------------


def parse_collections(body: Optional[dict]) -> List[Dict[str, Any]]:
    """``list_v2`` 响应 → ``[{id, name, desc, note_num}]``;读不出结构返回 []。

    ``data.collection_info_list`` 是设计 2.9 实测的键名,不做多键猜测。
    """
    raw = ((body or {}).get("data") or {}).get("collection_info_list")
    if not isinstance(raw, list):
        return []
    out = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        cid = str(item.get("id") or "").strip()
        if not cid:
            continue
        out.append({
            "id": cid,
            "name": _norm(item.get("name")),
            "desc": _norm(item.get("desc")),
            "note_num": item.get("note_num"),
        })
    return out


# 活动项里"名字"与"简介"各自的候选键名。设计只实测到 ``extraInfo.name``(活动名),
# 列表项的字段名**没有实测记录**,故按候选键取值而不是硬认一个;取不到就是空串,
# 筛选时该活动只按拿得到的文本判定(宁可漏筛也不瞎猜键名)。
_ACTIVITY_NAME_KEYS = ("name", "activity_name", "activityName", "title")
_ACTIVITY_DESC_KEYS = (
    "desc", "description", "activity_desc", "intro", "introduction",
    "sub_title", "subTitle", "summary", "brief",
)
# 活动列表在 data 下的候选键名(同样未实测,按常见键名试,再退回"第一个字典列表")
_ACTIVITY_LIST_KEYS = (
    "list", "activities", "activity_list", "activityList", "items",
    "activity_info_list", "activity_infos",
)


def _pick(raw: dict, keys) -> str:
    """按候选键顺序取第一个非空字符串值(归一化);都没有返回空串。"""
    for key in keys:
        value = raw.get(key)
        if isinstance(value, str) and value.strip():
            return _norm(value)
    return ""


def _activity_rows(body: Optional[dict]) -> List[dict]:
    """从 ``activity_center/list`` 响应里挖出活动数组(键名未实测,故按候选 + 兜底扫描)。"""
    data = (body or {}).get("data")
    if isinstance(data, list):
        return [x for x in data if isinstance(x, dict)]
    if not isinstance(data, dict):
        return []
    for key in _ACTIVITY_LIST_KEYS:
        value = data.get(key)
        if isinstance(value, list) and any(isinstance(x, dict) for x in value):
            return [x for x in value if isinstance(x, dict)]
    # 兜底:按键名字典序扫第一个"字典列表",并把用了哪个键记进日志(便于事后校准键名)
    for key in sorted(data.keys()):
        value = data.get(key)
        if isinstance(value, list) and any(isinstance(x, dict) for x in value):
            logger.info(f"[note_components] 活动列表取自兜底键 data.{key}")
            return [x for x in value if isinstance(x, dict)]
    return []


def parse_activities(body: Optional[dict]) -> List[Dict[str, Any]]:
    """``activity_center/list`` 响应 → ``[{id, name, desc}]``;无 id 的项丢弃。

    ``id`` 统一转字符串:提交载荷里 ``ACTIVITY_COMPONENT.bizId`` 实测是字符串 ``"43561"``,
    而列表里可能是数字,两边比对必须同型。
    """
    out = []
    for raw in _activity_rows(body):
        aid = raw.get("id") or raw.get("activity_id") or raw.get("activityId")
        if isinstance(aid, bool) or aid is None or str(aid).strip() == "":
            continue
        out.append({
            "id": str(aid).strip(),
            "name": _pick(raw, _ACTIVITY_NAME_KEYS),
            "desc": _pick(raw, _ACTIVITY_DESC_KEYS),
        })
    return out


def match_keywords(activity: Dict[str, Any], keywords) -> List[str]:
    """活动命中了哪些关键词:**name + 简介联合**匹配(设计 2.10)。

    只查 name 会假阳性(实测「howto穿出自我」因"自我"命中,实为穿搭活动)——那个坑靠
    **窄关键词表**堵,联合简介是为了不漏掉"名字没提心理、简介在讲心理"的活动。
    """
    text = f"{activity.get('name') or ''} {activity.get('desc') or ''}"
    return [k for k in keywords if k and k in text]


def filter_activities(
    activities: List[Dict[str, Any]], keywords=DEFAULT_ACTIVITY_KEYWORDS
) -> List[Dict[str, Any]]:
    """按关键词筛活动,给每条附 ``matched_keywords``(让调用方看得见凭什么命中)。

    ``keywords`` 为空(None / 空序列)时**不过滤**,原样返回并附空 matched 列表。
    """
    if not keywords:
        return [{**a, "matched_keywords": []} for a in activities]
    out = []
    for activity in activities:
        matched = match_keywords(activity, keywords)
        if matched:
            out.append({**activity, "matched_keywords": matched})
    return out


def extract_topics(text: str) -> List[str]:
    """从正文里提取话题实体名(``#身边的心理学[话题]#`` → ``身边的心理学``)。"""
    return [m.strip() for m in _TOPIC_PATTERN.findall(text or "") if m.strip()]


def appended_part(before: str, after: str) -> str:
    """正文的**追加**部分:关联活动是把话题空格分隔拼在末尾(设计 2.7①,实测追加不覆盖)。

    ``after`` 不是以 ``before`` 开头时(说明不只是追加)返回整段 after,交调用方如实上报。
    """
    before = before or ""
    after = after or ""
    if after.startswith(before):
        return after[len(before):].strip()
    return after.strip()


# ---------------- 页面就绪 / 只读取证 ----------------


def open_update_page(page, account_id: int, note_id: str) -> None:
    """深链进更新页并等编辑器真就绪(骨架屏 → 轮询等「内容设置」文案)。

    两轮:首访被重定向到登录页时用 publish 页预热 SSO 再重进,与
    ``note_visibility._open_note_manager`` 同款 fast-path。两轮都不就绪按未登录处理。
    """
    url = _UPDATE_URL.format(note_id=note_id)
    for attempt in (1, 2):
        if attempt == 2:
            logger.info(
                f"[note_components] 账号{account_id}: 更新页未就绪,"
                f"走 publish_url 预热 SSO 后重进"
            )
            _goto_creator(page, _PUBLISH_URL)
        _goto_creator(page, url)
        if _wait_editor_ready(page):
            logger.info(f"[note_components] 账号{account_id}: 更新页就绪 note_id={note_id}")
            return
    raise NoteComponentsError(
        f"editor_not_ready: 更新页始终没渲染出「{_EDITOR_READY_TEXT}」"
        f"(note_id={note_id};creator 域可能需重新扫码登录,或该 note_id 不属于本号)"
    )


def _wait_editor_ready(page) -> bool:
    """轮询等编辑器就绪:``networkidle`` 不够(骨架屏),只认「内容设置」文案出现。"""
    deadline = time.monotonic() + _EDITOR_READY_TIMEOUT_S
    while time.monotonic() < deadline:
        try:
            if _EDITOR_READY_TEXT in (page.inner_text("body") or ""):
                return True
        except Exception:  # noqa: BLE001 — 导航中读 body 会炸,下一跳再读
            pass
        page.wait_for_timeout(400)
    return False


# 只读取证:正文当前文本(关联活动会往正文追加话题,提交前后各读一次做差)
_BODY_TEXT_JS = r"""
() => {
    const sels = ["div[contenteditable='true'][data-placeholder*='正文']",
                  "div[contenteditable='true'][placeholder*='正文']",
                  "textarea[placeholder*='正文']",
                  "div[contenteditable='true']"];
    for (const s of sels) {
        const el = document.querySelector(s);
        if (el) return (el.innerText || el.value || '').trim();
    }
    return null;
}
"""


def read_body_text(page) -> str:
    """只读回正文文本;读不出返回空串(不为此中止,交差值上报"没观察到")。"""
    try:
        return _norm(page.evaluate(_BODY_TEXT_JS)) or ""
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[note_components] 正文读取失败(忽略): {exc}")
        return ""


def read_permission_label(page) -> Optional[str]:
    """只读回权限档位文案(``.permission-card-wrapper .d-select-description``)。

    读不出返回 None —— 调用方**必须**把 None 当"权限不可确认"中止,不得当"没变"放行:
    提交是全量覆盖语义,权限档位是这条链路唯一不可逆的东西。
    """
    try:
        el = page.query_selector(_PERMISSION_DESC)
    except Exception:  # noqa: BLE001
        return None
    if el is None:
        return None
    text = _norm(el.inner_text())
    return text or None


def read_note_title(page) -> str:
    """只读回编辑器里的标题(平台**当前**标题,比台账 title 权威)。

    设计 3.2:平台上「粤语咨询师-黄安麟…」在台账里是「心理咨询师-…」,台账 title 会过期。
    这里读到的是这一刻平台的真值,权限回滚需要按标题定位卡片时用它。
    """
    try:
        el = page.query_selector(_TITLE_INPUT)
        if el is None:
            return ""
        return _norm(el.input_value())
    except Exception:  # noqa: BLE001
        return ""


# ---------------- 组件当前态回读(逐项) ----------------


def read_collection_label(page) -> Optional[str]:
    """已加入的合集名;未加入返回 None。

    优先读 ``.collection-plugin-choose``(已加入时的展示区,内含**绝不能点**的
    ``.close-icon``);没有它就看入口按钮文案,是「选择合集」即未加入。
    """
    chosen = page.query_selector(_COLLECTION_CHOSEN)
    if chosen is not None:
        text = _norm(chosen.inner_text())
        if text:
            return text
    btn = page.query_selector(_COLLECTION_BUTTON)
    if btn is None:
        return None
    text = _norm(btn.inner_text())
    if not text or _COLLECTION_EMPTY_TEXT in text:
        return None
    return text


def read_quote_text(page) -> str:
    """引用区当前文本(空态文案未实测,故不判"空",只把原文交给调用方做包含判定)。"""
    el = page.query_selector(_QUOTE_CONTAINER)
    return _norm(el.inner_text()) if el is not None else ""


def _find_activity_card(page, activity_name: str):
    """按 ``.activity-name`` 文案在 ``.activity-card`` 里定位活动卡;找不到返回 None。

    **按卡片取同卡的 ``.activity-action``**,天然避开 ``.activity-plugin-label .more``
    那个同名陷阱(页面上另有推荐话题区的「更多」,纯文本匹配会命中错的)。
    """
    target = _norm(activity_name)
    if not target:
        return None
    for card in page.query_selector_all(_ACTIVITY_CARD):
        try:
            name_el = card.query_selector(_ACTIVITY_NAME)
            if name_el is not None and _norm(name_el.inner_text()) == target:
                return card
        except Exception:  # noqa: BLE001 — 单张卡读失败只跳过它
            continue
    return None


def read_components_snapshot(page, account_id: int, note_id: str) -> Dict[str, Any]:
    """**只读**打开更新页,读回该笔记当前的组件状态快照;零点击零修改。

    为什么要有它(2026-08-04 运营 P0-1):引用与合集在正文里没有任何痕迹,调用方除了
    人工开 App 逐条看**没有任何程序化验证手段**;而台账 published_notes 压根没有
    quoted_note_id / collection_id 列(运营误读的是 publish_jobs 的请求参数列)。
    8 月计划 360 篇每篇都要挂引用+合集,必须能程序化自证。

    读的都是本模块既有的只读 helper,与 set_note_components 的回读同一套口径:

    - ``quote_text`` 原文 + ``quote_set`` 判读(空态文案「引用笔记」= 未设置;
      已设置时平台显示「引用 @作者 的笔记」,**不含被引笔记标题**,故只能判有无、
      判不了"引的是不是那一篇"——那由设置时的选卡阶段保证);
    - ``collection_label`` 原文 + ``collection_set`` 判读(空态「选择合集」)+
      ``collection_entry_present``(页面有没有「加入合集」入口——2026-08-04 起两个
      账号实测入口消失,这个布尔就是 P1-1 的定位器);
    - ``topics``:正文里的话题实体(``#xx[话题]#``),活动是否生效也可由此侧写;
    - ``image_count`` / ``permission`` / ``title`` / ``body_head``。
    """
    open_update_page(page, account_id, note_id)
    quote_text = read_quote_text(page)
    collection_label = read_collection_label(page)
    body = None
    try:
        body = read_body_text(page)
    except Exception as exc:  # noqa: BLE001 — 单项读不出不拖垮整个快照
        logger.warning(f"[note_components] 快照读正文失败(留空): {exc}")
    return {
        "note_id": note_id,
        "title": read_note_title(page),
        "permission": read_permission_label(page),
        "quote_text": quote_text,
        "quote_set": bool(_norm(quote_text)) and _norm(quote_text) != _QUOTE_EMPTY_TEXT,
        "collection_label": collection_label,
        "collection_set": bool(_norm(collection_label or ""))
        and _COLLECTION_EMPTY_TEXT not in (collection_label or ""),
        "collection_entry_present": page.query_selector(_COLLECTION_BUTTON) is not None,
        "topics": extract_topics(body),
        "image_count": len(page.query_selector_all(
            ".img-upload-area .img-container"
        )),
        "body_head": _norm(body or "")[:80],
    }


def read_activity_action_text(page, activity_name: str) -> Optional[str]:
    """该活动卡上按钮的当前文案(「关联」/「取消关联」);卡或按钮找不到返回 None。"""
    card = _find_activity_card(page, activity_name)
    if card is None:
        return None
    action = card.query_selector(_ACTIVITY_ACTION)
    return _norm(action.inner_text()) if action is not None else None


def classify_activity_action(action_text: Optional[str]) -> str:
    """活动按钮文案 → ``linked`` / ``unlinked`` / ``unknown``(纯函数)。

    **两种上下文文案不同**:内联活动区是「关联」/「取消关联」,「更多活动」面板里是
    「关联活动」/「取消关联活动」。上线前是裸相等判断 ``!= "关联"``,面板里那颗按钮
    会被判成"文案异常,拒绝点击"—— 永远关联不上。

    判定顺序是硬要求:**先判「取消」**。「取消关联活动」里含有「关联活动」,反过来判
    会把已关联误读成未关联,后果不是白跑一趟,是**点掉「取消关联」**(本模块最硬的红线)。
    读不出 / 陌生文案一律 ``unknown``,调用方据此一次都不点。
    """
    text = _norm(action_text or "")
    if not text:
        return "unknown"
    if text.startswith("取消") and _ACTIVITY_UNLINKED_TEXT in text:
        return "linked"
    if text in (_ACTIVITY_UNLINKED_TEXT, f"{_ACTIVITY_UNLINKED_TEXT}活动"):
        return "unlinked"
    return "unknown"


def consent_ticked_from_simulator_class(simulator_class: Optional[str]) -> bool:
    """协议复选框勾上了没(纯函数):模拟器元素的 class 里**没有** ``unchecked`` 才算勾上。

    为什么不读隐藏 ``input.checked``:探针实测那个 input 的 rect 是 0×0(拿不到也点不着),
    与「原创声明」大开关同一套路 —— 这套组件库把真实状态放在模拟器元素的 class 上。
    读不到 class(None)一律算**没勾上**:读不到 ≠ 好了。
    """
    if not simulator_class:
        return False
    return "unchecked" not in simulator_class


def probe_activity_section(page) -> Dict[str, Any]:
    """回读「关联活动」区的存在性证据:卡片数 + 容器选择器 + 区标题文案。

    **三样一起读、双判据判存在**(容器选择器 or 区标题文案),只认其中一样都会误判:
    - 只认文案:图文页与视频页的标题文案若不同、或平台改文案,存在的区会被判成"不存在",
      运营据此以为平台没这功能;
    - 只认卡片数:推荐位为空(0 张卡)时同样会把存在的区判成不存在。
    """
    cards = 0
    container = False
    try:
        cards = len(page.query_selector_all(_ACTIVITY_CARD))
    except Exception:  # noqa: BLE001 — 归因辅助,读不到就当没有,绝不制造新异常
        pass
    for selector in _ACTIVITY_SECTION_CONTAINERS:
        try:
            if page.query_selector(selector) is not None:
                container = True
                break
        except Exception:  # noqa: BLE001
            continue
    try:
        section_text = _ACTIVITY_SECTION_TEXT in (page.inner_text("body") or "")
    except Exception:  # noqa: BLE001
        section_text = False
    return {"cards": cards, "container": container, "section_text": section_text}


def explain_activity_card_missing(activity_name: str, observed: Dict[str, Any]) -> str:
    """目标活动卡找不到时的失败原因文案(纯函数,两种成因分开说)。

    为什么必须分开:平台 2026-08-03 把编辑页的活动区整个收走过,而视频笔记页的活动区
    是 2026-08-07 用户实拍才确认存在的(此前的探针 fixtures 没采到)。两种情形都走
    "告警不阻断发布",但运营的下一步动作完全相反 —— 一个是"这个页型可能压根没有活动区,
    带 job_id 上报",一个是"活动下线了,重新拉活动列表"。混成一句话等于让运营猜。

    判 ``activity_section_absent`` 要求**卡片、容器、区标题文案三样全空**(双判据,
    见 ``probe_activity_section``);任一命中即认为区在,报 ``activity_card_not_found``
    并说清「更多」面板试过没有 —— 目标活动不在推荐位时正是要靠面板才找得到。
    """
    cards = int(observed.get("cards") or 0)
    scrolls = observed.get("scrolls", _ACTIVITY_REVEAL_SCROLLS)
    section_present = bool(
        cards > 0 or observed.get("container") or observed.get("section_text")
    )
    if not section_present:
        return (
            f"activity_section_absent: 活动卡、活动区容器、区标题文案三样都读不到"
            f"(已下滚 {scrolls} 轮触发懒渲染),疑该页型没有「关联活动」这个设置区。"
            f"活动没设上,但笔记照发;要「{activity_name}」真挂上请带 job_id 上报"
        )
    if observed.get("panel_opened"):
        panel_note = "「更多活动」面板已打开并滚动查找过,里面也没有它"
    elif observed.get("more_entry"):
        panel_note = "找到了「更多」入口但没能打开面板"
    else:
        panel_note = "活动区里没有「更多」入口可点,只查了内联推荐位"
    return (
        f"activity_card_not_found: 活动区在(内联 {cards} 张卡)但没有名为"
        f"「{activity_name}」的那张;{panel_note}(已下滚 {scrolls} 轮触发懒渲染)。"
        f"活动频繁上下线,请重新拉取活动列表确认它还在"
    )


def _find_activity_more_entry(page):
    """定位活动区自己的「更多」入口;找不到返回 None。

    **只在活动区内找**:先试设计 2.8 记载的 ``.activity-plugin-label .more``,
    再退到活动卡最近祖先子树内文案为「更多」的元素(用 JS **只读**求坐标,点击仍走
    拟人层)。两条路都锚在活动区上 —— 页面上推荐话题区也有个「更多」,一次全页
    ``text=更多`` 匹配会点错、展开的是话题面板。
    """
    try:
        entry = page.query_selector(_ACTIVITY_MORE_ENTRY)
    except Exception:  # noqa: BLE001
        entry = None
    if entry is not None:
        return entry
    # 兜底:从活动卡往上找 ≤4 层祖先,在**仍包含该活动卡**的子树里找「更多」。
    # 起点是活动卡本身,所以话题区天然不在搜索范围内(同名陷阱的结构性规避)。
    try:
        box = page.evaluate(r"""() => {
            const card = document.querySelector('.activity-card');
            if (!card) return null;
            let node = card;
            for (let i = 0; i < 4 && node.parentElement; i++) {
                node = node.parentElement;
                if (!node.contains(card)) break;
                for (const el of node.querySelectorAll('*')) {
                    if (el.contains(card)) continue;
                    if (!/^更多\s*[>》›]?$/.test((el.textContent || '').trim())) continue;
                    const r = el.getBoundingClientRect();
                    if (r.width > 0 && r.height > 0) {
                        return {x: r.x + r.width / 2, y: r.y + r.height / 2};
                    }
                }
            }
            return null;
        }""")
    except Exception:  # noqa: BLE001
        box = None
    if isinstance(box, dict):
        return (box["x"], box["y"])
    return None


def _activity_linked(page, activity_name: str) -> bool:
    """该活动是否已关联 —— 判据是按钮文案翻转成「取消关联」(实测唯一可靠信号)。

    走 ``classify_activity_action`` 而非裸相等:面板里的文案是「取消关联活动」。
    """
    return classify_activity_action(
        read_activity_action_text(page, activity_name)) == "linked"


def _reveal_in_more_panel(page, human, activity_name: str) -> tuple:
    """点开「更多活动」面板,在里面(滚动懒加载)找目标活动卡。

    返回 ``(按钮文案 或 None, 过程证据 dict)``。证据里的 ``more_entry`` / ``panel_opened``
    供失败归因用 —— **必须在这里记**:面板一打开就可能盖住「更多」入口,事后再探一次会
    读到 None,把"面板试过了"错报成"压根没入口可点"。

    为什么必须有(2026-08-07 用户实拍推翻旧假设):内联活动区只渲染约 2 张**推荐**卡,
    目标活动不在推荐位就永远 ``activity_card_not_found`` —— 这大概率就是此前
    「活动挂不上」的真根因(当时归因成平台侧问题,是错的)。

    面板里滚动前**先把鼠标悬到面板上**:``human.scroll`` 走 ``mouse.wheel``,滚轮事件
    落在光标当前位置,光标还停在「更多」入口上就滚不动这个独立滚动容器。

    找不到入口直接返回 None(不点任何东西);面板开了但滚完仍没有,同样返回 None,
    由调用方统一归因 —— 这里绝不猜。
    """
    meta = {"more_entry": False, "panel_opened": False}
    entry = _find_activity_more_entry(page)
    if entry is None:
        logger.info(f"[note_components] 活动区里没有「更多」入口,不再深找「{activity_name}」")
        return None, meta
    meta["more_entry"] = True
    meta["panel_opened"] = True
    human.click(entry, reason=f"打开「更多活动」面板(找「{activity_name}」)")
    human.wait(0.8, 1.5, context="等更多活动面板渲染")
    for turn in range(_ACTIVITY_PANEL_SCROLLS + 1):
        action_text = read_activity_action_text(page, activity_name)
        if action_text is not None:
            logger.info(
                f"[note_components] ✓ 在「更多活动」面板里找到「{activity_name}」"
                f"(滚了 {turn} 轮),按钮文案={action_text!r}"
            )
            return action_text, meta
        if turn >= _ACTIVITY_PANEL_SCROLLS:
            break
        # 滚轮打在**光标当前位置**,不先把鼠标移进面板就是在滚主页面(旧仓踩过:
        # 初始光标在 (0,0) 的顶栏上,滚了等于没滚,表现成"翻两页就停")。
        # 面板容器的选择器没有实测证据,故不猜:取 DOM 里**最后一张**活动卡当悬停锚点
        # —— 面板是后挂上去的,它的卡排在内联区那几张之后。这是本次改动里证据最薄的一处,
        # e2e 若发现面板滚不动,先怀疑这个锚点。
        try:
            cards = page.query_selector_all(_ACTIVITY_CARD) or []
            if cards:
                human.hover(cards[-1])
        except Exception:  # noqa: BLE001 — 悬停失败就照常滚,最坏是这轮白滚
            pass
        human.scroll("down")
        human.wait(0.4, 0.9, context="等面板列表懒加载")
    logger.info(
        f"[note_components] 「更多活动」面板滚了 {_ACTIVITY_PANEL_SCROLLS} 轮仍没有"
        f"「{activity_name}」"
    )
    return None, meta


# ---------------- 单个组件的设置动作 ----------------


def _visible_modal_texts(page) -> List[str]:
    """当前页上**可见**的弹窗文案(只读探测,读不出就当没有)。

    只服务于移出合集那一步的 fail-loud:点 × 之后页面上冒出任何弹窗,都说明我们踩进了
    一个没被验证过的交互分支,此时**唯一正确的动作是停手并把原文交给人**。
    """
    texts: List[str] = []
    try:
        nodes = page.query_selector_all(_ANY_MODAL)
    except Exception:  # noqa: BLE001 — 探测失败不当成"有弹窗",URL/回读还有兜底
        return texts
    for node in nodes:
        try:
            if node.is_visible() and _norm(node.inner_text()):
                texts.append(_norm(node.inner_text())[:200])
        except Exception:  # noqa: BLE001
            continue
    return texts


def _remove_collection(
    page, human: SyncHumanActions, responses: ComponentResponses, collection_id: str,
    collection_name: str | None = None,
) -> Dict[str, Any]:
    """移出合集:确认当前所在合集就是目标 → **悬停 chip 让 × 显出来** → 点 × → 读回空态。

    与 ``_set_collection`` 对称的幂等语义(运营 2026-08-07 需求第三节 1):

    ==================== ============ ==========================================
    当前态                目标 C        结果
    ==================== ============ ==========================================
    空态(「选择合集」)   移出 C       ``skipped`` —— 本就不在,**零点击**
    在 C 里               移出 C       悬停 → 点 × → 回读空态 → ``done``
    在 D 里(D≠C,已比对) 移出 C       ``skipped``,reason 带出实际所在合集名
    已选但名字比对不了     移出 C       ``error``,**绝不点 ×**
    ==================== ============ ==========================================

    这是本模块唯一一处**被允许点 ``.close-icon``** 的地方(模块 docstring 那条"绝不点合集
    的 .close-icon"是加入路径的纪律,一字不改)。开这个受控例外的代价用四道闸补上:

    1. **名字比对不过绝不动手** —— 移出是破坏性操作,点错等于把笔记从**正确的**合集里
       摘出来;``collection_name`` 是主路径(已选态开不了弹层,拿不到 id→名映射)。比对
       判据是**全等**不是包含:同族合集名互为前缀时(「科普」/「科普合集」)包含判据会
       在笔记其实属于「科普合集」时通过,然后把它从那个**错误**的合集里摘掉、回读空态、
       报成功——静默的破坏。不全等但包含时 fail-loud 带出 chip 原文,零点击零提交;
    2. **选择器以 ``.collection-plugin-choose`` 为容器 scope** —— 裸 ``.close-icon`` 不唯一;
    3. **未验证的弹窗即停** —— 点 × 后是否有确认弹窗、是立即生效还是要提交才落地,都是
       设计 docs/design/2026-08-07-collection-remove-design.md §6 的未验证点(取证轮未跑到)。见到任何可见弹窗就抛 ``NoteComponentsError``
       中止**整单**:页面处于不可预期态,继续走下去会带着弹窗去点发布,而那是一次全量
       覆盖提交;
    4. **点完当场回读** —— chip 必须回到空态或至少不再含目标名,不然报 ``collection_not_removed``。

    Raises:
        NoteComponentsError: 点 × 前后出现未验证的弹窗(硬失败,调用方据此弃提交)。
    """
    target_name = _norm(collection_name or "")
    if not target_name:
        # 兜一次 list_v2 被动缓存 —— 已选态基本拿不到(弹层没开过就不会发这个接口),
        # 所以 collection_name 才是主路径,与 _set_collection 已选态同款取舍。
        catalog = parse_collections(responses.latest(_COLLECTION_API_MARK))
        hit = next((c for c in catalog if c["id"] == str(collection_id)), None)
        target_name = _norm(hit["name"]) if hit else ""

    chosen = read_collection_label(page)
    if not chosen or _COLLECTION_EMPTY_TEXT in chosen:
        return {"status": "skipped", "collection_id": str(collection_id),
                "name": target_name,
                "reason": "collection_already_absent: 该笔记本就不在任何合集里,零点击"}
    if not target_name:
        return {
            "status": "error",
            "reason": f"collection_remove_unverifiable: 该笔记在合集「{_norm(chosen)[:20]}」里,"
                      f"但无法确认它就是目标 id={collection_id}(已选态开不了弹层拿不到 "
                      "id→名映射);移出是破坏性操作,比对不上**绝不动手**——"
                      "请求里带 remove_collection_name 即可确认",
        }
    chosen_name = _norm(chosen)
    if chosen_name != target_name:
        if target_name in chosen_name:
            # 包含但不全等:要么是同族合集名(「科普」vs「科普合集」——此时笔记压根不在
            # 目标里,点下去就是从**错误**的合集里摘人),要么是 chip 文案带了我们没取证过
            # 的装饰字符(× 之类)。两者在这里分不开,而代价不对称,所以一律不动手。
            return {
                "status": "error", "collection_id": str(collection_id),
                "name": target_name,
                "reason": f"collection_remove_unverifiable: 合集区文案 {chosen_name[:40]!r} "
                          f"只是**包含**目标「{target_name}」而不全等;同族合集名互为前缀时"
                          f"(「科普」/「科普合集」)按包含动手等于把笔记从错误的合集里摘出来,"
                          f"移出是破坏性操作,比不到全等**绝不动手**,零点击零提交——"
                          f"请按合集区实际文案传 remove_collection_name",
            }
        return {
            "status": "skipped", "collection_id": str(collection_id), "name": target_name,
            "reason": f"collection_in_another_not_target: 该笔记在合集「{chosen_name[:20]}」里,"
                      f"不在目标「{target_name}」里 —— 本就不在目标合集,幂等语义下不算失败,"
                      f"一次都没点",
        }

    # 弹窗基线取在动手**之前**:判据是"点 × 之后**新**冒出来的弹窗",不是"页面上有弹窗"。
    # 编辑器页本就可能挂着别的(隐藏或常驻的)`.d-modal` 容器,拿"有没有"当判据会把这个
    # 能力整个卡死;而真正要拦的是那个**因为我们点了 × 才出现**的确认框。
    baseline_modals = _visible_modal_texts(page)
    if baseline_modals:
        logger.warning(
            f"[note_components] 移出前页面已可见弹窗 {baseline_modals},作为基线排除"
        )

    _scroll_row_to_mid_viewport(page, human, _COLLECTION_CHOSEN)
    chip = page.query_selector(_COLLECTION_CHOSEN)
    if chip is None:
        return {"status": "error",
                "reason": "collection_chip_vanished: 滚动后合集展示条不见了,拒绝盲点"}
    # × 是 hover 态才渲染的(实拍):不悬停就查不到它,更点不到
    human.hover(chip, reason=f"悬停合集「{target_name}」让移出的 × 显出来")
    human.wait(0.4, 0.9, context="等 hover 态的 × 渲染")
    close_icon = page.query_selector(_COLLECTION_CLOSE_ICON)
    if close_icon is None:
        return {
            "status": "error",
            "reason": f"close_icon_not_found_after_hover: 悬停合集展示条后仍查不到 "
                      f"{_COLLECTION_CLOSE_ICON};当时 chip 文案 {_norm(chosen)[:40]!r}"
                      f"(它也可能是「这篇其实不在合集里」的反证,请先用 "
                      "note-component-reads 核对当前状态)",
        }

    human.click(close_icon, reason=f"移出合集「{target_name}」")
    human.wait(0.6, 1.2, context="等移出生效")

    modals = [t for t in _visible_modal_texts(page) if t not in baseline_modals]
    if modals:
        # 未验证点:确认弹窗的形态/文案/按钮全没取证过。**绝不盲点**——猜错按钮的代价
        # 从"没移出"到"删了别的东西"都有可能。原文带出来给人看,补完取证再实现这一支。
        raise NoteComponentsError(
            f"collection_remove_unknown_modal: 点 × 后冒出未验证过的弹窗 {modals};"
            f"确认弹窗形态尚未取证(设计 docs/design/2026-08-07-collection-remove-design.md §6-2),**绝不盲点任何按钮**,整单中止不提交。"
            f"笔记是否已被移出未知,请人工核对后再决定"
        )

    current = read_collection_label(page)
    if current is not None and target_name in _norm(current):
        return {
            "status": "error",
            "reason": f"collection_not_removed: 点了 × 之后合集区仍是 {current!r},"
                      f"「{target_name}」没被摘掉",
            "observed": current,
        }
    return {"status": "done", "collection_id": str(collection_id), "name": target_name,
            "observed": current}


def _set_collection(
    page, human: SyncHumanActions, responses: ComponentResponses, collection_id: str,
    collection_name: str | None = None,
) -> Dict[str, Any]:
    """加入合集:点入口 → 等弹层 → 按名字点选 → **当场读回**是否真加上了。

    合集名由 ``list_v2`` 响应里的 id→name 映射得到(调用方传的是 id);映射拿不到就
    报错**不点**——按文案点一个不知道是不是它的条目,等于瞎猜。
    """
    # **先认已选态**(2026-08-04 P1-1 翻案):笔记已在某个合集里时,页面显示的是已选
    # 展示条(_COLLECTION_CHOSEN,如「咨询师简介」),「加入合集」按钮**本来就不渲染**。
    # 旧代码直接报 entry_not_found,把「已是目标态」误报成失败——运营建合集时把 9 篇
    # 选了进去,批量挂载全数撞上,还被归因成"账号玄学"。语义对齐活动的纪律:
    # 已选同一个合集 → skipped(绝不重复操作);已选**别的**合集 → 明确报错——
    # 换合集 = 先移出旧的(移除是「绝不点」红线 .close-icon),那是业务决策不是本函数
    # 能替用户做的。
    chosen = read_collection_label(page)
    if chosen and _COLLECTION_EMPTY_TEXT not in chosen:
        # 已选态的 id→名:优先用调用方随 payload 传的 collection_name(他们从
        # GET collections 本就有映射);其次 list_v2 被动缓存 —— 但注意 **list_v2 只在
        # 弹层打开时才发**(设计 2.9),已选态开不了弹层,缓存多半是空的,所以 name
        # 才是主路径。两样都没有 = 无法确认已选的是不是目标,如实报出让调用方判断。
        target_name = _norm(collection_name or "")
        if not target_name:
            catalog0 = parse_collections(responses.latest(_COLLECTION_API_MARK))
            target0 = next((c for c in catalog0 if c["id"] == str(collection_id)), None)
            target_name = _norm(target0["name"]) if target0 else ""
        if target_name and target_name in _norm(chosen):
            return {"status": "skipped", "collection_id": str(collection_id),
                    "name": target_name, "reason": "该笔记本就在这个合集里"}
        if target_name:
            return {
                "status": "error",
                "reason": f"collection_already_in_another: 该笔记已在合集「{_norm(chosen)[:20]}」"
                          f"里,不是目标「{target_name}」;换合集需先移出旧的,"
                          "那是移除类操作本函数绝不代做",
            }
        return {
            "status": "error",
            "reason": f"collection_chosen_unverifiable: 该笔记已在合集「{_norm(chosen)[:20]}」里,"
                      f"但无法确认是否即目标 id={collection_id}(已选态开不了弹层拿不到 id→名;"
                      "请求里带 collection_name 即可确认)。若名字就是你要的合集,视为已挂,"
                      "可用 note-component-reads 复核",
        }

    btn = page.query_selector(_COLLECTION_BUTTON)
    if btn is None:
        return {"status": "error", "reason": "collection_entry_not_found: 页面没有合集入口"}

    seen = responses.count(_COLLECTION_API_MARK)  # 基线必须在点击**之前**取
    _scroll_row_to_mid_viewport(page, human, _COLLECTION_BUTTON)
    btn = page.query_selector(_COLLECTION_BUTTON) or btn
    human.click(btn, reason="打开合集弹层")
    # 弹层打开才会发 list_v2(设计 2.9);等响应与等 DOM 各等一次,谁先到都不影响
    body = _wait_body(page, responses, _COLLECTION_API_MARK, _POPOVER_TIMEOUT_S, seen)
    catalog = parse_collections(body or responses.latest(_COLLECTION_API_MARK))
    if not catalog:
        return {
            "status": "error",
            "reason": "collection_catalog_unavailable: 没收到合集列表响应,无法把 "
                      f"collection_id={collection_id} 映射成合集名,拒绝盲点",
        }
    target = next((c for c in catalog if c["id"] == str(collection_id)), None)
    if target is None:
        return {
            "status": "error",
            "reason": f"collection_not_found: 该号合集列表里没有 id={collection_id}"
                      f"(现有 {[c['id'] for c in catalog]})",
        }

    name = target["name"]
    item = _wait_collection_item(page, name)
    if item is None:
        # 当场取证:弹层此刻实际渲染出的条目(RCA 2026-08-10 出轨贴:catalog 里有、DOM 没有,
        # 次日复查又有——平台瞬态,但当时没记条目清单,黑箱了一晚。**临时诊断字段**,
        # 瞬态机制坐实/排除即撤,保质期纪律同 poll_timeline。)
        items_seen = []
        for el in page.query_selector_all(_COLLECTION_POPOVER_ITEM):
            try:
                items_seen.append(_norm(el.inner_text())[:40])
            except Exception:  # noqa: BLE001 — 单条读不到只跳过
                continue
            if len(items_seen) >= 12:
                break
        return {
            "status": "error",
            "reason": f"collection_item_not_found: 弹层里没有文案精确等于「{name}」的条目",
            "items_seen": items_seen,
        }
    human.click(item, reason=f"选择合集「{name}」")
    human.wait(0.6, 1.2, context="等合集选中生效")

    current = read_collection_label(page)
    if current is None or name not in current:
        return {
            "status": "error",
            "reason": f"collection_not_applied: 点选后合集区仍是 {current!r},未加上「{name}」",
            "observed": current,
        }
    return {"status": "done", "collection_id": str(collection_id), "name": name}


def _wait_collection_item(page, name: str):
    """轮询等弹层渲染出文案精确等于 ``name`` 的条目;命中不唯一/超时返回 None。

    **严格排除「创建合集」**:选择器已用 ``> .item`` 把 ``.popover-footer`` 排除在外,
    这里再按文案兜一道 —— 点到它会真的建一个新合集,是唯一会凭空造实体的误点。
    """
    target = _norm(name)
    deadline = time.monotonic() + _POPOVER_TIMEOUT_S
    while time.monotonic() < deadline:
        hits = []
        for el in page.query_selector_all(_COLLECTION_POPOVER_ITEM):
            try:
                text = _norm(el.inner_text())
            except Exception:  # noqa: BLE001
                continue
            if _COLLECTION_CREATE_TEXT in text:
                continue
            if text == target:
                hits.append(el)
        if len(hits) == 1:
            return hits[0]
        if len(hits) > 1:
            logger.warning(
                f"[note_components] 合集弹层里「{target}」命中 {len(hits)} 条,拒绝猜"
            )
            return None
        page.wait_for_timeout(400)
    return None


# ── 设置行防遮挡滚动(2026-08-05 quote_probe 夹具实锤的根因修复)──
# 底部悬浮发布按钮 XHS-PUBLISH-BTN 是 closed-shadow 自定义元素,透明命中区远大于
# 红色可见钮;设置行落在视口底带时 elementFromPoint 命中它,行上的所有点击被静默吞掉
# ——quote_candidates_unavailable 11 例(跨3账号)与标本 6a707e9f 七连零反应全由此来。
# scroll_into_view_if_needed 帮不上忙:行"技术上可见"就不滚,而它恰好可见于底带;
# 855 高视口上它还会把视口外的行正好滚到底边=滚进遮挡带。
_ROW_BAND = (0.25, 0.65)   # 行中心要落进视口高度的这个带(底部发布钮带之外)
_ROW_BAND_TRIES = 4
_ROW_BAND_JS = (
    "(sel) => { const el = document.querySelector(sel); /* row-band-probe */"
    " if (!el) return null; const r = el.getBoundingClientRect();"
    " return {cx: r.x + r.width / 2, cy: r.y + r.height / 2,"
    " ih: window.innerHeight}; }"
)


def _scroll_row_to_mid_viewport(page, human: SyncHumanActions, selector: str) -> None:
    """把设置区某行拟人滚到视口中带(尽力而为,滚不进只告警不拒绝)。

    与 E8「每个编辑步前重新滚进视口」同族,判据从"在视口内"收紧为"在中带内"
    (``_ROW_BAND``)——底部悬浮发布钮的透明命中区盖住底带,行在带外点了也是白点。
    尽力而为的取舍:内容比视口短的页面滚不动,硬拒绝会把本来能点的场景误杀;
    点击后本就有"没反应"的显式报错兜底,这里只负责把命中率从看运气变成确定。
    """
    hovered = False
    for _ in range(_ROW_BAND_TRIES):
        band = None
        try:
            band = page.evaluate(_ROW_BAND_JS, selector)
        except Exception:  # noqa: BLE001 — 读不出位置就不滚,按原路径点
            return
        if not band:
            return
        ratio = band["cy"] / max(band["ih"], 1)
        if _ROW_BAND[0] <= ratio <= _ROW_BAND[1]:
            return
        if not hovered:
            # mouse.wheel 打在鼠标当前位置,而创作中心滚的是**内层容器**不是窗口
            # (2026-05 踩坑存档:拟人滚动前必须 hover 到可滚区)——先把鼠标移进内容列
            # 中部(行的 x 中线、视口 45% 高:在可滚容器内、也在发布钮遮挡带外)再滚。
            human.hover(
                (band["cx"], band["ih"] * 0.45), reason="移进内容列中部准备滚动"
            )
            hovered = True
        human.scroll("down" if ratio > _ROW_BAND[1] else "up")
        human.wait(0.3, 0.7, context="把设置行滚出底部发布钮遮挡带")
    logger.warning(
        f"[note_components] 设置行 {selector} 滚 {_ROW_BAND_TRIES} 次仍不在视口中带,"
        "按当前位置点击(点击无反应会由后续显式报错兜底)"
    )


def _set_quote(
    page,
    human: SyncHumanActions,
    responses: ComponentResponses,
    quoted_note_id: str,
    quoted_note_is_own: Optional[bool] = None,
) -> Dict[str, Any]:
    """引用笔记:开弹窗 → **按 note_id 定位候选**(标题交叉校验)→ 确认引用 → 读回。

    候选卡 DOM 里不暴露 note_id(与笔记管理页同源,实测含 24 位 hex 的元素 0 个),
    故用弹窗打开时页面自己发的 ``posted?tab=1`` 响应做**有序映射**:响应第 i 条对应
    第 i 张卡。这个"同序"假设没有实测背书,所以**必须**再用标题交叉校验一次
    ——对不上就报错,绝不引用一篇不确定是不是它的笔记。

    **弹窗一旦打开就必须收尾**(2026-08-02 真号事故):本函数开弹窗之后有六条失败早返回
    路径,此前没有一条关掉它。而弹窗是**覆盖层**——开着的时候发布按钮根本点不到,
    偏偏 ``step7`` 的兜底会在全页找红色按钮,正好把弹窗里那个 disabled 的「确认引用」
    当成发布按钮点掉,于是"点了但什么都没发生",最后报「发布超时(30秒)」。
    好好生活号 job 132/133/134 连续三次全栽在这,而运营侧看到的只有一句超时,
    换图片体积/换活动/压正文字数试了三轮全是徒劳——**没有一个变量与真正的原因有关**。

    故收尾放在 ``finally`` 里:无论从哪条路径返回、还是中途抛异常,弹窗都关掉。

    ``quoted_note_is_own``:被引用那篇在台账里是不是本账号自己的(``None``=台账查不到)。
    只用来拦"本账号笔记走他人 tab"这条必死的降级路,判定见 ``_block_other_tab_reason``。
    """
    container = page.query_selector(_QUOTE_CONTAINER)
    if container is None:
        return {"status": "error", "reason": "quote_entry_not_found: 页面没有引用笔记入口"}

    # 先滚出底部发布钮遮挡带,再重新定位(滚动会换 rect)——根因见 _scroll_row_to_mid_viewport
    _scroll_row_to_mid_viewport(page, human, _QUOTE_CONTAINER)
    container = page.query_selector(_QUOTE_CONTAINER) or container
    seen = responses.count(_POSTED_API_MARK)  # 基线必须在点击**之前**取
    human.click(container, reason="打开引用笔记弹窗")
    try:
        result = _set_quote_in_modal(page, human, responses, quoted_note_id, seen)
        if result.get("status") == "done":
            return result
        # **只对"这篇不是本账号的"这一种降级**:目标不在「我的笔记」候选里,多半就是
        # 跨账号引用(业务上真实存在——咨询师推介笔记要引用主号那篇小助手联系方式)。
        # 其余失败(卡片顺序与接口对不上、确认后没生效)都是"状态不确定",这时候换条路
        # 再试等于拿不确定去赌,正是本模块一直拒绝做的事。
        if "quoted_note_not_in_candidates" not in (result.get("reason") or ""):
            return result
        blocked = _block_other_tab_reason(result, quoted_note_id, quoted_note_is_own)
        if blocked is not None:
            result["reason"] = blocked
            return result
        logger.info(
            f"[note_components] 引用目标不在本账号笔记里,改走「{_QUOTE_TAB_OTHER_TEXT}」: "
            f"{quoted_note_id}"
        )
        other = _set_quote_via_other_tab(page, human, responses, quoted_note_id)
        # 两条路都没成时,把两个原因都带上——只报后一个会让人以为"本账号里也没有"这件事
        # 没发生过,排查时又要重走一遍。
        if other.get("status") != "done":
            other["reason"] = f"{other.get('reason')}(先前:{result.get('reason')})"
            # **结构化取证也要跟着过来**:候选覆盖面(candidates_count/oldest/
            # scroll_rounds/exhausted)是判"目标在窗口外还是翻页没到底"的唯一依据,
            # 只合并 reason 文本等于把它们丢在降级路上——调用方拿到的是他人 tab 的
            # 失败结构,里面没有这几个数,于是又只能靠猜。
            # 不覆盖 other 自己已有的键:后一条路的取证优先级更高。
            for key, value in result.items():
                if key not in ("status", "reason") and key not in other:
                    other[key] = value
        return other
    finally:
        # 成功路径上「确认引用」自己会关掉弹窗,这里是幂等收尾:还开着才点「取消」
        _close_quote_modal(page, human)


def _block_other_tab_reason(
    result: Dict[str, Any], quoted_note_id: str, quoted_note_is_own: Optional[bool]
) -> Optional[str]:
    """该不该拦住「他人笔记」这条降级路?该拦就返回**替换用的报错文案**,否则 None。

    降级门原本假设"不在候选 = 别人的笔记"。2026-08-12/13 生产实录证明这个假设会被击穿:
    三篇引用目标**都是各自账号自己的公开笔记**,只因候选列表懒加载没翻到它们就走了他人
    tab —— 而他人 tab 的检索**按设计排除本账号笔记**,必然返回空,于是两路全死,运营看到
    的却是一句"笔记被删/私密/平台限制"的误导性报错,顺着它查一整轮都查不到东西。

    两种拦法,给运营的下一步不同:

    - **台账说这篇归本账号**(``quoted_note_is_own=True``):他人 tab 是确定的死路,不去。
    - **候选列表没翻到底**(封顶轮数用完还在出新页):"不在候选里"这个前提根本没成立,
      此时降级等于拿一个没验证的结论去赌。

    台账里查不到这篇(``None``)且列表确实翻到底了 → 放行:那才是"多半是别人的笔记"
    (跨账号引用接待员联系方式那篇是真实业务,不能一刀切堵死)。
    """
    if quoted_note_is_own:
        return (
            f"quoted_note_not_in_candidates_after_scroll: 台账里 note_id={quoted_note_id} "
            f"就是**本账号自己**的笔记,但把「我的笔记」候选列表翻完也没有它 —— "
            f"不走「{_QUOTE_TAB_OTHER_TEXT}」(那条路按设计排除本账号笔记,检索必然返回空)。"
            f"先看下面的候选覆盖面:平台给候选列表设了**上限**(实测 ≈50 篇,按时间倒序),"
            f"比窗口更老的笔记再怎么翻也进不来 —— 覆盖面里最老那篇比目标还新,就是这种;"
            f"翻的轮数明显偏少才该怀疑翻页。也请核对这篇是否已删/转私密/不在图文 tab 下。"
            f"原始判定:{result.get('reason')}"
        )
    if result.get("candidates_exhausted") is False:
        return (
            f"quoted_note_candidates_truncated: 候选列表滚满封顶轮数仍在出新页,"
            f"没能确认 note_id={quoted_note_id} 真不在本账号笔记里,故不降级到"
            f"「{_QUOTE_TAB_OTHER_TEXT}」(这个号的笔记数可能超出翻页上限,"
            f"需要调高 _QUOTE_SCROLL_ROUNDS);原始判定:{result.get('reason')}"
        )
    return None


def _set_quote_via_other_tab(
    page, human: SyncHumanActions, responses: ComponentResponses, quoted_note_id: str
) -> Dict[str, Any]:
    """跨账号引用:切「他人笔记」→ 填 note_id → 等唯一候选 → 选中 → 确认引用 → 回读。

    2026-08-02 真号只读观察确定的流程(不是猜的):切 tab 零网络请求;输入框虽写着
    「请粘贴笔记链接」,填 **note_id** 就能检索出来;检索后才渲染候选卡。

    **只接受恰好一张候选**:0 张说明这个 id 检索不到(笔记被删/私密/id 写错),
    多张说明这个 id 不足以唯一确定一篇——两种都拒绝,**绝不点第一张凑数**
    (与「我的笔记」那条路"对不上就报错"同一条纪律)。

    回读判据用**基线对比**而不是"包含标题":跨账号引用的典型目标(主号那篇小助手联系
    方式)是**空标题**笔记,拿标题去比对必然失败。故设置前先记引用区原文,确认后必须
    变了才算数——比"没报错就算成功"强,也是空标题下唯一站得住的判据。
    """
    tab = _find_button_by_text(page, _QUOTE_TAB_OTHER_TEXT)
    if tab is None:
        return {"status": "error",
                "reason": f"quote_other_tab_not_found: 弹窗里没有「{_QUOTE_TAB_OTHER_TEXT}」"}
    human.click(tab, reason=f"切到{_QUOTE_TAB_OTHER_TEXT}")
    human.wait(0.6, 1.2, context="等 tab 切换")

    box = page.query_selector(_QUOTE_LINK_INPUT)
    if box is None:
        return {"status": "error", "reason": "quote_other_input_not_found: 没找到笔记链接输入框"}
    before = read_quote_text(page)  # 基线必须在设置**之前**取
    seen = responses.count(_OTHERS_SEARCH_API_MARK)  # 基线在输入**之前**取
    human.type_text(box, str(quoted_note_id))
    human.wait(1.2, 2.0, context="等检索结果")

    # **按接口响应精确校验**,而不是"只有一张卡就认了":逐字输入过程中页面会对中间态
    # (note_link=68d508 这种半截 id)也发一次检索,所以不能只看"有没有结果",
    # 必须确认最后拿到的那条 note_id 就是要引用的那篇。
    body = _wait_body(page, responses, _OTHERS_SEARCH_API_MARK, _MODAL_TIMEOUT_S, seen)
    got_id = str(((body or {}).get("data") or {}).get("note_id") or "").strip()
    if got_id != str(quoted_note_id):
        # 空 id 与"拿错 id"是两种处境,报错分开说(2026-08-04 P1-2 缺陷 b,b90dfb4f 实录):
        # 空 = 该 id 检索不出结果(笔记被删/私密/平台限制)或消费到了逐字输入中间态的
        # 空响应(竞态,待复现取证);非空不等 = 消费错了响应。都拒绝,但给运营的下一步不同。
        if not got_id:
            return {
                "status": "error",
                "reason": f"quote_other_id_mismatch: 他人笔记检索返回空(note_id=''),"
                          f"id={quoted_note_id!r} 检索不到——可能笔记被删/私密/平台限制,"
                          f"或检索响应竞态;请带本次 job_id 复现上报以便网络层取证;"
                          f"短期规避:显式传本账号内的 quoted_note_id 或暂不挂引用",
            }
        return {
            "status": "error",
            "reason": f"quote_other_id_mismatch: 检索接口返回的是 note_id={got_id!r},"
                      f"不是要引用的 {quoted_note_id!r},拒绝引用一篇不确定是不是它的笔记",
        }

    cards = _wait_quote_cards(page)
    if len(cards) != 1:
        return {
            "status": "error",
            "reason": f"quote_other_not_unique: 按 note_id 检索到 {len(cards)} 张候选卡"
                      f"(要求恰好 1 张,绝不猜)",
        }
    # 选中态回读与「我的笔记」那条路共用一套(同一个弹窗、同一颗「确认引用」)
    picked = _select_quote_card(page, human, cards[0], "他人笔记")
    if picked["status"] != "done":
        return picked
    failed = _click_quote_confirm(page, human)
    if failed is not None:
        return failed

    after = read_quote_text(page)
    # 与「我的笔记」那条路同口径:看是不是不再是空态,不看"跟之前比变了没有"
    # (重复设同一篇时前后一样,用变化当判据会把幂等重跑判成失败)。
    if not after or _norm(after) == _QUOTE_EMPTY_TEXT:
        return {
            "status": "error",
            "reason": f"quote_not_applied: 确认后引用区仍是未设置态 {after[:40]!r}",
            "observed": after,
        }
    return {
        "status": "done",
        "quoted_note_id": str(quoted_note_id),
        "via": "other_notes_tab",
        "observed": after,
        # 提交后重进页面回读要用它做基线(空标题笔记没法按标题比对,见 _verify_after_submit)
        "quote_text_before": before,
    }


def _close_quote_modal(page, human: SyncHumanActions) -> None:
    """把「选择笔记」弹窗关掉;**已经关了就什么都不做**,且**绝不抛异常**。

    绝不抛的理由:收尾失败不该把一个"组件没设上"升级成"发布崩溃"——组件本就是
    不阻断发布的。但收尾**做没做成要留在日志里**,因为没关掉就等于发布必然超时。

    只点「取消」:**Escape 关不掉这条产品线的弹窗**(笔记管理页那批弹窗实测按了仍开着,
    见 note_ledger 排查记录),别再试键盘。
    """
    try:
        modal = page.query_selector(_QUOTE_MODAL)
        if modal is None or not modal.is_visible():
            return
        cancel = _find_button_by_text(page, _QUOTE_CANCEL_TEXT)
        if cancel is None:
            logger.warning(
                f"[note_components] 引用弹窗还开着却找不到「{_QUOTE_CANCEL_TEXT}」,"
                "发布按钮会被它盖住"
            )
            return
        human.click(cancel, reason="关掉引用笔记弹窗")
        human.wait(0.3, 0.8, context="等弹窗收起")
        left = page.query_selector(_QUOTE_MODAL)
        if left is not None and left.is_visible():
            logger.warning("[note_components] 点了「取消」引用弹窗仍未关闭,发布按钮可能被盖住")
    except Exception as exc:  # noqa: BLE001 — 收尾绝不上抛
        logger.warning(f"[note_components] 关引用弹窗异常(不阻断发布): {exc}")


def _merge_candidate_notes(responses: ComponentResponses, seen: int) -> List[dict]:
    """把 ``seen`` 之后收到的候选分页按到达顺序拼起来(按 note_id 去重)。

    去重但**保持首次出现的顺序**:顺序是选卡算法的依据(见 ``_pick_untitled_card``),
    打乱它等于把修好的东西又弄坏。
    """
    merged: List[dict] = []
    seen_ids: set = set()
    for body in (responses.bodies.get(_POSTED_API_MARK) or [])[seen:]:
        for note in ((body or {}).get("data") or {}).get("notes") or []:
            note_id = str((note or {}).get("id") or "").strip()
            if not note_id or note_id in seen_ids:
                continue
            seen_ids.add(note_id)
            merged.append(note)
    return merged


def _settle_candidate_pages(
    page, responses: ComponentResponses, baseline: int, *, require_first: bool
) -> bool:
    """等候选分页安静下来:连续 ``_PAGE_SETTLE_S`` 没有新页即收工;返回**有没有到过新页**。

    两种用法,差别只在"一页都没来时等多久"——这个差别很贵,不能合并:

    - ``require_first=True``(**开弹窗那次**):页必然会来,只是不知道多晚,故必须先等到
      第一页才谈得上"安静",等不到就耗满 ``_MODAL_TIMEOUT_S``;
    - ``require_first=False``(**每次滚动之后**):很可能已经到底、根本没有下一页。这时
      从滚完那一刻起算静默窗,``_PAGE_SETTLE_S`` 内没动静就判"没有下一页"——**不能**
      套用上一种(那会让每一轮空滚都白烧 12 秒,两轮到底就是 24 秒纯等待)。

    判错了也不丢数据:响应是累加的,迟到的那一页会在下一轮重新合并时被算进去。
    """
    timeout_s = _MODAL_TIMEOUT_S if require_first else _QUOTE_SCROLL_WAIT_S
    deadline = time.monotonic() + timeout_s
    last_count = baseline
    settled_at = None if require_first else time.monotonic()
    while time.monotonic() < deadline:
        count = responses.count(_POSTED_API_MARK)
        if count > last_count:
            last_count = count
            settled_at = time.monotonic()
        elif settled_at is not None and time.monotonic() - settled_at >= _PAGE_SETTLE_S:
            break
        page.wait_for_timeout(300)
    return last_count > baseline


def _pick_scroll_anchor(page, cards: List[Any]):
    """挑滚轮落点:**滚动容器的可见矩形中心**;容器读不出才退回候选卡启发式。

    ``mouse.wheel`` 打在鼠标**当前位置**,落点挑错就是滚了别的容器(本仓 2026-05 血案:
    滚轮打在侧栏,"翻两页就停")。这里点名 ``.select-note-modal__list-wrap`` 这个类,
    **不是**按"最大 overflow 容器"之类的面积启发式去猜 —— 2026-08-13 真号探针把整条
    祖先链的 ``overflow-y`` / ``scrollHeight`` 都拍下来了,滚的就是它,而且滚轮冒泡到它
    就被消费(``prevented_count=0``)。容器中心必然落在它自己的矩形里,这是唯一
    "怎么滚都不会打偏"的落点。

    **为什么不能继续用候选卡**(缺陷 A 的病灶):老判据取"页脚上沿之上的最后一张卡",
    可页脚上沿在 y=632,而容器可视区下沿只到 y=608 —— 中间 24px 是被 ``overflow`` 裁掉、
    ``bounding_box()`` 却照样读得出的**死带**。第一轮滚动之后,落进死带的卡就会被选成落点,
    鼠标于是停在滚动容器外面,滚轮打给不可滚的 ``d-modal-content``,后两轮空转被判"到底了"。
    生产实录:候选停在 22 篇,而同一时刻探针连滚两轮拿到 40 篇。

    容器读不出时退回原有的候选卡启发式(那份 ``quote_modal`` 夹具就没采到容器);
    **两者都没有返回 ``None``**,调用方据此判"弹窗里根本没东西可滚"。
    """
    center = _list_wrap_center(page)
    if center is not None:
        return center
    if not cards:
        return None
    logger.warning(
        f"[note_components] 引用弹窗里读不出滚动容器 {_QUOTE_LIST_WRAP},"
        "退回候选卡当滚轮落点(可能落进页脚死带,翻页会打折扣)"
    )
    limit = _element_top(_find_button_by_text(page, _QUOTE_CANCEL_TEXT))
    if limit is None:
        return cards[0]
    visible = [c for c in cards if _above_limit(_element_center_y(c), limit)]
    return visible[-1] if visible else cards[0]


def _list_wrap_box(page) -> Optional[Dict[str, float]]:
    """候选列表滚动容器的矩形;容器不在/矩形读不出返回 None。

    它同时是两件事的依据:滚轮落点(``_pick_scroll_anchor``)和"目标卡在不在可视区里"
    (``_card_view_state``)—— 后者才是 2026-08-13 号 7 三单失败的判据来源。
    """
    try:
        wrap = page.query_selector(_QUOTE_LIST_WRAP)
    except Exception:  # noqa: BLE001 — 读不出就当没有,交给退回路径
        return None
    box = _element_box(wrap)
    if not box or box["width"] <= 0 or box["height"] <= 0:
        return None
    return box


def _list_wrap_center(page) -> Optional[tuple]:
    """候选列表滚动容器的中心坐标(已夹进视口);容器不在/矩形读不出返回 None。"""
    box = _list_wrap_box(page)
    if box is None:
        return None
    return _clamp_to_viewport(
        page, box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
    )


def _element_box(element) -> Optional[Dict[str, float]]:
    """元素矩形;元素不在/已 detach 返回 None。"""
    if element is None:
        return None
    try:
        return element.bounding_box()
    except Exception:  # noqa: BLE001
        return None


def _clamp_to_viewport(page, x: float, y: float) -> tuple:
    """把坐标夹进当前视口(留 10% 边距);读不到视口尺寸就按常见 1280x800 兜底。

    与 ``note_comments_read._clamp_to_viewport`` 同款(本仓既有写法):鼠标"移"到视口外
    不是真的悬停,滚轮落点也就无从谈起。引用弹窗一般整个在视口里,这一道是防御性的 ——
    小窗口/高 DPI 缩放下容器中心确实可能被挤出去。
    """
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


def _element_top(element) -> Optional[float]:
    """元素矩形上沿;元素不在/读不出返回 None(调用方按"没有这条边界"处理)。"""
    box = _element_box(element)
    return box["y"] if box else None


def _element_center_y(element) -> Optional[float]:
    """元素矩形中心的纵坐标;读不出返回 None。"""
    box = _element_box(element)
    return box["y"] + box["height"] / 2 if box else None


def _above_limit(center_y: Optional[float], limit: float) -> bool:
    """中心在 limit 之上(读不出坐标一律判否:宁可不选它当落点)。"""
    return center_y is not None and center_y < limit


def _scroll_candidate_list(page, human: SyncHumanActions) -> bool:
    """在候选列表里拟人滚一屏(翻页用)。

    返回 ``False`` **只**表示一件事:弹窗里连滚动容器带候选卡一个都找不到 —— 真的没得可翻。
    这个语义是刻意收严的:老实现"列表里一张卡都没有"就返回 False,而上层把 False 当成
    "列表到底了"直接收工。可"这一瞬没渲染出卡"跟"平台没有下一页"是两回事,把前者当后者
    就是缺陷 A 的上层放大器 —— 一轮都不肯再试。**落点挑不出不等于到底**,该由上层按
    "这轮没新增"的正常停滞计数去判。
    """
    anchor = _pick_scroll_anchor(page, page.query_selector_all(_QUOTE_NOTE_CARD))
    if anchor is None:
        logger.warning(
            "[note_components] 引用弹窗里既没有滚动容器也没有候选卡,没得可翻"
        )
        return False
    human.hover(anchor, reason="移进引用候选列表准备滚动")
    human.scroll("down")
    human.wait(0.4, 0.9, context="等候选列表加载下一页")
    return True


def _wait_all_candidate_notes(
    page,
    human: SyncHumanActions,
    responses: ComponentResponses,
    seen: int,
    target_note_id: Optional[str] = None,
) -> tuple:
    """收齐引用候选列表的**全部分页**(必要时主动滚动翻页)。

    返回 ``(notes, exhausted, rounds)``;``rounds`` 是实际滚了几轮 —— 它和候选篇数一起
    构成**候选覆盖面**,是 ``quoted_note_not_in_candidates`` 那条失败唯一能自证的东西
    (见 ``_set_quote_in_modal``:翻满了还没有 = 目标在平台候选窗口外;没翻满就停 =
    翻页本身还有问题。少了轮数这两种分不开,只能靠猜)。

    **候选列表是分页 + 懒加载的**。分页那一半 2026-08-03 回放夹具已实测:弹窗打开时连发
    ``posted?tab=1&page=0``(11 条)与 ``page=1``(10 条),两页一起渲染成 20 张卡;
    原实现用 ``_wait_body`` 取 ``latest`` 只看得见最后一页,已于当时修成"合并全部分页"。

    懒加载那一半是 2026-08-13 生产 RCA 补的:**没有任何代码去滚这个列表**,所以自动发的
    头一两页就是全部——49 篇的号实录只收到 12 篇候选,排在第 37 位的目标必然被判
    ``quoted_note_not_in_candidates``,再被降级门当成"别人的笔记"送进必死的他人 tab。
    故这里主动在列表内拟人滚动,每滚一轮再等一次分页。

    停止条件三选一(``exhausted`` 只在后两种为真):

    - **目标已出现**在累计响应里 —— 再往下翻没有意义,直接收工;
    - 连续 ``_QUOTE_SCROLL_IDLE_ROUNDS`` 轮既没有新页、也没有新卡 → 列表到底了;
    - 用满 ``_QUOTE_SCROLL_ROUNDS`` 轮 → **``exhausted=False``**:此时"不在候选里"这个
      结论不成立(可能只是还没翻到),调用方据此拒绝降级到他人 tab。

    ``exhausted`` 是给降级门用的:只有"确实把本账号笔记翻完了都没有它",
    才谈得上"这多半是别人的笔记"。
    """
    _settle_candidate_pages(page, responses, seen, require_first=True)
    merged = _merge_candidate_notes(responses, seen)
    target = str(target_note_id) if target_note_id else None

    def _hit() -> bool:
        return bool(target) and any(
            str((n or {}).get("id") or "").strip() == target for n in merged
        )

    idle = 0
    rounds = 0
    for _ in range(_QUOTE_SCROLL_ROUNDS):
        if _hit():
            return merged, False, rounds
        cards_before = len(page.query_selector_all(_QUOTE_NOTE_CARD))
        notes_before = len(merged)
        baseline = responses.count(_POSTED_API_MARK)
        if not _scroll_candidate_list(page, human):
            # 收严后这里只剩一种情形:滚动容器和候选卡**双双**不在 —— 弹窗里真没东西可滚。
            # (老实现"没卡就 False"会把"卡还没渲染出来"那一瞬当成到底,一轮都不肯再试。)
            return merged, True, rounds
        rounds += 1
        _settle_candidate_pages(page, responses, baseline, require_first=False)
        merged = _merge_candidate_notes(responses, seen)
        cards_after = len(page.query_selector_all(_QUOTE_NOTE_CARD))
        # "有没有进展"看的是**多出来的笔记/卡片**,不是"有没有新响应到达":
        # 到底之后平台完全可能把同一页再发回来一次,拿"来了新响应"当进展会让循环
        # 永远用满封顶轮数,``exhausted`` 于是永远为假 —— 跨账号引用那条合法的降级路
        # 就被自己人堵死了。
        if len(merged) > notes_before or cards_after > cards_before:
            idle = 0
            continue
        idle += 1
        if idle >= _QUOTE_SCROLL_IDLE_ROUNDS:
            return merged, True, rounds
    logger.warning(
        f"[note_components] 引用候选列表滚满 {_QUOTE_SCROLL_ROUNDS} 轮仍在出新页"
        f"(已收 {len(merged)} 篇),不再翻;此时「不在候选里」不足以断定是他人笔记"
    )
    return merged, False, rounds


def _candidates_oldest(notes: List[dict]) -> Optional[str]:
    """翻到的**最老一篇**(时间;取不到退回它的标题,再取不到 None)——候选窗口的下边界。

    取"最后一条"而不是排序求最小:候选接口按时间**倒序**返回(2026-08-13 取证:号 7
    的 49 篇从当天一路排到 2026-02-13),末尾那条就是翻到的最老一篇。这么取也不必对
    ``time`` 的格式做任何假设 —— 换成别的写法照样是"最后那条"。
    """
    last = next((n for n in reversed(notes) if n), None)
    if not last:
        return None
    return _norm(last.get("time")) or _norm(last.get("display_title")) or None


def _coverage_phrase(count: int, oldest: Optional[str], rounds: int) -> str:
    """候选覆盖面的一句人话(结构化版本是 ``candidates_count`` / ``candidates_oldest``
    / ``scroll_rounds`` 三个字段)。

    两个数一起看就能判定该往哪儿查:**翻满了还没有** = 目标在平台候选窗口外(换目标);
    **没翻满就停** = 翻页本身还有问题(查滚动落点)。
    """
    tail = f",最老一篇 {oldest}" if oldest else ""
    return f"候选覆盖面:滚了 {rounds} 轮共翻到 {count} 篇{tail}"


def _pick_untitled_card(cards: List[Any], notes: List[dict], index: int):
    """空标题笔记的选卡:**按"扣除未渲染项之后的位置"**取,而不是裸下标。

    为什么非要支持它:主号那篇「小助手联系方式」二维码笔记就是空标题的,而业务规则要求
    每篇咨询师推介笔记都引用它 —— 单主号自己就有 20 篇推介笔记落在这条路上。空标题没法
    按文案认卡,又不能不做。

    为什么"扣除未渲染项"这个算法站得住(2026-08-03 真号夹具实测):弹窗会把**当前正在
    编辑的那篇**排除掉(不能自己引用自己),接口 21 条只渲染 20 张卡;而**剔除那一条之后,
    接口顺序与卡片顺序严格对齐 20/20**。所以顺序本身是可靠的,原实现唯一的错是没扣除
    被排除的那篇。

    **算不准就拒绝**:只有当"标题非空却没出现在任何卡里"的条数恰好等于接口与卡片的数量差
    时,才说明所有被排除项都已被识别、算术可信。否则(比如列表里还有别的空标题笔记,
    没法判断它渲染了没有)一律拒绝 —— 引用错一篇笔记比不引用糟得多。
    """
    rendered = [_norm(c.inner_text()) for c in cards]

    def _titled_but_absent(note: dict) -> bool:
        title = _norm((note or {}).get("display_title"))
        return bool(title) and not any(title in r for r in rendered)

    excluded_total = len(notes) - len(cards)
    identified = [i for i, n in enumerate(notes) if _titled_but_absent(n)]
    if excluded_total < 0 or len(identified) != excluded_total:
        return {
            "status": "error",
            "reason": f"quote_untitled_position_unverifiable: 接口 {len(notes)} 条 / 卡片 "
                      f"{len(cards)} 张,只认出 {len(identified)} 条未渲染项,"
                      f"对不上数量差 {excluded_total},无法确定空标题笔记落在第几张,拒绝猜",
        }
    card_index = index - sum(1 for i in identified if i < index)
    if not 0 <= card_index < len(cards):
        return {
            "status": "error",
            "reason": f"quote_untitled_index_out_of_range: 算出的卡片位置 {card_index} "
                      f"不在 0..{len(cards) - 1}",
        }
    return {"_card": cards[card_index], "_title": ""}


def _quote_confirm_state(page) -> Dict[str, Any]:
    """读「确认引用」按钮的可点态:``{"found", "enabled", "cls"}``。

    判据见 ``_QUOTE_DISABLED_TOKEN`` 的注释(夹具实测的两路:``disabled`` 属性 +
    class 里的独立 token)。**纯属性读取,不走 JS** —— 弹窗这条路要能在回放夹具上跑,
    而 JS 求值没法离线重放(见 ``tests/page_replay``)。

    找不到按钮返回 ``{"found": False}``,调用方当作**不可点**处理:找不到是页面状态异常,
    不是"可以点了"。
    """
    button = _find_button_by_text(page, _QUOTE_CONFIRM_TEXT)
    if button is None:
        return {"found": False, "enabled": False, "cls": ""}
    try:
        cls = button.get_attribute("class") or ""
        disabled_attr = button.get_attribute("disabled") is not None
    except Exception:  # noqa: BLE001 — 元素已 detach,当作读不出 → 不可点
        return {"found": False, "enabled": False, "cls": ""}
    disabled = disabled_attr or _QUOTE_DISABLED_TOKEN in cls.split()
    return {"found": True, "enabled": not disabled, "cls": cls}


def _safe_class(element) -> str:
    """读元素 class,读不出返回空串(只用于取证日志,绝不因此打断流程)。"""
    try:
        return element.get_attribute("class") or ""
    except Exception:  # noqa: BLE001
        return ""


def _wait_confirm_enabled(page) -> Dict[str, Any]:
    """轮询等「确认引用」解禁(选中态生效的**可观测判据**);超时返回最后一次读数。"""
    deadline = time.monotonic() + _QUOTE_SELECT_SETTLE_S
    state = _quote_confirm_state(page)
    while not state.get("enabled") and time.monotonic() < deadline:
        page.wait_for_timeout(200)
        state = _quote_confirm_state(page)
    return state


def _card_view_state(page, card) -> Dict[str, Any]:
    """判目标候选卡在不在**滚动容器可视区**里,并把判据用到的两组矩形一起带出来。

    ``inside`` 三态:``True`` 在区内、``False`` 在区外、``None`` **读不出判据**
    (卡片矩形读不出,或容器和页脚两条边界都没有)。``None`` 按"不折腾"处理 ——
    没有判据时硬判失败会把一条能走通的路堵死。

    边界取 ``.select-note-modal__list-wrap`` 的矩形,**上下都判**、且要求卡片矩形
    **完整**落在里面:

    - 用容器而不是弹窗页脚:2026-08-13 真号探针(``..._171433``)拍到目标卡
      ``y=727 h=182``、容器可视区 ``y=184 h=424``(即 [184, 608])—— 卡在可视区**下方
      119px**,点击落点(卡心 y≈818)落在弹窗之外,根本没点到卡。老判据的边界是页脚上沿
      ``y=632``,而 608~632 这 24px 是被 ``overflow`` 裁掉、``bounding_box()`` 却照样读得出
      的死带,拿它当边界等于把"已经看不见了"判成"还在区里"。
    - 上沿同样要判:卡被滚到列表顶部之上一样点不中。**不能指望** ``human.click`` 自带的
      ``scroll_into_view_if_needed`` —— 本仓早有记录:它认的是"元素技术上可见",元素被
      祖先容器裁掉时它不滚。
    - 容器读不出(``quote_modal`` 那份夹具就没采到)才退回原来的页脚判据
      (告警由 ``_bring_card_into_view`` 发,那里一轮只发一次)。
    """
    card_box = _element_box(card)
    state: Dict[str, Any] = {
        "inside": None,
        "mode": "unknown",
        "card_rect": card_box,
        "view_rect": None,
        "footer_top": None,
    }
    if card_box is None:
        return state

    wrap_box = _list_wrap_box(page)
    if wrap_box is not None:
        top = wrap_box["y"]
        bottom = wrap_box["y"] + wrap_box["height"]
        state["mode"] = "list_wrap"
        state["view_rect"] = wrap_box
        state["inside"] = (
            card_box["y"] >= top and card_box["y"] + card_box["height"] <= bottom
        )
        return state

    limit = _element_top(_find_button_by_text(page, _QUOTE_CANCEL_TEXT))
    if limit is None:
        return state                 # 两条边界都没有:没判据,不折腾
    state["mode"] = "footer"
    state["footer_top"] = limit
    state["inside"] = _above_limit(card_box["y"] + card_box["height"] / 2, limit)
    return state


def _fmt_rect(box: Optional[Dict[str, float]]) -> str:
    """矩形写成一行取证文案(带纵向区间 —— 判据比的就是这两个区间)。"""
    if not box:
        return "读不出"
    top = box["y"]
    bottom = box["y"] + box["height"]
    return (f"x={box['x']:.0f} y={top:.0f} w={box['width']:.0f} h={box['height']:.0f}"
            f"(纵向 [{top:.0f}, {bottom:.0f}])")


def _view_evidence(state: Dict[str, Any]) -> str:
    """把"卡片矩形 vs 可视区矩形"两组数摊开 —— 它们本身就是判据,报错必须带上。"""
    card = f"卡片矩形 {_fmt_rect(state.get('card_rect'))}"
    if state.get("mode") == "footer":
        return f"{card};滚动容器读不出,退回页脚上沿 y={state.get('footer_top')}"
    return f"{card};滚动容器可视区 {_fmt_rect(state.get('view_rect'))}"


def _card_scroll_plan(state: Dict[str, Any]) -> tuple:
    """滚哪边、滚多远:``(direction, distance)``,``distance=None`` 表示用默认随机距离。

    卡在可视区**上方**就往回滚(方向挑反了三轮全白费),并且**差多少滚多少**——
    理由见 ``_QUOTE_CARD_SCROLL_GAIN`` 那段注释。退回页脚判据时没有容器矩形、算不出
    差值,那条路仍走默认随机距离。
    """
    card = state.get("card_rect") or {}
    view = state.get("view_rect")
    if not view or card.get("y") is None:
        return "down", None
    if card["y"] < view["y"]:
        direction, gap = "up", view["y"] - card["y"]
    else:
        direction = "down"
        gap = (card["y"] + card["height"]) - (view["y"] + view["height"])
    return direction, max(_QUOTE_CARD_SCROLL_MIN_PX, int(gap * _QUOTE_CARD_SCROLL_GAIN))


def _bring_card_into_view(page, human: SyncHumanActions, card) -> Dict[str, Any]:
    """把目标候选卡滚进候选列表可视区;返回 ``{"ok": bool, "state": 最后一次判读}``。

    ``ok=False`` 就是**真的没滚进去**,调用方据此报 ``quote_card_offscreen``、**不点**。
    老实现在这里"尽力而为":滚满仍在区外也返回 ``True``,然后照着区外坐标点下去 ——
    点到的是弹窗外的别的元素,失败于是以"选中没生效 / 平台拒绝"的面目出现在下游
    (2026-08-13 号 7 三单)。判据与三态语义见 ``_card_view_state``。
    """
    state = _card_view_state(page, card)
    if state["mode"] == "footer":
        logger.warning(
            f"[note_components] 引用弹窗里读不出滚动容器 {_QUOTE_LIST_WRAP},"
            f"退回页脚上沿(y={state['footer_top']})判卡在不在可视区里 —— "
            "页脚与容器下沿之间有死带,这条判据偏松"
        )
    for _ in range(_QUOTE_CARD_VIEW_TRIES):
        if state["inside"] is not False:   # True=已在区内;None=没判据,不折腾
            return {"ok": True, "state": state}
        cards = page.query_selector_all(_QUOTE_NOTE_CARD)
        if not cards:
            break                          # 列表空了,滚也没意义
        human.hover(_pick_scroll_anchor(page, cards), reason="移进引用候选列表准备滚动")
        human.scroll(*_card_scroll_plan(state))
        human.wait(0.3, 0.7, context="把目标候选卡滚进弹窗可视区")
        state = _card_view_state(page, card)
    if state["inside"] is not False:
        return {"ok": True, "state": state}
    logger.warning(
        f"[note_components] 目标候选卡滚 {_QUOTE_CARD_VIEW_TRIES} 次仍在候选列表可视区外,"
        f"拒绝按当前坐标点击;{_view_evidence(state)}"
    )
    return {"ok": False, "state": state}


def _read_quote_reject_toast(page) -> str:
    """读平台 toast:含拒绝语义就返回**平台原文**,否则空串(读不出一律当没有)。

    优先读 ``_QUOTE_TOAST_TEXT`` 那个叶子 —— 根容器 ``.d-new-toast`` 会把多条通知累加进
    同一个节点,连点几次就读成「…无法引用 …无法引用 …无法引用」,拿它当 detail 只会
    刷屏。叶子读不到再退回根容器(总比丢掉平台的原话强)。
    """
    for sel in (_QUOTE_TOAST_TEXT, _QUOTE_TOAST):
        try:
            nodes = page.query_selector_all(sel)
        except Exception:  # noqa: BLE001 — 取证读数绝不制造异常
            continue
        for node in nodes:
            text = _norm_safe_text(node)
            if _QUOTE_TOAST_REJECT_MARK in text:
                return text
    return ""


def _select_quote_card(page, human: SyncHumanActions, card, label: str) -> Dict[str, Any]:
    """点选候选卡并**回读选中态**;出现平台提示就当场收工,静默失效才重试一次。

    2026-08-13 生产 RCA(号 7 连续三单同款失败):候选里找到了目标卡、点了、也点了
    「确认引用」,回读却仍是空态 —— 而夹具证明**没选中时「确认引用」本来就是 disabled 的**,
    点一颗禁用按钮当然什么都不会发生。原实现点完卡只 ``wait`` 一下就往下走,
    于是"选中没生效"这件事一路裸奔到最后,才以 ``quote_not_applied`` 的面目出现,
    把人往"确认按钮/引用区"上引 —— 真正坏掉的是**上一步**。

    回读判据用**「确认引用」由禁用转可点**:这是夹具里有直接证据的那条(卡片选中态挂了
    什么 class 没有实测,猜一个就是回到"照着代码的假设写测试"的老路)。

    **三个错误码,语义不同,调用方据此判下一步**:

    - ``quote_card_offscreen`` —— 目标卡滚不进候选列表可视区,**一次都没点**
      (点区外坐标只会命中别的元素)。detail 带卡片矩形与可视区矩形两组数。
    - ``quote_target_not_quotable`` —— 点卡之后页面上出现了含「无法引用」的平台提示。
      **只是"点完出现了这条提示"这个事实**,不是"这篇不可引用"的结论:2026-08-14
      人工在同一目标(002c0c)上手动引用成功,可见平台并未禁止;而 toast 根容器会把历史
      通知累加进同一个节点,提示本身也可能是**上一次**操作留下的。detail 照抄原文,
      归因交给看日志的人。
    - ``quote_card_select_not_applied`` —— 点了、没提示、确认钮也没解禁,属于点击没生效。
      这一种才重试:按本仓堆叠浮层纪律**小目标关随机偏移**(卡片中心是选中命中率最稳的
      落点,0.3~0.7 的随机偏移可能落在封面角标/遮罩这类吃掉事件的子元素上)。

    先读 toast 再等解禁,顺序是刻意的:toast 会自己消失,先去耗满
    ``_QUOTE_SELECT_SETTLE_S`` 可能把它等没了 —— 那条平台原文是这次失败里少有的
    一手线索,丢了就只剩"确认钮没解禁"这句什么也说明不了的话。

    没选上时把**卡片自己的 class** 也记进日志:平台到底靠什么标记选中态,
    下次排查时由生产日志白送上门,不必为这一个问题再开一次真号。
    """
    def _attempt(random_offset: bool, reason: str) -> Dict[str, Any]:
        view = _bring_card_into_view(page, human, card)
        if not view["ok"]:
            return {"status": "offscreen", "view": view["state"]}
        human.click(card, random_offset=random_offset, reason=reason)
        human.wait(0.5, 1.0, context="等选中态生效")
        rejected = _read_quote_reject_toast(page)
        if rejected:
            return {"status": "rejected", "toast": rejected}
        state = _wait_confirm_enabled(page)
        if state.get("enabled"):
            return {"status": "done", "confirm_cls": state.get("cls", "")}
        return {"status": "not_applied", "state": state}

    out = _attempt(True, f"选中被引用笔记「{label}」")
    if out["status"] == "done":
        return out
    if out["status"] == "offscreen":
        return _card_offscreen(label, out["view"])
    if out["status"] == "rejected":
        return _not_quotable(label, out["toast"])

    state = out["state"]
    logger.warning(
        f"[note_components] 候选卡「{label}」点了但「{_QUOTE_CONFIRM_TEXT}」仍禁用"
        f"(confirm_cls={state.get('cls', '')[:120]!r} "
        f"card_cls={_safe_class(card)[:120]!r}),关随机偏移重点一次"
    )
    out = _attempt(False, f"重选被引用笔记「{label}」(取卡片中心)")
    if out["status"] == "done":
        return out
    if out["status"] == "offscreen":
        return _card_offscreen(label, out["view"])
    if out["status"] == "rejected":
        return _not_quotable(label, out["toast"])
    state = out["state"]
    return {
        "status": "error",
        "reason": f"quote_card_select_not_applied: 候选卡「{label}」点了两次(第二次取卡片"
                  f"中心)「{_QUOTE_CONFIRM_TEXT}」仍是禁用态,选中没生效 —— "
                  f"卡没点上就不可能引用成功,拒绝去点一颗禁用按钮;"
                  f"多为点击没命中候选卡(卡片落在滚动容器可视区外),"
                  f"先看回执里的矩形数据"
                  f"(confirm_found={state.get('found')} cls={state.get('cls', '')[:120]!r})",
    }


def _card_offscreen(label: str, state: Dict[str, Any]) -> Dict[str, Any]:
    """滚不进可视区就**不点**:区外那个坐标落在弹窗之外,点下去命中的是别的元素。

    detail 带**卡片矩形与可视区矩形两组数** —— 判据就是这两个纵向区间,
    下次再出同类失败,回执自己会说明它是不是同一堵墙,不必再开一次真号。
    """
    evidence = _view_evidence(state)
    logger.warning(f"[note_components] 候选卡「{label}」滚不进候选列表可视区: {evidence}")
    return {
        "status": "error",
        "reason": f"quote_card_offscreen: 候选卡「{label}」滚 {_QUOTE_CARD_VIEW_TRIES} 次"
                  f"仍在候选列表可视区之外,一次都没点(点区外坐标只会命中别的元素);"
                  f"{evidence}",
        "card_rect": state.get("card_rect"),
        "view_rect": state.get("view_rect"),
    }


def _not_quotable(label: str, toast: str) -> Dict[str, Any]:
    """点完卡出现了含「无法引用」的平台提示:**原文照抄**,不做任何转译、也不下结论。

    转译等于把平台的原话换成我们的猜测,而这条提示的**归因并不确定**:
    2026-08-14 人工在同一目标上手动引用成功,证明平台并未禁止引用它;提示既可能来自
    点击落在候选卡之外命中的其它元素,也可能是 toast 根容器累加下来的历史通知。
    所以这里只报"出现了这条提示"这个事实,由看日志的人结合矩形数据判因。
    """
    logger.warning(f"[note_components] 点候选卡「{label}」后出现平台提示: {toast}")
    return {
        "status": "error",
        "reason": f"quote_target_not_quotable: 点了候选卡「{label}」,页面出现平台提示"
                  f"「{toast}」。**注意**:该提示可能来自点击落在候选卡之外命中的其它元素,"
                  f"且 toast 容器会累加历史通知 —— 不能据此断定该笔记不可被引用"
                  f"(2026-08-14 实测同一目标人工可成功引用)",
        "platform_toast": toast,
    }


def _click_quote_confirm(page, human: SyncHumanActions) -> Optional[Dict[str, Any]]:
    """点「确认引用」;按钮不在/仍禁用时**不点**并返回错误,一切正常返回 None。

    点前查禁用态是 2026-08-09 播客 7 单假绿留下的规矩(``podcast.create_button_state``):
    点一颗禁用按钮无事发生,而"点了"这个事实会把排查引向下游。
    """
    state = _quote_confirm_state(page)
    if not state.get("found"):
        return {"status": "error",
                "reason": f"quote_confirm_not_found: 弹窗里没有「{_QUOTE_CONFIRM_TEXT}」"}
    if not state.get("enabled"):
        return {
            "status": "error",
            "reason": f"quote_confirm_disabled: 「{_QUOTE_CONFIRM_TEXT}」是禁用态,不点"
                      f"(点了也无事发生,只会把排查引向下游);cls={state.get('cls', '')[:120]!r}",
        }
    confirm = _find_button_by_text(page, _QUOTE_CONFIRM_TEXT)
    if confirm is None:
        return {"status": "error",
                "reason": f"quote_confirm_not_found: 弹窗里没有「{_QUOTE_CONFIRM_TEXT}」"}
    human.click(confirm, reason="确认引用")
    human.wait(0.8, 1.5, context="等引用生效")
    return None


def _set_quote_in_modal(
    page,
    human: SyncHumanActions,
    responses: ComponentResponses,
    quoted_note_id: str,
    seen: int,
) -> Dict[str, Any]:
    """引用弹窗内的本体流程(弹窗的开与关都由 ``_set_quote`` 负责)。"""
    notes, exhausted, rounds = _wait_all_candidate_notes(
        page, human, responses, seen, target_note_id=quoted_note_id
    )
    if not notes:
        return {
            "status": "error",
            "reason": "quote_candidates_unavailable: 没收到候选笔记列表响应,"
                      "无法按 note_id 定位要引用的笔记",
        }

    index = next(
        (i for i, n in enumerate(notes)
         if str((n or {}).get("id") or "").strip() == str(quoted_note_id)),
        None,
    )
    if index is None:
        oldest = _candidates_oldest(notes)
        failed = {
            "status": "error",
            "reason": f"quoted_note_not_in_candidates: 候选列表({len(notes)} 篇)里没有 "
                      f"note_id={quoted_note_id}(候选只含**我的笔记**这一 tab);"
                      f"{_coverage_phrase(len(notes), oldest, rounds)}",
            # 给降级门用:列表没翻到底时,"不在候选里"这个结论本身就不成立
            "candidates_exhausted": exhausted,
            # **候选覆盖面**(2026-08-13 取证补的):平台自己给候选列表设了上限,与懒加载
            # 翻页是**两堵独立的墙** —— 号 7 候选 49 篇≈上限 50、只覆盖到 2026-02-13,
            # 2025-05-18 那篇**永远翻不到**;号 6 覆盖到 07-27,更老的 5 篇同样进不去。
            # 没有这三个数,"目标不存在"和"目标在候选窗口外"就分不开,只能靠猜。
            "candidates_count": len(notes),
            "scroll_rounds": rounds,
        }
        if oldest:
            failed["candidates_oldest"] = oldest
        return failed

    cards = _wait_quote_cards(page)
    title = _norm((notes[index] or {}).get("display_title"))

    # **按标题找卡,不按下标取卡**(2026-08-03 真号证伪):原实现假设"响应第 i 条 ↔
    # 弹窗第 i 张卡",该假设当初就写明"没有实测背书" —— 现在有反例了:实测第 6 张卡是
    # 「心理咨询师-彭旱雨…」,而接口第 6 条是「粤语咨询师-黄安麟…」。同序不成立,
    # 于是每次都判 quote_card_title_mismatch,引用功能整体不可用。
    #
    # 改成在**全部卡片**里找文案包含该标题的那一张:命中恰好一张才用,0 张或多张都拒绝
    # (多张 = 同号有重名笔记,认不准就不认,与本模块一贯纪律一致)。
    before = read_quote_text(page)   # 基线必须在设置**之前**取(空标题笔记只能靠它复核)
    if title:
        hits = [c for c in cards if title in _norm(c.inner_text())]
        if len(hits) != 1:
            return {
                "status": "error",
                "reason": f"quote_card_not_unique_by_title: 弹窗 {len(cards)} 张卡里,"
                          f"文案含平台标题「{title}」的有 {len(hits)} 张(要求恰好 1 张,绝不猜)",
            }
        card = hits[0]
    else:
        picked = _pick_untitled_card(cards, notes, index)
        if "_card" not in picked:
            return picked            # 算不准就原样上抛拒绝原因
        card = picked["_card"]

    picked = _select_quote_card(page, human, card, title[:15] or "(空标题)")
    if picked["status"] != "done":
        return picked
    failed = _click_quote_confirm(page, human)
    if failed is not None:
        return failed

    quoted = read_quote_text(page)
    # 判据与提交后回读同口径:**看引用区是不是不再是空态**,而不是"含不含标题"。
    # 真号实测(2026-08-03):确认引用之后,引用区显示的是「引用 @<作者昵称> 的笔记」
    # —— **编辑器内也不含标题**。拿标题去比对必然假阴性:引用其实设上了却报没设上。
    #
    # 那"引对了没有"谁保证?**选卡阶段**:按 note_id 在候选响应里定位 → 取其平台标题 →
    # 在全部卡片里唯一命中那一张才点。身份在那时就已经钉死,这一步只需确认"设上了"。
    applied = bool(quoted) and _norm(quoted) != _QUOTE_EMPTY_TEXT
    if not applied:
        return {
            "status": "error",
            "reason": f"quote_not_applied: 确认后引用区仍是未设置态 {quoted[:40]!r}",
            "observed": quoted,
        }
    return {
        "status": "done",
        "quoted_note_id": str(quoted_note_id),
        "title": title,
        # 提交后回读要用它做基线:回读文案是「引用 @作者 的笔记」**不含标题**,
        # 只能比"变了没有"(见 _verify_after_submit)
        "quote_text_before": before,
    }


def _wait_quote_cards(page) -> List[Any]:
    """轮询等弹窗把候选卡渲染出来,返回**当前**卡片列表(可能为空)。"""
    deadline = time.monotonic() + _MODAL_TIMEOUT_S
    cards: List[Any] = []
    while time.monotonic() < deadline:
        cards = page.query_selector_all(_QUOTE_NOTE_CARD)
        if cards:
            return cards
        page.wait_for_timeout(400)
    return cards


def _find_button_by_text(page, label: str):
    """在弹窗里按文案精确匹配一个可见 button;找不到返回 None(不做前缀/包含放宽)。"""
    for sel in (f"{_QUOTE_MODAL} button", ".d-modal button", "button"):
        for el in page.query_selector_all(sel):
            try:
                if _norm(el.inner_text()) == label and el.is_visible():
                    return el
            except Exception:  # noqa: BLE001 — 单个元素读失败只跳过它
                continue
    return None


def _set_activity(
    page, human: SyncHumanActions, responses: ComponentResponses, activity_id: str
) -> Dict[str, Any]:
    """关联活动:按 id→name 定位活动卡 → 点「关联」→ **校验文案翻转**,没翻转就重试。

    三条硬约束(设计 2.7):

    - **只重试同一个活动**,``_ACTIVITY_CLICK_ATTEMPTS`` 次封顶。活动是互斥单选,换活动
      会取消旧的,但旧活动注入正文的话题**不回收** —— 反复切换话题会单调累积并真发出去。
    - 按钮文案已经是「取消关联」= 本来就关联着,直接算 ``skipped``,**绝不点它**。
    - 文案既不是「关联」也不是「取消关联」→ 状态未知,一次都不点。
    """
    catalog = parse_activities(responses.latest(_ACTIVITY_API_MARK))
    if not catalog:
        return {
            "status": "error",
            "reason": "activity_catalog_unavailable: 没收到活动列表响应,无法把 "
                      f"activity_id={activity_id} 映射成活动名,拒绝盲点",
        }
    target = next((a for a in catalog if a["id"] == str(activity_id)), None)
    if target is None:
        return {
            "status": "error",
            "reason": f"activity_not_found: 活动列表({len(catalog)} 条)里没有 "
                      f"id={activity_id}(活动频繁上下线,请重新拉取列表)",
        }
    name = target["name"]
    if not name:
        return {
            "status": "error",
            "reason": f"activity_name_unreadable: id={activity_id} 在列表响应里读不出活动名,"
                      f"无法在页面上定位对应卡片",
        }

    action_text = read_activity_action_text(page, name)
    if action_text is None:
        # **滚动触发懒渲染再找**(2026-08-03 文字版事故):文字版是超长竖图,把「关联活动」
        # 区顶到页面很深处,首屏 DOM 里压根没渲染活动卡 —— 直接报 not_found 是误判。
        # 拟人分段下滚,每滚一轮重查;找到即止,滚到底仍没有才是真没有。
        for _ in range(_ACTIVITY_REVEAL_SCROLLS):
            human.scroll("down")
            human.wait(0.4, 0.9, context="等活动区渲染")
            action_text = read_activity_action_text(page, name)
            if action_text is not None:
                break
    # 内联区没有 → 点开「更多活动」面板再找:内联只渲染约 2 张**推荐**卡,
    # 目标活动不在推荐位时只有面板里才有(2026-08-07 用户实拍确认的结构)。
    via = "inline"
    panel_meta = {"more_entry": False, "panel_opened": False}
    if action_text is None:
        action_text, panel_meta = _reveal_in_more_panel(page, human, name)
        if action_text is not None:
            via = "more_panel"
    if action_text is None:
        # 内联与面板都没有:先分清是「整个活动区不在」还是「区在但没这张卡」再报 ——
        # 两者运营动作相反,见 explain_activity_card_missing。
        observed = dict(probe_activity_section(page))
        observed["scrolls"] = _ACTIVITY_REVEAL_SCROLLS
        observed.update(panel_meta)
        return {
            "status": "error",
            "reason": explain_activity_card_missing(name, observed),
            "observed": observed,
        }
    state = classify_activity_action(action_text)
    if state == "linked":
        # 本来就关联着 —— 绝不点「取消关联」
        return {"status": "skipped", "activity_id": str(activity_id), "name": name,
                "via": via, "reason": "该活动本就已关联"}
    if state != "unlinked":
        return {
            "status": "error",
            "reason": f"activity_action_unexpected: 按钮文案是 {action_text!r},"
                      f"既不是「{_ACTIVITY_UNLINKED_TEXT}(活动)」也不是"
                      f"「{_ACTIVITY_LINKED_TEXT}(活动)」,拒绝点击",
        }

    for attempt in range(1, _ACTIVITY_CLICK_ATTEMPTS + 1):
        card = _find_activity_card(page, name)
        action = card.query_selector(_ACTIVITY_ACTION) if card is not None else None
        if action is None:
            return {
                "status": "error",
                "reason": f"activity_action_detached: 第 {attempt} 次尝试时「{name}」的按钮不见了",
            }
        if classify_activity_action(action.inner_text()) != "unlinked":
            break  # 已经翻转(或状态变了),交下面统一复核
        human.click(action, reason=f"关联活动「{name}」(第 {attempt} 次)")
        if _wait_activity_flip(page, name):
            logger.info(f"[note_components] ✓ 活动「{name}」已关联(第 {attempt} 次点击生效)")
            return {"status": "done", "activity_id": str(activity_id), "name": name,
                    "via": via, "clicks": attempt}
        logger.warning(
            f"[note_components] 活动「{name}」第 {attempt} 次点击静默失效"
            f"(文案未翻转、无 toast),重试同一个活动"
        )

    if _activity_linked(page, name):
        return {"status": "done", "activity_id": str(activity_id), "name": name,
                "via": via}
    return {
        "status": "error",
        "reason": f"activity_not_linked: 点了 {_ACTIVITY_CLICK_ATTEMPTS} 次「{name}」"
                  f"({via}),按钮始终没翻转成「{_ACTIVITY_LINKED_TEXT}」(静默失效)",
        "observed": read_activity_action_text(page, name),
    }


def _wait_activity_flip(page, name: str) -> bool:
    """轮询等按钮文案翻转成「取消关联」。

    每跳都**重新按名字找卡**:关联成功后该卡会被顶到第一位(实测),持有旧 handle 会读到
    已经 detach 的元素。
    """
    deadline = time.monotonic() + _ACTIVITY_FLIP_TIMEOUT_S
    while time.monotonic() < deadline:
        page.wait_for_timeout(400)
        if _activity_linked(page, name):
            return True
    return False


# ---------------- 编辑器内设置(发布 / 更新共用) ----------------


# ── 原创声明(发布链无条件开;夹具 tests/fixtures/pages/content_settings.json 实证)──
_ORIGINAL_ROW = ".original-wrapper"
_ORIGINAL_SWITCH = ".original-wrapper .d-switch"
_ORIGINAL_MODAL_CLOSE = "[class*='d-modal-close']"
_ORIGINAL_CHECKED_JS = (
    "() => { const b = document.querySelector('.original-wrapper .d-switch input');"
    " return b ? b.checked : null; }"
)
_ORIGINAL_TOGGLE_TRIES = 2
# ── 原创声明的**协议弹窗**(2026-08-07 用户实拍 + 夹具复核) ──
# 夹具 content_settings.json 里这个弹窗一直都在,只是被读成了"无底栏 = 没有确认按钮":
# 它的 class 确实带 d-modal-no-footer,但「声明原创」按钮在 .d-modal-content **里**,
# 不在底栏。必须勾「我已阅读并同意《原创声明须知》」→ 按钮解禁 → 点它,才算真声明。
_ORIGINAL_MODAL = ".d-modal.creator-modal-style"
_ORIGINAL_CONSENT_TEXT = "我已阅读并同意"
_ORIGINAL_CONFIRM_TEXT = "声明原创"
# 协议复选框:首选已由**发布页真号探针**证实(account10,2026-08-07,
# tests/fixtures/pages/original_modal_publish_page.json):可点的是那个
# `div.d-grid.d-checkbox.d-checkbox-main-label.d-clickable`,里面的 input[type=checkbox]
# 是 0×0 隐藏节点(点不着)。后两个候选留作平台改版时的兜底,全不命中即 fail-loud。
_ORIGINAL_CONSENT_CANDIDATES = (
    ".d-modal.creator-modal-style .d-checkbox.d-checkbox-main-label",
    ".d-modal.creator-modal-style .d-checkbox.d-clickable",
    ".d-modal.creator-modal-style [class*='checkbox']",
)
# 「声明原创」按钮真值(探针 account10);文案匹配作兜底
_ORIGINAL_CONFIRM_BUTTON = ".d-modal.creator-modal-style button.custom-button.bg-red"
# 勾选态**看模拟器元素的 class 有没有 unchecked** —— 隐藏 input 的 rect 是 0×0、
# 拿不到也点不着,与大开关同套路(探针 account10 实测)。
_ORIGINAL_CONSENT_SIMULATOR = ".d-modal.creator-modal-style .d-checkbox-simulator"
# 「声明原创」按钮当前可点否(三处 disabled 写法都读,与 step7 同口径)
_ORIGINAL_CONFIRM_ENABLED_JS = r"""(text) => {
    const modal = document.querySelector('.d-modal.creator-modal-style');
    if (!modal) return null;
    const isDisabled = (el) => !!(
        el.disabled ||
        /(^|\s)disabled(\s|$)/.test(el.getAttribute('class') || '') ||
        el.getAttribute('aria-disabled') === 'true'
    );
    for (const el of modal.querySelectorAll('button, div, span, a')) {
        if ((el.textContent || '').trim() !== text) continue;
        const r = el.getBoundingClientRect();
        if (r.width <= 0 || r.height <= 0) continue;
        return !isDisabled(el);
    }
    return null;
}"""
# 等按钮解禁的轮询窗口(秒)
_ORIGINAL_CONFIRM_TIMEOUT_S = 8.0


def _complete_original_consent(page, human: SyncHumanActions) -> Dict[str, Any]:
    """走完协议弹窗:勾「我已阅读并同意」→ 等「声明原创」解禁 → 点它。

    返回 ``{"ok": bool, "reason": str, "observed": dict}``。**走不完一律把弹窗关掉**
    —— 残留弹窗会盖住发布按钮(2026-08-02 事故同型),比声明没成更严重。
    """
    observed: Dict[str, Any] = {}
    consent = None
    for selector in _ORIGINAL_CONSENT_CANDIDATES:
        try:
            consent = page.query_selector(selector)
        except Exception:  # noqa: BLE001
            consent = None
        if consent is not None:
            observed["consent_selector"] = selector
            break
    if consent is None:
        return {"ok": False, "observed": observed,
                "reason": "original_consent_checkbox_not_found: 协议弹窗里没找到"
                          "「我已阅读并同意」复选框(选择器候选全未命中)"}
    # 点的是那个 16×16 的 simulator 方块,**不是**上面查到的宽容器。
    # 真号录屏实测(2026-08-07,账号2)发布页协议弹窗三个矩形(页面坐标):
    #   容器 .d-checkbox.d-checkbox-main-label : x=506 y=483 w=508 h=23  中心 (760,494)
    #   simulator 方块 .d-checkbox-simulator   : x=506 y=486 w=16  h=16  中心 (514,494)
    #   链接《原创声明须知》.custom-link        : x=636       w=107      → 页面 636~743
    # 先说清楚**不是**什么原因:"容器太宽点不中小方块"这个假设已被推翻 —— 实测点容器
    # 几何中心 (760,494)(距方块 246px、落在链接之后的纯文字区)照样勾选成功,整个
    # d-clickable 容器都绑同一个 toggle。真问题是**随机偏移撞上了链接**:human.click
    # 默认 random_offset=True,落点取容器宽度 30%~70% 的随机位置,对 w=508 就是页面
    # 658~862,与链接区间 636~743 **重叠 658~743,约占随机区间 40%**。落到
    # <a class="custom-link"> 上时链接吃掉事件、不冒泡到父级 toggle,于是"点在容器里却
    # 没勾上" —— 生产 e2e 就是这么失败的;录屏那次精确点 760,恰在链接右侧 17px 侥幸避开。
    # 方块内部不含链接,点它必然只触发 toggle,把这 40% 风险清零。
    # random_offset=False:16×16 上再叠 ±20% 偏移只有 ±3px 振幅,毫无拟人价值,却把
    # 落点推向方块边缘徒增风险 —— 拟人性由 human 层的贝塞尔移动/悬停/按压时序承担,
    # 不靠这 3px。定位不到方块才回退点宽容器(旧行为,带那 40% 风险,聊胜于不点)。
    try:
        simulator_target = page.query_selector(_ORIGINAL_CONSENT_SIMULATOR)
    except Exception:  # noqa: BLE001
        simulator_target = None
    consent_reason = f"勾选「{_ORIGINAL_CONSENT_TEXT}《原创声明须知》」"
    if simulator_target is not None:
        observed["consent_click_target"] = "simulator"
        human.click(simulator_target, random_offset=False,
                    reason=f"{consent_reason}(点 simulator 方块)")
    else:
        observed["consent_click_target"] = "container"
        human.click(consent, reason=f"{consent_reason}(方块没定位到,回退点容器)")
    human.wait(0.4, 0.9, context="等「声明原创」按钮解禁")
    # 回读勾选态:模拟器 class 掉了 unchecked 才算真勾上(隐藏 input 0×0 不可用)
    try:
        simulator = page.query_selector(_ORIGINAL_CONSENT_SIMULATOR)
        simulator_class = (
            simulator.get_attribute("class") if simulator is not None else None
        )
    except Exception:  # noqa: BLE001
        simulator_class = None
    observed["consent_simulator_class"] = simulator_class
    ticked = consent_ticked_from_simulator_class(simulator_class)
    observed["consent_ticked"] = ticked
    # 读态没确认就**不往下走**:再等 8s 按钮解禁只是把同一个失败拖成另一个 reason,
    # 还把真因(没勾上)糊成"按钮没解禁"。读不到 class 也算没勾上(读不到 ≠ 好了)。
    if not ticked:
        return {"ok": False, "observed": observed,
                "reason": "original_consent_not_ticked: 点了协议复选框但回读 simulator "
                          f"class={simulator_class!r} 仍是未勾态"}

    deadline = time.monotonic() + _ORIGINAL_CONFIRM_TIMEOUT_S
    enabled = None
    while time.monotonic() < deadline:
        try:
            enabled = page.evaluate(_ORIGINAL_CONFIRM_ENABLED_JS, _ORIGINAL_CONFIRM_TEXT)
        except Exception:  # noqa: BLE001
            enabled = None
        if enabled:
            break
        page.wait_for_timeout(300)
    observed["confirm_enabled"] = enabled
    if not enabled:
        return {"ok": False, "observed": observed,
                "reason": f"original_confirm_never_enabled: 勾了同意但「{_ORIGINAL_CONFIRM_TEXT}」"
                          f"{_ORIGINAL_CONFIRM_TIMEOUT_S:.0f}s 内没解禁"}

    try:
        button = page.query_selector(_ORIGINAL_CONFIRM_BUTTON)
    except Exception:  # noqa: BLE001
        button = None
    if button is None:  # 真值选择器没命中就退回按文案找(平台改 class 时的兜底)
        button = _find_text_in_section(page, _ORIGINAL_MODAL, _ORIGINAL_CONFIRM_TEXT)
    if button is None:
        return {"ok": False, "observed": observed,
                "reason": f"original_confirm_not_found: 读到按钮可点,但按文案取不到"
                          f"「{_ORIGINAL_CONFIRM_TEXT}」元素"}
    human.click(button, reason=f"点「{_ORIGINAL_CONFIRM_TEXT}」完成声明")
    human.wait(0.6, 1.2, context="等协议弹窗关闭")
    try:
        still_open = page.query_selector(_ORIGINAL_MODAL) is not None
    except Exception:  # noqa: BLE001
        still_open = False
    observed["modal_still_open"] = still_open
    if still_open:
        return {"ok": False, "observed": observed,
                "reason": "original_modal_not_closed: 点了声明原创但弹窗仍在"}
    # 成功终态判据 = **弹窗消失** 且 **开关行回读 checked 为真**,两个都要。
    # 单看弹窗消失不够:点 X 关掉弹窗同样"消失",而探针实测那条路 checked 会被重置成
    # False —— 正是这个差别把"真声明了"和"把弹窗关掉了"区分开。
    try:
        final_checked = page.evaluate(_ORIGINAL_CHECKED_JS)
    except Exception:  # noqa: BLE001
        final_checked = None
    observed["final_checked"] = final_checked
    if final_checked is not True:
        return {"ok": False, "observed": observed,
                "reason": "original_switch_not_on_after_confirm: 弹窗关了但开关行回读"
                          f"checked={final_checked!r},不是开态"}
    return {"ok": True, "observed": observed, "reason": ""}


def _close_original_modal(page, human: SyncHumanActions) -> None:
    """兜底关掉协议弹窗(绝不能让它留着盖住发布按钮);点不动就算了。"""
    try:
        close = page.query_selector(_ORIGINAL_MODAL_CLOSE)
        if close is not None:
            human.click(close, reason="关掉原创声明协议弹窗(链走不完的兜底)")
            human.wait(0.4, 0.9, context="等弹窗关闭")
    except Exception:  # noqa: BLE001 — 兜底动作失败不额外制造异常
        pass


def apply_original_declaration(
    page, human: SyncHumanActions, *, handle_consent_modal: bool = False
) -> Dict[str, Any]:
    """打开「原创声明」开关 → ``{"status": "done"|"skipped"|"error", ...}``。

    夹具实证(2026-08-05 真号采集 content_settings.json):
    - 开关 ``.original-wrapper .d-switch``,状态在隐藏 ``input.checked``(class 不翻转,
      不能拿 class 判态);
    - 首次点开会弹**无底栏**说明弹窗(``d-modal-no-footer``,只有右上 X,没有确认按钮),
      弹窗必须关掉——弹窗不关会盖住发布按钮(2026-08-02 事故同型);
    - 已是开态 → ``skipped`` 零点击(判「现在是什么状态」不判「变了没有」,幂等重跑安全)。

    ``handle_consent_modal=True``(视频路径用)走**协议弹窗链**:勾「我已阅读并同意」→
    等「声明原创」解禁 → 点它 → 弹窗消失才算 done。

    ⚠️ **为什么此时不能拿 checked 当终判**:夹具 content_settings.json 铁证 ——
    ``original_row.checkbox_checked=False`` → 点开关后 ``after_toggle_on.checkbox_checked
    =True`` 而**同一快照里弹窗仍 visible**。隐藏 input.checked 是**乐观 UI 态**,不是
    "已声明"的证据;拿它当终判 = 关掉弹窗什么都没声明却报成功。故开启本参数后,
    ``done`` 的依据是"协议链真走完了",checked 只作 ``observed`` 佐证。

    ``handle_consent_modal=False``(缺省,图文生产路径)维持上线前行为:弹窗出现就 X 关掉、
    以回读 checked 为终判。**图文发布页是否也弹这个协议弹窗尚无真号证据**,在拿到证据前
    不动正在跑的生产路径(见交给 lead 的单独报告)。
    """
    if page.query_selector(_ORIGINAL_ROW) is None:
        return {"status": "error",
                "reason": "original_entry_not_found: 页面没有「原创声明」入口"}
    if page.evaluate(_ORIGINAL_CHECKED_JS) is True:
        return {"status": "skipped", "observed": "already_on"}
    for _ in range(_ORIGINAL_TOGGLE_TRIES):
        toggle = page.query_selector(_ORIGINAL_SWITCH)
        if toggle is None:
            return {"status": "error",
                    "reason": "original_switch_not_found: 原创声明行里没有开关"}
        # 不用 scroll_into_view_if_needed:视口外的行会被它滚到底边=正好滚进
        # 发布钮遮挡带(根因见 _scroll_row_to_mid_viewport)
        _scroll_row_to_mid_viewport(page, human, _ORIGINAL_SWITCH)
        toggle = page.query_selector(_ORIGINAL_SWITCH) or toggle
        human.click(toggle, reason="打开原创声明开关")
        human.wait(0.8, 1.5, context="等原创声明开关/弹窗反应")

        if handle_consent_modal:
            try:
                modal_present = page.query_selector(_ORIGINAL_MODAL) is not None
            except Exception:  # noqa: BLE001
                modal_present = False
            if modal_present:
                outcome = _complete_original_consent(page, human)
                if outcome["ok"]:
                    return {"status": "done", "observed": {
                        "via": "consent_modal",
                        "checked": page.evaluate(_ORIGINAL_CHECKED_JS),
                        **outcome["observed"],
                    }}
                _close_original_modal(page, human)
                return {"status": "error", "reason": outcome["reason"],
                        "observed": {"via": "consent_modal", **outcome["observed"]}}
            # 没弹协议弹窗:退回读 checked 的老判据(发布页可能就是不弹)
            if page.evaluate(_ORIGINAL_CHECKED_JS) is True:
                return {"status": "done", "observed": {"via": "no_modal",
                                                       "checked": True}}
            continue

        close = page.query_selector(_ORIGINAL_MODAL_CLOSE)
        if close is not None:
            try:
                human.click(close, reason="关掉原创声明说明弹窗(无底栏仅 X)")
                human.wait(0.5, 1.0, context="等弹窗关闭")
            except Exception:  # noqa: BLE001 — 残留隐藏节点点不动就算了,以回读为准
                pass
        if page.evaluate(_ORIGINAL_CHECKED_JS) is True:
            return {"status": "done", "observed": "checked_on"}
    return {"status": "error",
            "reason": f"original_not_applied: 点了 {_ORIGINAL_TOGGLE_TRIES} 轮开关,"
                      "回读 checked 仍不是开态"}


def apply_components(
    page,
    human: SyncHumanActions,
    responses: ComponentResponses,
    *,
    collection_id: Optional[str] = None,
    collection_name: Optional[str] = None,
    remove_collection_id: Optional[str] = None,
    remove_collection_name: Optional[str] = None,
    quoted_note_id: Optional[str] = None,
    quoted_note_is_own: Optional[bool] = None,
    activity_id: Optional[str] = None,
) -> Dict[str, Dict[str, Any]]:
    """在**已打开的编辑器页**上设置三组件,逐项返回结果(单项失败不阻断其余项)。

    只处理传了 id 的项;返回形如
    ``{"collection": {"status": "done"|"skipped"|"error", ...}, ...}``,键只含请求过的组件。

    ``quoted_note_is_own``:被引用那篇在 ``published_notes`` 台账里**是不是本账号自己的**
    (``None``=台账里查不到,即"不知道")。浏览器层查不了库,这个事实必须由服务层带进来;
    它只有一个用处 —— 拦住"本账号笔记走他人 tab"这条必死的降级路(见 ``_block_other_tab_reason``)。

    这里的 ``done`` 是**编辑器内**回读确认(合集区显示了名字 / 引用区出现了标题 / 活动
    按钮翻转成「取消关联」),**不等于**服务端接受 —— 私密笔记的合集绑定会被服务端静默
    丢弃(设计 2.6),那要靠提交后重进页面回读才发现。
    """
    # **移出排第 0**:将来"换合集"分两次请求接力时,顺序才是对的(先摘旧的再挂新的)。
    steps = (
        ("collection_remove", remove_collection_id, lambda cid: _remove_collection(
            page, human, responses, cid, collection_name=remove_collection_name)),
        ("collection", collection_id, lambda cid: _set_collection(
            page, human, responses, cid, collection_name=collection_name)),
        ("quote", quoted_note_id, lambda nid: _set_quote(
            page, human, responses, nid, quoted_note_is_own=quoted_note_is_own)),
        ("activity", activity_id, lambda aid: _set_activity(page, human, responses, aid)),
    )
    outcomes: Dict[str, Dict[str, Any]] = {}
    for key, value, step in steps:
        if not value:
            continue
        if outcomes:
            human.wait(0.8, 1.8, context="组件设置间隔")
        try:
            outcomes[key] = step(str(value))
        except NoteComponentsError:
            # 硬失败(目前只有"移出后冒出未验证弹窗"这一支)**必须打断整单**:页面已处于
            # 不可预期态,再往下走就是带着弹窗去点发布,而那是一次全量覆盖提交。
            raise
        except Exception as exc:  # noqa: BLE001 — 单项异常不阻断其余项
            logger.warning(f"[note_components] {key} 设置异常: {exc}")
            outcomes[key] = {"status": "error", "reason": f"{key}_exception: {exc}"}
        logger.info(
            f"[note_components] {key}: {outcomes[key]['status']} "
            f"{outcomes[key].get('reason', '')}"
        )
    return outcomes


# ---------------- 发布按钮(closed shadow DOM) ----------------


def _publish_host_rect(page) -> Optional[Dict[str, float]]:
    """只读回 ``<xhs-publish-btn>`` 的视口矩形;host 不在/尺寸为 0 → None。"""
    try:
        return page.evaluate(
            "() => { const h = document.querySelector('xhs-publish-btn');"
            " if (!h) return null; const r = h.getBoundingClientRect();"
            " return {x: r.x, y: r.y, w: r.width, h: r.height,"
            "  ih: window.innerHeight}; }"
        )
    except Exception:  # noqa: BLE001
        return None


def _red_centroid(page, rect: Dict[str, float]) -> Optional[tuple]:
    """在 host 像素带内按「小红书红」求质心,返回 CSS 坐标;红像素太少返回 None。

    与 ``atomic_tasks.step7`` 同款算法(阈值、DPR 换算一模一样)。**故意各写一份**:
    step7 那份与发布流程的级联重试/防重复发布语义缠在一起,抽公共函数要动那条真号验证
    过的主路径,风险高于这几行重复。
    """
    try:
        from PIL import Image
    except Exception:  # noqa: BLE001 — 没装 PIL 就退回 host 比例回退点
        logger.warning("[note_components] 未装 PIL,发布按钮无法做颜色定位")
        return None
    try:
        image = Image.open(BytesIO(page.screenshot())).convert("RGB")
        width, height = image.size
        viewport = page.evaluate(
            "() => ({iw: innerWidth, ih: innerHeight, dpr: window.devicePixelRatio || 1})"
        )
        scale = width / max(1, viewport["iw"])  # 物理px / CSSpx
        pixels = image.load()
        x0 = max(0, int(rect["x"] * scale))
        x1 = min(width, int((rect["x"] + rect["w"]) * scale))
        y0 = max(0, int(rect["y"] * scale))
        y1 = min(height, int((rect["y"] + rect["h"]) * scale))
        xs, ys = [], []
        for yy in range(y0, y1):
            for xx in range(x0, x1):
                r, g, b = pixels[xx, yy]
                if r > 180 and g < 120 and b < 140 and (r - g) > 90 and (r - b) > 60:
                    xs.append(xx)
                    ys.append(yy)
        logger.info(
            f"[note_components] 红按钮检测 vp={viewport} shot=({width}x{height}) "
            f"scale={scale:.3f} 红像素n={len(xs)}"
        )
        if len(xs) < _RED_MIN_PIXELS:
            return None
        return ((sum(xs) / len(xs)) / scale, (sum(ys) / len(ys)) / scale)
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[note_components] 红按钮检测失败: {exc}")
        return None


def _element_tag_at(page, x: float, y: float) -> Optional[str]:
    """只读回该坐标上最上层元素的 tagName(落点复核用)。"""
    try:
        return page.evaluate(
            "([x, y]) => { const e = document.elementFromPoint(x, y);"
            " return e ? e.tagName : null; }",
            [x, y],
        )
    except Exception:  # noqa: BLE001
        return None


def click_publish(page, human: SyncHumanActions) -> None:
    """点发布(closed shadow DOM):host 矩形 → 红质心 → ``elementFromPoint`` 复核 → 拟人点。

    **不得写死坐标**:组件设置会改变页面高度顶动按钮,每次按 host 矩形现算。复核不通过
    (落点不是 ``XHS-PUBLISH-BTN``)一律抛错**不点** —— 这一带上方就是正文与组件区,
    点空了轻则无效、重则改到别的东西。
    """
    rect = _publish_host_rect(page)
    if rect is None:
        raise NoteComponentsError("publish_host_not_found: 页面上没有 <xhs-publish-btn>")
    # 按钮不在视口内就拟人滚动几次(不用 JS scrollIntoView:evaluate 只用于读)
    for _ in range(3):
        if 0 <= rect["y"] and rect["y"] + rect["h"] <= rect["ih"]:
            break
        human.scroll("down")
        human.wait(0.3, 0.7, context="滚到发布栏")
        rect = _publish_host_rect(page) or rect

    centroid = _red_centroid(page, rect)
    if centroid is None:
        raise NoteComponentsError(
            "publish_button_not_located: host 像素带内没找到足够的小红书红像素,"
            "拒绝按比例猜坐标落点"
        )
    x, y = centroid
    tag = _element_tag_at(page, x, y)
    if tag != _PUBLISH_HOST.upper():
        raise NoteComponentsError(
            f"publish_point_mismatch: 质心 ({x:.0f},{y:.0f}) 上是 {tag!r} 而非 "
            f"{_PUBLISH_HOST.upper()},拒绝落点"
        )
    human.wait(0.4, 1.0, context="确认要提交的内容")
    human.click((x, y), reason="发布(closed shadow 红色质心)")
    logger.info(f"[note_components] 已点发布 @({x:.0f},{y:.0f})")


def _wait_submitted(page, responses: ComponentResponses, seen: int) -> Optional[dict]:
    """等那条 ``note/update`` 的 PUT 响应(被动读,不构造)。超时返回 None。"""
    deadline = time.monotonic() + _SUBMIT_TIMEOUT_S
    while time.monotonic() < deadline:
        if responses.count(_UPDATE_API_MARK) > seen:
            return responses.latest(_UPDATE_API_MARK)
        page.wait_for_timeout(400)
    return None


# ---------------- 破坏性编辑步(标题 / 正文 / 图片增删)的编排 ----------------


def _image_gate_reason(
    page, images_before: Optional[int], expected_image_count: Optional[int]
) -> Optional[str]:
    """图片两步的**共用前提**(编辑设计 4.3 前提②):图数可确认 + 页面实数 == expected。

    返回 ``None`` = 闸过;返回字符串 = 拒绝原因,此时**一次 hover、一次点击都没发生**。
    它不是"某一步失败"而是"前提不成立",所以两个图片步会记同一条原因。

    台账认知过期(别处改过这篇、或调用方拿的是旧快照)时,下标语义整个失效——"第 3 张"
    已经不是它以为的那张了。此时唯一安全的动作是零点击退出,而不是"尽力删删看"。
    """
    if images_before is None:
        return ("image_count_unconfirmable: 留底时图数双判据没取到一致值,图数不可确认,"
                "一次图片点击都不发(数错一张就等于删错一张,而误删不可逆)")
    if expected_image_count is None:
        return ("expected_image_count_missing: 请求了图片增删却没声明 expected_image_count,"
                "无从判断调用方对这篇的认知是否过期,零点击退出")
    gate = image_gate(page, expected_image_count)
    if gate.get("status") != "ok":
        return gate.get("reason")
    return None


def _run_edit_steps(
    page,
    human: SyncHumanActions,
    *,
    title: Optional[str],
    content: Optional[str],
    add_images: Optional[List[str]],
    remove_image_indexes: Optional[List[int]],
    expected_image_count: Optional[int],
    images_before: Optional[int],
) -> Dict[str, Any]:
    """按 ``_EDIT_STEP_KEYS`` 顺序跑破坏性编辑步;**任一失败立刻停手**(编辑设计 4.4)。

    Returns:
        ``{"outcomes": {键: 步骤结果}, "aborted": bool, "abort_reason": str|None,
        "removed": 实删数, "added": 实增数, "topics_dropped": list|None}``。

    与组件步的失败语义**故意不同**:组件失败只是"没设上",原内容无损,所以部分成也提交;
    而这里失败意味着编辑器里躺着**残缺态**(标题清了一半、图删了一张卡住),提交出去就是
    把残缺真发布——不可逆。所以任一步 error 就整单弃提交,由调用方在⑦之前返回。
    安全底座是附录 C / E4 实证:编辑器内的改动是纯前端态,不提交就不落库,笔记原样未动。

    ``title`` 用 ``is not None`` 判"有没有请求":``title=""`` 是"清空标题"这一合法意图
    (编辑设计 3.1),真值判断会把它静默丢掉。
    """
    plan = [
        key for key, requested in (
            ("image_remove", bool(remove_image_indexes)),
            ("image_add", bool(add_images)),
            ("title", title is not None),
            ("content", content is not None),
        ) if requested
    ]
    # 闸在**任何**图片动作之前判一次(编辑设计 4.3):不过就一步都不做
    gate_reason = (
        _image_gate_reason(page, images_before, expected_image_count)
        if any(key in _IMAGE_STEP_KEYS for key in plan) else None
    )

    outcomes: Dict[str, Dict[str, Any]] = {}
    aborted = False
    abort_reason: Optional[str] = None
    removed = 0
    added = 0
    topics_dropped: Optional[List[str]] = None

    for key in plan:
        if gate_reason and key in _IMAGE_STEP_KEYS:
            outcomes[key] = {"status": "error", "reason": gate_reason}
            aborted = True
            abort_reason = abort_reason or gate_reason
            continue
        if aborted:
            outcomes[key] = {"status": "error", "reason": _SKIPPED_REASON}
            continue
        try:
            if key == "image_remove":
                outcome = remove_images_step(page, human, list(remove_image_indexes))
                removed = int(outcome.get("removed") or 0)
            elif key == "image_add":
                outcome = add_images_step(page, human, list(add_images))
                added = int(outcome.get("added") or 0)
            elif key == "title":
                outcome = apply_title_edit(page, human, title)
            else:
                outcome = apply_content_edit(page, human, content)
                # 正文整体替换会把既有话题实体一并冲掉,本期不重建、只如实上报(编辑设计 1.2)。
                # 失败路径也带得出来(替换前就取好了),所以不判 status。
                topics_dropped = list(outcome.get("topics_dropped") or [])
        except Exception as exc:  # noqa: BLE001 — 异常也必须收敛成"这步失败 → 弃提交"
            # 绝不能让异常穿出去:穿出去就丢了 aborted_before_submit,调用方无从知道
            # "笔记原样未动、可安全重试"这件事(与"提交了但部分生效"是完全不同的处境)。
            logger.exception(f"[note_components] 编辑步 {key} 异常")
            outcome = {"status": "error", "reason": f"{key}_exception: {exc}"}
        outcomes[key] = outcome
        logger.info(
            f"[note_components] 编辑步 {key}: {outcome.get('status')} "
            f"{outcome.get('reason', '')}"
        )
        if outcome.get("status") != "done":
            aborted = True
            abort_reason = outcome.get("reason")

    return {
        "outcomes": outcomes,
        "aborted": aborted,
        "abort_reason": abort_reason,
        "removed": removed,
        "added": added,
        "topics_dropped": topics_dropped,
    }


def _skipped_components(
    collection_id: Optional[str], quoted_note_id: Optional[str], activity_id: Optional[str],
    remove_collection_id: Optional[str] = None,
    set_original_declaration: bool = False,
    cover_path: Optional[str] = None,
) -> Dict[str, Dict[str, Any]]:
    """弃提交时组件步一步都没走:请求过的组件如实记「因前序失败未执行」。

    不记的话,``failed`` 里就只剩编辑步,调用方会以为组件已经设上了(其实连弹层都没开)。
    """
    return {
        key: {"status": "error", "reason": _SKIPPED_REASON}
        for key, value in (
            ("collection_remove", remove_collection_id),
            ("collection", collection_id),
            ("quote", quoted_note_id),
            ("activity", activity_id),
            ("original_declaration", set_original_declaration),
            ("cover", cover_path),
        ) if value
    }


# 「零变更」的幂等 skipped 步:编辑器里一个字都没改,不值得为它付一次全量覆盖提交。
# 见 set_note_components ⑥bis。``cover`` 落 skipped 表示"本就是自定义封面"。
_IDEMPOTENT_NOOP_KEYS = ("collection_remove", "original_declaration", "cover")


def _finalize_topics(
    result: Dict[str, Any],
    plan: Dict[str, Any],
    step: Optional[Dict[str, Any]],
    topics_after: Optional[List[str]],
) -> Dict[str, Any]:
    """把补话题的计划 / 编辑器结果 / 回读实况汇总进对外结果,并把话题成败折进整体 status。

    补话题(2026-08-08 需求)语义是**追加**:``plan`` 由 ``plan_topic_appends`` 算好差集。
    这条产品线的失败是静默的,所以话题也**只认回读的平台实况**,不是"点了就算":

    - ``applied.topics`` = 回读到的平台实况**全量**话题列表(``None``=没能回读)——验收
      「补 4 个后结果 5 个、原 1 个保留」看的就是它;
    - ``topics_added`` = ``to_add`` 里回读确认真挂上的;``topics_truncated`` = 因超 10 上限
      没补的;``topics_failed`` = 逐个失败原因(下拉没中 / 回读没确认 / 超上限截断),
      **不连坐**其余话题也不连坐其余组件;
    - status 折算:每个 ``to_add`` 是一个单元(回读确认才 True),``truncated`` 各算一个
      失败单元;``to_add`` 与 ``truncated`` 都空(请求的全已挂)算一个成功单元(幂等成功)。
    """
    to_add = plan["to_add"]
    truncated = plan["truncated"]
    step_failed = {f["tag"]: f for f in (step or {}).get("failed", [])}
    if topics_after is None:
        confirmed: Optional[List[str]] = None
        added: List[str] = []
        missing = list(to_add)
    else:
        confirmed = list(topics_after)
        after_set = set(topics_after)
        added = [t for t in to_add if t in after_set]
        missing = [t for t in to_add if t not in after_set]

    result["applied"]["topics"] = confirmed
    result["topics_existing"] = plan["existing"]
    result["topics_added"] = added
    result["topics_truncated"] = truncated

    failed: List[Dict[str, Any]] = []
    for tag in missing:
        if tag in step_failed:
            extra = {k: v for k, v in step_failed[tag].items() if k not in ("tag", "reason")}
            failed.append({
                "component": f"topic:{tag}",
                "reason": step_failed[tag].get("reason", "topic_dropdown_miss"),
                **extra,
            })
        else:
            failed.append({
                "component": f"topic:{tag}",
                "reason": "topic_not_confirmed_on_readback: 编辑器内点选了但回读平台实况里"
                          "没有(可能被静默丢弃)——先核对当前话题再决定,别盲目重跑",
            })
    for tag in truncated:
        failed.append({
            "component": f"topic:{tag}",
            "reason": f"topic_truncated_over_cap: 现有话题够多,补上会超过 {XHS_MAX_TOPICS} "
                      f"个上限,本个未补(追加语义:先来的先补)",
        })
    result["topics_failed"] = failed
    result["failed"].extend(failed)

    # 折算整体 status:非话题单元(applied 里除 topics 外的三态 bool)+ 话题单元
    non_topic = [v for k, v in result["applied"].items() if k != "topics"]
    topic_units: List[bool] = []
    if to_add or truncated:
        confirmed_set = set(confirmed or [])
        topic_units.extend(tag in confirmed_set for tag in to_add)
        topic_units.extend([False] * len(truncated))
    else:
        topic_units.append(True)  # 请求的全已挂 = 一个成功单元(幂等成功,零点击)
    units = non_topic + topic_units
    oks = sum(1 for v in units if v is True)
    if units and oks == len(units):
        result["status"] = "done"
        result.pop("error", None)
    elif oks:
        result["status"] = "partially_applied"
        result.pop("error", None)
    else:
        result["status"] = "failed"
        result["error"] = (
            "note_components_all_failed: 请求的组件/话题一项都没确认生效;"
            f"逐项原因见 failed({[f['component'] for f in result['failed']]})"
        )
    return result


def _build_readback_summary(
    read_back: Dict[str, Optional[str]],
    images_after: Optional[int],
    add_images: Optional[List[str]],
) -> Dict[str, Any]:
    """从**已有回读结果**零成本算一份"当场可判定"摘要:不额外起浏览器动作、不额外读页面。

    动机(2026-08-09 内容运营取证):编辑长笔记(如调价「¥800→¥600」)后,调用方想立刻确认
    生效内容,却只能 ①自己从完整 ``read_back`` dict 里捞正文再切片,或 ②拿 explore 公开页去比
    —— 而公开页有**分钟级传播滞后**,拿它当即时真值会误判成假绿。这份摘要把提交那刻的编辑器
    回读真值切成可直接判定的小字段,**结构通用**、不含任何业务概念(价格 / 咨询师之类)。

    字段与数据源(全部取自 ``set_note_components`` 已经拿到的回读结果,不再多读一次页面):

    - ``content_length`` / ``content_head`` / ``content_tail`` / ``topics_count``:取自
      ``read_back["content"]``(提交后重进页面的正文回读真值);仅在**正文被编辑过**时有值,
      否则为 None。``content_tail`` 给末 40 字(调用方从这看到末句 / 价格行),``content_head``
      给头 30 字(看开头改没改),``topics_count`` 用 ``extract_topics`` 数正文里的话题实体;
    - ``image_count``:提交后回读的图数(``images_after`` —— ``count_images`` 的双判据计数);
    - ``last_image``:本次**追加**的最后一张本地图的 basename。⚠️ 回读侧只拿得到图**数** ——
      ``count_images`` 与快照 image_count 都是纯计数,更新页上**没有零成本可读的 file_id / URL**
      (要读得额外遍历一趟 DOM,违背"零成本纯从已有结果算")。故这里给的是**请求侧**"我最后
      追加的那张"的可辨识名,让调用方对上"末图是不是我传的那张";纯删图(本次没追加)时为 None。
    """
    body = (read_back or {}).get("content")
    return {
        "content_length": len(body) if body is not None else None,
        "content_head": body[:30] if body is not None else None,
        "content_tail": body[-40:] if body is not None else None,
        "topics_count": len(extract_topics(body)) if body is not None else None,
        "image_count": images_after,
        "last_image": os.path.basename(add_images[-1]) if add_images else None,
    }


# ---------------- 编辑已发布笔记:完整流程 ----------------


def set_note_components(
    page,
    account_id: int,
    note_id: str,
    *,
    collection_id: Optional[str] = None,
    collection_name: Optional[str] = None,
    remove_collection_id: Optional[str] = None,
    remove_collection_name: Optional[str] = None,
    quoted_note_id: Optional[str] = None,
    quoted_note_is_own: Optional[bool] = None,
    activity_id: Optional[str] = None,
    title: Optional[str] = None,
    content: Optional[str] = None,
    add_images: Optional[List[str]] = None,
    remove_image_indexes: Optional[List[int]] = None,
    expected_image_count: Optional[int] = None,
    set_original_declaration: bool = False,
    cover_path: Optional[str] = None,
    topics: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """给一篇**已发布**笔记设组件 / 改标题正文 / 增删图片 / 换封面 / 补挂话题:进更新页 → 改 → 提交 → 回读。

    **全流程只有一次提交**(⑧那一次 ``click_publish``),这是方案 A 的立命之本:提交是
    全量覆盖语义,提交次数就是风险次数。文本 / 图片 / 组件混在一次请求里也只覆盖一次。

    Args:
        page: 已建好登录态的同步 Playwright Page(SyncClient.start 之后)。
        account_id: 账号 id(日志用)。
        note_id: 目标笔记的平台 id(深链定位,设计 3.2 —— 台账 title 会过期,只认 id)。
        collection_id / quoted_note_id / activity_id: 要设置的三组件,均可选。
        quoted_note_is_own: 被引用那篇在 ``published_notes`` 台账里是不是**本账号自己**的
            (``None``=台账查不到)。只用来拦"本账号笔记走他人 tab"这条必死的降级路,
            由服务层查库后带进来(浏览器层不碰 DB)。
        remove_collection_id: 要**移出**的合集 id(与 collection_id 加入对称,幂等:
            本就不在 → skipped 不算失败)。``remove_collection_name`` 强烈建议同传 ——
            浏览器层靠名字确认"当前所在合集就是目标",比对不上绝不动手。
        title: 整体替换标题;``None``=不改,``""``=清空(两者语义不同,编辑设计 3.1)。
        content: 整体替换正文;``None``=不改(不支持清空,编辑设计 1.2)。
        add_images: 追加到图序末尾的**本地路径**列表(REST 层已落好盘)。
        remove_image_indexes: 按发布态图序删除的 1-based 下标。
        expected_image_count: 调用方声明的现有图数;有图片操作时必给,与页面实数不符
            → 一次图片点击都不发生,整单弃提交(编辑设计 4.3)。
        set_original_declaration: 给这篇**补录原创声明**(只开不关)。走的是与发布链
            **同一个** ``apply_original_declaration(handle_consent_modal=True)``——
            08-07 那个"拟人随机偏移 40% 概率撞上《原创声明须知》超链接"的修复在那个函数
            **内部**,所以编辑链共用它就自动覆盖了同一个修复,不会重蹈 08-05~08-07 的覆辙。
            幂等:进页面先读当前态,已是开态 → ``skipped`` 且**零点击**。
        cover_path: 给这篇换**自定义封面**的本地图片路径(只对视频笔记有效 —— 图文笔记的
            封面就是第一张图,更新页上根本没有封面区)。走更新页「设置封面」弹窗
            (``apply_cover_change``),与发布页那条内联链是两个操作面。幂等:已经是
            自定义封面 → ``skipped`` 且**零点击**;失败**不阻断**其余组件。
        topics: 给这篇**补挂话题**(2026-08-08,存量视频笔记话题空置的补救)。语义是
            **追加**不是替换 —— 先读现有话题算差集(``plan_topic_appends``),只补差集、
            去重、总数 >10 截断(全量替换在已有话题的笔记上太危险)。话题输入走发布链同一套
            正向判据(``append_topics`` → ``topic_dropdown.select_topic_option``),逐个独立
            成败、失败回删不留残缺、**不连坐**其余话题也不弃提交。回读判据是**平台实况**:
            提交后重进页面读正文里的话题实体,``applied.topics`` 反映真挂上的全量话题
            (不是"点了就算")。请求的全已挂 → 零点击零提交(幂等)。

    Returns:
        ``{"status": "done"|"partially_applied"|"failed", "applied": {...}, "failed": [...],
        "permission_before"/"permission_after"/"permission_preserved", "body_appended",
        "topics_injected", "submitted", "aborted_before_submit", "components": {逐项明细}}``;
        请求了编辑字段时按需带 ``topics_dropped`` / ``images_before`` / ``images_after`` /
        ``read_back``(标题正文的回读真值,服务层台账回写要用)。
        一项都没生效时额外带 ``"error"`` 键(调用方据此把台账落 error,而不是假 done)。

        ``aborted_before_submit=True`` 是**独立于"提交了但没全成"的终态**:破坏性编辑步
        失败导致放弃提交,一次发布都没点、编辑器态不落库,笔记原样未动 —— 调用方可以直接
        重试。其余失败必须先人工核对笔记现状(编辑设计 3.2 / 6.3)。

    Raises:
        NoteComponentsError: 前置/硬失败 —— 页面进不去、权限读不出、提交前权限已变。
            这几种情况下**一次发布都没点**,笔记原样未动。
    """
    human = SyncHumanActions(page)
    responses = ComponentResponses()
    responses.attach(page)
    wants_images = bool(add_images or remove_image_indexes)
    try:
        open_update_page(page, account_id, note_id)
        human.wait(0.8, 1.6, context="编辑页浏览")

        # ① 权限只读留底:读不出就中止 —— 提交是全量覆盖语义,权限不可确认时不许提交
        permission_before = read_permission_label(page)
        if permission_before is None:
            raise NoteComponentsError(
                "permission_unreadable: 读不到权限档位,拒绝编辑"
                f"(选择器 {_PERMISSION_DESC});提交是全量覆盖语义,权限不可确认就不能提交"
            )
        title_on_page = read_note_title(page)
        body_before = read_body_text(page)
        # 图数留底**只在真要动图时数**:不动图就没有"数错一张=删错一张"的风险敞口,
        # 也不必为纯组件请求多跑一次清点。None = 不可确认,由 ③ 的闸拦下(编辑设计 4.3)。
        images_before = count_images(page) if wants_images else None
        logger.info(
            f"[note_components] 账号{account_id} note_id={note_id} "
            f"权限={permission_before!r} 标题={title_on_page[:20]!r} 图数={images_before}"
        )

        # ②③④ 破坏性编辑步(图片闸 → 删图 → 加图 → 标题 → 正文),任一失败即停手
        edits = _run_edit_steps(
            page, human,
            title=title, content=content, add_images=add_images,
            remove_image_indexes=remove_image_indexes,
            expected_image_count=expected_image_count, images_before=images_before,
        )
        if edits["aborted"]:
            # 弃提交(编辑设计 4.4):**不走组件步、不点发布**,直接返回。此刻编辑器里可能
            # 躺着残缺态,但它是纯前端的(附录 C / E4 实证),离开页面即恢复原状。
            logger.error(
                f"[note_components] 账号{account_id} note_id={note_id}: 破坏性编辑步失败,"
                f"放弃提交(一次发布都没点,笔记原样未动):{edits['abort_reason']}"
            )
            return _compose(
                note_id,
                _skipped_components(collection_id, quoted_note_id, activity_id,
                                    remove_collection_id, set_original_declaration,
                                    cover_path),
                {}, submitted=False,
                permission_before=permission_before, permission_after=permission_before,
                body_before=body_before, body_after=body_before,
                edit_outcomes=edits["outcomes"], images_before=images_before,
                images_after=None, topics_dropped=edits["topics_dropped"],
                aborted_before_submit=True, abort_reason=edits["abort_reason"],
            )

        # ⑤ 三组件逐项设置(单项失败不阻断其余项 —— 与破坏性编辑步的语义差异见 4.4)
        outcomes = apply_components(
            page, human, responses,
            collection_id=collection_id,
            collection_name=collection_name,
            remove_collection_id=remove_collection_id,
            remove_collection_name=remove_collection_name,
            quoted_note_id=quoted_note_id,
            quoted_note_is_own=quoted_note_is_own,
            activity_id=activity_id,
        )

        # ⑤bis 原创声明补录(运营 2026-08-08 来文:08-05~08-07 那 49 篇要补上标记)。
        # **复用发布链同一个函数**,不另写一份协议弹窗逻辑 —— 08-07 那个修复(点 16×16 的
        # .d-checkbox-simulator 方块而非宽容器,躲开 40% 概率撞上《原创声明须知》超链接)
        # 就在 apply_original_declaration 内部,共用它 = 修复自动覆盖编辑链。运营特意提醒
        # "确认那个修复也覆盖了编辑链",答案就是这一行:同一个函数,没有第二份实现。
        # 位置与发布链一致(组件之后),失败仅告警不阻断其余组件。
        if set_original_declaration:
            try:
                outcomes["original_declaration"] = apply_original_declaration(
                    page, human, handle_consent_modal=True,
                )
            except Exception as exc:  # noqa: BLE001 — 辅助步绝不阻断其余组件
                outcomes["original_declaration"] = {
                    "status": "error", "reason": f"original_exception: {exc}"}
            logger.info(
                f"[note_components] 原创声明补录: "
                f"{outcomes['original_declaration'].get('status')} "
                f"{outcomes['original_declaration'].get('reason', '')}"
            )

        # ⑤ter 改封面(2026-08-08 上线,只对视频笔记):走更新页「设置封面」弹窗。
        # 与组件同族语义 —— 失败只是"没换上",原封面无损,所以**不阻断**其余组件、也不弃提交;
        # 幂等已是自定义封面则 skipped 且零点击(见 _IDEMPOTENT_NOOP_KEYS)。
        if cover_path:
            try:
                outcomes["cover"] = apply_cover_change(page, human, cover_path)
            except Exception as exc:  # noqa: BLE001 — 辅助步绝不阻断其余组件
                outcomes["cover"] = {"status": "error", "reason": f"cover_exception: {exc}"}
            logger.info(
                f"[note_components] 改封面: {outcomes['cover'].get('status')} "
                f"{outcomes['cover'].get('reason', '')}"
            )

        body_after = read_body_text(page)

        # ⑤quater 补挂话题(2026-08-08,追加语义):**读现有话题 → 算差集 → 只补差集**。
        # 现有话题以 body_after 为准(此刻正文已含活动可能注入的话题,一并去重免得重复挂)。
        # 话题是追加(非破坏性):失败只是"没挂上",正文原内容无损,故**不阻断**其余组件、
        # 也不弃提交。body_after 在**补话题之前**读定,故 topics_injected(活动注入)不会被
        # 我补的话题污染。
        topic_plan: Optional[Dict[str, Any]] = None
        topic_step: Optional[Dict[str, Any]] = None
        if topics:
            topic_plan = plan_topic_appends(extract_topics(body_after), topics)
            if topic_plan["to_add"]:
                try:
                    topic_step = append_topics(page, human, topic_plan["to_add"])
                except Exception as exc:  # noqa: BLE001 — 辅助步绝不阻断其余组件
                    topic_step = {
                        "status": "error", "in_editor_added": [],
                        "failed": [{"tag": t, "reason": f"topics_exception: {exc}"}
                                   for t in topic_plan["to_add"]],
                    }
                logger.info(
                    f"[note_components] 补话题: {topic_step.get('status')} "
                    f"added={topic_step.get('in_editor_added')} "
                    f"truncated={topic_plan['truncated']}"
                )
            else:
                # 请求的全已挂 / 全被上限截掉:一个都不打字(零点击)
                topic_step = {"status": "skipped", "in_editor_added": [], "failed": []}
        topics_added_in_editor = bool(topic_step and topic_step.get("in_editor_added"))

        # ⑥ 提交决策:组件 done/skipped 或**任一编辑步 done** 或**补上了话题**都算"有东西可提交"
        in_editor_ok = [k for k, v in outcomes.items() if v["status"] in ("done", "skipped")]
        edits_ok = [k for k, v in edits["outcomes"].items() if v.get("status") == "done"]
        # ⑥bis **幂等零点击不提交**:``collection_remove`` 落 skipped 表示"本就不在该合集",
        # ``original_declaration`` 落 skipped 表示"本就是开态",两者都是编辑器里一个字
        # 都没改。若这次请求只有这类步算数,就**不点发布** —— 提交是全量覆盖语义,为一次
        # 零变更付一次覆盖风险毫无道理;存量清理 / 批量补录会对上百篇已达标的笔记跑这条路,
        # 每篇白提交一次就是上百次真发布。生效结论直接取编辑器内回读(那本就是"它已经
        # 是目标状态"的直接证据,不需要提交后再确认一遍)。
        noop_skipped = [
            key for key in _IDEMPOTENT_NOOP_KEYS
            if (outcomes.get(key) or {}).get("status") == "skipped"
        ]
        # 补话题不算"要提交的变更"仅当它零点击(全已挂/全截断);补上了话题就得提交。
        if (noop_skipped and set(in_editor_ok) <= set(noop_skipped) and not edits_ok
                and not topics_added_in_editor):
            logger.info(
                f"[note_components] 账号{account_id} note_id={note_id}: "
                f"{noop_skipped} 本就已是目标状态,零点击零提交(幂等)"
            )
            result = _compose(
                note_id, outcomes, {key: True for key in noop_skipped}, submitted=False,
                permission_before=permission_before, permission_after=permission_before,
                body_before=body_before, body_after=body_after,
                edit_outcomes=edits["outcomes"], images_before=images_before,
                images_after=None, topics_dropped=edits["topics_dropped"],
            )
            if topic_plan is not None:
                # 没提交 → 平台实况就是编辑器现有话题(补话题没打任何字)
                _finalize_topics(result, topic_plan, topic_step, topic_plan["existing"])
            return result
        if not in_editor_ok and not edits_ok and not topics_added_in_editor:
            # 编辑器里一项都没设上 —— 提交毫无意义,而每次提交都是一次全量覆盖,不做。
            # (补话题若全已挂 → topic_plan.to_add 空,幂等成功走这里;若请求的词全都下拉
            #  没命中 → in_editor_added 空,也走这里,话题逐项失败原因由 _finalize_topics 给出。)
            logger.warning(
                f"[note_components] 账号{account_id} note_id={note_id}: "
                f"编辑器内一项都没设上,不点发布(避免无意义的全量覆盖提交)"
            )
            result = _compose(
                note_id, outcomes, {}, submitted=False,
                permission_before=permission_before, permission_after=permission_before,
                body_before=body_before, body_after=body_after,
                edit_outcomes=edits["outcomes"], images_before=images_before,
                images_after=None, topics_dropped=edits["topics_dropped"],
            )
            if topic_plan is not None:
                _finalize_topics(result, topic_plan, topic_step, topic_plan["existing"])
            return result

        # ⑦ 点发布前再读一次权限:不符立刻中止,**不点** —— 这是硬约束,不是可选校验
        permission_now = read_permission_label(page)
        if permission_now != permission_before:
            raise NoteComponentsError(
                f"permission_changed_before_submit: 权限档位从 {permission_before!r} 变成 "
                f"{permission_now!r},中止提交(一次发布都没点,笔记原样未动)"
            )

        seen_updates = responses.count(_UPDATE_API_MARK)
        click_publish(page, human)
        submitted = _wait_submitted(page, responses, seen_updates)
        if submitted is None:
            # 没收到提交响应:不补点(提交是全量覆盖,重复点有风险),照常回读看真相
            logger.warning(
                f"[note_components] 账号{account_id} note_id={note_id}: "
                f"{_SUBMIT_TIMEOUT_S}s 内没收到 note/update 响应,直接进回读核实"
            )
        elif not submitted.get("success"):
            logger.warning(f"[note_components] 提交响应非 success: {submitted}")

        # ⑨ 重进更新页逐项回读 —— success:true 不等于生效(设计 2.6 合集被静默丢弃)
        human.wait(1.5, 3.0, context="等提交落地")
        verified, permission_after, readback = _verify_after_submit(
            page, account_id, note_id,
            collection_id=collection_id, remove_collection_id=remove_collection_id,
            quoted_note_id=quoted_note_id,
            activity_id=activity_id, outcomes=outcomes, responses=responses,
            set_original_declaration=set_original_declaration,
            cover_path=cover_path,
            title=title, content=content,
            wants_add=bool(add_images), wants_remove=bool(remove_image_indexes),
            images_before=images_before, removed=edits["removed"], added=edits["added"],
        )

        # 正文被整体替换过时,"活动往正文末尾追加了什么"要拿**替换后**的正文当基线做差
        # (编辑设计 4.2①)。拿留底的旧正文做差会把新正文里调用方自己写的话题统统算成
        # "活动注入的"—— 那是假报,而这条产品线最忌讳的就是假报。
        content_step = edits["outcomes"].get("content") or {}
        diff_baseline = body_before
        if content_step.get("status") == "done" and content_step.get("body_read_back") is not None:
            diff_baseline = _norm(content_step["body_read_back"])

        result = _compose(
            note_id, outcomes, verified, submitted=submitted is not None,
            permission_before=permission_before, permission_after=permission_after,
            body_before=diff_baseline, body_after=body_after,
            edit_outcomes=edits["outcomes"], images_before=images_before,
            images_after=readback["images_after"], topics_dropped=edits["topics_dropped"],
        )
        if readback["read_back"]:
            # 标题/正文的回读真值:服务层台账回写只认它(不拿请求值凑数,编辑设计 3.3)
            result["read_back"] = readback["read_back"]
        # readback_summary:正文/图片被改过时,从上面**已有的回读结果**零成本切一份"当场
        # 可判定"摘要(不再起浏览器动作)。调用方据此直接判生效内容,不必自己捞完整 read_back
        # dict、更不该拿 explore 公开页的即时值验收(公开页有分钟级传播滞后)。没改这两样
        # (纯组件 / 纯话题请求)就不放 —— 对它无意义(编辑设计 3.3)。
        if content is not None or wants_images:
            result["readback_summary"] = _build_readback_summary(
                readback["read_back"], readback["images_after"], add_images
            )
        # ⑨bis 补话题回读:_verify_after_submit 已重进更新页,此刻读正文里的话题实体就是
        # **平台实况**(不是"点了就算")。读不出正文 → None(applied.topics=None,未确认)。
        if topic_plan is not None:
            topics_after = extract_topics(read_body_text(page)) if submitted is not None \
                else topic_plan["existing"]
            _finalize_topics(result, topic_plan, topic_step, topics_after)
        # ⑩ 权限被改动:大声告警 + 尝试改回(只有实测支持的两档能改回)
        if permission_after is not None and permission_after != permission_before:
            result["permission_restored"] = _restore_permission(
                page, account_id, note_id, title_on_page, permission_before, permission_after
            )
        return result
    finally:
        responses.detach()


def _verify_after_submit(
    page,
    account_id: int,
    note_id: str,
    *,
    collection_id: Optional[str],
    quoted_note_id: Optional[str],
    activity_id: Optional[str],
    outcomes: Dict[str, Dict[str, Any]],
    remove_collection_id: Optional[str] = None,
    set_original_declaration: bool = False,
    cover_path: Optional[str] = None,
    responses: ComponentResponses,
    title: Optional[str] = None,
    content: Optional[str] = None,
    wants_add: bool = False,
    wants_remove: bool = False,
    images_before: Optional[int] = None,
    removed: int = 0,
    added: int = 0,
) -> tuple:
    """重进更新页,逐项回读三组件 / 标题 / 正文 / 图数与权限。

    Returns:
        ``({项: bool|None}, 权限文案|None, {"read_back": {...}, "images_after": int|None})``
        —— ``read_back`` 只含请求过的 title/content 键(值为 ``None`` 表示没读到),
        服务层台账回写要用。

    回读本身失败(页面进不去)不抛错 —— 那时"改没改成"确实未知,一律记 None,由调用方
    如实上报为未确认,而不是乐观当成功。
    """
    verified: Dict[str, Optional[bool]] = {}
    read_back: Dict[str, Optional[str]] = {}
    if title is not None:
        read_back["title"] = None
    if content is not None:
        read_back["content"] = None
    extra: Dict[str, Any] = {"read_back": read_back, "images_after": None}
    try:
        open_update_page(page, account_id, note_id)
    except NoteComponentsError as exc:
        logger.error(f"[note_components] 回读进不去更新页(生效情况未确认): {exc.reason}")
        return {k: None for k in outcomes}, None, extra

    if remove_collection_id:
        # 判据与"加入"相反,且**两档证据分开**:空态是强证据(chip 整个没了);
        # "非空但已不含目标名"是弱底线(引用区那次踩过的坑——提交后显示形态可能变,
        # 只认精确空态会假阴性)。名字都不知道时非空态**判不了**,记 None(未确认)而不是
        # 乐观当 True —— 这条产品线的失败是静默的。
        name = _norm((outcomes.get("collection_remove") or {}).get("name") or "")
        current = read_collection_label(page)
        gone = current is None or _COLLECTION_EMPTY_TEXT in current
        if gone:
            verified["collection_remove"] = True
        elif name:
            # 与 ``_remove_collection`` 的比对判据**同一条**:全等才算"还在这个合集里"。
            # 用包含会把「科普合集」判成「科普」还在,同族名下回执直接反过来。
            verified["collection_remove"] = _norm(current) != name
        else:
            verified["collection_remove"] = None
        if verified["collection_remove"] is not True:
            logger.error(
                f"[note_components] 移出合集回读未确认:期望「{name}」不在,实读 {current!r}"
            )
    if collection_id:
        name = (outcomes.get("collection") or {}).get("name") or ""
        current = read_collection_label(page)
        verified["collection"] = bool(name and current and name in current)
        if not verified["collection"]:
            logger.error(
                f"[note_components] 合集回读未生效:期望「{name}」,实读 {current!r}"
                f"(私密笔记的合集绑定会被服务端静默丢弃,见设计 2.6)"
            )
    if quoted_note_id:
        outcome = outcomes.get("quote") or {}
        title = outcome.get("title") or ""
        quoted = read_quote_text(page)
        # **提交后的回读一律用"变了没有",绝不用"包含标题"**(2026-08-03 真号实测):
        # 编辑器里引用区显示的是被引用笔记的**标题**,而提交后重进页面显示的是
        # 「引用 @<作者昵称> 的笔记」—— **根本不含标题**。拿标题去比对必然假阴性:
        # 引用其实设成了,却报「回读未生效」。真号那次就是这么被误判成失败的
        # (in-editor 已 done、submitted=true、权限也没动,唯独这一步判错)。
        #
        # 基线是设置**之前**的引用区文案(无引用时是「引用笔记」这类占位),由设置阶段
        # 带出来(quote_text_before)。判据:非空且与基线不同。
        # 判据是"**现在有没有引用**",不是"跟之前比变了没有":重复给同一篇设同一个引用时
        # 前后文案一模一样,拿变化当判据会把幂等重跑判成失败。
        # 至于"引用的是不是**对**的那一篇",由设置阶段按 note_id 定位 + 标题交叉校验保证
        # (见 _set_quote_in_modal);这里只负责确认它**提交后仍然在**。
        verified["quote"] = bool(quoted) and _norm(quoted) != _QUOTE_EMPTY_TEXT
        if not verified["quote"]:
            logger.error(
                f"[note_components] 引用回读未生效:引用区仍是未设置态,实读 {quoted[:40]!r}"
            )
    if activity_id:
        name = (outcomes.get("activity") or {}).get("name") or ""
        if not name:
            # 设置阶段就没拿到活动名(catalog 不可用),回读时再试一次映射
            catalog = parse_activities(responses.latest(_ACTIVITY_API_MARK))
            name = next(
                (a["name"] for a in catalog if a["id"] == str(activity_id)), ""
            )
        verified["activity"] = _activity_linked(page, name) if name else None
        if not verified["activity"]:
            logger.error(
                f"[note_components] 活动回读未生效:「{name}」按钮不是「{_ACTIVITY_LINKED_TEXT}」"
            )
    if set_original_declaration:
        # 判据 = 重进编辑页把开关行的 checked 再读一遍。三态如实:True / False /
        # 读不到(页面没这行、脚本抛异常)→ None(未确认),绝不乐观当成功。
        # ⚠️ **尚无"已声明笔记的编辑页"夹具**:平台是否在编辑页回显已声明态没有实测证据。
        # 若它不回显,这里会把真声明成功的笔记报成 False —— 首批真号跑完必须人工对一次
        # 平台侧标记再放量,别让一次假阴性引出反复重跑(每次重跑都是一次真提交)。
        try:
            checked = page.evaluate(_ORIGINAL_CHECKED_JS)
        except Exception:  # noqa: BLE001 — 读不到就是未确认,不是失败
            checked = None
        verified["original_declaration"] = checked if isinstance(checked, bool) else None
        if verified["original_declaration"] is not True:
            logger.error(
                f"[note_components] 原创声明回读未确认:开关行 checked={checked!r}"
            )

    if cover_path:
        # 回读判据分层(2026-08-08 账号5 真号首验暴露的过严缺陷):**强信号(指纹变化)成立
        # 即判 true,辅助信号(noCover 消失)读不到时不参与、绝不否决强信号**。
        #   - 强信号:提交前的封面区背景图指纹(``fingerprint_before``,平台首帧的正式 CDN)
        #     变成非空的新指纹 → 封面真换了。这条最可靠 —— 账号5 上 App 已目视确认换成功、
        #     指纹从 sns-na-i2 CDN 变成刚上传的 ros-preview;
        #   - 辅助信号:noCover class 从 True 消失成 False = 平台侧确认换成自定义封面;读不到
        #     (None)时不参与判定,**不能因为辅助信号缺失就把强信号已成立的结果推翻成 false**。
        # 注意用 ``_read_cover_verify_state`` 而非 ``_read_cover_state``:后者 .operator 浮层
        # 缺失就整条返回 None,会把指纹一起丢掉(账号5 正是 .operator 读不到但指纹在)。
        cover_outcome = outcomes.get("cover") or {}
        fp_before = cover_outcome.get("fingerprint_before")
        nc_before = cover_outcome.get("no_cover_before")
        post = _read_cover_verify_state(page)
        fp_after = (post or {}).get("fingerprint") or None
        nc_after = (post or {}).get("no_cover") if isinstance(post, dict) else None
        verified["cover"] = _cover_verified(fp_before, fp_after, nc_before, nc_after)
        extra["cover_observed"] = {
            "fingerprint_before": fp_before, "fingerprint_after": fp_after,
            "no_cover_before": nc_before, "no_cover_after": nc_after,
        }
        if verified["cover"] is not True:
            logger.error(
                f"[note_components] 封面回读未确认:指纹 {fp_before!r}→{fp_after!r}、"
                f"noCover {nc_before!r}→{nc_after!r}(强信号未变、辅助信号未确认消失)"
            )

    # ---- 编辑项回读(编辑设计 3.2 判据)----
    if title is not None:
        got = read_title_value(page)
        read_back["title"] = got
        # 标题用**全等**(归一后):没有"末尾会被追加东西"的情形。读不出 → None(未确认),
        # 绝不当成"清空成功"——`title=""` 时 _norm(None) == _norm("") 会把定位失败谎报成生效。
        verified["title"] = None if got is None else (_norm(got) == _norm(title))
        if verified["title"] is not True:
            logger.error(
                f"[note_components] 标题回读未生效:期望 {title[:40]!r},实读 {got!r}"
            )
    if content is not None:
        got = read_body_value(page)
        read_back["content"] = got
        # **这里才用前缀判据**:活动步已经把话题追加到正文末尾了,全等必然假阴性。
        # 编辑器内那一步用的是全等(note_editing.apply_content_edit),两条判据别混用。
        verified["content"] = None if got is None else content_prefix_ok(got, content)
        if verified["content"] is not True:
            logger.error(
                f"[note_components] 正文回读未生效:实读 {(got or '')[:60]!r} "
                f"不以目标正文 {content[:40]!r} 开头"
            )
    if wants_add or wants_remove:
        images_after = count_images(page)
        extra["images_after"] = images_after
        # 两个键共用同一条计数等式(编辑设计 3.2),分开报只是为了让调用方定位哪半失败
        if images_after is None or images_before is None:
            applied_images = None
            logger.error("[note_components] 图数回读不可确认(双判据没取到一致值)")
        else:
            expected = image_count_equation(images_before, removed, added)
            applied_images = images_after == expected
            if not applied_images:
                logger.error(
                    f"[note_components] 图数回读未生效:实读 {images_after} != 期望 "
                    f"{expected}(留底 {images_before} - 实删 {removed} + 实增 {added})"
                )
        if wants_remove:
            verified["image_remove"] = applied_images
        if wants_add:
            verified["image_add"] = applied_images
    return verified, read_permission_label(page), extra


def _compose(
    note_id: str,
    outcomes: Dict[str, Dict[str, Any]],
    verified: Dict[str, Optional[bool]],
    *,
    submitted: bool,
    permission_before: Optional[str],
    permission_after: Optional[str],
    body_before: str,
    body_after: str,
    edit_outcomes: Optional[Dict[str, Dict[str, Any]]] = None,
    images_before: Optional[int] = None,
    images_after: Optional[int] = None,
    topics_dropped: Optional[List[str]] = None,
    aborted_before_submit: bool = False,
    abort_reason: Optional[str] = None,
) -> Dict[str, Any]:
    """把逐项结果(三组件 + 破坏性编辑步)汇总成对外结果;**一项都没生效时带 ``error`` 键**。

    ``applied[k]`` 三态:``True``=回读确认生效;``False``=回读确认没生效;``None``=没能
    回读(未确认)。**只有 True 才算数** —— 这条产品线的失败是静默的,"没报错"不是凭据。

    编辑相关的键**按请求出现**(``topics_dropped`` / ``images_before`` / ``images_after``):
    纯组件请求的结果与接线之前逐字节一致,老调用方不会凭空多出看不懂的键。唯一的例外是
    ``aborted_before_submit`` —— 它在**所有**结果里都给值(正常路径 False),因为
    "笔记原样未动"与"提交了但没全成"是完全不同的处境,调用方必须能一眼分清(编辑设计 3.2)。
    """
    steps = {**outcomes, **(edit_outcomes or {})}
    applied = {k: verified.get(k) for k in steps}
    # 回读兜底文案区分两态(前缀 *_not_verified 不变,运营按前缀匹配的代码不破;
    # 运营 08-05 需求 §五-1:None=状态未知先核对,False=确认没生效,处置完全不同):
    failed = [
        {"component": k, "reason": (steps[k].get("reason") or (
            f"{k}_not_verified: 提交后回读确认没生效"
            if applied.get(k) is False
            else f"{k}_not_verified: 提交后没能回读(状态未知——先用 "
                 "note-component-reads 核对当前状态再决定,别盲目重跑)"
        )),
         # 步骤自带的取证字段透传(items_seen 等)——白名单式只取 reason 已经吞过三次证据
         # (poll_timeline/caret_rect/items_seen),凡 step 里 reason/status 之外的键一律带上
         **{fk: fv for fk, fv in steps[k].items() if fk not in ("reason", "status")}}
        for k in steps if applied.get(k) is not True
    ]
    appended = appended_part(body_before, body_after)
    result: Dict[str, Any] = {
        "note_id": note_id,
        "submitted": submitted,
        "applied": applied,
        "failed": failed,
        "components": outcomes,
        "permission_before": permission_before,
        "permission_after": permission_after,
        "permission_preserved": (
            permission_after is not None and permission_after == permission_before
        ),
        "body_appended": appended,
        "topics_injected": extract_topics(appended),
        "aborted_before_submit": aborted_before_submit,
    }
    if topics_dropped is not None:
        result["topics_dropped"] = topics_dropped
    if any(k in steps for k in _IMAGE_STEP_KEYS):
        result["images_before"] = images_before
        result["images_after"] = images_after
    ok_count = sum(1 for v in applied.values() if v is True)
    if ok_count == len(applied) and applied:
        result["status"] = "done"
    elif ok_count:
        result["status"] = "partially_applied"
    else:
        result["status"] = "failed"
        result["error"] = (
            f"note_edit_aborted_before_submit: 破坏性编辑步失败,已放弃提交 —— "
            f"一次发布都没点,笔记原样未动,可安全重试;首个失败原因:{abort_reason}"
            if aborted_before_submit else
            "note_components_all_failed: 请求的组件一项都没确认生效;"
            f"逐项原因见 failed({[f['component'] for f in failed]})"
        )
    return result


def _restore_permission(
    page,
    account_id: int,
    note_id: str,
    title: str,
    permission_before: str,
    permission_after: Optional[str],
) -> Dict[str, Any]:
    """权限被这次提交改掉了:大声告警 + 尝试改回原档位。

    改回走**权限弹窗**(``note_visibility.set_note_visibility``)而不是再提交一次笔记
    —— 后者又是一次全量覆盖,正是把权限改坏的那条路。只有实测支持的两档能改回
    (公开可见 / 仅自己可见);其余档位只告警,交人工。
    """
    logger.error(
        f"[note_components] ⚠️ 账号{account_id} note_id={note_id} 权限被改动:"
        f"{permission_before!r} → {permission_after!r},尝试改回"
    )
    from app.browser.note_visibility import (  # 局部导入:仅这条异常路径需要
        NoteVisibilityError,
        _PRIVACY_LABELS,
        set_note_visibility,
    )

    target = next((code for code, label in _PRIVACY_LABELS.items()
                   if label in permission_before), None)
    if target is None or not title:
        reason = (f"原档位「{permission_before}」不在可改回的两档内"
                  if target is None else "读不到标题,无法在笔记管理页定位卡片")
        logger.error(f"[note_components] 权限改不回来({reason}),**必须人工处理**")
        return {"ok": False, "reason": reason}
    try:
        outcome = set_note_visibility(page, account_id, note_id, title, target)
        logger.info(f"[note_components] 权限已改回:{outcome}")
        return {"ok": True, "permission_code": outcome.get("permission_code")}
    except NoteVisibilityError as exc:
        logger.error(f"[note_components] 权限改回失败({exc.reason}),**必须人工处理**")
        return {"ok": False, "reason": exc.reason}
    except Exception as exc:  # noqa: BLE001 — 改回失败不能盖掉主结果
        logger.exception("[note_components] 权限改回异常")
        return {"ok": False, "reason": f"restore_exception: {exc}"}


# ---------------- 目录读取(合集 / 活动列表) ----------------


def read_catalog(page, account_id: int, note_id: str) -> Dict[str, Any]:
    """只读打开一篇笔记的更新页,收集合集列表 + 活动列表。

    **全程只读**:进页面(活动列表随页面加载自己发出)→ 点一下合集入口让页面自己发
    ``list_v2`` → 收响应。不选任何条目、不点发布、不碰 ``.close-icon``,离开即恢复原状
    (未提交的编辑器状态不落库)。

    Returns:
        ``{"collections": [{id,name,desc,note_num}], "activities": [{id,name,desc}]}``。
    """
    human = SyncHumanActions(page)
    responses = ComponentResponses()
    responses.attach(page)
    try:
        open_update_page(page, account_id, note_id)
        # 活动列表随页面加载一次性返回全部(设计 2.9,实测 181 条);再等一小会兜住慢响应
        activity_body = responses.latest(_ACTIVITY_API_MARK)
        if activity_body is None:
            activity_body = _wait_body(
                page, responses, _ACTIVITY_API_MARK, _CATALOG_TIMEOUT_S, 0
            )

        collections_body = responses.latest(_COLLECTION_API_MARK)
        if collections_body is None:
            # 合集列表要点开弹层才发(只是打开弹层,不选任何条目)
            btn = page.query_selector(_COLLECTION_BUTTON)
            if btn is not None:
                seen = responses.count(_COLLECTION_API_MARK)
                human.click(btn, reason="打开合集弹层(只读列表)")
                collections_body = _wait_body(
                    page, responses, _COLLECTION_API_MARK, _CATALOG_TIMEOUT_S, seen
                )
            else:
                logger.warning("[note_components] 页面没有合集入口,合集列表读不到")

        collections = parse_collections(collections_body)
        activities = parse_activities(activity_body)
        logger.info(
            f"[note_components] 账号{account_id} 目录:合集 {len(collections)} 个 / "
            f"活动 {len(activities)} 条"
        )
        return {"collections": collections, "activities": activities}
    finally:
        responses.detach()


# ---------------- 视频笔记封面(结构已由真号探针证实为**内联**,非弹窗) ----------------
#
# 真号探针 + 截图实证(account9,2026-08-07,data/scene_captures/video_cover/):
# 封面区是**内联**的,点「设置封面」**不弹任何弹窗** —— 探针报 warning_no_modal 的原因是
# 它点的是**区标题**「设置封面」(那是文案不是按钮)。截图里的真实结构是:
#   .publish-page-content-cover
#     ├─ 「设置封面」标题 + 「默认截取第一帧作为封面…」说明 + 「优质封面示例」链接
#     ├─ 右上「PK封面」开关(**绝不碰**)
#     ├─ 左侧一块灰色方块(≈112×150)  ← 本地上传入口
#     └─ 「智能推荐封面」+ 3 张平台推荐图(**绝不碰**,点了就是选平台的图不是你的)
#
# 因此正确形状是「往封面区里的隐藏 file input 直接 set_input_files」,与视频/图片上传同源
# (绝不点上传按钮 —— 真桌面上会弹原生 GTK 文件框卡死整条流程)。
#
# ⚠️ 三轮探针共 4 种策略都**没能触发出任何弹窗**,这个"没有弹窗"的结论别再花成本重验。
# 后续接手的人请先读完这段:
#   - 触发点疑似 `.publish-page-content-cover .cover .upload-cover` 那个 tile
#     (文案「设置封面 遇到问题?」),但它是**瞬时态**:平台自动生成 3 张候选帧之后
#     它疑似被替换/改名,存在**竞态窗口** —— 也就是说"进编辑器就去点"和"等候选帧出来再点"
#     看到的 DOM 可能不是一个东西;
#   - 第一轮的启发式定位还误点过页面**底部无关的 activity-cover 横幅**(同名 cover 咬人,
#     与活动区/话题区那个「更多」同型),所以任何封面定位都必须收口在
#     `.publish-page-content-cover` 容器内,绝不全页找 `[class*='cover']`。
#
# 2026-08-07 真号 e2e(账号11)实测把上面的"疑似"钉成了事实:
#   observed = {cover_section_present: true, imgs_in_cover: 3, file_inputs_in_cover: 0}
# 即**候选帧已生成**的那个时刻,封面区里既没有 file input,`.cover-upload` /
# `[class*='upload']` 也都不在了 → 上传位确实改了名或换了形。据此这一轮做三件事:
#   1. 上传位候选扩成三层:class → 文案(「本地上传」「上传封面」「遇到问题」)→
#      尺寸 tile 启发式(区内、≈112×150、不含推荐图),每一层都过红线过滤;
#   2. **悬停优先于点击**:懒挂载常挂在 hover 上,而点上传位有弹**原生 GTK 文件框**
#      卡死整条流程的历史前科(Playwright 拦不住,见 atomic_tasks §2.4),所以顺序是
#      悬停 → 页面级唯一图片 input → 最后才点(且**只点一次**);
#   3. 失败时把封面区 **outerHTML** 一起交出去 —— 只报"选择器全未命中"等于什么都没说,
#      上一轮就卡在这里,拿不到真实 class 名。
_COVER_SECTION = ".publish-page-content-cover"
# 封面区内的隐藏 file input(候选;首选按 accept 收口避开视频那个)
_COVER_INPUT_CANDIDATES = (
    ".publish-page-content-cover input[type='file'][accept*='image']",
    ".publish-page-content-cover input[type='file']",
)
# 灰色上传位(候选;截图确认它在推荐图**左侧**、是封面区里第一个方块)
_COVER_ENTRY_CANDIDATES = (
    ".publish-page-content-cover .upload-cover",
    ".publish-page-content-cover .cover-upload",
    ".publish-page-content-cover [class*='upload']",
)
# 上传位的文案特征(截图实拍 tile 文案是「设置封面 遇到问题?」;「设置封面」单独出现是
# **区标题**不是按钮,所以只认「遇到问题」这类区分度够的词)
_COVER_ENTRY_TEXTS = ("本地上传", "上传封面", "遇到问题")
# 尺寸 tile 启发式的边界(截图实测灰色方块 ≈112×150)
_COVER_TILE_W = (60.0, 240.0)
_COVER_TILE_H = (80.0, 300.0)
# 平台推荐封面/PK 封面:**绝不碰**。点推荐图 = 选了平台的图而不是运营给的封面,
# 是"看起来成功了其实换错图"的静默错误,比失败更难发现。
_COVER_FORBIDDEN = ("智能推荐封面", "PK封面", "优质封面示例")
# 灌图后等封面预览渲染的窗口(秒)
_COVER_APPLY_TIMEOUT_S = 30.0
# 悬停后等 input 挂载的窗口(秒);悬停零风险但也别干等太久
_COVER_HOVER_TIMEOUT_S = 4.0
# 失败取证里封面区 outerHTML 的截断长度
_COVER_HTML_DUMP_CHARS = 2000


def _cover_probe(page, *, with_html: bool = False) -> Dict[str, Any]:
    """回读封面区的当场证据(定位失败时随 error 一起交出去,别只丢一句"没找到")。

    ``with_html`` 只在**失败**路径开:那段 HTML 有 2000 字,成功回执里带上它等于往 job
    台账里灌垃圾,而回读轮询每 0.5s 调一次本函数,顺手 dump 也是白烧协议往返。
    """
    evidence: Dict[str, Any] = {}
    section = None
    try:
        section = page.query_selector(_COVER_SECTION)
        evidence["cover_section_present"] = section is not None
        evidence["cover_section_text"] = _norm(section.inner_text())[:200] if section else ""
    except Exception:  # noqa: BLE001 — 取证本身绝不制造新异常
        evidence["cover_section_present"] = False
        evidence["cover_section_text"] = ""
    if with_html:
        # 封面区 outerHTML:上传位的真实 class 只能从这里读出来,是下一次真跑一击定位的凭据
        try:
            html = section.evaluate("el => el.outerHTML") if section is not None else ""
        except Exception:  # noqa: BLE001
            html = ""
        evidence["cover_section_html"] = (html or "")[:_COVER_HTML_DUMP_CHARS]
    for key, selector in (("file_inputs_in_cover", _COVER_INPUT_CANDIDATES[1]),
                          ("imgs_in_cover", ".publish-page-content-cover img"),
                          # 区内 0 而页面级 >0 = 上传控件挂在 body 级 portal 里
                          ("file_inputs_in_page", "input[type='file']"),
                          ("image_inputs_in_page", "input[type='file'][accept*='image']")):
        try:
            evidence[key] = len(page.query_selector_all(selector))
        except Exception:  # noqa: BLE001
            evidence[key] = 0
    return evidence


def _cover_entry_is_safe(element) -> bool:
    """上传位候选的红线过滤:文案沾到推荐封面/PK封面/区标题的一律否掉。

    这条同时兼作**范围闸** —— 命中整个封面区的宽泛选择器(拿到的元素文案会把红线词
    一起带上)会在这里被否掉,不至于点到区标题上。
    """
    try:
        text = _norm(element.inner_text())
    except Exception:  # noqa: BLE001 — 读不到文案就不敢用
        return False
    return all(kw not in text for kw in _COVER_FORBIDDEN)


def _find_cover_entry(page):
    """在封面区**内**找本地上传位 → ``(element, 说明)``;找不到返回 ``(None, "")``。

    三层递进,每层都过 :func:`_cover_entry_is_safe`:class 选择器 → 文案 → 尺寸 tile。
    绝不全页找 ``[class*='cover']``(会咬到页面底部的 activity-cover 横幅)。
    """
    element, selector = _first_match(page, _COVER_ENTRY_CANDIDATES)
    if element is not None and _cover_entry_is_safe(element):
        return element, f"class 候选 {selector}"

    try:
        section = page.query_selector(_COVER_SECTION)
        tiles = section.query_selector_all("div, button, a, label") if section else []
    except Exception:  # noqa: BLE001
        tiles = []

    for tile in tiles:
        try:
            text = _norm(tile.inner_text())
        except Exception:  # noqa: BLE001
            continue
        # 红线过滤在这里同时兼作范围闸:包住整个封面区的外层容器会把红线词一起带上,
        # 于是自动出局,不至于把上传位定位到区标题/推荐图的公共祖先上。
        if (any(kw in text for kw in _COVER_ENTRY_TEXTS)
                and all(kw not in text for kw in _COVER_FORBIDDEN)):
            return tile, f"文案候选「{text[:20]}」"

    # 最后一层:区内尺寸对得上的方块,且**不含 img**(含 img 的是那 3 张推荐帧)
    for tile in tiles:
        if not _cover_entry_is_safe(tile):
            continue
        try:
            if tile.query_selector("img") is not None:
                continue
            box = tile.bounding_box() or {}
        except Exception:  # noqa: BLE001
            continue
        w, h = box.get("width", 0), box.get("height", 0)
        if _COVER_TILE_W[0] <= w <= _COVER_TILE_W[1] and _COVER_TILE_H[0] <= h <= _COVER_TILE_H[1]:
            return tile, f"尺寸 tile 候选({w:.0f}×{h:.0f})"
    return None, ""


def _poll_cover_input(page, timeout_s: float):
    """在窗口内轮询封面区的 file input(懒挂载:触发之后才出现)。"""
    deadline = time.monotonic() + timeout_s
    while True:
        upload, used = _first_match(page, _COVER_INPUT_CANDIDATES)
        if upload is not None:
            return upload, used
        if time.monotonic() >= deadline:
            return None, None
        page.wait_for_timeout(400)


def _lone_page_image_input(page):
    """页面级**唯一**一个图片 file input → 拿它;不唯一就宁可不猜。

    上传控件挂 body 级 portal(不在封面区 DOM 里)是常见形状,能这样拿到就不用点 ——
    点上传位有原生文件框卡死的风险。"唯一"是安全边界;即便拿错了,后面"封面区图片数
    必须变多"的回读闸也会把它判成 error,不会静默换错图。
    """
    selector = "input[type='file'][accept*='image']"
    try:
        found = page.query_selector_all(selector)
    except Exception:  # noqa: BLE001
        return None, None
    if len(found) != 1:
        return None, None
    return found[0], f"页面级唯一 {selector}"


def _first_match(page, selectors):
    """按序取第一个命中的元素;全不命中返回 ``(None, None)``。"""
    for selector in selectors:
        try:
            found = page.query_selector(selector)
        except Exception:  # noqa: BLE001
            found = None
        if found is not None:
            return found, selector
    return None, None


def apply_video_cover(page, human: SyncHumanActions, cover_path: str) -> Dict[str, Any]:
    """给视频笔记设自定义封面 → ``{"status": "done"|"error", ...}``。

    形状已由真号截图证实是**内联**(不是弹窗,见本段顶部)。做法与视频/图片上传同源:
    找封面区里的隐藏 ``input[type=file]`` 直接 ``set_input_files``,**绝不点上传按钮**
    (真桌面上会弹原生 GTK 文件框卡死流程)。input 懒挂载时按"悬停 → 页面级唯一图片
    input → 最后才点一次上传位"的顺序把它逼出来,能不点就不点。

    **绝不碰**「智能推荐封面」的 3 张图与「PK封面」开关:点推荐图 = 换成平台的图而不是
    运营给的封面,那是"看着成功其实换错图"的静默错误,比失败更难发现。

    调用方(sync_client)对 error 的处理是**告警不阻断**:笔记照发,退回平台自动首帧。
    """
    if page.query_selector(_COVER_SECTION) is None:
        return {"status": "error",
                "reason": f"cover_section_not_found: 页面上没有封面区 {_COVER_SECTION}",
                "observed": _cover_probe(page, with_html=True)}

    upload, used = _first_match(page, _COVER_INPUT_CANDIDATES)
    entry, entry_desc = (None, "")
    if upload is None:
        entry, entry_desc = _find_cover_entry(page)
        if entry is not None:
            # 先悬停:懒挂载常挂在 hover 上,而悬停不会触发任何文件框
            try:
                human.hover(entry, reason=f"悬停封面区的本地上传位({entry_desc})")
            except Exception as exc:  # noqa: BLE001 — 悬停失败不致命,继续往下试
                logger.info(f"[note_components] 悬停封面上传位失败({exc}),继续")
            upload, used = _poll_cover_input(page, _COVER_HOVER_TIMEOUT_S)
    if upload is None:
        # 上传控件可能挂在 body 级 portal 里(不在封面区 DOM 内);拿得到就不用点
        upload, used = _lone_page_image_input(page)
    if upload is None:
        if entry is None:
            return {"status": "error",
                    "reason": "cover_upload_entry_not_found: 封面区里既没有 file input,"
                              "也没找到本地上传位(class/文案/尺寸三层候选全未命中,"
                              "真实结构见 observed.cover_section_html)",
                    "observed": _cover_probe(page, with_html=True)}
        # 走到这里才点,且**只点一次**:点上传位有弹原生 GTK 文件框卡死整条流程的前科
        # (Playwright 拦不住,见 atomic_tasks §2.4)。日志停在这句 = 就是那个卡死。
        logger.warning(
            f"[note_components] 悬停没挂出 input,即将点击封面上传位({entry_desc});"
            "若日志到此为止,即原生文件框卡死"
        )
        human.click(entry, reason=f"点封面区的本地上传位({entry_desc})")
        human.wait(0.6, 1.2, context="等封面上传入口挂载")
        upload, used = _poll_cover_input(page, _COVER_APPLY_TIMEOUT_S)
    if upload is None:
        return {"status": "error",
                "reason": f"cover_file_input_not_found: 点了上传位({entry_desc})仍没等到 file input",
                "observed": _cover_probe(page, with_html=True)}

    before = _cover_probe(page)
    try:
        upload.set_input_files([cover_path])
    except Exception as exc:  # noqa: BLE001 — 灌文件失败如实报,不静默
        return {"status": "error",
                "reason": f"cover_set_input_failed: {exc}",
                "observed": _cover_probe(page, with_html=True)}
    human.wait(1.0, 2.0, context="等封面预览渲染")

    # 回读:封面区里的图片数变多了才算真换上(与"点了就当成功"划清界限)。
    deadline = time.monotonic() + _COVER_APPLY_TIMEOUT_S
    after = before
    while time.monotonic() < deadline:
        after = _cover_probe(page)
        if after.get("imgs_in_cover", 0) > before.get("imgs_in_cover", 0):
            return {"status": "done", "cover_path": cover_path,
                    "observed": {"input_selector": used, **after}}
        page.wait_for_timeout(500)
    return {"status": "error",
            "reason": "cover_preview_unchanged: 灌了封面图但封面区预览没变化"
                      f"(图片数 {before.get('imgs_in_cover')} → {after.get('imgs_in_cover')})",
            "observed": {"input_selector": used, **_cover_probe(page, with_html=True)}}


# ---------------- 已发布笔记改封面(更新页「设置封面」**弹窗**链) ----------------
#
# ⚠️ 与上面那段**发布页**的内联封面链(``apply_video_cover``)是两条不同的操作面,别合并:
# 发布页封面区是内联的(点不出弹窗,已三轮探针钉死),而**更新页**点「修改封面」会弹出
# 一个 ``.d-modal`` —— 下面这条链走的是更新页那个弹窗。
#
# 真号取证(账号2 视频笔记 6a1e76f9…,2026-08-08,data/scene_captures/edit_cover/):
#   .publish-page-content-cover
#     └─ .cover
#         ├─ div.default.column[style=background-image:url(…封面图…)]   ← 缩略图
#         └─ div.operator.default.column.center.noCover.pointer         ← 悬停才显的浮层
#              ├─ .text        「修改封面」  rect {x445,y427,w56,h22}
#              └─ .down-grade  「遇到问题?」 rect {x450,y468,w46,h20}   ← **绝不点**
# 三条实证结论,每条都对应一次踩坑:
#   1. ``.operator`` 的 class 带 ``noCover`` = 当前用的还是平台自动截的首帧。它就是幂等
#      判据:没有 noCover 说明已经是自定义封面,再点一遍等于白覆盖一次;
#   2. 入口与「遇到问题?」贴得极近且有 tooltip 覆盖区,``element.click()`` 会撞上它 ——
#      所以按**实测矩形中心**点(``random_offset=False``,拟人层照样走贝塞尔+down/up);
#   3. 弹窗默认停在「截取封面」tab,**图片 file input 是懒挂载在「上传封面」tab 里的**:
#      不先切 tab 就永远 ``file_inputs=[]``(phase-1 整轮就卡在这)。
# 弹窗内实测:``input.upload-input[type=file]`` accept=image/png,image/jpeg,image/*;
# 「确定」``.btn-confirm`` 选图前 **class 带 disabled 且有 disabled 属性**(两处都要判);
# 「取消」``.cancelBtn``;「上传图片」``.btn-upload`` —— **绝不点它**,真桌面上会弹原生
# GTK 文件框卡死整条流程(见 atomic_tasks §2.4),灌文件一律走 set_input_files。
_COVER_OPERATOR_TEXT = f"{_COVER_SECTION} .operator .text"
# 缩略图(悬停它才唤出 .operator)。``.operator`` 自己也带 ``default column`` 两个 class,
# 故首选带 :not(.operator) 收口,退化候选靠 DOM 顺序(缩略图在前)兜住。
_COVER_THUMB_CANDIDATES = (
    f"{_COVER_SECTION} .cover .default.column:not(.operator)",
    f"{_COVER_SECTION} .cover .default.column",
    f"{_COVER_SECTION} .cover",
)
# 入口文案:必须含它才敢点(挡住"抓成了旁边那个「遇到问题?」"这类错位)
_COVER_ENTRY_TEXT = "修改封面"
# 「设置封面」弹窗。``.d-modal`` 与 ``_ANY_MODAL`` 同选择器 —— 页面上可能同时有别的
# ``.d-modal``,所以还要按文案特征认领(见 _find_cover_modal),绝不逮着一个就用。
_COVER_MODAL = ".d-modal"
_COVER_MODAL_MARKS = ("设置封面", "上传封面")
# 下面四个都在**认领到的弹窗元素内**查询(modal.query_selector / _all),ElementHandle
# 的查询限定在子树里求值 —— 根节点自己就是 .d-modal,不满足 `.d-modal X` 这种后代组合子,
# 带前缀会实测命中 0。故一律用裸(相对)形态,与本仓元素内查询的既有约定一致。
_COVER_MODAL_TAB = ".d-tabs-header"
_COVER_UPLOAD_TAB_TEXT = "上传封面"
_COVER_MODAL_FILE_INPUT = "input.upload-input[type='file']"
_COVER_MODAL_CONFIRM = ".btn-confirm"
_COVER_MODAL_CANCEL = ".cancelBtn"
# 各段窗口(秒):都按"真页面最慢那一次"给,失败路径靠它们收敛而不是干等
_COVER_OPERATOR_TIMEOUT_S = 4.0    # 悬停后等浮层渲染
_COVER_MODAL_OPEN_TIMEOUT_S = 10.0  # 点入口后等弹窗
_COVER_INPUT_TIMEOUT_S = 8.0       # 切 tab 后等 file input 懒挂载
_COVER_CONFIRM_TIMEOUT_S = 30.0    # 等平台收下这张图、「确定」解禁(上传要时间)
_COVER_MODAL_CLOSE_TIMEOUT_S = 10.0  # 点确定后等弹窗自己走
_COVER_PREVIEW_TIMEOUT_S = 10.0    # 弹窗关掉后等封面区刷新
# 封面区当前态:``no_cover``(还是平台首帧吗)+ ``fingerprint``(缩略图背景图 URL)。
# 判"换成了没有"用这两条的**任一变化**:noCover 只反映服务端态,提交前平台会不会当场
# 摘掉它没取证过,所以再加一条纯前端就能看见的背景图指纹兜底。
_COVER_STATE_JS = r"""() => {
  const sec = document.querySelector('.publish-page-content-cover');
  if (!sec) return null;
  const op = sec.querySelector('.operator');
  // 浮层不在 = 判不出现状。**绝不退化成 cls='' 那条路** —— 那会算出 no_cover:false,
  // 被读成"已经是自定义封面"而静默跳过整步(平台一改 class 名就全线静默空转)。
  if (!op) return null;
  const thumb = sec.querySelector('.cover .default.column:not(.operator)')
             || sec.querySelector('.cover .default.column');
  const cls = op.getAttribute('class') || '';
  let fingerprint = '';
  if (thumb) {
    fingerprint = thumb.style.backgroundImage
      || window.getComputedStyle(thumb).backgroundImage || '';
    const img = thumb.querySelector('img[src^="http"]');
    if (!fingerprint && img) fingerprint = img.src;
  }
  return {
    no_cover: cls.split(/\s+/).indexOf('noCover') >= 0,
    fingerprint: String(fingerprint).slice(0, 300),
  };
}"""


def _read_cover_state(page) -> Optional[Dict[str, Any]]:
    """读封面区当前态 → ``{"no_cover": bool, "fingerprint": str}``;读不到返回 ``None``。

    ``None`` 是"判不出现状",**绝不乐观当成没封面** —— 改封面是覆盖性动作,判不出就不动手。
    """
    try:
        state = page.evaluate(_COVER_STATE_JS)
    except Exception:  # noqa: BLE001 — 读不到就是不可确认,不是失败
        return None
    return state if isinstance(state, dict) else None


def _cover_changed(before: Dict[str, Any], after: Optional[Dict[str, Any]]) -> bool:
    """封面区真变了吗:``noCover`` 消失 或 缩略图指纹变了(任一即算)。"""
    if not isinstance(after, dict):
        return False
    if before.get("no_cover") and not after.get("no_cover"):
        return True
    return bool(after.get("fingerprint")) and after.get("fingerprint") != before.get("fingerprint")


# 提交后回读专用:与 ``_COVER_STATE_JS`` 的关键差别是 **.operator 缺失也照读指纹**。
# ``_COVER_STATE_JS`` 里"没 .operator 就整条返回 null"是**提交前幂等判据**的红线(判不出
# 现状就不动手);但提交后回读要的是"封面到底换没换",这时哪怕悬停浮层不在(账号5 真号
# 首验就是这样),缩略图背景图指纹仍是可靠强信号,绝不能连它一起丢。noCover 判不出就记
# null(辅助信号缺失,不参与判定),整个封面区都不在才返回 null。
_COVER_VERIFY_STATE_JS = r"""() => {
  const sec = document.querySelector('.publish-page-content-cover');
  if (!sec) return null;
  const op = sec.querySelector('.operator');
  const thumb = sec.querySelector('.cover .default.column:not(.operator)')
             || sec.querySelector('.cover .default.column');
  let fingerprint = '';
  if (thumb) {
    fingerprint = thumb.style.backgroundImage
      || window.getComputedStyle(thumb).backgroundImage || '';
    const img = thumb.querySelector('img[src^="http"]');
    if (!fingerprint && img) fingerprint = img.src;
  }
  return {
    no_cover: op ? (op.getAttribute('class') || '').split(/\s+/).indexOf('noCover') >= 0 : null,
    fingerprint: String(fingerprint).slice(0, 300),
  };
}"""


def _read_cover_verify_state(page) -> Optional[Dict[str, Any]]:
    """提交后回读封面区 → ``{"no_cover": bool|None, "fingerprint": str}``;整区不在返回 ``None``。

    ``no_cover`` 为 ``None`` 表示悬停浮层读不到(辅助信号缺失),但指纹仍照读 —— 这正是
    账号5 真号首验的处境,别再让浮层缺失把强信号一起吞掉。
    """
    try:
        state = page.evaluate(_COVER_VERIFY_STATE_JS)
    except Exception:  # noqa: BLE001 — 读不到就是不可确认,不是失败
        return None
    return state if isinstance(state, dict) else None


def _cover_verified(
    fp_before: Optional[str],
    fp_after: Optional[str],
    nc_before: Optional[bool],
    nc_after: Optional[bool],
) -> bool:
    """提交后封面回读判据(分层,任一强信号成立即 ``True``)。

    - **强信号 —— 指纹变化**:有提交前基线(``fp_before is not None``,排掉封面步没执行/出错
      时的空基线),且提交后封面区指纹非空并与基线不同 → 判换成。账号5 真号已证:这是
      悬停浮层读不到时唯一可靠的凭据;
    - **辅助信号 —— noCover 消失**:``nc_before`` 为 ``True`` 且 ``nc_after`` 为 ``False``
      (平台侧确认换成自定义封面)。``nc_after`` 读不到(``None``)时**不参与、不否决**;
    - 两者都不成立(指纹没变 / 没基线 + noCover 没确认消失)→ ``False``,保留 fail-loud。
    """
    if fp_before is not None and fp_after and fp_after != fp_before:
        return True
    if nc_before is True and nc_after is False:
        return True
    return False


def _find_cover_modal(page):
    """按文案认领「设置封面」弹窗;没有返回 ``None``。

    不逮着第一个 ``.d-modal`` 就用:这个页面上同时可能开着别的弹窗(合集移除确认、
    原创声明协议),点错弹窗里的「确定」是不可逆的。
    """
    try:
        modals = page.query_selector_all(_COVER_MODAL)
    except Exception:  # noqa: BLE001
        return None
    for modal in modals:
        try:
            text = _norm(modal.inner_text())
        except Exception:  # noqa: BLE001 — 读不到文案的一律不认领
            continue
        if any(mark in text for mark in _COVER_MODAL_MARKS):
            return modal
    return None


def _poll_cover(page, probe, timeout_s: float):
    """在窗口内轮询 ``probe()``,拿到非 ``None`` 就返回;超时返回 ``None``。"""
    deadline = time.monotonic() + timeout_s
    while True:
        try:
            value = probe()
        except Exception:  # noqa: BLE001 — 单次探测失败只当这一跳没命中
            value = None
        if value is not None:
            return value
        if time.monotonic() >= deadline:
            return None
        page.wait_for_timeout(300)


def _close_cover_modal(page, human: SyncHumanActions) -> None:
    """把「设置封面」弹窗关掉(点「取消」)。

    **每条失败路径都必须调它**:2026-08-02 引用弹窗没关、正好盖住发布按钮,兜底又点了
    禁用态的按钮 —— 那次事故的同型风险在这里一模一样。
    """
    modal = _find_cover_modal(page)
    if modal is None:
        return
    try:
        cancel = modal.query_selector(_COVER_MODAL_CANCEL)
    except Exception:  # noqa: BLE001
        cancel = None
    if cancel is None:
        logger.error(
            f"[note_components] 设置封面弹窗关不掉:没找到取消按钮 {_COVER_MODAL_CANCEL},"
            "弹窗可能盖住发布按钮,**这一单不要再点发布**"
        )
        return
    try:
        human.click(cancel, reason="关掉「设置封面」弹窗(取消)")
    except Exception as exc:  # noqa: BLE001 — 收尾失败只告警,别把原始失败原因盖掉
        logger.error(f"[note_components] 点取消关设置封面弹窗失败: {exc}")


def _cover_error(page, human, reason: str, *, close_modal: bool = True,
                 **extra) -> Dict[str, Any]:
    """封面步的失败回执:先关弹窗(不能留着盖发布按钮),再带当场取证交出去。"""
    if close_modal:
        _close_cover_modal(page, human)
    observed = {**_cover_probe(page, with_html=True), **extra}
    logger.error(f"[note_components] 改封面失败: {reason}")
    return {"status": "error", "reason": reason, "observed": observed}


def apply_cover_change(page, human: SyncHumanActions, cover_path: str) -> Dict[str, Any]:
    """在**更新页**给一篇已发布笔记换自定义封面 → ``{"status": "done"|"skipped"|"error"}``。

    链路(每一步都由真号取证钉过,见本段顶部):悬停缩略图唤出浮层 → 按矩形中心点
    「修改封面」→ 弹窗里**先切「上传封面」tab** → 往 ``input.upload-input`` 灌文件 →
    等「确定」解禁 → 点确定 → 等弹窗关 → **回读封面区真变了才算成功**。

    三条红线:
    - **幂等**:``.operator`` 没有 ``noCover`` = 已经是自定义封面 → ``skipped`` 且**零点击**;
    - **绝不碰**「智能推荐封面」「PK封面」「优质封面示例」「遇到问题?」,也**绝不点**
      「上传图片」按钮(原生文件框会卡死整条流程);
    - 判据是**封面区变了**,不是"点了就成功";任何一步失败都先把弹窗关掉再报。

    与破坏性编辑步(标题/正文/图片)的语义不同:本步失败**不阻断**其余组件,由调用方
    按组件语义汇总(见 ``set_note_components`` ⑤ter)。
    """
    before = _read_cover_state(page)
    if before is None:
        try:
            section_present = page.query_selector(_COVER_SECTION) is not None
        except Exception:  # noqa: BLE001
            section_present = False
        if not section_present:
            return _cover_error(
                page, human,
                f"cover_section_not_found: 更新页上没有封面区 {_COVER_SECTION}"
                "(这篇多半不是视频笔记,或平台改版了),一次点击都没发",
                close_modal=False,
            )
        return _cover_error(
            page, human,
            "cover_state_unreadable: 封面区在,但读不出当前是不是平台首帧"
            "(.operator 的 noCover 判据取不到);改封面是覆盖性动作,判不出现状就不动手,"
            "一次点击都没发",
            close_modal=False,
        )
    if not before.get("no_cover"):
        logger.info("[note_components] 这篇已经是自定义封面(.operator 无 noCover),跳过改封面")
        return {"status": "skipped",
                "reason": "cover_already_custom: 已经是自定义封面(不是平台自动首帧),"
                          "零点击跳过 —— 再换一次等于白覆盖",
                "fingerprint_before": before.get("fingerprint")}

    # ① 悬停缩略图:``.operator`` 浮层是 hover 才显的,不悬停连入口都不存在
    thumb, thumb_sel = _first_match(page, _COVER_THUMB_CANDIDATES)
    if thumb is not None:
        try:
            human.hover(thumb, reason=f"悬停封面缩略图唤出操作浮层({thumb_sel})")
        except Exception as exc:  # noqa: BLE001 — 悬停失败不致命,浮层也可能本就在
            logger.info(f"[note_components] 悬停封面缩略图失败({exc}),继续找入口")

    # ② 入口按**实测矩形中心**点:它与「遇到问题?」贴得极近且有 tooltip 覆盖区,
    #    element.click() 的可点性判定会撞上去(真号两轮取证都靠坐标点才命中)
    entry = _poll_cover(
        page, lambda: page.query_selector(_COVER_OPERATOR_TEXT), _COVER_OPERATOR_TIMEOUT_S
    )
    if entry is None:
        return _cover_error(
            page, human,
            f"cover_entry_not_found: 悬停缩略图后仍没找到「{_COVER_ENTRY_TEXT}」"
            f"({_COVER_OPERATOR_TEXT});真实结构见 observed.cover_section_html",
            close_modal=False,
        )
    try:
        entry_text = _norm(entry.inner_text())
        box = entry.bounding_box() or {}
    except Exception as exc:  # noqa: BLE001
        return _cover_error(page, human, f"cover_entry_unreadable: {exc}", close_modal=False)
    if _COVER_ENTRY_TEXT not in entry_text or any(k in entry_text for k in _COVER_FORBIDDEN):
        return _cover_error(
            page, human,
            f"cover_entry_text_mismatch: {_COVER_OPERATOR_TEXT} 的文案是 {entry_text[:30]!r},"
            f"不是「{_COVER_ENTRY_TEXT}」——不敢点(旁边就是「遇到问题?」与推荐封面)",
            close_modal=False,
        )
    if not (box.get("width") and box.get("height")):
        return _cover_error(
            page, human,
            f"cover_entry_rect_unmeasurable: 「{_COVER_ENTRY_TEXT}」量不到矩形({box}),"
            "按坐标点是躲开 tooltip 覆盖区的唯一办法,量不到就不点",
            close_modal=False,
        )
    center = (box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
    human.click(
        entry, random_offset=False,
        reason=f"点封面浮层「{_COVER_ENTRY_TEXT}」(矩形中心 {center[0]:.0f},{center[1]:.0f})",
    )
    human.wait(0.6, 1.2, context="等设置封面弹窗")

    # ③ 弹窗
    modal = _poll_cover(page, lambda: _find_cover_modal(page), _COVER_MODAL_OPEN_TIMEOUT_S)
    if modal is None:
        return _cover_error(
            page, human,
            f"cover_modal_not_opened: 点了「{_COVER_ENTRY_TEXT}」但 {_COVER_MODAL_OPEN_TIMEOUT_S}s "
            "内没等到「设置封面」弹窗;**不猜别的路径**,一个文件都没灌",
            close_modal=False,
        )

    # ④ 切「上传封面」tab —— 图片 file input 懒挂载在这个 tab 里,不切就永远找不到
    tab = _find_upload_tab(modal)
    if tab is None:
        return _cover_error(
            page, human,
            f"cover_upload_tab_not_found: 弹窗里没有「{_COVER_UPLOAD_TAB_TEXT}」tab"
            f"({_COVER_MODAL_TAB});图片 input 就挂在它下面,平台多半改版了",
        )
    human.click(tab, reason=f"切到弹窗的「{_COVER_UPLOAD_TAB_TEXT}」tab")
    human.wait(0.5, 1.0, context="等上传 tab 挂载")

    # ⑤ 灌文件:**绝不点**「上传图片」按钮(.btn-upload),真桌面上会弹原生 GTK 文件框
    upload = _poll_cover(page, lambda: _cover_file_input(page), _COVER_INPUT_TIMEOUT_S)
    if upload is None:
        return _cover_error(
            page, human,
            f"cover_file_input_not_found: 切到「{_COVER_UPLOAD_TAB_TEXT}」tab 后 "
            f"{_COVER_INPUT_TIMEOUT_S}s 内仍没挂出 {_COVER_MODAL_FILE_INPUT};"
            "**不退回去点「上传图片」按钮**(原生文件框会卡死整条流程)",
        )
    try:
        upload.set_input_files([cover_path])
    except Exception as exc:  # noqa: BLE001
        return _cover_error(page, human, f"cover_set_input_failed: {exc}")
    human.wait(1.0, 2.0, context="等平台收下这张封面图")

    # ⑥ 等「确定」解禁:class 带 disabled 或有 disabled 属性都算禁用(实测两处同时出现)
    confirm = _poll_cover(page, lambda: _enabled_cover_confirm(page), _COVER_CONFIRM_TIMEOUT_S)
    if confirm is None:
        return _cover_error(
            page, human,
            f"cover_confirm_not_enabled: 灌了图但「确定」{_COVER_MODAL_CONFIRM} 在 "
            f"{_COVER_CONFIRM_TIMEOUT_S}s 内一直是禁用态(平台没接受这张图);"
            "**绝不硬点禁用按钮**",
        )
    human.click(confirm, reason="点「设置封面」弹窗的确定")
    human.wait(0.8, 1.5, context="等弹窗关闭")

    # ⑦ 弹窗必须走掉:它会盖住发布按钮(2026-08-02 引用弹窗同型事故)
    closed = _poll_cover(
        page, lambda: True if _find_cover_modal(page) is None else None,
        _COVER_MODAL_CLOSE_TIMEOUT_S,
    )
    if closed is None:
        return _cover_error(
            page, human,
            f"cover_modal_not_closed: 点了确定但弹窗 {_COVER_MODAL_CLOSE_TIMEOUT_S}s 内没关,"
            "已强行点取消关掉(留着它会盖住发布按钮)",
        )

    # ⑧ 回读:封面区真变了才算成功(与"点了就当成功"划清界限)
    def _changed_state():
        state = _read_cover_state(page)
        return state if _cover_changed(before, state) else None

    after = _poll_cover(page, _changed_state, _COVER_PREVIEW_TIMEOUT_S)
    if after is None:
        return _cover_error(
            page, human,
            "cover_preview_unchanged: 弹窗关了但封面区没变"
            f"(noCover 还在且缩略图指纹没动,原值 {before.get('fingerprint', '')[:60]!r});"
            "**不当成功**",
            close_modal=False,
            fingerprint_before=before.get("fingerprint"),
        )
    logger.info("[note_components] 封面已在编辑器内换上(待提交生效)")
    return {"status": "done", "cover_path": cover_path,
            "no_cover_before": before.get("no_cover"),
            "fingerprint_before": before.get("fingerprint"),
            "fingerprint_after": after.get("fingerprint")}


def _find_upload_tab(modal):
    """弹窗里那个「上传封面」tab;没有返回 ``None``(文案**全等**,别用包含)。"""
    try:
        tabs = modal.query_selector_all(_COVER_MODAL_TAB)
    except Exception:  # noqa: BLE001
        return None
    for tab in tabs:
        try:
            if _norm(tab.inner_text()) == _COVER_UPLOAD_TAB_TEXT:
                return tab
        except Exception:  # noqa: BLE001
            continue
    return None


def _cover_file_input(page):
    """「上传封面」tab 里那个图片 file input;还没挂载返回 ``None``。"""
    modal = _find_cover_modal(page)
    if modal is None:
        return None
    try:
        return modal.query_selector(_COVER_MODAL_FILE_INPUT)
    except Exception:  # noqa: BLE001
        return None


def _enabled_cover_confirm(page):
    """弹窗里**已解禁**的「确定」;还禁着(class 带 disabled 或有 disabled 属性)返回 ``None``。"""
    modal = _find_cover_modal(page)
    if modal is None:
        return None
    try:
        confirm = modal.query_selector(_COVER_MODAL_CONFIRM)
        if confirm is None:
            return None
        if confirm.get_attribute("disabled") is not None:
            return None
        if "disabled" in (confirm.get_attribute("class") or ""):
            return None
    except Exception:  # noqa: BLE001
        return None
    return confirm


def _find_text_in_section(page, section_selector: str, text: str):
    """在某个容器**内**找文案精确匹配的可点元素;找不到返回 None。

    收口在容器内而不是全页 ``text=``:同名文案在这个页面上反复咬人
    (活动区/话题区各有一个「更多」;「确定」更是满页都是)。
    """
    try:
        section = page.query_selector(section_selector)
    except Exception:  # noqa: BLE001
        return None
    if section is None:
        return None
    target = _norm(text)
    try:
        for el in section.query_selector_all("button, div, span, a, li"):
            try:
                if _norm(el.inner_text()) == target:
                    return el
            except Exception:  # noqa: BLE001 — 单个元素读失败只跳过它
                continue
    except Exception:  # noqa: BLE001
        return None
    return None


# ══════════════════ 建笔记合集(2026-08-09 号8 三段只读探针实拍)══════════════════
#
# **创建入口不在笔记管理页**:实拍确认笔记管理页上根本没有"合集"tab,唯一的创建入口在
# 笔记编辑器「加入合集」弹层的**底栏** ``.collection-plugin-popover .popover-footer-content``
# (文案「创建合集」)。所以建合集必须借一篇**载体笔记**打开编辑器 —— 调用方传的
# ``carrier_note_id`` 应当就是本来就该挂进这个合集的那一篇("创建并加入"会把它加进去)。
#
# 创建表单是个极简 modal(实拍):合集名称(≤20 字,带 0/20 计数)/ 合集简介(≤50 字,
# 带 0/50 计数)/ [取消][创建并加入]。**没有封面字段** —— 与播客合集(封面必填 + 裁剪
# 二次确认)完全不同,故 REST 层对 ``cover`` 参数显式 422 而不是静默忽略。
#
# **两件未取证的事,首验要靠本实现的回执分辨**:
#   ① 创建是**即时落地**(点完就有独立 API 落库)还是**随笔记提交才生效**;
#   ② 创建 API 的 URL 特征 / 响应形态 / 回不回 id。
# 做法:点「创建并加入」**前**挂一层创建 API 拦截(``_CreateApiCapture``),点完之后
# **重进更新页**再从干净列表回读 —— 重进会丢弃一切未提交的编辑器状态(仓内既有结论),
# 所以"重进后列表里仍有这个名字"就是①的「即时落地」证据,"没有"就是「要随提交才生效」。
# 两种都如实回报,绝不替平台圆场。
#
# 判据是**双信号**(播客合集 7 单假绿的直接教训):modal 收起 **且** 干净列表里出现该名。
# 单看页面文本一律不算数 —— 那正是预览卡伪证栽过的地方。

# 弹层底栏的「创建合集」(实拍选择器 + 实拍文案,两路都用:class 变了还有文案兜底)
_COLLECTION_CREATE_FOOTER = ".collection-plugin-popover .popover-footer-content"
_COLLECTION_POPOVER = ".collection-plugin-popover"
# 创建 modal:容器 class **未取证**,只能按"可见 + 含实拍标志文案"认领,再取最内层
# (祖先容器的 innerText 是子孙的超集,故按文本最短挑)。**绝不逮着第一个 .d-modal 就用**。
_CREATE_MODAL_SCOPES = (".d-modal", "[class*='modal']")
_CREATE_SUBMIT_TEXT = "创建并加入"
_CREATE_MODAL_MARKS = (_CREATE_SUBMIT_TEXT, "合集名称")
# 名称 / 简介输入框:placeholder 文案**未取证**(实拍只看到了字段标签),故按由细到粗
# 的候选找,全落空时**带 modal 的 HTML 一起 fail-loud**,绝不退回全页找 input ——
# 全页找会摸到笔记**标题框**,那一下就是对载体笔记的真实改动。
_CREATE_NAME_HINTS = ("合集名", "名称")
_CREATE_DESC_HINTS = ("简介", "描述")
# modal 里的字段一律只在 modal 容器内找(同名陷阱与"别在全页找"的红线都在这一条上)
_CREATE_FIELD_SCOPE = "input, textarea"
# 未知结构时留的取证:modal 的 HTML(硬上限)。**保质期字段** —— 首验把 placeholder /
# class 钉死之后即撤,别让临时 dump 变成永久债。
_CREATE_MODAL_HTML_CHARS = 1500

# 创建 API 拦截:URL 特征未取证,只能宽松匹配。三道过滤缺一不可(见 _CreateApiCapture)。
_CREATE_API_HINT = "collection"
_CREATE_API_MAX_ENTRIES = 5
_CREATE_API_BODY_CHARS = 800
# 创建响应里 id 的候选键(**未取证**,取到就当线索,取不到不影响判定)
_CREATE_ID_KEYS = ("id", "collection_id", "collectionId")

_CREATE_MODAL_TIMEOUT_S = 12.0
# 没有封面上传要等,名称一填按钮就该翻转;给 20s 是留够平台自己的防抖/校验
_CREATE_ENABLE_TIMEOUT_S = 20.0
_CREATE_CLOSE_TIMEOUT_S = 20.0


class _CreateApiCapture:
    """点「创建并加入」之后新增的 **POST** 响应取证(创建 API 的 URL/形态未取证)。

    与 ``ComponentResponses`` 分开而不是往它的 ``_MARKS`` 里加一条:那一族是**已取证**
    的接口特征,这里是"还不知道要抓什么"的探路取证,混在一起会让人以为创建 API 也已实证。

    三道过滤(**第二道是重点**,回答"宽松匹配会不会误抓 list_v2"):

    1. URL 含 ``collection``(宽松,创建 API 的路径未取证);
    2. URL **不含** ``note/collection/pc/list_v2`` —— 弹层列表接口自己就带 ``collection``
       字样,只靠第 1 道必然把它抓进来,那样取证里全是列表响应、真正的创建响应反而被
       ``_CREATE_API_MAX_ENTRIES`` 挤掉;
    3. 请求方法必须是 ``POST`` —— 写操作才可能是创建;列表是读接口(即便它哪天改成 POST,
       第 2 道也已经把它挡在门外,两道各自独立生效)。

    ``entries`` 有硬上限,body 截断;响应体必须在回调里当场读(导航之后取不到)。
    """

    def __init__(self) -> None:
        self.entries: List[Dict[str, Any]] = []
        self._page = None

    def handle(self, response) -> None:
        try:
            url = response.url or ""
        except Exception:  # noqa: BLE001 — 响应对象已失效,读 url 都会炸
            return
        if _CREATE_API_HINT not in url.lower():
            return
        if _COLLECTION_API_MARK in url:
            return  # 弹层列表接口,不是创建(见 docstring 第 2 道)
        try:
            method = (response.request.method or "").upper()
        except Exception:  # noqa: BLE001 — 读不到方法就不认领,取证宁缺毋滥
            return
        if method != "POST":
            return
        if len(self.entries) >= _CREATE_API_MAX_ENTRIES:
            return
        try:
            status = response.status
        except Exception:  # noqa: BLE001
            status = None
        try:
            body = response.text() or ""
        except Exception as exc:  # noqa: BLE001 — 读不到体也要留下这一条的存在
            body = f"<响应体读取失败: {exc}>"
        self.entries.append({
            "url": url[:300],
            "method": method,
            "status": status,
            "body": body[:_CREATE_API_BODY_CHARS],
        })

    def attach(self, page) -> None:
        """挂监听(幂等:同一实例只挂一次,换 page 时先摘旧的)。"""
        if self._page is page:
            return
        self.detach()
        page.on("response", self.handle)
        self._page = page

    def detach(self) -> None:
        """摘监听:同一个 page 会被后续任务复用,留着会继续吃响应体。"""
        if self._page is None:
            return
        try:
            self._page.remove_listener("response", self.handle)
        except Exception:  # noqa: BLE001
            logger.warning("[note_components] 摘除创建 API 监听失败(忽略)")
        self._page = None

    def count(self) -> int:
        return len(self.entries)

    def since(self, seen: int) -> List[Dict[str, Any]]:
        """基线之后新增的那些(基线必须在点击**之前**取)。"""
        return self.entries[seen:]


def parse_created_collection_id(entries: Optional[List[dict]]) -> Optional[str]:
    """尽力从创建 API 响应体里抠出合集 id;抠不到返回 None(纯函数,**未取证不抱期望**)。

    只在 ``data`` 下与顶层各看一眼 ``_CREATE_ID_KEYS``,**不做深度递归搜索** —— 递归会
    在响应里随便捞到一个别的 id(用户 id / trace id),给出一个看着像真的假 id 比给 None
    坏得多:调用方会拿它去挂笔记,然后收获一个静默失败。
    """
    for entry in reversed(entries or []):
        if not isinstance(entry, dict):
            continue
        try:
            body = json.loads(entry.get("body") or "")
        except (TypeError, ValueError):
            continue
        if not isinstance(body, dict):
            continue
        for scope in (body.get("data"), body):
            if not isinstance(scope, dict):
                continue
            for key in _CREATE_ID_KEYS:
                value = scope.get(key)
                if isinstance(value, (str, int)) and str(value).strip():
                    return str(value).strip()
    return None


def create_join_enabled(cls: Optional[str], disabled_attr: bool) -> bool:
    """「创建并加入」按钮可不可点(纯函数,**四路取或**,任一命中即不可点)。

    ① 有 ``disabled`` 属性;② class 里有整词 ``disabled``;③ 有以 ``-disabled`` 结尾的
    类名(如 ``create-btn-disabled`` / ``d-button-disabled``,同一套设计系统的常见形态);
    ④ loading 态(``d-button-primary-loading`` 或任何 ``-primary-loading`` 结尾的类名)。

    ②③④ 一律**按空白切分后整词比较**,不是 substring:``--color-static`` 这类类名里本就
    不含 ``disabled``,但 substring 判法会被将来任何带 disabled 字样的类名命中,把按钮
    **永久判死** —— 那是比假绿更难查的反向故障(0.20.3 播客合集 RCA 的原话)。

    本函数与 ``app.browser.podcast.create_button_state`` **同源不共用**:那颗按钮有实拍
    class(``button.create-btn``)、读法走 JS 选择器;这颗按钮的 class 未取证,只能从元素
    句柄上读属性。判据逻辑抄的是同一条 RCA 结论,谁先拿到真号 class 谁再收口成一份。
    """
    if disabled_attr:
        return False
    tokens = (cls or "").split()
    for token in tokens:
        if token == "disabled" or token.endswith("-disabled"):
            return False
        if token == "d-button-primary-loading" or token.endswith("-primary-loading"):
            return False
    return True


def read_create_join_state(element) -> Dict[str, Any]:
    """从按钮**元素句柄**读禁用态 → ``{"found", "enabled", "cls"}``。

    ``disabled`` 属性必须用 ``is not None`` 判:``<button disabled>`` 读回来是**空串**,
    ``bool("")`` 是 False —— 拿真值判断会把一颗明确禁用的按钮判成可点(与 0.20.3 那条
    "禁用形态漏了一种"是同一类错误,只是换了个读法)。

    读不到 / 元素为 None 一律 ``{"found": False}``,调用方当**不可点**处理。
    """
    if element is None:
        return {"found": False}
    try:
        cls = element.get_attribute("class") or ""
        disabled_attr = element.get_attribute("disabled") is not None
    except Exception:  # noqa: BLE001 — 句柄失效
        return {"found": False}
    return {"found": True, "enabled": create_join_enabled(cls, disabled_attr), "cls": cls}


def _find_create_modal(page):
    """认领创建合集的 modal:**可见 + 含实拍标志文案 + 文本最短**;没有返回 None。

    "文本最短"是取最内层的代理判据:祖先容器的 ``innerText`` 必然是子孙的超集,所以同样
    命中标志文案时,文本最短的那个就是最贴近 modal 本体的。不用面积/位置启发式 ——
    浮层用面积猜层级在这条产品线上栽过(点中了祖先容器)。
    """
    best = None
    best_len = None
    for scope in _CREATE_MODAL_SCOPES:
        try:
            nodes = page.query_selector_all(scope)
        except Exception:  # noqa: BLE001
            continue
        for node in nodes:
            try:
                if not node.is_visible():
                    continue
                text = _norm(node.inner_text())
            except Exception:  # noqa: BLE001 — 读不出的一律不认领
                continue
            if not any(mark in text for mark in _CREATE_MODAL_MARKS):
                continue
            if best_len is None or len(text) < best_len:
                best, best_len = node, len(text)
    return best


def _modal_fields(modal) -> List[Any]:
    """modal 内的全部输入控件(input / textarea),读不到返回 []。"""
    try:
        return list(modal.query_selector_all(_CREATE_FIELD_SCOPE))
    except Exception:  # noqa: BLE001
        return []


def _pick_field(fields: List[Any], hints, *, fallback_index: Optional[int]):
    """从 modal 的输入控件里挑一个:先按 placeholder 命中提示词,再退位置兜底。

    位置兜底只在"控件数量正好对得上表单形状"时才用(``fallback_index`` 由调用方按实拍的
    两字段形状给),挑不出就返回 None 让调用方 fail-loud —— **绝不扩大到 modal 之外找**:
    页面上还有笔记标题框,摸错一下就是对载体笔记的真实改动。
    """
    for field in fields:
        try:
            placeholder = field.get_attribute("placeholder") or ""
        except Exception:  # noqa: BLE001
            continue
        if any(hint in placeholder for hint in hints):
            return field
    if fallback_index is not None and 0 <= fallback_index < len(fields):
        return fields[fallback_index]
    return None


def _find_in_modal_by_text(modal, text: str):
    """在 modal 内按文案精确找可点元素,取**最内层**;找不到返回 None。

    最内层判据:候选自身的子树里没有同样精确命中该文案的元素。整卡/整容器点击在这条
    产品线上反复咬人(点容器中点会命中错误子元素),所以宁可找不到 fail-loud,也不点
    一个"文案对得上但不知道是不是它"的大容器。
    """
    target = _norm(text)
    scope = "button, div, span, a"
    try:
        candidates = [
            el for el in modal.query_selector_all(scope)
            if _norm(el.inner_text()) == target
        ]
    except Exception:  # noqa: BLE001
        return None
    for el in candidates:
        try:
            inner = [c for c in el.query_selector_all(scope)
                     if _norm(c.inner_text()) == target]
        except Exception:  # noqa: BLE001
            inner = []
        if not inner:
            return el
    return None


def _find_create_submit(page, modal):
    """定位「创建并加入」:先在 modal 内找最内层,再退回按钮文案全页精确匹配。"""
    found = _find_in_modal_by_text(modal, _CREATE_SUBMIT_TEXT) if modal is not None else None
    return found if found is not None else _find_button_by_text(page, _CREATE_SUBMIT_TEXT)


def _modal_html(page) -> str:
    """**当场**认领 modal 再取它的 HTML(硬上限截断);读不到返回空串。

    每次现找而不是吃调用方手上那个句柄:表单一重渲染,旧句柄指向的就是已经脱离文档的
    旧节点,拿它取证会交出一份**过期的现场**——比没有取证更误导人。
    **保质期字段**,首验把表单结构钉死之后即撤。
    """
    modal = _find_create_modal(page)
    if modal is None:
        return ""
    try:
        return (modal.inner_html() or "")[:_CREATE_MODAL_HTML_CHARS]
    except Exception:  # noqa: BLE001
        return ""


def collection_create_probe(page) -> Dict[str, Any]:
    """建合集流程的当场取证(任何一步失败都随 error 一起交出去)。

    modal **每次现找**,不收调用方手上的句柄:表单一重渲染旧句柄就指向脱离文档的旧节点,
    读它等于交出一份过期现场(尤其是按钮的禁用态,那正是最要紧的一格)。
    """
    evidence: Dict[str, Any] = {}
    for key, selector in (
        ("popover_present", _COLLECTION_POPOVER),
        ("create_footer_present", _COLLECTION_CREATE_FOOTER),
        ("collection_button_present", _COLLECTION_BUTTON),
    ):
        try:
            evidence[key] = page.query_selector(selector) is not None
        except Exception:  # noqa: BLE001 — 取证本身绝不制造新异常
            evidence[key] = False
    modal = _find_create_modal(page)
    evidence["create_modal_present"] = modal is not None
    if modal is not None:
        evidence["create_modal_text"] = _norm_safe_text(modal)[:300]
        evidence["create_modal_fields"] = len(_modal_fields(modal))
        evidence["create_submit"] = read_create_join_state(
            _find_in_modal_by_text(modal, _CREATE_SUBMIT_TEXT)
        )
    try:
        evidence["collection_label"] = read_collection_label(page)
    except Exception:  # noqa: BLE001
        evidence["collection_label"] = None
    # 页面文本兜一段:modal 容器认不出来时(class 与实拍不同),上面几格全是 False,
    # 光看它们说不清"表单到底弹没弹"。**只作取证,绝不参与成败判定** —— 页面文本里出现
    # 合集名不构成任何证据(播客合集预览卡伪证的教训)。
    try:
        evidence["page_text"] = _norm(page.inner_text("body"))[:600]
    except Exception:  # noqa: BLE001
        evidence["page_text"] = ""
    return evidence


def _norm_safe_text(element) -> str:
    """元素文案(归一);读不到返回空串(取证读数绝不制造异常)。"""
    try:
        return _norm(element.inner_text())
    except Exception:  # noqa: BLE001
        return ""


def _open_collection_popover(
    page, human: SyncHumanActions, responses: ComponentResponses
) -> Dict[str, Any]:
    """点「加入合集」开弹层并等 ``list_v2`` → ``{"catalog": [...]}`` 或 ``{"error": ...}``。

    与 ``_set_collection`` 开弹层那几行同源(含防遮挡滚动):底部悬浮发布钮的透明命中区
    会把设置行上的点击静默吞掉,不先滚进中带就是看运气。

    **空列表不是错误**:新号本来就一个合集都没有。判"读没读到"只认有没有收到响应体,
    不看列表长不长 —— 拿"列表为空"当失败会让第一次建合集永远建不成。
    """
    btn = page.query_selector(_COLLECTION_BUTTON)
    if btn is None:
        return {"error": "collection_entry_not_found: 载体笔记的编辑页上没有「加入合集」"
                         "入口(该笔记可能已在某个合集里 —— 已选态下入口本就不渲染;"
                         "换一篇不在任何合集里的笔记当载体)"}
    seen = responses.count(_COLLECTION_API_MARK)  # 基线必须在点击**之前**取
    _scroll_row_to_mid_viewport(page, human, _COLLECTION_BUTTON)
    btn = page.query_selector(_COLLECTION_BUTTON) or btn
    human.click(btn, reason="打开合集弹层")
    body = _wait_body(page, responses, _COLLECTION_API_MARK, _POPOVER_TIMEOUT_S, seen)
    if body is None:
        # 列表可能在**页面加载时**就随编辑器预取了(号8图文载体首验实测:点「加入合集」只
        # 渲染缓存、不再发新请求,等"新增响应"必然超时)。回落到已捕获的最近响应 ——
        # 与 GET /collections 流程同源的"先认预取"语义(RCA 2026-08-09)。
        body = responses.latest(_COLLECTION_API_MARK)
    if body is None:
        return {"error": "collection_catalog_unavailable: 点开弹层后没收到合集列表响应,"
                         "建前查重做不了 —— 不查重就建会造出第二个同名合集(平台不去重"
                         "同名已实证),故整单中止"}
    return {"catalog": parse_collections(body)}


def create_note_collection(
    page,
    account_id: int,
    carrier_note_id: str,
    name: str,
    description: Optional[str] = None,
) -> Dict[str, Any]:
    """借载体笔记的编辑器新建一个**笔记合集** → ``{"status": "done"|"error", ...}``。

    流程(每步失败都当场取证 fail-loud):进载体笔记更新页 → 开合集弹层拿列表 →
    **建前查重**(同名即停,不重建)→ 点弹层底栏「创建合集」→ 填名称/简介 →
    等「创建并加入」翻转(四路禁用判定)→ 挂创建 API 拦截并点击 →
    **双信号判定**(modal 收起 **且** 重进更新页后的干净列表里有这个名字)。

    **不提交载体笔记**:全程零发布点击,收尾靠导航离开 —— 未提交的编辑器状态不落库,
    所以载体笔记本身零改动(「并加入」若已由平台独立落库,那是平台侧行为,回执里的
    ``joined_carrier`` 如实报出来)。

    Raises:
        NoteComponentsError: 载体笔记的更新页进不去(前置硬失败,由服务层翻译成 error)。
    """
    name = _norm(name)
    description = _norm(description or "") or None
    if not name:
        return {"status": "error", "reason": "collection_name_empty: 合集名称是必填项"}

    human = SyncHumanActions(page)
    responses = ComponentResponses()
    capture = _CreateApiCapture()
    responses.attach(page)
    capture.attach(page)
    try:
        open_update_page(page, account_id, carrier_note_id)

        opened = _open_collection_popover(page, human, responses)
        if "error" in opened:
            return {"status": "error", "reason": opened["error"],
                    "observed": collection_create_probe(page)}
        catalog = opened["catalog"]

        # 建前查重:平台**不去重同名**(播客合集实证),重建只会造出第二个同名合集,
        # 而"多一个空合集"要人工去平台删。有同名就把现有那条的 id 交回去,调用方直接用。
        existing = next((c for c in catalog if _norm(c["name"]) == name), None)
        if existing is not None:
            return {
                "status": "error",
                "reason": f"collection_name_already_exists: 该号已有同名合集「{name}」"
                          f"(id={existing['id']},note_num={existing.get('note_num')});"
                          f"平台不去重同名,重建只会多出一个空合集 —— **没有创建任何东西**,"
                          f"请直接用回执里的 collection_id 挂笔记",
                "collection_id": existing["id"],
                "name": name,
                "note_num": existing.get("note_num"),
                "name_preexisted": True,
            }

        entry = page.query_selector(_COLLECTION_CREATE_FOOTER)
        if entry is None or _COLLECTION_CREATE_TEXT not in _norm_safe_text(entry):
            # class 变了还有文案兜底,但仍**收口在弹层容器内**(全页找「创建合集」会咬到别处)
            entry = _find_text_in_section(
                page, _COLLECTION_POPOVER, _COLLECTION_CREATE_TEXT
            )
        if entry is None:
            return {"status": "error",
                    "reason": f"collection_create_entry_not_found: 弹层底栏没有"
                              f"「{_COLLECTION_CREATE_TEXT}」入口"
                              f"({_COLLECTION_CREATE_FOOTER} 与文案兜底均未命中)",
                    "observed": collection_create_probe(page)}
        human.click(entry, reason=f"{_COLLECTION_CREATE_TEXT}(打开创建表单)")

        modal = _wait_create_modal(page)
        if modal is None:
            return {"status": "error",
                    "reason": "collection_create_modal_not_shown: 点了「创建合集」但创建"
                              "表单没出来",
                    "observed": collection_create_probe(page)}

        filled = _fill_create_form(page, human, modal, name, description)
        if "error" in filled:
            return {"status": "error", "reason": filled["error"],
                    "observed": collection_create_probe(page),
                    "modal_html": _modal_html(page)}

        submit, state = _wait_create_submit_enabled(page, modal)
        if submit is None:
            return {"status": "error",
                    "reason": "create_join_never_enabled: 名称已填但「创建并加入」按钮"
                              "始终禁用/loading,**绝不点禁用按钮**(平台可能又加了必填项,"
                              "或名称被判不合法)",
                    "observed": collection_create_probe(page),
                    "create_submit_state": state,
                    "modal_html": _modal_html(page)}

        seen_api = capture.count()  # 基线必须在点击**之前**取
        human.click(submit, reason=f"{_CREATE_SUBMIT_TEXT}「{name}」")

        return _verify_created(
            page, human, responses, capture, account_id, carrier_note_id, name,
            description, seen_api,
        )
    finally:
        responses.detach()
        capture.detach()


def _wait_create_modal(page):
    """轮询等创建 modal 渲染出来(要求它已经能被认领**且**里面有输入控件);超时 None。"""
    deadline = time.monotonic() + _CREATE_MODAL_TIMEOUT_S
    while time.monotonic() < deadline:
        modal = _find_create_modal(page)
        if modal is not None and _modal_fields(modal):
            return modal
        page.wait_for_timeout(300)
    return None


def _fill_create_form(
    page, human: SyncHumanActions, modal, name: str, description: Optional[str]
) -> Dict[str, Any]:
    """在 modal 内填名称(必填)与简介(可选)→ ``{}`` 或 ``{"error": ...}``。

    两个框都**只在 modal 容器内**找(见 ``_pick_field``)。请求了简介却找不到简介框时
    **报错而不是跳过**:静默丢掉调用方给的字段,是这条产品线最讨厌的那种失败。

    填完名称之后**重新认领一次 modal 再找简介框**:名称一进去就会触发重渲染(字数计数器
    要更新),此时打字前抓的那个简介句柄可能已经脱离文档 —— 往脱离文档的节点里打字**不会
    报错**,简介就这么没了,而回执还是绿的。宁可多找一次。
    """
    fields = _modal_fields(modal)
    # 位置兜底只认实拍的两字段形状:名称在前、简介在后
    name_field = _pick_field(fields, _CREATE_NAME_HINTS,
                             fallback_index=0 if len(fields) in (1, 2) else None)
    if name_field is None:
        return {"error": "collection_name_input_not_found: 创建表单里认不出合集名称输入框"
                         f"(placeholder 未命中 {list(_CREATE_NAME_HINTS)},控件数 "
                         f"{len(fields)} 也对不上实拍的两字段形状);**一个字都没填**——"
                         "绝不退到 modal 之外找输入框(页面上还有笔记标题框)"}
    human.type_text(name_field, name)

    if description:
        live = _find_create_modal(page) or modal
        fields = _modal_fields(live) or fields
        name_field = _pick_field(fields, _CREATE_NAME_HINTS,
                                 fallback_index=0 if len(fields) in (1, 2) else None)
        desc_field = _pick_field(fields, _CREATE_DESC_HINTS,
                                 fallback_index=1 if len(fields) == 2 else None)
        if desc_field is None:
            return {"error": "collection_desc_input_not_found: 传了简介但创建表单里认不出"
                             "简介输入框;不静默丢掉调用方给的字段"}
        if desc_field is name_field:
            return {"error": "collection_desc_input_ambiguous: 简介框与名称框认成了同一个"
                             "控件,拒绝把简介打进名称里"}
        human.type_text(desc_field, description)
    return {}


def _wait_create_submit_enabled(page, modal):
    """等「创建并加入」从禁用翻转成可点 → ``(元素, state)``;超时给 ``(None, state)``。

    每一跳都**连 modal 带按钮重新定位**:填完名称那一下会触发重渲染,此时手上的 modal
    句柄指向的已是脱离文档的旧节点,在它的子树里找按钮只会一路读到**旧的禁用 class**,
    一直读到超时(看着像"平台永远不给我解禁",实际是我们在读一份已经过期的 DOM)。
    传进来的 ``modal`` 只当兜底。**绝不点禁用按钮**。
    """
    deadline = time.monotonic() + _CREATE_ENABLE_TIMEOUT_S
    state: Dict[str, Any] = {"found": False}
    while time.monotonic() < deadline:
        button = _find_create_submit(page, _find_create_modal(page) or modal)
        state = read_create_join_state(button)
        if state.get("enabled"):
            return button, state
        page.wait_for_timeout(300)
    return None, state


def _create_modal_open(page) -> bool:
    """创建表单还开着没有:认领得到 modal **且**里面还有输入控件(名称框没消失)。"""
    modal = _find_create_modal(page)
    return modal is not None and bool(_modal_fields(modal))


def _wait_create_modal_closed(page) -> bool:
    """轮询等创建表单收起;超时返回 False(**表单不收起 = 大概率没提交出去**)。"""
    deadline = time.monotonic() + _CREATE_CLOSE_TIMEOUT_S
    while time.monotonic() < deadline:
        if not _create_modal_open(page):
            return True
        page.wait_for_timeout(400)
    return not _create_modal_open(page)


def _verify_created(
    page,
    human: SyncHumanActions,
    responses: ComponentResponses,
    capture: "_CreateApiCapture",
    account_id: int,
    carrier_note_id: str,
    name: str,
    description: Optional[str],
    seen_api: int,
) -> Dict[str, Any]:
    """点完「创建并加入」之后的**双信号**判定:表单收起 ``且`` 干净列表里有这个名字。

    第二个信号刻意做成"**重进更新页**再开弹层读 ``list_v2``",而不是在原页面上就地回读:

    - 重进会丢弃一切未提交的编辑器状态,所以重进后列表里还有这个名字 = 平台侧**已经独立
      落库**(创建是即时的);读不到 = 这次创建**要随笔记提交才生效**(而我们不提交)。
      这正是首验要分辨的那两种可能,判据本身就把答案带出来了;
    - 原页面就地回读会重蹈播客合集的覆辙 —— 表单/预览区里出现自己刚打的字不构成任何证据。

    载体笔记若真被"并加入"了,重进后合集区会是**已选态**、「加入合集」入口不渲染,弹层就
    开不了 —— 这不是失败,是最强的落库证据,故单独收一支(``confirmed_by=carrier_chip``)。
    """
    modal_closed = _wait_create_modal_closed(page)
    created_api = capture.since(seen_api)
    api_id = parse_created_collection_id(created_api)
    common: Dict[str, Any] = {
        "name": name,
        "description": description,
        # 查重挡在创建之前,走到这里必然没有同名(留成常驻字段,回执形状不随分支变)
        "name_preexisted": False,
        "created_api_capture": created_api,
        "modal_closed": modal_closed,
    }
    if not modal_closed:
        modal = _find_create_modal(page)
        return {
            **common,
            "status": "error",
            "reason": "create_modal_still_open: 点了「创建并加入」但表单一直没收起 ——"
                      "**大概率没提交出去**(播客合集 7 单假绿正是这个形态)。做没做成"
                      "以人工核对为准,**别自动重建**(平台不去重同名)",
            "observed": collection_create_probe(page),
            "create_submit_state": read_create_join_state(_find_create_submit(page, modal)),
            "modal_html": _modal_html(page),
        }

    # 重进更新页 = 丢弃一切未提交状态,拿到的才是"干净列表"
    try:
        open_update_page(page, account_id, carrier_note_id)
    except NoteComponentsError as exc:
        return {
            **common,
            "status": "error",
            "reason": f"verify_reload_failed: 表单已收起,但重进载体笔记更新页失败"
                      f"({exc.reason}),干净列表回读做不了 —— **建没建成未知**,"
                      f"请人工核对(创建 API 取证已随回执带出)",
            "collection_id": api_id,
        }

    opened = _open_collection_popover(page, human, responses)
    if "error" in opened:
        label = read_collection_label(page)
        if opened["error"].startswith("collection_entry_not_found") and _norm(label or "") == name:
            # 重进之后载体笔记已经在这个合集里 —— 未提交状态早被重进丢掉了,还能读到,
            # 只可能是平台侧已独立落库。这是比列表回读更强的证据,照样算成功。
            return {
                **common,
                "status": "done",
                "confirmed_by": "modal_closed_and_carrier_chip",
                "collection_id": api_id,
                "joined_carrier": None,
                "carrier_collection_label": label,
                "note": "载体笔记重进后已处于该合集的已选态,弹层入口因此不渲染、列表读不到,"
                        "故 collection_id 只能取自创建 API 取证(可能为 null)。要拿 id 请对"
                        "**另一篇不在任何合集里**的笔记调 GET /api/accounts/{id}/collections",
            }
        return {
            **common,
            "status": "error",
            "reason": f"verify_list_unreadable: 表单已收起,但干净列表回读失败"
                      f"({opened['error']}) —— **建没建成未知**,请人工核对,别自动重建",
            "collection_id": api_id,
            "carrier_collection_label": label,
        }

    catalog = opened["catalog"]
    hit = next((c for c in catalog if _norm(c["name"]) == name), None)
    label = read_collection_label(page)
    if hit is None:
        return {
            **common,
            "status": "error",
            "reason": "collection_absent_from_fresh_list: 表单收起了,但**重进更新页后的"
                      "干净列表里没有这个合集**。两种可能:①创建根本没落库;②这次创建要"
                      "随笔记提交才生效,而本能力刻意不提交载体笔记。看 created_api_capture "
                      "有没有抓到创建请求即可分辨 —— 请人工核对后再决定,**别自动重建**",
            "collection_id": api_id,
            "collections_seen": [c["name"] for c in catalog][:20],
            "carrier_collection_label": label,
        }
    return {
        **common,
        "status": "done",
        "confirmed_by": "modal_closed_and_in_fresh_list",
        "collection_id": hit["id"],
        # 「并加入」到底随不随笔记提交生效:重进后这个合集的 note_num >0 即已独立落库
        "joined_carrier": hit.get("note_num"),
        "carrier_collection_label": label,
        "created_api_id": api_id,
    }
