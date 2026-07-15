#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
RUNTIME_DIR="$ROOT_DIR/.runtime"
BOT_PID_FILE="$RUNTIME_DIR/paper_bot.pid"
SERVER_PID_FILE="$RUNTIME_DIR/status_server.pid"

force_stop_pid() {
    local pid="$1"
    local label="${2:-process}"
    if [[ -z "${pid:-}" ]] || ! kill -0 "$pid" 2>/dev/null; then
        return 0
    fi

    kill -TERM "$pid" 2>/dev/null || true
    for _ in 1 2 3 4 5; do
        if ! kill -0 "$pid" 2>/dev/null; then
            return 0
        fi
        sleep 1
    done

    if kill -0 "$pid" 2>/dev/null; then
        echo "强制停止残留 ${label} PID: $pid"
        kill -KILL "$pid" 2>/dev/null || true
        sleep 1
    fi
}

stop_pid_file() {
    local pid_file="$1"
    if [[ -f "$pid_file" ]]; then
        local pid
        pid="$(cat "$pid_file")"
        if [[ -n "${pid:-}" ]] && kill -0 "$pid" 2>/dev/null; then
            force_stop_pid "$pid" "$(basename "$pid_file")"
        fi
        rm -f "$pid_file"
    fi
}

stop_port_listener() {
    local port="$1"
    local pids
    pids="$(lsof -tiTCP:"$port" -sTCP:LISTEN 2>/dev/null || true)"
    if [[ -n "${pids:-}" ]]; then
        for pid in $pids; do
            force_stop_pid "$pid" "port-$port-listener"
        done
    fi
}

stop_pid_file "$BOT_PID_FILE"
stop_pid_file "$SERVER_PID_FILE"
stop_port_listener 8889

echo "模拟交易与监控服务已停止"
