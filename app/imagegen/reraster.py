"""gpt-image 生图去水印:Playwright 截图重栅格化(自薯营家 screenshot_reraster 移植)。

gpt-image-2 输出除 C2PA 文件元数据外,还在像素里嵌了耐久水印(C2PA manifest 含
c2pa.watermarked 断言)。仅剥文件元数据无法触及像素级水印。本模块用无头 Chromium
把图重新渲染并截图——2x 渲染 + 降采样的双重重采样扰动像素级水印,同时天然丢弃全部
C2PA/EXIF/XMP 元数据。

非破坏性:任何异常都被吞掉并返回 success=False + 原始 path,绝不抛、绝不阻断出图。
浏览器阶段共享总超时预算,收尾另有独立预算且对取消免疫(护栏见 playwright_guard)。

效力说明:重采样对耐久水印是"扰动"而非"保证清除",能否规避目标平台 AI 检测以平台
真实行为为准,本模块不做此保证。
"""
from __future__ import annotations

import io
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from loguru import logger

# 浏览器阶段总预算:实测正常 ~1.2s,留 16 倍余量。收尾预算见 playwright_guard。
_DEFAULT_TIMEOUT_S = 20.0
_LABEL = "reraster"

_HTML_TEMPLATE = (
    '<!doctype html><html><head><meta charset="utf-8"><style>'
    "*{margin:0;padding:0}"
    "html,body{width:__W__px;height:__H__px;overflow:hidden;background:#fff}"
    "img{display:block;width:__W__px;height:__H__px}"
    "</style></head><body><img src=\"__SRC__\"></body></html>"
)


@dataclass
class ReRasterResult:
    success: bool
    path: str
    error: Optional[str] = None


async def reraster_image(path: str) -> ReRasterResult:
    """把图片用无头浏览器重渲染 + 截图,返回新 png 路径(已重采样、无元数据)。

    落盘为 ``{原stem}.shot.png``,尺寸与原图一致(2x 截图后降采样回原尺寸)。
    """
    if not path or not os.path.isfile(path):
        return ReRasterResult(False, path, error="source file missing")

    stem, _ext = os.path.splitext(path)
    # 产物存 JPEG q92:gpt-image 的连续色调图用 PNG(无损)每张 ~2MB,5 张近 10MB,
    # 拖垮下游发布提交(实测 CF 524);q92 JPEG 视觉无损、体积 ~1/7(实测 9.5MB→1.4MB)。
    out_path = f"{stem}.shot.jpg"
    tmp_html = None
    try:
        from PIL import Image

        with Image.open(path) as im:
            w, h = im.size

        html = (
            _HTML_TEMPLATE
            .replace("__W__", str(w))
            .replace("__H__", str(h))
            .replace("__SRC__", Path(path).as_uri())
        )
        with tempfile.NamedTemporaryFile(
            suffix=".html", delete=False, mode="w", encoding="utf-8"
        ) as f:
            f.write(html)
            tmp_html = f.name

        from app.imagegen.playwright_guard import guarded_chromium, guarded_step

        async with guarded_chromium(_DEFAULT_TIMEOUT_S, label=_LABEL) as (browser, deadline):
            page = await guarded_step(
                browser.new_page(
                    viewport={"width": w, "height": h},
                    device_scale_factor=2,  # 2x 渲染,配合后续降采样构成双重重采样
                ),
                deadline, "new_page", label=_LABEL,
            )
            await guarded_step(
                page.goto(Path(tmp_html).as_uri()), deadline, "goto", label=_LABEL)
            await guarded_step(
                page.wait_for_load_state("networkidle"),
                deadline, "wait_for_load_state", label=_LABEL)
            shot = await guarded_step(
                page.screenshot(type="png"), deadline, "screenshot", label=_LABEL)

        # 2x 截图降采样回原尺寸:第二次重采样 + 保持下游尺寸不变 + 再次丢弃元数据
        with Image.open(io.BytesIO(shot)) as big:
            final = big.convert("RGB").resize((w, h), Image.LANCZOS)
            final.save(out_path, format="JPEG", quality=92)

        if not os.path.isfile(out_path):
            return ReRasterResult(False, path, error="screenshot not saved")
        return ReRasterResult(True, out_path)
    except Exception as e:  # noqa: BLE001 — 绝不阻断出图:失败退回原图
        logger.warning(f"reraster_image fallback path={path} err={e}")
        try:
            if os.path.isfile(out_path):
                os.remove(out_path)
        except Exception:  # noqa: BLE001
            pass
        return ReRasterResult(False, path, error=str(e))
    finally:
        if tmp_html and os.path.isfile(tmp_html):
            try:
                os.remove(tmp_html)
            except Exception:  # noqa: BLE001
                pass
