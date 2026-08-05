"""即梦(Dreamina)视频片段任务表：一条 text2video / image2video / multimodal2video 生成任务。

设计 ``docs/design/2026-08-05-dreamina-clips-design.md``；需求契约
``NBDpsy/文档/2026-08-05-server需求-即梦视频生成服务化.md``。

设计意图与不变量（每条都有事故背书，见需求第四节）：

- ``clip_id`` 是**对外唯一句柄**，形态钉死 ``vc_`` + 10 位 hex（``secrets.token_hex(5)``）。
  **绝不能是 16 位纯小写 hex**——那正是本机 dreamina CLI ``submit_id`` 的形态，skill 侧
  auto 模式靠形态判断「这个 id 问 server 还是问本机 CLI」，撞车会把 server 的 clip 派到本机
  CLI 查一个不存在的任务、空转到超时（需求第三节第 1 条 / 验收第 8 条）。
- ``(created_by, client_ref)`` 联合唯一 = 幂等键，**按运营隔离**。同 ref 重发返回已有 clip
  而不是新建：submit 即占即梦队列位、success 即扣积分，重复提交不是浪费时间而是烧钱。
  联合而非单列唯一，防跨运营 ref 抢注（A 用 ref="shot-1" 后 B 也用 ref="shot-1" 会拿到
  A 的 clip_id——既抢注又泄漏他人任务状态）。SQLite 里 NULL 不参与唯一比较，故不带
  client_ref 的任务可任意多条。
- 状态机 ``queued → submitting → submitted → querying → done|error``。``submitting`` 是
  **内部态**（CLI 调用在途），对外 GET 一律映射回 ``queued``——契约状态机只有五态。
  它的存在是为了让「原子认领」有落点：``UPDATE ... WHERE status='queued'`` rowcount==1 才算
  占到，杜绝两轮扫描对同一 clip 二次 submit（= 双倍扣积分）。
- ``submit_id`` 是钱：一旦拿到就绝不覆盖、绝不自动重提。卡 querying 数小时是**正常排队**
  （高峰 fast 近 2 小时），重提是运营的决策，服务端只如实下发 ``queued_seconds``。
- ``last_poll_error`` 与 ``error`` 分离：查询接口连续失败（异常）≠ 任务失败（终态）。
  排队中的任务无法取消，把查询侧抖动写进 ``error`` 会让运营误判任务已死而重发。
- ``expires_at``：产物 MP4 落盘给免鉴权直链，到期由 ClipReaper 删目录并清 ``video_url``，
  但 ``status`` 保持 done、``credit_count`` 保留（积分对账要用）。

宿主惯例：Mapped/mapped_column 声明式；一表一文件并在 ``app/models/__init__`` 注册，使
Base.metadata 感知（init_db create_all 与 Alembic autogenerate 据此建表）。
"""

from datetime import datetime

from sqlalchemy import DateTime, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base


class VideoClip(Base):
    """一条即梦视频片段生成任务。"""

    __tablename__ = "video_clips"
    __table_args__ = (
        # 幂等键按运营隔离：同一运营的同 ref 只对应一条任务；跨运营互不干扰。
        UniqueConstraint("created_by", "client_ref", name="uq_video_clips_creator_ref"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    # 对外公开句柄 vc_<10hex>（形态钉死，见模块 docstring）
    clip_id: Mapped[str] = mapped_column(String(24), unique=True, index=True)
    # 批次句柄 vcb_<10hex>（单镜提交为空）
    batch_id: Mapped[str | None] = mapped_column(String(24), index=True, default=None)
    # 批内原始序（与请求 shots 下标一致，供调用方按序映射 shot-NN）
    batch_index: Mapped[int | None] = mapped_column(Integer, default=None)
    # 调用方幂等键（可空；与 created_by 联合唯一）
    client_ref: Mapped[str | None] = mapped_column(String(64), default=None)

    # 生成入参
    operation: Mapped[str] = mapped_column(String(20))
    prompt: Mapped[str] = mapped_column(Text)
    model: Mapped[str] = mapped_column(String(30))
    duration: Mapped[int] = mapped_column(Integer)
    # image2video 画幅由输入图推断，该形态下恒为空
    ratio: Mapped[str | None] = mapped_column(String(8), default=None)
    # 参考图来源原文（图床直链 或 本服务 /uploads 路径），仅供追溯
    image_source: Mapped[str | None] = mapped_column(Text, default=None)
    # 参考图物化后的本地文件（clip 工作目录内的独立副本，脱离图床 7 天清理）
    image_path: Mapped[str | None] = mapped_column(Text, default=None)

    # 生命周期：queued|submitting|submitted|querying|done|error
    status: Mapped[str] = mapped_column(String(16), default="queued", index=True)
    # 即梦任务号（拿到即不可覆盖，绝不自动重提）
    submit_id: Mapped[str | None] = mapped_column(String(32), index=True, default=None)
    # 该任务实际消耗积分（success 才结算；TTL 清产物后仍保留供对账）
    credit_count: Mapped[int | None] = mapped_column(Integer, default=None)
    video_path: Mapped[str | None] = mapped_column(Text, default=None)
    video_url: Mapped[str | None] = mapped_column(Text, default=None)
    # 终态失败原因（含合规授权原文透传）
    error: Mapped[str | None] = mapped_column(Text, default=None)
    # 查询侧瞬时故障（不改 status；区分「在排队」与「查询接口异常」）
    last_poll_error: Mapped[str | None] = mapped_column(Text, default=None)

    # 创建者 operator id，不设 FK 降低耦合（与 video_jobs 同惯例）
    created_by: Mapped[int] = mapped_column(Integer, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime, default=None)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, default=None)
    # 产物 TTL 到期时刻（done 时落；ClipReaper 据此删目录）
    expires_at: Mapped[datetime | None] = mapped_column(DateTime, default=None)
