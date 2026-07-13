"""发布流程自愈学到的选择器持久化(JSON)。

结构:{intent_key: {"desc": str, "learned": [{"selector","source","learned_at","success_count"}]}}
learned 按 success_count 降序返回(学得越稳越先试)。success_count 语义见 design spec:
自愈重新学到该选择器的次数(稳定性代理),非 learned 前置路径的命中次数。
不用 SQLite——learned 选择器进 selectors 前列即等效缓存。
"""

import json
import os
import threading
from pathlib import Path

from app.core.config import settings


class SelectorRegistry:
    def __init__(self, path: str | None = None) -> None:
        self._path = Path(path) if path else Path(settings.DATA_DIR) / "selector_registry.json"
        self._lock = threading.Lock()

    def _load(self) -> dict:
        if not self._path.is_file():
            return {}
        try:
            return json.loads(self._path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}

    def _save(self, data: dict) -> None:
        # 原子写:先写同目录临时文件再 os.replace(同文件系统原子),避免写到一半崩溃损坏 JSON。
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_name(f"{self._path.name}.{os.getpid()}.tmp")
        tmp.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        os.replace(tmp, self._path)

    def get(self, intent_key: str) -> list[str]:
        entry = self._load().get(intent_key)
        if not entry:
            return []
        learned = sorted(
            entry.get("learned", []),
            key=lambda x: x.get("success_count", 0),
            reverse=True,
        )
        return [x["selector"] for x in learned]

    def learn(self, intent_key: str, selector: str, desc: str, learned_at: str) -> None:
        with self._lock:
            data = self._load()
            entry = data.setdefault(intent_key, {"desc": desc, "learned": []})
            entry["desc"] = desc
            for item in entry["learned"]:
                if item["selector"] == selector:
                    item["success_count"] = item.get("success_count", 1) + 1
                    item["learned_at"] = learned_at
                    self._save(data)
                    return
            entry["learned"].append({
                "selector": selector, "source": "selfheal",
                "learned_at": learned_at, "success_count": 1,
            })
            self._save(data)


# 进程级单例(默认路径)。所有 XHSPublishAtomicTasks 实例共用同一 registry + 同一把实例锁,
# 消除 PUBLISH_CONCURRENCY>1 下多个发布实例对同一 JSON load-modify-save 无互斥、互相覆盖的竞争。
# 显式 SelectorRegistry(path=...) 构造仍可用(测试注入临时路径),单例只覆盖默认路径场景。
_default_registry: SelectorRegistry | None = None
_default_registry_lock = threading.Lock()


def get_default_registry() -> SelectorRegistry:
    """返回默认路径的进程级单例 SelectorRegistry(双检锁保证只构造一次)。"""
    global _default_registry
    if _default_registry is None:
        with _default_registry_lock:
            if _default_registry is None:
                _default_registry = SelectorRegistry()
    return _default_registry
