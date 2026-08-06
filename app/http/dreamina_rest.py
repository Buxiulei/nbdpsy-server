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
import json
import math
import re
from datetime import datetime
from pathlib import Path
from typing import Literal, get_args

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
# 抽出来的段帧 PNG（services.dreamina.frame_name 的两种形态）。与 MP4 分开一条正则而不是
# 并进 _NAME_RE：媒体类型要按它区分，混一条就得再解析一次文件名才知道回什么 Content-Type。
_FRAME_NAME_RE = re.compile(r"^frame_(last|[0-9]{1,6}\.[0-9]{3})\.png$")
# 批次号形态（GET batch 的入参闸，与 services.dreamina.new_batch_id 同形）
_BATCH_ID_RE = re.compile(r"^vcb_[0-9a-f]{10}$")

_OPERATIONS = Literal["text2video", "image2video", "multimodal2video", "frames2video",
                      "multiframe2video"]
_MODELS = Literal["seedance2.0", "seedance2.0fast", "seedance2.0_vip", "seedance2.0fast_vip",
                  "seedance2.0mini", "seedance2.5"]
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


def _price_note() -> str:
    """价格文案**由 ``_PRICE_PER_5S`` 现算**，不手写。

    手写那版在 seedance2.5 回填实测价之后整整落后了一版（还挂着「2.5 未实测故不估」），
    而 2.5 是双端默认档——运营照这段文案做预算、并据此提了缺口。文档与表同源就没有第二次。
    """
    priced = "、".join(f"{m}={dreamina.price_per_5s(m)}" for m in dreamina.priced_models())
    unpriced = "、".join(m for m in get_args(_MODELS) if dreamina.price_per_5s(m) is None)
    return (
        f"**积分估算 = 该档 5s/720p 实测单价 × 时长（按 5s 档向上取整）线性折算**。"
        f"有实测价的档：{priced}（seedance2.5=130 由三次生产实测互证：5s=130、10s=260×2）。"
        f"**未实测故不估的档：{unpriced}**——用这些档提交时 estimated_credits 为 null、"
        "也不会带低积分 warning，那是「估不出」不是「余额充足」。"
        "**multiframe2video 恒不估**（模型由平台下发，本表不适用）。"
        "**frames2video 的价格从未实测**，估算沿用同 model 的 5s 档粗估，可能有偏差。"
    )


_MODEL_NOTE = (
    "模型全集 seedance2.0 | seedance2.0fast | seedance2.0fast_vip | seedance2.0_vip | "
    "seedance2.0mini | seedance2.5，**默认 seedance2.5**（2.5 没有 fast / vip 变体）。"
    "**seedance2.5 是 VIP-only**，时长 4-30s；其余模型 4-15s，超了 422。"
    "**2.5 首次使用可能需先到即梦网页端完成一次生成**做账号级合规授权，否则回 "
    "AigcComplianceConfirmationRequired——那是要人去点的一次性动作，服务端重试无意义。"
    + _price_note()
)

_ESTIMATE_NOTE = (
    "**estimated_credits 是估算不是账单**：按上面的线性折算给出，**实际扣分一律以 "
    "success 后回填的 `credit_count` 为准**，两者可能有偏差（排队期换档、平台调价、"
    "frames2video 这类没实测过的形态都会让估算偏）。口径是**本次请求新增的预估消耗**："
    "纯重放命中（零新建零扣分）与物化失败的 error 行都计 0；"
    "重放命中一条被**复活**回 queued 的 error 行会真去提交，故按实价计入。"
    "估不出的镜为 null。"
)

_REF_NOTE = (
    "参考图三种给法：单张 `image`、多张 `images`（multimodal2video 专用）、首尾两帧 "
    "`first_image`+`last_image`（frames2video 专用）。**image 与 images 只能给一个**，"
    "同给 422（本层不静默丢字段）。"
    "**多图张数按模型分档**：seedance2.5 最多 30 张，seedance2.0 家族 / mini 最多 9 张，"
    "超限当场 422 不白跑（网上流传的「Seedance 最多 9 张」是 2.0 的数字，别套到 2.5 上）。"
    "多图用途是锁跨镜一致性——一镜同时给「本镜场景图 + 全片人物定妆图 + 关键道具图」。"
    "**frames2video 是分镜级运动控制**（精确指定镜头从哪一帧开始、到哪一帧结束，中间过渡"
    "交给模型）：两帧必须都给，缺一帧 422；**不接受 ratio**——画幅由首帧图推断，CLI 不收"
    "该参数，传了 422 免得调用方以为设置生效；时长仍按模型分档（2.5 到 30s、其余 15s）。"
    "本服务只透传参考**图**，CLI 的 --video / --audio 输入面尚未开放。"
)

_MULTIFRAME_NOTE = (
    "**multiframe2video = 多图连贯故事**（一次出一条多镜片子，镜间衔接由模型统一处理，"
    "比 25-30 次单镜提交 + 本地拼接少很多风格漂移）。用 `images` 给 **2-20 张**，"
    "N 张图 = **N-1 段转场**。两种形态不能混："
    "①**长式**（任意 2-20 张）：`transition_prompts` 恰好 N-1 段，每段描述一帧如何演进到下一帧；"
    "`transition_durations` 可选、给了也必须是 N-1 段（整个省略则 CLI 每段默认 3s）；"
    "此形态下 **prompt / duration 传了就 422**（CLI 那两个参数是「恰好 2 张」专用简写，"
    "我们收下也送不出去）。"
    "②**简写**（恰好 2 张）：`prompt` + `duration` 两者必填，duration **2-8s**"
    "（每段 1-8s 且总时长 ≥2s，2 张只有一段，那一段就是总时长）。"
    "每段 1-8s、总时长 ≥2s，越界当场 422。"
    "**`model` 传了就 422**（判据是请求体里出现过这个键，传成默认档一样 422）："
    "这条 operation 的模型由平台固定、CLI 不接受 --model_version，服务端也无从得知是哪档；"
    "**GET 回显的 model 是占位符 `platform_fixed`**，那不是档位名，别拿它判档或查价。"
    "**不接受 ratio**（画幅由第一张图推断）。"
    "**单价从未实测**：提交必带一条「估不出」的 warning，低积分估算对它不可用"
    "（服务端不瞎编价格），真实消耗等 success 后看 credit_count；"
    "余额连最便宜一镜都不够时**照旧 409 硬拦**。"
    "**入参回显看 GET 的 `transitions`**（逐段原话，[{prompt, duration}]）——"
    "库里那条行的 prompt / duration 是服务端为台账合成的派生值（各段连起来 / 各段之和），"
    "本来就不在对外视图里，别去别处找你的原话。"
)

MANIFEST_ENTRIES = [
    {
        "method": "POST", "path": "/api/video-clips",
        "summary": "提交一条即梦视频生成任务（异步入队，202 返回 clip_id）",
        "admin_only": False,
        "params": {
            "operation": "body,str(text2video|image2video|multimodal2video|frames2video|"
                         "multiframe2video)",
            "prompt": "body,str(1-2000 字；**multiframe2video 长式不接受**，见 notes)",
            "duration": "body,int(4-30 整数秒；**仅 seedance2.5 到 30s**，其余模型 4-15；"
                        "**multiframe2video 简写是 2-8s、长式不接受**)",
            "model": "body,str(seedance2.0|seedance2.0fast|seedance2.0_vip|seedance2.0fast_vip|"
                     "seedance2.0mini|seedance2.5，默认 seedance2.5；"
                     "**multiframe2video 传了就 422**，那条 operation 的模型由平台固定)",
            "ratio": "body,str|None(1:1|3:4|4:3|9:16|16:9|21:9；**image2video / frames2video "
                     "传了就 422**，其画幅由输入图 / 首帧图推断)",
            "image": "body,str|None(图床直链 或 本服务 /uploads 路径；image2video 必填，"
                     "multimodal2video 与 images 二选一，text2video/frames2video 传了就 422。"
                     "不收 base64 大包)",
            "images": "body,list[str]|None(多张图，顺序即传给 CLI 的顺序；multimodal2video 下是"
                      "参考图，seedance2.5 最多 30 张、其余模型最多 9 张；multiframe2video 下是"
                      "故事帧，**2-20 张**；超限 422)",
            "first_image": "body,str|None(首帧；**仅 frames2video**，与 last_image 必须成对)",
            "last_image": "body,str|None(尾帧；**仅 frames2video**，与 first_image 必须成对)",
            "transition_prompts": "body,list[str]|None(逐段转场提示词，**仅 multiframe2video**；"
                                  "N 张图恰好 N-1 段，段数不符 422)",
            "transition_durations": "body,list[float]|None(逐段转场秒数，**仅 multiframe2video**；"
                                    "同样 N-1 段，每段 1-8s、总时长 ≥2s；整个省略则 CLI 每段默认 3s)",
            "client_ref": "body,str|None(1-64，幂等键)",
        },
        "returns": "{clip_id, status(重放命中时带当前状态), estimated_credits(本次新增预估消耗，"
                   "估不出为 null、纯重放为 0), warning?(低积分提示，不拦截)}",
        "errors": "422=参数校验失败（含 image2video/frames2video/multiframe2video+ratio、"
                  "text2video+参考图、image 与 images 同给、多图超模型张数上限、"
                  "frames2video 缺首帧或尾帧、multiframe2video 段数不符 / 传了 model / "
                  "长式传了 prompt 或 duration、duration 越界，**含「非 2.5 模型传了 >15s」**）；"
                  "400=参考图下载失败或不是图片（**多张里坏一张即整镜失败**）；"
                  "409=积分不足以再提交任何一镜；"
                  "503=即梦登录态失效（**不会静默排队**）；401=apikey 无效",
        "notes": _MODEL_NOTE + " " + _ESTIMATE_NOTE + " " + _REF_NOTE + " " + _MULTIFRAME_NOTE
                 + " " + _POLL_NOTE
                 + " client_ref 幂等：同一运营用同 ref 重发返回**原 clip_id**、"
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
        "returns": "{clip_id, status, operation, model, transitions, submit_id, credit_count, "
                   "video_url, error, last_poll_error, queued_seconds, expires_at, expired, "
                   "batch_id, client_ref, created_at}",
        "errors": "403=非本人任务且非 admin；404=clip 不存在",
        "notes": "**transitions** 是 multiframe2video 的逐段**原话**回显"
                 "（[{prompt, duration}]，duration 为 null 表示该段用 CLI 的 3s 默认），"
                 "其余 operation 恒为 null——多帧故事的入参只有这一份能原样取回，"
                 "事后查「我第 3 段写的什么」看它。**model 对 multiframe2video 是占位符 "
                 "`platform_fixed`**（那条 operation 的模型由平台固定、CLI 不给查），"
                 "**别拿它判档、也别拿它查价**。"
                 "status 五态 queued|submitted|querying|done|error。**error 与 last_poll_error "
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
        "params": {
            "shots": f"body,list[同单镜入参]（1-{settings.CLIP_MAX_BATCH} 镜）",
            "max_credits": "body,int|None(**预算护栏**：整批预估超过它就整批 409 拒绝，"
                           "一镜都不建；不传 = 不设预算线，行为与从前逐字节一致)",
        },
        "returns": "{batch_id(**纯重放批可能为 null**，见 notes), "
                   "clip_ids[](与 shots **等长同序**，可直接按下标映射 shot-NN), "
                   "estimated_credits(整批新增预估合计；**批内有估不出的镜时为 null**), "
                   "estimated_credits_per_shot[](与 shots 等长同序，逐镜；不新提交的镜为 0、"
                   "估不出的为 null), warning?}",
        "errors": "422=结构校验失败或镜数越界；409=积分不足以再提交任何一镜，"
                  "**或整批预估超 max_credits**，**或批内含无法估价的镜致护栏无法执行**；"
                  "503=即梦登录态失效；401=apikey 无效",
        "notes": _MODEL_NOTE + " " + _ESTIMATE_NOTE
                 + " **max_credits 预算护栏**：给了就按估算表算整批预估，超了**整批 409**，"
                 "此时一镜未创建、零参考图物化、零 CLI 调用、零扣分（闸排在一切副作用之前，"
                 "与「余额不足以提交任何一镜」那道 409 同族）。"
                 "**批内只要有一镜估不出价（如 multiframe2video、未实测档），一律 409 而不是"
                 "按能估的部分放行**——护栏的承诺是「绝不超支」，含未知项时兑现不了这个承诺，"
                 "响应文案会指出是第几镜、哪个 operation/model；处置是把不可估的镜拆出来单独"
                 "提交，或本批不带 max_credits。**不传 max_credits 时不做任何预算判定**。"
                 + " " + _REF_NOTE + " " + _MULTIFRAME_NOTE
                 + " **shots[] 与单镜入参逐字段同构**，多图 images / 首尾帧 first_image+"
                 "last_image / 多帧故事 transition_prompts 在批量端点一样可用"
                 "（电影化的 25-30 镜只能走这里）。"
                 " **逐镜按 client_ref 去重**：整批重放（同 shots 同 refs）返回原 clip_ids、"
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
        "method": "GET", "path": "/api/video-clips/{clip_id}/frame",
        "summary": "抽一帧成 PNG（分段续接：上一段的尾帧当下一段的首帧参考）",
        "admin_only": False,
        "params": {"clip_id": "path,str(vc_ 开头，须 status=done 且产物未过期)",
                   "t": "query,str(**默认 last**=末帧；也可给秒数如 t=3 / t=2.5)"},
        "returns": "{clip_id, t, frame_url(免鉴权 PNG 直链), expires_at}",
        "errors": "409=片段尚未完成（没有可抽帧的视频）；410=产物已过 TTL 被清理；"
                  "422=t 形态非法或超出视频时长；403=非本人任务且非 admin；404=clip 不存在；"
                  "500=ffmpeg 抽帧失败（服务端故障，文案带 ffmpeg 原文）",
        "notes": "**回的是直链不是图片流**：这个 `/uploads/...` 路径可以直接当下一镜的 "
                 "`image` / `first_image` 传回来（本服务的参考图物化认自家 /uploads 路径），"
                 "省掉「拉 mp4 → 本地抽帧 → 再上传」一个来回。"
                 "帧 PNG 落在 clip 自己的工作目录里，**与 clip 同 TTL**（产物过期时一起清）。"
                 "**幂等**：同一个 t 重复请求复用已抽好的那张，不会重跑 ffmpeg。"
                 "t 越界当场 422（**绝不回半张图 / 空图**），错误文案带视频真实时长。",
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
                 "不在其中不代表未授权（CLI 无查询授权状态的接口）。"
                 "**不含 multiframe2video 的行**：那条 operation 的 model 是占位符，"
                 "混进来就不是近似而是污染。",
    },
]


# ── 请求模型 ────────────────────────────────────────────────────────────────
class CreateClipRequest(BaseModel):
    """单镜入参。校验矩阵直接对应 CLI 的真实能力，宁可 422 也不静默丢字段。"""

    operation: _OPERATIONS
    # prompt / duration 的类型界为了 multiframe2video 的**长式**才放宽成可选（那里逐段提示词
    # 走 transition_prompts、时长走 transition_durations，CLI 的 --prompt/--duration 是
    # 「恰好 2 张」专用简写）。放宽的只是类型界：其余四种 operation 仍然必填、下限仍是
    # DURATION_MIN，由 _check_required_fields 钉死。
    prompt: str | None = Field(default=None, min_length=1, max_length=2000)
    duration: int | None = Field(default=None, ge=1, le=dreamina.DURATION_MAX)
    ratio: _RATIOS | None = None
    model: _MODELS = dreamina.DEFAULT_MODEL
    # 单张参考图（老字段，语义不变）。多图参考用 images，**两者只能给一个**（见 _check_media_matrix）。
    image: str | None = None
    # 多张参考图（multimodal2video 专用，顺序即传给 CLI 的 --image 顺序）。字段界取全家族
    # 最宽的 30，逐模型收紧在 _check_ref_image_ceiling 里（与 duration 同款两段式）。
    images: list[str] | None = Field(default=None, min_length=1,
                                     max_length=dreamina.REF_IMAGE_MAX)
    # 首尾帧（frames2video 专用，两者必须成对出现）
    first_image: str | None = None
    last_image: str | None = None
    # 逐段转场（multiframe2video 专用）：N 张图恰好 N-1 段，两个列表各自与段序对齐。
    # transition_durations 可整体省略（CLI 按每段 3s 默认）；给了就必须给满 N-1 个。
    transition_prompts: list[str] | None = Field(
        default=None, min_length=1, max_length=dreamina.MULTIFRAME_IMAGE_MAX - 1)
    transition_durations: list[float] | None = Field(
        default=None, min_length=1, max_length=dreamina.MULTIFRAME_IMAGE_MAX - 1)
    client_ref: str | None = Field(default=None, min_length=1, max_length=64)

    @model_validator(mode="after")
    def _check_required_fields(self) -> "CreateClipRequest":
        """``prompt`` / ``duration`` 的必填与下限——**除 multiframe2video 外逐字保持老规矩**。

        这两个字段的类型界放宽只是为了让 multiframe 的长式能合法地不带它们（见字段注释）。
        规矩没放宽：别的 operation 少传一个照旧 422，否则 ``text2video`` 缺 duration 会一路
        放行到 worker 才被 CLI 拒，留下一条要人回头清理的 error 行。
        """
        if self.operation == "multiframe2video":
            return self               # 这条 operation 的必填规则全在 _check_multiframe
        if not self.prompt:
            raise ValueError("prompt 必填（1-2000 字）")
        if self.duration is None:
            raise ValueError(
                f"duration 必填（{dreamina.DURATION_MIN}-{dreamina.DURATION_MAX} 整数秒）")
        if self.duration < dreamina.DURATION_MIN:
            raise ValueError(
                f"duration={self.duration}s 低于下限：全家族最短 {dreamina.DURATION_MIN}s")
        return self

    @model_validator(mode="after")
    def _check_duration_ceiling(self) -> "CreateClipRequest":
        """时长上限**按模型**收紧（字段界只能取全家族最宽的 30s，否则 seedance2.5 过不去）。

        不在这里收紧的话，``seedance2.0fast + duration=20`` 会一路放行到 worker 才被 CLI 拒——
        那时任务行已建好、参考图已物化，结局是一条要人回头清理的 error 行，而不是当场 422。
        """
        if self.duration is None:
            return self               # multiframe 长式没有整片时长，逐段闸在 _check_multiframe
        ceiling = dreamina.max_duration(self.model)
        if self.duration > ceiling:
            raise ValueError(
                f"duration={self.duration}s 超出 {self.model} 的上限："
                f"该模型上限 {ceiling}s，仅 seedance2.5 支持到 {dreamina.DURATION_MAX}s"
            )
        return self

    @model_validator(mode="after")
    def _check_ref_image_ceiling(self) -> "CreateClipRequest":
        """参考图张数上限**按模型**收紧（字段界只能取全家族最宽的 30，否则 seedance2.5 过不去）。

        理由与时长闸逐字相同：不在这里拦，``seedance2.0fast + 10 张`` 会先把 10 张图下载
        物化、把行建好，再由 CLI 拒掉——留下一条要人回头清理的 error 行外加十次白下载。
        """
        if self.operation == "multiframe2video":
            return self               # 它的 2-20 张不看模型档，见 _check_multiframe
        ceiling = dreamina.max_ref_images(self.model)
        if self.images and len(self.images) > ceiling:
            raise ValueError(
                f"参考图 {len(self.images)} 张超出 {self.model} 的上限：该模型最多 "
                f"{ceiling} 张，仅 seedance2.5 支持到 {dreamina.REF_IMAGE_MAX} 张"
            )
        return self

    @model_validator(mode="after")
    def _check_media_matrix(self) -> "CreateClipRequest":
        """operation × (image / images / first_image / last_image / ratio) 的合法组合闸。

        - ``image`` 与 ``images`` **只能给一个**。静默取其一是本层最不该做的事：调用方无从
          发现另一半没生效，而这里每一次提交都在花钱（需求第三节「别静默吞」）。
        - ``image2video``：必须有 image（CLI 的 ``--image`` 在这个子命令下是单张）；
          **有 ratio 就 422**——画幅由输入图推断，CLI 不收该参数。
        - ``text2video``：不收任何参考图。语义清晰优于静默忽略。
        - ``multimodal2video``：image 或 images 至少给一个（CLI 的 ``--image`` 是 stringArray）。
        - ``frames2video``：首尾两帧都必须给（缺一帧 CLI 就退化成别的形态，不是我们要的
          镜头调度）；**不收 ratio**——CLI help 原文 "ratio is inferred from the first frame
          image size"，传了会被它严格校验拒收，我们提前 422 免得调用方以为设置生效。
        """
        if self.image and self.images:
            raise ValueError(
                "image 与 images 只能给一个（单张参考图继续用 image，多图参考用 images）；"
                "同时给时无法判定以哪个为准，本层宁可 422 也不静默丢字段"
            )
        if self.operation != "frames2video" and (self.first_image or self.last_image):
            raise ValueError("first_image / last_image 只属于 frames2video")
        if self.operation == "image2video":
            # images 的判定排在「必须有 image」之前：只给 images 的调用方是在用错 operation，
            # 回「必须提供 image」等于让他去补一个本就不该在这里出现的字段。
            if self.images:
                raise ValueError("image2video 只收单张 image，多图参考请用 multimodal2video")
            if not self.image:
                raise ValueError("image2video 必须提供 image（图床直链或 /uploads 路径）")
            if self.ratio:
                raise ValueError("image2video 的画幅由输入图推断，不接受 ratio（CLI 不收该参数）")
        elif self.operation == "text2video":
            if self.image or self.images:
                raise ValueError("text2video 不接受参考图（生成不参考图片）")
        elif self.operation == "frames2video":
            if not (self.first_image and self.last_image):
                raise ValueError("frames2video 必须同时提供 first_image 与 last_image")
            if self.image or self.images:
                raise ValueError("frames2video 只收首尾两帧，不接受 image / images")
            if self.ratio:
                raise ValueError(
                    "frames2video 的画幅由**首帧图**推断，不接受 ratio（CLI 不收该参数）")
        elif self.operation == "multiframe2video":
            if self.image:
                raise ValueError("multiframe2video 的多张图走 images，不接受单张 image")
            if self.ratio:
                raise ValueError(
                    "multiframe2video 的画幅由**第一张图**推断，不接受 ratio（CLI 不收该参数）")
        elif not (self.image or self.images):
            raise ValueError("multimodal2video 必须提供 image 或 images")
        return self

    @model_validator(mode="after")
    def _check_multiframe(self) -> "CreateClipRequest":
        """multiframe2video（多图连贯故事）的全套闸，逐条对应 CLI help 原文。

        两种合法形态，**不能混**：

        - **长式**（任意 2-20 张）：逐段 ``transition_prompts``，恰好 N-1 段；可选
          ``transition_durations`` 同样 N-1 段（省略则 CLI 每段默认 3s）。此时 ``prompt`` /
          ``duration`` 传了就 422——CLI 的那两个 flag 是「恰好 2 张」专用简写，收下再丢弃
          就是静默吞字段。
        - **简写**（恰好 2 张）：``prompt`` + ``duration``，两者都必填。duration 的有效区间是
          **2-8s 而不是 1-8s**：CLI 要求每段 1-8s **且总时长 ≥2s**，2 张只有一段，那一段就是
          总时长。

        ``model`` 传了非默认值一律 422："model_version is fixed and is not configurable on
        this command"。静默忽略会让调用方以为自己选上了档（与 frames2video 拒绝 ratio 同一条
        纪律）——但注意这一闸挡不住「显式传了默认档」，字段有默认值时两者在服务端无法区分。
        """
        if self.operation != "multiframe2video":
            if self.transition_prompts or self.transition_durations:
                raise ValueError(
                    "transition_prompts / transition_durations 只属于 multiframe2video")
            return self

        count = len(self.images or [])
        if not (dreamina.MULTIFRAME_IMAGE_MIN <= count <= dreamina.MULTIFRAME_IMAGE_MAX):
            raise ValueError(
                f"multiframe2video 需要 {dreamina.MULTIFRAME_IMAGE_MIN}-"
                f"{dreamina.MULTIFRAME_IMAGE_MAX} 张 images（收到 {count} 张）")
        if "model" in self.model_fields_set:
            # 判据是「请求体里真的出现过 model 这个键」而不是「值等于默认档」：后者分不清
            # 「显式传了 seedance2.5」和「根本没传」，会放过一半的误解。用 model_fields_set
            # 就不必为这条 operation 去动另三种的字段默认值契约。
            raise ValueError(
                "multiframe2video 的模型由平台固定、**不可选**，传 model 无效"
                "（CLI 在这条子命令上不收 --model_version）；请去掉该字段")
        if self.transition_prompts:
            return self._check_multiframe_segments(count)
        if self.transition_durations:
            raise ValueError(
                "只给 transition_durations 不给 transition_prompts 不是合法形态："
                "逐段时长要跟逐段提示词一一对应")
        if count != 2:
            raise ValueError(
                f"{count} 张图必须逐段给 transition_prompts（{count - 1} 段）；"
                "只有**恰好 2 张**才能走 prompt + duration 简写")
        if not self.prompt:
            raise ValueError("multiframe2video 恰好 2 张时必须提供 prompt（简写形态）")
        if self.duration is None:
            raise ValueError("multiframe2video 恰好 2 张时必须提供 duration（简写形态）")
        if not (dreamina.MULTIFRAME_TOTAL_MIN <= self.duration
                <= dreamina.MULTIFRAME_SEGMENT_MAX):
            raise ValueError(
                f"multiframe2video 简写的 duration 需在 {dreamina.MULTIFRAME_TOTAL_MIN:.0f}-"
                f"{dreamina.MULTIFRAME_SEGMENT_MAX:.0f}s：每段 1-8s 且总时长 ≥2s，"
                "2 张只有一段，那一段就是总时长")
        return self

    def _check_multiframe_segments(self, count: int) -> "CreateClipRequest":
        """长式的逐段闸：段数对齐 N-1、每段 1-8s、总时长 ≥2s、不收简写字段。"""
        need = count - 1
        if len(self.transition_prompts) != need:
            raise ValueError(
                f"{count} 张图需要 {need} 段转场，收到 {len(self.transition_prompts)} 段"
                "（CLI 口径：for N images, the transition count is N-1）")
        if any(not p.strip() for p in self.transition_prompts):
            raise ValueError("transition_prompts 里不能有空段（每段都要描述一次画面演进）")
        if self.prompt or self.duration is not None:
            raise ValueError(
                "逐段转场形态下不接受 prompt / duration：CLI 的那两个参数是「恰好 2 张」"
                "专用简写，这里收下也送不出去；整片描述请逐段写进 transition_prompts")
        durations = self.transition_durations
        if durations is None:
            total = dreamina.MULTIFRAME_DEFAULT_SEGMENT * need   # CLI 每段默认 3s
        else:
            if len(durations) != need:
                raise ValueError(
                    f"{count} 张图需要 {need} 段 transition_durations，收到 {len(durations)} 段"
                    "（要么给满，要么整个省略让 CLI 按每段 3s 默认走）")
            for seconds in durations:
                if not (dreamina.MULTIFRAME_SEGMENT_MIN <= seconds
                        <= dreamina.MULTIFRAME_SEGMENT_MAX):
                    raise ValueError(
                        f"转场段时长 {seconds}s 越界：每段必须 "
                        f"{dreamina.MULTIFRAME_SEGMENT_MIN:.0f}-"
                        f"{dreamina.MULTIFRAME_SEGMENT_MAX:.0f}s")
            total = sum(durations)
        if total < dreamina.MULTIFRAME_TOTAL_MIN:
            raise ValueError(
                f"转场总时长 {total}s 不足：CLI 要求总时长 ≥ "
                f"{dreamina.MULTIFRAME_TOTAL_MIN:.0f}s")
        return self


class CreateBatchRequest(BaseModel):
    """批量入参：整批结构校验（结构错 = 调用方 bug，整批 422 可接受）。"""

    shots: list[CreateClipRequest]
    # 预算护栏（可选）。**不传就完全不做预算判定**——这条端点在电影化产线上一次就是 20-50 镜，
    # 调用方侧任何一个循环 bug 都能把积分烧穿，而原有的闸只在「余额连一镜都不够」时才响，
    # 那是破产线不是预算线。给了它就按估算表算整批预估，超了整批拒（见 _guard_max_credits）。
    max_credits: int | None = Field(default=None, ge=1)

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
    """对外单条视图（字段名与 skill 侧 K_* 常量逐字对齐）。

    ``transitions`` 回的是 multiframe 长式的**逐段原话**（``[{prompt, duration}]``），
    不能只给 ``prompt`` 那个连起来的派生串：调用方拿派生串还原不出自己传了什么，
    事后查「我第 3 段写的什么」就没了出处。其余 operation 为 null。
    """
    segments = dreamina.transitions(clip)
    return {
        "clip_id": clip.clip_id,
        "status": _public_status(clip),
        "operation": clip.operation,
        "model": clip.model,
        "transitions": segments or None,
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


def _shot_estimate(req: CreateClipRequest) -> int | None:
    """一镜的预估消耗（None = 该档没实测价，估不出）。

    按**落库口径**取 model / duration（``_stored_*``）而不是请求原文：multiframe 长式的整片
    时长是各段之和、model 是占位符，拿请求里那个字段默认值去查价会算出一个凭空的数。
    """
    return dreamina.estimate_credit(_stored_model(req), _stored_duration(req), req.operation)


def _incremental_estimate(clip: VideoClip, *, will_submit: bool) -> int | None:
    """这一镜在**本次请求**里新增的预估消耗。

    ``will_submit=False`` 恒 0：纯重放命中的镜早就在队里（这次零新建零扣分），物化失败的
    error 行则根本不会被提交。把它们算进预估等于把已花的钱或不会花的钱再报一遍。
    """
    if not will_submit:
        return 0
    return dreamina.estimate_credit(clip.model, clip.duration, clip.operation)


def _total_estimate(per_shot: list[int | None]) -> int | None:
    """整批合计；**只要有一镜估不出就回 None**——一个漏算了几镜的数字当总账比没有更危险。"""
    if any(v is None for v in per_shot):
        return None
    return sum(per_shot)


def _guard_max_credits(max_credits: int | None,
                       chargeable: list[tuple[int, CreateClipRequest]]) -> None:
    """预算护栏：整批预估超上限 → 整批 409，**一镜不建、零物化、零 CLI 调用、零扣分**。

    调用点必须排在一切副作用之前（建行、参考图物化、error 行复活都算），与 ``_guard_credit``
    同族——护栏拒绝之后留下半批任务，比没有护栏更糟。

    **批里只要有一镜估不出价就一律 409**，不按「能估的那部分」放行：护栏对运营的承诺是
    「绝不超支」，含未知项时这个承诺兑现不了，悄悄放行等于卖一个假保证。处置写进文案
    （拆分提交 / 去掉 max_credits），不让人对着 409 猜。
    """
    if max_credits is None:
        return                       # 不传 = 不设预算线，判定一步都不做
    total = 0
    for index, shot in chargeable:
        estimate = _shot_estimate(shot)
        if estimate is None:
            raise HTTPException(
                409,
                f"批内第 {index + 1} 镜（operation={shot.operation}、"
                f"model={_stored_model(shot)}）无法估价：该档单价从未实测，"
                f"服务端不瞎编价格，因而无法保证整批不超 max_credits={max_credits}。"
                "整批已拒绝，一镜未创建、未提交、未扣分。"
                "处置：把无法估价的镜拆分提交（单独发一批），或本批不带 max_credits。",
            )
        total += estimate
    if total > max_credits:
        raise HTTPException(
            409,
            f"本批预估消耗 {total} 积分，超过预算上限 max_credits={max_credits}："
            "整批已拒绝，一镜未创建、未提交、未扣分。"
            "预估按各档 5s 实测单价×时长线性折算，**实际扣分以 success 后的 credit_count "
            "为准**；请下调镜数/时长、或确认后提高 max_credits 重发。",
        )


def _low_credit_warning(credit: int | None, estimate: int | None) -> str | None:
    """低积分提示（不拦截：扣费 success 才结算，排队中还有变数）。"""
    if credit is None or not estimate or credit >= estimate:
        return None
    return (f"积分余额 {credit} 低于本次粗估消耗 {estimate}（按 5s 档价×时长粗估），"
            "已照常入队；扣费在 success 时才结算，余额不足的镜可能失败")


# multiframe2video 没有实测单价，估算给不出数。如实说「估不出」而不是沉默：沉默与
# 「估过了，余额够」在调用方看来一模一样，而这条 operation 恰恰是最贵的那类长片。
_UNPRICED_NOTE = (
    "本次含 multiframe2video：该模式单价**从未实测**，消耗估算不可用（服务端不瞎编价格），"
    "请自行留意余额；扣费在 success 时才结算，真实消耗由 credit_count 回填"
)


def _merge_warning(*parts: str | None) -> str | None:
    """把若干条提示并成一条 warning（都为空则不带这个键）。"""
    kept = [p for p in parts if p]
    return " ".join(kept) if kept else None


def _ref_specs(req: CreateClipRequest) -> list[tuple[str, str]]:
    """本镜要物化的 ``(来源, 副本名主干)`` 列表，**顺序即落库顺序**。

    - ``frames2video`` 恒为 ``[首, 尾]``：``build_submit_args`` 靠这个顺序取 --first/--last，
      换了序就是让镜头倒着走、ratio 还会按错的那张图推断；
    - 多图参考按 ``images`` 传入序，主干 ``ref`` / ``ref_2`` / ``ref_3``…（同名会互相覆盖）；
    - 单 ``image`` 落 ``ref``，与本次改动之前一字不差。
    """
    if req.operation == "frames2video":
        return [(req.first_image, "first"), (req.last_image, "last")]
    sources = req.images or ([req.image] if req.image else [])
    return [(s, "ref" if i == 0 else f"ref_{i + 1}") for i, s in enumerate(sources)]


def _segments_json(req: CreateClipRequest) -> str | None:
    """multiframe 长式的逐段转场序列化成 ``transitions_json``；简写与其余 operation 为 None。

    ``ensure_ascii=False`` 让中文提示词在库里可读（运营要直接看这一列排查）。
    """
    if req.operation != "multiframe2video" or not req.transition_prompts:
        return None
    durations = req.transition_durations
    segments = [{"prompt": p, "duration": durations[i] if durations else None}
                for i, p in enumerate(req.transition_prompts)]
    return json.dumps(segments, ensure_ascii=False)


def _stored_model(req: CreateClipRequest) -> str:
    """落 ``model`` 列的值。multiframe 存占位符——**绝不把一个我们不知道真假的档位名写进库**。

    那条 operation 的模型由平台固定、CLI 不接受 --model_version，请求里那个 seedance2.5
    只是字段默认值。存它等于记一条我们明知不成立的事实，而且骗得毫无痕迹（见
    ``dreamina.MULTIFRAME_MODEL_PLACEHOLDER``）。占位符不会进提交参数：multiframe 的参数
    组装根本不带 --model_version。
    """
    if req.operation == "multiframe2video":
        return dreamina.MULTIFRAME_MODEL_PLACEHOLDER
    return req.model


def _stored_prompt(req: CreateClipRequest) -> str:
    """落 ``prompt`` 列的值。multiframe 长式没有整片提示词，用逐段的连成台账串。

    列是 NOT NULL 且要给运营看，空着或塞占位符都不如把真实内容连起来。**这是派生值**，
    真正提交给 CLI 的是 ``transitions_json`` 里那几段。
    """
    if req.prompt:
        return req.prompt
    return " → ".join(req.transition_prompts or [])


def _stored_duration(req: CreateClipRequest) -> int:
    """落 ``duration`` 列的值。multiframe 长式没有整片时长，用各段之和（同样是派生值）。

    省略 ``transition_durations`` 时按 CLI 的每段 3s 默认折算——只影响这一列的可读性，
    提交参数里不会出现我们编的时长（见 services.dreamina._multiframe_args）。
    """
    if req.duration is not None:
        return req.duration
    segments = req.transition_durations
    need = len(req.transition_prompts or [])
    total = sum(segments) if segments else dreamina.MULTIFRAME_DEFAULT_SEGMENT * need
    return round(total)


def _first_ref_source(req: CreateClipRequest) -> str | None:
    """落 ``image_source`` 的那一条来源原文（多来源时取第一条：多图的首张 / 首尾帧的首帧）。

    第 2..N 张的来源原文不单独落库——``image_source`` 只做追溯提示，权威的是
    ``image_paths_json`` 里那几份本地副本（它们与任务同 TTL，图床过期也还在）。
    """
    specs = _ref_specs(req)
    return specs[0][0] if specs else None


async def _materialize(clip_id: str, req: CreateClipRequest) -> tuple[list[str], str | None]:
    """物化本镜的全部参考图 → (本地路径列表, 错误说明)。无参考图时 ``([], None)``。

    **一张失败即整镜失败**：少一张参考图生成出来的镜是废片，却照样占队列位、照样扣积分。
    镜内也并发（多图参考可达 30 张，逐张串行 30s 上限会顶穿调用方超时）。
    """
    specs = _ref_specs(req)
    if not specs:
        return [], None
    workdir = dreamina.clip_dir(clip_id)
    try:
        paths = await asyncio.gather(*(
            dreamina.materialize_ref_image(src, workdir, stem=stem) for src, stem in specs))
    except ValueError as exc:
        return [], str(exc)
    return [str(p) for p in paths], None


def _apply_revive(clip: VideoClip, req: CreateClipRequest,
                  image_paths: list[str], img_error: str | None) -> None:
    """把复活的物化结果落到行上（**同一行同一 clip_id**，不新建）。

    物化再失败就维持 error、只更新文案——图源还是坏的，重置回 queued 只会让调度器提交一条
    没有参考图的任务。行仍然是可复活的（没碰过 CLI），下次再重放还有机会。
    """
    if img_error:
        clip.error = img_error
        clip.finished_at = datetime.utcnow()
        return
    clip.image_source = _first_ref_source(req)
    clip.image_path = image_paths[0] if image_paths else None
    clip.image_paths_json = json.dumps(image_paths) if image_paths else None
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
    for (clip, req), (image_paths, img_error) in zip(items, results):
        _apply_revive(clip, req, image_paths, img_error)
    await session.commit()


async def _insert_clip(session, op: Operator, req: CreateClipRequest, *, clip_id: str,
                       batch_id: str | None = None, batch_index: int | None = None,
                       image_paths: list[str] | None = None,
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
        # prompt / duration 对 multiframe 长式是派生值（见 _stored_prompt / _stored_duration）
        prompt=_stored_prompt(req),
        model=_stored_model(req),
        duration=_stored_duration(req),
        ratio=req.ratio,
        image_source=_first_ref_source(req),
        # 两列都写：image_paths_json 是权威全集，image_path 存第一张（老读者与老行的回落口）
        image_path=image_paths[0] if image_paths else None,
        image_paths_json=json.dumps(image_paths) if image_paths else None,
        transitions_json=_segments_json(req),
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
            revived = dreamina.is_revivable(existing)
            if revived:
                await _revive_clips(session, [(existing, req)])
            # 复活成功（回到 queued）的镜会真去提交、真扣分，故按实价报；其余重放新增消耗为 0。
            return {"clip_id": existing.clip_id, "status": _public_status(existing),
                    "reused": True,
                    "estimated_credits": _incremental_estimate(
                        existing, will_submit=revived and existing.status == "queued")}
    status = await _require_login()
    _guard_credit(status.get("credit"))
    async with get_session() as session:
        clip_id = dreamina.new_clip_id()
        image_paths, img_error = await _materialize(clip_id, req)
        if img_error:
            raise ValueError(img_error)  # → 400（宿主 ValueError 处理器）
        clip, created = await _insert_clip(
            session, op, req, clip_id=clip_id, image_paths=image_paths)
        # 估价按**落库后的行**取（multiframe 长式的 duration 是派生值，请求里根本没有）。
        # created=False 是并发同 ref 撞唯一约束后拿到的别人那条行——那笔钱记在先到者头上，本次 0。
        estimate = _incremental_estimate(clip, will_submit=created)
        payload = {"clip_id": clip.clip_id, "estimated_credits": estimate}
        warning = _merge_warning(
            _low_credit_warning(status.get("credit"), estimate),
            _UNPRICED_NOTE if clip.operation == "multiframe2video" else None)
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


def _parse_frame_t(t: str) -> str | float:
    """``t`` 入参 → ``"last"`` 或非负秒数；形态不合法当场 422。

    只认这两种形态：分段续接要的就是「上一段的末帧」或「某个确定时刻」，别的写法（负数、
    时间码 00:00:03、空串）与其猜一个语义，不如让调用方当场知道自己写错了。
    """
    raw = (t or "").strip()
    if raw.lower() == dreamina.FRAME_LAST:
        return dreamina.FRAME_LAST
    try:
        seconds = float(raw)
    except ValueError:
        raise HTTPException(422, f"t={t!r} 形态不合法：只接受 last 或非负秒数（如 t=3 / t=2.5）")
    if not math.isfinite(seconds) or seconds < 0:
        raise HTTPException(422, f"t={t!r} 不是有效的秒数（须 ≥ 0 的有限值）")
    return seconds


@router.get("/api/video-clips/{clip_id}/frame")
async def get_video_clip_frame(clip_id: str, t: str = dreamina.FRAME_LAST) -> dict:
    """抽一帧成 PNG 直链（分段续接：上一段的尾帧当下一段的首帧参考）。

    回**直链而不是图片流**：这个 ``/uploads/...`` 路径能原样当下一镜的 ``image`` /
    ``first_image`` 传回来（参考图物化认自家 /uploads 路径），省掉「拉 mp4 → 本地抽帧 →
    再上传」一个来回，正是这条端点存在的理由。

    每一种「拿不到帧」都给自己的码，**绝不回半张图**：没跑完 409、产物被 TTL 清了 410、
    t 越界 422。帧落在 clip 工作目录里，跟着 clip 一起过期。
    """
    op = current_operator()
    target = _parse_frame_t(t)
    async with get_session() as session:
        clip = await dreamina.get_by_clip_id(session, clip_id)
        if clip is None:
            raise NotFoundError(f"片段任务 {clip_id} 不存在")
        if not _can_access(clip, op):
            raise AccessDenied("无权访问该片段任务")
        status, video_path, expires_at = clip.status, clip.video_path, clip.expires_at
        public_status = _public_status(clip)
    if status != "done":
        raise HTTPException(
            409, f"片段 {clip_id} 尚未完成（status={public_status}），没有可抽帧的视频；"
                 "轮询到 status=done 再来取")
    video = Path(video_path) if video_path else None
    if video is None or not video.is_file():
        raise HTTPException(
            410, f"片段 {clip_id} 的产物已过 TTL 被清理（{settings.CLIP_TTL_DAYS} 天），"
                 "无法抽帧；任务本身仍是 done，credit_count 保留供对账")
    if target != dreamina.FRAME_LAST:
        duration = await dreamina.probe_duration(video)
        if duration is not None and target >= duration:
            raise HTTPException(
                422, f"t={target} 超出视频时长（{duration:.3f}s）；要末帧请用 t=last")
    out = dreamina.clip_dir(clip_id) / dreamina.frame_name(target)
    error = await dreamina.extract_frame(video, out, target)
    if error:
        raise HTTPException(500, f"抽帧失败：{error}")
    return {
        "clip_id": clip_id,
        "t": target,
        "frame_url": dreamina.clip_public_url(clip_id, out.name),
        "expires_at": expires_at.isoformat() if expires_at else None,
    }


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
    # 逐镜预估，**默认 0 = 本次不会新提交**（纯重放命中、物化失败的 error 行都不花钱）
    per_shot: list[int | None] = [0] * len(req.shots)
    pending: list[tuple[int, CreateClipRequest]] = []
    revivable: list[tuple[int, VideoClip, CreateClipRequest]] = []
    hit_batches: set[str | None] = set()
    async with get_session() as session:
        for index, shot in enumerate(req.shots):
            existing = await dreamina.find_by_client_ref(session, op.id, shot.client_ref)
            if existing is not None:
                clip_ids[index] = existing.clip_id     # 重放：原 clip_id，零新增任务
                hit_batches.add(existing.batch_id)
                if dreamina.is_revivable(existing):
                    revivable.append((index, existing, shot))
            else:
                pending.append((index, shot))
        # 预算护栏挡在**一切副作用之前**：复活会重新物化参考图并把行改回 queued（=真会去提交、
        # 真会扣分），所以它和新建镜一样计入预估，而这道闸必须排在 _revive_clips 前面。
        _guard_max_credits(
            req.max_credits,
            sorted(pending + [(i, s) for i, _c, s in revivable], key=lambda p: p[0]))
        # 复活「从没跑过 submit CLI」的 error 行（图源瞬时故障留下的那种）：重物化 + 回 queued，
        # 保同 clip_id。不复活的话那些镜被自己的幂等键永久烧死，修好图重放也生不出来。
        await _revive_clips(session, [(clip, shot) for _i, clip, shot in revivable])
        for index, clip, _shot in revivable:
            # 复活失败的仍是 error 行（图源还是坏的），不会提交 → 留 0。
            per_shot[index] = _incremental_estimate(clip, will_submit=clip.status == "queued")
    if not pending:
        # 整批纯重放（零新建），闸一律不挂。**batch_id 不再现编一个**：那会是一个数据库里
        # 一行都没有的号，调用方拿去 GET batch 必 404，比 null 更难排查。命中镜同属一批时
        # 回那个真批次号，否则 null——定位一律以 clip_ids 为准。
        return {"batch_id": hit_batches.pop() if len(hit_batches) == 1 else None,
                "clip_ids": clip_ids,
                "estimated_credits": _total_estimate(per_shot),
                "estimated_credits_per_shot": per_shot}

    status = await _require_login()
    _guard_credit(status.get("credit"))
    batch_id = dreamina.new_batch_id()
    new_ids = [dreamina.new_clip_id() for _ in pending]
    materialized = await asyncio.gather(
        *(_materialize(cid, shot) for cid, (_i, shot) in zip(new_ids, pending)))
    unpriced = False
    async with get_session() as session:
        for clip_id, (index, shot), (image_paths, img_error) in zip(
                new_ids, pending, materialized):
            clip, created = await _insert_clip(
                session, op, shot, clip_id=clip_id, batch_id=batch_id, batch_index=index,
                image_paths=image_paths, error=img_error,
            )
            clip_ids[index] = clip.clip_id
            per_shot[index] = _incremental_estimate(
                clip, will_submit=created and not img_error)
            if created and not img_error:
                unpriced = unpriced or clip.operation == "multiframe2video"
    payload = {"batch_id": batch_id, "clip_ids": clip_ids,
               "estimated_credits": _total_estimate(per_shot),
               "estimated_credits_per_shot": per_shot}
    # 批里只要有一镜估不出价，整批的估算就是不完整的——必须说，别让人拿它当总账。
    # warning 用「能估出来的那部分之和」比余额（合计为 null 时它仍是有用的下界）。
    estimate = sum(v for v in per_shot if v)
    warning = _merge_warning(_low_credit_warning(status.get("credit"), estimate),
                             _UNPRICED_NOTE if unpriced else None)
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
    """取回片段产物：成片 MP4 或抽出来的段帧 PNG（白名单免鉴权：HMAC token 目录即访问控制）。

    ``/uploads`` 前缀在鉴权中间件白名单内，故本路由免 apikey——不可猜的
    ``{clip_id}-{hmac16}`` 目录名（SECRET_KEY 派生）承担访问控制。skill 侧不带
    Authorization 直接拉这个链接落盘成 shot-NN.mp4 进 ffmpeg 合成。
    正则白名单 + resolve/is_relative_to 双保险挡路径穿越；非文件 404。
    """
    if not _TOKEN_DIR_RE.fullmatch(token_dir):
        raise HTTPException(status_code=404, detail="资源不存在")
    if _NAME_RE.fullmatch(name):
        media_type = "video/mp4"
    elif _FRAME_NAME_RE.fullmatch(name):
        media_type = "image/png"
    else:
        raise HTTPException(status_code=404, detail="资源不存在")
    root = (Path(settings.DATA_DIR) / "uploads" / "clips").resolve()
    file_path = (root / token_dir / name).resolve()
    if not file_path.is_relative_to(root) or not file_path.is_file():
        raise HTTPException(status_code=404, detail="资源不存在")
    return FileResponse(file_path, media_type=media_type)
