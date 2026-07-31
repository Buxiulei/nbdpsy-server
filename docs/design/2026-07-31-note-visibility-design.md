# 笔记可见性(仅自己可见 / 所有人可见)设计

日期:2026-07-31
状态:真号受控实验已完成,待实现

## 一、需求

把已发布的笔记设为「仅自己可见」,以及把「仅自己可见」的笔记改回「所有人可见」;
**这个状态要落到数据库里**。

## 二、实验结论(真号受控实验,账号 NBDpsy-我们都有病)

### 2.1 可见性字段语义(零状态变更即可确定)

基线响应里天然存在两组对照样本,不需要做任何切换就得出结论:

| 字段 | 公开 | 仅自己可见 | 是否编码可见性 |
|---|---|---|---|
| `permission_code` | `0` | `1` | **是** |
| `permission_msg` | `""` | `"仅自己可见"` | **是**(文案) |
| `tab_status` | 1 | 1 | 否(恒为 1) |
| `sticky` | 置顶为 true | — | 否(置顶的公开笔记 sticky=true) |

两档**同在 `tab=0` 下,不分 tab**;`data.tags` 只有一项「所有笔记」,没有可见性维度的 tab。
视觉标记是封面左上角一枚「仅自己可见」文字角标,公开笔记无可见性角标。

### 2.2 提交变更的接口契约(零净变更实验抓到)

```
POST https://edith.xiaohongshu.com/web_api/sns/v1/note/privacy
     ?note_id=<id>&privacy=1&user_ids=&source=2
请求体:无(参数全在 query string)
响应 200:{"data":{"msg":"笔记已设置为仅自己可见"},"code":0,"success":true,"msg":"成功"}
```

三点注意:域名是 `edith.xiaohongshu.com` **不是** creator;POST 但参数走 query string;
`user_ids` 空,应是留给「部分人可见/不可见」两档。

**只实测了 `privacy=1`**。`privacy=0`(公开)是从公开笔记 `permission_code=0` 推断的,
另外三档与 `user_ids` 格式**完全未验证**。

**不要默认这个接口能从 Python 直调**:`edith.xiaohongshu.com/web_api/*` 这类接口通常要求
前端 JS 计算的 `X-s` / `X-t` 签名头,本次没记请求头,无法确认。**本期一律驱动 UI**,
想走直调必须先补一次抓包确认签名头。

### 2.3 页面定位(tooltip 那条路是死的)

上一轮设想的"用 tooltip 文案「权限设置」定位"**不可用**——逐个悬停 4 个按钮,
`[class*=tooltip]` / `[role=tooltip]` / `[class*=popper]` 全部返回空。

实测的真实结构:卡片 `.note-card`(列表页卡内 `.title` 取不到,标题只能从 `innerText` 读);
悬停后 4 个 `.note-card__action-btn`:

```
note-card__action-btn                                  ← ①权限设置
note-card__action-btn note-card__action-btn--disabled  ← ②置顶(私密笔记上是禁用态)
note-card__action-btn                                  ← ③编辑
note-card__action-btn note-card__action-btn--del       ← ④删除
```

**最危险的一点:①和③ 的 class 完全相同,只能靠 DOM 顺序区分。** 顺序一变就会静默点到编辑。

弹窗 `.d-modal.d-modal-default.d-modal-centered.permission-modal`;下拉
`.perm-select-wrapper`(其 `innerText` 就是当前档位文案,**可不展开直接只读回读当前值**);
展开后 `.d-options-wrapper .custom-option` 五项按 公开可见 / 仅自己可见 / 仅互关好友可见 /
部分人可见 / 部分人不可见 排,选中项带 `--color-primary`;底部两个 `d-button` 靠 `innerText`
精确匹配「取消」「确定」;右上角关闭叉 `.d-modal-close`。

### 2.4 顺带查出的既有缺陷:台账分页只抓到 20/37

接口自报账号1 有 **37 篇**,但翻页只出 2 批共 20 篇就不再请求(连滚 4 次未触发新分页)。
已上线的 `note_ledger` 同步因此**长期漏掉 17 篇**。这是独立于本需求的既有缺口,
但补可见性入库时会直接踩到,**必须一并修**。

## 三、设计

### 3.1 落库字段(published_notes 新增)

**存平台原值,不自己发明映射**——只实测了 2 档,另外 3 档语义未知,自造
`public`/`private` 枚举会在遇到第三态时丢信息或误判。

- `permission_code` INTEGER NULL —— 平台原值(0=公开 / 1=仅自己可见 / 其余未知)
- `permission_msg` TEXT NULL —— 平台原文案
- `visibility_changed_at` DATETIME NULL —— **我们主动**切换成功的时刻
- `visibility_changed_by` INTEGER NULL —— 发起切换的 operator_id

前两个是平台侧事实(T2 同步纠正);后两个是我们自己的操作留痕。NULL 表示**未知**,
不等于公开——今天就是因为台账缺这个字段,把一篇用户刻意隐藏的笔记误判成"低价值公开笔记"。

### 3.2 同步纠正(T2)

`_apply_platform_fields` 增加 `permission_code` / `permission_msg` 的覆盖,比照 `title`
现有纪律(平台权威、可覆盖)。这样运营在 APP 上手改可见性也会被定时同步纠正回台账。
不动 `visibility_changed_at` / `visibility_changed_by`(那是我们自己的操作留痕)。

### 3.3 切换操作

新 kind `note_visibility`,payload `{note_id, title, target_privacy}`。

**执行路径(全程驱动 UI,零 JS 注入,全部走 SyncHumanActions)**:

1. 进笔记管理页,按 **title 精确匹配**定位卡片,**命中数必须恰好为 1**;
2. 悬停出图标,断言 `.note-card__action-btn` **恰好 4 个**,且 `btns[0].className`
   **严格等于** `'note-card__action-btn'`(完全相等、不带任何修饰类),`btns[3]` 含 `--del`;
3. 点 `btns[0]`,随即**事后校验**:`.permission-modal` 存在且可见,且**所有可见 dialog 的
   文案都不含「删除」**——不满足立刻点弹窗内「取消」中止;
4. 只读回读 `.perm-select-wrapper` 当前档位;**已经是目标档位就直接点「取消」返回
   `skipped`**,不做无谓提交;
5. 展开下拉,按 `innerText` 精确匹配目标档位文案点选,再点「确定」;
6. **回读校验**:重新抓一次 posted 接口,确认该 note_id 的 `permission_code` 已变为目标值,
   变了才算 `done`;没变返回 error,**绝不"点了就当成功"**。

**硬约束**:

- **绝不用坐标启发式定位图标**(如"最右边""第几个")。上一轮调研就是用"最右侧图标"
  当更多菜单,误命中删除并弹出删除确认框,只因后续点击坐标没撞上确认按钮才没删成。
- **Escape 关不掉这条产品线的弹窗**(实测按了仍开着)。退出只能点弹窗内「取消」。
- 第 3 步的事后校验是**唯一挡住误点删除的东西**,实现里绝不能省。

**定位限制(如实记录)**:靠标题定位,则**标题为空或在该号下重复的笔记无法定位**。
账号1 那 3 篇无标题私密笔记就属于这种。遇到这类情况返回
`note_not_locatable`,**绝不猜**。这是可接受的 v1 限制。

**幂等性**:`note_visibility` **不进** `_IDEMPOTENT_KINDS`。虽然重复设同一档位在平台侧是
无害的(实验证实了),但僵死自动重跑有实际危险:若期间运营手工改回了公开,过期任务重跑
会**再次把它藏起来**。与 `note_delete` / `matrix_interact` 同款纪律,僵死置 error 不自动重跑。

### 3.4 分页缺口修复

修 `creator_note_list.py` 的翻页终止条件,把 37 篇全部抓全。可用 `data.tags[0].notes_count`
(实测 37)作为期望总数做校验:抓到的条数少于它就说明翻页提前终止,应告警而非静默成功。

## 四、本期不做

- `privacy=0/2/3/4` 与 `user_ids` 格式未验证,**只做「公开 ↔ 仅自己可见」二档**,
  其余三档不实现;
- 不走接口直调(签名头未确认),一律驱动 UI;
- 不做无标题/重复标题笔记的定位(见 3.3 限制)。

## 五、验收

1. 单元测试:目标档位已达成时 `skipped` 不提交;回读未变时落 error 不算成功;
   标题为空/重复时返回 `note_not_locatable` 不猜;`note_visibility` 未进 `_IDEMPOTENT_KINDS`;
   `permission_code` → 落库与 T2 覆盖;分页抓不满 `notes_count` 时告警。
2. 真号 e2e:**只允许拿一篇本来就是「仅自己可见」的笔记做幂等重设**(设成它已经是的档位),
   验证链路通且状态不变。**绝对不允许把用户已隐藏的笔记改成公开**——用户名下 10 篇私密
   笔记是他长期有意隐藏的(2025 年个人向老帖 + 3 篇无标题),擅自公开是不可挽回的对外发布。
3. 同步后核对:账号1 台账里 `permission_code` 10 篇为 0、10 篇为 1,与实验清单一致;
   且总数从 20 补齐到 37。
4. 全程 headed 真屏 + SyncHumanActions,零 JS 注入式点击。
