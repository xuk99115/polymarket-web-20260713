"""
LowBuy-Double 引擎 — BTC 15m 盘口"低买翻倍"策略 (盘口深度版).

原理 (2026-06-23 简化)
----
**纯盘口深度信号**, 不依赖 BTC 行情 / K 线 / 反转动量.

入场条件 (全部只看盘口):
  1. 时间窗: 窗口剩余 [LOWBUY_MIN_MINUTES, LOWBUY_MAX_MINUTES] 分钟
     (中段, 避开尾盘被结算预期压平 + 避开刚开盘定价未定型)
  2. Entry band: ask 在中低位回归带内
     (避开超低价弱势腿, 也避开高价追涨)
  3. Breakout guard: 对侧过强或本侧瞬间崩落时不接飞刀
  4. bid ≥ ask × (1 - LOWBUY_MAX_SPREAD_PCT) (流动性过滤)
  5. ask ≥ LOWBUY_MIN_ENTRY (避免垃圾流动性盘)

出场:
  - bid ≥ entry × LOWBUY_TP_MULT (1.4×) → 立即市价卖出
  - 窗口剩 LOWBUY_TIME_STOP_BEFORE_END 秒 (60s) 还没翻倍 → 强平
  - 否则持有到期 (EXPIRY_EXIT)

跟 reversal 关系
---------------
- reversal: 尾盘 (≤ 90s), 反转动量 + K 线判定
- LowBuy: 中段 (5-12min), **只看盘口深度**, 任何一方 ask ≤ 15¢ 都入
- 仓位池 = 2, 可同时存在

设计要点
--------
- 不需要 BTC 价格推送 (删除 push_btc_price)
- 不需要 Binance API (韩国网络封的痛点消失)
- scan() 是无状态的, 每次只看传入的 markets 数据
"""

import logging
import time as _time
from datetime import datetime, timezone, timedelta
from typing import Optional, Dict, Any, List, Tuple

logger = logging.getLogger("lowbuy_double")

# ============================================================
# 可调参数
# ============================================================

# 时间窗 (中段, 避开尾盘和开盘)
LOWBUY_MIN_MINUTES = 10.0         # 窗口剩余 ≥ 10 分钟才考虑入场 (2026-06-28 收紧; 分界后 8-10min 桶 15 笔 0 胜, pnl=-0.8504)
LOWBUY_MAX_MINUTES = 14.0         # 窗口剩余 ≤ 14 分钟才考虑入场 (保留中段早期窗口)

# 2026-07-07: 最近样本显示 30¢ 左右仍会接到趋势破位飞刀;
# 先只保留 32-34¢ 的窄回归带, 等新样本验证质量后再放宽。
# 2026-07-07 v2: 放宽到 30-36¢. 32-34¢ 区间窗口频率太低(24h 仅 ~5 个),
# S/R filter 已承担"破位"拦截职责, 不再需要 30-31¢ 的额外隔离.
LOWBUY_MAX_ENTRY = 0.36           # 入口上限: 36¢

# 价格阈值 (2026-06-24 调整: 中价位回归 → 低位捡便宜)
# 旧: LOWBUY_MAX_ENTRY=0.15, LOWBUY_MIN_ENTRY=0.04 → 超低价 (≤15¢)
#   问题: 超低价一般意味着强趋势压制, 15min 内难以翻倍回归
# 新: LOWBUY_MAX_ENTRY=0.50 → 还是太宽, 50¢ 不叫便宜
# 2026-06-24 v2: 改为 LOWBUY_MAX_ENTRY=0.40 — 只买 20-40¢ 的"便宜反转方"
#   在 Polymarket 二元盘口里, ask≤40¢ 意味该 outcome 概率≤40%,
#   有 15-25% 的回归空间 (40→60¢ = 1.5× TP)
# 2026-06-25 v3: 拉宽到 22-45¢ — 当前盘口长期 U49/D52, 26-39¢ 一周难碰一次.
#   22¢ 仍有 2×+ 翻倍空间 (→44¢), 45¢ 仍算便宜 (→90¢ TP 难但 TIME_STOP 兜底).
#   这条历史注释保留作演化背景; 当前生产链路已不启用 Fair Value 入场过滤.
LOWBUY_MIN_ENTRY = 0.30           # ask ≥ 30¢ (放宽, S/R filter 兜底破位拦截)
LOWBUY_CORE_MAX_ENTRY = 0.36      # 当前全部入口都按主入口处理
LOWBUY_EXT_MIN_MINUTES = 12.0     # 扩展入口只在更靠后的窗口使用 (12-14min)
LOWBUY_EXT_MIN_FV_UP = 0.58       # 扩展入口需要 FV 强支持 Up
LOWBUY_EXT_MAX_FV_UP = 0.42       # 扩展入口需要 FV 强支持 Down

# 趋势破位保护: 不把强趋势里砸出来的低价误判成便宜。
LOWBUY_MAX_OPPOSITE_ASK = 0.67     # 对侧 ask ≥67¢ 时, 候选侧通常已是强趋势下的弱势腿
LOWBUY_DROP_LOOKBACK_SEC = 20.0    # 回看最近 20 秒 ask 变化
LOWBUY_MAX_RECENT_ASK_DROP = 0.08  # 候选 ask 20 秒内下跌 ≥8¢, 不接飞刀

# 订单簿失衡度 (OBI) 过滤 (2026-06-26)
# LOWBUY 是逆向策略 (买被市场抛弃的廉价 outcome).
# OBI = (bid_vol_top3 - ask_vol_top3) / (bid_vol_top3 + ask_vol_top3) ∈ [-1, +1]
# 负值 = 卖盘占优 (价格被压低 → 逆向入场良机)
# 正值 = 买盘占优 (价格被拉高 → 追高接飞刀风险)
# ≥ REJECT: 买盘过热, 拒绝入场 (接飞刀)
# ≤ BOOST: 卖盘抛售, 逆向入场 (高信心)
LOWBUY_REJECT_OBI = 0.7           # ≥+0.7: 买盘过热 → 拒 (逆向策略不追高)
LOWBUY_BOOST_OBI = -0.5           # ≤-0.5: 卖盘抛售 → 逆向入场良机, 提高信心

# S/R 支撑阻力过滤 (2026-07-07 新增)
# 基于 _quote_history 计算 BTC 在近期支撑-阻力区间的分位数,
# 拦截结构性方向错判: 阻力区不买涨, 支撑区不买跌.
LOWBUY_SR_WINDOWS = 4              # S/R 计算回看窗口数 (15m × 4 ≈ 1h)
LOWBUY_SR_UP_SUPPORT_THRES = 0.25  # BTC 处于支撑区 (分位数 ≤ 0.25) 时, 优先支持 UP 合约
LOWBUY_SR_DOWN_RESIST_THRES = 0.75 # BTC 处于阻力区 (分位数 ≥ 0.75) 时, 优先支持 DOWN 合约

# 流动性过滤
LOWBUY_MAX_SPREAD_PCT = 0.15      # bid ≥ ask × (1 - 15%) → spread ≤ 15%

# 止盈参数 (2026-06-25 v3: TP 回到 2.0×, 配合 22-45¢ entry 用)
# 低价位 22¢ × 2.0 = 44¢ (可行); 高价位 45¢ × 2.0 = 90¢ (15min 困难, 靠 TIME_STOP 兜底)
LOWBUY_TP_MULT = 1.4              # bid ≥ entry × 1.4 立即平仓 (2026-06-27 改: Q1 模拟 TP×1.4 胜率 61.3% vs TP×2.0 16.1%; 同份 51000 ticks 28 slug 31 次触发)

# TP 深度/持续时间检查 (2026-06-26 防假突破):
# paper 模拟把整笔 stake 都按 best_bid 成交, 但实盘盘口可能只有 0.01-0.5 shares @ 73¢
# 加三层过滤: bid 厚度, 持续时间, stake 对比.
LOWBUY_TP_MIN_DEPTH = 0.5          # best_bid 处至少 0.5 shares 才能 TP (防薄盘口假突破)
LOWBUY_TP_MIN_DURATION = 1.0       # bid >= tp_target 持续 1 秒才触发 (防瞬间跳价; 2026-06-26 从 3s 降低, 薄盘口 3s 太长)
LOWBUY_TP_MIN_COVERAGE = 0.5       # bid 厚度覆盖 >= 50% stake 才算有效 TP

# 5 分钟止损 (2026-06-24 新增: 剩余 5min 时还没到 TP 就止损)
# 逻辑: 窗口剩 5 分钟时, 该侧价格还没翻倍, 说明方向大概率错了
# 及时止损比等到归零好 (亏 50-70% vs 亏 100%)
LOWBUY_STOP_AT_MINUTES = 5.0      # 剩 N 分钟时检查止损
LOWBUY_HARD_STOP_MULT = 0.55      # bid ≤ entry*55% 时提前止损, 不等 5 分钟

# 时间止损 (2026-06-26 修复: EXPIRY_EXIT 兜底)
# 旧 LOWBUY_TIME_STOP_BEFORE_END = 0 → 兜底永不触发, 仓位永远卡在 OPEN 状态
# _manage_open_positions 又跳过 LOWBUY 仓位, EXPIRY_EXIT 路径走不到
# 改 -1: 任何过期都触发兜底清理 (markets API 不再返回 slug 时)
# 用户可以靠 EXPIRY_EXIT 让仓位自然归零 (2.x.c0 模型)
LOWBUY_TIME_STOP_BEFORE_END = -1   # -1 = 过期即清理

# 仓位 (单笔)
# 2026-06-24 调整: 0.30 → 1.00, 跟 reversal 一致
# 旧 0.30 太小: 100 笔才 $30 波动, 期望值难体现, 心理上也不痛不痒
# 新 1.00: 单笔 ±$1 才有 impact, 100 笔 100×1.5×50% win = $50 上限
LOWBUY_POSITION_USD = 2.00        # 跟 reversal 同仓位; 2026-06-24 从 1.00 提高到 2.00


class LowBuyDoubleEngine:
    """低买翻倍策略引擎 — 纯盘口深度版.

    用法:
        engine = LowBuyDoubleEngine()
        signals = engine.scan(markets, now_utc)
    """

    def __init__(self):
        # 已发出但尚未平仓的仓位: slug -> {outcome_index, entry_price, entry_time, tp_target}
        self._open_positions: Dict[str, Dict[str, Any]] = {}
        self._quote_history: Dict[str, List[Dict[str, Any]]] = {}
        # 最近一次扫描的元信息
        self._last_scan_at: Optional[datetime] = None
        self._last_scan_summary: Dict[str, Any] = {}

    def _record_market_quotes(self, market: Dict[str, Any], now_utc: datetime) -> None:
        slug = market.get("slug", "")
        cutoff = now_utc.timestamp() - max(LOWBUY_DROP_LOOKBACK_SEC * 2, 60.0)
        for idx, outcome in enumerate(market.get("outcomes") or []):
            ask = outcome.get("best_ask")
            bid = outcome.get("best_bid")
            if ask is None or bid is None:
                continue
            try:
                ask_f = float(ask)
                bid_f = float(bid)
            except (TypeError, ValueError):
                continue
            key = f"{slug}:{idx}"
            history = self._quote_history.setdefault(key, [])
            history.append({"t": now_utc.timestamp(), "ask": ask_f, "bid": bid_f})
            self._quote_history[key] = [item for item in history if item.get("t", 0) >= cutoff][-80:]

    def _recent_ask_drop(self, slug: str, outcome_index: int, current_ask: float, now_utc: datetime) -> float:
        cutoff = now_utc.timestamp() - LOWBUY_DROP_LOOKBACK_SEC
        history = [
            item for item in self._quote_history.get(f"{slug}:{outcome_index}", [])
            if cutoff <= item.get("t", 0) < now_utc.timestamp()
        ]
        if not history:
            return 0.0
        previous_peak = max(float(item.get("ask", current_ask) or current_ask) for item in history)
        return max(0.0, previous_peak - current_ask)

    def _infer_direction_fallback(
        self,
        direction_hint: Optional[str],
        outcome_index: int,
        ask: float,
        opposite_ask: Optional[float],
        recent_ask_drop: float,
    ) -> Optional[str]:
        """Fallback for weak/flat BTC direction hints using market structure."""
        normalized = str(direction_hint or "").lower()
        if normalized in {"up", "down"}:
            return normalized

        if opposite_ask is None:
            return None

        # If one side has already expanded to ~67c while the candidate side
        # just fell into the 30s, treat it as an active directional move even
        # when upstream BTC direction is still flat.
        if outcome_index == 0 and opposite_ask >= LOWBUY_MAX_OPPOSITE_ASK and (
            ask <= 0.34 or recent_ask_drop >= 0.04
        ):
            return "down"
        if outcome_index == 1 and opposite_ask >= LOWBUY_MAX_OPPOSITE_ASK and (
            ask <= 0.34 or recent_ask_drop >= 0.04
        ):
            return "up"
        return None

    # ----- 仓位追踪 -----

    def register_entry(
        self,
        slug: str,
        outcome_index: int,
        entry_price: float,
        now_utc: datetime,
        stake: Optional[float] = None,
    ) -> None:
        key = f"{slug}:{outcome_index}"
        self._open_positions[key] = {
            "outcome_index": outcome_index,
            "entry_price": entry_price,
            "entry_time": now_utc,
            "tp_target": entry_price * LOWBUY_TP_MULT,
            "stake": stake if stake is not None else LOWBUY_POSITION_USD,
        }
        logger.info(
            "💰 [LowBuy] 注册仓位: slug=%s outcome=%d entry=%.2f¢ tp=%.2f¢",
            slug, outcome_index, entry_price * 100, entry_price * LOWBUY_TP_MULT * 100,
        )

    def close_position(self, slug: str, outcome_index: Optional[int] = None) -> Optional[Dict[str, Any]]:
        key = f"{slug}:{outcome_index}" if outcome_index is not None else slug
        return self._open_positions.pop(key, None)

    def open_positions(self) -> Dict[str, Dict[str, Any]]:
        return dict(self._open_positions)

    def _has_position(self, slug: str) -> bool:
        """检查 slug 是否有任何 open 仓位 (可能多个 outcome)."""
        return any(k.startswith(f"{slug}:") for k in self._open_positions)

    def _open_keys_for_slug(self, slug: str) -> List[str]:
        """返回 slug 对应的所有 open 仓位 key."""
        return [k for k in self._open_positions if k.startswith(f"{slug}:")]

    # ----- 信号扫描 -----

    def scan(
        self,
        markets: List[Dict[str, Any]],
        now_utc: datetime,
        fair_up: Optional[float] = None,
        direction_hint: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """扫描所有 BTC 15m 窗口, 返回信号列表.

        每个信号:
            {
                "action": "BUY" | "TAKE_PROFIT" | "TIME_STOP",
                "slug": str,
                "outcome_index": int,
                "outcome_label": str,
                "current_bid": float,
                "current_ask": float,
                "reason": str,
                "confidence": float,
            }
        """
        signals: List[Dict[str, Any]] = []
        self._last_scan_at = now_utc

        for market in markets:
            slug = market.get("slug", "")
            end_date = market.get("end_date", "")
            if not slug or not end_date:
                continue

            # 只看 BTC 15m 窗口
            if not slug.startswith("btc-updown-15m-"):
                continue

            try:
                end_dt = datetime.fromisoformat(end_date.replace("Z", "+00:00"))
            except Exception:
                continue
            if end_dt.tzinfo is None:
                end_dt = end_dt.replace(tzinfo=timezone.utc)

            seconds_to_end = (end_dt - now_utc).total_seconds()
            minutes_to_end = seconds_to_end / 60.0
            self._record_market_quotes(market, now_utc)

            # 已有仓位 → 检查 TAKE_PROFIT 优先, TIME_STOP 兜底
            # Bug fix 2026-06-26: 先检查 TP 再 TIME_STOP
            # 旧逻辑 `if minutes_to_end < 5: TIME_STOP else: TP` 导致
            # 最后 5 分钟 bid/tp_target 达标时不 TP, 被迫低位 TIME_STOP.
            if self._has_position(slug):
                for key in self._open_keys_for_slug(slug):
                    pos = self._open_positions.get(key)
                    if not pos:
                        continue
                    oi = pos.get("outcome_index")
                    if not isinstance(oi, int):
                        continue
                    # 先检查 TP (不分时间窗口, 任何时候达到就卖)
                    tp_sig = self._check_take_profit(market, slug, oi)
                    if tp_sig:
                        signals.append(tp_sig)
                        continue
                    hard_stop_sig = self._check_hard_stop(market, slug, oi, pos)
                    if hard_stop_sig:
                        signals.append(hard_stop_sig)
                        continue
                    # 没到 TP 且快结束 → TIME_STOP
                    if minutes_to_end < (LOWBUY_TIME_STOP_BEFORE_END / 60.0):
                        sig = self._maybe_time_stop(market, slug, oi, seconds_to_end)
                        if sig:
                            signals.append(sig)
                    elif minutes_to_end <= LOWBUY_STOP_AT_MINUTES:
                        # 剩余 ≤5 分钟还没翻倍 → 时间止损 (方向大概率错了)
                        # Bug fix 2026-06-27: 移出过期条件, 否则 LOWBUY_TIME_STOP_BEFORE_END=-1
                        # 让这个分支永远进不去, 5分钟止损形同虚设
                        sig = self._stop_at_time(market, slug, oi, pos, seconds_to_end)
                        if sig:
                            signals.append(sig)
                    else:
                        # 还有时间, 继续持仓等待
                        pass
                continue

            # 还没仓位 → 检查入场条件 (时间窗)
            if not (LOWBUY_MIN_MINUTES <= minutes_to_end <= LOWBUY_MAX_MINUTES):
                logger.info("[LowBuy/时间窗] 跳过 %s: mte=%.1fmin 不在 [%.0f, %.0f]min",
                            slug, minutes_to_end,
                            LOWBUY_MIN_MINUTES, LOWBUY_MAX_MINUTES)
                continue

            entry_sigs = self._check_entries(
                market, slug, minutes_to_end,
                fair_up=fair_up,
                direction_hint=direction_hint,
                now_utc=now_utc,
            )
            for sig in entry_sigs:
                signals.append(sig)

        # 兜底检查: scan 末尾检查所有 _open_positions 里但 markets 列表没返回的 slug
        # (因为市场关闭后,Polymarket API 不再返回该 slug,导致扫描漏掉)
        # 注意: _open_positions 的 key 是 "slug:outcome_index" 格式 (compound key)
        actual_slugs = {m.get("slug") for m in markets if m.get("slug")}
        for compound_key in list(self._open_positions.keys()):
            # 从 compound_key 提取真实 slug (格式: "slug:outcome_index")
            parts = compound_key.rsplit(":", 1)
            real_slug = parts[0]
            if real_slug in actual_slugs:
                continue
            pos = self._open_positions.get(compound_key)
            if not pos:
                continue
            # 仓位但市场已不再返回 → 计算过期时间
            # 优先从 slug 时间戳取精确过期 (btc-updown-15m-{end_timestamp})
            # 回退到 entry_time + 15min 估算
            approx_end = None
            if real_slug.startswith("btc-updown-15m-"):
                try:
                    ts = int(real_slug.split("-")[-1])
                    approx_end = datetime.fromtimestamp(ts, tz=timezone.utc)
                except Exception:
                    pass
            if approx_end is None:
                entry_time = pos.get("entry_time", now_utc)
                if isinstance(entry_time, str):
                    try:
                        entry_time = datetime.fromisoformat(entry_time.replace("Z","+00:00"))
                        if entry_time.tzinfo is None:
                            entry_time = entry_time.replace(tzinfo=timezone.utc)
                    except Exception:
                        entry_time = now_utc
                # 默认窗口 15 分钟 — 估算过期时间
                approx_end = entry_time + timedelta(minutes=15)
            seconds_to_end = (approx_end - now_utc).total_seconds()
            if seconds_to_end <= LOWBUY_TIME_STOP_BEFORE_END:
                # 过期盘口: 等 Polymarket settlement 出来再结算
                # 不要立即用 estimated_bid=0 强平 — 震荡行情尾盘 bid 可能跳动,
                # 等 outcomePrices 出来后再用 _resolve_settlement_price 结算
                logger.info("🧹 [LowBuy] 兜底检查: %s 过期 %.0fs, 等 settlement...", real_slug, seconds_to_end)
            else:
                # 市场还在 active slugs 但没到过期时间 → 跳过 (等正常循环处理)
                continue

            # 如果 settlement 正在等 (sold slug still in markets list or outcomePrices not available),
            # 先检查有没有 outcomePrices (从市场价格快照). 如果没有, 跳过本轮.
            oi = pos.get("outcome_index", 0)
            entry_px = pos.get("entry_price", 0) or 0
            estimated_bid = 0.0  # fallback

            # 检查同 slug 的 market 是否在传入的 markets 里且包含 outcomePrices
            for m in markets:
                if m.get("slug") == real_slug:
                    outcome_prices = m.get("outcomePrices")
                    if outcome_prices and isinstance(outcome_prices, (list, tuple)):
                        if oi < len(outcome_prices):
                            try:
                                sp = float(outcome_prices[oi])
                                if 0.0 <= sp <= 1.0:
                                    estimated_bid = sp
                                    logger.info("  [LowBuy] 过期盘口 %s outcome=%d 结算价=%.1f¢",
                                                real_slug, oi, sp * 100)
                            except (ValueError, TypeError):
                                pass
                    break

            # 如果没有 outcomePrices 且过期不到 60s, 等下一轮 (可能 settlement 还没就绪)
            if estimated_bid == 0.0:
                elapsed_since_expiry = max(0.0, -seconds_to_end)
                if elapsed_since_expiry < 60.0:
                    logger.debug("[LowBuy] %s 过期 %.0fs, 无 outcomePrices, 等待结算 (已等 %.0fs)",
                                real_slug, -seconds_to_end, elapsed_since_expiry)
                    continue

            logger.info("  [LowBuy] %s 清理: 结算价=%.1f¢", real_slug, estimated_bid * 100)
            signals.append({
                    "action": "TIME_STOP",
                    "slug": real_slug,
                    "outcome_index": oi,
                    "outcome_label": "?" if isinstance(oi, int) else "?",
                    "current_bid": estimated_bid,
                    "current_ask": estimated_bid,
                    "reason": f"[LowBuy] 兜底清理: 市场已过期, entry={entry_px*100:.1f}¢",
                    "close_reason_code": "expiry_settle",
                    "close_reason_label": "到期结算",
                    "confidence": 0.0,
                    "position_entry_price": entry_px,
                })

        self._last_scan_summary = {
            "scanned": len(markets),
            "open_positions": len(self._open_positions),
            "signals": len(signals),
            "signals_breakdown": {
                "BUY": sum(1 for s in signals if s["action"] == "BUY"),
                "TAKE_PROFIT": sum(1 for s in signals if s["action"] == "TAKE_PROFIT"),
                "TIME_STOP": sum(1 for s in signals if s["action"] == "TIME_STOP"),
            },
        }
        logger.debug("[LowBuy/scan] 返回 %d 信号 (%d 清理)", len(signals),
            sum(1 for s in signals if "兜底" in s.get("reason","")))
        return signals

    # ----- 入场判定 -----

    @staticmethod
    def _compute_obi(outcome: Dict[str, Any]) -> Optional[float]:
        """计算订单簿失衡度 OBI = (bid_vol - ask_vol) / (bid_vol + ask_vol).

        使用 top-3 深度, 范围 [-1, +1].
        +1 = 完全买盘占优, -1 = 完全卖盘占优.
        返回 None 如果没有深度数据.
        """
        bids = outcome.get("depth_bids") or outcome.get("bids") or []
        asks = outcome.get("depth_asks") or outcome.get("asks") or []
        bid_vol = sum(float(b.get("size", 0) or 0) for b in bids[:3])
        ask_vol = sum(float(a.get("size", 0) or 0) for a in asks[:3])
        total = bid_vol + ask_vol
        if total <= 0:
            return None
        return round((bid_vol - ask_vol) / total, 3)

    def _compute_sr_position(self, slug: str, current_ask: float) -> Optional[float]:
        """
        基于 _quote_history 历史 bid/ask 中间价, 计算 BTC 在近期支撑位-阻力位区间的分位数.

        0.0 = 处于支撑位 (近期最低), 1.0 = 处于阻力位 (近期最高).
        None = 历史数据不足 (< 8 个样本).

        实现细节: 同时取 Up (idx=0) 和 Down (idx=1) 的 bid/ask 中间价,
        Down 用 (1.0 - mid) 反向, 这样两种 outcome 的 mid 数值有可比性
        (在有效市场中 Up_mid + Down_mid ≈ 1.0).
        """
        key_0 = f"{slug}:0"
        key_1 = f"{slug}:1"
        hist_0 = self._quote_history.get(key_0, [])
        hist_1 = self._quote_history.get(key_1, [])

        prices: List[float] = []
        for h in hist_0:
            ask = h.get("ask")
            bid = h.get("bid")
            if ask is not None and bid is not None:
                prices.append((float(ask) + float(bid)) / 2.0)
        for h in hist_1:
            ask = h.get("ask")
            bid = h.get("bid")
            if ask is not None and bid is not None:
                prices.append(1.0 - (float(ask) + float(bid)) / 2.0)

        if len(prices) < 8:
            return None

        low = min(prices)
        high = max(prices)
        if high <= low:
            return 0.5

        pos = (current_ask - low) / (high - low)
        return min(1.0, max(0.0, pos))

    def _check_entries(
        self,
        market: Dict[str, Any],
        slug: str,
        minutes_to_end: float,
        fair_up: Optional[float] = None,
        direction_hint: Optional[str] = None,
        now_utc: Optional[datetime] = None,
    ) -> List[Dict[str, Any]]:
        """检查是否满足低买入场条件.

        返回所有符合条件的 BUY 信号列表 (可能 0, 1, 或 2 个).

        单边便宜: 22-30¢ 维持原始 lowbuy 逻辑
        扩展区间: 30-40¢ 需要更强确认 (更靠后 MTE + 盘口/趋势保护)

        条件 (每个 outcome):
          1. ask ∈ [LOWBUY_MIN_ENTRY, LOWBUY_MAX_ENTRY]  (默认 26-39¢)
          2. bid ≥ ask × (1 - LOWBUY_MAX_SPREAD_PCT)     (流动性过滤)
          3. BTC 趋势方向过滤 (2026-06-26): 趋势跌时不买 Up, 趋势涨时不买 Down
          4. 当前生产链路不启用 Fair Value 入场过滤; FV 仅保留研究/记录用途
        """
        outcomes = market.get("outcomes") or []
        if len(outcomes) < 2:
            return []

        candidates: List[Dict[str, Any]] = []
        for idx, outcome in enumerate(outcomes):
            ask = outcome.get("best_ask")
            bid = outcome.get("best_bid")
            if ask is None or bid is None:
                continue
            ask = float(ask)
            bid = float(bid)

            if ask > LOWBUY_MAX_ENTRY or ask < LOWBUY_MIN_ENTRY:
                logger.info("[LowBuy/entry] 跳过 %s: ask=%.1f¢ 不在区间 [%.0f,%.0f]¢",
                            outcome.get("label", "?"), ask*100,
                            LOWBUY_MIN_ENTRY*100, LOWBUY_MAX_ENTRY*100)
                continue
            if bid < ask * (1 - LOWBUY_MAX_SPREAD_PCT):
                logger.info("[LowBuy/spread] 跳过 %s: spread=%.1f%% > %.0f%% (bid=%.3f ask=%.3f)",
                            outcome.get("label", "?"), (ask-bid)/ask*100,
                            LOWBUY_MAX_SPREAD_PCT*100, bid, ask)
                continue

            opposite_ask = None
            if len(outcomes) >= 2:
                try:
                    opposite_ask = float(outcomes[1 - idx].get("best_ask"))
                except (TypeError, ValueError):
                    opposite_ask = None
            if opposite_ask is not None and opposite_ask >= LOWBUY_MAX_OPPOSITE_ASK:
                logger.info(
                    "[LowBuy/破位] 跳过 %s: 对侧 ask=%.1f¢ ≥ %.0f¢, 候选侧可能是强趋势弱势腿",
                    outcome.get("label", "?"), opposite_ask * 100, LOWBUY_MAX_OPPOSITE_ASK * 100,
                )
                continue

            recent_ask_drop = 0.0
            if now_utc is not None:
                recent_ask_drop = self._recent_ask_drop(slug, idx, ask, now_utc)
            if recent_ask_drop >= LOWBUY_MAX_RECENT_ASK_DROP:
                logger.info(
                    "[LowBuy/跳变] 跳过 %s: ask 最近 %.0fs 下跌 %.1f¢ ≥ %.1f¢, 不接飞刀",
                    outcome.get("label", "?"), LOWBUY_DROP_LOOKBACK_SEC,
                    recent_ask_drop * 100, LOWBUY_MAX_RECENT_ASK_DROP * 100,
                )
                continue

            is_extension = ask > LOWBUY_CORE_MAX_ENTRY

            # 扩展区间只在更靠后的窗口使用
            if is_extension and minutes_to_end < LOWBUY_EXT_MIN_MINUTES:
                logger.info(
                    "[LowBuy/entry] 跳过 %s: ask=%.1f¢ 属于扩展区间, 但 mte=%.1fmin < %.0fmin",
                    outcome.get("label", "?"), ask * 100, minutes_to_end, LOWBUY_EXT_MIN_MINUTES,
                )
                continue

            # BTC 趋势方向过滤 (2026-06-26)
            # 趋势向下时不买 Up (接飞刀), 趋势向上时不买 Down (逆势)
            effective_direction_hint = self._infer_direction_fallback(
                direction_hint=direction_hint,
                outcome_index=idx,
                ask=ask,
                opposite_ask=opposite_ask,
                recent_ask_drop=recent_ask_drop,
            )
            if effective_direction_hint == "down" and idx == 0:
                logger.info("[LowBuy/趋势] 跳过 Up: BTC trend=down (阴跌趋势不买涨)")
                continue
            if effective_direction_hint == "up" and idx == 1:
                logger.info("[LowBuy/趋势] 跳过 Down: BTC trend=up (上涨趋势不买跌)")
                continue

            # Fair Value 方向过滤逻辑保留给研究/实验用途。
            # 当前生产链路上游不会传入 fair_up, 因此不会阻断入场。
            # 扩展区间当前主要靠更靠后的 MTE、趋势、OBI、S/R 等保护。
            if is_extension and fair_up is not None:
                if idx == 0 and fair_up < LOWBUY_EXT_MIN_FV_UP:
                    logger.info(
                        "[LowBuy/FV] 跳过 Up: fair_up=%.2f < %.2f (扩展区间需要更强看涨)",
                        fair_up, LOWBUY_EXT_MIN_FV_UP,
                    )
                    continue
                if idx == 1 and fair_up > LOWBUY_EXT_MAX_FV_UP:
                    logger.info(
                        "[LowBuy/FV] 跳过 Down: fair_up=%.2f > %.2f (扩展区间需要更强看跌)",
                        fair_up, LOWBUY_EXT_MAX_FV_UP,
                    )
                    continue
            elif fair_up is not None:
                if idx == 0 and fair_up < 0.35:  # Up, 但 FV 说大概率跌
                    logger.info("[LowBuy/FV] 跳过 Up: fair_up=%.2f < 0.35 (FV看跌)", fair_up)
                    continue
                if idx == 1 and fair_up > 0.65:  # Down, 但 FV 说大概率涨
                    logger.info("[LowBuy/FV] 跳过 Down: fair_up=%.2f > 0.65 (FV看涨)", fair_up)
                    continue

            # S/R 过滤 (2026-07-07 新增): 阻力区不买涨, 支撑区不买跌
            sr_pos = self._compute_sr_position(slug, ask)
            if sr_pos is not None:
                if idx == 0 and sr_pos >= LOWBUY_SR_DOWN_RESIST_THRES:
                    logger.info(
                        "[LowBuy/SR] 跳过 Up: sr_pos=%.2f >= %.2f (BTC 阻力区, 不买涨)",
                        sr_pos, LOWBUY_SR_DOWN_RESIST_THRES,
                    )
                    continue
                if idx == 1 and sr_pos <= LOWBUY_SR_UP_SUPPORT_THRES:
                    logger.info(
                        "[LowBuy/SR] 跳过 Down: sr_pos=%.2f <= %.2f (BTC 支撑区, 不买跌)",
                        sr_pos, LOWBUY_SR_UP_SUPPORT_THRES,
                    )
                    continue

            # OBI 过滤: 订单簿失衡度 (2026-06-26)
            # LOWBUY 逆向策略: 卖盘抛售时(负OBI)入场, 买盘过热时(正OBI)拒
            obi = self._compute_obi(outcome)
            if obi is not None and obi >= LOWBUY_REJECT_OBI:
                logger.info("[LowBuy/OBI] 跳过 %s: OBI=%.2f ≥ %.1f (买盘过热, 追高接飞刀风险)",
                             outcome.get("label", "?"), obi, LOWBUY_REJECT_OBI)
                continue
            if is_extension and obi is not None and obi > 0.15:
                logger.info(
                    "[LowBuy/OBI] 跳过 %s: 扩展区间 OBI=%.2f 偏热, 不够冷",
                    outcome.get("label", "?"), obi,
                )
                continue

            opposite_ask_text = f"{opposite_ask*100:.1f}¢" if opposite_ask is not None else "--"
            candidate = {
                "action": "BUY",
                "slug": slug,
                "outcome_index": idx,
                "outcome_label": outcome.get("label", ""),
                "current_bid": bid,
                "current_ask": ask,
                "entry_mte": round(minutes_to_end, 3),
                "entry_bid": bid,
                "entry_ask": ask,
                "opposite_ask": opposite_ask,
                "direction_hint": effective_direction_hint or direction_hint,
                "obi": obi,
                "recent_ask_drop": round(recent_ask_drop, 4),
                "reason": (
                    f"[LowBuy] 低买入场: {outcome.get('label', '?')} "
                    f"ask={ask*100:.1f}¢ (区间 {LOWBUY_MIN_ENTRY*100:.0f}-{LOWBUY_MAX_ENTRY*100:.0f}¢), "
                    f"{'扩展确认' if is_extension else '主入口'} TP={ask * LOWBUY_TP_MULT * 100:.1f}¢ "
                    f"(剩 {minutes_to_end:.1f}min, spread {(ask-bid)/ask*100:.1f}%, "
                    f"opp_ask={opposite_ask_text}, recent_drop={recent_ask_drop*100:.1f}¢)"
                ),
                "confidence": 0.70,
            }
            candidates.append(candidate)
            # OBI 标记: 如果 OBI <= BOOST, 卖盘抛售→逆向入场, 提高信心
            if obi is not None and obi <= LOWBUY_BOOST_OBI:
                candidates[-1]["confidence"] = 0.85
                candidates[-1]["reason"] += f" (OBI={obi:.2f}, 卖盘抛售逆向)"

        # 两边都便宜 (< $1) 看起来像套利, 但当前执行层实行"每窗口一仓"
        # (manager._lowbuy_open 会按 slug 拦第二笔), 且 LowBuy engine 仍会对第一笔
        # 执行 TP/5min TIME_STOP, 并不会真的持有到期锁定 $1 payout。
        # 旧代码把两边候选都标成 "双边无风险" + hold_to_expiry=True, 实际只成交一边,
        # 会把单边方向风险误报成无风险。修复: 不再改写信号语义; 让两个候选继续
        # 作为普通 LowBuy BUY, 执行层只会开第一笔。后续若要真做双边, 必须实现
        # 原子 pair-open + pair-close/pair-settle, 不能靠这个标记。
        if len(candidates) == 2:
            sum_pct = sum(c["current_ask"] * 100 for c in candidates)
            for c in candidates:
                c["reason"] += f" (同窗两侧候选合计 {sum_pct:.0f}¢; 当前执行层每窗口只开一侧, 不按双边无风险处理)"

        return candidates

    # ----- 出场判定 -----

    def _check_take_profit(
        self,
        market: Dict[str, Any],
        slug: str,
        outcome_index: int,
    ) -> Optional[Dict[str, Any]]:
        """如果 best_bid ≥ entry × LOWBUY_TP_MULT, 出 TAKE_PROFIT 信号.

        Bug fix 2026-06-26: 加三层防假突破检查.
        1) 厚度: best_bid 处至少 LOWBUY_TP_MIN_DEPTH shares (0.5)
        2) 持续: bid >= tp_target 持续 LOWBUY_TP_MIN_DURATION 秒 (3s)
        3) 覆盖: best_bid 厚度 >= LOWBUY_TP_MIN_COVERAGE × stake (50%)
        """
        key = f"{slug}:{outcome_index}"
        pos = self._open_positions.get(key)
        if not pos:
            return None
        tp_target = pos["tp_target"]
        entry_price = pos["entry_price"]
        stake = pos.get("stake", LOWBUY_POSITION_USD)

        outcomes = market.get("outcomes") or []
        if outcome_index >= len(outcomes):
            return None
        outcome = outcomes[outcome_index]
        bid = outcome.get("best_bid")
        if bid is None:
            return None
        bid = float(bid)

        if bid < tp_target:
            # 清除持续时间记录 (bid 跌回阈值以下)
            pos.pop("tp_first_hit_at", None)
            return None

        # 1) 持续时间检查: 第一次达到阈值的时间 (不依赖 bids 深度数据, 放第一)
        now = datetime.now(timezone.utc)
        first_hit = pos.get("tp_first_hit_at")
        if first_hit is None:
            pos["tp_first_hit_at"] = now
            return None
        elapsed = (now - first_hit).total_seconds()
        if elapsed < LOWBUY_TP_MIN_DURATION:
            logger.debug("[LowBuy] TP 防假突破: bid 达阈值才 %.1fs (< %.1fs)",
                         elapsed, LOWBUY_TP_MIN_DURATION)
            return None

        # 2) 厚度检查: best_bid 处的挂单 shares
        # 注意: outcome.get("depth_bids") 来自 _merge_book_quotes 存入的原始深度.
        # 如果 depth_bids 不可用 (兜底路径用 fake outcome), 跳过厚度/覆盖检查,
        # 仅保留持续时间过滤
        bids = outcome.get("depth_bids") or outcome.get("bids") or []
        if bids:
            depth_at_bid = 0.0
            for b in bids:
                bp = float(b.get("price", 0) or 0)
                bs = float(b.get("size", 0) or 0)
                if bp >= tp_target - 0.001:  # 同价位或更高
                    depth_at_bid += bs
            if depth_at_bid < LOWBUY_TP_MIN_DEPTH:
                logger.debug("[LowBuy] TP 防假突破: bid 厚度 %.2f < %.2f",
                             depth_at_bid, LOWBUY_TP_MIN_DEPTH)
                pos.pop("tp_first_hit_at", None)
                return None

            # 3) 覆盖检查: bid 厚度是否覆盖 stake
            shares_needed = stake / entry_price  # 我们要卖出的份数
            coverage = depth_at_bid / shares_needed if shares_needed > 0 else 0
            if coverage < LOWBUY_TP_MIN_COVERAGE:
                logger.info("[LowBuy] TP 防假突破: 厚度覆盖 %.0f%% stake (%.2f/%.2f shares, 需要>=%.0f%%)",
                            coverage * 100, depth_at_bid, shares_needed, LOWBUY_TP_MIN_COVERAGE * 100)
                pos.pop("tp_first_hit_at", None)
                return None
        else:
            # 无深度数据: 跳过厚度/覆盖检查, 仅靠持续时间过滤
            depth_at_bid = 0.0
            shares_needed = stake / entry_price if entry_price > 0 else 0
            coverage = 0.0
            logger.debug("[LowBuy] TP: 无深度数据, 跳过厚度/覆盖检查 (仅持续时间过滤 %.0fs)",
                         LOWBUY_TP_MIN_DURATION)

        # 三层都通过, 真实 TP
        return {
            "action": "TAKE_PROFIT",
            "slug": slug,
            "outcome_index": outcome_index,
            "outcome_label": outcome.get("label", ""),
            "current_bid": bid,
            "current_ask": bid,
            "depth_at_bid": depth_at_bid,
            "shares_needed": shares_needed,
            "coverage": coverage,
            "elapsed_sec": elapsed,
            "reason": (
                f"[LowBuy] 翻倍达成! entry={entry_price*100:.1f}¢ "
                f"→ exit={bid*100:.1f}¢ (×{bid/entry_price:.2f}, "
                f"厚度 {depth_at_bid:.1f}/{shares_needed:.1f} = {coverage:.0%}, "
                f"持续 {elapsed:.1f}s)"
            ),
            "confidence": 1.0,
            "position_entry_price": entry_price,
            "tp_target": tp_target,
        }
        return None

    def _maybe_time_stop(
        self,
        market: Dict[str, Any],
        slug: str,
        outcome_index: int,
        seconds_to_end: float,
    ) -> Optional[Dict[str, Any]]:
        """剩 ≤ LOWBUY_TIME_STOP_BEFORE_END 秒且未翻倍 → 强平."""
        if seconds_to_end > LOWBUY_TIME_STOP_BEFORE_END:
            return None

        key = f"{slug}:{outcome_index}"
        pos = self._open_positions.get(key)
        if not pos:
            return None

        outcomes = market.get("outcomes") or []
        if outcome_index >= len(outcomes):
            return None
        outcome = outcomes[outcome_index]
        bid = outcome.get("best_bid")
        if bid is None:
            return None
        bid = float(bid)

        return {
            "action": "TIME_STOP",
            "slug": slug,
            "outcome_index": outcome_index,
            "outcome_label": outcome.get("label", ""),
            "current_bid": bid,
            "current_ask": bid,
            "reason": (
                f"[LowBuy] 时间止损: 剩 {int(seconds_to_end)}s, entry={pos['entry_price']*100:.1f}¢ "
                f"bid={bid*100:.1f}¢ (未翻倍)"
            ),
            "close_reason_code": "late_time_stop",
            "close_reason_label": "尾盘离场",
            "confidence": 0.0,
            "position_entry_price": pos["entry_price"],
        }

    def _check_hard_stop(
        self,
        market: Dict[str, Any],
        slug: str,
        outcome_index: int,
        pos: Dict[str, Any],
    ) -> Optional[Dict[str, Any]]:
        outcomes = market.get("outcomes") or []
        if outcome_index >= len(outcomes):
            return None
        outcome = outcomes[outcome_index]
        bid = outcome.get("best_bid")
        if bid is None:
            return None
        try:
            bid = float(bid)
            entry = float(pos.get("entry_price", 0) or 0)
        except (TypeError, ValueError):
            return None
        if entry <= 0 or bid > entry * LOWBUY_HARD_STOP_MULT:
            return None
        return {
            "action": "TIME_STOP",
            "slug": slug,
            "outcome_index": outcome_index,
            "outcome_label": outcome.get("label", ""),
            "current_bid": bid,
            "current_ask": bid,
            "reason": (
                f"[LowBuy] 硬止损: entry={entry*100:.1f}¢ "
                f"bid={bid*100:.1f}¢ ≤ {LOWBUY_HARD_STOP_MULT:.0%} entry"
            ),
            "close_reason_code": "hard_stop",
            "close_reason_label": "硬止损",
            "confidence": 0.0,
            "position_entry_price": entry,
        }

    def _stop_at_time(
        self,
        market: Dict[str, Any],
        slug: str,
        outcome_index: int,
        pos: Dict[str, Any],
        seconds_to_end: float,
    ) -> Optional[Dict[str, Any]]:
        """剩余 5 分钟时还没翻倍 → 止损 (方向错了, 及时止损)."""
        outcomes = market.get("outcomes") or []
        if outcome_index >= len(outcomes):
            return None
        outcome = outcomes[outcome_index]
        bid = outcome.get("best_bid")
        if bid is None:
            return None
        bid = float(bid)

        entry = pos.get("entry_price", 0) or 0
        loss_pct = (entry - bid) / entry * 100 if entry > 0 else 0

        return {
            "action": "TIME_STOP",
            "slug": slug,
            "outcome_index": outcome_index,
            "outcome_label": outcome.get("label", ""),
            "current_bid": bid,
            "current_ask": bid,
            "reason": (
                f"[LowBuy] 5分钟止损: entry={entry*100:.1f}¢ "
                f"bid={bid*100:.1f}¢ ({'-' if loss_pct > 0 else '+'}{abs(loss_pct):.0f}%), "
                f"剩 {int(seconds_to_end)}s 未翻倍"
            ),
            "close_reason_code": "five_min_stop",
            "close_reason_label": "5分钟止损",
            "confidence": 0.0,
            "position_entry_price": entry,
        }

    # ----- 调试 -----

    def get_state_summary(self) -> Dict[str, Any]:
        return {
            "open_positions": list(self._open_positions.keys()),
            "open_count": len(self._open_positions),
            "last_scan": self._last_scan_summary,
        }
