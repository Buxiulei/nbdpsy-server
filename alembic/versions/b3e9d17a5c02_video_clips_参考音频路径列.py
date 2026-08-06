"""video_clips 参考音频路径列(audio_paths_json)

Revision ID: b3e9d17a5c02
Revises: a7f2c9d10e64
Create Date: 2026-08-06 17:30:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b3e9d17a5c02'
down_revision: Union[str, Sequence[str], None] = 'a7f2c9d10e64'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema:video_clips 加 audio_paths_json(**只加列,不动既有列**)。

    multimodal2video 的参考音频(CLI 的 --audio stringArray)与参考图 / 参考视频三分输入面,
    三类的条数闸、魔数白名单、提交 flag 全不一样,故各占一列而不是混进一列靠猜后缀去分。
    存量行与其余四种 operation 该列恒为 NULL,读侧 dreamina.ref_audio_paths() 对 NULL 回
    空列表,语义与改前一致。
    """
    # 防御式:生产库若已被 create_all 补过列,加列会重复报错(部署漏跑迁移的历史教训)
    cols = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("video_clips")}
    if "audio_paths_json" not in cols:
        op.add_column("video_clips", sa.Column("audio_paths_json", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("video_clips", "audio_paths_json")
