"""
LowBuy 窗口盘口时间序列采样器 (2026-06-27 部署, 跟仓位完全脱钩).

职责: 每 2 秒对所有活跃 BTC 15m 窗口采样盘口快照.
- 记录每个窗口的 best_bid / best_ask / spread / depth (Up + Down 两边)
- 跟仓位完全无关, 不读 state.trades, 不触发任何开仓/平仓
- 独立 JSONL 写入 data/market_ticks.jsonl
- 可独立启停 (关闭 bot 也能跑)

设计目标: 找出"盘口 < 50 美分区间的翻倍规律" — 也就是:
- 哪些 best_bid 价格区间最容易"从某价涨到 2×"
- TP 目标价 (entry × 2) 对应盘口的 hit ratio
- 时长分布 (从开仓到 hit TP 用了多久)
"""
from __future__ import annotations

import json
import logging
import os
import time as _time
from datetime import datetime, timezone
from threading import Lock
from typing import Any, Dict, List, Optional

logger = logging.getLogger("trading_manager")

# 采样频率 (秒) — 跟 bot 主循环对齐
SAMPLE_INTERVAL = 2.0

# 文件路径 (绝对路径, 不依赖 cwd)
_DATA_DIR = os.path.abspath(os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "data",
))
TICKS_FILE = os.path.join(_DATA_DIR, "market_ticks.jsonl")
DUALBUY_FILE = os.path.join(_DATA_DIR, "dualbuy_opportunities.jsonl")

# 写锁 (JSONL 追加写需单线程, 否则多线程交错)
_write_lock = Lock()


def _safe_float(val: Any) -> Optional[float]:
    if val is None or val == "":
        return None
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


def _extract_outcome_snapshot(outcome: Dict[str, Any]) -> Dict[str, Any]:
    """从一个 outcome dict 提取盘口快照.

    Polymarket CLOB outcome 通常包含:
    - best_bid / best_ask: top of book
    - bids / asks: 完整 depth list [{price, size}, ...]
    - price: last trade price (兜底)
    """
    bid = _safe_float(outcome.get("best_bid"))
    ask = _safe_float(outcome.get("best_ask"))
    if bid is None:
        bid = _safe_float(outcome.get("price"))
    if ask is None:
        ask = _safe_float(outcome.get("price"))

    spread = None
    if bid is not None and ask is not None and ask > 0:
        spread = round(ask - bid, 6)

    # 深度: top of book 的 size
    depth_bid_top = None
    depth_ask_top = None
    bids = outcome.get("depth_bids") or outcome.get("bids") or []
    asks = outcome.get("depth_asks") or outcome.get("asks") or []
    if bids and isinstance(bids, list) and isinstance(bids[0], dict):
        depth_bid_top = _safe_float(bids[0].get("size"))
    if asks and isinstance(asks, list) and isinstance(asks[0], dict):
        depth_ask_top = _safe_float(asks[0].get("size"))

    return {
        "best_bid": bid,
        "best_ask": ask,
        "spread": spread,
        "depth_bid_top": depth_bid_top,
        "depth_ask_top": depth_ask_top,
    }


def _minutes_to_end(market: Dict[str, Any]) -> Optional[float]:
    """从 market.end_date 算到现在的剩余分钟数."""
    end_str = market.get("end_date") or market.get("endDate") or market.get("endDateIso")
    if not end_str:
        return None
    try:
        # 兼容 "2026-06-27T02:30:00Z" / "2026-06-27T02:30:00+00:00" / "2026-06-27T02:30:00.000Z"
        end_dt = datetime.fromisoformat(str(end_str).replace("Z", "+00:00"))
        now = datetime.now(timezone.utc)
        return round((end_dt - now).total_seconds() / 60, 2)
    except Exception:
        return None


def _depth_at_or_better(levels: Any, limit_price: Optional[float], *, side: str) -> float:
    """计算 limit_price 可成交 depth.

    side="ask": 买入时可吃到 price <= limit_price 的 ask 深度。
    side="bid": 卖出时可打到 price >= limit_price 的 bid 深度。
    """
    if limit_price is None or not isinstance(levels, list):
        return 0.0
    total = 0.0
    for level in levels:
        if not isinstance(level, dict):
            continue
        px = _safe_float(level.get("price"))
        sz = _safe_float(level.get("size"))
        if px is None or sz is None or sz <= 0:
            continue
        if side == "ask" and px <= limit_price + 1e-9:
            total += sz
        elif side == "bid" and px >= limit_price - 1e-9:
            total += sz
    return round(total, 6)


def _dualbuy_event(market: Dict[str, Any], now_utc: datetime) -> Optional[Dict[str, Any]]:
    """提取同一二元市场的双边买入观察事件; 只记录, 不交易."""
    outcomes = market.get("outcomes") or []
    if len(outcomes) != 2:
        return None
    snaps = [_extract_outcome_snapshot(outcome) for outcome in outcomes[:2]]
    asks = [snap.get("best_ask") for snap in snaps]
    bids = [snap.get("best_bid") for snap in snaps]
    if asks[0] is None or asks[1] is None:
        return None
    ask_sum = round(float(asks[0]) + float(asks[1]), 6)
    gross_edge = round(1.0 - ask_sum, 6)
    ask_depths = []
    bid_depths = []
    for outcome, snap in zip(outcomes[:2], snaps):
        asks_book = outcome.get("depth_asks") or outcome.get("asks") or []
        bids_book = outcome.get("depth_bids") or outcome.get("bids") or []
        ask_depths.append(_depth_at_or_better(asks_book, snap.get("best_ask"), side="ask"))
        bid_depths.append(_depth_at_or_better(bids_book, snap.get("best_bid"), side="bid"))
    max_pair_shares_at_top = round(min(ask_depths), 6) if ask_depths else 0.0
    executable_notional = round(max_pair_shares_at_top * ask_sum, 6)
    return {
        "t": now_utc.isoformat(),
        "slug": market.get("slug", ""),
        "question": (market.get("question") or "")[:80],
        "minutes_to_end": _minutes_to_end(market),
        "outcomes": [
            {
                "index": outcomes[i].get("index"),
                "label": outcomes[i].get("label", ""),
                "best_bid": bids[i],
                "best_ask": asks[i],
                "ask_depth_at_top": ask_depths[i],
                "bid_depth_at_top": bid_depths[i],
            }
            for i in range(2)
        ],
        "ask_sum": ask_sum,
        "gross_edge": gross_edge,
        "max_pair_shares_at_top": max_pair_shares_at_top,
        "executable_notional_at_top": executable_notional,
        "is_profitable_before_fees": ask_sum < 1.0,
        "is_strict_candidate_98": ask_sum <= 0.98 and max_pair_shares_at_top > 0,
        "is_strict_candidate_97": ask_sum <= 0.97 and max_pair_shares_at_top > 0,
    }


def sample_dualbuy_opportunities(markets: List[Dict[str, Any]], now_utc: Optional[datetime] = None) -> Dict[str, Any]:
    """记录双边开仓机会频次和可成交 depth; observer only, 不下单."""
    if not markets:
        return {"markets": 0, "events": 0, "profitable": 0, "strict_98": 0, "strict_97": 0}
    now_utc = now_utc or datetime.now(timezone.utc)
    events: List[Dict[str, Any]] = []
    try:
        for market in markets:
            slug = market.get("slug", "")
            if not slug or not slug.startswith("btc-updown-15m-"):
                continue
            ev = _dualbuy_event(market, now_utc)
            if ev is not None:
                events.append(ev)
        if events:
            with _write_lock:
                os.makedirs(_DATA_DIR, exist_ok=True)
                with open(DUALBUY_FILE, "a", encoding="utf-8") as f:
                    for ev in events:
                        f.write(json.dumps(ev, ensure_ascii=False, default=str) + "\n")
                    f.flush()
        return {
            "markets": len(markets),
            "events": len(events),
            "profitable": sum(1 for ev in events if ev.get("is_profitable_before_fees")),
            "strict_98": sum(1 for ev in events if ev.get("is_strict_candidate_98")),
            "strict_97": sum(1 for ev in events if ev.get("is_strict_candidate_97")),
            "file": DUALBUY_FILE,
        }
    except Exception as exc:
        logger.debug("[DualBuyObserver] 写入失败 (非阻塞): %s", exc)
        return {"markets": len(markets), "events": 0, "profitable": 0, "strict_98": 0, "strict_97": 0, "error": str(exc)}


def sample_markets(markets: List[Dict[str, Any]], now_utc: Optional[datetime] = None) -> int:
    """采样一批活跃窗口的盘口, 追加写入 JSONL.

    Args:
        markets: 完整 market 对象列表 (从 snapshots / market_api 来)
        now_utc: 时间戳 (默认 now)

    Returns:
        写入的事件数. 出错返回 0.
    """
    if not markets:
        return 0
    try:
        now_utc = now_utc or datetime.now(timezone.utc)
        events: List[Dict[str, Any]] = []
        for m in markets:
            slug = m.get("slug", "")
            if not slug:
                continue
            minutes = _minutes_to_end(m)
            outcomes = m.get("outcomes") or []
            # 每个 outcome (Up/Down) 单独一行, 便于按方向分桶
            for o in outcomes:
                snap = _extract_outcome_snapshot(o)
                ev = {
                    "t": now_utc.isoformat(),
                    "slug": slug,
                    "question": (m.get("question") or "")[:80],
                    "minutes_to_end": minutes,
                    "outcome_label": o.get("label", ""),
                    "outcome_index": o.get("index"),
                    **snap,
                }
                events.append(ev)
        if not events:
            return 0
        with _write_lock:
            os.makedirs(_DATA_DIR, exist_ok=True)
            with open(TICKS_FILE, "a", encoding="utf-8") as f:
                for ev in events:
                    f.write(json.dumps(ev, ensure_ascii=False, default=str) + "\n")
                f.flush()
        return len(events)
    except Exception as exc:
        logger.debug("[TickSampler] 写入失败 (非阻塞): %s", exc)
        return 0


def get_tick_stats(file_path: str = TICKS_FILE) -> Dict[str, Any]:
    """快速汇总 ticks.jsonl 状态."""
    try:
        if not os.path.exists(file_path):
            return {"exists": False, "ticks": 0, "slugs": 0}
        n = 0
        slugs: set = set()
        with open(file_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                    n += 1
                    slugs.add(ev.get("slug", ""))
                except json.JSONDecodeError:
                    continue
        size_kb = round(os.path.getsize(file_path) / 1024, 1)
        return {"exists": True, "ticks": n, "slugs": len(slugs), "size_kb": size_kb}
    except Exception as exc:
        return {"error": str(exc)}
