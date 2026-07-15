"""孤儿 camoufox 进程周期回收:杀掉"无主 + 超龄"的残留浏览器,防内存泄露。

浏览器操作(发布 / cookie 检测)崩溃或超时被打断时,camoufox 子进程可能变成孤儿
残留在系统里,长期堆积吃满内存。本模块周期扫 ``/proc``,对每个 camoufox 进程:

1. 从 argv 里的 profile 路径 ``DATA_DIR/browser/account_{id}`` 反解出它属于哪个账号;
2. 若该账号的 ``account_locks`` 锁**未持有**(说明当前没有在跑的浏览器操作占用它),
   **且**进程存活已超过 ``settings.BROWSER_REAP_AGE`` 秒(排除刚起还没抢到锁的新进程);
3. → SIGKILL 回收。

三条件缺一不杀,尤其"锁被持有"绝不杀 —— 正在发帖/检测的浏览器必须保命,防误杀。
纯判定 ``should_reap`` 抽出可单测;``reap_once`` 全程 try/except,单进程失败不中断其余,
``BrowserReaper`` 后台循环单轮异常不崩 loop(对齐 ``CookieChecker`` 的 start/stop 结构)。

设计对齐 ``CookieChecker``:``BROWSER_REAP_INTERVAL=0`` 时 lifespan 不起该循环;循环
**先睡后扫**,故默认间隔(300s)下,毫秒级进出 lifespan 的单测永不真正触发 /proc 回收。
"""

import asyncio
import os
import signal
import time

from loguru import logger

from app.browser.account_locks import account_locks
from app.browser.profile_guard import browser_profiles_root, iter_camoufox_procs
from app.core.config import settings


def should_reap(
    is_camoufox: bool, locked: bool, age_seconds: float, age_threshold: float
) -> bool:
    """纯判定:是否应回收该进程(三条件缺一不杀,便于单测)。

    - ``is_camoufox``:必须是 camoufox 进程(非 camoufox 一律不碰);
    - ``not locked``:账号锁未持有,即当前没有在跑的浏览器操作(锁持有绝不杀,防误杀在跑链);
    - ``age_seconds > age_threshold``:存活严格超龄(排除刚起还没抢到锁的新进程)。
    """
    return is_camoufox and not locked and age_seconds > age_threshold


def _boot_time() -> float:
    """读 ``/proc/stat`` 的 ``btime`` 得系统启动时刻(epoch 秒),用于换算进程存活时长。"""
    with open("/proc/stat", encoding="utf-8") as f:
        for line in f:
            if line.startswith("btime "):
                return float(line.split()[1])
    raise ValueError("/proc/stat 缺 btime 行")


def _proc_age_seconds(pid: int, boot_time: float, now: float) -> float:
    """由 ``/proc/<pid>/stat`` 的 starttime(字段 22)换算进程已存活秒数。

    comm 字段(字段 2)含括号且可能带空格/括号,故按**最后一个** ``)`` 切分后再
    取空白分隔字段:starttime 是第 22 字段,切分后位于索引 19。失败返回 0(视作"很新",
    不会被超龄判定命中 → 保守不杀)。
    """
    try:
        with open(f"/proc/{pid}/stat", encoding="utf-8") as f:
            data = f.read()
        after_comm = data[data.rfind(")") + 2 :].split()
        starttime_ticks = int(after_comm[19])
        clk_tck = os.sysconf("SC_CLK_TCK")
        start_epoch = boot_time + starttime_ticks / clk_tck
        return now - start_epoch
    except (OSError, ValueError, IndexError):
        return 0.0


def _account_id_from_argv(argv: list[str]) -> int | None:
    """从 argv 中锚定 ``browser_profiles_root()`` 下的 profile 路径,反解 account_id。

    路径约定唯一 owner 为 ``profile_guard.browser_profiles_root()``,精确锚定到
    ``DATA_DIR/browser/`` 前缀再取下一段 ``account_{id}``,避免误命中 argv 里其它含
    ``account_`` 的无关路径。解析不出返回 None(调用方据此保守不杀)。

    路径前缀锚定复用 ``browser_profiles_root()``;因 id 由 argv 中的 profile 路径反解,
    等价于 ``_argv_targets_profile`` 的精确匹配,故不再重复回验。
    """
    base = str(browser_profiles_root())
    prefix = base + os.sep
    for tok in argv:
        norm = os.path.normpath(str(tok))
        if not norm.startswith(prefix):
            continue
        first_segment = norm[len(prefix) :].split(os.sep, 1)[0]
        if first_segment.startswith("account_"):
            suffix = first_segment[len("account_") :]
            if suffix.isdigit():
                return int(suffix)
    return None


def _scan_processes() -> list[tuple[int, bool, int | None, float]]:
    """扫 ``/proc``,返回 camoufox 进程的 ``(pid, is_camoufox, account_id, age_seconds)``。

    枚举复用 ``profile_guard.iter_camoufox_procs``(/proc 迭代 + camoufox 判定为唯一真相);
    account_id 由 profile 路径反解(解析不出为 None);age 由 starttime 换算。
    """
    results: list[tuple[int, bool, int | None, float]] = []
    boot_time = _boot_time()
    now = time.time()
    for pid, argv in iter_camoufox_procs():
        account_id = _account_id_from_argv(argv)
        age = _proc_age_seconds(pid, boot_time, now)
        results.append((pid, True, account_id, age))
    return results


def reap_once() -> int:
    """扫一轮并回收孤儿 camoufox 进程,返回本轮杀掉的进程数。

    对每个进程:account_id 解析不出(None)保守视为"占用中"不杀;否则查该账号锁是否持有,
    经 ``should_reap`` 三条件判定 → SIGKILL。全程 try/except:枚举失败返 0 不冒泡,单进程
    kill 失败(已退/无权限)吞掉不影响其余。
    """
    killed = 0
    try:
        procs = _scan_processes()
    except Exception:
        logger.exception("[browser_reaper] 扫描 /proc 失败,本轮跳过")
        return killed

    threshold = settings.BROWSER_REAP_AGE
    for pid, is_camoufox, account_id, age in procs:
        try:
            # account_id 解析不出时无法确认是否在用,保守当作"锁持有"不杀。
            locked = account_id is None or account_locks.get(account_id).locked()
            if not should_reap(is_camoufox, locked, age, threshold):
                continue
            os.kill(pid, signal.SIGKILL)
            killed += 1
            logger.info(
                f"[browser_reaper] 已回收孤儿 camoufox PID={pid} "
                f"account={account_id} age={age:.0f}s"
            )
        except (ProcessLookupError, PermissionError) as e:
            logger.warning(f"[browser_reaper] 回收进程失败 PID={pid}: {e}")
        except Exception:
            logger.exception(f"[browser_reaper] 处理进程异常 PID={pid}")
    return killed


class BrowserReaper:
    """周期回收后台循环:每 ``interval`` 秒跑一轮 ``reap_once``,单轮异常不打断循环。

    **先睡后扫**:循环开头先 ``_sleep(interval)`` 再回收,故默认间隔(300s)下,毫秒级
    进出 lifespan 的单测不会触发真实 /proc 回收。``stop()`` 优雅取消(可打断 interval 休眠)。
    """

    def __init__(self, interval: float) -> None:
        self._interval = interval
        self._stop_event: asyncio.Event | None = None
        self._loop_task: asyncio.Task | None = None

    def start(self) -> None:
        """启动后台回收循环。"""
        self._stop_event = asyncio.Event()
        self._loop_task = asyncio.create_task(self._run_loop())

    async def _run_loop(self) -> None:
        """后台循环:先睡 interval 再跑一轮回收;单轮异常仅记录不崩循环。"""
        while self._stop_event is not None and not self._stop_event.is_set():
            await self._sleep(self._interval)
            if self._stop_event is not None and self._stop_event.is_set():
                break
            try:
                reap_once()
            except Exception:
                logger.exception("[browser_reaper] 回收轮次异常")

    async def _sleep(self, timeout: float) -> None:
        """可被 stop() 立即打断的休眠;未 start 时退化为普通 sleep。"""
        if self._stop_event is None:
            await asyncio.sleep(timeout)
            return
        try:
            await asyncio.wait_for(self._stop_event.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            pass

    async def stop(self) -> None:
        """优雅停:置停止信号 → 等后台循环退出。"""
        if self._stop_event is not None:
            self._stop_event.set()
        if self._loop_task is not None:
            await self._loop_task
            self._loop_task = None
