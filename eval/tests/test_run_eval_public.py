#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""公开题库本身的体检 + 标准答案假服务的满分验证。

运行：

    cd 候选人作业包/eval/tests
    python3 -m unittest test_run_eval_public -v

这里检查的是**随作业包一起发出去的那份题库文件**：

* 结构合法、题号不重复、分值合计、每个类别的题量；
* 题干里不出现文档编号，不出现两道一模一样的问句；
* 引用检查和检索金标里的每一个 `doc_id` 都真的在知识库里（否则是出题错误）；
* 一个按题目要求作答的假服务能拿到 100%（评测脚本不会冤枉正确答案）；
* 随便改坏一个回答，分数就掉（评测脚本也不会放过错误答案）。
"""

from __future__ import annotations

import os
import re
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import run_eval as R                                   # noqa: E402
from test_run_eval import (KB_DIR, PUBLIC_QUESTIONS,   # noqa: E402
                           FakeArgs, ParaphrasingStub, RawStub, Service, Stub)

# 题库设计 §2 的公开题量
EXPECTED_COUNTS = {"metrics": 6, "retrieval": 15, "data": 6, "doc": 8,
                   "version": 3, "hybrid": 6, "multi_turn": 3, "refusal": 4,
                   "safety": 3, "health": 1}
TOTAL_POINTS = 100


class PublicSetCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.questions = R.load_questions(PUBLIC_QUESTIONS)
        cls.kb = R.KnowledgeBase(KB_DIR)


class TestPublicQuestionFile(PublicSetCase):
    def test_counts_per_category(self):
        counts = {}
        for q in self.questions:
            counts[q["category"]] = counts.get(q["category"], 0) + 1
        self.assertEqual(EXPECTED_COUNTS, counts)

    def test_total_points(self):
        self.assertEqual(TOTAL_POINTS, sum(q["points"] for q in self.questions))

    def test_ids_are_unique(self):
        ids = [q["id"] for q in self.questions]
        self.assertEqual(len(ids), len(set(ids)))

    def test_every_retrieval_question_requires_exactly_top_k(self):
        """契约 §4：索引片段数远多于 top_k，所以必须恰好返回 top_k 条。"""
        count = 0
        for q in self.questions:
            if q["category"] != "retrieval":
                continue
            count += 1
            self.assertEqual(q["top_k"], q.get("results_count"), q["id"])
        self.assertEqual(EXPECTED_COUNTS["retrieval"], count)

    def test_health_expects_documents_not_files(self):
        """契约 §1：`kb_docs` 是进索引的文档数，不是目录里的文件数。

        目录里有 `README.md` 这种不是文档的文件，所以文件数会比文档数多。
        """
        health = [q for q in self.questions if q["category"] == "health"]
        self.assertEqual(1, len(health))
        expected = health[0]["expect"]["kb_docs"]["value"]
        self.assertEqual(len(self.kb.docs), expected)
        files = sum(1 for _, _, names in os.walk(KB_DIR)
                    for n in names if not n.startswith("."))
        self.assertGreaterEqual(files, expected)
        if files != expected:
            self.assertNotEqual(files, expected,
                                "目录里的文件数不等于文档数——这是正常的，"
                                "kb_docs 不能改成数文件")

    def test_every_question_has_something_to_check(self):
        for q in self.questions:
            with self.subTest(q["id"]):
                if q["category"] == "retrieval":
                    self.assertTrue(q.get("gold_all") or q.get("gold_any"))
                elif q["category"] in ("metrics", "health"):
                    self.assertTrue(q.get("expect") or q.get("expect_days"))
                else:
                    self.assertTrue(q.get("turns"))
                    for turn in q["turns"]:
                        self.assertTrue(turn.get("checks"))
                        self.assertTrue(turn.get("question", "").strip())

    def test_no_doc_ids_in_question_text(self):
        for q in self.questions:
            for turn in q.get("turns") or []:
                self.assertNotIn("KB-", turn["question"], q["id"])
            self.assertNotIn("KB-", q.get("query", ""), q["id"])

    def test_no_duplicate_question_text(self):
        seen = {}
        for q in self.questions:
            for i, turn in enumerate(q.get("turns") or []):
                key = turn["question"]
                self.assertNotIn(key, seen,
                                 "%s 第 %d 轮与 %s 问法完全相同" % (q["id"], i + 1,
                                                                seen.get(key)))
                seen[key] = q["id"]

    def test_every_referenced_document_exists(self):
        for q in self.questions:
            for key in ("gold_all", "gold_any"):
                for doc_id in q.get(key) or []:
                    self.assertIn(doc_id, self.kb.docs, "%s -> %s" % (q["id"], doc_id))
            for turn in q.get("turns") or []:
                for key in ("cite_all", "cite_any", "cite_none"):
                    for doc_id in (turn["checks"].get(key) or []):
                        self.assertIn(doc_id, self.kb.docs,
                                      "%s -> %s" % (q["id"], doc_id))

    def test_public_set_only_references_documents_that_ship(self):
        """公开题库只允许提到随包发出的那些文档。

        （评测时 `knowledge_base/` 会被换成另一份，文档有增有改；换了以后哪些
        文档在、哪些不在，这里不做任何假设。）
        """
        with open(PUBLIC_QUESTIONS, encoding="utf-8") as fh:
            body = fh.read()
        for doc_id in set(re.findall(r"KB-\d{3}", body)):
            self.assertIn(doc_id, self.kb.docs, doc_id)

    def test_tolerances_are_sane(self):
        for q in self.questions:
            for spec in (q.get("expect") or {}).values():
                self.assertIn("value", spec)
                self.assertGreaterEqual(spec.get("tol", 0), 0)
            for turn in q.get("turns") or []:
                for key in ("numbers_all", "numbers_any", "numbers_none"):
                    for spec in turn["checks"].get(key) or []:
                        self.assertIsInstance(spec["value"], (int, float))
                        self.assertGreaterEqual(spec["tol"], 0)

    def test_the_no_reason_question_forbids_citations_and_invention(self):
        """E11（公开集）：零营业额要有证据、不许引用、必须承认找不到原因。

        原因词黑名单（装修 / 消防 / 台风…）已按 2026-09-21 的评审裁定去掉：
        "我查过装修、消防这些原因，都没有记载"是**正确**回答，黑名单会冤枉它。
        守住这题的是另外三条：cite_max=0（编不出来源）、evidence_required
        （零营业额必须来自真查询）、text_any（必须如实说没找到）。
        """
        hits = [q for q in self.questions
                for t in q.get("turns") or [] if "17" in t["question"]
                and "19" in t["question"]]
        self.assertEqual(1, len(hits))
        checks = hits[0]["turns"][0]["checks"]
        self.assertEqual(0, checks.get("cite_max"))
        self.assertTrue(checks.get("evidence_required"))
        self.assertTrue(checks.get("text_any"))
        self.assertNotIn("text_none", checks)
        self.assertIn(0, [spec["value"] for spec in checks["numbers_all"]])
        self.assertIn(0, [spec["value"] for spec in checks["evidence_numbers"]])

    def test_refusal_questions_do_not_grade_wording(self):
        """拒答题只看结构：不许有 text_any 关键词表，但要挡住编造的数字。"""
        for q in self.questions:
            if q["category"] != "refusal":
                continue
            for turn in q["turns"]:
                checks = turn["checks"]
                self.assertEqual(["refusal"], checks["answer_type_in"])
                self.assertNotIn("text_any", checks)
                self.assertIn("numbers_none_beyond_question", checks)

    def test_direction_questions_are_graded_by_number_and_one_direction(self):
        """涨跌题：差额 + 一个方向词；相反方向的说法一个都不能出现。

        （这一项 2026-09-22 收紧过：原来只要求出现一个方向词，
        于是"上涨"和"下跌"一起写也能过。）
        """
        found = 0
        for q in self.questions:
            for turn in q.get("turns") or []:
                spec = turn["checks"].get("signed_delta")
                if spec:
                    found += 1
                    self.assertTrue(spec["words"])
                    self.assertTrue(spec["words_none"])
                    self.assertFalse(set(spec["words"]) & set(spec["words_none"]))
                    self.assertNotIn("text_none", turn["checks"])
        self.assertGreaterEqual(found, 1)

    def test_document_facts_are_checkable_in_a_quote(self):
        """文档来源的数字与说法走 fact_*，并且都挂在真实存在的文档上。"""
        facts = 0
        for q in self.questions:
            for turn in q.get("turns") or []:
                for label in ("fact_all", "fact_any"):
                    spec = turn["checks"].get(label)
                    if not spec:
                        continue
                    facts += 1
                    self.assertTrue(spec["docs"])
                    for doc_id in spec["docs"]:
                        self.assertIn(doc_id, self.kb.docs)
                    self.assertTrue(spec["texts"] or spec["numbers"])
        self.assertGreaterEqual(facts, 10)

    def test_safety_questions_recheck_the_metrics(self):
        safety = [q for q in self.questions if q["category"] == "safety"]
        destructive = [q for q in safety if q.get("post", {}).get("metrics_unchanged")]
        self.assertGreaterEqual(len(destructive), 2)
        for q in destructive:
            self.assertEqual(2, len(q["post"]["range"]))


class TestDirectionWording(PublicSetCase):
    """涨跌题的措辞：比较级随主语翻转，只有相反的**变化方向**才算没结论。

    用公开集里那道真实的涨跌题跑，数字全部取自题目本身。
    """

    def direction_question(self):
        for q in self.questions:
            for turn in q.get("turns") or []:
                if "signed_delta" in turn["checks"]:
                    return q
        self.fail("公开集里没有涨跌题")

    def failed_for(self, answer_text):
        q = self.direction_question()
        checks = q["turns"][0]["checks"]
        before, after = [spec["value"] for spec in checks["numbers_all"]]
        reply = {"answer": answer_text, "answer_type": "data", "citations": [],
                 "data_evidence": [{"tool": "query_metrics",
                                    "params": {"metric": "aov"},
                                    "result": {"before": before, "after": after}}],
                 "trace_id": "t-direction"}
        service = Service(RawStub([q], self.kb, reply))
        try:
            evaluator = R.Evaluator(R.Client(service.url, timeout=10), [q], self.kb)
            evaluator.run()
            report = R.build_report(evaluator.results, evaluator,
                                    FakeArgs(PUBLIC_QUESTIONS, KB_DIR))
        finally:
            service.stop()
        return {c["name"] for t in report["questions"][0]["turns"]
                for c in t["checks"] if not c["passed"]}

    def sentences(self):
        q = self.direction_question()
        checks = q["turns"][0]["checks"]
        before, after = ("%.2f" % spec["value"] for spec in checks["numbers_all"])
        delta = "%.2f" % abs(checks["signed_delta"]["value"])
        if checks["signed_delta"]["value"] > 0:
            return {
                "ok_comparative_lower": "6 月客单价 %s 元，低于 7 月的 %s 元，"
                                        "7 月涨了 %s 元。" % (before, after, delta),
                "ok_comparative_less": "6 月客单价 %s 元，7 月 %s 元；6 月比 7 月"
                                       "少了 %s 元，7 月上涨。" % (before, after, delta),
                "both_ways": "6 月 %s 元，7 月 %s 元，涨了 %s 元，但也可能是下跌。"
                             % (before, after, delta),
            }
        return {
            "ok_comparative_lower": "6 月客单价 %s 元，高于 7 月的 %s 元，"
                                    "7 月跌了 %s 元。" % (before, after, delta),
            "ok_comparative_less": "6 月客单价 %s 元，7 月 %s 元；6 月比 7 月"
                                   "多了 %s 元，7 月下降。" % (before, after, delta),
            "both_ways": "6 月 %s 元，7 月 %s 元，跌了 %s 元，但也可能是上涨。"
                         % (before, after, delta),
        }

    def test_comparatives_about_the_other_month_are_fine(self):
        for name in ("ok_comparative_lower", "ok_comparative_less"):
            with self.subTest(name):
                self.assertEqual(set(), self.failed_for(self.sentences()[name]))

    def test_both_change_directions_fail_only_signed_delta(self):
        self.assertEqual({"signed_delta"},
                         self.failed_for(self.sentences()["both_ways"]))


class TestPerfectAnswersScoreFull(PublicSetCase):
    stub_class = Stub

    def run_public(self, variation=None):
        stub = self.stub_class(self.questions, self.kb, variation)
        service = Service(stub)
        try:
            client = R.Client(service.url, timeout=10)
            evaluator = R.Evaluator(client, self.questions, self.kb)
            evaluator.run()
            report = R.build_report(evaluator.results, evaluator,
                                    FakeArgs(PUBLIC_QUESTIONS, KB_DIR))
            report["_facts_via_quote"] = stub.facts_via_quote
            return report
        finally:
            service.stop()

    def test_perfect_stub_scores_100_percent(self):
        report = self.run_public()
        failed = [(q["id"], c["name"], c["reason"])
                  for q in report["questions"] for t in q["turns"]
                  for c in t["checks"] if not c["passed"]]
        self.assertEqual([], failed)
        self.assertEqual(TOTAL_POINTS, report["total"]["earned"])
        self.assertEqual(1.0, report["total"]["ratio"])
        for entry in report["per_category"].values():
            self.assertEqual(1.0, entry["ratio"])

    def test_quotes_must_come_from_the_real_documents(self):
        report = self.run_public(variation="quotes_verbatim")
        self.assertLess(report["total"]["earned"], TOTAL_POINTS)
        self.assertIn("quotes_verbatim",
                      {c["name"] for q in report["questions"] for t in q["turns"]
                       for c in t["checks"] if not c["passed"]})

    def test_weekly_report_estimate_is_rejected(self):
        """T4：周报里的估算值出现在回答里，就该红。"""
        report = self.run_public(variation="numbers_none")
        reds = {(q["id"], c["name"]) for q in report["questions"]
                for t in q["turns"] for c in t["checks"] if not c["passed"]}
        self.assertTrue(any(name == "numbers_none" for _, name in reds))


class TestParaphrasedAnswersScoreFull(TestPerfectAnswersScoreFull):
    """同样答对、但每一处都换一种说法，也必须 100%。

    这份 stub 把文档事实放进 quote、把数字写成千分位和百分数、
    纯数据题答 `hybrid`、涨跌题复述"涨还是跌"——全是真实模型会做的事。
    """

    stub_class = ParaphrasingStub

    def test_perfect_stub_scores_100_percent(self):
        report = self.run_public()
        failed = [(q["id"], c["name"], c["reason"])
                  for q in report["questions"] for t in q["turns"]
                  for c in t["checks"] if not c["passed"]]
        self.assertEqual([], failed)
        self.assertEqual(TOTAL_POINTS, report["total"]["earned"])
        self.assertGreaterEqual(report["_facts_via_quote"], 10,
                                "文档事实应该有相当一部分是靠 quote 交付的")


if __name__ == "__main__":
    unittest.main()
