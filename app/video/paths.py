"""视频管线产物目录约定：{DATA_DIR}/uploads/video/{job_id}-{token}/{raw,tts,out}。

设计意图与不变量：
- 产物落在 DATA_DIR/uploads 下即天然被宿主 /uploads 静态路由暴露（uploads_rest 以
  `DATA_DIR/uploads` 为根、请求时读 settings.DATA_DIR）。故父段用不可猜的 HMAC token——
  攻击者无 SECRET_KEY 无法由 job_id 枚举下载他人成片。M4 会补 GET /uploads/video/... 只读路由。
- token 用 **宿主既有 SECRET_KEY** 派生（与源实现一致，见 security.py：SECRET_KEY 是本仓
  唯一应用密钥，Fernet cookie 加密亦由它派生）。不新增 VIDEO_PATH_SECRET：HMAC-SHA256 不泄露
  密钥，同一密钥服务两个独立 HMAC/Fernet 用途是标准做法，避免 config 膨胀。
- SECRET_KEY 固定 → 同 job_id 每次派生同一 token，全生命周期稳定、跨 stage 目录一致
  （raw/tts/out 共用同一 {job_id}-{token} 父目录），worker 崩溃恢复后仍能定位既有产物。
- 所有路径均**请求时**读 settings.DATA_DIR（不在 import 期绑定），使测试对 DATA_DIR 的
  monkeypatch 生效，与 uploads_rest 同款惯例。
"""

import hashlib
import hmac
from pathlib import Path

from app.core.config import settings


def _uploads_root() -> Path:
    """宿主 /uploads 静态根：DATA_DIR/uploads（与 uploads_rest 同源，to_public_url 据此算相对路径）。"""
    return (Path(settings.DATA_DIR) / "uploads").resolve()


def _base() -> Path:
    """视频产物根：DATA_DIR/uploads/video。"""
    return _uploads_root() / "video"


def _job_token(job_id: int) -> str:
    """SECRET_KEY 派生的不可猜 token（HMAC-SHA256 取前 16 位十六进制），同 job_id 恒定。"""
    return hmac.new(
        settings.SECRET_KEY.encode(), str(job_id).encode(), hashlib.sha256
    ).hexdigest()[:16]


def _job_root(job_id: int) -> Path:
    """同一 job 的 raw/tts/out 共用的 {job_id}-{token} 父目录。"""
    return _base() / f"{job_id}-{_job_token(job_id)}"


def job_dir(job_id: int) -> Path:
    """确保并返回 job 父目录（递归建）。"""
    d = _job_root(job_id)
    d.mkdir(parents=True, exist_ok=True)
    return d


def raw_dir(job_id: int) -> Path:
    """原始素材/中间产物目录（下载、转写 JSON 等）。"""
    d = job_dir(job_id) / "raw"
    d.mkdir(exist_ok=True)
    return d


def tts_dir(job_id: int) -> Path:
    """配音音频目录（逐句 wav 片段）。"""
    d = job_dir(job_id) / "tts"
    d.mkdir(exist_ok=True)
    return d


def out_dir(job_id: int) -> Path:
    """成片输出目录（最终 mp4、字幕文件）。"""
    d = job_dir(job_id) / "out"
    d.mkdir(exist_ok=True)
    return d


def to_public_url(path: Path) -> str:
    """产物绝对路径 → 宿主可访问的 /uploads/video/... 相对 URL（相对 DATA_DIR/uploads）。"""
    rel = Path(path).resolve().relative_to(_uploads_root())
    return f"/uploads/{rel.as_posix()}"


def to_absolute_url(path: Path) -> str:
    """产物绝对路径 → 完整外链（PUBLIC_BASE_URL + 相对 URL），供跨系统直链下载。"""
    return settings.PUBLIC_BASE_URL.rstrip("/") + to_public_url(path)
