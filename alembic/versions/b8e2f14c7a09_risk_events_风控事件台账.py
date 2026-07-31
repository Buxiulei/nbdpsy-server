"""risk_events 风控事件台账(撞验证墙必须留痕)

Revision ID: b8e2f14c7a09
Revises: a9c4e73f1b26
Create Date: 2026-07-31 15:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b8e2f14c7a09'
down_revision: Union[str, Sequence[str], None] = 'c1d7f3a90b52'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema:建 risk_events 表(账号撞风控验证墙的取证历史)。"""
    op.create_table(
        'risk_events',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('account_id', sa.Integer(), nullable=False),
        sa.Column('wall_type', sa.String(), nullable=False),
        sa.Column('source', sa.String(), nullable=False),
        sa.Column('target_url', sa.String(), nullable=True),
        sa.Column('landed_url', sa.String(), nullable=True),
        sa.Column('page_text', sa.Text(), nullable=True),
        sa.Column('detected_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['account_id'], ['xhs_accounts.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(
        'ix_risk_events_account_detected', 'risk_events',
        ['account_id', 'detected_at'],
    )


def downgrade() -> None:
    """Downgrade schema:删 risk_events 表。"""
    op.drop_index('ix_risk_events_account_detected', table_name='risk_events')
    op.drop_table('risk_events')
