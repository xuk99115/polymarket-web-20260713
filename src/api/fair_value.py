"""
Fair Value Model for BTC UPDOWN Markets
=======================================

Computes fair UP/DOWN probabilities for the Polymarket "btc-updown-15m-{ts}"
binary using a log-normal price model.

Model
-----
We treat BTC mid-window → end-of-window as geometric Brownian motion with
drift μ ≈ 0 over a 15-minute horizon:

    z = ln(s_now / ref_px) / (sigma * sqrt(tau / window_sec))
    fair_up   = Φ(z)
    fair_down = 1 - fair_up

This is the standard Black-Scholes-style digital-option formula with strike
ref_px, vol sigma_15m, and time-to-expiry tau_sec. The drift term is
exposed as a parameter (default 0) but in practice is negligible for
15-minute windows.

Edge vs market
--------------
edge_bps_vs_market = (fair_up - market_price) * 10000

A positive edge means our fair estimate sits above the market's UP price →
the market is underpricing UP (and overpricing DOWN).
"""

import math
import logging
from typing import Any, Dict, Optional, Union

logger = logging.getLogger("fair_value")

# Minimum values to avoid numerical issues
MIN_SIGMA = 0.0001   # 0.01% minimum volatility
MIN_TAU_SEC = 1.0    # 1 second minimum time remaining
DEFAULT_WINDOW_SEC = 900.0  # 15 minutes


def standard_normal_cdf(x: float) -> float:
    """Standard normal CDF Φ(x) via math.erf.

    Φ(x) = (1 + erf(x / √2)) / 2
    """
    try:
        return (1.0 + math.erf(x / math.sqrt(2.0))) / 2.0
    except (ValueError, OverflowError):
        # erf() raises on NaN/inf inputs; fall back to sane extremes.
        if x != x:  # NaN
            return 0.5
        return 1.0 if x > 0 else 0.0


def compute_fair_updown(
    s_now: float,
    ref_px: float,
    sigma_15m: float,
    tau_sec: float,
    window_sec: float = DEFAULT_WINDOW_SEC,
    drift: float = 0.0,
    market_price: Optional[float] = None,
) -> Dict[str, Any]:
    """Compute fair UP/DOWN probabilities and edge vs market.

    Parameters
    ----------
    s_now       : current BTC spot price
    ref_px      : reference (window-start) BTC price
    sigma_15m   : 15-minute volatility (std of log returns, e.g. 0.003 = 0.3%)
    tau_sec     : seconds remaining until window end
    window_sec  : total window length (default 900 = 15 min)
    drift       : optional drift term (default 0, rarely needed)
    market_price: optional UP-side market price (0..1) for edge_bps_vs_market

    Returns
    -------
    dict with keys: fair_up, fair_down, z_score, edge_bps_vs_market
    """
    result: Dict[str, Any] = {}
    # --- Edge cases: price inputs invalid → flat 50/50 ---
    try:
        s_now = float(s_now)
        ref_px = float(ref_px)
    except (TypeError, ValueError):
        logger.warning("fair_value_invalid_prices s_now=%s ref_px=%s", s_now, ref_px)
        return _default_fair(market_price)

    if s_now <= 0 or ref_px <= 0:
        logger.warning("fair_value_non_positive_prices s_now=%s ref_px=%s", s_now, ref_px)
        return _default_fair(market_price)

    # --- Edge case: sigma too small → can't estimate reliably ---
    try:
        sigma = float(sigma_15m)
    except (TypeError, ValueError):
        sigma = 0.0
    if sigma < MIN_SIGMA or math.isnan(sigma) or math.isinf(sigma):
        return _default_fair(market_price)

    # --- Edge case: tau too small → fair ≈ current state ---
    try:
        tau = float(tau_sec)
    except (TypeError, ValueError):
        tau = 0.0
    if tau < MIN_TAU_SEC:
        return _default_fair(market_price)

    # --- Normal math path ---
    try:
        window = max(float(window_sec), 1.0)
        tau_norm = tau / window
        # Guard sigma_scaled to avoid divide-by-zero in degenerate cases
        sigma_scaled = sigma * math.sqrt(tau_norm)
        if sigma_scaled < MIN_SIGMA:
            sigma_scaled = MIN_SIGMA

        log_ratio = math.log(s_now / ref_px)
        z_score = (log_ratio + drift * tau_norm) / sigma_scaled
    except (ValueError, ZeroDivisionError, OverflowError) as exc:
        logger.warning("fair_value_compute_error: %s", exc)
        return _default_fair(market_price)

    fair_up = standard_normal_cdf(z_score)
    fair_down = 1.0 - fair_up

    result = {
        "fair_up": round(fair_up, 4),
        "fair_down": round(fair_down, 4),
        "z_score": round(z_score, 3),
    }

    # edge_bps_vs_market: positive = market underprices UP
    edge_bps = None
    if market_price is not None:
        try:
            mp = float(market_price)
            if 0.0 < mp < 1.0:
                edge_bps = round((fair_up - mp) * 10000.0, 2)
        except (TypeError, ValueError):
            edge_bps = None
    result["edge_bps_vs_market"] = edge_bps
    return result


def compute_edge_bps(fair: Optional[float], market: Optional[float]) -> Optional[float]:
    """Edge in basis points: positive = fair is above market.

    edge = (fair - market) * 10000
    """
    if fair is None or market is None:
        return None
    try:
        return (float(fair) - float(market)) * 10000.0
    except (TypeError, ValueError):
        return None


def _default_fair(market_price: Optional[float] = None) -> Dict[str, Any]:
    """Neutral 50/50 fallback with edge vs market if provided."""
    result = {
        "fair_up": 0.5,
        "fair_down": 0.5,
        "z_score": 0.0,
    }
    if market_price is not None:
        try:
            mp = float(market_price)
            if 0.0 < mp < 1.0:
                result["edge_bps_vs_market"] = round((0.5 - mp) * 10000.0, 2)
            else:
                result["edge_bps_vs_market"] = None
        except (TypeError, ValueError):
            result["edge_bps_vs_market"] = None
    else:
        result["edge_bps_vs_market"] = None
    return result