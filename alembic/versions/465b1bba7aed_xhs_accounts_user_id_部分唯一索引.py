"""xhs_accounts.user_id 部分唯一索引

Revision ID: 465b1bba7aed
Revises: 7281a40eaaf8
Create Date: 2026-07-07 19:14:01.763411

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '465b1bba7aed'
down_revision: Union[str, Sequence[str], None] = '7281a40eaaf8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # user_id 部分唯一索引:非 NULL 时全库唯一;NULL(仅 name 建的号)不受约束。
    op.create_index(
        'uq_xhs_account_user_id',
        'xhs_accounts',
        ['user_id'],
        unique=True,
        sqlite_where=sa.text('user_id IS NOT NULL'),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index('uq_xhs_account_user_id', table_name='xhs_accounts')
