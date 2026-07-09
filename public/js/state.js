/* ========= state.js: 核心状态管理 ========= */

export const dashboardState = {
    accountMode: 'paper',
    systemView: 'primary',
    tradingEnabled: true,
    togglePending: false,
    controlError: '',
    paperBalance: null,
    realBalance: null,
    config: null,
    aiHistory: [],
    positionCounts: { paper: 0, real: 0 },
    expandedPositionId: null,
    realPositions: [],   // 实盘持仓缓存
    realTrades: [],      // 实盘成交缓存
    instances: {
        primary: null,
        parallel: null,
    },
};

// 允许旧有脚本或控制台访问
window.dashboardState = dashboardState;

// 从 localStorage 初始化持久化状态
try {
    const savedMode = window.localStorage.getItem('polymarket_account_mode');
    if (savedMode === 'real' || savedMode === 'paper') {
        dashboardState.accountMode = savedMode;
    }
    const savedView = window.localStorage.getItem('polymarket_system_view');
    if (savedView === 'primary' || savedView === 'parallel') {
        dashboardState.systemView = savedView;
    }
} catch (e) {
    console.warn('LocalStorage initialization failed:', e);
}

export function getActiveAccountMode() {
    return dashboardState.accountMode === 'real' ? 'real' : 'paper';
}
window.getActiveAccountMode = getActiveAccountMode;

export function getActiveSystemView() {
    return dashboardState.systemView === 'parallel' ? 'parallel' : 'primary';
}
window.getActiveSystemView = getActiveSystemView;
