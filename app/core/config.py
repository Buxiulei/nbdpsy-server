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
    # Cookie 巡检间隔（秒，0 表示关闭）
    COOKIE_CHECK_INTERVAL: int = 0

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
    # 自动续跑扫描间隔(秒,0=关闭):没有它,存量补量只有靠人每天手动触发才跑得完。
    # **它不放宽任何闸**——每轮只是问一次 plan_round"还有得补吗",日上限/单轮上限/
    # 冷却/在途去重全在原处判。调小只会更频繁地问,答案照样受上面两道闸约束;
    # 真正决定"多久补完"的是日上限,不是这个值。
    INTERACTION_BACKFILL_INTERVAL: int = 1800

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
