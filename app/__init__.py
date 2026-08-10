"""nbdpsy-api 应用包。"""

# 应用版本:无打包元数据,以此常量为单一事实源
# (GET /api/manifest、GET /api/guide 的 meta、GET /api/extension 引用)。
# 改它必须同笔在 CHANGELOG.md 顶部加对应版本段——tests/test_version_changelog.py 钉着两者相等。
__version__ = "0.22.0"
