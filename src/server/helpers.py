#!/usr/bin/env python3
"""
status_server 工具函数模块
拆出 load_json_file / send_json / 盘口工具等纯函数.
"""

import json
import os
from datetime import datetime, timezone

# 项目路径 (与 status_server.py 共享)
CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(os.path.dirname(CURRENT_DIR))
DATA_DIR = os.path.join(ROOT_DIR, "data")
CONTROL_FILE = os.path.join(DATA_DIR, "trading_control.json")
STATUS_FILE = os.path.join(DATA_DIR, "bot_status.json")
PAPER_STATE_FILE = os.path.join(DATA_DIR, "paper_trade_state.json")


def load_json_file(path, default=None):
    """加载 JSON 文件, 失败返回 default."""
    try:
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        pass
    return default


def load_status_from_file():
    """从 bot_status.json 加载 Bot 状态"""
    return load_json_file(STATUS_FILE, None)


def load_paper_state():
    """从 paper_trade_state.json 加载模拟交易数据"""
    return load_json_file(PAPER_STATE_FILE, None)


def save_trading_control(trading_enabled):
    """写 trading_control.json"""
    control = load_json_file(CONTROL_FILE, {})
    control["trading_enabled"] = bool(trading_enabled)
    control["updated_at"] = datetime.now(timezone.utc).isoformat()
    try:
        with open(CONTROL_FILE, "w", encoding="utf-8") as f:
            json.dump(control, f, indent=2, ensure_ascii=False)
    except Exception:
        pass
    return control


# ---- HTTP 响应工具 ----


def send_json(handler, data, status=200):
    """Write a JSON response. Swallows BrokenPipeError."""
    try:
        handler.send_response(status)
        handler.send_header("Content-type", "application/json; charset=utf-8")
        handler.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
        handler.send_header("Pragma", "no-cache")
        handler.send_header("Expires", "0")
        handler.end_headers()
        handler.wfile.write(json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8"))
    except (BrokenPipeError, ConnectionResetError):
        return


# ---- 盘口工具 ----


def derive_complement_price(value):
    """二项盘口下，计算对方的公允价格 (1 - value)"""
    try:
        return round(max(0.0, min(1.0, 1 - float(value))), 4)
    except Exception:
        return None


def normalize_orderbook_levels(levels, descending=False):
    """将原始盘口数据标准化为 [{"price": ..., "size": ...}, ...]"""
    normalized = []
    for level in levels or []:
        try:
            normalized.append({
                "price": round(float(level.get("price", 0)), 4),
                "size": round(float(level.get("size", 0)), 4),
            })
        except Exception:
            continue
    return sorted(normalized, key=lambda item: item["price"], reverse=descending)


def derive_display_price(last_trade_price, best_bid, best_ask):
    """根据最新成交价、bid、ask 估算合理显示价"""
    spread = None
    if best_bid is not None and best_ask is not None:
        spread = round(best_ask - best_bid, 4)
    if spread is not None and spread <= 0.10:
        return round((best_bid + best_ask) / 2, 4), spread
    if last_trade_price is not None:
        return round(float(last_trade_price), 4), spread
    if best_bid is not None and best_ask is not None:
        return round((best_bid + best_ask) / 2, 4), spread
    return None, spread
