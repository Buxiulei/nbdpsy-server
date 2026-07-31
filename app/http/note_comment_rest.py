"""note-comments 分组 REST(2 端点):对某篇笔记发一条评论(202)+ 轮询结果。

评论 2026-07-31 从矩阵互动三件套里拆出来独立成一条能力:矩阵互动固定点赞 + 收藏、
发布成功后自动触发;评论走本组端点手工触发。浏览器侧复用真号验证过的 ``_do_comment``
(点「说点什么」激活入口 → 等输入框可交互 → 拟人点击聚焦 → 逐字输入 → 等发送键去掉
gray → 点发送 → 复核输入框清空且评论出现在列表才算成功),定位复用矩阵互动那套主页
路径,**都没有重写**。

⚠️ 这条链路**非幂等**:评论是追加不是开关,重复调会发出**重复评论**。不进
``_IDEMPOTENT_KINDS``,僵死不自动重跑;调用方看到失败也必须先去笔记下核对评论到底
发出去没有再决定,不能盲目重试。
"""

from fastapi import APIRouter
from pydantic import BaseModel, Field

from app.auth.context import current_operator
from app.auth.guards import assert_account_access
from app.core.db import get_session
from app.core.errors import NotFoundError
from app.http.job_polling import base_view, load_job
from app.models.xhs_account import XhsAccount
from app.services import note_comment
from app.services.quota import assert_operator_quota

router = APIRouter()

# 评论文案长度上限:接口侧护栏,**不是**实测出来的平台上限(平台真实上限未验证)。
# 取 200 是因为互动评论的用途是短句钩子,超过这个长度的多半是把正文传错了字段。
_MAX_TEXT_LEN = 200

MANIFEST_ENTRIES = [
    {
        "method": "POST", "path": "/api/accounts/{account_id}/note-comments",
        "summary": "异步触发指定账号对某篇笔记发一条评论(会发出真实评论,慎用)",
        "admin_only": False,
        "params": {
            "account_id": "path,int(**发评论的账号**,不是被评论笔记的作者)",
            "publisher_user_id": "body,str(目标笔记发布者的小红书 user_id,进其主页用)",
            "title": "body,str(目标笔记标题,精确匹配定位)",
            "text": f"body,str(评论文案,**必填**,1-{_MAX_TEXT_LEN} 字)",
        },
        "returns": '{comment_id, status:"queued"}',
        "errors": "403=无该账号授权;404=账号不存在;"
                  f"422=publisher_user_id/title/text 为空或 text 超 {_MAX_TEXT_LEN} 字;"
                  "429=运营者未完成任务配额已满",
        "notes": "异步契约:起后台浏览器进发布者主页 → 按标题找卡片 → 打开详情 → 拟人浏览 → "
                 "激活评论区逐字输入 → 发送 → 复核评论真的出现在列表(约 1-2 分钟);拿 "
                 "comment_id 后每 5-10s 轮询 GET /api/note-comments/{comment_id}。"
                 "⚠️ 三条要点:"
                 "① **非幂等,失败不自动重跑**——评论是追加不是开关,重复调会发出**重复评论**;"
                 "看到 error/unknown 必须先去那篇笔记下核对评论发出去没有,**不要盲目重试**;"
                 "② **靠标题精确匹配定位**,标题对不上就 error(评论没发出去);"
                 "③ account_id 是发评论的号(需对它有授权),publisher_user_id 是被评论笔记的"
                 "作者,两者不要传反;作者不必是本系统的账号。"
                 "同号浏览器操作(发布/cookie 检测/导出/切可见性/互动)共享 per-account 锁串行。",
    },
    {
        "method": "GET", "path": "/api/note-comments/{comment_id}",
        "summary": "轮询评论结果",
        "admin_only": False, "params": {"comment_id": "path,str"},
        "returns": "{status, note_url?, commented?, reason?}",
        "errors": "403=无该号授权;404=comment_id 不存在",
        "notes": "status 五态:queued(待派发)/ running(执行中)/ done(评论已发出**并复核**"
                 "到它出现在评论列表,附 note_url 与 commented:true)/ error(附 reason,如 "
                 "note_not_found=标题定位不到、comment_submit_disabled=发送键始终禁用、"
                 "comment_unverified=点了发送但没复核到评论)/ **unknown(执行进程中断,"
                 "评论发没发出去未知)**。"
                 "⚠️ error 里 comment_unverified 与 unknown 两种情况**都不能断定没发出去**"
                 "——前者是点了发送但复核超时,后者连提没提交都不知道;非幂等,重试前必须"
                 "人工去笔记评论区核对,否则会留下重复评论。"
                 "note_url 是实际打开的目标笔记链接,可用来核对定位对不对(error 时也可能有)。",
    },
]


class NoteCommentRequest(BaseModel):
    """单篇评论请求体。文案必填——本端点唯一的动作就是评论,空文案没有可执行的动作。"""

    publisher_user_id: str = Field(
        min_length=1, max_length=64, description="目标笔记发布者的小红书 user_id"
    )
    title: str = Field(
        min_length=1, max_length=100, description="目标笔记标题(精确匹配定位)"
    )
    text: str = Field(
        min_length=1, max_length=_MAX_TEXT_LEN, description="评论文案(必填)"
    )


@router.post("/api/accounts/{account_id}/note-comments", status_code=202)
async def start_note_comment_endpoint(
    account_id: int, payload: NoteCommentRequest
) -> dict:
    """异步触发一条评论,立即返回 comment_id。

    过配额闸的理由同 note-exports / note-deletions / note-visibility-changes:要起一个真
    camoufox 会话,配额闸护的正是这条浏览器流水线。account_id 是**发评论的账号**(鉴权
    对象);publisher_user_id 是目标笔记的作者,不必是本系统的号,故不对它做存在性校验。
    """
    operator = current_operator()
    # 运营配额闸:未完成任务达上限 → 429(admin 豁免),不建评论任务。
    await assert_operator_quota(operator)
    async with get_session() as session:
        await assert_account_access(operator, account_id, session)
        if await session.get(XhsAccount, account_id) is None:
            raise NotFoundError(f"账号 {account_id} 不存在")
    comment_id = note_comment.start_comment(
        account_id, payload.publisher_user_id, payload.title, payload.text
    )
    return {"comment_id": comment_id, "status": "queued"}


@router.get("/api/note-comments/{comment_id}")
async def get_note_comment_endpoint(comment_id: str) -> dict:
    """轮询评论结果:queued / running / done(附 note_url + commented)/ error / unknown。

    error 时也把 note_url 带出来(定位成功但评论失败的情况下它是有值的),方便调用方
    直接点进去人工核对评论到底发出去没有——非幂等链路,核对是重试前的必要步骤。
    """
    row = await load_job(comment_id, "note_comment", "comment_id")
    view = base_view(row)
    result = row.get("result") or {}
    if row["status"] == "done":
        view["note_url"] = result.get("note_url")
        view["commented"] = bool(result.get("commented"))
    elif result.get("note_url"):
        view["note_url"] = result["note_url"]
    return view
