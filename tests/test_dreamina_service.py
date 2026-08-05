"""即梦服务层单测：clip_id 形态 / 积分与登录态 / 参考图物化 / 产物 TTL 清理。

全离线：dreamina CLI 一律 monkeypatch ``app.services.dreamina._run_cli``（消费方命名空间），
httpx 一律 monkeypatch ``dreamina.httpx``——**绝不真跑 CLI、绝不发真网络请求**（真跑就是烧
公司积分 + 占即梦队列位）。
"""

import asyncio
import fcntl
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


def test_estimate_credit_by_five_second_tier():
    assert dreamina.estimate_credit("seedance2.0fast", 5) == 25
    assert dreamina.estimate_credit("seedance2.0fast", 4) == 25      # 不足一档按一档
    assert dreamina.estimate_credit("seedance2.0fast", 8) == 50      # ceil(8/5)=2
    assert dreamina.estimate_credit("seedance2.0fast_vip", 5) == 55
    # seedance2.5 已实测：130/5s（2026-08-05 生产 vc_3e1260f8ce 对账精确）。
    assert dreamina.estimate_credit("seedance2.5", 5) == 130
    assert dreamina.estimate_credit("seedance2.5", 30) == 780     # ceil(30/5)=6 档
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
