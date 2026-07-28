"""style_profiles 多套 + 每套独立版本链(profile_set)

Revision ID: f4a2c8e1b9d7
Revises: d5a71c93b02e
Create Date: 2026-07-28 22:00:00.000000

设计 docs/design-2026-07-28-profile-sets.md(§5 迁移 + §9 评审修订)。

在「运营 ↔ 档案」之间加「套」这一层:
- style_profiles 加 name/kind/is_active,唯一约束 (operator_id) → (operator_id, name);
  管理员默认(NULL operator)另加部分唯一索引 uq_admin_set_name(WHERE operator_id IS NULL)。
- style_profile_versions 加 set_id(FK→style_profiles.id),唯一约束 (operator_id,version)
  → (set_id, version);版本链改挂到套上。

数据迁移识别两形态(幂等 + 防御式):
- 老平铺(无 schema 或 schema != profiles-v1;含解析失败兜底):落一套 图文/carousel/is_active,
  版本行 set_id 回填、版本号不变。
- profiles-v1 容器(现生产为 0,但预留):按 profiles 拆 N 套,active 决定 is_active;整份历史
  挂 active(承接)套 + note 标注,承接套 current version = max(history)+1 落一条拆分后单套快照,
  其余套各自 v1。管理员默认(NULL)同构(不进版本表,operator_id NOT NULL)。
"""
import json
from datetime import datetime
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'f4a2c8e1b9d7'
down_revision: Union[str, Sequence[str], None] = 'd5a71c93b02e'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _parse_profile(raw) -> object:
    """宽松解析 profile_json;失败返 None(调用方按老平铺兜底,不中断迁移)。"""
    if isinstance(raw, (dict, list)):
        return raw
    try:
        return json.loads(raw)
    except Exception:
        return None


def _migrate_data(conn) -> None:
    """回填 set_id + 拆分 profiles-v1 容器(两形态,幂等 + 防御式)。"""
    now = datetime.utcnow().isoformat(sep=" ")
    rows = conn.execute(
        sa.text(
            "SELECT id, operator_id, version, profile_json FROM style_profiles ORDER BY id"
        )
    ).fetchall()
    for sp_id, op_id, cur_version, raw_profile in rows:
        profile = _parse_profile(raw_profile)
        is_container = (
            isinstance(profile, dict)
            and profile.get("schema") == "profiles-v1"
            and isinstance(profile.get("profiles"), dict)
            and bool(profile.get("profiles"))
        )
        if not is_container:
            # 老平铺(或解析失败兜底):name/kind/is_active 已由 server_default 落 图文/carousel/1;
            # 只需回填该 owner 版本行的 set_id(仅尚为空的,幂等)。管理员默认(op_id NULL)无版本行。
            if op_id is not None:
                conn.execute(
                    sa.text(
                        "UPDATE style_profile_versions SET set_id=:sid "
                        "WHERE operator_id=:op AND set_id IS NULL"
                    ),
                    {"sid": sp_id, "op": op_id},
                )
            continue

        # ---- profiles-v1 容器:拆 N 套 ----
        sub = profile["profiles"]
        active_name = profile.get("active")
        if active_name not in sub:
            active_name = next(iter(sub))  # 兜底:active 不在 profiles 里,取第一套
        active_content = sub[active_name]
        active_kind = (
            active_content.get("kind", "carousel")
            if isinstance(active_content, dict)
            else "carousel"
        )

        # 承接套 current version:实运营 = max(history)+1(先算,后面 UPDATE/快照共用);
        # 管理员默认不进版本流,记 1。**在插入任何新版本行前算,避免把新行的版本号也算进 MAX**。
        if op_id is not None:
            maxv = conn.execute(
                sa.text(
                    "SELECT COALESCE(MAX(version), :cur) FROM style_profile_versions WHERE operator_id=:op"
                ),
                {"cur": cur_version, "op": op_id},
            ).scalar()
            new_current = (maxv or cur_version) + 1
        else:
            new_current = 1

        # 0) 当前行先改写为 active 套单套内容(**必须最先做**:释放 server_default 的 '图文' 名,
        #    否则拆出的同名 图文 套会撞 (operator_id, name) 唯一)。再跑时容器已被替换 → 走老平铺路径,幂等。
        conn.execute(
            sa.text(
                "UPDATE style_profiles SET name=:name, kind=:kind, is_active=1, "
                "version=:ver, profile_json=:pj WHERE id=:id"
            ),
            {
                "name": active_name,
                "kind": active_kind,
                "ver": new_current,
                "pj": json.dumps(active_content, ensure_ascii=False),
                "id": sp_id,
            },
        )

        # 1) 其余套 → 新行(各自 v1);幂等:name 已存在则跳过
        for name, content in sub.items():
            if name == active_name:
                continue
            # 排除承接行(sp_id):它此刻还挂 server_default 的 '图文',稍后才改成 active_name,
            # 不排除会让"拆出的 图文 套"误判为已存在而漏建。幂等靠"再跑时容器已被替换走老平铺路径"。
            if op_id is not None:
                exists = conn.execute(
                    sa.text(
                        "SELECT id FROM style_profiles "
                        "WHERE operator_id=:op AND name=:name AND id != :sp"
                    ),
                    {"op": op_id, "name": name, "sp": sp_id},
                ).first()
            else:
                exists = conn.execute(
                    sa.text(
                        "SELECT id FROM style_profiles "
                        "WHERE operator_id IS NULL AND name=:name AND id != :sp"
                    ),
                    {"name": name, "sp": sp_id},
                ).first()
            if exists:
                continue
            kind = (
                content.get("kind", "carousel")
                if isinstance(content, dict)
                else "carousel"
            )
            res = conn.execute(
                sa.text(
                    "INSERT INTO style_profiles "
                    "(operator_id, name, kind, is_active, version, profile_json, source, note, updated_at, updated_by) "
                    "VALUES (:op, :name, :kind, 0, 1, :pj, 'manual', NULL, :now, NULL)"
                ),
                {
                    "op": op_id,
                    "name": name,
                    "kind": kind,
                    "pj": json.dumps(content, ensure_ascii=False),
                    "now": now,
                },
            )
            if op_id is not None:  # 管理员默认不进版本表
                conn.execute(
                    sa.text(
                        "INSERT INTO style_profile_versions "
                        "(set_id, operator_id, version, profile_json, source, note, created_at, created_by) "
                        "VALUES (:sid, :op, 1, :pj, 'manual', '迁移拆分', :now, NULL)"
                    ),
                    {
                        "sid": res.lastrowid,
                        "op": op_id,
                        "pj": json.dumps(content, ensure_ascii=False),
                        "now": now,
                    },
                )

        # 2) 承接套(=当前行)= active 套:整份历史挂它 + note 标注,落一条拆分后单套快照 v=new_current
        if op_id is not None:
            # 整份历史挂承接套 + note 前缀(仅 set_id 空的,幂等;此刻拆出套的 v1 快照 set_id 已非空,不被误挂)
            conn.execute(
                sa.text(
                    "UPDATE style_profile_versions "
                    "SET set_id=:sid, note='迁移前整份历史;' || COALESCE(note,'') "
                    "WHERE operator_id=:op AND set_id IS NULL"
                ),
                {"sid": sp_id, "op": op_id},
            )
            snap_exists = conn.execute(
                sa.text(
                    "SELECT 1 FROM style_profile_versions WHERE set_id=:sid AND version=:ver"
                ),
                {"sid": sp_id, "ver": new_current},
            ).first()
            if not snap_exists:
                conn.execute(
                    sa.text(
                        "INSERT INTO style_profile_versions "
                        "(set_id, operator_id, version, profile_json, source, note, created_at, created_by) "
                        "VALUES (:sid, :op, :ver, :pj, 'migration', '迁移拆分后单套快照', :now, NULL)"
                    ),
                    {
                        "sid": sp_id,
                        "op": op_id,
                        "ver": new_current,
                        "pj": json.dumps(active_content, ensure_ascii=False),
                        "now": now,
                    },
                )


def upgrade() -> None:
    """Upgrade schema + 两形态数据迁移。"""
    # 1. style_profiles:加列 + 唯一约束改(SQLite 走 batch_alter_table)
    with op.batch_alter_table('style_profiles', schema=None) as batch_op:
        batch_op.add_column(
            sa.Column('name', sa.String(length=40), nullable=False, server_default='图文')
        )
        batch_op.add_column(
            sa.Column('kind', sa.String(length=20), nullable=False, server_default='carousel')
        )
        batch_op.add_column(
            sa.Column('is_active', sa.Boolean(), nullable=False, server_default=sa.true())
        )
        batch_op.drop_constraint('uq_style_profiles_operator', type_='unique')
        batch_op.create_unique_constraint(
            'uq_style_profiles_operator_name', ['operator_id', 'name']
        )
    # 管理员默认(NULL operator)套名部分唯一索引:联合约束对 NULL 不生效,这里补(§9 B1)
    op.create_index(
        'uq_admin_set_name',
        'style_profiles',
        ['name'],
        unique=True,
        sqlite_where=sa.text('operator_id IS NULL'),
        postgresql_where=sa.text('operator_id IS NULL'),
    )

    # 2. style_profile_versions:加 set_id(可空,供回填)+ **先换唯一约束**(set_id,version)。
    #    唯一约束必须在数据迁移前换掉:老 (operator_id, version) 会拦住"同运营多套复用同版本号"
    #    (拆容器时 图文/水墨 各自 v1 都是 operator_id 相同 version=1)。回填期 set_id 全 NULL,
    #    (set_id,version) 对 NULL 不约束(NULL≠NULL),不误伤;回填后各套内 (set_id,version) 唯一。
    with op.batch_alter_table('style_profile_versions', schema=None) as batch_op:
        batch_op.add_column(sa.Column('set_id', sa.Integer(), nullable=True))
        batch_op.create_index(
            batch_op.f('ix_style_profile_versions_set_id'), ['set_id'], unique=False
        )
        batch_op.drop_constraint('uq_style_profile_versions_op_ver', type_='unique')
        batch_op.create_unique_constraint(
            'uq_style_profile_versions_set_ver', ['set_id', 'version']
        )
        batch_op.create_foreign_key(
            'fk_spv_set_id', 'style_profiles', ['set_id'], ['id'], ondelete='CASCADE'
        )

    # 3. 数据迁移:回填 set_id + 拆分容器(两形态)
    _migrate_data(op.get_bind())

    # 4. 回填完成,set_id 收 NOT NULL
    with op.batch_alter_table('style_profile_versions', schema=None) as batch_op:
        batch_op.alter_column('set_id', existing_type=sa.Integer(), nullable=False)


def downgrade() -> None:
    """Downgrade schema:恢复旧约束、删新列/索引。

    仅还原表结构(假定回退时每 owner 单套);多套并存时无法无损还原,回退前请自行收敛到单套。
    """
    with op.batch_alter_table('style_profile_versions', schema=None) as batch_op:
        batch_op.drop_constraint('fk_spv_set_id', type_='foreignkey')
        batch_op.drop_index(batch_op.f('ix_style_profile_versions_set_id'))
        batch_op.drop_constraint('uq_style_profile_versions_set_ver', type_='unique')
        batch_op.create_unique_constraint(
            'uq_style_profile_versions_op_ver', ['operator_id', 'version']
        )
        batch_op.drop_column('set_id')

    op.drop_index('uq_admin_set_name', table_name='style_profiles')
    with op.batch_alter_table('style_profiles', schema=None) as batch_op:
        batch_op.drop_constraint('uq_style_profiles_operator_name', type_='unique')
        batch_op.create_unique_constraint(
            'uq_style_profiles_operator', ['operator_id']
        )
        batch_op.drop_column('is_active')
        batch_op.drop_column('kind')
        batch_op.drop_column('name')
