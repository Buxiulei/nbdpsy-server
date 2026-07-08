"""accounts 分组 MCP 工具:受托管小红书账号的列/查/改/删(RBAC 收窄到 caller 有权的号)。

register_accounts(mcp) 注册 4 个工具。每个工具取 current_operator() 传入 account_service,
由 service 层做鉴权(operator 仅其被 grant 的号,admin 全见)。

返回体只给账号元信息,**绝不含 login_cookies(明文/密文)**——cookie 的读取走 cookies 分组的
get_cookies 工具,受同样的 access 限制。
"""

from datetime import datetime, timezone

from fastmcp import FastMCP
from sqlalchemy import or_, select

from app.auth.context import current_operator
from app.auth.guards import assert_account_access, visible_account_ids
from app.core.db import get_session
from app.models.xhs_account import XhsAccount
from app.services import account_service


def _parse_since(since: str) -> datetime:
    """把 ISO 时间串解析为 naive UTC datetime,与 last_login_at/created_at 的存储基准对齐。

    tz-aware 输入先 astimezone(UTC) 再去掉 tzinfo 归一到 naive UTC;naive 输入原样当作 UTC。
    这样比较不会因时区错位早/晚 8 小时(get_extension_download 的 server_time 即 naive UTC)。
    """
    parsed = datetime.fromisoformat(since)
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed


def _account_view(account: XhsAccount) -> dict:
    """把账号序列化为对外元信息视图;刻意不含 login_cookies,避免泄露登录态。"""
    return {
        "id": account.id,
        "name": account.name,
        "nickname": account.nickname,
        "user_id": account.user_id,
        "red_id": account.red_id,
        "avatar": account.avatar,
        "status": account.status,
        "cookie_status": account.cookie_status,
        "last_check_at": (
            account.last_check_at.isoformat() if account.last_check_at else None
        ),
        "last_login_at": (
            account.last_login_at.isoformat() if account.last_login_at else None
        ),
        "created_at": (
            account.created_at.isoformat() if account.created_at else None
        ),
    }


def register_accounts(mcp: FastMCP) -> None:
    """把 accounts 分组工具注册到 mcp 实例(装饰器需闭包内的 mcp)。"""

    @mcp.tool
    async def list_accounts() -> dict:
        """列出当前运营者可见的小红书账号(admin 全见;不含 cookie)。

        cookie_status 取值:
          - unknown:尚未巡检过
          - valid:登录态有效,可发布
          - invalid:登录态失效,需人重新扫码登录
          - captcha:被验证码/滑块拦截,需人工过验证
          - error:巡检时浏览器基础设施失败(非 cookie 失效,登录态未知,见 check_cookies)
        status 字段**预留未启用**,判断登录态请看 cookie_status。
        提示:可先用本工具返回的 cookie_status/last_check_at 做**廉价预检**(cookie 近期
        valid 就直接发),不必每次盲调慢的 check_cookies(那会起浏览器,20-40s)。
        """
        operator = current_operator()
        async with get_session() as session:
            accounts = await account_service.list_accounts(session, operator)
            return {"accounts": [_account_view(a) for a in accounts]}

    @mcp.tool
    async def get_account(account_id: int) -> dict:
        """查看单个账号元信息(需 access;不含 cookie)。

        cookie_status 取值:unknown/valid/invalid/captcha/error(含义见 list_accounts)。
        status 字段**预留未启用**,判断登录态请看 cookie_status。
        """
        operator = current_operator()
        async with get_session() as session:
            account = await account_service.get_account(
                session, operator, account_id
            )
            return _account_view(account)

    @mcp.tool
    async def update_account(account_id: int, name: str | None = None) -> dict:
        """更新账号安全字段(当前仅内部展示名 name;需 access)。"""
        operator = current_operator()
        fields: dict = {}
        if name is not None:
            fields["name"] = name
        async with get_session() as session:
            account = await account_service.update_account(
                session, operator, account_id, **fields
            )
            return _account_view(account)

    @mcp.tool
    async def delete_account(account_id: int) -> dict:
        """删除账号并清其全部授权行(需 access)。"""
        operator = current_operator()
        async with get_session() as session:
            await account_service.delete_account(session, operator, account_id)
            return {"deleted": account_id}

    @mcp.tool
    async def poll_login(since: str, account_id: int | None = None) -> dict:
        """轮询"等登录完成"信号:自 since 起有无新号/新登录(远程登录闭环的收口判据)。

        登录闭环(没有登录工具,登录靠人 + chrome 插件):
          1) 调 get_extension_download 拿 server_time 与插件包,把插件递给操作者;
          2) 人装插件、扫码登录(插件自动把 cookie 推回后台);
          3) 每 ~10s 调 poll_login(since=server_time[, account_id]) 直到 done=true;
             建议设 5-10 分钟超时(超时即视为登录未完成,提示操作者重试)。

        since 传 ISO 时间串(用步骤 1 的 server_time);tz-aware 会归一到 naive UTC 再比较。
        两种场景:
          - **登新号**(不传 account_id):在你可见的号里(admin 全见)找 last_login_at 或
            created_at 晚于 since 的号 → done = 命中数>0,accounts 为命中账号视图列表。
          - **重登旧号**(传 account_id):鉴权后取该号,done = 其 last_login_at 晚于 since
            (即这轮确有重新登录刷新),account 为该号视图。
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
                    "accounts": [_account_view(a) for a in accounts],
                }
            # 重登旧号:先鉴权再看该号 last_login_at 是否被这轮登录刷新到 since 之后。
            await assert_account_access(operator, account_id, session)
            account = await session.get(XhsAccount, account_id)
            if account is None:
                raise ValueError(f"账号 {account_id} 不存在")
            done = (
                account.last_login_at is not None
                and account.last_login_at > since_dt
            )
            return {"done": done, "account": _account_view(account)}
