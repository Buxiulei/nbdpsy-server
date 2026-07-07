"""chrome-extension/manifest.json 静态校验。

JS 逻辑（service worker / popup / content script）无浏览器难做单测,靠 manifest 的
静态断言守住移植后的核心能力契约,浏览器内行为放到 P5 手动加载验证。断言:
- MV3(manifest_version == 3)。
- permissions 至少含 cookies 与 webRequest(采集 cookie + 拦 Set-Cookie 补抓 httpOnly 的前提)。
- host_permissions 覆盖小红书域(采集/注入 cookie 的前提)。
"""

import json
from pathlib import Path

# 仓库根 = tests/ 的上一级;manifest 在 chrome-extension/manifest.json。
_MANIFEST_PATH = (
    Path(__file__).resolve().parent.parent / "chrome-extension" / "manifest.json"
)


def _load_manifest() -> dict:
    """读取并解析扩展 manifest.json。"""
    return json.loads(_MANIFEST_PATH.read_text(encoding="utf-8"))


def test_manifest_exists():
    """manifest.json 必须存在(移植前该文件不存在 → 本用例先红)。"""
    assert _MANIFEST_PATH.is_file(), f"缺少 {_MANIFEST_PATH}"


def test_manifest_is_mv3():
    """必须是 Manifest V3。"""
    assert _load_manifest().get("manifest_version") == 3


def test_permissions_have_cookies_and_webrequest():
    """permissions 必须含 cookies(采集 cookie)与 webRequest(拦 Set-Cookie 补抓 httpOnly)。"""
    permissions = _load_manifest().get("permissions", [])
    assert "cookies" in permissions
    assert "webRequest" in permissions


def test_host_permissions_include_xiaohongshu():
    """host_permissions 必须覆盖小红书域(否则无法跨站采集/操作 cookie)。"""
    host_permissions = _load_manifest().get("host_permissions", [])
    assert any(
        "xiaohongshu.com" in pattern for pattern in host_permissions
    ), f"host_permissions 未覆盖 xiaohongshu.com: {host_permissions}"
