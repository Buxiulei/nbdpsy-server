"""笔记三组件(合集 / 引用笔记 / 关联活动)单测(不起真浏览器)。

锁的是设计第二节那些**真号实测出来的静默失败**,以及为它们加的每一道闸:

- **静默失败必须被识别**:合集被服务端静默丢弃(``success:true`` 照返)、活动按钮点了
  不翻转 —— 两者都要落 ``partially_applied`` / ``failed``,**绝不报 done**;
- **权限保全**:权限读不出 → 一次都不点;提交前权限变了 → **不点发布**;提交后权限被改
  → 告警 + 尝试改回;
- **活动重试只重试同一个活动、有上限**:绝不"换个活动重试"(换活动会取消旧的,但旧活动
  注入正文的话题不回收,反复切换话题会单调累积并真发出去);
- **绝不点撤销类按钮**:「取消关联」「创建合集」在任何路径下都不许被点到;
- **定位优先 note_id**:引用候选按 note_id 映射并用标题交叉校验,对不上就拒绝;
  可见性切换先把 note_id 翻译成平台当前标题(台账 title 会过期);
- **活动筛选的假阳性排除**:默认词表不得命中「howto穿出自我」这类穿搭活动。

patch 纪律:打在被测模块的命名空间(顶层 import 的依赖),不是源模块。
"""

import time

import pytest

from app.browser import matrix_interact as mi
from app.browser import note_components as bnc
from app.browser import note_visibility as bnv
from app.services import browser_jobs_repo as repo
from app.services import note_components as svc


# ---------------- 测试替身:一个能被点出副作用的假编辑器 ----------------


class _El:
    """假元素:只提供被测代码真用到的能力(读文本/读 value/取矩形/子查询/被点)。"""

    def __init__(self, text="", *, on_click=None, value="", children=None, href=None,
                 on_type=None, on_hover=None, on_files=None, cls=None, attrs=None,
                 rect=None):
        self._text = text
        self._cls = cls
        self._attrs = dict(attrs or {})   # 任意属性(「确认引用」的 disabled 要读它)
        self._rect = rect                 # 需要几何关系时给(引用弹窗判"卡在页脚之上没")
        self.on_click = on_click
        self.on_type = on_type   # 被 type_text 输入时的副作用(他人笔记检索框用)
        self.on_hover = on_hover  # 被 hover 时的副作用(合集 chip 的 × 是 hover 才显)
        self.on_files = on_files  # 被 set_input_files 灌文件时的副作用(改封面用)
        self._value = value
        self._children = children or {}
        self._href = href

    def inner_text(self):
        return self._text

    def input_value(self):
        return self._value

    def is_visible(self):
        return True

    def get_attribute(self, name):
        if name in self._attrs:
            return self._attrs[name]
        if name == "class":
            return self._cls
        return self._href if name == "href" else None

    def bounding_box(self):
        return self._rect or {"x": 10.0, "y": 20.0, "width": 100.0, "height": 40.0}

    def query_selector(self, sel):
        hits = self._children.get(sel) or []
        return hits[0] if hits else None

    def query_selector_all(self, sel):
        return list(self._children.get(sel) or [])

    def set_input_files(self, paths):
        if self.on_files:
            self.on_files(paths)


# 引用弹窗的几何:候选卡在页脚**之上**(照夹具的相对关系),这样"卡在不在可视区里"
# 这条判据在假页面上也是真的走了一遍,而不是恰好读到同一个默认矩形。
_CARD_RECT = {"x": 10.0, "y": 100.0, "width": 100.0, "height": 40.0}
_FOOTER_RECT = {"x": 10.0, "y": 300.0, "width": 68.0, "height": 36.0}


class _Human:
    """假拟人层:点击直接触发元素副作用,并记录 (reason, 文案) 供断言"不该点的没点"。"""

    def __init__(self, _page=None):
        self._page = _page
        self.clicks = []
        self.typed = []
        self.hovers = []
        self.scrolls = 0

    def wait(self, *_a, **_kw):
        pass

    def scroll(self, *_a, **_kw):
        self.scrolls += 1
        # 真页面上滚列表会触发懒加载;假页面把这条迁移交给 Editor(它才知道还剩几页)
        editor = getattr(self._page, "editor", None)
        if editor is not None:
            editor.on_scroll()

    def scroll_to_element(self, _el):
        pass

    def hover(self, target=None, *, reason="", **_kw):
        self.hovers.append((reason, getattr(target, "inner_text", lambda: "")()
                            if hasattr(target, "inner_text") else target))
        if hasattr(target, "on_hover") and target.on_hover:
            target.on_hover()

    def navigate(self, *_a, **_kw):
        pass

    def click(self, target, *, reason="", **_kw):
        text = target.inner_text() if hasattr(target, "inner_text") else ""
        self.clicks.append((reason, text))
        if hasattr(target, "on_click") and target.on_click:
            target.on_click()

    def type_text(self, target, text, **_kw):
        self.typed.append(text)
        if hasattr(target, "on_type") and target.on_type:
            target.on_type(text)

    @property
    def texts(self):
        return [t for _r, t in self.clicks]


class Editor:
    """假编辑器页模型:三组件状态 + 点击副作用 + 接口响应回放。

    每个可配的"坏行为"都对应设计里一条实测结论:``drop_collection_on_submit``=服务端静默
    丢弃合集;``silent_activity_clicks``=活动首次点击静默失效;``permission_after_*``=
    权限被改动。
    """

    def __init__(
        self,
        *,
        permission="公开可见",
        collection=None,
        collections=(("c1", "咨询师简介"),),
        activities=(("43561", "身边的心理学", "心理科普活动"),),
        linked_activity=None,
        notes=(("n-quote", "心理咨询师-徐瑞恒"), ("n-other", "另一篇")),
        body="原有正文",
        title="目标笔记",
        drop_collection_on_submit=False,
        close_icon_hover_gated=True,
        close_icon_absent=False,
        silent_close_icon=False,
        modal_after_close_icon=None,
        silent_activity_clicks=0,
        permission_before_submit=None,
        permission_after_submit=None,
        quote_card_titles=None,
        quote_select_silent=0,
        quote_pages=None,
        other_notes=(),
        original_declared=False,
        original_consent_effective=True,
        original_row_absent=False,
        original_persists_on_readback=True,
        cover_no_cover=True,
        cover_entry_absent=False,
        cover_persists_on_readback=True,
        drop_topics_on_submit=(),
    ):
        self.permission = permission
        self.row_band_probes = []  # 防遮挡带探测记录(选择器)
        self.collection = collection
        self.collections = list(collections)
        self.activities = [
            {"id": i, "name": n, "desc": d, "linked": n == linked_activity}
            for i, n, d in activities
        ]
        self.notes = list(notes)
        # 候选列表**懒加载**:quote_pages 给"每滚一次才放出多少条"的节奏,None=一次给全
        # (老行为)。放出来的条数同时决定接口发几页与渲染几张卡 —— 真页面就是这样。
        self.quote_pages = list(quote_pages) if quote_pages else None
        self.quote_released = len(self.notes) if self.quote_pages is None else 0
        self.quote_page_i = 0
        self.quote_card_titles = (
            list(quote_card_titles) if quote_card_titles is not None
            else [t for _i, t in self.notes]
        )
        self.quote_select_silent = quote_select_silent
        self.body = body
        self.title = title
        self.quote_text = "引用笔记"
        self.popover_open = False
        self.modal_open = False
        # 「他人笔记」tab:{note_id: 卡片文案};切过去后要检索才出候选(真号实测)
        self.other_notes = dict(other_notes)
        self.other_tab = False
        self.other_query = ""

        self.drop_collection_on_submit = drop_collection_on_submit
        # ── 移出合集的可配坏行为(用户 2026-08-07 实拍锁定操作面,其余按 fail-loud 预设)──
        # hover_gated: × 只在悬停 chip 后才出现(实拍事实);absent: 悬停了也没有 ×;
        # silent: 点了 × chip 却不消失;modal_after: 点 × 后弹出一个我们没验证过的弹窗。
        self.close_icon_hover_gated = close_icon_hover_gated
        self.close_icon_absent = close_icon_absent
        self.silent_close_icon = silent_close_icon
        self.modal_after_close_icon = modal_after_close_icon
        self.chip_hovered = False
        self.modal_text = None
        self.silent_activity_clicks = silent_activity_clicks
        self.permission_before_submit = permission_before_submit
        self.permission_after_submit = permission_after_submit
        # ── 原创声明补录(夹具 content_settings.json 从编辑页 /publish/update 采;
        # 协议弹窗形态与发布页夹具 original_modal_publish_page.json 一致)──
        # declared_for_real = 平台侧真的声明了;checked = 开关行回读到的 checked
        # (点开关会**乐观**翻 true,X 关掉弹窗又重置 —— 探针实证的那个坑)。
        self.original_row_absent = original_row_absent
        self.original_consent_effective = original_consent_effective
        self.original_persists_on_readback = original_persists_on_readback
        self.original_declared_for_real = original_declared
        self.original_checked = original_declared
        self.original_modal_open = False
        self.original_consent_ticked = False
        self.original_closed_by_x = False
        # ── 改封面(更新页「设置封面」弹窗链)──
        # no_cover = 封面区 .operator 还带着 noCover(平台自动首帧);
        # declared_for_real = 平台侧真的已是自定义封面(提交才落地);
        # persists_on_readback = 提交后重进页面还认不认(false 复刻"静默丢弃")。
        self.cover_entry_absent = cover_entry_absent
        self.cover_persists_on_readback = cover_persists_on_readback
        # 补话题:提交后平台**静默丢弃**这些话题(复刻"点了却没真挂上",回读判据的试金石)
        self.drop_topics_on_submit = tuple(drop_topics_on_submit)
        self.cover_declared_for_real = not cover_no_cover
        self.cover_no_cover = cover_no_cover
        self.cover_fingerprint = "cdn/frame-0"
        self.cover_modal_open = False
        self.cover_upload_tab = False
        self.cover_files = []
        self.cover_chosen = False
        self.submitted = 0
        self.page = _FakePage(self)

    # ---- 接口响应回放(页面自己发,我们只被动读) ----

    def load(self):
        """一次页面加载:活动列表随页面返回(设计 2.9)。"""
        self.popover_open = False
        self.modal_open = False
        # 重进页面:弹窗态清空,开关回读取平台真值(可配是否回显已声明态)
        self.original_modal_open = False
        self.original_consent_ticked = False
        self.original_checked = (
            self.original_declared_for_real and self.original_persists_on_readback
        )
        # 重进页面:封面弹窗态清空,封面区回读取平台真值(可配提交后认不认)
        self.cover_modal_open = False
        self.cover_upload_tab = False
        self.cover_chosen = False
        self.cover_no_cover = not (
            self.cover_declared_for_real and self.cover_persists_on_readback
        )
        # 指纹随封面态同步:换成功且落地 → 自定义指纹;被静默丢弃则退回平台首帧指纹
        # (回读强信号靠 fingerprint_before≠fingerprint_after 判定,丢弃时两者必须相等)
        self.cover_fingerprint = "cdn/frame-0" if self.cover_no_cover else "cdn/custom-1"
        self.page.emit(
            "https://creator.xiaohongshu.com/api/galaxy/v2/creator/activity_center/list",
            {"data": {"list": [
                {"id": a["id"], "name": a["name"], "desc": a["desc"]}
                for a in self.activities
            ]}},
        )

    def _emit_collections(self):
        self.page.emit(
            "https://edith.xiaohongshu.com/api/sns/v1/note/collection/pc/list_v2",
            {"data": {"collection_info_list": [
                {"id": cid, "name": name, "desc": "", "note_num": 10}
                for cid, name in self.collections
            ]}},
        )

    def _emit_posted(self, start, end):
        self.page.emit(
            "https://creator.xiaohongshu.com/api/galaxy/v2/creator/note/user/posted?tab=1",
            {"data": {"notes": [
                {"id": nid, "display_title": t} for nid, t in self.notes[start:end]
            ]}},
        )

    def _emit_next_quote_page(self):
        """放出候选列表的下一页;没有下一页返回 False(什么都不发,与真页面同语义)。"""
        if self.quote_pages is None:
            self._emit_posted(0, len(self.notes))   # 老行为:一次给全
            return True
        if self.quote_page_i >= len(self.quote_pages):
            return False
        size = self.quote_pages[self.quote_page_i]
        start, end = self.quote_released, min(self.quote_released + size, len(self.notes))
        self.quote_page_i += 1
        self.quote_released = end
        self._emit_posted(start, end)
        return True

    def on_scroll(self):
        """弹窗列表被滚 → 平台懒加载放出下一页(这正是原实现从来没触发过的那一步)。

        ``quote_pages is None`` 时列表一次就给全了,再滚也不会有新响应 —— 如实照做。
        """
        if self.modal_open and not self.other_tab and self.quote_pages is not None:
            self._emit_next_quote_page()

    def submit(self):
        """点发布:服务端处理 —— 这里回放"合集被静默丢弃"与"权限被改"两种实测坏行为。"""
        self.submitted += 1
        for name in self.drop_topics_on_submit:
            self.body = self.body.replace(f"#{name}[话题]#", "").strip()
        if self.cover_chosen:
            self.cover_declared_for_real = True
        if self.drop_collection_on_submit:
            self.collection = None
        if self.permission_after_submit is not None:
            self.permission = self.permission_after_submit
        self.page.emit(
            "https://edith.xiaohongshu.com/web_api/sns/capa/postgw/note/update",
            {"result": 0, "success": True, "msg": ""},
        )

    # ---- 点击副作用 ----

    def _hover_chip(self):
        self.chip_hovered = True

    def _click_close_icon(self):
        """点 × :实拍只锁到"点 × 即移出",生效时机与确认弹窗未验证,故两者都可配。"""
        if self.modal_after_close_icon is not None:
            self.modal_text = self.modal_after_close_icon
            return
        if self.silent_close_icon:
            return
        self.collection = None
        self.chip_hovered = False

    def _open_popover(self):
        self.popover_open = True
        self._emit_collections()

    def _choose_collection(self, name):
        self.collection = name
        self.popover_open = False

    def _open_modal(self):
        self.modal_open = True
        if self.quote_pages is not None:
            self.quote_page_i = 0
            self.quote_released = 0
        self._emit_next_quote_page()

    def _confirm_quote(self):
        if self._selected_quote is None:
            return
        self.quote_text = f"引用了 {self._selected_quote}"
        self.modal_open = False

    def _cancel_quote(self):
        self.modal_open = False

    def _set_other_query(self, text):
        self.other_query = text
        # 真号实测:输入即触发 GET creator/search/others/note?note_link=<输入>,
        # 响应 data 带 note_id / display_title。逐字输入时中间态也会发,这里如实回放。
        hit = self.other_notes.get(text)
        self.page.emit(
            f"https://creator.xiaohongshu.com/api/galaxy/v2/creator/search/others/note?note_link={text}",
            {"data": {"note_id": text, "display_title": ""} if hit else None},
        )

    def _switch_other(self):
        self.other_tab = True   # 真号实测:切 tab 是纯前端,零网络请求

    def _switch_mine(self):
        self.other_tab = False

    _selected_quote = None

    def _select_quote(self, title):
        """点候选卡:``quote_select_silent`` 轮之内静默失效(复刻 2026-08-13 号 7 现场)。

        真页面上"点了卡片但选中没生效"是可观测的 —— 「确认引用」保持禁用态。
        """
        if self.quote_select_silent > 0:
            self.quote_select_silent -= 1
            return
        self._selected_quote = title

    # ---- 原创声明的点击副作用(复刻真号探针实证的三段语义) ----

    def _click_original_switch(self):
        """点开关:checked **乐观**翻 true,同时弹出协议弹窗(两份夹具都如此)。"""
        self.original_checked = True
        self.original_modal_open = True

    def _tick_original_consent(self):
        """勾「我已阅读并同意」;撞上《原创声明须知》超链接时事件被吃掉,勾不上。"""
        if self.original_consent_effective:
            self.original_consent_ticked = True

    def _confirm_original(self):
        """点「声明原创」:没勾同意时按钮是 disabled,点了也没用。"""
        if not self.original_consent_ticked:
            return
        self.original_modal_open = False
        self.original_declared_for_real = True

    def _close_original_modal(self):
        """点 X 关弹窗:探针实证 checked 被重置回未声明态。"""
        self.original_modal_open = False
        self.original_closed_by_x = True
        self.original_checked = self.original_declared_for_real

    # ---- 改封面的点击副作用(更新页「设置封面」弹窗链) ----

    def _open_cover_modal(self):
        self.cover_modal_open = True
        self.cover_upload_tab = False   # 默认停在「截取封面」tab

    def _switch_cover_upload_tab(self):
        self.cover_upload_tab = True    # 图片 file input 切过来才懒挂载

    def _choose_cover_files(self, paths):
        self.cover_files = list(paths)

    def _confirm_cover(self):
        if not self.cover_files:
            return                      # 没选图时「确定」是禁用态,点了也没用
        self.cover_modal_open = False
        self.cover_chosen = True
        self.cover_no_cover = False
        self.cover_fingerprint = "cdn/custom-1"

    def _cancel_cover(self):
        self.cover_modal_open = False

    def _cover_modal_el(self):
        return _El("设置封面 截取封面 上传封面 取消 确定", children={
            bnc._COVER_MODAL_TAB: [
                _El("截取封面"),
                _El("上传封面", on_click=self._switch_cover_upload_tab),
            ],
            bnc._COVER_MODAL_FILE_INPUT: (
                [_El("", cls="upload-input", on_files=self._choose_cover_files)]
                if self.cover_upload_tab else []
            ),
            bnc._COVER_MODAL_CONFIRM: [
                _El("确定", cls="d-button btn-confirm", on_click=self._confirm_cover)
            ],
            bnc._COVER_MODAL_CANCEL: [_El("取消", cls="cancelBtn", on_click=self._cancel_cover)],
        })

    def _click_activity(self, name):
        if self.silent_activity_clicks > 0:
            self.silent_activity_clicks -= 1
            return  # 静默失效:无 toast、零网络请求、按钮不翻转(设计 2.7④)
        for a in self.activities:
            a["linked"] = a["name"] == name  # 互斥单选

    # ---- 选择器 → 元素 ----

    def select(self, sel):
        if sel == bnc._COLLECTION_BUTTON:
            label = self.collection or "选择合集"
            return [_El(label, on_click=self._open_popover)]
        if sel == bnc._COLLECTION_CHOSEN:
            if not self.collection:
                return []
            return [_El(self.collection, on_hover=self._hover_chip)]
        if sel == bnc._COLLECTION_CLOSE_ICON:
            # × 是 hover 态才渲染的(实拍):静态查不到,悬停后才出现
            if not self.collection or self.close_icon_absent:
                return []
            if self.close_icon_hover_gated and not self.chip_hovered:
                return []
            return [_El("", on_click=self._click_close_icon)]
        # 封面弹窗与 _ANY_MODAL 同是 .d-modal:开着时先认领它(被测代码按文案区分),
        # 没开则走原来的"点 × 之后弹出了我们没验证过的弹窗"那条语义。
        if sel == bnc._COVER_MODAL and self.cover_modal_open:
            return [self._cover_modal_el()]
        if sel == bnc._ANY_MODAL:
            return [_El(self.modal_text)] if self.modal_text else []
        if sel == bnc._COVER_SECTION:
            return [_El("设置封面 PK封面 优质封面示例")]
        if sel in bnc._COVER_THUMB_CANDIDATES:
            return [_El("")]
        if sel == bnc._COVER_OPERATOR_TEXT:
            if self.cover_entry_absent:
                return []
            return [_El("修改封面", on_click=self._open_cover_modal)]
        if sel == bnc._COLLECTION_POPOVER_ITEM:
            if not self.popover_open:
                return []
            items = [
                _El(name, on_click=(lambda n=name: self._choose_collection(n)))
                for _cid, name in self.collections
            ]
            # 弹层里还有一条「创建合集」:点到它会真的建一个新合集,断言里要求一次都没点
            items.append(_El("创建合集", on_click=self._boom_create))
            return items
        if sel == bnc._QUOTE_CONTAINER:
            return [_El(self.quote_text, on_click=self._open_modal)]
        if sel == bnc._QUOTE_NOTE_CARD:
            if not self.modal_open:
                return []
            if self.other_tab:
                # 他人笔记:检索前空,检索后按 note_id 命中才出卡
                hit = self.other_notes.get(self.other_query)
                return [_El(hit, rect=_CARD_RECT,
                            on_click=(lambda x=hit: self._select_quote(x)))] if hit else []
            # 只渲染**已放出**的那几页(懒加载:没滚就只有第一页)
            return [
                _El(f"{t} 封面", rect=_CARD_RECT,
                    on_click=(lambda x=t: self._select_quote(x)))
                for t in self.quote_card_titles[:self.quote_released]
            ]
        if sel == bnc._QUOTE_LINK_INPUT:
            if not (self.modal_open and self.other_tab):
                return []
            return [_El("", on_type=self._set_other_query)]
        if sel == bnc._QUOTE_MODAL:
            # 弹窗本体:_close_quote_modal 靠它判断"还开着吗"
            return [_El("选择笔记")] if self.modal_open else []
        if sel in (f"{bnc._QUOTE_MODAL} button", ".d-modal button", "button"):
            if not self.modal_open:
                return []
            # 真弹窗里「确认引用」旁边就是「取消」——收尾只能点它(Escape 关不掉)。
            # 「确认引用」的**禁用态照夹具实拍**:没选中任何卡时 disabled 属性 + class
            # 里的裸 token 双双在案(quote_modal.json)。这不是装饰 —— 2026-08-13 号 7
            # 三单失败正是点了这颗禁用按钮。
            selected = self._selected_quote is not None
            confirm_attrs = None if selected else {"disabled": ""}
            confirm_cls = (
                "d-button d-button-default d-button-with-content bg-red confirm-width"
                if selected else
                "d-button d-button-default disabled d-button-with-content "
                "--color-text-disabled bg-red disabled confirm-width"
            )
            return [
                _El("我的笔记", on_click=self._switch_mine),
                _El("他人笔记", on_click=self._switch_other),
                _El("确认引用", cls=confirm_cls, attrs=confirm_attrs,
                    rect=_FOOTER_RECT, on_click=self._confirm_quote),
                _El("取消", rect=_FOOTER_RECT, on_click=self._cancel_quote),
            ]
        if sel == bnc._ACTIVITY_CARD:
            cards = []
            for a in self.activities:
                text = bnc._ACTIVITY_LINKED_TEXT if a["linked"] else bnc._ACTIVITY_UNLINKED_TEXT
                cards.append(_El(a["name"], children={
                    bnc._ACTIVITY_NAME: [_El(a["name"])],
                    bnc._ACTIVITY_ACTION: [
                        _El(text, on_click=(lambda n=a["name"]: self._click_activity(n)))
                    ],
                }))
            return cards
        if sel == bnc._ORIGINAL_ROW:
            return [] if self.original_row_absent else [_El("原创声明")]
        if sel == bnc._ORIGINAL_SWITCH:
            if self.original_row_absent:
                return []
            return [_El("", on_click=self._click_original_switch)]
        if sel == bnc._ORIGINAL_MODAL:
            return [_El("笔记完成原创声明后")] if self.original_modal_open else []
        if sel == bnc._ORIGINAL_MODAL_CLOSE:
            if not self.original_modal_open:
                return []
            return [_El("", on_click=self._close_original_modal)]
        if sel in bnc._ORIGINAL_CONSENT_CANDIDATES:
            if not self.original_modal_open:
                return []
            return [_El("我已阅读并同意 《原创声明须知》",
                        on_click=self._tick_original_consent)]
        if sel == bnc._ORIGINAL_CONSENT_SIMULATOR:
            if not self.original_modal_open:
                return []
            # 勾选态看 class 有没有 unchecked(隐藏 input 是 0×0,读不到也点不着)
            cls = "d-checkbox-simulator --color-bg-white"
            if not self.original_consent_ticked:
                cls += " unchecked"
            return [_El("", cls=cls, on_click=self._tick_original_consent)]
        if sel == bnc._ORIGINAL_CONFIRM_BUTTON:
            if not self.original_modal_open:
                return []
            return [_El(bnc._ORIGINAL_CONFIRM_TEXT, on_click=self._confirm_original)]
        if sel == bnc._PERMISSION_DESC:
            return [_El(self.permission)] if self.permission else []
        if sel == bnc._TITLE_INPUT:
            return [_El(value=self.title)]
        return []

    @staticmethod
    def _boom_create():
        raise AssertionError("点到了「创建合集」——会凭空建一个新合集,绝对禁止")


class _FakePage:
    """假 page:选择器走 Editor,evaluate 按脚本特征分派,response 监听真回放。"""

    def __init__(self, editor):
        self.editor = editor
        self._listeners = []
        self.body_text = "内容设置 权限设置"

    # response 监听(与真 playwright 同接口)
    def on(self, event, fn):
        if event == "response":
            self._listeners.append(fn)

    def remove_listener(self, event, fn):
        if event == "response" and fn in self._listeners:
            self._listeners.remove(fn)

    def emit(self, url, body):
        for fn in list(self._listeners):
            fn(_FakeResponse(url, body))

    # 页面读写
    def inner_text(self, _sel):
        return self.body_text

    def query_selector(self, sel):
        hits = self.editor.select(sel)
        return hits[0] if hits else None

    def query_selector_all(self, sel):
        return self.editor.select(sel)

    def wait_for_timeout(self, _ms):
        pass

    def evaluate(self, js, _arg=None):
        if "noCover" in js:
            # 封面区当前态(改封面链的幂等判据 + 回读判据)
            return {
                "no_cover": self.editor.cover_no_cover,
                "fingerprint": self.editor.cover_fingerprint,
            }
        if "original-wrapper" in js:
            # 开关行没有时读不到 input → null(与真页面同语义,不是 False)
            if self.editor.original_row_absent:
                return None
            return self.editor.original_checked
        if "isDisabled" in js:
            # 「声明原创」按钮:勾了同意才解禁
            if not self.editor.original_modal_open:
                return None
            return self.editor.original_consent_ticked
        if "contenteditable" in js:
            return self.editor.body
        if "elementFromPoint" in js:
            return "XHS-PUBLISH-BTN"
        if "xhs-publish-btn" in js:
            return {"x": 100.0, "y": 500.0, "w": 120.0, "h": 40.0, "ih": 900.0}
        if "innerWidth" in js:
            return {"iw": 1920, "ih": 900, "dpr": 1}
        if "row-band-probe" in js:
            # 默认行就在中带(不触发滚动),流程测试因此真实走过防遮挡探测路径
            self.editor.row_band_probes.append(_arg)
            return {"cy": 450, "ih": 900}
        return None


class _FakeResponse:
    def __init__(self, url, body):
        self.url = url
        self._body = body

    def json(self):
        return self._body


@pytest.fixture
def wired(monkeypatch):
    """把浏览器层的外部依赖换成假的:导航触发页面加载、拟人层可观测、点发布走假提交。"""
    humans = []

    def fake_human(page):
        human = _Human(page)
        humans.append(human)
        return human

    monkeypatch.setattr(bnc, "SyncHumanActions", fake_human)
    # 轮询窗口压到刚够跑一两跳:逻辑不变,只缩时间(假 page 的 wait_for_timeout 是 no-op)
    for name, value in (
        ("_EDITOR_READY_TIMEOUT_S", 0.5),
        ("_POPOVER_TIMEOUT_S", 0.4),
        ("_MODAL_TIMEOUT_S", 0.4),
        ("_PAGE_SETTLE_S", 0.05),
        ("_QUOTE_SELECT_SETTLE_S", 0.1),
        ("_CATALOG_TIMEOUT_S", 0.4),
        ("_ACTIVITY_FLIP_TIMEOUT_S", 0.4),
        ("_SUBMIT_TIMEOUT_S", 0.4),
    ):
        monkeypatch.setattr(bnc, name, value)
    return humans


def _wire(monkeypatch, editor, humans, *, publish=True):
    """把 ``_goto_creator`` 接到编辑器的页面加载,把点发布接到编辑器的提交。"""
    monkeypatch.setattr(bnc, "_goto_creator", lambda _page, _url: editor.load())
    if publish:
        monkeypatch.setattr(
            bnc, "click_publish",
            lambda page, human: (humans[-1].clicks.append(("发布", "")), editor.submit()),
        )
    else:
        def boom(*_a, **_kw):
            raise AssertionError("本用例不应点发布")

        monkeypatch.setattr(bnc, "click_publish", boom)


def _run(editor, **components):
    return bnc.set_note_components(editor.page, 1, "n-target", **components)


# ---------------- 全部生效:done ----------------


def test_all_three_applied_is_done(monkeypatch, wired):
    """三项都设上且提交后回读确认 → done,并如实报出正文被追加的话题。"""
    editor = Editor()
    _wire(monkeypatch, editor, wired)
    # 关联活动会往正文追加话题(追加不覆盖,且话题名 ≠ 活动名)
    original_click = editor._click_activity

    def click_with_topic(name):
        original_click(name)
        if any(a["linked"] for a in editor.activities):
            editor.body = f"{editor.body} #心理学小课堂[话题]#"

    editor._click_activity = click_with_topic

    result = _run(
        editor, collection_id="c1", quoted_note_id="n-quote", activity_id="43561"
    )

    assert result["status"] == "done"
    assert result["applied"] == {"collection": True, "quote": True, "activity": True}
    assert result["failed"] == []
    assert result["submitted"] is True
    assert result["permission_preserved"] is True
    assert result["topics_injected"] == ["心理学小课堂"]
    assert result["body_appended"] == "#心理学小课堂[话题]#"
    assert "创建合集" not in wired[0].texts


# ---------------- 静默失败:合集被服务端丢弃 ----------------


def test_collection_silently_dropped_is_partially_applied(monkeypatch, wired):
    """合集在编辑器里选上了、提交也 success:true,但回读没绑上 → partially_applied。

    这是设计 2.6 的真号实测:私密笔记的 noteCollectionBind 被服务端**静默丢弃**,
    零 toast 零错误码。**绝不能**因为"没报错"就报 done。
    """
    editor = Editor(drop_collection_on_submit=True)
    _wire(monkeypatch, editor, wired)

    result = _run(editor, collection_id="c1", activity_id="43561")

    assert result["status"] == "partially_applied"
    assert result["applied"] == {"collection": False, "activity": True}
    assert [f["component"] for f in result["failed"]] == ["collection"]
    assert "error" not in result  # 还有一项成了,不是整体失败


# ---------------- 静默失败:活动按钮不翻转 ----------------


def test_activity_first_click_silent_then_retried_same_activity(monkeypatch, wired):
    """首次点击静默失效 → 重试**同一个活动**并成功;绝不点到别的活动。"""
    editor = Editor(
        activities=(
            ("43561", "身边的心理学", "心理科普"),
            ("999", "明日方舟创作应援", "游戏"),
        ),
        silent_activity_clicks=1,
    )
    _wire(monkeypatch, editor, wired)

    result = _run(editor, activity_id="43561")

    assert result["status"] == "done"
    assert result["components"]["activity"]["clicks"] == 2
    reasons = [r for r, _t in wired[0].clicks]
    assert all("明日方舟" not in r for r in reasons), "绝不换活动重试(旧话题不回收)"


def test_activity_never_flips_hits_cap_and_does_not_submit(monkeypatch, wired):
    """按钮始终不翻转 → 点击次数封顶、整体 failed 带 error,且**一次发布都不点**。

    编辑器里一项都没设上时提交毫无意义,而每次提交都是一次全量覆盖,故不点。
    """
    editor = Editor(silent_activity_clicks=99)
    _wire(monkeypatch, editor, wired, publish=False)

    result = _run(editor, activity_id="43561")

    assert result["status"] == "failed"
    assert "note_components_all_failed" in result["error"]
    assert result["submitted"] is False
    assert result["applied"] == {"activity": None}
    clicks = [r for r, _t in wired[0].clicks if "关联活动" in r]
    assert len(clicks) == bnc._ACTIVITY_CLICK_ATTEMPTS
    assert "activity_not_linked" in result["failed"][0]["reason"]


def test_already_linked_activity_never_clicks_cancel(monkeypatch, wired):
    """活动本来就关联着 → skipped,**绝不点「取消关联」**。"""
    editor = Editor(linked_activity="身边的心理学")
    _wire(monkeypatch, editor, wired)

    result = _run(editor, activity_id="43561")

    assert result["components"]["activity"]["status"] == "skipped"
    assert bnc._ACTIVITY_LINKED_TEXT not in wired[0].texts
    assert result["status"] == "done"  # 回读时它确实是关联态


def test_activity_action_text_unexpected_clicks_nothing(monkeypatch, wired):
    """按钮文案既不是「关联」也不是「取消关联」→ 状态未知,一次都不点。"""
    editor = Editor()
    _wire(monkeypatch, editor, wired, publish=False)
    monkeypatch.setattr(
        bnc, "read_activity_action_text", lambda _p, _n: "已结束"
    )

    result = _run(editor, activity_id="43561")

    assert "activity_action_unexpected" in result["failed"][0]["reason"]
    assert wired[0].clicks == []


# ---------------- 权限保全 ----------------


def test_permission_unreadable_aborts_before_touching_anything(monkeypatch, wired):
    """权限档位读不出 → 抛错中止,一个组件都不设、发布不点(提交是全量覆盖语义)。"""
    editor = Editor(permission=None)
    _wire(monkeypatch, editor, wired, publish=False)

    with pytest.raises(bnc.NoteComponentsError) as exc:
        _run(editor, collection_id="c1")

    assert exc.value.reason.startswith("permission_unreadable")
    assert wired[0].clicks == []


def test_permission_changed_before_submit_aborts_without_publishing(monkeypatch, wired):
    """设完组件、点发布前复读发现权限变了 → **不点发布**,抛错中止(笔记原样未动)。"""
    editor = Editor()
    _wire(monkeypatch, editor, wired, publish=False)
    real_read = bnc.read_permission_label
    calls = {"n": 0}

    def read_twice(page):
        calls["n"] += 1
        # 第 1 次是编辑前留底,第 2 次是点发布前的复读 —— 让它"变了"
        return "公开可见" if calls["n"] == 1 else "仅自己可见"

    monkeypatch.setattr(bnc, "read_permission_label", read_twice)
    try:
        with pytest.raises(bnc.NoteComponentsError) as exc:
            _run(editor, collection_id="c1")
    finally:
        monkeypatch.setattr(bnc, "read_permission_label", real_read)

    assert exc.value.reason.startswith("permission_changed_before_submit")
    assert editor.submitted == 0


def test_permission_changed_after_submit_alarms_and_restores(monkeypatch, wired):
    """提交后回读发现权限被改 → permission_preserved=False + 自动改回(走权限弹窗)。"""
    editor = Editor(permission_after_submit="仅自己可见")
    _wire(monkeypatch, editor, wired)
    restored = {}

    def fake_set_visibility(_page, account_id, note_id, title, target):
        restored.update(
            {"account_id": account_id, "note_id": note_id, "title": title, "target": target}
        )
        return {"status": "done", "permission_code": target}

    monkeypatch.setattr(bnv, "set_note_visibility", fake_set_visibility)

    result = _run(editor, collection_id="c1")

    assert result["permission_preserved"] is False
    assert result["permission_restored"]["ok"] is True
    # 改回的是**原档位**(公开可见 → 0),且按编辑器里读到的平台当前标题定位
    assert restored["target"] == 0
    assert restored["title"] == "目标笔记"
    assert restored["note_id"] == "n-target"


# ---------------- 引用笔记:按 note_id 定位 + 标题交叉校验 ----------------


def test_quote_locates_by_note_id_not_by_order_guess(monkeypatch, wired):
    """引用按 note_id 在候选响应里定位,并用平台标题交叉校验对应那张卡。"""
    editor = Editor(
        notes=(("n-a", "第一篇"), ("n-quote", "心理咨询师-徐瑞恒"), ("n-c", "第三篇"))
    )
    _wire(monkeypatch, editor, wired)

    result = _run(editor, quoted_note_id="n-quote")

    assert result["status"] == "done"
    assert result["components"]["quote"]["title"] == "心理咨询师-徐瑞恒"
    assert "心理咨询师-徐瑞恒" in editor.quote_text


def test_quote_card_title_mismatch_refuses_to_guess(monkeypatch, wired):
    """卡片顺序与接口不一致(标题对不上)→ 拒绝引用,**不点「确认引用」**。"""
    editor = Editor(
        notes=(("n-a", "第一篇"), ("n-quote", "心理咨询师-徐瑞恒")),
        quote_card_titles=["完全不同的甲", "完全不同的乙"],
    )
    _wire(monkeypatch, editor, wired, publish=False)

    result = _run(editor, quoted_note_id="n-quote")

    assert "quote_card_not_unique_by_title" in result["failed"][0]["reason"]
    assert "确认引用" not in wired[0].texts


def test_quote_note_not_in_candidates(monkeypatch, wired):
    """要引用的笔记不在候选列表里 → 明确报错,不退而求其次选别的。"""
    editor = Editor(notes=(("n-a", "第一篇"),))
    _wire(monkeypatch, editor, wired, publish=False)

    result = _run(editor, quoted_note_id="n-missing")

    assert "quoted_note_not_in_candidates" in result["failed"][0]["reason"]
    assert "确认引用" not in wired[0].texts


# ---------------- 候选列表懒加载:必须主动滚动翻页(2026-08-13 缺陷 A) ----------------
#
# 生产现场:号 6 有 49 篇笔记,弹窗候选却只有 12 篇 —— 因为**没有任何代码去滚这个列表**,
# 只被动等页面自己发的头一两页。排在第 37 位的目标必然被判「候选列表里没有」,
# 再被降级门当成"别人的笔记"送进他人 tab,而那条路按设计排除本账号笔记,检索必然返回空。
# 两路全死,运营看到的却是一句"笔记被删/私密/平台限制"的误导性报错。


def test_quote_scrolls_to_load_later_pages(monkeypatch, wired):
    """目标在**第 3 页**:必须滚出来才引得到(不滚的老实现在这里必红)。"""
    notes = tuple((f"n-{i}", f"第{i}篇") for i in range(12))
    editor = Editor(notes=notes, quote_pages=[5, 4, 3])
    _wire(monkeypatch, editor, wired)

    result = _run(editor, quoted_note_id="n-10")     # 第 11 位,只在第 3 页里

    assert result["status"] == "done"
    assert result["components"]["quote"]["title"] == "第10篇"
    assert "第10篇" in editor.quote_text
    assert wired[0].scrolls >= 2, "至少要滚两轮才拿得到第 3 页"


def test_quote_scroll_anchor_is_a_candidate_card(monkeypatch, wired):
    """滚轮落点必须**先 hover 到候选卡上**。

    ``mouse.wheel`` 打在鼠标当前位置:落点挑错就是滚了别的容器(本仓 2026-05 血案:
    滚轮打在侧栏,"翻两页就停")。所以这条锁的是"滚之前手放在列表里",
    而不只是"滚了几次"。
    """
    notes = tuple((f"n-{i}", f"第{i}篇") for i in range(9))
    editor = Editor(notes=notes, quote_pages=[4, 5])
    _wire(monkeypatch, editor, wired)

    _run(editor, quoted_note_id="n-8")

    card_hovers = [t for r, t in wired[0].hovers if "封面" in str(t)]
    assert card_hovers, f"滚动前没 hover 到候选卡上,实际 hover 记录:{wired[0].hovers}"


def test_quote_stops_scrolling_once_target_arrives(monkeypatch, wired):
    """目标一出现就收工:不为了"翻到底"白滚(每一轮都是真人时长)。"""
    notes = tuple((f"n-{i}", f"第{i}篇") for i in range(20))
    editor = Editor(notes=notes, quote_pages=[5, 5, 5, 5])
    _wire(monkeypatch, editor, wired)

    result = _run(editor, quoted_note_id="n-6")      # 第 2 页就有

    assert result["status"] == "done"
    assert wired[0].scrolls == 1, f"第 2 页就命中,只该滚 1 轮,实滚 {wired[0].scrolls}"


def test_quote_scroll_stops_when_list_bottoms_out(monkeypatch, wired):
    """翻到底(连续两轮没有新笔记也没有新卡)就停,不空转满封顶轮数。"""
    editor = Editor(notes=(("n-a", "第一篇"), ("n-b", "第二篇")), quote_pages=[2])
    _wire(monkeypatch, editor, wired, publish=False)

    result = _run(editor, quoted_note_id="n-missing")

    assert "quoted_note_not_in_candidates" in result["failed"][0]["reason"]
    assert wired[0].scrolls == bnc._QUOTE_SCROLL_IDLE_ROUNDS, (
        f"到底后该只再滚 {bnc._QUOTE_SCROLL_IDLE_ROUNDS} 轮确认,实滚 {wired[0].scrolls}"
    )


def test_post_scroll_settle_uses_a_short_window(monkeypatch):
    """滚完之后没有下一页 → 按**短窗**收工,不烧开弹窗那次的整个超时。

    这条钉的是成本:滚到底之后本就没有下一页,若沿用 ``_MODAL_TIMEOUT_S``(12 秒),
    两轮确认到底就是 24 秒纯等待,每一次引用都要白付。
    """
    monkeypatch.setattr(bnc, "_PAGE_SETTLE_S", 0.05)
    monkeypatch.setattr(bnc, "_QUOTE_SCROLL_WAIT_S", 0.3)
    monkeypatch.setattr(bnc, "_MODAL_TIMEOUT_S", 5.0)
    page = Editor().page
    responses = bnc.ComponentResponses()

    started = time.monotonic()
    got_page = bnc._settle_candidate_pages(page, responses, 0, require_first=False)
    elapsed = time.monotonic() - started

    assert got_page is False
    assert elapsed < 1.0, f"滚后静默窗烧了 {elapsed:.1f} 秒,说明用错了超时"


def test_quote_scroll_treats_repeated_page_as_no_progress(monkeypatch, wired):
    """平台把**同一页**重复发回来 → 算没进展,照样判到底。

    "有没有进展"必须看多出来的笔记,不能看"有没有新响应到达"——到底之后平台完全可能
    再回一次同样的内容,拿响应计数当进展会让循环永远用满封顶轮数,
    ``exhausted`` 于是永远为假,跨账号引用那条合法的降级路就被自己人堵死了。
    """
    editor = Editor(notes=(("n-a", "第一篇"),), quote_pages=[1])
    # 每次滚动都把**同一页**再发一遍(响应计数在涨,内容一条没多)
    editor.on_scroll = lambda: editor._emit_posted(0, 1) if editor.modal_open else None
    _wire(monkeypatch, editor, wired, publish=False)

    result = _run(editor, quoted_note_id="n-missing")

    assert "quoted_note_not_in_candidates" in result["failed"][0]["reason"]
    assert wired[0].scrolls == bnc._QUOTE_SCROLL_IDLE_ROUNDS


# ---------------- 降级到「他人笔记」的闸(2026-08-13 缺陷 A 第二层) ----------------


def test_own_note_never_degrades_to_other_tab(monkeypatch, wired):
    """台账说这篇是**本账号自己的** → 不在候选里也绝不走他人 tab(那是确定的死路)。"""
    editor = Editor(notes=(("n-a", "第一篇"),), other_notes=(("n-own", "本号的笔记 封面"),))
    _wire(monkeypatch, editor, wired, publish=False)

    result = _run(editor, quoted_note_id="n-own", quoted_note_is_own=True)

    reason = result["failed"][0]["reason"]
    assert "quoted_note_not_in_candidates_after_scroll" in reason
    assert "他人笔记" not in wired[0].texts, "本账号笔记不该切到他人 tab"
    assert "确认引用" not in wired[0].texts


def test_unknown_owner_still_degrades_to_other_tab(monkeypatch, wired):
    """台账里查不到这篇(``None``)→ 照旧降级他人 tab。

    跨账号引用接待员联系方式那篇是**真实业务**,不能因为台账没同步到就一并堵死。
    """
    editor = Editor(notes=(("n-a", "第一篇"),), other_notes=((_QR_NOTE, "小助手联系方式 封面"),))
    _wire(monkeypatch, editor, wired)

    result = _run(editor, quoted_note_id=_QR_NOTE, quoted_note_is_own=None)

    assert result["status"] == "done"
    assert result["components"]["quote"]["via"] == "other_notes_tab"


def test_known_other_owner_degrades_to_other_tab(monkeypatch, wired):
    """台账说这篇归**别的账号** → 正是他人 tab 该管的情形,照走。"""
    editor = Editor(notes=(("n-a", "第一篇"),), other_notes=((_QR_NOTE, "小助手联系方式 封面"),))
    _wire(monkeypatch, editor, wired)

    result = _run(editor, quoted_note_id=_QR_NOTE, quoted_note_is_own=False)

    assert result["status"] == "done"
    assert result["components"]["quote"]["via"] == "other_notes_tab"


# ---------------- 选中态回读 + 「确认引用」禁用闸(2026-08-13 缺陷 B) ----------------
#
# 生产现场:号 7 连续三单,候选里**找到了**目标卡、点了、也点了「确认引用」,回读却仍是
# 空态。而夹具证明:没选中任何卡时「确认引用」本来就是 disabled 的(disabled 属性 +
# class 里的裸 token 双双在案)—— 点一颗禁用按钮当然什么都不会发生。
# 原实现点完卡只 wait 一下就往下走,"选中没生效"一路裸奔到最后才以 quote_not_applied
# 的面目出现,把人往"确认按钮/引用区"上引,而真正坏掉的是**上一步**。


def test_quote_confirm_state_reads_fixture_disabled_form(monkeypatch, wired):
    """禁用判据认夹具实拍的两路:``disabled`` 属性 + class 里的**裸 token**。"""
    editor = Editor()
    _wire(monkeypatch, editor, wired, publish=False)
    editor._open_modal()

    assert bnc._quote_confirm_state(editor.page) == {
        "found": True, "enabled": False,
        "cls": "d-button d-button-default disabled d-button-with-content "
               "--color-text-disabled bg-red disabled confirm-width",
    }

    editor._selected_quote = "随便哪篇"
    assert bnc._quote_confirm_state(editor.page)["enabled"] is True


def test_quote_retries_once_when_selection_silently_fails(monkeypatch, wired):
    """首次点卡静默失效 → **关随机偏移**重点一次,成功。"""
    editor = Editor(
        notes=(("n-quote", "心理咨询师-徐瑞恒"),), quote_select_silent=1
    )
    _wire(monkeypatch, editor, wired)

    result = _run(editor, quoted_note_id="n-quote")

    assert result["status"] == "done"
    assert "心理咨询师-徐瑞恒" in editor.quote_text
    reasons = [r for r, _t in wired[0].clicks]
    assert any("重选被引用笔记" in r for r in reasons), f"没走重试:{reasons}"


def test_quote_select_not_applied_never_clicks_disabled_confirm(monkeypatch, wired):
    """两次都没选上 → 报 ``quote_card_select_not_applied``,且**一次都不点「确认引用」**。

    这正是号 7 三单的病灶:老实现会去点那颗禁用按钮,于是"点了"这个事实把排查引向下游。
    """
    editor = Editor(
        notes=(("n-quote", "心理咨询师-徐瑞恒"),), quote_select_silent=99
    )
    _wire(monkeypatch, editor, wired, publish=False)

    result = _run(editor, quoted_note_id="n-quote")

    reason = result["failed"][0]["reason"]
    assert "quote_card_select_not_applied" in reason
    assert "确认引用" not in wired[0].texts
    assert editor.modal_open is False, "失败也要关弹窗,否则盖住发布按钮"


def test_other_tab_also_reads_back_selection(monkeypatch, wired):
    """「他人笔记」那条路共用同一颗「确认引用」,选中态回读一视同仁。"""
    editor = Editor(
        notes=(("n-a", "第一篇"),),
        other_notes=((_QR_NOTE, "小助手联系方式 封面"),),
        quote_select_silent=99,
    )
    _wire(monkeypatch, editor, wired, publish=False)

    result = _run(editor, quoted_note_id=_QR_NOTE)

    assert "quote_card_select_not_applied" in result["failed"][0]["reason"]
    assert "确认引用" not in wired[0].texts


def test_quote_card_below_footer_is_scrolled_into_view_first(monkeypatch, wired):
    """目标卡落在弹窗页脚**之下** → 先滚进可视区再点(点在页脚下就是白点)。"""
    editor = Editor(notes=(("n-quote", "心理咨询师-徐瑞恒"),))
    _wire(monkeypatch, editor, wired)
    # 让候选卡的矩形落到页脚之下(其余几何不变)
    below = {**_FOOTER_RECT, "y": _FOOTER_RECT["y"] + 100.0}
    monkeypatch.setattr(_El, "bounding_box", lambda self: (
        below if "封面" in self.inner_text() else (self._rect or _CARD_RECT)
    ))

    result = _run(editor, quoted_note_id="n-quote")

    assert result["status"] == "done"
    assert wired[0].scrolls >= 1, "卡在页脚之下却一次都没滚"


# ---------------- 引用弹窗必须收尾(2026-08-02 发布连续超时事故) ----------------
#
# 事故经过:引用目标不在候选里 → 组件判 error 记「不阻断发布」→ **但弹窗留在页面上**。
# 弹窗是覆盖层,发布按钮点不到;而 step7 的全页兜底把弹窗里那个 disabled 的「确认引用」
# 当成发布按钮点了,什么都没发生,最后只报一句「发布超时(30秒),未检测到成功标志」。
# 好好生活号 job 132/133/134 连续三次全栽在这,运营换图片体积/换活动/压字数试了三轮,
# **没有一个变量与真正的原因有关**。
#
# 所以这组用例锁的是:**不管从哪条路径退出,弹窗都必须关掉**。


@pytest.mark.parametrize(
    "case,notes,card_titles,quoted_id",
    [
        # 目标不在候选里(正是线上那次)
        ("not_in_candidates", (("n-a", "第一篇"),), None, "n-missing"),
        # 卡片顺序与接口对不上,拒绝盲选
        ("title_mismatch", (("n-a", "第一篇"), ("n-quote", "徐瑞恒")),
         ["完全不同的甲", "完全不同的乙"], "n-quote"),
    ],
)
def test_quote_modal_closed_on_every_failure_path(
    monkeypatch, wired, case, notes, card_titles, quoted_id
):
    """引用失败后弹窗必须已关闭 —— 否则它会盖住发布按钮,让整篇笔记发不出去。"""
    editor = Editor(notes=notes, quote_card_titles=card_titles)
    _wire(monkeypatch, editor, wired, publish=False)

    result = _run(editor, quoted_note_id=quoted_id)

    assert result["failed"], f"{case} 本应判失败"
    assert editor.modal_open is False, f"{case}: 引用弹窗没关,发布按钮会被它盖住"
    assert "取消" in wired[0].texts, f"{case}: 没点「取消」收尾"


def test_quote_success_does_not_click_cancel(monkeypatch, wired):
    """成功路径上「确认引用」自己会关弹窗 —— 收尾是幂等的,**不能再点「取消」**。

    点了会怎样:取消掉刚设好的引用,组件白设。所以收尾必须先看弹窗还开不开着。
    """
    editor = Editor(notes=(("n-a", "第一篇"), ("n-quote", "心理咨询师-徐瑞恒")))
    _wire(monkeypatch, editor, wired)

    result = _run(editor, quoted_note_id="n-quote")

    assert result["status"] == "done"
    assert "心理咨询师-徐瑞恒" in editor.quote_text  # 引用还在,没被取消掉
    assert "取消" not in wired[0].texts
    assert editor.modal_open is False


# ---------------- 合集:id 映射 + 弹层条目 ----------------


def test_collection_id_not_in_catalog_refuses(monkeypatch, wired):
    """给的 collection_id 不在该号合集列表里 → 拒绝(绝不按文案瞎点一个)。"""
    editor = Editor()
    _wire(monkeypatch, editor, wired, publish=False)

    result = _run(editor, collection_id="不存在的id")

    assert "collection_not_found" in result["failed"][0]["reason"]
    assert wired[0].texts == ["选择合集"]  # 只点了入口,一个条目都没点


def test_collection_item_not_found_dumps_items_seen(monkeypatch, wired):
    """C-2 取证锁:catalog 有、DOM 条目匹配不上时,失败回执必须带**当场渲染出的条目清单**。

    RCA 2026-08-10 出轨贴:昨晚两跑 item_not_found、今晨复查条目又在(平台瞬态),但当时
    没记"看到过什么",黑箱了一晚。items_seen 让瞬态复发时直接抓现行。临时诊断字段,
    机制坐实/排除后按保质期纪律撤。
    """
    editor = Editor()
    _wire(monkeypatch, editor, wired, publish=False)
    monkeypatch.setattr(bnc, "_wait_collection_item", lambda _p, _n: None)

    result = _run(editor, collection_id="c1")

    f = result["failed"][0]
    assert "collection_item_not_found" in f["reason"]
    assert isinstance(f.get("items_seen"), list), "失败回执必须带 items_seen 取证"


# ---------------- 发布按钮:落点复核 ----------------


def test_click_publish_requires_element_from_point_match(monkeypatch):
    """质心落点上不是 ``XHS-PUBLISH-BTN`` → 抛错**不点**(那一带上方就是正文与组件区)。"""
    editor = Editor()
    page = editor.page
    human = _Human(page)
    monkeypatch.setattr(bnc, "_red_centroid", lambda _p, _r: (160.0, 520.0))
    monkeypatch.setattr(bnc, "_element_tag_at", lambda _p, _x, _y: "DIV")

    with pytest.raises(bnc.NoteComponentsError) as exc:
        bnc.click_publish(page, human)

    assert exc.value.reason.startswith("publish_point_mismatch")
    assert human.clicks == []


def test_click_publish_refuses_when_no_red_pixels(monkeypatch):
    """红像素找不到 → 抛错,**不按比例猜坐标**(组件设置会改变页面高度顶动按钮)。"""
    editor = Editor()
    human = _Human(editor.page)
    monkeypatch.setattr(bnc, "_red_centroid", lambda _p, _r: None)

    with pytest.raises(bnc.NoteComponentsError) as exc:
        bnc.click_publish(editor.page, human)

    assert exc.value.reason.startswith("publish_button_not_located")
    assert human.clicks == []


def test_click_publish_happy_path(monkeypatch):
    """质心复核通过 → 拟人点坐标(不是点元素句柄,closed shadow 里根本没有句柄)。"""
    editor = Editor()
    human = _Human(editor.page)
    monkeypatch.setattr(bnc, "_red_centroid", lambda _p, _r: (160.0, 520.0))

    bnc.click_publish(editor.page, human)

    assert len(human.clicks) == 1 and "发布" in human.clicks[0][0]


# ---------------- 纯函数:响应解析 / 活动筛选 / 正文差 ----------------


def test_parse_collections_reads_platform_keys():
    body = {"data": {"collection_info_list": [
        {"id": "6a69e9e316fb000000000001", "name": "咨询师简介", "desc": "全员北大", "note_num": 10},
        {"name": "没有 id 的丢弃"},
    ]}}
    assert bnc.parse_collections(body) == [{
        "id": "6a69e9e316fb000000000001", "name": "咨询师简介",
        "desc": "全员北大", "note_num": 10,
    }]
    assert bnc.parse_collections(None) == []


def test_parse_activities_coerces_id_to_string():
    """id 统一转字符串:提交载荷里的 bizId 实测是字符串,两边比对必须同型。"""
    got = bnc.parse_activities({"data": {"list": [
        {"id": 43561, "name": "身边的心理学", "desc": "心理科普"},
        {"name": "没有 id 的丢弃"},
    ]}})
    assert got == [{"id": "43561", "name": "身边的心理学", "desc": "心理科普"}]


def test_activity_filter_excludes_the_measured_false_positive():
    """默认词表不得命中「howto穿出自我」(实测的假阳性:它其实是穿搭活动)。

    这条正是"只查 name + 用过宽的词"会踩的坑,故默认词表里刻意没有「自我」「成长」。
    """
    activities = [
        {"id": "1", "name": "身边的心理学", "desc": "聊聊情绪与生活"},
        {"id": "2", "name": "howto穿出自我", "desc": "穿搭灵感大赏,穿出自我风格"},
        {"id": "3", "name": "夏日好物", "desc": "记录你的焦虑缓解好物"},  # 简介命中
    ]
    got = bnc.filter_activities(activities)
    assert [a["id"] for a in got] == ["1", "3"]
    assert got[0]["matched_keywords"] == ["心理", "情绪"]
    assert got[1]["matched_keywords"] == ["焦虑"]  # name 没提,靠**简介**联合匹配到


def test_activity_filter_empty_keywords_means_no_filter():
    activities = [{"id": "1", "name": "穿搭", "desc": ""}]
    assert len(bnc.filter_activities(activities, ())) == 1


def test_body_append_and_topic_extraction():
    """关联活动是把话题**追加**到正文末尾(设计 2.7①:追加不覆盖)。"""
    before = "#身边的心理学[话题]#"
    after = "#身边的心理学[话题]# #明日方舟[话题]#"
    assert bnc.appended_part(before, after) == "#明日方舟[话题]#"
    assert bnc.extract_topics(after) == ["身边的心理学", "明日方舟"]
    # 不是纯追加(正文被改写)时如实交出整段,不假装"只加了一点"
    assert bnc.appended_part("原文", "完全换了") == "完全换了"


# ---------------- 服务层契约 ----------------


@pytest.mark.parametrize(
    "payload, mark",
    [
        ({"collection_id": "c1"}, "缺 note_id"),
        ({"note_id": "n1"}, "no_component_requested"),
        ({"note_id": "n1", "collection_id": "", "activity_id": None}, "no_component_requested"),
    ],
)
async def test_execute_rejects_bad_payload_without_browser(monkeypatch, payload, mark):
    """入参不合法一律收敛成 {"error"},**不起浏览器**(取 cookie 一被调就失败)。"""

    async def boom(_account_id):
        raise AssertionError("入参不合法时不应取 cookie / 起浏览器")

    monkeypatch.setattr(svc, "load_account_cookies", boom)

    result = await svc.execute(1, payload)

    assert mark in result["error"]


async def test_execute_wraps_browser_error(monkeypatch):
    """浏览器层抛 NoteComponentsError → {"error": reason},不抛出(台账才不会悬挂)。"""

    async def fake_load(_account_id):
        return [{"name": "a", "value": "b"}]

    def boom(*_a, **_kw):
        raise bnc.NoteComponentsError("permission_unreadable: 读不到权限档位")

    monkeypatch.setattr(svc, "load_account_cookies", fake_load)
    monkeypatch.setattr(svc, "_apply_sync", boom)

    result = await svc.execute(1, {"note_id": "n1", "collection_id": "c1"})

    assert result == {"error": "permission_unreadable: 读不到权限档位"}


async def test_execute_returns_error_without_cookies(monkeypatch):
    async def fake_load(_account_id):
        return []

    monkeypatch.setattr(svc, "load_account_cookies", fake_load)

    result = await svc.execute(1, {"note_id": "n1", "activity_id": "1"})

    assert "error" in result


async def test_execute_passes_partial_result_through(monkeypatch):
    """部分生效的结果原样透出(不含 error 键 → 台账 done,result_status 才是真相)。"""

    async def fake_load(_account_id):
        return [{"name": "a", "value": "b"}]

    monkeypatch.setattr(svc, "load_account_cookies", fake_load)
    monkeypatch.setattr(
        svc, "_apply_sync",
        lambda *_a, **_kw: {"status": "partially_applied", "applied": {"collection": False}},
    )

    result = await svc.execute(1, {"note_id": "n1", "collection_id": "c1"})

    assert result["status"] == "partially_applied" and "error" not in result


async def test_read_catalog_requires_note_id(monkeypatch):
    """目录查询没有 note_id 就没有落脚点 → 直接给可读错误,不起浏览器。"""

    async def boom(_account_id):
        raise AssertionError("不应起浏览器")

    monkeypatch.setattr(svc, "load_account_cookies", boom)

    assert "error" in await svc.read_catalog(1, "")


# ---------------- 台账纪律 ----------------


def test_note_components_is_not_idempotent_kind():
    """僵死绝不自动重跑:提交是全量覆盖,且关联活动会把话题**再追加一遍**。"""
    assert "note_components" not in repo._IDEMPOTENT_KINDS


def test_account_worker_resolves_note_components_execute():
    from app import account_worker

    assert account_worker._resolve_execute("note_components") is not None


# ---------------- 既有链路改成"定位优先 note_id" ----------------


def test_visibility_prefers_platform_title_over_stale_ledger_title(monkeypatch):
    """可见性切换:按 note_id 现拉平台标题定位,台账里的过期标题只作兜底。

    实测:平台显示「粤语咨询师-黄安麟」而台账里是「心理咨询师-黄安麟」——按台账标题
    精确匹配必然 note_not_locatable。
    """
    monkeypatch.setattr(
        bnv, "fetch_posted_notes",
        lambda *_a, **_kw: [{"id": "n1", "display_title": "粤语咨询师-黄安麟"}],
    )

    got = bnv._resolve_locating_title(None, 1, "n1", "心理咨询师-黄安麟")

    assert got == "粤语咨询师-黄安麟"


def test_visibility_falls_back_to_caller_title(monkeypatch):
    """平台列表里没这条 note_id → 回退调用方给的标题(不直接判失败)。"""
    monkeypatch.setattr(
        bnv, "fetch_posted_notes", lambda *_a, **_kw: [{"id": "other", "display_title": "别的"}]
    )

    assert bnv._resolve_locating_title(None, 1, "n1", "兜底标题") == "兜底标题"


def test_visibility_without_id_or_title_is_not_locatable(monkeypatch):
    """平台查不到 note_id 且调用方也没给 title → note_not_locatable,绝不猜。"""
    monkeypatch.setattr(bnv, "fetch_posted_notes", lambda *_a, **_kw: [{"id": "other"}])

    with pytest.raises(bnv.NoteVisibilityError) as exc:
        bnv._resolve_locating_title(None, 1, "n1", "")

    assert exc.value.reason.startswith("note_not_locatable")


def test_comment_card_matching_prefers_note_id():
    """评论定位:主页卡片链接 href 里带 note_id,按 id 命中优先于标题。"""
    card = _El("完全不同的标题", children={
        "a": [_El(href="/user/profile/u1/6a4ce556?xsec_token=x")]
    })
    assert mi._card_matches_note_id(card, "6a4ce556")
    assert not mi._card_matches_note_id(card, "6a4ce557")
    assert not mi._card_matches_note_id(card, "")


# ---------------- 跨账号引用:走「他人笔记」tab ----------------
#
# 业务上真实存在:每个账号背后是不同的运营、各有 KPI,所以咨询师推介笔记**只能引用自己
# 账号的**;唯一的例外是"小助手联系方式"那篇——它是二维码有违规风险,集中放在主号,
# 于是别的号引用它必然是**跨账号**的。
#
# 2026-08-02 真号只读观察确定(不是猜的):切「他人笔记」是纯前端零请求;输入框写着
# 「请粘贴笔记链接 http://...」,但**直接填 note_id 就能检索到**(不需要 xsec_token,
# 那玩意儿是账号绑定且短效的,拼完整 URL 反而更脆);检索前候选区空,检索后才出卡。


_QR_NOTE = "68d50838000000000e00c3b6"   # 主号那篇小助手联系方式(**空标题**)


def test_falls_back_to_other_tab_when_not_own_note(monkeypatch, wired):
    """目标不在本账号笔记里 → 自动切「他人笔记」按 note_id 检索并引用成功。"""
    editor = Editor(
        notes=(("n-a", "第一篇"),),                      # 本账号候选里没有目标
        other_notes=((_QR_NOTE, "小助手联系方式 封面"),),  # 他人笔记里有
    )
    _wire(monkeypatch, editor, wired)

    result = _run(editor, quoted_note_id=_QR_NOTE)

    assert result["status"] == "done"
    quote = result["components"]["quote"]
    assert quote["status"] == "done" and quote["via"] == "other_notes_tab"
    assert "小助手联系方式" in editor.quote_text
    assert "他人笔记" in wired[0].texts        # 确实切过 tab
    assert _QR_NOTE in wired[0].typed          # 确实按 note_id 检索


def test_other_tab_refuses_when_search_misses(monkeypatch, wired):
    """检索不到那篇 → 报错,**绝不点第一张凑数**。

    判据走**接口返回的 note_id**,不是"页面上有几张卡":逐字输入时页面会对中间态
    (note_link=68d508 这种半截 id)也发一次检索,只看"有没有结果"会认错。
    """
    editor = Editor(notes=(("n-a", "第一篇"),), other_notes=())
    _wire(monkeypatch, editor, wired, publish=False)

    result = _run(editor, quoted_note_id=_QR_NOTE)

    reason = result["failed"][0]["reason"]
    assert "quote_other_id_mismatch" in reason
    # 两条路的原因都要带上,否则排查时以为"本账号里也没有"这件事没发生过
    assert "quoted_note_not_in_candidates" in reason
    assert "确认引用" not in wired[0].texts


def test_no_fallback_on_uncertain_failures(monkeypatch, wired):
    """卡片顺序与接口对不上属于"状态不确定" → **不降级**,不拿不确定去赌另一条路。"""
    editor = Editor(
        notes=(("n-a", "第一篇"), ("n-quote", "徐瑞恒")),
        quote_card_titles=["完全不同的甲", "完全不同的乙"],
        other_notes=(("n-quote", "不该被用到"),),
    )
    _wire(monkeypatch, editor, wired, publish=False)

    result = _run(editor, quoted_note_id="n-quote")

    assert "quote_card_not_unique_by_title" in result["failed"][0]["reason"]
    assert "他人笔记" not in wired[0].texts   # 一次都没切过去


def test_other_tab_still_closes_modal_on_failure(monkeypatch, wired):
    """走他人笔记失败时,弹窗同样必须关掉 —— 否则又会盖住发布按钮(见上面那组用例)。"""
    editor = Editor(notes=(("n-a", "第一篇"),), other_notes=())
    _wire(monkeypatch, editor, wired, publish=False)

    _run(editor, quoted_note_id=_QR_NOTE)

    assert editor.modal_open is False
    assert "取消" in wired[0].texts


def test_other_tab_rejects_wrong_note_from_search(monkeypatch, wired):
    """检索接口返回的是**另一篇**的 note_id → 拒绝引用。

    这是逐字输入的真实副作用:输到一半时页面就会拿半截 id 去查,可能查出别的笔记。
    只要最终响应的 note_id 与目标不符,就一篇都不引用。
    """
    editor = Editor(notes=(("n-a", "第一篇"),), other_notes=(("别的笔记id", "别的笔记 封面"),))
    _wire(monkeypatch, editor, wired, publish=False)

    result = _run(editor, quoted_note_id=_QR_NOTE)

    assert "quote_other_id_mismatch" in result["failed"][0]["reason"]
    assert "确认引用" not in wired[0].texts


def test_quote_matches_card_by_title_not_by_index(monkeypatch, wired):
    """**卡片顺序与接口不一致时仍要找对那张卡** —— 这是引用功能整体不可用的真因。

    2026-08-03 真号证伪:原实现假设"响应第 i 条 ↔ 弹窗第 i 张卡"(当初就写明"没有实测
    背书")。实测第 6 张卡是「心理咨询师-彭旱雨…」,而接口第 6 条是「粤语咨询师-黄安麟…」
    —— 同序不成立,于是每次都判 mismatch,引用功能整体不可用。

    这里把卡片顺序**故意打乱**成与接口不同,断言仍然选中标题对得上的那一张。
    """
    editor = Editor(
        notes=(("n-a", "第一篇"), ("n-quote", "徐瑞恒"), ("n-c", "第三篇")),
        # 接口顺序是 第一篇/徐瑞恒/第三篇,弹窗渲染顺序完全不同(真号就是这样)
        quote_card_titles=["第三篇", "第一篇", "徐瑞恒"],
    )
    _wire(monkeypatch, editor, wired)

    result = _run(editor, quoted_note_id="n-quote")

    assert result["status"] == "done"
    assert result["components"]["quote"]["title"] == "徐瑞恒"
    assert "徐瑞恒" in editor.quote_text      # 引用的确实是它,不是下标位置上那张


# ---------------- 空标题笔记的引用(主号那篇二维码) ----------------
#
# 业务上非做不可:主号「小助手联系方式」那篇是**空标题**的,而规则要求每篇咨询师推介
# 笔记都引用它 —— 单主号自己就有 20 篇推介笔记落在这条路上。
#
# 空标题没法按文案认卡,改用"扣除未渲染项之后的位置"。这个算法的依据是真号夹具实测:
# 弹窗排除掉当前正在编辑的那篇后,接口顺序与卡片顺序**严格对齐 20/20**
# (见 test_quote_modal_replay.py)。


def test_untitled_note_picked_by_position_after_exclusion(monkeypatch, wired):
    """空标题目标:扣掉"标题非空却没渲染"的那条后,按位置取到正确的卡。"""
    editor = Editor(
        notes=(("n-a", "第一篇"), ("n-skip", "被排除的那篇"), ("n-qr", ""), ("n-c", "第三篇")),
        # 弹窗少渲染了「被排除的那篇」(= 当前正在编辑的那篇),空标题卡文案只有作者与赞数
        quote_card_titles=["第一篇", "作者 5", "第三篇"],
    )
    _wire(monkeypatch, editor, wired)

    result = _run(editor, quoted_note_id="n-qr")

    assert result["status"] == "done"
    # 接口里 n-qr 在第 3 位(下标 2),前面有 1 条未渲染 → 卡片第 2 位(下标 1)
    assert "作者 5" in editor.quote_text


def test_untitled_refuses_when_exclusions_cannot_be_accounted_for(monkeypatch, wired):
    """数量差对不上认出的未渲染项 → 拒绝(引用错一篇比不引用糟得多)。

    构造:接口 4 条、卡片只有 2 张(差 2),但只能认出 1 条"标题非空却没渲染"的 ——
    另一条差额来源不明(可能是另一篇空标题笔记没渲染),位置就算不准了。
    """
    editor = Editor(
        notes=(("n-a", "第一篇"), ("n-skip", "被排除的那篇"), ("n-qr", ""), ("n-x", "")),
        quote_card_titles=["第一篇", "作者 5"],
    )
    _wire(monkeypatch, editor, wired, publish=False)

    result = _run(editor, quoted_note_id="n-qr")

    assert "quote_untitled_position_unverifiable" in result["failed"][0]["reason"]
    assert "确认引用" not in wired[0].texts


def test_untitled_verifies_by_empty_state(monkeypatch, wired):
    """空标题没有可比对的文案,复核看引用区**是不是还停在空态**;是就判失败。

    (不看"变了没有":重复设同一篇时前后一样,拿变化当判据会把幂等重跑判成失败。)
    """
    editor = Editor(
        notes=(("n-qr", ""),),
        quote_card_titles=["作者 5"],
    )
    # 让"确认引用"不生效:引用区保持原样
    editor._confirm_quote = lambda: setattr(editor, "modal_open", False)
    _wire(monkeypatch, editor, wired, publish=False)

    result = _run(editor, quoted_note_id="n-qr")

    assert "quote_not_applied" in result["failed"][0]["reason"]
    assert "未设置态" in result["failed"][0]["reason"]


def test_verify_after_submit_accepts_platform_rendering(monkeypatch, wired):
    """提交后回读的文案是「引用 @作者 的笔记」——**不含标题**,不能拿标题去比对。

    2026-08-03 真号实测的假阴性:编辑器里引用区显示被引用笔记的**标题**,提交后重进
    页面却显示「引用 @NBDpsy-好好生活 的笔记」。原判据 `title in quoted` 于是必然失败,
    功能明明成了(in-editor done、submitted=true、权限未动)却报「回读未生效」。

    改成基线对比后,这种平台渲染必须被认成生效。
    """
    editor = Editor(notes=(("n-a", "第一篇"), ("n-quote", "徐瑞恒")))
    _wire(monkeypatch, editor, wired)
    # 模拟平台行为:提交后引用区变成不含标题的「引用 @作者 的笔记」
    real_submit = editor.submit

    def submit_then_render():
        real_submit()
        editor.quote_text = "引用 @NBDpsy-好好生活 的笔记"

    editor.submit = submit_then_render

    result = _run(editor, quoted_note_id="n-quote")

    assert result["status"] == "done", f"平台渲染形态被误判成失败: {result.get('failed')}"
    assert result["applied"]["quote"] is True


def test_verify_after_submit_survives_idempotent_reapply(monkeypatch, wired):
    """给**已经带引用**的笔记再设一次同样的引用 → 仍判生效(幂等重跑不该报失败)。

    这是"基线对比"顶不住的边界:重复设同一篇时提交前后文案一模一样,拿变化当判据会把
    正确状态判成失败。所以回读判据改成认**空态文案**——问的是"现在有没有引用",
    而不是"跟之前比变了没有"。
    """
    editor = Editor(notes=(("n-a", "第一篇"), ("n-quote", "徐瑞恒")))
    editor.quote_text = "引用 @NBDpsy-好好生活 的笔记"   # 这篇本来就已经带着引用
    _wire(monkeypatch, editor, wired)
    real_submit = editor.submit

    def submit_then_render():
        real_submit()
        editor.quote_text = "引用 @NBDpsy-好好生活 的笔记"   # 前后完全一样

    editor.submit = submit_then_render

    result = _run(editor, quoted_note_id="n-quote")

    assert result["status"] == "done", f"幂等重跑被判失败: {result.get('failed')}"
    assert result["applied"]["quote"] is True


def test_verify_after_submit_still_catches_real_failure(monkeypatch, wired):
    """真没生效时仍要判失败 —— 放宽判据不能把"静默丢弃"也放过去。

    这条产品线的失败是静默的(私密笔记的合集绑定就会被服务端丢掉),所以"回读没变化"
    必须继续判失败,否则整套回读复核就形同虚设。
    """
    editor = Editor(notes=(("n-a", "第一篇"), ("n-quote", "徐瑞恒")))
    _wire(monkeypatch, editor, wired)
    real_submit = editor.submit

    def submit_then_revert():
        real_submit()
        editor.quote_text = "引用笔记"      # 服务端静默丢弃:回到未设置态(空态文案)

    editor.submit = submit_then_revert

    result = _run(editor, quoted_note_id="n-quote")

    assert result["status"] != "done"
    assert result["applied"]["quote"] is False


# ---------------- 合集已选态识别(2026-08-04 P1-1 翻案) ----------------
#
# 运营建合集时把笔记选了进去 → 页面显示已选展示条,「加入合集」按钮本来就不渲染。
# 旧代码报 entry_not_found,把「已是目标态」误报成失败,还被归因成"账号玄学"
# (两号复现两号正常——其实是笔记在不在合集里的差别)。


def test_collection_already_chosen_same_target_is_skipped(monkeypatch, wired):
    """已在目标合集里 → skipped(与活动「已关联绝不点」同一纪律),一次点击都不发生。"""
    editor = Editor(collection="咨询师简介")
    _wire(monkeypatch, editor, wired)

    result = _run(editor, collection_id="c-1", collection_name="咨询师简介")

    assert result["status"] == "done"
    assert result["applied"]["collection"] is True   # skipped=平台已是目标态,回读也过
    assert "打开合集弹层" not in [r for r, _t in wired[0].clicks]


def test_collection_chosen_different_target_refuses(monkeypatch, wired):
    """已在**别的**合集里 → 明确报错,绝不自动换(换=先移出,移除是红线)。"""
    editor = Editor(collection="另一个合集")
    _wire(monkeypatch, editor, wired, publish=False)

    result = _run(editor, collection_id="c-1", collection_name="咨询师简介")

    reason = result["failed"][0]["reason"]
    assert "collection_already_in_another" in reason
    assert "另一个合集" in reason
    assert "打开合集弹层" not in [r for r, _t in wired[0].clicks]


def test_other_tab_empty_search_reason_is_actionable(monkeypatch, wired):
    """检索返回空 id(got_id='')时,原因码升级为可操作提示(2026-08-04 P1-2 缺陷 b)。

    b90dfb4f 实录:跨账号引接待员笔记,检索接口返回 note_id='',运营只看到
    "返回的是 note_id=''"没法行动。空 id 与"拿错 id"是两种处境:前者要复现取证
    (响应竞态/笔记不可检索),后者是消费错了响应——报错必须区分并给出下一步。
    """
    editor = Editor(notes=(("n-a", "第一篇"),), other_notes=())
    _wire(monkeypatch, editor, wired, publish=False)

    result = _run(editor, quoted_note_id=_QR_NOTE)

    reason = result["failed"][0]["reason"]
    assert "quote_other_id_mismatch" in reason          # 前缀不变,运营侧按前缀匹配
    assert "检索返回空" in reason                        # 空 id 单独定性
    assert "job_id" in reason                            # 指路:带 job_id 复现取证
    assert "显式传本账号" in reason                      # 指路:短期规避


# ---------------- 设置行防遮挡滚动(底部发布钮 closed-shadow 吞点击的根因锁)----------------


class _BandPage:
    """只回答 row-band-probe 的假页面:按预置序列给出行中心位置。"""

    def __init__(self, seq):
        self.seq = list(seq)

    def evaluate(self, js, _arg=None):
        assert "row-band-probe" in js
        return self.seq.pop(0) if self.seq else None


class _BandHuman:
    def __init__(self):
        self.scrolls = []
        self.hovers = []

    def scroll(self, direction="down", distance=None):
        self.scrolls.append(direction)

    def hover(self, target, *, reason=""):
        self.hovers.append(target)

    def wait(self, *_a, **_kw):
        pass


def test_row_in_bottom_band_scrolls_down_until_mid():
    """行在底带(0.94)→ 滚轮向下一次进中带即停。

    根因(quote_probe 夹具 2026-08-05):底部发布钮 XHS-PUBLISH-BTN 透明命中区盖住底带,
    行在带内点了被吞——标本 6a707e9f 七连零反应、quote_candidates_unavailable 11 例。
    """
    page = _BandPage([{"cx": 942, "cy": 1190, "ih": 1266}, {"cx": 942, "cy": 633, "ih": 1266}])
    human = _BandHuman()
    bnc._scroll_row_to_mid_viewport(page, human, ".quote-note-container")
    assert human.scrolls == ["down"]
    # 滚前必须先把鼠标移进内容列(mouse.wheel 打在鼠标位置,创作中心滚的是内层容器):
    # hover 点 = 行 x 中线、视口 45% 高
    assert human.hovers == [(942, 1266 * 0.45)]


def test_row_in_top_band_scrolls_up():
    """行在顶带 → 向上滚(只会向下的写法遇到被顶到上方的 E8 情形会越滚越远)。"""
    page = _BandPage([{"cx": 942, "cy": 100, "ih": 1266}, {"cx": 942, "cy": 500, "ih": 1266}])
    human = _BandHuman()
    bnc._scroll_row_to_mid_viewport(page, human, "x")
    assert human.scrolls == ["up"]


def test_row_already_mid_band_zero_scroll():
    """行已在中带 → 一次滚动都不发生。"""
    page = _BandPage([{"cx": 942, "cy": 633, "ih": 1266}])
    human = _BandHuman()
    bnc._scroll_row_to_mid_viewport(page, human, "x")
    assert human.scrolls == []


def test_row_never_reaches_band_gives_up_bounded():
    """滚满上限仍不在带内 → 告警放行(内容短滚不动的页面不误杀),滚动次数有界。"""
    page = _BandPage([{"cx": 942, "cy": 1200, "ih": 1266}] * 10)
    human = _BandHuman()
    bnc._scroll_row_to_mid_viewport(page, human, "x")
    assert human.scrolls == ["down"] * bnc._ROW_BAND_TRIES


def test_quote_flow_probes_band_before_modal_click(monkeypatch, wired):
    """引用流程在点入口前必须做防遮挡探测(接线锁,防止日后有人把这步删了)。"""
    editor = Editor(notes=(("n-quote", "要引用的那篇"),))
    _wire(monkeypatch, editor, wired)

    result = _run(editor, quoted_note_id="n-quote")

    assert result["status"] == "done"
    assert bnc._QUOTE_CONTAINER in editor.row_band_probes


def test_collection_flow_probes_band_before_click(monkeypatch, wired):
    """合集流程点入口前同样必须做防遮挡探测(接线锁)。"""
    editor = Editor()
    _wire(monkeypatch, editor, wired)

    result = _run(editor, collection_id="c1")

    assert result["status"] == "done"
    assert bnc._COLLECTION_BUTTON in editor.row_band_probes


# ---------------- 活动按钮文案分类(内联区 / 更多面板两种上下文) ----------------


def test_activity_action_text_classified_across_both_contexts():
    """内联区文案是「关联」,更多面板里是「关联活动」—— 两种都得认成"未关联"。

    上线前是裸 ``!= "关联"`` 的相等判断,面板里那颗按钮会被判成"文案异常,拒绝点击",
    于是永远关联不上。
    """
    assert bnc.classify_activity_action("关联") == "unlinked"
    assert bnc.classify_activity_action("关联活动") == "unlinked"


def test_activity_action_cancel_wins_over_containment():
    """「取消关联活动」含有「关联活动」——必须先判「取消」,否则会把已关联误读成未关联。

    误读的后果不是白跑一趟,是**点掉「取消关联」**(本模块最硬的一条红线)。
    """
    assert bnc.classify_activity_action("取消关联") == "linked"
    assert bnc.classify_activity_action("取消关联活动") == "linked"


def test_activity_action_unknown_is_not_clickable():
    """读不出 / 陌生文案 → unknown(调用方据此一次都不点)。"""
    for text in (None, "", "   ", "查看详情", "已结束"):
        assert bnc.classify_activity_action(text) == "unknown"


# ---------------- 活动卡缺失的归因:容器 + 文本双判据 ----------------


def _obs(cards=0, container=False, section_text=False, scrolls=6,
         more_entry=False, panel_opened=False):
    return {"cards": cards, "container": container, "section_text": section_text,
            "scrolls": scrolls, "more_entry": more_entry, "panel_opened": panel_opened}


def test_section_absent_only_when_container_and_text_both_missing():
    """判「区不存在」必须容器选择器与区标题文案**都**读不到 —— 单靠文案会误报。

    单判据的坑:图文页与视频页的区标题文案若不同(或平台改文案),纯文本判据会把
    存在的活动区判成"不存在",运营据此以为平台没这功能。所以双判据都空才算 absent。
    """
    assert bnc.explain_activity_card_missing("身边的心理学", _obs()).startswith(
        "activity_section_absent:")
    # 容器在 → 区在,不是 absent
    assert not bnc.explain_activity_card_missing(
        "身边的心理学", _obs(container=True)).startswith("activity_section_absent:")
    # 只有文案在 → 区也在,不是 absent
    assert not bnc.explain_activity_card_missing(
        "身边的心理学", _obs(section_text=True)).startswith("activity_section_absent:")
    # 有卡 → 区显然在
    assert not bnc.explain_activity_card_missing(
        "身边的心理学", _obs(cards=2)).startswith("activity_section_absent:")


def test_card_not_found_reason_says_whether_more_panel_was_tried():
    """区在但没找到目标卡:必须说清「更多」面板试过没有 —— 决定运营下一步查什么。"""
    tried = bnc.explain_activity_card_missing(
        "身边的心理学", _obs(cards=2, container=True, more_entry=True, panel_opened=True))
    not_tried = bnc.explain_activity_card_missing(
        "身边的心理学", _obs(cards=2, container=True, more_entry=False))
    assert tried.startswith("activity_card_not_found:")
    assert not_tried.startswith("activity_card_not_found:")
    assert "更多" in tried and "更多" in not_tried
    assert tried != not_tried


def test_missing_reason_always_carries_name_and_observations():
    """两种归因都带活动名与观测量(取证:证明结论不是"没滚够"或"没试面板"滚出来的)。"""
    for observed in (_obs(), _obs(cards=3, container=True, panel_opened=True)):
        reason = bnc.explain_activity_card_missing("身边的心理学", observed)
        assert "身边的心理学" in reason
        assert "6" in reason  # scrolls


# ---------------- 「更多」入口:必须收口在活动区内 ----------------


def test_more_entry_probe_is_scoped_to_activity_section(monkeypatch):
    """定位「更多」只在活动区容器内找 —— 页面上推荐话题区也有个「更多」。

    这条锁的是同名陷阱本身:探测函数拿到的候选**必须**来自活动区选择器,
    绝不能是一次全页 text=更多 的匹配(点错了会展开推荐话题面板)。
    """
    seen = []

    class _P:
        def query_selector(self, sel):
            seen.append(sel)
            return "MORE" if sel == bnc._ACTIVITY_MORE_ENTRY else None

        def evaluate(self, *_a, **_k):
            return None

    got = bnc._find_activity_more_entry(_P())
    assert got is not None
    assert all("activity" in sel for sel in seen), \
        f"「更多」的候选选择器必须全部收口在活动区内,实际尝试了: {seen}"


# ---------------- 更多面板路径:内联未命中 → 点更多 → 面板命中 → 翻转 ----------------


class _RecHuman:
    """记录每一次拟人动作;点到活动按钮时把文案翻成已关联(模拟平台响应)。"""

    def __init__(self, state):
        self.state = state
        self.clicks = []

    def click(self, target, reason="", **_k):
        self.clicks.append(reason)
        if target == "MORE":
            self.state["panel_open"] = True
        elif target is self.state["action"]:
            self.state["text"] = "取消关联活动"

    def hover(self, *_a, **_k):
        self.state["hovered"] = True

    def scroll(self, *_a, **_k):
        self.state["scrolls"] += 1

    def wait(self, *_a, **_k):
        return None


class _FakeAction:
    def __init__(self, state):
        self.state = state

    def inner_text(self):
        return self.state["text"]


class _FakeCard:
    def __init__(self, action):
        self._action = action

    def query_selector(self, _sel):
        return self._action


class _PanelPage:
    """最小 page 替身:只提供 _set_activity / _wait_activity_flip 会碰到的读方法。"""

    def wait_for_timeout(self, _ms):
        return None

    def query_selector(self, _sel):
        return None

    def query_selector_all(self, _sel):
        # 面板开着时列表里当然有卡(只是不一定有目标那张)——滚动锚点要靠它
        return ["PANEL_CARD"]

    def inner_text(self, _sel):
        return ""

    def evaluate(self, *_a, **_k):
        return None


def _wire_more_panel(monkeypatch, *, more_entry_found=True):
    """内联区永远没有目标卡,只有点开「更多」面板后才找得到。"""
    state = {"panel_open": False, "text": "关联活动", "scrolls": 0, "hovered": False}
    state["action"] = _FakeAction(state)
    card = _FakeCard(state["action"])

    monkeypatch.setattr(
        bnc, "parse_activities",
        lambda raw: [{"id": "43561", "name": "身边的心理学", "desc": "心理科普"}],
    )
    monkeypatch.setattr(
        bnc, "read_activity_action_text",
        lambda p, n: state["text"] if state["panel_open"] else None,
    )
    monkeypatch.setattr(
        bnc, "_find_activity_card",
        lambda p, n: card if state["panel_open"] else None,
    )
    monkeypatch.setattr(
        bnc, "_find_activity_more_entry",
        lambda p: "MORE" if more_entry_found else None,
    )
    monkeypatch.setattr(bnc, "probe_activity_section",
                        lambda p: {"cards": 2, "container": True, "section_text": True})
    monkeypatch.setattr(bnc, "_ACTIVITY_REVEAL_SCROLLS", 2)
    monkeypatch.setattr(bnc, "_ACTIVITY_PANEL_SCROLLS", 3)
    return state, _RecHuman(state)


def test_more_panel_path_links_activity_not_in_recommended_slots(monkeypatch):
    """内联区只有约 2 张推荐卡,目标活动不在推荐位 → 点「更多」进面板再关联。

    真实调用序列断言(不是"没抛异常"):先点「更多」→ 面板打开 → 再点该活动的按钮 →
    文案翻转 → done。上线前这条路根本不存在,目标不在推荐位就永远 card_not_found ——
    大概率就是此前「活动挂不上」的真根因。
    """
    state, human = _wire_more_panel(monkeypatch)

    out = bnc._set_activity(_PanelPage(), human, _StubResponses(), "43561")

    assert out["status"] == "done", out
    assert out["via"] == "more_panel"
    assert out["name"] == "身边的心理学"
    # 序列:先「更多」后活动按钮,顺序不许颠倒(面板没开时那颗按钮压根不在 DOM 里)
    more_at = next(i for i, r in enumerate(human.clicks) if r.startswith("打开"))
    act_at = next(i for i, r in enumerate(human.clicks) if r.startswith("关联活动「"))
    assert more_at < act_at, human.clicks
    assert state["panel_open"] is True


def test_more_panel_scrolls_the_panel_before_giving_up(monkeypatch):
    """面板列表是懒加载的:找不到卡要在面板内滚动再找,而不是开完就判没有。"""
    state = {"panel_open": False, "text": "关联活动", "scrolls": 0, "hovered": False}
    state["action"] = _FakeAction(state)
    card = _FakeCard(state["action"])
    monkeypatch.setattr(
        bnc, "parse_activities",
        lambda raw: [{"id": "43561", "name": "身边的心理学", "desc": ""}],
    )
    # 面板开了也要滚够 2 轮才渲染出目标卡
    def _read(p, n):
        return state["text"] if state["panel_open"] and state["scrolls"] >= 2 else None

    monkeypatch.setattr(bnc, "read_activity_action_text", _read)
    monkeypatch.setattr(
        bnc, "_find_activity_card",
        lambda p, n: card if state["panel_open"] and state["scrolls"] >= 2 else None,
    )
    monkeypatch.setattr(bnc, "_find_activity_more_entry", lambda p: "MORE")
    monkeypatch.setattr(bnc, "probe_activity_section",
                        lambda p: {"cards": 2, "container": True, "section_text": True})
    monkeypatch.setattr(bnc, "_ACTIVITY_REVEAL_SCROLLS", 0)
    monkeypatch.setattr(bnc, "_ACTIVITY_PANEL_SCROLLS", 5)
    human = _RecHuman(state)

    out = bnc._set_activity(_PanelPage(), human, _StubResponses(), "43561")

    assert out["status"] == "done", out
    assert state["scrolls"] >= 2
    assert state["hovered"] is True, "滚面板前必须先把鼠标移进面板(wheel 打在光标位置)"


def test_no_more_entry_falls_back_to_attribution_and_clicks_nothing(monkeypatch):
    """连「更多」入口都没有 → 走归因报错,且**一次点击都不发生**(绝不乱点)。"""
    state, human = _wire_more_panel(monkeypatch, more_entry_found=False)

    out = bnc._set_activity(_PanelPage(), human, _StubResponses(), "43561")

    assert out["status"] == "error"
    assert out["reason"].startswith("activity_card_not_found:")
    assert human.clicks == [], f"没找到入口就不该点任何东西,实际点了: {human.clicks}"


def test_more_panel_never_clicks_cancel_when_already_linked(monkeypatch):
    """面板里那张卡本来就是「取消关联活动」→ skipped 零点击(红线:绝不点取消)。"""
    state, human = _wire_more_panel(monkeypatch)
    state["text"] = "取消关联活动"

    out = bnc._set_activity(_PanelPage(), human, _StubResponses(), "43561")

    assert out["status"] == "skipped"
    # 开面板是允许的(不开就看不到这张卡);红线是绝不点那颗「取消关联活动」按钮
    assert all(not r.startswith("关联活动「") for r in human.clicks), human.clicks


class _NoopHuman:
    """只满足 _set_activity 会用到的动作(下滚触发懒渲染 + 悬停 + 停顿)。"""

    def click(self, *_a, **_k):
        raise AssertionError("本用例不应发生任何点击")

    def hover(self, *_a, **_k):
        return None

    def scroll(self, *_a, **_k):
        return None

    def wait(self, *_a, **_k):
        return None


class _StubResponses:
    """活动列表响应的最小替身(内容不重要,parse_activities 已被打桩)。"""

    def latest(self, _mark):
        return {}


def _set_activity_missing(monkeypatch, **observed):
    """跑 _set_activity,让目标卡在内联区与更多面板里都找不到。"""
    monkeypatch.setattr(bnc, "read_activity_action_text", lambda p, n: None)
    monkeypatch.setattr(bnc, "_find_activity_more_entry", lambda p: None)
    monkeypatch.setattr(bnc, "probe_activity_section", lambda p: observed)
    monkeypatch.setattr(
        bnc, "parse_activities",
        lambda raw: [{"id": "act-1", "name": "心理健康周", "desc": ""}],
    )
    monkeypatch.setattr(bnc, "_ACTIVITY_REVEAL_SCROLLS", 1)
    return bnc._set_activity(_PanelPage(), _NoopHuman(), _StubResponses(), "act-1")


def test_set_activity_reports_section_absent_when_nothing_found(monkeypatch):
    """卡 / 容器 / 区标题文案三样都没有 → activity_section_absent。"""
    out = _set_activity_missing(
        monkeypatch, cards=0, container=False, section_text=False)
    assert out["status"] == "error"
    assert out["reason"].startswith("activity_section_absent:")


def test_set_activity_reports_card_not_found_when_section_present(monkeypatch):
    """区在(容器或文案任一命中)、只是没有目标那张 → activity_card_not_found。"""
    out = _set_activity_missing(
        monkeypatch, cards=2, container=True, section_text=True)
    assert out["status"] == "error"
    assert out["reason"].startswith("activity_card_not_found:")


def test_panel_tried_evidence_survives_the_panel_covering_the_entry(monkeypatch):
    """面板开过但没找到:归因必须说「面板已打开并滚动查找过」。

    证据要在**过程中**记,不能事后重探一次「更多」入口 —— 面板一打开就可能把入口盖住,
    事后探到 None 会把"面板试过了"错报成"压根没入口可点",运营据此查错方向。
    """
    calls = {"n": 0}

    def _entry_disappears_after_open(_page):
        calls["n"] += 1
        return "MORE" if calls["n"] == 1 else None  # 开面板后入口被盖住

    monkeypatch.setattr(bnc, "read_activity_action_text", lambda p, n: None)
    monkeypatch.setattr(bnc, "_find_activity_card", lambda p, n: None)
    monkeypatch.setattr(bnc, "_find_activity_more_entry", _entry_disappears_after_open)
    monkeypatch.setattr(bnc, "probe_activity_section",
                        lambda p: {"cards": 2, "container": True, "section_text": True})
    monkeypatch.setattr(
        bnc, "parse_activities",
        lambda raw: [{"id": "43561", "name": "身边的心理学", "desc": ""}],
    )
    monkeypatch.setattr(bnc, "_ACTIVITY_REVEAL_SCROLLS", 1)
    monkeypatch.setattr(bnc, "_ACTIVITY_PANEL_SCROLLS", 1)
    state = {"panel_open": False, "text": "", "scrolls": 0, "hovered": False,
             "action": None}
    human = _RecHuman(state)

    out = bnc._set_activity(_PanelPage(), human, _StubResponses(), "43561")

    assert out["status"] == "error"
    assert out["reason"].startswith("activity_card_not_found:")
    assert "面板已打开" in out["reason"], out["reason"]
    assert out["observed"]["panel_opened"] is True


# ---------------- 视频封面:内联结构(真号截图证实非弹窗) ----------------
#
# 2026-08-07 真号 e2e(账号11,视频)实测 observed:封面区在、区里 3 张推荐图在、
# **file input 数 = 0**,而两个上传位候选(.cover-upload / [class*='upload'])全未命中
# → cover_upload_entry_not_found。所以这一轮:上传位候选扩成三层(class → 文案 → 尺寸
# tile)、**悬停优先于点击**(点上传位有弹原生 GTK 文件框卡死的历史前科)、失败时把封面区
# outerHTML 一起交出去。下面的替身按那次 observed 的形状搭。


class _Tile:
    """封面区里的一个后代元素(文案 / 尺寸 / 是否含推荐图)。"""

    def __init__(self, text, *, w=112, h=150, has_img=False):
        self.text = text
        self._box = {"x": 10.0, "y": 20.0, "width": float(w), "height": float(h)}
        self._has_img = has_img

    def inner_text(self):
        return self.text

    def bounding_box(self):
        return dict(self._box)

    def query_selector(self, sel):
        return "IMG" if (sel == "img" and self._has_img) else None


# 区标题那一坨:文案里带「优质封面示例」,是红线词之一(拿它当上传位就点歪了)
_COVER_TITLE_TEXT = ("设置封面 默认截取第一帧作为封面,优质的封面会吸引更多人浏览笔记 "
                     "优质封面示例")


class _CoverPage:
    """封面区的最小替身:内联、隐藏 file input、灌图后预览多一张 img。"""

    def __init__(self, *, section=True, has_input=True, entry=True, preview_grows=True,
                 entry_by_class=True, input_mounts_on="click", page_image_inputs=0,
                 upload_tile_text="设置封面 遇到问题?"):
        self._section = section
        self._has_input = has_input
        self._preview_grows = preview_grows
        self._entry_by_class = entry_by_class and entry
        self._mounts = input_mounts_on
        self.page_image_inputs = page_image_inputs
        self.imgs = 3           # 截图实测:一开始就有 3 张「智能推荐封面」
        self.files = None
        self.entry_clicked = False
        self.hovered = False
        self.section_html = "<div class='publish-page-content-cover'>…</div>"
        self.tiles = [_Tile(_COVER_TITLE_TEXT, w=600, h=40), _Tile("PK封面", w=60, h=24)]
        if entry:
            self.tiles.append(_Tile(upload_tile_text))
        self.tiles += [_Tile("智能推荐封面", has_img=True) for _ in range(3)]

    def _input_mounted(self):
        if self._has_input:
            return True
        if self._mounts == "hover":
            return self.hovered or self.entry_clicked
        return self.entry_clicked

    def query_selector(self, sel):
        if sel == bnc._COVER_SECTION:
            return _FakeSection(self) if self._section else None
        if "input[type='file']" in sel:
            if sel.startswith(bnc._COVER_SECTION):
                return _FakeUpload(self) if self._input_mounted() else None
            return _FakeUpload(self) if self.page_image_inputs else None
        if "upload" in sel:
            return _Tile("设置封面 遇到问题?") if self._entry_by_class else None
        return None

    def query_selector_all(self, sel):
        if sel.endswith("img"):
            return ["img"] * self.imgs
        if "input[type='file']" in sel:
            if sel.startswith(bnc._COVER_SECTION):
                return [_FakeUpload(self)] if self._input_mounted() else []
            n = self.page_image_inputs + (1 if self._input_mounted() else 0)
            return [_FakeUpload(self) for _ in range(n)]
        return []

    def wait_for_timeout(self, _ms):
        return None


class _FakeSection:
    def __init__(self, page):
        self._page = page

    def inner_text(self):
        return " ".join(t.text for t in self._page.tiles)

    def evaluate(self, _js):
        return self._page.section_html

    def query_selector_all(self, _sel):
        return list(self._page.tiles)


class _FakeUpload:
    def __init__(self, page):
        self._page = page

    def set_input_files(self, paths):
        self._page.files = paths
        if self._page._preview_grows:
            self._page.imgs += 1


class _CoverHuman:
    def __init__(self, page):
        self.page = page
        self.clicks = []
        self.click_targets = []
        self.hovers = []

    def click(self, target, reason="", **_k):
        self.clicks.append(reason)
        self.click_targets.append(target)
        self.page.entry_clicked = True

    def hover(self, target, reason="", **_k):
        self.hovers.append(reason)
        self.page.hovered = True

    def wait(self, *_a, **_k):
        return None


@pytest.fixture
def fast_cover_polls(monkeypatch):
    """替身的 wait_for_timeout 是空转,轮询窗口按真实秒走会白烧 CPU;测里压到毫秒级。"""
    monkeypatch.setattr(bnc, "_COVER_HOVER_TIMEOUT_S", 0.05)
    monkeypatch.setattr(bnc, "_COVER_APPLY_TIMEOUT_S", 0.05)


def test_cover_set_via_hidden_input_without_clicking_upload_button():
    """封面走 set_input_files 直传,**不点任何上传按钮**(避原生 GTK 文件框卡死)。"""
    page = _CoverPage()
    human = _CoverHuman(page)
    out = bnc.apply_video_cover(page, human, "/data/cover.jpg")

    assert out["status"] == "done", out
    assert page.files == ["/data/cover.jpg"]
    assert human.clicks == [], "file input 已在 DOM 里就不该点任何东西"
    assert human.hovers == [], "input 现成就连悬停都不必"


def test_cover_clicks_upload_slot_only_when_input_missing(fast_cover_polls):
    """input 懒挂载时才点灰色上传位;**绝不点推荐图**(点了是换成平台的图)。"""
    page = _CoverPage(has_input=False)
    human = _CoverHuman(page)
    out = bnc.apply_video_cover(page, human, "/data/cover.jpg")

    assert out["status"] == "done", out
    assert len(human.clicks) == 1 and "上传位" in human.clicks[0]
    assert all(kw not in human.clicks[0] for kw in bnc._COVER_FORBIDDEN)


def test_cover_prefers_hover_over_click_for_lazy_input():
    """悬停就能把 input 挂出来时,**一次都不点** —— 点上传位有原生文件框卡死前科。"""
    page = _CoverPage(has_input=False, input_mounts_on="hover")
    human = _CoverHuman(page)
    out = bnc.apply_video_cover(page, human, "/data/cover.jpg")

    assert out["status"] == "done", out
    assert human.hovers, "懒挂载时必须先试零风险的悬停"
    assert human.clicks == [], "悬停已经挂出 input,不该再点"


def test_cover_finds_entry_by_text_when_class_candidates_all_miss(fast_cover_polls):
    """class 候选全不命中(e2e 实测就是这样)时靠**文案**兜住上传位,而不是直接报错。"""
    page = _CoverPage(has_input=False, entry_by_class=False)
    human = _CoverHuman(page)
    out = bnc.apply_video_cover(page, human, "/data/cover.jpg")

    assert out["status"] == "done", out
    assert len(human.clicks) == 1
    assert human.click_targets[0].text == "设置封面 遇到问题?"


def test_cover_finds_entry_by_tile_size_when_wording_unknown(fast_cover_polls):
    """连文案都换了:退到尺寸 tile 启发式(≈112×150、区内、不含推荐图)。"""
    page = _CoverPage(has_input=False, entry_by_class=False, upload_tile_text="")
    human = _CoverHuman(page)
    out = bnc.apply_video_cover(page, human, "/data/cover.jpg")

    assert out["status"] == "done", out
    assert human.click_targets[0].text == ""


def test_cover_entry_never_targets_recommended_or_pk(fast_cover_polls):
    """红线:推荐图 tile / PK封面 / 区标题 一律不得被选成上传位。"""
    page = _CoverPage(has_input=False, entry_by_class=False)
    human = _CoverHuman(page)
    bnc.apply_video_cover(page, human, "/data/cover.jpg")

    touched = [t.text for t in human.click_targets] + list(human.hovers) + list(human.clicks)
    for blob in touched:
        assert all(kw not in blob for kw in bnc._COVER_FORBIDDEN), blob


def test_cover_uses_lone_page_level_image_input_without_clicking():
    """封面区内没有 input、但页面上**唯一**一个图片 input → 直接灌它,零点击。

    上传控件挂 body 级 portal 是常见形状;能不点就不点(点 = 原生文件框风险)。
    唯一性是安全边界:多于一个就宁可不猜。
    """
    page = _CoverPage(has_input=False, entry=False, page_image_inputs=1)
    human = _CoverHuman(page)
    out = bnc.apply_video_cover(page, human, "/data/cover.jpg")

    assert out["status"] == "done", out
    assert human.clicks == [] and human.hovers == []


def test_cover_reports_error_when_preview_never_changes(fast_cover_polls):
    """灌了图但封面区预览没变 → error(不许"点了就当成功")。"""
    page = _CoverPage(preview_grows=False)
    out = bnc.apply_video_cover(page, _CoverHuman(page), "/data/cover.jpg")
    assert out["status"] == "error"
    assert out["reason"].startswith("cover_preview_unchanged:")


def test_cover_missing_section_is_loud():
    """封面区都不在 → 明确报错带取证,绝不静默跳过。"""
    page = _CoverPage(section=False)
    out = bnc.apply_video_cover(page, _CoverHuman(page), "/data/cover.jpg")
    assert out["status"] == "error"
    assert out["reason"].startswith("cover_section_not_found:")
    assert "observed" in out


def test_cover_no_input_and_no_entry_is_loud():
    """既没 input 也没上传位 → 报错,别静默跳过。"""
    page = _CoverPage(has_input=False, entry=False)
    out = bnc.apply_video_cover(page, _CoverHuman(page), "/data/cover.jpg")
    assert out["status"] == "error"
    assert out["reason"].startswith("cover_upload_entry_not_found:")


def test_cover_failure_dumps_section_outer_html():
    """失败取证必须带封面区 **outerHTML**(截断到上限)——只报"候选未命中"等于没说。

    上一轮 e2e 就是只拿到一句"选择器全未命中",无从判断真实 class 叫什么;
    有了这段 HTML,下一次真跑一眼就能定位上传位的真值。
    """
    page = _CoverPage(has_input=False, entry=False)
    page.section_html = "<div class='publish-page-content-cover'>" + "x" * 9000
    out = bnc.apply_video_cover(page, _CoverHuman(page), "/data/cover.jpg")

    dump = out["observed"]["cover_section_html"]
    assert dump.startswith("<div class='publish-page-content-cover'>")
    assert len(dump) == bnc._COVER_HTML_DUMP_CHARS
    assert out["observed"]["file_inputs_in_page"] == 0
    assert out["observed"]["file_inputs_in_cover"] == 0
# ================= 移出合集(P0,2026-08-07 运营需求)=================
#
# 幂等矩阵四格 + 三条 fail-loud 闸(× 找不到 / chip 不消失 / 冒出没验证过的弹窗)。
# 判据来源:用户 2026-08-07 编辑页四张实拍(× 是 hover 态才显);**取证轮的 DOM dump
# 当时尚未跑到**,故"点 × 后是否有确认弹窗、是否立即生效"一律按 fail-loud 写死:
# 见到任何可见弹窗就中止整单,绝不盲点确认。


def _remove(editor, **kw):
    human = _Human(editor.page)
    out = bnc._remove_collection(
        editor.page, human, _StubResponses(), kw.pop("collection_id", "c1"), **kw
    )
    return out, human


def test_remove_skipped_when_note_in_no_collection():
    """空态:本就不在任何合集 → skipped(幂等语义,不算失败),且**零点击零悬停**。"""
    editor = Editor(collection=None)
    out, human = _remove(editor, collection_name="咨询师简介")
    assert out["status"] == "skipped"
    assert human.clicks == [] and human.hovers == []


def test_remove_done_when_chip_is_target():
    """在目标合集里:先悬停 chip 让 × 显出来 → 点 × → chip 回到空态 → done。"""
    editor = Editor(collection="咨询师简介")
    out, human = _remove(editor, collection_name="咨询师简介")
    assert out["status"] == "done", out
    assert out["name"] == "咨询师简介"
    assert editor.collection is None
    # 悬停必须发生在点击**之前** —— × 静态不存在,不悬停就点不到
    assert human.hovers, "没有悬停 chip,× 根本不会出现"
    assert len(human.clicks) == 1


def test_remove_skipped_when_in_another_collection():
    """在别的合集里 = 本就不在目标合集 → skipped;reason 必须带出实际所在合集名。"""
    editor = Editor(collection="心理咨询师")
    out, human = _remove(editor, collection_name="咨询师简介")
    assert out["status"] == "skipped"
    assert "心理咨询师" in out["reason"]
    assert human.clicks == []


def test_remove_refuses_when_name_cannot_be_verified():
    """比对不上就**绝不点 ×**:移出是破坏性操作,瞎点可能把笔记从正确的合集里摘出来。"""
    editor = Editor(collection="咨询师简介")
    out, human = _remove(editor)  # 不传 name,响应缓存也是空的
    assert out["status"] == "error"
    assert out["reason"].startswith("collection_remove_unverifiable:")
    assert human.clicks == []


def test_remove_errors_when_close_icon_absent_after_hover():
    """悬停了 × 仍不出现 → fail-loud,reason 带当时 chip 文案(它也是"不在合集"的反证)。"""
    editor = Editor(collection="咨询师简介", close_icon_absent=True)
    out, human = _remove(editor, collection_name="咨询师简介")
    assert out["status"] == "error"
    assert out["reason"].startswith("close_icon_not_found_after_hover:")
    assert "咨询师简介" in out["reason"]
    assert human.clicks == []


def test_remove_errors_when_chip_survives_the_click():
    """点了 × chip 还在 → collection_not_removed(绝不拿"没报错"当成功凭据)。"""
    editor = Editor(collection="咨询师简介", silent_close_icon=True)
    out, _human = _remove(editor, collection_name="咨询师简介")
    assert out["status"] == "error"
    assert out["reason"].startswith("collection_not_removed:")


def test_remove_aborts_whole_request_on_unverified_modal():
    """点 × 后冒出没验证过的弹窗:**整单中止**(硬错),绝不猜哪个按钮是"确认"。

    弹窗形态是设计 §7 的未验证点,取证轮没跑到。此时页面处于不可预期态,继续走下去
    就是在弹窗上盲操作、并且可能带着弹窗去点发布 —— 一次全量覆盖提交的代价太大。
    """
    editor = Editor(collection="咨询师简介",
                    modal_after_close_icon="确认移出该合集?")
    with pytest.raises(bnc.NoteComponentsError) as exc:
        _remove(editor, collection_name="咨询师简介")
    assert exc.value.reason.startswith("collection_remove_unknown_modal:")
    assert "确认移出该合集?" in exc.value.reason


def test_remove_ignores_modals_that_were_already_there():
    """判据是"**新**冒出来的弹窗",不是"页面上有弹窗"。

    编辑器页本就可能挂着别的 ``.d-modal`` 容器,拿"有没有"当判据会把这个能力整个卡死;
    真正要拦的是那个**因为我们点了 × 才出现**的确认框。基线里的那个不算。
    """
    editor = Editor(collection="咨询师简介")
    editor.modal_text = "页面上本来就有的别的弹窗"
    out, _human = _remove(editor, collection_name="咨询师简介")
    assert out["status"] == "done", out


# ---------------- 名字比对必须全等(同族合集名) ----------------
#
# 「科普」与「科普合集」并存时,包含判据会一路放行:笔记明明在「科普合集」里,请求移出
# 「科普」照样点 ×、回读空态、报 done —— 笔记被从**错误的**合集里摘了出来,回执还是成功。
# 移出是破坏性操作,这条比对是唯一防线,所以三处判据(动手闸 / 提交后回读 / P1 扫描名单)
# 一律改全等。加入侧的同款包含判据不动:那边错判只是良性 skip。


def test_remove_refuses_when_chip_only_contains_target_name():
    """chip「科普合集」+ 目标「科普」:包含但不全等 → fail-loud,**零点击零悬停**。

    reason 里必须带当场 chip 原文 —— chip 文案会不会掺装饰字符尚未取证,这条 fail-loud
    正好把真实文案交给真号首验去收敛。
    """
    editor = Editor(collection="科普合集")
    out, human = _remove(editor, collection_id="c2", collection_name="科普")
    assert out["status"] == "error"
    assert out["reason"].startswith("collection_remove_unverifiable:")
    assert "'科普合集'" in out["reason"], out["reason"]
    assert human.clicks == [] and human.hovers == []
    assert editor.collection == "科普合集", "笔记被从错误的合集里摘出去了"


def test_remove_skips_when_chip_is_a_prefix_of_the_target():
    """反方向(chip「科普」+ 目标「科普合集」):本就不在目标合集 → skipped,一次都没点。"""
    editor = Editor(collection="科普")
    out, human = _remove(editor, collection_id="c2", collection_name="科普合集")
    assert out["status"] == "skipped"
    assert out["reason"].startswith("collection_in_another_not_target:")
    assert human.clicks == []


def test_remove_exact_name_still_goes_through():
    """全等命中不回归:该点的还得点,该 done 的还得 done。"""
    editor = Editor(collection="科普")
    out, human = _remove(editor, collection_id="c2", collection_name="科普")
    assert out["status"] == "done", out
    assert editor.collection is None
    assert len(human.clicks) == 1


def _verify_remove(editor, monkeypatch, *, step_name):
    monkeypatch.setattr(bnc, "open_update_page", lambda *_a, **_kw: None)
    verified, _permission, _extra = bnc._verify_after_submit(
        editor.page, 1, "n1",
        collection_id=None, quoted_note_id=None, activity_id=None,
        remove_collection_id="c2",
        outcomes={"collection_remove": {"status": "done", "name": step_name}},
        responses=_StubResponses(),
    )
    return verified["collection_remove"]


def test_verify_readback_reads_sibling_name_as_target_absent(monkeypatch):
    """提交后回读到「科普合集」:目标「科普」确实不在里面 —— 用包含会反过来判成"还在"。"""
    assert _verify_remove(Editor(collection="科普合集"), monkeypatch, step_name="科普") is True


def test_verify_readback_still_catches_the_target_surviving(monkeypatch):
    """全等命中不回归:目标名原样躺在合集区 = 没移掉,照旧判 False。"""
    assert _verify_remove(Editor(collection="科普"), monkeypatch, step_name="科普") is False


# ---------------- apply_components 编排 ----------------


def test_remove_step_runs_before_join_step(monkeypatch):
    """移出排在加入**之前**:将来"换合集"分两次请求接力时顺序才是对的。"""
    order = []
    monkeypatch.setattr(bnc, "_remove_collection",
                        lambda *a, **kw: order.append("remove") or {"status": "done"})
    monkeypatch.setattr(bnc, "_set_collection",
                        lambda *a, **kw: order.append("join") or {"status": "done"})
    editor = Editor()
    out = bnc.apply_components(
        editor.page, _Human(editor.page), _StubResponses(),
        collection_id="c1", remove_collection_id="c2",
    )
    assert order == ["remove", "join"]
    assert set(out) == {"collection", "collection_remove"}


def test_remove_only_request_touches_no_other_step(monkeypatch):
    """只传移出:引用 / 活动 / 加入三步一步都不许跑。"""
    for name in ("_set_collection", "_set_quote", "_set_activity"):
        monkeypatch.setattr(bnc, name, lambda *a, **kw: (_ for _ in ()).throw(
            AssertionError("没请求的组件步被跑了")))
    editor = Editor(collection="咨询师简介")
    out = bnc.apply_components(
        editor.page, _Human(editor.page), _StubResponses(),
        remove_collection_id="c1", remove_collection_name="咨询师简介",
    )
    assert list(out) == ["collection_remove"]
    assert out["collection_remove"]["status"] == "done"


# ---------------- 全流程:提交 + 回读三态 ----------------


def test_remove_full_flow_is_done(monkeypatch, wired):
    """在目标合集 → 移出 → 提交 → 重进页面回读确认空态 → applied.collection_remove=True。"""
    editor = Editor(collection="咨询师简介")
    _wire(monkeypatch, editor, wired)
    out = _run(editor, remove_collection_id="c1", remove_collection_name="咨询师简介")
    assert out["status"] == "done", out
    assert out["applied"] == {"collection_remove": True}
    assert out["submitted"] is True
    assert editor.submitted == 1


def test_remove_full_flow_reports_false_when_platform_restores(monkeypatch, wired):
    """提交后重进页面合集又回来了(服务端没接受)→ applied=False,绝不报 done。"""
    editor = Editor(collection="咨询师简介")
    _wire(monkeypatch, editor, wired)
    original_submit = editor.submit

    def submit_then_restore():
        original_submit()
        editor.collection = "咨询师简介"   # 服务端把绑定又写回来了

    monkeypatch.setattr(editor, "submit", submit_then_restore)
    out = _run(editor, remove_collection_id="c1", remove_collection_name="咨询师简介")
    assert out["applied"]["collection_remove"] is False
    assert out["status"] == "failed"


def test_remove_full_flow_reports_none_when_readback_page_dies(monkeypatch, wired):
    """回读进不去页面 → applied=None(未确认),如实上报,不乐观当成功。"""
    editor = Editor(collection="咨询师简介")
    _wire(monkeypatch, editor, wired)
    calls = {"n": 0}
    real_open = bnc.open_update_page

    def flaky_open(page, account_id, note_id):
        calls["n"] += 1
        if calls["n"] > 1:
            raise bnc.NoteComponentsError("editor_not_ready: 回读进不去")
        return real_open(page, account_id, note_id)

    monkeypatch.setattr(bnc, "open_update_page", flaky_open)
    out = _run(editor, remove_collection_id="c1", remove_collection_name="咨询师简介")
    assert out["applied"]["collection_remove"] is None


def test_remove_noop_never_submits(monkeypatch, wired):
    """本就不在该合集 → **一次发布都不点**:提交是全量覆盖语义,零变更不值得付这个风险。

    存量清理会对上百篇非目标笔记跑这条路,每篇白提交一次就是上百次真发布。
    """
    editor = Editor(collection=None)
    _wire(monkeypatch, editor, wired, publish=False)
    out = _run(editor, remove_collection_id="c1", remove_collection_name="咨询师简介")
    assert out["status"] == "done"
    assert out["applied"] == {"collection_remove": True}
    assert out["submitted"] is False
    assert editor.submitted == 0


def test_remove_aborted_edit_marks_step_as_not_executed(monkeypatch, wired):
    """前序破坏性编辑步失败弃提交时,移出步也要如实记「因前序失败未执行」。"""
    editor = Editor(collection="咨询师简介")
    _wire(monkeypatch, editor, wired, publish=False)
    monkeypatch.setattr(
        bnc, "_run_edit_steps",
        lambda *a, **kw: {"aborted": True, "abort_reason": "图片闸不过",
                          "outcomes": {}, "topics_dropped": [], "removed": 0, "added": 0},
    )
    out = _run(editor, remove_collection_id="c1", remove_collection_name="咨询师简介",
               title="新标题")
    assert out["aborted_before_submit"] is True
    assert out["components"]["collection_remove"]["reason"] == bnc._SKIPPED_REASON


# ---------------- 补录原创声明:真走一遍假 DOM(运营 2026-08-08 来文) ----------------
#
# 与上面 wire 那组的分工:那边钉编排(谁先谁后、提不提交、结果怎么汇总),这边钉
# **页面上到底怎么做的** —— 开关点了没、协议弹窗走没走完、失败时弹窗关没关。
# 两份夹具的结构一致性由 test_original_declaration.py 那组选择器真值锁负责。


def _original_clicks(human):
    """本次跑里所有与原创声明有关的点击 reason(不含三组件/发布)。"""
    return [reason for reason, _text in human.clicks
            if "原创" in reason or "同意" in reason or "声明" in reason]


def test_original_already_on_is_skipped_with_zero_clicks(monkeypatch, wired):
    """**幂等的立命之本**:进页面先读当前态,已是开态 → skipped 且一次都不点。

    运营要拿这条对 49 篇批量重跑;只要这里多点一下,重跑就会把已达标的笔记再走一遍
    协议弹窗,还顺带引出一次真提交。
    """
    editor = Editor(original_declared=True)
    _wire(monkeypatch, editor, wired, publish=False)

    out = _run(editor, set_original_declaration=True)

    assert out["components"]["original_declaration"]["status"] == "skipped"
    assert out["components"]["original_declaration"]["observed"] == "already_on"
    assert _original_clicks(wired[-1]) == [], "已是开态就一次都不许点"
    assert editor.original_modal_open is False, "零点击就不会弹出协议弹窗"
    assert out["submitted"] is False and editor.submitted == 0
    assert out["applied"] == {"original_declaration": True}
    assert out["status"] == "done"


def test_original_off_walks_the_whole_consent_chain(monkeypatch, wired):
    """关态 → 点开关 → 勾「我已阅读并同意」→ 点「声明原创」→ done,顺序不许乱。"""
    editor = Editor(original_declared=False)
    _wire(monkeypatch, editor, wired)

    out = _run(editor, set_original_declaration=True)

    reasons = _original_clicks(wired[-1])
    toggle = next(i for i, r in enumerate(reasons) if "打开原创声明开关" in r)
    consent = next(i for i, r in enumerate(reasons) if "同意" in r)
    confirm = next(i for i, r in enumerate(reasons) if "点「声明原创」" in r)
    assert toggle < consent < confirm, reasons
    assert editor.original_declared_for_real is True
    assert editor.original_closed_by_x is False, "走成了就不该用 X 关弹窗"
    assert out["components"]["original_declaration"]["status"] == "done"
    assert out["components"]["original_declaration"]["observed"]["via"] == "consent_modal"
    assert out["submitted"] is True and editor.submitted == 1
    assert out["applied"] == {"original_declaration": True}


def test_original_consent_click_targets_the_16px_square(monkeypatch, wired):
    """勾同意点的必须是 16×16 的 simulator 方块,且**不加随机偏移**。

    这正是 08-07 那个修复:点宽容器时随机偏移约 40% 概率落进《原创声明须知》超链接,
    链接吃掉事件 → 勾不上 → 08-05~08-07 那 49 篇全没标上。编辑链共用同一个函数,
    所以这条修复在这里必须同样成立 —— 这是运营点名要确认的事。
    """
    editor = Editor(original_declared=False)
    _wire(monkeypatch, editor, wired)
    seen = []
    real_click = _Human.click

    def spy(self, target, *, reason="", **kw):
        seen.append((reason, kw.get("random_offset", True)))
        return real_click(self, target, reason=reason, **kw)

    monkeypatch.setattr(_Human, "click", spy)

    out = _run(editor, set_original_declaration=True)

    hits = [(r, off) for r, off in seen if "同意" in r]
    assert len(hits) == 1, f"勾同意应恰好点一次: {seen}"
    reason, random_offset = hits[0]
    assert "simulator 方块" in reason, f"必须点方块而不是宽容器: {reason}"
    assert random_offset is False, "16×16 的方块上随机偏移只会把落点推向边缘"
    assert out["components"]["original_declaration"]["status"] == "done"


def test_original_consent_ineffective_reports_reason_and_observed(monkeypatch, wired):
    """勾不上(复刻撞链接)→ error 带可执行的 reason 与当场取证,且**把弹窗关掉**。

    残留弹窗会盖住发布按钮(2026-08-02 事故同型),比声明没成更严重。
    """
    editor = Editor(original_declared=False, original_consent_effective=False)
    _wire(monkeypatch, editor, wired, publish=False)

    out = _run(editor, set_original_declaration=True)

    step = out["components"]["original_declaration"]
    assert step["status"] == "error"
    assert step["reason"].startswith("original_consent_not_ticked:"), step["reason"]
    assert step["observed"]["via"] == "consent_modal"
    assert step["observed"]["consent_ticked"] is False
    assert "unchecked" in step["observed"]["consent_simulator_class"]
    assert editor.original_modal_open is False, "链走不完必须关弹窗"
    assert editor.original_closed_by_x is True
    assert editor.original_declared_for_real is False
    # 本单只有补声明 → 整单失败,且一次发布都不点
    assert out["status"] == "failed"
    assert out["submitted"] is False and editor.submitted == 0
    assert "note_components_all_failed" in out["error"]


def test_original_entry_missing_is_error_not_silent_skip(monkeypatch, wired):
    """页面上压根没有「原创声明」这一行 → 报错,绝不静默当成"不用补"。"""
    editor = Editor(original_declared=False, original_row_absent=True)
    _wire(monkeypatch, editor, wired, publish=False)

    out = _run(editor, set_original_declaration=True)

    step = out["components"]["original_declaration"]
    assert step["status"] == "error"
    assert step["reason"].startswith("original_entry_not_found:"), step["reason"]
    assert out["submitted"] is False


def test_original_readback_false_when_platform_does_not_show_it(monkeypatch, wired):
    """编辑器里声明成了,但重进页面开关没回显开态 → applied=False,绝不报 done。

    平台是否在编辑页回显已声明态**没有实测证据**(手上没有"已声明笔记的编辑页"夹具)。
    真要是不回显,这里就该如实报 false 让人去人工核对,而不是乐观当成功 ——
    这条产品线的失败是静默的。
    """
    editor = Editor(original_declared=False, original_persists_on_readback=False)
    _wire(monkeypatch, editor, wired)

    out = _run(editor, set_original_declaration=True)

    assert editor.original_declared_for_real is True, "编辑器里确实点成了"
    assert out["components"]["original_declaration"]["status"] == "done"
    assert out["applied"]["original_declaration"] is False
    assert out["status"] == "failed"


def test_original_declaration_with_collection_submits_once(monkeypatch, wired):
    """补声明与三组件同一单:仍然只提交一次,两项都回读确认。"""
    editor = Editor(original_declared=False)
    _wire(monkeypatch, editor, wired)

    out = _run(editor, collection_id="c1", collection_name="咨询师简介",
               set_original_declaration=True)

    assert editor.submitted == 1, "提交次数就是风险次数"
    assert out["applied"] == {"collection": True, "original_declaration": True}
    assert out["status"] == "done"


# ---------------- 补挂话题(追加语义,2026-08-08)----------------
#
# 编排层测试:话题的 DOM 步骤(append_topics)由 test_note_topics.py 单独钉死,这里把它
# 打桩成"往 editor.body 追加话题实体",专测 set_note_components 的编排——追加差集算得对、
# 提交决策把补上话题算成要提交、回读取平台实况(非乐观态)、结果键汇总与 status 折算。


def _stub_append_topics(monkeypatch, editor):
    """把 bnc.append_topics 打桩:命中的话题写进 editor.body(#名字[话题]#),模拟点选成实体。

    返回记录调用的 dict(seen["to_add"]),供断言"编排层只把差集喂进来"。
    """
    seen = {}

    def fake_append(page, human, to_add):
        seen["to_add"] = list(to_add)
        for tag in to_add:
            name = str(tag).lstrip("#").strip()
            editor.body = f"{editor.body} #{name}[话题]#"
        return {"status": "done", "in_editor_added": [str(t).lstrip("#").strip()
                                                       for t in to_add], "failed": []}

    monkeypatch.setattr(bnc, "append_topics", fake_append)
    return seen


def test_topics_appended_keeps_existing_and_reads_back_all(monkeypatch, wired):
    """验收「追加语义」:已有 1 个话题,补 4 个 → 只补差集、提交后回读得 5 个、原 1 个保留。"""
    editor = Editor(body="正文 #过度寻求保证[话题]#")
    _wire(monkeypatch, editor, wired)
    seen = _stub_append_topics(monkeypatch, editor)

    out = _run(editor, topics=["投射性认同", "焦虑型依恋", "亲密关系", "心理科普"])

    # 编排层只把"现有没有的"喂给补话题步骤(去重差集)
    assert seen["to_add"] == ["投射性认同", "焦虑型依恋", "亲密关系", "心理科普"]
    assert editor.submitted == 1
    assert out["status"] == "done"
    # applied.topics 是**平台实况全量话题列表**:5 个,原「过度寻求保证」保留
    assert set(out["applied"]["topics"]) == {
        "过度寻求保证", "投射性认同", "焦虑型依恋", "亲密关系", "心理科普"
    }
    assert len(out["applied"]["topics"]) == 5
    assert set(out["topics_added"]) == {"投射性认同", "焦虑型依恋", "亲密关系", "心理科普"}
    assert out["topics_existing"] == ["过度寻求保证"]
    assert out["topics_truncated"] == []
    assert out["topics_failed"] == []


def test_topics_readback_reflects_platform_truth_not_optimistic(monkeypatch, wired):
    """回读判据 = 平台实况:点选了 3 个但平台静默丢 1 个 → applied.topics 只认真挂上的。"""
    editor = Editor(body="正文", drop_topics_on_submit=("被丢弃",))
    _wire(monkeypatch, editor, wired)
    _stub_append_topics(monkeypatch, editor)

    out = _run(editor, topics=["投射性认同", "被丢弃", "亲密关系"])

    assert editor.submitted == 1
    # 乐观态会报 3 个全成;平台实况只有 2 个(被丢弃的没挂上)
    assert set(out["applied"]["topics"]) == {"投射性认同", "亲密关系"}
    assert set(out["topics_added"]) == {"投射性认同", "亲密关系"}
    # 没挂上的进 topics_failed,原因是"回读没确认"(非连坐,其余两个照样成)
    failed_tags = {f["component"] for f in out["topics_failed"]}
    assert failed_tags == {"topic:被丢弃"}
    assert "not_confirmed_on_readback" in out["topics_failed"][0]["reason"]
    # 一个话题没挂上 → 整体部分生效,不谎报 done
    assert out["status"] == "partially_applied"


def test_topics_all_present_is_idempotent_zero_submit(monkeypatch, wired):
    """请求的话题全已挂 → 差集为空、零点击零提交(幂等),applied.topics 仍给现有实况。"""
    editor = Editor(body="正文 #复杂性创伤[话题]# #CPTSD[话题]#")
    _wire(monkeypatch, editor, wired, publish=False)   # 不该点发布
    seen = _stub_append_topics(monkeypatch, editor)

    out = _run(editor, topics=["复杂性创伤", "#CPTSD"])

    assert "to_add" not in seen              # 差集空 → append_topics 压根没被调用
    assert editor.submitted == 0             # 幂等零提交
    assert out["status"] == "done"
    assert set(out["applied"]["topics"]) == {"复杂性创伤", "CPTSD"}
    assert out["topics_added"] == []


def test_topics_truncated_over_ten_reports_which_kept(monkeypatch, wired):
    """验收「上限截断」:现有 8 + 补 4 → 只补 2、截 2,truncated 如实说明,整体部分生效。"""
    existing = " ".join(f"#旧{i}[话题]#" for i in range(8))
    editor = Editor(body=f"正文 {existing}")
    _wire(monkeypatch, editor, wired)
    seen = _stub_append_topics(monkeypatch, editor)

    out = _run(editor, topics=["新A", "新B", "新C", "新D"])

    assert seen["to_add"] == ["新A", "新B"]           # 只补补得下的 2 个
    assert editor.submitted == 1
    assert len(out["applied"]["topics"]) == 10        # 填满上限
    assert set(out["topics_added"]) == {"新A", "新B"}
    assert out["topics_truncated"] == ["新C", "新D"]
    truncated_fail = {f["component"] for f in out["topics_failed"]}
    assert truncated_fail == {"topic:新C", "topic:新D"}
    assert all("truncated_over_cap" in f["reason"] for f in out["topics_failed"])
    # 有补上的也有截掉的 → 部分生效
    assert out["status"] == "partially_applied"


def test_topics_combine_with_collection_single_submit(monkeypatch, wired):
    """话题与合集混在一次请求:仍只一次提交,两者各自回读,applied 同时含 collection 与 topics。"""
    editor = Editor(body="正文")
    _wire(monkeypatch, editor, wired)
    _stub_append_topics(monkeypatch, editor)

    out = _run(editor, collection_id="c1", collection_name="咨询师简介",
               topics=["心理科普"])

    assert editor.submitted == 1, "话题+组件仍只一次全量覆盖提交"
    assert out["applied"]["collection"] is True
    assert out["applied"]["topics"] == ["心理科普"]
    assert out["status"] == "done"
