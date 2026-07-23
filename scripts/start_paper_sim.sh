#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
# 解释器放 /tmp：/data FUSE 上的 venv 原生 .so（pycryptodome）会 EIO 秒退。
TMP_VENV="${TMP_VENV:-/tmp/polymarket-fv-edge-venv}"
PROJECT_VENV="$ROOT_DIR/venv"
REQ_FILE="$ROOT_DIR/requirements.txt"
MARKER_FILE="$TMP_VENV/.requirements.cksum"

RUNTIME_STATE_DIR="${RUNTIME_DIR:-/tmp/polymarket-fv-edge/data}"
RUNTIME_LOG_DIR="${RUNTIME_LOG_DIR:-/tmp/polymarket-fv-edge/logs}"
PERSIST_DIR="${PERSIST_DIR:-$ROOT_DIR/data}"
LOCAL_RUNTIME_DIR="$ROOT_DIR/.runtime"

BOT_PID_FILE="$LOCAL_RUNTIME_DIR/paper_bot.pid"
SERVER_PID_FILE="$LOCAL_RUNTIME_DIR/status_server.pid"
BOT_PID_FILE_TMP="$RUNTIME_STATE_DIR/../paper_bot.pid"
SERVER_PID_FILE_TMP="$RUNTIME_STATE_DIR/../status_server.pid"
# normalize ../ above
BOT_PID_FILE_TMP="/tmp/polymarket-fv-edge/paper_bot.pid"
SERVER_PID_FILE_TMP="/tmp/polymarket-fv-edge/status_server.pid"

BOT_LOG="$RUNTIME_LOG_DIR/paper_bot.stdout.log"
SERVER_LOG="$RUNTIME_LOG_DIR/status_server.log"
STATUS_PORT="${STATUS_PORT:-8889}"

mkdir -p "$LOCAL_RUNTIME_DIR" "$RUNTIME_LOG_DIR" "$RUNTIME_STATE_DIR" /tmp/polymarket-fv-edge

export RUNTIME_DIR="$RUNTIME_STATE_DIR"
export PERSIST_DIR
export RUNTIME_LOG_DIR
export PYTHONDONTWRITEBYTECODE=1
export PYTHONPYCACHEPREFIX="${PYTHONPYCACHEPREFIX:-/tmp/polymarket-pycache}"
mkdir -p "$PYTHONPYCACHEPREFIX"

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
    # 按真实 cmdline 收尾，兼容 python -u bot.py；不匹配 bash wrapper。
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

ensure_tmp_venv() {
    local py="$TMP_VENV/bin/python3"
    local pip="$TMP_VENV/bin/pip"
    local need_install=0

    if [[ ! -x "$py" ]]; then
        echo "📦 创建 /tmp venv: $TMP_VENV"
        python3 -m venv "$TMP_VENV"
        need_install=1
    fi

    if [[ ! -f "$REQ_FILE" ]]; then
        echo "❌ 缺少 requirements.txt: $REQ_FILE"
        exit 1
    fi

    local req_sum
    req_sum="$(cksum "$REQ_FILE" 2>/dev/null | cut -d' ' -f1 || true)"
    if [[ "$need_install" -eq 1 ]] || [[ ! -f "$MARKER_FILE" ]] || [[ "$(cat "$MARKER_FILE" 2>/dev/null || true)" != "$req_sum" ]]; then
        echo "🛠️  安装/同步依赖到 $TMP_VENV ..."
        "$pip" install --quiet --upgrade pip
        "$pip" install --quiet -r "$REQ_FILE"
        printf '%s\n' "$req_sum" >"$MARKER_FILE"
        echo "   ✓ 依赖就绪"
    else
        echo "   ✓ /tmp venv 依赖已是最新"
    fi

    # 冒烟：原生扩展必须能从 /tmp 加载，否则宁可不启动
    if ! "$py" - <<'PY'
from Crypto.Cipher import AES
from Crypto.Util import _cpu_features
assert AES.block_size == 16
import py_clob_client  # noqa: F401
print("tmp_venv_smoke_ok")
PY
    then
        echo "❌ /tmp venv 冒烟失败（Crypto/py_clob_client）。拒绝回退到 /data venv。"
        echo "   可手动: $TMP_VENV/bin/pip install -r $REQ_FILE"
        exit 4
    fi

    PYTHON_BIN="$py"
}

prestart_protect_runtime() {
    # Runtime 更新时先 R->P，避免旧 persist 被误当恢复源。
    "$PYTHON_BIN" - <<'PY'
import json
import os
from pathlib import Path

runtime = Path(os.environ["RUNTIME_DIR"])
persist = Path(os.environ["PERSIST_DIR"])
rt = runtime / "paper_trade_state.json"
pt = persist / "paper_trade_state.json"
rs = runtime / "state_summary.json"
ps = persist / "state_summary.json"

def load(path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None

rt_s, pt_s = load(rs), load(ps)
if rt_s and pt_s:
    rc = float(rt_s.get("cash_balance") or 0)
    pc = float(pt_s.get("cash_balance") or 0)
    rt_n = int(rt_s.get("total_trades") or 0)
    pt_n = int(pt_s.get("total_trades") or 0)
    print(f"prestart cash runtime={rc} persist={pc} trades runtime={rt_n} persist={pt_n}")
    if (rc > pc + 1e-9) or (rt_n > pt_n):
        from src.core.sync_runtime import force_sync
        n = force_sync(str(runtime), str(persist))
        print(f"prestart force R->P files={n}")
        pt_s2 = load(ps) or {}
        if abs(float(pt_s2.get("cash_balance") or 0) - rc) > 1e-6:
            print("WARNING: persist cash still behind after force_sync; continuing with runtime authority")
elif rt.exists() and not pt.exists():
    print("prestart: persist missing paper state; runtime will be used")
elif (not rt.exists()) and pt.exists():
    print("WARNING: runtime missing paper_trade_state.json; bot may restore/create empty state")
PY
}

# --- stop previous ---
stop_pid_file "$BOT_PID_FILE"
stop_pid_file "$SERVER_PID_FILE"
stop_pid_file "$BOT_PID_FILE_TMP"
stop_pid_file "$SERVER_PID_FILE_TMP"
stop_matching_python "bot.py"
stop_matching_python "status_server.py"
stop_port_listener "$STATUS_PORT"

# === Ensure local SOCKS5 proxy (xray) is up before bot starts ===
bash "$(dirname "$0")/ensure_xray.sh" || { echo "FATAL: xray proxy not available, refusing to start bot"; exit 4; }

archive_previous_run() {
    local archive_root="$ROOT_DIR/history"
    local stamp
    stamp="$(date '+%Y-%m-%d_%H-%M-%S')"
    local archive_dir="$archive_root/$stamp"
    local has_files=0

    mkdir -p "$archive_dir"

    for file in \
        "$RUNTIME_STATE_DIR/bot_status.json" \
        "$RUNTIME_STATE_DIR/paper_trade_state.json" \
        "$RUNTIME_STATE_DIR/state_summary.json" \
        "$PERSIST_DIR/bot_status.json" \
        "$PERSIST_DIR/paper_trade_state.json" \
        "$ROOT_DIR/docs/paper_trade_report.md" \
        "$BOT_LOG" \
        "$SERVER_LOG"
    do
        if [[ -f "$file" ]]; then
            cp "$file" "$archive_dir/$(basename "$file")" 2>/dev/null || true
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
ensure_tmp_venv

export TRADING_MODE="${TRADING_MODE:-paper_live}"
export PAPER_START_BALANCE="${PAPER_START_BALANCE:-100}"
export PAPER_WALLET_LABEL="${PAPER_WALLET_LABEL:-LOCAL-FV-EDGE}"
export FV_EDGE_POSITION_USD="${FV_EDGE_POSITION_USD:-2.0}"
export FV_EDGE_THRESHOLD_BPS="${FV_EDGE_THRESHOLD_BPS:-300}"
export FV_EDGE_UP_THRESHOLD_BPS="${FV_EDGE_UP_THRESHOLD_BPS:-1200}"
export FV_EDGE_DOWN_THRESHOLD_BPS="${FV_EDGE_DOWN_THRESHOLD_BPS:-800}"
export FV_EDGE_MAX_MTE="${FV_EDGE_MAX_MTE:-1.5}"
export FV_DIRECTION_MODE="${FV_DIRECTION_MODE:-shadow}"
export STATUS_PORT
export STATUS_BIND_HOST="${STATUS_BIND_HOST:-127.0.0.1}"

cd "$ROOT_DIR"
prestart_protect_runtime

launch_bg() {
    local log_file="$1"
    shift
    # daemonize: double-fork + setsid，脱离 Hermes session 生命周期
    local daemon_pid
    daemon_pid="$("$PYTHON_BIN" "$ROOT_DIR/scripts/daemonize.py" "$log_file" "$@")"
    echo "$daemon_pid"
}

write_pid() {
    local pid="$1"
    shift
    for f in "$@"; do
        printf '%s\n' "$pid" >"$f" 2>/dev/null || true
    done
}

echo "使用解释器: $PYTHON_BIN"
BOT_PID="$(launch_bg "$BOT_LOG" "$PYTHON_BIN" -u "$ROOT_DIR/bot.py")"
write_pid "$BOT_PID" "$BOT_PID_FILE" "$BOT_PID_FILE_TMP"

SERVER_PID="$(launch_bg "$SERVER_LOG" "$PYTHON_BIN" -u "$ROOT_DIR/src/server/status_server.py")"
write_pid "$SERVER_PID" "$SERVER_PID_FILE" "$SERVER_PID_FILE_TMP"

sleep 3

if ! kill -0 "$BOT_PID" 2>/dev/null; then
    echo "Bot 启动失败 PID=$BOT_PID"
    tail -n 50 "$BOT_LOG" || true
    exit 1
fi

# 再确认 cmdline 真是 bot.py，且不是立刻变僵尸
if ! python3 - "$BOT_PID" <<'PY'
import sys
from pathlib import Path
pid = sys.argv[1]
p = Path("/proc") / pid
if not p.exists():
    raise SystemExit(1)
stat = (p / "stat").read_text().split()[2]
args = [x.decode() for x in (p / "cmdline").read_bytes().split(b"\0") if x]
ok = stat != "Z" and any(a.endswith("bot.py") for a in args)
print("bot_check", pid, stat, " ".join(args)[:120], "ok" if ok else "BAD")
raise SystemExit(0 if ok else 1)
PY
then
    echo "Bot 进程异常（非 bot.py 或已僵尸）"
    tail -n 50 "$BOT_LOG" || true
    exit 1
fi

if ! kill -0 "$SERVER_PID" 2>/dev/null; then
    echo "监控服务启动失败 PID=$SERVER_PID"
    tail -n 50 "$SERVER_LOG" || true
    exit 1
fi

echo "模拟交易已启动"
echo "Python: $PYTHON_BIN"
echo "Bot PID: $BOT_PID"
echo "Server PID: $SERVER_PID"
echo "Runtime: $RUNTIME_DIR"
echo "Dashboard: http://localhost:${STATUS_PORT}"
echo "Bot Log: $BOT_LOG"
echo "Server Log: $SERVER_LOG"
# 明确提示：项目 venv 仅作遗留路径，不再用于启动
if [[ -x "$PROJECT_VENV/bin/python3" ]]; then
    echo "Note: project venv ($PROJECT_VENV) is ignored due to /data FUSE EIO risk"
fi
