#!/usr/bin/env python3
"""
盘口工具函数集
拆自 manager.py 的模块级辅助函数.
"""

from typing import Any, Dict, List, Optional, Tuple

from ..core.utils import safe_float
from ..core.config import Config

DEFAULT_REASONABLE_SPREAD = 0.25
BTC_DIRECTIONAL_EDGE = 0.01
logger = __import__("logging").getLogger("trading_manager")


def _book_summary(book: Dict[str, Any], outcome_index: int) -> str:
    for outcome_book in book.get("outcomes", []):
        if outcome_book.get("index") != outcome_index:
            continue
        summary = outcome_book.get("summary")
        if summary:
            return summary
        bids = (outcome_book.get("bids") or [])[:3]
        asks = (outcome_book.get("asks") or [])[:3]
        if bids or asks:
            return f"Bids(top3): {bids} | Asks(top3): {asks}"
    return "暂无深度数据"


def _quote_is_reasonable(bid: Optional[float], ask: Optional[float], reference_price: Optional[float] = None) -> bool:
    bid = safe_float(bid)
    ask = safe_float(ask)
    reference_price = safe_float(reference_price)
    if bid is None or ask is None:
        return False
    if bid <= 0 or ask <= 0 or ask >= 1:
        return False
    # Bug fix 2026-06-26 v2: 允许 bid >= ask (价差反转).
    if bid >= ask and ask > 0.95:
        return False
    spread = ask - bid
    if spread > DEFAULT_REASONABLE_SPREAD:
        return False
    if reference_price is not None:
        midpoint = (bid + ask) / 2
        if abs(midpoint - reference_price) > 0.2 and spread > 0.12:
            return False
    return True


def _pick_reasonable_quote(bids: List[Dict[str, Any]], asks: List[Dict[str, Any]], reference_price: Optional[float]) -> Tuple[Optional[float], Optional[float]]:
    for bid in bids[:5]:
        bid_price = safe_float(bid.get("price"))
        if bid_price is None:
            continue
        for ask in asks[:5]:
            ask_price = safe_float(ask.get("price"))
            if ask_price is None:
                continue
            if _quote_is_reasonable(bid_price, ask_price, reference_price):
                return bid_price, ask_price
    return None, None


def _filter_relevant_levels(
    levels: List[Dict[str, Any]],
    *,
    side: str,
    reference_price: Optional[float],
    anchor_price: Optional[float],
) -> List[Dict[str, Any]]:
    reference_price = safe_float(reference_price)
    anchor_price = safe_float(anchor_price)
    result: List[Dict[str, Any]] = []
    for level in levels[:8]:
        price = safe_float(level.get("price"))
        size = safe_float(level.get("size"))
        if price is None or size is None or size <= 0:
            continue
        if reference_price is not None and abs(price - reference_price) > 0.12:
            continue
        if anchor_price is not None:
            if side == "bid" and price > anchor_price + 0.03:
                continue
            if side == "bid" and price < max(0.01, anchor_price - 0.08):
                continue
            if side == "ask" and price < anchor_price - 0.03:
                continue
            if side == "ask" and price > min(0.99, anchor_price + 0.08):
                continue
        result.append({"price": round(price, 4), "size": round(size, 2)})
        if len(result) >= 3:
            break
    return result


def _format_levels(levels: List[Dict[str, Any]]) -> str:
    if not levels:
        return "--"
    return ", ".join(f"{item['price']:.3f} x {item['size']:.0f}" for item in levels)


def _market_price_edge(market: Dict[str, Any]) -> Optional[float]:
    market_prices = [safe_float(item.get("price")) for item in market.get("outcomes", [])]
    if len(market_prices) < 2 or None in market_prices[:2]:
        return None
    return abs((market_prices[0] or 0.0) - (market_prices[1] or 0.0))


def _build_ai_prompt(market: Dict[str, Any], book: Dict[str, Any], extra_context: str = "") -> str:
    outcome_lines = []
    book_lines = []
    is_btc_15m = str(market.get("slug", "")).startswith("btc-updown-15m-")
    price_edge = _market_price_edge(market)
    custom_skill = str(Config.get("AI_TRADING_SKILL", "") or "").strip()
    for outcome in market.get("outcomes", []):
        label = outcome.get("label", f"Outcome {outcome.get('index', '?')}")
        price = outcome.get("price")
        best_bid = outcome.get("best_bid")
        best_ask = outcome.get("best_ask")
        quote_source = outcome.get("quote_source", "unknown")
        outcome_lines.append(
            f"- [{outcome.get('index')}] {label}: 最新成交价 {price if price is not None else '--'}"
            f" | Bid {best_bid if best_bid is not None else '--'}"
            f" | Ask {best_ask if best_ask is not None else '--'}"
            f" | 参考价来源 {quote_source}"
        )
        book_lines.append(f"- [{outcome.get('index')}] {label}: {_book_summary(book, outcome.get('index'))}")

    prompt_rules = [
        "判定规则:",
        "1. 只有在某一边存在明确方向优势，且可成交价差可接受时，才返回 BUY。",
        "2. 如果价格接近 50/50、缺少信息、或盘口浅导致交易成本高，返回 SKIP。",
        "3. 忽略远离最新成交价和可成交参考价的极端挂单，不要把它们当成真实流动性。",
    ]
    if is_btc_15m:
        prompt_rules.extend([
            f"4. 对 BTC 15m，若 Up/Down 定价差小于 {BTC_DIRECTIONAL_EDGE:.2f} 且短线动量不强，视为无明显 edge。",
            "5. 如果 1m/3m/5m 动量同向且 15m 不逆向，可以把它视为短线方向信号；此时即便定价差只有 0.01~0.02，也可以在成本可接受时返回 BUY。",
            "6. 如果可成交参考价仅来自 gamma，说明近价 CLOB 深度不足，应将流动性视为偏弱，但不代表绝对不能交易。",
        ])

    prompt_lines = [
        f"市场问题: {market.get('question', '?')}",
        f"市场 slug: {market.get('slug', '?')}",
        f"结束时间: {market.get('end_date', '?')}",
    ]
    if price_edge is not None:
        prompt_lines.append(f"Up/Down 定价差: {price_edge:.3f}")
    prompt_lines.extend(["可交易结果:", *outcome_lines, "", "订单簿深度:", *book_lines])
    if extra_context:
        prompt_lines.extend(["", extra_context.strip()])
    if custom_skill:
        prompt_lines.extend(["", "自定义交易 Skill:", custom_skill,
                             "请把上面的内容当作交易偏好和补充判断框架，但不要违反下述硬性规则，也不要绕过风险控制。"])
    prompt_lines.extend(["", *prompt_rules,
                         "请只在某一边存在明确优势、盘口可接受、且不是临近到期的情况下返回 BUY。",
                         "如果优势不清楚、信息不足、或赔率/流动性不理想，请返回 SKIP。"])
    return "\n".join(prompt_lines)


def _merge_book_quotes(market: Dict[str, Any], book: Dict[str, Any]) -> None:
    """合并 book 深度到 market outcomes.

    也把原始 bids/asks 写入 outcome["depth_bids"] / outcome["depth_asks"],
    供 _check_take_profit 做厚度检查.
    """
    by_index = {item.get("index"): item for item in book.get("outcomes", [])}
    for outcome in market.get("outcomes", []):
        book_item = by_index.get(outcome.get("index"))
        if not book_item:
            continue
        bids = book_item.get("bids") or []
        asks = book_item.get("asks") or []
        # 存储原始深度供 TP 厚度检查
        outcome["depth_bids"] = bids
        outcome["depth_asks"] = asks
        bid_price, ask_price = _pick_reasonable_quote(bids, asks, outcome.get("price"))
        if bid_price is not None and ask_price is not None:
            outcome["best_bid"] = bid_price
            outcome["best_ask"] = ask_price
            outcome["quote_source"] = "clob"
            relevant_bids = _filter_relevant_levels(bids, side="bid", reference_price=outcome.get("price"), anchor_price=bid_price)
            relevant_asks = _filter_relevant_levels(asks, side="ask", reference_price=outcome.get("price"), anchor_price=ask_price)
            book_item["summary"] = (
                f"有效近价 Bid: {_format_levels(relevant_bids)} | "
                f"有效近价 Ask: {_format_levels(relevant_asks)}"
            )
        else:
            outcome["quote_source"] = "gamma"
            book_item["summary"] = (
                f"近价 CLOB 深度不足，忽略极端挂单；"
                f"参考成交区间 Bid {outcome.get('best_bid', '--')} / Ask {outcome.get('best_ask', '--')} (gamma)"
            )
