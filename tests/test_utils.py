"""核心工具函数单测。

覆盖:
- safe_float / first_float 的边界
- extract_market_slug 各种输入
- parse_json_list
- save_json_file 原子写
"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.core.utils import (
    safe_float,
    first_float,
    extract_market_slug,
    parse_json_list,
    save_json_file,
    load_json_file,
    load_trading_control,
)


class TestSafeFloat(unittest.TestCase):
    def test_none_returns_default(self):
        self.assertIsNone(safe_float(None))
        self.assertEqual(safe_float(None, 0.5), 0.5)

    def test_empty_string_returns_default(self):
        self.assertEqual(safe_float("", 0.0), 0.0)

    def test_valid_string(self):
        self.assertEqual(safe_float("3.14"), 3.14)

    def test_invalid_returns_default(self):
        self.assertEqual(safe_float("not_a_number", 99.0), 99.0)

    def test_zero_preserved(self):
        self.assertEqual(safe_float(0, 99.0), 0.0)
        self.assertEqual(safe_float("0", 99.0), 0.0)


class TestFirstFloat(unittest.TestCase):
    def test_picks_first_valid(self):
        self.assertEqual(first_float(None, "3.14", default=0.0), 3.14)

    def test_zero_is_valid(self):
        """0.0 是有效值,不是 None,所以应该被选。"""
        self.assertEqual(first_float(0.0, 3.14, default=99.0), 0.0)

    def test_all_none_returns_default(self):
        self.assertEqual(first_float(None, None, default=0.5), 0.5)

    def test_skips_invalid_string(self):
        self.assertEqual(first_float("abc", 2.5, default=0.0), 2.5)


class TestExtractMarketSlug(unittest.TestCase):
    def test_url_event(self):
        self.assertEqual(extract_market_slug("https://polymarket.com/event/foo-bar"), "foo-bar")

    def test_url_market(self):
        self.assertEqual(extract_market_slug("https://polymarket.com/market/baz"), "baz")

    def test_strips_query_and_fragment(self):
        self.assertEqual(extract_market_slug("https://polymarket.com/event/foo?x=1#y"), "foo")

    def test_plain_slug(self):
        self.assertEqual(extract_market_slug("btc-updown-15m-12345"), "btc-updown-15m-12345")

    def test_empty(self):
        self.assertEqual(extract_market_slug(""), "")
        self.assertEqual(extract_market_slug(None), "")


class TestParseJsonList(unittest.TestCase):
    def test_already_list(self):
        self.assertEqual(parse_json_list([1, 2, 3]), [1, 2, 3])

    def test_json_string(self):
        self.assertEqual(parse_json_list('["a", "b"]'), ["a", "b"])

    def test_invalid_string(self):
        self.assertEqual(parse_json_list("not json"), [])

    def test_empty(self):
        self.assertEqual(parse_json_list(None), [])
        self.assertEqual(parse_json_list(""), [])


class TestSaveJsonFileAtomic(unittest.TestCase):
    def test_writes_and_reads_back(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "test.json")
            save_json_file(path, {"a": 1, "b": [1, 2, 3]})
            self.assertEqual(load_json_file(path, None), {"a": 1, "b": [1, 2, 3]})
            # tmp 文件不应残留
            self.assertFalse(os.path.exists(path + ".tmp"))

    def test_overwrite(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "test.json")
            save_json_file(path, {"v": 1})
            save_json_file(path, {"v": 2})
            self.assertEqual(load_json_file(path, None), {"v": 2})


class TestTradingControl(unittest.TestCase):
    def test_missing_control_defaults_closed(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "missing.json")
            self.assertEqual(load_trading_control(path)["trading_enabled"], False)

    def test_invalid_control_defaults_closed(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "bad.json")
            with open(path, "w", encoding="utf-8") as f:
                f.write("{bad json")
            self.assertEqual(load_trading_control(path)["trading_enabled"], False)

    def test_explicit_control_value_preserved(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "control.json")
            save_json_file(path, {"trading_enabled": True})
            self.assertEqual(load_trading_control(path)["trading_enabled"], True)


if __name__ == "__main__":
    unittest.main()
