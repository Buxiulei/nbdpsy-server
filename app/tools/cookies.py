"""cookies 分组 MCP 工具:插件/远程 agent 灌入 cookie 与回读 cookie。

register_cookies(mcp) 注册 2 个工具,均取 current_operator() 传入 cookie_service:
- import_cookies:把 cookies_json 字符串 json.loads 成 list 再 upsert 唯一账号行,返回
  {account_id, created};新建时给导入 operator 建 access。
- get_cookies:鉴权后解密回读某号 cookie(受 access 限制,admin 放行)。

注:check_cookies(活性巡检)依赖 P3 的浏览器 sync_client,不在本组,留到后续任务。
"""

import json

from fastmcp import FastMCP

from app.auth.context import current_operator
from app.core.db import get_session
from app.services import cookie_service


def register_cookies(mcp: FastMCP) -> None:
    """把 cookies 分组工具注册到 mcp 实例(装饰器需闭包内的 mcp)。"""

    @mcp.tool
    async def import_cookies(
        account_name: str, cookies_json: str, user_info: dict | None = None
    ) -> dict:
        """灌入 cookie:解析 cookies_json 字符串后 upsert 唯一账号,返回 {account_id, created}。"""
        operator = current_operator()
        cookies = json.loads(cookies_json)
        async with get_session() as session:
            account, created = await cookie_service.import_cookies(
                session, operator, account_name, cookies, user_info
            )
            return {"account_id": account.id, "created": created}

    @mcp.tool
    async def get_cookies(account_id: int) -> dict:
        """回读某号解密后的 cookies(受 access 限制,admin 放行)。"""
        operator = current_operator()
        async with get_session() as session:
            cookies = await cookie_service.get_cookies(
                session, operator, account_id
            )
            return {"account_id": account_id, "cookies": cookies}
