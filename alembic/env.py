"""Alembic 迁移环境。

改造要点(相对 alembic init 默认模板):
- target_metadata 指向 app.core.db.Base.metadata,并 `import app.models` 触发全部
  模型注册,使 autogenerate 能感知 4 张核心表。
- 迁移用同步驱动:从 settings.DATABASE_URL 去掉 `+aiosqlite`(async 驱动),
  例如 sqlite+aiosqlite:///./data/nbdpsy.db → sqlite:///./data/nbdpsy.db。
- SQLite 开启 batch 模式(render_as_batch),使后续 ALTER 类迁移可用。
"""

from logging.config import fileConfig

from sqlalchemy import engine_from_config, pool

from alembic import context

import app.models  # noqa: F401  触发 4 张核心表注册到 Base.metadata
from app.core.config import settings
from app.core.db import Base

# Alembic Config 对象:提供对 .ini 文件内配置的访问。
config = context.config

# 迁移用同步 url:去掉 async 驱动后缀,避免 alembic 走异步 DBAPI。
sync_url = settings.DATABASE_URL.replace("+aiosqlite", "")
config.set_main_option("sqlalchemy.url", sync_url)

# 配置 Python 日志。
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# autogenerate 依据的模型元数据。
target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """离线模式:仅用 url 生成迁移 SQL,不建立实际连接。"""
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_as_batch=True,
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """在线模式:建 Engine 连接后执行迁移。"""
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            render_as_batch=True,
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
