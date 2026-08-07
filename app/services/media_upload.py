"""大媒体分片上传服务层(视频 / 音频通用):建 session → 收分片 → 校验拼接。

**为什么必须分片**:mcp.nbdpsy.com 走 Cloudflare Tunnel,单请求体上限 **100MB**;
而用户要传 15-30 分钟的 GB 级视频与 ≤1GB 的播客音频 —— 单发 POST 必死在隧道层,
而且报的是网关错误,查不到我们这儿。所以 chunk_size 由**服务端**定并压在
``MAX_CHUNK_BYTES``(90MB,留隧道余量)以下,客户端说了不算。

**为什么每片独立落盘而不是往稀疏文件里按 offset 写**:稀疏写省一次拷贝,但写完最后一片
文件就已经是 total_size 长,``complete`` 时**空洞验不出来** —— 缺片要等发布那一刻才炸。
每片独立成 ``{index:06d}.part`` 则:同 index 重传天然覆盖(幂等)、零并发写竞争、
``complete`` 能按"分片集合是否齐全 + 每片长度对不对"精确逮住缺片。多一次拷贝换这个,值。

**为什么不建 DB 表**:session 元数据创建时写一次 JSON 之后只读(分片写入不碰它),
没有可变共享状态,也就没有并发写竞争;TTL 清理走目录 mtime 扫描。为它加一张表和一次
迁移,是为不存在的问题增加实体。

目录布局:``DATA_DIR/uploads/media/{upload_id}/`` 下 ``meta.json`` + ``NNNNNN.part``,
``complete`` 后同目录产出真实文件名的成品,其绝对路径即 publish 的 ``video`` 参数。
"""

import hashlib
import json
import secrets
import shutil
import time
from pathlib import Path
from typing import Any, Dict, Optional

from loguru import logger

from app.core.config import settings
from app.core.errors import NotFoundError
from app.publish.policy import XHS_AUDIO_EXTENSIONS, XHS_VIDEO_EXTENSIONS

# 播客音频扩展名(与视频白名单并列;平台侧上限 1GB,配置在 UPLOAD_AUDIO_MAX_MB)。
# 别名到 policy 里那份**唯一**定义:发布入口(audio_reject)与上传通道必须认同一套
# 白名单,各写一份迟早漂移成"传得上去但发不出来"。
AUDIO_EXTENSIONS = XHS_AUDIO_EXTENSIONS

# 单分片硬上限:Cloudflare Tunnel 单请求体 100MB,留 10MB 余量给请求头/编码开销。
# 客户端传再大的 chunk_size 也压到这条线以下 —— 越线不是"慢一点",是隧道层直接砍断。
MAX_CHUNK_BYTES = 90 * 1024 * 1024

# 分片文件名:定长零填充,保证目录序 = 分片序
_PART_FMT = "{:06d}.part"
_META_NAME = "meta.json"


def _media_root() -> Path:
    """分片 session 根目录(每次读 settings,便于测试 monkeypatch DATA_DIR)。"""
    return Path(settings.DATA_DIR) / "uploads" / "media"


def session_dir(upload_id: str) -> Path:
    """某个 session 的目录。``upload_id`` 由 token_urlsafe 生成,调用侧已白名单校验。"""
    return _media_root() / upload_id


def classify_media_kind(filename: str) -> Optional[str]:
    """按扩展名归类 ``video`` / ``audio``;不在任何白名单里返回 ``None``。

    调用方不必传 kind —— 扩展名已经说明一切,多一个入参就多一处能对不上的地方。
    """
    lowered = (filename or "").lower()
    if any(lowered.endswith(ext) for ext in XHS_VIDEO_EXTENSIONS):
        return "video"
    if any(lowered.endswith(ext) for ext in AUDIO_EXTENSIONS):
        return "audio"
    return None


def _size_cap_bytes(kind: str) -> int:
    """该 kind 的体积上限(字节);配置为 0 表示不限。"""
    mb = (
        settings.UPLOAD_AUDIO_MAX_MB if kind == "audio" else settings.UPLOAD_MEDIA_MAX_MB
    )
    return int(mb) * 1024 * 1024


def chunk_count(total_size: int, chunk_size: int) -> int:
    """分片总数(向上取整);``total_size`` 为 0 时算 1 片(允许空文件走完流程)。"""
    if total_size <= 0:
        return 1
    return (total_size + chunk_size - 1) // chunk_size


def _default_chunk_bytes() -> int:
    return min(int(settings.UPLOAD_CHUNK_MB) * 1024 * 1024, MAX_CHUNK_BYTES)


def create_session(
    filename: str,
    total_size: int,
    operator_id: int,
    chunk_size: Optional[int] = None,
) -> Dict[str, Any]:
    """建一个分片上传 session,返回 ``{upload_id, chunk_size, chunk_count, kind, ...}``。

    扩展名不认、体积超上限一律当场 ``ValueError`` —— 别让人传三个小时才说不行。
    ``chunk_size`` 客户端可建议,但**服务端说了算**:压到 ``MAX_CHUNK_BYTES`` 以下。
    """
    kind = classify_media_kind(filename)
    if kind is None:
        raise ValueError(
            f"文件格式不支持:{filename};视频接受 {'/'.join(XHS_VIDEO_EXTENSIONS)},"
            f"音频接受 {'/'.join(AUDIO_EXTENSIONS)}"
        )
    if total_size < 0:
        raise ValueError("total_size 不能为负")
    cap = _size_cap_bytes(kind)
    if cap and total_size > cap:
        raise ValueError(
            f"文件超出 {kind} 体积上限:{total_size} 字节 > {cap} 字节"
            f"({cap // 1024 // 1024}MB)"
        )
    size = chunk_size or _default_chunk_bytes()
    size = max(1, min(int(size), MAX_CHUNK_BYTES))

    # 懒清理:每次开新会话顺手扫一遍过期弃单。与同族的 upload_service.save_images
    # 一个路子 —— 分片只在有人上传时才产生,清理跟着它走天然对齐,不必为它多养一个
    # 后台循环(也就不必多一个 interval 配置)。清理失败绝不阻断开会话。
    try:
        sweep_expired_sessions()
    except Exception:  # noqa: BLE001 — 卫生工作绝不挡住正事
        logger.warning("[media_upload] 懒清理过期会话异常(忽略)")

    upload_id = secrets.token_urlsafe(16)
    directory = session_dir(upload_id)
    directory.mkdir(parents=True, exist_ok=True)
    meta = {
        "upload_id": upload_id,
        "filename": Path(filename).name,  # 只留文件名,杜绝调用方拿路径穿越
        "total_size": int(total_size),
        "chunk_size": size,
        "chunk_count": chunk_count(int(total_size), size),
        "kind": kind,
        "operator_id": int(operator_id),
        "created_at": time.time(),
    }
    (directory / _META_NAME).write_text(
        json.dumps(meta, ensure_ascii=False), encoding="utf-8"
    )
    logger.info(
        f"[media_upload] 建 session {upload_id} kind={kind} "
        f"total={total_size} chunk={size} count={meta['chunk_count']}"
    )
    return meta


def _load_meta(upload_id: str, operator_id: int) -> Dict[str, Any]:
    """读 session 元数据并校验归属;不存在 → NotFoundError,不是你的 → PermissionError。"""
    meta_path = session_dir(upload_id) / _META_NAME
    if not meta_path.is_file():
        raise NotFoundError(f"上传会话 {upload_id} 不存在(可能已完成或已过期清理)")
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    if int(meta.get("operator_id", -1)) != int(operator_id):
        raise PermissionError(f"上传会话 {upload_id} 不属于当前调用方")
    return meta


def write_chunk(
    upload_id: str, index: int, data: bytes, operator_id: int
) -> Dict[str, Any]:
    """落一个分片(同 index 重传直接覆盖 = 幂等)。

    长度校验:非末片必须**恰好** ``chunk_size``,末片可以短。长度对不上多半是客户端
    切片逻辑错了,当场拒绝比拼出一个错文件强得多。
    """
    meta = _load_meta(upload_id, operator_id)
    total = int(meta["chunk_count"])
    if index < 0 or index >= total:
        raise ValueError(f"分片 index 越界:{index},本次共 {total} 片(0..{total - 1})")
    chunk_size = int(meta["chunk_size"])
    is_last = index == total - 1
    if not is_last and len(data) != chunk_size:
        raise ValueError(
            f"分片长度不符:第 {index} 片收到 {len(data)} 字节,应为 {chunk_size} 字节"
        )
    if is_last and len(data) > chunk_size:
        raise ValueError(
            f"分片长度不符:末片收到 {len(data)} 字节,超过 chunk_size {chunk_size}"
        )
    part = session_dir(upload_id) / _PART_FMT.format(index)
    # 先写临时文件再原子 rename:重传与读取并发时不会读到写了一半的片
    tmp = part.with_suffix(".part.tmp")
    tmp.write_bytes(data)
    tmp.replace(part)
    return {"index": index, "size": len(data), "chunk_count": total}


def complete_session(
    upload_id: str, operator_id: int, sha256: Optional[str] = None
) -> Dict[str, Any]:
    """校验分片齐全后按序拼接,返回 ``{path, size, kind, filename}``。

    校验三件事,任一不过就不产出成品(**绝不把半截文件交出去让发布链路去炸**):
    ① 分片集合齐全(缺哪片就报哪片);② 拼出来的总长与 ``total_size`` 一致;
    ③ 给了 ``sha256`` 就逐字节校验和。
    """
    meta = _load_meta(upload_id, operator_id)
    directory = session_dir(upload_id)
    total = int(meta["chunk_count"])

    # 幂等:成品已在就直接返回同一个 path。这不是顺手加的健壮性 —— 网络抖动下调用方
    # **必然**会重试 complete(拿不到响应时它无法区分"没执行"和"执行了但响应丢了"),
    # 而 concat 成功后分片碎片当场就清了,照旧走校验会把一次成功的上传报成"分片不齐"。
    # 归属校验在 _load_meta 里已经做过,幂等不放宽访问控制。
    done = directory / meta["filename"]
    if done.is_file():
        return {
            "upload_id": upload_id,
            "path": str(done),
            "size": done.stat().st_size,
            "kind": meta["kind"],
            "filename": meta["filename"],
            "already_completed": True,
        }

    missing = [i for i in range(total) if not (directory / _PART_FMT.format(i)).is_file()]
    if missing:
        raise ValueError(
            f"分片不齐,缺第 {missing[:20]} 片(共 {total} 片);"
            f"请补传缺失分片后再调 complete"
        )

    target = directory / meta["filename"]
    digest = hashlib.sha256() if sha256 else None
    written = 0
    with open(target, "wb") as out:
        for i in range(total):
            part = directory / _PART_FMT.format(i)
            with open(part, "rb") as src:
                while True:
                    buf = src.read(1024 * 1024)
                    if not buf:
                        break
                    out.write(buf)
                    written += len(buf)
                    if digest is not None:
                        digest.update(buf)

    expected = int(meta["total_size"])
    if expected and written != expected:
        target.unlink(missing_ok=True)
        raise ValueError(f"拼接后总长 {written} 字节,与声明的 total_size {expected} 不符")
    if digest is not None and digest.hexdigest() != sha256.lower():
        target.unlink(missing_ok=True)
        raise ValueError(
            f"sha256 校验失败:实际 {digest.hexdigest()},声明 {sha256.lower()}"
        )

    # 成品已产出,分片碎片没用了,当场清掉(省一倍磁盘)
    for i in range(total):
        (directory / _PART_FMT.format(i)).unlink(missing_ok=True)
    logger.info(f"[media_upload] session {upload_id} 完成,成品 {target}({written} 字节)")
    return {
        "upload_id": upload_id,
        "path": str(target),
        "size": written,
        "kind": meta["kind"],
        "filename": meta["filename"],
    }


def sweep_expired_sessions(ttl_hours: Optional[int] = None) -> int:
    """删掉超过 TTL 的 session 目录,返回删了几个;**绝不抛异常**。

    未完成的分片是纯垃圾,只增不减会把磁盘吃满(截图目录已经吃过一次这个亏)。
    按目录 mtime 判定:写分片会刷新 mtime,所以"还在传"的 session 不会被误删。
    """
    hours = ttl_hours if ttl_hours is not None else settings.UPLOAD_SESSION_TTL_HOURS
    if hours <= 0:
        return 0
    root = _media_root()
    if not root.is_dir():
        return 0
    cutoff = time.time() - hours * 3600
    removed = 0
    for directory in root.iterdir():
        try:
            if not directory.is_dir() or directory.stat().st_mtime >= cutoff:
                continue
            shutil.rmtree(directory, ignore_errors=True)
            removed += 1
        except OSError:
            continue  # 单个目录删不掉就跳过,不影响其余
    if removed:
        logger.info(f"[media_upload] 清理过期上传会话 {removed} 个(TTL {hours}h)")
    return removed
