"""Polymarket WebSocket client for real-time CLOB order-book streaming.

Why a WS client at all
----------------------
Polymarket publishes incremental CLOB updates over a public WebSocket feed:

    wss://ws-subscriptions-clob.polymarket.com/ws/market

Compared with polling ``GET /book?token_id=...`` every 60s, the WS feed gives us:

* sub-second top-of-book refresh (vs ~60s polling latency)
* ability to see price changes *between* our snapshots, so the
  arbitrage / signal pipeline reacts before the market has already
  re-priced

This module is intentionally narrow in scope:

* it does **not** make any trading decisions
* it only maintains a local copy of every subscribed token's order book
  and exposes thread-safe / task-safe accessors

Public surface
--------------
* ``PolymarketWSClient`` - per-token live CLOB cache
* ``PolymarketWSClient.instance()`` - module-level singleton accessor
* ``client.apply_message(msg)`` - synchronous dispatcher (used by the
  background read loop and the unit tests)
* ``client.get_book_snapshot(token_id)`` - synchronous read of the
  current top-of-book
* ``client.best_bid_ask(token_id)`` - convenience: ``(bid, ask)`` tuple
* ``await client.wait_for_book(token_id, timeout)`` - block until the
  first book frame arrives, then return the snapshot
* ``await client.connect(token_ids)`` - subscribe and start streaming
* ``await client.disconnect()`` - clean shutdown
* ``client.is_running`` - ``True`` while the background task is alive

Message format (from Polymarket docs)
-------------------------------------
* subscribe ack::

    {"type": "subscribed", "channel": "book", "assets_id": "..."}

* book snapshot (initial): full bids/asks list::

    {"channel": "book",
     "data": {"asset_id": "...", "bids": [{"price": "0.55", "size": "100"}],
              "asks": [{"price": "0.57", "size": "200"}], "hash": "..."}}

  When ``"hash"`` is present the server is asking us to reconcile against
  REST because the local state has drifted - we drop our local copy
  and re-seed from the new snapshot.

* price_change (incremental): single price-level update::

    {"channel": "price_change",
     "data": {"asset_id": "...", "price": "0.55", "size": "100",
              "side": "BUY" | "SELL"}}

Thread / task safety
--------------------
The bot runs many coroutines in the same loop. The singleton is shared.
Two rules of thumb:

* All public mutating ops are synchronous and fast (no I/O on the hot
  path) - safe to call from any task or thread that owns the loop.
* The message-decoding helpers (``_handle_book``, ``_handle_price_change``)
  are pure functions of the message dict so they can be unit-tested
  without any network.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from typing import Any, Dict, List, Optional, Tuple

import websockets
from websockets.exceptions import ConnectionClosed

logger = logging.getLogger("polymarket_ws")

# Public Polymarket WS endpoint for CLOB market data
PM_WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"

# Reconnect backoff (seconds). Cycles back to start after the last entry.
RECONNECT_BACKOFF: Tuple[int, ...] = (1, 2, 5, 10, 30)

# After this many failed handshakes WITHOUT ever connecting, give up the
# WS background loop. The sum of RECONNECT_BACKOFF (n entries) is bounded;
# 8 attempts ≈ 1+2+5+10+30+30+30+30 ≈ 2.5 minutes of trying, then
# quiet REST-only mode. Counted attempts only (failed connects), resets
# to 0 the moment a successful handshake completes.
MAX_RECONNECT_ATTEMPTS: int = 8

# Bug fix 2026-06-27: 首次连接成功后, 任何时候连续失败 ≥ MAX_CONSECUTIVE_FAILURES
# 也退出. 否则网络持续抖动时日志刷屏 (你 cron 报 "WS 未触发 give-up 但 warning 过多")
MAX_CONSECUTIVE_FAILURES: int = 8

# Default timeout (seconds) when waiting for the first book event
DEFAULT_BOOK_WAIT_TIMEOUT = 2.0

# Book depth returned to callers (mirrors get_microstructure REST depth)
TOP_OF_BOOK_DEPTH = 5

# Side values Polymarket uses in price_change events
_BUY = "BUY"
_SELL = "SELL"
_VALID_SIDES = {_BUY, _SELL}


# ---------------------------------------------------------------------------
# Pure helpers (no I/O - safe to unit test)
# ---------------------------------------------------------------------------

def _coerce_levels(levels: Any) -> List[Tuple[float, float]]:
    """Normalize bids/asks lists to ``[(price, size), ...]``.

    Polymarket sends prices and sizes as strings; some intermediate
    proxies / docs send them as floats. Accept both.
    Malformed entries are dropped (we never want one bad row to take
    down the whole snapshot).
    """
    if not levels or not isinstance(levels, list):
        return []
    out: List[Tuple[float, float]] = []
    for item in levels:
        if not isinstance(item, dict):
            continue
        raw_price = item.get("price")
        raw_size = item.get("size")
        if raw_price is None or raw_size is None:
            continue
        try:
            p = float(raw_price)
            s = float(raw_size)
        except (TypeError, ValueError):
            continue
        if p < 0 or s < 0:
            continue
        out.append((p, s))
    return out


def derive_best_quotes(
    bids: List[Tuple[float, float]],
    asks: List[Tuple[float, float]],
) -> Tuple[Optional[float], Optional[float]]:
    """Return ``(best_bid, best_ask)`` from order-book level lists.

    Be defensive and compute max/min here instead of relying on callers
    to pre-sort. A stale or partially-updated WS book must not publish a
    lower bid / higher ask merely because a list got out of order.
    """
    best_bid = max((p for p, _ in bids), default=None)
    best_ask = min((p for p, _ in asks), default=None)
    return best_bid, best_ask


def _backoff_delay(attempt_idx: int) -> float:
    """Pick the reconnect delay for ``attempt_idx``.

    Cycles through ``RECONNECT_BACKOFF`` indefinitely - we never wait
    forever. Pure function so it's trivial to test.
    """
    n = len(RECONNECT_BACKOFF)
    if n == 0:
        return 1.0
    return float(RECONNECT_BACKOFF[attempt_idx % n])


def _message_channel(msg: Any) -> Optional[str]:
    """Extract the channel name from a frame dict.

    Frames use ``channel`` for data messages and ``type`` for
    subscribe acks. Returns the channel-equivalent string or ``None``
    if the input isn't a usable dict.
    """
    if not isinstance(msg, dict):
        return None
    ch = msg.get("channel")
    if isinstance(ch, str) and ch:
        return ch
    mtype = msg.get("type")
    if isinstance(mtype, str) and mtype:
        return mtype
    return None


def _extract_asset_id(msg: Dict[str, Any]) -> Optional[str]:
    """Find the asset id from a Polymarket frame, supporting both
    ``asset_id`` (current docs) and ``assets_id`` (older payloads)."""
    if not isinstance(msg, dict):
        return None
    data = msg.get("data")
    if isinstance(data, dict):
        for k in ("asset_id", "assets_id"):
            v = data.get(k)
            if isinstance(v, str) and v:
                return v
    for k in ("asset_id", "assets_id"):
        v = msg.get(k)
        if isinstance(v, str) and v:
            return v
    return None


# ---------------------------------------------------------------------------
# Per-token book state
# ---------------------------------------------------------------------------

class _BookState:
    """Mutable order book for a single asset.

    We keep bids and asks as plain lists sorted by price (bids
    descending, asks ascending). Each list holds ``(price, size)``
    tuples. Polymarket ``price_change`` events are size-updates: if
    ``size == 0`` we delete the level, otherwise we replace it (one
    price can only have one size at a time).
    """

    __slots__ = ("asset_id", "bids", "asks", "hash", "last_update_ts")

    def __init__(self, asset_id: str) -> None:
        self.asset_id = asset_id
        # bids: highest first; asks: lowest first
        self.bids: List[Tuple[float, float]] = []
        self.asks: List[Tuple[float, float]] = []
        self.hash: Optional[str] = None
        self.last_update_ts: float = 0.0

    def apply_snapshot(
        self,
        bids: List[Tuple[float, float]],
        asks: List[Tuple[float, float]],
        hash_: Optional[str],
    ) -> bool:
        # Full replacement - sort and dedupe by price
        self.bids = sorted({(p, s) for (p, s) in bids}, key=lambda x: -x[0])
        self.asks = sorted({(p, s) for (p, s) in asks}, key=lambda x: x[0])
        self.hash = hash_
        self.last_update_ts = time.time()
        return True

    def apply_price_change(self, price: float, size: float, side: str) -> bool:
        if side not in _VALID_SIDES:
            return False
        # Polymarket price_change side is taker side: BUY lifts/reduces asks,
        # SELL hits/reduces bids. Updating the opposite side pollutes best_bid
        # / best_ask and can trigger false LowBuy entry/TP decisions.
        book = self.asks if side == _BUY else self.bids
        # Find existing level at this price (linear scan; depth is small)
        idx = None
        for i, (p, _) in enumerate(book):
            if p == price:
                idx = i
                break
        if size <= 0.0:
            if idx is not None:
                book.pop(idx)
        else:
            if idx is not None:
                book[idx] = (price, size)
            else:
                book.append((price, size))
                # Keep sorted; bids desc, asks asc
                book.sort(key=lambda x, _side=side: -x[0] if _side == _BUY else x[0])
        self.last_update_ts = time.time()
        return True

    def to_snapshot(self, depth: int = TOP_OF_BOOK_DEPTH) -> Dict[str, Any]:
        # The test suite and PolymarketClient.get_microstructure both
        # expect this shape: {"bids": [(price, size), ...], "asks": [...],
        # "updated_at": float, "hash": str|None}. PolymarketClient does
        # the string formatting downstream.
        return {
            "asset_id": self.asset_id,
            "bids": self.bids[:depth],
            "asks": self.asks[:depth],
            "hash": self.hash,
            "updated_at": self.last_update_ts,
        }


# ---------------------------------------------------------------------------
# Main client
# ---------------------------------------------------------------------------

class PolymarketWSClient:
    """Async WebSocket client maintaining a live CLOB order-book cache.

    Usage::

        client = PolymarketWSClient(url="wss://...market", backoff=(1,2,5))
        await client.connect(["12345...", "67890..."])
        snap = client.get_book_snapshot("12345...")
        bid, ask = client.best_bid_ask("12345...")
        await client.disconnect()

    The client auto-reconnects with exponential backoff if the server
    drops the connection. Subscriptions are re-issued on every (re)connect.
    """

    def __init__(
        self,
        url: str = PM_WS_URL,
        backoff: Tuple[int, ...] = RECONNECT_BACKOFF,
    ) -> None:
        self._url = url
        self._backoff = tuple(backoff)
        # Per-asset state
        self._books: Dict[str, _BookState] = {}
        # Subscribed token ids (ordered, deduplicated; list not set so
        # callers can rely on insertion order)
        self._subscribed: List[str] = []
        # Background task + ws handle
        self._task: Optional[asyncio.Task] = None
        self._ws: Optional[Any] = None
        # First-frame signals: asyncio.Event per token_id
        self._first_event: Dict[str, asyncio.Event] = {}
        # Backoff attempt counter (resets only on successful connect)
        self._attempt = 0
        # Bug fix 2026-06-27: 重命名 _ever_connected → _consecutive_failures,
        # 之前是"首次连上后永远给机会",导致抖动时日志刷屏无 give-up.
        # 改成任何时候连续失败 ≥ MAX_CONSECUTIVE_FAILURES 都退出 → REST fallback.
        self._consecutive_failures: int = 0
        # Lifecycle
        self._stop = False
        self._loop: Optional[asyncio.AbstractEventLoop] = None

    @classmethod
    def instance(cls) -> "PolymarketWSClient":
        """Compatibility shim for tests/callers that patch the singleton on the class."""
        return instance()

    # ----- public read API (synchronous, hot-path safe) -----

    @property
    def is_running(self) -> bool:
        return self._task is not None and not self._task.done()

    def best_bid_ask(self, token_id: str) -> Tuple[Optional[float], Optional[float]]:
        """Return ``(best_bid, best_ask)`` for ``token_id``.

        Both are ``None`` if the book is empty / unknown.
        """
        state = self._books.get(token_id)
        if state is None:
            return None, None
        return derive_best_quotes(state.bids, state.asks)

    def get_book_snapshot(self, token_id: str, depth: int = TOP_OF_BOOK_DEPTH) -> Optional[Dict[str, Any]]:
        """Return a snapshot dict compatible with
        ``PolymarketClient._bids_to_levels`` (string price/size).
        Returns ``None`` if no data yet.
        """
        state = self._books.get(token_id)
        if state is None or (not state.bids and not state.asks):
            return None
        return state.to_snapshot(depth=depth)

    # ----- message dispatch (synchronous - called by the read loop) -----

    def apply_message(self, msg: Any) -> bool:
        """Dispatch a single frame (already parsed or raw string/bytes).

        Returns True if the message mutated local state. Malformed
        frames return False without raising.
        """
        # Normalize to dict
        if msg is None:
            return False
        if isinstance(msg, (bytes, bytearray)):
            try:
                msg = msg.decode("utf-8", errors="replace")
            except Exception:  # noqa: BLE001
                return False
        if isinstance(msg, str):
            try:
                msg = json.loads(msg)
            except (ValueError, json.JSONDecodeError):
                return False
        if not isinstance(msg, dict):
            return False

        channel = _message_channel(msg)
        if channel in (None,):
            return False
        if channel == "subscribed":
            return False  # ack
        if channel == "book":
            return self._handle_book(msg)
        if channel == "price_change":
            return self._handle_price_change(msg)
        # Unknown channel - ignore silently (this is the live feed; new
        # channels may appear over time).
        return False

    def _handle_book(self, msg: Dict[str, Any]) -> bool:
        asset_id = _extract_asset_id(msg)
        if not asset_id:
            return False
        data = msg.get("data") or {}
        if not isinstance(data, dict):
            return False
        bids = _coerce_levels(data.get("bids"))
        asks = _coerce_levels(data.get("asks"))
        # If a hash is present the server is telling us to drop and
        # reseed; we do that implicitly in apply_snapshot.
        hash_ = data.get("hash") if isinstance(data.get("hash"), str) else None
        # Partial book (no bids and no asks) must NOT wipe an existing
        # state - the WS spec uses empty arrays to mean "no change" or
        # "drain", and we want a real REST reconcile for that case.
        if not bids and not asks:
            return False
        state = self._books.get(asset_id)
        if state is None:
            state = _BookState(asset_id)
            self._books[asset_id] = state
        state.apply_snapshot(bids, asks, hash_)
        # Signal first-event for wait_for_book()
        evt = self._first_event.get(asset_id)
        if evt is not None and not evt.is_set():
            evt.set()
        return True

    def _handle_price_change(self, msg: Dict[str, Any]) -> bool:
        data = msg.get("data") or {}
        items = data if isinstance(data, list) else [data]
        changed = False
        for item in items:
            if not isinstance(item, dict):
                continue
            asset_id = _extract_asset_id({"data": item})
            if not asset_id:
                continue
            try:
                price = float(item.get("price", 0))
                size = float(item.get("size", 0))
            except (TypeError, ValueError):
                continue
            side = item.get("side")
            if side not in _VALID_SIDES:
                # Unknown / malformed side - skip this row but keep going
                continue
            state = self._books.get(asset_id)
            if state is None:
                state = _BookState(asset_id)
                self._books[asset_id] = state
            if state.apply_price_change(price, size, side):
                changed = True
        if changed:
            for asset_id, evt in self._first_event.items():
                if self._books.get(asset_id) is not None and not evt.is_set():
                    evt.set()
        return changed

    # ----- async accessors -----

    async def wait_for_book(
        self,
        token_id: str,
        timeout: float = DEFAULT_BOOK_WAIT_TIMEOUT,
    ) -> Optional[Dict[str, Any]]:
        """Block until the first book event for ``token_id`` arrives.

        Returns the snapshot dict, or ``None`` if the timeout elapses
        without a frame.
        """
        # If we already have data, return immediately
        snap = self.get_book_snapshot(token_id)
        if snap is not None:
            return snap
        # Set up first-event signal
        loop = asyncio.get_event_loop()
        evt = self._first_event.get(token_id)
        if evt is None:
            evt = asyncio.Event()
            self._first_event[token_id] = evt
        try:
            await asyncio.wait_for(evt.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            return None
        return self.get_book_snapshot(token_id)

    # ----- lifecycle -----

    async def connect(self, token_ids: List[str]) -> None:
        """Open the WS connection (if not already) and subscribe.

        Idempotent: re-calling with overlapping ids is a no-op. If
        the connection drops, the background task reconnects and
        re-subscribes automatically.
        """
        loop = asyncio.get_event_loop()
        self._loop = loop
        # Add new ids to subscription list (preserving order, no dups)
        for tid in token_ids:
            if tid and tid not in self._subscribed:
                self._subscribed.append(tid)
            # Pre-create first-event signal so wait_for_book() works
            # even if a frame arrives before the caller awaits it.
            if tid and tid not in self._first_event:
                self._first_event[tid] = asyncio.Event()
            # Pre-register book state so best_bid_ask() always returns
            # ``(None, None)`` cleanly instead of KeyError-ing.
            if tid and tid not in self._books:
                self._books[tid] = _BookState(tid)

        if self.is_running:
            return  # background task already up; new ids will be sent
                     # on the next reconnect (acceptable; a hot add
                     # would require a runtime subscribe message).

        self._stop = False
        self._attempt = 0
        self._task = loop.create_task(self._run(), name="polymarket-ws")

    async def disconnect(self) -> None:
        """Stop the background task and close the WS connection."""
        self._stop = True
        ws = self._ws
        if ws is not None:
            try:
                await ws.close()
            except Exception:  # noqa: BLE001
                pass
        task = self._task
        if task is not None:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        self._task = None
        self._ws = None

    # ----- background loop -----

    async def _run(self) -> None:
        """Connect, read forever, reconnect on failure with backoff."""
        while not self._stop:
            try:
                async with websockets.connect(
                    self._url,
                    ping_interval=20,
                    ping_timeout=20,
                    max_queue=1024,
                ) as ws:
                    self._ws = ws
                    logger.info("Polymarket WS connected to %s", self._url)
                    # Reset backoff and consecutive failure counter on successful connect
                    self._attempt = 0
                    self._consecutive_failures = 0
                    # (Re)subscribe everything we know about
                    await self._send_subscribe(list(self._subscribed))
                    # Read loop
                    async for raw in ws:
                        if self._stop:
                            break
                        try:
                            self.apply_message(raw)
                        except Exception as exc:  # noqa: BLE001
                            logger.warning("WS message handler error: %s", exc)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                wait_s = _backoff_delay(self._attempt)
                self._attempt += 1
                self._consecutive_failures += 1
                logger.debug(
                    "Polymarket WS reconnect (attempt %d in %ds, consecutive=%d): %s",
                    self._attempt, wait_s, self._consecutive_failures, exc,
                )
                # Bug fix 2026-06-27: 两种 give-up 条件 —
                # 1) 从未连上 + 失败 ≥ MAX_RECONNECT_ATTEMPTS (8 次 ≈ 2.5 分钟)
                # 2) 任何时候连续失败 ≥ MAX_CONSECUTIVE_FAILURES (8 次 ≈ 4 分钟)
                # 之前只看条件 1, 一旦连上后永远不给机会, 持续刷日志
                total_failed = self._consecutive_failures
                if total_failed >= MAX_CONSECUTIVE_FAILURES:
                    logger.warning(
                        "Polymarket WS gave up after %d consecutive failures; "
                        "falling back to REST-only mode for this session",
                        total_failed,
                    )
                    self._task = None
                    return
                try:
                    await asyncio.sleep(wait_s)
                except asyncio.CancelledError:
                    raise
            finally:
                self._ws = None

    async def _send_subscribe(self, token_ids: List[str]) -> None:
        if not token_ids or self._ws is None:
            return
        msg = {"type": "subscribe", "channel": "book", "assets_id": token_ids}
        try:
            await self._ws.send(json.dumps(msg))
            msg2 = {"type": "subscribe", "channel": "price_change", "assets_id": token_ids}
            await self._ws.send(json.dumps(msg2))
        except ConnectionClosed:
            logger.debug("WS closed before subscribe ack could be sent")


# ---------------------------------------------------------------------------
# Singleton accessor
# ---------------------------------------------------------------------------

_instance: Optional[PolymarketWSClient] = None


def instance() -> PolymarketWSClient:
    """Return the process-wide singleton, creating it on first call."""
    global _instance
    if _instance is None:
        _instance = PolymarketWSClient()
    return _instance


def reset_instance() -> None:
    """Test helper: drop the singleton so the next ``instance()`` builds fresh."""
    global _instance
    _instance = None
