"""video_clips 参考视频路径列(video_paths_json)

Revision ID: a7f2c9d10e64
Revises: d4b8e6a1c530
Create Date: 2026-08-06 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a7f2c9d10e64'
down_revision: Union[str, Sequence[str], None] = 'd4b8e6a1c530'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema:video_clips 加 video_paths_json(**只加列,不动既有列**)。

    multimodal2video 的参考视频(CLI 的 --video stringArray)在既有列里没有落点:
    image_paths_json 只装参考图,两类素材的条数闸 / 时长闸 / 魔数白名单 / 提交 flag 全不一样,
    混一列就得靠猜后缀去分。存量行与其余四种 operation 该列恒为 NULL,读侧
    dreamina.ref_video_paths() 对 NULL 回空列表,语义与改前一致。
    """
    # 防御式:生产库若已被 create_all 补过列,加列会重复报错(部署漏跑迁移的历史教训)
    cols = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("video_clips")}
    if "video_paths_json" not in cols:
        op.add_column("video_clips", sa.Column("video_paths_json", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("video_clips", "video_paths_json")
