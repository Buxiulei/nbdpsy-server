"""笔记三组件(合集 / 引用笔记 / 关联活动)设置器 + 编辑已发布笔记(纯同步,吃已登录 page)。

设计 docs/design/2026-08-01-note-components-design.md 第二 / 三节(真号受控写测试结论)。

两个对外入口:

- ``apply_components``:在**已经打开的编辑器页**上设置三组件(发布新笔记与编辑已发布
  笔记共用这一段);
- ``set_note_components``:编辑已发布笔记的完整流程 —— 深链进更新页 → 权限只读留底 →
  设置组件 → 点发布 → **重进更新页逐项回读**。

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
- **绝不点删除、绝不点合集的 ``.close-icon``(移除合集)、绝不点「取消关联」**:所有会
  撤销/删除的按钮在本模块里只被**读**,读到就当"已是目标态"跳过,永远不点。
- 全程 ``SyncHumanActions``;``page.evaluate`` **只用于只读取证**,不做任何 JS 合成点击
  或 JS 设值。等待用 ``page.wait_for_timeout``(同步 API 下 ``time.sleep`` 期间 response
  监听器一个都不会触发,见 ``creator_note_list`` 模块 docstring)。
"""

import re
import time
from io import BytesIO
from typing import Any, Dict, List, Optional

from loguru import logger

from app.browser.creator_export import _goto_creator
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
_QUOTE_CONTAINER = ".quote-note-container"
_QUOTE_MODAL = ".d-modal.select-note-modal"
_QUOTE_NOTE_CARD = ".d-modal.select-note-modal .select-note-modal__note-grid > .note-card"
_QUOTE_CONFIRM_TEXT = "确认引用"
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
_CATALOG_TIMEOUT_S = 15.0
_ACTIVITY_FLIP_TIMEOUT_S = 8.0
_SUBMIT_TIMEOUT_S = 25.0

# 活动「关联」按钮的点击上限:实测**首次点击静默失效**(零网络请求、按钮不翻转),
# 第二次用完全相同的方式立刻成功。给到 3 次封顶——**只重试同一个活动**,绝不换活动
# (换活动会取消旧的,但旧活动注入正文的话题不回收,反复切换话题会单调累积并真发出去)。
_ACTIVITY_CLICK_ATTEMPTS = 3

# 发布按钮像素定位:小红书红筛(与 atomic_tasks step7 同款阈值)
_RED_MIN_PIXELS = 50

# 活动筛选默认关键词(设计 2.10)。**不要**加"自我""成长"这类过宽的词:实测
# 「howto穿出自我」因"自我"命中,实为穿搭活动。活动频繁上下线,**绝不做死名单**。
DEFAULT_ACTIVITY_KEYWORDS = (
    "心理", "情绪", "焦虑", "抑郁", "疗愈", "精神", "认知", "内耗",
)

# 正文里的话题实体形如 ``#身边的心理学[话题]#``(设计 2.7),从提交前后的正文差里提取
_TOPIC_PATTERN = re.compile(r"#([^#\[\]]+)\[话题\]#")


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


def read_activity_action_text(page, activity_name: str) -> Optional[str]:
    """该活动卡上按钮的当前文案(「关联」/「取消关联」);卡或按钮找不到返回 None。"""
    card = _find_activity_card(page, activity_name)
    if card is None:
        return None
    action = card.query_selector(_ACTIVITY_ACTION)
    return _norm(action.inner_text()) if action is not None else None


def _activity_linked(page, activity_name: str) -> bool:
    """该活动是否已关联 —— 判据是按钮文案翻转成「取消关联」(实测唯一可靠信号)。"""
    return read_activity_action_text(page, activity_name) == _ACTIVITY_LINKED_TEXT


# ---------------- 单个组件的设置动作 ----------------


def _set_collection(
    page, human: SyncHumanActions, responses: ComponentResponses, collection_id: str
) -> Dict[str, Any]:
    """加入合集:点入口 → 等弹层 → 按名字点选 → **当场读回**是否真加上了。

    合集名由 ``list_v2`` 响应里的 id→name 映射得到(调用方传的是 id);映射拿不到就
    报错**不点**——按文案点一个不知道是不是它的条目,等于瞎猜。
    """
    btn = page.query_selector(_COLLECTION_BUTTON)
    if btn is None:
        return {"status": "error", "reason": "collection_entry_not_found: 页面没有合集入口"}

    seen = responses.count(_COLLECTION_API_MARK)  # 基线必须在点击**之前**取
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
        return {
            "status": "error",
            "reason": f"collection_item_not_found: 弹层里没有文案精确等于「{name}」的条目",
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


def _set_quote(
    page, human: SyncHumanActions, responses: ComponentResponses, quoted_note_id: str
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
    """
    container = page.query_selector(_QUOTE_CONTAINER)
    if container is None:
        return {"status": "error", "reason": "quote_entry_not_found: 页面没有引用笔记入口"}

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
        logger.info(
            f"[note_components] 引用目标不在本账号笔记里,改走「{_QUOTE_TAB_OTHER_TEXT}」: "
            f"{quoted_note_id}"
        )
        other = _set_quote_via_other_tab(page, human, responses, quoted_note_id)
        # 两条路都没成时,把两个原因都带上——只报后一个会让人以为"本账号里也没有"这件事
        # 没发生过,排查时又要重走一遍。
        if other.get("status") != "done":
            other["reason"] = f"{other.get('reason')}(先前:{result.get('reason')})"
        return other
    finally:
        # 成功路径上「确认引用」自己会关掉弹窗,这里是幂等收尾:还开着才点「取消」
        _close_quote_modal(page, human)


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
    human.click(cards[0], reason="选中被引用的他人笔记")
    human.wait(0.5, 1.0, context="等选中态生效")

    confirm = _find_button_by_text(page, _QUOTE_CONFIRM_TEXT)
    if confirm is None:
        return {"status": "error", "reason": "quote_confirm_not_found: 弹窗里没有「确认引用」"}
    human.click(confirm, reason="确认引用")
    human.wait(0.8, 1.5, context="等引用生效")

    after = read_quote_text(page)
    if not after or after == before:
        return {
            "status": "error",
            "reason": f"quote_not_applied: 确认后引用区仍是 {after[:40]!r}(设置前 {before[:40]!r})",
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


def _set_quote_in_modal(
    page,
    human: SyncHumanActions,
    responses: ComponentResponses,
    quoted_note_id: str,
    seen: int,
) -> Dict[str, Any]:
    """引用弹窗内的本体流程(弹窗的开与关都由 ``_set_quote`` 负责)。"""
    body = _wait_body(page, responses, _POSTED_API_MARK, _MODAL_TIMEOUT_S, seen)
    notes = ((body or {}).get("data") or {}).get("notes")
    if not isinstance(notes, list) or not notes:
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
        return {
            "status": "error",
            "reason": f"quoted_note_not_in_candidates: 候选列表({len(notes)} 篇)里没有 "
                      f"note_id={quoted_note_id}(候选只含**我的笔记**这一 tab)",
        }

    cards = _wait_quote_cards(page)
    title = _norm((notes[index] or {}).get("display_title"))

    # **按标题找卡,不按下标取卡**(2026-08-03 真号证伪):原实现假设"响应第 i 条 ↔
    # 弹窗第 i 张卡",该假设当初就写明"没有实测背书" —— 现在有反例了:实测第 6 张卡是
    # 「心理咨询师-彭旱雨…」,而接口第 6 条是「粤语咨询师-黄安麟…」。同序不成立,
    # 于是每次都判 quote_card_title_mismatch,引用功能整体不可用。
    #
    # 改成在**全部卡片**里找文案包含该标题的那一张:命中恰好一张才用,0 张或多张都拒绝
    # (多张 = 同号有重名笔记,认不准就不认,与本模块一贯纪律一致)。
    if not title:
        # 空标题笔记没法按文案认卡。**不退回下标**(那正是被证伪的假设),交给
        # 「他人笔记」那条路按 note_id 精确检索 —— 它不依赖任何顺序假设。
        return {
            "status": "error",
            "reason": "quoted_note_not_in_candidates: 目标是空标题笔记,"
                      "「我的笔记」只能按文案认卡,改走按 note_id 精确检索",
        }
    hits = [c for c in cards if title in _norm(c.inner_text())]
    if len(hits) != 1:
        return {
            "status": "error",
            "reason": f"quote_card_not_unique_by_title: 弹窗 {len(cards)} 张卡里,"
                      f"文案含平台标题「{title}」的有 {len(hits)} 张(要求恰好 1 张,绝不猜)",
        }
    card = hits[0]

    human.click(card, reason=f"选中被引用笔记「{title[:15]}」")
    human.wait(0.5, 1.0, context="等选中态生效")

    confirm = _find_button_by_text(page, _QUOTE_CONFIRM_TEXT)
    if confirm is None:
        return {"status": "error", "reason": "quote_confirm_not_found: 弹窗里没有「确认引用」"}
    human.click(confirm, reason="确认引用")
    human.wait(0.8, 1.5, context="等引用生效")

    quoted = read_quote_text(page)
    if title and title not in quoted:
        return {
            "status": "error",
            "reason": f"quote_not_applied: 确认后引用区是 {quoted[:40]!r},没出现「{title}」",
            "observed": quoted,
        }
    return {"status": "done", "quoted_note_id": str(quoted_note_id), "title": title}


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
        return {
            "status": "error",
            "reason": f"activity_card_not_found: 页面上没有名为「{name}」的活动卡",
        }
    if action_text == _ACTIVITY_LINKED_TEXT:
        # 本来就关联着 —— 绝不点「取消关联」
        return {"status": "skipped", "activity_id": str(activity_id), "name": name,
                "reason": "该活动本就已关联"}
    if action_text != _ACTIVITY_UNLINKED_TEXT:
        return {
            "status": "error",
            "reason": f"activity_action_unexpected: 按钮文案是 {action_text!r},"
                      f"既不是「{_ACTIVITY_UNLINKED_TEXT}」也不是「{_ACTIVITY_LINKED_TEXT}」,拒绝点击",
        }

    for attempt in range(1, _ACTIVITY_CLICK_ATTEMPTS + 1):
        card = _find_activity_card(page, name)
        action = card.query_selector(_ACTIVITY_ACTION) if card is not None else None
        if action is None:
            return {
                "status": "error",
                "reason": f"activity_action_detached: 第 {attempt} 次尝试时「{name}」的按钮不见了",
            }
        if _norm(action.inner_text()) != _ACTIVITY_UNLINKED_TEXT:
            break  # 已经翻转(或状态变了),交下面统一复核
        human.click(action, reason=f"关联活动「{name}」(第 {attempt} 次)")
        if _wait_activity_flip(page, name):
            logger.info(f"[note_components] ✓ 活动「{name}」已关联(第 {attempt} 次点击生效)")
            return {"status": "done", "activity_id": str(activity_id), "name": name,
                    "clicks": attempt}
        logger.warning(
            f"[note_components] 活动「{name}」第 {attempt} 次点击静默失效"
            f"(文案未翻转、无 toast),重试同一个活动"
        )

    if _activity_linked(page, name):
        return {"status": "done", "activity_id": str(activity_id), "name": name}
    return {
        "status": "error",
        "reason": f"activity_not_linked: 点了 {_ACTIVITY_CLICK_ATTEMPTS} 次「{name}」,"
                  f"按钮始终没翻转成「{_ACTIVITY_LINKED_TEXT}」(静默失效)",
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


def apply_components(
    page,
    human: SyncHumanActions,
    responses: ComponentResponses,
    *,
    collection_id: Optional[str] = None,
    quoted_note_id: Optional[str] = None,
    activity_id: Optional[str] = None,
) -> Dict[str, Dict[str, Any]]:
    """在**已打开的编辑器页**上设置三组件,逐项返回结果(单项失败不阻断其余项)。

    只处理传了 id 的项;返回形如
    ``{"collection": {"status": "done"|"skipped"|"error", ...}, ...}``,键只含请求过的组件。

    这里的 ``done`` 是**编辑器内**回读确认(合集区显示了名字 / 引用区出现了标题 / 活动
    按钮翻转成「取消关联」),**不等于**服务端接受 —— 私密笔记的合集绑定会被服务端静默
    丢弃(设计 2.6),那要靠提交后重进页面回读才发现。
    """
    steps = (
        ("collection", collection_id, lambda cid: _set_collection(page, human, responses, cid)),
        ("quote", quoted_note_id, lambda nid: _set_quote(page, human, responses, nid)),
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


# ---------------- 编辑已发布笔记:完整流程 ----------------


def set_note_components(
    page,
    account_id: int,
    note_id: str,
    *,
    collection_id: Optional[str] = None,
    quoted_note_id: Optional[str] = None,
    activity_id: Optional[str] = None,
) -> Dict[str, Any]:
    """给一篇**已发布**笔记设置三组件:进更新页 → 设置 → 提交 → 逐项回读。

    Args:
        page: 已建好登录态的同步 Playwright Page(SyncClient.start 之后)。
        account_id: 账号 id(日志用)。
        note_id: 目标笔记的平台 id(深链定位,设计 3.2 —— 台账 title 会过期,只认 id)。
        collection_id / quoted_note_id / activity_id: 要设置的三组件,均可选,至少一个。

    Returns:
        ``{"status": "done"|"partially_applied"|"failed", "applied": {...}, "failed": [...],
        "permission_before"/"permission_after"/"permission_preserved", "body_appended",
        "topics_injected", "submitted", "components": {逐项明细}}``;
        一项都没生效时额外带 ``"error"`` 键(调用方据此把台账落 error,而不是假 done)。

    Raises:
        NoteComponentsError: 前置/硬失败 —— 页面进不去、权限读不出、提交前权限已变。
            这几种情况下**一次发布都没点**,笔记原样未动。
    """
    human = SyncHumanActions(page)
    responses = ComponentResponses()
    responses.attach(page)
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
        logger.info(
            f"[note_components] 账号{account_id} note_id={note_id} "
            f"权限={permission_before!r} 标题={title_on_page[:20]!r}"
        )

        # ② 逐项设置(单项失败不阻断其余项)
        outcomes = apply_components(
            page, human, responses,
            collection_id=collection_id,
            quoted_note_id=quoted_note_id,
            activity_id=activity_id,
        )
        body_after = read_body_text(page)
        in_editor_ok = [k for k, v in outcomes.items() if v["status"] in ("done", "skipped")]
        if not in_editor_ok:
            # 编辑器里一项都没设上 —— 提交毫无意义,而每次提交都是一次全量覆盖,不做
            logger.warning(
                f"[note_components] 账号{account_id} note_id={note_id}: "
                f"编辑器内一项都没设上,不点发布(避免无意义的全量覆盖提交)"
            )
            return _compose(
                note_id, outcomes, {}, submitted=False,
                permission_before=permission_before, permission_after=permission_before,
                body_before=body_before, body_after=body_after,
            )

        # ③ 点发布前再读一次权限:不符立刻中止,**不点** —— 这是硬约束,不是可选校验
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

        # ④ 重进更新页逐项回读 —— success:true 不等于生效(设计 2.6 合集被静默丢弃)
        human.wait(1.5, 3.0, context="等提交落地")
        verified, permission_after = _verify_after_submit(
            page, account_id, note_id,
            collection_id=collection_id, quoted_note_id=quoted_note_id,
            activity_id=activity_id, outcomes=outcomes, responses=responses,
        )

        result = _compose(
            note_id, outcomes, verified, submitted=submitted is not None,
            permission_before=permission_before, permission_after=permission_after,
            body_before=body_before, body_after=body_after,
        )
        # ⑤ 权限被改动:大声告警 + 尝试改回(只有实测支持的两档能改回)
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
    responses: ComponentResponses,
) -> tuple:
    """重进更新页,逐项回读三组件与权限;返回 ``({组件: bool|None}, 权限文案|None)``。

    回读本身失败(页面进不去)不抛错 —— 那时"改没改成"确实未知,一律记 None,由调用方
    如实上报为未确认,而不是乐观当成功。
    """
    verified: Dict[str, Optional[bool]] = {}
    try:
        open_update_page(page, account_id, note_id)
    except NoteComponentsError as exc:
        logger.error(f"[note_components] 回读进不去更新页(生效情况未确认): {exc.reason}")
        return {k: None for k in outcomes}, None

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
        if title:
            verified["quote"] = title in quoted
        else:
            # 跨账号引用(走「他人笔记」)拿不到标题:目标典型就是主号那篇**空标题**的
            # 小助手联系方式笔记,拿标题去比对必然失败。退而用"引用区跟设置前不一样了"
            # 作判据 —— 比"没报错就算成功"强,也是空标题下唯一站得住的口径。
            # 候选卡文案不能拿来当 title:卡上是「笔记封面 作者 ♡5」这类 UI 文字,
            # 与引用区的显示文案根本不是一回事,用它比对必然假阴性。
            before = outcome.get("quote_text_before") or ""
            verified["quote"] = bool(quoted) and quoted != before
        if not verified["quote"]:
            logger.error(
                f"[note_components] 引用回读未生效:期望"
                f"{('包含「%s」' % title) if title else '引用区非空且已变化'},"
                f"实读 {quoted[:40]!r}"
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
    return verified, read_permission_label(page)


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
) -> Dict[str, Any]:
    """把逐项结果汇总成对外结果;**一项都没生效时带 ``error`` 键**(绝不假 done)。

    ``applied[k]`` 三态:``True``=回读确认生效;``False``=回读确认没生效;``None``=没能
    回读(未确认)。**只有 True 才算数** —— 这条产品线的失败是静默的,"没报错"不是凭据。
    """
    applied = {k: verified.get(k) for k in outcomes}
    failed = [
        {"component": k, "reason": (outcomes[k].get("reason")
                                    or f"{k}_not_verified: 提交后回读未确认生效")}
        for k in outcomes if applied.get(k) is not True
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
    }
    ok_count = sum(1 for v in applied.values() if v is True)
    if ok_count == len(applied) and applied:
        result["status"] = "done"
    elif ok_count:
        result["status"] = "partially_applied"
    else:
        result["status"] = "failed"
        result["error"] = (
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
