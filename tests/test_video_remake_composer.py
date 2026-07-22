"""composer：concat 清单转义 / tones=None 短路 / compose 声明文案注入（mock mux）
+ 真 ffmpeg 混音立体声保真（C1 回归）。"""
import subprocess
import wave
from pathlib import Path
from unittest.mock import AsyncMock, patch

import numpy as np
import pytest

from app.video.pipeline.remake import composer


class TestConcat:
    pytestmark = pytest.mark.unit

    @pytest.mark.asyncio
    async def test_concat_writes_escaped_list_and_calls_ffmpeg(self, tmp_path):
        scenes = [tmp_path / "a'b.mp4", tmp_path / "s2.mp4"]
        for s in scenes:
            s.touch()
        run = AsyncMock()
        with patch.object(composer, "_run_ffmpeg", run):
            out = await composer.concat_scenes(scenes, tmp_path / "full.mp4")
        listfile = tmp_path / "full.concat.txt"
        text = listfile.read_text(encoding="utf-8")
        assert "a'\\''b.mp4" in text            # concat 清单单引号转义
        argv = run.call_args.args[0]
        assert "-f" in argv and "concat" in argv and "-c" in argv


class TestMix:
    pytestmark = pytest.mark.unit

    @pytest.mark.asyncio
    async def test_none_tones_short_circuits(self, tmp_path):
        dub = tmp_path / "dub.wav"; dub.touch()
        out = await composer.mix_audio(dub, None, tmp_path / "m.wav")
        assert out == dub

    @pytest.mark.asyncio
    async def test_mix_uses_amix_normalize_off(self, tmp_path):
        dub = tmp_path / "dub.wav"; dub.touch()
        tone = tmp_path / "tones.wav"; tone.touch()
        run = AsyncMock()
        with patch.object(composer, "_run_ffmpeg", run):
            out = await composer.mix_audio(dub, tone, tmp_path / "m.wav")
        argv = run.call_args.args[0]
        fc = argv[argv.index("-filter_complex") + 1]
        assert "normalize=0" in fc              # 配音不被 amix 压 6dB（搬运链教训）
        assert out == tmp_path / "m.wav"


class TestCompose:
    pytestmark = pytest.mark.unit

    @pytest.mark.asyncio
    async def test_compose_passes_remake_disclaimer(self, tmp_path):
        segs = [{"start": 0.0, "end": 2.0, "zh": "你好", "en": "hi"}]
        with patch.object(composer.muxer, "build_ass") as build, \
             patch.object(composer.muxer, "mux", new=AsyncMock()) as mux:
            await composer.compose(tmp_path / "v.mp4", tmp_path / "a.wav",
                                   segs, tmp_path / "out.mp4", use_nvenc=False)
        assert build.call_args.kwargs["disclaimer"] == composer.REMAKE_DISCLAIMER
        assert mux.await_count == 1

    @pytest.mark.asyncio
    async def test_compose_disclaimer_override(self, tmp_path):
        # B4：global_param.disclaimer_text 覆盖片头声明文案
        segs = [{"start": 0.0, "end": 2.0, "zh": "你好", "en": "hi"}]
        with patch.object(composer.muxer, "build_ass") as build, \
             patch.object(composer.muxer, "mux", new=AsyncMock()):
            await composer.compose(tmp_path / "v.mp4", tmp_path / "a.wav",
                                   segs, tmp_path / "out.mp4", use_nvenc=False,
                                   disclaimer="自定义声明文案")
        assert build.call_args.kwargs["disclaimer"] == "自定义声明文案"

    @pytest.mark.asyncio
    async def test_compose_passes_centered_disclaimer_alignment(self, tmp_path):
        # F1：remake 声明页居中——compose 给 build_ass 传 disclaimer_alignment=5（正中）
        segs = [{"start": 0.0, "end": 2.0, "zh": "你好", "en": "hi"}]
        with patch.object(composer.muxer, "build_ass") as build, \
             patch.object(composer.muxer, "mux", new=AsyncMock()):
            await composer.compose(tmp_path / "v.mp4", tmp_path / "a.wav",
                                   segs, tmp_path / "out.mp4", use_nvenc=False)
        assert build.call_args.kwargs["disclaimer_alignment"] == 5

    def test_disclaimer_copy_exact(self):
        # 文案逐字与 spec §8 一致
        assert composer.REMAKE_DISCLAIMER == (
            "本视频由 NBDpsy 心理咨询工作室制作，练习设计参考国际公开 EMDR 自助资料，\n"
            "不构成医疗建议，如有需要请咨询专业人员")
        assert composer.ATTRIBUTION == "练习设计参考国际公开的 EMDR 双侧刺激自助方法"

    @pytest.mark.asyncio
    async def test_compose_passes_fade_out_to_mux(self, tmp_path):
        # A6：给 total_duration → compose 把 3s 末尾淡出参数透传给 muxer.mux
        segs = [{"start": 0.0, "end": 2.0, "zh": "你好", "en": "hi"}]
        with patch.object(composer.muxer, "build_ass"), \
             patch.object(composer.muxer, "mux", new=AsyncMock()) as mux:
            await composer.compose(tmp_path / "v.mp4", tmp_path / "a.wav",
                                   segs, tmp_path / "out.mp4", use_nvenc=False,
                                   total_duration=120.0)
        assert mux.await_args.kwargs["fade_out_seconds"] == composer._FADE_OUT_SECONDS
        assert mux.await_args.kwargs["total_seconds"] == 120.0
        assert composer._FADE_OUT_SECONDS == 3.0

    @pytest.mark.asyncio
    async def test_compose_no_fade_without_total(self, tmp_path):
        # 不给 total_duration → 不淡出（fade_out_seconds=None，行为不变）
        segs = [{"start": 0.0, "end": 2.0, "zh": "你好", "en": "hi"}]
        with patch.object(composer.muxer, "build_ass"), \
             patch.object(composer.muxer, "mux", new=AsyncMock()) as mux:
            await composer.compose(tmp_path / "v.mp4", tmp_path / "a.wav",
                                   segs, tmp_path / "out.mp4", use_nvenc=False)
        assert mux.await_args.kwargs["fade_out_seconds"] is None


@pytest.mark.integration
@pytest.mark.slow
class TestMixStereoFidelity:
    """C1 回归：dub 是 24kHz mono，混音必须保住立体声——不 mock _run_ffmpeg，跑真 ffmpeg。

    构造仅右声道有音的立体声提示音 + 单声道静音配音，混音后断言输出双声道
    且右声道能量显著高于左声道（EMDR 左右交替不被降混塌成 mono）。
    """

    @staticmethod
    def _read_stereo(path: Path):
        with wave.open(str(path), "rb") as w:
            ch = w.getnchannels()
            data = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)
        return ch, data.reshape(-1, ch) if ch else data

    @pytest.mark.asyncio
    async def test_mix_preserves_stereo_and_right_channel(self, tmp_path):
        dub = tmp_path / "dub.wav"          # 24kHz 单声道静音（复刻 dubber.build_track）
        subprocess.run(["ffmpeg", "-y", "-f", "lavfi",
                        "-i", "anullsrc=r=24000:cl=mono:d=0.5", str(dub)],
                       check=True, capture_output=True)
        tones = tmp_path / "tones.wav"      # 立体声：仅右声道有 440Hz 音，左声道静
        subprocess.run(["ffmpeg", "-y", "-f", "lavfi",
                        "-i", "sine=frequency=440:duration=0.5:sample_rate=44100",
                        "-af", "pan=stereo|c0=0*c0|c1=c0", str(tones)],
                       check=True, capture_output=True)

        out = await composer.mix_audio(dub, tones, tmp_path / "mixed.wav")

        ch, frames = self._read_stereo(out)
        assert ch == 2                                   # 立体声未被降混
        left_max = int(np.abs(frames[:, 0]).max())
        right_max = int(np.abs(frames[:, 1]).max())
        assert right_max > 1000                          # 右声道保住提示音能量
        assert right_max > left_max * 5                  # 右显著高于左（左近静音）
