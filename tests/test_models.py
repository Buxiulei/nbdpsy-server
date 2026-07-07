"""4 张核心表模型单测:建表 + 默认值 + 唯一约束。

复用 conftest 的 db fixture(每测试独立临时 sqlite,自动建表 + 清理)。
覆盖:
- 四模型均可建表并落库,主键自增可用。
- 默认值:Operator.enabled=True / XhsAccount.status=cookie_status='unknown'
  / PublishJob.status='pending' 且 retries=0 / created_at 自动填充。
- Operator.apikey_hash 唯一约束。
- OperatorAccountAccess 的 UNIQUE(operator_id, xhs_account_id) 复合唯一约束。
"""

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Operator, OperatorAccountAccess, PublishJob, XhsAccount


async def test_create_models(db: AsyncSession):
    """四模型建表落库 + 默认值全部生效。"""
    op = Operator(name="a", apikey_hash="h", role="admin")
    db.add(op)
    await db.commit()
    assert op.id and op.enabled is True
    assert op.created_at is not None

    acc = XhsAccount(name="号1")
    db.add(acc)
    await db.commit()
    assert acc.status == "unknown"
    assert acc.cookie_status == "unknown"
    assert acc.created_at is not None

    job = PublishJob(
        account_id=acc.id, title="t", content="c", images_json="[]", topics_json="[]"
    )
    db.add(job)
    await db.commit()
    assert job.status == "pending" and job.retries == 0
    assert job.created_at is not None

    access = OperatorAccountAccess(operator_id=op.id, xhs_account_id=acc.id)
    db.add(access)
    await db.commit()
    assert access.id and access.created_at is not None


async def test_apikey_hash_unique(db: AsyncSession):
    """apikey_hash 唯一:重复插入触发 IntegrityError。"""
    db.add(Operator(name="a", apikey_hash="dup", role="operator"))
    await db.commit()
    db.add(Operator(name="b", apikey_hash="dup", role="operator"))
    with pytest.raises(IntegrityError):
        await db.commit()


async def test_operator_account_access_unique(db: AsyncSession):
    """UNIQUE(operator_id, xhs_account_id):同一对重复授权触发 IntegrityError。"""
    op = Operator(name="a", apikey_hash="h", role="operator")
    acc = XhsAccount(name="号1")
    db.add_all([op, acc])
    await db.commit()

    db.add(OperatorAccountAccess(operator_id=op.id, xhs_account_id=acc.id))
    await db.commit()
    db.add(OperatorAccountAccess(operator_id=op.id, xhs_account_id=acc.id))
    with pytest.raises(IntegrityError):
        await db.commit()
