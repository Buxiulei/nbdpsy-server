"""上传/取图 REST 端点测试(A 端点):POST 上传 / GET 取图 / GET 列表 / GET 上传页。

隔离手法复用 tests/rest_helpers.rest_client(隔离库 + 真实 lifespan),并额外把
settings.DATA_DIR monkeypatch 到 tmp,使 save_images 落盘与 GET 取图打在同一临时目录、
不碰生产 ./data。真图由 Pillow 现造(save_images 靠真解格式定扩展名,不信客户端扩展名)。

覆盖(brief 用例):
- POST /api/uploads/images:2 真图 + admin key → 200 {batch_id, urls, expires_at};
  无 key → 401(不在白名单);非图片 → 400;>18 张 → 400。
- GET /uploads/{batch}/01.png:免 key → 200 + 正确 content-type;不存在 → 404;
  路径穿越(name/batch_id 正则白名单不匹配)→ 404。
- GET /upload:免 key → 200 text/html。
- GET /api/uploads:带 key → 列自己批次。
"""

from io import BytesIO

import pytest
from PIL import Image

from app.core import config as config_module
from tests.rest_helpers import ADMIN_KEY, bearer, rest_client


def _png_bytes(color=(200, 30, 30)) -> bytes:
    """现造一张 4x4 PNG 真图字节流。"""
    buf = BytesIO()
    Image.new("RGB", (4, 4), color).save(buf, format="PNG")
    return buf.getvalue()


def _jpeg_bytes(color=(30, 160, 60)) -> bytes:
    """现造一张 4x4 JPEG 真图字节流。"""
    buf = BytesIO()
    Image.new("RGB", (4, 4), color).save(buf, format="JPEG")
    return buf.getvalue()


def _patch_data_dir(tmp_path, monkeypatch) -> None:
    """把 DATA_DIR 指到 tmp,save_images 落盘与 GET 取图共用此临时目录。"""
    monkeypatch.setattr(config_module.settings, "DATA_DIR", str(tmp_path / "data"))


async def _upload(client, files) -> "tuple[int, dict]":
    """POST 一批图片(admin key),返回 (status_code, json)。"""
    r = await client.post(
        "/api/uploads/images", files=files, headers=bearer(ADMIN_KEY)
    )
    body = r.json() if r.headers.get("content-type", "").startswith(
        "application/json"
    ) else {}
    return r.status_code, body


# ---------------- POST /api/uploads/images ----------------


async def test_upload_images_returns_batch(tmp_path, monkeypatch):
    """2 真图 + admin key → 200,返回 {batch_id, urls(2 条), expires_at}。"""
    _patch_data_dir(tmp_path, monkeypatch)
    async with rest_client(tmp_path, monkeypatch) as client:
        files = [
            ("files", ("a.png", _png_bytes(), "image/png")),
            ("files", ("b.jpg", _jpeg_bytes(), "image/jpeg")),
        ]
        status, body = await _upload(client, files)
        assert status == 200, body
        assert body["batch_id"]
        assert isinstance(body["urls"], list) and len(body["urls"]) == 2
        # 落盘页序 01/02,扩展名由真实格式定(png/jpg)
        assert body["urls"][0].endswith(f"/uploads/{body['batch_id']}/01.png")
        assert body["urls"][1].endswith(f"/uploads/{body['batch_id']}/02.jpg")
        assert body["expires_at"]


async def test_upload_without_key_401(tmp_path, monkeypatch):
    """无 apikey → 401(/api/uploads/images 不在白名单,走鉴权)。"""
    _patch_data_dir(tmp_path, monkeypatch)
    async with rest_client(tmp_path, monkeypatch) as client:
        r = await client.post(
            "/api/uploads/images",
            files=[("files", ("a.png", _png_bytes(), "image/png"))],
        )
        assert r.status_code == 401


async def test_upload_non_image_400(tmp_path, monkeypatch):
    """非图片内容 → save_images 抛 ValueError → 400。"""
    _patch_data_dir(tmp_path, monkeypatch)
    async with rest_client(tmp_path, monkeypatch) as client:
        files = [("files", ("fake.png", b"not-an-image", "image/png"))]
        status, body = await _upload(client, files)
        assert status == 400, body
        assert body["error"]


async def test_upload_too_many_400(tmp_path, monkeypatch):
    """>18 张 → save_images 抛 ValueError → 400。"""
    _patch_data_dir(tmp_path, monkeypatch)
    async with rest_client(tmp_path, monkeypatch) as client:
        files = [
            ("files", (f"{i}.png", _png_bytes(), "image/png")) for i in range(19)
        ]
        status, body = await _upload(client, files)
        assert status == 400, body


# ---------------- GET /uploads/{batch_id}/{name} ----------------


async def test_uploaded_image_served_without_key(tmp_path, monkeypatch):
    """上传后 GET /uploads/{batch}/01.png 免 key → 200 + content-type image/png。"""
    _patch_data_dir(tmp_path, monkeypatch)
    async with rest_client(tmp_path, monkeypatch) as client:
        files = [("files", ("a.png", _png_bytes(), "image/png"))]
        status, body = await _upload(client, files)
        assert status == 200, body
        path = f"/uploads/{body['batch_id']}/01.png"
        r = await client.get(path)  # 不带任何 apikey
        assert r.status_code == 200, r.text
        assert r.headers["content-type"] == "image/png"
        assert len(r.content) > 0


async def test_uploaded_jpeg_content_type(tmp_path, monkeypatch):
    """jpg 取图 content-type = image/jpeg(按扩展名推断)。"""
    _patch_data_dir(tmp_path, monkeypatch)
    async with rest_client(tmp_path, monkeypatch) as client:
        files = [("files", ("a.jpg", _jpeg_bytes(), "image/jpeg"))]
        status, body = await _upload(client, files)
        assert status == 200, body
        r = await client.get(f"/uploads/{body['batch_id']}/01.jpg")
        assert r.status_code == 200, r.text
        assert r.headers["content-type"] == "image/jpeg"


async def test_serve_nonexistent_404(tmp_path, monkeypatch):
    """batch_id/name 合法但文件不存在 → 404。"""
    _patch_data_dir(tmp_path, monkeypatch)
    async with rest_client(tmp_path, monkeypatch) as client:
        r = await client.get("/uploads/AAAAAAAAAAAA/01.png")
        assert r.status_code == 404


async def test_serve_bad_name_ext_404(tmp_path, monkeypatch):
    """name 扩展名不在白名单(.txt)→ 正则不匹配 → 404。"""
    _patch_data_dir(tmp_path, monkeypatch)
    async with rest_client(tmp_path, monkeypatch) as client:
        r = await client.get("/uploads/AAAAAAAAAAAA/01.txt")
        assert r.status_code == 404


async def test_serve_bad_name_shape_404(tmp_path, monkeypatch):
    """name 非 NN.ext 形态(traversal 类)→ 正则不匹配 → 404。"""
    _patch_data_dir(tmp_path, monkeypatch)
    async with rest_client(tmp_path, monkeypatch) as client:
        r = await client.get("/uploads/AAAAAAAAAAAA/passwd.png")
        assert r.status_code == 404


async def test_serve_encoded_traversal_404(tmp_path, monkeypatch):
    """编码穿越(..%2F / %0A 尾随换行)必 404,把'编码穿越挡死'锁进回归。

    ..%2F 解码成多段不匹配单段路由;%0A 由 fullmatch(非 match+$)拒绝——防未来正则被放松。
    """
    _patch_data_dir(tmp_path, monkeypatch)
    async with rest_client(tmp_path, monkeypatch) as client:
        for path in (
            "/uploads/AAAA/..%2F..%2Fetc%2Fpasswd",
            "/uploads/..%2F..%2Fetc/01.png",
            "/uploads/AAAA/01.png%0a",
        ):
            r = await client.get(path)
            assert r.status_code == 404, f"{path} 应 404,得 {r.status_code}"


async def test_serve_bad_batch_id_404(tmp_path, monkeypatch):
    """batch_id 含非 token_urlsafe 字符(点号)→ 正则不匹配 → 404。"""
    _patch_data_dir(tmp_path, monkeypatch)
    async with rest_client(tmp_path, monkeypatch) as client:
        r = await client.get("/uploads/ba.tch/01.png")
        assert r.status_code == 404


# ---------------- GET /upload(上传页) ----------------


async def test_upload_page_without_key_html(tmp_path, monkeypatch):
    """GET /upload 免 key → 200 text/html(白名单精确放行)。"""
    async with rest_client(tmp_path, monkeypatch) as client:
        r = await client.get("/upload")
        assert r.status_code == 200, r.text
        assert r.headers["content-type"].startswith("text/html")
        # 页面内联 fetch 目标端点,且禁 emoji(图标用 SVG)
        assert "/api/uploads/images" in r.text


# ---------------- GET /api/uploads(列表) ----------------


async def test_list_uploads_lists_own(tmp_path, monkeypatch):
    """上传一批后 GET /api/uploads 带 key → batches 含该 batch_id。"""
    _patch_data_dir(tmp_path, monkeypatch)
    async with rest_client(tmp_path, monkeypatch) as client:
        files = [("files", ("a.png", _png_bytes(), "image/png"))]
        status, body = await _upload(client, files)
        assert status == 200, body
        batch_id = body["batch_id"]

        r = await client.get("/api/uploads", headers=bearer(ADMIN_KEY))
        assert r.status_code == 200, r.text
        listing = r.json()
        assert "batches" in listing
        ids = [b["batch_id"] for b in listing["batches"]]
        assert batch_id in ids


async def test_list_uploads_without_key_401(tmp_path, monkeypatch):
    """GET /api/uploads 无 key → 401。"""
    async with rest_client(tmp_path, monkeypatch) as client:
        r = await client.get("/api/uploads")
        assert r.status_code == 401
