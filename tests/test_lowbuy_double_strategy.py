from datetime import datetime, timezone

from src.trading.lowbuy_double import (
    LowBuyDoubleEngine,
)


def _market(up_ask, up_bid, down_ask, down_bid, end_dt):
    return {
        "slug": "btc-updown-15m-1234567890",
        "end_date": end_dt.isoformat(),
        "outcomes": [
            {"label": "Up", "best_ask": up_ask, "best_bid": up_bid},
            {"label": "Down", "best_ask": down_ask, "best_bid": down_bid},
        ],
    }


def test_core_entry_allows_without_fv():
    engine = LowBuyDoubleEngine()
    now = datetime(2026, 6, 30, 12, 0, tzinfo=timezone.utc)
    end = now.replace(minute=12)
    market = _market(0.33, 0.32, 0.58, 0.57, end)

    signals = engine.scan([market], now, fair_up=None, direction_hint="flat")

    assert any(sig["action"] == "BUY" and sig["outcome_index"] == 0 for sig in signals)


def test_rejects_too_cheap_core_entry():
    # 2026-07-07: 入场放宽到 30-36¢, 0.30 现在是允许的边界, 0.28 才应被拒
    engine = LowBuyDoubleEngine()
    now = datetime(2026, 6, 30, 12, 0, tzinfo=timezone.utc)
    end = now.replace(minute=12)
    market = _market(0.28, 0.27, 0.65, 0.64, end)

    signals = engine.scan([market], now, fair_up=None, direction_hint="flat")

    assert not any(sig["action"] == "BUY" and sig["outcome_index"] == 0 for sig in signals)


def test_allows_lower_boundary_entry():
    # 2026-07-07: 入场放宽到 30-36¢, 0.30 应允许入场
    engine = LowBuyDoubleEngine()
    now = datetime(2026, 6, 30, 12, 0, tzinfo=timezone.utc)
    end = now.replace(minute=12)
    market = _market(0.30, 0.29, 0.65, 0.64, end)

    signals = engine.scan([market], now, fair_up=None, direction_hint="flat")

    assert any(sig["action"] == "BUY" and sig["outcome_index"] == 0 for sig in signals)


def test_rejects_opposite_side_breakout():
    engine = LowBuyDoubleEngine()
    now = datetime(2026, 6, 30, 12, 0, tzinfo=timezone.utc)
    end = now.replace(minute=12)
    market = _market(0.70, 0.69, 0.33, 0.32, end)

    signals = engine.scan([market], now, fair_up=None, direction_hint="flat")

    assert not any(sig["action"] == "BUY" and sig["outcome_index"] == 1 for sig in signals)


def test_rejects_opposite_side_breakout_at_67c():
    engine = LowBuyDoubleEngine()
    now = datetime(2026, 6, 30, 12, 0, tzinfo=timezone.utc)
    end = now.replace(minute=12)
    market = _market(0.34, 0.33, 0.67, 0.66, end)

    signals = engine.scan([market], now, fair_up=None, direction_hint="flat")

    assert not any(sig["action"] == "BUY" and sig["outcome_index"] == 0 for sig in signals)


def test_rejects_recent_ask_drop():
    engine = LowBuyDoubleEngine()
    now = datetime(2026, 6, 30, 12, 0, tzinfo=timezone.utc)
    end = now.replace(minute=12)
    engine.scan([_market(0.51, 0.50, 0.50, 0.49, end)], now, fair_up=None, direction_hint="flat")

    later = now.replace(second=10)
    market = _market(0.66, 0.65, 0.33, 0.32, end)
    signals = engine.scan([market], later, fair_up=None, direction_hint="flat")

    assert not any(sig["action"] == "BUY" and sig["outcome_index"] == 1 for sig in signals)


def test_rejects_up_entry_when_flat_hint_but_structure_is_down():
    engine = LowBuyDoubleEngine()
    now = datetime(2026, 6, 30, 12, 0, tzinfo=timezone.utc)
    end = now.replace(minute=12)
    market = _market(0.34, 0.33, 0.67, 0.66, end)

    signals = engine.scan([market], now, fair_up=None, direction_hint="flat")

    assert not any(sig["action"] == "BUY" and sig["outcome_index"] == 0 for sig in signals)


def test_hard_stop_triggers_before_five_minute_stop():
    engine = LowBuyDoubleEngine()
    now = datetime(2026, 6, 30, 12, 0, tzinfo=timezone.utc)
    end = now.replace(minute=11)
    engine.register_entry("btc-updown-15m-1234567890", 1, 0.33, now, stake=2.0)
    market = _market(0.86, 0.85, 0.18, 0.17, end)

    signals = engine.scan([market], now.replace(minute=1), fair_up=None, direction_hint="flat")

    assert any(sig["action"] == "TIME_STOP" and sig.get("close_reason_code") == "hard_stop" for sig in signals)
