/**
 * NBDpsy 小红书账号助手 - 后台服务 (Service Worker)
 *
 * 职责：
 * 1. 采集小红书 Cookies（跨所有 cookieStore + Set-Cookie 响应头补抓 httpOnly）
 * 2. 打开无痕窗口让操作者人工登录，登录成功后采集 Cookies + 用户信息
 * 3. 统一推送到后台 POST {serverUrl}/api/cookies/import，鉴权用 Operator apikey
 * 4. 客户端注入（路线 B）：从后台取某号解密 cookie，开无痕窗注入后打开小红书
 *    （openAccountSession，popup"我的账号"列表点击入口）
 *
 * 与旧仓差异：
 * - 鉴权从 JWT Bearer token 换成 Operator apikey（Authorization: Bearer <apikey>）。
 * - 推送端点统一为 /api/cookies/import（去掉 save-cookies / create-with-cookies 二分）。
 * - serverUrl + apikey 由 popup 存入 chrome.storage.local，本 worker 读取后使用。
 * - openAccountSession 移植旧仓 openRemoteBrowser 的无痕开窗+注入逻辑，但 sameSite 保原值
 *   映射（不统一降 lax）、入口改为 popup 消息（无 onMessageExternal 前端桥）、cookie 源改为
 *   带 apikey 拉 /api/accounts/{id}/cookies。
 */

// 后台默认地址（对应后端 config.PUBLIC_BASE_URL 默认值），操作者可在 popup 覆盖。
const DEFAULT_SERVER_URL = 'https://mcp.nbdpsy.com';

// 读取 serverUrl + apikey 配置。
function getConfig() {
    return new Promise((resolve) => {
        chrome.storage.local.get(['serverUrl', 'apikey'], (result) => {
            resolve({
                serverUrl: (result.serverUrl || DEFAULT_SERVER_URL).replace(/\/+$/, ''),
                apikey: result.apikey || ''
            });
        });
    });
}

// 把任意来源的 cookie 归一到后台 /api/cookies/import 期望的字段形状。
// 后台 Pydantic 只约束 cookies: list[dict]，服务层 normalize_cookies 会再规范 sameSite，
// 并保留 name/value/domain/path/httpOnly/secure/expires。
function formatCookie(c) {
    return {
        name: c.name,
        value: c.value,
        domain: c.domain,
        path: c.path || '/',
        httpOnly: c.httpOnly || false,
        secure: c.secure || false,
        sameSite: c.sameSite || 'Lax',
        // chrome.cookies API 用 expirationDate；拦截/解析出的 cookie 用 expires；都没有则 -1（会话 cookie）。
        expires: c.expirationDate ?? c.expires ?? -1
    };
}

// 统一推送 cookie 到后台。account_name 是后台必填字段（Pydantic）。
async function pushCookies({ accountName, cookies, userInfo }) {
    const { serverUrl, apikey } = await getConfig();
    if (!apikey) {
        return { success: false, error: '未配置 apikey，请在扩展弹窗中填写' };
    }

    const body = {
        account_name: accountName,
        cookies: cookies.map(formatCookie),
        user_info: userInfo || null
    };

    const response = await fetch(`${serverUrl}/api/cookies/import`, {
        method: 'POST',
        headers: {
            'Content-Type': 'application/json',
            'Authorization': `Bearer ${apikey}`
        },
        body: JSON.stringify(body)
    });

    let result = {};
    try {
        result = await response.json();
    } catch (e) {
        result = {};
    }

    if (!response.ok) {
        return {
            success: false,
            error: result.detail || result.error || `HTTP ${response.status}`
        };
    }
    // 后台返回 {account_id, created}
    return { success: true, accountId: result.account_id, created: result.created };
}

// 采集当前会话的小红书 Cookies（chrome.cookies API 含 httpOnly）。
async function collectXHSCookies() {
    const cookies = await chrome.cookies.getAll({ domain: '.xiaohongshu.com' });
    console.log(`[NBDpsy] 采集到 ${cookies.length} 个 Cookies`);
    return cookies.map(formatCookie);
}

// 检查是否已登录小红书（关键 cookie 判断）。
async function checkLoginStatus() {
    const cookies = await collectXHSCookies();
    const hasA1 = cookies.some(c => c.name === 'a1');
    const hasWebSession = cookies.some(c => c.name === 'web_session');
    const hasWebId = cookies.some(c => c.name === 'webId');
    return {
        isLoggedIn: hasA1 && hasWebId,
        hasSession: hasWebSession,
        cookieCount: cookies.length
    };
}

// ── Set-Cookie 响应头解析（捕获 chrome.cookies API 可能漏掉的 httpOnly cookie，如 web_session）──
function parseSetCookieHeader(headerValue) {
    try {
        const parts = headerValue.split(';').map(s => s.trim());
        const [nameValue, ...attrs] = parts;
        const eqIdx = nameValue.indexOf('=');
        if (eqIdx <= 0) return null;

        const cookie = {
            name: nameValue.substring(0, eqIdx),
            value: nameValue.substring(eqIdx + 1),
            domain: '.xiaohongshu.com',
            path: '/',
            httpOnly: false,
            secure: false,
            sameSite: 'Lax'
        };

        for (const attr of attrs) {
            const lower = attr.toLowerCase();
            if (lower.startsWith('domain=')) {
                cookie.domain = attr.substring(7);
            } else if (lower.startsWith('path=')) {
                cookie.path = attr.substring(5);
            } else if (lower === 'httponly') {
                cookie.httpOnly = true;
            } else if (lower === 'secure') {
                cookie.secure = true;
            } else if (lower.startsWith('samesite=')) {
                cookie.sameSite = attr.substring(9);
            } else if (lower.startsWith('expires=')) {
                try {
                    cookie.expires = new Date(attr.substring(8)).getTime() / 1000;
                } catch (e) { /* 忽略 */ }
            } else if (lower.startsWith('max-age=')) {
                const maxAge = parseInt(attr.substring(8), 10);
                if (!isNaN(maxAge)) {
                    cookie.expires = Date.now() / 1000 + maxAge;
                }
            }
        }
        return cookie;
    } catch (e) {
        return null;
    }
}

// 远程登录采集：打开无痕窗口让操作者人工登录（含扫码/短信/验证码），
// 登录成功后跨所有 cookieStore + Set-Cookie 响应头采集 Cookies 与用户信息，推送后台。
async function startRemoteLogin() {
    console.log('[NBDpsy] 开始远程登录采集流程...');

    // 启动 Set-Cookie 响应头拦截器（捕获 chrome.cookies API 看不到的 httpOnly cookies）
    const interceptedCookies = new Map();  // key: name@domain
    const headerListener = (details) => {
        const setCookieHeaders = (details.responseHeaders || []).filter(
            h => h.name.toLowerCase() === 'set-cookie'
        );
        for (const header of setCookieHeaders) {
            const parsed = parseSetCookieHeader(header.value);
            if (parsed && parsed.name) {
                interceptedCookies.set(`${parsed.name}@${parsed.domain}`, parsed);
            }
        }
    };

    try {
        chrome.webRequest.onHeadersReceived.addListener(
            headerListener,
            { urls: ['https://*.xiaohongshu.com/*'] },
            ['responseHeaders', 'extraHeaders']
        );
        console.log('[NBDpsy] Set-Cookie 响应头拦截器已启动');
    } catch (e) {
        console.warn('[NBDpsy] Set-Cookie 拦截器启动失败:', e.message);
    }

    try {
        // 随机窗口特征，降低被风控识别的概率
        const windowSizes = [
            { width: 1920, height: 1080 },
            { width: 1440, height: 900 },
            { width: 1536, height: 864 },
            { width: 1366, height: 768 },
            { width: 1280, height: 720 },
            { width: 1600, height: 900 },
            { width: 1680, height: 1050 }
        ];
        const randomSize = windowSizes[Math.floor(Math.random() * windowSizes.length)];
        const randomDelay = 2000 + Math.floor(Math.random() * 2000);

        console.log(`[NBDpsy] 随机特征: 窗口 ${randomSize.width}x${randomSize.height}, 延迟 ${randomDelay}ms`);

        // 1. 创建无痕窗口打开小红书
        const loginWindow = await chrome.windows.create({
            incognito: true,
            width: randomSize.width,
            height: randomSize.height,
            url: 'https://www.xiaohongshu.com'
        });
        const windowId = loginWindow.id;
        console.log(`[NBDpsy] 无痕登录窗口已创建，窗口 ID: ${windowId}`);

        // 获取 tab（无痕模式下 tabs 可能为空，需主动查询；多次重试缓解偶发早退）
        let tabId;
        if (loginWindow.tabs && loginWindow.tabs.length > 0) {
            tabId = loginWindow.tabs[0].id;
        } else {
            for (let i = 0; i < 6 && tabId == null; i++) {
                await new Promise(r => setTimeout(r, 500));
                const tabs = await chrome.tabs.query({ windowId: windowId });
                if (tabs && tabs.length > 0) tabId = tabs[0].id;
            }
            if (tabId == null) {
                throw new Error('无法获取无痕窗口的 tab，请确保扩展已开启"在无痕模式下启用"');
            }
        }
        console.log(`[NBDpsy] tabId: ${tabId}`);

        await new Promise(resolve => setTimeout(resolve, randomDelay));

        return new Promise((resolve) => {
            let loginDetected = false;
            let checkInterval = null;
            let windowClosed = false;
            let checkCount = 0;
            const maxChecks = 150; // 5 分钟 / 2 秒

            const onWindowRemoved = (closedWindowId) => {
                if (closedWindowId === windowId) {
                    windowClosed = true;
                    cleanup();
                    if (!loginDetected) {
                        resolve({ success: false, error: '用户关闭了登录窗口' });
                    }
                }
            };
            chrome.windows.onRemoved.addListener(onWindowRemoved);

            const cleanup = () => {
                if (checkInterval) {
                    clearInterval(checkInterval);
                    checkInterval = null;
                }
                chrome.windows.onRemoved.removeListener(onWindowRemoved);
            };

            const checkLoginViaContentScript = async () => {
                try {
                    return await chrome.tabs.sendMessage(tabId, { action: 'checkPageLogin' });
                } catch (e) {
                    return null;
                }
            };

            checkInterval = setInterval(async () => {
                if (windowClosed || loginDetected) return;

                checkCount++;
                if (checkCount > maxChecks) {
                    cleanup();
                    try { await chrome.windows.remove(windowId); } catch (e) { }
                    resolve({ success: false, error: '登录超时（5分钟），请重试' });
                    return;
                }

                try {
                    const loginStatus = await checkLoginViaContentScript();

                    if (loginStatus && loginStatus.success && loginStatus.loginStatus?.isLoggedIn) {
                        console.log('[NBDpsy] 检测到页面已登录！');
                        const profileUrl = loginStatus.loginStatus.profileUrl;
                        loginDetected = true;
                        cleanup();

                        await new Promise(r => setTimeout(r, 2000));

                        // 2. 进入个人主页采集用户信息
                        const targetUrl = profileUrl || 'https://www.xiaohongshu.com/user/profile/me';
                        console.log(`[NBDpsy] 进入个人主页: ${targetUrl}`);
                        await chrome.tabs.update(tabId, { url: targetUrl });
                        await new Promise(r => setTimeout(r, 5000));

                        let userInfo = null;
                        let profileAttempts = 0;
                        const maxProfileAttempts = 60; // 最多约 2 分钟

                        while (profileAttempts < maxProfileAttempts && !userInfo?.nickname) {
                            profileAttempts++;
                            await new Promise(r => setTimeout(r, 2000));

                            let currentTab;
                            try {
                                currentTab = await chrome.tabs.get(tabId);
                            } catch (e) {
                                break;
                            }
                            const currentUrl = currentTab.url || '';

                            // 遇到验证码 / 未到主页：继续等待人工处理
                            if (currentUrl.includes('captcha') || currentUrl.includes('verify')) {
                                continue;
                            }
                            if (!currentUrl.includes('/user/profile/')) {
                                continue;
                            }

                            try {
                                const info = await chrome.tabs.sendMessage(tabId, { action: 'getUserInfo' });
                                if (info && info.success && info.userInfo) {
                                    userInfo = info.userInfo;
                                    if (userInfo.nickname) {
                                        console.log('[NBDpsy] 成功采集到用户信息:', userInfo.nickname);
                                        break;
                                    }
                                }
                            } catch (e) {
                                // content script 尚未就绪，继续等待
                            }
                        }

                        if (!userInfo?.nickname) {
                            console.warn('[NBDpsy] 未能采集到用户信息（可能验证码未完成或超时）');
                        }

                        // 3. 采集 Cookies（跨所有 cookieStore + Set-Cookie 拦截）
                        console.log('[NBDpsy] 等待最终 Cookie 写入...');
                        await new Promise(r => setTimeout(r, 3000));

                        const cookieMap = new Map();
                        const addCookies = (cookies, source) => {
                            let added = 0;
                            for (const c of cookies) {
                                const key = `${c.name}@${c.domain}`;
                                if (!cookieMap.has(key)) {
                                    cookieMap.set(key, c);
                                    added++;
                                }
                            }
                            if (added > 0) {
                                console.log(`[NBDpsy] 从 ${source} 添加了 ${added} 个新 Cookie`);
                            }
                        };

                        // 遍历所有 cookieStore（无痕窗口有独立 store），主站 + creator 子域全量采集
                        const stores = await chrome.cookies.getAllCookieStores();
                        for (const store of stores) {
                            try {
                                const xhsCookies = (await chrome.cookies.getAll({ storeId: store.id }))
                                    .filter(c => c.domain.includes('xiaohongshu.com'));
                                addCookies(xhsCookies, `store[${store.id}]`);
                            } catch (e) { /* 忽略无权限 store */ }
                        }

                        // 通过页面 JS 读取 document.cookie（补采非 httpOnly cookie）
                        try {
                            const jsResults = await chrome.scripting.executeScript({
                                target: { tabId: tabId },
                                func: () => document.cookie
                            });
                            const docCookieStr = jsResults?.[0]?.result || '';
                            for (const pair of docCookieStr.split(';').map(s => s.trim()).filter(Boolean)) {
                                const eqIdx = pair.indexOf('=');
                                if (eqIdx > 0) {
                                    const name = pair.substring(0, eqIdx).trim();
                                    const value = pair.substring(eqIdx + 1).trim();
                                    const jsKey = `${name}@.xiaohongshu.com`;
                                    if (!cookieMap.has(jsKey)) {
                                        cookieMap.set(jsKey, {
                                            name, value,
                                            domain: '.xiaohongshu.com',
                                            path: '/', httpOnly: false, secure: true, sameSite: 'Lax'
                                        });
                                    }
                                }
                            }
                        } catch (e) {
                            console.warn('[NBDpsy] document.cookie 采集失败:', e.message);
                        }

                        // 合并 Set-Cookie 响应头拦截到的 cookies（httpOnly 关键路径）
                        if (interceptedCookies.size > 0) {
                            for (const [key, c] of interceptedCookies) {
                                if (!cookieMap.has(key)) cookieMap.set(key, c);
                            }
                            console.log(`[NBDpsy] 从 Set-Cookie 响应头合并 ${interceptedCookies.size} 个 Cookie`);
                        }

                        try {
                            chrome.webRequest.onHeadersReceived.removeListener(headerListener);
                        } catch (e) { /* 忽略 */ }

                        const allCookies = Array.from(cookieMap.values());
                        console.log(`[NBDpsy] 最终采集到 ${allCookies.length} 个 Cookies`);

                        // 4. 推送后台（account_name 用昵称 / 用户 ID / 时间戳兜底）
                        const accountName = userInfo?.nickname
                            || (userInfo?.user_id ? `xhs_${userInfo.user_id}` : `xhs_account_${Date.now()}`);
                        const pushResult = await pushCookies({ accountName, cookies: allCookies, userInfo });
                        console.log('[NBDpsy] 后台响应:', pushResult);

                        try {
                            await chrome.windows.remove(windowId);
                        } catch (e) { /* 忽略 */ }

                        if (pushResult.success) {
                            resolve({
                                success: true,
                                cookiesCollected: allCookies.length,
                                accountId: pushResult.accountId,
                                created: pushResult.created,
                                userInfo: userInfo,
                                message: userInfo?.nickname
                                    ? `登录成功！欢迎 ${userInfo.nickname}`
                                    : '登录成功，Cookies 已保存'
                            });
                        } else {
                            resolve({ success: false, error: pushResult.error });
                        }
                    } else if (checkCount % 10 === 0) {
                        console.log(`[NBDpsy] 等待用户登录... (${checkCount * 2}秒)`);
                    }
                } catch (e) {
                    console.warn('[NBDpsy] 检查登录状态出错:', e.message);
                }
            }, 2000);
        });
    } catch (error) {
        try { chrome.webRequest.onHeadersReceived.removeListener(headerListener); } catch (e) { }
        console.error('[NBDpsy] 远程登录采集失败:', error);
        throw error;
    }
}

// ── 客户端注入（路线 B）：列出后台托管账号 → 点一个 → 开无痕窗注入该号 cookie 打开小红书 ──

// 把后台下发 cookie 的 sameSite 映射到 chrome.cookies.set 要求的小写枚举，**保留原值语义**
// （不像旧仓统一降 lax，否则 XHS 跨站 cookie（SameSite=None）会失效）。
// 后台存的是 Playwright 形式（Strict/Lax/None）；兼容小写与 Chrome 扩展别名 no_restriction。
// 缺省/未识别 → 'lax'。
function toChromeSameSite(value) {
    const v = typeof value === 'string' ? value.toLowerCase() : '';
    if (v === 'none' || v === 'no_restriction') return 'no_restriction';
    if (v === 'strict') return 'strict';
    if (v === 'lax') return 'lax';
    return 'lax';
}

// 从后台取某号解密 cookie（带 apikey）；返回 { ok, cookies, error }。
async function fetchAccountCookies(accountId) {
    const { serverUrl, apikey } = await getConfig();
    if (!apikey) {
        return { ok: false, error: '未配置 apikey，请在扩展弹窗中填写' };
    }
    let response;
    try {
        response = await fetch(`${serverUrl}/api/accounts/${accountId}/cookies`, {
            headers: { 'Authorization': `Bearer ${apikey}` }
        });
    } catch (e) {
        return { ok: false, error: `拉取 cookie 失败: ${e.message}` };
    }
    let result = {};
    try {
        result = await response.json();
    } catch (e) {
        result = {};
    }
    if (!response.ok) {
        return {
            ok: false,
            error: result.detail || result.error || `HTTP ${response.status}`
        };
    }
    return { ok: true, cookies: result.cookies || [] };
}

// 把 cookie 逐条注入指定 cookieStore；单条失败只记 log 不中断，返回 { injected, failed }。
async function injectCookiesIntoStore(cookies, storeId) {
    let injected = 0;
    let failed = 0;
    for (const cookie of cookies) {
        try {
            const domain = (cookie.domain || '').replace(/^\./, '');
            const path = cookie.path || '/';
            const details = {
                url: `https://${domain}${path}`,
                name: cookie.name,
                value: cookie.value,
                domain: cookie.domain,
                path: path,
                // 未显式 secure=false 即视为 secure（与旧仓一致；SameSite=None 亦要求 secure）。
                secure: cookie.secure !== false,
                // httpOnly 原样透传。
                httpOnly: cookie.httpOnly || false,
                // sameSite 保留原值映射（见 toChromeSameSite）。
                sameSite: toChromeSameSite(cookie.sameSite),
                storeId: storeId
            };
            // expires>0 才带 expirationDate（<=0 视为会话 cookie，不设过期）。
            const expires = cookie.expires ?? cookie.expirationDate;
            if (typeof expires === 'number' && expires > 0) {
                details.expirationDate = expires;
            }
            await chrome.cookies.set(details);
            injected++;
        } catch (e) {
            console.warn(`[NBDpsy] 注入 Cookie ${cookie && cookie.name} 失败:`, e.message);
            failed++;
        }
    }
    return { injected, failed };
}

// 开无痕窗口 + 注入某号 cookie + 导航到小红书（移植旧仓 openRemoteBrowser，适配路线 B）。
async function openAccountSession(accountId) {
    // 1. 从后台取该号解密 cookie。
    const fetched = await fetchAccountCookies(accountId);
    if (!fetched.ok) {
        return { success: false, error: fetched.error };
    }
    const cookies = fetched.cookies;
    if (cookies.length === 0) {
        return { success: false, error: '该账号暂无可用 cookie（可能尚未登录采集）' };
    }

    // 2. 创建无痕窗口（先空白页，拿到独立 cookieStore 后再导航，避免注入前就发请求）。
    const win = await chrome.windows.create({
        incognito: true,
        state: 'maximized',
        url: 'about:blank'
    });
    const windowId = win.id;
    console.log(`[NBDpsy] 无痕窗口已创建，窗口 ID: ${windowId}`);

    // 3. 定位该无痕窗的 cookieStore（无痕有独立 store）；匹配不到回退 '1'（无痕常见值）。
    let storeId = '1';
    try {
        const stores = await chrome.cookies.getAllCookieStores();
        const winTabIds = (win.tabs || []).map(t => t.id);
        const matched = stores.find(
            s => (s.tabIds || []).some(id => winTabIds.includes(id))
        );
        if (matched) storeId = matched.id;
    } catch (e) {
        console.warn('[NBDpsy] 获取 cookieStore 失败，回退 storeId=1:', e.message);
    }
    console.log(`[NBDpsy] 使用 Cookie Store ID: ${storeId}`);

    // 4. 先清该 store 里旧的 .xiaohongshu.com cookie（避免与既有会话串味）。
    try {
        const existing = await chrome.cookies.getAll({
            domain: '.xiaohongshu.com',
            storeId: storeId
        });
        for (const c of existing) {
            try {
                await chrome.cookies.remove({
                    url: `https://${c.domain.replace(/^\./, '')}${c.path}`,
                    name: c.name,
                    storeId: storeId
                });
            } catch (e) { /* 单条清理失败忽略 */ }
        }
        if (existing.length > 0) {
            console.log(`[NBDpsy] 已清除 ${existing.length} 个旧 Cookie`);
        }
    } catch (e) {
        console.warn('[NBDpsy] 清理旧 cookie 失败:', e.message);
    }

    // 5. 逐条注入（sameSite 保原值；单条失败不中断）。
    const { injected, failed } = await injectCookiesIntoStore(cookies, storeId);
    console.log(`[NBDpsy] 注入完成 ${injected} 成功 / ${failed} 失败`);

    // 6. 导航到小红书（无痕窗 tab 可能异步就绪，兜底查询一次）。
    let tabId = win.tabs && win.tabs[0] && win.tabs[0].id;
    if (tabId == null) {
        try {
            const tabs = await chrome.tabs.query({ windowId: windowId });
            if (tabs && tabs.length > 0) tabId = tabs[0].id;
        } catch (e) { /* 忽略 */ }
    }
    if (tabId != null) {
        try {
            await chrome.tabs.update(tabId, { url: 'https://www.xiaohongshu.com' });
        } catch (e) {
            console.warn('[NBDpsy] 导航到小红书失败:', e.message);
        }
    }

    return { success: true, injected, failed, windowId, accountId };
}

// 采集当前会话 cookie + 活动标签页用户信息后推送后台（快速同步路径）。
async function syncCurrentSession() {
    const cookies = await collectXHSCookies();
    if (cookies.length === 0) {
        return { success: false, error: '未采集到小红书 Cookies，请先登录' };
    }

    // 从当前活动标签页尝试读取用户信息（失败不阻断，用户信息可选）
    let userInfo = null;
    try {
        const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
        if (tab && tab.url && tab.url.includes('xiaohongshu.com')) {
            const resp = await chrome.tabs.sendMessage(tab.id, { action: 'getUserInfo' });
            if (resp && resp.success) userInfo = resp.userInfo;
        }
    } catch (e) { /* 用户信息可选 */ }

    const accountName = userInfo?.nickname
        || (userInfo?.user_id ? `xhs_${userInfo.user_id}` : `xhs_account_${Date.now()}`);
    return pushCookies({ accountName, cookies, userInfo });
}

// 远程采集是最长 5 分钟的流程，而点击后 popup 会因新窗口聚焦立即销毁、消息回调随之断裂。
// 因此结果不走 sendResponse，改写入 storage + 打扩展徽标；popup 下次打开时读取并展示。
async function finishRemoteLogin(result) {
    await chrome.storage.local.set({ remoteLoginResult: { ...result, ts: Date.now() } });
    try {
        await chrome.action.setBadgeText({ text: result.success ? '✓' : '!' });
        await chrome.action.setBadgeBackgroundColor({ color: result.success ? '#28a745' : '#dc3545' });
    } catch (e) { /* 徽标可选，失败不影响采集结果落地 */ }
}

// 打开无痕账号会话点击后，聚焦到新无痕窗口会立即销毁 popup、断裂消息回调，
// 故结果不走 sendResponse，改写入 storage + 打扩展徽标；popup 下次打开或仍在时读取展示。
async function finishAccountSession(result) {
    await chrome.storage.local.set({ accountSessionResult: { ...result, ts: Date.now() } });
    try {
        await chrome.action.setBadgeText({ text: result.success ? '✓' : '!' });
        await chrome.action.setBadgeBackgroundColor({ color: result.success ? '#28a745' : '#dc3545' });
    } catch (e) { /* 徽标可选，失败不影响注入结果落地 */ }
}

// 监听来自 popup 的消息
chrome.runtime.onMessage.addListener((request, sender, sendResponse) => {
    console.log('[NBDpsy] 收到消息:', request.action);

    switch (request.action) {
        case 'collectCookies':
            collectXHSCookies()
                .then(cookies => sendResponse({ success: true, cookies }))
                .catch(error => sendResponse({ success: false, error: error.message }));
            return true;

        case 'checkLogin':
            checkLoginStatus()
                .then(status => sendResponse({ success: true, status }))
                .catch(error => sendResponse({ success: false, error: error.message }));
            return true;

        case 'getConfig':
            getConfig().then(cfg => sendResponse({ success: true, ...cfg }));
            return true;

        case 'setConfig':
            chrome.storage.local.set(
                { serverUrl: request.serverUrl, apikey: request.apikey },
                () => sendResponse({ success: true })
            );
            return true;

        case 'syncCurrentSession':
            syncCurrentSession()
                .then(result => sendResponse(result))
                .catch(error => sendResponse({ success: false, error: error.message }));
            return true;

        case 'startRemoteLogin':
            // 立即 ack（不依赖 popup 存活）；真正结果经 finishRemoteLogin 写 storage
            sendResponse({ success: true, started: true });
            startRemoteLogin()
                .then(r => finishRemoteLogin(r))
                .catch(e => finishRemoteLogin({ success: false, error: e.message }));
            return false;

        case 'openAccountSession':
            // 立即 ack（聚焦新无痕窗会销毁 popup）；注入结果经 finishAccountSession 写 storage
            sendResponse({ success: true, started: true });
            openAccountSession(request.accountId)
                .then(r => finishAccountSession(r))
                .catch(e => finishAccountSession({ success: false, error: e.message }));
            return false;

        default:
            sendResponse({ success: false, error: '未知操作' });
    }
});

// 扩展安装/更新时初始化默认服务器地址
chrome.runtime.onInstalled.addListener((details) => {
    console.log('[NBDpsy] 扩展已安装/更新:', details.reason);
    chrome.storage.local.get(['serverUrl'], (result) => {
        if (!result.serverUrl) {
            chrome.storage.local.set({ serverUrl: DEFAULT_SERVER_URL });
        }
    });
});

console.log('[NBDpsy] Service Worker 已启动');
