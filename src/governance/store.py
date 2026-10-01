"""JSON 文件持久化：每次写操作原子落盘，服务重启后状态完整延续。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

COLLECTIONS = (
    "briefs",
    "licenses",
    "assets",
    "models",
    "candidates",
    "simulations",
    "reviews",
    "exceptions",
    "decisions",
)


def _empty() -> dict[str, Any]:
    data: dict[str, Any] = {"seq": 0, "events": []}
    for name in COLLECTIONS:
        data[name] = {}
    return data


class Store:
    """面向单文件的治理状态仓库。

    写盘采用临时文件加替换，避免崩溃时留下半个文件。
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        if self.path.exists():
            self.data = json.loads(self.path.read_text(encoding="utf-8"))
        else:
            self.data = _empty()

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(self.path.name + ".tmp")
        temporary.write_text(
            json.dumps(self.data, ensure_ascii=False, indent=1),
            encoding="utf-8",
        )
        temporary.replace(self.path)
