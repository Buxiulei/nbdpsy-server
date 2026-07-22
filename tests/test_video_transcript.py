"""VTT 解析 + ASR 兜底选择逻辑 + 字幕空窗 ASR 补漏（平移自 test_transcript.py）。

mock 面随 import 面更换：源 ``get_asr(...).transcribe(url, language="en") -> R(.segments)`` →
薄 provider ``asr_transcribe(url) -> [{start,end,text}]``（直接返回列表，无 .segments 包装、无
language 参数），故 mock 直接返回列表、side_effect 签名去掉 language。
"""
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from app.video.pipeline.transcript import (
    detect_gaps,
    ensure_transcript,
    fill_gaps_with_asr,
    parse_vtt,
)

_TRANSCRIPT = "app.video.pipeline.transcript"

VTT = """WEBVTT
Kind: captions

00:00:01.000 --> 00:00:03.500
attachment theory was developed

00:00:03.500 --> 00:00:06.000
attachment theory was developed
by John Bowlby

00:00:06.000 --> 00:00:08.000
in the 1950s
"""


class TestParseVtt:
    def test_parses_and_dedups_rollup(self, tmp_path):
        p = tmp_path / "v.en.vtt"
        p.write_text(VTT, encoding="utf-8")
        segs = parse_vtt(p)
        # 滚动重复行去重：第二块只保留新增行 "by John Bowlby"
        texts = [s["text"] for s in segs]
        assert texts == ["attachment theory was developed", "by John Bowlby", "in the 1950s"]
        assert segs[0]["start"] == 1.0 and segs[0]["end"] == 3.5

    def test_strips_inline_tags(self, tmp_path):
        p = tmp_path / "t.vtt"
        p.write_text("WEBVTT\n\n00:00:00.000 --> 00:00:01.000\n<c>hello</c> <00:00:00.500>world\n",
                     encoding="utf-8")
        assert parse_vtt(p)[0]["text"] == "hello world"


class TestEnsureTranscript:
    async def test_uses_subtitle_when_present(self, tmp_path):
        p = tmp_path / "v.en.vtt"
        p.write_text(VTT, encoding="utf-8")
        result = await ensure_transcript(1, {
            "subtitle_path": str(p), "subtitle_source": "manual",
            "video_path": str(tmp_path / "v.mp4")})
        assert result["source"] == "manual"
        assert len(result["segments"]) == 3

    async def test_fill_gaps_false_skips_asr(self, tmp_path):
        # fill_gaps=False（transport 带字幕路径）→ 纯字幕零 ASR，即便存在 >60s 空窗也不补漏
        vtt = ("WEBVTT\n\n"
               "00:00:01.000 --> 00:00:03.000\nintro\n\n"
               "00:02:00.000 --> 00:02:03.000\nafter long gap\n")   # 3→120s 空窗 >60
        p = tmp_path / "v.en.vtt"
        p.write_text(vtt, encoding="utf-8")
        with patch(f"{_TRANSCRIPT}.asr_transcribe") as m_asr:
            result = await ensure_transcript(
                1, {"subtitle_path": str(p), "subtitle_source": "manual",
                    "video_path": str(tmp_path / "v.mp4")}, fill_gaps=False)
        m_asr.assert_not_called()
        assert result["source"] == "manual"
        assert [s["text"] for s in result["segments"]] == ["intro", "after long gap"]
        # 纯字幕路径不带空窗补漏统计键
        assert "gap_asr_segments" not in result and "warnings" not in result

    async def test_falls_back_to_asr(self, tmp_path):
        fake_asr = AsyncMock(return_value=[{"start": 0.0, "end": 2.0, "text": "hi"}])
        with patch(f"{_TRANSCRIPT}.asr_transcribe", fake_asr), \
             patch(f"{_TRANSCRIPT}.extract_audio",
                   AsyncMock(return_value=tmp_path / "a.m4a")), \
             patch(f"{_TRANSCRIPT}.paths") as m_paths:
            m_paths.raw_dir.return_value = tmp_path
            m_paths.to_absolute_url.return_value = "https://xhs.nbdpsy.com/uploads/a.m4a"
            result = await ensure_transcript(1, {
                "subtitle_path": None, "subtitle_source": None,
                "video_path": str(tmp_path / "v.mp4")})
        assert result["source"] == "asr"
        fake_asr.assert_awaited_once()


class TestDetectGaps:
    def test_threshold_is_strict_greater_than(self):
        cues = [{"start": 0.0, "end": 10.0, "text": "a"},
                {"start": 70.0, "end": 80.0, "text": "b"}]   # 间隔正好 60.0
        assert detect_gaps(cues, 80.0) == []
        cues2 = [{"start": 0.0, "end": 10.0, "text": "a"},
                 {"start": 70.1, "end": 80.0, "text": "b"}]
        assert detect_gaps(cues2, 80.0) == [(10.0, 70.1)]

    def test_head_and_tail_detected(self):
        cues = [{"start": 70.0, "end": 80.0, "text": "mid"}]
        gaps = detect_gaps(cues, 200.0)
        assert (0.0, 70.0) in gaps
        assert (80.0, 200.0) in gaps
        assert len(gaps) == 2

    def test_head_tail_below_threshold_ignored(self):
        cues = [{"start": 30.0, "end": 80.0, "text": "mid"}]  # 头 30<60 尾 40<60
        assert detect_gaps(cues, 120.0) == []

    def test_empty_cues_whole_video_single_gap(self):
        assert detect_gaps([], 500.0) == [(0.0, 500.0)]


class TestFillGapsWithAsr:
    async def test_no_gaps_short_circuits(self, tmp_path):
        cues = [{"start": 1.0, "end": 2.0, "text": "x"}]
        merged, warnings = await fill_gaps_with_asr(1, cues, [], tmp_path / "v.mp4")
        assert merged == cues and warnings == []
        assert not (tmp_path / "asr_gaps").exists()

    async def test_segments_shifted_to_global_axis(self, tmp_path):
        # 空窗 380→500，ASR 段内相对 5-12s → 平移回全片轴 385-392
        gaps = [(380.0, 500.0)]
        fake = AsyncMock(return_value=[{"start": 5.0, "end": 12.0, "text": "Now close your eyes"}])
        with patch(f"{_TRANSCRIPT}.asr_transcribe", fake), \
             patch(f"{_TRANSCRIPT}._run_ffmpeg", AsyncMock()), \
             patch(f"{_TRANSCRIPT}.paths.raw_dir", return_value=tmp_path), \
             patch(f"{_TRANSCRIPT}.paths.to_absolute_url",
                   side_effect=lambda p: f"https://x/{Path(p).name}"):
            merged, warnings = await fill_gaps_with_asr(2, [], gaps, tmp_path / "v.mp4")
        assert merged == [{"start": 385.0, "end": 392.0, "text": "Now close your eyes"}]
        assert warnings == []
        assert not (tmp_path / "asr_gaps").exists()

    async def test_orchestration_one_success_one_fail(self, tmp_path):
        # 两个空窗：第一个成功并入、第二个 ASR 抛错 → 跳过 + 一条 warning + 目录清理
        cues = [{"start": 100.0, "end": 110.0, "text": "sub"}]
        gaps = [(0.0, 90.0), (200.0, 290.0)]

        def _side(url):
            if "gap1" in url:                      # 第二个空窗恒失败（含段级重试）
                raise RuntimeError("SERVER_ERROR")
            return [{"start": 5.0, "end": 8.0, "text": "close your eyes"}]

        fake = AsyncMock(side_effect=_side)
        m_ff = AsyncMock()
        with patch(f"{_TRANSCRIPT}.asr_transcribe", fake), \
             patch(f"{_TRANSCRIPT}._run_ffmpeg", m_ff), \
             patch(f"{_TRANSCRIPT}.asyncio.sleep", AsyncMock()), \
             patch(f"{_TRANSCRIPT}.logger") as m_log, \
             patch(f"{_TRANSCRIPT}.paths.raw_dir", return_value=tmp_path), \
             patch(f"{_TRANSCRIPT}.paths.to_absolute_url",
                   side_effect=lambda p: f"https://x/{Path(p).name}"):
            merged, warnings = await fill_gaps_with_asr(3, cues, gaps, tmp_path / "v.mp4")
        recovered = [s for s in merged if s["text"] == "close your eyes"]
        assert recovered and recovered[0]["start"] == 5.0 and recovered[0]["end"] == 8.0
        assert merged[0]["start"] == 5.0 and merged[-1]["start"] == 100.0
        assert len(warnings) == 1 and "200" in warnings[0]
        assert m_log.warning.called
        assert m_ff.await_count >= 1
        assert not (tmp_path / "asr_gaps").exists()

    async def test_asr_segment_overlapping_cue_discarded(self, tmp_path):
        # 字幕优先：与既有 cue(90-100) 重叠 >50% 的 ASR 段(92-98=100%)丢弃，无重叠段保留
        cues = [{"start": 90.0, "end": 100.0, "text": "sub"}]
        gaps = [(0.0, 89.0)]
        fake = AsyncMock(return_value=[
            {"start": 10.0, "end": 20.0, "text": "keep"},
            {"start": 92.0, "end": 98.0, "text": "drop"}])
        with patch(f"{_TRANSCRIPT}.asr_transcribe", fake), \
             patch(f"{_TRANSCRIPT}._run_ffmpeg", AsyncMock()), \
             patch(f"{_TRANSCRIPT}.paths.raw_dir", return_value=tmp_path), \
             patch(f"{_TRANSCRIPT}.paths.to_absolute_url",
                   side_effect=lambda p: f"https://x/{Path(p).name}"):
            merged, warnings = await fill_gaps_with_asr(4, cues, gaps, tmp_path / "v.mp4")
        texts = [s["text"] for s in merged]
        assert "keep" in texts and "drop" not in texts

    async def test_all_gaps_fail_raises_and_cleans_dir(self, tmp_path):
        gaps = [(0.0, 50.0), (100.0, 160.0)]
        fake = AsyncMock(side_effect=RuntimeError("boom"))
        with patch(f"{_TRANSCRIPT}.asr_transcribe", fake), \
             patch(f"{_TRANSCRIPT}._run_ffmpeg", AsyncMock()), \
             patch(f"{_TRANSCRIPT}.asyncio.sleep", AsyncMock()), \
             patch(f"{_TRANSCRIPT}.paths.raw_dir", return_value=tmp_path), \
             patch(f"{_TRANSCRIPT}.paths.to_absolute_url",
                   side_effect=lambda p: f"https://x/{Path(p).name}"):
            with pytest.raises(RuntimeError):
                await fill_gaps_with_asr(5, [], gaps, tmp_path / "v.mp4")
        assert not (tmp_path / "asr_gaps").exists()

    async def test_long_gap_split_into_overlapping_chunks(self, tmp_path):
        # 700s 长空窗 → 3 段(≤300s)，段间重叠 2s：-ss 起点 0 / 298 / 596
        gaps = [(0.0, 700.0)]
        fake = AsyncMock(return_value=[])
        m_ff = AsyncMock()
        with patch(f"{_TRANSCRIPT}.asr_transcribe", fake), \
             patch(f"{_TRANSCRIPT}._run_ffmpeg", m_ff), \
             patch(f"{_TRANSCRIPT}.paths.raw_dir", return_value=tmp_path), \
             patch(f"{_TRANSCRIPT}.paths.to_absolute_url",
                   side_effect=lambda p: f"https://x/{Path(p).name}"):
            await fill_gaps_with_asr(6, [], gaps, tmp_path / "v.mp4")
        assert m_ff.await_count == 3
        assert fake.await_count == 3
        ss_values = [float(c.args[0][c.args[0].index("-ss") + 1])
                     for c in m_ff.await_args_list]
        assert ss_values == [0.0, 298.0, 596.0]

    async def test_gap_retry_recovers_after_transient_failures(self, tmp_path):
        # 段级重试：前两次 ASR 抛（DashScope 抖动）、第三次成功 → gap 恢复、无 warning
        gaps = [(380.0, 500.0)]
        fake = AsyncMock(side_effect=[
            RuntimeError("blip-1"),
            RuntimeError("blip-2"),
            [{"start": 5.0, "end": 12.0, "text": "Now close your eyes"}],
        ])
        with patch(f"{_TRANSCRIPT}.asr_transcribe", fake), \
             patch(f"{_TRANSCRIPT}._run_ffmpeg", AsyncMock()), \
             patch(f"{_TRANSCRIPT}.asyncio.sleep", AsyncMock()) as m_sleep, \
             patch(f"{_TRANSCRIPT}.paths.raw_dir", return_value=tmp_path), \
             patch(f"{_TRANSCRIPT}.paths.to_absolute_url",
                   side_effect=lambda p: f"https://x/{Path(p).name}"):
            merged, warnings = await fill_gaps_with_asr(7, [], gaps, tmp_path / "v.mp4")
        assert merged == [{"start": 385.0, "end": 392.0, "text": "Now close your eyes"}]
        assert warnings == []
        assert fake.await_count == 3           # 恰 3 次尝试（第 3 次成功即返回）
        assert m_sleep.await_count == 2        # 指数退避 sleep 两次（2s、6s）

    async def test_gap_retry_exhausted_skips_with_diagnostic(self, tmp_path):
        # 段级重试耗尽（某 gap 持续抛）→ 该 gap 跳过 + 一条含异常类型名的 warning；另一 gap 成功不 raise。
        # 直接 spy 模块 logger（而非 caplog）：宿主 loguru 与 stdlib 混用下 caplog 全套跑会漏抓，
        # 直接断言 logger.warning 被调且诊断含异常类型名更确定（源同款 patch(logger) 手法）。
        cues = [{"start": 100.0, "end": 110.0, "text": "sub"}]
        gaps = [(0.0, 90.0), (200.0, 290.0)]

        def _side(url):
            if "gap1" in url:                    # 第二个空窗恒失败（重试也失败）
                raise RuntimeError("SERVER_ERROR")
            return [{"start": 5.0, "end": 8.0, "text": "recovered"}]

        fake = AsyncMock(side_effect=_side)
        with patch(f"{_TRANSCRIPT}.asr_transcribe", fake), \
             patch(f"{_TRANSCRIPT}._run_ffmpeg", AsyncMock()), \
             patch(f"{_TRANSCRIPT}.asyncio.sleep", AsyncMock()), \
             patch(f"{_TRANSCRIPT}.logger") as m_log, \
             patch(f"{_TRANSCRIPT}.paths.raw_dir", return_value=tmp_path), \
             patch(f"{_TRANSCRIPT}.paths.to_absolute_url",
                   side_effect=lambda p: f"https://x/{Path(p).name}"):
            merged, warnings = await fill_gaps_with_asr(
                8, cues, gaps, tmp_path / "v.mp4")
        assert [s["text"] for s in merged] == ["recovered", "sub"]
        assert len(warnings) == 1 and "RuntimeError" in warnings[0]
        # 日志 warning 非空且含异常类型名（诊断非空，root-cause 可查）
        warn_args = [str(c.args) for c in m_log.warning.call_args_list]
        assert warn_args and any("RuntimeError" in m for m in warn_args)
        gap2_calls = [c for c in fake.await_args_list if "gap1" in c.args[0]]
        assert len(gap2_calls) == 4            # 1 次原调用 + 3 次重试后放弃
