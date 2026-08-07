"""notes/extract 分组 REST(2 端点):提取他人笔记内容 + 轮询评论抓取结果。

运营做对标拆解时发来一条分享链接,要把这篇的正文/图片/评论/互动数据完整取回来
(需求:``/home/roots/NBDpsy/docs/2026-08-07-小红书笔记内容提取能力-需求.md``)。

**同步 + 异步混合契约**,分界线就是"要不要烧浏览器会话":

- 正文 / 话题 / 图片 / 互动数 / 作者 / 发布时间 / 商品笔记标记 —— 纯 HTTP 一次 GET
  就能拿全(取证实证),故 ``POST /api/notes/extract`` **同步**返回,零会话成本;
- 评论 —— 服务端渲染里压根没有,必须让真实浏览器去拉。故 ``with_comments>0`` 时另起
  一条 ``browser_jobs``,POST 里带回 ``comments_job``,调用方轮询
  ``GET /api/note-extracts/{job_id}`` 取评论。

返回里 ``source.browser_session_used`` 明确告知本次有没有消耗会话额度(运营要求)。
"""

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.auth.context import current_operator
from app.auth.guards import assert_account_access
from app.core.db import get_session
from app.core.errors import NotFoundError
from app.http.job_polling import base_view, load_job
from app.models.xhs_account import XhsAccount
from app.services import note_extract, note_extract_comments
from app.services.quota import assert_operator_quota

router = APIRouter()

# 评论条数上限:超过这个数要滚很久,会话越长风控暴露面越大。运营场景(找标题素材)
# 几十条足够;真要全量请分次或另提需求。
_MAX_COMMENTS = 100

MANIFEST_ENTRIES = [
    {
        "method": "POST", "path": "/api/notes/extract",
        "summary": "提取一篇**他人**小红书笔记的完整内容(对标拆解用):正文/图片/互动数/作者",
        "admin_only": False,
        "params": {
            "url": "body,str(分享短链 xhslink.cn/... 或完整笔记链接,两种都吃)",
            "with_images": "body,bool=true(true 时代下图片存自家图床,images[].url 给 mcp 直链)",
            "with_comments": f"body,int=0(要前 N 条评论;>0 会**起一次浏览器会话**,0-{_MAX_COMMENTS})",
            "account_id": "body,int|None(**仅 with_comments>0 时必填**:用哪个号去看评论)",
            "refresh": "body,bool=false(true 跳过 24h 缓存强制重抓)",
        },
        "returns": "{note_id, note_type, title, content, topics[], images[], video, "
                   "comments, comments_complete, comments_source, "
                   "interact{liked,collected,comment,share}, author{nickname,user_id,profile_url,"
                   "ip_location}, published_at, is_goods_note, is_goods_note_source, "
                   "unavailable{键→拿不到的原因}, source{final_url,from_cache,browser_session_used}, "
                   "comments_job?{job_id,status}}",
        "errors": "400=链接里没有 note_id / 笔记页面拉不到 / 页面结构变了(reason 说明是哪种);"
                  "403=无该 account_id 授权;404=account_id 不存在;"
                  f"422=with_comments 越界(0-{_MAX_COMMENTS})或 with_comments>0 却没给 account_id;"
                  "429=运营者未完成任务配额已满",
        "notes": "**拿不到的字段一律给 null 并在 unavailable 里说明原因,绝不静默省略键**。要点:"
                 "① **正文/图片/互动数据零会话成本**(纯 HTTP),``with_comments=0`` 时本端点"
                 "完全不碰浏览器,爱调多少次调多少次(仍有 24h 缓存);"
                 "② **评论必须起浏览器会话**:传 with_comments>0 + account_id 后本调用同步返回内容、"
                 "另给 comments_job.job_id,每 5-10s 轮询 GET /api/note-extracts/{job_id} 取评论"
                 "(约 1-2 分钟)。**account_id 请用非品牌号**——浏览他人笔记是水军号的正常行为,"
                 "用品牌号既浪费该号会话额度又不像真人;"
                 "③ **source.browser_session_used 只说「本次调用」烧没烧会话额度**;"
                 "评论数据本身是不是靠会话取回来的看 comments_source(命中缓存时它是 "
                 "browser_session 而 browser_session_used 是 false —— 数据来源与本次成本是两回事);"
                 "④ **24h 缓存**:同一 note_id 一天内重复请求直接回缓存(source.from_cache=true),"
                 "评论抓完也会并进缓存件,再问同一篇不再开会话;原帖改过了传 refresh=true;"
                 "⑤ **is_goods_note 的唯一判据是链接上的 noteAttributes=goods**(取证确认页面本身"
                 "没有任何商品笔记的结构化信号),所以**务必用分享按钮生成的原始链接**,手工精简过的"
                 "URL 会让它静默判 false;判 false 时 unavailable 里会写明这一点;"
                 "⑥ images[].url 是我方图床直链(下载零障碍),另给 permanent_url(平台永久原图链,"
                 "体积大 10 倍以上)与 signed_url(平台签名展示图,18 天过期);"
                 "⑦ 视频笔记的页面结构**尚未取证**,video 字段是尽力扫描的结果并带 "
                 "schema_verified=false,拿去下载前请自行验一次可达性。",
    },
    {
        "method": "GET", "path": "/api/note-extracts/{job_id}",
        "summary": "轮询评论抓取结果(POST /api/notes/extract 带 with_comments 时才有)",
        "admin_only": False, "params": {"job_id": "path,str"},
        "returns": "{status, comments?[{comment_id,author,text,like_count,is_author_reply,"
                   "sub_comments[]}], count?, complete?, stop_reason?, reason?}",
        "errors": "403=无该号授权;404=job_id 不存在",
        "notes": "status 五态:queued / running / done / error(reason 给原因,如 wall_scan_qr="
                 "该号撞了验证墙需手机扫码)/ unknown。**幂等**:纯只读抓取,失败可直接重发。"
                 "complete=false 表示没抓到底(轮数封顶),comments 里是已抓到的那些;"
                 "stop_reason 说明为什么停(reached_limit=拿够了 / no_new_after_scroll=到底了 / "
                 "reached_expected_total=已抓够平台标称的评论总数 / round_cap=轮数封顶 / empty=没有评论)。"
                 "⚠️ comments 为空且 stop_reason=empty 时,请对照 interact.comment(平台标称评论数)"
                 "交叉验证:标称不为 0 却抓到 0 条,多半是页面结构变了而不是真没人评论。",
    },
]


class NoteExtractRequest(BaseModel):
    """提取请求体。``url`` 两种形态都吃(短链 / 完整链接),服务端自行跟跳。"""

    url: str = Field(min_length=8, max_length=2048, description="分享短链或完整笔记链接")
    with_images: bool = Field(default=True, description="是否代下图片进自家图床")
    with_comments: int = Field(
        default=0, ge=0, le=_MAX_COMMENTS, description="取前 N 条评论(>0 消耗浏览器会话)"
    )
    account_id: int | None = Field(
        default=None, description="抓评论用哪个号(with_comments>0 时必填,建议用非品牌号)"
    )
    refresh: bool = Field(default=False, description="跳过 24h 缓存强制重抓")


@router.post("/api/notes/extract")
async def extract_note_endpoint(payload: NoteExtractRequest) -> dict:
    """提取一篇他人笔记:内容同步返回;要评论则另起浏览器任务并带回 job_id。

    先做 RBAC/配额(会烧会话的那一半)再拉页面 —— 没授权就不该白拉一次外站页面。
    """
    operator = current_operator()
    if payload.with_comments > 0:
        if payload.account_id is None:
            raise HTTPException(
                status_code=422,
                detail="要抓评论必须给 account_id(评论只能靠真实浏览器会话拿到);"
                       "请用非品牌号,浏览他人笔记是水军号的正常行为",
            )
        await assert_operator_quota(operator)
        async with get_session() as session:
            await assert_account_access(operator, payload.account_id, session)
            if await session.get(XhsAccount, payload.account_id) is None:
                raise NotFoundError(f"账号 {payload.account_id} 不存在")

    async with get_session() as session:
        # NoteExtractError 是 ValueError 子类,由 app 级 handler 统一转 400 {"error"},
        # 这里不再包一层 —— 多包一层只会让错误文案在两处各写一遍。
        result = await note_extract.extract(
            payload.url,
            with_images=payload.with_images,
            with_comments=payload.with_comments,
            operator=operator,
            session=session,
            refresh=payload.refresh,
        )

    if payload.with_comments > 0 and result.get("comments") is None:
        job_id = note_extract_comments.start_comments(
            payload.account_id,
            {
                "note_id": result["note_id"],
                "note_url": result["source"]["final_url"],
                "max_count": payload.with_comments,
                "note_author_user_id": (result.get("author") or {}).get("user_id"),
                "expected_total": (result.get("interact") or {}).get("comment"),
            },
        )
        result["comments_job"] = {
            "job_id": job_id,
            "status": "queued",
            "poll": f"/api/note-extracts/{job_id}",
            "session_cost": f"本次会消耗账号 {payload.account_id} 的一次浏览器会话额度",
        }
    else:
        result["comments_job"] = None
    return result


@router.get("/api/note-extracts/{job_id}")
async def get_note_extract_endpoint(job_id: str) -> dict:
    """轮询评论抓取:queued / running / done(附 comments)/ error / unknown。"""
    row = await load_job(job_id, note_extract_comments.JOB_KIND, "job_id")
    view = base_view(row)
    result = row.get("result") or {}
    if row["status"] == "done":
        view["comments"] = result.get("comments") or []
        view["count"] = result.get("count")
        view["complete"] = result.get("complete")
        view["stop_reason"] = result.get("stop_reason")
    elif result.get("partial_comments"):
        # 撞墙中断:已抓到的那些仍交出去(证据不丢),但状态仍是 error
        view["comments"] = result["partial_comments"]
        view["complete"] = False
    return view
