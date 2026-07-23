from datetime import datetime, timedelta, timezone

from src.trading.fv_edge import FVEdgeStrategy
from src.trading.manager import TradingBotManager


def make_market(now, *, up_ask=0.55, down_ask=0.45):
    start = int((now - timedelta(minutes=13)).timestamp())
    return {
        "slug": f"btc-updown-15m-{start}",
        "end_date": (now + timedelta(minutes=1)).isoformat(),
        "outcomes": [
            {"index": 0, "label": "Up", "best_bid": up_ask - 0.01, "best_ask": up_ask, "quote_source": "clob"},
            {"index": 1, "label": "Down", "best_bid": down_ask - 0.01, "best_ask": down_ask, "quote_source": "clob"},
        ],
    }


def update_strategy(strategy, market, *, price=100000, sigma=0.003, late_ref=False):
    strategy.update_btc_snapshot(
        {
            "price": price,
            "sigma_15m": sigma,
            "source": "chainlink_rtds",
            "captured_at": datetime.now(timezone.utc).isoformat(),
        },
        {market["slug"]: {"ref_px": 100000, "source": "chainlink", "late_ref": late_ref}},
    )


def test_missing_sigma_produces_no_signal():
    now = datetime.now(timezone.utc)
    market = make_market(now)
    strategy = FVEdgeStrategy(threshold_bps=0)
    strategy.update_btc_snapshot({"price": 100000}, {market["slug"]: {"ref_px": 100000}})
    assert strategy.scan([market], now) == []


def test_down_edge_uses_fair_down_minus_down_ask():
    now = datetime.now(timezone.utc)
    market = make_market(now, up_ask=0.8, down_ask=0.2)
    strategy = FVEdgeStrategy(threshold_bps=100)
    update_strategy(strategy, market, price=99900)

    signal = strategy.scan([market], now)[0]

    assert signal["outcome_index"] == 1
    expected = (1.0 - signal["fair_up"] - 0.2) * 10000
    assert abs(signal["edge_down_bps"] - expected) < 1.0
    assert signal["edge_bps"] == signal["edge_down_bps"]


def test_non_favorite_side_is_rejected_even_with_positive_edge():
    now = datetime.now(timezone.utc)
    market = make_market(now, up_ask=0.9, down_ask=0.1)
    strategy = FVEdgeStrategy(threshold_bps=500)
    update_strategy(strategy, market, price=100100)

    assert strategy.scan([market], now) == []


def test_favorite_side_with_positive_edge_is_accepted():
    now = datetime.now(timezone.utc)
    market = make_market(now, up_ask=0.7, down_ask=0.4)
    strategy = FVEdgeStrategy(threshold_bps=500)
    update_strategy(strategy, market, price=100100)

    signal = strategy.scan([market], now)[0]
    assert signal["outcome_label"] == "Up"
    assert signal["fair_selected"] > 0.5


def test_binance_current_price_is_rejected_in_chainlink_mode():
    now = datetime.now(timezone.utc)
    market = make_market(now, up_ask=0.7, down_ask=0.4)
    strategy = FVEdgeStrategy(threshold_bps=500)
    strategy.update_btc_snapshot(
        {"price": 100100, "sigma_15m": 0.003, "source": "binance"},
        {market["slug"]: {"ref_px": 100000, "source": "chainlink"}},
    )

    assert strategy.scan([market], now) == []


def test_late_window_reference_is_rejected():
    now = datetime.now(timezone.utc)
    market = make_market(now, up_ask=0.2, down_ask=0.8)
    strategy = FVEdgeStrategy(threshold_bps=0)
    update_strategy(strategy, market, price=100100, late_ref=True)
    assert strategy.scan([market], now) == []


def test_price_filter_is_public_and_inclusive():
    strategy = FVEdgeStrategy(min_price=0.1, max_price=0.85)
    assert strategy.accepts_price(0.1)
    assert strategy.accepts_price(0.85)
    assert not strategy.accepts_price(0.09)


def test_book_observation_captures_depth_and_stake_vwap():
    observed = TradingBotManager._book_observation(
        {
            "best_bid": 0.40,
            "best_ask": 0.45,
            "book_observed_at": "2026-07-23T00:00:00+00:00",
            "depth_bids": [{"price": 0.40, "size": 10}],
            "depth_asks": [
                {"price": 0.45, "size": 2},
                {"price": 0.46, "size": 10},
            ],
        },
        target_stake=1.0,
    )
    assert observed["best_ask"] == 0.45
    assert observed["depth_asks"] == [
        {"price": 0.45, "size": 2},
        {"price": 0.46, "size": 10},
    ]
    assert abs(observed["executable_vwap_for_stake"] - 0.45098039) < 1e-8
