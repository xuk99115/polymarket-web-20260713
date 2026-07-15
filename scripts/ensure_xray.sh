#!/usr/bin/env bash
# ============================================================
# ensure_xray.sh
# 保活本地 SOCKS5 代理 (xray 监听 127.0.0.1:10808)
# 由 start_paper_sim.sh 调用；也可单独运行做健康检查/启动。
#
# 行为：
# 1. 如果 10808 端口已经在 LISTEN → 啥也不做
# 2. 否则从 /data/xray/bin/xray 启动后台进程
# 3. 用 setsid 完全脱离调用 shell
#
# 设计原因：
# - VPS 重启后，/usr/local/bin 等系统路径会重置
# - xray 二进制和配置都在 /data/xray/（永久卷）
# - 这个脚本跟 polymarket_web 一起活在永久卷上
# ============================================================
set -e

XRAY_BIN="/data/xray/bin/xray"
XRAY_CONFIG="/data/xray/config.json"
XRAY_LOG_DIR="/data/xray/log"
XRAY_STDOUT_LOG="$XRAY_LOG_DIR/stdout.log"
PROXY_PORT="${PROXY_PORT:-10808}"
PROXY_HOST="${PROXY_HOST:-127.0.0.1}"
PID_FILE="$XRAY_LOG_DIR/xray.pid"

mkdir -p "$XRAY_LOG_DIR"

log() { echo "[ensure_xray] $*" >&2; }

# 1. 检查二进制
if [[ ! -x "$XRAY_BIN" ]]; then
    log "FATAL: xray not found at $XRAY_BIN"
    exit 1
fi

# 2. 快速端口探测 (用 /dev/tcp bash built-in)
probe_port() {
    local host="$1" port="$2"
    timeout 2 bash -c "exec 3<>/dev/tcp/$host/$port" 2>/dev/null && return 0 || return 1
}

# 3. 如果端口已经在听，啥也不做
if probe_port "$PROXY_HOST" "$PROXY_PORT"; then
    log "OK: xray SOCKS5 already listening on $PROXY_HOST:$PROXY_PORT"
    # 顺便刷新 PID 文件
    pgrep -f "$XRAY_BIN run -c $XRAY_CONFIG" | head -1 > "$PID_FILE" 2>/dev/null || true
    exit 0
fi

# 4. 端口不通 → 启动
log "Starting xray..."
# 校验 config
if ! "$XRAY_BIN" run -c "$XRAY_CONFIG" -test >/dev/null 2>&1; then
    log "FATAL: xray config invalid: $XRAY_CONFIG"
    exit 2
fi

# 用 setsid 完全脱离当前 shell / process group
nohup setsid "$XRAY_BIN" run -c "$XRAY_CONFIG" >> "$XRAY_STDOUT_LOG" 2>&1 &
XRAY_PID=$!
disown $XRAY_PID 2>/dev/null || true
echo "$XRAY_PID" > "$PID_FILE"

# 等一下确认它启动并占住端口
for i in 1 2 3 4 5; do
    if probe_port "$PROXY_HOST" "$PROXY_PORT"; then
        log "OK: xray started, PID=$XRAY_PID, listening on $PROXY_HOST:$PROXY_PORT"
        exit 0
    fi
    sleep 1
done

log "FATAL: xray started but port $PROXY_PORT not listening after 5s"
log "Last lines of stdout.log:"
tail -20 "$XRAY_STDOUT_LOG" >&2
exit 3
