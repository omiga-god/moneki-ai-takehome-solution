"""规划：一句话进来，决定查数还是查文档、查哪段时间、哪家店。"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from typing import Callable, Optional

from . import entities as E
from .followup import FollowUps
from .timeparse import TimeSpec, parse_time

#: 意图 -> 检索时补充的领域同义词。纯语言层面的扩写，帮助“卖多少钱”命中“售价/调价”。
INTENT_KEYWORDS = {
    "price": ("售价", "价格", "调价", "单价"),
    "target": ("目标", "达标", "方案"),
    "anomaly": ("通知", "公告", "原因", "说明"),
    "payment": ("支付", "收款", "终端"),
    "hours": ("营业时间", "闭店", "延长"),
}


@dataclass
class Plan:
    question: str
    standalone: str
    search_query: str
    intent: str = "data"
    kind: str = "summary"
    window: Optional[tuple[str, str]] = None
    compare_window: Optional[tuple[str, str]] = None
    as_of: Optional[date] = None
    year: Optional[int] = None
    store_id: Optional[str] = None
    product_id: Optional[str] = None
    metric: str = "net_revenue"
    needs_data: bool = False
    needs_docs: bool = False
    refusal: Optional[str] = None
    notes: list[str] = field(default_factory=list)
    slots: dict = field(default_factory=dict)

    def as_trace(self) -> dict:
        return {
            "question": self.question,
            "standalone_question": self.standalone,
            "search_query": self.search_query,
            "intent": self.intent,
            "kind": self.kind,
            "window": self.window,
            "compare_window": self.compare_window,
            "as_of": self.as_of.isoformat() if self.as_of else None,
            "year": self.year,
            "store_id": self.store_id,
            "product_id": self.product_id,
            "metric": self.metric,
            "needs_data": self.needs_data,
            "needs_docs": self.needs_docs,
            "refusal": self.refusal,
            "notes": self.notes,
        }


class Planner:
    def __init__(
        self,
        catalog: E.Catalog,
        today: date,
        data_period: dict,
        scout: Optional[Callable[[str], tuple[float, float]]] = None,
    ) -> None:
        self.catalog = catalog
        self.today = today
        self.data_period = data_period
        self.followups = FollowUps(catalog, today)
        #: 给一句话“探个底”：返回（词表覆盖率，检索最高分）。越界判断要靠它。
        self.scout = scout or (lambda text: (1.0, 100.0))

    def plan(self, question: str, history: Optional[list[dict]] = None) -> Plan:
        standalone, inherited = self.followups.resolve(question, history or [])
        plan = Plan(question=question, standalone=standalone, search_query=standalone)
        history = history or []
        if not history and E.looks_like_follow_up(question) and len(question.strip()) <= 12:
            plan.intent, plan.kind = "clarify", "need_context"
            plan.refusal = "这句像是追问，但这个会话里没有上文。请把问题补完整，例如“7 月的净营业额是多少”。"
            return plan
        if standalone != question:
            plan.notes.append("这是一句追问，已按上一轮补全为：%s" % standalone)

        # 越界判断放在追问还原之后：“那 7 月呢”要先补成完整问题才判得准。
        head = E.head_clause(standalone)
        reason = E.out_of_scope(standalone, *self.scout(head))
        if reason:
            plan.intent, plan.kind = "refusal", "out_of_scope"
            plan.notes.append("越界判断：%s" % reason)
            plan.refusal = (
                "这个问题超出了系统能回答的范围：%s。数据库里只有 %s 至 %s 的销售明细，"
                "所以我不能回答。" % (reason, self.data_period["start"], self.data_period["end"])
            )
            return plan

        spec = parse_time(standalone, self.today)
        self.followups.inherit_time(plan, spec, question, inherited)
        plan.as_of = spec.as_of or self.today
        plan.year = spec.year
        store_id, unknown_store = self.catalog.find_store(standalone)
        product_id, unknown_product = self.catalog.find_product(standalone)
        plan.store_id = store_id or (inherited.get("store_id") if not unknown_store else None)
        plan.product_id = product_id or (inherited.get("product_id") if not unknown_product else None)

        if unknown_store:
            plan.intent = "refusal"
            plan.kind = "unknown_entity"
            plan.refusal = "数据库里没有 %s 这家门店，现有门店是 %s。" % (
                unknown_store,
                "、".join("%s %s" % (s["store_id"], s["store_name"]) for s in self.catalog.stores),
            )
            return plan
        if unknown_product:
            plan.intent = "refusal"
            plan.kind = "unknown_entity"
            plan.refusal = "商品表里没有 %s 这个商品编号。" % unknown_product
            return plan

        self._fill_measure(plan, spec, inherited)
        if plan.slots.get("needs_month"):
            plan.intent, plan.kind = "clarify", "need_month"
            plan.refusal = "只说了“%s 号”，没说是哪个月。数据区间是 %s 至 %s，请把月份补上。" % (
                plan.slots.get("loose_day", ""),
                self.data_period["start"],
                self.data_period["end"],
            )
            return plan
        self._choose_kind(plan, spec)
        self._check_period(plan, spec)
        self._build_search_query(plan, spec)
        recent = [
            window
            for window in (inherited.get("recent_windows") or []) + [plan.window]
            if window
        ]
        deduped: list = []
        for window in recent:
            if window not in deduped:
                deduped.append(window)
        plan.slots.update(
            {
                "recent_windows": deduped[-3:],
                "store_id": plan.store_id,
                "product_id": plan.product_id,
                "metric": plan.metric,
                "window": plan.window,
                "kind": plan.kind,
            }
        )
        return plan

    # -- 细节 -------------------------------------------------------------------

    def _fill_measure(self, plan: Plan, spec: TimeSpec, inherited: dict) -> None:
        text = plan.standalone
        metric = E.find_metric(text)
        plan.metric = metric or inherited.get("metric") or "net_revenue"
        plan.notes.append(
            "识别：指标=%s 门店=%s 商品=%s 时间=%s"
            % (
                metric or "未指定",
                plan.store_id or "全部",
                plan.product_id or "全部",
                spec.labels or "未指定",
            )
        )
        plan.slots["metric_explicit"] = bool(metric)
        plan.slots["time_explicit"] = bool(spec.explicit and spec.windows)
        plan.slots["time_scoped"] = bool(spec.explicit or spec.relative_now)
        # 问旧版有两种问法：给了具体日期的，按那天生效的版本选（as-of）；
        # 只说“以前/旧口径”没给日期的，才整体解除“已废止”过滤。
        dated = bool(spec.windows) and spec.as_of is not None and spec.as_of < self.today
        plan.slots["historical"] = E.wants_historical(text) and not dated
        plan.slots["as_of_dated"] = dated
        if plan.slots["historical"]:
            plan.notes.append("问的是过去那一版的规定，已把已废止的文档放回检索范围。")
        elif dated and E.wants_historical(text):
            plan.notes.append("问的是 %s 当时的规定，按 effective_from 选当时生效的版本。" % spec.as_of)

    def _choose_kind(self, plan: Plan, spec: TimeSpec) -> None:
        """先判断这是“问数字”还是“问规定”，再细分到具体的取数方式。"""
        text = plan.standalone
        windows = list(spec.windows)
        if not windows and spec.relative_now and not spec.whole_period:
            # “今天卖了多少”问的就是今天，数据区间之外的话会被 _check_period 拦住。
            windows = [(self.today.isoformat(), self.today.isoformat())]
        elif spec.whole_period or not windows:
            windows = [(self.data_period["start"], self.data_period["end"])]
        plan.window = windows[0]
        explicit_metric = bool(plan.slots.get("metric_explicit"))
        asks_policy = E.has_any(text, E.POLICY_WORDS)
        asks_rank = E.has_any(text, E.RANK_WORDS)
        asks_payment = E.has_any(text, E.PAYMENT_WORDS)
        asks_why = E.has_any(text, E.WHY_WORDS)
        asks_target = E.has_any(text, E.TARGET_WORDS)
        asks_price = E.has_any(text, E.PRICE_WORDS)
        asks_amount = E.has_any(text, ("多少", "几", "是多少", "有多少")) or asks_rank

        asks_business = E.has_any(text, E.BUSINESS_WORDS)
        abnormal = E.is_abnormal(text)
        has_subject = bool(plan.store_id or plan.product_id or plan.slots.get("time_explicit"))
        # 只有问句里真的点到了数据库能算的东西，才允许走取数路线。
        may_query = bool(
            explicit_metric
            or asks_payment
            or E.has_any(text, E.SALES_RANK_WORDS)
            or (asks_business and plan.slots.get("time_scoped"))
        )
        compares = len(windows) > 1 and E.has_any(text, E.TREND_WORDS)
        if asks_target:
            plan.kind, plan.intent = "target", "hybrid"
        elif asks_price and plan.product_id:
            plan.kind, plan.intent = "price", "hybrid"
        elif (asks_why or abnormal) and has_subject and (explicit_metric or abnormal):
            # “怎么这么低”“一单都没有”也是在问原因，不必出现“为什么”三个字。
            plan.kind, plan.intent = "anomaly", "hybrid"
        elif compares:
            # 两个时间 + 比较说法：问的就是这两段时间的数字，指标没写就按净营业额。
            plan.window, plan.compare_window = windows[0], windows[1]
            plan.kind, plan.intent = "compare", "data"
        elif asks_policy and not (explicit_metric and plan.slots.get("time_explicit")):
            # 问规定的时候，即使句子里出现了指标名，也该去知识库。
            plan.kind, plan.intent = "doc", "doc"
        elif not may_query:
            plan.kind, plan.intent = "doc", "doc"

        elif len(windows) > 1 and E.has_any(text, E.TREND_WORDS):
            plan.window, plan.compare_window = windows[0], windows[1]
            plan.kind, plan.intent = "compare", "data"
        elif asks_payment:
            plan.kind, plan.intent = "payment", "data"
        elif asks_rank and E.has_any(text, E.CATEGORY_WORDS):
            plan.kind, plan.intent = "category", "data"
        elif E.has_any(text, E.STORE_WORDS) and not plan.store_id:
            # “各门店 7 月营业额分别是多少”没有排名词，但要的就是分店明细。
            plan.kind, plan.intent = "by_store", "data"
        elif asks_rank:
            plan.kind, plan.intent = "top_products", "data"
        elif E.has_any(text, E.DAILY_WORDS):
            plan.kind, plan.intent = "daily", "data"
        else:
            plan.kind, plan.intent = "summary", "data"

        # 路由：问“多少/多久/几”的就是要数字，问“为什么/原因”的就是要说法。
        # 两边都走一遍太慢，没必要。
        if E.has_any(text, ("多少", "多久", "几")):
            plan.intent = "data"
            if plan.kind in ("doc", "anomaly", "target", "price"):
                plan.kind = "summary"
        elif E.has_any(text, ("为什么", "原因", "怎么回事", "咋回事")):
            plan.intent, plan.kind = "doc", "doc"

        plan.slots["asks_why"] = bool(asks_why or abnormal)
        plan.slots["about_names"] = E.asks_about_names(text)
        plan.slots["two_part"] = False
        # 什么抓手都没有时（没有指标、时间、门店、商品、支付方式、排名，
        # 连一个具体数字或制度词都没有），宁可反问，也不要拿一个不相干的结果糊弄。
        plan.slots["underspecified"] = not (
            explicit_metric
            or plan.slots.get("time_scoped")
            or plan.store_id
            or plan.product_id
            or asks_payment
            or asks_rank
            or asks_policy
            or re.search(r"\d", text)
        )
        if spec.relative_now and not spec.windows and plan.intent != "data":
            # “现在的售价/现在的规定”问的是哪一版生效，不是今天的销量：区间恢复成全区间。
            plan.window = (self.data_period["start"], self.data_period["end"])
        plan.needs_data = plan.kind not in ("doc",)
        plan.needs_docs = plan.intent in ("doc", "hybrid")
        _ = asks_amount
        if spec.first_month:
            plan.notes.append("按“首月”处理：以该商品在数据库里的首个销售日所在自然月为区间。")

    def _check_period(self, plan: Plan, spec: TimeSpec) -> None:
        """问到数据区间之外的时间，如实说没有数据，不猜。"""
        if not plan.needs_data or not plan.window:
            return
        start, end = plan.window
        if end < self.data_period["start"] or start > self.data_period["end"]:
            plan.intent = "refusal"
            plan.kind = "out_of_period"
            plan.refusal = "数据库里只有 %s 至 %s 的销售明细，%s 至 %s 没有任何数据。" % (
                self.data_period["start"],
                self.data_period["end"],
                start,
                end,
            )

    def _build_search_query(self, plan: Plan, spec: TimeSpec) -> None:
        # “现在/今天/目前”只是判生效日期用的，检索时是纯噪声，去掉。
        text = plan.standalone
        for word in ("现在", "今天", "目前", "当前", "此刻", "的时候"):
            text = text.replace(word, "")
        plan.slots["clean_question"] = text.strip() or plan.standalone
        parts = [plan.slots["clean_question"]]
        if plan.store_id:
            parts.append("%s %s" % (plan.store_id, self.catalog.store_name(plan.store_id)))
        if plan.product_id:
            parts.append(self.catalog.product_name(plan.product_id))
        for key, words in INTENT_KEYWORDS.items():
            if key == "price" and plan.kind == "price":
                parts.extend(words)
            elif key == "target" and plan.kind == "target":
                parts.extend(words)
            elif key == "anomaly" and plan.kind == "anomaly":
                parts.extend(words)
            elif key == "payment" and plan.kind == "payment":
                parts.extend(words)
            elif key == "hours" and E.has_any(plan.standalone, ("营业到", "几点", "营业时间", "开门", "关门")):
                parts.extend(words)
        plan.search_query = " ".join(parts)


