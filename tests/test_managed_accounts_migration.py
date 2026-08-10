"""代管账号迁移 f2b8d41c7e09 的行为测试:加三列 + 建审计表,且**一个账号都不 seed**。

最要紧的是 seed 那条:上一版迁移里写过「凡 published_notes 里有过记录的账号一律
managed=1」,理由是"发过内容的就是内容号"。实查生产库后推翻 —— published_notes 是台账
**全量同步**捞回来的,纯互动号(水军号)名下同样有行,那些是同步时捞到的**他人个人笔记**
orphan 行。照那条 seed 上线,水军号会被静默标成代管号,之后不带 account_id 的发布会广播
到它们,而且它们会进入笔记上限淘汰的作用域开始删笔记。

所以这里跑的是**真迁移链**(不是 create_all):先升到前一个修订、在那张表里造出"有
published_notes 行的账号"这个反例,再升到 head,断言它仍然 managed=0。create_all 建出来的
库测不到这个 —— seed 是迁移脚本里的 UPDATE,只有跑迁移才会执行。
"""

import pathlib
import sqlite3

from alembic import command
from alembic.config import Config

from app.core.config import settings

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
# 代管迁移的 down_revision:在这一步造反例数据,再升到 head 看 seed 有没有动它
_BEFORE_MANAGED = "b7f2c093ad41"


def _cfg() -> Config:
    return Config(str(_REPO_ROOT / "alembic.ini"))


def _columns(db_file: str, table: str) -> set[str]:
    conn = sqlite3.connect(db_file)
    try:
        return {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    finally:
        conn.close()


def _required_columns(db_file: str, table: str) -> list[str]:
    """该表里「NOT NULL 且无默认值」的列——裸 INSERT 必须显式给值的那些。"""
    conn = sqlite3.connect(db_file)
    try:
        return [
            r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()
            if r[3] and r[4] is None and not r[5]
        ]
    finally:
        conn.close()


def _insert(db_file: str, table: str, values: dict) -> None:
    """按 values 建一行;表里其余 NOT NULL 无默认列统一填占位值,免得随模型演进而失修。"""
    conn = sqlite3.connect(db_file)
    try:
        row = dict(values)
        for col in _required_columns(db_file, table):
            row.setdefault(col, "x")
        cols = ", ".join(row)
        marks = ", ".join("?" for _ in row)
        conn.execute(f"INSERT INTO {table} ({cols}) VALUES ({marks})", list(row.values()))
        conn.commit()
    finally:
        conn.close()


def test_migration_adds_columns_and_audit_table(monkeypatch, tmp_path):
    """三列(managed/note_cap/published_notes.deleted_at)与 retention_runs 都出自迁移链。

    「PR 合并了但漏跑 alembic upgrade」的结局是缺列 + 静默 500,而缺列在 create_all 建的库上
    测不出来(那边表是照模型现建的)。这里跑纯迁移链,列在不在只取决于迁移文件。
    """
    db_file = str(tmp_path / "mig.db")
    monkeypatch.setattr(settings, "DATABASE_URL", f"sqlite+aiosqlite:///{db_file}")
    command.upgrade(_cfg(), "head")

    assert {"managed", "note_cap"} <= _columns(db_file, "xhs_accounts")
    # 淘汰链的收敛标记:没有它,删掉的笔记仍在库存里计数,每天被重新选中建幽灵删除任务
    assert "deleted_at" in _columns(db_file, "published_notes")
    conn = sqlite3.connect(db_file)
    try:
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        indexes = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index'").fetchall()}
    finally:
        conn.close()
    assert "retention_runs" in tables
    assert "ix_retention_runs_account_date" in indexes


def test_migration_does_not_seed_managed_for_accounts_with_notes(monkeypatch, tmp_path):
    """**有 published_notes 行的账号迁移后仍然 managed=0** —— 一个号都不许被 seed 成代管。

    反例就是这条测试的全部意义:published_notes 里有行 ≠ 这些行是我们发的内容。台账全量
    同步会把**他人的个人笔记**当 orphan 行捞进来,水军号名下照样有。按"有行即内容号"seed,
    水军号会被静默拉进代管 → 广播发布发到它们头上 + 它们开始被淘汰删笔记。

    加入代管是显式动作,归 PUT /api/accounts/{id}/managed 管,不归迁移替人做主。
    """
    db_file = str(tmp_path / "seed.db")
    monkeypatch.setattr(settings, "DATABASE_URL", f"sqlite+aiosqlite:///{db_file}")

    # 先升到代管迁移之前,在那张表上造反例:一个名下有台账行的账号(即"疑似内容号")
    command.upgrade(_cfg(), _BEFORE_MANAGED)
    _insert(db_file, "xhs_accounts", {"id": 9, "name": "水军号9"})
    _insert(db_file, "published_notes", {
        "id": 1, "account_id": 9, "title": "别人的个人笔记(同步捞回来的 orphan 行)",
    })

    command.upgrade(_cfg(), "head")

    conn = sqlite3.connect(db_file)
    try:
        managed, note_cap = conn.execute(
            "SELECT managed, note_cap FROM xhs_accounts WHERE id=9").fetchone()
    finally:
        conn.close()
    assert managed == 0, "迁移把有 published_notes 行的账号 seed 成代管号了(F1 回归)"
    assert note_cap == 100  # 上限仍取默认值,与 seed 无关


def test_migration_downgrade_removes_everything(monkeypatch, tmp_path):
    """downgrade 回前序修订:三列与审计表全部消失,前序表不受牵连。"""
    db_file = str(tmp_path / "down.db")
    monkeypatch.setattr(settings, "DATABASE_URL", f"sqlite+aiosqlite:///{db_file}")
    cfg = _cfg()
    command.upgrade(cfg, "head")
    command.downgrade(cfg, _BEFORE_MANAGED)

    conn = sqlite3.connect(db_file)
    try:
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    finally:
        conn.close()
    assert "retention_runs" not in tables
    assert "published_notes" in tables  # 前序表还在
    assert not {"managed", "note_cap"} & _columns(db_file, "xhs_accounts")
    assert "deleted_at" not in _columns(db_file, "published_notes")
