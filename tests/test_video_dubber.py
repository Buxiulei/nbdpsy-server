"""统一语速同步算法：plan_timeline 纯函数 + 全局 rate 二分 + 两遍合成全片统一语速 +
adjusted_segments 时间轴 + build_track 底长扩展 + 幂等（平移自 test_dubber.py）。

mock 面随 import 面更换：源 ``get_tts(...).synthesize -> TtsResult(.duration_seconds)`` → 薄
provider ``tts_synthesize(text, voice=, out_path=, rate=) -> float``（直接返回时长秒）。
"""
import hashlib
import subprocess
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from app.video.pipeline.dubber import (
    _MAX_DRIFT,
    _decide_rate,
    _max_drift,
    build_track,
    plan_timeline,
    synthesize_all,
    synthesize_natural,
)

_NO_DRIFT_CAP = 999.0   # 关掉漂移约束，隔离测「总时长」维度的 rate 决策
_DUB = "app.video.pipeline.dubber"


def _zh_hash(zh: str) -> str:
    return hashlib.md5(zh.encode("utf-8")).hexdigest()[:8]


def _sine_wav(path: Path, duration: float):
    subprocess.run(["ffmpeg", "-y", "-f", "lavfi", "-i",
                    f"sine=frequency=440:duration={duration}",
                    "-ar", "24000", "-ac", "1", str(path)], capture_output=True, check=True)


class TestPlanTimeline:
    def test_short_clips_keep_original_axis_with_gaps(self):
        starts = [0.0, 5.0, 10.0]
        durations = [1.0, 1.0, 1.0]
        plan, cur_final = plan_timeline(starts, durations)
        assert [p["start"] for p in plan] == [0.0, 5.0, 10.0]
        assert [p["end"] for p in plan] == [1.0, 6.0, 11.0]
        assert cur_final == 11.0

    def test_long_clips_carry_over(self):
        starts = [0.0, 2.0, 4.0]
        durations = [3.0, 3.0, 3.0]
        plan, cur_final = plan_timeline(starts, durations, gap=0.0)
        assert [p["start"] for p in plan] == [0.0, 3.0, 6.0]
        assert cur_final == 9.0

    def test_carryover_then_recover_on_big_gap(self):
        starts = [0.0, 2.0, 20.0]
        durations = [3.0, 3.0, 3.0]
        plan, _ = plan_timeline(starts, durations, gap=0.0)
        assert [p["start"] for p in plan] == [0.0, 3.0, 20.0]

    def test_rate_divides_duration(self):
        starts = [0.0, 0.0, 0.0]
        durations = [4.0, 4.0, 4.0]
        _, cur_final = plan_timeline(starts, durations, rate=2.0, gap=0.0)
        assert cur_final == 6.0

    def test_no_overlap_invariant_when_clips_overflow(self):
        starts = [0.0, 1.0, 2.0]
        durations = [5.0, 5.0, 5.0]
        plan, _ = plan_timeline(starts, durations)   # 默认 gap=0.12
        for i in range(1, len(plan)):
            assert plan[i]["start"] >= plan[i - 1]["end"] - 1e-9
            assert plan[i]["start"] >= plan[i - 1]["end"] + 0.12 - 1e-9

    def test_breathing_gap_value(self):
        starts = [0.0, 0.5]
        durations = [3.0, 3.0]
        plan, _ = plan_timeline(starts, durations, gap=0.2)
        assert plan[1]["start"] - plan[0]["end"] == pytest.approx(0.2)

    def test_first_sentence_not_pushed_by_gap(self):
        plan, _ = plan_timeline([0.0], [2.0], gap=0.5)
        assert plan[0]["start"] == 0.0

    def test_empty(self):
        plan, cur_final = plan_timeline([], [])
        assert plan == [] and cur_final == 0.0


class TestDecideRate:
    def test_keep_1_when_within_limit(self):
        rate, warning = _decide_rate([0.0, 0.0, 0.0], [4.0, 4.0, 4.0], total_limit=100.0,
                                     max_rate=2.0, max_drift=_NO_DRIFT_CAP)
        assert rate == 1.0 and warning is None

    def test_binary_search_uniform_rate(self):
        rate, warning = _decide_rate([0.0, 0.0, 0.0], [4.0, 4.0, 4.0], total_limit=6.0,
                                     max_rate=3.0, gap=0.0, max_drift=_NO_DRIFT_CAP)
        assert rate == pytest.approx(2.0, abs=0.011)
        assert warning is None

    def test_gap_raises_required_rate(self):
        rate_no_gap, _ = _decide_rate([0.0, 0.0, 0.0], [4.0, 4.0, 4.0], total_limit=6.0,
                                      max_rate=3.0, gap=0.0, max_drift=_NO_DRIFT_CAP)
        rate_big_gap, _ = _decide_rate([0.0, 0.0, 0.0], [4.0, 4.0, 4.0], total_limit=6.0,
                                       max_rate=3.0, gap=0.5, max_drift=_NO_DRIFT_CAP)
        assert rate_big_gap > rate_no_gap

    def test_max_rate_still_overflows_records_warning(self):
        rate, warning = _decide_rate([0.0, 0.0], [100.0, 100.0], total_limit=10.0,
                                     max_rate=1.2, max_drift=_NO_DRIFT_CAP)
        assert rate == 1.2 and warning

    def test_never_below_1(self):
        rate, _ = _decide_rate([0.0], [1.0], total_limit=100.0, max_rate=2.0)
        assert rate >= 1.0


class TestDriftCap:
    def test_drift_cap_forces_higher_rate(self):
        starts = [0.0, 1.0, 2.0, 3.0, 4.0]
        durations = [3.0] * 5
        rate, warning = _decide_rate(starts, durations, total_limit=20.0, max_rate=3.0, gap=0.0)
        assert warning is None
        assert rate > 1.0
        plan, _ = plan_timeline(starts, durations, rate=rate, gap=0.0)
        assert _max_drift(plan, starts) <= _MAX_DRIFT + 1e-6

    def test_drift_cap_warning_when_max_rate_insufficient(self):
        starts = [0.0, 0.1, 0.2, 0.3, 0.4]
        durations = [10.0] * 5
        rate, warning = _decide_rate(starts, durations, total_limit=100.0, max_rate=1.2, gap=0.0)
        assert rate == 1.2 and warning

    def test_no_drift_when_sentences_sparse(self):
        rate, warning = _decide_rate([0.0, 10.0, 20.0], [2.0, 2.0, 2.0], total_limit=100.0,
                                     max_rate=3.0)
        assert rate == 1.0 and warning is None


def _fake_tts(calls: list, base: float):
    """mock TTS provider：记录 (text, rate)，产出时长 = base/rate 的 sine，返回 float 时长。"""
    async def fake_synth(text, *, voice, rate=1.0, out_path):
        calls.append((text, rate))
        dur = base / rate
        _sine_wav(Path(out_path), dur)
        return dur
    return AsyncMock(side_effect=fake_synth)


class TestSynthesizeAll:
    async def test_all_sentences_same_rate(self, tmp_path):
        translated = [{"start": 0.0, "end": 4.0, "en": "a", "zh": "甲"},
                      {"start": 0.0, "end": 4.0, "en": "b", "zh": "乙"},
                      {"start": 0.0, "end": 4.0, "en": "c", "zh": "丙"}]
        calls: list = []
        with patch(f"{_DUB}.tts_synthesize", _fake_tts(calls, base=4.0)), \
             patch(f"{_DUB}.paths") as m_paths:
            m_paths.tts_dir.return_value = tmp_path
            clips, adjusted = await synthesize_all(
                1, translated, voice="v", max_rate=3.0, video_duration=6.0)
        assert len({c["rate"] for c in clips}) == 1
        rate = clips[0]["rate"]
        assert rate > 1.005                       # 触发了第二遍
        distinct = {r for _, r in calls}
        assert distinct - {1.0} == {rate}
        second_pass = [t for t, r in calls if r == rate]
        assert set(second_pass) == {"甲", "乙", "丙"}

    async def test_no_second_pass_when_fits(self, tmp_path):
        translated = [{"start": 0.0, "end": 2.0, "en": "a", "zh": "你好"},
                      {"start": 5.0, "end": 7.0, "en": "b", "zh": "再见"}]
        calls: list = []
        with patch(f"{_DUB}.tts_synthesize", _fake_tts(calls, base=1.0)), \
             patch(f"{_DUB}.paths") as m_paths:
            m_paths.tts_dir.return_value = tmp_path
            clips, adjusted = await synthesize_all(
                1, translated, voice="v", max_rate=2.0, video_duration=100.0)
        assert all(c["rate"] == 1.0 for c in clips)
        assert all(r == 1.0 for _, r in calls)
        assert len(calls) == 2                     # 只一遍，两句

    async def test_adjusted_segments_axis_and_orig_preserved(self, tmp_path):
        translated = [{"start": 0.0, "end": 2.0, "en": "a", "zh": "你好"},
                      {"start": 5.0, "end": 7.0, "en": "b", "zh": "再见"}]
        calls: list = []
        with patch(f"{_DUB}.tts_synthesize", _fake_tts(calls, base=1.0)), \
             patch(f"{_DUB}.paths") as m_paths:
            m_paths.tts_dir.return_value = tmp_path
            _, adjusted = await synthesize_all(
                1, translated, voice="v", max_rate=2.0, video_duration=100.0)
        assert adjusted[0]["start"] == 0.0 and adjusted[0]["end"] == 1.0
        assert adjusted[1]["start"] == 5.0 and adjusted[1]["end"] == 6.0
        assert adjusted[0]["orig_start"] == 0.0 and adjusted[0]["orig_end"] == 2.0
        assert adjusted[1]["orig_start"] == 5.0 and adjusted[1]["orig_end"] == 7.0
        assert adjusted[0]["zh"] == "你好" and adjusted[1]["en"] == "b"

    async def test_idempotent_skip_existing(self, tmp_path):
        translated = [{"start": 0.0, "end": 2.0, "en": "a", "zh": "你好"}]
        calls: list = []
        with patch(f"{_DUB}.tts_synthesize", _fake_tts(calls, base=1.0)), \
             patch(f"{_DUB}.paths") as m_paths:
            m_paths.tts_dir.return_value = tmp_path
            await synthesize_all(1, translated, voice="v", max_rate=2.0, video_duration=100.0)
            await synthesize_all(1, translated, voice="v", max_rate=2.0, video_duration=100.0)
        assert len(calls) == 1                     # 第二次全跳过

    async def test_rerun_on_adjusted_json_is_idempotent(self, tmp_path):
        base_translated = [{"start": 0.0, "end": 2.0, "en": "a", "zh": "你好"},
                           {"start": 5.0, "end": 7.0, "en": "b", "zh": "再见"}]
        calls: list = []
        with patch(f"{_DUB}.tts_synthesize", _fake_tts(calls, base=1.0)), \
             patch(f"{_DUB}.paths") as m_paths:
            m_paths.tts_dir.return_value = tmp_path
            _, adjusted1 = await synthesize_all(
                1, base_translated, voice="v", max_rate=2.0, video_duration=100.0)
            _, adjusted2 = await synthesize_all(
                1, adjusted1, voice="v", max_rate=2.0, video_duration=100.0)
        assert [s["start"] for s in adjusted2] == [s["start"] for s in adjusted1]
        assert [s["end"] for s in adjusted2] == [s["end"] for s in adjusted1]


class TestSynthesizeNatural:
    async def test_all_rate_1_and_structure(self, tmp_path):
        segments = [{"start": 0.0, "end": 4.0, "en": "a", "zh": "甲"},
                    {"start": 6.0, "end": 9.0, "en": "b", "zh": "乙"}]
        calls: list = []
        with patch(f"{_DUB}.tts_synthesize", _fake_tts(calls, base=1.0)), \
             patch(f"{_DUB}.paths") as m_paths:
            m_paths.tts_dir.return_value = tmp_path
            clips = await synthesize_natural(1, segments, voice="v")
        assert all(r == 1.0 for _, r in calls)
        assert [t for t, _ in calls] == ["甲", "乙"]
        assert [c["index"] for c in clips] == [0, 1]
        assert clips[0]["path"] == str(tmp_path / f"00000_{_zh_hash('甲')}.wav")
        assert clips[1]["path"] == str(tmp_path / f"00001_{_zh_hash('乙')}.wav")
        assert set(clips[0]) == {"index", "path", "duration"}
        assert clips[0]["duration"] == pytest.approx(1.0, abs=0.05)

    async def test_idempotent_skip_existing(self, tmp_path):
        segments = [{"start": 0.0, "end": 2.0, "en": "a", "zh": "你好"}]
        calls: list = []
        with patch(f"{_DUB}.tts_synthesize", _fake_tts(calls, base=1.0)), \
             patch(f"{_DUB}.paths") as m_paths:
            m_paths.tts_dir.return_value = tmp_path
            await synthesize_natural(1, segments, voice="v")
            await synthesize_natural(1, segments, voice="v")
        assert len(calls) == 1

    async def test_hash_name_reuses_unchanged_resynths_edited(self, tmp_path):
        orig = [{"start": 0.0, "end": 4.0, "en": "a", "zh": "引言原文"},
                {"start": 4.0, "end": 8.0, "en": "b", "zh": "正文不变"}]
        calls1: list = []
        with patch(f"{_DUB}.tts_synthesize", _fake_tts(calls1, base=1.0)), \
             patch(f"{_DUB}.paths") as m_paths:
            m_paths.tts_dir.return_value = tmp_path
            await synthesize_natural(1, orig, voice="v")
        assert sorted(t for t, _ in calls1) == ["引言原文", "正文不变"]

        edited = [{"start": 0.0, "end": 4.0, "en": "a", "zh": "引言改写"},
                  {"start": 4.0, "end": 8.0, "en": "b", "zh": "正文不变"}]
        calls2: list = []
        with patch(f"{_DUB}.tts_synthesize", _fake_tts(calls2, base=1.0)), \
             patch(f"{_DUB}.paths") as m_paths:
            m_paths.tts_dir.return_value = tmp_path
            clips = await synthesize_natural(2, edited, voice="v")
        assert [t for t, _ in calls2] == ["引言改写"]
        assert clips[0]["path"] == str(tmp_path / f"00000_{_zh_hash('引言改写')}.wav")
        assert clips[1]["path"] == str(tmp_path / f"00001_{_zh_hash('正文不变')}.wav")

    async def test_no_dub_skips_synth_and_keeps_index(self, tmp_path):
        segments = [{"start": 0.0, "end": 4.0, "en": "a", "zh": "甲"},
                    {"start": 4.0, "end": 8.0, "en": "b", "zh": "免责", "no_dub": True},
                    {"start": 8.0, "end": 12.0, "en": "c", "zh": "丙"}]
        calls: list = []
        with patch(f"{_DUB}.tts_synthesize", _fake_tts(calls, base=1.0)), \
             patch(f"{_DUB}.paths") as m_paths:
            m_paths.tts_dir.return_value = tmp_path
            clips = await synthesize_natural(1, segments, voice="v")
        assert [t for t, _ in calls] == ["甲", "丙"]
        assert [c["index"] for c in clips] == [0, 1, 2]
        assert clips[1]["no_dub"] is True
        assert clips[1]["duration"] == 0.0 and clips[1]["path"] is None
        assert clips[0]["path"] == str(tmp_path / f"00000_{_zh_hash('甲')}.wav")
        assert clips[2]["path"] == str(tmp_path / f"00002_{_zh_hash('丙')}.wav")
        assert clips[2]["duration"] == pytest.approx(1.0, abs=0.05)


class TestBuildTrack:
    def test_track_duration_and_offsets(self, tmp_path):
        _sine_wav(tmp_path / "0.wav", 1.0)
        _sine_wav(tmp_path / "1.wav", 1.0)
        clips = [{"index": 0, "path": str(tmp_path / "0.wav"), "start": 0.5, "duration": 1.0},
                 {"index": 1, "path": str(tmp_path / "1.wav"), "start": 3.0, "duration": 1.0}]
        out = build_track(clips, total_duration=5.0, out_wav=tmp_path / "track.wav")
        from pydub import AudioSegment
        track = AudioSegment.from_wav(str(out))
        assert abs(len(track) - 5000) < 50         # 底长=视频时长(未溢出)
        assert track[500:600].rms > track[0:100].rms   # 0.5s 有声 / 0.1s 静音

    def test_track_extends_when_overflow(self, tmp_path):
        _sine_wav(tmp_path / "0.wav", 2.0)
        clips = [{"index": 0, "path": str(tmp_path / "0.wav"), "start": 4.0, "duration": 2.0}]
        out = build_track(clips, total_duration=5.0, out_wav=tmp_path / "track.wav")
        from pydub import AudioSegment
        track = AudioSegment.from_wav(str(out))
        assert abs(len(track) - 6300) < 50         # max(5.0, 4.0+2.0+0.3)=6.3s


class TestRetry:
    async def test_retry_succeeds_on_fourth_attempt(self):
        from app.video.pipeline.dubber import _retry
        calls = {"n": 0}

        async def flaky():
            calls["n"] += 1
            if calls["n"] < 4:
                raise RuntimeError("TTS 偶发失败")
            return "ok"

        with patch(f"{_DUB}.asyncio.sleep", new=AsyncMock()) as m_sleep:
            result = await _retry(flaky)
        assert result == "ok" and calls["n"] == 4
        assert [c.args[0] for c in m_sleep.call_args_list] == [2.0, 6.0, 18.0]
