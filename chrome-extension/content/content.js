/**
 * NBDpsy 小红书账号助手 - 内容脚本 (Content Script)
 *
 * 功能：
 * 1. 从小红书页面采集用户信息（供 service worker 的登录采集流程调用）
 * 2. 检测页面登录状态
 *
 * 说明：旧仓里向网页 postMessage 的 EXTENSION_READY / CHECK_EXTENSION 握手是给
 * 旧后台前端桥（externally_connectable）用的，本仓改由 popup + apikey 驱动，已删除。
 */

(function () {
    'use strict';

    console.log('[NBDpsy] 内容脚本已加载');

    // 采集用户信息（选择器与后端个人主页解析保持一致）
    function collectUserInfo() {
        try {
            const result = {
                nickname: null,
                red_id: null,
                user_id: null,
                avatar: null,
                bio: null,
                follow_count: null,
                fans_count: null,
                like_count: null,
                total_notes: 0,
                debug_info: []
            };

            // 从 URL 获取真正的用户 ID
            const url = window.location.href;
            const userIdMatch = url.match(/user\/profile\/([a-zA-Z0-9]+)/);
            if (userIdMatch) {
                result.user_id = userIdMatch[1];
                result.debug_info.push('从URL获取到用户ID: ' + result.user_id);
            }

            // 获取昵称（多种选择器尝试）
            const nicknameElem = document.querySelector('.user-name') ||
                document.querySelector('[class*="user-name"]') ||
                document.querySelector('.username') ||
                document.querySelector('[class*="username"]');
            if (nicknameElem) {
                result.nickname = nicknameElem.textContent.trim();
                result.debug_info.push('找到昵称元素');
            } else {
                result.debug_info.push('未找到昵称元素');
            }

            // 获取头像
            const avatarElem = document.querySelector('.user-avatar img') ||
                document.querySelector('[class*="avatar"] img') ||
                document.querySelector('img[src*="sns-avatar"]');
            if (avatarElem && avatarElem.src) {
                result.avatar = avatarElem.src;
                result.debug_info.push('找到头像');
            }

            // 获取小红书号（通常在昵称下方）
            const userInfoElems = document.querySelectorAll('.user-info span, .user-redId, [class*="user-id"], [class*="red-id"]');
            for (const elem of userInfoElems) {
                const text = elem.textContent.trim();
                // 小红书号格式：小红书号：xxxxx
                if (text.includes('小红书号') || text.includes('redId')) {
                    const match = text.match(/[：:]\s*([a-zA-Z0-9_-]+)/);
                    if (match) {
                        result.red_id = match[1];
                        result.debug_info.push('找到小红书号: ' + result.red_id);
                    }
                }
            }

            // 获取简介
            const bioElem = document.querySelector('.user-desc, .user-bio, [class*="user-desc"], [class*="bio"]');
            if (bioElem) {
                result.bio = bioElem.textContent.trim();
                result.debug_info.push('找到简介');
            }

            // 获取关注数、粉丝数、获赞与收藏数
            const statsElems = document.querySelectorAll('.user-interactions span, .count, [class*="count"]');
            const statsTexts = Array.from(statsElems).map(el => el.textContent.trim());

            // 查找包含"关注"、"粉丝"、"获赞与收藏"的元素
            for (let i = 0; i < statsTexts.length; i++) {
                const text = statsTexts[i];
                if (text.includes('关注') && i > 0) {
                    result.follow_count = statsTexts[i - 1];
                }
                if (text.includes('粉丝') && i > 0) {
                    result.fans_count = statsTexts[i - 1];
                }
                if ((text.includes('获赞') || text.includes('收藏')) && i > 0) {
                    result.like_count = statsTexts[i - 1];
                }
            }

            // 获取笔记数量（统计 class="footer" 的元素数量）- 与后端逻辑一致
            const notesContainerXPath = '/html/body/div[2]/div[1]/div[2]/div[2]/div/div[3]';
            const notesContainerResult = document.evaluate(
                notesContainerXPath,
                document,
                null,
                XPathResult.FIRST_ORDERED_NODE_TYPE,
                null
            );
            const notesContainer = notesContainerResult.singleNodeValue;

            if (notesContainer) {
                const footerElems = notesContainer.querySelectorAll('.footer');
                result.total_notes = footerElems.length;
                result.debug_info.push(`找到笔记容器，包含 ${footerElems.length} 个笔记`);
            } else {
                result.debug_info.push('未找到笔记容器');
            }

            result.debug_info.push(`当前 URL: ${window.location.href}`);

            console.log('[NBDpsy] 采集到用户信息:', result);
            return result;

        } catch (error) {
            console.error('[NBDpsy] 采集用户信息失败:', error);
            return {
                nickname: null,
                user_id: null,
                red_id: null,
                avatar: null,
                bio: null,
                follow_count: null,
                fans_count: null,
                like_count: null,
                total_notes: 0,
                debug_info: ['采集失败: ' + error.message]
            };
        }
    }

    // 检测页面是否已登录（检查"我"链接 / 登录按钮）
    function checkPageLogin() {
        try {
            // 检查是否有登录按钮（表示未登录）
            const loginButton = document.querySelector('#login-btn, .login-btn, button.login-btn');

            // 检查是否有"我"链接（表示已登录）
            // 新 DOM：<a class="link-wrapper" href="/user/profile/xxx"><span class="channel">我</span></a>
            // 老 DOM：<a class="link-wrapper" href="/user/profile/xxx" title="我">
            let myProfileLink = document.querySelector('a.link-wrapper[title="我"], a[title="我"]');
            if (!myProfileLink) {
                const profileLinks = document.querySelectorAll('a.link-wrapper[href*="/user/profile/"], a[href*="/user/profile/"]');
                for (const a of profileLinks) {
                    const ch = a.querySelector('.channel');
                    if (ch && ch.textContent.trim() === '我') {
                        myProfileLink = a;
                        break;
                    }
                }
            }

            // 检查是否有用户头像
            const userAvatar = document.querySelector(
                '.reds-avatar img[src*="sns-avatar"], ' +
                '.reds-avatar img[src*="avatar"], ' +
                'img.reds-img[src*="sns-avatar"]'
            );

            // 从"我"链接中提取用户 ID
            let userId = null;
            if (myProfileLink) {
                const href = myProfileLink.getAttribute('href');
                if (href) {
                    const match = href.match(/\/user\/profile\/([a-zA-Z0-9]+)/);
                    if (match) {
                        userId = match[1];
                    }
                }
            }

            // 判断登录状态：有"我"链接且没有登录按钮
            const isLoggedIn = !!myProfileLink && !loginButton;

            console.log('[NBDpsy] 登录检测:', {
                hasLoginButton: !!loginButton,
                hasMyProfileLink: !!myProfileLink,
                hasUserAvatar: !!userAvatar,
                userId: userId,
                isLoggedIn: isLoggedIn
            });

            return {
                isLoggedIn: isLoggedIn,
                hasUserAvatar: !!userAvatar,
                hasMyProfileLink: !!myProfileLink,
                hasLoginButton: !!loginButton,
                userId: userId,
                profileUrl: myProfileLink ? myProfileLink.href : null
            };
        } catch (error) {
            console.error('[NBDpsy] 检测登录状态失败:', error);
            return {
                isLoggedIn: false,
                hasUserAvatar: false,
                hasMyProfileLink: false,
                hasLoginButton: true,
                userId: null,
                profileUrl: null
            };
        }
    }

    // 点击"我"进入个人主页
    function clickMyProfile() {
        try {
            const myProfileLink = document.querySelector('a.link-wrapper[title="我"], a[title="我"]');
            if (myProfileLink) {
                console.log('[NBDpsy] 点击"我"进入主页');
                myProfileLink.click();
                return { success: true };
            } else {
                return { success: false, error: '找不到"我"链接' };
            }
        } catch (error) {
            return { success: false, error: error.message };
        }
    }

    // 监听来自 popup 或 background 的消息
    chrome.runtime.onMessage.addListener((request, sender, sendResponse) => {
        console.log('[NBDpsy] 内容脚本收到消息:', request.action);

        switch (request.action) {
            case 'getUserInfo':
                sendResponse({ success: true, userInfo: collectUserInfo() });
                break;

            case 'checkPageLogin':
                sendResponse({ success: true, loginStatus: checkPageLogin() });
                break;

            case 'clickMyProfile':
                sendResponse(clickMyProfile());
                break;

            default:
                sendResponse({ success: false, error: '未知操作' });
        }

        return true;
    });

})();
