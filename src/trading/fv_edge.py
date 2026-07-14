"""
FV + Edge Strategy
==================

A second decision engine, parallel to LowBuyDoubleEngine.

Strategy logic (V2 from analyze_fv_edge_combined.py 2026-07-10):
  1. For each BTC 15m window with current mte <= FV_EDGE_MAX_MTE:
     a. Compute fair_up from BTC price + ref_px + sigma_15m + tau_sec
     b. Compute edge = fair_up - market_up_ask (in bps)
     c. If |edge| >= threshold AND FV direction agrees with edge → BUY
  2. Direction agreement:
     - edge > 0 (FV says UP underpriced) AND fair_up > 0.5 (FV leans UP)
     - edge < 0 (FV says DOWN underpriced) AND fair_up < 0.5 (FV leans DOWN)

Baseline (paper backtest, 31 windows, target mte=1.0):
  V2 edge>=3% + FV agrees: 14 trades, 64.3% win, +47.30% ROI
  V2 edge>=5% + FV agrees: 17 trades, 70.6% win, +39.21% ROI
  V2 edge>=7% + FV agrees: 14 trades, 64.3% win, +47.30% ROI

Phase 1 (2026-07-10): skeleton written, NOT integrated into bot.py yet.
Phase 2 (2026-07-11): integrated into TradingBotManager.
  - manager.py imports FVEdgeStrategy and feeds btc snapshot + window refs.
  - signals emitted with action="BUY" + source="fv_edge" + hold_to_expiry=True.
  - BUY signals are routed through _execute_lowbuy_signal / _lowbuy_open so we
    share the position-book, dedupe, and audit pipeline with LowBuy.
  - hold_to_expiry=True means we skip TP/TIME_STOP registration in the
    lowbuy_engine (last 2 minutes of a 15-minute window — too short for TP).
  - Settled at window close via the same EXPIRY_EXIT path as LowBuy.
"""

from __future__ import annotations
import logging
import math
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger("fv_edge")

# Tunables (validated 2026-07-10 by scripts/analyze_fv_edge_combined.py)
FV_EDGE_THRESHOLD_BPS = 300       # 3% minimum edge to act
FV_EDGE_MAX_MTE = 2.0             # only enter in last 2 minutes of window
FV_EDGE_MIN_PRICE = 0.10          # avoid illiquid penny trades
FV_EDGE_MAX_PRICE = 0.85          # avoid 90c+ late bias zone

# Default position size in USDC. Smaller than LowBuy because fv_edge only
# fires in the last 2 minutes of a window — we want exposure on size but
# the win-rate * edge per trade should still be the dominant PnL driver.
FV_EDGE_DEFAULT_POSITION_USD = 2.0

# Confidence curve (2026-07-11):
#   edge=300bps -> 0.65
#   edge=500bps -> 0.75
#   edge=700bps -> 0.85
#   edge>=1000bps -> 0.95 (cap)
#
# Rationale: original abs(edge)/1000 produced 0.30 at 300bps which is
# BELOW AI_MIN_CONFIDENCE=0.45 in production and would silently filter out
# the most common edge zone (the +47% ROI bucket from backtest). The new
# curve keeps all qualifying edges above the BTC_AI_MIN_CONFIDENCE=0.45
# threshold so backtest ↔ production numbers are comparable.
FV_EDGE_CONFIDENCE_BASE = 0.5
FV_EDGE_CONFIDENCE_BPS_SCALE = 2000.0   # 1 / scale added per 1 bps
FV_EDGE_CONFIDENCE_CAP = 0.95


class FVEdgeStrategy:
    """Pure FV + edge strategy, separate from LowBuy.

    Lifecycle:
      - manager creates self.fv_edge = FVEdgeStrategy() in __init__
      - Each cycle: manager calls update_btc_snapshot(snap, window_refs)
        with latest BTC data + the per-slug ref_px map maintained by manager.
      - Each cycle: manager calls fv_edge.scan(markets, now_utc) -> signals
      - Signals use the SAME format as LowBuyDoubleEngine (action/slug/
        outcome_index/current_bid/current_ask/reason/confidence) plus:
          - source = "fv_edge"           (so executor path can branch)
          - hold_to_expiry = True        (skip TP/TIME_STOP — last 2 min only)
          - stake = <usdc>               (default FV_EDGE_DEFAULT_POSITION_USD)
        so the existing _execute_lowbuy_signal path can consume them unchanged.
    """

    def __init__(self, position_usd: float = FV_EDGE_DEFAULT_POSITION_USD) -> None:
        self.last_scan_at: Optional[datetime] = None
        self.last_btc_snap: Optional[Dict[str, Any]] = None
        self._window_refs: Dict[str, Dict[str, Any]] = {}
        self._position_usd = float(position_usd or FV_EDGE_DEFAULT_POSITION_USD)
        self.signals_emitted = 0           # total BUY signals ever produced
        self.last_evaluation_count = 0     # markets evaluated in last scan
        self.last_qualifying_count = 0     # markets that passed edge filter

    def update_btc_snapshot(
        self,
        snap: Dict[str, Any],
        window_refs: Optional[Dict[str, Dict[str, Any]]] = None,
    ) -> None:
        """Manager calls this every cycle with latest BTC data + window refs.

        Expected snap keys:
          - price (float): current BTC price
          - ref_px (float, optional): window strike price (fallback = price)
          - sigma_15m (float, optional): 15-minute volatility
        window_refs: {slug: {"ref_px": float, ...}} — manager's authoritative
          per-slug ref_px cache. If provided, overrides snap["ref_px"] when
          looking up the ref for a specific market in scan().
        """
        self.last_btc_snap = snap
        if window_refs is not None:
            self._window_refs = dict(window_refs)

    def scan(
        self,
        markets: List[Dict[str, Any]],
        now_utc: datetime,
    ) -> List[Dict[str, Any]]:
        """Scan BTC 15m markets for FV-vs-market edge.

        Returns signals in same format as LowBuyDoubleEngine.scan() (action,
        slug, outcome_index, outcome_label, current_bid, current_ask, reason,
        confidence) plus source="fv_edge", hold_to_expiry=True, stake=<usdc>.

        Empty list if no BTC data or no qualifying markets.
        """
        signals: List[Dict[str, Any]] = []
        self.last_scan_at = now_utc
        self.last_evaluation_count = 0
        self.last_qualifying_count = 0

        btc = self.last_btc_snap or {}
        btc_price = btc.get("price")
        if btc_price is None or btc_price <= 0:
            return signals
        sigma_15m = btc.get("sigma_15m") or 0.0001
        # Default ref_px = current price (fallback). Per-market override
        # happens inside _evaluate_market via _window_refs.
        global_ref_px = btc.get("ref_px") or btc_price

        for market in markets:
            self.last_evaluation_count += 1
            slug = market.get("slug", "")
            # Per-market ref_px: prefer manager's window_refs, then market
            # field, then global snap ref_px.
            ref_record = self._window_refs.get(slug) or {}
            ref_override = ref_record.get("ref_px")
            # A global current price is not a valid strike for a specific
            # rolling window. Require a post-start window reference and skip
            # late references rather than manufacturing a false edge.
            if slug.startswith("btc-updown-15m-") and (
                not ref_override or ref_record.get("late_ref")
            ):
                continue
            market_ref = market.get("ref_px") or ref_override or global_ref_px
            sig = self._evaluate_market(
                market, now_utc, btc_price, market_ref, sigma_15m,
            )
            if sig is not None:
                signals.append(sig)
                self.signals_emitted += 1
                self.last_qualifying_count += 1
        return signals

    def _evaluate_market(
        self,
        market: Dict[str, Any],
        now_utc: datetime,
        btc_price: float,
        ref_px: float,
        sigma_15m: float,
    ) -> Optional[Dict[str, Any]]:
        slug = market.get("slug", "")
        if not slug.startswith("btc-updown-15m-"):
            return None

        try:
            end_dt = datetime.fromisoformat(market["end_date"].replace("Z", "+00:00"))
            if end_dt.tzinfo is None:
                end_dt = end_dt.replace(tzinfo=timezone.utc)
        except (KeyError, ValueError):
            return None

        tau_sec = (end_dt - now_utc).total_seconds()
        if tau_sec <= 1.0:
            return None
        minutes_to_end = tau_sec / 60.0
        if minutes_to_end > FV_EDGE_MAX_MTE:
            return None

        outcomes = market.get("outcomes", [])
        if len(outcomes) < 2:
            return None
        up, down = outcomes[0], outcomes[1]
        up_ask = float(up.get("best_ask") or 0)
        up_bid = float(up.get("best_bid") or 0)
        down_ask = float(down.get("best_ask") or 0)
        down_bid = float(down.get("best_bid") or 0)
        if up_ask <= 0 or up_bid <= 0 or down_ask <= 0 or down_bid <= 0:
            return None

        fair_up = self._compute_fair_up(btc_price, ref_px, sigma_15m, tau_sec)
        if fair_up is None:
            return None

        edge_up_bps = (fair_up - up_ask) * 10000.0
        if abs(edge_up_bps) < FV_EDGE_THRESHOLD_BPS:
            return None

        # Direction agreement (V2 strategy)
        if edge_up_bps > 0 and fair_up < 0.5:
            return None
        if edge_up_bps < 0 and fair_up > 0.5:
            return None

        # Pick side
        if edge_up_bps > 0:
            outcome_index, outcome_label = 0, "Up"
            buy_price, buy_bid = up_ask, up_bid
        else:
            outcome_index, outcome_label = 1, "Down"
            buy_price, buy_bid = down_ask, down_bid

        # Price range filter
        if not (FV_EDGE_MIN_PRICE <= buy_price <= FV_EDGE_MAX_PRICE):
            return None

        # Confidence curve: base 0.5 + |edge_bps| / scale, capped.
        # Kept consistent with BTC_AI_MIN_CONFIDENCE=0.45 so that the most
        # common backtest bucket (300-700bps edge) clears production gates.
        confidence = min(
            FV_EDGE_CONFIDENCE_BASE + abs(edge_up_bps) / FV_EDGE_CONFIDENCE_BPS_SCALE,
            FV_EDGE_CONFIDENCE_CAP,
        )

        return {
            "action": "BUY",
            "slug": slug,
            "outcome_index": outcome_index,
            "outcome_label": outcome_label,
            "current_bid": buy_bid,
            "current_ask": buy_price,
            "book_observed_at": market.get("book_observed_at"),
            "book_fetch_latency_ms": market.get("book_fetch_latency_ms"),
            "btc_captured_at": self.last_btc_snap.get("captured_at") if self.last_btc_snap else None,
            "btc_fetched_at": self.last_btc_snap.get("fetched_at") if self.last_btc_snap else None,
            "btc_cache_age_secs": self.last_btc_snap.get("cache_age_secs", 0) if self.last_btc_snap else None,
            "reason": (
                f"FV+edge | edge={edge_up_bps:+.0f}bps fair={fair_up:.2f} "
                f"ref={ref_px:.0f} mte={minutes_to_end:.1f}min"
            ),
            "confidence": round(confidence, 3),
            "source": "fv_edge",
            # Routing hints for manager._lowbuy_open / lowbuy_engine:
            #   - source identifies the strategy in audit / position book
            #   - hold_to_expiry=True skips TP/TIME_STOP registration in
            #     lowbuy_engine — last 2 minutes of window, no time to TP
            #   - stake overrides LOWBUY_POSITION_USD inside _lowbuy_open
            #     (manager reads signal.get("stake") if present)
            "hold_to_expiry": True,
            "stake": self._position_usd,
            # Diagnostic extras — handy in bot_status / ai_history
            "fair_up": round(fair_up, 4),
            "edge_bps": round(edge_up_bps, 1),
            "mte_minutes": round(minutes_to_end, 2),
        }

    @staticmethod
    def _compute_fair_up(
        s_now: float,
        ref_px: float,
        sigma_15m: float,
        tau_sec: float,
        window_sec: float = 900.0,
    ) -> Optional[float]:
        """Standard normal CDF for log-normal GBM (Black-Scholes digital).

        z = ln(s_now / ref_px) / (sigma * sqrt(tau / window_sec))
        fair_up = Phi(z)
        """
        if sigma_15m <= 0 or tau_sec <= 0 or s_now <= 0 or ref_px <= 0:
            return None
        try:
            z = math.log(s_now / ref_px) / (sigma_15m * math.sqrt(tau_sec / window_sec))
            return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))
        except (ValueError, OverflowError):
            return None

    def diagnostics(self) -> Dict[str, Any]:
        """Snapshot for dashboard or log."""
        btc = self.last_btc_snap or {}
        return {
            "strategy": "FVEdge",
            "enabled": True,
            "last_scan_at": self.last_scan_at.isoformat() if self.last_scan_at else None,
            "signals_emitted_total": self.signals_emitted,
            "last_evaluation_count": self.last_evaluation_count,
            "last_qualifying_count": self.last_qualifying_count,
            "btc_price": btc.get("price"),
            "ref_px": btc.get("ref_px"),
            "sigma_15m": btc.get("sigma_15m"),
            "position_usd": self._position_usd,
            "thresholds": {
                "edge_bps": FV_EDGE_THRESHOLD_BPS,
                "max_mte": FV_EDGE_MAX_MTE,
                "min_price": FV_EDGE_MIN_PRICE,
                "max_price": FV_EDGE_MAX_PRICE,
            },
        }
