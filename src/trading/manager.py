import asyncio
import json
import logging
import math
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ..api.fair_value import (
    DEFAULT_WINDOW_SEC as FAIR_WINDOW_SEC,
    MIN_SIGMA as FAIR_MIN_SIGMA,
    compute_fair_updown,
)
from ..api.market import BTCDataprovider, PolymarketClient
from ..core.config import Config, CONTROL_FILE, DATA_DIR, PAPER_STATE_FILE
from ..core.state import StateManager, StatusExporter
from ..core.utils import (
    iso_to_utc_dt,
    load_json_file,
    load_trading_control,
    safe_float,
    save_json_file,
)
from .audit import _append_position_audit
from .executor import LiveExecutor, PaperExecutor
from .fv_edge import FVEdgeStrategy
from .market_helpers import _merge_book_quotes

logger = logging.getLogger("fv_edge_manager")

ACTIVE_ORDER_STATUSES = {"SUBMITTED", "OPEN", "PENDING", "PENDING_FILL", "PARTIAL_FILL"}
CLOSED_TRADE_STATUSES = {
    "EXPIRY_EXIT",
    "CLOSED",
    "SETTLED",
    "FILLED",
}
_JSONL_ERROR_LOGGED_AT: Dict[str, float] = {}


def _market_settled(market: Dict[str, Any]) -> bool:
    raw_prices = market.get("outcomePrices")
    if not isinstance(raw_prices, (list, tuple)) or len(raw_prices) < 2:
        return False
    try:
        prices = [float(value) for value in raw_prices[:2]]
    except (TypeError, ValueError):
        return False
    return any(price >= 0.95 for price in prices) and any(price <= 0.05 for price in prices)


def _resolve_settlement_price(
    market: Dict[str, Any], outcome: Dict[str, Any]
) -> Optional[float]:
    if not _market_settled(market):
        return None
    outcome_index = outcome.get("index")
    prices = market.get("outcomePrices") or []
    if not isinstance(outcome_index, int) or outcome_index >= len(prices):
        return None
    try:
        value = float(prices[outcome_index])
    except (TypeError, ValueError):
        return None
    return 1.0 if value >= 0.95 else 0.0


class _SaveBatchContext:
    def __init__(self, state_manager: StateManager):
        self._state_manager = state_manager
        self._was_deferred = False

    def __enter__(self):
        self._was_deferred = self._state_manager._defer_save
        self._state_manager._defer_save = True
        return self._state_manager

    def __exit__(self, exc_type, exc, traceback):
        self._state_manager._defer_save = self._was_deferred
        if not self._was_deferred:
            self._state_manager.flush()
        return False


class TradingBotManager:
    """FV Edge-only trading loop for BTC 15-minute markets."""

    # ================================================================
    # 双目录架构：核心状态写永久卷，日志写临时卷
    # RUNTIME_DIR: 容器临时卷 — 零 EIO，断电丢失可接受
    # PERSIST_DIR: 永久卷 — 断电不丢
    # ================================================================
    _RUNTIME_DIR = os.environ.get("RUNTIME_DIR", "/tmp/polymarket-fv-edge/data")
    _PERSIST_DIR = os.environ.get("PERSIST_DIR", os.path.join(str(Path(__file__).resolve().parents[2]), "data"))
    os.makedirs(_RUNTIME_DIR, exist_ok=True)
    os.makedirs(_PERSIST_DIR, exist_ok=True)

    # 核心状态 → 永久卷（断电不丢）
    BTC_SNAPSHOT_FILE = os.path.join(_PERSIST_DIR, "btc_snapshot.json")
    BTC_WINDOW_REFS_FILE = os.path.join(_PERSIST_DIR, "btc_window_refs.json")
    POSITION_AUDIT_FILE = os.path.join(_PERSIST_DIR, "position_audit.jsonl")

    # 日志型数据 → 临时卷（EIO 无害）
    BTC_TICKS_FILE = os.path.join(_RUNTIME_DIR, "btc_ticks.jsonl")
    FAIR_VALUE_PREDICTIONS_FILE = os.path.join(_RUNTIME_DIR, "fair_value_predictions.jsonl")

    def __init__(self):
        self.state_manager = StateManager(PAPER_STATE_FILE)
        self.market_api = PolymarketClient()
        self.btc_api = BTCDataprovider()
        self.fv_edge = FVEdgeStrategy(
            position_usd=Config.get_float("FV_EDGE_POSITION_USD", "2.0"),
            threshold_bps=Config.get_float("FV_EDGE_THRESHOLD_BPS", "300"),
            max_mte=Config.get_float("FV_EDGE_MAX_MTE", "1.5"),
            min_price=Config.get_float("FV_EDGE_MIN_PRICE", "0.10"),
            max_price=Config.get_float("FV_EDGE_MAX_PRICE", "0.85"),
            require_favorite_side=Config.get_bool("FV_EDGE_REQUIRE_FAVORITE_SIDE", "true"),
            require_chainlink=Config.get_bool("FV_EDGE_REQUIRE_CHAINLINK", "true"),
            max_book_age_seconds=Config.get_float("FV_EDGE_MAX_BOOK_AGE_SECONDS", "3"),
        )

        requested_mode = self._requested_mode()
        self.executor = self._create_executor(requested_mode)
        self.current_mode = "live" if self.executor.mode == "live" else "paper_live"
        if requested_mode == "live" and self.current_mode != "live":
            self._force_trading_mode("paper_live")

        self.running = True
        self._market_cache: List[Dict[str, Any]] = []
        self._market_cache_at = 0.0
        self._latest_markets: List[Dict[str, Any]] = []
        self._latest_btc: Dict[str, Any] = {}
        self._btc_history: List[Dict[str, Any]] = []
        self._btc_window_refs: Dict[str, Dict[str, Any]] = (
            load_json_file(self.BTC_WINDOW_REFS_FILE, {}) or {}
        )

        state = self.state_manager.get_state()
        state["strategy"] = "fv_edge"
        state.setdefault("fv_signal_history", [])
        self.state_manager.save()

    @staticmethod
    def _requested_mode() -> str:
        value = str(Config.get("TRADING_MODE", "paper_live") or "paper_live").lower()
        return "live" if value == "live" else "paper_live"

    def _create_executor(self, mode: str):
        if mode != "live":
            return PaperExecutor(self.state_manager)
        if not Config.get_bool("FV_EDGE_ENABLE_LIVE", "false"):
            logger.error("FV Edge live mode disabled: settlement/redeem lifecycle is not implemented")
            return PaperExecutor(self.state_manager)
        try:
            return LiveExecutor(self.state_manager)
        except Exception as exc:
            logger.error("实盘执行器初始化失败，退回模拟盘: %s", exc)
            return PaperExecutor(self.state_manager)

    def _force_trading_mode(self, mode: str) -> None:
        control = load_json_file(CONTROL_FILE, {}) or {}
        control["TRADING_MODE"] = mode
        save_json_file(CONTROL_FILE, control)
        Config.invalidate()

    async def check_mode_swap(self) -> None:
        requested_mode = self._requested_mode()
        effective_mode = "live" if self.current_mode == "live" else "paper_live"
        if requested_mode == effective_mode:
            return

        state = self.state_manager.get_state()
        has_exposure = bool(state.get("positions")) or any(
            order.get("status") in ACTIVE_ORDER_STATUSES
            for order in state.get("orders", [])
        )
        if has_exposure:
            logger.error("存在持仓或在途订单，拒绝切换交易模式")
            self._force_trading_mode(effective_mode)
            return

        executor = self._create_executor(requested_mode)
        new_mode = "live" if executor.mode == "live" else "paper_live"
        self.executor = executor
        self.current_mode = new_mode
        if requested_mode != new_mode:
            self._force_trading_mode(new_mode)

    async def _get_markets(self, now_utc: datetime) -> List[Dict[str, Any]]:
        refresh_seconds = max(2.0, Config.get_float("FV_MARKET_REFRESH_SECONDS", "10"))
        now_mono = time.monotonic()
        if self._market_cache and now_mono - self._market_cache_at < refresh_seconds:
            return self._market_cache
        markets = await self.market_api.get_market_snapshots(now_utc)
        self._market_cache = [
            market
            for market in markets
            if str(market.get("slug", "")).startswith("btc-updown-15m-")
        ]
        self._market_cache_at = now_mono
        return self._market_cache

    async def _refresh_books(self, markets: List[Dict[str, Any]]) -> None:
        async def refresh(market: Dict[str, Any]) -> None:
            try:
                book = await self.market_api.get_microstructure(market)
                _merge_book_quotes(market, book)
            except Exception as exc:
                logger.debug("盘口刷新失败 %s: %s", market.get("slug"), exc)

        await asyncio.gather(*(refresh(market) for market in markets))

    async def _load_position_markets(
        self, known_markets: List[Dict[str, Any]]
    ) -> Dict[str, Dict[str, Any]]:
        by_slug = {
            market.get("slug"): market
            for market in known_markets
            if market.get("slug")
        }
        state = self.state_manager.get_state()
        missing = {
            position.get("market_slug")
            for position in state.get("positions", [])
            if position.get("market_slug") not in by_slug
        }
        missing |= {
            order.get("market_slug")
            for order in state.get("orders", [])
            if order.get("status") in ACTIVE_ORDER_STATUSES
            and order.get("market_slug") not in by_slug
        }

        async def load(slug: str) -> Tuple[str, Optional[Dict[str, Any]]]:
            market = await self.market_api.get_market(slug)
            if market:
                await self._refresh_books([market])
            return slug, market

        if missing:
            for slug, market in await asyncio.gather(*(load(slug) for slug in missing if slug)):
                if market:
                    by_slug[slug] = market
        return by_slug

    @staticmethod
    def _find_outcome(
        market: Dict[str, Any], position: Dict[str, Any]
    ) -> Optional[Dict[str, Any]]:
        for outcome in market.get("outcomes", []):
            if position.get("token_id") == outcome.get("token_id"):
                return outcome
            if position.get("outcome_index") == outcome.get("index"):
                return outcome
        return None

    async def _manage_positions(
        self, now_utc: datetime, markets: List[Dict[str, Any]]
    ) -> List[str]:
        markets_by_slug = await self._load_position_markets(markets)
        messages = await self.executor.sync_state(markets_by_slug, now_utc)
        state = self.state_manager.get_state()

        for position in list(state.get("positions", [])):
            if position.get("status", "OPEN") != "OPEN":
                continue
            market = markets_by_slug.get(position.get("market_slug"))
            if not market:
                continue
            outcome = self._find_outcome(market, position)
            if not outcome:
                continue
            position["current_bid"] = safe_float(
                outcome.get("best_bid"), position.get("current_bid")
            )
            position["current_ask"] = safe_float(
                outcome.get("best_ask"), position.get("current_ask")
            )

            end_date = market.get("end_date") or position.get("end_date")
            expired = bool(market.get("closed"))
            if end_date:
                try:
                    expired = expired or iso_to_utc_dt(end_date) <= now_utc
                except (TypeError, ValueError):
                    pass
            if not expired:
                continue

            if self.current_mode == "live":
                position["status"] = "SETTLEMENT_PENDING"
                continue

            settlement = _resolve_settlement_price(market, outcome)
            if settlement is None:
                continue
            message = await self.executor.close_position(
                position, settlement, "EXPIRY_EXIT"
            )
            messages.append(message)
            _append_position_audit(
                self.POSITION_AUDIT_FILE,
                {
                    "t": now_utc.isoformat(),
                    "action": "FV_EDGE_SETTLE",
                    "slug": market.get("slug"),
                    "outcome_index": outcome.get("index"),
                    "close_price": settlement,
                    "strategy": "fv_edge",
                },
            )

        self._refresh_summary()
        self.state_manager.save()
        return messages

    def _has_exposure(self, slug: str) -> bool:
        state = self.state_manager.get_state()
        if any(
            position.get("market_slug") == slug
            and position.get("status", "OPEN") in {"OPEN", "CLOSING", "SETTLEMENT_PENDING"}
            for position in state.get("positions", [])
        ):
            return True
        return any(
            order.get("market_slug") == slug
            and order.get("status") in ACTIVE_ORDER_STATUSES
            for order in state.get("orders", [])
        )

    def _open_exposure_count(self) -> int:
        state = self.state_manager.get_state()
        positions = sum(
            position.get("status", "OPEN") in {"OPEN", "CLOSING", "SETTLEMENT_PENDING"}
            for position in state.get("positions", [])
        )
        orders = sum(
            order.get("status") in ACTIVE_ORDER_STATUSES
            for order in state.get("orders", [])
        )
        return positions + orders

    async def _open_signal(
        self,
        signal: Dict[str, Any],
        market: Dict[str, Any],
        now_utc: datetime,
    ) -> Optional[str]:
        slug = market.get("slug", "")
        outcome_index = signal.get("outcome_index")
        outcomes = market.get("outcomes") or []
        if self._has_exposure(slug):
            return None
        if self._open_exposure_count() >= Config.get_int("FV_EDGE_MAX_OPEN_POSITIONS", "1"):
            return None
        if not isinstance(outcome_index, int) or not 0 <= outcome_index < len(outcomes):
            return None

        fresh_market = market
        signal_to_execute = signal
        if self.current_mode == "live":
            fetched = await self.market_api.get_market(slug)
            if not fetched:
                return None
            await self._refresh_books([fetched])
            fresh_market = fetched
            outcomes = fresh_market.get("outcomes") or []
            if outcome_index >= len(outcomes):
                return None
            # Re-run the complete FV decision on the fresh BTC, tau and book;
            # an ask-only drift check cannot detect a changed fair value.
            if self._latest_btc:
                self.fv_edge.update_btc_snapshot(self._latest_btc, self._btc_window_refs)
                refreshed = self.fv_edge.scan([fresh_market], now_utc)
                signal_to_execute = next(
                    (item for item in refreshed if item.get("outcome_index") == outcome_index),
                    None,
                )
                if signal_to_execute is None:
                    return None

        outcome = outcomes[outcome_index]
        ask = safe_float(outcome.get("best_ask"))
        if ask is None or not self.fv_edge.accepts_price(ask):
            return None
        signal_ask = safe_float(signal_to_execute.get("current_ask"), ask) or ask
        max_drift = Config.get_float("FV_EDGE_MAX_ASK_DRIFT", "0.01")
        if self.current_mode == "live" and ask > signal_ask + max_drift:
            return None

        quote = {
            "token_id": outcome.get("token_id"),
            "label": outcome.get("label"),
            "outcome_index": outcome.get("index"),
            "best_bid": outcome.get("best_bid"),
            "best_ask": ask,
        }
        result = await self.executor.open_position(
            fresh_market,
            signal_to_execute,
            ask,
            outcome.get("label", f"Outcome {outcome_index}"),
            quote,
        )
        if "成功" not in result:
            return result

        state = self.state_manager.get_state()
        for collection in (state.get("positions", []), state.get("orders", []), state.get("trades", [])):
            if collection and collection[0].get("market_slug") == slug:
                collection[0]["strategy"] = "fv_edge"
                collection[0]["source"] = "fv_edge"
                collection[0]["hold_to_expiry"] = True
        self.state_manager.save()
        _append_position_audit(
            self.POSITION_AUDIT_FILE,
            {
                "t": now_utc.isoformat(),
                "action": "FV_EDGE_OPEN",
                "slug": slug,
                "outcome_index": outcome_index,
                "outcome_label": outcome.get("label"),
                "entry_price": ask,
                "amount": signal_to_execute.get("stake"),
                "fair_up": signal_to_execute.get("fair_up"),
                "edge_bps": signal_to_execute.get("edge_bps"),
                "ref_px": signal_to_execute.get("ref_px"),
                "strategy": "fv_edge",
            },
        )
        self._record_signal_history(signal_to_execute, fresh_market, result, now_utc)
        self._refresh_summary()
        return result

    def _record_signal_history(
        self,
        signal: Dict[str, Any],
        market: Dict[str, Any],
        execution_summary: str,
        now_utc: datetime,
    ) -> None:
        state = self.state_manager.get_state()
        history = state.setdefault("fv_signal_history", [])
        history.insert(
            0,
            {
                "decision_id": f"FV-{now_utc.strftime('%Y%m%d-%H%M%S')}",
                "generated_at": now_utc.isoformat(),
                "action": signal.get("action", "BUY"),
                "prediction": signal.get("outcome_label"),
                "confidence": signal.get("confidence"),
                "model": "fv_edge",
                "reasoning": signal.get("reason"),
                "execution_summary": execution_summary,
                "focus_market": market.get("question"),
                "key_factors": [
                    f"edge: {signal.get('edge_bps')} bps",
                    f"fair_up: {signal.get('fair_up')}",
                    f"mte: {signal.get('mte_minutes')} min",
                ],
                "risk_flags": [],
            },
        )
        state["fv_signal_history"] = history[:50]
        self.state_manager.save()

    def _refresh_summary(self) -> None:
        state = self.state_manager.get_state()
        positions = state.get("positions", [])
        cash = safe_float(state.get("cash_balance"), 0.0) or 0.0
        reserved = sum(safe_float(position.get("stake"), 0.0) or 0.0 for position in positions)
        unrealized = 0.0
        for position in positions:
            shares = safe_float(position.get("shares"), position.get("size")) or 0.0
            bid = safe_float(position.get("current_bid"), position.get("entry_price")) or 0.0
            stake = safe_float(position.get("stake"), 0.0) or 0.0
            unrealized += shares * bid - stake

        closed = [
            trade
            for trade in state.get("trades", [])
            if trade.get("side") == "SELL"
            and str(trade.get("status", "")).upper() in CLOSED_TRADE_STATUSES
        ]
        realized = round(sum(safe_float(trade.get("realized_profit"), 0.0) or 0.0 for trade in closed), 4)
        wins = sum((safe_float(trade.get("realized_profit"), 0.0) or 0.0) > 0 for trade in closed)
        losses = sum((safe_float(trade.get("realized_profit"), 0.0) or 0.0) < 0 for trade in closed)
        total = len(closed)
        start_balance = Config.get_float("PAPER_START_BALANCE", "100")
        ending = round(cash + reserved + unrealized, 4)
        state["stats"] = {
            "total_trades": total,
            "winning_trades": wins,
            "losing_trades": losses,
            "total_profit": realized,
        }
        state["summary"] = {
            "cash_balance": round(cash, 4),
            "reserved_balance": round(reserved, 4),
            "ending_balance": ending,
            "open_positions": len(positions),
            "realized_pnl": realized,
            "unrealized_pnl": round(unrealized, 4),
            "total_trades": total,
            "winning_trades": wins,
            "losing_trades": losses,
            "win_rate": round(wins / total * 100, 2) if total else 0.0,
            "session_started_at": state.get("session_started_at"),
        }
        state["report"] = {
            "strategy": "FV Edge",
            "profit": round(realized + unrealized, 4),
            "roi_percent": round((ending - start_balance) / start_balance * 100, 2)
            if start_balance
            else 0.0,
            "result": "running",
            "session_started_at": state.get("session_started_at"),
        }
        self.state_manager.save()

    @staticmethod
    def _append_jsonl(path: str, event: Dict[str, Any]) -> bool:
        """Best-effort telemetry write; never abort a trading cycle."""
        try:
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            with open(path, "a", encoding="utf-8") as file:
                file.write(json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n")
            return True
        except OSError as exc:
            now = time.monotonic()
            if now - _JSONL_ERROR_LOGGED_AT.get(path, 0.0) >= 30.0:
                logger.error("telemetry append failed path=%s: %s", path, exc)
                _JSONL_ERROR_LOGGED_AT[path] = now
            return False

    @staticmethod
    def _slug_start_dt(slug: str) -> Optional[datetime]:
        try:
            return datetime.fromtimestamp(int(slug.rsplit("-", 1)[-1]), tz=timezone.utc)
        except (TypeError, ValueError, OSError):
            return None

    def _ensure_window_ref(
        self, market: Dict[str, Any], price: float, now_utc: datetime
    ) -> Optional[Dict[str, Any]]:
        slug = str(market.get("slug", ""))
        existing = self._btc_window_refs.get(slug)
        if existing and existing.get("source") == "chainlink" and safe_float(existing.get("ref_px")):
            return existing
        start = self._slug_start_dt(slug)
        if not start or now_utc < start:
            return None
        delay = (now_utc - start).total_seconds()
        max_delay = Config.get_float("FV_EDGE_MAX_REF_DELAY_SECONDS", "10")
        official_ref = safe_float(market.get("chainlink_ref_px"))
        if Config.get_bool("FV_EDGE_REQUIRE_CHAINLINK", "true") and not official_ref:
            return None
        if official_ref:
            ref_px = official_ref
            source = "chainlink"
            late_ref = False
        else:
            if price <= 0:
                return None
            ref_px = price
            source = self._latest_btc.get("source", "btc_spot")
            late_ref = delay > max_delay
        ref = {
            "window_start": start.isoformat(),
            "window_end": market.get("end_date"),
            "ref_px": round(ref_px, 8),
            "captured_at": now_utc.isoformat(),
            "measurement_at": start.isoformat() if source == "chainlink" else now_utc.isoformat(),
            "source": source,
            "late_ref": late_ref,
            "delay_seconds": round(delay, 3),
        }
        self._btc_window_refs[slug] = ref
        self._btc_window_refs = dict(list(self._btc_window_refs.items())[-200:])
        save_json_file(self.BTC_WINDOW_REFS_FILE, self._btc_window_refs)
        return ref

    def _estimate_sigma_from_history(self) -> Optional[float]:
        observations = self._btc_history[-600:]
        if len(observations) < 4:
            return None
        returns: List[Tuple[float, float]] = []
        total_dt = 0.0
        for previous, current in zip(observations, observations[1:]):
            dt = current["ts"] - previous["ts"]
            if not 0 < dt <= 60:
                continue
            returns.append((math.log(current["price"] / previous["price"]), dt))
            total_dt += dt
        if len(returns) < 3 or total_dt < 120:
            return None
        drift = sum(value for value, _ in returns) / total_dt
        variance = sum((value - drift * dt) ** 2 for value, dt in returns) / total_dt
        sigma = math.sqrt(max(variance, 0.0) * FAIR_WINDOW_SEC)
        return sigma if sigma >= FAIR_MIN_SIGMA and math.isfinite(sigma) else None

    def _record_fv_predictions(
        self, markets: List[Dict[str, Any]], btc: Dict[str, Any], now_utc: datetime
    ) -> None:
        price = safe_float(btc.get("price"))
        sigma = safe_float(btc.get("sigma_15m"))
        if not price or not sigma:
            return
        for market in markets:
            ref = self._ensure_window_ref(market, price, now_utc)
            ref_px = safe_float((ref or {}).get("ref_px"))
            if not ref_px:
                continue
            try:
                tau = (iso_to_utc_dt(market.get("end_date", "")) - now_utc).total_seconds()
            except (TypeError, ValueError):
                continue
            if not 0 < tau <= FAIR_WINDOW_SEC + 120:
                continue
            outcomes = market.get("outcomes") or []
            up_ask = safe_float(outcomes[0].get("best_ask")) if len(outcomes) > 0 else None
            down_ask = safe_float(outcomes[1].get("best_ask")) if len(outcomes) > 1 else None
            fair = compute_fair_updown(price, ref_px, sigma, tau, market_price=up_ask)
            fair_up = safe_float(fair.get("fair_up"), 0.5) or 0.5
            self._append_jsonl(
                self.FAIR_VALUE_PREDICTIONS_FILE,
                {
                    "t": now_utc.isoformat(),
                    "slug": market.get("slug"),
                    "minutes_to_end": round(tau / 60, 3),
                    "ref_px": round(ref_px, 2),
                    "s_now": round(price, 2),
                    "sigma_15m": round(sigma, 6),
                    "fair_up": round(fair_up, 4),
                    "fair_down": round(1 - fair_up, 4),
                    "market_up_ask": up_ask,
                    "market_down_ask": down_ask,
                    "market_up_bid": safe_float(outcomes[0].get("best_bid")) if len(outcomes) > 0 else None,
                    "market_down_bid": safe_float(outcomes[1].get("best_bid")) if len(outcomes) > 1 else None,
                    "quote_source_up": outcomes[0].get("quote_source") if len(outcomes) > 0 else None,
                    "quote_source_down": outcomes[1].get("quote_source") if len(outcomes) > 1 else None,
                    "book_observed_at": market.get("book_observed_at"),
                    "book_fetch_latency_ms": market.get("book_fetch_latency_ms"),
                    "btc_source": btc.get("source"),
                    "btc_captured_at": btc.get("captured_at"),
                    "btc_fetched_at": btc.get("fetched_at"),
                    "btc_cache_age_secs": btc.get("cache_age_secs"),
                    "ref_source": (ref or {}).get("source"),
                    "ref_measurement_at": (ref or {}).get("measurement_at"),
                    "edge_up_bps": round((fair_up - up_ask) * 10000, 2) if up_ask else None,
                    "edge_down_bps": round(((1 - fair_up) - down_ask) * 10000, 2) if down_ask else None,
                    "late_ref": bool((ref or {}).get("late_ref")),
                },
            )

    async def _btc_monitor_loop(self) -> None:
        price_interval = max(1.0, Config.get_float("BTC_PRICE_POLL_SECONDS", "2"))
        signal_interval = max(30.0, Config.get_float("BTC_SIGNAL_POLL_SECONDS", "60"))
        last_signal_at = 0.0
        signal_context: Dict[str, Any] = {}
        while self.running:
            started = time.monotonic()
            try:
                now_utc = datetime.now(timezone.utc)
                if started - last_signal_at >= signal_interval:
                    context = await self.btc_api.get_signal_context()
                    if context:
                        signal_context = dict(context)
                        last_signal_at = started
                spot = await self.btc_api.get_price()
                if spot and safe_float(spot.get("price")):
                    btc = {**signal_context, **spot}
                    measurement_at = spot.get("captured_at") or spot.get("measurement_at")
                    if not measurement_at:
                        measurement_at = now_utc.isoformat()
                    btc["captured_at"] = measurement_at
                    btc["fetched_at"] = now_utc.isoformat()
                    try:
                        btc["cache_age_secs"] = max(
                            0.0, (now_utc - iso_to_utc_dt(measurement_at)).total_seconds()
                        )
                    except (TypeError, ValueError):
                        btc["cache_age_secs"] = 9999.0
                    price = float(btc["price"])
                    history_ts = iso_to_utc_dt(measurement_at).timestamp()
                    self._btc_history.append({"ts": history_ts, "price": price})
                    self._btc_history = self._btc_history[-600:]
                    sigma = self._estimate_sigma_from_history() or safe_float(
                        signal_context.get("sigma_15m")
                    )
                    if sigma and sigma >= FAIR_MIN_SIGMA:
                        btc["sigma_15m"] = round(sigma, 6)
                    else:
                        btc.pop("sigma_15m", None)
                    self._latest_btc = btc
                    for market in self._latest_markets:
                        self._ensure_window_ref(market, price, now_utc)
                    self._append_jsonl(
                        self.BTC_TICKS_FILE,
                        {
                            "t": now_utc.isoformat(),
                            "measurement_at": measurement_at,
                            "price": round(price, 2),
                            "source": btc.get("source"),
                        },
                    )
                    save_json_file(self.BTC_SNAPSHOT_FILE, btc)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.debug("BTC monitor failed: %s", exc)
            elapsed = time.monotonic() - started
            await asyncio.sleep(max(0.1, price_interval - elapsed))

    async def run_cycle(self) -> None:
        with _SaveBatchContext(self.state_manager):
            await self._run_cycle()

    async def _run_cycle(self) -> None:
        await self.check_mode_swap()
        now_utc = datetime.now(timezone.utc)
        control = load_trading_control(CONTROL_FILE)
        trading_enabled = bool(control.get("trading_enabled", False))

        markets = await self._get_markets(now_utc)
        self._latest_markets = markets
        if markets:
            await self._refresh_books(markets)
        messages = await self._manage_positions(now_utc, markets)

        signals: List[Dict[str, Any]] = []
        btc = dict(self._latest_btc)
        captured_at = btc.get("captured_at")
        if captured_at:
            try:
                btc["cache_age_secs"] = max(
                    0, int((now_utc - iso_to_utc_dt(captured_at)).total_seconds())
                )
            except (TypeError, ValueError):
                btc["cache_age_secs"] = 9999
        max_btc_age = Config.get_int("FV_EDGE_MAX_BTC_AGE_SECONDS", "3")
        if trading_enabled and btc and btc.get("cache_age_secs", 9999) <= max_btc_age:
            self.fv_edge.update_btc_snapshot(btc, window_refs=self._btc_window_refs)
            signals = self.fv_edge.scan(markets, now_utc)
            by_slug = {market.get("slug"): market for market in markets}
            for signal in signals:
                market = by_slug.get(signal.get("slug"))
                if not market:
                    continue
                result = await self._open_signal(signal, market, now_utc)
                if result:
                    messages.append(result)

        self._record_fv_predictions(markets, btc, now_utc)
        # Persist position/trade state before publishing the status snapshot.
        self.state_manager.flush()
        focus = min(
            markets,
            key=lambda market: market.get("end_date") or "9999",
            default={},
        )
        StatusExporter.export(
            {
                "running": True,
                "last_update": now_utc.isoformat(),
                "trading_mode": self.current_mode,
                "trading_enabled": trading_enabled,
                "strategy_profile": "fv_edge",
                "market_slug": focus.get("slug", ""),
                "market_question": focus.get("question", ""),
                "market_end_date": focus.get("end_date", ""),
                "market_outcomes": [
                    {
                        "index": outcome.get("index"),
                        "label": outcome.get("label"),
                        "price": outcome.get("price"),
                        "best_bid": outcome.get("best_bid"),
                        "best_ask": outcome.get("best_ask"),
                    }
                    for outcome in focus.get("outcomes", [])
                ],
                "execution_summary": "；".join(messages) if messages else "FV Edge 监控中",
                "fv_edge": self.fv_edge.diagnostics(),
                "fv_signals": signals,
            }
        )

    async def start(self) -> None:
        logger.info("FV Edge 交易机器人启动，模式=%s", self.current_mode)
        btc_task = asyncio.create_task(self._btc_monitor_loop(), name="fv-edge-btc")
        cycle_seconds = max(1.0, Config.get_float("FV_CYCLE_SECONDS", "2"))
        try:
            while self.running:
                started = time.monotonic()
                try:
                    await self.run_cycle()
                except Exception as exc:
                    logger.exception("FV Edge cycle failed: %s", exc)
                await asyncio.sleep(max(0.1, cycle_seconds - (time.monotonic() - started)))
        finally:
            btc_task.cancel()
            try:
                await btc_task
            except asyncio.CancelledError:
                pass
