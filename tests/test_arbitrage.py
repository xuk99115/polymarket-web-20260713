"""套利阶梯 + 减仓单测。

覆盖：
- SPREAD 阶梯映射（0.18/0.12/0.08/0.05 各档）
- check_arbitrage 在边界（0.05 刚好 / 0.04 不触发）
- should_close_arb_pair 触发条件
- arb_pair_status 聚合正确性
"""
# pyright: reportOptionalSubscript=false
import os
import sys
import unittest
from typing import Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.trading._arbitrage import (
    check_arbitrage,
    should_close_arb_pair,
    arb_pair_status,
    _pick_tier,
    ARB_TIERS,
    ARB_CLOSE_SPREAD,
    ARB_CLOSE_MIN_PROFIT,
)


def make_market(ask0, ask1, gamma0=None, gamma1=None, label0="YES", label1="NO"):
    """构造最小可用 market dict。"""
    g0 = gamma0 if gamma0 is not None else ask0
    g1 = gamma1 if gamma1 is not None else ask1
    return {
        "outcomes": [
            {"index": 0, "label": label0, "price": g0, "best_bid": ask0 - 0.01, "best_ask": ask0},
            {"index": 1, "label": label1, "price": g1, "best_bid": ask1 - 0.01, "best_ask": ask1},
        ]
    }


class TestPickTier(unittest.TestCase):
    def test_strong_tier(self):
        """spread 0.20 应匹配强套利档（≥0.18）。"""
        t = _pick_tier(0.20)
        self.assertIsNotNone(t)
        self.assertEqual(t[3], "强套利")
        self.assertEqual(t[2], 20.00)  # max_stake

    def test_mid_tier(self):
        """spread 0.13 应匹配中套利档（≥0.12）。"""
        t = _pick_tier(0.13)
        self.assertIsNotNone(t)
        self.assertEqual(t[3], "中套利")
        self.assertEqual(t[1], 0.30)
        self.assertEqual(t[2], 12.00)

    def test_weak_tier(self):
        """spread 0.09 应匹配弱套利档（≥0.08）。"""
        t = _pick_tier(0.09)
        self.assertIsNotNone(t)
        self.assertEqual(t[3], "弱套利")
        self.assertEqual(t[2], 7.00)

    def test_micro_tier(self):
        """spread 0.05 应匹配微套利档（≥0.05）。"""
        t = _pick_tier(0.05)
        self.assertIsNotNone(t)
        self.assertEqual(t[3], "微套利")
        self.assertEqual(t[2], 4.00)

    def test_below_threshold(self):
        """spread 0.04 不应触发任何档。"""
        t = _pick_tier(0.04)
        self.assertIsNone(t)

    def test_float_boundary_close(self):
        """回归测试：浮点 0.51 - 0.48 = 0.03000000000000027，应触发减仓。"""
        m = make_market(ask0=0.51, ask1=0.48)
        # 确认确实是浮点边界
        self.assertGreater(0.51 - 0.48, 0.03)
        # 但减仓函数应该容忍这个误差
        sig = should_close_arb_pair(m)
        self.assertIsNotNone(sig)

    def test_float_boundary_tier(self):
        """回归测试：spread 恰好 0.05（浮点边界）应匹配微套利档。"""
        tier = _pick_tier(0.05)
        self.assertIsNotNone(tier)
        self.assertEqual(tier[3], "微套利")


class TestCheckArbitrage(unittest.TestCase):
    def test_strong_signal(self):
        """spread 0.22 + cost 0.98 → 锁 0.02/份，应返回完整 ARB signal。"""
        m = make_market(ask0=0.60, ask1=0.38)  # spread=0.22, cost=0.98
        sig = check_arbitrage(m)
        self.assertIsNotNone(sig)
        self.assertEqual(sig["action"], "ARBITRAGE")
        self.assertEqual(sig["tier_label"], "强套利")
        self.assertAlmostEqual(sig["total_cost"], 0.98, places=4)
        self.assertAlmostEqual(sig["locked_profit_per_unit"], 0.02, places=4)
        self.assertIn("pair_id", sig)

    def test_micro_signal(self):
        """spread 0.06 + cost 0.98 → 微套利档，锁 0.02/份。"""
        m = make_market(ask0=0.52, ask1=0.46)  # spread=0.06, cost=0.98
        sig = check_arbitrage(m)
        self.assertIsNotNone(sig)
        self.assertEqual(sig["tier_label"], "微套利")
        self.assertEqual(sig["max_stake_per_side"], 4.00)
        self.assertAlmostEqual(sig["locked_profit_per_unit"], 0.02, places=4)

    def test_no_signal_no_profit(self):
        """spread 大但 cost ≥ 0.99（无锁定利润）不应触发。"""
        m = make_market(ask0=0.70, ask1=0.30)  # spread=0.40 but cost=1.00
        self.assertIsNone(check_arbitrage(m))

    def test_ask_gap_filter(self):
        """ask 偏离 gamma 过远应被过滤。"""
        m = make_market(ask0=0.60, ask1=0.38, gamma0=0.40, gamma1=0.38)
        self.assertIsNone(check_arbitrage(m))

    def test_non_binary_market(self):
        """非二元盘不应触发。"""
        m = {"outcomes": [{"index": 0}, {"index": 1}, {"index": 2}]}
        self.assertIsNone(check_arbitrage(m))


class TestShouldCloseArbPair(unittest.TestCase):
    def test_converged_spread(self):
        """spread ≤ ARB_CLOSE_SPREAD 时应返回 CLOSE_ARB_SIDE。"""
        m = make_market(ask0=0.50, ask1=0.50)  # spread=0
        sig = should_close_arb_pair(m)
        self.assertIsNotNone(sig)
        self.assertEqual(sig["action"], "CLOSE_ARB_SIDE")
        # ask0 == ask1 → 走 else 分支（ask0 > ask1 为 False）→ outcome_index=1
        self.assertEqual(sig["outcome_index"], 1)
        self.assertAlmostEqual(sig["current_spread"], 0.0, places=4)

    def test_just_below_threshold(self):
        """spread = 0.03 应触发减仓（≤ ARB_CLOSE_SPREAD=0.03）。"""
        m = make_market(ask0=0.51, ask1=0.48)  # spread=0.03 exactly
        sig = should_close_arb_pair(m)
        self.assertIsNotNone(sig)
        # ask0 > ask1 → 卖 ask 高的那一边 (idx=0)
        self.assertEqual(sig["outcome_index"], 0)

    def test_no_close_when_spread_high(self):
        """spread > ARB_CLOSE_SPREAD 不应减仓。"""
        m = make_market(ask0=0.65, ask1=0.50)  # spread=0.15
        self.assertIsNone(should_close_arb_pair(m))

    def test_sells_higher_ask(self):
        """应卖 ask 更高（更便宜到期 redeem）的那一边。"""
        m = make_market(ask0=0.49, ask1=0.51)  # ask1 > ask0, spread=0.02 ≤ 0.03
        sig = should_close_arb_pair(m)
        self.assertIsNotNone(sig)
        self.assertEqual(sig["outcome_index"], 1)


class TestArbPairStatus(unittest.TestCase):
    def test_groups_by_pair_id(self):
        """按 pair_id 分组聚合。"""
        positions = [
            {"arbitrage_pair_id": "abc", "market_slug": "m1", "market_title": "M1",
             "outcome": "YES", "stake": 3.0, "shares": 10.0,
             "entry_price": 0.30, "current_value": 3.5},
            {"arbitrage_pair_id": "abc", "market_slug": "m1", "market_title": "M1",
             "outcome": "NO", "stake": 7.0, "shares": 10.0,
             "entry_price": 0.70, "current_value": 6.5},
            {"arbitrage_pair_id": "xyz", "market_slug": "m2", "market_title": "M2",
             "outcome": "YES", "stake": 3.0, "shares": 6.0,
             "entry_price": 0.5, "current_value": 3.1},
        ]
        pairs = arb_pair_status(positions)
        self.assertEqual(len(pairs), 2)

        abc_pair = next(p for p in pairs if p["pair_id"] == "abc")
        self.assertEqual(abc_pair["sides_count"], 2)
        self.assertEqual(abc_pair["total_stake"], 10.0)
        self.assertEqual(abc_pair["total_shares"], 20.0)
        self.assertAlmostEqual(abc_pair["current_value"], 10.0, places=4)
        # weighted entry = (0.30*10 + 0.70*10) / 20 = 0.50
        # locked_per_unit = 1 - 0.50 = 0.50
        # locked = 0.50 * 20 = 10.0
        self.assertAlmostEqual(abc_pair["locked_profit"], 10.0, places=4)
        # realized = current_value (10.0) - total_stake (10.0) = 0
        self.assertAlmostEqual(abc_pair["realized_pnl"], 0.0, places=4)

    def test_ignores_non_arb_positions(self):
        """没 pair_id 的持仓不参与聚合。"""
        positions = [
            {"market_slug": "m1", "outcome": "YES", "stake": 5.0,
             "shares": 10.0, "entry_price": 0.5, "current_value": 5.5},
        ]
        pairs = arb_pair_status(positions)
        self.assertEqual(len(pairs), 0)

    def test_locked_with_real_profit(self):
        """真实锁定利润计算。"""
        positions = [
            {"arbitrage_pair_id": "p1", "market_slug": "m1", "outcome": "YES",
             "stake": 5.9, "shares": 10.0, "entry_price": 0.59,
             "current_value": 6.0},
            {"arbitrage_pair_id": "p1", "market_slug": "m1", "outcome": "NO",
             "stake": 3.9, "shares": 10.0, "entry_price": 0.39,
             "current_value": 4.0},
        ]
        pairs = arb_pair_status(positions)
        self.assertEqual(len(pairs), 1)
        p = pairs[0]
        # total_shares=20, entry 加权平均 (0.59+0.39)/2=0.49, locked_per_unit = 1-0.49=0.51
        # locked = 0.51 * 20 = 10.2
        # 但 stake 总和 = 9.8，所以 realized = 10.0 - 9.8 = 0.2
        self.assertAlmostEqual(p["locked_profit"], 10.2, places=4)
        self.assertAlmostEqual(p["realized_pnl"], 0.2, places=4)


class TestTierConfiguration(unittest.TestCase):
    """档位配置 sanity check."""

    def test_tiers_sorted_descending(self):
        """档位应该按 min_spread 降序排列（强→微）。"""
        spreads = [t[0] for t in ARB_TIERS]
        self.assertEqual(spreads, sorted(spreads, reverse=True))

    def test_tiers_have_valid_stakes(self):
        """每档 cash_fraction 和 max_stake 应为正数。"""
        for t in ARB_TIERS:
            self.assertGreater(t[1], 0)  # cash_fraction
            self.assertGreater(t[2], 0)  # max_stake

    def test_close_threshold_lower_than_min_tier(self):
        """减仓阈值应小于最小档 spread，避免立刻减仓。"""
        self.assertLess(ARB_CLOSE_SPREAD, ARB_TIERS[-1][0])


if __name__ == "__main__":
    unittest.main()