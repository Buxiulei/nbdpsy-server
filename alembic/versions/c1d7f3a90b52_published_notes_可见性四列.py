"""published_notes 可见性四列(平台原值 + 切换留痕)

Revision ID: c1d7f3a90b52
Revises: a9c4e73f1b26
Create Date: 2026-07-31 21:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c1d7f3a90b52'
down_revision: Union[str, Sequence[str], None] = 'a9c4e73f1b26'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema:published_notes 加可见性四列(**只加列,不删不改既有列**)。

    permission_code / permission_msg 是平台原值(T2 同步覆盖);visibility_changed_at /
    visibility_changed_by 是我们主动切换成功的留痕。四列全 nullable:存量行没有这些信息,
    NULL = **未知**(不等于公开),等同步跑过才有值。
    """
    op.add_column(
        'published_notes', sa.Column('permission_code', sa.Integer(), nullable=True)
    )
    op.add_column(
        'published_notes', sa.Column('permission_msg', sa.String(), nullable=True)
    )
    op.add_column(
        'published_notes', sa.Column('visibility_changed_at', sa.DateTime(), nullable=True)
    )
    op.add_column(
        'published_notes', sa.Column('visibility_changed_by', sa.Integer(), nullable=True)
    )


def downgrade() -> None:
    """Downgrade schema:删掉这四列。"""
    op.drop_column('published_notes', 'visibility_changed_by')
    op.drop_column('published_notes', 'visibility_changed_at')
    op.drop_column('published_notes', 'permission_msg')
    op.drop_column('published_notes', 'permission_code')
