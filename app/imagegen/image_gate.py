"""进程级生图并发闸:封顶同时在飞的上游图像请求数,超出排队不崩。

与 ``browser_gate`` 同源同构(懒建单例 + async with 归还),但管的是 OpenAI Images
上游请求而非 camoufox 进程,两者互不相干、各自独立计数。

**为什么需要它**:生图有两层并发且**相乘**——调用方的篇级并行(skill 侧约定 10 篇)
× 单批次内部的页级并发(``OPENAI_IMAGE_CONCURRENCY``,10 路)= 100 在飞。这个乘积
只靠"约定"维持:一旦有人一次提交几十篇、或调用方私自调高篇级并行,乘积会瞬间冲破
上游 tier 限额(gpt-image-2 Tier 5 为 250 IPM),把所有运营一起拖进 429。本闸是那道
**不依赖任何人守约**的兜底:无论上游怎么并发提交,进程内在飞总数恒 ≤
``OPENAI_IMAGE_GLOBAL_CONCURRENCY``,超出的在信号量上排队(不拒绝、不丢任务)。

**获取顺序铁律**:调用方必须先拿页级信号量、再拿本闸(见 openai_image 的
``_edit_one`` / ``_fallback_one``)。全局统一这个顺序 → 不存在循环等待 → 无死锁;
反过来交叉获取才会死锁。信号量非锁,每请求 acquire→release,``async with`` 保证
异常路径也归还名额。
"""

import asyncio
from contextlib import asynccontextmanager

from app.core.config import settings

# 进程级信号量单例:首次 _get_semaphore() 时按 settings.OPENAI_IMAGE_GLOBAL_CONCURRENCY 懒建。
_sem: asyncio.Semaphore | None = None


def _get_semaphore() -> asyncio.Semaphore:
    """取进程级生图名额信号量;首次调用懒建,之后复用。

    懒建而非 import 期建:让信号量在运行中的事件循环里创建/首次阻塞即绑定该 loop,
    避免无 loop 的 import 期构造并误绑(理由同 browser_gate)。
    配置被填 0/负数时退化为 1(而非 Semaphore(0) 直接死锁)。
    """
    global _sem
    if _sem is None:
        _sem = asyncio.Semaphore(max(1, settings.OPENAI_IMAGE_GLOBAL_CONCURRENCY))
    return _sem


@asynccontextmanager
async def image_slot():
    """占用一个上游生图名额;满则 await 排队,出作用域(含异常)自动归还。

    用法::

        async with image_slot():
            await client.images.edit(...)
    """
    async with _get_semaphore():
        yield


def _reset_for_test() -> None:
    """把模块级信号量单例置空(仅测试用)。

    测试 monkeypatch settings.OPENAI_IMAGE_GLOBAL_CONCURRENCY 后调此函数,
    使下次 _get_semaphore() 按新并发值重建;也用于隔离各测试的信号量状态。
    """
    global _sem
    _sem = None
