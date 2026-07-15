"""LiveExecutor 凭证校验单测。

覆盖:
- 缺 POLYMARKET_PRIVATE_KEY / POLYMARKET_FUNDER_ADDRESS 时 raise ValueError
- 不会静默进 dry_run
"""
import os
import sys
import unittest
import asyncio
from unittest.mock import MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class TestLiveExecutorCredentialCheck(unittest.TestCase):
    def setUp(self):
        # 屏蔽真实 .env: 让 _read_env 返回空
        import src.core.config as cfg
        self._orig_read_env = cfg._ConfigCache._read_env
        cfg._ConfigCache._read_env = staticmethod(lambda: {})
        cfg._CACHE.invalidate()
        # 屏蔽进程 env: 让 os.getenv 也不返回真值（保存所有原值用于恢复）
        self._saved_env = {}
        for k in ("POLYMARKET_PRIVATE_KEY", "POLYMARKET_FUNDER_ADDRESS",
                  "POLYMARKET_API_KEY", "POLYMARKET_API_SECRET",
                  "POLYMARKET_API_PASSPHRASE", "POLYMARKET_WALLET_ADDRESS"):
            self._saved_env[k] = os.environ.pop(k, None)
        cfg.invalidate()

    def tearDown(self):
        import src.core.config as cfg
        cfg._ConfigCache._read_env = staticmethod(self._orig_read_env)
        cfg._CACHE.invalidate()
        # 恢复被 pop 的环境变量
        for k, v in self._saved_env.items():
            if v is not None:
                os.environ[k] = v

    def test_missing_private_key_raises(self):
        from src.trading.executor import LiveExecutor
        from src.core.state import StateManager
        from src.core.config import PAPER_STATE_FILE
        sm = StateManager(PAPER_STATE_FILE)
        with self.assertRaises(ValueError) as ctx:
            LiveExecutor(sm)
        self.assertIn("POLYMARKET_PRIVATE_KEY", str(ctx.exception))

    def test_missing_funder_raises(self):
        import src.core.config as cfg
        # 这次给 private key 但不给 funder
        cfg._ConfigCache._read_env = staticmethod(lambda: {"POLYMARKET_PRIVATE_KEY": "0xtest"})
        cfg.invalidate()
        from src.trading.executor import LiveExecutor
        from src.core.state import StateManager
        from src.core.config import PAPER_STATE_FILE
        sm = StateManager(PAPER_STATE_FILE)
        with self.assertRaises(ValueError) as ctx:
            LiveExecutor(sm)
        self.assertIn("POLYMARKET_FUNDER_ADDRESS", str(ctx.exception))


class TestLiveExecutorSyncFailure(unittest.TestCase):
    def test_open_order_query_failure_preserves_local_status(self):
        from src.trading.executor import LiveExecutor

        state = {"orders": [{"id": "o1", "status": "SUBMITTED"}]}
        executor = LiveExecutor.__new__(LiveExecutor)
        executor.state_manager = MagicMock()
        executor.state_manager.get_state.return_value = state
        executor.live_trader = MagicMock()
        executor.live_trader.get_open_orders.return_value = None

        messages = asyncio.run(executor.sync_state())

        self.assertEqual(state["orders"][0]["status"], "SUBMITTED")
        self.assertIn("未变更", messages[0])
        executor.state_manager.save.assert_not_called()


if __name__ == "__main__":
    unittest.main()
