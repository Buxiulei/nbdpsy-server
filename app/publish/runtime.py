"""发布调度器运行时单例:桥接 lifespan(生产者)与 publish 端点(消费者)。

REST 端点函数拿不到 FastAPI ``app.state``(handler 签名不接 Request/app);用模块级单例把
lifespan 启动的 ``PublishScheduler`` 交给端点,让 ``publish_note_endpoint`` 能把无定时发布
任务立即投入调度器内部队列(免等下个 scan 周期)。

- ``set_active_scheduler``:lifespan 启动时置入,shutdown 时置回 None。
- ``get_active_scheduler``:端点读取;未初始化时抛 RuntimeError(不应在无调度器时被调用)。

测试可 ``set_active_scheduler(假对象)`` 注入一个只记录 submit 的假调度器,断言入队行为
而不起真实后台循环 / 浏览器。
"""

from app.publish.scheduler import PublishScheduler

# 当前活跃调度器;None 表示尚未初始化(进程未起 lifespan / 已 shutdown)。
_active_scheduler: "PublishScheduler | None" = None


def set_active_scheduler(scheduler: "PublishScheduler | None") -> None:
    """置入 / 清空当前活跃调度器(lifespan 启停调用)。"""
    global _active_scheduler
    _active_scheduler = scheduler


def get_active_scheduler() -> "PublishScheduler":
    """取当前活跃调度器;未初始化时抛 RuntimeError。"""
    if _active_scheduler is None:
        raise RuntimeError("发布调度器未初始化,无法投递发布任务")
    return _active_scheduler
