"""style_profiles / style_profile_versions 每用户风格档案两表 + 管理员默认档案 seed

Revision ID: c7e9a4b21d38
Revises: b1f4c2a09e77
Create Date: 2026-07-26 10:00:00.000000

需求 /home/roots/NBDpsy/文档/2026-07-26-每用户风格档案-server需求.md。

- style_profiles:当前档案,operator_id 唯一(一运营一份);operator_id IS NULL 的那一行
  是本迁移 seed 的管理员默认档案(无个人档案时的回落内容)。
- style_profile_versions:append-only 快照,(operator_id, version) 唯一。
- 两表 FK 均带 ondelete=CASCADE(声明意图 / 供 PG 与开了 PRAGMA 的场景生效);SQLite 下
  真正的级联清空由 operator_service.delete_operator 在应用层做。
- seed 的 profile 内容与 app/services/style_profile.ADMIN_DEFAULT_PROFILE 逐字一致
  (迁移按惯例内联字面量而非 import 应用常量——迁移是历史快照,不该随后续改动漂移;
  两者一致性由 tests/test_style_profile.py 的防漂移测试钉死)。
"""
import json
from datetime import datetime
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c7e9a4b21d38'
down_revision: Union[str, Sequence[str], None] = 'b1f4c2a09e77'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# 管理员默认档案:现有莫兰迪三色 + 固定人物卡那一套(需求文档第四节示例值)。
# density 的五个 key 是 skill 侧定死的中文字段名,原样保留。
ADMIN_DEFAULT_PROFILE = {
    "visual": {
        "palette": [
            {"name": "雾霾蓝灰", "hex": "#A8B5C4"},
            {"name": "暖米白", "hex": "#E8D8C4"},
            {"name": "鼠尾草绿", "hex": "#C9D6CE"},
        ],
        "text_color": "#5A6B7B",
        "character_card": "圆脸、齐肩微卷短发的东亚年轻女性,穿燕麦色针织衫,神情温和平静",
        "texture": "柔和扁平矢量、莫兰迪低饱和、暖米白纸质底、圆润线条、柔光无强阴影",
        "cover_layout": "上方留白区大标题 + 下方主体场景,标题占画面高约 1/3",
        "content_layout": "满版分 2–3 区块,每条信息配一个各不相同的具体场景小图",
    },
    "density": {
        "信息密度档位": "默认",
        "每页文字量": "200–400 字",
        "每页信息点": "6–10 个",
        "版式档": "满版",
        "运营原话": "—",
    },
    "tone": {
        "person": "第二人称「你」为主",
        "sentence_length": "平均 14 字,最长 26 字",
        "emoji_target": "8–14",
        "emoji_style": "温和款 🌱🫧🌙☁️💭🤍🌿,避高唤起款",
        "quote_rule": "仅人物原话可带引号,全文 ≤4 对",
        "paragraph_rhythm": "单句段占 40%,每段后空一行",
    },
    "structure": {
        "title_structure": "核心议题词 + 场景钩子(议题词占前 10 字)",
        "hook_type": "具体场景句(时间 + 动作)",
        "ending": "一个今天能做、门槛低于付费的小动作",
    },
}


def upgrade() -> None:
    """Upgrade schema:建两表 + 唯一约束/索引,并 seed 管理员默认档案行。"""
    op.create_table(
        'style_profiles',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('operator_id', sa.Integer(), nullable=True),
        sa.Column('version', sa.Integer(), nullable=False),
        sa.Column('profile_json', sa.JSON(), nullable=False),
        sa.Column('source', sa.String(length=20), nullable=False),
        sa.Column('note', sa.Text(), nullable=True),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.Column('updated_by', sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(['operator_id'], ['operators.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('operator_id', name='uq_style_profiles_operator'),
    )
    op.create_table(
        'style_profile_versions',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('operator_id', sa.Integer(), nullable=False),
        sa.Column('version', sa.Integer(), nullable=False),
        sa.Column('profile_json', sa.JSON(), nullable=False),
        sa.Column('source', sa.String(length=20), nullable=False),
        sa.Column('note', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('created_by', sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(['operator_id'], ['operators.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint(
            'operator_id', 'version', name='uq_style_profile_versions_op_ver'
        ),
    )
    with op.batch_alter_table('style_profile_versions', schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f('ix_style_profile_versions_operator_id'),
            ['operator_id'], unique=False,
        )

    # seed 管理员默认档案:operator_id NULL、version 0(该行不参与版本流)。
    # 时间格式与 SQLAlchemy DateTime 落 sqlite 的格式一致(可字典序比较)。
    op.execute(
        sa.text(
            "INSERT INTO style_profiles"
            " (operator_id, version, profile_json, source, note, updated_at, updated_by)"
            " VALUES (NULL, 0, :profile, 'admin_default', :note, :now, NULL)"
        ).bindparams(
            profile=json.dumps(ADMIN_DEFAULT_PROFILE, ensure_ascii=False),
            note="管理员默认风格档案(无个人档案时的回落)",
            now=datetime.utcnow().isoformat(sep=" "),
        )
    )


def downgrade() -> None:
    """Downgrade schema:删索引 + 两表(含 seed 行与全部历史版本)。"""
    with op.batch_alter_table('style_profile_versions', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_style_profile_versions_operator_id'))

    op.drop_table('style_profile_versions')
    op.drop_table('style_profiles')
