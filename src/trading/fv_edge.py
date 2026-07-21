"""FV Edge strategy for BTC 15-minute UP/DOWN markets.

Strategy logic (V2 risk-gated FV Edge):
  1. For each BTC 15m window with current mte <= FV_EDGE_MAX_MTE:
     a. Compute fair_up from BTC price + ref_px + sigma_15m + tau_sec
     b. Compute independent UP and DOWN edges against each side's ask
     c. Buy the side with the largest positive edge above the threshold, but
        require the selected side to remain the model favorite by default

Backtest results are intentionally not embedded here; replay must use the
same first-eligible event and both executable asks as production.

Signals are consumed directly by the FV-only trading manager and held to expiry.
"""

from __future__ import annotations
import logging
import math
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger("fv_edge")

# Tunables (validated 2026-07-10 by scripts/analyze_fv_edge_combined.py)
FV_EDGE_THRESHOLD_BPS = 500       # 5% minimum edge to act (raised 2026-07-17: 300-500bps 区间 4 笔全输)
FV_EDGE_MAX_MTE = 1.5             # 1.5 minutes: only trade fresh signals (tightened 2026-07-18)
FV_EDGE_MIN_PRICE = 0.10          # avoid illiquid penny trades
FV_EDGE_MAX_PRICE = 0.85          # avoid 90c+ late bias zone
FV_EDGE_REQUIRE_FAVORITE_SIDE = True
FV_EDGE_REQUIRE_CHAINLINK = True
FV_EDGE_MAX_BOOK_AGE_SECONDS = 3.0

# Default position size in USDC.
FV_EDGE_DEFAULT_POSITION_USD = 2.0
# Conservative size tier: only use after the current data-quality reconciliation.
# $3 is deliberately not a multiplier; all other qualifying signals retain $2.
FV_EDGE_HIGH_CONFIDENCE_EDGE_BPS = 800.0
FV_EDGE_HIGH_CONFIDENCE_MIN_PRICE = 0.70
FV_EDGE_HIGH_CONFIDENCE_POSITION_USD = 3.0

# Confidence curve (2026-07-11):
#   edge=300bps -> 0.65
#   edge=500bps -> 0.75
#   edge=700bps -> 0.85
#   edge>=1000bps -> 0.95 (cap)
#
# This value is diagnostic only. The execution gate is the configured edge
# threshold, so backtest and production use the same decision rule.
FV_EDGE_CONFIDENCE_BASE = 0.5
FV_EDGE_CONFIDENCE_BPS_SCALE = 2000.0   # 1 / scale added per 1 bps
FV_EDGE_CONFIDENCE_CAP = 0.95


class FVEdgeStrategy:
    """Pure FV Edge signal engine.

    Lifecycle:
      - manager creates self.fv_edge = FVEdgeStrategy() in __init__
      - Each cycle: manager calls update_btc_snapshot(snap, window_refs)
        with latest BTC data + the per-slug ref_px map maintained by manager.
      - Each cycle: manager calls fv_edge.scan(markets, now_utc) -> signals
      - Signals carry market, outcome, edge diagnostics, and stake.
      - The manager deduplicates exposure and holds accepted signals to expiry.
    """

    def __init__(
        self,
        position_usd: float = FV_EDGE_DEFAULT_POSITION_USD,
        *,
        threshold_bps: float = FV_EDGE_THRESHOLD_BPS,
        max_mte: float = FV_EDGE_MAX_MTE,
        min_price: float = FV_EDGE_MIN_PRICE,
        max_price: float = FV_EDGE_MAX_PRICE,
        require_favorite_side: bool = FV_EDGE_REQUIRE_FAVORITE_SIDE,
        require_chainlink: bool = FV_EDGE_REQUIRE_CHAINLINK,
        max_book_age_seconds: float = FV_EDGE_MAX_BOOK_AGE_SECONDS,
    ) -> None:
        self.last_scan_at: Optional[datetime] = None
        self.last_btc_snap: Optional[Dict[str, Any]] = None
        self._window_refs: Dict[str, Dict[str, Any]] = {}
        self._position_usd = float(position_usd or FV_EDGE_DEFAULT_POSITION_USD)
        self._threshold_bps = max(0.0, float(threshold_bps))
        self._max_mte = max(0.0, float(max_mte))
        self._min_price = max(0.0, float(min_price))
        self._max_price = min(1.0, float(max_price))
        self._require_favorite_side = bool(require_favorite_side)
        self._require_chainlink = bool(require_chainlink)
        self._max_book_age_seconds = max(0.0, float(max_book_age_seconds))
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
        direction_filter=None,
    ) -> List[Dict[str, Any]]:
        """Scan BTC 15m markets for FV-vs-market edge.

        Returns FV Edge BUY signals with execution and diagnostic fields.

        Empty list if no BTC data or no qualifying markets.
        
        Args:
            direction_filter: DirectionFilter 实例，用于方向过滤（可选）
        """
        signals: List[Dict[str, Any]] = []
        self.last_scan_at = now_utc
        self.last_evaluation_count = 0
        self.last_qualifying_count = 0
        
        btc = self.last_btc_snap or {}
        btc_price = btc.get("price")
        if btc_price is None or btc_price <= 0:
            return signals
        if self._require_chainlink and btc.get("source") != "chainlink_rtds":
            return signals
        sigma_15m = btc.get("sigma_15m")
        try:
            sigma_15m = float(sigma_15m)
        except (TypeError, ValueError):
            return signals
        if sigma_15m <= 0:
            return signals
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
            market_ref = ref_override or market.get("chainlink_ref_px") or market.get("ref_px") or global_ref_px

            # Shadow mode: 先检查方向过滤（不实际限制），记录所有候选
            was_filtered = False
            assumed_pnl = 0.0
            if direction_filter is not None and direction_filter.mode == "shadow":
                up_sig = {
                    "outcome_label": "Up", "slug": slug,
                    "edge_bps": 0, "fair_selected": 0.5,
                    "current_ask": 0, "mte_minutes": 0,
                }
                down_sig = {
                    "outcome_label": "Down", "slug": slug,
                    "edge_bps": 0, "fair_selected": 0.5,
                    "current_ask": 0, "mte_minutes": 0,
                }
                # 评估 Up 方向
                up_eval = self._evaluate_market(
                    market, now_utc, btc_price, market_ref, sigma_15m,
                    ref_source=ref_record.get("source") or market.get("chainlink_ref_source"),
                    direction_filter=None,
                )
                if up_eval:
                    up_sig["edge_bps"] = up_eval.get("edge_bps", 0)
                    up_sig["fair_selected"] = up_eval.get("fair_selected", 0.5)
                    up_sig["current_ask"] = up_eval.get("current_ask", 0)
                    up_sig["mte_minutes"] = up_eval.get("mte_minutes", 0)
                    decision = direction_filter.evaluate_trade(up_sig, now_utc.timestamp())
                    if not decision["direction_would_allow"]:
                        # 被方向过滤拦截（反事实）→ 记录为 filtered
                        assumed_pnl = up_eval.get("edge_bps", 0) * up_sig.get("current_ask", 0) / 10000.0 * 2.0
                        direction_filter.record_shadow_candidate(up_sig, was_filtered=True, assumed_pnl=assumed_pnl)
                    else:
                        direction_filter.record_shadow_candidate(up_sig, was_filtered=False)
                # 评估 Down 方向
                down_eval = self._evaluate_market(
                    market, now_utc, btc_price, market_ref, sigma_15m,
                    ref_source=ref_record.get("source") or market.get("chainlink_ref_source"),
                    direction_filter=None,
                )
                if down_eval:
                    down_sig["edge_bps"] = down_eval.get("edge_bps", 0)
                    down_sig["fair_selected"] = down_eval.get("fair_selected", 0.5)
                    down_sig["current_ask"] = down_eval.get("current_ask", 0)
                    down_sig["mte_minutes"] = down_eval.get("mte_minutes", 0)
                    decision = direction_filter.evaluate_trade(down_sig, now_utc.timestamp())
                    if not decision["direction_would_allow"]:
                        # 被方向过滤拦截（反事实）→ 记录为 filtered
                        assumed_pnl = down_eval.get("edge_bps", 0) * down_sig.get("current_ask", 0) / 10000.0 * 2.0
                        direction_filter.record_shadow_candidate(down_sig, was_filtered=True, assumed_pnl=assumed_pnl)
                    else:
                        direction_filter.record_shadow_candidate(down_sig, was_filtered=False)

            sig = self._evaluate_market(
                market,
                now_utc,
                btc_price,
                market_ref,
                sigma_15m,
                ref_source=ref_record.get("source") or market.get("chainlink_ref_source"),
                direction_filter=direction_filter,
            )
            if sig is None:
                continue

            if direction_filter is not None:
                sig.update(direction_filter.evaluate_trade(sig, now_utc.timestamp()))
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
        *,
        ref_source: Optional[str] = None,
        direction_filter: Optional[Any] = None,
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
        if minutes_to_end > self._max_mte:
            return None
        if self._require_chainlink and ref_source not in {"chainlink", "polymarket_crypto_price"}:
            return None

        outcomes = market.get("outcomes", [])
        if len(outcomes) < 2:
            return None
        up, down = outcomes[0], outcomes[1]
        up_qsrc = up.get("quote_source", "")
        down_qsrc = down.get("quote_source", "")
        # 只使用 CLOB 实时盘口, 跳过 gamma mid fallback (可能过期或无真实流动性)
        if up_qsrc != "clob" or down_qsrc != "clob":
            return None
        observed_at = market.get("book_observed_at")
        if observed_at and self._max_book_age_seconds > 0:
            try:
                age = (
                    now_utc
                    - datetime.fromisoformat(observed_at.replace("Z", "+00:00"))
                ).total_seconds()
            except (TypeError, ValueError):
                return None
            if age < 0 or age > self._max_book_age_seconds:
                return None
        up_ask = float(up.get("best_ask") or 0)
        up_bid = float(up.get("best_bid") or 0)
        down_ask = float(down.get("best_ask") or 0)
        down_bid = float(down.get("best_bid") or 0)
        if up_ask <= 0 or up_bid <= 0 or down_ask <= 0 or down_bid <= 0:
            return None

        fair_up = self._compute_fair_up(btc_price, ref_px, sigma_15m, tau_sec)
        if fair_up is None:
            return None

        fair_down = 1.0 - fair_up
        edge_up_bps = (fair_up - up_ask) * 10000.0
        edge_down_bps = (fair_down - down_ask) * 10000.0

        # 方向过滤：在 max(edge) 之前限制候选方向
        if direction_filter is not None:
            allowed_up = direction_filter.should_allow_trade(
                {"outcome_label": "Up", "slug": slug}, now_utc.timestamp()
            )
            allowed_down = direction_filter.should_allow_trade(
                {"outcome_label": "Down", "slug": slug}, now_utc.timestamp()
            )
            if not allowed_up and not allowed_down:
                return None  # UNKNOWN/TRANSITION → 禁止新开仓
            if not allowed_up:
                # 只考虑 Down
                candidates = [(edge_down_bps, 1, "Down", down_ask, down_bid)]
            elif not allowed_down:
                # 只考虑 Up
                candidates = [(edge_up_bps, 0, "Up", up_ask, up_bid)]
            else:
                # NEUTRAL → 双向都允许
                candidates = [
                    (edge_up_bps, 0, "Up", up_ask, up_bid),
                    (edge_down_bps, 1, "Down", down_ask, down_bid),
                ]
        else:
            candidates = [
                (edge_up_bps, 0, "Up", up_ask, up_bid),
                (edge_down_bps, 1, "Down", down_ask, down_bid),
            ]
        edge_bps, outcome_index, outcome_label, buy_price, buy_bid = max(
            candidates, key=lambda item: item[0]
        )
        if edge_bps < self._threshold_bps:
            return None

        # Price range filter
        if not (self._min_price <= buy_price <= self._max_price):
            return None

        selected_fair = fair_up if outcome_index == 0 else fair_down
        if self._require_favorite_side and selected_fair <= 0.5:
            return None

        # Signal strength: base 0.5 + edge_bps / scale, capped.
        confidence = min(
            FV_EDGE_CONFIDENCE_BASE + edge_bps / FV_EDGE_CONFIDENCE_BPS_SCALE,
            FV_EDGE_CONFIDENCE_CAP,
        )
        high_confidence = (
            edge_bps >= FV_EDGE_HIGH_CONFIDENCE_EDGE_BPS
            and buy_price >= FV_EDGE_HIGH_CONFIDENCE_MIN_PRICE
        )
        stake = (
            FV_EDGE_HIGH_CONFIDENCE_POSITION_USD
            if high_confidence
            else self._position_usd
        )
        size_tier = "high_confidence" if high_confidence else "base"

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
                f"FV Edge | {outcome_label} edge={edge_bps:+.0f}bps "
                f"fair={fair_up if outcome_index == 0 else fair_down:.2f} "
                f"ref={ref_px:.0f} mte={minutes_to_end:.1f}min"
            ),
            "confidence": round(confidence, 3),
            "source": "fv_edge",
            # Execution metadata consumed by the FV-only manager.
            "hold_to_expiry": True,
            "stake": stake,
            "size_tier": size_tier,
            "high_confidence": high_confidence,
            "size_tier_edge_bps": FV_EDGE_HIGH_CONFIDENCE_EDGE_BPS,
            "size_tier_min_price": FV_EDGE_HIGH_CONFIDENCE_MIN_PRICE,
            # Diagnostics consumed by status export and signal history.
            "fair_up": round(fair_up, 4),
            "edge_bps": round(edge_bps, 1),
            "edge_up_bps": round(edge_up_bps, 1),
            "edge_down_bps": round(edge_down_bps, 1),
            "fair_selected": round(selected_fair, 4),
            "btc_source": self.last_btc_snap.get("source") if self.last_btc_snap else None,
            "ref_source": ref_source,
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
                "edge_bps": self._threshold_bps,
                "max_mte": self._max_mte,
                "min_price": self._min_price,
                "max_price": self._max_price,
            },
        }

    def accepts_price(self, price: float) -> bool:
        """Return whether an executable ask is inside the strategy's range."""
        return self._min_price <= price <= self._max_price
