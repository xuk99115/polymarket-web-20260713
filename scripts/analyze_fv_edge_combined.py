#!/usr/bin/env python3
"""Explore FV + edge combined strategies.

Hypothesis: edge alone makes money, but combining FV confidence / z-score
as a filter or weighting layer could improve.

Variants tested (all require |edge| >= baseline threshold first):
  V1: edge baseline (no FV filter)
  V2: edge + FV direction agrees (e.g. edge>0 means FV says UP > market)
  V3: edge + FV confidence high (fair_up extreme or fair_z_score large)
  V4: edge + FV doesn't disagree strongly (avoid cases FV is bullish but market knows better)
  V5: edge + z-score magnitude weighted (size bet by z)
  V6: edge + FV overconfidence discount (skip trades where FV calibration is biased)
"""

from __future__ import annotations
import json
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PRED = ROOT / "data" / "fair_value_predictions.jsonl"
RESO = ROOT / "data" / "fair_value_resolutions.json"
OUT = ROOT / "data" / "fv_edge_combined_report.json"


def load():
    rows = []
    with PRED.open() as f:
        for line in f:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    resos = json.loads(RESO.read_text()) if RESO.exists() else {}
    enriched = []
    for r in rows:
        slug = r.get("slug")
        reso = resos.get(slug)
        if not reso or reso.get("resolved_up") is None:
            continue
        r2 = dict(r)
        r2["resolved_up"] = int(reso["resolved_up"])
        enriched.append(r2)
    return enriched


def pick_per_slug(rows, target_mte):
    """For each slug, pick the row nearest-but-not-after target_mte."""
    by_slug = defaultdict(list)
    for r in rows:
        by_slug[r["slug"]].append(r)
    picked = []
    for slug, slug_rows in by_slug.items():
        slug_rows.sort(key=lambda x: x.get("minutes_to_end", 0), reverse=True)
        chosen = None
        for r in slug_rows:
            if r.get("minutes_to_end", 0) <= target_mte:
                chosen = r
                break
        if chosen is None and slug_rows:
            chosen = min(slug_rows, key=lambda x: abs(x.get("minutes_to_end", 0) - target_mte))
        if chosen:
            picked.append(chosen)
    return picked


def edge_trade(r, edge_thr_bps):
    """Return (won, pnl) or None if no edge."""
    edge = r.get("edge_up_bps")
    if edge is None or abs(edge) < edge_thr_bps:
        return None
    market_up_ask = r.get("market_up_ask")
    resolved = r.get("resolved_up")
    if market_up_ask is None or resolved is None:
        return None
    if edge > 0:
        buy_price = market_up_ask
        won = (resolved == 1)
    else:
        buy_price = 1.0 - market_up_ask
        won = (resolved == 0)
    pnl = (1.0 - buy_price) if won else -buy_price
    return {"won": won, "pnl": pnl, "buy_price": buy_price}


def stats(trades):
    if not trades:
        return {"trades": 0}
    n = len(trades)
    wins = sum(1 for t in trades if t["won"])
    total_pnl = sum(t["pnl"] for t in trades)
    invested = sum(t["buy_price"] for t in trades)
    return {
        "trades": n,
        "win_rate": round(wins / n * 100, 2),
        "total_pnl": round(total_pnl, 3),
        "roi_pct": round(total_pnl / invested * 100, 2) if invested else 0,
    }


def variant_v1_edge_only(picked, edge_thr):
    trades = []
    for r in picked:
        t = edge_trade(r, edge_thr)
        if t:
            trades.append(t)
    return trades


def variant_v2_edge_and_fv_agrees(picked, edge_thr):
    """Same direction bet — FV agrees with direction (edge>0 means FV > market, so FV bullish)."""
    trades = []
    for r in picked:
        t = edge_trade(r, edge_thr)
        if not t:
            continue
        fair = r.get("fair_up")
        market = r.get("market_up_ask")
        edge = r.get("edge_up_bps")
        if fair is None or market is None:
            continue
        # Long UP: need FV bullish (fair > 0.5). Long DOWN: need FV bearish (fair < 0.5).
        if edge > 0 and fair > 0.5:
            trades.append(t)
        elif edge < 0 and fair < 0.5:
            trades.append(t)
    return trades


def variant_v3_edge_and_strong_fv(picked, edge_thr, z_min=1.0):
    """Require FV has at least z=z_min confidence in the direction."""
    trades = []
    for r in picked:
        t = edge_trade(r, edge_thr)
        if not t:
            continue
        z = r.get("fair_z_score", 0) or 0
        edge = r.get("edge_up_bps")
        if edge > 0 and z >= z_min:
            trades.append(t)
        elif edge < 0 and z <= -z_min:
            trades.append(t)
    return trades


def variant_v4_edge_and_low_mte(picked, edge_thr, mte_max=2.0):
    """FV is more accurate near window close — only bet when mte is small."""
    trades = []
    for r in picked:
        mte = r.get("minutes_to_end", 0)
        if mte > mte_max:
            continue
        t = edge_trade(r, edge_thr)
        if t:
            trades.append(t)
    return trades


def variant_v5_calibration_discount(picked, edge_thr):
    """When FV is in known biased bucket (0.4-0.5 or 0.9-1.0), reduce conviction.
    Just skip trades where FV is in the bad buckets from calibration analysis."""
    trades = []
    for r in picked:
        t = edge_trade(r, edge_thr)
        if not t:
            continue
        fair = r.get("fair_up")
        if fair is None:
            continue
        # FV calibration bad buckets: 0.2-0.3, 0.4-0.5, 0.9-1.0
        bad = (0.2 <= fair < 0.3) or (0.4 <= fair < 0.5) or (0.9 <= fair <= 1.0)
        if bad:
            continue
        trades.append(t)
    return trades


def variant_v6_edge_and_strong_fv_strict(picked, edge_thr):
    """Combo: edge + FV agrees (v2) + low mte (v4)."""
    trades = []
    for r in picked:
        if r.get("minutes_to_end", 0) > 2.0:
            continue
        t = edge_trade(r, edge_thr)
        if not t:
            continue
        fair = r.get("fair_up")
        edge = r.get("edge_up_bps")
        if fair is None:
            continue
        if edge > 0 and fair > 0.5:
            trades.append(t)
        elif edge < 0 and fair < 0.5:
            trades.append(t)
    return trades


def main():
    rows = load()
    print(f"Loaded {len(rows)} rows, {len(set(r['slug'] for r in rows))} unique slugs")

    picked = pick_per_slug(rows, target_mte=1.0)
    print(f"Picked {len(picked)} per-slug decisions (target mte=1.0)")

    results = {}
    for edge_thr in [100, 300, 500, 700]:
        v1 = variant_v1_edge_only(picked, edge_thr)
        v2 = variant_v2_edge_and_fv_agrees(picked, edge_thr)
        v3 = variant_v3_edge_and_strong_fv(picked, edge_thr)
        v4 = variant_v4_edge_and_low_mte(picked, edge_thr)
        v5 = variant_v5_calibration_discount(picked, edge_thr)
        v6 = variant_v6_edge_and_strong_fv_strict(picked, edge_thr)

        results[f"edge{edge_thr}_bps"] = {
            "V1_edge_only": stats(v1),
            "V2_edge+FV_agrees": stats(v2),
            "V3_edge+|z|>=1.0": stats(v3),
            "V4_edge+mte<=2": stats(v4),
            "V5_edge-skip_bad_cal": stats(v5),
            "V6_edge+FV_agrees+mte<=2": stats(v6),
        }

    print(f"\n--- COMBINED STRATEGY COMPARISON (target mte=1.0) ---")
    print(f"{'edge':<6} {'variant':<28} {'trades':>6} {'win%':>6} {'ROI%':>8}")
    for edge_thr in [100, 300, 500, 700]:
        key = f"edge{edge_thr}_bps"
        print(f"\n  Threshold: {edge_thr} bps")
        for vname, vstats in results[key].items():
            if vstats.get("trades", 0) > 0:
                print(f"  {vname:<28} {vstats['trades']:>6} {vstats['win_rate']:>5.1f}% {vstats['roi_pct']:>+7.2f}%")

    # Save report
    OUT.write_text(json.dumps(results, indent=2))
    print(f"\nReport saved: {OUT}")

    # Verdict
    print(f"\n=== VERDICT ===")
    best_v1_roi = max((results[k]["V1_edge_only"].get("roi_pct", 0) for k in results), default=0)
    print(f"  Baseline V1 best ROI: {best_v1_roi:+.2f}%")
    for variant in ["V2_edge+FV_agrees", "V3_edge+|z|>=1.0", "V4_edge+mte<=2",
                    "V5_edge-skip_bad_cal", "V6_edge+FV_agrees+mte<=2"]:
        best = max((results[k][variant].get("roi_pct", 0)
                    for k in results if results[k][variant].get("trades", 0) > 0), default=0)
        beats = best > best_v1_roi
        marker = "+" if beats else "-"
        print(f"  {variant:<28} best ROI: {best:>+7.2f}%  beats_V1={marker}")


if __name__ == "__main__":
    main()