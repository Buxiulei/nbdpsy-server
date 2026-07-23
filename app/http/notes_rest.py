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
from app.services import note_delete, note_export
from app.services.note_metrics_service import list_notes, note_trend

router = APIRouter()

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
                   "count": "body,int=1(同题多篇时一次会话最多删几篇)"},
        "returns": '{deletion_id, status:"running"}',
        "errors": "403=无该号授权;404=账号不存在",
        "notes": "异步契约:起后台浏览器进创作中心笔记管理页,按标题悬停出删除图标→确认弹窗删除"
                 "(约 1-2 分钟);拿 deletion_id 后每 3-5s 轮询 GET /api/note-deletions/{deletion_id}。"
                 "删除不可逆!确认弹窗文案必须含「删除」才会点确认,防误点。同号浏览器操作共享"
                 "per-account 锁串行。",
    },
    {
        "method": "GET", "path": "/api/note-deletions/{deletion_id}",
        "summary": "轮询笔记删除结果",
        "admin_only": False, "params": {"deletion_id": "path,str"},
        "returns": "{status, deleted?, remaining?, reason?}",
        "errors": "403=无该号授权;404=deletion_id 不存在或已过期",
        "notes": "status 三态:running/done(deleted=实际删除数,remaining=剩余同题卡数)/"
                 "error(reason 如 note_not_found/need_manual_login)。进程级内存台账,重启即丢。",
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
        "returns": "默认 {notes:[最新快照, ...]};trend=daily+title+publish_time → {trend:[每日行, ...]}",
        "errors": "403=无该号授权",
        "notes": "小红书创作中心导出无 note_id / 封面 URL,故以 (account_id, 标题, 发布时间) 三元组为"
                 "笔记业务主键;数据由 note-exports 导出落库,需该号 creator 登录态先跑过导出。"
                 "trend 缺 title/publish_time 时退化为读最新快照列表。",
    },
]


@router.post("/api/accounts/{account_id}/note-exports", status_code=202)
async def start_note_export_endpoint(account_id: int) -> dict:
    """异步触发该号创作中心笔记导出,立即返回 export_id(导出后台跑,不阻塞)。"""
    operator = current_operator()
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


@router.post("/api/accounts/{account_id}/note-deletions", status_code=202)
async def start_note_deletion_endpoint(
    account_id: int, payload: NoteDeletionRequest
) -> dict:
    """异步触发按标题删除该号笔记(不可逆),立即返回 deletion_id。"""
    operator = current_operator()
    async with get_session() as session:
        await assert_account_access(operator, account_id, session)
        account = await session.get(XhsAccount, account_id)
        if account is None:
            raise NotFoundError(f"账号 {account_id} 不存在")
        cookies = _decrypt_account_cookies(account)
    deletion_id = note_delete.start_delete(
        account_id, cookies, payload.title, payload.count
    )
    return {"deletion_id": deletion_id, "status": "running"}


@router.get("/api/note-deletions/{deletion_id}")
async def get_note_deletion_endpoint(deletion_id: str) -> dict:
    """轮询删除结果:running / done(deleted+remaining)/ error(reason);越权 403。"""
    entry = note_delete.get_delete(deletion_id)
    if entry is None:
        raise NotFoundError(f"deletion_id {deletion_id} 不存在或已过期")
    operator = current_operator()
    async with get_session() as session:
        await assert_account_access(operator, entry["account_id"], session)
    result: dict = {"status": entry["status"]}
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
    result: dict = {"status": entry["status"]}
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

    RBAC 由 note_metrics_service.list_notes / note_trend 内部 assert_account_access 收窄
    (admin 全见,operator 仅授权号,无权抛 AccessDenied → 403)。
    """
    operator = current_operator()
    async with get_session() as session:
        if trend == "daily" and title and publish_time:
            rows = await note_trend(session, operator, account_id, title, publish_time)
            return {"trend": rows}
        rows = await list_notes(session, operator, account_id)
        return {"notes": rows}
