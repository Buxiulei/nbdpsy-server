"""cookies 分组 MCP 工具:插件/远程 agent 灌入 cookie / 回读 cookie / 活性巡检。

register_cookies(mcp) 注册 3 个工具,均取 current_operator() 收窄到有 access 的号:
- import_cookies:把 cookies(list 或 JSON 字符串)归一成 list[dict] 再 upsert 唯一账号行,
  返回 {account_id, created};新建时给导入 operator 建 access。
- get_cookies:鉴权后解密回读某号 cookie(受 access 限制,admin 放行)。
- check_cookies:鉴权后解密该号 cookie → 线程内起浏览器跑登录检测(check_login_once)→
  把 valid/invalid/captcha 写回 cookie_status/last_check_at(有 user_info 则回填资料)→ 返回。
  基础设施失败(error 态)不写回,保留原 cookie_status,避免把好号误标失效。
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

# import_cookies 入参非法时的中文提示(list 或 JSON 字符串两种合法形态)
_COOKIES_ARG_HINT = (
    'cookies_json 必须是 cookie 对象数组的 JSON 字符串,'
    '例:[{"name":...,"value":...}]'
)


def _parse_cookies_arg(cookies_json: "str | list") -> list[dict]:
    """把 import_cookies 的 cookies 入参归一为 list[dict](接受已解析的 list 或 JSON 字符串)。

    - list:直接使用(向后兼容 MCP 客户端已传数组的情形)
    - str:json.loads;解析失败给中文明确错误
    最终校验结果是 list 且元素均为 dict(cookie 对象),否则同样明确报错——早失败,不把
    脏数据吞进 upsert 造出坏账号行。
    """
    if isinstance(cookies_json, str):
        try:
            parsed = json.loads(cookies_json)
        except (json.JSONDecodeError, ValueError) as exc:
            raise ValueError(_COOKIES_ARG_HINT) from exc
    else:
        parsed = cookies_json
    if not isinstance(parsed, list) or not all(
        isinstance(item, dict) for item in parsed
    ):
        raise ValueError(_COOKIES_ARG_HINT)
    return parsed


def register_cookies(mcp: FastMCP) -> None:
    """把 cookies 分组工具注册到 mcp 实例(装饰器需闭包内的 mcp)。"""

    @mcp.tool
    async def import_cookies(
        account_name: str,
        cookies_json: str | list,
        user_info: dict | None = None,
    ) -> dict:
        """手动灌入某号 cookie(解析后 upsert 唯一账号,返回 {account_id, created})。

        多数情况**不用手动调**:正常"远程登录"是 chrome 插件在操作者真实浏览器登录后自动把
        cookie 推到后台 /api/cookies/import。本工具用于已有 cookie 的程序化注入。

        cookies_json 接受两种形态(向后兼容):
          - list:cookie 对象数组,直接使用
          - str:cookie 对象数组的 JSON 字符串(内部 json.loads)
        每个 cookie 对象形如 {"name":..., "value":..., "domain":..., "path":..., "sameSite":...}。
        user_info(可选)含 user_id/nickname/red_id/avatar;其中 **user_info.user_id 是 upsert
        的去重键**——同一 user_id 命中既有号走更新,否则按 account_name 兜底或新建。首次导入
        新号会自动给当前运营者建 access。
        """
        operator = current_operator()
        cookies = _parse_cookies_arg(cookies_json)
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
        """巡检某号 cookie 活性:起浏览器跑登录检测,把状态写回账号并返回 {status, ...}。

        **注意:本调用会真起浏览器,通常耗时 20-40s,请耐心等待、勿重复调用。**

        鉴权后解密该号 cookie → 线程内 check_login_once(不阻塞事件循环)→ 按状态处理:
          - valid:登录态有效,写回 cookie_status;带 user_info 时回填 nickname/user_id/red_id/avatar
          - invalid:页面正常加载但未登录(cookie 真失效),写回 cookie_status
          - captcha:被验证码/滑块拦截,写回 cookie_status
          - error:浏览器基础设施失败(启动失败/超时/异常),**不写回 cookie_status(保留原值)**,
            返回 {status:"error", reason}——这不代表 cookie 失效,别据此让人重登。
        valid/invalid/captcha 三态会写回 cookie_status/last_check_at;error 态不改任何字段。
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

        # D4:基础设施失败(error)不写回 —— 保留原 cookie_status,避免把好号误标失效。
        if status == "error":
            return {"status": "error", "reason": result.get("reason")}

        # valid/invalid/captcha:写回巡检态 + 回填资料(会话内重取账号,避免操作 detached 实例)
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
