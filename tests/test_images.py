"""images.materialize_images 单测（不起浏览器）。

覆盖发布前物料化三条入口:
- base64 dict `{b64, ext}` → 解码落盘
- data URI 字符串 → 解析 mediatype 解码落盘
- http URL → httpx 下载落盘(monkeypatch 假图字节)
- 混合列表顺序保持
"""
import base64

import pytest

from app.browser import images

# 合法 1x1 透明 PNG（base64,无换行）
_PNG_1x1_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwC"
    "AAAAC0lEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
)
_PNG_1x1 = base64.b64decode(_PNG_1x1_B64)


def test_materialize_base64_dict(tmp_path):
    """`{b64, ext}` 形式 → 按 ext 落盘,文件存在且内容匹配。"""
    paths = images.materialize_images([{"b64": _PNG_1x1_B64, "ext": "png"}], tmp_path)
    assert len(paths) == 1
    assert paths[0].exists()
    assert paths[0].suffix == ".png"
    assert paths[0].read_bytes() == _PNG_1x1


def test_materialize_base64_dict_default_ext(tmp_path):
    """`{b64}` 缺 ext → 兜底 .jpg,仍落盘成功。"""
    paths = images.materialize_images([{"b64": _PNG_1x1_B64}], tmp_path)
    assert paths[0].exists()
    assert paths[0].suffix == ".jpg"


def test_materialize_data_uri(tmp_path):
    """data URI 字符串 → 从 mediatype 推扩展名解码落盘。"""
    uri = "data:image/png;base64," + _PNG_1x1_B64
    paths = images.materialize_images([uri], tmp_path)
    assert paths[0].exists()
    assert paths[0].suffix == ".png"
    assert paths[0].read_bytes() == _PNG_1x1


def test_materialize_http_url(tmp_path, monkeypatch):
    """http URL → httpx 下载(monkeypatch 假图字节)→ 落盘。"""

    class _FakeResp:
        content = _PNG_1x1
        headers = {"content-type": "image/png"}

        def raise_for_status(self):
            return None

    def _fake_get(url, **kwargs):
        assert url == "https://cdn.example.com/pic.png"
        return _FakeResp()

    monkeypatch.setattr(images.httpx, "get", _fake_get)

    paths = images.materialize_images(["https://cdn.example.com/pic.png"], tmp_path)
    assert paths[0].exists()
    assert paths[0].read_bytes() == _PNG_1x1
    assert paths[0].suffix == ".png"


def test_materialize_http_url_ext_from_content_type(tmp_path, monkeypatch):
    """URL 无扩展名 → 从 Content-Type 推扩展名。"""

    class _FakeResp:
        content = _PNG_1x1
        headers = {"content-type": "image/webp; charset=binary"}

        def raise_for_status(self):
            return None

    monkeypatch.setattr(images.httpx, "get", lambda url, **kw: _FakeResp())

    paths = images.materialize_images(["https://cdn.example.com/download?id=42"], tmp_path)
    assert paths[0].exists()
    assert paths[0].suffix == ".webp"


def test_materialize_mixed_order_preserved(tmp_path, monkeypatch):
    """混合列表(base64 / data uri / http)顺序必须与输入一致。"""

    class _FakeResp:
        content = b"HTTPBYTES"
        headers = {"content-type": "image/jpeg"}

        def raise_for_status(self):
            return None

    monkeypatch.setattr(images.httpx, "get", lambda url, **kw: _FakeResp())

    items = [
        {"b64": base64.b64encode(b"AAA").decode(), "ext": "png"},
        "data:image/gif;base64," + base64.b64encode(b"BBB").decode(),
        "https://cdn.example.com/pic.jpg",
    ]
    paths = images.materialize_images(items, tmp_path)
    assert len(paths) == 3
    assert paths[0].read_bytes() == b"AAA"
    assert paths[1].read_bytes() == b"BBB"
    assert paths[2].read_bytes() == b"HTTPBYTES"
    # 扩展名各自正确
    assert paths[0].suffix == ".png"
    assert paths[1].suffix == ".gif"
    assert paths[2].suffix == ".jpg"


def test_materialize_unknown_item_raises(tmp_path):
    """既非 http 也非 base64 的项 → 抛 ValueError(不静默丢图打乱顺序)。"""
    with pytest.raises(ValueError):
        images.materialize_images([12345], tmp_path)


def test_materialize_empty_list(tmp_path):
    """空列表 → 返回空列表,不建垃圾文件。"""
    assert images.materialize_images([], tmp_path) == []


def test_materialize_empty_b64_raises(tmp_path):
    """N4:空 b64 → ValueError(与 URL 空 body 一致),不写 0 字节文件。"""
    with pytest.raises(ValueError):
        images.materialize_images([{"b64": "", "ext": "png"}], tmp_path)
    # 未写出任何文件
    assert list(tmp_path.iterdir()) == []


def test_materialize_missing_b64_raises(tmp_path):
    """N4:缺 b64 键 → ValueError,不写 0 字节文件。"""
    with pytest.raises(ValueError):
        images.materialize_images([{"ext": "png"}], tmp_path)
    assert list(tmp_path.iterdir()) == []


def test_materialize_local_uploads_shortcut(tmp_path, monkeypatch):
    """本服务 /uploads 直链(绝对 URL 或相对路径)走本地文件短路,不发网络请求;
    路径穿越/不存在的相对路径 fail-loud。"""
    import pytest
    from app.browser.images import materialize_images

    uploads = tmp_path / "uploads" / "opimg_abc"
    uploads.mkdir(parents=True)
    (uploads / "01.jpg").write_bytes(b"\xff\xd8fakejpg")
    monkeypatch.setattr("app.core.config.settings.DATA_DIR", str(tmp_path))

    def no_net(*a, **k):  # 命中短路后绝不应走 http 下载
        raise AssertionError("不应发起网络请求")
    monkeypatch.setattr("app.browser.images._materialize_http", no_net)

    out = materialize_images(
        ["https://mcp.nbdpsy.com/uploads/opimg_abc/01.jpg",
         "/uploads/opimg_abc/01.jpg"],
        tmp_path / "wd")
    assert len(out) == 2 and all(p.is_file() for p in out)
    assert out[0].read_bytes().startswith(b"\xff\xd8")

    with pytest.raises(ValueError):
        materialize_images(["/uploads/opimg_abc/nope.jpg"], tmp_path / "wd2")
