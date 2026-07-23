#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
RUNTIME_DIR="$ROOT_DIR/.runtime"
BOT_PID_FILE="$RUNTIME_DIR/paper_bot.pid"
SERVER_PID_FILE="$RUNTIME_DIR/status_server.pid"
BOT_PID_FILE_TMP="/tmp/polymarket-fv-edge/paper_bot.pid"
SERVER_PID_FILE_TMP="/tmp/polymarket-fv-edge/status_server.pid"
STATUS_PORT="${STATUS_PORT:-8889}"

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
        pid="$(cat "$pid_file" 2>/dev/null || true)"
        if [[ -n "${pid:-}" ]] && kill -0 "$pid" 2>/dev/null; then
            force_stop_pid "$pid" "$(basename "$pid_file")"
        fi
        rm -f "$pid_file" 2>/dev/null || true
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

stop_matching_python() {
    local needle="$1"
    python3 - "$needle" <<'PY'
import os, signal, sys, time
from pathlib import Path
needle = sys.argv[1]
killed = []
for p in Path("/proc").iterdir():
    if not p.name.isdigit():
        continue
    try:
        args = [x.decode("utf-8", "replace") for x in (p / "cmdline").read_bytes().split(b"\0") if x]
        comm = (p / "comm").read_text().strip()
        stat = (p / "stat").read_text().split()[2]
    except Exception:
        continue
    if stat == "Z" or not comm.startswith("python"):
        continue
    if any(a.endswith(needle) for a in args):
        pid = int(p.name)
        try:
            os.kill(pid, signal.SIGTERM)
            killed.append(pid)
        except OSError:
            pass
if killed:
    time.sleep(1)
    for pid in killed:
        try:
            os.kill(pid, 0)
        except OSError:
            continue
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass
    print("stopped", needle, killed)
PY
}

stop_pid_file "$BOT_PID_FILE"
stop_pid_file "$SERVER_PID_FILE"
stop_pid_file "$BOT_PID_FILE_TMP"
stop_pid_file "$SERVER_PID_FILE_TMP"
stop_matching_python "bot.py"
stop_matching_python "status_server.py"
stop_port_listener "$STATUS_PORT"

echo "模拟交易与监控服务已停止"
