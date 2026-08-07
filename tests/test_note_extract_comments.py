"""评论抓取服务层:入参把关 / 无 cookie 不开会话 / 撞墙留痕 / 结果并进缓存。

浏览器与 DB 全部 mock —— 这里测的是**编排与收敛纪律**(异常绝不上抛、撞墙落 risk_events、
抓完并进 24h 缓存),不是浏览器行为本身。
"""

from datetime import datetime, timezone

import pytest

from app.services import note_extract, note_extract_comments as nec

_NOTE_ID = "6a4f50d0000000001c025535"
_PAYLOAD = {
    "note_id": _NOTE_ID,
    "note_url": f"https://www.xiaohongshu.com/discovery/item/{_NOTE_ID}?xsec_token=T",
    "max_count": 20,
}


@pytest.fixture(autouse=True)
def isolated_data_dir(tmp_path, monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "DATA_DIR", str(tmp_path))


async def test_missing_payload_fields_never_opens_browser(monkeypatch):
    opened = []
    monkeypatch.setattr(nec, "_fetch_sync", lambda *a: opened.append(1))
    result = await nec.execute(9, {"note_id": _NOTE_ID})
    assert "error" in result
    assert opened == [], "缺 note_url 就该直接判入参错误,不白起一次会话"


async def test_no_cookie_returns_error_without_session(monkeypatch):
    opened = []
    monkeypatch.setattr(nec, "load_account_cookies", _async_return([]))
    monkeypatch.setattr(nec, "_fetch_sync", lambda *a: opened.append(1))
    result = await nec.execute(9, _PAYLOAD)
    assert "无可用 cookie" in result["error"]
    assert opened == []


async def test_browser_exception_converges_to_error_not_raise(monkeypatch):
    monkeypatch.setattr(nec, "load_account_cookies", _async_return([{"name": "a"}]))
    monkeypatch.setattr(nec, "_fetch_sync", _boom)
    result = await nec.execute(9, _PAYLOAD)
    assert "error" in result, "异常必须收敛成结果,绝不上抛(否则台账悬挂)"


async def test_success_merges_into_cache(monkeypatch):
    # 先放一份内容缓存件(模拟同一篇刚被纯 HTTP 提取过)
    payload = {
        "note_id": _NOTE_ID, "images": [], "comments": None, "comments_complete": False,
        "unavailable": {"comments": "要浏览器会话"},
        "source": {"fetched_at": datetime.now(timezone.utc).isoformat(),
                   "browser_session_used": False},
    }
    note_extract.cache_store(_NOTE_ID, payload)

    monkeypatch.setattr(nec, "load_account_cookies", _async_return([{"name": "a"}]))
    monkeypatch.setattr(nec, "_fetch_sync", lambda *a: {
        "comments": [{"comment_id": "c1", "text": "扎心"}],
        "complete": True, "stop_reason": "no_new_after_scroll", "rounds": 2,
    })
    result = await nec.execute(9, _PAYLOAD)
    assert result["count"] == 1
    assert result["complete"] is True

    cached = note_extract.cache_load(_NOTE_ID)
    assert cached["comments"][0]["text"] == "扎心"
    assert cached["comments_complete"] is True
    # 并入后那条"拿不到评论"的说明必须消失,且如实标注消耗过会话
    assert "comments" not in cached["unavailable"]
    assert cached["comments_source"] == "browser_session"
    assert cached["source"]["browser_session_used"] is True


async def test_wall_is_recorded_to_risk_events(monkeypatch):
    recorded = []
    monkeypatch.setattr(nec, "load_account_cookies", _async_return([{"name": "a"}]))
    monkeypatch.setattr(nec, "_fetch_sync", lambda *a: {
        "error": "wall_scan_qr: 撞墙",
        "partial_comments": [],
        "wall": {"wall_type": "scan_qr", "target_url": "u", "landed_url": "captcha", "page_text": "扫码"},
    })

    async def _record(_factory, account_id, wall, source):
        recorded.append((account_id, wall["wall_type"], source))
        return True

    monkeypatch.setattr(nec.risk_events, "record_wall", _record)
    result = await nec.execute(9, _PAYLOAD)
    assert "wall_scan_qr" in result["error"]
    assert recorded == [(9, "scan_qr", nec.JOB_KIND)]


async def test_merge_skipped_when_no_cache_file(monkeypatch):
    """没有内容缓存件时并入应静默跳过,不炸(下次提取会重建)。"""
    monkeypatch.setattr(nec, "load_account_cookies", _async_return([{"name": "a"}]))
    monkeypatch.setattr(nec, "_fetch_sync", lambda *a: {
        "comments": [], "complete": True, "stop_reason": "empty", "rounds": 0,
    })
    result = await nec.execute(9, _PAYLOAD)
    assert result["count"] == 0
    assert note_extract.cache_load(_NOTE_ID) is None


def test_kind_is_idempotent():
    """纯只读 + 按 note_id 覆盖写缓存 → 僵死可自动重跑。"""
    from app.services import browser_jobs_repo

    assert nec.JOB_KIND in browser_jobs_repo._IDEMPOTENT_KINDS


def test_worker_knows_this_kind():
    """NBDPSY_ROLE=api 时执行方是 worker 子进程 —— 派发表漏登记就永远 queued。"""
    from app import account_worker

    assert account_worker._resolve_execute(nec.JOB_KIND) is not None


# ---- 小工具 ----


def _async_return(value):
    async def _inner(*_a, **_kw):
        return value

    return _inner


def _boom(*_a, **_kw):
    raise RuntimeError("camoufox 起不来")
