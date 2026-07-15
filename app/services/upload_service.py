"""图片上传服务层:校验 + 落盘(页序) + 插归属批次行 + 懒清理 + 只读自己未过期批次。

约定(与其它 service 一致):纯业务逻辑,用调用方传入的 AsyncSession,不自开引擎。
- save_images:校验张数(1-18)/单张大小/逐张 Pillow 真解;落盘 DATA_DIR/uploads/{batch_id}/{NN}.{ext}
  (NN 从 01 递增即页序 = 上传顺序,ext 由 Pillow 真实 format 定,不信客户端扩展名);
  插一行 UploadBatch(归属 + TTL);末尾懒清理过期批次;返回 {batch_id, urls, expires_at}。
- list_batches:列该 operator 当前未过期的批次(按创建时间倒序)。
- sweep_expired:删 expires_at < now 的批次目录 + 行,返回删除数。
"""

import secrets
import shutil
from datetime import datetime, timedelta
from io import BytesIO
from pathlib import Path

from PIL import Image, UnidentifiedImageError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.operator import Operator
from app.models.upload_batch import UploadBatch

# 单批张数上限(下限 1)。
_MAX_FILES = 18

# Pillow 真实 format → 落盘扩展名。仅放行常见图片格式,其余格式判非法。
_FORMAT_EXT = {
    "PNG": "png",
    "JPEG": "jpg",
    "WEBP": "webp",
}


def _uploads_root() -> Path:
    """上传根目录 DATA_DIR/uploads(每次读 settings,便于测试 monkeypatch DATA_DIR)。"""
    return Path(settings.DATA_DIR) / "uploads"


def _resolve_ext(data: bytes) -> str:
    """用 Pillow 真解字节流,返回落盘扩展名;非图片或不支持格式抛 ValueError。

    Image.open 惰性,必须 verify() 才真正校验完整性;verify 后该 Image 不可再用,故只取 format。
    """
    try:
        img = Image.open(BytesIO(data))
        img.verify()
    except (UnidentifiedImageError, OSError, SyntaxError, ValueError) as exc:
        raise ValueError("上传内容不是有效图片") from exc
    ext = _FORMAT_EXT.get(img.format or "")
    if ext is None:
        raise ValueError(f"不支持的图片格式: {img.format}")
    return ext


async def save_images(
    session: AsyncSession,
    operator: Operator,
    files: list[tuple[str, bytes]],
    now: datetime,
) -> dict:
    """校验并落盘一批图片,插归属批次行,末尾懒清理过期批次;返回 {batch_id, urls, expires_at}。

    校验(任一不过即抛 ValueError,且不落任何盘):张数 1-18;单张 ≤ UPLOAD_MAX_MB;
    逐张 Pillow 真解得真实 format → 扩展名。落盘页序 01..NN = 入参顺序;落盘中途失败清理已写文件不留半批。
    """
    # 1. 张数校验(先于任何落盘)。
    if not 1 <= len(files) <= _MAX_FILES:
        raise ValueError(f"图片张数须在 1-{_MAX_FILES} 之间,当前 {len(files)}")

    # 2. 逐张校验大小 + 真解格式(全部通过才落盘,避免半批)。
    max_bytes = settings.UPLOAD_MAX_MB * 1024 * 1024
    resolved: list[tuple[bytes, str]] = []
    for _filename, data in files:
        if len(data) > max_bytes:
            raise ValueError(f"单张图片超过 {settings.UPLOAD_MAX_MB}MB 上限")
        resolved.append((data, _resolve_ext(data)))

    # 3. 落盘:DATA_DIR/uploads/{batch_id}/{NN}.{ext},NN 从 01 即页序。
    batch_id = secrets.token_urlsafe(12)
    batch_dir = _uploads_root() / batch_id
    batch_dir.mkdir(parents=True, exist_ok=True)
    urls: list[str] = []
    try:
        for i, (data, ext) in enumerate(resolved, start=1):
            name = f"{i:02d}.{ext}"
            (batch_dir / name).write_bytes(data)
            urls.append(f"{settings.PUBLIC_BASE_URL}/uploads/{batch_id}/{name}")
    except OSError:
        # 落盘中途失败:清理已写的半批,不留残目录。
        shutil.rmtree(batch_dir, ignore_errors=True)
        raise

    # 4. 插归属批次行(归属 + TTL)。
    expires_at = now + timedelta(days=settings.UPLOAD_TTL_DAYS)
    session.add(
        UploadBatch(
            batch_id=batch_id,
            operator_id=operator.id,
            file_count=len(resolved),
            created_at=now,
            expires_at=expires_at,
        )
    )
    await session.commit()

    # 5. 懒清理:顺带扫掉已过期批次(不影响本次刚插入的新批次)。
    await sweep_expired(session, now)

    return {"batch_id": batch_id, "urls": urls, "expires_at": expires_at}


async def list_batches(session: AsyncSession, operator: Operator) -> list[dict]:
    """列该 operator 当前未过期的批次(按创建时间倒序),供 GET /api/uploads 用。

    过期判定用当前时刻(datetime.utcnow),与落盘 now 注入无关——列表反映"此刻还有效的批次"。
    """
    now = datetime.utcnow()
    rows = (
        await session.execute(
            select(UploadBatch)
            .where(
                UploadBatch.operator_id == operator.id,
                UploadBatch.expires_at > now,
            )
            .order_by(UploadBatch.created_at.desc())
        )
    ).scalars().all()
    return [
        {
            "batch_id": r.batch_id,
            "file_count": r.file_count,
            "created_at": r.created_at.isoformat() if r.created_at else None,
            "expires_at": r.expires_at.isoformat() if r.expires_at else None,
        }
        for r in rows
    ]


async def sweep_expired(session: AsyncSession, now: datetime) -> int:
    """删 expires_at < now 的批次目录(shutil.rmtree ignore_errors)+ 行,返回删除数。"""
    expired = (
        await session.execute(
            select(UploadBatch).where(UploadBatch.expires_at < now)
        )
    ).scalars().all()
    for batch in expired:
        shutil.rmtree(_uploads_root() / batch.batch_id, ignore_errors=True)
        await session.delete(batch)
    if expired:
        await session.commit()
    return len(expired)
