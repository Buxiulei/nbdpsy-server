"""视频产物目录（HMAC token）单测：token 稳定/随 job_id 变、目录布局、公网 URL 换算。

用 monkeypatch 把 DATA_DIR 指到 tmp_path（paths 请求时读 settings.DATA_DIR，故生效），
避免污染真实 data 目录。
"""

from pathlib import Path

from app.core.config import settings
from app.video import paths


def _patch_data_dir(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "DATA_DIR", str(tmp_path))


def test_token_stable_per_job_and_varies(monkeypatch, tmp_path):
    """同 job_id 每次派生同一 token（全生命周期稳定）；不同 job_id token 不同。"""
    _patch_data_dir(monkeypatch, tmp_path)
    t1a = paths._job_token(1)
    t1b = paths._job_token(1)
    t2 = paths._job_token(2)
    assert t1a == t1b
    assert len(t1a) == 16
    assert t1a != t2


def test_token_depends_on_secret_key(monkeypatch, tmp_path):
    """token 由 SECRET_KEY 派生：换 key → token 变（不可猜性来源）。"""
    _patch_data_dir(monkeypatch, tmp_path)
    monkeypatch.setattr(settings, "SECRET_KEY", "secret-A" + "0" * 24)
    ta = paths._job_token(7)
    monkeypatch.setattr(settings, "SECRET_KEY", "secret-B" + "0" * 24)
    tb = paths._job_token(7)
    assert ta != tb


def test_dir_layout_shared_parent(monkeypatch, tmp_path):
    """raw/tts/out 共用同一 {job_id}-{token} 父目录，均落在 DATA_DIR/uploads/video 下。"""
    _patch_data_dir(monkeypatch, tmp_path)
    base = (tmp_path / "uploads" / "video").resolve()

    raw = paths.raw_dir(3)
    tts = paths.tts_dir(3)
    out = paths.out_dir(3)

    assert raw.parent == tts.parent == out.parent
    assert raw.parent.parent == base
    assert raw.parent.name == f"3-{paths._job_token(3)}"
    assert raw.name == "raw" and tts.name == "tts" and out.name == "out"
    for d in (raw, tts, out):
        assert d.is_dir()


def test_to_public_url(monkeypatch, tmp_path):
    """产物绝对路径 → /uploads/video/... 相对 URL（相对 DATA_DIR/uploads）。"""
    _patch_data_dir(monkeypatch, tmp_path)
    out = paths.out_dir(5)
    f = out / "final.mp4"
    f.write_bytes(b"x")
    url = paths.to_public_url(f)
    assert url == f"/uploads/video/5-{paths._job_token(5)}/out/final.mp4"


def test_to_absolute_url(monkeypatch, tmp_path):
    """完整外链 = PUBLIC_BASE_URL + 相对 URL。"""
    _patch_data_dir(monkeypatch, tmp_path)
    monkeypatch.setattr(settings, "PUBLIC_BASE_URL", "https://mcp.nbdpsy.com/")
    f = paths.out_dir(9) / "final.mp4"
    f.write_bytes(b"x")
    assert paths.to_absolute_url(f) == (
        "https://mcp.nbdpsy.com" + paths.to_public_url(f))


def test_base_under_data_dir_uploads(monkeypatch, tmp_path):
    """根锚在 DATA_DIR/uploads/video（与宿主 /uploads 静态根一致）。"""
    _patch_data_dir(monkeypatch, tmp_path)
    assert paths._base() == (Path(str(tmp_path)) / "uploads" / "video").resolve()
