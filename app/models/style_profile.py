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
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base


class StyleProfile(Base):
    """某运营某一「套」风格档案的当前态;一运营 N 套,每套一行。

    ``operator_id IS NULL`` 的行是**管理员默认档案**(无个人档案时的回落内容),也可多套。

    唯一性(设计 §9 B1):
    - 实 operator 靠联合唯一约束 ``(operator_id, name)``。
    - 管理员默认(NULL)靠**部分唯一索引** ``uq_admin_set_name``(WHERE operator_id IS NULL)——
      SQLite/PG 的 UNIQUE 都不约束 NULL(NULL≠NULL,两行 (NULL,'图文') 都能插),而本表新增
      管理员默认多套=写多 NULL 行,拆了"只有一行 NULL"的旧根基,故必须用部分索引兜住。
    - ``is_active``「同一 owner 恒有且仅一个 True」是**应用层不变量**(建套/设默认/删套事务内
      先改后数复核,同 operator_service._ensure_admin_remains),不靠 DB 约束。
    """

    __tablename__ = "style_profiles"
    __table_args__ = (
        UniqueConstraint(
            "operator_id", "name", name="uq_style_profiles_operator_name"
        ),
        # 管理员默认(NULL operator)的套名唯一:联合约束对 NULL 不生效,这里补部分唯一索引。
        Index(
            "uq_admin_set_name",
            "name",
            unique=True,
            sqlite_where=text("operator_id IS NULL"),
            postgresql_where=text("operator_id IS NULL"),
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    # 归属运营(需求里的"运营账号"在本仓即 operators 表);NULL = 管理员默认档案行。
    # ondelete=CASCADE 声明意图,但 SQLite 需 PRAGMA foreign_keys=ON 才真生效,而本仓
    # app/core/db.py 只设 journal_mode/busy_timeout —— 故真正的级联清空由
    # operator_service.delete_operator 在应用层显式做(与 OperatorAccountAccess 同款)。
    operator_id: Mapped[int | None] = mapped_column(
        ForeignKey("operators.id", ondelete="CASCADE"), nullable=True, default=None
    )
    # 套名:运营自定义,≤20 显示宽度、允许中文;(operator_id, name) 唯一,NULL 靠部分索引唯一。
    name: Mapped[str] = mapped_column(String(40), default="图文")
    # 套类型:carousel(图文)/typeset(文字版);server 原样存不理解含义,客户端按它选套。
    kind: Mapped[str] = mapped_column(String(20), default="carousel")
    # 该套是否为该 owner 的默认套;应用层保证恒有且仅一个 True。
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
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
    """一条历史版本快照(append-only,永不删改);(set_id, version) 唯一。

    版本链挂在**套**(set_id)上而非 operator 上——回退/并发写以套为单位互不干扰。
    唯一约束同时是"读当前 version → 写 version+1"这段读改写竞态的最后一道闸:
    并发双写至多落一条,不会出现两条同号版本。正常并发由 PUT 的 base_version
    乐观锁在业务层拦掉(409)。

    ``operator_id`` 保留(不再作版本归属键):供 operator_service.delete_operator 按运营
    一次清空其全部套的历史(应用层级联,不依赖 SQLite 外键)。管理员默认(NULL operator)
    的套不进本表——``operator_id`` NOT NULL,且管理员默认不可回退,与拆分前语义一致。
    """

    __tablename__ = "style_profile_versions"
    __table_args__ = (
        UniqueConstraint(
            "set_id", "version", name="uq_style_profile_versions_set_ver"
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    # 归属套:FK → style_profiles.id;取代原先用 operator_id 关联版本的方式。
    set_id: Mapped[int] = mapped_column(
        ForeignKey("style_profiles.id", ondelete="CASCADE"), index=True
    )
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
