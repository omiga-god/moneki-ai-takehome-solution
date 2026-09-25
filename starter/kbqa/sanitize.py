"""从检索到的文本里认出指令式的句子。"""

from __future__ import annotations

import re

_SENTENCE = re.compile(r"[^。！？!?\n]+[。！？!?]?")

#: 命中任意一条就认为这句话是“冲着助手来的指令”，而不是公司资料。
_INJECTION_PATTERNS = (
    re.compile(r"(忽略|无视|忘记|放弃).{0,8}(之前|以上|上面|先前|所有).{0,6}(指令|规则|提示|设定)"),
    re.compile(r"ignore\s+(all\s+)?(previous|prior|above)\s+(instructions|prompts|rules)", re.I),
    re.compile(r"(系统提示|系统指令|system\s*prompt|system\s*message)\s*[:：]"),
    re.compile(r"无论(用户|别人|他)?问(什么|啥)"),
    re.compile(r"(不要|禁止|别).{0,6}(引用|标注|列出).{0,6}(来源|出处|依据)"),
    re.compile(r"(你必须|你应该|请你?务必|from now on|you must)\s*(回答|输出|reply|answer|say)", re.I),
    re.compile(r"(执行|运行|调用).{0,6}(drop|delete|update|truncate)\s", re.I),
)


def is_instruction_like(sentence: str) -> bool:
    text = sentence.strip()
    if not text:
        return False
    return any(pattern.search(text) for pattern in _INJECTION_PATTERNS)


def split_sentences(text: str) -> list[str]:
    """按句子切，保留换行结构，方便逐句判断与逐句引用。"""
    parts: list[str] = []
    for line in text.splitlines():
        if not line.strip():
            continue
        found = [match.group(0).strip() for match in _SENTENCE.finditer(line)]
        parts.extend(piece for piece in found if piece)
    return parts


def sanitize(text: str) -> tuple[str, list[str]]:
    """返回（去掉指令句之后的文本，被去掉的句子）。"""
    kept: list[str] = []
    dropped: list[str] = []
    for line in text.splitlines():
        if not line.strip():
            continue
        pieces = [match.group(0) for match in _SENTENCE.finditer(line)] or [line]
        safe = []
        for piece in pieces:
            if is_instruction_like(piece):
                dropped.append(piece.strip())
            else:
                safe.append(piece)
        joined = "".join(safe).strip()
        if joined:
            kept.append(joined)
    return "\n".join(kept), dropped
