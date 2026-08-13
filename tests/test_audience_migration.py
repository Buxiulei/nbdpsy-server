"""受众行为库迁移(a7c31e9b48d2)行为测试:跑**真迁移链**,不是 create_all。

「PR 合并了但漏跑 alembic upgrade」的结局是缺表 + 静默 500,而缺表在 create_all 建的库上
永远测不出来(那边表是照模型现建的)。这里跑纯迁移链,表/索引在不在只取决于迁移文件。

另外两条:

- **一行都不 seed**:历史随时可回采,预置游标等于宣称"这段采过了"而库里一条都没有;
- **合规红线**:表结构里不许出现任何指向来访者真实身份的列。这条用不着靠人记 ——
  列清单在这里钉死,谁加了会当场红。
"""

import pathlib
import sqlite3

from alembic import command
from alembic.config import Config

from app.core.config import settings

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]


def _cfg() -> Config:
    return Config(str(_REPO_ROOT / "alembic.ini"))


def _upgraded(monkeypatch, tmp_path, name: str) -> str:
    db_file = str(tmp_path / name)
    monkeypatch.setattr(settings, "DATABASE_URL", f"sqlite+aiosqlite:///{db_file}")
    command.upgrade(_cfg(), "head")
    return db_file


def _query(db_file: str, sql: str) -> list:
    conn = sqlite3.connect(db_file)
    try:
        return conn.execute(sql).fetchall()
    finally:
        conn.close()


def test_migration_creates_tables_and_indexes(monkeypatch, tmp_path):
    db_file = _upgraded(monkeypatch, tmp_path, "aud_mig.db")

    tables = {r[0] for r in _query(
        db_file, "SELECT name FROM sqlite_master WHERE type='table'")}
    indexes = {r[0] for r in _query(
        db_file, "SELECT name FROM sqlite_master WHERE type='index'")}

    assert {"audience_events", "audience_sync_state"} <= tables
    assert {"ix_audience_events_actor", "ix_audience_events_account_time"} <= indexes


def test_unique_key_includes_account_id(monkeypatch, tmp_path):
    """去重键必须是 (account_id, platform_event_id) —— 只用事件 id 会让两个号互相顶掉。"""
    db_file = _upgraded(monkeypatch, tmp_path, "aud_uq.db")
    conn = sqlite3.connect(db_file)
    try:
        row = lambda aid: (  # noqa: E731
            "INSERT INTO audience_events (account_id, platform_event_id, actor_userid,"
            " actor_nickname, event_type, event_time, raw_json)"
            f" VALUES ({aid}, 'same-id', 'u1', 'n', 'like_note', 1, '{{}}')"
        )
        conn.execute(row(1))
        conn.execute(row(2))  # 同事件 id 不同号:必须能共存
        conn.commit()
        try:
            conn.execute(row(1))  # 同号同事件 id:必须被 UNIQUE 拦住
            conn.commit()
            raise AssertionError("同号同事件 id 竟然插进去了,去重键没生效")
        except sqlite3.IntegrityError:
            pass
    finally:
        conn.close()


def test_no_seed_rows(monkeypatch, tmp_path):
    """迁移不预置任何事件与游标(预置游标 = 宣称"采过了",而库里一条都没有)。"""
    db_file = _upgraded(monkeypatch, tmp_path, "aud_seed.db")

    assert _query(db_file, "SELECT COUNT(*) FROM audience_events")[0][0] == 0
    assert _query(db_file, "SELECT COUNT(*) FROM audience_sync_state")[0][0] == 0


def test_timestamp_columns_have_ddl_defaults(monkeypatch, tmp_path):
    """时间列必须带 DDL DEFAULT:本仓有裸 sqlite3 显式列清单 INSERT 的路径,
    NOT NULL 无默认值的列会让那些语句当场炸,而且只在跑到那条路径时才炸。"""
    db_file = _upgraded(monkeypatch, tmp_path, "aud_def.db")

    for table, column in (("audience_events", "created_at"),
                          ("audience_sync_state", "updated_at")):
        info = {r[1]: r for r in _query(db_file, f"PRAGMA table_info({table})")}
        assert info[column][4] is not None, f"{table}.{column} 缺 DDL 默认值"


def test_schema_has_no_visitor_identity_columns(monkeypatch, tmp_path):
    """**合规红线钉死在列清单上**:两张表里不许出现任何指向来访者真实身份的列。

    这不是形式主义 —— 设计里明确写了"绝不建立 actor_userid → 来访者真实身份(姓名/手机/
    预约记录/咨询关系)的任何关联字段或外键"。靠人记会忘,靠这条会当场红。
    """
    db_file = _upgraded(monkeypatch, tmp_path, "aud_pii.db")
    banned = ("phone", "mobile", "real_name", "realname", "client_id", "patient",
              "appointment", "booking", "wechat", "email", "id_card", "visitor")

    for table in ("audience_events", "audience_sync_state"):
        columns = [r[1].lower() for r in _query(db_file, f"PRAGMA table_info({table})")]
        for column in columns:
            assert not any(word in column for word in banned), (
                f"{table}.{column} 看着像来访者身份关联列,设计里明确禁止"
            )


def test_downgrade_removes_tables(monkeypatch, tmp_path):
    """回滚干净:受众三张表是纯新增,downgrade 后一点痕迹不留(也不碰任何既有表)。

    回滚目标用**显式版本号**(受众功能前的 head)而不是 "-1":功能已横跨两个迁移
    (a7c31e9b48d2 两张表 + b3d9f2c4a1e7 自家号登记表),步数会随链生长漂移。
    """
    db_file = _upgraded(monkeypatch, tmp_path, "aud_down.db")
    command.downgrade(_cfg(), "c4e7a91d3b58")

    tables = {r[0] for r in _query(
        db_file, "SELECT name FROM sqlite_master WHERE type='table'")}
    assert "audience_events" not in tables and "audience_sync_state" not in tables
    assert "audience_self_userids" not in tables
    # 既有表毫发无损
    assert {"xhs_accounts", "published_notes"} <= tables
