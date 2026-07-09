import os
import shutil

file_path = '/mnt/local/polymarket_web/src/trading/lowbuy_double.py'

# 1. 彻底将文件从 Git 还原，获取没有任何污染的最纯净版本
os.system('rm -f /mnt/local/polymarket_web/.git/index.lock')
os.system('cd /mnt/local/polymarket_web && git checkout HEAD -- src/trading/lowbuy_double.py')

with open(file_path, 'r', encoding='utf-8') as f:
    content = f.read()

# 2. 注入常量
target_const = 'LOWBUY_BOOST_OBI = -0.5           # ≤-0.5: 卖盘抛售 → 逆向入场良机, 提高信心'
new_consts = """LOWBUY_BOOST_OBI = -0.5           # ≤-0.5: 卖盘抛售 → 逆向入场良机, 提高信心

# 支撑位与阻力位 (S/R) 过滤参数 (2026-07-07 新增)
LOWBUY_SR_WINDOWS = 4             # 过滤参数：回看窗口数量
LOWBUY_SR_UP_SUPPORT_THRES = 0.25  # BTC处于支撑区 (分位数<=0.25) 时，优先支持 UP 合约
LOWBUY_SR_DOWN_RESIST_THRES = 0.75 # BTC处于阻力区 (分位数>=0.75) 时，优先支持 DOWN 合约"""

if target_const in content:
    content = content.replace(target_const, new_consts)
    print("Constants replaced.")
else:
    print("Warning: Constants pattern not found!")

# 3. 注入 _compute_sr_position 辅助方法
target_helper = '    def _recent_ask_drop(self, slug: str, outcome_index: int, current_ask: float, now_utc: datetime) -> float:'
new_helper = """    def _compute_sr_position(self, slug: str, current_ask: float) -> Optional[float]:
        # 通过历史价格序列计算当前处于 S/R 的分位数位置 (0.0=支撑位, 1.0=阻力位)
        key_0 = f"{slug}:0"
        key_1 = f"{slug}:1"
        hist_0 = self._quote_history.get(key_0, [])
        hist_1 = self._quote_history.get(key_1, [])
        
        prices = []
        for h in hist_0:
            if h.get('ask') and h.get('bid'):
                prices.append((float(h['ask']) + float(h['bid'])) / 2)
        for h in hist_1:
            if h.get('ask') and h.get('bid'):
                prices.append(1.0 - (float(h['ask']) + float(h['bid'])) / 2)
                
        if len(prices) < 8:
            return None
            
        low = min(prices)
        high = max(prices)
        if high <= low:
            return 0.5
            
        pos = (current_ask - low) / (high - low)
        return min(1.0, max(0.0, pos))

    def _recent_ask_drop(self, slug: str, outcome_index: int, current_ask: float, now_utc: datetime) -> float:"""

if target_helper in content:
    content = content.replace(target_helper, new_helper)
    print("Helper injected.")
else:
    print("Warning: Helper pattern not found!")

# 4. 注入入场过滤逻辑
target_filter = '            # OBI 过滤: 订单簿失衡度 (2026-06-26)'
new_filter = """            # S/R 支撑阻力方向过滤 (2026-07-07 新增)
            sr_pos = self._compute_sr_position(slug, ask)
            if sr_pos is not None:
                if idx == 0 and sr_pos > LOWBUY_SR_DOWN_RESIST_THRES:
                    logger.info("[LowBuy/SR] 跳过 Up: S/R位置=%.2f > %.2f (BTC在阻力区，不买涨)", sr_pos, LOWBUY_SR_DOWN_RESIST_THRES)
                    continue
                if idx == 1 and sr_pos < LOWBUY_SR_UP_SUPPORT_THRES:
                    logger.info("[LowBuy/SR] 跳过 Down: S/R位置=%.2f < %.2f (BTC在支撑区，不买跌)", sr_pos, LOWBUY_SR_UP_SUPPORT_THRES)
                    continue

            # OBI 过滤: 订单簿失衡度 (2026-06-26)"""

if target_filter in content:
    content = content.replace(target_filter, new_filter)
    print("Filter logic injected.")
else:
    print("Warning: Filter pattern not found!")

with open(file_path, 'w', encoding='utf-8') as f:
    f.write(content)

print("Patching complete!")
