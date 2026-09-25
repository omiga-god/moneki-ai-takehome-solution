"""给模型看的工具定义（OpenAI 函数调用格式）。"""

from __future__ import annotations

_DATE = {"type": "string", "description": "日期，YYYY-MM-DD，闭区间"}
_STORE = {"type": "string", "description": "门店编号，例如 S01；不传表示全部门店"}
_PRODUCT = {"type": "string", "description": "商品编号，例如 P06；不传表示全部商品"}


def _fn(name: str, description: str, properties: dict, required: list[str]) -> dict:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {"type": "object", "properties": properties, "required": required},
        },
    }


TOOLS = [
    _fn(
        "query_metrics",
        "按 KB-001 口径查一个区间的净营业额、退款金额、有效订单数、客单价、销量。",
        {"start": _DATE, "end": _DATE, "store_id": _STORE, "product_id": _PRODUCT},
        ["start", "end"],
    ),
    _fn(
        "daily_metrics",
        "按天返回区间内每一天的净营业额、订单数、客单价，没有营业额的日期也会出现。",
        {"start": _DATE, "end": _DATE, "store_id": _STORE, "product_id": _PRODUCT},
        ["start", "end"],
    ),
    _fn(
        "payment_mix",
        "各支付方式的订单数、金额与占比。",
        {"start": _DATE, "end": _DATE, "store_id": _STORE},
        ["start", "end"],
    ),
    _fn(
        "top_products",
        "区间内卖得最好的商品排行。",
        {
            "start": _DATE,
            "end": _DATE,
            "store_id": _STORE,
            "limit": {"type": "integer", "description": "返回条数，默认 10"},
        },
        ["start", "end"],
    ),
    _fn(
        "by_store",
        "区间内各门店的指标，按净营业额从高到低。",
        {"start": _DATE, "end": _DATE, "product_id": _PRODUCT},
        ["start", "end"],
    ),
    _fn(
        "by_store_category",
        "区间内各门店品类（拉面、轻食等）的指标汇总。",
        {"start": _DATE, "end": _DATE},
        ["start", "end"],
    ),
    _fn(
        "compare_periods",
        "比较两个区间的全部指标，返回差值与涨跌幅。",
        {
            "start_a": _DATE,
            "end_a": _DATE,
            "start_b": _DATE,
            "end_b": _DATE,
            "store_id": _STORE,
            "product_id": _PRODUCT,
        },
        ["start_a", "end_a", "start_b", "end_b"],
    ),
    _fn(
        "unit_price_check",
        "某商品的实收单价分布与维表建档价，用来判断现行售价与维表是否一致。",
        {"product_id": _PRODUCT, "start": _DATE, "end": _DATE},
        ["product_id"],
    ),
    _fn(
        "run_sql",
        "在清洗表上执行一条 SQL，工具覆盖不到的查法用这个。",
        {"sql": {"type": "string", "description": "要执行的 SQL 语句"}},
        ["sql"],
    ),
    _fn(
        "search_kb",
        "检索公司知识库，返回最相关的文档片段。回答里引用某一份文档时，"
        "在句末写上它的编号，例如 [KB-013]。",
        {
            "query": {"type": "string", "description": "检索用的问题或关键词"},
            "top_k": {"type": "integer", "description": "返回片段数，默认 5"},
        },
        ["query"],
    ),
]

TOOL_NAMES = [tool["function"]["name"] for tool in TOOLS]
