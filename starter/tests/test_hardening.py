"""Regression cases from the submission audit; never mutate the supplied database."""
import sqlite3

import httpx
import pytest

from kbqa.cleaning import open_readonly
from kbqa.live import LiveEngine
from kbqa.llm import LLMClient
from kbqa.tools import DataTools
from kbqa.trace import Trace


def database(tmp_path):
    path = tmp_path / "sample.db"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE sales_clean(amount_cents INTEGER)")
        conn.execute("INSERT INTO sales_clean VALUES (9876)")
    return path


def test_database_connection_is_physically_readonly(tmp_path):
    conn = open_readonly(database(tmp_path))
    try:
        with pytest.raises(sqlite3.DatabaseError):
            conn.execute("DELETE FROM sales_clean")
    finally:
        conn.close()


@pytest.mark.parametrize("sql", [
    "DELETE FROM sales_clean", "DROP TABLE sales_clean", "PRAGMA query_only=OFF",
    "ATTACH DATABASE ':memory:' AS other",
    "WITH x AS (SELECT 1) DELETE FROM sales_clean",
    "SELECT * FROM sales_clean; DELETE FROM sales_clean",
])
def test_sql_tool_rejects_mutation_and_keeps_rows(tmp_path, sql):
    tools = DataTools(database(tmp_path))
    try:
        assert "error" in tools.run_sql(sql)
        assert tools.conn.execute("SELECT amount_cents FROM sales_clean").fetchone()[0] == 9876
    finally:
        tools.close()


def test_sql_tool_allows_cte_and_reports_query_errors(tmp_path):
    tools = DataTools(database(tmp_path))
    try:
        assert tools.run_sql("WITH x AS (SELECT * FROM sales_clean) SELECT * FROM x")["rows"] == [{"amount_cents": 9876}]
        assert "error" in tools.run_sql("SELECT * FROM nonexistent")
    finally:
        tools.close()


def test_internal_error_is_recorded(client, monkeypatch):
    from kbqa import server
    def broken(*args):
        raise ValueError("audit sentinel")
    monkeypatch.setattr(server.service().planner, "plan", broken)
    response = client.post("/api/chat", json={"question": "七月营业额"})
    assert response.status_code == 200
    data = response.json()
    assert data["answer_type"] == "refusal"
    trace = client.get("/api/trace/" + data["trace_id"]).json()
    assert "audit sentinel" in str(trace["errors"])


def test_live_cannot_use_document_target_as_sales_amount(client):
    from kbqa import server
    service = server.service()
    plan = service.planner.plan("618当天S02牛肉poke卖了多少份，达到目标了吗？", [])
    engine = LiveEngine(None, service.answerer, service.run_tool, "2026-09-01", service.data_period)
    answer = engine._finalise(plan, "净营业额为 120 元。[KB-023]", [], {}, Trace("audit", plan.question, None))
    assert answer.data_evidence
    assert "净营业额为 120 元" not in answer.answer


def test_llm_trace_preserves_full_request_and_output(monkeypatch):
    payload = {"choices": [{"finish_reason": "stop", "message": {"role": "assistant", "content": "完整回答" * 1500}}]}
    def fake_post(*args, **kwargs):
        return httpx.Response(200, json=payload)
    async def async_post(*args, **kwargs):
        return fake_post()
    monkeypatch.setattr(httpx, "post", fake_post)
    monkeypatch.setattr(httpx.AsyncClient, "post", async_post)
    messages = [{"role": "user", "content": "长提示词" * 1500}]
    records = []
    LLMClient("http://example.invalid", "secret-audit-key", "model").chat(messages, on_call=records.append)
    assert records[0]["request"]["messages"] == messages
    assert records[0]["response"] == payload
    assert "secret-audit-key" not in str(records)


def test_data_chat_trace_contains_executed_results(client):
    data = client.post("/api/chat", json={"question": "2026年7月全店营业额是多少？"}).json()
    assert data["data_evidence"]
    trace = client.get("/api/trace/" + data["trace_id"]).json()
    assert any(s["step"] == "tool" and "result" in s["detail"] for s in trace["steps"])
