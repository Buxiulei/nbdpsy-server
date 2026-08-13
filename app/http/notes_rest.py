"""notes 分组 REST(3 端点):触发笔记数据导出(202)/ 轮询导出结果 / 读快照与日趋势。

配合 note_export(进程级内存台账 + 后台浏览器导出)与 note_metrics_service(RBAC 收窄的读):
- POST /api/accounts/{account_id}/note-exports:鉴权后解密该号 cookie → 交
  note_export.start_export 起后台创作中心导出(不阻塞,约数十秒到数分钟)→ 立即返回 export_id。
- GET /api/note-exports/{export_id}:轮询导出结果,鉴权用台账里存的 account_id 防越权;
  running/done/error 三态含义见 MANIFEST_ENTRIES。
- GET /api/accounts/{account_id}/notes:默认读该号最新快照列表;trend=daily + title +
  publish_time 时读某笔记的每日趋势升序序列。RBAC 由 note_metrics_service 内部收窄。
"""

from fastapi import APIRouter
from pydantic import BaseModel, Field

from app.auth.context import current_operator
from app.auth.guards import assert_account_access
from app.core.db import get_session
from app.core.errors import NotFoundError
from app.http.cookies_rest import _decrypt_account_cookies
from app.models.xhs_account import XhsAccount
from app.http.job_polling import QUEUE_MANIFEST_NOTE
from app.services import note_delete, note_export, queue_status
from app.services.note_metrics_service import (
    account_trends,
    field_meta_block,
    list_notes,
    note_trend,
)
from app.services.quota import assert_operator_quota

router = APIRouter()

# /notes 两个分支各自的真实排序口径,随 meta 下发(field_meta_block 的 ordering 必填参数)。
# 分开写不合并:两个分支 order_by 本来就不同,合成一句"通用排序"正是老 bug 的形状。
_ORDER_LATEST_SNAPSHOT = (
    "notes 按**入库顺序**(NoteMetric.id 升序)返回,**不是**按 views/点赞等任何指标排序"
    "——notes[:5] 拿到的是最早入库的 5 篇,**不是**「Top5 高表现笔记」,"
    "最火的一篇完全可能排在中间。要按表现取 Top N:自己按 notes[].views(或其它指标)排序,"
    "或直接调 GET /api/accounts/{account_id}/note-trends——它已按最新 views 降序,"
    "还带率值与逐日序列。本端点也不下发 series(逐日序列),要序列用 trend=daily 或 note-trends。"
)
_ORDER_TREND_DAILY = (
    "trend 按 snapshot_date **升序**(最早在前);本形态只返所指定那一条笔记的逐日行,"
    "不存在跨笔记排序。"
)

MANIFEST_ENTRIES = [
    {
        "method": "POST", "path": "/api/accounts/{account_id}/note-exports",
        "summary": "异步触发某号创作中心笔记数据导出",
        "admin_only": False, "params": {"account_id": "path,int"},
        "returns": '{export_id, status:"running"}',
        "errors": "403=无该号授权;404=账号不存在",
        "notes": "异步契约:本调用不等待,起后台浏览器导出(需该号 creator 登录态,约数十秒到数分钟);"
                 "拿到 export_id 后每 3-5s 轮询 GET /api/note-exports/{export_id} 直到 done/error。"
                 "同号浏览器操作(发布/cookie 检测/导出)共享 per-account 锁串行,别对同号并发发起。",
    },
    {
        "method": "GET", "path": "/api/note-exports/{export_id}",
        "queue": QUEUE_MANIFEST_NOTE,
        "summary": "轮询笔记导出结果",
        "admin_only": False, "params": {"export_id": "path,str"},
        "returns": "{status, note_count?, reason?}",
        "errors": "403=无该号授权;404=export_id 不存在或已过期",
        "notes": "status 三态:running(仍在导出,继续轮询)/done(导出并落库成功,附 note_count "
                 "落库条数)/error(导出失败,附 reason,如 need_manual_login/浏览器起不来;不落库,"
                 "不代表下次必失败)。export_id 是进程级内存台账,进程重启即丢,404 时重新发起导出。",
    },
    {
        "method": "POST", "path": "/api/accounts/{account_id}/note-deletions",
        "summary": "异步触发按标题删除该号笔记(不可逆,慎用)",
        "admin_only": False,
        "params": {"account_id": "path,int",
                   "title": "body,str(笔记标题,精确匹配,容忍卡片截断)",
                   "count": "body,int=1(同题多篇时一次会话最多删几篇)",
                   "allow_ambiguous": "body,bool=false(同题卡≥2张默认拒绝执行报"
                                      " ambiguous_title——删除按管理页序取首张=最新篇,"
                                      "同题一死一活会删错;确系删N留1清理才带 true)"},
        "returns": '{deletion_id, status:"running"}',
        "errors": "403=无该号授权;404=账号不存在",
        "notes": "异步契约:起后台浏览器进创作中心笔记管理页,按标题悬停出删除图标→确认弹窗删除"
                 "(约 1-2 分钟);拿 deletion_id 后每 3-5s 轮询 GET /api/note-deletions/{deletion_id}。"
                 "删除不可逆!确认弹窗文案必须含「删除」才会点确认,防误点。同号浏览器操作共享"
                 "per-account 锁串行。",
    },
    {
        "method": "GET", "path": "/api/note-deletions/{deletion_id}",
        "queue": QUEUE_MANIFEST_NOTE,
        "summary": "轮询笔记删除结果",
        "admin_only": False, "params": {"deletion_id": "path,str"},
        "returns": "{status, deleted?, remaining?, reason?}",
        "errors": "403=无该号授权;404=deletion_id 不存在",
        "notes": "status 四态:running/done(deleted=实际删除数,remaining=剩余同题卡数)/"
                 "error(reason 如 note_not_found/need_manual_login)/unknown(server 重启"
                 "打断了删除,结果未知,按 reason 指引人工核对)。台账已持久化:重启后终态"
                 "仍可查,不再 404。",
    },
    {
        "method": "GET", "path": "/api/accounts/{account_id}/note-trends",
        "summary": "一次拉取该号完整趋势分析包(数分 agent 专用,免二次组装)",
        "admin_only": False, "params": {"account_id": "path,int"},
        "returns": "{account:{id,name,nickname,cookie_status}, meta:{snapshot_dates,"
                   "latest_snapshot_date,notes_tracked,field_meta(全量逐字段口径,"
                   "含 6 个派生率 + delta/days_between),field_notes(口径说明)}, "
                   "account_daily:[{snapshot_date,note_count,7量指标合计,delta:{增量,days_between}}], "
                   "notes:[{title,publish_time,days_since_publish,latest(11指标),"
                   "rates(like/collect/comment/engage/follow_rate/follow_rate_t1),"
                   "series:[逐日行+delta]}]}",
        "errors": "403=无该号授权",
        "notes": "为数据分析设计:指标全是快照日累计值;delta=与上一快照日的差,带 days_between"
                 "(快照可能断档,日均要除以它);rates 分母是最新 views,views=0 时 null;"
                 "notes 按最新 views 降序。数据来自每日自动采集(note_metrics_scheduler)+"
                 "手动 note-exports;某天没快照=当天没采到(断档),不是数据为 0。",
    },
    {
        "method": "GET", "path": "/api/accounts/{account_id}/notes",
        "summary": "读该号笔记最新快照,或某笔记的每日趋势序列",
        "admin_only": False,
        "params": {
            "account_id": "path,int",
            "title": "query,str|None(与 publish_time + trend=daily 联用定位单条笔记)",
            "publish_time": "query,str|None(Excel 原文发布时间字符串,与 title 组成业务主键)",
            "trend": "query,str|None(=daily 且带 title+publish_time 时返日趋势;否则返最新快照列表)",
        },
        "returns": "默认 {notes:[最新快照, ...](按入库顺序,非按表现排序), meta:{field_meta"
                   "(只含本端点实际下发的 11 个平台原生列,不含率值),field_notes"
                   "(读法 + 本形态真实排序口径)}};"
                   "trend=daily+title+publish_time → {trend:[每日行, ...](按 snapshot_date 升序), "
                   "meta:同上(排序口径换成该形态的)}",
        "errors": "403=无该号授权",
        "notes": "小红书创作中心导出无 note_id / 封面 URL,故以 (account_id, 标题, 发布时间) 三元组为"
                 "笔记业务主键;数据由 note-exports 导出落库,需该号 creator 登录态先跑过导出。"
                 "trend 缺 title/publish_time 时退化为读最新快照列表。"
                 "meta.field_meta 给出本端点下发的每个指标的官方口径/时间窗(T 实时 vs T-1 截至昨日)/"
                 "单位/来源,**分析前先读它**——跨 window 字段相除算不出真实转化率。"
                 "本端点**不下发**派生率值(like_rate/engage_rate/follow_rate_t1 等)与 delta 增量,"
                 "field_meta 里也不声明;要率值调 GET /api/accounts/{account_id}/note-trends。"
                 "⚠️ 默认形态的 notes 是**入库顺序**(NoteMetric.id 升序),**不是**按 views 等指标排序"
                 "——notes[:5] 是最早入库的 5 篇而非 Top5;要 Top N 自己排序,或调 note-trends"
                 "(它按最新 views 降序)。",
    },
]


@router.post("/api/accounts/{account_id}/note-exports", status_code=202)
async def start_note_export_endpoint(account_id: int) -> dict:
    """异步触发该号创作中心笔记导出,立即返回 export_id(导出后台跑,不阻塞)。"""
    operator = current_operator()
    # 运营配额闸:未完成任务达上限 → 429(admin 豁免),不发起导出。
    await assert_operator_quota(operator)
    async with get_session() as session:
        await assert_account_access(operator, account_id, session)
        account = await session.get(XhsAccount, account_id)
        if account is None:
            raise NotFoundError(f"账号 {account_id} 不存在")
        cookies = _decrypt_account_cookies(account)
    export_id = note_export.start_export(account_id, cookies)
    return {"export_id": export_id, "status": "running"}


class NoteDeletionRequest(BaseModel):
    """按标题删除笔记的请求体。删除不可逆,title 精确匹配(容忍卡片截断省略号)。"""

    title: str = Field(min_length=1, max_length=100, description="笔记标题(精确匹配)")
    count: int = Field(default=1, ge=1, le=10, description="同题多篇时一次最多删几篇")
    allow_ambiguous: bool = Field(
        default=False,
        description="管理页同题卡 ≥2 张时是否仍执行。默认拒绝(ambiguous_title):"
                    "删除按管理页顺序取首张=最新篇,同题一死一活场景会删错;"
                    "确系同题清理(删 N 留 1)才显式带 true",
    )


@router.post("/api/accounts/{account_id}/note-deletions", status_code=202)
async def start_note_deletion_endpoint(
    account_id: int, payload: NoteDeletionRequest
) -> dict:
    """异步触发按标题删除该号笔记(不可逆),立即返回 deletion_id。"""
    operator = current_operator()
    # 运营配额闸:未完成任务达上限 → 429(admin 豁免),不发起删除。
    await assert_operator_quota(operator)
    async with get_session() as session:
        await assert_account_access(operator, account_id, session)
        account = await session.get(XhsAccount, account_id)
        if account is None:
            raise NotFoundError(f"账号 {account_id} 不存在")
        cookies = _decrypt_account_cookies(account)
    deletion_id = note_delete.start_delete(
        account_id, cookies, payload.title, payload.count,
        allow_ambiguous=payload.allow_ambiguous,
    )
    return {"deletion_id": deletion_id, "status": "running"}


@router.get("/api/note-deletions/{deletion_id}")
async def get_note_deletion_endpoint(deletion_id: str) -> dict:
    """轮询删除结果:running / done(deleted+remaining)/ error / unknown;越权 403。

    先查内存台账(热路径);miss 再回退 DB 持久台账——server 重启后终态仍可查,
    重启打断的 running 行译成 unknown(删除不可逆,绝不冒充还在跑)。
    """
    entry = note_delete.get_delete(deletion_id)
    if entry is None:
        entry = await note_delete.get_delete_persisted(deletion_id)
    if entry is None:
        raise NotFoundError(f"deletion_id {deletion_id} 不存在或已过期")
    operator = current_operator()
    async with get_session() as session:
        await assert_account_access(operator, entry["account_id"], session)
    # 同 note-exports:台账的 queued 在这里也被译成 running,排队看不出来,补 queue 段
    result: dict = {
        "status": entry["status"],
        "queue": await queue_status.for_browser_job_id(deletion_id),
    }
    for key in ("deleted", "remaining", "reason"):
        if entry.get(key) is not None:
            result[key] = entry[key]
    return result


@router.get("/api/note-exports/{export_id}")
async def get_note_export_endpoint(export_id: str) -> dict:
    """轮询导出结果:running / done(附 note_count)/ error(附 reason);越权 403,不存在 404。"""
    entry = note_export.get_export(export_id)
    if entry is None:
        raise NotFoundError(f"export_id {export_id} 不存在或已过期")
    operator = current_operator()
    async with get_session() as session:
        await assert_account_access(operator, entry["account_id"], session)
    # 本端点把台账的 queued/running 一律译成 running,排队与在跑从 status 上分不出来
    # ——queue 段正是补这个(同 cookie-checks:多一次主键点查取台账行)。
    result: dict = {
        "status": entry["status"],
        "queue": await queue_status.for_browser_job_id(export_id),
    }
    if entry.get("note_count") is not None:
        result["note_count"] = entry["note_count"]
    if entry.get("reason") is not None:
        result["reason"] = entry["reason"]
    return result


@router.get("/api/accounts/{account_id}/notes")
async def list_account_notes_endpoint(
    account_id: int,
    title: str | None = None,
    publish_time: str | None = None,
    trend: str | None = None,
) -> dict:
    """默认读最新快照 {notes:[...]};trend=daily + title + publish_time 时读日趋势 {trend:[...]}。

    两种形态都附 meta(field_meta 逐字段口径 + field_notes 读法):口径随数据一起下发,
    数分 agent 不会拿到一堆裸数字只能按"口径未知"保守处理。排序口径两个形态各不相同
    (默认=入库序、trend=snapshot_date 升序),各自经 ordering= 自报,不共用一句。

    两种形态下发的都是平台原生列(最新快照 / 每日行),没有率值那一层,故 field_meta 取
    include_derived=False 收窄到实际下发的字段 + 一句指路(率值去 note-trends 拿)。

    RBAC 由 note_metrics_service.list_notes / note_trend 内部 assert_account_access 收窄
    (admin 全见,operator 仅授权号,无权抛 AccessDenied → 403)。
    """
    operator = current_operator()
    async with get_session() as session:
        if trend == "daily" and title and publish_time:
            rows = await note_trend(session, operator, account_id, title, publish_time)
            return {
                "trend": rows,
                "meta": field_meta_block(
                    include_derived=False, ordering=_ORDER_TREND_DAILY
                ),
            }
        rows = await list_notes(session, operator, account_id)
        return {
            "notes": rows,
            "meta": field_meta_block(
                include_derived=False, ordering=_ORDER_LATEST_SNAPSHOT
            ),
        }


@router.get("/api/accounts/{account_id}/note-trends")
async def account_note_trends_endpoint(account_id: int) -> dict:
    """一次拉取该号完整趋势分析包(账号级日汇总+每篇笔记序列/增量/率值+口径说明)。

    RBAC 由 note_metrics_service.account_trends 内部 assert_account_access 收窄。
    """
    operator = current_operator()
    async with get_session() as session:
        return await account_trends(session, operator, account_id)
