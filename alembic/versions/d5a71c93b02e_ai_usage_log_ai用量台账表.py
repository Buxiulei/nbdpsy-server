"""ai_usage_log AI 用量台账表(生图 usage 落库,供 NBDpsy 监控页跨库拉取)

Revision ID: d5a71c93b02e
Revises: c7e9a4b21d38
Create Date: 2026-07-28 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'd5a71c93b02e'
down_revision: Union[str, Sequence[str], None] = 'c7e9a4b21d38'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema:建 ai_usage_log 表(自增 id 即跨库拉取游标)。"""
    op.create_table(
        'ai_usage_log',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('kind', sa.String(), nullable=False),
        sa.Column('model', sa.String(), nullable=False),
        sa.Column('request_kind', sa.String(), nullable=False),
        sa.Column('input_tokens', sa.Integer(), nullable=True),
        sa.Column('output_tokens', sa.Integer(), nullable=True),
        sa.Column('image_input_tokens', sa.Integer(), nullable=True),
        sa.Column('text_input_tokens', sa.Integer(), nullable=True),
        sa.Column('cached_input_tokens', sa.Integer(), nullable=True),
        sa.Column('image_count', sa.Integer(), nullable=False),
        sa.Column('image_size', sa.String(), nullable=True),
        sa.Column('image_quality', sa.String(), nullable=True),
        sa.Column('success', sa.Boolean(), nullable=False),
        sa.Column('job_id', sa.String(), nullable=True),
        sa.Column('error', sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint('id'),
        # 跨库游标要求 id 单调不复用:不带 AUTOINCREMENT 时 sqlite 会复用被删最大行的 rowid
        sqlite_autoincrement=True,
    )
    op.create_index(
        op.f('ix_ai_usage_log_created_at'), 'ai_usage_log', ['created_at'], unique=False
    )


def downgrade() -> None:
    """Downgrade schema:删 ai_usage_log 表。"""
    op.drop_index(op.f('ix_ai_usage_log_created_at'), table_name='ai_usage_log')
    op.drop_table('ai_usage_log')
