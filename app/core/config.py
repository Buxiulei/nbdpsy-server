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

    # ── 选择器自愈(SelfHealLocator)。默认关闭,配 LLM_API_KEY 且开 ENABLED 才生效。 ──
    SELFHEAL_ENABLED: bool = False
    LLM_API_KEY: str = ""
    LLM_BASE_URL: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    LLM_MODEL: str = "qwen-flash"
    LLM_TIMEOUT: int = 15

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
