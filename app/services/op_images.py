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
- ``result.orig_urls``(2026-07-26 新增)与 prompts **同样等长、下标对齐**:去水印**前**的
  provider 原图直链(``NN.orig.png``,无原图处空串)。原图始终留在盘上,即便该页去水印
  失败(``urls[i]==""``)原图依然可取,只是不占"默认交付"那个位;
- **去水印失败 = 该页失败**:``urls[i]=""`` + ``errors[i]`` 写明去水印失败。绝不拿带
  水印的原图冒充默认交付(旧的"只剥元数据"兜底已删,理由见 imagegen/postprocess.py),
  运营按需用 --pages 重出该页,或直接取 ``orig_urls[i]`` 自行处置;
- ``result.attempts``(2026-08-05 新增)与 prompts **同样等长、下标对齐**:该页打到上游的
  实际尝试次数(含 429/超时重试;没打到上游的位为 0)。运营据此判断慢/抖动是不是常态;
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


def _safe_ext(path: Path) -> str:
    """取落盘扩展名,收进 /uploads 免鉴权路由白名单(非法一律当 .png)。"""
    ext = path.suffix.lower()
    return ext if ext in (".png", ".jpg", ".jpeg", ".webp") else ".png"


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
    prompts: List[str], anchor_url: Optional[str] = None, *, aspect_ratio: str = "3:4"
) -> tuple[int, str]:
    """登记 browser_jobs 台账并(all 模式)派进程内执行,立即返回 (job_id, session_id)。"""
    job_id = 1  # 对外 ext job_id 恒 1(session_id 全局唯一,复合主键不撞)
    session_id = uuid.uuid4().hex
    payload = {"prompts": list(prompts), "anchor_url": anchor_url,
               "aspect_ratio": aspect_ratio}
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

    正常(含额度错/单页失败/锚点解析失败)返回 {"urls","errors","orig_urls","attempts"}
    (四者等长且与 prompts 下标对齐);任务级意外崩溃返回 {"error": str},不抛出。
    """
    prompts: List[str] = (payload or {}).get("prompts") or []
    anchor_url: Optional[str] = (payload or {}).get("anchor_url")
    # 出图比例:老台账(改动前入队的 job)没有该键 → 回落 3:4,与改动前行为一致。
    aspect_ratio: str = (payload or {}).get("aspect_ratio") or "3:4"
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
                    "orig_urls": ["" for _ in prompts],
                    "attempts": [0 for _ in prompts],
                }

        # job 专属产物目录:不可猜 token 目录名即访问控制(/uploads 免鉴权直链)。
        dirname = f"opimg_{uuid.uuid4().hex[:12]}"
        out_dir = _uploads_root() / dirname
        provider = OpenAIImageProvider(save_dir=str(out_dir))

        results = await provider.generate_batch(
            prompts, anchor_path=anchor_path, save_prefix="p",
            aspect_ratio=aspect_ratio)

        # 逐张去水印(非整数缩小 0.855 + PNG,失败即该页失败,不拿带水印图冒充交付),
        # 终名改页序 NN.ext 作默认交付;去水印前的原图另存 NN.orig.ext 供提取——
        # /uploads/{batch}/{name} 免鉴权路由的 _NAME_RE 只放行 NN.ext 与 NN.orig.ext
        # 两种形态,生图产物遵守同一约定,不放宽安全白名单。
        urls: List[str] = []
        errors: List[str] = []
        orig_urls: List[str] = []
        attempts: List[int] = []
        for i, r in enumerate(results):
            # 上游实际尝试次数(含重试),与另三条数组等长同序;没打到上游的位为 0。
            meta = getattr(r, "metadata", None) or {}
            attempts.append(int(meta.get("upstream_attempts", 0) or 0))
            if not (r.success and r.path):
                urls.append("")
                errors.append(r.error or "unknown")
                orig_urls.append("")
                continue
            # 先去水印(要读原图),再把原图改名归档——两个不同文件,别把原图 rename 没了
            cleaned = await dewatermark(r.path)
            if cleaned:
                # 扩展名跟随真实格式(去水印后为 .png;免鉴权路由白名单 png/jpg/webp)
                cleaned_path = Path(cleaned)
                serve_path = out_dir / f"{i + 1:02d}{_safe_ext(cleaned_path)}"
                # rename 单独兜底:改名炸了(权限/目标占用等)只塌这一位,绝不冒泡到
                # execute 顶层把整批(含已成功、已付费的页)一起判崩——与 openai_image
                # 的 _edit_one/_fallback_one「保证该下标位不塌陷」同款纪律。
                try:
                    cleaned_path.rename(serve_path)
                    urls.append(f"/uploads/{dirname}/{serve_path.name}")
                    errors.append("")
                except Exception as exc:  # noqa: BLE001
                    logger.warning(f"[op_images] 交付改名失败[{i}](本页塌陷,不影响其它页): {exc}")
                    urls.append("")
                    errors.append(f"交付改名失败: {exc}")
            else:
                urls.append("")
                errors.append("去水印失败(非整数缩小未产出),本页未交付;"
                              "原图见 orig_urls,或用 --pages 重出该页")
            # 原图无论去水印成功与否都保留可取(用户明确要求的提取通道);
            # 同样单独兜底——原图归档失败只让本位 orig_urls 空,不牵连交付与其它页。
            orig_path = Path(r.path)
            orig_serve = out_dir / f"{i + 1:02d}.orig{_safe_ext(orig_path)}"
            try:
                orig_path.rename(orig_serve)
                orig_urls.append(f"/uploads/{dirname}/{orig_serve.name}")
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[op_images] 原图归档失败[{i}](不影响交付): {exc}")
                orig_urls.append("")
        # 结果长度与 prompts 对齐的兜底(provider 契约本就对齐,此处防御截断)
        while len(urls) < len(prompts):
            urls.append("")
            errors.append("result_missing")
            orig_urls.append("")
            attempts.append(0)

        ok = sum(1 for u in urls if u)
        retried = sum(1 for n in attempts if n > 1)
        logger.info(
            f"[op_images] job 完成: {ok}/{len(prompts)} 成功"
            + (f",{retried} 页经上游重试" if retried else ""))
        return {"urls": urls, "errors": errors, "orig_urls": orig_urls,
                "attempts": attempts}
    except Exception as exc:  # noqa: BLE001 — 任务级意外崩溃才 failed
        logger.exception("[op_images] job 崩溃")
        return {"error": str(exc)}
