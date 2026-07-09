from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from src.trading.hedged_limit import HedgedLimitEngine


def _cfg_get(key, default=""):
    values = {
        "HEDGE_MARKET_PREFIX": "",
        "BTC_5M_SLUG_PREFIX": "btc-updown-5m-",
    }
    return values.get(key, default)


def _cfg_float(key, default="0"):
    values = {
        "HEDGE_INITIAL_LIMIT": 0.49,
        "HEDGE_MAX_TOTAL_COST": 0.97,
        "HEDGE_MAX_HEDGE_PRICE": 0.49,
        "HEDGE_STAKE_PER_LEG": 2.0,
        "HEDGE_TARGET_SHARES": 0.0,
        "HEDGE_ENTRY_FRACTION": 0.5,
        "HEDGE_FIRST_LEG_MIN_SECONDS_TO_END": 120.0,
        "HEDGE_CANCEL_PENDING_BEFORE_END_SECONDS": 60.0,
        "HEDGE_SINGLE_EXIT_BEFORE_END_SECONDS": 35.0,
        "HEDGE_ENTRY_TICK_SIZE": 0.01,
    }
    return values.get(key, float(default))


def _cfg_int(key, default="0"):
    values = {
        "HEDGE_WINDOW_SECONDS": 300,
        "BTC_5M_WINDOW_SECONDS": 300,
    }
    return values.get(key, int(default))


def _cfg_bool(key, default="false"):
    if key == "HEDGE_LIMIT_ENABLED":
        return True
    if key in {"HEDGE_LIMIT_ALLOW_LIVE", "HEDGE_ALLOW_NON_BTC"}:
        return False
    return str(default).lower() in {"true", "1", "yes", "on"}


def make_market(start, up_ask, down_ask, up_bid=None, down_bid=None):
    end = start + timedelta(minutes=5)
    ts = int(start.timestamp())
    return {
        "slug": f"btc-updown-5m-{ts}",
        "question": "BTC Up or Down - 5m",
        "end_date": end.isoformat(),
        "binary": True,
        "outcomes": [
            {
                "index": 0,
                "label": "Up",
                "token_id": "up-token",
                "price": up_ask,
                "best_bid": up_bid if up_bid is not None else max(0.01, up_ask - 0.01),
                "best_ask": up_ask,
            },
            {
                "index": 1,
                "label": "Down",
                "token_id": "down-token",
                "price": down_ask,
                "best_bid": down_bid if down_bid is not None else max(0.01, down_ask - 0.01),
                "best_ask": down_ask,
            },
        ],
    }


def make_state():
    return {
        "cash_balance": 100.0,
        "trades": [],
        "positions": [],
        "orders": [],
        "stats": {"total_trades": 0, "winning_trades": 0, "losing_trades": 0, "total_profit": 0.0},
    }


def run_with_config(fn):
    with patch("src.trading.hedged_limit.Config.get", side_effect=_cfg_get), \
         patch("src.trading.hedged_limit.Config.get_float", side_effect=_cfg_float), \
         patch("src.trading.hedged_limit.Config.get_int", side_effect=_cfg_int), \
         patch("src.trading.hedged_limit.Config.get_bool", side_effect=_cfg_bool):
        fn()


def test_double_fill_locks_and_settles_profit():
    def scenario():
        start = datetime(2026, 7, 3, 12, 0, tzinfo=timezone.utc)
        state = make_state()
        engine = HedgedLimitEngine()
        market = make_market(start, up_ask=0.47, down_ask=0.47, up_bid=0.46, down_bid=0.46)

        engine.apply([market], state, start + timedelta(seconds=10), mode="paper")
        pair = state["hedge_pairs"][0]
        assert pair["status"] == "LOCKED"
        assert pair["locked_profit"] == 0.2448
        assert len([t for t in state["trades"] if t["side"] == "BUY"]) == 2

        engine.apply([market], state, start + timedelta(minutes=5, seconds=1), mode="paper")
        assert pair["status"] == "SETTLED"
        assert state["cash_balance"] == 100.2448
        assert state["stats"]["winning_trades"] == 1
        assert state["stats"]["total_profit"] == 0.2448

    run_with_config(scenario)


def test_single_leg_can_complete_second_leg_at_cost_cap():
    def scenario():
        start = datetime(2026, 7, 3, 12, 0, tzinfo=timezone.utc)
        state = make_state()
        engine = HedgedLimitEngine()

        first = make_market(start, up_ask=0.47, down_ask=0.55, up_bid=0.46, down_bid=0.54)
        engine.apply([first], state, start + timedelta(seconds=10), mode="paper")
        pair = state["hedge_pairs"][0]
        assert pair["status"] == "LEG_OPEN"
        assert pair["entry_side_label"] == "Up"

        second = make_market(start, up_ask=0.60, down_ask=0.49)
        engine.apply([second], state, start + timedelta(seconds=40), mode="paper")
        assert pair["status"] == "LOCKED"
        assert pair["orders"]["1"]["avg_price"] == 0.49
        assert pair["locked_profit"] == 0.1632

    run_with_config(scenario)


def test_first_leg_uses_cheaper_best_ask_immediately():
    def scenario():
        start = datetime(2026, 7, 3, 12, 0, tzinfo=timezone.utc)
        state = make_state()
        engine = HedgedLimitEngine()

        market = make_market(start, up_ask=0.49, down_ask=0.51, up_bid=0.48, down_bid=0.50)
        engine.apply([market], state, start + timedelta(seconds=10), mode="paper")
        pair = state["hedge_pairs"][0]
        up_order = pair["orders"]["0"]
        down_order = pair["orders"]["1"]
        assert pair["entry_side_label"] == "Up"
        assert up_order["status"] == "FILLED"
        assert up_order["avg_price"] == 0.49
        assert down_order["status"] == "OPEN"
        assert down_order["limit_price"] == 0.48

    run_with_config(scenario)


def test_half_time_exits_single_leg_when_hedge_is_too_expensive():
    def scenario():
        start = datetime(2026, 7, 3, 12, 0, tzinfo=timezone.utc)
        state = make_state()
        engine = HedgedLimitEngine()

        first = make_market(start, up_ask=0.47, down_ask=0.55, up_bid=0.46, down_bid=0.54)
        engine.apply([first], state, start + timedelta(seconds=10), mode="paper")
        pair = state["hedge_pairs"][0]
        assert pair["status"] == "LEG_OPEN"

        still_expensive = make_market(start, up_ask=0.60, down_ask=0.55, up_bid=0.46, down_bid=0.54)
        engine.apply([still_expensive], state, start + timedelta(seconds=267), mode="paper")
        assert pair["status"] == "EXITED_SINGLE"
        assert pair["realized_profit"] == -0.0408
        assert state["cash_balance"] == 99.9592
        assert state["stats"]["losing_trades"] == 1

    run_with_config(scenario)


def test_threshold_price_fills_first_leg_immediately():
    def scenario():
        start = datetime(2026, 7, 3, 12, 0, tzinfo=timezone.utc)
        state = make_state()
        engine = HedgedLimitEngine()

        market = make_market(start, up_ask=0.49, down_ask=0.53, up_bid=0.47, down_bid=0.48)
        engine.apply([market], state, start + timedelta(seconds=10), mode="paper")
        pair = state["hedge_pairs"][0]
        assert pair["status"] == "LEG_OPEN"
        assert pair["entry_side_label"] == "Up"
        assert pair["orders"]["0"]["avg_price"] == 0.49

    run_with_config(scenario)


def test_strategy_can_start_after_half_if_enough_time_remains():
    def scenario():
        start = datetime(2026, 7, 3, 12, 0, tzinfo=timezone.utc)
        state = make_state()
        engine = HedgedLimitEngine()

        market = make_market(start, up_ask=0.48, down_ask=0.55, up_bid=0.47, down_bid=0.54)
        engine.apply([market], state, start + timedelta(seconds=170), mode="paper")
        pair = state["hedge_pairs"][0]
        assert pair["status"] == "LEG_OPEN"
        assert pair["entry_side_label"] == "Up"

    run_with_config(scenario)


def test_first_leg_too_expensive_skips_opening_pair():
    def scenario():
        start = datetime(2026, 7, 3, 12, 0, tzinfo=timezone.utc)
        state = make_state()
        engine = HedgedLimitEngine()

        market = make_market(start, up_ask=0.50, down_ask=0.51, up_bid=0.49, down_bid=0.50)
        engine.apply([market], state, start + timedelta(seconds=10), mode="paper")
        assert not state.get("hedge_pairs")

    run_with_config(scenario)
