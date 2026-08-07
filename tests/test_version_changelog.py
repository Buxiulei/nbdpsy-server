"""`app.__version__` 与 CHANGELOG.md 最新条目的一致性防漂移测试。

为什么值得单独一条测试:``__version__`` 会经 ``GET /api/manifest``、``GET /api/guide``
的 meta、``GET /api/extension`` 三处报给外部。报一个**错的**版本号比不报更糟——调用方
拿它去对代码、对能力面,而它此前静默落后了 CHANGELOG 两个版本(0.17.0 vs 0.19.1),
没有任何东西会红。这条测试就是那个"会红的东西"。

两份变更记录**有意不互相生成**:CHANGELOG.md 是版本 × 长篇施工叙事给人读,guide 的
CHANGELOG_ENTRIES 是日期 × 对调用方影响给机器读,互相生成不是砍掉前者的根因叙述,就是
把后者撑成第二份长文档。但"各写各的"确实是腐烂之源,所以钉住两条**真·不变量**:

1. `__version__` 必须等于 CHANGELOG.md 最新条目的版本号;
2. CHANGELOG.md 里 `CHANGELOG_COVERAGE_SINCE` 之后的**每个版本段,guide 都必须至少有一条
   同日期记录** —— 即"发了版却没在机器可读的那份里告诉调用方"会当场红。

反向(guide 有而 CHANGELOG 没有)刻意**不钉**:同一天多次合并到 main 却只发一个版本是
本仓常态(0.20.0 就折了 12 次上线),要求日期一一对应等于逼人为每次合并造一个版本号。
"""

import re
from datetime import date
from pathlib import Path

from app import __version__

# 仓库根:tests/ 的上一级。不用 cwd —— pytest 可能从任意目录启动。
_CHANGELOG = Path(__file__).resolve().parent.parent / "CHANGELOG.md"

# CHANGELOG 版本行形如:## 0.20.0 (2026-08-07)
_HEADING = re.compile(r"^## (\d+\.\d+\.\d+) \((\d{4}-\d{2}-\d{2})\)\s*$")


def _changelog_headings() -> list[tuple[str, str]]:
    """按文件顺序取出全部 (版本, 日期)。"""
    return [
        (m.group(1), m.group(2))
        for line in _CHANGELOG.read_text(encoding="utf-8").splitlines()
        if (m := _HEADING.match(line))
    ]


def test_changelog_has_parseable_headings():
    """先证明解析本身有效——正则失配会让下面两条测试假绿(空列表恒过)。"""
    headings = _changelog_headings()
    assert len(headings) >= 2, f"CHANGELOG.md 解析不出版本行,解析到 {headings}"


def test_version_matches_latest_changelog_entry():
    """`app.__version__` 必须等于 CHANGELOG.md 最新(最上面)那条的版本号。

    加了 CHANGELOG 段落却忘了改常量 → 这里红;反之亦然。
    """
    latest_version, _ = _changelog_headings()[0]
    assert __version__ == latest_version, (
        f"app.__version__={__version__} 与 CHANGELOG.md 最新条目 {latest_version} 不一致——"
        f"发版时两处必须同笔改"
    )


def test_changelog_versions_unique_and_dates_valid():
    """版本号不重复、日期合法。重复版本号会让上面那条测试钉到一个有歧义的目标。"""
    headings = _changelog_headings()
    versions = [v for v, _ in headings]
    assert len(versions) == len(set(versions)), f"CHANGELOG.md 版本号重复: {versions}"
    for version, day in headings:
        date.fromisoformat(day)  # 非法日期直接抛 ValueError


def test_every_released_version_is_announced_in_guide():
    """每个发布版本(补录下界之后的)都要在 guide 的 changelog 里有同日期记录。

    这是两份记录之间**唯一钉得住又真有意义**的一致性:发了版却没在机器可读的那份里
    告诉调用方,正是本接口要消灭的腐烂。反向不钉——同一天多次合并只发一个版本是常态。
    """
    from app.http.guide import CHANGELOG_COVERAGE_SINCE, CHANGELOG_ENTRIES

    floor = date.fromisoformat(CHANGELOG_COVERAGE_SINCE)
    announced = {e["date"] for e in CHANGELOG_ENTRIES}
    missing = [
        (version, day)
        for version, day in _changelog_headings()
        if date.fromisoformat(day) >= floor and day not in announced
    ]
    assert not missing, (
        f"这些已发布版本在 guide 的 changelog 里没有同日期记录: {missing};"
        f"发版必须同笔往 CHANGELOG_ENTRIES 里加一条(补录下界 {CHANGELOG_COVERAGE_SINCE})"
    )
