"""publish_jobs 加 video_path 列(视频笔记发布)

Revision ID: c1d47f92ab60
Revises: b3e9d17a5c02
Create Date: 2026-08-07 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c1d47f92ab60'
down_revision: Union[str, Sequence[str], None] = 'b3e9d17a5c02'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema:publish_jobs 加 video_path 列(**只加列,不动数据、不改既有列**)。

    视频笔记的服务器侧视频文件路径;nullable —— 存量任务与图文新任务都是 NULL,
    发布链路见到 NULL 就走原来的图文分支,行为与本次改动前逐字节一致。
    """
    op.add_column('publish_jobs', sa.Column('video_path', sa.Text(), nullable=True))


def downgrade() -> None:
    """Downgrade schema:删掉 video_path 列。"""
    op.drop_column('publish_jobs', 'video_path')
