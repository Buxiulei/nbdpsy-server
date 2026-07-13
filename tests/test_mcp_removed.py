"""MCP 已删除的回归钉:/mcp 不再存在,fastmcp 不再被 app 引用。"""

import subprocess
from pathlib import Path

from tests.rest_helpers import ADMIN_KEY, bearer, rest_client

# 仓库根下的 app 目录(从本测试文件位置推导绝对路径,不依赖运行时 cwd,
# 避免 cwd 非仓库根时 grep 打空目录→stdout 空→断言假性通过)。
_APP_DIR = Path(__file__).resolve().parent.parent / "app"


async def test_mcp_endpoint_gone(tmp_path, monkeypatch):
    async with rest_client(tmp_path, monkeypatch) as client:
        r = await client.post("/mcp/", headers=bearer(ADMIN_KEY), json={})
        assert r.status_code == 404


def test_no_fastmcp_import_in_app():
    """app/ 源码里不允许再出现 fastmcp 引用(文档/历史除外)。"""
    assert _APP_DIR.is_dir(), f"app 目录不存在,路径推导错误:{_APP_DIR}"
    out = subprocess.run(
        ["grep", "-ri", "fastmcp", str(_APP_DIR)],
        capture_output=True,
        text=True,
    )
    assert out.stdout == "", f"app/ 仍引用 fastmcp:\n{out.stdout}"
