"""即梦(Dreamina)视频生成服务：CLI 封装 + 提交/轮询调度 + 产物 TTL 清理。

设计 ``docs/design/2026-08-05-dreamina-clips-design.md``；需求契约
``NBDpsy/文档/2026-08-05-server需求-即梦视频生成服务化.md``（第三节端点 / 第四节行为 / 第七节验收）。

搬迁边界：dreamina CLI 的持有、登录态、submit / poll / fetch / 积分查询全部进 server；
产物 MP4 落 ``DATA_DIR/uploads/clips/{clip_id}-{hmac16}/clip.mp4`` 给免鉴权直链。分镜、TTS、
BGM、ffmpeg 合成、审查仍在 skill 侧，不在本模块范围。

**本模块每一条重试语义都围绕一条事实设计：submit 即占即梦队列位、success 即扣积分。**
所以「重复提交」不是浪费时间而是烧钱，且排队中的任务 CLI 无法取消。由此推出三条铁律：

1. **绝不自动重提**。任务卡 querying 数小时是正常排队（高峰 fast 近 2 小时），服务端只如实
   下发 ``queued_seconds``，重提是运营的决策（通常换 fast_vip，旧 submit_id 保留谁先出用谁）。
2. **歧义结局判 error 而不是重排**。submit 超时 / CLI 被信号打断 / 拿不到 submit_id——任务是否
   已进即梦队列**未知**。此时重排 = 可能双倍扣分，故一律落 error 并把「未知」如实写进 error
   文案，交人决策。同理进程崩在 submit 中途留下的 ``submitting`` 行，启动自愈也判 error。
3. **查询失败 ≠ 任务失败**。query_result 瞬时失败/非 JSON 只写 ``last_poll_error``，status 不动
   ——把它写成终态会让运营误判任务已死而重发。

CLI 事实（均已实测，非二手）：输出干净 JSON；submit 后顶层 ``submit_id``（16 位 hex）；
``query_result --submit_id=X --download_dir=Y`` 成功返回 ``result_json.videos[].path`` +
``credit_count``；``gen_status`` ∈ querying/success/failed/…；``--video_resolution`` 必填且逐档校验，720p 是
唯一对全家族都合法的一档；``image2video`` 画幅由输入图推断、不接受 ``--ratio``。

非阻塞红线：本模块跑在 worker 的单一事件循环上（与 supervisor 扫描、视频调度共享），
所有 CLI 调用一律 ``asyncio.create_subprocess_exec``，**禁止 subprocess.run**。
"""

import asyncio
import fcntl
import hashlib
import hmac
import ipaddress
import json
import math
import os
import re
import secrets
import shutil
import socket
import time
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlparse

import httpx
from loguru import logger
from sqlalchemy import select, update

from app.core.config import settings
from app.models.video_clip import VideoClip

# ── 常量 ────────────────────────────────────────────────────────────────────
# 默认档（六档模型 / 六种画幅 / 三种 operation 的枚举闸在 REST 层的 Literal 上，不在此重复）。
# 2026-08-05 CLI 升级后模型面从四档扩到六档（新增 seedance2.0mini / seedance2.5，2.5 **没有**
# fast / vip 变体），默认档同时切到 seedance2.5。
DEFAULT_MODEL = "seedance2.5"

# 时长闸：下限全家族都是 4s；上限**只有 seedance2.5 到 30s**，其余仍是 15s。
# REST 的 Pydantic 字段界取全家族最宽（4-30）才放得过 2.5，逐模型收紧在 model_validator 里
# ——不收紧的话 `seedance2.0fast + duration=20` 要等到 CLI 那边才被拒，那时行已建、参考图已物化，
# 运营看到的是一条要人回头清理的 error 行。
DURATION_MIN = 4
DURATION_MAX = 30
_DURATION_MAX_DEFAULT = 15
_DURATION_MAX_BY_MODEL = {"seedance2.5": 30}

# 5s/720p 单镜实测价（仅这两档有实测值；未知档不估、不给 warning，绝不瞎猜）。
# **seedance2.5 与 seedance2.0mini 不编价**：2.5 是 VIP-only 的新档，两档都没实测过单镜消耗，
# 编一个「看着合理」的数只会让 warning 给出假的估算。首次真跑后按实测回填。
_PRICE_PER_5S = {"seedance2.0fast": 25, "seedance2.0fast_vip": 55}
# 已知最便宜的一镜（5s fast）。余额低于它 = 连一镜都提交不起 → 409。
MIN_CLIP_CREDIT = 25

# 首次用某模型可能返回它：需人到 Dreamina 网页端做一次性授权（账号级），服务端重试无意义。
COMPLIANCE_MARKER = "AigcComplianceConfirmationRequired"
COMPLIANCE_HINT = "（需人到 Dreamina 网页端对该模型做一次性授权，授权后重发即可；服务端重试无意义）"

# 歧义结局话术：任务是否已入即梦队列未知，绝不自动重提（重提 = 可能双倍扣分）。
AMBIGUOUS_HINT = (
    "进程或 CLI 中断，任务是否已到即梦队列未知——不自动重提防双扣；"
    "请核对后由运营决定是否重发"
)

# 内部/对外状态
INTERNAL_SUBMITTING = "submitting"
_IN_FLIGHT = ("submitted", "querying")
# CLI 超时的伪返回码（与 skill 侧本地实现同值，便于日志对照）
_RC_TIMEOUT = 124
# 等不到 CLI 锁的伪返回码。与 _RC_TIMEOUT 语义**相反**：CLI 根本没被调起，任务确定没入队，
# 所以调用方可以安全重试（复位回 queued），不是要人裁决的歧义结局。
_RC_LOCK_BUSY = 125

# CLI 串行闸：单账号 + CLI 本地 tasks.db，并发调用会互相打架。**两层锁**：
# - 进程内 ``asyncio.Lock``：同进程的协程串行；
# - 跨进程文件锁（flock）：生产是 api / worker 两个 systemd 单元，API 侧 ``user_credit``
#   与 worker 侧 submit/query 是两个 OS 进程，asyncio.Lock 对它们完全不生效。
_CLI_LOCK = asyncio.Lock()
_CLI_LOCK_FILE = "dreamina-cli.lock"
# 等锁上限：等不到就回 _RC_LOCK_BUSY 让调用方各自处置，绝不无限等——阻塞版 flock 会让 API 的
# 一次积分查询被 worker 的 300s query_result 拖到请求超时。
_CLI_LOCK_WAIT_SECONDS = 10.0

# 积分缓存（进程内 60s）：user_credit 每次都起子进程，POST 高频调用不该次次跑 CLI。
_CREDIT_TTL_SECONDS = 60.0
_credit_cache: dict = {"at": None, "value": None}


def _utcnow() -> datetime:
    """与宿主各表 created_at 一致的 naive UTC。"""
    return datetime.utcnow()


# ── clip 工作目录（HMAC token 目录 = 免鉴权直链的访问控制）───────────────────
def new_clip_id() -> str:
    """生成对外句柄 ``vc_<10hex>``。

    **绝不能是 16 位纯小写 hex**——那是本机 dreamina CLI submit_id 的形态，skill 侧 auto 模式
    靠形态判断「问 server 还是问本机 CLI」，撞车会把 server 的 clip 派到本机 CLI 空转到超时
    （需求第三节第 1 条 / 验收第 8 条）。``vc_`` 前缀 + 10 位 hex 两重不撞。"""
    return "vc_" + secrets.token_hex(5)


def new_batch_id() -> str:
    """生成批次句柄 ``vcb_<10hex>``（同样不与 submit_id 形态碰撞）。"""
    return "vcb_" + secrets.token_hex(5)


def _clip_token(clip_id: str) -> str:
    """SECRET_KEY 派生的不可猜 token（手法与 app/video/paths.py 一致，不改动那边）。

    产物落在 ``DATA_DIR/uploads`` 下即天然被免鉴权 /uploads 路由暴露，故父段用 HMAC token：
    攻击者无 SECRET_KEY 无法由 clip_id 枚举他人成片。"""
    return hmac.new(
        settings.SECRET_KEY.encode(), clip_id.encode(), hashlib.sha256
    ).hexdigest()[:16]


def clip_token_dir(clip_id: str) -> str:
    """产物目录名 ``{clip_id}-{hmac16}``（直链路径里的那一段）。"""
    return f"{clip_id}-{_clip_token(clip_id)}"


def clips_root() -> Path:
    """片段产物根：``DATA_DIR/uploads/clips``。请求时读 settings（不在 import 期绑定），
    使测试对 DATA_DIR 的 monkeypatch 生效，与 uploads_rest / video.paths 同惯例。"""
    return (Path(settings.DATA_DIR) / "uploads" / "clips").resolve()


def clip_dir(clip_id: str) -> Path:
    """确保并返回单条 clip 的工作目录（参考图副本 + 产物 MP4 都在里面）。"""
    d = clips_root() / clip_token_dir(clip_id)
    d.mkdir(parents=True, exist_ok=True)
    return d


def clip_public_url(clip_id: str, name: str) -> str:
    """产物文件名 → 免鉴权直链相对路径。"""
    return f"/uploads/clips/{clip_token_dir(clip_id)}/{name}"


# ── CLI 封装（全部经这里；测试 monkeypatch 本模块的 _run_cli）─────────────────
def _parse_json(text: str):
    """容错解析 dreamina 输出：整体 loads 失败则从首个 ``{`` / ``[`` 起再试。"""
    text = (text or "").strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    starts = [i for i in (text.find("{"), text.find("[")) if i != -1]
    if not starts:
        return None
    try:
        return json.loads(text[min(starts):])
    except json.JSONDecodeError:
        return None


class _CliLockBusy(Exception):
    """等锁超时：别的进程正占着 dreamina CLI（本进程一次都没调起它）。"""


def _cli_lock_path() -> Path:
    """跨进程 CLI 锁文件。放 ``DATA_DIR`` 下（api / worker 两个单元共享同一份），请求时读
    settings 不在 import 期绑定——与 ``clips_root`` 同惯例，测试 monkeypatch DATA_DIR 即生效。"""
    return Path(settings.DATA_DIR) / _CLI_LOCK_FILE


def _acquire_cli_file_lock(deadline: float):
    """抢跨进程 CLI 文件锁（阻塞，跑在线程里），返回持锁的文件句柄；等过 deadline 抛 _CliLockBusy。

    非阻塞 flock + 轮询而不是阻塞 flock：阻塞版拿不到就一直挂着，没有逃生口。
    """
    path = _cli_lock_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fh = open(path, "a+")
    while True:
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return fh
        except BlockingIOError:
            if time.monotonic() >= deadline:
                fh.close()
                raise _CliLockBusy() from None
            time.sleep(0.2)


def _release_cli_file_lock(fh) -> None:
    """放锁并关句柄（关闭本身也会释放 flock，显式解锁只为可读）。"""
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
    finally:
        fh.close()


async def _run_cli(args: list[str], timeout: float) -> tuple[int, str, str]:
    """跑 ``dreamina <args>``，返回 ``(rc, stdout, stderr)``。

    - 双层锁串行化：单账号 + CLI 本地 tasks.db，两个 CLI 同时跑会互相踩。进程内
      ``asyncio.Lock`` 管本进程协程，**文件锁管跨进程**（api / worker 是两个 systemd 单元，
      API 的 ``user_credit`` 与 worker 的 submit/query 会真并发拉起同一个二进制）。
    - ``create_subprocess_exec``（**不是** subprocess.run）：worker 单事件循环上还有心跳泵、
      视频调度与 supervisor 扫描，同步阻塞会把整个循环冻住；抢文件锁也走 ``to_thread``。
    - 超时：kill 子进程并回 ``_RC_TIMEOUT``——调用方据此判**歧义结局**，绝不重提。
    - 等锁超时：回 ``_RC_LOCK_BUSY``——CLI 没被调起，调用方可安全重试。
    """
    async with _CLI_LOCK:
        deadline = time.monotonic() + _CLI_LOCK_WAIT_SECONDS
        try:
            lock_fh = await asyncio.to_thread(_acquire_cli_file_lock, deadline)
        except _CliLockBusy:
            sub = args[0] if args else ""
            return (_RC_LOCK_BUSY, "",
                    f"另一进程正在跑 dreamina，等锁超时({_CLI_LOCK_WAIT_SECONDS}s)："
                    f"{sub} 未执行")
        except OSError as exc:  # 锁文件不可用（DATA_DIR 权限/磁盘）——与 CLI 不可执行同档处理
            return 127, "", f"dreamina CLI 锁文件不可用（{_cli_lock_path()}）：{exc}"
        try:
            try:
                proc = await asyncio.create_subprocess_exec(
                    settings.DREAMINA_BIN, *args,
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                )
            except OSError as exc:  # FileNotFoundError / PermissionError 均是 OSError
                return 127, "", f"dreamina CLI 不可执行（{settings.DREAMINA_BIN}）：{exc}"
            try:
                out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
                sub = args[0] if args else ""
                return _RC_TIMEOUT, "", f"dreamina {sub} 超时({timeout}s)"
            rc = proc.returncode if proc.returncode is not None else 0
            return (rc,
                    out.decode("utf-8", "replace") if out else "",
                    err.decode("utf-8", "replace") if err else "")
        finally:
            await asyncio.to_thread(_release_cli_file_lock, lock_fh)


async def cli_user_credit() -> tuple[int, object, str]:
    """``dreamina user_credit`` → (rc, 解析后的 JSON 或 None, stdout+stderr 原文)。"""
    rc, out, err = await _run_cli(["user_credit"], timeout=60)
    return rc, _parse_json(out), (out + "\n" + err).strip()


async def cli_submit(args: list[str]) -> tuple[int, object, str]:
    """跑生成子命令（``--poll=0`` 纯提交）→ (rc, JSON|None, 原文)。"""
    rc, out, err = await _run_cli(args, timeout=settings.CLIP_SUBMIT_TIMEOUT)
    return rc, _parse_json(out), (out + "\n" + err).strip()


async def cli_query(submit_id: str, download_dir: str) -> tuple[int, object, str]:
    """``dreamina query_result --submit_id=X --download_dir=Y`` → (rc, JSON|None, 原文)。"""
    rc, out, err = await _run_cli(
        ["query_result", f"--submit_id={submit_id}", f"--download_dir={download_dir}"],
        timeout=settings.CLIP_QUERY_TIMEOUT,
    )
    return rc, _parse_json(out), (out + "\n" + err).strip()


# ── 积分 / 登录态 ───────────────────────────────────────────────────────────
async def get_credit_status(*, force: bool = False) -> dict:
    """``{logged_in, credit}``（进程内 60s 缓存）。

    登录态是这条产线唯一发不出去的凭据（本地回调流，凭据落 ``~/.dreamina_cli/``），全服务
    只有 server 上一份。``user_credit`` 跑不通或解析不出余额 = 登录失效/CLI 不可用 →
    ``logged_in=False``，POST 据此 **503 明确报错而不是静默排队**（验收第 6 条）。

    唯一例外是 ``_RC_LOCK_BUSY``（worker 正占着 CLI）：那不是登录态的证据，判 false 会被缓存
    60s——期间所有提交 503、skill 侧 auto 探测还会整进程回落本机 CLI。故改用上一次的结论且
    **不刷新缓存**，下次请求重试。
    """
    now = time.monotonic()
    cached = _credit_cache["value"]
    if not force and cached is not None and _credit_cache["at"] is not None:
        if now - _credit_cache["at"] < _CREDIT_TTL_SECONDS:
            return dict(cached)
    rc, data, blob = await cli_user_credit()
    if rc == _RC_LOCK_BUSY:
        if cached is not None:
            return dict(cached)
        return {"logged_in": False, "credit": None,
                "error": (blob or "dreamina CLI 正忙，本次未查到积分")[:500]}
    if rc != 0 or not isinstance(data, dict):
        status = {"logged_in": False, "credit": None,
                  "error": (blob or "dreamina user_credit 失败")[:500]}
    else:
        credit = data.get("total_credit")
        if credit is None:
            credit = data.get("credit")
        if isinstance(credit, bool) or not isinstance(credit, (int, float)):
            status = {"logged_in": False, "credit": None,
                      "error": "user_credit 未返回积分余额（登录态可能已失效）"}
        else:
            status = {"logged_in": True, "credit": int(credit), "error": None}
    _credit_cache["at"] = now
    _credit_cache["value"] = status
    return dict(status)


def reset_credit_cache() -> None:
    """清积分缓存（测试与「刚换过登录态文件」时用）。"""
    _credit_cache["at"] = None
    _credit_cache["value"] = None


def max_duration(model: str) -> int:
    """该模型的单镜时长上限（秒）。未知模型按家族默认 15s 判——宁可窄不宜宽，
    放宽等于让一条 CLI 必拒的任务先建行再失败。"""
    return _DURATION_MAX_BY_MODEL.get(model, _DURATION_MAX_DEFAULT)


def estimate_credit(model: str, duration: int) -> int | None:
    """按 5s 档粗估一镜积分；未知模型返回 None（不估、不给 warning）。

    只用于**提示**：扣费 success 才结算、排队中还有变数，所以低余额一律 warning 不拦截
    （需求第四节第 5 条）。真正拦截只有一种情况——余额连最便宜一镜都不够。
    """
    unit = _PRICE_PER_5S.get(model)
    if unit is None:
        return None
    return unit * max(1, math.ceil(duration / 5))


# ── 参考图物化（POST 时同步做：坏图当场 4xx，绝不建了任务再失败）──────────────
# 图片魔数白名单：内容不是图片就拒收（防把 HTML 错误页 / 大文件当参考图喂给 CLI）
_IMAGE_MAGICS = (
    (b"\x89PNG\r\n\x1a\n", ".png"),
    (b"\xff\xd8\xff", ".jpg"),
    (b"GIF87a", ".gif"),
    (b"GIF89a", ".gif"),
    (b"BM", ".bmp"),
)


def _sniff_ext(data: bytes) -> str | None:
    """按魔数判扩展名；非图片返回 None。WEBP 需同时看 RIFF 头与 WEBP 标记。"""
    for magic, ext in _IMAGE_MAGICS:
        if data.startswith(magic):
            return ext
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp"
    return None


def _uploads_local_file(path: str) -> Path | None:
    """裸 ``/uploads/...`` 路径 → 本地文件；越界/不存在返回 None。

    **只收裸路径**，不再从 URL 里抠 path：判来源的唯一依据是 scheme（见
    ``materialize_ref_image``）。以前「URL 的 path 以 /uploads 开头就当本地文件」会把
    *任意主机* 的 ``https://evil.example/uploads/x.png`` 当成本服务图床去本地找。
    """
    if not path.startswith("/uploads/"):
        return None
    root = (Path(settings.DATA_DIR) / "uploads").resolve()
    target = (root / path[len("/uploads/"):]).resolve()
    if not target.is_relative_to(root):
        return None
    return target if target.is_file() else None


# 远程参考图下载的**总时长**上限（秒）。httpx 的 timeout 是每次操作各自计时，
# 慢速滴流的服务端（每 29s 吐一个字节）能把单次下载拖到无限长，故整段再包一层
# ``asyncio.wait_for``——批量端点「单张最多 30s」的承诺靠的就是这一层。
_REMOTE_FETCH_TIMEOUT = 30.0


def _assert_public_host(host: str | None) -> None:
    """拒绝指向内网/环回/链路本地的参考图 URL（SSRF 闸）；不合规抛 ValueError。

    参考图 URL 由调用方给，服务端会**主动去 GET**——不设闸就是一个任意内网探测器：
    ``http://127.0.0.1:8000/api/...``、``http://169.254.169.254/``（云元数据）、
    ``http://10.x/`` 都会被服务端代为访问，响应内容还会以「不是有效图片」的形式回灌。

    解析出的**每个** A/AAAA 都要合规（多 A 记录里混一条 127.0.0.1 就能绕过只查首条的实现）。
    残留 TOCTOU（这里解析完、httpx 再解析一次，之间 DNS 可能翻脸）不在本闸射程内——
    要堵得把连接固定到已校验的 IP 上，那要接管 httpx 的 transport，代价远超收益。
    """
    if not host:
        raise ValueError("参考图 URL 缺少主机名")
    host = host.strip("[]")           # IPv6 字面量在 URL 里带方括号
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror as exc:
        raise ValueError(f"参考图域名解析失败（{host}）：{exc}") from exc
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if (ip.is_private or ip.is_loopback or ip.is_link_local
                or ip.is_reserved or ip.is_multicast or ip.is_unspecified):
            raise ValueError(
                f"参考图不允许内网地址（{host} 解析到 {ip}）。"
                "本服务图床的图请直接传裸 /uploads/... 路径，不要传内网 URL"
            )


async def _fetch_remote_image(source: str) -> bytes:
    """下载远程参考图（限大小 + 每跳过 SSRF 闸）；失败抛 ValueError。"""
    limit = settings.CLIP_IMAGE_MAX_MB * 1024 * 1024
    chunks: list[bytes] = []
    size = 0

    async def _guard_redirect(request) -> None:
        """每次真实请求（**含重定向后的每一跳**）都过闸。

        只查初始 URL 挡不住 ``https://public.example/r`` → ``http://127.0.0.1/`` 这种
        跳板；httpx 的 request 事件钩子对每一跳都会触发，闸放这里才是完整的。
        """
        _assert_public_host(request.url.host)

    try:
        async with httpx.AsyncClient(
            timeout=_REMOTE_FETCH_TIMEOUT, follow_redirects=True,
            event_hooks={"request": [_guard_redirect]},
        ) as client:
            async with client.stream("GET", source) as resp:
                resp.raise_for_status()
                async for chunk in resp.aiter_bytes():
                    size += len(chunk)
                    if size > limit:
                        raise ValueError(
                            f"参考图超过 {settings.CLIP_IMAGE_MAX_MB}MB 上限：{source}")
                    chunks.append(chunk)
    except httpx.HTTPError as exc:
        raise ValueError(f"参考图下载失败（{source}）：{exc}") from exc
    return b"".join(chunks)


async def materialize_ref_image(source: str, workdir: Path) -> Path:
    """把参考图落成 clip 工作目录内的**独立副本**，返回本地路径；非法来源抛 ValueError。

    两种来源（与 note-components ``add_images`` 同款约定，**不收 base64 大包**——524 教训），
    **判据是 scheme 而不是 path 形状**：
    - ``http(s)://``：一律走远程下载（哪怕 path 长得像 ``/uploads/...``——本服务公网域名的
      完整直链就是这个样子，它经 Cloudflare 解析到公网 IP，回环下载正常）。限
      ``CLIP_IMAGE_MAX_MB`` / 总时长 30s / 每跳过 SSRF 闸，内容非图片魔数直接拒收；
    - 裸 ``/uploads/...`` 路径：从本服务图床解析并**复制**（不是引用）——图床 7 天懒清理，
      clip 的 TTL 是独立的，引用会在图床过期后让重查/重发的镜找不到图。

    在 POST 里同步执行：坏图当场 400，不建任务；建完任务再发现图坏了只能落 error 行，运营
    还得回头清理。
    """
    source = (source or "").strip()
    workdir.mkdir(parents=True, exist_ok=True)
    if source.startswith(("http://", "https://")):
        _assert_public_host(urlparse(source).hostname)
        try:
            data = await asyncio.wait_for(
                _fetch_remote_image(source), timeout=_REMOTE_FETCH_TIMEOUT)
        except asyncio.TimeoutError:
            raise ValueError(
                f"参考图下载超时（{_REMOTE_FETCH_TIMEOUT:.0f}s 总时长上限）：{source}"
            ) from None
        ext = _sniff_ext(data)
        if ext is None:
            raise ValueError(f"参考图不是有效图片（按内容判定，非扩展名）：{source}")
        target = workdir / f"ref{ext}"
        target.write_bytes(data)
        return target
    if source.startswith("/uploads/"):
        local = _uploads_local_file(source)
        if local is None:
            # 形态对但文件不在：图床 7 天懒清理过 / 路径越界。单独给话，别混进「形态不对」里。
            raise ValueError(
                f"参考图在本服务图床里找不到（可能已过期清理或路径越界）：{source[:120]}")
        ext = local.suffix.lower() or ".jpg"
        target = workdir / f"ref{ext}"
        shutil.copyfile(local, target)
        return target
    raise ValueError(
        f"参考图只接受图床直链（http/https）或本服务 /uploads 路径，收到：{source[:120]}"
    )


# ── 提交参数组装 ────────────────────────────────────────────────────────────
def build_submit_args(clip: VideoClip) -> list[str]:
    """按 operation 组装 dreamina 生成子命令参数（与 skill 侧本地实现逐字同形）。

    ``--poll=0`` 纯提交不等待：即梦排队常达数小时，服务端只拿 submit_id 就走，轮询交给
    poll 阶段。``--video_resolution=720p``：CLI 升级后该参数**必填且严格校验**，各档支持的
    分辨率并不一致（2.5 只有 480p/720p、2.0_vip 到 4k、其余只有 720p），**720p 是唯一对全家族
    都合法的一档**，故继续统一传它。``image2video`` **一律不带 --ratio**（画幅由输入图推断，
    CLI 不收该参数）。
    """
    common = [
        f"--prompt={clip.prompt}",
        f"--duration={clip.duration}",
        f"--model_version={clip.model}",
        "--video_resolution=720p",
        "--poll=0",
    ]
    if clip.operation == "image2video":
        return ["image2video", f"--image={clip.image_path}", *common]
    if clip.operation == "multimodal2video":
        args = ["multimodal2video", f"--image={clip.image_path}", *common]
        if clip.ratio:
            args.append(f"--ratio={clip.ratio}")
        return args
    args = ["text2video", *common]
    if clip.ratio:
        args.append(f"--ratio={clip.ratio}")
    return args


# 锚定成**恰好** 16 位：不带边界时 32 位 md5 / 更长的 logid 的前 16 位也会命中，
# 把「错误原文里的 trace id」当成 submit_id。
_SUBMIT_ID_RE = re.compile(r"(?<![0-9a-f])[0-9a-f]{16}(?![0-9a-f])")


def extract_submit_id(data, blob: str | None) -> str | None:
    """从 CLI 回执里取 submit_id：优先结构化字段，退而从原文里捞 16 位 hex。

    ``blob`` 传 None = **禁用正则兜底**（只认结构化字段）。调用方在 CLI 明确失败时必须这么传：
    字节系错误响应常带 logid / request_id / trace id，正则会把它当 submit_id，让一条**确定失败**
    的任务落成 submitted + 假 id，poll 永远查不到、无终态出口，运营看到的却是「在正常排队」。
    """
    if isinstance(data, dict):
        for key in ("submit_id", "submitId", "task_id"):
            val = data.get(key)
            if isinstance(val, str) and val.strip():
                return val.strip()
        inner = data.get("data")
        if isinstance(inner, dict):
            val = inner.get("submit_id")
            if isinstance(val, str) and val.strip():
                return val.strip()
    m = _SUBMIT_ID_RE.search(blob or "")
    return m.group(0) if m else None


# ── 台账查询 helper（REST 层消费）────────────────────────────────────────────
async def find_by_client_ref(session, created_by: int, client_ref: str | None):
    """按 (运营, 幂等键) 查已有任务；无 ref 或未命中返回 None。"""
    if not client_ref:
        return None
    stmt = (select(VideoClip)
            .where(VideoClip.created_by == created_by)
            .where(VideoClip.client_ref == client_ref))
    return (await session.execute(stmt)).scalars().first()


def is_revivable(clip: VideoClip) -> bool:
    """这条 error 行是否**从没让 dreamina CLI 为它跑过一次**（=资金状态确定为「没花钱」）。

    幂等键去重命中一条 error 行时，要区分它的**资金状态**再决定能否复活重试：

    - 物化参考图失败 / 建行即 error 这类：CLI 一次都没被调起，即梦队列里没有它，积分没动。
      图源瞬时故障（图床 502、域名抖动）后运营修好图用同 ref 重放，这行**必须能复活**，
      否则那一镜被这条 error 行永久占着幂等键、再也生成不出来（而单镜端点同样入参是 400
      不建行、重放就能成功——不对称）。
    - 歧义结局（submit 超时 / rc=0 无 submit_id）、CLI 明确失败、合规授权失败、
      ``submitting`` 残留自愈：CLI 已经被调起过，任务是否入队/是否扣分**未知或已知花过**，
      一律不复活（复活 = 赌它没入队，赌输就是双倍扣分且排队中无法取消）。

    判据全是**结构化字段**，绝不解析 error 文案（文案会被改、会被截断、还带 CLI 原文）：
    调度器在原子认领的那一刻就写 ``submitted_at``，故
    ``submitted_at IS NULL`` ⟺ 这行从没被认领过 ⟺ CLI 从没为它跑过。
    ``submit_id IS NULL`` 是同向的第二道保险（拿到 submit_id 就是确定入了队）。
    """
    return (clip.status == "error"
            and clip.submit_id is None
            and clip.submitted_at is None)


async def get_by_clip_id(session, clip_id: str):
    """按对外句柄取任务；不存在返回 None。"""
    return (await session.execute(
        select(VideoClip).where(VideoClip.clip_id == clip_id)
    )).scalars().first()


async def compliance_confirmed_models(session) -> list[str]:
    """DB 里有 done 记录的 distinct model 列表（合规授权状态的**观测近似**）。

    CLI 没有「查某模型是否已授权」的接口，能确证的只有「这个模型真出过片」。故这是下界：
    没出现的模型不代表未授权，只代表本服务还没成功用它出过片。
    """
    rows = (await session.execute(
        select(VideoClip.model).where(VideoClip.status == "done").distinct()
    )).scalars().all()
    return sorted(m for m in rows if m)


# ── 调度器：submit 阶段 + poll 阶段 + 启动自愈 ────────────────────────────────
class DreaminaScheduler:
    """即梦片段调度：每轮先提交 queued，再轮询在飞任务。单进程、无并发（CLI 已全局串行）。

    生命周期与宿主其它后台组件同构（``start()`` / ``await stop()``，由 worker Supervisor 持有）。
    没有 VideoScheduler 那套 stage 框架 / 心跳泵 / 僵死回收——本调度不跑长任务，一轮里的
    CLI 调用都有硬超时，「僵死」在这里的等价物是 ``submitting`` 残留：启动时由
    ``heal_submitting`` 全量处置，运行期由每轮的 ``sweep_stale_submitting`` 兜住。
    """

    def __init__(self, session_factory, *, poll_interval: float | None = None,
                 query_interval: float | None = None) -> None:
        self._session_factory = session_factory
        self._poll_interval = (poll_interval if poll_interval is not None
                               else settings.CLIP_POLL_SECONDS)
        self._query_interval = (query_interval if query_interval is not None
                                else settings.CLIP_QUERY_INTERVAL)
        # clip.id → 上次 query 的 monotonic 时刻。放内存而非加列：重启后全量 poll 一轮无害
        # （query_result 是只读查询，不占队列、不扣分）。
        self._last_poll: dict[int, float] = {}
        self._stop_event: asyncio.Event | None = None
        self._loop_task: asyncio.Task | None = None

    def _should_stop(self) -> bool:
        """是否收到停机信号（未 start 的调度器恒 False，便于单测直接调阶段函数）。

        submit / poll 逐条循环里每条开头都查一次：一轮积压可能有几十条、每条 CLI 最长
        120s/300s 且全部串行，不查就意味着停机耗时 = Σ超时 → 超过 systemd TimeoutStopSec
        被 SIGKILL → 恰在 submit 中途的行留下 ``submitting`` 残留 → 下次启动自愈判 error，
        人为制造一单「是否已入队未知」的烧钱裁决。
        """
        return self._stop_event is not None and self._stop_event.is_set()

    # ---- 启动自愈 ----
    async def heal_submitting(self) -> int:
        """把遗留的 ``submitting`` 行判 error（进程崩在 submit 中途的唯一正确处置）。

        为什么不重排回 queued：``submitting`` 意味着 CLI 已经被调起过，任务**可能已经进了
        即梦队列**（submit 即占队列位、success 即扣积分）。重排 = 赌它没进去，赌输就是双倍
        扣分且排队中无法取消。故如实落 error 并写明「未知」，由运营核对后决定是否重发。
        """
        async with self._session_factory() as session:
            rows = (await session.execute(
                select(VideoClip).where(VideoClip.status == INTERNAL_SUBMITTING)
            )).scalars().all()
            for clip in rows:
                clip.status = "error"
                clip.error = f"服务重启时发现提交中途的残留任务：{AMBIGUOUS_HINT}"
                clip.finished_at = _utcnow()
            if rows:
                await session.commit()
                logger.warning(f"[dreamina] 启动自愈：{len(rows)} 条 submitting 残留判 error")
            return len(rows)

    # ---- submit 阶段 ----
    async def submit_once(self) -> int:
        """把全部 ``queued`` 逐条提交（按 id 序）。返回处理条数。"""
        async with self._session_factory() as session:
            ids = (await session.execute(
                select(VideoClip.id).where(VideoClip.status == "queued")
                .order_by(VideoClip.id)
            )).scalars().all()
        done = 0
        for clip_id in ids:
            if self._should_stop():
                break
            try:
                if await self._submit_one(clip_id):
                    done += 1
            except Exception:
                logger.exception(f"[dreamina] 提交 clip id={clip_id} 异常")
        return done

    async def _submit_one(self, row_id: int) -> bool:
        """原子认领 → 跑 CLI → 按结局落终态。**任何路径都不会二次 submit 同一 clip**。"""
        async with self._session_factory() as session:
            res = await session.execute(
                update(VideoClip)
                .where(VideoClip.id == row_id)
                .where(VideoClip.status == "queued")
                # 认领即写 submitted_at：它同时是「这行被 CLI 碰过」的**结构化标记**，
                # ①`sweep_stale_submitting` 靠它算认领时长；②`is_revivable` 靠它把
                # 「CLI 从没跑过（可安全复活）」与「跑过、资金状态未知（绝不复活）」分开。
                # 提交成功时会被刷成真正的提交时刻，故 queued_seconds 的语义不受影响。
                .values(status=INTERNAL_SUBMITTING, submitted_at=_utcnow())
            )
            await session.commit()
            if res.rowcount != 1:
                return False  # 别处已认领 / 状态已变，绝不重复提交
            clip = await session.get(VideoClip, row_id)
            clip_id = clip.clip_id
            args = build_submit_args(clip)

        rc, data, blob = await cli_submit(args)
        # **正则兜底只在 rc==0 时启用**：CLI 明确失败时错误原文里的 16 位 hex（trace id /
        # request_id）会被当成 submit_id，把「确定失败」伪装成「在排队」。结构化字段任何 rc
        # 都认——它是「CLI 真的把任务交出去了」的硬证据，丢了才会出「扣了分却没人去取片」。
        submit_id = extract_submit_id(data, blob if rc == 0 else None)

        claimed = True
        if rc == _RC_LOCK_BUSY:
            # 另一进程占着 CLI，本次**根本没调起它** → 任务确定没入队，复位回 queued 下轮再提。
            # 这是唯一可以安全复位的失败：其余路径 CLI 都已被调起，复位就是赌双扣。
            values = {"status": "queued"}
            logger.info(f"[dreamina] {clip_id} 等 CLI 锁超时，复位 queued 下轮再提")
            claimed = False
        elif COMPLIANCE_MARKER in blob:
            # 合规授权：任务确定没入队，需人到网页端做一次性授权，服务端重试无意义。
            # 放在 submit_id 之前——原文里的 hex 不是 submit_id，别把它吞成「已提交」。
            values = {"status": "error", "error": blob[:1500] + COMPLIANCE_HINT,
                      "finished_at": _utcnow()}
            logger.warning(f"[dreamina] {clip_id} 需合规授权：{blob[:200]}")
        elif submit_id:
            values = {"status": "submitted", "submit_id": submit_id,
                      "submitted_at": _utcnow(), "error": None}
            logger.info(f"[dreamina] {clip_id} 已提交 submit_id={submit_id}")
        elif rc == _RC_TIMEOUT or rc < 0:
            # 超时 / 被信号打断：任务是否已入队未知 → 歧义结局，绝不重提
            values = {"status": "error", "error": f"{AMBIGUOUS_HINT}。CLI 原文：{blob[:800]}",
                      "finished_at": _utcnow()}
            logger.warning(f"[dreamina] {clip_id} 提交歧义结局(rc={rc})，判 error 不重提")
        elif rc != 0:
            # CLI 明确报错：任务没入队，错误原文透出（无输出时至少给出 rc）
            values = {"status": "error",
                      "error": (blob or f"dreamina 提交失败(rc={rc})，CLI 无输出")[:1800],
                      "finished_at": _utcnow()}
            logger.warning(f"[dreamina] {clip_id} 提交失败(rc={rc})：{blob[:200]}")
        else:
            # rc==0 却拿不到 submit_id：歧义结局（CLI 可能已提交但回执异常）
            values = {"status": "error",
                      "error": f"{AMBIGUOUS_HINT}。CLI 未返回 submit_id，原文：{blob[:800]}",
                      "finished_at": _utcnow()}
            logger.warning(f"[dreamina] {clip_id} 提交无 submit_id，判 error 不重提")

        async with self._session_factory() as session:
            # 落库带 ``WHERE status='submitting'`` 守卫：CLI 一跑可能几十秒到 120s，
            # 这期间别人（启动自愈 / sweep / 人工改库）可能已经把这行改成别的状态了。
            # 盲写会把一条已被判 error 的行顶回 submitted，或把自愈刚写的「未知」结论抹掉。
            res = await session.execute(
                update(VideoClip)
                .where(VideoClip.id == row_id)
                .where(VideoClip.status == INTERNAL_SUBMITTING)
                .values(**values)
            )
            await session.commit()
        if res.rowcount != 1:
            # 只记不补写：这行现在的状态是别人按自己的证据写的，我们这份结论已经过期。
            # submit_id 一并打进日志——它是钱的凭据，写不进库至少要能从日志里捞回来对账。
            logger.warning(
                f"[dreamina] {clip_id} 提交结果落库被跳过（认领态已被改写，rowcount="
                f"{res.rowcount}），本次结论 status={values.get('status')} "
                f"submit_id={values.get('submit_id')}"
            )
            return False
        return claimed

    async def sweep_stale_submitting(self) -> int:
        """把认领后久久不落终态的 ``submitting`` 行判 error（话术同启动自愈）。

        启动自愈只在进程起来的那一刻扫一次，可**运行期**照样会留下 submitting 残留：
        第二段落库时 DB 抖动 / 协程被取消 / 落库守卫 rowcount 异常。不扫的话那一行就永远
        卡在内部态——对外一直显示 queued，submit 阶段又只捡 queued，运营看着「在排队」
        其实没有任何东西在推进它。

        阈值取 ``3 × CLIP_SUBMIT_TIMEOUT``：单次 CLI 提交最长就是 CLIP_SUBMIT_TIMEOUT，
        再宽放三倍（含等 CLI 锁的 10s 与落库耗时），确保绝不误伤**正在提交中**的行。
        处置仍是 error 而不是复位 queued——理由与启动自愈逐字相同：CLI 已被调起，
        任务是否已入即梦队列未知，复位就是赌双扣。
        """
        stale_after = settings.CLIP_SUBMIT_TIMEOUT * 3
        cutoff = _utcnow() - timedelta(seconds=stale_after)
        async with self._session_factory() as session:
            rows = (await session.execute(
                select(VideoClip)
                .where(VideoClip.status == INTERNAL_SUBMITTING)
                .where(VideoClip.submitted_at.is_not(None))
                .where(VideoClip.submitted_at < cutoff)
            )).scalars().all()
            for clip in rows:
                clip.status = "error"
                clip.error = f"提交认领后 {stale_after}s 仍未落终态：{AMBIGUOUS_HINT}"
                clip.finished_at = _utcnow()
            if rows:
                await session.commit()
                logger.warning(f"[dreamina] sweep：{len(rows)} 条 submitting 滞留判 error")
            return len(rows)

    # ---- poll 阶段 ----
    async def poll_once(self) -> int:
        """轮询到期的在飞任务（submitted/querying 且距上次查询 ≥ query_interval）。"""
        now = time.monotonic()
        async with self._session_factory() as session:
            rows = (await session.execute(
                select(VideoClip.id).where(VideoClip.status.in_(_IN_FLIGHT))
                .order_by(VideoClip.id)
            )).scalars().all()
        polled = 0
        for row_id in rows:
            if self._should_stop():
                break
            last = self._last_poll.get(row_id)
            if last is not None and now - last < self._query_interval:
                continue
            try:
                await self._poll_one(row_id)
                polled += 1
            except Exception:
                logger.exception(f"[dreamina] 轮询 clip id={row_id} 异常")
        return polled

    async def _poll_one(self, row_id: int) -> None:
        """查一条在飞任务：success 落产物、failed 落 error、其余保持排队语义。"""
        async with self._session_factory() as session:
            clip = await session.get(VideoClip, row_id)
            if clip is None or clip.status not in _IN_FLIGHT or not clip.submit_id:
                return
            clip_id, submit_id = clip.clip_id, clip.submit_id

        workdir = clip_dir(clip_id)
        rc, data, blob = await cli_query(submit_id, str(workdir))
        if rc == _RC_LOCK_BUSY:
            # 另一进程占着 CLI：本轮压根没查，不写 last_poll_error（那是「查询接口故障」的语义），
            # 也不占节流位——下一轮立刻重来。
            return
        self._last_poll[row_id] = time.monotonic()
        gen = data.get("gen_status") if isinstance(data, dict) else None

        async with self._session_factory() as session:
            clip = await session.get(VideoClip, row_id)
            if clip is None or clip.status not in _IN_FLIGHT:
                return
            if gen is None:
                # 查询侧瞬时故障（网络/CLI 抖动/非 JSON）：**只记不改 status**。
                # 排队中的任务无法取消，把查询失败写成终态会让运营误判任务已死而重发（=烧钱）。
                clip.last_poll_error = (blob or f"query_result 未返回 gen_status(rc={rc})")[:800]
                await session.commit()
                return
            if gen == "success":
                names = _rename_products(data, workdir)
                if not names:
                    # success 却没拿到视频路径：当查询侧异常处理（保住任务，下轮再查），
                    # 绝不落假 done——done 但没 video_url 会让 skill 侧「done 但没带 video_url」。
                    clip.last_poll_error = "gen_status=success 但未取到视频路径，下轮重查"
                    await session.commit()
                    return
                clip.status = "done"
                clip.video_path = str(workdir / names[0])
                clip.video_url = clip_public_url(clip_id, names[0])
                credit = data.get("credit_count")
                clip.credit_count = int(credit) if isinstance(credit, (int, float)) else None
                clip.finished_at = _utcnow()
                clip.expires_at = _utcnow() + timedelta(days=settings.CLIP_TTL_DAYS)
                clip.error = None
                clip.last_poll_error = None
                logger.info(f"[dreamina] {clip_id} 完成，积分 {clip.credit_count}")
            elif gen in ("failed", "fail", "error", "not_pass", "rejected"):
                clip.status = "error"
                reason = data.get("fail_reason") or f"任务 {gen}"
                clip.error = (str(reason)[:1500] + COMPLIANCE_HINT
                              if COMPLIANCE_MARKER in str(reason) else str(reason)[:1800])
                clip.finished_at = _utcnow()
                logger.warning(f"[dreamina] {clip_id} 失败：{reason}")
            else:
                # querying 及其它中间态：正常排队（高峰 fast 近 2 小时），清瞬时错误标记
                clip.status = "querying"
                clip.last_poll_error = None
            await session.commit()
            if clip.status not in _IN_FLIGHT:
                self._last_poll.pop(row_id, None)   # 已终态，别把节流表留成只增不减

    # ---- 生命周期 ----
    def start(self) -> None:
        """起后台循环（先自愈残留 submitting，再进主循环）。"""
        self._stop_event = asyncio.Event()
        self._loop_task = asyncio.create_task(self._run_loop())

    async def _run_loop(self) -> None:
        try:
            await self.heal_submitting()
        except Exception:
            logger.exception("[dreamina] 启动自愈失败（继续主循环）")
        while self._stop_event is not None and not self._stop_event.is_set():
            try:
                # 先扫滞留的 submitting 残留再提交：运行期产生的残留不该等到下次重启才被处置。
                await self.sweep_stale_submitting()
            except Exception:
                logger.exception("[dreamina] submitting 滞留巡检异常")
            try:
                await self.submit_once()
            except Exception:
                logger.exception("[dreamina] 提交轮次异常")
            try:
                await self.poll_once()
            except Exception:
                logger.exception("[dreamina] 轮询轮次异常")
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=self._poll_interval)
            except asyncio.TimeoutError:
                pass

    async def stop(self) -> None:
        """优雅停：置信号 → 等主循环退出（**不 cancel**）。

        逐条循环会在每条开头查停机信号，故最长只等当前这一条 CLI 调用。cancel 反而更糟：
        取消恰在 submit 中的协程会留下 ``submitting`` 残留 + 一个不知是否已入队的即梦任务，
        正是本模块最想避免的那类烧钱裁决。
        """
        if self._stop_event is not None:
            self._stop_event.set()
        if self._loop_task is not None:
            await self._loop_task
            self._loop_task = None


def _rename_products(data: dict, workdir: Path) -> list[str]:
    """把 CLI 下载到 workdir 的 ``{submit_id}_video_N.mp4`` 改名成 ``clip.mp4`` / ``clip_2.mp4``…

    改名而非直接暴露原名：免鉴权直链的文件名走白名单（``^clip(_\\d{1,2})?\\.mp4$``），
    原名带 submit_id 会把即梦任务号泄进 URL。返回落位后的文件名列表（按原序）。
    """
    videos = (data.get("result_json") or {}).get("videos") or []
    names: list[str] = []
    for i, item in enumerate(videos):
        src = (item or {}).get("path")
        if not src:
            continue
        src_path = Path(src)
        if not src_path.is_file():
            continue
        name = "clip.mp4" if not names else f"clip_{len(names) + 1}.mp4"
        target = workdir / name
        if src_path.resolve() != target.resolve():
            os.replace(src_path, target)
        names.append(name)
    return names


# ── 产物 TTL 清理（仿 ArchiveReaper）────────────────────────────────────────
def _rmtree_if_exists(path: Path) -> bool:
    """目录存在才删；返回是否真删了（让清理计数只反映真实动作，不会每轮虚报）。"""
    if not path.is_dir():
        return False
    shutil.rmtree(path, ignore_errors=True)
    return True


async def reap_clips_once(session_factory) -> int:
    """产物 TTL 清理，三类各自独立；返回本轮真实清理的条数（行 + 目录）。

    1. **done 且产物过期**：删工作目录 + 清 ``video_url`` / ``video_path``。
       ``status`` 保持 done、``credit_count`` **保留**——积分对账要用，把行删掉等于毁账。
       **不往 ``error`` 里写清理说明**：``error`` 只装「任务失败原因」这一种语义，掺进
       「产物过期了」会让运营（和 skill 侧判 error 的分支）把一条成功的片当成失败。
       产物是否还在，走 GET 视图算出来的 ``expired`` 键表达。
    2. **error 终态超 TTL**：只删工作目录，**行保留**——error 文案是运营复盘/对账的依据。
       不清这类目录的话，参考图副本会永久堆在盘上（失败越多堆越多，且永远没人来收）。
    3. **无主孤儿目录**：``uploads/clips/`` 下没有对应 DB 行、且 mtime 超 TTL 的目录。
       来源是「先建目录物化参考图、再插行」这个顺序中途失败（校验 4xx / 进程崩）留下的半截。
       卡 TTL 而不是「没行就删」，正是为了不误杀正在物化、行还没插进去的那几秒。

    纯函数（只吃 session_factory），可脱离后台循环单测。
    """
    now = _utcnow()
    ttl_cutoff = now - timedelta(days=settings.CLIP_TTL_DAYS)
    root = clips_root()
    reaped = 0
    async with session_factory() as session:
        expired_rows = (await session.execute(
            select(VideoClip)
            .where(VideoClip.status == "done")
            .where(VideoClip.video_url.is_not(None))
            .where(VideoClip.expires_at.is_not(None))
            .where(VideoClip.expires_at < now)
        )).scalars().all()
        for clip in expired_rows:
            _rmtree_if_exists(root / clip_token_dir(clip.clip_id))
            clip.video_url = None
            clip.video_path = None
        if expired_rows:
            await session.commit()
            reaped += len(expired_rows)
            logger.info(f"[dreamina] TTL 清理过期片段产物 {len(expired_rows)} 条")

        # 终态 error 的工作目录（行保留）。finished_at 缺失时退回 created_at 兜底：
        # 老行 / 异常路径可能没写 finished_at，没有兜底它们的目录就永远不会被回收。
        error_rows = (await session.execute(
            select(VideoClip).where(VideoClip.status == "error")
        )).scalars().all()
        error_reaped = 0
        for clip in error_rows:
            stamp = clip.finished_at or clip.created_at
            if stamp is None or stamp >= ttl_cutoff:
                continue
            if _rmtree_if_exists(root / clip_token_dir(clip.clip_id)):
                error_reaped += 1
        if error_reaped:
            reaped += error_reaped
            logger.info(f"[dreamina] TTL 清理 error 终态工作目录 {error_reaped} 个")

        known = {clip_token_dir(cid) for cid in (await session.execute(
            select(VideoClip.clip_id))).scalars().all()}

    orphan_reaped = 0
    if root.is_dir():
        mtime_cutoff = time.time() - settings.CLIP_TTL_DAYS * 86400
        for entry in root.iterdir():
            if not entry.is_dir() or entry.name in known:
                continue
            try:
                if entry.stat().st_mtime >= mtime_cutoff:
                    continue
            except OSError:          # 正被别的轮次删掉 / 权限异常，交给下一轮
                continue
            if _rmtree_if_exists(entry):
                orphan_reaped += 1
    if orphan_reaped:
        reaped += orphan_reaped
        logger.info(f"[dreamina] TTL 清理无主孤儿目录 {orphan_reaped} 个")
    return reaped


class ClipReaper:
    """片段产物 TTL 后台循环（结构与 ArchiveReaper / PlaceholderReaper 一致）。

    先睡后扫 + interval==0 不启（由调用方判）+ 单轮异常不崩循环 + stop() 优雅取消。
    """

    def __init__(self, session_factory, interval: float) -> None:
        self._session_factory = session_factory
        self._interval = interval
        self._stop_event: asyncio.Event | None = None
        self._loop_task: asyncio.Task | None = None

    def start(self) -> None:
        self._stop_event = asyncio.Event()
        self._loop_task = asyncio.create_task(self._run_loop())

    async def _run_loop(self) -> None:
        while self._stop_event is not None and not self._stop_event.is_set():
            await self._sleep(self._interval)
            if self._stop_event is not None and self._stop_event.is_set():
                break
            try:
                await reap_clips_once(self._session_factory)
            except Exception:
                logger.exception("[dreamina] 产物 TTL 清理轮次异常")

    async def _sleep(self, timeout: float) -> None:
        if self._stop_event is None:
            await asyncio.sleep(timeout)
            return
        try:
            await asyncio.wait_for(self._stop_event.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            pass

    async def stop(self) -> None:
        if self._stop_event is not None:
            self._stop_event.set()
        if self._loop_task is not None:
            await self._loop_task
            self._loop_task = None
