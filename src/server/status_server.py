#!/usr/bin/env python3
"""
Bot 状态监控 HTTP 服务器 - 精简路由层
职责: HTTP 路由分发 + 子进程管理 + 启动
数据层: 委托 helpers / api_proxy / market_data 模块
"""

from __future__ import annotations

import http.server
import json
import os
import secrets
import signal
import socketserver
import subprocess
import sys
import urllib.parse
from datetime import datetime, timezone
from urllib.parse import urlparse

from dotenv import dotenv_values

# 让顶层能 import src 模块
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from src.core.utils import (
    extract_market_slug as _extract_market_slug,
    load_trading_control as _load_trading_control_util,
    save_json_file,
)
from src.core.config import Config

from src.server.helpers import (
    load_json_file, load_status_from_file, load_paper_state,
    save_trading_control, send_json,
    derive_complement_price,
)
from src.server.api_proxy import (
    build_hmac_headers, clob_get, data_api_get,
    fetch_market_snapshot, build_synthetic_orderbook, fetch_order_book,
    find_current_btc15m_slug, get_active_market_slug,
    get_real_wallet_balance, _extract_market_slug as _api_extract_slug,
)
from src.server.market_data import get_btc_price

PORT = int(os.environ.get("STATUS_PORT") or Config.get("STATUS_PORT", "8889"))
CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(os.path.dirname(CURRENT_DIR))
DATA_DIR = os.path.join(ROOT_DIR, "data")
RUNTIME_DIR = os.environ.get("RUNTIME_DIR", "/tmp/polymarket-fv-edge/data")
PUBLIC_DIR = os.path.join(ROOT_DIR, "public")
WEB_TOKEN_FILE = os.path.join(DATA_DIR, ".web_token")
STATUS_FILE = os.path.join(DATA_DIR, "bot_status.json")
PAPER_STATE_FILE = os.path.join(DATA_DIR, "paper_trade_state.json")
CONTROL_FILE = os.path.join(DATA_DIR, "trading_control.json")
ENV_FILE = os.path.join(ROOT_DIR, ".env")

PUBLIC_PATHS = {"/", "/favicon.ico"}
PUBLIC_EXTS = (".html", ".css", ".js", ".png", ".jpg", ".jpeg", ".ico", ".svg", ".woff", ".woff2", ".map", ".json", ".txt")
_bot_process: subprocess.Popen | None = None
BOT_SCRIPT = os.path.join(ROOT_DIR, "bot.py")


def _trade_sort_key(trade: dict) -> str:
    return str(trade.get("closed_at") or trade.get("created_at") or trade.get("timestamp") or "")


def _trade_dedupe_key(trade: dict) -> str:
    raw_id = trade.get("id")
    if raw_id:
        return f"id:{raw_id}"
    return "|".join(str(trade.get(k) or "") for k in ("market_slug", "outcome", "created_at", "status"))


def _load_paper_trades_with_archives(limit: int = 1000) -> list:
    """Return the current paper trades using the live state file as the source of truth."""
    state = load_paper_state() or {}
    merged = []
    seen = set()

    def add_trades(items):
        for trade in items or []:
            if not isinstance(trade, dict):
                continue
            key = _trade_dedupe_key(trade)
            if key in seen:
                continue
            seen.add(key)
            merged.append(trade)

    add_trades(state.get("trades", []))

    merged.sort(key=_trade_sort_key, reverse=True)
    return merged[:limit]


def _instance_key(parsed) -> str:
    """FV Edge is the repository's only runtime instance."""
    return "primary"


def _instance_context(instance: str) -> dict:
    return {
        "key": "primary",
        "label": "FV Edge",
        "root_dir": ROOT_DIR,
        "data_dir": DATA_DIR,
        # 实时 API 只读运行时快照；永久卷仅作备份和恢复来源。
        "status_file": os.path.join(RUNTIME_DIR, "bot_status.json"),
        "state_file": os.path.join(RUNTIME_DIR, "paper_trade_state.json"),
        "control_file": CONTROL_FILE,
        "env_file": ENV_FILE,
    }


def _load_status_for_instance(ctx: dict) -> dict:
    return load_json_file(ctx.get("status_file", ""), {}) or {}


def _load_control_for_instance(ctx: dict) -> dict:
    return load_json_file(ctx.get("control_file", ""), {}) or {}


def _load_env_for_instance(ctx: dict) -> dict:
    env_file = ctx.get("env_file")
    if not env_file or not os.path.exists(env_file):
        return {}
    try:
        return {k: v for k, v in dotenv_values(env_file).items() if v not in (None, "")}
    except Exception:
        return {}


def _instance_env(ctx: dict, control: dict, key: str, default=""):
    if key in control:
        return control.get(key)
    env = _load_env_for_instance(ctx)
    if key in env:
        return env.get(key)
    if ctx.get("key") == "primary":
        return Config.get(key, default)
    return default


def _normalized_state_for_instance(ctx: dict, state: dict) -> dict:
    return state or {}


def _load_state_for_instance(ctx: dict) -> dict:
    state = load_json_file(ctx.get("state_file", ""), {}) or {}
    return _normalized_state_for_instance(ctx, state)


def _build_positions_from_trades(trades: list) -> list:
    open_trades = [t for t in (trades or []) if t.get("status") == "OPEN"]
    positions = []
    for t in open_trades:
        positions.append({
            "id": t.get("id", ""),
            "market": t.get("market", ""),
            "market_slug": t.get("market_slug", ""),
            "title": t.get("market", ""),
            "outcome": t.get("outcome", ""),
            "outcome_name": t.get("outcome", ""),
            "size": t.get("size", 0),
            "amount": t.get("amount", 0),
            "avg_price": t.get("price", 0),
            "entry_price": t.get("price", 0),
            "avgPrice": t.get("price", 0),
            "created_at": t.get("created_at", ""),
            "strategy": t.get("strategy", ""),
            "reason": t.get("reason", ""),
        })
    return positions


def _build_balance_payload(ctx: dict, state: dict) -> dict:
    # 只用独立 summary 快照，不读 state.summary
    state_file = ctx.get("state_file", "")
    summary_file = os.path.join(os.path.dirname(state_file), "state_summary.json") if state_file else ""
    summary = {}
    if summary_file and os.path.exists(summary_file):
        try:
            with open(summary_file, "r", encoding="utf-8") as f:
                summary = json.load(f)
        except Exception:
            pass
    return {
        "balance": summary.get("cash_balance", summary.get("ending_balance", float(_instance_env(ctx, _load_control_for_instance(ctx), "PAPER_START_BALANCE", "100")))),
        "wallet": state.get("wallet") or _instance_env(ctx, _load_control_for_instance(ctx), "PAPER_WALLET_LABEL", "LOCAL-SIM"),
        "source": "paper_live",
        "cash_balance": summary.get("cash_balance"),
        "reserved_balance": summary.get("reserved_balance"),
        "realized_pnl": summary.get("realized_pnl"),
        "unrealized_pnl": summary.get("unrealized_pnl"),
        "starting_balance": _instance_env(ctx, _load_control_for_instance(ctx), "PAPER_START_BALANCE", "100"),
    }


def _build_config_payload(ctx: dict, state: dict, bot_status: dict) -> dict:
    report = state.get("report", {})
    control = _load_control_for_instance(ctx)
    # 只用独立 summary 快照，不读 state.summary（可能不完整）
    state_file = ctx.get("state_file", "")
    summary_file = os.path.join(os.path.dirname(state_file), "state_summary.json") if state_file else ""
    summary = {}
    if summary_file and os.path.exists(summary_file):
        try:
            with open(summary_file, "r", encoding="utf-8") as f:
                summary = json.load(f)
        except Exception:
            pass
    return {
        "instance_key": ctx.get("key"),
        "instance_label": ctx.get("label"),
        "trading_mode": _instance_env(ctx, control, "TRADING_MODE", "paper_live"),
        "market_selection_mode": "auto_btc_15m",
        "strategy_profile": "fv_edge",
        "market_question": bot_status.get("market_question") or state.get("market", {}).get("question") or "",
        "market_end_date": bot_status.get("market_end_date") or state.get("market", {}).get("end_date") or "",
        "market_outcomes": bot_status.get("market_outcomes") or [],
        "paper_start_balance": _instance_env(ctx, control, "PAPER_START_BALANCE", "100"),
        "paper_bet_amount": _instance_env(ctx, control, "FV_EDGE_POSITION_USD", "2.0"),
        "cash_balance": summary.get("cash_balance"),
        "reserved_balance": summary.get("reserved_balance"),
        "open_positions": summary.get("open_positions"),
        "total_trades": summary.get("total_trades"),
        "paper_win_rate": summary.get("win_rate"),
        "paper_winning_trades": summary.get("winning_trades"),
        "paper_losing_trades": summary.get("losing_trades"),
        "paper_profit": summary.get("realized_pnl", report.get("profit")),
        "paper_roi_percent": report.get("roi_percent"),
        "paper_balance": summary.get("cash_balance", summary.get("ending_balance")),
        "paper_session_started_at": state.get("session_started_at") or summary.get("session_started_at") or report.get("session_started_at"),
        "FV_EDGE_THRESHOLD_BPS": _instance_env(ctx, control, "FV_EDGE_THRESHOLD_BPS", "300"),
        "FV_EDGE_MAX_MTE": _instance_env(ctx, control, "FV_EDGE_MAX_MTE", "1.5"),
        "FV_EDGE_MAX_OPEN_POSITIONS": _instance_env(ctx, control, "FV_EDGE_MAX_OPEN_POSITIONS", "1"),
        "FV_EDGE_MAX_BTC_AGE_SECONDS": _instance_env(ctx, control, "FV_EDGE_MAX_BTC_AGE_SECONDS", "3"),
        "wallet": state.get("wallet") or _instance_env(ctx, control, "PAPER_WALLET_LABEL", "LOCAL-SIM"),
        "trading_enabled": control.get("trading_enabled", False),
        "strategy_name": "FV Edge",
    }


def _build_instance_dashboard(ctx: dict) -> dict:
    if not ctx:
        return {"error": "实例未配置"}
    state = _load_state_for_instance(ctx)
    status = _load_status_for_instance(ctx)
    trades = list((state.get("trades") or []))[:1000]
    positions = _build_positions_from_trades(trades)
    orderbook = {}
    slug = status.get("market_slug")
    if slug:
        try:
            orderbook = fetch_order_book(slug)
        except Exception as exc:
            orderbook = {"error": str(exc)}
    return {
        "instance_key": ctx.get("key"),
        "instance_label": ctx.get("label"),
        "status": status,
        "balance": _build_balance_payload(ctx, state),
        "positions": positions,
        "trades": trades,
        "config": _build_config_payload(ctx, state, status),
        "orderbook": orderbook,
    }


def _load_or_create_web_token() -> str:
    """启动时加载或生成 web UI API token."""
    try:
        if os.path.exists(WEB_TOKEN_FILE):
            with open(WEB_TOKEN_FILE, "r", encoding="utf-8") as f:
                token = f.read().strip()
            if token and len(token) >= 32:
                return token
    except Exception:
        pass
    token = secrets.token_urlsafe(32)
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(WEB_TOKEN_FILE, "w", encoding="utf-8") as f:
            f.write(token)
        os.chmod(WEB_TOKEN_FILE, 0o600)
    except Exception:
        pass
    return token


WEB_API_TOKEN = _load_or_create_web_token()
print(f"\U0001f510 Web UI API Token 已加载 ({WEB_TOKEN_FILE})")
print("   本机/SSH 隧道访问自动放行；公网请求必须携带有效 X-Api-Key。")


def _requested_account(parsed):
    """从 URL ?account= 或 Config.TRADING_MODE 判断是 paper 还是 real"""
    params = urllib.parse.parse_qs(parsed.query)
    requested = (params.get("account", [Config.get("TRADING_MODE", "paper")])[0] or "").strip().lower()
    return "real" if requested == "real" else "paper"


def _config_or_control(key, default=""):
    """优先读 Config, 再读 control.json 覆盖（用于多处并行覆盖的场景）"""
    # Config.get() 已经走 os.environ / .env
    val = Config.get(key, None)
    if val is not None:
        return val
    try:
        control = load_json_file(CONTROL_FILE, {})
        if key in control:
            return control[key]
    except Exception:
        pass
    return os.environ.get(key, default)


def _get_configured_market_input():
    """合并获取市场配置"""
    return (
        _config_or_control("TARGET_MARKET_URL") or
        _config_or_control("TARGET_MARKET_SLUG") or
        Config.get("BTC_UPDOWN_MARKET_ID", "")
    )


# =========================== HTTP Handler ===========================


class StatusHandler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=PUBLIC_DIR, **kwargs)

    def log_message(self, format, *args):
        pass  # 安静模式

    def handle_one_request(self):
        try:
            super().handle_one_request()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            try:
                self.wfile.close()
            except Exception:
                pass
            try:
                self.rfile.close()
            except Exception:
                pass

    def handle_error(self, request, client_address):
        import traceback
        exc_type, exc_value, _ = sys.exc_info()
        if exc_type in (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            return
        if exc_type is Exception:
            sys.stderr.write(f"[status_server] {exc_type.__name__}: {exc_value}\n")
            return
        traceback.print_exc()

    def end_headers(self):
        headers = getattr(self, "headers", None)
        origin = headers.get("Origin") if headers else None
        if origin and (origin.startswith("http://localhost:") or origin.startswith("http://127.0.0.1:")):
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
        else:
            self.send_header("Access-Control-Allow-Origin", f"http://localhost:{PORT}")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Api-Key, Authorization")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
        self.send_header("Pragma", "no-cache")
        self.send_header("Expires", "0")
        super().end_headers()

    def _is_public_path(self, path: str) -> bool:
        if path in PUBLIC_PATHS:
            return True
        return path.endswith(PUBLIC_EXTS)

    def _check_token(self) -> bool:
        """本机/SSH 隧道放行；其他来源必须提供正确 token."""
        client_host = (self.client_address[0] if self.client_address else "") or ""
        if client_host in {"127.0.0.1", "::1", "localhost"}:
            return True
        auth = (self.headers.get("X-Api-Key") or "").strip()
        if not auth:
            bearer = (self.headers.get("Authorization") or "").strip()
            if bearer.lower().startswith("bearer "):
                auth = bearer[7:].strip()
        return bool(auth) and secrets.compare_digest(auth, WEB_API_TOKEN)

    def _require_auth(self) -> bool:
        parsed = urlparse(self.path)
        if self._is_public_path(parsed.path):
            return True
        return self._check_token()

    def read_json_body(self):
        length = int(self.headers.get("Content-Length", "0") or "0")
        if length <= 0:
            return {}
        raw = self.rfile.read(length).decode("utf-8")
        if not raw.strip():
            return {}
        return json.loads(raw)

    # ---- POST ----

    def do_OPTIONS(self):
        self.send_response(204)
        origin = self.headers.get("Origin")
        if origin and (origin.startswith(f"http://localhost:{PORT}") or origin.startswith("http://127.0.0.1:")):
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
        else:
            self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Api-Key, Authorization")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
        self.send_header("Pragma", "no-cache")
        self.send_header("Expires", "0")
        self.end_headers()

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path

        if not self._require_auth():
            send_json(self, {"error": "未授权"}, 401)
            return

        if path == "/api/control":
            try:
                payload = self.read_json_body()
            except Exception as e:
                send_json(self, {"error": f"无效 JSON: {e}"}, 400)
                return
            if "trading_enabled" not in payload:
                send_json(self, {"error": "缺少 trading_enabled"}, 400)
                return
            value = payload.get("trading_enabled")
            if isinstance(value, bool):
                send_json(self, save_trading_control(value))
            else:
                send_json(self, save_trading_control(str(value).strip().lower() in {"1", "true", "yes", "on"}))
            return

        elif path == "/api/start-bot":
            global _bot_process
            if _bot_process is not None:
                ret = _bot_process.poll()
                if ret is None:
                    send_json(self, {"status": "already_running", "pid": _bot_process.pid})
                    return
                _bot_process = None
            save_trading_control(True)
            venv_python = os.path.join(ROOT_DIR, "venv", "bin", "python3")
            if not os.path.exists(venv_python):
                venv_python = "python3"
            try:
                _bot_process = subprocess.Popen(
                    [venv_python, BOT_SCRIPT],
                    cwd=ROOT_DIR,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    env={**os.environ},
                )
                send_json(self, {"status": "started", "pid": _bot_process.pid})
            except Exception as e:
                send_json(self, {"error": f"启动 bot.py 失败: {e}"}, 500)
            return

        elif path == "/api/stop-bot":

            if _bot_process is None:
                send_json(self, {"status": "not_running"})
                return
            ret = _bot_process.poll()
            if ret is not None:
                _bot_process = None
                send_json(self, {"status": "already_stopped", "exit_code": ret})
                return
            try:
                os.kill(_bot_process.pid, signal.SIGTERM)
                try:
                    _bot_process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    _bot_process.kill()
                    _bot_process.wait()
            except Exception:
                pass
            _bot_process = None
            save_trading_control(False)
            send_json(self, {"status": "stopped"})
            return

        elif path == "/api/update-config":
            try:
                payload = self.read_json_body()
                ENV_WRITABLE_KEYS = {
                    "POLYMARKET_API_KEY", "POLYMARKET_API_SECRET", "POLYMARKET_API_PASSPHRASE",
                    "POLYMARKET_PRIVATE_KEY", "POLYMARKET_FUNDER_ADDRESS", "POLYMARKET_WALLET_ADDRESS",
                }
                env_updates = {k: v for k, v in payload.items() if k in ENV_WRITABLE_KEYS}
                if env_updates:
                    try:
                        from dotenv import set_key
                        for k, v in env_updates.items():
                            set_key(ENV_FILE, k, str(v) if v is not None else "")
                    except ImportError:
                        import shlex
                        lines = []
                        replaced = set()
                        if os.path.exists(ENV_FILE):
                            with open(ENV_FILE, "r") as f:
                                for line in f:
                                    stripped = line.strip()
                                    if stripped and not stripped.startswith("#") and "=" in stripped:
                                        key = stripped.split("=", 1)[0].strip()
                                        if key in env_updates:
                                            lines.append(f"{key}={shlex.quote(str(env_updates[key]))}\n")
                                            replaced.add(key)
                                            continue
                                    lines.append(line if line.endswith("\n") else line + "\n")
                        for k, v in env_updates.items():
                            if k not in replaced:
                                lines.append(f"{k}={shlex.quote(str(v))}\n")
                        with open(ENV_FILE, "w") as f:
                            f.writelines(lines)
                    try:
                        Config.invalidate()
                    except Exception:
                        pass
                CONTROL_WRITABLE_KEYS = [
                    "TRADING_MODE", "trading_enabled",
                    "FV_EDGE_POSITION_USD", "FV_EDGE_THRESHOLD_BPS",
                    "FV_EDGE_MAX_MTE", "FV_EDGE_MAX_OPEN_POSITIONS",
                    "FV_EDGE_MAX_BTC_AGE_SECONDS",
                ]
                control = load_json_file(CONTROL_FILE, {})
                for k in CONTROL_WRITABLE_KEYS:
                    if k in payload:
                        control[k] = payload[k]
                control["updated_at"] = datetime.now().isoformat()
                save_json_file(CONTROL_FILE, control)
                send_json(self, {"success": True})
            except Exception as e:
                send_json(self, {"error": str(e)}, 500)
            return

        send_json(self, {"error": "未找到接口"}, 404)

    # ---- GET ----

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path

        if self._is_public_path(path):
            if path == "/":
                self.path = "/status.html"
            super().do_GET()
            return

        if not self._require_auth():
            send_json(self, {"error": "未授权"}, 401)
            return

        # === Bot 状态 ===
        if path in {"/status-json", "/api/status"}:
            ctx = _instance_context(_instance_key(parsed))
            data = _load_status_for_instance(ctx)
            send_json(self, data or {})

        elif path == "/api/web-token":
            send_json(self, {"token": WEB_API_TOKEN})

        # === BTC 实时价格 ===
        elif path == "/api/btc":
            send_json(self, get_btc_price())

        # === 单实例 Dashboard 打包数据 ===
        elif path == "/api/instance-dashboard":
            ctx = _instance_context(_instance_key(parsed))
            if not ctx:
                send_json(self, {"error": "并行实例未配置"}, 400)
                return
            send_json(self, _build_instance_dashboard(ctx))

        # === BTC 趋势 ===
        elif path == "/api/btc-trend":
            snap = load_json_file(os.path.join(DATA_DIR, "btc_snapshot.json"), None)
            if not snap:
                send_json(self, {"error": "btc_snapshot 未生成 (bot 可能还在启动)", "fallback": get_btc_price()})
            else:
                snap = dict(snap)
                # FV training status for dashboard sample and reference diagnostics.
                try:
                    pred_path = os.path.join(DATA_DIR, "fair_value_predictions.jsonl")
                    refs_path = os.path.join(DATA_DIR, "btc_window_refs.json")
                    pred_lines = []
                    if os.path.exists(pred_path):
                        with open(pred_path, "r", encoding="utf-8") as f:
                            pred_lines = [line for line in f if line.strip()]
                    refs = load_json_file(refs_path, {}) or {}
                    latest_pred = None
                    if pred_lines:
                        latest_pred = json.loads(pred_lines[-1])
                    current_ref = None
                    if latest_pred and latest_pred.get("slug") in refs:
                        current_ref = refs.get(latest_pred.get("slug"))
                    elif refs:
                        current_ref = list(refs.values())[-1]
                    snap["fv_training"] = {
                        "enabled": True,
                        "prediction_samples": len(pred_lines),
                        "window_ref_count": len(refs),
                        "latest_slug": latest_pred.get("slug") if latest_pred else None,
                        "latest_fair_up": latest_pred.get("fair_up") if latest_pred else None,
                        "latest_ref_px": latest_pred.get("ref_px") if latest_pred else None,
                        "latest_s_now": latest_pred.get("s_now") if latest_pred else None,
                        "latest_minutes_to_end": latest_pred.get("minutes_to_end") if latest_pred else None,
                        "late_ref": bool((current_ref or {}).get("late_ref")),
                    }
                except Exception as e:
                    snap["fv_training"] = {"enabled": True, "error": str(e)[:120]}
                send_json(self, snap)

        # === 余额 ===
        elif path == "/api/balance":
            account = _requested_account(parsed)
            if account == "paper":
                ctx = _instance_context(_instance_key(parsed))
                state = _load_state_for_instance(ctx)
                send_json(self, _build_balance_payload(ctx, state))
                return
            try:
                wallet = Config.get("POLYMARKET_WALLET_ADDRESS", "")
                api_key = Config.get("POLYGONSCAN_API_KEY", "") or Config.get("ETHERSCAN_API_KEY", "")
                send_json(self, get_real_wallet_balance(wallet, api_key))
            except Exception as e:
                send_json(self, {"error": str(e)})

        # === 真实钱包余额 ===
        elif path == "/api/real-balance":
            try:
                wallet = Config.get("POLYMARKET_WALLET_ADDRESS", "")
                api_key = Config.get("POLYGONSCAN_API_KEY", "") or Config.get("ETHERSCAN_API_KEY", "")
                send_json(self, get_real_wallet_balance(wallet, api_key))
            except Exception as e:
                # Keep this endpoint non-fatal for the dashboard and smoke tests:
                # missing wallet/API config should surface as payload error, not HTTP 400.
                send_json(self, {"error": str(e)})

        # === 交易控制状态 ===
        elif path == "/api/control":
            send_json(self, _load_trading_control_util(CONTROL_FILE))

        # === 当前持仓 ===
        elif path == "/api/positions":
            account = _requested_account(parsed)
            if account == "paper":
                ctx = _instance_context(_instance_key(parsed))
                state = _load_state_for_instance(ctx)
                send_json(self, _build_positions_from_trades(state.get("trades", [])))
            else:
                wallet = Config.get("POLYMARKET_WALLET_ADDRESS", "")
                if not wallet:
                    send_json(self, {"error": "未配置钱包地址"}, 400)
                    return
                data = data_api_get(f"/positions?user={wallet}")
                send_json(self, data)

        # === Order Book ===
        elif path == "/api/orderbook":
            params = urllib.parse.parse_qs(parsed.query)
            slug = _extract_market_slug((params.get("slug") or [""])[0])
            if not slug:
                ctx = _instance_context(_instance_key(parsed))
                slug = (_load_status_for_instance(ctx) or {}).get("market_slug") or get_active_market_slug()
            if not slug:
                send_json(self, {"error": "未配置 market slug"}, 400)
                return
            send_json(self, fetch_order_book(slug))

        # === 最近交易 ===
        elif path == "/api/trades":
            account = _requested_account(parsed)
            if account == "paper":
                params = urllib.parse.parse_qs(parsed.query)
                limit = 1000
                try:
                    limit = max(1, min(5000, int((params.get("limit") or [limit])[0])))
                except Exception:
                    limit = 1000
                ctx = _instance_context(_instance_key(parsed))
                state = _load_state_for_instance(ctx)
                trades = list((state.get("trades") or []))
                trades.sort(key=_trade_sort_key, reverse=True)
                send_json(self, trades[:limit])
            else:
                wallet = Config.get("POLYMARKET_WALLET_ADDRESS", "")
                if not wallet:
                    send_json(self, {"error": "未配置钱包地址"}, 400)
                    return
                data = data_api_get(f"/trades?user={wallet}&limit=20")
                send_json(self, data)

        # === FV Edge signals ===
        elif path == "/api/fv-signals":
            state = load_paper_state() or {}
            send_json(self, list(state.get("fv_signal_history", [])))

        # === 挂单 ===
        elif path == "/api/orders":
            account = _requested_account(parsed)
            if account == "paper":
                state = load_paper_state() or {}
                send_json(self, state.get("orders", []))
            else:
                wallet = Config.get("POLYMARKET_WALLET_ADDRESS", "")
                if not wallet:
                    send_json(self, {"error": "未配置钱包地址"}, 400)
                    return
                data = data_api_get(f"/activity?user={wallet}&limit=10")
                send_json(self, data)

        # === 配置 ===
        elif path == "/api/config":
            ctx = _instance_context(_instance_key(parsed))
            state = _load_state_for_instance(ctx)
            bot_status = _load_status_for_instance(ctx)
            report = state.get("report", {})
            control = _load_control_for_instance(ctx)
            # 只用独立 summary 快照，不读 state.summary
            state_file = ctx.get("state_file", "")
            summary_file = os.path.join(os.path.dirname(state_file), "state_summary.json") if state_file else ""
            summary = {}
            if summary_file and os.path.exists(summary_file):
                try:
                    with open(summary_file, "r", encoding="utf-8") as f:
                        summary = json.load(f)
                except Exception:
                    pass
            # 合并 control.json 覆盖的键
            def _env(key, default=""):
                return _instance_env(ctx, control, key, default)
            config = {
                "trading_mode": _env("TRADING_MODE", "paper_live"),
                "paper_start_balance": _env("PAPER_START_BALANCE", "100"),
                "paper_bet_amount": _env("FV_EDGE_POSITION_USD", "2.0"),
                "FV_EDGE_POSITION_USD": _env("FV_EDGE_POSITION_USD", "2.0"),
                "FV_EDGE_THRESHOLD_BPS": _env("FV_EDGE_THRESHOLD_BPS", "300"),
                "FV_EDGE_MAX_MTE": _env("FV_EDGE_MAX_MTE", "1.5"),
                "FV_EDGE_MAX_OPEN_POSITIONS": _env("FV_EDGE_MAX_OPEN_POSITIONS", "1"),
                "FV_EDGE_MAX_BTC_AGE_SECONDS": _env("FV_EDGE_MAX_BTC_AGE_SECONDS", "3"),
                "market_id": (bot_status.get("market_slug") or state.get("market", {}).get("slug") or get_active_market_slug() or "")[:48],
                "market_question": bot_status.get("market_question") or state.get("market", {}).get("question") or "",
                "market_end_date": bot_status.get("market_end_date") or state.get("market", {}).get("end_date") or "",
                "market_outcomes": bot_status.get("market_outcomes") or [],
                "market_selection_mode": "auto_btc_15m",
                "strategy_profile": "fv_edge",
                "wallet": state.get("wallet") or (Config.get("POLYMARKET_WALLET_ADDRESS", "")[:10] + "..."),
                "paper_result": report.get("result"),
                "paper_profit": summary.get("realized_pnl", report.get("profit")),
                "paper_roi_percent": report.get("roi_percent"),
                "paper_balance": summary.get("cash_balance", summary.get("ending_balance")),
                "paper_session_started_at": state.get("session_started_at") or summary.get("session_started_at") or report.get("session_started_at"),
                "cash_balance": summary.get("cash_balance"),
                "reserved_balance": summary.get("reserved_balance"),
                "open_positions": summary.get("open_positions"),
                "total_trades": summary.get("total_trades"),
                "paper_win_rate": summary.get("win_rate"),
                "strategy_name": "FV Edge",
                "POLYMARKET_WALLET_ADDRESS": Config.get("POLYMARKET_WALLET_ADDRESS", ""),
                "POLYMARKET_FUNDER_ADDRESS": Config.get("POLYMARKET_FUNDER_ADDRESS", ""),
                "exit_rule": "FV Edge 持有到到期结算",
                "trading_enabled": control.get("trading_enabled", False),
            }
            send_json(self, config)

        else:
            send_json(self, {"error": "not found"}, 404)


def run_server():
    """运行 HTTP 服务器"""
    socketserver.ThreadingTCPServer.allow_reuse_address = True
    socketserver.ThreadingTCPServer.daemon_threads = True
    bind_host = os.environ.get("STATUS_BIND_HOST", "127.0.0.1")
    with socketserver.ThreadingTCPServer((bind_host, PORT), StatusHandler) as httpd:
        print(f"\U0001f310 状态监控页面: http://{bind_host}:{PORT}")
        httpd.serve_forever()


if __name__ == "__main__":
    print(f"\U0001f310 启动状态监控页面: http://localhost:{PORT}")
    print("按 Ctrl+C 停止")
    run_server()
