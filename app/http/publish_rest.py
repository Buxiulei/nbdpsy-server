"""publish 分组 REST:建发布任务(202)/ 查状态 / 列任务 / 取消。

端点体与 app/tools/publish.py 的 4 个 MCP 工具逐行对齐(平移自那里),仅两处改动:
①"发布任务 … 不存在"从裸 ValueError 改为 NotFoundError(→ 404,而非 400);
②入参从工具签名改为请求体 Pydantic 模型 / query 参数。

images/topics 序列化成 images_json/topics_json 落库;images 每项为 URL/base64(远程 agent
供图),到发布 runner 里再由 materialize_images 落成本地文件,本端点不碰浏览器。

视频笔记走 ``video`` 字段、播客笔记走 ``audio`` 字段(与 images **三选一**),存的都是
**服务器侧文件路径**,分别落 ``video_path`` / ``audio_path`` 列 —— 这类文件动辄几百 MB 到
1GB,既不走请求体也不需要物料化,发布 runner 直接拿路径 set_input_files。路径的存在性、
扩展名、体积与(音频的)时长在入参校验层就查掉(422),不造注定失败的 pending 任务。
"""

import json
import random
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, model_validator
from sqlalchemy import func, select, update

from app.auth.context import AccessDenied, current_operator
from app.auth.guards import assert_account_access, visible_account_ids
from app.core.config import settings
from app.core.db import get_session
from app.core.errors import NotFoundError
from app.models.publish_job import PublishJob
from app.models.xhs_account import XhsAccount
from app.publish.policy import (
    XHS_COVER_EXTENSIONS,
    XHS_VIDEO_EXTENSIONS,
    audio_cover_reject,
    audio_reject,
    cover_ext_allowed,
    video_ext_allowed,
)
from app.publish.runtime import get_active_scheduler
from app.http.job_polling import QUEUE_MANIFEST_NOTE
from app.services import counselor_quote, queue_status
from app.services.quota import assert_operator_quota

# 发布任务状态枚举(与 DB / 调度器生命周期一致):校验 list_publish_jobs 的 status 入参用。
_JOB_STATUSES = ("pending", "publishing", "published", "failed", "canceled")
# 图文笔记图片张数硬上限(小红书图文最多 18 张);下限为 1(纯图文,无图不成立)。
_MAX_IMAGES = 18
# 播客合集名称长度上限(实拍确认:创建页 input 的 maxlength="20")
_MAX_PODCAST_COLLECTION_NAME = 20


def _parse_schedule_time(raw: str | None) -> datetime | None:
    """把 ISO8601 schedule_time 解析为 **naive UTC**(与模型/调度器统一的 utcnow 基准一致)。

    tz-aware 输入(如 ``2026-01-01T09:00:00+08:00``)先 astimezone(UTC) 再去掉 tzinfo,存成
    naive UTC(此例 → 01:00);naive 输入原样返回。否则带 +08:00 的定时时刻会被 scan_once
    的 ``utcnow()`` 当 UTC 直接比较,早/晚 8 小时发布。
    """
    if not raw:
        return None
    dt = datetime.fromisoformat(raw)
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def _next_active_window_start(now: datetime) -> datetime:
    """算次日活跃窗口起点(naive UTC,带抖动):次日 ``PUBLISH_ACTIVE_WINDOW_START_UTC_HOUR``
    整点 + ``random.uniform(0, PUBLISH_ACTIVE_WINDOW_JITTER_SEC)``。抖动避免整点节律指纹。

    供每账号每日上限达标后顺延新任务用——不丢 job,改落到次日窗口的 pending 定时任务。
    """
    base = (now + timedelta(days=1)).replace(
        hour=settings.PUBLISH_ACTIVE_WINDOW_START_UTC_HOUR,
        minute=0,
        second=0,
        microsecond=0,
    )
    jitter = random.uniform(0, settings.PUBLISH_ACTIVE_WINDOW_JITTER_SEC)
    return base + timedelta(seconds=jitter)


def _job_view(job: PublishJob) -> dict:
    """把发布任务序列化为对外视图(不含图片/正文等大字段,只给调度可读的元信息)。

    时间字段(schedule_time/next_retry_at/created_at)库里存的是 **naive UTC**(与调度器
    utcnow 基准一致)。读回时显式补上 ``+00:00`` 偏移:数值不变,只把这个 naive datetime
    本就代表的 UTC 时区标注出来,消除"裸串被当本地时间"的歧义——消费方(含非北京时区)可自行
    astimezone 到目标时区。不转成 +08:00,避免把 Beijing-only 假设硬编码进通用 REST 契约,
    也避免与存储/调度器一直锚定 UTC 的语义分裂(详见入口 _parse_schedule_time)。
    """
    return {
        "job_id": job.id,
        "account_id": job.account_id,
        "title": job.title,
        "status": job.status,
        "note_id": job.note_id,
        "note_url": job.note_url,
        "error": job.error,
        "retries": job.retries,
        "schedule_time": (
            job.schedule_time.replace(tzinfo=timezone.utc).isoformat()
            if job.schedule_time
            else None
        ),
        "next_retry_at": (
            job.next_retry_at.replace(tzinfo=timezone.utc).isoformat()
            if job.next_retry_at
            else None
        ),
        "created_at": (
            job.created_at.replace(tzinfo=timezone.utc).isoformat()
            if job.created_at
            else None
        ),
        **_pending_explain(job),
        **_applied_echo(job),
    }


def _applied_echo(job: PublishJob) -> dict:
    """发布结果回显:服务端**实际应用**的话题逐个成败与三组件逐项结果。

    为什么必须有(2026-08-03 运营上报):参数被静默丢弃(文字版话题全丢)时,调用方
    只能等笔记发出去、人工读正文才察觉 —— 运营为验证这点白删了一篇笔记。有了回显,
    `topics_applied: []` 一眼可见。NULL(本功能上线前发布的)不下发该键,别把"没记录"
    渲染成"什么都没应用"。
    """
    raw = getattr(job, "result_json", None)
    if not raw:
        return {}
    try:
        return {"applied": json.loads(raw)}
    except (TypeError, ValueError):
        return {"applied_unreadable": True}


# pending 长期不动的两种情形,**表象一模一样**,后果却完全相反:
#   ① 定时任务在等它自己的点 —— 正常,不该动它;
#   ② 已到点却始终没被派发 —— 异常,要查。
# 2026-08-03 运营就是分不清这两者,把一批排到 08-08 的定时稿当成"卡了 4 天的僵尸任务",
# 进而要求"pending 超 30 分钟自动置 failed" —— **那会把定时发布整个功能杀死**
# (每篇定时稿都在创建 30 分钟后、远早于发布时间被判失败)。
# 所以这里不是锦上添花:**接口必须自己说清楚它在等什么**,否则误判必然重演。
_WAITING_SCHEDULE = "waiting_schedule"
_WAITING_RETRY = "waiting_retry"
_DUE = "due"
# 已到点却迟迟没派发,超过这个秒数就明说"不正常"(派发周期只有 5s,给到 30 分钟极宽松)
_OVERDUE_ALERT_SECONDS = 1800


def _pending_explain(job: PublishJob) -> dict:
    """给 pending 任务附一句"它到底在等什么",终态任务不附。

    ``pending_reason`` 三态 + ``pending_seconds_remaining``(还要等多久,已到点为 0)+
    ``pending_overdue``(已到点且等超阈值 = 真异常,值得查)。
    """
    if job.status != "pending":
        return {}
    now = datetime.utcnow()
    waits = []
    if job.schedule_time and job.schedule_time > now:
        waits.append((_WAITING_SCHEDULE, job.schedule_time))
    if job.next_retry_at and job.next_retry_at > now:
        waits.append((_WAITING_RETRY, job.next_retry_at))
    if waits:
        reason, until = max(waits, key=lambda x: x[1])
        remaining = int((until - now).total_seconds())
        return {
            "pending_reason": reason,
            "pending_until": until.replace(tzinfo=timezone.utc).isoformat(),
            "pending_seconds_remaining": remaining,
            "pending_overdue": False,
            "pending_hint": (
                f"按计划等待中,还有约 {remaining // 3600} 小时 {(remaining % 3600) // 60} 分钟到点;"
                "这是正常状态,不要当成卡死"
            ),
        }
    # 已到点:派发周期 5 秒,正常情况下几乎立刻会被领走
    waited = int((now - (job.created_at or now)).total_seconds())
    overdue = waited > _OVERDUE_ALERT_SECONDS
    return {
        "pending_reason": _DUE,
        "pending_until": None,
        "pending_seconds_remaining": 0,
        "pending_overdue": overdue,
        "pending_hint": (
            f"已到点但等了 {waited // 60} 分钟还没被派发,**不正常**,请查 worker"
            if overdue else "已到点,等待派发(通常几秒内)"
        ),
    }


router = APIRouter()

MANIFEST_ENTRIES = [
    {
        "method": "POST", "path": "/api/publish-jobs",
        "summary": "发布一条小红书笔记:图文(images)/ 视频(video)/ 播客(audio),三选一"
                    "(异步入队,需对该账号有 access)",
        "admin_only": False,
        "params": {
            "account_id": "body,int|None(**省略/null = 广播**:给每个代管账号"
                           "(GET /api/managed-accounts 里 managed=true 的)各建一条发布任务;"
                           "传了就只发这一个号(「特指账号」)。广播时你必须对这些号有 access,"
                           "一个都没授权 → 403;系统里一个代管账号都没有 → 422",
            "title": "body,str(显示长度截断 ≤20,静默不报错)",
            "content": "body,str(截断 ≤900,静默不报错)",
            "images": "body,list|None(图文笔记走这个;1-18 项,越界立即 400);每项三形态之一:"
                       "http(s) URL 字符串 / data URI 字符串 / {b64, ext} 对象;"
                       "**与 video / audio 互斥,三选一必填**(多给或都不给都 422)",
            "video": "body,str|None(视频笔记走这个;**服务器侧文件路径**,不是 URL 也不是 "
                      "base64 —— 视频动辄几百 MB,先把文件落到本服务器再把路径传进来)。"
                      "支持 .mp4/.mov/.flv/.f4v/.mkv/.rm/.rmvb/.m4v/.mpg/.mpeg/.ts;"
                      "扩展名不在此列 → 422,路径不存在/不可读 → 422;"
                      "**与 images / audio 互斥,三选一必填**(多给或都不给都 422)",
            "audio": "body,str|None(**播客笔记**走这个;**服务器侧文件路径**,语义同 video)。"
                      "支持 .m4a/.mp3/.wav/.flac/.aac;**时长必须在 10 分钟~2 小时之间**"
                      "(闭区间)、**大小 ≤1GB** —— 四条准入(文件存在 / 扩展名 / 体积 / 时长)"
                      "任一不过当场 422 且不建 job,报错点名是哪一条。时长由服务端 ffprobe 现读,"
                      "**读不出时长一律拒收**(读不出来多半不是有效音频,放行只会造一条注定失败"
                      "的任务)。GB 级音频请走分片上传 POST /api/uploads/media-sessions,"
                      "complete 返回的 path 就填这里;**与 images / video 互斥,三选一必填**",
            "cover": "body,str|None(**视频或播客任务**的自定义封面图,服务器侧图片路径,语义同 "
                      "video)。支持 .jpg/.jpeg/.png/.webp;扩展名不在此列或文件不存在 → 422;"
                      "**体积上限按任务类型分档**:视频封面不限、播客音频封面 ≤32MB(平台规格),"
                      "超限 422。**图文任务传 cover → 422**(图文的封面就是第一张图,没有独立"
                      "封面这个概念)。不传:视频 = 用平台自动截取的第一帧,播客 = 不设封面。"
                      "封面图很小,不必走分片:POST /api/uploads/images 传完即落在 "
                      "DATA_DIR/uploads/{batch_id}/NN.ext,把那个服务器侧路径填进来即可",
            "podcast_collection": "body,str|None(**仅播客任务**:发布时把这一集加进哪个播客合集,"
                                   "传**合集名称**不是 id,≤20 字)。合集要先建:"
                                   "POST /api/accounts/{id}/podcast-collections。"
                                   "图文/视频任务传它 → 422(它是播客发布表单独有的控件);"
                                   "与 collection_id 同时给也 → 422(两者共用同一个落库字段)。"
                                   "⚠️ **发布表单里的合集选择控件尚未经真号取证**,设置失败"
                                   "**不阻断发布**(笔记照发、不进合集),结果见 "
                                   "applied.components.podcast_collection",
            "topics": "body,list[str]|None(默认[];去重后截断 ≤10,静默不报错)",
            "schedule_time": "body,str|None(ISO8601,务必带时区偏移,如 "
                              "2026-01-01T09:00:00+08:00;不传则立即入队;不带偏移按 UTC 解释)",
            "collection_id": "body,str|None(加入合集,取自 GET /api/accounts/{id}/collections)",
            "quoted_note_id": "body,str|None(引用本号的哪篇笔记;**优先级高于 related_counselor**;"
                              "**广播时不可给**——笔记 id 归属单账号,给了 422)",
            "activity_id": "body,str|None(关联活动,取自 GET /api/accounts/{id}/activities)",
            "related_counselor": "body,str|None(这篇推介哪位咨询师的姓名,如「李宇」;"
                                  "没给 quoted_note_id 时据它自动推导该引用哪篇笔记)",
            "note_purpose": "body,str|None(这篇笔记的核心目的,**给以后调用它的 agent 读**;"
                             "推荐词表:推介咨询师/概念解读/案例剖析/热点分析/互动引导/"
                             "个人记录/其他,词表会扩,传别的词也收)",
        },
        "returns": "指定账号:{job_id, status:'pending'};"
                    "广播(省略 account_id):{broadcast:true, jobs:[{account_id, job_id}, ...]}"
                    "——逐条按各自账号的会话闸/日上限调度,轮询各自的 job_id",
        "errors": "400=显式给了 images 但为空数组或超 18 张;"
                  "422=images / video / audio 多给或都不给 / video、audio、cover 格式不支持 / "
                  "三者任一文件不存在 / audio 时长越界或读不出 / audio 超 1GB / "
                  "播客封面超 32MB / 图文任务传了 cover / 非播客任务传了 podcast_collection / "
                  "podcast_collection 与 collection_id 同时给 / **广播时系统里没有代管账号** / "
                  "**广播时显式给了 quoted_note_id**;"
                  "403=无该账号 access(广播时=你对代管账号一个都没授权)",
        "notes": "**不传 account_id = 广播给全部代管账号**(运营侧默认动作:"
                 "「除非特指账号,默认所有代管账号一起发」)。广播不是一条任务发多个号,"
                 "而是**每号各一条独立任务**:各自排队、各自受本号的每日上限与会话闸约束、"
                 "各自重试、各自可 cancel/PATCH,任何一个号失败都不影响别的号。响应里的 "
                 "jobs 数组就是逐号 job_id,照常一个个轮询。谁是代管账号看 "
                 "GET /api/managed-accounts,改用 PUT /api/accounts/{account_id}/managed。"
                 "**引用推导与日上限顺延都是逐号各算的**——A 号今天到量顺延到次日窗口,"
                 "不牵连 B 号;引用只引本账号自己的咨询师推介笔记(见下)。"
                 "所以**广播时不许给 quoted_note_id**(给了 422):笔记 id 归属单个账号,"
                 "同一个 id 发给 N 个号最多只有它的主人引得上,其余是「引用悄悄没生效、笔记"
                 "照常发出去」的静默失败。跨号引用请传 related_counselor 让每号各自推导。"
                 "异步契约:拿到 job_id 后每 5-10s 调 GET /api/publish-jobs/{job_id} 轮询,直到 "
                 "published/failed;publishing 常态耗时 1-3 分钟;失败自动重试(最多 3 次,退避约 "
                 "2/10/30 分钟),单条任务最长约 40 分钟才会落 failed。同一账号的发布自动串行。"
                 "**视频笔记(video)与播客笔记(audio)都与图文共用本请求体的每一个字段** "
                 "—— 不是窄版接口:title / content / topics / schedule_time / quoted_note_id / "
                 "activity_id / related_counselor / note_purpose **全部照常生效**,语义、"
                 "截断规则、引用推导四条规则(见下)、重试退避、账号串行、每日上限一字不差"
                 "(合集那一项播客走 podcast_collection、其余走 collection_id)。"
                 "唯一区别是媒体那一步:视频要等平台上传+转码完成才能继续录入,故 publishing "
                 "阶段比图文长(取决于文件大小,服务端等待上限按体积自动伸缩,见 "
                 "VIDEO_UPLOAD_TIMEOUT_* 配置);播客同理 —— 音频上传完才会放行「去发布」,"
                 "1GB 上限下 publishing 可能到 20 分钟级,轮询别设短超时。"
                 "**封面**:传 cover 用你的自定义封面;不传时视频用平台自动截取的第一帧、"
                 "播客不设封面。**封面设置失败绝不阻断发布**(与三组件同语义):笔记照发、"
                 "退回平台默认,失败原因在 `applied.components.cover.status='error'` 与 "
                 "`.reason` 里可查(所以传了 cover 的任务发完请顺手看一眼这个字段,"
                 "别默认封面一定换上了);"
                 "视频页独有的「添加章节 / 关联直播预告」不设置。"
                 "⚠️ **播客链路的取证覆盖度仍低于图文/视频**:「发播客」tab、播客合集创建页、"
                 "**音频上传弹窗内部、「去发布」之后的发布表单、合集卡**均已真号取证换成真值;"
                 "但**合集卡点开之后的候选结构**仍未取证,那一步继续 fail-loud —— 定位不到就带"
                 "当场取证报错,**绝不静默假装做过**;媒体步失败即整条任务失败进重试,合集这类"
                 "辅助步失败只告警。另注:压在「上传音频」按钮上的引导浮层**关不掉**(四种手段"
                 "实测全无效),现走「点按钮右侧暴露缝穿透」的绕过,窗口尺寸变化时它会失效。"
                 "播客发布**尚未跑过真号 e2e 全链**(取证刻意停在「去发布」之前没真发);"
                 "**尤其最终发布门**——点「去发布」进发布表单后等 `<xhs-publish-btn>` 的 "
                 "submit-disabled 翻转那道门,在播客页从未取证(判据取自视频页),首跑可能整条"
                 "卡死在这里。"
                 "**关联活动**:视频页与图文页同源(内联区渲染约 2 张推荐活动卡 + 区标题右侧"
                 "「更多」入口)。传的 activity_id 若不在推荐位,服务端会自动点开「更多活动」"
                 "面板滚动查找 —— **不必挑推荐位里的活动**。设置失败**不阻断发布**,笔记照发,"
                 "reason 前缀可自判:`activity_card_not_found`=活动区在但没有这个活动"
                 "(多半已下线,重新拉活动列表);`activity_section_absent`=活动卡/容器/区标题"
                 "三样全无,疑该页型没有活动区(不该出现,遇到请带 job_id 上报)。"
                 "三组件(collection_id / quoted_note_id / activity_id)在发布链路里于话题之后、"
                 "点发布之前设置,**失败只告警不阻断发布**(图都传完了不为辅助组件废掉整篇)。"
                 "组件逐项结果**发出去之后能查**:成功后 GET /api/publish-jobs/{job_id} 的 "
                 "applied.components 给每项 status(202 响应体里当然没有,那时还没开始发)。"
                 "但要注意那是**编辑器内**回读,逮不住服务端静默丢弃(私密笔记的合集绑定),"
                 "要板上钉钉得事后调 POST /api/accounts/{id}/note-components 复核或补设。"
                 "**「原创声明」每次发布无条件打开,不用传任何参数**(运营裁定 2026-08-05):"
                 "已是开态就零点击,结果同样在 applied.components.original_declaration 里。"
                 "⚠️ activity_id 会让平台把该活动的话题**追加**进正文(话题名可能与活动名不同)。"
                 "**引用哪篇笔记可以不用自己算**:不传 quoted_note_id 时按四条规则自动推导"
                 "(建 job 那一刻算完落库):① 标题形如「X咨询师-姓名，…」= 这篇本身就是咨询师"
                 "推介笔记 → 引用「接待员联系方式」那篇(**这条最先判**,所以给推介笔记传 "
                 "related_counselor 也不会让它引用自己);② 传了 related_counselor → 引用"
                 "**本账号**该咨询师的公开推介笔记;③ 标题里提到某位已知咨询师 → 同上;"
                 "④ 都不满足 → 不引用。**只引用 permission_code=0 的公开笔记,推不出来一律"
                 "留空绝不猜**。⚠️ **只会引用本账号自己的咨询师推介笔记**:每个账号背后是"
                 "不同运营,从该账号来的客户算其 KPI,跨账号引用等于把客户导到别人名下抢其"
                 "绩效,故本账号没有该咨询师的公开推介笔记时**留空,绝不跨账号兜底**。唯一"
                 "例外是接待员联系方式那篇(含二维码有违规风险,集中在单一账号,由服务端配置"
                 "指定;**未配置时规则①同样留空不引用**)。两者都传以显式 quoted_note_id 为准。"
                 "**note_purpose 建议每篇都传**:它是发布当场随任务落进笔记台账的核心目的"
                 "(台账记 purpose_source='declared'),以后 agent 要引用/评论/排期这篇笔记时"
                 "靠它判断意图。不传也能发,台账里那两列留 null,事后系统只能从正文推断"
                 "(purpose_source='inferred',可信度低于你亲口声明的)。",
    },
    {
        "method": "GET", "path": "/api/publish-jobs/{job_id}",
        "queue": QUEUE_MANIFEST_NOTE,
        "summary": "轮询发布任务状态(caller 须对该 job 的账号有 access)",
        "admin_only": False, "params": {"job_id": "path,int"},
        "returns": "{job_id, account_id, title, status, note_id, note_url, error, "
                    "retries, schedule_time, next_retry_at, created_at, "
                    "applied?:{topics_requested, topics_applied, topics_failed, "
                    "components:{collection?/quote?/activity?/original_declaration: "
                    "{status, ...}}}}",
        "errors": "403=无该账号 access;404=job 不存在",
        "notes": "status 枚举五态:pending(排队中,含定时未到期/失败等待重试)、publishing"
                 "(发布中,常态 1-3 分钟)、published(成功,保证有 note_url,note_id 可能为空)、"
                 "failed(重试耗尽后的终态,error 给最后一次失败原因)、canceled(被 cancel 取消)。"
                 "next_retry_at 是失败后回 pending 的下次重试时刻(未安排重试则为 null);"
                 "retries 是已重试次数。轮询节奏建议每 5-10s 一次直到 published/failed。"
                 "schedule_time/next_retry_at/created_at 读回均带 +00:00 显式 UTC 偏移"
                 "(如 2026-01-01T01:00:00+00:00),即该时刻的 UTC 值;要看本地时间自行 +8 小时。"
                 "── **applied 是「服务端到底应用了什么」的回显**,只有 published 的任务才有"
                 "(本功能上线前发布的老任务不下发该键,别把「没记录」渲染成「什么都没应用」)。"
                 "topics_requested vs topics_applied 一对比就知道话题有没有被平台静默丢弃。"
                 "topics_failed 的每条除 tag/reason 外还带**当场取证**:`candidates`="
                 "话题下拉浮层里实际枚举到的候选文案(前 10 条)、`item_count`=候选总数、"
                 "`layer_class`=浮层容器 class、`editor_tail`=正文框末尾回读。"
                 "reason=`no_exact_match` 时看这几项即可定性:候选是一排不相干的默认推荐话题 "
                 "→ 搜索没被触发;候选确是相关词但差一点 → 这个话题平台真没有,换词即可。"
                 "components 是逐项组件结果,键只含**请求过的**组件,外加一个恒有的 "
                 "**original_declaration ——「原创声明」每次发布无条件打开,不需要传参**。"
                 "它和其余组件同为三态:done=点开成功;skipped=**本来就是开的,零点击**"
                 "(同样算成功,别当成「跳过没做」);error=没开成,**不阻断发布**,笔记照发,"
                 "reason 给原因,遇到请带 job_id 上报。"
                 "组件回读都是**编辑器内**确认,逮不住服务端静默丢弃,要板上钉钉见 "
                 "POST /api/publish-jobs 的 notes。"
                 "视频笔记的 applied 结构与图文完全一致(话题回显 + 同一组 components 键)。"
                 "activity 成功时带 via 字段:`inline`=在内联推荐位直接点上的;"
                 "`more_panel`=推荐位里没有,服务端点开「更多活动」面板找到并关联的。"
                 "失败的 error 分两种,reason 前缀能区分:`activity_card_not_found`="
                 "活动区在(内联卡数与面板是否试过都写在 reason 里)但没有这个活动,"
                 "多半已下线,重新拉活动列表即可;`activity_section_absent`=活动卡/容器/"
                 "区标题三样全无,疑该页型没有活动区,带 job_id 上报。",
    },
    {
        "method": "GET", "path": "/api/publish-jobs",
        "summary": "列发布任务(按 caller 可见账号过滤,admin 全见)",
        "admin_only": False,
        "params": {
            "account_id": "query,int|None(显式鉴权;越权 403)",
            "status": "query,str|None(pending|publishing|published|failed|canceled;"
                      "非法值 400,而非静默返回空)",
            "limit": "query,int(默认 50,按新→旧取前 N)",
        },
        "returns": "{jobs: [同 GET /api/publish-jobs/{job_id} 的单条视图, ...]}",
        "errors": "400=status 非法;403=account_id 越权",
        "notes": "",
    },
    {
        "method": "POST", "path": "/api/publish-jobs/{job_id}/cancel",
        "summary": "取消发布任务(仅 pending 可取消,置 canceled)",
        "admin_only": False, "params": {"job_id": "path,int"},
        "returns": "{ok:true} 成功取消;{ok:false, status:<当前状态>} 非 pending 取消不了",
        "errors": "403=无该账号 access;404=job 不存在",
        "notes": "",
    },
    {
        "method": "PATCH", "path": "/api/publish-jobs/{job_id}",
        "summary": "原地修改待发(pending)定时任务:改时间/标题/正文/图片/话题",
        "admin_only": False,
        "params": {
            "job_id": "path,int",
            "title": "body,str|None(省略=不改)",
            "content": "body,str|None(省略=不改)",
            "images": "body,list|None(省略=不改;传则 1-18 项,越界 400)",
            "topics": "body,list[str]|None(省略=不改)",
            "schedule_time": "body,str|None(省略=不改;显式 null=清空转立即发;"
                              "ISO8601 带时区如 2026-01-01T09:00:00+08:00)",
        },
        "returns": "{ok:true, job:<同 GET 单条视图>} 改成功;{ok:false, status:<当前态>} 非 pending 改不了",
        "errors": "400=images 越界;422=给**视频或播客任务**传了 images(见 notes);"
                  "403=无该账号 access;404=job 不存在",
        "notes": "仅 pending 可改(定时未到期/失败等待重试均属 pending);publishing/published/failed/"
                 "canceled 一律 ok:false。已在发/已终态的任务改不动,需另建新任务。空请求体 {} 为 no-op "
                 "返 ok:true;schedule_time 传空串等价 null(清空转立即发)。"
                 "**视频/播客任务能改什么**:title / content / topics / schedule_time 与图文"
                 "任务**完全一样**,照常可改。**只有 images 是硬拒**:给视频任务传 images 一律 "
                 "422「视频任务不可改图片,请取消后重建」、给播客任务一律 422「播客任务不可改"
                 "图片,请取消后重建」,images_json 与 video_path / audio_path 都一个字节不动"
                 "(空数组同样拒——显式传这个字段就是在选图文那条路)。"
                 "为什么硬拒而不是照写:runner 是按 audio_path / video_path 路由的,images "
                 "写进去也永远不生效,你却会拿到 ok:true —— 那是比报错危险得多的静默态。"
                 "本端点没有 video / audio 参数,所以反方向(图文任务想变视频/播客)自然不可达。"
                 "要换媒体:cancel 掉再建一条新的。",
    },
]


class PublishNoteRequest(BaseModel):
    # 发哪个号。**省略/null = 广播给全部代管账号**(managed=true),这是运营侧的默认动作:
    # 「除非特指账号,默认所有代管账号一起发」。传了就是「特指账号」,行为与上线前一字不变。
    account_id: int | None = None
    title: str
    content: str
    # 图文笔记的图片(URL / data URI / {b64, ext});与 video 互斥,二选一必填。
    # 上线视频发布前它是必填字段,现在改成可省略 —— 省略 = 选了视频那条路,
    # 由 _check_media_exclusive 把"两个都没给"挡在 422。
    images: list | None = None
    # 视频笔记的**服务器侧**文件路径(不是 URL、不是 base64:视频动辄几百 MB,
    # 走请求体传输不现实,由调用方先落到服务器再把路径给我们)。与 images 互斥。
    video: str | None = None
    # 播客笔记的**服务器侧**音频文件路径(理由同 video:动辄几百 MB~1GB)。
    # 与 images / video 三选一。四条准入(存在性/扩展名/体积/时长)见 policy.audio_reject。
    audio: str | None = None
    # 视频/播客笔记的自定义封面图,同样是**服务器侧路径**。
    # 视频不传 = 用平台自动截取的第一帧;播客不传 = 不设封面。
    # 图文笔记没有独立封面这个概念(封面就是首图),传了一律 422。
    cover: str | None = None
    # 播客合集名称(**仅播客任务**)。用名称不用 id:合集创建流程能否回读到平台侧 id
    # 未取证(E4/E5),而名称是实拍确认的必填项(≤20 字);发布表单里按名称选中。
    # 落库复用 collection_id 列(列级多态,见 app/models/publish_job.py 注释)。
    podcast_collection: str | None = None
    topics: list[str] = []
    schedule_time: str | None = None
    # 笔记三组件(全可选,字段名与 POST /api/accounts/{id}/note-components 一致):
    # 在发布链路的 step6 之后、step7 之前设置,失败只告警不阻断发布。
    collection_id: str | None = None
    quoted_note_id: str | None = None
    activity_id: str | None = None
    # 这篇笔记推介哪位咨询师(姓名)。没给 quoted_note_id 时据它(+ 标题)推导该引用哪篇,
    # 推不出来就留空绝不猜;两者都给时**以显式 quoted_note_id 为准**。
    related_counselor: str | None = None
    # 这篇笔记的核心目的(推荐词表见 app/services/note_purpose.py,不强制枚举)。
    # T0 发布当场带进台账并记 purpose_source='declared';不传则留空,事后由回填链路推断。
    note_purpose: str | None = None

    @model_validator(mode="after")
    def _check_media_exclusive(self) -> "PublishNoteRequest":
        """images / video / audio **三选一** + 各自路径可用性校验(违反一律 422)。

        为什么放在 pydantic 校验层而不是端点体内:这些全是**纯入参形状**问题,
        一条也不需要 DB/账号上下文,放这里让 FastAPI 统一给 422 与字段定位。
        (端点体内的 ``ValueError`` 走 400,那是既有图片张数校验的位置,不动它。)

        「显式 ``images: []`` 且没给 video/audio」故意**不**在这里拦:那是调用方明确选了
        图文这条路只是没给图,继续落到端点体内既有的 400「至少 1 张图片」,上线前的契约
        不变。真正的"都没给"是 images 省略/为 null 且 video、audio 均为空 —— 那才是 422。
        """
        video = (self.video or "").strip()
        audio = (self.audio or "").strip()
        given = [
            name for name, on in
            (("images", self.images is not None), ("video", bool(video)),
             ("audio", bool(audio)))
            if on
        ]
        if len(given) > 1:
            raise ValueError(
                f"images / video / audio 三选一:图文传 images,视频传 video,"
                f"播客传 audio,不能同时给(本次给了 {' 与 '.join(given)})"
            )
        if not given:
            raise ValueError(
                "images / video / audio 三选一必填:图文笔记传 images(1-18 张),"
                "视频笔记传 video、播客笔记传 audio(均为服务器侧文件路径)"
            )
        if video:
            if not video_ext_allowed(video):
                raise ValueError(
                    f"video 格式不支持:{video};小红书只接受 "
                    f"{'/'.join(XHS_VIDEO_EXTENSIONS)}"
                )
            if not Path(video).is_file():
                raise ValueError(f"video 文件不存在(需为本服务器可读的绝对路径):{video}")
        if audio:
            # 四条准入(存在性/扩展名/体积/时长)收在 policy 一处,理由各异故返回具体
            # 理由而不是裸 bool —— 换文件 / 转格式 / 压缩 / 剪辑 是四种不同的补救。
            reason = audio_reject(audio)
            if reason:
                raise ValueError(reason)
        cover = (self.cover or "").strip()
        if cover:
            if not video and not audio:
                raise ValueError(
                    "cover 只对视频/播客笔记有效:图文笔记的封面就是第一张图,"
                    "没有独立封面这个概念,请去掉 cover"
                )
            if audio:
                # 播客封面另有 ≤32MB 的体积上限(实拍规格),与视频封面**不合并成一个
                # 函数**:合并后只能靠调用方传参区分档位,而传错档的失败是静默的。
                reason = audio_cover_reject(cover)
                if reason:
                    raise ValueError(reason)
            else:
                if not cover_ext_allowed(cover):
                    raise ValueError(
                        f"cover 封面图格式不支持:{cover};只接受 "
                        f"{'/'.join(XHS_COVER_EXTENSIONS)}"
                    )
                if not Path(cover).is_file():
                    raise ValueError(
                        f"cover 封面图文件不存在(需为本服务器可读的绝对路径):{cover}")
        collection = (self.podcast_collection or "").strip()
        if collection:
            if not audio:
                raise ValueError(
                    "podcast_collection 只对播客笔记有效(它是播客发布表单独有的控件);"
                    "图文/视频笔记要加合集请用 collection_id"
                )
            if self.collection_id:
                # 两者共用 collection_id 一列,后写会静默盖掉前者 —— 这正是"看着成功、
                # 实际只生效一个"的静默态,入口直接拒绝而不是替调用方挑一个。
                raise ValueError(
                    "podcast_collection 与 collection_id 不能同时给:播客合集与笔记合集"
                    "共用同一个落库字段,同时给必然有一个被静默丢弃"
                )
            if len(collection) > _MAX_PODCAST_COLLECTION_NAME:
                raise ValueError(
                    f"podcast_collection 名称 {len(collection)} 字超过平台上限 "
                    f"{_MAX_PODCAST_COLLECTION_NAME} 字"
                )
        return self

    @model_validator(mode="after")
    def _check_broadcast_quote(self) -> "PublishNoteRequest":
        """广播(省略 account_id)+ 显式 quoted_note_id → 422。

        **笔记 id 归属单个账号**:引用别号的 note_id 在本号的引用弹窗里根本搜不到,广播时
        把同一个 quoted_note_id 发给 N 个号,最多只有它的主人能引用成功,其余 N-1 个号是
        「引用悄悄没生效、笔记照常发出去」的静默失败 —— 而引用失败只告警不阻断发布,事后
        没人会发现。要跨号引用请用 related_counselor 让每个号各自推导本号的推介笔记。
        """
        if self.account_id is None and (self.quoted_note_id or "").strip():
            raise ValueError(
                "广播不支持显式 quoted_note_id(笔记 id 归属单账号),"
                "跨号引用请用 related_counselor 逐号推导"
            )
        return self


async def _broadcast_targets(operator, session) -> list[int]:
    """广播目标:全部代管账号(managed=true)∩ caller 可见账号,按 id 升序。

    两种空集分开报,因为补救动作完全不同:
    - 系统里一个代管账号都没有 → 422(去 PUT /api/accounts/{id}/managed 把号加进来);
    - 有代管账号但 caller 一个都没授权 → 403(找管理员要授权,或显式传 account_id)。
    **不静默发 0 条**:调用方拿到 202 会以为发出去了,那是最难查的静默失败。
    """
    managed = list((await session.execute(
        select(XhsAccount.id)
        .where(XhsAccount.managed.is_(True))
        .order_by(XhsAccount.id)
    )).scalars().all())
    if not managed:
        raise HTTPException(
            status_code=422,
            detail="系统里没有任何代管账号(managed=true),广播发布无处可发:"
                   "请先 PUT /api/accounts/{account_id}/managed 把账号加入代管,"
                   "或在请求体里显式指定 account_id",
        )
    visible = await visible_account_ids(operator, session)
    if visible is None:  # admin 全见
        return managed
    targets = [aid for aid in managed if aid in set(visible)]
    if not targets:
        raise AccessDenied(
            "你没有任何代管账号的授权,无法广播发布;请显式指定一个你有权的 account_id,"
            "或联系管理员授权"
        )
    return targets


async def _create_publish_job(
    session, payload: "PublishNoteRequest", operator, account_id: int,
    scheduled_at: datetime | None, now_utc: datetime,
    video_path: str | None, audio_path: str | None,
) -> tuple[int, bool]:
    """给一个账号建一条 pending 发布任务;返回 (job_id, 是否需要立即 nudge)。

    从原端点体内原样提炼,唯一改动是**日上限顺延与引用推导逐号各算**(用局部变量而非
    改写外层 scheduled_at)—— 广播时 A 号今天到量顺延到次日窗口,不能把 B 号一起顺延。
    只 flush 不 commit:一次广播的 N 条任务由调用方一次提交,避免建到一半失败留下半批。
    """
    # F2:每账号每自然日发布上限。统计该账号当日(UTC)status in
    # (pending/publishing/published) 的 job 数;达上限且本任务本会当日发出(立即或定时在今日
    # 之内)则不立即发,顺延到次日活跃窗口起点(带抖动),仍落库 pending,不丢 job。
    job_scheduled_at = scheduled_at
    today_start = now_utc.replace(hour=0, minute=0, second=0, microsecond=0)
    due_today = (
        job_scheduled_at is None
        or job_scheduled_at < today_start + timedelta(days=1)
    )
    if due_today:
        count_stmt = (
            select(func.count())
            .select_from(PublishJob)
            .where(PublishJob.account_id == account_id)
            .where(PublishJob.status.in_(("pending", "publishing", "published")))
            .where(PublishJob.created_at >= today_start)
        )
        day_count = (await session.execute(count_stmt)).scalar_one()
        if day_count >= settings.PUBLISH_DAILY_CAP:
            job_scheduled_at = _next_active_window_start(now_utc)
    # 引用哪篇笔记:显式 quoted_note_id 优先;没给才按 related_counselor + 标题推导
    # (规则见 app/services/counselor_quote.py),推不出来落 None —— 不引用,绝不猜。
    # 在建 job 这一刻定下来而不是等发到一半再算:落库的就是最终值,事后翻 job 行即可
    # 知道当时引了谁,不必去猜发布那一刻台账长什么样。
    quoted_note_id = payload.quoted_note_id or await counselor_quote.resolve_quoted_note_id(
        session, account_id, payload.title, payload.related_counselor
    )
    job = PublishJob(
        account_id=account_id,
        title=payload.title,
        content=payload.content,
        images_json=json.dumps(payload.images or [], ensure_ascii=False),
        video_path=video_path,
        audio_path=audio_path,
        cover_path=(payload.cover or "").strip() or None,
        topics_json=json.dumps(payload.topics or [], ensure_ascii=False),
        schedule_time=job_scheduled_at,
        status="pending",
        created_by=operator.id,
        # 列级多态:播客任务这里存的是**播客合集名称**,图文/视频存笔记合集 id。
        # 两者互斥已在入参校验层钉死,不会互相覆盖。
        collection_id=(
            (payload.podcast_collection or "").strip() or None
            if audio_path else payload.collection_id
        ),
        quoted_note_id=quoted_note_id,
        activity_id=payload.activity_id,
        related_counselor=payload.related_counselor,
        note_purpose=payload.note_purpose,
    )
    session.add(job)
    await session.flush()  # 拿自增 id,commit 由调用方统一做
    return job.id, job_scheduled_at is None


@router.post("/api/publish-jobs", status_code=202)
async def publish_note_endpoint(payload: PublishNoteRequest) -> dict:
    """发布笔记(异步入队):图文走 images,视频走 video,播客走 audio(三选一,已由入参校验钉死)。

    ``account_id`` 省略 = **广播给全部代管账号**,每号各建一条独立任务(逐号排队、逐号
    受各自的日上限与会话闸约束);传了 = 只发这一个号,行为与上线前一字不变。
    """
    operator = current_operator()
    # 运营配额闸:未完成任务达上限 → 429(admin 豁免),不建 job。
    await assert_operator_quota(operator)
    scheduled_at = _parse_schedule_time(payload.schedule_time)
    async with get_session() as session:
        # 校验顺序不变:先鉴权(或展开广播目标),再校验形状,最后才建任务。
        if payload.account_id is not None:
            await assert_account_access(operator, payload.account_id, session)
            targets = [payload.account_id]
        else:
            targets = await _broadcast_targets(operator, session)
        # D1:建 job 前先校验图片张数,避免造出注定失败的 pending 任务。
        # 视频/播客笔记(video/audio 已通过入参校验)不走图片这条路,张数校验整段跳过。
        video_path = (payload.video or "").strip() or None
        audio_path = (payload.audio or "").strip() or None
        if video_path is None and audio_path is None:
            if not payload.images:
                raise ValueError("图文笔记至少需要 1 张图片")
            if len(payload.images) > _MAX_IMAGES:
                raise ValueError(f"最多 {_MAX_IMAGES} 张图片")
        now_utc = datetime.utcnow()
        created: list[tuple[int, int, bool]] = []
        for account_id in targets:
            job_id, immediate = await _create_publish_job(
                session, payload, operator, account_id, scheduled_at, now_utc,
                video_path, audio_path,
            )
            created.append((account_id, job_id, immediate))
        await session.commit()
    # 立即发布 nudge(可空):有进程内调度器(单进程 all 模式/测试注入)时投队免等扫描;
    # 常态(api/worker 拆分)为 None → 静默跳过,由 worker 5s 扫描兜底(最坏多等 5s)。
    scheduler = get_active_scheduler()
    if scheduler is not None:
        for _account_id, job_id, immediate in created:
            if immediate:
                scheduler.submit(job_id)
    if payload.account_id is not None:
        return {"job_id": created[0][1], "status": "pending"}
    return {
        "broadcast": True,
        "jobs": [{"account_id": a, "job_id": j} for a, j, _ in created],
    }


@router.get("/api/publish-jobs/{job_id}")
async def get_publish_status_endpoint(job_id: int) -> dict:
    """job 不存在 → NotFoundError(404);越权 → 403;返回 _job_view。"""
    operator = current_operator()
    async with get_session() as session:
        job = await session.get(PublishJob, job_id)
        if job is None:
            raise NotFoundError(f"发布任务 {job_id} 不存在")
        await assert_account_access(operator, job.account_id, session)
        # 发布同样会排队(会话总闸满帽时 pending 能排 40 分钟以上),queue 段与 browser
        # 类任务同形。复用本会话,不另开一个。
        return {
            **_job_view(job),
            "queue": await queue_status.for_publish_job(job, session),
        }


@router.get("/api/publish-jobs")
async def list_publish_jobs_endpoint(
    account_id: int | None = None, status: str | None = None, limit: int = 50
) -> dict:
    """与 list_publish_jobs 工具逐行对齐;status 非法 → 裸 ValueError(400)。"""
    operator = current_operator()
    # D2:status 传了就必须合法,否则明确报错(避免"筛错拼写→静默空列表"的误导)。
    if status is not None and status not in _JOB_STATUSES:
        raise ValueError(
            f"status 非法:{status};合法值为 {'/'.join(_JOB_STATUSES)}"
        )
    async with get_session() as session:
        visible = await visible_account_ids(operator, session)
        stmt = select(PublishJob)
        # 非 admin:收窄到可见账号(空列表 → 无结果)
        if visible is not None:
            stmt = stmt.where(PublishJob.account_id.in_(visible))
        # 指定 account_id:显式鉴权(越权抛),再按其筛
        if account_id is not None:
            await assert_account_access(operator, account_id, session)
            stmt = stmt.where(PublishJob.account_id == account_id)
        if status is not None:
            stmt = stmt.where(PublishJob.status == status)
        stmt = stmt.order_by(PublishJob.id.desc()).limit(limit)
        jobs = (await session.execute(stmt)).scalars().all()
        return {"jobs": [_job_view(j) for j in jobs]}


@router.post("/api/publish-jobs/{job_id}/cancel")
async def cancel_publish_job_endpoint(job_id: int) -> dict:
    """仅 pending 可取消;job 不存在 → 404。"""
    operator = current_operator()
    async with get_session() as session:
        job = await session.get(PublishJob, job_id)
        if job is None:
            raise NotFoundError(f"发布任务 {job_id} 不存在")
        await assert_account_access(operator, job.account_id, session)
        if job.status != "pending":
            return {"ok": False, "status": job.status}
        job.status = "canceled"
        await session.commit()
        return {"ok": True}


class PublishJobPatchRequest(BaseModel):
    """PATCH 部分更新入参:字段全可选,只有请求体里显式出现的字段才落库(model_fields_set)。"""

    title: str | None = None
    content: str | None = None
    images: list | None = None
    topics: list[str] | None = None
    schedule_time: str | None = None


@router.patch("/api/publish-jobs/{job_id}")
async def patch_publish_job_endpoint(job_id: int, payload: PublishJobPatchRequest) -> dict:
    """原地修改待发(pending)任务:改 schedule_time / title / content / images / topics。

    仅 pending 可改;非 pending 返回 {ok:false,status}。PATCH 部分更新:只改请求体里显式出现
    的字段(model_fields_set);schedule_time 显式 null=清空转立即发并 submit。条件更新
    WHERE status='pending' 防与 scan_once 抢占的竞态,rowcount=0 视为已被抢走。
    """
    operator = current_operator()
    async with get_session() as session:
        job = await session.get(PublishJob, job_id)
        if job is None:
            raise NotFoundError(f"发布任务 {job_id} 不存在")
        await assert_account_access(operator, job.account_id, session)
        if job.status != "pending":
            return {"ok": False, "status": job.status}

        # 只取请求体里显式出现的字段,避免把默认 None 误当成"清空"。
        fields = payload.model_fields_set
        changes: dict = {}
        if "title" in fields:
            # 显式传 null 无法表达"不改"(省略才是),且 title 列 NOT NULL 落库会 IntegrityError→500;
            # 这里拒绝 null 给清晰 400。
            if payload.title is None:
                raise ValueError("title 不可为 null(不改请省略该字段)")
            changes["title"] = payload.title
        if "content" in fields:
            # 同上:content 列 NOT NULL,显式 null 拒绝并给 400。
            if payload.content is None:
                raise ValueError("content 不可为 null(不改请省略该字段)")
            changes["content"] = payload.content
        if "images" in fields:
            # 视频任务硬拒改图。放任 images 落库产生的**不是**"改成了图文任务",而是第三种
            # 没人预期的迷惑态:images_json 写进去了、video_path 还在,而 runner 是按
            # video_path 路由的 —— 图片永远不生效,调用方却拿到 ok:true,只能等笔记发出来
            # 人工看才发现。破坏性/类型迁移决定必须显式拒绝,不靠 manifest 里一句警告兜底。
            # 422 走 HTTPException 的 detail 体,与本仓既有的 409/429 同一个通道。
            if job.audio_path:
                raise HTTPException(
                    status_code=422,
                    detail="播客任务不可改图片,请取消后重建",
                )
            if job.video_path:
                raise HTTPException(
                    status_code=422,
                    detail="视频任务不可改图片,请取消后重建",
                )
            imgs = payload.images or []
            if not imgs:
                raise ValueError("图文笔记至少需要 1 张图片")
            if len(imgs) > _MAX_IMAGES:
                raise ValueError(f"最多 {_MAX_IMAGES} 张图片")
            changes["images_json"] = json.dumps(imgs, ensure_ascii=False)
        if "topics" in fields:
            changes["topics_json"] = json.dumps(payload.topics or [], ensure_ascii=False)
        schedule_cleared = False
        if "schedule_time" in fields:
            parsed = _parse_schedule_time(payload.schedule_time)
            changes["schedule_time"] = parsed
            schedule_cleared = parsed is None

        if not changes:
            return {"ok": True, "job": _job_view(job)}

        # 条件更新:仅当仍为 pending 才落库,防与 scan_once 的 mark_publishing 抢占。
        result = await session.execute(
            update(PublishJob)
            .where(PublishJob.id == job_id, PublishJob.status == "pending")
            .values(**changes)
        )
        await session.commit()
        if result.rowcount == 0:
            # rowcount=0 说明状态已被 scan_once 抢走(不再 pending)。expire_on_commit=False 下
            # 普通 get 命中身份映射不发 SQL,会返回函数开头的陈旧 pending 对象;populate_existing=True
            # 强制从 DB 重载,拿到真实当前态。
            fresh = await session.get(PublishJob, job_id, populate_existing=True)
            return {"ok": False, "status": fresh.status if fresh else "unknown"}
        if schedule_cleared:
            # 同上:nudge 可空,无进程内调度器时靠 worker 扫描兜底。
            scheduler = get_active_scheduler()
            if scheduler is not None:
                scheduler.submit(job_id)
        await session.refresh(job)
        return {"ok": True, "job": _job_view(job)}
