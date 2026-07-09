"""
Hedged limit pair strategy for short BTC Up/Down markets.

Core rule:
- Start with one dynamic anchor-leg BUY limit capped by HEDGE_INITIAL_LIMIT.
- If the first leg fills, complete the opposite leg only when
  first_avg_price + hedge_vwap <= HEDGE_MAX_TOTAL_COST (default 0.97).
- Once both equal legs are filled, do not split the pair; hold to settlement.
- Near expiry, cancel untouched anchor orders and exit any naked single leg.

Risk controls added for 5m deployment:
- Do not accept the first fill too close to expiry.
- Cancel untouched anchor orders shortly before expiry.
- If only one leg is filled and the remaining buffer is too short, exit early.
"""

import logging
import math
import time as _time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from ..core.config import Config
from ..core.utils import iso_to_utc_dt, safe_float

logger = logging.getLogger("hedged_limit")

HEDGE_STRATEGY = "hedged_limit"
FINAL_PAIR_STATUSES = {"CANCELLED", "EXITED_SINGLE", "SETTLED"}
ACTIVE_PAIR_STATUSES = {"PENDING_BOTH", "LEG_OPEN", "LOCKED"}
STAGED_ORDER_STATUSES = {"STAGED", "OPEN", "PARTIAL"}


def _floor_shares(value: float, decimals: int = 2) -> float:
    factor = 10 ** decimals
    return math.floor(max(value, 0.0) * factor) / factor


def _short_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


def _parse_dt(value: Any) -> Optional[datetime]:
    if not value:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    try:
        return iso_to_utc_dt(str(value))
    except Exception:
        return None


def _window_start_from_slug(slug: str) -> Optional[datetime]:
    try:
        ts = int(str(slug).rsplit("-", 1)[-1])
        return datetime.fromtimestamp(ts, tz=timezone.utc)
    except Exception:
        return None


def _order_key(index: int) -> str:
    return str(index)


class HedgedLimitEngine:
    """Paper-first implementation of the single-leg-first hedged-pair playbook."""

    def __init__(self):
        self._last_scan_at: Optional[datetime] = None
        self._last_messages: List[str] = []

    def apply(
        self,
        markets: List[Dict[str, Any]],
        state: Dict[str, Any],
        now_utc: datetime,
        *,
        mode: str = "paper",
    ) -> List[str]:
        self._last_scan_at = now_utc
        self._last_messages = []

        if not Config.get_bool("HEDGE_LIMIT_ENABLED", "false"):
            return []
        if mode == "live" and not Config.get_bool("HEDGE_LIMIT_ALLOW_LIVE", "false"):
            logger.warning("[Hedge] live mode detected; HEDGE_LIMIT_ALLOW_LIVE=false, skip")
            return []

        pairs = state.setdefault("hedge_pairs", [])
        by_slug = {m.get("slug"): m for m in markets if m.get("slug")}

        for pair in list(pairs):
            if pair.get("status") in FINAL_PAIR_STATUSES:
                continue
            market = by_slug.get(pair.get("market_slug"))
            self._manage_pair(pair, market, state, now_utc)

        for market in markets:
            if self._should_start_pair(market, pairs, now_utc):
                pair = self._create_pair(market, state, now_utc)
                if pair:
                    pairs.insert(0, pair)
                    self._last_messages.append(
                        f"Hedge 挂双边 {market.get('slug')} @ {pair['initial_limit'] * 100:.0f}¢"
                    )
                    self._manage_pair(pair, market, state, now_utc)

        return list(self._last_messages)

    def get_state_summary(self, state: Dict[str, Any]) -> Dict[str, Any]:
        pairs = state.get("hedge_pairs", []) or []
        active = [p for p in pairs if p.get("status") in ACTIVE_PAIR_STATUSES]
        locked = [p for p in active if p.get("status") == "LOCKED"]
        single = [p for p in active if p.get("status") == "LEG_OPEN"]
        return {
            "enabled": Config.get_bool("HEDGE_LIMIT_ENABLED", "false"),
            "active_pairs": len(active),
            "locked_pairs": len(locked),
            "single_leg_pairs": len(single),
            "last_scan_at": self._last_scan_at.isoformat() if self._last_scan_at else None,
            "recent": [
                {
                    "pair_id": p.get("id"),
                    "slug": p.get("market_slug"),
                    "status": p.get("status"),
                    "locked_profit": p.get("locked_profit"),
                    "net_exposure": p.get("net_exposure"),
                }
                for p in pairs[:5]
            ],
        }

    def _market_window_seconds(self, market: Dict[str, Any]) -> int:
        slug = str(market.get("slug", ""))
        if "-5m-" in slug:
            return 300
        if "-15m-" in slug:
            return 900
        return Config.get_int("HEDGE_WINDOW_SECONDS", "300")

    def _timing(self, market: Dict[str, Any], now_utc: datetime) -> Optional[Tuple[float, float, int]]:
        end_dt = _parse_dt(market.get("end_date"))
        if not end_dt:
            return None
        window_seconds = self._market_window_seconds(market)
        start_dt = _window_start_from_slug(str(market.get("slug", "")))
        if start_dt is None:
            start_dt = end_dt.replace(tzinfo=timezone.utc) if end_dt.tzinfo is None else end_dt
            start_dt = start_dt.timestamp() - window_seconds
            start_dt = datetime.fromtimestamp(start_dt, tz=timezone.utc)
        elapsed = (now_utc - start_dt).total_seconds()
        seconds_to_end = (end_dt - now_utc).total_seconds()
        return elapsed, seconds_to_end, window_seconds

    def _supported_market(self, market: Dict[str, Any]) -> bool:
        if not market or not market.get("binary") or len(market.get("outcomes") or []) != 2:
            return False
        slug = str(market.get("slug", ""))
        prefix = str(Config.get("HEDGE_MARKET_PREFIX", "") or "").strip()
        if prefix and not slug.startswith(prefix):
            return False
        return "btc-updown-" in slug or bool(Config.get_bool("HEDGE_ALLOW_NON_BTC", "false"))

    def _should_start_pair(self, market: Dict[str, Any], pairs: List[Dict[str, Any]], now_utc: datetime) -> bool:
        if not self._supported_market(market):
            return False
        slug = market.get("slug")
        if any(p.get("market_slug") == slug for p in pairs):
            return False
        timing = self._timing(market, now_utc)
        if not timing:
            return False
        elapsed, seconds_to_end, window_seconds = timing
        first_leg_min_seconds_to_end = Config.get_float("HEDGE_FIRST_LEG_MIN_SECONDS_TO_END", "90")
        if elapsed < 0 or seconds_to_end <= first_leg_min_seconds_to_end:
            return False
        return True

    def _reserved_pending_cash(self, state: Dict[str, Any]) -> float:
        reserved = 0.0
        for pair in state.get("hedge_pairs", []) or []:
            if pair.get("status") not in ACTIVE_PAIR_STATUSES:
                continue
            for order in (pair.get("orders") or {}).values():
                if order.get("status") != "OPEN":
                    continue
                remaining = max(0.0, (safe_float(order.get("target_shares"), 0.0) or 0.0) - (safe_float(order.get("filled_shares"), 0.0) or 0.0))
                reserved += remaining * (safe_float(order.get("limit_price"), 0.0) or 0.0)
        return round(reserved, 4)

    def _create_pair(self, market: Dict[str, Any], state: Dict[str, Any], now_utc: datetime) -> Optional[Dict[str, Any]]:
        initial_limit = Config.get_float("HEDGE_INITIAL_LIMIT", "0.48")
        stake_per_leg = Config.get_float("HEDGE_STAKE_PER_LEG", "2.0")
        configured_shares = Config.get_float("HEDGE_TARGET_SHARES", "0")
        target_shares = _floor_shares(configured_shares if configured_shares > 0 else stake_per_leg / initial_limit)
        if target_shares <= 0:
            return None

        required = round(target_shares * Config.get_float("HEDGE_MAX_TOTAL_COST", "0.97"), 4)
        cash = safe_float(state.get("cash_balance"), 0.0) or 0.0
        available = cash - self._reserved_pending_cash(state)
        if available + 1e-9 < required:
            logger.warning("[Hedge] cash not enough: available=$%.2f required=$%.2f", available, required)
            return None

        entry_side = self._choose_entry_outcome(market, initial_limit)
        if not entry_side:
            return None

        orders: Dict[str, Dict[str, Any]] = {}
        for outcome in market.get("outcomes", [])[:2]:
            index = int(outcome.get("index", len(orders)))
            is_entry_side = index == entry_side.get("index")
            orders[_order_key(index)] = {
                "outcome_index": index,
                "outcome": outcome.get("label", f"Outcome {index}"),
                "token_id": outcome.get("token_id"),
                "limit_price": entry_side.get("limit_price") if is_entry_side else None,
                "target_shares": target_shares,
                "filled_shares": 0.0,
                "filled_value": 0.0,
                "avg_price": None,
                "status": "OPEN" if is_entry_side else "STAGED",
            }

        return {
            "id": _short_id("hedge"),
            "strategy": HEDGE_STRATEGY,
            "market_slug": market.get("slug"),
            "market_title": market.get("question"),
            "end_date": market.get("end_date"),
            "created_at": now_utc.isoformat(),
            "initial_limit": initial_limit,
            "max_total_cost": Config.get_float("HEDGE_MAX_TOTAL_COST", "0.97"),
            "entry_side_index": entry_side.get("index"),
            "entry_side_label": entry_side.get("label"),
            "target_shares": target_shares,
            "status": "PENDING_BOTH",
            "orders": orders,
            "locked_profit": 0.0,
            "net_exposure": 0.0,
        }

    def _manage_pair(
        self,
        pair: Dict[str, Any],
        market: Optional[Dict[str, Any]],
        state: Dict[str, Any],
        now_utc: datetime,
    ) -> None:
        if pair.get("status") == "LOCKED":
            if self._is_settle_time(pair, now_utc):
                self._settle_locked_pair(pair, state, now_utc)
            return

        seconds_to_end = self._seconds_to_end(pair, market, now_utc)
        filled_before = self._filled_orders(pair)
        if seconds_to_end is not None:
            cancel_before_end = Config.get_float("HEDGE_CANCEL_PENDING_BEFORE_END_SECONDS", "60")
            single_exit_before_end = Config.get_float("HEDGE_SINGLE_EXIT_BEFORE_END_SECONDS", "35")
            if not filled_before and 0 < seconds_to_end <= cancel_before_end:
                self._cancel_pair(pair, now_utc, f"距结算仅剩 {int(seconds_to_end)}s，撤销首腿挂单")
                return
            if len(filled_before) == 1 and 0 < seconds_to_end <= single_exit_before_end:
                self._exit_single_leg(pair, market, state, now_utc, early=True)
                return

        if market:
            self._refresh_order_limits(pair)
            for outcome in market.get("outcomes", [])[:2]:
                order = (pair.get("orders") or {}).get(_order_key(int(outcome.get("index", 0))))
                if order and order.get("status") in {"OPEN", "PARTIAL"}:
                    self._try_fill_order(pair, order, outcome, state, now_utc)
            self._refresh_pair_status(pair)

            # First leg may fill this cycle. Open hedge leg and try immediate hedge
            # in same market snapshot instead of waiting next polling round.
            self._refresh_order_limits(pair)
            for outcome in market.get("outcomes", [])[:2]:
                order = (pair.get("orders") or {}).get(_order_key(int(outcome.get("index", 0))))
                if order and order.get("status") in {"OPEN", "PARTIAL"}:
                    self._try_fill_order(pair, order, outcome, state, now_utc)
            self._refresh_pair_status(pair)

        if pair.get("status") == "LOCKED":
            return

        if self._is_settle_time(pair, now_utc):
            if self._filled_orders(pair):
                self._exit_single_leg(pair, market, state, now_utc)
            else:
                self._cancel_pair(pair, now_utc, "到期前未成交，取消首腿挂单")

    def _choose_entry_outcome(self, market: Dict[str, Any], max_entry: float) -> Optional[Dict[str, Any]]:
        candidates: List[Dict[str, Any]] = []
        for outcome in market.get("outcomes", [])[:2]:
            best_ask = safe_float(outcome.get("best_ask"), outcome.get("price"))
            if best_ask is None or best_ask <= 0:
                continue
            if best_ask - max_entry > 1e-9:
                continue
            limit_price = min(max_entry, best_ask)
            limit_price = round(limit_price, 4)
            if limit_price <= 0:
                continue
            gap = round(best_ask - limit_price, 6)
            candidates.append({
                "index": int(outcome.get("index", 0)),
                "label": outcome.get("label"),
                "limit_price": limit_price,
                "best_ask": best_ask,
                "gap": gap,
            })
        if not candidates:
            return None
        candidates.sort(key=lambda item: (item["gap"], item["best_ask"], item["index"]))
        return candidates[0]

    def _refresh_order_limits(self, pair: Dict[str, Any]) -> None:
        filled = self._filled_orders(pair)
        if len(filled) != 1:
            return
        first = filled[0]
        cap = round((safe_float(pair.get("max_total_cost"), 0.97) or 0.97) - (safe_float(first.get("avg_price"), first.get("limit_price")) or 0.0), 4)
        cap = min(cap, Config.get_float("HEDGE_MAX_HEDGE_PRICE", "0.50"))
        for order in (pair.get("orders") or {}).values():
            if order.get("filled_shares"):
                continue
            if order.get("status") in STAGED_ORDER_STATUSES:
                order["limit_price"] = max(0.0, cap)
                order["status"] = "OPEN"

    def _try_fill_order(
        self,
        pair: Dict[str, Any],
        order: Dict[str, Any],
        outcome: Dict[str, Any],
        state: Dict[str, Any],
        now_utc: datetime,
    ) -> None:
        existing_filled = self._filled_orders(pair)
        if not existing_filled:
            seconds_to_end = self._seconds_to_end(pair, {"end_date": pair.get("end_date"), "slug": pair.get("market_slug")}, now_utc)
            first_leg_min = Config.get_float("HEDGE_FIRST_LEG_MIN_SECONDS_TO_END", "120")
            if seconds_to_end is not None and seconds_to_end < first_leg_min:
                self._cancel_pair(pair, now_utc, f"首腿最晚成交保护触发，距结算仅剩 {int(seconds_to_end)}s")
                return

        remaining = (safe_float(order.get("target_shares"), 0.0) or 0.0) - (safe_float(order.get("filled_shares"), 0.0) or 0.0)
        remaining = _floor_shares(remaining)
        if remaining <= 0:
            order["status"] = "FILLED"
            return
        limit_price = safe_float(order.get("limit_price"), 0.0) or 0.0
        fill_shares, avg_price = self._fillable_shares(outcome, limit_price, remaining)
        fill_shares = _floor_shares(fill_shares)
        if fill_shares <= 0 or avg_price <= 0:
            return

        max_affordable = _floor_shares((safe_float(state.get("cash_balance"), 0.0) or 0.0) / avg_price)
        fill_shares = min(fill_shares, max_affordable)
        if fill_shares <= 0:
            return

        value = round(fill_shares * avg_price, 4)
        state["cash_balance"] = round((safe_float(state.get("cash_balance"), 0.0) or 0.0) - value, 4)
        order["filled_shares"] = round((safe_float(order.get("filled_shares"), 0.0) or 0.0) + fill_shares, 4)
        order["filled_value"] = round((safe_float(order.get("filled_value"), 0.0) or 0.0) + value, 4)
        order["avg_price"] = round(order["filled_value"] / order["filled_shares"], 6) if order["filled_shares"] > 0 else None
        order["filled_at"] = now_utc.isoformat()
        if order["filled_shares"] + 1e-9 >= (safe_float(order.get("target_shares"), 0.0) or 0.0):
            order["status"] = "FILLED"
        else:
            order["status"] = "PARTIAL"

        trade_id = _short_id("hedge-buy")
        order.setdefault("trade_ids", []).append(trade_id)
        state.setdefault("trades", []).insert(0, {
            "id": trade_id,
            "created_at": now_utc.isoformat(),
            "side": "BUY",
            "outcome": order.get("outcome"),
            "outcome_index": order.get("outcome_index"),
            "market": pair.get("market_title"),
            "market_slug": pair.get("market_slug"),
            "amount": value,
            "size": fill_shares,
            "price": round(avg_price, 6),
            "status": "OPEN",
            "reason": f"[Hedge] limit fill pair={pair.get('id')} limit={limit_price:.2f}",
            "strategy": HEDGE_STRATEGY,
            "source": HEDGE_STRATEGY,
            "pair_id": pair.get("id"),
            "hedge_pair_id": pair.get("id"),
        })
        self._last_messages.append(f"Hedge 成交 {order.get('outcome')} {fill_shares:.2f} @ {avg_price * 100:.1f}¢")

    def _fillable_shares(self, outcome: Dict[str, Any], limit_price: float, needed: float) -> Tuple[float, float]:
        levels = []
        for level in outcome.get("depth_asks") or []:
            price = safe_float(level.get("price"))
            size = safe_float(level.get("size"))
            if price is None or size is None or size <= 0 or price > limit_price + 1e-9:
                continue
            levels.append((price, size))
        if levels:
            remaining = needed
            filled = 0.0
            value = 0.0
            for price, size in sorted(levels, key=lambda item: item[0]):
                take = min(remaining, size)
                filled += take
                value += take * price
                remaining -= take
                if remaining <= 1e-9:
                    break
            return filled, (value / filled if filled > 0 else 0.0)

        best_ask = safe_float(outcome.get("best_ask"))
        if best_ask is not None and 0 < best_ask <= limit_price + 1e-9:
            return needed, best_ask
        return 0.0, 0.0

    def _filled_orders(self, pair: Dict[str, Any]) -> List[Dict[str, Any]]:
        return [
            order for order in (pair.get("orders") or {}).values()
            if (safe_float(order.get("filled_shares"), 0.0) or 0.0) > 0
        ]

    def _refresh_pair_status(self, pair: Dict[str, Any]) -> None:
        if pair.get("status") in FINAL_PAIR_STATUSES:
            return
        orders = list((pair.get("orders") or {}).values())
        filled = self._filled_orders(pair)
        if pair.get("cancel_reason") and not filled:
            pair["status"] = "CANCELLED"
            pair["net_exposure"] = 0.0
            return
        if len(filled) == 2:
            shares = [safe_float(o.get("filled_shares"), 0.0) or 0.0 for o in orders]
            prices = [safe_float(o.get("avg_price"), 0.0) or 0.0 for o in orders]
            if abs(shares[0] - shares[1]) <= 0.01 and sum(prices) <= (safe_float(pair.get("max_total_cost"), 0.97) or 0.97) + 1e-9:
                pair["status"] = "LOCKED"
                locked_per_share = round(1.0 - sum(prices), 6)
                pair["locked_profit"] = round(min(shares) * locked_per_share, 4)
                pair["net_exposure"] = 0.0
                for order in orders:
                    order["status"] = "FILLED"
                self._last_messages.append(
                    f"Hedge 锁利完成 {pair.get('market_slug')} profit=${pair['locked_profit']:.3f}"
                )
            else:
                pair["status"] = "LEG_OPEN"
                pair["net_exposure"] = round(shares[0] - shares[1], 4)
        elif len(filled) == 1:
            pair["status"] = "LEG_OPEN"
            pair["net_exposure"] = round(safe_float(filled[0].get("filled_shares"), 0.0) or 0.0, 4)
        else:
            pair["status"] = "PENDING_BOTH"
            pair["net_exposure"] = 0.0

    def _half_time_reached(self, pair: Dict[str, Any], market: Optional[Dict[str, Any]], now_utc: datetime) -> bool:
        end_dt = _parse_dt(pair.get("end_date") or (market or {}).get("end_date"))
        if not end_dt:
            return False
        window_seconds = self._market_window_seconds(market or pair)
        start_dt = _window_start_from_slug(str(pair.get("market_slug", "")))
        if start_dt is None:
            start_dt = datetime.fromtimestamp(end_dt.timestamp() - window_seconds, tz=timezone.utc)
        half = window_seconds * Config.get_float("HEDGE_ENTRY_FRACTION", "0.5")
        return (now_utc - start_dt).total_seconds() >= half

    def _seconds_to_end(self, pair: Dict[str, Any], market: Optional[Dict[str, Any]], now_utc: datetime) -> Optional[float]:
        end_dt = _parse_dt(pair.get("end_date") or (market or {}).get("end_date"))
        if not end_dt:
            return None
        return (end_dt - now_utc).total_seconds()

    def _seconds_to_half(self, pair: Dict[str, Any], market: Optional[Dict[str, Any]], now_utc: datetime) -> Optional[float]:
        end_dt = _parse_dt(pair.get("end_date") or (market or {}).get("end_date"))
        if not end_dt:
            return None
        window_seconds = self._market_window_seconds(market or pair)
        start_dt = _window_start_from_slug(str(pair.get("market_slug", "")))
        if start_dt is None:
            start_dt = datetime.fromtimestamp(end_dt.timestamp() - window_seconds, tz=timezone.utc)
        half = window_seconds * Config.get_float("HEDGE_ENTRY_FRACTION", "0.5")
        return (start_dt + timedelta(seconds=half) - now_utc).total_seconds()

    def _is_settle_time(self, pair: Dict[str, Any], now_utc: datetime) -> bool:
        end_dt = _parse_dt(pair.get("end_date"))
        if not end_dt:
            return False
        return now_utc >= end_dt

    def _settle_locked_pair(self, pair: Dict[str, Any], state: Dict[str, Any], now_utc: datetime) -> None:
        orders = list((pair.get("orders") or {}).values())
        if len(orders) != 2:
            return
        shares = min(safe_float(o.get("filled_shares"), 0.0) or 0.0 for o in orders)
        total_cost = sum(safe_float(o.get("filled_value"), 0.0) or 0.0 for o in orders)
        payout = round(shares * 1.0, 4)
        pnl = round(payout - total_cost, 4)
        state["cash_balance"] = round((safe_float(state.get("cash_balance"), 0.0) or 0.0) + payout, 4)
        pair["status"] = "SETTLED"
        pair["settled_at"] = now_utc.isoformat()
        pair["realized_profit"] = pnl
        self._mark_pair_trades(
            state,
            pair.get("id"),
            "SETTLED_LEG",
            close_price=None,
            realized_profit=None,
            close_reason_code="hedge_settle",
            close_reason_label="到期结算",
        )
        self._record_pair_result(state, pair, now_utc, "SETTLE", shares, payout, pnl, "双侧锁利到期结算")
        self._update_stats(state, pnl)
        self._last_messages.append(f"Hedge 到期结算 profit=${pnl:+.3f}")

    def _exit_single_leg(
        self,
        pair: Dict[str, Any],
        market: Optional[Dict[str, Any]],
        state: Dict[str, Any],
        now_utc: datetime,
        early: bool = False,
    ) -> None:
        filled = self._filled_orders(pair)
        if len(filled) != 1:
            self._cancel_pair(pair, now_utc, "半程状态异常，取消")
            return
        order = filled[0]
        exit_price = self._exit_bid_for_order(order, market)
        shares = safe_float(order.get("filled_shares"), 0.0) or 0.0
        cost = safe_float(order.get("filled_value"), 0.0) or 0.0
        proceeds = round(shares * exit_price, 4)
        pnl = round(proceeds - cost, 4)
        state["cash_balance"] = round((safe_float(state.get("cash_balance"), 0.0) or 0.0) + proceeds, 4)
        pair["status"] = "EXITED_SINGLE"
        pair["closed_at"] = now_utc.isoformat()
        pair["realized_profit"] = pnl
        pair["exit_price"] = exit_price
        pair["exit_reason"] = "补腿时间不足，提前单腿离场" if early else "半程单腿离场"
        pair["net_exposure"] = 0.0
        for pending in (pair.get("orders") or {}).values():
            if pending.get("status") != "FILLED":
                pending["status"] = "CANCELLED"
                pending["cancel_reason"] = "首腿已成交，剩余补腿时间不足"
        self._mark_pair_trades(
            state,
            pair.get("id"),
            "TIME_STOP",
            close_price=exit_price,
            realized_profit=pnl,
            close_reason_code="hedge_early_exit" if early else "hedge_half_exit",
            close_reason_label="提前离场" if early else "半程离场",
        )
        self._update_stats(state, pnl)
        if early:
            self._last_messages.append(f"Hedge 提前单腿离场 {order.get('outcome')} @ {exit_price * 100:.1f}¢ pnl=${pnl:+.3f}")
        else:
            self._last_messages.append(f"Hedge 半程单腿离场 {order.get('outcome')} @ {exit_price * 100:.1f}¢ pnl=${pnl:+.3f}")

    def _exit_bid_for_order(self, order: Dict[str, Any], market: Optional[Dict[str, Any]]) -> float:
        if market:
            for outcome in market.get("outcomes", []) or []:
                if outcome.get("index") == order.get("outcome_index"):
                    bid = safe_float(outcome.get("best_bid"), outcome.get("price"))
                    if bid is not None and bid > 0:
                        return bid
        return safe_float(order.get("avg_price"), order.get("limit_price")) or 0.0

    def _cancel_pair(self, pair: Dict[str, Any], now_utc: datetime, reason: str) -> None:
        pair["status"] = "CANCELLED"
        pair["closed_at"] = now_utc.isoformat()
        pair["cancel_reason"] = reason
        for order in (pair.get("orders") or {}).values():
            if order.get("status") != "FILLED":
                order["status"] = "CANCELLED"
        self._last_messages.append(f"Hedge 取消 {pair.get('market_slug')}: {reason}")

    def _mark_pair_trades(
        self,
        state: Dict[str, Any],
        pair_id: Optional[str],
        status: str,
        *,
        close_price: Optional[float],
        realized_profit: Optional[float],
        close_reason_code: Optional[str] = None,
        close_reason_label: Optional[str] = None,
    ) -> None:
        for trade in state.get("trades", []) or []:
            if trade.get("hedge_pair_id") != pair_id or trade.get("status") != "OPEN":
                continue
            trade["status"] = status
            trade["closed_at"] = datetime.now(timezone.utc).isoformat()
            if close_reason_code:
                trade["close_reason_code"] = close_reason_code
            if close_reason_label:
                trade["close_reason_label"] = close_reason_label
            if close_price is not None:
                trade["close_price"] = close_price
            if realized_profit is not None:
                trade["realized_profit"] = realized_profit

    def _record_pair_result(
        self,
        state: Dict[str, Any],
        pair: Dict[str, Any],
        now_utc: datetime,
        side: str,
        shares: float,
        amount: float,
        pnl: float,
        reason: str,
    ) -> None:
        state.setdefault("trades", []).insert(0, {
            "id": _short_id("hedge-result"),
            "created_at": now_utc.isoformat(),
            "side": side,
            "outcome": "PAIR",
            "market": pair.get("market_title"),
            "market_slug": pair.get("market_slug"),
            "amount": round(amount, 4),
            "size": round(shares, 4),
            "price": 1.0,
            "status": "SETTLED",
            "reason": f"[Hedge] {reason}",
            "realized_profit": pnl,
            "strategy": HEDGE_STRATEGY,
            "source": HEDGE_STRATEGY,
            "pair_id": pair.get("id"),
            "hedge_pair_id": pair.get("id"),
        })

    def _update_stats(self, state: Dict[str, Any], pnl: float) -> None:
        stats = state.setdefault("stats", {"total_trades": 0, "winning_trades": 0, "losing_trades": 0, "total_profit": 0.0})
        stats["total_trades"] = int(stats.get("total_trades", 0) or 0) + 1
        stats["total_profit"] = round((safe_float(stats.get("total_profit"), 0.0) or 0.0) + pnl, 4)
        if pnl >= 0:
            stats["winning_trades"] = int(stats.get("winning_trades", 0) or 0) + 1
        else:
            stats["losing_trades"] = int(stats.get("losing_trades", 0) or 0) + 1
