# Polymarket 交易终端 - 高阶交易策略蓝图 (Advanced Trading Strategies)

基于对当前代码库 (`reversal.py`, `lowbuy_double.py`, `_arbitrage.py`, `fair_value.py`) 的微观结构和逻辑审查，我们为您设计了以下四套**可无缝接入当前架构、具备数学期望值 (EV) 支撑**的高阶交易策略。

---

## 1. 挂单做市与流动性提供策略 (Maker / LP Strategy)

### 💡 背景与痛点
目前系统的所有策略（包括套利和动量）全部是 **Taker 模式**（吃单），即买入 `best_ask` 或卖出 `best_bid`。这意味着系统在频繁支付买卖价差（Spread）。在 Polygon 链上，Gas 极其便宜，但价差损耗是侵蚀 T&P 利润的头号杀手。

### 🛠️ 策略设计：双侧网格做市 (Grid Market Making)
利用现有的 `polymarket_ws.py` 实时盘口推送和 `fair_value.py` 的 Black-Scholes 公允价格估计，进行**被动做市**：
1. **公允价锚定**：通过 `fair_value.py` 实时计算 YES 合约的公允概率 $P_{fair}$（例如 0.55）。
2. **双侧挂单**：
   - 在 $P_{fair} - Spread\_Buffer$ 处挂 Limit Buy（例如 0.53 挂买）。
   - 在 $P_{fair} + Spread\_Buffer$ 处挂 Limit Sell（例如 0.57 挂卖）。
3. **动态撤改单**：利用 WebSocket 监听 BTC 现货价格。一旦 BTC 波动导致 $P_{fair}$ 偏离超过 0.01c，立即通过 `LiveTrader` 批量撤单并重新挂单（Polygon 延迟约 1-2 秒，完全可行）。
4. **收益来源**：吃掉买卖双方的价差利润，同时规避 Taker 损耗。

---

## 2. 多市场跨期与相关性套利 (Cross-Market Lag Arbitrage)

### 💡 背景与痛点
BTC 15m 盘口由于有高频量化资金和做市商守护，定价修正极快（延迟通常在 5-15 秒内）。单看一个盘口，常会出现“看得到、吃不到”的窘境。

### 🛠️ 策略设计：前导-滞后效应 (Lead-Lag Arbitrage)
Polymarket 上存在大量高相关性、但流动性较弱的**滞后市场**。
1. **信号源（前导）**：监控 BTC 15m 盘口的动量或 `reversal_engine` 突发信号。
2. **执行端（滞后）**：一旦 BTC 15m 发生暴涨/暴跌方向确认，**不交易 BTC 15m**，而是立即通过 Taker 扫货以下滞后盘口：
   - **ETH 15m / SOL 15m 盘口**：山寨币短线走势极度依赖 BTC，但其二元盘口的做市商反应通常慢 3-10 秒。
   - **BTC 30m / 1h / Daily 盘口**：中长线盘口对短线暴跌的定价反应更慢。
3. **优势**：利用主战场（BTC 15m）的确定性信号，收割次战场（ETH 15m 或长周期盘口）做市商的延迟。

---

## 3. 订单簿不平衡与微观结构流 (Order Book Imbalance & Toxic Flow)

### 💡 背景与痛点
`lowbuy_double.py` 策略仅判断 `ask <= 45c`，是一种被动的“捡便宜”策略。如果市场出现强烈的单边压制（有毒订单流 Toxic Flow），价格会一路阴跌归零，导致低吸变成“接飞刀”。

### 🛠️ 策略设计：OBI 过滤与大单跟踪
在入场条件中引入**订单簿失衡度 (Order Book Imbalance, OBI)** 指标：
1. **OBI 计算**：
   $$OBI = rac{\sum_{i=1}^3 Volume_{bid, i} - \sum_{i=1}^3 Volume_{ask, i}}{\sum_{i=1}^3 Volume_{bid, i} + \sum_{i=1}^3 Volume_{ask, i}}$$
   - OBI 的值在 $[-1, 1]$ 之间。
2. **过滤机制**：
   - 在 `LowBuy` 扫描时，若某 outcome 的 $Ask \le 45c$，但其盘口的 $OBI \le -0.7$（意味着上方挂了大量卖单，下方几乎无支撑买单），说明大资金在疯狂出逃。**此时坚决不入场**。
   - 反之，若 $Ask \le 45c$ 且 $OBI \ge 0.5$（卖单很薄，买单开始堆积），说明均值回归即将开始，**高信心入场**。
3. **大单跟踪 (Whale Chasing)**：通过 WS 捕获单笔金额超过 $500 USDC 的成交，直接顺着大单方向进行 30 秒的极短线剥头皮 (Scalping)。

---

## 4. 贝叶斯期望值决策集成 (Bayesian Expected Value Classifier)

### 💡 背景与痛点
当前 `manager.py` 采用的 `_build_btc_rule_signal` 评分机制（1m/3m/5m 动量打分累加）属于**硬编码启发式规则**（Heuristics），权重的设定（如 `up_score += 2`）带有主观性，未经过严格的期望值验证。

### 🛠️ 策略设计：正规化期望值决策
将硬编码打分升级为**概率分类器**：
1. **数据归档**：利用项目自带的 `history/` 目录累积的历史成交和价格数据，提取特征（1m/3m/5m 收益率、Vol、Z-score、Fair Value Edge）。
2. **逻辑回归模型 (Logistic Regression)**：在后台训练一个极简的概率预测模型：
   $$P(UP) = \sigma(w_0 + w_1 \cdot \Delta BTC_{1m} + w_2 \cdot Z\_score + w_3 \cdot Edge)$$
3. **期望值 (EV) 入场**：
   - 每次循环计算真实的数学期望：
     $$EV = P(UP) \cdot 1.0 - Price_{ask}$$
   - **硬性规则**：只有当 $EV > Margin\_of\_Safety$（例如 $EV > 0.03$）时，才触发 `BUY`。
4. **优势**：从“凭感觉打分”进化为“寻找数学上的正期望”。长期来看，只要 EV 持续为正，系统的大数定律将确保稳定盈利。
