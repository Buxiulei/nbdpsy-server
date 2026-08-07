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

import os
import random
from datetime import datetime
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
