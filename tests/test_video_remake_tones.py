"""双侧提示音：端点时刻计算 + 声道方向与球位置一致 + 轨长正确。"""
import math
import wave
from pathlib import Path

import numpy as np
import pytest

from app.video.pipeline.remake import tones

pytestmark = pytest.mark.unit


class TestEndpointTimes:
    def test_first_endpoint_is_right_at_quarter_period(self):
        # 球位置 = sin(2πt/T)：t=T/4 时 sin=+1（右端点）
        pts = tones.endpoint_times(0.0, 4.0, period_s=2.0)
        assert pts[0] == (pytest.approx(0.5), +1)

    def test_alternating_sides(self):
        pts = tones.endpoint_times(0.0, 4.0, period_s=2.0)
        sides = [s for _, s in pts]
        assert sides == [+1, -1, +1, -1]

    def test_global_phase_respected_mid_scene(self):
        # 场景从 t0=3.0 开始、T=2.0：全局端点在 0.5+k*1.0，落在 [3,5] 内的是 3.5(左) 4.5(右)
        # (计划原稿此处期望写反，已按数学修正：sin(3.5π)=-1 为左端点)
        pts = tones.endpoint_times(3.0, 5.0, period_s=2.0)
        assert [round(t, 3) for t, _ in pts] == [3.5, 4.5]
        assert [s for _, s in pts] == [-1, +1]
        # 与球位置公式互验：sin(2π*3.5/2)=-1 → 左
        assert math.sin(2 * math.pi * 3.5 / 2.0) == pytest.approx(-1.0)


class TestBilateralTrack:
    def _read(self, path: Path):
        with wave.open(str(path), "rb") as w:
            assert w.getnchannels() == 2
            rate = w.getframerate()
            data = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)
        return rate, data.reshape(-1, 2)

    def test_no_ball_scenes_returns_none(self, tmp_path):
        assert tones.bilateral_track([], 10.0, tmp_path / "t.wav") is None

    def test_track_length_and_channel_direction(self, tmp_path):
        scene = {"t0": 0.0, "t1": 4.0, "type": "ball_exercise",
                 "params": {"period_s": 2.0}}
        out = tones.bilateral_track([scene], 4.0, tmp_path / "t.wav")
        rate, frames = self._read(out)
        assert len(frames) == pytest.approx(4.0 * rate, rel=0.01)
        # t=0.5s（右端点）附近右声道有能量、左声道≈0；t=1.5s（左端点）反之
        w0 = frames[int(0.5 * rate):int(0.62 * rate)]
        assert np.abs(w0[:, 1]).max() > 1000       # 右声道响
        assert np.abs(w0[:, 0]).max() < 50         # 左声道静
        w1 = frames[int(1.5 * rate):int(1.62 * rate)]
        assert np.abs(w1[:, 0]).max() > 1000
        assert np.abs(w1[:, 1]).max() < 50

    def test_silence_outside_ball_scene(self, tmp_path):
        scene = {"t0": 2.0, "t1": 4.0, "params": {"period_s": 2.0}}
        out = tones.bilateral_track([scene], 6.0, tmp_path / "t.wav")
        rate, frames = self._read(out)
        head = frames[: int(1.8 * rate)]
        assert np.abs(head).max() < 50             # 球段外全静音

    def test_static_scene_produces_no_tones(self, tmp_path):
        # wave2 问题③：静止休息球段无双侧提示音——唯一场景静止 → 返回 None
        scene = {"t0": 0.0, "t1": 4.0, "type": "ball_exercise",
                 "params": {"period_s": 2.0, "static": True}}
        assert tones.bilateral_track([scene], 4.0, tmp_path / "t.wav") is None

    def test_static_segment_silent_among_moving(self, tmp_path):
        # 运动段[0,4]有提示音、静止段[4,8]静音
        moving = {"t0": 0.0, "t1": 4.0, "params": {"period_s": 2.0}}
        rest = {"t0": 4.0, "t1": 8.0, "params": {"period_s": 2.0, "static": True}}
        out = tones.bilateral_track([moving, rest], 8.0, tmp_path / "t.wav")
        rate, frames = self._read(out)
        assert np.abs(frames[: int(4.0 * rate)]).max() > 1000   # 运动段有声
        assert np.abs(frames[int(4.0 * rate):]).max() < 50      # 静止段全静音
