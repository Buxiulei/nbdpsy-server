"""psych_glossary 术语表

Revision ID: cb468a963422
Revises: 61197d6e298f
Create Date: 2026-07-22 13:26:38.667557

心理学中英术语表（transport/remake translate 阶段消费）。模型已在 M3a 注册 Base，
但 M3a 走 create_all 建表（未加迁移）；I-1 审查硬项要求纯 alembic 部署也能建此表，
故补本条 autogenerate 迁移。psych_glossary 为独立新表（非缺列），upgrade 全建、
downgrade 全删，与 video_jobs 迁移不交叠。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'cb468a963422'
down_revision: Union[str, Sequence[str], None] = '61197d6e298f'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema：建 psych_glossary 表 + en_term 唯一索引。"""
    op.create_table(
        'psych_glossary',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('en_term', sa.String(length=255), nullable=False),
        sa.Column('zh_term', sa.String(length=255), nullable=False),
        sa.Column('aliases_json', sa.JSON(), nullable=False),
        sa.Column('source', sa.String(length=10), nullable=False),
        sa.Column('confidence', sa.Float(), nullable=False),
        sa.Column('approved', sa.Boolean(), nullable=False),
        sa.Column('evidence', sa.String(length=500), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    with op.batch_alter_table('psych_glossary', schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f('ix_psych_glossary_en_term'), ['en_term'], unique=True
        )


def downgrade() -> None:
    """Downgrade schema：删索引 + 删表。"""
    with op.batch_alter_table('psych_glossary', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_psych_glossary_en_term'))

    op.drop_table('psych_glossary')
