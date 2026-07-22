"""muxer 测试——ffmpeg 真跑 2 秒合成素材，验证音轨替换/字幕断行/音频分层链路。

平移自 test_muxer.py：仅换 import 面（``app.video.pipeline.muxer``），logo 资产随包在
pipeline/resources/nbdpsy_logo.png（_LOGO_PATH 指向它）。
"""
import subprocess
from pathlib import Path

import pytest

from app.video.pipeline import muxer
from app.video.pipeline.muxer import (
    _split_screens,
    _wrap_zh,
    build_ass,
    mux,
    probe_nvenc,
)


def _make_fixtures(tmp: Path):
    subprocess.run(["ffmpeg", "-y", "-f", "lavfi", "-i", "testsrc=duration=2:size=320x240:rate=10",
                    "-f", "lavfi", "-i", "sine=frequency=220:duration=2",
                    "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac",
                    str(tmp / "v.mp4")], capture_output=True, check=True)
    subprocess.run(["ffmpeg", "-y", "-f", "lavfi", "-i", "sine=frequency=880:duration=2",
                    "-ar", "24000", "-ac", "1", str(tmp / "dub.wav")],
                   capture_output=True, check=True)


def _probe_json(path: Path) -> dict:
    import json
    out = subprocess.run(["ffprobe", "-v", "quiet", "-print_format", "json",
                          "-show_streams", str(path)], capture_output=True, check=True)
    return json.loads(out.stdout)


class TestMuxer:
    def test_build_ass_escapes_and_orders(self, tmp_path):
        segs = [{"start": 0.0, "end": 1.2, "zh": "依恋{理论}，第一行"},
                {"start": 1.2, "end": 2.0, "zh": "第二行"}]
        p = build_ass(segs, tmp_path / "s.ass", disclaimer=None)
        text = p.read_text(encoding="utf-8")
        assert "Dialogue: 0,0:00:00.00,0:00:01.20" in text
        assert "{理论}" not in text  # 大括号必须转义（ASS 覆盖标签语法）

    def test_build_ass_prepends_disclaimer(self, tmp_path):
        # 默认在片头插入 Notice 版权声明：0 起、显示 _DISCLAIMER_SECONDS 秒、含 NBDpsy
        segs = [{"start": 0.5, "end": 2.0, "zh": "正文第一句"}]
        text = build_ass(segs, tmp_path / "s.ass").read_text(encoding="utf-8")
        assert "Style: Notice" in text  # Notice 样式已声明在头部
        notices = [ln for ln in text.splitlines()
                   if ln.startswith("Dialogue") and ",Notice," in ln]
        assert len(notices) == 1
        end = notices[0][len("Dialogue: "):].split(",", 9)[2]
        assert notices[0].split(",", 9)[1].endswith("0:00:00.00")
        assert end == muxer._ass_time(muxer._DISCLAIMER_SECONDS)
        assert "NBDpsy" in notices[0] and "如有侵权" in notices[0]

    def test_build_ass_disclaimer_can_be_disabled(self, tmp_path):
        segs = [{"start": 0.0, "end": 2.0, "zh": "正文"}]
        text = build_ass(segs, tmp_path / "s.ass", disclaimer=None).read_text(encoding="utf-8")
        assert ",Notice," not in text  # 关闭后无片头声明 Dialogue

    @staticmethod
    def _notice_alignment(text: str) -> str:
        line = next(ln for ln in text.splitlines() if ln.startswith("Style: Notice"))
        return line.split(":", 1)[1].split(",")[9].strip()

    def test_build_ass_disclaimer_default_alignment_top(self, tmp_path):
        segs = [{"start": 0.0, "end": 2.0, "zh": "正文"}]
        text = build_ass(segs, tmp_path / "s.ass").read_text(encoding="utf-8")
        assert self._notice_alignment(text) == "8"

    def test_build_ass_disclaimer_alignment_centered(self, tmp_path):
        segs = [{"start": 0.0, "end": 2.0, "zh": "正文"}]
        text = build_ass(segs, tmp_path / "s.ass",
                         disclaimer_alignment=5).read_text(encoding="utf-8")
        assert self._notice_alignment(text) == "5"

    def test_ass_time_minute_carry(self):
        from app.video.pipeline.muxer import _ass_time
        assert _ass_time(59.999) == "0:01:00.00"
        assert _ass_time(3661.5) == "1:01:01.50"

    async def test_mux_copy_replaces_audio(self, tmp_path):
        _make_fixtures(tmp_path)
        out = tmp_path / "out.mp4"
        # 无字幕无 logo → 走 copy 快路径（logo_path=None 显式关水印）
        await mux(tmp_path / "v.mp4", tmp_path / "dub.wav", out,
                  ass_path=None, use_nvenc=False, logo_path=None)
        streams = _probe_json(out)["streams"]
        v = [s for s in streams if s["codec_type"] == "video"][0]
        assert v["codec_name"] == "h264"  # 视频流未重编码（copy）
        assert any(s["codec_type"] == "audio" for s in streams)

    async def test_mux_overlays_logo(self, tmp_path):
        # logo 水印路径：随包 logo 资产叠加到右下角，产出有效带音视频的 mp4
        _make_fixtures(tmp_path)
        out = tmp_path / "logo.mp4"
        await mux(tmp_path / "v.mp4", tmp_path / "dub.wav", out,
                  ass_path=None, use_nvenc=False, logo_path=muxer._LOGO_PATH)
        assert out.exists() and out.stat().st_size > 1000
        streams = _probe_json(out)["streams"]
        assert any(s["codec_type"] == "video" for s in streams)
        assert any(s["codec_type"] == "audio" for s in streams)

    async def test_mux_missing_logo_degrades(self, tmp_path):
        # logo 文件缺失 → 跳过水印不报错，无字幕时仍走 copy 快路径
        _make_fixtures(tmp_path)
        out = tmp_path / "nologo.mp4"
        await mux(tmp_path / "v.mp4", tmp_path / "dub.wav", out,
                  ass_path=None, use_nvenc=False, logo_path=tmp_path / "missing.png")
        assert out.exists() and out.stat().st_size > 1000

    async def test_mux_burn_subtitles(self, tmp_path):
        _make_fixtures(tmp_path)
        segs = [{"start": 0.2, "end": 1.8, "zh": "测试字幕"}]
        ass = build_ass(segs, tmp_path / "s.ass")
        out = tmp_path / "burn.mp4"
        await mux(tmp_path / "v.mp4", tmp_path / "dub.wav", out,
                  ass_path=ass, use_nvenc=False)
        assert out.exists() and out.stat().st_size > 1000

    async def test_probe_nvenc_returns_bool(self):
        assert isinstance(await probe_nvenc(), bool)

    async def test_mux_appends_fade_out_filters(self, tmp_path, monkeypatch):
        # 给 fade_out_seconds + total_seconds → filter 链尾追加 fade=out / afade=out
        captured = {}

        async def _fake_run(argv, *, timeout):
            captured["argv"] = argv
        monkeypatch.setattr(muxer, "_run_ffmpeg", _fake_run)
        segs = [{"start": 0.0, "end": 2.0, "zh": "字幕"}]
        ass = build_ass(segs, tmp_path / "s.ass")
        await mux(tmp_path / "v.mp4", tmp_path / "dub.wav", tmp_path / "out.mp4",
                  ass_path=ass, use_nvenc=False, logo_path=None,
                  fade_out_seconds=3.0, total_seconds=120.0)
        argv = captured["argv"]
        fc = argv[argv.index("-filter_complex") + 1]
        assert "fade=t=out:st=117:d=3" in fc          # 视频末尾 3s 淡出
        assert "afade=t=out:st=117:d=3" in fc         # 音频末尾 3s 淡出
        assert "[vsub]fade=t=out" in fc               # 叠加在字幕流之后（vsub → vfade）
        assert "[vfade]" in argv and "[aout]" in argv  # 最终流：视频 vfade / 音频 afade 输出
        assert "copy" not in argv                     # 淡出请求禁用 copy 快路径（须重编码）

    async def test_mux_nvenc_runtime_failure_falls_back_libx264(self, tmp_path, monkeypatch):
        # NVENC probe 通过但真编码失败（半坏驱动/显存争用，e2e job1 实证）→ 降级 libx264 重试一次
        calls = []

        async def _fake_run(argv, *, timeout):
            calls.append(list(argv))
            if "h264_nvenc" in argv:
                raise muxer.MuxError("ffmpeg 失败: InitializeEncoder failed: out of memory (10)")
        monkeypatch.setattr(muxer, "_run_ffmpeg", _fake_run)
        segs = [{"start": 0.0, "end": 2.0, "zh": "字幕"}]
        ass = build_ass(segs, tmp_path / "s.ass")
        await mux(tmp_path / "v.mp4", tmp_path / "dub.wav", tmp_path / "out.mp4",
                  ass_path=ass, use_nvenc=True, logo_path=None)
        assert len(calls) == 2                       # 第一轮 nvenc 失败 + 第二轮 libx264 重试
        assert "h264_nvenc" in calls[0]
        assert "libx264" in calls[1] and "h264_nvenc" not in calls[1]
        # 除编码器四参外其余 argv 不变（滤镜链/映射/音频参数原样保留）
        strip = lambda a: [x for x in a if x not in (
            "h264_nvenc", "libx264", "p4", "veryfast")]
        assert strip(calls[0]) == strip(calls[1])

    async def test_mux_libx264_failure_raises_no_retry(self, tmp_path, monkeypatch):
        # 本就走 libx264 的失败原样上抛，不重试
        calls = []

        async def _fake_run(argv, *, timeout):
            calls.append(list(argv))
            raise muxer.MuxError("ffmpeg 失败: boom")
        monkeypatch.setattr(muxer, "_run_ffmpeg", _fake_run)
        segs = [{"start": 0.0, "end": 2.0, "zh": "字幕"}]
        ass = build_ass(segs, tmp_path / "s.ass")
        with pytest.raises(muxer.MuxError):
            await mux(tmp_path / "v.mp4", tmp_path / "dub.wav", tmp_path / "out.mp4",
                      ass_path=ass, use_nvenc=False, logo_path=None)
        assert len(calls) == 1

    async def test_mux_no_fade_kwargs_unchanged(self, tmp_path, monkeypatch):
        # 不给 fade 参数（transport 链默认）→ 无字幕无 logo 仍走 copy 快路径、无 fade
        captured = {}

        async def _fake_run(argv, *, timeout):
            captured["argv"] = argv
        monkeypatch.setattr(muxer, "_run_ffmpeg", _fake_run)
        await mux(tmp_path / "v.mp4", tmp_path / "dub.wav", tmp_path / "out.mp4",
                  ass_path=None, use_nvenc=False, logo_path=None)
        assert "copy" in captured["argv"]
        assert "-filter_complex" not in captured["argv"]

    def test_build_ass_wraps_long_segment(self, tmp_path):
        seg = [{"start": 0.0, "end": 5.0,
                "zh": "压力并不总是坏事，它能帮你爆发额外的能量和专注力，比如在进行竞技体育比赛的时候。"}]
        text = build_ass(seg, tmp_path / "s.ass", disclaimer=None).read_text(encoding="utf-8")
        dialogue = [ln for ln in text.splitlines() if ln.startswith("Dialogue")][0]
        assert "\\N" in dialogue  # 已断行

    @staticmethod
    def _dialogues(text: str) -> list[str]:
        return [ln for ln in text.splitlines() if ln.startswith("Dialogue")]

    @staticmethod
    def _dialogue_body(line: str) -> str:
        return line[len("Dialogue: "):].split(",", 9)[9]

    @staticmethod
    def _dialogue_times(line: str) -> tuple[str, str]:
        parts = line[len("Dialogue: "):].split(",", 9)
        return parts[1], parts[2]

    def test_build_ass_splits_over_two_lines_into_screens(self, tmp_path):
        seg = [{"start": 0.0, "end": 9.0, "zh": "啊" * 90}]
        text = build_ass(seg, tmp_path / "s.ass", disclaimer=None).read_text(encoding="utf-8")
        dialogues = self._dialogues(text)
        assert len(dialogues) == 3
        for d in dialogues:
            assert self._dialogue_body(d).count("\\N") <= 1  # ≤2 行 = ≤1 个换行符

    def test_build_ass_screens_time_seamless_and_total(self, tmp_path):
        seg = [{"start": 0.0, "end": 9.0, "zh": "啊" * 90}]
        text = build_ass(seg, tmp_path / "s.ass", disclaimer=None).read_text(encoding="utf-8")
        times = [self._dialogue_times(d) for d in self._dialogues(text)]
        assert times[0][0] == "0:00:00.00"
        assert times[-1][1] == "0:00:09.00"
        for prev, nxt in zip(times, times[1:]):
            assert prev[1] == nxt[0]  # 前屏 end 与后屏 start 字面一致，无缝无重叠

    def test_build_ass_screens_no_leading_punctuation(self, tmp_path):
        seg = [{"start": 0.0, "end": 8.0,
                "zh": "第一句话讲的是压力管理，第二句话讲的是情绪调节，第三句话讲的是自我关怀的重要性和方法。"}]
        text = build_ass(seg, tmp_path / "s.ass", disclaimer=None).read_text(encoding="utf-8")
        for d in self._dialogues(text):
            for row in self._dialogue_body(d).rstrip("\n").split("\\N"):
                assert not row or row[0] not in muxer._ZH_WRAP_PUNCT

    def test_build_ass_short_segment_single_screen(self, tmp_path):
        seg = [{"start": 1.0, "end": 3.0, "zh": "这是一句短字幕"}]
        text = build_ass(seg, tmp_path / "s.ass", disclaimer=None).read_text(encoding="utf-8")
        dialogues = self._dialogues(text)
        assert len(dialogues) == 1
        assert self._dialogue_times(dialogues[0]) == ("0:00:01.00", "0:00:03.00")


class TestWrapZh:
    def test_short_not_wrapped(self):
        assert _wrap_zh("这是一句短字幕") == "这是一句短字幕"
        assert "\\N" not in _wrap_zh("短句")

    def test_long_wrapped_each_line_within_limit(self):
        text = "这是一个非常非常长的中文句子需要被自动断行处理成多行显示才不会溢出屏幕左右两边"
        wrapped = _wrap_zh(text, max_chars=18)
        assert "\\N" in wrapped
        assert all(len(line) <= 18 for line in wrapped.split("\\N"))

    def test_prefers_punctuation_break(self):
        wrapped = _wrap_zh("今天天气真的非常好啊，我们一起出去公园里散步吧", max_chars=18)
        assert wrapped.split("\\N")[0].endswith("，")

    def test_hard_break_when_no_punct(self):
        wrapped = _wrap_zh("啊" * 40, max_chars=18)
        lines = wrapped.split("\\N")
        assert len(lines) == 3 and all(len(line) <= 18 for line in lines)

    def test_respects_existing_newline_marker(self):
        assert _wrap_zh("第一段\\N第二段") == "第一段\\N第二段"

    def test_no_line_starts_with_punctuation(self):
        text = "啊" * 18 + "。" + "啊" * 18 + "，" + "啊" * 6
        lines = _wrap_zh(text, max_chars=18).split("\\N")
        assert all(not line or line[0] not in muxer._ZH_WRAP_PUNCT for line in lines)
        assert lines[0].endswith("。")

    def test_consecutive_punct_backstop_no_leading_punct(self):
        text = "啊" * 18 + "，。！" + "啊" * 10
        lines = _wrap_zh(text, max_chars=18).split("\\N")
        assert all(not line or line[0] not in muxer._ZH_WRAP_PUNCT for line in lines)


class TestSplitScreens:
    def test_one_line_single_screen(self):
        assert _split_screens(["行一"]) == [["行一"]]

    def test_two_lines_single_screen(self):
        assert _split_screens(["行一", "行二"]) == [["行一", "行二"]]

    def test_three_lines_two_screens(self):
        assert _split_screens(["a", "b", "c"]) == [["a", "b"], ["c"]]

    def test_five_lines_three_screens(self):
        assert _split_screens(["a", "b", "c", "d", "e"]) == [["a", "b"], ["c", "d"], ["e"]]

    def test_each_screen_within_max_lines(self):
        screens = _split_screens([str(i) for i in range(7)], max_lines=2)
        assert all(len(s) <= 2 for s in screens)

    def test_empty_input(self):
        assert _split_screens([]) == []


def _probe_duration(path: Path) -> float:
    import json
    out = subprocess.run(["ffprobe", "-v", "quiet", "-print_format", "json",
                          "-show_format", str(path)], capture_output=True, check=True)
    return float(json.loads(out.stdout)["format"]["duration"])


class TestBuildMixedAudio:
    async def test_amix_layers_dub_and_accompaniment(self, tmp_path, monkeypatch):
        # mock demucs 分离（不拉 GPU/模型），真跑 ffmpeg amix 验证两轨混合
        _make_fixtures(tmp_path)  # v.mp4(带 sine220 音轨) + dub.wav(sine880)
        no_vocals = tmp_path / "no_vocals.wav"
        subprocess.run(["ffmpeg", "-y", "-f", "lavfi", "-i", "sine=frequency=440:duration=2",
                        "-ar", "44100", "-ac", "2", str(no_vocals)], capture_output=True, check=True)

        async def _fake_demucs(audio_path, out_root, *, timeout):
            return no_vocals
        monkeypatch.setattr(muxer, "_run_demucs", _fake_demucs)

        out = tmp_path / "mixed.wav"
        result = await muxer.build_mixed_audio(tmp_path / "v.mp4", tmp_path / "dub.wav", out)
        assert result == out and out.exists()
        streams = _probe_json(out)["streams"]
        assert any(s["codec_type"] == "audio" for s in streams)
        assert abs(_probe_duration(out) - 2.0) < 0.3  # duration=first(配音) → ≈2s

    async def test_degrades_to_dub_when_demucs_unavailable(self, tmp_path, monkeypatch):
        _make_fixtures(tmp_path)

        async def _fake_demucs(audio_path, out_root, *, timeout):
            return None  # demucs 不可用/失败
        monkeypatch.setattr(muxer, "_run_demucs", _fake_demucs)

        out = tmp_path / "mixed.wav"
        result = await muxer.build_mixed_audio(tmp_path / "v.mp4", tmp_path / "dub.wav", out)
        assert result == tmp_path / "dub.wav"  # 降级返回原配音
        assert not out.exists()  # 未产出混音文件
