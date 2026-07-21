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


# ── check_login 官方 API 地面真值优先(修 DOM 启发式假阳性)单测 ──
# 背景:DETECT_LOGIN_JS 在登出的 explore 页仍会因笔记内容渲染而误判"已登录",
# 导致过期 cookie 被标 valid、发布静默失败。check_login 现以 API 判定为准。

def _client():
    return SyncClient(1, _COOKIES)


def test_check_login_api_expired_overrides_dom(monkeypatch):
    """API 明确"登录已过期" → invalid，即便 DOM 启发式(假阳性)说已登录。"""
    cli = _client()
    monkeypatch.setattr(SyncClient, "_is_captcha", lambda self: False)
    monkeypatch.setattr(SyncClient, "_api_login_status", lambda self: False)
    # DOM 假阳性:说登录了,但必须被 API 的 False 否决
    monkeypatch.setattr(SyncClient, "_detect_login", lambda self: {"is_logged_in": True})
    assert cli.check_login()["status"] == "invalid"


def test_check_login_api_valid_overrides_dom_false_negative(monkeypatch):
    """API 明确已登录 → valid，即便 DOM 假阴性说未登录(不被误杀)。"""
    cli = _client()
    monkeypatch.setattr(SyncClient, "_is_captcha", lambda self: False)
    monkeypatch.setattr(SyncClient, "_api_login_status", lambda self: True)
    monkeypatch.setattr(SyncClient, "_detect_login", lambda self: {"is_logged_in": False, "profile_url": None})
    monkeypatch.setattr(SyncClient, "_get_user_info", lambda self, url: {"nickname": "x"})
    assert cli.check_login()["status"] == "valid"


def test_check_login_api_unreachable_falls_back_to_dom_invalid(monkeypatch):
    """API 不可达(None) → 降级 DOM;DOM 说未登录 → invalid。"""
    cli = _client()
    monkeypatch.setattr(SyncClient, "_is_captcha", lambda self: False)
    monkeypatch.setattr(SyncClient, "_api_login_status", lambda self: None)
    monkeypatch.setattr(SyncClient, "_detect_login", lambda self: {"is_logged_in": False})
    assert cli.check_login()["status"] == "invalid"


def test_check_login_api_unreachable_falls_back_to_dom_valid(monkeypatch):
    """API 不可达(None) → 降级 DOM;DOM 说已登录 → valid(保留原行为不误杀)。"""
    cli = _client()
    monkeypatch.setattr(SyncClient, "_is_captcha", lambda self: False)
    monkeypatch.setattr(SyncClient, "_api_login_status", lambda self: None)
    monkeypatch.setattr(SyncClient, "_detect_login", lambda self: {"is_logged_in": True, "profile_url": "u"})
    monkeypatch.setattr(SyncClient, "_get_user_info", lambda self, url: {"nickname": "y"})
    assert cli.check_login()["status"] == "valid"
