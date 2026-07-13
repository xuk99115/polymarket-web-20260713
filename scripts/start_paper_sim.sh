#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
RUNTIME_DIR="$ROOT_DIR/.runtime"
DATA_DIR="$ROOT_DIR/data"
DOCS_DIR="$ROOT_DIR/docs"
BOT_PID_FILE="$RUNTIME_DIR/paper_bot.pid"
SERVER_PID_FILE="$RUNTIME_DIR/status_server.pid"
BOT_LOG="$RUNTIME_DIR/paper_bot.log"
SERVER_LOG="$RUNTIME_DIR/status_server.log"
STATUS_PORT="${STATUS_PORT:-8889}"

mkdir -p "$RUNTIME_DIR"

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
stop_port_listener "$STATUS_PORT"

# === Ensure local SOCKS5 proxy (xray) is up before bot starts ===
# 2026-07-10: VPS 被 Binance 地理封锁, BTC 价必须通过本地代理拿
# ensure_xray.sh 是 idempotent 的: 端口在听就啥也不做, 否则拉起
bash "$(dirname "$0")/ensure_xray.sh" || { echo "FATAL: xray proxy not available, refusing to start bot"; exit 4; }

archive_previous_run() {
    local archive_root="$ROOT_DIR/history"
    local stamp
    stamp="$(date '+%Y-%m-%d_%H-%M-%S')"
    local archive_dir="$archive_root/$stamp"
    local has_files=0

    mkdir -p "$archive_dir"

    for file in \
        "$DATA_DIR/bot_status.json" \
        "$DATA_DIR/paper_trade_state.json" \
        "$DOCS_DIR/paper_trade_report.md" \
        "$BOT_LOG" \
        "$SERVER_LOG"
    do
        if [[ -f "$file" ]]; then
            cp "$file" "$archive_dir/$(basename "$file")"
            has_files=1
        fi
    done

    if [[ "$has_files" -eq 1 ]]; then
        echo "已归档上一轮记录到: $archive_dir"
    else
        rmdir "$archive_dir" 2>/dev/null || true
    fi
}

archive_previous_run

export TRADING_MODE="${TRADING_MODE:-paper_live}"
export PAPER_START_BALANCE="${PAPER_START_BALANCE:-100}"
export BET_AMOUNT="${BET_AMOUNT:-1}"
export PAPER_BET_AMOUNT="${PAPER_BET_AMOUNT:-1}"
export MAX_BET_AMOUNT="${MAX_BET_AMOUNT:-1}"
export PAPER_MIN_ENTRY_PRICE="${PAPER_MIN_ENTRY_PRICE:-0.15}"
export PAPER_MAX_ENTRY_PRICE="${PAPER_MAX_ENTRY_PRICE:-0.60}"
export PAPER_MAX_SPREAD="${PAPER_MAX_SPREAD:-0.06}"
export PAPER_MIN_TOP_BOOK_SIZE="${PAPER_MIN_TOP_BOOK_SIZE:-25}"
export PAPER_MIN_MINUTES_TO_EXPIRY="${PAPER_MIN_MINUTES_TO_EXPIRY:-3}"
export PAPER_TAKE_PROFIT_USD="${PAPER_TAKE_PROFIT_USD:-0.12}"
export PAPER_POLL_INTERVAL_SECONDS="${PAPER_POLL_INTERVAL_SECONDS:-15}"
export PAPER_MAX_OPEN_POSITIONS="${PAPER_MAX_OPEN_POSITIONS:-1}"
export PAPER_MAX_NEW_POSITIONS_PER_CYCLE="${PAPER_MAX_NEW_POSITIONS_PER_CYCLE:-1}"
export PAPER_MARKET_INTERVAL_MINUTES="${PAPER_MARKET_INTERVAL_MINUTES:-15}"
export PAPER_FORWARD_SLOT_COUNT="${PAPER_FORWARD_SLOT_COUNT:-8}"
export PAPER_WALLET_LABEL="${PAPER_WALLET_LABEL:-LOCAL-SIM-100U}"
export STOP_LOSS_ENABLED="${STOP_LOSS_ENABLED:-false}"
export AI_ENABLED="${AI_ENABLED:-true}"
export AI_DECISION_INTERVAL_SECONDS="${AI_DECISION_INTERVAL_SECONDS:-180}"

cd "$ROOT_DIR"

launch_bg() {
    local log_file="$1"
    shift
    # 用 scripts/daemonize.py 启动命令 (macOS 没有 setsid(1))。
    #
    # 为什么需要 daemonize: 纯 nohup 只能免疫 SIGHUP (终端挂断)。Hermes 每次
    # session 结束时会对整个进程组发 SIGTERM/SIGKILL,纯 nohup 挡不住。
    # daemonize 用经典 double-fork + os.setsid() 让 bot 跳出当前进程组,
    # PPID 变 1 (launchd),完全脱离任何 shell/session 的生命周期。
    #
    # daemonize.py stdout 输出 grandchild PID,bash 用 command substitution 拿到。
    local daemon_pid
    daemon_pid="$("$ROOT_DIR/venv/bin/python3" "$ROOT_DIR/scripts/daemonize.py" "$log_file" "$@")"
    echo "$daemon_pid"
}

BOT_PID="$(launch_bg "$BOT_LOG" ./venv/bin/python3 -u bot.py)"
echo "$BOT_PID" >"$BOT_PID_FILE"

SERVER_PID="$(launch_bg "$SERVER_LOG" ./venv/bin/python3 -u src/server/status_server.py)"
echo "$SERVER_PID" >"$SERVER_PID_FILE"

sleep 2

if ! kill -0 "$BOT_PID" 2>/dev/null; then
    echo "Bot 启动失败"
    tail -n 50 "$BOT_LOG" || true
    exit 1
fi

if ! kill -0 "$SERVER_PID" 2>/dev/null; then
    echo "监控服务启动失败"
    tail -n 50 "$SERVER_LOG" || true
    exit 1
fi

echo "模拟交易已启动"
echo "Bot PID: $BOT_PID"
echo "Server PID: $SERVER_PID"
echo "Dashboard: http://localhost:${STATUS_PORT}"
echo "Bot Log: $BOT_LOG"
echo "Server Log: $SERVER_LOG"
