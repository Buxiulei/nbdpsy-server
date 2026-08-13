"""受众行为库:audience_events 事件流表 + audience_sync_state 增量游标表

Revision ID: a7c31e9b48d2
Revises: c4e7a91d3b58
Create Date: 2026-08-12 12:00:00.000000

设计 docs/design/2026-08-12-audience-behavior-library-design.md 第 2 / 6 节。

**不 seed 任何一行**:历史随时可回采(通知流两年触底,号1 实采 922 条),首次采集由
``AudienceSyncScheduler`` 自然派一张 full 单补齐。迁移不预置数据,也不预置游标 ——
预置游标等于宣称"这段已经采过了",而库里一条都没有。

⚠️ **合规边界(改这两张表前必读)**:这是受众**公开行为**库,只存平台在通知流里已经公开
发给我们的字段(userid / 昵称 / 头像 / 关系 / 被互动的公开笔记)。**绝不新增
``actor_userid`` → 来访者真实身份(姓名 / 手机 / 预约记录 / 咨询关系)的任何关联列或外键**
—— 那会把"受众行为分析"变成"个人档案追踪",是设计里明确划死的红线。

两处 DDL 纪律:

- ``target_note_id`` **不做外键**(同 note_interactions 的理由):被互动的笔记不一定在
  ``published_notes`` 里 —— 已删的、台账没同步的、甚至是别人的笔记(赞评论那类事件记的是
  评论所在的那篇)。台账缺行不该挡住记账;
- 两张表的时间列都带 ``server_default``:本仓有若干路径用裸 sqlite3 显式列清单 INSERT,
  NOT NULL 无 DDL 默认值的列会让那些语句当场炸,且**只在跑到那条路径时**才炸
  (xhs_accounts.managed 那次的教训)。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a7c31e9b48d2'
down_revision: Union[str, Sequence[str], None] = 'c4e7a91d3b58'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema:建两张新表(无 seed)。"""
    op.create_table(
        'audience_events',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('account_id', sa.Integer(), nullable=False),
        sa.Column('platform_event_id', sa.String(), nullable=False),
        sa.Column('actor_userid', sa.String(), nullable=False),
        sa.Column('actor_nickname', sa.String(), nullable=False),
        sa.Column('actor_image', sa.String(), nullable=True),
        sa.Column('event_type', sa.String(), nullable=False),
        # 不做外键,理由见模块 docstring
        sa.Column('target_note_id', sa.String(), nullable=True),
        sa.Column('target_note_title', sa.String(), nullable=True),
        sa.Column('fstatus', sa.String(), nullable=True),
        sa.Column('event_time', sa.Integer(), nullable=False),
        sa.Column('raw_json', sa.Text(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False,
                  server_default=sa.func.current_timestamp()),
        sa.ForeignKeyConstraint(['account_id'], ['xhs_accounts.id']),
        sa.PrimaryKeyConstraint('id'),
        # 去重键带 account_id:平台没承诺过事件 id 跨账号全局唯一,少了它两个号收到同 id
        # 的事件会互相顶掉一条,而那是两条真实互动
        sa.UniqueConstraint('account_id', 'platform_event_id',
                            name='uq_audience_events_account_event'),
    )
    # 纵向轨迹按 userid 聚合(单人时间线 / 跨号分布 / 打分全走它)
    op.create_index('ix_audience_events_actor', 'audience_events', ['actor_userid'])
    # 增量采集游标与时间范围查询
    op.create_index('ix_audience_events_account_time', 'audience_events',
                    ['account_id', 'event_time'])

    op.create_table(
        'audience_sync_state',
        sa.Column('account_id', sa.Integer(), nullable=False),
        sa.Column('channel', sa.String(), nullable=False),
        # NULL = 这条 channel 还没采过,下一轮走全量
        sa.Column('last_event_time', sa.Integer(), nullable=True),
        sa.Column('last_full_sync_at', sa.DateTime(), nullable=True),
        sa.Column('updated_at', sa.DateTime(), nullable=False,
                  server_default=sa.func.current_timestamp()),
        sa.ForeignKeyConstraint(['account_id'], ['xhs_accounts.id']),
        # 复合主键:一个号的 likes 与 connections 各自独立推进(两条流深度差一个数量级,
        # 共用游标会让浅的那条被深的拖着反复重翻)
        sa.PrimaryKeyConstraint('account_id', 'channel'),
    )


def downgrade() -> None:
    """Downgrade schema:删两张表(它们是纯新增,回滚不影响任何既有链路)。"""
    op.drop_table('audience_sync_state')
    op.drop_index('ix_audience_events_account_time', table_name='audience_events')
    op.drop_index('ix_audience_events_actor', table_name='audience_events')
    op.drop_table('audience_events')
