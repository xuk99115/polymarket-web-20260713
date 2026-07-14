import asyncio
import contextlib
import json
import logging
import math
import os
import time as _time
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional, Tuple

from ..ai.decision import AIDecisionEngine
from ..api.market import BTCDataprovider, PolymarketClient
from ..api.fair_value import compute_fair_updown, DEFAULT_WINDOW_SEC as FAIR_WINDOW_SEC, MIN_SIGMA as FAIR_MIN_SIGMA
from ..core.config import Config, CONTROL_FILE, PAPER_STATE_FILE, DATA_DIR
from ..core.state import StateManager, StatusExporter
from ..core.utils import iso_to_utc_dt, load_trading_control, safe_float, save_json_file, load_json_file
from .executor import LiveExecutor, PaperExecutor
from ..trading._arbitrage import should_close_arb_pair, arb_pair_status
from ..trading.reversal import ReversalEngine, MAX_REVERSAL_PRICE
from ..trading.lowbuy_double import (
    LowBuyDoubleEngine,
    LOWBUY_POSITION_USD,
    LOWBUY_TP_MULT,
    LOWBUY_MIN_ENTRY,
    LOWBUY_MAX_ENTRY,
)
from ..trading.hedged_limit import HedgedLimitEngine
from ..trading.fv_edge import FVEdgeStrategy, FV_EDGE_DEFAULT_POSITION_USD
from ..trading.market_helpers import (
    _merge_book_quotes,
    _quote_is_reasonable,
    _market_price_edge,
    _build_ai_prompt,
    DEFAULT_REASONABLE_SPREAD,
)
from ..trading.audit import _append_position_audit
# 2026-06-27: 旁路盘口时间序列采样器 (lowbuy_observer.sample_markets) - 跟仓位完全脱钩

logger = logging.getLogger("trading_manager")
LOWBUY_LIVE_MAX_ASK_DRIFT = 0.01
LOWBUY_LIVE_MAX_ASK_DRIFT_PCT = 0.03
_CLOSED_TRADE_STATUSES = {
    "TAKE_PROFIT",
    "TP",
    "TIME_STOP",
    "STOP_LOSS",
    "EXPIRY_EXIT",
    "CLOSED",
    "SETTLED",
    "FILLED",
}

# 常量/盘口工具从 market_helpers 引用
# _book_summary / _quote_is_reasonable / _pick_reasonable_quote
# _filter_relevant_levels / _format_levels / _market_price_edge
# _build_ai_prompt / _merge_book_quotes / DEFAULT_REASONABLE_SPREAD


def _build_btc_rule_signal(
    market: Dict[str, Any],
    btc: Optional[Dict[str, Any]],
    now_utc: datetime,
) -> Optional[Dict[str, Any]]:
    if not btc or not str(market.get("slug", "")).startswith("btc-updown-15m-"):
        return None

    outcomes = market.get("outcomes", [])
    if len(outcomes) < 2:
        return None

    end_date = market.get("end_date")
    if end_date:
        try:
            seconds_to_end = (iso_to_utc_dt(end_date) - now_utc).total_seconds()
            if seconds_to_end <= Config.get_int("BTC_RULE_MIN_SECONDS_TO_EXPIRY", "300"):
                return None
        except Exception:
            pass

    up_outcome, down_outcome = outcomes[0], outcomes[1]
    up_bid = safe_float(up_outcome.get("best_bid"), up_outcome.get("price"))
    up_ask = safe_float(up_outcome.get("best_ask"), up_outcome.get("price"))
    down_bid = safe_float(down_outcome.get("best_bid"), down_outcome.get("price"))
    down_ask = safe_float(down_outcome.get("best_ask"), down_outcome.get("price"))

    if not _quote_is_reasonable(up_bid, up_ask, up_outcome.get("price")):
        return None
    if not _quote_is_reasonable(down_bid, down_ask, down_outcome.get("price")):
        return None

    change_1m = safe_float(btc.get("change_1m"), 0.0) or 0.0
    change_3m = safe_float(btc.get("change_3m"), 0.0) or 0.0
    change_5m = safe_float(btc.get("change_5m"), 0.0) or 0.0
    change_15m = safe_float(btc.get("change_15m"), 0.0) or 0.0
    range_position = safe_float(btc.get("range_position_15m"), 0.5) or 0.5
    volume_ratio = safe_float(btc.get("volume_ratio_5m"), 1.0) or 1.0
    direction_hint = str(btc.get("direction_hint", "flat")).lower()
    if direction_hint not in {"up", "down"}:
        if change_3m <= -0.04 and change_5m <= -0.06:
            direction_hint = "down"
        elif change_3m >= 0.04 and change_5m >= 0.06:
            direction_hint = "up"

    up_score = 0
    down_score = 0
    up_align = 0
    down_align = 0

    if change_1m >= 0.015:
        up_score += 1
        up_align += 1
    elif change_1m <= -0.015:
        down_score += 1
        down_align += 1

    if change_3m >= 0.04:
        up_score += 2
        up_align += 1
    elif change_3m <= -0.04:
        down_score += 2
        down_align += 1

    if change_5m >= 0.06:
        up_score += 2
        up_align += 1
    elif change_5m <= -0.06:
        down_score += 2
        down_align += 1

    if change_15m >= 0.02:
        up_score += 1
    elif change_15m <= -0.02:
        down_score += 1

    if range_position >= 0.62:
        up_score += 1
    elif range_position <= 0.38:
        down_score += 1

    if direction_hint == "up":
        up_score += 1
    elif direction_hint == "down":
        down_score += 1

    if volume_ratio >= 1.05:
        if up_score > down_score:
            up_score += 1
        elif down_score > up_score:
            down_score += 1

    # --- Fair value dimension (incremental; does not alter existing logic) ---
    # Recompute fair value with the actual tau from market end_date and the
    # UP-side market price, so we get a real edge_bps vs this specific market.
    fair_result: Dict[str, Any] = {
        "fair_up": 0.5,
        "fair_down": 0.5,
        "z_score": 0.0,
        "edge_bps_vs_market": None,
    }
    try:
        tau_sec_fair = None
        if end_date:
            try:
                tau_sec_fair = (iso_to_utc_dt(end_date) - now_utc).total_seconds()
            except Exception:
                tau_sec_fair = None
        if tau_sec_fair is None or tau_sec_fair <= 0:
            tau_sec_fair = FAIR_WINDOW_SEC

        ref_px_fair = safe_float(btc.get("ref_px")) or safe_float(btc.get("price")) or 0.0
        sigma_fair = safe_float(btc.get("sigma_15m"))
        if sigma_fair is None or sigma_fair <= 0:
            # Fallback: derive sigma from 15m range span percent if available
            span_pct = safe_float(btc.get("range_span_15m_pct"))
            sigma_fair = (span_pct / 100.0) if span_pct else FAIR_MIN_SIGMA
        if sigma_fair < FAIR_MIN_SIGMA:
            sigma_fair = FAIR_MIN_SIGMA

        market_price_fair = safe_float(up_outcome.get("best_ask"), up_outcome.get("price"))

        if ref_px_fair > 0:
            fair_result = compute_fair_updown(
                s_now=ref_px_fair,
                ref_px=ref_px_fair,  # baseline ref ≈ current (see BTC provider)
                sigma_15m=sigma_fair,
                tau_sec=tau_sec_fair,
                window_sec=FAIR_WINDOW_SEC,
                drift=0.0,
                market_price=market_price_fair,
            )
    except Exception as exc:
        logger.debug("fair_value_compute_in_rule_skipped: %s", exc)

    fair_up_v = float(fair_result.get("fair_up") or 0.5)
    fair_down_v = float(fair_result.get("fair_down") or 0.5)
    fair_z = safe_float(fair_result.get("z_score")) or 0.0
    fair_edge_bps = safe_float(fair_result.get("edge_bps_vs_market"))

    # Score impact: ±50bps edge threshold → ±2 to the corresponding direction.
    # edge_bps > 50 ⇒ market underprices UP ⇒ boost up_score.
    # edge_bps < -50 ⇒ market underprices DOWN ⇒ boost down_score.
    if fair_edge_bps is not None:
        if fair_edge_bps > 50:
            up_score += 2
            up_align += 1
        elif fair_edge_bps < -50:
            down_score += 2
            down_align += 1
        elif abs(fair_edge_bps) > 25:
            # Mild confirmation in the direction fair is leaning
            if fair_up_v > 0.55:
                up_align += 1
            elif fair_down_v > 0.55:
                down_align += 1

    price_edge = _market_price_edge(market) or 0.0
    if price_edge > 0.03:
        return None

    # === KRONOS 主信号 + momentum 校验模式 ===
    # 2026-06-23 backtest 显示 Kronos-mini 在 BTC 15min 窗口准确率 4/4=100% (vs momentum 5动量打分的 70%)
    # 主信号策略: Kronos direction 是主, momentum 当 sanity check, 不一致时降信心
    kronos_dir = btc.get("kronos_direction")
    kronos_conf = safe_float(btc.get("kronos_confidence")) or 0.0
    kronos_edge_bps_v = safe_float(btc.get("kronos_edge_bps"))
    kronos_loaded = bool(btc.get("kronos_loaded"))

    # Momentum 集合信号 (用 up_score/down_score 的差值, ±2 是显著)
    momentum_diff = up_score - down_score
    momentum_signal = (
        "UP" if momentum_diff >= 2 else
        "DOWN" if momentum_diff <= -2 else
        "FLAT"
    )

    # 1. Kronos 已加载 + 高信心 → 直接主导
    if kronos_loaded and kronos_conf >= 0.7 and kronos_dir in ("UP", "DOWN"):
        # 注意: 如果 momentum 是 FLAT, 不能选 FLAT 作为 dominant_label
        # 因为 downstream 代码用 dominant_label 选 outcome (up/down), FLAT 会导致错误
        if momentum_signal == "FLAT":
            # Kronos 有方向但 momentum 中性 → 用 Kronos 主导
            dominant_label = kronos_dir.lower()
            dominant_outcome = up_outcome if dominant_label == "up" else down_outcome
            dominant_ask = up_ask if dominant_label == "up" else down_ask
            dominant_bid = up_bid if dominant_label == "up" else down_bid
            dominant_score = max(up_score, down_score)
            dominant_align = max(up_align, down_align)
            confidence = max(0.55, kronos_conf - 0.05)
            source_label = "kronos_primary"
            direction_text = "向上" if dominant_label == "up" else "向下"
            reason = f"Kronos预测{direction_text} (confidence {kronos_conf:.0%}), momentum中性"
        elif kronos_dir == momentum_signal:
            # Kronos 和 momentum 一致 (或 momentum 中性) → 高信心 BUY
            dominant_label = kronos_dir.lower()
            dominant_outcome = up_outcome if dominant_label == "up" else down_outcome
            dominant_ask = up_ask if dominant_label == "up" else down_ask
            dominant_bid = up_bid if dominant_label == "up" else down_bid
            dominant_score = max(up_score, down_score)
            dominant_align = max(up_align, down_align)
            # 用 Kronos confidence 当 base
            confidence = max(0.55, kronos_conf - 0.05)  # 留点 buffer
            source_label = "kronos_primary"
            direction_text = "向上" if dominant_label == "up" else "向下"
            reason = f"Kronos预测{direction_text} (confidence {kronos_conf:.0%})"
        else:
            # Kronos 和 momentum 严重背离 → 降信心, 用 momentum
            dominant_label = momentum_signal.lower()
            dominant_outcome = up_outcome if dominant_label == "up" else down_outcome
            dominant_ask = up_ask if dominant_label == "up" else down_ask
            dominant_bid = up_bid if dominant_label == "up" else down_bid
            dominant_score = max(up_score, down_score)
            dominant_align = max(up_align, down_align)
            confidence = 0.45
            source_label = "momentum_fallback"
            direction_text = "向上" if dominant_label == "up" else "向下"
            reason = f"Kronos({kronos_dir})与momentum({momentum_signal})背离, 信任momentum"
    else:
        # 2. Kronos 未加载 / 信心不足 → fallback 到原 momentum 主导逻辑
        dominant_label = None
        dominant_outcome = None
        dominant_ask = None
        dominant_bid = None
        dominant_score = 0
        dominant_align = 0

        if up_score >= down_score + 2:
            dominant_label = "up"
            dominant_outcome = up_outcome
            dominant_ask = up_ask
            dominant_bid = up_bid
            dominant_score = up_score
            dominant_align = up_align
        elif down_score >= up_score + 2:
            dominant_label = "down"
            dominant_outcome = down_outcome
            dominant_ask = down_ask
            dominant_bid = down_bid
            dominant_score = down_score
            dominant_align = down_align

        if not dominant_outcome or dominant_ask is None or dominant_bid is None:
            return None
        if dominant_align < 2 or dominant_score < 5:
            return None

        confidence = 0.45
        source_label = "momentum_primary"
        direction_text = "向上" if dominant_label == "up" else "向下"
        reason = f"BTC短线动量{direction_text}一致，盘口仍接近50/50"

    spread = dominant_ask - dominant_bid
    if spread > Config.get_float("BTC_RULE_MAX_SPREAD", "0.03"):
        return None
    if dominant_ask > Config.get_float("BTC_RULE_MAX_ENTRY_PRICE", "0.56"):
        return None

    # Confidence 加成
    confidence += min(0.18, max(0.0, dominant_score - 5) * 0.04)
    confidence += min(0.06, max(0.0, 0.03 - price_edge))

    # Fair value confidence bonus: larger |edge_bps| → higher confidence
    # (capped at +0.05, so 500bps edge → full bonus).
    if fair_edge_bps is not None:
        confidence += min(0.05, max(0.0, abs(fair_edge_bps) / 10000.0))

    # Consistency bonus: when fair_z_score's sign agrees with the dominant
    # direction, add a small confidence bump.
    if dominant_label == "up" and fair_z > 0.3:
        confidence += 0.01
    elif dominant_label == "down" and fair_z < -0.3:
        confidence += 0.01

    confidence = min(confidence, 0.85)

    label = dominant_outcome.get("label", "Up" if dominant_label == "up" else "Down")
    return {
        "action": "BUY",
        "outcome_index": dominant_outcome.get("index"),
        "outcome_label": label,
        "confidence": round(confidence, 2),
        "reason": reason,
        "source": "btc_rule",
        "primary_source": source_label,
        # Expose fair value diagnostics for downstream consumers
        "fair_up": round(fair_up_v, 4),
        "fair_down": round(fair_down_v, 4),
        "fair_z_score": round(fair_z, 3),
        "fair_edge_bps": (round(fair_edge_bps, 2) if fair_edge_bps is not None else None),
        # Expose kronos diagnostics
        "kronos_direction": kronos_dir,
        "kronos_confidence": round(kronos_conf, 3) if kronos_loaded else None,
        "kronos_edge_bps": round(kronos_edge_bps_v, 2) if kronos_edge_bps_v is not None else None,
    }


def _has_arb_pair(slug: str, state: Dict) -> bool:
    return any(
        p.get("arbitrage_pair_id") and p.get("market_slug") == slug
        for p in state.get("positions", [])
    )



def _resolve_settlement_price(market: Dict[str, Any], outcome: Dict[str, Any],
                            signal: Optional[Dict[str, Any]] = None) -> float:
    """计算过期盘口的结算价.

    优先级:
    1) outcomePrices (Polymarket 官方结算数据, 最准确)
    2) signal.current_bid (兜底清理信号自带的估算)

    Bug fix 2026-07-01: redeem-aware 结算.
    历史问题: outcomePrices 返回的是 order book 上的当前 bid (maker 还在挂单),
    不是 redeem 的 1.0. 所以赢家实际拿到 1.0, 但 close_price 记成 0.81,
    漏算 ~19% 的 redeem 利润.
    修法: 如果市场已 settled (mte<0 或 closed=True) 且 outcomePrices[my_idx] >= 0.5,
    返回 1.0 (按 Polymarket redeem 规则, 赢家拿 1 USD per share).

    Bug fix 2026-06-27: 删除第 3 层 "outcome.price > 0.5 → 1.0" 的猜测逻辑.
    原因: 过期盘口的 outcome.price 是 stale 的最后一次成交价, 不是结算价.
    用 stale price 猜"赢了"会误判 (e.g. 过期前最后成交 0.49, 实际赢了 → 1.0).
    修正后: 没 outcomePrices 又没 signal 时返回 0.0 (兜底), 但调用方应该在
    下个 cycle 再试一次 (Polymarket settlement 通常 1-5 分钟就绪).
    """
    settlement = 0.0
    # 1) outcomePrices
    outcome_prices = market.get("outcomePrices")
    outcome_idx = outcome.get("index")
    if outcome_prices and isinstance(outcome_prices, (list, tuple)) and outcome_idx is not None:
        if outcome_idx < len(outcome_prices):
            try:
                sp = float(outcome_prices[outcome_idx])
                if 0.0 <= sp <= 1.0:
                    # Bug fix 2026-07-01: 赢家 redeem 到 1.0
                    if sp >= 0.5 and _market_settled(market):
                        settlement = 1.0
                    elif sp >= 0.5:
                        # Oracle 还没结算, outcomePrices 是 stale 最后成交价.
                        # 返回 0.0 让调用方走延迟结算 (pending_settle), 等 oracle.
                        settlement = 0.0
                    else:
                        settlement = sp
            except (ValueError, TypeError):
                pass
    # 2) signal.current_bid (低优兜底清理估算)
    if settlement == 0.0 and signal is not None:
        sig_bid = safe_float(signal.get("current_bid"))
        if sig_bid is not None and 0.0 <= sig_bid <= 1.0:
            settlement = sig_bid
    # 注: 不再基于 outcome.price 推断赢了/输了 — 过期盘口的 price 是 stale 数据
    return settlement


def _market_settled(market: Dict[str, Any]) -> bool:
    """判断 Polymarket 市场是否已 settle (oracle 已出结果).

    用于 redeem-aware 逻辑: 只有 oracle 确实结算了, 才把 outcomePrices 解释为
    "赢家/输家", 返回 1.0 (redeem). 如果 outcomePrices 仍是 stale 的最后成交价
    (两个价格都在 0.05~0.95 之间), 说明 oracle 还没结算, 返回 False.

    Oracle 结算后, 赢家价格一定是 1.0 (或接近 1.0), 输家是 0.0 (或接近 0.0).
    """
    # 1) 检查 outcomePrices 是否已经是 oracle 结算结果
    outcome_prices = market.get("outcomePrices")
    if outcome_prices and isinstance(outcome_prices, (list, tuple)):
        prices = []
        for p in outcome_prices:
            try:
                prices.append(float(p))
            except (ValueError, TypeError):
                return False
        if len(prices) >= 2:
            # 两个价格都在中间区间 → oracle 还没结算, 是 stale 最后成交价
            if all(0.05 < p < 0.95 for p in prices[:2]):
                return False
            # 至少有一个价格接近 1.0 → oracle 已结算
            if any(p >= 0.95 for p in prices[:2]):
                return True

    # 2) 兜底: 用 closed 标记判断 (Polymarket 官方标记)
    if market.get("closed"):
        return True
    # 没有 outcomePrices 就不能确认 oracle 已结算, 等下次重试
    return False


# Bug fix 2026-07-01: batch save 上下文管理器.
# 用法: `with _SaveBatchContext(state_manager):` 包住一个 trading cycle,
# 范围内的 state_manager.save() 只标 dirty 不写盘, 退出时 (即使异常)
# 自动 flush 一次. 嵌套安全 (count 计数).
class _SaveBatchContext:
    def __init__(self, state_manager: "StateManager"):
        self._sm = state_manager
        self._prev_defer = False
        self._is_outer = False  # 是否最外层

    def __enter__(self):
        self._prev_defer = self._sm._defer_save
        # 只有当进入前不在 batch 模式时, 我们才是最外层.
        self._is_outer = (not self._prev_defer)
        self._sm._defer_save = True
        return self._sm

    def __exit__(self, exc_type, exc, tb):
        # 只在最外层退出时 flush. 嵌套的内层只恢复 defer 状态, 不写盘.
        # 异常路径: 即使有异常, 外层退出时仍 flush (保留脏数据).
        try:
            if self._is_outer and self._sm._dirty:
                self._sm.flush()
        finally:
            self._sm._defer_save = self._prev_defer
        return False  # 不吞异常


class TradingBotManager:
    """Unified trading loop for paper/live execution."""

    def __init__(self):
        self.state_manager = StateManager(PAPER_STATE_FILE)
        self.market_api = PolymarketClient()
        self.btc_api = BTCDataprovider()
        self.ai_engine = AIDecisionEngine()
        self.reversal_engine = ReversalEngine()
        self.lowbuy_engine = LowBuyDoubleEngine()
        self.hedged_limit_engine = HedgedLimitEngine()
        # 2026-07-11: FV+Edge 策略接入.
        # 默认参数从 .env 的 FV_EDGE_POSITION_USD 读, 默认 2.0 USDC.
        # 完整 .env 开关见 docs/advanced_strategies.md.
        fv_edge_position_usd = Config.get_float(
            "FV_EDGE_POSITION_USD", str(FV_EDGE_DEFAULT_POSITION_USD),
        )
        self.fv_edge = FVEdgeStrategy(position_usd=fv_edge_position_usd)

        self.current_mode = Config.get("TRADING_MODE", "paper").lower()
        self.executor = self._create_executor(self.current_mode)
        self.running = True
        # 待结算仓位: slug -> {entry_price, shares, amount, top_up, ...}
        # TIME_STOP 时 outcomePrices 未就绪, 等下个 cycle 再试
        self._pending_settle: Dict[str, dict] = {}
        self._btc_window_refs: Dict[str, Dict[str, Any]] = load_json_file(self.BTC_WINDOW_REFS_FILE, {}) or {}
        self._last_btc_tick_price: Optional[float] = None
        self._last_btc_tick_write_ts: float = 0.0

    # ------------------------------------------------------------------
    # Bug fix 2026-07-01: batch save 上下文.
    # 单个 trading cycle 内多次 state_manager.save() 会写多个中间帧到 disk,
    # 前端 15s 轮询撞上中间帧 → trades/stats 数字在两个值之间跳 (99↔101).
    # 修法: run_cycle 入口打开 defer_save, cycle 末尾统一 flush 一次.
    # 内存里的 state 改动对其他读路径立即可见 (state 是同一 dict 引用),
    # 只合并 disk write. _open_lowbuy_position 内部 / _record_ai_history 里的
    # save 也会被 batch (因为它们内部用 self.state_manager.save()), 但
    # STATUS_FILE (bot_status.json) 走的是 StatusExporter, 不受影响.
    # ------------------------------------------------------------------
    def _save_batch(self):
        """上下文管理器: 范围内的 save() 合并到退出时一次性 flush."""
        sm = self.state_manager
        return _SaveBatchContext(sm)

    def _force_save(self):
        """绕过 batch 模式强制写盘 (用于关键的"现在必须落地"场景, 例如 START 边界 marker)."""
        self.state_manager.save(force=True)

    def _create_executor(self, mode: str):
        if mode == "live":
            logger.info("🚀 初始化实盘执行引擎 (Live Mode)")
            try:
                return LiveExecutor(self.state_manager)
            except (ValueError, Exception) as exc:
                # LiveExecutor 初始化失败（凭证不全/网络不可达/SDK 报错）
                # → 严格退回 paper，不静默进 dry_run 造成误判
                logger.error(
                    "❌ 实盘执行器初始化失败，自动回退 Paper 模式。原因: %s", exc,
                )
                logger.error("   请检查 .env 中的 POLYMARKET_PRIVATE_KEY / POLYMARKET_FUNDER_ADDRESS / 网络可达性。")
                # 同时强制 TRADING_MODE 退回 paper，避免下个周期再撞
                Config.invalidate()
                return PaperExecutor(self.state_manager)
        logger.info("🧪 初始化模拟执行引擎 (Paper Mode)")
        return PaperExecutor(self.state_manager)

    async def check_mode_swap(self):
        new_mode = Config.get("TRADING_MODE", "paper").lower()
        if new_mode != self.current_mode:
            logger.warning("🔄 检测到模式变更: %s -> %s", self.current_mode, new_mode)
            if self.current_mode.startswith("paper") and new_mode == "live":
                # paper → live: 丢弃 paper 状态
                logger.warning("🛡️ 模式切换安全响应: 丢弃模拟持仓")
                self.state_manager.update("positions", [])
                self.state_manager.update("orders", [])
            elif self.current_mode == "live" and new_mode.startswith("paper"):
                # live → paper: 归档实盘状态、重置 paper 现金，避免 paper 凭空多钱
                self._archive_live_to_paper()
            self.current_mode = new_mode
            self.executor = self._create_executor(new_mode)
            # 如果用户要求切 live 但被 _create_executor 回退到 paper，
            # 同步把 TRADING_MODE 也改回 paper，避免 config 与 executor 状态错位
            if new_mode == "live" and self.executor.mode != "live":
                logger.warning("🛡️ 配置强制为 TRADING_MODE=paper 以与 executor 保持一致")
                self._force_trading_mode("paper")

    def _archive_live_to_paper(self):
        """live → paper 时：归档实盘状态，重置 paper 现金余额。

        避免 paper 模式直接读到链上真实 USDC 当作起始资金。
        """
        state = self.state_manager.get_state()
        positions = state.get("positions", []) or []
        orders = state.get("orders", []) or []
        if positions or orders:
            archived = state.setdefault("archived_live_sessions", [])
            archived.insert(0, {
                "archived_at": datetime.now(timezone.utc).isoformat(),
                "positions": positions,
                "orders": orders,
                "cash_balance_at_archive": state.get("cash_balance"),
            })
            # 只保留最近 5 份归档，避免 state 文件无限增长
            state["archived_live_sessions"] = archived[:5]
            logger.warning(
                "🗃️ 已归档实盘状态: %d 持仓, %d 挂单 (cash=%.2f)。paper 模式将从 PAPER_START_BALANCE 重新开始。",
                len(positions), len(orders), state.get("cash_balance", 0.0),
            )
        state["positions"] = []
        state["orders"] = []
        state["cash_balance"] = Config.get_float("PAPER_START_BALANCE", "100")
        self.state_manager.save()

    def _force_trading_mode(self, mode: str):
        """运行时强制覆盖 TRADING_MODE（写入 trading_control.json，原子写入）"""
        try:
            control = load_json_file(CONTROL_FILE, {})
            control["TRADING_MODE"] = mode
            save_json_file(CONTROL_FILE, control)
            Config.invalidate()
        except Exception as exc:
            logger.error("写入 TRADING_MODE 失败: %s", exc)

    def _find_outcome(self, market: Dict[str, Any], position: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        for outcome in market.get("outcomes", []):
            if position.get("token_id") and position.get("token_id") == outcome.get("token_id"):
                return outcome
            if position.get("outcome_index") is not None and position.get("outcome_index") == outcome.get("index"):
                return outcome
        return None

    def _build_market_outcomes(self, market: Dict[str, Any]) -> List[Dict[str, Any]]:
        return [
            {
                "index": outcome.get("index"),
                "label": outcome.get("label"),
                "price": outcome.get("price"),
                "best_bid": outcome.get("best_bid"),
                "best_ask": outcome.get("best_ask"),
            }
            for outcome in market.get("outcomes", [])
        ]

    def _refresh_summary(self):
        state = self.state_manager.get_state()
        positions = state.get("positions", [])
        reserved_balance = round(sum(safe_float(pos.get("stake"), 0.0) or 0.0 for pos in positions), 4)
        unrealized_pnl = 0.0
        # Bug fix 2026-06-27: 用 set 去重, 防止 positions[] + trades[] 双源同时含同仓位时
        # 双重计数. LowBuy 开仓时同时写 positions 和 trades, 旧代码这里会把同一笔算两次.
        # 去重 key: "{slug}|{outcome_label}" (outcome_index 在 BUG-1 修后才有, 兜底用 label).
        counted_keys: set = set()
        for position in positions:
            shares = safe_float(position.get("shares"), safe_float(position.get("size"), 0.0)) or 0.0
            current_bid = safe_float(position.get("current_bid"), safe_float(position.get("entry_price"), 0.0)) or 0.0
            stake = safe_float(position.get("stake"), 0.0) or 0.0
            key = f"{position.get('market_slug', '')}|{position.get('outcome_label', '') or position.get('outcome_index', '')}"
            if key in counted_keys:
                continue
            counted_keys.add(key)
            unrealized_pnl += round(shares * current_bid - stake, 4)

        # trades[] + lowbuy_trades[] 的盈亏统计
        trades = state.get("lowbuy_trades", []) + state.get("trades", [])
        for t in trades:
            if t.get("status") != "OPEN":
                continue
            if t.get("strategy") == "arbitrage":  # 排除套利
                continue
            key = f"{t.get('market_slug', '')}|{t.get('outcome', '')}"
            if key in counted_keys:
                continue
            counted_keys.add(key)
            shares = safe_float(t.get("size"), 0.0) or 0.0
            entry_price = safe_float(t.get("price"), 0.0) or 0.0
            stake = safe_float(t.get("amount"), entry_price * shares) or 0.0
            # 没有 current_bid, 保守按 entry_price 算 (=0 unrealized_pnl, 不会高估)
            current_bid = entry_price
            unrealized_pnl += round(shares * current_bid - stake, 4)

        closed_trades = []
        for trade in trades:
            status = str(trade.get("status") or "").upper()
            if status == "OPEN":
                continue
            if status and status not in _CLOSED_TRADE_STATUSES and trade.get("closed_at") is None:
                continue
            closed_trades.append(trade)
        # 只算 SELL 侧 PnL, 避免 BUY+SELL 双重计数
        sell_closed = [t for t in closed_trades if t.get("side") == "SELL"]
        realized_pnl = round(sum(safe_float(t.get("realized_profit"), 0.0) or 0.0 for t in sell_closed), 4)
        total_trades = len(sell_closed)
        winning_trades = sum(1 for t in sell_closed if (safe_float(t.get("realized_profit"), 0.0) or 0.0) > 0)
        losing_trades = sum(1 for t in sell_closed if (safe_float(t.get("realized_profit"), 0.0) or 0.0) < 0)
        paper_start = Config.get_float("PAPER_START_BALANCE", "100")
        cash_balance = round(safe_float(state.get("cash_balance"), 0.0) or 0.0, 4)
        expected_cash_balance = round(paper_start + realized_pnl, 4)
        if not positions and abs(cash_balance - expected_cash_balance) >= 0.01:
            logger.warning(
                "纸面余额对账修复: cash_balance %.4f -> %.4f (start %.4f + realized %.4f)",
                cash_balance, expected_cash_balance, paper_start, realized_pnl,
            )
            cash_balance = expected_cash_balance
            state["cash_balance"] = cash_balance
        ending_balance = round(cash_balance + reserved_balance + unrealized_pnl, 4)
        win_rate = round((winning_trades / total_trades) * 100, 2) if total_trades else 0.0
        roi_percent = round(((ending_balance - paper_start) / paper_start) * 100, 2) if paper_start else 0.0
        state["stats"] = {
            "total_trades": total_trades,
            "winning_trades": winning_trades,
            "losing_trades": losing_trades,
            "total_profit": realized_pnl,
        }

        state["summary"] = {
            "cash_balance": cash_balance,
            "reserved_balance": reserved_balance,
            "ending_balance": ending_balance,
            "open_positions": len(positions),
            "realized_pnl": realized_pnl,
            "unrealized_pnl": round(unrealized_pnl, 4),
            "total_trades": total_trades,
            "winning_trades": winning_trades,
            "losing_trades": losing_trades,
            "win_rate": win_rate,
            "session_started_at": state.get("session_started_at"),
        }
        state["report"] = {
            "strategy": "Generic Binary V1.1",
            "profit": round(realized_pnl + unrealized_pnl, 4),
            "roi_percent": roi_percent,
            "result": "running",
            "session_started_at": state.get("session_started_at"),
        }
        self.state_manager.save()

    async def _load_markets_for_positions(self, current_market: Optional[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
        state = self.state_manager.get_state()
        slugs = {
            pos.get("market_slug")
            for pos in state.get("positions", [])
            if pos.get("status", "OPEN") in {"OPEN", "CLOSING"} and pos.get("market_slug")
        }
        slugs |= {
            order.get("market_slug")
            for order in state.get("orders", [])
            if order.get("status") in {"SUBMITTED", "OPEN", "PENDING", "PENDING_FILL", "PARTIAL_FILL"} and order.get("market_slug")
        }
        if current_market and current_market.get("slug"):
            slugs.add(current_market["slug"])

        async def _load(slug: str) -> Tuple[str, Optional[Dict[str, Any]]]:
            market = current_market if current_market and current_market.get("slug") == slug else await self.market_api.get_market(slug)
            if market:
                book = await self.market_api.get_microstructure(market)
                _merge_book_quotes(market, book)
            return slug, market

        markets: Dict[str, Dict[str, Any]] = {}
        if not slugs:
            return markets
        results = await asyncio.gather(*[_load(slug) for slug in slugs])
        for slug, market in results:
            if market:
                markets[slug] = market
        return markets


    def _should_close_position(
        self,
        now_utc: datetime,
        market: Dict[str, Any],
        position: Dict[str, Any],
        outcome: Dict[str, Any],
    ) -> Tuple[Optional[float], Optional[str]]:
        exit_price = safe_float(outcome.get("best_bid"), safe_float(outcome.get("price"), position.get("current_bid")))
        if exit_price is None or exit_price <= 0:
            return None, None

        end_date = market.get("end_date") or position.get("end_date")
        is_closed = bool(market.get("closed"))
        seconds_to_end: Optional[float] = None
        if end_date:
            try:
                seconds_to_end = (iso_to_utc_dt(end_date) - now_utc).total_seconds()
                is_closed = is_closed or seconds_to_end <= 0
            except Exception:
                pass
        # 兜底: 从 slug 时间戳算过期 (BTC 15m 窗口 slug 含开始时间)
        if not is_closed:
            slug_start = self._slug_start_dt(position.get("market_slug") or market.get("slug", ""))
            if slug_start:
                # BTC 15m 窗口持续 15 分钟
                slug_end = slug_start + timedelta(minutes=15)
                if now_utc >= slug_end:
                    is_closed = True

        if self.current_mode == "live":
            if is_closed:
                return None, None
            exit_before_expiry = Config.get_int("LIVE_EXIT_BEFORE_EXPIRY_SECONDS", "90")
            if seconds_to_end is not None and seconds_to_end <= exit_before_expiry:
                return exit_price, "EXPIRY_EXIT"
        elif is_closed:
            # 模拟盘: 过期按实际结果结算, 不按当前 bid
            settlement = _resolve_settlement_price(market, outcome)
            logger.info("⚖️ [结算] %s outcome=%s 过期结算价=%.1f¢",
                        market.get("slug","")[-16:], outcome.get("label","?"), settlement * 100)
            if settlement <= 0 and not _market_settled(market):
                # Oracle 尚未结算, 跳过本轮, 下个 cycle 再试
                logger.debug("或可结算: outcomePrices 未就绪, 跳过 (slug=%s)", market.get("slug","")[-16:])
                return None, None
            return settlement, "EXPIRY_EXIT"

        stake = safe_float(position.get("stake"), 0.0) or 0.0
        shares = safe_float(position.get("shares"), safe_float(position.get("size"), 0.0)) or 0.0
        pnl = shares * exit_price - stake

        # FVEdge 策略: 持有到到期, 不走 TP/SL 提前平仓
        if position.get("hold_to_expiry"):
            return None, None

        take_profit_usd = (
            stake * Config.get_float("TAKE_PROFIT_PERCENT", "0.18")
            if self.current_mode == "live"
            else Config.get_float("PAPER_TAKE_PROFIT_USD", "0.12")
        )
        if pnl >= take_profit_usd:
            return exit_price, "TAKE_PROFIT"

        # STOP_LOSS 关闭 (2026-06-23 用户决策):
        # 旧逻辑: pnl <= -stop_loss_usd → STOP_LOSS 平仓
        #   问题: 71% 触发率, 都是 -$1.34/笔, 累计 -$13.41 吞噬所有 TP 收益
        # 新逻辑: 不设固定止损, 让 PnL 自然走向 EXPIRY_EXIT / TAKE_PROFIT
        #   - 只有当价格被钉到接近 0 (跟窗口结果一致) 时,EXPIRY_EXIT 会处理
        #   - 正常的反向波动会随时间被均值回归救回来 (这正是 reversal 的 edge)
        # STOP_LOSS_ENABLED 配置保留但默认 false (用户随时可以重新开启)
        if Config.get_bool("STOP_LOSS_ENABLED", "false") and stake > 0:
            stop_loss_usd = stake * Config.get_float("STOP_LOSS_PERCENT", "0.10")
            if pnl <= -stop_loss_usd:
                return exit_price, "STOP_LOSS"

        return None, None

    async def _manage_open_positions(self, now_utc: datetime, current_market: Optional[Dict[str, Any]]) -> List[str]:
        markets = await self._load_markets_for_positions(current_market)
        close_messages = await self.executor.sync_state(markets, now_utc)
        state = self.state_manager.get_state()
        updated = False

        for position in list(state.get("positions", [])):
            if position.get("status", "OPEN") != "OPEN":
                continue
            # Bug fix 2026-06-26: 跳过 LOWBUY 仓位, 它有独立的 TP/TIME_STOP 机制
            # _manage_open_positions + executor.close_position 会插入幽灵 SELL trade,
            # 跟 LOWBUY _lowbuy_close 重复触发同一仓位, cash 被双倍记账.
            # LOWBUY 仓位由 _lowbuy_sync_positions + lowbuy_engine.scan 管理,
            # _check_take_profit + _stop_at_time 触发 _lowbuy_close 完成平仓.
            if position.get("strategy") == "lowbuy_double":
                continue
            market_slug = position.get("market_slug")
            market = markets.get(market_slug or "")
            if not market:
                continue
            outcome = self._find_outcome(market, position)
            if not outcome:
                continue

            position["current_bid"] = safe_float(outcome.get("best_bid"), position.get("current_bid"))
            position["current_ask"] = safe_float(outcome.get("best_ask"), position.get("current_ask"))
            updated = True

            if self.current_mode == "live":
                market_closed = bool(market.get("closed"))
                end_date = market.get("end_date") or position.get("end_date")
                if end_date:
                    try:
                        market_closed = market_closed or iso_to_utc_dt(end_date) <= now_utc
                    except Exception:
                        pass
                if market_closed:
                    position["status"] = "SETTLEMENT_PENDING"
                    continue

            exit_price, exit_reason = self._should_close_position(now_utc, market, position, outcome)
            if exit_price is None or not exit_reason:
                continue

            if self.current_mode == "live" and any(
                order.get("status") in {"SUBMITTED", "OPEN", "PENDING", "PENDING_FILL", "PARTIAL_FILL"}
                and order.get("side") == "SELL"
                and order.get("position_id") == position.get("id")
                for order in state.get("orders", [])
            ):
                continue

            message = await self.executor.close_position(position, exit_price, exit_reason)
            close_messages.append(message)

        if updated and not close_messages:
            self.state_manager.save()
        self._refresh_summary()
        return close_messages

    def _find_duplicate_exposure(self, market: Dict[str, Any]) -> Optional[str]:
        state = self.state_manager.get_state()
        market_slug = market.get("slug")
        # Bug fix 2026-06-25: 优先读 trades (source of truth), lowbuy_trades 兜底, positions 兜底
        for t in state.get("lowbuy_trades", []) + state.get("trades", []):
            if t.get("status") == "OPEN" and t.get("market_slug") == market_slug:
                return f"已有持仓 (trades): {t.get('outcome') or t.get('outcome_name')}"
        for position in state.get("positions", []):
            if position.get("status", "OPEN") == "OPEN" and position.get("market_slug") == market_slug:
                return f"已有持仓 (positions): {position.get('outcome_name') or position.get('outcome')}"

        active_statuses = {"SUBMITTED", "OPEN", "PENDING", "PENDING_FILL", "PARTIAL_FILL"}
        for order in state.get("orders", []):
            if order.get("status") in active_statuses and order.get("market_slug") == market_slug:
                end_date = order.get("end_date")
                if end_date:
                    try:
                        if iso_to_utc_dt(end_date) <= datetime.now(timezone.utc):
                            continue
                    except Exception:
                        pass
                return f"已有挂单 {order.get('outcome') or order.get('outcome_name')}"
        return None

    def _max_open_exposure(self) -> int:
        if self.current_mode == "live":
            return Config.get_int("LIVE_MAX_OPEN_POSITIONS", "2")
        # paper 默认 2: 1 给 reversal, 1 给 lowbuy_double
        return Config.get_int("PAPER_MAX_OPEN_POSITIONS", "2")

    def _open_exposure_count(self) -> int:
        state = self.state_manager.get_state()
        # Bug fix 2026-06-27: 真实 source-of-truth 是 trades[] (status==OPEN),
        # positions[] 可能含幽灵 (旧版 _lowbuy_close 没删). 用 trades 兜底, 避免
        # positions 脏数据导致永远撞 PAPER_MAX_OPEN_POSITIONS=2 上限.
        # 排除套利持仓（这些不计入方向判断的开仓限制）
        open_from_positions = sum(
            1 for pos in state.get("positions", [])
            if pos.get("status", "OPEN") in {"OPEN", "CLOSING"}
            and pos.get("strategy") != "arbitrage"
        )
        open_from_trades = sum(
            1 for t in state.get("lowbuy_trades", []) + state.get("trades", [])
            if t.get("status") == "OPEN"
            and t.get("strategy") != "arbitrage"
        )
        # 取最大值作为保守估计 (positions 多了不可信, trades 少了可能漏;
        # 用 max 是为了不漏开新仓位)
        open_positions = max(open_from_positions, open_from_trades)
        active_orders = sum(
            1 for order in state.get("orders", [])
            if order.get("status") in {"SUBMITTED", "OPEN", "PENDING", "PENDING_FILL", "PARTIAL_FILL"}
        )
        return open_positions + active_orders

    # ============================================================
    # LowBuy-Double 集成方法
    # ============================================================

    def _lowbuy_sync_positions(self):
        """从 state.json 同步 lowbuy 引擎的 open positions.

        启动时 / cycle 开始时调用, 保证 bot 重启后 engine 不会"忘记"
        哪些 lowbuy 仓位还在等翻倍 / 时间止损.

        Bug fix 2026-06-25: state.positions 一直空着, 真实仓位在 trades[]/lowbuy_trades[].status==OPEN.
        所以同步源必须用 trades + lowbuy_trades.
        """
        try:
            state = self.state_manager.get_state()
            current_slugs = set(self.lowbuy_engine.open_positions().keys())
            actual_slugs = set()

            # 1) 同步源: state.lowbuy_trades 里 status==OPEN 的 lowbuy 仓位 (跟主 trades 拆分)
            for t in state.get("lowbuy_trades", []) + state.get("trades", []):
                if (
                    t.get("strategy") == "lowbuy_double"
                    and t.get("status") == "OPEN"
                    and t.get("market_slug")
                ):
                    slug = t["market_slug"]
                    actual_slugs.add(slug)
                    if slug not in current_slugs:
                        # Bug fix 2026-06-27: trade_record 现在写入了 outcome_index (由 _open_lowbuy_position),
                        # 不再用硬编码 0 — 硬编码会让 Down 仓位 (oi=1) 被注册成 slug:0, 引擎监控错对象,
                        # TP 永不触发, 5 分钟兜底全亏. 兜底才用 0 (旧 trade_record 没字段).
                        outcome_index = t.get("outcome_index", 0)
                        entry = safe_float(t.get("price"), 0.0) or 0.0
                        # entry_time 用 created_at
                        entry_time_str = t.get("created_at")
                        try:
                            entry_time = datetime.fromisoformat(
                                entry_time_str.replace("Z", "+00:00")
                            ) if entry_time_str else datetime.now(timezone.utc)
                            if entry_time.tzinfo is None:
                                entry_time = entry_time.replace(tzinfo=timezone.utc)
                        except Exception:
                            entry_time = datetime.now(timezone.utc)
                        self.lowbuy_engine.register_entry(
                            slug,
                            outcome_index,
                            entry,
                            entry_time,
                            stake=safe_float(t.get("amount"), LOWBUY_POSITION_USD),
                        )
                        logger.info("🔄 [LowBuy/同步] 从 trades 恢复 OPEN 仓位: %s outcome=%d entry=%.1f¢",
                                    slug, outcome_index, entry * 100)
            # 同步: state.json 里没有的,从引擎移除 (已被平仓)
            # 注意: engine 的 key 是 "slug:outcome_index" 格式
            for stale_key in current_slugs:
                actual_slug_part = stale_key.rsplit(":", 1)[0]
                if actual_slug_part not in actual_slugs:
                    self.lowbuy_engine.close_position(stale_key)

            # Bug fix 2026-06-27: prune state.positions[] 里的 lowbuy 幽灵仓位
            # 现象: 旧版 _lowbuy_close 走 trades 直写路径 (manager.py:1067+), 不再删 positions[].
            # 结果: 累计 10 个 OPEN 状态 position (slug 跟 closed trades 完全一致),
            # _open_exposure_count() 把它当真, 触发 PAPER_MAX_OPEN_POSITIONS=2 上限,
            # 永远开不了新仓位. 这里同步清理: positions[] 里 strategy=lowbuy_double 且
            # trades[] 里没有对应 OPEN 的, 直接删.
            positions = state.get("positions", []) or []
            kept_positions = []
            pruned_count = 0
            for pos in positions:
                if pos.get("strategy") == "lowbuy_double":
                    pos_key = f"{pos.get('market_slug')}:{pos.get('outcome_index')}" if pos.get("outcome_index") is not None else None
                    if not pos_key or pos_key not in current_slugs:
                        pruned_count += 1
                        continue
                kept_positions.append(pos)
            if pruned_count:
                state["positions"] = kept_positions
                self.state_manager.save()
                logger.warning(
                    "🧹 [LowBuy/同步] prune %d 个幽灵仓位 (state.positions[] 里 lowbuy 但 trades 无对应 OPEN)",
                    pruned_count,
                )
        except Exception as exc:
            logger.debug("[LowBuy] sync_positions 失败: %s", exc)

    async def _execute_lowbuy_signal(
        self,
        signal: Dict[str, Any],
        snapshots: List[Dict[str, Any]],
        now_utc: datetime,
    ) -> Optional[str]:
        """执行一个 lowbuy 信号 (BUY / TAKE_PROFIT / TIME_STOP).

        返回简短日志字符串 (供 execution_summary 用), 失败返回 None.
        """
        action = signal.get("action", "")
        slug = signal.get("slug", "")
        if not slug:
            return None

        # 找对应的完整 market 对象 (从 snapshots 里)
        market = None
        for snap in snapshots:
            if snap.get("slug") == slug:
                market = snap
                break
        if not market:
            # 过期窗口不在 snapshots 列表里
            # Bug fix 2026-06-27: 优先从 API 拉真实盘口数据 (带 outcomePrices 结算价).
            try:
                expired_market = await self.market_api.get_market(slug)
                if expired_market and expired_market.get("outcomes"):
                    market = expired_market
                    logger.info("[LowBuy] TIME_STOP 从 API 拉取过期盘口 %s (outcomePrices=%s)",
                                slug, bool(expired_market.get("outcomePrices")))
            except Exception as exc:
                logger.debug("[LowBuy] get_market(%s) 失败: %s", slug, exc)

            if not market:
                # API 也拿不到, 构造伪 market (无 outcomePrices, 兜底)
                reason = signal.get("reason", "")
                if "[LowBuy] 兜底清理" in reason or action == "TIME_STOP":
                    # 用 signal 数据构造伪 market + outcome
                    fake_outcome = {
                        "index": signal.get("outcome_index", 0),
                        "label": signal.get("outcome_label", "?"),
                        "best_bid": signal.get("current_bid", 0),
                        "best_ask": signal.get("current_bid", 0),
                    }
                    market = {
                        "slug": slug,
                        "end_date": "",
                        "outcomes": [fake_outcome],
                    }
                    outcome = fake_outcome
                    action = signal.get("action", "")
                    if action == "TIME_STOP":
                        return await self._lowbuy_close(market, outcome, signal, "TIME_STOP", now_utc)
                logger.debug("[LowBuy] 找不到 slug=%s 的 market", slug)
                return None

        outcomes = market.get("outcomes") or []
        outcome_index = signal.get("outcome_index")
        if not isinstance(outcome_index, int) or outcome_index >= len(outcomes):
            return None
        outcome = outcomes[outcome_index]

        if action == "BUY":
            return await self._lowbuy_open(market, outcome, signal, now_utc)
        elif action == "TAKE_PROFIT":
            return await self._lowbuy_close(market, outcome, signal, "TP", now_utc)
        elif action == "TIME_STOP":
            return await self._lowbuy_close(market, outcome, signal, "TIME_STOP", now_utc)
        return None

    async def _lowbuy_open(
        self,
        market: Dict[str, Any],
        outcome: Dict[str, Any],
        signal: Dict[str, Any],
        now_utc: datetime,
    ) -> Optional[str]:
        """lowbuy / fv_edge 入场 (BUY).

        通用 BUY 路径: 同时服务于 LowBuyDoubleEngine 和 FVEdgeStrategy.
        两种策略共享:
          - 仓位簿 (state.json)
          - 每 slug 一仓去重 (trades 检查)
          - audit log
        区别:
          - LowBuy 走 LOWBUY_POSITION_USD 仓位 + 内部 TP/TIME_STOP 监控
          - fv_edge 走 signal["stake"] 仓位 (默认 2.0 USDC) + 持有到期
            → register_entry 跳过 (避免 LowBuy TP 接管)
            → entry price 区间检查放宽到 fv_edge 的 [0.10, 0.85]
              (scan 里已经过滤, 这里双重保险)
        """
        # 选择仓位大小: fv_edge 用自己的 stake, lowbuy 用 LOWBUY_POSITION_USD
        signal_source = (signal or {}).get("source") or "lowbuy_double"
        if signal_source == "fv_edge":
            stake = float(signal.get("stake") or LOWBUY_POSITION_USD)
            if stake <= 0:
                stake = LOWBUY_POSITION_USD
        else:
            stake = LOWBUY_POSITION_USD
        # Bug fix 2026-06-27: 只用 best_ask 做入场价, 别回退到 price (gamma mid).
        # price 常比 best_ask 高 3-5¢, 导致以为 32¢ 入场实际 38¢ 成交, 严重超出 MAX_ENTRY=35¢.
        ask_price = safe_float(outcome.get("best_ask"))
        if not ask_price or ask_price <= 0:
            return None

        # 再次校验入场价在策略允许的区间内.
        #   LowBuy: 严格 [LOWBUY_MIN_ENTRY, LOWBUY_MAX_ENTRY] = [0.30, 0.36]
        #   fv_edge: 宽松 [0.10, 0.85] (scan 里已经过滤, 这里只是兜底)
        if signal_source == "fv_edge":
            entry_floor = 0.10
            entry_ceiling = 0.85
        else:
            entry_floor = LOWBUY_MIN_ENTRY
            entry_ceiling = LOWBUY_MAX_ENTRY
        if ask_price < entry_floor or ask_price > entry_ceiling:
            logger.warning(
                "[%s] 入场价 %.2f¢ 超出区间 [%.0f,%.0f]¢, 拒单",
                signal_source, ask_price * 100, entry_floor * 100, entry_ceiling * 100,
            )
            return None

        # 检查是否有重复 exposure: 每个 slug 是唯一的 15min 窗口
        # 只要在该 slug 上有过交易记录 (任何状态), 就不要再开第二笔
        # 2026-06-25 fix: 读 trades 而不是 positions; 拦所有状态, 不只是 OPEN
        # 2026-07-11 fix: 改用 (slug, strategy) 作为去重 key, 允许 fv_edge 和
        # lowbuy_double 在不同窗口共存 (例: lowbuy 中段先入场, fv_edge 末段不
        # 再覆盖同窗口 — 这反而是想要的, 末段信号不应该破坏中段仓位)
        state = self.state_manager.get_state()
        slug = market.get("slug", "")
        for t in state.get("lowbuy_trades", []) + state.get("trades", []):
            if (
                t.get("market_slug") == slug
                and t.get("strategy") == signal_source
            ):
                logger.info("[%s] 跳过: %s slug=%s 已有过交易（每窗口一仓）",
                            signal_source, outcome.get("label"), slug[-14:])
                return None

        # 构造一个合成 signal 给 executor 用
        synth_signal = {
            "action": "BUY",
            "outcome_index": outcome.get("index", signal.get("outcome_index")),
            "outcome_label": outcome.get("label", signal.get("outcome_label", "")),
            "confidence": signal.get("confidence", 0.70),
            "reason": signal.get("reason", ""),
            "source": "lowbuy_double",
        }
        quote = {
            "token_id": outcome.get("token_id"),
            "label": outcome.get("label"),
            "outcome_index": outcome.get("index"),
            "best_bid": outcome.get("best_bid"),
            "best_ask": outcome.get("best_ask"),
        }

        if self.executor.mode == "live":
            live_result = await self._open_lowbuy_live_position(
                market, outcome, ask_price, stake, signal, now_utc,
            )
            if live_result:
                return live_result
            return None

        # Paper LowBuy 继续走本地 state 写入路径。
        try:
            position = await self._open_lowbuy_position(
                market, outcome, ask_price, stake, signal, now_utc,
            )
            if position:
                # 写入策略标识, _lowbuy_sync_positions 才能识别
                # 2026-07-11 fix: 不能硬编码 "lowbuy_double" — fv_edge 仓位需要保留
                # 自己的 strategy 标签, 否则会被 _lowbuy_sync_positions 当幽灵清掉,
                # 但 trade dict 还在, 导致后续 _manage_open_positions / EXPIRY_EXIT
                # 找不到对应 position, 单子永远卡 OPEN.
                position["strategy"] = signal_source
                position["source"] = signal_source
                position["entry_price"] = ask_price
                position["entry_time"] = now_utc.isoformat()
                position["outcome_index"] = outcome.get("index", signal.get("outcome_index"))
                # 保存
                self.state_manager.save()

                hold_to_expiry = signal.get("hold_to_expiry", False)
                # 双边仓位: 注册 TP 监控,哪侧先翻倍就一起平
                # 2026-07-11: fv_edge 走 hold_to_expiry 路径, 不进 LowBuy TP/TIME_STOP 池
                #   (last 2 min 没空间跑 TP, 直接等 EXPIRY_EXIT)
                if signal_source == "lowbuy_double":
                    self.lowbuy_engine.register_entry(
                        market.get("slug", ""),
                        outcome.get("index", signal.get("outcome_index")),
                        ask_price,
                        now_utc,
                        stake=stake,
                    )
                # Bug fix 2026-06-26: 审计 log 记录开仓 + 当时盘口
                state = self.state_manager.get_state()
                shares_count = stake / ask_price if ask_price > 0 else 0
                _append_position_audit(self.POSITION_AUDIT_FILE, {
                    "t": now_utc.isoformat(),
                    "action": "OPEN",
                    "slug": market.get("slug", ""),
                    "outcome_index": outcome.get("index"),
                    "outcome_label": outcome.get("label", ""),
                    "entry_price": ask_price,
                    "shares": round(shares_count, 4),
                    "amount": stake,
                    "bid_at_open": outcome.get("best_bid"),
                    "ask_at_open": outcome.get("best_ask"),
                    "tp_target": position.get("tp_target", ask_price * LOWBUY_TP_MULT),
                    "reason": signal.get("reason", ""),
                    "entry_mte": signal.get("entry_mte"),
                    "opposite_ask": signal.get("opposite_ask"),
                    "direction_hint": signal.get("direction_hint"),
                    "obi": signal.get("obi"),
                    "recent_ask_drop": signal.get("recent_ask_drop"),
                    "hold_to_expiry": hold_to_expiry,
                    "cash_after": state.get("cash_balance", 0),
                })
                pos_label = "双边无风险" if hold_to_expiry else ""
                # tp_target 从 engine.register_entry 里拿 (engine 里算的是 entry_price * LOWBUY_TP_MULT)
                tp_target = position.get("tp_target", ask_price * LOWBUY_TP_MULT)
                logger.info(
                    "%s [LowBuy] 开仓: %s @ %.1f¢, stake=$%.2f, TP=%.1f¢",
                    "🟢" if hold_to_expiry else "💰",
                    outcome.get("label"), ask_price * 100, stake, tp_target * 100,
                )
                return f"LowBuy 开仓 {outcome.get('label')} @ {ask_price*100:.1f}¢ (${stake})"
        except Exception as exc:
            logger.error("[LowBuy] 开仓失败: %s", exc)
            return f"LowBuy 开仓失败: {exc}"
        return None

    async def _open_lowbuy_live_position(
        self,
        market: Dict[str, Any],
        outcome: Dict[str, Any],
        signal_ask: float,
        stake_usd: float,
        signal: Dict[str, Any],
        now_utc: datetime,
    ) -> Optional[str]:
        """Live LowBuy entry: re-check top-of-book, then submit a strict limit order."""
        outcome_index = outcome.get("index", signal.get("outcome_index"))
        if not isinstance(outcome_index, int):
            logger.warning("[LowBuy/live] outcome_index 无效, 拒单")
            return None

        fresh_market = market
        try:
            fetched = await self.market_api.get_market(market.get("slug", ""))
            if fetched and fetched.get("outcomes"):
                fresh_market = fetched
                try:
                    book = await self.market_api.get_microstructure(fresh_market)
                    _merge_book_quotes(fresh_market, book)
                except Exception as exc:
                    logger.warning("[LowBuy/live] 最新盘口深度拉取失败: %s", exc)
        except Exception as exc:
            logger.warning("[LowBuy/live] 最新盘口复核失败: %s", exc)

        fresh_outcomes = fresh_market.get("outcomes") or []
        if outcome_index >= len(fresh_outcomes):
            logger.warning("[LowBuy/live] 最新盘口缺少 outcome_index=%s, 拒单", outcome_index)
            return None

        fresh_outcome = fresh_outcomes[outcome_index]
        fresh_ask = safe_float(fresh_outcome.get("best_ask"))
        if fresh_ask is None or fresh_ask <= 0:
            logger.warning("[LowBuy/live] 最新 ask 缺失, 拒单")
            return None
        if fresh_ask < LOWBUY_MIN_ENTRY or fresh_ask > LOWBUY_MAX_ENTRY:
            logger.warning(
                "[LowBuy/live] 最新 ask %.1f¢ 超出 LowBuy 区间 [%.0f,%.0f]¢, 拒单",
                fresh_ask * 100, LOWBUY_MIN_ENTRY * 100, LOWBUY_MAX_ENTRY * 100,
            )
            return None

        max_allowed = min(signal_ask + LOWBUY_LIVE_MAX_ASK_DRIFT, signal_ask * (1 + LOWBUY_LIVE_MAX_ASK_DRIFT_PCT))
        if fresh_ask > max_allowed:
            logger.warning(
                "[LowBuy/live] ask 漂移过大: signal=%.1f¢ fresh=%.1f¢ max=%.1f¢, 拒单",
                signal_ask * 100, fresh_ask * 100, max_allowed * 100,
            )
            return None

        quote = {
            "token_id": fresh_outcome.get("token_id"),
            "label": fresh_outcome.get("label"),
            "outcome_index": fresh_outcome.get("index"),
            "best_bid": fresh_outcome.get("best_bid"),
            "best_ask": fresh_outcome.get("best_ask"),
        }
        live_signal = {
            "action": "BUY",
            "outcome_index": fresh_outcome.get("index", outcome_index),
            "outcome_label": fresh_outcome.get("label", signal.get("outcome_label", "")),
            "confidence": signal.get("confidence", 0.70),
            "reason": signal.get("reason", ""),
            "source": "lowbuy_double",
            "stake": stake_usd,
        }
        result = await self.executor.open_position(
            fresh_market,
            live_signal,
            fresh_ask,
            fresh_outcome.get("label", signal.get("outcome_label", f"Outcome {outcome_index}")),
            quote,
        )
        if "成功" not in result:
            logger.warning("[LowBuy/live] 下单失败: %s", result)
            return result

        self.lowbuy_engine.register_entry(
            fresh_market.get("slug", ""),
            fresh_outcome.get("index", outcome_index),
            fresh_ask,
            now_utc,
            stake=stake_usd,
        )
        _append_position_audit(self.POSITION_AUDIT_FILE, {
            "t": now_utc.isoformat(),
            "action": "LIVE_ORDER_SUBMITTED",
            "slug": fresh_market.get("slug", ""),
            "outcome_index": fresh_outcome.get("index"),
            "outcome_label": fresh_outcome.get("label", ""),
            "signal_ask": signal_ask,
            "limit_price": fresh_ask,
            "amount": stake_usd,
            "bid_at_open": fresh_outcome.get("best_bid"),
            "ask_at_open": fresh_outcome.get("best_ask"),
            "reason": signal.get("reason", ""),
        })
        logger.info(
            "🛡️ [LowBuy/live] 限价单提交: %s signal=%.1f¢ limit=%.1f¢ stake=$%.2f",
            fresh_outcome.get("label"), signal_ask * 100, fresh_ask * 100, stake_usd,
        )
        return f"LowBuy 实盘限价单 {fresh_outcome.get('label')} @ {fresh_ask*100:.1f}¢ (${stake_usd})"

    async def _lowbuy_close(
        self,
        market: Dict[str, Any],
        outcome: Dict[str, Any],
        signal: Dict[str, Any],
        reason_label: str,
        now_utc: datetime,
    ) -> Optional[str]:
        """lowbuy 平仓 (TP 或 TIME_STOP)."""
        slug = market.get("slug", "")
        close_reason_code = signal.get("close_reason_code") or ("take_profit" if reason_label == "TP" else "")
        close_reason_label = signal.get("close_reason_label") or ("止盈" if reason_label == "TP" else "")
        bid_price = safe_float(outcome.get("best_bid"), outcome.get("price"))
        if bid_price is None:
            bid_price = 0.0
        # 注意: 兜底清理的过期盘口 bid=0, 这是真实的"全损"语义, 不要 return
        # 之前用 `if not bid_price or bid_price <= 0: return None` 会让兜底永远不执行
        # 改成: 走真实 trade 路径, 用 entry price 估算 pnl

        # 找 state.json 里 lowbuy 的 open 仓位 (从 lowbuy_trades 找, 跟主流水拆分)
        # Bug fix 2026-06-25: 读 trades 而不是 positions (trades 才是真相).
        state = self.state_manager.get_state()
        position = None
        for t in state.get("lowbuy_trades", []) + state.get("trades", []):
            if (
                t.get("market_slug") == slug
                and t.get("strategy") == "lowbuy_double"
                and t.get("status") == "OPEN"
            ):
                position = t
                break
        # 兜底: 旧 state 可能 positions 里有 (兼容老格式)
        if not position:
            for pos in state.get("positions", []):
                if (
                    pos.get("market_slug") == slug
                    and pos.get("strategy") == "lowbuy_double"
                    and pos.get("status", "OPEN") in {"OPEN", "CLOSING"}
                ):
                    position = pos
                    break

        if not position:
            logger.debug("[LowBuy] 平仓请求: state.json 里没找到对应仓位 (%s)", slug)
            # 引擎自己清掉 (防止僵尸)
            self.lowbuy_engine.close_position(slug)
            # 同时把 lowbuy_trades 里匹配的 OPEN 标为过期 (兜底兜底, 防止僵尸)
            for t in state.get("lowbuy_trades", []) + state.get("trades", []):
                if (t.get("market_slug") == slug
                    and t.get("status") == "OPEN"
                    and t.get("strategy") == "lowbuy_double"):
                    t["status"] = reason_label  # TIME_STOP or TAKE_PROFIT
                    t["closed_at"] = datetime.now(timezone.utc).isoformat()
                    if close_reason_code:
                        t["close_reason_code"] = close_reason_code
                    if close_reason_label:
                        t["close_reason_label"] = close_reason_label
                    entry_p = safe_float(t.get("price"), 0.0) or 0.0
                    sz = safe_float(t.get("size"), 0.0) or 0.0
                    t["realized_profit"] = (bid_price - entry_p) * sz
                    logger.info("💸 [LowBuy/兜底平仓] %s @ %.1f¢ (entry=%.1f¢, pnl=$%.3f)",
                                slug, bid_price * 100, entry_p * 100, t.get("realized_profit", 0))
                    # Bug fix 2026-06-26: 审计 log (兜底路径, position 未找到)
                    _append_position_audit(self.POSITION_AUDIT_FILE, {
                        "t": datetime.now(timezone.utc).isoformat(),
                        "action": f"FALLBACK_CLOSE_{reason_label}",
                        "slug": slug,
                        "entry_price": entry_p,
                        "close_price": bid_price,
                        "shares": sz,
                        "pnl": t.get("realized_profit", 0),
                        "reason": "兜底清理: position 未找到, 直接更新 trades",
                        "cash_after": state.get("cash_balance", 0),
                    })
            self.state_manager.save()
            return None

        # 检查 position 是从 trades 来的(用 size/amount) 还是从 state.positions 来的(用 shares/stake)
        # Bug fix 2026-06-25: trades 没有 shares/stake 字段, executor.close_position 会 KeyError.
        is_trade_record = "size" in position and "shares" not in position

        # Bug fix 2026-06-27: pending_settle 位置由 _retry_settlements 处理, 跳过 TIME_STOP 路径
        if reason_label == "TIME_STOP" and position.get("pending_settle"):
            return None

        if is_trade_record and reason_label == "TIME_STOP" and bid_price == 0.0:
            # 兜底清理: 过期盘口 bid=0.
            # Bug fix 2026-06-26: 先检查 outcomePrices 做真实结算,
            # 而不是一律按 bid=0 全损.
            entry_price = safe_float(position.get("price"), 0.0) or 0.0
            shares = safe_float(position.get("size"), 0.0) or 0.0
            bid_price = _resolve_settlement_price(market, outcome, signal)

            # Bug fix 2026-06-27: outcomePrices 未就绪 → 延迟结算
            if bid_price == 0.0 and not market.get("outcomePrices"):
                position["pending_settle"] = True
                self.state_manager.save()
                logger.info("[LowBuy] TIME_STOP 延迟结算: outcomePrices 未就绪 (%s), 下个 cycle 重试", slug)
                self._pending_settle[slug] = {
                    "entry_price": entry_price,
                    "shares": shares,
                    "amount": safe_float(position.get("amount"), entry_price * shares) or 0.0,
                }
                return f"LowBuy 延迟结算: 等待 outcomePrices ({slug})"

            pnl = (bid_price - entry_price) * shares
            position["status"] = reason_label
            position["closed_at"] = datetime.now(timezone.utc).isoformat()
            position["realized_profit"] = round(pnl, 4)
            position["close_price"] = bid_price
            if close_reason_code:
                position["close_reason_code"] = close_reason_code
            if close_reason_label:
                position["close_reason_label"] = close_reason_label
            # 从引擎追踪移除
            self.lowbuy_engine.close_position(slug, outcome.get("index"))
            # 退回现金
            amount = safe_float(position.get("amount"), entry_price * shares) or 0.0
            current_cash = safe_float(state.get("cash_balance"), 0.0) or 0.0
            state["cash_balance"] = round(current_cash + amount + pnl, 4)
            # 更新统计
            stats = state.setdefault("stats", {"total_trades": 0, "winning_trades": 0, "losing_trades": 0, "total_profit": 0.0})
            stats["total_trades"] = stats.get("total_trades", 0) + 1
            stats["total_profit"] = round(stats.get("total_profit", 0.0) + pnl, 4)
            if pnl >= 0:
                stats["winning_trades"] = stats.get("winning_trades", 0) + 1
            else:
                stats["losing_trades"] = stats.get("losing_trades", 0) + 1
            logger.info("💸 [LowBuy/%s-兜底] 平仓: %s @ %.1f¢ (entry=%.1f¢, pnl=$%.3f, cash=$%.2f)",
                        reason_label, outcome.get("label"), bid_price * 100,
                        entry_price * 100, pnl, state["cash_balance"])
            # Bug fix 2026-06-26: 审计 log (TIME_STOP 兜底)
            bid_used = market.get("outcomePrices") or signal.get("current_bid")
            reason_detail = f"TIME_STOP 兜底 (settle=%.1f¢, pnl=${pnl:+.4f})" % (bid_price * 100)
            if bid_used:
                reason_detail = f"TIME_STOP 兜底 (结算价=%.1f¢, pnl=${pnl:+.4f})" % (bid_price * 100)
            _append_position_audit(self.POSITION_AUDIT_FILE, {
                "t": datetime.now(timezone.utc).isoformat(),
                "action": f"TIMEOUT_CLOSE_{reason_label}",
                "slug": slug,
                "entry_price": entry_price,
                "close_price": bid_price,
                "shares": shares,
                "amount": amount,
                "pnl": round(pnl, 4),
                "tp_target": position.get("tp_target", entry_price * LOWBUY_TP_MULT),
                "reason": reason_detail,
                "cash_after": state["cash_balance"],
            })
            self.state_manager.save()
            return f"LowBuy 兜底清理 {outcome.get('label')} @ {bid_price*100:.1f}¢ (pnl=${pnl:+.3f})"

        # Bug fix 2026-06-25: trades 用 size/amount, executor 用 shares/stake.
        # 干脆绕开 executor, 直接在 trades 上标记 closed + 算 pnl + 改 cash.
        # (executor 会 insert 一条新 SELL trade, 跟原 OPEN trade 重复)
        is_trade_record = "size" in position and "shares" not in position
        if is_trade_record and reason_label in ("TIME_STOP", "TP"):
            entry_price = safe_float(position.get("price"), 0.0) or 0.0
            original_shares = safe_float(position.get("size"), 0.0) or 0.0

            # Bug fix 2026-06-26: 限价单模拟
            # 实盘限价单: 实际成交 = min(挂单shares, bid价位上的depth)
            # 没成交部分保留仓位, 等下一次扫描
            # TIME_STOP 走市价单 (强制成交, 接受滑点)
            if reason_label == "TP":
                # 从 signal 里读之前 _check_take_profit 计算的 depth
                fillable_shares = original_shares  # fallback
                if signal:
                    # Bug fix 2026-06-27: 检查 depth > 0 而非 is not None, 否则薄盘口
                    # (bid 达阈值但 size=0) 会让 fillable_shares=0, 后续 shares*0=0 啥也没干,
                    # 但仓位改 status=OPEN 留着 → 卡死.
                    depth = signal.get("depth_at_bid")
                    if depth is not None and float(depth) > 0:
                        fillable_shares = min(original_shares, float(depth))
                # 部分成交
                shares = fillable_shares
                logger.info("[LowBuy/TP-limit-order] 限价模拟: 我们的 %.2f shares, depth=%.2f, 成交 %.2f",
                            original_shares, signal.get("depth_at_bid", 0) if signal else 0, shares)
            else:
                # TIME_STOP: 市价单, 整笔成交
                shares = original_shares

            pnl = (bid_price - entry_price) * shares
            position["status"] = reason_label
            position["closed_at"] = datetime.now(timezone.utc).isoformat()
            position["realized_profit"] = round(pnl, 4)
            position["close_price"] = bid_price
            if close_reason_code:
                position["close_reason_code"] = close_reason_code
            if close_reason_label:
                position["close_reason_label"] = close_reason_label
            # Bug fix 2026-06-26: 初始化 amount, 下面 if/else 都可能用到
            amount = safe_float(position.get("amount"), entry_price * shares) or 0.0

            # Bug fix 2026-06-26: 部分成交逻辑
            # 如果成交份额 < 原始份额 (限价单薄盘口), 不能立刻标记完全 closed
            unfilled = original_shares - shares
            if unfilled > 0.01 and reason_label == "TP":
                # 部分成交: 保留剩余仓位, 等下一次扫描
                # 同步到 trades[]: 把仓位 size 改成剩余 unfilled
                # trade 状态保持 OPEN (因为还有未平份额)
                position["status"] = "OPEN"
                position["size"] = round(unfilled, 4)
                position.pop("closed_at", None)
                position.pop("realized_profit", None)
                position.pop("close_price", None)
                # 同步到 trades / lowbuy_trades 列表
                trade_record = next((t for t in state.get("lowbuy_trades", []) + state.get("trades", [])
                                     if t.get("market_slug") == slug
                                     and t.get("status") == "OPEN"
                                     and t.get("strategy") == "lowbuy_double"), None)
                if trade_record:
                    trade_record["size"] = round(unfilled, 4)
                    trade_record["amount"] = round(unfilled * entry_price, 4)
                    # 记一个部分成交事件, 但保留 OPEN 状态
                    trade_record.setdefault("partial_fills", []).append({
                        "t": datetime.now(timezone.utc).isoformat(),
                        "filled_shares": round(shares, 4),
                        "fill_price": bid_price,
                        "fill_pnl": round(pnl, 4),
                    })
                logger.info("[LowBuy/TP-partial] 部分成交 %.2f/%.2f shares @ %.1f¢, 剩余 %.2f shares 继续挂单",
                            shares, original_shares, bid_price * 100, unfilled)
                # 不退 amount, 重新计算正确的 amount (基于剩余 unfilled)
                remaining_amount = round(unfilled * entry_price, 4)
                current_cash = safe_float(state.get("cash_balance"), 0.0) or 0.0
                # 退回已成交部分的卖出收入。开仓时现金已经扣了全额本金,
                # 且上面已经把剩余仓位 amount 改成 unfilled * entry_price;
                # 这里只加 PnL 会永久吞掉 filled_shares 的本金。
                state["cash_balance"] = round(current_cash + shares * bid_price, 4)
                # 不把仓位从 state.positions 删除, 也不要完全 closed
                # 但需要把 TP 累计时间清掉, 让下次重新计时
                pos_record = self.lowbuy_engine._open_positions.get(f"{slug}:{outcome.get('index')}")
                if pos_record is not None:
                    pos_record["tp_first_hit_at"] = None
                    pos_record["entry_price"] = entry_price  # 剩余仓位 entry 还是原值
                    # 调整 stake 到剩余
                    pos_record["stake"] = remaining_amount
            else:
                # 完整成交 (或 TIME_STOP): 标记 closed, 移除追踪, 退 amount + pnl
                self.lowbuy_engine.close_position(slug, outcome.get("index"))
                current_cash = safe_float(state.get("cash_balance"), 0.0) or 0.0
                state["cash_balance"] = round(current_cash + amount + pnl, 4)
                # 更新统计
                stats = state.setdefault("stats", {"total_trades": 0, "winning_trades": 0, "losing_trades": 0, "total_profit": 0.0})
                stats["total_trades"] = stats.get("total_trades", 0) + 1
                stats["total_profit"] = round(stats.get("total_profit", 0.0) + pnl, 4)
                if pnl >= 0:
                    stats["winning_trades"] = stats.get("winning_trades", 0) + 1
                else:
                    stats["losing_trades"] = stats.get("losing_trades", 0) + 1
                # 把 state.positions 里同名仓位也删掉
                state["positions"] = [p for p in state.get("positions", []) if p.get("market_slug") != slug]
            logger.info("💸 [LowBuy/%s-trade] 平仓: %s @ %.1f¢ (entry=%.1f¢, pnl=$%.3f, cash=$%.2f)",
                        reason_label, outcome.get("label"), bid_price * 100,
                        entry_price * 100, pnl, state["cash_balance"])
            # Bug fix 2026-06-26: 审计 log 记录平仓 + 当时盘口
            _append_position_audit(self.POSITION_AUDIT_FILE, {
                "t": datetime.now(timezone.utc).isoformat(),
                "action": f"CLOSE_{reason_label}",
                "slug": slug,
                "outcome_index": outcome.get("index"),
                "outcome_label": outcome.get("label", ""),
                "entry_price": entry_price,
                "close_price": bid_price,
                "shares": shares,
                "amount": amount,
                "pnl": round(pnl, 4),
                "bid_at_close": outcome.get("best_bid"),
                "ask_at_close": outcome.get("best_ask"),
                "tp_target": position.get("tp_target", entry_price * LOWBUY_TP_MULT),
                "tp_hit": reason_label == "TP",
                "reason": f"入仓 {entry_price*100:.1f}¢ → 平仓 {bid_price*100:.1f}¢ × {shares:.4f}股",
                "cash_after": state["cash_balance"],
            })
            self.state_manager.save()
            return f"LowBuy {reason_label} {outcome.get('label')} @ {bid_price*100:.1f}¢ (pnl=${pnl:+.3f})"

        # 老 path: 走 executor (适用于 state.positions 真有 shares/stake 字段的旧格式)
        # Bug fix 2026-06-27: 永远不该走到这里. trades 直写路径 (上面 if is_trade_record 分支)
        # 已经覆盖所有 _open_lowbuy_position 写入的仓位. 如果走这里, 说明
        # 1) 有外部代码往 state.positions 写 "老格式" 仓位 (没人这么做了)
        # 2) 或者是 trades 格式变了导致上面 is_trade_record 误判
        # 直接 raise 让 bug 暴露, 而不是静默走老 path (老 path 会让 executor 再插一条 SELL,
        # 双重记账). 历史的 state.positions 幽灵会被 _lowbuy_sync_positions prune 掉.
        raise RuntimeError(
            f"[_lowbuy_close] 不该走老 executor path: "
            f"slug={slug} outcome_index={outcome.get('index')} "
            f"is_trade_record={is_trade_record} reason={reason_label}. "
            f"检查 _open_lowbuy_position 是否还在写 trades[].size, 以及 is_trade_record 判断是否失效."
        )

    async def _open_lowbuy_position(
        self,
        market: Dict[str, Any],
        outcome: Dict[str, Any],
        entry_price: float,
        stake_usd: float,
        signal: Dict[str, Any],
        now_utc: datetime,
    ) -> Optional[Dict[str, Any]]:
        """底层: 在 state.json 写一条 lowbuy 仓位.

        跟 executor.open_position 不同,这里走最简路径(避免重复
        duplicate-exposure 检查造成循环),手动构造 position dict.
        返回新 position dict (已经 add 到 state.positions 但未 save).
        """
        try:
            # 构造 position
            shares = round(stake_usd / entry_price, 4) if entry_price > 0 else 0
            if shares <= 0:
                logger.warning("[LowBuy] shares<=0: stake=$%.2f entry=%.2f", stake_usd, entry_price)
                return None

            # 2026-07-11: 跟随 signal.source 决定 strategy 标签, 让 fv_edge
            # 仓能被 frontend / audit 正确识别. 兜底用 lowbuy_double 保持
            # 向后兼容 (旧 signal 调用路径不带 source).
            strategy_tag = (signal or {}).get("source") or "lowbuy_double"
            if strategy_tag not in ("lowbuy_double", "fv_edge"):
                strategy_tag = "lowbuy_double"

            position = {
                "id": f"LOWBUY-{int(_time.time() * 1000)}",
                "market_slug": market.get("slug", ""),
                "market_question": market.get("question", ""),
                "token_id": outcome.get("token_id"),
                "outcome_label": outcome.get("label", ""),
                "outcome_index": outcome.get("index"),
                "side": "BUY",
                "status": "OPEN",
                "shares": shares,
                "stake": stake_usd,
                "entry_price": entry_price,
                "entry_time": now_utc.isoformat(),
                "tp_target": round(entry_price * LOWBUY_TP_MULT, 6),
                "current_bid": entry_price,
                "strategy": strategy_tag,
                "source": strategy_tag,
                "ai_reason": signal.get("reason", ""),
                "ai_confidence": signal.get("confidence", 0.7),
                "entry_mte": signal.get("entry_mte"),
                "entry_bid": signal.get("entry_bid"),
                "entry_ask": signal.get("entry_ask"),
                "opposite_ask": signal.get("opposite_ask"),
                "direction_hint": signal.get("direction_hint"),
                "obi": signal.get("obi"),
                "recent_ask_drop": signal.get("recent_ask_drop"),
                "book_observed_at": signal.get("book_observed_at"),
                "book_fetch_latency_ms": signal.get("book_fetch_latency_ms"),
                "btc_captured_at": signal.get("btc_captured_at"),
                "btc_fetched_at": signal.get("btc_fetched_at"),
                "btc_cache_age_secs": signal.get("btc_cache_age_secs"),
                "code_version": "v2",
                "hold_to_expiry": strategy_tag == "fv_edge",
            }

            # 扣现金前做余额防御。LowBuy 走直写 state 路径, 不经过 PaperExecutor.open_position()
            # 的资金检查；如果长期跑到 cash < stake, 旧代码会继续开仓导致 paper cash 变负。
            state = self.state_manager.get_state()
            cash = safe_float(state.get("cash_balance"), 0.0) or 0.0
            if cash < stake_usd:
                logger.warning("[LowBuy] 资金不足, 跳过开仓: cash=$%.2f < stake=$%.2f", cash, stake_usd)
                return None
            state["cash_balance"] = round(cash - stake_usd, 4)

            positions = state.setdefault("positions", [])
            positions.append(position)

            # 追加到对应流水: fv_edge 进主 trades, lowbuy 进 lowbuy_trades
            if strategy_tag == "fv_edge":
                trades = state.setdefault("trades", [])
            else:
                trades = state.setdefault("lowbuy_trades", [])
            # 用 position 里已有的 tp_target (如果有), 否则算 entry_price * LOWBUY_TP_MULT
            tp_target = position.get("tp_target", entry_price * LOWBUY_TP_MULT)
            trade_record = {
                "id": f"trade-buy-{position['id']}",
                "created_at": now_utc.isoformat(),
                "side": "BUY",
                "outcome": position["outcome_label"],
                "outcome_index": position.get("outcome_index"),  # Bug fix 2026-06-27: 必须写入, _lowbuy_sync_positions 用它恢复引擎 key
                "market": position["market_question"],
                "market_slug": position["market_slug"],
                "amount": stake_usd,
                "size": shares,
                "price": entry_price,
                "status": "OPEN",
                "tp_target": tp_target,
                "reason": signal.get("reason") or f"[LowBuy] 震荡入场, TP={tp_target * 100:.1f}¢",
                "strategy": strategy_tag,
                "source": strategy_tag,
                "entry_mte": signal.get("entry_mte"),
                "entry_bid": signal.get("entry_bid"),
                "entry_ask": signal.get("entry_ask"),
                "opposite_ask": signal.get("opposite_ask"),
                "direction_hint": signal.get("direction_hint"),
                "obi": signal.get("obi"),
                "recent_ask_drop": signal.get("recent_ask_drop"),
                "book_observed_at": signal.get("book_observed_at"),
                "book_fetch_latency_ms": signal.get("book_fetch_latency_ms"),
                "btc_captured_at": signal.get("btc_captured_at"),
                "btc_fetched_at": signal.get("btc_fetched_at"),
                "btc_cache_age_secs": signal.get("btc_cache_age_secs"),
                "code_version": "v2",
                "hold_to_expiry": strategy_tag == "fv_edge",
            }
            trades.append(trade_record)

            logger.info(
                "📝 [LowBuy] state.json 写入: id=%s, slug=%s, shares=%.2f, stake=$%.2f",
                position["id"][:20], position["market_slug"], shares, stake_usd,
            )
            return position
        except Exception as exc:
            logger.error("[LowBuy] _open_lowbuy_position 失败: %s", exc)
            return None

    # ============================================================

    def _record_ai_history(
        self,
        now_utc: datetime,
        market: Optional[Dict[str, Any]],
        action: str,
        confidence: float,
        reason: str,
        execution_summary: str,
        selected_label: Optional[str],
    ):
        state = self.state_manager.get_state()
        market_outcomes = self._build_market_outcomes(market) if market else []
        history = state.setdefault("ai_history", [])
        history.insert(0, {
            "decision_id": f"LOCAL-{now_utc.strftime('%Y%m%d-%H%M%S')}",
            "generated_at": now_utc.isoformat(),
            "action": action,
            "decision": action,
            "prediction": action,
            "confidence": confidence,
            "model": Config.get("AI_MODEL", "gpt-4o-mini"),
            "reasoning": reason,
            "thought_markdown": reason,
            "key_factors": [
                f"市场: {market.get('question', '--') if market else '--'}",
                f"选择结果: {selected_label or '--'}",
                "盘口: " + (" | ".join(
                    f"[{item.get('index')}] {item.get('label')} @ {item.get('price', '--')}" for item in market_outcomes
                ) if market_outcomes else "--"),
            ],
            "risk_flags": [],
            "execution_summary": execution_summary,
            "focus_market": market.get("question", "") if market else "",
        })
        state["ai_history"] = history[:20]
        self.state_manager.save()

    async def _retry_settlements(self, now_utc: datetime) -> None:
        """重试 pending_settle 仓位的结算.

        每个 run_cycle 调用一次, 检查 outcomePrices 是否已就绪. 不能只依赖
        self._pending_settle: bot 重启后内存会清空, 但 state.trades[] 里仍然有
        pending_settle=true 的 OPEN 仓位。这里每轮从 state 重新 hydrate, 防止
        待结算仓位永久卡 OPEN。
        """
        state = self.state_manager.get_state()
        for t in state.get("lowbuy_trades", []) + state.get("trades", []):
            if (
                t.get("status") == "OPEN"
                and t.get("pending_settle")
                and t.get("market_slug")
            ):
                slug = t["market_slug"]
                entry_price = safe_float(t.get("price"), 0.0) or 0.0
                shares = safe_float(t.get("size"), 0.0) or 0.0
                self._pending_settle.setdefault(slug, {
                    "entry_price": entry_price,
                    "shares": shares,
                    "amount": safe_float(t.get("amount"), entry_price * shares) or 0.0,
                })

        if not self._pending_settle:
            return
        settled_slugs = []
        for slug, info in list(self._pending_settle.items()):
            try:
                market = await self.market_api.get_market(slug)
                if not market or not market.get("outcomes"):
                    continue
                outcome_prices = market.get("outcomePrices")
                if not outcome_prices:
                    continue
                # 找到对应的 outcome 和仓位
                for t in state.get("lowbuy_trades", []) + state.get("trades", []):
                    if t.get("market_slug") == slug and t.get("status") == "OPEN" and t.get("pending_settle"):
                        outcome_idx = t.get("outcome_index")
                        outcomes = market.get("outcomes", [])
                        if outcome_idx is not None and outcome_idx < len(outcomes):
                            entry_price = safe_float(t.get("price"), 0.0) or 0.0
                            shares = safe_float(t.get("size"), 0.0) or 0.0
# 用 outcomePrices 结算
                            bid_price = float(outcome_prices[outcome_idx]) if outcome_idx < len(outcome_prices) else 0.0
                            # Bug fix 2026-07-01: redeem-aware — 赢家按 1.0 USD/share 结算
                            if bid_price >= 0.5 and _market_settled(market):
                                bid_price = 1.0
                            pnl = (bid_price - entry_price) * shares
                            t["status"] = "TIME_STOP"
                            t["closed_at"] = now_utc.isoformat()
                            t["realized_profit"] = round(pnl, 4)
                            t["close_price"] = bid_price
                            t.pop("pending_settle", None)
                            # 从引擎追踪移除 (防止继续生成 TIME_STOP)
                            self.lowbuy_engine.close_position(slug, outcome_idx)
                            # 退回现金
                            amount = safe_float(t.get("amount"), entry_price * shares) or 0.0
                            current_cash = safe_float(state.get("cash_balance"), 0.0) or 0.0
                            state["cash_balance"] = round(current_cash + amount + pnl, 4)
                            # 更新统计
                            stats = state.setdefault("stats", {"total_trades": 0, "winning_trades": 0, "losing_trades": 0, "total_profit": 0.0})
                            stats["total_trades"] = stats.get("total_trades", 0) + 1
                            stats["total_profit"] = round(stats.get("total_profit", 0.0) + pnl, 4)
                            if pnl >= 0:
                                stats["winning_trades"] = stats.get("winning_trades", 0) + 1
                            else:
                                stats["losing_trades"] = stats.get("losing_trades", 0) + 1
                            logger.info("🔄 [LowBuy/结算重试] %s: outcomePrices=%.4f, entry=%.1f¢, pnl=$%.3f, cash=$%.2f",
                                        slug, bid_price, entry_price * 100, pnl, state["cash_balance"])
                            settled_slugs.append(slug)
            except Exception as exc:
                logger.debug("[LowBuy] 结算重试失败 (%s): %s", slug, exc)
        # 清理已结算
        for slug in settled_slugs:
            self._pending_settle.pop(slug, None)
        if settled_slugs:
            self.state_manager.save()

    async def run_cycle(self):
        # Bug fix 2026-07-01: 用 _SaveBatchContext 包住整个 cycle,
        # cycle 内所有 state_manager.save() 合并到 cycle 末尾一次性写盘.
        # 修前端 trades 数量在 99↔101 之间跳的问题 (写中间帧被前端 15s 轮询抓到).
        # 异常路径也 flush (保留脏数据), 但不吞异常.
        with _SaveBatchContext(self.state_manager):
            await self._run_cycle_impl()

    async def _run_cycle_impl(self):
        await self.check_mode_swap()
        control = load_trading_control(CONTROL_FILE)
        is_enabled = control.get("trading_enabled", False)
        now_utc = datetime.now(timezone.utc)

        # Bug fix 2026-06-27: 重试待结算仓位 (等 outcomePrices)
        await self._retry_settlements(now_utc)

        base_status = {
            "running": True,
            "last_update": now_utc.isoformat(),
            "trading_mode": self.current_mode,
            "trading_enabled": is_enabled,
            "strategy_profile": Config.get("STRATEGY_PROFILE", "generic_binary"),
        }

        focus_market = await self.market_api.get_focus_market(now_utc)
        if focus_market and (not focus_market.get("binary") and not Config.get_bool("ALLOW_MULTI_OUTCOME", "false")):
            logger.warning("⚠️ 当前版本仅支持二元盘口: %s", focus_market.get("slug"))
            StatusExporter.export({
                **base_status,
                "market_slug": focus_market.get("slug", ""),
                "market_question": focus_market.get("question", ""),
                "market_end_date": focus_market.get("end_date", ""),
                "market_error": "当前版本仅支持二元盘口",
            })
            return

        close_messages = await self._manage_open_positions(now_utc, focus_market)

        if not is_enabled:
            logger.info("⏸ 交易已在控制台关闭，本轮跳过执行")
            execution_summary = "交易关闭" + (f"；{len(close_messages)} 笔持仓已处理" if close_messages else "")
            StatusExporter.export({**base_status, "execution_summary": execution_summary})
            return

        if not focus_market:
            selection_mode = Config.get("MARKET_SELECTION_MODE", "manual").strip().lower()
            if selection_mode in {"auto_btc_15m", "auto_btc_5m"}:
                market_error = "BTC 自动选盘失败"
                logger.warning("⚠️ %s，请检查 BTC 滚动市场是否开放", market_error)
            else:
                market_error = "未配置或无法解析目标市场"
                logger.warning("⚠️ 未找到目标盘口，请先配置 TARGET_MARKET_SLUG / TARGET_MARKET_URL")
            StatusExporter.export({
                **base_status,
                "market_error": market_error,
                "execution_summary": "未执行",
            })
            return

        # --- 扫全部 BTC 15m 窗口找套利机会，不分主次 ---
        market_selection_mode = Config.get("MARKET_SELECTION_MODE", "manual").strip().lower()
        auto_btc = market_selection_mode in {"auto_btc_15m", "auto_btc_5m"}
        snapshots: List[Dict[str, Any]] = []  # 提前初始化, 给 LowBuy 复用
        if auto_btc:
            snapshots = await self.market_api.get_market_snapshots(now_utc)
            # 2026-06-28: 停用旧盘口时间序列采集器.
            # data/market_ticks.jsonl 已积累足够样本; 后续改用离线 replay_lowbuy_current_params.py
            # 对既有数据做更贴近真实 bot 的 ask-entry 回放, 不再每轮写入 tick 文件.
            # 设置当前跟踪的 BTC 15m 窗口（选最近结束的那个）
            best_slug = None
            best_end: Optional[datetime] = None
            for snap in snapshots:
                slug = snap.get("slug", "")
                end_date = iso_to_utc_dt(snap.get("end_date", ""))
                if slug and end_date:
                    if best_slug is None or (best_end is not None and end_date < best_end):
                        best_slug = slug
                        best_end = end_date
            if best_slug and best_end:
                pass  # reversal 已关闭, 不再设置窗口

        book = await self.market_api.get_microstructure(focus_market)
        _merge_book_quotes(focus_market, book)
        extra_context = ""
        rule_signal: Optional[Dict[str, Any]] = None
        is_btc_15m = str(focus_market.get("slug", "")).startswith("btc-updown-15m-")
        btc = None
        if is_btc_15m:
            # 2026-06-23 优化: BTC signal API 在韩国网络下经常卡/超时.
            # 3s 硬超时 — 失败就 None, 不阻塞 cycle (LowBuy 也不再用 BTC).
            try:
                btc = await asyncio.wait_for(
                    self.btc_api.get_signal_context(),
                    timeout=3.0,
                )
            except (asyncio.TimeoutError, Exception) as exc:
                logger.debug("BTC signal API timeout/error: %s", exc)
                btc = None
            if btc:
                range_position = (safe_float(btc.get("range_position_15m"), 0.5) or 0.5) * 100
                extra_context = (
                    f"BTC 参考行情: 当前价格 ${btc['price']:.2f}，24h 涨跌 {btc['change_24h']:+.2f}% "
                    f"(来源: {btc.get('source', 'unknown')})。\n"
                    f"短线动量: 1m {btc.get('change_1m', 0):+.3f}% | 3m {btc.get('change_3m', 0):+.3f}% | "
                    f"5m {btc.get('change_5m', 0):+.3f}% | 15m {btc.get('change_15m', 0):+.3f}%。\n"
                    f"15m 区间: low ${btc.get('range_low_15m', '--')} -> high ${btc.get('range_high_15m', '--')}，"
                    f"当前位于区间 {range_position:.0f}% 位置，"
                    f"近5m/前5m 量比 {btc.get('volume_ratio_5m', 1.0):.2f}，方向提示 {btc.get('direction_hint', 'flat')}。\n"
                    "对于 BTC 15m 盘口，请优先依据 1m/3m/5m 动量是否同向、15m 是否配合、以及盘口定价是否仍接近 50/50 来决定是否存在可做多或做空的短线 edge。"
                )
                rule_signal = _build_btc_rule_signal(focus_market, btc, now_utc)

        # --- K线反转检测已关闭 (2026-06-25 用户决策: 从不触发, 省资源) ---
        signal = {"action": "SKIP", "confidence": 0.0, "reason": "反转策略已关闭"}
        logger.info("⏸️ [反转] 策略已关闭，SKIP")

        action = str(signal.get("action", "SKIP")).upper() if signal else "SKIP"
        confidence = safe_float(signal.get("confidence"), 0.0) if signal else 0.0
        reason = signal.get("reason", "AI 调用失败") if signal else "AI 调用失败"
        outcome_index = signal.get("outcome_index") if signal else None
        if not isinstance(outcome_index, int):
            outcome_index = None

        logger.info("💡 AI 决策: %s outcome=%s (%.0f%%) | %s", action, outcome_index, confidence * 100, reason)

        execution_summary = "未执行"
        chosen_outcome: Optional[Dict[str, Any]] = None
        min_confidence = Config.get_float("AI_MIN_CONFIDENCE", "0.6")
        if is_btc_15m:
            min_confidence = Config.get_float("BTC_AI_MIN_CONFIDENCE", str(min(min_confidence, 0.45)))

        duplicate_reason = self._find_duplicate_exposure(focus_market)

        if duplicate_reason:
            reason = f"阻止重复开仓：{duplicate_reason}"
            execution_summary = reason
        elif self._open_exposure_count() >= self._max_open_exposure():
            action = "SKIP"
            reason = "已达到最大开仓数量限制"
            execution_summary = reason
        elif action == "BUY" and confidence >= min_confidence and outcome_index is not None:
            outcomes = focus_market.get("outcomes", [])
            if 0 <= outcome_index < len(outcomes):
                chosen_outcome = outcomes[outcome_index]
                entry_price = safe_float(chosen_outcome.get("best_ask"), chosen_outcome.get("price"))
                if entry_price and entry_price > 0:
                    # 中性区间过滤 (2026-06-23 加入):
                    # 0.30-0.70 价格区间 polymarket 接近 50/50, edge 弱, 71% 止损触发率都来自这里
                    # 仅在 [PAPER_MIN_ENTRY_PRICE, 0.30) ∪ (0.70, PAPER_MAX_ENTRY_PRICE] 入场
                    # 例外: source="lowbuy_double" 不受此限 — LowBuy 故意买 ≤15¢ 反转方
                    #   (中性区间过滤会把它误伤, 跳过这部分)
                    is_lowbuy = (signal or {}).get("source") == "lowbuy_double"
                    if not is_lowbuy:
                        neutral_low = Config.get_float("PAPER_NEUTRAL_SKIP_LOW", "0.30")
                        neutral_high = Config.get_float("PAPER_NEUTRAL_SKIP_HIGH", "0.70")
                        min_entry = Config.get_float("PAPER_MIN_ENTRY_PRICE", "0.15")
                        max_entry = Config.get_float("PAPER_MAX_ENTRY_PRICE", "0.60")
                        if (
                            neutral_low <= entry_price <= neutral_high
                            or entry_price < min_entry
                            or entry_price > max_entry
                        ):
                            action = "SKIP"
                            reason = (
                                f"中性区间或边界过滤: entry={entry_price*100:.1f}¢ "
                                f"(避开 {neutral_low*100:.0f}-{neutral_high*100:.0f}¢ 中性区间, "
                                f"边界 {min_entry*100:.0f}-{max_entry*100:.0f}¢)"
                            )
                            execution_summary = reason
                            logger.info("⏭️ %s", reason)
                            # 直接跳过, 不进入下面的 open_position 分支
                        else:
                            quote = {
                                "token_id": chosen_outcome.get("token_id"),
                                "label": chosen_outcome.get("label"),
                                "outcome_index": chosen_outcome.get("index"),
                                "best_bid": chosen_outcome.get("best_bid"),
                                "best_ask": chosen_outcome.get("best_ask"),
                            }
                            execution_summary = await self.executor.open_position(
                                focus_market,
                                signal,
                                entry_price,
                                chosen_outcome.get("label", f"Outcome {outcome_index}"),
                                quote,
                            )
                            if "成功" not in execution_summary:
                                action = "SKIP"
                                reason = execution_summary
                                chosen_outcome = None
                            self._refresh_summary()
                    else:
                        # LowBuy 路径 — 自己的过滤在 _check_entry 已做 (price <= 0.15),
                        # 这里直接开仓
                        quote = {
                            "token_id": chosen_outcome.get("token_id"),
                            "label": chosen_outcome.get("label"),
                            "outcome_index": chosen_outcome.get("index"),
                            "best_bid": chosen_outcome.get("best_bid"),
                            "best_ask": chosen_outcome.get("best_ask"),
                        }
                        execution_summary = await self.executor.open_position(
                            focus_market,
                            signal,
                            entry_price,
                            chosen_outcome.get("label", f"Outcome {outcome_index}"),
                            quote,
                        )
                        if "成功" not in execution_summary:
                            action = "SKIP"
                            reason = execution_summary
                            chosen_outcome = None
                        self._refresh_summary()
                else:
                    action = "SKIP"
                    reason = "目标 outcome 缺少有效价格"
                    execution_summary = reason
            else:
                action = "SKIP"
                reason = "AI 返回的 outcome_index 超出范围"
                execution_summary = reason
        elif action == "BUY":
            action = "SKIP"
            reason = "未达到最小信心阈值或缺少 outcome_index"
            execution_summary = reason
        else:
            execution_summary = "AI 选择观望"

        market_outcomes = self._build_market_outcomes(focus_market)
        selected_index = chosen_outcome.get("index") if action == "BUY" and chosen_outcome else None
        selected_label = chosen_outcome.get("label") if action == "BUY" and chosen_outcome else None
        if close_messages:
            execution_summary = "；".join(close_messages + ([execution_summary] if execution_summary else []))

        self._record_ai_history(
            now_utc=now_utc,
            market=focus_market,
            action=action,
            confidence=confidence,
            reason=reason,
            execution_summary=execution_summary,
            selected_label=selected_label,
        )

        # ============================================================
        # Hedged-Limit 阶段 — 5m 双侧等份 47¢ 限价对冲
        # ============================================================
        hedge_messages: List[str] = []
        hedge_snapshots = snapshots if snapshots else ([focus_market] if focus_market else [])
        hedge_enabled = Config.get_bool("HEDGE_LIMIT_ENABLED", "false")
        if hedge_enabled and hedge_snapshots:
            hedge_prefix = str(Config.get("HEDGE_MARKET_PREFIX", "") or "").strip()

            def _hedge_candidate(snap: Dict[str, Any]) -> bool:
                slug = str(snap.get("slug", ""))
                if hedge_prefix:
                    return slug.startswith(hedge_prefix)
                return "btc-updown-" in slug

            hedge_candidates = [s for s in hedge_snapshots if _hedge_candidate(s)]
            if hedge_candidates:
                async def _fetch_hedge_book(snap):
                    try:
                        book = await self.market_api.get_microstructure(snap)
                        _merge_book_quotes(snap, book)
                    except Exception as exc:
                        logger.debug("[Hedge] microstructure 拉取失败 (%s): %s", snap.get("slug"), exc)

                await asyncio.gather(*[_fetch_hedge_book(s) for s in hedge_candidates])
                try:
                    hedge_messages = self.hedged_limit_engine.apply(
                        hedge_candidates,
                        self.state_manager.get_state(),
                        now_utc,
                        mode=self.current_mode,
                    )
                    if hedge_messages:
                        self.state_manager.save()
                        execution_summary = (
                            execution_summary + " | " + "；".join(hedge_messages)
                            if execution_summary and execution_summary != "未执行"
                            else "；".join(hedge_messages)
                        )
                        self._refresh_summary()
                except Exception as exc:
                    logger.error("[Hedge] 执行失败: %s", exc)
                    hedge_messages.append(f"Hedge 异常: {exc}")

        # ============================================================
        # LowBuy-Double 阶段 — 中段低买翻倍 (跟 reversal 并行)
        # ============================================================
        lowbuy_enabled = Config.get_bool("lowbuy_enabled", "true")
        if lowbuy_enabled:
            # 先同步 lowbuy 引擎的 open positions (从 state.json 重新读,
            # 这样 bot 重启 / state 漂移后能自愈)
            self._lowbuy_sync_positions()

        # 拉所有 BTC 15m 窗口的最新盘口 (lowbuy 可能看上非 focus_market 的窗口)
        snapshots_for_lowbuy = []
        if auto_btc and snapshots:
            snapshots_for_lowbuy = snapshots
        else:
            # manual 模式也要扫所有 BTC 15m
            try:
                snapshots_for_lowbuy = await self.market_api.get_market_snapshots(now_utc)
            except Exception as exc:
                logger.debug("[LowBuy] snapshot 拉取失败: %s", exc)

        # 给每个 snapshot 拉一次 microstructure (best_bid/best_ask)
        # 2026-06-23 优化: 并发拉取, 5 窗口从 ~50s 降到 ~10s (单次请求 ~10s 受网络限制)
        btc_snaps = [s for s in snapshots_for_lowbuy if s.get("slug", "").startswith("btc-updown-15m-")]
        if btc_snaps:
            async def _fetch_one(snap):
                try:
                    book = await self.market_api.get_microstructure(snap)
                    _merge_book_quotes(snap, book)
                except Exception as exc:
                    logger.debug("[LowBuy] microstructure 拉取失败 (%s): %s", snap.get("slug"), exc)

            await asyncio.gather(*[_fetch_one(s) for s in btc_snaps])
            # 2026-06-28: 停用旧双边机会 observer.
            # data/dualbuy_opportunities.jsonl 已验证长期 0 个 strict candidate;
            # 后续如需复盘可读既有 JSONL, 不再每轮追加写入.

        # ============================================================
        # FV+Edge 阶段 — 末段 (≤2min) 期权纯 edge 入场
        # ============================================================
        # 2026-07-11: 接入 FV+Edge 策略. 仅在窗口剩 ≤ FV_EDGE_MAX_MTE 分钟时
        # 才发信号, 信号走 _execute_lowbuy_signal 同一管道 (复用 lowbuy 的
        # 去重 + audit + 结算逻辑). 走 fv_edge 的仓会被标识 strategy="fv_edge"
        # + hold_to_expiry=True, 因此不会被 LowBuy 的 TP/TIME_STOP 监控接管.
        fv_edge_messages: List[str] = []
        fv_edge_signals: List[Dict[str, Any]] = []
        fv_edge_enabled = Config.get_bool("FV_EDGE_ENABLED", "true")
        fv_edge_allow_live = Config.get_bool("FV_EDGE_ALLOW_LIVE", "false")
        if (
            fv_edge_enabled
            and (self.current_mode != "live" or fv_edge_allow_live)
            and btc_snaps
        ):
            try:
                # 喂 BTC 当前价 + sigma + 窗口 ref_px 缓存
                self.fv_edge.update_btc_snapshot(
                    btc or {}, window_refs=self._btc_window_refs,
                )
                fv_edge_signals = self.fv_edge.scan(btc_snaps, now_utc)
            except Exception as exc:
                logger.exception("[FVEdge] scan 失败: %s", exc)
                fv_edge_signals = []

            # 按 slug 去重, 避免跟 lowbuy / 其他来源抢同一窗口
            seen_slugs_fve = set()
            deduped_fve = []
            for sig in fv_edge_signals:
                slug = sig.get("slug", "")
                if slug in seen_slugs_fve:
                    continue
                seen_slugs_fve.add(slug)
                deduped_fve.append(sig)
            fv_edge_signals = deduped_fve

            if fv_edge_signals:
                logger.info(
                    "🔍 [FVEdge] %d 个 edge 信号 (阈值 %dbps, max_mte %.1fmin)",
                    len(fv_edge_signals),
                    300, 2.0,
                )
                for sig in fv_edge_signals:
                    try:
                        msg = await self._execute_lowbuy_signal(
                            sig, snapshots_for_lowbuy, now_utc,
                        )
                        if msg:
                            fv_edge_messages.append(msg)
                    except Exception as exc:
                        logger.error("[FVEdge] 执行信号失败: %s", exc)
                        fv_edge_messages.append(f"FVEdge 异常: {exc}")

        # FV 当前仅用于训练/校准与样本落盘, 不参与生产 LowBuy 入场拦截。
        # 如未来重新启用, 统一从 LOWBUY_FV_FILTER_ENABLED 进入, 避免隐式生效。
        lowbuy_fair_up = None
        if self.LOWBUY_FV_FILTER_ENABLED and rule_signal:
            lowbuy_fair_up = rule_signal.get("fair_up")
        lowbuy_messages = []
        if lowbuy_enabled:
            # 获取 BTC 趋势方向用于 LowBuy 方向过滤
            lowbuy_direction = (btc or {}).get("direction_hint", "flat") if btc else None
            lowbuy_signals = self.lowbuy_engine.scan(
                snapshots_for_lowbuy, now_utc,
                fair_up=lowbuy_fair_up, direction_hint=lowbuy_direction,
            )
            if snapshots_for_lowbuy:
                # debug: 打印每个窗口的实际价格
                for s in snapshots_for_lowbuy:
                    if s.get("slug","").startswith("btc-updown-15m-"):
                        end = iso_to_utc_dt(s.get("end_date",""))
                        mins = (end - now_utc).total_seconds() / 60
                        prices = []
                        for o in s.get("outcomes",[]):
                            a = float(o.get("best_ask",0) or 0)
                            b = float(o.get("best_bid",0) or 0)
                            prices.append(f'{o["label"][:1]}{a*100:.0f}¢/b{b*100:.0f}¢')
                        if 0 <= mins <= 15:
                            logger.info("  [LowBuy] %s 剩 %.1fmin: %s → 信号=%d",
                                s["slug"][-14:], mins, " ".join(prices), len(lowbuy_signals))
                logger.info(
                    "🔍 [LowBuy] 扫描 %d 个窗口, 信号=%d",
                    len(snapshots_for_lowbuy),
                    len(lowbuy_signals),
                )
            # Bug fix 2026-06-26: 信号去重 — 防止同 cycle 内重复 signal 被多次执行.
            # 同一 slug 的 BUY + TP/TIME_STOP 同时触发时, 先平后开逻辑要串行化.
            # 用 dict 做 signature → signal 映射, 只保留每个 slug 第一个出现的信号.
            seen_slugs = set()
            deduped_signals = []
            for sig in lowbuy_signals:
                slug = sig.get("slug", "")
                action = sig.get("action", "")
                key = f"{slug}:{action}"
                if key in seen_slugs:
                    continue
                seen_slugs.add(key)
                deduped_signals.append(sig)
            lowbuy_signals = deduped_signals

            # 处理顺序: BUY 和 TP/TIME_STOP 优先级
            # 1) 先执行所有 TAKE_PROFIT/TIME_STOP (关闭现有仓位)
            # 2) 再执行所有 BUY (开新仓位, 不会被 slug 唯一性拦截因为已关闭)
            for sig in [s for s in lowbuy_signals if s.get("action") in ("TAKE_PROFIT", "TIME_STOP")]:
                try:
                    msg = await self._execute_lowbuy_signal(sig, snapshots_for_lowbuy, now_utc)
                    if msg:
                        lowbuy_messages.append(msg)
                except Exception as exc:
                    logger.error("[LowBuy] 执行信号失败: %s", exc)
                    lowbuy_messages.append(f"LowBuy 异常: {exc}")

            for sig in [s for s in lowbuy_signals if s.get("action") == "BUY"]:
                try:
                    msg = await self._execute_lowbuy_signal(sig, snapshots_for_lowbuy, now_utc)
                    if msg:
                        lowbuy_messages.append(msg)
                except Exception as exc:
                    logger.error("[LowBuy] 执行信号失败: %s", exc)
                    lowbuy_messages.append(f"LowBuy 异常: {exc}")

            if lowbuy_messages:
                execution_summary = (
                    execution_summary + " | " + "；".join(lowbuy_messages)
                    if execution_summary and execution_summary != "未执行"
                    else "；".join(lowbuy_messages)
                )

        # 2026-07-11: fv_edge 信号消息并入 execution_summary (供前端展示)
        if fv_edge_messages:
            execution_summary = (
                execution_summary + " | " + "；".join(fv_edge_messages)
                if execution_summary and execution_summary != "未执行"
                else "；".join(fv_edge_messages)
            )

        # 把 lowbuy 引擎状态加到 status export
        lowbuy_summary = self.lowbuy_engine.get_state_summary() if lowbuy_enabled else {
            "strategy": "LowBuy",
            "enabled": False,
            "reason": "已通过 LOWBUY_ENABLED=false 关闭",
        }
        hedged_limit_summary = self.hedged_limit_engine.get_state_summary(self.state_manager.get_state())
        fv_edge_summary = self.fv_edge.diagnostics() if hasattr(self, "fv_edge") else {
            "strategy": "FVEdge", "enabled": False,
        }

        StatusExporter.export({
            **base_status,
            "market_slug": focus_market.get("slug", ""),
            "market_question": focus_market.get("question", ""),
            "market_end_date": focus_market.get("end_date", ""),
            "market_outcomes": market_outcomes,
            "ai_prediction": action,
            "ai_action": action,
            "ai_confidence": confidence,
            "ai_outcome_index": selected_index,
            "ai_outcome_label": selected_label,
            "decision_reason": reason,
            "execution_summary": execution_summary,
            "lowbuy": lowbuy_summary,
            "hedged_limit": hedged_limit_summary,
            "fv_edge": fv_edge_summary,
        })

    async def start(self):
        logger.info("🤖 Polymarket 通用交易机器人启动")
        logger.info("📊 当前模式: %s", self.current_mode)

        # 后台 BTC 趋势监控 (与 run_cycle 独立, 5s 拉一次)
        btc_task = asyncio.create_task(self._btc_monitor_loop())

        try:
            while self.running:
                try:
                    _cycle_start = _time.monotonic()
                    await self.run_cycle()
                    # 绝对时间调度: 计算本轮实际耗时, sleep(2 - 耗时).
                    # 如果 run_cycle 耗时 >= 2s, 跳过 sleep 立即执行下一轮.
                    _elapsed = _time.monotonic() - _cycle_start
                    _remain = 2.0 - _elapsed
                    if _remain > 0:
                        await asyncio.sleep(_remain)
                except Exception as exc:  # noqa: BLE001
                    logger.exception("run_cycle crashed: %s", exc)
                    await asyncio.sleep(10)
        finally:
            btc_task.cancel()
            try:
                await btc_task
            except asyncio.CancelledError:
                pass

    BTC_SNAPSHOT_FILE = os.path.join(DATA_DIR, "btc_snapshot.json")
    BTC_WINDOW_REFS_FILE = os.path.join(DATA_DIR, "btc_window_refs.json")
    BTC_TICKS_FILE = os.path.join(DATA_DIR, "btc_ticks.jsonl")
    FAIR_VALUE_PREDICTIONS_FILE = os.path.join(DATA_DIR, "fair_value_predictions.jsonl")
    BTC_SNAPSHOT_HISTORY = 60  # 保留最近 60 条样本 (5 分钟)
    POSITION_AUDIT_FILE = os.path.join(DATA_DIR, "position_audit.jsonl")  # 开仓/平仓审计 log
    _last_btc_price = {}       # 上次成功的价格缓存 (用于 CoinGecko 限流时兜底)
    _last_btc_ts = 0.0         # 上次成功获取的时间戳
    _btc_fetch_interval = 60   # 两次 API 调用最小间隔 (秒, 防止 CoinGecko 429)
    # FV 继续训练/落盘；实时 LowBuy 入场暂不使用 FV，等修正实时 FV 接法并 A/B 后再启用。
    # 生产开关: 当前保持关闭。Fair Value 仅做监控/记录, 不做 LowBuy 入场过滤。
    LOWBUY_FV_FILTER_ENABLED = False

    @staticmethod
    def _append_jsonl(path: str, event: Dict[str, Any]) -> None:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n")

    @staticmethod
    def _slug_start_dt(slug: str) -> Optional[datetime]:
        try:
            ts = int(str(slug).rsplit("-", 1)[-1])
            return datetime.fromtimestamp(ts, tz=timezone.utc)
        except Exception:
            return None

    def _record_btc_tick(self, snap: Dict[str, Any], now_ts: float) -> None:
        price = safe_float(snap.get("price"))
        if price is None or price <= 0:
            return
        # 只记录真实刷新点: cached 且 cache_age_secs>0 的 2s 前端刷新样本不写入训练 tick。
        if snap.get("cached") and int(snap.get("cache_age_secs", 0) or 0) > 0:
            return
        if now_ts - self._last_btc_tick_write_ts < 1.0 and price == self._last_btc_tick_price:
            return
        event = {
            "t": snap.get("captured_at") or datetime.now(timezone.utc).isoformat(),
            "price": round(float(price), 2),
            "source": snap.get("source", "unknown"),
            "cached": bool(snap.get("cached", False)),
            "cache_age_secs": int(snap.get("cache_age_secs", 0) or 0),
        }
        try:
            self._append_jsonl(self.BTC_TICKS_FILE, event)
            self._last_btc_tick_write_ts = now_ts
            self._last_btc_tick_price = price
        except Exception as exc:
            logger.debug("btc_tick_write_failed: %s", exc)

    def _estimate_sigma_from_history(self, history: List[Dict[str, Any]]) -> Optional[float]:
        observations = []
        for item in history[-60:]:
            # Cached 2s dashboard samples are not new market observations.
            if item.get("cached"):
                continue
            p = safe_float(item.get("price"))
            raw_t = item.get("t")
            if p is None or p <= 0 or not raw_t:
                continue
            try:
                t = datetime.fromisoformat(str(raw_t).replace("Z", "+00:00")).timestamp()
            except (TypeError, ValueError, OverflowError):
                continue
            observations.append((t, p))
        if len(observations) < 4:
            return None
        observations.sort(key=lambda item: item[0])
        returns = []
        total_dt = 0.0
        for (t0, p0), (t1, p1) in zip(observations, observations[1:]):
            dt = t1 - t0
            if dt <= 0 or dt > 900 or p0 <= 0 or p1 <= 0:
                continue
            returns.append((math.log(p1 / p0), dt))
            total_dt += dt
        if len(returns) < 3 or total_dt < 120.0:
            return None

        # Estimate variance per second using the actual interval between
        # fresh observations. This avoids treating a 60s API refresh as 2s.
        drift_rate = sum(ret for ret, _ in returns) / total_dt
        variance_rate = sum((ret - drift_rate * dt) ** 2 for ret, dt in returns) / total_dt
        sigma_15m = math.sqrt(max(variance_rate, 0.0) * 900.0)
        if sigma_15m <= 0 or math.isnan(sigma_15m) or math.isinf(sigma_15m):
            return None
        return max(sigma_15m, FAIR_MIN_SIGMA)

    def _ensure_window_ref(self, market: Dict[str, Any], price: float, now_utc: datetime) -> Optional[Dict[str, Any]]:
        slug = str(market.get("slug", ""))
        if not slug.startswith("btc-updown-15m-") or price <= 0:
            return None
        ref = self._btc_window_refs.get(slug)
        if ref and safe_float(ref.get("ref_px")):
            return ref
        start_dt = self._slug_start_dt(slug)
        end_dt = iso_to_utc_dt(market.get("end_date", "")) if market.get("end_date") else None
        # Do not use a pre-window price as the settlement strike. The market
        # scanner can see upcoming windows before their start timestamp.
        if start_dt and now_utc < start_dt:
            return None
        # Record the first post-start price. If the bot started late, retain
        # the marker so those samples can be excluded during calibration.
        ref = {
            "window_start": start_dt.isoformat() if start_dt else None,
            "window_end": end_dt.isoformat() if end_dt else None,
            "ref_px": round(float(price), 2),
            "captured_at": now_utc.isoformat(),
            "source": "btc_monitor",
            "late_ref": bool(start_dt and (now_utc - start_dt).total_seconds() > 90),
        }
        self._btc_window_refs[slug] = ref
        # 只保留最近 200 个窗口，防止 JSON 无限增长。
        if len(self._btc_window_refs) > 200:
            ordered = sorted(
                self._btc_window_refs.items(),
                key=lambda kv: str(kv[1].get("window_start") or ""),
            )[-200:]
            self._btc_window_refs = dict(ordered)
        save_json_file(self.BTC_WINDOW_REFS_FILE, self._btc_window_refs)
        logger.info("[FV/train] 记录窗口 ref_px: %s ref=$%.2f late_ref=%s", slug, price, ref["late_ref"])
        return ref

    def _record_fv_predictions(self, markets: List[Dict[str, Any]], snap: Dict[str, Any], now_utc: datetime) -> None:
        price = safe_float(snap.get("price"))
        if price is None or price <= 0:
            return
        history = getattr(self, "_btc_history", [])
        sigma_hist = self._estimate_sigma_from_history(history)
        sigma_default = safe_float(snap.get("sigma_15m")) or FAIR_MIN_SIGMA
        sigma = sigma_hist or sigma_default or FAIR_MIN_SIGMA
        if sigma < FAIR_MIN_SIGMA:
            sigma = FAIR_MIN_SIGMA
        for market in markets:
            slug = str(market.get("slug", ""))
            if not slug.startswith("btc-updown-15m-"):
                continue
            try:
                end_dt = iso_to_utc_dt(market.get("end_date", ""))
            except Exception:
                continue
            tau = (end_dt - now_utc).total_seconds()
            if tau <= 0 or tau > FAIR_WINDOW_SEC + 120:
                continue
            ref = self._ensure_window_ref(market, float(price), now_utc)
            ref_px = safe_float((ref or {}).get("ref_px"))
            if ref_px is None or ref_px <= 0:
                continue
            outcomes = market.get("outcomes") or []
            market_up_ask = safe_float(outcomes[0].get("best_ask")) if len(outcomes) > 0 else None
            market_down_ask = safe_float(outcomes[1].get("best_ask")) if len(outcomes) > 1 else None
            fv = compute_fair_updown(
                s_now=float(price),
                ref_px=float(ref_px),
                sigma_15m=float(sigma),
                tau_sec=float(tau),
                window_sec=FAIR_WINDOW_SEC,
                drift=0.0,
                market_price=market_up_ask,
            )
            fair_up = safe_float(fv.get("fair_up"), 0.5) or 0.5
            event = {
                "t": now_utc.isoformat(),
                "slug": slug,
                "minutes_to_end": round(tau / 60.0, 3),
                "ref_px": round(float(ref_px), 2),
                "s_now": round(float(price), 2),
                "sigma_15m": round(float(sigma), 6),
                "sigma_source": "history" if sigma_hist else "provider",
                "fair_up": round(fair_up, 4),
                "fair_down": round(1.0 - fair_up, 4),
                "fair_z_score": fv.get("z_score"),
                "market_up_ask": market_up_ask,
                "market_down_ask": market_down_ask,
                "edge_up_bps": fv.get("edge_bps_vs_market"),
                "edge_down_bps": (round(((1.0 - fair_up) - market_down_ask) * 10000.0, 2) if market_down_ask else None),
                "lowbuy_filter_enabled": self.LOWBUY_FV_FILTER_ENABLED,
                "late_ref": bool((ref or {}).get("late_ref")),
                "btc_captured_at": snap.get("captured_at"),
                "btc_fetched_at": snap.get("fetched_at"),
                "btc_cache_age_secs": snap.get("cache_age_secs", 0),
                "book_observed_at": market.get("book_observed_at"),
                "book_fetch_latency_ms": market.get("book_fetch_latency_ms"),
            }
            try:
                self._append_jsonl(self.FAIR_VALUE_PREDICTIONS_FILE, event)
            except Exception as exc:
                logger.debug("fv_prediction_write_failed: %s", exc)

    async def _btc_monitor_loop(self):
        """每 2s 写一次快照 (含 FV), 但每 60s 才打一次 CoinGecko/Binance.
        
        CoinGecko 免费 API 限流严重 (10-30次/分), 缓存上次成功价格.
        第一次启动时如果没有缓存, 最多等 60s 后重试.
        """
        import time as _time
        await asyncio.sleep(2)
        while self.running:
            try:
                now_ts = _time.time()
                fresh_fetch = False
                # 距离上次 API 调用超过间隔才 fetch
                if now_ts - self._last_btc_ts >= self._btc_fetch_interval:
                    snap = await self.btc_api.get_signal_context()
                    if not snap or snap.get("price") is None:
                        snap = await self.btc_api._get_coingecko_price()
                    if snap:
                        snap = dict(snap)
                        snap["fetched_at"] = datetime.now(timezone.utc).isoformat()
                        self._last_btc_price = snap
                        self._last_btc_ts = now_ts
                        fresh_fetch = True
                        logger.debug("btc_monitor: fetched price=%.2f source=%s",
                                     snap.get("price", 0), snap.get("source", "?"))
                    else:
                        logger.warning("btc_monitor: all price sources failed, waiting %ds",
                                       self._btc_fetch_interval)
                # 用缓存数据写快照 (每 2s 一次, 保持前端刷新)
                if self._last_btc_price:
                    snap = dict(self._last_btc_price)
                    snap["captured_at"] = datetime.now(timezone.utc).isoformat()
                    snap["cached"] = not fresh_fetch
                    snap["cache_age_secs"] = int(now_ts - self._last_btc_ts)
                    ref_px = safe_float(snap.get("ref_px")) or safe_float(snap.get("price")) or 0.0
                    sigma = safe_float(snap.get("sigma_15m"))
                    if sigma is None or sigma <= 0:
                        sigma = FAIR_MIN_SIGMA
                    if ref_px > 0:
                        fv = compute_fair_updown(
                            s_now=ref_px,
                            ref_px=ref_px,
                            sigma_15m=sigma,
                            tau_sec=FAIR_WINDOW_SEC,
                            window_sec=FAIR_WINDOW_SEC,
                            drift=0.0,
                            market_price=None,
                        )
                        snap["fair_up"] = round(float(fv.get("fair_up", 0.5)), 4)
                        snap["fair_down"] = round(float(fv.get("fair_down", 0.5)), 4)
                        snap["fair_z_score"] = round(float(fv.get("z_score", 0.0)), 3)
                        snap["sigma_15m"] = round(sigma, 6)
                        # Edge bps: fair 偏离 50/50 的程度 (正值=方向性信号)
                        fv_up = snap["fair_up"]
                        fv_down = snap["fair_down"]
                        edge = abs(fv_up - 0.5) * 10000
                        snap["fair_edge_bps"] = round(edge, 1)
                        snap["fair_direction"] = "up" if fv_up > fv_down else "down"
                    # 滚动保存历史 (in-memory)
                    history = getattr(self, "_btc_history", [])
                    if fresh_fetch:
                        history.append({
                            "t": snap.get("fetched_at") or snap["captured_at"],
                            "price": snap.get("price"),
                            "cached": False,
                        })
                    self._btc_history = history[-self.BTC_SNAPSHOT_HISTORY:]
                    snap["history"] = self._btc_history

                    self._record_btc_tick(snap, now_ts)
                    try:
                        fv_markets = await self.market_api.get_market_snapshots(datetime.now(timezone.utc))
                        self._record_fv_predictions(fv_markets, snap, datetime.now(timezone.utc))
                    except Exception as exc:
                        logger.debug("fv_training_record_skipped: %s", exc)

                    save_json_file(self.BTC_SNAPSHOT_FILE, snap)
                else:
                    # 尚未获取到价格, 跳过本轮
                    pass
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.debug("btc_monitor: %s", exc)
            await asyncio.sleep(2)
