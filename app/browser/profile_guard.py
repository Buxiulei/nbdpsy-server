"""Camoufox/Firefox 持久化 profile 的守护工具。

移植自旧仓 ``backend/app/utils/camoufox_helper.py`` 的四件套,并统一目录约定:

- profile 目录统一为 ``DATA_DIR/browser/account_{id}`` 一套
  (旧仓存在 ``account_{id}`` 与 ``camoufox_account_{id}`` 两套分裂目录,新仓收敛)。
- 启动前清残留锁(``lock`` / ``.parentlock``),否则 Firefox 死等锁释放到超时。
- 启动前删 ``cookies.sqlite``,否则持久上下文旧 cookie 覆盖新注入 → 登成别人号。
- 精确杀占用该 profile 的 camoufox-bin 孤儿进程。
  关键坑:旧仓用 ``pgrep -f 'camoufox-bin.*{profile}'`` 做子串匹配,
  ``account_2`` 是 ``account_20`` 的前缀会误杀兄弟号。本模块改为逐 token
  精确匹配(见 ``_argv_targets_profile``),从根上杜绝误杀。
- ``proxy=None`` 键剔除:Firefox 把 None 当空代理配置 → 连接被拒。

纯逻辑函数(``_argv_targets_profile`` / ``sanitize_launch_options`` /
``clean_locks`` / ``delete_cookies_db``)不依赖真实进程或浏览器,可直接单测。
"""
import os
import signal
from pathlib import Path
from typing import List, Union

from loguru import logger

from app.core.config import settings

# Firefox profile 锁文件名(lock 为符号链接,.parentlock 为空文件)
_LOCK_FILES = ("lock", ".parentlock")
# 需一并清理的 cookie 数据库及其 WAL/SHM 边车(否则 WAL 可回放出旧 cookie)
_COOKIE_FILES = ("cookies.sqlite", "cookies.sqlite-wal", "cookies.sqlite-shm")


def profile_dir(account_id: int) -> Path:
    """返回账号的统一 profile 目录 ``DATA_DIR/browser/account_{id}``。

    纯路径计算,不创建目录(创建交给真正要落盘的调用方,便于测试隔离)。
    """
    return Path(settings.DATA_DIR) / "browser" / f"account_{account_id}"


def clean_locks(profile_dir: Path) -> None:
    """清除残留的 Firefox profile 锁文件(存在才删,缺失不报错)。

    上一次浏览器崩溃/超时退出后,``lock`` 与 ``.parentlock`` 不会被自动清理,
    下次启动同一 profile 会死等锁释放直到超时。``lock`` 是符号链接,悬空时
    ``exists()`` 返回 False,故需一并判断 ``is_symlink()``。
    """
    for name in _LOCK_FILES:
        lock_path = profile_dir / name
        try:
            if lock_path.exists() or lock_path.is_symlink():
                lock_path.unlink()
                logger.info(f"[profile_guard] 已清除残留锁文件: {lock_path}")
        except OSError as e:
            logger.warning(f"[profile_guard] 清除锁文件失败: {lock_path} - {e}")


def delete_cookies_db(profile_dir: Path) -> None:
    """启动前删除 ``cookies.sqlite``(含 WAL/SHM 边车),存在才删,缺失不报错。

    持久化上下文会保留上次会话的 cookie,不清则旧 cookie 可能覆盖新注入 →
    登录成别人的账号。同时删 ``-wal`` / ``-shm``,防止 WAL 日志回放出旧 cookie。
    """
    for name in _COOKIE_FILES:
        cookie_path = profile_dir / name
        try:
            if cookie_path.exists():
                cookie_path.unlink()
                logger.info(f"[profile_guard] 已删除旧 cookie 文件: {cookie_path}")
        except OSError as e:
            logger.warning(f"[profile_guard] 删除 cookie 文件失败: {cookie_path} - {e}")


def sanitize_launch_options(opts: dict) -> dict:
    """规整 Camoufox 启动选项:``proxy`` 为 None 则剔除该键。

    ``launch_options()`` 默认返回 ``proxy=None``,而 Firefox 的
    ``launch_persistent_context`` 收到 ``proxy=None`` 会误解为空代理配置,
    触发 ``NS_ERROR_PROXY_CONNECTION_REFUSED``,必须删除此键。

    返回浅拷贝,不就地修改调用方传入的 dict。
    """
    result = dict(opts)
    if result.get("proxy") is None:
        result.pop("proxy", None)
    return result


def _tokenize(argv: Union[str, List[str]]) -> List[str]:
    """把 argv 归一化为 token 列表。

    - list/tuple:逐项转字符串。
    - str:兼容 ``/proc/<pid>/cmdline`` 的 ``\\x00`` 分隔与普通空白分隔。
    """
    if isinstance(argv, (list, tuple)):
        return [str(t) for t in argv]
    return str(argv).replace("\x00", " ").split()


def _argv_targets_profile(argv: Union[str, List[str]], profile_dir: Path) -> bool:
    """判定某进程 argv 是否精确占用指定 profile 目录(纯函数,可单测)。

    精确匹配而非子串匹配:某个 argv token 必须**恰好等于**该 profile 目录,
    或是其子路径(``token == dir`` 或 ``token`` 以 ``dir + os.sep`` 开头)。
    这样 ``account_2`` 不会误命中 ``account_20``(前缀陷阱)。
    """
    target = os.path.normpath(str(profile_dir))
    prefix = target + os.sep
    for tok in _tokenize(argv):
        norm = os.path.normpath(tok)
        if norm == target or norm.startswith(prefix):
            return True
    return False


def kill_orphans(profile_dir: Path) -> None:
    """精确杀占用该 profile 的 camoufox-bin 孤儿进程。

    扫描 ``/proc/<pid>/cmdline``,仅当进程是 camoufox 且其 argv 经
    ``_argv_targets_profile`` 精确命中本 profile 时才 SIGKILL。逐 token
    精确匹配,杜绝 ``account_2`` 误杀 ``account_20`` 的前缀陷阱。
    """
    proc_root = Path("/proc")
    for entry in proc_root.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            raw = (entry / "cmdline").read_bytes()
        except (FileNotFoundError, ProcessLookupError, PermissionError):
            continue
        if not raw:
            continue
        argv = raw.decode("utf-8", "replace").split("\x00")
        # 仅针对 camoufox-bin 进程(argv[0] 为可执行路径)
        if "camoufox" not in argv[0]:
            continue
        if not _argv_targets_profile(argv, profile_dir):
            continue
        pid = int(entry.name)
        try:
            os.kill(pid, signal.SIGKILL)
            logger.info(f"[profile_guard] 已强杀 camoufox 孤儿进程 PID={pid} (profile={profile_dir})")
        except (ProcessLookupError, PermissionError) as e:
            logger.warning(f"[profile_guard] 强杀进程失败 PID={pid}: {e}")
