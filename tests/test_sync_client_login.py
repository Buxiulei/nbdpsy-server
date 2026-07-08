"""check_login_once 的 error(基础设施失败)vs invalid(未登录)分流单测(不起真浏览器)。

D4:把"浏览器启动失败 / 页面超时 / 异常"归为 status='error'(带 reason 说明),把
"页面正常加载但检测到未登录"才归为 'invalid'。核心价值:上层据此保留好号状态,不把
浏览器起不来误当成 cookie 失效、白白让真人重登。

隔离手法:monkeypatch SyncClient 的 start/check_login/stop,完全不触发真实 Camoufox。
"""

from app.browser import sync_client
from app.browser.sync_client import SyncClient

_COOKIES = [{"name": "a", "value": "x"}]


def test_check_login_once_start_failure_is_error(monkeypatch):
    """start 返回 success=False(浏览器起不来)→ status='error',带浏览器启动失败 reason。"""
    monkeypatch.setattr(
        SyncClient, "start", lambda self: {"success": False, "error": "boom"}
    )
    # check_login 不应被调用到(start 就失败了),给个会暴露误调的返回
    monkeypatch.setattr(
        SyncClient, "check_login", lambda self: {"status": "valid", "user_info": None}
    )
    monkeypatch.setattr(SyncClient, "stop", lambda self: None)

    res = sync_client.check_login_once(1, _COOKIES)
    assert res["status"] == "error"
    assert res["user_info"] is None
    assert "浏览器启动失败" in res["reason"]
    assert "boom" in res["reason"]


def test_check_login_once_exception_is_error(monkeypatch):
    """start 抛异常 → status='error',带浏览器异常 reason(不被吞成 invalid)。"""

    def boom_start(self):
        raise RuntimeError("kaboom")

    monkeypatch.setattr(SyncClient, "start", boom_start)
    monkeypatch.setattr(SyncClient, "stop", lambda self: None)

    res = sync_client.check_login_once(1, _COOKIES)
    assert res["status"] == "error"
    assert res["user_info"] is None
    assert "浏览器异常" in res["reason"]
    assert "kaboom" in res["reason"]


def test_check_login_once_not_logged_in_is_invalid(monkeypatch):
    """start 成功但页面未登录 → status='invalid'(cookie 真失效),不带 error reason。"""
    monkeypatch.setattr(
        SyncClient, "start", lambda self: {"success": True, "logged_in": False}
    )
    monkeypatch.setattr(
        SyncClient, "check_login", lambda self: {"status": "invalid", "user_info": None}
    )
    monkeypatch.setattr(SyncClient, "stop", lambda self: None)

    res = sync_client.check_login_once(1, _COOKIES)
    assert res["status"] == "invalid"
    assert res["user_info"] is None
    assert "reason" not in res  # invalid 来自 check_login,不带基础设施 reason


def test_check_login_once_valid_passthrough(monkeypatch):
    """start 成功且已登录 → 透传 check_login 的 valid + user_info。"""
    monkeypatch.setattr(
        SyncClient, "start", lambda self: {"success": True, "logged_in": True}
    )
    monkeypatch.setattr(
        SyncClient,
        "check_login",
        lambda self: {"status": "valid", "user_info": {"nickname": "小蓝"}},
    )
    monkeypatch.setattr(SyncClient, "stop", lambda self: None)

    res = sync_client.check_login_once(1, _COOKIES)
    assert res["status"] == "valid"
    assert res["user_info"]["nickname"] == "小蓝"
