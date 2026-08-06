"""视频资产库模型：从 clip 产物显式转存出来的**长期**镜头资产。

需求 ``NBDpsy/文档/2026-08-06-server需求-制片级四缺口（单价估算-资产库-预算护栏-段帧提取）.md``
第三节。即梦是抽卡式生成，偶尔出一条完美镜头（角色定妆动起来那条、光线绝佳的空镜），
它是**跨片资产**：等 multimodal2video 的 videos[] 开放后，上一部片的好镜头就是下一部片
最好的运镜/风格参考。而 clip 产物 ``expires_at`` 一到就被 ClipReaper 连目录删掉。

与 video_clips 的三条本质区别：

- **无 TTL 列**。资产不过期就是它存在的理由；清理靠运营显式 DELETE。
- **文件是独立副本**，不是指向 clip 工作目录的指针——源 clip 到期即被整目录删除，
  存指针等于假长期（比照 content_archive 的媒体独立副本）。
- **按 caller 归属**（``created_by``）。clip 是任务、资产是个人素材库，
  ``(created_by, source_clip_id)`` 联合唯一 = 幂等键：同一人重复转存同一条 clip
  回原资产而不是再拷一份（存储是成本，不能被重放刷爆）。

``source_*`` 几列是**转存那一刻的 clip 快照**：源 clip 行会随 TTL 被清产物（error 行更是
只剩台账），事后再 join 已拿不全，且资产要能脱离 clip 独立检索（q 搜提示词就靠这一列）。

宿主惯例：Mapped/mapped_column 声明式；一表一文件并在 ``app/models/__init__`` 注册。
"""

from datetime import datetime

from sqlalchemy import DateTime, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base


class VideoAsset(Base):
    """一条长期视频资产（一个镜头一行，文件是 clip 产物的独立副本）。"""

    __tablename__ = "video_assets"
    __table_args__ = (
        # 幂等键按运营隔离：同一人的同一条 clip 只存一份；跨运营互不干扰。
        UniqueConstraint("created_by", "source_clip_id", name="uq_video_assets_creator_clip"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    # 对外公开句柄 va_<10hex>（形态与 vc_/vcb_ 同族，直链目录名由它派生 HMAC）
    asset_id: Mapped[str] = mapped_column(String(24), unique=True, index=True)

    name: Mapped[str] = mapped_column(String(120))
    tags_json: Mapped[str] = mapped_column(String, default="[]")

    # 转存时的 clip 快照（源行可能已被清产物，不能指望事后 join）
    source_clip_id: Mapped[str] = mapped_column(String(24), index=True)
    source_operation: Mapped[str | None] = mapped_column(String(20), default=None)
    source_model: Mapped[str | None] = mapped_column(String(30), default=None)
    source_prompt: Mapped[str | None] = mapped_column(Text, default=None)
    duration: Mapped[int | None] = mapped_column(Integer, default=None)
    # 副本体积：资产不过期，盘是有限的，列表回显它让运营看得见存储成本
    size_bytes: Mapped[int | None] = mapped_column(Integer, default=None)

    # 归属运营 id，不设 FK 降低耦合（与 video_clips 同惯例）
    created_by: Mapped[int] = mapped_column(Integer, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
