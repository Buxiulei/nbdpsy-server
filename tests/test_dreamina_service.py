"""即梦服务层单测：clip_id 形态 / 积分与登录态 / 参考图物化 / 产物 TTL 清理。

全离线：dreamina CLI 一律 monkeypatch ``app.services.dreamina._run_cli``（消费方命名空间），
httpx 一律 monkeypatch ``dreamina.httpx``——**绝不真跑 CLI、绝不发真网络请求**（真跑就是烧
公司积分 + 占即梦队列位）。
"""

import asyncio
import fcntl
import json
import os
import re
import socket
import time
from datetime import datetime, timedelta
from pathlib import Path

import httpx
import pytest

from app.core.config import settings
from app.models.video_clip import VideoClip
from app.services import dreamina

_PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 64
# 与 _PNG 字节不同的另一张合法 PNG：用来证明「远程下载」真的走了网络分支而不是读了本地同名文件
_PNG_REMOTE = b"\x89PNG\r\n\x1a\n" + b"1" * 64


def _stub_dns(monkeypatch, ip: str = "93.184.216.34"):
    """把域名解析钉死到指定 IP。

    单测不碰真 DNS（慢 + 网络不通时假红），但 SSRF 闸仍**留在调用链上**——正例用公网 IP
    证明闸不误伤，反例喂内网 IP 证明闸真拦得住。
    """
    def _fake(host, *_a, **_kw):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 443))]

    monkeypatch.setattr(dreamina.socket, "getaddrinfo", _fake)


@pytest.fixture(autouse=True)
def _clear_credit_cache():
    """每个用例前后清积分缓存，避免跨用例串味（缓存是进程级 60s）。"""
    dreamina.reset_credit_cache()
    yield
    dreamina.reset_credit_cache()


# ── clip_id 形态（需求第三节第 1 条 / 验收第 8 条）────────────────────────────
def test_clip_id_never_collides_with_cli_submit_id_shape():
    """clip_id 必须带 vc_ 前缀且**不是 16 位纯小写 hex**——撞 CLI submit_id 形态会让 skill 侧
    auto 模式把 server 的 clip 派到本机 CLI 查一个不存在的任务、空转到超时。"""
    ids = [dreamina.new_clip_id() for _ in range(200)]
    for cid in ids:
        assert cid.startswith("vc_"), cid
        assert not re.fullmatch(r"[0-9a-f]{16}", cid), f"撞 submit_id 形态: {cid}"
        assert re.fullmatch(r"vc_[0-9a-f]{10}", cid), cid
    assert len(set(ids)) == len(ids), "clip_id 应唯一"


def test_batch_id_shape():
    bid = dreamina.new_batch_id()
    assert re.fullmatch(r"vcb_[0-9a-f]{10}", bid)
    assert not re.fullmatch(r"[0-9a-f]{16}", bid)


def test_clip_token_dir_is_secret_derived_and_stable(monkeypatch):
    """同 clip_id 恒定派生同 token（跨进程可复算），不同 clip_id 不同。"""
    a1 = dreamina.clip_token_dir("vc_0123456789")
    a2 = dreamina.clip_token_dir("vc_0123456789")
    b = dreamina.clip_token_dir("vc_9876543210")
    assert a1 == a2 != b
    assert re.fullmatch(r"vc_[0-9a-f]{10}-[0-9a-f]{16}", a1)


# ── 积分 / 登录态 ───────────────────────────────────────────────────────────
def _stub_cli(monkeypatch, result):
    """把 _run_cli 换成固定返回，并记录调用次数。"""
    calls = []

    async def _fake(args, timeout):
        calls.append(list(args))
        return result

    monkeypatch.setattr(dreamina, "_run_cli", _fake)
    return calls


async def test_credit_status_ok_and_cached(monkeypatch):
    calls = _stub_cli(monkeypatch, (0, '{"total_credit": 1234}', ""))
    first = await dreamina.get_credit_status()
    assert first == {"logged_in": True, "credit": 1234, "error": None}
    # 60s 进程内缓存：第二次不再起子进程
    await dreamina.get_credit_status()
    assert len(calls) == 1
    # force 绕过缓存
    await dreamina.get_credit_status(force=True)
    assert len(calls) == 2


async def test_credit_status_cli_failure_means_logged_out(monkeypatch):
    """user_credit 跑不通 = 登录态失效 → logged_in=False（POST 据此 503，绝不静默排队）。"""
    _stub_cli(monkeypatch, (1, "", "not logged in"))
    status = await dreamina.get_credit_status()
    assert status["logged_in"] is False and status["credit"] is None
    assert "not logged in" in status["error"]


async def test_credit_status_unparsable_means_logged_out(monkeypatch):
    _stub_cli(monkeypatch, (0, "<html>login</html>", ""))
    assert (await dreamina.get_credit_status())["logged_in"] is False


async def test_credit_status_missing_credit_field_means_logged_out(monkeypatch):
    _stub_cli(monkeypatch, (0, '{"ok": true}', ""))
    assert (await dreamina.get_credit_status())["logged_in"] is False


def test_estimate_credit_is_linear_per_second():
    # 原断言写的是 4s==25、8s==50（「不足一档按一档」/ ceil(8/5)=2），那是把**块状取整**
    # 当成了正确行为；生产 vc_0cf759e417（2.5 / 4s）实扣 104 = 26×4 已证伪它——平台按秒计。
    # 故这两条改成按秒的 20 / 40，它们同时是能区分两种公式的判别点。
    assert dreamina.estimate_credit("seedance2.0fast", 5) == 25
    assert dreamina.estimate_credit("seedance2.0fast", 4) == 20       # 块状会算 25
    assert dreamina.estimate_credit("seedance2.0fast", 8) == 40       # 块状会算 50
    assert dreamina.estimate_credit("seedance2.0fast_vip", 5) == 55
    # seedance2.5 = 26/秒。4s / 7s / 29s 是判别点（块状分别算 130 / 182→260 / 780）。
    assert dreamina.estimate_credit("seedance2.5", 4) == 104          # 实测点 vc_0cf759e417
    assert dreamina.estimate_credit("seedance2.5", 7) == 182          # 块状会算 260
    assert dreamina.estimate_credit("seedance2.5", 29) == 754         # 块状会算 780
    # 5 的整数倍处两种公式同值，保留但没有判别力。
    assert dreamina.estimate_credit("seedance2.5", 5) == 130
    assert dreamina.estimate_credit("seedance2.5", 30) == 780
    # 无实测价的档一律不估、不给 warning（宁可不提示也不瞎猜）。
    assert dreamina.estimate_credit("seedance2.0", 5) is None
    assert dreamina.estimate_credit("seedance2.0mini", 5) is None


def test_max_duration_only_seedance25_reaches_thirty():
    assert dreamina.max_duration("seedance2.5") == 30
    for model in ("seedance2.0", "seedance2.0fast", "seedance2.0fast_vip",
                  "seedance2.0_vip", "seedance2.0mini"):
        assert dreamina.max_duration(model) == 15
    # 未知档按家族默认判（宁可窄不宜宽：放宽等于让一条 CLI 必拒的任务先建行再失败）
    assert dreamina.max_duration("seedance9.9") == 15
    assert dreamina.DEFAULT_MODEL == "seedance2.5"


# ── 参考图物化 ──────────────────────────────────────────────────────────────
class _FakeStream:
    def __init__(self, chunks):
        self._chunks = chunks

    def raise_for_status(self):
        return None

    async def aiter_bytes(self):
        for c in self._chunks:
            yield c


class _FakeHttpx:
    """只实现 materialize_ref_image 用到的两处：AsyncClient(...).stream() 与 HTTPError。"""

    HTTPError = httpx.HTTPError

    def __init__(self, chunks):
        self._chunks = chunks

    def _make_stream(self):
        """响应体工厂（子类覆写即可换成别的读取行为，如慢速滴流）。"""
        return _FakeStream(self._chunks)

    def AsyncClient(self, **_kw):  # noqa: N802 — 仿 httpx 的类名
        outer = self

        class _Client:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *_a):
                return False

            def stream(self, _method, _url):
                class _Ctx:
                    async def __aenter__(self):
                        return outer._make_stream()

                    async def __aexit__(self, *_a):
                        return False

                return _Ctx()

        return _Client()


async def test_materialize_from_uploads_makes_independent_copy(tmp_path, monkeypatch):
    """/uploads 参考图必须**复制**而非引用：图床 7 天清理，clip 的 TTL 是独立的。"""
    monkeypatch.setattr(settings, "DATA_DIR", str(tmp_path))
    src = tmp_path / "uploads" / "batchX"
    src.mkdir(parents=True)
    (src / "01.png").write_bytes(_PNG)
    workdir = tmp_path / "work"
    got = await dreamina.materialize_ref_image("/uploads/batchX/01.png", workdir)
    assert got.is_file() and got.parent == workdir
    (src / "01.png").unlink()                      # 图床过期清理
    assert got.read_bytes() == _PNG                # 副本仍在


async def test_materialize_rejects_local_path_and_missing_uploads(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "DATA_DIR", str(tmp_path))
    with pytest.raises(ValueError, match="只接受图床直链"):
        await dreamina.materialize_ref_image("./shots/ref.png", tmp_path / "w")
    with pytest.raises(ValueError, match="图床里找不到"):
        await dreamina.materialize_ref_image("/uploads/nope/01.png", tmp_path / "w")
    # 路径穿越：解析后越出 uploads 根 → 当"找不到"处理，不会读到 /etc
    with pytest.raises(ValueError):
        await dreamina.materialize_ref_image("/uploads/../../etc/passwd", tmp_path / "w")


async def test_materialize_http_ok(tmp_path, monkeypatch):
    _stub_dns(monkeypatch)
    monkeypatch.setattr(dreamina, "httpx", _FakeHttpx([_PNG[:8], _PNG[8:]]))
    got = await dreamina.materialize_ref_image("https://img.example/a.png", tmp_path / "w")
    assert got.suffix == ".png" and got.read_bytes() == _PNG


async def test_materialize_http_rejects_non_image(tmp_path, monkeypatch):
    """按**内容魔数**判定而非扩展名：坏图/错误页当场拒收，不让它进 CLI。"""
    _stub_dns(monkeypatch)
    monkeypatch.setattr(dreamina, "httpx", _FakeHttpx([b"<html>404 not found</html>"]))
    with pytest.raises(ValueError, match="不是有效图片"):
        await dreamina.materialize_ref_image("https://img.example/a.png", tmp_path / "w")


async def test_materialize_http_rejects_oversize(tmp_path, monkeypatch):
    _stub_dns(monkeypatch)
    monkeypatch.setattr(settings, "CLIP_IMAGE_MAX_MB", 1)
    monkeypatch.setattr(dreamina, "httpx", _FakeHttpx([b"x" * (1024 * 1024 + 1)]))
    with pytest.raises(ValueError, match="上限"):
        await dreamina.materialize_ref_image("https://img.example/big.png", tmp_path / "w")


@pytest.mark.parametrize("ip", ["127.0.0.1", "10.0.0.5", "192.168.1.7", "172.16.0.9",
                                "169.254.169.254", "0.0.0.0"])
async def test_materialize_rejects_internal_addresses(tmp_path, monkeypatch, ip):
    """SSRF 闸：参考图 URL 解析到内网/环回/链路本地一律拒收，且**根本不发请求**。

    不设闸的话这个端点就是个任意内网探测器——服务端会替调用方去 GET
    ``http://127.0.0.1:8000/api/...``、云元数据 ``169.254.169.254``、``10.x`` 内网服务，
    响应内容还会以「不是有效图片」的错误形式回灌给调用方。
    """
    _stub_dns(monkeypatch, ip)

    def _boom(**_kw):
        raise AssertionError("闸没拦住，已经发出请求了")

    monkeypatch.setattr(dreamina, "httpx", type("X", (), {
        "HTTPError": httpx.HTTPError, "AsyncClient": staticmethod(_boom)})())
    with pytest.raises(ValueError, match="内网地址"):
        await dreamina.materialize_ref_image("https://img.example/a.png", tmp_path / "w")


async def test_materialize_http_uploads_path_goes_remote_not_local(tmp_path, monkeypatch):
    """**判来源看 scheme，不看 path 形状**：``https://host/uploads/...`` 走远程下载。

    改前是「URL 的 path 以 /uploads 开头就当本服务图床去本地找」——两个反效果：
    ① 本服务公网域名的完整直链（运营手上最顺手的那种）会被拿去本地找、找不到就报「图床里
    找不到」；② 任意主机的 ``https://evil.example/uploads/x.png`` 会被误认成本服务的图。
    """
    _stub_dns(monkeypatch)
    monkeypatch.setattr(settings, "DATA_DIR", str(tmp_path))
    local = tmp_path / "uploads" / "b7"
    local.mkdir(parents=True)
    (local / "01.png").write_bytes(_PNG)               # 同名本地文件，内容不同
    monkeypatch.setattr(dreamina, "httpx", _FakeHttpx([_PNG_REMOTE]))

    got = await dreamina.materialize_ref_image(
        "https://mcp.nbdpsy.com/uploads/b7/01.png", tmp_path / "w")

    assert got.read_bytes() == _PNG_REMOTE, "http(s) 一律走远程下载，不该读到本地同路径文件"


async def test_materialize_http_total_time_is_capped(tmp_path, monkeypatch):
    """慢速滴流的图床拖不垮提交：整段下载有**总时长**上限（httpx 的 timeout 是每次操作各自计时）。"""
    _stub_dns(monkeypatch)
    monkeypatch.setattr(dreamina, "_REMOTE_FETCH_TIMEOUT", 0.2)

    class _DripStream(_FakeStream):
        """每 50ms 滴一个字节、永不结束的响应体（每次读都不超时，但整段永远读不完）。"""

        async def aiter_bytes(self):
            while True:
                await asyncio.sleep(0.05)
                yield b"\x89"

    class _DripHttpx(_FakeHttpx):
        def _make_stream(self):
            return _DripStream([])

    monkeypatch.setattr(dreamina, "httpx", _DripHttpx([]))
    with pytest.raises(ValueError, match="下载超时"):
        await dreamina.materialize_ref_image("https://img.example/a.png", tmp_path / "w")


# ── 提交参数组装 ────────────────────────────────────────────────────────────
def _clip(**kw) -> VideoClip:
    base = dict(clip_id="vc_0000000001", operation="text2video", prompt="温暖诊室空镜",
                model="seedance2.0fast", duration=5, ratio="9:16", created_by=1)
    base.update(kw)
    return VideoClip(**base)


def test_build_submit_args_matrix():
    t2v = dreamina.build_submit_args(_clip())
    assert t2v[0] == "text2video"
    assert "--ratio=9:16" in t2v and "--poll=0" in t2v and "--video_resolution=720p" in t2v
    assert "--duration=5" in t2v and "--model_version=seedance2.0fast" in t2v

    # image2video：带 --image，**绝不带 --ratio**（CLI 不收，画幅由输入图推断）
    i2v = dreamina.build_submit_args(
        _clip(operation="image2video", ratio=None, image_path="/tmp/ref.png"))
    assert i2v[0] == "image2video" and "--image=/tmp/ref.png" in i2v
    assert not any(a.startswith("--ratio") for a in i2v)

    m2v = dreamina.build_submit_args(
        _clip(operation="multimodal2video", image_path="/tmp/ref.png"))
    assert m2v[0] == "multimodal2video" and "--image=/tmp/ref.png" in m2v
    assert "--ratio=9:16" in m2v


def test_is_revivable_only_for_rows_that_never_ran_cli():
    """能复活的**只有**从没让 CLI 跑过的 error 行；碰过 CLI 的一律不复活（资金状态未知）。

    判据必须是结构化字段：认领进 submitting 的那一刻就写 submitted_at，故它为空 ⟺ CLI
    从没为这行跑过。靠解析 error 文案区分是禁止的——文案会被截断、会带 CLI 原文、会改。
    """
    # 物化参考图失败落的 error 行：CLI 一次都没被调起 → 可安全复活
    assert dreamina.is_revivable(
        _clip(status="error", error="参考图在本服务图床里找不到"))

    # 歧义结局（超时 / rc=0 无 submit_id）：认领过 → submitted_at 非空 → 绝不复活
    assert not dreamina.is_revivable(
        _clip(status="error", error=dreamina.AMBIGUOUS_HINT, submitted_at=datetime.utcnow()))
    # CLI 明确失败 / 合规授权失败：同样认领过
    assert not dreamina.is_revivable(
        _clip(status="error", error="Error: quota exceeded", submitted_at=datetime.utcnow()))
    # 已拿到 submit_id：确定入了即梦队列，复活 = 双倍扣分
    assert not dreamina.is_revivable(
        _clip(status="error", submit_id="3d64c2221c0e07da", submitted_at=datetime.utcnow()))
    # 非 error 的行一律不碰（在飞任务被「复活」成 queued 就是二次提交）
    for state in ("queued", "submitting", "submitted", "querying", "done"):
        assert not dreamina.is_revivable(_clip(status=state))


def test_extract_submit_id():
    assert dreamina.extract_submit_id({"submit_id": "3d64c2221c0e07da"}, "") == "3d64c2221c0e07da"
    assert dreamina.extract_submit_id({"data": {"submit_id": "abc"}}, "") == "abc"
    assert dreamina.extract_submit_id(None, "task 3d64c2221c0e07da queued") == "3d64c2221c0e07da"
    assert dreamina.extract_submit_id(None, "no id here") is None


def test_extract_submit_id_fallback_is_anchored_and_optional():
    """兜底正则的两道闸：**恰好 16 位**（32 位 md5/logid 不算）+ ``blob=None`` 可整个关掉。

    不锚定时 `9f8e...` 这类 32 位 request_id 的前 16 位会被当成 submit_id；关不掉的话
    CLI 明确失败的原文也会被翻出个假 id 来，把「确定失败」伪装成「在排队」。"""
    long_hex = "9f8e7d6c5b4a39281726354453627180"          # 32 位，不是 submit_id
    assert dreamina.extract_submit_id(None, f"request_id={long_hex}") is None
    assert dreamina.extract_submit_id(None, "logid=0123456789abcdef!") == "0123456789abcdef"
    # blob=None：只认结构化字段
    assert dreamina.extract_submit_id(None, None) is None
    assert dreamina.extract_submit_id(None, None) is None
    assert dreamina.extract_submit_id(
        {"submit_id": "3d64c2221c0e07da"}, None) == "3d64c2221c0e07da"


# ── 跨进程 CLI 锁 ───────────────────────────────────────────────────────────
async def test_cli_file_lock_blocks_other_process(tmp_path, monkeypatch):
    """文件锁真能挡住**另一个 OS 进程**（asyncio.Lock 挡不住 api / worker 两个 systemd 单元）。

    这里用子进程持锁，主进程走真实的 `_run_cli` 路径：CLI 一次都不该被调起，
    回 `_RC_LOCK_BUSY` 而不是超时（后者语义是「歧义结局」，会白白判 error）。"""
    import subprocess
    import sys

    monkeypatch.setattr(settings, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(dreamina, "_CLI_LOCK_WAIT_SECONDS", 0.5)
    lock_file = tmp_path / dreamina._CLI_LOCK_FILE
    lock_file.parent.mkdir(parents=True, exist_ok=True)
    # 绝不指向真 dreamina：真跑就是提交真任务、烧公司积分
    monkeypatch.setattr(settings, "DREAMINA_BIN", "/bin/true")

    holder = subprocess.Popen(
        [sys.executable, "-c",
         f"import fcntl,time;fh=open({str(lock_file)!r},'a+');"
         "fcntl.flock(fh.fileno(),fcntl.LOCK_EX);time.sleep(5)"])
    try:
        for _ in range(50):                      # 等子进程真拿到锁
            await asyncio.sleep(0.1)
            try:
                with open(lock_file, "a+") as probe:
                    fcntl.flock(probe.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    fcntl.flock(probe.fileno(), fcntl.LOCK_UN)
            except BlockingIOError:
                break
        rc, out, err = await dreamina._run_cli(["user_credit"], timeout=30)
    finally:
        holder.kill()
        holder.wait()

    assert rc == dreamina._RC_LOCK_BUSY, (rc, out, err)
    assert "等锁超时" in err
    # 锁放开后同样的调用能跑通（/bin/true → rc=0）
    assert (await dreamina._run_cli(["user_credit"], timeout=30))[0] == 0


async def test_credit_status_lock_busy_keeps_last_verdict(monkeypatch):
    """CLI 忙 ≠ 登录失效：沿用上次结论且**不刷新缓存**（判 false 会缓存 60s，期间提交全 503）。"""
    _stub_cli(monkeypatch, (0, '{"total_credit": 900}', ""))
    assert (await dreamina.get_credit_status())["logged_in"] is True

    _stub_cli(monkeypatch, (dreamina._RC_LOCK_BUSY, "", "等锁超时"))
    busy = await dreamina.get_credit_status(force=True)
    assert busy == {"logged_in": True, "credit": 900, "error": None}

    # 缓存没被 busy 覆盖：锁一放开，下一次查询照常拿到新余额
    _stub_cli(monkeypatch, (0, '{"total_credit": 800}', ""))
    assert (await dreamina.get_credit_status(force=True))["credit"] == 800


async def test_credit_status_lock_busy_without_cache_is_not_cached(monkeypatch):
    """冷启动就撞上 CLI 忙：只能报未登录，但**绝不写缓存**——下次请求要能立刻重试。"""
    _stub_cli(monkeypatch, (dreamina._RC_LOCK_BUSY, "", "等锁超时"))
    assert (await dreamina.get_credit_status())["logged_in"] is False

    calls = _stub_cli(monkeypatch, (0, '{"total_credit": 700}', ""))
    assert (await dreamina.get_credit_status())["credit"] == 700
    assert len(calls) == 1, "busy 的结论不该被缓存 60s"


# ── 产物 TTL 清理 ──────────────────────────────────────────────────────────
async def _seed_clip(factory, **kw) -> int:
    async with factory() as s:
        clip = VideoClip(**{**dict(
            clip_id=dreamina.new_clip_id(), operation="text2video", prompt="p",
            model="seedance2.0fast", duration=5, status="queued", created_by=1), **kw})
        s.add(clip)
        await s.commit()
        return clip.id


async def test_reap_removes_expired_products_but_keeps_ledger(db_factory, tmp_path, monkeypatch):
    """过期只删产物：status 保持 done、credit_count **保留**（积分对账要用），video_url 清空。"""
    monkeypatch.setattr(settings, "DATA_DIR", str(tmp_path))
    expired_id = await _seed_clip(
        db_factory, status="done", credit_count=25,
        expires_at=datetime.utcnow() - timedelta(days=1))
    fresh_id = await _seed_clip(
        db_factory, status="done", credit_count=55,
        expires_at=datetime.utcnow() + timedelta(days=1))
    async with db_factory() as s:
        for row_id in (expired_id, fresh_id):
            clip = await s.get(VideoClip, row_id)
            d = dreamina.clip_dir(clip.clip_id)
            (d / "clip.mp4").write_bytes(b"mp4")
            clip.video_url = dreamina.clip_public_url(clip.clip_id, "clip.mp4")
            clip.video_path = str(d / "clip.mp4")
        await s.commit()

    assert await dreamina.reap_clips_once(db_factory) == 1

    async with db_factory() as s:
        expired = await s.get(VideoClip, expired_id)
        fresh = await s.get(VideoClip, fresh_id)
        assert expired.status == "done" and expired.credit_count == 25
        assert expired.video_url is None and expired.video_path is None
        # **error 不装清理说明**：那一格只表示「任务失败」，掺进「产物过期了」会让运营和
        # skill 侧的 error 分支把一条成功的片当成失败。过期与否走 GET 视图的 expired 键。
        assert expired.error is None
        assert not (dreamina.clips_root() / dreamina.clip_token_dir(expired.clip_id)).exists()
        # 未过期的一条完全不受影响
        assert fresh.video_url and Path(fresh.video_path).is_file()

    # 幂等：再跑一轮无可清理（video_url 已为 NULL）
    assert await dreamina.reap_clips_once(db_factory) == 0


async def test_reap_removes_error_workdirs_past_ttl(db_factory, tmp_path, monkeypatch):
    """error 终态的工作目录也要按 TTL 收（**行保留**）。

    不收的话参考图副本会永久堆盘：失败越多堆越多，而 done 分支根本扫不到这些行
    （它只看 status='done' + video_url 非空），没有任何人来收。
    """
    monkeypatch.setattr(settings, "DATA_DIR", str(tmp_path))
    old_id = await _seed_clip(db_factory, status="error", error="内容审核未通过",
                              finished_at=datetime.utcnow() - timedelta(days=99))
    new_id = await _seed_clip(db_factory, status="error", error="内容审核未通过",
                              finished_at=datetime.utcnow())
    async with db_factory() as s:
        dirs = {}
        for row_id in (old_id, new_id):
            clip = await s.get(VideoClip, row_id)
            d = dreamina.clip_dir(clip.clip_id)
            (d / "ref.png").write_bytes(_PNG)
            dirs[row_id] = d

    assert await dreamina.reap_clips_once(db_factory) == 1

    assert not dirs[old_id].exists(), "超 TTL 的 error 工作目录该删"
    assert (dirs[new_id] / "ref.png").is_file(), "刚失败的行还在 TTL 内，目录不该动"
    async with db_factory() as s:
        # 行必须留着：error 文案是运营复盘 / 对账的依据，删行等于毁证
        assert (await s.get(VideoClip, old_id)).error == "内容审核未通过"

    assert await dreamina.reap_clips_once(db_factory) == 0, "目录已删，不该每轮虚报"


async def test_reap_removes_orphan_dirs_but_spares_fresh_ones(db_factory, tmp_path,
                                                              monkeypatch):
    """无主孤儿目录（没有对应 DB 行）按 mtime 收；**新鲜的孤儿不碰**。

    孤儿来自「先建目录物化参考图、再插行」这个顺序中途失败（校验 4xx / 进程崩）。
    卡 TTL 而不是「没行就删」，正是为了不误杀那几秒里正在物化、行还没插进去的目录——
    否则并发提交会被清理线程把参考图从脚下抽走。
    """
    monkeypatch.setattr(settings, "DATA_DIR", str(tmp_path))
    live_id = await _seed_clip(db_factory, status="queued")
    async with db_factory() as s:
        live_dir = dreamina.clip_dir((await s.get(VideoClip, live_id)).clip_id)
    (live_dir / "ref.png").write_bytes(_PNG)

    stale_orphan = dreamina.clip_dir("vc_0000000099")      # 无 DB 行
    fresh_orphan = dreamina.clip_dir("vc_0000000098")      # 无 DB 行，但刚建（正在物化中）
    (stale_orphan / "ref.png").write_bytes(_PNG)
    (fresh_orphan / "ref.png").write_bytes(_PNG)
    old = time.time() - (settings.CLIP_TTL_DAYS + 1) * 86400
    os.utime(stale_orphan, (old, old))

    assert await dreamina.reap_clips_once(db_factory) == 1

    assert not stale_orphan.exists()
    assert fresh_orphan.exists(), "刚建的孤儿目录可能正在物化，绝不能删"
    assert live_dir.exists(), "有 DB 行的目录不是孤儿"


# ── 多图参考 / 首尾帧（CLI 能力面开放）──────────────────────────────────────
def test_max_ref_images_is_per_model():
    """参考图张数上限**按模型分档**：2.5 是 30，2.0 家族/mini 只有 9。

    网上流传的「Seedance 最多 9 张」是 **2.0** 的数字（CLI help 原文：seedance2.5 ->
    image<=30；seedance2.0 family/seedance2.0mini -> image<=9），套到 2.5 上会白白砍掉
    我们锁人物一致性最需要的那 21 个槽。未知模型按 9 判——宁窄不宽。
    """
    assert dreamina.max_ref_images("seedance2.5") == 30
    for model in ("seedance2.0", "seedance2.0fast", "seedance2.0_vip",
                  "seedance2.0fast_vip", "seedance2.0mini", "未来某档"):
        assert dreamina.max_ref_images(model) == 9


def test_ref_paths_prefers_json_and_falls_back_to_legacy_column():
    """读参考图一律走 ref_paths：新行看 image_paths_json，多图列上线前的老行回落 image_path。"""
    legacy = _clip(operation="multimodal2video", image_path="/tmp/ref.png")
    assert dreamina.ref_paths(legacy) == ["/tmp/ref.png"]

    multi = _clip(operation="multimodal2video", image_path="/tmp/a.png",
                  image_paths_json='["/tmp/a.png", "/tmp/b.png", "/tmp/c.png"]')
    assert dreamina.ref_paths(multi) == ["/tmp/a.png", "/tmp/b.png", "/tmp/c.png"]

    assert dreamina.ref_paths(_clip()) == []
    # JSON 坏了不炸：回落单列（这列只承载路径，坏值不该让一条已建好的任务提不出去）
    broken = _clip(image_path="/tmp/ref.png", image_paths_json="{不是 JSON")
    assert dreamina.ref_paths(broken) == ["/tmp/ref.png"]


def test_build_submit_args_multi_image():
    """multimodal2video 多图：每张一个 --image（CLI 的 --image 是 stringArray，可重复）。"""
    args = dreamina.build_submit_args(_clip(
        operation="multimodal2video", image_path="/tmp/a.png",
        image_paths_json='["/tmp/a.png", "/tmp/b.png", "/tmp/c.png"]'))
    assert args[0] == "multimodal2video"
    assert [a for a in args if a.startswith("--image=")] == [
        "--image=/tmp/a.png", "--image=/tmp/b.png", "--image=/tmp/c.png"]
    assert "--ratio=9:16" in args


def test_build_submit_args_frames2video():
    """首尾帧：--first/--last 取 image_paths_json 的 [首, 尾]，**绝不带 --ratio**。

    CLI help 原文 "ratio is inferred from the first frame image size"——传了会被严格校验拒收。
    """
    args = dreamina.build_submit_args(_clip(
        operation="frames2video", ratio=None, model="seedance2.5", duration=8,
        image_path="/tmp/first.png",
        image_paths_json='["/tmp/first.png", "/tmp/last.png"]'))
    assert args[0] == "frames2video"
    assert "--first=/tmp/first.png" in args and "--last=/tmp/last.png" in args
    assert not any(a.startswith("--ratio") for a in args)
    assert "--video_resolution=720p" in args and "--poll=0" in args
    assert "--duration=8" in args and "--model_version=seedance2.5" in args
    assert not any(a.startswith("--image=") for a in args)


async def test_materialize_writes_distinct_copies_per_stem(tmp_path, monkeypatch):
    """多张参考图各自落成**独立副本**：文件名由 stem 区分，后一张不许盖掉前一张。"""
    monkeypatch.setattr(settings, "DATA_DIR", str(tmp_path))
    src = tmp_path / "uploads" / "refs"
    src.mkdir(parents=True)
    (src / "01.png").write_bytes(_PNG)
    (src / "02.png").write_bytes(_PNG + b"second")
    workdir = tmp_path / "w"

    first = await dreamina.materialize_ref_image("/uploads/refs/01.png", workdir)
    second = await dreamina.materialize_ref_image(
        "/uploads/refs/02.png", workdir, stem="ref_2")

    assert first.name == "ref.png", "单张默认名不变（老行为逐字节保持）"
    assert second.name == "ref_2.png"
    assert first.read_bytes() == _PNG and second.read_bytes() == _PNG + b"second"


# ── 多帧故事 multiframe2video ────────────────────────────────────────────────
def test_estimate_credit_is_unknown_for_multiframe():
    """multiframe2video 单价从未实测 → 一律返回 None，**绝不套用别的档硬算一个数**。

    这条 operation 的模型由平台固定，我们库里那个 model 值只是占位；拿它去查 _PRICE_PER_5S
    会算出 seedance2.5 的 130/5s，那是个凭空捏造的数字，会让运营按它做预算。
    """
    assert dreamina.estimate_credit("seedance2.5", 10) == 260          # 老口径不变
    assert dreamina.estimate_credit(
        "seedance2.5", 10, operation="multiframe2video") is None
    assert dreamina.estimate_credit(
        "seedance2.5", 10, operation="multimodal2video") == 260


def test_build_submit_args_multiframe_shorthand():
    """恰好 2 张的简写：--prompt + --duration，**不带任何 transition-***。

    也不带 --model_version（CLI help：model_version is fixed and is not configurable
    on this command）与 --ratio（ratio is inferred from the first image）。
    """
    args = dreamina.build_submit_args(_clip(
        operation="multiframe2video", ratio=None, duration=5,
        prompt="镜头从空椅缓缓推到窗外",
        image_path="/tmp/a.png", image_paths_json='["/tmp/a.png", "/tmp/b.png"]'))
    assert args[0] == "multiframe2video"
    # --images 是 strings（逗号连接），**不是** --image；写错 flag 名 CLI 直接拒
    assert "--images=/tmp/a.png,/tmp/b.png" in args
    assert "--prompt=镜头从空椅缓缓推到窗外" in args and "--duration=5" in args
    assert not any(a.startswith("--transition-") for a in args)
    assert not any(a.startswith("--model_version") for a in args)
    assert not any(a.startswith("--ratio") for a in args)
    assert "--video_resolution=720p" in args and "--poll=0" in args


def test_build_submit_args_multiframe_transitions():
    """3+ 张：逐段 --transition-prompt / --transition-duration 各 N-1 个，不带简写的两个 flag。"""
    args = dreamina.build_submit_args(_clip(
        operation="multiframe2video", ratio=None, duration=12, prompt="甲 → 乙 → 丙",
        image_path="/tmp/1.png",
        image_paths_json='["/tmp/1.png", "/tmp/2.png", "/tmp/3.png"]',
        transitions_json='[{"prompt": "甲转向乙", "duration": 4.0},'
                         ' {"prompt": "乙推近丙", "duration": 8.0}]'))
    assert args[0] == "multiframe2video"
    assert "--images=/tmp/1.png,/tmp/2.png,/tmp/3.png" in args
    assert [a for a in args if a.startswith("--transition-prompt=")] == [
        "--transition-prompt=甲转向乙", "--transition-prompt=乙推近丙"]
    assert [a for a in args if a.startswith("--transition-duration=")] == [
        "--transition-duration=4.0", "--transition-duration=8.0"]
    # 长式下简写的两个 flag 一个都不能出现（CLI 的 --prompt/--duration 是 2 张专用）
    assert not any(a.startswith("--prompt=") or a.startswith("--duration=") for a in args)


def test_build_submit_args_multiframe_omits_durations_when_unset():
    """没给逐段时长就**整个 flag 不出现**，让 CLI 按它自己的每段 3s 默认走。

    补一串我们自己编的 3.0 上去是多余的：CLI 的默认值将来若变，我们会把它钉死在旧值上。
    """
    args = dreamina.build_submit_args(_clip(
        operation="multiframe2video", ratio=None, duration=6, prompt="甲 → 乙 → 丙",
        image_path="/tmp/1.png",
        image_paths_json='["/tmp/1.png", "/tmp/2.png", "/tmp/3.png"]',
        transitions_json='[{"prompt": "甲转向乙", "duration": null},'
                         ' {"prompt": "乙推近丙", "duration": null}]'))
    assert len([a for a in args if a.startswith("--transition-prompt=")]) == 2
    assert not any(a.startswith("--transition-duration") for a in args)


async def test_compliance_models_exclude_multiframe_rows(db_factory):
    """合规观测列表**排除 multiframe 行**：那条 operation 的模型由平台固定，我们库里那个值
    是占位符，混进「真出过片的模型」里就不是「近似」而是污染——读的人会当它是个真实档位。
    """
    await _seed_clip(db_factory, status="done", model="seedance2.0fast_vip")
    await _seed_clip(db_factory, status="done", operation="multiframe2video",
                     model=dreamina.MULTIFRAME_MODEL_PLACEHOLDER)
    async with db_factory() as s:
        assert await dreamina.compliance_confirmed_models(s) == ["seedance2.0fast_vip"]


# ── 单价表：四次实测互证的 seedance2.5 按秒线性 ─────────────────────────────
def test_seedance25_price_is_linear_across_measured_durations():
    """2.5 = 26/秒，**四次独立生产实测互证**（vc_0cf759e417 4s=104、vc_3e1260f8ce 5s=130、
    vc_9090b4f40b 10s=260、vc_5d0ec24ff7 10s=260），故按**秒**线性、不按 5s 档取整。

    这条锁的是「默认档必须能估价」：2.5 是双端默认档，它估不出等于估算表对绝大多数提交无效。
    """
    assert dreamina.estimate_credit("seedance2.5", 4) == 104     # 判别点：块状会算 130
    assert dreamina.estimate_credit("seedance2.5", 5) == 130
    assert dreamina.estimate_credit("seedance2.5", 10) == 260
    assert dreamina.estimate_credit("seedance2.5", 30) == 780
    assert dreamina.price_per_5s("seedance2.5") == 130


def test_unmeasured_tiers_stay_unpriced():
    """没实测过的档**一律不估**（回归锁）：编一个「看着合理」的数会让预算护栏与 warning
    建立在一个没人验证过的常量上，而这条产线里每个估算都会被拿去做花钱决策。"""
    for model in ("seedance2.0", "seedance2.0_vip", "seedance2.0mini"):
        assert dreamina.estimate_credit(model, 5) is None
        assert dreamina.price_per_5s(model) is None
    assert dreamina.priced_models() == [
        "seedance2.0fast", "seedance2.0fast_vip", "seedance2.5"]


def test_multiframe_never_priced_even_on_a_priced_model():
    """回归锁：multiframe2video 恒 None，**哪怕 model 列是有价的档**。

    它的档由平台下发、CLI 不接受 --model_version，库里那列是占位符；本表对它不适用。
    """
    assert dreamina.estimate_credit("seedance2.5", 10, "multiframe2video") is None
    assert dreamina.estimate_credit(
        dreamina.MULTIFRAME_MODEL_PLACEHOLDER, 10, "multiframe2video") is None


# ── 段帧提取（分段续接）──────────────────────────────────────────────────────
def _make_video(path: Path, seconds: int = 2) -> Path:
    """真跑 ffmpeg 造一段 testsrc 视频（与 test_video_muxer 同款做法）。"""
    import subprocess
    subprocess.run(
        ["ffmpeg", "-y", "-f", "lavfi", "-i",
         f"testsrc=duration={seconds}:size=160x120:rate=10", "-pix_fmt", "yuv420p", str(path)],
        check=True, capture_output=True)
    return path


def test_frame_name_is_stable_per_t():
    """文件名即幂等键：同一个 t 必须映射到同一个名字，且形态被直链白名单认得。"""
    assert dreamina.frame_name("last") == "frame_last.png"
    assert dreamina.frame_name(3) == dreamina.frame_name(3.0) == "frame_3.000.png"
    assert dreamina.frame_name(2.5) == "frame_2.500.png"


async def test_extract_frame_last_and_at_second(tmp_path):
    video = _make_video(tmp_path / "clip.mp4")
    last = tmp_path / dreamina.frame_name("last")
    assert await dreamina.extract_frame(video, last, "last") is None
    assert last.stat().st_size > 0 and last.read_bytes().startswith(b"\x89PNG")

    at_one = tmp_path / dreamina.frame_name(1)
    assert await dreamina.extract_frame(video, at_one, 1) is None
    assert at_one.stat().st_size > 0
    # **t=last 必须是真末帧**，不是「末尾前 1s 那一帧」。2s 的片里 t=1 恰好落在尾部窗口的
    # 起点：两张一模一样就说明尾帧取法退化成了取窗口首帧（分段续接会因此跳掉一秒）。
    assert last.read_bytes() != at_one.read_bytes()
    tail = tmp_path / "tail-1.9.png"
    assert await dreamina.extract_frame(video, tail, 1.9) is None
    assert last.read_bytes() == tail.read_bytes(), "t=last 应等于片尾那一帧"


async def test_extract_frame_is_idempotent(tmp_path):
    """同 t 重复请求复用磁盘上那张，不再跑 ffmpeg（分段续接里重取是常态）。"""
    video = _make_video(tmp_path / "clip.mp4")
    out = tmp_path / dreamina.frame_name("last")
    assert await dreamina.extract_frame(video, out, "last") is None
    stamp = out.stat().st_mtime_ns
    assert await dreamina.extract_frame(video, out, "last") is None
    assert out.stat().st_mtime_ns == stamp, "已抽过的帧不该被重抽"


async def test_extract_frame_beyond_end_reports_error_and_leaves_no_half_image(tmp_path):
    """t 落在末帧之后：**返回错误且不留半张图**——留个 0 字节 PNG 会被下次幂等复用命中。"""
    video = _make_video(tmp_path / "clip.mp4")
    out = tmp_path / dreamina.frame_name(99)
    error = await dreamina.extract_frame(video, out, 99)
    assert error and not out.exists()


async def test_probe_duration_reads_real_length_and_tolerates_garbage(tmp_path):
    video = _make_video(tmp_path / "clip.mp4", seconds=2)
    assert abs(await dreamina.probe_duration(video) - 2.0) < 0.3
    junk = tmp_path / "not-a-video.mp4"
    junk.write_bytes(b"nope")
    assert await dreamina.probe_duration(junk) is None


# ── 参考视频物化（CLI 的 --video 输入面）────────────────────────────────────
# ISO BMFF：4-8 字节是 ftyp，随后是 major brand。mov 的 brand 以 "qt" 开头。
_MP4 = b"\x00\x00\x00\x20ftypisom" + b"\x00" * 32
_MOV = b"\x00\x00\x00\x14ftypqt  " + b"\x00" * 32
_WEBM = b"\x1a\x45\xdf\xa3" + b"\x00" * 32


def test_video_caps_are_per_model():
    """条数 / 总输入 / 时长三档全部按模型分档，未知档按 2.0 家族的窄档判。

    CLI help 原文：``seedance2.5 -> image<=30, video<=10, audio<=10, total inputs<=50;
    each and total video/audio duration 2-30s``；``seedance2.0 family/seedance2.0mini ->
    image<=9, video<=3, audio<=3, total inputs<=12``（时长 2-15s）。
    """
    assert dreamina.max_ref_videos("seedance2.5") == 10
    assert dreamina.max_ref_audios("seedance2.5") == 10
    assert dreamina.max_total_inputs("seedance2.5") == 50
    assert dreamina.max_ref_media_seconds("seedance2.5") == 30.0
    assert dreamina.allows_audio_only("seedance2.5") is True
    for model in ("seedance2.0", "seedance2.0fast", "seedance2.0fast_vip",
                  "seedance2.0_vip", "seedance2.0mini", "seedance9.9"):
        assert dreamina.max_ref_videos(model) == 3
        assert dreamina.max_ref_audios(model) == 3
        assert dreamina.max_total_inputs(model) == 12
        assert dreamina.max_ref_media_seconds(model) == 15.0
        # 纯音频只有 2.5 允许（2.0 家族 help 明写 at least one --image or --video）
        assert dreamina.allows_audio_only(model) is False


def test_total_inputs_cap_is_reachable_now_that_three_kinds_pass_through():
    """总输入闸**真会响**——videos 那一轮它还不可达，audio 一开就可达了。

    上一轮（只有图 + 视频）分项上限把合计钉死在 30+10=40≤50、9+3=12≤12，故当时没设闸、
    只留了一条推导守护测试。三类齐了之后 2.0 家族 9 图 + 3 视频 + 3 音频 = 15 > 12，
    分项条条合法而合计超限——这条断言那个组合确实越界，REST 层的总输入闸不是摆设
    （闸本身的行为由 test_total_inputs_ceiling_across_three_kinds 在端点上验）。
    """
    widest = ("seedance2.0fast", "seedance2.0mini")
    for model in widest:
        full = (dreamina.max_ref_images(model) + dreamina.max_ref_videos(model)
                + dreamina.max_ref_audios(model))
        assert full > dreamina.max_total_inputs(model), model
    # 2.5 那档三类拉满仍在总闸内（30+10+10=50 恰好顶格），故它只会被分项闸拦
    assert (dreamina.max_ref_images("seedance2.5") + dreamina.max_ref_videos("seedance2.5")
            + dreamina.max_ref_audios("seedance2.5")) == dreamina.max_total_inputs("seedance2.5")


def test_video_magic_whitelist_is_separate_from_image_whitelist():
    """视频走**独立**魔数白名单：图片那条是安全闸，绝不为了收视频把它放宽。"""
    assert dreamina._sniff_video_ext(_MP4) == ".mp4"
    assert dreamina._sniff_video_ext(_MOV) == ".mov"
    assert dreamina._sniff_video_ext(_WEBM) == ".webm"
    for junk in (_PNG, b"<html>404</html>", b"", b"RIFF____AVI "):
        assert dreamina._sniff_video_ext(junk) is None, junk[:8]
    # 反向：图片白名单**没有**因此认视频（放宽它等于同时放宽 image2video / 首尾帧 / 多图参考）
    for video in (_MP4, _MOV, _WEBM):
        assert dreamina._sniff_ext(video) is None


async def test_materialize_ref_video_http_ok(tmp_path, monkeypatch):
    _stub_dns(monkeypatch)
    monkeypatch.setattr(dreamina, "httpx", _FakeHttpx([_MP4[:8], _MP4[8:]]))
    got = await dreamina.materialize_ref_video("https://cdn.example/a.mp4", tmp_path / "w")
    assert got.suffix == ".mp4" and got.read_bytes() == _MP4


async def test_materialize_ref_video_rejects_non_video_content(tmp_path, monkeypatch):
    """按内容魔数判容器：PNG / 错误页冒充 .mp4 一律拒收（扩展名不作数）。"""
    _stub_dns(monkeypatch)
    monkeypatch.setattr(dreamina, "httpx", _FakeHttpx([_PNG]))
    with pytest.raises(ValueError, match="不是有效视频容器"):
        await dreamina.materialize_ref_video("https://cdn.example/fake.mp4", tmp_path / "w")


async def test_materialize_ref_image_still_rejects_video_bytes(tmp_path, monkeypatch):
    """回归锁：**图片白名单一寸没放宽**——mp4 字节喂给参考图通道照旧当场拒收。"""
    _stub_dns(monkeypatch)
    monkeypatch.setattr(dreamina, "httpx", _FakeHttpx([_MP4]))
    with pytest.raises(ValueError, match="不是有效图片"):
        await dreamina.materialize_ref_image("https://cdn.example/a.mp4", tmp_path / "w")


async def test_materialize_ref_video_from_uploads_makes_independent_copy(tmp_path, monkeypatch):
    """/uploads 参考视频同样**复制**而非引用（本服务自己的成片直接回传当参考就是这条路）。"""
    monkeypatch.setattr(settings, "DATA_DIR", str(tmp_path))
    src = tmp_path / "uploads" / "clips" / "vc_x-token"
    src.mkdir(parents=True)
    (src / "clip.mp4").write_bytes(_MP4)
    got = await dreamina.materialize_ref_video(
        "/uploads/clips/vc_x-token/clip.mp4", tmp_path / "w")
    assert got.is_file() and got.parent == (tmp_path / "w")
    (src / "clip.mp4").unlink()                    # 源片被 TTL 清了
    assert got.read_bytes() == _MP4                # 副本仍在


async def test_materialize_ref_video_has_its_own_size_limit(tmp_path, monkeypatch):
    """视频有独立体积闸：图片那个 15MB 套上来会把正常参考视频全拒掉。"""
    _stub_dns(monkeypatch)
    monkeypatch.setattr(settings, "CLIP_IMAGE_MAX_MB", 1)
    monkeypatch.setattr(settings, "CLIP_VIDEO_MAX_MB", 1)
    monkeypatch.setattr(dreamina, "httpx", _FakeHttpx([_MP4 + b"x" * (1024 * 1024)]))
    with pytest.raises(ValueError, match="参考视频超过 1MB 上限"):
        await dreamina.materialize_ref_video("https://cdn.example/big.mp4", tmp_path / "w")


# ── 参考视频时长前置校验（ffprobe，省一次白跑）───────────────────────────────
def _stub_probe(monkeypatch, values: dict):
    """把 ffprobe 换成查表：{路径尾名: 秒数或 None}。"""
    async def _fake(path):
        return values[Path(path).name]

    monkeypatch.setattr(dreamina, "probe_duration", _fake)


async def test_ref_video_duration_window_is_per_model(monkeypatch):
    """每条 2-30s（2.0 家族 2-15s）：越界当场给说明，省掉一次必被 CLI 拒的提交。"""
    _stub_probe(monkeypatch, {"vid.mp4": 20.0})
    assert await dreamina.check_ref_media_durations(["/w/vid.mp4"], "seedance2.5") is None
    over = await dreamina.check_ref_media_durations(["/w/vid.mp4"], "seedance2.0fast")
    assert over and "15" in over and "第 1 份" in over

    _stub_probe(monkeypatch, {"vid.mp4": 1.2})
    short = await dreamina.check_ref_media_durations(["/w/vid.mp4"], "seedance2.5")
    assert short and "越界" in short


async def test_ref_video_total_duration_is_also_capped(monkeypatch):
    """CLI 口径是 "each **and total**"：每条都合规、合计超了照样拦。"""
    _stub_probe(monkeypatch, {"vid.mp4": 20.0, "vid_2.mp4": 15.0})
    bad = await dreamina.check_ref_media_durations(
        ["/w/vid.mp4", "/w/vid_2.mp4"], "seedance2.5")
    assert bad and "合计" in bad


async def test_unprobeable_video_is_not_blocked(monkeypatch):
    """探不出时长不拦：把「探测器不给力」变成拒绝服务是本末倒置，真越界 CLI 那边还有一道。"""
    _stub_probe(monkeypatch, {"vid.mp4": None})
    assert await dreamina.check_ref_media_durations(["/w/vid.mp4"], "seedance2.5") is None
    assert await dreamina.check_ref_media_durations([], "seedance2.5") is None


# ── 提交参数组装：--video 与 --image 同序拼接 ────────────────────────────────
def test_build_submit_args_carries_videos_after_images():
    """multimodal2video 的 --video 逐条一个 flag，**顺序即 @视频N**，排在 --image 之后。"""
    clip = _clip(operation="multimodal2video", model="seedance2.5", ratio=None,
                 image_paths_json=json.dumps(["/w/ref.png", "/w/ref_2.png"]),
                 video_paths_json=json.dumps(["/w/vid.mp4", "/w/vid_2.mp4"]))
    args = dreamina.build_submit_args(clip)
    assert [a for a in args if a.startswith("--image=")] == [
        "--image=/w/ref.png", "--image=/w/ref_2.png"]
    assert [a for a in args if a.startswith("--video=")] == [
        "--video=/w/vid.mp4", "--video=/w/vid_2.mp4"]
    assert args.index("--image=/w/ref_2.png") < args.index("--video=/w/vid.mp4")


def test_video_only_multimodal_submits_without_any_image_flag():
    """纯参考视频（一张图都不给）也是合法输入面，参数里就不该出现 --image。"""
    clip = _clip(operation="multimodal2video", model="seedance2.5", ratio=None,
                 video_paths_json=json.dumps(["/w/vid.mp4"]))
    args = dreamina.build_submit_args(clip)
    assert not any(a.startswith("--image=") for a in args)
    assert "--video=/w/vid.mp4" in args


def test_ref_video_paths_tolerates_old_rows_and_bad_json():
    """本列上线前的老行恒空；坏 JSON 也回空——一个坏值不该让已建好的任务永远提交不出去。"""
    assert dreamina.ref_video_paths(_clip()) == []
    assert dreamina.ref_video_paths(_clip(video_paths_json="{坏的")) == []
    assert dreamina.ref_video_paths(_clip(video_paths_json='["/w/vid.mp4"]')) == ["/w/vid.mp4"]


async def test_materialize_ref_video_sniffs_local_uploads_too(tmp_path, monkeypatch):
    """本地 /uploads 来源的**视频也过魔数**：判错的代价是一次真提交、一次真扣分。

    参考图那条路仍按后缀走（本函数抽出来之前就是这样，本次不动它）——图床里的图是上传闸
    校验过的，而参考视频最可能的误用恰恰是「把一张图当参考视频传进来」。
    """
    monkeypatch.setattr(settings, "DATA_DIR", str(tmp_path))
    src = tmp_path / "uploads" / "mixed"
    src.mkdir(parents=True)
    (src / "fake.mp4").write_bytes(_PNG)            # 后缀对、内容是图
    with pytest.raises(ValueError, match="不是有效视频容器"):
        await dreamina.materialize_ref_video("/uploads/mixed/fake.mp4", tmp_path / "w")

    # 反向：内容对但后缀错的，按**内容**落扩展名（送给 CLI 的副本名不会骗它）
    (src / "mislabeled.bin").write_bytes(_MOV)
    got = await dreamina.materialize_ref_video("/uploads/mixed/mislabeled.bin", tmp_path / "w")
    assert got.suffix == ".mov"


# ── 参考音频物化（CLI 的 --audio 输入面）────────────────────────────────────
_MP3_ID3 = b"ID3\x04\x00\x00" + b"\x00" * 32          # 带 ID3v2 标签的 MP3
_MP3_BARE = b"\xff\xfb\x90\x00" + b"\x00" * 32        # 无标签 MP3（裸帧同步）
_WAV = b"RIFF\x24\x00\x00\x00WAVEfmt " + b"\x00" * 16
_M4A = b"\x00\x00\x00\x20ftypM4A " + b"\x00" * 32     # ISO BMFF，但 brand 是 M4A（音频）
_FLAC = b"fLaC" + b"\x00" * 32
_OGG = b"OggS" + b"\x00" * 32
_AAC = b"\xff\xf1\x50\x80" + b"\x00" * 32             # ADTS AAC


def test_audio_magic_whitelist_is_its_own_channel():
    """音频是**第三条独立白名单**：三类各认各的，绝不为了收一类去放宽另一类。"""
    assert dreamina._sniff_audio_ext(_MP3_ID3) == ".mp3"
    assert dreamina._sniff_audio_ext(_MP3_BARE) == ".mp3"
    assert dreamina._sniff_audio_ext(_WAV) == ".wav"
    assert dreamina._sniff_audio_ext(_M4A) == ".m4a"
    assert dreamina._sniff_audio_ext(_FLAC) == ".flac"
    assert dreamina._sniff_audio_ext(_OGG) == ".ogg"
    assert dreamina._sniff_audio_ext(_AAC) == ".aac"
    for junk in (_PNG, _MP4, _WEBM, b"<html>404</html>", b""):
        assert dreamina._sniff_audio_ext(junk) is None, junk[:8]
    # 反向两条：图片、视频两条白名单都没有因此认音频
    for audio in (_MP3_ID3, _WAV, _M4A, _FLAC, _OGG, _AAC):
        assert dreamina._sniff_ext(audio) is None, audio[:8]
        assert dreamina._sniff_video_ext(audio) is None, audio[:8]


def test_m4a_is_audio_not_video_despite_sharing_iso_bmff_header():
    """mp4 与 m4a 的头一模一样，只有 major brand 能分——视频通道必须显式排掉 M4A/M4B。

    不排的话一条 m4a 传进 videos 会被当 .mp4 收下，送给 CLI 的是一个「视频输入」里塞了
    纯音频——CLI 侧拒了是白跑一次，没拒就是出一条我们没打算要的片。
    """
    assert _M4A[4:8] == _MP4[4:8] == b"ftyp"          # 头确实一样
    assert dreamina._sniff_video_ext(_M4A) is None
    assert dreamina._sniff_audio_ext(_M4A) == ".m4a"
    assert dreamina._sniff_video_ext(_MP4) == ".mp4"
    assert dreamina._sniff_audio_ext(_MP4) is None


async def test_materialize_ref_audio_http_ok_and_rejects_non_audio(tmp_path, monkeypatch):
    _stub_dns(monkeypatch)
    monkeypatch.setattr(dreamina, "httpx", _FakeHttpx([_MP3_ID3[:4], _MP3_ID3[4:]]))
    got = await dreamina.materialize_ref_audio("https://cdn.example/bgm.mp3", tmp_path / "w")
    assert got.suffix == ".mp3" and got.read_bytes() == _MP3_ID3

    monkeypatch.setattr(dreamina, "httpx", _FakeHttpx([_MP4]))
    with pytest.raises(ValueError, match="不是有效音频容器"):
        await dreamina.materialize_ref_audio("https://cdn.example/fake.mp3", tmp_path / "w")


async def test_materialize_ref_audio_from_uploads_sniffs_and_copies(tmp_path, monkeypatch):
    """本地 /uploads 来源同样过魔数 + **复制**而非引用（与参考视频同款）。"""
    monkeypatch.setattr(settings, "DATA_DIR", str(tmp_path))
    src = tmp_path / "uploads" / "bgm"
    src.mkdir(parents=True)
    (src / "track.wav").write_bytes(_WAV)
    got = await dreamina.materialize_ref_audio("/uploads/bgm/track.wav", tmp_path / "w")
    assert got.suffix == ".wav"
    (src / "track.wav").unlink()
    assert got.read_bytes() == _WAV

    (src / "notaudio.mp3").write_bytes(_PNG)          # 后缀对、内容是图
    with pytest.raises(ValueError, match="不是有效音频容器"):
        await dreamina.materialize_ref_audio("/uploads/bgm/notaudio.mp3", tmp_path / "w")


async def test_materialize_ref_audio_has_its_own_size_limit(tmp_path, monkeypatch):
    """三类素材三个体积闸，各按各的量级（音频比视频窄、比图片宽）。"""
    _stub_dns(monkeypatch)
    monkeypatch.setattr(settings, "CLIP_AUDIO_MAX_MB", 1)
    monkeypatch.setattr(dreamina, "httpx", _FakeHttpx([_MP3_ID3 + b"x" * (1024 * 1024)]))
    with pytest.raises(ValueError, match="参考音频超过 1MB 上限"):
        await dreamina.materialize_ref_audio("https://cdn.example/big.mp3", tmp_path / "w")


async def test_ref_audio_duration_window_is_per_model(monkeypatch):
    """音频与视频同一个时长窗口，且**每类各自合计**（两次调用互不相加）。"""
    _stub_probe(monkeypatch, {"aud.mp3": 20.0})
    assert await dreamina.check_ref_media_durations(
        ["/w/aud.mp3"], "seedance2.5", kind="参考音频") is None
    over = await dreamina.check_ref_media_durations(
        ["/w/aud.mp3"], "seedance2.0fast", kind="参考音频")
    assert over and "参考音频" in over and "15" in over

    _stub_probe(monkeypatch, {"aud.mp3": 20.0, "aud_2.mp3": 15.0})
    total = await dreamina.check_ref_media_durations(
        ["/w/aud.mp3", "/w/aud_2.mp3"], "seedance2.5", kind="参考音频")
    assert total and "合计" in total and "参考音频" in total


def test_build_submit_args_carries_all_three_kinds_in_order():
    """三类各自逐份一个 flag，顺序 图 → 视频 → 音频，**每类内部顺序即 @编号**。"""
    clip = _clip(operation="multimodal2video", model="seedance2.5", ratio=None,
                 image_paths_json=json.dumps(["/w/ref.png"]),
                 video_paths_json=json.dumps(["/w/vid.mp4", "/w/vid_2.mp4"]),
                 audio_paths_json=json.dumps(["/w/aud.mp3", "/w/aud_2.wav"]))
    args = dreamina.build_submit_args(clip)
    assert [a for a in args if a.startswith("--audio=")] == [
        "--audio=/w/aud.mp3", "--audio=/w/aud_2.wav"]
    assert (args.index("--image=/w/ref.png") < args.index("--video=/w/vid.mp4")
            < args.index("--audio=/w/aud.mp3"))


def test_audio_only_clip_submits_without_image_or_video_flags():
    """纯音频（2.5 专属）：参数里既没有 --image 也没有 --video，只有 --audio。"""
    clip = _clip(operation="multimodal2video", model="seedance2.5", ratio=None,
                 audio_paths_json=json.dumps(["/w/aud.mp3"]))
    args = dreamina.build_submit_args(clip)
    assert not any(a.startswith(("--image=", "--video=")) for a in args)
    assert "--audio=/w/aud.mp3" in args


def test_ref_audio_paths_tolerates_old_rows_and_bad_json():
    assert dreamina.ref_audio_paths(_clip()) == []
    assert dreamina.ref_audio_paths(_clip(audio_paths_json="{坏的")) == []
    assert dreamina.ref_audio_paths(_clip(audio_paths_json='["/w/a.mp3"]')) == ["/w/a.mp3"]
