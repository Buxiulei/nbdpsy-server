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

    # Cookie 巡检间隔（秒，0 表示关闭）
    COOKIE_CHECK_INTERVAL: int = 0

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
