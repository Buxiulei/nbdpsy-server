"""图片上传/取图 REST + /upload 上传页。

四个入口(消费 app.services.upload_service 的 save_images/list_batches):
- POST /api/uploads/images(鉴权):收 multipart 图片 → 落盘得图床直链,供发布 image_urls 用。
- GET /uploads/{batch_id}/{name}(白名单免鉴权):按页序取回落盘图片。**防路径穿越**——
  batch_id 只允许 token_urlsafe 字符集、name 只允许 NN.(png|jpg|jpeg|webp),否则 404;
  拼 DATA_DIR/uploads/{batch_id}/{name},非文件 404;FileResponse(media_type 按扩展名)。
- GET /upload(白名单免鉴权):内联单文件上传页(apikey 输入 + 拖拽/选图 → fetch 上传)。
- GET /api/uploads(鉴权):列自己当前未过期的上传批次。

白名单在 app/auth/middleware.py 里:/upload 精确、/uploads 前缀免鉴权;/api/uploads/*
带 /api 前缀天然走鉴权。GET /uploads/{} 与 /upload 是免鉴权静态页,不进 manifest
(manifest 只列 /api/* 鉴权端点)。
"""

import re
from datetime import UTC, datetime
from pathlib import Path

from fastapi import APIRouter, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse

from app.auth.context import current_operator
from app.core.config import settings
from app.core.db import get_session
from app.services.upload_service import list_batches, save_images

router = APIRouter()

# 防路径穿越:batch_id 只允许 secrets.token_urlsafe 的字符集;name 只允许页序 NN.ext。
# 二者均为单路径段(FastAPI 路径参数不跨 /),叠加正则白名单后 ../ 类 name 无法匹配 → 404。
_BATCH_RE = re.compile(r"^[A-Za-z0-9_-]+$")
_NAME_RE = re.compile(r"^\d{2}\.(png|jpe?g|webp)$")

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
]


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
