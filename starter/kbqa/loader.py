"""读知识库目录，把文件变成文档对象，顺手认出编号、标题、生效日期。"""

from __future__ import annotations

import html as html_module
import re
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Optional

SUPPORTED_SUFFIXES = {".md", ".markdown"}

#: 文件名开头的编号就是 doc_id，与文件格式无关（契约 §0）。
_DOC_ID = re.compile(r"^(KB-\d+)")
_FRONT_MATTER = re.compile(r"^---\s*\n(.*?)\n---\s*\n?", re.S)
_STORE_CODE = re.compile(r"\bS\d{2}\b")

#: 正文里的生效日期：优先“自 2026 年 8 月 15 日起”“生效日期：2026-07-01”这类明确写法。
_CN_DATE = r"(\d{4})\s*[-/年]\s*(\d{1,2})\s*[-/月]\s*(\d{1,2})\s*日?"
_EFFECTIVE_PATTERNS = (
    re.compile(r"(?:自|从)\s*" + _CN_DATE + r"\s*(?:起|开始)"),
    re.compile(r"生效(?:日期)?[：: ]\s*" + _CN_DATE),
    re.compile(r"(?:执行|实施)(?:日期)?[：: ]\s*" + _CN_DATE),
)
_ANY_DATE = re.compile(_CN_DATE)
_EMAIL_DATE = re.compile(r"^Date:\s*(.+)$", re.M)
_MONTHS = "jan feb mar apr may jun jul aug sep oct nov dec".split()

#: KB-001 §5.2：周报、会议纪要、活动复盘里的数字是人工估算，不能当答案。
ESTIMATE_TYPES = {"周报", "会议纪要", "复盘", "活动复盘"}


@dataclass
class Document:
    doc_id: str
    title: str
    text: str
    path: Path
    fmt: str
    doc_type: str = ""
    status: str = "现行"
    effective_from: Optional[date] = None
    superseded_by: Optional[str] = None
    stores: list[str] = field(default_factory=list)
    #: `stores` 是不是文档自己声明的。正文里认出来的门店只能当线索，
    #: 不能当硬过滤条件——KB-001 的正文里就举了 `s01` 当例子。
    stores_explicit: bool = False
    updated_at: Optional[date] = None
    warnings: list[str] = field(default_factory=list)

    @property
    def estimates_only(self) -> bool:
        return self.doc_type in ESTIMATE_TYPES

    @property
    def title_year(self) -> Optional[int]:
        match = re.search(r"(20\d{2})", self.title)
        return int(match.group(1)) if match else None

    def meta(self) -> dict:
        return {
            "doc_id": self.doc_id,
            "title": self.title,
            "type": self.doc_type,
            "state": self.status,
            "effective_from": self.effective_from.isoformat() if self.effective_from else None,
            "superseded_by": self.superseded_by,
            "stores": self.stores,
            "stores_explicit": self.stores_explicit,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
            "estimates_only": self.estimates_only,
            "title_year": self.title_year,
            "format": self.fmt,
            "filename": self.path.name,
        }


_HTML_TITLE = re.compile(r"<title>(.*?)</title>", re.S | re.I)


def decode_bytes(raw: bytes, path: Path, warnings: list[str]) -> str:
    """统一按 UTF-8 读。个别老文件里有怪字符，忽略掉就行，不影响检索。"""
    return raw.decode("utf-8", errors="ignore")


def parse_front_matter(text: str) -> tuple[dict, str]:
    """极简 YAML 头解析：`key: value` 与 `key: [a, b]`，够用且不引依赖。"""
    match = _FRONT_MATTER.match(text)
    if not match:
        return {}, text
    meta: dict = {}
    for line in match.group(1).splitlines():
        if not line.strip() or line.lstrip().startswith("#") or ":" not in line:
            continue
        key, _, value = line.partition(":")
        value = value.strip()
        if value.startswith("[") and value.endswith("]"):
            meta[key.strip()] = [
                item.strip().strip("'\"") for item in value[1:-1].split(",") if item.strip()
            ]
        else:
            meta[key.strip()] = value.strip().strip("'\"")
    return meta, text[match.end():]


def _as_date(value) -> Optional[date]:
    if isinstance(value, date):
        return value
    text = str(value or "").strip()
    match = _ANY_DATE.search(text)
    if not match:
        return None
    try:
        return date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
    except ValueError:
        return None


def _effective_from_body(text: str) -> Optional[date]:
    """没有 YAML 头时，从正文里认生效日期。"""
    for pattern in _EFFECTIVE_PATTERNS:
        match = pattern.search(text)
        if match:
            try:
                return date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
            except ValueError:
                continue
    mail = _EMAIL_DATE.search(text)
    if mail:
        parts = mail.group(1).replace(",", " ").split()
        day = month = year = None
        for token in parts:
            low = token.lower()[:3]
            if low in _MONTHS:
                month = _MONTHS.index(low) + 1
            elif token.isdigit() and len(token) == 4:
                year = int(token)
            elif token.isdigit() and len(token) <= 2:
                day = int(token)
        if day and month and year:
            return date(year, month, day)
    match = _ANY_DATE.search(text)
    if match:
        try:
            return date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
        except ValueError:
            return None
    return None


def _title_from_body(text: str, fallback: str) -> str:
    for line in text.splitlines():
        stripped = line.strip().lstrip("#").strip()
        if not stripped or set(stripped) <= set("=-*_ "):
            continue
        if stripped.startswith(("From:", "To:", "Cc:", "Date:")):
            continue
        if stripped.startswith("Subject:"):
            return stripped.split(":", 1)[1].strip()
        if stripped.startswith("标题：") or stripped.startswith("标题:"):
            return stripped.split("：", 1)[-1].split(":", 1)[-1].strip()
        return stripped[:80]
    return fallback


def load_document(path: Path) -> Optional[Document]:
    """读一个文件。不是知识库文档（没有 KB 编号）时返回 None。"""
    warnings: list[str] = []
    raw = path.read_bytes()
    text = decode_bytes(raw, path, warnings)
    suffix = path.suffix.lower()
    fmt = {".md": "md", ".markdown": "md", ".txt": "txt"}.get(suffix, "html")

    meta: dict = {}
    if fmt == "md":
        meta, text = parse_front_matter(text)
    elif fmt == "html":
        # html 直接按文本入库，标签也就那么几个，BM25 自己会忽略。
        match_title = _HTML_TITLE.search(text)
        html_title = html_module.unescape(match_title.group(1).strip()) if match_title else ""
        meta = {"title": html_title.split("-")[0].strip() or html_title}

    match = _DOC_ID.match(path.name)
    doc_id = str(meta.get("doc_id") or (match.group(1) if match else "")).strip()
    if not doc_id:
        return None

    declared = meta.get("stores")
    stores = declared or _sorted_unique(_STORE_CODE.findall(text))
    doc_type = str(meta.get("type") or "").strip()
    if not doc_type:
        doc_type = _guess_type(path.name, text)
    return Document(
        doc_id=doc_id,
        title=str(meta.get("title") or _title_from_body(text, path.stem)).strip(),
        text=text.strip("\n"),
        path=path,
        fmt=fmt,
        doc_type=doc_type,
        status=str(meta.get("status") or "现行").strip(),
        effective_from=_as_date(meta.get("effective_from")) or _effective_from_body(text),
        superseded_by=(str(meta.get("superseded_by")).strip() if meta.get("superseded_by") else None),
        stores=[s.upper() for s in stores],
        stores_explicit=bool(declared),
        updated_at=_as_date(meta.get("updated_at")),
        warnings=warnings,
    )


def _sorted_unique(values) -> list[str]:
    return sorted({value.upper() for value in values})


def _guess_type(filename: str, text: str) -> str:
    head = filename + " " + text[:200]
    for marker in ("周报", "通知", "政策", "手册", "纪要", "报告", "FAQ", "档案"):
        if marker in head:
            return marker
    return "文档"


def load_knowledge_base(kb_dir: Path) -> tuple[list[Document], list[str]]:
    """加载整个知识库目录，返回（文档列表，告警列表）。"""
    documents: list[Document] = []
    warnings: list[str] = []
    seen: dict[str, Path] = {}
    if not kb_dir.exists():
        return documents, ["知识库目录不存在：%s" % kb_dir]
    for path in sorted(kb_dir.rglob("*")):
        if not path.is_file() or path.name.startswith("."):
            continue
        if path.suffix.lower() not in SUPPORTED_SUFFIXES:
            continue
        document = load_document(path)
        if document is None:
            warnings.append("跳过没有 KB 编号的文件：%s" % path.name)
            continue
        if document.doc_id in seen:
            warnings.append(
                "doc_id 重复：%s 同时出现在 %s 与 %s"
                % (document.doc_id, seen[document.doc_id].name, path.name)
            )
            continue
        seen[document.doc_id] = path
        warnings.extend(document.warnings)
        documents.append(document)
    return documents, warnings
