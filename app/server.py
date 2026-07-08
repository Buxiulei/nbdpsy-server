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

import app.core.db as db_module
from app.auth.bootstrap import bootstrap_admin
from app.auth.context import AccessDenied, AuthError, current_operator
from app.auth.middleware import ApiKeyMiddleware
from app.browser.cookie_checker import CookieChecker
from app.core.config import assert_secret_key_configured, settings
from app.http.cookies_import import router as cookies_import_router
from app.http.downloads import router as downloads_router
from app.publish.runtime import set_active_scheduler
from app.publish.scheduler import PublishScheduler
from app.tools import register_all


# FastMCP 服务自述:agent 连上后经 MCP 拿到这段,等于整个服务的"docstring"。
# 讲清接入方式 + 三条最容易踩的使用要点;客户端安装步骤详见 README「安装 / 接入 MCP 客户端」。
MCP_INSTRUCTIONS = """nbdpsy-mcp:小红书运营能力的 MCP 后台(自动发布 / 多账号管理 / cookie 管理 / 远程登录)。

接入:传输为 Streamable HTTP,端点是本服务地址加 /mcp/(带结尾斜杠,无斜杠会 307),
鉴权头 Authorization: Bearer <你的-operator-apikey>(或 X-API-Key)。把本服务装进
Claude Code / Claude Desktop / Cursor 等客户端的完整步骤见项目 README「安装 / 接入 MCP 客户端」。

使用要点(重要):
- 除白名单外所有调用都要带 operator apikey;访问不属于你的小红书账号会抛 AccessDenied(403),admin 全见。
- 远程登录没有"登录工具":小红书登录及各种验证由人 + chrome 插件在真实浏览器完成——调
  get_extension_download 拿插件包递给操作者装好扫码,插件自动把 cookie(含 httpOnly)推回后台。
- 发布是异步的:publish_note 只返回 {job_id},必须用 get_publish_status(job_id) 轮询到 published/failed;仅图文无视频。
- 典型编排:whoami → list_accounts →(操作者用插件登录)→ check_cookies → publish_note → 轮询 get_publish_status。

发布硬约束速览(publish_note,均为服务端强制):
- 仅图文,图片 ≥1 且 ≤18 张(越界立即报错);标题按显示长度截断 ≤20、正文截断 ≤900、
  话题去重后截断 ≤10——长度类**均静默硬截断不报错**,请自行控长。
- schedule_time 定时发布**务必带时区偏移**(如 +08:00);不带偏移按 UTC 解释,会早/晚 8 小时。

登录闭环协议(没有登录工具,登录靠人 + 插件):
- 调 get_extension_download 把插件包 + 安装步骤 + apikey 引导语递给操作者,让其装好插件扫码登录。
- 发起后每 ~10s 轮询 list_accounts,直到出现新账号或某号 cookie_status 变 valid;建议设 5-10 分钟超时。
- 别盲调 check_cookies 探登录(它会起浏览器、20-40s);先看 list_accounts 的 cookie_status/last_check_at 做廉价预检。
"""


def create_app() -> FastAPI:
    """构建并返回挂载了 FastMCP 的 FastAPI 应用。"""
    # 0. 启动闸:生产必须设置非默认 SECRET_KEY(否则 Fernet 加密形同虚设),fail-fast 拒绝起服务。
    assert_secret_key_configured()

    # 1. FastMCP 实例 + 注册全部工具(此刻只 system.health)。
    mcp = FastMCP("nbdpsy-mcp", instructions=MCP_INSTRUCTIONS)
    register_all(mcp)

    # 2. Streamable HTTP ASGI app(子 app 内路径 "/",挂到父应用 /mcp)。
    #    关掉 MCP 传输层的 Host/Origin(DNS-rebinding)防护:它默认只放行 localhost,经反代/隧道
    #    进来的公网 Host(如 mcp.nbdpsy.com)会被判 421 Misdirected Request。本服务真正的鉴权是
    #    apikey 中间件(每个 /mcp 调用都要 Bearer),该防护针对"浏览器 DNS rebinding"在此冗余。
    mcp_app = mcp.http_app(path="/", host_origin_protection=False)

    # 3. 父应用 lifespan:建表 → 引导 root 管理员 → 起发布调度器(队列 worker + scan 循环)→
    #    可选起 cookie 周期巡检;需与 mcp_app.lifespan 组合以启动 session manager。发布调度器
    #    经模块级单例交给 publish_note 工具(投递立即发布任务)。session_factory 在此处读
    #    db_module.async_session 而非 import 期绑定,使测试对 async_session 的 monkeypatch 生效
    #    (落隔离库、不碰生产库)。cookie 巡检默认关闭(COOKIE_CHECK_INTERVAL=0),测试不受影响。
    @asynccontextmanager
    async def app_lifespan(_app: FastAPI):
        await db_module.init_db()
        await bootstrap_admin()
        scheduler = PublishScheduler(db_module.async_session)
        scheduler.start()
        set_active_scheduler(scheduler)
        # 可选后台 cookie 巡检:仅在配置 >0 时起(逐个号跑登录检测并写回状态,号间隔防频控)。
        cookie_checker: CookieChecker | None = None
        if settings.COOKIE_CHECK_INTERVAL > 0:
            cookie_checker = CookieChecker(
                db_module.async_session, settings.COOKIE_CHECK_INTERVAL
            )
            cookie_checker.start()
        try:
            yield
        finally:
            set_active_scheduler(None)
            await scheduler.stop()
            if cookie_checker is not None:
                await cookie_checker.stop()

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

    # 7.1 挂载插件包下载端点。路径落在中间件白名单 /downloads 前缀,无需 apikey 即可下载。
    app.include_router(downloads_router)

    # 8. 挂载 MCP 端点。客户端须用 "/mcp/"(带结尾斜杠);POST "/mcp"(无斜杠)会 307。
    app.mount("/mcp", mcp_app)
    return app
