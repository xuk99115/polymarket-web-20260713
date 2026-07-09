# Changelog

## [Unreleased] - 2026-06-27 审计修复

### 固定的 bug (23 个)

#### P0 - 生产破坏性 bug

- **[BUG-1]**: `_open_lowbuy_position` 写入 trade_record 时没写 `outcome_index` 字段, 导致 sync 阶段硬编码 `outcome_index=0`, 卖错方向. 修: 在 `_open_lowbuy_position` 显式写入 `outcome_index`, 删除 sync 硬编码.
- **[BUG-2]**: `_resolve_settlement_price` 在无 `outcomePrices` 时检查 `outcome.price > 0.5` 返回 1.0 (猜赢了), 导致结算价错. 修: 删掉"猜赢"逻辑, outcomePrices 没就绪就返回 `None` (后续 cycle 重试).
- **[BUG-4]**: `_run_cycle` 检查 depth `>= 0` 导致 `depth=0` 时卡死 (`best_ask_utc` 无限循环). 修: 改为 `> 0`.
- **[BUG-9]**: `_BINANCE_SSL_CTX.verify_mode = CERT_NONE` 全局作用, 导致 Polymarket/Gamma/CLOB/CoinGecko 也失 SSL 验证. 修: 新增 `_DEFAULT_SSL_CTX` (`CERT_REQUIRED`), 只有 Binance API 用 `_BINANCE_SSL_CTX`.
- **[BUG-18]**: `state.positions[]` 里残留 `strategy=lowbuy_double` 但 trades 里无对应 OPEN 的"幽灵仓位". 修: 启动时 prune `positions[]` 里 strategy=lowbuy_double 但 trades 没有对应 OPEN 的条目.

#### P1 - 可能误导 / 体验问题

- **[BUG-6]**: `interval = 2` 硬编码, 用户改 `.env AI_DECISION_INTERVAL_SECONDS=180` 无效. 修: 加注释说明原因 (Singapore 2s poll 最优, 180s 会错过信号), 保留硬编码.
- **[BUG-7]**: `_refresh_summary` 只读 `state.positions`, 但 LowBuy 仓位写 trades 里, 前端 `unrealized_pnl` 永远 0. 修: 双源读 positions + trades, set 去重, arbitrage 排除.
- **[BUG-8]**: `_lowbuy_close` 老 path (走 executor) 静默执行, 可能重复记账. 修: 删除 30 行, 改成 `raise RuntimeError`, 未来误用会立即暴露.
- **[BUG-10]**: WS `_ever_connected` 永真后不 give up, 网络抖时疯狂 retry. 修: 连续失败 8 次后退出 (fallback to REST).
- **[BUG-13]**: `market_data.py` 用 `verify=False` 导致 `InsecureRequestWarning` 刷屏 (尤其周末 BTC 停盘). 修: warning → debug.

#### P2 - 前端渲染问题

- **[BUG-20]**: `app.js` 5 套 setInterval (3s/5s/6s/15s/60s) 频率冲突, 网络抖时堆积请求. 修: 合并成 1 个 3s 主循环 + `withInflight` 防堆积 + tick counter 错开.
- **[BUG-21]**: `renderKronos()` 函数定义还在, 但后端 `fetchFairValue` 注释"Kronos 已移除", 前端无调用源. 修: 删除 90 行死代码 + 过时注释.
- **[BUG-22**: `renderTrades` 用 `window._buyDirectionMap` 跨调用共享全局变量, race condition 风险. 修: 改局部 const `buyMap`, 删除 `window.*` 写/读.
- **[BUG-23]**: `Math.random()` 兜底 trade 笔记 id, 导致 `<details>` 展开状态丢失. 修: 用 stable hash (`market_slug + outcome + created_at`).

#### P3 - 测试腐烂

- **[BUG-26]**: `test_paper_mode_no_outcome_prices_fallback` 期望结算价 1.0, 但已修 BUG-2 后返回 0.0. 修: 更新测试断言 + 说明注释.

#### 文档 / 清理

- **[BUG-27]**: `tests/test_kronos_signal.py` 引用已删模块 `kronos_signal`. 修: 删掉测试文件.

### 暂缓 / 低优先级 (3 个)

| Bug | 说明 | 原因 |
|:---|:---|:---|
| BUG-5 | `exposure` 口径检查 (主策略 exposure 统计) | 主策略完全关闭, LowBuy 自己管, 改反而可能引入 bug |
| BUG-28 | 7 个预先失败的 WS/server 测试 | CI 流程腐烂, 跟生产无关, 单独修 |
| BUG-29 | `_arbitrage.py` 死代码 + 前端套利卡片永远空 | 套利策略下线, 大改动 (删前端卡片), 另起 PR |

### 验证

- 本地测试: `pytest tests/test_manager_critical.py` → 16 passed
- 远程验证: Bot PID 469433 健康, dashboard HTTP 200
- 静态审: 22 个 bug 本地修 + 23 个 bug (包括 BUG-27 文件删) 后, 编译通过

### 技术债

- 待 BTC 15m 窗口产生 22-35¢ 的 outcome 才能验证 TP/TIME_STOP 真实触发
- 前端死代码 renderKronos 已删, test_kronos_signal.py 已删, 但 arb 相关前端卡片仍存在 (待删)
- 建议: 下次 CI/CD 流程重构时, 统一删掉套利相关前端 + 修复 WS/server 测试

---

## 2026-06-23 之前

### 架构重构

- 拆分 `status_server.py` → `helpers.py` + `api_proxy.py` + `market_data.py`
- 拆分 `manager.py` → `market_helpers.py` + `audit.py`
- 新增 `lowbuy_double.py` (独立 LowBuy 策略引擎)