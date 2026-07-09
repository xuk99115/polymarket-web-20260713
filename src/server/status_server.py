#!/usr/bin/env python3
"""
Bot 状态监控 HTTP 服务器 - 精简路由层
职责: HTTP 路由分发 + 子进程管理 + 启动
数据层: 委托 helpers / api_proxy / market_data 模块
"""

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
from pathlib import Path

from dotenv import dotenv_values

# 让顶层能 import src 模块
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from src.core.utils import (
    extract_market_slug as _extract_market_slug,
    parse_json_list as _parse_json_list,
    load_trading_control as _load_trading_control_util,
    save_json_file,
    safe_float,
)
from src.core.config import Config

from src.server.helpers import (
    load_json_file, load_status_from_file, load_paper_state,
    save_trading_control, send_json,
    derive_complement_price, normalize_orderbook_levels,
)
from src.server.api_proxy import (
    build_hmac_headers, clob_get, data_api_get, http_json_get,
    fetch_market_snapshot, build_synthetic_orderbook, fetch_order_book,
    find_current_btc15m_slug, get_active_market_slug,
    get_real_wallet_balance, _extract_market_slug as _api_extract_slug,
)
from src.server.market_data import get_btc_price

# 套利状态
try:
    from src.trading._arbitrage import arb_pair_status, ARB_TIERS
except ImportError:
    arb_pair_status = None
    ARB_TIERS = []

PORT = int(os.environ.get("STATUS_PORT") or Config.get("STATUS_PORT", "8889"))
CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(os.path.dirname(CURRENT_DIR))
DATA_DIR = os.path.join(ROOT_DIR, "data")
PUBLIC_DIR = os.path.join(ROOT_DIR, "public")
WEB_TOKEN_FILE = os.path.join(DATA_DIR, ".web_token")
STATUS_FILE = os.path.join(DATA_DIR, "bot_status.json")
PAPER_STATE_FILE = os.path.join(DATA_DIR, "paper_trade_state.json")
CONTROL_FILE = os.path.join(DATA_DIR, "trading_control.json")
ENV_FILE = os.path.join(ROOT_DIR, ".env")

PUBLIC_PATHS = {"/", "/favicon.ico"}
PUBLIC_EXTS = (".html", ".css", ".js", ".png", ".jpg", ".jpeg", ".ico", ".svg", ".woff", ".woff2", ".map", ".json", ".txt")
SECONDARY_STATUS_URL = os.environ.get("SECONDARY_STATUS_URL") or Config.get("SECONDARY_STATUS_URL", "")
SECONDARY_STATUS_FILE = os.environ.get("SECONDARY_STATUS_FILE") or Config.get("SECONDARY_STATUS_FILE", "")
SECONDARY_ROOT = os.environ.get("SECONDARY_ROOT") or Config.get("SECONDARY_ROOT", "")

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
    params = urllib.parse.parse_qs(parsed.query)
    requested = (params.get("instance") or ["primary"])[0].strip().lower()
    return "parallel" if requested in {"parallel", "secondary", "5m", "hedge"} else "primary"


def _secondary_root_dir() -> str:
    if SECONDARY_ROOT:
        return SECONDARY_ROOT
    if SECONDARY_STATUS_FILE:
        try:
            return str(Path(SECONDARY_STATUS_FILE).resolve().parent.parent)
        except Exception:
            return ""
    return ""


def _instance_context(instance: str) -> dict:
    if instance == "parallel":
        root_dir = _secondary_root_dir()
        if not root_dir:
            return {}
        data_dir = os.path.join(root_dir, "data")
        return {
            "key": "parallel",
            "label": "5m 对冲系统",
            "root_dir": root_dir,
            "data_dir": data_dir,
            "status_file": SECONDARY_STATUS_FILE or os.path.join(data_dir, "bot_status.json"),
            "state_file": os.path.join(data_dir, "paper_trade_state.json"),
            "control_file": os.path.join(data_dir, "trading_control.json"),
            "env_file": os.path.join(root_dir, ".env"),
        }
    return {
        "key": "primary",
        "label": "15m 主系统",
        "root_dir": ROOT_DIR,
        "data_dir": DATA_DIR,
        "status_file": STATUS_FILE,
        "state_file": PAPER_STATE_FILE,
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


def _is_parallel_market_slug(value: str) -> bool:
    return str(value or "").startswith("btc-updown-5m-")


def _is_parallel_trade(trade: dict) -> bool:
    if not isinstance(trade, dict):
        return False
    strategy = str(trade.get("strategy") or trade.get("source") or "").lower()
    reason = str(trade.get("reason") or "").lower()
    slug = str(trade.get("market_slug") or "")
    return strategy == "hedged_limit" or "[hedge]" in reason or _is_parallel_market_slug(slug)


def _is_parallel_position(position: dict) -> bool:
    if not isinstance(position, dict):
        return False
    slug = str(position.get("market_slug") or position.get("slug") or "")
    strategy = str(position.get("strategy") or position.get("source") or "").lower()
    return strategy == "hedged_limit" or _is_parallel_market_slug(slug)


def _realized_trade_profit(trade: dict) -> float:
    for key in ("realized_profit", "realized_pnl", "pnl"):
        value = trade.get(key)
        if value not in (None, ""):
            try:
                return float(value)
            except Exception:
                continue
    close_price = trade.get("close_price")
    price = trade.get("price")
    size = trade.get("size")
    if close_price not in (None, "") and price not in (None, "") and size not in (None, ""):
        try:
            return (float(close_price) - float(price)) * float(size)
        except Exception:
            return 0.0
    return 0.0


def _normalized_state_for_instance(ctx: dict, state: dict) -> dict:
    if ctx.get("key") != "parallel":
        return state or {}

    normalized = dict(state or {})
    trades = [dict(t) for t in list(normalized.get("trades") or []) if _is_parallel_trade(t)]
    positions = [p for p in list(normalized.get("positions") or []) if _is_parallel_position(p)]
    orders = [o for o in list(normalized.get("orders") or []) if _is_parallel_position(o)]
    for trade in trades:
        pair_id = trade.get("pair_id") or trade.get("hedge_pair_id")
        if pair_id:
            trade["pair_id"] = pair_id
            trade["hedge_pair_id"] = pair_id
    trades.sort(key=_trade_sort_key, reverse=True)

    try:
        start_balance = float(_instance_env(ctx, _load_control_for_instance(ctx), "PAPER_START_BALANCE", "100"))
    except Exception:
        start_balance = 100.0
    realized = round(sum(_realized_trade_profit(t) for t in trades), 4)
    winning = sum(1 for t in trades if _realized_trade_profit(t) > 0)
    roi = round((realized / start_balance) * 100, 2) if start_balance else 0.0
    ending = round(start_balance + realized, 4)
    session_started_at = None
    if trades:
        oldest = trades[-1]
        session_started_at = oldest.get("created_at") or oldest.get("opened_at")

    summary = dict(normalized.get("summary") or {})
    summary.update({
        "cash_balance": ending,
        "reserved_balance": 0,
        "ending_balance": ending,
        "open_positions": len(positions),
        "realized_pnl": realized,
        "unrealized_pnl": 0.0,
        "total_trades": len(trades),
        "winning_trades": winning,
        "win_rate": round((winning / len(trades)) * 100, 2) if trades else 0.0,
        "session_started_at": session_started_at,
    })

    report = dict(normalized.get("report") or {})
    report.update({
        "profit": realized,
        "roi_percent": roi,
        "session_started_at": session_started_at,
    })

    normalized["trades"] = trades
    normalized["positions"] = positions
    normalized["orders"] = orders
    normalized["summary"] = summary
    normalized["report"] = report
    normalized["session_started_at"] = session_started_at
    return normalized


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


def _build_hedge_pairs_payload(state: dict, bot_status: dict | None = None) -> list:
    pairs = []
    market_by_index = {}
    market_slug = ""
    if isinstance(bot_status, dict):
        market_slug = str(bot_status.get("market_slug") or "")
        for item in list(bot_status.get("market_outcomes") or []):
            try:
                market_by_index[int(item.get("index", 0))] = item
            except Exception:
                continue
    for pair in list(state.get("hedge_pairs") or []):
        if not isinstance(pair, dict):
            continue
        pair_status = str(pair.get("status") or "").upper()
        raw_orders = list((pair.get("orders") or {}).values())
        pair_market = str(pair.get("market_slug") or "")
        live_market = market_by_index if pair_market and pair_market == market_slug else {}
        orders = []
        for raw in raw_orders:
            if not isinstance(raw, dict):
                continue
            filled_shares = safe_float(raw.get("filled_shares"), 0.0) or 0.0
            target_shares = safe_float(raw.get("target_shares"), 0.0) or 0.0
            order_status = str(raw.get("status") or "").upper()
            current = live_market.get(int(raw.get("outcome_index", 0))) if live_market else {}
            if order_status == "FILLED":
                display_status = "已成交"
            elif order_status == "PARTIAL":
                display_status = "部分成交"
            elif order_status == "STAGED":
                display_status = "待补腿"
            elif order_status == "CANCELLED":
                display_status = "未成交已取消" if filled_shares <= 0 else "剩余已取消"
            else:
                display_status = "首腿挂单中"
            orders.append({
                "outcome": raw.get("outcome"),
                "outcome_index": raw.get("outcome_index"),
                "status": order_status,
                "status_label": display_status,
                "target_shares": target_shares,
                "filled_shares": filled_shares,
                "limit_price": safe_float(raw.get("limit_price")),
                "avg_price": safe_float(raw.get("avg_price")),
                "filled_value": safe_float(raw.get("filled_value")),
                "cancel_reason": raw.get("cancel_reason"),
                "current_price": safe_float(current.get("price")) if current else None,
                "current_best_bid": safe_float(current.get("best_bid")) if current else None,
                "current_best_ask": safe_float(current.get("best_ask")) if current else None,
            })
        orders.sort(key=lambda item: safe_float(item.get("outcome_index"), 0.0) or 0.0)
        filled_orders = [order for order in orders if (safe_float(order.get("filled_shares"), 0.0) or 0.0) > 0]
        first_leg = filled_orders[0] if len(filled_orders) == 1 else None
        hedge_leg = next((order for order in orders if order is not first_leg), None)
        first_leg_price = safe_float((first_leg or {}).get("avg_price"), (first_leg or {}).get("limit_price"))
        hedge_limit_price = safe_float((hedge_leg or {}).get("limit_price"))
        hedge_best_ask = safe_float((hedge_leg or {}).get("current_best_ask"))
        hedge_gap = None
        if hedge_limit_price is not None and hedge_best_ask is not None:
            hedge_gap = round(hedge_best_ask - hedge_limit_price, 4)
        pairs.append({
            "id": pair.get("id"),
            "market_slug": pair.get("market_slug"),
            "market_title": pair.get("market_title"),
            "status": pair_status,
            "status_label": {
                "PENDING_BOTH": "首腿等待成交",
                "LEG_OPEN": "单腿暴露",
                "LOCKED": "双边锁利",
                "EXITED_SINGLE": "单腿已离场",
                "SETTLED": "已结算",
                "CANCELLED": "已取消",
            }.get(pair_status, str(pair.get("status") or "--")),
            "entry_side_label": pair.get("entry_side_label"),
            "entry_side_index": pair.get("entry_side_index"),
            "created_at": pair.get("created_at"),
            "closed_at": pair.get("closed_at") or pair.get("settled_at"),
            "locked_profit": safe_float(pair.get("locked_profit")),
            "realized_profit": safe_float(pair.get("realized_profit")),
            "exit_price": safe_float(pair.get("exit_price")),
            "net_exposure": safe_float(pair.get("net_exposure")),
            "cancel_reason": pair.get("cancel_reason"),
            "first_leg_price": first_leg_price,
            "hedge_limit_price": hedge_limit_price,
            "hedge_best_ask": hedge_best_ask,
            "hedge_gap_to_fill": hedge_gap,
            "can_hedge_now": (hedge_gap is not None and hedge_gap <= 1e-9),
            "orders": orders,
        })
    pairs.sort(key=lambda item: str(item.get("created_at") or ""), reverse=True)
    return pairs[:12]


def _build_balance_payload(ctx: dict, state: dict) -> dict:
    summary = state.get("summary", {})
    return {
        "balance": summary.get("ending_balance", float(_instance_env(ctx, _load_control_for_instance(ctx), "PAPER_START_BALANCE", "100"))),
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
    summary = state.get("summary", {})
    signal = state.get("last_signal", {})
    control = _load_control_for_instance(ctx)
    market_mode = _instance_env(ctx, control, "MARKET_SELECTION_MODE", "manual")
    return {
        "instance_key": ctx.get("key"),
        "instance_label": ctx.get("label"),
        "trading_mode": _instance_env(ctx, control, "TRADING_MODE", "paper_live"),
        "market_selection_mode": market_mode,
        "strategy_profile": _instance_env(ctx, control, "STRATEGY_PROFILE", "generic_binary"),
        "market_question": bot_status.get("market_question") or state.get("market", {}).get("question") or "",
        "market_end_date": bot_status.get("market_end_date") or state.get("market", {}).get("end_date") or "",
        "market_outcomes": bot_status.get("market_outcomes") or [],
        "target_market_slug": _instance_env(ctx, control, "TARGET_MARKET_SLUG", ""),
        "target_market_url": _instance_env(ctx, control, "TARGET_MARKET_URL", ""),
        "paper_start_balance": _instance_env(ctx, control, "PAPER_START_BALANCE", "100"),
        "paper_bet_amount": _instance_env(ctx, control, "PAPER_BET_AMOUNT", _instance_env(ctx, control, "BET_AMOUNT", "1")),
        "cash_balance": summary.get("cash_balance"),
        "reserved_balance": summary.get("reserved_balance"),
        "open_positions": summary.get("open_positions"),
        "total_trades": summary.get("total_trades"),
        "paper_win_rate": summary.get("win_rate"),
        "paper_profit": report.get("profit"),
        "paper_roi_percent": report.get("roi_percent"),
        "paper_balance": summary.get("ending_balance"),
        "paper_session_started_at": state.get("session_started_at") or summary.get("session_started_at") or report.get("session_started_at"),
        "AI_MIN_CONFIDENCE": _instance_env(ctx, control, "AI_MIN_CONFIDENCE", "0.60"),
        "ai_trading_skill": _instance_env(ctx, control, "AI_TRADING_SKILL", ""),
        "take_profit_usd": _instance_env(ctx, control, "PAPER_TAKE_PROFIT_USD", "0.12"),
        "max_spread": _instance_env(ctx, control, "PAPER_MAX_SPREAD", "0.06"),
        "wallet": state.get("wallet") or _instance_env(ctx, control, "PAPER_WALLET_LABEL", "LOCAL-SIM"),
        "trading_enabled": control.get("trading_enabled", False),
        "signal_reason": signal.get("reason"),
        "strategy_name": report.get("strategy") or "配置 AI 驱动决策",
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
        "hedge_pairs": _build_hedge_pairs_payload(state, status),
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
                    "AI_API_KEY", "AI_BASE_URL", "AI_MODEL", "AI_MIN_CONFIDENCE",
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
                    "bet_amount", "paper_bet_amount", "take_profit_usd",
                    "TARGET_MARKET_SLUG", "TARGET_MARKET_URL",
                    "MARKET_SELECTION_MODE", "STRATEGY_PROFILE", "ALLOW_MULTI_OUTCOME",
                    "AI_MIN_CONFIDENCE", "AI_TRADING_SKILL",
                    "LIVE_MAX_OPEN_POSITIONS", "PAPER_MAX_OPEN_POSITIONS",
                    "AI_DECISION_INTERVAL_SECONDS",
                ]
                MAX_SKILL_BYTES = 4096
                control = load_json_file(CONTROL_FILE, {})
                for k in CONTROL_WRITABLE_KEYS:
                    if k in payload:
                        if k == "AI_TRADING_SKILL":
                            val = payload[k]
                            if val is not None and len(str(val).encode("utf-8")) > MAX_SKILL_BYTES:
                                send_json(self, {"error": f"AI_TRADING_SKILL 超过 {MAX_SKILL_BYTES} 字节限制"}, 400)
                                return
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
            data = load_status_from_file()
            send_json(self, data or {})

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
                # FV 训练状态: 给前端展示样本数 / 当前 ref / LowBuy FV 拦截开关。
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
                        "lowbuy_filter_enabled": False,
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
                send_json(self, {"error": str(e)}, 400)

        # === 交易控制状态 ===
        elif path == "/api/control":
            send_json(self, _load_trading_control_util(CONTROL_FILE))

        # === 并行第二系统状态 ===
        elif path == "/api/parallel-status":
            if SECONDARY_STATUS_FILE:
                try:
                    data = load_json_file(SECONDARY_STATUS_FILE, {}) or {}
                    if not isinstance(data, dict):
                        data = {"raw": data}
                    data["enabled"] = True
                    data["source_file"] = SECONDARY_STATUS_FILE
                    send_json(self, data)
                    return
                except Exception as e:
                    send_json(self, {"enabled": True, "error": str(e), "source_file": SECONDARY_STATUS_FILE}, 502)
                    return
            if not SECONDARY_STATUS_URL:
                send_json(self, {"enabled": False, "error": "未配置 SECONDARY_STATUS_URL/FILE"})
                return
            try:
                data = http_json_get(SECONDARY_STATUS_URL, timeout=3) or {}
                if not isinstance(data, dict):
                    data = {"raw": data}
                data["enabled"] = True
                data["source_url"] = SECONDARY_STATUS_URL
                send_json(self, data)
            except Exception as e:
                send_json(self, {"enabled": True, "error": str(e), "source_url": SECONDARY_STATUS_URL}, 502)

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

        # === 套利对子 ===
        elif path == "/api/arb-status":
            state = load_paper_state() or {}
            positions = state.get("positions", [])
            pairs = arb_pair_status(positions) if arb_pair_status else []
            for p in pairs:
                pid = p["pair_id"]
                slug = p["market_slug"]
                try:
                    snap = fetch_market_snapshot(slug)
                    token_ids = _parse_json_list(snap.get("clobTokenIds"))
                    outcomes_raw = _parse_json_list(snap.get("outcomes"))
                    if len(token_ids) >= 2 and not snap.get("error"):
                        asks = []
                        labels = []
                        for idx, tid in enumerate(token_ids[:2]):
                            book = http_json_get(f"https://clob.polymarket.com/book?token_id={tid}", timeout=5)
                            ask = None
                            asks_raw = book.get("asks") if isinstance(book, dict) else None
                            if asks_raw:
                                parsed = normalize_orderbook_levels(asks_raw, descending=False)
                                ask = parsed[0]["price"] if parsed else None
                            asks.append(ask)
                            labels.append(str(outcomes_raw[idx]).upper() if idx < len(outcomes_raw) else f"OUTCOME {idx+1}")
                        if asks[0] is not None and asks[1] is not None:
                            spread = abs(asks[0] - asks[1])
                            p["current_spread"] = round(spread, 4)
                            p["upside_locked"] = round(1.0 - (asks[0] + asks[1]), 4)
                            if asks[0] > asks[1]:
                                p["cheaper_side"] = labels[1]
                                p["cheaper_price"] = asks[1]
                            else:
                                p["cheaper_side"] = labels[0]
                                p["cheaper_price"] = asks[0]
                except Exception as e:
                    p["spread_error"] = str(e)[:100]
            send_json(self, {
                "tiers": [{"min_spread": t[0], "cash_fraction": t[1], "max_stake": t[2], "label": t[3]} for t in ARB_TIERS],
                "pairs": pairs,
                "summary": {
                    "open_pairs": len(pairs),
                    "total_locked": round(sum(p.get("locked_profit", 0) for p in pairs), 4),
                    "total_realized": round(sum(p.get("realized_pnl", 0) for p in pairs), 4),
                },
            })

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

        # === AI 决策 ===
        elif path == "/api/ai-decisions":
            state = load_paper_state() or {}
            history = list(state.get("ai_history", []))
            bot_status = load_status_from_file() or {}
            if bot_status.get("ai_prediction") and bot_status.get("last_update"):
                market_outcomes = bot_status.get("market_outcomes") or []
                outcome_summary = []
                for item in market_outcomes:
                    outcome_summary.append(f"[{item.get('index')}] {item.get('label')} @ {item.get('price', '--')}")
                latest = {
                    "decision_id": "LIVE-" + bot_status["last_update"][:16].replace("T", "-").replace(":", ""),
                    "generated_at": bot_status["last_update"],
                    "prediction": bot_status.get("ai_action", "SKIP"),
                    "action": bot_status.get("ai_action", "SKIP"),
                    "decision": bot_status.get("ai_action", "SKIP"),
                    "confidence": bot_status.get("ai_confidence", 0.5),
                    "model": "MiniMax-M2.7-highspeed",
                    "source": "live_bot",
                    "reasoning": bot_status.get("decision_reason", "--"),
                    "thought_markdown": bot_status.get("decision_reason", "--"),
                    "key_factors": [
                        f"市场: {bot_status.get('market_question', '--')}",
                        f"选择结果: {bot_status.get('ai_outcome_label', '--')}",
                        "盘口: " + (" | ".join(outcome_summary) if outcome_summary else "--"),
                    ],
                    "risk_flags": [],
                    "execution_summary": bot_status.get("execution_summary") or (
                        f"实盘模式 · {bot_status.get('trading_mode', 'live').upper()} · "
                        f"{'交易开启' if bot_status.get('trading_enabled') else '交易关闭'}"
                    ),
                    "focus_market": bot_status.get("market_question", ""),
                }
                history.insert(0, latest)
            send_json(self, history)

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
            summary = state.get("summary", {})
            signal = state.get("last_signal", {})
            control = _load_control_for_instance(ctx)
            # 合并 control.json 覆盖的键
            def _env(key, default=""):
                return _instance_env(ctx, control, key, default)
            config = {
                "ai_decision_interval_seconds": _env("AI_DECISION_INTERVAL_SECONDS", signal.get("decision_interval_seconds") or "15"),
                "bet_amount": _env("BET_AMOUNT", "1"),
                "max_bet_amount": _env("MAX_BET_AMOUNT", "10"),
                "paper_bet_amount": _env("PAPER_BET_AMOUNT", "1"),
                "min_probability_diff": _env("MIN_PROBABILITY_DIFF", "0.05"),
                "trading_mode": _env("TRADING_MODE", "paper"),
                "take_profit_percent": _env("TAKE_PROFIT_PERCENT", "0.18"),
                "take_profit_usd": _env("PAPER_TAKE_PROFIT_USD", "0.12"),
                "min_entry_price": _env("PAPER_MIN_ENTRY_PRICE", "0.15"),
                "max_entry_price": _env("PAPER_MAX_ENTRY_PRICE", "0.60"),
                "max_spread": _env("PAPER_MAX_SPREAD", "0.06"),
                "min_top_book_size": _env("PAPER_MIN_TOP_BOOK_SIZE", "25"),
                "min_minutes_to_expiry": _env("PAPER_MIN_MINUTES_TO_EXPIRY", "3"),
                "max_new_positions_per_cycle": _env("PAPER_MAX_NEW_POSITIONS_PER_CYCLE", "1"),
                "market_interval_minutes": _env("PAPER_MARKET_INTERVAL_MINUTES", "15"),
                "forward_slot_count": _env("PAPER_FORWARD_SLOT_COUNT", "8"),
                "paper_start_balance": _env("PAPER_START_BALANCE", "100"),
                "paper_max_open_positions": _env("PAPER_MAX_OPEN_POSITIONS", "1"),
                "live_max_open_positions": _env("LIVE_MAX_OPEN_POSITIONS", "1"),
                "stop_loss_enabled": _env("STOP_LOSS_ENABLED", "true"),
                "stop_loss_percent": _env("STOP_LOSS_PERCENT", "0.10"),
                "market_id": (bot_status.get("market_slug") or state.get("market", {}).get("slug") or get_active_market_slug() or "")[:48],
                "market_question": bot_status.get("market_question") or state.get("market", {}).get("question") or "",
                "market_end_date": bot_status.get("market_end_date") or state.get("market", {}).get("end_date") or "",
                "market_outcomes": bot_status.get("market_outcomes") or [],
                "target_market_slug": _env("TARGET_MARKET_SLUG", ""),
                "target_market_url": _env("TARGET_MARKET_URL", ""),
                "market_selection_mode": _env("MARKET_SELECTION_MODE", "manual"),
                "strategy_profile": _env("STRATEGY_PROFILE", "generic_binary"),
                "wallet": state.get("wallet") or (Config.get("POLYMARKET_WALLET_ADDRESS", "")[:10] + "..."),
                "ai_up_threshold": _env("AI_UP_THRESHOLD", "0.02"),
                "ai_down_threshold": _env("AI_DOWN_THRESHOLD", "-0.02"),
                "ai_min_confidence": _env("AI_MIN_CONFIDENCE", "0.60"),
                "paper_result": report.get("result"),
                "paper_profit": report.get("profit"),
                "paper_roi_percent": report.get("roi_percent"),
                "paper_balance": summary.get("ending_balance"),
                "paper_session_started_at": state.get("session_started_at") or summary.get("session_started_at") or report.get("session_started_at"),
                "cash_balance": summary.get("cash_balance"),
                "reserved_balance": summary.get("reserved_balance"),
                "open_positions": summary.get("open_positions"),
                "total_trades": summary.get("total_trades"),
                "paper_win_rate": summary.get("win_rate"),
                "daily_open": signal.get("daily_open"),
                "signal_price": signal.get("current_price"),
                "daily_change_percent": signal.get("change_percent"),
                "signal_reason": signal.get("reason"),
                "strategy_name": report.get("strategy") or "配置 AI 驱动决策",
                "ai_enabled": _env("AI_ENABLED", "true"),
                "ai_model": signal.get("ai_model") or _env("AI_MODEL", "gpt-4o-mini"),
                "AI_MODEL": _env("AI_MODEL", "gpt-4o-mini"),
                "AI_BASE_URL": _env("AI_BASE_URL", ""),
                "AI_DECISION_INTERVAL_SECONDS": _env("AI_DECISION_INTERVAL_SECONDS", "15"),
                "AI_MIN_CONFIDENCE": _env("AI_MIN_CONFIDENCE", "0.60"),
                "AI_TRADING_SKILL": _env("AI_TRADING_SKILL", ""),
                "ai_trading_skill": _env("AI_TRADING_SKILL", ""),
                "LIVE_MAX_OPEN_POSITIONS": _env("LIVE_MAX_OPEN_POSITIONS", "1"),
                "PAPER_MAX_OPEN_POSITIONS": _env("PAPER_MAX_OPEN_POSITIONS", "1"),
                "POLYMARKET_WALLET_ADDRESS": Config.get("POLYMARKET_WALLET_ADDRESS", ""),
                "POLYMARKET_FUNDER_ADDRESS": Config.get("POLYMARKET_FUNDER_ADDRESS", ""),
                "ai_source": signal.get("ai_source"),
                "ai_decision_id": signal.get("decision_id"),
                "exit_rule": f"best bid 浮盈 > ${Config.get('PAPER_TAKE_PROFIT_USD', '0.12')} 提前卖出，否则到期离场",
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
