import json
import os
import time
from pathlib import Path


def test_get_btc_price_prefers_fresh_snapshot(monkeypatch, tmp_path):
    from src.server import market_data

    snapshot = tmp_path / "btc_snapshot.json"
    snapshot.write_text(json.dumps({
        "price": 59999.5,
        "change_24h": -1.25,
        "source": "binance",
    }))
    now = time.time()
    os.utime(snapshot, (now, now))

    monkeypatch.setattr(market_data, "BTC_SNAPSHOT_FILE", str(snapshot))
    monkeypatch.setattr(market_data, "_BTC_CACHE", None)
    monkeypatch.setattr(market_data, "_BTC_CACHE_TS", 0.0)

    def fail_get(*args, **kwargs):
        raise AssertionError("network must not be called when snapshot is fresh")

    monkeypatch.setattr(market_data.requests, "get", fail_get)

    result = market_data.get_btc_price()
    assert result["price"] == 59999.5
    assert result["change_24h"] == -1.25
    assert result["source"] == "snapshot:binance"
    assert result["cached"] is True


def test_get_btc_price_reuses_memory_cache_on_429(monkeypatch, tmp_path):
    from src.server import market_data

    missing_snapshot = tmp_path / "missing.json"
    monkeypatch.setattr(market_data, "BTC_SNAPSHOT_FILE", str(missing_snapshot))
    monkeypatch.setattr(market_data, "_BTC_CACHE", {
        "price": 60001.0,
        "change_24h": 0.12,
        "source": "coingecko",
    })
    monkeypatch.setattr(market_data, "_BTC_CACHE_TS", time.time() - 120)
    monkeypatch.setattr(market_data, "_BTC_MIN_FETCH_INTERVAL", 0.0)
    monkeypatch.setenv("BINANCE_PROXY_URL", "")
    monkeypatch.setenv("BINANCE_PROXY_URLS", "")

    class DummyResponse:
        url = "https://api.coingecko.com/api/v3/simple/price"
        def raise_for_status(self):
            import requests
            raise requests.HTTPError("429 Client Error: Too Many Requests for url: " + self.url)

    monkeypatch.setattr(market_data, "_direct_get", lambda *args, **kwargs: DummyResponse())

    result = market_data.get_btc_price()
    assert result["price"] == 60001.0
    assert result["cached"] is True
    assert "429" in result["warning"]
    assert "error" not in result
