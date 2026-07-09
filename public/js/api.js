/* ========= api.js: 数据通信逻辑 ========= */
import { dashboardState, getActiveAccountMode } from './state.js';
import { shortTime, formatUSD } from './utils.js';
import { 
    setOffline, renderTradingControl, renderAccountMode, 
    renderConfig, renderAiHistory, renderTrades, 
    renderPositions, renderCapitalPanel, renderOrderBook,
    renderPaperPerformance, renderRealBalance, renderSystemWorkspace
} from './ui.js';

// ========= Web UI 鉴权: SSH 隧道访问无需手填；公网/API 客户端可用 X-Api-Key =========
const WEB_TOKEN_KEY = 'web_token';
const API_BASE = '';
const _origFetch = window.fetch.bind(window);
let _webTokenPromise = null;

async function ensureWebToken() {
    if (getWebToken()) return getWebToken();
    if (_webTokenPromise) return _webTokenPromise;
    _webTokenPromise = (async () => {
        try {
            const resp = await _origFetch('/api/web-token?ts=' + Date.now(), { cache: 'no-store' });
            if (!resp.ok) return null;
            const data = await resp.json();
            if (data && data.token) {
                setWebToken(data.token);
                return data.token;
            }
        } catch (_) {
            return null;
        } finally {
            _webTokenPromise = null;
        }
        return null;
    })();
    return _webTokenPromise;
}

window.fetch = function(input, init = {}) {
    // 绕过代理：相对路径 /api/* 改为 127.0.0.1 直连
    if (typeof input === 'string' && (input.startsWith('/api/') || input.startsWith('/status-json'))) {
        input = API_BASE + input;
    } else if (typeof input === 'object' && input?.url && (input.url.startsWith('/api/') || input.url.startsWith('/status-json'))) {
        input = new Request(API_BASE + input.url, input);
    }
    const isTokenBootstrap = typeof input === 'string' && input.startsWith('/api/web-token');
    return (async () => {
        if (!isTokenBootstrap && (typeof input === 'string' ? input.startsWith('/api/') || input.startsWith('/status-json') : true)) {
            await ensureWebToken();
        }
        const token = getWebToken();
        if (token) {
            const headers = new Headers(init.headers || {});
            headers.set('X-Api-Key', token);
            init = { ...init, headers };
        }
        return _origFetch(input, init);
    })();
};

export function setWebToken(token) {
    if (token) {
        // 去不可见字符 (空格/换行/零宽空格)
        const clean = String(token).replace(/[\s\u200B-\u200D\uFEFF]/g, '');
        if (clean) sessionStorage.setItem(WEB_TOKEN_KEY, clean);
        else sessionStorage.removeItem(WEB_TOKEN_KEY);
    } else {
        sessionStorage.removeItem(WEB_TOKEN_KEY);
    }
}
export function getWebToken() {
    const v = sessionStorage.getItem(WEB_TOKEN_KEY);
    return v ? v.replace(/[\s\u200B-\u200D\uFEFF]/g, '') : null;
}
window.setWebToken = setWebToken;
window.getWebToken = getWebToken;
window.ensureWebToken = ensureWebToken;

export async function fetchBtc() {
    try {
        const resp = await fetch('/api/btc?ts=' + Date.now(), { cache: 'no-store' });
        const data = await resp.json();
        renderBtcPrice(data);
    } catch (e) {
        const priceEl = document.getElementById('btc-price');
        if (priceEl) priceEl.textContent = '离线';
    }
}
window.fetchBtc = fetchBtc;

function renderBtcPrice(data) {
    const priceEl = document.getElementById('btc-price');
    const changeEl = document.getElementById('btc-change');
    if (!priceEl || !changeEl) return;

    if (!data || data.error) {
        priceEl.textContent = '错误';
        changeEl.textContent = data?.error || 'BTC 数据不可用';
        return;
    }

    priceEl.textContent = formatUSD(data.price);
    const ch = Number(data.change_24h);
    changeEl.textContent = (ch > 0 ? '+' : '') + ch.toFixed(2) + '% (24h)';
    changeEl.className = 'metric-sub ' + (ch > 0 ? 'c-green' : ch < 0 ? 'c-red' : '');
}

function _colorizePct(value) {
    if (value == null || isNaN(value)) return 'c-mute';
    if (value > 0.005) return 'c-green';
    if (value < -0.005) return 'c-red';
    return 'c-mute';
}

function _fmtPct(value) {
    if (value == null || isNaN(value)) return '--';
    const sign = value > 0 ? '+' : '';
    return sign + value.toFixed(3) + '%';
}

function drawSparkline(canvas, prices) {
    if (!canvas || !prices || prices.length < 2) return;
    const dpr = window.devicePixelRatio || 1;
    const cssW = canvas.clientWidth || 200;
    const cssH = 40;
    canvas.width = cssW * dpr;
    canvas.height = cssH * dpr;
    canvas.style.height = cssH + 'px';
    const ctx = canvas.getContext('2d');
    ctx.setTransform(1, 0, 0, 1, 0, 0);
    ctx.scale(dpr, dpr);
    ctx.clearRect(0, 0, cssW, cssH);

    const min = Math.min(...prices);
    const max = Math.max(...prices);
    const range = max - min || 1;
    const last = prices[prices.length - 1];
    const first = prices[0];
    const trendUp = last >= first;
    const lineColor = trendUp ? '#5fd99a' : '#ff7a8e';
    const fillTop = trendUp ? 'rgba(95, 217, 154, 0.32)' : 'rgba(255, 122, 142, 0.32)';
    const fillBot = trendUp ? 'rgba(95, 217, 154, 0)' : 'rgba(255, 122, 142, 0)';

    const yFor = (p) => cssH - ((p - min) / range) * (cssH - 6) - 3;

    // 填充区
    ctx.beginPath();
    prices.forEach((p, i) => {
        const x = (i / (prices.length - 1)) * cssW;
        const y = yFor(p);
        if (i === 0) ctx.moveTo(x, y);
        else ctx.lineTo(x, y);
    });
    ctx.lineTo(cssW, cssH);
    ctx.lineTo(0, cssH);
    ctx.closePath();
    const grad = ctx.createLinearGradient(0, 0, 0, cssH);
    grad.addColorStop(0, fillTop);
    grad.addColorStop(1, fillBot);
    ctx.fillStyle = grad;
    ctx.fill();

    // 折线
    ctx.beginPath();
    prices.forEach((p, i) => {
        const x = (i / (prices.length - 1)) * cssW;
        const y = yFor(p);
        if (i === 0) ctx.moveTo(x, y);
        else ctx.lineTo(x, y);
    });
    ctx.strokeStyle = lineColor;
    ctx.lineWidth = 1.5;
    ctx.lineJoin = 'round';
    ctx.lineCap = 'round';
    ctx.stroke();

    // 当前点高亮
    const lastX = cssW - 1;
    const lastY = yFor(last);
    ctx.beginPath();
    ctx.arc(lastX, lastY, 3, 0, 2 * Math.PI);
    ctx.fillStyle = lineColor;
    ctx.fill();
    ctx.beginPath();
    ctx.arc(lastX, lastY, 5, 0, 2 * Math.PI);
    ctx.strokeStyle = lineColor + '55';
    ctx.lineWidth = 1;
    ctx.stroke();
}

export async function fetchBtcTrend() {
    try {
        const resp = await fetch('/api/btc-trend?ts=' + Date.now(), { cache: 'no-store' });
        const data = await resp.json();
        if (data.error) {
            // bot 还没生成 snapshot，使用 fallback（已是上次 /api/btc 的 price + 24h）
            return;
        }

        renderBtcPrice(data);

        const set = (id, val) => {
            const el = document.getElementById(id);
            if (!el) return;
            el.textContent = _fmtPct(val);
            el.className = 'btc-trend-val mono ' + _colorizePct(val);
        };
        set('btc-1m', data.change_1m);
        set('btc-3m', data.change_3m);
        set('btc-5m', data.change_5m);
        set('btc-15m', data.change_15m);

        const lowEl = document.getElementById('btc-range-low');
        const highEl = document.getElementById('btc-range-high');
        const posEl = document.getElementById('btc-range-pos');
        if (lowEl) lowEl.textContent = data.range_low_15m ? '$' + data.range_low_15m.toLocaleString() : '--';
        if (highEl) highEl.textContent = data.range_high_15m ? '$' + data.range_high_15m.toLocaleString() : '--';
        if (posEl) {
            const pos = data.range_position_15m;
            posEl.textContent = pos != null ? (pos * 100).toFixed(0) + '%' : '--';
        }

        // sparkline: 画 BTC 价格 5 分钟走势
        if (Array.isArray(data.history) && data.history.length >= 2) {
            const canvas = document.getElementById('btc-sparkline');
            const prices = data.history.map(h => h.price).filter(p => p != null);
            if (canvas && prices.length >= 2) {
                // 延迟一帧让 DOM 布局生效，避免 canvas.clientWidth=0
                requestAnimationFrame(() => drawSparkline(canvas, prices));
            }
        }

        // Fair Value 模型渲染（log-normal price model + 15m σ）
        renderFairValue(data);
        // Bug fix 2026-06-27: 删除 "Kronos 已移除" 注释 — renderKronos 整个函数已删, 注释失去意义
    } catch (e) {
        // silent
    }
}
window.fetchBtcTrend = fetchBtcTrend;

function renderFairValue(data) {
    const setText = (id, val) => {
        const el = document.getElementById(id);
        if (el) el.textContent = val;
    };

    const fairUp = data.fair_up;
    const fairDown = data.fair_down;
    const z = data.fair_z_score;
    const sigma = data.sigma_15m;
    const refPx = data.ref_px;
    const edgeBps = data.fair_edge_bps;

    // Fair UP/DOWN (×100 变百分比)
    setText('fair-up', fairUp != null ? (fairUp * 100).toFixed(2) + '%' : '--');
    setText('fair-down', fairDown != null ? (fairDown * 100).toFixed(2) + '%' : '--');

    // Z-score (带正负颜色)
    const zEl = document.getElementById('fair-z');
    if (zEl) {
        if (z == null) {
            zEl.textContent = '--';
            zEl.className = 'fair-value mono';
        } else {
            const sign = z >= 0 ? '+' : '';
            zEl.textContent = sign + z.toFixed(3);
            zEl.className = 'fair-value mono ' + (z > 0.1 ? 'c-green' : z < -0.1 ? 'c-red' : '');
        }
    }

    // Sigma
    setText('fair-sigma', sigma != null ? (sigma * 100).toFixed(3) + '%' : '--');

    // Ref Price
    setText('fair-ref', refPx != null ? '$' + Number(refPx).toLocaleString(undefined, { maximumFractionDigits: 2 }) : '--');

    // Edge bps — 核心指标
    const edgeEl = document.getElementById('fair-edge');
    const edgeSubEl = document.getElementById('fair-edge-sub');
    const badgeEl = document.getElementById('fair-direction-badge');
    if (edgeEl) {
        if (edgeBps == null) {
            edgeEl.textContent = '--';
            edgeEl.className = 'fair-value mono';
            if (edgeSubEl) edgeSubEl.textContent = '需要市场 UP 价格才能计算';
        } else {
            const sign = edgeBps >= 0 ? '+' : '';
            edgeEl.textContent = sign + edgeBps.toFixed(1) + ' bps';
            edgeEl.className = 'fair-value mono ' + (edgeBps > 50 ? 'c-green' : edgeBps < -50 ? 'c-red' : '');
            if (edgeSubEl) {
                if (Math.abs(edgeBps) < 25) edgeSubEl.textContent = '≈ 公平定价';
                else if (edgeBps > 0) edgeSubEl.textContent = `市场低估 UP ${(edgeBps / 100).toFixed(2)}¢`;
                else edgeSubEl.textContent = `市场低估 DOWN ${(Math.abs(edgeBps) / 100).toFixed(2)}¢`;
            }
            if (badgeEl) {
                if (Math.abs(edgeBps) < 25) badgeEl.textContent = '中性';
                else if (edgeBps > 0) badgeEl.textContent = '↑ UP 优势';
                else badgeEl.textContent = '↓ DOWN 优势';
            }
        }
    }

    // Fair UP/DOWN 横向条形图
    const upPct = fairUp != null ? (fairUp * 100) : 50;
    const downPct = fairDown != null ? (fairDown * 100) : 50;
    const upBar = document.getElementById('fair-bar-up');
    const downBar = document.getElementById('fair-bar-down');
    if (upBar) upBar.style.width = upPct.toFixed(2) + '%';
    if (downBar) downBar.style.width = downPct.toFixed(2) + '%';
    setText('fair-bar-up-label', upPct.toFixed(1) + '%');
    setText('fair-bar-down-label', downPct.toFixed(1) + '%');

    // FV 训练状态（只展示，不影响交易）
    const train = data.fv_training || {};
    const latestRef = train.latest_ref_px != null ? train.latest_ref_px : refPx;
    setText('fv-train-enabled', train.enabled ? 'ON' : '--');
    setText('fv-lowbuy-filter', train.lowbuy_filter_enabled ? 'ON' : 'OFF');
    setText('fv-samples', train.prediction_samples != null ? String(train.prediction_samples) : '--');
    setText('fv-refs', train.window_ref_count != null ? String(train.window_ref_count) : '--');
    setText('fv-current-ref', latestRef != null ? '$' + Number(latestRef).toLocaleString(undefined, { maximumFractionDigits: 2 }) : '--');
    setText('fv-late-ref', train.late_ref == null ? '--' : (train.late_ref ? 'true' : 'false'));
}

// Bug fix 2026-06-27: 删除 renderKronos 死代码 (~90 行).
// 原因: 后端已禁用 Kronos (manager.py:1411, btc_monitor 不再发 kronos_* 字段),
// fetchBtcTrend 注释 line 192 也说 "Kronos 已移除". 函数没人调用, 留着只是占空间
// + 误导后来者以为 Kronos 还在用. 删掉后 server 不再发 kronos_*, 前端也不消费.


export async function fetchControl() {
    try {
        const resp = await fetch('/api/control?ts=' + Date.now(), { cache: 'no-store' });
        const data = await resp.json();
        dashboardState.tradingEnabled = data.trading_enabled !== false;
        dashboardState.controlError = '';
    } catch (e) {
        dashboardState.controlError = '交易控制状态读取失败';
    }
    renderTradingControl();
    renderConfig();
    renderSystemWorkspace();
}
window.fetchControl = fetchControl;

export async function fetchInstanceDashboard(instance = 'primary') {
    try {
        const mode = getActiveAccountMode();
        if (mode !== 'paper') return;
        const resp = await fetch(`/api/instance-dashboard?instance=${instance}&account=paper&ts=` + Date.now(), { cache: 'no-store' });
        const data = await resp.json();
        dashboardState.instances[instance] = data && !data.error ? data : null;
        renderSystemWorkspace();
    } catch (e) {
        dashboardState.instances[instance] = null;
        renderSystemWorkspace();
    }
}
window.fetchInstanceDashboard = fetchInstanceDashboard;

export async function fetchBotStatus() {
    if (getActiveAccountMode() === 'paper') {
        renderSystemWorkspace();
        return;
    }
    try {
        const resp = await fetch('/status-json?ts=' + Date.now(), { cache: 'no-store' });
        const data = await resp.json();
        if (!data || Object.keys(data).length === 0) {
            setOffline();
            return;
        }

        if (data.trading_enabled !== undefined) {
            dashboardState.tradingEnabled = data.trading_enabled !== false;
            if (!dashboardState.togglePending) dashboardState.controlError = '';
            renderTradingControl();
        }

        const dot = document.getElementById('status-dot');
        const label = document.getElementById('status-label');
        if (dot && label) {
            if (data.running) {
                dot.className = 'status-dot online';
                label.textContent = dashboardState.tradingEnabled ? '机器人运行中 · 交易开启' : '机器人运行中 · 交易关闭';
            } else {
                dot.className = 'status-dot offline';
                label.textContent = 'Bot 已停止';
            }
        }

        const action = String(data.ai_action || data.ai_prediction || 'SKIP').toUpperCase();
        const chosenLabel = String(data.ai_outcome_label || '').toUpperCase();
        const predEl = document.getElementById('ai-prediction');
        const aiLabelEl = document.getElementById('ai-label');
        
        if (predEl) {
            predEl.textContent = action === 'BUY'
                ? (chosenLabel ? `买 ${chosenLabel}` : 'AI 买入')
                : 'AI 观望';
            predEl.className = 'metric-value ' + (action === 'BUY' ? 'c-green' : 'c-amber');
        }
        
        if (aiLabelEl) {
            if (data.market_error) {
                aiLabelEl.textContent = data.market_error;
            } else if (data.market_question) {
                const suffix = data.market_end_date ? ` · 到期 ${shortTime(data.market_end_date)}` : '';
                aiLabelEl.textContent = `聚焦盘口：${data.market_question}${suffix}`;
            } else {
                aiLabelEl.textContent = '等待目标市场';
            }
        }

        const timeEl = document.getElementById('update-time');
        if (data.last_update && timeEl) {
            timeEl.textContent = shortTime(data.last_update);
        }
        
        renderCapitalPanel(data);
        // 实盘模式下用真实余额重绘资金面板
        if (getActiveAccountMode() === 'real' && dashboardState.realBalance) {
            renderCapitalPanel(data);
        }
    } catch (e) {
        setOffline();
    }
}
window.fetchBotStatus = fetchBotStatus;

export async function fetchParallelStatus() {
    if (getActiveAccountMode() === 'paper') {
        renderSystemWorkspace();
        return;
    }
    const setText = (id, value) => {
        const el = document.getElementById(id);
        if (el) el.textContent = value;
    };
    try {
        const resp = await fetch('/api/parallel-status?ts=' + Date.now(), { cache: 'no-store' });
        const data = await resp.json();
        const badge = document.getElementById('parallel-badge');
        if (!data || data.enabled === false) {
            if (badge) badge.textContent = '未接入';
            setText('parallel-running', '--');
            setText('parallel-mode', '--');
            setText('parallel-market', '--');
            setText('parallel-summary', data?.error || '--');
            return;
        }
        if (badge) badge.textContent = data.running ? '在线' : '离线';
        setText('parallel-running', data.running ? 'RUNNING' : 'STOPPED');
        setText('parallel-mode', data.trading_mode || '--');
        setText('parallel-market', data.market_slug || data.market_question || '--');
        setText('parallel-summary', data.execution_summary || data.market_error || '--');
    } catch (e) {
        const badge = document.getElementById('parallel-badge');
        if (badge) badge.textContent = '异常';
        setText('parallel-running', '--');
        setText('parallel-mode', '--');
        setText('parallel-market', '--');
        setText('parallel-summary', '读取失败');
    }
}
window.fetchParallelStatus = fetchParallelStatus;

export async function fetchAiHistory() {
    try {
        const resp = await fetch('/api/ai-decisions?ts=' + Date.now(), { cache: 'no-store' });
        const data = await resp.json();
        dashboardState.aiHistory = Array.isArray(data) ? data : [];
        renderAiHistory();
    } catch (e) {
        const list = document.getElementById('ai-history-list');
        if (list) list.innerHTML = '<div class="empty-row">AI 决策历史读取失败</div>';
    }
}
window.fetchAiHistory = fetchAiHistory;

export async function fetchBalance() {
    if (getActiveAccountMode() === 'paper') {
        renderSystemWorkspace();
        return;
    }
    try {
        const resp = await fetch('/api/balance?ts=' + Date.now(), { cache: 'no-store' });
        const data = await resp.json();
        if (!data.error) {
            let balance = null;
            if (typeof data === 'number') balance = data;
            else if (data.balance !== undefined) balance = Number(data.balance);
            
            if (balance !== null && !isNaN(balance)) {
                dashboardState.paperBalance = data;
                renderPaperPerformance();
                renderConfig();
                return;
            }
        }
    } catch (e) {
        console.warn('Paper balance fetch failed, using fallback.');
    }
}
window.fetchBalance = fetchBalance;

export async function fetchRealBalance() {
    try {
        const resp = await fetch('/api/real-balance?ts=' + Date.now(), { cache: 'no-store' });
        const data = await resp.json();
        if (!data.error) {
            dashboardState.realBalance = data;
            renderConfig();
            renderRealBalance();
            // 更新资金面板中的实盘余额
            if (getActiveAccountMode() === 'real') renderCapitalPanel({});
        }
    } catch (e) {
        console.warn('Real balance fetch failed.');
    }
}
window.fetchRealBalance = fetchRealBalance;

export async function fetchTrades() {
    if (getActiveAccountMode() === 'paper') {
        renderSystemWorkspace();
        return;
    }
    try {
        const mode = getActiveAccountMode();
        const resp = await fetch(`/api/trades?account=${mode}&ts=` + Date.now(), { cache: 'no-store' });
        const data = await resp.json();
        const trades = Array.isArray(data) ? data : (data.data || []);
        if (mode === 'real') {
            dashboardState.realTrades = trades;
            renderCapitalPanel({});  // 有了成交数据后更新统计
        }
        renderTrades(trades);
    } catch (e) {
        renderTrades([]);
    }
}
window.fetchTrades = fetchTrades;

export async function fetchOrders() {
    if (getActiveAccountMode() === 'paper') {
        renderSystemWorkspace();
        return;
    }
    try {
        const mode = getActiveAccountMode();
        const resp = await fetch(`/api/positions?account=${mode}&ts=` + Date.now(), { cache: 'no-store' });
        const data = await resp.json();
        const positions = Array.isArray(data) ? data : (data.data || []);
        if (mode === 'real') {
            dashboardState.realPositions = positions;
            renderCapitalPanel({});  // 有了持仓数据后更新仓位占用
        }
        renderPositions(positions);
    } catch (e) {
        renderPositions([]);
    }
}
window.fetchOrders = fetchOrders;

export async function fetchArbStatus() {
    try {
        const resp = await fetch('/api/arb-status?ts=' + Date.now(), { cache: 'no-store' });
        const data = await resp.json();
        renderArbPairs(data);
        dashboardState.arbStatus = data;
    } catch (e) {
        renderArbPairs({ pairs: [], tiers: [], summary: {} });
    }
}
window.fetchArbStatus = fetchArbStatus;

export async function fetchOrderBook() {
    if (getActiveAccountMode() === 'paper') {
        renderSystemWorkspace();
        return;
    }
    try {
        const resp = await fetch('/api/orderbook?ts=' + Date.now(), { cache: 'no-store' });
        const data = await resp.json();
        renderOrderBook(data);
    } catch (e) {
        renderOrderBook({});
    }
}
window.fetchOrderBook = fetchOrderBook;

export async function fetchConfig() {
    if (getActiveAccountMode() === 'paper') {
        renderSystemWorkspace();
        return;
    }
    try {
        const resp = await fetch('/api/config?ts=' + Date.now(), { cache: 'no-store' });
        const data = await resp.json();
        dashboardState.config = data;
        renderConfig();
        renderTradingControl();
        renderPaperPerformance();
        renderCapitalPanel(data);
    } catch (e) {
        console.warn('Config fetch failed.');
    }
}
window.fetchConfig = fetchConfig;

// 主动操作 API
export async function toggleTrading() {
    if (dashboardState.togglePending) return;

    const isRealView = getActiveAccountMode() === 'real';
    const runningMode = (dashboardState.config && dashboardState.config.trading_mode) || 'paper_live';
    const isReadyToGoLive = isRealView && runningMode !== 'live';
    const isReadyToGoPaper = !isRealView && runningMode !== 'paper_live';

    dashboardState.togglePending = true;
    dashboardState.controlError = '';
    renderTradingControl();

    try {
        // 如果交易未开启，先尝试启动 bot 进程
        if (!dashboardState.tradingEnabled) {
            const startResp = await fetch('/api/start-bot', { method: 'POST' });
            const startData = await startResp.json();
            if (startData.error) {
                console.warn('start-bot 返回错误（可能已在运行）:', startData.error);
            }
        } else {
            // 如果交易已开启（用户要关），同时关掉 bot 进程
            await fetch('/api/stop-bot', { method: 'POST' });
        }

        if (isReadyToGoLive || isReadyToGoPaper) {
            const targetMode = isReadyToGoLive ? 'live' : 'paper_live';
            const modeResp = await fetch('/api/update-config', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ TRADING_MODE: targetMode }),
            });
            if (!modeResp.ok) throw new Error('切换运行模式失败');
        }

        const resp = await fetch('/api/control', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ trading_enabled: !dashboardState.tradingEnabled }),
        });
        const data = await resp.json();
        dashboardState.tradingEnabled = data.trading_enabled !== false;

        await Promise.allSettled([fetchControl(), fetchBotStatus(), fetchConfig()]);
    } catch (e) {
        dashboardState.controlError = '更新失败: ' + String(e.message || e).substring(0, 40);
    } finally {
        dashboardState.togglePending = false;
        renderTradingControl();
    }
}
window.toggleTrading = toggleTrading;

function _getInputVal(id) {
    const el = document.getElementById(id);
    return el ? el.value.trim() : '';
}

export async function saveSystemSettings() {
    const saveSettingsBtn = document.getElementById('save-settings');
    const settingsModal = document.getElementById('settings-modal');
    if (!saveSettingsBtn) return;

    saveSettingsBtn.disabled = true;
    saveSettingsBtn.textContent = '保存中...';

    const selectedMode = document.querySelector('.mode-selector .mode-item.active')?.dataset.mode || 'paper_live';
    const marketMode = _getInputVal('cfg-input-market-mode') || 'manual';
    const betVal  = parseFloat(_getInputVal('cfg-input-bet'))        || 1;
    const tpVal   = parseFloat(_getInputVal('cfg-input-tp'))         || 0.60;
    const confVal = parseFloat(_getInputVal('cfg-input-confidence')) || 0.60;
    const maxPosVal = parseInt(_getInputVal('cfg-input-max-positions'), 10) || 1;
    const scanIntervalVal = parseInt(_getInputVal('cfg-input-scan-interval'), 10) || 15;

    // 基础字段总是覆盖
    const updatePayload = {
        TRADING_MODE:       selectedMode,
        bet_amount:         betVal,
        paper_bet_amount:   betVal,
        take_profit_usd:    tpVal,
        AI_MIN_CONFIDENCE:  confVal,
        AI_DECISION_INTERVAL_SECONDS: scanIntervalVal,
        LIVE_MAX_OPEN_POSITIONS: maxPosVal,
        PAPER_MAX_OPEN_POSITIONS: maxPosVal,
        MARKET_SELECTION_MODE: marketMode,
        STRATEGY_PROFILE: 'generic_binary',
        TARGET_MARKET_URL: '',
        TARGET_MARKET_SLUG: '',
    };

    const marketInput = _getInputVal('cfg-input-market');
    if (marketMode === 'manual' && marketInput) {
        const looksLikeUrl = /polymarket\.com\/|^https?:\/\//i.test(marketInput);
        updatePayload.TARGET_MARKET_URL = looksLikeUrl ? marketInput : '';
        updatePayload.TARGET_MARKET_SLUG = looksLikeUrl ? '' : marketInput;
    }

    // Polymarket 凭证 — 只有有填写才覆盖（避免清空已有配置）
    const apiKey  = _getInputVal('cfg-input-api-key');
    const apiSec  = _getInputVal('cfg-input-api-secret');
    const apiPass = _getInputVal('cfg-input-api-pass');
    const privKey = _getInputVal('cfg-input-private-key');
    const funder  = _getInputVal('cfg-input-funder');
    if (apiKey)  updatePayload.POLYMARKET_API_KEY        = apiKey;
    if (apiSec)  updatePayload.POLYMARKET_API_SECRET     = apiSec;
    if (apiPass) updatePayload.POLYMARKET_API_PASSPHRASE = apiPass;
    if (privKey) updatePayload.POLYMARKET_PRIVATE_KEY    = privKey;
    if (funder)  { updatePayload.POLYMARKET_FUNDER_ADDRESS = funder; updatePayload.POLYMARKET_WALLET_ADDRESS = funder; }

    // AI 引擎 — 有填才覆盖
    const aiKey   = _getInputVal('cfg-input-ai-key');
    const aiUrl   = _getInputVal('cfg-input-ai-url');
    const aiModel = _getInputVal('cfg-input-ai-model');
    const aiSkill = _getInputVal('cfg-input-ai-skill');
    if (aiKey)   updatePayload.AI_API_KEY  = aiKey;
    if (aiUrl)   updatePayload.AI_BASE_URL = aiUrl;
    if (aiModel) updatePayload.AI_MODEL    = aiModel;
    updatePayload.AI_TRADING_SKILL = aiSkill;

    try {
        const resp = await fetch('/api/update-config', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(updatePayload)
        });

        if (resp.ok) {
            if (settingsModal) settingsModal.classList.remove('active');
            window.refreshAll();
        } else {
            alert('配置更新失败，请检查服务端日志');
        }
    } catch (err) {
        console.error('Save error:', err);
        alert('网络请求异常');
    } finally {
        saveSettingsBtn.disabled = false;
        saveSettingsBtn.textContent = '保存并应用';
    }
}
window.saveSystemSettings = saveSystemSettings;

export function setAccountMode(mode, shouldRefresh = true) {
    dashboardState.accountMode = mode === 'real' ? 'real' : 'paper';
    dashboardState.controlError = '';
    try {
        window.localStorage.setItem('polymarket_account_mode', dashboardState.accountMode);
    } catch (e) {}
    renderAccountMode();
    renderTradingControl();
    renderConfig();
    renderSystemWorkspace();
    if (shouldRefresh) {
        if (dashboardState.accountMode === 'paper') {
            Promise.allSettled([fetchInstanceDashboard('primary'), fetchInstanceDashboard('parallel')]);
        } else {
            Promise.allSettled([fetchTrades(), fetchOrders()]);
        }
    }
}
window.setAccountMode = setAccountMode;

export function refreshAll() {
    if (getActiveAccountMode() === 'paper') {
        Promise.allSettled([
            fetchBtc(), fetchBtcTrend(), fetchControl(),
            fetchInstanceDashboard('primary'), fetchInstanceDashboard('parallel'),
            fetchParallelStatus()
        ]);
        return;
    }
    Promise.allSettled([
        fetchBtc(), fetchControl(), fetchBotStatus(),
        fetchConfig(), fetchBalance(), fetchRealBalance(),
        fetchTrades(), fetchOrders(), fetchAiHistory(), fetchOrderBook(),
        fetchParallelStatus()
    ]);
}
window.refreshAll = refreshAll;
