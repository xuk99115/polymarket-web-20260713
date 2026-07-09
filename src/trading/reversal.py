"""
K-line 反转检测引擎 — BTC 15m 盘口尾盘反转策略。

原理
----
BTC 15m 窗口走到最后 30 秒时，若此前有明确趋势但 K 线突然反转，
Polymarket 盘口因为价格更新延迟 20-30 秒，反转方的价格仍很低。
此时买入反转方，窗口关闭后大概率获利。

使用方式
--------
engine = ReversalEngine()
engine.push_price(time, price)    # 每收到一个 BTC 报价就 push
signal = engine.check(slug, end_date, outcomes)
if signal:
    # signal = {"action": "BUY", "outcome_index": 0|1, "price": 0.35, ...}
"""

import logging
import time as _time
from collections import deque
from datetime import datetime, timezone
from typing import Optional, Dict, Any, List, Tuple

logger = logging.getLogger("reversal")

# ============================================================
# 可调参数
# ============================================================
TREND_THRESHOLD = 0.15           # 趋势判定阈值 (%)
REVERSAL_THRESHOLD = 0.08       # 反转判定阈值 (%)
MAX_REVERSAL_PRICE = 0.45       # 反转方最高买入价 (¢)
FINAL_WINDOW_SECS = 30          # 最后 N 秒进入 FINAL 阶段
TREND_OBSERVATION_RATIO = 0.35  # 前 35% 时间用于确定趋势
MAX_PRICE_HISTORY = 300         # 最多保留最近 300 个报价 (15min @ 1/s)


def _compute_trend(prices: List[float], threshold: float) -> Optional[str]:
    """根据价格列表判定趋势方向：'up', 'down', 或 None（无明确趋势）"""
    if len(prices) < 5:
        return None
    first = prices[0]
    last = prices[-1]
    if first == 0:
        return None
    change_pct = (last - first) / first * 100
    if change_pct > threshold:
        return "up"
    elif change_pct < -threshold:
        return "down"
    return None


def _detect_reversal(
    prices: List[float],
    trend: str,
    threshold: float,
) -> Tuple[bool, float]:
    """在 FINAL 阶段检查是否发生反转。

    Returns:
        (reversed, magnitude_pct)
        reversed = True 表示发生了反转
        magnitude_pct = 反向波动的幅度（百分比）
    """
    if len(prices) < 3:
        return False, 0.0

    # 取最后 N 个 tick 的平均值作为最新价格
    recent = prices[-min(5, len(prices)):]
    recent_avg = sum(recent) / len(recent)

    # 取前 N 个 tick 的平均值作为趋势基准
    early = prices[:min(5, len(prices))]
    early_avg = sum(early) / len(early)

    if early_avg == 0:
        return False, 0.0

    change_pct = (recent_avg - early_avg) / early_avg * 100

    if trend == "up" and change_pct < -threshold:
        return True, abs(change_pct)
    elif trend == "down" and change_pct > threshold:
        return True, abs(change_pct)

    return False, 0.0


class WindowTracker:
    """跟踪单个 BTC 15m 窗口的状态"""

    def __init__(self, slug: str, end_date: datetime):
        self.slug = slug
        self.end_date = end_date
        self.created_at = _time.monotonic()
        self.prices: List[float] = []
        self.trend: Optional[str] = None       # up / down
        self.peak_price: Optional[float] = None
        self.trough_price: Optional[float] = None
        self.signal_fired = False               # 防止重复触发

    @property
    def age_secs(self) -> float:
        return _time.monotonic() - self.created_at

    @property
    def window_duration(self) -> float:
        """窗口总长（秒）"""
        now_utc = datetime.now(timezone.utc)
        remaining = (self.end_date - now_utc).total_seconds()
        return remaining + self.age_secs

    @property
    def progress(self) -> float:
        """窗口进度 0.0~1.0"""
        total = self.window_duration
        if total <= 0:
            return 1.0
        age = self.age_secs
        return min(1.0, age / total)

    def push_price(self, price: float):
        self.prices.append(price)
        # 保持上限
        if len(self.prices) > MAX_PRICE_HISTORY:
            self.prices = self.prices[-MAX_PRICE_HISTORY:]

        # 更新 peak / trough
        if self.peak_price is None or price > self.peak_price:
            self.peak_price = price
        if self.trough_price is None or price < self.trough_price:
            self.trough_price = price

        # EARLY → MID 转移：收集足够数据后确定趋势
        if self.trend is None and self.progress > TREND_OBSERVATION_RATIO:
            self.trend = _compute_trend(self.prices, TREND_THRESHOLD)
            if self.trend:
                logger.info("[反转/%s] 趋势确定: %s (%.3f%%)",
                           self.slug[-8:], self.trend.upper(),
                           self._total_change())

    def _total_change(self) -> float:
        if len(self.prices) < 2 or self.prices[0] == 0:
            return 0.0
        return (self.prices[-1] - self.prices[0]) / self.prices[0] * 100

    def is_in_final(self, now_utc: datetime) -> bool:
        """是否进入最后 FINAL_WINDOW_SECS 秒"""
        remaining = (self.end_date - now_utc).total_seconds()
        return 0 < remaining <= FINAL_WINDOW_SECS

    def check_reversal(self, now_utc: datetime) -> Tuple[bool, float]:
        """检查反转信号。

        Returns:
            (fired, magnitude_pct)
        """
        if self.signal_fired:
            return False, 0.0
        if self.trend is None:
            return False, 0.0
        if not self.is_in_final(now_utc):
            return False, 0.0
        if len(self.prices) < 10:
            return False, 0.0

        # 取当前价格 vs 趋势起始前价格的差值
        fired, mag = _detect_reversal(self.prices, self.trend, REVERSAL_THRESHOLD)
        return fired, mag


class ReversalEngine:
    """反转检测引擎，只跟踪当前活跃的 BTC 15m 窗口，结束时自动切到下一个。"""

    def __init__(self):
        self._current: Optional[WindowTracker] = None
        self._signal: Optional[Dict[str, Any]] = None

    def push_price(self, price: float):
        """向当前窗口推送最新 BTC 价格。"""
        if self._current:
            self._current.push_price(price)

    def set_window(self, slug: str, end_date: datetime):
        """设置当前跟踪的窗口。如果 slug 变了则替换（窗口已切换）。"""
        if self._current and self._current.slug == slug and self._current.end_date == end_date:
            return  # 同一个窗口，不重置

        # 窗口已过期？替换为新窗口
        now_utc = datetime.now(timezone.utc)
        if self._current and self._current.end_date <= now_utc:
            logger.info("[反转] 窗口 %s 已结束，切换到 %s", self._current.slug[-8:], slug[-8:])
        elif self._current is None:
            logger.info("[反转] 启动跟踪窗口 %s, 结束 %s", slug[-8:], end_date.isoformat())
        else:
            logger.info("[反转] 窗口切换: %s → %s", self._current.slug[-8:], slug[-8:])

        self._current = WindowTracker(slug, end_date)
        self._signal = None

    def check(self, now_utc: datetime) -> Optional[Dict[str, Any]]:
        """检查当前窗口的反转信号。

        Returns:
            signal dict or None
        """
        if self._signal:
            return self._signal

        w = self._current
        if w is None:
            return None

        # 窗口已结束，清理
        if w.end_date <= now_utc:
            logger.debug("[反转] 窗口 %s 已过结束时间，等待下一个", w.slug[-8:])
            return None

        fired, mag = w.check_reversal(now_utc)
        if not fired:
            return None

        self._signal = {
            "action": "BUY",
            "strategy": "reversal",
            "slug": w.slug,
            "trend": w.trend,
            "magnitude_pct": round(mag, 3),
            "window_tracker": w,
        }
        logger.info("🔀 [反转/%s] 信号触发! trend=%s, 反转幅度=%.3f%%",
                   w.slug[-8:], w.trend or "?", mag)
        return self._signal

    def consume_signal(self) -> Optional[Dict[str, Any]]:
        """消费当前信号。"""
        sig = self._signal
        self._signal = None
        if sig and sig.get("window_tracker"):
            sig["window_tracker"].signal_fired = True
        return sig
