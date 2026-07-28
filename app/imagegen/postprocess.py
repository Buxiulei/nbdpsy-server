"""生图后处理编排:去水印(gpt-image 专用)。

**原理(2026-07-28 换代,用户真机实发验证)**:把 AI 图**非整数倍缩小到 85.5%
(factor=0.855)并存 PNG 无损**。非整数缩小会对每个像素重新插值,打乱扩散模型输出的
像素网格生成指纹,视觉依旧清晰;PNG 重编码天然丢弃全部 C2PA/EXIF/XMP 元数据。

锚定值来源(实测,勿改):用户微信截图(986×1312 PNG,相当于把原图按 986/1152≈0.855
缩过)实发小红书未被标 AI;lead 用 PIL 复刻(原图 BICUBIC 缩到 round(w*0.855) + 存
PNG)与该微信截图逐像素 MAE 仅 3.18。**BICUBIC / 0.855 / PNG 三者是实测锚定值。**

被替换的旧实现(reraster:headless chromium 截图重栅格化,**保原尺寸** + JPEG q92):
保尺寸时像素网格未经非整数重采样打乱,job28 用它实发仍被检测——已连同 reraster/
playwright_guard 一并删除。

**诚实说明**:非整数缩小是对生成指纹的**扰动**而非保证清除,能否规避目标平台 AI
检测以平台真实行为为准,本模块不做此保证。

**双重去水印的幂等(内容标记)**:发布口 ``dewatermark_all`` 是 fail-closed 闸,对图片
字节快照统一重做;而生图侧 ``op_images`` 也各去一次水印——两处叠加会把 0.855 复利成
0.731(把图缩过头)。判"这张缩过没有"必须用**可靠判据**:早期版本用"尺寸是否为 gpt
原生三尺寸"当判据,但生产上 14%(190 张里 27 张)是 1152×1536 等**非原生尺寸**(job28
那批从上传通道来的图),会被尺寸门原样透传、带 C2PA 元数据发出——恰恰漏掉核心 case;
发布口 ``images_json`` 来自远程 agent 供图(Gemini/即梦/上传通道)尺寸五花八门,几乎全
非三尺寸 → 全透传 → job14/15/16 发原图的事故复活。**尺寸不是可靠的"已处理"判据。**

正解是**内容标记**:缩图存 PNG 时用 tEXt chunk 写入 ``_MARK_KEY`` 标记(该标记能穿过
``materialize_images`` 的 base64/dataURI 两形态)。``dewatermark`` 只在**图上无标记**时
无条件缩 0.855 + 打标(不管尺寸/来源),已带标记的原样透传——这样 op_images 已处理的图
在发布口被识别透传避免双缩,而任何外源非标尺寸图(job28 那批)也一律被缩不遗漏。

批量入口 ``dewatermark_all`` 是**发布口的 fail-closed 闸**:发布任务拿到的是图片字节
快照、判断不了"这张是否已清洗",故对每张统一走 ``dewatermark``(内容标记保证只缩一次),
任一张失败即抛异常让整个发布任务失败——绝不退回"用原图发"。
"""
from __future__ import annotations

import os
from typing import Optional

from loguru import logger

# 非整数缩小因子:用户微信截图 986/1152≈0.855 实测过小红书 AIDE 未被标 AI;lead PIL
# 复刻(BICUBIC + round(w*0.855) + PNG)与之逐像素 MAE 3.18。实测锚定值,不要改。
_SHRINK_FACTOR = 0.855

# 我们自己写进 PNG tEXt chunk 的"已去水印"处理标记 key。用来在发布口识别 op_images
# 已缩过的图、避免双缩(把 0.855 复利成 0.731)。它**不是** AI 生成标记——小红书 AIDE
# 看的是像素指纹不看 PNG chunk,写它不会让图被判 AI;它只是本模块自己的幂等判据。
# 尺寸不是可靠判据:外源非标尺寸图(job28 那批 1152×1536)会漏过尺寸门带元数据发出,
# 故改用内容标记——无标记就一律缩+打标,只有我们打过标的图才透传。
_MARK_KEY = "nbdpsy_dewm"


async def dewatermark(path: str) -> Optional[str]:
    """对一张生成图去水印:非整数缩小 0.855 + 存 PNG,成功返产物路径,失败返 ``None``。

    内容标记幂等:打开图检查 PNG tEXt 里的 ``_MARK_KEY``——
    - **已带标记**(op_images 已缩过并打过标的图)→ 原样返回输入路径,不二次缩小
      (发布口对同一张图重复调用不会把 0.855 复利成 0.731);
    - **无标记**(全新图 / 任何外源图,**不管尺寸/来源**)→ 无条件缩到 ``round(*0.855)``
      存 ``{stem}.clean.png`` 并写入 ``_MARK_KEY`` 标记,返回新路径。

    为何从"尺寸感知"改成"内容标记":尺寸不是可靠的"已处理"判据——生产上外源非标尺寸图
    (job28 那批 1152×1536,占 14%)会被尺寸门原样透传、带 C2PA 元数据发出,恰是核心 case
    漏网;改用内容标记后"无标记即缩+打标"恢复了统一处理不遗漏的 fail-closed 语义。

    fail-closed:源文件不存在/打不开/任何异常一律返回 ``None``,**不退回原图**——调用方
    据 ``None`` 判该页失败即可(运营用 --pages 重出该页),原图另有提取通道。

    非整数缩小是对生成指纹的扰动而非保证清除,能否规避平台检测以平台真实行为为准。
    """
    if not path or not os.path.isfile(path):
        logger.warning(f"[postprocess] 去水印失败:源文件不存在 path={path}")
        return None
    try:
        from PIL import Image, PngImagePlugin

        with Image.open(path) as im:
            if im.info.get(_MARK_KEY):
                # 内容标记幂等:我们自己缩过并打过标的图,原样透传不二次缩。
                return path
            w, h = im.size
            # convert RGB 统一去 alpha/palette;BICUBIC 非整数重采样打乱像素网格指纹。
            resized = im.convert("RGB").resize(
                (round(w * _SHRINK_FACTOR), round(h * _SHRINK_FACTOR)), Image.BICUBIC
            )
        stem, _ext = os.path.splitext(path)
        out_path = f"{stem}.clean.png"
        # 存 PNG 只写 _MARK_KEY 这一个自有标记(供发布口识别已处理),不传 exif/icc_profile
        # → C2PA/EXIF/XMP 全部天然丢弃(勿专门加回)。
        meta = PngImagePlugin.PngInfo()
        meta.add_text(_MARK_KEY, "1")
        resized.save(out_path, format="PNG", pnginfo=meta)
        if not os.path.isfile(out_path):
            return None
        return out_path
    except Exception as e:  # noqa: BLE001 — 任何异常都算失败,fail-closed 返 None
        logger.warning(f"[postprocess] 去水印失败(PIL: {e}) path={path}")
        return None


async def dewatermark_all(paths: list[str]) -> list[str]:
    """发布口的 fail-closed 去水印闸:按页序逐张重做,返回等长的清洗后路径列表。

    发布任务手上只有图片字节快照(``publish_jobs.images_json``),**判断不了某张图是否
    已经去过水印**。所以不猜,对每张统一走 ``dewatermark``——其内容标记幂等保证已打标的
    图原样透传、无标记的(不管尺寸/来源)一律被缩,同一张图全链路只缩一次(不复利成 0.731)。

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
                f"第 {index + 1} 页去水印失败(未产出),"
                f"拒绝发布未去水印的图: {path}"
            )
        cleaned.append(out)
    return cleaned
