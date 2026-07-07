#!/usr/bin/env bash
# 把 chrome-extension/ 目录内容打包成 DATA_DIR/extension.zip。
#
# 幂等、可重复跑:每次先删旧 zip 再重建,产出的 zip 根目录即插件目录内容
# (manifest.json 在 zip 根),用户解压后可直接在 chrome://extensions 加载已解压的扩展。
#
# DATA_DIR:优先取环境变量(与 app.core.config.Settings.DATA_DIR 对齐),默认仓库根下 ./data。
set -euo pipefail

# 仓库根 = 本脚本所在 scripts/ 的上一级。
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

EXT_DIR="$REPO_ROOT/chrome-extension"
DATA_DIR="${DATA_DIR:-$REPO_ROOT/data}"
OUT="$DATA_DIR/extension.zip"

if [ ! -d "$EXT_DIR" ]; then
    echo "错误:插件目录不存在:$EXT_DIR" >&2
    exit 1
fi

mkdir -p "$DATA_DIR"
rm -f "$OUT"

# 进插件目录后打包其内容(而非把 chrome-extension/ 这层目录名裹进去)。
# -r 递归、-q 静默、-X 不存额外文件属性;排除 macOS 噪声文件。
( cd "$EXT_DIR" && zip -r -q -X "$OUT" . -x '*.DS_Store' )

echo "已打包插件:$OUT"
