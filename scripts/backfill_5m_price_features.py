#!/usr/bin/env python3
"""Backfill 5m child-market price features for Fair Value prediction rows.

This script turns each recorded 15m Fair Value sample into a richer row by
joining the three related 5m child markets and their historical token prices.

Data sources
------------
- Gamma API market metadata (slug -> token ids / endDate)
- CLOB price history endpoint (token -> time series of traded prices)

Outputs
-------
- data/five_min_market_cache.json
- data/five_min_price_history_cache.json
- data/fair_value_5m_features.jsonl
"""

from __future__ import annotations

import json
from bisect import bisect_right
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests


ROOT = Path(__file__).resolve().parents[1]
PREDICTIONS = ROOT / "data" / "fair_value_predictions.jsonl"
MARKET_CACHE = ROOT / "data" / "five_min_market_cache.json"
PRICE_CACHE = ROOT / "data" / "five_min_price_history_cache.json"
OUT_FILE = ROOT / "data" / "fair_value_5m_features.jsonl"

GAMMA_MARKET_URL = "https://gamma-api.polymarket.com/markets/slug/{slug}"
CLOB_HISTORY_URL = "https://clob.polymarket.com/prices-history"


def load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def save_json(path: Path, data: Any) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def load_rows() -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    with PREDICTIONS.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            slug = str(row.get("slug") or "")
            if slug.startswith("btc-updown-15m-"):
                rows.append(row)
    return rows


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


def fetch_market_meta(slug: str, session: requests.Session) -> Optional[Dict[str, Any]]:
    resp = session.get(GAMMA_MARKET_URL.format(slug=slug), timeout=15)
    resp.raise_for_status()
    raw = resp.json()
    try:
        token_ids = raw.get("clobTokenIds") or []
        if not isinstance(token_ids, list):
            token_ids = json.loads(token_ids)
        outcomes = raw.get("outcomes") or []
        if not isinstance(outcomes, list):
            outcomes = json.loads(outcomes)
    except Exception:
        return None
    if len(token_ids) < 2:
        return None
    return {
        "slug": slug,
        "end_ts": slug_end_ts(slug),
        "end_date": raw.get("endDate"),
        "up_token_id": str(token_ids[0]),
        "down_token_id": str(token_ids[1]),
        "outcomes": outcomes[:2],
    }


def fetch_price_history(token_id: str, start_ts: int, end_ts: int, session: requests.Session) -> List[Dict[str, Any]]:
    params = {
        "market": token_id,
        "startTs": start_ts,
        "endTs": end_ts,
        "fidelity": 60,
    }
    resp = session.get(CLOB_HISTORY_URL, params=params, timeout=20)
    resp.raise_for_status()
    data = resp.json()
    history = data.get("history") or []
    cleaned = []
    for item in history:
        try:
            cleaned.append({"t": int(item["t"]), "p": float(item["p"])})
        except Exception:
            continue
    cleaned.sort(key=lambda x: x["t"])
    return cleaned


def nearest_price_at_or_before(history: List[Dict[str, Any]], ts: int) -> Optional[float]:
    if not history:
        return None
    points = [item["t"] for item in history]
    idx = bisect_right(points, ts) - 1
    if idx < 0:
        return None
    return float(history[idx]["p"])


def status_for_child(child_end_ts: Optional[int], decision_ts: int) -> str:
    if child_end_ts is None:
        return "unknown"
    child_start_ts = child_end_ts - 300
    if decision_ts >= child_end_ts:
        return "completed"
    if child_start_ts <= decision_ts < child_end_ts:
        return "active"
    return "future"


def main() -> None:
    rows = load_rows()
    if not rows:
        raise SystemExit(f"no FV rows found in {PREDICTIONS}")

    market_cache: Dict[str, Dict[str, Any]] = load_json(MARKET_CACHE, {})
    price_cache: Dict[str, List[Dict[str, Any]]] = load_json(PRICE_CACHE, {})

    child_slugs = sorted({child for row in rows for child in child_5m_slugs(str(row.get("slug") or ""))})
    session = requests.Session()

    for slug in child_slugs:
        if slug not in market_cache:
            meta = fetch_market_meta(slug, session)
            if meta:
                market_cache[slug] = meta
    save_json(MARKET_CACHE, market_cache)

    for slug, meta in market_cache.items():
        end_ts = meta.get("end_ts")
        if not end_ts:
            continue
        start_ts = int(end_ts) - 900
        end_ts_q = int(end_ts) + 60
        for side in ("up_token_id", "down_token_id"):
            token_id = str(meta.get(side) or "")
            if not token_id or token_id in price_cache:
                continue
            price_cache[token_id] = fetch_price_history(token_id, start_ts, end_ts_q, session)
    save_json(PRICE_CACHE, price_cache)

    written = 0
    coverage_rows = 0
    with OUT_FILE.open("w", encoding="utf-8") as out:
        for row in rows:
            slug_15m = str(row.get("slug") or "")
            end_ts = slug_end_ts(slug_15m)
            if end_ts is None:
                continue
            decision_ts = end_ts - int(round(float(row.get("minutes_to_end") or 0.0) * 60.0))
            feature_row = dict(row)
            child_features = []
            price_hits = 0
            for idx, child_slug in enumerate(child_5m_slugs(slug_15m), start=1):
                meta = market_cache.get(child_slug) or {}
                child_end_ts = meta.get("end_ts")
                up_hist = price_cache.get(str(meta.get("up_token_id") or ""), [])
                down_hist = price_cache.get(str(meta.get("down_token_id") or ""), [])
                up_px = nearest_price_at_or_before(up_hist, decision_ts)
                down_px = nearest_price_at_or_before(down_hist, decision_ts)
                if up_px is not None or down_px is not None:
                    price_hits += 1
                child_features.append(
                    {
                        "index": idx,
                        "slug": child_slug,
                        "status_at_decision": status_for_child(child_end_ts, decision_ts),
                        "minutes_to_child_end": (
                            round((int(child_end_ts) - decision_ts) / 60.0, 3) if child_end_ts is not None else None
                        ),
                        "up_price": up_px,
                        "down_price": down_px,
                    }
                )
            feature_row["decision_ts"] = decision_ts
            feature_row["five_min_children"] = child_features
            feature_row["five_min_price_hits"] = price_hits
            out.write(json.dumps(feature_row, ensure_ascii=False) + "\n")
            written += 1
            if price_hits > 0:
                coverage_rows += 1

    print(f"fv_rows={len(rows)}")
    print(f"child_slugs={len(child_slugs)}")
    print(f"market_cache={MARKET_CACHE}")
    print(f"price_cache={PRICE_CACHE}")
    print(f"output={OUT_FILE}")
    print(f"written_rows={written}")
    print(f"rows_with_any_5m_price={coverage_rows}")


if __name__ == "__main__":
    main()
