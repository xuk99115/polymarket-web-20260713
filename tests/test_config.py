"""Config 单测: 验证 cache、invalidate、basic get 行为。"""
import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class TestConfigCache(unittest.TestCase):
    def setUp(self):
        # 创建临时 .env 和临时 trading_control.json
        self.tmp = tempfile.TemporaryDirectory()
        self.env_file = os.path.join(self.tmp.name, ".env")
        self.control_file = os.path.join(self.tmp.name, "trading_control.json")
        with open(self.env_file, "w") as f:
            f.write("TEST_KEY=hello\nTEST_NUM=42\n")
        with open(self.control_file, "w") as f:
            f.write("{}")

        # 改 module-level 路径常量指向临时目录
        import src.core.config as cfg
        cfg.ENV_FILE = self.env_file
        cfg.CONTROL_FILE = self.control_file
        cfg.invalidate()

    def tearDown(self):
        import src.core.config as cfg
        self.tmp.cleanup()

    def test_basic_get(self):
        from src.core.config import Config
        self.assertEqual(Config.get("TEST_KEY"), "hello")
        self.assertEqual(Config.get_int("TEST_NUM", "0"), 42)

    def test_default_when_missing(self):
        from src.core.config import Config
        self.assertEqual(Config.get("MISSING_KEY", "fallback"), "fallback")

    def test_runtime_overrides_env(self):
        """trading_control.json 中的值应该覆盖 .env。"""
        from src.core.config import Config
        with open(self.control_file, "w") as f:
            f.write('{"TEST_KEY": "from_runtime"}')
        from src.core import config as cfg
        cfg.invalidate()
        self.assertEqual(Config.get("TEST_KEY"), "from_runtime")

    def test_cache_ttl(self):
        """20 次连续 get 应当只读 1 次盘 (有 cache)。"""
        from src.core import config as cfg
        from src.core.config import Config

        original = cfg._ConfigCache._read_env
        call_count = [0]

        def counting_read():
            call_count[0] += 1
            return original()

        cfg._ConfigCache._read_env = staticmethod(counting_read)
        cfg.invalidate()
        try:
            for _ in range(20):
                Config.get("TEST_KEY")
            self.assertLessEqual(call_count[0], 1, f"期望 ≤1 次读盘,实际 {call_count[0]}")
        finally:
            cfg._ConfigCache._read_env = staticmethod(original)
            cfg.invalidate()

    def test_invalidate_forces_reread(self):
        """invalidate 后应当能读到新值。"""
        from src.core import config as cfg
        from src.core.config import Config

        with open(self.env_file, "w") as f:
            f.write("TEST_KEY=initial")
        cfg.invalidate()
        self.assertEqual(Config.get("TEST_KEY"), "initial")

        with open(self.env_file, "w") as f:
            f.write("TEST_KEY=updated")
        cfg.invalidate()
        self.assertEqual(Config.get("TEST_KEY"), "updated")


if __name__ == "__main__":
    unittest.main()
