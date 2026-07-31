"""风控事件台账模型:账号撞验证墙这件事必须留痕(2026-07-31 盲区事故)。

事故复盘:NBDpsy-聊创伤 被小红书挂上扫码验证墙,系统仍认为号是好的继续派活,任务全败;
而 ``browser_jobs`` 全文检索「验证 / 风控 / captcha / 滑块 / security」**零命中** ——
库里查不到任何证据,只能靠人拿浏览器实地撞出来,还据此做了错误决策。

为什么单开一张表,而不是塞进 ``browser_jobs.result`` 或往 ``xhs_accounts`` 上加列:

- 墙是**账号维度的历史事实**(哪个号、什么时候、哪种墙、当时在访问什么),不是某个 job
  的执行细节。落在这里可以直接按账号 + 时间排,不必先猜是哪条链路的哪个 job;
- 事件要跨 kind 复用(cookie 检测、后续互动/导出链路都可能撞墙),塞进各 kind 各自的
  ``result`` 形状后就再也无法统一检索——这正是本次排查落空的原因;
- 往 ``xhs_accounts`` 加列只能存"最后一次",而两种墙的演变过程(扫码验证 → 请求太频繁)
  恰恰是判断"是不是被我们自己反复起会话打限流"的关键,必须留多行。

账号的**当前**状态由 ``xhs_accounts.cookie_status='restricted'`` 表达,本表只记历史。
事件量极低(仅撞墙时写),不设 TTL 清理。
"""

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base


class RiskEvent(Base):
    """一次撞风控墙的取证记录。"""

    __tablename__ = "risk_events"

    # 排查入口就是"这个号最近撞过什么墙",按 (account_id, detected_at) 建索引
    __table_args__ = (
        Index("ix_risk_events_account_detected", "account_id", "detected_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("xhs_accounts.id"))
    # 墙的种类:scan_qr(扫码验证身份)/ rate_limit(请求太频繁)/ unknown(文案不认识)
    wall_type: Mapped[str] = mapped_column()
    # 哪条链路撞的:cookie_check(手动检测)/ cookie_patrol(后台巡检)/ 后续可扩
    source: Mapped[str] = mapped_column()
    # 当时想访问什么(被重定向前的目标 URL)
    target_url: Mapped[str | None] = mapped_column(nullable=True)
    # 实际落到的墙 URL(含 verifyType / verifyBiz 等参数,是判型的原始证据)
    landed_url: Mapped[str | None] = mapped_column(nullable=True)
    # 墙页面正文前若干字符,可能较长用 Text;区分「扫码验证身份」与「请求太频繁」
    page_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    detected_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
