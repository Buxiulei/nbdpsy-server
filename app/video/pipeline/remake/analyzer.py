"""原片分镜分析（spec §4）：场景切分 + VL 分类 + 小球参数实测。

小球测量纯像素处理不走 VL：颜色阶段稀疏抽帧（5s 一帧）聚类切换点；
周期在每个颜色阶段开头 12s 窗口按 10fps 密集抽帧，质心 x 过零间隔估计。
"""
import asyncio
import difflib
import json
import logging
import re
import time
import uuid
from pathlib import Path

import numpy as np
from PIL import Image

from app.video.pipeline.muxer import _run_ffmpeg
from app.video.pipeline.remake import style
from app.video.providers import vl_describe

logger = logging.getLogger(__name__)

_SCENE_THRESHOLD = 0.3                # ffmpeg 场景切分阈值
_BRIGHT_LUMA = 60                     # 亮像素判定阈值（深底上的球/文字）
_PHASE_SAMPLE_S = 5.0                 # 颜色阶段稀疏采样间隔
_PERIOD_WINDOW_S = 12.0               # 周期密集采样窗口
_PERIOD_FPS = 10.0
_MIN_SCENE_S = 1.0                    # 低于该时长的碎场景并入前一场景

# 像素采样分割兜底（帧差切分对黑底渐变内容失明时启用）。
# 阈值出处：生产 job6（EMDR 1754s 全黑底）真实帧标定（1080p，luma>60、
# step-2 采样，与 _centroid_x 同参）：
#   文字/标题卡  bright_count 45446~51424，亮区 bbox 宽度占比 0.652~0.843
#   小球帧（全颜色阶段）bright_count 3949~4054，亮区 bbox 宽度占比 0.073~0.074
# 两类相差一个数量级，阈值边际巨大。
_PIXEL_EMPTY_MAX = 100                # 亮像素 < 该数 → empty（纯黑/渐隐过渡帧）
_PIXEL_BALL_BBOX_RATIO = 0.2         # 亮区 bbox 宽度占比 < 该值 → 窄幅（小球）
_PIXEL_BALL_COUNT_MAX = 15000        # 亮像素 < 该数 且窄幅 → ball_like
_PIXEL_SAMPLE_S = 5.0                 # 像素采样分割抽帧间隔
_PIXEL_REFINE_STEPS = 3              # 段边界二分细化步数（5s/2^3 ≈ 0.6s）

# 球段静止检测（wave2 问题③：白球段实为 EMDR 组间休息，质心 0 位移）。
_STATIC_MIN_SAMPLES = 8               # 质心样本数下限（不足无法判静止）
_STATIC_MAX_SPAN_PX = 15              # 密集采样窗口质心 x 极差 < 该值 → 静止休息段

# 长 content 段文本采样细分（wave2 问题①：开场免责声明+多章节卡+口播被并成一个
# 空段，像素采样只看中点帧判 other → 渲染成 199s 空白品牌卡）。
_CONTENT_SUBDIV_MIN_S = 45.0          # content 段长 > 该值才细分（短段仍用中点单判）
_CONTENT_SAMPLE_S = 20.0             # 细分采样间隔
_CONTENT_REFINE_STEPS = 3           # 变化边界二分步数（20s/2^3 ≈ 2.5s）
_CONTENT_VL_BUDGET = 30             # 单 content 段 VL 调用总数封顶
_CONTENT_SIM_RATIO = 0.7            # 规范化文本相似度判同阈值（防 OCR 抖动误切）

_CLASSIFY_PROMPT = (
    "这是一个视频的关键帧。请判断画面类型并输出 JSON（只输出 JSON）：\n"
    '{"kind": "title_card|text_card|ball_exercise|other", "text": "画面上的文字，无则空串"}\n'
    "title_card=章节标题卡；text_card=大段文字说明卡；"
    "ball_exercise=纯色背景上一个圆球（EMDR 双侧刺激练习画面）；其余为 other。")


async def _extract_frame(video: Path, t: float, out_jpg: Path, *,
                         timeout: float = 60.0) -> Path:
    await _run_ffmpeg(["-ss", f"{t}", "-i", str(video), "-frames:v", "1",
                       "-q:v", "3", str(out_jpg)], timeout=timeout)
    return out_jpg


_BATCH_GROUP_SPAN_S = 60.0            # 批量抽帧单组时间跨度上限（组内连续解码一次）


def _group_times(times: list[float], max_span: float) -> list[list[float]]:
    """升序时间点按邻近性贪心分组：组内跨度（末-首）≤ max_span。纯函数。

    每组对应一次 ffmpeg 连续解码——跨度封顶避免为几个远隔时刻解码超长区间。
    """
    groups: list[list[float]] = []
    for t in times:
        if groups and t - groups[-1][0] <= max_span:
            groups[-1].append(t)
        else:
            groups.append([t])
    return groups


def _group_rate(group: list[float]) -> float:
    """组内目标时刻 → fps 网格采样率：取 1/最小间隔，使每个时刻落到不同网格帧。

    均匀网格（周期 10fps、颜色/像素 5s）恰好一帧一时刻、零浪费；上限 30（源帧率）
    避免间隔过小时网格过密。单帧组用 _PERIOD_FPS 兜底（跨度≈0，取值不影响结果）。
    """
    if len(group) < 2:
        return _PERIOD_FPS
    gaps = [b - a for a, b in zip(group, group[1:]) if b - a > 1e-6]
    if not gaps:
        return _PERIOD_FPS
    return min(1.0 / min(gaps), 30.0)


async def _extract_frames_batch(video: Path, times: list[float], out_dir: Path, *,
                                deadline: float | None = None,
                                timeout: float = 120.0) -> dict[float, Path]:
    """批量抽帧原语：均匀采样时刻排序按邻近性分组，每组单次 ffmpeg 连续解码出帧。

    替代逐帧独立起进程（每帧冷启+seek ~130ms）——单组内以 `-ss 起点 -t 跨度`
    连续解码 + `fps=网格率` 滤镜一次出全组帧（dav1d 解码 ~1ms/帧）。fps 滤镜在每个
    1/率 桶的中心 (k+0.5)/率 出帧，故 seek 前移半个桶使桶中心恰好落在请求时刻上
    （均匀网格映射精度 ±1 帧）。网格率取 1/最小间隔——调用方均按固定步长采样
    （周期 10fps、颜色/像素 5s），恰好一帧一时刻。返回 {请求时刻: 帧文件路径}；
    越界回退最近可得帧。out_dir 由调用方创建并清理。
    """
    result: dict[float, Path] = {}
    uniq = sorted(set(times))
    if not uniq:
        return result
    token = uuid.uuid4().hex[:8]                 # 同一 out_dir 多次调用防前缀撞名
    for gi, group in enumerate(_group_times(uniq, _BATCH_GROUP_SPAN_S)):
        if deadline is not None and time.monotonic() > deadline:
            raise TimeoutError("analyze 阶段预算耗尽")
        rate = _group_rate(group)
        half = 0.5 / rate                        # 半个桶：seek 前移使桶中心对齐请求时刻
        seek = max(0.0, group[0] - half)
        span = (group[-1] - seek) + 1.0 / rate + 0.05
        prefix = f"{token}_g{gi}_"
        await _run_ffmpeg(
            ["-ss", f"{seek}", "-i", str(video), "-t", f"{span}",
             "-vf", f"fps={rate}", "-q:v", "3", str(out_dir / f"{prefix}%05d.jpg")],
            timeout=timeout)
        files = sorted(out_dir.glob(f"{prefix}*.jpg"))
        if not files:
            continue
        for t in group:
            k = round((t - seek) * rate - 0.5)   # 最近的 fps 输出桶（桶中心 (k+0.5)/率）
            result[t] = files[min(max(k, 0), len(files) - 1)]
    return result


def _centroid_x(img: Image.Image) -> float | None:
    """亮像素质心 x；无亮像素（纯深底）返回 None。"""
    # 隔行隔列 step-2 采样后向量化：subsampled 列下标 j 对应原图 x=2j，
    # 逐值对齐旧双层 Python 循环（x 用原图坐标系累加，total>20 才返回）。
    arr = np.asarray(img.convert("L"))[::2, ::2]
    mask = arr > _BRIGHT_LUMA
    total = int(mask.sum())
    if total <= 20:
        return None
    col_counts = mask.sum(axis=0).astype(np.int64)          # 每列亮像素数
    xs = np.arange(mask.shape[1], dtype=np.int64) * 2        # 还原原图 x 坐标
    sx = int((col_counts * xs).sum())
    return sx / total


def _frame_features(img: Image.Image) -> tuple[int, float]:
    """帧亮区特征：亮像素计数 + 亮区 bbox 宽度占比（无亮像素 → (0, 0.0)）。

    与 _centroid_x 同参（luma>_BRIGHT_LUMA、隔行隔列 step-2 采样）。宽度占比
    用于区分「宽幅文字/标题卡」与「窄幅小球」——标定数据见模块常量注释。
    """
    gray = img.convert("L")
    w = gray.width                                          # 原图宽度（占比分母）
    arr = np.asarray(gray)[::2, ::2]
    mask = arr > _BRIGHT_LUMA
    count = int(mask.sum())
    if count == 0:
        return 0, 0.0
    cols = np.flatnonzero(mask.any(axis=0))                 # 含亮像素的 subsampled 列（升序）
    min_x, max_x = int(cols[0]) * 2, int(cols[-1]) * 2      # 还原原图 x 坐标
    return count, (max_x - min_x) / w


def _pixel_kind(bright_count: int, bbox_w_ratio: float) -> str:
    """亮区特征 → 像素级画面类：empty / ball_like / content_like。

    empty 兜掉纯黑/渐隐过渡帧；ball_like=窄幅且亮像素少；其余 content_like。
    阈值出处见模块常量注释（生产 job6 真实帧标定）。
    """
    if bright_count < _PIXEL_EMPTY_MAX:
        return "empty"
    if bbox_w_ratio < _PIXEL_BALL_BBOX_RATIO and bright_count < _PIXEL_BALL_COUNT_MAX:
        return "ball_like"
    return "content_like"


def _mean_bright_color(img: Image.Image) -> str | None:
    """亮像素平均色 hex（球色检测）；无亮像素返回 None。"""
    # 与 _centroid_x 同参采样；亮判走 L 通道，取色走 RGB 通道，n>20 才返回。
    gray = np.asarray(img.convert("L"))[::2, ::2]
    rgb = np.asarray(img.convert("RGB"))[::2, ::2]
    mask = gray > _BRIGHT_LUMA
    n = int(mask.sum())
    if n <= 20:
        return None
    bright = rgb[mask]                                      # (n,3) 亮像素 RGB
    rs, gs, bs = (int(bright[:, i].sum()) for i in range(3))
    return "#{:02X}{:02X}{:02X}".format(rs // n, gs // n, bs // n)


def _estimate_period(xs: list[float], fps: float) -> float | None:
    """质心 x 序列过零间隔估计摆动周期；序列无摆动返回 None。

    过零 = (x - 均值) 变号点；周期 = 2 × 相邻过零平均间隔。
    """
    if len(xs) < 8:
        return None
    mean = sum(xs) / len(xs)
    centered = [x - mean for x in xs]
    if max(abs(c) for c in centered) < 5:      # 摆幅小于 5px 视为静止
        return None
    crossings = [i for i in range(1, len(centered))
                 if centered[i - 1] * centered[i] < 0]
    if len(crossings) < 3:
        return None
    gaps = [(b - a) / fps for a, b in zip(crossings, crossings[1:])]
    return 2 * sum(gaps) / len(gaps)


async def _detect_cuts(video: Path, duration: float,
                       deadline: float | None) -> list[float]:
    """ffmpeg 场景切分：select+showinfo 从 stderr 抓 pts_time。"""
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg", "-i", str(video),
        "-vf", f"select='gt(scene,{_SCENE_THRESHOLD})',showinfo",
        "-f", "null", "-",
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
    timeout = 600.0
    if deadline is not None:
        timeout = max(60.0, min(timeout, deadline - time.monotonic()))
    try:
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        raise
    cuts = [float(m.group(1)) for m in
            re.finditer(r"pts_time:([\d.]+)", stderr.decode(errors="replace"))]
    return sorted(t for t in cuts if 0 < t < duration)


async def _classify_scene(video: Path, t: float,
                          deadline: float | None) -> dict:
    """关键帧 VL 分类；失败/解析不出返回 other（storyboard 降级卡兜底）。"""
    import tempfile
    with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as f:
        jpg = Path(f.name)
    try:
        await _extract_frame(video, t, jpg)
        # 换 import 面：源 get_vision(key).describe(...).description → 薄 provider vl_describe
        # （直接返回描述文本；失败抛原异常，由本函数 except 兜底降级 other）。
        text = await vl_describe(str(jpg), _CLASSIFY_PROMPT)
        start, end = text.find("{"), text.rfind("}")
        parsed = json.loads(text[start:end + 1]) if start >= 0 else {}
        kind = parsed.get("kind")
        if kind not in ("title_card", "text_card", "ball_exercise", "other"):
            kind = "other"
        return {"kind": kind, "text": (parsed.get("text") or "").strip()}
    except Exception as exc:
        logger.warning("VL 分类失败(t=%.1f)，按 other 降级: %s", t, exc)
        return {"kind": "other", "text": ""}
    finally:
        jpg.unlink(missing_ok=True)


async def _ball_phases(video: Path, t0: float, t1: float,
                       deadline: float | None) -> list[dict]:
    """球段内按颜色切分阶段并逐阶段测周期。

    稀疏抽帧（5s）测球色 → 相邻同色合并成阶段；每阶段开头 12s 窗口
    密集抽帧（10fps）测周期，失败回退 DEFAULT_PERIOD_S。
    """
    import shutil
    import tempfile
    work = Path(tempfile.mkdtemp(prefix="ballph_"))
    try:
        # 颜色稀疏采样（5s）测球色——批量抽帧一次连续解码
        color_times: list[float] = []
        t = t0
        while t < t1:
            color_times.append(t)
            t += _PHASE_SAMPLE_S
        frames = await _extract_frames_batch(video, color_times, work, deadline=deadline)

        # 【非阻塞红线】PIL 解码 + numpy 亮像素向量化是同步 CPU 段——整批下沉线程，不占事件循环。
        def _color_samples() -> list[tuple[float, str | None]]:
            return [(t, _mean_bright_color(Image.open(frames[t])))
                    for t in color_times if t in frames]
        samples: list[tuple[float, str | None]] = await asyncio.to_thread(_color_samples)
        for p in set(frames.values()):
            p.unlink(missing_ok=True)                # 及时释放，控制峰值文件数

        # 相邻同参考色合并为阶段（映射到 white/green/red 参考名后比较）
        def _ref(hexc: str | None) -> str:
            if not hexc:
                return "white"
            h = hexc.lstrip("#")
            rgb = tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))
            return min(style.ORIG_BALL_REFS,
                       key=lambda n: sum((a - b) ** 2 for a, b in
                                         zip(rgb, style.ORIG_BALL_REFS[n])))

        phases: list[dict] = []
        for ts, hexc in samples:
            name = _ref(hexc)
            if phases and phases[-1]["_ref"] == name:
                phases[-1]["t1"] = min(ts + _PHASE_SAMPLE_S, t1)
            else:
                phases.append({"t0": ts, "t1": min(ts + _PHASE_SAMPLE_S, t1),
                               "_ref": name, "ball_color_hex": hexc or "#FFFFFF"})
        if phases:
            phases[0]["t0"] = t0
            phases[-1]["t1"] = t1

        # 逐阶段测周期（先判静止：EMDR 组间休息段质心 0 位移，非「实测失败」）
        for ph in phases:
            if deadline is not None and time.monotonic() > deadline:
                raise TimeoutError("analyze 阶段预算耗尽")
            window_end = min(ph["t0"] + _PERIOD_WINDOW_S, ph["t1"])
            n = int((window_end - ph["t0"]) * _PERIOD_FPS)
            # 密集周期采样（10fps，120 帧一组）批量抽帧——单窗口一次连续解码
            period_times = [ph["t0"] + i / _PERIOD_FPS for i in range(n)]
            pframes = await _extract_frames_batch(video, period_times, work,
                                                  deadline=deadline)

            # 【非阻塞红线】质心 x 的 PIL 解码 + numpy 向量化整批下沉线程，不占事件循环。
            def _centroids() -> list[float]:
                out: list[float] = []
                for t in period_times:
                    if t not in pframes:
                        continue
                    cx = _centroid_x(Image.open(pframes[t]))
                    if cx is not None:
                        out.append(cx)
                return out
            xs: list[float] = await asyncio.to_thread(_centroids)
            for p in set(pframes.values()):
                p.unlink(missing_ok=True)
            if len(xs) >= _STATIC_MIN_SAMPLES and (max(xs) - min(xs)) < _STATIC_MAX_SPAN_PX:
                ph["static"] = True                 # 静止休息段：不设 period_s、不记 warning
            else:
                period = _estimate_period(xs, _PERIOD_FPS)
                ph["period_s"] = period if period is not None else style.DEFAULT_PERIOD_S
                ph["period_estimated"] = period is not None
            ph.pop("_ref", None)
        return phases
    finally:
        shutil.rmtree(work, ignore_errors=True)


def _ball_scene_from_phase(ph: dict, warnings: list[str]) -> dict:
    """球 phase → ball_exercise 场景 dict（向后兼容新增可选字段 static/period_estimated）。

    静止 phase（EMDR 组间休息）不带 period_s、不记 warning；回退默认周期的记一条
    warning（period_estimated 显式为 False 时）。
    """
    scene = {"t0": ph["t0"], "t1": ph["t1"], "kind": "ball_exercise",
             "ball_color_hex": ph.get("ball_color_hex", "#FFFFFF")}
    if ph.get("static"):
        scene["static"] = True
        return scene
    scene["period_s"] = ph["period_s"]
    scene["period_estimated"] = bool(ph.get("period_estimated"))
    if ph.get("period_estimated") is False:
        warnings.append(f"球段[{ph['t0']:.0f}s]周期实测失败，回退默认")
    return scene


async def _pixel_kind_at(video: Path, t: float) -> str:
    """抽单帧算像素级画面类（empty/ball_like/content_like）。"""
    import tempfile
    with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as f:
        jpg = Path(f.name)
    try:
        await _extract_frame(video, t, jpg)
        # 【非阻塞红线】PIL 解码 + 亮区特征 numpy 段下沉线程。
        return await asyncio.to_thread(
            lambda: _pixel_kind(*_frame_features(Image.open(jpg))))
    finally:
        jpg.unlink(missing_ok=True)


async def _refine_boundary(video: Path, lo: float, hi: float,
                           lo_kind: str, deadline: float | None) -> float:
    """相邻异类样本间二分 _PIXEL_REFINE_STEPS 步收敛段边界，返回区间中点。

    mid 帧判为 lo_kind 则边界右移，否则（含 empty 过渡帧）左移。
    """
    for _ in range(_PIXEL_REFINE_STEPS):
        if deadline is not None and time.monotonic() > deadline:
            raise TimeoutError("analyze 阶段预算耗尽")
        mid = (lo + hi) / 2
        if await _pixel_kind_at(video, mid) == lo_kind:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


async def _sample_segments(video: Path, duration: float,
                           deadline: float | None) -> list[dict]:
    """像素采样分割（帧差切分失效时的兜底，spec RCA）。

    每 5s 抽帧判像素类 → 相邻同类合并 → empty 并入邻段 → 段边界二分细化到
    ~0.6s → 输出恰好铺满 [0, duration] 的 [{"t0","t1","pixel_kind"}]。
    """
    # 1. 稀疏抽帧判类（5s）——批量抽帧一次连续解码
    import shutil
    import tempfile
    ts: list[float] = []
    t = 0.0
    while t < duration:
        ts.append(t)
        t += _PIXEL_SAMPLE_S
    work = Path(tempfile.mkdtemp(prefix="pixseg_"))
    try:
        frames = await _extract_frames_batch(video, ts, work, deadline=deadline)

        # 【非阻塞红线】逐帧像素类判定的 PIL 解码 + numpy 段整批下沉线程。
        def _kinds() -> list[str]:
            return [_pixel_kind(*_frame_features(Image.open(frames[t]))) if t in frames
                    else "empty" for t in ts]
        kinds: list[str] = await asyncio.to_thread(_kinds)
    finally:
        shutil.rmtree(work, ignore_errors=True)

    # 2. empty 并入邻段：前向填充；打头的 empty 用首个非空类兜底
    first_solid = next((k for k in kinds if k != "empty"), "content_like")
    resolved: list[str] = []
    last = first_solid
    for k in kinds:
        if k != "empty":
            last = k
        resolved.append(last)

    # 3. 相邻同类合并为 run（记录首/尾样本索引）
    runs: list[tuple[str, int, int]] = []
    for i, k in enumerate(resolved):
        if runs and runs[-1][0] == k:
            runs[-1] = (k, runs[-1][1], i)
        else:
            runs.append((k, i, i))

    # 4. run 间边界二分细化 + 组装铺满 [0, duration] 的段
    segments: list[dict] = []
    prev = 0.0
    for idx, (kind, _first, last_i) in enumerate(runs):
        if idx < len(runs) - 1:
            next_first = runs[idx + 1][1]
            boundary = await _refine_boundary(
                video, ts[last_i], ts[next_first], kind, deadline)
        else:
            boundary = duration
        segments.append({"t0": prev, "t1": boundary, "pixel_kind": kind})
        prev = boundary
    return segments


def _normalize_card_text(text: str) -> str:
    """卡片文本规范化：去空白/标点/下划线 + lower，用于判定相邻采样是否同一张卡。"""
    return re.sub(r"[\s\W_]+", "", (text or "").lower())


def _same_card(kind_a: str, norm_a: str, kind_b: str, norm_b: str) -> bool:
    """相邻采样是否同一张卡：kind 相同且（规范化文本相等或 SequenceMatcher>阈值）。

    ratio 判同用于容忍 VL OCR 抖动（同一张卡多次识别文字略有出入），防止误切。
    """
    if kind_a != kind_b:
        return False
    if norm_a == norm_b:
        return True
    return difflib.SequenceMatcher(None, norm_a, norm_b).ratio() > _CONTENT_SIM_RATIO


async def _subdivide_content_segment(video: Path, a: float, b: float,
                                     deadline: float | None,
                                     warnings: list[str]) -> list[dict]:
    """长 content 段按 VL 文本采样细分（wave2 问题①）。

    每 20s 用 _classify_scene 采样 (kind, 文本)，(kind, 规范化文本) 变化处二分
    细化边界；VL 调用总数封顶 _CONTENT_VL_BUDGET，超限按已得边界收束并记 warning。
    返回恰好铺满 [a, b] 的 content 场景 [{"t0","t1","kind","text"}]。
    """
    def _kind_of(cls: dict) -> str:
        # 内容段不信 VL 判球（RCA 洞见⑤：球只由像素 ball_like 段产出）
        return "other" if cls["kind"] == "ball_exercise" else cls["kind"]

    budget = _CONTENT_VL_BUDGET
    samples: list[tuple[float, str, str, str]] = []   # (t, kind, 规范化文本, 原文)
    truncated = False
    t = a
    while t < b:
        if budget <= 0:
            truncated = True
            break
        cls = await _classify_scene(video, t, deadline)
        budget -= 1
        raw = cls.get("text", "") or ""
        samples.append((t, _kind_of(cls), _normalize_card_text(raw), raw))
        t += _CONTENT_SAMPLE_S

    if not samples:                                   # 预算即 0 的极端：退化中点单判
        cls = await _classify_scene(video, (a + b) / 2, deadline)
        return [{"t0": a, "t1": b, "kind": _kind_of(cls),
                 "text": cls.get("text", "") or ""}]

    # 相邻同卡合并为 run（变化处即分段边界）
    runs: list[list[int]] = [[0]]
    for idx in range(1, len(samples)):
        _, pk, pn, _ = samples[idx - 1]
        _, ck, cn, _ = samples[idx]
        if _same_card(pk, pn, ck, cn):
            runs[-1].append(idx)
        else:
            runs.append([idx])

    # 逐 run 组装 + 变化边界二分细化（消费同一 VL 预算）
    result: list[dict] = []
    prev_t = a
    for ri, run in enumerate(runs):
        _, kind, _, raw = samples[run[0]]
        if ri < len(runs) - 1:
            li, fi = run[-1], runs[ri + 1][0]
            lo, hi = samples[li][0], samples[fi][0]
            lk, ln = samples[li][1], samples[li][2]
            for _ in range(_CONTENT_REFINE_STEPS):
                if budget <= 0:
                    truncated = True
                    break
                if deadline is not None and time.monotonic() > deadline:
                    raise TimeoutError("analyze 阶段预算耗尽")
                mid = (lo + hi) / 2
                cls = await _classify_scene(video, mid, deadline)
                budget -= 1
                if _same_card(lk, ln, _kind_of(cls),
                              _normalize_card_text(cls.get("text", "") or "")):
                    lo = mid
                else:
                    hi = mid
            boundary = (lo + hi) / 2
        else:
            boundary = b
        result.append({"t0": prev_t, "t1": boundary, "kind": kind, "text": raw})
        prev_t = boundary

    if truncated:
        warnings.append(
            f"content 段[{a:.0f}s,{b:.0f}s] VL 采样超预算 {_CONTENT_VL_BUDGET} 次，按已得边界收束")
    return result


async def _scenes_from_pixel_segments(video: Path, duration: float,
                                      deadline: float | None,
                                      warnings: list[str]) -> list[dict]:
    """像素采样分割 → 场景：ball_like 段走 _ball_phases（不经 VL，RCA 洞见⑤：
    球段不应依赖 VL）；content_like 段走 _classify_scene 中点帧 VL 判 title/text/other。
    """
    scenes: list[dict] = []
    for seg in await _sample_segments(video, duration, deadline):
        a, b = seg["t0"], seg["t1"]
        if seg["pixel_kind"] == "ball_like":
            for ph in await _ball_phases(video, a, b, deadline):
                scenes.append(_ball_scene_from_phase(ph, warnings))
        else:
            # 长 content 段（开场多卡叠一段）走文本采样细分，短段仍用中点单判
            if b - a > _CONTENT_SUBDIV_MIN_S:
                subs = await _subdivide_content_segment(video, a, b, deadline, warnings)
            else:
                cls = await _classify_scene(video, (a + b) / 2, deadline)
                # RCA 洞见⑤：球只由像素 ball_like 段产出，内容段不信 VL 判球
                kind = "other" if cls["kind"] == "ball_exercise" else cls["kind"]
                subs = [{"t0": a, "t1": b, "kind": kind,
                         "text": cls.get("text", "") or ""}]
            for s in subs:
                if s["kind"] == "other":
                    warnings.append(
                        f"场景[{s['t0']:.0f}s,{s['t1']:.0f}s] VL 判为 other，将降级品牌底卡")
                scenes.append(s)
    return scenes


async def analyze(video_path: Path, duration: float, *,
                  deadline: float | None = None) -> dict:
    """原片 → 分镜事实 facts（spec §4 输出契约）。"""
    warnings: list[str] = []
    cuts = await _detect_cuts(Path(video_path), duration, deadline)
    bounds = [0.0] + cuts + [float(duration)]
    # 合并碎场景（切分抖动会产生 <1s 碎片）
    merged: list[tuple[float, float]] = []
    for a, b in zip(bounds, bounds[1:]):
        if merged and b - a < _MIN_SCENE_S:
            merged[-1] = (merged[-1][0], b)
        else:
            merged.append((a, b))

    # 长视频切点可疑地少 → 帧差分割对黑底渐变内容失明（RCA：黑底内容前景占比
    # 极小 + 卡片渐隐渐现无硬切，scene 均值差不过阈值）→ 改用像素采样分割兜底。
    if duration > 120 and len(merged) < max(2, duration / 300):
        warnings.append("场景切分过少(疑似黑底渐变内容)，启用像素采样分割")
        scenes = await _scenes_from_pixel_segments(
            Path(video_path), duration, deadline, warnings)
        return {"scenes": scenes, "warnings": warnings}

    scenes: list[dict] = []
    for a, b in merged:
        mid = (a + b) / 2
        cls = await _classify_scene(Path(video_path), mid, deadline)
        if cls["kind"] == "ball_exercise":
            for ph in await _ball_phases(Path(video_path), a, b, deadline):
                scenes.append(_ball_scene_from_phase(ph, warnings))
        else:
            if cls["kind"] == "other":
                warnings.append(f"场景[{a:.0f}s,{b:.0f}s] VL 判为 other，将降级品牌底卡")
            scenes.append({"t0": a, "t1": b, "kind": cls["kind"],
                           "text": cls["text"]})
    return {"scenes": scenes, "warnings": warnings}
