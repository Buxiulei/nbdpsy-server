"""笔记提取(纯 HTTP 层)单测:短链解析 / __INITIAL_STATE__ 解析 / 推荐流隔离 / 契约完整性。

夹具 ``tests/fixtures/note_extract/sample_note_page.html`` 是 2026-08-07 真号取证的
详情页响应裁剪件(``window.__INITIAL_STATE__`` 那一段逐字节原样)。断言里的具体值
(6 张图的 file_id、1026 赞、发布时间)来自那次取证,**不是照着代码写的期望**。
"""

import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.services import note_extract as ne

_FIXTURE = Path(__file__).parent / "fixtures" / "note_extract" / "sample_note_page.html"
_NOTE_ID = "6a4f50d0000000001c025535"
# 取证记录的 6 个 file_id(顺序 = imageList 数组序 = 页面第几张图);
# 取证表里 segment 与 file_id 分两列列出,平台原字段 fileId 是「段/id」合起来的整串。
_FILE_IDS = [
    "notes_pre_post/1040g3k0322d4qu867k6g5ng8nl8g8qh8cfscgjg",
    "notes_pre_post/1040g3k0322d4qu867k5g5ng8nl8g8qh8iu1skdo",
    "notes_pre_post/1040g3k0322d4qu867k2g5ng8nl8g8qh842qq6jg",
    "notes_pre_post/1040g3k0322d4qu867k605ng8nl8g8qh84q06iog",
    "notes_pre_post/1040g3k0322d4qu867k405ng8nl8g8qh8mqo8gig",
    "notes_pre_post/1040g3k0322d4qu867k4g5ng8nl8g8qh8dlfi270",
]
_FULL_URL = (
    "https://www.xiaohongshu.com/discovery/item/6a4f50d0000000001c025535"
    "?app_platform=android&noteAttributes=goods"
    "&xsec_token=CBM_ieBCttxPSXFg5AzYZBrcs4RPZYLuiQHNZKanPbKXU%3D&xsec_source=app_share"
)


def _html() -> str:
    return _FIXTURE.read_text(encoding="utf-8")


def _state() -> dict:
    return ne.parse_initial_state(_html())


# ---------------- 短链与链接解析 ----------------


def test_short_link_recognised():
    assert ne.is_share_link("http://xhslink.cn/o/6uGYemZFqDN") is True
    assert ne.is_share_link("https://www.xiaohongshu.com/discovery/item/abc") is False


def test_parse_note_ref_keeps_xsec_token_and_goods_flag():
    ref = ne.parse_note_ref(_FULL_URL)
    assert ref.note_id == _NOTE_ID
    # xsec_token 必须保留且已解码(URL 里是 %3D 结尾)
    assert ref.xsec_token == "CBM_ieBCttxPSXFg5AzYZBrcs4RPZYLuiQHNZKanPbKXU="
    assert ref.xsec_source == "app_share"
    assert "goods" in ref.note_attributes


def test_parse_note_ref_explore_and_item_paths():
    for path in ("discovery/item", "explore"):
        ref = ne.parse_note_ref(f"https://www.xiaohongshu.com/{path}/{_NOTE_ID}?xsec_token=T1")
        assert ref.note_id == _NOTE_ID
        assert ref.xsec_token == "T1"


def test_parse_note_ref_rejects_url_without_note_id():
    with pytest.raises(ne.NoteExtractError):
        ne.parse_note_ref("https://www.xiaohongshu.com/explore")


async def test_resolve_share_link_follows_302(monkeypatch):
    """短链跟跳:mock HTTP,断言拿到最终 URL 且 xsec_token 一路保留。"""
    calls = []

    class _Resp:
        def __init__(self, url):
            self.url = url
            self.status_code = 200

    class _Client:
        async def get(self, url, **kw):
            calls.append((url, kw))
            assert kw.get("follow_redirects") is True
            return _Resp(_FULL_URL)

    final = await ne.resolve_share_link("http://xhslink.cn/o/6uGYemZFqDN", _Client())
    assert final == _FULL_URL
    assert calls[0][0] == "http://xhslink.cn/o/6uGYemZFqDN"


async def test_resolve_share_link_dead_link_raises(monkeypatch):
    class _Resp:
        url = "http://xhslink.cn/o/dead"
        status_code = 404

    class _Client:
        async def get(self, url, **kw):
            return _Resp()

    with pytest.raises(ne.NoteExtractError):
        await ne.resolve_share_link("http://xhslink.cn/o/dead", _Client())


# ---------------- __INITIAL_STATE__ 解析 ----------------


def test_parse_initial_state_handles_undefined_literal():
    """真页面里 __INITIAL_STATE__ 是 JS 字面量,含裸 undefined —— 直接 json.loads 会炸。"""
    raw = re.search(r"window\.__INITIAL_STATE__=(.+?)</script>", _html(), re.S).group(1)
    with pytest.raises(json.JSONDecodeError):
        json.loads(raw)
    state = ne.parse_initial_state(_html())
    assert _NOTE_ID in state["note"]["noteDetailMap"]


def test_parse_initial_state_missing_raises():
    with pytest.raises(ne.NoteExtractError):
        ne.parse_initial_state("<html><body>没有状态</body></html>")


def test_select_note_by_id():
    note = ne.select_note(_state(), _NOTE_ID)
    assert note["title"] == "停止担心2小时和8公里以外的事｜正念日记"
    assert note["type"] == "normal"


def test_select_note_missing_id_raises():
    with pytest.raises(ne.NoteExtractError):
        ne.select_note(_state(), "0" * 24)


def test_select_note_names_the_missing_token_when_link_had_none():
    """链接没带 xsec_token 时,报错要**直说是链接的问题**,不要和"已删除/私密"混在一起。

    运营最常做的就是从浏览器地址栏里复制一条裸 explore 链接。那种链接必然被平台挡掉,
    而旧文案把三种可能并列("已删除/私密,或链接缺 xsec_token"),读的人第一反应是去查
    笔记状态,查完一圈才发现问题出在链接上 —— 而这一点我们当场就知道:token 在不在,
    parse_note_ref 已经解析出来了。
    """
    with pytest.raises(ne.NoteExtractError) as bare:
        ne.select_note(_state(), "0" * 24, has_xsec_token=False)
    assert "xsec_token" in str(bare.value)
    assert "已删除" not in str(bare.value), "没带 token 是确定的,别再并列一堆猜测"

    # 带了 token 还取不到,才是"可能已删除/私密"那几种
    with pytest.raises(ne.NoteExtractError) as with_token:
        ne.select_note(_state(), "0" * 24, has_xsec_token=True)
    assert "已删除" in str(with_token.value)


# ---------------- 推荐流隔离 ----------------


def test_images_isolated_from_recommendation_feed():
    """隔离判据 = 按 note_id 从 noteDetailMap 取,**不是**全文正则扫 urlDefault。

    污染部分是在**真夹具之上**注入的(SSR 这一页恰好没带推荐流),被断言的是隔离
    机制本身:注入 3 张推荐流图后,全文正则会多捞 3 张,而按 note_id 取仍是原样 6 张。

    污染的规模有真号数据背书:2026-08-07 账号 9 打开这同一篇的详情页,**整页 123 个
    ``<img>``,只有 9 个命中这 6 个 file_id**(轮播 + 缩略图重复计),其余一百多张全是
    推荐流。这正是本实现坚持从 ``__INITIAL_STATE__`` 按 note_id 取、**一张图都不从 DOM
    上捡**的理由 —— 走 DOM 就得在 123 张里挑,挑错了运营拿到的就是别人家的图。
    """
    state = _state()
    polluted = json.loads(json.dumps(state))
    other_id = "1" * 24
    polluted["note"]["noteDetailMap"][other_id] = {
        "note": {
            "noteId": other_id, "type": "normal", "title": "推荐流里的别人家笔记",
            "desc": "", "tagList": [], "user": {}, "interactInfo": {},
            "imageList": [
                {"urlDefault": f"http://sns-webpic-qc.xhscdn.com/x/y/notes/rec{i}!nd_dft_wlteh_webp_3",
                 "fileId": f"notes/rec{i}"}
                for i in range(3)
            ],
        }
    }
    raw = json.dumps(polluted, ensure_ascii=False)
    naive = re.findall(r'"urlDefault":\s*"(http[^"]+)"', raw)
    assert len(naive) == 9, "污染夹具没造对:全文正则应当多捞到 3 张推荐流图"

    images = ne.build_images(ne.select_note(polluted, _NOTE_ID))
    assert len(images) == 6
    assert [i["file_id"] for i in images] == _FILE_IDS
    assert all("rec" not in i["file_id"] for i in images)


def test_images_isolated_when_target_note_absent():
    """目标笔记不在 map 里时报错,**绝不**退而取 map 里的第一篇(那就是取到推荐流了)。"""
    state = _state()
    only_other = {"note": {"noteDetailMap": {"2" * 24: state["note"]["noteDetailMap"][_NOTE_ID]}}}
    with pytest.raises(ne.NoteExtractError):
        ne.select_note(only_other, _NOTE_ID)


# ---------------- 图片链接两条规则 ----------------


def test_image_urls_signed_asis_plus_permanent():
    images = ne.build_images(ne.select_note(_state(), _NOTE_ID))
    first = images[0]
    # 签名展示图:**原样**用 urlDefault 自带的后缀(取证:换成自造 jpg 后缀 6 张全 403)
    assert first["signed_url"].endswith("!nd_dft_wlteh_webp_3")
    assert "sns-webpic-qc.xhscdn.com" in first["signed_url"]
    # 永久链:sns-img-qc/{段}/{file_id},由 fileId 直接拼,不从签名 URL 里正则抠
    assert first["permanent_url"] == "https://sns-img-qc.xhscdn.com/" + _FILE_IDS[0]
    assert first["ordinal"] == 1
    assert first["width"] == 3024 and first["height"] == 4032


def test_permanent_image_url_needs_segment():
    assert ne.permanent_image_url("notes_pre_post/abc") == (
        "https://sns-img-qc.xhscdn.com/notes_pre_post/abc"
    )
    # 老笔记 fileId 可能没有路径段 —— 仍拼得出链接,不抛
    assert ne.permanent_image_url("abc") == "https://sns-img-qc.xhscdn.com/abc"
    assert ne.permanent_image_url("") is None


# ---------------- 返回契约 ----------------


def test_payload_contract_keys_never_silently_dropped():
    ref = ne.parse_note_ref(_FULL_URL)
    payload = ne.build_payload(ne.select_note(_state(), _NOTE_ID), ref)
    for key in (
        "note_id", "note_type", "note_type_raw", "title", "content", "topics",
        "images", "video", "comments", "comments_complete", "comments_source",
        "interact", "author", "published_at",
        "is_goods_note", "is_goods_note_source", "unavailable", "source",
    ):
        assert key in payload, f"契约键 {key} 被省略了"


def test_payload_values_match_forensics():
    ref = ne.parse_note_ref(_FULL_URL)
    p = ne.build_payload(ne.select_note(_state(), _NOTE_ID), ref)
    assert p["note_id"] == _NOTE_ID
    assert p["note_type"] == "图文" and p["note_type_raw"] == "normal"
    assert p["title"] == "停止担心2小时和8公里以外的事｜正念日记"
    # 正文原样:含换行,末尾自带 #话题[话题]# 串
    assert "\n" in p["content"] and "两小时后的会议" in p["content"]
    assert p["topics"][:3] == ["释放压力", "迷走神经", "记录吧就现在"]
    assert len(p["topics"]) == 10
    assert p["interact"] == {"liked": 1026, "collected": 557, "comment": 17, "share": 92}
    assert p["author"]["nickname"] == "念头河"
    assert p["author"]["ip_location"] == "广东"
    assert p["author"]["user_id"] == "5e08bd510000000001006a28"
    # 主页链接自行拼(平台没给现成字段),必须带 user 自己的 xsec_token
    assert "5e08bd510000000001006a28" in p["author"]["profile_url"]
    assert "xsec_token=" in p["author"]["profile_url"]
    assert p["published_at"] == "2026-07-09T15:42:08+08:00"
    assert p["published_at_epoch_ms"] == 1783582928000


def test_is_goods_note_only_source_is_url_param():
    """取证结论:页面里没有任何商品笔记的结构化信号,判据只有 URL 上的 noteAttributes。"""
    note = ne.select_note(_state(), _NOTE_ID)
    with_goods = ne.build_payload(note, ne.parse_note_ref(_FULL_URL))
    assert with_goods["is_goods_note"] is True
    assert with_goods["is_goods_note_source"] == "url:noteAttributes=goods"

    stripped = _FULL_URL.replace("&noteAttributes=goods", "")
    without = ne.build_payload(note, ne.parse_note_ref(stripped))
    # 同一篇笔记、同一份页面数据,去掉参数就判 false —— 这是已知的脆弱点,
    # 必须在返回里自曝来源,不能让调用方以为 false 是页面证据支持的结论。
    assert without["is_goods_note"] is False
    assert without["is_goods_note_source"] == "url_param_absent"
    assert "is_goods_note" in without["unavailable"]


def test_comments_absent_reason_is_explicit():
    p = ne.build_payload(ne.select_note(_state(), _NOTE_ID), ne.parse_note_ref(_FULL_URL))
    assert p["comments"] is None
    assert "comments" in p["unavailable"]
    assert "浏览器会话" in p["unavailable"]["comments"]


def test_video_null_for_image_note():
    p = ne.build_payload(ne.select_note(_state(), _NOTE_ID), ne.parse_note_ref(_FULL_URL))
    assert p["video"] is None
    assert "video" in p["unavailable"]


def test_video_note_best_effort_scan():
    """视频笔记 schema **未取证**:用通用扫描找可下载地址与时长,找不到就 null + 说明。"""
    note = {
        "noteId": "a1b2c3d4e5f60718293a4b5c", "type": "video", "title": "视频笔记", "desc": "正文",
        "tagList": [], "user": {"nickname": "某人", "userId": "u1"}, "interactInfo": {},
        "imageList": [],
        "video": {
            "capa": {"duration": 63},
            "media": {"stream": {"h264": [{"masterUrl": "https://sns-video.xhscdn.com/a.mp4"}]}},
        },
    }
    p = ne.build_payload(note, ne.parse_note_ref("https://www.xiaohongshu.com/explore/a1b2c3d4e5f60718293a4b5c"))
    assert p["note_type"] == "视频"
    assert p["video"]["url"] == "https://sns-video.xhscdn.com/a.mp4"
    assert p["video"]["duration_seconds"] == 63
    assert p["video"]["schema_verified"] is False
    assert p["video"]["transcript"] is None


def test_video_note_without_recognisable_url():
    note = {
        "noteId": "a1b2c3d4e5f60718293a4b5c", "type": "video", "title": "t", "desc": "", "tagList": [],
        "user": {}, "interactInfo": {}, "imageList": [], "video": {"consumer": {}},
    }
    p = ne.build_payload(note, ne.parse_note_ref("https://www.xiaohongshu.com/explore/a1b2c3d4e5f60718293a4b5c"))
    assert p["video"] is None
    assert "video" in p["unavailable"]


def test_unknown_note_type_not_guessed():
    note = {
        "noteId": "f0e1d2c3b4a59687564738a9", "type": "livephoto_or_whatever", "title": "t", "desc": "",
        "tagList": [], "user": {}, "interactInfo": {}, "imageList": [],
    }
    p = ne.build_payload(note, ne.parse_note_ref("https://www.xiaohongshu.com/explore/f0e1d2c3b4a59687564738a9"))
    assert p["note_type"] is None
    assert p["note_type_raw"] == "livephoto_or_whatever"
    assert "note_type" in p["unavailable"]


# ---------------- 缓存 ----------------


def _cache_settings(tmp_path, monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "DATA_DIR", str(tmp_path))


def test_cache_roundtrip_and_ttl(tmp_path, monkeypatch):
    _cache_settings(tmp_path, monkeypatch)
    payload = {
        "note_id": _NOTE_ID, "images": [], "comments": None,
        "source": {"fetched_at": datetime.now(timezone.utc).isoformat()},
    }
    ne.cache_store(_NOTE_ID, payload)
    hit = ne.cache_load(_NOTE_ID)
    assert hit is not None and hit["note_id"] == _NOTE_ID

    # 把 fetched_at 推到 25 小时前 → 过期不命中
    path = ne.cache_path(_NOTE_ID)
    data = json.loads(path.read_text(encoding="utf-8"))
    data["source"]["fetched_at"] = (
        datetime.now(timezone.utc) - timedelta(hours=25)
    ).isoformat()
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    assert ne.cache_load(_NOTE_ID) is None


def test_cache_not_served_when_request_needs_more(tmp_path, monkeypatch):
    """缓存覆盖判定:缓存里没有的东西不能拿旧件冒充。"""
    _cache_settings(tmp_path, monkeypatch)
    cached = {
        "note_id": _NOTE_ID,
        "images": [{"ordinal": 1, "url": None}],  # 当初 with_images=False,没代下
        "comments": None,
        "source": {"fetched_at": datetime.now(timezone.utc).isoformat()},
    }
    assert ne.cache_covers(cached, with_images=False, with_comments=0) is True
    assert ne.cache_covers(cached, with_images=True, with_comments=0) is False
    assert ne.cache_covers(cached, with_images=False, with_comments=20) is False

    cached["images"][0]["url"] = "https://mcp.nbdpsy.com/uploads/b/01.webp"
    cached["comments"] = [{"text": f"c{i}"} for i in range(5)]
    assert ne.cache_covers(cached, with_images=True, with_comments=5) is True
    # 要 20 条但只缓存了 5 条:除非当初已抓到底,否则不算覆盖
    assert ne.cache_covers(cached, with_images=True, with_comments=20) is False
    cached["comments_complete"] = True
    assert ne.cache_covers(cached, with_images=True, with_comments=20) is True


def test_expired_cache_files_are_swept_off_disk(tmp_path, monkeypatch):
    """过期 = 从盘上删掉,不是"读的时候当没有"。

    缓存件里是**他人**的正文、评论原话与评论者 user_id;TTL 到了这些字节就该消失,
    而不是永久堆在我方磁盘上只是不再被读到。损坏件同理(读不出来更没有留着的理由)。
    """
    _cache_settings(tmp_path, monkeypatch)
    now = datetime.now(timezone.utc)
    fresh, stale = "a" * 24, "b" * 24
    ne.cache_store(fresh, {"note_id": fresh, "source": {"fetched_at": now.isoformat()}})
    ne.cache_store(stale, {
        "note_id": stale,
        "source": {"fetched_at": (now - timedelta(hours=25)).isoformat()},
    })
    broken = ne.cache_path("c" * 24)
    broken.write_text("{ 半截件", encoding="utf-8")

    assert ne.cache_sweep_expired() == 2
    assert ne.cache_path(fresh).is_file(), "没过期的不许动"
    assert not ne.cache_path(stale).exists(), "过期件必须真删,不能只是读不到"
    assert not broken.exists()


# ---------------- 图床代下 ----------------


class _StubResponse:
    def __init__(self, content: bytes):
        self.status_code = 200
        self.content = content


class _StubCdn:
    """假 CDN:最后一张给超大字节(复刻"签名链失效退到永久原图链"那一幕),其余给小图。"""

    def __init__(self, big: bytes):
        self.big = big

    async def get(self, url, **kw):
        return _StubResponse(self.big if url.endswith("#6") else b"small-bytes")


def _image_payload(count: int = 6) -> dict:
    return {
        "note_id": _NOTE_ID,
        "images": [
            {
                "ordinal": i + 1, "url": None, "signed_url": f"https://sns-webpic-qc.xhscdn.com/x#{i + 1}",
                "permanent_url": f"https://sns-img-qc.xhscdn.com/y#{i + 1}", "bytes": None,
            }
            for i in range(count)
        ],
        "unavailable": {},
        "author": {"user_id": "5e08bd510000000001006a28"},
        "source": {"final_url": _FULL_URL, "fetched_at": datetime.now(timezone.utc).isoformat()},
    }


def _install_fake_bed(monkeypatch, tmp_path, recorded: dict):
    """假 save_images:**照抄真实现的两条契约** —— 单张超限抛 ValueError、且整批一起黄。

    照抄这两条是这个用例的牙:去掉被测的逐张过滤,超限那张就会被送进来,这里就抛,
    六张全变 None —— 断言必红。
    """
    from app.core.config import settings
    from app.services import upload_service

    monkeypatch.setattr(settings, "DATA_DIR", str(tmp_path))
    max_bytes = settings.UPLOAD_MAX_MB * 1024 * 1024

    async def _fake_save_images(session, operator, files, now):
        for _name, data in files:
            if len(data) > max_bytes:
                raise ValueError(f"单张图片超过 {settings.UPLOAD_MAX_MB}MB 上限")
        recorded["files"] = files
        batch_dir = Path(settings.DATA_DIR) / "uploads" / "BATCH1"
        batch_dir.mkdir(parents=True, exist_ok=True)
        return {
            "batch_id": "BATCH1",
            "urls": [f"https://mcp.nbdpsy.com/uploads/BATCH1/{i:02d}.png" for i in range(1, len(files) + 1)],
            "expires_at": datetime.now(timezone.utc) + timedelta(days=7),
        }

    monkeypatch.setattr(upload_service, "save_images", _fake_save_images)
    return max_bytes


async def test_oversize_image_filtered_instead_of_sinking_whole_batch(tmp_path, monkeypatch):
    """一张超限图**不许**拖垮整批:五张正常的照进图床,第六张给 None + 说明。

    根因是两条实现叠在一起:``download_image`` 签名链失败会退到永久链,而永久链是
    **原图**(审查实测同一篇第一张:签名件 112KB vs 原图 15.0MB,差 139 倍);
    ``save_images`` 又是"全部通过才落盘",单张超限直接 ValueError。不逐张过滤的话,
    一张退到原图就足以让六张 url 全 None。
    """
    recorded: dict = {}
    max_bytes = _install_fake_bed(monkeypatch, tmp_path, recorded)
    payload = _image_payload()
    await ne._fill_image_bed(payload, _StubCdn(b"\0" * (max_bytes + 1)), object(), object())

    assert len(recorded["files"]) == 5, "超限那张不该被送进 save_images(送了整批一起黄)"
    assert all(img["url"] for img in payload["images"][:5]), "其余五张必须照常进图床"
    assert payload["images"][5]["url"] is None
    # 序号绝不重排:第 6 张仍是第 6 张
    assert [img["ordinal"] for img in payload["images"]] == [1, 2, 3, 4, 5, 6]
    reason = payload["unavailable"]["images"]
    assert "[6]" in reason and "MB 上限" in reason
    # 拿不到图床链的那张,原始链接照给,运营仍能自己去取
    assert payload["images"][5]["signed_url"] and payload["images"][5]["permanent_url"]
    assert payload["images"][5]["bytes"] == max_bytes + 1


async def test_image_bed_batch_carries_third_party_source_marker(tmp_path, monkeypatch):
    """代下的他人图与运营自有素材同桶同表,盘上必须留得下"这批是代下的"这条信息。"""
    from app.core.config import settings

    recorded: dict = {}
    _install_fake_bed(monkeypatch, tmp_path, recorded)
    payload = _image_payload(count=2)
    await ne._fill_image_bed(payload, _StubCdn(b"never-used"), object(), object())

    marker = Path(settings.DATA_DIR) / "uploads" / payload["image_batch"]["batch_id"] / ".source.json"
    assert marker.is_file(), "批次目录里没有来源标记,盘上分不出他人素材与我方素材"
    info = json.loads(marker.read_text(encoding="utf-8"))
    assert info["kind"] == "third_party_note_extract"
    assert info["note_id"] == _NOTE_ID
    assert info["note_url"] == _FULL_URL
    assert info["author_user_id"] == "5e08bd510000000001006a28"


def test_cache_no_images_at_all_counts_as_covered(tmp_path, monkeypatch):
    """纯图 0 张的笔记(理论上不存在,但视频笔记就是 0 张):别把空列表判成没缓存。"""
    _cache_settings(tmp_path, monkeypatch)
    cached = {
        "note_id": _NOTE_ID, "images": [], "comments": None,
        "source": {"fetched_at": datetime.now(timezone.utc).isoformat()},
    }
    assert ne.cache_covers(cached, with_images=True, with_comments=0) is True
