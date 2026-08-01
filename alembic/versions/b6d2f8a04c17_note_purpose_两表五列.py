"""published_notes 四列 + publish_jobs 一列:笔记核心目的与正文回填

Revision ID: b6d2f8a04c17
Revises: e7a4b9c02d13
Create Date: 2026-08-01 21:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b6d2f8a04c17'
down_revision: Union[str, Sequence[str], None] = 'e7a4b9c02d13'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema:两表加五列(**只加列,不删不改既有列**)。

    ``published_notes``:

    - ``note_purpose`` 核心目的(给调用笔记的 agent 读的意图标注,存字符串不做枚举约束);
    - ``purpose_source`` 这个值怎么来的:``declared``(发布时调用方声明,可直接信)/
      ``inferred``(从正文推断,要留余地)/ NULL(未知)。**必须有它** —— agent 得知道
      目的是人声明的还是机器猜的,两者可信度不同;
    - ``content_text`` 笔记正文(手工发布的 orphan 行本地一个字都没有,靠只读进编辑页抓回来);
    - ``content_fetched_at`` 正文抓取时刻(非空 = 已经开过一次编辑页,不必再开)。

    ``publish_jobs``:``note_purpose`` 发布入参,T0 发布当场带进台账并置
    ``purpose_source='declared'``。

    五列全 nullable:存量行与不填的新行都是 NULL。NULL 语义是**未知**,不代表"没有目的"。
    """
    op.add_column('published_notes', sa.Column('note_purpose', sa.String(), nullable=True))
    op.add_column('published_notes', sa.Column('purpose_source', sa.String(), nullable=True))
    op.add_column('published_notes', sa.Column('content_text', sa.Text(), nullable=True))
    op.add_column(
        'published_notes', sa.Column('content_fetched_at', sa.DateTime(), nullable=True)
    )
    op.add_column('publish_jobs', sa.Column('note_purpose', sa.String(), nullable=True))


def downgrade() -> None:
    """Downgrade schema:删掉这五列。"""
    op.drop_column('publish_jobs', 'note_purpose')
    op.drop_column('published_notes', 'content_fetched_at')
    op.drop_column('published_notes', 'content_text')
    op.drop_column('published_notes', 'purpose_source')
    op.drop_column('published_notes', 'note_purpose')
