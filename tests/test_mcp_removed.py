"""MCP 已删除的回归钉:/mcp 不再存在,fastmcp 不再被 app 引用。"""

import subprocess
import sys

from tests.rest_helpers import ADMIN_KEY, bearer, rest_client


async def test_mcp_endpoint_gone(tmp_path, monkeypatch):
    async with rest_client(tmp_path, monkeypatch) as client:
        r = await client.post("/mcp/", headers=bearer(ADMIN_KEY), json={})
        assert r.status_code == 404


def test_no_fastmcp_import_in_app():
    """app/ 源码里不允许再出现 fastmcp 引用(文档/历史除外)。"""
    out = subprocess.run(
        ["grep", "-ri", "fastmcp", "app/"], capture_output=True, text=True
    )
    assert out.stdout == "", f"app/ 仍引用 fastmcp:\n{out.stdout}"
