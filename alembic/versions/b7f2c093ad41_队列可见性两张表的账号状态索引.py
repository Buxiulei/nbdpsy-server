"""队列可见性:browser_jobs / publish_jobs 的 (account_id, status) 复合索引

轮询端点的 queue 段每次都要按 (account_id, status) 取该号的待派队列、在跑行、窗口内
会话行,轮询频率是 3-5 秒一次乘以在飞任务数;没有索引这些全是全表扫,台账越长越慢。

Revision ID: b7f2c093ad41
Revises: d2f4a8c19e63
Create Date: 2026-08-07

"""
from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'b7f2c093ad41'
down_revision: Union[str, Sequence[str], None] = 'd2f4a8c19e63'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema:两张任务表各加一条 (account_id, status) 复合索引。"""
    op.create_index(
        'ix_browser_jobs_account_status', 'browser_jobs', ['account_id', 'status']
    )
    op.create_index(
        'ix_publish_jobs_account_status', 'publish_jobs', ['account_id', 'status']
    )


def downgrade() -> None:
    """Downgrade schema:删掉两条索引。"""
    op.drop_index('ix_publish_jobs_account_status', table_name='publish_jobs')
    op.drop_index('ix_browser_jobs_account_status', table_name='browser_jobs')
