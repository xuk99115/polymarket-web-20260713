"""BTC 方向过滤器 — 基于 Chainlink BTC 价格计算趋势方向。

功能：
- 计算最近 15 分钟和 60 分钟的 BTC 收益率
- 根据阈值判断方向：UP / DOWN / NEUTRAL / UNKNOWN / TRANSITION
- 支持 shadow（只记录）和 enforce（限制交易）两种模式
- 状态机：连续两次确认才切换方向，反转走 TRANSITION 过渡
- shadow 模式记录被过滤交易详情用于对比统计
"""

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
    """单次方向计算结果。"""
    direction: DirectionState
    pct_15m: float = 0.0
    pct_60m: float = 0.0
    data_points_15m: int = 0
    data_points_60m: int = 0
    stale_seconds: float = 0.0
    confirmed_count: int = 0


@dataclass
class DirectionFilter:
    """方向过滤器。"""
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
        """设置历史价格数据。"""
        self._history = list(ticks)

    def calculate(self, now: Optional[float] = None) -> DirectionResult:
        """计算当前方向（含状态机）。

        返回的是状态机确认后的 effective direction，用于交易门控。
        日志/状态写入的是 raw direction（原始计算结果），用于审计。
        """
        now = now or time.time()

        if now - self._last_calc_time < self.update_seconds:
            return self._cached_result(now)

        self._last_calc_time = now
        raw_result = self._do_calculate()
        self._update_state_machine(raw_result)

        # 日志/状态写入 raw direction（可审计）
        self._log_result(raw_result)
        self._write_status(raw_result)

        # 返回 effective direction（用于门控）
        return DirectionResult(
            direction=self._last_direction,
            pct_15m=raw_result.pct_15m,
            pct_60m=raw_result.pct_60m,
            data_points_15m=raw_result.data_points_15m,
            data_points_60m=raw_result.data_points_60m,
            stale_seconds=raw_result.stale_seconds,
            confirmed_count=raw_result.confirmed_count,
        )

    def should_allow_trade(self, signal: Dict[str, Any], now: Optional[float] = None) -> bool:
        """判断是否允许交易信号通过。

        enforce 模式：
        - UNKNOWN → 禁止新开仓
        - TRANSITION → 禁止新开仓
        - UP → 只允许 Up
        - DOWN → 只允许 Down
        - NEUTRAL → 允许双向

        shadow 模式：
        - 所有方向都放行
        """
        if self.mode == "off":
            return True

        result = self.calculate(now)
        direction = result.direction

        if direction == DirectionState.NEUTRAL:
            return True

        if direction == DirectionState.UNKNOWN:
            if self.mode == "shadow":
                return True
            return False

        if direction == DirectionState.TRANSITION:
            if self.mode == "shadow":
                return True
            return False

        if self.mode == "enforce":
            outcome_label = signal.get("outcome_label", "")
            if direction == DirectionState.UP and outcome_label != "Up":
                return False
            if direction == DirectionState.DOWN and outcome_label != "Down":
                return False

        return True

    def record_shadow_candidate(self, signal: Dict[str, Any], was_filtered: bool, assumed_pnl: float = 0.0) -> None:
        """记录 shadow 模式下的候选交易。

        当 direction_filter 存在且 mode=shadow 时，
        对每个通过边缘筛选的信号调用此方法：
        - was_filtered=True: 被方向过滤拦截，记录假设 PnL
        - was_filtered=False: 方向过滤放行，正常记录
        """
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
        """获取 shadow 模式下的统计信息。"""
        stats = dict(self._shadow_stats)
        stats["current_direction"] = self._last_direction.value
        stats["transition_target"] = (
            self._transition_target.value if self._transition_target else None
        )
        stats["confirm_count"] = self._confirm_count
        return stats

    def _do_calculate(self) -> DirectionResult:
        """执行原始方向计算（不含状态机）。

        关键修复：
        - 检查价格窗口是否真正覆盖 15m/60m 范围，而非仅仅数据点数量
        - 数据失效（stale）后返回 UNKNOWN
        """
        if not self._history:
            return DirectionResult(
                direction=DirectionState.UNKNOWN,
                stale_seconds=self.max_stale_seconds + 1,
            )

        now_ts = time.time()
        latest = self._history[-1]
        latest_ts = latest.get("ts", now_ts)
        stale = now_ts - latest_ts

        if stale > self.max_stale_seconds:
            return DirectionResult(
                direction=DirectionState.UNKNOWN,
                stale_seconds=stale,
                data_points_15m=len(self._history),
                data_points_60m=len(self._history),
            )

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

        # 冷启动保护：
        # 1. 至少需要 2 个数据点
        # 2. 必须真正覆盖 15m/60m 时间窗口（有历史价格）
        if pts_15m < 2 or pts_60m < 2:
            return DirectionResult(
                direction=DirectionState.UNKNOWN,
                pct_15m=round(pct_15m, 4),
                pct_60m=round(pct_60m, 4),
                data_points_15m=pts_15m,
                data_points_60m=pts_60m,
                stale_seconds=round(stale, 1),
            )

        # 关键修复：如果找不到历史价格（窗口太窄），返回 UNKNOWN
        if price_15m is None or price_60m is None:
            return DirectionResult(
                direction=DirectionState.UNKNOWN,
                pct_15m=round(pct_15m, 4),
                pct_60m=round(pct_60m, 4),
                data_points_15m=pts_15m,
                data_points_60m=pts_60m,
                stale_seconds=round(stale, 1),
            )

        bps_15m = pct_15m * 100
        bps_60m = pct_60m * 100

        if bps_60m >= self.threshold_60m_bps and bps_15m >= self.threshold_15m_bps:
            direction = DirectionState.UP
        elif bps_60m <= -self.threshold_60m_bps and bps_15m <= -self.threshold_15m_bps:
            direction = DirectionState.DOWN
        else:
            direction = DirectionState.NEUTRAL

        return DirectionResult(
            direction=direction,
            pct_15m=round(pct_15m, 4),
            pct_60m=round(pct_60m, 4),
            data_points_15m=pts_15m,
            data_points_60m=pts_60m,
            stale_seconds=round(stale, 1),
        )

    def _update_state_machine(self, result: DirectionResult) -> None:
        """更新方向状态机。

        规则：
        - 每 update_seconds 计算一次方向
        - 连续 confirmations 次结果一致才切换
        - 确认结果包括 UP、DOWN、NEUTRAL（不只有方向变化才算）
        - 反转走 TRANSITION 过渡，不停已有仓位
        - UNKNOWN 重置状态机
        - 已确认方向后，相同方向不再进入 TRANSITION
        """
        current = self._last_direction
        new_dir = result.direction

        # UNKNOWN → 重置状态机（数据失效）
        if new_dir == DirectionState.UNKNOWN:
            self._confirm_count = 0
            self._transition_target = None
            self._last_direction = DirectionState.UNKNOWN
            return

        # 正在 TRANSITION 中
        if self._transition_target is not None:
            if new_dir == self._transition_target:
                self._confirm_count += 1
                if self._confirm_count >= self.confirmations:
                    # 确认切换
                    self._last_direction = self._transition_target
                    self._transition_target = None
                    self._confirm_count = 0
            else:
                # 方向变了，取消本次反转确认
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
                # 方向一致，增加确认计数
                self._confirm_count += 1
                if self._confirm_count < self.confirmations:
                    # 还没够确认次数，显示为 TRANSITION
                    self._transition_target = new_dir
                    self._last_direction = DirectionState.TRANSITION
            else:
                # 方向变化
                if current == DirectionState.UNKNOWN:
                    # 首次确认：直接设为新方向，不经过 TRANSITION
                    self._confirm_count = 1
                    self._last_direction = new_dir
                else:
                    # 方向变化：开始新的确认
                    self._confirm_count = 1
                    self._transition_target = new_dir
                    self._last_direction = DirectionState.TRANSITION

        result.confirmed_count = self._confirm_count

    def _cached_result(self, now: float) -> DirectionResult:
        """缓存结果（未到达 update_seconds 时返回）。"""
        return DirectionResult(
            direction=self._last_direction,
            stale_seconds=now - self._last_calc_time,
            confirmed_count=self._confirm_count,
        )

    def _log_result(self, result: DirectionResult) -> None:
        """记录审计日志（写入 raw direction）。"""
        if not self.log_file:
            return
        log_entry = {
            "t": datetime.now(timezone.utc).isoformat(),
            "direction": result.direction.value,
            "pct_15m": result.pct_15m,
            "pct_60m": result.pct_60m,
            "data_points_15m": result.data_points_15m,
            "data_points_60m": result.data_points_60m,
            "stale_seconds": result.stale_seconds,
            "confirmed_count": result.confirmed_count,
            "mode": self.mode,
        }
        try:
            with open(self.log_file, "a") as f:
                f.write(json.dumps(log_entry) + "\n")
                f.flush()
        except (OSError, IOError) as e:
            logger.debug("Failed to write direction log: %s", e)

    def _write_status(self, result: DirectionResult) -> None:
        """写入方向状态到 bot_status.json。

        关键修复：增量写入，不覆盖其他字段。
        """
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
