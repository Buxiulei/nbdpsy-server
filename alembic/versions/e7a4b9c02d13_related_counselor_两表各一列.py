"""publish_jobs / published_notes 各加 related_counselor 一列

Revision ID: e7a4b9c02d13
Revises: d9b3e7c41a86
Create Date: 2026-08-01 18:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'e7a4b9c02d13'
down_revision: Union[str, Sequence[str], None] = 'd9b3e7c41a86'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema:两表各加 related_counselor(**只加列,不删不改既有列**)。

    这篇笔记推介哪位咨询师(姓名)。publish_jobs 上是入参,建 job 时据它推导该引用哪篇
    笔记(app/services/counselor_quote.py);published_notes 上是发布当场(T0)从
    publish_jobs 带过去的留痕,与 generated_at / operator_id 同批写入。

    两列都 nullable:存量行与不填的新行都是 NULL,推导链路见 NULL 就走标题解析兜底,
    行为与本次改动前一致。NULL 语义是**未知**,不代表"这篇没推介谁"。
    """
    op.add_column('publish_jobs', sa.Column('related_counselor', sa.String(), nullable=True))
    op.add_column('published_notes', sa.Column('related_counselor', sa.String(), nullable=True))


def downgrade() -> None:
    """Downgrade schema:删掉这两列。"""
    op.drop_column('published_notes', 'related_counselor')
    op.drop_column('publish_jobs', 'related_counselor')
