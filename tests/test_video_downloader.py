"""下载器测试——yt-dlp 子进程 mock，不打真网（平移自 test_downloader.py 的 TestDownloader）。

TestPaths 部分不迁移：paths 已由 M1 迁入并有 tests/test_video_paths.py 覆盖。
"""
import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from app.video.pipeline.downloader import (
    DownloadError,
    DurationExceededError,
    _build_download_argv,
    download,
    probe_metadata,
)


def _mock_proc(stdout=b"", returncode=0):
    proc = AsyncMock()
    proc.communicate = AsyncMock(return_value=(stdout, b""))
    proc.returncode = returncode
    return proc


class TestDownloader:
    async def test_probe_metadata_parses_json(self):
        info = {"id": "abc123", "title": "Attachment Theory", "duration": 600}
        with patch("asyncio.create_subprocess_exec",
                   return_value=_mock_proc(json.dumps(info).encode())):
            meta = await probe_metadata("https://www.youtube.com/watch?v=abc123")
        assert meta["duration"] == 600

    async def test_download_rejects_overlong(self, tmp_path):
        # duration 超 VIDEO_TRANSPORT_MAX_DURATION_SECONDS(7200) → DurationExceededError
        info = {"id": "x", "title": "t", "duration": 99999}
        with patch("app.video.pipeline.downloader.probe_metadata",
                   return_value=info):
            with pytest.raises(DurationExceededError):
                await download("https://y/watch?v=x", tmp_path)

    def test_build_argv_prefers_manual_subs(self):
        argv = _build_download_argv("https://u", Path("/w"), 1080, auto_subs=False)
        assert "--write-subs" in argv and "--write-auto-subs" not in argv
        argv2 = _build_download_argv("https://u", Path("/w"), 1080, auto_subs=True)
        assert "--write-auto-subs" in argv2

    async def test_probe_metadata_rejects_flag_url(self):
        # 以 - 开头的伪 URL 会被 yt-dlp 当成选项（argv 走私）——入口校验须直接拒绝，
        # 且绝不能启动子进程
        with patch("asyncio.create_subprocess_exec") as spawn:
            with pytest.raises(DownloadError):
                await probe_metadata("-o/tmp/pwn")
        spawn.assert_not_called()

    def test_build_argv_puts_url_after_end_of_options(self):
        # url 前一位必须是 -- （end-of-options），纵深防御 argv 走私
        argv = _build_download_argv("https://u", Path("/w"), 1080, auto_subs=False)
        assert argv[-1] == "https://u"
        assert argv[-2] == "--"
