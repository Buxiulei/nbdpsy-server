# chrome 插件交互精简(账号管理器化)设计

**日期**:2026-07-15
**决策**:插件从"当前标签页 cookie 采集器"精简为"**我的账号管理器**"——只围绕后台托管账号,
所有小红书会话走无痕窗。移除当前页 cookie 采集与当前标签页状态展示;只留 apikey 录入 +
账号列表 + 点卡注入开无痕 + 无痕登录采集 + per-account 验活五条。

## 背景(已核实现状)

现 popup(v2.0.4)含:配置(server-url + apikey)、当前标签页状态指示 + 用户信息区(头像/昵称/
cookie 计数)、「同步当前账号」抓当前页 cookie、账号列表(点卡→注入无痕窗)、「远程登录采集」
开无痕登录、「打开小红书」。

service-worker 现有函数:
- 保留:`openAccountSession(accountId)`(开无痕 + 注入该号 cookie + 导航小红书)、
  `startRemoteLogin()`(开无痕人工登录 + 采 cookie 推 `/api/cookies/import`)、
  `fetchAccountCookies(accountId)`(拉 `/api/accounts/{id}/cookies`)、账号验活(`/api/accounts/{id}/cookie-checks`)。
- 移除:`syncCurrentSession()` / `collectXHSCookies()` / `checkLoginStatus()`(读当前标签页 cookie)。

## 目标交互(五条"只需要")

1. **录 apikey**:主界面唯一必填。server-url 折叠进"高级设置",默认 `https://mcp.nbdpsy.com`。
2. **看我的账号**:apikey 拉后台托管账号列表(归属当前 operator),卡片展示名称 + cookie_status 徽标。
3. **点卡注入开无痕**:点账号卡 → `openAccountSession` 开无痕窗 + 注入该号 cookie + 打开小红书。
4. **无痕登录采集**:「打开隐私窗口登录(加/换号)」按钮 → `startRemoteLogin` 开无痕人工登录 → 采 cookie 上传。
5. **per-account 验活**:每张卡一个「检测」按钮 → 调 `/api/accounts/{id}/cookie-checks` 轮询到终态,
   徽标随之更新;`error` 态标注"非 cookie 失效"不误伤。

## 移除项(YAGNI / 你的"不需要")

- 「同步当前账号」按钮 + `syncAccount`/`syncCurrentSession`/`collectXHSCookies`(当前页 cookie 采集)。
- 当前标签页状态指示器 + 用户信息区(头像/昵称/cookie 计数)+ `checkLoginStatus`/`getUserInfo`。
- 「打开小红书」普通标签按钮(btn-open-xhs)——开小红书统一走点卡注入无痕。
- 上述元素牵连的 popup.css 样式块、service-worker 死代码一并清(仅清本次移除产生的孤儿,不动无关代码)。

## 改动文件

- `popup/popup.html`:删状态指示 / 用户信息区 / btn-sync / btn-open-xhs;config 区 server-url 折叠;
  保留 apikey + 账号列表 + 无痕登录按钮。
- `popup/popup.js`:删 `syncAccount`/`checkLoginStatus`/`getUserInfo` 及其 storage 监听 / 元素引用;
  保留 `loadConfig`/账号列表渲染/点卡注入/无痕登录/per-card 验活轮询。
- `background/service-worker.js`:删 `syncCurrentSession`/`collectXHSCookies`/`checkLoginStatus` 及
  其 message 分派;保留 `openAccountSession`/`startRemoteLogin`/`fetchAccountCookies`/`pushCookies`/验活。
  `permissions`:**默认保留全部权限**。`webRequest` 极可能被 `startRemoteLogin` 采 cookie(监听
  Set-Cookie / `parseSetCookieHeader`)依赖,`cookies` 被注入 + 无痕采集依赖——除非逐一证实某权限
  **仅**被已删的当前页采集用到,否则不删(误删权限会静默废掉无痕流程,风险 >> 清理收益)。
- `popup/popup.css`:清理被删元素样式,轻度视觉统一(沿用现有语言,不大改)。
- `manifest.json`:version `2.0.4` → `2.1.0`。

## 错误处理(沿用现有防线)

- 未授无痕权限:`chrome.windows.create({incognito:true})` 返 null → 现有 null 守卫,中文指引去
  `chrome://extensions` 开"无痕模式下启用",不读 `.id` 报天书。
- apikey 未填/无效:列账号 401 → 提示"请先填写并保存有效 apikey",不静默空列表。
- 无痕登录 5 分钟长流程:popup 因新窗聚焦销毁后,结果经 `chrome.storage.local`
  (`remoteLoginResult`/`accountSessionResult`)回传,重开 popup 仍能看到终态(现有机制保留)。

## 验证(插件无自动化测试,手工走查)

1. load unpacked 加载 2.1.0。
2. 填 apikey 保存 → 账号列表渲染出归属账号 + 徽标。
3. 点一张卡 → 开无痕窗 + 注入 + 小红书已登录态。
4. 点「打开隐私窗口登录」→ 无痕窗人工登录 → 采集成功 → 后台新增/更新账号。
5. 点某卡「检测」→ 轮询到有效/失效/异常,徽标更新。
6. 确认已无"同步当前账号"/当前页状态/打开小红书普通标签入口。
7. 回归:注入 / 无痕登录 / 验活三条无痕流程功能不变。

## 明确不做(YAGNI)

- 不做全局一键检测(per-card 检测足够)。
- 不删 server-url(仅折叠;纯删会锁死域名,自建/换域名无法用)。
- 不重写视觉体系(沿用现有 popup.css 语言 + 轻度统一)。
- 不动后台 REST(能力已全,插件仅调用方)。
