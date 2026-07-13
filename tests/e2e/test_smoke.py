"""端到端冒烟:整个纯 REST 后台的两条关键链路走通。

两条链:
1. RBAC 链(不标 slow,纯 DB/REST 调用,不起浏览器):
   POST /api/operators → POST /api/operators/{id}/grants → 该 operator 只见被授权号。
   走真实 REST 端点(rest_client 起真实 lifespan),验证管理面 + 访问收窄端到端自洽。
2. 发布链(标 slow,需真 cookie + 浏览器,默认不在 CI 跑):
   POST /api/cookies/import → cookie-checks 轮询 → POST /api/publish-jobs → 轮询终态。
   缺真账号素材(环境变量未配)时 skip,绝不阻塞 CI。

隔离手法与单测一致:rest_client(tmp sqlite + 真实 lifespan),operator 用各自的 apikey
过 Bearer 头鉴权,不再走 ContextVar 直注(那是 MCP 工具直调年代的手法)。
"""

import asyncio
import json
import os

import pytest

import app.core.db as db_module
from app.models import XhsAccount
from tests.rest_helpers import ADMIN_KEY, bearer, rest_client

# ============ 链路 1:RBAC 端到端(不 slow)============


async def test_rbac_chain_operator_sees_only_granted(tmp_path, monkeypatch):
    """建 operator → 建两号 → grant 其一 → operator 只见被授权号,越权号被拒。"""
    async with rest_client(tmp_path, monkeypatch) as c:
        # 1. admin 建一个 operator(拿到一次性 apikey)
        r = await c.post(
            "/api/operators",
            json={"name": "运营小张", "role": "operator"},
            headers=bearer(ADMIN_KEY),
        )
        assert r.status_code == 200, r.text
        op = r.json()
        op_id = op["id"]
        op_key = op["apikey"]
        assert op["role"] == "operator"
        assert op_key  # 一次性明文 apikey 已返回

        # 2. admin 建两个受托管账号(直接入库,避免起浏览器)
        async with db_module.async_session() as s:
            acc1 = XhsAccount(name="号1")
            acc2 = XhsAccount(name="号2")
            s.add_all([acc1, acc2])
            await s.commit()
            acc1_id, acc2_id = acc1.id, acc2.id

        # 3. admin 只把 acc1 授权给该 operator
        r = await c.post(
            f"/api/operators/{op_id}/grants",
            json={"xhs_account_id": acc1_id},
            headers=bearer(ADMIN_KEY),
        )
        assert r.status_code == 200, r.text
        assert r.json()["xhs_account_id"] == acc1_id

        # 4. admin 视角:授权清单只含 acc1
        r = await c.get(f"/api/operators/{op_id}/grants", headers=bearer(ADMIN_KEY))
        assert r.status_code == 200, r.text
        assert r.json()["xhs_account_ids"] == [acc1_id]

        # 5. operator 视角:GET /api/accounts 只见 acc1;取 acc2 被拒(越权 403)
        r = await c.get("/api/accounts", headers=bearer(op_key))
        assert r.status_code == 200, r.text
        visible = {a["id"] for a in r.json()["accounts"]}
        assert visible == {acc1_id}

        r = await c.get(f"/api/accounts/{acc1_id}", headers=bearer(op_key))
        assert r.status_code == 200, r.text
        assert r.json()["id"] == acc1_id

        r = await c.get(f"/api/accounts/{acc2_id}", headers=bearer(op_key))
        assert r.status_code == 403


# ============ 链路 2:发布端到端(slow,需真账号)============

# 发布冒烟需真 cookie:环境变量 NBDPSY_E2E_COOKIES(cookies JSON 字符串)+
# NBDPSY_E2E_ACCOUNT_NAME(可选,默认 e2e-号)。未配则 skip,绝不阻塞 CI。
_E2E_COOKIES = os.getenv("NBDPSY_E2E_COOKIES")


@pytest.mark.slow
@pytest.mark.skipif(
    not _E2E_COOKIES,
    reason="需真小红书 cookie:设 NBDPSY_E2E_COOKIES(+可选 NBDPSY_E2E_ACCOUNT_NAME)后手动跑",
)
async def test_publish_chain_real_account(tmp_path, monkeypatch):
    """POST /api/cookies/import → cookie-checks → POST /api/publish-jobs → 轮询终态(需真号真浏览器)。

    默认 skip;手动跑时需 Xvfb(:99)可用 + 真 cookie。发布内容用明确的测试标记,
    跑完请自行到小红书后台删除产出的测试笔记(本冒烟不自动删远端笔记)。
    """
    account_name = os.getenv("NBDPSY_E2E_ACCOUNT_NAME", "e2e-号")

    async with rest_client(tmp_path, monkeypatch) as c:
        # 1. 灌 cookie(admin 建号即拥有 access)
        r = await c.post(
            "/api/cookies/import",
            json={
                "account_name": account_name,
                "cookies": json.loads(_E2E_COOKIES),
            },
            headers=bearer(ADMIN_KEY),
        )
        assert r.status_code == 200, r.text
        account_id = r.json()["account_id"]

        # 2. 活性巡检(异步):发起返 check_id → 轮询到终态
        r = await c.post(
            f"/api/accounts/{account_id}/cookie-checks", headers=bearer(ADMIN_KEY)
        )
        assert r.status_code == 202, r.text
        check_id = r.json()["check_id"]
        check_status = None
        for _ in range(30):  # 最多 ~60s 等浏览器检测
            r = await c.get(
                f"/api/cookie-checks/{check_id}", headers=bearer(ADMIN_KEY)
            )
            check_status = r.json()["status"]
            if check_status != "checking":
                break
            await asyncio.sleep(2)
        assert check_status == "valid", "cookie 已失效,先重新导出再跑"

        # 3. 建发布任务(立即入队;至少 1 张图片,用占位远程图 URL)
        r = await c.post(
            "/api/publish-jobs",
            json={
                "account_id": account_id,
                "title": "e2e 冒烟测试笔记",
                "content": "这是一条自动化端到端冒烟测试,请忽略。",
                "images": ["https://via.placeholder.com/800x600.png"],
                "topics": [],
            },
            headers=bearer(ADMIN_KEY),
        )
        assert r.status_code == 202, r.text
        job_id = r.json()["job_id"]

        # 4. 轮询终态(最多 ~2 分钟),断言落到 published
        final_status = None
        for _ in range(60):
            r = await c.get(
                f"/api/publish-jobs/{job_id}", headers=bearer(ADMIN_KEY)
            )
            final_status = r.json()["status"]
            if final_status in ("published", "failed"):
                break
            await asyncio.sleep(2)
        assert final_status == "published", f"发布未成功,终态={final_status}"
