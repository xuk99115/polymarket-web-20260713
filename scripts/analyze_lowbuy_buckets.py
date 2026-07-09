#!/usr/bin/env python3
"""
盘口时间序列翻倍分桶分析 (2026-06-27 重写版, 跟仓位完全脱钩).

读 data/market_ticks.jsonl, 对每条 tick 回答:
"在 [t, t+15min] 这个时间窗里, 同一个 outcome 方向的最佳盘口 best_bid 是否达到过 2 × 初始 best_bid?"

按初始 best_bid 区间分桶:
    [0.00, 0.10)
    [0.10, 0.20)
    [0.20, 0.30)
    [0.30, 0.40)
    [0.40, 0.50)
    [0.50, 1.00)

每个桶的指标:
- 采样 tick 数
- "翻倍可达" 的 tick 数 (后续 15min 内 best_bid ≥ 2× 初始价)
- "翻倍可达" 占比 (核心指标: 哪些价格区间最容易翻倍)
- 实际达到的最高 best_bid (平均值 + 最大值)
- 平均间隔 (从初始价到达到 2× 用了多少分钟)

Usage:
    python3 scripts/analyze_lowbuy_buckets.py
    python3 scripts/analyze_lowbuy_buckets.py --window 10  # 10 分钟窗口
    python3 scripts/analyze_lowbuy_buckets.py --file /path/to/ticks.jsonl
"""
import argparse
import json
import os
import sys
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

DEFAULT_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "data", "market_ticks.jsonl",
)

# 分桶 (单位: 美元, 固定区间)
BUCKETS: List[Tuple[float, float, str]] = [
    (0.00, 0.10, "0.00-0.10"),
    (0.10, 0.20, "0.10-0.20"),
    (0.20, 0.30, "0.20-0.30"),
    (0.30, 0.40, "0.30-0.40"),
    (0.40, 0.50, "0.40-0.50"),
    (0.50, 1.01, "0.50-1.00"),
]

# 翻倍窗口 (从 tick 时间往后多少分钟内出现 ≥ 2× 就算可达)
DEFAULT_WINDOW_MIN = 15


def load_ticks(path: str) -> List[Dict[str, Any]]:
    if not os.path.exists(path):
        print(f"❌ 文件不存在: {path}", file=sys.stderr)
        return []
    ticks = []
    with open(path, "r", encoding="utf-8") as f:
        for lineno, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
                ev["_lineno"] = lineno
                ticks.append(ev)
            except json.JSONDecodeError as exc:
                print(f"⚠️  第 {lineno} 行 JSON 解析失败: {exc}", file=sys.stderr)
    return ticks


def parse_iso(s: str) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except Exception:
        return None


def bucket_of(price: Optional[float]) -> str:
    if price is None:
        return "UNKNOWN"
    for lo, hi, label in BUCKETS:
        if lo <= price < hi:
            return label
    return "UNKNOWN"


def group_by_outcome(ticks: List[Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
    """按 (slug, outcome_index) 分组, 每组按时间排序."""
    groups: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for t in ticks:
        key = f"{t.get('slug','')}:{t.get('outcome_index','?')}"
        groups[key].append(t)
    # 排序
    for key in groups:
        groups[key].sort(key=lambda x: parse_iso(x.get("t", "")) or datetime.min.replace(tzinfo=timezone.utc))
    return groups


def analyze(ticks: List[Dict[str, Any]], window_min: float = DEFAULT_WINDOW_MIN) -> None:
    """主分析: 对每条 tick 检查后续 window_min 分钟内是否出现 ≥ 2× 初始价."""
    if not ticks:
        print("⚠️  无数据可分析")
        return

    print(f"📂 加载 {len(ticks)} 条 tick, 按 (slug, outcome_index) 分组...")
    groups = group_by_outcome(ticks)
    print(f"   共 {len(groups)} 个 (slug, outcome) 时间序列\n")

    # 桶 -> [list of (initial_bid, hit_2x: bool, max_bid: float, time_to_2x_min: float or None)]
    bucket_data: Dict[str, List[Dict[str, Any]]] = defaultdict(list)

    total_processed = 0
    for key, series in groups.items():
        if len(series) < 2:
            continue  # 单点无意义
        # 解析时间戳数组
        times = [parse_iso(t.get("t", "")) for t in series]
        bids = [t.get("best_bid") for t in series]
        # 对每条 tick 找后续窗口
        for i, t_ev in enumerate(series):
            t0 = times[i]
            bid0 = bids[i]
            if bid0 is None or t0 is None:
                continue
            if bid0 <= 0 or bid0 >= 1.0:
                continue  # 只看 0 < bid < 1.0 (实际就是 < 0.50 区间, 因 < 0.5 才有翻倍可能)
            target = 2.0 * bid0
            # 找后续 window_min 分钟内是否有 bid >= target
            max_bid = bid0
            time_to_hit = None
            hit = False
            for j in range(i + 1, len(series)):
                tj = times[j]
                bidj = bids[j]
                if tj is None or bidj is None:
                    continue
                dt_min = (tj - t0).total_seconds() / 60
                if dt_min > window_min:
                    break
                if bidj > max_bid:
                    max_bid = bidj
                if not hit and bidj >= target:
                    hit = True
                    time_to_hit = dt_min
            bucket_data[bucket_of(bid0)].append({
                "initial_bid": bid0,
                "target_2x": target,
                "hit_2x": hit,
                "max_bid": max_bid,
                "time_to_hit_min": time_to_hit,
            })
            total_processed += 1

    print(f"{'='*88}")
    print(f"📊 盘口时间序列翻倍分析  (窗口={window_min:.0f}min, 翻倍阈值=2× 初始 best_bid)")
    print(f"{'='*88}")
    print(f"{'桶 (best_bid)':<14} {'tick数':>7} {'2×可达':>7} {'可达率':>8} {'理论上限':>10} {'平均最高':>9} {'绝对最高':>10} {'平均用时':>9}")
    print("-" * 88)

    rank_list: List[Tuple[str, float, int, int]] = []
    for lo, hi, label in BUCKETS:
        items = bucket_data.get(label, [])
        n = len(items)
        if n == 0:
            print(f"{label:<14} {0:>7} {0:>7} {'--':>8} {'100.0%':>10} {'--':>9} {'--':>10} {'--':>9}")
            continue
        hits = sum(1 for it in items if it["hit_2x"])
        hit_rate = hits / n
        max_bids = [it["max_bid"] for it in items]
        avg_max = sum(max_bids) / len(max_bids)
        abs_max = max(max_bids)
        times = [it["time_to_hit_min"] for it in items if it["time_to_hit_min"] is not None]
        avg_time = sum(times) / len(times) if times else None
        # 理论上限: 这个区间内 max(bid) 必然 <= 1.00, 所以 2×initial 必然 <= 1.0
        # 当 hi <= 0.5 时, 2 × hi <= 1.0 一定可达 (理论上)
        theoretical = 1.0 if hi <= 0.5 else 0.0
        print(
            f"{label:<14} {n:>7} {hits:>7} {hit_rate*100:>7.1f}% {theoretical*100:>9.1f}% "
            f"{avg_max*100:>8.1f}¢ {abs_max*100:>9.1f}¢ "
            f"{(f'{avg_time:.1f}m' if avg_time is not None else '--'):>9}"
        )
        rank_list.append((label, hit_rate, n, hits))

    # UNKNOWN 桶
    unk = bucket_data.get("UNKNOWN", [])
    if unk:
        print("-" * 88)
        n = len(unk)
        hits = sum(1 for it in unk if it["hit_2x"])
        print(f"{'UNKNOWN':<14} {n:>7} {hits:>7} (best_bid 缺失)")

    print("=" * 88)

    # 排名榜
    print(f"\n🏆 翻倍可达率排名 (按桶, 只看 best_bid < 0.50):")
    ranked = [(label, rate, n, hits) for (label, rate, n, hits) in rank_list
              if n >= 5 and rate > 0]  # 至少 5 个样本
    ranked.sort(key=lambda x: -x[1])
    if not ranked:
        print("   (样本不足, 需要每个桶至少 5 个 tick)")
    else:
        for i, (label, rate, n, hits) in enumerate(ranked, 1):
            medal = "🥇" if i == 1 else "🥈" if i == 2 else "🥉" if i == 3 else "  "
            print(f"   {medal} {label}: {hits}/{n} = {rate*100:.1f}%")

    # 具体到每个桶的"实际最高 bid"分布, 看看是"经常到 0.5"还是"经常到 0.9"
    print(f"\n📈 实际盘口峰值分布 (每个桶 max_bid 的分位数):")
    print(f"   {'桶':<14} {'p25':>8} {'p50':>8} {'p75':>8} {'max':>8}")
    for lo, hi, label in BUCKETS:
        items = bucket_data.get(label, [])
        if not items:
            continue
        max_bids = sorted([it["max_bid"] for it in items])
        n = len(max_bids)
        p25 = max_bids[n // 4]
        p50 = max_bids[n // 2]
        p75 = max_bids[(3 * n) // 4]
        mx = max_bids[-1]
        print(
            f"   {label:<14} {p25*100:>7.1f}¢ {p50*100:>7.1f}¢ {p75*100:>7.1f}¢ {mx*100:>7.1f}¢"
        )
    print()


def main():
    p = argparse.ArgumentParser(description="盘口时间序列翻倍分桶分析 (跟仓位脱钩)")
    p.add_argument("--file", default=DEFAULT_FILE, help=f"JSONL 文件路径 (默认 {DEFAULT_FILE})")
    p.add_argument("--window", type=float, default=DEFAULT_WINDOW_MIN,
                   help=f"翻倍窗口 (分钟, 默认 {DEFAULT_WINDOW_MIN})")
    args = p.parse_args()

    ticks = load_ticks(args.file)
    if not ticks:
        sys.exit(0)

    slugs = {t.get("slug", "") for t in ticks}
    print(f"📂 加载 {len(ticks)} 条 tick, {len(slugs)} 个 slug, 跨 {len(slugs)} 个窗口 from {args.file}\n")
    analyze(ticks, window_min=args.window)


if __name__ == "__main__":
    main()
