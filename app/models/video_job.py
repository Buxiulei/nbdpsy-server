"""视频管线任务表：一条 transport（搬运）/ remake（分镜级再制作）/ revision（成片修订）任务。

设计意图与不变量（迁自小红书运营工具 VideoTransportJob，同构平移）：
- 一张表承载三种 mode，靠 mode 字段区分；revision 任务经 parent_job_id 指回被修订的父 job
  （派生链），普通任务 parent_job_id 为空——增量重制据此复用父 job 已缓存的阶段产物。
- 状态机：status ∈ {queued|running|completed|failed}，stage 为当前阶段名（取值见调度器
  STAGE_ORDER）。二者是调度器"原子占用 + 逐阶段自链"的锚点，M2/M3 依赖其语义。
- 大数据落盘、表里只存路径+计数（源铁律）：stages/options/products/term_sheet 四个 JSON 字段
  存的是各阶段元信息与产物索引，不塞原始音视频/逐句文本。**属性名与源保持一字不差**
  （stages/options/products/term_sheet），底层列名带 `_json` 后缀——M3 平移代码按 `.stages`
  `.options` 等原样访问，改列名会断掉平移面。
- heartbeat_at：阶段任务运行时每 300s touch 一次，恢复扫描据此判僵死（超 15min 未跳→从
  first_incomplete_stage 续跑）。这是方案 C worker 崩溃恢复的唯一依据，不可省。

宿主惯例：Mapped/mapped_column 声明式；一表一文件并在 app/models/__init__ 注册，使
Base.metadata 感知（init_db create_all 与 Alembic autogenerate 据此建表）。
"""

from datetime import datetime

from sqlalchemy import JSON, DateTime, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base


class VideoJob(Base):
    """一条视频管线任务（transport|remake|revision 三合一表）。"""

    __tablename__ = "video_jobs"

    id: Mapped[int] = mapped_column(primary_key=True)
    # 源视频地址（transport=YouTube 链接；remake/revision 亦复用此列记来源）
    url: Mapped[str] = mapped_column(String(500))
    # YouTube 视频 ID（去重/追溯用），可空
    video_id: Mapped[str | None] = mapped_column(String(64), index=True, default=None)
    title: Mapped[str | None] = mapped_column(String(500), default=None)
    duration_seconds: Mapped[int | None] = mapped_column(Integer, default=None)
    # 任务类型：transport（搬运）| remake（分镜级再制作）| revision（成片修订）
    mode: Mapped[str] = mapped_column(
        String(20), default="transport", server_default="transport"
    )
    # revision 派生链：指向被修订的父 job；普通 job 为空
    parent_job_id: Mapped[int | None] = mapped_column(Integer, index=True, default=None)
    # 生命周期：queued → running → completed / failed
    status: Mapped[str] = mapped_column(String(20), default="queued", index=True)
    # 当前阶段名，取值见调度器 STAGE_ORDER
    stage: Mapped[str] = mapped_column(String(20), default="download")
    # 各阶段元信息：{stage: {status, started_at, finished_at, error, stats}}
    stages: Mapped[dict] = mapped_column("stages_json", JSON, default=dict)
    # 任务入参（源链接/模式选项等）
    options: Mapped[dict] = mapped_column("options_json", JSON, default=dict)
    # 阶段产物索引（各阶段落盘路径 + 计数，不塞原始数据）
    products: Mapped[dict] = mapped_column("products_json", JSON, default=dict)
    # 术语表 [{"en","zh","source"}]，翻译阶段沉淀，供整片术语一致
    term_sheet: Mapped[list] = mapped_column("term_sheet_json", JSON, default=list)
    # 最近一次失败原因
    error: Mapped[str | None] = mapped_column(Text, default=None)
    retry_count: Mapped[int] = mapped_column(Integer, default=0)
    # 阶段任务定期 touch，恢复扫描判僵死
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime, default=None)
    # 创建者 operator id，不设 FK 降低耦合
    created_by: Mapped[int | None] = mapped_column(Integer, index=True, default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )
