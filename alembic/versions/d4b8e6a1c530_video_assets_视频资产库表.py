"""video_assets 视频资产库表(长期镜头资产,无 TTL)

Revision ID: d4b8e6a1c530
Revises: c8a5e21fb730
Create Date: 2026-08-06 20:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'd4b8e6a1c530'
down_revision: Union[str, Sequence[str], None] = 'c8a5e21fb730'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema:建 video_assets(clip 产物转存出的长期资产,按 caller 归属,不设 TTL)。

    防御式:生产库若已被 init_db 的 create_all 先建过表,直接 create_table 会重复报错
    (部署漏跑 alembic 的历史教训)。故先 inspect 已有表名再决定建不建。
    """
    inspector = sa.inspect(op.get_bind())
    if 'video_assets' in inspector.get_table_names():
        return
    op.create_table(
        'video_assets',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('asset_id', sa.String(length=24), nullable=False),
        sa.Column('name', sa.String(length=120), nullable=False),
        sa.Column('tags_json', sa.String(), nullable=False),
        sa.Column('source_clip_id', sa.String(length=24), nullable=False),
        sa.Column('source_operation', sa.String(length=20), nullable=True),
        sa.Column('source_model', sa.String(length=30), nullable=True),
        sa.Column('source_prompt', sa.Text(), nullable=True),
        sa.Column('duration', sa.Integer(), nullable=True),
        sa.Column('size_bytes', sa.Integer(), nullable=True),
        sa.Column('created_by', sa.Integer(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        # 幂等键:同一运营的同一条 clip 只存一份资产(重复转存回原资产,不再拷副本)
        sa.UniqueConstraint('created_by', 'source_clip_id',
                            name='uq_video_assets_creator_clip'),
    )
    op.create_index('ix_video_assets_asset_id', 'video_assets', ['asset_id'], unique=True)
    op.create_index('ix_video_assets_source_clip_id', 'video_assets', ['source_clip_id'])
    op.create_index('ix_video_assets_created_by', 'video_assets', ['created_by'])


def downgrade() -> None:
    """Downgrade schema:删 video_assets 表(索引随表一并消失)。"""
    op.drop_table('video_assets')
