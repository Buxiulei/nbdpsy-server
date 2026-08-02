"""发布失败现场取回:按 job 列清单 + 下载单张 + 路径穿越防护。

背景(2026-08-02 事故):截图一直在存,但没有任何东西把它和某次发布关联起来,于是运营
拿到的全部信息只有一句「发布超时(30秒)」,只能换外部变量试错(试了三轮全是徒劳)。
而那次的根因看一眼 `12_before_publish` 就能看出来——引用弹窗盖住了发布按钮。

所以这组用例锁的是:**该次发布的现场能按 job 取回,而且顺序是发布流程的真实时序**
(按文件名排序会把 waiting_6s 排到 waiting_12s 后面,"卡在哪一步"就看不出来了)。
"""

from io import BytesIO
from pathlib import Path

import pytest
from PIL import Image

from app.core import config as config_module
from tests.rest_helpers import ADMIN_KEY, bearer, rest_client


def _shot(root: Path, name: str) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    path = root / name
    buf = BytesIO()
    Image.new("RGB", (4, 4), (200, 30, 30)).save(buf, format="PNG")
    path.write_bytes(buf.getvalue())
    return path


@pytest.fixture
def shots(tmp_path, monkeypatch):
    """把 DATA_DIR 指到临时目录,返回截图根目录。"""
    monkeypatch.setattr(config_module.settings, "DATA_DIR", str(tmp_path))
    return tmp_path / "debug_screenshots"


@pytest.mark.asyncio
async def test_lists_only_that_jobs_shots_in_flow_order(shots, tmp_path, monkeypatch):
    """只列该 job 的,且按**发布流程时序**排(不是文件名字典序)。"""
    _shot(shots, "job132_publish_07_16_waiting_12s_1785659087.png")
    _shot(shots, "job132_publish_07_16_waiting_6s_1785659079.png")
    _shot(shots, "job132_publish_02_04_after_upload_1785658902.png")
    _shot(shots, "job999_publish_07_16_timeout_1785659105.png")  # 别的 job,不该出现

    async with rest_client(tmp_path, monkeypatch) as client:
        r = await client.get("/api/publish-jobs/132/artifacts", headers=bearer(ADMIN_KEY))

    assert r.status_code == 200
    body = r.json()
    assert body["count"] == 3
    names = [f["name"] for f in body["files"]]
    assert all("job132_" in n for n in names)
    # step 升序;同 step 内按时间戳 —— waiting_6s 必须排在 waiting_12s 前面
    assert names[0].startswith("job132_publish_02_")
    assert "waiting_6s" in names[1] and "waiting_12s" in names[2]


@pytest.mark.asyncio
async def test_empty_list_explains_why(shots, tmp_path, monkeypatch):
    """没有现场时要说清是"上线前发的"还是"总开关关着",否则调用方只会以为没留。"""
    async with rest_client(tmp_path, monkeypatch) as client:
        r = await client.get("/api/publish-jobs/404/artifacts", headers=bearer(ADMIN_KEY))

    body = r.json()
    assert body["count"] == 0 and body["files"] == []
    assert "DEBUG_SCREENSHOTS_ENABLED" in body["hint"]


@pytest.mark.asyncio
async def test_downloads_single_shot(shots, tmp_path, monkeypatch):
    name = "job132_publish_07_12_before_publish_1785659065.png"
    _shot(shots, name)

    async with rest_client(tmp_path, monkeypatch) as client:
        r = await client.get(
            f"/api/publish-jobs/132/artifacts/{name}", headers=bearer(ADMIN_KEY)
        )

    assert r.status_code == 200
    assert r.headers["content-type"] == "image/png"
    assert r.content[:4] == b"\x89PNG"


@pytest.mark.asyncio
@pytest.mark.parametrize("name", [
    "../../../etc/passwd",
    "..%2f..%2fetc%2fpasswd",
    "job132_publish_07_12_x_1.png/../../secret.png",
    "notmine.png",
])
async def test_rejects_path_traversal_and_alien_names(shots, name, tmp_path, monkeypatch):
    """只放行本模块自己生成的文件名形态,别的一律 404。"""
    async with rest_client(tmp_path, monkeypatch) as client:
        r = await client.get(
            f"/api/publish-jobs/132/artifacts/{name}", headers=bearer(ADMIN_KEY)
        )
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_cannot_read_another_jobs_shot_through_this_job(shots, tmp_path, monkeypatch):
    """名字合法但不属于该 job → 404(否则 job_id 形同虚设)。"""
    name = "job999_publish_07_16_timeout_1785659105.png"
    _shot(shots, name)

    async with rest_client(tmp_path, monkeypatch) as client:
        r = await client.get(
            f"/api/publish-jobs/132/artifacts/{name}", headers=bearer(ADMIN_KEY)
        )

    assert r.status_code == 404
