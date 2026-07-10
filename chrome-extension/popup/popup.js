/**
 * NBDpsy 小红书账号助手 - 弹出窗口逻辑
 *
 * 职责：配置 serverUrl + apikey（存 chrome.storage.local）；触发两种采集：
 * - 同步当前账号：抓当前会话 cookie 直接推后台
 * - 远程登录采集：让后台 service worker 开无痕窗口人工登录后采集推送
 * 所有推送均由 service worker 带 Authorization: Bearer <apikey> 打到 /api/cookies/import。
 */

const elements = {
    statusIndicator: document.getElementById('status-indicator'),
    statusText: document.querySelector('.status-text'),
    cookieCount: document.getElementById('cookie-count'),
    userInfoSection: document.getElementById('user-info-section'),
    userAvatar: document.getElementById('user-avatar'),
    userNickname: document.getElementById('user-nickname'),
    userId: document.getElementById('user-id'),
    serverUrl: document.getElementById('server-url'),
    apikey: document.getElementById('apikey'),
    serverStatus: document.getElementById('server-status'),
    btnSaveConfig: document.getElementById('btn-save-config'),
    btnSync: document.getElementById('btn-sync'),
    btnRemoteLogin: document.getElementById('btn-remote-login'),
    btnOpenXhs: document.getElementById('btn-open-xhs'),
    message: document.getElementById('message')
};

let currentCookies = [];
let currentUserInfo = null;
let serverUrl = 'https://mcp.nbdpsy.com';

document.addEventListener('DOMContentLoaded', async () => {
    console.log('[NBDpsy] Popup 加载');
    // 版本号从 manifest 动态读，避免写死漂移
    const verEl = document.querySelector('.version');
    if (verEl) verEl.textContent = 'v' + chrome.runtime.getManifest().version;
    await loadConfig();
    await checkLoginStatus();
    bindEvents();
    // 远程采集结果可能在 popup 关闭期间产生，打开时先读一次兜底
    chrome.storage.local.get('remoteLoginResult', ({ remoteLoginResult }) => {
        if (remoteLoginResult) showRemoteResult(remoteLoginResult);
    });
    // popup 仍开着时采集完成，实时展示
    chrome.storage.onChanged.addListener((changes, area) => {
        if (area === 'local' && changes.remoteLoginResult?.newValue) {
            showRemoteResult(changes.remoteLoginResult.newValue);
        }
    });
});

// 加载 serverUrl + apikey
async function loadConfig() {
    return new Promise((resolve) => {
        chrome.runtime.sendMessage({ action: 'getConfig' }, (response) => {
            if (response && response.success) {
                serverUrl = response.serverUrl;
                elements.serverUrl.value = response.serverUrl || '';
                elements.apikey.value = response.apikey || '';
            }
            resolve();
        });
    });
}

// 检查登录状态
async function checkLoginStatus() {
    setStatus('checking', '检测中...');

    try {
        const cookieResponse = await sendMessage({ action: 'collectCookies' });

        if (cookieResponse.success) {
            currentCookies = cookieResponse.cookies;
            elements.cookieCount.textContent = `${currentCookies.length} 个 Cookies`;

            const hasA1 = currentCookies.some(c => c.name === 'a1');
            const hasWebId = currentCookies.some(c => c.name === 'webId');

            if (hasA1 && hasWebId) {
                setStatus('success', '已登录小红书');
                elements.btnSync.disabled = false;
                await getUserInfo();
            } else {
                setStatus('error', '未登录小红书');
                elements.btnSync.disabled = true;
            }
        } else {
            setStatus('error', '检测失败');
            elements.btnSync.disabled = true;
        }
    } catch (error) {
        console.error('[NBDpsy] 检测状态失败:', error);
        setStatus('error', '检测失败');
        elements.btnSync.disabled = true;
    }
}

// 从当前活动标签页获取用户信息（可选）
async function getUserInfo() {
    try {
        const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
        if (tab && tab.url && tab.url.includes('xiaohongshu.com')) {
            const response = await chrome.tabs.sendMessage(tab.id, { action: 'getUserInfo' });
            if (response && response.success && response.userInfo) {
                currentUserInfo = response.userInfo;
                if (currentUserInfo.nickname) {
                    elements.userInfoSection.style.display = 'flex';
                    elements.userNickname.textContent = currentUserInfo.nickname;
                    elements.userId.textContent = currentUserInfo.red_id ? `小红书号: ${currentUserInfo.red_id}` : '';
                    elements.userAvatar.src = currentUserInfo.avatar ||
                        'data:image/svg+xml,<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="%23999"><circle cx="12" cy="8" r="4"/><ellipse cx="12" cy="18" rx="7" ry="4"/></svg>';
                }
            }
        }
    } catch (error) {
        // 用户信息可选，不阻断
        console.log('[NBDpsy] 获取用户信息失败:', error);
    }
}

// 同步当前账号到后台（service worker 抓当前会话 cookie 后推送）
async function syncAccount() {
    if (!elements.apikey.value.trim()) {
        showMessage('error', '请先填写并保存 apikey');
        return;
    }

    setSyncing(true, '同步中...');
    try {
        const result = await sendMessage({ action: 'syncCurrentSession' });
        if (result && result.success) {
            showMessage('success', `账号同步成功（account_id=${result.accountId}${result.created ? '，新建' : '，更新'}）`);
        } else {
            showMessage('error', `同步失败: ${(result && result.error) || '未知错误'}`);
        }
    } catch (error) {
        console.error('[NBDpsy] 同步失败:', error);
        showMessage('error', `同步失败: ${error.message}`);
    } finally {
        setSyncing(false, '同步当前账号到后台');
    }
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

    // 探活后台（/healthz 免鉴权）
    showServerStatus('info', '正在测试连接...');
    const isConnected = await checkServerConnection();
    showServerStatus(isConnected ? 'success' : 'error',
        isConnected ? '连接成功' : '无法连接到服务器');
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
    elements.btnSync.addEventListener('click', syncAccount);
    elements.btnRemoteLogin.addEventListener('click', remoteLogin);
    elements.btnSaveConfig.addEventListener('click', saveConfig);
    elements.btnOpenXhs.addEventListener('click', () => {
        chrome.tabs.create({ url: 'https://www.xiaohongshu.com' });
    });
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

function setStatus(type, text) {
    elements.statusIndicator.className = `status-indicator status-${type}`;
    elements.statusText.textContent = text;
}

function setSyncing(on, text) {
    elements.btnSync.disabled = on;
    elements.btnSync.classList.toggle('loading', on);
    elements.btnSync.querySelector('.btn-text').textContent = text;
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
