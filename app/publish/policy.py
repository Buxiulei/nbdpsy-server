"""发布终态/重试与冷却判定的纯函数层(API/worker 拆分共用)。

从 ``app/publish/scheduler.py`` 的 ``finish``/``account_cooldown_gate`` 语义**逐字节对照**
提炼:scheduler(进程内调度)与 ``app/account_worker``(账号子进程)两侧共用同一套裁决,
杜绝"两处各写一份终态规则 → 语义漂移"。本模块只做纯计算,不碰 DB、不碰浏览器。

对照源(scheduler.finish):
- success → published(note_id/note_url 由调用方回填,error 清空)。
- need_manual_login(I1)→ 直接 failed,不排重试、retries 不递增(重试只会反复 SSO 失败)。
- account_restricted(F3)→ 直接 failed,不排重试、retries 不递增(重发是更强封号信号)。
- 普通失败且 ``retries < len(retry_delays)`` → 回 pending,按 ``retry_delays[retries]``
  乘 ``uniform(0.8, 1.5)`` 抖动排下次重试,retries 递增(去固定退避节律指纹)。
- 重试耗尽 → failed。
"""

import json
import os
import random
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional, Sequence

# 与 scheduler.finish 的缺省文案逐字一致(I1 / F3)
NEED_MANUAL_LOGIN_ERROR = "需要人工登录/重新扫码"
ACCOUNT_RESTRICTED_ERROR = "账号被小红书限制发布(违规/处罚态)"


def decide_finish(
    success: bool,
    need_manual_login: bool,
    account_restricted: bool,
    error: Optional[str],
    retries: int,
    retry_delays: Sequence[int],
    *,
    jitter: Callable[[float, float], float] = random.uniform,
) -> dict:
    """裁决一次发布结果应落的终态(纯函数,语义 = scheduler.finish)。

    返回 dict:
    - ``status``:``published`` | ``pending``(排重试)| ``failed``
    - ``error``:落库的 error 文案(published 恒为 None)
    - ``next_retry_delta_s``:下次重试距今秒数(仅排重试时非 None,已含 0.8~1.5 抖动)
    - ``retries_increment``:是否递增 retries(仅排重试时 True)

    ``jitter`` 可注入(测试给确定值);默认 ``random.uniform`` 与 scheduler 抖动一致。
    """
    if success:
        return {
            "status": "published",
            "error": None,
            "next_retry_delta_s": None,
            "retries_increment": False,
        }
    if need_manual_login:
        # I1:cookie/SSO 坏,重试无用 → 立即终态,不递增 retries
        return {
            "status": "failed",
            "error": error or NEED_MANUAL_LOGIN_ERROR,
            "next_retry_delta_s": None,
            "retries_increment": False,
        }
    if account_restricted:
        # F3:违规/处罚禁发,重发有害 → 立即终态,不递增 retries
        return {
            "status": "failed",
            "error": error or ACCOUNT_RESTRICTED_ERROR,
            "next_retry_delta_s": None,
            "retries_increment": False,
        }
    if retries < len(retry_delays):
        # 还有重试额度:按当前 retries 取延迟乘抖动,回 pending
        delay = retry_delays[retries] * jitter(0.8, 1.5)
        return {
            "status": "pending",
            "error": error,
            "next_retry_delta_s": delay,
            "retries_increment": True,
        }
    # 重试耗尽:终态 failed
    return {
        "status": "failed",
        "error": error,
        "next_retry_delta_s": None,
        "retries_increment": False,
    }


def cooldown_remaining_s(
    last_started_at: Optional[datetime],
    now: datetime,
    interval_s: float,
) -> float:
    """账号发布冷却剩余秒数(纯函数,数学 = scheduler.account_cooldown_gate)。

    ``last_started_at`` 为该账号最近一条 published/publishing job 的 started_at
    (naive UTC,调用方自查自排除当前 job);``interval_s`` 由调用方现抽
    ``uniform(PUBLISH_MIN_INTERVAL_MIN, PUBLISH_MIN_INTERVAL_MAX)``。
    返回值 ``<= 0`` 表示冷却已满足可发;``> 0`` 为还需等待的秒数。
    无历史发布(None)视作满足,返回 0。
    """
    if last_started_at is None:
        return 0.0
    elapsed = (now - last_started_at).total_seconds()
    return interval_s - elapsed


def daily_cap_reached(published_today: int, daily_cap: int) -> bool:
    """每账号每自然日发布上限判定(纯函数):当日已发布数达到上限即 True。"""
    return published_today >= daily_cap


# ── 视频笔记:平台接受的视频扩展名 ──
# 逐字来自真号采集(data/scene_captures/video_publish/account9_video_publish_probe.json)
# 里视频上传 input 的 accept 属性 —— 平台给的是**扩展名列表**而不是 MIME,所以判据也只能
# 按扩展名走(靠 `accept*='video'` 之类的猜测在这个页面上一个都匹配不到)。
XHS_VIDEO_EXTENSIONS = (
    ".mp4", ".mov", ".flv", ".f4v", ".mkv", ".rm", ".rmvb",
    ".m4v", ".mpg", ".mpeg", ".ts",
)


def video_ext_allowed(video_path: str) -> bool:
    """视频路径的扩展名是否在平台白名单内(纯函数,大小写不敏感)。"""
    lowered = (video_path or "").lower()
    return any(lowered.endswith(ext) for ext in XHS_VIDEO_EXTENSIONS)


# ── 视频笔记封面:平台接受的图片扩展名 ──
# 封面走图片而非视频那套白名单;与既有图片上传服务(upload_service._FORMAT_EXT)
# 放行的格式一致,多带一个 .jpeg 写法。
XHS_COVER_EXTENSIONS = (".jpg", ".jpeg", ".png", ".webp")


def cover_ext_allowed(cover_path: str) -> bool:
    """封面路径的扩展名是否在白名单内(纯函数,大小写不敏感)。"""
    lowered = (cover_path or "").lower()
    return any(lowered.endswith(ext) for ext in XHS_COVER_EXTENSIONS)


# ── 大媒体:等待/硬超时按文件体积伸缩 ──
# 用户会传 15-30 分钟的 GB 级视频,上传到小红书 + 平台转码可能到 10-30 分钟级。
# 固定超时在这个量级上必错:给小了大文件永远发不出去,给大了一条坏视频占死进程几小时。
# **一处定义两处用**:step3v 等上传转码、supervisor 的账号子进程硬超时,共用本公式
# (语义都是"这么大的文件合理要多久"),避免两边各写一份然后漂移。


def media_timeout_s(
    size_bytes: int | None, *, base_s: int, per_100mb_s: int, cap_s: int
) -> int:
    """按媒体文件体积算超时秒数:``base + 大小/100MB * per_100mb``,封顶 ``cap``。

    - 大小未知(``None``)或非正数 → 退回 ``base``:**不假装知道**,给个保守基数。
    - 结果**永不低于 base**:``cap`` 配得比 ``base`` 还小(配置写错)时也不许把超时
      塌缩成比基数还短,那会让所有任务无差别地被砍在半路。
    """
    if not size_bytes or size_bytes <= 0:
        return base_s
    scaled = base_s + int(size_bytes / (100 * 1024 * 1024) * per_100mb_s)
    return max(base_s, min(scaled, cap_s))


def media_file_size(path: str | None) -> int | None:
    """读文件字节数;路径为空/读不到一律 None(交给 ``media_timeout_s`` 退回基数)。"""
    if not path:
        return None
    try:
        return os.path.getsize(path)
    except OSError:
        return None


# ── 播客音频:平台规格(实拍确认,见 data/scene_captures/podcast/)──
# 上传区规格文案逐字:「时长不超过10分钟,最长不超过2小时,大小不超过1GB」
# (前半句是产品文案笔误,实际语义 = 最短 10 分钟)。
XHS_AUDIO_EXTENSIONS = (".m4a", ".mp3", ".wav", ".flac", ".aac")
XHS_AUDIO_MAX_BYTES = 1024 * 1024 * 1024
# 时长门取**闭区间**(=600s / =7200s 放行):平台真实边界语义未取证,先取宽松侧
# 避免误杀合法输入 —— 真号验出平台拒收整点值时改这两个常量即可。
AUDIO_MIN_DURATION_S = 600
AUDIO_MAX_DURATION_S = 7200


def audio_ext_allowed(audio_path: str | None) -> bool:
    """音频路径的扩展名是否在平台白名单内(纯函数,大小写不敏感)。"""
    lowered = (audio_path or "").lower()
    return any(lowered.endswith(ext) for ext in XHS_AUDIO_EXTENSIONS)


def audio_duration_s(audio_path: str | None) -> float | None:
    """用 ffprobe 读音频时长(秒);读不出来一律 ``None``,**不假装知道**。

    为什么用 ffprobe:worker 主机已有 ffmpeg 栈(视频管线在用),零新依赖;它只读
    容器头,毫秒级返回,放在同步 REST 端点里可接受。

    ``None`` 的调用方语义是**拒收**而不是放行(见 ``audio_reject``):平台侧超长会拒,
    而我们要等发布那一刻才知道——宁可在入口误杀一个读不出时长的坏文件。
    """
    if not audio_path:
        return None
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "json", audio_path],
            capture_output=True, text=True, timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        # ffprobe 不可执行 / 超时:读不到就是读不到
        return None
    if out.returncode != 0:
        return None
    try:
        raw = json.loads(out.stdout or "{}").get("format", {}).get("duration")
        value = float(raw)
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    return value if value > 0 else None


def audio_reject(audio_path: str) -> str | None:
    """播客音频的四条准入(存在性 / 扩展名 / 体积 / 时长);合格返 ``None``,否则给拒绝理由。

    返回理由字符串而不是裸 bool:这四条的补救动作完全不同(换文件 / 转格式 / 压缩 /
    剪辑),只说一句"不合格"等于让调用方去猜。

    顺序是有意的 —— 扩展名先于 ffprobe:对一个必拒的文件白跑一次子进程没有意义。
    """
    if not audio_path or not Path(audio_path).is_file():
        return f"audio 文件不存在(需为本服务器可读的绝对路径):{audio_path}"
    if not audio_ext_allowed(audio_path):
        return (
            f"audio 格式不支持:{audio_path};小红书播客只接受 "
            f"{'/'.join(XHS_AUDIO_EXTENSIONS)}"
        )
    size = media_file_size(audio_path)
    if size is not None and size > XHS_AUDIO_MAX_BYTES:
        return (
            f"audio 大小 {size} 字节超过平台上限 {XHS_AUDIO_MAX_BYTES} 字节(1GB)"
        )
    duration = audio_duration_s(audio_path)
    if duration is None:
        return (
            f"无法读取 audio 时长(ffprobe 读不出来,多半不是有效音频文件):{audio_path};"
            f"平台要求 {AUDIO_MIN_DURATION_S // 60}-{AUDIO_MAX_DURATION_S // 60} 分钟,"
            f"读不出来一律不放行"
        )
    if duration < AUDIO_MIN_DURATION_S or duration > AUDIO_MAX_DURATION_S:
        return (
            f"audio 时长 {duration:.0f}s 越界:平台要求 {AUDIO_MIN_DURATION_S}-"
            f"{AUDIO_MAX_DURATION_S}s(最短 10 分钟、最长 2 小时,闭区间)"
        )
    return None


# ── 两档封面体积上限 ──
# 扩展名白名单三处**同一套**(XHS_COVER_EXTENSIONS,含 webp):真号取证读到播客合集
# 封面 input 的 accept 就是 ".jpg,.jpeg,.png,.webp",与设计文档「合集封面无 webp」的
# 假设相反,以实测 DOM 为准。差异只在体积上限,故只分两个常量、不分两套白名单。
AUDIO_COVER_MAX_BYTES = 32 * 1024 * 1024
PODCAST_COLLECTION_COVER_MAX_BYTES = 5 * 1024 * 1024


def _cover_reject(cover_path: str, max_bytes: int, label: str) -> str | None:
    """封面准入共用体:存在性 + 扩展名 + 体积;合格返 ``None``,否则给理由。"""
    if not cover_path or not Path(cover_path).is_file():
        return f"{label} 文件不存在(需为本服务器可读的绝对路径):{cover_path}"
    if not cover_ext_allowed(cover_path):
        return (
            f"{label} 格式不支持:{cover_path};只接受 "
            f"{'/'.join(XHS_COVER_EXTENSIONS)}"
        )
    size = media_file_size(cover_path)
    if size is not None and size > max_bytes:
        return f"{label} 大小 {size} 字节超过上限 {max_bytes} 字节"
    return None


def audio_cover_reject(cover_path: str) -> str | None:
    """播客音频封面准入(扩展名 + ≤32MB);合格返 ``None``,否则给拒绝理由。"""
    return _cover_reject(cover_path, AUDIO_COVER_MAX_BYTES, "cover 音频封面")


def podcast_collection_cover_reject(cover_path: str) -> str | None:
    """播客合集封面准入(扩展名 + ≤5MB);合格返 ``None``,否则给拒绝理由。

    与音频封面**刻意不合并成一个函数**:两者体积上限不同(5MB vs 32MB),合并后
    只能靠调用方传参区分,而传错参数的失败是静默的(放行一张平台会拒的图)。
    """
    return _cover_reject(
        cover_path, PODCAST_COLLECTION_COVER_MAX_BYTES, "cover 合集封面"
    )
