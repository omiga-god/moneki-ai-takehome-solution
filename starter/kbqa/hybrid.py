"""混合问答的三条路子：达标、现价、异常解释。单独放一个文件，不然作答那边太长。"""

from __future__ import annotations

import re
from datetime import date, timedelta
from typing import Optional

from . import render
from .planner import Plan
from .retriever import Hit, SearchResult
from .schemas import Answer
from .tokenizer import normalise

_TARGET = re.compile(r"目标[^。；\n]{0,12}?(\d[\d,]*(?:\.\d+)?)\s*(份|杯|单|件|元|%)")
_PRICE = re.compile(r"(?:调整为|调为|现价|活动价|售价为|售价|价格为)\s*[¥￥]?\s*(\d+(?:\.\d+)?)")
#: 解释异常时优先挑带因果说明的句子，而不是标题或整改措施。分两级。
_CAUSE_EXPLICIT = re.compile(r"(原因|因为|由于|导致|受.{0,4}影响)")
_CAUSE_EVENT = re.compile(r"(故障|停业|整改|暂停|闭店|停售|检查|预警|停电|施工)")


class HybridAnswers:
    """混合路径。依赖 Answerer 提供的 `_call`/`_scope`/`_search`/`facts`。"""

    # -- 混合：达标 -------------------------------------------------------------

    def _answer_target(self, plan: Plan, trace=None) -> Answer:
        evidence: list[dict] = []
        window = self._first_month_window(plan) or plan.window
        metrics = self._call(
            evidence,
            "query_metrics",
            start=window[0],
            end=window[1],
            store_id=plan.store_id,
            product_id=plan.product_id,
        )
        result = self._search(plan, window=window, trace=trace)
        target, unit, citation, meta = self._find_target(plan, result)
        scope = self._scope(plan, window)
        actual = metrics["qty"] if unit in ("份", "杯", "件", "") else metrics["net_revenue"]
        head = render.describe_metrics(metrics, scope, "qty" if unit in ("份", "杯", "件", "") else "net_revenue")
        if target is None:
            return Answer(
                answer=head + "知识库里没有找到对应的目标值，无法判断是否达标。",
                answer_type="data",
                data_evidence=evidence,
            )
        gap = round(actual - target, 2)
        verdict = "已达标，超出" if gap >= 0 else "未达标，差"
        gap_text = render.count(abs(gap))
        return Answer(
            answer="%s目标为 %s %s（%s《%s》%s），实际 %s %s，%s %s。"
            % (
                head,
                render.count(target),
                unit,
                citation["doc_id"],
                meta.get("title", ""),
                self.facts.version_note(meta),
                render.count(actual),
                unit,
                "%s %s" % (verdict, gap_text),
                unit,
            ),
            answer_type="hybrid",
            citations=[citation],
            data_evidence=evidence,
        )

    def _find_target(self, plan: Plan, result: SearchResult):
        for hit in self.answerable_hits(plan, result):
            if self.retriever.index.docs_meta.get(hit.doc_id, {}).get("estimates_only"):
                continue  # KB-001 §5.2：周报、纪要里的数字是估算，不能当目标或答案
            for sentence in self.facts.sentences(hit.doc_id):
                match = _TARGET.search(sentence)
                if not match:
                    continue
                if plan.product_id:
                    name = self.catalog.product_name(plan.product_id)
                    if normalise(name) not in normalise(sentence) and not self._same_topic(hit, plan):
                        continue
                citation = self.facts.cite(hit.doc_id, sentence)
                if citation:
                    return (
                        float(match.group(1).replace(",", "")),
                        match.group(2),
                        citation,
                        self.retriever.index.docs_meta.get(hit.doc_id, {}),
                    )
        return None, "", None, {}

    def _same_topic(self, hit: Hit, plan: Plan) -> bool:
        name = self.catalog.product_name(plan.product_id) if plan.product_id else ""
        return bool(name) and normalise(name) in normalise(
            self.retriever.index.texts.get(hit.doc_id, "")
        )

    def _first_month_window(self, plan: Plan) -> Optional[tuple[str, str]]:
        if "首月" not in plan.standalone and "第一个月" not in plan.standalone:
            return None
        if not plan.product_id:
            return None
        first = self.tools.first_sale_date(plan.product_id)
        if not first:
            return None
        start = date.fromisoformat(first).replace(day=1)
        nxt = (start.replace(day=28) + timedelta(days=4)).replace(day=1)
        return start.isoformat(), (nxt - timedelta(days=1)).isoformat()

    # -- 混合：现价 -------------------------------------------------------------

    def _answer_price(self, plan: Plan, trace=None) -> Answer:
        """现价，或者“某天卖多少钱”。

        问到具体某一天时，以数据库里那天真实的实收单价为准（活动价只在一家店发生过，
        所以按门店分开说），再配上那天生效的那份文档。
        """
        if plan.slots.get("time_explicit") and plan.window and plan.window[1] < self.today.isoformat():
            return self._answer_price_as_of(plan, trace)
        evidence: list[dict] = []
        result = self._search(plan, trace=trace)
        price, citation, meta = None, None, {}
        for hit in sorted(
            self.answerable_hits(plan, result),
            key=lambda item: item.meta.get("effective_from") or "",
            reverse=True,
        ):
            if self.retriever.index.docs_meta.get(hit.doc_id, {}).get("estimates_only"):
                continue
            for sentence in self.facts.sentences(hit.doc_id):
                if plan.product_id and normalise(
                    self.catalog.product_name(plan.product_id)
                ) not in normalise(sentence):
                    continue
                match = _PRICE.search(sentence)
                if match and self.facts.cite(hit.doc_id, sentence):
                    price = float(match.group(1))
                    citation = self.facts.cite(hit.doc_id, sentence)
                    meta = self.retriever.index.docs_meta.get(hit.doc_id, {})
                    break
            if price is not None:
                break
        check = self._call(
            evidence,
            "unit_price_check",
            product_id=plan.product_id,
            start=self.data_period["start"],
            end=self.data_period["end"],
        )
        name = self.catalog.product_name(plan.product_id) if plan.product_id else "该商品"
        if price is None:
            # 知识库没有调价通知不等于答不上来：实收单价数据库里就有。
            latest = check.get("latest_price")
            if latest is None:
                return Answer(
                    answer="知识库里没有 %s 的调价通知，数据库里也没有它的成交记录，给不出价格。" % name,
                    answer_type="refusal",
                    data_evidence=evidence,
                )
            return Answer(
                answer="知识库里没有找到 %s 的调价通知；数据库里最近一次成交的实收单价是 %s 元（%s）。"
                "维表建档价是 %s 元，口径以实收为准。"
                % (
                    name,
                    render.money(latest),
                    check.get("latest_date"),
                    render.money(check.get("table_unit_price")),
                ),
                answer_type="data",
                data_evidence=evidence,
            )
        latest = check.get("latest_price")
        lag = check.get("table_unit_price")
        pieces = [
            "%s 现在的售价是 %s 元（%s《%s》%s）。"
            % (name, render.money(price), citation["doc_id"], meta.get("title", ""), self.facts.version_note(meta))
        ]
        if latest is not None:
            pieces.append(
                "数据库里最近一次成交的实收单价是 %s 元，与通知一致。" % render.money(latest)
            )
        if lag is not None and abs(float(lag) - price) >= 0.01:
            pieces.append(
                "注意 products 维表里的建档价仍是 %s 元，由财务月底统一更新，属于维表滞后，不能当成交价。"
                % render.money(lag)
            )
        return Answer(
            answer="".join(pieces),
            answer_type="hybrid",
            citations=[citation],
            data_evidence=evidence,
        )

    def _answer_price_as_of(self, plan: Plan, trace=None) -> Answer:
        evidence: list[dict] = []
        start, end = plan.window
        check = self._call(
            evidence,
            "unit_price_check",
            product_id=plan.product_id,
            start=start,
            end=end,
            store_id=plan.store_id,
        )
        name = self.catalog.product_name(plan.product_id) if plan.product_id else "该商品"
        label = render.window_label(start, end)
        prices = check.get("observed_unit_prices") or {}
        if not prices:
            return Answer(
                answer="%s %s 在数据库里没有成交记录，没法给出那天的实收单价。" % (label, name),
                answer_type="data",
                data_evidence=evidence,
            )
        pieces = []
        if len(prices) == 1:
            only = next(iter(prices))
            pieces.append("%s %s 的实收单价是 %s 元（%d 行成交）。" % (label, name, only, check["rows"]))
        else:
            detail = "；".join(
                "%s %s 元（%d 行）"
                % (store, max(bucket, key=bucket.get), bucket[max(bucket, key=bucket.get)])
                for store, bucket in sorted(check.get("by_store", {}).items())
            )
            pieces.append(
                "%s %s 各门店的实收单价并不一样：%s。" % (label, name, detail)
            )
        result = self._search(plan, trace=trace)
        citations = []
        for hit in self.answerable_hits(plan, result):
            for sentence in self.facts.sentences(hit.doc_id):
                if plan.product_id and normalise(
                    self.catalog.product_name(plan.product_id)
                ) not in normalise(sentence):
                    continue
                if not _PRICE.search(sentence):
                    continue
                citation = self.facts.cite(hit.doc_id, sentence)
                if citation:
                    meta = self.retriever.index.docs_meta.get(hit.doc_id, {})
                    pieces.append(
                        "当时生效的说明见 %s《%s》%s：%s"
                        % (hit.doc_id, meta.get("title", ""), self.facts.version_note(meta), sentence)
                    )
                    citations = [citation]
                    break
            if citations:
                break
        table_price = check.get("table_unit_price")
        if table_price is not None and "%.2f" % table_price not in prices:
            pieces.append(
                "products 维表里的建档价是 %s 元，与当天实收不一致，以实收为准。"
                % render.money(table_price)
            )
        return Answer(
            answer="".join(pieces),
            answer_type="hybrid" if citations else "data",
            citations=citations,
            data_evidence=evidence,
        )

    # -- 混合：异常解释 ---------------------------------------------------------

    def _answer_anomaly(self, plan: Plan, trace=None) -> Answer:
        evidence: list[dict] = []
        start, end = plan.window
        metrics = self._call(
            evidence,
            "query_metrics",
            start=start,
            end=end,
            store_id=plan.store_id,
            product_id=plan.product_id,
        )
        scope = self._scope(plan)
        head = render.describe_metrics(metrics, scope, plan.metric)
        head += self._zero_days(plan, evidence, start, end)
        baseline = self._baseline(plan, evidence)
        body, citations = self._cause_block(plan, start, end, trace)
        if not citations:
            return Answer(
                answer=head + baseline + "知识库里没有找到能解释这段时间的通知或说明，"
                "所以只能确认数字本身，不能给出原因。",
                answer_type="data",
                data_evidence=evidence,
                notes=["异常解释：窗口内没有可引用的文档"],
            )
        return Answer(
            answer=head + baseline + body,
            answer_type="hybrid",
            citations=citations,
            data_evidence=evidence,
        )

    def _cause_block(self, plan: Plan, start: str, end: str, trace=None) -> tuple[str, list[dict]]:
        """找“这段时间出了什么事”的文档：只认真正覆盖这个时间窗的文档。

        找不到就返回空——公开知识库里没有解释的异常，必须如实说没找到，不能拿
        相邻时间的通知硬凑一个原因。
        """
        result = self._search(plan, window=(start, end), trace=trace)
        for hit in self.answerable_hits(plan, result):
            if not self._covers_window(hit.doc_id, start, end):
                continue
            ranked = self.facts.rank(plan.search_query, hit.doc_id, limit=40)
            headings = {
                unit.text for unit in self.facts.units(hit.doc_id) if unit.kind == "heading"
            }
            body = [unit for _, unit in ranked if unit.text not in headings]
            explicit = [unit for unit in body if _CAUSE_EXPLICIT.search(unit.text)]
            event = [unit for unit in body if _CAUSE_EVENT.search(unit.text)]
            chosen = next(iter(explicit + event + body), None)
            if chosen is None:
                sentence = next(iter(self.facts.sentences(hit.doc_id)), "")
            else:
                # 命中的只是“停业/下架”这类事件句时，把写原因的那句一起引上（同 P-1）。
                sentence = self.facts.extend_to_cause(chosen, prefer=body).text
            citation = self.facts.cite(hit.doc_id, sentence)
            if not citation:
                continue
            meta = self.retriever.index.docs_meta.get(hit.doc_id, {})
            return (
                "原因见 %s《%s》%s：%s"
                % (
                    hit.doc_id,
                    meta.get("title", ""),
                    self.facts.version_note(meta),
                    self.facts.render(hit.doc_id, sentence),
                ),
                [citation],
            )
        return "", []

    def _zero_days(self, plan: Plan, evidence: list[dict], start: str, end: str) -> str:
        """区间不止一天时，指出里面到底是哪几天不对。"""
        if start == end:
            return ""
        daily = self._call(
            evidence,
            "daily_metrics",
            start=start,
            end=end,
            store_id=plan.store_id,
            product_id=plan.product_id,
        )
        days = daily.get("days") or []
        zero = [day["date"] for day in days if day["net_revenue"] == 0]
        if not zero:
            return ""
        return "其中 %s 共 %d 天没有任何营业额。" % ("、".join(zero), len(zero))

    def _baseline(self, plan: Plan, evidence: list[dict]) -> str:
        """给异常一个参照：同样长度的上一个区间。"""
        start, end = plan.window
        first, last = date.fromisoformat(start), date.fromisoformat(end)
        span = (last - first).days + 1
        previous = (first - timedelta(days=span)).isoformat(), (first - timedelta(days=1)).isoformat()
        if previous[0] < self.data_period["start"]:
            return ""
        result = self._call(
            evidence,
            "query_metrics",
            start=previous[0],
            end=previous[1],
            store_id=plan.store_id,
            product_id=plan.product_id,
        )
        return "作为对照，上一个同样长度的区间（%s 至 %s）净营业额为 %s 元。" % (
            previous[0],
            previous[1],
            render.money(result["net_revenue"]),
        )

    def _covers_window(self, doc_id: str, start: str, end: str) -> bool:
        """这篇文档是不是在说这段时间的事：生效日在窗口内，或标题里的日期落在窗口内。"""
        meta = self.retriever.index.docs_meta.get(doc_id, {})
        effective = meta.get("effective_from")
        if effective and start <= effective <= end:
            return True
        for match in re.finditer(r"(20\d{2})[-/年](\d{1,2})[-/月](\d{1,2})", meta.get("title", "")):
            try:
                found = date(*(int(part) for part in match.groups())).isoformat()
            except ValueError:
                continue
            if start <= found <= end:
                return True
        return False
