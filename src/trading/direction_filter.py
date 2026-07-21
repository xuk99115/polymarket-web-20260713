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
            cached = self._cached_result(now)
            self._write_cached_status(cached)
            return cached
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

    def evaluate_trade(self, signal: Dict[str, Any], now: Optional[float] = None) -> Dict[str, Any]:
        """Return the enforce counterfactual without changing shadow execution."""
        evaluated_at = datetime.fromtimestamp(now or time.time(), tz=timezone.utc)
        result = self.calculate(now)
        direction = result.direction
        outcome_label = signal.get("outcome_label", "")
        would_allow = (
            direction == DirectionState.NEUTRAL
            or (direction == DirectionState.UP and outcome_label == "Up")
            or (direction == DirectionState.DOWN and outcome_label == "Down")
        )
        return {
            "direction_mode": self.mode,
            "direction_gate": direction.value,
            "direction_would_allow": would_allow,
            "direction_evaluated_at": evaluated_at.isoformat(),
            "direction_pct_15m": result.pct_15m,
            "direction_pct_60m": result.pct_60m,
            "direction_stale_seconds": result.stale_seconds,
            "direction_confirmed": result.confirmed_count,
        }

    def should_allow_trade(self, signal: Dict[str, Any], now: Optional[float] = None) -> bool:
        if self.mode in {"off", "shadow"}:
            return True
        return self.evaluate_trade(signal, now)["direction_would_allow"]

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
            # 找历史价格：从后往前找第一个 ts < cutoff 的
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


        # 如果找不到历史价格（窗口太窄），返回 UNKNOWN
        if price_15m is None or price_60m is None:
            return DirectionResult(
                direction=DirectionState.UNKNOWN,
                pct_15m=round(pct_15m, 4),
                pct_60m=round(pct_60m, 4),
                data_points_15m=pts_15m,
                data_points_60m=pts_60m,
                stale_seconds=round(stale, 1),
            )
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
        """两次确认的安全状态机。"""
        current = self._last_direction
        new_dir = result.direction

        # stale/窗口不足等 UNKNOWN 必须立刻撤销旧门控状态。
        if new_dir == DirectionState.UNKNOWN:
            self._confirm_count = 0
            self._transition_target = None
            self._last_direction = DirectionState.UNKNOWN
            result.confirmed_count = 0
            return

        # 冷启动：首次任何可识别方向先进入 TRANSITION，绝不直接放行。
        if current == DirectionState.UNKNOWN:
            self._confirm_count = 1
            self._transition_target = new_dir
            self._last_direction = DirectionState.TRANSITION
            result.confirmed_count = self._confirm_count
            return

        # 反转确认中：连续同一 raw direction 达到 confirmations 才切换。
        if self._transition_target is not None:
            if new_dir == self._transition_target:
                self._confirm_count += 1
                if self._confirm_count >= self.confirmations:
                    self._last_direction = self._transition_target
                    self._transition_target = None
                    self._confirm_count = 0
            else:
                # raw 方向改变：以新方向重新开始确认，不沿用旧计数。
                self._confirm_count = 1
                self._transition_target = new_dir
                self._last_direction = DirectionState.TRANSITION
            result.confirmed_count = self._confirm_count
            return

        if new_dir == current:
            # 已确认方向遇到同向 raw：保持，不得回跳 TRANSITION。
            self._confirm_count = 0
            result.confirmed_count = 0
            return

        # 已确认方向发生变化：进入 TRANSITION，等待连续确认。
        self._confirm_count = 1
        self._transition_target = new_dir
        self._last_direction = DirectionState.TRANSITION
        result.confirmed_count = self._confirm_count

    def _write_cached_status(self, result: DirectionResult) -> None:
        """写入缓存结果的状态（仅更新 timestamp）。"""
        runtime_status = os.path.join(
            os.environ.get("RUNTIME_DIR", "/tmp/polymarket-fv-edge/data"),
            "direction_state.json",
        )
        try:
            cached_data = {}
            if os.path.exists(runtime_status):
                with open(runtime_status, "r") as f:
                    try:
                        cached_data = json.load(f)
                    except json.JSONDecodeError:
                        pass
            cached_data["direction_updated_at"] = datetime.now(timezone.utc).isoformat()
            with open(runtime_status, "w") as f:
                json.dump(cached_data, f, indent=2, ensure_ascii=False)
                f.flush()
        except (OSError, IOError) as e:
            logger.warning("Failed to write cached direction state: %s", e)

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
        """写入方向状态到运行时目录和持久卷。"""
        direction_data = {
            "direction": result.direction.value,
            "direction_pct_15m": result.pct_15m,
            "direction_pct_60m": result.pct_60m,
            "direction_stale_seconds": result.stale_seconds,
            "direction_confirmed": result.confirmed_count,
            "direction_mode": self.mode,
            "direction_updated_at": datetime.now(timezone.utc).isoformat(),
        }

        # 写入独立方向状态文件（运行时目录，避免 EIO）
        runtime_status = os.path.join(
            os.environ.get("RUNTIME_DIR", "/tmp/polymarket-fv-edge/data"),
            "direction_state.json",
        )
        try:
            with open(runtime_status, "w") as f:
                json.dump(direction_data, f, indent=2, ensure_ascii=False)
                f.flush()
        except (OSError, IOError) as e:
            logger.warning("Failed to write direction runtime state: %s", e)

        # 写入 bot_status.json（增量更新）
        if self.status_file:
            try:
                status = {}
                if os.path.exists(self.status_file):
                    with open(self.status_file, "r") as f:
                        try:
                            status = json.load(f)
                        except json.JSONDecodeError:
                            pass
                status.update(direction_data)
                with open(self.status_file, "w") as f:
                    json.dump(status, f, indent=2, ensure_ascii=False)
                    f.flush()
            except (OSError, IOError) as e:
                logger.warning("Failed to write direction status to bot_status.json: %s", e)
