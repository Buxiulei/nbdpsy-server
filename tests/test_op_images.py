"""一致性生图迁移测试:REST 契约 + job 服务语义(不真调 OpenAI / 不起浏览器)。

契约锚(skill gen_images.py 零改动恢复的关键,见 NBDpsy 协同记录):
- POST /api/op/consistent-images → 202 {"job_id": int, "session_id": str}
- GET /api/op/drafts/{sid}/jobs/{jid} → {"status", "result"};404=不存在
- done 时 result.urls 与 prompts 下标对齐(失败位空串),errors 等长(成功位空串)
- 额度错/单页失败 = done + errors 有值(不是 failed);任务级崩溃才 failed
- anchor_url 解析不到 → done + 全失败位(不静默降级)
- 无鉴权 → 401
- result.orig_urls 与 urls/errors 等长同序:去水印前原图 NN.orig.ext;
  去水印失败 = 该页失败(urls 空 + errors 写明),但原图照样可取
"""

import asyncio
from pathlib import Path

from app.imagegen.openai_image import ImageGenResult
from app.services import op_images
from tests.rest_helpers import ADMIN_KEY, bearer, rest_client


def _fake_batch(results_map):
    """构造假 generate_batch:按 prompts 逐个吐 results_map 里的结果。"""
    async def fake(self, prompts, *, anchor_path=None, aspect_ratio="3:4", save_prefix="p"):
        return [results_map(i, p, anchor_path) for i, p in enumerate(prompts)]
    return fake


async def _fake_dewatermark_ok(path):
    """假去水印成功:照真 reraster 契约另存 ``{stem}.shot.jpg``,原图原地不动。"""
    src = Path(path)
    out = src.with_suffix(".shot.jpg")
    out.write_bytes(b"\xff\xd8dewatermarked-" + src.name.encode())
    return str(out)


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
        # 假去水印(不起浏览器):照真契约另存一个产物文件,原图留在原地
        monkeypatch.setattr(
            "app.services.op_images.dewatermark", _fake_dewatermark_ok)

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


# ---------------- 双产物:默认交付(去水印图)+ 原图提取通道 ----------------


def _patch_uploads(tmp_path, monkeypatch) -> Path:
    """把 uploads 根指到 tmp,返回该根路径(execute 的产物落这里)。"""
    monkeypatch.setattr("app.services.op_images.settings.DATA_DIR", str(tmp_path))
    return tmp_path / "uploads"


def _disk(uploads_root: Path, url: str) -> Path:
    """/uploads/{dir}/{name} 相对直链 → 本地磁盘路径。"""
    return uploads_root / url[len("/uploads/"):]


def _gen_ok(tmp_path):
    """假 provider:每页都成功,落一张内容各异的 png 原图。"""
    def mapper(i, prompt, anchor):
        p = tmp_path / f"raw{i}.png"
        p.write_bytes(b"\x89PNGraw-original-" + str(i).encode())
        return ImageGenResult(success=True, path=str(p))
    return mapper


async def test_success_page_has_both_delivery_and_orig(tmp_path, monkeypatch):
    """成功页:urls 指 NN.jpg(去水印图)、orig_urls 指 NN.orig.png(原图),两文件都在且内容不同。"""
    uploads = _patch_uploads(tmp_path, monkeypatch)
    monkeypatch.setattr(
        "app.imagegen.openai_image.OpenAIImageProvider.generate_batch",
        _fake_batch(_gen_ok(tmp_path)),
    )
    monkeypatch.setattr("app.services.op_images.dewatermark", _fake_dewatermark_ok)

    res = await op_images.execute({"prompts": ["p1", "p2"]})

    assert res["urls"][0].endswith("/01.jpg") and res["urls"][1].endswith("/02.jpg")
    assert res["orig_urls"][0].endswith("/01.orig.png")
    assert res["orig_urls"][1].endswith("/02.orig.png")
    for i in range(2):
        served = _disk(uploads, res["urls"][i])
        orig = _disk(uploads, res["orig_urls"][i])
        assert served.is_file() and orig.is_file()
        # 交付图是去水印产物,原图是 provider 原始字节:两者内容必须不同
        assert served.read_bytes() != orig.read_bytes()
        assert orig.read_bytes().startswith(b"\x89PNGraw-original-")


async def test_dewatermark_failure_fails_page_but_keeps_orig(tmp_path, monkeypatch):
    """去水印失败页:urls[i]="" + errors[i] 写明失败;原图仍落盘可取(绝不拿带水印图冒充交付)。"""
    uploads = _patch_uploads(tmp_path, monkeypatch)
    monkeypatch.setattr(
        "app.imagegen.openai_image.OpenAIImageProvider.generate_batch",
        _fake_batch(_gen_ok(tmp_path)),
    )

    async def fake_dewatermark(path):
        return None  # reraster 整条主路失败

    monkeypatch.setattr("app.services.op_images.dewatermark", fake_dewatermark)

    res = await op_images.execute({"prompts": ["p1"]})

    assert res["urls"] == [""]
    assert "去水印失败" in res["errors"][0]
    assert res["orig_urls"][0].endswith("/01.orig.png")
    orig = _disk(uploads, res["orig_urls"][0])
    assert orig.is_file() and orig.read_bytes().startswith(b"\x89PNGraw-original-")
    # 带水印的原图绝不能占默认交付位
    assert not (orig.parent / "01.png").exists()
    assert not (orig.parent / "01.jpg").exists()


async def test_three_arrays_aligned_with_middle_page_failures(tmp_path, monkeypatch):
    """urls/errors/orig_urls 三者等长且与 prompts 严格同序——中间页失败不许移位。"""
    uploads = _patch_uploads(tmp_path, monkeypatch)

    def mapper(i, prompt, anchor):
        if i == 2:  # 第 3 页 provider 直接失败(无原图)
            return ImageGenResult(success=False, error="billing_hard_limit_reached")
        p = tmp_path / f"raw{i}.png"
        p.write_bytes(b"\x89PNGraw-original-" + str(i).encode())
        return ImageGenResult(success=True, path=str(p))

    monkeypatch.setattr(
        "app.imagegen.openai_image.OpenAIImageProvider.generate_batch",
        _fake_batch(mapper),
    )

    async def fake_dewatermark(path):
        if path.endswith("raw1.png"):  # 第 2 页去水印失败(有原图)
            return None
        return await _fake_dewatermark_ok(path)

    monkeypatch.setattr("app.services.op_images.dewatermark", fake_dewatermark)

    res = await op_images.execute({"prompts": ["p1", "p2", "p3", "p4"]})

    assert len(res["urls"]) == len(res["errors"]) == len(res["orig_urls"]) == 4
    # 第 1/4 页成功;第 2 页去水印失败;第 3 页 provider 失败
    assert res["urls"][0].endswith("/01.jpg") and res["urls"][3].endswith("/04.jpg")
    assert res["urls"][1] == "" and res["urls"][2] == ""
    assert res["errors"][0] == "" and res["errors"][3] == ""
    assert "去水印失败" in res["errors"][1]
    assert "billing" in res["errors"][2]
    # 原图:有 provider 产物的三页都可取,provider 失败那页空串占位(不移位)
    assert res["orig_urls"][0].endswith("/01.orig.png")
    assert res["orig_urls"][1].endswith("/02.orig.png")
    assert res["orig_urls"][2] == ""
    assert res["orig_urls"][3].endswith("/04.orig.png")
    for i in (0, 1, 3):
        assert _disk(uploads, res["orig_urls"][i]).is_file()


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


async def test_rename_failure_only_collapses_that_slot(tmp_path, monkeypatch):
    """单页 rename 炸掉只塌该位,不得冒泡把整批(含已成功已付费的页)判崩。

    评审确认的纪律不一致:openai_image 的 _edit_one/_fallback_one 显式 try/except
    「保证该下标位不塌陷」,而这里两处 rename 裸奔——一炸整个 job 变 {"error"},
    之前已成功的页结果随返回值一起丢。
    """
    import app.services.op_images as oi

    monkeypatch.setattr(oi.settings, "DATA_DIR", str(tmp_path))

    async def fake_batch(self, prompts, *, anchor_path=None, save_prefix="p",
                         aspect_ratio="3:4"):
        out = []
        for i, _ in enumerate(prompts):
            p = tmp_path / f"raw{i}.png"
            p.write_bytes(b"orig")
            out.append(type("R", (), {"success": True, "path": str(p), "error": None})())
        return out

    monkeypatch.setattr(oi.OpenAIImageProvider, "generate_batch", fake_batch)

    async def fake_dw(path):
        clean = Path(path).with_suffix(".shot.jpg")
        clean.write_bytes(b"cleaned")
        return str(clean)

    monkeypatch.setattr(oi, "dewatermark", fake_dw)

    real_rename = Path.rename
    def flaky_rename(self, target):
        if str(target).endswith("02.jpg"):          # 只让第 2 页的交付改名炸
            raise OSError("rename boom")
        return real_rename(self, target)
    monkeypatch.setattr(Path, "rename", flaky_rename)

    res = await oi.execute({"prompts": ["a", "b", "c"]})

    assert "error" not in res, f"整批不该崩:{res}"
    assert len(res["urls"]) == len(res["errors"]) == len(res["orig_urls"]) == 3
    assert res["urls"][0] and res["urls"][2], "其余页照常交付"
    assert res["urls"][1] == "" and "改名失败" in res["errors"][1], "只塌第 2 位"
    assert res["orig_urls"][1], "原图仍可取"


def test_aspect_ratio_reaches_provider(tmp_path, monkeypatch):
    """公众号配图要横版：aspect_ratio 必须原样透到 provider，缺省回落 3:4。

    这条锁死的是「端点 → start_images_job → payload → execute → provider」整条
    透传链。链上任一处漏掉该键，出图就会静默回落成竖版 1024x1536——公众号封面
    拿到竖图不会报错，只会版面全毁，是最难在事后发现的一类回归。
    """
    seen: dict = {}

    class FakeProvider:
        def __init__(self, save_dir=None):
            pass

        async def generate_batch(self, prompts, *, anchor_path=None,
                                 save_prefix="p", aspect_ratio="3:4"):
            seen["aspect_ratio"] = aspect_ratio
            return []

    monkeypatch.setattr(op_images, "OpenAIImageProvider", FakeProvider)
    monkeypatch.setattr(op_images, "_uploads_root", lambda: tmp_path)

    asyncio.run(op_images.execute({"prompts": ["p1"], "aspect_ratio": "16:9"}))
    assert seen["aspect_ratio"] == "16:9", "16:9 必须透到 provider，否则公众号拿到竖图"

    seen.clear()
    asyncio.run(op_images.execute({"prompts": ["p1"]}))  # 老台账无该键
    assert seen["aspect_ratio"] == "3:4", "缺省必须回落 3:4，保小红书轮播不变"


# ── 重试可见性:attempts 第四个等长数组(2026-08-05 生图超时加固)──────────────

async def test_attempts_array_aligned_with_urls(tmp_path, monkeypatch):
    """result.attempts 与 urls/errors/orig_urls 等长同序,失败页也占位不移位。

    运营据此判断"慢/抖动是不是常态"(需求文档四条验收之一);下标对齐是硬约束,
    新增数组只能"再加一条等长的",不许改动原三条的语义。
    """
    _patch_uploads(tmp_path, monkeypatch)

    def mapper(i, prompt, anchor):
        if i == 1:  # 第 2 页超时重试耗尽(3 次尝试)
            return ImageGenResult(
                success=False, error="openai_image_edit_failed: Request timed out.",
                metadata={"upstream_attempts": 3})
        p = tmp_path / f"raw{i}.png"
        p.write_bytes(b"\x89PNGraw-original-" + str(i).encode())
        return ImageGenResult(success=True, path=str(p),
                              metadata={"upstream_attempts": 2 if i == 0 else 1})

    monkeypatch.setattr(
        "app.imagegen.openai_image.OpenAIImageProvider.generate_batch",
        _fake_batch(mapper),
    )
    monkeypatch.setattr("app.services.op_images.dewatermark", _fake_dewatermark_ok)

    res = await op_images.execute({"prompts": ["p1", "p2", "p3"]})

    assert len(res["attempts"]) == len(res["urls"]) == len(res["errors"]) == 3
    assert res["attempts"] == [2, 3, 1]
    assert res["urls"][1] == "" and "timed out" in res["errors"][1]


async def test_attempts_defaults_to_zero_without_upstream_call(tmp_path, monkeypatch):
    """没打到上游的失败(锚点解析失败/provider 没给计数)→ attempts 占位 0,长度照样对齐。"""
    _patch_uploads(tmp_path, monkeypatch)

    res = await op_images.execute(
        {"prompts": ["p1", "p2"], "anchor_url": "/uploads/nope/does-not-exist.png"})

    assert res["attempts"] == [0, 0]
    assert res["urls"] == ["", ""] and len(res["errors"]) == 2
