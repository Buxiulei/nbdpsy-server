"""published_notes 加媒体清单两列(media_json / media_fetched_at)

只存归一化后的永久媒体 URL,不落文件:平台给的带签名 URL 18 天就过期(实证 403),
剥成 sns-img-qc/{路径段}/{file_id} 永久有效且是原图,故按需下载即可。

Revision ID: a3f1d92e77b4
Revises: e2c47f9b3a15
"""

import sqlalchemy as sa
from alembic import op

revision = "a3f1d92e77b4"
down_revision = "e2c47f9b3a15"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 防御式:生产库若已被 create_all 补过列,加列会重复报错(部署漏跑迁移的历史教训)
    cols = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("published_notes")}
    if "media_json" not in cols:
        op.add_column("published_notes", sa.Column("media_json", sa.Text(), nullable=True))
    if "media_fetched_at" not in cols:
        op.add_column(
            "published_notes", sa.Column("media_fetched_at", sa.DateTime(), nullable=True)
        )


def downgrade() -> None:
    op.drop_column("published_notes", "media_fetched_at")
    op.drop_column("published_notes", "media_json")
