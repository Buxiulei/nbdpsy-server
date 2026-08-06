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
唯一对全家族都合法的一档；``image2video`` 画幅由输入图推断、不接受 ``--ratio``；
``multiframe2video`` 自成一格——图列表是逗号串的 ``--images``、模型平台固定不可配、
时长按 N-1 段逐段给（详见 ``_multiframe_args``）。

非阻塞红线：本模块跑在 worker 的单一事件循环上（与 supervisor 扫描、视频调度共享），
所有 CLI 调用一律 ``asyncio.create_subprocess_exec``，**禁止 subprocess.run**。
"""

import asyncio
import fcntl
import hashlib
import hmac
import ipaddress
import json
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
# 默认档（六档模型 / 六种画幅 / 五种 operation 的枚举闸在 REST 层的 Literal 上，不在此重复）。
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

# multimodal2video 的参考图张数上限，同样**按模型分档**（CLI help 原文：
# ``seedance2.5 -> image<=30``；``seedance2.0 family/seedance2.0mini -> image<=9``）。
# 网上流传的「Seedance 最多 9 张」是 **2.0** 的数字，套到 2.5 上会白砍掉 21 个图槽——
# 那些槽正是跨镜锁人物一致性（本镜场景图 + 全片人物定妆图 + 关键道具图）要用的。
REF_IMAGE_MAX = 30
_REF_IMAGE_MAX_DEFAULT = 9
_REF_IMAGE_MAX_BY_MODEL = {"seedance2.5": REF_IMAGE_MAX}

# multimodal2video 的参考**视频**条数上限（CLI help 原文：``seedance2.5 -> video<=10``；
# ``seedance2.0 family/seedance2.0mini -> video<=3``）。CLI 的 ``--video stringArray``
# 一直都在，是本服务此前没透传——参考视频的用途是把上一部片里成立的运镜/风格当下一部片的
# 参考，与参考图各占一路输入面。**audio 仍未透传**（运营未要求，本模块也没有 --audio 的组装）。
REF_VIDEO_MAX = 10
_REF_VIDEO_MAX_DEFAULT = 3
_REF_VIDEO_MAX_BY_MODEL = {"seedance2.5": REF_VIDEO_MAX}

# 总输入上限（CLI help：``total inputs<=50``/``<=12``）。**本服务不为它单设闸**——audio 不
# 透传，总输入 = 图 + 视频，而分项闸已把上界钉死在 30+10=40≤50、9+3=12≤12，那道闸永远不会响。
# 常量仍照 CLI 原文留着，由 test_total_inputs_cap_is_implied_by_per_kind_caps 守住这个推导：
# 分项上限被放宽 / audio 开了透传时它立刻红，提醒补闸。
TOTAL_INPUTS_MAX = 50
_TOTAL_INPUTS_MAX_DEFAULT = 12
_TOTAL_INPUTS_MAX_BY_MODEL = {"seedance2.5": TOTAL_INPUTS_MAX}

# 参考视频的时长窗口（CLI help 原文："each and total video/audio duration 2-30s"，
# 2.0 家族是 2-15s）。**每条**与**合计**同一个窗口——数字与出片时长上限撞巧一样，但那是
# 两件事（一个是输入素材有多长，一个是出片有多长），故各存各的表，不互相引用。
REF_VIDEO_SECONDS_MIN = 2.0
REF_VIDEO_SECONDS_MAX = 30.0
_REF_VIDEO_SECONDS_MAX_DEFAULT = 15.0
_REF_VIDEO_SECONDS_MAX_BY_MODEL = {"seedance2.5": REF_VIDEO_SECONDS_MAX}

# multiframe2video（多图连贯故事）的口径，全部照 CLI help 原文，**与上面几档无关**：
# "inputs: 2-20 images"、"for N images, the transition count is N-1"、
# "each duration segment must be 1-8 seconds and total duration must be >= 2"、
# "omit --transition-duration to default each segment to 3 seconds"。
# 张数上限**不看 model**——这条子命令的模型由平台固定（"model_version is fixed and is not
# configurable on this command"），所以 max_ref_images 那套按档分级在这里不适用。
MULTIFRAME_IMAGE_MIN = 2
MULTIFRAME_IMAGE_MAX = 20
MULTIFRAME_SEGMENT_MIN = 1.0
MULTIFRAME_SEGMENT_MAX = 8.0
MULTIFRAME_TOTAL_MIN = 2.0
# CLI 省略 --transition-duration 时每段的默认秒数。只用来**估算台账里的总时长**，
# 提交时绝不把它拼成显式参数——那会把 CLI 将来可能改的默认值钉死在我们这边。
MULTIFRAME_DEFAULT_SEGMENT = 3.0
# multiframe 行的 ``model`` 列存这个占位符，**绝不存真实档位名**。这条子命令的模型由平台
# 按 TCC 下发、CLI 不接受 --model_version，我们无从得知它到底用了哪档；写 seedance2.5 进去
# 就是在库里记一条**我们明知不成立**的事实，日后排查 / 统计 / 对账的人会被骗且毫无痕迹。
# 故意取一个不像档位名的值：下游一眼能看出「这列对这条 operation 不适用」，而 NULL 会被
# 当成「老行 / 漏写」。真实档位要等跑一条真任务读 list_task 的 benefit_type 才能确知。
MULTIFRAME_MODEL_PLACEHOLDER = "platform_fixed"

# 5s/720p 单镜实测价（仅实测档有值；未知档不估、不给 warning，绝不瞎猜）。
# seedance2.5 = 130/5s = **26/秒，按秒线性**（credit_count 与余额扣减逐次对账）：
#   - 2026-08-05 vc_3e1260f8ce  5s  → 130
#   - 2026-08-06 vc_9090b4f40b  10s → 260
#   - 2026-08-06 vc_5d0ec24ff7  10s → 260
#   - 2026-08-06 vc_0cf759e417  4s  → **104**（= 26×4）
# 前三条是 5 的整数倍，「按秒」与「按 5s 档取整」在这些点同值、分不出高下；**4s=104 是判别
# 点**：按档取整会算 130，与实扣差 26。故估算按秒线性（见 estimate_credit）。
# **seedance2.0mini / seedance2.0 / seedance2.0_vip 仍不编价**（从未实测）；
# multiframe2video 恒不估（模型由平台下发，本表不适用，见 estimate_credit）。
_PRICE_PER_5S = {"seedance2.0fast": 25, "seedance2.0fast_vip": 55, "seedance2.5": 130}
# 提交闸的**保守**下限。按秒计价后理论上最便宜的一镜是 4s fast = 20，这里取 25（5s fast 的
# 价）：余额落在 [20, 25) 时其实还提得起一镜 4s fast，会被多拦一次。这是有意为之——它是破产
# 线不是预算线，多拦的那一格只影响「余额见底且恰好发 4s fast 单」这种边角，而放宽一道拦截闸
# 换不回什么。409 文案与 manifest 都只引这个常量、不手写数字，改这里即全线同步；只有
# _guard_credit 的 docstring 复述了「25 = 5s fast」这个理由，改档价时记得一并看一眼。
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


def max_ref_images(model: str) -> int:
    """该模型 multimodal2video 的参考图张数上限。未知模型按家族默认 9 判——宁可窄不宜宽，
    放宽等于让一条 CLI 必拒的任务先物化 N 张图、建行、再失败。"""
    return _REF_IMAGE_MAX_BY_MODEL.get(model, _REF_IMAGE_MAX_DEFAULT)


def max_ref_videos(model: str) -> int:
    """该模型 multimodal2video 的参考视频条数上限（未知模型按家族默认 3 判，理由同上）。"""
    return _REF_VIDEO_MAX_BY_MODEL.get(model, _REF_VIDEO_MAX_DEFAULT)


def max_total_inputs(model: str) -> int:
    """该模型 multimodal2video 的**总输入**上限（本服务口径 = 参考图 + 参考视频）。"""
    return _TOTAL_INPUTS_MAX_BY_MODEL.get(model, _TOTAL_INPUTS_MAX_DEFAULT)


def max_ref_video_seconds(model: str) -> float:
    """该模型对参考视频的时长上限（每条及合计各自适用；未知模型按家族默认 15s 判）。"""
    return _REF_VIDEO_SECONDS_MAX_BY_MODEL.get(model, _REF_VIDEO_SECONDS_MAX_DEFAULT)


def estimate_credit(model: str, duration: int, operation: str | None = None) -> int | None:
    """按秒线性估一镜积分；未知模型返回 None（不估、不给 warning）。

    只用于**提示**：扣费 success 才结算、排队中还有变数，所以低余额一律 warning 不拦截
    （需求第四节第 5 条）。真正拦截只有一种情况——余额低于 ``MIN_CLIP_CREDIT`` 那条保守线。

    ``multiframe2video`` 恒返回 None：它的模型由平台固定，我们库里那个 ``model`` 值只是
    请求默认档的占位，拿它去查价会算出 seedance2.5 的 130/5s——一个凭空来的数字，运营会
    照它做预算。真实单价等第一条任务跑完由 ``credit_count`` 结算给出（绝不瞎编价格常量）。
    """
    if operation == "multiframe2video":
        return None
    unit = _PRICE_PER_5S.get(model)
    if unit is None:
        return None
    # **按秒线性，不按 5s 档向上取整**：生产 vc_0cf759e417（2.5 / 4s）实扣 104 = 26×4，
    # 而按档取整会算 130 —— 平台是按秒计的。三档 5s 单价都能被 5 整除（25/55/130 →
    # 每秒 5/11/26），先整除再乘保证结果恒为整数、不引入浮点误差。
    return duration * (unit // 5)


def price_per_5s(model: str) -> int | None:
    """该档的 5s 实测单价；没实测过返回 None。

    manifest 的价格文案由它 + ``priced_models`` 生成，**不再手写**：手写那份在 2.5 回填后
    整整落后了一版（还挂着「2.5 未实测故不估」），运营照它做预算、照它提意见。
    """
    return _PRICE_PER_5S.get(model)


def priced_models() -> list[str]:
    """有实测单价的档位（有序，供 manifest 文案生成）。"""
    return sorted(_PRICE_PER_5S)


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


def _sniff_video_ext(data: bytes) -> str | None:
    """按容器魔数判视频扩展名；不是已知容器返回 None。

    **独立于图片白名单**，绝不往 ``_IMAGE_MAGICS`` 里掺视频容器：那条白名单是「这个字节流
    确实是一张图」的安全闸（挡 HTML 错误页/任意大文件冒充参考图），放宽它等于同时放宽
    image2video / frames2video / 多图参考的全部入口。

    认两类：ISO BMFF（mp4 / mov / m4v，特征是 4-8 字节的 ``ftyp``，major brand 以 ``qt``
    开头的是 QuickTime）与 Matroska/WebM（EBML 头 ``1A 45 DF A3``）。**不认 avi 等老容器**
    ——即梦网页端与 CLI 的实际用法都是 mp4/mov，白名单窄一点的代价是极少见的误拒（调用方
    转码即可），放宽的代价是把「是不是视频」这道闸糊掉。
    """
    if data[4:8] == b"ftyp":
        return ".mov" if data[8:10] == b"qt" else ".mp4"
    if data[:4] == b"\x1a\x45\xdf\xa3":
        return ".webm"
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
# 参考视频的那一份预算：同样的 30s 对一条几十 MB 的视频是**够不着的**（30MB 要 1MB/s 才能
# 在 30s 内拉完），照搬会让体积上限形同虚设——所有正常参考视频都在下载超时上失败。
# 仍然有硬上限（不是无限等）：批量端点的超时建议按它换算。
_REMOTE_VIDEO_FETCH_TIMEOUT = 60.0


def _assert_public_host(host: str | None) -> None:
    """拒绝指向内网/环回/链路本地的参考素材 URL（SSRF 闸）；不合规抛 ValueError。

    话术说「参考素材」而不是「参考图」：这道闸同时服务参考图与参考视频两条物化通道。

    参考素材 URL 由调用方给，服务端会**主动去 GET**——不设闸就是一个任意内网探测器：
    ``http://127.0.0.1:8000/api/...``、``http://169.254.169.254/``（云元数据）、
    ``http://10.x/`` 都会被服务端代为访问，响应内容还会以「不是有效图片 / 视频」的形式回灌。

    解析出的**每个** A/AAAA 都要合规（多 A 记录里混一条 127.0.0.1 就能绕过只查首条的实现）。
    残留 TOCTOU（这里解析完、httpx 再解析一次，之间 DNS 可能翻脸）不在本闸射程内——
    要堵得把连接固定到已校验的 IP 上，那要接管 httpx 的 transport，代价远超收益。
    """
    if not host:
        raise ValueError("参考素材 URL 缺少主机名")
    host = host.strip("[]")           # IPv6 字面量在 URL 里带方括号
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror as exc:
        raise ValueError(f"参考素材域名解析失败（{host}）：{exc}") from exc
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if (ip.is_private or ip.is_loopback or ip.is_link_local
                or ip.is_reserved or ip.is_multicast or ip.is_unspecified):
            raise ValueError(
                f"参考素材不允许内网地址（{host} 解析到 {ip}）。"
                "本服务图床的素材请直接传裸 /uploads/... 路径，不要传内网 URL"
            )


async def _fetch_remote(source: str, *, kind: str, limit_mb: int, timeout: float) -> bytes:
    """下载远程参考素材（限大小 + 每跳过 SSRF 闸）；失败抛 ValueError。

    ``kind`` 只进错误话术（「参考图」/「参考视频」），``limit_mb`` 是该类素材的体积上限
    ——视频比图片大一到两个量级，共用一个 15MB 的闸会把正常的参考视频全拒掉。
    """
    limit = limit_mb * 1024 * 1024
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
            timeout=timeout, follow_redirects=True,
            event_hooks={"request": [_guard_redirect]},
        ) as client:
            async with client.stream("GET", source) as resp:
                resp.raise_for_status()
                async for chunk in resp.aiter_bytes():
                    size += len(chunk)
                    if size > limit:
                        raise ValueError(f"{kind}超过 {limit_mb}MB 上限：{source}")
                    chunks.append(chunk)
    except httpx.HTTPError as exc:
        raise ValueError(f"{kind}下载失败（{source}）：{exc}") from exc
    return b"".join(chunks)


async def _materialize_ref(source: str, workdir: Path, stem: str, *, kind: str,
                           limit_mb: int, timeout: float, sniff, content_hint: str,
                           local_ext_default: str, sniff_local: bool = False) -> Path:
    """参考素材物化的公共实现（参考图 / 参考视频只在白名单、体积上限与话术上不同）。

    两种来源（与 note-components ``add_images`` 同款约定，**不收 base64 大包**——524 教训），
    **判据是 scheme 而不是 path 形状**：
    - ``http(s)://``：一律走远程下载（哪怕 path 长得像 ``/uploads/...``——本服务公网域名的
      完整直链就是这个样子，它经 Cloudflare 解析到公网 IP，回环下载正常）。限体积 /
      总时长 30s / 每跳过 SSRF 闸，内容不合 ``sniff`` 的魔数白名单直接拒收；
    - 裸 ``/uploads/...`` 路径：从本服务图床解析并**复制**（不是引用）——图床 7 天懒清理，
      clip 的 TTL 是独立的，引用会在图床过期后让重查/重发的镜找不到素材。

    ``sniff_local`` 决定本地来源要不要也过魔数：
    - 参考图 **False**（沿用本函数抽出来之前的行为，一字不差）——``/uploads`` 根下的图是
      上传闸校验过的，后缀可信；
    - 参考视频 **True**——最可能的误用是把一张图 / 一份成片以外的东西当参考视频传进来，
      而这里判错的代价是**一次真提交、一次真扣分**。读 16 字节换掉这个代价，值。
    """
    source = (source or "").strip()
    workdir.mkdir(parents=True, exist_ok=True)
    if source.startswith(("http://", "https://")):
        _assert_public_host(urlparse(source).hostname)
        try:
            data = await asyncio.wait_for(
                _fetch_remote(source, kind=kind, limit_mb=limit_mb, timeout=timeout),
                timeout=timeout)
        except asyncio.TimeoutError:
            raise ValueError(
                f"{kind}下载超时（{timeout:.0f}s 总时长上限）：{source}") from None
        ext = sniff(data)
        if ext is None:
            raise ValueError(f"{kind}{content_hint}：{source}")
        target = workdir / f"{stem}{ext}"
        target.write_bytes(data)
        return target
    if source.startswith("/uploads/"):
        local = _uploads_local_file(source)
        if local is None:
            # 形态对但文件不在：图床 7 天懒清理过 / 路径越界。单独给话，别混进「形态不对」里。
            raise ValueError(
                f"{kind}在本服务图床里找不到（可能已过期清理或路径越界）：{source[:120]}")
        ext = local.suffix.lower() or local_ext_default
        if sniff_local:
            # 只读文件头 32 字节（**不是 read_bytes()[:32]**：那会把整条几百 MB 的视频
            # 先读进内存，只为看前 12 个字节的容器魔数）
            with local.open("rb") as fh:
                sniffed = sniff(fh.read(32))
            if sniffed is None:
                raise ValueError(f"{kind}{content_hint}：{source[:120]}")
            ext = sniffed
        target = workdir / f"{stem}{ext}"
        shutil.copyfile(local, target)
        return target
    raise ValueError(
        f"{kind}只接受图床直链（http/https）或本服务 /uploads 路径，收到：{source[:120]}"
    )


async def materialize_ref_image(source: str, workdir: Path, *, stem: str = "ref") -> Path:
    """把参考图落成 clip 工作目录内的**独立副本**，返回本地路径；非法来源抛 ValueError。

    ``stem`` 是副本的文件名主干（扩展名按内容判）。一条 clip 可能有多张参考图（多图参考
    30 张 / 首尾帧两张），共用一个名字会让后一张盖掉前一张，故由调用方给互不相同的 stem
    （多图 ``ref``/``ref_2``…、首尾帧 ``first``/``last``）。默认值让单图调用一字不变。

    在 POST 里同步执行：坏图当场 400，不建任务；建完任务再发现图坏了只能落 error 行，运营
    还得回头清理。
    """
    return await _materialize_ref(
        source, workdir, stem, kind="参考图", limit_mb=settings.CLIP_IMAGE_MAX_MB,
        timeout=_REMOTE_FETCH_TIMEOUT, sniff=_sniff_ext,
        content_hint="不是有效图片（按内容判定，非扩展名）", local_ext_default=".jpg")


async def materialize_ref_video(source: str, workdir: Path, *, stem: str = "vid") -> Path:
    """把参考视频落成 clip 工作目录内的**独立副本**，返回本地路径；非法来源抛 ValueError。

    与参考图共用一条实现，但**走独立的视频魔数白名单**（``_sniff_video_ext``）与独立的体积
    上限 ``CLIP_VIDEO_MAX_MB``——绝不为了收视频去放宽图片那条白名单（那是安全闸）。

    ``stem`` 默认 ``vid``，多条时由调用方给 ``vid``/``vid_2``…；与参考图的 ``ref*`` /
    ``first`` / ``last`` 不撞名，两类素材可以同时落在一个 clip 工作目录里。
    """
    return await _materialize_ref(
        source, workdir, stem, kind="参考视频", limit_mb=settings.CLIP_VIDEO_MAX_MB,
        timeout=_REMOTE_VIDEO_FETCH_TIMEOUT, sniff=_sniff_video_ext,
        content_hint="不是有效视频容器（按内容魔数判定，非扩展名；支持 mp4 / mov / webm）",
        local_ext_default=".mp4", sniff_local=True)


async def check_ref_video_durations(paths: list[str], model: str) -> str | None:
    """物化后用 ffprobe 前置校验参考视频时长；越界返回中文说明，合规返回 None。

    CLI 口径 "each **and total** video/audio duration 2-30s"（2.0 家族 2-15s）：每条与合计
    各自都要在窗口内，故两道都判。放在服务端做是为了**省一次白跑**——不判的话越界要等 CLI
    在 worker 侧拒收，那时行已建、素材已下载，运营看到的是一条要人回头清理的 error 行。

    ``probe_duration`` 探不出（ffprobe 缺失 / 容器怪异）时**跳过那一条不拦**，与
    ``get_video_clip_frame`` 的越界判定同一条纪律：把「探测器不给力」变成拒绝服务是本末倒置，
    真越界了 CLI 那边还有一道。
    """
    if not paths:
        return None
    ceiling = max_ref_video_seconds(model)
    seconds = await asyncio.gather(*(probe_duration(Path(p)) for p in paths))
    total = 0.0
    for index, value in enumerate(seconds):
        if value is None:
            continue                 # 探不出：不拦（真越界由 CLI 兜底）
        if not (REF_VIDEO_SECONDS_MIN <= value <= ceiling):
            return (f"第 {index + 1} 条参考视频时长 {value:.1f}s 越界：{model} 要求每条 "
                    f"{REF_VIDEO_SECONDS_MIN:.0f}-{ceiling:.0f}s"
                    f"（仅 seedance2.5 支持到 {REF_VIDEO_SECONDS_MAX:.0f}s）")
        total += value
    if total > ceiling:
        return (f"参考视频合计时长 {total:.1f}s 越界：{model} 要求合计也在 "
                f"{REF_VIDEO_SECONDS_MIN:.0f}-{ceiling:.0f}s 内"
                "（CLI 口径 each and total video duration）")
    return None


# ── 提交参数组装 ────────────────────────────────────────────────────────────
def ref_paths(clip: VideoClip) -> list[str]:
    """这条 clip 的参考图本地副本路径（**顺序即语义**，见 VideoClip.image_paths_json）。

    新行两列都写：``image_paths_json`` 是权威全集，``image_path`` 存第一张。多图列上线
    **之前**建的行只有 ``image_path``，故这里 json 优先、回落单列——老行的参数组装因此
    与改前逐字节一致。json 坏掉时同样回落：这列只装路径，一个坏值不该让一条已建好的
    任务永远提交不出去。
    """
    if clip.image_paths_json:
        try:
            paths = json.loads(clip.image_paths_json)
        except json.JSONDecodeError:
            paths = None
        if isinstance(paths, list) and paths:
            return [str(p) for p in paths]
    return [clip.image_path] if clip.image_path else []


def ref_video_paths(clip: VideoClip) -> list[str]:
    """这条 clip 的参考视频本地副本路径（**顺序即语义**，见 VideoClip.video_paths_json）。

    没有 ``image_path`` 那种单列回落：本列上线前的老行一律没有参考视频，空列表就是它们的
    正确答案。坏 JSON 同样按空处理——理由与 ``ref_paths`` 一样，一个坏值不该让一条已建好的
    任务永远提交不出去（少一路参考的片仍是可用产出，而卡死的行要人来收）。
    """
    if not clip.video_paths_json:
        return []
    try:
        paths = json.loads(clip.video_paths_json)
    except json.JSONDecodeError:
        return []
    return [str(p) for p in paths] if isinstance(paths, list) else []


def transitions(clip: VideoClip) -> list[dict]:
    """这条 clip 的逐段转场（``[{"prompt": str, "duration": float|None}, …]``）。

    只有 multiframe2video 的长式有值；简写（恰好 2 张走 prompt + duration）与其余四种
    operation 恒为空列表。坏 JSON 按空处理并回落简写分支——理由同 ``ref_paths``。
    """
    if not clip.transitions_json:
        return []
    try:
        items = json.loads(clip.transitions_json)
    except json.JSONDecodeError:
        return []
    return [t for t in items if isinstance(t, dict)] if isinstance(items, list) else []


def _multiframe_args(clip: VideoClip, paths: list[str]) -> list[str]:
    """multiframe2video 的参数（形态与另四个子命令差得多，单独拆出来）。

    三处与别处**不一样**，都照 CLI help 原文：

    1. flag 名是 ``--images``（``strings`` 类型，逗号连接一次给全），不是别处那个可重复的
       ``--image``。CLI 自己的示例就是 ``--images ./a.png,./b.png``。前提是路径里不含逗号
       ——路径由我们自己生成（``{clip_dir}/ref_N.ext``），只有 DATA_DIR 含逗号才会出事。
    2. **不带 ``--model_version``**："model_version is fixed and is not configurable on this
       command"。带上去就是给一个 CLI 不认的参数。
    3. **不带 ``--ratio``**："ratio is inferred from the first image"（REST 层已 422 拦过）。

    形态二选一：恰好 2 张可走简写 ``--prompt`` + ``--duration``；否则逐段
    ``--transition-prompt`` ×(N-1)。逐段时长全为 null 时整个 flag 不出现，交给 CLI 的默认。
    """
    args = ["multiframe2video", f"--images={','.join(paths)}",
            "--video_resolution=720p", "--poll=0"]
    segments = transitions(clip)
    if not segments:
        args.append(f"--prompt={clip.prompt}")
        args.append(f"--duration={clip.duration}")
        return args
    args.extend(f"--transition-prompt={t.get('prompt', '')}" for t in segments)
    if any(t.get("duration") is not None for t in segments):
        args.extend(f"--transition-duration={t['duration']}" for t in segments)
    return args


def build_submit_args(clip: VideoClip) -> list[str]:
    """按 operation 组装 dreamina 生成子命令参数（与 skill 侧本地实现逐字同形）。

    ``--poll=0`` 纯提交不等待：即梦排队常达数小时，服务端只拿 submit_id 就走，轮询交给
    poll 阶段。``--video_resolution=720p``：CLI 升级后该参数**必填且严格校验**，各档支持的
    分辨率并不一致（2.5 只有 480p/720p、2.0_vip 到 4k、其余只有 720p），**720p 是唯一对全家族
    都合法的一档**，故继续统一传它。``image2video`` **一律不带 --ratio**（画幅由输入图推断，
    CLI 不收该参数）；``frames2video`` 同理——CLI help 原文 "ratio is inferred from the first
    frame image size"，显式传会被严格校验拒收。``multiframe2video`` 的参数形态与这四种
    差得远（不带 --model_version、图列表是逗号串、时长逐段给），单独在 ``_multiframe_args``。
    """
    paths = ref_paths(clip)
    if clip.operation == "multiframe2video":
        # 这条 operation 与 common 一个参数都不共用（模型不可配、逐段时长、逗号串图列表）
        return _multiframe_args(clip, paths)
    common = [
        f"--prompt={clip.prompt}",
        f"--duration={clip.duration}",
        f"--model_version={clip.model}",
        "--video_resolution=720p",
        "--poll=0",
    ]
    if clip.operation == "image2video":
        return ["image2video", f"--image={paths[0]}", *common]
    if clip.operation == "frames2video":
        # image_paths_json 恒为 [首帧, 尾帧]（REST 层的组合闸保证两张都在），顺序不可换：
        # 换了就是让镜头倒着走，而且 ratio 也会按错的那张图推断。
        return ["frames2video", f"--first={paths[0]}", f"--last={paths[1]}", *common]
    if clip.operation == "multimodal2video":
        # --image / --video 都是 stringArray（CLI help：repeat for each local input
        # image/video path），每份素材一个 flag，**顺序即 prompt 里的 @图片N / @视频N**。
        # 条数闸在 REST 层按模型分档拦过（图 2.5≤30 / 2.0 家族≤9，视频 ≤10 / ≤3，
        # 总输入 ≤50 / ≤12）。图在前视频在后，与 CLI 自己的示例同序。
        args = ["multimodal2video", *(f"--image={p}" for p in paths),
                *(f"--video={p}" for p in ref_video_paths(clip)), *common]
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

    **排除 multiframe2video 行**：它们的 model 列是 ``MULTIFRAME_MODEL_PLACEHOLDER`` 占位符
    （模型由平台固定，我们不知道是哪档）。「近似」和「报一个我们明知不成立的档位」是两回事
    ——后者是污染，读的人会把占位符当成一个真实模型。
    """
    rows = (await session.execute(
        select(VideoClip.model)
        .where(VideoClip.status == "done")
        .where(VideoClip.operation != "multiframe2video")
        .distinct()
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


# ── 段帧提取（分段续接：上一段的尾帧当下一段的首帧参考）──────────────────────
# 帧 PNG 落在 clip 自己的工作目录里，**因此与 clip 同 TTL**（reaper 删的是整个目录，
# 见 reap_clips_once）——不另立生命周期，也就没有「视频没了帧还在」的孤儿。
FRAME_LAST = "last"
_FRAME_TIMEOUT = 60.0
# t=last 的取法：从**末尾**回退 1s 开始解一帧（-sseof）。不用 ffprobe 得到的 duration 减
# epsilon：容器时长与最后一个可解帧的 PTS 常有毫秒级出入，按 duration 去 seek 经常落在
# 末帧之后抽不出图；-sseof 是 ffmpeg 自己算的尾部窗口，短于 1s 的片也会被它夹到 0。
_FRAME_TAIL_SEEK = "-1"


def frame_name(t: str | float) -> str:
    """帧文件名：``frame_last.png`` / ``frame_3.000.png``。

    定长三位小数不是洁癖：免鉴权直链的文件名走白名单正则，形态固定才能既放行帧图又不放
    宽到能撞别的东西；同时它也是**幂等键**——同一个 t 必然映射到同一个文件名，重复请求
    直接复用磁盘上那张，不再跑第二次 ffmpeg。
    """
    if t == FRAME_LAST:
        return "frame_last.png"
    return f"frame_{float(t):.3f}.png"


async def probe_duration(video: Path) -> float | None:
    """ffprobe 取视频时长（秒）；探不出返回 None。

    只用来判 ``t`` 越界并把真实时长写进错误文案。探不出时**不拦**（回 None 让调用方放行给
    ffmpeg），把「探测器不给力」变成拒绝服务是本末倒置。
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            "ffprobe", "-v", "error", "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1", str(video),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
    except OSError:
        return None
    try:
        out, _err = await asyncio.wait_for(proc.communicate(), timeout=_FRAME_TIMEOUT)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return None
    try:
        return float((out or b"").decode("utf-8", "replace").strip())
    except ValueError:
        return None


async def extract_frame(video: Path, out: Path, t: str | float) -> str | None:
    """抽一帧到 ``out``（PNG）。成功返回 None，失败返回中文错误说明。

    **幂等**：``out`` 已存在且非空即直接复用，不再调 ffmpeg——同一个 t 反复请求是分段续接
    的常态（重试、换个 agent 再取一次），每次重抽既慢又白占 CPU。
    """
    if out.is_file() and out.stat().st_size > 0:
        return None
    if t == FRAME_LAST:
        # 尾帧的取法：seek 到末尾前 1s，然后**解完这段里的每一帧、逐帧覆盖同一个输出**
        # （-update 1 且**不能加 -frames:v 1**）——最后写进去的那张就是真正的末帧。
        # 加了 -frames:v 1 只会拿到「末尾前 1s 处的那一帧」，看着也是张图，实测出来的正是
        # 前一秒的画面：分段续接靠它接第二段，接缝处会跳掉一秒。
        seek, limit = ["-sseof", _FRAME_TAIL_SEEK], []
    else:
        seek, limit = ["-ss", f"{float(t):.3f}"], ["-frames:v", "1"]
    argv = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            *seek, "-i", str(video), *limit, "-update", "1", str(out)]
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    except OSError as exc:
        return f"ffmpeg 不可执行：{exc}"
    try:
        _out, err = await asyncio.wait_for(proc.communicate(), timeout=_FRAME_TIMEOUT)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return f"ffmpeg 抽帧超时({_FRAME_TIMEOUT}s)"
    if proc.returncode:
        return f"ffmpeg 抽帧失败(rc={proc.returncode})：" \
               f"{(err or b'').decode('utf-8', 'replace')[-400:]}"
    if not (out.is_file() and out.stat().st_size > 0):
        # rc=0 却没产物：t 落在最后一个可解帧之后是最常见的成因。**不回半张图**，
        # 空文件也当失败清掉——留着它会让下一次幂等复用直接命中一张 0 字节的 PNG。
        out.unlink(missing_ok=True)
        return "ffmpeg 未抽到帧（t 可能落在视频末帧之后）"
    return None


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
