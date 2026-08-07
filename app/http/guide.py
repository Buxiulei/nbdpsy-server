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
                   "可见性切换、删除、合集清理。写类操作会真起浏览器改动线上内容。",
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
                 "三处浏览器选择器仍是占位值,详见 known_limitations 里 area=publish 的播客那条。",
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
        "area": "publish",
        "what": "播客(audio)发布链的三处浏览器控件——音频上传弹窗内部、「去发布」之后的发布表单、"
                "表单里的合集选择——选择器目前仍是**占位值**,零真号 fixtures"
                "(已取证的只有:发播客 tab 的切换判据、首屏上传区文案与红色「上传音频」按钮、"
                "合集创建页)。三处均写成 fail-loud:定位不到就带当场取证报 error,绝不静默假装做过。"
                "首次真号发播客**大概率直接失败**,那是预期内的取证跑,不是回归。",
        "why": "两轮真号取证都被「播客合集上线啦」引导浮层挡住(它正压在上传按钮上);"
               "且发播客 tab 首屏 input[type=file] 实测为 0,说明 input 是点开弹窗后才挂的,"
               "隔着浮层根本抓不到。宁可首跑硬失败,也不能让调用方以为发出去了。",
        "since": "2026-08-07",
    },
    {
        "area": "publish",
        "what": "视频自定义封面(cover)的上传位选择器**至今没有一次真号命中**:最近一轮真号 "
                "e2e 仍是 cover_upload_entry_not_found,之后扩成的三层候选(class → 文案 → "
                "尺寸 tile)尚未经真号复跑。封面设置失败**不阻断发布**,会退回平台自动截取的"
                "第一帧——任务照样落 published,所以**不能拿 status 当封面设上了的证据**,"
                "要看任务结果里的封面步骤字段。",
        "why": "上传位是懒挂载且形态会变:平台自动生成 3 张候选帧之后它被改名/换形"
               "(实测那一刻封面区里 file_inputs=0、.cover-upload 也不在了),存在竞态窗口。"
               "注:结构已由真号探针证实是**内联**的、点「设置封面」不弹任何弹窗——"
               "这条别再花成本重验。",
        "since": "2026-08-07",
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
        "what": "话题(标签)在视频发布上连续失配:一轮真号 e2e 里 6/6 全 no_exact_match,"
                "同期图文是 171/181 成功。原因未定性,当前只把现场证据(浮层实际候选文案、"
                "候选条数、容器 class)带回任务结果供下一次定性。",
        "why": "两种可能尚未分辨:搜索没触发(候选是一排默认推荐话题)/ 词本身在平台不存在"
               "(候选是相关词但差一点)。不猜着改逻辑。",
        "since": "2026-08-07",
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
