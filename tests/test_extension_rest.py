"""GET /api/extension 端点测试:平移自 tests/test_extension_download.py 的工具级用例。

覆盖(brief 必测):
- 无 apikey → 401(中间件挡)。
- 带合法 apikey → 200,返回体键与 get_extension_download 工具全等
  {download_url, version, apikey_hint, install_steps, server_time};
  download_url 含 /downloads/extension.zip?t= 的 cache-buster;install_steps 非空 list;
  server_time 可被 datetime.fromisoformat 解析;apikey_hint 不含任何明文 key。
"""

from datetime import datetime

from app import __version__
from tests.rest_helpers import ADMIN_KEY, bearer, rest_client as isolated_client


async def test_extension_requires_apikey(tmp_path, monkeypatch):
    """无 apikey GET /api/extension → 401(中间件挡,不进业务层)。"""
    async with isolated_client(tmp_path, monkeypatch) as c:
        r = await c.get("/api/extension")
        assert r.status_code == 401


async def test_extension_returns_download_info(tmp_path, monkeypatch):
    """admin apikey GET /api/extension → 200,键与 get_extension_download 工具全等。"""
    async with isolated_client(tmp_path, monkeypatch, root_key=ADMIN_KEY) as c:
        r = await c.get("/api/extension", headers=bearer(ADMIN_KEY))
        assert r.status_code == 200, r.text
        data = r.json()
        assert set(data.keys()) == {
            "download_url", "version", "apikey_hint", "install_steps", "server_time",
        }
        assert "/downloads/extension.zip?t=" in data["download_url"]
        assert data["version"] == __version__
        assert isinstance(data["install_steps"], list) and len(data["install_steps"]) > 0
        assert all(isinstance(s, str) and s for s in data["install_steps"])
        # server_time 可被 fromisoformat 解析(不抛异常即通过)。
        datetime.fromisoformat(data["server_time"])
        # apikey_hint 是引导语而非明文 key,绝不泄露 ADMIN_KEY。
        assert ADMIN_KEY not in data["apikey_hint"]
