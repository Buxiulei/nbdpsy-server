"""GET /api/guide 统一指南接口 + 防腐烂测试。

本文件的核心职责不是"测接口能返回 200",而是**让文档漂移在 CI 就红**:
分组漏归、changelog 写了不存在的端点、字段缺失、日期非法、倒序坏掉,
任何一条都会在这里失败。guide 的价值全部建立在这些约束上。
"""

from datetime import date

from tests.rest_helpers import ADMIN_KEY, bearer, rest_client


def _manifest_paths() -> set[str]:
    """实际注册的 /api/* 路径集合(以 manifest 聚合表为准)。"""
    from app.http import ALL_MANIFEST_ENTRIES

    return {e["path"] for e in ALL_MANIFEST_ENTRIES}


async def test_guide_requires_apikey(tmp_path, monkeypatch):
    """鉴权与 manifest 同款:裸调 401。"""
    async with rest_client(tmp_path, monkeypatch) as client:
        r = await client.get("/api/guide")
        assert r.status_code == 401


async def test_guide_returns_four_sections(tmp_path, monkeypatch):
    """四段齐全且非空,meta 字段完整。"""
    async with rest_client(tmp_path, monkeypatch) as client:
        r = await client.get("/api/guide", headers=bearer(ADMIN_KEY))
        assert r.status_code == 200, r.text
        data = r.json()
        for key in ("capabilities", "changelog", "known_limitations", "meta"):
            assert data[key], f"guide 缺 {key}"
        meta = data["meta"]
        for key in ("server_version", "generated_at", "manifest_entry_count",
                    "guide_contract_version"):
            assert meta[key], f"guide.meta 缺 {key}"
        from app.http import ALL_MANIFEST_ENTRIES

        assert meta["manifest_entry_count"] == len(ALL_MANIFEST_ENTRIES)
        # 端点详情不内联,只给指针——体积由此收口。
        assert "/api/manifest" in data["see"]


async def test_guide_capabilities_inline_summary_only(tmp_path, monkeypatch):
    """能力域内联的端点摘要只有 method/path/summary,不复制 params/errors 长文案。"""
    async with rest_client(tmp_path, monkeypatch) as client:
        r = await client.get("/api/guide", headers=bearer(ADMIN_KEY))
        assert r.status_code == 200, r.text
        for group in r.json()["capabilities"]:
            assert group["key"] and group["title"] and group["summary"]
            assert group["endpoints"], f"能力域 {group['key']} 没有端点"
            for e in group["endpoints"]:
                assert set(e) == {"method", "path", "summary"}, (
                    f"{e['path']} 内联了多余字段: {sorted(set(e) - {'method', 'path', 'summary'})}"
                )


def test_capability_groups_cover_all_manifest_paths():
    """防漏归组:分组声明的路径集合与 manifest 路径集合双向全等。

    新增端点没归组 → 这里报"未归组";删了端点分组表没跟着删 → 报"归了不存在的"。
    """
    from app.http.guide import CAPABILITY_GROUPS

    grouped = {p for g in CAPABILITY_GROUPS for p in g["paths"]}
    actual = _manifest_paths()
    assert grouped == actual, (
        f"未归组: {sorted(actual - grouped)}; 归了不存在的路径: {sorted(grouped - actual)}"
    )


def test_capability_groups_no_duplicate_path():
    """一个路径只能属于一个能力域,否则 guide 会把同一端点讲两遍。"""
    from app.http.guide import CAPABILITY_GROUPS

    seen: dict[str, str] = {}
    for g in CAPABILITY_GROUPS:
        for p in g["paths"]:
            assert p not in seen, f"{p} 同时归入 {seen[p]} 与 {g['key']}"
            seen[p] = g["key"]


def test_capability_group_keys_unique_and_described():
    """能力域自身字段完整、key 不重。"""
    from app.http.guide import CAPABILITY_GROUPS

    keys = [g["key"] for g in CAPABILITY_GROUPS]
    assert len(keys) == len(set(keys)), f"能力域 key 重复: {keys}"
    for g in CAPABILITY_GROUPS:
        assert g["title"] and g["summary"] and g["paths"]


def test_changelog_entries_reference_real_endpoints():
    """防写了不存在的端点:changelog 每条 endpoints 里的路径都要在 manifest 里。"""
    from app.http.guide import CHANGELOG_ENTRIES

    actual = _manifest_paths()
    for entry in CHANGELOG_ENTRIES:
        for path in entry["endpoints"]:
            assert path in actual, (
                f"changelog「{entry['title']}」写了不存在的端点 {path}"
            )


def test_changelog_entries_field_shape():
    """字段完整 + kind 枚举合法 + date 是合法 ISO 日期。"""
    from app.http.guide import CHANGELOG_KINDS, CHANGELOG_ENTRIES

    for entry in CHANGELOG_ENTRIES:
        assert set(entry) <= {"date", "title", "kind", "summary", "endpoints", "notes"}
        for key in ("date", "title", "kind", "summary", "endpoints"):
            assert entry.get(key) is not None, f"changelog 条目缺 {key}: {entry}"
        assert entry["kind"] in CHANGELOG_KINDS, (
            f"kind 非法 {entry['kind']},合法值 {sorted(CHANGELOG_KINDS)}"
        )
        date.fromisoformat(entry["date"])  # 非法日期直接抛 ValueError
        assert entry["title"] and entry["summary"]


def test_changelog_is_newest_first():
    """倒序:最新的在最前,skill 侧读前 N 条就是最近变更。"""
    from app.http.guide import CHANGELOG_ENTRIES

    dates = [date.fromisoformat(e["date"]) for e in CHANGELOG_ENTRIES]
    assert dates == sorted(dates, reverse=True), f"changelog 未按日期倒序: {dates}"


def test_known_limitations_field_shape():
    """边界清单每条四要素齐全,since 是合法日期,area 落在已声明的能力域里。"""
    from app.http.guide import CAPABILITY_GROUPS, KNOWN_LIMITATIONS

    valid_areas = {g["key"] for g in CAPABILITY_GROUPS}
    for item in KNOWN_LIMITATIONS:
        assert set(item) == {"area", "what", "why", "since"}, item
        assert item["area"] in valid_areas, (
            f"边界 area={item['area']} 不是已声明的能力域 {sorted(valid_areas)}"
        )
        assert item["what"] and item["why"]
        date.fromisoformat(item["since"])


def test_guide_is_self_describing():
    """自举:查询能力的端点自己必须能被查到(在 manifest 里且归了组)。"""
    from app.http.guide import CAPABILITY_GROUPS

    assert "/api/guide" in _manifest_paths(), "guide 自己没进 manifest"
    grouped = {p for g in CAPABILITY_GROUPS for p in g["paths"]}
    assert "/api/guide" in grouped, "guide 自己没归组"


def test_manifest_points_back_to_guide():
    """manifest 条目里给 guide 的反向指针,免得只读 manifest 的老 skill 永远发现不了 guide。"""
    from app.http.manifest import MANIFEST_ENTRIES

    entry = next(e for e in MANIFEST_ENTRIES if e["path"] == "/api/manifest")
    assert "/api/guide" in entry["notes"]
