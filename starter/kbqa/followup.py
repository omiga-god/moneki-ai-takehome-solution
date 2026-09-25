"""追问还原：把“那 7 月呢”这种补成完整问题再去规划。"""

from __future__ import annotations

import re
from datetime import date

from . import entities as E
from .timeparse import TimeSpec, loose_days, parse_time


class FollowUps:
    """需要目录（认门店与商品）和固定的“今天”。"""

    def __init__(self, catalog: E.Catalog, today: date) -> None:
        self.catalog = catalog
        self.today = today

    def _is_follow_up(self, question: str, previous: dict) -> bool:
        """这一句是不是接着上一轮说的。

        除了“那 7 月呢”这种明显的指代，还包括“供应商后来赔了多少？”：
        它自己既没有门店也没有商品，却明显在接着上一轮的话题问。
        """
        own_time = bool(parse_time(question, self.today).explicit)
        own_topic = E.has_any(question, E.BUSINESS_WORDS) or bool(E.find_metric(question))
        if own_time and own_topic:
            # “最近整体经营情况怎么样”自带时间和主题，不是接着上一轮说的。
            return False
        if E.looks_like_follow_up(question):
            return True
        text = question.strip()
        if len(text) > 26:
            return False
        if not re.search(
            r"(后来|之后|然后|还有|再|又|那次|这次|当时|结果|这两|那两|两个月|两者|这段|那一周|这一周)",
            text,
        ):
            return False
        store, _ = self.catalog.find_store(text)
        product, _ = self.catalog.find_product(text)
        # 说了一半的写法（“三文鱼那次断供”）也算自带话题，不能当成无主语的追问。
        loose = self.catalog.aliases.mentions(text) if self.catalog.aliases else []
        return not (store or product or loose)

    def resolve(self, question: str, history: list[dict]) -> tuple[str, dict]:
        """把“那 7 月呢”还原成完整问题，并带回上一轮的槽位。"""
        previous = history[-1] if history else None
        if not previous or not self._is_follow_up(question, previous):
            return question, {}
        base = previous.get("standalone") or previous.get("question") or ""
        old_spec = parse_time(base, self.today)
        new_spec = parse_time(question, self.today)
        cleaned = re.sub(r"\s+", "", base)
        if new_spec.windows or new_spec.whole_period:
            for label in old_spec.labels:
                cleaned = cleaned.replace(label, "")
            for word in ("现在", "目前", "当前", "最近"):
                cleaned = cleaned.replace(word, "")
        extra = re.sub(r"^(那么|那|接着|然后)", "", question.strip())
        extra = re.sub(r"(呢)?[？?]?$", "", extra).strip()
        if not (new_spec.windows or new_spec.whole_period) and not E.looks_like_follow_up(question):
            # “供应商后来赔了多少”：保留问句本身，只把上一轮的主题词接在后面。
            standalone = extra + " " + _topic_terms(cleaned)
        else:
            # 中间留一个空格：直接粘起来会造出“月充”这种跨词二元组，把检索带偏。
            standalone = (
                (extra + " " + cleaned)
                if (new_spec.windows or new_spec.whole_period)
                else (cleaned + " " + extra)
            )
        # “的时候/当时”只是时间标记，时间已经解析出来了，留着只会把检索带偏。
        for filler in ("的时候", "那会儿", "当时", "那时"):
            standalone = standalone.replace(filler, "")
        return standalone.strip() or question, previous.get("slots") or {}

    # -- 规划 -------------------------------------------------------------------


    def inherit_time(self, plan, spec: TimeSpec, question: str, inherited: dict) -> None:
        """把只说了一半的时间补全：“8 号那天”“这两个月”“那一周”。"""
        recent = [tuple(window) for window in (inherited.get("recent_windows") or []) if window]
        if not spec.windows:
            days = loose_days(question)
            anchor = recent[-1] if recent else None
            if days and not anchor:
                # 只说“8 号”又没有上文，宁可反问，也不要默默按全区间算。
                plan.slots["needs_month"] = True
                plan.slots["loose_day"] = days[0]
            if days and anchor:
                month = date.fromisoformat(anchor[0])
                points = sorted(
                    date(month.year, month.month, min(day, 28 if month.month == 2 else 31))
                    for day in days
                )
                spec.windows = [(points[0].isoformat(), points[-1].isoformat())]
                spec.labels.append("%d月%s日" % (month.month, "、".join(str(d) for d in days)))
                spec.explicit = True
                spec.as_of = points[-1]
                plan.notes.append("“%s”按上一轮的月份补全为 %s。" % (question.strip(), spec.windows[0]))
        if re.search(r"(这|那|前)?\s*(两个月|两个区间|两段时间|两者|两个月份)", question) and len(recent) >= 2:
            spec.windows = [recent[-2], recent[-1]]
            spec.explicit = True
            plan.notes.append("“这两个月”指上文提到的 %s 与 %s。" % (recent[-2], recent[-1]))
        if re.search(r"(那一周|这一周|那周|同一周)", question) and recent:
            spec.windows = [recent[-1]]
            spec.explicit = True


def _topic_terms(previous: str) -> str:
    """把上一轮问句里的实词留下来当话题，用于接住“后来赔了多少”这类追问。"""
    text = re.sub(r"[？?。，,、：:]", " ", previous)
    text = re.sub(r"(为什么|怎么|多少|是不是|吗|呢|的|了|吧)", " ", text)
    return " ".join(part for part in text.split() if part)
