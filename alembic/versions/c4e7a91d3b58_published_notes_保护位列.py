"""published_notes 加保护位 protected:被标记的笔记永不进淘汰候选

Revision ID: c4e7a91d3b58
Revises: f2b8d41c7e09
Create Date: 2026-08-11 12:00:00.000000

0.22.0 的每日淘汰按五指标加权删得分最低的几篇,底下压着一条假设:**低互动 = 低价值**。
内容运营实战当场推翻了它 —— 全矩阵置顶的**功能位笔记**(品牌片、二维码导流笔记)浏览量
只有 11-13,天然垫底,却是矩阵的门面与转化入口。照原口径它们会被当"最差的那几篇"删掉,
而删除**不可逆**。

保护位就是给这条假设开的显式例外:``protected=1`` 的笔记**排除出淘汰候选,但仍计入库存**
—— 它占着上限的名额(平台上确实有这一篇),只是永远不被选中。库存不算它的话,一个号标满
保护位就等于把 note_cap 悄悄放大,那是另一种失控。

**一篇都不 seed**:哪几篇是功能位只有运营知道,迁移替人做主等于凭空替一批笔记做承诺。
谁受保护由 ``PUT /api/accounts/{account_id}/notes/{note_id}/protected`` 显式开启。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c4e7a91d3b58'
down_revision: Union[str, Sequence[str], None] = 'f2b8d41c7e09'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema:published_notes 加 protected(NOT NULL,DDL 默认 0)。"""
    # **server_default 不可省**,两个理由各自独立:
    # ① SQLite 给已有行加 NOT NULL 列必须有默认值,否则整条迁移失败;
    # ② ``app/services/note_ledger.py`` 建台账行走的是**显式列名的裸 sqlite3 INSERT**
    #    (不经 ORM,列清单写死在 SQL 串里),新列在 DDL 里没有 DEFAULT 时那条 INSERT 会
    #    当场炸 —— 症状是"发布成功了、台账行却落不下来",而发布链对台账写入是吞异常的,
    #    连报错都看不到。tests/test_managed_accounts_migration.py 有一条用例专钉这点。
    op.add_column(
        'published_notes',
        sa.Column('protected', sa.Boolean(), nullable=False, server_default=sa.text('0')),
    )


def downgrade() -> None:
    """Downgrade schema:删 protected 列。"""
    op.drop_column('published_notes', 'protected')
