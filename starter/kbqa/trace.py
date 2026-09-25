"""追踪：一次问答的每一步、耗时、错误都记下来，调试面板用。"""

from __future__ import annotations

import threading
import time
import traceback
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional


@dataclass
class Trace:
    trace_id: str
    question: str
    session_id: Optional[str] = None
    started_at: str = field(default_factory=lambda: datetime.now().isoformat(timespec="milliseconds"))
    steps: list[dict] = field(default_factory=list)
    errors: list[dict] = field(default_factory=list)
    llm_calls: list[dict] = field(default_factory=list)
    _t0: float = field(default_factory=time.perf_counter)

    def step(self, name: str, payload: Any = None, started: Optional[float] = None) -> None:
        now = time.perf_counter()
        self.steps.append(
            {
                "step": name,
                "at_ms": round((now - self._t0) * 1000, 1),
                "took_ms": round((now - started) * 1000, 1) if started else None,
                "detail": payload,
            }
        )

    def error(self, where: str, exc: BaseException) -> None:
        """真实原因要留下来：类型、消息、堆栈，一个都不少。"""
        self.errors.append(
            {
                "where": where,
                "type": type(exc).__name__,
                "message": str(exc),
                "traceback": traceback.format_exc(limit=8),
            }
        )

    def llm(self, payload: dict) -> None:
        self.llm_calls.append(payload)

    def as_dict(self) -> dict:
        return {
            "trace_id": self.trace_id,
            "session_id": self.session_id,
            "question": self.question,
            "started_at": self.started_at,
            "total_ms": round((time.perf_counter() - self._t0) * 1000, 1),
            "steps": self.steps,
            "llm_calls": self.llm_calls,
            "errors": self.errors,
        }


class TraceStore:
    def __init__(self, capacity: int = 200) -> None:
        self._data: "OrderedDict[str, dict]" = OrderedDict()
        self._lock = threading.Lock()
        self.capacity = capacity
        self._counter = 0

    def new_id(self, today: str) -> str:
        with self._lock:
            self._counter += 1
            return "t-%s-%04d" % (today.replace("-", ""), self._counter)

    def save(self, trace: Trace) -> None:
        with self._lock:
            self._data[trace.trace_id] = trace.as_dict()
            self._data.move_to_end(trace.trace_id)
            while len(self._data) > self.capacity:
                self._data.popitem(last=False)

    def get(self, trace_id: str) -> Optional[dict]:
        with self._lock:
            return self._data.get(trace_id)
