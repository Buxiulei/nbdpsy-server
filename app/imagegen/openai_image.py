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
import random
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from loguru import logger

from app.core.config import settings
from app.imagegen.image_gate import image_slot

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

# 限流(429)的兜底文案关键词,同 moderation 的双层判定理由:走国内中转时结构化
# error 常被拍扁成纯文本,只认 openai.RateLimitError 会漏判成系统级失败直接判死。
_RATE_LIMIT_KEYWORDS = (
    "rate limit",
    "rate_limit",
    "429",
    "too many requests",
    "限流",
)

# 429 退避重试:最多重试 3 次,基数 2s 指数退避(2s/4s/8s)+ 抖动。
# moderation 是 prompt 本身的问题,重试纯烧钱,一律不重试。
_RATE_LIMIT_MAX_RETRIES = 3
_RATE_LIMIT_BACKOFF_BASE = 2.0

# 超时的兜底文案关键词,同 429/moderation 的双层判定理由:中转会把结构化异常拍扁成
# 纯文本("Request timed out." 是 openai SDK APITimeoutError 的硬编码文案)。
_TIMEOUT_KEYWORDS = (
    "timed out",
    "timeout",
    "超时",
)

# 超时退避:基数 5s、倍数 3(5s/15s),**比 429 慢得多**且预算与 429 分开计。
# 依据(2026-08-05 实测):失败耗时 18/20.7/33s 短于成功耗时 43-76s,说明挂的是瞬时
# 网络抖动而非"生成太慢";SDK 内建的三次快速重试(合计约 16s)跨不过抖动窗口,退避
# 必须显著更长才有意义。次数由 settings.OPENAI_IMAGE_TIMEOUT_RETRIES 控制。
_TIMEOUT_BACKOFF_BASE = 5.0
_TIMEOUT_BACKOFF_FACTOR = 3

# 交给 openai SDK 的自身重试次数:显式压到 1。SDK 默认 2 次,与本模块的重试相乘会让
# 单页最坏尝试次数膨胀且无法解释;重试节奏统一由 _call_with_backoff 掌握。
_SDK_MAX_RETRIES = 1
# httpx 超时分项:connect 由 settings 控制,read 复用 OPENAI_IMAGE_TIMEOUT,
# write 给锚点图上传留足(默认 5s 对 1MB+ 载荷偏紧),pool 为等连接池名额的上限。
_WRITE_TIMEOUT = 120.0
_POOL_TIMEOUT = 30.0

# 锚点图喂上游前的瘦身阈值:长边超过即等比缩到该值并转 JPEG。锚点只用于风格锚定,
# 不需要原始像素;而 1.4MB 原图的上传窗口更长,抖动期更容易踩超时(需求文档第 2 条)。
_ANCHOR_MAX_SIDE = 1536
_ANCHOR_JPEG_QUALITY = 90


@dataclass
class ImageGenResult:
    """单张生图结果;``path`` 为落盘绝对路径(success 时非空)。"""

    success: bool
    path: Optional[str] = None
    error: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class _Attempts:
    """上游尝试次数计数盒(可变):重试耗尽抛异常时,调用方仍能读到真实尝试次数。"""

    n: int = 0


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


def _is_rate_limit_error(exc: Exception) -> bool:
    """判定异常是否为上游限流 429(结构化类型/状态码/code,再兜底文案关键词)。"""
    try:
        from openai import RateLimitError

        if isinstance(exc, RateLimitError):
            return True
    except Exception:  # noqa: BLE001
        pass
    if getattr(exc, "status_code", None) == 429:
        return True
    code = getattr(exc, "code", None)
    if isinstance(code, str) and "rate_limit" in code.lower():
        return True
    msg = str(exc).lower()
    return any(kw in msg for kw in _RATE_LIMIT_KEYWORDS)


def _is_timeout_error(exc: Exception) -> bool:
    """判定异常是否为上游超时(结构化类型优先,再兜底中转拍扁后的纯文案)。

    结构化覆盖三层:openai 的 APITimeoutError、httpx 的 TimeoutException(自带
    http_client 时直接冒出)、以及本模块 asyncio.wait_for 抛的裸 TimeoutError
    (str 为空串,只靠文案关键词判不出来)。
    """
    try:
        from openai import APITimeoutError

        if isinstance(exc, APITimeoutError):
            return True
    except Exception:  # noqa: BLE001
        pass
    if isinstance(exc, asyncio.TimeoutError):
        return True
    try:
        import httpx

        if isinstance(exc, httpx.TimeoutException):
            return True
    except Exception:  # noqa: BLE001
        pass
    msg = str(exc).lower()
    return any(kw in msg for kw in _TIMEOUT_KEYWORDS)


def _compress_anchor(raw: bytes) -> tuple[str, bytes, str]:
    """锚点图瘦身:长边超阈值则等比缩到 _ANCHOR_MAX_SIDE + 转 JPEG q90。

    返回 ``(文件名, bytes, mime)`` 三元组,即 images.edit 的 image 载荷形状(顺序是
    SDK 约定的 file tuple,别写反)。长边未超阈值原样返回(gpt-image 原生 1024x1536
    走这条,不做多余重编码)。
    **任何失败一律回退原字节**:压缩只是给上传瘦身,绝不能因为它把一次生图搞挂。
    """
    try:
        import io

        from PIL import Image

        with Image.open(io.BytesIO(raw)) as im:
            if max(im.size) <= _ANCHOR_MAX_SIDE:
                return "anchor.png", raw, "image/png"
            ratio = _ANCHOR_MAX_SIDE / max(im.size)
            new_size = (max(1, round(im.width * ratio)), max(1, round(im.height * ratio)))
            # JPEG 不支持 alpha:RGBA/P 等模式必须先转 RGB,否则 save 直接抛
            resized = im.convert("RGB").resize(new_size, Image.LANCZOS)
            buf = io.BytesIO()
            resized.save(buf, format="JPEG", quality=_ANCHOR_JPEG_QUALITY)
        data = buf.getvalue()
        logger.info(
            f"[openai_image] 锚点压缩: {len(raw) / 1024:.0f}KB → {len(data) / 1024:.0f}KB "
            f"{new_size}")
        return "anchor.jpg", data, "image/jpeg"
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[openai_image] 锚点压缩失败,回退原字节: {exc}")
        return "anchor.png", raw, "image/png"


def _ratelimit_headers(exc: Exception) -> str:
    """从 429 异常里摘出 OpenAI 的 x-ratelimit-* 响应头,拼成日志后缀(取不到返回空串)。

    这些头是**我们这把 key 真实限额的唯一权威来源**(官方分档表只是参考,实际以组织
    limits 面板/响应头为准),记下来就能据实调并发,不必靠猜。全防御:日志辅助信息
    绝不能反过来把重试搞崩。
    """
    try:
        headers = getattr(getattr(exc, "response", None), "headers", None) or {}
        picked = {k: v for k, v in headers.items() if "ratelimit" in k.lower()}
        retry_after = headers.get("retry-after")
        if retry_after:
            picked["retry-after"] = retry_after
        return f" | 限额头: {picked}" if picked else ""
    except Exception:  # noqa: BLE001
        return ""


async def _call_with_backoff(make_call, *, tag: str, counter: Optional[_Attempts] = None):
    """执行一次上游图像调用(带超时),对 429 与上游超时分别做退避重试。

    ``make_call`` 是零参协程工厂(每次重试重新发起请求)。moderation 与其它异常
    一律原样抛出交由调用方分层;重试耗尽也抛出最后一次异常。``counter`` 非空时累计
    实际尝试次数(成功与抛出两条路都已写入,供结果里透出 upstream_attempts)。

    **429 与超时各计各的预算**:成因不同(限流是配额、超时是网络抖动),共用一个预算
    会让先撞几次限流的页把超时重试额度提前花光。退避基数也不同,理由见 _TIMEOUT_* 常量。
    """
    rate_limit_retries = 0
    timeout_retries = 0
    while True:
        if counter is not None:
            counter.n += 1
        try:
            # 进程级闸只包住**真正在飞的那次请求**:退避 sleep 期间名额归还给别的任务
            # (被限流时占着名额干等纯属浪费吞吐)。获取顺序恒为 页级 sem → 本闸,
            # 全局一致故无循环等待、无死锁。
            async with image_slot():
                return await asyncio.wait_for(
                    make_call(), timeout=settings.OPENAI_IMAGE_TIMEOUT)
        except Exception as exc:  # noqa: BLE001
            if _is_moderation_error(exc):
                raise
            if _is_rate_limit_error(exc):
                if rate_limit_retries >= _RATE_LIMIT_MAX_RETRIES:
                    raise
                delay = _RATE_LIMIT_BACKOFF_BASE * (2 ** rate_limit_retries)
                rate_limit_retries += 1
                logger.warning(
                    f"[openai_image] 限流 429 {tag},{delay:.1f}s 后重试"
                    f"({rate_limit_retries}/{_RATE_LIMIT_MAX_RETRIES}): {exc}"
                    f"{_ratelimit_headers(exc)}")
            elif _is_timeout_error(exc):
                budget = max(0, settings.OPENAI_IMAGE_TIMEOUT_RETRIES)
                if timeout_retries >= budget:
                    raise
                delay = _TIMEOUT_BACKOFF_BASE * (_TIMEOUT_BACKOFF_FACTOR ** timeout_retries)
                timeout_retries += 1
                logger.warning(
                    f"[openai_image] 上游超时 {tag},{delay:.1f}s 后重试"
                    f"({timeout_retries}/{budget}): {exc}")
            else:
                raise
            # 抖动:并发多路同时失败时错开重试,避免整齐撞回同一秒再次一起挂
            await asyncio.sleep(delay + random.uniform(0, delay / 2))


def _log_usage(response: Any, *, kind: str, index: int) -> None:
    """把上游返回的 usage 落一行日志(成本核算用)。

    中转实现常不返回 usage(为 None 或缺字段),此处全防御——记录 usage 绝不能
    反过来把一次已成功的生图弄失败。
    """
    try:
        usage = getattr(response, "usage", None)
        if usage is None:
            logger.info(f"[openai_image] usage({kind}[{index}]): 上游未返回")
            return
        logger.info(
            f"[openai_image] usage({kind}[{index}]): "
            f"input={getattr(usage, 'input_tokens', None)} "
            f"output={getattr(usage, 'output_tokens', None)} "
            f"total={getattr(usage, 'total_tokens', None)} "
            f"input_details={getattr(usage, 'input_tokens_details', None)}"
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"[openai_image] usage 记录失败(忽略): {exc}")


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
                import httpx
                from openai import AsyncOpenAI

                if not settings.OPENAI_IMAGE_API_KEY:
                    logger.warning("[openai_image] OPENAI_IMAGE_API_KEY 未配置,gpt-image 不可用")
                    return None
                # 显式分项超时:SDK 默认 connect 只有 5s,抖动期毫无缓冲直接判死
                # (给 httpx.AsyncClient 传标量 timeout 也是一样的坑——四项一起被设成
                # 300s 或 5s,都不是我们要的)。两支分支都必须挂上,别只改一支。
                timeout = httpx.Timeout(
                    connect=float(settings.OPENAI_IMAGE_CONNECT_TIMEOUT),
                    read=float(settings.OPENAI_IMAGE_TIMEOUT),
                    write=_WRITE_TIMEOUT,
                    pool=_POOL_TIMEOUT,
                )
                http_client = None
                if settings.OPENAI_IMAGE_PROXY:
                    http_client = httpx.AsyncClient(
                        proxy=settings.OPENAI_IMAGE_PROXY,
                        timeout=timeout,
                    )
                self._client = AsyncOpenAI(
                    api_key=settings.OPENAI_IMAGE_API_KEY,
                    base_url=settings.OPENAI_IMAGE_BASE_URL,
                    http_client=http_client,
                    timeout=timeout,
                    max_retries=_SDK_MAX_RETRIES,
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
        counter = _Attempts()
        try:
            response = await _call_with_backoff(
                lambda: client.images.generate(
                    model=settings.OPENAI_IMAGE_MODEL,
                    prompt=prompt,
                    size=size,
                    quality=settings.OPENAI_IMAGE_QUALITY,
                    n=1,
                ),
                tag="generate",
                counter=counter,
            )
        except Exception as exc:  # noqa: BLE001
            # 错误分层:安全审查拦截是"prompt 本身有问题"的业务信号;其余是系统级失败。
            # 失败结果同样带尝试次数——调用方要据此判断"慢/抖动是不是常态"。
            meta = {"upstream_attempts": counter.n}
            if _is_moderation_error(exc):
                logger.warning(f"[openai_image] 内容审查拦截: {exc}")
                return ImageGenResult(
                    success=False, error=f"moderation_blocked: {exc}", metadata=meta)
            logger.error(f"[openai_image] 生成失败(尝试 {counter.n} 次): {exc}")
            return ImageGenResult(
                success=False, error=f"openai_image_call_failed: {exc}", metadata=meta)
        _log_usage(response, kind="generate", index=0)
        if not response.data:
            return ImageGenResult(success=False, error="empty_response",
                                  metadata={"upstream_attempts": counter.n})
        result = self._save(response.data[0].b64_json, save_prefix)
        if result.success:
            result.metadata = {"size": size, "aspect_ratio": aspect_ratio}
        result.metadata["upstream_attempts"] = counter.n
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
        - edit 段按 ``OPENAI_IMAGE_CONCURRENCY`` 有界并发发出(信号量),结果仍严格
          按下标对齐:失败位是 success=False 占位,绝不跳过/丢弃导致后续位左移。
        """
        if not prompts:
            return []
        client = self.client
        if not client:
            return [ImageGenResult(success=False, error="openai_image_client_unavailable")
                    for _ in prompts]

        size = ASPECT_RATIO_TO_OPENAI_SIZE.get(aspect_ratio, "1024x1024")
        # 有界并发:各页都锚同一张 P1、彼此无依赖,可并发发出;但路数必须封顶,
        # 齐发会撞上游 429(且中转带宽有限)。信号量同时管 edit 段与降级段。
        sem = asyncio.Semaphore(max(1, settings.OPENAI_IMAGE_CONCURRENCY))

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

                async def _fallback_one(p: str) -> ImageGenResult:
                    """降级单张;失败就地转失败结果,保证该下标位不塌陷。

                    与 _edit_one 同款纪律:``generate`` 里 ``_save`` 的落盘
                    (write_bytes)在 try 之外,磁盘满/目录被并发清理会原样抛出;
                    若让它冲进 gather,整批(含已成功、已付费的张)会一起丢。
                    """
                    async with sem:
                        try:
                            return await self.generate(
                                p, aspect_ratio=aspect_ratio, save_prefix=save_prefix)
                        except Exception as exc:  # noqa: BLE001
                            logger.error(f"[openai_image] 降级生成异常: {exc}")
                            return ImageGenResult(
                                success=False, error=f"openai_image_call_failed: {exc}")

                fallback = await asyncio.gather(
                    *[_fallback_one(p) for p in prompts], return_exceptions=True)
                out: List[ImageGenResult] = []
                for item in fallback:
                    if isinstance(item, BaseException):
                        out.append(ImageGenResult(
                            success=False, error=f"openai_image_call_failed: {item}"))
                    else:
                        out.append(item)
                for r in out:
                    r.metadata["consistency_fallback"] = True
                return out
            results = [anchor_result]
            anchor_bytes = Path(anchor_result.path).read_bytes()
            edit_start = 1

        # anchor bytes 只读一次并压一次,所有 edit 复用同一载荷(每张都锚同一张 P1)。
        # 是不可变 bytes 三元组而非文件句柄,并发复用不存在读游标竞争。
        anchor_file = _compress_anchor(anchor_bytes)

        async def _edit_one(i: int) -> ImageGenResult:
            """第 i 张 edit;失败就地转成失败结果返回,保证该下标位不塌陷。"""
            async with sem:
                counter = _Attempts()
                try:
                    response = await _call_with_backoff(
                        lambda: client.images.edit(
                            model=settings.OPENAI_IMAGE_MODEL,
                            image=anchor_file,
                            prompt=prompts[i],
                            size=size,
                            quality=settings.OPENAI_IMAGE_QUALITY,
                            n=1,
                        ),
                        tag=f"edit[{i}]",
                        counter=counter,
                    )
                except Exception as exc:  # noqa: BLE001
                    meta = {"upstream_attempts": counter.n}
                    if _is_moderation_error(exc):
                        logger.warning(f"[openai_image] edit 内容审查拦截[{i}]: {exc}")
                        return ImageGenResult(
                            success=False, error=f"moderation_blocked: {exc}", metadata=meta)
                    logger.error(f"[openai_image] edit 失败[{i}](尝试 {counter.n} 次): {exc}")
                    return ImageGenResult(
                        success=False, error=f"openai_image_edit_failed: {exc}", metadata=meta)
                _log_usage(response, kind="edit", index=i)
                if not response.data:
                    return ImageGenResult(success=False, error="empty_response",
                                          metadata={"upstream_attempts": counter.n})
                r = self._save(response.data[0].b64_json, save_prefix)
                if r.success:
                    r.metadata = {"size": size, "consistency": "anchor"}
                r.metadata["upstream_attempts"] = counter.n
                return r

        # gather 保序:edits[k] 恒对应 prompts[edit_start + k]。return_exceptions 兜住
        # per-item try 之外的漏网异常,一律转失败占位——下标位绝不可被跳过/丢弃,
        # 否则下游按下标落 P01.png…PNN.png 会整体错位。
        edits = await asyncio.gather(
            *[_edit_one(i) for i in range(edit_start, len(prompts))],
            return_exceptions=True,
        )
        for offset, item in enumerate(edits):
            if isinstance(item, BaseException):
                logger.error(f"[openai_image] edit 未捕获异常[{edit_start + offset}]: {item}")
                results.append(ImageGenResult(
                    success=False, error=f"openai_image_edit_failed: {item}"))
            else:
                results.append(item)

        return results
