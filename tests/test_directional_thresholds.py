import unittest
from datetime import datetime, timedelta, timezone

from src.trading.fv_edge import FVEdgeStrategy


def market(now, up_ask, down_ask):
    return {
        "slug": f"btc-updown-15m-{int((now - timedelta(minutes=13)).timestamp())}",
        "end_date": (now + timedelta(minutes=1)).isoformat(),
        "book_observed_at": now.isoformat().replace('+00:00', 'Z'),
        "outcomes": [
            {"index": 0, "label": "Up", "best_bid": max(0.01, up_ask - 0.01), "best_ask": up_ask, "quote_source": "clob"},
            {"index": 1, "label": "Down", "best_bid": max(0.01, down_ask - 0.01), "best_ask": down_ask, "quote_source": "clob"},
        ],
    }


class TestDirectionalThresholds(unittest.TestCase):
    def test_up_trade_requires_higher_up_threshold(self):
        now = datetime.now(timezone.utc)
        strat = FVEdgeStrategy(position_usd=2.0, threshold_bps=300, max_mte=1.5)
        strat._threshold_up_bps = 1200.0
        strat._threshold_down_bps = 800.0
        strat.update_btc_snapshot({
            "price": 101.0,
            "ref_px": 100.0,
            "sigma_15m": 0.001,
            "source": "chainlink_rtds",
        }, window_refs={f"btc-updown-15m-{int((now - timedelta(minutes=13)).timestamp())}": {"ref_px": 100.0, "source": "chainlink"}})
        sig = strat.scan([market(now, 0.90, 0.40)], now)
        self.assertEqual(sig, [])

    def test_down_trade_still_allowed_at_800_threshold(self):
        now = datetime.now(timezone.utc)
        strat = FVEdgeStrategy(position_usd=2.0, threshold_bps=300, max_mte=1.5)
        strat._threshold_up_bps = 1200.0
        strat._threshold_down_bps = 800.0
        strat.update_btc_snapshot({
            "price": 99.0,
            "ref_px": 100.0,
            "sigma_15m": 0.001,
            "source": "chainlink_rtds",
        }, window_refs={f"btc-updown-15m-{int((now - timedelta(minutes=13)).timestamp())}": {"ref_px": 100.0, "source": "chainlink"}})
        sig = strat.scan([market(now, 0.70, 0.75)], now)
        self.assertEqual(len(sig), 1)
        self.assertEqual(sig[0]["outcome_label"], "Down")


if __name__ == "__main__":
    unittest.main()
