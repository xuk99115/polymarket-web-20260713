from typing import Any, Dict, List, Optional, Tuple

from ..core.utils import safe_float

DEFAULT_REASONABLE_SPREAD = 0.25


def _quote_is_reasonable(
    bid: Optional[float],
    ask: Optional[float],
    reference_price: Optional[float] = None,
) -> bool:
    bid = safe_float(bid)
    ask = safe_float(ask)
    reference_price = safe_float(reference_price)
    if bid is None or ask is None or bid <= 0 or ask <= 0 or ask >= 1:
        return False
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


def _pick_reasonable_quote(
    bids: List[Dict[str, Any]],
    asks: List[Dict[str, Any]],
    reference_price: Optional[float],
) -> Tuple[Optional[float], Optional[float]]:
    for bid in bids[:5]:
        bid_price = safe_float(bid.get("price"))
        if bid_price is None:
            continue
        for ask in asks[:5]:
            ask_price = safe_float(ask.get("price"))
            if ask_price is not None and _quote_is_reasonable(
                bid_price, ask_price, reference_price
            ):
                return bid_price, ask_price
    return None, None


def _merge_book_quotes(market: Dict[str, Any], book: Dict[str, Any]) -> None:
    observed_at = book.get("observed_at")
    if observed_at:
        market["book_observed_at"] = observed_at
        market["book_fetch_started_at"] = book.get("fetch_started_at")
        market["book_fetch_latency_ms"] = book.get("fetch_latency_ms")
    by_index = {item.get("index"): item for item in book.get("outcomes", [])}
    for outcome in market.get("outcomes", []):
        book_item = by_index.get(outcome.get("index"))
        if not book_item:
            continue
        bids = book_item.get("bids") or []
        asks = book_item.get("asks") or []
        outcome["depth_bids"] = bids
        outcome["depth_asks"] = asks
        if observed_at:
            outcome["book_observed_at"] = observed_at
        bid_price, ask_price = _pick_reasonable_quote(
            bids, asks, outcome.get("price")
        )
        if bid_price is None or ask_price is None:
            outcome["quote_source"] = "gamma"
            continue
        outcome["best_bid"] = bid_price
        outcome["best_ask"] = ask_price
        outcome["quote_source"] = "clob"
