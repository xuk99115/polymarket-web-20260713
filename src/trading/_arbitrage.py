"""
二元盘口套利检测 + 减仓管理：
- 当两边定价差 >= ARB_TIERS 中某一档时，等份数同时买入两边，锁定价差利润。
- 当已有套利对子且 spread 收敛到 ARB_CLOSE_SPREAD 以下时，SELL 一边兑现利润。
"""
import logging
import uuid
from typing import Optional, Dict, Any, List, Tuple
from ..core.utils import safe_float

logger = logging.getLogger("arbitrage")

# === 入场阶梯（spread → stake 占比 + 单边最大金额）===
# 每档 (最小spread, 现金占比, 单边最大金额, 标签)
# 最小档 0.05 给主策略让路；最大档 0.18 保留原行为。
ARB_TIERS: List[Tuple[float, float, float, str]] = [
    (0.18, 0.45, 20.00, "强套利"),   # 原阈值：吃肉大窗口
    (0.12, 0.30, 12.00, "中套利"),   # 中等窗口
    (0.08, 0.18, 7.00,  "弱套利"),   # 小窗口
    (0.05, 0.10, 4.00,  "微套利"),   # 最小窗口，主策略让路
]

ARBITRAGE_MIN_SPREAD = ARB_TIERS[-1][0]    # 兼容旧逻辑：0.05
ARBITRAGE_MAX_ASK_GAP = 0.04
ARBITRAGE_CASH_FRACTION = 0.45             # 兼容旧逻辑：默认档位

# === 减仓阈值 ===
# 当已有套利对子且 spread 收敛到 ≤ 此值，SELL 一边（高价那侧）锁定利润。
# 必须 < ARB_TIERS[-1][0] (0.05)，给微套利档留出加仓后还没收敛的空间。
ARB_CLOSE_SPREAD = 0.03
# 减仓时如果单边浮动利润 ≥ 此值才动手（避免贴本平）
ARB_CLOSE_MIN_PROFIT = 0.02


def _pick_tier(spread: float) -> Optional[Tuple[float, float, float, str]]:
    """根据 spread 选档位。spread 越大匹配越激进的档（取第一个 ≥ spread 的）。
    加 1e-9 epsilon 容忍浮点误差。
    """
    for min_spread, cash_frac, max_stake, label in ARB_TIERS:
        if spread >= min_spread - 1e-9:
            return (min_spread, cash_frac, max_stake, label)
    return None


def check_arbitrage(market: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """检测盘口是否存在套利机会。

    Returns signal dict with action='ARBITRAGE' + both outcome prices + tier info,
    or None if no opportunity.
    """
    outcomes = market.get("outcomes", [])
    if len(outcomes) != 2:
        return None

    out0, out1 = outcomes[0], outcomes[1]

    # 取可成交价：best_ask 优先，其次 gamma price
    ask0 = safe_float(out0.get("best_ask"), out0.get("price"))
    ask1 = safe_float(out1.get("best_ask"), out1.get("price"))
    gamma0 = safe_float(out0.get("price"))
    gamma1 = safe_float(out1.get("price"))

    if ask0 is None or ask1 is None or gamma0 is None or gamma1 is None:
        return None

    # ask 偏离 gamma 过远说明流动性差
    if abs(ask0 - gamma0) > ARBITRAGE_MAX_ASK_GAP or abs(ask1 - gamma1) > ARBITRAGE_MAX_ASK_GAP:
        logger.debug("arbitrage: ask 偏离 gamma 过远, skip")
        return None

    spread = abs(ask0 - ask1)
    tier = _pick_tier(spread)
    if tier is None:
        return None

    # 总成本低于 $1 才锁定利润
    total_cost = ask0 + ask1
    if total_cost >= 0.99:
        logger.debug("arbitrage: 总成本 %.3f >= 0.99, 无利润空间", total_cost)
        return None

    # 双面各取 ask 价格
    if ask0 <= ask1:
        buy_side = {"outcome_index": 0, "ask": ask0, "label": out0.get("label", "Outcome0")}
        sell_side = {"outcome_index": 1, "ask": ask1, "label": out1.get("label", "Outcome1")}
        diff_label = f"{buy_side['label']}@{ask0:.2f} - {sell_side['label']}@{ask1:.2f}"
    else:
        buy_side = {"outcome_index": 1, "ask": ask1, "label": out1.get("label", "Outcome1")}
        sell_side = {"outcome_index": 0, "ask": ask0, "label": out0.get("label", "Outcome0")}
        diff_label = f"{sell_side['label']}@{ask0:.2f} - {buy_side['label']}@{ask1:.2f}"

    locked_profit = round(1.0 - total_cost, 4)
    min_spread, cash_frac, max_stake, tier_label = tier

    logger.info(
        "🔀 套利机会[%s]: spread %.3f 总成本 %.3f, 锁定 %.4f/份 (%s)",
        tier_label, spread, total_cost, locked_profit, diff_label,
    )

    return {
        "action": "ARBITRAGE",
        "buy_side": buy_side,
        "sell_side": sell_side,
        "total_cost": total_cost,
        "spread_at_entry": spread,
        "locked_profit_per_unit": locked_profit,
        "tier_label": tier_label,
        "cash_fraction": cash_frac,
        "max_stake_per_side": max_stake,
        "pair_id": uuid.uuid4().hex[:10],
    }


def should_close_arb_pair(market: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """检查套利对子是否该减仓（spread 收敛时 SELL 一边兑现）。

    减仓策略：
    - 当 spread ≤ ARB_CLOSE_SPREAD 时视为价差收敛（市场自动修正）
    - 此时按当前最佳 bid SELL 较贵那一边，保留便宜那一边到期 redeem
    - 这样：(a) 兑现一部分锁定利润，(b) 释放 cash，(c) 仍持有到期净赚 1-cost

    Returns dict with side_to_sell + reasoning, or None.
    """
    outcomes = market.get("outcomes", [])
    if len(outcomes) != 2:
        return None

    out0, out1 = outcomes[0], outcomes[1]
    bid0 = safe_float(out0.get("best_bid"), out0.get("price"))
    bid1 = safe_float(out1.get("best_bid"), out1.get("price"))
    ask0 = safe_float(out0.get("best_ask"), out0.get("price"))
    ask1 = safe_float(out1.get("best_ask"), out1.get("price"))

    if bid0 is None or bid1 is None or ask0 is None or ask1 is None:
        return None

    spread = abs(ask0 - ask1)
    # 加 1e-9 epsilon 避免浮点误差（0.51 - 0.48 = 0.030000000000000027）
    if spread > ARB_CLOSE_SPREAD + 1e-9:
        return None

    # 决定卖哪边：卖 ask 高（便宜于到期 redeem）的那一边
    # 假设两边持仓股数相等（等份数买入），SELL 高价那一边回笼更多现金
    if ask0 > ask1:
        sell_idx, sell_bid, sell_label = 0, bid0, out0.get("label", "Outcome0")
    else:
        sell_idx, sell_bid, sell_label = 1, bid1, out1.get("label", "Outcome1")

    return {
        "action": "CLOSE_ARB_SIDE",
        "outcome_index": sell_idx,
        "exit_price": sell_bid,
        "exit_label": sell_label,
        "current_spread": spread,
        "reason": f"价差收敛至 {spread:.3f} ≤ {ARB_CLOSE_SPREAD:.2f}，兑现 {sell_label} 边",
    }


def arb_pair_status(positions: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """聚合所有套利对子的当前状态，给前端展示用。

    按 pair_id 分组，统计：(已锁利润 / 未锁利润 / 总持仓 / 当前 spread)。
    """
    grouped: Dict[str, List[Dict[str, Any]]] = {}
    for p in positions:
        pid = p.get("arbitrage_pair_id")
        if not pid:
            continue
        grouped.setdefault(pid, []).append(p)

    pairs = []
    for pid, plist in grouped.items():
        total_stake = sum(safe_float(p.get("stake"), 0) or 0 for p in plist)
        total_shares = sum(safe_float(p.get("shares"), 0) or 0 for p in plist)
        current_value = sum(safe_float(p.get("current_value"), 0) or 0 for p in plist)
        # 锁定利润 = 1 - 总成本（成本 = stake 总和），兑现利润 = current_value - stake
        locked_per_unit = 1.0 - sum(
            (safe_float(p.get("entry_price"), 0) or 0) * (safe_float(p.get("shares"), 0) or 0)
            for p in plist
        ) / total_shares if total_shares > 0 else 0
        locked = locked_per_unit * total_shares
        realized = current_value - total_stake
        pairs.append({
            "pair_id": pid,
            "market_slug": plist[0].get("market_slug", ""),
            "market_title": plist[0].get("market_title") or plist[0].get("market", ""),
            "outcomes": [p.get("outcome", "") for p in plist],
            "sides_count": len(plist),
            "total_stake": round(total_stake, 4),
            "total_shares": round(total_shares, 4),
            "current_value": round(current_value, 4),
            "locked_profit": round(locked, 4),
            "realized_pnl": round(realized, 4),
            "status": "open",
        })

    return pairs


