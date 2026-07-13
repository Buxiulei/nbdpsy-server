"""cookie 活性巡检 REST(异步对):发起检测(202)+ 轮询结果。

平移自 app/tools/cookies.py 的 check_cookies / get_cookie_check MCP 工具(逻辑与鉴权点
原样搬,只是把入口从 MCP 工具换成 REST 端点):
- POST /api/accounts/{account_id}/cookie-checks:鉴权后解密该号 cookie → 交
  app.services.cookie_check 起后台任务(不阻塞等待,约 20-40s)→ 立即返回 check_id。
- GET /api/cookie-checks/{check_id}:轮询该 check_id 的检测结果,鉴权用台账里存的
  account_id 防越权;checking/valid/invalid/captcha/error 五态含义见 MANIFEST_ENTRIES。
"""

import json

from fastapi import APIRouter

from app.auth.context import current_operator
from app.auth.guards import assert_account_access
from app.core.db import get_session
from app.core.errors import NotFoundError
from app.core.security import decrypt_cookies
from app.models.xhs_account import XhsAccount
from app.services import cookie_check

router = APIRouter()

MANIFEST_ENTRIES = [
    {
        "method": "POST", "path": "/api/accounts/{account_id}/cookie-checks",
        "summary": "异步发起某号 cookie 活性检测",
        "admin_only": False, "params": {"account_id": "path,int"},
        "returns": '{check_id, status:"checking"}',
        "errors": "403=无该号授权;404=账号不存在",
        "notes": "检测约 20-40s,本调用不等待完成;别对同号重复发起;随后每 2-5s 轮询 "
                 "GET /api/cookie-checks/{check_id}。",
    },
    {
        "method": "GET", "path": "/api/cookie-checks/{check_id}",
        "summary": "轮询 cookie 活性检测结果",
        "admin_only": False, "params": {"check_id": "path,str"},
        "returns": "{status, user_info?, reason?}",
        "errors": "403=无该号授权;404=check_id 不存在或已过期",
        "notes": "status 五态:checking(仍在跑,继续轮询)/valid(登录态有效,附 user_info)/"
                 "invalid(未登录,cookie 真失效,需人重新扫码)/captcha(被验证码拦截,需人工过验证)/"
                 "error(浏览器起不来/超时等基础设施失败,不代表 cookie 失效,别据此让人重登,附 reason)。"
                 "check_id 是进程级内存台账,进程重启即丢,404 时重新发起检测。",
    },
]


@router.post("/api/accounts/{account_id}/cookie-checks", status_code=202)
async def start_cookie_check_endpoint(account_id: int) -> dict:
    """异步发起该号 cookie 活性检测,立即返回 check_id(检测 20-40s,不阻塞)。"""
    operator = current_operator()
    async with get_session() as session:
        await assert_account_access(operator, account_id, session)
        account = await session.get(XhsAccount, account_id)
        if account is None:
            raise NotFoundError(f"账号 {account_id} 不存在")
        cookies = _decrypt_account_cookies(account)
    check_id = cookie_check.start_check(account_id, cookies)
    return {"check_id": check_id, "status": "checking"}


@router.get("/api/cookie-checks/{check_id}")
async def get_cookie_check_endpoint(check_id: str) -> dict:
    """轮询检测结果:checking / valid / invalid / captcha / error(error≠cookie 失效)。"""
    entry = cookie_check.get_check(check_id)
    if entry is None:
        raise NotFoundError(f"check_id {check_id} 不存在或已过期")
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
