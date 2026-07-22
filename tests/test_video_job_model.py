"""video_jobs 模型单测：建表落库 + 默认值 + JSON 字段属性名/列名映射 + 派生链。

复用 conftest 的 db fixture（每测试独立临时 sqlite，自动建表 + 清理）。
"""

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import VideoJob


async def test_create_video_job_defaults(db: AsyncSession):
    """最小入参建 job，默认值全部生效（mode/status/stage/JSON 容器/计数/时间戳）。"""
    job = VideoJob(url="https://youtu.be/abc")
    db.add(job)
    await db.commit()

    assert job.id
    assert job.mode == "transport"
    assert job.status == "queued"
    assert job.stage == "download"
    assert job.stages == {}
    assert job.options == {}
    assert job.products == {}
    assert job.term_sheet == []
    assert job.retry_count == 0
    assert job.parent_job_id is None
    assert job.heartbeat_at is None
    assert job.created_at is not None
    assert job.updated_at is not None


async def test_json_fields_roundtrip_and_column_names(db: AsyncSession):
    """JSON 字段可存取复杂结构；属性名 stages/options/products/term_sheet，列名带 _json 后缀。"""
    job = VideoJob(
        url="u",
        mode="remake",
        stages={"download": {"status": "done"}},
        options={"src": "x"},
        products={"out": "/uploads/video/1-tok/out/final.mp4"},
        term_sheet=[{"en": "CPTSD", "zh": "复杂性创伤后应激障碍", "source": "manual"}],
    )
    db.add(job)
    await db.commit()
    await db.refresh(job)

    assert job.stages["download"]["status"] == "done"
    assert job.term_sheet[0]["en"] == "CPTSD"

    # 底层列名确为 *_json（M3 平移代码按属性名 .stages 访问，列名改动会断平移面）
    cols = (await db.execute(text("PRAGMA table_info(video_jobs)"))).fetchall()
    names = {row[1] for row in cols}
    assert {"stages_json", "options_json", "products_json", "term_sheet_json"} <= names
    assert not ({"stages", "options", "products", "term_sheet"} & names)


async def test_revision_parent_link(db: AsyncSession):
    """revision 派生链：parent_job_id 指回父 job。"""
    parent = VideoJob(url="u", mode="remake")
    db.add(parent)
    await db.commit()

    child = VideoJob(url="u", mode="revision", parent_job_id=parent.id)
    db.add(child)
    await db.commit()

    assert child.parent_job_id == parent.id
