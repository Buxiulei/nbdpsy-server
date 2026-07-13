"""GET /api/accounts + 单号 CRUD + /api/accounts/{id}/cookies + GET /api/login/poll。

端点均不在中间件白名单(白名单仅 /healthz、/downloads)→ 走 apikey 中间件校验后,端点内
current_operator() 即当前运营者;RBAC 复用服务层:
- list_accounts 本就按 visible_account_ids 收窄(admin 全见,operator 仅其被 grant 的号);
- get/update/delete_account、get_cookies 内部 assert_account_access,无权抛 AccessDenied →
  server.py 的全局 handler 映 403;账号不存在抛 NotFoundError → 404。

/api/accounts 系返回体复用 account_service.account_view(与 accounts 分组 MCP 工具同一视图,
**不含 login_cookies**);/api/accounts/{id}/cookies 返回解密 cookie,专供插件注入无痕窗口。

GET /api/login/poll 是远程登录闭环的收口判据(逻辑整体平移自原 app/tools/accounts.py 的
poll_login 工具,含 _parse_since):没有登录接口,登录靠人 + chrome 插件,轮询 since 之后
有无新号/新登录即可判断登录是否完成。
"""

from datetime import datetime, timezone

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict
from sqlalchemy import or_, select

from app.auth.context import current_operator
from app.auth.guards import assert_account_access, visible_account_ids
from app.core.db import get_session
from app.core.errors import NotFoundError
from app.models.xhs_account import XhsAccount
from app.services import account_service, cookie_service

router = APIRouter()


def _parse_since(since: str) -> datetime:
    """把 ISO 时间串解析为 naive UTC datetime,与 last_login_at/created_at 的存储基准对齐。

    tz-aware 输入先 astimezone(UTC) 再去掉 tzinfo 归一到 naive UTC;naive 输入原样当作 UTC。
    这样比较不会因时区错位早/晚 8 小时(get_extension_download 的 server_time 即 naive UTC)。
    """
    parsed = datetime.fromisoformat(since)
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed


class AccountUpdateRequest(BaseModel):
    """账号可改字段白名单(目前仅 name);extra 字段直接 422。"""

    model_config = ConfigDict(extra="forbid")
    name: str | None = None


MANIFEST_ENTRIES = [
    {
        "method": "GET", "path": "/api/accounts",
        "summary": "列出 caller 可见的小红书账号(operator 只见被授权的,admin 全见)",
        "admin_only": False, "params": {},
        "returns": "{accounts: [{id, name, nickname, user_id, red_id, avatar, status, cookie_status, last_check_at, last_login_at, created_at}]}",
        "errors": "",
        "notes": "刻意不含 cookie 明文;cookie_status/last_check_at 可做廉价活性预检。",
    },
    {
        "method": "GET", "path": "/api/accounts/{account_id}/cookies",
        "summary": "解密回读某号 cookie(受授权限制)",
        "admin_only": False, "params": {"account_id": "path,int"},
        "returns": "{account_id, cookies: [cookie 对象]}",
        "errors": "403=无该号授权",
        "notes": "用于把 cookie 注入自己的浏览器等程序化场景。",
    },
    {
        "method": "GET", "path": "/api/accounts/{account_id}",
        "summary": "查看单个账号元信息(需 access;不含 cookie)",
        "admin_only": False, "params": {"account_id": "path,int"},
        "returns": "account_view 同 GET /api/accounts 单条元素",
        "errors": "403=无该号授权;404=账号不存在",
        "notes": "",
    },
    {
        "method": "PATCH", "path": "/api/accounts/{account_id}",
        "summary": "更新账号安全字段(当前仅内部展示名 name;需 access)",
        "admin_only": False,
        "params": {"account_id": "path,int", "name": "body,str,可选"},
        "returns": "account_view(更新后)",
        "errors": "403=无该号授权;404=账号不存在;422=传了 name 之外的字段",
        "notes": "请求体 schema 只收 name,多余字段直接 422(与旧工具 ValueError 语义的合理收严)。",
    },
    {
        "method": "DELETE", "path": "/api/accounts/{account_id}",
        "summary": "删除账号并清其全部授权行(需 access)",
        "admin_only": False, "params": {"account_id": "path,int"},
        "returns": "{deleted: account_id}",
        "errors": "403=无该号授权",
        "notes": "账号本体不存在时静默幂等(不报错)。",
    },
    {
        "method": "GET", "path": "/api/login/poll",
        "summary": "轮询远程登录是否完成(自 since 起有无新号/新登录)",
        "admin_only": False,
        "params": {"since": "query,str,ISO 时间串(取 GET /api/extension 的 server_time)",
                   "account_id": "query,int,可选(重登旧号传,登新号不传)"},
        "returns": "登新号:{done, accounts: [account_view,...]};"
                   "重登旧号(传 account_id):{done, account: account_view}",
        "errors": "400=since 非 ISO8601;404=account_id 指定的账号不存在",
        "notes": "登录闭环轮询协议:每 ~10s 轮一次,建议设 5-10 分钟超时(超时视为登录未完成,"
                 "提示操作者重试);登新号不传 account_id(在可见账号里找 last_login_at/created_at "
                 "晚于 since 的号),重登旧号传 account_id(判该号 last_login_at 是否刷新到 since 之后)。",
    },
]


@router.get("/api/accounts")
async def list_accounts_endpoint() -> dict:
    """列出当前运营者可见的小红书账号(admin 全见;不含 cookie),供插件"我的账号"列表渲染。"""
    operator = current_operator()
    async with get_session() as session:
        accounts = await account_service.list_accounts(session, operator)
        return {"accounts": [account_service.account_view(a) for a in accounts]}


@router.get("/api/accounts/{account_id}/cookies")
async def get_account_cookies_endpoint(account_id: int) -> dict:
    """取某号解密 cookie 供插件注入无痕窗口;无 access → AccessDenied(全局 handler 映 403)。"""
    operator = current_operator()
    async with get_session() as session:
        cookies = await cookie_service.get_cookies(session, operator, account_id)
        return {"account_id": account_id, "cookies": cookies}


@router.get("/api/accounts/{account_id}")
async def get_account_endpoint(account_id: int) -> dict:
    """查看单个账号元信息(需 access;不含 cookie)。"""
    operator = current_operator()
    async with get_session() as session:
        account = await account_service.get_account(session, operator, account_id)
        return account_service.account_view(account)


@router.patch("/api/accounts/{account_id}")
async def update_account_endpoint(
    account_id: int, payload: AccountUpdateRequest
) -> dict:
    """更新账号安全字段(当前仅内部展示名 name;需 access)。"""
    operator = current_operator()
    fields = {k: v for k, v in payload.model_dump().items() if v is not None}
    async with get_session() as session:
        account = await account_service.update_account(
            session, operator, account_id, **fields
        )
        return account_service.account_view(account)


@router.delete("/api/accounts/{account_id}")
async def delete_account_endpoint(account_id: int) -> dict:
    """删除账号并清其全部授权行(需 access)。"""
    operator = current_operator()
    async with get_session() as session:
        await account_service.delete_account(session, operator, account_id)
        return {"deleted": account_id}


@router.get("/api/login/poll")
async def poll_login_endpoint(since: str, account_id: int | None = None) -> dict:
    """轮询"等登录完成"信号:自 since 起有无新号/新登录(远程登录闭环的收口判据)。

    没有登录工具,登录靠人 + chrome 插件:先调 GET /api/extension 拿 server_time,把插件递给
    操作者装好扫码登录;之后每 ~10s 调本端点(since=server_time[, account_id])直到 done=true,
    建议设 5-10 分钟超时。两种场景:登新号不传 account_id(在可见账号里找 last_login_at/
    created_at 晚于 since 的号);重登旧号传 account_id(判该号 last_login_at 是否刷新到 since 之后)。
    """
    operator = current_operator()
    since_dt = _parse_since(since)
    async with get_session() as session:
        if account_id is None:
            ids = await visible_account_ids(operator, session)
            stmt = (
                select(XhsAccount)
                .where(
                    or_(
                        XhsAccount.last_login_at > since_dt,
                        XhsAccount.created_at > since_dt,
                    )
                )
                .order_by(XhsAccount.id)
            )
            if ids is not None:
                if not ids:  # 无任何可见号,直接判未完成
                    return {"done": False, "accounts": []}
                stmt = stmt.where(XhsAccount.id.in_(ids))
            accounts = list((await session.execute(stmt)).scalars().all())
            return {
                "done": len(accounts) > 0,
                "accounts": [account_service.account_view(a) for a in accounts],
            }
        # 重登旧号:先鉴权再看该号 last_login_at 是否被这轮登录刷新到 since 之后。
        await assert_account_access(operator, account_id, session)
        account = await session.get(XhsAccount, account_id)
        if account is None:
            raise NotFoundError(f"账号 {account_id} 不存在")
        done = (
            account.last_login_at is not None and account.last_login_at > since_dt
        )
        return {"done": done, "account": account_service.account_view(account)}
