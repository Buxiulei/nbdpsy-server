"""publish_jobs 加 audio_path 列(播客音频发布)

Revision ID: d2f4a8c19e63
Revises: c1d47f92ab60
Create Date: 2026-08-07 16:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'd2f4a8c19e63'
down_revision: Union[str, Sequence[str], None] = 'c1d47f92ab60'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema:publish_jobs 加 audio_path 列(**只加列,不动数据、不改既有列**)。

    播客笔记的服务器侧音频文件路径;nullable —— 存量任务、图文与视频新任务都是 NULL,
    发布链路见到 NULL 就走原来的分支,行为与本次改动前逐字节一致。

    为什么**新开一个 revision** 而不是像 cover_path 那样直接改 c1d47f92ab60:
    那条迁移在本改动开工时已被视为可能已部署,改一条已跑过的迁移不会在目标库上重放,
    只会让线上库与迁移文件永久对不上(本仓「幽灵迁移」事故同族)。

    ``cover_path``(c1d47f92ab60 里)播客侧**复用**存音频封面,``collection_id``
    复用存播客合集名称 —— 两者都不新增列,理由见 app/models/publish_job.py 的列注释。
    """
    op.add_column('publish_jobs', sa.Column('audio_path', sa.Text(), nullable=True))


def downgrade() -> None:
    """Downgrade schema:删掉 audio_path 列。"""
    op.drop_column('publish_jobs', 'audio_path')
