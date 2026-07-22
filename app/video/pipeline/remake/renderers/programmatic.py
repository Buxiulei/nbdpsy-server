"""小球段程序化渲染：一条 ffmpeg 命令出整段（spec §6）。

相位连续（Global Constraints）：overlay 的 t 是段内时间（从 0 起），
表达式写 (t + global_t0) 补偿成全局相位，相邻球段拼接处球位置无跳变。
"""
from pathlib import Path

from PIL import Image

from app.video.pipeline.muxer import _run_ffmpeg
from app.video.pipeline.remake import style


def overlay_x_expr(*, width: int, amplitude_ratio: float,
                   period_s: float, global_t0: float) -> str:
    """球心 x 表达式：(W-w)/2 + 振幅px * sin(2π*(t+t0)/T)。"""
    amp = round(width * amplitude_ratio)
    return f"(W-w)/2+{amp}*sin(2*PI*(t+{global_t0})/{period_s})"


def ball_canvas_px(radius_px: int) -> int:
    """球贴图画布边长：2×半径（锐利实心球紧贴画布，无外扩光晕）。"""
    return 2 * radius_px


def ball_png(out_path: Path, *, color_hex: str, radius_px: int) -> Path:
    """品牌色锐利实心球 PNG：实心到 radius-2px，最后 2px 线性渐落到 0（仅抗锯齿软边）。

    设计取向（F2）：动画工作室式干净锐利球。回滚 A7 的大光晕软边——120fps 下帧间步进
    ~13px，锐利实心球本身即流畅；假的软光晕/运动模糊在静帧、暂停时观感糊成一团（用户实拍打回）。
    """
    import numpy as np

    size = ball_canvas_px(radius_px)
    c = size // 2                                   # 球心锚点：画布几何中心像素
    h = color_hex.lstrip("#")
    rgb = tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))
    yy, xx = np.ogrid[:size, :size]
    dist = np.sqrt((xx - c) ** 2 + (yy - c) ** 2)
    # 分段线性 alpha：≤ radius-2 恒 255（实心），radius-2 → radius 线性降到 0（2px 抗锯齿软边），
    # ≥ radius 为 0（clip 收束两端）。除数 2.0 = 软边宽度（像素）。
    alpha = np.clip((radius_px - dist) / 2.0, 0.0, 1.0)
    arr = np.zeros((size, size, 4), dtype=np.uint8)
    arr[..., 0], arr[..., 1], arr[..., 2] = rgb
    arr[..., 3] = np.round(alpha * 255).astype(np.uint8)
    out_path = Path(out_path)
    Image.fromarray(arr, "RGBA").save(out_path)
    return out_path


async def render(scene: dict, out_path: Path, *,
                 deadline: float | None = None) -> Path:
    """渲染一个球场景为无声视频段（统一输出规格见 style）。"""
    import asyncio
    import time
    params = scene["params"]
    duration = float(scene["t1"]) - float(scene["t0"])
    radius = max(2, round(style.VIDEO_H * style.BALL_RADIUS_RATIO))
    # 【非阻塞红线】ball_png 是 numpy 生成 + PIL 落盘的同步 CPU 段——下沉线程，不占事件循环
    # （单进程 asyncio worker，阻塞会饿死心跳泵/其它 job，见 scheduler.py 硬契约）。
    ball = await asyncio.to_thread(
        ball_png, Path(out_path).with_suffix(".ball.png"),
        color_hex=params["ball_color"], radius_px=radius)
    half = ball_canvas_px(radius) // 2             # 球贴图半宽 = 球心贴图左上偏移
    # overlay y 是贴图左上角：球心 = y + half，令其落在目标纵向位置。
    # revision ball_style.y_ratio 覆盖时 storyboard 写入 params.y_ratio，缺省读 style 常量。
    y = round(style.VIDEO_H * params.get("y_ratio", style.BALL_Y_RATIO)) - half
    try:
        bg = params["bg_color"].lstrip("#")
        # F2：静止/运动都原生 120fps 单级 overlay 直出，无 tmix 快门平均——回滚 A7 的运动模糊方案。
        # 120fps 帧间步进 ~13px，锐利实心球本身即流畅；假模糊在静帧/暂停时糊成一团（用户实拍打回）。
        if params.get("static"):                   # 静止休息球：固定居中
            x_expr = "(W-w)/2"
        else:                                       # 运动球：全局相位 sin 摆动
            x_expr = overlay_x_expr(width=style.VIDEO_W,
                                    amplitude_ratio=float(params["amplitude_ratio"]),
                                    period_s=float(params["period_s"]),
                                    global_t0=float(scene["t0"]))
        filter_v = f"[0:v][1:v]overlay=x='{x_expr}':y={y}[v]"
        timeout = 1800.0
        if deadline is not None:
            timeout = max(60.0, min(timeout, deadline - time.monotonic()))
        await _run_ffmpeg([
            "-f", "lavfi",
            "-i", f"color=c=0x{bg}:s={style.VIDEO_W}x{style.VIDEO_H}:r={style.FPS}:d={duration}",
            "-i", str(ball),
            "-filter_complex", filter_v,
            "-map", "[v]", "-c:v", "libx264", "-preset", "veryfast",
            "-crf", "18", "-pix_fmt", "yuv420p", "-an", str(out_path),
        ], timeout=timeout)
    finally:
        ball.unlink(missing_ok=True)           # 渲染完/失败都清理临时球图
    return Path(out_path)
