"""互动台账模型:谁给哪篇笔记点过赞 / 收藏过,一次一行。

设计 docs/design/2026-08-02-interaction-backfill-design.md 第 3.1 节。

**这张表不是正确性依据,是效率优化**。平台状态才是唯一真相:``_icon_action`` 每次都读
``use[xlink:href]`` 判断 ``#like``/``#liked``,已是目标态就 skipped 不重复点(重复点 =
取消赞)。本表的用途只有一个 —— **让选篇阶段跳过已处理项,不为它白开一次笔记页**。
"开页"是整条链路里最贵、也最招风控的动作(实测同号一小时 5 次会话就从「扫码验证」
被打成「请求太频繁」),能靠一行台账省掉的会话,绝不去平台上现问一遍。

行只在**真的开过页并动过手**之后写:选篇阶段跳过的篇目不写行,浏览器没起来 / 撞墙中止
那一轮里没轮到的篇目也不写行。故"表里有行" ⇔ "为这篇花过一次页面访问",当日配额据此计数。

``status`` 三态:``done``(点成了)/ ``skipped``(平台上本来就是目标态)/ ``error``
(这一下没成,原因记在 ``detail``)。error 不是终身判决:选篇侧对 error 行给冷却期,
过期可再试一次 —— 一次渲染抖动不该让一篇笔记永久失去补量机会。
"""

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base


class NoteInteraction(Base):
    """一个账号对一篇笔记的一个互动动作(点赞 / 收藏)的台账行。"""

    __tablename__ = "note_interactions"
    __table_args__ = (
        # 同一个号对同一篇的同一个动作只有一行:重复执行是更新这行,不是叠加历史。
        # 叠加历史会让"处理过没有"变成一次聚合查询,而这张表存在的理由正是让那个判断
        # 快到可以在选篇时对全表跑一遍。
        UniqueConstraint(
            "actor_account_id", "note_id", "action",
            name="uq_note_interactions_actor_note_action",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    # 去互动的那个号(不是笔记作者)
    actor_account_id: Mapped[int] = mapped_column(ForeignKey("xhs_accounts.id"))
    # 平台笔记 id。**不做外键**:被互动的笔记不一定在 published_notes 里(矩阵外的、
    # 台账还没同步到的),台账缺行不该挡住记账。
    note_id: Mapped[str] = mapped_column()
    # like / collect(评论不进这张表:评论只在本系统发布时触发,不属于补量范畴)
    action: Mapped[str] = mapped_column()
    # done / skipped(平台上已是目标态)/ error
    status: Mapped[str] = mapped_column()
    # 失败原因或跳过原因(如「已点赞」/「like_button_not_found」)
    detail: Mapped[str | None] = mapped_column(nullable=True)
    # 这一下发生的时刻(重试会覆盖);当日配额按它算
    done_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
