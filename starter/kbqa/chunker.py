"""把文档切成检索用的小块。"""

from __future__ import annotations

from dataclasses import dataclass, field

from .loader import Document

#: 切块参数变了，索引缓存必须失效，所以写进缓存键里。
CHUNKER_VERSION = "chunker-3"

CHUNK_SIZE = 300


@dataclass
class Chunk:
    doc_id: str
    chunk_id: str
    text: str
    source_text: str
    heading: str = ""
    kind: str = "text"
    table_header: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "doc_id": self.doc_id,
            "chunk_id": self.chunk_id,
            "text": self.text,
            "source_text": self.source_text,
            "heading": self.heading,
            "kind": self.kind,
            "table_header": self.table_header,
        }


def chunk_document(document: Document) -> list[Chunk]:
    """一篇文档按固定长度切开，300 字一块，末尾不足一块的也保留。"""
    text = document.text
    if len(text) <= CHUNK_SIZE:
        piece = text.strip() or document.title
        return [
            Chunk(
                doc_id=document.doc_id,
                chunk_id="%s#1" % document.doc_id,
                text=piece,
                source_text=piece,
                heading=document.title,
            )
        ]
    chunks: list[Chunk] = []
    for number, start in enumerate(range(0, len(text), CHUNK_SIZE), start=1):
        piece = text[start : start + CHUNK_SIZE]
        chunks.append(
            Chunk(
                doc_id=document.doc_id,
                chunk_id="%s#%d" % (document.doc_id, number),
                text=piece,
                source_text=piece,
                heading=document.title,
            )
        )
    return chunks


def chunk_documents(documents: list[Document]) -> list[Chunk]:
    chunks: list[Chunk] = []
    for document in documents:
        chunks.extend(chunk_document(document))
    return chunks
