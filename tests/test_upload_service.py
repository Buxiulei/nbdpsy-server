"""upload_service 服务层单测:图片落盘(页序)+ 归属批次行 + 懒清理 + 只读自己未过期批次。

复用 conftest 的 db fixture(每测试独立临时 sqlite,自动建表 + 清理)。
DATA_DIR 由 monkeypatch 指到 tmp_path;now 由测试注入固定值,保证落盘目录与过期判定可复现。
真图用 Pillow 现造 PNG bytes,非图片用任意 bytes(触发 verify 失败)。
"""

from datetime import datetime, timedelta
from io import BytesIO
from pathlib import Path

import pytest
from PIL import Image
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models import Operator, UploadBatch
from app.services import upload_service as svc


def _png_bytes(color: tuple[int, int, int] = (200, 30, 30), size=(4, 4)) -> bytes:
    """现造一张真 PNG 的字节串(Pillow 可正常 verify)。"""
    buf = BytesIO()
    Image.new("RGB", size, color).save(buf, format="PNG")
    return buf.getvalue()


async def _make_operator(db: AsyncSession, name: str = "op") -> Operator:
    """造一个启用中的运营者(apikey_hash 占位,单测不走鉴权中间件)。"""
    op = Operator(name=name, role="operator", apikey_hash=f"h-{name}", enabled=True)
    db.add(op)
    await db.commit()
    return op


async def test_save_images_writes_pageorder_and_row(db, tmp_path, monkeypatch):
    """2 张真图 → 落盘 01/02.png、urls 顺序=入参顺序、插一行 file_count=2 expires_at=now+7天。"""
    monkeypatch.setattr(settings, "DATA_DIR", str(tmp_path))
    op = await _make_operator(db)
    now = datetime(2026, 7, 13, 12, 0, 0)
    files = [("first.png", _png_bytes((200, 0, 0))), ("second.png", _png_bytes((0, 0, 200)))]

    result = await svc.save_images(db, op, files, now)

    batch_id = result["batch_id"]
    batch_dir = Path(str(tmp_path)) / "uploads" / batch_id
    # 落盘页序 01/02,扩展名由 Pillow 真实 format 定(PNG→png)
    assert (batch_dir / "01.png").exists()
    assert (batch_dir / "02.png").exists()
    # urls 顺序 = 入参顺序,用 PUBLIC_BASE_URL 拼
    assert result["urls"] == [
        f"{settings.PUBLIC_BASE_URL}/uploads/{batch_id}/01.png",
        f"{settings.PUBLIC_BASE_URL}/uploads/{batch_id}/02.png",
    ]
    assert result["expires_at"] == now + timedelta(days=settings.UPLOAD_TTL_DAYS)

    # 插一行 UploadBatch,归属 + 计数 + TTL 正确
    rows = (await db.execute(select(UploadBatch))).scalars().all()
    assert len(rows) == 1
    assert rows[0].batch_id == batch_id
    assert rows[0].operator_id == op.id
    assert rows[0].file_count == 2
    assert rows[0].expires_at == now + timedelta(days=settings.UPLOAD_TTL_DAYS)


async def test_save_rejects_non_image(db, tmp_path, monkeypatch):
    """非图片 bytes → Pillow verify 失败 → 抛 ValueError,不落半批(uploads 目录不残留)。"""
    monkeypatch.setattr(settings, "DATA_DIR", str(tmp_path))
    op = await _make_operator(db)
    now = datetime(2026, 7, 13, 12, 0, 0)

    with pytest.raises(ValueError):
        await svc.save_images(db, op, [("fake.png", b"notimage")], now)

    # 校验阶段即失败,尚未 mkdir,uploads 目录不应存在
    assert not (Path(str(tmp_path)) / "uploads").exists()
    # 未插行
    assert (await db.execute(select(UploadBatch))).scalars().all() == []


async def test_save_rejects_too_many(db, tmp_path, monkeypatch):
    """>18 张 → ValueError,不落盘。"""
    monkeypatch.setattr(settings, "DATA_DIR", str(tmp_path))
    op = await _make_operator(db)
    now = datetime(2026, 7, 13, 12, 0, 0)
    files = [(f"{i}.png", _png_bytes()) for i in range(19)]

    with pytest.raises(ValueError):
        await svc.save_images(db, op, files, now)

    assert not (Path(str(tmp_path)) / "uploads").exists()


async def test_save_rejects_too_large(db, tmp_path, monkeypatch):
    """单张 > UPLOAD_MAX_MB → ValueError,不落盘。"""
    monkeypatch.setattr(settings, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(settings, "UPLOAD_MAX_MB", 1)
    op = await _make_operator(db)
    now = datetime(2026, 7, 13, 12, 0, 0)
    oversized = b"0" * (1 * 1024 * 1024 + 1)  # 1MB + 1 字节,超 1MB 上限

    with pytest.raises(ValueError):
        await svc.save_images(db, op, [("big.png", oversized)], now)

    assert not (Path(str(tmp_path)) / "uploads").exists()


async def test_sweep_expired_removes_dir_and_row(db, tmp_path, monkeypatch):
    """造一过期 + 一未过期批次(各落个文件)→ sweep 删过期目录+行返回1;未过期不动。"""
    monkeypatch.setattr(settings, "DATA_DIR", str(tmp_path))
    op = await _make_operator(db)
    now = datetime(2026, 7, 13, 12, 0, 0)
    uploads = Path(str(tmp_path)) / "uploads"

    # 过期批次:expires_at < now,物理造目录 + 文件
    expired_dir = uploads / "expired_batch"
    expired_dir.mkdir(parents=True)
    (expired_dir / "01.png").write_bytes(_png_bytes())
    db.add(
        UploadBatch(
            batch_id="expired_batch",
            operator_id=op.id,
            file_count=1,
            created_at=now - timedelta(days=8),
            expires_at=now - timedelta(days=1),
        )
    )
    # 未过期批次:expires_at > now
    alive_dir = uploads / "alive_batch"
    alive_dir.mkdir(parents=True)
    (alive_dir / "01.png").write_bytes(_png_bytes())
    db.add(
        UploadBatch(
            batch_id="alive_batch",
            operator_id=op.id,
            file_count=1,
            created_at=now,
            expires_at=now + timedelta(days=1),
        )
    )
    await db.commit()

    deleted = await svc.sweep_expired(db, now)

    assert deleted == 1
    assert not expired_dir.exists()  # 过期目录被删
    assert alive_dir.exists()  # 未过期目录保留
    remaining = (await db.execute(select(UploadBatch))).scalars().all()
    assert [r.batch_id for r in remaining] == ["alive_batch"]


async def test_list_batches_only_own_unexpired(db, tmp_path, monkeypatch):
    """operator A 两批(一过期)→ list 只返未过期;B 看不到 A 的。"""
    monkeypatch.setattr(settings, "DATA_DIR", str(tmp_path))
    op_a = await _make_operator(db, "A")
    op_b = await _make_operator(db, "B")

    # 用远过去/远未来的 expires_at,使过期判定不依赖真实时钟
    db.add_all(
        [
            UploadBatch(
                batch_id="a_expired",
                operator_id=op_a.id,
                file_count=1,
                created_at=datetime(2000, 1, 1),
                expires_at=datetime(2000, 1, 8),
            ),
            UploadBatch(
                batch_id="a_alive",
                operator_id=op_a.id,
                file_count=1,
                created_at=datetime(2999, 1, 1),
                expires_at=datetime(2999, 1, 8),
            ),
            UploadBatch(
                batch_id="b_alive",
                operator_id=op_b.id,
                file_count=1,
                created_at=datetime(2999, 1, 1),
                expires_at=datetime(2999, 1, 8),
            ),
        ]
    )
    await db.commit()

    got_a = await svc.list_batches(db, op_a)
    assert [b["batch_id"] for b in got_a] == ["a_alive"]

    got_b = await svc.list_batches(db, op_b)
    assert [b["batch_id"] for b in got_b] == ["b_alive"]
