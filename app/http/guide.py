"""GET /api/guide —— 统一指南:能力分组 + 变更记录 + 已知边界,一次调用全拿。

与 `GET /api/manifest` 的分工:manifest 是**端点级机器契约**(每个端点的
params/returns/errors 全文),guide 是它的**超集视角**——回答 manifest 回答不了的
三个问题:这些端点分别属于哪个能力域、最近改了什么、哪些地方现在还不能指望。

设计铁律(本文件存在的全部理由):三段内容一律用 Python 结构体承载,不写 markdown。
markdown 文档没人能测,写错了要等调用方撞墙才发现;结构体有 tests/test_guide.py
钉着——漏归组、引用不存在的端点、字段缺失、日期非法、倒序坏掉,全部在 CI 就红。
"""

import subprocess
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path

from fastapi import APIRouter

from app import __version__

router = APIRouter()

# guide 响应体自身的契约版本:响应结构(段名/字段名/字段语义)变了才 +1,
# 内容增删不动它。消费方据此判断自己的解析代码还能不能用。
GUIDE_CONTRACT_VERSION = 1

# 端点详情不在 guide 里复制一遍,只给指针——两处维护同一份长文案必然走向不一致。
_SEE = "端点完整 params/returns/errors 见 GET /api/manifest(本接口只给 method/path/summary)"

# ---------------------------------------------------------------------------
# 一、能力域分组
#
# 集中在这一处映射,不散落到各 router 文件:分组是**跨端点的视角**,拆到端点旁边
# 就没有任何一个地方能看出"有没有漏"。tests/test_guide.py 拿这里的路径集合与
# manifest 路径集合做双向全等——新端点没归组即报红,这是防漏归组的唯一机制。
# 粒度按 path(不按 method):同一路径上的不同方法必然属于同一能力域。
# ---------------------------------------------------------------------------

CAPABILITY_GROUPS = [
    {
        "key": "system",
        "title": "服务自描述",
        "summary": "接入第一站:验 key、拿契约、拿本指南。",
        "paths": [
            "/api/whoami",
            "/api/manifest",
            "/api/guide",
        ],
    },
    {
        "key": "account_login",
        "title": "账号与登录",
        "summary": "小红书账号的登录(靠人 + chrome 插件扫码)、cookie 灌入与活性检测。"
                   "服务端没有登录接口,登录动作必须由操作者本人在插件里完成。",
        "paths": [
            "/api/accounts",
            "/api/accounts/{account_id}",
            "/api/accounts/{account_id}/cookies",
            "/api/cookies/import",
            "/api/login/poll",
            "/api/extension",
            "/api/accounts/{account_id}/cookie-checks",
            "/api/cookie-checks/{check_id}",
        ],
    },
    {
        "key": "publish",
        "title": "发布",
        "summary": "发一条新笔记:图文 / 视频 / 播客三选一,异步入队后轮询终态;"
                   "含定时改期、取消、失败现场截图取证,以及发播客用的合集创建。",
        "paths": [
            "/api/publish-jobs",
            "/api/publish-jobs/{job_id}",
            "/api/publish-jobs/{job_id}/cancel",
            "/api/publish-jobs/{job_id}/artifacts",
            "/api/publish-jobs/{job_id}/artifacts/{name}",
            "/api/accounts/{account_id}/podcast-collections",
            "/api/podcast-collections/{job_id}",
            "/api/accounts/{account_id}/activities",
        ],
    },
    {
        "key": "published_notes",
        "title": "已发布笔记管理",
        "summary": "对**已经发出去**的笔记做事:台账与数据读取、正文/图片/三组件编辑、"
                   "可见性切换、删除、笔记合集的创建与清理。"
                   "写类操作会真起浏览器改动线上内容。",
        "paths": [
            "/api/accounts/{account_id}/note-exports",
            "/api/note-exports/{export_id}",
            "/api/accounts/{account_id}/note-deletions",
            "/api/note-deletions/{deletion_id}",
            "/api/accounts/{account_id}/note-trends",
            "/api/accounts/{account_id}/notes",
            "/api/accounts/{account_id}/published-notes",
            "/api/published-notes/{note_id}",
            "/api/accounts/{account_id}/note-ledger-syncs",
            "/api/note-ledger-syncs/{sync_id}",
            "/api/accounts/{account_id}/note-purpose-backfills",
            "/api/note-purpose-backfills/{backfill_id}",
            "/api/accounts/{account_id}/note-visibility-changes",
            "/api/note-visibility-changes/{change_id}",
            "/api/accounts/{account_id}/note-component-reads",
            "/api/note-component-reads/{job_id}",
            "/api/accounts/{account_id}/note-components",
            "/api/note-components/{job_id}",
            "/api/accounts/{account_id}/collections",
            "/api/accounts/{account_id}/note-collections",
            "/api/note-collections/{job_id}",
            "/api/accounts/{account_id}/collection-batches",
            "/api/collection-batches/{job_id}",
        ],
    },
    {
        "key": "interact",
        "title": "互动",
        "summary": "以自己的号对笔记做真实互动:发评论、历史笔记点赞收藏补量。"
                   "全部会在平台留下真实痕迹,且受同号会话频次总闸约束。",
        "paths": [
            "/api/accounts/{account_id}/note-comments",
            "/api/note-comments/{comment_id}",
            "/api/interaction-backfills",
            "/api/interaction-backfills/{job_id}",
        ],
    },
    {
        "key": "assets",
        "title": "素材:上传与生图",
        "summary": "发布前的素材准备:图片上传得直链、GB 级视频/音频分片上传得服务器路径、"
                   "一致性批量生图,以及已发布内容的归档库(跨账号复用文案与图)。",
        "paths": [
            "/api/uploads",
            "/api/uploads/images",
            "/api/uploads/media-sessions",
            "/api/uploads/media-sessions/{upload_id}/chunks/{index}",
            "/api/uploads/media-sessions/{upload_id}/complete",
            "/api/op/consistent-images",
            "/api/op/drafts/{session_id}/jobs/{job_id}",
            "/api/content-archive",
            "/api/content-archive/{archive_id}",
        ],
    },
    {
        "key": "note_extract",
        "title": "他人笔记提取",
        "summary": "对标拆解用:提取**别人**的笔记正文/图片/互动数/作者。"
                   "正文与图片纯 HTTP 零会话,只有抓评论才烧一次浏览器会话。",
        "paths": [
            "/api/notes/extract",
            "/api/note-extracts/{job_id}",
        ],
    },
    {
        "key": "media_gen",
        "title": "视频音频生成",
        "summary": "成片管线(搬运 / 分镜级再制作 / 成片修订)与即梦片段生成"
                   "(单镜、批量、抽帧续接、积分与登录态)。产物给不过期直链。",
        "paths": [
            "/api/video/jobs",
            "/api/video/jobs/{job_id}",
            "/api/video/jobs/{job_id}/retry",
            "/api/video/jobs/{job_id}/revise",
            "/api/video-assets",
            "/api/video-assets/{asset_id}",
            "/api/video-clips",
            "/api/video-clips/{clip_id}",
            "/api/video-clips/{clip_id}/frame",
            "/api/video-clip-batches",
            "/api/video-clip-batches/{batch_id}",
            "/api/video-credits",
            "/api/dreamina-status",
        ],
    },
    {
        "key": "style_profile",
        "title": "风格档案",
        "summary": "每个运营自己的写作/视觉风格档案:多套并存、每套独立版本链、可回退;"
                   "未建档运营实时回落管理员默认档。",
        "paths": [
            "/api/style-profile",
            "/api/style-profile/versions",
            "/api/style-profile/versions/{version}",
            "/api/style-profile/rollback",
            "/api/style-profile/admin-default",
            "/api/style-profile/sets",
            "/api/style-profile/sets/{name}",
        ],
    },
    {
        "key": "ops",
        "title": "运维与权限",
        "summary": "[管理员] 运营者账号增删改、apikey 轮转、小红书账号操作权授予与回收。",
        "paths": [
            "/api/operators",
            "/api/operators/{operator_id}",
            "/api/operators/{operator_id}/rotate-apikey",
            "/api/operators/{operator_id}/grants",
            "/api/operators/{operator_id}/grants/{xhs_account_id}",
        ],
    },
]

# ---------------------------------------------------------------------------
# 二、变更记录
#
# 面向**调用方**写,不是给自己看的施工记录:每条只回答"我的代码要不要改、行为会不会
# 不一样"。kind 语义:
#   feature     新能力/新端点/新参数,老调用不受影响
#   fix         行为修正,老调用的**预期**不变但实际结果变对了
#   breaking    老调用可能因此失败或行为明显不同,必须看
#   deprecation 仍可用但将下线
# endpoints 里的每个路径都被测试比对 manifest,写错即红。
#
# 补录起点 2026-07-28(0.17.0),更早的看 CHANGELOG.md。本表与 CHANGELOG.md **有意不互相
# 生成**:两者组织轴不同(那边版本 × 长篇施工叙事给人读,这边日期 × 对调用方影响给机器读),
# 互相生成不是砍掉那边的细节就是把这边撑成第二份长文档。机械钉住的只有真正会静默腐烂又真正
# 误导人的那一项——版本号,见 tests/test_version_changelog.py。
# ---------------------------------------------------------------------------

CHANGELOG_KINDS = frozenset({"feature", "fix", "breaking", "deprecation"})

# 本表声明的补录下界。它不是摆设:tests/test_version_changelog.py 拿它当边界,要求
# **CHANGELOG.md 里这个日期之后的每个版本段,在本表里都至少有一条同日期记录**——
# 「发了版却没告诉调用方」正是本接口要消灭的那种腐烂。改小它等于承诺补更早的记录。
CHANGELOG_COVERAGE_SINCE = "2026-07-28"

CHANGELOG_ENTRIES = [
    {
        "date": "2026-08-10",
        "title": "挂载失败取证:items_seen 当场条目清单 + failed 条目步骤字段通用透传",
        "kind": "fix",
        "summary": "①collection_item_not_found 失败时回执新增 items_seen(弹层当场渲染出的"
                   "条目清单,临时诊断字段)——RCA:出轨贴两跑失败时 catalog 有、DOM 匹配不上,"
                   "次日复查条目又在(平台瞬态),当时没记'看到过什么'黑箱一晚;②failed 条目"
                   "组装从'只取 reason'改为**通用透传**步骤自带的全部取证字段——白名单式"
                   "取键已连吞三次证据(poll_timeline/caret_rect/items_seen),一次根治。"
                   "回执形状向后兼容(纯增键)。",
        "endpoints": ["/api/accounts/{account_id}/note-components"],
    },
    {
        "date": "2026-08-09",
        "title": "播客合集判据盲点修+建前查重升级为拦截,podcast-collections 解冻(P1)",
        "kind": "fix",
        "summary": "两处:①判据盲点——创建表单收起后页面常落在「上传视频」tab(号6播客首验"
                   "实拍),而合集区在发播客 tab,不切回去验名,真建成也只能报"
                   "create_page_closed_name_missing;现在收起后先切回发播客 tab 再验名。"
                   "②建前查重从「记录 name_preexisted 继续建」升级为「同名即拦截」"
                   "(collection_name_already_exists),对齐笔记合集语义——平台不去重同名"
                   "(号5 双会客厅实证),纯新建场景下记录性字段等于放行重复。"
                   "confirmed_by 的 _name_preexisted 后缀因此不再出现。**端点解冻**,"
                   "薛定谔提交(loading 态提交可能延迟落地)教训:建前必查列表,本升级把它"
                   "编码进了流程。",
        "endpoints": ["/api/accounts/{account_id}/podcast-collections"],
    },
    {
        "date": "2026-08-09",
        "title": "建笔记合集:弹层列表预取回落——图文载体点弹层不发新请求,误报 catalog_unavailable",
        "kind": "fix",
        "summary": "号8 首验实测:图文载体的编辑器在**页面加载时**就预取了合集列表,点「加入"
                   "合集」只渲染缓存、不再发新请求,而查重那步只等\"点击后的新增响应\",必然"
                   "超时、整单误报 collection_catalog_unavailable 中止。修:等不到新增响应时"
                   "回落到已捕获的预取响应(与 GET /collections 流程\"先认预取\"同源),回落"
                   "仍拿不到才如实中止。首验因此重跑,判据语义不变。",
        "endpoints": ["/api/accounts/{account_id}/note-collections"],
    },
    {
        "date": "2026-08-09",
        "title": "新能力:**建笔记合集**(图文笔记挂载用的那套合集,与播客合集是两套系统)",
        "kind": "feature",
        "summary": "此前只能建**播客合集**,而图文笔记挂载(``POST /note-components`` 的 "
                   "``collection_id``)认的是**笔记合集** —— 也就是 "
                   "``GET /api/accounts/{id}/collections`` 读的那套 picker 系统,它一直只能"
                   "读不能建,存量笔记归拢全卡在「需要的合集不存在」上。现在补齐:"
                   "``POST /api/accounts/{id}/note-collections``。"
                   "**入口不在笔记管理页**(实拍确认那里根本没有合集 tab),在**笔记编辑器"
                   "「加入合集」弹层的底栏**「创建合集」——所以必须传一篇 ``carrier_note_id`` "
                   "载体笔记来打开编辑器,而平台的提交按钮是**「创建并加入」**:这篇载体会被"
                   "加进新合集,请传一篇本来就该挂进去的笔记。表单只有名称(≤20)与简介"
                   "(**≤50**,与播客合集的 100 不同)两项,**没有封面字段**,故传 ``cover`` "
                   "一律 422 而不是静默忽略。"
                   "判据直接继承播客合集 7 单假绿的教训,**双信号缺一不可**:创建表单收起 "
                   "**且** **重进更新页之后**的干净合集列表里出现这个名字(重进会丢弃一切"
                   "未提交的编辑器状态,所以这一条同时证明合集真落库了);页面文本里出现"
                   "合集名一律不算证据。禁用判定同样四路取或(disabled 属性 / 整词 "
                   "``disabled`` / ``-disabled`` 结尾类名 / loading 类名),等不到翻转就"
                   "如实报错、**绝不点禁用按钮**。"
                   "另有**建前查重**:该号已有同名合集时**一个字都不建**,直接报 "
                   "``collection_name_already_exists`` 并把现有那条的 id / note_num 回给你"
                   "——平台不去重同名,重建只会多出一个空合集要人工删。",
        "endpoints": [
            "/api/accounts/{account_id}/note-collections",
            "/api/note-collections/{job_id}",
        ],
        "notes": "⚠️ **两件事尚未真号取证,首验靠回执分辨**:①创建是即时落地还是随笔记提交"
                 "才生效 —— 看 ``joined_carrier``(重进后该合集的 note_num)与 "
                 "``created_api_capture``(点「创建并加入」后新增的 POST 响应取证,已排除"
                 "弹层列表接口 list_v2);②创建 API 的形态与回不回 id —— 同样看 "
                 "``created_api_capture``。``created_api_capture`` 与 ``modal_html`` 都是"
                 "**临时诊断字段**,首验钉死之后即撤,勿建硬依赖。"
                 "另:载体笔记**不能是已在某个合集里的笔记**(已选态下「加入合集」入口本身"
                 "不渲染,会报 ``collection_entry_not_found``);本能力**全程零提交**,"
                 "不点发布。",
    },
    {
        "date": "2026-08-09",
        "title": "播客合集创建**双判据修复**:禁用态补裸 ``disabled`` token + 成功判据杜绝"
                 "预览卡伪证(7 单历史假绿,**待真号复验**)",
        "kind": "fix",
        "summary": "真号 7 单创建播客合集**全部报 done、平台侧一个都没建出来**(事后 "
                   "GET /collections 实测新合集一个不存在)。取证里两个缺陷叠在一起:"
                   "①**禁用判据漏了一种形态** —— 假绿单的按钮 class 原文是 "
                   "``d-button d-button-large … disabled --color-static bold "
                   "d-button-primary-loading … create-btn``,禁用只体现在一个**裸 token** "
                   "``disabled`` 上,按钮既没有 disabled 属性、也没有 ``create-btn-disabled``;"
                   "旧判据两条都不命中 → ``_wait_create_enabled`` 秒判「可点」→ 点下一颗禁用"
                   "按钮无事发生。②**成功判据里藏着伪证人** —— 创建表单右侧渲染一张**实时"
                   "预览卡**,把刚打进输入框的合集名原样显示出来(假绿单 page_text 实录:"
                   "表单文案「创建播客合集 / 合集名称* / 11/20」与预览卡「NBDpsy心理会客厅 / "
                   "播客 / 更新至0集 / 0人听过」并存);旧实现「表单收起 **或** 页面文本里有"
                   "这个名字」任一命中即算成功,于是表单根本没提交的那 7 单,全部拿自己打的字"
                   "当证人判了 done。"
                   "**改法**:①禁用判据从两路加到四路取或,新增 class 按空白切分后的**整词** "
                   "``disabled`` 与 ``d-button-primary-loading``(loading 态点了也白点);整词"
                   "比较不用 substring,是防将来任何 ``xxx-disabled`` 类名把按钮永久判死。"
                   "②成功判据改成**与**:``表单收起`` 且 ``收起后页面文本里出现该合集名``,"
                   "**表单还开着时页面里出现合集名不构成任何成功证据**;失败细分成 "
                   "``create_form_still_open``(带按钮 cls 全文 + 按钮中心 elementFromPoint "
                   "落点链 + 引导浮层现状,回答「下一单为什么还提交不出去」)与 "
                   "``create_page_closed_name_missing``(做没做成未知,请人工核对);新增 "
                   "``name_preexisted``(创建**前**列表里就有同名 → confirmed_by 后缀 "
                   "``_name_preexisted``,名字检查对它不构成新建证据)。③``创建`` 按钮的等待"
                   "窗口 20s → 45s:判据修对之前这一步**从没真等过**,而它等的是封面(≤5MB)"
                   "上传完 + 平台处理完(假绿单实测按钮同时挂 loading 态)。"
                   "**如实说明:两条修法都对症于假绿单的 observed 实录,但尚未经真号复验**"
                   "——不要当作「建播客合集已经修好了」,done 之后仍请到发播客页看一眼。",
        "endpoints": [
            "/api/accounts/{account_id}/podcast-collections",
            "/api/podcast-collections/{job_id}",
        ],
        "notes": "⚠️ **``confirmed_by=name_in_list`` 已废除**(它就是假绿的直接凭据),"
                 "done 只剩 ``create_page_closed`` 与 ``create_page_closed_name_preexisted``;"
                 "调用方若按 ``name_in_list`` 做过分支,直接删掉那条。"
                 "轮询回执新增 ``name_shown_after_close`` / ``name_preexisted``(布尔,常驻)"
                 "与 ``create_button_forensics`` + ``guide_tooltip_present``(仅提交类失败时带:"
                 "按钮 cls 全文、矩形、``point_element_chain``、``point_hits_button``、"
                 "同一时刻引导浮层在不在)。"
                 "**这两个取证字段是临时诊断字段**,与 ``point_element_chain`` "
                 "同一条保质期纪律:真号复验坐实/排除「按钮点不动」这个候选即撤,勿建硬依赖。"
                 "历史影响:0.20.3 之前拿到的 ``done`` **不可信**(尤其 "
                 "``confirmed_by=name_in_list`` 的),按合集列表实况为准。",
    },
    {
        "date": "2026-08-09",
        "title": "补挂话题锚定判据放宽:浮层「中心在正文栏内」改「与正文栏相交」(**待真号复验**)",
        "kind": "fix",
        "summary": "上一条(0.20.1)上线的条件轮询**没有把失败清零**:真号三单复验里追加的词"
                   "仍全报 ``topic_dropdown_not_shown``,而新加的 ``poll_timeline`` 恰好把"
                   "「浮层晚到」这个候选**排除掉了** —— 8 个 tick 全程 ``layers_seen`` 稳定在"
                   "14-19 层、8 秒恒定,浮层不是来得晚。剩下的候选是「浮层被几何锚定判据拒错」,"
                   "机制上能自洽解释全部历史现象:联想浮层挂在**光标**上,正文里话题 chip 一多"
                   "光标就被顶向行尾、浮层跟着右移,水平**中心**滑出正文栏右缘就被判据拒掉——"
                   "位置败不是词败(号6 播客同一单前 2 个词成功、第 3 个起连败)、按笔记确定性"
                   "(号1 三连败)、正文换行即复活(号5 短片 5/5 全成)都对得上。"
                   "①**锚定判据从中心测试改相交测试**:判别性质自始至终是「右侧手机预览面板与"
                   "正文栏水平**不相交**」(两种页型生产截图双实证),「中心落在栏内」只是它的"
                   "一个**过紧代理**;现在直接判「浮层与正文栏水平相交」,预览面板零相交照拒不误。"
                   "②**失败回执新增 ``rejected_with_items``**:被判据拒掉、却**带话题选项**的层"
                   "(最多 3 层,记 class / 选项数 / x,y,w)。判据修对了它应恒空;若照样失败,"
                   "它会直接指名被拒的真凶是谁——``rejected_classes`` 只记前 5 个类名、也不说"
                   "那层有没有内容,真下拉被它挡住过一整轮。③**``poll_timeline`` 每 tick 新增 "
                   "``with_items``**(本 tick 带话题选项的层数,不分收下还是拒掉):恒 0 = 联想"
                   "内容压根没回来,非 0 = 浮层来了被拒。④**失败回执新增 ``caret_rect``**:"
                   "光标矩形 ``{x, y}``(页面取不到留 null)。联想浮层挂在光标上,所以光标就是"
                   "「浮层该出现在哪」的地面真值 —— 有了 光标 + 浮层 rect + 正文栏 rect 三者,"
                   "下一次回执一测便知是水平错锚(光标 x 顶到栏右缘)还是垂直方向的事,不必再推断。"
                   "**如实说明:本条的依据是 0.20.1 真号三单的 poll_timeline 证据链 + 上述机制"
                   "假设,修法对症但尚未经真号复验**——不要当作「补话题已经修好了」。",
        "endpoints": [
            "/api/accounts/{account_id}/note-components",
            "/api/note-components/{job_id}",
        ],
        "notes": "调用方接口不变,失败条目里多三个取证字段:``rejected_with_items`` 与 "
                 "``caret_rect``(条目级)、``poll_timeline`` 每 tick 的 ``with_items``。"
                 "**三个都是临时诊断字段**,与 "
                 "``poll_timeline`` / ``point_element_chain`` 同一条保质期纪律,锚定判据候选"
                 "坐实或排除即撤,勿建硬依赖。补话题仍**别当 100% 会成**、逐条看 topics_failed;"
                 "``topic_dropdown_not_shown`` / ``topic_dropdown_not_found`` 仍然是我们的定位"
                 "问题,**不要换词**,把失败条目原样反馈回来。",
    },
    {
        "date": "2026-08-09",
        "title": "补挂话题真因复位:等浮层改条件轮询 + no_exact_match 语义收严 + 轮询时间线取证"
                 " + 聚焦点击避开底部操作栏",
        "kind": "fix",
        "summary": "同日上一条把追加场景失败归因为「``#`` 粘连前一个话题实体」,**真号复验推翻了"
                   "它**:补了空格分隔、正文末尾确实变成 ``[话题]# #失眠``,浮层照样不弹;而且"
                   "同一个词在第 2 位补得上、在第 5 位补不上——**位置败不是词败**。"
                   "①**等浮层改条件轮询**:打完 ``#话题`` 后按拟人节奏最多轮询 8 次 / 约 8 秒,"
                   "每次重新采集一遍浮层,弹出即点选;旧实现是定长等 1.5~2.5s **只看一眼**,"
                   "对晚到的浮层零容错。②**``no_exact_match`` 语义收严**:通过判据但**一个选项"
                   "都没有**的空壳浮层(真号样本 ``layer_class=suffix item_count=0``)此前被当成"
                   "「下拉在、没这词」,现在一律归 ``topic_dropdown_not_shown``——它是浮层没弹,"
                   "不是平台没这词。③**失败回执新增 ``poll_timeline``**:每轮的 "
                   "``{tick, elapsed_s, layers_seen, reason}``,把"
                   "「浮层晚到」与「浮层被判据拒错」两个候选分得开。分隔空格作为无害的卫生措施保留。"
                   "④**聚焦点击改「可见区带内偏上」(底栏吞点击几何修复)**:同批取证钉死的"
                   "另一条独立缺陷 —— 正文最长的那篇笔记把编辑器矩形撑到最下面、框底探出视口,"
                   "矩形**死中心**落点离视口底只剩 72px,正好压在编辑更新页底部那条固定操作栏"
                   "(更新/取消)上被它吞掉,补话题第一步就报 ``content_box_focus_failed``"
                   "(号6播客三次复现;同账号别的笔记正文短、落点高,全绿 = 笔记级绑定正文长度)。"
                   "现在落点收进**矩形与视口的交集**并取偏上三分之一,避开底栏;落点落在框内"
                   "哪个位置没有语义(聚焦成功后紧跟 ``Control+End`` 把光标移到正文末尾)。"
                   "⑤**聚焦失败取证新增 ``elementFromPoint`` 落点链**:``point_element_chain``"
                   "(落点元素起向上最多 4 层祖先)+ ``point_inside_editor``(落点是否在正文框"
                   "内部),直接回答「那一下到底点在了谁身上」——这两个是**临时诊断字段**,"
                   "与 ``poll_timeline`` 同一条保质期纪律,底栏候选坐实/排除即撤。",
        "endpoints": [
            "/api/accounts/{account_id}/note-components",
            "/api/note-components/{job_id}",
        ],
        "notes": "**对调用方的直接影响:收到 ``no_exact_match`` 才换词这条判据变得更可信了**——"
                 "此前空壳浮层也报这个码,照它换词是白烧会话(词本身没问题)。现在这类失败一律"
                 "报 ``topic_dropdown_not_shown``,该反馈我们修、不要换词。失败条目里多一个 "
                 "``poll_timeline`` 数组,反馈缺陷时**请把它一并带上**,它是定位真因的关键证据。"
                 "``content_box_focus_failed`` 的失败条目里另多两个临时取证字段 "
                 "``point_element_chain`` / ``point_inside_editor``,同样请一并带回。"
                 "单个话题最坏耗时从约 4s 升到约 11-13s(轮询等满+拟人疲劳系数放大等待+重页面采集),补 10 个话题也仍远在账号子进程"
                 "硬超时(1800s)之内。",
    },
    {
        "date": "2026-08-09",
        "title": "补挂话题三修:追加场景 # 粘连修复 + 聚焦失败取证 + 失败原因细分",
        "kind": "fix",
        "summary": "放量实测暴露的两个真号缺陷 + 一条原因码细分。"
                   "①**追加场景 # 粘连**:笔记已有话题实体(蓝色 chip)时往后补,``#`` 直接粘在"
                   "chip 边界(tail 见 ``[话题]##失眠``)编辑器不弹联想浮层,浮层空被误判换词——"
                   "「失眠」这种大众词也报不存在。修法:每个话题输入前先补一个空格分隔,``#`` 与"
                   "前一个实体分开浮层才弹(从零场景多一个空格无害,小红书吞多余空格);失败回删"
                   "连分隔空格一起删净不留残缺。②**content_box_focus_failed 不再是黑箱**:补话题"
                   "第一步聚焦正文框失败时带回当场取证(主选择器是否命中/页面有几个 "
                   "contenteditable/命中框矩形与视口高/是否滚进过视口),分清「选择器没命中」"
                   "(页面结构异常)与「命中了但焦点没落进」(时序)。③**失败原因细分**:原 "
                   "``no_floating_layer`` 更名 ``topic_dropdown_not_shown``,语义钉死为「浮层根本"
                   "没弹」(candidates 空)。",
        "endpoints": [
            "/api/accounts/{account_id}/note-components",
            "/api/note-components/{job_id}",
        ],
        "notes": "**话题失败原因码按 candidates 空/非空区分处置**:``topic_dropdown_not_shown``"
                 "(candidates 空,浮层没弹)/ ``topic_dropdown_not_found``(有浮层没找到真下拉,"
                 "多半抓到右侧预览面板镜像)—— 这两者是**定位/输入问题,该反馈我们修,绝不换词**;"
                 "只有 ``no_exact_match``(candidates 非空、真下拉在但没这词)才是真·平台没这词、"
                 "换词才有意义。此前追加场景一律糊成 no_exact_match 误导调用方白烧会话换词。",
    },
    {
        "date": "2026-08-08",
        "title": "已发布笔记可以补挂话题了(追加语义,存量视频笔记话题空置的补救)",
        "kind": "feature",
        "summary": "note-components 端点加 topics 参数,给已发布笔记补挂/修正话题标签。"
                   "起因:8 条存量视频笔记发布时话题 7/8 全空(发布链话题下拉定位缺陷,已修但"
                   "存量丢的救不回)。**语义是追加不是替换**:传入话题先读现有话题算差集,只补差集、"
                   "去重、总数 >10 截断(全量替换在已有话题的笔记上太危险,一次手滑清空别人的话题)。"
                   "话题输入**复用发布链同一套正向判据**(topic_dropdown.select_topic_option,"
                   "输 #话题名 → 浮层 → 几何锚定选精确匹配),绝不走已定性有缺陷的旧「面积最小」"
                   "近似。**回读判据从第一天就用平台实况**:提交后重进页面读正文里的话题实体,"
                   "applied.topics 反映真挂上的全量话题(不是点了就算——发布链的话题正是乐观态"
                   "踩的雷);逐个话题独立成败,失败进 topics_failed 带原因、不连坐其余。"
                   "请求的全已挂 → 零点击零提交(幂等,存量批量补齐安全)。**任何 note_type 都受理**"
                   "(视频/图文都靠正文编辑器输话题)。",
        "endpoints": [
            "/api/accounts/{account_id}/note-components",
            "/api/note-components/{job_id}",
        ],
        "notes": "验收判据:已有 1 个话题的笔记补 4 个 → 结果 5 个、原话题保留(看 applied.topics "
                 "长度);现有+新增 >10 → topics_truncated 如实说明留了哪些;某话题平台拒绝 → "
                 "topics_failed 给原因、其余不受连坐。**已真号验证通过**(2026-08-08 号1 job278"
                 "「我追得越紧他退得越远」:回读平台确认原「过度寻求保证」+补的4个共5个话题实际"
                 "挂上、追加语义与回读判据都成立)——更新页正文输 #话题**确实会弹话题联想浮层、"
                 "能选中成话题实体**,这条编辑链是通的。放量仍建议头 1-2 篇到笔记里人工核对,"
                 "别看到 applied.topics 缺词就反复重跑(每次重跑都是一次真提交)。"
                 "待补名单见需求文档(号 1/5/6/8 各 2 条),job278 那条已补齐。",
    },
    {
        "date": "2026-08-08",
        "title": "known_limitations 按真号验证结果刷新:三条从「待验」改「已验」,补三条新坑",
        "kind": "fix",
        "summary": "把 known_limitations 里被 08-08 真号验证追平的条目如实更新,不留过期建议:"
                   "①补录原创声明——编辑页幂等与回显已真号验证(账号5),从「待验证」改「已验」;"
                   "②改封面——已发布改封面(弹窗链)已真号首验通过+判据已修,与发布时封面(内联链)"
                   "拆成两条讲清别混。新增三坑:发布端点合集只认 collection_id(传 --collection-name "
                   "会被静默忽略,实测五单没建成);判笔记类型认 note_type 字段不认 media_json"
                   "(后者可能漏同步视频字段);改封面只对视频/播客、图文传 cover 直接 422。",
        "endpoints": ["/api/guide", "/api/accounts/{account_id}/note-components", "/api/publish-jobs"],
        "notes": "known_limitations 是活的:真号验掉一条就把它从「待验」撤下来,新踩一个坑就补进去。"
                 "留着过期的「尚未验证」和漏掉新坑一样有害。",
    },
    {
        "date": "2026-08-08",
        "title": "改封面回读判据分层:换成功却报 false 的过严缺陷已修(账号5 真号首验)",
        "kind": "fix",
        "summary": "改封面能力真号首验暴露过严缺陷:封面**确实换成功了**(App 目视确认,指纹从"
                   "正式 CDN sns-na-i2 变成刚上传的 ros-preview 预览),回执却报 "
                   "applied.cover=false / cover_not_verified。根因:旧判据依赖提交后重进更新页"
                   "读封面浮层 .operator 的 noCover class,而这条笔记提交后 .operator 读不到"
                   "(noCover 前后都是 None),辅助判据失效时**没退回只认指纹变化**,把强信号"
                   "已成立的结果推翻成了 false。**修法**:回读判据改成分层 —— ①**指纹变化是"
                   "强信号**(提交前封面区背景图指纹变成非空新指纹即判换成,最可靠);②noCover "
                   "消失是**辅助信号**,读不到(None)时不参与、**绝不否决**强信号;③两者都没变/"
                   "读不到才 false(保留 fail-loud)。判据仍是**封面真的变了**,不是「点了就算成」。",
        "endpoints": [
            "/api/accounts/{account_id}/note-components",
            "/api/note-components/{job_id}",
        ],
        "notes": "回执 observed 里 fingerprint_before/after 与 no_cover_before/after 都保留供排查。"
                 "提交后回读改用「.operator 缺失也照读缩略图指纹」的宽松读法,不再让浮层缺失把"
                 "指纹一起吞掉;提交前的幂等判据仍是严格读法(判不出现状就不动手)。",
    },
    {
        "date": "2026-08-08",
        "title": "播客选择器换真值:三处占位全部落地,并揪出一个从没被发现的 tab 判据缺陷",
        "kind": "fix",
        "summary": "第三轮真号取证(账号9)首次走到发布表单,播客链的三处占位选择器换成实测真值:"
                   "① 上传弹窗 .audio-upload-modal 里**两个 file input 的 class 完全相同**,"
                   "唯一区分是 accept(音频 .mp3,.wav,.aac,.flac,.m4a / 封面 .jpg,.jpeg,.png,.webp);"
                   "② 发布表单的标题 / 正文 / 内容设置区;③ 合集卡真实文案是**「加入播客合集」**"
                   "——旧占位猜的「播客合集 / 选择合集 / 加入合集」三个全部落空。"
                   "**顺带修掉一个此前无人知道的缺陷**:DOM 里同时存在两个 .creator-tab.active"
                   "(一个陈旧残留在「上传视频」上),querySelector 取文档序第一个恰好取到错的那个,"
                   "导致「切到发播客 tab」在三次会话里全程误判为失败——判据改成读全部 active "
                   "取并集 + 叠一路独立的内容判据兜底。",
        "endpoints": ["/api/publish-jobs", "/api/publish-jobs/{job_id}"],
        "notes": "**播客仍未跑过真号 e2e 全链**(取证刻意停在「去发布」之前没真发)。两处仍未取证:"
                 "合集卡点开后的候选结构(选合集这步仍 fail-loud、失败只告警不阻断)、"
                 "接近 2 小时上限的长音频有没有额外转码等待。另外「播客合集上线啦」引导浮层"
                 "**关不掉**(四招实测全无效),现走「点按钮右侧约 39px 暴露缝穿透」这条绕过,"
                 "它随窗口尺寸变化而脆——详见 known_limitations 里 area=publish 的播客那条。",
    },
    {
        "date": "2026-08-08",
        "title": "已发布的视频笔记可以改封面了",
        "kind": "feature",
        "summary": "笔记编辑端点新增 cover:给**已发布的视频笔记**换自定义封面(服务器侧本地"
                   "图片路径)。走更新页封面区悬停出的「修改封面」→「设置封面」弹窗 →"
                   "「上传封面」tab 灌图 → 确定,全程不碰「智能推荐封面」与「PK封面」。"
                   "**只对视频笔记**:图文笔记的封面就是第一张图,传了直接 422。"
                   "**幂等**:已经是自定义封面就 skipped 且**一次发布都不点**,批量重跑安全。"
                   "回执三态与三组件同款(applied.cover = true/false/null),判据是**封面区"
                   "真的变了**,不是「点了就算成」。",
        "endpoints": [
            "/api/accounts/{account_id}/note-components",
            "/api/note-components/{job_id}",
        ],
        "notes": "选择器由 2026-08-08 真号只读取证锁定(账号2 一篇视频笔记):幂等判据是"
                 "封面浮层 .operator 的 noCover class;入口按实测矩形中心点(它与「遇到问题?」"
                 "贴得极近、element.click() 会撞 tooltip 覆盖区);图片 file input **懒挂载在"
                 "「上传封面」tab 里**,不先切 tab 就永远找不到。⚠️ **真上传+提交那一段尚未跑过"
                 "真号 e2e**,首批先跑 1-2 篇人工核对封面真的换了再放量。",
    },
    {
        "date": "2026-08-08",
        "title": "话题下拉定位换正向判据:视频发布话题 6/6 全败的根因已定性",
        "kind": "fix",
        "summary": "视频笔记的话题连续全败,昨天带回的取证一击定性:定位下拉的启发式是"
                   "「取页面上面积最小的浮层」,在视频页稳定抓成**右侧手机预览面板的作者信息区**"
                   "(class base-info,内容是昵称/关注/编辑于;它含话题文案是因为预览镜像了正文)。"
                   "现在改成正向判据——浮层必须水平锚定在正文栏那一列(下拉挂在光标上,预览面板"
                   "在页面右侧,两种页型都不相交),并在**所有**候选层里找精确匹配,不再挑一层"
                   "走到黑。失败原因随之三分:no_floating_layer(没浮层)/ "
                   "**topic_dropdown_not_found(有浮层但没找到真下拉,新增)** / "
                   "no_exact_match(下拉在、词不在)。前两者是我们的定位问题,最后一个才是换词。",
        "endpoints": ["/api/publish-jobs", "/api/publish-jobs/{job_id}"],
        "notes": "话题失败语义未变:回删不留残缺文本、不阻断发布。取证字段全部保留并加两项:"
                 "layers_seen(共看到几层浮层)、rejected_classes(被判据拒掉的层 class)。",
    },
    {
        "date": "2026-08-08",
        "title": "已发布笔记可以补录原创声明了",
        "kind": "feature",
        "summary": "笔记编辑端点新增 set_original_declaration:给**已发布**的笔记补上原创声明"
                   "(为 08-05~08-07 那批漏标的补标)。**只支持开启**,传 false 直接 422"
                   "(关闭的平台行为未取证且是破坏性动作)。**幂等**:已是开态就 skipped 且"
                   "**一次发布都不点**,批量重跑安全。回执三态与三组件同款"
                   "(applied.original_declaration = true/false/null)。",
        "endpoints": [
            "/api/accounts/{account_id}/note-components",
            "/api/note-components/{job_id}",
        ],
        "notes": "补声明走的是**发布链完全同一个** apply_original_declaration(协议弹窗链),"
                 "所以 08-07 那个根因修复(勾选点位从宽容器收窄到 16×16 方块、关掉随机偏移,"
                 "躲开约 40% 撞进《原创声明须知》链接的概率)自动覆盖编辑链,系统里没有第二份"
                 "协议弹窗实现。批量补录时按号分散(同号有每小时会话帽),看到 queued 别重试。",
    },
    {
        "date": "2026-08-08",
        "title": "新号自动融进矩阵——补上真正会触发它的那一环",
        "kind": "fix",
        "summary": "8-07 记的「新号自动融进矩阵」在生产**一次也没触发过**:转正引导链只挂在"
                   "手动检测与周期巡检两条路上,而周期巡检默认关闭、生产也没开——新号除非有人"
                   "手动 POST 一次 cookie-checks,否则永远卡在 cookie_status='unknown'"
                   "(实测一个号加入 27 小时仍是 unknown)。现在有一个轻量调度器专管这件事:"
                   "有 cookie 却还没转正的号自动排**一次**检测,转 valid 后引导链照常接管。"
                   "调用方侧无新参数,表现为新号灌完 cookie 后真的不再需要人工推一把。",
        "endpoints": [
            "/api/accounts/{account_id}/cookie-checks",
            "/api/interaction-backfills",
        ],
        "notes": "刻意**不是**打开全矩阵周期巡检:那是每号每轮各烧一次浏览器会话,会与同号会话"
                 "总闸(系统 4 次/时)正面抢额度。新号要的是一次性检测,转正即出局;检测不过去的号"
                 "有按号退避,不会以扫描间隔无限重试。invalid / captcha / restricted 三态仍维持"
                 "不自动重试。",
    },
    {
        "date": "2026-08-07",
        "title": "排队可见性:轮询直接告诉你排第几、在等什么、还要等多久",
        "kind": "feature",
        "summary": "14 个轮询端点的排队态一律带 queue 段:position / ahead / "
                   "account_queue_depth / running(当前执行那条)/ blocked_by + detail。"
                   "blocked_by 四态:session_cap(附 used/cap/window_resets_at,精确算出额度"
                   "何时重新有位)、account_busy、global_concurrency、null(没被闸住或排期未"
                   "到点,附 not_before)。**看到 queued 不要重试**——重试只会再灌一条进同一个队列。",
        "endpoints": [
            "/api/publish-jobs/{job_id}",
            "/api/cookie-checks/{check_id}",
            "/api/note-exports/{export_id}",
            "/api/note-deletions/{deletion_id}",
            "/api/note-components/{job_id}",
            "/api/note-component-reads/{job_id}",
            "/api/note-comments/{comment_id}",
            "/api/note-extracts/{job_id}",
            "/api/collection-batches/{job_id}",
            "/api/interaction-backfills/{job_id}",
            "/api/podcast-collections/{job_id}",
        ],
        "notes": "起因是会话总闸上线后出现过全矩阵 9 个号满帽、11 条排队、running=0,单条排 "
                 "40 分钟以上,而调用方只看得到 status=queued。顺带修了一处真缺陷:"
                 "not_before 带时区偏移时会撞 TypeError 被当成「立即可派」静默放行,排期直接失效。",
    },
    {
        "date": "2026-08-07",
        "title": "矩阵互动扇出改一轮一会话做多篇",
        "kind": "fix",
        "summary": "发布成功后的矩阵互动此前按「每篇一条任务」登记,同号待互动的多篇会摊成多次"
                   "浏览器会话,在会话总闸下既占额度又排长队。改为同号待互动笔记合并登记,"
                   "一轮会话做多篇。调用方无参数变化,表现为发布后的互动扇出不再霸占队列。",
        "endpoints": ["/api/publish-jobs"],
        "notes": None,
    },
    {
        "date": "2026-08-07",
        "title": "统一指南接口 + 版本号补齐到 0.20.0",
        "kind": "fix",
        "summary": "新增 GET /api/guide(本接口):能力域分组 + 变更记录 + 已知边界。"
                   "同笔修掉 server_version 报错版本号的问题——它此前静默落后 CHANGELOG 两个"
                   "版本(0.17.0 vs 0.19.1),而 manifest / guide.meta / extension 三处都在报它。"
                   "现已加测试钉死 app.__version__ 与 CHANGELOG.md 最新条目一致,漏改即 CI 红。",
        "endpoints": ["/api/guide", "/api/manifest", "/api/extension"],
        "notes": "meta.server_version 自此可信;meta.git_revision 仍给出,用于分辨同版本号下的"
                 "具体代码。",
    },
    {
        "date": "2026-08-07",
        "title": "他人笔记提取",
        "kind": "feature",
        "summary": "新增提取**别人**笔记的端点(对标拆解用):正文/图片/互动数/作者。"
                   "不带 with_comments 时是纯 HTTP 同步返回、零浏览器会话、不烧账号频次;"
                   "带 with_comments 才落异步任务、起一次会话,用 /api/note-extracts/{job_id} 轮询。",
        "endpoints": ["/api/notes/extract", "/api/note-extracts/{job_id}"],
        "notes": "首版 manifest 曾把失败态写成可重试,当天即改正:选择器失配时如实报"
                 "失配而不谎报抓完;单张原图下载失败不再拖垮整批。",
    },
    {
        "date": "2026-08-07",
        "title": "播客(音频)发布 + 播客合集创建",
        "kind": "feature",
        "summary": "POST /api/publish-jobs 的媒体参数从「图文 images / 视频 video」二选一"
                   "扩成三选一,新增 audio(服务器侧音频路径,.m4a/.mp3/.wav/.flac/.aac,"
                   "时长 10 分钟~2 小时、≤1GB)。配套新增播客合集创建端点,发播客时按名称加入。",
        "endpoints": [
            "/api/publish-jobs",
            "/api/accounts/{account_id}/podcast-collections",
            "/api/podcast-collections/{job_id}",
        ],
        "notes": "GB 级音频先走 /api/uploads/media-sessions 分片上传拿 path,别直传。"
                 "(当日三处浏览器选择器还是占位值;08-08 已换成真号取证真值,"
                 "详见同日那条 changelog 与 known_limitations 里 area=publish 的播客那条。)",
    },
    {
        "date": "2026-08-07",
        "title": "自定义视频封面 + 大媒体分片上传通道",
        "kind": "feature",
        "summary": "① POST /api/publish-jobs 加 cover(服务器侧图片路径,jpg/jpeg/png/webp),"
                   "**仅视频任务有效,图文任务传 cover 一律 422**(图文封面就是首图);不传 = "
                   "平台自动截首帧。② 新增分片上传三端点(开会话 / PUT 裸二进制分片 / complete),"
                   "反代单请求体上限 100MB,GB 级文件单发必死在隧道层,必须走这条。",
        "endpoints": [
            "/api/publish-jobs",
            "/api/uploads/media-sessions",
            "/api/uploads/media-sessions/{upload_id}/chunks/{index}",
            "/api/uploads/media-sessions/{upload_id}/complete",
        ],
        "notes": "complete 幂等(重复调回同一 path);封面设置失败**不阻断发布**,退回平台自动首帧。",
    },
    {
        "date": "2026-08-07",
        "title": "封面上传位三层候选 + 话题失败带浮层取证",
        "kind": "fix",
        "summary": "真号 e2e 暴露封面上传位在候选帧生成后改了名且懒挂载,原单一 class 选择器"
                   "100% 未命中。改为三层候选 + 悬停优先(点上传位有弹原生文件框卡死整条流程的"
                   "前科)。话题连续失配的现场证据(浮层实际候选文案/条数/容器 class)现在会带回"
                   "任务结果,调用方可据此分辨「搜索没触发」还是「词本身不存在」。",
        "endpoints": ["/api/publish-jobs", "/api/publish-jobs/{job_id}"],
        "notes": "封面/话题失败的语义均未变:封面失败退回平台自动首帧,话题失败回删不留残缺文本。",
    },
    {
        "date": "2026-08-07",
        "title": "移出合集 + 合集成员名单 + 批量清理",
        "kind": "feature",
        "summary": "笔记编辑端点补上「加入合集」的对称面:可把笔记移出合集;新增合集批量清理端点"
                   "(扫描名单只读 / 批量移出),**每轮篇数有硬上限**,一轮跑不完由返回的 remaining "
                   "告诉你还剩多少,再发一轮。",
        "endpoints": [
            "/api/accounts/{account_id}/note-components",
            "/api/note-components/{job_id}",
            "/api/accounts/{account_id}/collection-batches",
            "/api/collection-batches/{job_id}",
        ],
        "notes": "合集名比对是**全等**不是包含:同族合集(如「案例集」与「案例集2」)"
                 "用包含判据会把笔记从错误的合集里摘出去。",
    },
    {
        "date": "2026-08-07",
        "title": "原创声明协议弹窗打通(图文与视频两条路径)",
        "kind": "fix",
        "summary": "原创声明此前从未真正生效过:勾选框的 checked 是乐观态,关掉弹窗不等于已声明,"
                   "而代码一直在报 error(不是假成功)。本轮把协议链走完并对两条发布路径统一,"
                   "调用方要求原创声明的发布任务现在才会真的带上声明。",
        "endpoints": ["/api/publish-jobs"],
        "notes": "勾选点位收窄到 16×16 的真实方块:宽容器上的拟人随机偏移有约 40% 概率撞进旁边的链接。",
    },
    {
        "date": "2026-08-07",
        "title": "视频笔记发布线",
        "kind": "feature",
        "summary": "POST /api/publish-jobs 支持 video(服务器侧文件路径,"
                   ".mp4/.mov/.flv/.f4v/.mkv/.rm/.rmvb/.m4v/.mpg/.mpeg/.ts),与 images 互斥;"
                   "视频与图文共用全字段面(标题/正文/话题/三组件/原创声明)。上传超时按文件体积"
                   "伸缩,不再是固定值。活动关联补上「更多」面板路径——目标活动不在推荐位时"
                   "此前永远关联不上。",
        "endpoints": [
            "/api/publish-jobs",
            "/api/publish-jobs/{job_id}",
            "/api/accounts/{account_id}/activities",
        ],
        "notes": "视频跳过整条图片管线(物料化 + 去水印)。上传完成判据不照搬图文的"
                 "「标题框存在即就绪」——视频页标题 input 在进度 0% 时就已挂进 DOM。",
    },
    {
        "date": "2026-08-07",
        "title": "视频任务 PATCH images 改成 422 硬拒",
        "kind": "breaking",
        "summary": "给**视频**发布任务 PATCH images 此前被静默忽略(写进去也永不生效,"
                   "任务仍按视频发),现在一律 422 硬拒,detail 是单条字符串"
                   "「视频任务不可改图片,请取消后重建」。原先靠这个静默行为的调用方会开始收到 422。",
        "endpoints": ["/api/publish-jobs/{job_id}"],
        "notes": "422 的 detail 既可能是 FastAPI 的数组也可能是这种单条字符串,两种都要能解析。",
    },
    {
        "date": "2026-08-07",
        "title": "同号浏览器会话总闸(系统层 + 运营层)",
        "kind": "breaking",
        "summary": "风控红线是同号一小时 ≤4-5 次浏览器会话,而各子系统只守自己的闸,"
                   "生产实测出现单号 51 次/时。现在派发层统一按滚动小时窗计数并封顶:"
                   "系统自发任务默认 4 次/时,运营触发默认 12 次/时。**超帽的任务留在队列里等下轮"
                   "重估,不改状态、不失败、不排期**——调用方会看到 queued 停留变长,这不是卡死。",
        "endpoints": [
            "/api/accounts/{account_id}/note-component-reads",
            "/api/interaction-backfills",
            "/api/accounts/{account_id}/cookie-checks",
        ],
        "notes": "起因正是 skill 侧拿运营 apikey 逐篇回读组件、一小时 192 条全豁免直通。"
                 "批量诉求请改用批量端点(如 collection-batches / video-clip-batches)或自行限速。"
                 "受影响的是**所有会起浏览器的端点**,这里只列最容易撞上的三个。",
    },
    {
        "date": "2026-08-07",
        "title": "新号自动融进矩阵",
        "kind": "feature",
        "summary": "新加入的号此前会卡在 cookie_status='unknown':巡检只选 valid 号,"
                   "新号永远等不到人替它检测转正。现在巡检覆盖 unknown(且有 cookie 的)号,"
                   "转 valid 后自动登记两条引导任务(历史笔记入台账 + 去互动其余号)。"
                   "调用方侧无新参数,表现为新号灌完 cookie 后不再需要人工推一把。",
        "endpoints": [
            "/api/accounts/{account_id}/cookie-checks",
            "/api/interaction-backfills",
        ],
        "notes": "invalid / captcha / restricted 三态维持不自动重试——那三态需人工处置,"
                 "继续起浏览器只会把限流催得更狠。",
    },
    {
        "date": "2026-08-06",
        "title": "好镜头转存长期资产库",
        "kind": "feature",
        "summary": "片段产物默认会被 TTL 清道夫收走;满意的镜头可转存成**不过期**的长期资产,"
                   "按 caller 归属可检索、有不过期直链。新增 4 个端点。",
        "endpoints": ["/api/video-assets", "/api/video-assets/{asset_id}"],
        "notes": "转存是**拷贝副本**并挪出清道夫射程,不是改 TTL 标记;拷贝后 verify,"
                 "半截拷贝不会被当成转存成功。",
    },
    {
        "date": "2026-08-06",
        "title": "即梦输入面扩容:多图参考 / 首尾帧 / 多帧故事 / 参考视频 / 参考音频 / 抽帧续接",
        "kind": "feature",
        "summary": "片段提交从「一句提示词(可带一张图)」扩到全输入面:多图参考(锁人物一致性)、"
                   "首尾帧(分镜级运动控制)、multiframe2video 多帧故事(一次出连贯多镜)、"
                   "参考视频与参考音频。另加抽帧端点,把上一段的尾帧当下一段首帧参考做长片续接。",
        "endpoints": [
            "/api/video-clips",
            "/api/video-clips/{clip_id}/frame",
            "/api/video-clip-batches",
        ],
        "notes": "multiframe2video 的模型由平台下发,库里记的 model 是占位符**不是真名**,"
                 "也因此恒不给积分估算;纯音频输入只对 seedance2.5 放行。",
    },
    {
        "date": "2026-08-06",
        "title": "即梦积分估算改按秒线性 + seedance2.5 实测价入表",
        "kind": "fix",
        "summary": "此前按 5s 档向上取整估算,与平台实扣对不上(4s 的 2.5 实扣 104,按档取整会"
                   "算 130)。现改为按秒线性,并回填 seedance2.5 的三次实测单价——该档的低积分 "
                   "warning 从「估不出」变成真能估。",
        "endpoints": ["/api/video-clips", "/api/video-clip-batches"],
        "notes": "价格文案自此由代码现算,不再手写——手写那份在 2.5 回填后整整落后一版,"
                 "运营照它做过预算。",
    },
    {
        "date": "2026-08-05",
        "title": "每次发布无条件打开原创声明",
        "kind": "breaking",
        "summary": "运营裁定:所有发布一律打开原创声明,不再由调用方逐条决定。"
                   "老调用不需要改参数,但发出去的笔记会都带原创声明。",
        "endpoints": ["/api/publish-jobs"],
        "notes": None,
    },
    {
        "date": "2026-08-05",
        "title": "笔记媒体清单入台账(含视频笔记)",
        "kind": "feature",
        "summary": "已发布笔记台账开始存媒体清单——只存**归一化的永久链接**(平台签名链 18 天"
                   "过期,永久链不过期且是原图),按需再下原图。视频笔记同样支持。",
        "endpoints": [
            "/api/accounts/{account_id}/published-notes",
            "/api/published-notes/{note_id}",
            "/api/accounts/{account_id}/note-ledger-syncs",
        ],
        "notes": "媒体从**编辑页**抓不从详情页:详情页有推荐流污染 + 轮播懒加载两个硬伤。"
                 "更老的笔记 URL 没有路径段,单独兼容。",
    },
    {
        "date": "2026-08-05",
        "title": "即梦模型面跟上 CLI 升级:默认档切 seedance2.5",
        "kind": "breaking",
        "summary": "片段生成的 model 枚举补 seedance2.0mini / seedance2.5 两档,"
                   "**默认档由 seedance2.0fast 改为 seedance2.5**——不显式传 model 的调用方"
                   "会拿到不同模型、不同计费。时长上限按模型分档:seedance2.5 为 4-30s,"
                   "其余仍 4-15s,越界当场 422。",
        "endpoints": [
            "/api/video-clips",
            "/api/video-clip-batches",
        ],
        "notes": "seedance2.5 是 VIP-only;首次使用可能需先到即梦网页端完成一次生成做账号级"
                 "合规授权,否则回 AigcComplianceConfirmationRequired(服务端重试无意义)。",
    },
    {
        "date": "2026-08-05",
        "title": "即梦视频生成服务化",
        "kind": "feature",
        "summary": "CLI / 登录态 / 积分 / 提交轮询取片全部进 server,运营侧零登录零装 CLI。"
                   "单镜与批量提交、抽帧续接、积分余额、登录态健康检查共 6 个端点,产物给免鉴权直链。",
        "endpoints": [
            "/api/video-clips",
            "/api/video-clips/{clip_id}",
            "/api/video-clips/{clip_id}/frame",
            "/api/video-clip-batches",
            "/api/video-clip-batches/{batch_id}",
            "/api/video-credits",
            "/api/dreamina-status",
        ],
        "notes": "clip_id 形态钉死 vc_<10hex>,与本机 CLI 的 16 位纯 hex submit_id 不同形——"
                 "auto 模式靠形态判断该问 server 还是问本机 CLI,认错会空转到超时。"
                 "所有重试语义围绕「submit 即占队列位、success 即扣积分」设计:"
                 "卡 querying 数小时**不自动重提**,只如实回 queued_seconds。",
    },
    {
        "date": "2026-08-04",
        "title": "已发布笔记的组件状态只读查询",
        "kind": "feature",
        "summary": "新增只读端点:引用/合集/话题/图数/权限到底有没有真挂上,终于能程序化自证,"
                   "不用再靠人去页面上看。同期把发布后的自动互动收窄为只点赞+收藏(摘掉自动评论)。",
        "endpoints": [
            "/api/accounts/{account_id}/note-component-reads",
            "/api/note-component-reads/{job_id}",
        ],
        "notes": "只读也要起浏览器,同样吃会话额度——逐篇轮询整个账号正是后来触发会话总闸的打法。",
    },
    {
        "date": "2026-08-03",
        "title": "已发布笔记编辑:标题 / 正文 / 图片增删",
        "kind": "feature",
        "summary": "新增编辑端点,标题、正文、图片增删与三组件走**同一入口、同一次提交**。"
                   "任何一步失败即整单弃提交,不会半截落地。",
        "endpoints": [
            "/api/accounts/{account_id}/note-components",
            "/api/note-components/{job_id}",
        ],
        "notes": "话题 chip 是 atomic node,Ctrl+A 选不中,清空靠逐键退格;"
                 "更新页的活动区自 08-03 起被平台收走,已发布笔记**无法补挂活动**。",
    },
    {
        "date": "2026-08-02",
        "title": "发布失败现场截图按 job 取回",
        "kind": "feature",
        "summary": "截图一直在存,缺的只是接出来这一层。发布失败后可按 job 列出各步骤截图并逐张下载,"
                   "定位「卡在哪一步」不必再找人捞服务器。",
        "endpoints": [
            "/api/publish-jobs/{job_id}/artifacts",
            "/api/publish-jobs/{job_id}/artifacts/{name}",
        ],
        "notes": None,
    },
    {
        "date": "2026-08-02",
        "title": "历史笔记互动补量",
        "kind": "feature",
        "summary": "矩阵内互相给历史笔记补点赞收藏,分多轮摊开做。四层闸(日配额 / 篇间抖动 / "
                   "每轮篇数硬上限 / 撞墙即停)都在服务端,**撞墙即停比做完更重要**。",
        "endpoints": ["/api/interaction-backfills", "/api/interaction-backfills/{job_id}"],
        "notes": "非幂等,失败不自动重跑;自动续跑让一批补量走完而不必人天天戳。",
    },
    {
        "date": "2026-07-31",
        "title": "矩阵互动:发布成功后矩阵内其余账号自动点赞/收藏/评论",
        "kind": "feature",
        "summary": "发布成功会自动扇出一批延时互动任务(落库排期,不是领了任务再 sleep 干等)。"
                   "调用方无新参数,但要知道:一次发布不再只消耗发布方一个号的会话额度。",
        "endpoints": ["/api/publish-jobs"],
        "notes": "笔记定位走「发布者主页 + 标题匹配」,匹配不到即放弃,绝不退而求其次点第一篇"
                 "(窗口内可能发了多篇)。风险(同机同出口 IP 把风控图上孤立的号连成完全图)"
                 "已披露并由用户拍板按全员方案执行。",
    },
    {
        "date": "2026-07-28",
        "title": "风格档案多套 + 每套独立版本链",
        "kind": "feature",
        "summary": "在运营与档案之间加了「套」层,版本链挂到套上。6 个既有端点加可选 ?set=,"
                   "**不带 set 的老调用逐字段与此前一致**(读 is_active 套),另加 4 个套管理端点。",
        "endpoints": [
            "/api/style-profile",
            "/api/style-profile/versions",
            "/api/style-profile/versions/{version}",
            "/api/style-profile/rollback",
            "/api/style-profile/admin-default",
            "/api/style-profile/sets",
            "/api/style-profile/sets/{name}",
        ],
        "notes": "base_version / 409 乐观锁语义不变;剩最后一套时拒绝删除。",
    },
]

# ---------------------------------------------------------------------------
# 三、已知边界
#
# 这一段是"能力上线但对方不知道 / 文档写反"这类事故的产物,必须长期可见。
# 收录标准:**当前仍然成立**、且会让调用方做出错误预期的事实。修掉了就删条目
# (顺手在 CHANGELOG_ENTRIES 里记一笔 fix),不要留着当历史。
# ---------------------------------------------------------------------------

KNOWN_LIMITATIONS = [
    {
        "area": "published_notes",
        "what": "**补挂话题在追加场景仍可能间歇失败,最新一版修法尚未经真号复验**:两个真因候选"
                "现在只剩一个 ——「浮层异步渲染晚到」已被 0.20.1 的 ``poll_timeline`` **排除**"
                "(真号三单全程 layers_seen 稳定 14-19 层、8 秒恒定,浮层不是来得晚),"
                "剩下的「话题 chip 累积后光标右移、浮层水平中心滑出正文栏被锚定判据拒错」是"
                "**领先假设**,0.20.2 已照它把判据从中心测试改成相交测试,**但还没有真号数据"
                "证明失败清零**。所以:补挂结果请逐条看 topics_failed,别当 100% 会成;"
                "reason 是 ``topic_dropdown_not_shown`` / ``topic_dropdown_not_found`` 时"
                "**不要换词**(那是我们的定位/时序问题),把失败条目里的 ``poll_timeline``"
                "(含每 tick 的 ``with_items``)、``rejected_with_items`` 与 ``caret_rect`` "
                "一并反馈过来——它们就是为坐实/推翻这个假设而加的取证,**假设一坐实或排除就该"
                "撤掉**,别让它们变成永久字段。"
                "**另一条独立缺陷「聚焦正文框失败」(``content_box_focus_failed``)已修但同样"
                "未经真号**:正文最长的笔记把编辑器矩形撑到探出视口,矩形死中心落点压在底部"
                "固定操作栏上被吞;落点已改「可见区带内偏上」避开它,只在按真号取证几何复刻的"
                "假件上验过。仍收到这个 reason 时请把新增的 ``point_element_chain`` / "
                "``point_inside_editor`` 一并反馈——它俩与 ``poll_timeline`` 同属临时取证字段,"
                "同一条保质期纪律。",
        "why": "这已是同一个缺陷被推翻的第二轮归因:先定「``#`` 粘连前一个话题实体」(补空格,"
               "真号推翻——tail 确实变成 ``[话题]# #失眠`` 浮层照样不弹),再定「浮层晚到」"
               "(条件轮询,真号推翻——8 tick 全程有层、8 秒恒定)。现在这版靠的是同一批"
               "poll_timeline 证据链 + 一个机制假设(浮层挂光标、chip 积多光标右移、中心出栏"
               "被拒),它能自洽解释全部历史现象(位置败非词败 / 按笔记确定性 / 换行即复活),"
               "但**解释力不等于验证**,真号复验之前不写「已修好」。聚焦那半的"
               "证据是三次复现的同一份取证(矩形 y=592 h=260 探出 794 视口、死中心落点离视口底"
               "只剩 72px、选择器命中且滚进过视口),修法对症但同样只有假件几何背书。",
        "since": "2026-08-09",
    },
    {
        "area": "published_notes",
        "what": "补录原创声明(set_original_declaration)的**幂等与编辑页回显已真号验证**"
                "(2026-08-08 账号5):一篇已声明笔记进编辑页读到开态、返回 skipped/already_on、"
                "**零点击零提交**,与 App 目视一致——所以平台**确实在编辑页回显已声明态**,"
                "批量重跑不会把已声明的笔记反复操作。**仍未直接验证的**只有「一篇确实没标记"
                "的笔记从关态→走协议链→变开态」这条完整真声明链(那次首验意外选中的是已声明"
                "笔记),但它复用的是发布链完全同一个 apply_original_declaration 函数、已被发布"
                "链生产数据背书。放量时仍建议**头 2-3 篇人工核对平台标记**,里面只要有一篇是"
                "真没标记的,真声明链就顺带端到端验了。",
        "why": "编辑链此前完全没接过原创声明,08-07 取证跑的是发布页;08-08 补做了编辑页幂等"
               "首验,把「平台是否回显已声明态」这个唯一悬着的风险点坐实了。",
        "since": "2026-08-08",
    },
    {
        "area": "publish",
        "what": "播客(audio)发布链的选择器**已从占位值换成真号取证真值**(音频上传弹窗内部、"
                "发布表单、合集卡三处都拿到了),但**仍未跑过一次真号 e2e 全链**——取证那轮"
                "刻意停在「去发布」之前、没有真的发出去。**剩三处仍未取证**:① 合集卡点开"
                "之后的候选列表/弹窗结构(为控制真号操作范围没有再点),故按名称选合集这一步"
                "仍是 fail-loud,失败只告警不阻断、笔记照发只是不进合集;② 接近 2 小时上限的"
                "长音频有没有额外的转码等待(取证用的是 10 分钟 / 2.35MB,解禁几乎是即时的);"
                "③ **最终发布门**(点「去发布」进发布表单后,等 ``<xhs-publish-btn>`` 的 "
                "submit-disabled 翻转那道 wait_for_submit_enabled)在播客页**从未取证**——它的"
                "判据取自视频页,播客页是不是同款 host 没验过,首跑很可能整条卡死在这道门上。"
                "另外「播客合集上线啦」引导浮层**关不掉**(四种手段实测全无效),现在走的是"
                "「点按钮右侧约 39px 暴露缝穿透它」这条绕过——**这条随窗口尺寸变化而脆**,"
                "算不出缝就退回直接点按钮、那一下大概率点在浮层上并以 file input 找不到收口。",
        "why": "08-08 第三轮真号取证(账号9)走到了发布表单:弹窗里两个 file input 的 class "
               "完全相同、唯一区分是 accept;合集卡真实文案是「加入播客合集」——旧占位猜的"
               "「播客合集 / 选择合集 / 加入合集」三个全部落空。没验过的部分仍如实标未验,"
               "不拿「选择器换了真值」冒充「链路跑通了」。",
        "since": "2026-08-07",
    },
    {
        "area": "publish",
        "what": "**发布时**的视频封面(publish-jobs 的 cover)与**已发布后改封面**"
                "(note-components 的 cover)是两条不同的链,别混:①发布时封面是内联结构、"
                "点「设置封面」不弹窗,上传位懒挂载且形态会变(平台生成候选帧后 .cover-upload "
                "被改名),仍是三层候选兜底、**真号命中未复验**;②已发布改封面是**弹窗**结构"
                "(点缩略图「修改封面」→ 弹窗切「上传封面」tab → 图片 input),**已真号首验通过**"
                "(2026-08-08 账号5「这6种反应」换 3:4 封面成功、App 目视确认),且回读判据已修成"
                "分层(指纹变化是强信号、noCover 读不到不否决)。**改封面只对视频/播客笔记**"
                "(note_type=video),图文传 cover 直接 422(图文封面是第一张图,换首图请用 "
                "add_images/remove_image_indexes)。批量改封面看 applied.cover=true 即真换,"
                "false 才需人工核对。"
                "**内部待办(结构一钉死就该撤)**:发布时封面上传位失败时暂存封面区 "
                "outerHTML(2000 字上限)当取证,这是临时态——真号 e2e 一旦跑出发布页上传位"
                "真实 class,就把这段 dump 降级成 cover_entry_class 结构化字段。落库约定:"
                "结构未知的页型允许留原始 HTML,结构钉死即撤回,别让临时 dump 变成永久债。",
        "why": "两条链的页面形态经真号取证证实不同(发布页内联无弹窗 vs 更新页有弹窗),"
               "共用一套选择器会两头都错。改封面回读曾因判据过严(要指纹变且 noCover 消失"
               "两者)把真换成功报成 false,08-08 真号首验暴露并已修。",
        "since": "2026-08-08",
    },
    {
        "area": "publish",
        "what": "原创声明**近期才第一次真正打通**(此前从未生效过:checked 是乐观态,关掉弹窗不"
                "等于已声明),修复**尚待新一轮真号验证**确认长期稳定。要求原创声明的发布任务"
                "请逐条复核结果里的声明步骤回执,先别当它已经稳。",
        "why": "定位那一轮真号录屏是**发现问题**的那次、发生在修复之前;修复(勾选点位从宽容器"
               "收窄到 16×16 的 simulator 方块、关掉随机偏移)之后没有再跑过真号 e2e。"
               "宽容器上的随机偏移有约 40% 撞进《原创声明须知》链接、被 <a> 吃掉事件,"
               "这个根因已定死,但「改对了」目前只有单测在证。",
        "since": "2026-08-07",
    },
    {
        "area": "publish",
        "what": "话题(标签)在视频发布上连续失配的**根因已定性并修复**(抓成了右侧预览面板"
                "而非话题下拉,判据换成水平锚定正文栏),但修复**尚待新一轮真号 e2e 验证**。"
                "视频任务请逐条复核结果里的 topics_applied / topics_failed,先别当它已经稳;"
                "失败条目的 reason 若是 topic_dropdown_not_found,那是我们没找到下拉(代码问题),"
                "不是这个词不存在,换词没用。",
        "why": "定性靠的是生产取证字段(layer_class=base-info、候选是昵称/关注/编辑于),"
               "根因已定死;但「改对了」目前只有单测在证,视频页真号 e2e 尚未复跑。",
        "since": "2026-08-07",
    },
    {
        "area": "publish",
        "what": "**合集参数在不同端点不同名,别混传**:发布端点 publish-jobs **只认 "
                "collection_id**(合集的 hex id);补挂/移出端点 note-components 额外收 "
                "collection_name(内部解析成 id)。把 collection_name/`--collection-name` 传给"
                "发布端点会被**静默忽略**——2026-08-08 实测:skill 侧发布脚本带 --collection-name "
                "第一轮五单全部没建成合集,去掉后只传 id 才成。发布时请确保传的是 collection_id。",
        "why": "补挂工具为方便支持按名字;发布端点的请求体里根本没有 name 字段,多余字段被 "
               "pydantic 丢弃、不报错,于是「传了没生效」最难查。",
        "since": "2026-08-08",
    },
    {
        "area": "published_notes",
        "what": "判断一条已发布笔记是不是**视频**,认 published_notes 的 **note_type 字段**"
                "(video / normal / null),**不要看 media_json**——media_json 可能没同步到视频"
                "字段而为空,把真视频笔记误当图文(2026-08-08 实测一条 note_type=video 的笔记 "
                "media_json 是空的)。改封面端点的图文/视频校验走的就是 note_type,是对的;"
                "自己判类型时也以它为准。拿不准就用 note-extract 从平台页面拉真实 note_type。",
        "why": "台账同步对视频媒体字段有历史遗漏,note_type 字段一直可靠、media_json 不一定。",
        "since": "2026-08-08",
    },
    {
        "area": "interact",
        "what": "同号浏览器会话有滚动小时窗硬帽(系统任务默认 4/时,运营触发默认 12/时)。"
                "超帽的任务**停在 queued 不动**,既不失败也不报错,实测出现过全矩阵满帽、"
                "单条排 40 分钟以上。**别当卡死重发**——重发只会再灌一条进同一个队列。"
                "轮询响应的 queue 段会直接告诉你排第几、被什么闸住(blocked_by=session_cap 时"
                "附 window_resets_at)、还要等多久,判断前先读它,别靠猜。",
        "why": "风控红线是同号一小时 ≤4-5 次会话,实测一小时 5 次就把号弹上验证墙。"
               "各子系统只守自己的闸,必须在派发层做第二层防御。",
        "since": "2026-08-07",
    },
    {
        "area": "published_notes",
        "what": "「移出合集」点 × 之后**是否有确认弹窗**是未验证点(取证轮没跑到)。"
                "撞见任何可见弹窗即硬失败,并且**打断整单笔记编辑**——同一次调用里"
                "标题/正文/图片等其余改动会一并弃提交,不会半截落地。",
        "why": "确认弹窗的形态/文案/按钮全没取证过,盲点一个按钮的代价可能是把笔记从别的"
               "合集里摘出去、甚至提交一次错的编辑。宁可整单不做,也不做一半。",
        "since": "2026-08-07",
    },
    {
        "area": "published_notes",
        "what": "批量类端点每轮篇数有硬上限,一轮吃不完整批:合集批量清理与互动补量都会返回"
                "remaining 告诉你还剩多少,要靠调用方再发一轮。不要指望一次调用跑完全量。",
        "why": "同上——每篇都要起浏览器会话,一轮跑穿会直接撞会话总闸乃至风控。",
        "since": "2026-08-07",
    },
    {
        "area": "note_extract",
        "what": "不带 with_comments 的提取走纯 HTTP,**服务端没有任何限速闸**(会话总闸管的是"
                "浏览器任务,管不到它)。一口气拆几百篇不会被拦,但暴露的是我方服务器 IP。"
                "调用方必须自觉控频(连续拆解建议间隔数秒);同 note_id 24 小时内走缓存不重复抓。",
        "why": "8 月初已有过运营侧一小时 192 次的洪峰事故,只是那次烧的是账号会话。"
               "这条路径没有天然的资源约束替调用方刹车。",
        "since": "2026-08-07",
    },
    {
        "area": "note_extract",
        "what": "评论抓取失败时**不是一律可重发**:虽然整个提取是纯只读、幂等,但 reason 以 "
                "wall_ 开头(如 wall_scan_qr)表示该号撞了验证墙,此时禁止重发,"
                "要先按墙型处置该号(wall_scan_qr 需人工扫码解墙)。其余 error 可直接重发。",
        "why": "撞墙即重发是把号往风控深处推的打法。首版 manifest 把「撞墙」和「失败可直接重发」"
               "两句挨着写,照着写代码就会得到一个撞墙重试循环。",
        "since": "2026-08-07",
    },
    {
        "area": "media_gen",
        "what": "部分模型档**没有实测单价**(multiframe2video 更是模型由平台下发、恒不估),"
                "提交时的低积分 warning 对它们估不出来。铁律:**不带 warning 是「估不出」"
                "不是「余额充足」**,别拿它当余额判据,要看余额自己查 /api/video-credits。"
                "当前哪些档有价由 manifest 的价格文案现算给出,别在别处抄一份。",
        "why": "没实测过就编一个「看着合理」的数,只会让 warning 给出假估算、运营照它做预算。"
               "价格文案曾被手写过一份,2.5 回填实测价后那份整整落后一版——所以这里只讲规则"
               "不抄数字。",
        "since": "2026-08-05",
    },
    {
        "area": "media_gen",
        "what": "片段任务卡在 querying 数小时时服务端**不会自动重提**,只如实回 queued_seconds;"
                "submit 超时或拿不到 submit_id 的歧义结局一律判 error 并写明「是否已入队未知」,"
                "同样不重排。调用方也不该自行重发同一镜。",
        "why": "submit 即占队列位、success 即扣积分,排队中无法取消。重排 = 赌它没入队,"
               "赌输就是双倍扣分。",
        "since": "2026-08-05",
    },
]


@lru_cache(maxsize=1)
def _git_revision() -> str | None:
    """本代码所在仓库的最近 commit 短哈希;取不到回 None(不是致命信息,不许抛)。

    进程内只跑一次:版本在进程生命周期里不会变,每请求 fork 一次 git 是纯浪费。
    cwd 锚在本文件目录而不是进程 cwd —— 否则从别处启动时会报出**另一个仓库**的哈希,
    那比 None 更糟(拿着错哈希去对代码,比知道自己不知道更害人)。
    """
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=Path(__file__).resolve().parent,
            capture_output=True, text=True, timeout=2, check=True,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout.strip() or None


def _capabilities(entries: list[dict]) -> list[dict]:
    """按 CAPABILITY_GROUPS 把 manifest 端点摊成分组视图(只带 method/path/summary)。

    详情不内联,避免 guide 变成 manifest 的第二份拷贝——两份长文案必然走向不一致。
    """
    by_path: dict[str, list[dict]] = {}
    for e in entries:
        by_path.setdefault(e["path"], []).append(e)
    return [
        {
            "key": g["key"],
            "title": g["title"],
            "summary": g["summary"],
            "endpoints": [
                {"method": e["method"], "path": e["path"], "summary": e["summary"]}
                for path in g["paths"]
                for e in by_path.get(path, [])
            ],
        }
        for g in CAPABILITY_GROUPS
    ]


MANIFEST_ENTRIES = [{
    "method": "GET", "path": "/api/guide",
    "summary": "统一指南:能力域分组 + 变更记录 + 已知边界(manifest 的超集视角)",
    "admin_only": False, "params": {},
    "returns": "{guide_contract_version, meta, see, capabilities, changelog, known_limitations}",
    "errors": "401=apikey 缺失/无效/停用",
    "notes": "manifest 回答「这个端点怎么调」,guide 回答「有哪些能力域、最近改了什么、"
             "哪些地方现在还不能指望」。上手读 manifest,每次开工前读 guide 的 changelog "
             "前几条与 known_limitations。端点详情不在这里,按 see 指针回 /api/manifest。",
}]


@router.get("/api/guide")
async def guide() -> dict:
    """统一指南(须鉴权,与 manifest 同款)。"""
    # 延迟导入聚合表,避免与 app.http 包 __init__ 循环导入(与 manifest.py 同因)。
    from app.http import ALL_MANIFEST_ENTRIES

    return {
        "guide_contract_version": GUIDE_CONTRACT_VERSION,
        "meta": {
            "server_version": __version__,
            "git_revision": _git_revision(),
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "manifest_entry_count": len(ALL_MANIFEST_ENTRIES),
            "guide_contract_version": GUIDE_CONTRACT_VERSION,
            "changelog_covers_since": CHANGELOG_COVERAGE_SINCE,
        },
        "see": _SEE,
        "capabilities": _capabilities(ALL_MANIFEST_ENTRIES),
        "changelog": CHANGELOG_ENTRIES,
        "known_limitations": KNOWN_LIMITATIONS,
    }
