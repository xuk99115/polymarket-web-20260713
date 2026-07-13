import aiohttp
import asyncio
import logging
import math
import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger("market_api")

# 关键: aiohttp 原生不支持 socks5 proxy, 必须 import aiohttp_socks 才能让
# proxy="socks5://..." 工作 (不然 aiohttp 默默失败, 不报错也不工作)
try:
    import aiohttp_socks  # noqa: F401
    _HAS_AIOHTTP_SOCKS = True
except ImportError:
    _HAS_AIOHTTP_SOCKS = False
    logger.warning("⚠️ aiohttp_socks 没装, SOCKS5 代理不会生效")

from ..core.config import Config
from ..core.utils import extract_market_slug, parse_json_list, safe_float
from .fair_value import compute_fair_updown, DEFAULT_WINDOW_SEC as _FAIR_WINDOW_SEC, MIN_SIGMA as _FAIR_MIN_SIGMA
from .polymarket_ws import PolymarketWSClient, TOP_OF_BOOK_DEPTH

# Legacy BTC 15m scanner support
BTC_15M_SLUG_PREFIX = "btc-updown-15m-"
BTC_5M_SLUG_PREFIX = "btc-updown-5m-"
WINDOW_SECONDS = 900  # 15 minutes
DEFAULT_REASONABLE_SPREAD = 0.25
# Binance SSL: 新加坡 VPS 网络存在 HTTPS 劫持 (透明代理),
# 走 SOCKS5 时 vless 节点出口是 CloudFront CDN, 证书不匹配 Binance,
# 因此仅对 Binance API 调用跳过证书验证.
# Bug fix 2026-06-27: 之前用模块级 _BINANCE_SSL_CTX 给所有 aiohttp session 用, 导致
# Polymarket Gamma / CLOB / CoinGecko 全部失去 SSL 验证, 真安全漏洞. 改成只给 Binance 用.
# 警告: 这会使 Binance HTTPS 暴露于中间人, 但 Polymarket/CoinGecko 仍使用正常验证.
import ssl as _ssl
import logging as _logging
_BINANCE_SSL_CTX = _ssl.create_default_context()
_BINANCE_SSL_CTX.check_hostname = False
_BINANCE_SSL_CTX.verify_mode = _ssl.CERT_NONE
_logging.getLogger("trading_manager").warning(
    "🔓 Binance SSL 证书检查已禁用 (仅限 Binance API 调用, 因 VPS 网络 HTTPS 劫持)"
)
_DEFAULT_SSL_CTX = _ssl.create_default_context()  # 默认验证, Polymarket/Gamma/CoinGecko 用这个


def _complement_price(value: Optional[float]) -> Optional[float]:
    if value is None:
        return None
    return round(max(0.0, min(1.0, 1 - float(value))), 4)


def _quote_is_reasonable(
    bid: Optional[float],
    ask: Optional[float],
    reference_price: Optional[float] = None,
) -> bool:
    bid = safe_float(bid)
    ask = safe_float(ask)
    reference_price = safe_float(reference_price)
    if bid is None or ask is None:
        return False
    if bid <= 0 or ask <= 0 or bid >= ask or ask >= 1:
        return False
    spread = ask - bid
    if spread > DEFAULT_REASONABLE_SPREAD:
        return False
    if reference_price is not None:
        midpoint = (bid + ask) / 2
        if abs(midpoint - reference_price) > 0.2 and spread > 0.12:
            return False
    return True


class PolymarketClient:
    """Polymarket public market data wrapper."""

    def __init__(self):
        self.BASE_URL = "https://gamma-api.polymarket.com"
        self.headers = {"Accept": "application/json"}

    def _current_window_slugs(
        self,
        now_utc: datetime,
        lookahead: int = 3,
        *,
        prefix: str = BTC_15M_SLUG_PREFIX,
        window_seconds: int = WINDOW_SECONDS,
    ) -> List[str]:
        """
        生成当前及未来 BTC 15m 盘口 slug。
        
        修复（2026-06-23）：
        原来用 ceil 只生成未来盘口，漏掉了当前已开盘的窗口。
        现在同时包含当前窗口（floor）和未来的 lookahead 个窗口。
        """
        ts = int(now_utc.timestamp())
        # floor → 当前已开盘的窗口（或刚结束的窗口）
        current_base = (ts // window_seconds) * window_seconds
        # ceil → 下一个即将开盘的窗口
        next_base = math.ceil(ts / window_seconds) * window_seconds
        
        slugs = []
        # 加入当前窗口（如果还没结束）
        current_end = current_base + window_seconds
        if current_end > ts:
            slugs.append(f"{prefix}{current_base}")
        # 加入未来窗口
        start_base = next_base if next_base != current_base else next_base + window_seconds
        for i in range(lookahead):
            slug = f"{prefix}{start_base + i * window_seconds}"
            if slug not in slugs:
                slugs.append(slug)
        
        return slugs

    def _score_btc_snapshot(self, market: Dict[str, Any], now_utc: datetime) -> float:
        """
        评分逻辑（修复版 2026-06-23）：
        核心原则：优先选"已经开盘、有真实 CLOB 深度、真实流动性"的盘口。
        
        问题修复：
        - 原来"距离到期越远分越高" → 导致未开盘的盘口得分最高（错误）
        - 现在改为"窗口期评分"：3-10 分钟最佳，太近(<3)或太远(>15)都扣分
        - gamma 预估价（无真实 CLOB）→ 大幅惩罚
        - 真实流动性权重加大
        """
        score = 0.0
        outcomes = market.get("outcomes") or []
        primary = outcomes[0] if outcomes else {}

        # 1. 盘口合理性 (±10) — 基础门槛
        if _quote_is_reasonable(primary.get("best_bid"), primary.get("best_ask"), primary.get("price")):
            score += 10.0
        else:
            score -= 10.0

        # 2. CLOB 深度检查 — 区分"真实交易"vs"gamma 预估"
        # quote_source 在 manager._merge_book_quotes() 中设置:
        #   "clob" = 有真实 CLOB 深度, "gamma" = 仅 Gamma 预估价（无真实流动性）
        quote_source = primary.get("quote_source", "gamma")
        if quote_source == "clob":
            score += 15.0  # 有真实 CLOB 深度 → 大幅加分
        elif quote_source == "gamma":
            score -= 20.0  # 仅 Gamma 预估 → 大幅惩罚（未开盘或深度不足）

        # 3. 到期时间 → "窗口期"评分（非距离越远越好）
        #    最佳窗口: 3-10 分钟（有足够时间入场，又不仓促）
        #    太远 >15 分钟: 未开盘，流动性假，扣分
        #    太近 <3 分钟: 时间不足，扣分
        end_date = market.get("end_date")
        if end_date:
            try:
                minutes_to_expiry = (datetime.fromisoformat(end_date.replace("Z", "+00:00")) - now_utc).total_seconds() / 60
                min_minutes = Config.get_int("PAPER_MIN_MINUTES_TO_EXPIRY", "3")

                if minutes_to_expiry < min_minutes:
                    # 快到期了，不交易
                    score -= 25.0
                elif minutes_to_expiry <= 10:
                    # 最佳窗口: 3-10 分钟
                    score += 12.0
                elif minutes_to_expiry <= 15:
                    # 可接受: 10-15 分钟（已开盘但窗口偏长）
                    score += 6.0
                else:
                    # 太远 >15 分钟: 很可能未开盘，流动性是假的
                    score -= 10.0
            except Exception:
                pass

        # 4. 流动性评分 — 但只算"真实流动性"
        #    gamma 市场的 liquidity 是预估的，不是真实的，所以只给 20% 权重
        liquidity = safe_float(market.get("liquidity"), 0.0) or 0.0
        if quote_source == "clob":
            # 真实 CLOB 市场 → 全权重
            score += min(liquidity, 5000.0) / 1000.0  # 最高 +5
        else:
            # Gamma 预估市场 → 20% 权重（防止假流动性误导）
            score += min(liquidity, 5000.0) / 5000.0  # 最高 +1

        return score

    def _select_btc_snapshot(self, snapshots: List[Dict[str, Any]], now_utc: datetime) -> Optional[Dict[str, Any]]:
        if not snapshots:
            return None
        # 排序时缓存得分, 避免日志重复计算
        scored = [(self._score_btc_snapshot(item, now_utc), item) for item in snapshots]
        scored.sort(key=lambda pair: pair[0], reverse=True)
        top_score, selected = scored[0]
        logger.info("BTC 15m 选盘结果: %s (score=%.2f)", selected.get("slug"), top_score)
        return selected

    def _build_outcomes(self, raw_market: Dict[str, Any]) -> List[Dict[str, Any]]:
        token_ids = parse_json_list(raw_market.get("clobTokenIds"))
        labels = parse_json_list(raw_market.get("outcomes"))
        prices_raw = parse_json_list(raw_market.get("outcomePrices"))

        outcomes: List[Dict[str, Any]] = []
        for index, token_id in enumerate(token_ids):
            label = str(labels[index]) if index < len(labels) else f"Outcome {index + 1}"
            price = safe_float(prices_raw[index] if index < len(prices_raw) else None)
            outcomes.append({
                "index": index,
                "label": label,
                "token_id": token_id,
                "price": price,
                "best_bid": None,
                "best_ask": None,
            })

        market_best_bid = safe_float(raw_market.get("bestBid"))
        market_best_ask = safe_float(raw_market.get("bestAsk"))
        if outcomes:
            outcomes[0]["best_bid"] = market_best_bid
            outcomes[0]["best_ask"] = market_best_ask
        if len(outcomes) == 2:
            outcomes[1]["best_bid"] = _complement_price(market_best_ask)
            outcomes[1]["best_ask"] = _complement_price(market_best_bid)

        return outcomes

    def _normalize_market(
        self,
        raw_market: Dict[str, Any],
        *,
        slug_hint: str = "",
        question: Optional[str] = None,
        end_date: Optional[str] = None,
        active: Optional[bool] = None,
        closed: Optional[bool] = None,
        liquidity: Optional[float] = None,
        neg_risk: Optional[bool] = None,
    ) -> Optional[Dict[str, Any]]:
        outcomes = self._build_outcomes(raw_market)
        if not outcomes:
            return None

        token_ids = [item["token_id"] for item in outcomes]
        prices = [item["price"] for item in outcomes]
        slug = str(raw_market.get("slug") or slug_hint or "").strip()
        is_binary = len(outcomes) == 2

        return {
            "slug": slug,
            "question": question or raw_market.get("question") or raw_market.get("title") or raw_market.get("name") or "",
            "end_date": end_date or raw_market.get("endDate"),
            "active": bool(raw_market.get("active", active if active is not None else True)),
            "closed": bool(raw_market.get("closed", closed if closed is not None else False)),
            "liquidity": safe_float(raw_market.get("liquidity"), liquidity if liquidity is not None else 0.0) or 0.0,
            "outcomes": outcomes,
            "outcome_count": len(outcomes),
            "binary": is_binary,
            "prices": prices,
            "token_ids": token_ids,
            "up_token_id": token_ids[0] if len(token_ids) > 0 else None,
            "down_token_id": token_ids[1] if len(token_ids) > 1 else None,
            "best_bid": outcomes[0]["best_bid"] if outcomes else None,
            "best_ask": outcomes[0]["best_ask"] if outcomes else None,
            "neg_risk": bool(raw_market.get("negRisk", neg_risk if neg_risk is not None else False)),
            "tick_size": str(raw_market.get("orderPriceMinTickSize", "0.01")),
            "accepting_orders": raw_market.get("acceptingOrders", True),
            # 2026-07-11 Bug fix: _resolve_settlement_price 依赖 market["outcomePrices"]
            # 才能在窗口过期时正确读到 0/1 settlement 价. 之前 _build_outcomes 把
            # outcomePrices 解析进了 outcomes[].price 但顶层 dict 漏了, 导致
            # _should_close_position 拿到的 settlement 永远是 0.0 (按 0 兜底),
            # fv_edge 末段入场永远按全亏结算. 这里从 raw_market 重新解析, 保证类型一致.
            "outcomePrices": parse_json_list(raw_market.get("outcomePrices")),
        }

    def _pick_market_from_event(self, event: Dict[str, Any], slug_hint: str) -> Optional[Dict[str, Any]]:
        markets = [market for market in (event.get("markets") or []) if parse_json_list(market.get("clobTokenIds"))]
        if not markets:
            return None

        for market in markets:
            if str(market.get("slug") or "").strip() == slug_hint:
                return market

        if len(markets) == 1:
            return markets[0]

        binary_markets = [market for market in markets if len(parse_json_list(market.get("clobTokenIds"))) == 2]
        if len(binary_markets) == 1:
            return binary_markets[0]

        logger.warning("事件 %s 包含多个可交易市场，请直接使用具体 market slug", slug_hint)
        return None

    async def _fetch_market_by_market_slug(
        self,
        session: aiohttp.ClientSession,
        slug: str,
    ) -> Optional[Dict[str, Any]]:
        url = f"{self.BASE_URL}/markets/slug/{slug}"
        try:
            async with session.get(url, headers=self.headers, timeout=aiohttp.ClientTimeout(total=8)) as resp:
                if resp.status != 200:
                    return None
                raw_market = await resp.json()
                if not raw_market or raw_market.get("error"):
                    return None
                return self._normalize_market(raw_market, slug_hint=slug)
        except Exception as exc:
            logger.debug("fetch_market_by_slug %s: %s", slug, exc)
            return None

    async def _fetch_event_by_slug(
        self,
        session: aiohttp.ClientSession,
        slug: str,
    ) -> Optional[Dict[str, Any]]:
        url = f"{self.BASE_URL}/events?slug={slug}"
        try:
            async with session.get(url, headers=self.headers, timeout=aiohttp.ClientTimeout(total=8)) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json()
                if not data:
                    return None
                event = data[0]
                if event.get("closed") or not event.get("active"):
                    return None
                market = self._pick_market_from_event(event, slug)
                if not market:
                    return None
                return self._normalize_market(
                    market,
                    slug_hint=str(market.get("slug") or slug),
                    question=event.get("title"),
                    end_date=event.get("endDate"),
                    active=event.get("active"),
                    closed=event.get("closed"),
                    liquidity=safe_float(event.get("liquidity"), 0.0),
                    neg_risk=event.get("negRisk", False),
                )
        except Exception as exc:
            logger.debug("fetch_event_by_slug %s: %s", slug, exc)
            return None

    async def get_market(self, market_input: str) -> Optional[Dict[str, Any]]:
        """Resolve a target market from a slug or Polymarket URL."""
        slug = extract_market_slug(market_input)
        if not slug:
            logger.warning("get_market: empty slug from input '%s'", market_input)
            return None

        async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=_DEFAULT_SSL_CTX)) as session:
            market = await self._fetch_market_by_market_slug(session, slug)
            if market:
                logger.info("get_market: found via markets/slug: %s", market.get('slug'))
                return market
            logger.debug("get_market: markets/slug failed, trying events")
            result = await self._fetch_event_by_slug(session, slug)
            if result:
                logger.info("get_market: found via events: %s", result.get('slug'))
            else:
                logger.warning("get_market: both endpoints returned no data for slug '%s'", slug)
            return result

    async def get_focus_market(self, now_utc: Optional[datetime] = None) -> Optional[Dict[str, Any]]:
        """Return the configured target market, or legacy BTC 15m discovery if requested."""
        target_input = (
            Config.get("TARGET_MARKET_URL", "")
            or Config.get("TARGET_MARKET_SLUG", "")
            or Config.get("BTC_UPDOWN_MARKET_ID", "")
        )
        if target_input:
            market = await self.get_market(target_input)
            if market:
                return market
            logger.warning("未能解析目标市场: %s", target_input)

        selection_mode = Config.get("MARKET_SELECTION_MODE", "manual").strip().lower()
        if selection_mode in {"auto_btc_15m", "auto_btc_5m"}:
            current_time = now_utc or datetime.now(timezone.utc)
            snapshots = await self.get_market_snapshots(current_time)
            selected = self._select_btc_snapshot(snapshots, current_time)
            if selected:
                return selected
            logger.warning("%s 已启用，但未找到可交易的 BTC 滚动盘口", selection_mode)
            return None
        return None

    async def get_market_snapshots(self, now_utc: datetime) -> List[Dict[str, Any]]:
        """BTC rolling market discovery.

        Defaults to the existing BTC 15m preset. When MARKET_SELECTION_MODE is
        auto_btc_5m, use the 5m slug cadence needed by the hedged-limit system.
        """
        selection_mode = Config.get("MARKET_SELECTION_MODE", "manual").strip().lower()
        if selection_mode == "auto_btc_5m":
            prefix = Config.get("BTC_5M_SLUG_PREFIX", BTC_5M_SLUG_PREFIX)
            window_seconds = Config.get_int("BTC_5M_WINDOW_SECONDS", "300")
        else:
            prefix = BTC_15M_SLUG_PREFIX
            window_seconds = WINDOW_SECONDS
        slugs = self._current_window_slugs(
            now_utc,
            lookahead=4,
            prefix=prefix,
            window_seconds=window_seconds,
        )
        results = []
        async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=_DEFAULT_SSL_CTX)) as session:
            tasks = [self._fetch_event_by_slug(session, slug) for slug in slugs]
            for snapshot in await asyncio.gather(*tasks):
                if snapshot:
                    results.append(snapshot)
        if results:
            logger.info("找到 %s 个活跃 BTC 滚动盘口: %s", len(results), [item["slug"] for item in results])
        else:
            # Bug fix 2026-06-27: warning → debug. 周末 / 节假日 BTC 15m 经常停盘,
            # 这是常态 (cron 每小时 tick 会刷), 不该每分钟打 warning 刷屏.
            logger.debug("未找到活跃的 BTC 15m 盘口（市场可能尚未开放或已全部关闭）")
        return results

    async def get_order_book(
        self,
        token_id: str,
        *,
        session: Optional[aiohttp.ClientSession] = None,
    ) -> Optional[Dict[str, Any]]:
        """Fetch CLOB order book for a token.

        2026-06-24 优化: timeout 5s → 2s, 加快失败快速 fallback.
        实测韩国网络下 6s 单请求, 5s timeout 让 cycle 拖到 35s,
        2s timeout + 并发 10 个 token 也只 2s (asyncio.gather).
        """
        if not token_id:
            return None

        url = f"https://clob.polymarket.com/book?token_id={token_id}"

        async def _request(client: aiohttp.ClientSession) -> Optional[Dict[str, Any]]:
            try:
                async with client.get(url, timeout=aiohttp.ClientTimeout(total=2)) as resp:
                    if resp.status == 200:
                        return await resp.json()
            except Exception as exc:
                logger.debug("order_book %s: %s", token_id, exc)
            return None

        if session is not None:
            return await _request(session)

        async with aiohttp.ClientSession() as client:
            return await _request(client)

    async def get_microstructure(self, market: Dict[str, Any]) -> Dict[str, Any]:
        """Fetch top-of-book data for each outcome in the target market.

        Dispatches to the WebSocket feed when ``USE_POLYMARKET_WS`` is
        truthy (default ``true``). Falls back to REST polling if the
        WS client fails to produce a snapshot within ``1`` second or
        raises any exception.

        ``USE_POLYMARKET_WS=false`` forces the REST path.

        2026-06-24 优化: timeout 从 2s 降到 1s — WS 在韩国网络下基本连不上,
        2s timeout 让每个 microstructure 拖到 8-10s, 拖慢整个 cycle.
        """
        use_ws = Config.get_bool("USE_POLYMARKET_WS", "true")
        if use_ws:
            try:
                result = await self.get_microstructure_ws(market, timeout=1.0)
                if result.get("outcomes"):
                    return result
                logger.debug("WS microstructure returned empty, falling back to REST")
            except Exception as exc:
                logger.debug("WS microstructure skipped (%s), REST fallback", exc)
        return await self._get_microstructure_rest(market)

    async def _get_microstructure_rest(self, market: Dict[str, Any]) -> Dict[str, Any]:
        """Original REST implementation - kept as fallback."""
        outcomes = market.get("outcomes") or []
        if not outcomes:
            return {"outcomes": [], "source": "none"}

        async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=_DEFAULT_SSL_CTX)) as session:
            tasks = [self.get_order_book(outcome.get("token_id"), session=session) for outcome in outcomes]
            books = await asyncio.gather(*tasks)

        result = []
        for outcome, book in zip(outcomes, books):
            if not book:
                continue
            result.append({
                "index": outcome.get("index"),
                "label": outcome.get("label"),
                "token_id": outcome.get("token_id"),
                "bids": (book.get("bids") or [])[:5],
                "asks": (book.get("asks") or [])[:5],
            })
        return {"outcomes": result, "source": "clob" if result else "none"}

    @property
    def _ws_client(self) -> PolymarketWSClient:
        """Lazy accessor for the singleton WS client.

        Tests can monkey-patch this property to inject a fake.
        """
        # `PolymarketWSClient.instance()` is kept as a class-level compatibility
        # shim so tests and callers can monkey-patch it. Do not import the module
        # function directly here, or class-level patches are bypassed.
        return PolymarketWSClient.instance()

    async def get_microstructure_ws(
        self,
        market: Dict[str, Any],
        *,
        timeout: float = 2.0,
    ) -> Dict[str, Any]:
        """Top-of-book via the WebSocket feed.

        Subscribes the market's token_ids (idempotent - safe to call every
        tick) and waits up to ``timeout`` seconds for the first snapshot
        to land. Returns the same ``{"outcomes": [...], "source": "..."}``
        shape as the REST path; ``source`` is ``"ws"`` on success.
        """
        outcomes = market.get("outcomes") or []
        if not outcomes:
            return {"outcomes": [], "source": "none"}

        token_ids = [o.get("token_id") for o in outcomes if o.get("token_id")]
        if not token_ids:
            return {"outcomes": [], "source": "none"}

        ws = self._ws_client
        try:
            await ws.connect(token_ids)
        except Exception as exc:
            logger.warning("WS connect failed: %s", exc)
            return {"outcomes": [], "source": "ws_failed"}

        result = []
        for outcome in outcomes:
            token_id = outcome.get("token_id")
            if not token_id:
                continue
            snap = await ws.wait_for_book(token_id, timeout=timeout)
            if not snap:
                continue
            # Convert (price, size) tuples back to the {"price","size"} dict
            # shape the rest of the codebase expects (mirrors REST output).
            bids = [{"price": str(p), "size": str(s)} for (p, s) in (snap.get("bids") or [])[:TOP_OF_BOOK_DEPTH]]
            asks = [{"price": str(p), "size": str(s)} for (p, s) in (snap.get("asks") or [])[:TOP_OF_BOOK_DEPTH]]
            result.append({
                "index": outcome.get("index"),
                "label": outcome.get("label"),
                "token_id": token_id,
                "bids": bids,
                "asks": asks,
            })
        return {"outcomes": result, "source": "ws" if result else "none"}


class BTCDataprovider:
    """Optional BTC price feed kept for dashboard/reference use."""

    async def get_price(self) -> Optional[Dict[str, Any]]:
        source = Config.get("BTC_PRICE_SOURCE", "binance")
        if source == "binance":
            return await self._get_binance_price()
        return await self._get_coingecko_price()

    async def get_signal_context(self, market_end_utc=None) -> Optional[Dict[str, Any]]:
        source = Config.get("BTC_PRICE_SOURCE", "binance")
        if source == "binance":
            ctx = await self._get_binance_signal_context()
        else:
            ctx = await self.get_price()

        # 增量集成: Kronos 预测已禁用 (2026-06-25)
        if ctx and source == "binance":
            pass

        return ctx

    # 代理池: 支持多个 SOCKS5 端口轮询, 失败剔除
    # .env 配置: BINANCE_PROXY_URLS=socks5://127.0.0.1:10808,socks5://127.0.0.1:10908,...
    # 单个 URL 也能用 (兼容老配置)
    _PROXY_POOL: List[str] = []
    _PROXY_POOL_IDX: int = 0
    _PROXY_FAIL_COUNT: Dict[str, int] = {}  # 失败次数统计
    _PROXY_BACKOFF_UNTIL: Dict[str, float] = {}  # 失败后退避时间戳

    def _get_proxy_pool(self) -> List[str]:
        """从 env 读取代理池, 支持 BINANCE_PROXY_URLS (多个) 和 BINANCE_PROXY_URL (单个)"""
        if self._PROXY_POOL:
            return self._PROXY_POOL
        urls_str = Config.get("BINANCE_PROXY_URLS", "")
        if urls_str:
            urls = [u.strip() for u in urls_str.split(",") if u.strip()]
        else:
            single = Config.get("BINANCE_PROXY_URL", "")
            urls = [single] if single else []
        self._PROXY_POOL = urls
        return urls

    def _pick_proxy(self) -> Optional[str]:
        """轮询选一个可用代理, 跳过失败退避中的"""
        import time as _t
        pool = self._get_proxy_pool()
        if not pool:
            return None
        now = _t.time()
        n = len(pool)
        for offset in range(n):
            idx = (self._PROXY_POOL_IDX + offset) % n
            url = pool[idx]
            backoff = self._PROXY_BACKOFF_UNTIL.get(url, 0)
            if backoff > now:
                continue
            self._PROXY_POOL_IDX = (idx + 1) % n
            return url
        # 全部退避中, 用第一个
        return pool[0]

    def _mark_proxy_failed(self, url: str) -> None:
        """标记代理失败, 退避 30s"""
        import time as _t
        if not url:
            return
        self._PROXY_FAIL_COUNT[url] = self._PROXY_FAIL_COUNT.get(url, 0) + 1
        # 失败次数越多退避越长: 30s, 60s, 120s
        fail_n = self._PROXY_FAIL_COUNT[url]
        backoff = min(30 * (2 ** (fail_n - 1)), 300)
        self._PROXY_BACKOFF_UNTIL[url] = _t.time() + backoff
        logger.warning("🚫 [Proxy Pool] %s 失败 %d 次, 退避 %ds", url, fail_n, backoff)

    def _mark_proxy_ok(self, url: str) -> None:
        """标记代理成功, 清零失败计数"""
        if not url:
            return
        if self._PROXY_FAIL_COUNT.get(url, 0) > 0:
            logger.info("✅ [Proxy Pool] %s 恢复可用", url)
        self._PROXY_FAIL_COUNT[url] = 0
        self._PROXY_BACKOFF_UNTIL[url] = 0

    async def _get_binance_price(self) -> Optional[Dict[str, Any]]:
        url = "https://api.binance.com/api/v3/ticker/24hr?symbol=BTCUSDT"
        timeout = aiohttp.ClientTimeout(total=5)
        pool = self._get_proxy_pool()
        if not pool:
            # 没配置代理, 直连
            return await self._fetch_binance_price_direct(url, timeout)
        # 轮询代理池
        tried = set()
        for _ in range(len(pool)):
            proxy_url = self._pick_proxy()
            if not proxy_url or proxy_url in tried:
                break
            tried.add(proxy_url)
            result = await self._fetch_binance_price_direct(url, timeout, proxy_url)
            if result is not None:
                self._mark_proxy_ok(proxy_url)
                return result
            self._mark_proxy_failed(proxy_url)
        # 全部失败
        return None

    async def _fetch_binance_price_direct(self, url, timeout, proxy_url=None) -> Optional[Dict[str, Any]]:
        # 走 SOCKS5 时 vless 节点出口是 CloudFront CDN, 证书是 CDN 的不是 Binance 的
        # 必须关 SSL 验证, 不然 SSLCertVerificationError
        import ssl as _ssl
        ssl_ctx = _ssl.create_default_context()
        ssl_ctx.check_hostname = False
        ssl_ctx.verify_mode = _ssl.CERT_NONE
        connector = aiohttp.TCPConnector(ssl=ssl_ctx)
        async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
            try:
                if proxy_url:
                    async with session.get(url, proxy=proxy_url) as resp:
                        if resp.status == 200:
                            data = await resp.json()
                            return {
                                "price": float(data["lastPrice"]),
                                "change_24h": float(data["priceChangePercent"]),
                                "source": "binance",
                            }
                else:
                    async with session.get(url) as resp:
                        if resp.status == 200:
                            data = await resp.json()
                            return {
                                "price": float(data["lastPrice"]),
                                "change_24h": float(data["priceChangePercent"]),
                                "source": "binance",
                            }
            except Exception as exc:
                logger.error("Binance API error: %s", exc)
        return None

    async def _get_binance_signal_context(self) -> Optional[Dict[str, Any]]:
        ticker_url = "https://api.binance.com/api/v3/ticker/24hr?symbol=BTCUSDT"
        klines_url = "https://api.binance.com/api/v3/klines?symbol=BTCUSDT&interval=1m&limit=20"
        timeout = aiohttp.ClientTimeout(total=5)
        pool = self._get_proxy_pool()
        if not pool:
            return await self._fetch_binance_signal_direct(ticker_url, klines_url, timeout)

        tried = set()
        for _ in range(len(pool)):
            proxy_url = self._pick_proxy()
            if not proxy_url or proxy_url in tried:
                break
            tried.add(proxy_url)
            result = await self._fetch_binance_signal_direct(ticker_url, klines_url, timeout, proxy_url)
            if result is not None:
                self._mark_proxy_ok(proxy_url)
                return result
            self._mark_proxy_failed(proxy_url)
        return None

    async def _fetch_binance_signal_direct(self, ticker_url, klines_url, timeout, proxy_url=None) -> Optional[Dict[str, Any]]:
        # 走 SOCKS5 时 vless 节点出口是 CloudFront CDN, 证书是 CDN 的不是 Binance 的
        import ssl as _ssl
        ssl_ctx = _ssl.create_default_context()
        ssl_ctx.check_hostname = False
        ssl_ctx.verify_mode = _ssl.CERT_NONE
        connector = aiohttp.TCPConnector(ssl=ssl_ctx)
        # 拆 headers / proxy 分开传, 避免 kwargs 类型推断混乱
        async with aiohttp.ClientSession(connector=connector, timeout=timeout, headers={"User-Agent": "Mozilla/5.0"}) as session:
            try:
                if proxy_url:
                    async with session.get(ticker_url, proxy=proxy_url) as ticker_resp:
                        if ticker_resp.status != 200:
                            return None
                        ticker = await ticker_resp.json()
                    async with session.get(klines_url, proxy=proxy_url) as klines_resp:
                        if klines_resp.status != 200:
                            return None
                        klines = await klines_resp.json()
                else:
                    async with session.get(ticker_url) as ticker_resp:
                        if ticker_resp.status != 200:
                            return None
                        ticker = await ticker_resp.json()
                    async with session.get(klines_url) as klines_resp:
                        if klines_resp.status != 200:
                            return None
                        klines = await klines_resp.json()
            except Exception as exc:
                logger.error("Binance signal API error: %s", exc)
                return None

        closes = [safe_float(item[4]) for item in klines if len(item) > 5]
        highs = [safe_float(item[2]) for item in klines if len(item) > 5]
        lows = [safe_float(item[3]) for item in klines if len(item) > 5]
        volumes = [safe_float(item[5]) for item in klines if len(item) > 5]
        if len(closes) < 16:
            return {
                "price": float(ticker["lastPrice"]),
                "change_24h": float(ticker["priceChangePercent"]),
                "source": "binance",
            }

        def pct_change(current: Optional[float], previous: Optional[float]) -> float:
            if current in (None, 0) or previous in (None, 0):
                return 0.0
            return ((current - previous) / previous) * 100

        price = safe_float(ticker.get("lastPrice"), closes[-1]) or closes[-1] or 0.0
        change_24h = safe_float(ticker.get("priceChangePercent"), 0.0) or 0.0
        change_1m = pct_change(closes[-1], closes[-2])
        change_3m = pct_change(closes[-1], closes[-4])
        change_5m = pct_change(closes[-1], closes[-6])
        change_15m = pct_change(closes[-1], closes[-16])
        range_high = max(item for item in highs[-15:] if item is not None)
        range_low = min(item for item in lows[-15:] if item is not None)
        range_span_pct = (((range_high - range_low) / range_low) * 100) if range_low else 0.0
        if range_high and range_low and range_high > range_low:
            range_position = (price - range_low) / (range_high - range_low)
        else:
            range_position = 0.5
        recent_volume = sum(item for item in volumes[-5:] if item is not None)
        prior_volume = sum(item for item in volumes[-10:-5] if item is not None)
        volume_ratio = (recent_volume / prior_volume) if prior_volume else 1.0

        direction = "flat"
        if change_3m > 0.08 and change_5m > 0.12:
            direction = "up"
        elif change_3m < -0.08 and change_5m < -0.12:
            direction = "down"
        elif change_3m > 0.04 and change_5m > 0.06:
            direction = "up"
        elif change_3m < -0.04 and change_5m < -0.06:
            direction = "down"

        # --- Fair value inputs (baseline: full 15-min window) ---
        # The BTC provider has no market context (no end_date), so we use a
        # full-window tau as a neutral baseline. The manager layer will
        # recompute fair value with the actual tau once it has the market.
        # ref_px ≈ current price (z ≈ 0) is the explicit baseline described
        # in the task spec; sigma is roughly derived from the observed
        # 15-min high/low range as a percentage.
        ref_px = float(price) if price else 0.0
        # range_span_15m_pct is in percent; convert to a dimensionless vol.
        # A 0.3% span → sigma ≈ 0.003. This is a coarse proxy; real vol
        # estimation should use log returns, but this is good enough as a
        # fallback when only OHLC extremes are known.
        sigma_15m = float(range_span_pct) / 100.0 if range_span_pct else 0.0
        # Floor at MIN_SIGMA so the math path is taken (otherwise fallback
        # to 50/50).
        if sigma_15m < _FAIR_MIN_SIGMA:
            sigma_15m = _FAIR_MIN_SIGMA

        fair = compute_fair_updown(
            s_now=ref_px,
            ref_px=ref_px,
            sigma_15m=sigma_15m,
            tau_sec=_FAIR_WINDOW_SEC,
            window_sec=_FAIR_WINDOW_SEC,
            drift=0.0,
        )

        return {
            "price": round(price, 2),
            "change_24h": round(change_24h, 2),
            "change_1m": round(change_1m, 4),
            "change_3m": round(change_3m, 4),
            "change_5m": round(change_5m, 4),
            "change_15m": round(change_15m, 4),
            "range_high_15m": round(range_high, 2) if range_high is not None else None,
            "range_low_15m": round(range_low, 2) if range_low is not None else None,
            "range_span_15m_pct": round(range_span_pct, 4),
            "range_position_15m": round(range_position, 4),
            "volume_ratio_5m": round(volume_ratio, 4),
            "direction_hint": direction,
            # Fair value (baseline; manager will recompute with real tau)
            "ref_px": round(ref_px, 2),
            "sigma_15m": round(sigma_15m, 6),
            "fair_up": fair["fair_up"],
            "fair_down": fair["fair_down"],
            "fair_z_score": fair["z_score"],
            "fair_edge_bps": fair["edge_bps_vs_market"],
            "source": "binance",
            # Kronos 预测字段 (默认 None, 调用者按需触发)
            "kronos_direction": None,
            "kronos_confidence": None,
            "kronos_edge_bps": None,
            "kronos_predicted_price": None,
            "kronos_anchor_price": None,
            "kronos_loaded": False,
        }

    async def _get_coingecko_price(self) -> Optional[Dict[str, Any]]:
        url = "https://api.coingecko.com/api/v3/simple/price?ids=bitcoin&vs_currencies=usd&include_24hr_change=true"
        async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=_DEFAULT_SSL_CTX)) as session:
            try:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        btc = data.get("bitcoin", {})
                        return {
                            "price": float(btc.get("usd", 0)),
                            "change_24h": float(btc.get("usd_24h_change", 0)),
                            "source": "coingecko",
                        }
            except Exception as exc:
                logger.error("CoinGecko API error: %s", exc)
        return None
