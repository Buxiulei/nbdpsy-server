#!/usr/bin/env bash
# 启停 Xvfb 虚拟显示（headless 浏览器自动化用）。子命令 start/stop/status，幂等可重复跑。
#
# display 从 XVFB_DISPLAY 取（与 app.core.config.Settings.XVFB_DISPLAY 对齐），默认 :99。
# 分辨率写死 1920x1080x24（与发布客户端指纹/窗口尺寸预期一致）。
set -euo pipefail

DISPLAY_NUM="${XVFB_DISPLAY:-:99}"
SCREEN="1920x1080x24"

# 取当前该 display 上 Xvfb 进程的 pid（可能多行；无则空）。带尾随空格避免 :9 误匹配 :99。
_pids() {
    pgrep -f "Xvfb ${DISPLAY_NUM} " || true
}

case "${1:-}" in
    start)
        if [ -n "$(_pids)" ]; then
            echo "Xvfb ${DISPLAY_NUM} 已在运行 (pid $(_pids | tr '\n' ' '))"
            exit 0
        fi
        Xvfb "${DISPLAY_NUM}" -screen 0 "${SCREEN}" >/dev/null 2>&1 &
        # 等待其真正就绪（最多 ~3s），避免调用方拿到未起好的 display。
        for _ in $(seq 1 30); do
            [ -n "$(_pids)" ] && break
            sleep 0.1
        done
        if [ -n "$(_pids)" ]; then
            echo "已启动 Xvfb ${DISPLAY_NUM} (${SCREEN})"
        else
            echo "错误：Xvfb ${DISPLAY_NUM} 启动失败" >&2
            exit 1
        fi
        ;;
    stop)
        pids="$(_pids)"
        if [ -z "$pids" ]; then
            echo "Xvfb ${DISPLAY_NUM} 未在运行"
            exit 0
        fi
        # shellcheck disable=SC2086
        kill $pids
        echo "已停止 Xvfb ${DISPLAY_NUM}"
        ;;
    status)
        pids="$(_pids)"
        if [ -n "$pids" ]; then
            echo "Xvfb ${DISPLAY_NUM} 运行中 (pid $(echo "$pids" | tr '\n' ' '))"
        else
            echo "Xvfb ${DISPLAY_NUM} 未运行"
            exit 1
        fi
        ;;
    *)
        echo "用法: $0 {start|stop|status}" >&2
        exit 2
        ;;
esac
