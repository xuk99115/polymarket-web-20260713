"""Status server smoke test: 启一个临时 server 在 random port, 遍历所有 /api/* 端点, 断言 200。

用途: 防止 status_server 改动导致静默 502 / 5xx, 避免前端用 fallback 数据掩盖真问题。

注意: 不动现有 8889 server。测试自己起一个独立实例。
"""
from __future__ import annotations

import json
import os
import socket
import sys
import threading
import time
import unittest
import urllib.request
import urllib.error

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class StatusServerSmokeTest(unittest.TestCase):
    """启一个临时 status_server 实例 (port 随机, 不冲突), 跑 API smoke。"""

    @classmethod
    def setUpClass(cls):
        # 找一个空闲端口
        cls.port = _free_port()
        # Monkey-patch PORT
        from src.server import status_server as srv
        cls._orig_port = srv.PORT
        srv.PORT = cls.port

        # 启 server 在后台线程
        from src.server.status_server import run_server
        cls._server_thread = threading.Thread(target=run_server, daemon=True)
        cls._server_thread.start()
        # 等 server ready
        deadline = time.time() + 5
        while time.time() < deadline:
            try:
                with socket.create_connection(("127.0.0.1", cls.port), timeout=0.3):
                    break
            except OSError:
                time.sleep(0.1)

    @classmethod
    def tearDownClass(cls):
        # 端口关了 ThreadingTCPServer 会自然退出
        from src.server import status_server as srv
        srv.PORT = cls._orig_port
        # 不需要显式 kill, daemon thread 跟 test runner 一起退出

    def setUp(self):
        # 读 token
        from src.core.config import DATA_DIR
        token_path = os.path.join(DATA_DIR, ".web_token")
        if os.path.exists(token_path):
            self.token = open(token_path).read().strip()
        else:
            self.token = ""
        self.base = f"http://127.0.0.1:{self.port}"

    def _get(self, path: str, *, with_token: bool = True) -> tuple[int, dict | str]:
        headers = {}
        if with_token and self.token:
            headers["X-Api-Key"] = self.token
        req = urllib.request.Request(self.base + path, headers=headers)
        try:
            resp = urllib.request.urlopen(req, timeout=5)
            body = resp.read().decode("utf-8", errors="replace")
            try:
                return resp.status, json.loads(body)
            except Exception:
                return resp.status, body
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="replace")
            try:
                return e.code, json.loads(body)
            except Exception:
                return e.code, body

    def _post(self, path: str, payload: dict) -> tuple[int, dict | str]:
        headers = {"Content-Type": "application/json"}
        if self.token:
            headers["X-Api-Key"] = self.token
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(self.base + path, data=data, headers=headers, method="POST")
        try:
            resp = urllib.request.urlopen(req, timeout=5)
            body = resp.read().decode("utf-8", errors="replace")
            try:
                return resp.status, json.loads(body)
            except Exception:
                return resp.status, body
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="replace")
            try:
                return e.code, json.loads(body)
            except Exception:
                return e.code, body

    # ===== 静态文件 (不需要 token) =====

    def test_static_html(self):
        status, _ = self._get("/status.html", with_token=False)
        self.assertEqual(status, 200, "/status.html 静态文件必须 200")

    def test_static_js(self):
        status, _ = self._get("/js/app.js", with_token=False)
        self.assertEqual(status, 200, "/js/app.js 静态文件必须 200")

    def test_root_redirects_to_html(self):
        status, _ = self._get("/", with_token=False)
        self.assertEqual(status, 200, "/ 应该 serve status.html")

    # ===== Auth: 401 必须是 401 =====

    def test_api_requires_token(self):
        from src.server import status_server as srv
        original = srv.StatusHandler._check_token
        try:
            srv.StatusHandler._check_token = lambda _self: False
            status, body = self._get("/api/btc", with_token=False)
            self.assertEqual(status, 401, "非本机 API 不带 token 必须 401")
        finally:
            srv.StatusHandler._check_token = original

    def test_api_rejects_wrong_token(self):
        from src.server import status_server as srv
        original = srv.StatusHandler._check_token
        try:
            srv.StatusHandler._check_token = lambda _self: False
            req = urllib.request.Request(
                self.base + "/api/btc",
                headers={"X-Api-Key": "definitely-wrong-token-xyz123"},
            )
            try:
                urllib.request.urlopen(req, timeout=3)
                self.fail("错 token 应该 401")
            except urllib.error.HTTPError as e:
                self.assertEqual(e.code, 401)
        finally:
            srv.StatusHandler._check_token = original

    def test_localhost_api_without_token_allowed(self):
        status, body = self._get("/api/btc", with_token=False)
        self.assertEqual(status, 200, "本机/SSH 隧道访问不需要手填 token")
        self.assertIsInstance(body, dict)

    # ===== GET API: 必须 200 =====

    def test_api_btc(self):
        status, body = self._get("/api/btc")
        self.assertEqual(status, 200, "/api/btc 必须 200")
        self.assertIsInstance(body, dict, "/api/btc 必须返回 JSON dict")

    def test_api_btc_trend(self):
        status, body = self._get("/api/btc-trend")
        self.assertEqual(status, 200, "/api/btc-trend 必须 200 (bot 没启动时返回 fallback)")
        self.assertIsInstance(body, dict)

    def test_api_control(self):
        status, body = self._get("/api/control")
        self.assertEqual(status, 200, "/api/control 必须 200")
        self.assertIsInstance(body, dict)
        self.assertIn("trading_enabled", body)

    def test_api_status(self):
        status, body = self._get("/status-json")
        self.assertEqual(status, 200, "/status-json 必须 200")
        self.assertIsInstance(body, dict)

    def test_api_config(self):
        """这是上次出 bug 的地方, 重点保护。"""
        status, body = self._get("/api/config")
        self.assertEqual(status, 200, "/api/config 必须 200 (不允许 502)")
        self.assertIsInstance(body, dict)
        # 关键字段都得在
        for k in (
            "trading_mode", "paper_start_balance", "strategy_profile",
            "FV_EDGE_POSITION_USD", "FV_EDGE_THRESHOLD_BPS", "FV_EDGE_MAX_MTE",
        ):
            self.assertIn(k, body, f"/api/config 必须包含 {k}")

    def test_api_balance_paper(self):
        status, body = self._get("/api/balance?account=paper")
        self.assertEqual(status, 200, "/api/balance?account=paper 必须 200")
        self.assertIsInstance(body, dict)
        self.assertIn("balance", body)
        self.assertIn("source", body)

    def test_api_real_balance(self):
        status, body = self._get("/api/real-balance")
        self.assertEqual(status, 200, "/api/real-balance 必须 200")
        self.assertIsInstance(body, dict)

    def test_api_positions(self):
        status, body = self._get("/api/positions?account=paper")
        self.assertEqual(status, 200, "/api/positions 必须 200")

    def test_api_trades(self):
        status, body = self._get("/api/trades?account=paper")
        self.assertEqual(status, 200, "/api/trades 必须 200")
        self.assertIsInstance(body, list)

        from src.server.helpers import load_json_file
        from src.core.config import DATA_DIR
        state = load_json_file(os.path.join(DATA_DIR, "paper_trade_state.json"), {}) or {}
        expected = state.get("summary", {}).get("total_trades")
        if expected is not None:
            self.assertEqual(len(body), expected, "纸面交易流水只应返回当前会话的统计口径")
    def test_api_orders(self):
        status, body = self._get("/api/orders?account=paper")
        self.assertEqual(status, 200, "/api/orders 必须 200")

    def test_api_fv_signals(self):
        status, body = self._get("/api/fv-signals")
        self.assertEqual(status, 200, "/api/fv-signals 必须 200")
        self.assertIsInstance(body, list)

    def test_api_orderbook(self):
        """Validate routing without depending on a live Polymarket response."""
        from src.server import status_server as srv

        original = srv.fetch_order_book
        try:
            srv.fetch_order_book = lambda slug: {"slug": slug, "bids": [], "asks": []}
            status, body = self._get("/api/orderbook?slug=btc-updown-15m-test")
            self.assertEqual(status, 200)
            self.assertEqual(body["slug"], "btc-updown-15m-test")
        finally:
            srv.fetch_order_book = original

    # ===== POST API =====

    def test_post_control(self):
        from src.server import status_server as srv

        original = open(srv.CONTROL_FILE, "rb").read() if os.path.exists(srv.CONTROL_FILE) else None
        try:
            status, body = self._post("/api/control", {"trading_enabled": True})
            self.assertEqual(status, 200, "/api/control POST 必须 200")
            self.assertIsInstance(body, dict)
        finally:
            if original is None:
                if os.path.exists(srv.CONTROL_FILE):
                    os.remove(srv.CONTROL_FILE)
            else:
                with open(srv.CONTROL_FILE, "wb") as file:
                    file.write(original)

    def test_post_update_config_accepts_fv_settings(self):
        from src.server import status_server as srv

        original = open(srv.CONTROL_FILE, "rb").read() if os.path.exists(srv.CONTROL_FILE) else None
        try:
            status, body = self._post("/api/update-config", {
                "FV_EDGE_THRESHOLD_BPS": 350,
                "FV_EDGE_MAX_MTE": 1.5,
            })
            self.assertEqual(status, 200)
            self.assertTrue(body.get("success"))
        finally:
            if original is None:
                if os.path.exists(srv.CONTROL_FILE):
                    os.remove(srv.CONTROL_FILE)
            else:
                with open(srv.CONTROL_FILE, "wb") as file:
                    file.write(original)


if __name__ == "__main__":
    unittest.main()
