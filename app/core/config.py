from pydantic_settings import BaseSettings, SettingsConfigDict

# SECRET_KEY 出厂占位值:生产(DEBUG=False)必须改成强随机值,否则 create_app 启动 fail-fast。
# 单一来源:既作 Settings.SECRET_KEY 默认,也作启动闸的比对基准,防两处漂移。
DEFAULT_SECRET_KEY = "change-me-32bytes-minimum-secret-key"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # 应用元信息
    APP_NAME: str = "nbdpsy-mcp"
    DEBUG: bool = False
    LOG_LEVEL: str = "INFO"
    LOG_FILE: str = "logs/app.log"

    # API 服务监听
    API_HOST: str = "0.0.0.0"
    API_PORT: int = 8848
    PUBLIC_BASE_URL: str = "http://127.0.0.1:8848"

    # 数据库
    DATABASE_URL: str = "sqlite+aiosqlite:///./data/nbdpsy.db"
    # SQLite 忙等超时(秒):并发写锁竞争时最多等待此秒数而非立即报 database is locked。
    # 同时作为 aiosqlite connect timeout 与 PRAGMA busy_timeout(*1000 毫秒)。仅 sqlite 生效。
    SQLITE_BUSY_TIMEOUT: int = 30

    # 安全
    SECRET_KEY: str = DEFAULT_SECRET_KEY
    ROOT_ADMIN_APIKEY: str = ""

    # 数据/上传目录
    DATA_DIR: str = "./data"
    UPLOAD_DIR: str = "./data/uploads"
    # 图片上传:单张大小上限(MB)与批次保留天数(懒清理据此判过期)
    UPLOAD_MAX_MB: int = 10
    UPLOAD_TTL_DAYS: int = 7

    # 浏览器自动化
    XVFB_DISPLAY: str = ":99"
    # 全局浏览器并发闸:同时运行的 camoufox 数上限(publish/cookie-check/note-export 统一套闸)
    BROWSER_CONCURRENCY: int = 6

    # 发布队列
    PUBLISH_CONCURRENCY: int = 2
    PUBLISH_RETRY_SCHEDULE: str = "120,600,1800"
    PUBLISH_JOB_TIMEOUT: int = 600
    # 账号级发布冷却(秒):同一账号两次发布的最小间隔,每次占用前用
    # random.uniform(MIN, MAX) 现抽,抖动化避免固定节律被指纹化(高频发布是封号信号)。
    # 冷却未到不丢 job,顺延其 next_retry_at 保持 pending,下轮 scan 再捞。
    PUBLISH_MIN_INTERVAL_MIN: int = 1200
    PUBLISH_MIN_INTERVAL_MAX: int = 3600
    # 每账号每自然日发布上限:建 job 入口达到即顺延到次日活跃窗口起点(带抖动),仍落库 pending。
    PUBLISH_DAILY_CAP: int = 8
    # 次日活跃窗口起点(UTC 小时,默认 1 = 北京时间 09:00)与其抖动跨度(秒),
    # 顺延时间在窗口起点 + random.uniform(0, JITTER) 内落点,避免整点节律。
    PUBLISH_ACTIVE_WINDOW_START_UTC_HOUR: int = 1
    PUBLISH_ACTIVE_WINDOW_JITTER_SEC: int = 7200
    # 视频/音频:上传 + 平台转码的等待上限,**按文件体积伸缩**(公式见
    # app/publish/policy.py::media_timeout_s,step3v 与账号子进程硬超时共用一套)。
    # 用户会传 15-30 分钟的 GB 级视频:固定超时在这个量级上必错 —— 给小了大文件永远
    # 发不出去,给大了一条坏视频占死进程几小时。基数 + 每 100MB 加时,封顶兜底。
    VIDEO_UPLOAD_TIMEOUT_BASE_S: int = 300
    VIDEO_UPLOAD_TIMEOUT_PER_100MB_S: int = 120
    VIDEO_UPLOAD_TIMEOUT_CAP_S: int = 3600
    # 大媒体分片上传(视频/音频通用)。分片是硬需求不是优化:mcp 反代走 Cloudflare
    # Tunnel,单请求体上限 100MB,GB 级文件单发必死在隧道层。UPLOAD_CHUNK_MB 是服务端
    # 下发给客户端的分片大小(会被 media_upload.MAX_CHUNK_BYTES=90MB 再压一道);
    # 两个体积上限按 kind 分开(平台侧:视频 20GB 但我们保守封 4GB,音频 1GB),0=不限。
    UPLOAD_CHUNK_MB: int = 50
    UPLOAD_MEDIA_MAX_MB: int = 4096
    UPLOAD_AUDIO_MAX_MB: int = 1024
    # 未完成的分片会话保留多久(小时);超期目录连同碎片一起清掉,0=不清理
    UPLOAD_SESSION_TTL_HOURS: int = 24
    # Cookie 巡检间隔（秒，0 表示关闭）
    COOKIE_CHECK_INTERVAL: int = 0

    # ── 新号接入调度(onboarding_scheduler)──
    # 扫描间隔(秒,0=关闭):有 cookie 却还卡在 cookie_status='unknown' 的号,每轮给他
    # 登记一次 cookie_check,转 valid 后引导链自动接管。**它不是周期巡检的替身**:
    # 只覆盖未转正的号,转正即出局,故不与同号会话总闸(系统 4 次/时)正面抢额度;
    # 打开全矩阵巡检才会——那是每号每轮各烧一次会话。本身只跑两条 SELECT,300s 的
    # 代价约等于零,决定新号"灌完 cookie 到自动转正"的等待上限。
    ONBOARDING_CHECK_INTERVAL: int = 300
    # 同号两次自动检测之间的最短间隔(小时):没有它,一个检测不过去的号会以上面那个
    # 间隔无限重试,独自打满自己的会话额度。取 1 小时的依据:留在 unknown 里反复入选
    # 的**只有 error(基础设施失败)**——invalid/captcha/restricted 都会写回账号状态、
    # 自己从候选里掉出去。所以这个值调的是"基础设施抖动多久重试一次":1 小时既够
    # camoufox/显示层那类抖动自愈,又把最坏情况从 12 次/时(必打满 4 次/时的总闸)
    # 压到 1 次/时(总闸的四分之一,且新号此时没有别的任务与它争)。
    ONBOARDING_CHECK_RETRY_HOURS: int = 1

    # 孤儿 camoufox 回收:巡检间隔(秒，0 表示关闭)与判定超龄阈值(秒)。
    # 无主(账号锁未持有)且存活超 REAP_AGE 的 camoufox 视作崩溃残留,SIGKILL 回收防内存泄露。
    BROWSER_REAP_INTERVAL: int = 300
    BROWSER_REAP_AGE: int = 900

    # 占位废账号(登录闭环 userInfo 采集失败留下的 xhs_account_<时间戳> 空号)根治:
    # A 服务端自愈——真登录成功时清同 operator 近窗内新建的占位行,窗口时长(分钟)。
    PLACEHOLDER_CLEAN_WINDOW_MINUTES: int = 30
    # B TTL 兜底 reaper——巡检间隔(秒,0=关闭)与占位行存活上限(小时,超过即回收)。
    PLACEHOLDER_REAP_INTERVAL: int = 3600
    PLACEHOLDER_TTL_HOURS: int = 24

    # 调试截图开关
    DEBUG_SCREENSHOTS_ENABLED: bool = False
    # 调试截图保留天数(0=不清理)。截图目录只增不减,2026-08-02 实测已 1633 个 / 469MB;
    # 磁盘满了会把发布、补量、同步一起拖垮。清理在每次发布前顺手做,不另起调度器。
    DEBUG_SCREENSHOT_RETENTION_DAYS: int = 14

    # ── 选择器自愈(SelfHealLocator)。默认关闭,配 LLM_API_KEY 且开 ENABLED 才生效。 ──
    SELFHEAL_ENABLED: bool = False
    LLM_API_KEY: str = ""
    LLM_BASE_URL: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    LLM_MODEL: str = "qwen-flash"
    LLM_TIMEOUT: int = 15

    # ── 视频管线(transport/remake/revise)──────────────────────────────────
    # DashScope 单一 apikey 打通 ASR(录音文件识别) + 翻译(qwen-mt) + LLM + VL 四种能力;
    # 四者共用 compatible-mode(openai 兼容)base_url,ASR 走 dashscope SDK 另说(SDK 自带端点)。
    DASHSCOPE_API_KEY: str = ""
    DASHSCOPE_BASE_URL: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    # ASR 录音文件识别模型(paraformer 系列,收公网 URL,任务轮询取转写正文)
    VIDEO_ASR_MODEL: str = "paraformer-v2"
    # transport 下载阶段的时长闸门(秒):超此时长直接拒收,防超长视频拖垮全链路(源默认 2h)
    VIDEO_TRANSPORT_MAX_DURATION_SECONDS: int = 7200
    # 逐句翻译走 qwen-mt 专用档:terms/domains/tm_list 三件套经 extra_body.translation_options 直传
    VIDEO_MT_MODEL: str = "qwen-mt-plus"
    # 重写/解析/本地化等通用 LLM 档(openai 兼容 chat)
    VIDEO_LLM_MODEL: str = "qwen3.7-plus"
    # 关键帧视觉理解(openai 兼容 multimodal,本地图转 base64 data URL 内联)
    # 2026-07-30 qwen-vl-max→qwen3-vl-plus：旧模型 2026-10-10 下线；新模型不带
    # enable_thinking 参数时默认非思考,与本调用形态兼容(已实测)
    VIDEO_VL_MODEL: str = "qwen3-vl-plus"

    # ── 豆包语音 TTS(声音复刻 v3 / seed-icl-2.0),视频配音默认走此 provider ──
    # v3 HTTP chunked 流式:多行 JSON event,音频在各行 data(base64 mp3 分片),须按行拼接
    DOUBAO_TTS_APPID: str = ""
    DOUBAO_TTS_TOKEN: str = ""
    # 默认复刻音色(牧羊,用户实测确认自然度优于 cosyvoice)
    DOUBAO_TTS_VOICE: str = "S_hoiqVFN72"
    DOUBAO_TTS_RESOURCE_ID: str = "seed-icl-2.0"
    # transport dub 阶段全片统一语速上限(倍率):二分统一语速塞不下总时长时的语速天花板,
    # 到顶仍溢出则取此值+告警(语速绝不再动,接受残余溢出/漂移)。源默认 1.2。
    TTS_MAX_RATE: float = 1.2

    # ── 一致性生图(gpt-image-2 锚点法,自薯营家 2026-07-23 停机迁移)──
    # OpenAI Images API:自定义 base_url 走国内中转;PROXY 非空时再叠 HTTP 代理。
    OPENAI_IMAGE_API_KEY: str = ""
    OPENAI_IMAGE_BASE_URL: str = "https://api.openai.com/v1"
    OPENAI_IMAGE_MODEL: str = "gpt-image-2"
    # 质量档:medium 为质量/成本平衡点(8 页约 $0.68);low 便宜但文字易糊
    OPENAI_IMAGE_QUALITY: str = "medium"
    # 单张生图调用超时(秒):gpt-image 单张常 30-120s,留足余量
    OPENAI_IMAGE_TIMEOUT: int = 300
    # 建连超时(秒):openai SDK 默认 connect 仅 5s,网络瞬时抖动时毫无缓冲就抛
    # APITimeoutError("Request timed out.")。2026-08-05 实测四次失败耗时 18/20.7/33s,
    # **短于**成功耗时 43-76s ——挂的是抖动窗口不是"生成太慢",故要调的是建连而非整体。
    OPENAI_IMAGE_CONNECT_TIMEOUT: int = 30
    # 超时重试次数(本模块层,与 429 预算分开计):跨过抖动窗口即可救回该页。
    # SDK 自身 max_retries 已显式压到 1,总尝试数才可控可解释。
    OPENAI_IMAGE_TIMEOUT_RETRIES: int = 2
    OPENAI_IMAGE_PROXY: str = ""
    # 单批次内并发路数(有界):锚点法各页互不依赖。此值决定**一篇要跑几波**——
    # 9 页 5 路要 2 波(约 100s),10 路 1 波(约 50s),故取 10 覆盖常见 6-9 页一波出完。
    # 与调用方的"篇级并行"相乘才是打到上游的总在飞:10 篇 × 10 路 = 100 并发
    # ≈ 120 张/分,占 gpt-image-2 Tier 5 上限(250 IPM)的 48%,留一半余量。
    # 换低 tier 或调用方提高篇级并行时必须同步下调此值(Tier 3 仅 50 IPM)。
    OPENAI_IMAGE_CONCURRENCY: int = 10
    # 进程级生图并发闸(image_gate):封顶**全进程**同时在飞的上游图像请求,不依赖调用方守约。
    # 页级 × 篇级是相乘的(10 路 × 10 篇 = 100),一旦有人一次提交几十篇就会冲破 tier 限额;
    # 本闸是兜底,超出的排队而非拒绝。100 = 约定稳态(≈120 张/分,占 Tier 5 250 IPM 的 48%)。
    OPENAI_IMAGE_GLOBAL_CONCURRENCY: int = 100

    # ── 视频 worker 调度(方案 C 独立 asyncio worker,scheduler.py 消费)──
    # 单机 CPU 编码,并发 1 足够(排队语义与源一致);阶段内 300s 周期 touch heartbeat_at;
    # 恢复扫描判僵死阈值 900s(15min),超阈从 first_incomplete_stage 续跑。
    VIDEO_WORKER_CONCURRENCY: int = 1
    VIDEO_HEARTBEAT_INTERVAL: int = 300
    VIDEO_STALE_TIMEOUT: int = 900

    # ── 即梦视频生成(dreamina clips,设计 2026-08-05-dreamina-clips-design.md)──
    # dreamina CLI 绝对路径:systemd 进程 PATH **不含** ~/.local/bin,必须写全路径,
    # 否则 worker 里一律 FileNotFoundError(本机交互 shell 能跑不代表服务能跑)。
    # 登录态在 CLI 自己的 ~/.dreamina_cli/(公司号一份,运营侧零登录),不进本配置。
    DREAMINA_BIN: str = "/home/roots/.local/bin/dreamina"
    # 调度主循环周期(秒):每轮先 submit 全部 queued,再 poll 到期的在飞任务。
    CLIP_POLL_SECONDS: int = 20
    # 单条在飞任务两次 query_result 的最小间隔(秒)。即梦排队常达数小时,查密了纯属
    # 给单账号 CLI 加锁竞争,60s 足够。
    CLIP_QUERY_INTERVAL: int = 60
    # CLI 子命令超时(秒):submit 只提交不等待故 120s 足够;query_result 成功时要下载
    # MP4,给 300s。**超时一律不重提**(见 services/dreamina 的歧义结局处置)。
    CLIP_SUBMIT_TIMEOUT: int = 120
    CLIP_QUERY_TIMEOUT: int = 300
    # 产物 MP4 保留天数(需求要求 ≥7 天:一条片从生成到过审可能跨几天)
    CLIP_TTL_DAYS: int = 14
    # 产物 TTL 巡检间隔(秒,0=关闭);默认 6h,与内容资产库同量级。
    CLIP_REAP_INTERVAL: int = 21600
    # 积分低水位:GET /api/video-credits 的 low_threshold_hit 判据(仅提示不拦截)
    CLIP_CREDIT_LOW_WATERMARK: int = 200
    # 单次批量提交的镜数上限(超出 422)
    CLIP_MAX_BATCH: int = 50
    # 参考图物化的单张大小上限(MB)
    CLIP_IMAGE_MAX_MB: int = 15
    # 参考视频物化的单条大小上限(MB)。视频比图片大一到两个量级(2-30s 的 720p 片常在
    # 10-60MB),共用图片那个 15MB 的闸会把正常参考视频全拒掉,故单列一档。
    CLIP_VIDEO_MAX_MB: int = 200
    # 参考音频物化的单条大小上限(MB)。2-30s 的无损音频撑死几十 MB,比视频窄得多,
    # 但仍远超图片那档,故也单列一档(三类素材三个闸,各按各的量级)。
    CLIP_AUDIO_MAX_MB: int = 50

    # ── 内容资产库(content_archive,设计 2026-07-25-content-archive-design.md)──
    # 发布成功自动归档;取详情即刷新 last_used_at;距最后使用超 TTL 天由 ArchiveReaper 删除。
    ARCHIVE_TTL_DAYS: int = 90
    # 归档 TTL 巡检间隔(秒,0=关闭);默认 6h,归档量低无需高频。
    ARCHIVE_REAP_INTERVAL: int = 21600

    # ── 咨询师推介引用推导(counselor_quote)──
    # 「接待员联系方式」笔记的平台 note_id:一篇笔记**本身就是咨询师推介笔记**时引用它。
    # 这篇笔记**含二维码、有违规风险**,正因如此才集中在 NBDpsy 主号一个账号上统一管理
    # ——它是全系统唯一允许跨账号引用的一篇(别的都只引本账号自己的,见 counselor_quote)。
    # **出厂留空**:真实 id 待运营指认,需配置后该规则才生效;为空时推介笔记一律不引用,
    # **绝不 fallback 到任何其它笔记**(猜一篇挂上去比不引用糟得多)。动它要谨慎。
    RECEPTIONIST_CONTACT_NOTE_ID: str = ""

    # ── 笔记数据定时采集(note_metrics_scheduler)──
    # 扫描间隔(秒,0=关闭)。语义:每号每天最多自动采 3 次,失败等比退避(1h→2h);
    # 间隔只决定断档后多快补上,默认 1h。
    NOTE_METRICS_INTERVAL: int = 3600

    # ── 代管账号的笔记数量上限淘汰(retention_scheduler)──
    # 设计:docs/design/2026-08-10-managed-accounts-design.md 第五节。
    # **这四个值里有三个是安全轨,不是调优旋钮** —— 淘汰是不可逆删除,调松任何一个都在
    # 加快"误删自己内容"的速度。
    # 总开关:0 = **只算不删**(照常打分、照常落 retention_runs 审计,就是不建删除 job)。
    # 这是 kill switch,也是观察窗:想知道这套评分会选谁,就关着它跑几天看审计行。
    RETENTION_ENABLED: bool = True
    # 安全轨①宽限期:发布不足这么多天的笔记不参与淘汰。新笔记指标还没成型(曝光要几天
    # 才铺开),不设宽限期等于每天把刚发的内容当"表现最差"删掉。
    RETENTION_GRACE_DAYS: int = 7
    # 安全轨③单日单号删除封顶:首次启用时库存可能远超上限(比如 140 篇对 100 的帽),
    # 不封顶就是第一天一口气删 40 篇。封顶后多花几天收敛,换掉"大屠杀"的可能性。
    RETENTION_DAILY_DELETE_MAX: int = 5
    # 五指标权重(JSON 串)。加权平均前每个指标先在**候选集内**做 min-max 归一化,
    # 故权重之比就是重要性之比,与指标量纲无关(浏览量上万、增粉个位数也能同台比)。
    # 解析失败/取值非法一律回退这里的默认值并告警,绝不用一份坏权重去删笔记。
    RETENTION_WEIGHTS: str = (
        '{"views":1,"likes":2,"collects":3,"comments":3,"follows":5}'
    )
    # 扫描间隔(秒,0=关闭,与其余调度器同门)。语义不是"每小时删一次":每 UTC 日
    # 每号至多跑一轮,且必须等当日数据快照到位才跑,间隔只决定"快照到位后多久接上"。
    RETENTION_CHECK_INTERVAL: int = 3600

    # ── 出口链路自检(egress_guard)──
    # 防"代理重装/更新覆盖掉 sing-box 里 camoufox 直连规则"静默复发:规则一丢,camoufox
    # 出国 → 小红书风控 401 踢登录,症状与 ark-401 一模一样极易误判。两级自检(读配置 +
    # 起一次性空 cookie camoufox 实测出口地区),间隔秒(0=关闭),默认 6h。
    EGRESS_CHECK_INTERVAL: int = 21600
    SINGBOX_CONFIG_PATH: str = "/opt/hysteria-client/singbox-tun.json"

    # ── 笔记核心目的回填(note_purpose)──
    # **每轮回填最多开几次编辑页**。手工发布的存量笔记有几十上百篇,一次性全抓必被风控:
    # 实测同一账号一小时内起 5 次会话,就会从"扫码验证"被打成"请求太频繁",两个账号因此
    # 被弹墙(其中一个靠人工扫码才解开)。调大这个值等于直接加快踩墙速度,谨慎。
    NOTE_PURPOSE_BACKFILL_LIMIT: int = 3

    # ── 历史笔记互动补量(interaction_backfill)──
    # **两道篇数闸,谁都不能省**。补量是"对老笔记的集中互动",是平台眼里最典型的补量特征,
    # 风险高于"新笔记发布后互动";而全矩阵补一遍是 139 篇 × 6 个号 ≈ 834 次互动。
    # 日上限:每个互动方账号每天最多互动几篇(全量补完 ≈ 6 天,这是**刻意的**);
    # 单轮上限:一次任务最多做几篇,超出的留给下一轮。
    # 调大任何一个都等于加快踩风控墙的速度,**风险由业务侧承担**。
    NOTE_INTERACTION_DAILY_LIMIT: int = 20
    NOTE_INTERACTION_ROUND_LIMIT: int = 5
    # ── 两个断路器(2026-08-13 事故驱动)──
    # 上面两道闸管的是"补得多快",管不了"白开多少次注定失败的会话"。实测:号12 自 08-08 起
    # 96 连败全是同一个 profile_not_loaded(登录态在、但打开任何发布者主页都渲染不出笔记卡),
    # 风控台账**零记录**——它没撞验证墙,"撞墙即停"那套压根不触发,error 冷却一过就再试一次,
    # 96 次白烧的都是真实的风控暴露。同期三篇 views=0 的笔记被全部 9 个 actor 报"主页找不到"
    # (大概率被平台屏蔽),各积了十几个 error 还在被反复重试。
    # actor 熔断:最近 N 条台账行(不分笔记,按 done_at 倒序)全是 error 就本轮跳过这个号。
    # 一篇失败落两行(赞 + 藏),故 6 ≈ 连续三篇整篇失败——偶发抖动到不了,半死号一天到得了。
    INTERACTION_ACTOR_BREAKER_N: int = 6
    # actor 熔断的半开探测间隔(小时):最新那条 error 距今超过它就放行一轮,那一轮**只做一篇**
    # (按整轮上限 5 篇放的话,每个冷却周期照旧白开 5 次页,断路器就只剩一半意义)。
    # **没有半开就是死锁**——熔断的号永不被选中,也就永远产不出成功记录来复位。
    # 成功即自然复位(最近 N 条不再全 error);再失败则从新的那条 error 重新计时。
    INTERACTION_ACTOR_BREAKER_COOLDOWN_H: int = 12
    # 笔记熔断:一篇被 ≥K 个**有资格**的不同 actor 在发布者主页里翻不到,就永久移出候选。
    # 资格 = cookie_status=valid 且未被 actor 熔断 —— 半死 actor 对**每篇**都报找不到,
    # 它的票恒为真、不承载信息,裸数票会让 K 变相打折(多一个半死号就少一个真信号)。
    # ⚠️ 翻不到有两种成因、台账区分不了(平台屏蔽 / 它在主页里排太靠后超出滚动预算),
    # 但两种情况下继续重试都同样徒劳,所以一律停调度、把篇目列进 suppressed_notes 交给人判。
    # **不自动恢复**:两种成因都不会自己好。人工核实后手工清那篇的 error 行即可重新入池。
    # 调 K 的方向:调大 = 更保守(少误伤主页翻不到的正常篇,代价是屏蔽篇多空转几轮)。
    INTERACTION_NOTE_BREAKER_ACTORS: int = 3

    # ── 发布后矩阵互动(matrix_interact)的单轮上限 ──
    # 一条任务 = 一次浏览器会话,一次会话里最多互动几篇。取 5 的依据有两条:
    # 1. 单轮预算 1200s 装得下 —— 5 篇 × (互动 ~40s + 篇间间隔最长 240s) ≈ 19 分钟,
    #    仍在账号子进程硬超时(ACCOUNT_PROC_TIMEOUT=1800s)之内;
    # 2. 覆盖得住真实发布量 —— 2026-08-07 实测峰值一小时发 4 篇,5 篇/轮意味着一个号
    #    一小时的发布扇出**一次会话就做完**(改前是 26 次会话打满全矩阵额度)。
    # 超出的不丢,排进下一轮。调大 = 单次会话开更久 + 更可能被硬超时强杀。
    MATRIX_INTERACT_ROUND_LIMIT: int = 5
    # 自动续跑扫描间隔(秒,0=关闭):没有它,存量补量只有靠人每天手动触发才跑得完。
    # **它不放宽任何闸**——每轮只是问一次 plan_round"还有得补吗",日上限/单轮上限/
    # 冷却/在途去重全在原处判。调小只会更频繁地问,答案照样受上面两道闸约束;
    # 真正决定"多久补完"的是日上限,不是这个值。
    INTERACTION_BACKFILL_INTERVAL: int = 1800

    # ── 受众行为库(audience,设计 docs/design/2026-08-12-audience-behavior-library-design.md)──
    # 采集调度总开关(kill switch)。关掉只停**采集**,已入库的数据与 5 个分析端点照常。
    # 出问题(平台改版 / 撞墙频繁)时先关它止血,不必回滚代码。
    AUDIENCE_SYNC_ENABLED: bool = True
    # 采集扫描间隔(秒)。它同时是**每号的到期门槛** —— 一个号距上次采集超过这么久才轮到它。
    # 一小时一次的依据:增量轮只滚到已知区(实测号1 全量 47 页 40 轮,增量封顶 5 轮),
    # 而通知流不会在一小时里堆出翻不完的量。调小 = 每号更频繁地开真号会话,
    # 直接顶到 ACCOUNT_HOURLY_SESSION_CAP=4 那条风控红线上,**风险由业务侧承担**。
    AUDIENCE_SYNC_INTERVAL: int = 3600
    # 潜客打分五维权重(JSON 串)。各维度先在候选人群内 min-max 归一化再加权,
    # 故权重之比就是重要性之比,与量纲无关。
    # ⚠️ **这是 v1 启发式,不是科学模型**:转化回流数据(谁最终进了私域)当前不存在,
    # 权重是运营直觉的初版。真实转化数据到位后必须回来重标定 —— 这个配置项就是为那天留的。
    # 解析失败/取值非法一律回退代码里的默认值并告警(见 audience_analytics.parse_weights)。
    AUDIENCE_SCORE_WEIGHTS: str = (
        '{"frequency":0.30,"cross_account":0.20,"recency":0.20,'
        '"depth":0.15,"relation":0.15}'
    )
    # 分析端点是否默认剔除自家矩阵号(user_id 从 xhs_accounts 现查,不硬编码)。
    # 出厂 true:不剔的话"最活跃的受众"永远是自家号互刷出来的量,整个库读起来就是废话。
    # 单次调用可用 ?exclude_self=false 覆盖(排查自家互刷量时要看得见它们)。
    AUDIENCE_SELF_EXCLUDE: bool = True

    # ── 合集批量清理(note_collection_batch,2026-08-07 运营移出需求 P1/P2)──
    # 两个上限差一个数量级,因为**两条路的代价差一个数量级**:
    # - 移出(dry_run=false)每篇是一次真「更新」提交(全量覆盖语义),比点赞收藏重得多,
    #   所以取比互动补量还保守的 5 篇/轮 —— 存量 ~100 篇摊几天清完完全可接受,
    #   **清理是一次性任务,慢比封号便宜**;
    # - 扫描(dry_run=true)只开页读一眼合集区,**零点击零提交**,风险约等于浏览笔记,
    #   所以放到 60 篇/轮(实际做几篇由单轮时间预算决定,剩下的下一轮接着来)。
    # 调大移出那个值等于加快踩风控墙的速度,风险由业务侧承担。
    NOTE_COLLECTION_REMOVE_ROUND_LIMIT: int = 5
    NOTE_COLLECTION_SCAN_ROUND_LIMIT: int = 60

    # ── 草稿箱周清理(draft_clean_scheduler)──
    # 扫描间隔(秒,0=关闭);语义=每号每 7 天清一次草稿箱(本系统不用草稿,全是垃圾)。
    DRAFT_CLEAN_INTERVAL: int = 86400

    # ── API/Worker 进程拆分(设计:docs/design/2026-07-24-api-worker-split-design.md)──
    # 进程角色:api=只挂 REST 路由;worker=只跑任务消费;all=兼跑(开发/测试/单进程小部署)。
    NBDPSY_ROLE: str = "all"
    # 每 operator 未完成任务配额:browser_jobs(queued/running)+ publish_jobs
    # (pending/publishing)合计达上限后再提交返 429;admin 不受限。
    OPERATOR_PENDING_QUOTA: int = 30
    # worker 调度中枢扫描 DB 队列周期(秒)
    WORKER_SCAN_INTERVAL: int = 5
    # 单轮派发中每账号子进程最多携带的任务数(账号间公平:先给各账号派一批再回头)
    WORKER_BATCH_PER_ACCOUNT: int = 3
    # 账号子进程硬超时(秒):超时视作僵死强杀,其任务由僵死恢复按 kind 语义处置
    ACCOUNT_PROC_TIMEOUT: int = 1800
    # 同号一小时浏览器会话总闸(0 = 关闸):supervisor 派发层按滚动小时窗数该号全部
    # 会话(browser_jobs + publish_jobs,不分触发方),达帽后**系统自发任务**延后派发。
    # 风控红线实测:同号一小时 5 次就把号弹上验证墙。
    ACCOUNT_HOURLY_SESSION_CAP: int = 4
    # 同号一小时**运营触发**会话帽(0 = 只关这层):运营任务放得比系统宽(人工意图优先),
    # 但不再无限直通——2026-08-07 实证 skill 用运营 apikey 批量回读组件,单号跑到
    # 51 次/时(红线 10 倍)。运营配额闸限的是并发未终态数,限不住速率,故加此帽。
    ACCOUNT_HOURLY_OPERATOR_SESSION_CAP: int = 12

    @property
    def retry_delays(self) -> list[int]:
        """把逗号分隔的重试计划字符串解析为秒数列表。"""
        return [int(x) for x in self.PUBLISH_RETRY_SCHEDULE.split(",") if x.strip()]


settings = Settings()


def assert_secret_key_configured() -> None:
    """N2 启动闸:生产(DEBUG=False)沿用默认 SECRET_KEY 直接 fail-fast。

    默认 key 是公开占位值,用它派生 Fernet 会让存量 cookie 加密形同虚设;上线前必须换成
    强随机值。DEBUG=True(开发/测试)放行,便于本地与单测跑默认值。放在 create_app 早期调用。
    """
    if not settings.DEBUG and settings.SECRET_KEY == DEFAULT_SECRET_KEY:
        raise RuntimeError(
            "生产环境必须设置 SECRET_KEY(不能沿用默认占位值);请在 .env 配置强随机 SECRET_KEY"
        )
