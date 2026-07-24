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
from app.auth.bootstrap import bootstrap_admin
from app.auth.context import AccessDenied, AuthError
from app.auth.middleware import ApiKeyMiddleware
from app.core.config import assert_secret_key_configured
from app.core.errors import NotFoundError
from app.http import ALL_ROUTERS
from app.mcp_facade import mcp
from app.browser.account_locks import account_locks
from app.publish.runtime import set_active_scheduler
from app.publish.scheduler import PublishScheduler
from app.services import browser_jobs_repo


def create_app() -> FastAPI:
    """构建并返回装配好全部 REST 路由的 FastAPI 应用。"""
    # 0. 启动闸:生产必须设置非默认 SECRET_KEY(否则 Fernet 加密形同虚设),fail-fast 拒绝起服务。
    assert_secret_key_configured()

    # 1. 父应用 lifespan:建表 → 引导 root 管理员 → 按 NBDPSY_ROLE 角色接缝挂后台组件
    #    (设计 docs/design/2026-07-24-api-worker-split-design.md §二):
    #    - api:只做 init_db + bootstrap_admin —— 不起调度/巡检/reaper/视频,API 进程
    #      秒级起停,部署零停机;后台消费全部由独立 worker 进程(python -m app.worker)承担。
    #    - all(默认,单进程回滚位与测试):**传统装配**——PublishScheduler 进程内发布
    #      + spawn_inline 四类任务 + 60s 台账清道夫;与拆分前语义一致(共享 account_locks
    #      同号互斥)。Supervisor/子进程派发专属 worker 角色,all 模式绝不引入(评审
    #      裁定:两执行域不共享进程锁会同号互杀)。
    #    - worker 角色不经 server.py(独立进程入口 python -m app.worker)。
    #    session_factory 在此处读 db_module.async_session 而非 import 期绑定,使测试对
    #    async_session 的 monkeypatch 生效(落隔离库、不碰生产库)。
    @asynccontextmanager
    async def app_lifespan(_app: FastAPI):
        await db_module.init_db()
        await bootstrap_admin()
        role = os.getenv("NBDPSY_ROLE", "all")
        scheduler: PublishScheduler | None = None
        janitor_task: asyncio.Task | None = None
        if role == "all":
            # all(单进程回滚位/测试位)= **传统装配**:PublishScheduler 进程内发布
            # (与 spawn_inline 的四类 browser_jobs 共享同一把进程级 account_locks——
            # 同号互斥的既有保障)+ 60s 台账清道夫(browser_jobs 僵死恢复,含发布外
            # 四类任务;发布僵死由 scheduler.recover_stale 自身覆盖)。
            # 评审裁定:all 模式绝不引入 Supervisor 子进程派发——子进程不共享父进程
            # account_locks,与 inline 构成两个互不可见的执行域,同号双 camoufox 会被
            # SyncClient.start() 的 kill_orphans 互杀。Supervisor 专属 worker 进程
            # (python -m app.worker,其内无 inline,账号互斥由"同号至多 1 子进程"保证)。
            scheduler = PublishScheduler(db_module.async_session, account_locks=account_locks)
            scheduler.start()
            set_active_scheduler(scheduler)

            async def _ledger_janitor() -> None:
                """all 模式台账清道夫:周期跑 browser_jobs 僵死恢复(inline 执行方
                崩溃/进程被杀留下的 running 孤儿)。执行期心跳已在消费路径补齐,
                正常在跑任务不会被误判。"""
                while True:
                    await asyncio.sleep(60)
                    try:
                        await browser_jobs_repo.recover_stale()
                    except Exception:
                        logger.exception("台账清道夫僵死恢复失败(下轮重试)")

            janitor_task = asyncio.create_task(_ledger_janitor())
        try:
            yield
        finally:
            set_active_scheduler(None)
            if janitor_task is not None:
                janitor_task.cancel()
                try:
                    await janitor_task
                except asyncio.CancelledError:
                    pass
            if scheduler is not None:
                await scheduler.stop()

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
