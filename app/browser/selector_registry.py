"""发布流程自愈学到的选择器持久化(JSON)。

结构:{intent_key: {"desc": str, "learned": [{"selector","source","learned_at","success_count"}]}}
learned 按 success_count 降序返回(学得越稳越先试)。success_count 语义见 design spec:
自愈重新学到该选择器的次数(稳定性代理),非 learned 前置路径的命中次数。
不用 SQLite——learned 选择器进 selectors 前列即等效缓存。
"""

import json
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
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )

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
