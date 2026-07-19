"""BTC 方向过滤器 — 基于 Chainlink BTC 价格计算趋势方向。"""

import json
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class DirectionState(str, Enum):
    UP = "UP"
    DOWN = "DOWN"
    NEUTRAL = "NEUTRAL"
    UNKNOWN = "UNKNOWN"
    TRANSITION = "TRANSITION"


@dataclass
class DirectionResult:
    direction: DirectionState
    pct_15m: float = 0.0
    pct_60m: float = 0.0
    data_points_15m: int = 0
    data_points_60m: int = 0
    stale_seconds: float = 0.0
    confirmed_count: int = 0


@dataclass
class DirectionFilter:
    mode: str = "shadow"
    update_seconds: int = 60
    confirmations: int = 2
    threshold_60m_bps: int = 30
    threshold_15m_bps: int = 10
    max_stale_seconds: int = 900

    status_file: str = ""
    log_file: str = ""

    _last_calc_time: float = 0.0
    _last_direction: DirectionState = DirectionState.UNKNOWN
    _confirm_count: int = 0
    _transition_target: Optional[DirectionState] = None
    _history: List[Dict[str, Any]] = field(default_factory=list)

    _shadow_stats: Dict[str, Any] = field(default_factory=dict)
    _shadow_filtered_signals: List[Dict[str, Any]] = field(default_factory=list)

    def __post_init__(self):
        if not self._shadow_stats:
            self._shadow_stats = {
                "mode": self.mode,
                "total_candidates": 0,
                "filtered_count": 0,
                "allowed_count": 0,
                "filtered_assumed_pnl": 0.0,
                "filtered_assumed_count": 0,
            }

    def set_history(self, ticks: List[Dict[str, Any]]) -> None:
        self._history = list(ticks)

    def calculate(self, now: Optional[float] = None) -> DirectionResult:
        now = now or time.time()
        if now - self._last_calc_time < self.update_seconds:
            return self._cached_result(now)
        self._last_calc_time = now
        raw = self._do_calculate()
        self._update_state_machine(raw)
        self._log_result(raw)
        self._write_status(raw)
        return DirectionResult(
            direction=self._last_direction,
            pct_15m=raw.pct_15m, pct_60m=raw.pct_60m,
            data_points_15m=raw.data_points_15m, data_points_60m=raw.data_points_60m,
            stale_seconds=raw.stale_seconds, confirmed_count=raw.confirmed_count,
        )

    def should_allow_trade(self, signal: Dict[str, Any], now: Optional[float] = None) -> bool:
        if self.mode == "off":
            return True
        result = self.calculate(now)
        d = result.direction
        if d == DirectionState.NEUTRAL:
            return True
        if d == DirectionState.UNKNOWN:
            return self.mode == "shadow"
        if d == DirectionState.TRANSITION:
            return self.mode == "shadow"
        if self.mode == "enforce":
            ol = signal.get("outcome_label", "")
            if d == DirectionState.UP and ol != "Up":
                return False
            if d == DirectionState.DOWN and ol != "Down":
                return False
        return True

    def record_shadow_candidate(self, signal: Dict[str, Any], was_filtered: bool, assumed_pnl: float = 0.0) -> None:
        stats = self._shadow_stats
        if was_filtered:
            stats["filtered_count"] += 1
            stats["filtered_assumed_pnl"] += assumed_pnl
            stats["filtered_assumed_count"] += 1
            self._shadow_filtered_signals.append({
                "t": datetime.now(timezone.utc).isoformat(),
                "slug": signal.get("slug"),
                "outcome_label": signal.get("outcome_label"),
                "edge_bps": signal.get("edge_bps"),
                "fair_selected": signal.get("fair_selected"),
                "current_ask": signal.get("current_ask"),
                "mte_minutes": signal.get("mte_minutes"),
                "assumed_pnl": assumed_pnl,
            })
            if len(self._shadow_filtered_signals) > 1000:
                self._shadow_filtered_signals = self._shadow_filtered_signals[-500:]
        else:
            stats["allowed_count"] += 1
        stats["total_candidates"] += 1

    def get_stats(self) -> Dict[str, Any]:
        stats = dict(self._shadow_stats)
        stats["current_direction"] = self._last_direction.value
        stats["transition_target"] = self._transition_target.value if self._transition_target else None
        stats["confirm_count"] = self._confirm_count
        return stats

    def _do_calculate(self) -> DirectionResult:
        if not self._history:
            return DirectionResult(direction=DirectionState.UNKNOWN, stale_seconds=self.max_stale_seconds + 1)

        now_ts = time.time()
        latest = self._history[-1]
        latest_ts = latest.get("ts", now_ts)
        stale = now_ts - latest_ts

        if stale > self.max_stale_seconds:
            return DirectionResult(direction=DirectionState.UNKNOWN, stale_seconds=stale,
                                   data_points_15m=len(self._history), data_points_60m=len(self._history))

        cutoff_15m = latest_ts - 900
        cutoff_60m = latest_ts - 3600
        price_now = latest["price"]
        price_15m = None
        price_60m = None
        pts_15m = 0
        pts_60m = 0

        for tick in reversed(self._history):
            ts = tick.get("ts", 0)
            if ts >= cutoff_15m:
                pts_15m += 1
            if ts >= cutoff_60m:
                pts_60m += 1
            # 找历史价格：从后往前找第一个 ts <= cutoff 的
            if price_15m is None and ts <= cutoff_15m:
                price_15m = tick["price"]
            if price_60m is None and ts <= cutoff_60m:
                price_60m = tick["price"]

        pct_15m = 0.0
        pct_60m = 0.0
        if price_15m and price_15m > 0:
            pct_15m = (price_now - price_15m) / price_15m * 100.0
        if price_60m and price_60m > 0:
            pct_60m = (price_now - price_60m) / price_60m * 100.0

        if pts_15m < 2 or pts_60m < 2:
            return DirectionResult(direction=DirectionState.UNKNOWN,
                                   pct_15m=round(pct_15m, 4), pct_60m=round(pct_60m, 4),
                                   data_points_15m=pts_15m, data_points_60m=pts_60m,
                                   stale_seconds=round(stale, 1))

        bps_15m = pct_15m * 100
        bps_60m = pct_60m * 100

        if bps_60m >= self.threshold_60m_bps and bps_15m >= self.threshold_15m_bps:
            direction = DirectionState.UP
        elif bps_60m <= -self.threshold_60m_bps and bps_15m <= -self.threshold_15m_bps:
            direction = DirectionState.DOWN
        else:
            direction = DirectionState.NEUTRAL

        return DirectionResult(direction=direction, pct_15m=round(pct_15m, 4), pct_60m=round(pct_60m, 4),
                               data_points_15m=pts_15m, data_points_60m=pts_60m, stale_seconds=round(stale, 1))

    def _update_state_machine(self, result: DirectionResult) -> None:
        current = self._last_direction
        new_dir = result.direction

        if new_dir == DirectionState.UNKNOWN:
            self._confirm_count = 0
            self._transition_target = None
            return

        if self._transition_target is not None:
            if new_dir == self._transition_target:
                self._confirm_count += 1
                if self._confirm_count >= self.confirmations:
                    self._last_direction = self._transition_target
                    self._transition_target = None
                    self._confirm_count = 0
            else:
                self._transition_target = None
                self._confirm_count = 0
                if new_dir != current:
                    self._confirm_count = 1
                    self._transition_target = new_dir
                    self._last_direction = DirectionState.TRANSITION
                else:
                    self._last_direction = current
        else:
            if new_dir == current:
                self._confirm_count += 1
                if self._confirm_count < self.confirmations:
                    self._transition_target = new_dir
                    self._last_direction = DirectionState.TRANSITION
            else:
                if current == DirectionState.UNKNOWN:
                    self._confirm_count = 1
                    self._last_direction = new_dir
                else:
                    self._confirm_count = 1
                    self._transition_target = new_dir
                    self._last_direction = DirectionState.TRANSITION
        result.confirmed_count = self._confirm_count

    def _cached_result(self, now: float) -> DirectionResult:
        return DirectionResult(direction=self._last_direction,
                               stale_seconds=now - self._last_calc_time,
                               confirmed_count=self._confirm_count)

    def _log_result(self, result: DirectionResult) -> None:
        if not self.log_file:
            return
        entry = {"t": datetime.now(timezone.utc).isoformat(), "direction": result.direction.value,
                 "pct_15m": result.pct_15m, "pct_60m": result.pct_60m,
                 "data_points_15m": result.data_points_15m, "data_points_60m": result.data_points_60m,
                 "stale_seconds": result.stale_seconds, "confirmed_count": result.confirmed_count,
                 "mode": self.mode}
        try:
            with open(self.log_file, "a") as f:
                f.write(json.dumps(entry) + "\n")
                f.flush()
        except (OSError, IOError) as e:
            logger.debug("Failed to write direction log: %s", e)

    def _write_status(self, result: DirectionResult) -> None:
        if not self.status_file:
            return
        try:
            status = {}
            if os.path.exists(self.status_file):
                with open(self.status_file, "r") as f:
                    try:
                        status = json.load(f)
                    except json.JSONDecodeError:
                        pass
            status["direction"] = result.direction.value
            status["direction_pct_15m"] = result.pct_15m
            status["direction_pct_60m"] = result.pct_60m
            status["direction_stale_seconds"] = result.stale_seconds
            status["direction_confirmed"] = result.confirmed_count
            status["direction_mode"] = self.mode
            status["direction_updated_at"] = datetime.now(timezone.utc).isoformat()
            with open(self.status_file, "w") as f:
                json.dump(status, f, indent=2, ensure_ascii=False)
                f.flush()
        except (OSError, IOError) as e:
            logger.debug("Failed to write direction status: %s", e)
