"""异步 SQLAlchemy 数据库层:Base / engine / async_session / get_session / init_db。

生产语义:模块级 engine 在 import 时依 settings.DATABASE_URL 建好,
init_db() 与 get_session() 均基于该模块级 engine 工作。测试隔离由
tests/conftest.py 的 db fixture(独立临时 engine)承担,不改动此处生产语义。
"""

from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from app.core.config import settings


class Base(DeclarativeBase):
    """所有 ORM 模型的声明式基类;Base.metadata 汇总全部表结构。"""


# 模块级 engine:import 时按配置构建。create_async_engine 惰性连接,
# 仅构建不会立即落盘,故 import app.core.db 不会创建生产库文件。
engine = create_async_engine(settings.DATABASE_URL, echo=False, future=True)

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
