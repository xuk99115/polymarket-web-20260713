"""方向过滤器单元测试。"""

import os
import sys
import time

_project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

from trading.direction_filter import DirectionFilter, DirectionState


def _mk(now, prices_offsets):
    """latest at now, others at now-offset."""
    ticks = []
    for i, (p, o) in enumerate(prices_offsets):
        if i == len(prices_offsets) - 1:
            ticks.append({"ts": now, "price": p})
        else:
            ticks.append({"ts": now - o, "price": p})
    ticks.sort(key=lambda x: x["ts"])
    return ticks


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
    assert f.calculate().direction == DirectionState.UNKNOWN


def test_up_strict_same_direction():
    now = time.time()
    f = DirectionFilter(mode="shadow"); f.set_history(_mk(now, [(100.0,3600),(100.95,900),(100.90,0)]))
    r = f.calculate()
    assert r.direction == DirectionState.NEUTRAL, f"NEUTRAL: {r.direction} 60m={r.pct_60m}% 15m={r.pct_15m}%"


def test_confirm_twice():
    now = time.time()
    f = DirectionFilter(mode="shadow", update_seconds=1); f.set_history(_mk(now, [(100.0,3600),(100.5,900),(101.0,0)]))
    assert f.calculate(now=now).direction == DirectionState.UP
    assert f.calculate(now=now+1).direction == DirectionState.UP


def test_transition_then_confirm():
    now = time.time()
    f = DirectionFilter(mode="enforce", update_seconds=1); f.set_history(_mk(now, [(100.0,3600),(100.5,900),(101.0,0)]))
    assert f.calculate(now=now).direction == DirectionState.UP
    f.set_history(_mk(now+1, [(100.0,3600),(99.5,900),(99.0,0)]))
    assert f.calculate(now=now+1).direction == DirectionState.TRANSITION
    assert f.calculate(now=now+2).direction == DirectionState.DOWN


def test_transition_cancel_on_up():
    now = time.time()
    f = DirectionFilter(mode="enforce", update_seconds=1); f.set_history(_mk(now, [(100.0,3600),(100.5,900),(101.0,0)]))
    assert f.calculate(now=now).direction == DirectionState.UP
    f.set_history(_mk(now+1, [(100.0,3600),(99.5,900),(99.0,0)]))
    assert f.calculate(now=now+1).direction == DirectionState.TRANSITION
    f.set_history(_mk(now+2, [(100.0,3600),(100.5,900),(101.0,0)]))
    assert f.calculate(now=now+2).direction == DirectionState.TRANSITION
    assert f.calculate(now=now+3).direction == DirectionState.UP


def test_shadow_does_not_block():
    f = DirectionFilter(mode="shadow")
    assert f.should_allow_trade({"outcome_label": "Up"}) is True


def test_enforce_unknown_blocks():
    f = DirectionFilter(mode="enforce"); f.set_history([])
    assert f.calculate().direction == DirectionState.UNKNOWN
    assert f.should_allow_trade({"outcome_label": "Up"}) is False


def test_enforce_transition_blocks():
    now = time.time()
    f = DirectionFilter(mode="enforce", update_seconds=1); f.set_history(_mk(now, [(100.0,3600),(100.5,900),(101.0,0)]))
    assert f.calculate(now=now).direction == DirectionState.UP
    f.set_history(_mk(now+1, [(100.0,3600),(99.5,900),(99.0,0)]))
    assert f.calculate(now=now+1).direction == DirectionState.TRANSITION
    assert f.should_allow_trade({"outcome_label": "Down"}) is False


def test_neutral_allows_both():
    now = time.time()
    f = DirectionFilter(mode="enforce"); f.set_history(_mk(now, [(100.0,3600),(100.95,900),(100.93,0)]))
    f.calculate(now=now)
    assert f.should_allow_trade({"outcome_label": "Up"}) is True
    assert f.should_allow_trade({"outcome_label": "Down"}) is True


def test_up_enforce_blocks_down():
    now = time.time()
    f = DirectionFilter(mode="enforce"); f.set_history(_mk(now, [(100.0,3600),(100.5,900),(101.0,0)]))
    f.calculate(now=now); f.calculate(now=now+1)
    assert f.should_allow_trade({"outcome_label": "Up"}) is True
    assert f.should_allow_trade({"outcome_label": "Down"}) is False


def test_integration_methods_exist():
    f = DirectionFilter(mode="shadow")
    for m in ['should_allow_trade', 'calculate', 'set_history', 'record_shadow_candidate', 'get_stats']:
        assert hasattr(f, m)


if __name__ == "__main__":
    tests = [test_up_trend, test_down_trend, test_neutral_60m_below_threshold, test_neutral_15m_below_threshold,
             test_unknown_no_data, test_unknown_stale_data, test_cold_start_single_tick, test_up_strict_same_direction,
             test_confirm_twice, test_transition_then_confirm, test_transition_cancel_on_up,
             test_shadow_does_not_block, test_enforce_unknown_blocks, test_enforce_transition_blocks,
             test_neutral_allows_both, test_up_enforce_blocks_down, test_integration_methods_exist]
    passed = failed = 0
    for t in tests:
        try: t(); print(f"  ✓ {t.__name__}"); passed += 1
        except Exception as e: print(f"  ✗ {t.__name__}: {e}"); failed += 1
    print(f"\n{passed}/{passed+failed} passed")
    sys.exit(0 if failed == 0 else 1)
