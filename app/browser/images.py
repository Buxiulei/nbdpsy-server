"""图片物料化:把 http URL / base64 图片落成本地文件,供 Playwright ``set_input_files`` 上传。

移植参考旧仓 ``publish_service`` 链路里的 ``localize_external_image``(op_create_media.py,
http 外链下载到本地缓存)。新仓收敛为单一入口 ``materialize_images``:接受一个混合列表
(每项是 http URL 或 base64),逐项解码/下载落盘到 ``workdir``,**保持顺序**返回本地
``Path`` 列表。供 P3.5 发布队列在起浏览器前把外链/内联图统一转成本地文件。

支持的三种项形式:
- ``{"b64": "<base64>", "ext": "png"}``  —— dict 携带 base64 与扩展名
- ``"data:image/png;base64,<...>"``      —— data URI 字符串(从 mediatype 推扩展名)
- ``"http(s)://..."``                    —— http URL(httpx 下载,扩展名从 URL 末段或 Content-Type 推断)

无法识别的项直接抛 ``ValueError`` —— 宁可失败也不静默丢图打乱顺序(下游按 index 对齐封面/正文)。
"""
import base64
import re
from pathlib import Path
from typing import Union

import httpx
from loguru import logger

# Content-Type / data URI mediatype → 文件扩展名
_CT_TO_EXT = {
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/gif": ".gif",
    "image/bmp": ".bmp",
}
# 已知图片扩展名(小写,含点)
_KNOWN_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"}
# data URI 头部:data:<mediatype>[;base64],<data>
_DATA_URI_RE = re.compile(r"^data:(?P<mt>[^;,]*)(?P<b64>;base64)?,(?P<data>.*)$", re.DOTALL)


def _norm_ext(ext: str) -> str:
    """把扩展名规整为 ``.xxx`` 小写形式;未知/空 → 兜底 ``.jpg``。"""
    if not ext:
        return ".jpg"
    ext = ext.lower().strip()
    if not ext.startswith("."):
        ext = "." + ext
    return ext if ext in _KNOWN_EXTS else ".jpg"


def _write(data: bytes, ext: str, workdir: Path, index: int) -> Path:
    """把字节写入 ``workdir/img_{index:02d}{ext}`` 并返回路径。"""
    target = workdir / f"img_{index:02d}{ext}"
    target.write_bytes(data)
    logger.info(f"[images] 物料化[{index}] → {target} ({len(data)} bytes)")
    return target


def _decode_b64(b64_str: str) -> bytes:
    """解码 base64 字符串(容错换行/空白;缺省补齐 padding)。"""
    s = "".join((b64_str or "").split())
    # base64 长度须为 4 的倍数,不足补 '='
    pad = (-len(s)) % 4
    if pad:
        s += "=" * pad
    return base64.b64decode(s)


def _materialize_base64_dict(item: dict, workdir: Path, index: int) -> Path:
    """``{b64, ext}`` → 解码落盘。"""
    data = _decode_b64(item.get("b64", ""))
    return _write(data, _norm_ext(item.get("ext", "")), workdir, index)


def _materialize_data_uri(uri: str, workdir: Path, index: int) -> Path:
    """``data:image/png;base64,...`` → 从 mediatype 推扩展名解码落盘。"""
    m = _DATA_URI_RE.match(uri)
    if not m:
        raise ValueError(f"非法 data URI[{index}]: {uri[:40]}...")
    mediatype = (m.group("mt") or "").strip().lower()
    ext = _CT_TO_EXT.get(mediatype, ".jpg")
    raw = m.group("data")
    data = _decode_b64(raw) if m.group("b64") else raw.encode("utf-8")
    return _write(data, ext, workdir, index)


def _materialize_http(url: str, workdir: Path, index: int) -> Path:
    """http URL → httpx 下载,扩展名从 URL 末段优先,其次 Content-Type。"""
    resp = httpx.get(url, timeout=30.0, follow_redirects=True)
    resp.raise_for_status()
    data = resp.content
    if not data:
        raise ValueError(f"URL 下载返回空 body[{index}]: {url[:60]}")

    # 扩展名:URL 末段 > Content-Type > .jpg 兜底
    url_path = url.split("?", 1)[0]
    ext = Path(url_path).suffix.lower()
    if ext not in _KNOWN_EXTS:
        ct = (resp.headers.get("content-type") or "").lower().split(";")[0].strip()
        ext = _CT_TO_EXT.get(ct, ".jpg")
    return _write(data, ext, workdir, index)


def materialize_images(images: list, workdir: Union[str, Path]) -> list:
    """把混合图片列表逐项落盘到 ``workdir``,保持顺序返回本地 ``Path`` 列表。

    Args:
        images: 每项为 http URL(str)、data URI(str)或 ``{"b64", "ext"}`` dict。
        workdir: 落盘目录(不存在则创建)。

    Returns:
        与输入等长、顺序一致的本地文件 ``Path`` 列表。

    Raises:
        ValueError: 遇到无法识别的项(既非 http 也非 base64)。
    """
    workdir = Path(workdir)
    workdir.mkdir(parents=True, exist_ok=True)

    paths = []
    for index, item in enumerate(images):
        if isinstance(item, dict):
            paths.append(_materialize_base64_dict(item, workdir, index))
        elif isinstance(item, str) and item.startswith("data:"):
            paths.append(_materialize_data_uri(item, workdir, index))
        elif isinstance(item, str) and (item.startswith("http://") or item.startswith("https://")):
            paths.append(_materialize_http(item, workdir, index))
        else:
            raise ValueError(
                f"无法识别的图片项[{index}]: {type(item).__name__} {str(item)[:40]}"
            )
    return paths
