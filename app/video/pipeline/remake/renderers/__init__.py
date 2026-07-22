"""渲染器注册表（spec §6）：统一契约 async render(scene, out_path, *, deadline) -> Path。

seedance 属 KNOWN 但未实现（v1 只有 programmatic / still_image），storyboard 校验
引用 storyboard.IMPLEMENTED_RENDERERS 常量在生成期拦截，此处 get_renderer 兜底 KeyError。
"""
from app.video.pipeline.remake.renderers import programmatic, still_image

RENDERERS = {
    "programmatic": programmatic.render,
    "still_image": still_image.render,
}


def get_renderer(name: str):
    return RENDERERS[name]
