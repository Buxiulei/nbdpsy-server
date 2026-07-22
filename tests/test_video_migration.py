"""alembic 迁移 up/down 单测：video_jobs 表随 upgrade head 建、随 downgrade 到前序修订删。

对临时 sqlite 跑真实 alembic 迁移链（env.py 请求时读 settings.DATABASE_URL，故 monkeypatch
即可把迁移目标指到 tmp 文件库，不碰真 data 库）。同时校验索引与前序表不受牵连。

downgrade 目标显式定位到 video_jobs 修订的 down_revision（5fdec94dd809，即 video_jobs 之前的
upload_batches 头），而非相对 ``-1``：M4 在 video_jobs 之上又叠了 psych_glossary 迁移成为新
head，相对 ``-1`` 只会退掉最上层 psych_glossary、留下 video_jobs，令本测试假失败。显式退到
video_jobs 之前的修订，才真正验证 video_jobs 迁移的 downgrade 把表删净。
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

    # upgrade 全链到 head：video_jobs / psych_glossary 建成，索引到位，前序表仍在
    command.upgrade(cfg, "head")
    tables = _table_names(db_file)
    assert "video_jobs" in tables
    assert "psych_glossary" in tables  # I-1：术语表随 alembic head 建（不再仅靠 create_all）
    assert "upload_batches" in tables  # 前序迁移不受牵连
    indexes = _index_names(db_file)
    assert {
        "ix_video_jobs_video_id",
        "ix_video_jobs_parent_job_id",
        "ix_video_jobs_status",
        "ix_video_jobs_created_by",
        "ix_psych_glossary_en_term",
    } <= indexes

    # downgrade 显式退到 video_jobs 修订的 down_revision（5fdec94dd809=upload_batches 头）：
    # video_jobs（及其上叠的 psych_glossary）消失，前序 upload_batches 表保留。相对 "-1" 会因
    # 新 head 是 psych_glossary 而只退掉它、留下 video_jobs 致假失败，故此处用绝对修订号定位。
    command.downgrade(cfg, "5fdec94dd809")
    tables_after = _table_names(db_file)
    assert "video_jobs" not in tables_after
    assert "psych_glossary" not in tables_after
    assert "upload_batches" in tables_after
