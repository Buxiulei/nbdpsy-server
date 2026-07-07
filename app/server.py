"""FastAPI + FastMCP(Streamable HTTP)骨架 + /healthz 探活。

================= fastmcp API 结论 =================
来源:context7(/prefecthq/fastmcp)核对 + 本地实测,安装版本 fastmcp 3.4.3。

1. 创建实例 & 注册工具
   mcp = FastMCP("名字")
   用装饰器 @mcp.tool(不带括号)把函数注册为工具;工具返回 dict 时,
   FastMCP 会同时给出 structured_content(dict)与 JSON 文本 content。
   实例上直接有异步 mcp.list_tools() / mcp.call_tool(name, args)(3.x;
   注意不是 2.x 的 get_tools(),也无 _tool_manager 私有属性)。

2. 取 Streamable HTTP ASGI app & 挂到 FastAPI
   mcp_app = mcp.http_app(path="/")     # 返回 StarletteWithLifespan
   # http_app 的 transport 默认 "http" 即 Streamable HTTP(SSE 为兼容旧协议)。
   # 子 app 内路径设 "/",挂到父应用 /mcp,则 MCP 端点落在 /mcp/。
   # 关键:必须把 mcp_app.lifespan 交给父应用,否则 MCP session manager 的
   #       task group 不初始化,请求 /mcp 会因 task group 未启动而报错。
   # 与父应用自身 lifespan 组合,用 fastmcp.utilities.lifespan.combine_lifespans:
   app = FastAPI(lifespan=combine_lifespans(app_lifespan, mcp_app.lifespan))
   app.mount("/mcp", mcp_app)
   # /healthz 是父应用上的独立明文路由,不经过 /mcp 子 app,故不依赖上面的
   # lifespan——探活/后续鉴权白名单选它正因如此。

3. 工具内读取 HTTP 请求头(Task 1.1 apikey 中间件将用)
   from fastmcp.server.dependencies import get_http_headers
   headers = get_http_headers()          # -> dict[str, str];无 HTTP 上下文返回 {}
   # 默认剔除 host/content-length 等;get_http_headers(include_all=True) 取全部。
   # 另有依赖注入写法(本地已确认可 import):
   #   from fastmcp.dependencies import CurrentHeaders, CurrentRequest
   #   @mcp.tool
   #   async def t(headers: dict = CurrentHeaders()): ...
   # Task 1.1 优先用 get_http_headers()(免改工具签名,鉴权在中间件/依赖里统一取)。
===================================================
"""

from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastmcp import FastMCP
from fastmcp.utilities.lifespan import combine_lifespans

from app.auth.bootstrap import bootstrap_admin
from app.auth.context import AccessDenied, AuthError, current_operator
from app.auth.middleware import ApiKeyMiddleware
from app.core.db import init_db
from app.http.cookies_import import router as cookies_import_router
from app.tools import register_all


def create_app() -> FastAPI:
    """构建并返回挂载了 FastMCP 的 FastAPI 应用。"""
    # 1. FastMCP 实例 + 注册全部工具(此刻只 system.health)。
    mcp = FastMCP("nbdpsy-mcp")
    register_all(mcp)

    # 2. Streamable HTTP ASGI app(子 app 内路径 "/",挂到父应用 /mcp)。
    mcp_app = mcp.http_app(path="/")

    # 3. 父应用 lifespan:建表后引导 root 管理员;需与 mcp_app.lifespan 组合以启动 session manager。
    @asynccontextmanager
    async def app_lifespan(_app: FastAPI):
        await init_db()
        await bootstrap_admin()
        yield

    app = FastAPI(
        title="nbdpsy-mcp",
        lifespan=combine_lifespans(app_lifespan, mcp_app.lifespan),
    )

    # 4. apikey 鉴权中间件:白名单(/healthz、/downloads)放行,其余(含 /mcp/)校验 apikey。
    app.add_middleware(ApiKeyMiddleware)

    # 4.1 app 级异常处理器:把 REST 端点里抛出的鉴权异常转成干净 HTTP,不泄栈成 500。
    #     (MCP 工具内部抛这些异常时 fastmcp 会自行包成工具错误返回,不走这里。)
    @app.exception_handler(AuthError)
    async def _handle_auth_error(_request: Request, exc: AuthError) -> JSONResponse:
        """未认证/认证失败 → 401 JSON。"""
        return JSONResponse({"error": str(exc)}, status_code=401)

    @app.exception_handler(AccessDenied)
    async def _handle_access_denied(
        _request: Request, exc: AccessDenied
    ) -> JSONResponse:
        """越权 → 403 JSON。仅映射专用 AccessDenied,不碰内置 PermissionError
        (后者是 OSError 子类,真实 OS 权限错误应自然走 500,不被误转 403 掩盖真因)。"""
        return JSONResponse({"error": str(exc)}, status_code=403)

    # 5. 明文探活 REST:独立于 /mcp,鉴权白名单放行,便于健康检查。
    @app.get("/healthz")
    async def healthz() -> dict:
        return {"ok": True}

    # 6. 受保护探针 REST:中间件校验通过后,读取上下文里的当前运营者(验证 ContextVar 穿透 REST 路由)。
    @app.get("/api/whoami")
    async def whoami() -> dict:
        op = current_operator()  # 未认证时中间件已拦截返回 401,此处必有运营者
        return {"name": op.name, "role": op.role}

    # 7. 挂载 REST 路由:插件推 cookie 端点。路径不在中间件白名单,自动受 apikey 保护。
    app.include_router(cookies_import_router)

    # 8. 挂载 MCP 端点。客户端须用 "/mcp/"(带结尾斜杠);POST "/mcp"(无斜杠)会 307。
    app.mount("/mcp", mcp_app)
    return app
