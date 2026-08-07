"""分片上传通道(视频 / 音频通用)单测:服务层 + REST 三端点。

为什么要分片(不是想复杂):mcp.nbdpsy.com 走 Cloudflare Tunnel,**单请求体上限 100MB**,
而用户要传 15-30 分钟的 GB 级视频、≤1GB 的播客音频 —— 单发 POST 必死在隧道层。

本文件锁的是分片通道最容易静默出错的地方:
- **缺片必须在 complete 当场被逮住**,不能等发布时才炸(所以每片独立落盘 + 集合校验,
  而不是往稀疏文件里按 offset 写 —— 那样写完最后一片文件就已是 total_size 长,
  长度校验形同虚设、空洞验不出来);
- 同 index 重传幂等(网络抖动重发是常态);
- 越界 index / 片长不符 / 超体积上限 / 扩展名不在白名单,一律当场拒绝;
- 未完成 session 有 TTL,不会把磁盘吃满。
"""

import hashlib

import pytest

from app.services import media_upload as mu


@pytest.fixture()
def root(tmp_path, monkeypatch):
    """把上传根目录指到隔离临时目录(不碰真 DATA_DIR)。"""
    monkeypatch.setattr(mu.settings, "DATA_DIR", str(tmp_path))
    return tmp_path


# ---------------- kind 归类与白名单 ----------------


def test_kind_classified_by_extension():
    """按扩展名自动归类 video / audio —— 调用方不必传 kind。"""
    assert mu.classify_media_kind("片子.mp4") == "video"
    assert mu.classify_media_kind("A.MOV") == "video"
    assert mu.classify_media_kind("播客.m4a") == "audio"
    assert mu.classify_media_kind("节目.MP3") == "audio"


def test_unknown_extension_rejected():
    """不在任何白名单里 → None(调用方据此 422),绝不放行未知格式占磁盘。"""
    assert mu.classify_media_kind("木马.exe") is None
    assert mu.classify_media_kind("没有扩展名") is None


# ---------------- 创建 session ----------------


def test_create_session_returns_server_chosen_chunk_size(root):
    """chunk_size 由**服务端**定:客户端要得再大也压到隧道安全线以下。

    隧道单请求 100MB 是硬墙,客户端自作主张传 200MB 分片只会在隧道层被砍,
    而且报的是网关错误、根本查不到我们这儿。
    """
    s = mu.create_session("a.mp4", total_size=500 * 1024 * 1024,
                          operator_id=1, chunk_size=999 * 1024 * 1024)
    assert s["chunk_size"] <= mu.MAX_CHUNK_BYTES
    assert s["kind"] == "video"
    assert s["chunk_count"] == mu.chunk_count(500 * 1024 * 1024, s["chunk_size"])


def test_create_session_rejects_bad_extension(root):
    with pytest.raises(ValueError, match="格式"):
        mu.create_session("x.exe", total_size=10, operator_id=1)


def test_create_session_rejects_oversize(root, monkeypatch):
    """超体积上限当场拒 —— 别等传了 3 个小时才说不行。"""
    monkeypatch.setattr(mu.settings, "UPLOAD_MEDIA_MAX_MB", 1)
    with pytest.raises(ValueError, match="上限"):
        mu.create_session("a.mp4", total_size=2 * 1024 * 1024, operator_id=1)


def test_audio_has_its_own_cap(root, monkeypatch):
    """音频上限独立(平台 1GB),不跟视频那个 4GB 混用。"""
    monkeypatch.setattr(mu.settings, "UPLOAD_MEDIA_MAX_MB", 4096)
    monkeypatch.setattr(mu.settings, "UPLOAD_AUDIO_MAX_MB", 1)
    with pytest.raises(ValueError, match="上限"):
        mu.create_session("a.mp3", total_size=2 * 1024 * 1024, operator_id=1)
    # 视频不受音频上限影响
    assert mu.create_session("a.mp4", total_size=2 * 1024 * 1024, operator_id=1)


def test_zero_cap_means_unlimited(root, monkeypatch):
    monkeypatch.setattr(mu.settings, "UPLOAD_MEDIA_MAX_MB", 0)
    assert mu.create_session("a.mp4", total_size=99 * 1024**3, operator_id=1)


# ---------------- 写分片 ----------------


def _mk(root, data: bytes, chunk_size: int = 4):
    s = mu.create_session("a.mp4", total_size=len(data), operator_id=1,
                          chunk_size=chunk_size)
    return s, [data[i:i + chunk_size] for i in range(0, len(data), chunk_size)]


def test_chunks_can_arrive_out_of_order(root):
    """乱序到达照样拼对 —— 客户端并发上传时乱序是常态,不是异常。"""
    data = b"0123456789AB"
    s, parts = _mk(root, data)
    for i in reversed(range(len(parts))):
        mu.write_chunk(s["upload_id"], i, parts[i], operator_id=1)
    out = mu.complete_session(s["upload_id"], operator_id=1)
    assert open(out["path"], "rb").read() == data


def test_same_index_retransmit_is_idempotent(root):
    """同 index 重传覆盖,不追加、不重复 —— 网络抖动重发不该把文件搞坏。"""
    data = b"0123456789AB"
    s, parts = _mk(root, data)
    for i, p in enumerate(parts):
        mu.write_chunk(s["upload_id"], i, p, operator_id=1)
    mu.write_chunk(s["upload_id"], 1, parts[1], operator_id=1)  # 重传中间一片
    mu.write_chunk(s["upload_id"], 1, parts[1], operator_id=1)
    out = mu.complete_session(s["upload_id"], operator_id=1)
    assert open(out["path"], "rb").read() == data


def test_index_out_of_range_rejected(root):
    data = b"0123456789AB"
    s, parts = _mk(root, data)
    with pytest.raises(ValueError, match="index"):
        mu.write_chunk(s["upload_id"], len(parts), b"x", operator_id=1)
    with pytest.raises(ValueError, match="index"):
        mu.write_chunk(s["upload_id"], -1, b"x", operator_id=1)


def test_wrong_chunk_length_rejected(root):
    """非末片长度必须等于 chunk_size —— 长度不对多半是客户端切片逻辑错了。"""
    data = b"0123456789AB"
    s, _parts = _mk(root, data)
    with pytest.raises(ValueError, match="长度"):
        mu.write_chunk(s["upload_id"], 0, b"xy", operator_id=1)  # 该 4 字节


def test_last_chunk_may_be_short(root):
    """末片天然短一截,不许因此报错。"""
    data = b"0123456789"  # 10 字节,chunk 4 → 4/4/2
    s, parts = _mk(root, data)
    for i, p in enumerate(parts):
        mu.write_chunk(s["upload_id"], i, p, operator_id=1)
    assert open(mu.complete_session(s["upload_id"], operator_id=1)["path"], "rb").read() == data


# ---------------- complete 的缺片检测 ----------------


def test_missing_chunk_detected_at_complete(root):
    """**缺片必须当场被逮住** —— 这是整个设计选"每片独立落盘"而不是稀疏写的理由。"""
    data = b"0123456789AB"
    s, parts = _mk(root, data)
    for i, p in enumerate(parts):
        if i == 1:
            continue  # 故意漏掉中间一片
        mu.write_chunk(s["upload_id"], i, p, operator_id=1)
    with pytest.raises(ValueError, match="缺"):
        mu.complete_session(s["upload_id"], operator_id=1)


def test_sha256_mismatch_detected(root):
    """给了 sha256 就校验;对不上一律拒绝,绝不把损坏的文件交出去。"""
    data = b"0123456789AB"
    s, parts = _mk(root, data)
    for i, p in enumerate(parts):
        mu.write_chunk(s["upload_id"], i, p, operator_id=1)
    with pytest.raises(ValueError, match="sha256"):
        mu.complete_session(s["upload_id"], operator_id=1, sha256="deadbeef")
    # 正确的 sha256 放行
    ok = mu.complete_session(
        s["upload_id"], operator_id=1, sha256=hashlib.sha256(data).hexdigest())
    assert open(ok["path"], "rb").read() == data


# ---------------- 归属与 TTL ----------------


def test_other_operator_cannot_touch_session(root):
    """别人的 session 既写不得也 complete 不得(上传通道也是访问控制的一部分)。"""
    s, parts = _mk(root, b"0123456789AB")
    with pytest.raises(PermissionError):
        mu.write_chunk(s["upload_id"], 0, parts[0], operator_id=999)
    with pytest.raises(PermissionError):
        mu.complete_session(s["upload_id"], operator_id=999)


def test_sweep_removes_only_expired_sessions(root, monkeypatch):
    """TTL 清理只删过期的未完成 session,没到期的一个都不许动。"""
    import os
    import time

    fresh = mu.create_session("a.mp4", total_size=4, operator_id=1, chunk_size=4)
    stale = mu.create_session("b.mp4", total_size=4, operator_id=1, chunk_size=4)
    old = time.time() - 25 * 3600
    os.utime(mu.session_dir(stale["upload_id"]), (old, old))

    removed = mu.sweep_expired_sessions(ttl_hours=24)

    assert removed == 1
    assert mu.session_dir(fresh["upload_id"]).exists()
    assert not mu.session_dir(stale["upload_id"]).exists()


def test_unknown_session_raises_not_found(root):
    from app.core.errors import NotFoundError

    with pytest.raises(NotFoundError):
        mu.complete_session("根本没有这个 id", operator_id=1)


def test_complete_is_idempotent(root):
    """重复 complete → 返回同一个 path,不报错不重拼。

    这不是"顺手加的健壮性":网络抖动下调用方**必然**会重试 complete(它拿不到响应时
    无法区分"没执行"和"执行了但响应丢了")。而 concat 成功后分片碎片当场就清掉了,
    第二次调用如果照旧走校验就会报"分片不齐"—— 一个已经成功的上传被报成失败。
    """
    data = b"0123456789AB"
    s, parts = _mk(root, data)
    for i, p in enumerate(parts):
        mu.write_chunk(s["upload_id"], i, p, operator_id=1)

    first = mu.complete_session(s["upload_id"], operator_id=1)
    second = mu.complete_session(s["upload_id"], operator_id=1)

    assert second["path"] == first["path"]
    assert second["size"] == first["size"] == len(data)
    assert second.get("already_completed") is True
    assert open(second["path"], "rb").read() == data


def test_completed_session_still_checks_ownership(root):
    """幂等不等于放行:别人的已完成 session 照样不许读到 path。"""
    data = b"0123456789AB"
    s, parts = _mk(root, data)
    for i, p in enumerate(parts):
        mu.write_chunk(s["upload_id"], i, p, operator_id=1)
    mu.complete_session(s["upload_id"], operator_id=1)

    with pytest.raises(PermissionError):
        mu.complete_session(s["upload_id"], operator_id=999)


def test_parts_removed_right_after_concat(root):
    """分片碎片在 concat 成功后**当场**清掉,不等 24h TTL。

    GB 级临时分片多躺一天就是白占一天盘;瞬时水位已经是 2× 文件大小了。
    """
    data = b"0123456789AB"
    s, parts = _mk(root, data)
    for i, p in enumerate(parts):
        mu.write_chunk(s["upload_id"], i, p, operator_id=1)
    mu.complete_session(s["upload_id"], operator_id=1)

    leftovers = list(mu.session_dir(s["upload_id"]).glob("*.part"))
    assert leftovers == [], f"分片没清干净: {leftovers}"


# ---------------- REST 三端点(端到端走一遍真分片流程) ----------------


async def test_rest_full_chunked_roundtrip(tmp_path, monkeypatch):
    """开会话 → 乱序 PUT 分片 → complete → 拿到的 path 能直接当 publish 的 video 参数。"""
    import hashlib as _h

    from tests.rest_helpers import ADMIN_KEY, bearer, rest_client

    async with rest_client(tmp_path, monkeypatch) as c:
        monkeypatch.setattr(mu.settings, "DATA_DIR", str(tmp_path))
        data = b"A" * 10 + b"B" * 6

        r = await c.post(
            "/api/uploads/media-sessions",
            json={"filename": "片子.mp4", "total_size": len(data), "chunk_size": 10},
            headers=bearer(ADMIN_KEY),
        )
        assert r.status_code == 201, r.text
        sess = r.json()
        assert sess["kind"] == "video" and sess["chunk_count"] == 2
        uid = sess["upload_id"]

        # 故意先传第 1 片再传第 0 片(乱序是常态)
        for idx in (1, 0):
            piece = data[idx * 10: idx * 10 + 10]
            r = await c.put(
                f"/api/uploads/media-sessions/{uid}/chunks/{idx}",
                content=piece, headers=bearer(ADMIN_KEY),
            )
            assert r.status_code == 200, r.text

        r = await c.post(
            f"/api/uploads/media-sessions/{uid}/complete",
            json={"sha256": _h.sha256(data).hexdigest()},
            headers=bearer(ADMIN_KEY),
        )
        assert r.status_code == 200, r.text
        out = r.json()
        assert open(out["path"], "rb").read() == data
        assert out["kind"] == "video"


async def test_rest_audio_session_accepted(tmp_path, monkeypatch):
    """音频走同一条通道(播客线要复用),kind 自动判成 audio。"""
    from tests.rest_helpers import ADMIN_KEY, bearer, rest_client

    async with rest_client(tmp_path, monkeypatch) as c:
        monkeypatch.setattr(mu.settings, "DATA_DIR", str(tmp_path))
        r = await c.post(
            "/api/uploads/media-sessions",
            json={"filename": "播客.m4a", "total_size": 8},
            headers=bearer(ADMIN_KEY),
        )
        assert r.status_code == 201, r.text
        assert r.json()["kind"] == "audio"


async def test_rest_bad_extension_422(tmp_path, monkeypatch):
    from tests.rest_helpers import ADMIN_KEY, bearer, rest_client

    async with rest_client(tmp_path, monkeypatch) as c:
        monkeypatch.setattr(mu.settings, "DATA_DIR", str(tmp_path))
        r = await c.post(
            "/api/uploads/media-sessions",
            json={"filename": "木马.exe", "total_size": 8},
            headers=bearer(ADMIN_KEY),
        )
        assert r.status_code == 422, r.text


async def test_rest_chunk_index_out_of_range_422(tmp_path, monkeypatch):
    from tests.rest_helpers import ADMIN_KEY, bearer, rest_client

    async with rest_client(tmp_path, monkeypatch) as c:
        monkeypatch.setattr(mu.settings, "DATA_DIR", str(tmp_path))
        r = await c.post(
            "/api/uploads/media-sessions",
            json={"filename": "a.mp4", "total_size": 4, "chunk_size": 4},
            headers=bearer(ADMIN_KEY),
        )
        uid = r.json()["upload_id"]
        r = await c.put(
            f"/api/uploads/media-sessions/{uid}/chunks/5",
            content=b"xxxx", headers=bearer(ADMIN_KEY),
        )
        assert r.status_code == 422, r.text


async def test_rest_unknown_session_404(tmp_path, monkeypatch):
    from tests.rest_helpers import ADMIN_KEY, bearer, rest_client

    async with rest_client(tmp_path, monkeypatch) as c:
        monkeypatch.setattr(mu.settings, "DATA_DIR", str(tmp_path))
        r = await c.post(
            "/api/uploads/media-sessions/nope/complete",
            json={}, headers=bearer(ADMIN_KEY),
        )
        assert r.status_code == 404, r.text


async def test_rest_requires_apikey(tmp_path, monkeypatch):
    """分片通道也是 /api/*,免鉴权白名单里没有它。"""
    from tests.rest_helpers import rest_client

    async with rest_client(tmp_path, monkeypatch) as c:
        r = await c.post("/api/uploads/media-sessions",
                         json={"filename": "a.mp4", "total_size": 8})
        assert r.status_code == 401, r.text


def test_creating_a_session_lazily_sweeps_expired_ones(root):
    """开新会话时顺手清过期弃单(同族 upload_service.save_images 一个路子)。

    不为它另养一个后台循环:分片只在有人上传时才产生,清理跟着上传走天然对齐,
    也就不必多一个 interval 配置。
    """
    import os
    import time

    stale = mu.create_session("b.mp4", total_size=4, operator_id=1, chunk_size=4)
    old = time.time() - 25 * 3600
    os.utime(mu.session_dir(stale["upload_id"]), (old, old))

    fresh = mu.create_session("c.mp4", total_size=4, operator_id=1, chunk_size=4)

    assert not mu.session_dir(stale["upload_id"]).exists()
    assert mu.session_dir(fresh["upload_id"]).exists()
