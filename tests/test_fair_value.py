"""Unit tests for src.api.fair_value.

Covers:
- standard_normal_cdf: known values (0, 1.96, -1.96)
- compute_fair_updown:
    * price far above ref → fair_up > 0.7
    * sigma below MIN_SIGMA → 50/50 fallback
    * tau below MIN_TAU_SEC → 50/50 fallback
    * edge_bps_vs_market correctness
    * z_score sign matches ln(s/ref)/sigma direction
    * output dict has expected keys
"""
import math
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.api.fair_value import (
    compute_fair_updown,
    compute_edge_bps,
    standard_normal_cdf,
    MIN_SIGMA,
    MIN_TAU_SEC,
    DEFAULT_WINDOW_SEC,
)


class TestStandardNormalCdf(unittest.TestCase):
    def test_at_zero_is_half(self):
        self.assertAlmostEqual(standard_normal_cdf(0.0), 0.5, places=8)

    def test_at_one_point_nine_six(self):
        # Classic 95% CI bound
        self.assertAlmostEqual(standard_normal_cdf(1.96), 0.975, places=3)

    def test_at_negative_one_point_nine_six(self):
        # Symmetry: Φ(-x) = 1 - Φ(x)
        self.assertAlmostEqual(standard_normal_cdf(-1.96), 0.025, places=3)

    def test_monotonic(self):
        # Larger x → larger CDF
        self.assertLess(standard_normal_cdf(0.0), standard_normal_cdf(1.0))
        self.assertLess(standard_normal_cdf(-1.0), standard_normal_cdf(0.0))

    def test_bounded_zero_one(self):
        for x in (-10, -3, -1, 0, 1, 3, 10):
            cdf = standard_normal_cdf(x)
            self.assertGreaterEqual(cdf, 0.0)
            self.assertLessEqual(cdf, 1.0)


class TestComputeFairUpdownEdgeCases(unittest.TestCase):
    def test_sigma_too_small_returns_half(self):
        result = compute_fair_updown(
            s_now=105000.0, ref_px=104000.0,
            sigma_15m=0.0, tau_sec=600.0,
        )
        self.assertEqual(result["fair_up"], 0.5)
        self.assertEqual(result["fair_down"], 0.5)
        self.assertEqual(result["z_score"], 0.0)
        self.assertIsNone(result["edge_bps_vs_market"])

    def test_sigma_below_min_returns_half(self):
        result = compute_fair_updown(
            s_now=105000.0, ref_px=104000.0,
            sigma_15m=MIN_SIGMA / 2, tau_sec=600.0,
        )
        self.assertEqual(result["fair_up"], 0.5)
        self.assertEqual(result["fair_down"], 0.5)

    def test_tau_too_small_returns_half(self):
        result = compute_fair_updown(
            s_now=105000.0, ref_px=104000.0,
            sigma_15m=0.003, tau_sec=0.5,
        )
        self.assertEqual(result["fair_up"], 0.5)
        self.assertEqual(result["fair_down"], 0.5)
        self.assertIsNone(result["edge_bps_vs_market"])

    def test_tau_below_min_returns_half(self):
        result = compute_fair_updown(
            s_now=105000.0, ref_px=104000.0,
            sigma_15m=0.003, tau_sec=MIN_TAU_SEC - 0.1,
        )
        self.assertEqual(result["fair_up"], 0.5)

    def test_invalid_prices_return_half(self):
        # Zero/negative prices → fallback
        r1 = compute_fair_updown(s_now=0, ref_px=100, sigma_15m=0.003, tau_sec=600)
        r2 = compute_fair_updown(s_now=100, ref_px=0, sigma_15m=0.003, tau_sec=600)
        r3 = compute_fair_updown(s_now=-100, ref_px=100, sigma_15m=0.003, tau_sec=600)
        for r in (r1, r2, r3):
            self.assertEqual(r["fair_up"], 0.5)
            self.assertEqual(r["fair_down"], 0.5)


class TestComputeFairUpdownMath(unittest.TestCase):
    def test_price_far_above_ref_fair_up_high(self):
        # 1% above ref, modest vol, half window left
        result = compute_fair_updown(
            s_now=105000.0, ref_px=104000.0,
            sigma_15m=0.003, tau_sec=450.0,
        )
        self.assertGreater(result["fair_up"], 0.7)
        self.assertLess(result["fair_down"], 0.3)
        # fair_up + fair_down ≈ 1
        self.assertAlmostEqual(result["fair_up"] + result["fair_down"], 1.0, places=3)

    def test_price_far_below_ref_fair_down_high(self):
        result = compute_fair_updown(
            s_now=103000.0, ref_px=104000.0,
            sigma_15m=0.003, tau_sec=450.0,
        )
        self.assertGreater(result["fair_down"], 0.7)
        self.assertLess(result["fair_up"], 0.3)

    def test_price_equals_ref_is_fifty_fifty(self):
        result = compute_fair_updown(
            s_now=100000.0, ref_px=100000.0,
            sigma_15m=0.003, tau_sec=450.0,
        )
        # ln(1) = 0 → z = 0 → fair_up = 0.5
        self.assertAlmostEqual(result["fair_up"], 0.5, places=3)
        self.assertAlmostEqual(result["z_score"], 0.0, places=3)

    def test_z_score_sign_matches_log_ratio(self):
        # z = ln(s/ref) / (sigma * sqrt(tau/window))
        s, r, sig, tau, win = 105000.0, 100000.0, 0.005, 600.0, 900.0
        result = compute_fair_updown(s_now=s, ref_px=r, sigma_15m=sig, tau_sec=tau, window_sec=win)
        expected_z = math.log(s / r) / (sig * math.sqrt(tau / win))
        self.assertAlmostEqual(result["z_score"], round(expected_z, 3), places=2)
        # When ref_px higher than s_now, z must be negative
        result2 = compute_fair_updown(s_now=95000.0, ref_px=100000.0, sigma_15m=0.005, tau_sec=600)
        self.assertLess(result2["z_score"], 0)

    def test_edge_bps_vs_market_positive_when_underpriced(self):
        # Compute fair_up from first call, then use it as market_price baseline.
        # Call 2: market_price < fair_up → positive edge.
        fair_only = compute_fair_updown(
            s_now=105000.0, ref_px=100000.0,
            sigma_15m=0.01, tau_sec=900.0,
        )
        fair_up_val = fair_only["fair_up"]
        # Use a market price strictly below fair_up
        market_under = fair_up_val - 0.02
        result = compute_fair_updown(
            s_now=105000.0, ref_px=100000.0,
            sigma_15m=0.01, tau_sec=900.0,
            market_price=market_under,
        )
        self.assertIsNotNone(result["edge_bps_vs_market"])
        # Edge should be ~200 bps (0.02 * 10000)
        self.assertAlmostEqual(result["edge_bps_vs_market"], 200.0, places=1)
        self.assertGreater(result["edge_bps_vs_market"], 0)

    def test_edge_bps_vs_market_negative_when_overpriced(self):
        # market price above fair → negative edge
        result = compute_fair_updown(
            s_now=100000.0, ref_px=100000.0,  # baseline ~0.5
            sigma_15m=0.005, tau_sec=900.0,
            market_price=0.55,
        )
        # fair ≈ 0.5, market 0.55 → edge ≈ -500 bps
        self.assertIsNotNone(result["edge_bps_vs_market"])
        self.assertAlmostEqual(result["edge_bps_vs_market"], -500.0, places=1)
        self.assertLess(result["edge_bps_vs_market"], 0)

    def test_edge_bps_none_when_market_price_invalid(self):
        result = compute_fair_updown(
            s_now=105000.0, ref_px=100000.0,
            sigma_15m=0.005, tau_sec=900.0,
            market_price=None,
        )
        self.assertIsNone(result["edge_bps_vs_market"])

    def test_output_keys(self):
        result = compute_fair_updown(
            s_now=105000.0, ref_px=100000.0,
            sigma_15m=0.005, tau_sec=900.0,
            market_price=0.5,
        )
        for key in ("fair_up", "fair_down", "z_score", "edge_bps_vs_market"):
            self.assertIn(key, result)

    def test_fair_probabilities_in_unit_interval(self):
        cases = [
            (100000, 100000, 0.003, 900, 900),
            (101000, 100000, 0.003, 600, 900),
            (99000, 100000, 0.005, 300, 900),
            (105000, 100000, 0.001, 100, 900),
        ]
        for s, r, sig, tau, win in cases:
            result = compute_fair_updown(
                s_now=s, ref_px=r, sigma_15m=sig, tau_sec=tau, window_sec=win
            )
            self.assertGreaterEqual(result["fair_up"], 0.0)
            self.assertLessEqual(result["fair_up"], 1.0)
            self.assertGreaterEqual(result["fair_down"], 0.0)
            self.assertLessEqual(result["fair_down"], 1.0)


class TestComputeEdgeBps(unittest.TestCase):
    def test_positive_edge(self):
        self.assertAlmostEqual(compute_edge_bps(0.55, 0.50), 500.0, places=4)

    def test_negative_edge(self):
        self.assertAlmostEqual(compute_edge_bps(0.45, 0.50), -500.0, places=4)

    def test_zero_edge(self):
        self.assertAlmostEqual(compute_edge_bps(0.50, 0.50), 0.0, places=4)

    def test_none_inputs(self):
        self.assertIsNone(compute_edge_bps(None, 0.5))
        self.assertIsNone(compute_edge_bps(0.5, None))


if __name__ == "__main__":
    unittest.main()
