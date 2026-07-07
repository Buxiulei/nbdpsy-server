"""cookies 分组 MCP 工具:插件/远程 agent 灌入 cookie / 回读 cookie / 活性巡检。

register_cookies(mcp) 注册 3 个工具,均取 current_operator() 收窄到有 access 的号:
- import_cookies:把 cookies_json 字符串 json.loads 成 list 再 upsert 唯一账号行,返回
  {account_id, created};新建时给导入 operator 建 access。
- get_cookies:鉴权后解密回读某号 cookie(受 access 限制,admin 放行)。
- check_cookies:鉴权后解密该号 cookie → 线程内起浏览器跑登录检测(check_login_once)→
  把三态 status 写回 cookie_status/last_check_at(有 user_info 则回填资料)→ 返回。
"""

import asyncio
import json
from datetime import datetime

from fastmcp import FastMCP

from app.auth.context import current_operator
from app.auth.guards import assert_account_access
from app.browser import sync_client
from app.core.db import get_session
from app.core.security import decrypt_cookies
from app.models.xhs_account import XhsAccount
from app.services import cookie_service

# check_cookies 回填到账号的 user_info 字段(与 cookie_service 一致的子集)
_USER_INFO_FIELDS = ("nickname", "user_id", "red_id", "avatar")


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

    @mcp.tool
    async def check_cookies(account_id: int) -> dict:
        """巡检某号 cookie 活性:起浏览器跑登录检测,把三态写回账号并返回 {status, user_info?}。

        鉴权后解密该号 cookie → 线程内 check_login_once(不阻塞事件循环)→ 三态
        status(valid|invalid|captcha)写回 cookie_status/last_check_at;valid 且带 user_info
        时回填 nickname/user_id/red_id/avatar。注:浏览器基础设施失败(启动失败等)会被
        check_login_once 保守归为 invalid,与真实 cookie 失效无法区分,v1 先接受。
        """
        operator = current_operator()
        async with get_session() as session:
            await assert_account_access(operator, account_id, session)
            account = await session.get(XhsAccount, account_id)
            if account is None:
                raise ValueError(f"账号 {account_id} 不存在")
            cookies = _decrypt_account_cookies(account)

        # 阻塞的 sync 浏览器调用下沉到线程,避免卡事件循环
        result = await asyncio.to_thread(
            sync_client.check_login_once, account_id, cookies
        )
        status = result.get("status", "invalid")
        user_info = result.get("user_info")

        # 写回巡检态 + 回填资料(在会话内重取账号,避免跨会话操作 detached 实例)
        async with get_session() as session:
            account = await session.get(XhsAccount, account_id)
            if account is not None:
                account.cookie_status = status
                account.last_check_at = datetime.utcnow()
                if user_info:
                    for field in _USER_INFO_FIELDS:
                        value = user_info.get(field)
                        if value:
                            setattr(account, field, value)
                await session.commit()

        return {"status": status, "user_info": user_info}


def _decrypt_account_cookies(account: XhsAccount) -> list[dict]:
    """直接解密账号 login_cookies 回列表(已鉴权,不再走 access 校验);空 → []。"""
    if account is None or not account.login_cookies:
        return []
    plaintext = decrypt_cookies(account.login_cookies)
    if not plaintext:
        return []
    return json.loads(plaintext)
