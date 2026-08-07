"""播客合集创建服务层:``execute()`` 的契约(不起浏览器)。

契约的三条硬要求(与 note_visibility / draft_clean 同源):
- 成功返回结果 dict、失败返回 ``{"error": ...}``,**绝不抛出**(抛了台账会悬挂);
- 浏览器层的 ``status != done`` 必须翻译成 ``error`` 键 —— 否则一次失败的创建会以
  ``done`` 收尾(静默假成功,这条产品线最要命的失败形态);
- 入参缺失在跑浏览器**之前**就拒。
"""

import pytest

from app.services import podcast_collection


@pytest.fixture()
def no_browser(monkeypatch):
    """把 cookie 读取与同步执行体换成替身,返回一个可写的 holder。"""
    holder = {"result": {"status": "done", "name": "X", "collection_id": None},
              "calls": []}

    async def _cookies(account_id):
        return [{"name": "a", "value": "b"}]

    monkeypatch.setattr(podcast_collection, "load_account_cookies", _cookies)

    def _sync(account_id, cookies, name, description, cover_path):
        holder["calls"].append((account_id, name, description, cover_path))
        if isinstance(holder["result"], Exception):
            raise holder["result"]
        return holder["result"]

    monkeypatch.setattr(podcast_collection, "_create_sync", _sync)
    return holder


async def test_execute_success_passes_through(no_browser):
    """浏览器层 done → 原样返回(含 collection_id 为 None 的情形)。"""
    no_browser["result"] = {"status": "done", "name": "心理急救包",
                            "collection_id": None, "confirmed_by": "name_in_list"}
    out = await podcast_collection.execute(
        7, {"name": "心理急救包", "description": "每周一集", "cover": "/tmp/c.png"}
    )
    assert out["status"] == "done" and out["collection_id"] is None
    assert no_browser["calls"] == [(7, "心理急救包", "每周一集", "/tmp/c.png")]


async def test_browser_error_becomes_error_key(no_browser):
    """浏览器层 error → 翻译成 ``{"error": reason}``,**绝不以 done 收尾**。

    台账的终态判据就是有没有 "error" 键;不翻译的话一次失败的创建会被记成成功,
    调用方拿到 done 却发现平台上没有这个合集 —— 静默假成功。
    """
    no_browser["result"] = {"status": "error",
                            "reason": "create_button_never_enabled: …",
                            "observed": {"create_button": {"enabled": False}}}
    out = await podcast_collection.execute(7, {"name": "X", "cover": "/tmp/c.png"})
    assert out["error"].startswith("create_button_never_enabled")
    assert out["observed"]["create_button"]["enabled"] is False
    assert "status" in out, "取证与原始 status 要一起带出来,便于排查"


async def test_exception_never_escapes(no_browser):
    """执行体抛异常 → 收敛成 error,不往外抛(抛了台账会悬挂在 running)。"""
    no_browser["result"] = RuntimeError("camoufox 挂了")
    out = await podcast_collection.execute(7, {"name": "X", "cover": "/tmp/c.png"})
    assert "camoufox 挂了" in out["error"]


async def test_missing_name_or_cover_rejected_before_browser(no_browser):
    """名称/封面缺失 → 直接 error,**一次浏览器都不起**(两者都是平台必填项)。"""
    assert "name" in (await podcast_collection.execute(7, {"cover": "/tmp/c.png"}))["error"]
    assert "cover" in (await podcast_collection.execute(7, {"name": "X"}))["error"]
    assert no_browser["calls"] == []


async def test_no_cookie_rejected(monkeypatch):
    """账号没 cookie → error(起了浏览器也进不去创作中心)。"""
    async def _cookies(account_id):
        return []

    monkeypatch.setattr(podcast_collection, "load_account_cookies", _cookies)
    out = await podcast_collection.execute(7, {"name": "X", "cover": "/tmp/c.png"})
    assert "cookie" in out["error"]


def test_kind_is_not_idempotent():
    """``podcast_collection_create`` **不能**进幂等 kind 表:僵死自动重跑会建出重复合集。"""
    from app.services import browser_jobs_repo

    assert podcast_collection.KIND not in browser_jobs_repo._IDEMPOTENT_KINDS
