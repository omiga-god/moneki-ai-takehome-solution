"""对外响应的五个字段，单独放一个文件避免循环引用。"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Answer:
    answer: str
    answer_type: str
    """data / doc / hybrid / refusal / clarify 五选一。"""
    citations: list[dict] = field(default_factory=list)
    data_evidence: list[dict] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
