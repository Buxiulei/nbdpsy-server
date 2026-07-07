"""profile_guard 纯逻辑单测(不起真浏览器)。

覆盖:
- profile_dir 统一目录约定
- _argv_targets_profile 前缀安全(account_2 不误杀 account_20)
- sanitize_launch_options proxy=None 剔除
- clean_locks / delete_cookies_db 的存在即删 + 缺失不抛
"""
from pathlib import Path

from app.browser import profile_guard
from app.browser.profile_guard import (
    _argv_targets_profile,
    clean_locks,
    delete_cookies_db,
    profile_dir,
    sanitize_launch_options,
)


def test_profile_dir_unified_path(monkeypatch, tmp_path):
    """profile_dir = DATA_DIR/browser/account_{id},只此一套。"""
    from app.core.config import settings

    monkeypatch.setattr(settings, "DATA_DIR", str(tmp_path))
    assert profile_dir(7) == tmp_path / "browser" / "account_7"


def test_argv_targets_profile_prefix_safe(tmp_path):
    """account_2 的 profile 不能匹配含 account_20 的 argv(前缀陷阱)。"""
    pdir2 = tmp_path / "browser" / "account_2"

    # 兄弟号 account_20 的进程 argv —— 绝不能命中 account_2
    argv_20 = ["camoufox-bin", "-profile", str(tmp_path / "browser" / "account_20")]
    assert _argv_targets_profile(argv_20, pdir2) is False

    # 自己的 argv —— 必须精确命中
    argv_2 = ["camoufox-bin", "-profile", str(pdir2)]
    assert _argv_targets_profile(argv_2, pdir2) is True


def test_argv_targets_profile_subpath_hits(tmp_path):
    """profile 目录内的文件路径(子路径)也算命中该 profile。"""
    pdir = tmp_path / "browser" / "account_5"
    argv = ["camoufox-bin", str(pdir / "prefs.js")]
    assert _argv_targets_profile(argv, pdir) is True


def test_argv_targets_profile_accepts_null_separated_string(tmp_path):
    """/proc cmdline 是 \\x00 分隔的字符串,也要能解析。"""
    pdir = tmp_path / "browser" / "account_3"
    raw = "\x00".join(["camoufox-bin", "-profile", str(pdir)])
    assert _argv_targets_profile(raw, pdir) is True

    raw_sibling = "\x00".join(
        ["camoufox-bin", "-profile", str(tmp_path / "browser" / "account_30")]
    )
    assert _argv_targets_profile(raw_sibling, pdir) is False


def test_sanitize_pops_none_proxy():
    """proxy=None → 剔除该键(Firefox 把 None 当空代理会拒连)。"""
    out = sanitize_launch_options({"proxy": None, "a": 1})
    assert "proxy" not in out
    assert out["a"] == 1


def test_sanitize_keeps_real_proxy():
    """proxy 非 None → 原样保留。"""
    opts = {"proxy": {"server": "http://127.0.0.1:10808"}, "a": 1}
    out = sanitize_launch_options(opts)
    assert out["proxy"] == {"server": "http://127.0.0.1:10808"}


def test_sanitize_does_not_mutate_input():
    """不应就地修改调用方传入的 dict。"""
    src = {"proxy": None, "a": 1}
    sanitize_launch_options(src)
    assert "proxy" in src  # 原 dict 未被改动


def test_clean_locks_removes_existing(tmp_path):
    """lock / .parentlock 存在 → 删除。"""
    (tmp_path / "lock").write_text("x")
    (tmp_path / ".parentlock").write_text("x")
    clean_locks(tmp_path)
    assert not (tmp_path / "lock").exists()
    assert not (tmp_path / ".parentlock").exists()


def test_clean_locks_removes_dangling_symlink(tmp_path):
    """Firefox 的 lock 是符号链接,悬空(指向不存在目标)时也要能删。"""
    link = tmp_path / "lock"
    link.symlink_to(tmp_path / "does-not-exist")
    assert link.is_symlink()
    clean_locks(tmp_path)
    assert not link.is_symlink()


def test_clean_locks_absent_no_raise(tmp_path):
    """锁文件不存在 → 静默返回,不抛异常。"""
    clean_locks(tmp_path)  # 不应抛


def test_delete_cookies_db_removes(tmp_path):
    """cookies.sqlite 及 WAL/SHM 边车文件一并删除(防 WAL 回放旧 cookie)。"""
    (tmp_path / "cookies.sqlite").write_text("x")
    (tmp_path / "cookies.sqlite-wal").write_text("x")
    (tmp_path / "cookies.sqlite-shm").write_text("x")
    delete_cookies_db(tmp_path)
    assert not (tmp_path / "cookies.sqlite").exists()
    assert not (tmp_path / "cookies.sqlite-wal").exists()
    assert not (tmp_path / "cookies.sqlite-shm").exists()


def test_delete_cookies_db_absent_no_raise(tmp_path):
    """cookies.sqlite 不存在 → 静默返回。"""
    delete_cookies_db(tmp_path)  # 不应抛


def test_kill_orphans_no_match_no_raise(tmp_path, monkeypatch):
    """真实扫 /proc 但没有匹配进程时,不应抛异常(冒烟)。"""
    pdir = tmp_path / "browser" / "account_999999"
    # 不 mock /proc:环境里不会有指向该临时 profile 的 camoufox 进程
    profile_guard.kill_orphans(pdir)
