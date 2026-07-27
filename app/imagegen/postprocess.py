"""生图后处理编排:去水印工作流(gpt-image 专用,自薯营家 proxy_image 后处理移植)。

**只有一条路**:``reraster_image`` 截图重栅格化(2x 渲染 + LANCZOS 降采样的双重重采样
扰动像素级耐久水印,同时天然丢弃全部 C2PA/EXIF/XMP 元数据)。主路失败即整体失败,
返回 ``None``,由调用方判该页失败——绝不拿带水印的图冒充"已处理"交付。

砍掉的两级旧兜底(2026-07-26,用户裁决):
- ② PIL 像素级重存(``Image.putdata`` 新建拷贝存 ``{stem}.clean.jpg``):**一个像素都不动**,
  只剥 C2PA/EXIF 文件元数据。像素级耐久水印原封不动 → 在"会不会被识别成 AI 生成"这件事上
  与直接交原图**没有任何区别**,留着只会让人误以为已经处理过。
- ③ 直接返回原图路径:同上,带水印交付且无声无息。

批量入口 ``dewatermark_all``(2026-07-27 新增)是**发布口的 fail-closed 闸**:发布任务
拿到的是图片字节快照、判断不了"这张是否已清洗",故统一重做,任一张失败即抛异常让整个
发布任务失败——绝不退回"用原图发"。

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


async def dewatermark_all(paths: list[str]) -> list[str]:
    """发布口的 fail-closed 去水印闸:按页序逐张重做,返回等长的清洗后路径列表。

    发布任务手上只有图片字节快照(``publish_jobs.images_json``),**判断不了某张图是否
    已经去过水印**——像素判据只能看出"有没有被重采样过",看不出"这张图对应的原图是谁"。
    所以不猜,统一重做;重复 reraster 只是轻微质量损失,可接受。

    任何一张失败即抛异常让整个发布任务失败,**绝不静默降级成"用原图发"**——那正是
    job 14/15/16 七张全带水印发出去的事故形态。

    Raises:
        RuntimeError: 任一页去水印失败(文案带页序 + 拒发声明,交由调用方的终态逻辑
            落 error 排重试)。
    """
    cleaned: list[str] = []
    for index, path in enumerate(paths):
        out = await dewatermark(path)
        if not out:
            raise RuntimeError(
                f"第 {index + 1} 页去水印失败(reraster 未产出),"
                f"拒绝发布未去水印的图: {path}"
            )
        cleaned.append(out)
    return cleaned
