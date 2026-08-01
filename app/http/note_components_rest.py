"""note-components 分组 REST(4 端点):合集列表 / 活动列表(只读)+ 三组件设置(202)+ 轮询。

设计 docs/design/2026-08-01-note-components-design.md 第 3.5 节。三组件 = 加入合集 /
引用笔记 / 关联活动,发布新笔记(``POST /api/publish-jobs`` 同名字段)与编辑已发布笔记
(本组端点)两条路都能设。

⚠️ 三条要让调用方一眼看见的事:

- **这条产品线的失败是静默的**:``success:true`` 照返但设置被服务端丢弃(私密笔记的合集
  绑定)、按钮点了没反应也没报错(活动首次点击)。故本组端点**逐项回读校验**,轮询结果
  里 ``applied`` 是逐个组件的真实生效情况,``done`` 只在**全部**回读确认后才给,
  部分生效一律 ``partially_applied``。
- **非幂等**:一次设置就是一次全量覆盖提交,且关联活动会往正文**追加**话题;失败**不会**
  自动重跑,调用方也别盲目重试。
- 两个 GET **要起一个真浏览器会话**(这两个列表只有笔记编辑器页会去要,没有独立只读页),
  常态 40-90 秒才返回 —— 调用方的 HTTP 超时请设到 ≥180 秒。
"""

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field, model_validator

from app.auth.context import current_operator
from app.auth.guards import assert_account_access
from app.browser.note_components import DEFAULT_ACTIVITY_KEYWORDS, filter_activities
from app.core.db import get_session
from app.core.errors import NotFoundError
from app.http.job_polling import base_view, load_job
from app.models.xhs_account import XhsAccount
from app.services import note_components
from app.services.quota import assert_operator_quota

router = APIRouter()

# 不过滤的显式写法(留空也等价);默认关键词表见 DEFAULT_ACTIVITY_KEYWORDS
_NO_FILTER = "*"

MANIFEST_ENTRIES = [
    {
        "method": "GET", "path": "/api/accounts/{account_id}/collections",
        "summary": "读该号的合集列表(会起浏览器,慢)",
        "admin_only": False,
        "params": {
            "account_id": "path,int",
            "note_id": "query,str|None(打开哪篇笔记的编辑器去读;省略则自动取该号台账里"
                       "最近一篇有 note_id 的笔记)",
        },
        "returns": "{collections:[{id,name,desc,note_num}], note_id(实际打开的那篇)}",
        "errors": "400=该号台账里没有任何带 note_id 的笔记(给不出落脚点,请显式传 note_id);"
                  "403=无该号授权;404=账号不存在;429=运营者未完成任务配额已满;"
                  "502=浏览器会话失败 / 账号无可用 cookie / 编辑页进不去(detail 给原因)",
        "notes": "⚠️ **同步阻塞端点,常态 40-90 秒**:合集列表只有笔记编辑器页会去要,没有"
                 "独立的只读页面,故要起一个真 camoufox 会话打开一篇笔记的编辑页被动读接口。"
                 "调用方 HTTP 超时请设 ≥180 秒。全程只读:打开编辑器不提交就不落库,对那篇"
                 "笔记无副作用。同号浏览器操作共享 per-account 锁串行,别对同号并发发起。"
                 "collections[].id 就是 POST 三组件时的 collection_id。",
    },
    {
        "method": "GET", "path": "/api/accounts/{account_id}/activities",
        "summary": "读平台活动列表并按心理学关键词筛选(会起浏览器,慢)",
        "admin_only": False,
        "params": {
            "account_id": "path,int",
            "note_id": "query,str|None(同 collections)",
            "keywords": "query,str|None(逗号分隔;省略=用默认心理学词表;传 * 或空=不过滤)",
        },
        "returns": "{activities:[{id,name,desc,matched_keywords}], total(筛选前总数), "
                   "keywords(实际生效的词表), note_id}",
        "errors": "同 GET /collections",
        "notes": "⚠️ 同步阻塞端点,常态 40-90 秒(理由同 collections)。"
                 f"默认词表 {'/'.join(DEFAULT_ACTIVITY_KEYWORDS)};"
                 "**按活动名 + 活动简介联合匹配**,并刻意不收「自我」「成长」这类过宽的词"
                 "(实测「howto穿出自我」因『自我』命中,其实是穿搭活动)。"
                 "活动频繁上下线,**系统里没有任何死名单**,每次都是现拉现筛;"
                 "matched_keywords 告诉你这条凭什么命中,不认可就自己传 keywords 重筛。"
                 "activities[].id 就是 POST 三组件时的 activity_id。",
    },
    {
        "method": "POST", "path": "/api/accounts/{account_id}/note-components",
        "summary": "异步给一篇已发布笔记设置三组件(会真提交一次发布,慎用)",
        "admin_only": False,
        "params": {
            "account_id": "path,int",
            "note_id": "body,str(要编辑的笔记平台 id,**必填**,深链定位)",
            "collection_id": "body,str|None(加入哪个合集,取自 GET /collections)",
            "quoted_note_id": "body,str|None(引用哪篇笔记,只能引用**本号自己**的笔记)",
            "activity_id": "body,str|None(关联哪个活动,取自 GET /activities)",
        },
        "returns": '{job_id, status:"queued"}',
        "errors": "403=无该号授权;404=账号不存在;"
                  "422=note_id 为空,或三个组件 id 一个都没给(那样这次编辑什么都不会改);"
                  "429=运营者未完成任务配额已满",
        "notes": "异步契约:起后台浏览器深链进笔记更新页 → 设置组件 → 点发布 → **重进页面"
                 "逐项回读**(约 2-4 分钟);拿 job_id 后每 5-10s 轮询 "
                 "GET /api/note-components/{job_id}。"
                 "⚠️ 四条硬约束:"
                 "① **非幂等,失败不自动重跑**——提交是**全量覆盖**语义的一次真发布,"
                 "重跑就是再覆盖一次;看到 error/unknown 必须先去笔记里核对当前状态再决定;"
                 "② **关联活动会改写正文**——平台会把该活动的话题**追加**到正文末尾"
                 "(注意话题名 ≠ 活动名,只能事后读),结果里 topics_injected / body_appended "
                 "如实给出;活动是互斥单选,换活动会取消旧的但**旧话题不回收**,所以别反复换;"
                 "③ **权限保全**——编辑前后各读一次可见性档位,提交前不符立刻中止(一次发布"
                 "都不点),提交后若发现被改会自动改回并在结果里告警(permission_* 字段);"
                 "④ **部分生效是常态**——私密笔记的合集绑定会被平台静默丢弃(照返成功),"
                 "所以 done 只在全部回读确认后才给,别拿 202 或 'no error' 当成功凭据。"
                 "同号浏览器操作共享 per-account 锁串行。",
    },
    {
        "method": "GET", "path": "/api/note-components/{job_id}",
        "summary": "轮询三组件设置结果(逐项生效情况)",
        "admin_only": False, "params": {"job_id": "path,str"},
        "returns": "{status, result_status?, applied?:{collection/quote/activity: true|false|null}, "
                   "failed?:[{component,reason}], submitted?, permission_before?, "
                   "permission_after?, permission_preserved?, permission_restored?, "
                   "body_appended?, topics_injected?, reason?}",
        "errors": "403=无该号授权;404=job_id 不存在",
        "notes": "status 五态:queued / running / done / error(附 reason)/ "
                 "**unknown(执行进程中断,改没改成未知)**。"
                 "done 时看 **result_status**:`done`=请求的组件**全部**回读确认生效;"
                 "`partially_applied`=只有一部分生效(哪项没成看 failed)。"
                 "applied 的每个值是三态:true=回读确认生效;false=回读确认**没**生效;"
                 "null=没能回读(未确认)——**只有 true 才算数**,这条产品线的失败是静默的。"
                 "一项都没生效时整体落 error(reason 里带 note_components_all_failed)。"
                 "submitted=有没有观察到那次提交请求的响应;"
                 "permission_preserved=false 表示可见性档位被这次提交改动了(严重),"
                 "permission_restored 给自动改回的结果,ok=false 时**必须人工处理**;"
                 "topics_injected 是关联活动往正文追加的话题名(可能与活动名不同)。",
    },
]


def _parse_keywords(raw: str | None) -> tuple:
    """query 里的 keywords → 词表元组:省略=默认心理学词表;``*``/空=不过滤(空元组)。"""
    if raw is None:
        return tuple(DEFAULT_ACTIVITY_KEYWORDS)
    text = raw.strip()
    if not text or text == _NO_FILTER:
        return ()
    parts = [p.strip() for p in text.replace("，", ",").split(",")]
    return tuple(p for p in parts if p)


async def _prepare_catalog(account_id: int, note_id: str | None) -> str:
    """两个 GET 的公共前置:鉴权 + 配额闸 + 确定要打开哪篇笔记;返回最终 note_id。

    过配额闸的理由与其它浏览器端点一致:这两个 GET 也要起一个真 camoufox 会话,
    配额闸护的正是这条浏览器流水线。
    """
    operator = current_operator()
    await assert_operator_quota(operator)
    async with get_session() as session:
        await assert_account_access(operator, account_id, session)
        if await session.get(XhsAccount, account_id) is None:
            raise NotFoundError(f"账号 {account_id} 不存在")
    resolved = (note_id or "").strip()
    if resolved:
        return resolved
    fallback = await note_components.default_catalog_note_id(account_id)
    if not fallback:
        raise ValueError(
            f"账号 {account_id} 的台账里没有任何带 note_id 的笔记,给不出打开编辑器的落脚点;"
            f"请显式传 note_id(合集/活动列表只有笔记编辑器页会返回)"
        )
    return fallback


def _unwrap(catalog: dict) -> dict:
    """目录读取结果:带 ``error`` 键即浏览器侧失败 → 502(不静默返回空列表)。"""
    if "error" in catalog:
        raise HTTPException(status_code=502, detail=catalog["error"])
    return catalog


@router.get("/api/accounts/{account_id}/collections")
async def list_collections_endpoint(
    account_id: int, note_id: str | None = Query(default=None)
) -> dict:
    """读该号的合集列表(同步阻塞,内部起一次只读浏览器会话)。"""
    resolved = await _prepare_catalog(account_id, note_id)
    catalog = _unwrap(await note_components.read_catalog(account_id, resolved))
    return {"collections": catalog.get("collections", []), "note_id": resolved}


@router.get("/api/accounts/{account_id}/activities")
async def list_activities_endpoint(
    account_id: int,
    note_id: str | None = Query(default=None),
    keywords: str | None = Query(default=None),
) -> dict:
    """读平台活动列表并按关键词筛选(同步阻塞,内部起一次只读浏览器会话)。"""
    resolved = await _prepare_catalog(account_id, note_id)
    words = _parse_keywords(keywords)
    catalog = _unwrap(await note_components.read_catalog(account_id, resolved))
    activities = catalog.get("activities", [])
    return {
        "activities": filter_activities(activities, words),
        "total": len(activities),
        "keywords": list(words),
        "note_id": resolved,
    }


class NoteComponentsRequest(BaseModel):
    """三组件设置请求体。三个组件 id 全可选,但**至少给一个**。

    id 一律用字符串:平台侧 ``ACTIVITY_COMPONENT.bizId`` 实测是字符串(``"43561"``),
    合集 id 是 24 位 hex,笔记 id 也是 hex —— 统一字符串,别让 JSON 数字把前导 0 吃掉。
    """

    note_id: str = Field(
        min_length=1, max_length=64, description="要编辑的笔记平台 id(深链定位)"
    )
    collection_id: str | None = Field(default=None, max_length=64)
    quoted_note_id: str | None = Field(default=None, max_length=64)
    activity_id: str | None = Field(default=None, max_length=64)

    @model_validator(mode="after")
    def _require_one_component(self):
        """一个组件都不给 → 422。

        这次编辑什么都不会改,却要真提交一次**全量覆盖**的发布 —— 纯风险零收益,
        在入口就拦掉,别让它排队两分钟后才在浏览器层失败。
        """
        if not any((self.collection_id, self.quoted_note_id, self.activity_id)):
            raise ValueError(
                "collection_id / quoted_note_id / activity_id 至少要给一个"
            )
        return self


@router.post("/api/accounts/{account_id}/note-components", status_code=202)
async def start_note_components_endpoint(
    account_id: int, payload: NoteComponentsRequest
) -> dict:
    """异步触发一次三组件设置,立即返回 job_id。"""
    operator = current_operator()
    # 运营配额闸:未完成任务达上限 → 429(admin 豁免),不建任务。
    await assert_operator_quota(operator)
    async with get_session() as session:
        await assert_account_access(operator, account_id, session)
        if await session.get(XhsAccount, account_id) is None:
            raise NotFoundError(f"账号 {account_id} 不存在")
    job_id = note_components.start_components(
        account_id,
        payload.note_id,
        payload.collection_id,
        payload.quoted_note_id,
        payload.activity_id,
    )
    return {"job_id": job_id, "status": "queued"}


@router.get("/api/note-components/{job_id}")
async def get_note_components_endpoint(job_id: str) -> dict:
    """轮询三组件设置结果:queued / running / done(逐项生效情况)/ error / unknown。

    done 时把服务侧的 ``status``(done=全部生效 / partially_applied=部分生效)另开一个键
    ``result_status`` 下发 —— 外层 status 是任务生命周期,内层是这次到底改成了几项,
    合成一个键会让"只成了一半"被读成"全成了"。
    """
    row = await load_job(job_id, "note_components", "job_id")
    view = base_view(row)
    result = row.get("result") or {}
    if row["status"] == "done":
        view["result_status"] = result.get("status")
        for key in (
            "applied", "failed", "submitted", "permission_before", "permission_after",
            "permission_preserved", "permission_restored", "body_appended",
            "topics_injected",
        ):
            if key in result:
                view[key] = result[key]
    return view
