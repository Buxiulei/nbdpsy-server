"""yt-dlp 封装。venv 内 python -m yt_dlp 调用，流式落盘。

字幕策略：第一遍只要人工字幕（--write-subs）；没有再补 --write-auto-subs 一遍
（yt-dlp 两类字幕同名覆盖，分两遍才能区分来源）。

平移自 video_transport/downloader.py：无 AI 收口，仅 config 走宿主 settings
（VIDEO_TRANSPORT_MAX_DURATION_SECONDS 时长闸门）；ffmpeg/yt-dlp 子进程本就 create_subprocess_exec。
"""
import asyncio
import json
import sys
import time
from pathlib import Path

from app.core.config import settings


class DownloadError(Exception):
    pass


class DurationExceededError(DownloadError):
    pass


_YTDLP = [sys.executable, "-m", "yt_dlp", "--no-playlist", "--no-progress"]


async def _run(argv: list[str], *, timeout: float) -> bytes:
    proc = await asyncio.create_subprocess_exec(
        *argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()  # 收尸，避免留僵尸子进程
        raise DownloadError(f"yt-dlp 超时({timeout}s)")
    if proc.returncode != 0:
        raise DownloadError(f"yt-dlp 失败: {stderr.decode(errors='replace')[-500:]}")
    return stdout


def _validate_url(url: str) -> None:
    """入口校验：只放行 http(s) URL。挡住以 - 开头的伪 URL 被 yt-dlp 当选项解析
    （argv 旗标走私，与 shell 无关）。"""
    if not url.startswith(("https://", "http://")):
        raise DownloadError(f"非法 URL: {url[:100]}")


async def probe_metadata(url: str) -> dict:
    _validate_url(url)
    # url 前加 -- end-of-options 分隔符，纵深防御 argv 走私
    stdout = await _run(_YTDLP + ["-J", "--", url], timeout=120)
    return json.loads(stdout)


def _build_download_argv(url: str, workdir: Path, max_resolution: int,
                         *, auto_subs: bool) -> list[str]:
    argv = _YTDLP + [
        "-f", f"bv*[height<={max_resolution}][ext=mp4]+ba[ext=m4a]/b[ext=mp4]/b",
        "--merge-output-format", "mp4",
        "-o", str(workdir / "video.%(ext)s"),
        "--write-info-json",
        "--sub-langs", "en.*,en",
        "--convert-subs", "vtt",
    ]
    argv.append("--write-auto-subs" if auto_subs else "--write-subs")
    argv.append("--")  # end-of-options，url 之后不再有选项，挡 argv 走私
    argv.append(url)
    return argv


def _find_subtitle(workdir: Path) -> Path | None:
    hits = sorted(workdir.glob("video*.vtt"))
    return hits[0] if hits else None


async def download(url: str, workdir: Path, *, max_resolution: int = 1080,
                   deadline: float | None = None) -> dict:
    _validate_url(url)
    info = await probe_metadata(url)
    duration = int(info.get("duration") or 0)
    limit = settings.VIDEO_TRANSPORT_MAX_DURATION_SECONDS
    if duration > limit:
        raise DurationExceededError(f"视频时长 {duration}s 超上限 {limit}s")

    def _remaining(default: float) -> float:
        if deadline is None:
            return default
        left = deadline - time.monotonic()
        if left <= 30:
            raise DownloadError("下载阶段预算耗尽")
        return min(default, left)

    await _run(_build_download_argv(url, workdir, max_resolution, auto_subs=False),
               timeout=_remaining(1500))
    subtitle = _find_subtitle(workdir)
    subtitle_source = "manual" if subtitle else None
    if subtitle is None:
        # 第二遍只拉自动字幕（视频已存在会跳过重下）
        await _run(_build_download_argv(url, workdir, max_resolution, auto_subs=True),
                   timeout=_remaining(600))
        subtitle = _find_subtitle(workdir)
        subtitle_source = "auto" if subtitle else None

    video = workdir / "video.mp4"
    if not video.exists():
        raise DownloadError("下载完成但 video.mp4 不存在")
    return {
        "video_path": str(video),
        "subtitle_path": str(subtitle) if subtitle else None,
        "subtitle_source": subtitle_source,
        "info": {"id": info.get("id"), "title": info.get("title"),
                 "duration": duration, "uploader": info.get("uploader"),
                 "license": info.get("license"), "webpage_url": info.get("webpage_url")},
    }
