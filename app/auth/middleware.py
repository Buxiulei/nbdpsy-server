"""apikey 鉴权中间件:从请求头取 apikey,校验后写入 Operator 上下文。

方案与 fastmcp 版本解耦:在父 FastAPI 上装 Starlette BaseHTTPMiddleware。
命中白名单(/healthz、/downloads)直接放行;其余请求(含挂载在 /mcp 的子 app)
必须携带有效 apikey(Authorization: Bearer <key> 或 X-API-Key),否则返回 401 JSON。

校验成功后把命中的 Operator 写入 ContextVar(set_current_operator),受保护的
REST 路由用 current_operator() 读取。BaseHTTPMiddleware 的 call_next 会以
copy_context() 派生子 task 跑下游 app,故 set 发生在 call_next 之前即可被
下游同 task 链继承。
"""

from sqlalchemy import select
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

from app.auth.context import reset_current_operator, set_current_operator
from app.core.db import get_session
from app.core.security import hash_apikey
from app.models.operator import Operator

# 无需鉴权的白名单:健康探活(精确)与下载静态资源(带斜杠前缀)。
# 下载白名单必须用带斜杠的边界前缀,否则裸 startswith("/downloads") 会把
# /downloads-evil、/downloadsX 等以此开头但非真下载路由的路径也误放行。
_WHITELIST_EXACT = frozenset({"/healthz"})
_DOWNLOADS_ROOT = "/downloads"


def _is_whitelisted(path: str) -> bool:
    """判断请求路径是否落在免鉴权白名单。

    healthz 走精确匹配;downloads 只放行 /downloads 本身或 /downloads/ 前缀下
    的子路径(带斜杠边界),避免 /downloads-evil / /downloadsX 借前缀绕过鉴权。
    """
    if path in _WHITELIST_EXACT:
        return True
    return path == _DOWNLOADS_ROOT or path.startswith(_DOWNLOADS_ROOT + "/")


def _extract_apikey(request: Request) -> str | None:
    """从 Authorization: Bearer 或 X-API-Key 取 apikey 明文;取不到返回 None。"""
    auth = request.headers.get("authorization", "")
    if auth[:7].lower() == "bearer ":
        key = auth[7:].strip()
        if key:
            return key
    key = request.headers.get("x-api-key")
    return key.strip() if key and key.strip() else None


class ApiKeyMiddleware(BaseHTTPMiddleware):
    """校验 apikey 并把命中的 Operator 写入 ContextVar 的鉴权中间件。"""

    async def dispatch(self, request: Request, call_next):
        # 1. 白名单路径直接放行,不校验、不写上下文。
        if _is_whitelisted(request.url.path):
            return await call_next(request)

        # 2. 取 apikey;缺失即 401。
        key = _extract_apikey(request)
        if not key:
            return JSONResponse({"detail": "缺失 apikey"}, status_code=401)

        # 3. 按 hash 反查启用中的 Operator;查不到即 401。
        key_hash = hash_apikey(key)
        async with get_session() as session:
            op = (
                await session.execute(
                    select(Operator).where(
                        Operator.apikey_hash == key_hash,
                        Operator.enabled.is_(True),
                    )
                )
            ).scalar_one_or_none()
        if op is None:
            return JSONResponse({"detail": "无效的 apikey"}, status_code=401)

        # 4. 写入上下文,放行下游;finally 复位避免请求间泄漏。
        token = set_current_operator(op)
        try:
            return await call_next(request)
        finally:
            reset_current_operator(token)
