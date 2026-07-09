#!/usr/bin/env python3
"""Sanity-check the ThreeTierDefenseNetwork against local history.

This script reconstructs:
- 1m closes from ``data/btc_ticks.jsonl``
- 5m window outcomes from the 1m closes
- ``recent_5m_contracts_y_ratio`` as ``Yes / N`` over the last N completed 5m windows

Then it evaluates the Tier-2 block rate for several ``fv_vol_multiplier`` values.
"""

from __future__ import annotations

import json
import math
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from pathlib import Path
from statistics import mean, stdev
from typing import Any, Dict, List, Optional, Tuple
import sys


def _find_root() -> Path:
    cwd = Path.cwd()
    if (cwd / "data" / "btc_ticks.jsonl").exists():
        return cwd
    here = Path(__file__).resolve()
    for candidate in [here.parent] + list(here.parents):
        if (candidate / "data" / "btc_ticks.jsonl").exists():
            return candidate
    return cwd


ROOT = _find_root()
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
TICK_FILE = ROOT / "data" / "btc_ticks.jsonl"
PRED_FILE = ROOT / "data" / "fair_value_predictions.jsonl"


def standard_normal_cdf(x: float) -> float:
    return (1.0 + math.erf(x / math.sqrt(2.0))) / 2.0


def parse_ts(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def floor_minute(dt: datetime) -> datetime:
    return dt.replace(second=0, microsecond=0)


def floor_5m(dt: datetime) -> datetime:
    minute = (dt.minute // 5) * 5
    return dt.replace(minute=minute, second=0, microsecond=0)


def load_jsonl(path: Path) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return rows


def build_minute_closes(ticks: List[Dict[str, Any]]) -> Dict[datetime, float]:
    buckets: Dict[datetime, float] = {}
    for tick in ticks:
        try:
            ts = parse_ts(str(tick["t"]))
            price = float(tick["price"])
        except Exception:
            continue
        buckets[floor_minute(ts)] = price
    return dict(sorted(buckets.items()))


def build_5m_outcomes(minute_closes: Dict[datetime, float]) -> Dict[datetime, int]:
    items = list(minute_closes.items())
    by_ts = {ts: price for ts, price in items}
    if not items:
        return {}
    start = floor_5m(items[0][0])
    end = floor_5m(items[-1][0])
    outcomes: Dict[datetime, int] = {}
    cursor = start
    while cursor <= end:
        start_price = by_ts.get(cursor)
        end_price = by_ts.get(cursor + timedelta(minutes=4))
        if start_price is not None and end_price is not None:
            outcomes[cursor + timedelta(minutes=5)] = 1 if end_price >= start_price else 0
        cursor += timedelta(minutes=5)
    return outcomes


def recent_5m_contracts_y_ratio(
    minute_closes: Dict[datetime, float],
    now: datetime,
    n: int = 5,
) -> float:
    if n <= 0:
        raise ValueError("n must be positive")
    completed_ends: List[datetime] = []
    current_end = floor_5m(now)
    for i in range(1, n + 1):
        completed_ends.append(current_end - timedelta(minutes=5 * i))
    ys = 0
    counted = 0
    for end_ts in completed_ends:
        start_ts = end_ts - timedelta(minutes=5)
        start_price = minute_closes.get(start_ts)
        end_price = minute_closes.get(end_ts - timedelta(minutes=1))
        if start_price is None or end_price is None:
            continue
        counted += 1
        if end_price >= start_price:
            ys += 1
    return ys / counted if counted else 0.5


def recent_15m_prices(minute_closes: Dict[datetime, float], now: datetime, n: int = 15) -> List[float]:
    closes = []
    cursor = floor_minute(now)
    for i in range(n):
        ts = cursor - timedelta(minutes=i)
        px = minute_closes.get(ts)
        if px is not None:
            closes.append(px)
    return list(reversed(closes))


class Result:
    def __init__(self) -> None:
        self.total = 0
        self.allow = 0
        self.tier1 = 0
        self.tier2 = 0
        self.tier3 = 0

    def record(self, tier: Optional[int], allowed: bool) -> None:
        self.total += 1
        if allowed:
            self.allow += 1
            return
        if tier == 1:
            self.tier1 += 1
        elif tier == 2:
            self.tier2 += 1
        elif tier == 3:
            self.tier3 += 1

    def summary(self) -> str:
        block = self.total - self.allow
        tier2_checked = self.total - self.tier1
        tier3_checked = self.total - self.tier1 - self.tier2
        tier2_rate = (self.tier2 / tier2_checked * 100) if tier2_checked else 0.0
        tier3_rate = (self.tier3 / tier3_checked * 100) if tier3_checked else 0.0
        return (
            f"total={self.total} allow={self.allow} block={block} "
            f"block_rate={block / self.total * 100:.2f}% "
            f"(tier1={self.tier1}, tier2={self.tier2}/{tier2_checked}={tier2_rate:.2f}%, "
            f"tier3={self.tier3}/{tier3_checked}={tier3_rate:.2f}%)"
        )


class ThreeTierDefenseNetwork:
    def __init__(
        self,
        sr_pivot_window: int = 4,
        sr_up_support_thres: float = 0.25,
        sr_down_resist_thres: float = 0.75,
        fv_vol_multiplier: float = 0.50,
        resonance_bear_block: float = 0.15,
        resonance_bull_block: float = 0.85,
        vol_lookback: int = 12,
    ):
        self.sr_pivot_window = sr_pivot_window
        self.sr_up_support_thres = sr_up_support_thres
        self.sr_down_resist_thres = sr_down_resist_thres
        self.fv_vol_multiplier = fv_vol_multiplier
        self.resonance_bear_block = resonance_bear_block
        self.resonance_bull_block = resonance_bull_block
        self.vol_lookback = vol_lookback
        self._price_history_5m: List[float] = []
        self._returns_5m: List[float] = []

    def record_5m_price(self, price: float, mode: str = "5m_close", accumulated_log_return: Optional[float] = None) -> None:
        if mode == "5m_close":
            if self._price_history_5m:
                prev = self._price_history_5m[-1]
                if prev > 0 and price > 0:
                    self._returns_5m.append(math.log(price / prev))
            self._price_history_5m.append(price)
        elif mode == "1m_accumulate":
            if accumulated_log_return is not None:
                self._returns_5m.append(accumulated_log_return)
            self._price_history_5m.append(price)
        else:
            raise ValueError(f"Unknown mode: {mode}")

        self._price_history_5m = self._price_history_5m[-(self.vol_lookback + 1):]
        self._returns_5m = self._returns_5m[-self.vol_lookback:]

    def _rolling_annualized_vol(self) -> float:
        if len(self._returns_5m) < 4:
            return 0.35
        return float(max(0.20, min(0.80, stdev(self._returns_5m) * math.sqrt(105_120))))

    @staticmethod
    def _pivot_extremes(prices: List[float], window: int) -> Tuple[float, float]:
        if len(prices) < window + 1:
            return min(prices), max(prices)
        highs = []
        lows = []
        for i in range(window, len(prices)):
            seg = prices[i - window : i + 1]
            highs.append(max(seg))
            lows.append(min(seg))
        return float(sorted(lows)[len(lows) // 2]), float(sorted(highs)[len(highs) // 2])

    def _compute_sr_position(self, current_price: float, history: List[float]) -> float:
        if len(history) < 2:
            return 0.5
        support, resistance = self._pivot_extremes(history, self.sr_pivot_window)
        if resistance <= support:
            return 0.5
        return float(max(0.0, min(1.0, (current_price - support) / (resistance - support))))

    def _estimate_fair_value(self, current_price: float, strike_price: float, time_to_expiry_days: float, is_up_contract: bool) -> float:
        vol = self._rolling_annualized_vol()
        if time_to_expiry_days <= 0 or vol <= 0:
            return 0.50
        sqrt_t = math.sqrt(time_to_expiry_days)
        d1 = (math.log(current_price / strike_price) + 0.5 * vol**2 * time_to_expiry_days) / (vol * sqrt_t)
        d2 = d1 - vol * sqrt_t
        prob_above = standard_normal_cdf(d2)
        return prob_above if is_up_contract else (1.0 - prob_above)

    def _dynamic_fv_threshold(self) -> float:
        return self.fv_vol_multiplier * self._rolling_annualized_vol()

    def check_filters(
        self,
        current_btc_price: float,
        strike_price: float,
        time_remaining_hours: float,
        is_up_contract: bool,
        market_price_cents: float,
        recent_15m_prices: List[float],
        recent_5m_contracts_y_ratio: float,
    ) -> Dict[str, Any]:
        vol = self._rolling_annualized_vol()
        sr_pos = self._compute_sr_position(current_btc_price, recent_15m_prices)

        if is_up_contract and sr_pos < self.sr_up_support_thres:
            return {"allow_entry": False, "tier": 1, "reason": "T1 Block"}
        if not is_up_contract and sr_pos > self.sr_down_resist_thres:
            return {"allow_entry": False, "tier": 1, "reason": "T1 Block"}

        expiry_days = time_remaining_hours / 24.0
        fair_value = self._estimate_fair_value(current_btc_price, strike_price, expiry_days, is_up_contract)
        market_prob = market_price_cents / 100.0
        deviation = abs(market_prob - fair_value)
        threshold = self._dynamic_fv_threshold()
        if deviation > threshold:
            return {
                "allow_entry": False,
                "tier": 2,
                "reason": f"T2 Block: {deviation:.3f} > {threshold:.3f} (vol={vol:.2f})",
            }

        if is_up_contract and recent_5m_contracts_y_ratio < self.resonance_bear_block:
            return {"allow_entry": False, "tier": 3, "reason": "T3 Block"}
        if (not is_up_contract) and recent_5m_contracts_y_ratio > self.resonance_bull_block:
            return {"allow_entry": False, "tier": 3, "reason": "T3 Block"}

        return {"allow_entry": True, "tier": 0, "reason": "OK"}


def evaluate(
    defense: ThreeTierDefenseNetwork,
    predictions: List[Dict[str, Any]],
    minute_closes: Dict[datetime, float],
    *,
    n: int = 5,
) -> Result:
    result = Result()
    if not minute_closes:
        return result
    min_ts = min(minute_closes)
    max_ts = max(minute_closes)
    for row in predictions:
        try:
            now = parse_ts(str(row["t"]))
            if now < min_ts + timedelta(minutes=25) or now > max_ts:
                continue
            current_price = float(row["s_now"])
            strike_price = float(row["ref_px"])
            time_remaining_hours = float(row["minutes_to_end"]) / 60.0
            history_15m = recent_15m_prices(minute_closes, now, n=15)
            ratio = recent_5m_contracts_y_ratio(minute_closes, now, n=n)
            for is_up in (True, False):
                market_cents = float(row["market_up_ask" if is_up else "market_down_ask"]) * 100.0
                out = defense.check_filters(
                    current_btc_price=current_price,
                    strike_price=strike_price,
                    time_remaining_hours=time_remaining_hours,
                    is_up_contract=is_up,
                    market_price_cents=market_cents,
                    recent_15m_prices=history_15m,
                    recent_5m_contracts_y_ratio=ratio,
                )
                result.record(out.get("tier"), bool(out.get("allow_entry")))
        except Exception:
            continue
    return result


def main() -> None:
    ticks = load_jsonl(TICK_FILE)
    preds = load_jsonl(PRED_FILE)
    minute_closes = build_minute_closes(ticks)
    min_ts = min(minute_closes) if minute_closes else None
    max_ts = max(minute_closes) if minute_closes else None
    overlap_preds = 0
    if min_ts and max_ts:
        for row in preds:
            try:
                now = parse_ts(str(row["t"]))
            except Exception:
                continue
            if min_ts + timedelta(minutes=25) <= now <= max_ts:
                overlap_preds += 1

    print(f"ticks={len(ticks)} minute_closes={len(minute_closes)} predictions={len(preds)}")
    print(f"overlap_predictions={overlap_preds}")
    sample_times = [parse_ts(str(r["t"])) for r in preds if r.get("t") and min_ts and max_ts and min_ts + timedelta(minutes=25) <= parse_ts(str(r["t"])) <= max_ts][:20]
    if sample_times:
        ratios = [recent_5m_contracts_y_ratio(minute_closes, t, n=5) for t in sample_times]
        print(f"recent_5m_contracts_y_ratio sample mean={mean(ratios):.3f} min={min(ratios):.3f} max={max(ratios):.3f}")

    for multiplier in (0.25, 0.50, 0.75):
        defense = ThreeTierDefenseNetwork(fv_vol_multiplier=multiplier)
        # Prime the rolling vol with the same minute series so thresholding is realistic.
        for px in list(minute_closes.values())[-13:-1]:
            defense.record_5m_price(px, mode="5m_close")
        metrics = evaluate(defense, preds, minute_closes, n=5)
        print(f"fv_vol_multiplier={multiplier:.2f} {metrics.summary()}")


if __name__ == "__main__":
    main()
