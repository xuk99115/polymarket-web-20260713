import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

from src.trading.executor import PaperExecutor
from src.trading.fv_edge import FVEdgeStrategy
from src.trading.manager import TradingBotManager, _resolve_settlement_price


class MemoryState:
    def __init__(self, state=None):
        self.state = state or {
            "cash_balance": 100.0,
            "positions": [],
            "orders": [],
            "trades": [],
            "stats": {},
            "summary": {},
            "report": {},
            "fv_signal_history": [],
        }

    def get_state(self):
        return self.state

    def save(self, force=False):
        return None


def market_at(now, *, up_ask=0.4, down_ask=0.6, prices=None):
    market = {
        "slug": f"btc-updown-15m-{int((now - timedelta(minutes=13)).timestamp())}",
        "question": "BTC Up or Down",
        "end_date": (now + timedelta(minutes=2)).isoformat(),
        "outcomes": [
            {"index": 0, "label": "Up", "token_id": "up", "best_bid": up_ask - 0.01, "best_ask": up_ask},
            {"index": 1, "label": "Down", "token_id": "down", "best_bid": down_ask - 0.01, "best_ask": down_ask},
        ],
    }
    if prices is not None:
        market["outcomePrices"] = prices
        market["closed"] = True
    return market


class TestSettlement(unittest.TestCase):
    def test_resolves_winner_and_loser(self):
        market = {"outcomePrices": [1.0, 0.0]}
        self.assertEqual(_resolve_settlement_price(market, {"index": 0}), 1.0)
        self.assertEqual(_resolve_settlement_price(market, {"index": 1}), 0.0)

    def test_waits_for_authoritative_prices(self):
        self.assertIsNone(_resolve_settlement_price({"outcomePrices": [0.7, 0.3]}, {"index": 0}))


class TestFvManager(unittest.IsolatedAsyncioTestCase):
    def make_manager(self, state=None):
        manager = TradingBotManager.__new__(TradingBotManager)
        manager.state_manager = MemoryState(state)
        manager.executor = PaperExecutor(manager.state_manager)
        manager.current_mode = "paper_live"
        manager.fv_edge = FVEdgeStrategy(position_usd=2.0)
        manager.market_api = MagicMock()
        manager._btc_window_refs = {}
        manager.POSITION_AUDIT_FILE = "/dev/null"
        return manager

    async def test_paper_open_is_tagged_fv_edge(self):
        now = datetime.now(timezone.utc)
        market = market_at(now)
        signal = {
            "action": "BUY",
            "outcome_index": 0,
            "outcome_label": "Up",
            "current_ask": 0.4,
            "stake": 2.0,
            "confidence": 0.8,
            "reason": "FV edge",
            "edge_bps": 500,
            "fair_up": 0.45,
            "mte_minutes": 2.0,
            "ref_px": 100000,
            "direction_mode": "shadow",
            "direction_gate": "UP",
            "direction_would_allow": True,
            "direction_evaluated_at": now.isoformat(),
        }
        manager = self.make_manager()

        result = await manager._open_signal(signal, market, now)

        self.assertIn("成功", result)
        state = manager.state_manager.get_state()
        self.assertEqual(state["positions"][0]["strategy"], "fv_edge")
        self.assertTrue(state["positions"][0]["hold_to_expiry"])
        self.assertEqual(state["positions"][0]["direction_gate"], "UP")
        self.assertEqual(state["trades"][0]["strategy"], "fv_edge")
        self.assertTrue(state["trades"][0]["direction_would_allow"])
        self.assertEqual(state["fv_signal_history"][0]["model"], "fv_edge")

    async def test_expired_paper_position_settles_at_zero(self):
        now = datetime.now(timezone.utc)
        market = market_at(now, prices=[1.0, 0.0])
        market["end_date"] = (now - timedelta(seconds=1)).isoformat()
        state = {
            "cash_balance": 98.0,
            "positions": [{
                "id": "p1", "market_slug": market["slug"], "outcome": "Down",
                "outcome_label": "Down", "outcome_index": 1, "token_id": "down",
                "stake": 2.0, "shares": 4.0, "entry_price": 0.5,
                "entry_trade_id": "t1", "status": "OPEN",
            }],
            "orders": [],
            "trades": [{
                "id": "t1", "market_slug": market["slug"], "outcome": "Down",
                "side": "BUY", "size": 4.0, "status": "OPEN",
            }],
            "stats": {}, "summary": {}, "report": {}, "fv_signal_history": [],
        }
        manager = self.make_manager(state)

        messages = await manager._manage_positions(now, [market])

        self.assertFalse(state["positions"])
        self.assertEqual(state["cash_balance"], 98.0)
        self.assertEqual(state["trades"][0]["realized_profit"], -2.0)
        self.assertTrue(any("EXPIRY_EXIT" in message for message in messages))

    async def test_settlement_backfills_direction_shadow_oracle_and_pnl(self):
        now = datetime.now(timezone.utc)
        market = market_at(now, prices=[1.0, 0.0])
        market["end_date"] = (now - timedelta(seconds=1)).isoformat()
        state = {
            "cash_balance": 98.0,
            "positions": [{
                "id": "p1", "market_slug": market["slug"], "outcome": "Up",
                "outcome_label": "Up", "outcome_index": 0, "token_id": "up",
                "stake": 2.0, "shares": 4.0, "entry_price": 0.5,
                "entry_trade_id": "t1", "status": "OPEN",
                "direction_mode": "shadow", "direction_gate": "DOWN",
                "direction_would_allow": False,
            }],
            "orders": [],
            "trades": [{
                "id": "t1", "market_slug": market["slug"], "outcome": "Up",
                "side": "BUY", "size": 4.0, "status": "OPEN",
                "direction_mode": "shadow", "direction_gate": "DOWN",
                "direction_would_allow": False,
            }],
            "stats": {}, "summary": {}, "report": {}, "fv_signal_history": [],
        }
        manager = self.make_manager(state)

        await manager._manage_positions(now, [market])

        buy = next(trade for trade in state["trades"] if trade["side"] == "BUY")
        sell = next(trade for trade in state["trades"] if trade["side"] == "SELL")
        self.assertEqual(buy["oracle_settlement_price"], 1.0)
        self.assertEqual(buy["direction_shadow_realized_pnl"], 2.0)
        self.assertEqual(buy["direction_enforce_realized_pnl"], 0.0)
        self.assertEqual(sell["direction_gate"], "DOWN")
        self.assertFalse(sell["direction_would_allow"])

    async def test_mode_swap_is_rejected_with_exposure(self):
        state = {
            "cash_balance": 98.0, "positions": [{"id": "p1"}], "orders": [],
            "trades": [], "stats": {}, "summary": {}, "report": {},
        }
        manager = self.make_manager(state)
        manager._requested_mode = MagicMock(return_value="live")
        manager._force_trading_mode = MagicMock()

        await manager.check_mode_swap()

        manager._force_trading_mode.assert_called_once_with("paper_live")
        self.assertEqual(manager.current_mode, "paper_live")

    def test_live_executor_failure_falls_back_to_paper(self):
        manager = self.make_manager()
        with patch("src.trading.manager.LiveExecutor", side_effect=ValueError("missing credentials")):
            executor = manager._create_executor("live")
        self.assertEqual(executor.mode, "paper_live")
