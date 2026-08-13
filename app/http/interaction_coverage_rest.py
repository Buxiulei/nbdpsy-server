"""interaction-coverage 分组 REST(1 端点):矩阵互刷完成率总账。

老板 KPI:**池内每个账号的历史所有公开笔记,都要被其余每个账号点赞 + 收藏,完成率 100%**。
在这个端点之前,运营是逐号翻 ``/api/accounts/{id}/self-interactions`` 再手拼分母报数 ——
分母拼错过两次(拿指标视图当台账)。这里给一份与补量调度(``interaction_backfill.plan_round``)
同口径的权威数,口径与理由全在 ``app.services.interaction_coverage``,本文件只做鉴权收窄
与视图组装。

**纯 DB 读,不起浏览器**:三条 select 全量取回内存聚合,零会话成本、零风控暴露,可随意调。
"""

from datetime import datetime

from fastapi import APIRouter

from app.auth.context import current_operator
from app.auth.guards import visible_account_ids
from app.core.db import get_session
from app.services import interaction_coverage

router = APIRouter()

MANIFEST_ENTRIES = [
    {
        "method": "GET", "path": "/api/interaction-coverage",
        "summary": "矩阵互刷完成率总账:每篇公开笔记 × 池内其余每个号,做完了几格(纯 DB 读)",
        "admin_only": False,
        "params": {},
        "returns": "{generated_at, pool_size, unowned_notes, "
                   "accounts:[{account_id, name, public_notes, combos_total, combos_covered, "
                   "combos_pending, completion_pct, "
                   "unschedulable:{ledger_no_note_id, nonpublic}, zero_touch_notes}], "
                   "actors:[{account_id, name, done_skipped_combos, pending_combos}], "
                   "totals:{同 accounts 结构的聚合}, note}",
        "errors": "401=缺 apikey",
        "notes": "**用途**:回答「互刷 KPI 到哪一步了、谁还欠多少活」,取代逐号翻 "
                 "self-interactions 手拼分母(分母拼错过两次)。与补量调度 plan_round 同口径。"
                 "**纯 DB 读**:不起浏览器、不消耗任何账号会话额度,可随意调。"
                 "⚠️ 六条读数纪律:"
                 "① **一格 = (一篇公开笔记, 一个非作者的池内号)**,不是一篇笔记 —— "
                 "combos_total = public_notes × (pool_size - 1);作者自己那格不存在,"
                 "他自己点的赞也不算覆盖;"
                 "② **分母笔记 = 未删除 + 有 note_id + permission_code=0**。permission_code "
                 "为 NULL 是**未知不是公开**,归 nonpublic 排除(把未知当公开就会对可能私密的"
                 "笔记去点赞);已删除的笔记平台上没有了,既不进分母也不进 unschedulable;"
                 "③ **covered 认 done 与 skipped**:skipped = 去点时平台上本来就是目标态,"
                 "对那个号来说赞就在那篇笔记上。只数 done 会少算一大半(生产 1215 done vs "
                 "1893 skipped);error 不算。**池外 actor 的行也不算** —— 退出矩阵的号点过的"
                 "赞不再顶 KPI;"
                 "④ **unschedulable 不进 combos_total 但要盯**:ledger_no_note_id 是台账行在、"
                 "平台 id 还没同步回来(运营动作 = 跑一次台账同步,同步完它们就进分母,"
                 "完成率会因此**下降**,那不是退步);nonpublic 是按「公开」口径主动排除;"
                 "⑤ **zero_touch_notes 是分母内一格都没到位的篇**,它们在选篇队列尾部"
                 "(plan_round 按 first_seen_at 倒序,老笔记最后轮到),会被覆盖,不是漏了;"
                 "⑥ **completion_pct 分母为 0 时给 100.0**(没有应做的活 = 没有欠账);"
                 "totals 的完成率**按格子重算**不是各号完成率求平均(平均会让 7 篇的新号和 "
                 "70 篇的主力号一样重)。"
                 "**池含全部账号**(不看 cookie 是否有效):KPI 说的是「池内每个号」。但 "
                 "plan_round 只派给 cookie_status=valid 的号 —— 失效号的 pending_combos "
                 "谁也接不走,先去恢复登录,别指望调度自己消化。"
                 "unowned_notes = 作者号已退出矩阵(账号行没了)的活笔记数:它们不进任何"
                 "账号行也不进 totals(调度同样永远不会再为它们派单),单独报数以免静默消失。"
                 "accounts 按 caller 可见账号收窄(admin 全见),**actors 出勤表始终是全池** "
                 "—— 收窄它会让某个号的 pending 凭空少掉,而「谁还没去点」正是它要回答的。",
    },
]


@router.get("/api/interaction-coverage")
async def interaction_coverage_endpoint() -> dict:
    """互刷完成率总账(纯 DB 读)。"""
    operator = current_operator()
    async with get_session() as session:
        account_ids = await visible_account_ids(operator, session)
        data = await interaction_coverage.compute_coverage(
            session, account_ids=account_ids
        )
    return {
        # naive UTC(与库内时间列同基准,与 self-interactions 的 first_at/last_at 一致)
        "generated_at": datetime.utcnow().isoformat(),
        **data,
        "note": interaction_coverage.COVERAGE_NOTE,
    }
