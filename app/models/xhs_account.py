"""小红书账号模型:账号资料 + 登录态 / cookie 巡检态 + 加密 cookie。"""

from datetime import datetime

from sqlalchemy import DateTime, Index, Text, text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base


class XhsAccount(Base):
    """一个受托管的小红书账号。

    login_cookies 存 Fernet 加密串(见 app.core.security),不落明文;
    status / cookie_status 由巡检任务更新,初始均为 'unknown'。
    """

    __tablename__ = "xhs_accounts"

    # user_id 部分唯一索引:非 NULL 时全库唯一(同一小红书号只允许一行);
    # user_id 为 NULL(仅 name 建的号)不受约束,可多行并存。
    __table_args__ = (
        Index(
            "uq_xhs_account_user_id",
            "user_id",
            unique=True,
            sqlite_where=text("user_id IS NOT NULL"),
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    # 内部展示名(运营者可读),必填
    name: Mapped[str] = mapped_column()
    # 小红书昵称 / 平台侧标识,登录后回填
    nickname: Mapped[str | None] = mapped_column(default=None)
    user_id: Mapped[str | None] = mapped_column(default=None)
    red_id: Mapped[str | None] = mapped_column(default=None)
    avatar: Mapped[str | None] = mapped_column(default=None)
    # 账号在线/登录态:'unknown' | 具体状态由巡检写入
    status: Mapped[str] = mapped_column(default="unknown")
    # cookie 有效性:'unknown' | 'valid' | 'invalid' 等,由 cookie 巡检写入
    cookie_status: Mapped[str] = mapped_column(default="unknown")
    last_check_at: Mapped[datetime | None] = mapped_column(DateTime, default=None)
    # Fernet 加密后的 cookie 串,可能较长,用 Text
    login_cookies: Mapped[str | None] = mapped_column(Text, default=None)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime, default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    # ── 代管账号计划(设计 docs/design/2026-08-10-managed-accounts-design.md)──
    # 是否加入代管:1=代管账号(内容号),0=水军号。两处生效:①不传 account_id 的广播发布
    # 只发给代管号;②笔记数量上限淘汰只对代管号跑。**其余一切行为不受它影响** ——
    # 互动补量 / 矩阵互动 / cookie 巡检 / 数据采集照旧覆盖全部账号,水军号只是语义标记。
    #
    # ⚠️ 这两列的 **server_default 不可省**(与迁移 f2b8d41c7e09 保持一致):本仓有若干
    # 同步路径用 sqlite3 直连 ``INSERT INTO xhs_accounts (…)`` 显式列清单建号(matrix_interact
    # / note_comment_task 的调度侧、以及它们的测试夹具),那些语句不会知道这两个新列。
    # 只写 Python 侧 default(=ORM 才生效)会让 create_all 建出 NOT NULL 无默认值的列,
    # 所有裸 SQL 插入当场 IntegrityError —— 且**只在跑到那条路径时**才炸。
    managed: Mapped[bool] = mapped_column(default=False, server_default=text("0"))
    # 该号笔记数量上限:超限时由 RetentionScheduler 按加权得分淘汰最低的几篇。
    # 仅对 managed=1 的号生效(非代管号不跑淘汰,这个值只是躺着)。
    note_cap: Mapped[int] = mapped_column(default=100, server_default=text("100"))
    # 该号的互动日上限;为空则用全局 NOTE_INTERACTION_DAILY_LIMIT。
    # 用途是**恢复期/新号爬坡**:一个刚被软风控隔离过的号(李牧阳 2026-08-13 连败 96 次
    # 后隔离一周)回池就顶满 20 篇是典型**行为突变**——绝对频次没超红线,但模式突变本身
    # 就是风控特征;新号入矩阵头几天同理,不该把最脆弱的时期配上最激进的行为。
    # <=0 视为未设(方向反了的兜底比没有兜底危险,与 note_cap 同一条教训)。
    interaction_daily_limit: Mapped[int | None] = mapped_column(nullable=True)
