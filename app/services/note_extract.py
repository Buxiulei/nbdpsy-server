"""他人笔记内容提取(纯 HTTP 主路):分享链接 → 结构化正文 / 图片 / 互动数据。

需求见 ``/home/roots/NBDpsy/docs/2026-08-07-小红书笔记内容提取能力-需求.md``:运营做对标
拆解时发来一条分享链接,要把这篇的正文、图片、评论、互动数据完整取回来。

**为什么主路是纯 HTTP 而不是浏览器**(2026-08-07 取证实证):小红书详情页是 SSR 的
——正文、话题、图片清单、互动数(赞藏评分享)、作者、发布时间**全在**
``window.__INITIAL_STATE__`` 里,一次 GET 就够,零会话成本。运营原报告说"互动数据拿不到"
是漏看了 ``interactInfo``。只有**评论**真的不在里面(``comments.list`` 是空数组 +
``firstRequestFinish:false``,纯客户端异步拉),那条走浏览器会话,见
``app.browser.note_comments_read``。

取证结论里三条与运营原始踩坑报告**不一致**的地方,以取证为准(理由写在对应代码处):

1. 图片必须**原样用 ``urlDefault`` 自带的后缀**请求,而不是硬编码 ``!nd_dft_wlteh_jpg_3``
   —— 取证那次的实际后缀是 webp 变体,换成 jpg 那个 6 张全 403(域名后第二段是覆盖变体
   参数的签名 hash,换后缀签名就对不上)。**本实现落地后又实拉了一次同一条链接:这次
   ``urlDefault`` 自带的是 ``!nd_dft_wlteh_jpg_3``(下回来的字节魔数 ffd8 确是 JPEG)**
   —— 同一篇笔记两次抓到的变体不同,正好证明运营报告的 jpg 与取证报告的 webp 都是
   "那一次的字面值",两个都不能写死。规则只有一条:**用页面给的那个串**;
2. Referer 当天不是必要条件(有无都 200),但仍照发 —— 零成本,防未来行为差异;
3. 互动数据纯 HTTP 就能拿全。

**两条图片链接规则是互补不是竞争**(本仓 ``note_media`` 的"剥成裸永久链"规则同样适用于
他人笔记):签名 URL 管"怎么把字节拿到手"(18 天过期,只适合当场下载),永久链
``sns-img-qc/{段}/{file_id}`` 管"怎么给这批字节一个未来还能验证/去重的标识"且是原图。
两个都返回,图床里存的是下载到的字节。
"""

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import parse_qs, urlparse

from loguru import logger

from app.core.config import settings

# 分享短链域名(运营从 App 分享按钮拿到的那种)。
SHARE_HOSTS = ("xhslink.cn", "xhslink.com")

# CDN 防盗链头:取证当天非必要,但零成本保留(见模块 docstring 第 2 条)。
CDN_HEADERS = {
    "Referer": "https://www.xiaohongshu.com/",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
}
# 拉详情页用的头(比 CDN 那套多一个 Accept,少了也能拿到,给全更像真浏览器)。
PAGE_HEADERS = {**CDN_HEADERS, "Accept": "text/html,application/xhtml+xml"}

# 永久图链前缀:``{host}/{fileId}``,fileId 形如 ``notes_pre_post/1040g...``。
PERMANENT_HOST = "https://sns-img-qc.xhscdn.com"

# 缓存 TTL:运营明确要 24h(对标拆解经常反复看同一篇)。
CACHE_TTL_SECONDS = 24 * 3600

# 北京时间:published_at 给运营看,用 +08:00 显式偏移(不是本地时间靠猜)。
_CST = timezone(timedelta(hours=8))

# 从 URL 路径里抠 note_id:24 位十六进制串,兼容 /explore/ 与 /discovery/item/ 两种路径。
_NOTE_ID_RE = re.compile(r"/(?:explore|discovery/item)/([0-9a-fA-F]{16,32})")

# 页面里的状态注入:``<script>window.__INITIAL_STATE__={...}</script>``。
_STATE_RE = re.compile(r"window\.__INITIAL_STATE__\s*=\s*(\{.*?\})\s*</script>", re.S)

# 平台 type → 运营要的中文类型。不在表里的类型**不猜**(见 build_payload)。
_TYPE_CN = {"normal": "图文", "video": "视频"}


class NoteExtractError(ValueError):
    """提取失败(链接不认识 / 页面结构变了 / 笔记不存在)。``reason`` 携失败语义。

    继承 ``ValueError`` 是为了直接落进本仓既有的错误契约:``app/server.py`` 的
    ValueError handler 把它转成 ``400 {"error": ...}``,端点层不必再包一层 try/except。
    """

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class NoteRef:
    """一条链接解析出来的定位信息。``note_attributes`` 是 is_goods_note 的唯一判据。"""

    note_id: str
    final_url: str
    xsec_token: str | None = None
    xsec_source: str | None = None
    note_attributes: tuple[str, ...] = field(default_factory=tuple)


# ---------------- 链接 ----------------


def is_share_link(url: str) -> bool:
    """是不是 xhslink 短链(需要先跟 302 才能拿到 note_id)。"""
    host = (urlparse(url).hostname or "").lower()
    return any(host == h or host.endswith("." + h) for h in SHARE_HOSTS)


async def resolve_share_link(url: str, client) -> str:
    """跟随重定向拿最终笔记 URL。``client`` 是 httpx.AsyncClient(便于测试注入假件)。

    取证:短链是**单跳 302** 直达最终页,且 ``xsec_token`` 就在最终 URL 上 —— 丢了它
    页面取不到内容,所以这里返回的是**完整最终 URL**,调用方不得自行精简。
    """
    resp = await client.get(url, follow_redirects=True, headers=PAGE_HEADERS, timeout=20.0)
    if resp.status_code >= 400:
        raise NoteExtractError(f"短链跟跳失败:HTTP {resp.status_code}(链接可能已失效)")
    return str(resp.url)


def parse_note_ref(url: str) -> NoteRef:
    """从完整笔记 URL 解析 note_id / xsec_token / noteAttributes。

    ``noteAttributes=goods`` 是商品笔记的**唯一**可用判据(取证:页面内无任何结构化
    信号),所以这里原样收下整串,判定与来源标注都交给 ``build_payload``。
    """
    match = _NOTE_ID_RE.search(urlparse(url).path)
    if not match:
        raise NoteExtractError(
            "链接里找不到 note_id:请用小红书分享按钮生成的原始链接(短链或完整笔记链接),"
            "不要手工精简 URL"
        )
    query = parse_qs(urlparse(url).query)
    attrs = tuple(
        a for raw in query.get("noteAttributes", []) for a in raw.split(",") if a
    )
    return NoteRef(
        note_id=match.group(1),
        final_url=url,
        xsec_token=(query.get("xsec_token") or [None])[0],
        xsec_source=(query.get("xsec_source") or [None])[0],
        note_attributes=attrs,
    )


# ---------------- 页面状态 ----------------


def parse_initial_state(html: str) -> dict:
    """从详情页 HTML 里取出 ``window.__INITIAL_STATE__``。

    **不是 JSON 是 JS 字面量**:里面有裸 ``undefined``,直接 ``json.loads`` 必炸
    (真夹具第 2425 字符处就是)。只把 ``undefined`` 换成 ``null`` —— 别的 JS 语法
    (函数、单引号)这份状态里没有,真出现了就该报错让人来看,不做花式容错。
    """
    match = _STATE_RE.search(html)
    if not match:
        raise NoteExtractError(
            "页面里没有 window.__INITIAL_STATE__:可能被风控挡了(返回的是验证页)"
            "或平台改版,需重新取证"
        )
    raw = match.group(1)
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        try:
            return json.loads(re.sub(r"(?<=[:,\[])\s*undefined", " null", raw))
        except json.JSONDecodeError as exc:
            raise NoteExtractError(f"__INITIAL_STATE__ 解析失败:{exc}") from exc


def select_note(state: dict, note_id: str) -> dict:
    """按 note_id 从 ``noteDetailMap`` 取目标笔记 —— **推荐流隔离就在这一步**。

    详情页会同时带上推荐流的其它笔记(SPA 内跳后尤其明显),运营原报告用的全文正则
    ``"urlDefault":\\s*"(http[^"]+)"`` 会把它们的图一起捞走。按 key 取则天然只拿目标篇。

    取不到时**报错,绝不退而取 map 里的第一篇** —— 那正好是取到推荐流的那条老路。

    污染规模有真号数据:2026-08-07 账号 9 开这篇详情页,整页 123 个 ``<img>``,只有 9 个
    命中本篇的 6 个 file_id(轮播 + 缩略图重复),其余一百多张全是推荐流。所以本实现
    **一张图都不从 DOM 上捡**。
    """
    detail_map = ((state or {}).get("note") or {}).get("noteDetailMap") or {}
    entry = detail_map.get(note_id)
    note = (entry or {}).get("note") if isinstance(entry, dict) else None
    if not note:
        raise NoteExtractError(
            f"页面数据里没有 note_id={note_id} 这篇(笔记可能已删除/私密,"
            f"或链接缺 xsec_token 被平台挡了)"
        )
    return note


# ---------------- 图片 ----------------


def permanent_image_url(file_id: str | None) -> str | None:
    """``fileId`` → 永久原图链。空 fileId 返回 None(老笔记可能给不出)。"""
    if not file_id:
        return None
    return f"{PERMANENT_HOST}/{file_id.lstrip('/')}"


def build_images(note: dict) -> list[dict]:
    """目标笔记的图片清单(顺序 = imageList 数组序 = 页面第几张图)。

    ``signed_url`` **原样**取 ``urlDefault``,不自己拼后缀(取证:自造后缀 6 张全 403)。
    ``url`` 留给图床代下后回填,这里先给 None —— 键先在,值后填,不静默省略。
    """
    images: list[dict] = []
    for index, item in enumerate(note.get("imageList") or [], start=1):
        if not isinstance(item, dict):
            continue
        file_id = item.get("fileId") or ""
        images.append({
            "ordinal": index,
            "url": None,
            "signed_url": item.get("urlDefault") or None,
            "permanent_url": permanent_image_url(file_id),
            "file_id": file_id,
            "width": item.get("width"),
            "height": item.get("height"),
            "live_photo": bool(item.get("livePhoto")),
            "bytes": None,
        })
    return images


# ---------------- 视频(schema 未取证,通用扫描) ----------------

# 可下载地址的候选键(平台在不同编码分支下键名不同,这里按语义扫而不按路径写死)。
_VIDEO_URL_KEYS = ("masterUrl", "backupUrls", "url", "videoUrl")
_VIDEO_DURATION_KEYS = ("duration", "videoDuration")


def _scan_video(node: Any) -> tuple[str | None, int | None]:
    """在 ``note.video`` 子树里找一个可下载地址与时长(深度优先,取第一个像样的)。

    **为什么是扫描不是写死路径**:2026-08-07 取证只有图文样例,视频笔记的
    ``video{}`` 结构未验证。写死一条猜的路径,平台一旦不是那个形状就静默返回 null;
    按键名语义扫则对结构变化不敏感,且找不到时能诚实说"没找到"。
    """
    url: str | None = None
    duration: int | None = None
    stack: list[Any] = [node]
    while stack:
        cur = stack.pop(0)
        if isinstance(cur, dict):
            for key, value in cur.items():
                if url is None and key in _VIDEO_URL_KEYS:
                    candidate = value[0] if isinstance(value, list) and value else value
                    if isinstance(candidate, str) and candidate.startswith("http"):
                        url = candidate
                if duration is None and key in _VIDEO_DURATION_KEYS:
                    if isinstance(value, (int, float)) and value > 0:
                        duration = int(value)
                if isinstance(value, (dict, list)):
                    stack.append(value)
        elif isinstance(cur, list):
            stack.extend(cur)
    return url, duration


# ---------------- 契约组装 ----------------


def build_payload(note: dict, ref: NoteRef) -> dict:
    """把平台的 note 对象译成运营要的返回契约。

    **拿不到的键给 None 并在 ``unavailable`` 里说明原因,绝不静默省略**(需求原话)。
    """
    unavailable: dict[str, str] = {}

    raw_type = note.get("type")
    note_type = _TYPE_CN.get(raw_type)
    if note_type is None:
        unavailable["note_type"] = (
            f"平台返回的类型是 {raw_type!r},不在已知的 normal(图文)/ video(视频) 里,"
            f"不猜;raw 值见 note_type_raw"
        )

    images = build_images(note)

    video = None
    if raw_type == "video":
        url, duration = _scan_video(note.get("video") or {})
        if url:
            video = {
                "url": url,
                "duration_seconds": duration,
                "transcript": None,
                # 视频笔记 schema 未取证(取证样例是图文),地址靠通用扫描得来,
                # 调用方拿它去下载前请自行验一次可达性。
                "schema_verified": False,
            }
            unavailable["video.transcript"] = "v1 不做转写:先把可下载地址给出来,转写另议"
        else:
            unavailable["video"] = (
                "这是视频笔记,但页面数据里没扫到可下载地址 —— 视频笔记的 __INITIAL_STATE__ "
                "结构尚未取证(样例是图文笔记),需要拿一条视频笔记链接补一次取证"
            )
    else:
        unavailable["video"] = f"本篇不是视频笔记(type={raw_type!r}),没有视频"

    interact_raw = note.get("interactInfo") or {}
    interact = {
        "liked": _to_int(interact_raw.get("likedCount")),
        "collected": _to_int(interact_raw.get("collectedCount")),
        "comment": _to_int(interact_raw.get("commentCount")),
        "share": _to_int(interact_raw.get("shareCount")),
    }

    user = note.get("user") or {}
    author = {
        "nickname": user.get("nickname"),
        "user_id": user.get("userId"),
        "avatar": user.get("avatar"),
        # 平台没有现成的"主页链接"字段,按 userId + user 自己的 xsec_token 拼。
        "profile_url": _profile_url(user),
        # IP 属地是**笔记级**字段(挂在 note 上不在 user 里),别去 user{} 里找。
        "ip_location": note.get("ipLocation"),
    }
    if not author["profile_url"]:
        unavailable["author.profile_url"] = "页面数据里没有作者 userId,拼不出主页链接"

    published_ms = note.get("time")
    published_at = _iso_cst(published_ms)
    if published_at is None:
        unavailable["published_at"] = "页面数据里没有发布时间字段"

    is_goods = "goods" in ref.note_attributes
    if is_goods:
        goods_source = "url:noteAttributes=goods"
    else:
        goods_source = "url_param_absent"
        unavailable["is_goods_note"] = (
            "判 false 的依据只是链接上没有 noteAttributes=goods —— 取证确认页面本身"
            "**没有**任何商品笔记的结构化信号(全文搜 goods/商品/GoodsCard 零命中),"
            "所以手工精简过的链接会静默判 false。要准确判定,请用分享按钮生成的原始短链。"
        )

    unavailable["comments"] = (
        "评论不在服务端渲染数据里(comments.list 为空 + firstRequestFinish=false,是纯客户端"
        "异步拉取),纯 HTTP 取不到;要评论请传 with_comments>0 + account_id,会起一次"
        "浏览器会话(消耗该号会话额度)"
    )

    return {
        "note_id": note.get("noteId") or ref.note_id,
        "note_type": note_type,
        "note_type_raw": raw_type,
        "title": note.get("title"),
        "content": note.get("desc"),
        "topics": [t.get("name") for t in (note.get("tagList") or []) if t.get("name")],
        "images": images,
        "video": video,
        "comments": None,
        "comments_complete": False,
        # 评论**数据**从哪来(历史事实);与 source.browser_session_used(**本次调用**有没有
        # 烧会话额度)是两件事 —— 命中缓存时前者是 browser_session、后者必须是 false。
        "comments_source": None,
        "interact": interact,
        "author": author,
        "published_at": published_at,
        "published_at_epoch_ms": published_ms if isinstance(published_ms, int) else None,
        "last_update_at": _iso_cst(note.get("lastUpdateTime")),
        "is_goods_note": is_goods,
        "is_goods_note_source": goods_source,
        "unavailable": unavailable,
        "source": {
            "final_url": ref.final_url,
            "xsec_token": ref.xsec_token,
            "xsec_source": ref.xsec_source,
            "note_attributes": list(ref.note_attributes),
            "fetched_at": datetime.now(timezone.utc).isoformat(),
            "from_cache": False,
            "browser_session_used": False,
        },
    }


def _to_int(value: Any) -> int | None:
    """平台的计数是字符串("1026");万/亿类简写原样返回不到,返 None 而不是瞎折算。"""
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _profile_url(user: dict) -> str | None:
    from urllib.parse import quote

    user_id = user.get("userId")
    if not user_id:
        return None
    token = user.get("xsecToken")
    if not token:
        return f"https://www.xiaohongshu.com/user/profile/{user_id}"
    return (
        f"https://www.xiaohongshu.com/user/profile/{user_id}"
        f"?xsec_token={quote(token, safe='')}&xsec_source=pc_note"
    )


def _iso_cst(epoch_ms: Any) -> str | None:
    """epoch 毫秒 → 北京时间 ISO8601(带显式 +08:00 偏移,不是"本地时间")。"""
    if not isinstance(epoch_ms, int) or epoch_ms <= 0:
        return None
    return datetime.fromtimestamp(epoch_ms / 1000, tz=_CST).isoformat()


# ---------------- 下载 ----------------


async def download_image(item: dict, client) -> bytes | None:
    """按"签名 URL 原样 → 永久链兜底"的顺序下载一张图;都失败返回 None。

    顺序是有理由的:签名 URL 是展示尺寸(约 100-200KB,拆解够看),永久链是原图
    (实测 1.7-15.7MB,大 10 倍以上)。默认取小的,签名过期/被拒时才退到原图。
    """
    for url in (item.get("signed_url"), item.get("permanent_url")):
        if not url:
            continue
        try:
            resp = await client.get(url, headers=CDN_HEADERS, timeout=30.0)
        except Exception as exc:  # noqa: BLE001 — 单张失败不拖垮整篇
            logger.warning(f"[note_extract] 图片下载异常 {url}: {exc}")
            continue
        if resp.status_code == 200 and resp.content:
            return resp.content
        logger.warning(f"[note_extract] 图片下载失败 HTTP {resp.status_code}: {url}")
    return None


# ---------------- 缓存(24h,文件级) ----------------


def cache_path(note_id: str) -> Path:
    """缓存文件路径。note_id 是平台给的十六进制串,仍白名单过一遍防路径穿越。"""
    safe = re.sub(r"[^0-9a-zA-Z]", "", note_id)
    return Path(settings.DATA_DIR) / "note_extracts" / f"{safe}.json"


def cache_store(note_id: str, payload: dict) -> None:
    """落一份缓存(原子写)。写失败只记日志 —— 缓存挂了不该让提取本身失败。

    **为什么不建表**:同 ``media_upload`` 的理由 —— 一次写、之后只读、TTL 靠时间字段,
    没有可变共享状态,加一张表和一次迁移是为不存在的问题增加实体。
    """
    path = cache_path(note_id)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(f".{id(payload)}.tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        tmp.replace(path)
    except OSError as exc:
        logger.warning(f"[note_extract] 缓存写入失败 note_id={note_id}: {exc}")


def cache_load(note_id: str) -> dict | None:
    """读缓存;不存在/损坏/超 24h 一律当没有。"""
    return _read_fresh(cache_path(note_id))


def cache_sweep_expired() -> int:
    """懒清理:把过期/损坏的缓存件从盘上**删掉**,返回删除数。

    **为什么必须真删而不是只在读取时当没有**:缓存件里装的是**他人**的笔记正文、评论
    原话与评论者 user_id。TTL 到了就该从我方磁盘上消失,而不是字节永久堆着、只是不再被
    读到。形态照 ``upload_service.sweep_expired`` —— 写新缓存时顺手扫一遍,零后台循环、
    零新实体。过期判据直接复用 ``_read_fresh``(与读路径同一条规则,不另写一套 TTL)。

    在途写入的 ``.tmp`` 件不在 ``*.json`` 里,天然不会被扫到。
    """
    root = Path(settings.DATA_DIR) / "note_extracts"
    if not root.is_dir():
        return 0
    removed = 0
    for path in root.glob("*.json"):
        if _read_fresh(path) is not None:
            continue
        try:
            path.unlink()
            removed += 1
        except OSError as exc:
            logger.warning(f"[note_extract] 过期缓存删除失败 {path.name}: {exc}")
    return removed


def _read_fresh(path: Path) -> dict | None:
    """读一个缓存件;不存在/损坏/超 TTL 返回 None。"""
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    fetched = ((data.get("source") or {}).get("fetched_at")) or ""
    try:
        stamp = datetime.fromisoformat(fetched)
    except ValueError:
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    if datetime.now(timezone.utc) - stamp > timedelta(seconds=CACHE_TTL_SECONDS):
        return None
    return data


def cache_covers(cached: dict, with_images: bool, with_comments: int) -> bool:
    """缓存件是否**够本次请求用** —— 不够就得重抓,绝不拿旧件冒充。

    - 要图:缓存里每张图都得已经代下到图床(``url`` 有值);一张图都没有的笔记算覆盖;
    - 要评论:条数够,或当初已经抓到底(``comments_complete``)。
    """
    if with_images:
        images = cached.get("images") or []
        if any(not img.get("url") for img in images):
            return False
    if with_comments > 0:
        comments = cached.get("comments")
        if comments is None:
            return False
        if len(comments) < with_comments and not cached.get("comments_complete"):
            return False
    return True


def merge_comments(payload: dict, comments: Iterable[dict], complete: bool) -> dict:  # noqa: D401
    """把浏览器会话抓回的评论并进内容件(同时抹掉 unavailable 里那条说明)。"""
    payload["comments"] = list(comments)
    payload["comments_complete"] = bool(complete)
    payload["comments_source"] = "browser_session"
    # setdefault 而不是直接下标:老缓存件可能没有这两个键,并入评论不该因此炸掉
    payload.setdefault("unavailable", {}).pop("comments", None)
    payload.setdefault("source", {})["browser_session_used"] = True
    return payload


# ---------------- 编排(纯 HTTP 主路) ----------------


async def extract(
    url: str,
    *,
    with_images: bool,
    with_comments: int,
    operator,
    session,
    refresh: bool = False,
    client=None,
) -> dict:
    """一次完整的纯 HTTP 提取:跟短链 → 拉页面 → 解析 → (代下图进图床) → 落缓存。

    **评论不在这条路上**(纯 HTTP 拿不到,见模块 docstring);``with_comments`` 只用于
    判断缓存件够不够用,真正的抓取由 REST 层另起浏览器任务。

    Args:
        refresh: True 时跳过缓存强制重抓(运营发现原帖改过时用)。
        client: httpx.AsyncClient;不给则本函数自建自关(测试注入假件)。
    """
    import httpx

    owns_client = client is None
    client = client or httpx.AsyncClient(follow_redirects=True)
    try:
        final_url = await resolve_share_link(url, client) if is_share_link(url) else url
        ref = parse_note_ref(final_url)

        if not refresh:
            cached = cache_load(ref.note_id)
            if cached and cache_covers(cached, with_images, with_comments):
                cached["source"]["from_cache"] = True
                # **本次调用**没有起任何浏览器会话(哪怕缓存里的评论当初是会话抓的)——
                # 运营要的是"这一次烧没烧额度",别把历史事实冒充成本次成本。
                cached["source"]["browser_session_used"] = False
                # 缓存件里 is_goods_note 的判据来自**当初那条链接**;本次链接若带了
                # goods 参数而缓存件没有,那是新信息,补上(反之不抹 —— 缺参数不是证据)。
                if "goods" in ref.note_attributes and not cached.get("is_goods_note"):
                    cached["is_goods_note"] = True
                    cached["is_goods_note_source"] = "url:noteAttributes=goods"
                    cached.get("unavailable", {}).pop("is_goods_note", None)
                return cached

        resp = await client.get(final_url, headers=PAGE_HEADERS, timeout=30.0)
        if resp.status_code >= 400:
            raise NoteExtractError(f"笔记页面拉取失败:HTTP {resp.status_code}")
        note = select_note(parse_initial_state(resp.text), ref.note_id)
        payload = build_payload(note, ref)

        if with_images and payload["images"]:
            await _fill_image_bed(payload, client, operator, session)

        cache_store(payload["note_id"], payload)
        # 懒清理:落新件时顺手把过期的他人内容从盘上扫掉(见 cache_sweep_expired)。
        cache_sweep_expired()
        return payload
    finally:
        if owns_client:
            await client.aclose()


async def _fill_image_bed(payload: dict, client, operator, session) -> None:
    """代下每张图存进自家图床,把 ``images[].url`` 回填成 mcp 链接(运营首选形态)。

    下不动的图 ``url`` 留 None 并在 ``unavailable`` 里记一笔 —— **绝不重排剩下的图**,
    序号就是"第几张图",错位了拆解时对不上原帖。

    **超限的那张要在这里先滤掉**:``save_images`` 是"全部通过才落盘",单张超
    ``UPLOAD_MAX_MB`` 直接 ValueError,整批一起放弃。而 ``download_image`` 的兜底恰恰会
    退到永久链 —— 那是**原图**(实测同一张图签名件 112KB、原图 15.0MB,差 139 倍),
    一张退到原图就足以让本来好好的其余几张全变 None。逐张过滤,别让一张拖垮整批。
    """
    from datetime import datetime as _dt

    from app.services.upload_service import save_images

    max_bytes = settings.UPLOAD_MAX_MB * 1024 * 1024
    downloaded: list[tuple[int, bytes]] = []
    oversize: list[int] = []
    for index, item in enumerate(payload["images"]):
        data = await download_image(item, client)
        if not data:
            continue
        item["bytes"] = len(data)
        if len(data) > max_bytes:
            oversize.append(item["ordinal"])
            logger.warning(
                f"[note_extract] 第 {item['ordinal']} 张 {len(data)} 字节超图床单张上限,"
                f"跳过入库(多半是签名链失效退到了永久原图链)"
            )
            continue
        downloaded.append((index, data))

    if downloaded:
        try:
            saved = await save_images(
                session,
                operator,
                [(f"{i:02d}.img", data) for i, data in downloaded],
                _dt.utcnow(),
            )
        except ValueError as exc:
            payload["unavailable"]["images"] = f"图床落盘失败:{exc}(signed_url / permanent_url 仍可用)"
            return
        for (index, _data), url in zip(downloaded, saved["urls"]):
            payload["images"][index]["url"] = url
        payload["image_batch"] = {
            "batch_id": saved["batch_id"], "expires_at": saved["expires_at"].isoformat()
        }
        _mark_batch_source(saved["batch_id"], payload)

    _explain_missing_images(payload, oversize)


def _explain_missing_images(payload: dict, oversize: list[int]) -> None:
    """把"哪几张没进图床、为什么"写进 ``unavailable``(两种原因分开说,别混成一句)。"""
    missing = [img["ordinal"] for img in payload["images"] if not img["url"]]
    if not missing:
        return
    reasons = []
    failed = [o for o in missing if o not in oversize]
    if failed:
        reasons.append(
            f"第 {failed} 张没下下来(签名 URL 与永久链都失败,多半是平台 CDN 策略变了)"
        )
    if oversize:
        reasons.append(
            f"第 {oversize} 张超过图床单张 {settings.UPLOAD_MAX_MB}MB 上限没入库"
            f"(签名展示图约 100-200KB,退到永久链拿回的是原图,实测可达 15MB)"
        )
    payload["unavailable"]["images"] = (
        ";".join(reasons) + ";这几张的 signed_url / permanent_url 仍原样给出,可自行带 Referer 取"
    )


def _mark_batch_source(batch_id: str, payload: dict) -> None:
    """在图床批次目录里落一个来源标记:盘上区分"代下的他人素材"与"运营自己上传的素材"。

    代下的图进的是与运营自有素材同一个桶(``DATA_DIR/uploads/{batch_id}/``)、同一张
    ``upload_batches`` 表,而 publish 封面正是从这个桶按路径取 —— 没有标记就无从分辨。

    **为什么是同目录一个点文件**(三种做法里实体最少的那个):目录名前缀要给
    ``save_images`` 加参数,那是 uploads_rest 也在用的公共签名;落库要加列 + 一次迁移。
    点文件零新实体、不进 ``urls`` 因而不影响任何按路径取图的调用方,且跟着批次目录被
    ``upload_service.sweep_expired`` 的 rmtree 一起清掉,不会变成孤儿。
    """
    path = Path(settings.DATA_DIR) / "uploads" / batch_id / ".source.json"
    try:
        path.write_text(
            json.dumps(
                {
                    "kind": "third_party_note_extract",
                    "note_id": payload.get("note_id"),
                    "note_url": (payload.get("source") or {}).get("final_url"),
                    "author_user_id": (payload.get("author") or {}).get("user_id"),
                    "fetched_at": (payload.get("source") or {}).get("fetched_at"),
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
    except OSError as exc:  # 标记写不上不该让提取本身失败
        logger.warning(f"[note_extract] 图床来源标记写入失败 batch_id={batch_id}: {exc}")
