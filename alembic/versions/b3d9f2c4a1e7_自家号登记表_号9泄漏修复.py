"""受众分析自家号排除:audience_self_userids 追加型登记表 + 号9 数据修复

Revision ID: b3d9f2c4a1e7
Revises: a7c31e9b48d2
Create Date: 2026-08-13 02:40:00.000000

2026-08-13 号9 泄漏事故的结构性修复(内容运营验收抓到):受众分析的自家号排除原来
只查 ``xhs_accounts`` 活名单,扛得住加号、扛不住删号 —— 号9(米之木木)的账号行被
移出系统后,它 55 条互刷事件以互动第一名顶在潜客漏斗头部(还改了昵称「淡三花」,
实证排除键只能认 user_id 不能认昵称)。

修法:登记表**只进不出**(进过矩阵就永远排除),``self_account_userids`` 每次调用把
活名单合并进来,排除按登记表走。

两条 seed 都是安全的(对照 f2b8d41c7e09 的"不 seed"教训):
- 现存 ``xhs_accounts`` 的 user_id 全量入登记 —— "现在是自家号"是表结构自身语义,
  不涉及从行为反推身份的推理;
- 号9 的 user_id 单条补录 —— 已确证的泄漏个案数据修复,证据:audience_events 里
  55 条 actor_userid=5c2b6136… 全部是矩阵互刷任务产物。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b3d9f2c4a1e7'
down_revision: Union[str, Sequence[str], None] = 'a7c31e9b48d2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """建登记表 + 现存账号种子 + 号9 补录。"""
    op.create_table(
        'audience_self_userids',
        sa.Column('user_id', sa.String(), nullable=False),
        sa.Column('first_seen', sa.DateTime(), nullable=False,
                  server_default=sa.text('CURRENT_TIMESTAMP')),
        sa.PrimaryKeyConstraint('user_id'),
    )
    op.execute(
        "INSERT INTO audience_self_userids(user_id) "
        "SELECT DISTINCT user_id FROM xhs_accounts "
        "WHERE user_id IS NOT NULL AND user_id != ''"
    )
    # 号9(米之木木→淡三花)已不在 xhs_accounts,上面那条捞不到它,单独补录
    op.execute(
        "INSERT OR IGNORE INTO audience_self_userids(user_id) "
        "VALUES ('5c2b613600000000070106a0')"
    )


def downgrade() -> None:
    """删登记表(排除退回活名单口径,号9 会重新泄漏——降级即接受)。"""
    op.drop_table('audience_self_userids')
