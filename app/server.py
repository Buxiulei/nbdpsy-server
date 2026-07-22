"""FastAPI 装配骨架 + /healthz 探活。

REST 装配:路由见 app/http 注册表,自描述见 GET /api/manifest。
重挂薄 MCP facade(/mcp,Streamable HTTP,给 claude.ai);业务仍在 REST。
"""

from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastmcp.utilities.lifespan import combine_lifespans
from loguru import logger

import app.core.db as db_module
from app.auth.bootstrap import bootstrap_admin
from app.auth.context import AccessDenied, AuthError
from app.auth.middleware import ApiKeyMiddleware
from app.browser.account_locks import account_locks
from app.browser.browser_reaper import BrowserReaper
from app.browser.cookie_checker import CookieChecker
from app.core.config import assert_secret_key_configured, settings
from app.core.errors import NotFoundError
from app.http import ALL_ROUTERS
from app.mcp_facade import mcp
from app.publish.runtime import set_active_scheduler
from app.publish.scheduler import PublishScheduler
from app.services.placeholder_reaper import PlaceholderReaper


def create_app() -> FastAPI:
    """构建并返回装配好全部 REST 路由的 FastAPI 应用。"""
    # 0. 启动闸:生产必须设置非默认 SECRET_KEY(否则 Fernet 加密形同虚设),fail-fast 拒绝起服务。
    assert_secret_key_configured()

    # 1. 父应用 lifespan:建表 → 引导 root 管理员 → 起发布调度器(队列 worker + scan 循环)→
    #    可选起 cookie 周期巡检。发布调度器经模块级单例交给 publish_note 端点(投递立即发布任务)。
    #    session_factory 在此处读 db_module.async_session 而非 import 期绑定,使测试对
    #    async_session 的 monkeypatch 生效(落隔离库、不碰生产库)。cookie 巡检默认关闭
    #    (COOKIE_CHECK_INTERVAL=0),测试不受影响。
    @asynccontextmanager
    async def app_lifespan(_app: FastAPI):
        await db_module.init_db()
        await bootstrap_admin()
        # 传入进程级共享 account_locks:发布链与 cookie 检测后台任务共用同一把 per-account 锁,
        # 同号浏览器操作串行,避免 SyncClient.start() 的 kill_orphans 误杀对方在跑的浏览器。
        scheduler = PublishScheduler(db_module.async_session, account_locks=account_locks)
        scheduler.start()
        set_active_scheduler(scheduler)
        # 可选后台 cookie 巡检:仅在配置 >0 时起(逐个号跑登录检测并写回状态,号间隔防频控)。
        cookie_checker: CookieChecker | None = None
        if settings.COOKIE_CHECK_INTERVAL > 0:
            cookie_checker = CookieChecker(
                db_module.async_session, settings.COOKIE_CHECK_INTERVAL
            )
            cookie_checker.start()
        # 可选孤儿 camoufox 回收:仅在配置 >0 时起(周期扫 /proc 杀无主超龄残留防内存泄露)。
        reaper: BrowserReaper | None = None
        if settings.BROWSER_REAP_INTERVAL > 0:
            reaper = BrowserReaper(settings.BROWSER_REAP_INTERVAL)
            reaper.start()
        # 可选占位废账号 TTL 兜底回收:仅在配置 >0 时起(周期删超龄未回填 user_id 的
        # xhs_account_ 占位行,兜底"登录失败后一直没重试"的残留)。
        placeholder_reaper: PlaceholderReaper | None = None
        if settings.PLACEHOLDER_REAP_INTERVAL > 0:
            placeholder_reaper = PlaceholderReaper(
                db_module.async_session, settings.PLACEHOLDER_REAP_INTERVAL
            )
            placeholder_reaper.start()
        try:
            yield
        finally:
            set_active_scheduler(None)
            await scheduler.stop()
            if cookie_checker is not None:
                await cookie_checker.stop()
            if reaper is not None:
                await reaper.stop()
            if placeholder_reaper is not None:
                await placeholder_reaper.stop()

    # 1.1 薄 MCP facade 的 Streamable HTTP ASGI app(子 app 内路径 "/",挂到父应用 /mcp)。
    #      host_origin_protection=False:关掉 MCP 传输层的 Host/Origin(DNS-rebinding)防护,
    #      否则经反代/隧道进来的公网 Host(如 mcp.nbdpsy.com)会被判 421 Misdirected Request;
    #      本服务真正的鉴权是 apikey 中间件(每个 /mcp 调用都要 Bearer),该防护在此冗余。
    #      其 lifespan 必须与父 lifespan 组合(combine_lifespans),否则 MCP session manager 的
    #      task group 不启动,/mcp 请求会报错。
    mcp_app = mcp.http_app(path="/", host_origin_protection=False)

    app = FastAPI(
        title="nbdpsy-api",
        lifespan=combine_lifespans(app_lifespan, mcp_app.lifespan),
    )

    # 2. apikey 鉴权中间件:白名单(/healthz、/downloads)放行,其余(含 /api/*、/mcp/)校验 apikey。
    app.add_middleware(ApiKeyMiddleware)

    # 2.1 app 级异常处理器:把 REST 端点里抛出的鉴权异常转成干净 HTTP,不泄栈成 500。
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

    @app.exception_handler(NotFoundError)
    async def _handle_not_found(_request: Request, exc: NotFoundError) -> JSONResponse:
        """资源不存在 → 404 JSON。"""
        return JSONResponse({"error": str(exc)}, status_code=404)

    @app.exception_handler(ValueError)
    async def _handle_value_error(_request: Request, exc: ValueError) -> JSONResponse:
        """入参非法 → 400 JSON(NotFoundError 是其子类但按精确类优先走 404)。"""
        return JSONResponse({"error": str(exc)}, status_code=400)

    @app.exception_handler(Exception)
    async def _handle_unexpected(_request: Request, exc: Exception) -> JSONResponse:
        """未预期异常 → 500 JSON,兜底统一错误契约。

        兑现 manifest error_contract 声明的 500 → {"error": ...}:没有这个 catch-all,
        非上述精确类的意外异常(RuntimeError/KeyError/SQLAlchemyError 等)会落到 Starlette
        默认的 text/plain "Internal Server Error",让"照 manifest 统一 resp.json()['error']"
        的 agent 消费方在 500 路径 JSONDecodeError。此处按精确类优先仅作最末兜底,不影响
        401/403/404/400 分派。返回通用文案不回显内部细节,真实异常落 loguru 供管理员排查。
        """
        logger.exception("未处理异常,返回 500")
        return JSONResponse(
            {"error": "服务器内部错误,请联系管理员查日志"}, status_code=500
        )

    # 3. 明文探活 REST:鉴权白名单放行,便于健康检查。
    @app.get("/healthz")
    async def healthz() -> dict:
        return {"ok": True}

    # 4. 挂载全部 REST 路由(system/manifest/accounts/admin/cookies/publish/extension/downloads);
    #    鉴权由 apikey 中间件按路径白名单统一把关,注册顺序见 app/http/__init__.py。
    for r in ALL_ROUTERS:
        app.include_router(r)

    # 5. 挂载薄 MCP facade 端点(给 claude.ai)。客户端须用 "/mcp/"(带结尾斜杠);
    #    POST "/mcp"(无斜杠)会 307。鉴权仍由 apikey 中间件把关(/mcp 不在白名单)。
    app.mount("/mcp", mcp_app)

    return app
