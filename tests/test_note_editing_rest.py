"""已发布笔记编辑(标题 / 正文 / 图片增删)的 REST + 服务层契约测试。

设计 docs/design/2026-08-03-note-editing-design.md 3.1 / 3.2 / 3.3,测试矩阵见 7.1。
与 test_note_components_rest.py 同一入口(POST /api/accounts/{id}/note-components),
故沿用同一套隔离手法:rest_client 跑真实 lifespan(隔离库)+ ``NBDPSY_ROLE=api``
(只登记台账不派执行,不起浏览器)。

覆盖:

- 请求体校验矩阵:title 显示长度 >20 **拒绝不截断**、``title=""`` 与 ``None`` 区分、
  content 空串/超长、remove 下标越界·重复·剩余<1、expected 必填条件与总数越界、
  "至少给一个"扩展到八个字段;
- 服务层前置(建 job 前):台账查无此 note_id → 404、note_type 非图文 → 422、
  add_images 坏图 → 422(**都不登记任务**);
- payload:编辑五键齐全 + add_images 已落成本地路径;纯组件请求 payload **一字不变**;
- 轮询视图:编辑新增的结果键(topics_dropped / images_before / images_after /
  ledger_synced / aborted_before_submit)+ applied 新键透传;
- 服务层台账回写 ``write_back_ledger``:只认 applied 里的 True、写回读真值、
  失败不吞(返回 False 而不是抛)。
"""

import json
from datetime import datetime
from pathlib import Path

from sqlalchemy import func, select

import app.core.db as db_module
from app.core.config import settings
from app.models.browser_job import BrowserJob
from app.models.published_note import PublishedNote
from app.services import note_components
from tests.rest_helpers import ADMIN_KEY, bearer, rest_client, seed_account

_COOKIES = [{"name": "a1", "value": "x", "domain": ".xiaohongshu.com"}]
# 1x1 PNG(与 test_images.py 同款),避免测试联网
_PNG_1x1_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)
_IMG = {"b64": _PNG_1x1_B64, "ext": "png"}
_NOTE = "6a6f18c4"


def _api_role(monkeypatch, tmp_path) -> None:
    """role=api(只登记不执行)+ 图片落盘落到 tmp_path,不脏用户 data 目录。"""
    monkeypatch.setenv("NBDPSY_ROLE", "api")
    monkeypatch.setattr(settings, "UPLOAD_DIR", str(tmp_path / "uploads"))


async def _seed_note(account_id: int, note_id: str = _NOTE, note_type="normal") -> None:
    """种一行台账(编辑类请求的前置:得先认得这篇、且是图文)。"""
    now = datetime(2026, 8, 1)
    async with db_module.async_session() as s:
        s.add(
            PublishedNote(
                account_id=account_id, note_id=note_id, title="旧标题",
                note_type=note_type, content_text="旧正文", published_at=now,
                sync_status="linked", first_seen_at=now, last_synced_at=now,
            )
        )
        await s.commit()


async def _job_count() -> int:
    async with db_module.async_session() as s:
        return await s.scalar(select(func.count()).select_from(BrowserJob))


async def _payload_of(job_id: str) -> dict:
    async with db_module.async_session() as s:
        return json.loads((await s.get(BrowserJob, job_id)).payload)


async def _seed_job(job_id: str, account_id: int, status: str, result) -> None:
    async with db_module.async_session() as s:
        s.add(
            BrowserJob(
                id=job_id, kind="note_components", account_id=account_id, operator_id=0,
                payload="{}", status=status,
                result=json.dumps(result, ensure_ascii=False) if result else None,
            )
        )
        await s.commit()


# ---------------- 请求体校验矩阵(设计 3.1) ----------------


async def test_title_too_long_is_rejected_not_truncated(tmp_path, monkeypatch):
    """标题显示长度 >20 → 422,**绝不截断**:编辑场景给的是精确意图,截断=替他改错。"""
    _api_role(monkeypatch, tmp_path)
    async with rest_client(tmp_path, monkeypatch) as c:
        acc = await seed_account("编辑标题号", "uEdTitle", _COOKIES)
        await _seed_note(acc)
        url = f"/api/accounts/{acc}/note-components"

        r = await c.post(
            url, json={"note_id": _NOTE, "title": "标" * 21}, headers=bearer(ADMIN_KEY)
        )
        assert r.status_code == 422, r.text
        assert await _job_count() == 0  # 不登记注定要改错的任务

        # 恰好 20 放行
        r = await c.post(
            url, json={"note_id": _NOTE, "title": "标" * 20}, headers=bearer(ADMIN_KEY)
        )
        assert r.status_code == 202, r.text


async def test_title_emoji_counts_by_display_length(tmp_path, monkeypatch):
    """标题长度按发布链路同款 get_display_length 度量(emoji 额外 +1),不是裸 len。"""
    _api_role(monkeypatch, tmp_path)
    async with rest_client(tmp_path, monkeypatch) as c:
        acc = await seed_account("编辑emoji号", "uEdEmoji", _COOKIES)
        await _seed_note(acc)
        # len=20 但含一个 emoji → 显示长度 21 → 拒绝
        r = await c.post(
            f"/api/accounts/{acc}/note-components",
            json={"note_id": _NOTE, "title": "🏃" + "标" * 19},
            headers=bearer(ADMIN_KEY),
        )
        assert r.status_code == 422, r.text


async def test_empty_title_clears_but_empty_content_rejected(tmp_path, monkeypatch):
    """``title=""``=清空标题(合法显式意图,且算"给了一个");``content=""`` → 422(本期不做)。"""
    _api_role(monkeypatch, tmp_path)
    async with rest_client(tmp_path, monkeypatch) as c:
        acc = await seed_account("编辑空串号", "uEdEmpty", _COOKIES)
        await _seed_note(acc)
        url = f"/api/accounts/{acc}/note-components"

        r = await c.post(url, json={"note_id": _NOTE, "title": ""}, headers=bearer(ADMIN_KEY))
        assert r.status_code == 202, r.text
        # 空串是"给了",且原样进 payload —— None 与 "" 必须能被浏览器层区分开
        assert (await _payload_of(r.json()["job_id"]))["title"] == ""

        r = await c.post(
            url, json={"note_id": _NOTE, "content": ""}, headers=bearer(ADMIN_KEY)
        )
        assert r.status_code == 422, r.text


async def test_content_length_boundary(tmp_path, monkeypatch):
    """正文 ≤900 放行,901 → 422(发布链路同款 XHS_MAX_BODY_LENGTH)。"""
    _api_role(monkeypatch, tmp_path)
    async with rest_client(tmp_path, monkeypatch) as c:
        acc = await seed_account("编辑正文号", "uEdBody", _COOKIES)
        await _seed_note(acc)
        url = f"/api/accounts/{acc}/note-components"

        assert (await c.post(
            url, json={"note_id": _NOTE, "content": "字" * 900}, headers=bearer(ADMIN_KEY)
        )).status_code == 202
        assert (await c.post(
            url, json={"note_id": _NOTE, "content": "字" * 901}, headers=bearer(ADMIN_KEY)
        )).status_code == 422


async def test_image_edit_validation_matrix(tmp_path, monkeypatch):
    """图片增删的六条校验:expected 必填 / 下标越界 / 重复 / 剩余<1 / 总数>18 / 空列表。"""
    _api_role(monkeypatch, tmp_path)
    async with rest_client(tmp_path, monkeypatch) as c:
        acc = await seed_account("编辑图片号", "uEdImg", _COOKIES)
        await _seed_note(acc)
        url = f"/api/accounts/{acc}/note-components"

        bad_bodies = [
            # 给了图片操作却没给 expected_image_count
            {"note_id": _NOTE, "remove_image_indexes": [1]},
            {"note_id": _NOTE, "add_images": [_IMG]},
            # 下标 0(1-based)/ 超过 expected
            {"note_id": _NOTE, "remove_image_indexes": [0], "expected_image_count": 3},
            {"note_id": _NOTE, "remove_image_indexes": [4], "expected_image_count": 3},
            # 重复下标(不去重,拒绝)
            {"note_id": _NOTE, "remove_image_indexes": [2, 2], "expected_image_count": 3},
            # 删完原图剩 0(不许拿 add_images 凑数)
            {"note_id": _NOTE, "remove_image_indexes": [1, 2], "expected_image_count": 2},
            {
                "note_id": _NOTE, "remove_image_indexes": [1, 2],
                "expected_image_count": 2, "add_images": [_IMG, _IMG],
            },
            # 改完总数 >18
            {"note_id": _NOTE, "add_images": [_IMG] * 3, "expected_image_count": 16},
            # 空列表(等于什么都不改,还要真提交一次全量覆盖)
            {"note_id": _NOTE, "add_images": [], "expected_image_count": 3},
            {"note_id": _NOTE, "remove_image_indexes": [], "expected_image_count": 3},
            # expected 自身越界
            {"note_id": _NOTE, "add_images": [_IMG], "expected_image_count": 19},
            {"note_id": _NOTE, "add_images": [_IMG], "expected_image_count": 0},
        ]
        for body in bad_bodies:
            r = await c.post(url, json=body, headers=bearer(ADMIN_KEY))
            assert r.status_code == 422, f"未被拒: {body!r} -> {r.text}"

        assert await _job_count() == 0

        # 边界合法:删到只剩 1 张 + 追加到正好 18 张
        assert (await c.post(url, json={
            "note_id": _NOTE, "remove_image_indexes": [3, 1], "expected_image_count": 3,
        }, headers=bearer(ADMIN_KEY))).status_code == 202
        assert (await c.post(url, json={
            "note_id": _NOTE, "add_images": [_IMG, _IMG], "expected_image_count": 16,
        }, headers=bearer(ADMIN_KEY))).status_code == 202


async def test_at_least_one_field_extended_to_edits(tmp_path, monkeypatch):
    """「至少给一个」扩展到八个字段:只给 title / 只给 content / 只给图片操作都放行。"""
    _api_role(monkeypatch, tmp_path)
    async with rest_client(tmp_path, monkeypatch) as c:
        acc = await seed_account("编辑单项号", "uEdOne", _COOKIES)
        await _seed_note(acc)
        url = f"/api/accounts/{acc}/note-components"

        for body in (
            {"note_id": _NOTE, "title": "新标题"},
            {"note_id": _NOTE, "content": "新正文"},
            {"note_id": _NOTE, "add_images": [_IMG], "expected_image_count": 2},
            {"note_id": _NOTE, "remove_image_indexes": [1], "expected_image_count": 2},
        ):
            r = await c.post(url, json=body, headers=bearer(ADMIN_KEY))
            assert r.status_code == 202, f"{body!r} -> {r.text}"

        # 八个字段一个都不给(expected_image_count 单独给不算)仍是 422
        r = await c.post(
            url, json={"note_id": _NOTE, "expected_image_count": 3}, headers=bearer(ADMIN_KEY)
        )
        assert r.status_code == 422, r.text


# ---------------- 服务层前置校验(建 job 前) ----------------


async def test_unknown_note_in_ledger_is_404(tmp_path, monkeypatch):
    """带编辑字段但台账查无此 note_id → 404,不登记任务。"""
    _api_role(monkeypatch, tmp_path)
    async with rest_client(tmp_path, monkeypatch) as c:
        acc = await seed_account("编辑无台账号", "uEdNoLedger", _COOKIES)
        r = await c.post(
            f"/api/accounts/{acc}/note-components",
            json={"note_id": "不在台账里", "title": "新标题"},
            headers=bearer(ADMIN_KEY),
        )
        assert r.status_code == 404, r.text
        assert await _job_count() == 0


async def test_ledger_check_is_scoped_by_account(tmp_path, monkeypatch):
    """台账查这篇按 (account_id, note_id) 定位:别的号有这篇不算数 → 404。"""
    _api_role(monkeypatch, tmp_path)
    async with rest_client(tmp_path, monkeypatch) as c:
        owner = await seed_account("编辑本号", "uEdOwner", _COOKIES)
        other = await seed_account("编辑他号", "uEdOther", _COOKIES)
        await _seed_note(owner)
        r = await c.post(
            f"/api/accounts/{other}/note-components",
            json={"note_id": _NOTE, "title": "新标题"},
            headers=bearer(ADMIN_KEY),
        )
        assert r.status_code == 404, r.text


async def test_non_image_note_type_is_422(tmp_path, monkeypatch):
    """非图文(视频 / 类型未知 null)→ 422:更新页结构只对图文验证过,这是全量覆盖提交。"""
    _api_role(monkeypatch, tmp_path)
    async with rest_client(tmp_path, monkeypatch) as c:
        acc = await seed_account("编辑视频号", "uEdVideo", _COOKIES)
        await _seed_note(acc, note_id="video-note", note_type="video")
        await _seed_note(acc, note_id="unknown-note", note_type=None)

        for note_id in ("video-note", "unknown-note"):
            r = await c.post(
                f"/api/accounts/{acc}/note-components",
                json={"note_id": note_id, "content": "新正文"},
                headers=bearer(ADMIN_KEY),
            )
            assert r.status_code == 422, f"{note_id} -> {r.text}"
            assert "note_type" in r.text
        assert await _job_count() == 0


async def test_pure_component_request_skips_ledger_precheck(tmp_path, monkeypatch):
    """纯三组件请求**不查台账**:老调用方语义一字不变(台账里没有也照常 202)。"""
    _api_role(monkeypatch, tmp_path)
    async with rest_client(tmp_path, monkeypatch) as c:
        acc = await seed_account("纯组件号", "uEdPureNc", _COOKIES)
        r = await c.post(
            f"/api/accounts/{acc}/note-components",
            json={"note_id": "台账里没有这篇", "collection_id": "c1"},
            headers=bearer(ADMIN_KEY),
        )
        assert r.status_code == 202, r.text
        # payload 与老版本逐字节一致:不多出编辑五键
        assert await _payload_of(r.json()["job_id"]) == {
            "note_id": "台账里没有这篇", "collection_id": "c1",
            "quoted_note_id": None, "activity_id": None, "related_counselor": None,
        }


async def test_bad_add_image_is_422_before_job(tmp_path, monkeypatch):
    """坏图 → 建 job 前就 422:别让它排队两分钟后才在浏览器层失败。"""
    _api_role(monkeypatch, tmp_path)
    async with rest_client(tmp_path, monkeypatch) as c:
        acc = await seed_account("编辑坏图号", "uEdBadImg", _COOKIES)
        await _seed_note(acc)
        url = f"/api/accounts/{acc}/note-components"

        for bad in ({"b64": ""}, 12345, "ftp://x/y.png"):
            r = await c.post(
                url,
                json={"note_id": _NOTE, "add_images": [bad], "expected_image_count": 2},
                headers=bearer(ADMIN_KEY),
            )
            assert r.status_code == 422, f"{bad!r} -> {r.text}"
            # 确认是落盘那一关拦的(而不是别的校验碰巧也 422)
            assert "落盘失败" in r.text, r.text
        assert await _job_count() == 0


async def test_add_images_materialized_into_payload(tmp_path, monkeypatch):
    """add_images 在建 job 前落成本地文件,payload 里存的是路径而不是 base64。"""
    _api_role(monkeypatch, tmp_path)
    async with rest_client(tmp_path, monkeypatch) as c:
        acc = await seed_account("编辑落盘号", "uEdMat", _COOKIES)
        await _seed_note(acc)
        r = await c.post(
            f"/api/accounts/{acc}/note-components",
            json={
                "note_id": _NOTE, "title": "新标题", "content": "新正文",
                "add_images": [_IMG, "data:image/png;base64," + _PNG_1x1_B64],
                "remove_image_indexes": [3, 1], "expected_image_count": 4,
                "activity_id": "43561",
            },
            headers=bearer(ADMIN_KEY),
        )
        assert r.status_code == 202, r.text

        payload = await _payload_of(r.json()["job_id"])
        # 编辑五键齐全(哪怕值为 None):事后翻台账查"当时改了什么"要一眼看全
        assert payload["title"] == "新标题" and payload["content"] == "新正文"
        assert payload["remove_image_indexes"] == [3, 1]  # 顺序原样,降序排是浏览器层的事
        assert payload["expected_image_count"] == 4
        assert payload["activity_id"] == "43561"  # 组件与编辑同一次请求共存
        paths = payload["add_images"]
        assert len(paths) == 2
        for p in paths:
            assert Path(p).is_file(), f"没落盘: {p}"
            assert str(tmp_path) in p  # 落在 UPLOAD_DIR 下的本次专用目录


# ---------------- 轮询视图透传(设计 3.2) ----------------


async def test_poll_exposes_edit_result_keys(tmp_path, monkeypatch):
    """编辑新增的结果键与 applied 新键都要下发,少一个调用方就无从判断改成没有。"""
    _api_role(monkeypatch, tmp_path)
    async with rest_client(tmp_path, monkeypatch) as c:
        acc = await seed_account("编辑轮询号", "uEdPoll", _COOKIES)
        await _seed_job(
            "ed-done-1", acc, "done",
            {
                "status": "partially_applied",
                "applied": {
                    "title": True, "content": True,
                    "image_add": False, "image_remove": None, "activity": True,
                },
                "failed": [{"component": "image_add", "reason": "image_add_count_mismatch"}],
                "submitted": True,
                "topics_dropped": ["身边的心理学"],
                "images_before": 4, "images_after": 3,
                "ledger_synced": True,
            },
        )
        body = (await c.get(
            "/api/note-components/ed-done-1", headers=bearer(ADMIN_KEY)
        )).json()

        assert body["result_status"] == "partially_applied"
        assert body["applied"]["title"] is True
        assert body["applied"]["image_remove"] is None
        assert body["topics_dropped"] == ["身边的心理学"]
        assert body["images_before"] == 4 and body["images_after"] == 3
        assert body["ledger_synced"] is True


async def test_poll_exposes_aborted_before_submit(tmp_path, monkeypatch):
    """弃提交是特殊终态:error 也必须把 aborted_before_submit 透出(笔记原样未动,可重试)。"""
    _api_role(monkeypatch, tmp_path)
    async with rest_client(tmp_path, monkeypatch) as c:
        acc = await seed_account("编辑弃提交号", "uEdAbort", _COOKIES)
        await _seed_job(
            "ed-abort-1", acc, "error",
            {
                "status": "failed",
                "error": "image_remove_failed: 第 2 张删除后容器数没变,停手不再点下一张",
                "aborted_before_submit": True,
                "submitted": False,
                "applied": {"image_remove": False, "title": None},
                "failed": [{"component": "image_remove", "reason": "container_count_unchanged"}],
                "images_before": 4,
            },
        )
        body = (await c.get(
            "/api/note-components/ed-abort-1", headers=bearer(ADMIN_KEY)
        )).json()

        assert body["status"] == "error"
        assert body["aborted_before_submit"] is True
        assert body["submitted"] is False
        assert "image_remove_failed" in body["reason"]


# ---------------- 服务层:执行入口把编辑字段送到浏览器层 ----------------


async def test_execute_passes_edit_fields_to_browser_layer(monkeypatch):
    """带编辑字段的任务把五个键**同名**送到 set_note_components,与组件参数同一次调用。

    这条用例原先钉的是「浏览器层没接线 → 一律 fail-closed」;T6 把编排接上后闸拆除,
    它改钉接线本身。要防的坏事没变 —— **静默丢改动比失败坏得多**:参数在服务层被丢掉的话,
    我们照样会点一次全量覆盖提交,然后报 done。所以这里断言的是"真送到了",而不是"没报错"。
    """
    seen = {}

    async def fake_load(_account_id):
        return [{"name": "a", "value": "b"}]

    class _FakeClient:
        def __init__(self, *_a, **_kw):
            self.page = object()

        def start(self):
            return {"success": True}

        def stop(self):
            pass

    def spy(_page, account_id, note_id, **kwargs):
        seen.update(kwargs, account_id=account_id, note_id=note_id)
        # applied 里没有 True 的文本项 → 不触发台账回写(回写另有专门用例)
        return {"status": "partially_applied", "applied": {"title": None}}

    monkeypatch.setattr(note_components, "load_account_cookies", fake_load)
    monkeypatch.setattr(note_components, "SyncClient", _FakeClient)
    monkeypatch.setattr(note_components, "set_note_components", spy)

    result = await note_components.execute(1, {
        "note_id": _NOTE, "activity_id": "43561",
        "title": "", "content": "新正文",
        "add_images": ["/tmp/a.png"], "remove_image_indexes": [3, 1],
        "expected_image_count": 4,
    })

    assert result["status"] == "partially_applied" and "error" not in result
    assert seen["note_id"] == _NOTE and seen["activity_id"] == "43561"
    # title="" 是"清空标题"这一合法意图,**必须**送下去(真值判断会把它静默丢掉)
    assert seen["title"] == "" and seen["content"] == "新正文"
    assert seen["add_images"] == ["/tmp/a.png"]
    assert seen["remove_image_indexes"] == [3, 1]   # 顺序原样,降序排是浏览器层的事
    assert seen["expected_image_count"] == 4


# ---------------- 服务层:台账回写(设计 3.3) ----------------


async def test_write_back_uses_read_values_only_when_verified(tmp_path, monkeypatch):
    """只有 applied 里为 True 的项才回写,且写的是**回读真值**(不是请求值)。"""
    _api_role(monkeypatch, tmp_path)
    async with rest_client(tmp_path, monkeypatch) as c:  # noqa: F841 — 只要隔离库
        acc = await seed_account("回写号", "uWb", _COOKIES)
        await _seed_note(acc)

        async with db_module.async_session() as s:
            ok = await note_components.write_back_ledger(
                s, acc, _NOTE,
                {"title": True, "content": True},
                {"title": "平台改写过的标题", "content": "平台回读的正文"},
            )
        assert ok is True

        async with db_module.async_session() as s:
            row = await s.scalar(
                select(PublishedNote).where(PublishedNote.note_id == _NOTE)
            )
        assert row.title == "平台改写过的标题"
        assert row.content_text == "平台回读的正文"
        assert row.content_fetched_at is not None


async def test_write_back_skips_unverified_items(tmp_path, monkeypatch):
    """False / None 一律不回写:台账写错比没写更坏(后续所有引用都会拿到假值)。"""
    _api_role(monkeypatch, tmp_path)
    async with rest_client(tmp_path, monkeypatch) as c:  # noqa: F841
        acc = await seed_account("回写未确认号", "uWbSkip", _COOKIES)
        await _seed_note(acc)

        async with db_module.async_session() as s:
            ok = await note_components.write_back_ledger(
                s, acc, _NOTE,
                {"title": False, "content": None, "image_add": True},
                {"title": "不该被写进去", "content": "也不该"},
            )
        # 没有"该写而没写成"的项 → True(不是失败)
        assert ok is True

        async with db_module.async_session() as s:
            row = await s.scalar(
                select(PublishedNote).where(PublishedNote.note_id == _NOTE)
            )
        assert row.title == "旧标题" and row.content_text == "旧正文"
        assert row.content_fetched_at is None


async def test_write_back_missing_read_value_is_false(tmp_path, monkeypatch):
    """回读值缺失 → 不拿请求值凑数,返回 False(台账保持原样)。"""
    _api_role(monkeypatch, tmp_path)
    async with rest_client(tmp_path, monkeypatch) as c:  # noqa: F841
        acc = await seed_account("回写缺值号", "uWbNone", _COOKIES)
        await _seed_note(acc)

        async with db_module.async_session() as s:
            ok = await note_components.write_back_ledger(
                s, acc, _NOTE, {"title": True}, {"content": "只有正文"}
            )
        assert ok is False

        async with db_module.async_session() as s:
            row = await s.scalar(
                select(PublishedNote).where(PublishedNote.note_id == _NOTE)
            )
        assert row.title == "旧标题"


async def test_write_back_missing_ledger_row_is_false_not_raise(tmp_path, monkeypatch):
    """台账没这行 → 只记日志返回 False,**不抛也不凭空建行**(建行是 note_ledger 的职责)。"""
    _api_role(monkeypatch, tmp_path)
    async with rest_client(tmp_path, monkeypatch) as c:  # noqa: F841
        acc = await seed_account("回写无行号", "uWbNoRow", _COOKIES)

        async with db_module.async_session() as s:
            ok = await note_components.write_back_ledger(
                s, acc, "查无此篇", {"title": True}, {"title": "新标题"}
            )
        assert ok is False

        async with db_module.async_session() as s:
            assert await s.scalar(select(func.count()).select_from(PublishedNote)) == 0


async def test_write_back_db_failure_is_swallowed_into_false(tmp_path, monkeypatch):
    """落库异常也不上抛:平台侧改动已生效,为一次留痕把任务判成 error 是误报。"""
    _api_role(monkeypatch, tmp_path)
    async with rest_client(tmp_path, monkeypatch) as c:  # noqa: F841
        acc = await seed_account("回写异常号", "uWbBoom", _COOKIES)
        await _seed_note(acc)

        class _BoomSession:
            async def scalar(self, *_a, **_kw):
                raise RuntimeError("库炸了")

        ok = await note_components.write_back_ledger(
            _BoomSession(), acc, _NOTE, {"title": True}, {"title": "新标题"}
        )
        assert ok is False
