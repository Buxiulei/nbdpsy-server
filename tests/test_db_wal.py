"""core/db 的 SQLite WAL + busy_timeout 加固单测。

背景:20+ 运营并发写同一 sqlite 库时,默认 journal_mode=delete 会让后到的写直接
报 "database is locked"。开 WAL(多读并发一写)+ busy_timeout(锁竞争时等待而非立即
报错)把"报错"变成"排队等"。加固只针对 sqlite;Postgres 迁移路径必须跳过 sqlite-only
的 connect_args/pragma,否则传给 pg 会崩。

隔离策略:两用例均用 db._create_engine 复用生产建法,不碰模块级生产 engine,也不真连 pg。
"""

from sqlalchemy import text

import app.core.db as db_module
from app.core.config import settings


async def test_sqlite_engine_wal_and_timeout(tmp_path):
    """sqlite engine 连接后:journal_mode == wal;busy_timeout == SQLITE_BUSY_TIMEOUT*1000。"""
    url = f"sqlite+aiosqlite:///{tmp_path}/wal.db"
    engine = db_module._create_engine(url)
    try:
        async with engine.connect() as conn:
            journal_mode = (await conn.execute(text("PRAGMA journal_mode"))).scalar()
            busy_timeout = (await conn.execute(text("PRAGMA busy_timeout"))).scalar()
        # WAL 是库级持久设置,连接后 PRAGMA 查询应返回 wal
        assert journal_mode == "wal"
        # busy_timeout 是连接级(毫秒),须每个新连接都设,值为配置秒数 * 1000
        assert busy_timeout == settings.SQLITE_BUSY_TIMEOUT * 1000
    finally:
        await engine.dispose()


def test_non_sqlite_skips_pragma(monkeypatch):
    """非 sqlite URL(如 Postgres):不套 sqlite-only 的 connect_args,也不挂 connect 事件。

    不真连 pg(环境未装 asyncpg)——monkeypatch create_async_engine 为桩,只验分支逻辑:
    is_sqlite 为假时,connect_args 不传、sync_engine 上无 connect 监听。
    """
    captured: dict = {}
    listened: list = []

    class _FakeEngine:
        """假 async engine;sync_engine 用于观测是否被挂 connect 事件监听。"""

        def __init__(self):
            self.sync_engine = object()

    def _fake_create_async_engine(url, **kwargs):
        captured["url"] = url
        captured["kwargs"] = kwargs
        return _FakeEngine()

    def _fake_listens_for(target, identifier, *args, **kwargs):
        # 记录任何在此 engine 上注册 connect 监听的意图(非 sqlite 不应发生)
        listened.append((target, identifier))
        return lambda fn: fn

    monkeypatch.setattr(db_module, "create_async_engine", _fake_create_async_engine)
    monkeypatch.setattr(db_module.event, "listens_for", _fake_listens_for)

    db_module._create_engine("postgresql+asyncpg://user:pass@localhost/db")

    # 非 sqlite 分支:绝不传 sqlite-only 的 connect_args(timeout 会让 pg 驱动报错)
    assert "connect_args" not in captured["kwargs"]
    # 非 sqlite 构造只带通用参数,与 sqlite 分支的 pragma/timeout 隔离
    assert captured["kwargs"] == {"echo": False, "future": True}
    # 且不挂任何 connect 事件监听(WAL/busy_timeout pragma 仅 sqlite 需要)
    assert listened == []
