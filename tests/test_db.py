"""core/db 单测:验证 Base / engine / async_session / get_session / init_db。

隔离策略:
- `test_db_fixture_executes` 直接用 conftest 的 db fixture(独立临时库)。
- `test_init_db_and_get_session` monkeypatch 模块级 engine/async_session 指向
  tmp_path 临时库,验证生产语义的 init_db()/get_session() 能建表并 select 1,
  且不污染生产库文件 ./data/nbdpsy.db。
"""

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

import app.core.db as db_module
from app.core.db import Base, get_session, init_db


def test_interfaces_exist():
    """产出接口齐全:Base 为 DeclarativeBase 子类,engine/async_session 就位。"""
    assert issubclass(Base, DeclarativeBase)
    assert db_module.engine is not None
    assert db_module.async_session is not None


async def test_db_fixture_executes(db: AsyncSession):
    """conftest 的 db fixture 能给出可用会话。"""
    assert (await db.execute(text("select 1"))).scalar() == 1


async def test_init_db_and_get_session(tmp_path, monkeypatch):
    """init_db() 建表 + get_session() 执行,均落在临时库、不碰生产文件。"""
    url = f"sqlite+aiosqlite:///{tmp_path}/t.db"
    tmp_engine = create_async_engine(url, future=True)
    tmp_sessionmaker = async_sessionmaker(
        tmp_engine, class_=AsyncSession, expire_on_commit=False
    )
    # init_db/get_session 在调用时按模块全局名解析,故 patch 模块属性即可生效
    monkeypatch.setattr(db_module, "engine", tmp_engine)
    monkeypatch.setattr(db_module, "async_session", tmp_sessionmaker)

    await init_db()
    async with get_session() as s:
        assert (await s.execute(text("select 1"))).scalar() == 1

    await tmp_engine.dispose()
