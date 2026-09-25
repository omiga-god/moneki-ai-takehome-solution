"""把原始 sales 导进 var/clean.db，指标都查这张表。"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import date
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from pathlib import Path
from typing import Iterable, Optional

_ISO_DATE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")
_SLASH_DATE = re.compile(r"^(\d{4})/(\d{1,2})/(\d{1,2})$")
_DMY_DATE = re.compile(r"^(\d{1,2})-(\d{1,2})-(\d{4})$")

REMOVAL_REASONS = (
    "1_unparseable_date",
    "2_empty_amount",
    "3_qty_le_zero",
    "4_store_not_in_stores",
    "5_product_not_in_products",
    "6_duplicate_row",
)


def parse_date(value: Optional[str]) -> Optional[str]:
    """KB-001 §2.2：三种格式都变成 `YYYY-MM-DD`。`DD-MM-YYYY` 日在前。"""
    text = (value or "").strip()
    if not text:
        return None
    iso = _ISO_DATE.fullmatch(text)
    slash = _SLASH_DATE.fullmatch(text)
    dmy = _DMY_DATE.fullmatch(text)
    if iso:
        year, month, day = (int(iso.group(1)), int(iso.group(2)), int(iso.group(3)))
    elif slash:
        year, month, day = (int(slash.group(1)), int(slash.group(2)), int(slash.group(3)))
    elif dmy:
        day, month, year = (int(dmy.group(1)), int(dmy.group(2)), int(dmy.group(3)))
    else:
        return None
    try:
        return date(year, month, day).isoformat()
    except ValueError:
        return None


def parse_amount(value: Optional[str]) -> tuple[Optional[int], str]:
    """返回 (分, 状态)。状态取值：`ok`、`empty`、`bad`。

    KB-001 §2.3 与 §3.2：去掉 `¥` 前缀和首尾空白后再解析。
    `¥38.00` 与 `38.00` 是同一个金额；空金额或无法解析的金额直接剔除，不回填。
    """
    text = (value or "").strip()
    if text[:1] in "¥￥":
        text = text[1:].strip()
    if not text:
        return None, "empty"
    try:
        cents = (Decimal(text) * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    except (InvalidOperation, ValueError):
        return None, "bad"
    return int(cents), "ok"


def parse_qty(value: Optional[str]) -> Optional[int]:
    """KB-001 §2.4：按整数解析。空值、小数和非数字返回 None，由 §3.3 剔除。"""
    text = (value or "").strip()
    if not text:
        return None
    try:
        number = Decimal(text)
    except (InvalidOperation, ValueError):
        return None
    if number != number.to_integral_value():
        return None
    return int(number)


@dataclass
class CleaningReport:
    raw_rows: int = 0
    kept_rows: int = 0
    kept_sales_rows: int = 0
    kept_refund_rows: int = 0
    removed: dict[str, int] = field(default_factory=lambda: {k: 0 for k in REMOVAL_REASONS})
    note_unparseable_amount: int = 0

    def as_dict(self) -> dict:
        return {
            "raw_rows": self.raw_rows,
            "removed": dict(self.removed, note_unparseable_amount=self.note_unparseable_amount),
            "kept_rows": self.kept_rows,
            "kept_sales_rows": self.kept_sales_rows,
            "kept_refund_rows": self.kept_refund_rows,
        }


def open_readonly(path: Path) -> sqlite3.Connection:
    """打开数据库。"""
    conn = sqlite3.connect(path.as_posix(), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def clean_rows(
    rows: Iterable[sqlite3.Row], store_ids: set[str], product_ids: set[str]
) -> tuple[list[tuple], CleaningReport]:
    """按 KB-001 §2–§3 先规范化，再按固定顺序剔除。同一行只记第一条命中的原因。"""
    report = CleaningReport()
    kept: list[tuple] = []
    seen: set[tuple] = set()
    for row in rows:
        report.raw_rows += 1
        parsed_date = parse_date(row["date"])
        if parsed_date is None:
            report.removed["1_unparseable_date"] += 1
            continue
        cents, status = parse_amount(row["amount"])
        if status != "ok":
            report.removed["2_empty_amount"] += 1
            if status == "bad":
                report.note_unparseable_amount += 1
            continue
        qty = parse_qty(row["qty"])
        if qty is None or qty <= 0:
            report.removed["3_qty_le_zero"] += 1
            continue
        store_id = (row["store_id"] or "").strip().upper()
        if store_id not in store_ids:
            report.removed["4_store_not_in_stores"] += 1
            continue
        product_id = (row["product_id"] or "").strip().upper()
        if product_id not in product_ids:
            report.removed["5_product_not_in_products"] += 1
            continue
        order_id = (row["order_id"] or "").strip()
        payment = (row["payment"] or "").strip()
        key = (order_id, parsed_date, store_id, product_id, qty, cents, payment)
        if key in seen:
            report.removed["6_duplicate_row"] += 1
            continue
        seen.add(key)
        is_refund = 1 if cents < 0 else 0
        kept.append((order_id, parsed_date, store_id, product_id, qty, cents, payment, is_refund))
        if cents < 0:
            report.kept_refund_rows += 1
        elif cents > 0:
            report.kept_sales_rows += 1
    report.kept_rows = len(kept)
    return kept, report


_SCHEMA = """
CREATE TABLE stores (store_id TEXT PRIMARY KEY, store_name TEXT, category TEXT, district TEXT);
CREATE TABLE products (product_id TEXT PRIMARY KEY, product_name TEXT,
                       product_category TEXT, unit_price REAL);
CREATE TABLE sales_clean (
    order_id TEXT, date TEXT, store_id TEXT, product_id TEXT,
    qty INTEGER, amount_cents INTEGER, payment TEXT, is_refund INTEGER
);
CREATE INDEX idx_clean_date ON sales_clean(date);
CREATE INDEX idx_clean_store ON sales_clean(store_id);
CREATE INDEX idx_clean_product ON sales_clean(product_id);
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
"""


def build_clean_db(source: Path, target: Path) -> CleaningReport:
    """从只读的源库重建清洗表。返回清洗台账，供 `/api/health` 与数据质量面板使用。"""
    if not source.exists():
        raise FileNotFoundError("找不到源数据库：%s" % source)
    src = open_readonly(source)
    try:
        stores = [tuple(r) for r in src.execute("SELECT store_id, store_name, category, district FROM stores")]
        products = [
            tuple(r)
            for r in src.execute(
                "SELECT product_id, product_name, product_category, unit_price FROM products"
            )
        ]
        store_ids = {(row[0] or "").strip().upper() for row in stores}
        product_ids = {(row[0] or "").strip().upper() for row in products}
        rows, report = clean_rows(
            src.execute("SELECT order_id, date, store_id, product_id, qty, amount, payment FROM sales"),
            store_ids,
            product_ids,
        )
    finally:
        src.close()

    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        target.unlink()
    out = sqlite3.connect(target)
    try:
        out.executescript(_SCHEMA)
        out.executemany("INSERT INTO stores VALUES (?,?,?,?)", stores)
        out.executemany("INSERT INTO products VALUES (?,?,?,?)", products)
        out.executemany("INSERT INTO sales_clean VALUES (?,?,?,?,?,?,?,?)", rows)
        out.execute(
            "INSERT INTO meta VALUES ('cleaning_report', ?)",
            (json.dumps(report.as_dict(), ensure_ascii=False),),
        )
        out.execute("INSERT INTO meta VALUES ('source_db', ?)", (source.name,))
        out.commit()
    finally:
        out.close()
    return report
