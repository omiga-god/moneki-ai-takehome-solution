"""KB-001 清洗与指标回归。

数字都来自本文件里的小样本，不引用公开题库的期望值。
修复前：规范化、按序剔除、退款、订单数、闭区间和 health 计数都对不上。
"""

from __future__ import annotations

import sqlite3
from datetime import date
from pathlib import Path

import pytest

from kbqa.cleaning import build_clean_db
from kbqa.config import Settings
from kbqa.service import Service
from kbqa.tools import DataTools


def _write_source(path: Path, rows: list[tuple], stores=None, products=None) -> None:
    stores = stores or [
        ("S01", "甲店", "面", "东"),
        ("S02", "乙店", "饭", "西"),
    ]
    products = products or [
        ("P01", "牛肉面", "面", 32.0),
        ("P06", "牛肉poke", "饭", 48.0),
    ]
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE stores (store_id TEXT, store_name TEXT, category TEXT, district TEXT)"
    )
    conn.execute(
        "CREATE TABLE products (product_id TEXT, product_name TEXT, "
        "product_category TEXT, unit_price REAL)"
    )
    conn.execute(
        "CREATE TABLE sales (order_id TEXT, date TEXT, store_id TEXT, product_id TEXT, "
        "qty TEXT, amount TEXT, payment TEXT)"
    )
    conn.executemany("INSERT INTO stores VALUES (?,?,?,?)", stores)
    conn.executemany("INSERT INTO products VALUES (?,?,?,?)", products)
    conn.executemany("INSERT INTO sales VALUES (?,?,?,?,?,?,?)", rows)
    conn.commit()
    conn.close()


def _build(tmp_path: Path, rows: list[tuple], **kwargs):
    tmp_path.mkdir(parents=True, exist_ok=True)
    source = tmp_path / "pos.db"
    target = tmp_path / "clean.db"
    _write_source(source, rows, **kwargs)
    report = build_clean_db(source, target)
    return report, DataTools(target), target


def _kept(target: Path) -> list[sqlite3.Row]:
    conn = sqlite3.connect(target)
    conn.row_factory = sqlite3.Row
    rows = list(conn.execute("SELECT * FROM sales_clean ORDER BY date, order_id, product_id"))
    conn.close()
    return rows


def test_normalize_store_and_product_before_foreign_key(tmp_path):
    """大小写和空白要先规范化，再判断外键；未知编号才剔除。"""
    _, tools, target = _build(
        tmp_path,
        [
            ("A1", "2026-06-01", " s01 ", "p06", "1", "48.00", "现金"),
            ("A2", "2026-06-01", "S99", "P01", "1", "32.00", "现金"),
            ("A3", "2026-06-01", "S01", "P99", "1", "32.00", "现金"),
        ],
    )
    kept = _kept(target)
    assert [(row["order_id"], row["store_id"], row["product_id"]) for row in kept] == [
        ("A1", "S01", "P06")
    ]
    metrics = tools.query_metrics("2026-06-01", "2026-06-01", store_id="s01")
    assert metrics["net_revenue"] == 48.0
    assert metrics["orders"] == 1


def test_date_formats_and_day_first_dmy(tmp_path):
    """YYYY/M/D 与 DD-MM-YYYY（日在前）都要变成 ISO；无法解析的日期先剔除。"""
    report, tools, target = _build(
        tmp_path,
        [
            ("D1", "25-07-2026", "S01", "P01", "1", "10.00", "现金"),
            ("D2", "07-06-2026", "S01", "P01", "1", "8.00", "现金"),
            ("D3", "2026/5/3", "S01", "P01", "2", "16.00", "现金"),
            ("D4", "N/A", "S01", "P01", "1", "99.00", "现金"),
            ("D5", "", "S01", "P01", "1", "99.00", "现金"),
            ("D6", "2026/02/31", "S01", "P01", "1", "99.00", "现金"),
        ],
    )
    assert report.removed["1_unparseable_date"] == 3
    assert sorted(row["date"] for row in _kept(target)) == [
        "2026-05-03",
        "2026-06-07",
        "2026-07-25",
    ]
    june = tools.query_metrics("2026-06-07", "2026-06-07")
    july_wrong = tools.query_metrics("2026-07-06", "2026-07-06")
    assert june["net_revenue"] == 8.0
    assert july_wrong["net_revenue"] == 0.0
    assert july_wrong["orders"] == 0


def test_empty_and_bad_amount_dropped_yen_amount_kept(tmp_path):
    """空金额不回填；带 ¥ 的金额去掉符号后保留，并与裸数字视为同一金额。"""
    report, tools, _ = _build(
        tmp_path,
        [
            ("Y1", "2026-06-01", "S01", "P01", "2", "¥38.00", "现金"),
            ("Y1", "2026-06-01", "S01", "P01", "2", " ¥38.00 ", "现金"),
            ("Y2", "2026-06-01", "S01", "P01", "2", "   ", "现金"),
            ("Y3", "2026-06-01", "S01", "P01", "2", "", "现金"),
            ("Y4", "2026-06-01", "S01", "P01", "2", "abc", "现金"),
            ("Y5", "2026-06-01", "S01", "P06", "1", "¥ 12.00", "微信"),
        ],
        products=[
            ("P01", "牛肉面", "面", 32.0),
            ("P06", "牛肉poke", "饭", 48.0),
        ],
    )
    assert report.removed["2_empty_amount"] == 3
    assert report.removed["6_duplicate_row"] == 1
    assert report.kept_rows == 2
    metrics = tools.query_metrics("2026-06-01", "2026-06-01")
    # 不能用 qty × unit_price 把空金额回填成 64。
    assert metrics["net_revenue"] == 50.0
    assert metrics["orders"] == 2
    assert metrics["qty"] == 3


def test_non_positive_and_non_integer_qty_dropped(tmp_path):
    report, _, target = _build(
        tmp_path,
        [
            ("Q1", "2026-06-01", "S01", "P01", "0", "10.00", "现金"),
            ("Q2", "2026-06-01", "S01", "P01", "-1", "10.00", "现金"),
            ("Q3", "2026-06-01", "S01", "P01", "1.5", "10.00", "现金"),
            ("Q4", "2026-06-01", "S01", "P01", "", "10.00", "现金"),
            ("Q5", "2026-06-01", "S01", "P01", "2", "10.00", "现金"),
        ],
    )
    assert report.removed["3_qty_le_zero"] == 4
    assert [row["order_id"] for row in _kept(target)] == ["Q5"]


def test_duplicate_removed_but_multiline_order_kept(tmp_path):
    report, tools, target = _build(
        tmp_path,
        [
            ("M1", "2026-06-01", "S01", "P01", "1", "10.00", "现金"),
            ("M1", "2026-06-01", "S01", "P01", "1", "10.00", "现金"),
            ("M1", "2026-06-01", "S01", "P01", "1", "10.00", "微信"),
            ("M1", "2026-06-01", "S01", "P06", "2", "20.00", "现金"),
        ],
    )
    assert report.removed["6_duplicate_row"] == 1
    assert report.kept_rows == 3
    assert sorted(row["product_id"] for row in _kept(target)) == ["P01", "P01", "P06"]
    metrics = tools.query_metrics("2026-06-01", "2026-06-01")
    assert metrics["orders"] == 1
    assert metrics["net_revenue"] == 40.0
    assert metrics["qty"] == 4
    assert metrics["aov"] == 40.0


def test_removal_stops_at_the_first_failing_rule(tmp_path):
    """同一行只记最先命中的剔除原因，后面的规则不再计数。"""
    report, _, _ = _build(
        tmp_path,
        [
            ("R1", "N/A", "S99", "P99", "0", "", "现金"),
            ("R2", "2026-06-01", "S99", "P99", "0", "", "现金"),
        ],
    )
    assert report.removed["1_unparseable_date"] == 1
    assert report.removed["2_empty_amount"] == 1
    assert report.removed["3_qty_le_zero"] == 0
    assert report.removed["4_store_not_in_stores"] == 0
    assert report.removed["5_product_not_in_products"] == 0
    assert report.kept_rows == 0


def test_refund_net_orders_qty_and_aov(tmp_path):
    """净额含退款，退款金额取绝对值，订单只数销售行，销量是销售数量减退款数量。"""
    _, tools, _ = _build(
        tmp_path,
        [
            ("S1", "2026-06-01", "S01", "P01", "2", "40.00", "现金"),
            ("S2", "2026-06-02", "S01", "P01", "1", "-15.00", "现金"),
            ("S3", "2026-06-02", "S01", "P06", "1", "0.02", "现金"),
            ("S4", "2026-06-02", "S01", "P06", "1", "0.03", "微信"),
        ],
    )
    metrics = tools.query_metrics("2026-06-01", "2026-06-02")
    # 40 - 15 + 0.02 + 0.03。当前实现会丢掉结束日和退款，得到 40。
    assert metrics["net_revenue"] == pytest.approx(25.05)
    assert metrics["refund_amount"] == 15.0
    assert metrics["orders"] == 3
    assert metrics["qty"] == 3
    assert metrics["aov"] == pytest.approx(8.35)
    half = tools.query_metrics("2026-06-02", "2026-06-02", product_id="P06")
    # 0.05 / 2 = 0.025，四舍五入到 0.03，不能用银行家舍入得到 0.02。
    assert half["net_revenue"] == pytest.approx(0.05)
    assert half["orders"] == 2
    assert half["aov"] == pytest.approx(0.03)
    refund_only = tools.query_metrics("2026-06-02", "2026-06-02", product_id="P01")
    assert refund_only["net_revenue"] == -15.0
    assert refund_only["refund_amount"] == 15.0
    assert refund_only["orders"] == 0
    assert refund_only["qty"] == -1
    assert refund_only["aov"] is None


def test_summary_range_is_closed_on_both_ends(tmp_path):
    _, tools, _ = _build(
        tmp_path,
        [
            ("E0", "2026-06-01", "S01", "P01", "1", "5.00", "现金"),
            ("E1", "2026-06-30", "S01", "P01", "1", "7.00", "现金"),
        ],
    )
    metrics = tools.query_metrics("2026-06-01", "2026-06-30")
    assert metrics["net_revenue"] == 12.0
    assert metrics["orders"] == 2
    end_only = tools.query_metrics("2026-06-30", "2026-06-30")
    assert end_only["net_revenue"] == 7.0
    assert end_only["orders"] == 1


def test_daily_includes_every_day_and_the_end_date(tmp_path):
    _, tools, _ = _build(
        tmp_path,
        [("E1", "2026-06-03", "S01", "P01", "1", "7.00", "现金")],
    )
    days = tools.daily_metrics("2026-06-01", "2026-06-03")["days"]
    assert [day["date"] for day in days] == ["2026-06-01", "2026-06-02", "2026-06-03"]
    assert days[0]["net_revenue"] == 0.0
    assert days[0]["orders"] == 0
    assert days[0]["aov"] is None
    assert days[2]["net_revenue"] == 7.0
    assert days[2]["orders"] == 1
    assert days[2]["aov"] == 7.0


def test_empty_range_returns_zeros_and_null_aov(tmp_path):
    _, tools, _ = _build(
        tmp_path,
        [("E1", "2026-06-01", "S01", "P01", "1", "7.00", "现金")],
    )
    metrics = tools.query_metrics("2026-09-01", "2026-09-30")
    assert metrics["net_revenue"] == 0.0
    assert metrics["refund_amount"] == 0.0
    assert metrics["orders"] == 0
    assert metrics["qty"] == 0
    assert metrics["aov"] is None


def test_rebuilt_metrics_follow_the_source_rows(tmp_path):
    """换一份输入再重建，结果跟着变，不保留上一份数据里的金额。"""
    _, first, _ = _build(tmp_path / "first", [("A", "2026-06-01", "S01", "P01", "1", "10.00", "现金")])
    assert first.query_metrics("2026-06-01", "2026-06-01")["net_revenue"] == 10.0
    _, second, _ = _build(
        tmp_path / "second",
        [
            ("A", "2026-06-01", "S01", "P01", "1", "10.00", "现金"),
            ("A", "2026-06-01", "S01", "P01", "1", "10.00", "现金"),
            ("B", "2026-06-01", "S01", "P01", "1", "4.00", "现金"),
        ],
    )
    # 完全相同的第二行被去掉，净额是 14，不是 24，也不是上一轮的 10。
    assert second.query_metrics("2026-06-01", "2026-06-01")["net_revenue"] == 14.0


def test_data_period_and_valid_rows_ignore_unusable_source_rows(tmp_path):
    report, tools, _ = _build(
        tmp_path,
        [
            ("P1", "", "S01", "P01", "1", "10.00", "现金"),
            ("P2", "N/A", "S01", "P01", "1", "10.00", "现金"),
            ("P3", "2026/5/3", "S01", "P01", "1", "10.00", "现金"),
            ("P4", "31-08-2026", "S01", "P01", "1", "-4.00", "现金"),
            ("P5", "2026-06-01", "S01", "P01", "1", "0.00", "现金"),
        ],
    )
    assert report.kept_sales_rows == 1
    assert report.kept_refund_rows == 1
    assert tools.valid_sales_rows() == 2
    assert tools.data_period() == {"start": "2026-05-03", "end": "2026-08-31"}


def _mini_kb(root: Path) -> Path:
    kb = root / "kb"
    (kb / "handbook").mkdir(parents=True)
    (kb / "notices").mkdir()
    (kb / "README.md").write_text("这是说明，不是 KB 文档。\n", encoding="utf-8")
    (kb / "notes.txt").write_text("没有编号的备注。\n", encoding="utf-8")
    (kb / "handbook" / "KB-001_口径.md").write_text(
        "---\ndoc_id: KB-001\ntitle: 口径\n---\n\n净营业额含退款。\n",
        encoding="utf-8",
    )
    (kb / "notices" / "KB-014_note.txt").write_text("临时通知正文。\n", encoding="utf-8")
    (kb / "notices" / "KB-015_faq.html").write_text(
        "<html><title>常见问题</title><body><p>外卖可以退款。</p></body></html>",
        encoding="utf-8",
    )
    return kb


def test_health_counts_indexed_docs_and_cleaned_sales(tmp_path, monkeypatch):
    data = tmp_path / "data"
    data.mkdir()
    _write_source(
        data / "pos.db",
        [
            ("P1", "N/A", "S01", "P01", "1", "99.00", "现金"),
            ("P2", "2026/6/1", "S01", "P01", "1", "11.00", "现金"),
            ("P3", "2026-06-02", "S01", "P01", "1", "-2.00", "现金"),
        ],
    )
    kb = _mini_kb(tmp_path)
    monkeypatch.setenv("INDEX_PATH", str(tmp_path / "index.json"))
    settings = Settings(
        data_dir=data,
        kb_dir=kb,
        var_dir=tmp_path / "var",
        today=date(2026, 9, 1),
        llm_base_url="",
        llm_api_key="",
        llm_model="",
        llm_timeout=120.0,
        chat_budget=150.0,
    )
    body = Service(settings).health()
    assert body["status"] == "ok"
    assert body["kb_docs"] == 3
    assert body["valid_sales_rows"] == 2
    assert body["data_period"] == {"start": "2026-06-01", "end": "2026-06-02"}
