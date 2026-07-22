"""programmatic 渲染器：表达式生成（纯函数）+ 球 PNG + 渲染冒烟。"""
from pathlib import Path

import pytest
from PIL import Image

from app.video.pipeline.remake import style
from app.video.pipeline.remake.renderers import programmatic, get_renderer, RENDERERS


class TestStaticRender:
    pytestmark = pytest.mark.unit

    @pytest.mark.asyncio
    async def test_static_scene_x_has_no_sin_and_centered(self, monkeypatch, tmp_path):
        # wave2 问题③：静止休息球固定居中，overlay x 表达式不带 sin
        captured = {}

        async def fake_run(argv, *, timeout):
            captured["argv"] = argv
        monkeypatch.setattr(programmatic, "_run_ffmpeg", fake_run)
        scene = {"id": 1, "t0": 0.0, "t1": 3.0, "type": "ball_exercise",
                 "renderer": "programmatic",
                 "params": {"ball_color": style.CREAM, "bg_color": style.DARK_BG,
                            "period_s": 1.6, "amplitude_ratio": 0.42,
                            "audio_cue": "alternating_tone", "static": True}}
        await programmatic.render(scene, tmp_path / "seg.mp4")
        # 只查 filter_complex 值（避开 tmp 路径里可能含 "sin" 的假阳性）
        argv = captured["argv"]
        fc = argv[argv.index("-filter_complex") + 1]
        assert "sin" not in fc                       # 静止：无摆动
        assert "(W-w)/2" in fc                        # 居中

    @pytest.mark.asyncio
    async def test_moving_scene_x_has_sin(self, monkeypatch, tmp_path):
        captured = {}

        async def fake_run(argv, *, timeout):
            captured["argv"] = argv
        monkeypatch.setattr(programmatic, "_run_ffmpeg", fake_run)
        scene = {"id": 1, "t0": 0.0, "t1": 3.0, "type": "ball_exercise",
                 "renderer": "programmatic",
                 "params": {"ball_color": style.BURGUNDY, "bg_color": style.DARK_BG,
                            "period_s": 1.6, "amplitude_ratio": 0.42,
                            "audio_cue": "alternating_tone"}}
        await programmatic.render(scene, tmp_path / "seg.mp4")
        argv = captured["argv"]
        fc = argv[argv.index("-filter_complex") + 1]
        assert "sin" in fc                           # 运动：带摆动


class TestBallPosition:
    pytestmark = pytest.mark.unit

    def test_ball_vertically_centered(self):
        # A5 意见 4：球心垂直居中——BALL_Y_RATIO 使球心落在帧高中线（按常量计算，非硬编码）
        assert round(style.VIDEO_H * style.BALL_Y_RATIO) == style.VIDEO_H // 2

    @pytest.mark.asyncio
    async def test_render_places_ball_center_on_midline(self, monkeypatch, tmp_path):
        # overlay 的 y 是球贴图左上角：球心 = y + radius，应落在帧高中线（由常量驱动）
        captured = {}

        async def fake_run(argv, *, timeout):
            captured["argv"] = argv
        monkeypatch.setattr(programmatic, "_run_ffmpeg", fake_run)
        scene = {"id": 1, "t0": 0.0, "t1": 3.0, "type": "ball_exercise",
                 "renderer": "programmatic",
                 "params": {"ball_color": style.BURGUNDY, "bg_color": style.DARK_BG,
                            "period_s": 1.6, "amplitude_ratio": 0.42,
                            "audio_cue": "alternating_tone"}}
        await programmatic.render(scene, tmp_path / "seg.mp4")
        argv = captured["argv"]
        fc = argv[argv.index("-filter_complex") + 1]
        radius = max(2, round(style.VIDEO_H * style.BALL_RADIUS_RATIO))
        # F2：画布 2×radius，球心锚点 = 画布半宽（= radius）——y 计算相应调整
        half = programmatic.ball_canvas_px(radius) // 2
        import re
        # overlay 链 :y= 后接 [v]，用正则取整数 y，避免尾字符误伤
        y_top = int(re.search(r":y=(\d+)", fc).group(1))
        assert y_top + half == round(style.VIDEO_H * style.BALL_Y_RATIO)
        assert y_top + half == style.VIDEO_H // 2


class TestExpr:
    pytestmark = pytest.mark.unit

    def test_expr_contains_global_phase(self):
        # 场景 t0=10、T=2：局部 t 需补偿 +10 才是全局相位
        expr = programmatic.overlay_x_expr(
            width=1920, amplitude_ratio=0.42, period_s=2.0, global_t0=10.0)
        assert "(t+10.0)" in expr.replace(" ", "")
        assert "sin" in expr and "806" in expr    # 振幅 = round(1920*0.42) = 806

    def test_expr_centered(self):
        expr = programmatic.overlay_x_expr(
            width=1920, amplitude_ratio=0.42, period_s=1.6, global_t0=0.0)
        assert expr.startswith("(W-w)/2")

    def test_registry_exposes_implemented_only(self):
        assert set(RENDERERS) == {"programmatic", "still_image"}
        with pytest.raises(KeyError):
            get_renderer("seedance")


class TestBallPng:
    pytestmark = pytest.mark.unit

    def test_png_size_and_color(self, tmp_path):
        p = programmatic.ball_png(tmp_path / "ball.png",
                                  color_hex=style.BURGUNDY, radius_px=26)
        img = Image.open(p).convert("RGBA")
        # F2：锐利实心球画布 = 2×26 = 52，球心在几何中心
        size = programmatic.ball_canvas_px(26)
        assert size == 52 and img.size == (52, 52)
        c = size // 2
        r, g, b, a = img.getpixel((c, c))
        assert (r, g, b) == (0x7A, 0x1F, 0x2B) and a == 255   # 核心实心 alpha=255
        # 四角远超半径——全透明
        assert img.getpixel((0, 0))[3] == 0

    def test_sharp_ball_only_2px_soft_edge(self, tmp_path):
        # F2：锐利实心球——中心/radius-3 处 alpha=255（实心），2px 软边内 0<alpha<255，radius 处归 0
        radius = 40
        p = programmatic.ball_png(tmp_path / "sharp.png",
                                  color_hex=style.GOLD, radius_px=radius)
        img = Image.open(p).convert("RGBA")
        size = programmatic.ball_canvas_px(radius)
        assert size == 2 * radius
        c = size // 2
        assert img.getpixel((c, c))[3] == 255                 # 中心实心
        assert img.getpixel((c + radius - 3, c))[3] == 255    # radius-3 仍实心（软边只有 2px）
        a_edge = img.getpixel((c + radius - 1, c))[3]
        assert 0 < a_edge < 255                                # 2px 软边内半透明（抗锯齿）
        assert img.getpixel((0, c))[3] == 0                   # 水平最左 = radius 距离，归 0
        assert img.getpixel((0, 0))[3] == 0                   # 四角全透明


class TestFilterChain:
    """F2：滤镜链 argv 断言——原生 120fps 单级 overlay，无 tmix 超采样（纯参数校验）。"""
    pytestmark = pytest.mark.unit

    @staticmethod
    def _patch(monkeypatch):
        captured = {}

        async def fake_run(argv, *, timeout):
            captured["argv"] = argv
        monkeypatch.setattr(programmatic, "_run_ffmpeg", fake_run)
        return captured

    @staticmethod
    def _color_src(argv):
        return next(a for a in argv if a.startswith("color=c="))

    @pytest.mark.asyncio
    async def test_moving_scene_native_fps_single_overlay(self, monkeypatch, tmp_path):
        # F2：运动源原生 120fps → 单级 overlay 直出，无 tmix、无 480 超采样
        captured = self._patch(monkeypatch)
        scene = {"id": 1, "t0": 0.0, "t1": 3.0, "type": "ball_exercise",
                 "renderer": "programmatic",
                 "params": {"ball_color": style.BURGUNDY, "bg_color": style.DARK_BG,
                            "period_s": 1.6, "amplitude_ratio": 0.42,
                            "audio_cue": "alternating_tone"}}
        await programmatic.render(scene, tmp_path / "seg.mp4")
        argv = captured["argv"]
        src = self._color_src(argv)
        assert f"r={style.FPS}" in src and f"r={4 * style.FPS}" not in src  # 原生 120，无 480 超采样
        fc = argv[argv.index("-filter_complex") + 1]
        assert "overlay=" in fc and "tmix" not in fc                        # 单级 overlay，无 tmix

    @pytest.mark.asyncio
    async def test_static_scene_native_fps_no_tmix(self, monkeypatch, tmp_path):
        # 静止：原生 120fps 直出，跳过 tmix（无运动无需快门平均，省 4x 算力）
        captured = self._patch(monkeypatch)
        scene = {"id": 2, "t0": 0.0, "t1": 3.0, "type": "ball_exercise",
                 "renderer": "programmatic",
                 "params": {"ball_color": style.CREAM, "bg_color": style.DARK_BG,
                            "period_s": 1.6, "amplitude_ratio": 0.42,
                            "audio_cue": "alternating_tone", "static": True}}
        await programmatic.render(scene, tmp_path / "seg.mp4")
        argv = captured["argv"]
        src = self._color_src(argv)
        assert f"r={style.FPS}" in src and f"r={4 * style.FPS}" not in src
        fc = argv[argv.index("-filter_complex") + 1]
        assert "tmix" not in fc


@pytest.mark.integration
@pytest.mark.slow
class TestRenderSmoke:
    # 真 ffmpeg 出球段（宿主 CI 跑 not slow，慢测本地跑）。
    @pytest.mark.asyncio
    async def test_render_two_second_segment(self, tmp_path):
        scene = {"id": 3, "t0": 4.0, "t1": 6.0, "type": "ball_exercise",
                 "renderer": "programmatic",
                 "params": {"ball_color": style.BURGUNDY, "bg_color": style.DARK_BG,
                            "period_s": 1.6, "amplitude_ratio": 0.42,
                            "audio_cue": "alternating_tone"}}
        out = await programmatic.render(scene, tmp_path / "seg.mp4")
        import asyncio, json
        proc = await asyncio.create_subprocess_exec(
            "ffprobe", "-v", "quiet", "-show_entries",
            "format=duration:stream=width,height,avg_frame_rate",
            "-of", "json", str(out),
            stdout=asyncio.subprocess.PIPE)
        raw, _ = await proc.communicate()
        info = json.loads(raw)
        assert float(info["format"]["duration"]) == pytest.approx(2.0, abs=0.2)
        assert info["streams"][0]["width"] == 1920
        assert info["streams"][0]["height"] == 1080
        # 原生渲染输出 120fps（契约 2）
        assert info["streams"][0]["avg_frame_rate"] == f"{style.FPS}/1"


@pytest.mark.integration
@pytest.mark.slow
class TestSharpBallNoSmear:
    """F2 真 ffmpeg 冒烟：任一帧都是锐利实心球，高速时刻无明显拖尾（回滚 A7 运动模糊）。"""

    @staticmethod
    async def _extract_frame(src: Path, t: float, out: Path):
        import asyncio
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-y", "-v", "quiet", "-i", str(src),
            "-ss", str(t), "-frames:v", "1", str(out),
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        await proc.communicate()

    @staticmethod
    def _bright_bbox_width(frame: Path, bg_hex: str) -> int:
        # 亮像素 = 与背景色曼哈顿距离 > 60；返回其水平包围盒宽度
        im = Image.open(frame).convert("RGB")
        px = im.load()
        bg = tuple(int(bg_hex.lstrip("#")[i:i + 2], 16) for i in (0, 2, 4))
        xmin, xmax = im.width, -1
        for yy in range(0, im.height, 2):
            for xx in range(im.width):
                r, g, b = px[xx, yy]
                if abs(r - bg[0]) + abs(g - bg[1]) + abs(b - bg[2]) > 60:
                    if xx < xmin:
                        xmin = xx
                    if xx > xmax:
                        xmax = xx
        return (xmax - xmin + 1) if xmax >= 0 else 0

    @pytest.mark.asyncio
    async def test_high_speed_frame_stays_sharp(self, tmp_path):
        import asyncio, json
        common = {"ball_color": style.CREAM, "bg_color": style.DARK_BG,
                  "period_s": 1.0, "amplitude_ratio": 0.42,
                  "audio_cue": "alternating_tone"}
        moving = {"id": 7, "t0": 0.0, "t1": 2.0, "type": "ball_exercise",
                  "renderer": "programmatic", "params": dict(common)}
        static = {"id": 8, "t0": 0.0, "t1": 2.0, "type": "ball_exercise",
                  "renderer": "programmatic", "params": {**common, "static": True}}
        mv = await programmatic.render(moving, tmp_path / "mv.mp4")
        st = await programmatic.render(static, tmp_path / "st.mp4")

        # 契约 2：输出 120fps
        proc = await asyncio.create_subprocess_exec(
            "ffprobe", "-v", "quiet", "-select_streams", "v:0",
            "-show_entries", "stream=avg_frame_rate", "-of", "json", str(mv),
            stdout=asyncio.subprocess.PIPE)
        raw, _ = await proc.communicate()
        assert json.loads(raw)["streams"][0]["avg_frame_rate"] == f"{style.FPS}/1"

        # 静止基线宽度（= 球直径）
        await self._extract_frame(st, 1.0, tmp_path / "st.png")
        w_static = self._bright_bbox_width(tmp_path / "st.png", style.DARK_BG)
        assert w_static > 0
        # F2 契约 4：高速时刻（T=1.0，球每 0.5s 过中点）任一帧仍是单个锐利球，无拖尾——
        # 亮像素包围盒宽 ≤ 静止球直径 ×1.15（仅容抗锯齿软边的微小差异）
        for t in (0.45, 0.48, 0.5, 0.52, 0.55, 0.95, 1.0, 1.05):
            f = tmp_path / f"mv_{t}.png"
            await self._extract_frame(mv, t, f)
            w_motion = self._bright_bbox_width(f, style.DARK_BG)
            assert w_motion <= w_static * 1.15, f"t={t} 出现拖尾 w={w_motion} 基线={w_static}"
