"""publish_jobs 加 result_json:发布结果回显(实际应用的话题/组件)

Revision ID: a1f6d2e83b90
Revises: c3e8b1d7f204
Create Date: 2026-08-03 14:30:00.000000

为什么要这列(2026-08-03 运营上报):发布参数被静默丢弃(文字版话题全丢)时,调用方
只能等笔记发出去、人工读正文才能察觉 —— 运营为验证这一点白删了一篇笔记。回显
"服务端实际应用了什么"(topics_applied/topics_failed/components)让丢弃当场可见。
只加一列 nullable,存量行 NULL,读侧视 NULL 为"无回显(该功能上线前发布的)"。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'a1f6d2e83b90'
down_revision: Union[str, Sequence[str], None] = 'c3e8b1d7f204'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('publish_jobs', sa.Column('result_json', sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column('publish_jobs', 'result_json')
