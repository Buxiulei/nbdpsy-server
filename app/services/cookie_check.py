"""cookie 活性检测服务:browser_jobs 台账落库 + 契约 execute()(2026-07-24 架构升级 P1)。

原进程级内存台账(_registry)收敛进 browser_jobs 表(kind=cookie_check),重启不丢
任务/终态,check_id 永不因重启 404:

- start_check 登记一条 queued 台账行立即返回 check_id;NBDPSY_ROLE=all 时经
  spawn_inline 在本进程消费(claim→execute→finish 纪律),role=api 时纯 enqueue
  交 worker 进程执行;
- execute() 为提炼出的契约执行函数(account_worker 子进程与进程内消费共用):
  持号锁串行 → 浏览器闸 → 线程内跑登录检测 → 写回账号,**不碰 browser_jobs 台账**
  (claim/finish 由调用方负责);
- 写回沿用既有语义:valid/invalid/captcha/restricted 写回 cookie_status/last_check_at +
  回填 user_info;error(基础设施失败)**不写回、保留原值**,避免把好号误标失效;
- ``restricted``(2026-07-31 新增)= cookie 有效但账号被小红书挂了风控验证墙:检测在
  首页登录判定之外多探一次**他人主页**可达性,撞墙即判定,并把事件落 ``risk_events``;
- 同号浏览器操作靠**共享 AccountLocks**(app.browser.account_locks 的进程级单例)与
  发布链串行:检测与发布共用同一 profile 目录,SyncClient.start() 的 kill_orphans
  会按 argv 精确杀该 profile 的所有 camoufox,不串行会互杀,故不能各持独立锁;
- 安全:payload 不存明文 cookie,execute 时从账号行重新解密(login_cookies 落库
  本就是 Fernet 加密,台账不能开明文旁路)。
"""

import asyncio
import json
from datetime import datetime

from loguru import logger
from sqlalchemy import select

from app.browser import sync_client
from app.browser.account_locks import account_locks
from app.browser.browser_gate import browser_slot
from app.core.db import get_session
from app.core.security import decrypt_cookies
from app.models.xhs_account import XhsAccount
from app.services import browser_jobs_repo, risk_events

# check_login_once 返回 user_info 时回填到账号的字段(与 cookie_checker 一致的子集)
_USER_INFO_FIELDS = ("nickname", "user_id", "red_id", "avatar")


def start_check(account_id: int, cookies: list[dict]) -> str:
    """登记一条 browser_jobs 台账并(all 模式)派进程内执行,立即返回 check_id。

    cookies 参数仅保签名兼容(REST 层解密后传入):执行时从账号行重新解密取最新值,
    不落明文进台账。
    """
    payload: dict = {}
    check_id = browser_jobs_repo.enqueue_from_request(
        "cookie_check", payload, account_id=account_id
    )
    browser_jobs_repo.spawn_inline(
        check_id, lambda: execute(account_id, payload)
    )
    return check_id


def get_check(check_id: str) -> dict | None:
    """按 check_id 读台账并映射回既有 entry 形状;不存在返回 None(REST 报 404)。

    status 映射:queued/running → checking;done → 检测结果四态(valid/invalid/
    captcha/error);台账 error(执行崩溃)→ error + reason。
    """
    row = browser_jobs_repo.get_job_sync(
        browser_jobs_repo.current_db_path(), check_id
    )
    if row is None or row["kind"] != "cookie_check":
        return None
    entry = {
        "status": "checking",
        "account_id": row["account_id"],
        "user_info": None,
        "reason": None,
        "wall": None,
    }
    result = row["result"] or {}
    if row["status"] == "done":
        entry["status"] = result.get("status", "error")
        entry["user_info"] = result.get("user_info")
        entry["reason"] = result.get("reason")
        entry["wall"] = result.get("wall")
    elif row["status"] == "error":
        entry["status"] = "error"
        entry["reason"] = result.get("error")
    return entry


async def execute(account_id: int, payload: dict) -> dict:
    """执行一次 cookie 活性检测(契约函数,不碰 browser_jobs 台账)。

    返回 {"status","user_info","reason","wall"};任何意外兜底为 status=error,绝不抛出
    (error 属检测结果语义,台账仍记 done——与"执行崩溃"的台账 error 区分)。

    检测在首页登录判定之外**多探一次他人主页**(探测目标见 ``pick_probe_user_id``):
    撞验证墙 → status=restricted 写回账号 + 事件落 risk_events 留痕。
    """
    try:
        cookies = await load_account_cookies(account_id)
        probe_user_id = await pick_probe_user_id(get_session, account_id)
        # 与发布链共用同一把 per-account 锁:同号发布/检测串行,避免 kill_orphans 互杀。
        async with account_locks.get(account_id):
            # 全局浏览器并发闸:封顶总 camoufox 数,超出排队(仅罩浏览器段,不含写回)。
            async with browser_slot():
                result = await _run_check_with_watchdog(
                    account_id, cookies, probe_user_id
                )
        status = result.get("status", "invalid")
        user_info = result.get("user_info")
        reason = result.get("reason")
        wall = result.get("wall")

        # error:基础设施失败,不写回账号(保留原 cookie_status),仅报结果。
        if status != "error":
            await _write_back(account_id, status, user_info)
        # 撞墙留痕(captcha / restricted 都带 wall):落库失败不影响检测结论。
        await risk_events.record_wall(get_session, account_id, wall, "cookie_check")
        return {
            "status": status,
            "user_info": user_info,
            "reason": reason,
            "wall": wall,
        }
    except Exception as exc:  # 兜底:检测异常也要给终态结果,别让轮询方死等
        logger.exception(f"cookie 检测任务异常 account_id={account_id}")
        return {"status": "error", "user_info": None, "reason": f"检测任务异常:{exc}"}


# 浏览器段看门狗:正常全链检测(launch+首页+API+个人页+他人主页探测)实测 <90s,
# 180s 只逮真僵死(camoufox launch 卡死 / driver 死锁),不误杀慢而正常的检测。
_WATCHDOG_S = 180.0
# 强杀浏览器后等检测线程归队的上限:阻塞的 playwright 调用在浏览器死掉后应立刻抛错。
_REJOIN_GRACE_S = 30.0


def _kill_profile_browser(account_id: int) -> None:
    """强杀该账号 profile 的全部 camoufox 进程(看门狗超时专用,同步函数)。"""
    from app.browser.profile_guard import kill_orphans, profile_dir

    kill_orphans(profile_dir(account_id))


async def _run_check_with_watchdog(
    account_id: int, cookies: list[dict], probe_user_id: str | None
) -> dict:
    """跑 check_login_once 并加墙钟看门狗;超时先杀浏览器、限时 rejoin,再返回 error。

    为什么不能裸 ``asyncio.wait_for(to_thread(...))``:超时只取消 await,同步线程还
    抱着浏览器活着;锁一放,下一个同号任务 SyncClient.start() 里的 kill_orphans 会与
    残存会话互杀。正确顺序是**先杀浏览器**(让阻塞的 sync 调用抛错、线程尽快返回)→
    限时等线程归队 → 才把锁还回去。超时结果一律判 error(超时):被强杀线程的迟到
    结果不可信(浏览器中途被杀),不能拿去写回账号状态——error 语义正好是"基础设施
    失败,保留账号原值"。
    """
    task = asyncio.ensure_future(
        asyncio.to_thread(
            sync_client.check_login_once, account_id, cookies, probe_user_id
        )
    )
    try:
        # shield:超时只取消外层等待,不取消线程 task(to_thread 本就不可取消)
        return await asyncio.wait_for(asyncio.shield(task), timeout=_WATCHDOG_S)
    except asyncio.TimeoutError:
        logger.warning(
            f"[cookie_check] 检测超过 {_WATCHDOG_S:.0f}s 未返回,强杀浏览器 "
            f"account_id={account_id}"
        )
        try:
            await asyncio.to_thread(_kill_profile_browser, account_id)
        except Exception:
            logger.exception(f"[cookie_check] 强杀浏览器失败 account_id={account_id}")
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=_REJOIN_GRACE_S)
        except asyncio.TimeoutError:
            # 极端僵死:线程连浏览器被杀都唤不醒(如 driver 启动死锁)。浏览器进程已杀,
            # 放锁是安全的;线程泄漏记日志观察,不为它把整条检测队列拖死。
            logger.error(
                f"[cookie_check] 强杀后检测线程 {_REJOIN_GRACE_S:.0f}s 仍未归队,"
                f"放弃等待(线程泄漏,浏览器已杀)account_id={account_id}"
            )
        except Exception:
            logger.exception(
                f"[cookie_check] 检测线程收尾异常(已按超时处理)account_id={account_id}"
            )
        return {
            "status": "error",
            "user_info": None,
            "reason": (
                f"检测超时(>{_WATCHDOG_S:.0f}s 未返回):已强杀浏览器,"
                "账号状态保留原值,可直接重试"
            ),
        }


async def load_account_cookies(account_id: int) -> list[dict]:
    """从账号行解密 login_cookies 回列表;无号/空 cookie → []。

    note_export / note_delete 的 execute 也复用(payload 不存明文 cookie 的统一取数口)。
    """
    async with get_session() as session:
        account = await session.get(XhsAccount, account_id)
    if account is None or not account.login_cookies:
        return []
    plaintext = decrypt_cookies(account.login_cookies)
    if not plaintext:
        return []
    return json.loads(plaintext)


async def pick_probe_user_id(session_factory, account_id: int) -> str | None:
    """挑一个「他人主页」探测目标:矩阵内**另一个**账号的 user_id(按 id 升序取第一个)。

    用矩阵内账号互为探测目标,而不是写死某个站外 user_id:不引入对陌生账号是否还存在的
    依赖,也不给固定某个陌生人主页刷访问。取不到(库里只有本号 / 别的号都还没回填
    user_id)→ 返回 None,调用方**跳过这一步且不报错**:探测是增量能力,没目标就退化回
    原来的首页判定,绝不能因此让整次检测失败。

    ``session_factory`` 用法同 ``risk_events.record_wall``,``get_session`` 与后台巡检的
    ``async_sessionmaker`` 都能传。查库出错同样按"取不到"处理(告警 + None),不上抛。
    """
    try:
        async with session_factory() as session:
            result = await session.execute(
                select(XhsAccount.user_id)
                .where(
                    XhsAccount.id != account_id,
                    XhsAccount.user_id.isnot(None),
                    XhsAccount.user_id != "",
                )
                .order_by(XhsAccount.id)
                .limit(1)
            )
            return result.scalars().first()
    except Exception as exc:  # noqa: BLE001 — 探测目标取不到只降级,不能搞崩检测
        logger.warning(f"[cookie_check] 取他人主页探测目标失败,跳过风控墙探测: {exc}")
        return None


async def _write_back(account_id: int, status: str, user_info: dict | None) -> None:
    """把 valid/invalid/captcha/restricted 写回 cookie_status/last_check_at,并回填非空 user_info。

    用 get_session()(读 db_module.async_session,测试对其 monkeypatch 生效),会话内重取
    账号避免操作 detached 实例。
    """
    async with get_session() as session:
        account = await session.get(XhsAccount, account_id)
        if account is not None:
            account.cookie_status = status
            account.last_check_at = datetime.utcnow()
            if user_info:
                for field in _USER_INFO_FIELDS:
                    value = user_info.get(field)
                    if value:
                        setattr(account, field, value)
                # 内部展示名跟随小红书昵称实时更新:运营在小红书 App 改了账号名后,
                # 一次检测即把 name 同步为最新昵称(前端/插件展示 name),名称随平台侧收敛。
                nickname = user_info.get("nickname")
                if nickname:
                    account.name = nickname
            await session.commit()
    # 新账号首次基底:cookie 确认 valid 且从未有快照 → 立即 enqueue 一次数据采集。
    # 绝不打断检测主流程(基底失败只记日志,调度器每小时兜底)。
    if status == "valid":
        try:
            import app.core.db as db_module
            from app.services.note_metrics_scheduler import ensure_baseline

            await ensure_baseline(db_module.async_session, account_id)
        except Exception:
            logger.exception(f"基底采集 enqueue 失败(不影响检测)account_id={account_id}")
