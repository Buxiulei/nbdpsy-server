"""异步 SQLAlchemy 数据库层:Base / engine / async_session / get_session / init_db。

生产语义:模块级 engine 在 import 时依 settings.DATABASE_URL 建好,
init_db() 与 get_session() 均基于该模块级 engine 工作。测试隔离由
tests/conftest.py 的 db fixture(独立临时 engine)承担,不改动此处生产语义。
"""

from contextlib import asynccontextmanager

from sqlalchemy import event
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from app.core.config import settings


class Base(DeclarativeBase):
    """所有 ORM 模型的声明式基类;Base.metadata 汇总全部表结构。"""


def _create_engine(url: str):
    """按 URL 类型构建 async engine。

    sqlite:开 WAL(多读并发一写)+ busy_timeout(锁竞争时等待而非立即报
    "database is locked"),支撑 20+ 运营并发写。非 sqlite(如 Postgres 迁移路径)
    原样构建,不套 sqlite-only 的 connect_args/pragma,避免把 timeout 传给 pg 驱动而崩。
    """
    is_sqlite = url.startswith("sqlite")
    if not is_sqlite:
        return create_async_engine(url, echo=False, future=True)

    # aiosqlite 的 timeout 即连接级忙等超时(秒):底层 sqlite3.connect(timeout=)。
    new_engine = create_async_engine(
        url,
        echo=False,
        future=True,
        connect_args={"timeout": settings.SQLITE_BUSY_TIMEOUT},
    )

    @event.listens_for(new_engine.sync_engine, "connect")
    def _set_sqlite_pragma(dbapi_conn, _connection_record):
        """每个新连接执行:开 WAL(库级持久)+ 设 busy_timeout(连接级,须每连设)。"""
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute(f"PRAGMA busy_timeout={settings.SQLITE_BUSY_TIMEOUT * 1000}")
        cursor.close()

    return new_engine


# 模块级 engine:import 时按配置构建。create_async_engine 惰性连接,
# 仅构建不会立即落盘,故 import app.core.db 不会创建生产库文件。
engine = _create_engine(settings.DATABASE_URL)

# 会话工厂:expire_on_commit=False,提交后仍可安全读取已加载对象属性。
async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


@asynccontextmanager
async def get_session():
    """异步会话上下文:进入得到 AsyncSession,退出自动关闭。"""
    async with async_session() as session:
        yield session


async def init_db() -> None:
    """导入模型完成注册后,在模块级 engine 上建好所有表。"""
    import app.models  # noqa: F401  触发模型注册到 Base.metadata

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
