import unittest

from src.trading.fv_edge import FVEdgeStrategy


class TestDirectionalDiagnostics(unittest.TestCase):
    def test_diagnostics_exposes_split_thresholds(self):
        strat = FVEdgeStrategy(position_usd=2.0, threshold_bps=300, threshold_up_bps=1200, threshold_down_bps=800)
        diag = strat.diagnostics()
        self.assertEqual(diag['thresholds']['edge_bps'], 300.0)
        self.assertEqual(diag['thresholds']['edge_up_bps'], 1200.0)
        self.assertEqual(diag['thresholds']['edge_down_bps'], 800.0)


if __name__ == '__main__':
    unittest.main()
