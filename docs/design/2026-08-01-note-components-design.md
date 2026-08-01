# 笔记三组件(合集 / 引用笔记 / 关联活动)+ 编辑已发布笔记 设计

日期:2026-08-01
状态:真号受控写测试已完成,待实现

## 一、需求

发布笔记时、以及编辑已发布笔记时,支持:

1. **加入合集** —— 默认加入「咨询师简介」;
2. **引用笔记** —— 推介哪个咨询师就引用哪篇咨询师推介笔记;若本身就是咨询师推介笔记,
   则引用「小助手联系方式」那篇;
3. **关联活动** —— 关联与心理学相关的活动蹭流量。

**全部要开成 REST 供外部 skill 调用。**

## 二、真号实验结论(受控写测试,账号1,私密笔记 `6a4ce556...`)

### 2.1 编辑页可深链直达(最大风险消除)

```
https://creator.xiaohongshu.com/publish/update?id={note_id}&noteType=normal
```

三次独立验证成功。**不必再走"笔记管理页悬停点第 3 个图标"那条路**——那条路上
①权限设置 与 ③编辑 的 class 完全相同、④是删除,调研期间已真的弹出过删除确认框。

编辑页是骨架屏,`networkidle` 不够,要**轮询等「内容设置」文案出现**(实测约 1.5-2s)。

### 2.2 整体提交,一次 PUT 落库(已实证)

三个组件选中时**一个请求都不发**(实测三次选中操作的非 GET 请求数均为 0),全是前端状态。
真正落库只有:

```
PUT https://edith.xiaohongshu.com/web_api/sns/capa/postgw/note/update
→ {"result":0,"success":true,"msg":"","data":{"id":"...","score":10},"share_link":"..."}
```

### 2.3 提交载荷是**全量快照**,不是 patch

完整结构(值缩略,键名结构完整):

```
common:
  type, note_id, source, title(""也显式传), desc, ats[], hash_tag[],
  business_binds(JSON 字符串), post_loc, post_locs,
  privacy_info: {op_type:1, type:1, user_ids:[]},   ← type=1 即仅自己可见
  goods_info, metadata(JSON 字符串), biz_relations[], capa_trace_info
image_info: images[{file_id, width, height, metadata, stickers, extra_info_json}]
video_info: null
```

`business_binds` 反序列化后(注意它是**字符串,二次序列化**):

```json
{"version":1,"bizType":0,
 "noteCollectionBind":{"id":"6a69e9e316fb000000000001"},      ← 合集
 "optionRelationList":[
   {"type":"REF_POST","relationList":[{"bizId":"<被引用note_id>",...}]},   ← 引用
   {"type":"ACTIVITY_COMPONENT","relationList":[{"bizId":"43561",
     "extraInfo":"{\"name\":\"身边的心理学\",\"start_time\":...,\"end_time\":...}"}]}]}  ← 活动
```

**空值是显式传的**(`title:""`、`ats:[]`、`biz_relations:[]`、
`noteSketchCollectionBind.id:""`)。若后端是 patch 语义,前端没必要显式传空
——**强烈指向覆盖语义**。

> 实证与推断的边界:"漏带 `privacy_info` 会把私密笔记变公开"这条**没有实证**。
> 验证它必须构造残缺请求发出去,既违反"不构造请求"的铁律,后果又正是我们最怕的
> (把用户刻意隐藏的内容曝光)。故停在推断,实现按最坏情况防。

### 2.4 **绝不自己构造这个 PUT**(硬约束)

载荷里含编辑页加载时服务端下发的**会话态数据**:`metadata.history_id`、
`capa_trace_info.contextJson`、`source`、图片的 `file_id` 与 `extra_info_json`。
这些自己拼构造不出来,硬猜迟早出事;叠加覆盖语义,漏字段风险直通权限。

**正确姿势:走 UI 让前端自己序列化** —— 进编辑页 → 点组件 → 点发布。
这样全量字段天然是对的,`privacy_info` 天然跟页面当前状态走,不存在漏带。

### 2.5 发布按钮是 closed shadow DOM(任何选择器都穿不透)

```html
<xhs-publish-btn is-publish="true" submit-text="发布" save-text="暂存离开"
                 submit-disabled="false" submit-loading="false"></xhs-publish-btn>
```

`host.shadowRoot === null` 且 `childElementCount === 0`。querySelector、Playwright
locator、`page.evaluate` **全都穿不透**——上一轮按 `button,[role=button],.d-button`
+ 文本匹配返回空数组不是选择器写错,是那个 DOM 根本不可达。

**定位法(与 `atomic_tasks.py` step7 同款)**:取 host 的 `getBoundingClientRect`,
在该像素带内按小红书红筛 `r>180 && g<120 && b<140 && (r-g)>90 && (r-b)>60` 求质心;
点击前用 `elementFromPoint` 复核返回 `XHS-PUBLISH-BTN` 才落点,再 `human.click((x,y))`。

**不得写死坐标**:组件设置会改变页面高度顶动按钮,每次按 host rect 重算质心。

### 2.6 `success:true` 不等于设置生效(最要防的坑)

实测:私密笔记的 `noteCollectionBind` 被**服务端静默丢弃**——`success:true` 照返、
零 toast、零错误码,但回读时合集区仍是「选择合集」、`.close-icon` 数为 0。

已做对照实验排除"回显坏了":只读打开公开笔记 `6a6aa311...` 的编辑页,合集区正常回显
「咨询师简介」且有 1 个 close-icon。所以回显机制是好的,是服务端丢弃了。

**最合理解释:小红书不允许「仅自己可见」的笔记进合集。** 但只在私密笔记上验证过,
公开笔记能否绑上无直接证据(对照笔记本身在合集里,算间接支持)。

同批次里**引用笔记与关联活动都生效了**(回读到《心理咨询师-徐瑞恒…》、
活动卡显示「取消关联」并被顶到第一位)。

### 2.7 关联活动会**改写笔记正文**

关联「身边的心理学」后,提交载荷的 `desc` 从空变成 `#身边的心理学[话题]#`,
`hash_tag` 数组多了一条完整 topic 记录。发布后重进编辑页读回,内容仍在,确认落库。

**未查清**:正文本来有内容时是追加还是覆盖(本次样本正文为空,两种行为表现一致,
区分不了);取消关联会不会把标签删掉(全程未点「取消关联」)。**实现前必须先查清追加/覆盖**,
否则可能把真实笔记的正文覆盖掉。

### 2.8 各组件真实选择器

```
合集入口   .collection-plugin-button(未加入时文案「选择合集」)
合集弹层   .collection-plugin-popover .collection-plugin-popover-content > .item
           必须排除 .popover-footer(那是「创建合集」)
合集移除   .collection-plugin-choose 内的 .close-icon   ← 危险,操作时严格避开
引用入口   .quote-note-container
引用弹窗   .d-modal.select-note-modal
           → .select-note-modal__note-grid > .note-card
           → 文案「确认引用」的 button(选中前带 disabled class)
           两个 tab:我的笔记 / 他人笔记;底部为**单选**摘要区(一次只能引用 1 篇)
活动       .activity-card 内按 .activity-name 文案匹配,取同卡的 .activity-action
           这样天然避开 .activity-plugin-label .more 那个同名陷阱
           (页面上另有推荐话题区的「更多」,纯文本匹配会命中错的)
权限只读位 .permission-card-wrapper .d-select-description
```

### 2.9 读接口(可直接复用)

```
合集列表  POST /api/sns/v1/note/collection/pc/list_v2
          → data.collection_info_list[](id/name/desc/note_num)
          账号1 目前只有 1 个:「咨询师简介」id=6a69e9e316fb000000000001,
          已含 10 篇,简介「全员北大临床心理硕博咨询师」—— 正是需求要的,不用新建
引用候选  GET /api/galaxy/v2/creator/note/user/posted?tab=1&page=N
          ← 与笔记管理页同一接口,仅 tab 不同(管理页是 tab=0)
          **所以候选列表不需要新接口,查 published_notes 台账即可**
当前引用  GET /api/galaxy/v2/creator/edit/ref_info?ref_id=X&ref_type=note
活动列表  GET /api/galaxy/v2/creator/activity_center/list?sort=1&type=1&source=3&topic_activity=0
          页面加载时一次性返回**全部**(实测 181 条),点「更多」不发新请求
```

### 2.10 活动筛选规则

181 条里强相关只有 **「身边的心理学」**(08-01 至 09-30)一条。

**必须按 `name` + 活动简介**联合关键词匹配,只查 name 会假阳性
(实测「howto穿出自我」因"自我"命中,实为穿搭活动)。关键词建议
`心理/情绪/焦虑/抑郁/疗愈/精神/认知/内耗`,**不要**用"自我""成长"这类过宽的词。

**活动频繁上下线,绝不做死名单,每次现拉接口。**

## 三、设计

### 3.1 统一走 UI,不构造请求(总原则)

发布新笔记:在 `step6_set_publish_options` 之后、`step7_click_publish_and_wait` 之前
插入三组件设置。

编辑已发布笔记:深链进编辑页 → 设置组件 → 点发布(即更新)。

### 3.2 定位一律优先用 `note_id`,标题只作兜底

**已发现的真实问题**:平台上「心理咨询师-黄安麟…」的标题实际显示为
「**粤语**咨询师-黄安麟…」(三处独立证据:`ref_info` 响应体、两份 DOM 快照),
而台账里是「心理咨询师-…」。**台账 title 会过期。**

所有靠标题定位的既有功能(可见性切换、单篇评论、删除笔记)都因此可能失配。
本次一并改为:**有 `note_id` 就用深链/id 定位,标题仅在无 id 时兜底**。

### 3.3 每一步都必须回读校验

`success:true` 不可信(合集被静默丢弃就是活例子)。提交后必须重抓
`posted` 接口 / 重进编辑页回读,**逐项确认三个组件是否真的生效**,
未生效的要如实报 `partially_applied` 并列出哪项没成,不得整体报 done。

### 3.4 权限保全(硬约束)

编辑任何笔记前**必须先只读确认权限档位**;提交后**必须回读确认权限未变**。
若发现权限被改,立刻改回并大声告警。

理由:提交是全量覆盖语义,而用户名下有 28 篇刻意隐藏的私密笔记,
一次误操作就可能把它们曝光,不可逆。

### 3.5 REST 面

```
GET  /api/accounts/{id}/collections            合集列表(转发 list_v2)
GET  /api/accounts/{id}/activities             活动列表(转发 activity_center,带筛选参数)
POST /api/accounts/{id}/note-components  → 202 {job_id}
       note_id                必填,定位用
       collection_id          可选
       quoted_note_id         可选
       activity_id            可选
GET  /api/note-components/{job_id}             轮询,返回逐项生效情况
```

发布时设置则扩展 `POST /api/publish-jobs` 请求体(同名字段)。

**非幂等**,不进 `_IDEMPOTENT_KINDS`:重跑会重复提交、且活动会重复注入话题。

## 四、本期不做 / 待查

1. **正文注入是追加还是覆盖** —— 必须在实现前用一篇**有正文的私密笔记**查清;
2. **取消关联是否删除话题标签** —— 未观察;
3. **私密笔记不能进合集** —— 只在私密笔记验证过,公开笔记未直接验证;
4. **「小助手联系方式」笔记是否存在** —— 台账中未见此类标题,若确实没有,
   "咨询师推介笔记引用小助手笔记"这条规则**落不了地**,需先发一篇;
5. 合集数量多于 1 个时弹层是否变搜索/分页 —— 本号只有 1 个合集,无法验证;
6. 引用候选列表是否支持搜索/分页 —— 只见 19 张卡一次性渲染的初始态。

## 五、其他实现坑

- `business_binds` / `metadata` / `extraInfo` 是嵌了**两到三层**的 JSON 字符串,层数易错;
- `ark.xiaohongshu.com/api/edith/bridge/trade_note/permission` 在更新页加载时照常发出,
  被 PAC 黑洞挡成网络错误(有 request 无 response),编辑器全程未被踢登录页
  —— **现有 ark 黑洞防护在更新页同样有效,不要动它**;
- 更新态 host 属性 `is-save-draft="false"`,「暂存离开」按钮**不渲染**,底部只有「发布」一颗。
