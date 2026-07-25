"""content_archive 内容资产库表

Revision ID: b1f4c2a09e77
Revises: e3c9a51b7d24
Create Date: 2026-07-25 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b1f4c2a09e77'
down_revision: Union[str, Sequence[str], None] = 'e3c9a51b7d24'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema:建 content_archive(发布内容归档,供跨账号复用,90 天滑动 TTL)。"""
    op.create_table(
        'content_archive',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('title', sa.String(), nullable=False),
        sa.Column('content', sa.String(), nullable=False),
        sa.Column('topics_json', sa.String(), nullable=False),
        sa.Column('media_json', sa.String(), nullable=False),
        sa.Column('kind', sa.String(), nullable=False),
        sa.Column('source_account_id', sa.Integer(), nullable=False),
        sa.Column('source_note_url', sa.String(), nullable=True),
        sa.Column('source_operator_id', sa.Integer(), nullable=True),
        sa.Column('source_publish_job_id', sa.Integer(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('last_used_at', sa.DateTime(), nullable=False),
        sa.Column('use_count', sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(['source_account_id'], ['xhs_accounts.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('source_publish_job_id', name='uq_content_archive_pubjob'),
    )


def downgrade() -> None:
    """Downgrade schema:删 content_archive 表。"""
    op.drop_table('content_archive')
