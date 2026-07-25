"""内容资产库服务:发布内容归档 + 跨账号复用取用 + 90 天滑动 TTL 清理。

设计 docs/superpowers/specs/2026-07-25-content-archive-design.md。

- ``archive_published_job``(sync,发布 published 钩子调):从 publish_jobs 读内容,把图片
  物料化成**独立副本**存 DATA_DIR/uploads/archive_{id}/NN.ext(脱离 uploads 7 天清理),
  insert content_archive 行;按 source_publish_job_id 幂等(重复归档跳过)。绝不抛错阻断发布。
- ``list_archives`` / ``get_and_touch``(取详情即刷新 last_used_at + use_count) /
  ``delete_archive``:REST 消费(全局共享,跨账号复用)。
- ``reap_archive_once``:ArchiveReaper 周期调,删距最后使用超 ARCHIVE_TTL_DAYS 的归档(行+目录)。
"""

import json
import os
import shutil
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

from loguru import logger
from sqlalchemy import delete, select

from app.browser.images import materialize_images
from app.core.config import settings
from app.models.content_archive import ContentArchive

# 归档媒体根:落 uploads 下的 archive_{id} 子目录,复用 /uploads 免鉴权直链路由;
# 无 UploadBatch 行 → 不被 uploads 7 天懒清理覆盖,仅 ArchiveReaper 按 TTL 管。
_UPLOADS_ROOT = Path(settings.DATA_DIR) / "uploads"


def _archive_dir(archive_id: int) -> Path:
    return _UPLOADS_ROOT / f"archive_{archive_id}"


def _now() -> str:
    """与 SQLAlchemy DateTime 落库格式一致的 UTC 串(sync 侧用,可字典序比较)。"""
    return datetime.utcnow().isoformat(sep=" ")


# ---------------- 自动归档(发布 published 钩子)----------------


def archive_published_job(db_path: str, job_id: int) -> int | None:
    """把一条已 published 的发布任务归档;返回 archive_id(已归档或失败返回 None)。

    幂等:source_publish_job_id 已存在则跳过。**绝不抛错**——发布终态已先行落库,归档
    是事后副作用,任何异常只告警(媒体拷贝失败会回滚已建行与目录,不留半截)。
    """
    try:
        with sqlite3.connect(db_path, timeout=30) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT id,account_id,title,content,images_json,topics_json,"
                "note_url,created_by FROM publish_jobs WHERE id=?", (job_id,)
            ).fetchone()
            if row is None:
                return None
            # 幂等:该发布已归档过
            dup = conn.execute(
                "SELECT id FROM content_archive WHERE source_publish_job_id=?", (job_id,)
            ).fetchone()
            if dup is not None:
                return dup["id"]
            now = _now()
            # 先插行拿 archive_id(媒体目录名依赖 id);media_json 稍后回填
            cur = conn.execute(
                "INSERT INTO content_archive"
                "(title,content,topics_json,media_json,kind,source_account_id,"
                " source_note_url,source_operator_id,source_publish_job_id,"
                " created_at,last_used_at,use_count)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,0)",
                (row["title"] or "", row["content"] or "", row["topics_json"] or "[]",
                 "[]", "image_note", row["account_id"], row["note_url"],
                 row["created_by"], job_id, now, now),
            )
            archive_id = cur.lastrowid
            conn.commit()

        # 媒体独立副本:物料化图片到临时目录 → 改名 NN.ext 存 archive 目录。
        media = _materialize_media(row["images_json"], archive_id)
        with sqlite3.connect(db_path, timeout=30) as conn:
            conn.execute(
                "UPDATE content_archive SET media_json=? WHERE id=?",
                (json.dumps(media, ensure_ascii=False), archive_id),
            )
            conn.commit()
        logger.info(f"[content_archive] 已归档 job={job_id} → archive={archive_id} "
                    f"媒体 {len(media)} 项")
        return archive_id
    except Exception as exc:  # noqa: BLE001 — 归档绝不阻断发布,失败只告警
        logger.warning(f"[content_archive] 归档失败 job={job_id}(忽略,不阻断发布): {exc}")
        return None


def _materialize_media(images_json: str | None, archive_id: int) -> list[dict]:
    """把发布图片物料化成独立副本(NN.ext)存 archive 目录,返回 media 清单。

    复用 materialize_images(支持 /uploads 本地短路 / http / b64),再按页序改名成
    两位数字文件名(复用 /uploads 免鉴权路由的 _NAME_RE 白名单)。
    """
    raw = json.loads(images_json or "[]")
    if not raw:
        return []
    adir = _archive_dir(archive_id)
    tmp = adir / "_tmp"
    try:
        local = materialize_images(raw, tmp)  # 落成本地文件(混合来源统一)
        media: list[dict] = []
        for i, path in enumerate(local):
            ext = Path(path).suffix.lower()
            if ext not in (".png", ".jpg", ".jpeg", ".webp"):
                ext = ".jpg"
            name = f"{i + 1:02d}{ext}"
            shutil.move(str(path), str(adir / name))
            media.append({"type": "image", "name": name})
        return media
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---------------- 复用/浏览(REST 消费,async)----------------


def _media_urls(archive_id: int, media_json: str) -> list[str]:
    """把 media 清单映射成免鉴权直链(/uploads/archive_{id}/NN.ext)。"""
    base = settings.PUBLIC_BASE_URL.rstrip("/")
    out = []
    for m in json.loads(media_json or "[]"):
        out.append(f"{base}/uploads/archive_{archive_id}/{m['name']}")
    return out


def _summary(row: ContentArchive) -> dict:
    return {
        "id": row.id,
        "title": row.title,
        "topics": json.loads(row.topics_json or "[]"),
        "kind": row.kind,
        "source_account_id": row.source_account_id,
        "media_count": len(json.loads(row.media_json or "[]")),
        "last_used_at": row.last_used_at.isoformat() if row.last_used_at else None,
        "use_count": row.use_count,
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


async def list_archives(session, q: str | None = None, limit: int = 50) -> list[dict]:
    """列库摘要(浏览,**不刷新**);q 模糊搜 title + topics_json;按最近使用降序。"""
    stmt = select(ContentArchive).order_by(ContentArchive.last_used_at.desc())
    if q:
        like = f"%{q}%"
        stmt = stmt.where(
            ContentArchive.title.like(like) | ContentArchive.topics_json.like(like)
        )
    stmt = stmt.limit(max(1, min(limit, 200)))
    rows = (await session.execute(stmt)).scalars().all()
    return [_summary(r) for r in rows]


async def get_and_touch(session, archive_id: int) -> dict | None:
    """取单条完整内容并**刷新 last_used_at + use_count**(取内容=使用,续命 TTL)。"""
    row = await session.get(ContentArchive, archive_id)
    if row is None:
        return None
    row.last_used_at = datetime.utcnow()
    row.use_count = (row.use_count or 0) + 1
    await session.commit()
    return {
        "id": row.id,
        "title": row.title,
        "content": row.content,
        "topics": json.loads(row.topics_json or "[]"),
        "kind": row.kind,
        "media_urls": _media_urls(row.id, row.media_json),
        "source_account_id": row.source_account_id,
        "source_note_url": row.source_note_url,
        "last_used_at": row.last_used_at.isoformat(),
        "use_count": row.use_count,
    }


async def delete_archive(session, archive_id: int) -> bool:
    """删归档(行 + 媒体目录);不存在返回 False。"""
    row = await session.get(ContentArchive, archive_id)
    if row is None:
        return False
    await session.delete(row)
    await session.commit()
    shutil.rmtree(_archive_dir(archive_id), ignore_errors=True)
    return True


# ---------------- 90 天滑动 TTL(ArchiveReaper 调)----------------


async def reap_archive_once(session_factory) -> int:
    """删距最后使用超 ARCHIVE_TTL_DAYS 的归档(先删媒体目录再删行)。返回删除条数。

    纯函数(套 placeholder_reaper 模板),可脱离后台循环单测。
    """
    cutoff = datetime.utcnow() - timedelta(days=settings.ARCHIVE_TTL_DAYS)
    async with session_factory() as session:
        ids = (await session.execute(
            select(ContentArchive.id).where(ContentArchive.last_used_at < cutoff)
        )).scalars().all()
        if not ids:
            return 0
        # 先删媒体目录(删行后 id 仍可算目录名,但先删更稳:行删了目录漏删=孤儿)
        for aid in ids:
            shutil.rmtree(_archive_dir(aid), ignore_errors=True)
        # DELETE 内重申判据防并发(与 placeholder_reaper 同款纪律)
        res = await session.execute(
            delete(ContentArchive).where(ContentArchive.last_used_at < cutoff)
        )
        await session.commit()
        if res.rowcount:
            logger.info(f"[content_archive] TTL 清理过期归档 {res.rowcount} 条")
        return res.rowcount


# ---------------- 后台巡检类(套 placeholder_reaper 模板)----------------


class ArchiveReaper:
    """内容资产库 TTL 后台循环:每 ``interval`` 秒删距最后使用超 ARCHIVE_TTL_DAYS 的归档。

    先睡后扫 + interval==0 不启(由调用方判)+ 单轮异常不崩循环 + stop() 优雅取消。
    结构与 PlaceholderReaper/BrowserReaper 一致。
    """

    def __init__(self, session_factory, interval: float) -> None:
        self._session_factory = session_factory
        self._interval = interval
        self._stop_event = None
        self._loop_task = None

    def start(self) -> None:
        import asyncio
        self._stop_event = asyncio.Event()
        self._loop_task = asyncio.create_task(self._run_loop())

    async def _run_loop(self) -> None:
        while self._stop_event is not None and not self._stop_event.is_set():
            await self._sleep(self._interval)
            if self._stop_event is not None and self._stop_event.is_set():
                break
            try:
                await reap_archive_once(self._session_factory)
            except Exception:
                logger.exception("[content_archive] TTL 清理轮次异常")

    async def _sleep(self, timeout: float) -> None:
        import asyncio
        if self._stop_event is None:
            await asyncio.sleep(timeout)
            return
        try:
            await asyncio.wait_for(self._stop_event.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            pass

    async def stop(self) -> None:
        if self._stop_event is not None:
            self._stop_event.set()
        if self._loop_task is not None:
            await self._loop_task
            self._loop_task = None
