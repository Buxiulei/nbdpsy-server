"""note_interactions 互动台账表:历史笔记互动补量的增量依据

Revision ID: c3e8b1d7f204
Revises: b6d2f8a04c17
Create Date: 2026-08-02 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c3e8b1d7f204'
down_revision: Union[str, Sequence[str], None] = 'b6d2f8a04c17'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema:新建 ``note_interactions``(**只加表,不动任何既有表**)。

    历史笔记互动补量要做增量,就必须知道"这个号给这篇点过没有";没有这张表,每一篇都得
    先开一次笔记页才知道,而开页正是整条链路里最贵、最招风控的动作。

    唯一约束 ``(actor_account_id, note_id, action)``:同号同篇同动作只有一行,重复执行
    是更新而非叠加 —— 选篇阶段要对全表判"处理过没有",叠加历史会把它变成聚合查询。

    ``note_id`` 刻意**不建外键**到 published_notes:被互动的笔记不一定在台账里
    (矩阵外的、还没同步到的),台账缺行不该挡住记账。
    """
    op.create_table(
        'note_interactions',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('actor_account_id', sa.Integer(), nullable=False),
        sa.Column('note_id', sa.String(), nullable=False),
        sa.Column('action', sa.String(), nullable=False),
        sa.Column('status', sa.String(), nullable=False),
        sa.Column('detail', sa.String(), nullable=True),
        sa.Column('done_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['actor_account_id'], ['xhs_accounts.id'], ),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint(
            'actor_account_id', 'note_id', 'action',
            name='uq_note_interactions_actor_note_action',
        ),
    )


def downgrade() -> None:
    """Downgrade schema:删掉这张表。"""
    op.drop_table('note_interactions')
