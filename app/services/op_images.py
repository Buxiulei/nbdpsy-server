"""一致性生图 job 服务:进程级内存台账 + 后台锚点法批量出图 + 去水印后处理。

契约(自薯营家迁移,skill 侧 gen_images.py 零改动恢复,见 NBDpsy 仓
《2026-07-23-一致性生图未迁移-协同记录.md》):

- ``start_images_job(prompts, anchor_url)`` → (job_id:int, session_id:str),立即返回;
- 轮询语义 ``queued|running|done|failed``:
  - **额度错/单页失败表现为 done + errors 有值**(不是整任务 failed)——failed 只留给
    任务级意外崩溃;
  - done 时 ``result.urls`` 与提交 prompts **按下标对齐**(失败位为空串 ""),
    ``result.errors`` 为与 urls 等长的消息数组(成功位空串);
- ``result.urls`` 是相对 ``/uploads/{dir}/{name}`` 路径,拼 base 即公网直链、免鉴权
  (不可猜目录名即访问控制,与视频/发布产物同款);
- ``anchor_url``(P1 闸门):非空时解析回本地 uploads 文件让全部页锚定它(不再重画
  P1);解析不到 → 整批失败位 + errors,不静默降级。

台账为进程内存 dict(对齐 note_export 等三兄弟);批量出图可长达数十分钟,终态
TTL 放宽到 2 小时。
"""

import asyncio
import itertools
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Optional
from urllib.parse import urlparse

from loguru import logger

from app.core.config import settings
from app.imagegen.openai_image import OpenAIImageProvider
from app.imagegen.postprocess import dewatermark

_TERMINAL_STATUSES = ("done", "failed")
_ENTRY_TTL = timedelta(hours=2)

# (session_id, job_id) -> {"status","result","created_at"} 的进程级台账。
_registry: dict[tuple[str, int], dict] = {}
_tasks: set[asyncio.Task] = set()
# job_id 进程内自增(契约要求 int;进程重启从头计,session_id 全局唯一故键不撞)。
_job_seq = itertools.count(1)


def _uploads_root() -> Path:
    return (Path(settings.DATA_DIR) / "uploads").resolve()


def _evict_stale() -> None:
    """驱逐超龄终态条目(进行中不动),防台账无界增长。"""
    cutoff = datetime.utcnow() - _ENTRY_TTL
    stale = [
        key for key, entry in _registry.items()
        if entry["status"] in _TERMINAL_STATUSES and entry["created_at"] <= cutoff
    ]
    for key in stale:
        _registry.pop(key, None)


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
    """登记 queued 台账并起后台生图任务,立即返回 (job_id, session_id)。"""
    _evict_stale()
    job_id = next(_job_seq)
    session_id = uuid.uuid4().hex
    _registry[(session_id, job_id)] = {
        "status": "queued",
        "result": {},
        "created_at": datetime.utcnow(),
    }
    task = asyncio.create_task(_run_job(session_id, job_id, prompts, anchor_url))
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
    return job_id, session_id


def get_images_job(session_id: str, job_id: int) -> dict | None:
    """取台账条目;不存在返回 None。"""
    _evict_stale()
    return _registry.get((session_id, job_id))


async def _run_job(
    session_id: str, job_id: int, prompts: List[str], anchor_url: Optional[str]
) -> None:
    """后台生图:锚点法批量 → 逐张去水印 → 按下标对齐落 result;意外崩溃才 failed。"""
    key = (session_id, job_id)
    entry = _registry.get(key)
    if entry is None:
        return
    entry["status"] = "running"
    try:
        # 外部锚点先解析;解析不到按契约整批报错(done + 全失败位),不静默降级。
        anchor_path: Optional[str] = None
        if anchor_url:
            anchor_path = resolve_anchor_path(anchor_url)
            if not anchor_path:
                msg = f"anchor_url 解析失败/文件不存在: {anchor_url}"
                entry["result"] = {
                    "urls": ["" for _ in prompts],
                    "errors": [msg for _ in prompts],
                }
                entry["status"] = "done"
                return

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

        entry["result"] = {"urls": urls, "errors": errors}
        entry["status"] = "done"  # 额度错/单页失败也是 done+errors,failed 只留给崩溃
        ok = sum(1 for u in urls if u)
        logger.info(
            f"[op_images] job 完成 session={session_id} job={job_id}: "
            f"{ok}/{len(prompts)} 成功")
    except Exception as exc:  # noqa: BLE001 — 任务级意外崩溃才 failed
        logger.exception(f"[op_images] job 崩溃 session={session_id} job={job_id}")
        entry["result"] = {"error": str(exc)}
        entry["status"] = "failed"
