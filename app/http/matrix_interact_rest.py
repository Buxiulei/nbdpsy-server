"""matrix-interact 分组 REST(2 端点):手工触发一次矩阵互动(202)+ 轮询结果。

此前矩阵互动只有"发布成功自动触发"这一条路(``schedule_matrix_interact`` 给全部有效号
群发延时任务),没法手工指定谁去互动哪一篇。本模块把服务端已有的 ``matrix_interact.execute``
开成对外端点:指定操作账号 + 目标笔记(发布者 user_id + 标题)+ 评论文案。

**动作固定为点赞 + 收藏 + 评论三件套,不做可选**:``interact_with_note`` 的三步是写死的,
开放"只点赞不收藏"要改浏览器层签名 + execute 契约 + 成败判定逻辑 + 发布钩子 payload 四处,
而当前的实际需求就是三件套 + 能传评论文案。``comment`` 传空串已经等价于"只点赞收藏"
(评论那一步记 ``not_requested``,不参与成败判定),真正取不到的只有"单点赞"/"单收藏"
这类组合,没有需求支撑。
"""

from fastapi import APIRouter
from pydantic import BaseModel, Field

from app.auth.context import current_operator
from app.auth.guards import assert_account_access
from app.core.db import get_session
from app.core.errors import NotFoundError
from app.http.job_polling import base_view, load_job
from app.models.xhs_account import XhsAccount
from app.services import matrix_interact
from app.services.quota import assert_operator_quota

router = APIRouter()

# 评论文案长度上限:接口侧护栏,**不是**实测出来的平台上限(平台真实上限未验证)。
# 取 200 是因为互动评论的用途是营销钩子短句,超过这个长度的多半是传错了字段。
_MAX_COMMENT_LEN = 200

MANIFEST_ENTRIES = [
    {
        "method": "POST", "path": "/api/accounts/{account_id}/matrix-interactions",
        "summary": "异步触发指定账号对某篇笔记做一次互动(点赞+收藏+评论)",
        "admin_only": False,
        "params": {
            "account_id": "path,int(**执行互动的账号**,不是发布者)",
            "publisher_user_id": "body,str(目标笔记发布者的小红书 user_id,进其主页用)",
            "title": "body,str(目标笔记标题,精确匹配定位)",
            "comment": f"body,str=''(评论文案,≤{_MAX_COMMENT_LEN} 字;空串=只点赞收藏)",
        },
        "returns": '{interaction_id, status:"queued"}',
        "errors": "403=无该操作账号的授权;404=账号不存在;"
                  f"422=publisher_user_id/title 为空或 comment 超 {_MAX_COMMENT_LEN} 字;"
                  "429=运营者未完成任务配额已满",
        "notes": "异步契约:起后台浏览器进发布者主页 → 按标题找卡片 → 打开详情 → 点赞 + 收藏 + "
                 "评论(约 1-2 分钟);拿 interaction_id 后每 5-10s 轮询 "
                 "GET /api/matrix-interactions/{interaction_id}。"
                 "⚠️ 三条要点:"
                 "① **动作固定三件套**,不支持只做其中一两个;传空 comment 时评论那步记 "
                 "not_requested,实际效果就是只点赞收藏;"
                 "② **靠标题精确匹配定位**,标题对不上就整任务 error(一个动作都没做);"
                 "③ **非幂等,失败不自动重跑**——重复执行会把已点的赞取消掉,所以看到 error/unknown "
                 "要先核对实际状态再决定,别盲目重试。"
                 "account_id 是去互动的那个号(需对它有授权),publisher_user_id 是被互动笔记的"
                 "作者;两者不要传反。同号浏览器操作共享 per-account 锁串行。",
    },
    {
        "method": "GET", "path": "/api/matrix-interactions/{interaction_id}",
        "summary": "轮询矩阵互动结果",
        "admin_only": False, "params": {"interaction_id": "path,str"},
        "returns": "{status, note_url?, actions?, reason?}",
        "errors": "403=无该号授权;404=interaction_id 不存在",
        "notes": "status 五态:queued / running / done / error(附 reason)/ "
                 "**unknown(执行进程中断,做没做成未知)**。"
                 "done 时 actions 是逐动作结果 {like/collect/comment: {status, reason?}},"
                 "动作级 status 四种:done(做成了)/ skipped(本就已赞/已藏,目标已达成,算成功)/ "
                 "not_requested(没传评论文案,这次压根没要求做,不算成败)/ error(该动作失败)。"
                 "**动作之间互不阻断**:一个失败其余照做,所以 done 也可能夹着个别 error 动作,"
                 "要逐个读 actions 判定;只有本次要求做的动作全都没成功才整任务落 error。"
                 "note_url 是实际打开的目标笔记链接,可用来核对定位对不对。",
    },
]


class MatrixInteractRequest(BaseModel):
    """手工互动请求体。动作固定三件套,只有评论文案可传(见模块 docstring 的取舍说明)。"""

    publisher_user_id: str = Field(
        min_length=1, max_length=64, description="目标笔记发布者的小红书 user_id"
    )
    title: str = Field(
        min_length=1, max_length=100, description="目标笔记标题(精确匹配定位)"
    )
    comment: str = Field(
        default="",
        max_length=_MAX_COMMENT_LEN,
        description="评论文案;空串则跳过评论,只点赞收藏",
    )


@router.post("/api/accounts/{account_id}/matrix-interactions", status_code=202)
async def start_matrix_interaction_endpoint(
    account_id: int, payload: MatrixInteractRequest
) -> dict:
    """异步触发一次矩阵互动,立即返回 interaction_id。

    account_id 是**执行互动的账号**(鉴权对象),publisher_user_id 是目标笔记的作者
    ——后者不需要是本系统的号(可以去互动任何人的笔记),故不对它做账号存在性校验。
    """
    operator = current_operator()
    # 运营配额闸:未完成任务达上限 → 429(admin 豁免),不建互动任务。
    await assert_operator_quota(operator)
    async with get_session() as session:
        await assert_account_access(operator, account_id, session)
        if await session.get(XhsAccount, account_id) is None:
            raise NotFoundError(f"账号 {account_id} 不存在")
    interaction_id = matrix_interact.start_interact(
        account_id, payload.publisher_user_id, payload.title, payload.comment
    )
    return {"interaction_id": interaction_id, "status": "queued"}


@router.get("/api/matrix-interactions/{interaction_id}")
async def get_matrix_interaction_endpoint(interaction_id: str) -> dict:
    """轮询互动结果:queued / running / done(附 note_url + 逐动作结果)/ error / unknown。"""
    row = await load_job(interaction_id, "matrix_interact", "interaction_id")
    view = base_view(row)
    if row["status"] == "done":
        result = row.get("result") or {}
        view["note_url"] = result.get("note_url")
        view["actions"] = result.get("actions") or {}
    return view
