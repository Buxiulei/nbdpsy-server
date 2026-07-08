"""accounts 分组 MCP 工具:受托管小红书账号的列/查/改/删(RBAC 收窄到 caller 有权的号)。

register_accounts(mcp) 注册 4 个工具。每个工具取 current_operator() 传入 account_service,
由 service 层做鉴权(operator 仅其被 grant 的号,admin 全见)。

返回体只给账号元信息,**绝不含 login_cookies(明文/密文)**——cookie 的读取走 cookies 分组的
get_cookies 工具,受同样的 access 限制。
"""

from fastmcp import FastMCP

from app.auth.context import current_operator
from app.core.db import get_session
from app.models.xhs_account import XhsAccount
from app.services import account_service


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
