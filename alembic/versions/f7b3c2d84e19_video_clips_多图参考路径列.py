"""video_clips 多图参考路径列(image_paths_json)

Revision ID: f7b3c2d84e19
Revises: a3f1d92e77b4
Create Date: 2026-08-05 22:10:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'f7b3c2d84e19'
down_revision: Union[str, Sequence[str], None] = 'a3f1d92e77b4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema:video_clips 加 image_paths_json(**只加列,不动既有列**)。

    单列 image_path 装不下 multimodal2video 的多张参考图(2.5 收 30 张)与 frames2video 的
    首尾两帧,故加一列存 JSON 数组。**存量行不回填**:它们只有 image_path,读侧
    ``dreamina.ref_paths()`` 对 NULL 回落单列,语义与改前逐字节一致。
    """
    # 防御式:生产库若已被 create_all 补过列,加列会重复报错(部署漏跑迁移的历史教训)
    cols = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("video_clips")}
    if "image_paths_json" not in cols:
        op.add_column("video_clips", sa.Column("image_paths_json", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("video_clips", "image_paths_json")
