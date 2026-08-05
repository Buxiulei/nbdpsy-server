"""video_clips 多帧转场列(transitions_json)

Revision ID: c8a5e21fb730
Revises: f7b3c2d84e19
Create Date: 2026-08-05 23:55:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c8a5e21fb730'
down_revision: Union[str, Sequence[str], None] = 'f7b3c2d84e19'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema:video_clips 加 transitions_json(**只加列,不动既有列**)。

    multiframe2video 的逐段转场(N 张图 → N-1 段,每段一个提示词与可选时长)在既有列里没有
    落点。存量行与其余四种 operation 该列恒为 NULL,读侧对 NULL 走简写分支,语义与改前一致。
    """
    # 防御式:生产库若已被 create_all 补过列,加列会重复报错(部署漏跑迁移的历史教训)
    cols = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("video_clips")}
    if "transitions_json" not in cols:
        op.add_column("video_clips", sa.Column("transitions_json", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("video_clips", "transitions_json")
