#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""`llm_gateway.py` 的自测：只依赖标准库，全部在本进程内的空闲端口上跑，不碰任何外部服务。

运行：

    cd 候选人作业包/eval/tests
    python3 -m unittest test_llm_gateway -v

测试结构：

* `TestFakeRouting`        —— 只有 `{prefix}/chat/completions` 可用，其余路径一律 404 并被记录。
* `TestFakeAuth`           —— 必须是 `Authorization: Bearer <注入的 Key>`，否则 401。
* `TestFakeThinking`       —— 思考默认开启，`thinking.type=disabled` 关掉（D5/D7）。
* `TestFakeToolRounds`     —— 两轮工具调用，其中一轮一次返回两个工具调用（D12）。
* `TestFakeEchoRule`       —— 带 `tools` 时不原样回传 `reasoning_content` 就 400（D8）。
* `TestFakeScenarios`      —— 15 个场景逐个验证形状（D10/D11/D13/D14）。
* `TestFakeStreaming`      —— SSE 先思考后正文、工具分片、`[DONE]`、keep-alive 注释。
* `TestFakeControl`        —— 控制面：切场景、查请求、清空。
* `TestProxy`              —— 字节级透传、日志内容、Key 只记长度、`--inject-thinking`。
* `TestPreflightCompliant` —— 一个**完全合规**的迷你服务，14 项检查全绿。
* `TestPreflightVariants`  —— 每个变体只违反一条规则，断言**恰好**那一项变红。
  （一个永远不会红的检查什么也证明不了，所以每一项都必须有一个能让它变红的变体。）
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import socket
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import llm_gateway as G  # noqa: E402

# ======================================================================================
# 测试里用的时间参数：保证整套测试在几十秒内跑完。
# ======================================================================================

SLOW_DELAY = 0.12
KEEPALIVES = 2
HANG_CAP = 8.0
MAX_CHAT_SECONDS = 2.5
CHAT_TIMEOUT = 12.0
FAST_LLM_TIMEOUT = 0.8
SLOW_LLM_TIMEOUT = 4.0

API_KEY = "preflight-key-3b9c1f"

TOOL_METRICS = {
    "type": "function",
    "function": {
        "name": "query_metrics",
        "description": "按门店和日期区间查经营指标。",
        "parameters": {
            "type": "object",
            "properties": {
                "start": {"type": "string", "description": "开始日期"},
                "end": {"type": "string", "description": "结束日期"},
                "store_id": {"type": "string"},
                "top_k": {"type": "integer"},
            },
            "required": ["start", "end", "store_id"],
        },
    },
}

TOOL_DOCS = {
    "type": "function",
    "function": {
        "name": "search_docs",
        "description": "在知识库里检索。",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "top_k": {"type": "integer"},
            },
            "required": ["query"],
        },
    },
}

#: 官方文档允许省略 `parameters` 来表示无参数函数。预检不能把这种写法判成违规。
TOOL_PARAMETERLESS = {"type": "function", "function": {"name": "ping"}}

#: 只声明 `type: object`、不给 `properties`，也是合法的空对象 schema。
TOOL_BARE_OBJECT = {
    "type": "function",
    "function": {"name": "healthcheck", "parameters": {"type": "object"}},
}

TOOLS = [TOOL_METRICS, TOOL_DOCS]

PREFLIGHT_QUESTIONS = ("你们的退款规则是怎么规定的？",)


# ======================================================================================
# 通用小工具
# ======================================================================================


def raw_request(url, payload=None, api_key=API_KEY, method=None, timeout=20.0):
    """发一个原始请求，返回 (status, headers, bytes)。HTTP 错误也当成正常响应返回。"""
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
    headers = {}
    if data is not None:
        headers["Content-Type"] = "application/json; charset=utf-8"
    if api_key is not None:
        headers["Authorization"] = "Bearer " + api_key
    request = urllib.request.Request(
        url, data=data, headers=headers, method=method or ("POST" if data else "GET")
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return int(response.status), dict(response.headers), response.read()
    except urllib.error.HTTPError as exc:
        with exc:
            return int(exc.code), dict(exc.headers), exc.read()


def sse_lines(raw):
    return raw.decode("utf-8").splitlines()


def sse_events(raw):
    """把 SSE 正文拆成 data 行的列表，`[DONE]` 用字符串表示，注释行忽略。"""
    out = []
    for line in sse_lines(raw):
        if not line.startswith("data:"):
            continue
        payload = line[len("data:") :].strip()
        if payload == "[DONE]":
            out.append("[DONE]")
        elif payload:
            out.append(json.loads(payload))
    return out


def delta_of(chunk):
    return chunk["choices"][0]["delta"]


def reassemble(events):
    """测试自己的一份 SSE 拼装实现，刻意不复用被测模块，免得互相遮掩。"""
    content, reasoning = [], []
    calls = {}
    finish = None
    usage = None
    for chunk in events:
        if chunk == "[DONE]":
            continue
        if isinstance(chunk.get("usage"), dict):
            usage = chunk["usage"]
        for choice in chunk.get("choices") or []:
            if choice.get("finish_reason"):
                finish = choice["finish_reason"]
            delta = choice.get("delta") or {}
            if isinstance(delta.get("content"), str):
                content.append(delta["content"])
            if isinstance(delta.get("reasoning_content"), str):
                reasoning.append(delta["reasoning_content"])
            for raw in delta.get("tool_calls") or []:
                index = raw.get("index", 0)
                slot = calls.setdefault(
                    index, {"id": None, "type": "function", "function": {"name": None, "arguments": ""}}
                )
                if raw.get("id"):
                    slot["id"] = raw["id"]
                fn = raw.get("function") or {}
                if fn.get("name"):
                    slot["function"]["name"] = fn["name"]
                if isinstance(fn.get("arguments"), str):
                    slot["function"]["arguments"] += fn["arguments"]
    return {
        "content": "".join(content),
        "reasoning_content": "".join(reasoning),
        "tool_calls": [calls[k] for k in sorted(calls)],
        "finish_reason": finish,
        "usage": usage,
    }


# ======================================================================================
# 假 DeepSeek 服务
# ======================================================================================


class FakeServerTestCase(unittest.TestCase):
    scenario = "normal"

    def setUp(self):
        self.state = G.FakeLLMState(
            scenario=self.scenario,
            api_key=API_KEY,
            slow_delay=SLOW_DELAY,
            keepalives=KEEPALIVES,
            hang_cap=HANG_CAP,
        )
        self.server, self.thread = G.start_fake_server(port=0, state=self.state)
        self.addCleanup(G.stop_server, self.server, self.thread)
        self.origin = G.server_origin(self.server)
        self.base = G.fake_base_url(self.server)
        self.chat_url = self.base + "/chat/completions"

    def chat(self, payload, **kwargs):
        return raw_request(self.chat_url, payload, **kwargs)

    def chat_json(self, payload, **kwargs):
        status, _, raw = self.chat(payload, **kwargs)
        return status, json.loads(raw.decode("utf-8"))

    def ask(self, **extra):
        payload = {"model": "m", "messages": [{"role": "user", "content": "问题"}]}
        payload.update(extra)
        return self.chat_json(payload)


class TestFakeRouting(FakeServerTestCase):
    def test_base_url_carries_the_unusual_prefix(self):
        self.assertTrue(self.base.endswith("/ds-gw"))
        self.assertNotIn("/v1", self.base)

    def test_only_the_prefixed_chat_completions_path_works(self):
        status, body = self.ask()
        self.assertEqual(status, 200)
        self.assertEqual(body["object"], "chat.completion")

    def test_every_other_path_is_404_and_recorded(self):
        others = [
            "/v1/chat/completions",
            "/chat/completions",
            "/ds-gw/v1/chat/completions",
            "/anthropic/v1/messages",
            "/responses",
            "/beta/chat/completions",
            "/ds-gw/models",
            "/models",
            "/user/balance",
            "/ds-gw/user/balance",
        ]
        for path in others:
            with self.subTest(path=path):
                status, _, _ = raw_request(self.origin + path, {"model": "m", "messages": []})
                self.assertEqual(status, 404)
        recorded = {(r["method"], r["path"]) for r in self.state.list_requests()}
        for path in others:
            self.assertIn(("POST", path), recorded)

    def test_a_404_body_looks_like_a_deepseek_error(self):
        status, _, raw = raw_request(self.origin + "/v1/chat/completions", {"model": "m"})
        body = json.loads(raw.decode("utf-8"))
        self.assertEqual(status, 404)
        self.assertIn("error", body)
        self.assertEqual(body["error"]["code"], "not_found")

    def test_get_on_the_chat_path_is_also_404(self):
        status, _, _ = raw_request(self.chat_url, None)
        self.assertEqual(status, 404)

    def test_records_carry_the_status_we_answered_with(self):
        self.ask()
        raw_request(self.origin + "/models", {"a": 1})
        statuses = [(r["path"], r["response_status"]) for r in self.state.list_requests()]
        self.assertIn(("/ds-gw/chat/completions", 200), statuses)
        self.assertIn(("/models", 404), statuses)


class TestFakeAuth(FakeServerTestCase):
    def test_missing_authorization_is_401(self):
        status, _, raw = self.chat({"model": "m", "messages": []}, api_key=None)
        body = json.loads(raw.decode("utf-8"))
        self.assertEqual(status, 401)
        self.assertEqual(body["error"]["code"], "authentication_error")
        self.assertIn("认证失败", body["error"]["message"])

    def test_wrong_key_is_401(self):
        status, _, _ = self.chat({"model": "m", "messages": []}, api_key="not-the-key")
        self.assertEqual(status, 401)

    def test_non_bearer_scheme_is_401(self):
        request = urllib.request.Request(
            self.chat_url,
            data=b"{}",
            headers={"Content-Type": "application/json", "Authorization": "Basic " + API_KEY},
            method="POST",
        )
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(request, timeout=10)
        self.assertEqual(caught.exception.code, 401)
        caught.exception.close()

    def test_the_key_value_is_never_recorded(self):
        self.ask()
        record = self.state.list_requests()[-1]
        blob = json.dumps(record, ensure_ascii=False)
        self.assertNotIn(API_KEY, blob)
        self.assertIn("<redacted len=", record["headers"]["Authorization"])
        self.assertEqual(record["authorization"]["scheme"], "Bearer")
        self.assertEqual(record["authorization"]["token_length"], len(API_KEY))
        self.assertTrue(record["authorization"]["matches_expected"])


class TestFakeThinking(FakeServerTestCase):
    def test_thinking_is_on_by_default_and_carries_the_marker(self):
        _, body = self.ask()
        message = body["choices"][0]["message"]
        self.assertTrue(message["reasoning_content"])
        self.assertIn(self.state.marker, message["reasoning_content"])
        self.assertNotIn(self.state.marker, message["content"])

    def test_thinking_can_be_disabled(self):
        _, body = self.ask(thinking={"type": "disabled"})
        message = body["choices"][0]["message"]
        self.assertNotIn("reasoning_content", message)
        self.assertTrue(message["content"])

    def test_thinking_enabled_explicitly_still_thinks(self):
        _, body = self.ask(thinking={"type": "enabled"})
        self.assertTrue(body["choices"][0]["message"]["reasoning_content"])

    def test_usage_reports_reasoning_tokens(self):
        _, body = self.ask()
        self.assertGreater(
            body["usage"]["completion_tokens_details"]["reasoning_tokens"], 0
        )

    def test_marker_is_unique_per_server(self):
        other = G.FakeLLMState()
        self.assertNotEqual(other.marker, self.state.marker)


class TestFakeToolRounds(FakeServerTestCase):
    def first_round(self):
        status, body = self.chat_json(
            {"model": "m", "messages": [{"role": "user", "content": "问题"}], "tools": TOOLS}
        )
        self.assertEqual(status, 200)
        return body["choices"][0]

    def test_first_round_returns_two_tool_calls_in_one_message(self):
        choice = self.first_round()
        self.assertEqual(choice["finish_reason"], "tool_calls")
        self.assertEqual(choice["message"]["content"], "")
        self.assertEqual(len(choice["message"]["tool_calls"]), 2)

    def test_tool_call_names_come_from_the_declared_tools(self):
        choice = self.first_round()
        names = [c["function"]["name"] for c in choice["message"]["tool_calls"]]
        self.assertEqual(names, ["query_metrics", "search_docs"])

    def test_arguments_are_a_json_string_built_from_each_schema(self):
        choice = self.first_round()
        for call, tool in zip(choice["message"]["tool_calls"], TOOLS):
            arguments = call["function"]["arguments"]
            self.assertIsInstance(arguments, str)
            parsed = json.loads(arguments)
            schema = tool["function"]["parameters"]
            self.assertTrue(set(parsed) <= set(schema["properties"]))
            for name in schema["required"]:
                self.assertIn(name, parsed)
                expected = schema["properties"][name]["type"]
                if expected == "string":
                    self.assertIsInstance(parsed[name], str)
                elif expected == "integer":
                    self.assertIsInstance(parsed[name], int)

    def test_a_parameterless_tool_is_called_with_empty_arguments(self):
        """省略 `parameters` 的无参函数，参数必须是 `{}`，不能凭空编出没声明过的字段。"""
        _, body = self.chat_json(
            {
                "model": "m",
                "messages": [{"role": "user", "content": "问题"}],
                "tools": [TOOL_PARAMETERLESS],
            }
        )
        calls = body["choices"][0]["message"]["tool_calls"]
        self.assertTrue(calls)
        for call in calls:
            self.assertEqual(call["function"]["name"], "ping")
            self.assertEqual(call["function"]["arguments"], "{}")
            self.assertEqual(json.loads(call["function"]["arguments"]), {})

    def test_a_bare_object_schema_is_also_called_with_empty_arguments(self):
        _, body = self.chat_json(
            {
                "model": "m",
                "messages": [{"role": "user", "content": "问题"}],
                "tools": [TOOL_BARE_OBJECT],
            }
        )
        for call in body["choices"][0]["message"]["tool_calls"]:
            self.assertEqual(json.loads(call["function"]["arguments"]), {})

    def test_the_fallback_schema_is_only_for_requests_without_tools(self):
        """兜底 schema 只在请求压根没带 tools 时才用。"""
        name, params = G.tool_function([], 0)
        self.assertEqual(params, G.FALLBACK_TOOL_PARAMETERS)
        self.assertEqual(G.tool_function([TOOL_PARAMETERLESS], 0), ("ping", {}))
        self.assertEqual(
            G.tool_function([{"type": "function", "function": {"name": "x", "parameters": "nope"}}], 0),
            ("x", {}),
        )

    def test_two_tool_rounds_then_a_final_answer(self):
        first = self.first_round()
        messages = [{"role": "user", "content": "问题"}, first["message"]]
        messages += [
            {"role": "tool", "tool_call_id": c["id"], "content": "{}"}
            for c in first["message"]["tool_calls"]
        ]
        status, body = self.chat_json({"model": "m", "messages": messages, "tools": TOOLS})
        second = body["choices"][0]
        self.assertEqual(status, 200)
        self.assertEqual(second["finish_reason"], "tool_calls")
        self.assertEqual(len(second["message"]["tool_calls"]), 1)

        messages = messages + [second["message"]]
        messages += [
            {"role": "tool", "tool_call_id": c["id"], "content": "{}"}
            for c in second["message"]["tool_calls"]
        ]
        status, body = self.chat_json({"model": "m", "messages": messages, "tools": TOOLS})
        third = body["choices"][0]
        self.assertEqual(third["finish_reason"], "stop")
        self.assertNotIn("tool_calls", third["message"])
        self.assertTrue(third["message"]["content"])

    def test_reasoning_differs_between_rounds(self):
        first = self.first_round()
        messages = [{"role": "user", "content": "问题"}, first["message"]]
        messages += [
            {"role": "tool", "tool_call_id": c["id"], "content": "{}"}
            for c in first["message"]["tool_calls"]
        ]
        _, body = self.chat_json({"model": "m", "messages": messages, "tools": TOOLS})
        self.assertNotEqual(
            first["message"]["reasoning_content"],
            body["choices"][0]["message"]["reasoning_content"],
        )

    def test_without_tools_the_answer_comes_straight_back(self):
        _, body = self.ask()
        choice = body["choices"][0]
        self.assertEqual(choice["finish_reason"], "stop")
        self.assertNotIn("tool_calls", choice["message"])

    def test_a_service_that_rewrites_ids_still_terminates(self):
        """就算候选人把 tool_call_id 改写了，轮次也会推进，不会两边死等。"""
        first = self.first_round()
        message = json.loads(json.dumps(first["message"]))
        for index, call in enumerate(message["tool_calls"]):
            call["id"] = "rewritten-%d" % index
        messages = [{"role": "user", "content": "问题"}, message]
        _, body = self.chat_json({"model": "m", "messages": messages, "tools": TOOLS})
        self.assertEqual(len(body["choices"][0]["message"]["tool_calls"]), 1)


class TestFakeEchoRule(FakeServerTestCase):
    def setUp(self):
        super().setUp()
        _, body = self.chat_json(
            {"model": "m", "messages": [{"role": "user", "content": "问题"}], "tools": TOOLS}
        )
        self.assistant = body["choices"][0]["message"]
        self.results = [
            {"role": "tool", "tool_call_id": c["id"], "content": "{}"}
            for c in self.assistant["tool_calls"]
        ]

    def second(self, assistant, tools=TOOLS):
        payload = {
            "model": "m",
            "messages": [{"role": "user", "content": "问题"}, assistant] + self.results,
        }
        if tools is not None:
            payload["tools"] = tools
        return self.chat_json(payload)

    def test_verbatim_echo_is_accepted(self):
        status, _ = self.second(self.assistant)
        self.assertEqual(status, 200)

    def test_dropping_reasoning_content_is_400(self):
        stripped = {k: v for k, v in self.assistant.items() if k != "reasoning_content"}
        status, body = self.second(stripped)
        self.assertEqual(status, 400)
        self.assertIn("reasoning_content", body["error"]["message"])
        self.assertEqual(body["error"]["code"], "invalid_request_error")

    def test_altering_reasoning_content_is_400(self):
        altered = dict(self.assistant)
        altered["reasoning_content"] = altered["reasoning_content"][:-3]
        status, _ = self.second(altered)
        self.assertEqual(status, 400)

    def test_empty_reasoning_content_is_400(self):
        altered = dict(self.assistant)
        altered["reasoning_content"] = ""
        status, _ = self.second(altered)
        self.assertEqual(status, 400)

    def test_without_tools_the_echo_is_ignored(self):
        stripped = {k: v for k, v in self.assistant.items() if k != "reasoning_content"}
        status, _ = self.second(stripped, tools=None)
        self.assertEqual(status, 200)

    def test_the_400_is_recorded_with_a_note(self):
        stripped = {k: v for k, v in self.assistant.items() if k != "reasoning_content"}
        self.second(stripped)
        record = self.state.list_requests()[-1]
        self.assertEqual(record["response_status"], 400)
        self.assertIn("reasoning_content", record["response_note"])

    def test_disabled_thinking_never_trips_the_echo_rule(self):
        _, body = self.chat_json(
            {
                "model": "m",
                "messages": [{"role": "user", "content": "问题"}],
                "tools": TOOLS,
                "thinking": {"type": "disabled"},
            }
        )
        message = body["choices"][0]["message"]
        self.assertNotIn("reasoning_content", message)
        status, _ = self.chat_json(
            {
                "model": "m",
                "messages": [{"role": "user", "content": "问题"}, message]
                + [
                    {"role": "tool", "tool_call_id": c["id"], "content": "{}"}
                    for c in message["tool_calls"]
                ],
                "tools": TOOLS,
                "thinking": {"type": "disabled"},
            }
        )
        self.assertEqual(status, 200)


class TestFakeScenarios(FakeServerTestCase):
    def switch(self, scenario):
        self.state.set_scenario(scenario)

    def test_every_documented_scenario_is_reachable(self):
        self.assertEqual(len(G.SCENARIOS), 16)
        for scenario in G.SCENARIOS:
            with self.subTest(scenario=scenario):
                self.state.set_scenario(scenario)

    def test_every_error_finish_reason_in_the_docs_has_a_scenario(self):
        """D11 列出的四种要按错误处理的 finish_reason，假服务都能复现。"""
        produced = set()
        for scenario in ("thinking_starved", "content_filter", "insufficient_resource", "aborted"):
            self.switch(scenario)
            _, body = self.ask(max_tokens=256)
            produced.add(body["choices"][0]["finish_reason"])
        self.assertEqual(
            produced, {"length", "content_filter", "insufficient_system_resource", "aborted"}
        )

    def test_thinking_starved_bites_only_below_the_threshold(self):
        self.switch("thinking_starved")
        _, small = self.ask(max_tokens=256)
        self.assertEqual(small["choices"][0]["finish_reason"], "length")
        self.assertEqual(small["choices"][0]["message"]["content"], "")
        _, big = self.ask(max_tokens=4096)
        self.assertEqual(big["choices"][0]["finish_reason"], "stop")
        self.assertTrue(big["choices"][0]["message"]["content"])
        _, absent = self.ask()
        self.assertEqual(absent["choices"][0]["finish_reason"], "stop")

    def test_empty_content_keeps_finish_reason_stop(self):
        self.switch("empty_content")
        _, body = self.ask()
        self.assertEqual(body["choices"][0]["message"]["content"], "")
        self.assertEqual(body["choices"][0]["finish_reason"], "stop")
        self.assertTrue(body["choices"][0]["message"]["reasoning_content"])

    def test_json_empty_bites_only_with_json_object(self):
        self.switch("json_empty")
        _, plain = self.ask()
        self.assertTrue(plain["choices"][0]["message"]["content"])
        _, jsoned = self.ask(response_format={"type": "json_object"})
        self.assertEqual(jsoned["choices"][0]["message"]["content"], "")

    def test_json_object_normally_returns_parsable_json(self):
        _, body = self.ask(response_format={"type": "json_object"})
        self.assertIsInstance(json.loads(body["choices"][0]["message"]["content"]), dict)

    def test_unsupported_response_format_is_422(self):
        for bad in ("json_schema", "xml", None, 1):
            with self.subTest(bad=bad):
                status, body = self.ask(response_format={"type": bad})
                self.assertEqual(status, 422)
                self.assertIn("response_format.type", body["error"]["message"])

    def test_text_response_format_is_accepted(self):
        status, _ = self.ask(response_format={"type": "text"})
        self.assertEqual(status, 200)

    def test_bad_tool_args_returns_unparseable_arguments(self):
        self.switch("bad_tool_args")
        _, body = self.chat_json(
            {"model": "m", "messages": [{"role": "user", "content": "问题"}], "tools": TOOLS}
        )
        calls = body["choices"][0]["message"]["tool_calls"]
        self.assertTrue(calls)
        for call in calls:
            with self.assertRaises(ValueError):
                json.loads(call["function"]["arguments"])

    def test_content_filter_returns_partial_content(self):
        self.switch("content_filter")
        status, body = self.ask()
        choice = body["choices"][0]
        self.assertEqual(status, 200)
        self.assertEqual(choice["finish_reason"], "content_filter")
        self.assertTrue(choice["message"]["content"])
        self.assertTrue(choice["message"]["content"].startswith(G.FAKE_FILTERED_TEXT))

    def test_insufficient_resource_returns_empty_content(self):
        self.switch("insufficient_resource")
        status, body = self.ask()
        choice = body["choices"][0]
        self.assertEqual(status, 200)
        self.assertEqual(choice["finish_reason"], "insufficient_system_resource")
        self.assertEqual(choice["message"]["content"], "")

    def test_every_failure_scenario_output_carries_the_failure_marker(self):
        """失败场景的模型输出里一定有失败标记，P9 才能抓到“把失败当成功转发”。"""
        cases = {
            "empty_content": {},
            "insufficient_resource": {},
            "content_filter": {},
            "aborted": {},
            "thinking_starved": {"max_tokens": 256},
            "json_empty": {"response_format": {"type": "json_object"}},
        }
        for scenario, extra in cases.items():
            with self.subTest(scenario=scenario):
                self.switch(scenario)
                _, body = self.ask(**extra)
                message = body["choices"][0]["message"]
                blob = message.get("content", "") + message.get("reasoning_content", "")
                self.assertIn(self.state.failure_marker, blob)

    def test_bad_tool_args_arguments_carry_the_failure_marker(self):
        self.switch("bad_tool_args")
        _, body = self.chat_json(
            {"model": "m", "messages": [{"role": "user", "content": "问题"}], "tools": TOOLS}
        )
        for call in body["choices"][0]["message"]["tool_calls"]:
            self.assertIn(self.state.failure_marker, call["function"]["arguments"])
            with self.assertRaises(ValueError):
                json.loads(call["function"]["arguments"])

    def test_the_failure_marker_is_unique_per_server_and_absent_from_good_answers(self):
        self.assertTrue(self.state.failure_marker.startswith(G.FAILURE_MARKER_PREFIX))
        self.assertNotEqual(G.FakeLLMState().failure_marker, self.state.failure_marker)
        _, body = self.ask()  # normal 场景
        message = body["choices"][0]["message"]
        self.assertNotIn(
            self.state.failure_marker, message["content"] + message["reasoning_content"]
        )

    def test_disabled_thinking_keeps_empty_failures_empty(self):
        """关掉思考时，空正文的失败场景没地方放标记，也不能因此凭空多出 reasoning_content。"""
        self.switch("empty_content")
        _, body = self.ask(thinking={"type": "disabled"})
        message = body["choices"][0]["message"]
        self.assertNotIn("reasoning_content", message)
        self.assertEqual(message["content"], "")

    def test_aborted_returns_partial_content_not_an_empty_string(self):
        """aborted 的陷阱就在这里：正文非空，只判断“内容为空”的实现会把半句话发给用户。"""
        self.switch("aborted")
        status, body = self.ask()
        choice = body["choices"][0]
        self.assertEqual(status, 200)
        self.assertEqual(choice["finish_reason"], "aborted")
        content = choice["message"]["content"]
        self.assertTrue(content)
        # 前半截是正常回答的真前缀，所以看起来“像是”一个合法回答；
        # 尾巴上跟着失败标记，转发出去就会被 P9 抓到。
        self.assertTrue(content.startswith(G.FAKE_ABORTED_TEXT))
        self.assertTrue(G.FAKE_ANSWER_TEXT.startswith(G.FAKE_ABORTED_TEXT))
        self.assertTrue(content.endswith(self.state.failure_marker))

    def test_aborted_is_treated_as_a_failure_scenario(self):
        self.assertIn("aborted", G.ALWAYS_FAILING_SCENARIOS)

    def test_aborted_streams_the_partial_content_too(self):
        self.switch("aborted")
        status, _, raw = self.chat(
            {"model": "m", "messages": [{"role": "user", "content": "x"}], "stream": True}
        )
        self.assertEqual(status, 200)
        merged = reassemble(sse_events(raw))
        self.assertEqual(merged["finish_reason"], "aborted")
        self.assertTrue(merged["content"])

    def test_every_http_error_scenario_uses_its_documented_code(self):
        for scenario, (status_code, code, _type, cn_name) in G.HTTP_ERROR_SCENARIOS.items():
            with self.subTest(scenario=scenario):
                self.switch(scenario)
                status, body = self.ask()
                self.assertEqual(status, status_code)
                self.assertEqual(body["error"]["code"], code)
                self.assertIn(cn_name, body["error"]["message"])

    def test_http_error_scenarios_also_hit_valid_keys(self):
        self.switch("http_402")
        status, _ = self.ask()
        self.assertEqual(status, 402)

    def test_slow_non_stream_prefixes_the_body_with_blank_lines(self):
        self.switch("slow")
        started = time.monotonic()
        status, _, raw = self.chat({"model": "m", "messages": [{"role": "user", "content": "x"}]})
        elapsed = time.monotonic() - started
        self.assertEqual(status, 200)
        self.assertTrue(raw.startswith(b"\n" * KEEPALIVES))
        self.assertGreaterEqual(elapsed, SLOW_DELAY * 0.5)
        self.assertEqual(json.loads(raw.decode("utf-8"))["object"], "chat.completion")

    def test_slow_stream_emits_keep_alive_comments_before_and_between_data(self):
        self.switch("slow")
        status, _, raw = self.chat(
            {"model": "m", "messages": [{"role": "user", "content": "x"}], "stream": True}
        )
        lines = [line for line in sse_lines(raw) if line]
        self.assertEqual(status, 200)
        self.assertEqual(lines[:KEEPALIVES], [": keep-alive"] * KEEPALIVES)
        first_data = next(i for i, line in enumerate(lines) if line.startswith("data:"))
        self.assertIn(": keep-alive", lines[first_data + 1 :])
        self.assertEqual(lines[-1], "data: [DONE]")

    def test_hang_never_answers(self):
        self.switch("hang")
        with self.assertRaises(Exception):
            self.chat({"model": "m", "messages": []}, timeout=0.4)

    def test_unknown_scenario_is_rejected_at_construction(self):
        with self.assertRaises(ValueError):
            G.FakeLLMState(scenario="does-not-exist")
        with self.assertRaises(ValueError):
            self.state.set_scenario("does-not-exist")

    def test_identical_requests_give_identical_bytes(self):
        payload = {"model": "m", "messages": [{"role": "user", "content": "x"}]}
        _, _, first = self.chat(payload)
        _, _, second = self.chat(payload)
        self.assertEqual(first, second)


class TestFakeStreaming(FakeServerTestCase):
    def stream(self, **extra):
        payload = {"model": "m", "messages": [{"role": "user", "content": "问题"}], "stream": True}
        payload.update(extra)
        status, headers, raw = self.chat(payload)
        self.assertEqual(status, 200)
        self.assertIn("text/event-stream", headers.get("Content-Type", ""))
        return sse_events(raw)

    def test_reasoning_deltas_arrive_before_content_deltas(self):
        events = self.stream()
        kinds = []
        for chunk in events:
            if chunk == "[DONE]":
                continue
            delta = delta_of(chunk)
            if delta.get("reasoning_content"):
                kinds.append("r")
            elif delta.get("content"):
                kinds.append("c")
        self.assertTrue(kinds.count("r") > 0 and kinds.count("c") > 0)
        self.assertLess(max(i for i, k in enumerate(kinds) if k == "r"),
                        min(i for i, k in enumerate(kinds) if k == "c"))

    def test_the_stream_ends_with_done(self):
        events = self.stream()
        self.assertEqual(events[-1], "[DONE]")
        self.assertEqual(events.count("[DONE]"), 1)

    def test_stream_reassembles_to_the_same_payload_as_non_stream(self):
        events = self.stream()
        merged = reassemble(events)
        _, blocking = self.ask()
        message = blocking["choices"][0]["message"]
        self.assertEqual(merged["content"], message["content"])
        self.assertEqual(merged["reasoning_content"], message["reasoning_content"])
        self.assertEqual(merged["finish_reason"], blocking["choices"][0]["finish_reason"])

    def test_tool_calls_are_split_into_argument_fragments(self):
        events = self.stream(tools=TOOLS)
        merged = reassemble(events)
        self.assertEqual(len(merged["tool_calls"]), 2)
        self.assertEqual(merged["finish_reason"], "tool_calls")
        _, blocking = self.chat_json(
            {"model": "m", "messages": [{"role": "user", "content": "问题"}], "tools": TOOLS}
        )
        expected = blocking["choices"][0]["message"]["tool_calls"]
        self.assertEqual(
            [(c["id"], c["function"]["name"], c["function"]["arguments"]) for c in merged["tool_calls"]],
            [(c["id"], c["function"]["name"], c["function"]["arguments"]) for c in expected],
        )
        fragments = sum(
            1
            for chunk in events
            if chunk != "[DONE]" and delta_of(chunk).get("tool_calls")
        )
        self.assertGreater(fragments, 2)

    def test_empty_content_stream_has_no_content_fragments(self):
        self.state.set_scenario("empty_content")
        events = self.stream()
        self.assertEqual(reassemble(events)["content"], "")
        self.assertTrue(reassemble(events)["reasoning_content"])

    def test_include_usage_rides_on_the_final_finish_reason_chunk(self):
        """现行文档：usage 随最后一个带 finish_reason 的分片返回，没有独立的空 choices 块。"""
        events = [c for c in self.stream(stream_options={"include_usage": True}) if c != "[DONE]"]
        usage_chunks = [c for c in events if c.get("usage")]
        self.assertEqual(len(usage_chunks), 1)
        carrier = usage_chunks[0]
        self.assertIs(carrier, events[-1])  # 就是最后一个分片
        self.assertEqual(len(carrier["choices"]), 1)
        self.assertEqual(carrier["choices"][0]["finish_reason"], "stop")
        self.assertIn("completion_tokens", carrier["usage"])
        # 任何一个分片都不许是 choices 为空的 usage 块。
        for chunk in events:
            self.assertTrue(chunk["choices"], msg=json.dumps(chunk, ensure_ascii=False))

    def test_include_usage_also_rides_the_final_chunk_for_tool_calls(self):
        events = [
            c
            for c in self.stream(tools=TOOLS, stream_options={"include_usage": True})
            if c != "[DONE]"
        ]
        self.assertIn("usage", events[-1])
        self.assertEqual(events[-1]["choices"][0]["finish_reason"], "tool_calls")

    def test_no_usage_chunk_by_default(self):
        events = self.stream()
        self.assertFalse([c for c in events if c != "[DONE]" and c.get("usage")])


class TestFakeControl(FakeServerTestCase):
    def control(self, path, payload=None, method=None):
        status, _, raw = raw_request(
            self.origin + G.CONTROL_PREFIX + path, payload, api_key=None, method=method
        )
        return status, json.loads(raw.decode("utf-8"))

    def test_scenario_can_be_switched_over_http(self):
        status, body = self.control("/scenario", {"scenario": "http_429"})
        self.assertEqual(status, 200)
        self.assertEqual(body["scenario"], "http_429")
        self.assertEqual(self.state.snapshot()[0], "http_429")
        status, _ = self.ask()
        self.assertEqual(status, 429)

    def test_unknown_scenario_is_rejected(self):
        status, body = self.control("/scenario", {"scenario": "nope"})
        self.assertEqual(status, 400)
        self.assertEqual(sorted(body["known"]), sorted(G.SCENARIOS))

    def test_requests_can_be_listed_and_reset(self):
        self.ask()
        status, body = self.control("/requests")
        self.assertEqual(status, 200)
        self.assertEqual(body["count"], 1)
        self.assertEqual(body["chat_path"], "/ds-gw/chat/completions")
        self.assertEqual(body["marker"], self.state.marker)
        self.assertNotIn(API_KEY, json.dumps(body, ensure_ascii=False))
        self.assertIn("<redacted len=", body["requests"][0]["headers"]["Authorization"])
        status, _ = self.control("/reset", {}, method="POST")
        self.assertEqual(status, 200)
        self.assertEqual(self.control("/requests")[1]["count"], 0)

    def test_control_traffic_is_not_recorded_as_model_traffic(self):
        self.control("/requests")
        self.control("/scenario", {"scenario": "normal"})
        self.assertEqual(self.state.list_requests(), [])

    def test_unknown_control_endpoint_is_404(self):
        status, _ = self.control("/nope")
        self.assertEqual(status, 404)

    def test_reset_also_forgets_issued_reasoning(self):
        self.chat_json({"model": "m", "messages": [{"role": "user", "content": "x"}], "tools": TOOLS})
        self.assertTrue(self.state.issued_reasoning)
        self.control("/reset", {}, method="POST")
        self.assertFalse(self.state.issued_reasoning)


# ======================================================================================
# 代理
# ======================================================================================


class TestProxy(unittest.TestCase):
    def setUp(self):
        self.state = G.FakeLLMState(
            api_key=API_KEY, slow_delay=SLOW_DELAY, keepalives=KEEPALIVES, hang_cap=HANG_CAP
        )
        self.fake, self.fake_thread = G.start_fake_server(port=0, state=self.state)
        self.addCleanup(G.stop_server, self.fake, self.fake_thread)
        self.upstream = G.fake_base_url(self.fake)  # 上游带 /ds-gw，代理再套一层前缀
        self.tmp = tempfile.mkdtemp(prefix="llm_gateway_proxy_")
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def start_proxy(self, **kwargs):
        log_path = kwargs.pop("log_path", os.path.join(self.tmp, "traffic.jsonl"))
        server, thread = G.start_proxy_server(
            upstream=self.upstream, port=0, log_path=log_path, **kwargs
        )
        self.addCleanup(G.stop_proxy_server, server, thread)
        return G.proxy_base_url(server), log_path

    def read_log(self, path):
        with open(path, encoding="utf-8") as handle:
            return [json.loads(line) for line in handle if line.strip()]

    def test_printed_base_url_carries_the_prefix(self):
        base, _ = self.start_proxy()
        self.assertTrue(base.endswith("/ds-gw"))

    def test_non_stream_pass_through_is_byte_faithful(self):
        base, log_path = self.start_proxy()
        payload = {"model": "m", "messages": [{"role": "user", "content": "问题"}]}
        _, _, direct = raw_request(self.upstream + "/chat/completions", payload)
        status, _, through = raw_request(base + "/chat/completions", payload)
        self.assertEqual(status, 200)
        self.assertEqual(through, direct)
        entry = self.read_log(log_path)[0]
        self.assertEqual(entry["response_status"], 200)
        self.assertEqual(entry["request_body"], payload)
        self.assertEqual(entry["path"], "/ds-gw/chat/completions")
        self.assertIn("usage", entry)
        self.assertIsInstance(entry["latency_ms"], float)
        self.assertIsNone(entry["injected"])

    def test_stream_pass_through_is_byte_faithful_and_reassembled_in_the_log(self):
        base, log_path = self.start_proxy()
        payload = {
            "model": "m",
            "messages": [{"role": "user", "content": "问题"}],
            "stream": True,
            "tools": TOOLS,
        }
        _, _, direct = raw_request(self.upstream + "/chat/completions", payload)
        _, headers, through = raw_request(base + "/chat/completions", payload)
        self.assertEqual(through, direct)
        self.assertIn("text/event-stream", headers.get("Content-Type", ""))
        entry = self.read_log(log_path)[0]
        summary = entry["stream"]
        merged = reassemble(sse_events(direct))
        self.assertEqual(summary["reasoning_content"], merged["reasoning_content"])
        self.assertEqual(summary["content"], merged["content"])
        self.assertEqual(len(summary["tool_calls"]), 2)
        self.assertEqual(summary["finish_reason"], "tool_calls")
        self.assertGreater(summary["chunks"], 5)

    def test_keep_alive_comments_pass_through_untouched(self):
        self.state.set_scenario("slow")
        base, log_path = self.start_proxy()
        payload = {"model": "m", "messages": [{"role": "user", "content": "x"}], "stream": True}
        _, _, through = raw_request(base + "/chat/completions", payload)
        self.assertEqual(through.count(b": keep-alive\n\n"), through.count(b": keep-alive\n\n"))
        self.assertTrue(through.startswith(b": keep-alive\n\n"))
        self.assertGreaterEqual(self.read_log(log_path)[0]["stream"]["keepalive_comments"], KEEPALIVES)

    def test_blank_lines_before_a_non_stream_body_pass_through(self):
        self.state.set_scenario("slow")
        base, _ = self.start_proxy()
        _, _, through = raw_request(
            base + "/chat/completions", {"model": "m", "messages": [{"role": "user", "content": "x"}]}
        )
        self.assertTrue(through.startswith(b"\n" * KEEPALIVES))

    def test_time_to_first_content_is_recorded(self):
        base, log_path = self.start_proxy()
        raw_request(
            base + "/chat/completions",
            {"model": "m", "messages": [{"role": "user", "content": "问题"}], "stream": True},
        )
        summary = self.read_log(log_path)[0]["stream"]
        self.assertIsNotNone(summary["time_to_first_content_ms"])
        self.assertGreaterEqual(summary["time_to_first_content_ms"], 0)

    def test_authorization_is_forwarded_but_only_its_length_is_logged(self):
        base, log_path = self.start_proxy()
        status, _, _ = raw_request(
            base + "/chat/completions", {"model": "m", "messages": []}, api_key=API_KEY
        )
        self.assertEqual(status, 200)  # 上游认了这个 Key，说明确实转发了
        entry = self.read_log(log_path)[0]
        self.assertEqual(entry["authorization_length"], len("Bearer " + API_KEY))
        with open(log_path, encoding="utf-8") as handle:
            blob = handle.read()
        self.assertNotIn(API_KEY, blob)

    def test_a_wrong_key_still_reaches_upstream_and_comes_back_as_401(self):
        base, _ = self.start_proxy()
        status, _, raw = raw_request(
            base + "/chat/completions", {"model": "m", "messages": []}, api_key="wrong"
        )
        self.assertEqual(status, 401)
        self.assertEqual(json.loads(raw.decode("utf-8"))["error"]["code"], "authentication_error")

    def test_inject_thinking_only_when_the_request_does_not_set_it(self):
        base, log_path = self.start_proxy(inject_thinking="disabled")
        raw_request(base + "/chat/completions", {"model": "m", "messages": []})
        raw_request(
            base + "/chat/completions",
            {"model": "m", "messages": [], "thinking": {"type": "enabled"}},
        )
        entries = self.read_log(log_path)
        self.assertEqual(entries[0]["injected"], {"thinking": {"type": "disabled"}})
        self.assertIsNone(entries[1]["injected"])
        seen = [r["body"].get("thinking") for r in self.state.list_requests()]
        self.assertEqual(seen, [{"type": "disabled"}, {"type": "enabled"}])

    def test_injection_really_changes_the_upstream_response(self):
        base, _ = self.start_proxy(inject_thinking="disabled")
        _, _, raw = raw_request(base + "/chat/completions", {"model": "m", "messages": []})
        self.assertNotIn("reasoning_content", json.loads(raw.decode("utf-8"))["choices"][0]["message"])

    def test_no_injection_by_default(self):
        base, log_path = self.start_proxy()
        raw_request(base + "/chat/completions", {"model": "m", "messages": []})
        self.assertIsNone(self.read_log(log_path)[0]["injected"])
        self.assertNotIn("thinking", self.state.list_requests()[0]["body"])

    def test_paths_outside_the_prefix_are_refused_and_logged(self):
        base, log_path = self.start_proxy()
        origin = base[: -len("/ds-gw")]
        status, _, raw = raw_request(origin + "/chat/completions", {"model": "m"})
        self.assertEqual(status, 404)
        self.assertIn("/ds-gw", json.loads(raw.decode("utf-8"))["error"]["message"])
        self.assertEqual(self.read_log(log_path)[0]["response_status"], 404)
        self.assertEqual(self.state.list_requests(), [])

    def test_other_paths_under_the_prefix_are_forwarded(self):
        base, _ = self.start_proxy()
        status, _, _ = raw_request(base + "/models", {"a": 1})
        self.assertEqual(status, 404)  # 上游的 404，不是代理的
        self.assertEqual(self.state.list_requests()[0]["path"], "/ds-gw/models")

    def test_error_bodies_pass_through_untouched(self):
        self.state.set_scenario("http_503")
        base, log_path = self.start_proxy()
        _, _, direct = raw_request(self.upstream + "/chat/completions", {"model": "m", "messages": []})
        status, _, through = raw_request(base + "/chat/completions", {"model": "m", "messages": []})
        self.assertEqual(status, 503)
        self.assertEqual(through, direct)
        self.assertEqual(self.read_log(log_path)[0]["response_status"], 503)

    def test_dead_upstream_yields_502_and_is_logged(self):
        log_path = os.path.join(self.tmp, "dead.jsonl")
        server, thread = G.start_proxy_server(
            upstream="http://127.0.0.1:%d" % G.free_port(), port=0, log_path=log_path
        )
        self.addCleanup(G.stop_proxy_server, server, thread)
        base = G.proxy_base_url(server)
        status, _, _ = raw_request(base + "/chat/completions", {"model": "m"})
        self.assertEqual(status, 502)
        self.assertEqual(self.read_log(log_path)[0]["response_status"], 502)

    def test_logging_can_be_switched_off(self):
        server, thread = G.start_proxy_server(upstream=self.upstream, port=0, log_path=None)
        self.addCleanup(G.stop_proxy_server, server, thread)
        status, _, _ = raw_request(
            G.proxy_base_url(server) + "/chat/completions", {"model": "m", "messages": []}
        )
        self.assertEqual(status, 200)


# ======================================================================================
# 迷你服务：一个合规示例，外加一批各坏一处的变体
# ======================================================================================


class LLMError(Exception):
    pass


class MiniConfig:
    """迷你服务的配置。`defect` 为 None 时它是完全合规的。"""

    def __init__(
        self,
        defect=None,
        stream=False,
        llm_timeout=FAST_LLM_TIMEOUT,
        no_thinking=False,
        tools=None,
    ):
        self.defect = defect
        self.stream = stream
        # 契约 §7.3 允许关掉思考模式，这不是缺陷，是一个正当选择。
        self.no_thinking = no_thinking
        # 声明哪些工具。省略 parameters 的无参工具是官方允许的写法，同样不是缺陷。
        self.tools = TOOLS if tools is None else tools
        self.llm_timeout = llm_timeout
        self.base_url = None
        self.api_key = None
        self.model = None
        self.dead_url = None
        self.counter = 0


def mini_read_stream(response, cfg):
    """按契约 §7.3 解析 SSE：跳过空行和 `: keep-alive` 注释，先思考后正文。"""
    events = []
    while True:
        line = response.readline()
        if not line:
            break
        line = line.rstrip(b"\r\n")
        if not line:
            continue
        if line.startswith(b":"):
            if cfg.defect == "sse_comment_intolerant":
                raise LLMError("无法解析的 SSE 行：%r" % line[:20])
            continue
        if not line.startswith(b"data:"):
            continue
        payload = line[len(b"data:") :].strip()
        if payload == b"[DONE]":
            break
        events.append(json.loads(payload.decode("utf-8")))
    merged = reassemble(events)
    message = {"role": "assistant", "content": merged["content"]}
    if merged["reasoning_content"]:
        message["reasoning_content"] = merged["reasoning_content"]
    if merged["tool_calls"]:
        message["tool_calls"] = merged["tool_calls"]
    return {"message": message, "finish_reason": merged["finish_reason"]}


def mini_call_llm(cfg, messages, tools=None):
    """迷你服务调用大模型的唯一入口，严格按契约 §7.2 / §7.3 写。"""
    base = cfg.dead_url if cfg.defect == "ignore_base_url" else cfg.base_url
    payload = {
        "model": cfg.model,
        "messages": messages,
        "max_tokens": 4096,
        "temperature": 0.2,
    }
    if cfg.stream:
        payload["stream"] = True
    if cfg.no_thinking:
        payload["thinking"] = {"type": "disabled"}
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = "auto"
    if cfg.defect == "hardcoded_model":
        payload["model"] = "deepseek-chat"  # 缺陷：写死模型名
    if cfg.defect == "extra_param":
        payload["seed"] = 42  # 缺陷：文档里没有的参数
    if cfg.defect == "small_max_tokens":
        payload["max_tokens"] = 256  # 缺陷：额度太小，思考会把它吃光

    headers = {"Content-Type": "application/json; charset=utf-8"}
    if cfg.defect != "no_auth":
        headers["Authorization"] = "Bearer " + (cfg.api_key or "")

    if cfg.defect == "probe_models":
        # 缺陷：启动/每次调用前先列一次模型，规范明确禁止。
        try:
            with urllib.request.urlopen(base + "/models", timeout=cfg.llm_timeout) as probe:
                probe.read()
        except urllib.error.HTTPError as exc:
            exc.close()
        except Exception:
            pass

    request = urllib.request.Request(
        base + "/chat/completions",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        response = urllib.request.urlopen(request, timeout=cfg.llm_timeout)
    except urllib.error.HTTPError as exc:
        exc.close()
        raise LLMError("模型返回 HTTP %d" % exc.code) from exc
    except Exception as exc:
        raise LLMError("模型调用失败：%s" % type(exc).__name__) from exc
    try:
        if cfg.stream:
            return mini_read_stream(response, cfg)
        raw = response.read()
    finally:
        response.close()
    if cfg.defect == "strict_body_prefix" and not raw.startswith(b"{"):
        # 缺陷：自己解析 HTTP 正文，却不肯跳过保持连接的空行。
        raise LLMError("模型响应不是以 { 开头")
    try:
        return json.loads(raw.decode("utf-8"))["choices"][0]
    except Exception as exc:
        raise LLMError("模型响应无法解析：%s" % type(exc).__name__) from exc


MINI_TOOL_RESULTS = {
    "query_metrics": {"net_revenue": 27431.0, "qty": 118},
    "search_docs": {"doc_id": "KB-013", "quote": "外卖订单在送达后 30 分钟内可以申请退款"},
    "ping": {"pong": True},
    "healthcheck": {"ok": True},
}

BAD_FINISH_REASONS = ("length", "content_filter", "insufficient_system_resource", "aborted")


def mini_chat(cfg, question):
    """合规的问答链路：多轮工具调用 → 本地执行 → 回传结果 → 由代码渲染数字。"""
    cfg.counter += 1
    trace_id = "t-20260901-%04d" % cfg.counter

    def refusal(reason):
        answer = "暂时无法回答：%s。原因已记录在 trace %s 里。" % (reason, trace_id)
        if cfg.defect == "empty_answer":
            answer = ""  # 缺陷：拒答时把 answer 写成空串
        return {
            "answer": answer,
            "answer_type": "refusal",
            "citations": [],
            "data_evidence": [],
            "trace_id": trace_id,
        }

    messages = [
        {"role": "system", "content": "你是门店运营助手，回答里的数字必须来自工具结果。"},
        {"role": "user", "content": question},
    ]
    executed = []
    last_message = None

    for _round in range(3):
        try:
            choice = mini_call_llm(cfg, messages, tools=cfg.tools)
        except LLMError as exc:
            return refusal(str(exc))
        message = choice.get("message") or {}
        finish_reason = choice.get("finish_reason")
        last_message = message
        tool_calls = message.get("tool_calls")

        if cfg.defect == "malicious_passthrough" and message.get("content"):
            # 缺陷（Codex 反驳里的恶意客户端）：完全不看 finish_reason，
            # 把模型吐出来的任何非空正文当成成功回答，再配一条从没执行过的证据。
            return {
                "answer": message["content"],
                "answer_type": "data",
                "citations": [],
                "data_evidence": [{"sql": "SELECT 0 /* never run */", "result": 0}],
                "trace_id": "nonexistent",
            }

        if isinstance(tool_calls, list) and tool_calls:
            # 契约 §7.3：带 tools 的多轮请求，assistant 消息要整条原样追加。
            if cfg.defect == "drop_reasoning":
                # 缺陷：自己挑字段重组，把 reasoning_content 丢了。
                messages.append(
                    {
                        "role": "assistant",
                        "content": message.get("content") or "",
                        "tool_calls": tool_calls,
                    }
                )
            else:
                messages.append(message)
            for call in tool_calls:
                try:
                    args = json.loads(call["function"]["arguments"])
                except Exception:
                    return refusal("模型返回的工具参数不是合法 JSON")
                name = call["function"]["name"]
                result = MINI_TOOL_RESULTS.get(name, {})
                executed.append({"tool": name, "params": args, "result": result})
                if cfg.defect == "wrong_tool_role":
                    # 缺陷：结果没有以 role=tool + tool_call_id 回传。
                    messages.append(
                        {"role": "user", "content": "工具结果：" + json.dumps(result, ensure_ascii=False)}
                    )
                else:
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": call.get("id"),
                            "content": json.dumps(result, ensure_ascii=False),
                        }
                    )
            continue

        if finish_reason in BAD_FINISH_REASONS:
            return refusal("模型以 finish_reason=%s 结束" % finish_reason)
        content = message.get("content") or ""
        if not content.strip():
            return refusal("模型返回了空回答")
        break
    else:
        return refusal("工具调用轮次超过上限")

    # 数字全部由代码从工具结果渲染，不让模型转述。
    metrics = next((e for e in executed if e["tool"] == "query_metrics"), None)
    docs = next((e for e in executed if e["tool"] == "search_docs"), None)
    parts = []
    if metrics:
        parts.append(
            "净营业额 %.2f 元，销量 %d 份。"
            % (metrics["result"]["net_revenue"], metrics["result"]["qty"])
        )
    if docs:
        parts.append("规则原文：%s。" % docs["result"]["quote"])
    if not parts and executed:
        # 无参工具（例如 ping）只能证明链路通，数字还是由代码渲染，这里没有数字可渲染。
        parts.append("已成功调用 %d 次工具，链路正常。" % len(executed))
    answer = "".join(parts) or "已根据知识库作答。"
    if cfg.defect == "leak_reasoning":
        # 缺陷：把思考过程拼进了给用户看的回答。
        answer = (last_message.get("reasoning_content") or "") + answer

    if metrics and docs:
        answer_type = "hybrid"
    elif docs and not metrics:
        answer_type = "doc"
    elif executed:
        answer_type = "data"
    else:
        answer_type = "doc"
    citations = (
        [{"doc_id": docs["result"]["doc_id"], "quote": docs["result"]["quote"]}] if docs else []
    )
    return {
        "answer": answer,
        "answer_type": answer_type,
        "citations": citations,
        "data_evidence": executed,
        "trace_id": trace_id,
    }


class MiniHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # noqa: A003
        pass

    def _send(self, status, obj):
        payload = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):  # noqa: N802
        cfg = self.server.cfg
        if urllib.parse.urlparse(self.path).path == "/api/health":
            mode = "live" if cfg.api_key else "mock"
            if cfg.defect == "mock_health":
                mode = "mock"  # 缺陷：明明注入了环境变量却报 mock
            self._send(
                200,
                {
                    "status": "ok",
                    "llm_mode": mode,
                    "kb_docs": 35,
                    "kb_chunks": 412,
                    "valid_sales_rows": 17890,
                },
            )
            return
        self._send(404, {"error": "not found"})

    def do_POST(self):  # noqa: N802
        cfg = self.server.cfg
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        if urllib.parse.urlparse(self.path).path != "/api/chat":
            self._send(404, {"error": "not found"})
            return
        try:
            body = json.loads(raw.decode("utf-8"))
        except Exception:
            body = {}
        result = mini_chat(cfg, body.get("question") or "")
        if cfg.defect == "http_500" and result["answer_type"] == "refusal":
            # 缺陷：内部错误直接 500，违反“任何情况下都返回 200 + 合法 JSON”。
            self._send(500, {"detail": "internal error"})
            return
        self._send(200, result)


class MiniServer(G.OfflineThreadingHTTPServer):
    def __init__(self, address, cfg):
        self.cfg = cfg
        super().__init__(address, MiniHandler)


def start_mini_service(cfg):
    server = MiniServer(("127.0.0.1", 0), cfg)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    thread.start()
    return server, thread


def stop_mini_service(server, thread):
    server.shutdown()
    server.server_close()
    thread.join(timeout=5.0)


class PreflightRun:
    """一次完整预检的产物：结果、打印出来的文字、报告目录。"""

    def __init__(self, result, out, out_dir):
        self.result = result
        self.out = out
        self.out_dir = out_dir


def drive_preflight(
    defect=None, stream=False, cleanup=None, no_thinking=False, tools=None, **kwargs
):
    """起一个迷你服务，跑一遍预检，返回 PreflightRun。`cleanup` 用来登记收尾动作。"""
    llm_timeout = SLOW_LLM_TIMEOUT if defect == "slow_llm" else FAST_LLM_TIMEOUT
    cfg = MiniConfig(
        defect=defect,
        stream=stream,
        llm_timeout=llm_timeout,
        no_thinking=no_thinking,
        tools=tools,
    )
    cfg.dead_url = "http://127.0.0.1:%d/ds-gw" % G.free_port()
    server, thread = start_mini_service(cfg)
    cleanup(stop_mini_service, server, thread)
    out_dir = tempfile.mkdtemp(prefix="llm_gateway_preflight_")
    cleanup(shutil.rmtree, out_dir, True)
    out = io.StringIO()

    def ready(env):
        cfg.base_url = env["LLM_BASE_URL"]
        cfg.api_key = env["LLM_API_KEY"]
        cfg.model = env["LLM_MODEL"]

    params = dict(
        service_url=G.server_origin(server),
        no_wait=True,
        ready_hook=ready,
        out_dir=out_dir,
        out_stream=out,
        slow_delay=SLOW_DELAY,
        keepalives=KEEPALIVES,
        hang_cap=HANG_CAP,
        max_chat_seconds=MAX_CHAT_SECONDS,
        chat_timeout=CHAT_TIMEOUT,
        questions=PREFLIGHT_QUESTIONS,
    )
    params.update(kwargs)
    return PreflightRun(G.run_preflight(**params), out, out_dir)


def summarise(result):
    return "\n".join("%s %s: %s" % (c.id, c.status, c.reason) for c in result.checks)


class PreflightTestCase(unittest.TestCase):
    def drive(self, defect=None, **kwargs):
        run = drive_preflight(defect=defect, cleanup=self.addCleanup, **kwargs)
        self.result = run.result
        self.out = run.out
        self.out_dir = run.out_dir
        return run.result

    def assert_exactly(self, result, red, skipped=()):
        actual_red = sorted((c.id for c in result.checks if c.status == G.FAIL), key=_pnum)
        actual_skip = sorted((c.id for c in result.checks if c.status == G.SKIP), key=_pnum)
        message = "\n" + summarise(result)
        self.assertEqual(actual_red, sorted(red, key=_pnum), msg=message)
        self.assertEqual(actual_skip, sorted(skipped, key=_pnum), msg=message)


def _pnum(check_id):
    return int(check_id[1:])


class TestPreflightCompliant(PreflightTestCase):
    """一个完全合规的迷你服务，14 项检查一项不红。"""

    @classmethod
    def setUpClass(cls):
        cls.run_blocking = drive_preflight(cleanup=cls.addClassCleanup)
        cls.run_streaming = drive_preflight(stream=True, cleanup=cls.addClassCleanup)

    def test_every_check_is_green(self):
        result = self.run_blocking.result
        for check in result.checks:
            self.assertEqual(check.status, G.PASS, msg="%s：%s" % (check.id, check.reason))
            self.assertTrue(check.reason.strip())
        self.assertTrue(result.passed)

    def test_there_are_exactly_fourteen_checks_in_order(self):
        result = self.run_blocking.result
        self.assertEqual([c.id for c in result.checks], list(G.CHECK_IDS))
        self.assertEqual(len(result.checks), 14)
        self.assertEqual(set(G.CHECK_TITLES), set(G.CHECK_IDS))

    def test_a_streaming_service_also_passes_every_check(self):
        result = self.run_streaming.result
        for check in result.checks:
            self.assertEqual(check.status, G.PASS, msg="%s：%s" % (check.id, check.reason))

    def test_every_scenario_was_exercised_against_the_fake(self):
        result = self.run_blocking.result
        self.assertEqual([r.scenario for r in result.scenario_runs], list(G.SCENARIOS))
        for run in result.scenario_runs:
            self.assertTrue(run.attempts)
            for attempt in run.attempts:
                self.assertEqual(attempt.result.status, 200)
            self.assertTrue(
                G._chat_requests(run.llm_requests, "/ds-gw/chat/completions"),
                msg="场景 %s 没有观察到模型请求" % run.scenario,
            )

    def test_failure_scenarios_end_in_a_refusal_and_good_ones_do_not(self):
        result = self.run_blocking.result
        by_name = {r.scenario: r for r in result.scenario_runs}
        for scenario in G.ALWAYS_FAILING_SCENARIOS:
            types = {
                (a.result.body or {}).get("answer_type") for a in by_name[scenario].attempts
            }
            self.assertEqual(types, {"refusal"}, msg=scenario)
        for scenario in ("normal", "slow", "thinking_starved", "json_empty"):
            self.assertTrue(by_name[scenario].answered, msg=scenario)

    def test_aborted_partial_content_never_becomes_the_answer(self):
        """aborted 的正文非空，合规实现必须靠 finish_reason 判错，而不是靠“正文空不空”。"""
        result = self.run_blocking.result
        run = next(r for r in result.scenario_runs if r.scenario == "aborted")
        self.assertTrue(run.attempts)
        for attempt in run.attempts:
            body = attempt.result.body
            self.assertEqual(body["answer_type"], "refusal")
            self.assertNotIn(G.FAKE_ABORTED_TEXT, body["answer"])
            self.assertTrue(body["answer"].strip())

    def test_two_tool_rounds_really_happened(self):
        result = self.run_blocking.result
        normal = next(r for r in result.scenario_runs if r.scenario == "normal")
        chat = G._chat_requests(normal.llm_requests, "/ds-gw/chat/completions")
        self.assertEqual(len(chat), 3 * len(PREFLIGHT_QUESTIONS))
        announced = result.check("P7").evidence["announced_tool_calls"]
        self.assertGreaterEqual(announced, 3)
        self.assertEqual(announced, result.check("P7").evidence["returned_tool_results"])

    def test_reports_are_written_and_complete(self):
        result = self.run_blocking.result
        self.assertTrue(os.path.isfile(result.report_md_path))
        with open(result.report_md_path, encoding="utf-8") as handle:
            markdown = handle.read()
        self.assertIn("大模型接入预检报告", markdown)
        for check_id in G.CHECK_IDS:
            self.assertIn(check_id, markdown)
        for scenario in G.SCENARIOS:
            self.assertIn(scenario, markdown)
        self.assertNotIn(result.marker, markdown.split("## 逐项证据")[0])
        with open(result.report_json_path, encoding="utf-8") as handle:
            payload = json.load(handle)
        self.assertTrue(payload["passed"])
        self.assertEqual(len(payload["checks"]), 14)
        self.assertEqual(payload["failed_checks"], [])
        self.assertEqual(payload["skipped_checks"], [])
        self.assertEqual(len(payload["scenarios"]), len(G.SCENARIOS))
        self.assertNotIn("__chat_path", payload["env"])

    def test_report_markdown_puts_one_sentence_per_line(self):
        with open(self.run_blocking.result.report_md_path, encoding="utf-8") as handle:
            body = handle.read()
        prose = [
            line
            for line in body.splitlines()
            if line and not line.startswith(("#", "|", "`", "-", " ", "{", "}", '"', "["))
        ]
        self.assertTrue(prose)
        for line in prose:
            self.assertLessEqual(line.count("。"), 1, msg=line)

    def test_the_printed_table_tells_you_what_to_export(self):
        text = self.run_blocking.out.getvalue()
        self.assertIn("export LLM_BASE_URL=", text)
        self.assertIn("export LLM_API_KEY=", text)
        self.assertIn("export LLM_MODEL=", text)
        self.assertIn("/ds-gw", text)
        for check_id in G.CHECK_IDS:
            self.assertIn(check_id, text)
        self.assertIn("预检通过", text)

    def test_the_marker_is_unique_per_run(self):
        self.assertNotEqual(self.run_blocking.result.marker, self.run_streaming.result.marker)


class TestPreflightLegitimateChoices(PreflightTestCase):
    """规范明确允许的做法，不能被判成失败。"""

    def test_disabling_thinking_leaves_both_reasoning_checks_unchecked(self):
        """契约 §7.3：关闭思考之后，与 reasoning_content 有关的两项显示“未检查”而不是“通过”。"""
        result = self.drive(no_thinking=True)
        self.assert_exactly(result, red=[], skipped=["P10", "P13"])
        for check_id in ("P10", "P13"):
            reason = result.check(check_id).reason
            self.assertIn("思考", reason)
            self.assertNotIn("没有出现在任何对外字段里", reason)
        self.assertIn("不扣分", result.check("P10").reason)
        self.assertTrue(result.passed)  # 未检查不算失败，退出码仍然是 0
        self.assertEqual(
            result.check("P10").evidence["responses_carrying_the_marker"], 0
        )

    def test_a_parameterless_tool_declaration_passes_every_check(self):
        """官方文档允许省略 `parameters`；预检不能因为这种写法就判红（Codex 反驳的错误拒绝）。"""
        result = self.drive(tools=[TOOL_PARAMETERLESS])
        self.assert_exactly(result, red=[])
        self.assertEqual(result.status_of("P7"), G.PASS)
        self.assertTrue(result.passed)
        normal = next(r for r in result.scenario_runs if r.scenario == "normal")
        self.assertTrue(normal.answered)
        # 假模型确实用空参数调了它，没有凭空编出没声明过的字段。
        chat = G._chat_requests(normal.llm_requests, "/ds-gw/chat/completions")
        echoed = [
            call
            for request in chat
            for message in request["body"]["messages"]
            if isinstance(message, dict) and message.get("role") == "assistant"
            for call in (message.get("tool_calls") or [])
        ]
        self.assertTrue(echoed)
        for call in echoed:
            self.assertEqual(call["function"]["name"], "ping")
            self.assertEqual(json.loads(call["function"]["arguments"]), {})

    def test_a_bare_object_schema_also_passes_every_check(self):
        self.assert_exactly(self.drive(tools=[TOOL_BARE_OBJECT]), red=[])

    def test_disabling_thinking_really_removed_the_field(self):
        result = self.drive(no_thinking=True)
        normal = next(r for r in result.scenario_runs if r.scenario == "normal")
        chat = G._chat_requests(normal.llm_requests, "/ds-gw/chat/completions")
        self.assertTrue(chat)
        for request in chat:
            self.assertEqual(request["body"]["thinking"], {"type": "disabled"})
        # 关掉思考之后服务照样能答出来，其余 12 项照常通过。
        self.assertTrue(normal.answered)


class TestPreflightVariants(PreflightTestCase):
    """每个变体只坏一处，断言恰好对应的那一项变红。"""

    def test_p1_service_ignores_the_injected_base_url(self):
        result = self.drive(defect="ignore_base_url")
        # 一次请求都没收到：P2–P7、P10、P13 没有素材，P14 没有可比的基准。
        self.assert_exactly(
            result,
            red=["P1"],
            skipped=["P2", "P3", "P4", "P5", "P6", "P7", "P10", "P13", "P14"],
        )
        self.assertEqual(result.check("P1").evidence["chat_completions_requests"], 0)
        # 服务本身活着，所以耗时是有样本的，P11 仍然是实打实的通过。
        self.assertEqual(result.status_of("P11"), G.PASS)
        self.assertGreater(result.check("P11").evidence["timed_attempts"], 0)

    def test_p2_hardcoded_model_name(self):
        result = self.drive(defect="hardcoded_model")
        self.assert_exactly(result, red=["P2"])
        self.assertIn("deepseek-chat", result.check("P2").evidence["seen"])

    def test_p3_missing_authorization_header(self):
        result = self.drive(defect="no_auth")
        # 每次调用都被 401 挡下：假模型没发出过思考内容（P10），走不到多轮工具调用（P13），
        # normal 也没答出来，P14 没有可比的基准。
        self.assert_exactly(result, red=["P3"], skipped=["P10", "P13", "P14"])
        self.assertTrue(result.check("P3").evidence["missing_authorization_in"])
        self.assertEqual(result.check("P10").evidence["responses_carrying_the_marker"], 0)

    def test_p4_uses_a_parameter_the_docs_do_not_have(self):
        result = self.drive(defect="extra_param")
        self.assert_exactly(result, red=["P4"])
        self.assertIn("seed", result.check("P4").evidence["extra_params"])

    def test_p5_max_tokens_too_small(self):
        result = self.drive(defect="small_max_tokens")
        self.assert_exactly(result, red=["P5"])
        self.assertEqual(result.check("P5").evidence["offenders"][0]["max_tokens"], 256)
        # 额度太小时 thinking_starved 才会咬人，它也确实被算成了失败场景。
        self.assertIn("thinking_starved", result.check("P9").evidence["failing_scenarios"])

    def test_p6_lists_models_before_calling(self):
        result = self.drive(defect="probe_models")
        self.assert_exactly(result, red=["P6"])
        self.assertIn(
            "/ds-gw/models", {item["path"] for item in result.check("P6").evidence["other_paths"]}
        )

    def test_a_failing_report_still_puts_one_sentence_per_line(self):
        """失败时的说明常常是两三句话，写进报告正文时也得一句一行。"""
        result = self.drive(defect="probe_models")
        reason = result.check("P6").reason
        self.assertGreater(reason.count("。"), 1)  # 前提：这条说明确实不止一句
        with open(result.report_md_path, encoding="utf-8") as handle:
            markdown = handle.read()
        prose = [
            line
            for line in markdown.splitlines()
            if line and not line.startswith(("#", "|", "`", "-", " ", "{", "}", '"', "["))
        ]
        for line in prose:
            self.assertLessEqual(line.count("。"), 1, msg=line)
        # 拆开之后一个字都不能丢。
        for sentence in G.split_sentences(reason):
            self.assertIn(sentence, markdown)
        self.assertEqual("".join(G.split_sentences(reason)), reason)

    def test_the_p6_message_is_scoped_to_the_openai_compatible_route(self):
        result = self.drive(defect="probe_models")
        reason = result.check("P6").reason
        self.assertIn("OpenAI 兼容", reason)
        self.assertIn("LLM_SETUP.md", reason)
        self.assertIn("本身不算错", reason)

    def test_p7_tool_results_sent_with_the_wrong_role(self):
        result = self.drive(defect="wrong_tool_role")
        self.assert_exactly(result, red=["P7"])
        self.assertTrue(result.check("P7").evidence["problems"])
        self.assertEqual(result.check("P7").evidence["returned_tool_results"], 0)

    def test_p8_returns_http_500_when_the_model_fails(self):
        result = self.drive(defect="http_500")
        # 模型不可用的场景全都变成了 500，P9 一次合法响应都没拿到，只能是未检查。
        self.assert_exactly(result, red=["P8"], skipped=["P9"])
        self.assertTrue(
            any("HTTP 500" in f["problem"] for f in result.check("P8").evidence["failures"])
        )
        self.assertEqual(result.check("P9").evidence["checked"], 0)

    def test_p9_empty_answer_on_refusal(self):
        result = self.drive(defect="empty_answer")
        self.assert_exactly(result, red=["P9"])
        self.assertTrue(
            any("空串" in p["problem"] for p in result.check("P9").evidence["problems"])
        )

    def test_p9_malicious_passthrough_with_fabricated_evidence(self):
        """Codex 反驳里的恶意客户端：无视 finish_reason，把半截正文当成 data 回答，配一条假证据。"""
        result = self.drive(defect="malicious_passthrough")
        self.assert_exactly(result, red=["P9"])
        problems = result.check("P9").evidence["problems"]
        scenarios = {p["scenario"] for p in problems}
        # aborted 与 content_filter 这两个“非空正文”的失败场景都必须被抓到。
        self.assertIn("aborted", scenarios)
        self.assertIn("content_filter", scenarios)
        joined = json.dumps(problems, ensure_ascii=False)
        self.assertIn("失败的模型输出", joined)
        self.assertIn("现编的", joined)
        self.assertIn("SELECT 0", joined)

    def test_p9_fabricated_sql_alone_is_enough_to_go_red(self):
        """就算不转发模型输出，只靠一条常量 SQL 充当证据，也算不上“有据可查”。"""
        chat_path = "/ds-gw/chat/completions"
        run = G.ScenarioRun(
            scenario="http_500",
            attempts=[
                G.ChatAttempt(
                    scenario="http_500",
                    question="q",
                    session_id="s",
                    result=G.HttpResult(
                        url="u",
                        status=200,
                        body={
                            "answer": "净营业额 0.00 元。",
                            "answer_type": "data",
                            "citations": [],
                            "data_evidence": [{"sql": "SELECT 0 /* FROM orders */", "result": 0}],
                            "trace_id": "t-1",
                        },
                        body_text="",
                        elapsed=0.1,
                        error=None,
                    ),
                )
            ],
            llm_requests=[{"path": chat_path, "method": "POST", "body": {}}],
        )
        check = G.check_p9([run], {"http_500"}, "FAILOUT-deadbeef", {"query_metrics"})
        self.assertEqual(check.status, G.FAIL)
        self.assertIn("现编的", check.reason)
        # 换成一条真的从表里取数的 SQL 就放行。
        run.attempts[0].result.body["data_evidence"] = [
            {"sql": "SELECT SUM(net) FROM orders WHERE day = '2026-06-18'", "result": 27431.0}
        ]
        self.assertEqual(G.check_p9([run], {"http_500"}, "FAILOUT-deadbeef", {"q"}).status, G.PASS)

    def test_p9_tool_name_never_declared_counts_as_fabricated(self):
        declared = {"query_metrics"}
        self.assertTrue(G.evidence_is_grounded({"tool": "query_metrics", "result": 1}, declared))
        self.assertFalse(G.evidence_is_grounded({"tool": "make_it_up", "result": 1}, declared))
        # 服务压根没声明过工具时无从对照，放过，不做假阳性。
        self.assertTrue(G.evidence_is_grounded({"tool": "make_it_up", "result": 1}, set()))
        self.assertFalse(G.evidence_is_grounded({"tool": "query_metrics"}, declared))
        self.assertFalse(G.evidence_is_grounded({"sql": "SELECT 0", "result": 0}, declared))
        self.assertFalse(G.evidence_is_grounded({"sql": "SELECT 0 -- FROM x", "result": 0}, declared))
        self.assertTrue(
            G.evidence_is_grounded({"sql": "select qty from sales", "result": 1}, declared)
        )
        self.assertFalse(G.evidence_is_grounded("not a dict", declared))

    def test_p10_leaks_reasoning_into_the_answer(self):
        result = self.drive(defect="leak_reasoning")
        self.assert_exactly(result, red=["P10"])
        leaks = result.check("P10").evidence["leaks"]
        self.assertTrue(leaks)
        self.assertEqual({leak["field"] for leak in leaks}, {"answer"})

    def test_p11_takes_too_long_when_the_model_hangs(self):
        result = self.drive(defect="slow_llm")
        self.assert_exactly(result, red=["P11"])
        self.assertEqual(
            {item["scenario"] for item in result.check("P11").evidence["offenders"]}, {"hang"}
        )

    def test_p12_health_still_reports_mock(self):
        result = self.drive(defect="mock_health")
        self.assert_exactly(result, red=["P12"])
        self.assertEqual(result.check("P12").evidence["body"]["llm_mode"], "mock")

    def test_p13_rebuilds_the_assistant_message_without_reasoning(self):
        result = self.drive(defect="drop_reasoning")
        # 第二轮被 400 挡下，连 normal 都答不出来，所以 P14 没有可比的基准。
        self.assert_exactly(result, red=["P13"], skipped=["P14"])
        self.assertTrue(result.check("P13").evidence["requests_rejected_with_400"])

    def test_p14_blank_lines_before_the_body_break_the_client(self):
        result = self.drive(defect="strict_body_prefix")
        self.assert_exactly(result, red=["P14"])
        self.assertTrue(result.check("P14").evidence["normal_answered"])
        self.assertFalse(result.check("P14").evidence["slow_answered"])

    def test_p14_sse_comments_break_the_streaming_client(self):
        result = self.drive(defect="sse_comment_intolerant", stream=True)
        self.assert_exactly(result, red=["P14"])
        self.assertTrue(result.check("P14").evidence["normal_answered"])

    def test_every_check_has_a_variant_that_turns_it_red(self):
        """守卫：14 项检查都必须在上面有一个专门让它变红的用例。"""
        covered = set()
        for name in dir(self):
            if not name.startswith("test_p") or len(name) < 7 or not name[6].isdigit():
                continue
            digits = ""
            for ch in name[6:]:
                if not ch.isdigit():
                    break
                digits += ch
            covered.add("P" + digits)
        self.assertEqual(covered, set(G.CHECK_IDS))


class TestPreflightRobustness(PreflightTestCase):
    def drive_dead_service(self, **kwargs):
        dead = "http://127.0.0.1:%d" % G.free_port()
        out_dir = tempfile.mkdtemp(prefix="llm_gateway_dead_")
        self.addCleanup(shutil.rmtree, out_dir, True)
        params = dict(
            service_url=dead,
            no_wait=True,
            out_dir=out_dir,
            out_stream=io.StringIO(),
            slow_delay=SLOW_DELAY,
            keepalives=KEEPALIVES,
            hang_cap=HANG_CAP,
            max_chat_seconds=MAX_CHAT_SECONDS,
            chat_timeout=2.0,
            health_timeout=2.0,
            questions=PREFLIGHT_QUESTIONS,
            scenarios=["normal", "slow"],
        )
        params.update(kwargs)
        return G.run_preflight(**params)

    def test_a_service_that_is_not_running_produces_failures_not_a_crash(self):
        result = self.drive_dead_service()
        self.assertFalse(result.passed)
        self.assertIn("P1", [c.id for c in result.failed])
        self.assertIn("P8", [c.id for c in result.failed])
        self.assertIn("P12", [c.id for c in result.failed])
        self.assertTrue(os.path.isfile(result.report_md_path))

    def test_an_unreachable_service_is_never_accused_of_a_timeout(self):
        """连不上不等于超时。

        以前 P11 会把“连接被拒绝、耗时 0.00 秒”的问答算成“超过了 180 秒”，那是假话。
        """
        result = self.drive_dead_service()
        check = result.check("P11")
        self.assertEqual(check.status, G.SKIP, msg=check.reason)
        self.assertEqual(check.evidence["timed_attempts"], 0)
        self.assertEqual(check.evidence["offenders"], [])
        self.assertTrue(check.evidence["unreachable_attempts"])
        for claim in ("超过了", "秒以内", "最慢"):
            self.assertNotIn(claim, check.reason, msg=check.reason)
        self.assertIn("P8", check.reason)
        # 报告里也不能留下这句假话。
        with open(result.report_md_path, encoding="utf-8") as handle:
            markdown = handle.read()
        self.assertNotIn("次问答超过了", markdown)

    def test_an_unreachable_service_makes_no_vacuous_claims_anywhere(self):
        """一次有效响应都没拿到时，凭响应下结论的检查都必须是未检查，而不是通过。"""
        result = self.drive_dead_service()
        for check_id in ("P2", "P3", "P4", "P5", "P6", "P7", "P9", "P10", "P11", "P13", "P14"):
            with self.subTest(check=check_id):
                self.assertEqual(
                    result.status_of(check_id), G.SKIP, msg=result.check(check_id).reason
                )
        # 只有真正观察到了事实的三项才允许失败。
        self.assertEqual({c.id for c in result.failed}, {"P1", "P8", "P12"})
        self.assertNotIn("只访问了", result.check("P6").reason)
        self.assertNotIn("从不是空串", result.check("P9").reason)
        self.assertNotIn("没有出现在任何对外字段里", result.check("P10").reason)

    def test_a_service_that_never_answers_does_fail_p11(self):
        """反过来：服务活着但一直不回，那确实是超时，P11 必须红。"""
        release = threading.Event()
        self.addCleanup(release.set)

        class BlackHole(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):  # noqa: A003
                pass

            def _stall(self):
                release.wait(20.0)
                self.close_connection = True

            do_GET = _stall
            do_POST = _stall

        server = G.OfflineThreadingHTTPServer(("127.0.0.1", 0), BlackHole)
        thread = threading.Thread(
            target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
        )
        thread.start()
        self.addCleanup(stop_mini_service, server, thread)
        out_dir = tempfile.mkdtemp(prefix="llm_gateway_blackhole_")
        self.addCleanup(shutil.rmtree, out_dir, True)
        result = G.run_preflight(
            service_url=G.server_origin(server),
            no_wait=True,
            out_dir=out_dir,
            out_stream=io.StringIO(),
            slow_delay=SLOW_DELAY,
            keepalives=KEEPALIVES,
            hang_cap=HANG_CAP,
            max_chat_seconds=0.4,
            chat_timeout=1.0,
            health_timeout=1.0,
            questions=PREFLIGHT_QUESTIONS,
            scenarios=["normal"],
        )
        check = result.check("P11")
        self.assertEqual(check.status, G.FAIL, msg=check.reason)
        self.assertTrue(all(o["cut_off_by_preflight"] for o in check.evidence["offenders"]))
        self.assertIn("总预算", check.reason)
        self.assertIn("没等到响应", check.reason)

    def test_a_service_that_returns_garbage_is_reported_not_fatal(self):
        class Garbage(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):  # noqa: A003
                pass

            def _blob(self):
                payload = b"not json at all"
                self.send_response(200)
                self.send_header("Content-Type", "text/plain")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            do_GET = _blob
            do_POST = _blob

        server = G.OfflineThreadingHTTPServer(("127.0.0.1", 0), Garbage)
        server.daemon_threads = True
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        thread.start()
        self.addCleanup(stop_mini_service, server, thread)
        out_dir = tempfile.mkdtemp(prefix="llm_gateway_garbage_")
        self.addCleanup(shutil.rmtree, out_dir, True)
        result = G.run_preflight(
            service_url="http://127.0.0.1:%d" % server.server_address[1],
            no_wait=True,
            out_dir=out_dir,
            out_stream=io.StringIO(),
            slow_delay=SLOW_DELAY,
            keepalives=KEEPALIVES,
            hang_cap=HANG_CAP,
            max_chat_seconds=MAX_CHAT_SECONDS,
            chat_timeout=2.0,
            health_timeout=2.0,
            questions=PREFLIGHT_QUESTIONS,
            scenarios=["normal"],
        )
        self.assertFalse(result.passed)
        self.assertEqual(result.status_of("P8"), G.FAIL)
        self.assertEqual(result.status_of("P12"), G.FAIL)

    def test_an_unknown_scenario_is_refused_up_front(self):
        with self.assertRaises(ValueError):
            G.run_preflight(
                service_url="http://127.0.0.1:1",
                no_wait=True,
                write_reports=False,
                out_stream=io.StringIO(),
                scenarios=["nope"],
            )


# ======================================================================================
# 单元测试与命令行
# ======================================================================================


class TestUnits(unittest.TestCase):
    def test_documented_params_match_the_deepseek_doc_list(self):
        self.assertEqual(
            G.DOCUMENTED_PARAMS,
            frozenset(
                {
                    "model",
                    "messages",
                    "thinking",
                    "reasoning_effort",
                    "max_tokens",
                    "response_format",
                    "stop",
                    "stream",
                    "stream_options",
                    "temperature",
                    "top_p",
                    "presence_penalty",
                    "frequency_penalty",
                    "tools",
                    "tool_choice",
                    "logprobs",
                    "top_logprobs",
                    "user_id",
                }
            ),
        )
        for forbidden in ("parallel_tool_calls", "seed", "n"):
            self.assertNotIn(forbidden, G.DOCUMENTED_PARAMS)

    def test_contract_thresholds(self):
        self.assertEqual(G.MIN_MAX_TOKENS, 2048)
        self.assertEqual(G.DEFAULT_MAX_CHAT_SECONDS, 180.0)
        self.assertEqual(G.ANSWER_TYPES, ("data", "doc", "hybrid", "refusal", "clarify"))

    def test_starting_a_server_never_does_a_dns_lookup(self):
        """标准库的 HTTPServer 会在 bind 时做一次反向 DNS；断网的机器上那一步会卡住。"""
        import socket as socket_module

        original = socket_module.getfqdn

        def forbidden(*args, **kwargs):
            raise AssertionError("server_bind 不应该查 DNS")

        socket_module.getfqdn = forbidden
        self.addCleanup(setattr, socket_module, "getfqdn", original)
        server, thread = G.start_fake_server(port=0)
        self.addCleanup(G.stop_server, server, thread)
        self.assertEqual(server.server_name, "127.0.0.1")
        proxy, proxy_thread = G.start_proxy_server(upstream="http://127.0.0.1:1", port=0)
        self.addCleanup(G.stop_proxy_server, proxy, proxy_thread)
        self.assertEqual(proxy.server_name, "127.0.0.1")

    def test_free_port_is_usable(self):
        port = G.free_port()
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind(("127.0.0.1", port))

    def test_int_or_none_rejects_booleans(self):
        self.assertIsNone(G.int_or_none(True))
        self.assertIsNone(G.int_or_none("2048"))
        self.assertEqual(G.int_or_none(2048), 2048)
        self.assertEqual(G.int_or_none(2048.0), 2048)

    def test_display_width_counts_cjk_as_two_columns(self):
        self.assertEqual(G.display_width("abc"), 3)
        self.assertEqual(G.display_width("中文"), 4)

    def test_normalise_prefix(self):
        self.assertEqual(G.normalise_prefix("ds-gw"), "/ds-gw")
        self.assertEqual(G.normalise_prefix("/ds-gw/"), "/ds-gw")
        self.assertEqual(G.normalise_prefix("/"), "")
        self.assertEqual(G.normalise_prefix(""), "")

    def test_synthesized_arguments_respect_types(self):
        args = G.synthesize_tool_arguments(TOOL_METRICS["function"]["parameters"], 0)
        self.assertEqual(sorted(args), ["end", "start", "store_id"])
        for value in args.values():
            self.assertIsInstance(value, str)

    def test_synthesized_arguments_differ_between_variants(self):
        first = G.synthesize_tool_arguments(TOOL_METRICS["function"]["parameters"], 0)
        second = G.synthesize_tool_arguments(TOOL_METRICS["function"]["parameters"], 1)
        self.assertNotEqual(first, second)

    def test_reassemble_stream_handles_empty_input(self):
        merged = G.reassemble_stream([])
        self.assertEqual(merged["content"], "")
        self.assertEqual(merged["chunks"], 0)
        self.assertIsNone(merged["time_to_first_content_ms"])

    def test_count_tool_rounds(self):
        self.assertEqual(G.count_tool_rounds({}), 0)
        self.assertEqual(
            G.count_tool_rounds(
                {"messages": [{"role": "assistant", "tool_calls": [{"id": "a"}]}, {"role": "tool"}]}
            ),
            1,
        )

    def test_failing_scenarios_are_conditional_where_they_should_be(self):
        chat_path = "/ds-gw/chat/completions"

        def run(scenario, body):
            return G.ScenarioRun(
                scenario=scenario,
                llm_requests=[{"path": chat_path, "method": "POST", "body": body}],
            )

        runs = [
            run("thinking_starved", {"max_tokens": 4096}),
            run("json_empty", {}),
            run("http_500", {}),
        ]
        self.assertEqual(G.failing_scenarios(runs, chat_path), {"http_500"})
        runs = [
            run("thinking_starved", {"max_tokens": 128}),
            run("json_empty", {"response_format": {"type": "json_object"}}),
        ]
        self.assertEqual(
            G.failing_scenarios(runs, chat_path), {"thinking_starved", "json_empty"}
        )

    def test_contract_problems_spot_bad_shapes(self):
        good = {
            "answer": "x",
            "answer_type": "data",
            "citations": [],
            "data_evidence": [{"tool": "t", "params": {}, "result": {}}],
            "trace_id": "t-1",
        }
        self.assertEqual(G._contract_problems(good), [])
        bad = dict(good, answer_type="nope", citations=[{"doc_id": 1}])
        problems = G._contract_problems(bad)
        self.assertTrue(any("answer_type" in p for p in problems))
        self.assertTrue(any("citations[0]" in p for p in problems))
        self.assertTrue(G._contract_problems("not a dict"))

    def test_error_body_carries_the_chinese_name(self):
        body = G.error_body(402, "insufficient_balance", "invalid_request_error", "余额不足")
        self.assertIn("余额不足", body["error"]["message"])
        self.assertEqual(body["error"]["code"], "insufficient_balance")


class TestCli(unittest.TestCase):
    def assert_parser_rejects(self, argv):
        """argparse 出错时会往 stderr 写用法，测试输出里不需要它。"""
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                G.build_parser().parse_args(argv)

    def test_parser_accepts_the_documented_invocations(self):
        parser = G.build_parser()
        args = parser.parse_args(["preflight", "--service-url", "http://localhost:8000"])
        self.assertEqual(args.command, "preflight")
        self.assertEqual(args.model, G.DEFAULT_FAKE_MODEL)
        self.assertEqual(args.prefix, "/ds-gw")

        args = parser.parse_args(
            ["proxy", "--upstream", "https://api.deepseek.com", "--log", "llm_traffic.jsonl"]
        )
        self.assertEqual(args.upstream, "https://api.deepseek.com")
        self.assertIsNone(args.inject_thinking)

        args = parser.parse_args(["fake", "--port", "0", "--scenario", "http_429"])
        self.assertEqual(args.scenario, "http_429")

    def test_inject_thinking_only_takes_the_two_documented_values(self):
        parser = G.build_parser()
        self.assertEqual(
            parser.parse_args(
                ["proxy", "--upstream", "http://x", "--inject-thinking", "disabled"]
            ).inject_thinking,
            "disabled",
        )
        self.assert_parser_rejects(
            ["proxy", "--upstream", "http://x", "--inject-thinking", "none"]
        )

    def test_unknown_scenario_is_rejected_by_the_parser(self):
        self.assert_parser_rejects(["fake", "--scenario", "nope"])

    def test_a_subcommand_is_required(self):
        self.assert_parser_rejects([])


if __name__ == "__main__":
    unittest.main()
