"""Small, fail-closed client for Polymarket's public Chainlink RTDS feed.

The RTDS endpoint commonly sends a recent tick snapshot on subscription and
may not stream follow-up updates reliably.  Callers therefore treat every
fetch as a snapshot and must validate the measurement age before trading.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, Optional

import websockets

logger = logging.getLogger("chainlink_rtds")

RTDS_URL = "wss://ws-live-data.polymarket.com"
CHAINLINK_TOPIC = "crypto_prices_chainlink"
CHAINLINK_SYMBOL = "btc/usd"


def _as_ticks(message: Any) -> Iterable[Dict[str, Any]]:
    """Normalize both RTDS snapshot and single-update payload shapes."""
    if isinstance(message, str):
        try:
            message = json.loads(message)
        except (TypeError, ValueError):
            return ()
    if not isinstance(message, dict):
        return ()
    payload = message.get("payload")
    if not isinstance(payload, dict) or payload.get("symbol") != CHAINLINK_SYMBOL:
        return ()
    rows = payload.get("data")
    if isinstance(rows, list):
        return (row for row in rows if isinstance(row, dict))
    if "value" in payload:
        return (payload,)
    return ()


def parse_latest_tick(message: Any, *, now_ms: Optional[int] = None) -> Optional[Dict[str, Any]]:
    """Return the newest valid Chainlink tick from an RTDS frame."""
    ticks = []
    for row in _as_ticks(message):
        try:
            timestamp_ms = int(row.get("timestamp"))
            value = float(row.get("value"))
        except (TypeError, ValueError):
            continue
        if timestamp_ms <= 0 or value <= 0:
            continue
        if now_ms is not None and timestamp_ms > now_ms + 5_000:
            continue
        ticks.append((timestamp_ms, value))
    if not ticks:
        return None
    timestamp_ms, value = max(ticks)
    measured_at = datetime.fromtimestamp(timestamp_ms / 1000.0, tz=timezone.utc)
    return {
        "price": value,
        "measurement_ts_ms": timestamp_ms,
        "captured_at": measured_at.isoformat(),
        "source": "chainlink_rtds",
    }


class ChainlinkRTDSClient:
    """Fetch a fresh Chainlink snapshot with a short serialized cache."""

    def __init__(self, *, cache_seconds: float = 2.0, timeout_seconds: float = 5.0):
        self.cache_seconds = max(0.0, float(cache_seconds))
        self.timeout_seconds = max(1.0, float(timeout_seconds))
        self._latest: Optional[Dict[str, Any]] = None
        self._fetched_at = 0.0
        self._lock = asyncio.Lock()

    async def get_latest(self) -> Optional[Dict[str, Any]]:
        now = asyncio.get_running_loop().time()
        if self._latest is not None and now - self._fetched_at <= self.cache_seconds:
            logger.debug("Chainlink RTDS returning cached price (age=%.1fs)", now - self._fetched_at)
            return dict(self._latest)
        async with self._lock:
            now = asyncio.get_running_loop().time()
            if self._latest is not None and now - self._fetched_at <= self.cache_seconds:
                logger.debug("Chainlink RTDS returning cached price (race, age=%.1fs)", now - self._fetched_at)
                return dict(self._latest)
            logger.info("Chainlink RTDS fetching fresh price...")
            tick = await self._fetch_snapshot()
            if tick is None:
                logger.warning("Chainlink RTDS fetch returned None")
                return None
            tick["fetched_at"] = datetime.now(timezone.utc).isoformat()
            self._latest = tick
            self._fetched_at = asyncio.get_running_loop().time()
            logger.info("Chainlink RTDS price fetched: $%.2f, captured_at=%s", tick.get("price", 0), tick.get("captured_at", "?"))
            return dict(tick)

    async def _fetch_snapshot(self) -> Optional[Dict[str, Any]]:
        logger.info("Chainlink RTDS connecting to %s...", RTDS_URL)
        subscription = {
            "action": "subscribe",
            "subscriptions": [{
                "topic": CHAINLINK_TOPIC,
                "type": "update",
                "filters": json.dumps({"symbol": CHAINLINK_SYMBOL}),
            }],
        }
        try:
            async with websockets.connect(
                RTDS_URL,
                open_timeout=self.timeout_seconds,
                close_timeout=1,
                ping_interval=20,
            ) as socket:
                logger.info("Chainlink RTDS WebSocket connected, subscribing...")
                await socket.send(json.dumps(subscription))
                logger.info("Chainlink RTDS subscribed, waiting for snapshot (timeout=%.1fs)...", self.timeout_seconds)
                deadline = asyncio.get_running_loop().time() + self.timeout_seconds
                frames_received = 0
                while asyncio.get_running_loop().time() < deadline:
                    remaining = max(0.1, deadline - asyncio.get_running_loop().time())
                    frame = await asyncio.wait_for(socket.recv(), timeout=remaining)
                    frames_received += 1
                    tick = parse_latest_tick(frame, now_ms=int(datetime.now(timezone.utc).timestamp() * 1000))
                    if tick is not None:
                        logger.info("Chainlink RTDS parsed tick #%d: $%.2f", frames_received, tick.get("price", 0))
                        return tick
                logger.warning("Chainlink RTDS received %d frames but no valid tick", frames_received)
        except (asyncio.TimeoutError, OSError, ValueError, websockets.WebSocketException) as exc:
            logger.warning("Chainlink RTDS snapshot unavailable: %s", exc)
        return None
