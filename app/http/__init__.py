"""REST 路由注册表:server.py 统一 include;manifest 聚合各模块端点元数据。

新增 router 模块的接线口就在这里:import 模块 → ALL_ROUTERS 加 router →
ALL_MANIFEST_ENTRIES 拼其 MANIFEST_ENTRIES(/api/* 之外的路由如 downloads 不进 manifest)。
漏接会被 tests/test_manifest.py 防漂移测试逮住。
"""

from app.http import (
    accounts_rest,
    admin_rest,
    cookies_import,
    cookies_rest,
    downloads,
    extension_rest,
    manifest,
    notes_rest,
    publish_rest,
    system,
    uploads_rest,
    video_rest,
)

ALL_ROUTERS = [
    system.router,
    manifest.router,
    accounts_rest.router,
    admin_rest.router,
    cookies_import.router,
    cookies_rest.router,
    extension_rest.router,
    publish_rest.router,
    notes_rest.router,
    downloads.router,
    uploads_rest.router,
    video_rest.router,
]

ALL_MANIFEST_ENTRIES = [
    *system.MANIFEST_ENTRIES,
    *manifest.MANIFEST_ENTRIES,
    *accounts_rest.MANIFEST_ENTRIES,
    *admin_rest.MANIFEST_ENTRIES,
    *cookies_import.MANIFEST_ENTRIES,
    *cookies_rest.MANIFEST_ENTRIES,
    *extension_rest.MANIFEST_ENTRIES,
    *publish_rest.MANIFEST_ENTRIES,
    *notes_rest.MANIFEST_ENTRIES,
    *uploads_rest.MANIFEST_ENTRIES,
    *video_rest.MANIFEST_ENTRIES,
]
