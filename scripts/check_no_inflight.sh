#!/usr/bin/env bash
# check_no_inflight.sh —— 部署纪律工具:重启 worker 前确认没有在跑/排队的浏览器任务。
#
# 查两张台账:
#   publish_jobs  status IN (pending, publishing)
#   browser_jobs  status IN (queued, running)
# 计数为 0 → 退出码 0(可以安全 restart nbdpsy-worker);
# 计数非 0 → 退出码 1 并打印每行明细(id/kind/账号/状态/创建时间)。
#
# 用法:
#   bash scripts/check_no_inflight.sh [db 路径]
#   db 路径缺省为 <仓库根>/data/nbdpsy.db(按脚本自身位置推导,worktree 里也正确)。
#
# 实现走 venv python 的 sqlite3 标准库(本机未装 sqlite3 CLI);只读打开,不碰任何写。
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DB_PATH="${1:-$REPO_ROOT/data/nbdpsy.db}"
PYTHON="$REPO_ROOT/.venv/bin/python"
# worktree 副本没有 venv 时退回主检出解释器(仅标准库,无项目依赖)
[ -x "$PYTHON" ] || PYTHON="/home/roots/nbdpsy-server/.venv/bin/python"

if [ ! -f "$DB_PATH" ]; then
    echo "错误:数据库文件不存在:$DB_PATH" >&2
    exit 2
fi

exec "$PYTHON" - "$DB_PATH" <<'PYEOF'
import sqlite3
import sys

db_path = sys.argv[1]
# 只读打开:部署检查绝不能对生产库产生写入
conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
conn.row_factory = sqlite3.Row

rows: list[tuple[str, str, str, str, str]] = []  # (台账, id, kind/标题, 账号, 状态+时间)

for row in conn.execute(
    "SELECT id, account_id, status, created_at FROM publish_jobs "
    "WHERE status IN ('pending', 'publishing') ORDER BY id"
):
    rows.append((
        "publish_jobs", str(row["id"]), "publish",
        str(row["account_id"]), f"{row['status']} @ {row['created_at']}",
    ))

try:
    for row in conn.execute(
        "SELECT id, kind, account_id, status, created_at FROM browser_jobs "
        "WHERE status IN ('queued', 'running') ORDER BY created_at, id"
    ):
        rows.append((
            "browser_jobs", str(row["id"]), str(row["kind"]),
            str(row["account_id"]), f"{row['status']} @ {row['created_at']}",
        ))
except sqlite3.OperationalError as exc:
    if "no such table" in str(exc):
        # browser_jobs 迁移尚未部署(P1 之前的库):只按 publish_jobs 判定,提示一句
        print("提示:browser_jobs 表不存在(迁移未到位),仅检查 publish_jobs", file=sys.stderr)
    else:
        raise

conn.close()

if not rows:
    print("OK:无在跑/排队任务,可以安全重启 worker")
    sys.exit(0)

print(f"发现 {len(rows)} 个未完成任务,不要重启 worker:")
print(f"{'台账':<14}{'id':<40}{'kind':<14}{'账号':<8}状态")
for ledger, job_id, kind, account, state in rows:
    print(f"{ledger:<14}{job_id:<40}{kind:<14}{account:<8}{state}")
sys.exit(1)
PYEOF
