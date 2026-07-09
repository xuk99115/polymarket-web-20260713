#!/usr/bin/env python3
"""
盘口回放模拟 (2026-06-27) - 在 market_ticks.jsonl 上模拟 LowBuy 入仓/平仓,
按入仓价 best_bid 分桶, 统计胜率. 完全脱钩, 不读 state.

策略模仿:
- 入仓: best_bid 跌到目标价 (假设立即成交在 best_bid)
- 持仓中: 持续盯盘
- TP (赢): best_bid 达到 入仓价 × 2  → WIN
- TIME_STOP (亏): 窗口到期 (minutes_to_end <= 0) 还没翻倍 → LOSS, 按最后 bid 结算
- 每个 (slug, outcome) 只开 1 次仓, 平仓后退出

Usage:
    python3 scripts/simulate_lowbuy_buckets.py
    python3 scripts/simulate_lowbuy_buckets.py --entry-thresholds 0.10 0.20 0.30 0.40
    python3 scripts/simulate_lowbuy_buckets.py --tp-mult 2.0 --file /path/to/ticks.jsonl
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

# 固定分桶 (按入仓价 best_bid, 美元)
BUCKETS: List[Tuple[float, float, str]] = [
    (0.10, 0.20, "0.10-0.20"),
    (0.20, 0.30, "0.20-0.30"),
    (0.30, 0.40, "0.30-0.40"),
    (0.40, 0.50, "0.40-0.50"),
]

# 默认入仓阈值 (每个桶的开仓触发价: best_bid 跌到 ≤ 这个值)
# 0.10-0.20 桶 = 触发价 0.20 (bid 一旦跌到 20¢ 就开仓)
# 0.20-0.30 桶 = 触发价 0.30
# ...
DEFAULT_THRESHOLDS = {
    "0.10-0.20": 0.20,
    "0.20-0.30": 0.30,
    "0.30-0.40": 0.40,
    "0.40-0.50": 0.50,
}


def parse_iso(s: str) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except Exception:
        return None


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


def group_by_outcome(ticks: List[Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
    groups: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for t in ticks:
        key = f"{t.get('slug','')}:{t.get('outcome_index','?')}"
        groups[key].append(t)
    for key in groups:
        groups[key].sort(key=lambda x: parse_iso(x.get("t", "")) or datetime.min.replace(tzinfo=timezone.utc))
    return groups


def bucket_of_entry(price: float) -> Optional[str]:
    """按入仓价 best_bid 分桶."""
    for lo, hi, label in BUCKETS:
        if lo <= price < hi:
            return label
    return None


def simulate_series(series: List[Dict[str, Any]], threshold: float, tp_mult: float,
                    min_minutes_to_entry: float = 5.0
                    ) -> Optional[Dict[str, Any]]:
    """在一个 (slug, outcome) 时间序列上模拟一次开仓+平仓.

    规则:
    - 找第一个 best_bid <= threshold 且 minutes_to_end > min_minutes_to_entry 的 tick 作为入仓点
      (保证有足够时间窗口让 TP 跑出来, 不在窗口尾段开仓)
    - 入仓后, 后续 tick 中 best_bid >= entry × tp_mult 触发 TP (WIN)
    - 如果 minutes_to_end <= 0 还没触发 TP, 强制 TIME_STOP (LOSS), 结算价 = 最后 bid
    - 一旦平仓, 立刻退出 (不再开第二次)

    Returns: dict {entry_t, entry_price, exit_t, exit_price, result, gain_ratio, hold_min}
             或 None (没有触发入仓)
    """
    entry_t = None
    entry_price = None
    entry_idx = None
    for i, tick in enumerate(series):
        bid = tick.get("best_bid")
        mte = tick.get("minutes_to_end")
        if bid is None or bid <= 0 or bid >= 1.0:
            continue
        # 排除窗口尾段入仓 (没时间跑 TP)
        if mte is not None and mte <= min_minutes_to_entry:
            continue
        if bid <= threshold:
            entry_t = tick.get("t")
            entry_price = float(bid)
            entry_idx = i
            break
    if entry_t is None:
        return None

    target = entry_price * tp_mult
    last_bid = entry_price
    exit_t = entry_t
    exit_idx = entry_idx
    result = "LOSS"
    for j in range(entry_idx + 1, len(series)):
        tick = series[j]
        bid = tick.get("best_bid")
        mte = tick.get("minutes_to_end")
        if bid is not None and bid > 0:
            last_bid = float(bid)
        # 触发 TP
        if bid is not None and bid >= target:
            exit_t = tick.get("t")
            exit_idx = j
            result = "WIN"
            break
        # 触发 TIME_STOP (窗口到期)
        if mte is not None and mte <= 0:
            exit_t = tick.get("t")
            exit_idx = j
            result = "LOSS"
            break
    # 算持仓时长
    t0 = parse_iso(entry_t)
    t1 = parse_iso(exit_t)
    hold_min = round((t1 - t0).total_seconds() / 60, 2) if (t0 and t1) else None
    # gain_ratio = (exit - entry) / entry
    gain_ratio = round((last_bid - entry_price) / entry_price, 4) if entry_price > 0 else None
    return {
        "entry_t": entry_t,
        "entry_price": entry_price,
        "exit_t": exit_t,
        "exit_price": last_bid,
        "target_2x": target,
        "result": result,
        "gain_ratio": gain_ratio,
        "hold_min": hold_min,
        "ticks_held": exit_idx - entry_idx,
    }


def simulate_all(ticks: List[Dict[str, Any]], tp_mult: float) -> Dict[str, List[Dict[str, Any]]]:
    """对每个 (slug, outcome) 时间序列跑一次模拟, 按入仓桶分组."""
    groups = group_by_outcome(ticks)
    bucket_results: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    no_trade = 0
    for key, series in groups.items():
        if len(series) < 3:
            continue
        # 先扫一遍, 找到这个序列的最低 bid, 看它落在哪个桶
        valid_bids = [t.get("best_bid") for t in series
                      if t.get("best_bid") is not None and 0 < t.get("best_bid") < 1.0]
        if not valid_bids:
            continue
        min_bid = min(valid_bids)
        bucket = bucket_of_entry(min_bid)
        if bucket is None:
            no_trade += 1
            continue
        threshold = DEFAULT_THRESHOLDS[bucket]
        result = simulate_series(series, threshold, tp_mult)
        if result is None:
            no_trade += 1
            continue
        result["bucket"] = bucket
        result["series"] = key
        result["min_bid_seen"] = min_bid
        bucket_results[bucket].append(result)
    print(f"   共 {len(groups)} 个 (slug, outcome) 序列, "
          f"触发入仓 {sum(len(v) for v in bucket_results.values())} 次, "
          f"未触发 {no_trade} 次\n", file=sys.stderr)
    return bucket_results


def print_report(bucket_results: Dict[str, List[Dict[str, Any]]], tp_mult: float) -> None:
    """打印分桶分析表."""
    print(f"{'='*92}")
    print(f"📊 LowBuy 模拟回放 - 盘口时间序列上的开仓/平仓 (TP×{tp_mult:.1f}, 一次/序列)")
    print(f"{'='*92}")
    header = (f"{'入仓桶 (best_bid)':<18} {'开仓数':>6} {'WIN':>5} {'LOSS':>5} "
              f"{'胜率':>7} {'平均涨幅':>9} {'平均用时':>9} {'平均持tick':>10}")
    print(header)
    print("-" * 92)

    rank_list = []
    for lo, hi, label in BUCKETS:
        items = bucket_results.get(label, [])
        n = len(items)
        if n == 0:
            print(f"{label:<18} {0:>6} {0:>5} {0:>5} {'--':>7} {'--':>9} {'--':>9} {'--':>10}")
            continue
        wins = sum(1 for it in items if it["result"] == "WIN")
        losses = sum(1 for it in items if it["result"] == "LOSS")
        win_rate = wins / n
        # 平均涨幅: WIN 和 LOSS 分开算 (因为模拟结果是 raw 涨幅, 跟 PnL 不完全等价)
        gains = [it["gain_ratio"] for it in items if it["gain_ratio"] is not None]
        avg_gain = sum(gains) / len(gains) if gains else None
        # 持仓用时
        holds = [it["hold_min"] for it in items if it["hold_min"] is not None]
        avg_hold = sum(holds) / len(holds) if holds else None
        # 持 tick 数
        ticks_held = [it["ticks_held"] for it in items if it.get("ticks_held") is not None]
        avg_ticks = sum(ticks_held) / len(ticks_held) if ticks_held else None

        print(
            f"{label:<18} {n:>6} {wins:>5} {losses:>5} {win_rate*100:>6.1f}% "
            f"{(f'{avg_gain*100:+.1f}%' if avg_gain is not None else '--'):>9} "
            f"{(f'{avg_hold:.1f}m' if avg_hold is not None else '--'):>9} "
            f"{(f'{avg_ticks:.0f}' if avg_ticks is not None else '--'):>10}"
        )
        rank_list.append((label, win_rate, n, wins, losses))

    print("=" * 92)

    # 排名
    print(f"\n🏆 胜率排名 (按入仓桶, 至少 3 个样本):")
    ranked = [(l, r, n, w, ls) for (l, r, n, w, ls) in rank_list if n >= 3]
    ranked.sort(key=lambda x: -x[1])
    if not ranked:
        print("   (样本不足)")
    else:
        for i, (label, rate, n, wins, losses) in enumerate(ranked, 1):
            medal = "🥇" if i == 1 else "🥈" if i == 2 else "🥉" if i == 3 else "  "
            print(f"   {medal} {label}: 胜 {wins}/{n} = {rate*100:.1f}% (输 {losses})")

    # WIN vs LOSS 涨幅分布
    print(f"\n💰 实际涨幅 (按结果拆分):")
    print(f"   {'入仓桶':<18} {'WIN 平均':>10} {'WIN 中位':>10} {'LOSS 平均':>10} {'LOSS 中位':>10}")
    for lo, hi, label in BUCKETS:
        items = bucket_results.get(label, [])
        if not items:
            continue
        wins = [it["gain_ratio"] for it in items if it["result"] == "WIN" and it["gain_ratio"] is not None]
        losses = [it["gain_ratio"] for it in items if it["result"] == "LOSS" and it["gain_ratio"] is not None]
        win_avg = sum(wins) / len(wins) if wins else None
        win_med = sorted(wins)[len(wins) // 2] if wins else None
        loss_avg = sum(losses) / len(losses) if losses else None
        loss_med = sorted(losses)[len(losses) // 2] if losses else None
        print(
            f"   {label:<18} "
            f"{(f'{win_avg*100:+.1f}%' if win_avg is not None else '--'):>10} "
            f"{(f'{win_med*100:+.1f}%' if win_med is not None else '--'):>10} "
            f"{(f'{loss_avg*100:+.1f}%' if loss_avg is not None else '--'):>10} "
            f"{(f'{loss_med*100:+.1f}%' if loss_med is not None else '--'):>10}"
        )

    # 几个具体例子 (最近 5 笔)
    print(f"\n📋 最近 5 笔模拟 (按时间倒序):")
    all_trades = []
    for label, items in bucket_results.items():
        for it in items:
            it["_bucket"] = label
            all_trades.append(it)
    all_trades.sort(key=lambda x: x.get("entry_t", ""), reverse=True)
    for it in all_trades[:5]:
        emoji = "✅" if it["result"] == "WIN" else "❌"
        print(f"   {emoji} {it['_bucket']:<10} entry={it['entry_price']*100:.0f}¢ → "
              f"exit={it['exit_price']*100:.0f}¢ ({it['result']}, "
              f"gain={it['gain_ratio']*100:+.0f}%, {it['hold_min']:.1f}m) "
              f"series={it['series']}")
    print()


def main():
    p = argparse.ArgumentParser(description="盘口时间序列上的 LowBuy 模拟回放")
    p.add_argument("--file", default=DEFAULT_FILE, help=f"JSONL 路径 (默认 {DEFAULT_FILE})")
    p.add_argument("--tp-mult", type=float, default=2.0, help="TP 翻倍倍数 (默认 2.0)")
    p.add_argument("--show-bucket", help="只看某个桶的详细数据 (例: 0.10-0.20)")
    args = p.parse_args()

    ticks = load_ticks(args.file)
    if not ticks:
        sys.exit(0)
    slugs = {t.get("slug", "") for t in ticks}
    print(f"📂 加载 {len(ticks)} 条 tick, {len(slugs)} 个 slug\n", file=sys.stderr)

    results = simulate_all(ticks, args.tp_mult)
    print_report(results, args.tp_mult)

    if args.show_bucket:
        items = results.get(args.show_bucket, [])
        if not items:
            print(f"\n桶 {args.show_bucket} 无数据")
        else:
            print(f"\n📋 桶 {args.show_bucket} 详细 ({len(items)} 笔):")
            items.sort(key=lambda x: x.get("entry_t", ""))
            for it in items:
                emoji = "✅" if it["result"] == "WIN" else "❌"
                print(f"   {emoji} entry_t={it['entry_t']} entry={it['entry_price']*100:.0f}¢ "
                      f"→ exit={it['exit_price']*100:.0f}¢ ({it['result']}, "
                      f"gain={it['gain_ratio']*100:+.0f}%, {it['hold_min']:.1f}m, "
                      f"min_bid={it['min_bid_seen']*100:.0f}¢) series={it['series']}")


if __name__ == "__main__":
    main()
