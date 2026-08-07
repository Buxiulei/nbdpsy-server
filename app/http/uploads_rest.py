"""图片上传/取图 REST + /upload 上传页。

四个入口(消费 app.services.upload_service 的 save_images/list_batches):
- POST /api/uploads/images(鉴权):收 multipart 图片 → 落盘得图床直链,供发布 image_urls 用。
- GET /uploads/{batch_id}/{name}(白名单免鉴权):按页序取回落盘图片。**防路径穿越**——
  batch_id 只允许 token_urlsafe 字符集、name 只允许 NN.(png|jpg|jpeg|webp) 或
  NN.orig.(同扩展名)(生图去水印前的原图),否则 404;拼 DATA_DIR/uploads/{batch_id}/{name},
  非文件 404;FileResponse(media_type 按扩展名)。
- GET /upload(白名单免鉴权):内联单文件上传页(apikey 输入 + 拖拽/选图 → fetch 上传)。
- GET /api/uploads(鉴权):列自己当前未过期的上传批次。

白名单在 app/auth/middleware.py 里:/upload 精确、/uploads 前缀免鉴权;/api/uploads/*
带 /api 前缀天然走鉴权。GET /uploads/{} 与 /upload 是免鉴权静态页,不进 manifest
(manifest 只列 /api/* 鉴权端点)。
"""

import re
from datetime import UTC, datetime
from pathlib import Path

from fastapi import APIRouter, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel

from app.auth.context import current_operator
from app.core.config import settings
from app.core.db import get_session
from app.core.errors import NotFoundError
from app.services import media_upload
from app.services.upload_service import list_batches, save_images

router = APIRouter()

# 防路径穿越:batch_id 只允许 secrets.token_urlsafe 的字符集;name 只允许页序 NN.ext
# 或生图原图 NN.orig.ext。二者均为单路径段(FastAPI 路径参数不跨 /),叠加正则白名单后
# ../ 类 name 无法匹配 → 404。
# .orig 是**唯一**放行的额外形态(一致性生图的去水印前原图提取通道,见
# services/op_images.py);这是免鉴权路由,白名单本身就是访问控制的一部分,
# 严禁放宽成任意文件名/任意中缀。
_BATCH_RE = re.compile(r"^[A-Za-z0-9_-]+$")
_NAME_RE = re.compile(r"^\d{2}(\.orig)?\.(png|jpe?g|webp)$")

# 落盘扩展名 → 取图响应 content-type。
_MEDIA_TYPES = {
    "png": "image/png",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "webp": "image/webp",
}

MANIFEST_ENTRIES = [
    {
        "method": "POST", "path": "/api/uploads/images",
        "summary": "上传一批图片(1-18 张)得图床直链,供发布 image_urls 用",
        "admin_only": False, "params": {"files": "multipart,file[](字段名 files,可多值)"},
        "returns": "{batch_id, urls:[图片直链], expires_at}",
        "errors": "400=张数越界(须 1-18)/非图片/单张超上限;401=apikey 无效",
        "notes": "multipart/form-data 上传,字段名统一 files;落盘页序即上传顺序(01..NN);"
                 "urls 是可直接用于发布的公网直链,默认 7 天后懒清理过期批次。",
    },
    {
        "method": "GET", "path": "/api/uploads",
        "summary": "列出自己当前未过期的上传批次",
        "admin_only": False, "params": {},
        "returns": "{batches:[{batch_id, file_count, created_at, expires_at}]}",
        "errors": "401=apikey 无效",
        "notes": "按创建时间倒序;只含调用者本人的批次,过期批次已被懒清理不再列出。",
    },
    {
        "method": "POST", "path": "/api/uploads/media-sessions",
        "summary": "开一个大媒体分片上传会话(视频/音频通用),拿 upload_id 与服务端定的 chunk_size",
        "admin_only": False,
        "params": {
            "filename": "body,str(带扩展名;**按扩展名自动归类** video/audio,不用你传 kind)",
            "total_size": "body,int(文件总字节数,用于分片数与完整性校验)",
            "chunk_size": "body,int|None(**建议值,服务端说了算**——会被压到 90MB 以下)",
        },
        "returns": "{upload_id, chunk_size, chunk_count, kind, filename, total_size}",
        "errors": "422=扩展名不在白名单 / total_size 超该 kind 的体积上限;401=apikey 无效",
        "notes": "**为什么必须分片**:本服务的反代是 Cloudflare Tunnel,**单请求体上限 100MB**,"
                 "GB 级文件单发 POST 必死在隧道层且报的是网关错误(查不到我们这儿)。"
                 "所以 chunk 别自己拍脑袋定——用返回的 chunk_size(默认 50MB,硬上限 90MB)。"
                 "视频接受 .mp4/.mov/.flv/.f4v/.mkv/.rm/.rmvb/.m4v/.mpg/.mpeg/.ts,"
                 "音频接受 .m4a/.mp3/.wav/.flac/.aac;体积上限视频/音频各自配置(默认 4GB / 1GB)。"
                 "拿到 upload_id 后按 chunk_size 切片逐片 PUT,最后 POST complete 拿服务器侧路径,"
                 "那个路径可直接当 POST /api/publish-jobs 的 video 参数。"
                 "未完成的会话默认 24 小时后连同碎片清理,别攒着。",
    },
    {
        "method": "PUT", "path": "/api/uploads/media-sessions/{upload_id}/chunks/{index}",
        "summary": "上传第 index 片(**裸二进制请求体**,不是 multipart)",
        "admin_only": False,
        "params": {
            "upload_id": "path,str", "index": "path,int(从 0 开始,< chunk_count)",
            "(body)": "**裸二进制**;非末片长度必须恰好等于 chunk_size,末片可短",
        },
        "returns": "{index, size, chunk_count}",
        "errors": "422=index 越界 / 分片长度不符;403=会话不属于你;404=会话不存在或已过期",
        "notes": "**同 index 重传直接覆盖(幂等)**——网络抖动重发是常态,放心重传。"
                 "分片可**乱序/并发**上传,服务端按 index 拼,不要求顺序。"
                 "长度校验会当场逮住客户端切片逻辑错误,别忽略 422。",
    },
    {
        "method": "POST", "path": "/api/uploads/media-sessions/{upload_id}/complete",
        "summary": "收工:校验分片齐全后拼接,拿服务器侧文件路径",
        "admin_only": False,
        "params": {
            "upload_id": "path,str",
            "sha256": "body,str|None(给了就逐字节校验,强烈建议给)",
        },
        "returns": "{upload_id, path, size, kind, filename, already_completed?}——"
                    "path 直接当 publish 的 video 参数;already_completed:true 表示这次是"
                    "幂等重放,不是重新拼的",
        "errors": "422=分片不齐(报缺哪片)/ 总长与 total_size 不符 / sha256 对不上;"
                  "403=会话不属于你;404=会话不存在或已过期",
        "notes": "三道校验都过才产出成品:分片集合齐全、拼接总长一致、sha256(若给)。"
                 "**任一不过就不产出文件**,不会把半截文件交给你去发布。"
                 "**complete 是幂等的**:重复调用返回同一个 path(带 already_completed:true),"
                 "不报错也不重拼 —— 拿不到响应时放心重试。"
                 "成功后分片碎片**当场**清掉(不等 TTL),只留成品。"
                 "⚠️ 运维提示:拼接那一刻瞬时磁盘占用 ≈ **2× 文件大小**(碎片 + 成品并存),"
                 "传 4GB 视频需保证 DATA_DIR 所在盘有 8GB 以上余量。",
    },
]


class MediaSessionRequest(BaseModel):
    """开分片会话的入参;kind 按 filename 扩展名自动判,不需要调用方传。"""

    filename: str
    total_size: int
    chunk_size: int | None = None


class MediaCompleteRequest(BaseModel):
    """收工入参;sha256 可选但强烈建议给(唯一能逮住静默损坏的手段)。"""

    sha256: str | None = None


@router.post("/api/uploads/images")
async def upload_images(files: list[UploadFile] = File(...)) -> dict:
    """收 multipart 图片 → save_images 落盘 → {batch_id, urls, expires_at}。

    张数越界/非图片/单张超上限由 save_images 抛 ValueError,经 app 级处理器转 400。
    """
    operator = current_operator()
    payload = [(f.filename or "", await f.read()) for f in files]
    async with get_session() as session:
        return await save_images(session, operator, payload, datetime.now(UTC))


@router.get("/api/uploads")
async def list_uploads() -> dict:
    """列出调用者当前未过期的上传批次。"""
    operator = current_operator()
    async with get_session() as session:
        batches = await list_batches(session, operator)
    return {"batches": batches}


@router.post("/api/uploads/media-sessions", status_code=201)
async def create_media_session(payload: MediaSessionRequest) -> dict:
    """开分片上传会话。扩展名/体积不合规 → ValueError,经 app 级处理器转 400→这里显式 422。"""
    operator = current_operator()
    try:
        return media_upload.create_session(
            payload.filename, payload.total_size, operator.id, payload.chunk_size
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.put("/api/uploads/media-sessions/{upload_id}/chunks/{index}")
async def upload_media_chunk(upload_id: str, index: int, request: Request) -> dict:
    """收一个分片(裸二进制体)。

    **流式读进内存再落盘**:单片被 MAX_CHUNK_BYTES(90MB)封顶,这个量级一次性
    持有是安全的;真正不能整读的是**整个文件**(GB 级),而那正是分片要解决的问题。
    """
    operator = current_operator()
    body = await request.body()
    try:
        return media_upload.write_chunk(upload_id, index, body, operator.id)
    except NotFoundError:
        # NotFoundError 继承自 ValueError:不先放行就会被下面的 422 吞掉,
        # 「会话不存在」会伪装成「入参非法」,调用方查错方向。交 app 级处理器转 404。
        raise
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/api/uploads/media-sessions/{upload_id}/complete")
async def complete_media_session(
    upload_id: str, payload: MediaCompleteRequest
) -> dict:
    """校验并拼接分片,返回服务器侧成品路径(可直接当 publish 的 video 参数)。"""
    operator = current_operator()
    try:
        return media_upload.complete_session(upload_id, operator.id, payload.sha256)
    except NotFoundError:
        # NotFoundError 继承自 ValueError:不先放行就会被下面的 422 吞掉,
        # 「会话不存在」会伪装成「入参非法」,调用方查错方向。交 app 级处理器转 404。
        raise
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/uploads/{batch_id}/{name}")
async def serve_upload(batch_id: str, name: str) -> FileResponse:
    """取回落盘图片(白名单免鉴权)。正则白名单挡路径穿越,非文件 404。"""
    # 请求时读 settings.DATA_DIR(而非 import 期绑定),使测试对 DATA_DIR 的 monkeypatch 生效。
    # fullmatch(非 match+$):match+$ 容忍尾随换行("01.png\n" 会通过),虽不构成穿越但
    # 会让 ext 带 "\n" 撞 _MEDIA_TYPES KeyError→500;fullmatch 收成真正的全串白名单。
    if not _BATCH_RE.fullmatch(batch_id) or not _NAME_RE.fullmatch(name):
        raise HTTPException(status_code=404, detail="资源不存在")
    uploads_root = (Path(settings.DATA_DIR) / "uploads").resolve()
    file_path = (uploads_root / batch_id / name).resolve()
    # 纵深防御:正则已结构性排除逃逸字符,这里再确认最终路径确在 uploads 根内(双保险)。
    if not file_path.is_relative_to(uploads_root) or not file_path.is_file():
        raise HTTPException(status_code=404, detail="资源不存在")
    ext = name.rsplit(".", 1)[1].lower()
    return FileResponse(file_path, media_type=_MEDIA_TYPES[ext])


@router.get("/upload")
async def upload_page() -> HTMLResponse:
    """内联单文件上传页(白名单免鉴权):填 apikey → 拖拽/选图 → 上传得直链。"""
    return HTMLResponse(_UPLOAD_PAGE_HTML)


# 内联单文件上传页:apikey(password,默认不落 localStorage)+ 拖拽/选图 → fetch POST
# /api/uploads/images(Authorization: Bearer)→ 展示 batch_id + 直链 + 复制按钮。
# 图标一律内联 SVG,禁 emoji。
_UPLOAD_PAGE_HTML = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>图片上传 · nbdpsy</title>
<style>
  :root {
    --bg: #f7f4ec; --card: #fffdf8; --ink: #2b2622; --muted: #8a8178;
    --line: #e6ddcb; --gold: #c9a24b; --wine: #7a2233; --wine-ink: #fff;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0; background: var(--bg); color: var(--ink);
    font-family: -apple-system, BlinkMacSystemFont, "PingFang SC", "Microsoft YaHei", sans-serif;
    line-height: 1.5; padding: 32px 16px;
  }
  .wrap { max-width: 640px; margin: 0 auto; }
  h1 { font-size: 22px; font-weight: 600; margin: 0 0 4px; }
  .sub { color: var(--muted); font-size: 13px; margin: 0 0 24px; }
  .card {
    background: var(--card); border: 1px solid var(--line); border-radius: 14px;
    padding: 20px; margin-bottom: 16px;
  }
  label { display: block; font-size: 13px; font-weight: 600; margin-bottom: 6px; }
  input[type=password] {
    width: 100%; padding: 10px 12px; border: 1px solid var(--line);
    border-radius: 10px; font-size: 14px; background: #fff; color: var(--ink);
  }
  input[type=password]:focus { outline: none; border-color: var(--gold); }
  .drop {
    margin-top: 16px; border: 2px dashed var(--line); border-radius: 12px;
    padding: 32px 16px; text-align: center; cursor: pointer;
    transition: border-color .15s, background .15s;
  }
  .drop:hover, .drop.over { border-color: var(--gold); background: #fbf7ec; }
  .drop svg { width: 40px; height: 40px; color: var(--gold); }
  .drop p { margin: 10px 0 0; font-size: 14px; color: var(--muted); }
  .hint { font-size: 12px; color: var(--muted); margin-top: 8px; }
  button.btn {
    margin-top: 16px; width: 100%; padding: 12px; border: none; border-radius: 10px;
    background: var(--wine); color: var(--wine-ink); font-size: 15px; font-weight: 600;
    cursor: pointer;
  }
  button.btn:disabled { opacity: .5; cursor: not-allowed; }
  .status { font-size: 13px; margin-top: 12px; min-height: 18px; }
  .status.err { color: var(--wine); }
  .status.ok { color: #2e7d32; }
  .result { margin-top: 16px; display: none; }
  .result.show { display: block; }
  .batch { font-size: 12px; color: var(--muted); margin-bottom: 10px; word-break: break-all; }
  .url-row {
    display: flex; align-items: center; gap: 8px; padding: 8px 10px;
    border: 1px solid var(--line); border-radius: 10px; margin-bottom: 8px; background: #fff;
  }
  .url-row span { flex: 1; font-size: 12px; word-break: break-all; }
  .copy {
    flex: none; display: inline-flex; align-items: center; gap: 4px;
    padding: 5px 10px; border: 1px solid var(--line); border-radius: 8px;
    background: #fff; color: var(--ink); font-size: 12px; cursor: pointer;
  }
  .copy:hover { border-color: var(--gold); }
  .copy svg { width: 14px; height: 14px; }
  .copy-all { margin-top: 4px; }
</style>
</head>
<body>
<div class="wrap">
  <h1>图片上传</h1>
  <p class="sub">填入 apikey,拖拽或点击选择图片(1-18 张),上传后得到可直接用于发布的图片直链。</p>

  <div class="card">
    <label for="apikey">apikey</label>
    <input id="apikey" type="password" placeholder="Bearer apikey(仅本次会话使用,不保存)" autocomplete="off">

    <div id="drop" class="drop" tabindex="0" role="button" aria-label="选择或拖拽图片">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6"
           stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
        <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"></path>
        <polyline points="17 8 12 3 7 8"></polyline>
        <line x1="12" y1="3" x2="12" y2="15"></line>
      </svg>
      <p id="drop-label">点击选择,或将图片拖到此处</p>
    </div>
    <input id="file-input" type="file" accept="image/png,image/jpeg,image/webp" multiple hidden>
    <div class="hint">支持 PNG / JPEG / WebP,单张上限见服务端配置。</div>

    <button id="submit" class="btn" disabled>上传</button>
    <div id="status" class="status"></div>
  </div>

  <div id="result" class="result card">
    <div id="batch" class="batch"></div>
    <div id="urls"></div>
    <button id="copy-all" class="copy copy-all" type="button">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6"
           stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
        <rect x="9" y="9" width="13" height="13" rx="2"></rect>
        <path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"></path>
      </svg>
      复制全部直链
    </button>
  </div>
</div>

<script>
(function () {
  var apikey = document.getElementById('apikey');
  var drop = document.getElementById('drop');
  var fileInput = document.getElementById('file-input');
  var dropLabel = document.getElementById('drop-label');
  var submit = document.getElementById('submit');
  var statusEl = document.getElementById('status');
  var resultEl = document.getElementById('result');
  var batchEl = document.getElementById('batch');
  var urlsEl = document.getElementById('urls');
  var copyAllBtn = document.getElementById('copy-all');
  var picked = [];
  var lastUrls = [];

  var COPY_SVG = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor"'
    + ' stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">'
    + '<rect x="9" y="9" width="13" height="13" rx="2"></rect>'
    + '<path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"></path></svg>';

  function refresh() {
    submit.disabled = picked.length === 0;
    dropLabel.textContent = picked.length
      ? ('已选 ' + picked.length + ' 张,点击可重选')
      : '点击选择,或将图片拖到此处';
  }
  function setStatus(msg, kind) {
    statusEl.textContent = msg || '';
    statusEl.className = 'status' + (kind ? ' ' + kind : '');
  }

  drop.addEventListener('click', function () { fileInput.click(); });
  drop.addEventListener('keydown', function (e) {
    if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); fileInput.click(); }
  });
  fileInput.addEventListener('change', function () {
    picked = Array.prototype.slice.call(fileInput.files);
    refresh();
  });
  ['dragenter', 'dragover'].forEach(function (ev) {
    drop.addEventListener(ev, function (e) { e.preventDefault(); drop.classList.add('over'); });
  });
  ['dragleave', 'drop'].forEach(function (ev) {
    drop.addEventListener(ev, function (e) { e.preventDefault(); drop.classList.remove('over'); });
  });
  drop.addEventListener('drop', function (e) {
    picked = Array.prototype.slice.call(e.dataTransfer.files).filter(function (f) {
      return f.type.indexOf('image/') === 0;
    });
    refresh();
  });

  function copyText(text, btn, label) {
    navigator.clipboard.writeText(text).then(function () {
      var prev = btn.getAttribute('data-label');
      btn.textContent = '已复制';
      setTimeout(function () { btn.innerHTML = COPY_SVG + (label || prev || ''); }, 1200);
    });
  }

  function renderResult(data) {
    lastUrls = data.urls || [];
    batchEl.textContent = 'batch_id: ' + data.batch_id
      + '(过期时间 ' + (data.expires_at || '') + ')';
    urlsEl.innerHTML = '';
    lastUrls.forEach(function (url) {
      var row = document.createElement('div');
      row.className = 'url-row';
      var span = document.createElement('span');
      span.textContent = url;
      var btn = document.createElement('button');
      btn.className = 'copy';
      btn.type = 'button';
      btn.innerHTML = COPY_SVG + '复制';
      btn.addEventListener('click', function () { copyText(url, btn, '复制'); });
      row.appendChild(span);
      row.appendChild(btn);
      urlsEl.appendChild(row);
    });
    resultEl.classList.add('show');
  }

  copyAllBtn.addEventListener('click', function () {
    if (lastUrls.length) { copyText(lastUrls.join('\\n'), copyAllBtn, '复制全部直链'); }
  });

  submit.addEventListener('click', function () {
    var key = apikey.value.trim();
    if (!key) { setStatus('请先填入 apikey', 'err'); return; }
    if (!picked.length) { setStatus('请先选择图片', 'err'); return; }
    var form = new FormData();
    picked.forEach(function (f) { form.append('files', f, f.name); });
    submit.disabled = true;
    setStatus('上传中…');
    fetch('/api/uploads/images', {
      method: 'POST',
      headers: { 'Authorization': 'Bearer ' + key },
      body: form
    }).then(function (resp) {
      return resp.json().then(function (data) { return { ok: resp.ok, data: data }; });
    }).then(function (r) {
      if (!r.ok) {
        setStatus('上传失败:' + (r.data.error || r.data.detail || '未知错误'), 'err');
      } else {
        setStatus('上传成功', 'ok');
        renderResult(r.data);
      }
    }).catch(function (err) {
      setStatus('请求出错:' + err, 'err');
    }).finally(function () {
      submit.disabled = picked.length === 0;
    });
  });

  refresh();
})();
</script>
</body>
</html>
"""
