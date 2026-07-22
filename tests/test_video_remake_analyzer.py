"""analyzer：周期估计/质心/球色 纯函数单测 + analyze 编排（mock 抽帧与 VL）。"""
import math
import random

import pytest
from PIL import Image, ImageDraw

from app.video.pipeline.remake import analyzer

pytestmark = pytest.mark.unit

_LUMA = analyzer._BRIGHT_LUMA


# ---- 向量化改造前的旧纯 Python 双层循环实现，逐字保留作等值对拍参照 ----
def _centroid_x_ref(img: Image.Image):
    gray = img.convert("L")
    w, h = gray.size
    data = gray.load()
    total = 0
    sx = 0.0
    for y in range(0, h, 2):
        for x in range(0, w, 2):
            if data[x, y] > _LUMA:
                total += 1
                sx += x
    return (sx / total) if total > 20 else None


def _frame_features_ref(img: Image.Image):
    gray = img.convert("L")
    w, h = gray.size
    data = gray.load()
    count = 0
    min_x, max_x = w, 0
    for y in range(0, h, 2):
        for x in range(0, w, 2):
            if data[x, y] > _LUMA:
                count += 1
                if x < min_x:
                    min_x = x
                if x > max_x:
                    max_x = x
    if count == 0:
        return 0, 0.0
    return count, (max_x - min_x) / w


def _mean_bright_color_ref(img: Image.Image):
    rgb = img.convert("RGB")
    gray = img.convert("L")
    w, h = rgb.size
    px, gx = rgb.load(), gray.load()
    n, rs, gs, bs = 0, 0, 0, 0
    for y in range(0, h, 2):
        for x in range(0, w, 2):
            if gx[x, y] > _LUMA:
                r, g, b = px[x, y]
                n, rs, gs, bs = n + 1, rs + r, gs + g, bs + b
    if n <= 20:
        return None
    return "#{:02X}{:02X}{:02X}".format(rs // n, gs // n, bs // n)


def _frame_with_ball(x: int, color=(255, 255, 255), size=(640, 360)) -> Image.Image:
    img = Image.new("RGB", size, (10, 10, 10))
    d = ImageDraw.Draw(img)
    d.ellipse([x - 12, 50, x + 12, 74], fill=color)
    return img


def _frame_with_text_bar(size=(640, 360)) -> Image.Image:
    """黑底宽幅文字条帧（模拟标题/文字卡）：宽度占比 0.7*w。"""
    img = Image.new("RGB", size, (10, 10, 10))
    d = ImageDraw.Draw(img)
    w, _ = size
    bar_w = int(0.7 * w)
    x0 = (w - bar_w) // 2
    d.rectangle([x0, 150, x0 + bar_w, 210], fill=(240, 240, 240))
    return img


def _uniform_frame(v: int, size=(640, 360)) -> Image.Image:
    """纯色填充帧（灰阶 v）：L 通道恰为 v，用于卡 luma>60 边界（59/60/61）。"""
    return Image.new("RGB", size, (v, v, v))


def _noise_frame(size=(200, 120), seed=0) -> Image.Image:
    """随机灰噪帧：亮像素散布全图，压力测试质心/bbox 的坐标还原。"""
    random.seed(seed)
    img = Image.new("RGB", size)
    img.putdata([(random.randint(0, 255),) * 3 for _ in range(size[0] * size[1])])
    return img


class TestPureFunctions:
    def test_centroid_x_locates_ball(self):
        assert analyzer._centroid_x(_frame_with_ball(200)) == pytest.approx(200, abs=3)

    def test_centroid_none_on_dark_frame(self):
        img = Image.new("RGB", (640, 360), (8, 8, 8))
        assert analyzer._centroid_x(img) is None

    def test_mean_bright_color_detects_green(self):
        hexc = analyzer._mean_bright_color(_frame_with_ball(200, color=(162, 196, 12)))
        # 亮像素均值应接近绿色参考
        r, g, b = (int(hexc[i:i + 2], 16) for i in (1, 3, 5))
        assert g > r and g > b

    def test_estimate_period_recovers_known_period(self):
        # 已知 T=1.6s 的正弦质心序列 @10fps × 12s
        fps, T = 10.0, 1.6
        xs = [320 + 250 * math.sin(2 * math.pi * (i / fps) / T)
              for i in range(120)]
        est = analyzer._estimate_period(xs, fps)
        assert est == pytest.approx(T, rel=0.08)

    def test_estimate_period_none_on_noise(self):
        xs = [320.0] * 120                      # 静止：无过零，无法估计
        assert analyzer._estimate_period(xs, 10.0) is None


def _equiv_frames() -> list[Image.Image]:
    """向量化对拍用多样帧：球/文字条/纯黑/边界亮度 59-60-61/奇数边长/随机噪声。"""
    return [
        _frame_with_ball(50, color=(200, 30, 30)),
        _frame_with_ball(200),
        _frame_with_ball(320, color=(162, 196, 12)),
        _frame_with_ball(590, color=(30, 30, 220)),
        _frame_with_text_bar(),
        Image.new("RGB", (640, 360), (8, 8, 8)),   # 纯黑（无亮像素）
        _uniform_frame(59),                        # 59 <60 边界外
        _uniform_frame(60),                        # 60 恰等阈值（>60 为 False）
        _uniform_frame(61),                        # 61 >60 边界内
        _frame_with_ball(101, size=(641, 361)),    # 奇数边长：subsampling 尾元素对齐
        _frame_with_text_bar(size=(641, 361)),
        _noise_frame(seed=1),
        _noise_frame(seed=2),
        _noise_frame(size=(201, 121), seed=3),
    ]


class TestVectorizedEquivalence:
    """向量化实现 vs 旧纯 Python 参照逐值对拍（契约护栏：新旧必须逐帧完全一致）。"""

    @pytest.mark.parametrize("img", _equiv_frames())
    def test_centroid_x_matches_ref(self, img):
        assert analyzer._centroid_x(img) == _centroid_x_ref(img)

    @pytest.mark.parametrize("img", _equiv_frames())
    def test_frame_features_matches_ref(self, img):
        assert analyzer._frame_features(img) == _frame_features_ref(img)

    @pytest.mark.parametrize("img", _equiv_frames())
    def test_mean_bright_color_matches_ref(self, img):
        assert analyzer._mean_bright_color(img) == _mean_bright_color_ref(img)

    def test_luma_boundary_semantics(self):
        # luma>60 严格判定：59/60 无亮像素→None/(0,0.0)；61 全亮
        assert analyzer._centroid_x(_uniform_frame(60)) is None
        assert analyzer._frame_features(_uniform_frame(60)) == (0, 0.0)
        assert analyzer._mean_bright_color(_uniform_frame(60)) is None
        assert analyzer._centroid_x(_uniform_frame(61)) is not None


class TestPixelSegmentation:
    """像素采样分割兜底（黑底渐变内容帧差失明时启用）。"""

    def test_frame_features_ball_is_narrow(self):
        count, ratio = analyzer._frame_features(_frame_with_ball(320))
        assert count > 100                         # 球有可观亮像素
        assert ratio < 0.2                         # 但亮区窄
        assert analyzer._pixel_kind(count, ratio) == "ball_like"

    def test_frame_features_text_bar_is_wide(self):
        count, ratio = analyzer._frame_features(_frame_with_text_bar())
        assert ratio > 0.2                         # 文字条亮区宽
        assert analyzer._pixel_kind(count, ratio) == "content_like"

    def test_frame_features_black_is_empty(self):
        black = Image.new("RGB", (640, 360), (8, 8, 8))
        count, ratio = analyzer._frame_features(black)
        assert (count, ratio) == (0, 0.0)
        assert analyzer._pixel_kind(count, ratio) == "empty"

    def test_pixel_kind_thresholds(self):
        assert analyzer._pixel_kind(50, 0.0) == "empty"        # 亮像素太少
        assert analyzer._pixel_kind(4000, 0.07) == "ball_like"  # 窄且少 = 球
        assert analyzer._pixel_kind(50000, 0.7) == "content_like"  # 宽 = 卡
        assert analyzer._pixel_kind(4000, 0.7) == "content_like"  # 宽即便亮像素少也非球

    @pytest.mark.asyncio
    async def test_sample_segments_splits_card_and_ball(self, monkeypatch, tmp_path):
        # 打桩抽帧：t<40 文字卡，t>=40 小球 → 应切两段且边界经细化落在 40±1
        def _render(t, out_jpg):
            (_frame_with_ball(320) if t >= 40 else _frame_with_text_bar()).save(out_jpg)
        async def fake_extract(video, t, out_jpg, *, timeout=60.0):
            _render(t, out_jpg)                       # 边界二分细化仍走单帧
            return out_jpg
        async def fake_batch(video, times, out_dir, *, deadline=None, timeout=120.0):
            mapping = {}
            for i, t in enumerate(sorted(set(times))):
                p = out_dir / f"f_{i:05d}.jpg"
                _render(t, p)
                mapping[t] = p
            return mapping
        monkeypatch.setattr(analyzer, "_extract_frame", fake_extract)
        monkeypatch.setattr(analyzer, "_extract_frames_batch", fake_batch)
        segs = await analyzer._sample_segments(tmp_path / "v.mp4", 80.0, None)
        assert [s["pixel_kind"] for s in segs] == ["content_like", "ball_like"]
        assert segs[0]["t1"] == pytest.approx(40, abs=1.0)   # 边界细化
        assert segs[1]["t0"] == pytest.approx(40, abs=1.0)
        assert segs[0]["t0"] == 0.0                          # 铺满 [0,80]
        assert segs[-1]["t1"] == 80.0


class TestAnalyzeOrchestration:
    @pytest.mark.asyncio
    async def test_analyze_pixel_fallback_when_cuts_sparse(self, monkeypatch, tmp_path):
        # 黑底渐变：0 切点 + 长视频 → 触发像素采样分割兜底；ball_like 段不经 VL
        called = {"vl": [], "sample": 0}

        async def fake_cuts(video, duration, deadline):
            return []
        async def fake_sample(video, duration, deadline):
            called["sample"] += 1
            return [{"t0": 0.0, "t1": 300.0, "pixel_kind": "content_like"},
                    {"t0": 300.0, "t1": 600.0, "pixel_kind": "ball_like"}]
        async def fake_phases(video, t0, t1, deadline):
            return [{"t0": t0, "t1": t1, "ball_color_hex": "#FFFFFF", "period_s": 1.5}]
        async def fake_classify(video, t, deadline):
            called["vl"].append(t)
            return {"kind": "text_card", "text": "hello"}
        monkeypatch.setattr(analyzer, "_detect_cuts", fake_cuts)
        monkeypatch.setattr(analyzer, "_sample_segments", fake_sample)
        monkeypatch.setattr(analyzer, "_ball_phases", fake_phases)
        monkeypatch.setattr(analyzer, "_classify_scene", fake_classify)
        facts = await analyzer.analyze(tmp_path / "v.mp4", 600.0)
        assert called["sample"] == 1                         # 走了像素采样分割
        assert [s["kind"] for s in facts["scenes"]] == ["text_card", "ball_exercise"]
        # 300s content 段 >45s 触发文本采样细分：VL 只落在 content 段内、绝不进球段
        assert called["vl"], "content 段应至少采样一次 VL"
        assert all(0 <= t < 300 for t in called["vl"])
        assert any("像素采样分割" in w for w in facts["warnings"])

    @pytest.mark.asyncio
    async def test_analyze_no_fallback_when_cuts_sufficient(self, monkeypatch, tmp_path):
        # 长视频但切点充足 → 走旧路径，不触发像素采样分割
        async def fake_cuts(video, duration, deadline):
            return [60.0 * i for i in range(1, 10)]          # 9 切点 → 10 段
        async def fake_classify(video, t, deadline):
            return {"kind": "title_card", "text": ""}
        async def boom_sample(video, duration, deadline):
            raise AssertionError("切点充足时不应触发像素采样分割")
        monkeypatch.setattr(analyzer, "_detect_cuts", fake_cuts)
        monkeypatch.setattr(analyzer, "_classify_scene", fake_classify)
        monkeypatch.setattr(analyzer, "_sample_segments", boom_sample)
        facts = await analyzer.analyze(tmp_path / "v.mp4", 600.0)
        assert len(facts["scenes"]) == 10
        assert not any("像素采样分割" in w for w in facts["warnings"])

    @pytest.mark.asyncio
    async def test_analyze_classifies_and_measures(self, monkeypatch, tmp_path):
        # 打桩：切点 → [10.0]；两场景关键帧 VL 分类 → 卡片 + 球；球段测色/测周期打桩
        async def fake_cuts(video, duration, deadline):
            return [10.0]
        async def fake_classify(video, t, deadline):
            return ({"kind": "title_card", "text": "introduction"} if t < 10
                    else {"kind": "ball_exercise", "text": ""})
        async def fake_phases(video, t0, t1, deadline):
            return [{"t0": t0, "t1": t1, "ball_color_hex": "#FFFFFF",
                     "period_s": 1.5}]
        monkeypatch.setattr(analyzer, "_detect_cuts", fake_cuts)
        monkeypatch.setattr(analyzer, "_classify_scene", fake_classify)
        monkeypatch.setattr(analyzer, "_ball_phases", fake_phases)
        facts = await analyzer.analyze(tmp_path / "v.mp4", 20.0)
        kinds = [s["kind"] for s in facts["scenes"]]
        assert kinds == ["title_card", "ball_exercise"]
        assert facts["scenes"][1]["period_s"] == 1.5
        assert facts["scenes"][0]["t1"] == 10.0

    @pytest.mark.asyncio
    async def test_analyze_merges_tiny_scene(self, monkeypatch, tmp_path):
        # 碎场景合并：cuts 打桩产生 0.5s(<1s) 碎片 → 并入前段，不单独成场景
        async def fake_cuts(video, duration, deadline):
            return [10.0, 10.5]
        async def fake_classify(video, t, deadline):
            return {"kind": "title_card", "text": ""}
        monkeypatch.setattr(analyzer, "_detect_cuts", fake_cuts)
        monkeypatch.setattr(analyzer, "_classify_scene", fake_classify)
        facts = await analyzer.analyze(tmp_path / "v.mp4", 20.0)
        assert len(facts["scenes"]) == 2          # 3 段中 0.5s 碎片被并入前段
        assert facts["scenes"][0]["t1"] == 10.5   # 首段吞掉碎片延伸到 10.5


class TestBallStaticDetection:
    """球段静止检测（wave2 问题③：白球段实为 EMDR 组间休息，质心 0 位移）。"""

    @pytest.mark.asyncio
    async def test_ball_phases_marks_static_rest(self, monkeypatch, tmp_path):
        # 抽帧恒返回居中静止球 → 质心极差 0 → 该 phase 标 static、不带 period_s
        async def fake_batch(video, times, out_dir, *, deadline=None, timeout=120.0):
            mapping = {}
            for i, t in enumerate(sorted(set(times))):
                p = out_dir / f"f_{i:05d}.jpg"
                _frame_with_ball(320, color=(255, 255, 255)).save(p)
                mapping[t] = p
            return mapping
        monkeypatch.setattr(analyzer, "_extract_frames_batch", fake_batch)
        phases = await analyzer._ball_phases(tmp_path / "v.mp4", 0.0, 10.0, None)
        assert len(phases) == 1
        assert phases[0].get("static") is True
        assert "period_s" not in phases[0]

    @pytest.mark.asyncio
    async def test_ball_phases_measures_moving_period(self, monkeypatch, tmp_path):
        # 抽帧返回按已知周期摆动的球 → 测得周期且标 period_estimated=True
        T = 2.0

        async def fake_batch(video, times, out_dir, *, deadline=None, timeout=120.0):
            mapping = {}
            for i, t in enumerate(sorted(set(times))):
                x = int(320 + 250 * math.sin(2 * math.pi * t / T))
                p = out_dir / f"f_{i:05d}.jpg"
                _frame_with_ball(x, color=(255, 255, 255)).save(p)
                mapping[t] = p
            return mapping
        monkeypatch.setattr(analyzer, "_extract_frames_batch", fake_batch)
        phases = await analyzer._ball_phases(tmp_path / "v.mp4", 0.0, 12.0, None)
        assert phases[0].get("period_estimated") is True
        assert phases[0]["period_s"] == pytest.approx(T, rel=0.15)
        assert not phases[0].get("static")

    def test_scene_from_static_phase_no_warning(self):
        warnings: list[str] = []
        scene = analyzer._ball_scene_from_phase(
            {"t0": 0.0, "t1": 4.0, "ball_color_hex": "#FFFFFF", "static": True},
            warnings)
        assert scene["static"] is True
        assert "period_s" not in scene
        assert warnings == []                       # 静止段不记「周期实测失败」

    def test_scene_from_fallback_phase_warns(self):
        warnings: list[str] = []
        scene = analyzer._ball_scene_from_phase(
            {"t0": 5.0, "t1": 9.0, "ball_color_hex": "#FFFFFF",
             "period_s": 1.6, "period_estimated": False}, warnings)
        assert scene["period_estimated"] is False
        assert any("周期实测失败" in w for w in warnings)

    def test_scene_from_measured_phase_no_warning(self):
        warnings: list[str] = []
        scene = analyzer._ball_scene_from_phase(
            {"t0": 5.0, "t1": 9.0, "ball_color_hex": "#FFFFFF",
             "period_s": 2.5, "period_estimated": True}, warnings)
        assert scene["period_estimated"] is True
        assert scene["period_s"] == 2.5
        assert warnings == []


class TestContentSubdivision:
    """长 content 段文本采样细分（wave2 问题①：开场多卡被并成 199s 空段）。"""

    @pytest.mark.asyncio
    async def test_splits_on_text_change(self, monkeypatch, tmp_path):
        # 0-40s 卡A(text_card)、40-100s 卡B(title_card) → 细分两段，边界≈40
        async def fake_classify(video, t, deadline):
            if t < 40:
                return {"kind": "text_card", "text": "第一张卡 免责声明"}
            return {"kind": "title_card", "text": "第二张 引言"}
        monkeypatch.setattr(analyzer, "_classify_scene", fake_classify)
        warnings: list[str] = []
        subs = await analyzer._subdivide_content_segment(
            tmp_path / "v.mp4", 0.0, 100.0, None, warnings)
        assert [s["kind"] for s in subs] == ["text_card", "title_card"]
        assert subs[0]["t1"] == pytest.approx(40, abs=3)      # 边界二分细化
        assert subs[0]["t0"] == 0.0 and subs[-1]["t1"] == 100.0

    @pytest.mark.asyncio
    async def test_merges_similar_text_no_false_split(self, monkeypatch, tmp_path):
        # OCR 抖动：同卡文字微差（规范化后不等但 ratio>0.7）→ 不误切
        texts = {0: "第一段引导语请保持放松",
                 20: "第一段引导语请保持放松平稳",
                 40: "第一段引导语请保持放松均匀"}

        async def fake_classify(video, t, deadline):
            key = min(texts, key=lambda k: abs(k - t))
            return {"kind": "text_card", "text": texts[key]}
        monkeypatch.setattr(analyzer, "_classify_scene", fake_classify)
        warnings: list[str] = []
        subs = await analyzer._subdivide_content_segment(
            tmp_path / "v.mp4", 0.0, 60.0, None, warnings)
        assert len(subs) == 1                       # 抖动不误切成多段
        assert subs[0]["kind"] == "text_card"

    def test_same_card_ratio_gate(self):
        # 规范化相等 → 同卡；ratio>0.7 → 同卡；kind 不同 → 异卡
        assert analyzer._same_card("text_card", "abc", "text_card", "abc")
        assert analyzer._same_card(
            "text_card", "第一段引导语请保持放松",
            "text_card", "第一段引导语请保持放松平稳")
        assert not analyzer._same_card("text_card", "abc", "title_card", "abc")
        assert not analyzer._same_card("text_card", "卡0", "text_card", "卡1")

    @pytest.mark.asyncio
    async def test_vl_budget_cap(self, monkeypatch, tmp_path):
        # 超长段 + 每 20s 换卡 → VL 调用撞 30 次封顶，记一条 warning
        calls = {"n": 0}

        async def fake_classify(video, t, deadline):
            calls["n"] += 1
            return {"kind": "text_card", "text": f"卡{int(t) // 20 % 2}"}
        monkeypatch.setattr(analyzer, "_classify_scene", fake_classify)
        warnings: list[str] = []
        subs = await analyzer._subdivide_content_segment(
            tmp_path / "v.mp4", 0.0, 2000.0, None, warnings)
        assert calls["n"] <= 30                     # VL 预算封顶
        assert subs[0]["t0"] == 0.0 and subs[-1]["t1"] == 2000.0
        assert any("预算" in w for w in warnings)


class TestFrameBatchGrouping:
    """批量抽帧分组/采样率纯函数单测。"""

    def test_group_times_splits_on_span(self):
        # 90-0 跨度 >60 → 另起一组
        assert analyzer._group_times([0.0, 10.0, 20.0, 90.0, 100.0], 60.0) == \
            [[0.0, 10.0, 20.0], [90.0, 100.0]]

    def test_group_times_single_and_empty(self):
        assert analyzer._group_times([5.0], 60.0) == [[5.0]]
        assert analyzer._group_times([], 60.0) == []

    def test_group_times_exact_span_boundary(self):
        # 恰好 =max_span 仍归同组（≤ 边界）
        assert analyzer._group_times([0.0, 60.0], 60.0) == [[0.0, 60.0]]
        assert analyzer._group_times([0.0, 60.1], 60.0) == [[0.0], [60.1]]

    def test_group_rate_uniform_grids(self):
        # 均匀 0.1s → 10fps；5s → 0.2fps（周期/颜色两类真实网格）
        assert analyzer._group_rate([0.0, 0.1, 0.2, 0.3]) == pytest.approx(10.0, rel=1e-3)
        assert analyzer._group_rate([0.0, 5.0, 10.0]) == pytest.approx(0.2, rel=1e-3)

    def test_group_rate_caps_and_single(self):
        assert analyzer._group_rate([0.0, 0.001]) == 30.0     # 间隔极小封顶源帧率
        assert analyzer._group_rate([3.0]) == analyzer._PERIOD_FPS   # 单帧兜底


@pytest.fixture(scope="module")
def lavfi_clip(tmp_path_factory):
    """合成 30s 测试片（testsrc 逐帧不同），供批量抽帧真 ffmpeg 冒烟。"""
    import subprocess
    d = tmp_path_factory.mktemp("batchclip")
    out = d / "clip.mp4"
    subprocess.run(
        ["ffmpeg", "-y", "-v", "quiet", "-f", "lavfi",
         "-i", "testsrc=size=320x240:rate=30:duration=30",
         "-c:v", "libx264", "-pix_fmt", "yuv420p", str(out)], check=True)
    return out


@pytest.mark.integration
@pytest.mark.slow
class TestFrameBatchExtraction:
    """批量抽帧原语真 ffmpeg 冒烟：帧数/时间映射精度/与旧逐帧原语像素一致性。

    原语面向调用方的均匀采样网格（周期 10fps、颜色/像素 5s）。testsrc 逐帧剧变，
    ±1 帧即可见明显灰度差，故 <3 阈值是对「落到同一帧」的严格校验。
    """

    @staticmethod
    def _mean_gray_diff(a, b) -> float:
        from PIL import ImageChops
        ia, ib = Image.open(a).convert("L"), Image.open(b).convert("L")
        d = ImageChops.difference(ia, ib)
        return sum(d.getdata()) / (ia.width * ia.height)

    async def _assert_within_1_frame(self, clip, batch_frame, t, tmp_path):
        """批量帧应与旧法在 t 或 t±1帧(1/30s) 处像素一致（±1 帧映射精度契约）。"""
        best = 999.0
        for dt in (0.0, -1 / 30.0, 1 / 30.0):
            old = tmp_path / f"cmp_{t:.4f}_{dt:.4f}.jpg"
            await analyzer._extract_frame(clip, max(0.0, t + dt), old)
            best = min(best, self._mean_gray_diff(batch_frame, old))
        assert best < 1.0, f"t={t:.2f} 批量帧偏离旧法 >1 帧（最小灰度差 {best:.2f}）"

    @pytest.mark.asyncio
    async def test_count_and_all_times_mapped(self, lavfi_clip, tmp_path):
        # 密集 10fps 20 帧（单组）→ 每个请求时刻都有落盘帧
        times = [1.0 + i * 0.1 for i in range(20)]
        frames = await analyzer._extract_frames_batch(lavfi_clip, times, tmp_path)
        assert set(frames) == set(times)
        assert all(p.exists() for p in frames.values())

    @pytest.mark.asyncio
    async def test_dense_grid_pixel_matches_old(self, lavfi_clip, tmp_path):
        # 周期式密集网格（10fps）：桶中心对齐后与旧法落同一帧（±1 帧内）
        times = [5.0 + i * 0.1 for i in range(12)]
        frames = await analyzer._extract_frames_batch(lavfi_clip, times, tmp_path)
        for t in times:
            await self._assert_within_1_frame(lavfi_clip, frames[t], t, tmp_path)

    @pytest.mark.asyncio
    async def test_sparse_grid_pixel_matches_old(self, lavfi_clip, tmp_path):
        # 颜色/像素式稀疏网格（5s）：粗网格下桶中心对齐仍落同一帧（±1 帧内）
        times = [5.0 + i * 5.0 for i in range(4)]       # 5,10,15,20
        frames = await analyzer._extract_frames_batch(lavfi_clip, times, tmp_path)
        for t in times:
            await self._assert_within_1_frame(lavfi_clip, frames[t], t, tmp_path)

    @pytest.mark.asyncio
    async def test_multigroup_maps_all(self, lavfi_clip, tmp_path, monkeypatch):
        # 收紧跨度上限强制多组 → 跨组映射仍每帧命中且与旧法一致
        monkeypatch.setattr(analyzer, "_BATCH_GROUP_SPAN_S", 8.0)
        times = [5.0, 10.0, 15.0, 20.0, 25.0]           # 跨度20 → 多组
        frames = await analyzer._extract_frames_batch(lavfi_clip, times, tmp_path)
        assert set(frames) == set(times)
        for t in times:
            await self._assert_within_1_frame(lavfi_clip, frames[t], t, tmp_path)
