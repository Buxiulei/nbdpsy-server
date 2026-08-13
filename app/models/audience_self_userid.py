"""自家号 user_id 追加型登记表:进过矩阵的号,退出后仍从受众分析里排除。

## 为什么活名单不够(2026-08-13 号9 泄漏事故)

受众分析的自家号排除最初只查 ``xhs_accounts`` 活名单("现查现用,加号自动跟上")。
它扛得住**加号**,扛不住**删号**:号9(米之木木,user_id ``5c2b6136…``)的账号行被移出
系统后,它历史上 55 条互刷事件瞬间没人认领,以互动第一名的身份顶在潜客漏斗头部——
运营按潜客分找人,找到的第一名是自己家的号。内容运营验收当场抓到(它还改名成了
「淡三花」,任何按昵称的匹配都会随改名失效,**排除键只认 user_id**)。

## 语义:进过就永远排除

一个号在矩阵里期间产生的互动是互刷产物,不是真实受众行为 —— 这个事实不随账号
退出矩阵而改变。所以本表**只进不出**:``self_account_userids`` 每次调用把活名单
合并进来(新号自动登记),从不删除。账号行删了,登记还在,历史事件照样被排除。
"""

from datetime import datetime

from sqlalchemy import DateTime, func
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base


class AudienceSelfUserid(Base):
    """一个曾经(或现在)属于本矩阵的平台 user_id。"""

    __tablename__ = "audience_self_userids"

    user_id: Mapped[str] = mapped_column(primary_key=True)
    # 首次进入登记的时刻(纯审计,不参与任何判断)
    first_seen: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, server_default=func.current_timestamp()
    )
