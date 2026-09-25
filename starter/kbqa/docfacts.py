"""从文档里挑出能回答问题的那一句，并出具引用。"""

from __future__ import annotations

import re
from typing import Optional

from .entities import focus_kinds
from .tokenizer import STOP_CHARS, content_tokens, tokenize
from .units import MAX_QUOTE, Unit, UnitIndex

MARKERS = {"✓", "✔", "√", "有", "×", "✗", "—", "-", "无", "N/A"}

#: 一句话里有没有“问句要的那种东西”。问句焦点是钱就找金额，是原因就找因果说明，
#: 是时长就找“24 小时内”这类跨度，是商品就找真的写了商品名的句子。
_CARRIES = {
    "money": re.compile(r"[¥￥]\s*\d|(?:CNY|RMB|USD)\s*[\d,]|\d[\d,.]*\s*(?:元|块)"),
    "clock": re.compile(r"\d{1,2}\s*[:：]\s*\d{2}|\d{4}[-/]\d{1,2}[-/]\d{1,2}|\d{1,2}\s*月\s*\d{1,2}\s*[日号]"),
    "duration": re.compile(r"\d[\d,.]*\s*(?:小时|分钟|天|工作日|周|个月|年)"),
    "count": re.compile(r"\d[\d,.]*\s*(?:份|条|杯|家|单|次|个|人|件|碗|张|折|分|%)"),
    # 问“怎么算/口径”时，答案是一句规则，不是一句描述。
    "rule": re.compile(r"(计入|不计|剔除|回填|分母|除以|÷|＝|=|之和|口径|按.{0,6}(计|算|统计))"),
    # 问“为什么”时，明说原因的句子最好；只描述“做了什么决定”的次之，两种都要收。
    "reason": re.compile(
        r"(原因|因为|由于|导致|受.{0,4}影响|不合格|故障|漏|坏|低于|高出|损耗|预警|事故)"
    ),
    "reason_event": re.compile(r"(决定|决议|下架|停售|停业|取消|不再|整改|检查|停电|施工|调整)"),
    # 问了一个“量”却没说单位时，至少要求句子里有个数字。
    "value": re.compile(r"\d"),
}

def carries(kind: str, text: str) -> bool:
    pattern = _CARRIES.get(kind)
    return bool(pattern and pattern.search(text))


def carries_any_reason(text: str) -> bool:
    """明说原因，或者描述了“出了什么事”。"""
    return carries("reason", text) or carries("reason_event", text)


class DocFacts:
    """文档侧的取证：挑句、扩引、逐字核对、表格行渲染。"""

    def __init__(self, index) -> None:
        self.index = index
        self.store = UnitIndex(index)

    # -- 委托给 units.py --------------------------------------------------------

    def units(self, doc_id: str) -> list[Unit]:
        return self.store.units(doc_id)

    def sentences(self, doc_id: str) -> list[str]:
        return self.store.sentences(doc_id)

    def stripped(self, doc_id: str) -> tuple[str, list[int]]:
        return self.store.stripped(doc_id)

    def slice_quote(self, doc_id: str, start: int, end: int) -> str:
        return self.store.slice_quote(doc_id, start, end)

    # -- 挑句 -------------------------------------------------------------------

    def term_weights(self, query: str) -> dict[str, float]:
        """问句归一到数据库写法之后的词，用来在文档里挑句子。

        三条：
        * 只补“数据库写法”，不把全部别名都当成必须命中的词——别名是同一个东西的
          不同叫法，不是额外的要求；句子侧做同样的归一，两边才对得上。
        * 语料里根本没有的词直接丢掉：任何句子都不含它，留着只会把所有候选句压平。
          “知识库里有没有这件事”由 `_vocab_coverage` 单独判断。
        * 含虚字的二元组（“在充”“么开”）多半是切词残渣，减半计权。
        """
        index = self.index
        terms = set(content_tokens(query))
        for canonical in index.aliases.mentions(query):
            terms.update(content_tokens(canonical))
        weights = {}
        for term in terms:
            if not index.doc_freq.get(term):
                continue
            weight = index.idf(term)
            if len(term) == 2 and any(char in STOP_CHARS for char in term):
                weight *= 0.5
            weights[term] = weight
        return weights

    def focus_of(self, unit: Unit, kinds: list[str]) -> float:
        """这句话满足了几个焦点。

        “哪个商品”要求句子里真的写了商品名；问“为什么”时，明说原因的句子算满分，
        只写了“决定停售”的算半分——两种都是答案，但前者更是答案。
        """
        satisfied = 0.0
        for kind in kinds:
            if kind == "entity":
                if self.index.aliases.strict_mentions(unit.text):
                    satisfied += 1
            elif kind == "reason":
                if carries("reason", unit.text):
                    satisfied += 1
                elif carries("reason_event", unit.text):
                    satisfied += 0.5
            elif carries(kind, unit.text):
                satisfied += 1
        return satisfied

    def rank(
        self, query: str, doc_id: str, limit: int = 3, require_value: bool = False
    ) -> list[tuple[float, str]]:
        """在一篇文档里挑最能回答问题的句子。

        问句的焦点决定答案长什么样：问“为什么”就要因果句，问“多少钱”就要带金额的句子，
        问“多久”就要带时长的句子，问“哪个商品”就要真的写了商品名的句子。
        一个问题可以同时有几个焦点，满足得越多越靠前。
        `require_value` 为真时只保留至少满足一个焦点的句子；全部文档都挑不出来时由调用方放开。
        """
        weights = self.term_weights(query)
        total = sum(weights.values()) or 1.0
        kinds = focus_kinds(query)
        units = self.units(doc_id)
        if kinds and require_value:
            units = [unit for unit in units if self.focus_of(unit, kinds)]
        scored: list[tuple[float, int, str]] = []
        for position, unit in enumerate(units):
            if len(unit.text) < 8 and unit.kind != "table":
                continue  # 半截短语（HTML 的标签、页脚碎片）不是答案
            direct = set(tokenize(unit.text))
            for canonical in self.index.aliases.strict_mentions(unit.text):
                direct.update(tokenize(canonical))
            hit = 0.0
            for term, weight in weights.items():
                if term in direct:
                    hit += weight
                elif term in unit.context:
                    # 标题带来的相关性是间接的，算一半。
                    hit += weight * 0.5
            if hit <= 0:
                continue
            # 同样的覆盖率，短句子是更好的答案；标题与问句本身都不是答案。
            score = hit / total * (80.0 / (80.0 + max(len(unit.text), 24))) ** 0.5
            if unit.kind == "heading":
                score *= 0.5  # 标题几乎不会是答案本身
            if unit.text.rstrip().endswith(("？", "?")):
                score *= 0.6
            if kinds:
                # 满足焦点的句子显著优先；一个都不满足的要让位。
                score *= 1.0 + 0.8 * self.focus_of(unit, kinds) / len(kinds)
            scored.append((score, position, unit))
        # 分数相同时取文档里更靠前的那一句：一段话的第一句通常就是结论。
        scored.sort(key=lambda item: (-item[0], item[1]))
        picked = []
        for score, position, unit in scored[:limit]:
            target = self._answer_after_question(units, position)
            picked.append((score, self._with_lead(units, target)))
        return picked

    def extend_to_cause(self, unit: Unit, prefer: Optional[list] = None) -> Unit:
        """问“为什么”时，如果命中的只是一句决议，把写原因的那句一并引上。

        “会议决定……下架”回答的是“做了什么”，同一条议题里的“毛利率低于 35%、
        损耗高”才回答“为什么”。按位置向两边找最近的原因句，合并成一段连续原文，
        并受契约 §5 的 400 字上限约束——这个上限本身就把范围限制在同一条议题内。
        """
        if unit.start < 0 or carries("reason", unit.text):
            return unit
        units = [item for item in self.units(unit.doc_id) if item.start >= 0 and item is not unit]

        def distance(item: Unit) -> int:
            return min(abs(item.start - unit.end), abs(unit.start - item.end))

        # 先找明说原因的句子；一篇通知里连这样的句子都没有时，退一步找
        # 描述“出了什么事”的句子（“检查后要求整改”这类），它同样回答了为什么。
        explicit = sorted((i for i in units if carries("reason", i.text)), key=distance)
        event = sorted((i for i in units if carries("reason_event", i.text)), key=distance)
        # 调用方给了按相关度排好的候选时，优先用它：距离最近的往往只是一句连接语
        # （“经与物业确认，现决定：”），真正写原因的那句未必紧贴着。
        ranked = [
            item
            for item in (prefer or [])
            if item is not unit and item.start >= 0 and carries_any_reason(item.text)
        ]
        for candidate in ranked + explicit + event:
            start = min(unit.start, candidate.start)
            end = max(unit.end, candidate.end)
            if end - start > MAX_QUOTE:
                continue
            text = self.slice_quote(unit.doc_id, start, end)
            if text:
                return Unit(
                    text, unit.context, unit.kind, unit.header, unit.doc_id, unit.line_id, start, end
                )
        return unit

    def _with_lead(self, units: list, unit: Unit) -> Unit:
        """命中的是一段话的第二句时，把同一段的前一句一起纳入引用范围。

        FAQ 的答案常常是“先给做法，再补条件”，只引后半句等于把答案丢了。
        """
        if unit.kind != "text" or unit.line_id < 0 or unit.start < 0:
            return unit
        position = units.index(unit)
        start = unit.start
        cursor = position - 1
        while cursor >= 0 and units[cursor].line_id == unit.line_id and units[cursor].start >= 0:
            if unit.end - units[cursor].start > 180:
                break
            start = units[cursor].start
            cursor -= 1
        if start == unit.start:
            return unit
        text = self.slice_quote(unit.doc_id, start, unit.end)
        if not text:
            return unit
        return Unit(
            text, unit.context, unit.kind, unit.header, unit.doc_id, unit.line_id, start, unit.end
        )

    @staticmethod
    def _answer_after_question(units: list, position: int) -> Unit:
        """FAQ 里命中的常常是问句本身，答案在它下一句。"""
        unit = units[position]
        if not unit.text.rstrip().endswith(("？", "?")):
            return unit
        for candidate in units[position + 1 : position + 3]:
            text = candidate.text.strip()
            if text and not text.endswith(("？", "?")) and len(text) >= 8:
                return candidate
        return unit

    def verbatim(self, doc_id: str, quote: str) -> bool:
        source = re.sub(r"\s+", "", self.index.texts.get(doc_id, ""))
        return re.sub(r"\s+", "", quote) in source

    def cite(self, doc_id: str, quote: str) -> Optional[dict]:
        quote = quote.strip()
        if not quote or not self.verbatim(doc_id, quote):
            return None
        return {"doc_id": doc_id, "quote": quote}

    def render_row(self, header: list[str], line: str) -> str:
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if not header or len(cells) < 2:
            return line.strip()
        marks = [index for index, cell in enumerate(cells) if cell in MARKERS]
        if len(marks) >= max(2, len(cells) // 2):
            keys = [cell for index, cell in enumerate(cells) if index not in marks and cell]
            yes = [
                header[index]
                for index in marks
                if index < len(header) and cells[index] in {"✓", "✔", "√", "有"}
            ]
            return "%s：%s。" % (
                " ".join(keys),
                ("含有 " + "、".join(yes)) if yes else "对照表里列出的项目都不含",
            )
        pairs = [
            "%s %s" % (header[index], cell)
            for index, cell in enumerate(cells)
            if index < len(header) and cell and index > 0
        ]
        return "%s：%s。" % (cells[0], "，".join(pairs))

    def table_header_for(self, doc_id: str, line: str) -> list[str]:
        for unit in self.units(doc_id):
            if unit.kind == "table" and unit.text == line.strip():
                return unit.header
        return []

    def render(self, doc_id: str, sentence: str) -> str:
        if sentence.strip().startswith("|"):
            return self.render_row(self.table_header_for(doc_id, sentence), sentence)
        return sentence.strip()

    def version_note(self, meta: dict) -> str:
        status = meta.get("status") or ""
        parts = []
        if meta.get("effective_from"):
            parts.append("%s 起生效" % meta["effective_from"])
        if status and status != "现行":
            parts.append(status)
            if meta.get("superseded_by"):
                parts.append("已由 %s 取代" % meta["superseded_by"])
        return "（%s）" % "，".join(parts) if parts else ""

    def vocab_coverage(self, text: str) -> float:
        """问题里的词，有多少在知识库的词表里出现过。

        只看二元组与英文词：单个汉字在二元组索引里本来就不会出现，
        把它算成“词表里没有”会系统性地压低覆盖率。
        """
        terms = {term for term in content_tokens(text) if len(term) >= 2}
        if not terms:
            terms = set(content_tokens(text))
        if not terms:
            return 0.0
        known = [term for term in terms if self.index.doc_freq.get(term)]
        return len(known) / len(terms)
