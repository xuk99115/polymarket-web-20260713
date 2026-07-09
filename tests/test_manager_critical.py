#!/usr/bin/env python3
"""
TradingBotManager 关键路径单元测试。
覆盖资金安全核心逻辑：
  - _should_close_position: 过期结算、止盈、止损判定
  - check_mode_swap: paper/live 模式切换与状态清理
  - _archive_live_to_paper: 实盘→模拟 归档与现金重置
"""

import json
import pytest
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch, AsyncMock, PropertyMock
from typing import Any, Dict, Optional

# ── 测试用 fixtures ──


@pytest.fixture
def mock_config():
    """Config.get/get_float/get_int/get_bool 的 mock 桩"""
    with patch("src.trading.manager.Config") as cfg:
        cfg.get.side_effect = lambda k, d="": {
            "TRADING_MODE": "paper",
            "LIVE_EXIT_BEFORE_EXPIRY_SECONDS": "90",
            "TAKE_PROFIT_PERCENT": "0.18",
            "PAPER_TAKE_PROFIT_USD": "0.12",
            "STOP_LOSS_ENABLED": "false",
            "STOP_LOSS_PERCENT": "0.10",
            "PAPER_START_BALANCE": "100",
        }.get(k, d)
        cfg.get_float.side_effect = lambda k, d=0.0: {
            "TAKE_PROFIT_PERCENT": 0.18,
            "PAPER_TAKE_PROFIT_USD": 0.12,
            "STOP_LOSS_PERCENT": 0.10,
            "PAPER_START_BALANCE": 100.0,
        }.get(k, float(d))
        cfg.get_int.side_effect = lambda k, d=0: {
            "LIVE_EXIT_BEFORE_EXPIRY_SECONDS": 90,
        }.get(k, int(d))
        cfg.get_bool.side_effect = lambda k, d="false": {
            "STOP_LOSS_ENABLED": False,
        }.get(k, d.lower() in {"true", "1", "yes"})
        yield cfg


@pytest.fixture
def state_manager():
    """StateManager mock: get_state/save/update 都 mock 掉"""
    mock = MagicMock()
    mock.get_state.return_value = {
        "positions": [],
        "orders": [],
        "cash_balance": 500.0,
        "trades": [],
        "stats": {},
        "summary": {},
    }
    return mock


@pytest.fixture
def manager(mock_config, state_manager):
    """创建一个 TradingBotManager 实例 (所有外部依赖都 mock 掉)"""
    with patch("src.trading.manager.PolymarketClient"), \
         patch("src.trading.manager.BTCDataprovider"), \
         patch("src.trading.manager.AIDecisionEngine"), \
         patch("src.trading.manager.ReversalEngine"), \
         patch("src.trading.manager.LowBuyDoubleEngine"), \
         patch("src.trading.manager.StateManager", return_value=state_manager), \
         patch("src.trading.manager.PaperExecutor") as mock_exec:

        mock_exec.return_value.mode = "paper"
        from src.trading.manager import TradingBotManager
        m = TradingBotManager()
        m.state_manager = state_manager
        yield m


# ── _should_close_position ──

class TestShouldClosePosition:
    """_should_close_position 是资金安全第一道防线: 何时平仓、过期结算价计算"""

    def test_live_mode_market_closed(self, manager):
        """实盘模式 + 市场已关闭 → 不应主动平仓 (等 Polymarket 结算)"""
        manager.current_mode = "live"
        now = datetime.now(timezone.utc)
        market = {"closed": True, "end_date": None}
        position = {"stake": 2.0, "shares": 5.0, "current_bid": 0.45}
        outcome = {"best_bid": 0.45, "index": 0}

        exit_price, reason = manager._should_close_position(now, market, position, outcome)
        assert exit_price is None
        assert reason is None

    def test_live_mode_expiry_exit(self, manager):
        """实盘模式 + 临近到期 (<90s) → EXPIRY_EXIT"""
        manager.current_mode = "live"
        now = datetime(2026, 6, 26, 10, 14, 30, tzinfo=timezone.utc)
        market = {"end_date": "2026-06-26T10:15:00Z", "closed": None}
        position = {"stake": 2.0, "shares": 5.0, "current_bid": 0.45}
        outcome = {"best_bid": 0.45, "index": 0}

        exit_price, reason = manager._should_close_position(now, market, position, outcome)
        assert exit_price == 0.45
        assert reason == "EXPIRY_EXIT"

    def test_paper_mode_expiry_settlement_win(self, manager):
        """模拟盘 + 市场已关闭 + outcomePrices 赢家 → 结算价 1.0"""
        manager.current_mode = "paper"
        now = datetime(2026, 6, 26, 10, 15, 1, tzinfo=timezone.utc)
        market = {
            "closed": True,
            "end_date": "2026-06-26T10:15:00Z",
            "outcomePrices": [1.0, 0.0],
        }
        position = {"stake": 2.0, "shares": 5.0}
        outcome = {"best_bid": 0.33, "index": 0, "price": 0.5}

        exit_price, reason = manager._should_close_position(now, market, position, outcome)
        assert exit_price == 1.0
        assert reason == "EXPIRY_EXIT"

    def test_paper_mode_expiry_settlement_lose(self, manager):
        """模拟盘 + 市场已关闭 + outcomePrices 输家 → 结算价 0.0"""
        manager.current_mode = "paper"
        now = datetime(2026, 6, 26, 10, 15, 1, tzinfo=timezone.utc)
        market = {
            "closed": True,
            "end_date": "2026-06-26T10:15:00Z",
            "outcomePrices": [1.0, 0.0],
        }
        position = {"stake": 2.0, "shares": 5.0}
        outcome = {"best_bid": 0.9, "index": 1, "price": 0.5}  # Down side, index=1 → 0.0

        exit_price, reason = manager._should_close_position(now, market, position, outcome)
        assert exit_price == 0.0
        assert reason == "EXPIRY_EXIT"

    def test_paper_mode_no_outcome_prices_fallback(self, manager):
        """模拟盘 + 无 outcomePrices → 兜底返回 0.0 (不猜赢了).

        Bug fix 2026-06-27: 旧逻辑在 price > 0.5 时返回 1.0 (猜赢了),
        是 BUG-2 的源头 (用 stale 的过期前 last-trade 猜"赢了", 实际可能输).
        修复后: outcomePrices 没就绪 → 返回 0.0 (兜底), 调用方下个 cycle 再试.
        真实生产中 Polymarket settlement 通常 1-5 分钟就绪, 不会持续 0.0.
        """
        manager.current_mode = "paper"
        now = datetime(2026, 6, 26, 10, 15, 1, tzinfo=timezone.utc)
        market = {"closed": True, "end_date": "2026-06-26T10:15:00Z"}
        position = {"stake": 2.0, "shares": 5.0}
        outcome = {"best_bid": 0.55, "index": 0, "price": 0.55}

        exit_price, reason = manager._should_close_position(now, market, position, outcome)
        # Bug fix 2026-06-27: 期望从 1.0 改为 0.0 — 我们主动删了 "猜赢" 逻辑.
        # 测试目的是确认 "无 outcomePrices → 兜底 0.0 而非错误推断".
        assert exit_price == 0.0
        assert reason == "EXPIRY_EXIT"

    def test_take_profit(self, manager):
        """pnl >= 止盈阈值 → TAKE_PROFIT"""
        manager.current_mode = "paper"
        now = datetime.now(timezone.utc)
        market = {}
        position = {"stake": 2.0, "shares": 5.0}
        outcome = {"best_bid": 0.80, "index": 0}
        # pnl = 5*0.80 - 2 = 2.0 ≥ 0.12 (PAPER_TAKE_PROFIT_USD)

        exit_price, reason = manager._should_close_position(now, market, position, outcome)
        assert exit_price == 0.80
        assert reason == "TAKE_PROFIT"

    def test_stop_loss_enabled(self, manager):
        """STOP_LOSS 开启 + pnl 跌破阈值 → STOP_LOSS"""
        manager.current_mode = "paper"
        now = datetime.now(timezone.utc)
        market = {}
        position = {"stake": 2.0, "shares": 5.0}
        outcome = {"best_bid": 0.10, "index": 0}
        # pnl = 5*0.10 - 2 = -1.5 ≤ -0.20 (STOP_LOSS 10% × 2.0)

        with patch("src.trading.manager.Config.get_bool", return_value=True):
            exit_price, reason = manager._should_close_position(now, market, position, outcome)
            assert exit_price == 0.10
            assert reason == "STOP_LOSS"

    def test_no_action(self, manager):
        """pnl 在止盈和止损之间 → 不触发"""
        manager.current_mode = "paper"
        now = datetime.now(timezone.utc)
        market = {}
        position = {"stake": 2.0, "shares": 5.0}
        outcome = {"best_bid": 0.45, "index": 0}
        # pnl = 5*0.45 - 2 = 0.25 ≥ 0.12 → TAKE_PROFIT would trigger!
        # Let me use a lower bid
        outcome2 = {"best_bid": 0.38, "index": 0}
        # pnl = 5*0.38 - 2 = -0.10

        exit_price, reason = manager._should_close_position(now, market, position, outcome2)
        # -0.10 < 0.12 (take profit) and -0.10 > -0.20 (stop loss disabled)
        # So None
        assert exit_price is None
        assert reason is None

    def test_bid_zero_no_close(self, manager):
        """bid = 0 → 不触发平仓 (等 EXPIRY_EXIT 兜底)"""
        manager.current_mode = "paper"
        now = datetime.now(timezone.utc)
        market = {}
        position = {"stake": 2.0, "shares": 5.0}
        outcome = {"best_bid": 0.0, "index": 0}

        exit_price, reason = manager._should_close_position(now, market, position, outcome)
        assert exit_price is None
        assert reason is None


# ── check_mode_swap ──

class TestCheckModeSwap:
    """模式切换：错误的切换会直接导致资金损失或状态污染"""

    def test_same_mode_noop(self, manager):
        """TRADING_MODE 没变 → 什么都不做"""
        manager.current_mode = "paper"
        manager.executor.mode = "paper"
        with patch("src.trading.manager.Config.get", return_value="paper"):
            import asyncio
            asyncio.run(manager.check_mode_swap())
        assert manager.current_mode == "paper"

    def test_paper_to_live_clears_positions(self, manager, state_manager):
        """paper → live: 丢弃模拟持仓和挂单"""
        manager.current_mode = "paper"
        manager.executor = MagicMock()
        manager.executor.mode = "live"

        with patch("src.trading.manager.Config.get", return_value="live"):
            import asyncio
            asyncio.run(manager.check_mode_swap())

        state_manager.update.assert_any_call("positions", [])
        state_manager.update.assert_any_call("orders", [])
        assert manager.current_mode == "live"

    def test_live_to_paper_archives(self, manager, state_manager):
        """live → paper: 归档 + 重置现金"""
        manager.current_mode = "live"
        manager.executor = MagicMock()
        manager.executor.mode = "paper"
        state_manager.get_state.return_value = {
            "positions": [{"id": "1", "stake": 10}],
            "orders": [{"id": "o1"}],
            "cash_balance": 500.0,
        }

        with patch("src.trading.manager.Config.get", return_value="paper"):
            import asyncio
            asyncio.run(manager.check_mode_swap())

        assert manager.current_mode == "paper"
        archived = state_manager.get_state.return_value.get("archived_live_sessions", [])
        assert len(archived) > 0
        assert archived[0]["cash_balance_at_archive"] == 500.0
        # cash reset
        assert state_manager.get_state.return_value["cash_balance"] == 100.0

    def test_force_trading_mode_on_executor_mismatch(self, manager, state_manager):
        """请求 live 但 executor 回退到 paper → force_trading_mode(paper)"""
        manager.current_mode = "paper"
        manager.executor = MagicMock()
        manager.executor.mode = "paper"

        with patch("src.trading.manager.Config.get", return_value="live"), \
             patch("src.trading.manager.LiveExecutor") as mock_live, \
             patch.object(manager, "_force_trading_mode") as mock_force:
            # LiveExecutor should fail → fallback to PaperExecutor
            mock_live.side_effect = ValueError("缺少凭证")
            import asyncio
            asyncio.run(manager.check_mode_swap())

        mock_force.assert_called_once_with("paper")


# ── _archive_live_to_paper ──

class TestArchiveLiveToPaper:
    """live → paper 转换时的数据安全"""

    def test_archive_with_positions(self, manager, state_manager):
        """有持仓/挂单 → 存档 + 清空 + 重置现金"""
        state = {
            "positions": [{"id": "p1", "stake": 10.0}],
            "orders": [{"id": "o1"}],
            "cash_balance": 520.0,
            "trades": [],
            "stats": {},
        }
        state_manager.get_state.return_value = state

        manager._archive_live_to_paper()

        assert state["positions"] == []  # cleared
        assert state["orders"] == []     # cleared
        assert state["cash_balance"] == 100.0  # reset
        assert len(state["archived_live_sessions"]) == 1
        session = state["archived_live_sessions"][0]
        assert session["cash_balance_at_archive"] == 520.0
        assert session["positions"] == [{"id": "p1", "stake": 10.0}]

    def test_archive_without_positions(self, manager, state_manager):
        """无持仓/挂单 → 不存档, 但清空+重置现金"""
        state = {
            "positions": [],
            "orders": [],
            "cash_balance": 500.0,
            "trades": [],
            "stats": {},
        }
        state_manager.get_state.return_value = state

        manager._archive_live_to_paper()

        assert "archived_live_sessions" not in state
        assert state["cash_balance"] == 100.0

    def test_archive_max_cap(self, manager, state_manager):
        """归档列表不超过 5 份"""
        old_archived = [
            {"archived_at": f"2026-06-26T0{i}:00:00Z", "positions": [], "orders": [], "cash_balance_at_archive": 100}
            for i in range(5)
        ]
        state = {
            "positions": [{"id": "p1"}],
            "orders": [],
            "cash_balance": 600.0,
            "archived_live_sessions": old_archived,
        }
        state_manager.get_state.return_value = state

        manager._archive_live_to_paper()

        archived = state["archived_live_sessions"]
        assert len(archived) == 5
        assert archived[0]["cash_balance_at_archive"] == 600.0  # newest first


class TestRefreshSummary:
    def test_refresh_summary_rebuilds_stats_from_trades(self, manager, state_manager):
        state = {
            "positions": [],
            "orders": [],
            "cash_balance": 90.3132,
            "trades": [
                {"status": "TIME_STOP", "realized_profit": -1.2, "closed_at": "2026-07-06T11:40:03Z"},
                {"status": "TAKE_PROFIT", "realized_profit": 2.5, "closed_at": "2026-07-06T11:30:03Z"},
                {"status": "OPEN", "realized_profit": 99.0},
            ],
            "stats": {"total_trades": 233, "winning_trades": 72, "losing_trades": 161, "total_profit": 13.2484},
            "summary": {},
            "report": {},
        }
        state_manager.get_state.return_value = state

        manager._refresh_summary()

        assert state["stats"] == {
            "total_trades": 2,
            "winning_trades": 1,
            "losing_trades": 1,
            "total_profit": 1.3,
        }
        assert state["summary"]["realized_pnl"] == 1.3
        assert state["summary"]["total_trades"] == 2
        assert state["summary"]["winning_trades"] == 1
        assert state["summary"]["ending_balance"] == 101.3

    def test_refresh_summary_repairs_flat_cash_balance_drift(self, manager, state_manager):
        state = {
            "positions": [],
            "orders": [],
            "cash_balance": 113.2483,
            "trades": [
                {"status": "TIME_STOP", "realized_profit": -1.2, "closed_at": "2026-07-06T11:40:03Z"},
                {"status": "TAKE_PROFIT", "realized_profit": 2.5, "closed_at": "2026-07-06T11:30:03Z"},
            ],
            "stats": {"total_trades": 233, "winning_trades": 72, "losing_trades": 161, "total_profit": 13.2484},
            "summary": {},
            "report": {},
        }
        state_manager.get_state.return_value = state

        manager._refresh_summary()

        assert state["cash_balance"] == 101.3
        assert state["summary"]["cash_balance"] == 101.3
        assert state["summary"]["ending_balance"] == 101.3
