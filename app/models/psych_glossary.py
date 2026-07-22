"""心理学中英术语表——翻译阶段术语强约束 + 自动回写资产（迁自小红书运营工具 PsychGlossary）。

作用（transport/remake translate 阶段消费）：
- ``glossary.match_terms`` 把候选专名匹配到本表，产出术语一致约束（manual/approved > seed > auto）；
- 未命中的候选经 LLM 直译后由 ``glossary.upsert_auto_term`` 以 source=auto 回写本表，逐步沉淀。

迁移取舍（设计 §glossary）：源依赖运营工具 DB 表 + 由知识图谱生成的种子脚本。nbdpsy-server 无该
知识图谱，故本表**建表即空**、无种子——术语靠 auto 回写按需沉淀；行为等价源「术语表未命中→LLM 直译」
路径（源 seed 命中只是加速，缺失不改语义正确性）。选 DB 表而非 JSON 资产：宿主惯例一表一文件 +
upsert 天然是 DB 操作 + AsyncSession 并发写安全，比 JSON 文件回写更契合宿主。

宿主惯例：Mapped/mapped_column 声明式；一表一文件并在 app/models/__init__ 注册，使 Base.metadata
感知（init_db create_all 与 Alembic 据此建表）。属性名与源保持一致（en_term/zh_term/aliases/...）。
"""

from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, Float, String
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base


class PsychGlossary(Base):
    """一条心理学中英术语（归一化英文名唯一，含别名与来源优先级元信息）。"""

    __tablename__ = "psych_glossary"

    id: Mapped[int] = mapped_column(primary_key=True)
    # 归一化小写英文术语（match/upsert 主键，normalize_term 产物）
    en_term: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    zh_term: Mapped[str] = mapped_column(String(255))
    # 别名列表（en 主名之外的等价写法，match 时一并索引）；属性名 aliases，列名带 _json 后缀
    aliases: Mapped[list] = mapped_column("aliases_json", JSON, default=list)
    # 来源：seed（种子词典）| auto（自动回写候选）| manual（人工终审）
    source: Mapped[str] = mapped_column(String(10), default="auto")
    confidence: Mapped[float] = mapped_column(Float, default=0.5)
    # 人工终审标记：approved 叠加权重，始终压过任何自动来源
    approved: Mapped[bool] = mapped_column(Boolean, default=False)
    # 出处（entity id / doi / 论文标题），可空
    evidence: Mapped[str | None] = mapped_column(String(500), default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )
