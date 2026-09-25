"""指标查询，全部走清洗表。"""

from __future__ import annotations

import sqlite3
import threading
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any, Optional

from .cleaning import open_readonly

METRIC_FIELDS = ("net_revenue", "refund_amount", "orders", "aov", "qty")


def yuan(cents: int) -> float:
    """分转元，保留 2 位小数。"""
    return float(Decimal(cents) / 100)


def round2(value: Decimal) -> float:
    """四舍五入保留 2 位（KB-001 §4 的客单价口径，不用银行家舍入）。"""
    return float(value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


class DataTools:
    """清洗表之上的一组只读工具。"""

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        # SQLite 连接绑定线程，而 FastAPI 的同步接口跑在线程池里，
        # 所以每个线程各持一条只读连接。
        self._local = threading.local()

    # -- 基础设施 ---------------------------------------------------------------

    @property
    def conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = open_readonly(self.db_path)
            self._local.conn = conn
        return conn

    def close(self) -> None:
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None

    def _where(self, start: str, end: str, store_id=None, product_id=None) -> tuple[str, list]:
        clause = ["date >= ?", "date < ?"]
        params: list[Any] = [start, end]
        if store_id:
            clause.append("store_id = ?")
            params.append(store_id.strip().upper())
        if product_id:
            clause.append("product_id = ?")
            params.append(product_id.strip().upper())
        return " AND ".join(clause), params

    # -- 元信息 -----------------------------------------------------------------

    def cleaning_report(self) -> dict:
        row = self.conn.execute("SELECT value FROM meta WHERE key='cleaning_report'").fetchone()
        import json

        return json.loads(row[0]) if row else {}

    def valid_sales_rows(self) -> int:
        return int(self.conn.execute("SELECT COUNT(*) FROM sales_clean").fetchone()[0])

    def run_sql(self, sql: str) -> dict:
        """执行一条 SQL。工具覆盖不到的查法，让模型自己写。"""
        cursor = self.conn.execute(sql)
        rows = [dict(row) for row in cursor.fetchall()] if cursor.description else []
        self.conn.commit()
        return {"sql": sql, "rows": rows[:50], "row_count": len(rows)}

    def stores(self) -> list[dict]:
        return [dict(r) for r in self.conn.execute("SELECT * FROM stores ORDER BY store_id")]

    def products(self) -> list[dict]:
        return [dict(r) for r in self.conn.execute("SELECT * FROM products ORDER BY product_id")]

    def data_period(self) -> dict:
        row = self.conn.execute("SELECT MIN(date), MAX(date) FROM sales_clean").fetchone()
        return {"start": row[0], "end": row[1]}

    # -- 指标 -------------------------------------------------------------------

    def query_metrics(self, start: str, end: str, store_id=None, product_id=None) -> dict:
        """营业额、退款、订单数、客单价、销量。客单价 = 营业额 ÷ 明细行数。"""
        where, params = self._where(start, end, store_id, product_id)
        # 退款行不是营业，直接排掉，省得把营业额算少了。
        row = self.conn.execute(
            """
            SELECT COALESCE(SUM(amount_cents), 0),
                   0,
                   COUNT(*),
                   COALESCE(SUM(qty), 0)
            FROM sales_clean WHERE %s AND is_refund = 0
            """
            % where,
            params,
        ).fetchone()
        net_cents, refund_cents, orders, qty = int(row[0]), int(row[1]), int(row[2]), int(row[3])
        aov = round2(Decimal(net_cents) / 100 / orders) if orders else None
        return {
            "start": start,
            "end": end,
            "store_id": store_id,
            "product_id": product_id,
            "net_revenue": yuan(net_cents),
            "refund_amount": yuan(-refund_cents),
            "orders": orders,
            "aov": aov,
            "qty": qty,
        }

    def daily_metrics(self, start: str, end: str, store_id=None, product_id=None) -> dict:
        """区间内每一天都要有一条记录，没有营业额的日期也要出现。"""
        where, params = self._where(start, end, store_id, product_id)
        rows = self.conn.execute(
            """
            SELECT date,
                   COALESCE(SUM(amount_cents), 0),
                   COUNT(DISTINCT CASE WHEN is_refund=0 THEN order_id END)
            FROM sales_clean WHERE %s GROUP BY date
            """
            % where,
            params,
        ).fetchall()
        found = {r[0]: (int(r[1]), int(r[2])) for r in rows}
        days = []
        cursor = date.fromisoformat(start)
        last = date.fromisoformat(end)
        while cursor <= last:
            key = cursor.isoformat()
            net_cents, orders = found.get(key, (0, 0))
            days.append(
                {
                    "date": key,
                    "net_revenue": yuan(net_cents),
                    "orders": orders,
                    "aov": round2(Decimal(net_cents) / 100 / orders) if orders else None,
                }
            )
            cursor += timedelta(days=1)
        return {"days": days}

    def payment_mix(self, start: str, end: str, store_id=None) -> dict:
        """各支付方式的订单数、金额与占比。"""
        where, params = self._where(start, end, store_id)
        rows = self.conn.execute(
            """
            SELECT payment,
                   COUNT(DISTINCT CASE WHEN is_refund=0 THEN order_id END),
                   COALESCE(SUM(amount_cents), 0),
                   COALESCE(SUM(CASE WHEN is_refund=0 THEN qty ELSE -qty END), 0)
            FROM sales_clean WHERE %s GROUP BY payment
            """
            % where,
            params,
        ).fetchall()
        total = self.query_metrics(start, end, store_id)
        total_orders = total["orders"]
        total_net = total["net_revenue"]
        payments = {}
        for payment, orders, cents, qty in rows:
            payments[payment] = {
                "orders": int(orders),
                "qty": int(qty),
                "net_revenue": yuan(int(cents)),
                "share_orders": round(orders / total_orders, 6) if total_orders else 0.0,
                "share_revenue": round(yuan(int(cents)) / total_net, 6) if total_net else 0.0,
            }
        return {
            "start": start,
            "end": end,
            "store_id": store_id,
            "total_orders": total_orders,
            "total_net_revenue": total_net,
            "payments": payments,
        }

    def top_products(self, start: str, end: str, store_id=None, limit: int = 10) -> dict:
        where, params = self._where(start, end, store_id)
        rows = self.conn.execute(
            """
            SELECT s.product_id, p.product_name, p.product_category,
                   COALESCE(SUM(s.amount_cents), 0),
                   COUNT(DISTINCT CASE WHEN s.is_refund=0 THEN s.order_id END),
                   COALESCE(SUM(CASE WHEN s.is_refund=0 THEN s.qty ELSE -s.qty END), 0)
            FROM sales_clean s LEFT JOIN products p ON p.product_id = s.product_id
            WHERE %s GROUP BY s.product_id ORDER BY 4 DESC
            """
            % where,
            params,
        ).fetchall()
        items = [
            {
                "product_id": r[0],
                "product_name": r[1],
                "product_category": r[2],
                "net_revenue": yuan(int(r[3])),
                "orders": int(r[4]),
                "qty": int(r[5]),
            }
            for r in rows
        ]
        return {"start": start, "end": end, "store_id": store_id, "products": items[: max(1, limit)]}

    def by_store(self, start: str, end: str, product_id=None) -> dict:
        stores = []
        for store in self.stores():
            metrics = self.query_metrics(start, end, store["store_id"], product_id)
            metrics.update(
                store_name=store["store_name"],
                category=store["category"],
                district=store["district"],
            )
            stores.append(metrics)
        stores.sort(key=lambda item: item["net_revenue"], reverse=True)
        return {"start": start, "end": end, "stores": stores}

    def by_store_category(self, start: str, end: str) -> dict:
        """按门店品类汇总（每家店的 category 来自 stores 维表）。"""
        groups: dict[str, dict] = {}
        for store in self.stores():
            metrics = self.query_metrics(start, end, store["store_id"])
            bucket = groups.setdefault(
                store["category"],
                {
                    "category": store["category"],
                    "stores": [],
                    "net_revenue": 0.0,
                    "refund_amount": 0.0,
                    "orders": 0,
                    "qty": 0,
                },
            )
            bucket["stores"].append(store["store_id"])
            bucket["net_revenue"] = round(bucket["net_revenue"] + metrics["net_revenue"], 2)
            bucket["refund_amount"] = round(bucket["refund_amount"] + metrics["refund_amount"], 2)
            bucket["orders"] += metrics["orders"]
            bucket["qty"] += metrics["qty"]
        items = []
        for bucket in groups.values():
            bucket["aov"] = (
                round2(Decimal(str(bucket["net_revenue"])) / bucket["orders"])
                if bucket["orders"]
                else None
            )
            items.append(bucket)
        items.sort(key=lambda item: item["net_revenue"], reverse=True)
        return {"start": start, "end": end, "categories": items}

    def first_sale_date(self, product_id: str) -> Optional[str]:
        row = self.conn.execute(
            "SELECT MIN(date) FROM sales_clean WHERE product_id = ? AND is_refund = 0",
            (product_id.strip().upper(),),
        ).fetchone()
        return row[0] if row and row[0] else None

    def unit_price_check(self, product_id: str, start: str, end: str, store_id=None) -> dict:
        """实收单价 vs 维表建档价（KB-001 §5.3 的维表滞后）。

        同一天不同门店可能卖不同的价（活动只在一家店做），所以按门店也拆一份。
        """
        product_id = (product_id or "").strip().upper()
        params: list = [product_id, start, end]
        clause = ""
        if store_id:
            clause = " AND store_id = ?"
            params.append(store_id.strip().upper())
        rows = self.conn.execute(
            """
            SELECT date, amount_cents, qty, store_id FROM sales_clean
            WHERE product_id = ? AND is_refund = 0 AND qty > 0 AND date >= ? AND date <= ?%s
            ORDER BY date
            """
            % clause,
            params,
        ).fetchall()
        prices: dict[str, int] = {}
        by_store: dict[str, dict[str, int]] = {}
        latest_price = None
        latest_date = None
        for day, cents, qty, store in rows:
            price = yuan(int(round(int(cents) / int(qty))))
            key = "%.2f" % price
            prices[key] = prices.get(key, 0) + 1
            bucket = by_store.setdefault(store, {})
            bucket[key] = bucket.get(key, 0) + 1
            if latest_date is None or day >= latest_date:
                latest_date, latest_price = day, price
        table_price = None
        row = self.conn.execute(
            "SELECT unit_price FROM products WHERE product_id = ?", (product_id,)
        ).fetchone()
        if row:
            table_price = float(row[0])
        return {
            "product_id": product_id,
            "start": start,
            "end": end,
            "store_id": store_id,
            "observed_unit_prices": prices,
            "by_store": by_store if len(prices) > 1 else {},
            "rows": len(rows),
            "latest_price": latest_price,
            "latest_date": latest_date,
            "table_unit_price": table_price,
        }

    def compare_periods(
        self,
        start_a: str,
        end_a: str,
        start_b: str,
        end_b: str,
        store_id=None,
        product_id=None,
    ) -> dict:
        """比较两个区间的全部指标：B 相对 A 的涨跌。"""
        first = self.query_metrics(start_a, end_a, store_id, product_id)
        second = self.query_metrics(start_b, end_b, store_id, product_id)
        deltas = {}
        for field in METRIC_FIELDS:
            before, after = first.get(field), second.get(field)
            if before is None or after is None:
                deltas[field] = {"delta": None, "pct": None, "direction": "未知"}
                continue
            delta = round(after - before, 2)
            deltas[field] = {
                "delta": delta,
                "pct": round(delta / before * 100, 2) if before else None,
                "direction": "涨" if delta > 0 else ("跌" if delta < 0 else "持平"),
            }
        return {"period_a": first, "period_b": second, "delta": deltas}
