"""podcast 分组 REST:播客合集创建(202 + 轮询)。

为什么是**异步 browser job** 而不是同步阻塞端点(对照 ``note_components_rest`` 的两个
只读 GET):合集创建是**改变账号状态的写操作**,与 ``op_images`` / ``note_components``
同类,天然该走 ``account_locks`` / ``browser_slot`` / ``job_polling`` 那一整套既有基建;
只读列表才适合同步阻塞。

播客发布本身不在这里,它是 ``POST /api/publish-jobs`` 的 ``audio`` 字段
(与 images/video 三选一)—— 本模块只收播客**专属**的能力。
"""

from pathlib import Path

from fastapi import APIRouter
from pydantic import BaseModel, Field, model_validator

from app.auth.context import current_operator
from app.auth.guards import assert_account_access
from app.core.db import get_session
from app.core.errors import NotFoundError
from app.http.job_polling import base_view, load_job
from app.models.xhs_account import XhsAccount
from app.publish.policy import podcast_collection_cover_reject
from app.services import podcast_collection
from app.services.quota import assert_operator_quota

router = APIRouter()

# 实拍确认:名称 input 的 maxlength="20"、简介 textarea 的 maxlength="100"
MAX_NAME_LEN = 20
MAX_DESC_LEN = 100

MANIFEST_ENTRIES = [
    {
        "method": "POST", "path": "/api/accounts/{account_id}/podcast-collections",
        "summary": "新建一个播客合集(异步,会起浏览器;发播客时用名称加入)",
        "admin_only": False,
        "params": {
            "account_id": "path,int",
            "name": "body,str(**必填**,合集名称,≤20 字;发布播客时 podcast_collection "
                     "传的就是这个名字)",
            "description": "body,str|None(合集简介,≤100 字;不传就不填)",
            "cover": "body,str(**必填**,合集封面的**服务器侧图片路径**;支持 "
                      ".jpg/.jpeg/.png/.webp,**≤5MB**,平台推荐 1:1。"
                      "POST /api/uploads/images 传完即落在 DATA_DIR/uploads/{batch_id}/NN.ext,"
                      "把那个路径填进来)",
        },
        "returns": '{job_id, status:"queued"}',
        "errors": "403=无该号授权;404=账号不存在;"
                  "422=name 为空或 >20 字 / description >100 字 / cover 缺失、格式不支持、"
                  "文件不存在或 >5MB(**以上都在建 job 前**,一步都没做);"
                  "429=运营者未完成任务配额已满",
        "notes": "异步契约:拿 job_id 后每 5-10s 轮询 GET /api/podcast-collections/{job_id} "
                 "到 done/error/unknown;常态 1-2 分钟(要起一个真 camoufox 会话)。"
                 "⚠️ 四条必读:"
                 "① **非幂等,失败不自动重跑** —— 重跑会建出第二个同名合集(平台会不会"
                 "去重未验证)。看到 error/unknown 先去发播客页核对再决定;"
                 "② **封面是两步流程**:选文件后平台会弹「封面裁剪」二次确认,服务端会"
                 "自动点「确定」—— 不点这一步封面根本没提交,「创建」按钮会一直禁用"
                 "(这是真号取证里最贵的一个发现);"
                 "③ **collection_id 可能为 null**:平台侧 id 能否回读未取证,抓不到就给 "
                 "null,**不影响创建成功**。发布时本来也是按 name 选合集,不需要 id;"
                 "④ 同号浏览器操作共享 per-account 锁串行,别对同号并发发起。",
    },
    {
        "method": "GET", "path": "/api/podcast-collections/{job_id}",
        "summary": "轮询播客合集创建结果",
        "admin_only": False, "params": {"job_id": "path,str"},
        "returns": "{status, reason?, name?, collection_id?, confirmed_by?, "
                   "cover_crop?, tooltip?, observed?}",
        "errors": "403=无该号授权;404=job_id 不存在",
        "notes": "status 五态:queued / running / done / error(附 reason)/ "
                 "**unknown(执行进程中断,建没建成未知)**。"
                 "done 时 `confirmed_by` 说明**凭什么判成功**:`create_page_closed`=创建页"
                 "收起了;`name_in_list`=合集列表里出现了这个名字。"
                 "⚠️ 成功判据本身未经真号验证(两轮取证都没能真的点下「创建」),所以 "
                 "**done 之后建议顺手到发播客页看一眼**;判不出来时服务端宁可报 "
                 "`create_result_unconfirmed` 也不谎报成功。"
                 "error 的 reason 前缀可自判:`podcast_tab_not_active`=切不到发播客 tab;"
                 "`collection_entry_not_found`=页面上没有「新建播客合集」入口;"
                 "`collection_create_page_not_loaded`=点了新建但创建页没出来;"
                 "`collection_cover_input_not_found` / `collection_cover_set_input_failed`="
                 "封面那一步;`create_button_never_enabled`=三项填完「创建」仍禁用"
                 "(多半是封面裁剪没确认完,或平台又加了必填项);"
                 "`create_result_unconfirmed`=点了创建但回读不到结果,**做没做成未知**。"
                 "`observed` 是当场取证(各输入框在不在、按钮 class、页面文本片段),"
                 "报障时请连它一起带上。",
    },
]


class PodcastCollectionRequest(BaseModel):
    """播客合集创建请求体。三项校验全在这里(纯入参形状,不需要 DB/账号上下文)。"""

    name: str = Field(min_length=1, max_length=MAX_NAME_LEN,
                      description="合集名称(平台必填,≤20 字)")
    description: str | None = Field(default=None, max_length=MAX_DESC_LEN,
                                    description="合集简介(≤100 字,不传就不填)")
    cover: str = Field(min_length=1, description="合集封面的服务器侧图片路径(平台必填)")

    @model_validator(mode="after")
    def _check_fields(self) -> "PodcastCollectionRequest":
        """名称非空白 + 封面准入(格式/体积/存在性)—— 违反一律 422,不建 job。

        为什么在入口全查掉:这个任务要起一个真 camoufox 会话跑一两分钟,而"封面是
        张 gif"这种事在浏览器层要等到把名字都填完了才在裁剪弹窗里炸 —— 白烧一次会话,
        还在账号上留了一次浏览器活动(会话频次是有风控红线的资源)。
        """
        if not self.name.strip():
            raise ValueError("name 不能是纯空白:合集名称是平台必填项")
        reason = podcast_collection_cover_reject(self.cover.strip())
        if reason:
            raise ValueError(reason)
        return self


@router.post("/api/accounts/{account_id}/podcast-collections", status_code=202)
async def create_podcast_collection_endpoint(
    account_id: int, payload: PodcastCollectionRequest
) -> dict:
    """异步触发一次播客合集创建,立即返回 job_id。"""
    operator = current_operator()
    # 运营配额闸:未完成任务达上限 → 429(admin 豁免),不建任务。
    await assert_operator_quota(operator)
    async with get_session() as session:
        await assert_account_access(operator, account_id, session)
        if await session.get(XhsAccount, account_id) is None:
            raise NotFoundError(f"账号 {account_id} 不存在")
    job_id = podcast_collection.start_create(
        account_id,
        payload.name.strip(),
        (payload.description or "").strip() or None,
        str(Path(payload.cover.strip())),
    )
    return {"job_id": job_id, "status": "queued"}


@router.get("/api/podcast-collections/{job_id}")
async def get_podcast_collection_endpoint(job_id: str) -> dict:
    """轮询播客合集创建结果:queued / running / done / error / unknown。

    **error 也下发逐项详情**(与 note-components 轮询同款教训):失败时的当场取证比
    成功时更值钱,藏起来会让调用方只能靠换外部变量盲测。
    """
    row = await load_job(job_id, podcast_collection.KIND, "job_id")
    view = base_view(row)
    result = row.get("result") or {}
    if row["status"] in ("done", "error"):
        for key in ("name", "collection_id", "confirmed_by", "cover_crop",
                    "tooltip", "observed"):
            if key in result:
                view[key] = result[key]
    return view
