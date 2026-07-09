#!/usr/bin/env python3
"""Analyze Fair Value calibration samples collected by manager.py.

Reads data/fair_value_predictions.jsonl and prints basic bucket counts. This is
only a training/diagnostic script; it does not change trading behavior.
"""

from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PRED = ROOT / "data" / "fair_value_predictions.jsonl"


def bucket(p: float) -> str:
    lo = int(p * 100 // 5 * 5)
    hi = lo + 5
    return f"{lo:02d}-{hi:02d}%"


def main() -> None:
    if not PRED.exists():
        print(f"missing: {PRED}")
        return
    rows = []
    with PRED.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    print(f"samples={len(rows)} file={PRED}")
    by_bucket = defaultdict(lambda: {"n": 0, "late_ref": 0})
    for row in rows:
        fair_up = row.get("fair_up")
        if fair_up is None:
            continue
        b = bucket(float(fair_up))
        by_bucket[b]["n"] += 1
        by_bucket[b]["late_ref"] += 1 if row.get("late_ref") else 0
    for b in sorted(by_bucket):
        v = by_bucket[b]
        print(f"fair_up {b}: n={v['n']} late_ref={v['late_ref']}")


if __name__ == "__main__":
    main()
