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
# 已发布笔记编辑的两个实现模块(T4/T5)。**单向依赖**:它们绝不 import 本模块(会成环),
# 编排点在本文件 —— 这正是设计第二节那条"结构性修正"(新逻辑不再堆进本已 1541 行的文件,
# 但提交路径仍然只有这里一条)。``add_images`` / ``remove_images`` 改名 import:
# ``set_note_components`` 的同名入参会在函数体里把模块级函数遮住。
from app.browser.note_editing import (
    apply_content_edit,
    apply_title_edit,
    content_prefix_ok,
    image_count_equation,
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
_QUOTE_CONTAINER = ".quote-note-container"
_QUOTE_MODAL = ".d-modal.select-note-modal"
_QUOTE_NOTE_CARD = ".d-modal.select-note-modal .select-note-modal__note-grid > .note-card"
_QUOTE_CONFIRM_TEXT = "确认引用"
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
    human.click(cards[0], reason="选中被引用的他人笔记")
    human.wait(0.5, 1.0, context="等选中态生效")

    confirm = _find_button_by_text(page, _QUOTE_CONFIRM_TEXT)
    if confirm is None:
        return {"status": "error", "reason": "quote_confirm_not_found: 弹窗里没有「确认引用」"}
    human.click(confirm, reason="确认引用")
    human.wait(0.8, 1.5, context="等引用生效")

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


def _wait_all_candidate_notes(page, responses: ComponentResponses, seen: int) -> List[dict]:
    """收齐引用候选列表的**全部分页**并按接口顺序拼起来(去重后返回)。

    **候选列表是分页的**(2026-08-03 回放夹具实测):弹窗打开时连发
    ``posted?tab=1&page=0``(11 条)与 ``page=1``(10 条),两页一起渲染成 20 张卡。
    而原实现用 ``_wait_body`` 取 ``latest`` —— **只看得见最后一页**,于是第 0 页的任何
    一篇都会被报成「候选列表里没有」,尽管它就在弹窗里摆着。生产日志里那些
    ``quoted_note_not_in_candidates`` 就是这么来的。

    等法:先等到第一页,之后只要还有新页到达就继续等,连续 ``_PAGE_SETTLE_S`` 没有新页
    才收工 —— 不能只等一页(会漏),也不能死等满超时(白白拖慢每一次引用)。

    按 note_id 去重但**保持首次出现的顺序**:顺序是选卡算法的依据(见
    ``_pick_untitled_card``),打乱它等于把刚修好的东西又弄坏。
    """
    deadline = time.monotonic() + _MODAL_TIMEOUT_S
    last_count = seen
    settled_at = None
    while time.monotonic() < deadline:
        count = responses.count(_POSTED_API_MARK)
        if count > last_count:
            last_count = count
            settled_at = time.monotonic()
        elif settled_at is not None and time.monotonic() - settled_at >= _PAGE_SETTLE_S:
            break
        page.wait_for_timeout(300)

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


def _set_quote_in_modal(
    page,
    human: SyncHumanActions,
    responses: ComponentResponses,
    quoted_note_id: str,
    seen: int,
) -> Dict[str, Any]:
    """引用弹窗内的本体流程(弹窗的开与关都由 ``_set_quote`` 负责)。"""
    notes = _wait_all_candidate_notes(page, responses, seen)
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

    human.click(card, reason=f"选中被引用笔记「{title[:15] or '(空标题)'}」")
    human.wait(0.5, 1.0, context="等选中态生效")

    confirm = _find_button_by_text(page, _QUOTE_CONFIRM_TEXT)
    if confirm is None:
        return {"status": "error", "reason": "quote_confirm_not_found: 弹窗里没有「确认引用」"}
    human.click(confirm, reason="确认引用")
    human.wait(0.8, 1.5, context="等引用生效")

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


def apply_original_declaration(page, human: SyncHumanActions) -> Dict[str, Any]:
    """打开「原创声明」开关 → ``{"status": "done"|"skipped"|"error", ...}``。

    夹具实证(2026-08-05 真号采集 content_settings.json):
    - 开关 ``.original-wrapper .d-switch``,状态在隐藏 ``input.checked``(class 不翻转,
      不能拿 class 判态);
    - 首次点开会弹**无底栏**说明弹窗(``d-modal-no-footer``,只有右上 X,没有确认按钮),
      弹窗必须关掉——弹窗不关会盖住发布按钮(2026-08-02 事故同型);
    - 已是开态 → ``skipped`` 零点击(判「现在是什么状态」不判「变了没有」,幂等重跑安全)。

    X 关弹窗后 checked 是否保持**没有**直接夹具证据(采集时是二次点开关恢复的),故防御:
    关弹窗后回读,不是开态就再点一轮,封顶 ``_ORIGINAL_TOGGLE_TRIES``;终判一律以回读
    checked 为准——这条产品线的失败是静默的,"没报错"不算数。
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
        ("collection", collection_id, lambda cid: _set_collection(
            page, human, responses, cid, collection_name=collection_name)),
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
    collection_id: Optional[str], quoted_note_id: Optional[str], activity_id: Optional[str]
) -> Dict[str, Dict[str, Any]]:
    """弃提交时组件步一步都没走:请求过的组件如实记「因前序失败未执行」。

    不记的话,``failed`` 里就只剩编辑步,调用方会以为组件已经设上了(其实连弹层都没开)。
    """
    return {
        key: {"status": "error", "reason": _SKIPPED_REASON}
        for key, value in (
            ("collection", collection_id),
            ("quote", quoted_note_id),
            ("activity", activity_id),
        ) if value
    }


# ---------------- 编辑已发布笔记:完整流程 ----------------


def set_note_components(
    page,
    account_id: int,
    note_id: str,
    *,
    collection_id: Optional[str] = None,
    collection_name: Optional[str] = None,
    quoted_note_id: Optional[str] = None,
    activity_id: Optional[str] = None,
    title: Optional[str] = None,
    content: Optional[str] = None,
    add_images: Optional[List[str]] = None,
    remove_image_indexes: Optional[List[int]] = None,
    expected_image_count: Optional[int] = None,
) -> Dict[str, Any]:
    """给一篇**已发布**笔记设组件 / 改标题正文 / 增删图片:进更新页 → 改 → 提交 → 回读。

    **全流程只有一次提交**(⑧那一次 ``click_publish``),这是方案 A 的立命之本:提交是
    全量覆盖语义,提交次数就是风险次数。文本 / 图片 / 组件混在一次请求里也只覆盖一次。

    Args:
        page: 已建好登录态的同步 Playwright Page(SyncClient.start 之后)。
        account_id: 账号 id(日志用)。
        note_id: 目标笔记的平台 id(深链定位,设计 3.2 —— 台账 title 会过期,只认 id)。
        collection_id / quoted_note_id / activity_id: 要设置的三组件,均可选。
        title: 整体替换标题;``None``=不改,``""``=清空(两者语义不同,编辑设计 3.1)。
        content: 整体替换正文;``None``=不改(不支持清空,编辑设计 1.2)。
        add_images: 追加到图序末尾的**本地路径**列表(REST 层已落好盘)。
        remove_image_indexes: 按发布态图序删除的 1-based 下标。
        expected_image_count: 调用方声明的现有图数;有图片操作时必给,与页面实数不符
            → 一次图片点击都不发生,整单弃提交(编辑设计 4.3)。

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
                _skipped_components(collection_id, quoted_note_id, activity_id),
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
            quoted_note_id=quoted_note_id,
            activity_id=activity_id,
        )
        body_after = read_body_text(page)
        # ⑥ 提交决策:组件 done/skipped 或**任一编辑步 done** 都算"有东西可提交"
        in_editor_ok = [k for k, v in outcomes.items() if v["status"] in ("done", "skipped")]
        edits_ok = [k for k, v in edits["outcomes"].items() if v.get("status") == "done"]
        if not in_editor_ok and not edits_ok:
            # 编辑器里一项都没设上 —— 提交毫无意义,而每次提交都是一次全量覆盖,不做
            logger.warning(
                f"[note_components] 账号{account_id} note_id={note_id}: "
                f"编辑器内一项都没设上,不点发布(避免无意义的全量覆盖提交)"
            )
            return _compose(
                note_id, outcomes, {}, submitted=False,
                permission_before=permission_before, permission_after=permission_before,
                body_before=body_before, body_after=body_after,
                edit_outcomes=edits["outcomes"], images_before=images_before,
                images_after=None, topics_dropped=edits["topics_dropped"],
            )

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
            collection_id=collection_id, quoted_note_id=quoted_note_id,
            activity_id=activity_id, outcomes=outcomes, responses=responses,
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
        ))}
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
