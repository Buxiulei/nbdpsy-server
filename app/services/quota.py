"""运营者配额:enqueue 入口的未完成任务数闸门。

设计依据 docs/design/2026-07-24-api-worker-split-design.md 第五节:
- enqueue 时统计该 operator 未终态任务数——browser_jobs(queued/running)+
  publish_jobs(pending/publishing),由 browser_jobs_repo.count_unfinished_for_operator
  统一实现;
- 达到 OPERATOR_PENDING_QUOTA(默认 30)→ 429,文案明确给出 N/上限;
- admin 不受限。无速率桶、无滑动窗——数据库计数即够,不增实体。

429 用 fastapi.HTTPException 直抛(FastAPI 默认 handler 返 {"detail": ...},
与鉴权中间件 401 的响应形状一致),不在 server.py 增专用异常映射。
"""

from fastapi import HTTPException

from app.core.config import settings
from app.models.operator import Operator

# browser_jobs_repo 由台账落库(P1)提供;并行开发窗口内可能尚未落地——导入失败时
# 置 None,配额检查跳过(fail-open:只影响公平限流,不影响任务正确性),整合后
# import 恒成功。测试 monkeypatch 本模块的 browser_jobs_repo 注入假实现即可计数。
try:
    from app.services import browser_jobs_repo
except ImportError:
    browser_jobs_repo = None


async def assert_operator_quota(operator: Operator) -> None:
    """校验 operator 未完成任务配额;达上限抛 HTTPException(429),admin 豁免。

    判定用 count >= quota:未完成数已到上限即拒绝新提交,保证任何 operator 的
    未完成任务数不越过 OPERATOR_PENDING_QUOTA。
    """
    if operator.role == "admin":
        return
    if browser_jobs_repo is None:
        return
    count = await browser_jobs_repo.count_unfinished_for_operator(operator.id)
    quota = settings.OPERATOR_PENDING_QUOTA
    if count >= quota:
        raise HTTPException(
            status_code=429,
            detail=f"配额已满:未完成任务 {count}/{quota},请等待完成后再提交",
        )
