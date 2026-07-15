/**
 * NBDpsy 小红书账号助手 - 弹出窗口逻辑
 *
 * 职责：配置 serverUrl + apikey（存 chrome.storage.local）；管理"我的账号"：
 * - 账号列表：拉后台托管账号，点卡片开无痕窗注入该号 cookie 打开小红书
 * - per-card 检测：验该号 cookie 活性
 * - 无痕登录采集：让后台 service worker 开无痕窗口人工登录后采集推送（加/换号）
 * 所有推送均由 service worker 带 Authorization: Bearer <apikey> 打到 /api/cookies/import。
 */

const elements = {
    serverUrl: document.getElementById('server-url'),
    apikey: document.getElementById('apikey'),
    serverStatus: document.getElementById('server-status'),
    btnSaveConfig: document.getElementById('btn-save-config'),
    btnRemoteLogin: document.getElementById('btn-remote-login'),
    accountsList: document.getElementById('accounts-list'),
    btnRefreshAccounts: document.getElementById('btn-refresh-accounts'),
    message: document.getElementById('message')
};

let serverUrl = 'https://mcp.nbdpsy.com';
// 已持久化到 storage 的 apikey（与 service-worker.js 的 getConfig 同源）。
// 列表加载 / 点击注入的准入判断都从这里读，不直接读输入框，避免"改了输入框没保存"导致
// 列表按新 key 拉、注入却用旧 storage key 的不一致。
let savedApikey = '';

document.addEventListener('DOMContentLoaded', async () => {
    console.log('[NBDpsy] Popup 加载');
    // 版本号从 manifest 动态读，避免写死漂移
    const verEl = document.querySelector('.version');
    if (verEl) verEl.textContent = 'v' + chrome.runtime.getManifest().version;
    await loadConfig();
    // 有 apikey 即直接渲染账号列表（loadAccounts 内部会对空 key 给占位提示）
    await loadAccounts();
    bindEvents();
    // 远程采集结果可能在 popup 关闭期间产生，打开时先读一次兜底
    chrome.storage.local.get(['remoteLoginResult', 'accountSessionResult'], (res) => {
        if (res.remoteLoginResult) showRemoteResult(res.remoteLoginResult);
        if (res.accountSessionResult) showAccountSessionResult(res.accountSessionResult);
    });
    // popup 仍开着时采集/注入完成，实时展示
    chrome.storage.onChanged.addListener((changes, area) => {
        if (area !== 'local') return;
        if (changes.remoteLoginResult?.newValue) {
            showRemoteResult(changes.remoteLoginResult.newValue);
        }
        if (changes.accountSessionResult?.newValue) {
            showAccountSessionResult(changes.accountSessionResult.newValue);
        }
    });
});

// 加载 serverUrl + apikey
async function loadConfig() {
    return new Promise((resolve) => {
        chrome.runtime.sendMessage({ action: 'getConfig' }, (response) => {
            if (response && response.success) {
                serverUrl = response.serverUrl;
                savedApikey = response.apikey || '';
                elements.serverUrl.value = response.serverUrl || '';
                elements.apikey.value = response.apikey || '';
            }
            resolve();
        });
    });
}

// 远程登录采集：触发 service worker 开无痕窗口。
// 采集是长流程，点击后本 popup 会被新窗口聚焦而关闭，故这里只"点火"，
// 结果由 SW 写入 storage，popup 下次打开或仍在时经 onChanged 展示（见 DOMContentLoaded）。
async function remoteLogin() {
    if (!elements.apikey.value.trim()) {
        showMessage('error', '请先填写并保存 apikey');
        return;
    }
    await chrome.storage.local.remove('remoteLoginResult');
    chrome.action?.setBadgeText?.({ text: '' });
    showMessage('info', '已打开无痕窗口，请在其中完成登录（扫码/短信/验证码）；登录成功后会自动采集，回到本弹窗即可看到结果');
    chrome.runtime.sendMessage({ action: 'startRemoteLogin' });
}

// 展示远程采集结果（成功/失败），并清掉一次性标记与徽标
function showRemoteResult(r) {
    if (!r) return;
    if (r.success) {
        showMessage('success', r.message || `采集成功（account_id=${r.accountId ?? '?'}）`);
    } else {
        showMessage('error', `采集失败: ${r.error || '未知错误'}`);
    }
    chrome.storage.local.remove('remoteLoginResult');
    chrome.action?.setBadgeText?.({ text: '' });
}

// ── 我的账号：列出后台托管账号 → 点卡片开无痕窗注入 ──

// cookie_status → 中文徽标文案（禁 emoji）。checking 为检测进行中的瞬时态（非库值）。
const ACCOUNT_STATUS_LABEL = {
    valid: '有效',
    invalid: '失效',
    captcha: '验证',
    error: '异常',
    unknown: '未检测',
    checking: '检测中...'
};

// 检测轮询节奏：每 CHECK_POLL_INTERVAL 轮一次，最多 CHECK_POLL_MAX 次（约 60s 超时）。
const CHECK_POLL_INTERVAL = 2500;
const CHECK_POLL_MAX = 24;

// 头像兜底占位（内联 SVG data URI，禁 emoji）。
const AVATAR_FALLBACK = 'data:image/svg+xml,<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="%23999"><circle cx="12" cy="8" r="4"/><ellipse cx="12" cy="18" rx="7" ry="4"/></svg>';

// 从后台拉取当前 apikey 可见的账号并渲染卡片列表。
// key 取已保存的 savedApikey（与注入路径同源的 storage 值），不直接读输入框——
// 否则用户改了输入框但没点"保存配置"时，列表会按新 key 拉、而点击注入仍用 storage 里的旧 key。
async function loadAccounts() {
    const key = savedApikey;
    const base = serverUrl.replace(/\/+$/, '');
    if (!base || !key) {
        renderAccountsEmpty('填好服务器地址与 apikey 后加载账号');
        return;
    }
    renderAccountsEmpty('加载中...');
    let resp;
    try {
        resp = await fetch(`${base}/api/accounts`, {
            headers: { 'Authorization': `Bearer ${key}` }
        });
    } catch (e) {
        renderAccountsEmpty('无法连接服务器，请检查地址');
        return;
    }
    if (resp.status === 401) {
        renderAccountsEmpty('apikey 无效，请在上方重新填写并保存');
        return;
    }
    if (!resp.ok) {
        renderAccountsEmpty(`加载失败（HTTP ${resp.status}）`);
        return;
    }
    let data = {};
    try { data = await resp.json(); } catch (e) { data = {}; }
    renderAccounts(data.accounts || []);
}

// 渲染一行占位/提示文案。
function renderAccountsEmpty(text) {
    elements.accountsList.innerHTML = '';
    const empty = document.createElement('div');
    empty.className = 'accounts-empty';
    empty.textContent = text;
    elements.accountsList.appendChild(empty);
}

// 渲染账号卡片列表。
function renderAccounts(accounts) {
    elements.accountsList.innerHTML = '';
    if (!accounts.length) {
        renderAccountsEmpty('暂无托管账号');
        return;
    }
    for (const acc of accounts) {
        elements.accountsList.appendChild(buildAccountCard(acc));
    }
}

// 用 DOM API 构造卡片（名称用 textContent 防注入）。卡片是 div（不能用 button，
// 内部要嵌「检测」「打开」两个动作按钮，button 不可嵌套）。检测=验 cookie 活性；打开=开无痕注入。
function buildAccountCard(acc) {
    const card = document.createElement('div');
    card.className = 'account-card';
    card.dataset.accountId = acc.id;

    const avatar = document.createElement('span');
    avatar.className = 'account-avatar';
    const img = document.createElement('img');
    img.src = acc.avatar || AVATAR_FALLBACK;
    img.alt = '';
    img.addEventListener('error', () => { img.src = AVATAR_FALLBACK; });
    avatar.appendChild(img);

    const meta = document.createElement('span');
    meta.className = 'account-meta';
    const name = document.createElement('span');
    name.className = 'account-name';
    name.textContent = acc.nickname || acc.name || `账号 ${acc.id}`;
    const status = acc.cookie_status || 'unknown';
    const badge = document.createElement('span');
    badge.className = `account-badge ${status}`;
    badge.textContent = ACCOUNT_STATUS_LABEL[status] || status;
    // 徽标挂到卡片上，检测轮询时原地改文案/配色，无需重渲染整卡。
    card.dataset.badge = '';
    meta.appendChild(name);
    meta.appendChild(badge);

    const actions = document.createElement('span');
    actions.className = 'account-actions';

    // 检测按钮：触发后端 cookie 活性巡检并在本卡轮询到终态。
    const detectBtn = document.createElement('button');
    detectBtn.type = 'button';
    detectBtn.className = 'account-detect-btn';
    detectBtn.textContent = '检测';
    detectBtn.title = '检测该账号 cookie 是否仍有效';
    detectBtn.addEventListener('click', () =>
        triggerCookieCheck(acc.id, badge, detectBtn));

    // 打开按钮：开无痕窗注入该号 cookie 打开小红书。
    const openBtn = document.createElement('button');
    openBtn.type = 'button';
    openBtn.className = 'account-open-btn';
    openBtn.title = '开无痕窗口注入该账号 cookie 打开小红书';
    openBtn.innerHTML = '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"/><polyline points="15 3 21 3 21 9"/><line x1="10" y1="14" x2="21" y2="3"/></svg>';
    openBtn.addEventListener('click', () => openAccountSessionFromCard(acc.id));

    actions.appendChild(detectBtn);
    actions.appendChild(openBtn);

    card.appendChild(avatar);
    card.appendChild(meta);
    card.appendChild(actions);
    return card;
}

// 把徽标原地改成某状态（含 checking 瞬时态）；复用 ACCOUNT_STATUS_LABEL 与配色 class。
function setBadge(badge, status, textOverride) {
    badge.className = `account-badge ${status}`;
    badge.textContent = textOverride || ACCOUNT_STATUS_LABEL[status] || status;
}

// 触发后端 cookie 活性巡检并在本卡轮询到终态。检测约 20-40s，popup 保持打开即可看到结果；
// 即便中途关掉 popup，后端仍会把 cookie_status 写回库，下次打开刷新列表照样是真实态，结果不丢。
async function triggerCookieCheck(accountId, badge, detectBtn) {
    if (!savedApikey) {
        showMessage('error', '请先填写并保存 apikey');
        return;
    }
    const base = serverUrl.replace(/\/+$/, '');
    detectBtn.disabled = true;
    setBadge(badge, 'checking');

    // 1. 发起检测，拿 check_id。
    let checkId;
    try {
        const resp = await fetch(`${base}/api/accounts/${accountId}/cookie-checks`, {
            method: 'POST',
            headers: { 'Authorization': `Bearer ${savedApikey}` }
        });
        const data = await resp.json().catch(() => ({}));
        if (!resp.ok) {
            throw new Error(data.error || data.detail || `HTTP ${resp.status}`);
        }
        checkId = data.check_id;
    } catch (e) {
        setBadge(badge, 'error', '发起失败');
        detectBtn.disabled = false;
        showMessage('error', `检测发起失败: ${e.message}`);
        return;
    }

    // 2. 轮询到终态或超时。
    for (let i = 0; i < CHECK_POLL_MAX; i++) {
        await new Promise(r => setTimeout(r, CHECK_POLL_INTERVAL));
        let data;
        try {
            const resp = await fetch(`${base}/api/cookie-checks/${checkId}`, {
                headers: { 'Authorization': `Bearer ${savedApikey}` }
            });
            data = await resp.json().catch(() => ({}));
            if (resp.status === 404) {
                setBadge(badge, 'error', '结果丢失');
                showMessage('error', '检测结果已丢失（服务重启或过期），请重试');
                detectBtn.disabled = false;
                return;
            }
            if (!resp.ok) {
                throw new Error(data.error || data.detail || `HTTP ${resp.status}`);
            }
        } catch (e) {
            setBadge(badge, 'error', '轮询失败');
            showMessage('error', `检测轮询失败: ${e.message}`);
            detectBtn.disabled = false;
            return;
        }

        const st = data.status;
        if (st === 'checking') continue;

        // 终态：valid/invalid/captcha/error。
        detectBtn.disabled = false;
        if (st === 'error') {
            // error 是基础设施失败（浏览器起不来/超时），不代表 cookie 失效，不误伤成"失效"。
            setBadge(badge, 'error', '检测异常');
            showMessage('info', `检测未完成（非 cookie 失效）: ${data.reason || '基础设施错误'}，可稍后重试`);
        } else {
            setBadge(badge, st);
            const msg = { valid: '该账号 cookie 有效', invalid: '该账号 cookie 已失效，需重新扫码登录', captcha: '被验证码/滑块拦截，需人工过验证' }[st] || `检测完成: ${st}`;
            showMessage(st === 'valid' ? 'success' : 'info', msg);
            // 有效时后端可能补全了 nickname/red_id/avatar，刷新列表拉最新信息。
            if (st === 'valid') loadAccounts();
        }
        return;
    }

    // 超时。
    setBadge(badge, 'error', '检测超时');
    detectBtn.disabled = false;
    showMessage('error', '检测超时（约 60s 未出结果），请重试');
}

// 点击账号卡片：触发 service worker 开无痕窗注入。聚焦新窗口会关闭 popup，
// 故这里只"点火"，结果由 SW 写入 storage，popup 下次打开或仍在时经 onChanged 展示。
async function openAccountSessionFromCard(accountId) {
    if (!savedApikey) {
        showMessage('error', '请先填写并保存 apikey');
        return;
    }
    await chrome.storage.local.remove('accountSessionResult');
    chrome.action?.setBadgeText?.({ text: '' });
    showMessage('info', '正在打开无痕窗口并注入该账号 cookie...');
    chrome.runtime.sendMessage({ action: 'openAccountSession', accountId });
}

// 展示无痕注入结果（成功/部分失败/失败），并清一次性标记与徽标。
function showAccountSessionResult(r) {
    if (!r) return;
    if (r.success) {
        const failNote = r.failed ? `，${r.failed} 条失败` : '';
        showMessage('success', `已在无痕窗口打开账号（注入 ${r.injected} 条 cookie${failNote}）。若未见新窗口，请到 chrome://extensions 勾选「在无痕模式下启用」`);
    } else {
        showMessage('error', `打开失败: ${r.error || '未知错误'}`);
    }
    chrome.storage.local.remove('accountSessionResult');
    chrome.action?.setBadgeText?.({ text: '' });
}

// 保存 serverUrl + apikey
async function saveConfig() {
    const newUrl = elements.serverUrl.value.trim();
    const newKey = elements.apikey.value.trim();

    if (!newUrl) {
        showServerStatus('error', '请输入服务器地址');
        return;
    }
    try {
        new URL(newUrl);
    } catch {
        showServerStatus('error', '无效的 URL 格式');
        return;
    }

    serverUrl = newUrl;
    await sendMessage({ action: 'setConfig', serverUrl: newUrl, apikey: newKey });
    savedApikey = newKey;

    // 探活后台（/healthz 免鉴权）
    showServerStatus('info', '正在测试连接...');
    const isConnected = await checkServerConnection();
    showServerStatus(isConnected ? 'success' : 'error',
        isConnected ? '连接成功' : '无法连接到服务器');

    // 配置更新后刷新"我的账号"列表（新 apikey 可见范围可能变化）。
    await loadAccounts();
}

// 探活后台（/healthz 不需要 apikey）
async function checkServerConnection() {
    try {
        const response = await fetch(`${serverUrl.replace(/\/+$/, '')}/healthz`, { method: 'GET' });
        return response.ok;
    } catch (error) {
        return false;
    }
}

function bindEvents() {
    elements.btnRemoteLogin.addEventListener('click', remoteLogin);
    elements.btnSaveConfig.addEventListener('click', saveConfig);
    elements.btnRefreshAccounts.addEventListener('click', loadAccounts);
    document.getElementById('btn-help').addEventListener('click', (e) => {
        e.preventDefault();
        chrome.tabs.create({ url: `${serverUrl.replace(/\/+$/, '')}/healthz` });
    });
}

// 工具函数
function sendMessage(message) {
    return new Promise((resolve) => {
        chrome.runtime.sendMessage(message, resolve);
    });
}

function showMessage(type, text) {
    elements.message.className = `message ${type}`;
    elements.message.textContent = text;
    elements.message.style.display = 'block';
    setTimeout(() => { elements.message.style.display = 'none'; }, 6000);
}

function showServerStatus(type, text) {
    elements.serverStatus.className = `server-status ${type}`;
    elements.serverStatus.textContent = text;
}
