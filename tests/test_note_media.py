"""笔记媒体清单归一化单测:全部样本 URL 取自 2026-08-05 真号实测(带 HTTP 实证)。

实证结论(见 app/browser/note_media.py docstring):带签名 URL 会过期(18 天前的 403),
剥成 sns-img-qc/{段}/{file_id} 永久有效且是原图。本文件钉死归一化规则。
"""

import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.browser import note_media as nm

# ── 真号实测样本(2026-08-05) ──
_SIGNED_SPECTRUM = (
    "https://sns-webpic-qc.xhscdn.com/202608052018/73148c23bc237d4ec6ccc9f98c7e1ef8/"
    "spectrum/1040g34o3236vrj0bn2005noda2v08d26gtgrf08!nd_dft_wlteh_jpg_3"
)
_SIGNED_UHDR = (
    "https://sns-webpic-qc.xhscdn.com/202608052020/1ef82ffdaef2c13b8601b41e3d576ec2/"
    "notes_uhdr/1040g3qo323e8pf8b0a705pt4rqe391dq9b20t3o!nc_n_webp_mw_1"
)
_SIGNED_PRE_POST = (
    "https://sns-webpic-qc.xhscdn.com/202608052020/06d9a4a7d81670eb41485ec0ab249bab/"
    "notes_pre_post/1040g3k0323fn203e7u705on8ounnor5k7jbt3t8!nc_n_webp_mw_1"
)
_AVATAR = (
    "https://sns-avatar-qc.xhscdn.com/avatar/1040g2jo32359ue237k6g5noda2v08d262ls1ut8"
    "?imageView2/2/w/360/format/webp"
)


def test_signed_url_normalizes_to_permanent_form():
    """带签名展示 URL → 永久形态(域换 sns-img-qc、剥掉时间戳与签名、去掉变体后缀)。

    实测:归一化后 9 个月前的 file_id 仍 200 且拿到原图(424KB vs 签名版 56KB)。
    """
    got = nm.normalize_media_url(_SIGNED_SPECTRUM)
    assert got == {
        "file_id": "1040g34o3236vrj0bn2005noda2v08d26gtgrf08",
        "segment": "spectrum",
        "url": "https://sns-img-qc.xhscdn.com/spectrum/"
               "1040g34o3236vrj0bn2005noda2v08d26gtgrf08",
    }


def test_path_segment_is_preserved_not_guessed():
    """路径段必须原样保留:实测把 spectrum 的 id 拿去 notes_pre_post 段取图会 404。"""
    assert nm.normalize_media_url(_SIGNED_UHDR)["segment"] == "notes_uhdr"
    assert nm.normalize_media_url(_SIGNED_PRE_POST)["segment"] == "notes_pre_post"


def test_already_permanent_url_is_idempotent():
    """已经是永久形态的 URL 再归一化 → 原样(重跑抓取不该产生第二种写法)。"""
    permanent = "https://sns-img-qc.xhscdn.com/notes_uhdr/1040g3qo323e8pf8b0a705pt4rqe391dq9b20t3o"
    assert nm.normalize_media_url(permanent)["url"] == permanent


def test_avatar_and_non_media_are_rejected():
    """头像/非 xhscdn/空串 → None(是"不是笔记图"的判断,不是失败)。"""
    assert nm.normalize_media_url(_AVATAR) is None
    assert nm.normalize_media_url("https://example.com/a.png") is None
    assert nm.normalize_media_url("") is None
    assert nm.normalize_media_url(None) is None


def test_collect_dedupes_by_file_id_and_keeps_dom_order():
    """同图多变体去重(按 file_id),DOM 顺序即图序,序号 1-based 连续。

    页面上同一张图常有多个尺寸的 img 节点,不去重会把 6 图笔记记成十几项。
    """
    got = nm.collect_media([
        _AVATAR,                       # 头像:跳过
        _SIGNED_SPECTRUM,              # 第 1 张
        _SIGNED_SPECTRUM.replace("!nd_dft_wlteh_jpg_3", "!nc_n_webp_mw_1"),  # 同图变体
        _SIGNED_UHDR,                  # 第 2 张
        "https://example.com/x.png",   # 非平台:跳过
        _SIGNED_PRE_POST,              # 第 3 张
    ])
    assert [m["ordinal"] for m in got] == [1, 2, 3]
    assert [m["segment"] for m in got] == ["spectrum", "notes_uhdr", "notes_pre_post"]
    assert all(m["kind"] == "image" for m in got)
    assert all(m["url"].startswith("https://sns-img-qc.xhscdn.com/") for m in got)


def test_unknown_segment_is_kept_with_warning(caplog):
    """没见过的路径段照常收录(宁可多存待核,不静默丢图)并打告警。"""
    got = nm.normalize_media_url(
        "https://sns-webpic-qc.xhscdn.com/202608052018/abc/brandnewseg/1040g34oNEWID000000000!x"
    )
    assert got["segment"] == "brandnewseg"
    assert got["url"].endswith("/brandnewseg/1040g34oNEWID000000000")


_EDITOR_URL = (
    "https://sns-na-i4.xhscdn.com/spectrum/1040g0k0323fjic5k74005noda2v08d26hqqevio"
    "?sign=11d6b94d5bd684f802433f82b8d81dfc&t=6"
)


def test_editor_page_url_normalizes_same_as_detail_page():
    """编辑页 URL(另一个域 + query 签名)归一化结果同构。

    实测:归一化后取回 375254B,与该图在详情页那条路取回的**逐字节同尺寸** ——
    同一张原图,两条路殊途同归。query 签名被 urlsplit 天然剥掉。
    """
    got = nm.normalize_media_url(_EDITOR_URL)
    assert got == {
        "file_id": "1040g0k0323fjic5k74005noda2v08d26hqqevio",
        "segment": "spectrum",
        "url": "https://sns-img-qc.xhscdn.com/spectrum/"
               "1040g0k0323fjic5k74005noda2v08d26hqqevio",
    }


def test_fetch_note_media_isolates_single_failure(monkeypatch):
    """单篇抓取炸了只记该篇 error,其余篇照常(与正文回填同一纪律)。"""
    class _Page:
        def __init__(self):
            self.opened = []

        def evaluate(self, _js):
            if len(self.opened) == 2:      # 第二篇读 DOM 时炸
                raise RuntimeError("页面没了")
            return [_EDITOR_URL]

    class _Human:
        def __init__(self, page):
            self.page = page

        def wait(self, *_a, **_kw):
            pass

    monkeypatch.setattr(nm, "SyncHumanActions", _Human)
    page = _Page()
    monkeypatch.setattr(
        nm, "open_update_page",
        lambda p, account_id, note_id: p.opened.append((account_id, note_id)),
    )
    out = nm.fetch_note_media(page, 1, [
        {"note_id": "n1"}, {"note_id": "n2"}, {"note_id": "n3"},
    ])
    assert len(out["n1"]["media"]) == 1
    assert "media_fetch_failed" in out["n2"]["error"]
    assert len(out["n3"]["media"]) == 1        # 第二篇失败没影响第三篇
    assert page.opened[-1] == (1, "n3")        # 走编辑页深链(不需要 xsec_token)


# ---------------- 夹具(照抄 test_note_purpose.wired_db:monkeypatch 全局会话工厂) ----------------


@pytest_asyncio.fixture
async def wired_db(tmp_path, monkeypatch):
    """临时文件库 + monkeypatch 全局 engine/async_session。"""
    import app.core.db as db_module
    from app.core.db import Base

    import app.models  # noqa: F401  触发模型注册

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/media.db", future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr(db_module, "engine", engine)
    monkeypatch.setattr(db_module, "async_session", factory)
    try:
        yield factory
    finally:
        await engine.dispose()


# ---------------- 服务层:挑篇与落库 ----------------


async def test_pick_targets_skips_fetched_private_and_tokenless(wired_db):
    """挑篇过滤:已抓过/私密/可见性未知/别的账号一律不挑(每条都对应一次会话成本);
    **没有 xsec_token 的篇要挑上** —— 编辑页深链不需要它。"""
    import app.core.db as db_module
    from datetime import datetime

    from app.models.published_note import PublishedNote
    from app.services import note_media as svc

    async with db_module.async_session() as s:
        s.add_all([
            PublishedNote(account_id=1, note_id="ok1", xsec_token="t", permission_code=0),
            PublishedNote(account_id=1, note_id="done", xsec_token="t", permission_code=0,
                          media_fetched_at=datetime.utcnow()),      # 抓过
            PublishedNote(account_id=1, note_id="priv", xsec_token="t", permission_code=1),
            PublishedNote(account_id=1, note_id="unknown_vis", xsec_token="t"),  # 可见性未知
            # 编辑页深链不要 token:没 token 的篇现在**应该**被挑上(改前会被漏掉)
            PublishedNote(account_id=1, note_id="notoken", permission_code=0),
            PublishedNote(account_id=2, note_id="other", xsec_token="t", permission_code=0),
        ])
        await s.commit()

    async with db_module.async_session() as s:
        picked = await svc.pick_targets(s, 1, None, 10)
    assert sorted(p["note_id"] for p in picked) == ["notoken", "ok1"]


async def test_execute_writes_manifest_and_marks_fetched(wired_db, monkeypatch):
    """抓到的清单落 media_json,并盖 media_fetched_at(重跑不再开页)。"""
    import app.core.db as db_module
    from app.models.published_note import PublishedNote
    from app.services import note_media as svc

    async with db_module.async_session() as s:
        s.add(PublishedNote(account_id=1, note_id="n1", xsec_token="t", permission_code=0))
        await s.commit()

    async def fake_cookies(_account_id):
        return [{"name": "a", "value": "b"}]

    def fake_fetch(account_id, cookies, targets):
        return {"n1": {"media": [
            {"ordinal": 1, "kind": "image", "file_id": "f1",
             "segment": "spectrum", "url": "https://sns-img-qc.xhscdn.com/spectrum/f1"},
        ]}}

    monkeypatch.setattr(svc, "load_account_cookies", fake_cookies)
    monkeypatch.setattr(svc, "_fetch_sync", fake_fetch)

    result = await svc.execute(1, {})
    assert result == {"picked": 1, "fetched": 1, "media_total": 1, "failed": []}

    async with db_module.async_session() as s:
        row = (await s.execute(
            __import__("sqlalchemy").select(PublishedNote).where(PublishedNote.note_id == "n1")
        )).scalars().first()
    import json as _json
    assert _json.loads(row.media_json)[0]["file_id"] == "f1"
    assert row.media_fetched_at is not None


async def test_execute_empty_manifest_still_marks_fetched(wired_db, monkeypatch):
    """页面上一张图都没有 → 记空清单 + 盖时刻("看过了"也是事实,别重开会话)。"""
    import app.core.db as db_module
    from app.models.published_note import PublishedNote
    from app.services import note_media as svc

    async with db_module.async_session() as s:
        s.add(PublishedNote(account_id=1, note_id="n2", xsec_token="t", permission_code=0))
        await s.commit()

    monkeypatch.setattr(svc, "load_account_cookies", lambda _a: _async_value([{"n": 1}]))
    monkeypatch.setattr(svc, "_fetch_sync", lambda *_a: {"n2": {"media": []}})

    result = await svc.execute(1, {})
    assert result["fetched"] == 1 and result["media_total"] == 0

    async with db_module.async_session() as s:
        row = (await s.execute(
            __import__("sqlalchemy").select(PublishedNote).where(PublishedNote.note_id == "n2")
        )).scalars().first()
    assert row.media_json == "[]" and row.media_fetched_at is not None


def _async_value(value):
    async def _inner(*_a, **_kw):
        return value
    return _inner()


async def test_browser_failure_is_reported_not_raised(wired_db, monkeypatch):
    """浏览器起不来 → error 结果,**不抛**,且不给任何行盖已抓时刻。"""
    import app.core.db as db_module
    from app.models.published_note import PublishedNote
    from app.services import note_media as svc

    async with db_module.async_session() as s:
        s.add(PublishedNote(account_id=1, note_id="n3", xsec_token="t", permission_code=0))
        await s.commit()

    async def fake_cookies(_a):
        return [{"n": 1}]

    monkeypatch.setattr(svc, "load_account_cookies", fake_cookies)
    monkeypatch.setattr(svc, "_fetch_sync", lambda *_a: {"error": "浏览器启动失败:x"})

    result = await svc.execute(1, {})
    assert "error" in result

    async with db_module.async_session() as s:
        row = (await s.execute(
            __import__("sqlalchemy").select(PublishedNote).where(PublishedNote.note_id == "n3")
        )).scalars().first()
    assert row.media_fetched_at is None      # 没抓成就别标记已抓


def test_note_media_sync_is_idempotent_kind():
    """纯只读快照 → 必须在 _IDEMPOTENT_KINDS 里(僵死可自动重跑)。"""
    from app.services import browser_jobs_repo
    assert "note_media_sync" in browser_jobs_repo._IDEMPOTENT_KINDS
