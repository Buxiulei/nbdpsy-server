"""video_jobs 表

Revision ID: 61197d6e298f
Revises: 5fdec94dd809
Create Date: 2026-07-22 11:15:00.129273

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '61197d6e298f'
down_revision: Union[str, Sequence[str], None] = '5fdec94dd809'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema：建 video_jobs 表（transport|remake|revision 三合一）。"""
    op.create_table(
        'video_jobs',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('url', sa.String(length=500), nullable=False),
        sa.Column('video_id', sa.String(length=64), nullable=True),
        sa.Column('title', sa.String(length=500), nullable=True),
        sa.Column('duration_seconds', sa.Integer(), nullable=True),
        sa.Column('mode', sa.String(length=20), server_default='transport', nullable=False),
        sa.Column('parent_job_id', sa.Integer(), nullable=True),
        sa.Column('status', sa.String(length=20), nullable=False),
        sa.Column('stage', sa.String(length=20), nullable=False),
        sa.Column('stages_json', sa.JSON(), nullable=False),
        sa.Column('options_json', sa.JSON(), nullable=False),
        sa.Column('products_json', sa.JSON(), nullable=False),
        sa.Column('term_sheet_json', sa.JSON(), nullable=False),
        sa.Column('error', sa.Text(), nullable=True),
        sa.Column('retry_count', sa.Integer(), nullable=False),
        sa.Column('heartbeat_at', sa.DateTime(), nullable=True),
        sa.Column('created_by', sa.Integer(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_video_jobs_video_id', 'video_jobs', ['video_id'])
    op.create_index('ix_video_jobs_parent_job_id', 'video_jobs', ['parent_job_id'])
    op.create_index('ix_video_jobs_status', 'video_jobs', ['status'])
    op.create_index('ix_video_jobs_created_by', 'video_jobs', ['created_by'])


def downgrade() -> None:
    """Downgrade schema：删索引 + 删表。"""
    op.drop_index('ix_video_jobs_created_by', table_name='video_jobs')
    op.drop_index('ix_video_jobs_status', table_name='video_jobs')
    op.drop_index('ix_video_jobs_parent_job_id', table_name='video_jobs')
    op.drop_index('ix_video_jobs_video_id', table_name='video_jobs')
    op.drop_table('video_jobs')
