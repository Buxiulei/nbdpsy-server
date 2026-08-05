"""即梦(Dreamina)视频生成 REST：提交单镜 / 批量 / 查状态 / 积分 / 登录态 + 产物直链。

需求契约 ``NBDpsy/文档/2026-08-05-server需求-即梦视频生成服务化.md``（第三节端点 / 第四节
行为 / 第七节验收）；设计 ``docs/design/2026-08-05-dreamina-clips-design.md``。
业务语义（CLI 调用、状态机、TTL）全在 ``app/services/dreamina.py``，本层只做校验 / 归属 / 视图。

家规沿用：apikey 鉴权中间件 + 202 异步契约 + manifest 自描述 + ``/uploads`` 免鉴权直链，
形态逐条比照 ``video_rest``（归属 ``_can_access``、``serve_video_product`` 的双重防穿越）。

三条与钱直接相关的取舍（需求第四节，每条都有事故背书）：

1. **幂等键先于一切副作用，也先于一切闸**。POST 带 ``client_ref`` 时先查
   ``(created_by, client_ref)``，命中直接回原 clip_id + 当前 status，**零新建、零物化、
   零 CLI 调用**，且不经登录闸/积分闸——重放本身没有副作用，用 503/409 挡它等于把「任务早已
   在队里」报成提交失败，运营重跑就是双倍扣分。批量端点同样逐镜去重（验收第 7 条）。
2. **未登录 503 而不是静默排队**。登录态是本产线唯一发不出去的凭据，失效时排一堆任务进队列
   只会让运营以为在跑（验收第 6 条）。闸只对**真要新建**的镜生效（见第 1 条）。
3. **低积分 warning 不拦截，只有连一镜都提交不起才 409**。扣费 success 才结算、排队中还有
   变数，拦下去等于凭估算拒绝一次真能跑成的提交（需求第四节第 5 条）。

router + MANIFEST_ENTRIES 接线 ``app.http.__init__`` 的 ALL_ROUTERS / ALL_MANIFEST_ENTRIES，
一致性由 tests/test_manifest.py 防漂移测试钉死。
"""

import asyncio
import re
from datetime import datetime
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field, field_validator, model_validator
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.auth.context import AccessDenied, current_operator
from app.core.config import settings
from app.core.db import get_session
from app.core.errors import NotFoundError
from app.models.operator import Operator
from app.models.video_clip import VideoClip
from app.services import dreamina

router = APIRouter()

# ── /uploads/clips 直链的防路径穿越白名单（形态与 video_rest 同款）──────────────
# 目录段 = {clip_id}-{hmac16}（services.dreamina.clip_token_dir）；文件名只允许改名后的
# clip.mp4 / clip_2.mp4…（原名带即梦 submit_id，不进 URL）。逐段正则 + resolve/is_relative_to
# 双保险，结构性排除 ../、空段与隐藏路径。
_TOKEN_DIR_RE = re.compile(r"^vc_[0-9a-f]{10}-[0-9a-f]{16}$")
_NAME_RE = re.compile(r"^clip(_[0-9]{1,2})?\.mp4$")
# 批次号形态（GET batch 的入参闸，与 services.dreamina.new_batch_id 同形）
_BATCH_ID_RE = re.compile(r"^vcb_[0-9a-f]{10}$")

_OPERATIONS = Literal["text2video", "image2video", "multimodal2video"]
_MODELS = Literal["seedance2.0", "seedance2.0fast", "seedance2.0_vip", "seedance2.0fast_vip"]
_RATIOS = Literal["1:1", "3:4", "4:3", "9:16", "16:9", "21:9"]

# 在飞态（GET 汇总用）：尚未落终态的一切状态
_IN_FLIGHT_STATES = ("queued", "submitting", "submitted", "querying")

_POLL_NOTE = (
    "异步契约：拿 clip_id 后每 15-30s 轮询 GET /api/video-clips/{clip_id}。"
    "**即梦排队常达数小时**（高峰 fast 档近 2 小时，fast_vip 约数分钟），status 长期停在 "
    "submitted/querying 是正常排队不是卡死，queued_seconds 给的就是排队秒数。"
    "submit 即占队列位、success 即扣积分，服务端**绝不自动重提**——要换档重发是运营的决策，"
    "旧任务保留，谁先出用谁。"
)

MANIFEST_ENTRIES = [
    {
        "method": "POST", "path": "/api/video-clips",
        "summary": "提交一条即梦视频生成任务（异步入队，202 返回 clip_id）",
        "admin_only": False,
        "params": {
            "operation": "body,str(text2video|image2video|multimodal2video)",
            "prompt": "body,str(1-2000 字)",
            "duration": "body,int(4-15 整数秒)",
            "model": "body,str(seedance2.0|seedance2.0fast|seedance2.0_vip|seedance2.0fast_vip，"
                     "默认 seedance2.0fast)",
            "ratio": "body,str|None(1:1|3:4|4:3|9:16|16:9|21:9；**image2video 传了就 422**，"
                     "其画幅由输入图推断)",
            "image": "body,str|None(图床直链 或 本服务 /uploads 路径；image2video/"
                     "multimodal2video 必填，text2video 传了就 422。不收 base64 大包)",
            "client_ref": "body,str|None(1-64，幂等键)",
        },
        "returns": "{clip_id, status(重放命中时带当前状态), warning?(低积分提示，不拦截)}",
        "errors": "422=参数校验失败（含 image2video+ratio / text2video+image / duration 越界）；"
                  "400=参考图下载失败或不是图片；409=积分不足以再提交任何一镜；"
                  "503=即梦登录态失效（**不会静默排队**）；401=apikey 无效",
        "notes": _POLL_NOTE + " client_ref 幂等：同一运营用同 ref 重发返回**原 clip_id**、"
                 "零新建零扣分（幂等键按运营隔离，跨运营同 ref 互不影响）。"
                 "**重放不过登录闸/积分闸**：ref 命中时即使掉登录或余额见底也照回原 clip_id，"
                 "不会把「早已在队里」报成提交失败。"
                 "命中的若是一条**从没进过即梦队列的 error 行**（参考图物化失败留下的），"
                 "同 ref 重放会**原地复活**它：重新物化参考图 + 回到 queued，clip_id 不变——"
                 "故图源修好后直接用同 ref 重发即可，不必换 ref。图还是坏的则维持 error "
                 "（仍回 202，status=error，错误文案已更新），不会新建第二条。"
                 "已拿到 submit_id / 已跑过 CLI 的 error 行**绝不复活**（资金状态未知，"
                 "复活 = 可能双倍扣分），原样返回。",
    },
    {
        "method": "GET", "path": "/api/video-clips/{clip_id}",
        "summary": "查单条片段任务状态 / 排队秒数 / 产物直链",
        "admin_only": False, "params": {"clip_id": "path,str(vc_ 开头)"},
        "returns": "{clip_id, status, operation, model, submit_id, credit_count, video_url, "
                   "error, last_poll_error, queued_seconds, expires_at, expired, batch_id, "
                   "client_ref, created_at}",
        "errors": "403=非本人任务且非 admin；404=clip 不存在",
        "notes": "status 五态 queued|submitted|querying|done|error。**error 与 last_poll_error "
                 "语义不同**：前者是任务终态失败，后者只是查询接口瞬时故障（任务仍在排队，别据此重发）。"
                 f"done 后 video_url 是免鉴权直链，{settings.CLIP_TTL_DAYS} 天后按 expires_at 清理"
                 "（产物没了但 status 仍是 done、credit_count 保留供对账）。"
                 "**产物过期看 expired 布尔键，不看 error**：error 只装任务失败原因，"
                 "expired=true 且 status=done 表示「片生成成功但产物已过 TTL 被清」，"
                 "此时 video_url 为 null——这不是失败，别按 error 分支处理。",
    },
    {
        "method": "POST", "path": "/api/video-clip-batches",
        "summary": "批量提交多镜（逐镜独立，一镜失败不连坐）",
        "admin_only": False,
        "params": {"shots": f"body,list[同单镜入参]（1-{settings.CLIP_MAX_BATCH} 镜）"},
        "returns": "{batch_id(**纯重放批可能为 null**，见 notes), "
                   "clip_ids[](与 shots **等长同序**，可直接按下标映射 shot-NN), warning?}",
        "errors": "422=结构校验失败或镜数越界；409=积分不足以再提交任何一镜；"
                  "503=即梦登录态失效；401=apikey 无效",
        "notes": "**逐镜按 client_ref 去重**：整批重放（同 shots 同 refs）返回原 clip_ids、"
                 "零新增任务、积分零新增；整批全命中时**登录闸/积分闸都不挂**（掉登录或余额"
                 "见底也照回原 clip_ids，不把「早已在队里」报成提交失败）。"
                 "**batch_id 可能为 null**：整批纯重放时一行都没新建，此时只在命中镜同属一个"
                 "原批次时回那个批次号，否则回 null（绝不现编一个 DB 里没有的号——拿去 "
                 "GET batch 必 404）。**clip 定位一律以 clip_ids 为准**，GET batch 对 null "
                 "不适用。单镜参考图物化失败"
                 "**只让那一镜落 error 行**，其余照常入队（clip_ids 仍等长同序，不连坐）。"
                 "这类 error 行**没进过即梦队列**，图源修好后用同 ref 重放会原地复活它"
                 "（同 clip_id 回 queued，零新建）——不必换 ref、也不会双倍扣分。"
                 "带远程图（http/https）的批在请求内并发下载参考图、单张最多 30s，"
                 "**调用方超时建议 ≥90s**；纯 text2video 批秒级返回。轮询按单镜 GET 逐条查，"
                 "或用 GET /api/video-clip-batches/{batch_id} 汇总。",
    },
    {
        "method": "GET", "path": "/api/video-clip-batches/{batch_id}",
        "summary": "查一批片段的逐镜状态汇总",
        "admin_only": False, "params": {"batch_id": "path,str(vcb_ 开头)"},
        "returns": "{batch_id, clips:[同单镜 GET 视图，按批内序], summary:{total,done,error,in_flight}}",
        "errors": "403=非本人批次且非 admin；404=批次不存在或 batch_id 形态不对（非 vcb_ 开头）",
        "notes": "只含**本批新建**的镜；重放时命中 client_ref 的镜复用原任务、留在其原批次里，"
                 "故重放批的汇总可能比 clip_ids 短，按 clip_ids 逐条查最准。"
                 "**纯重放批的 batch_id 是 null，本端点对它不适用**——那种批没有属于自己的"
                 "新建行，拿 clip_ids 逐条 GET 单镜即可。",
    },
    {
        "method": "GET", "path": "/api/video-credits",
        "summary": "查即梦积分余额（公司号，集中可观测）",
        "admin_only": False, "params": {},
        "returns": "{credit, low_threshold_hit, logged_in}",
        "errors": "401=apikey 无效",
        "notes": f"low_threshold_hit = credit < {settings.CLIP_CREDIT_LOW_WATERMARK}（低水位提示）。"
                 "登录态失效时 credit 为 null、logged_in=false，仍回 200。余额有 60s 缓存。",
    },
    {
        "method": "GET", "path": "/api/dreamina-status",
        "summary": "即梦登录态健康检查（skill 侧 auto 后端探测入口）",
        "admin_only": False, "params": {},
        "returns": "{logged_in, credit, compliance_confirmed_models[]}",
        "errors": "401=apikey 无效",
        "notes": f"CLI 路径 {settings.DREAMINA_BIN}（登录态文件在该 CLI 的 ~/.dreamina_cli/，"
                 "公司号一份，运营侧零登录；失效时管理员重扫码后把整目录 scp 到 server）。"
                 "compliance_confirmed_models 是**观测近似**——DB 里真出过片的模型列表，"
                 "不在其中不代表未授权（CLI 无查询授权状态的接口）。",
    },
]


# ── 请求模型 ────────────────────────────────────────────────────────────────
class CreateClipRequest(BaseModel):
    """单镜入参。校验矩阵直接对应 CLI 的真实能力，宁可 422 也不静默丢字段。"""

    operation: _OPERATIONS
    prompt: str = Field(min_length=1, max_length=2000)
    duration: int = Field(ge=4, le=15)
    ratio: _RATIOS | None = None
    model: _MODELS = dreamina.DEFAULT_MODEL
    image: str | None = None
    client_ref: str | None = Field(default=None, min_length=1, max_length=64)

    @model_validator(mode="after")
    def _check_media_matrix(self) -> "CreateClipRequest":
        """operation × (image, ratio) 的合法组合闸。

        - ``image2video``：必须有 image；**有 ratio 就 422**——CLI 不收该参数，画幅由输入图
          推断，静默丢弃会让调用方以为设置生效（需求第三节明确「别静默吞」）。
        - ``text2video``：不收 image。语义清晰优于静默忽略（本地 CLI 的 text2video 也是忽略
          images 的，skill 侧不会发，发了就是调用方 bug）。
        - ``multimodal2video``：必须有 image（server 单镜契约只有这一个媒体位）。
        """
        if self.operation == "image2video":
            if not self.image:
                raise ValueError("image2video 必须提供 image（图床直链或 /uploads 路径）")
            if self.ratio:
                raise ValueError("image2video 的画幅由输入图推断，不接受 ratio（CLI 不收该参数）")
        elif self.operation == "text2video":
            if self.image:
                raise ValueError("text2video 不接受 image（生成不参考图片）")
        elif not self.image:
            raise ValueError("multimodal2video 必须提供 image")
        return self


class CreateBatchRequest(BaseModel):
    """批量入参：整批结构校验（结构错 = 调用方 bug，整批 422 可接受）。"""

    shots: list[CreateClipRequest]

    @field_validator("shots")
    @classmethod
    def _check_size(cls, v: list) -> list:
        if not v:
            raise ValueError("shots 不能为空")
        if len(v) > settings.CLIP_MAX_BATCH:
            raise ValueError(f"单批最多 {settings.CLIP_MAX_BATCH} 镜，收到 {len(v)}")
        return v


# ── 视图 / 归属 ─────────────────────────────────────────────────────────────
def _can_access(clip: VideoClip, op: Operator) -> bool:
    """归属校验：admin 全量，否则仅本人 created_by（与 video_rest 逐字同义）。"""
    return op.role == "admin" or clip.created_by == op.id


def _public_status(clip: VideoClip) -> str:
    """内部态 submitting 对外映射回 queued（契约状态机只有五态）。"""
    return "queued" if clip.status == dreamina.INTERNAL_SUBMITTING else clip.status


def _queued_seconds(clip: VideoClip) -> int | None:
    """在飞任务已排队秒数；未提交（或已终态）为 null。

    「等待不是卡死」：把排队时长如实下发，调用方才能把「在排队（正常）」与「查询接口连续
    失败（异常，见 last_poll_error）」分开——服务端**不会**据此自动重提。
    """
    if clip.status not in ("submitted", "querying") or clip.submitted_at is None:
        return None
    return max(0, int((datetime.utcnow() - clip.submitted_at).total_seconds()))


def _is_expired(clip: VideoClip) -> bool:
    """done 行的产物是否已过期 / 已被 TTL 清理。

    产物没了**不是任务失败**，故不写进 ``error``（那一格只装任务失败原因，掺进清理说明会让
    运营和 skill 侧的 error 分支把一条成功的片当成失败）。改成这里按状态算出来：
    ``video_url`` 已被 reaper 清空，或 ``expires_at`` 已过（reaper 还没跑到），都算过期。
    非 done 行恒 False——「产物过期」对它们没有意义。
    """
    if clip.status != "done":
        return False
    if clip.video_url is None:
        return True
    return clip.expires_at is not None and clip.expires_at < datetime.utcnow()


def _clip_payload(clip: VideoClip) -> dict:
    """对外单条视图（字段名与 skill 侧 K_* 常量逐字对齐）。"""
    return {
        "clip_id": clip.clip_id,
        "status": _public_status(clip),
        "operation": clip.operation,
        "model": clip.model,
        "submit_id": clip.submit_id,
        "credit_count": clip.credit_count,
        "video_url": clip.video_url,
        "error": clip.error,
        "last_poll_error": clip.last_poll_error,
        "queued_seconds": _queued_seconds(clip),
        "expires_at": clip.expires_at.isoformat() if clip.expires_at else None,
        "expired": _is_expired(clip),
        "batch_id": clip.batch_id,
        "batch_index": clip.batch_index,
        "client_ref": clip.client_ref,
        "created_at": clip.created_at.isoformat() if clip.created_at else None,
    }


# ── 提交公共件 ──────────────────────────────────────────────────────────────
async def _require_login() -> dict:
    """登录态闸：失效即 503 明确报错，**绝不静默排队**（验收第 6 条）。"""
    status = await dreamina.get_credit_status()
    if not status.get("logged_in"):
        raise HTTPException(
            503,
            "即梦登录态失效（服务端 dreamina CLI 未登录或不可用），任务未入队。"
            f"处置：管理员重跑扫码登录后把 ~/.dreamina_cli/ 整目录放到服务账号家目录。"
            f"细节：{status.get('error') or '无'}",
        )
    return status


def _guard_credit(credit: int | None) -> None:
    """余额连最便宜一镜（5s fast = 25）都不够才 409；其余一律放行给 warning。"""
    if credit is not None and credit < dreamina.MIN_CLIP_CREDIT:
        raise HTTPException(
            409,
            f"即梦积分余额 {credit} 不足以再提交任何一镜（最低一镜约 "
            f"{dreamina.MIN_CLIP_CREDIT} 积分），请先充值",
        )


def _low_credit_warning(credit: int | None, estimate: int | None) -> str | None:
    """低积分提示（不拦截：扣费 success 才结算，排队中还有变数）。"""
    if credit is None or not estimate or credit >= estimate:
        return None
    return (f"积分余额 {credit} 低于本次粗估消耗 {estimate}（按 5s 档价×时长粗估），"
            "已照常入队；扣费在 success 时才结算，余额不足的镜可能失败")


async def _materialize(clip_id: str, req: CreateClipRequest) -> tuple[str | None, str | None]:
    """物化参考图 → (本地路径, 错误说明)。无 image 时两个都是 None。"""
    if not req.image:
        return None, None
    try:
        path = await dreamina.materialize_ref_image(req.image, dreamina.clip_dir(clip_id))
        return str(path), None
    except ValueError as exc:
        return None, str(exc)


def _apply_revive(clip: VideoClip, req: CreateClipRequest,
                  image_path: str | None, img_error: str | None) -> None:
    """把复活的物化结果落到行上（**同一行同一 clip_id**，不新建）。

    物化再失败就维持 error、只更新文案——图源还是坏的，重置回 queued 只会让调度器提交一条
    没有参考图的任务。行仍然是可复活的（没碰过 CLI），下次再重放还有机会。
    """
    if img_error:
        clip.error = img_error
        clip.finished_at = datetime.utcnow()
        return
    clip.image_source = req.image
    clip.image_path = image_path
    clip.status = "queued"
    clip.error = None
    clip.finished_at = None


async def _revive_clips(session, items: list[tuple[VideoClip, CreateClipRequest]]) -> None:
    """复活一批「从没跑过 submit CLI」的 error 行：重新物化参考图 → 重置回 queued。

    参考图**并发**物化（理由同新建镜：逐张串行 N×30s 会顶穿调用方超时）。
    """
    if not items:
        return
    results = await asyncio.gather(
        *(_materialize(clip.clip_id, req) for clip, req in items))
    for (clip, req), (image_path, img_error) in zip(items, results):
        _apply_revive(clip, req, image_path, img_error)
    await session.commit()


async def _insert_clip(session, op: Operator, req: CreateClipRequest, *, clip_id: str,
                       batch_id: str | None = None, batch_index: int | None = None,
                       image_path: str | None = None,
                       error: str | None = None) -> tuple[VideoClip, bool]:
    """落一行任务；返回 (clip, 是否真新建)。

    ``clip_id`` 由调用方先生成（参考图要先物化进 ``clip_dir(clip_id)``，两者必须是同一个 id，
    否则图落在一个目录、任务指向另一个目录）。

    ``(created_by, client_ref)`` 唯一约束是幂等的最后一道闸：并发同 ref 双发时先到者建成、
    后到者撞 IntegrityError → 回滚重查拿到同一条（**绝不新建第二条**，那就是双倍扣分）。
    """
    clip = VideoClip(
        clip_id=clip_id,
        batch_id=batch_id,
        batch_index=batch_index,
        client_ref=req.client_ref,
        operation=req.operation,
        prompt=req.prompt,
        model=req.model,
        duration=req.duration,
        ratio=req.ratio,
        image_source=req.image,
        image_path=image_path,
        status="error" if error else "queued",
        error=error,
        created_by=op.id,
    )
    if error:
        clip.finished_at = datetime.utcnow()
    session.add(clip)
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        existing = await dreamina.find_by_client_ref(session, op.id, req.client_ref)
        if existing is not None:
            return existing, False
        raise
    return clip, True


# ── 端点 ────────────────────────────────────────────────────────────────────
@router.post("/api/video-clips", status_code=202)
async def create_video_clip(req: CreateClipRequest) -> dict:
    """提交单镜（202 异步入队）：ref 幂等 → 登录闸 → 积分闸 → 物化参考图 → 落 queued。

    **幂等查询排在登录闸/积分闸之前**：纯重放零新建零物化零 CLI 零扣分，本该无条件回原
    clip_id。掉登录时 503、余额被排队中的任务扣穿时 409，都会把「任务其实早就在队里」报成
    提交失败——调用方按 5xx/4xx 处置，运营重跑就是双倍提交双倍扣分。

    参考图在**建行之前**同步物化（仿 note-components 的 add_images：坏图当场 4xx），
    不让「图坏了」变成一条要人回头清理的 error 行。

    ref 命中一条**从没跑过 submit CLI 的 error 行**（批量端点物化失败留下的那种）时走
    **复活**：重新物化 + 重置回 queued，保同一个 clip_id。不复活的话，图源一次瞬时故障就把
    这个幂等键永久烧死——修好图用同 ref 再发只会拿回那条死 error 行，那一镜再也生不出来。
    """
    op = current_operator()
    async with get_session() as session:
        # 幂等优先：命中即原样返回，零新建、零 CLI 调用、零扣分
        existing = await dreamina.find_by_client_ref(session, op.id, req.client_ref)
        if existing is not None:
            if dreamina.is_revivable(existing):
                await _revive_clips(session, [(existing, req)])
            return {"clip_id": existing.clip_id, "status": _public_status(existing),
                    "reused": True}
    status = await _require_login()
    _guard_credit(status.get("credit"))
    async with get_session() as session:
        clip_id = dreamina.new_clip_id()
        image_path, img_error = await _materialize(clip_id, req)
        if img_error:
            raise ValueError(img_error)  # → 400（宿主 ValueError 处理器）
        clip, _created = await _insert_clip(
            session, op, req, clip_id=clip_id, image_path=image_path)
        payload = {"clip_id": clip.clip_id}
        warning = _low_credit_warning(
            status.get("credit"), dreamina.estimate_credit(req.model, req.duration))
        if warning:
            payload["warning"] = warning
        return payload


@router.get("/api/video-clips/{clip_id}")
async def get_video_clip(clip_id: str) -> dict:
    """查单条：不存在 → 404；非本人且非 admin → 403。"""
    op = current_operator()
    async with get_session() as session:
        clip = await dreamina.get_by_clip_id(session, clip_id)
        if clip is None:
            raise NotFoundError(f"片段任务 {clip_id} 不存在")
        if not _can_access(clip, op):
            raise AccessDenied("无权访问该片段任务")
        return _clip_payload(clip)


@router.post("/api/video-clip-batches", status_code=202)
async def create_video_clip_batch(req: CreateBatchRequest) -> dict:
    """批量提交（202）：逐镜独立，clip_ids 与 shots **等长同序**。

    逐镜三步：ref 命中 → 复用原任务（保留其原 batch_id，零新建）；物化失败 → **只让该镜落
    error 行**（其余照常入队，不连坐）；正常 → 落 queued 并挂本批 batch_id + batch_index。

    **逐镜 ref 去重排在登录闸/积分闸之前**（验收第 7 条要保护的正是这个场景）：整批纯重放
    零新建零 CLI 零扣分，不该因为「首发已入队的任务把余额扣穿」而 409、或掉登录而 503——
    那会让调用方以为整批没提交，重跑就是 8 镜双倍烧分且排队中无法取消。

    远程参考图**并发物化**：逐张串行下载时 8 镜 × 30s 上限会顶穿调用方 60s 超时，
    调用方拿不到 clip_ids 而任务照建照跑照扣分。
    """
    op = current_operator()
    clip_ids: list[str | None] = [None] * len(req.shots)
    pending: list[tuple[int, CreateClipRequest]] = []
    revivable: list[tuple[VideoClip, CreateClipRequest]] = []
    hit_batches: set[str | None] = set()
    async with get_session() as session:
        for index, shot in enumerate(req.shots):
            existing = await dreamina.find_by_client_ref(session, op.id, shot.client_ref)
            if existing is not None:
                clip_ids[index] = existing.clip_id     # 重放：原 clip_id，零新增任务
                hit_batches.add(existing.batch_id)
                if dreamina.is_revivable(existing):
                    revivable.append((existing, shot))
            else:
                pending.append((index, shot))
        # 复活「从没跑过 submit CLI」的 error 行（图源瞬时故障留下的那种）：重物化 + 回 queued，
        # 保同 clip_id。不复活的话那些镜被自己的幂等键永久烧死，修好图重放也生不出来。
        await _revive_clips(session, revivable)
    if not pending:
        # 整批纯重放（零新建），闸一律不挂。**batch_id 不再现编一个**：那会是一个数据库里
        # 一行都没有的号，调用方拿去 GET batch 必 404，比 null 更难排查。命中镜同属一批时
        # 回那个真批次号，否则 null——定位一律以 clip_ids 为准。
        return {"batch_id": hit_batches.pop() if len(hit_batches) == 1 else None,
                "clip_ids": clip_ids}

    status = await _require_login()
    _guard_credit(status.get("credit"))
    batch_id = dreamina.new_batch_id()
    new_ids = [dreamina.new_clip_id() for _ in pending]
    materialized = await asyncio.gather(
        *(_materialize(cid, shot) for cid, (_i, shot) in zip(new_ids, pending)))
    estimate = 0
    async with get_session() as session:
        for clip_id, (index, shot), (image_path, img_error) in zip(
                new_ids, pending, materialized):
            clip, created = await _insert_clip(
                session, op, shot, clip_id=clip_id, batch_id=batch_id, batch_index=index,
                image_path=image_path, error=img_error,
            )
            clip_ids[index] = clip.clip_id
            if created and not img_error:
                estimate += dreamina.estimate_credit(shot.model, shot.duration) or 0
    payload = {"batch_id": batch_id, "clip_ids": clip_ids}
    warning = _low_credit_warning(status.get("credit"), estimate)
    if warning:
        payload["warning"] = warning
    return payload


@router.get("/api/video-clip-batches/{batch_id}")
async def get_video_clip_batch(batch_id: str) -> dict:
    """查一批的逐镜状态汇总（按 batch_index 序）。

    形态闸挡在查询前：批次号恒为 ``vcb_<10hex>``，别的形态直接 404。纯重放批的 batch_id 是
    **null**（见 POST），把 "null" / "None" 这类字面量当批次号查是调用方的 bug，早点 404
    比返回一个空批更好排查。
    """
    op = current_operator()
    if not _BATCH_ID_RE.fullmatch(batch_id):
        raise NotFoundError(f"批次 {batch_id} 不存在")
    async with get_session() as session:
        clips = (await session.execute(
            select(VideoClip).where(VideoClip.batch_id == batch_id)
            .order_by(VideoClip.batch_index, VideoClip.id)
        )).scalars().all()
        if not clips:
            raise NotFoundError(f"批次 {batch_id} 不存在")
        if any(not _can_access(c, op) for c in clips):
            raise AccessDenied("无权访问该批次")
        items = [_clip_payload(c) for c in clips]
        summary = {
            "total": len(items),
            "done": sum(1 for c in clips if c.status == "done"),
            "error": sum(1 for c in clips if c.status == "error"),
            "in_flight": sum(1 for c in clips if c.status in _IN_FLIGHT_STATES),
        }
        return {"batch_id": batch_id, "clips": items, "summary": summary}


@router.get("/api/video-credits")
async def get_video_credits() -> dict:
    """积分余额 + 低水位标记（60s 缓存，登录失效时 credit 为 null 但仍 200）。"""
    current_operator()
    status = await dreamina.get_credit_status()
    credit = status.get("credit")
    return {
        "credit": credit,
        "low_threshold_hit": (credit is not None
                              and credit < settings.CLIP_CREDIT_LOW_WATERMARK),
        "logged_in": bool(status.get("logged_in")),
    }


@router.get("/api/dreamina-status")
async def get_dreamina_status() -> dict:
    """登录态健康（skill 侧 auto 后端探测入口：200 且 logged_in=true 才走 server）。"""
    current_operator()
    status = await dreamina.get_credit_status()
    async with get_session() as session:
        models = await dreamina.compliance_confirmed_models(session)
    return {
        "logged_in": bool(status.get("logged_in")),
        "credit": status.get("credit"),
        "compliance_confirmed_models": models,
        "error": status.get("error"),
    }


@router.get("/uploads/clips/{token_dir}/{name}")
async def serve_clip_product(token_dir: str, name: str) -> FileResponse:
    """取回片段产物 MP4（白名单免鉴权：HMAC token 目录即访问控制，与 video_rest 同款）。

    ``/uploads`` 前缀在鉴权中间件白名单内，故本路由免 apikey——不可猜的
    ``{clip_id}-{hmac16}`` 目录名（SECRET_KEY 派生）承担访问控制。skill 侧不带
    Authorization 直接拉这个链接落盘成 shot-NN.mp4 进 ffmpeg 合成。
    正则白名单 + resolve/is_relative_to 双保险挡路径穿越；非文件 404。
    """
    if not _TOKEN_DIR_RE.fullmatch(token_dir) or not _NAME_RE.fullmatch(name):
        raise HTTPException(status_code=404, detail="资源不存在")
    root = (Path(settings.DATA_DIR) / "uploads" / "clips").resolve()
    file_path = (root / token_dir / name).resolve()
    if not file_path.is_relative_to(root) or not file_path.is_file():
        raise HTTPException(status_code=404, detail="资源不存在")
    return FileResponse(file_path, media_type="video/mp4")
