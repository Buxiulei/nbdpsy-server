"""每用户风格档案模型:当前档案(一运营一份)+ append-only 版本快照。

需求 /home/roots/NBDpsy/文档/2026-07-26-每用户风格档案-server需求.md。原先视觉调性
(莫兰迪三色 + 固定人物卡)是写死在 skill 文档里的全局常量,现降级为"管理员默认档案",
每个运营可有完全自主的一套。

两张表而非"一张表加历史列"的理由:
- ``style_profiles`` 是**当前态**(按 operator 唯一),读路径热、只取一行;
- ``style_profile_versions`` 是**append-only 快照**,永不 update/delete——回退实现为
  "以旧版内容造新版本"而不是拨版本指针,故历史只增不减,中间版本永远可回。
  存完整快照而非 diff:回退要一步取到完整内容,不做重放。

跨端接口约束:``profile`` 按 JSON **原样存取**,server 侧不校验语义、不做 key 规范化——
其中 density 的五个 key 是中文(信息密度档位/每页文字量/每页信息点/版式档/运营原话),
是 skill 侧 v1.37.0 定死的跨端契约,创作端与审查端都按这五个中文 key 读写,改名即断链。

宿主惯例:Mapped/mapped_column 声明式;一表一文件并在 app/models/__init__ 注册,使
Base.metadata 感知(init_db create_all 与 Alembic 据此建表)。
"""

from datetime import datetime

from sqlalchemy import (
    JSON,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base


class StyleProfile(Base):
    """某运营的当前风格档案;operator_id 唯一(一人一份当前态)。

    ``operator_id IS NULL`` 的那一行是**管理员默认档案**(无个人档案时的回落内容),
    由迁移 seed 一次。唯一约束在 SQLite/PG 下都不约束 NULL,故"只有一行 NULL"靠
    "应用层无任何写 NULL 行的路径"保证,不靠约束。
    """

    __tablename__ = "style_profiles"
    __table_args__ = (
        UniqueConstraint("operator_id", name="uq_style_profiles_operator"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    # 归属运营(需求里的"运营账号"在本仓即 operators 表);NULL = 管理员默认档案行。
    # ondelete=CASCADE 声明意图,但 SQLite 需 PRAGMA foreign_keys=ON 才真生效,而本仓
    # app/core/db.py 只设 journal_mode/busy_timeout —— 故真正的级联清空由
    # operator_service.delete_operator 在应用层显式做(与 OperatorAccountAccess 同款)。
    operator_id: Mapped[int | None] = mapped_column(
        ForeignKey("operators.id", ondelete="CASCADE"), nullable=True, default=None
    )
    # 当前版本号,从 1 起自增;管理员默认档案行置 0(它不参与版本流)
    version: Mapped[int] = mapped_column(Integer, default=1)
    # 风格内容:原样存取的 JSON(结构见需求文档第四节),server 不校验语义
    profile: Mapped[dict] = mapped_column("profile_json", JSON, default=dict)
    # 来源:manual / reference_sample / inherited_admin / rollback / admin_default(默认行)
    source: Mapped[str] = mapped_column(String(20), default="manual")
    note: Mapped[str | None] = mapped_column(Text, default=None)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_by: Mapped[int | None] = mapped_column(Integer, default=None)


class StyleProfileVersion(Base):
    """一条历史版本快照(append-only,永不删改);(operator_id, version) 唯一。

    唯一约束同时是"读当前 version → 写 version+1"这段读改写竞态的最后一道闸:
    并发双写至多落一条,不会出现两条同号版本。正常并发由 PUT 的 base_version
    乐观锁在业务层拦掉(409)。
    """

    __tablename__ = "style_profile_versions"
    __table_args__ = (
        UniqueConstraint(
            "operator_id", "version", name="uq_style_profile_versions_op_ver"
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    operator_id: Mapped[int] = mapped_column(
        ForeignKey("operators.id", ondelete="CASCADE"), index=True
    )
    version: Mapped[int] = mapped_column(Integer)
    # 该版本的**完整快照**(不是 diff):回退要能一步取到完整内容
    profile: Mapped[dict] = mapped_column("profile_json", JSON, default=dict)
    source: Mapped[str] = mapped_column(String(20), default="manual")
    note: Mapped[str | None] = mapped_column(Text, default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    created_by: Mapped[int | None] = mapped_column(Integer, default=None)
