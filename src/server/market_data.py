#!/usr/bin/env python3
"""
status_server BTC 价格模块
拆出 get_btc_price / _get_coingecko_price.
使用 requests + proxies= 替代全局 socket monkey-patch.
"""

import json
import os
import time
import requests


CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(os.path.dirname(CURRENT_DIR))
DATA_DIR = os.path.join(ROOT_DIR, "data")
BTC_SNAPSHOT_FILE = os.path.join(DATA_DIR, "btc_snapshot.json")

# Dashboard /api/btc may be polled frequently by every open browser tab.  Do not
# let those UI polls hit CoinGecko directly; Vultr/cloud IPs are rate-limited
# quickly and the bot already writes btc_snapshot.json for the reference panel.
_BTC_CACHE = None
_BTC_CACHE_TS = 0.0
_BTC_MIN_FETCH_INTERVAL = 60.0
_SNAPSHOT_MAX_AGE = 180.0
_BTC_FETCH_BUDGET_SECS = 4.0
_BTC_REQUEST_TIMEOUT_SECS = 2.0


def _load_snapshot_price(max_age_secs=_SNAPSHOT_MAX_AGE):
    """Return the bot-written BTC snapshot if it is fresh enough."""
    try:
        if not os.path.exists(BTC_SNAPSHOT_FILE):
            return None
        with open(BTC_SNAPSHOT_FILE, "r", encoding="utf-8") as f:
            snap = json.load(f) or {}
        price = snap.get("price")
        if price is None:
            return None
        age = time.time() - os.path.getmtime(BTC_SNAPSHOT_FILE)
        if age > max_age_secs:
            return None
        return {
            "price": float(price),
            "change_24h": float(snap.get("change_24h", 0) or 0),
            "source": f"snapshot:{snap.get('source', 'unknown')}",
            "cached": True,
            "cache_age_secs": int(age),
        }
    except Exception:
        return None


def _direct_get(url, **kwargs):
    """GET without inheriting shell-level HTTP(S)_PROXY/ALL_PROXY.

    The Hermes/macOS shell may export a SOCKS proxy for unrelated tooling; requests
    needs PySocks for that and otherwise raises "Missing dependencies for SOCKS
    support" even for what should be a direct CoinGecko fallback.
    """
    session = requests.Session()
    session.trust_env = False
    try:
        return session.get(url, **kwargs)
    finally:
        session.close()


def get_btc_price():
    """从 Binance 拉取 BTC 实时价格 (走 SOCKS5 代理, 失败时降级到 CoinGecko)

    使用 requests.get(url, proxies=...) 避免全局 socket monkey-patch,
    后者在多线程环境下(ThreadingTCPServer)存在竞态条件.
    """
    global _BTC_CACHE, _BTC_CACHE_TS

    # Prefer the bot's own cached snapshot. This keeps the dashboard usable even
    # when CoinGecko is currently returning HTTP 429.
    snapshot = _load_snapshot_price()
    if snapshot:
        _BTC_CACHE = snapshot
        _BTC_CACHE_TS = time.time()
        return snapshot

    now = time.time()
    if _BTC_CACHE and now - _BTC_CACHE_TS < _BTC_MIN_FETCH_INTERVAL:
        cached = dict(_BTC_CACHE)
        cached["cached"] = True
        cached["cache_age_secs"] = int(now - _BTC_CACHE_TS)
        return cached

    proxy_url = os.environ.get("BINANCE_PROXY_URL", "")
    proxy_urls = os.environ.get("BINANCE_PROXY_URLS", "")
    proxies = [p.strip() for p in proxy_urls.split(",") if p.strip()]
    if not proxies and proxy_url:
        proxies = [proxy_url]
    started_at = time.time()

    headers = {"User-Agent": "Mozilla/5.0"}

    def _remaining_budget():
        return _BTC_FETCH_BUDGET_SECS - (time.time() - started_at)

    def _fetch_via(proxy_url):
        try:
            timeout = min(_BTC_REQUEST_TIMEOUT_SECS, max(0.5, _remaining_budget()))
            r = requests.get(
                "https://api.binance.com/api/v3/ticker/24hr?symbol=BTCUSDT",
                proxies={"http": proxy_url, "https": proxy_url},
                headers=headers,
                timeout=timeout,
                verify=False,  # Binance over SOCKS5: CDN 证书不匹配, 必须跳过
            )
            r.raise_for_status()
            data = r.json()
            return {
                "price": float(data["lastPrice"]),
                "change_24h": float(data["priceChangePercent"]),
                "high_24h": float(data["highPrice"]),
                "low_24h": float(data["lowPrice"]),
                "volume_24h": float(data["volume"]),
                "source": "binance",
            }
        except Exception as exc:
            print(f"⚠️ SOCKS5 fetch via {proxy_url} failed: {exc}")
            return None

    # 先试代理池, 一个成功即可
    for p in proxies:
        if _remaining_budget() <= 0:
            break
        result = _fetch_via(p)
        if result:
            _BTC_CACHE = result
            _BTC_CACHE_TS = now
            return result

    # 全部代理失败, 降级到 CoinGecko (直连)
    try:
        timeout = min(_BTC_REQUEST_TIMEOUT_SECS, max(0.5, _remaining_budget()))
        r = _direct_get(
            "https://api.coingecko.com/api/v3/simple/price?ids=bitcoin&vs_currencies=usd&include_24hr_change=true",
            headers=headers,
            timeout=timeout,
        )
        r.raise_for_status()
        data = r.json()
        btc = data.get("bitcoin", {})
        result = {
            "price": float(btc.get("usd", 0)),
            "change_24h": float(btc.get("usd_24h_change", 0)),
            "source": "coingecko",
        }
        _BTC_CACHE = result
        _BTC_CACHE_TS = now
        return result
    except Exception as exc:
        print(f"⚠️ CoinGecko also failed: {exc}")
        if _BTC_CACHE:
            cached = dict(_BTC_CACHE)
            cached["cached"] = True
            cached["cache_age_secs"] = int(now - _BTC_CACHE_TS)
            cached["warning"] = f"live BTC refresh failed: {exc}"
            return cached
        return {"error": f"BTC price unavailable: {exc}"}


def _get_coingecko_price():
    """CoinGecko 兜底获取 BTC 价格"""
    try:
        r = _direct_get(
            "https://api.coingecko.com/api/v3/simple/price?ids=bitcoin&vs_currencies=usd&include_24hr_change=true",
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=8,
        )
        r.raise_for_status()
        data = r.json()
        btc = data.get("bitcoin", {})
        return {
            "price": float(btc.get("usd", 0)),
            "change_24h": float(btc.get("usd_24h_change", 0)),
            "source": "coingecko",
        }
    except Exception as exc:
        return {"error": f"BTC price unavailable: {exc}"}
