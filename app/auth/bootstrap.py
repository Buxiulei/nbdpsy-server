"""启动引导:确保存在一个 root 管理员(role=admin, name="root")。

- settings.ROOT_ADMIN_APIKEY 非空:upsert root 管理员,apikey_hash 与配置对齐
  (幂等——重启同 key 不产生重复行,换 key 则更新 hash)。
- 为空:仅当尚无 root 时,generate_apikey() 建一个并 logger.warning 打印明文一次
  (幂等——已有 root 则不重复生成,避免每次重启刷新旧 admin)。

在 create_app() 的 lifespan 里 init_db() 之后调用。
"""

from loguru import logger
from sqlalchemy import select

from app.core.config import settings
from app.core.db import get_session
from app.core.security import generate_apikey, hash_apikey
from app.models.operator import Operator


async def bootstrap_admin() -> None:
    """确保 root 管理员存在;策略见模块文档。"""
    async with get_session() as session:
        existing = (
            await session.execute(select(Operator).where(Operator.name == "root"))
        ).scalar_one_or_none()

        if settings.ROOT_ADMIN_APIKEY:
            new_hash = hash_apikey(settings.ROOT_ADMIN_APIKEY)
            if existing is None:
                session.add(
                    Operator(
                        name="root",
                        role="admin",
                        apikey_hash=new_hash,
                        enabled=True,
                    )
                )
                logger.info("bootstrap: 已创建 root 管理员(来自 ROOT_ADMIN_APIKEY)")
            else:
                # 幂等对齐:hash/role/enabled 与当前配置保持一致。
                changed = False
                if existing.apikey_hash != new_hash:
                    existing.apikey_hash = new_hash
                    changed = True
                if existing.role != "admin":
                    existing.role = "admin"
                    changed = True
                if not existing.enabled:
                    existing.enabled = True
                    changed = True
                if changed:
                    logger.info("bootstrap: 已更新 root 管理员以匹配 ROOT_ADMIN_APIKEY")
            await session.commit()
            return

        # ROOT_ADMIN_APIKEY 为空:已有 root 则不动;否则生成并打印明文一次。
        if existing is not None:
            return
        plain = generate_apikey()
        session.add(
            Operator(
                name="root",
                role="admin",
                apikey_hash=hash_apikey(plain),
                enabled=True,
            )
        )
        await session.commit()
        logger.warning(
            "bootstrap: 未配置 ROOT_ADMIN_APIKEY,已生成 root 管理员 apikey"
            f"(仅打印一次,请立即保存): {plain}"
        )
