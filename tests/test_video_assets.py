"""视频资产库 REST + 服务层测试：转存独立副本 / 归属隔离 / 检索 / 删除 / 幂等 / 迁移。

最要紧的一条是 ``test_stored_asset_survives_source_clip_deletion``：转存必须是**拷贝**
而非指针——源 clip 会被 ClipReaper 按 TTL 删掉，存指针等于假长期。
"""

import shutil
import sqlite3
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import select

import app.core.db as db_module
from app.core.config import settings
from app.models.video_clip import VideoClip
from app.services import dreamina, video_assets
from tests.rest_helpers import ADMIN_KEY, bearer, make_operator, rest_client

import pathlib

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]

# 资产库表落地前的 alembic 头（downgrade 目标）
_PREV_REVISION = "c8a5e21fb730"


async def _seed_done_clip(created_by: int, *, prompt: str = "空镜：黄昏的走廊",
                          model: str = "seedance2.5", content: bytes = b"mp4-bytes"):
    """造一条 done 的 clip（含真实落盘产物），返回 clip_id。DATA_DIR 须已 monkeypatch。"""
    clip_id = dreamina.new_clip_id()
    path = dreamina.clip_dir(clip_id) / "clip.mp4"
    path.write_bytes(content)
    async with db_module.async_session() as s:
        s.add(VideoClip(
            clip_id=clip_id, operation="text2video", prompt=prompt, model=model,
            duration=5, status="done", video_path=str(path),
            video_url=dreamina.clip_public_url(clip_id, "clip.mp4"),
            created_by=created_by,
        ))
        await s.commit()
    return clip_id


# ── 转存：独立副本 ───────────────────────────────────────────────────────────
async def test_stored_asset_survives_source_clip_deletion(tmp_path, monkeypatch):
    """转存 = 拷一份独立副本：源 clip 目录被 TTL 删光后，资产直链照样可读。"""
    monkeypatch.setattr(settings, "DATA_DIR", str(tmp_path))
    async with rest_client(tmp_path, monkeypatch) as client:
        op_key = "op-assets-copy"
        op_id = await make_operator(op_key)
        clip_id = await _seed_done_clip(op_id, content=b"perfect-shot")

        r = await client.post("/api/video-assets",
                              json={"clip_id": clip_id, "name": "定妆镜头", "tags": ["定妆"]},
                              headers=bearer(op_key))
        assert r.status_code == 201, r.text
        asset = r.json()
        assert asset["deduplicated"] is False
        assert asset["size_bytes"] == len(b"perfect-shot")

        # 模拟 ClipReaper：删掉源 clip 的整个工作目录
        shutil.rmtree(dreamina.clip_dir(clip_id))

        direct = await client.get(asset["video_url"])          # 免鉴权直链
        assert direct.status_code == 200 and direct.content == b"perfect-shot"
        assert direct.headers["content-type"] == "video/mp4"

        detail = await client.get(f"/api/video-assets/{asset['asset_id']}",
                                  headers=bearer(op_key))
        assert detail.status_code == 200
        assert detail.json()["video_url"] == asset["video_url"]
        assert detail.json()["source_clip_id"] == clip_id


async def test_asset_dir_survives_clip_reaper_orphan_sweep(tmp_path, monkeypatch):
    """资产目录必须落在 ClipReaper 的射程之外——这是本模块最容易被静默误杀的一条。

    ``dreamina.reap_clips_once`` 第三类清理会把 ``uploads/clips/`` 下「没有对应 video_clips
    行、且 mtime 超龄」的目录当孤儿 rmtree。资产副本正中这个判据：真放进去，存够 TTL 天
    就无声消失。这里把资产目录的 mtime 调到远古再真跑一轮 reaper，钉死它不被扫到。
    """
    import os
    import time

    monkeypatch.setattr(settings, "DATA_DIR", str(tmp_path))
    async with rest_client(tmp_path, monkeypatch) as client:
        op_key = "op-assets-reaper"
        op_id = await make_operator(op_key)
        clip_id = await _seed_done_clip(op_id)
        asset = (await client.post("/api/video-assets",
                                   json={"clip_id": clip_id, "name": "别删我"},
                                   headers=bearer(op_key))).json()
        adir = video_assets.assets_root() / video_assets.asset_token_dir(asset["asset_id"])
        assert not adir.is_relative_to(dreamina.clips_root()), "资产目录不得落在 clips 树下"

        ancient = time.time() - settings.CLIP_TTL_DAYS * 86400 * 10
        os.utime(adir, (ancient, ancient))
        os.utime(video_assets.assets_root(), (ancient, ancient))

        await dreamina.reap_clips_once(db_module.async_session)

        assert (adir / video_assets.ASSET_FILE_NAME).is_file(), "资产副本被 ClipReaper 误杀"
        assert (await client.get(asset["video_url"])).status_code == 200


async def test_truncated_copy_rolls_back(tmp_path, monkeypatch):
    """拷贝只落了半截（与 reaper 的竞态）→ 400 且不留资产行、不留半截目录。"""
    monkeypatch.setattr(settings, "DATA_DIR", str(tmp_path))
    async with rest_client(tmp_path, monkeypatch) as client:
        op_key = "op-assets-truncated"
        op_id = await make_operator(op_key)
        clip_id = await _seed_done_clip(op_id, content=b"x" * 4096)

        def _half_copy(src, dst, **kwargs):
            Path(dst).write_bytes(Path(src).read_bytes()[:100])
            return dst

        monkeypatch.setattr(video_assets.shutil, "copy2", _half_copy)
        r = await client.post("/api/video-assets",
                              json={"clip_id": clip_id, "name": "半截"},
                              headers=bearer(op_key))
        assert r.status_code == 400
        assert "不完整" in r.json()["error"]
        assert (await client.get("/api/video-assets",
                                 headers=bearer(op_key))).json()["assets"] == []
        root = video_assets.assets_root()
        assert not root.exists() or not any(root.iterdir()), "半截目录必须回滚干净"


async def test_direct_link_rejects_wrong_token_and_traversal(tmp_path, monkeypatch):
    """直链的访问控制就是不可猜的 HMAC 目录名：token 错 / 文件名不在白名单一律 404。"""
    monkeypatch.setattr(settings, "DATA_DIR", str(tmp_path))
    async with rest_client(tmp_path, monkeypatch) as client:
        op_key = "op-assets-token"
        op_id = await make_operator(op_key)
        clip_id = await _seed_done_clip(op_id)
        r = await client.post("/api/video-assets",
                              json={"clip_id": clip_id, "name": "空镜"},
                              headers=bearer(op_key))
        asset_id = r.json()["asset_id"]
        token_dir = video_assets.asset_token_dir(asset_id)

        wrong = f"{asset_id}-" + "0" * 16
        assert (await client.get(f"/uploads/video-assets/{wrong}/asset.mp4")).status_code == 404
        assert (await client.get(
            f"/uploads/video-assets/{token_dir}/asset.mp4.bak")).status_code == 404
        assert (await client.get(
            f"/uploads/video-assets/{token_dir}/../../t.db")).status_code == 404


# ── 转存失败：源不可用 ───────────────────────────────────────────────────────
async def test_store_rejects_missing_or_unusable_clip(tmp_path, monkeypatch):
    """clip 不存在 → 404；未完成 / 产物已被清理 → 400，绝不建指向空文件的资产行。"""
    monkeypatch.setattr(settings, "DATA_DIR", str(tmp_path))
    async with rest_client(tmp_path, monkeypatch) as client:
        op_key = "op-assets-bad"
        op_id = await make_operator(op_key)

        missing = await client.post("/api/video-assets",
                                    json={"clip_id": "vc_ffffffffff", "name": "x"},
                                    headers=bearer(op_key))
        assert missing.status_code == 404

        # 在飞任务（无产物）
        async with db_module.async_session() as s:
            queued_id = dreamina.new_clip_id()
            s.add(VideoClip(clip_id=queued_id, operation="text2video", prompt="p",
                            model="seedance2.5", duration=5, status="querying",
                            created_by=op_id))
            await s.commit()
        not_done = await client.post("/api/video-assets",
                                     json={"clip_id": queued_id, "name": "x"},
                                     headers=bearer(op_key))
        assert not_done.status_code == 400
        assert "未完成" in not_done.json()["error"]

        # done 但产物已被 TTL 清理（reaper 清空 video_path/video_url）
        reaped_id = await _seed_done_clip(op_id)
        shutil.rmtree(dreamina.clip_dir(reaped_id))
        async with db_module.async_session() as s:
            row = (await s.execute(
                select(VideoClip).where(VideoClip.clip_id == reaped_id))).scalar_one()
            row.video_path = None
            row.video_url = None
            await s.commit()
        expired = await client.post("/api/video-assets",
                                    json={"clip_id": reaped_id, "name": "x"},
                                    headers=bearer(op_key))
        assert expired.status_code == 400
        assert "已过期" in expired.json()["error"]

        listed = await client.get("/api/video-assets", headers=bearer(op_key))
        assert listed.json()["assets"] == [], "失败的转存不得留下资产行"


async def test_store_other_operators_clip_denied(tmp_path, monkeypatch):
    """转存别人的 clip → 403（归属规则与 GET /api/video-clips 同款：admin 全见）。"""
    monkeypatch.setattr(settings, "DATA_DIR", str(tmp_path))
    async with rest_client(tmp_path, monkeypatch) as client:
        owner_key, other_key = "op-clip-owner", "op-clip-other"
        owner_id = await make_operator(owner_key)
        await make_operator(other_key)
        clip_id = await _seed_done_clip(owner_id)

        denied = await client.post("/api/video-assets",
                                   json={"clip_id": clip_id, "name": "x"},
                                   headers=bearer(other_key))
        assert denied.status_code == 403
        ok = await client.post("/api/video-assets",
                               json={"clip_id": clip_id, "name": "x"},
                               headers=bearer(ADMIN_KEY))
        assert ok.status_code == 201


# ── 幂等 ────────────────────────────────────────────────────────────────────
async def test_repeat_store_returns_existing_asset(tmp_path, monkeypatch):
    """同一 clip 重复转存：回原资产 + deduplicated=true，零新副本（存储不白给）。"""
    monkeypatch.setattr(settings, "DATA_DIR", str(tmp_path))
    async with rest_client(tmp_path, monkeypatch) as client:
        op_key = "op-assets-dedup"
        op_id = await make_operator(op_key)
        clip_id = await _seed_done_clip(op_id)

        first = await client.post("/api/video-assets",
                                  json={"clip_id": clip_id, "name": "原名"},
                                  headers=bearer(op_key))
        second = await client.post("/api/video-assets",
                                   json={"clip_id": clip_id, "name": "新名"},
                                   headers=bearer(op_key))
        assert second.status_code == 201
        assert second.json()["asset_id"] == first.json()["asset_id"]
        assert second.json()["deduplicated"] is True
        assert second.json()["name"] == "原名", "重复转存不改名（要改名先删再存）"
        assert len(list(video_assets.assets_root().iterdir())) == 1


# ── 列表 / 检索 / 归属 ───────────────────────────────────────────────────────
async def test_list_scoped_to_caller(tmp_path, monkeypatch):
    """列表按 caller 归属：A 看不到 B 的；admin 全见（与 clips 同款）。"""
    monkeypatch.setattr(settings, "DATA_DIR", str(tmp_path))
    async with rest_client(tmp_path, monkeypatch) as client:
        a_key, b_key = "op-assets-a", "op-assets-b"
        a_id, b_id = await make_operator(a_key), await make_operator(b_key)
        for op_key, op_id, name in ((a_key, a_id, "A 的镜头"), (b_key, b_id, "B 的镜头")):
            clip_id = await _seed_done_clip(op_id)
            r = await client.post("/api/video-assets",
                                  json={"clip_id": clip_id, "name": name},
                                  headers=bearer(op_key))
            assert r.status_code == 201, r.text

        a_list = (await client.get("/api/video-assets", headers=bearer(a_key))).json()
        assert [x["name"] for x in a_list["assets"]] == ["A 的镜头"]
        b_list = (await client.get("/api/video-assets", headers=bearer(b_key))).json()
        assert [x["name"] for x in b_list["assets"]] == ["B 的镜头"]
        admin_list = (await client.get("/api/video-assets", headers=bearer(ADMIN_KEY))).json()
        assert len(admin_list["assets"]) == 2

        # 详情同样按归属：B 拿 A 的 asset_id → 403
        a_asset_id = a_list["assets"][0]["asset_id"]
        assert (await client.get(f"/api/video-assets/{a_asset_id}",
                                 headers=bearer(b_key))).status_code == 403
        assert (await client.get("/api/video-assets/va_ffffffffff",
                                 headers=bearer(a_key))).status_code == 404


async def test_list_filters_by_tag_and_query(tmp_path, monkeypatch):
    """?tag= 精确匹配标签；?q= 模糊搜名称 / 标签 / 源提示词。"""
    monkeypatch.setattr(settings, "DATA_DIR", str(tmp_path))
    async with rest_client(tmp_path, monkeypatch) as client:
        op_key = "op-assets-search"
        op_id = await make_operator(op_key)
        c1 = await _seed_done_clip(op_id, prompt="黄昏走廊的空镜，光线极佳")
        c2 = await _seed_done_clip(op_id, prompt="角色定妆特写")
        await client.post("/api/video-assets",
                          json={"clip_id": c1, "name": "走廊空镜", "tags": ["空镜", "光线"]},
                          headers=bearer(op_key))
        await client.post("/api/video-assets",
                          json={"clip_id": c2, "name": "定妆特写", "tags": ["人物"]},
                          headers=bearer(op_key))

        async def names(params):
            r = await client.get("/api/video-assets", params=params, headers=bearer(op_key))
            assert r.status_code == 200, r.text
            return sorted(x["name"] for x in r.json()["assets"])

        assert await names({"tag": "空镜"}) == ["走廊空镜"]
        assert await names({"tag": "人物"}) == ["定妆特写"]
        assert await names({"tag": "不存在"}) == []
        assert await names({"q": "定妆"}) == ["定妆特写"]          # 命中源提示词
        assert await names({"q": "光线"}) == ["走廊空镜"]          # 命中标签
        assert await names({"q": "空镜"}) == ["走廊空镜"]
        assert await names({"tag": "空镜", "q": "定妆"}) == []     # 两条件同时生效


# ── 删除 ────────────────────────────────────────────────────────────────────
async def test_delete_only_own_asset(tmp_path, monkeypatch):
    """删除只能删自己的；删掉即行与文件副本一起消失（不设 TTL，靠运营自清）。"""
    monkeypatch.setattr(settings, "DATA_DIR", str(tmp_path))
    async with rest_client(tmp_path, monkeypatch) as client:
        owner_key, other_key = "op-del-owner", "op-del-other"
        owner_id = await make_operator(owner_key)
        await make_operator(other_key)
        clip_id = await _seed_done_clip(owner_id)
        created = (await client.post("/api/video-assets",
                                     json={"clip_id": clip_id, "name": "留着"},
                                     headers=bearer(owner_key))).json()
        asset_id, url = created["asset_id"], created["video_url"]

        assert (await client.delete(f"/api/video-assets/{asset_id}",
                                    headers=bearer(other_key))).status_code == 403
        assert (await client.get(url)).status_code == 200

        assert (await client.delete(f"/api/video-assets/{asset_id}",
                                    headers=bearer(owner_key))).status_code == 200
        assert (await client.get(url)).status_code == 404
        # 直接拼路径而不走 asset_dir()——后者会 mkdir，反而把刚删掉的目录建回来
        assert not (video_assets.assets_root()
                    / video_assets.asset_token_dir(asset_id)).exists()
        assert (await client.delete(f"/api/video-assets/{asset_id}",
                                    headers=bearer(owner_key))).status_code == 404


async def test_blank_name_rejected(tmp_path, monkeypatch):
    """纯空白名字 → 422：列表里一行空名等于这条素材从此找不回来。"""
    monkeypatch.setattr(settings, "DATA_DIR", str(tmp_path))
    async with rest_client(tmp_path, monkeypatch) as client:
        op_key = "op-assets-blank"
        op_id = await make_operator(op_key)
        clip_id = await _seed_done_clip(op_id)
        r = await client.post("/api/video-assets",
                              json={"clip_id": clip_id, "name": "   "},
                              headers=bearer(op_key))
        assert r.status_code == 422


async def test_endpoints_require_apikey(tmp_path, monkeypatch):
    async with rest_client(tmp_path, monkeypatch) as client:
        for method, path in (("post", "/api/video-assets"),
                             ("get", "/api/video-assets"),
                             ("get", "/api/video-assets/va_x"),
                             ("delete", "/api/video-assets/va_x")):
            r = await client.request(method.upper(), path, json={})
            assert r.status_code == 401, f"{method} {path} → {r.status_code}"


# ── 迁移 ────────────────────────────────────────────────────────────────────
def _table_names(db_file: str) -> set[str]:
    conn = sqlite3.connect(db_file)
    try:
        return {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    finally:
        conn.close()


@pytest.mark.parametrize("pre_create", [False, True])
def test_migration_up_down(monkeypatch, tmp_path, pre_create):
    """upgrade head 建表 / downgrade 删表；**防御式**：表已存在时 upgrade 不炸。

    pre_create=True 复刻「生产库被 create_all 先建好表、后补跑迁移」的历史坑
    （部署漏跑 alembic 的教训），这条走一遍 inspect 分支。
    """
    db_file = str(tmp_path / f"mig-{pre_create}.db")
    monkeypatch.setattr(settings, "DATABASE_URL", f"sqlite+aiosqlite:///{db_file}")
    cfg = Config(str(_REPO_ROOT / "alembic.ini"))

    if pre_create:
        command.upgrade(cfg, _PREV_REVISION)
        conn = sqlite3.connect(db_file)
        conn.execute("CREATE TABLE video_assets (id INTEGER PRIMARY KEY)")
        conn.commit()
        conn.close()

    command.upgrade(cfg, "head")
    tables = _table_names(db_file)
    assert "video_assets" in tables
    assert "video_clips" in tables          # 前序迁移不受牵连

    command.downgrade(cfg, _PREV_REVISION)
    assert "video_assets" not in _table_names(db_file)
    assert "video_clips" in _table_names(db_file)
