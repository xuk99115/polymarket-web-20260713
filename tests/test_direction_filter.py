"""方向过滤器单元测试。"""

import os
import sys
import time

_project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

from trading.direction_filter import DirectionFilter, DirectionState


def _mk(now, prices_offsets):
    """创建 ticks：latest at now, others at now-offset."""
    ticks = []
    for i, (p, o) in enumerate(prices_offsets):
        if i == len(prices_offsets) - 1:
            ticks.append({"ts": now, "price": p})
        else:
            ticks.append({"ts": now - o, "price": p})
    ticks.sort(key=lambda x: x["ts"])
    return ticks


# ── 方向计算测试 ───────────────────────────────────────────

def test_up_trend():
    now = time.time()
    f = DirectionFilter(mode="shadow"); f.set_history(_mk(now, [(100.0,3600),(100.5,900),(101.0,0)]))
    r = f.calculate()
    assert r.direction == DirectionState.UP, f"UP: {r.direction} 60m={r.pct_60m}% 15m={r.pct_15m}%"


def test_down_trend():
    now = time.time()
    f = DirectionFilter(mode="shadow"); f.set_history(_mk(now, [(100.0,3600),(99.5,900),(99.0,0)]))
    r = f.calculate()
    assert r.direction == DirectionState.DOWN, f"DOWN: {r.direction}"


def test_neutral_60m_below_threshold():
    now = time.time()
    f = DirectionFilter(mode="shadow"); f.set_history(_mk(now, [(100.0,3600),(100.5,900),(100.015,0)]))
    r = f.calculate()
    assert r.direction == DirectionState.NEUTRAL


def test_neutral_15m_below_threshold():
    now = time.time()
    f = DirectionFilter(mode="shadow"); f.set_history(_mk(now, [(100.0,3600),(100.95,900),(100.93,0)]))
    r = f.calculate()
    assert r.direction == DirectionState.NEUTRAL, f"NEUTRAL: {r.direction} 60m={r.pct_60m}% 15m={r.pct_15m}%"


def test_unknown_no_data():
    f = DirectionFilter(mode="shadow"); f.set_history([])
    assert f.calculate().direction == DirectionState.UNKNOWN


def test_unknown_stale_data():
    now = time.time()
    f = DirectionFilter(mode="shadow"); f.set_history([{"ts": now-2000, "price": 100.0}])
    assert f.calculate().direction == DirectionState.UNKNOWN


def test_cold_start_single_tick():
    now = time.time()
    f = DirectionFilter(mode="shadow"); f.set_history([{"ts": now-1, "price": 100.0}])
    assert f.calculate().direction == DirectionState.UNKNOWN, f"Expected UNKNOWN for cold start, got {f.calculate().direction}"


def test_up_strict_same_direction():
    now = time.time()
    f = DirectionFilter(mode="shadow"); f.set_history(_mk(now, [(100.0,3600),(100.95,900),(100.90,0)]))
    r = f.calculate()
    assert r.direction == DirectionState.NEUTRAL, f"NEUTRAL: {r.direction} 60m={r.pct_60m}% 15m={r.pct_15m}%"


def test_cold_start_insufficient_window():
    """关键修复：只有 2 个很近的点（都在 15m 内），找不到 60m 历史价格 → UNKNOWN"""
    now = time.time()
    ticks = [{"ts": now - 100, "price": 100.0}, {"ts": now, "price": 101.0}]
    f = DirectionFilter(mode="shadow"); f.set_history(ticks)
    r = f.calculate()
    assert r.direction == DirectionState.UNKNOWN, f"Expected UNKNOWN (insufficient window), got {r.direction} (pts_15m={r.data_points_15m}, pts_60m={r.data_points_60m})"


# ── 状态机测试 ─────────────────────────────────────────────

def test_confirm_twice():
    """首次确认直接返回方向（不走 TRANSITION），第二次相同方向 confirm_count++"""
    now = time.time()
    ticks = _mk(now, [(100.0,3600),(100.5,900),(101.0,0)])
    f = DirectionFilter(mode="shadow", update_seconds=1)
    f.set_history(ticks)
    r1 = f.calculate(now=now)
    assert r1.direction == DirectionState.UP, f"First calc should be UP (initial), got {r1.direction}"
    r2 = f.calculate(now=now + 1)
    assert r2.direction == DirectionState.UP


def test_transition_then_confirm():
    now = time.time()
    ticks_up = _mk(now, [(100.0,3600),(100.5,900),(101.0,0)])
    f = DirectionFilter(mode="enforce", update_seconds=1)
    f.set_history(ticks_up)
    r1 = f.calculate(now=now)
    assert r1.direction == DirectionState.UP
    
    ticks_down = _mk(now + 1, [(100.0,3600),(99.5,900),(99.0,0)])
    f.set_history(ticks_down)
    r2 = f.calculate(now=now + 1)
    assert r2.direction == DirectionState.TRANSITION
    
    r3 = f.calculate(now=now + 2)
    assert r3.direction == DirectionState.DOWN


def test_transition_cancel_on_up():
    now = time.time()
    ticks_up = _mk(now, [(100.0,3600),(100.5,900),(101.0,0)])
    f = DirectionFilter(mode="enforce", update_seconds=1)
    f.set_history(ticks_up)
    r1 = f.calculate(now=now)
    assert r1.direction == DirectionState.UP
    
    ticks_down = _mk(now + 1, [(100.0,3600),(99.5,900),(99.0,0)])
    f.set_history(ticks_down)
    r2 = f.calculate(now=now + 1)
    assert r2.direction == DirectionState.TRANSITION
    
    # 恢复 UP → 取消反转，重新确认
    f.set_history(ticks_up)
    r3 = f.calculate(now=now + 2)
    assert r3.direction == DirectionState.TRANSITION
    r4 = f.calculate(now=now + 3)
    assert r4.direction == DirectionState.UP


def test_data_stale_resets_to_unknown():
    """数据失效后应重置为 UNKNOWN，而非沿用旧方向"""
    now = time.time()
    ticks_up = _mk(now, [(100.0,3600),(100.5,900),(101.0,0)])
    f = DirectionFilter(mode="enforce", update_seconds=1)
    f.set_history(ticks_up)
    r1 = f.calculate(now=now)
    assert r1.direction == DirectionState.UP
    
    # 数据过期（stale > 900s）
    stale_ticks = [{"ts": now - 2000, "price": 100.0}]
    f.set_history(stale_ticks)
    r2 = f.calculate(now=now + 1)
    assert r2.direction == DirectionState.UNKNOWN, f"Expected UNKNOWN for stale data, got {r2.direction}"


def test_confirmed_direction_not_reset_by_same():
    """已确认 UP 后，相同方向不应再次进入 TRANSITION"""
    now = time.time()
    ticks = _mk(now, [(100.0,3600),(100.5,900),(101.0,0)])
    f = DirectionFilter(mode="enforce", update_seconds=1)
    f.set_history(ticks)
    r1 = f.calculate(now=now)
    assert r1.direction == DirectionState.UP
    
    r2 = f.calculate(now=now + 1)
    assert r2.direction == DirectionState.UP, f"Same direction after confirm should be UP, not TRANSITION. Got {r2.direction}"


# ── 交易过滤测试 ───────────────────────────────────────────

def test_shadow_does_not_block():
    f = DirectionFilter(mode="shadow")
    signal = {"outcome_label": "Up", "slug": "test"}
    assert f.should_allow_trade(signal) is True


def test_enforce_unknown_blocks():
    f = DirectionFilter(mode="enforce")
    f.set_history([])
    signal = {"outcome_label": "Up", "slug": "test"}
    result = f.calculate()
    assert result.direction == DirectionState.UNKNOWN
    assert f.should_allow_trade(signal) is False


def test_enforce_transition_blocks():
    now = time.time()
    ticks_up = _mk(now, [(100.0,3600),(100.5,900),(101.0,0)])
    f = DirectionFilter(mode="enforce", update_seconds=1)
    f.set_history(ticks_up)
    r1 = f.calculate(now=now)
    assert r1.direction == DirectionState.UP
    
    ticks_down = _mk(now + 1, [(100.0,3600),(99.5,900),(99.0,0)])
    f.set_history(ticks_down)
    r2 = f.calculate(now=now + 1)
    assert r2.direction == DirectionState.TRANSITION
    
    signal = {"outcome_label": "Down", "slug": "test"}
    assert f.should_allow_trade(signal) is False


def test_neutral_allows_both():
    now = time.time()
    ticks = _mk(now, [(100.0,3600),(100.95,900),(100.93,0)])
    f = DirectionFilter(mode="enforce")
    f.set_history(ticks)
    
    up_signal = {"outcome_label": "Up", "slug": "test"}
    down_signal = {"outcome_label": "Down", "slug": "test"}
    
    r = f.calculate(now=now)
    # NEUTRAL 时 should_allow_trade 直接返回 True
    assert f.should_allow_trade(up_signal) is True
    assert f.should_allow_trade(down_signal) is True


def test_up_enforce_blocks_down():
    now = time.time()
    ticks = _mk(now, [(100.0,3600),(100.5,900),(101.0,0)])
    f = DirectionFilter(mode="enforce")
    f.set_history(ticks)
    
    f.calculate(now=now)
    f.calculate(now=now + 1)
    
    up_signal = {"outcome_label": "Up", "slug": "test"}
    down_signal = {"outcome_label": "Down", "slug": "test"}
    
    assert f.should_allow_trade(up_signal) is True
    assert f.should_allow_trade(down_signal) is False


def test_integration_methods_exist():
    f = DirectionFilter(mode="shadow")
    for m in ['should_allow_trade', 'calculate', 'set_history', 'record_shadow_candidate', 'get_stats']:
        assert hasattr(f, m)


if __name__ == "__main__":
    tests = [
        test_up_trend, test_down_trend,
        test_neutral_60m_below_threshold, test_neutral_15m_below_threshold,
        test_unknown_no_data, test_unknown_stale_data, test_cold_start_single_tick,
        test_up_strict_same_direction, test_cold_start_insufficient_window,
        test_confirm_twice, test_transition_then_confirm, test_transition_cancel_on_up,
        test_data_stale_resets_to_unknown, test_confirmed_direction_not_reset_by_same,
        test_shadow_does_not_block, test_enforce_unknown_blocks,
        test_enforce_transition_blocks, test_neutral_allows_both,
        test_up_enforce_blocks_down,
        test_integration_methods_exist,
    ]
    passed = failed = 0
    for t in tests:
        try: t(); print(f"  ✓ {t.__name__}"); passed += 1
        except Exception as e: print(f"  ✗ {t.__name__}: {e}"); failed += 1
    print(f"\n{passed}/{passed+failed} passed")
    sys.exit(0 if failed == 0 else 1)
