"""内容资产库测试:归档幂等 / 媒体独立副本 / 取即刷新 / 列表不刷新 / TTL 清理 / RBAC。

设计 docs/superpowers/specs/2026-07-25-content-archive-design.md。归档 sync 函数用真 sqlite
文件(archive_published_job 走 sqlite3 直连);REST/服务 async 用隔离库。
"""

import json
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

import app.core.db as db_module
from sqlalchemy import select

from app.models.content_archive import ContentArchive
from app.services import content_archive
from tests.rest_helpers import ADMIN_KEY, bearer, make_operator, rest_client, seed_account


async def _seed_published_job(session_factory, account_id, images):
    """造一条 published 发布任务,返回 job_id。"""
    from app.models.publish_job import PublishJob
    async with session_factory() as s:
        job = PublishJob(
            account_id=account_id, title="标题T", content="正文C",
            images_json=json.dumps(images), topics_json=json.dumps(["情绪疗愈", "自我成长"]),
            status="published", note_url="https://xhs/note/abc", created_by=7,
        )
        s.add(job)
        await s.commit()
        return job.id


def _db_path() -> str:
    return db_module.engine.url.database


# ---------------- 自动归档 ----------------


async def test_archive_published_job_stores_and_idempotent(tmp_path, monkeypatch):
    """归档:落 content_archive 行 + 媒体独立副本(NN.ext);同 job 二次归档幂等不新增。"""
    async with rest_client(tmp_path, monkeypatch) as c:
        acc = await seed_account("号A", "uA", [{"name": "a1"}])
        # 造两张真图片文件当发布素材(/uploads 短路读本地)
        up = Path(db_module.engine.url.database).parent / "uploads" / "srcbatch"
        up.mkdir(parents=True, exist_ok=True)
        (up / "01.jpg").write_bytes(b"\xff\xd8jpgA")
        (up / "02.jpg").write_bytes(b"\xff\xd8jpgB")
        monkeypatch.setattr(content_archive.settings, "DATA_DIR",
                            str(Path(db_module.engine.url.database).parent))
        monkeypatch.setattr(content_archive, "_UPLOADS_ROOT",
                            Path(db_module.engine.url.database).parent / "uploads")

        jid = await _seed_published_job(db_module.async_session, acc,
                                        ["/uploads/srcbatch/01.jpg", "/uploads/srcbatch/02.jpg"])
        aid = content_archive.archive_published_job(_db_path(), jid)
        assert aid is not None

        # 行落库 + 媒体独立副本存在(archive_{id}/01.jpg,02.jpg)
        async with db_module.async_session() as s:
            row = await s.get(ContentArchive, aid)
        assert row.title == "标题T" and row.source_publish_job_id == jid
        media = json.loads(row.media_json)
        assert [m["name"] for m in media] == ["01.jpg", "02.jpg"]
        adir = content_archive._archive_dir(aid)
        assert (adir / "01.jpg").is_file() and (adir / "02.jpg").is_file()
        assert (adir / "01.jpg").read_bytes() == b"\xff\xd8jpgA"  # 独立副本内容对

        # 幂等:再归档同 job 返回同 id,不新增行
        assert content_archive.archive_published_job(_db_path(), jid) == aid
        async with db_module.async_session() as s:
            cnt = len((await s.execute(select(ContentArchive))).scalars().all())
        assert cnt == 1


# ---------------- 取即刷新 / 列表不刷新 ----------------


async def test_get_touches_list_does_not(tmp_path, monkeypatch):
    """取详情刷新 last_used_at+use_count;浏览列表不刷新。"""
    async with rest_client(tmp_path, monkeypatch) as c:
        acc = await seed_account("号B", "uB", [{"name": "a1"}])
        old = datetime.utcnow() - timedelta(days=30)
        async with db_module.async_session() as s:
            row = ContentArchive(
                title="旧内容", content="c", topics_json='["x"]', media_json="[]",
                kind="image_note", source_account_id=acc, created_at=old, last_used_at=old,
                use_count=0)
            s.add(row)
            await s.commit()
            aid = row.id

        # 列表:不刷新
        r = await c.get("/api/content-archive", headers=bearer(ADMIN_KEY))
        assert r.status_code == 200 and len(r.json()["archives"]) == 1
        async with db_module.async_session() as s:
            assert (await s.get(ContentArchive, aid)).use_count == 0  # 浏览未刷新

        # 取详情:刷新
        r2 = await c.get(f"/api/content-archive/{aid}", headers=bearer(ADMIN_KEY))
        assert r2.status_code == 200 and r2.json()["title"] == "旧内容"
        async with db_module.async_session() as s:
            fresh = await s.get(ContentArchive, aid)
        assert fresh.use_count == 1 and fresh.last_used_at > old  # 取即刷新续命


async def test_get_missing_404(tmp_path, monkeypatch):
    async with rest_client(tmp_path, monkeypatch) as c:
        r = await c.get("/api/content-archive/999", headers=bearer(ADMIN_KEY))
        assert r.status_code == 404


# ---------------- TTL 清理 ----------------


async def test_reap_deletes_expired_with_media(tmp_path, monkeypatch):
    """距最后使用超 TTL 的归档被删(行 + 媒体目录);未超期的保留。"""
    async with rest_client(tmp_path, monkeypatch) as c:
        acc = await seed_account("号C", "uC", [{"name": "a1"}])
        monkeypatch.setattr(content_archive, "_UPLOADS_ROOT",
                            Path(db_module.engine.url.database).parent / "uploads")
        expired_at = datetime.utcnow() - timedelta(days=91)
        fresh_at = datetime.utcnow() - timedelta(days=10)
        async with db_module.async_session() as s:
            exp = ContentArchive(title="过期", content="c", topics_json="[]",
                                 media_json='[{"type":"image","name":"01.jpg"}]',
                                 kind="image_note", source_account_id=acc,
                                 created_at=expired_at, last_used_at=expired_at, use_count=0)
            keep = ContentArchive(title="保留", content="c", topics_json="[]",
                                  media_json="[]", kind="image_note", source_account_id=acc,
                                  created_at=fresh_at, last_used_at=fresh_at, use_count=0)
            s.add_all([exp, keep])
            await s.commit()
            exp_id, keep_id = exp.id, keep.id
        # 造过期项的媒体目录
        adir = content_archive._archive_dir(exp_id)
        adir.mkdir(parents=True, exist_ok=True)
        (adir / "01.jpg").write_bytes(b"x")

        n = await content_archive.reap_archive_once(db_module.async_session)
        assert n == 1
        async with db_module.async_session() as s:
            remain = [r.id for r in (await s.execute(select(ContentArchive))).scalars().all()]
        assert remain == [keep_id]  # 过期删、新鲜留
        assert not adir.exists()  # 媒体目录也删了


# ---------------- RBAC ----------------


async def test_delete_admin_only(tmp_path, monkeypatch):
    """DELETE 限 admin:普通 operator → 403;admin → 删成功。"""
    async with rest_client(tmp_path, monkeypatch) as c:
        acc = await seed_account("号D", "uD", [{"name": "a1"}])
        async with db_module.async_session() as s:
            row = ContentArchive(title="待删", content="c", topics_json="[]",
                                 media_json="[]", kind="image_note", source_account_id=acc,
                                 created_at=datetime.utcnow(), last_used_at=datetime.utcnow(),
                                 use_count=0)
            s.add(row)
            await s.commit()
            aid = row.id

        op_key = "op-archive-del"
        await make_operator(op_key)
        r = await c.delete(f"/api/content-archive/{aid}", headers=bearer(op_key))
        assert r.status_code == 403  # 非 admin 拒删

        r2 = await c.delete(f"/api/content-archive/{aid}", headers=bearer(ADMIN_KEY))
        assert r2.status_code == 200 and r2.json()["ok"] is True
        async with db_module.async_session() as s:
            assert await s.get(ContentArchive, aid) is None
