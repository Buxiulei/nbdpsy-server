"""OpenAI gpt-image-2 一致性生图 Provider(锚点法),自薯营家 openai_image.py 移植。

封装 OpenAI Images API,走 AsyncOpenAI SDK,支持自定义 base_url(国内中转)+ 可选
HTTP 代理。核心是 ``generate_batch`` 的**锚点法**:第 1 张(P1)当高保真锚点,
第 2..N 张各自 ``images.edit`` 锚定**同一张 P1**(不是锚上一张,避免风格逐张漂移
累积);外部已确认 P1(``anchor_path``)时全部页锚它、不再重画 P1。

成本模型(medium 质量、8 页约 $0.68):≈ 1×output + (N-1)×(anchor input + output)。
对比 previous_response_id 链式:链式成本 3-5 倍且风格随链漂移,故选锚点法。
"""
import asyncio
import base64
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from loguru import logger

from app.core.config import settings

# 宽高比 → gpt-image size 映射(gpt-image-2 仅支持三种尺寸)。
ASPECT_RATIO_TO_OPENAI_SIZE = {
    "3:4":  "1024x1536",
    "4:5":  "1024x1536",
    "2:3":  "1024x1536",
    "9:16": "1024x1536",
    "1:1":  "1024x1024",
    "4:3":  "1536x1024",
    "3:2":  "1536x1024",
    "16:9": "1536x1024",
}

# 内容审查/安全拦截的兜底文案关键词(大小写不敏感子串匹配)。
# 直连 OpenAI 时错误带结构化 code=="moderation_blocked";走国内中转/代理时结构化
# error 常被拍扁成纯文本,而 OpenAI 真实拒绝文案是 "safety system"——必须同时匹配
# 多种真实措辞,否则安全拦截会被误分层成系统级失败去徒劳重试。
_MODERATION_KEYWORDS = (
    "moderation",
    "safety system",
    "safety_system",
    "content_policy",
    "content policy",
    "rejected as a result of our safety",
)


@dataclass
class ImageGenResult:
    """单张生图结果;``path`` 为落盘绝对路径(success 时非空)。"""

    success: bool
    path: Optional[str] = None
    error: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)


def _is_moderation_error(exc: Exception) -> bool:
    """判定异常是否为内容审查/安全拦截(结构化 code 或兜底文案关键词)。"""
    try:
        from openai import BadRequestError

        if isinstance(exc, BadRequestError) and getattr(exc, "code", None) == "moderation_blocked":
            return True
    except Exception:  # noqa: BLE001
        pass
    msg = str(exc).lower()
    return any(kw in msg for kw in _MODERATION_KEYWORDS)


class OpenAIImageProvider:
    """gpt-image-2 文生图 provider;``save_dir`` 由调用方传入(job 专属目录)。"""

    def __init__(self, save_dir: str):
        self.save_dir = Path(save_dir)
        self.save_dir.mkdir(parents=True, exist_ok=True)
        self._client = None

    @property
    def client(self):
        """延迟初始化 AsyncOpenAI(自定义 base_url + 可选 HTTP 代理)。"""
        if self._client is None:
            try:
                from openai import AsyncOpenAI

                if not settings.OPENAI_IMAGE_API_KEY:
                    logger.warning("[openai_image] OPENAI_IMAGE_API_KEY 未配置,gpt-image 不可用")
                    return None
                http_client = None
                if settings.OPENAI_IMAGE_PROXY:
                    import httpx

                    http_client = httpx.AsyncClient(
                        proxy=settings.OPENAI_IMAGE_PROXY,
                        timeout=settings.OPENAI_IMAGE_TIMEOUT,
                    )
                self._client = AsyncOpenAI(
                    api_key=settings.OPENAI_IMAGE_API_KEY,
                    base_url=settings.OPENAI_IMAGE_BASE_URL,
                    http_client=http_client,
                )
            except ImportError:
                logger.error("[openai_image] openai SDK 未安装,gpt-image 不可用")
                return None
            except Exception as e:  # noqa: BLE001
                logger.error(f"[openai_image] 客户端初始化失败: {e}")
                return None
        return self._client

    def _save(self, b64: str, prefix: str) -> ImageGenResult:
        """b64 落盘为 png;解码失败返回失败结果。"""
        try:
            data = base64.b64decode(b64)
        except Exception as exc:  # noqa: BLE001
            return ImageGenResult(success=False, error=f"decode_failed: {exc}")
        filename = f"{prefix}_{uuid.uuid4().hex[:8]}_{int(time.time())}.png"
        filepath = self.save_dir / filename
        filepath.write_bytes(data)
        logger.info(f"[openai_image] 图片已保存: {filepath} ({len(data) / 1024:.1f}KB)")
        return ImageGenResult(success=True, path=str(filepath.resolve()))

    async def generate(
        self, prompt: str, *, aspect_ratio: str = "3:4", save_prefix: str = "img",
    ) -> ImageGenResult:
        """单张文生图(``images.generate``)。

        注意:绝不传 response_format —— gpt-image-1/2 会拒绝该参数,且强制以
        b64_json 返回,无需显式指定。
        """
        client = self.client
        if not client:
            return ImageGenResult(success=False, error="openai_image_client_unavailable")
        size = ASPECT_RATIO_TO_OPENAI_SIZE.get(aspect_ratio, "1024x1024")
        try:
            response = await asyncio.wait_for(
                client.images.generate(
                    model=settings.OPENAI_IMAGE_MODEL,
                    prompt=prompt,
                    size=size,
                    quality=settings.OPENAI_IMAGE_QUALITY,
                    n=1,
                ),
                timeout=settings.OPENAI_IMAGE_TIMEOUT,
            )
        except Exception as exc:  # noqa: BLE001
            # 错误分层:安全审查拦截是"prompt 本身有问题"的业务信号;其余是系统级失败。
            if _is_moderation_error(exc):
                logger.warning(f"[openai_image] 内容审查拦截: {exc}")
                return ImageGenResult(success=False, error=f"moderation_blocked: {exc}")
            logger.error(f"[openai_image] 生成失败: {exc}")
            return ImageGenResult(success=False, error=f"openai_image_call_failed: {exc}")
        if not response.data:
            return ImageGenResult(success=False, error="empty_response")
        result = self._save(response.data[0].b64_json, save_prefix)
        if result.success:
            result.metadata = {"size": size, "aspect_ratio": aspect_ratio}
        return result

    async def generate_batch(
        self,
        prompts: List[str],
        *,
        anchor_path: Optional[str] = None,
        aspect_ratio: str = "3:4",
        save_prefix: str = "img",
    ) -> List[ImageGenResult]:
        """锚点法一致性批量生图;返回与 prompts **按下标对齐**的结果列表。

        - ``anchor_path`` 为空(内部锚点):prompts[0] 走 generate 生成 P1 进结果,
          第 2..N 张各自 ``images.edit`` 锚定这张 P1;P1 失败则整批降级为独立并行
          (每张普通 generate,metadata 标 consistency_fallback=True)。
        - ``anchor_path`` 非空(外部已确认 P1,P1 闸门契约):跳过生成 P1,全部页
          edit 锚定该文件,返回张数 == len(prompts)。
        - 单张失败只影响该位(success=False + error),不阻断其它张。
        """
        if not prompts:
            return []
        client = self.client
        if not client:
            return [ImageGenResult(success=False, error="openai_image_client_unavailable")
                    for _ in prompts]

        size = ASPECT_RATIO_TO_OPENAI_SIZE.get(aspect_ratio, "1024x1024")

        # ── 锚点来源二选一 ──
        if anchor_path:
            try:
                anchor_bytes = Path(anchor_path).read_bytes()
            except Exception as exc:  # noqa: BLE001
                logger.error(f"[openai_image] 外部锚点读取失败 {anchor_path}: {exc}")
                return [ImageGenResult(success=False, error=f"anchor_read_failed: {exc}")
                        for _ in prompts]
            results: List[ImageGenResult] = []
            edit_start = 0
        else:
            anchor_result = await self.generate(
                prompts[0], aspect_ratio=aspect_ratio, save_prefix=save_prefix)
            if not (anchor_result.success and anchor_result.path):
                # 锚点 P1 失败 → 整批降级独立并行(标注 consistency_fallback)
                logger.warning(
                    f"[openai_image] 锚点 P1 生成失败,整批降级独立并行: {anchor_result.error}")
                fallback = await asyncio.gather(*[
                    self.generate(p, aspect_ratio=aspect_ratio, save_prefix=save_prefix)
                    for p in prompts
                ])
                for r in fallback:
                    r.metadata["consistency_fallback"] = True
                return list(fallback)
            results = [anchor_result]
            anchor_bytes = Path(anchor_result.path).read_bytes()
            edit_start = 1

        # anchor bytes 只读一次,所有 edit 复用同一载荷(每张都锚同一张 P1)。
        anchor_file = ("anchor.png", anchor_bytes, "image/png")

        for i in range(edit_start, len(prompts)):
            try:
                response = await asyncio.wait_for(
                    client.images.edit(
                        model=settings.OPENAI_IMAGE_MODEL,
                        image=anchor_file,
                        prompt=prompts[i],
                        size=size,
                        quality=settings.OPENAI_IMAGE_QUALITY,
                        n=1,
                    ),
                    timeout=settings.OPENAI_IMAGE_TIMEOUT,
                )
            except Exception as exc:  # noqa: BLE001
                if _is_moderation_error(exc):
                    logger.warning(f"[openai_image] edit 内容审查拦截[{i}]: {exc}")
                    results.append(ImageGenResult(
                        success=False, error=f"moderation_blocked: {exc}"))
                else:
                    logger.error(f"[openai_image] edit 失败[{i}]: {exc}")
                    results.append(ImageGenResult(
                        success=False, error=f"openai_image_edit_failed: {exc}"))
                continue
            if not response.data:
                results.append(ImageGenResult(success=False, error="empty_response"))
                continue
            r = self._save(response.data[0].b64_json, save_prefix)
            if r.success:
                r.metadata = {"size": size, "consistency": "anchor"}
            results.append(r)

        return results
