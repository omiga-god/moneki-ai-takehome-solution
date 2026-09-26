"""Facts must survive retrieval chunk boundaries and CRLF/LF differences."""
from datetime import date

from kbqa.docfacts import DocFacts
from kbqa.index import load_index


def test_fact_sentence_is_not_cut_at_retrieval_boundary(tmp_path):
    kb = tmp_path / "kb"
    kb.mkdir()
    statement = "试验活动目标销量为 777 杯，统计范围为指定活动日期内的全部门店，活动结束后不得把此目标用于其他月份。"
    body = "# 活动说明\n\n" + "背景说明与主题无关。" * 27 + "\n\n" + statement
    (kb / "KB-901.md").write_text(body, encoding="utf-8")
    facts = DocFacts(load_index(kb, tmp_path / "index.json", rebuild=True))
    assert statement in facts.sentences("KB-901")


def test_fact_units_are_invariant_under_line_endings(tmp_path):
    body = "# 试验说明\n\n" + "一般背景信息。\n" * 29 + "\n**首月（8 月 1 日至 8 月 31 日）全门店目标销量 777 杯。**\n操作规则另行通知。"
    versions = []
    for label, newline in [("lf", "\n"), ("crlf", "\r\n")]:
        kb = tmp_path / label
        kb.mkdir()
        (kb / "KB-901.md").write_bytes(body.replace("\n", newline).encode("utf-8"))
        index = load_index(kb, tmp_path / (label + ".json"), rebuild=True)
        versions.append(["".join(sentence.split()) for sentence in DocFacts(index).sentences("KB-901")])
    assert versions[0] == versions[1]
