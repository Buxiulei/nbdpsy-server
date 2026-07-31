"""风控事件落库:把一次撞验证墙写进 ``risk_events`` 台账。

表的设计理由见 ``app/models/risk_event.py`` 的模块 docstring。这里只有写入一件事。

纪律照抄 ``note_export`` / ``archive_published_job``:**绝不上抛**。留痕失败最多丢一条
证据(还有日志兜底),不能反过来把 cookie 检测主流程搞崩 —— 检测结论比证据更要紧。
"""

from loguru import logger

from app.browser.login_detector import WALL_UNKNOWN
from app.models.risk_event import RiskEvent


async def record_wall(
    session_factory, account_id: int, wall: dict | None, source: str
) -> bool:
    """把一次撞墙落成 ``risk_events`` 一行;返回是否真的写入。

    ``session_factory`` 既可传 ``get_session``(请求/任务上下文)也可传后台巡检自己的
    ``async_sessionmaker`` —— 两者都是 ``async with X() as session`` 的用法。
    ``wall`` 为空(没撞墙)直接跳过;写库异常只告警,不抛。
    """
    if not wall:
        return False
    try:
        async with session_factory() as session:
            session.add(
                RiskEvent(
                    account_id=account_id,
                    wall_type=wall.get("wall_type") or WALL_UNKNOWN,
                    source=source,
                    target_url=wall.get("target_url"),
                    landed_url=wall.get("landed_url"),
                    page_text=wall.get("page_text"),
                )
            )
            await session.commit()
        logger.warning(
            f"[risk_events] 账号 {account_id} 撞风控墙已留痕 "
            f"type={wall.get('wall_type')} source={source} landed={wall.get('landed_url')}"
        )
        return True
    except Exception:
        logger.exception(f"[risk_events] 风控事件落库失败(忽略)account_id={account_id}")
        return False
