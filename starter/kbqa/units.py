"""把文档拆成可引用的最小单位（一句话或表格一行），并记住它在原文里的位置。"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .sanitize import split_sentences
from .tokenizer import tokenize

#: 契约 §5：一条 quote 不超过 400 个字符。
MAX_QUOTE = 400

_SENTENCE_TAIL = "。！？；!?;"
_LIST_START = re.compile(r"^\s*(?:[-*>|]|#{1,6}\s|\d+[.、)]|[一二三四五六七八九十]+[、.])")
#: “发布部门：总部运营部”这类字段行是独立的一条信息，不能和上一行拼成一句。
_FIELD_LINE = re.compile(r"^[^：:\s]{1,6}[：:]")


def _lines_of(text: str, fmt: str) -> list[str]:
    """把一段正文按“原文里的一段”切开；同一段里的句子可以合起来引用。"""
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if fmt == "html":
        return lines
    merged: list[str] = []
    for line in lines:
        if (
            merged
            and len(merged[-1]) >= 20
            and merged[-1][-1] not in _SENTENCE_TAIL
            and not _LIST_START.match(line)
            and not _FIELD_LINE.match(line)
            and not _FIELD_LINE.match(merged[-1])
        ):
            merged[-1] = merged[-1] + " " + line
        else:
            merged.append(line)
    return merged


@dataclass
class Unit:
    """一个可引用的最小单位：一句话，或者表格里的一行。"""

    text: str
    context: set = field(default_factory=set)
    kind: str = "text"
    header: list = field(default_factory=list)
    doc_id: str = ""
    line_id: int = -1
    """同一个 `line_id` 的单位在原文里首尾相连。"""
    start: int = -1
    end: int = -1
    """在“去掉空白的正文”里的起止位置。按位置切片取原文，引用天然是逐字连续的。"""


class UnitIndex:
    """按文档缓存“单位列表”与“去空白视图”。"""

    def __init__(self, index) -> None:
        self.index = index
        self._cache: dict[str, list[Unit]] = {}
        self._stripped: dict[str, tuple[str, list[int]]] = {}

    def stripped(self, doc_id: str) -> tuple[str, list[int]]:
        """正文去掉全部空白之后的样子，以及每个字符在原文里的下标。

        引用要跟原文逐字对得上，所以定位在这个视图上做：按位置切回原文，
        引用一定是连续的真文字，拼相邻句子也不会破坏这一点。
        """
        cached = self._stripped.get(doc_id)
        if cached is None:
            text = self.index.texts.get(doc_id, "")
            chars, positions = [], []
            for position, char in enumerate(text):
                if not char.isspace():
                    chars.append(char)
                    positions.append(position)
            cached = ("".join(chars), positions)
            self._stripped[doc_id] = cached
        return cached

    def locate(self, doc_id: str, text: str, cursor: int) -> tuple[int, int]:
        """在去空白视图里找到这句话的位置，返回 (start, end)；找不到返回 (-1, -1)。"""
        stripped, _ = self.stripped(doc_id)
        needle = re.sub(r"\s+", "", text)
        if not needle:
            return -1, -1
        position = stripped.find(needle, cursor)
        if position < 0:
            position = stripped.find(needle)
        if position < 0:
            return -1, -1
        return position, position + len(needle)

    def slice_quote(self, doc_id: str, start: int, end: int) -> str:
        """按去空白视图的位置切出原文片段。"""
        text = self.index.texts.get(doc_id, "")
        _, positions = self.stripped(doc_id)
        if start < 0 or end <= start or end > len(positions):
            return ""
        return text[positions[start] : positions[end - 1] + 1].strip()

    # -- 拆句 -------------------------------------------------------------------

    def sentences(self, doc_id: str) -> list[str]:
        return [unit.text for unit in self.units(doc_id)]

    def units(self, doc_id: str) -> list[Unit]:
        cached = self._cache.get(doc_id)
        if cached is not None:
            return cached
        units: list[Unit] = []
        seen: set[str] = set()
        cursor = 0
        index = self.index
        headings: set[str] = set()
        for chunk in index.chunks_of(doc_id):
            headings.update(part.strip() for part in chunk.heading.split(" > ") if part.strip())
        for chunk in index.chunks_of(doc_id):
            context = set(tokenize(chunk.heading))
            for canonical in index.aliases.strict_mentions(chunk.heading):
                context.update(tokenize(canonical))
            if chunk.kind == "table":
                header_tokens = set(tokenize(" ".join(chunk.table_header)))
                header_cells = [cell.strip() for cell in chunk.table_header]
                for line in chunk.source_text.splitlines():
                    stripped = line.strip()
                    if not stripped or re.fullmatch(r"\|[\s:|-]+\|", stripped):
                        continue
                    # 表头行本身不是答案，跳过。
                    if [c.strip() for c in stripped.strip("|").split("|")] == header_cells:
                        continue
                    if stripped in seen:
                        continue
                    seen.add(stripped)
                    unit = Unit(stripped, context | header_tokens, "table", chunk.table_header, doc_id)
                    unit.start, unit.end = self.locate(doc_id, stripped, cursor)
                    cursor = max(cursor, unit.end)
                    units.append(unit)
                continue
            fmt = index.docs_meta.get(doc_id, {}).get("format", "md")
            for line_id, line in enumerate(_lines_of(chunk.source_text, fmt)):
                for sentence in split_sentences(line):
                    text = sentence.strip().lstrip("#").strip()
                    if not text or text in seen:
                        continue
                    seen.add(text)
                    kind = (
                        "heading"
                        if sentence.strip().startswith("#") or text in headings
                        else "text"
                    )
                    unit = Unit(text, context, kind, [], doc_id, line_id=id(chunk) * 1000 + line_id)
                    unit.start, unit.end = self.locate(doc_id, text, cursor)
                    cursor = max(cursor, unit.end)
                    units.append(unit)
        self._cache[doc_id] = units
        return units

