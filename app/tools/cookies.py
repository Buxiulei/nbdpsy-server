"""cookies 分组 MCP 工具:插件/远程 agent 灌入 cookie / 回读 cookie / 活性巡检。

register_cookies(mcp) 注册 3 个工具,均取 current_operator() 收窄到有 access 的号:
- import_cookies:把 cookies(list 或 JSON 字符串)归一成 list[dict] 再 upsert 唯一账号行,
  返回 {account_id, created};新建时给导入 operator 建 access。
- get_cookies:鉴权后解密回读某号 cookie(受 access 限制,admin 放行)。
- check_cookies:**异步**——鉴权后解密该号 cookie → 经 cookie_check.start_check 起后台浏览器
  检测并**立即返回 {check_id, status:"checking"}**;真正结果用 get_cookie_check(check_id) 轮询。
- get_cookie_check:按 check_id 取后台检测结果(鉴权防越权看别人号);valid/invalid/captcha/error
  四终态,error 为基础设施失败(不代表 cookie 失效),写回语义见 cookie_check 服务。
"""

import json

from fastmcp import FastMCP

from app.auth.context import current_operator
from app.auth.guards import assert_account_access
from app.core.db import get_session
from app.core.security import decrypt_cookies
from app.models.xhs_account import XhsAccount
from app.services import cookie_check, cookie_service

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
        """异步巡检某号 cookie 活性:起后台浏览器检测,立即返回 check_id(不阻塞等待)。

        起浏览器检测约 20-40s,**本调用不等检测完成、立即返回** {check_id, status:"checking"};
        随后每 ~2-5s 调 get_cookie_check(check_id) 轮询,直到 status 变
        valid/invalid/captcha/error(四态含义见 get_cookie_check),别重复对同号发起检测。

        鉴权后解密该号 cookie → 交 cookie_check 起后台任务(线程内跑登录检测,写回 cookie_status/
        last_check_at,error 态不写回保留原值)→ 返回 check_id。
        """
        operator = current_operator()
        async with get_session() as session:
            await assert_account_access(operator, account_id, session)
            account = await session.get(XhsAccount, account_id)
            if account is None:
                raise ValueError(f"账号 {account_id} 不存在")
            cookies = _decrypt_account_cookies(account)

        check_id = cookie_check.start_check(account_id, cookies)
        return {"check_id": check_id, "status": "checking"}

    @mcp.tool
    async def get_cookie_check(check_id: str) -> dict:
        """轮询 check_cookies 发起的异步检测结果(受 access 限制,admin 放行)。

        status 四态 + 进行中:
          - checking:检测仍在跑(浏览器未返回),继续轮询。
          - valid:登录态有效;附 user_info(nickname/user_id/red_id/avatar)。
          - invalid:页面正常加载但未登录,cookie 真失效,需人重新扫码登录。
          - captcha:被验证码/滑块拦截,需人工过验证。
          - error:浏览器起不来/超时等基础设施失败,**不代表 cookie 失效**,附 reason,别据此让人重登。
        找不到 check_id(从未发起 / 进程重启已丢 / 拼错)会报错;鉴权用台账里存的 account_id,
        防越权查看别人号的检测结果。
        """
        entry = cookie_check.get_check(check_id)
        if entry is None:
            raise ValueError(f"check_id {check_id} 不存在或已过期")
        operator = current_operator()
        async with get_session() as session:
            await assert_account_access(operator, entry["account_id"], session)

        result: dict = {"status": entry["status"]}
        if entry.get("user_info") is not None:
            result["user_info"] = entry["user_info"]
        if entry.get("reason") is not None:
            result["reason"] = entry["reason"]
        return result


def _decrypt_account_cookies(account: XhsAccount) -> list[dict]:
    """直接解密账号 login_cookies 回列表(已鉴权,不再走 access 校验);空 → []。"""
    if account is None or not account.login_cookies:
        return []
    plaintext = decrypt_cookies(account.login_cookies)
    if not plaintext:
        return []
    return json.loads(plaintext)
