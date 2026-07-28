# 设计 · 风格档案「多套 + 每套独立版本链」(profile_set)

> server 侧设计，对应需求 `NBDpsy/文档/2026-07-28-server需求-风格档案多套独立版本链.md`
> 现状(2026-07-28 实测):生产仅 2 份档案(管理员默认 + 运营3),**全是老平铺、0 个 profiles-v1 容器**,
> 版本历史 3 行。迁移窗口最优——趁还没有容器数据。

## 1. 核心:在 operator ↔ 档案之间加「套」这一层,版本链挂到套上

现状:`style_profiles`(operator_id 唯一,当前态) + `style_profile_versions`(operator_id, version, 版本链)。
一运营一份、整份版本化 → 回退误伤、并发假冲突、dropped_keys 稀释(见需求 §3)。

目标:一运营 N 套,每套独立当前态 + 独立版本链。

## 2. 数据模型:改造现有两表(不新建表,迁移最平滑)

**决策:改造 `style_profiles`/`style_profile_versions` 加列,不新建 `profile_sets` 表。**
理由:现有表语义天然对应(style_profiles=每套当前态、versions=版本链),改造只需给现有 2 行加列填值 +
版本行加 set 归属;新建表要搬数据 + 新旧并存过渡,对 2 份数据不值当。表名 `style_profiles` 理解为
「风格档案(每套一行)」语义不违和。

### `style_profiles`(每套当前态,一运营 N 行)
| 列 | 变化 | 说明 |
|---|---|---|
| id / operator_id / version / profile_json / source / note / updated_at / updated_by | 不变 | operator_id NULL = 管理员默认 |
| **name** | 新增 | 运营自定义,≤20 字符(按显示宽度截断,允许中文),`(operator_id, name)` 唯一 |
| **kind** | 新增 | `carousel`/`typeset`,server 不理解含义、原样存 |
| **is_active** | 新增 | bool,同一 operator 恒有且仅一个 True(应用层保证,非 DB 约束) |

- 唯一约束:`(operator_id)` → **`(operator_id, name)`**(SQLite 走 batch_alter_table)。
- `is_active` 的「唯一 True」:应用层在 PATCH set_active / 建套 / 删套时事务内维护(与「最后一个管理员」同款
  应用层不变量,不靠 DB 部分索引——SQLite 部分唯一索引对 NULL operator 处理易踩坑)。

### `style_profile_versions`(每套版本链)
| 列 | 变化 | 说明 |
|---|---|---|
| id / version / profile_json / source / note / created_at / created_by | 不变 | |
| **set_id** | 新增 | FK → style_profiles.id,取代原先用 operator_id 关联版本的方式 |
| operator_id | 保留但不再作版本归属键 | 迁移期兼容;唯一约束从 `(operator_id, version)` → **`(set_id, version)`** |

## 3. 端点

### 现有 6 端点:全部加 `?set={name}` 可选参数
| 端点 | 不带 set(铁律:与今天一致) | 带 set |
|---|---|---|
| GET /api/style-profile | 返回 **is_active** 那套当前版本 | 返回该套 |
| PUT /api/style-profile | 写 is_active 那套,base_version 是**该套的** | 写该套 |
| GET /versions | is_active 那套历史 | 该套历史 |
| GET /versions/{v} | is_active 那套某版 | 该套某版 |
| POST /rollback | 回退 is_active 那套 | 回退该套(不影响其他套) |
| GET/PUT /admin-default | 管理员默认的 is_active 那套 | 管理员默认的该套 |

- `set` 指向不存在的套 → 404(与「档案版本不存在」同族错误契约)。
- **响应体加 `set`/`kind` 字段**(告诉客户端读的是哪套);老客户端忽略多余字段,不破坏。
- `base_version`/409 语义完全不变(需求 §4.4)。

### 新增 4 端点:套管理
| 方法 | 路径 | body | 说明 |
|---|---|---|---|
| GET | /api/style-profile/sets | — | `{sets:[{name,kind,is_active,version,updated_at}]}` |
| POST | /api/style-profile/sets | `{name,kind,profile,from?}` | 新建;`from`=复制某套当前 profile;name 重复 409;首套自动 is_active |
| PATCH | /api/style-profile/sets/{name} | `{new_name?,is_active?}` | 改名(重名 409)/设默认(事务内把旧 active 置 False) |
| DELETE | /api/style-profile/sets/{name} | — | 删套;**剩一套拒绝(409)**;删的是 active → 事务内把另一套设 active |

## 4. 兼容性(铁律,需求标为最重要)

1. **不带 `set` → is_active 那套**。老客户端零感知,行为与今天逐字节一致。
2. 迁移后每运营恒有一个 is_active 套,所以「不带 set」永远有确定归属。
3. `base_version` 无条件下发 + 客户端原样透传,409 语义不变。
4. `profile` 仍**原样存取、不加 schema 校验**(需求 §6:多套容器顶层键是 schema/active/profiles,
   加校验会全线报错;而且 server 本就不该理解 profile 语义)。
5. `GET /admin-default` **保持一般用户可读**(不设 require_admin,0.12.0 起就是),客户端守卫依赖它。

## 5. 迁移(alembic 一个 revision,识别两种存量)

对每个 `style_profiles` 行:
- **老平铺**(无 `schema` 键或 `schema != profiles-v1`):`name="图文", kind="carousel", is_active=True`;
  该 operator 的 versions 行 `set_id` 指向此行。**版本号不变**(保留既有留痕引用)。
- **profiles-v1 容器**(`schema=="profiles-v1"`,现生产为 0,但预留):按 `profile.profiles` 拆成 N 套,
  `name`=容器里的键,`kind`=各套 `kind`,`active` 字段决定 is_active。
  **历史版本无法拆分**(历史里存整份容器)——把整份历史挂到 name="图文"(或 active 那套)下,
  标注 note 前缀「迁移前整份历史」;拆出的其余套各自 v1 无历史。需求 §4.3 已接受此有损处理。
- 管理员默认(operator_id NULL)同构迁移。

迁移**幂等 + 防御式**:再跑不重复建套(检测 name 已存在则跳过);profile_json 解析失败的行按老平铺兜底
(name=图文),不中断迁移。

## 6. dropped_keys 语义修正(需求 §3.3)

现在算「整份覆盖比上一版少的顶层+二级键」。多套拆开后,PUT 的 profile 就是**单套内容**(不再是容器),
所以 dropped_keys 天然回到「套内部字段」的正确语义——`visual` 整段弄没 = `dropped_keys:["visual"]`。
**这条不需要额外代码,是数据模型对齐后的自然结果**(需求 §5.2 客户端要拆的守卫也随之没必要)。

## 7. 测试要点(每条都要有)

- **兼容性(红线)**:不带 set 的全部 6 端点,行为与迁移前逐字段一致(读/写/回退 active 套)。
- **每套独立版本链**:建两套 → 改 A 套 → B 套 version/内容不变;回退 A 套不动 B 套(需求 §3.1 核心)。
- **并发不假冲突**:两套各自 PUT,base_version 互不干扰,都成功(需求 §3.2)。
- **is_active 唯一**:建套/设默认/删 active 套后,恒有且仅一个 is_active。
- **删到剩一套拒绝** 409。
- **set 管理**:新建(重名 409/from 复制/首套自动 active)、改名(重名 409)、设默认。
- **迁移**:老平铺→图文套(版本号不变、历史挂上);profiles-v1 容器→N 套(active 决定 is_active、
  历史挂图文套);解析失败兜底;幂等。
- **dropped_keys**:套内部 `visual` 丢失能算出。
- **admin-default 多套**:管理员默认可多套,一般用户可读。

## 8. 关键决策与 trade-off(供评审挑)

1. **改造现有表 vs 新建 profile_sets 表**:选改造(迁移平滑);代价是 style_profiles 表名语义微调。
2. **is_active 唯一靠应用层 vs DB 部分索引**:选应用层(SQLite 部分唯一索引对 NULL operator 易踩坑,
   且与「最后一个管理员」同款应用层不变量口径一致)。风险:并发设默认可能短暂两个 active——
   用事务内「先全置 False 再置目标 True」+ SQLite 写串行兜住(与最后管理员保护同机制)。
3. **profiles-v1 历史无法拆分**:需求已接受,整份历史挂图文套 + note 标注。
4. ~~管理员默认 (NULL, name) 靠 DB 约束唯一~~ **已被 §9 B1 证伪推翻**——见下。

---

## 9. 评审修订(fable 对抗评审,5 blocker 全补;骨架不动)

### B1 · name 唯一性:实 operator 靠联合约束,NULL 靠部分索引
- **实测证实**:SQLite `UNIQUE(operator_id, name)` 对 NULL operator **不生效**(NULL≠NULL,两行 (NULL,'图文')
  都能插);仓库 style_profile.py:41-43 早就记着「只有一行 NULL 靠应用层无写 NULL 路径保证」,而本设计恰恰新增
  管理员默认多套=写多 NULL 行,拆了旧不变量根基。
- **修复**:实 operator 用 `UNIQUE(operator_id, name)`;管理员默认(NULL)另加**部分唯一索引**
  `CREATE UNIQUE INDEX uq_admin_set_name ON style_profiles(name) WHERE operator_id IS NULL`
  (SQLite 3.8+/PG 原生;§8.2 拒绝部分索引仅针对 is_active,不适用 name)。
- **铁律**:所有 NULL 行的 UPDATE/SELECT/DELETE 必须写 `operator_id IS NULL`,**禁参数化 `= None`**
  (生成 `= NULL` 匹配零行的静默 bug,是这类代码最经典的坑)。

### B2 · 管理员默认「多套」+ 新运营继承(需求核心能力,原设计端点面不可达)
- **问题**:原 4 个 /sets 端点全走 current_operator() 作用于调用者本人,没有任何端点能给 NULL 行建第二套/
  改名/删/设默认。老板要「默认含图文+文字版两套」建不出来。
- **修复(套管理端点覆盖管理员默认)**:/sets 四端点 + /admin-default 读写,当 role=admin 且带
  `?scope=admin-default` 时作用于 NULL 行(写侧 require_admin;读侧 GET /sets、GET ?set= 不设门,客户端守卫依赖)。
- **继承**:server **不自动预建**运营档案。exists:false 时 GET 回落读管理员默认 is_active 套;运营要多套由
  **客户端** onboarding 时按默认套数 `POST /sets`(from 复制默认各套)。理由:server 不知运营「建档」时机,
  客户端建更可控。写进 manifest 向 skill 交代。

### B3 · 0 套运营首次 PUT(不带 set,新运营 onboarding 必经)
- 不带 set 的 PUT 且运营 0 套 → **自动建套** `name=图文/kind=carousel/is_active=True`(与迁移默认一致),version 1。
- exists:false 的 GET → 返回管理员默认 **is_active 那套**的 profile,`admin_default_version` 指该套版本。
- 进兼容性测试清单。

### B4 · DELETE 套必须同事务删版本链(数据损坏级,已实测复现)
- **实测链条**:删套不清 versions(本仓 PRAGMA foreign_keys 未开,CASCADE 只是声明)→ 孤儿版本留存 →
  SQLite **rowid 复用**让新套拿到同 id → 新套写 (set_id=旧id, v1) 撞死套孤儿历史的 `UNIQUE(set_id,version)`
  → IntegrityError 冒 500,且 GET /versions 列出别套尸体历史。
- **修复**:DELETE 在**同一事务内先删该套全部 style_profile_versions 再删 style_profiles 行**
  (应用层级联,同 operator_service.delete_operator)。设计话术「删套=历史随套删」。
- 回归测试:删套→重建同名/异名套→写 v1 成功且历史为空。

### B5 · 迁移后滞后客户端写容器的防御(上线窗口确定性事故)
- **问题**:v1.48.0 容器方案此刻在全线运营机器上跑。server 上线到全部换新版之间,旧客户端 --new-profile
  会 GET 整份 → 包成 profiles-v1 容器 → PUT 回。迁移后这个 PUT(不带 set,按铁律照单全收)会把整个容器塞进
  active 套的 profile_json = 套里套,dropped_keys/版本链/读取全错乱。一次性 alembic 不会再拆它。
- **修复**:PUT(带/不带 set)对 profile 顶层做**极窄哨兵检测**:同时含 `schema=="profiles-v1"` 且有
  `profiles` 键 → **400**,报错「工具包版本过旧,请升级后重试」。
- **这不违「不校验语义」**:需求 §6 红线前提是 server 不做多套;一旦 server 原生多套,容器进套=确定性数据
  损坏,拦它恰恰是保护铁律。此决策已拍板。

### concerns 处理(评审列的次级问题,一并定死)
- **兼容性措辞**:铁律改为「**字段只增不减、既有字段语义逐项不变**」(响应加 set/kind 是增字段,老客户端
  JSON 取键不受影响);联调**实测**老客户端三种信封剥壳对多余键的容忍度,不靠想当然。
- **容器历史挂靠**:明确挂 **active 那套**(非「图文 或 active」二选一);承接套 current version = max(history)+1,
  落一条**拆分后单套快照**,使当前态与版本号衔接(避免 GET /versions/{v} 取到整份容器与当前单套对不上)。
- **PATCH/POST 边界**:`is_active=false` 单发拒绝(否则 0 active);`name` 规则(非空、trim 首尾空白、禁 `/`
  `?` 等 URL 保留字符、≤20 显示宽度);`from` 指向不存在的套 → 404;**重名/首套并发 IntegrityError → 409 非 500**
  (复用 _write_new_version 的 TOCTOU 兜底同款);`new_name`+`is_active` 同时给要原子。
- **首套自动 active 并发**:建套在事务内 flush 后**复核 active 计数**(先改后数,同 _ensure_admin_remains),
  为 0 则置 active——覆盖「两个并发 POST 都读到 0 套双双自封」。
- **POST /sets 落 v1 快照**,source="manual";kind 建套后**不可改**(PATCH 仅 new_name/is_active,有意为之)。
- **GET ?set=X 语义**:运营无档案 → exists:false 回落读默认;有档案但无此套 → 404。rename 使旧名 404 可接受,
  manifest notes 向 skill 交代。
