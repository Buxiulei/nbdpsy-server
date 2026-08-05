"""video_clips 表（即梦视频生成服务化）

Revision ID: e2c47f9b3a15
Revises: a1f6d2e83b90
Create Date: 2026-08-05 14:20:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'e2c47f9b3a15'
down_revision: Union[str, Sequence[str], None] = 'a1f6d2e83b90'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema：建 video_clips 表（即梦片段任务台账）。"""
    op.create_table(
        'video_clips',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('clip_id', sa.String(length=24), nullable=False),
        sa.Column('batch_id', sa.String(length=24), nullable=True),
        sa.Column('batch_index', sa.Integer(), nullable=True),
        sa.Column('client_ref', sa.String(length=64), nullable=True),
        sa.Column('operation', sa.String(length=20), nullable=False),
        sa.Column('prompt', sa.Text(), nullable=False),
        sa.Column('model', sa.String(length=30), nullable=False),
        sa.Column('duration', sa.Integer(), nullable=False),
        sa.Column('ratio', sa.String(length=8), nullable=True),
        sa.Column('image_source', sa.Text(), nullable=True),
        sa.Column('image_path', sa.Text(), nullable=True),
        sa.Column('status', sa.String(length=16), nullable=False),
        sa.Column('submit_id', sa.String(length=32), nullable=True),
        sa.Column('credit_count', sa.Integer(), nullable=True),
        sa.Column('video_path', sa.Text(), nullable=True),
        sa.Column('video_url', sa.Text(), nullable=True),
        sa.Column('error', sa.Text(), nullable=True),
        sa.Column('last_poll_error', sa.Text(), nullable=True),
        sa.Column('created_by', sa.Integer(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('submitted_at', sa.DateTime(), nullable=True),
        sa.Column('finished_at', sa.DateTime(), nullable=True),
        sa.Column('expires_at', sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint('id'),
        # 幂等键按运营隔离（防跨运营 ref 抢注 / 他人任务状态泄漏）
        sa.UniqueConstraint('created_by', 'client_ref', name='uq_video_clips_creator_ref'),
    )
    op.create_index('ix_video_clips_clip_id', 'video_clips', ['clip_id'], unique=True)
    op.create_index('ix_video_clips_batch_id', 'video_clips', ['batch_id'])
    op.create_index('ix_video_clips_status', 'video_clips', ['status'])
    op.create_index('ix_video_clips_submit_id', 'video_clips', ['submit_id'])
    op.create_index('ix_video_clips_created_by', 'video_clips', ['created_by'])


def downgrade() -> None:
    """Downgrade schema：删索引 + 删表。"""
    op.drop_index('ix_video_clips_created_by', table_name='video_clips')
    op.drop_index('ix_video_clips_submit_id', table_name='video_clips')
    op.drop_index('ix_video_clips_status', table_name='video_clips')
    op.drop_index('ix_video_clips_batch_id', table_name='video_clips')
    op.drop_index('ix_video_clips_clip_id', table_name='video_clips')
    op.drop_table('video_clips')
