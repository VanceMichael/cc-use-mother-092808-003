"""追加式事件日志存储。

每条事件在追加时写入序列号、时间戳、前序哈希与自身哈希；日志只追加、
不修改、不删除。进程重启后通过 :meth:`AppendOnlyStore.replay` 重放即可
恢复全部状态，待办与到期许可也由派生状态自然延续。

存储抽象为内存列表加可选的 JSONL 落盘文件，单进程内串行追加即满足
线性一致；文件每行一条事件，崩溃时最后一行若不完整会被拒绝重放，
而不是悄悄截断前面的有效记录。
"""

from __future__ import annotations

import json
import threading
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

from src.governance.hashing import GENESIS, hash_event, verify_chain


class Clock:
    """可显式推进的逻辑时钟，事件时间戳与许可到期判定共用。

    读取（调用实例）不推进时间，:meth:`advance_to` / :meth:`advance`
    才推进；真实部署可把系统毫秒时钟包装成同样接口注入。
    """

    def __init__(self, start: int = 0) -> None:
        self._t = start

    def __call__(self) -> int:
        return self._t

    def advance_to(self, value: int) -> int:
        if value < self._t:
            raise ValueError("逻辑时钟不能回拨")
        self._t = value
        return self._t

    def advance(self, delta: int = 1) -> int:
        if delta < 0:
            raise ValueError("逻辑时钟不能回拨")
        self._t += delta
        return self._t


class AppendOnlyStore:
    """线程安全的追加式事件日志。"""

    def __init__(self, path: str | Path | None = None, *, clock: Callable[[], int] | None = None) -> None:
        self._path = Path(path) if path is not None else None
        self._clock: Callable[[], int] = clock or Clock()
        self._events: list[dict[str, Any]] = []
        self._lock = threading.RLock()
        if self._path is not None and self._path.exists():
            self._load()

    @property
    def clock(self) -> Callable[[], int]:
        return self._clock

    @property
    def now(self) -> int:
        """事件时间戳与业务规则共用的当前时间。"""
        return self._clock()

    @property
    def events(self) -> list[dict[str, Any]]:
        """事件的只读视图（副本，外部修改不影响日志）。"""
        with self._lock:
            return [dict(event) for event in self._events]

    def head(self) -> str:
        """当前链头哈希，空日志返回创世标记。"""
        with self._lock:
            return self._events[-1]["hash"] if self._events else GENESIS

    def append(self, event_type: str, payload: dict[str, Any]) -> dict[str, Any]:
        """追加一条事件并返回其最终形态。"""
        if not isinstance(payload, dict):
            raise TypeError("事件负载必须是对象")
        with self._lock:
            seq = len(self._events)
            event = {
                "seq": seq,
                "ts": self.now,
                "type": event_type,
                "payload": payload,
                "prev": self.head(),
            }
            event["hash"] = hash_event(event)
            self._events.append(event)
            if self._path is not None:
                with self._path.open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps(event, ensure_ascii=False) + "\n")
                    handle.flush()
            return dict(event)

    def replay(self) -> Iterable[dict[str, Any]]:
        """按序号产出全部事件；恢复前先校验哈希链。"""
        with self._lock:
            verify_chain(self._events)
            return [dict(event) for event in self._events]

    def _load(self) -> None:
        assert self._path is not None
        loaded: list[dict[str, Any]] = []
        for line_no, raw in enumerate(self._path.read_text(encoding="utf-8").splitlines(), start=1):
            if not raw.strip():
                continue
            event = json.loads(raw)
            expected = hash_event(event)
            if event.get("hash") != expected:
                raise ValueError(f"日志第 {line_no} 行哈希不符，拒绝静默截断")
            loaded.append(event)
        verify_chain(loaded)
        self._events = loaded
        # 重启后时钟不允许倒退到历史事件之前；注入的时钟若是 Clock，
        # 自动对齐到最后一条事件时间。
        if loaded and isinstance(self._clock, Clock):
            self._clock.advance_to(max(event["ts"] for event in loaded))
