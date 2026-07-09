/* ========= app.js: 应用引导与主循环 ========= */
import { refreshAll, fetchOrderBook, fetchBotStatus, fetchBtcTrend, fetchConfig, fetchInstanceDashboard } from './api.js?v=20260707c';
import { initSettings, renderAccountMode, renderTradingControl } from './ui.js?v=20260707c';

const ORDERBOOK_REFRESH_MS = 2000;
const BTC_TREND_REFRESH_MS = 6000;

// ResizeObserver: 窗口/侧边栏变化时重画 sparkline
(function() {
    if (typeof ResizeObserver === 'undefined') return;
    document.addEventListener('DOMContentLoaded', () => {
        const canvas = document.getElementById('btc-sparkline');
        if (!canvas) return;
        new ResizeObserver(() => {
            // 触发重画（最近一次拿到的 history 在 api.js 里是局部的，所以重新拉一次）
            if (typeof window.fetchBtcTrend === 'function') {
                window.fetchBtcTrend();
            }
        }).observe(canvas);
    });
})();

// Bug fix 2026-06-27: 用 inflight 标志防止慢响应时多次请求堆积.
// 之前 5 个独立 setInterval (3s/5s/6s/15s/60s), 网络抖动时 fetchOrderBook
// 服务端 timeout 5s 跟 interval 同频, 必堆积. 每个 fetch 加 _inflight 标志,
// 上次未完成则跳过本次, 不会雪崩.
function withInflight(fn) {
    if (fn._inflight) return;
    fn._inflight = true;
    const p = fn();
    Promise.resolve(p).finally(() => { fn._inflight = false; });
}

function refreshPaperDashboards() {
    if (typeof window.fetchInstanceDashboard !== 'function') return;
    Promise.allSettled([
        fetchInstanceDashboard('primary'),
        fetchInstanceDashboard('parallel'),
    ]);
}

document.addEventListener('DOMContentLoaded', () => {
    console.log('🚀 Polymarket 交易终端已启动 (模块化架构)');

    // 初始化 UI 状态
    renderAccountMode();
    renderTradingControl();
    initSettings();

    // 初始首屏数据加载
    refreshAll();
    fetchConfig();

    setInterval(() => {
        if (window.getActiveAccountMode && window.getActiveAccountMode() === 'paper') {
            refreshPaperDashboards();
            return;
        }
        withInflight(fetchOrderBook);
    }, ORDERBOOK_REFRESH_MS);

    setInterval(() => {
        withInflight(fetchBtcTrend);
    }, BTC_TREND_REFRESH_MS);

    // Bug fix 2026-06-27: 多套 setInterval 合并为 1 个主循环, 内部错开调用时间,
    // 加 withInflight 防堆积. 盘口和 BTC 趋势已拆到专用快速定时器;
    // 这里保留状态/成交/资金/配置等较低频数据。
    // 用 tickCounter 计数器实现错开, 不依赖 setTimeout 嵌套 (那个容易被 tab 后台暂停打断).
    let tickCounter = 0;
    setInterval(() => {
        tickCounter += 1;

        if (window.getActiveAccountMode && window.getActiveAccountMode() === 'paper') {
            refreshPaperDashboards();
            return;
        }

        // 每 tick 必做: 高频核心 (3s)
        if (window.fetchOrders) withInflight(window.fetchOrders);
        if (window.fetchArbStatus) withInflight(window.fetchArbStatus);
        withInflight(fetchBotStatus);

        // 每 5 ticks (≈15s): 低频成交/资金
        if (tickCounter % 5 === 0) {
            if (window.fetchTrades) withInflight(window.fetchTrades);
            if (window.fetchBalance) withInflight(window.fetchBalance);
            if (window.fetchRealBalance) withInflight(window.fetchRealBalance);
        }

        // 每 20 ticks (≈60s): 极低频配置/BTC价/AI历史
        if (tickCounter % 20 === 0) {
            if (window.fetchConfig) withInflight(window.fetchConfig);
            if (window.fetchBtc) withInflight(window.fetchBtc);
            if (window.fetchAiHistory) withInflight(window.fetchAiHistory);
        }
    }, 3000);
});
