#!/usr/bin/env bash
# 生产/本地启动脚本：确保 Xvfb → 迁移 DB → 打包插件 → 起 uvicorn（工厂模式）。
#
# 全程用项目 venv 的 python/uvicorn/alembic，绝不依赖系统解释器。
# 监听地址取环境变量 API_HOST/API_PORT（与 Settings 默认对齐），未设则用默认值。
set -euo pipefail

# 仓库根 = 本脚本所在 scripts/ 的上一级。
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$REPO_ROOT"

VENV="$REPO_ROOT/.venv"
UVICORN="$VENV/bin/uvicorn"
ALEMBIC="$VENV/bin/alembic"

HOST="${API_HOST:-0.0.0.0}"
PORT="${API_PORT:-8848}"

# 0. 确保 Xvfb 起（headless 浏览器发布需要虚拟显示）。起不来不致命，仅告警——
#    RBAC/账号/插件等不碰浏览器的功能仍可用，只有发布链会受影响。
bash "$SCRIPT_DIR/xvfb.sh" start || echo "警告：Xvfb 启动失败，浏览器发布可能不可用" >&2

# 1. DB 迁移到最新（建表 + 增量列，幂等）。
"$ALEMBIC" upgrade head

# 2. 打包 chrome 插件（生成 DATA_DIR/extension.zip，供 /downloads 明文下发）。
bash "$SCRIPT_DIR/pack_extension.sh"

# 3. 起服务（--factory：create_app 是工厂函数，非模块级 app 实例）。
#    exec 让 uvicorn 接管当前进程，信号/退出码直通，便于 systemd 托管。
exec "$UVICORN" app.server:create_app --factory --host "$HOST" --port "$PORT"
