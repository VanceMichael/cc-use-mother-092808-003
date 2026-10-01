"""内容哈希与事件链工具。

服务只用标准库；哈希用于：

1. 工件版本指纹（素材、提示、模型、候选、参数集、决定）；
2. 事件链完整性——每条事件携带前一条的哈希，任何重写都会断链；
3. 仿真会话幂等键，保证设备重传同一会话只记录一次。
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Mapping

GENESIS = "GENESIS"


def canonical_json(value: Any) -> str:
    """与键顺序无关、只依赖内容的规范 JSON 文本。"""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def content_hash(value: Any) -> str:
    """计算任意可 JSON 化内容的 SHA-256 十六进制指纹。"""
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def hash_event(event: Mapping[str, Any]) -> str:
    """计算单条事件的哈希（不含事件自身的 ``hash`` 字段）。"""
    body = {k: v for k, v in event.items() if k != "hash"}
    return content_hash(body)


def verify_chain(events: list[dict[str, Any]]) -> None:
    """校验事件序列的前后哈希链，篡改或缺漏立即报错。

    用于重放恢复与审计导出；事件类型本身不做白名单，以便日后扩展。
    """
    previous = GENESIS
    for index, event in enumerate(events):
        if event.get("prev") != previous:
            raise ValueError(f"事件链在第 {index} 条断裂")
        if event.get("hash") != hash_event(event):
            raise ValueError(f"事件链在第 {index} 条内容被篡改")
        previous = event["hash"]
