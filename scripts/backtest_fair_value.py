#!/usr/bin/env python3
"""Backtest Fair Value predictions against resolved Polymarket outcomes.

Workflow
--------
1. Read locally recorded Fair Value prediction samples from JSONL.
2. Pull each market slug's final resolution from Polymarket gamma API.
3. Cache those resolutions under ``data/`` for repeatable local runs.
4. Report:
   - overall forecast quality on all samples
   - per-horizon forecast quality (one sample per slug near N minutes to end)
   - simple one-bet-per-slug trading simulation using edge thresholds
"""

from __future__ import annotations

import argparse
import json
import math
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

import requests


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PREDICTIONS = ROOT / "data" / "fair_value_predictions.jsonl"
DEFAULT_CACHE = ROOT / "data" / "fair_value_resolutions.json"
GAMMA_MARKET_URL = "https://gamma-api.polymarket.com/markets/slug/{slug}"


def load_prediction_rows(path: Path) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not str(row.get("slug") or "").startswith("btc-updown-15m-"):
                continue
            rows.append(row)
    return rows


def load_cache(path: Path) -> Dict[str, Dict[str, Any]]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_cache(path: Path, data: Dict[str, Dict[str, Any]]) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def parse_resolution(raw_market: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    outcome_prices = raw_market.get("outcomePrices") or []
    outcomes_raw = raw_market.get("outcomes") or []
    try:
        prices = outcome_prices if isinstance(outcome_prices, list) else json.loads(outcome_prices)
        outcomes = outcomes_raw if isinstance(outcomes_raw, list) else json.loads(outcomes_raw)
        prices = [float(x) for x in prices]
        outcomes = [str(x) for x in outcomes]
    except Exception:
        return None

    if len(prices) < 2 or len(outcomes) < 2:
        return None

    up_price = prices[0]
    down_price = prices[1]
    if up_price < 0 or down_price < 0:
        return None

    resolved_up = 1 if up_price >= down_price else 0
    return {
        "slug": str(raw_market.get("slug") or ""),
        "closed": bool(raw_market.get("closed")),
        "end_date": raw_market.get("endDate"),
        "outcomes": outcomes[:2],
        "outcome_prices": [up_price, down_price],
        "resolved_up": resolved_up,
    }


def fetch_resolution(slug: str, session: requests.Session) -> Optional[Dict[str, Any]]:
    resp = session.get(GAMMA_MARKET_URL.format(slug=slug), timeout=15)
    resp.raise_for_status()
    data = resp.json()
    return parse_resolution(data)


def ensure_resolutions(
    slugs: Iterable[str],
    cache_path: Path,
    *,
    refresh: bool,
) -> Dict[str, Dict[str, Any]]:
    cache = load_cache(cache_path)
    session = requests.Session()
    updated = False
    for slug in sorted(set(slugs)):
        cached = cache.get(slug)
        if cached and cached.get("closed") and not refresh:
            continue
        resolution = fetch_resolution(slug, session)
        if resolution:
            cache[slug] = resolution
            updated = True
    if updated or not cache_path.exists():
        save_cache(cache_path, cache)
    return cache


def brier_score(rows: Iterable[Dict[str, Any]]) -> Optional[float]:
    vals = []
    for row in rows:
        fair_up = row.get("fair_up")
        resolved_up = row.get("resolved_up")
        if fair_up is None or resolved_up is None:
            continue
        vals.append((float(fair_up) - float(resolved_up)) ** 2)
    if not vals:
        return None
    return sum(vals) / len(vals)


def accuracy(rows: Iterable[Dict[str, Any]]) -> Optional[float]:
    vals = []
    for row in rows:
        fair_up = row.get("fair_up")
        resolved_up = row.get("resolved_up")
        if fair_up is None or resolved_up is None:
            continue
        pred = 1 if float(fair_up) >= 0.5 else 0
        vals.append(1.0 if pred == int(resolved_up) else 0.0)
    if not vals:
        return None
    return sum(vals) / len(vals)


def mean_abs_edge(rows: Iterable[Dict[str, Any]]) -> Optional[float]:
    vals = []
    for row in rows:
        edge_up_bps = row.get("edge_up_bps")
        edge_down_bps = row.get("edge_down_bps")
        edges = [abs(float(v)) for v in (edge_up_bps, edge_down_bps) if v is not None]
        if edges:
            vals.append(max(edges) / 10000.0)
    if not vals:
        return None
    return sum(vals) / len(vals)


def select_rows_for_target(rows: List[Dict[str, Any]], target_mte: float) -> List[Dict[str, Any]]:
    grouped: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[str(row.get("slug") or "")].append(row)

    selected: List[Dict[str, Any]] = []
    for slug_rows in grouped.values():
        slug_rows.sort(key=lambda x: float(x.get("minutes_to_end") or 0.0), reverse=True)
        picked = None
        for row in slug_rows:
            mte = float(row.get("minutes_to_end") or 0.0)
            if mte <= target_mte:
                picked = row
                break
        if picked is None:
            picked = min(slug_rows, key=lambda x: abs(float(x.get("minutes_to_end") or 0.0) - target_mte))
        selected.append(picked)
    return selected


def slug_end_ts(slug: str) -> Optional[int]:
    try:
        return int(str(slug).rsplit("-", 1)[-1])
    except Exception:
        return None


def child_5m_slugs(slug_15m: str) -> List[str]:
    end_ts = slug_end_ts(slug_15m)
    if end_ts is None:
        return []
    return [f"btc-updown-5m-{end_ts - 600}", f"btc-updown-5m-{end_ts - 300}", f"btc-updown-5m-{end_ts}"]


def attach_5m_confirmation(
    rows: List[Dict[str, Any]],
    resolutions: Dict[str, Dict[str, Any]],
) -> List[Dict[str, Any]]:
    enriched: List[Dict[str, Any]] = []
    for row in rows:
        slug = str(row.get("slug") or "")
        end_ts = slug_end_ts(slug)
        minutes_to_end = float(row.get("minutes_to_end") or 0.0)
        if end_ts is None:
            enriched.append(dict(row))
            continue

        decision_ts = end_ts - int(round(minutes_to_end * 60.0))
        completed = []
        for child_slug in child_5m_slugs(slug):
            child_end = slug_end_ts(child_slug)
            child_resolution = resolutions.get(child_slug)
            if child_end is None or not child_resolution:
                continue
            if child_end <= decision_ts:
                completed.append(
                    {
                        "slug": child_slug,
                        "end_ts": child_end,
                        "resolved_up": int(child_resolution["resolved_up"]),
                    }
                )

        up_count = sum(item["resolved_up"] for item in completed)
        down_count = len(completed) - up_count
        net_score = up_count - down_count
        row2 = dict(row)
        row2["completed_5m_count"] = len(completed)
        row2["completed_5m_up"] = up_count
        row2["completed_5m_down"] = down_count
        row2["completed_5m_net"] = net_score
        row2["five_min_confirms_up"] = bool(len(completed) > 0 and net_score > 0)
        row2["five_min_confirms_down"] = bool(len(completed) > 0 and net_score < 0)
        row2["five_min_neutral"] = bool(len(completed) == 0 or net_score == 0)
        enriched.append(row2)
    return enriched


def summarize_rows(label: str, rows: List[Dict[str, Any]]) -> None:
    acc = accuracy(rows)
    brier = brier_score(rows)
    mae = mean_abs_edge(rows)
    late_refs = sum(1 for row in rows if row.get("late_ref"))
    print(
        f"{label:<18} samples={len(rows):>4} "
        f"acc={(acc * 100 if acc is not None else float('nan')):>6.2f}% "
        f"brier={(brier if brier is not None else float('nan')):>7.4f} "
        f"avg_abs_edge={(mae * 100 if mae is not None else float('nan')):>6.2f}% "
        f"late_ref={late_refs:>3}"
    )


def simulate_trades(rows: List[Dict[str, Any]], threshold: float) -> Dict[str, Any]:
    trades = []
    for row in rows:
        resolved_up = row.get("resolved_up")
        up_ask = row.get("market_up_ask")
        down_ask = row.get("market_down_ask")
        edge_up = (float(row["edge_up_bps"]) / 10000.0) if row.get("edge_up_bps") is not None else None
        edge_down = (float(row["edge_down_bps"]) / 10000.0) if row.get("edge_down_bps") is not None else None

        side = None
        price = None
        if edge_up is not None and up_ask is not None and edge_up >= threshold:
            side = "UP"
            price = float(up_ask)
            edge = edge_up
        if edge_down is not None and down_ask is not None and edge_down >= threshold:
            if side is None or edge_down > edge:
                side = "DOWN"
                price = float(down_ask)
                edge = edge_down
        if side is None or price is None or resolved_up is None:
            continue

        win = bool(int(resolved_up) == 1) if side == "UP" else bool(int(resolved_up) == 0)
        pnl = (1.0 - price) if win else (-price)
        trades.append({"side": side, "price": price, "win": win, "pnl": pnl})

    if not trades:
        return {"count": 0}

    total_pnl = sum(t["pnl"] for t in trades)
    total_cost = sum(t["price"] for t in trades)
    wins = sum(1 for t in trades if t["win"])
    return {
        "count": len(trades),
        "win_rate": wins / len(trades),
        "avg_price": total_cost / len(trades),
        "avg_pnl": total_pnl / len(trades),
        "roi_on_cost": (total_pnl / total_cost) if total_cost > 0 else None,
        "total_pnl": total_pnl,
    }


def print_trade_summary(rows: List[Dict[str, Any]], thresholds: List[float], label: str) -> None:
    print(f"\nTrading sim: {label}")
    for threshold in thresholds:
        result = simulate_trades(rows, threshold)
        if result["count"] == 0:
            print(f"  edge>={threshold * 100:>4.1f}%  trades=0")
            continue
        roi = result["roi_on_cost"]
        print(
            f"  edge>={threshold * 100:>4.1f}%  trades={result['count']:>2} "
            f"win_rate={result['win_rate'] * 100:>6.2f}% "
            f"avg_price={result['avg_price']:>5.3f} "
            f"avg_pnl={result['avg_pnl']:>+6.3f} "
            f"roi={(roi * 100 if roi is not None else float('nan')):>+7.2f}% "
            f"total_pnl={result['total_pnl']:>+6.3f}"
        )


def direction_from_fv(row: Dict[str, Any]) -> str:
    fair_up = float(row.get("fair_up") or 0.5)
    return "UP" if fair_up >= 0.5 else "DOWN"


def apply_5m_confirmation(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    filtered = []
    for row in rows:
        completed = int(row.get("completed_5m_count") or 0)
        if completed <= 0:
            continue
        fv_dir = direction_from_fv(row)
        if fv_dir == "UP" and row.get("five_min_confirms_up"):
            filtered.append(row)
        elif fv_dir == "DOWN" and row.get("five_min_confirms_down"):
            filtered.append(row)
    return filtered


def apply_5m_veto_only(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    filtered = []
    for row in rows:
        completed = int(row.get("completed_5m_count") or 0)
        if completed <= 0:
            filtered.append(row)
            continue
        fv_dir = direction_from_fv(row)
        opposes = (
            (fv_dir == "UP" and row.get("five_min_confirms_down"))
            or (fv_dir == "DOWN" and row.get("five_min_confirms_up"))
        )
        if not opposes:
            filtered.append(row)
    return filtered


def print_confirmation_summary(rows: List[Dict[str, Any]], label: str) -> None:
    if not rows:
        print(f"\n5m confirmation: {label}\n  no rows")
        return
    completed = [int(row.get("completed_5m_count") or 0) for row in rows]
    confirmable = [row for row in rows if int(row.get("completed_5m_count") or 0) > 0]
    confirmed = apply_5m_confirmation(rows)
    print(f"\n5m confirmation: {label}")
    print(
        f"  rows={len(rows)} confirmable={len(confirmable)} confirmed={len(confirmed)} "
        f"avg_completed_5m={sum(completed) / len(completed):.2f}"
    )
    if confirmable:
        summarize_rows("confirmable", confirmable)
    if confirmed:
        summarize_rows("confirmed", confirmed)


def print_veto_summary(rows: List[Dict[str, Any]], label: str) -> None:
    if not rows:
        print(f"\n5m veto-only: {label}\n  no rows")
        return
    completed = [row for row in rows if int(row.get("completed_5m_count") or 0) > 0]
    kept = apply_5m_veto_only(rows)
    vetoed = len(rows) - len(kept)
    print(f"\n5m veto-only: {label}")
    print(
        f"  rows={len(rows)} with_completed_5m={len(completed)} kept={len(kept)} vetoed={vetoed}"
    )
    if kept:
        summarize_rows("kept", kept)


def attach_resolutions(
    rows: List[Dict[str, Any]],
    resolutions: Dict[str, Dict[str, Any]],
) -> List[Dict[str, Any]]:
    attached = []
    for row in rows:
        resolution = resolutions.get(str(row.get("slug") or ""))
        if not resolution:
            continue
        if resolution.get("resolved_up") is None:
            continue
        merged = dict(row)
        merged["resolved_up"] = resolution["resolved_up"]
        attached.append(merged)
    return attached


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Backtest Fair Value predictions using resolved Polymarket outcomes")
    p.add_argument("--predictions", type=Path, default=DEFAULT_PREDICTIONS)
    p.add_argument("--cache", type=Path, default=DEFAULT_CACHE)
    p.add_argument("--refresh", action="store_true", help="Force refresh market resolutions from Polymarket")
    p.add_argument(
        "--targets",
        type=float,
        nargs="*",
        default=[12.0, 10.0, 5.0, 2.0, 1.0],
        help="Minutes-to-end targets for one-sample-per-slug evaluation",
    )
    p.add_argument(
        "--thresholds",
        type=float,
        nargs="*",
        default=[0.03, 0.05, 0.07],
        help="Edge thresholds for the simple trading simulation",
    )
    return p.parse_args()


def main() -> None:
    args = parse_args()
    rows = load_prediction_rows(args.predictions)
    if not rows:
        raise SystemExit(f"no prediction rows found in {args.predictions}")

    slugs = sorted({str(row.get('slug') or '') for row in rows})
    child_slugs = []
    for slug in slugs:
        child_slugs.extend(child_5m_slugs(slug))
    resolutions = ensure_resolutions(slugs + child_slugs, args.cache, refresh=args.refresh)
    rows = attach_resolutions(rows, resolutions)
    rows = attach_5m_confirmation(rows, resolutions)
    if not rows:
        raise SystemExit("no rows could be matched to resolved outcomes")

    print(f"predictions_file={args.predictions}")
    print(f"resolution_cache={args.cache}")
    print(f"rows={len(rows)} unique_slugs={len(set(row['slug'] for row in rows))}\n")

    summarize_rows("all_samples", rows)
    summarize_rows("all_no_late_ref", [row for row in rows if not row.get("late_ref")])

    print("\nOne sample per slug by target minutes_to_end")
    selected_by_target: Dict[float, List[Dict[str, Any]]] = {}
    for target in args.targets:
        selected = select_rows_for_target(rows, target)
        selected_by_target[target] = selected
        summarize_rows(f"target_{target:g}m", selected)
        print_confirmation_summary(selected, f"target {target:g}m")
        print_veto_summary(selected, f"target {target:g}m")

    print_trade_summary([row for row in rows if not row.get("late_ref")], args.thresholds, "all no-late-ref samples")
    for target in args.targets:
        print_trade_summary(selected_by_target[target], args.thresholds, f"target {target:g}m")
        confirmed = apply_5m_confirmation(selected_by_target[target])
        print_trade_summary(confirmed, args.thresholds, f"target {target:g}m + 5m confirmation")
        veto_kept = apply_5m_veto_only(selected_by_target[target])
        print_trade_summary(veto_kept, args.thresholds, f"target {target:g}m + 5m veto-only")


if __name__ == "__main__":
    main()
