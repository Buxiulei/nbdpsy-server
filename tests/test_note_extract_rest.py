"""POST /api/notes/extract + GET /api/note-extracts/{job_id} 端点契约。

外站 HTTP 全部 mock(真夹具页面回放),**测试绝不真的去打小红书**。
"""

import json
from pathlib import Path

import pytest

import app.core.db as db_module
from app.services import note_extract, note_extract_comments, operator_service
from tests.rest_helpers import (
    ADMIN_KEY, bearer, make_operator, rest_client, seed_account,
)

_FIXTURE = Path(__file__).parent / "fixtures" / "note_extract" / "sample_note_page.html"
_NOTE_ID = "6a4f50d0000000001c025535"
_SHORT = "http://xhslink.cn/o/6uGYemZFqDN"
_FINAL = (
    "https://www.xiaohongshu.com/discovery/item/6a4f50d0000000001c025535"
    "?noteAttributes=goods&xsec_token=T%3D&xsec_source=app_share"
)
_COOKIES = [{"name": "a1", "value": "x", "domain": ".xiaohongshu.com"}]

def _png_bytes() -> bytes:
    """真 PNG 字节:图床落盘要过 Pillow 真解,手搓的十六进制串过不了。"""
    from io import BytesIO

    from PIL import Image

    buf = BytesIO()
    Image.new("RGB", (8, 8), (200, 180, 120)).save(buf, format="PNG")
    return buf.getvalue()


_PNG = _png_bytes()


class _FakeResponse:
    def __init__(self, url, status_code=200, text="", content=b""):
        self.url = url
        self.status_code = status_code
        self.text = text
        self.content = content


class _FakeClient:
    """假 httpx:短链跟跳 → 详情页 → CDN 取图,全部离线。"""

    def __init__(self, page_status=200, image_status=200):
        self.page_status = page_status
        self.image_status = image_status
        self.gets: list[str] = []

    async def get(self, url, **kw):
        self.gets.append(url)
        if "xhslink" in url:
            return _FakeResponse(_FINAL)
        if "xhscdn" in url:
            return _FakeResponse(
                url, self.image_status, content=_PNG if self.image_status == 200 else b""
            )
        return _FakeResponse(
            url, self.page_status,
            text=_FIXTURE.read_text(encoding="utf-8") if self.page_status == 200 else "挡了",
        )

    async def aclose(self):
        pass


@pytest.fixture
def fake_http(monkeypatch):
    """把 note_extract 里自建的 httpx.AsyncClient 换成假件。"""
    holder = {}

    def _install(client):
        holder["client"] = client
        import httpx

        monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **kw: client)
        return client

    return _install


@pytest.fixture(autouse=True)
def isolated_data_dir(tmp_path, monkeypatch):
    """缓存与图床都落进临时目录,别污染真 DATA_DIR。"""
    from app.core.config import settings

    monkeypatch.setattr(settings, "DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(settings, "PUBLIC_BASE_URL", "https://mcp.nbdpsy.com")


async def test_extract_returns_full_contract(tmp_path, monkeypatch, fake_http):
    fake_http(_FakeClient())
    async with rest_client(tmp_path, monkeypatch) as client:
        r = await client.post(
            "/api/notes/extract", json={"url": _SHORT}, headers=bearer(ADMIN_KEY)
        )
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["note_id"] == _NOTE_ID
        assert data["note_type"] == "图文"
        assert data["title"] == "停止担心2小时和8公里以外的事｜正念日记"
        assert data["interact"]["liked"] == 1026
        assert data["is_goods_note"] is True
        assert len(data["images"]) == 6
        # 默认 with_images=true → 已代下进自家图床
        assert data["images"][0]["url"].startswith("https://mcp.nbdpsy.com/uploads/")
        assert data["images"][0]["permanent_url"].startswith("https://sns-img-qc.xhscdn.com/")
        # 没要评论:comments 为 null 且 unavailable 里说清为什么
        assert data["comments"] is None
        assert "comments" in data["unavailable"]
        assert data["comments_job"] is None
        # 零会话成本必须明说
        assert data["source"]["browser_session_used"] is False
        assert data["source"]["from_cache"] is False


async def test_no_images_skips_download_but_keeps_keys(tmp_path, monkeypatch, fake_http):
    fake = fake_http(_FakeClient())
    async with rest_client(tmp_path, monkeypatch) as client:
        r = await client.post(
            "/api/notes/extract",
            json={"url": _SHORT, "with_images": False},
            headers=bearer(ADMIN_KEY),
        )
        data = r.json()
        assert len(data["images"]) == 6          # 清单照给
        assert data["images"][0]["url"] is None  # 只是没代下
        assert data["images"][0]["signed_url"]
        assert not [u for u in fake.gets if "xhscdn" in u], "with_images=False 不该下任何图"


async def test_second_call_hits_24h_cache(tmp_path, monkeypatch, fake_http):
    fake = fake_http(_FakeClient())
    async with rest_client(tmp_path, monkeypatch) as client:
        await client.post("/api/notes/extract", json={"url": _SHORT}, headers=bearer(ADMIN_KEY))
        first_gets = len(fake.gets)
        r = await client.post(
            "/api/notes/extract", json={"url": _SHORT}, headers=bearer(ADMIN_KEY)
        )
        data = r.json()
        assert data["source"]["from_cache"] is True
        # 短链仍要跟一次跳(不跟就不知道 note_id),但页面与 6 张图一次都不再拉
        assert len(fake.gets) == first_gets + 1


async def test_refresh_bypasses_cache(tmp_path, monkeypatch, fake_http):
    fake = fake_http(_FakeClient())
    async with rest_client(tmp_path, monkeypatch) as client:
        await client.post("/api/notes/extract", json={"url": _SHORT}, headers=bearer(ADMIN_KEY))
        first = len(fake.gets)
        r = await client.post(
            "/api/notes/extract",
            json={"url": _SHORT, "refresh": True},
            headers=bearer(ADMIN_KEY),
        )
        assert r.json()["source"]["from_cache"] is False
        assert len(fake.gets) > first + 1


async def test_bad_url_is_400_not_500(tmp_path, monkeypatch, fake_http):
    fake_http(_FakeClient())
    async with rest_client(tmp_path, monkeypatch) as client:
        r = await client.post(
            "/api/notes/extract",
            json={"url": "https://www.xiaohongshu.com/"},
            headers=bearer(ADMIN_KEY),
        )
        assert r.status_code == 400
        assert "note_id" in r.json()["error"]


async def test_blocked_page_is_400_with_reason(tmp_path, monkeypatch, fake_http):
    """被风控挡了(返回的不是笔记页)→ 400 + 说明,不是 500。"""
    fake_http(_FakeClient(page_status=200))
    async with rest_client(tmp_path, monkeypatch) as client:
        import httpx

        class _Blocked(_FakeClient):
            async def get(self, url, **kw):
                if "xhslink" in url:
                    return _FakeResponse(_FINAL)
                return _FakeResponse(url, 200, text="<html>安全验证</html>")

        monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **kw: _Blocked())
        r = await client.post(
            "/api/notes/extract", json={"url": _SHORT}, headers=bearer(ADMIN_KEY)
        )
        assert r.status_code == 400
        assert "__INITIAL_STATE__" in r.json()["error"]


async def test_image_download_failure_keeps_order_and_explains(tmp_path, monkeypatch, fake_http):
    """图全下不动:序号不重排,unavailable 里写清楚,原始链接照给。"""
    fake_http(_FakeClient(image_status=403))
    async with rest_client(tmp_path, monkeypatch) as client:
        r = await client.post(
            "/api/notes/extract", json={"url": _SHORT}, headers=bearer(ADMIN_KEY)
        )
        data = r.json()
        assert [i["ordinal"] for i in data["images"]] == [1, 2, 3, 4, 5, 6]
        assert all(i["url"] is None for i in data["images"])
        assert "images" in data["unavailable"]
        assert data["images"][0]["signed_url"]


# ---------------- 评论(会烧会话的那一半) ----------------


async def test_comments_without_account_id_is_422(tmp_path, monkeypatch, fake_http):
    fake_http(_FakeClient())
    async with rest_client(tmp_path, monkeypatch) as client:
        r = await client.post(
            "/api/notes/extract",
            json={"url": _SHORT, "with_comments": 20},
            headers=bearer(ADMIN_KEY),
        )
        assert r.status_code == 422
        assert "account_id" in r.json()["detail"]


async def test_comments_enqueues_browser_job_and_declares_session_cost(
    tmp_path, monkeypatch, fake_http
):
    fake_http(_FakeClient())
    started: list[tuple] = []
    monkeypatch.setattr(
        note_extract_comments, "start_comments",
        lambda account_id, payload: (started.append((account_id, payload)), "job-1")[1],
    )
    async with rest_client(tmp_path, monkeypatch) as client:
        acc = await seed_account("水军号", "u-9", _COOKIES)
        r = await client.post(
            "/api/notes/extract",
            json={"url": _SHORT, "with_comments": 20, "account_id": acc},
            headers=bearer(ADMIN_KEY),
        )
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["comments_job"]["job_id"] == "job-1"
        assert data["comments_job"]["poll"] == "/api/note-extracts/job-1"
        assert "会话额度" in data["comments_job"]["session_cost"]
        # 派给浏览器任务的 payload 必须带上定位与交叉验证所需的东西
        account_id, payload = started[0]
        assert account_id == acc
        assert payload["note_id"] == _NOTE_ID
        assert payload["max_count"] == 20
        assert payload["note_author_user_id"] == "5e08bd510000000001006a28"
        assert payload["expected_total"] == 17  # 平台标称评论数,用来判到底
        # xsec_token 必须留在派下去的 URL 里,否则浏览器也打不开
        assert "xsec_token=" in payload["note_url"]


async def test_comments_rbac_denied_for_unauthorised_account(tmp_path, monkeypatch, fake_http):
    fake_http(_FakeClient())
    async with rest_client(tmp_path, monkeypatch) as client:
        mine = await seed_account("我的号", "u-1", _COOKIES)
        others = await seed_account("别人的号", "u-2", _COOKIES)
        op_key = "op-key-extract"
        op_id = await make_operator(op_key)
        async with db_module.async_session() as s:
            await operator_service.grant_access(s, op_id, mine, None)
            await s.commit()
        r = await client.post(
            "/api/notes/extract",
            json={"url": _SHORT, "with_comments": 5, "account_id": others},
            headers=bearer(op_key),
        )
        assert r.status_code == 403


async def test_comments_account_not_found_is_404(tmp_path, monkeypatch, fake_http):
    fake_http(_FakeClient())
    async with rest_client(tmp_path, monkeypatch) as client:
        r = await client.post(
            "/api/notes/extract",
            json={"url": _SHORT, "with_comments": 5, "account_id": 9999},
            headers=bearer(ADMIN_KEY),
        )
        assert r.status_code == 404


async def test_comments_over_limit_is_422(tmp_path, monkeypatch, fake_http):
    fake_http(_FakeClient())
    async with rest_client(tmp_path, monkeypatch) as client:
        r = await client.post(
            "/api/notes/extract",
            json={"url": _SHORT, "with_comments": 500, "account_id": 1},
            headers=bearer(ADMIN_KEY),
        )
        assert r.status_code == 422


async def test_cached_comments_served_without_new_session(tmp_path, monkeypatch, fake_http):
    """评论已在缓存件里 → 直接回,不再起会话(运营明确要的省会话行为)。"""
    fake_http(_FakeClient())
    called = []
    monkeypatch.setattr(
        note_extract_comments, "start_comments",
        lambda account_id, payload: (called.append(1), "job-x")[1],
    )
    async with rest_client(tmp_path, monkeypatch) as client:
        acc = await seed_account("水军号", "u-9", _COOKIES)
        await client.post("/api/notes/extract", json={"url": _SHORT}, headers=bearer(ADMIN_KEY))
        # 手工把评论并进缓存件(模拟上一次浏览器任务已完成)
        cached = note_extract.cache_load(_NOTE_ID)
        note_extract.merge_comments(cached, [{"text": f"评论{i}"} for i in range(20)], True)
        note_extract.cache_store(_NOTE_ID, cached)

        r = await client.post(
            "/api/notes/extract",
            json={"url": _SHORT, "with_comments": 20, "account_id": acc},
            headers=bearer(ADMIN_KEY),
        )
        data = r.json()
        assert len(data["comments"]) == 20
        assert data["comments_job"] is None
        assert called == [], "缓存里已有评论,不该再起浏览器会话"
        # 评论**数据**来自会话,但**本次调用**一个会话都没烧 —— 两个键分别说清
        assert data["comments_source"] == "browser_session"
        assert data["source"]["browser_session_used"] is False


# ---------------- 轮询 ----------------


async def test_poll_done_returns_comments(tmp_path, monkeypatch, fake_http):
    fake_http(_FakeClient())
    async with rest_client(tmp_path, monkeypatch) as client:
        acc = await seed_account("水军号", "u-9", _COOKIES)
        from app.models.browser_job import BrowserJob

        async with db_module.async_session() as s:
            s.add(BrowserJob(
                id="job-done", kind=note_extract_comments.JOB_KIND, account_id=acc,
                operator_id=1, payload="{}", status="done",
                result=json.dumps({
                    "comments": [{"comment_id": "a", "text": "扎心了", "is_author_reply": False}],
                    "count": 1, "complete": True, "stop_reason": "no_new_after_scroll",
                }, ensure_ascii=False),
            ))
            await s.commit()
        r = await client.get("/api/note-extracts/job-done", headers=bearer(ADMIN_KEY))
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["status"] == "done"
        assert data["comments"][0]["text"] == "扎心了"
        assert data["complete"] is True
        assert data["stop_reason"] == "no_new_after_scroll"


async def test_poll_wall_error_still_hands_back_partial(tmp_path, monkeypatch, fake_http):
    fake_http(_FakeClient())
    async with rest_client(tmp_path, monkeypatch) as client:
        acc = await seed_account("水军号", "u-9", _COOKIES)
        from app.models.browser_job import BrowserJob

        async with db_module.async_session() as s:
            s.add(BrowserJob(
                id="job-wall", kind=note_extract_comments.JOB_KIND, account_id=acc,
                operator_id=1, payload="{}", status="error",
                result=json.dumps({
                    "error": "wall_scan_qr: 账号撞上风控验证墙",
                    "partial_comments": [{"comment_id": "a", "text": "抓到一半"}],
                }, ensure_ascii=False),
            ))
            await s.commit()
        r = await client.get("/api/note-extracts/job-wall", headers=bearer(ADMIN_KEY))
        data = r.json()
        assert data["status"] == "error"
        assert "wall_scan_qr" in data["reason"]
        assert data["comments"][0]["text"] == "抓到一半"
        assert data["complete"] is False


async def test_poll_unknown_job_is_404(tmp_path, monkeypatch, fake_http):
    fake_http(_FakeClient())
    async with rest_client(tmp_path, monkeypatch) as client:
        r = await client.get("/api/note-extracts/nope", headers=bearer(ADMIN_KEY))
        assert r.status_code == 404


# ---------------- 缓存清道夫 ----------------


async def test_extract_sweeps_expired_cache_files(tmp_path, monkeypatch, fake_http):
    """落新缓存时顺手把过期的他人内容从盘上清掉(懒清理,零后台循环)。"""
    from datetime import datetime, timedelta, timezone

    fake_http(_FakeClient())
    async with rest_client(tmp_path, monkeypatch) as client:
        stale = note_extract.cache_path("f" * 24)
        stale.parent.mkdir(parents=True, exist_ok=True)
        stale.write_text(json.dumps({
            "note_id": "f" * 24,
            "source": {
                "fetched_at": (datetime.now(timezone.utc) - timedelta(hours=25)).isoformat()
            },
        }), encoding="utf-8")

        r = await client.post(
            "/api/notes/extract", json={"url": _SHORT}, headers=bearer(ADMIN_KEY)
        )
        assert r.status_code == 200, r.text
        assert not stale.exists(), "过期缓存件该在这次落盘时被顺手清掉"
        assert note_extract.cache_path(_NOTE_ID).is_file(), "刚落的新件不许被误删"


# ---------------- manifest(skill 照它写代码,措辞即接口) ----------------


def _entry(path: str) -> dict:
    from app.http import note_extract_rest

    return next(e for e in note_extract_rest.MANIFEST_ENTRIES if e["path"] == path)


def test_manifest_forbids_resending_after_a_wall():
    """撞墙后重发正是把号往风控深处推的打法,manifest 不能教出这个循环。

    ``notes`` 是 skill 生成代码时唯一的依据:同一段里既说 wall_scan_qr 是常见 error,
    又说"失败可直接重发",skill 就会写出撞墙即重试的循环。
    """
    notes = _entry("/api/note-extracts/{job_id}")["notes"]
    assert "失败可直接重发" not in notes, "这句会直接造出撞墙重试循环"
    assert "禁止重发" in notes and "wall_" in notes
    assert "其余 error 可直接重发" in notes, "别矫枉过正到连普通失败都不敢重发"


def test_manifest_declares_empty_but_expected_state():
    notes = _entry("/api/note-extracts/{job_id}")["notes"]
    assert "empty_but_expected" in notes, "新增的 stop_reason 必须同步进 manifest"


def test_manifest_asks_caller_to_self_rate_limit():
    """纯 HTTP 路径没有会话额度天然限速,manifest 不能鼓励"随便打"。"""
    notes = _entry("/api/notes/extract")["notes"]
    assert "爱调多少次调多少次" not in notes
    assert "控制频率" in notes


async def test_extract_requires_apikey(tmp_path, monkeypatch, fake_http):
    fake_http(_FakeClient())
    async with rest_client(tmp_path, monkeypatch) as client:
        r = await client.post("/api/notes/extract", json={"url": _SHORT})
        assert r.status_code == 401
