"""发布调度器运行时单例:桥接 lifespan(生产者)与 publish 端点(消费者)。

REST 端点函数拿不到 FastAPI ``app.state``(handler 签名不接 Request/app);用模块级单例把
进程内调度器交给端点,让 ``publish_note_endpoint`` 能把无定时发布任务立即投入调度器内部
队列(免等下个 scan 周期)。

api/worker 进程拆分(设计 2026-07-24)后,nudge 改为**可空**:API 进程不再持有进程内
调度器,常态即 None —— 端点取到 None 时静默跳过立即投递,发布由 worker 进程的 5s 扫描
兜底(最坏多等 5s,换来进程解耦)。

- ``set_active_scheduler``:置入 / 清空(单进程 all 模式或测试注入时使用)。
- ``get_active_scheduler``:端点读取;常态返回 None,调用方须判空。

测试可 ``set_active_scheduler(假对象)`` 注入一个只记录 submit 的假调度器,断言入队行为
而不起真实后台循环 / 浏览器。
"""

from app.publish.scheduler import PublishScheduler

# 当前活跃调度器;None 为常态(api/worker 拆分后 API 进程不持调度器,扫描兜底)。
_active_scheduler: "PublishScheduler | None" = None


def set_active_scheduler(scheduler: "PublishScheduler | None") -> None:
    """置入 / 清空当前活跃调度器(单进程 all 模式 / 测试注入时调用)。"""
    global _active_scheduler
    _active_scheduler = scheduler


def get_active_scheduler() -> "PublishScheduler | None":
    """取当前活跃调度器;常态为 None(无进程内调度器,发布由 worker 扫描兜底)。"""
    return _active_scheduler
