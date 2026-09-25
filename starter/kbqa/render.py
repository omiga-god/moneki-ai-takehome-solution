"""把工具结果写成中文句子。回答里的数字都在这里成形。"""

from __future__ import annotations

from datetime import date
from typing import Optional

METRIC_LABELS = {
    "net_revenue": "净营业额",
    "refund_amount": "退款金额",
    "orders": "有效订单数",
    "aov": "客单价",
    "qty": "销量",
}
MONEY_METRICS = {"net_revenue", "refund_amount", "aov"}


def money(value: Optional[float]) -> str:
    return "—" if value is None else "%.2f" % float(value)


def count(value: Optional[float]) -> str:
    return "—" if value is None else "%d" % int(round(float(value)))


def percent(value: Optional[float]) -> str:
    return "—" if value is None else "%.2f%%" % (float(value) * 100)


def metric_value(metric: str, result: dict) -> str:
    value = result.get(metric)
    if metric in MONEY_METRICS:
        return money(value) + " 元"
    return count(value) + (" 件" if metric == "qty" else " 单" if metric == "orders" else "")


def window_label(start: str, end: str) -> str:
    if start == end:
        return start
    first, last = date.fromisoformat(start), date.fromisoformat(end)
    if first.day == 1 and (last + __import__("datetime").timedelta(days=1)).day == 1 and first.month == last.month:
        return "%d 年 %d 月" % (first.year, first.month)
    return "%s 至 %s" % (start, end)


def scope_label(window: tuple[str, str], store: str = "", product: str = "") -> str:
    parts = [window_label(*window)]
    parts.append(store or "全部门店")
    if product:
        parts.append(product)
    return "".join("（%s）" % part if index else part for index, part in enumerate(parts))


def describe_metrics(result: dict, scope: str, metric: str = "net_revenue") -> str:
    """一句话说清一个区间的全部关键指标，被问到的那个指标放在最前面。"""
    order = [metric] + [key for key in ("net_revenue", "orders", "aov", "qty", "refund_amount") if key != metric]
    pieces = []
    for key in order:
        if result.get(key) is None and key == "aov":
            pieces.append("客单价无（区间内没有有效订单）")
            continue
        pieces.append("%s %s" % (METRIC_LABELS[key], metric_value(key, result)))
    return "%s：%s。" % (scope, "，".join(pieces))


def describe_compare(compare: dict, metric: str, scope_a: str, scope_b: str) -> str:
    first, second = compare["period_a"], compare["period_b"]
    delta = compare["delta"][metric]
    label = METRIC_LABELS[metric]
    if delta["delta"] is None:
        return "%s 与 %s 的%s无法比较，其中一个区间没有有效订单。" % (scope_a, scope_b, label)
    unit = " 元" if metric in MONEY_METRICS else ""
    return (
        "%s的%s为 %s，%s为 %s，%s了 %s%s（%s）。"
        % (
            scope_b,
            label,
            metric_value(metric, second),
            scope_a,
            metric_value(metric, first),
            delta["direction"],
            abs(delta["delta"]) if metric not in MONEY_METRICS else money(abs(delta["delta"])),
            unit,
            ("%+.2f%%" % delta["pct"]) if delta["pct"] is not None else "无法计算百分比",
        )
    )


def describe_payment(result: dict, scope: str, focus: str = "") -> str:
    payments = result.get("payments") or {}
    if not payments:
        return "%s：区间内没有任何订单。" % scope
    ordered = sorted(payments.items(), key=lambda item: item[1]["orders"], reverse=True)
    if focus and focus in payments:
        ordered = [(focus, payments[focus])] + [item for item in ordered if item[0] != focus]
    pieces = [
        "%s %s 单、占订单数的 %s、金额 %s 元"
        % (name, count(data["orders"]), percent(data["share_orders"]), money(data["net_revenue"]))
        for name, data in ordered
    ]
    return "%s 共 %s 单，其中%s。" % (scope, count(result.get("total_orders")), "；".join(pieces))


def describe_top(result: dict, scope: str, limit: int = 3) -> str:
    items = result.get("products") or []
    if not items:
        return "%s：区间内没有销售记录。" % scope
    pieces = [
        "%s（%s）净营业额 %s 元、销量 %s 件"
        % (item["product_name"], item["product_id"], money(item["net_revenue"]), count(item["qty"]))
        for item in items[:limit]
    ]
    return "%s 卖得最好的是%s。" % (scope, "；".join(pieces))


def describe_by_store(result: dict, scope: str, limit: int = 5) -> str:
    stores = result.get("stores") or []
    if not stores:
        return "%s：区间内没有销售记录。" % scope
    pieces = [
        "%s %s 净营业额 %s 元"
        % (store["store_id"], store.get("store_name", ""), money(store["net_revenue"]))
        for store in stores[:limit]
    ]
    top = stores[0]
    return "%s 净营业额最高的是 %s %s，为 %s 元；各店依次为：%s。" % (
        scope,
        top["store_id"],
        top.get("store_name", ""),
        money(top["net_revenue"]),
        "，".join(pieces),
    )


def describe_category(result: dict, scope: str) -> str:
    items = result.get("categories") or []
    if not items:
        return "%s：区间内没有销售记录。" % scope
    top = items[0]
    pieces = [
        "%s（%s）%s 元" % (item["category"], "、".join(item["stores"]), money(item["net_revenue"]))
        for item in items
    ]
    return "%s 净营业额最高的门店品类是%s，%s 元；各品类依次为：%s。" % (
        scope,
        top["category"],
        money(top["net_revenue"]),
        "，".join(pieces),
    )


def describe_daily(result: dict, scope: str, limit: int = 7) -> str:
    days = result.get("days") or []
    shown = days[:limit]
    pieces = ["%s %s 元" % (day["date"], money(day["net_revenue"])) for day in shown]
    tail = "（共 %d 天，只列前 %d 天）" % (len(days), len(shown)) if len(days) > len(shown) else ""
    return "%s 每日净营业额：%s。%s" % (scope, "，".join(pieces), tail)
