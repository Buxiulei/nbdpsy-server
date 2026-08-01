"""publish_jobs 三组件三列(合集 / 引用笔记 / 关联活动)

Revision ID: d9b3e7c41a86
Revises: b8e2f14c7a09
Create Date: 2026-08-01 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'd9b3e7c41a86'
down_revision: Union[str, Sequence[str], None] = 'b8e2f14c7a09'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema:publish_jobs 加三组件三列(**只加列,不删不改既有列**)。

    发布时可选设置的三组件:collection_id(加入合集)/ quoted_note_id(引用笔记)/
    activity_id(关联活动)。三列全 nullable:存量任务与不设组件的新任务都是 NULL,
    发布链路见到 NULL 就完全跳过组件设置那一步,行为与本次改动前逐字节一致。
    """
    op.add_column('publish_jobs', sa.Column('collection_id', sa.String(), nullable=True))
    op.add_column('publish_jobs', sa.Column('quoted_note_id', sa.String(), nullable=True))
    op.add_column('publish_jobs', sa.Column('activity_id', sa.String(), nullable=True))


def downgrade() -> None:
    """Downgrade schema:删掉这三列。"""
    op.drop_column('publish_jobs', 'activity_id')
    op.drop_column('publish_jobs', 'quoted_note_id')
    op.drop_column('publish_jobs', 'collection_id')
