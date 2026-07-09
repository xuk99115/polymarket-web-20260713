#!/usr/bin/env python3
"""Compare FV-only features against FV + 5m price features.

Uses grouped cross-validation by 15m slug to avoid leaking repeated samples
from the same market window across train and test folds.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Tuple

import numpy as np
from sklearn.compose import ColumnTransformer
from sklearn.impute import SimpleImputer
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import accuracy_score, brier_score_loss, log_loss, roc_auc_score
from sklearn.model_selection import GroupKFold
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler


ROOT = Path(__file__).resolve().parents[1]
FEATURES_FILE = ROOT / "data" / "fair_value_5m_features.jsonl"
RESOLUTIONS_FILE = ROOT / "data" / "fair_value_resolutions.json"


def load_rows() -> List[Dict[str, Any]]:
    resolutions = json.loads(RESOLUTIONS_FILE.read_text(encoding="utf-8"))
    rows: List[Dict[str, Any]] = []
    with FEATURES_FILE.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            slug = str(row.get("slug") or "")
            resolved = resolutions.get(slug)
            if not resolved:
                continue
            row["resolved_up"] = int(resolved["resolved_up"])
            rows.append(row)
    return rows


def child_feature_map(row: Dict[str, Any]) -> Dict[str, float]:
    feats: Dict[str, float] = {}
    hits = 0
    imbalances: List[float] = []
    sums: List[float] = []
    active_imbalances: List[float] = []
    completed_imbalances: List[float] = []
    future_imbalances: List[float] = []
    observed_minutes: List[float] = []
    for child in row.get("five_min_children", []):
        up = child.get("up_price")
        down = child.get("down_price")
        mte = child.get("minutes_to_child_end")
        status = str(child.get("status_at_decision") or "unknown")
        if up is not None or down is not None:
            hits += 1
        if up is not None and down is not None:
            imbalance = float(up) - float(down)
            imbalances.append(imbalance)
            sums.append(float(up) + float(down))
            if status == "active":
                active_imbalances.append(imbalance)
            elif status == "completed":
                completed_imbalances.append(imbalance)
            elif status == "future":
                future_imbalances.append(imbalance)
        if (up is not None or down is not None) and mte is not None:
            observed_minutes.append(float(mte))

    def mean_or_nan(values: List[float]) -> float:
        return float(np.mean(values)) if values else np.nan

    feats["five_min_hits"] = float(hits)
    feats["five_min_mean_imbalance"] = mean_or_nan(imbalances)
    feats["five_min_mean_sum"] = mean_or_nan(sums)
    feats["five_min_active_imbalance"] = mean_or_nan(active_imbalances)
    feats["five_min_completed_imbalance"] = mean_or_nan(completed_imbalances)
    feats["five_min_future_imbalance"] = mean_or_nan(future_imbalances)
    feats["five_min_min_observed_mte"] = min(observed_minutes) if observed_minutes else np.nan
    return feats


def build_dataset(rows: List[Dict[str, Any]], *, require_5m_hits: bool) -> Tuple[List[Dict[str, float]], np.ndarray, np.ndarray]:
    samples: List[Dict[str, float]] = []
    y: List[int] = []
    groups: List[str] = []
    for row in rows:
        if row.get("late_ref"):
            continue
        if require_5m_hits and int(row.get("five_min_price_hits") or 0) <= 0:
            continue

        fair_up = row.get("fair_up")
        market_up = row.get("market_up_ask")
        edge_up = row.get("edge_up_bps")
        if fair_up is None or market_up is None or edge_up is None:
            continue

        ref_px = float(row.get("ref_px") or 0.0)
        s_now = float(row.get("s_now") or 0.0)
        base = {
            "fair_up": float(fair_up),
            "market_up_ask": float(market_up),
            "market_down_ask": float(row.get("market_down_ask") or np.nan),
            "edge_up": float(edge_up) / 10000.0,
            "minutes_to_end": float(row.get("minutes_to_end") or np.nan),
            "sigma_15m": float(row.get("sigma_15m") or np.nan),
            "log_return_from_ref": np.log(s_now / ref_px) if ref_px > 0 and s_now > 0 else np.nan,
            "fair_minus_market": float(fair_up) - float(market_up),
        }
        base.update(child_feature_map(row))
        samples.append(base)
        y.append(int(row["resolved_up"]))
        groups.append(str(row["slug"]))
    return samples, np.array(y), np.array(groups)


def evaluate(samples: List[Dict[str, float]], y: np.ndarray, groups: np.ndarray, columns: List[str]) -> Dict[str, float]:
    X = np.array([[row.get(col, np.nan) for col in columns] for row in samples], dtype=float)
    splitter = GroupKFold(n_splits=min(5, len(np.unique(groups))))

    probs = np.zeros(len(y), dtype=float)
    preds = np.zeros(len(y), dtype=int)
    for train_idx, test_idx in splitter.split(X, y, groups):
        numeric = list(range(len(columns)))
        pre = ColumnTransformer(
            transformers=[
                (
                    "num",
                    Pipeline(
                        steps=[
                            ("imputer", SimpleImputer(strategy="median")),
                            ("scaler", StandardScaler()),
                        ]
                    ),
                    numeric,
                )
            ]
        )
        model = Pipeline(
            steps=[
                ("prep", pre),
                ("clf", LogisticRegression(max_iter=2000, C=1.0)),
            ]
        )
        model.fit(X[train_idx], y[train_idx])
        fold_probs = model.predict_proba(X[test_idx])[:, 1]
        probs[test_idx] = fold_probs
        preds[test_idx] = (fold_probs >= 0.5).astype(int)

    return {
        "samples": float(len(y)),
        "groups": float(len(np.unique(groups))),
        "accuracy": accuracy_score(y, preds),
        "brier": brier_score_loss(y, probs),
        "logloss": log_loss(y, probs, labels=[0, 1]),
        "auc": roc_auc_score(y, probs) if len(np.unique(y)) > 1 else float("nan"),
        "positive_rate": float(np.mean(y)),
        "avg_prob": float(np.mean(probs)),
    }


def raw_fair_metrics(samples: List[Dict[str, float]], y: np.ndarray) -> Dict[str, float]:
    probs = np.array([row["fair_up"] for row in samples], dtype=float)
    preds = (probs >= 0.5).astype(int)
    return {
        "samples": float(len(y)),
        "groups": float("nan"),
        "accuracy": accuracy_score(y, preds),
        "brier": brier_score_loss(y, probs),
        "logloss": log_loss(y, probs, labels=[0, 1]),
        "auc": roc_auc_score(y, probs) if len(np.unique(y)) > 1 else float("nan"),
        "positive_rate": float(np.mean(y)),
        "avg_prob": float(np.mean(probs)),
    }


def print_metrics(title: str, metrics: Dict[str, float]) -> None:
    groups_str = "--" if np.isnan(metrics["groups"]) else f"{int(metrics['groups']):>2}"
    print(
        f"{title:<16} samples={int(metrics['samples']):>4} groups={groups_str} "
        f"acc={metrics['accuracy']*100:>6.2f}% brier={metrics['brier']:>7.4f} "
        f"logloss={metrics['logloss']:>7.4f} auc={metrics['auc']:>6.3f} "
        f"pos_rate={metrics['positive_rate']*100:>6.2f}% avg_p={metrics['avg_prob']:>6.3f}"
    )


def main() -> None:
    rows = load_rows()
    samples, y, groups = build_dataset(rows, require_5m_hits=True)
    if len(samples) < 50:
        raise SystemExit("not enough samples with 5m price hits")

    base_cols = [
        "fair_up",
        "market_up_ask",
        "market_down_ask",
        "edge_up",
        "minutes_to_end",
        "sigma_15m",
        "log_return_from_ref",
        "fair_minus_market",
    ]
    extra_cols = [
        "five_min_hits",
        "five_min_mean_imbalance",
        "five_min_mean_sum",
        "five_min_active_imbalance",
        "five_min_completed_imbalance",
        "five_min_future_imbalance",
        "five_min_min_observed_mte",
    ]

    print(f"dataset={FEATURES_FILE}")
    print(f"rows_with_5m_hits={len(samples)} unique_15m_slugs={len(np.unique(groups))}\n")
    print("Grouped CV by 15m slug")
    print_metrics("raw_fair_up", raw_fair_metrics(samples, y))
    print_metrics("fv_only", evaluate(samples, y, groups, base_cols))
    print_metrics("fv_plus_5m", evaluate(samples, y, groups, base_cols + extra_cols))


if __name__ == "__main__":
    main()
