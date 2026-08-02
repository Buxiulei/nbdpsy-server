"""发布失败现场取回:按 job 列出/下载那次发布的全流程截图。

**为什么要有它**(2026-08-02 事故的直接教训):截图其实**一直在存**,发布全流程逐步落盘
——点发布前、点击后逐秒、等待期每 6 秒、超时那一刻,一张不落。但此前没有任何东西把它
和某次发布关联起来,于是运营侧拿到的全部信息只有一句「发布超时(30秒),未检测到成功标志」,
只能靠换外部变量试错(实际试了三轮:图片体积 8.8MB→2.3MB、带不带活动、正文压到 840 字,
全是徒劳——没有一个变量与真正的原因有关)。

而那次的根因,是**看一眼 `publish_07_12_before_publish` 那张图就能看出来的**:
引用弹窗大剌剌盖在页面中间,发布按钮被它挡着。

所以本模块不产生任何新数据,只是把已有的现场**接出来**:
``_take_screenshot`` 给文件名打上 ``job{id}_`` 前缀,这里按前缀 glob。
**不需要为此加数据库列**。

鉴权与其余 REST 一致(``/api/`` 前缀走统一鉴权);文件下发照 ``uploads_rest`` 的写法,
正则白名单挡路径穿越 + 最终路径归属复核双保险。
"""

import re
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

from app.core.config import settings

router = APIRouter()

# 截图文件名白名单:只放行本模块自己生成的形态,别的一律 404
_NAME_RE = re.compile(r"job\d+_publish_[0-9A-Za-z_]+\.png")
# step 段:文件名形如 job{id}_publish_{step:02d}_{name}_{ts}.png
_PARSE_RE = re.compile(r"job(?P<job>\d+)_publish_(?P<step>\d+)_(?P<name>.+)_(?P<ts>\d+)\.png")

MANIFEST_ENTRIES = [
    {
        "method": "GET",
        "path": "/api/publish-jobs/{job_id}/artifacts",
        "summary": "取该次发布的失败现场截图清单(按步骤时序)",
        "admin_only": False,
        "params": {"job_id": "path,int(发布任务 id)"},
        "returns": "{job_id, count, files:[{name, step, stage, ts, size, url}], hint}",
        "notes": "发布报「超时/未检测到成功标志」时先看这里,不要靠换图片体积/活动/字数试错。"
                 "返回 files[].url 可直接下载。截图受 DEBUG_SCREENSHOTS_ENABLED 总开关约束,"
                 "关掉则为空。",
    },
    {
        "method": "GET",
        "path": "/api/publish-jobs/{job_id}/artifacts/{name}",
        "summary": "下载其中一张截图",
        "admin_only": False,
        "params": {"job_id": "path,int", "name": "path,str(清单里的 files[].name)"},
        "returns": "image/png 文件流",
    },
]


def _root() -> Path:
    """截图根目录(请求时读 settings,使测试对 DATA_DIR 的 monkeypatch 生效)。"""
    return (Path(settings.DATA_DIR) / "debug_screenshots").resolve()


@router.get("/api/publish-jobs/{job_id}/artifacts")
async def list_publish_artifacts(job_id: int) -> dict:
    """列出该 job 的截图,**按步骤 + 时间戳排序**(= 发布流程的真实时序)。

    按文件名排序是不行的:``publish_07_16_waiting_6s`` 会排在 ``waiting_12s`` 后面
    (字符串比较 "12" < "6"),而排查最要紧的就是"卡在哪一步、之后页面怎么变的"。
    """
    root = _root()
    files = []
    if root.is_dir():
        for path in root.glob(f"job{job_id}_publish_*.png"):
            m = _PARSE_RE.fullmatch(path.name)
            if m is None:
                continue
            files.append({
                "name": path.name,
                "step": int(m.group("step")),
                "stage": m.group("name"),
                "ts": int(m.group("ts")),
                "size": path.stat().st_size,
                "url": f"/api/publish-jobs/{job_id}/artifacts/{path.name}",
            })
    files.sort(key=lambda f: (f["step"], f["ts"], f["name"]))
    return {
        "job_id": job_id,
        "count": len(files),
        "files": files,
        # 空结果有两种完全不同的原因,不点明的话调用方只会以为"这次没留现场"
        "hint": (
            "空清单 = 该 job 没留截图:要么发布发生在本功能上线前,"
            "要么 DEBUG_SCREENSHOTS_ENABLED 关着。"
            if not files else
            "按 step 升序即发布流程时序;12_before_publish 是点发布前那一刻,"
            "16_timeout 是超时那一刻。"
        ),
    }


@router.get("/api/publish-jobs/{job_id}/artifacts/{name}")
async def serve_publish_artifact(job_id: int, name: str) -> FileResponse:
    """下发单张截图。正则白名单挡路径穿越,最终路径归属再复核一次(双保险)。"""
    if not _NAME_RE.fullmatch(name) or not name.startswith(f"job{job_id}_"):
        raise HTTPException(status_code=404, detail="资源不存在")
    root = _root()
    file_path = (root / name).resolve()
    if not file_path.is_relative_to(root) or not file_path.is_file():
        raise HTTPException(status_code=404, detail="资源不存在")
    return FileResponse(file_path, media_type="image/png")
