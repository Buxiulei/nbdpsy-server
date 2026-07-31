"""browser_jobs 轮询端点的公共读法:取台账行 + RBAC 收窄 + 五态映射。

``note_ledger_sync`` / ``note_visibility`` / ``matrix_interact`` 三个异步端点的轮询
逻辑只有"看哪些结果字段"不同,取行、防越权、把台账 status 译成对外语义这三步完全一样,
故收在这里,不在每个 router 模块各抄一遍。

**五态语义**(对外契约,三个端点共用):

- ``queued``:已登记待派发(worker 还没领);
- ``running``:执行中;
- ``done``:执行成功,结果字段见各端点;
- ``error``:执行失败,``reason`` 给原因;**不代表下次必失败**;
- ``unknown``:执行进程中断(僵死恢复),**做没做成未知**。只会出现在非幂等 kind
  (``note_visibility`` / ``matrix_interact``)上——幂等 kind 僵死后是重置回 queued
  自动重跑,不会译成 unknown。看到 unknown 必须人工核对实际状态再决定是否重发,
  ``reason`` 里带核对指引。
"""

from app.auth.context import current_operator
from app.auth.guards import assert_account_access
from app.core.db import get_session
from app.core.errors import NotFoundError
from app.services import browser_jobs_repo


async def load_job(job_id: str, kind: str, label: str) -> dict:
    """按 id 取 browser_jobs 行并做 RBAC 收窄;不存在/kind 不符 → 404,越权 → 403。

    kind 一并校验:拿 visibility 的 id 去查互动端点应当是 404,而不是返回一条别的
    任务的状态。RBAC 用台账行里存的 account_id(与 note-exports 轮询同款防越权)。
    """
    row = await browser_jobs_repo.get_job(job_id)
    if row is None or row["kind"] != kind:
        raise NotFoundError(f"{label} {job_id} 不存在")
    operator = current_operator()
    async with get_session() as session:
        await assert_account_access(operator, row["account_id"], session)
    return row


def base_view(row: dict) -> dict:
    """台账行 → ``{"status", "reason"?}`` 公共外壳(结果字段由各端点自行追加)。

    error 行带 ``unknown`` 标记(僵死恢复写入)时译成 ``unknown`` 语义——绝不冒充
    普通失败,那会让调用方以为"没做成"而放心重发。
    """
    result = row.get("result") or {}
    if row["status"] == "error":
        return {
            "status": "unknown" if result.get("unknown") else "error",
            "reason": result.get("error"),
        }
    return {"status": row["status"]}
