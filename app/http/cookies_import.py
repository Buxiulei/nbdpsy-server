"""POST /api/cookies/import —— chrome 插件推 cookie 的 HTTP 端点。

路径不在中间件白名单(白名单仅 /healthz、/downloads)→ 走 apikey 中间件校验后,端点内
current_operator() 即当前运营者;随后调 cookie_service.import_cookies upsert 唯一账号行。
请求体用 Pydantic 校验:字段缺失/类型不符由 FastAPI 直接 422,不进业务层。
"""

from fastapi import APIRouter
from pydantic import BaseModel

from app.auth.context import current_operator
from app.core.db import get_session
from app.services import cookie_service

router = APIRouter()

MANIFEST_ENTRIES = [{
    "method": "POST", "path": "/api/cookies/import",
    "summary": "灌入某号 cookie(upsert 唯一账号行)",
    "admin_only": False,
    "params": {"account_name": "body,str", "cookies": "body,list[cookie 对象]",
               "user_info": "body,dict|None(user_id/nickname/red_id/avatar)"},
    "returns": "{account_id, created, cleaned_placeholders}",
    "errors": "422=缺字段",
    "notes": "正常远程登录由 chrome 插件登录后自动推本端点,多数情况不用手调;"
             "user_info.user_id 是 upsert 去重键;首次导入新号自动给导入者建授权;"
             "cleaned_placeholders=本次真登录成功顺带清理的占位废账号数(user_info 空时恒 0)。",
}]


class CookiesImportRequest(BaseModel):
    """插件推送体:账号内部展示名 + cookie 列表 + 可选 user_info(回填 nickname/user_id 等)。"""

    account_name: str
    cookies: list[dict]
    user_info: dict | None = None


@router.post("/api/cookies/import")
async def import_cookies_endpoint(payload: CookiesImportRequest) -> dict:
    """灌入插件推送的 cookie:upsert 唯一账号行,返回 {account_id, created, cleaned_placeholders}。"""
    operator = current_operator()
    async with get_session() as session:
        account, created, cleaned = await cookie_service.import_cookies(
            session,
            operator,
            payload.account_name,
            payload.cookies,
            payload.user_info,
        )
        return {
            "account_id": account.id,
            "created": created,
            "cleaned_placeholders": cleaned,
        }
