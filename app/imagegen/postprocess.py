"""生图后处理编排:去水印工作流(gpt-image 专用,自薯营家 proxy_image 后处理移植)。

主路:``reraster_image`` 截图重栅格化(扰动像素级耐久水印 + 丢弃全部 C2PA/EXIF
元数据);失败退回 PIL 像素级重存兜底(``Image.putdata`` 新建像素拷贝重存 PNG——
不带任何源文件 chunk/元数据,至少剥掉 C2PA 清单)。

绝不阻断硬约束:任何失败都退回原图路径继续交付,宁可带水印出图也不空手。
(薯营家的 gemini SynthID 可见水印引擎不迁——本服务只有 gpt-image 一条生图路线。)
"""
from __future__ import annotations

import os

from loguru import logger

from app.imagegen.reraster import reraster_image


async def dewatermark(path: str) -> str:
    """对一张生成图执行去水印工作流,返回最终交付路径(失败退回原路径)。"""
    if not path or not os.path.isfile(path):
        return path
    # 主路:截图重栅格化
    rr = await reraster_image(path)
    if rr.success:
        return rr.path
    logger.warning(f"[postprocess] reraster 失败({rr.error}),退回 PIL 像素重存兜底")
    # 兜底:PIL 像素级拷贝重存(剥全部元数据,不动像素)
    try:
        from PIL import Image

        stem, _ext = os.path.splitext(path)
        out_path = f"{stem}.clean.png"
        with Image.open(path) as im:
            mode, size, data = im.mode, im.size, list(im.getdata())
        clean = Image.new(mode, size)
        clean.putdata(data)
        clean.save(out_path, format="PNG")
        return out_path
    except Exception as e:  # noqa: BLE001 — 绝不阻断:兜底也失败就原图交付
        logger.warning(f"[postprocess] 元数据剥离兜底失败({e}),原图交付")
        return path
