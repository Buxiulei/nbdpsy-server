"""published_notes 发布笔记永久台账 + publish_jobs.published_at

Revision ID: a9c4e73f1b26
Revises: f4a2c8e1b9d7
Create Date: 2026-07-31 16:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a9c4e73f1b26'
down_revision: Union[str, Sequence[str], None] = 'f4a2c8e1b9d7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema:建 published_notes(永久台账)+ 给 publish_jobs 加 published_at。

    只加表 + 加列:publish_jobs.note_id 早已存在(一直没被写对而已),不动既有列。
    """
    op.create_table(
        'published_notes',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('account_id', sa.Integer(), nullable=False),
        sa.Column('note_id', sa.String(), nullable=False),
        sa.Column('xsec_token', sa.String(), nullable=True),
        sa.Column('xsec_source', sa.String(), nullable=True),
        sa.Column('note_url', sa.String(), nullable=True),
        sa.Column('title', sa.String(), nullable=False),
        sa.Column('note_type', sa.String(), nullable=True),
        sa.Column('published_at', sa.DateTime(), nullable=True),
        sa.Column('source_publish_job_id', sa.Integer(), nullable=True),
        sa.Column('content_archive_id', sa.Integer(), nullable=True),
        sa.Column('first_seen_at', sa.DateTime(), nullable=False),
        sa.Column('last_synced_at', sa.DateTime(), nullable=False),
        sa.Column('likes', sa.Integer(), nullable=False),
        sa.Column('collects', sa.Integer(), nullable=False),
        sa.Column('comments', sa.Integer(), nullable=False),
        sa.Column('shares', sa.Integer(), nullable=False),
        sa.Column('views', sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(['account_id'], ['xhs_accounts.id']),
        sa.ForeignKeyConstraint(['source_publish_job_id'], ['publish_jobs.id']),
        sa.ForeignKeyConstraint(['content_archive_id'], ['content_archive.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('account_id', 'note_id', name='uq_published_notes_account_note'),
    )
    op.add_column(
        'publish_jobs', sa.Column('published_at', sa.DateTime(), nullable=True)
    )


def downgrade() -> None:
    """Downgrade schema:删 publish_jobs.published_at + 删 published_notes 表。"""
    op.drop_column('publish_jobs', 'published_at')
    op.drop_table('published_notes')
