"""创作中心笔记数据导出器(纯同步,吃已登录 page)。

移植自老仓 `creator_center_exporter`,适配为独立函数:调用方传入一个已建好
creator 域登录态的同步 Playwright ``page``,导出器完成
    主站预热 → creator warm-up(建 SSO session)→ 数据看板 → 内容分析 →
    导出数据(下载 Excel)→ openpyxl 解析
拿每条笔记 13 项指标,返回 list[dict]。全程兜底,任一步失败抛 ``CreatorExportError``
(含 reason),绝不静默返回半截数据。

关键约定:
- 导出器**不自取** ``datetime.now()`` —— 文件名时间戳 ``ts`` 与下载目录 ``download_dir``
  均由调用方(service 层)传入,便于测试与统一时间基准。
- 中文菜单/按钮选择器接进已有 ``SelfHealLocator``:硬编码失效且自愈开关开 + 配了
  ``LLM_API_KEY`` 时才 fallback LLM 定位;默认关时纯硬编码,与不接自愈逐字节等价。
"""

import logging
import os
import time
from typing import Any, Dict, List, Optional

from app.core.config import settings
from app.browser.self_heal import SelfHealLocator
from app.browser.sync_human_actions import SyncHumanActions

logger = logging.getLogger(__name__)


# Excel 中文列名 → 代码字段映射(13 项:标题 + 发布时间 + 11 指标)。
COLUMN_MAPPING: Dict[str, str] = {
    "笔记标题": "title",
    "首次发布时间": "publish_time",
    "点赞": "likes",
    "收藏": "collects",
    "评论": "comments",
    "弹幕": "danmu",
    "分享": "shares",
    "转载": "reposts",
    "涨粉": "follows",
    "封面点击率": "cover_ctr",
    "曝光": "exposure",
    "观看量": "views",
    "人均观看时长": "avg_view_duration",
}

# 整数指标列(逐行走 int 转换)。
_INT_FIELDS = frozenset(
    {"likes", "collects", "comments", "danmu", "shares",
     "reposts", "follows", "exposure", "views"}
)
# 浮点指标列(逐行走 float 转换)。
_FLOAT_FIELDS = frozenset({"cover_ctr", "avg_view_duration"})

# 数据看板左导航一级菜单 —— 数据看板就绪的正向标志。
_DATA_MENU_SELECTOR = '.d-sub-menu:has-text("数据看板")'


class CreatorExportError(Exception):
    """创作中心导出失败。``reason`` 携失败语义(如 ``need_manual_login``)。"""

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


def _safe_int(value: Any) -> int:
    """安全转整数:None / 非数值 / 空串 → 0;容忍千分位逗号与浮点串。"""
    if value is None:
        return 0
    try:
        return int(float(str(value).replace(",", "").strip()))
    except (ValueError, TypeError):
        return 0


def _safe_float(value: Any) -> float:
    """安全转浮点:None / 非数值 → 0.0;容忍千分位逗号与百分号。"""
    if value is None:
        return 0.0
    try:
        return float(str(value).replace(",", "").replace("%", "").strip())
    except (ValueError, TypeError):
        return 0.0


def parse_export_xlsx(path: str, account_id: int) -> List[Dict[str, Any]]:
    """解析创作中心导出的 .xlsx → list[dict](每行 13 字段 + account_id)。

    - 首行定位真正表头(跳过"最多导出前 1000 条"之类提示行,认「笔记标题」列所在行)。
    - 列名对字段做**子串**匹配(容忍 "封面点击率(%)" 之类带单位/后缀的表头)。
    - 逐行按 COLUMN_MAPPING 取值:整数列 int()、浮点列 float、其余原样存字符串;
      缺某列 → 该字段给默认(整数 0 / 浮点 0.0 / 文本 "")不崩。
    - cover_ctr 若为 0<x<1 的小数比率 → ×100 换算成百分数(0.12 → 12.0)。
    - 每行注入 ``account_id``。仅表头无数据 → 返回 []。
    """
    import openpyxl

    wb = openpyxl.load_workbook(path, read_only=True)
    try:
        ws = wb.active
        rows = list(ws.iter_rows(values_only=True))
    finally:
        wb.close()

    if len(rows) < 2:
        # 空表或仅表头(无数据行)
        return []

    # 定位真正的表头行:含「笔记标题」的那一行(跳过前置提示文本行)。
    header_row_idx = 0
    for i, row in enumerate(rows):
        row_texts = [str(c).strip() if c is not None else "" for c in row]
        if "笔记标题" in row_texts:
            header_row_idx = i
            break

    headers = [str(h).strip() if h is not None else "" for h in rows[header_row_idx]]

    # 建立字段 → 列索引映射(子串匹配,首个命中即止)。
    col_map: Dict[str, int] = {}
    for idx, header in enumerate(headers):
        for cn_name, en_name in COLUMN_MAPPING.items():
            if en_name not in col_map and cn_name in header:
                col_map[en_name] = idx
                break

    results: List[Dict[str, Any]] = []
    for row in rows[header_row_idx + 1:]:
        if not row or all(v is None for v in row):
            continue

        note: Dict[str, Any] = {}
        # 遍历全部 13 字段:缺列亦给默认,保证输出 schema 稳定。
        for en_name in COLUMN_MAPPING.values():
            idx = col_map.get(en_name)
            value = row[idx] if (idx is not None and idx < len(row)) else None
            if en_name in _INT_FIELDS:
                note[en_name] = _safe_int(value)
            elif en_name in _FLOAT_FIELDS:
                note[en_name] = _safe_float(value)
            else:
                note[en_name] = str(value) if value is not None else ""

        # 封面点击率统一为百分数:0.082 → 8.2(已是百分数则保持)。
        if 0 < note["cover_ctr"] < 1:
            note["cover_ctr"] = note["cover_ctr"] * 100

        note["account_id"] = account_id
        results.append(note)

    logger.info(
        "[creator_export] 账号%s: 解析完成 %d 条笔记数据", account_id, len(results)
    )
    return results


def _find_creator_element(
    page, selectors: List[str], intent_key: str, desc: str, timeout: int = 30000
):
    """查找可点击元素:先试硬编码选择器,全失效再走自愈兜底。

    - 硬编码:逐个 ``page.wait_for_selector(state="visible")``,命中即返回 handle。
    - 自愈兜底:全部失效 **且** ``settings.SELFHEAL_ENABLED and settings.LLM_API_KEY``
      时,``SelfHealLocator().locate(page, intent_key, desc)`` 取 handle。默认关时此路
      不触发,行为与纯硬编码等价。
    - 全取不到 → 返回 None(由调用方抛 CreatorExportError)。
    """
    for selector in selectors:
        try:
            element = page.wait_for_selector(
                selector, timeout=timeout, state="visible"
            )
            if element:
                logger.info("[creator_export] 命中选择器: %s", selector)
                return element
        except Exception:
            continue

    if settings.SELFHEAL_ENABLED and settings.LLM_API_KEY:
        try:
            found = SelfHealLocator().locate(page, intent_key, desc)
        except Exception as exc:  # locate 已全程兜底,这里再兜一层防御
            logger.warning("[creator_export] 自愈定位异常: %s", exc)
            found = None
        if found:
            handle, _selector = found
            logger.info("[creator_export] 自愈定位成功: intent=%s", intent_key)
            return handle

    return None


def _goto_creator(page, url: str) -> None:
    """goto 一个 creator 域 URL,容忍重定向中断 + 等页面基本加载完成。

    creator 域首访常立即重定向(401 → /login → SSO),浏览器会以 abort 中断原
    goto —— 属预期不致命;吞掉后再等 networkidle。
    """
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=40000)
    except Exception as exc:
        logger.debug("[creator_export] goto %s 被重定向中断: %s", url, exc)
    try:
        page.wait_for_load_state("networkidle", timeout=10000)
    except Exception:
        pass


def _open_data_board(page, account_id: int, max_attempts: int = 3) -> None:
    """warm-up 建立 creator 域 session 并等数据看板就绪(≤max_attempts 轮)。

    每轮 = warm-up(goto /publish/publish 触发 SSO)→ goto /creator/home →
    **正向**等「数据看板」菜单可见。登录页上该菜单永不出现,故 wait 超时即说明本轮
    未认证成功 —— 重整轮 warm-up 再试。全部失败 → 抛
    ``CreatorExportError("need_manual_login")``(creator 域需重扫登录)。
    """
    publish_url = "https://creator.xiaohongshu.com/publish/publish?source=official"
    home_url = "https://creator.xiaohongshu.com/creator/home"

    for attempt in range(1, max_attempts + 1):
        _goto_creator(page, publish_url)  # 触发 creator SSO
        _goto_creator(page, home_url)     # 数据看板首页
        try:
            page.locator(_DATA_MENU_SELECTOR).first.wait_for(
                state="visible", timeout=12000
            )
            logger.info(
                "[creator_export] 账号%s: 数据看板就绪(第 %d/%d 次)",
                account_id, attempt, max_attempts,
            )
            return
        except Exception:
            logger.warning(
                "[creator_export] 账号%s: 数据看板第 %d/%d 次未就绪,重试",
                account_id, attempt, max_attempts,
            )
            time.sleep(2)

    raise CreatorExportError("need_manual_login")


def export_notes(
    page, account_id: int, download_dir: str, ts: str
) -> List[Dict[str, Any]]:
    """导航创作中心 → 下载 Excel → 解析 → list[dict](每行 13 字段 + account_id)。

    Args:
        page: 已建好 creator 登录态的同步 Playwright Page。
        account_id: 账号 ID(注入每行结果)。
        download_dir: 下载保存目录(由调用方传入)。
        ts: 文件名时间戳(由调用方传入),存为 ``export_<account_id>_<ts>.xlsx``。

    Returns:
        list[dict];新号无笔记时返回 []。

    Raises:
        CreatorExportError: 任一步失败(reason 含语义;warm-up 三轮失败为
            ``need_manual_login``)。
    """
    # 合规:导出流程也是真实账号上的 XHS 交互,所有点击走 SyncHumanActions 拟人化。
    human = SyncHumanActions(page)
    try:
        # 1) 主站预热:让浏览器对主站 cookies 完成一次握手(失败不致命)。
        try:
            page.goto(
                "https://www.xiaohongshu.com",
                wait_until="domcontentloaded",
                timeout=30000,
            )
            try:
                page.wait_for_load_state("networkidle", timeout=10000)
            except Exception:
                pass
            human.wait(0.8, 2.0, context="主站预热握手")
        except Exception as exc:
            logger.warning(
                "[creator_export] 账号%s: 主站预热异常: %s;继续 warm-up",
                account_id, exc,
            )

        # 2) creator warm-up + 等数据看板就绪(≤3 轮,失败抛 need_manual_login)。
        _open_data_board(page, account_id)

        # 3) 点「数据看板」→「内容分析」。
        dashboard = _find_creator_element(
            page, [_DATA_MENU_SELECTOR],
            "creator_data_dashboard_menu", "创作中心左侧「数据看板」菜单",
        )
        if dashboard is None:
            raise CreatorExportError("data_dashboard_menu_not_found")
        human.click(dashboard, reason="数据看板菜单")
        human.wait(0.8, 2.0, context="数据看板→内容分析")

        analysis = _find_creator_element(
            page, ['.d-menu-item:has-text("内容分析")'],
            "creator_content_analysis_menu", "数据看板下「内容分析」菜单",
        )
        if analysis is None:
            raise CreatorExportError("content_analysis_menu_not_found")
        human.click(analysis, reason="内容分析菜单")
        human.wait(2.0, 4.0, context="内容分析页加载")

        # 4) 点「导出数据」并等待下载,存到调用方指定路径。
        #    先在 expect_download 之外定位按钮——expect_download 的计时从 __enter__ 起算,
        #    若把定位(可能吃满 30s + 自愈)放进 with 体内,硬编码失效会先耗尽 download waiter,
        #    自愈即便定位成功也已超时。故 with 体内只做 click。
        os.makedirs(download_dir, exist_ok=True)
        file_path = os.path.join(download_dir, f"export_{account_id}_{ts}.xlsx")
        export_btn = _find_creator_element(
            page, ['text=导出数据'],
            "creator_export_button", "内容分析页「导出数据」按钮",
        )
        if export_btn is None:
            raise CreatorExportError("export_button_not_found")
        with page.expect_download(timeout=30000) as download_info:
            human.click(export_btn, reason="导出数据按钮")
            human.wait(0.8, 2.0, context="导出下载触发")
        download_info.value.save_as(file_path)
        logger.info("[creator_export] 账号%s: 文件已保存 %s", account_id, file_path)

        # 5) openpyxl 解析。
        return parse_export_xlsx(file_path, account_id)

    except CreatorExportError:
        # 已带 reason,原样上抛(need_manual_login 等语义不被吞)。
        raise
    except Exception as exc:
        # 下载超时 / 选择器失效 / 解析失败等一律收成 CreatorExportError。
        raise CreatorExportError(f"export_failed: {exc}") from exc
