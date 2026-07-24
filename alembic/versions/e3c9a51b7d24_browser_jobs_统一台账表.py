"""browser_jobs 统一台账表(四类内存台账收敛落库,重启不丢任务/终态)

Revision ID: e3c9a51b7d24
Revises: a7d3e1f52c90
Create Date: 2026-07-24 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'e3c9a51b7d24'
down_revision: Union[str, Sequence[str], None] = 'a7d3e1f52c90'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema:建 browser_jobs 统一台账表(对外轮询 id 即主键)。"""
    op.create_table(
        'browser_jobs',
        sa.Column('id', sa.String(), nullable=False),
        sa.Column('kind', sa.String(), nullable=False),
        sa.Column('account_id', sa.Integer(), nullable=True),
        sa.Column('operator_id', sa.Integer(), nullable=False),
        sa.Column('payload', sa.Text(), nullable=False),
        sa.Column('status', sa.String(), nullable=False),
        sa.Column('result', sa.Text(), nullable=True),
        sa.Column('claimed_by', sa.String(), nullable=True),
        sa.Column('heartbeat_at', sa.DateTime(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['account_id'], ['xhs_accounts.id']),
        sa.PrimaryKeyConstraint('id'),
    )


def downgrade() -> None:
    """Downgrade schema:删 browser_jobs 表。"""
    op.drop_table('browser_jobs')
