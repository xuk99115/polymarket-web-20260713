#!/usr/bin/env python3
"""Pure-stdlib baseline analysis of FV predictions vs resolved outcomes.

Why no sklearn/torch: production venv currently has I/O bottlenecks
causing torch/sklearn imports to hang. This script proves the data
pipeline works using only stdlib (json, collections, statistics).

Outputs:
  1. Overall FV calibration (how often was FV right)
  2. Per-horizon quality (minutes_to_end buckets)
  3. Edge-strategy ROI for several thresholds (mirror backtest)
  4. Identify "fatal" windows where FV was confidently wrong
  5. Suggest whether ML has room to improve

Usage:
  python3 scripts/analyze_fv_baseline.py
"""

from __future__ import annotations
import json
import math
import statistics
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PRED = ROOT / "data" / "fair_value_predictions.jsonl"
RESO = ROOT / "data" / "fair_value_resolutions.json"
REPORT = ROOT / "data" / "fv_baseline_report.json"


def load() -> tuple[list[dict], dict]:
    rows = []
    with PRED.open() as f:
        for line in f:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    resolutions = json.loads(RESO.read_text()) if RESO.exists() else {}
    return rows, resolutions


def join(rows: list[dict], resolutions: dict) -> list[dict]:
    enriched = []
    for r in rows:
        slug = r.get("slug")
        reso = resolutions.get(slug)
        if not reso or reso.get("resolved_up") is None:
            continue
        r2 = dict(r)
        r2["resolved_up"] = int(reso["resolved_up"])
        enriched.append(r2)
    return enriched


def brier(probs: list[float], labels: list[int]) -> float:
    return sum((p - l) ** 2 for p, l in zip(probs, labels)) / len(probs)


def calibration_buckets(probs: list[float], labels: list[int], n_buckets: int = 10) -> list[dict]:
    buckets = []
    for i in range(n_buckets):
        lo, hi = i / n_buckets, (i + 1) / n_buckets
        probs_in = [(p, l) for p, l in zip(probs, labels) if lo <= p < hi or (i == n_buckets - 1 and p == hi)]
        if not probs_in:
            continue
        avg_p = sum(p for p, _ in probs_in) / len(probs_in)
        hit_rate = sum(l for _, l in probs_in) / len(probs_in)
        buckets.append({
            "bucket": f"{lo:.1f}-{hi:.1f}",
            "n": len(probs_in),
            "avg_p": round(avg_p, 3),
            "hit_rate": round(hit_rate, 3),
            "gap": round(avg_p - hit_rate, 3),
        })
    return buckets


def per_mte_quality(rows: list[dict]) -> list[dict]:
    buckets = defaultdict(list)
    for r in rows:
        mte = r.get("minutes_to_end")
        if mte is None:
            continue
        bucket = int(mte)
        fair = r.get("fair_up")
        market = r.get("market_up_ask")
        resolved = r.get("resolved_up")
        edge = (fair - market) if (fair is not None and market is not None) else None
        buckets[bucket].append({"fair": fair, "market": market, "resolved": resolved, "edge": edge})
    out = []
    for mte in sorted(buckets.keys()):
        samples = buckets[mte]
        n = len(samples)
        fv_right = sum(1 for s in samples
                       if (s["fair"] >= 0.5 and s["resolved"] == 1)
                       or (s["fair"] < 0.5 and s["resolved"] == 0))
        mkt_right = sum(1 for s in samples
                        if (s["market"] >= 0.5 and s["resolved"] == 1)
                        or (s["market"] < 0.5 and s["resolved"] == 0))
        fv_brier = brier([s["fair"] for s in samples], [s["resolved"] for s in samples])
        mkt_brier = brier([s["market"] for s in samples], [s["resolved"] for s in samples])
        out.append({
            "mte_minute": mte,
            "n": n,
            "fv_accuracy": round(fv_right / n * 100, 1),
            "market_accuracy": round(mkt_right / n * 100, 1),
            "fv_brier": round(fv_brier, 4),
            "market_brier": round(mkt_brier, 4),
            "fv_better_than_market": fv_brier < mkt_brier,
        })
    return out


def trade_sim(rows: list[dict], target_mte: float, edge_thr_bps: float) -> dict:
    by_slug = defaultdict(list)
    for r in rows:
        by_slug[r["slug"]].append(r)

    trades = []
    for slug, slug_rows in by_slug.items():
        slug_rows.sort(key=lambda x: x.get("minutes_to_end", 0), reverse=True)
        pick = None
        for r in slug_rows:
            if r.get("minutes_to_end", 0) <= target_mte:
                pick = r
                break
        if pick is None and slug_rows:
            pick = min(slug_rows, key=lambda x: abs(x.get("minutes_to_end", 0) - target_mte))
        if pick is None:
            continue

        edge_up = pick.get("edge_up_bps")
        if edge_up is None or abs(edge_up) < edge_thr_bps:
            continue
        market_up_ask = pick.get("market_up_ask")
        resolved = pick.get("resolved_up")
        if market_up_ask is None or resolved is None:
            continue

        if edge_up > 0:
            buy_price = market_up_ask
            won = (resolved == 1)
        else:
            buy_price = 1.0 - market_up_ask
            won = (resolved == 0)

        pnl = (1.0 - buy_price) if won else -buy_price
        trades.append({"slug": slug, "buy_price": buy_price, "won": won, "pnl": pnl})

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
        "invested": round(invested, 3),
        "roi_pct": round(total_pnl / invested * 100, 2) if invested else 0,
        "avg_pnl_per_trade": round(total_pnl / n, 3),
    }


def fatal_errors(rows: list[dict]) -> list[dict]:
    fatals = []
    by_slug = defaultdict(list)
    for r in rows:
        by_slug[r["slug"]].append(r)

    for slug, slug_rows in by_slug.items():
        if not slug_rows:
            continue
        max_row = max(slug_rows, key=lambda x: abs(x.get("fair_z_score", 0)))
        z = max_row.get("fair_z_score")
        resolved = max_row.get("resolved_up")
        fair_up = max_row.get("fair_up")
        if z is None or resolved is None:
            continue
        if abs(z) >= 1.5 and (
            (z > 0 and resolved == 0) or (z < 0 and resolved == 1)
        ):
            fatals.append({
                "slug": slug,
                "max_z": round(z, 2),
                "fair_up": round(fair_up, 3) if fair_up else None,
                "resolved_up": resolved,
                "mte_at_signal": round(max_row.get("minutes_to_end", 0), 1),
            })
    return sorted(fatals, key=lambda x: -abs(x["max_z"]))


def main() -> None:
    print("Loading data...")
    rows, resolutions = load()
    print(f"  predictions: {len(rows)}")
    print(f"  resolutions: {len(resolutions)}")

    enriched = join(rows, resolutions)
    print(f"  joined: {len(enriched)} ({len(set(r['slug'] for r in enriched))} unique slugs)")

    probs = [r["fair_up"] for r in enriched]
    labels = [r["resolved_up"] for r in enriched]
    overall_brier = brier(probs, labels)
    overall_acc = sum(1 for p, l in zip(probs, labels) if (p >= 0.5) == (l == 1)) / len(probs)

    market_probs = [r["market_up_ask"] for r in enriched]
    market_brier = brier(market_probs, labels)
    market_acc = sum(1 for p, l in zip(market_probs, labels) if (p >= 0.5) == (l == 1)) / len(probs)

    print(f"\n--- OVERALL CALIBRATION ---")
    print(f"  FV:      brier={overall_brier:.4f}, accuracy={overall_acc*100:.1f}%")
    print(f"  Market:  brier={market_brier:.4f}, accuracy={market_acc*100:.1f}%")
    print(f"  FV beat market: brier={overall_brier < market_brier}, acc={overall_acc > market_acc}")

    cal = calibration_buckets(probs, labels)
    print(f"\n--- CALIBRATION BUCKETS (10 buckets) ---")
    print(f"  {'bucket':<10} {'n':>5} {'avg_p':>7} {'hit':>7} {'gap':>7}")
    for b in cal:
        print(f"  {b['bucket']:<10} {b['n']:>5} {b['avg_p']:>7.3f} {b['hit_rate']:>7.3f} {b['gap']:>+7.3f}")

    mte_q = per_mte_quality(enriched)
    print(f"\n--- PER MINUTE-TO-END ---")
    print(f"  {'mte':>3} {'n':>5} {'fv_acc%':>8} {'mkt_acc%':>9} {'fv_brier':>10} {'mkt_brier':>11} {'fv_better':>10}")
    for r in mte_q:
        marker = "yes" if r["fv_better_than_market"] else "no"
        print(f"  {r['mte_minute']:>3} {r['n']:>5} {r['fv_accuracy']:>8.1f} {r['market_accuracy']:>9.1f} "
              f"{r['fv_brier']:>10.4f} {r['market_brier']:>11.4f} {marker:>10}")

    print(f"\n--- TRADE SIMS (one bet per slug near target_mte) ---")
    sim_results = {}
    for target_mte in [1.0, 2.0, 5.0]:
        for thr in [100, 300, 500, 700]:
            key = f"mte{target_mte}_edge{thr}bps"
            res = trade_sim(enriched, target_mte, thr)
            sim_results[key] = res
            if res.get("trades", 0) > 0:
                print(f"  {key:<22} trades={res['trades']:>3} win={res['win_rate']:>5.1f}% "
                      f"ROI={res['roi_pct']:>+6.2f}% total_pnl={res['total_pnl']:>+6.3f}")

    fatals = fatal_errors(enriched)
    print(f"\n--- FATAL FV ERRORS (|z|>=1.5 and lost) ---")
    print(f"  count: {len(fatals)} out of {len(set(r['slug'] for r in enriched))} windows")
    for f in fatals[:10]:
        print(f"  {f['slug']} z={f['max_z']:+.2f} FV={f['fair_up']} resolved={f['resolved_up']}")

    report = {
        "overall": {
            "fv_brier": overall_brier,
            "market_brier": market_brier,
            "fv_accuracy": overall_acc,
            "market_accuracy": market_acc,
            "n_samples": len(enriched),
            "n_slugs": len(set(r["slug"] for r in enriched)),
        },
        "calibration_buckets": cal,
        "per_mte": mte_q,
        "trade_sims": sim_results,
        "fatal_errors": fatals,
        "verdict": {
            "fv_better_overall": overall_brier < market_brier,
            "best_roi_sim": max(
                (r["roi_pct"] for r in sim_results.values() if r.get("trades", 0) > 0),
                default=0,
            ),
            "ml_room": "high" if overall_brier - market_brier > 0.02 else "low",
        },
    }
    REPORT.write_text(json.dumps(report, indent=2, ensure_ascii=False))
    print(f"\nReport saved: {REPORT}")

    v = report["verdict"]
    print(f"\n=== VERDICT ===")
    print(f"  FV beat market overall? {v['fv_better_overall']}")
    print(f"  Best edge-strategy ROI: {v['best_roi_sim']:+.2f}%")
    print(f"  ML improvement room: {v['ml_room']}")


if __name__ == "__main__":
    main()