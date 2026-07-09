import os

file_path = '/mnt/local/polymarket_web/src/trading/lowbuy_double.py'

# 1. 彻底用 Git HEAD 干净版本覆盖
os.system('cd /mnt/local/polymarket_web && git checkout HEAD -- src/trading/lowbuy_double.py')

with open(file_path, 'r', encoding='utf-8') as f:
    content = f.read()

# 2. 注入常数定义
target_const = 'LOWBUY_BOOST_OBI = -0.5           # \u2264-0.5: \u53e5\u76d8\u629b\u552e \u2192 \u9006\u5411\u5165\u52a0\u826f\u673a, \u63d0\u9ad8\u4fe1\u5fc3'
new_consts = """LOWBUY_BOOST_OBI = -0.5           # \u2264-0.5: \u53e5\u76d8\u629b\u552e \u2192 \u9006\u5411\u5165\u52a0\u826f\u673a, \u63d0\u9ad8\u4fe1\u5fc3

# \u652f\u6491\u4f4d\u4e0e\u963b\u529b\u4f4d (S/R) \u8fc7\u6ee4\u53c2\u6570 (2026-07-07 \u65b0\u589e)
LOWBUY_SR_WINDOWS = 4             # \u8fc7\u6ee4\u53c2\u6570\uff1a\u56de\u770b\u7684\u7a97\u53e3\u6570\u91cf
LOWBUY_SR_UP_SUPPORT_THRES = 0.25  # BTC\u5904\u4e8e\u652f\u6491\u533a (分位数<=0.25) \u65f6\uff0c\u4f18\u5148\u652f\u6301 UP \u5408\u7ea6
LOWBUY_SR_DOWN_RESIST_THRES = 0.75 # BTC\u5904\u4e8e\u963b\u529b\u533a (分位数>=0.75) \u65f6\uff0c\u4f18\u5148\u652f\u6301 DOWN \u5408\u7ea6"""

content = content.replace(target_const, new_consts)

# 3. 注入 _compute_sr_position 辅助计算方法到 _recent_ask_drop 前
target_helper = '    def _recent_ask_drop(self, slug: str, outcome_index: int, current_ask: float, now_utc: datetime) -> float:'
new_helper = """    def _compute_sr_position(self, slug: str, current_ask: float) -> Optional[float]:
        # \u901a\u8fc7\u5386\u53f2\u4ef7\u683c\u5e8f\u5217\u8ba1\u7b97\u5f53\u524d\u5904\u4e8e S/R \u7684\u5206\u4f4d\u6570\u4f4d\u7f6e (0.0=\u652f\u6491\u4f4d, 1.0=\u963b\u529b\u4f4d)
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

content = content.replace(target_helper, new_helper)

# 4. 在 _check_entries 的 OBI 过滤前注入具体的 S/R 校验逻辑
target_filter = '            # OBI \u8fc7\u6ee4: \u8ba2\u5355\u7c3f\u5931\u8861\u5ea6 (2026-06-26)'
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

content = content.replace(target_filter, new_filter)

# 写入目标文件
with open(file_path, 'w', encoding='utf-8') as f:
    f.write(content)

print("Patch applied successfully on clean checkout!")
