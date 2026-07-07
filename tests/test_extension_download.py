"""Task 4.2 测试:打包脚本 + 下载端点 + get_extension_download 工具。

三块契约各自独立验证,不碰生产库/生产 data 目录:
- pack_extension.sh:跑完在 DATA_DIR 产出非空、可被 zipfile 打开、含 manifest.json 的 zip。
- GET /downloads/extension.zip:白名单放行(带/不带 apikey 都 200)+ application/zip;
  zip 缺失 → 404。测试全程 monkeypatch settings.DATA_DIR 指向 tmp,且该端点白名单
  短路在 apikey 校验之前 → 不触发 DB,故无需跑 lifespan/隔离库。
- get_extension_download:download_url 前缀 PUBLIC_BASE_URL、version=app 版本、
  install_steps 非空,apikey_hint 是引导语而非明文 key。
"""

import subprocess
import zipfile
from pathlib import Path

from httpx import ASGITransport, AsyncClient

from app import __version__
from app.core import config as config_module
from app.server import create_app

# 仓库根 = tests/ 的上一级;打包脚本在 scripts/pack_extension.sh。
_REPO_ROOT = Path(__file__).resolve().parent.parent
_PACK_SCRIPT = _REPO_ROOT / "scripts" / "pack_extension.sh"


# ============ 打包脚本 ============


def test_pack_script_produces_valid_zip(tmp_path):
    """跑 pack_extension.sh(DATA_DIR 指向 tmp)→ 产出非空、可解压、含 manifest.json 的 zip。"""
    assert _PACK_SCRIPT.is_file(), f"缺少打包脚本 {_PACK_SCRIPT}"

    result = subprocess.run(
        ["bash", str(_PACK_SCRIPT)],
        env={"DATA_DIR": str(tmp_path), "PATH": "/usr/bin:/bin"},
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"脚本失败: {result.stderr}"

    zip_path = tmp_path / "extension.zip"
    assert zip_path.is_file(), "未产出 extension.zip"
    assert zip_path.stat().st_size > 0, "extension.zip 为空"

    with zipfile.ZipFile(zip_path) as zf:
        names = zf.namelist()
        assert "manifest.json" in names, f"zip 内缺 manifest.json: {names}"


def test_pack_script_is_idempotent(tmp_path):
    """重复跑两次都成功且都产出有效 zip(幂等,不因残留 zip 报错)。"""
    env = {"DATA_DIR": str(tmp_path), "PATH": "/usr/bin:/bin"}
    for _ in range(2):
        result = subprocess.run(
            ["bash", str(_PACK_SCRIPT)], env=env, capture_output=True, text=True
        )
        assert result.returncode == 0, f"脚本失败: {result.stderr}"
    zip_path = tmp_path / "extension.zip"
    with zipfile.ZipFile(zip_path) as zf:
        assert "manifest.json" in zf.namelist()


# ============ 下载端点 ============


def _make_fake_zip(data_dir: Path) -> Path:
    """在 data_dir 造一个最小合法 zip(含 manifest.json),供下载端点测试用。"""
    data_dir.mkdir(parents=True, exist_ok=True)
    zip_path = data_dir / "extension.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr("manifest.json", '{"manifest_version": 3}')
    return zip_path


async def test_download_returns_zip_with_apikey(tmp_path, monkeypatch):
    """带 apikey GET → 200 + application/zip(白名单放行,apikey 不影响)。"""
    monkeypatch.setattr(config_module.settings, "DATA_DIR", str(tmp_path))
    _make_fake_zip(tmp_path)

    app = create_app()
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://t"
    ) as c:
        r = await c.get(
            "/downloads/extension.zip",
            headers={"Authorization": "Bearer whatever"},
        )
        assert r.status_code == 200, r.text
        assert r.headers["content-type"] == "application/zip"
        assert len(r.content) > 0


async def test_download_without_apikey_still_200(tmp_path, monkeypatch):
    """不带 apikey GET → 仍 200(/downloads 在中间件白名单)。"""
    monkeypatch.setattr(config_module.settings, "DATA_DIR", str(tmp_path))
    _make_fake_zip(tmp_path)

    app = create_app()
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://t"
    ) as c:
        r = await c.get("/downloads/extension.zip")
        assert r.status_code == 200, r.text
        assert r.headers["content-type"] == "application/zip"


async def test_download_missing_zip_404(tmp_path, monkeypatch):
    """zip 未打包(DATA_DIR 无 extension.zip)→ 404,提示先跑打包脚本。"""
    monkeypatch.setattr(config_module.settings, "DATA_DIR", str(tmp_path))
    # 不造 zip

    app = create_app()
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://t"
    ) as c:
        r = await c.get("/downloads/extension.zip")
        assert r.status_code == 404


# ============ get_extension_download 工具 ============


async def _call_tool() -> dict:
    """构建裸 FastMCP、注册全部工具、调 get_extension_download,返回其结构化响应。"""
    from fastmcp import FastMCP

    from app.tools import register_all

    mcp = FastMCP("test")
    register_all(mcp)
    result = await mcp.call_tool("get_extension_download", {})
    return result.structured_content


async def test_tool_registered_and_download_url(monkeypatch):
    """download_url = PUBLIC_BASE_URL + /downloads/extension.zip;version = app 版本。"""
    monkeypatch.setattr(
        config_module.settings, "PUBLIC_BASE_URL", "https://xhs.example.com"
    )
    payload = await _call_tool()
    assert (
        payload["download_url"]
        == "https://xhs.example.com/downloads/extension.zip"
    )
    assert payload["version"] == __version__


async def test_tool_install_steps_non_empty():
    """install_steps 为非空的中文步骤数组。"""
    payload = await _call_tool()
    steps = payload["install_steps"]
    assert isinstance(steps, list)
    assert len(steps) > 0
    assert all(isinstance(s, str) and s for s in steps)


async def test_tool_apikey_hint_is_guidance_not_plaintext(monkeypatch):
    """apikey_hint 是引导语(提到 apikey 与 rotate),绝不回传任何明文 key。"""
    sentinel = "SENTINEL-PLAINTEXT-APIKEY-should-never-leak"
    monkeypatch.setattr(config_module.settings, "ROOT_ADMIN_APIKEY", sentinel)

    payload = await _call_tool()
    hint = payload["apikey_hint"]
    assert isinstance(hint, str) and hint
    assert "apikey" in hint
    assert "rotate_operator_apikey" in hint
    # 任何字段值都不得泄露明文 key。
    assert sentinel not in repr(payload)
