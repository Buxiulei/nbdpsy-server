"""笔记合集**创建** REST(202 + 轮询)。

与 ``podcast_rest`` 的播客合集端点是**两套系统**,别混用:

- 本端点建的是**笔记合集** —— 即 ``GET /api/accounts/{id}/collections`` 读的那套 picker
  系统,图文笔记挂载(``POST /note-components`` 的 ``collection_id``)只认它;
- ``POST /api/accounts/{id}/podcast-collections`` 建的是**播客合集**,只在发播客时用。

为什么是异步 browser job:创建是**改变账号状态的写操作**,与 note-components 同类,
天然该走 ``account_locks`` / ``browser_slot`` / ``job_polling`` 那一整套既有基建。
"""

from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel, Field, model_validator

from app.auth.context import current_operator
from app.auth.guards import assert_account_access
from app.core.db import get_session
from app.core.errors import NotFoundError
from app.http.job_polling import QUEUE_MANIFEST_NOTE, base_view, load_job
from app.models.xhs_account import XhsAccount
from app.services import note_collection_create
from app.services.quota import assert_operator_quota

router = APIRouter()

# 2026-08-09 号8 实拍:名称框 0/20 计数、简介框 0/50 计数。
# ⚠️ 简介上限与播客合集(100)**不同**,别照抄那边的常量。
MAX_NAME_LEN = 20
MAX_DESC_LEN = 50

# 封面:笔记合集的创建表单里**根本没有这个字段**(2026-08-09 实拍,与播客合集相反)。
# 收到就 422 而不是静默忽略 —— 静默忽略会让调用方以为封面设上了。
_COVER_REJECT = (
    "笔记合集平台无封面字段(2026-08-09 实拍):创建表单只有「合集名称」与「合集简介」两项。"
    "带封面的是**播客合集**(POST /api/accounts/{id}/podcast-collections),那是另一套系统。"
    "请去掉 cover 参数重发"
)

MANIFEST_ENTRIES = [
    {
        "method": "POST", "path": "/api/accounts/{account_id}/note-collections",
        "summary": "新建一个**笔记合集**(异步,会起浏览器;建完即可用它挂图文笔记)",
        "admin_only": False,
        "params": {
            "account_id": "path,int",
            "name": "body,str(**必填**,合集名称,≤20 字;实拍确认输入框 0/20 计数)",
            "description": "body,str|None(合集简介,**≤50 字**——与播客合集的 100 不同;"
                           "不传就不填)",
            "carrier_note_id": "body,str(**必填**,载体笔记的平台 note_id)。笔记合集的创建"
                               "入口**只在笔记编辑器的「加入合集」弹层底栏**(笔记管理页上"
                               "没有合集 tab,实拍确认),所以必须借一篇笔记打开编辑器。"
                               "平台的按钮是「创建并加入」——**这篇载体笔记会被加进新合集**,"
                               "所以请传一篇本来就该挂进该合集的笔记。"
                               "⚠️ 载体笔记**不能是已在某个合集里的笔记**:已选态下「加入合集」"
                               "入口本身不渲染,会直接报 collection_entry_not_found",
            "cover": "body,**不接受** —— 传了就 422。笔记合集没有封面字段(实拍)",
        },
        "returns": '{job_id, status:"queued"}',
        "errors": "403=无该号授权;404=账号不存在;"
                  "422=name 为空或 >20 字 / description >50 字 / carrier_note_id 缺失 / "
                  "**传了 cover**(以上都在建 job 前,一步都没做);"
                  "429=运营者未完成任务配额已满",
        "notes": "异步契约:拿 job_id 后每 5-10s 轮询 GET /api/note-collections/{job_id} "
                 "到 done/error/unknown;常态 1-3 分钟(要起一个真 camoufox 会话,且判定"
                 "会重进一次更新页做干净列表回读)。⚠️ 五条必读:"
                 "① **建前自动查重**:该号已有同名合集时**不创建**,直接 error "
                 "`collection_name_already_exists` 并把现有那条的 collection_id / note_num "
                 "回给你 —— 平台**不去重同名**,重建只会多出一个空合集要人工删。"
                 "看到这个 error 直接用回执里的 id 挂笔记即可;"
                 "② **非幂等,失败不自动重跑**,理由同①。看到 error/unknown 先用 "
                 "GET /api/accounts/{id}/collections 核对再决定;"
                 "③ **载体笔记零提交**:全程不点发布,创建完直接离开。载体笔记会不会真被"
                 "「并加入」,以回执的 joined_carrier / carrier_collection_label 为准;"
                 "④ 与播客合集是两套系统:本端点产出的 collection_id 才是 "
                 "POST /note-components 能用的那个;"
                 "⑤ 同号浏览器操作共享 per-account 锁串行,别对同号并发发起。",
    },
    {
        "method": "GET", "path": "/api/note-collections/{job_id}",
        "queue": QUEUE_MANIFEST_NOTE,
        "summary": "轮询笔记合集创建结果",
        "admin_only": False, "params": {"job_id": "path,str"},
        "returns": "{status, reason?, name?, description?, collection_id?, confirmed_by?, "
                   "name_preexisted?, note_num?, joined_carrier?, carrier_collection_label?, "
                   "modal_closed?, created_api_capture?, created_api_id?, collections_seen?, "
                   "observed?, create_submit_state?, modal_html?, note?}",
        "errors": "403=无该号授权;404=job_id 不存在",
        "notes": "status 五态:queued / running / done / error(附 reason)/ "
                 "**unknown(执行进程中断,建没建成未知)**。"
                 "done 的 `confirmed_by` 说明**凭什么判成功**,只有两种,都是**双信号**:"
                 "`modal_closed_and_in_fresh_list`=创建表单收起了 **且** "
                 "**重进更新页之后**的干净合集列表里出现了这个名字(重进会丢弃一切未提交的"
                 "编辑器状态,所以这条同时证明合集是真落库了);"
                 "`modal_closed_and_carrier_chip`=表单收起 且 重进后载体笔记已处于该合集的"
                 "已选态(此时「加入合集」入口不渲染、列表读不到,collection_id 只能取自"
                 "创建 API 取证,**可能为 null**——要拿 id 请对另一篇不在任何合集里的笔记调 "
                 "GET /api/accounts/{id}/collections)。"
                 "**页面文本里出现合集名一律不构成证据**(播客合集 7 单假绿的直接教训:"
                 "表单里的实时预览把自己刚打的字回显了)。"
                 "`joined_carrier`=重进后该合集在列表里的 note_num,用它判「创建并加入」"
                 "到底随不随笔记提交生效;`name_preexisted` 在 **done 上恒 false**"
                 "(同名在创建之前就被查重挡掉了,压根走不到创建),只有 "
                 "`collection_name_already_exists` 那条 error 上它是 true。"
                 "error 的 reason 前缀可自判:`editor_not_ready`=载体笔记的更新页进不去"
                 "(note_id 不属于本号,或 creator 域要重新扫码登录;**一步都没做**);"
                 "`collection_name_already_exists`=已有同名"
                 "(**什么都没建**,回执带现有 id);`collection_entry_not_found`=载体笔记"
                 "编辑页上没有「加入合集」入口(多半是这篇已在别的合集里,换一篇);"
                 "`collection_catalog_unavailable`=弹层开了但没收到合集列表响应(查不了重,"
                 "整单中止);`collection_create_entry_not_found`=弹层底栏没有「创建合集」;"
                 "`collection_create_modal_not_shown`=点了但创建表单没出来;"
                 "`collection_name_input_not_found`=表单里认不出名称输入框(**一个字都没填**,"
                 "绝不退到表单之外找输入框);`create_join_never_enabled`=「创建并加入」始终"
                 "禁用/loading(不点禁用按钮);`create_modal_still_open`=点了但表单没收起"
                 "(**大概率没提交出去**);`collection_absent_from_fresh_list`=表单收起了但"
                 "干净列表里没有它(**建没建成未知**);`verify_reload_failed` / "
                 "`verify_list_unreadable`=回读环节失败,同样是未知。"
                 "后四条**都别自动重建**,人工核对合集列表再说。"
                 "`created_api_capture` 是点「创建并加入」之后新增的 **POST** 响应取证"
                 "(URL / status / body 前 800 字;已排除弹层列表接口 list_v2)——创建 API 的"
                 "形态尚未取证,靠它钉死;`modal_html` 是认不出输入框时的表单 HTML 落点。"
                 "**这两个都是临时诊断字段**:首验把创建 API 与表单结构钉死之后即撤,"
                 "勿建硬依赖。",
    },
]


class NoteCollectionCreateRequest(BaseModel):
    """笔记合集创建请求体。校验全在这里(纯入参形状,不需要 DB/账号上下文)。"""

    name: str = Field(min_length=1, max_length=MAX_NAME_LEN,
                      description="合集名称(平台必填,≤20 字)")
    description: str | None = Field(default=None, max_length=MAX_DESC_LEN,
                                    description="合集简介(≤50 字,不传就不填)")
    carrier_note_id: str = Field(min_length=1, description="载体笔记 note_id(打开它的编辑器)")
    # 显式声明才拒得掉:pydantic 默认**静默忽略**多余字段,不写这一行的话调用方传了 cover
    # 会一路无声无息地被丢掉,然后以为封面设上了。
    cover: Any = Field(default=None, description="不接受;笔记合集没有封面字段")

    @model_validator(mode="after")
    def _check_fields(self) -> "NoteCollectionCreateRequest":
        """名称/载体非空白 + 封面显式拒收 —— 违反一律 422,不建 job。

        在入口全查掉的理由与播客合集同款:这个任务要起一个真 camoufox 会话跑一两分钟,
        白烧一次会话还在账号上留一次浏览器活动(会话频次是有风控红线的资源)。
        """
        if not self.name.strip():
            raise ValueError("name 不能是纯空白:合集名称是平台必填项")
        if not self.carrier_note_id.strip():
            raise ValueError(
                "carrier_note_id 不能是纯空白:笔记合集的创建入口只在笔记编辑器的"
                "「加入合集」弹层里,必须借一篇笔记打开编辑器"
            )
        if self.cover is not None:
            raise ValueError(_COVER_REJECT)
        return self


_RESULT_KEYS = (
    "name", "description", "collection_id", "confirmed_by", "name_preexisted",
    "note_num", "joined_carrier", "carrier_collection_label", "modal_closed",
    "created_api_capture", "created_api_id", "collections_seen", "observed",
    "create_submit_state", "modal_html", "note",
)


@router.post("/api/accounts/{account_id}/note-collections", status_code=202)
async def create_note_collection_endpoint(
    account_id: int, payload: NoteCollectionCreateRequest
) -> dict:
    """异步触发一次笔记合集创建,立即返回 job_id。"""
    operator = current_operator()
    # 运营配额闸:未完成任务达上限 → 429(admin 豁免),不建任务。
    await assert_operator_quota(operator)
    async with get_session() as session:
        await assert_account_access(operator, account_id, session)
        if await session.get(XhsAccount, account_id) is None:
            raise NotFoundError(f"账号 {account_id} 不存在")
    job_id = note_collection_create.start_create(
        account_id,
        payload.name.strip(),
        (payload.description or "").strip() or None,
        payload.carrier_note_id.strip(),
    )
    return {"job_id": job_id, "status": "queued"}


@router.get("/api/note-collections/{job_id}")
async def get_note_collection_endpoint(job_id: str) -> dict:
    """轮询笔记合集创建结果:queued / running / done / error / unknown。

    **error 也下发逐项详情**(与 note-components 轮询同款教训):失败时的当场取证比
    成功时更值钱,藏起来会让调用方只能靠换外部变量盲测。
    """
    row = await load_job(job_id, note_collection_create.KIND, "job_id")
    view = await base_view(row)
    result = row.get("result") or {}
    if row["status"] in ("done", "error"):
        for key in _RESULT_KEYS:
            if key in result:
                view[key] = result[key]
    return view
