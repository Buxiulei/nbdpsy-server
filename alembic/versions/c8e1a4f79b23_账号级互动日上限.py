"""账号级互动日上限:恢复期/新号爬坡用

Revision ID: c8e1a4f79b23
Revises: b3d9f2c4a1e7
Create Date: 2026-08-13 22:10:00.000000

全局 NOTE_INTERACTION_DAILY_LIMIT 改不了单个号。加 per-account 覆盖值,用于
**恢复期与新号爬坡**:李牧阳_北大心理 2026-08-13 连败 96 次(软风控形态——登录态在、
发布者主页渲染不出卡片)被隔离一周,重登回池后若立刻每天顶满 20 篇,是典型**行为突变**
——绝对频次没超红线,但模式突变本身就是风控特征。新号入矩阵头几天同理。

可空:空 = 用全局值,既有账号行为一字不变(故不需要 server_default)。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'c8e1a4f79b23'
down_revision: Union[str, Sequence[str], None] = 'b3d9f2c4a1e7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """加一列可空整数;不 seed 任何值(谁要爬坡由运营显式设)。"""
    op.add_column(
        'xhs_accounts',
        sa.Column('interaction_daily_limit', sa.Integer(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column('xhs_accounts', 'interaction_daily_limit')
