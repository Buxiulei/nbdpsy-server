"""一致性生图迁移测试:REST 契约 + job 服务语义(不真调 OpenAI / 不起浏览器)。

契约锚(skill gen_images.py 零改动恢复的关键,见 NBDpsy 协同记录):
- POST /api/op/consistent-images → 202 {"job_id": int, "session_id": str}
- GET /api/op/drafts/{sid}/jobs/{jid} → {"status", "result"};404=不存在
- done 时 result.urls 与 prompts 下标对齐(失败位空串),errors 等长(成功位空串)
- 额度错/单页失败 = done + errors 有值(不是 failed);任务级崩溃才 failed
- anchor_url 解析不到 → done + 全失败位(不静默降级)
- 无鉴权 → 401
"""

import asyncio

from app.imagegen.openai_image import ImageGenResult
from app.services import op_images
from tests.rest_helpers import ADMIN_KEY, bearer, rest_client


def _fake_batch(results_map):
    """构造假 generate_batch:按 prompts 逐个吐 results_map 里的结果。"""
    async def fake(self, prompts, *, anchor_path=None, aspect_ratio="3:4", save_prefix="p"):
        return [results_map(i, p, anchor_path) for i, p in enumerate(prompts)]
    return fake


async def _wait_terminal(sid, jid, timeout=5.0):
    """等 job 落终态(测试内 fake 批量应瞬时完成)。"""
    for _ in range(int(timeout * 20)):
        entry = op_images.get_images_job(sid, jid)
        if entry and entry["status"] in ("done", "failed"):
            return entry
        await asyncio.sleep(0.05)
    raise AssertionError("job 未在时限内落终态")


async def test_post_contract_202_and_poll_done(tmp_path, monkeypatch):
    """202 契约 + done 时 urls/errors 与 prompts 下标对齐(含单页失败位)。"""
    async with rest_client(tmp_path, monkeypatch) as c:
        # 假 provider:第 2 页失败(额度错),其余成功
        def mapper(i, prompt, anchor):
            if i == 1:
                return ImageGenResult(success=False, error="billing_hard_limit_reached")
            p = tmp_path / f"img{i}.png"
            p.write_bytes(b"png")
            return ImageGenResult(success=True, path=str(p))

        monkeypatch.setattr(
            "app.imagegen.openai_image.OpenAIImageProvider.generate_batch",
            _fake_batch(mapper),
        )
        # 去水印后处理直通(不起浏览器)
        async def fake_dewatermark(path):
            return path
        monkeypatch.setattr("app.services.op_images.dewatermark", fake_dewatermark)

        r = await c.post(
            "/api/op/consistent-images",
            json={"prompts": ["P1 提示词", "P2 提示词", "P3 提示词"]},
            headers=bearer(ADMIN_KEY),
        )
        assert r.status_code == 202, r.text
        body = r.json()
        assert isinstance(body["job_id"], int)
        assert isinstance(body["session_id"], str) and body["session_id"]

        entry = await _wait_terminal(body["session_id"], body["job_id"])
        # 额度错是 done + errors,不是 failed
        assert entry["status"] == "done"
        result = entry["result"]
        assert len(result["urls"]) == 3 and len(result["errors"]) == 3
        assert result["urls"][0].startswith("/uploads/")   # 成功位:相对直链
        assert result["urls"][1] == ""                      # 失败位:空串占位
        assert "billing" in result["errors"][1]
        assert result["errors"][0] == "" and result["errors"][2] == ""

        # REST 轮询同一形状
        r2 = await c.get(
            f"/api/op/drafts/{body['session_id']}/jobs/{body['job_id']}",
            headers=bearer(ADMIN_KEY),
        )
        assert r2.status_code == 200
        assert r2.json()["status"] == "done"
        assert r2.json()["result"]["urls"][0].startswith("/uploads/")


async def test_anchor_url_unresolvable_fails_loud(tmp_path, monkeypatch):
    """anchor_url 解析不到 → done + 全失败位 + 明确报错,绝不静默降级出图。"""
    async with rest_client(tmp_path, monkeypatch) as c:
        called = {"n": 0}

        def mapper(i, prompt, anchor):
            called["n"] += 1
            return ImageGenResult(success=True, path="x")

        monkeypatch.setattr(
            "app.imagegen.openai_image.OpenAIImageProvider.generate_batch",
            _fake_batch(mapper),
        )
        r = await c.post(
            "/api/op/consistent-images",
            json={"prompts": ["p1", "p2"],
                  "anchor_url": "/uploads/nope/missing.png"},
            headers=bearer(ADMIN_KEY),
        )
        assert r.status_code == 202
        body = r.json()
        entry = await _wait_terminal(body["session_id"], body["job_id"])
        assert entry["status"] == "done"
        assert entry["result"]["urls"] == ["", ""]
        assert all("anchor_url 解析失败" in e for e in entry["result"]["errors"])
        assert called["n"] == 0  # 没有静默降级去出图


async def test_job_crash_is_failed(tmp_path, monkeypatch):
    """任务级意外崩溃 → failed + result.error(与额度错的 done+errors 区分)。"""
    async with rest_client(tmp_path, monkeypatch) as c:
        async def boom(self, prompts, **kw):
            raise RuntimeError("底层爆炸")
        monkeypatch.setattr(
            "app.imagegen.openai_image.OpenAIImageProvider.generate_batch", boom)

        r = await c.post(
            "/api/op/consistent-images", json={"prompts": ["p1"]},
            headers=bearer(ADMIN_KEY),
        )
        body = r.json()
        entry = await _wait_terminal(body["session_id"], body["job_id"])
        assert entry["status"] == "failed"
        assert "底层爆炸" in entry["result"]["error"]


async def test_poll_unknown_404_and_auth_401(tmp_path, monkeypatch):
    """未知 job → 404;无鉴权 → 401(op 端点在鉴权墙内)。"""
    async with rest_client(tmp_path, monkeypatch) as c:
        r = await c.get("/api/op/drafts/nosuch/jobs/999", headers=bearer(ADMIN_KEY))
        assert r.status_code == 404
        r2 = await c.post("/api/op/consistent-images", json={"prompts": ["p"]})
        assert r2.status_code == 401


def test_resolve_anchor_path_guards(tmp_path, monkeypatch):
    """anchor 解析:uploads 内真实文件通过;路径穿越/域外/不存在全拒。"""
    uploads = tmp_path / "uploads" / "batch1"
    uploads.mkdir(parents=True)
    f = uploads / "P01.png"
    f.write_bytes(b"x")
    monkeypatch.setattr(
        "app.services.op_images.settings.DATA_DIR", str(tmp_path))

    ok = op_images.resolve_anchor_path("https://mcp.nbdpsy.com/uploads/batch1/P01.png")
    assert ok == str(f.resolve())
    assert op_images.resolve_anchor_path("/uploads/batch1/P01.png") == str(f.resolve())
    assert op_images.resolve_anchor_path("/uploads/../secrets.txt") is None
    assert op_images.resolve_anchor_path("/downloads/x.png") is None
    assert op_images.resolve_anchor_path("/uploads/batch1/none.png") is None
