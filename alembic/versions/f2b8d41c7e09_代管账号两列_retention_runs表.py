"""代管账号计划:xhs_accounts 两列(managed/note_cap)+ published_notes.deleted_at + retention_runs 审计表

Revision ID: f2b8d41c7e09
Revises: b7f2c093ad41
Create Date: 2026-08-10 12:00:00.000000

设计 docs/design/2026-08-10-managed-accounts-design.md 第三节。

**不 seed 任何 managed=1**:全部账号一律留在默认 managed=0,谁进代管由
``PUT /api/accounts/{account_id}/managed`` 显式开启 ——「加入代管」本来就是个显式动作,
不该由迁移替人做主。

⚠️ 这里原本写过一条 seed:「凡 published_notes 里有过记录的账号一律 managed=1」,理由是
"发过内容的就是内容号"。**这条推理是错的**,对抗审查实查生产库后推翻:``published_notes``
是台账**全量同步**捞回来的,9-12 那批纯互动号(水军号)名下同样有行 —— 那些是同步时捞到的
**他人个人笔记** orphan 行,不是我们发的内容。照那条 seed 上线,水军号会被静默标成代管号,
后果有两条:①之后不带 account_id 的发布会广播到水军号;②它们进入笔记数量上限淘汰的
作用域,可能开始删笔记。

教训:**「表里有行」不等于「这些行是我们的」**。seed 规则改的是生产数据,写之前必须
拿生产库实查一遍反例,不能从表名推语义。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'f2b8d41c7e09'
down_revision: Union[str, Sequence[str], None] = 'b7f2c093ad41'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema:加三列(带 server_default,存量行才有值)+ 建审计表。"""
    # server_default 不可省:SQLite 给已有行加 NOT NULL 列必须有默认值,否则整条迁移失败。
    op.add_column(
        'xhs_accounts',
        sa.Column('managed', sa.Boolean(), nullable=False, server_default=sa.text('0')),
    )
    op.add_column(
        'xhs_accounts',
        sa.Column('note_cap', sa.Integer(), nullable=False, server_default=sa.text('100')),
    )
    # 淘汰链的收敛标记:这篇被淘汰删除任务真删掉的时刻(NULL = 还在)。
    # **台账行不物理删** —— published_notes 是永久台账,"我们发过这篇"的事实要留着。
    # 没有这一列,被删掉的笔记仍在库存里计数,第二天照样被选中、照样再建一条删除任务,
    # 而平台上早就没有这张卡片了(幽灵 job)—— 淘汰永远不收敛。
    op.add_column(
        'published_notes',
        sa.Column('deleted_at', sa.DateTime(), nullable=True),
    )

    op.create_table(
        'retention_runs',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('account_id', sa.Integer(), nullable=False),
        sa.Column('run_date', sa.String(), nullable=False),
        sa.Column('platform_note_count', sa.Integer(), nullable=False),
        sa.Column('cap', sa.Integer(), nullable=False),
        sa.Column('eligible_count', sa.Integer(), nullable=False),
        sa.Column('deleted_count', sa.Integer(), nullable=False),
        sa.Column('dry_run', sa.Boolean(), nullable=False),
        sa.Column('details_json', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['account_id'], ['xhs_accounts.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(
        'ix_retention_runs_account_date', 'retention_runs', ['account_id', 'run_date']
    )


def downgrade() -> None:
    """Downgrade schema:删审计表与三列。"""
    op.drop_index('ix_retention_runs_account_date', table_name='retention_runs')
    op.drop_table('retention_runs')
    op.drop_column('published_notes', 'deleted_at')
    op.drop_column('xhs_accounts', 'note_cap')
    op.drop_column('xhs_accounts', 'managed')
