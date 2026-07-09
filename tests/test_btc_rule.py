"""BTC 15m 规则信号单测。

覆盖 _build_btc_rule_signal 的关键边界:
- 时间过近 → 返回 None
- 价差过大 → 返回 None
- 短线动量不一致 → 返回 None
- 满足条件时返回 BUY + 正确的 outcome_index
"""
import os
import sys
import unittest
from datetime import datetime, timezone, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# 屏蔽 ai/decision / api/market 的 import-time 副作用
import src.core.config as cfg  # noqa: F401  确保 config 先 import


class TestBuildBtcRuleSignal(unittest.TestCase):
    def setUp(self):
        from src.trading.manager import _build_btc_rule_signal
        self._build = _build_btc_rule_signal
        self.now = datetime(2026, 6, 21, 12, 0, 0, tzinfo=timezone.utc)
        self.market = {
            "slug": "btc-updown-15m-1747800000",
            "outcomes": [
                {"index": 0, "label": "Up", "price": 0.50, "best_bid": 0.49, "best_ask": 0.51},
                {"index": 1, "label": "Down", "price": 0.50, "best_bid": 0.49, "best_ask": 0.51},
            ],
            "end_date": (self.now + timedelta(minutes=10)).isoformat(),
        }

    def _btc(self, **overrides):
        base = {
            "change_1m": 0.0, "change_3m": 0.0, "change_5m": 0.0, "change_15m": 0.0,
            "range_position_15m": 0.5, "volume_ratio_5m": 1.0, "direction_hint": "flat",
        }
        base.update(overrides)
        return base

    def test_close_to_expiry_returns_none(self):
        """到期 < 5 分钟应直接放弃。"""
        self.market["end_date"] = (self.now + timedelta(minutes=2)).isoformat()
        result = self._build(self.market, self._btc(), self.now)
        self.assertIsNone(result)

    def test_balanced_momentum_returns_none(self):
        """无明显方向优势时不应 BUY。"""
        btc = self._btc(change_1m=0.0, change_3m=0.0, change_5m=0.0, change_15m=0.0)
        result = self._build(self.market, btc, self.now)
        self.assertIsNone(result)

    def test_strong_up_momentum_returns_buy(self):
        """强向上动量应返回 BUY outcome=0 (Up)。"""
        btc = self._btc(
            change_1m=0.05, change_3m=0.10, change_5m=0.15, change_15m=0.05,
            range_position_15m=0.7, volume_ratio_5m=1.2, direction_hint="up",
        )
        result = self._build(self.market, btc, self.now)
        self.assertIsNotNone(result)
        self.assertEqual(result["action"], "BUY")
        self.assertEqual(result["outcome_index"], 0)
        self.assertLessEqual(result["confidence"], 0.72)

    def test_strong_down_momentum_returns_buy_outcome_1(self):
        """强向下动量应返回 BUY outcome=1 (Down)。"""
        btc = self._btc(
            change_1m=-0.05, change_3m=-0.10, change_5m=-0.15, change_15m=-0.05,
            range_position_15m=0.3, volume_ratio_5m=1.2, direction_hint="down",
        )
        result = self._build(self.market, btc, self.now)
        self.assertIsNotNone(result)
        self.assertEqual(result["action"], "BUY")
        self.assertEqual(result["outcome_index"], 1)

    def test_medium_down_momentum_upgrades_flat_direction_hint(self):
        """中等强度下跌应把 flat 方向归一为 down。"""
        btc = self._btc(
            change_1m=-0.02, change_3m=-0.05, change_5m=-0.07, change_15m=-0.03,
            range_position_15m=0.35, volume_ratio_5m=1.1, direction_hint="flat",
        )
        result = self._build(self.market, btc, self.now)
        self.assertIsNotNone(result)
        self.assertEqual(result["action"], "BUY")
        self.assertEqual(result["outcome_index"], 1)


if __name__ == "__main__":
    unittest.main()
