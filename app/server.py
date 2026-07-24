"""FastAPI 装配骨架 + /healthz 探活。

REST 装配:路由见 app/http 注册表,自描述见 GET /api/manifest。
重挂薄 MCP facade(/mcp,Streamable HTTP,给 claude.ai);业务仍在 REST。
"""

import asyncio
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastmcp.utilities.lifespan import combine_lifespans
from loguru import logger

import app.core.db as db_module
import app.worker as worker_module
from app.auth.bootstrap import bootstrap_admin
from app.auth.context import AccessDenied, AuthError
from app.auth.middleware import ApiKeyMiddleware
from app.core.config import assert_secret_key_configured
from app.core.errors import NotFoundError
from app.http import ALL_ROUTERS
from app.mcp_facade import mcp
from app.publish.runtime import set_active_scheduler


def create_app() -> FastAPI:
    """构建并返回装配好全部 REST 路由的 FastAPI 应用。"""
    # 0. 启动闸:生产必须设置非默认 SECRET_KEY(否则 Fernet 加密形同虚设),fail-fast 拒绝起服务。
    assert_secret_key_configured()

    # 1. 父应用 lifespan:建表 → 引导 root 管理员 → 按 NBDPSY_ROLE 角色接缝挂后台组件
    #    (设计 docs/design/2026-07-24-api-worker-split-design.md §二):
    #    - api:只做 init_db + bootstrap_admin —— 不起调度/巡检/reaper/视频,API 进程
    #      秒级起停,部署零停机;后台消费全部由独立 worker 进程(python -m app.worker)承担。
    #    - all(默认,单进程回滚位与测试):额外起 Supervisor 调度中枢,顶替旧
    #      PublishScheduler(发布扫描/巡检/reaper 统一归 Supervisor,见 app/worker.py;
    #      PublishScheduler 类保留作发布终态 policy 语义参照物,此处不再实例化)。
    #    - worker 角色不经 server.py(独立进程入口 python -m app.worker)。
    #    session_factory 在此处读 db_module.async_session 而非 import 期绑定,使测试对
    #    async_session 的 monkeypatch 生效(落隔离库、不碰生产库)。
    @asynccontextmanager
    async def app_lifespan(_app: FastAPI):
        await db_module.init_db()
        await bootstrap_admin()
        role = os.getenv("NBDPSY_ROLE", "all")
        supervisor = None
        supervisor_task: asyncio.Task | None = None
        if role == "all":
            # 经 worker_module 属性引用(而非 from-import),让测试可 monkeypatch
            # app.worker.Supervisor 注入假件断言启停。视频调度不在此起
            # (include_video 默认 False):拆分前 server 就不跑视频 worker
            # (独立进程 python -m app.video.worker),all 模式维持该行为零回归。
            supervisor = worker_module.Supervisor(db_module.async_session)
            supervisor_task = asyncio.create_task(supervisor.run())
        try:
            yield
        finally:
            # set_active_scheduler(None) 常态化:活跃调度器常态即 None(publish 端点的
            # 立即投递 nudge 静默跳过,由 worker 5s 扫描兜底);此处重置只为清掉测试
            # 注入的假调度器,防跨用例泄漏。
            set_active_scheduler(None)
            if supervisor is not None:
                supervisor.request_stop()
                if supervisor_task is not None:
                    await supervisor_task

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
