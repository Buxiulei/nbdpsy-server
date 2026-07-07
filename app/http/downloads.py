"""GET /downloads/extension.zip —— chrome 插件包下载端点。

路径落在中间件白名单 /downloads 前缀 → 无需 apikey 即可下载(带/不带都放行),
便于把"装好即用"的插件包直接递给操作者。zip 由 scripts/pack_extension.sh 预先打到
DATA_DIR/extension.zip;未打包则 404 提示先跑打包脚本(不即时打包,保持端点纯读)。
"""

from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

from app.core.config import settings

router = APIRouter()


@router.get("/downloads/extension.zip")
async def download_extension() -> FileResponse:
    """返回 DATA_DIR/extension.zip;未打包 → 404 引导先跑 scripts/pack_extension.sh。"""
    # 请求时读 settings.DATA_DIR(而非 import 期绑定),使测试对 DATA_DIR 的 monkeypatch 生效。
    zip_path = Path(settings.DATA_DIR) / "extension.zip"
    if not zip_path.is_file():
        raise HTTPException(
            status_code=404,
            detail="插件包尚未生成,请先运行 scripts/pack_extension.sh 打包",
        )
    return FileResponse(
        zip_path,
        media_type="application/zip",
        filename="nbdpsy-extension.zip",
    )
