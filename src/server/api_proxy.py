#!/usr/bin/env python3
"""
status_server Polymarket API 代理模块
拆出 clob_get / data_api_get / fetch_market_snapshot 等.
"""

from __future__ import annotations

import json
import os
import socket
import urllib.request
import urllib.error
import urllib.parse

from src.server.helpers import (
    load_status_from_file, load_paper_state,
    normalize_orderbook_levels, derive_display_price, derive_complement_price,
)

# 项目路径
CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(os.path.dirname(CURRENT_DIR))
DATA_DIR = os.path.join(ROOT_DIR, "data")

# Polymarket API 基址
CLOB_BASE = "https://clob.polymarket.com"
DATA_API_BASE = "https://data-api.polymarket.com"
GAMMA_BASE = "https://gamma-api.polymarket.com"


def _is_local_proxy_url(value: str) -> tuple[str, int] | None:
    try:
        parsed = urllib.parse.urlparse(str(value or "").strip())
        if parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
            return None
        if not parsed.port:
            return None
        return parsed.hostname, int(parsed.port)
    except Exception:
        return None


def _should_bypass_dead_local_proxy() -> bool:
    """If proxy env points to a dead localhost port, bypass urllib env proxies."""
    for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        proxy = os.environ.get(key)
        endpoint = _is_local_proxy_url(proxy)
        if not endpoint:
            continue
        host, port = endpoint
        try:
            with socket.create_connection((host, port), timeout=0.25):
                return False
        except OSError:
            return True
    return False


def _urlopen(req, timeout=10):
    if _should_bypass_dead_local_proxy():
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        return opener.open(req, timeout=timeout)
    return urllib.request.urlopen(req, timeout=timeout)


def _parse_json_list(value):
    """安全解析 JSON 列表字段 (可能已经是 list 或 JSON 字符串)."""
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            return json.loads(value)
        except (json.JSONDecodeError, TypeError):
            pass
    return []


def _extract_market_slug(input_str):
    """从 URL 或 slug 中提取 market slug"""
    if not input_str:
        return ""
    if input_str.startswith("http"):
        from urllib.parse import urlparse
        parsed = urlparse(input_str)
        # 兼容 https://polymarket.com/event/... 和 /market/... 两种路径
        path_parts = [p for p in parsed.path.split("/") if p]
        for i, part in enumerate(path_parts):
            if part in ("market", "markets") and i + 1 < len(path_parts):
                return path_parts[i + 1]
            if part.startswith("btc-updown-"):
                return part
        return ""
    return input_str.strip()


def build_hmac_headers(api_key, api_secret, passphrase, method, path, body=""):
    """构建 Polymarket L2 HMAC-SHA256 认证头"""
    import base64, hashlib, hmac as hmac_mod, time as time_mod
    timestamp = str(int(time_mod.time()))
    message = timestamp + method.upper() + path + body
    secret_decoded = base64.urlsafe_b64decode(api_secret)
    signature = base64.urlsafe_b64encode(
        hmac_mod.new(secret_decoded, message.encode("utf-8"), hashlib.sha256).digest()
    ).decode("utf-8")
    return {
        "POLY-API-KEY": api_key,
        "POLY-SIGNATURE": signature,
        "POLY-TIMESTAMP": timestamp,
        "POLY-PASSPHRASE": passphrase,
    }


def http_json_get(url, timeout=10):
    """通用 GET 请求, 返回 JSON dict 或 {"error": ...}"""
    req = urllib.request.Request(url)
    req.add_header("Accept", "application/json")
    req.add_header("User-Agent", "Mozilla/5.0")
    try:
        with _urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        body = e.read().decode() if e.fp else ""
        return {"error": f"HTTP {e.code}", "detail": body}
    except Exception as e:
        return {"error": str(e)}


def clob_get(path, api_key="", api_secret="", passphrase="", timeout=10):
    """向 Polymarket CLOB API 发起带 HMAC 签名的 GET 请求"""
    url = f"{CLOB_BASE}{path}"
    req = urllib.request.Request(url)
    req.add_header("Content-Type", "application/json")
    req.add_header("Accept", "application/json")
    req.add_header("User-Agent", "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36")
    if api_key and api_secret and passphrase:
        headers = build_hmac_headers(api_key, api_secret, passphrase, "GET", path)
        for k, v in headers.items():
            req.add_header(k, v)
    try:
        with _urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        body = e.read().decode() if e.fp else ""
        return {"error": f"HTTP {e.code}", "detail": body}
    except Exception as e:
        return {"error": str(e)}


def data_api_get(path, timeout=10):
    """向 Polymarket Data API 发起 GET 请求（公开端点，无需认证）"""
    url = f"{DATA_API_BASE}{path}"
    req = urllib.request.Request(url)
    req.add_header("Accept", "application/json")
    req.add_header("User-Agent", "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36")
    try:
        with _urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        body = e.read().decode() if e.fp else ""
        return {"error": f"HTTP {e.code}", "detail": body}
    except Exception as e:
        return {"error": str(e)}


def fetch_market_snapshot(slug, timeout=10):
    """从 Gamma API 拉取 market 快照"""
    snapshot = http_json_get(f"{GAMMA_BASE}/markets/slug/{slug}", timeout=timeout)
    if snapshot and not snapshot.get("error") and _parse_json_list(snapshot.get("clobTokenIds")):
        snapshot.setdefault("slug", slug)
        return snapshot
    data = http_json_get(f"{GAMMA_BASE}/events?slug={slug}", timeout=timeout)
    if not isinstance(data, list) or not data:
        return snapshot if isinstance(snapshot, dict) else {"error": "未找到市场"}
    event = data[0]
    markets = [item for item in (event.get("markets") or []) if _parse_json_list(item.get("clobTokenIds"))]
    if not markets:
        return {"error": "事件下没有可交易市场"}
    exact = None
    for item in markets:
        if str(item.get("slug") or "").strip() == slug:
            exact = item
            break
    if exact is None:
        if len(markets) == 1:
            exact = markets[0]
        else:
            binary_markets = [item for item in markets if len(_parse_json_list(item.get("clobTokenIds"))) == 2]
            if len(binary_markets) == 1:
                exact = binary_markets[0]
            else:
                return {"error": "该事件包含多个市场，请使用具体 market slug"}
    merged = dict(exact)
    merged.setdefault("slug", slug)
    merged.setdefault("question", event.get("title"))
    merged.setdefault("endDate", event.get("endDate"))
    merged.setdefault("liquidity", event.get("liquidity"))
    merged.setdefault("active", event.get("active"))
    merged.setdefault("closed", event.get("closed"))
    merged.setdefault("negRisk", event.get("negRisk"))
    return merged


def build_synthetic_orderbook(snapshot):
    """当 CLOB 盘口不可用时，从 Gamma 快照合成盘口数据"""
    outcomes = _parse_json_list(snapshot.get("outcomes")) or ["YES", "NO"]
    if len(outcomes) != 2:
        return {"error": "当前版本仅支持二元盘口"}
    up_bid = snapshot.get("bestBid")
    up_ask = snapshot.get("bestAsk")
    outcome_prices = [float(item) for item in _parse_json_list(snapshot.get("outcomePrices")) or [0.5, 0.5]]
    if len(outcome_prices) < 2:
        outcome_prices = [0.5, 0.5]
    if up_bid is None:
        up_bid = round(max(0.01, float(outcome_prices[0]) - 0.01), 4)
    if up_ask is None:
        up_ask = round(min(0.99, float(outcome_prices[0]) + 0.01), 4)
    up_mid, up_spread = derive_display_price(outcome_prices[0], float(up_bid), float(up_ask))
    if up_mid is None:
        up_mid = round(float(outcome_prices[0]), 4)
    down_bid = derive_complement_price(up_ask)
    down_ask = derive_complement_price(up_bid)
    down_mid, down_spread = derive_display_price(outcome_prices[1], down_bid or 0.5, down_ask or 0.5)
    if down_mid is None:
        down_mid = round(float(outcome_prices[1]), 4)
    return {
        "source": "snapshot_fallback",
        "market": snapshot.get("question"),
        "closed": snapshot.get("closed"),
        "updated_at": snapshot.get("updatedAt"),
        "outcomes": [
            {
                "label": str(outcomes[0]).upper(),
                "mid": round(up_mid, 4),
                "best_bid": round(float(up_bid), 4),
                "best_ask": round(float(up_ask), 4),
                "spread": round(up_spread or 0.0, 4),
                "bids": [{"price": round(float(up_bid), 4), "size": snapshot.get("liquidity", "--")}],
                "asks": [{"price": round(float(up_ask), 4), "size": snapshot.get("liquidity", "--")}],
            },
            {
                "label": str(outcomes[1]).upper() if len(outcomes) > 1 else "DOWN",
                "mid": round(down_mid, 4),
                "best_bid": round(float(down_bid or max(0.01, down_mid - 0.01)), 4),
                "best_ask": round(float(down_ask or min(0.99, down_mid + 0.01)), 4),
                "spread": round(down_spread or 0.0, 4),
                "bids": [{"price": round(float(down_bid or max(0.01, down_mid - 0.01)), 4), "size": snapshot.get("liquidity", "--")}],
                "asks": [{"price": round(float(down_ask or min(0.99, down_mid + 0.01)), 4), "size": snapshot.get("liquidity", "--")}],
            },
        ],
    }


def fetch_order_book(slug, timeout=10):
    """获取指定 slug 的完整盘口数据 (CLOB)"""
    snapshot = fetch_market_snapshot(slug, timeout=timeout)
    if snapshot.get("error"):
        return snapshot
    if snapshot.get("closed"):
        return {
            "source": "closed",
            "market": snapshot.get("question") or slug,
            "closed": True,
            "message": "盘口已关闭，当前无活跃 BTC 15m 窗口",
            "outcomes": [],
        }
    token_ids = _parse_json_list(snapshot.get("clobTokenIds"))
    outcomes = _parse_json_list(snapshot.get("outcomes"))
    outcome_prices = [float(item) for item in _parse_json_list(snapshot.get("outcomePrices")) or []]
    if len(token_ids) != 2:
        return {"error": "当前版本仅支持二元盘口"}
    books = []
    source = "clob"
    for idx, token_id in enumerate(token_ids):
        book = http_json_get(f"{CLOB_BASE}/book?token_id={token_id}", timeout=timeout)
        label = str(outcomes[idx]).upper() if idx < len(outcomes) else f"OUTCOME {idx+1}"
        if book.get("error"):
            source = "snapshot_fallback"
            return build_synthetic_orderbook(snapshot)
        bids = normalize_orderbook_levels(book.get("bids"), descending=True)
        asks = normalize_orderbook_levels(book.get("asks"), descending=False)
        best_bid = bids[0]["price"] if bids else None
        best_ask = asks[0]["price"] if asks else None
        last_trade = outcome_prices[idx] if idx < len(outcome_prices) else None
        mid, spread = derive_display_price(last_trade, best_bid, best_ask)
        books.append({
            "label": label,
            "mid": round(float(mid), 4) if mid is not None else 0.5,
            "best_bid": best_bid,
            "best_ask": best_ask,
            "spread": round(spread or 0.0, 4),
            "bids": bids[:3],
            "asks": asks[:3],
        })
    return {
        "source": source,
        "market": snapshot.get("question"),
        "closed": snapshot.get("closed"),
        "updated_at": snapshot.get("updatedAt"),
        "outcomes": books,
    }


def find_current_btc15m_slug(timeout=5):
    """动态发现当前活跃的 BTC 15m 市场 slug"""
    import math, time as time_mod
    ts = int(time_mod.time())
    base = math.ceil(ts / 900) * 900
    for i in range(4):
        candidate_ts = base + i * 900
        slug = f"btc-updown-15m-{candidate_ts}"
        try:
            data = http_json_get(f"{GAMMA_BASE}/events?slug={slug}", timeout=timeout)
            if isinstance(data, list) and data:
                event = data[0]
                if event.get("active") and not event.get("closed"):
                    return slug
        except Exception:
            continue
    return ""


def get_active_market_slug():
    """获取当前活跃的 market slug (bot_status > paper_state > 配置 > 自动发现)"""
    status = load_status_from_file() or {}
    if status.get("market_slug"):
        return status["market_slug"]
    paper_state = load_paper_state() or {}
    market = paper_state.get("market", {})
    if market.get("slug"):
        return market["slug"]
    # 自动发现
    dynamic = find_current_btc15m_slug()
    if dynamic:
        return dynamic
    return ""


# ---- 链上余额 ----

_REAL_BALANCE_TIMEOUT_SECS = 2


def fetch_usdc_balance_polygonscan(wallet, api_key="", timeout=_REAL_BALANCE_TIMEOUT_SECS):
    """使用 Polygonscan V2 查询 Polygon 上的 USDC 余额"""
    query = urllib.parse.urlencode({
        "chainid": "137",
        "module": "account",
        "action": "tokenbalance",
        "contractaddress": "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174",
        "address": wallet,
        "tag": "latest",
        "apikey": api_key,
    })
    req = urllib.request.Request(f"https://api.etherscan.io/v2/api?{query}")
    req.add_header("Accept", "application/json")
    req.add_header("User-Agent", "Mozilla/5.0")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        result = json.loads(resp.read().decode())
    if result.get("status") == "1" and result.get("result") is not None:
        return int(result["result"]) / 1e6, "etherscan_v2"
    raise RuntimeError(result.get("result") or result.get("message") or "余额查询失败")


def fetch_usdc_balance_rpc(wallet, timeout=_REAL_BALANCE_TIMEOUT_SECS):
    """公共 Polygon RPC 兜底查询 USDC 余额"""
    wallet_hex = wallet.lower().replace("0x", "").rjust(64, "0")
    payload = json.dumps({
        "jsonrpc": "2.0",
        "method": "eth_call",
        "params": [{
            "to": "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174",
            "data": "0x70a08231" + wallet_hex,
        }, "latest"],
        "id": 1,
    }).encode("utf-8")
    req = urllib.request.Request(
        "https://gateway.tenderly.co/public/polygon",
        data=payload,
        headers={"Content-Type": "application/json", "Accept": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        result = json.loads(resp.read().decode())
    value = result.get("result")
    if not value:
        raise RuntimeError(result.get("error", {}).get("message", "RPC 返回为空"))
    return int(value, 16) / 1e6, "polygon_rpc"


def get_real_wallet_balance(wallet, api_key=""):
    """查询真实 Polymarket 资金钱包余额"""
    if not wallet:
        raise RuntimeError("未配置钱包地址")
    try:
        balance, source = fetch_usdc_balance_polygonscan(wallet, api_key, timeout=_REAL_BALANCE_TIMEOUT_SECS)
    except Exception:
        balance, source = fetch_usdc_balance_rpc(wallet, timeout=_REAL_BALANCE_TIMEOUT_SECS)
    return {
        "balance": balance,
        "wallet": wallet,
        "source": source,
    }
