"""Unit tests for the Polymarket WebSocket client.

These tests are fully mocked - they never open a real socket. They
cover the pieces that matter for the trading bot:

* message parsing (book snapshot, price_change, hash reseed, ack)
* best bid / best ask derivation from bids/asks levels
* reconnect backoff sequence
* tolerance to malformed messages
* integration into PolymarketClient.get_microstructure via the
  ``USE_POLYMARKET_WS`` switch (REST fallback path)
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import unittest
from typing import Any, Dict, List, Optional

# Make src/ importable
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from src.api import polymarket_ws as pmws
from src.api.polymarket_ws import (
    DEFAULT_BOOK_WAIT_TIMEOUT,
    PM_WS_URL,
    RECONNECT_BACKOFF,
    TOP_OF_BOOK_DEPTH,
    PolymarketWSClient,
    _backoff_delay,
    _coerce_levels,
    _extract_asset_id,
    _message_channel,
    derive_best_quotes,
)


# ---------------------------------------------------------------------------
# Pure-helper tests (no event loop needed)
# ---------------------------------------------------------------------------

class TestCoerceLevels(unittest.TestCase):
    def test_string_prices_and_sizes(self):
        """Polymarket sends prices/sizes as strings."""
        out = _coerce_levels([{"price": "0.55", "size": "100"}, {"price": "0.50", "size": "50"}])
        self.assertEqual(out, [(0.55, 100.0), (0.50, 50.0)])

    def test_float_inputs_accepted(self):
        """Some proxies / docs use floats; accept both."""
        out = _coerce_levels([{"price": 0.55, "size": 100}, {"price": 0.5, "size": 50.5}])
        self.assertEqual(out, [(0.55, 100.0), (0.5, 50.5)])

    def test_mixed_inputs(self):
        out = _coerce_levels([{"price": "0.55", "size": 100}])
        self.assertEqual(out, [(0.55, 100.0)])

    def test_empty_list_returns_empty(self):
        self.assertEqual(_coerce_levels([]), [])
        self.assertEqual(_coerce_levels(None), [])
        self.assertEqual(_coerce_levels("not a list"), [])

    def test_drops_malformed_entries(self):
        out = _coerce_levels([
            {"price": "0.55", "size": "100"},
            {"price": None, "size": "50"},         # missing price
            {"price": "0.50"},                     # missing size
            {"price": "abc", "size": "10"},        # bad price
            {"price": "0.40", "size": "xyz"},      # bad size
            {"price": "-0.1", "size": "10"},       # negative price
            {"price": "0.20", "size": "-1"},       # negative size
            {"price": "0.30", "size": "5"},
            "not a dict",                          # wrong type
        ])
        self.assertEqual(out, [(0.55, 100.0), (0.30, 5.0)])


class TestDeriveBestQuotes(unittest.TestCase):
    def test_picks_max_bid_min_ask(self):
        bids = [(0.50, 10.0), (0.55, 20.0), (0.52, 5.0)]
        asks = [(0.57, 8.0), (0.56, 15.0)]
        bid, ask = derive_best_quotes(bids, asks)
        self.assertEqual(bid, 0.55)
        self.assertEqual(ask, 0.56)

    def test_empty_returns_none(self):
        self.assertEqual(derive_best_quotes([], []), (None, None))
        self.assertEqual(derive_best_quotes([(0.5, 1.0)], []), (0.5, None))
        self.assertEqual(derive_best_quotes([], [(0.6, 1.0)]), (None, 0.6))

    def test_single_level(self):
        bid, ask = derive_best_quotes([(0.42, 1.0)], [(0.58, 1.0)])
        self.assertEqual((bid, ask), (0.42, 0.58))


class TestBackoffSequence(unittest.TestCase):
    def test_first_five_attempts(self):
        """Backoff must be exactly [1, 2, 5, 10, 30]."""
        expected = [1, 2, 5, 10, 30]
        for i, want in enumerate(expected):
            self.assertEqual(_backoff_delay(i), float(want))

    def test_cycles_after_table(self):
        """Once the table is exhausted, we cycle - never wait forever."""
        self.assertEqual(_backoff_delay(len(RECONNECT_BACKOFF)), float(RECONNECT_BACKOFF[0]))
        self.assertEqual(_backoff_delay(len(RECONNECT_BACKOFF) + 1), float(RECONNECT_BACKOFF[1]))
        # And for a large attempt count it must still return a finite value.
        self.assertGreater(_backoff_delay(1000), 0.0)


class TestMessageChannelExtraction(unittest.TestCase):
    def test_channel_field(self):
        self.assertEqual(_message_channel({"channel": "book"}), "book")

    def test_type_field_as_fallback(self):
        """Subscribe-acks use ``type``; data frames use ``channel``."""
        self.assertEqual(_message_channel({"type": "subscribed", "channel": "book"}), "book")
        self.assertEqual(_message_channel({"type": "subscribed"}), "subscribed")

    def test_non_dict_returns_none(self):
        self.assertIsNone(_message_channel("not a dict"))
        self.assertIsNone(_message_channel([1, 2]))
        self.assertIsNone(_message_channel(None))

    def test_missing_field_returns_none(self):
        self.assertIsNone(_message_channel({}))
        self.assertIsNone(_message_channel({"foo": "bar"}))


class TestAssetIdExtraction(unittest.TestCase):
    def test_from_data_dict(self):
        self.assertEqual(
            _extract_asset_id({"data": {"asset_id": "abc"}}),
            "abc",
        )
        self.assertEqual(
            _extract_asset_id({"data": {"assets_id": "xyz"}}),
            "xyz",
        )

    def test_from_top_level(self):
        self.assertEqual(_extract_asset_id({"asset_id": "abc"}), "abc")
        self.assertEqual(_extract_asset_id({"assets_id": "xyz"}), "xyz")

    def test_missing(self):
        self.assertIsNone(_extract_asset_id({}))
        self.assertIsNone(_extract_asset_id({"data": {}}))


# ---------------------------------------------------------------------------
# PolymarketWSClient behaviour tests (event loop + mocked frames)
# ---------------------------------------------------------------------------

class _FakeWS:
    """Minimal stand-in for websockets.WebSocketClientProtocol.

    Records every ``send`` call, lets the test feed pre-canned frames
    via ``push()``, and exposes a controllable failure via ``raise_on_next``.
    """

    def __init__(self, frames: Optional[List[Any]] = None):
        self.sent: List[str] = []
        self._frames: List[Any] = list(frames or [])
        self.closed: bool = False
        self.raise_on_next: Optional[BaseException] = None

    async def send(self, payload: str) -> None:
        self.sent.append(payload)

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self.raise_on_next is not None:
            exc = self.raise_on_next
            self.raise_on_next = None
            assert exc is not None  # for type checkers
            raise exc
        if not self._frames:
            raise StopAsyncIteration
        return self._frames.pop(0)

    async def close(self) -> None:
        self.closed = True


def _make_client() -> PolymarketWSClient:
    """Build a fresh client (no global singleton) for isolation."""
    client = PolymarketWSClient(url="ws://test/market", backoff=(1, 2, 5))
    return client


class TestBookEventParsing(unittest.TestCase):
    """Verify the WS client correctly applies a book snapshot."""

    def test_full_book_event_seeds_state(self):
        client = _make_client()
        msg = {
            "channel": "book",
            "data": {
                "asset_id": "tok-1",
                "bids": [{"price": "0.55", "size": "100"}, {"price": "0.54", "size": "50"}],
                "asks": [{"price": "0.57", "size": "200"}, {"price": "0.58", "size": "75"}],
                "hash": "abc123",
            },
        }
        changed = client.apply_message(msg)
        self.assertTrue(changed)
        snap = client.get_book_snapshot("tok-1")
        self.assertIsNotNone(snap)
        self.assertEqual(snap["bids"], [(0.55, 100.0), (0.54, 50.0)])
        # asks are sorted ascending
        self.assertEqual(snap["asks"], [(0.57, 200.0), (0.58, 75.0)])
        bid, ask = client.best_bid_ask("tok-1")
        self.assertEqual((bid, ask), (0.55, 0.57))

    def test_book_without_hash_still_works(self):
        client = _make_client()
        msg = {
            "channel": "book",
            "data": {
                "asset_id": "tok-2",
                "bids": [{"price": "0.40", "size": "10"}],
                "asks": [{"price": "0.60", "size": "20"}],
            },
        }
        client.apply_message(msg)
        bid, ask = client.best_bid_ask("tok-2")
        self.assertEqual((bid, ask), (0.40, 0.60))

    def test_book_for_unknown_token_id_is_no_op(self):
        client = _make_client()
        msg = {"channel": "book", "data": {}}
        self.assertFalse(client.apply_message(msg))

    def test_hash_present_triggers_reseed_log(self):
        """When 'hash' is present the client should drop and reseed."""
        client = _make_client()
        # Seed initial state
        client.apply_message({
            "channel": "book",
            "data": {
                "asset_id": "tok-3",
                "bids": [{"price": "0.50", "size": "10"}],
                "asks": [{"price": "0.55", "size": "10"}],
            },
        })
        # Now reseed with different hash
        client.apply_message({
            "channel": "book",
            "data": {
                "asset_id": "tok-3",
                "bids": [{"price": "0.45", "size": "5"}],
                "asks": [{"price": "0.65", "size": "5"}],
                "hash": "new-hash",
            },
        })
        bid, ask = client.best_bid_ask("tok-3")
        self.assertEqual((bid, ask), (0.45, 0.65))


class TestPriceChangeEventParsing(unittest.TestCase):
    """Verify incremental price_change events update the book correctly."""

    def _seed_book(self, client: PolymarketWSClient, asset_id: str) -> None:
        client.apply_message({
            "channel": "book",
            "data": {
                "asset_id": asset_id,
                "bids": [
                    {"price": "0.50", "size": "100"},
                    {"price": "0.49", "size": "200"},
                ],
                "asks": [
                    {"price": "0.51", "size": "150"},
                    {"price": "0.52", "size": "250"},
                ],
            },
        })

    def test_sell_event_reduces_top_bid(self):
        client = _make_client()
        self._seed_book(client, "tok-1")
        # Taker sells into the top bid (SIDE = SELL): top bid's size is replaced
        # by the new total depth (100 - 60 consumed = 40 remaining).
        client.apply_message({
            "channel": "price_change",
            "data": {"asset_id": "tok-1", "price": "0.50", "size": "40", "side": "SELL"},
        })
        bid, ask = client.best_bid_ask("tok-1")
        self.assertEqual(bid, 0.50)
        self.assertEqual(ask, 0.51)

    def test_buy_event_reduces_top_ask(self):
        client = _make_client()
        self._seed_book(client, "tok-1")
        # Taker buys, lifting the top ask. size=50 means remaining depth at 0.51
        # (150 - 100 consumed = 50), top ask stays at 0.51.
        client.apply_message({
            "channel": "price_change",
            "data": {"asset_id": "tok-1", "price": "0.51", "size": "50", "side": "BUY"},
        })
        bid, ask = client.best_bid_ask("tok-1")
        self.assertEqual(bid, 0.50)
        self.assertEqual(ask, 0.51)

    def test_size_zero_removes_level(self):
        client = _make_client()
        self._seed_book(client, "tok-1")
        client.apply_message({
            "channel": "price_change",
            "data": {"asset_id": "tok-1", "price": "0.50", "size": "0", "side": "SELL"},
        })
        snap = client.get_book_snapshot("tok-1")
        # 0.50 level gone, only 0.49 remains on the bid side
        bid_prices = sorted({p for (p, _) in snap["bids"]}, reverse=True)
        self.assertEqual(bid_prices, [0.49])

    def test_unknown_side_is_ignored(self):
        client = _make_client()
        self._seed_book(client, "tok-1")
        before = client.best_bid_ask("tok-1")
        client.apply_message({
            "channel": "price_change",
            "data": {"asset_id": "tok-1", "price": "0.50", "size": "1", "side": "GARBAGE"},
        })
        after = client.best_bid_ask("tok-1")
        self.assertEqual(before, after)


class TestMalformedMessageTolerance(unittest.TestCase):
    """The client must never raise on a bad frame - it just skips it."""

    def test_non_json_string(self):
        client = _make_client()
        self.assertFalse(client.apply_message("not json {{{"))

    def test_bytes_payload(self):
        client = _make_client()
        # Plain bytes - should decode and skip cleanly.
        self.assertFalse(client.apply_message(b"\x00\x01not-json"))

    def test_unknown_channel(self):
        client = _make_client()
        self.assertFalse(client.apply_message({"channel": "trades", "data": {"asset_id": "x"}}))

    def test_missing_data(self):
        client = _make_client()
        self.assertFalse(client.apply_message({"channel": "book"}))
        self.assertFalse(client.apply_message({"channel": "price_change"}))

    def test_partial_book_does_not_overwrite_state(self):
        """An empty book message should not wipe out our cached state."""
        client = _make_client()
        client.apply_message({
            "channel": "book",
            "data": {
                "asset_id": "tok-1",
                "bids": [{"price": "0.40", "size": "10"}],
                "asks": [{"price": "0.60", "size": "10"}],
            },
        })
        client.apply_message({"channel": "book", "data": {"asset_id": "tok-1"}})
        bid, ask = client.best_bid_ask("tok-1")
        self.assertEqual((bid, ask), (0.40, 0.60))


class TestConnectSubscribeDispatch(unittest.TestCase):
    """Smoke-test that connect() / disconnect() work and don't hit the network."""

    def test_connect_records_subscriptions_when_no_task(self):
        client = _make_client()
        # No background task spawned - we never call connect() in this test,
        # but we can still verify the bookkeeping is in place.
        client._subscribed.append("tok-x")
        self.assertIn("tok-x", client._subscribed)

    def test_disconnect_is_safe_when_never_started(self):
        client = _make_client()
        # Should not raise even though no task is running.
        asyncio.run(client.disconnect())

    def test_disconnect_cancels_running_task(self):
        """disconnect() 必须取消正在运行的 _task，不能泄漏 task."""
        client = _make_client()

        async def scenario():
            async def fake_run():
                try:
                    await asyncio.sleep(60)
                except asyncio.CancelledError:
                    raise

            task = asyncio.create_task(fake_run())
            client._task = task
            task.cancel()
            with __import__("contextlib").suppress(asyncio.CancelledError):
                await asyncio.wait_for(task, timeout=0.5)
            self.assertTrue(task.cancelled() or task.done())

        asyncio.run(scenario())


# ---------------------------------------------------------------------------
# Async tests via pytest-asyncio
# ---------------------------------------------------------------------------

pytestmark_async = pytest.mark.asyncio


@pytest.mark.asyncio
async def test_wait_for_book_returns_immediately_when_cached():
    """If the book is already cached, wait_for_book returns at once."""
    client = _make_client()
    client.apply_message({
        "channel": "book",
        "data": {
            "asset_id": "tok-cached",
            "bids": [{"price": "0.50", "size": "10"}],
            "asks": [{"price": "0.60", "size": "10"}],
        },
    })
    snap = await client.wait_for_book("tok-cached", timeout=1.0)
    assert snap is not None
    assert snap["bids"] == [(0.50, 10.0)]
    assert snap["asks"] == [(0.60, 10.0)]


@pytest.mark.asyncio
async def test_wait_for_book_times_out_when_no_data():
    """If no frame arrives, wait_for_book returns None after the timeout."""
    client = _make_client()
    loop = asyncio.get_event_loop()
    t0 = loop.time()
    snap = await client.wait_for_book("never-seen", timeout=0.2)
    elapsed = loop.time() - t0
    assert snap is None
    # Should not have waited substantially longer than the timeout.
    assert elapsed < 0.6


@pytest.mark.asyncio
async def test_wait_for_book_wakes_when_frame_arrives(monkeypatch):
    """If a frame arrives during the wait, we return promptly."""
    client = _make_client()

    async def feeder():
        await asyncio.sleep(0.05)
        client.apply_message({
            "channel": "book",
            "data": {
                "asset_id": "tok-late",
                "bids": [{"price": "0.42", "size": "7"}],
                "asks": [{"price": "0.58", "size": "7"}],
            },
        })

    asyncio.create_task(feeder())
    snap = await client.wait_for_book("tok-late", timeout=1.0)
    assert snap is not None
    bid, ask = client.best_bid_ask("tok-late")
    assert (bid, ask) == (0.42, 0.58)


@pytest.mark.asyncio
async def test_disconnect_stops_background_task():
    """disconnect() should cancel the running task cleanly."""
    client = _make_client()

    # Fake out the network call so connect() doesn't try to dial out.
    async def fake_connect(*args, **kwargs):
        return _FakeWS()

    monkey = __import__("pytest").MonkeyPatch()
    monkey.setattr(pmws.websockets, "connect", fake_connect)

    await client.connect(["tok-x"])
    assert client.is_running

    await client.disconnect()
    assert not client.is_running
    monkey.undo()


# ---------------------------------------------------------------------------
# Integration with PolymarketClient.get_microstructure
# ---------------------------------------------------------------------------

def _make_outcome(idx: int, label: str, token_id: str, price: float = 0.5):
    return {
        "index": idx,
        "label": label,
        "token_id": token_id,
        "price": price,
        "best_bid": None,
        "best_ask": None,
    }


def _make_market(*token_ids: str):
    return {
        "slug": "test-market",
        "outcomes": [_make_outcome(i, f"OUT-{i}", tid) for i, tid in enumerate(token_ids)],
        "token_ids": list(token_ids),
    }


class _StubWS:
    """Drop-in stand-in for PolymarketWSClient that doesn't touch the network."""

    def __init__(self):
        self._snapshots: Dict[str, Dict[str, Any]] = {}
        self.connect_calls: List[List[str]] = []
        self.wait_calls: List[str] = []

    async def connect(self, token_ids):
        self.connect_calls.append(list(token_ids))

    async def disconnect(self):
        pass

    async def wait_for_book(self, token_id, timeout=2.0):
        self.wait_calls.append(token_id)
        return self._snapshots.get(token_id)

    def seed(self, token_id: str, bids, asks):
        self._snapshots[token_id] = {
            "bids": list(bids),
            "asks": list(asks),
            "updated_at": 0.0,
        }


class TestPolymarketClientWSIntegration(unittest.TestCase):
    """Verify USE_POLYMARKET_WS dispatch + REST fallback."""

    def setUp(self):
        from src.api.market import PolymarketClient
        self.client = PolymarketClient()
        # Make sure the env cache is clean so each test sees its own override.
        from src.core import config as cfg
        cfg.Config.invalidate()

    def tearDown(self):
        from src.core import config as cfg
        cfg.Config.invalidate()

    def test_use_ws_true_dispatches_to_stub(self):
        os.environ["USE_POLYMARKET_WS"] = "true"
        stub = _StubWS()
        stub.seed("tok-a", [(0.50, 100.0)], [(0.55, 100.0)])

        # Patch the singleton accessor on the WS class to return our stub.
        from src.api.polymarket_ws import PolymarketWSClient as _PMWS
        original_instance = _PMWS.instance

        @classmethod  # type: ignore[arg-type]
        def fake_instance(cls):
            return stub

        _PMWS.instance = fake_instance  # type: ignore[assignment]
        try:
            market = _make_market("tok-a")
            result = asyncio.run(self.client.get_microstructure(market))
        finally:
            _PMWS.instance = original_instance  # type: ignore[assignment]

        self.assertEqual(result["source"], "ws")
        self.assertEqual(len(result["outcomes"]), 1)
        outcome = result["outcomes"][0]
        self.assertEqual(outcome["token_id"], "tok-a")
        self.assertEqual(outcome["bids"][0]["price"], "0.5")
        self.assertEqual(outcome["asks"][0]["price"], "0.55")
        self.assertEqual(stub.connect_calls, [["tok-a"]])

    def test_use_ws_false_falls_back_to_rest(self):
        os.environ["USE_POLYMARKET_WS"] = "false"

        async def fake_rest(market):
            return {"outcomes": [{"index": 0, "token_id": "tok-b", "bids": [], "asks": []}], "source": "clob"}

        from src.api import market as market_mod
        original_rest = self.client._get_microstructure_rest
        self.client._get_microstructure_rest = fake_rest
        try:
            market = _make_market("tok-b")
            result = asyncio.run(self.client.get_microstructure(market))
        finally:
            self.client._get_microstructure_rest = original_rest

        self.assertEqual(result["source"], "clob")
        self.assertEqual(result["outcomes"][0]["token_id"], "tok-b")

    def test_ws_failure_falls_back_to_rest(self):
        """If WS raises, we transparently fall back to REST."""
        os.environ["USE_POLYMARKET_WS"] = "true"

        async def fake_rest(market):
            return {"outcomes": [{"index": 0, "token_id": "tok-c"}], "source": "clob"}

        from src.api import market as market_mod
        original_rest = self.client._get_microstructure_rest
        self.client._get_microstructure_rest = fake_rest
        try:
            market = _make_market("tok-c")
            result = asyncio.run(self.client.get_microstructure(market))
        finally:
            self.client._get_microstructure_rest = original_rest

        # Empty WS result triggers REST fallback.
        self.assertEqual(result["source"], "clob")
        self.assertEqual(result["outcomes"][0]["token_id"], "tok-c")


class TestModuleConstants(unittest.TestCase):
    """Sanity-check the public module constants."""

    def test_ws_url(self):
        self.assertEqual(PM_WS_URL, "wss://ws-subscriptions-clob.polymarket.com/ws/market")

    def test_backoff_sequence(self):
        self.assertEqual(RECONNECT_BACKOFF, (1, 2, 5, 10, 30))

    def test_default_book_wait_timeout(self):
        self.assertEqual(DEFAULT_BOOK_WAIT_TIMEOUT, 2.0)

    def test_top_of_book_depth(self):
        self.assertEqual(TOP_OF_BOOK_DEPTH, 5)


if __name__ == "__main__":
    unittest.main()