"""note-components 分组 REST(4 端点):合集列表 / 活动列表(只读)+ 笔记编辑(202)+ 轮询。

设计 docs/design/2026-08-01-note-components-design.md 第 3.5 节。三组件 = 加入合集 /
引用笔记 / 关联活动,发布新笔记(``POST /api/publish-jobs`` 同名字段)与编辑已发布笔记
(本组端点)两条路都能设。

POST 那个端点**同时**是已发布笔记的内容编辑入口(标题 / 正文 / 图片增删,设计
docs/design/2026-08-03-note-editing-design.md):同一次请求、同一次提交,与三组件共存。
不另开 kind 的理由只有一条 —— **提交次数就是风险次数**,提交是全量覆盖语义,拆两条路
就是把同一颗雷埋两处。

⚠️ 三条要让调用方一眼看见的事:

- **这条产品线的失败是静默的**:``success:true`` 照返但设置被服务端丢弃(私密笔记的合集
  绑定)、按钮点了没反应也没报错(活动首次点击)。故本组端点**逐项回读校验**,轮询结果
  里 ``applied`` 是逐个组件的真实生效情况,``done`` 只在**全部**回读确认后才给,
  部分生效一律 ``partially_applied``。
- **非幂等**:一次设置就是一次全量覆盖提交,且关联活动会往正文**追加**话题;失败**不会**
  自动重跑,调用方也别盲目重试。
- 两个 GET **要起一个真浏览器会话**(这两个列表只有笔记编辑器页会去要,没有独立只读页),
  常态 40-90 秒才返回 —— 调用方的 HTTP 超时请设到 ≥180 秒。
"""

import asyncio
import shutil
from pathlib import Path
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import select

from app.auth.context import current_operator
from app.auth.guards import assert_account_access
from app.browser.atomic_tasks import (
    XHS_MAX_BODY_LENGTH,
    XHS_MAX_TITLE_DISPLAY,
    XHS_MAX_TOPICS,
)
from app.browser.images import materialize_images
from app.browser.note_components import DEFAULT_ACTIVITY_KEYWORDS, filter_activities
from app.browser.text_formatter import get_display_length
from app.core.config import settings
from app.core.db import get_session
from app.core.errors import NotFoundError
from app.http.job_polling import QUEUE_MANIFEST_NOTE, base_view, load_job
from app.models.published_note import PublishedNote
from app.models.xhs_account import XhsAccount
from app.publish.policy import XHS_COVER_EXTENSIONS, cover_ext_allowed
from app.services import counselor_quote, note_components
from app.services.quota import assert_operator_quota

router = APIRouter()

# 不过滤的显式写法(留空也等价);默认关键词表见 DEFAULT_ACTIVITY_KEYWORDS
_NO_FILTER = "*"

# 图文笔记图片张数硬上限(与 publish_rest._MAX_IMAGES 同源:小红书图文最多 18 张)
_MAX_IMAGES = 18
# 台账 note_type 里算"图文笔记"的取值:实测只见过 normal(见 published_note.note_type
# 注释)。**NULL 也不放行** —— NULL 是"未知",而这是一次全量覆盖提交,类型不可确认
# 就不编辑(编辑设计 1.2:深链 noteType=normal 只对图文验证过)。
_IMAGE_NOTE_TYPES = ("normal",)
# 台账 note_type 里算"视频笔记"的取值。**只有视频笔记有封面区** —— 图文笔记的封面就是
# 第一张图(与发布端点同口径),更新页上根本没有 .publish-page-content-cover 那一块。
# NULL 同样不放行:理由与图文那条一致,类型不可确认就不做全量覆盖提交。
_VIDEO_NOTE_TYPES = ("video",)
# 封面图扩展名白名单复用发布端同一真源(app.publish.policy):扩展名白名单三处同一套,
# 弹窗内 file input 实测 accept=image/png, image/jpeg, image/*,与它一致。

MANIFEST_ENTRIES = [
    {
        "method": "GET", "path": "/api/accounts/{account_id}/collections",
        "summary": "读该号的合集列表(会起浏览器,慢)",
        "admin_only": False,
        "params": {
            "account_id": "path,int",
            "note_id": "query,str|None(打开哪篇笔记的编辑器去读;省略则自动取该号台账里"
                       "最近一篇有 note_id 的笔记)",
        },
        "returns": "{collections:[{id,name,desc,note_num}], note_id(实际打开的那篇)}",
        "errors": "400=该号台账里没有任何带 note_id 的笔记(给不出落脚点,请显式传 note_id);"
                  "403=无该号授权;404=账号不存在;429=运营者未完成任务配额已满;"
                  "502=浏览器会话失败 / 账号无可用 cookie / 编辑页进不去(detail 给原因)",
        "notes": "⚠️ **同步阻塞端点,常态 40-90 秒**:合集列表只有笔记编辑器页会去要,没有"
                 "独立的只读页面,故要起一个真 camoufox 会话打开一篇笔记的编辑页被动读接口。"
                 "调用方 HTTP 超时请设 ≥180 秒。全程只读:打开编辑器不提交就不落库,对那篇"
                 "笔记无副作用。同号浏览器操作共享 per-account 锁串行,别对同号并发发起。"
                 "collections[].id 就是 POST 三组件时的 collection_id。",
    },
    {
        "method": "GET", "path": "/api/accounts/{account_id}/activities",
        "summary": "读平台活动列表并按心理学关键词筛选(会起浏览器,慢)",
        "admin_only": False,
        "params": {
            "account_id": "path,int",
            "note_id": "query,str|None(同 collections)",
            "keywords": "query,str|None(逗号分隔;省略=用默认心理学词表;传 * 或空=不过滤)",
        },
        "returns": "{activities:[{id,name,desc,matched_keywords}], total(筛选前总数), "
                   "keywords(实际生效的词表), note_id}",
        "errors": "同 GET /collections",
        "notes": "⚠️ 同步阻塞端点,常态 40-90 秒(理由同 collections)。"
                 f"默认词表 {'/'.join(DEFAULT_ACTIVITY_KEYWORDS)};"
                 "**按活动名 + 活动简介联合匹配**,并刻意不收「自我」「成长」这类过宽的词"
                 "(实测「howto穿出自我」因『自我』命中,其实是穿搭活动)。"
                 "活动频繁上下线,**系统里没有任何死名单**,每次都是现拉现筛;"
                 "matched_keywords 告诉你这条凭什么命中,不认可就自己传 keywords 重筛。"
                 "activities[].id 就是 POST 三组件时的 activity_id。",
    },
    {
        "method": "POST", "path": "/api/accounts/{account_id}/note-components",
        "summary": "异步编辑一篇已发布笔记:三组件 + 标题/正文/图片增删 + 补录原创声明"
                   "(会真提交一次发布,慎用)",
        "admin_only": False,
        "params": {
            "account_id": "path,int",
            "note_id": "body,str(要编辑的笔记平台 id,**必填**,深链定位)",
            "collection_id": "body,str|None(**把这篇笔记归拢进该合集——它会成为合集成员、"
                             "出现在合集页;这不是『引用/提及』某个合集**,只想在正文里导流到"
                             "合集请自行写进 content 文案。加入哪个合集,取自 GET /collections)",
            "remove_collection_id": "body,str|None(把这篇笔记**移出**该合集,与 collection_id "
                                    "加入对称;取自 GET /collections。**幂等**:笔记本就不在该"
                                    "合集(空态、或在别的合集里)→ components.collection_remove."
                                    "status='skipped'、applied.collection_remove=true,"
                                    "**不算失败且一次发布都不点**(零变更不值得付一次全量覆盖"
                                    "提交的风险),可安全重跑;与 collection_id **同时传 → 422**"
                                    "(换合集请分两次请求:先移出、回读确认、再加入))",
            "remove_collection_name": "body,str|None(要移出的那个合集的名字,**强烈建议每次带 "
                                      "remove_collection_id 时都传**——浏览器层靠它确认「当前"
                                      "所在合集就是目标」,比对不上会报 "
                                      "collection_remove_unverifiable 并**一次都不点**"
                                      "(移出是破坏性操作,点错等于把笔记从正确的合集里摘出来);"
                                      "单独给不构成组件请求)",
            "collection_name": "body,str|None(该合集的名字,**建议每次带 collection_id 时都顺带传**"
                               "——你从 GET /collections 本就有 id→名映射。笔记已在某合集时"
                               "「加入合集」按钮根本不渲染,浏览器层只能靠名字确认已选的是不是"
                               "目标:已在**同一**合集 → applied.collection=true(零点击,"
                               "components.collection.status='skipped');已在**别的**合集 → "
                               "collection_already_in_another;不传且确认不了 → "
                               "collection_chosen_unverifiable。单独给不构成组件请求)",
            "quoted_note_id": "body,str|None(引用哪篇笔记;本号笔记走「我的笔记」,"
                              "他号笔记自动走「他人笔记」tab 按 note_id 检索(显式跨账号"
                              "引用可行,如接待员联系方式那篇——检索不到会如实报错拒引);"
                              "**优先级高于 related_counselor**)",
            "activity_id": "body,str|None(关联哪个活动,取自 GET /activities)",
            "related_counselor": "body,str|None(这篇推介哪位咨询师的姓名,如「李宇」;"
                                  "是 quoted_note_id 的替代写法,由系统推导引用哪篇)",
            "set_original_declaration": "body,bool|None(**补录原创声明**,2026-08-08 上线。"
                                        "**只支持开启**:传 true 才动手,传 false 直接 422"
                                        "(关闭的平台行为未取证且是破坏性动作,不在本期范围);"
                                        "**幂等**:已是开态 → components.original_declaration."
                                        "status='skipped'、applied.original_declaration=true、"
                                        "**零点击**,可安全批量重跑)",
            "title": "body,str|None(整体替换标题;省略/null=不改;**空串=清空标题**,"
                     "与 null 语义不同;显示长度 >20 直接 422,**不截断**)",
            "content": "body,str|None(整体替换正文;省略/null=不改;≤900 字;"
                       "**不支持清空**,空串 422)",
            "add_images": "body,list|None(追加到现有图序**末尾**,1-18 项;每项三形态之一:"
                          "http URL / {b64,ext} / 本服务 /uploads 路径,与发布链路同款;"
                          "**建 job 前就落盘**,坏图当场 422)",
            "remove_image_indexes": "body,list[int]|None(要删的图片下标,**1-based 且按"
                                    "发布态图序**;不许重复;删完**原图必须剩 ≥1**"
                                    "(不许拿 add_images 凑数))",
            "expected_image_count": "body,int|None(你认为这篇现在有几张图;给了 add_images "
                                    "或 remove_image_indexes 时**必填**,1-18)",
            "cover": "body,str|None(**换自定义封面**,2026-08-08 上线。服务器侧本地图片路径,"
                     f"扩展名限 {'/'.join(XHS_COVER_EXTENSIONS)},文件不存在 → 422。"
                     "**只对视频笔记有效**:台账 note_type=video 才受理,**图文笔记传了直接 "
                     "422**(图文的封面就是第一张图,与发布端点同口径;真要换请用 "
                     "remove_image_indexes+add_images 换掉第一张),note_type 为 null 同样 422。"
                     "**幂等**:这篇已经是自定义封面(不是平台自动截的首帧)→ "
                     "components.cover.status='skipped'、applied.cover=true、**零点击**;"
                     "若这次请求只有它一件事且落了 skipped,**一次发布都不点**,批量重跑安全。"
                     "单独给它就构成一次有效请求)",
            "topics": "body,list[str]|None(**补挂话题**,2026-08-08 上线,为存量视频笔记补"
                      "话题空置。**追加语义不是替换**:传入话题**追加**到现有话题、去重、"
                      f"总数 >10 截断(小红书单篇 ≤{XHS_MAX_TOPICS};全量替换在已有话题的笔记上"
                      "太危险,一次手滑清空别人的话题,故只做追加)。带 # 或不带都行,内部规整"
                      "去重。回读判据是**平台实况**(applied.topics 反映真挂上的全量话题,不是"
                      "点了就算);逐个话题独立成败,失败进 failed[](component=`topic:话题名`)"
                      "带原因、**不连坐**其余话题;请求的全已挂 → 零点击零提交(幂等)。"
                      "**任何 note_type 都受理**(视频/图文都靠正文编辑器输话题,与封面只对视频"
                      "不同);单独给它就构成一次有效请求)",
        },
        "returns": '{job_id, status:"queued"}',
        "errors": "403=无该号授权;404=账号不存在,或**带编辑字段时**台账里查无此 note_id;"
                  "422=note_id 为空,或所有可选字段一个都没给,或只给了 related_counselor 但"
                  "推导不出任何公开推介笔记(以上三种这次编辑都什么都不会改),"
                  "或 collection_id 与 remove_collection_id 同时给(加入与移出语义相反),"
                  "或 set_original_declaration 传了 false(只支持开启);"
                  "或 topics 给了却全是空白(补话题至少要有一个非空话题名);"
                  "422 还包括编辑字段自身不合法:title 显示长度 >20 / content 为空串或 >900 / "
                  "给了图片操作却没给 expected_image_count / remove 下标重复或越界 / "
                  "删完原图剩 <1 / 改完总数 >18 / add_images 落盘失败 / "
                  "台账 note_type 不是图文(视频笔记与 null 一律拒绝);"
                  "422 还包括 cover 自身不合法:扩展名不在白名单 / 文件不存在 / "
                  "**台账 note_type 不是视频**(图文笔记与 null 一律拒绝,图文的封面是第一张图);"
                  "带 cover 时台账查无此 note_id 同样 404;"
                  "429=运营者未完成任务配额已满。**以上失败都在建 job 前**,一步都没做,"
                  "笔记原样未动",
        "notes": "异步契约:起后台浏览器深链进笔记更新页 → 设置组件 → 点发布 → **重进页面"
                 "逐项回读**(约 2-4 分钟);拿 job_id 后每 5-10s 轮询 "
                 "GET /api/note-components/{job_id}。"
                 "⚠️ 四条硬约束:"
                 "① **非幂等,失败不自动重跑**——提交是**全量覆盖**语义的一次真发布,"
                 "重跑就是再覆盖一次;看到 error/unknown 必须先去笔记里核对当前状态再决定;"
                 "② **关联活动会改写正文**——平台会把该活动的话题**追加**到正文末尾"
                 "(注意话题名 ≠ 活动名,只能事后读),结果里 topics_injected / body_appended "
                 "如实给出;活动是互斥单选,换活动会取消旧的但**旧话题不回收**,所以别反复换;"
                 "③ **权限保全**——编辑前后各读一次可见性档位,提交前不符立刻中止(一次发布"
                 "都不点),提交后若发现被改会自动改回并在结果里告警(permission_* 字段);"
                 "④ **部分生效是常态**——私密笔记的合集绑定会被平台静默丢弃(照返成功),"
                 "所以 done 只在全部回读确认后才给,别拿 202 或 'no error' 当成功凭据。"
                 "**合集为什么要你传 collection_name**:服务端并非不想自己查——合集的 id→名"
                 "映射只在合集弹层打开时才由平台接口下发,而笔记已在某合集时那个弹层根本打不开,"
                 "服务端手里多半没有映射。所以「已选态」下能不能判出「已选的就是目标」,"
                 "**取决于你传没传 collection_name**:传了就是 skipped(幂等成功,零点击),"
                 "不传只能报 collection_chosen_unverifiable —— 那不是失败,是「没法确认」,"
                 "reason 里带着实读到的合集名,名字对得上就视为已挂,"
                 "可用 note-component-reads 复核。"
                 "**移出合集(remove_collection_id)另有三条**(2026-08-07 上线):"
                 "① **幂等且零风险**——笔记本就不在该合集时 skipped、**一次发布都不点**,"
                 "所以拿它扫一批笔记做清理是安全的,重跑也安全;真移出时才走一次正常提交;"
                 "② **必须带 remove_collection_name**——比对不上一律拒绝动手,理由见参数说明;"
                 "③ **确认弹窗尚未取证**:点移除的 × 之后若平台弹出任何弹窗,系统**绝不盲点**,"
                 "整单中止(不提交)并把弹窗原文放进 error(collection_remove_unknown_modal);"
                 "看到它请把原文回报给我们补取证,别自行重试。"
                 "**补录原创声明(set_original_declaration)另有五条**(2026-08-08 上线,"
                 "为 08-05~08-07 那批漏标的笔记补标):"
                 "① **只支持开启**——传 false 直接 422,不静默忽略;"
                 "② **幂等 skipped**——进编辑页先读当前开关态,已是开态就 status='skipped'、"
                 "applied.original_declaration=true 且**零点击**,批量重跑安全;"
                 "③ **零改动不提交**——这次只请求了补声明且落了 skipped、其余组件/编辑字段都"
                 "没给时,**一次发布都不点**(submitted=false),上百次重跑不会变成上百次真提交;"
                 "④ **走的是与发布链完全同一段协议弹窗逻辑**——补声明调用的就是发布链那个"
                 "apply_original_declaration(handle_consent_modal=true),08-07 那个根因修复"
                 "(拟人点击的随机偏移有约 40% 概率落在《原创声明须知》超链接上、被链接吃掉"
                 "事件所以勾不上;改为精确点 16×16 的复选框方块)在该函数**内部**,"
                 "编辑链共用它 = 同一个修复自动覆盖,系统里没有第二份协议弹窗实现;"
                 "⑤ **回读判据 = 重进编辑页把开关 checked 再读一遍**,三态如实给"
                 "(true/false/null)。⚠️ 平台是否在编辑页回显「已声明」态**尚无实测证据**,"
                 "若它不回显,这里会把真补上的笔记报成 false —— **首批先跑 1-2 篇,人工到"
                 "笔记里确认原创标记真的出现了再放量**,别看到 false 就反复重跑"
                 "(每次重跑都是一次真提交)。"
                 "**改封面(cover)另有四条**(2026-08-08 上线,仅视频笔记):"
                 "① **只对视频笔记**——图文笔记的封面就是第一张图,传 cover 直接 422,"
                 "别拿这条去改图文;"
                 "② **幂等 skipped**——进更新页先读封面区,已经是自定义封面(不是平台自动"
                 "截的首帧)就 status='skipped'、**零点击**、且若只请求了它则**一次发布都不点**;"
                 "③ **回读判据是封面区真的变了**——编辑器内看「设置封面」弹窗关掉后封面缩略图"
                 "换没换,提交后重进页面再确认平台侧不再是自动首帧态;点了不等于成了,"
                 "所以 applied.cover 照样是 true/false/null 三态;"
                 "④ **绝不碰**「智能推荐封面」的候选图与「PK封面」开关(点它们=换成平台的图"
                 "而不是你给的封面,是比失败更难发现的静默错误);定位不到入口时带**当场取证**"
                 "(封面区 outerHTML)报 error,绝不静默假装换过。"
                 "**补挂话题(topics)另有四条**(2026-08-08 上线,存量视频笔记补话题空置):"
                 "① **追加不替换**——先读现有话题算差集,只补差集、去重、总数 >10 截断"
                 "(截掉的进 topics_truncated 如实说明留了哪些);验收:已有 1 个补 4 个 → 结果 "
                 "5 个、原 1 个保留;② **回读判据是平台实况**——提交后重进页面读正文里的话题实体,"
                 "applied.topics 反映真挂上的全量话题(不是点了就算,发布链的话题正是乐观态踩的雷);"
                 "③ **逐个独立不连坐**——某个话题平台没有/下拉没弹 → 它进 failed[] 带当场证据、"
                 "被 Escape+回删绝不留残缺文本,其余话题照常挂上;④ **幂等零提交**——请求的话题"
                 "全已挂 → 零点击零提交(存量批量补齐时对已达标的笔记跑这条是安全的)。"
                 "话题是追加(非破坏性)**不弃提交**:某个没挂上不影响其余组件。"
                 "⚠️ **改封面这条链尚未跑过真号 e2e**(选择器由 2026-08-08 真号只读取证锁定,"
                 "但真上传+提交那一段没实跑过):**首批先跑 1-2 篇、到笔记里人工确认封面真的"
                 "换了再放量**,看到 false 别直接重跑(每次重跑都是一次真提交)。"
                 "**批量补录的节奏**:本端点起真浏览器会话,受**同账号每小时会话帽**限制"
                 "(系统层 4、运营发起 12,见 queue.detail.kind_of_cap);所以跨多个账号的"
                 "补录**按号分散比堆在一个号上快得多**,同号内则本就严格串行"
                 "(per-account 锁)。看到 202 后的轮询里"
                 "**status=queued 是正常排队不是失败**——queue 段会告诉你排第几、大概何时"
                 "有额度,**照它等就行,千万别重试**(重试只会把队排得更长)。"
                 "**引用推导仅在显式引用意图时进行**(2026-08-04 收口):只传 "
                 "collection_id/activity_id/编辑项时**绝不**顺带推导出引用——本端点对已"
                 "发布笔记只做被请求的事(发布端点 POST /api/publish-jobs 的隐式推导是"
                 "另一条语义,保持不变)。传了 related_counselor(且没显式给 quoted_note_id)"
                 "才推导(标题从台账现查):① 这篇标题形如「X咨询师-姓名，…」= 它本身就是"
                 "咨询师推介笔记 → 引用「接待员联系方式」那篇(**这条最先判**,所以推介笔记"
                 "不会引用自己);② 否则引用**本账号**该咨询师的公开推介笔记;③ 推不出 → "
                 "留空不引用。**只引用 permission_code=0 的公开笔记,推不出来一律留空绝不"
                 "猜**。⚠️ **只会引用本账号自己的咨询师推介笔记**:每个账号背后是不同运营,"
                 "从该账号来的客户算其 KPI,跨账号引用等于抢同事绩效,故本账号没有该咨询师"
                 "的公开推介笔记时**留空,绝不跨账号兜底**;唯一例外是接待员联系方式那篇"
                 "(含二维码有违规风险,集中在单一账号,由服务端配置指定,**未配置时规则①"
                 "同样留空**)。"
                 "── 编辑标题/正文/图片(title / content / add_images / "
                 "remove_image_indexes)另有四条必读:"
                 "① **正文替换会丢既有话题实体**——旧正文里的 #xx[话题]# 实体在整体替换后"
                 "不复存在,本期**不做重建**,结果里 topics_dropped 如实列出丢了哪些;"
                 "要保话题只能自己在新 content 里重新安排;"
                 "② **aborted_before_submit=true 时笔记原样未动,可安全重试**——它表示"
                 "标题/正文/图片这类**破坏性编辑步**中途失败,系统主动放弃了提交(编辑器里"
                 "的改动不落库);**其余失败(尤其提交后)必须先人工核对笔记当前状态再决定**;"
                 "③ **图片下标以发布态图序为准**,且必须同时给 expected_image_count;"
                 "页面实数与它对不上 → 整个图片操作拒绝,一次点击都不发生(文本/组件照常);"
                 "④ **台账自动回写**——title / content 回读确认生效后,自动把**回读到的平台"
                 "真值**(不是你传的值,平台可能有服务端改写)写回 published_notes 的 "
                 "title / content_text,结果里 ledger_synced 给回写结果;图片不回写"
                 "(台账不存图列表)。",
    },
    {
        "method": "GET", "path": "/api/note-components/{job_id}",
        "queue": QUEUE_MANIFEST_NOTE,
        "summary": "轮询笔记编辑结果(三组件 + 标题/正文/图片,逐项生效情况)",
        "admin_only": False, "params": {"job_id": "path,str"},
        "returns": "{status, result_status?, applied?:{collection/collection_remove/quote/"
                   "activity/original_declaration/cover/title/content/image_add/image_remove: "
                   "true|false|null, **topics: [平台实况话题名] | null**}, "
                   "failed?:[{component,reason}], submitted?, permission_before?, "
                   "permission_after?, permission_preserved?, permission_restored?, "
                   "body_appended?, topics_injected?, topics_dropped?, "
                   "topics_existing?, topics_added?, topics_truncated?, topics_failed?, "
                   "images_before?, images_after?, ledger_synced?, aborted_before_submit?, reason?}",
        "errors": "403=无该号授权;404=job_id 不存在",
        "notes": "status 五态:queued / running / done / error(附 reason)/ "
                 "**unknown(执行进程中断,改没改成未知)**。"
                 "done 时看 **result_status**:`done`=请求的组件**全部**回读确认生效;"
                 "`partially_applied`=只有一部分生效(哪项没成看 failed)。"
                 "applied 的每个值是三态:true=回读确认生效;false=回读确认**没**生效;"
                 "null=没能回读(未确认)——**只有 true 才算数**,这条产品线的失败是静默的。"
                 "applied.collection_remove 同款三态,但它的 true 有两种来路:真移出后重进"
                 "页面确认合集区已不含目标(submitted=true),或**笔记本就不在该合集**"
                 "(幂等零点击,submitted=false)——两者都是「现在它确实不在那个合集里」。"
                 "applied.original_declaration 同款三态,true 也有两种来路:补上后重进页面"
                 "读到开关是开态(submitted=true),或**本就已声明**(幂等零点击,"
                 "submitted=false)。⚠️ 平台是否在编辑页回显已声明态尚无实测证据,"
                 "**首批先跑 1-2 篇人工核对原创标记再放量**,看到 false 别直接重跑"
                 "(每次重跑都是一次真提交);components.original_declaration.reason / "
                 "observed 给出当场取证(勾没勾上、按钮解没解禁、弹窗关没关)。"
                 "applied.cover 同款三态,true 也有两种来路:换上后重进页面确认封面区不再是"
                 "平台自动首帧(submitted=true),或**本就已是自定义封面**(幂等零点击,"
                 "submitted=false)。⚠️ 改封面尚未跑过真号 e2e,**首批先跑 1-2 篇人工核对"
                 "封面真的换了再放量**;components.cover.reason / observed 给出当场取证"
                 "(封面区状态、弹窗开没开、file input 挂没挂、封面区 outerHTML)。"
                 "一项都没生效时整体落 error(reason 里带 note_components_all_failed)。"
                 "submitted=有没有观察到那次提交请求的响应;"
                 "permission_preserved=false 表示可见性档位被这次提交改动了(严重),"
                 "permission_restored 给自动改回的结果,ok=false 时**必须人工处理**;"
                 "topics_injected 是关联活动往正文追加的话题名(可能与活动名不同)。"
                 "── 编辑相关的键:applied.title(回读标题全等)/ applied.content(回读正文"
                 "**以目标正文开头**,不是全等——关联活动会往末尾追话题)/ applied.image_add / "
                 "applied.image_remove(同一条图数等式,分两键报以便定位是哪半失败);"
                 "topics_dropped=正文替换丢掉的旧话题实体名;images_before / images_after="
                 "编辑前留底与提交后回读的图数;ledger_synced=台账 title/content_text 回写"
                 "结果(false 只表示台账没同步上,平台侧改动照样是真的);"
                 "**aborted_before_submit=true 是特殊终态**:破坏性编辑步失败导致主动放弃"
                 "提交,**笔记原样未动,可直接安全重试**——与「提交了但只成一半」完全不同,"
                 "别按同一套处理。"
                 "── 补话题(topics)相关的键:**applied.topics 是列表不是三态 bool**——它是"
                 "提交后回读到的**平台实况全量话题名**(null=没能回读,未确认);topics_existing="
                 "补之前笔记已有的话题;topics_added=本次 to_add 里回读确认真挂上的;"
                 "topics_truncated=因超 10 上限没补的;topics_failed=[{component:`topic:话题名`,"
                 "reason}] 逐个失败原因(下拉没中/回读没确认/超上限截断),**不连坐**其余话题也"
                 "不连坐其余组件。验收「补 4 个后结果 5 个、原 1 个保留」看的就是 applied.topics 的长度。",
    },
]


def _parse_keywords(raw: str | None) -> tuple:
    """query 里的 keywords → 词表元组:省略=默认心理学词表;``*``/空=不过滤(空元组)。"""
    if raw is None:
        return tuple(DEFAULT_ACTIVITY_KEYWORDS)
    text = raw.strip()
    if not text or text == _NO_FILTER:
        return ()
    parts = [p.strip() for p in text.replace("，", ",").split(",")]
    return tuple(p for p in parts if p)


async def _prepare_catalog(account_id: int, note_id: str | None) -> str:
    """两个 GET 的公共前置:鉴权 + 配额闸 + 确定要打开哪篇笔记;返回最终 note_id。

    过配额闸的理由与其它浏览器端点一致:这两个 GET 也要起一个真 camoufox 会话,
    配额闸护的正是这条浏览器流水线。
    """
    operator = current_operator()
    await assert_operator_quota(operator)
    async with get_session() as session:
        await assert_account_access(operator, account_id, session)
        if await session.get(XhsAccount, account_id) is None:
            raise NotFoundError(f"账号 {account_id} 不存在")
    resolved = (note_id or "").strip()
    if resolved:
        return resolved
    fallback = await note_components.default_catalog_note_id(account_id)
    if not fallback:
        raise ValueError(
            f"账号 {account_id} 的台账里没有任何带 note_id 的笔记,给不出打开编辑器的落脚点;"
            f"请显式传 note_id(合集/活动列表只有笔记编辑器页会返回)"
        )
    return fallback


def _unwrap(catalog: dict) -> dict:
    """目录读取结果:带 ``error`` 键即浏览器侧失败 → 502(不静默返回空列表)。"""
    if "error" in catalog:
        raise HTTPException(status_code=502, detail=catalog["error"])
    return catalog


@router.get("/api/accounts/{account_id}/collections")
async def list_collections_endpoint(
    account_id: int, note_id: str | None = Query(default=None)
) -> dict:
    """读该号的合集列表(同步阻塞,内部起一次只读浏览器会话)。"""
    resolved = await _prepare_catalog(account_id, note_id)
    catalog = _unwrap(await note_components.read_catalog(account_id, resolved))
    return {"collections": catalog.get("collections", []), "note_id": resolved}


@router.get("/api/accounts/{account_id}/activities")
async def list_activities_endpoint(
    account_id: int,
    note_id: str | None = Query(default=None),
    keywords: str | None = Query(default=None),
) -> dict:
    """读平台活动列表并按关键词筛选(同步阻塞,内部起一次只读浏览器会话)。"""
    resolved = await _prepare_catalog(account_id, note_id)
    words = _parse_keywords(keywords)
    catalog = _unwrap(await note_components.read_catalog(account_id, resolved))
    activities = catalog.get("activities", [])
    return {
        "activities": filter_activities(activities, words),
        "total": len(activities),
        "keywords": list(words),
        "note_id": resolved,
    }


class NoteComponentsRequest(BaseModel):
    """三组件设置请求体。三个组件 id 全可选,但**至少给一个**。

    id 一律用字符串:平台侧 ``ACTIVITY_COMPONENT.bizId`` 实测是字符串(``"43561"``),
    合集 id 是 24 位 hex,笔记 id 也是 hex —— 统一字符串,别让 JSON 数字把前导 0 吃掉。
    """

    note_id: str = Field(
        min_length=1, max_length=64, description="要编辑的笔记平台 id(深链定位)"
    )
    collection_id: str | None = Field(default=None, max_length=64)
    collection_name: str | None = Field(
        default=None,
        description="合集名(可选,**辅助字段**):笔记已在某合集里时,「加入合集」按钮不渲染,"
                    "浏览器层只能靠名字确认已选的就是目标——带上它,已在目标合集会得 "
                    "skipped(视为成功)而非报错;不带且无法确认时报 "
                    "collection_chosen_unverifiable。单独出现不构成组件请求",
    )
    remove_collection_id: str | None = Field(
        default=None, max_length=64,
        description="把这篇笔记**移出**该合集(与 collection_id 加入对称,幂等:本就不在 → "
                    "skipped 不算失败);与 collection_id **同时传 → 422**,换合集请分两次请求",
    )
    remove_collection_name: str | None = Field(
        default=None, max_length=64,
        description="要移出的那个合集的名字,**强烈建议每次带 remove_collection_id 时都传**:"
                    "浏览器层靠它确认「当前所在合集就是目标」,比对不上**绝不点移除**"
                    "(移出是破坏性操作,点错等于把笔记从正确的合集里摘出来);"
                    "单独出现不构成组件请求",
    )
    quoted_note_id: str | None = Field(default=None, max_length=64)
    activity_id: str | None = Field(default=None, max_length=64)
    related_counselor: str | None = Field(
        default=None, max_length=32,
        description="这篇笔记推介哪位咨询师(姓名),作为 quoted_note_id 的替代;"
                    "两者都给时以显式 quoted_note_id 为准",
    )
    set_original_declaration: bool | None = Field(
        default=None,
        description="给这篇**补录原创声明**(2026-08-08 上线,为 08-05~08-07 那批漏标的笔记"
                    "补标)。**只支持开启**:传 true 才动手,传 **false 直接 422** —— 平台侧"
                    "能不能关没取证,而关闭是破坏性动作,不在本期范围,静默忽略等于假成功。"
                    "**幂等**:进页面先读当前态,已是开态 → status='skipped' 且**零点击**;"
                    "若这次请求只有它一件事且落了 skipped,**一次发布都不点**",
    )
    # ── 已发布笔记编辑(设计 docs/design/2026-08-03-note-editing-design.md 3.1)──
    # 五个字段全可选,与三组件同一次请求、同一次提交共存。
    title: str | None = Field(
        default=None,
        description="整体替换标题;None=不改;**空串 \"\" = 清空标题**(平台侧合法显式值,"
                    "与 None 语义不同);显示长度 >20 直接 422,**绝不静默截断**",
    )
    content: str | None = Field(
        default=None, min_length=1, max_length=XHS_MAX_BODY_LENGTH,
        description="整体替换正文;None=不改;**不支持清空**(空串 422,收益趋零风险不明)",
    )
    add_images: list | None = Field(
        default=None, min_length=1, max_length=_MAX_IMAGES,
        description="追加到现有图序**末尾**的图片;每项三形态之一(http URL / base64 / "
                    "本服务 /uploads 路径),与发布链路同款",
    )
    remove_image_indexes: list[int] | None = Field(
        default=None, min_length=1,
        description="要删除的图片下标,**1-based 且按发布态图序**;不许有重复项",
    )
    expected_image_count: int | None = Field(
        default=None, ge=1, le=_MAX_IMAGES,
        description="调用方声明「我认为这篇现在有几张图」;给了 add_images / "
                    "remove_image_indexes 时**必填**。浏览器层实数不符 → 图片操作整体拒绝,"
                    "一次点击都不发生(删除类操作的第一道防呆)",
    )
    cover: str | None = Field(
        default=None,
        description="给这篇换**自定义封面**的图片路径(服务器侧本地路径,"
                    f"扩展名限 {'/'.join(XHS_COVER_EXTENSIONS)},文件不存在直接 422)。"
                    "**只对视频笔记有效**:图文笔记的封面就是第一张图(与发布端点同口径),"
                    "传了直接 422。**幂等**:这篇已经是自定义封面 → status='skipped'、"
                    "applied.cover=true 且**零点击**",
    )
    topics: list[str] | None = Field(
        default=None,
        description="给这篇**补挂话题**(2026-08-08,存量视频笔记话题空置的补救)。语义是"
                    "**追加**不是替换:传入话题**追加**到现有话题、去重、总数 >10 截断"
                    f"(小红书单篇 ≤{XHS_MAX_TOPICS} 个)。全量替换在已有话题的笔记上太危险"
                    "(一次手滑清空别人的话题),故只追加。回读判据是**平台实况**:提交后重进"
                    "页面读正文里的话题实体,applied.topics 反映真挂上的全量话题(不是点了就算);"
                    "逐个话题独立成败,失败进 topics_failed 带原因、不连坐其余。请求的全已挂 → "
                    "零点击零提交(幂等)。带 # 或不带都行,内部规整去重",
    )

    @model_validator(mode="after")
    def _original_declaration_is_open_only(self):
        """``set_original_declaration=false`` → 422,**不静默当成"没请求"**。

        本期只做**开启**:平台侧能不能关掉原创声明一次都没取证过,而关闭是破坏性动作
        (声明撤回可能影响流量权益,且不可逆性未知)。调用方显式传 false 是在表达一个
        我们兑现不了的意图 —— 静默忽略它就是这条产品线最忌讳的假成功,所以当场打回。
        """
        if self.set_original_declaration is False:
            raise ValueError(
                "set_original_declaration 只支持 true(开启):关闭原创声明的平台行为"
                "未取证且是破坏性动作,本期不做;不想补声明就整个字段别传"
            )
        return self

    @model_validator(mode="after")
    def _topics_not_all_blank(self):
        """``topics`` 给了就得至少有一个非空话题 —— 全空白/空列表是"表达了意图却给不出内容"。

        静默忽略它就是这条产品线最忌讳的假成功(调用方以为补了话题,其实一个都没提交);
        当场 422 打回,和请求体其它字段同样的失败语义。不去重不截断(那是追加语义的浏览器层
        逻辑,``plan_topic_appends`` 按现有话题现算),这里只挡"整列都是空白"。
        """
        if self.topics is not None and not any((t or "").strip() for t in self.topics):
            raise ValueError(
                "topics 给了却全是空白 —— 补挂话题至少要有一个非空话题名;"
                "不想补话题就整个字段别传"
            )
        return self

    @model_validator(mode="after")
    def _require_one_component(self):
        """一个组件 / 一项编辑都不给 → 422(``related_counselor`` 也算给了,它会推导出引用)。

        这次编辑什么都不会改,却要真提交一次**全量覆盖**的发布 —— 纯风险零收益,
        在入口就拦掉,别让它排队两分钟后才在浏览器层失败。

        ``title=""`` 是**给了**(清空标题是明确意图),故这里按 ``is not None`` 判而不是
        按真值判 —— 与 ``content`` / 三组件的判法不同,是有意的。

        注意这里只做"意图上给没给",``related_counselor`` 推不出笔记的情况在端点里
        推导完再拦一次(见 ``start_note_components_endpoint``)。
        """
        if not any(
            (
                self.collection_id,
                self.remove_collection_id,
                self.quoted_note_id,
                self.activity_id,
                self.related_counselor,
                self.set_original_declaration,
                self.title is not None,
                self.content is not None,
                self.add_images,
                self.remove_image_indexes,
                self.cover,
                self.topics,
            )
        ):
            raise ValueError(
                "collection_id / remove_collection_id / quoted_note_id / activity_id / "
                "related_counselor / set_original_declaration / title / content / "
                "add_images / remove_image_indexes / cover / topics 至少要给一个"
            )
        return self

    @model_validator(mode="after")
    def _validate_cover(self):
        """封面图:扩展名白名单 + **建 job 前**确认文件真在盘上,不合规一律 422。

        存在性放在入口判,是为了别让它排队两分钟、开完浏览器、进了更新页才在
        ``set_input_files`` 那一行撞墙 —— 那时已经白烧一次真会话(还占着该号的会话帽)。
        """
        if self.cover is None:
            return self
        path = Path(self.cover)
        if not cover_ext_allowed(self.cover):
            raise ValueError(
                f"cover 扩展名 {path.suffix!r} 不在白名单 {'/'.join(XHS_COVER_EXTENSIONS)} 内"
                f"(平台封面上传 accept 的就是这几种图)"
            )
        if not path.is_file():
            raise ValueError(
                f"cover 指向的文件不存在:{self.cover};"
                f"这是服务器侧本地路径,请先把图落到本服务能读到的位置"
            )
        return self

    @model_validator(mode="after")
    def _collection_join_and_remove_are_exclusive(self):
        """加入与移出**不许同一次请求同时给** → 422。

        「换合集」在页面上是不是原子的、点 chip 主体会不会重新弹出下拉,都还没取证
        (设计 docs/design/2026-08-07-collection-remove-design.md §1.1)。语义歧义(先移后加?原子换?)不该由服务端替调用方静默选一个 ——
        真要换合集就分两次请求:先移出旧的,确认生效,再加入新的。
        """
        if self.collection_id and self.remove_collection_id:
            raise ValueError(
                "collection_id 与 remove_collection_id 不能同时给(加入与移出语义相反,"
                "「换合集」的页面交互尚未取证);请分两次请求:先移出旧合集,回读确认后"
                "再加入新合集"
            )
        return self

    @model_validator(mode="after")
    def _validate_note_edits(self):
        """编辑字段逐条校验(设计 3.1 表)——**一律拒绝,绝不静默改写调用方给的值**。

        发布链路对超长标题做硬截断,这里恰恰相反:编辑场景调用方给的是**精确意图**,
        截断 = 替他改错内容,而这是一次全量覆盖提交,改错就得人工去改回来。同理重复
        下标不去重、越界不夹取,一律 422 打回。
        """
        if (
            self.title is not None
            and get_display_length(self.title) > XHS_MAX_TITLE_DISPLAY
        ):
            raise ValueError(
                f"title 显示长度 {get_display_length(self.title)} 超过 "
                f"{XHS_MAX_TITLE_DISPLAY}(小红书标题上限);编辑场景不做截断,请自行改短"
            )

        removes = self.remove_image_indexes or []
        images_requested = bool(self.add_images) or bool(removes)
        if images_requested and self.expected_image_count is None:
            raise ValueError(
                "给了 add_images / remove_image_indexes 就必须给 expected_image_count"
                "(你认为这篇现在有几张图);页面实数与它不符时图片操作整体拒绝,"
                "这是删除类操作的第一道防呆"
            )
        if len(set(removes)) != len(removes):
            raise ValueError(
                f"remove_image_indexes 有重复下标 {sorted(removes)};"
                f"不去重是有意的 —— 删除下标写重了多半是调用方算错了图序"
            )
        if removes:
            expected = self.expected_image_count
            bad = [i for i in removes if not 1 <= i <= expected]
            if bad:
                raise ValueError(
                    f"remove_image_indexes 下标 {bad} 越界:下标 **1-based**,"
                    f"取值范围 1..{expected}(expected_image_count)"
                )
            if expected - len(removes) < 1:
                raise ValueError(
                    f"删除 {len(removes)} 张后原图只剩 {expected - len(removes)} 张:"
                    f"图文笔记 0 图状态未知且不可逆,**剩余必须 ≥1**;"
                    f"且不许拿 add_images 凑数(追加可能失败,失败后就是删光)"
                )
        if images_requested:
            total = self.expected_image_count + len(self.add_images or []) - len(removes)
            if total > _MAX_IMAGES:
                raise ValueError(
                    f"改完共 {total} 张,超过图文笔记上限 {_MAX_IMAGES} 张"
                )
        return self

    def note_edits(self) -> dict | None:
        """本次请求里的编辑意图 → payload 片段;一项都没给返回 ``None``。

        返回 ``None`` 时 payload 与老版本**逐字节一致**(老调用方语义零变化);给了任一项
        才多出这五个键,五个一起给全(哪怕值是 None)—— 事后翻台账查"当时改了什么"要能
        一眼看到完整意图,而不是靠键在不在猜。``add_images`` 的落盘由端点另行替换。
        """
        if not any(
            (
                self.title is not None,
                self.content is not None,
                self.add_images,
                self.remove_image_indexes,
            )
        ):
            return None
        return {
            "title": self.title,
            "content": self.content,
            "add_images": list(self.add_images or []),
            "remove_image_indexes": list(self.remove_image_indexes or []),
            "expected_image_count": self.expected_image_count,
        }


async def _assert_editable_note(session, account_id: int, note_id: str) -> None:
    """请求带文本/图片编辑时的台账前置校验:这篇必须在台账里、且是**图文**笔记。

    编辑走的是更新页深链,只对图文(``noteType=normal``)验证过;视频笔记的更新页结构
    完全没碰过,而这是一次全量覆盖提交 —— 与其让它排队两分钟后在浏览器层撞墙,不如在
    入口按台账拦掉(设计 3.1 服务层前置校验)。

    只对**带编辑字段**的请求生效:纯三组件请求的台账要求与老版本一字不变。
    """
    row = await session.scalar(
        select(PublishedNote).where(
            PublishedNote.account_id == account_id,
            PublishedNote.note_id == note_id,
        )
    )
    if row is None:
        raise NotFoundError(
            f"账号 {account_id} 的台账里没有 note_id={note_id} 的笔记;"
            f"改标题/正文/图片要按台账认这篇是不是图文笔记,查不到就不编辑"
        )
    if row.note_type not in _IMAGE_NOTE_TYPES:
        raise HTTPException(
            status_code=422,
            detail=f"note_id={note_id} 的台账 note_type={row.note_type!r},不是图文笔记"
                   f"(图文为 {'/'.join(_IMAGE_NOTE_TYPES)});"
                   f"文本/图片编辑只对图文笔记验证过,视频笔记与未知类型一律拒绝。"
                   f"note_type 为 null 说明台账还没同步到这篇的平台类型,先同步再编辑",
        )


async def _assert_video_note(session, account_id: int, note_id: str) -> None:
    """请求带 ``cover`` 时的台账前置校验:这篇必须在台账里、且是**视频**笔记。

    只有视频笔记的更新页才有封面区(真号取证:``.publish-page-content-cover`` 里那个
    「默认截取第一帧作为封面」)。图文笔记的封面就是第一张图,改它得走 add/remove_images ——
    与其让调用方排队两分钟后拿到一句 ``cover_section_not_found``,不如在入口按台账拦掉。
    """
    row = await session.scalar(
        select(PublishedNote).where(
            PublishedNote.account_id == account_id,
            PublishedNote.note_id == note_id,
        )
    )
    if row is None:
        raise NotFoundError(
            f"账号 {account_id} 的台账里没有 note_id={note_id} 的笔记;"
            f"改封面要按台账认这篇是不是视频笔记,查不到就不改"
        )
    if row.note_type not in _VIDEO_NOTE_TYPES:
        raise HTTPException(
            status_code=422,
            detail=f"note_id={note_id} 的台账 note_type={row.note_type!r},不是视频笔记"
                   f"(视频为 {'/'.join(_VIDEO_NOTE_TYPES)});**图文笔记的封面就是第一张图**"
                   f"(与发布端点同口径),要换封面请用 remove_image_indexes + add_images 换掉"
                   f"第一张。note_type 为 null 说明台账还没同步到这篇的平台类型,先同步再改",
        )


async def _materialize_edit_images(edits: dict) -> None:
    """把 ``add_images`` 就地换成本地文件路径(**建 job 前**落盘,设计 3.1)。

    坏图 / 拉不下来 → 422 当场打回:别让它排队两分钟后才在浏览器层失败(与"入口拦
    无意义提交"同一纪律)。下载/解码是阻塞 I/O,下沉线程,不卡事件循环。
    """
    raw = edits.get("add_images") or []
    if not raw:
        return
    workdir = Path(settings.UPLOAD_DIR) / f"note_edit_{uuid4().hex}"
    try:
        paths = await asyncio.to_thread(materialize_images, raw, workdir)
    except Exception as exc:  # noqa: BLE001 — 任何落盘失败都译成 422,原因原样给调用方
        shutil.rmtree(workdir, ignore_errors=True)
        raise HTTPException(
            status_code=422,
            detail=f"add_images 落盘失败,这次编辑一步都没做:{exc}",
        ) from exc
    edits["add_images"] = [str(p) for p in paths]


@router.post("/api/accounts/{account_id}/note-components", status_code=202)
async def start_note_components_endpoint(
    account_id: int, payload: NoteComponentsRequest
) -> dict:
    """异步触发一次三组件设置 / 已发布笔记编辑,立即返回 job_id。"""
    operator = current_operator()
    # 运营配额闸:未完成任务达上限 → 429(admin 豁免),不建任务。
    await assert_operator_quota(operator)
    edits = payload.note_edits()
    async with get_session() as session:
        await assert_account_access(operator, account_id, session)
        if await session.get(XhsAccount, account_id) is None:
            raise NotFoundError(f"账号 {account_id} 不存在")
        if edits is not None:
            await _assert_editable_note(session, account_id, payload.note_id)
        if payload.cover:
            await _assert_video_note(session, account_id, payload.note_id)
        # 引用哪篇:显式 quoted_note_id 优先;推导**仅在显式给了 related_counselor 时**
        # 进行(2026-08-04 收口,运营清单 P1-2):编辑路径的语义是"对已发布笔记只做被
        # 请求的事"——只传 collection_id/activity_id/编辑项时绝不顺带推导出一个引用
        # (此前规则 3 会给推介笔记自动挂跨账号的接待员引用,运营完全没请求过)。
        # 发布路径(publish_rest)的隐式推导是"按业务规则产出完整笔记"的设计内业务,
        # 保持不变——两条路径触发条件刻意不同,别"顺手统一"。
        quoted_note_id = payload.quoted_note_id
        if quoted_note_id is None and (payload.related_counselor or "").strip():
            quoted_note_id = await counselor_quote.resolve_for_published_note(
                session, account_id, payload.note_id, payload.related_counselor
            )
    # 推导完还是一个组件都没有(且没给任何编辑项)→ 422。走到这里说明调用方只给了
    # related_counselor,而它没能落到任何一篇公开笔记上(查不到就留空是硬纪律)。此时这次
    # 编辑什么都不会改,却要真提交一次全量覆盖的发布,一样是纯风险零收益,和请求体校验
    # 那一关同样拦掉。
    if edits is None and not any(
        (payload.collection_id, payload.remove_collection_id, quoted_note_id,
         payload.activity_id, payload.set_original_declaration, payload.cover,
         payload.topics)
    ):
        # 用 422 而不是裸 ValueError(→400):与请求体校验那一关同样的失败语义,
        # 调用方不该因为"拦在哪一层"看到两种状态码。
        raise HTTPException(
            status_code=422,
            detail=f"related_counselor={payload.related_counselor!r} 推导不出可引用的笔记,"
                   f"且没给其它组件 —— 这次编辑什么都不会改。可能原因:本账号台账里没有该"
                   f"咨询师的公开推介笔记(**不会**去别的账号借,那会抢走对方运营的绩效),"
                   f"或这篇本身是咨询师推介笔记而服务端还没配「接待员联系方式」笔记 id。"
                   f"服务端日志里 [counselor_quote] 那行给出了确切原因码",
        )
    # 图片落盘放在所有便宜校验之后:前面任何一关拦下都不该白下载一遍图。
    if edits is not None:
        await _materialize_edit_images(edits)
    job_id = note_components.start_components(
        account_id,
        payload.note_id,
        payload.collection_id,
        quoted_note_id,
        payload.activity_id,
        payload.related_counselor,
        collection_name=payload.collection_name,
        remove_collection_id=payload.remove_collection_id,
        remove_collection_name=payload.remove_collection_name,
        set_original_declaration=bool(payload.set_original_declaration),
        cover=payload.cover,
        topics=payload.topics,
        edits=edits,
    )
    return {"job_id": job_id, "status": "queued"}


@router.get("/api/note-components/{job_id}")
async def get_note_components_endpoint(job_id: str) -> dict:
    """轮询三组件设置结果:queued / running / done(逐项生效情况)/ error / unknown。

    done 时把服务侧的 ``status``(done=全部生效 / partially_applied=部分生效)另开一个键
    ``result_status`` 下发 —— 外层 status 是任务生命周期,内层是这次到底改成了几项,
    合成一个键会让"只成了一半"被读成"全成了"。

    **error 也要下发逐项详情**(2026-08-03 运营上报):此前这段只在 ``status == "done"``
    时下发 ``failed`` / ``applied``,而"三组件一项都没设上"恰恰以 ``error`` 收尾 ——
    于是服务层明明写好了原因(``quote_card_title_mismatch: 第 6 张卡……与接口不一致``),
    调用方却一个字都拿不到,只能靠换外部变量盲测(运营实际连测三次全无信息)。

    **失败时的逐项原因比成功时更值钱**,把它藏起来是这个接口最不该有的行为。
    """
    row = await load_job(job_id, "note_components", "job_id")
    view = await base_view(row)
    result = row.get("result") or {}
    # 终态(done / error)都下发;queued / running 时 result 本就还没有内容
    if row["status"] in ("done", "error"):
        view["result_status"] = result.get("status")
        for key in (
            "applied", "failed", "submitted", "permission_before", "permission_after",
            "permission_preserved", "permission_restored", "body_appended",
            "topics_injected",
            # 编辑(标题/正文/图片)新增的结果键;applied 里的 title / content /
            # image_add / image_remove 走上面 applied 那一项原样透传,不必单列。
            "topics_dropped", "images_before", "images_after", "ledger_synced",
            "aborted_before_submit",
            # 补话题(2026-08-08):applied.topics 走上面 applied 那一项透传;这里补它的
            # 明细键。存量视频笔记补话题的回执靠它们(追加语义 + 平台实况 + 逐项失败)。
            "topics_existing", "topics_added", "topics_truncated", "topics_failed",
        ):
            if key in result:
                view[key] = result[key]
    return view
