"""一致性生图 job 服务:browser_jobs 台账落库 + 契约 execute()(2026-07-24 架构升级 P1)。

契约(自薯营家迁移,skill 侧 gen_images.py 零改动恢复,见 NBDpsy 仓
《2026-07-23-一致性生图未迁移-协同记录.md》):

- ``start_images_job(prompts, anchor_url)`` → (job_id:int, session_id:str),立即返回;
  台账落 browser_jobs(kind=op_images,account_id=NULL),id 用复合串
  "opimg_{session_id}_{ext_job_id}"(对外 (session_id, job_id) 二元组编进单表主键),
  对外 ext job_id 恒 1(原进程内自增序号已随内存台账废弃,session_id 全局唯一故不撞);
- 轮询语义 ``queued|running|done|failed``:
  - **额度错/单页失败表现为 done + errors 有值**(不是整任务 failed)——failed 只留给
    任务级意外崩溃(台账 error 行,含僵死恢复);
  - done 时 ``result.urls`` 与提交 prompts **按下标对齐**(失败位为空串 ""),
    ``result.errors`` 为与 urls 等长的消息数组(成功位空串);
- ``result.urls`` 是相对 ``/uploads/{dir}/{name}`` 路径,拼 base 即公网直链、免鉴权
  (不可猜目录名即访问控制,与视频/发布产物同款);
- ``anchor_url``(P1 闸门):非空时解析回本地 uploads 文件让全部页锚定它(不再重画
  P1);解析不到 → 整批失败位 + errors,不静默降级;
- execute(payload) 为契约执行函数(不碰 browser_jobs 台账):正常返回
  {"urls","errors"},任务级崩溃返回 {"error": str}(调用方据 "error" 键落台账 error)。
"""

import uuid
from pathlib import Path
from typing import List, Optional
from urllib.parse import urlparse

from loguru import logger

from app.core.config import settings
from app.imagegen.openai_image import OpenAIImageProvider
from app.imagegen.postprocess import dewatermark
from app.services import browser_jobs_repo


def _uploads_root() -> Path:
    return (Path(settings.DATA_DIR) / "uploads").resolve()


def _ledger_id(session_id: str, job_id: int) -> str:
    """(session_id, job_id) 二元组 → browser_jobs 复合主键串。"""
    return f"opimg_{session_id}_{job_id}"


def resolve_anchor_path(anchor_url: str) -> Optional[str]:
    """把 anchor_url(绝对或相对 /uploads/... URL)解析回本地文件路径。

    仅接受落在 DATA_DIR/uploads 下且真实存在的文件(realpath 校验防路径穿越);
    解析不到返回 None(调用方按契约报错,不静默降级)。
    """
    try:
        path = urlparse(anchor_url).path
        if not path.startswith("/uploads/"):
            return None
        rel = path[len("/uploads/"):]
        root = _uploads_root()
        target = (root / rel).resolve()
        if not str(target).startswith(str(root) + "/"):
            return None  # 路径穿越
        return str(target) if target.is_file() else None
    except Exception:  # noqa: BLE001
        return None


def start_images_job(
    prompts: List[str], anchor_url: Optional[str] = None
) -> tuple[int, str]:
    """登记 browser_jobs 台账并(all 模式)派进程内执行,立即返回 (job_id, session_id)。"""
    job_id = 1  # 对外 ext job_id 恒 1(session_id 全局唯一,复合主键不撞)
    session_id = uuid.uuid4().hex
    payload = {"prompts": list(prompts), "anchor_url": anchor_url}
    ledger_id = browser_jobs_repo.enqueue_from_request(
        "op_images", payload, account_id=None, job_id=_ledger_id(session_id, job_id)
    )
    browser_jobs_repo.spawn_inline(ledger_id, lambda: execute(payload))
    return job_id, session_id


def get_images_job(session_id: str, job_id: int) -> dict | None:
    """按 (session_id, job_id) 读台账并映射回既有 entry 形状;不存在返回 None。

    status 映射:queued/running 原样;done → done + result;台账 error(执行崩溃/
    僵死恢复)→ failed + result({"error": ...})。
    """
    row = browser_jobs_repo.get_job_sync(
        browser_jobs_repo.current_db_path(), _ledger_id(session_id, job_id)
    )
    if row is None or row["kind"] != "op_images":
        return None
    result = row["result"] or {}
    if row["status"] == "done":
        return {"status": "done", "result": result}
    if row["status"] == "error":
        return {"status": "failed", "result": result}
    return {"status": row["status"], "result": {}}


async def execute(payload: dict) -> dict:
    """执行一次锚点法批量生图(契约函数,不碰 browser_jobs 台账)。

    正常(含额度错/单页失败/锚点解析失败)返回 {"urls","errors"}(下标对齐);
    任务级意外崩溃返回 {"error": str},不抛出。
    """
    prompts: List[str] = (payload or {}).get("prompts") or []
    anchor_url: Optional[str] = (payload or {}).get("anchor_url")
    try:
        # 外部锚点先解析;解析不到按契约整批报错(done + 全失败位),不静默降级。
        anchor_path: Optional[str] = None
        if anchor_url:
            anchor_path = resolve_anchor_path(anchor_url)
            if not anchor_path:
                msg = f"anchor_url 解析失败/文件不存在: {anchor_url}"
                return {
                    "urls": ["" for _ in prompts],
                    "errors": [msg for _ in prompts],
                }

        # job 专属产物目录:不可猜 token 目录名即访问控制(/uploads 免鉴权直链)。
        dirname = f"opimg_{uuid.uuid4().hex[:12]}"
        out_dir = _uploads_root() / dirname
        provider = OpenAIImageProvider(save_dir=str(out_dir))

        results = await provider.generate_batch(
            prompts, anchor_path=anchor_path, save_prefix="p")

        # 逐张去水印(reraster 主路 + PIL 兜底,绝不阻断),终名改页序 NN.png——
        # /uploads/{batch}/{name} 免鉴权路由的 _NAME_RE 只放行两位数字文件名
        # (上传批次既有约定),生图产物遵守同一约定,不放宽安全白名单。
        urls: List[str] = []
        errors: List[str] = []
        for i, r in enumerate(results):
            if r.success and r.path:
                final_path = Path(await dewatermark(r.path))
                # 扩展名跟随真实格式(去水印后为 .jpg;免鉴权路由白名单 png/jpg/webp)
                ext = final_path.suffix.lower()
                if ext not in (".png", ".jpg", ".jpeg", ".webp"):
                    ext = ".png"
                serve_path = out_dir / f"{i + 1:02d}{ext}"
                final_path.rename(serve_path)
                urls.append(f"/uploads/{dirname}/{serve_path.name}")
                errors.append("")
            else:
                urls.append("")
                errors.append(r.error or "unknown")
        # 结果长度与 prompts 对齐的兜底(provider 契约本就对齐,此处防御截断)
        while len(urls) < len(prompts):
            urls.append("")
            errors.append("result_missing")

        ok = sum(1 for u in urls if u)
        logger.info(f"[op_images] job 完成: {ok}/{len(prompts)} 成功")
        return {"urls": urls, "errors": errors}
    except Exception as exc:  # noqa: BLE001 — 任务级意外崩溃才 failed
        logger.exception("[op_images] job 崩溃")
        return {"error": str(exc)}
