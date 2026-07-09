#!/usr/bin/env python3
"""
Replay LowBuy using the current live bot semantics on market_ticks.jsonl.

This is the stricter replacement for simulate_lowbuy_buckets.py:
- entry uses best_ask, not best_bid
- entry window defaults to MTE [10, 14] minutes
- entry ask defaults to [0.22, 0.40]
- spread filter matches LowBuy: bid >= ask * (1 - 0.15)
- TP uses later best_bid >= entry_ask * 1.4
- TIME_STOP uses first tick with minutes_to_end <= 5.0, closing at latest bid
- one position per slug, matching manager._lowbuy_open duplicate exposure behavior

Limits: market_ticks.jsonl does not include BTC trend/fair-value context, so this replay cannot
model trend/FV filters. OBI/depth filters are only applied when depth fields exist.
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Tuple

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_FILE = os.path.join(ROOT, "data", "market_ticks.jsonl")

ENTRY_BUCKETS: List[Tuple[float, float, str]] = [
    (0.22, 0.30, "0.22-0.30"),
    (0.30, 0.35, "0.30-0.35"),
    (0.35, 0.40, "0.35-0.40"),
    (0.40, 0.4000001, "0.40-boundary"),
]
MTE_BUCKETS: List[Tuple[float, float, str]] = [
    (10.0, 12.0, "10-12"),
    (12.0, 14.000001, "12-14"),
]


def parse_iso(value: Any) -> Optional[datetime]:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


def safe_float(value: Any) -> Optional[float]:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def load_ticks(path: str) -> List[Dict[str, Any]]:
    if not os.path.exists(path):
        raise FileNotFoundError(path)
    rows: List[Dict[str, Any]] = []
    bad = 0
    with open(path, "r", encoding="utf-8") as fh:
        for lineno, line in enumerate(fh, 1):
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                bad += 1
                continue
            row["_lineno"] = lineno
            rows.append(row)
    if bad:
        print(f"⚠️ skipped bad JSON lines: {bad}", file=sys.stderr)
    return rows


def group_ticks(rows: Iterable[Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
    groups: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for row in rows:
        slug = row.get("slug") or ""
        if not str(slug).startswith("btc-updown-15m-"):
            continue
        oi = row.get("outcome_index")
        groups[f"{slug}:{oi}"].append(row)
    for seq in groups.values():
        seq.sort(key=lambda x: parse_iso(x.get("t")) or datetime.min.replace(tzinfo=timezone.utc))
    return groups


def entry_bucket(price: float) -> str:
    for lo, hi, label in ENTRY_BUCKETS:
        if lo <= price < hi:
            return label
    return "other"


def mte_bucket(mte: Optional[float]) -> str:
    if mte is None:
        return "unknown"
    for lo, hi, label in MTE_BUCKETS:
        if lo <= mte < hi:
            return label
    if mte < 10:
        return "<10"
    return ">14"


def pct(value: Optional[float]) -> str:
    if value is None:
        return "--"
    return f"{value * 100:+.1f}%"


@dataclass
class ReplayParams:
    min_entry: float = 0.22
    max_entry: float = 0.40
    min_mte: float = 10.0
    max_mte: float = 14.0
    max_spread_pct: float = 0.15
    tp_mult: float = 1.4
    stop_mte: float = 5.0
    stake: float = 2.0
    one_per_slug: bool = True
    require_depth_for_tp: bool = False
    tp_min_depth: float = 0.5


def passes_entry(row: Dict[str, Any], params: ReplayParams) -> Tuple[bool, str]:
    ask = safe_float(row.get("best_ask"))
    bid = safe_float(row.get("best_bid"))
    mte = safe_float(row.get("minutes_to_end"))
    if ask is None or bid is None or ask <= 0 or bid <= 0:
        return False, "missing_quote"
    if mte is None or not (params.min_mte <= mte <= params.max_mte):
        return False, "mte"
    if not (params.min_entry <= ask <= params.max_entry):
        return False, "entry_ask"
    if bid < ask * (1 - params.max_spread_pct):
        return False, "spread"
    return True, "ok"


def simulate_sequence(key: str, seq: List[Dict[str, Any]], params: ReplayParams) -> Optional[Dict[str, Any]]:
    entry_index = None
    entry = None
    skip_reasons = defaultdict(int)
    for i, row in enumerate(seq):
        ok, reason = passes_entry(row, params)
        if not ok:
            skip_reasons[reason] += 1
            continue
        entry_index = i
        entry = row
        break
    if entry is None or entry_index is None:
        return None

    entry_ask = float(entry["best_ask"])
    entry_bid = float(entry["best_bid"])
    entry_t = entry.get("t")
    entry_mte = safe_float(entry.get("minutes_to_end"))
    target = entry_ask * params.tp_mult
    last_bid = entry_bid
    exit_row = entry
    result = "OPEN_EOF"
    action = "OPEN_EOF"

    for row in seq[entry_index + 1:]:
        bid = safe_float(row.get("best_bid"))
        mte = safe_float(row.get("minutes_to_end"))
        if bid is not None and bid > 0:
            last_bid = bid
        if bid is not None and bid >= target:
            if params.require_depth_for_tp:
                depth = safe_float(row.get("depth_bid_top"))
                if depth is not None and depth < params.tp_min_depth:
                    continue
            exit_row = row
            result = "TAKE_PROFIT"
            action = "WIN"
            break
        if mte is not None and mte <= params.stop_mte:
            exit_row = row
            result = "TIME_STOP"
            action = "WIN" if last_bid >= entry_ask else "LOSS"
            break

    t0 = parse_iso(entry_t)
    t1 = parse_iso(exit_row.get("t"))
    hold_min = round((t1 - t0).total_seconds() / 60, 2) if t0 and t1 else None
    shares = params.stake / entry_ask if entry_ask > 0 else 0.0
    pnl = shares * (last_bid - entry_ask)
    return {
        "series": key,
        "slug": key.rsplit(":", 1)[0],
        "outcome_index": entry.get("outcome_index"),
        "outcome_label": entry.get("outcome_label"),
        "entry_t": entry_t,
        "entry_mte": entry_mte,
        "entry_ask": entry_ask,
        "entry_bid": entry_bid,
        "target_bid": target,
        "exit_t": exit_row.get("t"),
        "exit_mte": safe_float(exit_row.get("minutes_to_end")),
        "exit_bid": last_bid,
        "status": result,
        "result": action,
        "hold_min": hold_min,
        "gain_ratio": (last_bid - entry_ask) / entry_ask if entry_ask else None,
        "pnl_usd": pnl,
        "entry_bucket": entry_bucket(entry_ask),
        "mte_bucket": mte_bucket(entry_mte),
        "entry_lineno": entry.get("_lineno"),
        "exit_lineno": exit_row.get("_lineno"),
    }


def simulate_all(groups: Dict[str, List[Dict[str, Any]]], params: ReplayParams) -> Tuple[List[Dict[str, Any]], Dict[str, int]]:
    trades: List[Dict[str, Any]] = []
    skipped_slugs = set()
    diagnostics = defaultdict(int)
    for key, seq in groups.items():
        slug = key.rsplit(":", 1)[0]
        if params.one_per_slug and slug in skipped_slugs:
            diagnostics["blocked_by_one_per_slug"] += 1
            continue
        result = simulate_sequence(key, seq, params)
        if result is None:
            diagnostics["no_entry"] += 1
            continue
        trades.append(result)
        if params.one_per_slug:
            skipped_slugs.add(slug)
    trades.sort(key=lambda x: x.get("entry_t") or "")
    return trades, dict(diagnostics)


def summarize_bucket(trades: List[Dict[str, Any]], field: str) -> None:
    grouped: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for trade in trades:
        grouped[trade.get(field, "unknown")].append(trade)
    for label in sorted(grouped):
        items = grouped[label]
        n = len(items)
        wins = sum(1 for x in items if x["pnl_usd"] >= 0)
        pnl = sum(x["pnl_usd"] for x in items)
        holds = [x["hold_min"] for x in items if x.get("hold_min") is not None]
        gains = [x["gain_ratio"] for x in items if x.get("gain_ratio") is not None]
        print(
            f"{label:<14} n={n:>4} wins={wins:>4} wr={wins/n*100:>5.1f}% "
            f"pnl=${pnl:+.4f} avg_gain={pct(sum(gains)/len(gains) if gains else None):>8} "
            f"avg_hold={(sum(holds)/len(holds) if holds else 0):>5.2f}m"
        )


def print_report(trades: List[Dict[str, Any]], diagnostics: Dict[str, int], groups_count: int, params: ReplayParams, show_recent: int) -> None:
    print("=" * 100)
    print("LowBuy current-params replay (ask-entry, MTE window, spread, TP/TIME_STOP)")
    print("=" * 100)
    print(
        f"params: ask=[{params.min_entry:.2f},{params.max_entry:.2f}], "
        f"mte=[{params.min_mte:.1f},{params.max_mte:.1f}], spread<={params.max_spread_pct*100:.0f}%, "
        f"tp={params.tp_mult:.2f}x, stop_mte<={params.stop_mte:.1f}, one_per_slug={params.one_per_slug}"
    )
    print(f"series={groups_count}, simulated_trades={len(trades)}, diagnostics={diagnostics}")
    if not trades:
        return
    total_pnl = sum(t["pnl_usd"] for t in trades)
    wins = sum(1 for t in trades if t["pnl_usd"] >= 0)
    tp = sum(1 for t in trades if t["status"] == "TAKE_PROFIT")
    ts = sum(1 for t in trades if t["status"] == "TIME_STOP")
    eof = sum(1 for t in trades if t["status"] == "OPEN_EOF")
    holds = [t["hold_min"] for t in trades if t.get("hold_min") is not None]
    print(
        f"overall: n={len(trades)}, wins={wins}, losses={len(trades)-wins}, "
        f"winrate={wins/len(trades)*100:.1f}%, pnl=${total_pnl:+.4f}, "
        f"TP={tp}, TIME_STOP={ts}, OPEN_EOF={eof}, avg_hold={(sum(holds)/len(holds) if holds else 0):.2f}m"
    )
    print("\nEntry ask buckets:")
    summarize_bucket(trades, "entry_bucket")
    print("\nEntry MTE buckets:")
    summarize_bucket(trades, "mte_bucket")
    print(f"\nRecent {show_recent} simulated trades:")
    for t in trades[-show_recent:]:
        print(
            f"{t['entry_t']} {t['outcome_label']} ask={t['entry_ask']*100:.1f}¢ "
            f"bid={t['entry_bid']*100:.1f}¢ → exit_bid={t['exit_bid']*100:.1f}¢ "
            f"{t['status']} pnl=${t['pnl_usd']:+.4f} mte={t['entry_mte']} hold={t['hold_min']} "
            f"slug={str(t['slug'])[-10:]}"
        )


def main() -> None:
    parser = argparse.ArgumentParser(description="Replay LowBuy using current bot params on market_ticks.jsonl")
    parser.add_argument("--file", default=DEFAULT_FILE)
    parser.add_argument("--min-entry", type=float, default=0.22)
    parser.add_argument("--max-entry", type=float, default=0.40)
    parser.add_argument("--min-mte", type=float, default=10.0)
    parser.add_argument("--max-mte", type=float, default=14.0)
    parser.add_argument("--tp-mult", type=float, default=1.4)
    parser.add_argument("--stop-mte", type=float, default=5.0)
    parser.add_argument("--max-spread-pct", type=float, default=0.15)
    parser.add_argument("--stake", type=float, default=2.0)
    parser.add_argument("--allow-multiple-per-slug", action="store_true")
    parser.add_argument("--require-depth-for-tp", action="store_true")
    parser.add_argument("--show-recent", type=int, default=12)
    parser.add_argument("--json-out", help="Optional path to write simulated trades JSON")
    args = parser.parse_args()

    params = ReplayParams(
        min_entry=args.min_entry,
        max_entry=args.max_entry,
        min_mte=args.min_mte,
        max_mte=args.max_mte,
        max_spread_pct=args.max_spread_pct,
        tp_mult=args.tp_mult,
        stop_mte=args.stop_mte,
        stake=args.stake,
        one_per_slug=not args.allow_multiple_per_slug,
        require_depth_for_tp=args.require_depth_for_tp,
    )
    rows = load_ticks(args.file)
    groups = group_ticks(rows)
    print(f"loaded ticks={len(rows)}, grouped_series={len(groups)} from {args.file}", file=sys.stderr)
    trades, diagnostics = simulate_all(groups, params)
    print_report(trades, diagnostics, len(groups), params, args.show_recent)
    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8") as fh:
            json.dump({"params": params.__dict__, "diagnostics": diagnostics, "trades": trades}, fh, ensure_ascii=False, indent=2)
        print(f"\nwrote {args.json_out}")


if __name__ == "__main__":
    main()
