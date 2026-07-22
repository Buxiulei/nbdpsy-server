"""alembic 迁移 up/down 单测：video_jobs 表随 upgrade head 建、随 downgrade -1 删。

对临时 sqlite 跑真实 alembic 迁移链（env.py 请求时读 settings.DATABASE_URL，故 monkeypatch
即可把迁移目标指到 tmp 文件库，不碰真 data 库）。同时校验索引与前序表不受牵连。
"""

import sqlite3

from alembic import command
from alembic.config import Config

from app.core.config import settings

# 仓库根（本文件在 tests/ 下）
import pathlib

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]


def _table_names(db_file: str) -> set[str]:
    conn = sqlite3.connect(db_file)
    try:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        return {r[0] for r in rows}
    finally:
        conn.close()


def _index_names(db_file: str) -> set[str]:
    conn = sqlite3.connect(db_file)
    try:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index'").fetchall()
        return {r[0] for r in rows}
    finally:
        conn.close()


def test_migration_up_creates_and_down_drops(monkeypatch, tmp_path):
    db_file = str(tmp_path / "mig.db")
    monkeypatch.setattr(
        settings, "DATABASE_URL", f"sqlite+aiosqlite:///{db_file}")

    cfg = Config(str(_REPO_ROOT / "alembic.ini"))

    # upgrade 全链到 head：video_jobs 建成，索引到位，前序表仍在
    command.upgrade(cfg, "head")
    tables = _table_names(db_file)
    assert "video_jobs" in tables
    assert "upload_batches" in tables  # 前序迁移不受牵连
    indexes = _index_names(db_file)
    assert {
        "ix_video_jobs_video_id",
        "ix_video_jobs_parent_job_id",
        "ix_video_jobs_status",
        "ix_video_jobs_created_by",
    } <= indexes

    # downgrade 一步（回退本迁移）：video_jobs 消失，前序表保留
    command.downgrade(cfg, "-1")
    tables_after = _table_names(db_file)
    assert "video_jobs" not in tables_after
    assert "upload_batches" in tables_after
