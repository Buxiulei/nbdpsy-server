"""小红书登录状态检测 JS(纯常量,无浏览器依赖)。

移植自旧仓 ``app/utils/login_detector.py``,只保留可复用的 JS 载荷:

- ``DETECT_LOGIN_JS``  —— 在 explore 页执行,判定登录态(返回信号 + 结论)
- ``GET_USER_INFO_JS`` —— 在个人主页执行,抓取昵称/小红书号/粉丝数等

旧仓那几个 async 编排函数(``detect_login_status`` / ``check_and_get_info``)
依赖 ``playwright.async_api``,与本仓 sync 浏览器层不匹配,故不移植 ——
由上层(check_cookies / 发布链路)用 sync page ``evaluate`` 直接调这两段 JS。

基于实际 DOM 对比研究结论(2026-03-27),只用 4 个确定性标志:
1. .channel 文本 "我"      → 仅登录时出现
2. CSS class "user"        → 仅登录时出现
3. CSS class "reds-avatar" → 仅登录时出现
4. CSS class "login-btn"   → 仅未登录时出现
"""

# ── 统一登录检测 JS ──
# 只用研究确认的 4 个确定性标志,不用 profileLinks/avatarCount 等不可靠指标
DETECT_LOGIN_JS = """
() => {
    const result = {
        is_logged_in: false,
        reason: '',
        profile_url: null,
        signals: {
            has_me_channel: false,
            has_user_class: false,
            has_reds_avatar: false,
            has_login_btn: false,
            login_btn_count: 0,
        }
    };

    // ━━━ 信号 1：.channel 文本为 "我"（最可靠正向标志） ━━━
    const channels = document.querySelectorAll('.channel');
    for (const ch of channels) {
        if (ch.textContent.trim() === '我') {
            result.signals.has_me_channel = true;
            // 提取 profile URL
            const link = ch.closest('a') || ch.parentElement?.closest('a');
            if (link && link.href && link.href.includes('/user/profile/')) {
                result.profile_url = link.href;
            }
            break;
        }
    }

    // ━━━ 信号 2：CSS class "user"（侧边栏用户信息容器） ━━━
    result.signals.has_user_class = !!document.querySelector('.user');

    // ━━━ 信号 3：CSS class "reds-avatar"（用户自己的头像） ━━━
    result.signals.has_reds_avatar = !!document.querySelector('.reds-avatar');

    // ━━━ 信号 4：CSS class "login-btn"（未登录时的登录按钮） ━━━
    const loginBtns = document.querySelectorAll('.login-btn');
    result.signals.login_btn_count = loginBtns.length;
    result.signals.has_login_btn = loginBtns.length > 0;

    // ━━━ 判定逻辑 ━━━
    if (result.signals.has_login_btn) {
        // 有 login-btn → 确定未登录
        result.is_logged_in = false;
        result.reason = '检测到登录按钮 (.login-btn)';
    } else if (result.signals.has_me_channel) {
        // 有 "我" channel → 确定已登录
        result.is_logged_in = true;
        result.reason = '检测到"我"导航栏';
    } else if (result.signals.has_user_class && result.signals.has_reds_avatar) {
        // 有 .user + .reds-avatar → 大概率已登录
        result.is_logged_in = true;
        result.reason = '检测到用户信息区域 (.user + .reds-avatar)';
    } else {
        // 都没有 → 未登录
        result.is_logged_in = false;
        result.reason = '无任何登录标志';
    }

    return result;
}
"""

# ── 获取用户信息的 JS(在个人主页执行) ──
# DOM 结构(2026-04-01 验证):
#   .user-name / .user-nickname — 昵称
#   .user-image — 头像 img
#   .user-redId — "小红书号:xxx"
#   .user-desc — 个人简介
#   .user-interactions > div > .count + .shows — 关注/粉丝/获赞与收藏
GET_USER_INFO_JS = """
() => {
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
    };

    // 从 URL 获取 user_id
    const m = window.location.href.match(/user\\/profile\\/([a-zA-Z0-9]+)/);
    if (m) result.user_id = m[1];

    // 昵称（.user-name 或 .user-nickname）
    const nameEl = document.querySelector('.user-name') || document.querySelector('.user-nickname');
    if (nameEl) result.nickname = nameEl.textContent.trim();

    // 头像（.user-image 是个人主页的头像 img）
    const avatarEl = document.querySelector('.user-image')
                  || document.querySelector('.avatar-wrapper img');
    if (avatarEl && avatarEl.src) result.avatar = avatarEl.src;

    // 小红书号（.user-redId 包含 "小红书号：xxx"）
    const redIdEl = document.querySelector('.user-redId');
    if (redIdEl) {
        const m2 = redIdEl.textContent.trim().match(/小红书号[：:]\\s*([a-zA-Z0-9_-]+)/);
        if (m2) result.red_id = m2[1];
    }

    // 简介
    const bioEl = document.querySelector('.user-desc');
    if (bioEl) result.bio = bioEl.textContent.trim();

    // 关注/粉丝/获赞与收藏
    // 结构：.user-interactions > div > span.count + span.shows
    const interactionDivs = document.querySelectorAll('.user-interactions > div');
    interactionDivs.forEach(div => {
        const countEl = div.querySelector('.count');
        const showsEl = div.querySelector('.shows');
        if (!countEl || !showsEl) return;
        const label = showsEl.textContent.trim();
        const value = countEl.textContent.trim();
        if (label.includes('关注')) result.follow_count = value;
        else if (label.includes('粉丝')) result.fans_count = value;
        else if (label.includes('获赞')) result.like_count = value;
    });

    // 笔记数(feed-type 区域的 tab 数字,或直接计数笔记卡片)
    const noteItems = document.querySelectorAll('.note-item, section.note-item');
    result.total_notes = noteItems.length;

    return result;
}
"""
