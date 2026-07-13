"""REST 路由注册表:server.py 统一 include;manifest 聚合各模块端点元数据。

新增 router 模块的接线口就在这里:import 模块 → ALL_ROUTERS 加 router →
ALL_MANIFEST_ENTRIES 拼其 MANIFEST_ENTRIES(/api/* 之外的路由如 downloads 不进 manifest)。
漏接会被 tests/test_manifest.py 防漂移测试逮住。
"""

from app.http import (
    accounts_rest, cookies_import, cookies_rest, downloads, manifest, system,
)

ALL_ROUTERS = [
    system.router,
    manifest.router,
    accounts_rest.router,
    cookies_import.router,
    cookies_rest.router,
    downloads.router,
]

ALL_MANIFEST_ENTRIES = [
    *system.MANIFEST_ENTRIES,
    *manifest.MANIFEST_ENTRIES,
    *accounts_rest.MANIFEST_ENTRIES,
    *cookies_import.MANIFEST_ENTRIES,
    *cookies_rest.MANIFEST_ENTRIES,
]
