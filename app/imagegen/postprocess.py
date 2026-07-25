"""生图后处理编排:去水印工作流(gpt-image 专用,自薯营家 proxy_image 后处理移植)。

**只有一条路**:``reraster_image`` 截图重栅格化(2x 渲染 + LANCZOS 降采样的双重重采样
扰动像素级耐久水印,同时天然丢弃全部 C2PA/EXIF/XMP 元数据)。主路失败即整体失败,
返回 ``None``,由调用方判该页失败——绝不拿带水印的图冒充"已处理"交付。

砍掉的两级旧兜底(2026-07-26,用户裁决):
- ② PIL 像素级重存(``Image.putdata`` 新建拷贝存 ``{stem}.clean.jpg``):**一个像素都不动**,
  只剥 C2PA/EXIF 文件元数据。像素级耐久水印原封不动 → 在"会不会被识别成 AI 生成"这件事上
  与直接交原图**没有任何区别**,留着只会让人误以为已经处理过。
- ③ 直接返回原图路径:同上,带水印交付且无声无息。

效力说明(承自 reraster 的诚实声明):重采样对耐久水印是"**扰动**"而非保证清除,
能否规避目标平台的 AI 检测以平台真实行为为准,本模块不做此保证。
(薯营家的 gemini SynthID 可见水印引擎不迁——本服务只有 gpt-image 一条生图路线。)
"""
from __future__ import annotations

import os
from typing import Optional

from loguru import logger

from app.imagegen.reraster import reraster_image


async def dewatermark(path: str) -> Optional[str]:
    """对一张生成图执行去水印工作流,成功返回产物路径,失败返回 ``None``。

    只跑 reraster 主路(截图重栅格化)。失败(含源文件不存在)一律返回 ``None``,
    **不退回原图、不做只剥元数据的像素级重存兜底**——那两种"兜底"交出去的像素与原图
    完全一致,对 AI 检测毫无帮助,只会掩盖失败。调用方据 ``None`` 判该页失败即可
    (运营用 --pages 重出该页),原图另有提取通道。

    重采样是对耐久水印的扰动而非保证清除,能否规避平台检测以平台真实行为为准。
    """
    if not path or not os.path.isfile(path):
        logger.warning(f"[postprocess] 去水印失败:源文件不存在 path={path}")
        return None
    rr = await reraster_image(path)
    if rr.success:
        return rr.path
    logger.warning(f"[postprocess] 去水印失败(reraster: {rr.error}) path={path}")
    return None
