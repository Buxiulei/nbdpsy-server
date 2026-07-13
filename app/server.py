"""FastAPI 装配骨架 + /healthz 探活。

纯 REST 装配:路由见 app/http 注册表,自描述见 GET /api/manifest。
"""

from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from loguru import logger

import app.core.db as db_module
from app.auth.bootstrap import bootstrap_admin
from app.auth.context import AccessDenied, AuthError
from app.auth.middleware import ApiKeyMiddleware
from app.browser.account_locks import account_locks
from app.browser.cookie_checker import CookieChecker
from app.core.config import assert_secret_key_configured, settings
from app.core.errors import NotFoundError
from app.http import ALL_ROUTERS
from app.publish.runtime import set_active_scheduler
from app.publish.scheduler import PublishScheduler


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
        try:
            yield
        finally:
            set_active_scheduler(None)
            await scheduler.stop()
            if cookie_checker is not None:
                await cookie_checker.stop()

    app = FastAPI(title="nbdpsy-api", lifespan=app_lifespan)

    # 2. apikey 鉴权中间件:白名单(/healthz、/downloads)放行,其余(含 /api/*)校验 apikey。
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

    return app
