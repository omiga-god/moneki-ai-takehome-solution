#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""大模型接入网关工具：假 DeepSeek 服务、接入预检、流量代理（只依赖标准库，Python 3.10+）。

三个子命令：

  fake       起一个按 DeepSeek 官方文档行为模拟的假 Chat Completions 服务。
             只在一个刻意取得很别扭的路径前缀下提供 `POST {prefix}/chat/completions`，
             其余路径一律 404 并记录下来；默认开启思考模式，支持工具调用、SSE 流式、
             保持连接的空行与 `: keep-alive` 注释，以及一整排错误场景。

  preflight  在本进程内起假服务，打印你需要注入的三个环境变量，等你重启服务后驱动你的
             `POST /api/chat`，逐项检查接入方式是否符合 `docs/API_CONTRACT.md` 7.1 与 7.3，
             输出 PASS/FAIL 表格、`preflight_report.md` 与 `preflight_report.json`。
             只适用于 OpenAI 兼容的 Chat Completions 路线；走别的协议的服务跑不了它，
             按契约 7.4 在 `LLM_SETUP.md` 第 7 节给等价证据。

  proxy      带路径前缀的反向代理，把你的服务发给模型的每一次请求和收到的每一次响应写成
             JSONL；流式响应逐字节透传，同时在日志里重新拼装成完整的 content /
             reasoning_content / tool_calls（拼装只认 OpenAI Chat Completions 的分片格式）。
             它会真的联网访问你指定的上游，用的是上游的 Key，产生的也是上游的费用。

事实来源：
  * `docs/API_CONTRACT.md` 第 5 节（`POST /api/chat` 的形状）与第 7 节（大模型接入）。
  * 内部记录《大模型接入实测》第一部分 D1–D16：2026-09-21 从 DeepSeek 官方文档逐页核对的事实。
  * 内部记录第二部分 L1–L11：一个本地思考型模型的实测，用于印证响应形状与流式顺序。

重要声明：本机没有 DeepSeek Key，下面所有“模拟 DeepSeek”的行为**全部来自文档，没有对照过
真实接口**。凡是文档没有明说、由我们推定的地方，都在代码注释里标了“推定”，并写进
`README_llm_gateway.md`。

设计约束：只用标准库；不写任何命令行未指定的文件；端口可以为 0 表示自动挑空闲端口；
每一个启动的服务和线程都能被关掉。
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import re
import signal
import socket
import socketserver
import sys
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Iterable, Iterator, Optional

# ======================================================================================
# 常量
# ======================================================================================

VERSION = "2.0.0"

#: 假服务与代理共用的路径前缀。
#: 刻意取一个不像任何厂商的前缀：评测时你的服务要经过带前缀的代理（契约 §7.2），
#: 任何“自己补 /v1”“只取域名”“截掉路径”的实现都会在这里露馅。
DEFAULT_PREFIX = "/ds-gw"

#: 控制面。它不属于“被测服务发来的流量”，既不记录也不参与 P6。
#: 规范没有规定控制面长什么样，这是本工具自己的约定。
CONTROL_PREFIX = "/__control"

#: D15：DeepSeek 文档列出的请求顶层参数（含限速页的 `user_id`）。
#: 文档里**没有** `parallel_tool_calls`、`seed`、`n`。
DOCUMENTED_PARAMS = frozenset(
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
)

#: 契约 §7.3：`max_tokens` 不设，或不小于 2048。
MIN_MAX_TOKENS = 2048

#: `thinking_starved` 场景的触发阈值：`max_tokens` 小于它就认为额度被思考吃光。
#: 阈值本身是我们定的（文档只说思考占输出额度、思考模式默认 64K，见 D9），
#: 取 1024 是为了让 2048 这条底线有一段安全余量。
THINKING_STARVED_THRESHOLD = 1024

#: 契约 §5：`answer_type` 五选一。
ANSWER_TYPES = ("data", "doc", "hybrid", "refusal", "clarify")

#: 刻意取一个不像任何厂商的模型名；写死模型名的实现会被 P2 抓到。
DEFAULT_FAKE_MODEL = "preflight-model-7f3a"
DEFAULT_FAKE_API_KEY = "preflight-key-3b9c1f"

DEFAULT_SLOW_DELAY = 6.0
DEFAULT_KEEPALIVES = 3
DEFAULT_HANG_CAP = 200.0
DEFAULT_MAX_CHAT_SECONDS = 180.0
DEFAULT_CHAT_TIMEOUT = 190.0
DEFAULT_PROXY_LOG = "llm_traffic.jsonl"

#: 全部场景。顺序就是预检的驱动顺序。
SCENARIOS = (
    "normal",
    "thinking_starved",
    "empty_content",
    "json_empty",
    "bad_tool_args",
    "content_filter",
    "insufficient_resource",
    "aborted",
    "http_401",
    "http_402",
    "http_422",
    "http_429",
    "http_500",
    "http_503",
    "slow",
    "hang",
)

#: 这些场景下模型一定是坏的，`/api/chat` 必须给出结构化的 refusal
#: （或不依赖模型、有据可查的回答），见契约 §7.3 最后两行与 §5 的最后一段。
ALWAYS_FAILING_SCENARIOS = frozenset(
    {
        "empty_content",
        "bad_tool_args",
        "content_filter",
        "insufficient_resource",
        "aborted",
        "http_401",
        "http_402",
        "http_422",
        "http_429",
        "http_500",
        "http_503",
        "hang",
    }
)

#: D13 的错误码与名称。HTTP 状态码来自文档；**错误正文的字段形状是推定的**，
#: 文档的错误码页只给了状态码和一句中文名称，没有给 JSON 正文的结构。
HTTP_ERROR_SCENARIOS = {
    "http_401": (401, "authentication_error", "invalid_request_error", "认证失败"),
    "http_402": (402, "insufficient_balance", "invalid_request_error", "余额不足"),
    "http_422": (422, "invalid_request_error", "invalid_request_error", "参数错误"),
    "http_429": (429, "rate_limit_reached", "invalid_request_error", "限速"),
    "http_500": (500, "server_error", "server_error", "服务器错误"),
    "http_503": (503, "service_unavailable", "server_error", "服务器繁忙"),
}

#: 假服务给出的固定正文。刻意不含任何真实数字，也不含思考标记。
FAKE_ANSWER_TEXT = "这是预检假模型的固定回答，具体数值请以工具结果为准。"

#: `aborted` 场景的正文：写到一半被掐断。
#: 它**不是空串**，所以只按“内容为空”判错的实现抓不到它，必须看 `finish_reason`（D11）。
FAKE_ABORTED_TEXT = "这是预检假模型的固定回答，具体数"

#: `content_filter` 场景的正文：同样是半截话，不是空串。
FAKE_FILTERED_TEXT = "这是预检假模型的固定回"

#: 失败场景的正文/思考里都会带上这个标记（每次运行都不一样，见 `FakeLLMState.failure_marker`）。
#: 它出现在 `answer` / `citations` / `data_evidence` 里，就说明服务把一次失败的模型输出
#: 当成了正常回答转给了用户——P9 会因此判红。
FAILURE_MARKER_PREFIX = "FAILOUT-"

#: 失败场景下，如果正文是空串，就把标记放进思考里（空串里塞不进东西）。
FAILURE_NOTE = "（这一轮是预检的失败场景，标记 %s 绝不允许出现在给用户看的任何字段里。）"

#: JSON 模式下的固定正文（D10：`response_format.type` 可以是 `json_object`）。
FAKE_JSON_ANSWER = '{"answer":"这是预检假模型的固定回答","source":"fake"}'

#: 响应里的 `created` 固定，保证同样的请求得到逐字节相同的响应（代理的字节级透传可被测试）。
FIXED_CREATED = 1780000000

#: 记录请求时需要脱敏的头。
REDACTED_HEADERS = frozenset({"authorization", "api-key", "x-api-key", "x-goog-api-key"})

#: 预检驱动 `/api/chat` 用的中性问题：不含门店号、不含日期、不含任何真实数字，
#: 保证任何实现都不会因为“答不上来”而被判失败。
DEFAULT_QUESTIONS = (
    "你们的退款规则是怎么规定的？",
    "最近一段时间的整体经营情况怎么样？",
)

PASS = "PASS"
FAIL = "FAIL"
SKIP = "SKIP"

_STATUS_CN = {PASS: "通过", FAIL: "失败", SKIP: "未检查"}

CHECK_TITLES = {
    "P1": "服务确实把请求发到了注入的 LLM_BASE_URL（含路径前缀）",
    "P2": "请求里的 model 等于注入的 LLM_MODEL",
    "P3": "注入的 Key 以 Authorization: Bearer 发送",
    "P4": "只用了 DeepSeek 文档列出的顶层参数",
    "P5": "max_tokens 不设，或不小于 2048",
    "P6": "没有访问 {prefix}/chat/completions 之外的任何路径",
    "P7": "工具定义规范，且每一个工具调用都以 role=tool + tool_call_id 回传",
    "P8": "每个场景下 /api/chat 都返回 HTTP 200 与字段完整的合法 JSON",
    "P9": "模型不可用时给出结构化 refusal，answer 从不是空串",
    "P10": "思考内容没有漏进 answer / citations / data_evidence",
    "P11": "/api/chat 在时限内返回（含长时间无响应的场景）",
    "P12": "注入环境变量后 /api/health 报告 llm_mode = live",
    "P13": "多轮工具调用之间 reasoning_content 原样回传（没有触发 400）",
    "P14": "保持连接的空行与 SSE 注释没有把服务弄坏",
}

CHECK_IDS = tuple("P%d" % i for i in range(1, 15))


# ======================================================================================
# 小工具
# ======================================================================================


def now_iso() -> str:
    """本地时区的 ISO 时间戳，精确到毫秒。"""
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="milliseconds")


def free_port(host: str = "127.0.0.1") -> int:
    """挑一个当前空闲的端口号。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((host, 0))
        return int(sock.getsockname()[1])


def parse_json_bytes(raw: bytes) -> tuple[Any, str]:
    """把正文解析成 (对象或 None, 文本)。不是 JSON 时对象为 None。"""
    if not raw:
        return None, ""
    text = raw.decode("utf-8", errors="replace")
    try:
        return json.loads(text), text
    except (ValueError, TypeError):
        return None, text


def truncate(text: str, limit: int = 4000) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + "…<已截断 %d 字符>" % (len(text) - limit)


def int_or_none(value: Any) -> Optional[int]:
    """整数化；`True`/`False` 不算数字。"""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value == int(value):
        return int(value)
    return None


def interruptible_sleep(stop_event: threading.Event, seconds: float) -> bool:
    """可被关服打断的 sleep。睡满返回 True，被打断返回 False。"""
    deadline = time.monotonic() + max(0.0, seconds)
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return True
        if stop_event.wait(min(0.05, remaining)):
            return False


def display_width(text: str) -> int:
    """终端显示宽度，中日韩全角字符按 2 列算。"""
    return sum(2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1 for ch in text)


def pad(text: str, width: int) -> str:
    return text + " " * max(0, width - display_width(text))


def split_pieces(text: str, size: int) -> list[str]:
    if not text:
        return []
    return [text[i : i + size] for i in range(0, len(text), size)]


def stable_id(prefix: str, *parts: Any) -> str:
    blob = "\x1f".join(
        json.dumps(part, ensure_ascii=False, sort_keys=True, default=str) for part in parts
    )
    return prefix + hashlib.sha256(blob.encode("utf-8")).hexdigest()[:20]


class OfflineThreadingHTTPServer(ThreadingHTTPServer):
    """不做任何 DNS 查询的 `ThreadingHTTPServer`。

    标准库的 `HTTPServer.server_bind()` 会调用 `socket.getfqdn()`，那是一次**反向 DNS 查询**。
    本工具承诺不产生任何网络请求；而且在断网或 DNS 不通的机器上，那一步会让服务启动卡住几秒到几十秒。
    这里改成直接用绑定的地址当 `server_name`。
    """

    daemon_threads = True
    block_on_close = False
    allow_reuse_address = True

    def server_bind(self) -> None:
        socketserver.TCPServer.server_bind(self)
        host, port = self.server_address[:2]
        self.server_name = host
        self.server_port = port


def normalise_prefix(prefix: str) -> str:
    """前缀统一成 `/xxx` 的形式；空前缀返回空串。"""
    prefix = (prefix or "").strip()
    if not prefix or prefix == "/":
        return ""
    if not prefix.startswith("/"):
        prefix = "/" + prefix
    return prefix.rstrip("/")


# ======================================================================================
# 假 DeepSeek 服务
# ======================================================================================


class FakeLLMState:
    """假服务的可变状态：当前场景、录到的请求、思考标记、发出去过的 reasoning。"""

    def __init__(
        self,
        scenario: str = "normal",
        model: str = DEFAULT_FAKE_MODEL,
        api_key: str = DEFAULT_FAKE_API_KEY,
        prefix: str = DEFAULT_PREFIX,
        slow_delay: float = DEFAULT_SLOW_DELAY,
        keepalives: int = DEFAULT_KEEPALIVES,
        hang_cap: float = DEFAULT_HANG_CAP,
        marker: Optional[str] = None,
        verbose: bool = False,
    ) -> None:
        if scenario not in SCENARIOS:
            raise ValueError("未知场景：%s" % scenario)
        self._lock = threading.Lock()
        self.scenario = scenario
        self.model = model
        self.api_key = api_key
        self.prefix = normalise_prefix(prefix)
        self.chat_path = self.prefix + "/chat/completions"
        self.slow_delay = float(slow_delay)
        self.keepalives = max(1, int(keepalives))
        self.hang_cap = float(hang_cap)
        # 思考内容里嵌的唯一标记；它出现在候选人的 answer 里就说明思考内容漏出去了。
        self.marker = marker or ("RSN-" + uuid.uuid4().hex[:12])
        # 失败场景的模型输出里嵌的唯一标记；它出现在对外字段里就说明服务把失败当成了成功。
        self.failure_marker = FAILURE_MARKER_PREFIX + uuid.uuid4().hex[:12]
        self.verbose = verbose
        self.requests: list[dict] = []
        self.issued_reasoning: dict[str, str] = {}
        self.seq = 0
        self.stop_event = threading.Event()

    # -- 场景 ---------------------------------------------------------------------------

    def snapshot(self) -> tuple[str, float, float, int]:
        with self._lock:
            return self.scenario, self.slow_delay, self.hang_cap, self.keepalives

    def set_scenario(
        self,
        scenario: str,
        slow_delay: Optional[float] = None,
        hang_cap: Optional[float] = None,
        keepalives: Optional[int] = None,
    ) -> None:
        if scenario not in SCENARIOS:
            raise ValueError("未知场景：%s" % scenario)
        with self._lock:
            self.scenario = scenario
            if slow_delay is not None:
                self.slow_delay = float(slow_delay)
            if hang_cap is not None:
                self.hang_cap = float(hang_cap)
            if keepalives is not None:
                self.keepalives = max(1, int(keepalives))

    # -- 请求记录 -----------------------------------------------------------------------

    def record(self, entry: dict) -> dict:
        with self._lock:
            self.seq += 1
            entry["seq"] = self.seq
            self.requests.append(entry)
            return entry

    def list_requests(self) -> list[dict]:
        with self._lock:
            return list(self.requests)

    def take_requests(self) -> list[dict]:
        """取走并清空当前累积的请求。"""
        with self._lock:
            taken = list(self.requests)
            self.requests.clear()
            return taken

    def reset(self) -> None:
        with self._lock:
            self.requests.clear()
            self.issued_reasoning.clear()

    # -- 思考内容登记（D8 的回传校验靠它） -------------------------------------------------

    def register_reasoning(self, call_id: str, reasoning: str) -> None:
        if not reasoning:
            return
        with self._lock:
            self.issued_reasoning[call_id] = reasoning

    def reasoning_for(self, call_id: str) -> Optional[str]:
        with self._lock:
            return self.issued_reasoning.get(call_id)


# -- 工具参数合成 ------------------------------------------------------------------------


def synthesize_value(name: str, spec: dict, variant: int) -> Any:
    """按参数名和 JSON Schema 造一个像样的值。`variant` 让不同的工具调用拿到不同的值。"""
    if not isinstance(spec, dict):
        spec = {}
    enum = spec.get("enum")
    if isinstance(enum, list) and enum:
        return enum[variant % len(enum)]
    type_ = spec.get("type")
    if isinstance(type_, list):
        type_ = next((t for t in type_ if t != "null"), None)
    if type_ == "integer":
        return 5 + variant
    if type_ == "number":
        return 5.0 + variant
    if type_ == "boolean":
        return variant % 2 == 0
    if type_ == "array":
        items = spec.get("items")
        if isinstance(items, dict):
            return [synthesize_value(name, items, variant)]
        return []
    if type_ == "object":
        return synthesize_tool_arguments(spec, variant)
    lowered = name.lower()
    if "start" in lowered or "from" in lowered or "begin" in lowered or "开始" in name:
        return "2026-0%d-01" % (6 + variant % 3)
    if "end" in lowered or "until" in lowered or "结束" in name:
        return "2026-0%d-28" % (6 + variant % 3)
    if "date" in lowered or "day" in lowered or "month" in lowered or "日期" in name:
        return "2026-06-%02d" % (1 + variant)
    if "store" in lowered or "shop" in lowered or "门店" in name:
        return "S%02d" % (1 + variant % 9)
    if "product" in lowered or "item" in lowered or "sku" in lowered or "商品" in name:
        return "P%02d" % (1 + variant % 9)
    if "query" in lowered or "question" in lowered or "text" in lowered or "查询" in name:
        return "预检合成的查询 %d" % variant
    return "preflight-%d" % variant


def synthesize_tool_arguments(parameters: Any, variant: int = 0) -> dict:
    """按工具自己声明的 JSON Schema 造出一组参数。"""
    if not isinstance(parameters, dict):
        return {}
    props = parameters.get("properties")
    if not isinstance(props, dict) or not props:
        return {}
    required = parameters.get("required")
    if not isinstance(required, list) or not required:
        required = list(props.keys())
    args: dict[str, Any] = {}
    for name in required:
        if not isinstance(name, str) or name not in props:
            continue
        args[name] = synthesize_value(name, props.get(name), variant)
    return args


#: 请求里压根没有 `tools` 时，假服务自己编一个工具用的 schema。
FALLBACK_TOOL_PARAMETERS = {
    "type": "object",
    "properties": {
        "start": {"type": "string"},
        "end": {"type": "string"},
        "store_id": {"type": "string"},
    },
    "required": ["start", "end", "store_id"],
}


def tool_function(tools: list, index: int) -> tuple[str, Any]:
    """取第 index 个工具的函数名和参数 schema。

    工具列表为空时给一个兜底 schema。
    工具**声明了名字但没有 `parameters`** 时，按官方文档的“无参数函数”处理，
    参数就是空对象 `{}`——绝不能拿兜底 schema 去替它编出一堆它没声明过的参数。
    """
    if not tools:
        return "query_metrics", FALLBACK_TOOL_PARAMETERS
    entry = tools[index % len(tools)]
    if not isinstance(entry, dict):
        return "query_metrics", FALLBACK_TOOL_PARAMETERS
    fn = entry.get("function")
    if not isinstance(fn, dict):
        return "query_metrics", FALLBACK_TOOL_PARAMETERS
    name = fn.get("name")
    if not isinstance(name, str) or not name:
        name = "query_metrics"
    parameters = fn.get("parameters")
    if not isinstance(parameters, dict):
        # 省略 `parameters`（或给了个不是对象的东西）＝ 无参数函数。
        parameters = {}
    return name, parameters


def build_tool_call(
    tools: list, variant: int, broken: bool, reasoning: str = "", broken_suffix: str = ""
) -> dict:
    """构造一次标准工具调用。

    D12：`tool_calls[].function.arguments` 是 JSON **字符串**，不是对象。
    `broken=True` 时故意给一段被截断的非法 JSON。

    `id` 由函数名、参数、轮次和**这一轮的思考内容**一起决定：
    同样的请求得到同样的 id（代理的字节级透传才可测），
    而开思考与关思考的同一个调用会拿到不同的 id，
    免得 D8 的回传校验把“这一轮本来就没有思考”误判成“没有回传”。
    """
    name, parameters = tool_function(tools, variant)
    args = synthesize_tool_arguments(parameters, variant)
    arguments = json.dumps(args, ensure_ascii=False, separators=(",", ":"))
    if broken:
        cut = max(1, int(len(arguments) * 0.6))
        arguments = arguments[:cut] + broken_suffix
        try:
            json.loads(arguments)
        except ValueError:
            pass
        else:  # 极短的参数被截断后仍然合法时，强行弄坏它
            arguments = arguments + ' {"truncated"'
    return {
        "id": stable_id("call_", name, arguments, variant, reasoning)[: len("call_") + 16],
        "type": "function",
        "function": {"name": name, "arguments": arguments},
    }


def reasoning_text(marker: str, round_index: int) -> str:
    """思考内容：非空，含唯一标记，每一轮都不一样，流式时能切成很多片。

    D5/D7：`deepseek-flash` 默认开启思考模式，思考过程放在 `reasoning_content`，与 `content` 同级。
    """
    return (
        "第 %d 轮思考：先判断这个问题要不要查数据。"
        "标记 %s 只出现在思考里，绝不能进入给用户看的回答。"
        "如果需要数字，就调用工具，拿到结果之后再组织语言。"
        "组织语言的时候数字要以工具结果为准，不要自己改写。"
    ) % (round_index + 1, marker)


def thinking_enabled(body: dict) -> bool:
    """D5：思考模式默认开启，`thinking: {"type": "disabled"}` 关掉。"""
    thinking = body.get("thinking")
    if isinstance(thinking, dict) and thinking.get("type") == "disabled":
        return False
    return True


def wants_json_object(body: dict) -> bool:
    rf = body.get("response_format")
    return isinstance(rf, dict) and rf.get("type") == "json_object"


def count_tool_rounds(body: dict) -> int:
    """历史里已经有几条带 `tool_calls` 的 assistant 消息，就是已经走过几轮工具调用。

    刻意不要求 `tool_call_id` 是我们发出去的那一个：即使候选人把 id 改写了，
    轮次也会正常推进，不会让假服务和被测服务互相死等。
    """
    messages = body.get("messages")
    if not isinstance(messages, list):
        return 0
    rounds = 0
    for message in messages:
        if not isinstance(message, dict) or message.get("role") != "assistant":
            continue
        calls = message.get("tool_calls")
        if isinstance(calls, list) and calls:
            rounds += 1
    return rounds


def plan_completion(body: dict, state: FakeLLMState, scenario: str) -> dict:
    """决定这一次要返回什么：正文、思考、工具调用、finish_reason。"""
    tools = body.get("tools") if isinstance(body.get("tools"), list) else []
    round_index = count_tool_rounds(body)
    reasoning = reasoning_text(state.marker, round_index) if thinking_enabled(body) else ""

    effective = scenario
    if effective == "slow":
        # slow 只影响传输节奏，不影响内容。
        effective = "normal"
    if effective == "thinking_starved":
        # 契约 §7.3 / D9：思考也占输出额度，额度设小了正文就是空的。
        max_tokens = int_or_none(body.get("max_tokens"))
        starved = max_tokens is not None and max_tokens < THINKING_STARVED_THRESHOLD
        effective = "__starved" if starved else "normal"
    if effective == "json_empty":
        # D10：JSON 模式“偶尔会返回空内容”。没请求 JSON 模式就不咬人。
        effective = "__json_empty" if wants_json_object(body) else "normal"

    def failed(content: str, finish_reason: str) -> dict:
        """一次“失败”的模型输出：一定带上失败标记，好让 P9 抓到被转发出去的情况。

        正文非空时标记贴在正文尾巴上（半截话本来就像被截断的乱码）；
        正文是空串时塞不进东西，就放进思考里。思考被关掉时两边都没有，
        那种情况下也无从泄漏。
        """
        note = FAILURE_NOTE % state.failure_marker
        if content:
            return {
                "content": content + state.failure_marker,
                "reasoning": reasoning,
                "tool_calls": None,
                "finish_reason": finish_reason,
            }
        return {
            "content": "",
            "reasoning": (reasoning + note) if reasoning else "",
            "tool_calls": None,
            "finish_reason": finish_reason,
        }

    if effective == "__starved":
        return failed("", "length")
    if effective in ("empty_content", "__json_empty"):
        return failed("", "stop")
    if effective == "content_filter":
        # D11：finish_reason 还有 content_filter。正文是半截话，**不是空串**。
        return failed(FAKE_FILTERED_TEXT, "content_filter")
    if effective == "insufficient_resource":
        # D11：finish_reason 还有 insufficient_system_resource。
        return failed("", "insufficient_system_resource")
    if effective == "aborted":
        # D11：finish_reason 还有 aborted。正文写到一半就断了，**不是空串**：
        # 只判断“内容为空”的实现会把这半句话当成正常回答发给用户。
        return failed(FAKE_ABORTED_TEXT, "aborted")

    broken = effective == "bad_tool_args"
    # 畸形工具参数也算失败输出：把标记塞进那串非法 JSON，
    # 好抓到“把原始 arguments 直接抄进 data_evidence.params”的实现。
    broken_suffix = state.failure_marker if broken else ""

    if tools and round_index == 0:
        # D12：一条 assistant 消息可以带多个 tool_calls，此时 content 可为空串。
        calls = [
            build_tool_call(tools, 0, broken, reasoning, broken_suffix),
            build_tool_call(tools, 1, broken, reasoning, broken_suffix),
        ]
        return {
            "content": "",
            "reasoning": reasoning,
            "tool_calls": calls,
            "finish_reason": "tool_calls",
        }
    if tools and round_index == 1:
        # 第二轮：再要一次工具，逼出“多轮之间也要回传 reasoning_content”。
        calls = [build_tool_call(tools, 2, broken, reasoning, broken_suffix)]
        return {
            "content": "",
            "reasoning": reasoning,
            "tool_calls": calls,
            "finish_reason": "tool_calls",
        }

    content = FAKE_JSON_ANSWER if wants_json_object(body) else FAKE_ANSWER_TEXT
    return {"content": content, "reasoning": reasoning, "tool_calls": None, "finish_reason": "stop"}


def completion_envelope(body: dict, comp: dict, model: str, scenario: str) -> dict:
    """非流式响应信封。字段名按 D2/D7/D11/D12 的文档形状。"""
    message: dict[str, Any] = {"role": "assistant", "content": comp["content"]}
    if comp["reasoning"]:
        # D7：思考过程通过 `reasoning_content` 返回，与 `content` 同级。
        message["reasoning_content"] = comp["reasoning"]
    if comp["tool_calls"]:
        message["tool_calls"] = comp["tool_calls"]
    prompt_tokens = max(1, len(json.dumps(body.get("messages") or [], ensure_ascii=False)) // 4)
    reasoning_tokens = max(0, len(comp["reasoning"]) // 4)
    completion_tokens = reasoning_tokens + max(0, len(comp["content"]) // 4) + 1
    return {
        "id": stable_id("chatcmpl-", body, scenario),
        "object": "chat.completion",
        "created": FIXED_CREATED,
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": message,
                "logprobs": None,
                "finish_reason": comp["finish_reason"],
            }
        ],
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
            "prompt_tokens_details": {"cached_tokens": 0},
            "completion_tokens_details": {"reasoning_tokens": reasoning_tokens},
        },
        "system_fingerprint": "fake_ds_gw_%s" % VERSION.replace(".", "_"),
    }


def iter_stream_chunks(
    body: dict, comp: dict, model: str, scenario: str, usage: Optional[dict] = None
) -> Iterator[dict]:
    """流式分片。

    契约 §7.3 / L10：先到的是 `delta.reasoning_content`，之后才是 `delta.content`。

    `usage` 不为空时，按现行文档的形状挂在**最后那个带 `finish_reason` 的分片**上，
    而不是另发一个 `choices: []` 的块。
    """
    chunk_id = stable_id("chatcmpl-", body, scenario)

    def make(delta: dict, finish: Optional[str] = None) -> dict:
        return {
            "id": chunk_id,
            "object": "chat.completion.chunk",
            "created": FIXED_CREATED,
            "model": model,
            "choices": [{"index": 0, "delta": delta, "logprobs": None, "finish_reason": finish}],
        }

    yield make({"role": "assistant", "content": ""})
    for piece in split_pieces(comp["reasoning"], 6):
        yield make({"reasoning_content": piece})
    if comp["tool_calls"]:
        for index, call in enumerate(comp["tool_calls"]):
            yield make(
                {
                    "tool_calls": [
                        {
                            "index": index,
                            "id": call["id"],
                            "type": "function",
                            "function": {"name": call["function"]["name"], "arguments": ""},
                        }
                    ]
                }
            )
            for piece in split_pieces(call["function"]["arguments"], 8):
                yield make({"tool_calls": [{"index": index, "function": {"arguments": piece}}]})
    else:
        for piece in split_pieces(comp["content"], 8):
            yield make({"content": piece})
    final = make({}, comp["finish_reason"])
    if usage is not None:
        final["usage"] = usage
    yield final


def error_body(status: int, code: str, type_: str, cn_name: str, detail: str = "") -> dict:
    """DeepSeek 风格的错误正文。

    **推定**：文档的错误码页只给了状态码和中文名称，没有给 JSON 正文的结构；
    这里按 OpenAI 兼容接口最常见的 `{"error": {...}}` 形状构造，并把 D13 的中文名写进 message。
    """
    message = "%s (HTTP %d %s)" % (code.replace("_", " ").title(), status, cn_name)
    if detail:
        message += "：" + detail
    return {"error": {"message": message, "type": type_, "param": None, "code": code}}


class FakeLLMHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "fake-deepseek/" + VERSION
    sys_version = ""

    # -- 基础设施 -----------------------------------------------------------------------

    @property
    def state(self) -> FakeLLMState:
        return self.server.state  # type: ignore[attr-defined]

    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003
        if self.state.verbose:
            sys.stderr.write("[fake] %s %s\n" % (self.address_string(), fmt % args))

    def handle_one_request(self) -> None:
        # 被测服务经常在超时后直接挂断（hang、slow 场景尤其如此）。
        # 那不是假服务的错误，不要把 traceback 喷到 stderr 上。
        try:
            super().handle_one_request()
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True

    def _read_body(self) -> bytes:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            length = 0
        if length <= 0:
            return b""
        return self.rfile.read(length)

    def _note(self, status: int, note: str = "") -> None:
        record = getattr(self, "_record", None)
        if isinstance(record, dict):
            record["response_status"] = status
            if note:
                record["response_note"] = note

    def _send_raw(self, status: int, content_type: str, payload: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _send_json(self, status: int, obj: Any, note: str = "") -> None:
        self._note(status, note)
        payload = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self._send_raw(status, "application/json; charset=utf-8", payload)

    # -- 路由 ---------------------------------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802
        self._dispatch()

    def do_POST(self) -> None:  # noqa: N802
        self._dispatch()

    def do_PUT(self) -> None:  # noqa: N802
        self._dispatch()

    def do_PATCH(self) -> None:  # noqa: N802
        self._dispatch()

    def do_DELETE(self) -> None:  # noqa: N802
        self._dispatch()

    def do_HEAD(self) -> None:  # noqa: N802
        self._dispatch()

    def _dispatch(self) -> None:
        state = self.state
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        raw = self._read_body()

        if path == CONTROL_PREFIX or path.startswith(CONTROL_PREFIX + "/"):
            # 控制面不算“被测服务发来的流量”，不记录、不参与 P6。
            self._handle_control(path, raw)
            return

        body, body_text = parse_json_bytes(raw)
        self._record = state.record(self._make_record(path, parsed.query, body, body_text))

        if path == state.chat_path and self.command == "POST":
            self._handle_chat(body if isinstance(body, dict) else {})
            return

        # 其它所有路径一律 404，并且已经记下来了：
        # `/v1/chat/completions`、`{prefix}/v1/...`、`/anthropic/...`、`/responses`、
        # `/beta/...`、`/models`、查余额等等都会在这里被 P6 抓到。
        self._send_json(
            404,
            {
                "error": {
                    "message": "Not Found (%s %s)。本服务只提供 POST %s。"
                    % (self.command, path, state.chat_path),
                    "type": "invalid_request_error",
                    "param": None,
                    "code": "not_found",
                }
            },
            note="路径不在 %s 之内" % state.chat_path,
        )

    def _make_record(self, path: str, query: str, body: Any, body_text: str) -> dict:
        state = self.state
        headers: dict[str, str] = {}
        for key, value in self.headers.items():
            if key.lower() in REDACTED_HEADERS:
                # 只记长度，不记值。
                headers[key] = "<redacted len=%d>" % len(value)
            else:
                headers[key] = value
        auth_raw = self.headers.get("Authorization")
        auth: dict[str, Any] = {"present": auth_raw is not None}
        if auth_raw is not None:
            scheme, _, token = auth_raw.partition(" ")
            token = token.strip()
            auth.update(
                {
                    "scheme": scheme,
                    "value_length": len(auth_raw),
                    "token_length": len(token),
                    # 只保留“是否等于注入的 Key”的布尔值，不保留 Key 本身。
                    "matches_expected": bool(
                        state.api_key and hmac.compare_digest(token, state.api_key)
                    ),
                }
            )
        scenario, _, _, _ = state.snapshot()
        return {
            "ts": now_iso(),
            "monotonic": round(time.monotonic(), 6),
            "scenario": scenario,
            "method": self.command,
            "path": path,
            "query": query,
            "headers": headers,
            "authorization": auth,
            "body": body,
            "body_text": truncate(body_text) if body is None else "",
            "response_status": None,
            "response_note": None,
        }

    # -- 控制面 -------------------------------------------------------------------------

    def _handle_control(self, path: str, raw: bytes) -> None:
        state = self.state
        if path == CONTROL_PREFIX + "/requests" and self.command == "GET":
            scenario, slow_delay, hang_cap, keepalives = state.snapshot()
            requests = state.list_requests()
            self._send_json(
                200,
                {
                    "scenario": scenario,
                    "slow_delay": slow_delay,
                    "hang_cap": hang_cap,
                    "keepalives": keepalives,
                    "marker": state.marker,
                    "model": state.model,
                    "chat_path": state.chat_path,
                    "count": len(requests),
                    "requests": requests,
                },
            )
            return
        if path == CONTROL_PREFIX + "/reset" and self.command == "POST":
            state.reset()
            self._send_json(200, {"ok": True})
            return
        if path == CONTROL_PREFIX + "/scenario" and self.command == "POST":
            body, _ = parse_json_bytes(raw)
            if not isinstance(body, dict):
                self._send_json(400, {"error": "body must be a JSON object"})
                return
            scenario = body.get("scenario")
            if scenario not in SCENARIOS:
                self._send_json(
                    400,
                    {"error": "unknown scenario: %r" % (scenario,), "known": list(SCENARIOS)},
                )
                return
            try:
                state.set_scenario(
                    scenario,
                    slow_delay=body.get("slow_delay"),
                    hang_cap=body.get("hang_cap"),
                    keepalives=body.get("keepalives"),
                )
            except (TypeError, ValueError) as exc:
                self._send_json(400, {"error": str(exc)})
                return
            current, slow_delay, hang_cap, keepalives = state.snapshot()
            self._send_json(
                200,
                {
                    "ok": True,
                    "scenario": current,
                    "slow_delay": slow_delay,
                    "hang_cap": hang_cap,
                    "keepalives": keepalives,
                },
            )
            return
        self._send_json(404, {"error": "unknown control endpoint: %s %s" % (self.command, path)})

    # -- chat/completions ---------------------------------------------------------------

    def _check_echo(self, body: dict) -> Optional[str]:
        """D8：带 `tools` 的请求，此前每一条 assistant 消息都要原样回传 `reasoning_content`。

        只认我们自己发出去过的 `tool_call_id`。思考被关掉的那些轮次没有登记过 reasoning，
        自然也不会被这条规则咬到。
        """
        tools = body.get("tools")
        if not isinstance(tools, list) or not tools:
            # D8：不带 tools 时，回传与否都会被忽略。
            return None
        messages = body.get("messages")
        if not isinstance(messages, list):
            return None
        for message in messages:
            if not isinstance(message, dict) or message.get("role") != "assistant":
                continue
            calls = message.get("tool_calls")
            if not isinstance(calls, list):
                continue
            expected = None
            call_id = None
            for call in calls:
                if not isinstance(call, dict):
                    continue
                candidate = call.get("id")
                if isinstance(candidate, str):
                    found = self.state.reasoning_for(candidate)
                    if found:
                        expected, call_id = found, candidate
                        break
            if expected is None:
                continue
            got = message.get("reasoning_content")
            if got != expected:
                if got is None:
                    detail = "assistant 消息（tool_call_id=%s）没有带 reasoning_content" % call_id
                else:
                    detail = "assistant 消息（tool_call_id=%s）的 reasoning_content 与下发时不一致" % call_id
                return detail
        return None

    def _handle_chat(self, body: dict) -> None:
        state = self.state
        scenario, slow_delay, hang_cap, keepalives = state.snapshot()

        # 1. 认证。D13：401 认证失败。
        auth = self.headers.get("Authorization") or ""
        scheme, _, token = auth.partition(" ")
        authorised = scheme.lower() == "bearer" and hmac.compare_digest(token.strip(), state.api_key)
        if not authorised:
            self._send_json(
                401,
                error_body(
                    401,
                    "authentication_error",
                    "invalid_request_error",
                    "认证失败",
                    "请用 Authorization: Bearer <LLM_API_KEY> 发送预检给你的 Key",
                ),
                note="缺少或不正确的 Authorization",
            )
            return

        # 2. 被点名的 HTTP 错误场景（D13）。
        if scenario in HTTP_ERROR_SCENARIOS:
            status, code, type_, cn_name = HTTP_ERROR_SCENARIOS[scenario]
            self._send_json(
                status, error_body(status, code, type_, cn_name), note="场景 %s" % scenario
            )
            return

        # 3. response_format（D10）：只接受 text 与 json_object。
        response_format = body.get("response_format")
        if isinstance(response_format, dict):
            rf_type = response_format.get("type")
            if rf_type not in ("text", "json_object"):
                # **推定**：文档只说“只能是 text 或 json_object”，没有给不合法取值时的状态码。
                # 这里按“参数错误”取 422（D13），真实状态码未经验证。
                self._send_json(
                    422,
                    error_body(
                        422,
                        "invalid_request_error",
                        "invalid_request_error",
                        "参数错误",
                        "response_format.type 只支持 text 与 json_object，收到 %r" % (rf_type,),
                    ),
                    note="不支持的 response_format.type",
                )
                return

        # 4. D8 的回传校验。
        echo_problem = self._check_echo(body)
        if echo_problem is not None:
            self._send_json(
                400,
                error_body(
                    400,
                    "invalid_request_error",
                    "invalid_request_error",
                    "格式错误",
                    echo_problem
                    + "；带 tools 的请求必须把收到的 assistant 消息整条追加进 messages",
                ),
                note="reasoning_content 没有原样回传",
            )
            return

        # 5. hang：收下连接但一直不回。
        if scenario == "hang":
            self._note(0, "场景 hang：不返回任何字节")
            interruptible_sleep(state.stop_event, hang_cap)
            self.close_connection = True
            return

        comp = plan_completion(body, state, scenario)
        for call in comp["tool_calls"] or []:
            state.register_reasoning(call["id"], comp["reasoning"])

        streaming = body.get("stream") is True
        slow = scenario == "slow"
        if streaming:
            self._send_stream(body, comp, state.model, scenario, slow, slow_delay, keepalives)
        else:
            self._send_blocking(body, comp, state.model, scenario, slow, slow_delay, keepalives)

    def _send_blocking(
        self,
        body: dict,
        comp: dict,
        model: str,
        scenario: str,
        slow: bool,
        slow_delay: float,
        keepalives: int,
    ) -> None:
        payload = json.dumps(
            completion_envelope(body, comp, model, scenario), ensure_ascii=False
        ).encode("utf-8")
        if not slow:
            self._note(200)
            self._send_raw(200, "application/json; charset=utf-8", payload)
            return
        # D14：服务繁忙时连接会保持，非流式响应体前面会持续返回空行。
        # 没有 Content-Length，用 `Connection: close` 表示“正文到连接关闭为止”。
        self._note(200, "场景 slow：正文前有 %d 行空行" % keepalives)
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Connection", "close")
        self.end_headers()
        per_gap = max(0.0, slow_delay) / max(1, keepalives)
        for _ in range(keepalives):
            if not interruptible_sleep(self.state.stop_event, per_gap):
                self.close_connection = True
                return
            self.wfile.write(b"\n")
            self.wfile.flush()
        self.wfile.write(payload)
        self.wfile.flush()
        self.close_connection = True

    def _send_stream(
        self,
        body: dict,
        comp: dict,
        model: str,
        scenario: str,
        slow: bool,
        slow_delay: float,
        keepalives: int,
    ) -> None:
        self._note(200, "场景 slow：data 前后夹 `: keep-alive` 注释" if slow else "")
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        per_gap = max(0.0, slow_delay) / max(1, keepalives) if slow else 0.0

        if slow:
            # D14：等待期间流式请求会持续返回 SSE 注释 `: keep-alive`。
            for _ in range(keepalives):
                if not interruptible_sleep(self.state.stop_event, per_gap):
                    self.close_connection = True
                    return
                self.wfile.write(b": keep-alive\n\n")
                self.wfile.flush()

        # 现行文档：`usage` 随**最后一个带 finish_reason 的分片**返回，
        # 没有独立的 `choices: []` usage 块。
        options = body.get("stream_options")
        usage = None
        if isinstance(options, dict) and options.get("include_usage"):
            usage = completion_envelope(body, comp, model, scenario)["usage"]

        first = True
        for chunk in iter_stream_chunks(body, comp, model, scenario, usage):
            if slow and not first:
                self.wfile.write(b": keep-alive\n\n")
            first = False
            payload = json.dumps(chunk, ensure_ascii=False).encode("utf-8")
            self.wfile.write(b"data: " + payload + b"\n\n")
            self.wfile.flush()

        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()
        self.close_connection = True


class FakeLLMServer(OfflineThreadingHTTPServer):
    def __init__(self, address: tuple[str, int], state: FakeLLMState) -> None:
        self.state = state
        super().__init__(address, FakeLLMHandler)


def start_fake_server(
    port: int = 0, host: str = "127.0.0.1", state: Optional[FakeLLMState] = None, **kwargs: Any
) -> tuple[FakeLLMServer, threading.Thread]:
    """在后台线程里起假服务，返回 (server, thread)。"""
    state = state or FakeLLMState(**kwargs)
    server = FakeLLMServer((host, port), state)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    thread.start()
    return server, thread


def stop_server(server: Any, thread: Optional[threading.Thread] = None) -> None:
    """关掉服务，并唤醒所有正在 sleep 的处理线程。"""
    state = getattr(server, "state", None)
    if state is not None and hasattr(state, "stop_event"):
        state.stop_event.set()
    try:
        server.shutdown()
    except Exception:  # pragma: no cover - 关服时的兜底
        pass
    try:
        server.server_close()
    except Exception:  # pragma: no cover
        pass
    if thread is not None:
        thread.join(timeout=5.0)


def server_origin(server: Any, host: str = "127.0.0.1") -> str:
    return "http://%s:%d" % (host, server.server_address[1])


def fake_base_url(server: FakeLLMServer, host: str = "127.0.0.1") -> str:
    """候选人要用的 `LLM_BASE_URL`：带路径前缀，不带 `/chat/completions`。"""
    return server_origin(server, host) + server.state.prefix


# ======================================================================================
# 代理
# ======================================================================================

HOP_BY_HOP = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
        "host",
        "content-length",
        # 不转发 Accept-Encoding，上游就不会压缩，正文才能逐字节透传。
        "accept-encoding",
    }
)


class JsonlLogger:
    """一行一个 JSON 对象的追加日志。"""

    def __init__(self, path: Optional[str]) -> None:
        self.path = path
        self._lock = threading.Lock()
        self._fh = open(path, "a", encoding="utf-8") if path else None

    def write(self, obj: Any) -> None:
        if self._fh is None:
            return
        line = json.dumps(obj, ensure_ascii=False, default=str)
        with self._lock:
            self._fh.write(line + "\n")
            self._fh.flush()

    def close(self) -> None:
        with self._lock:
            if self._fh is not None:
                self._fh.close()
                self._fh = None


def reassemble_stream(events: list[dict]) -> dict:
    """把 SSE 分片拼回完整的 content / reasoning_content / tool_calls。"""
    content: list[str] = []
    reasoning: list[str] = []
    tool_calls: dict[int, dict] = {}
    finish_reason = None
    usage = None
    first_content_ms = None
    for event in events:
        chunk = event.get("data")
        if not isinstance(chunk, dict):
            continue
        if isinstance(chunk.get("usage"), dict):
            usage = chunk["usage"]
        choices = chunk.get("choices")
        if not isinstance(choices, list):
            continue
        for choice in choices:
            if not isinstance(choice, dict):
                continue
            if choice.get("finish_reason"):
                finish_reason = choice["finish_reason"]
            delta = choice.get("delta")
            if not isinstance(delta, dict):
                continue
            if isinstance(delta.get("content"), str) and delta["content"]:
                if first_content_ms is None:
                    first_content_ms = event.get("offset_ms")
                content.append(delta["content"])
            if isinstance(delta.get("reasoning_content"), str):
                reasoning.append(delta["reasoning_content"])
            raw_calls = delta.get("tool_calls")
            if isinstance(raw_calls, list):
                for raw in raw_calls:
                    if not isinstance(raw, dict):
                        continue
                    index = int_or_none(raw.get("index")) or 0
                    slot = tool_calls.setdefault(
                        index,
                        {
                            "id": None,
                            "type": "function",
                            "function": {"name": None, "arguments": ""},
                        },
                    )
                    if raw.get("id"):
                        slot["id"] = raw["id"]
                    if raw.get("type"):
                        slot["type"] = raw["type"]
                    fn = raw.get("function")
                    if isinstance(fn, dict):
                        if fn.get("name"):
                            slot["function"]["name"] = fn["name"]
                        if isinstance(fn.get("arguments"), str):
                            slot["function"]["arguments"] += fn["arguments"]
    return {
        "chunks": len(events),
        "content": "".join(content),
        "reasoning_content": "".join(reasoning),
        "tool_calls": [tool_calls[k] for k in sorted(tool_calls)],
        "finish_reason": finish_reason,
        "usage": usage,
        "time_to_first_content_ms": first_content_ms,
    }


@dataclass
class ProxyConfig:
    upstream: str
    logger: JsonlLogger
    prefix: str = DEFAULT_PREFIX
    inject_thinking: Optional[str] = None
    timeout: float = 300.0
    verbose: bool = False


class ProxyHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "llm-gateway-proxy/" + VERSION
    sys_version = ""

    @property
    def cfg(self) -> ProxyConfig:
        return self.server.cfg  # type: ignore[attr-defined]

    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003
        if self.cfg.verbose:
            sys.stderr.write("[proxy] %s %s\n" % (self.address_string(), fmt % args))

    def handle_one_request(self) -> None:
        # 下游（被测服务）随时可能超时挂断，不要因此喷 traceback。
        try:
            super().handle_one_request()
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True

    def do_GET(self) -> None:  # noqa: N802
        self._proxy()

    def do_POST(self) -> None:  # noqa: N802
        self._proxy()

    def do_PUT(self) -> None:  # noqa: N802
        self._proxy()

    def do_PATCH(self) -> None:  # noqa: N802
        self._proxy()

    def do_DELETE(self) -> None:  # noqa: N802
        self._proxy()

    def _read_body(self) -> bytes:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            length = 0
        if length <= 0:
            return b""
        return self.rfile.read(length)

    def _send_json(self, status: int, obj: Any) -> None:
        payload = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _proxy(self) -> None:
        cfg = self.cfg
        parsed = urllib.parse.urlparse(self.path)
        path, query = parsed.path, parsed.query
        raw = self._read_body()
        body, body_text = parse_json_bytes(raw)

        prefix = cfg.prefix
        if prefix and not (path == prefix or path.startswith(prefix + "/")):
            record = {
                "ts": now_iso(),
                "method": self.command,
                "path": path,
                "error": "路径不在前缀 %s 之内，没有转发" % prefix,
                "response_status": 404,
            }
            cfg.logger.write(record)
            self._send_json(
                404,
                {
                    "error": {
                        "message": "本代理只转发 %s/... 的请求；收到 %s。"
                        "请把 LLM_BASE_URL 设成代理打印的那一行，不要自己改写地址。"
                        % (prefix, path),
                        "type": "invalid_request_error",
                        "param": None,
                        "code": "not_found",
                    }
                },
            )
            return

        rest = path[len(prefix) :] if prefix else path
        target = cfg.upstream.rstrip("/") + rest
        if query:
            target += "?" + query

        injected = None
        if (
            cfg.inject_thinking is not None
            and rest.endswith("/chat/completions")
            and isinstance(body, dict)
            and "thinking" not in body
        ):
            # 只在请求自己没有设置时注入（D5：thinking 默认开启，disabled 可以关掉）。
            body = dict(body)
            body["thinking"] = {"type": cfg.inject_thinking}
            raw = json.dumps(body, ensure_ascii=False).encode("utf-8")
            injected = {"thinking": {"type": cfg.inject_thinking}}

        headers = {}
        for key, value in self.headers.items():
            if key.lower() in HOP_BY_HOP:
                continue
            headers[key] = value
        auth_raw = self.headers.get("Authorization")

        record: dict[str, Any] = {
            "ts": now_iso(),
            "method": self.command,
            "path": path,
            "upstream_url": target,
            # Authorization 照常转发给上游，但日志里只留长度。
            "authorization_length": len(auth_raw) if auth_raw is not None else None,
            "request_body": body if body is not None else (truncate(body_text) or None),
            "injected": injected,
        }
        started = time.monotonic()
        request = urllib.request.Request(
            target, data=raw if raw else None, headers=headers, method=self.command
        )
        try:
            response = urllib.request.urlopen(request, timeout=cfg.timeout)
        except urllib.error.HTTPError as exc:
            response = exc  # HTTPError 本身就是一个可读的响应对象
        except Exception as exc:
            record["response_status"] = 502
            record["error"] = "%s: %s" % (type(exc).__name__, exc)
            record["latency_ms"] = round((time.monotonic() - started) * 1000, 2)
            cfg.logger.write(record)
            self._send_json(502, {"error": {"message": record["error"], "type": "proxy_error"}})
            return

        try:
            content_type = response.headers.get("Content-Type", "") or ""
            record["response_status"] = int(response.status)
            if "text/event-stream" in content_type.lower():
                self._pass_through_stream(response, record, started)
            else:
                self._pass_through_body(response, record, started)
        finally:
            try:
                response.close()
            except Exception:  # pragma: no cover
                pass
            cfg.logger.write(record)

    def _forward_headers(self, response: Any) -> None:
        self.send_response(int(response.status))
        for key, value in response.headers.items():
            if key.lower() in HOP_BY_HOP or key.lower() == "content-length":
                continue
            self.send_header(key, value)

    def _pass_through_body(self, response: Any, record: dict, started: float) -> None:
        raw = response.read()
        self._forward_headers(response)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)
        body, text = parse_json_bytes(raw)
        record["response_body"] = body if body is not None else truncate(text)
        record["latency_ms"] = round((time.monotonic() - started) * 1000, 2)
        if isinstance(body, dict) and isinstance(body.get("usage"), dict):
            record["usage"] = body["usage"]

    def _pass_through_stream(self, response: Any, record: dict, started: float) -> None:
        self._forward_headers(response)
        self.send_header("Connection", "close")
        self.end_headers()
        events: list[dict] = []
        comments = 0
        while True:
            line = response.readline()
            if not line:
                break
            # 逐字节透传，包括空行和 `: keep-alive` 注释。
            self.wfile.write(line)
            self.wfile.flush()
            stripped = line.strip()
            if stripped.startswith(b":"):
                comments += 1
                continue
            if stripped.startswith(b"data:"):
                payload = stripped[len(b"data:") :].strip()
                if payload and payload != b"[DONE]":
                    data, _ = parse_json_bytes(payload)
                    events.append(
                        {"offset_ms": round((time.monotonic() - started) * 1000, 2), "data": data}
                    )
        self.close_connection = True
        summary = reassemble_stream(events)
        summary["keepalive_comments"] = comments
        record["stream"] = summary
        record["latency_ms"] = round((time.monotonic() - started) * 1000, 2)
        if summary.get("usage"):
            record["usage"] = summary["usage"]


class ProxyServer(OfflineThreadingHTTPServer):
    def __init__(self, address: tuple[str, int], cfg: ProxyConfig) -> None:
        self.cfg = cfg
        super().__init__(address, ProxyHandler)


def start_proxy_server(
    upstream: str,
    port: int = 0,
    host: str = "127.0.0.1",
    log_path: Optional[str] = None,
    prefix: str = DEFAULT_PREFIX,
    inject_thinking: Optional[str] = None,
    timeout: float = 300.0,
    verbose: bool = False,
) -> tuple[ProxyServer, threading.Thread]:
    cfg = ProxyConfig(
        upstream=upstream,
        logger=JsonlLogger(log_path),
        prefix=normalise_prefix(prefix),
        inject_thinking=inject_thinking,
        timeout=timeout,
        verbose=verbose,
    )
    server = ProxyServer((host, port), cfg)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    thread.start()
    return server, thread


def stop_proxy_server(server: ProxyServer, thread: Optional[threading.Thread] = None) -> None:
    try:
        server.shutdown()
    except Exception:  # pragma: no cover
        pass
    try:
        server.server_close()
    except Exception:  # pragma: no cover
        pass
    try:
        server.cfg.logger.close()
    except Exception:  # pragma: no cover
        pass
    if thread is not None:
        thread.join(timeout=5.0)


def proxy_base_url(server: ProxyServer, host: str = "127.0.0.1") -> str:
    return server_origin(server, host) + server.cfg.prefix


# ======================================================================================
# 预检
# ======================================================================================


def _is_timeout(exc: BaseException) -> bool:
    """这个异常是不是“等到我们自己的超时都没等来响应”。

    连接被拒绝、域名解析不了、连接被重置都**不是**超时：那种情况下服务根本没有在跑，
    不能说它“超时”。区分这两者是 P11 不说假话的前提。
    """
    seen = set()
    while exc is not None and id(exc) not in seen:
        seen.add(id(exc))
        if isinstance(exc, TimeoutError):  # Python 3.10 起 socket.timeout 就是它的别名
            return True
        reason = getattr(exc, "reason", None)
        if isinstance(reason, BaseException):
            exc = reason
            continue
        exc = exc.__cause__ or exc.__context__
    return False


@dataclass
class HttpResult:
    url: str
    status: Optional[int]
    body: Any
    body_text: str
    elapsed: float
    error: Optional[str]
    #: 请求是被我们自己的超时掐断的（服务活着但没在时限内回），而不是压根连不上。
    timed_out: bool = False

    @property
    def ok_json(self) -> bool:
        return self.status == 200 and isinstance(self.body, dict)

    @property
    def has_timing_sample(self) -> bool:
        """这一次问答有没有给出可用的耗时样本。

        拿到了响应（无论什么状态码）算；被我们自己的超时掐断也算（它确实跑过头了）；
        连不上服务则不算——那是 P8 与 P12 的事，不是耗时问题。
        """
        return self.status is not None or self.timed_out


def http_json(
    url: str, payload: Optional[dict] = None, timeout: float = 30.0, method: Optional[str] = None
) -> HttpResult:
    """发一个 HTTP 请求，永远不抛异常：任何问题都变成 HttpResult.error。"""
    data = None
    headers = {"Accept": "application/json"}
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json; charset=utf-8"
    request = urllib.request.Request(
        url, data=data, headers=headers, method=method or ("POST" if data else "GET")
    )
    started = time.monotonic()
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
            body, text = parse_json_bytes(raw)
            return HttpResult(
                url, int(response.status), body, text, time.monotonic() - started, None
            )
    except urllib.error.HTTPError as exc:
        try:
            raw = exc.read()
        except Exception:  # pragma: no cover
            raw = b""
        finally:
            exc.close()
        body, text = parse_json_bytes(raw)
        return HttpResult(url, int(exc.code), body, text, time.monotonic() - started, None)
    except Exception as exc:
        return HttpResult(
            url,
            None,
            None,
            "",
            time.monotonic() - started,
            "%s: %s" % (type(exc).__name__, exc),
            timed_out=_is_timeout(exc),
        )


@dataclass
class ChatAttempt:
    scenario: str
    question: str
    session_id: str
    result: HttpResult


@dataclass
class ScenarioRun:
    scenario: str
    attempts: list[ChatAttempt] = field(default_factory=list)
    llm_requests: list[dict] = field(default_factory=list)

    @property
    def answered(self) -> bool:
        """这个场景下，服务有没有给出至少一次“不是 refusal”的 200 回答。"""
        for attempt in self.attempts:
            body = attempt.result.body
            if attempt.result.status == 200 and isinstance(body, dict):
                if body.get("answer_type") not in ("refusal", None):
                    return True
        return False


@dataclass
class CheckResult:
    id: str
    title: str
    status: str
    reason: str
    evidence: Any = None

    def to_json(self) -> dict:
        return {
            "id": self.id,
            "title": self.title,
            "status": self.status,
            "reason": self.reason,
            "evidence": self.evidence,
        }


@dataclass
class PreflightResult:
    service_url: str
    env: dict
    checks: list[CheckResult]
    scenario_runs: list[ScenarioRun]
    startup_requests: list[dict]
    health: HttpResult
    marker: str
    report_md_path: Optional[str] = None
    report_json_path: Optional[str] = None

    @property
    def failed(self) -> list[CheckResult]:
        return [c for c in self.checks if c.status == FAIL]

    @property
    def skipped(self) -> list[CheckResult]:
        return [c for c in self.checks if c.status == SKIP]

    @property
    def passed(self) -> bool:
        return not self.failed

    def check(self, check_id: str) -> CheckResult:
        for item in self.checks:
            if item.id == check_id:
                return item
        raise KeyError(check_id)

    def status_of(self, check_id: str) -> str:
        return self.check(check_id).status


# -- 检查项 -----------------------------------------------------------------------------


def _chat_requests(requests: Iterable[dict], chat_path: str) -> list[dict]:
    return [r for r in requests if r.get("path") == chat_path and r.get("method") == "POST"]


def _body_of(request: dict) -> dict:
    body = request.get("body")
    return body if isinstance(body, dict) else {}


def _result(check_id: str, status: str, reason: str, evidence: Any = None) -> CheckResult:
    return CheckResult(check_id, CHECK_TITLES[check_id], status, reason, evidence)


def _no_traffic(check_id: str) -> CheckResult:
    return _result(check_id, SKIP, "假模型一次请求都没收到，本项没有素材可查，先看 P1。", None)


def check_p1(chat: list[dict], runs: list[ScenarioRun], base_url: str, chat_path: str) -> CheckResult:
    per_scenario = {run.scenario: len(_chat_requests(run.llm_requests, chat_path)) for run in runs}
    evidence = {
        "expected_base_url": base_url,
        "expected_path": chat_path,
        "chat_completions_requests": len(chat),
        "per_scenario": per_scenario,
    }
    if chat:
        return _result(
            "P1", PASS, "共观察到 %d 次 POST %s。" % (len(chat), chat_path), evidence
        )
    return _result(
        "P1",
        FAIL,
        "假模型一次请求都没收到：服务没有按注入的 LLM_BASE_URL 发请求，"
        "或者还停在 mock 模式，或者没有用新环境变量重启。",
        evidence,
    )


def check_p2(chat: list[dict], expected_model: str) -> CheckResult:
    if not chat:
        return _no_traffic("P2")
    seen: dict[str, int] = {}
    for request in chat:
        value = _body_of(request).get("model")
        key = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
        seen[key] = seen.get(key, 0) + 1
    wrong = {k: v for k, v in seen.items() if k != expected_model}
    evidence = {"expected": expected_model, "seen": seen}
    if not wrong:
        return _result("P2", PASS, "全部请求都用了 %s。" % expected_model, evidence)
    return _result(
        "P2",
        FAIL,
        "请求里出现了注入值以外的模型名：%s；模型名必须来自 LLM_MODEL，不能写死。"
        % "、".join(sorted(wrong)),
        evidence,
    )


def check_p3(chat: list[dict]) -> CheckResult:
    if not chat:
        return _no_traffic("P3")
    missing, wrong_scheme, wrong_key = [], [], []
    for request in chat:
        auth = request.get("authorization") or {}
        if not auth.get("present"):
            missing.append(request.get("scenario"))
        elif (auth.get("scheme") or "").lower() != "bearer":
            wrong_scheme.append(auth.get("scheme"))
        elif not auth.get("matches_expected"):
            wrong_key.append(request.get("scenario"))
    evidence = {
        "requests": len(chat),
        "missing_authorization_in": sorted({x for x in missing if x}),
        "non_bearer_schemes": sorted({x for x in wrong_scheme if x}),
        "key_mismatch_in": sorted({x for x in wrong_key if x}),
    }
    if not missing and not wrong_scheme and not wrong_key:
        return _result("P3", PASS, "全部请求都带了正确的 Bearer Key。", evidence)
    parts = []
    if missing:
        parts.append("%d 次请求没有 Authorization 头" % len(missing))
    if wrong_scheme:
        parts.append("认证方式不是 Bearer")
    if wrong_key:
        parts.append("Key 不等于注入的 LLM_API_KEY")
    return _result(
        "P3",
        FAIL,
        "；".join(parts) + "。Key 必须原样读自 LLM_API_KEY，并以 `Authorization: Bearer <key>` 发出。",
        evidence,
    )


def check_p4(chat: list[dict]) -> CheckResult:
    if not chat:
        return _no_traffic("P4")
    extras: dict[str, dict] = {}
    for request in chat:
        for key in _body_of(request):
            if key in DOCUMENTED_PARAMS:
                continue
            entry = extras.setdefault(key, {"first_scenario": request.get("scenario"), "count": 0})
            entry["count"] += 1
    evidence = {"documented": sorted(DOCUMENTED_PARAMS), "extra_params": extras}
    if not extras:
        return _result("P4", PASS, "只出现了 DeepSeek 文档列出的顶层参数。", evidence)
    return _result(
        "P4",
        FAIL,
        "出现了文档之外的顶层参数：%s；文档没有的参数（例如 parallel_tool_calls、seed、n）不要用。"
        % "、".join(sorted(extras)),
        evidence,
    )


def check_p5(chat: list[dict]) -> CheckResult:
    if not chat:
        return _no_traffic("P5")
    offenders = []
    values = []
    for request in chat:
        raw = _body_of(request).get("max_tokens")
        if raw is None:
            continue
        values.append(raw)
        value = int_or_none(raw)
        if value is None or value < MIN_MAX_TOKENS:
            offenders.append({"scenario": request.get("scenario"), "max_tokens": raw})
    evidence = {"min_required": MIN_MAX_TOKENS, "values_seen": sorted(set(map(str, values))), "offenders": offenders}
    if not offenders:
        if not values:
            return _result("P5", PASS, "所有请求都没有设置 max_tokens，符合规范。", evidence)
        return _result("P5", PASS, "max_tokens 都不小于 %d。" % MIN_MAX_TOKENS, evidence)
    return _result(
        "P5",
        FAIL,
        "有 %d 次请求把 max_tokens 设成了 %s；思考也占输出额度，设小了额度会被思考吃光，"
        "回来的就是 finish_reason=length 加一个空正文。"
        % (len(offenders), "、".join(sorted({str(o["max_tokens"]) for o in offenders}))),
        evidence,
    )


def check_p6(all_requests: list[dict], chat_path: str) -> CheckResult:
    bad = [
        {
            "method": r.get("method"),
            "path": r.get("path"),
            "scenario": r.get("scenario"),
            "status": r.get("response_status"),
        }
        for r in all_requests
        if not (r.get("path") == chat_path and r.get("method") == "POST")
    ]
    evidence = {"allowed": "POST " + chat_path, "requests_seen": len(all_requests), "other_paths": bad}
    if not all_requests:
        # 一个请求都没有时说“只访问了 X”是说假话。
        return _no_traffic("P6")
    if not bad:
        return _result("P6", PASS, "只访问了 POST %s，没有碰任何别的路径。" % chat_path, evidence)
    listed = sorted({"%s %s" % (b["method"], b["path"]) for b in bad})
    return _result(
        "P6",
        FAIL,
        "访问了 %s 之外的路径：%s。"
        "在 OpenAI 兼容这条路线上只允许这一个接口：地址要原样拼接，不要自己补 /v1，"
        "不要截掉路径，也不要在启动时查余额或列模型。"
        "如果你本来走的就是 Anthropic 格式接口或 Responses API，那是另一条路线，本身不算错，"
        "只是预检查不了它，请改在 LLM_SETUP.md 第 7 节给等价证据。" % (chat_path, "、".join(listed)),
        evidence,
    )


def check_p7(chat: list[dict]) -> CheckResult:
    if not chat:
        return _no_traffic("P7")
    problems: list[str] = []
    saw_tools = False
    announced_total = 0
    returned_total = 0
    for request in chat:
        scenario = request.get("scenario")
        body = _body_of(request)
        tools = body.get("tools")
        if tools is not None:
            saw_tools = True
            if not isinstance(tools, list) or not tools:
                problems.append("[%s] tools 不是非空数组" % scenario)
            else:
                for index, tool in enumerate(tools):
                    prefix = "[%s] tools[%d]" % (scenario, index)
                    if not isinstance(tool, dict):
                        problems.append(prefix + " 不是对象")
                        continue
                    if tool.get("type") != "function":
                        problems.append(prefix + '.type 不是 "function"')
                    fn = tool.get("function")
                    if not isinstance(fn, dict):
                        problems.append(prefix + ".function 不是对象")
                        continue
                    if not isinstance(fn.get("name"), str) or not fn.get("name"):
                        problems.append(prefix + ".function.name 缺失或为空")
                    # 官方文档允许**省略** `parameters` 来表示无参数函数，
                    # 所以只有“写了但写错”才算问题，没写不算。
                    if "parameters" in fn:
                        params = fn["parameters"]
                        if not isinstance(params, dict):
                            problems.append(
                                prefix + ".function.parameters 写了，但不是 JSON Schema 对象；"
                                "无参数函数请直接省略这个字段"
                            )
                        else:
                            if "type" in params and params["type"] != "object":
                                problems.append(
                                    prefix + '.function.parameters.type 只能是 "object"'
                                )
                            if "properties" in params and not isinstance(
                                params["properties"], dict
                            ):
                                problems.append(
                                    prefix + ".function.parameters.properties 不是对象"
                                )
        messages = body.get("messages")
        if not isinstance(messages, list):
            continue
        announced: dict[str, int] = {}
        returned: set[str] = set()
        for index, message in enumerate(messages):
            if not isinstance(message, dict):
                continue
            if message.get("role") == "assistant" and isinstance(message.get("tool_calls"), list):
                for call in message["tool_calls"]:
                    if isinstance(call, dict) and isinstance(call.get("id"), str):
                        announced.setdefault(call["id"], index)
            if message.get("role") == "tool":
                call_id = message.get("tool_call_id")
                if not isinstance(call_id, str) or not call_id:
                    problems.append("[%s] role=tool 的消息没有 tool_call_id" % scenario)
                elif call_id not in announced:
                    problems.append(
                        "[%s] role=tool 的 tool_call_id=%s 在前面的 assistant.tool_calls 里找不到"
                        % (scenario, call_id)
                    )
                else:
                    returned.add(call_id)
        announced_total += len(announced)
        returned_total += len(returned)
        for call_id, index in announced.items():
            if index < len(messages) - 1 and call_id not in returned:
                problems.append(
                    '[%s] 工具调用 %s 的结果没有以 role="tool" + tool_call_id 回传；'
                    "一条 assistant 消息可能带多个工具调用，每一个都要回传" % (scenario, call_id)
                )
    evidence = {
        "declared_tools": saw_tools,
        "announced_tool_calls": announced_total,
        "returned_tool_results": returned_total,
        "problems": problems[:50],
    }
    if not saw_tools:
        return _result(
            "P7", SKIP, "服务没有声明 tools，本项无从检查；取数走 SQL 是允许的。", evidence
        )
    if problems:
        return _result("P7", FAIL, "工具协议有 %d 处问题，例如：%s。" % (len(problems), problems[0]), evidence)
    return _result("P7", PASS, "工具定义规范，%d 个工具调用的结果都正确回传了。" % returned_total, evidence)


def _contract_problems(body: Any) -> list[str]:
    problems: list[str] = []
    if not isinstance(body, dict):
        return ["响应不是 JSON 对象"]
    if not isinstance(body.get("answer"), str):
        problems.append("answer 不是字符串")
    answer_type = body.get("answer_type")
    if answer_type not in ANSWER_TYPES:
        problems.append("answer_type=%r 不在五选一里" % (answer_type,))
    citations = body.get("citations")
    if not isinstance(citations, list):
        problems.append("citations 不是数组")
    else:
        for index, item in enumerate(citations):
            if not isinstance(item, dict):
                problems.append("citations[%d] 不是对象" % index)
            elif not isinstance(item.get("doc_id"), str) or not isinstance(item.get("quote"), str):
                problems.append("citations[%d] 缺少字符串的 doc_id 或 quote" % index)
    evidence_rows = body.get("data_evidence")
    if not isinstance(evidence_rows, list):
        problems.append("data_evidence 不是数组")
    else:
        for index, item in enumerate(evidence_rows):
            if not isinstance(item, dict):
                problems.append("data_evidence[%d] 不是对象" % index)
                continue
            if "result" not in item:
                problems.append("data_evidence[%d] 缺少 result" % index)
            if "tool" not in item and "sql" not in item:
                problems.append("data_evidence[%d] 既没有 tool 也没有 sql" % index)
    if not isinstance(body.get("trace_id"), str):
        problems.append("trace_id 不是字符串")
    return problems


def check_p8(runs: list[ScenarioRun]) -> CheckResult:
    failures = []
    total = 0
    for run in runs:
        for attempt in run.attempts:
            total += 1
            result = attempt.result
            if result.error is not None:
                failures.append(
                    {
                        "scenario": run.scenario,
                        "question": attempt.question,
                        "problem": "请求失败：%s" % result.error,
                    }
                )
                continue
            if result.status != 200:
                failures.append(
                    {
                        "scenario": run.scenario,
                        "question": attempt.question,
                        "problem": "HTTP %s（契约要求任何内部错误都返回 200 + refusal）" % result.status,
                        "body": truncate(result.body_text, 500),
                    }
                )
                continue
            problems = _contract_problems(result.body)
            if problems:
                failures.append(
                    {
                        "scenario": run.scenario,
                        "question": attempt.question,
                        "problem": "；".join(problems),
                        "body": truncate(result.body_text, 500),
                    }
                )
    evidence = {"attempts": total, "failures": failures}
    if not failures:
        return _result("P8", PASS, "%d 次问答全部返回 200 和字段完整的 JSON。" % total, evidence)
    return _result(
        "P8",
        FAIL,
        "有 %d 次问答不合契约，例如 [%s] %s。"
        % (len(failures), failures[0]["scenario"], failures[0]["problem"]),
        evidence,
    )


#: SQL 注释，验证证据真伪之前先剥掉，免得 `SELECT 0 /* FROM orders */` 蒙混过关。
_SQL_COMMENT = re.compile(r"/\*.*?\*/|--[^\n]*", re.S)


def evidence_is_grounded(item: Any, declared_tools: set[str]) -> bool:
    """这一条 `data_evidence` 看起来是真查过，还是现编的。

    判为“现编”的两种情形：
      * `sql` 是一条没有 FROM 的常量查询（`SELECT 0` 这种），它不可能从数据里取到任何数字；
      * `tool` 名字压根不在这个服务向模型声明过的工具里（服务从未声明工具时无从对照，放过）。
    这里只能判断“明显是编的”，判不出“真的执行过”——真正的核对要看 `GET /api/trace/{trace_id}`。
    """
    if not isinstance(item, dict) or "result" not in item:
        return False
    tool = item.get("tool")
    if isinstance(tool, str) and tool:
        return not declared_tools or tool in declared_tools
    sql = item.get("sql")
    if isinstance(sql, str) and sql.strip():
        return re.search(r"\bfrom\b", _SQL_COMMENT.sub(" ", sql), re.I) is not None
    return False


def check_p9(
    runs: list[ScenarioRun],
    failing: set[str],
    failure_marker: str,
    declared_tools: set[str],
) -> CheckResult:
    problems = []
    checked = 0
    skipped = 0
    # 失败场景下，这些片段一个都不允许出现在对外字段里。
    forbidden = [t for t in (failure_marker, FAKE_ABORTED_TEXT, FAKE_FILTERED_TEXT) if t]
    for run in runs:
        for attempt in run.attempts:
            body = attempt.result.body
            if attempt.result.status != 200 or not isinstance(body, dict):
                skipped += 1
                continue
            answer = body.get("answer")
            answer_type = body.get("answer_type")
            if isinstance(answer, str) and not answer.strip():
                problems.append(
                    {
                        "scenario": run.scenario,
                        "question": attempt.question,
                        "problem": "answer 是空串；即使拒答也要写清原因",
                    }
                )
            if run.scenario not in failing:
                continue
            checked += 1
            citations = body.get("citations")
            data_evidence = body.get("data_evidence")

            # 1. 失败的那次模型输出，不许原封不动地转给用户。
            outward = json.dumps(
                {key: body.get(key) for key in ("answer", "citations", "data_evidence")},
                ensure_ascii=False,
            )
            leaked = [piece for piece in forbidden if piece in outward]
            if leaked:
                problems.append(
                    {
                        "scenario": run.scenario,
                        "question": attempt.question,
                        "problem": "把一次失败的模型输出（%s）直接当成了回答；"
                        "finish_reason 是 length/content_filter/insufficient_system_resource/aborted "
                        "的那几种，以及没有 tool_calls 而 content 为空的那种，都要按错误处理"
                        % "、".join(sorted(leaked)),
                        "answer": truncate(answer if isinstance(answer, str) else str(answer), 200),
                    }
                )

            # 2. 不是 refusal，就必须真的有据可查，而不是编一条证据充数。
            if answer_type in ("refusal", "clarify"):
                continue
            real_evidence = (
                [e for e in data_evidence if evidence_is_grounded(e, declared_tools)]
                if isinstance(data_evidence, list)
                else []
            )
            real_citations = citations if isinstance(citations, list) else []
            if answer_type in ("data", "hybrid") and real_evidence:
                continue
            if answer_type == "doc" and real_citations:
                continue
            fabricated = (
                [e for e in data_evidence if not evidence_is_grounded(e, declared_tools)]
                if isinstance(data_evidence, list)
                else []
            )
            detail = (
                "模型不可用时返回了 answer_type=%r，但没有一条站得住的证据；应当是结构化 refusal"
                % (answer_type,)
            )
            if fabricated:
                detail += "。这些证据像是现编的：%s" % truncate(
                    json.dumps(fabricated, ensure_ascii=False), 240
                )
            problems.append(
                {
                    "scenario": run.scenario,
                    "question": attempt.question,
                    "problem": detail,
                    "answer": truncate(answer if isinstance(answer, str) else str(answer), 200),
                }
            )
    evidence = {
        "failing_scenarios": sorted(failing),
        "checked": checked,
        "skipped_invalid_responses": skipped,
        "failure_marker": failure_marker,
        "declared_tools": sorted(declared_tools),
        "problems": problems,
    }
    if not problems and checked == 0:
        # 一次“模型不可用”的有效回答都没拿到，就没有资格说它做对了。
        return _result(
            "P9",
            SKIP,
            "模型不可用的那些场景，一次合法的 200 响应都没拿到，无从判断拒答是否规范，先看 P8。",
            evidence,
        )
    if not problems:
        return _result(
            "P9",
            PASS,
            "模型不可用的场景下都给了结构化 refusal 或有据可查的回答，answer 从不是空串。",
            evidence,
        )
    return _result(
        "P9",
        FAIL,
        "有 %d 处不合规，例如 [%s] %s。"
        % (len(problems), problems[0]["scenario"], problems[0]["problem"]),
        evidence,
    )


def check_p10(runs: list[ScenarioRun], marker: str, chat: list[dict]) -> CheckResult:
    # 只有假模型真的把带标记的 reasoning_content 发出去过，这一项才有意义。
    # 请求被 401/4xx 挡下，或者服务关掉了思考模式，都不存在“可能泄漏的内容”，
    # 这时说“没有泄漏”是句空话——契约 §7.3 也明确要求这种情况显示“未检查”。
    marker_bearing = [
        r for r in chat if r.get("response_status") == 200 and thinking_enabled(_body_of(r))
    ]
    thinking_off = [r for r in chat if not thinking_enabled(_body_of(r))]
    leaks = []
    inspected = 0
    for run in runs:
        for attempt in run.attempts:
            body = attempt.result.body
            if not isinstance(body, dict):
                continue
            inspected += 1
            for field_name in ("answer", "citations", "data_evidence"):
                value = body.get(field_name)
                if value is None:
                    continue
                text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
                if marker in text:
                    leaks.append(
                        {
                            "scenario": run.scenario,
                            "field": field_name,
                            "question": attempt.question,
                            "excerpt": truncate(text, 300),
                        }
                    )
    evidence = {
        "marker": marker,
        "inspected_answers": inspected,
        "responses_carrying_the_marker": len(marker_bearing),
        "requests_with_thinking_disabled": len(thinking_off),
        "leaks": leaks,
    }
    if leaks:
        pass  # 真漏了就不用管下面那些“无从检查”的情形了。
    elif not marker_bearing:
        if thinking_off and len(thinking_off) == len(chat):
            return _result(
                "P10",
                SKIP,
                "服务关掉了思考模式（thinking.type=disabled），假模型没有产生过思考内容，"
                "没有可泄漏的东西；规范允许这样做，不扣分，但请在 README 里写明理由。",
                evidence,
            )
        return _result(
            "P10",
            SKIP,
            "假模型一次都没能把带标记的思考内容发出去（请求没到、或者都被挡下了），本项无从检查，先看 P1 和 P8。",
            evidence,
        )
    elif not inspected:
        return _result(
            "P10", SKIP, "一次合法的 JSON 响应都没拿到，没有可检查的对外字段，先看 P8。", evidence
        )
    if not leaks:
        return _result(
            "P10",
            PASS,
            "%d 次回答里，思考标记都没有出现在任何对外字段里。" % inspected,
            evidence,
        )
    return _result(
        "P10",
        FAIL,
        "思考内容漏进了 %s；只读 message.content，不要把 reasoning_content 拼进回答。"
        % "、".join(sorted({leak["field"] for leak in leaks})),
        evidence,
    )


def check_p11(runs: list[ScenarioRun], limit: float) -> CheckResult:
    """只拿“真的跑过”的问答算耗时。

    连不上服务的那些问答，耗时是 0.00 秒，把它们算成“超时”是说假话——
    那是 P8 与 P12 要报告的问题。
    """
    slowest = []
    offenders = []
    unreachable = []
    for run in runs:
        for attempt in run.attempts:
            result = attempt.result
            elapsed = round(result.elapsed, 2)
            if not result.has_timing_sample:
                unreachable.append(
                    {"scenario": run.scenario, "error": result.error, "seconds": elapsed}
                )
                continue
            slowest.append({"scenario": run.scenario, "seconds": elapsed})
            if result.timed_out or result.elapsed > limit:
                offenders.append(
                    {
                        "scenario": run.scenario,
                        "question": attempt.question,
                        "seconds": elapsed,
                        "cut_off_by_preflight": result.timed_out,
                        "error": result.error,
                    }
                )
    slowest.sort(key=lambda item: item["seconds"], reverse=True)
    evidence = {
        "limit_seconds": limit,
        "timed_attempts": len(slowest),
        "slowest": slowest[:5],
        "offenders": offenders,
        "unreachable_attempts": unreachable[:5],
    }
    if not slowest:
        return _result(
            "P11",
            SKIP,
            "没有一次问答拿到过响应，也没有一次是等到超时才断的，耗时无从谈起；"
            "服务没起来或者根本连不上，先看 P8 和 P12。",
            evidence,
        )
    if not offenders:
        note = "最慢的一次是 %.2f 秒，都在 %g 秒以内。" % (slowest[0]["seconds"], limit)
        if unreachable:
            note += "另有 %d 次问答连不上服务，那是 P8 的问题，不计入耗时。" % len(unreachable)
        return _result("P11", PASS, note, evidence)
    cut_off = sum(1 for o in offenders if o["cut_off_by_preflight"])
    detail = "其中 %d 次直到预检自己的超时都没等到响应。" % cut_off if cut_off else ""
    return _result(
        "P11",
        FAIL,
        "有 %d 次问答超过了 %g 秒（最慢是 %s 场景的 %.2f 秒）。%s"
        "`/api/chat` 有 %g 秒的总预算，到点必须返回；"
        "单次模型调用的超时取“120 秒”和“剩余预算”中较小的那个，预算用尽就返回结构化的 refusal。"
        % (len(offenders), limit, offenders[0]["scenario"], offenders[0]["seconds"], detail, limit),
        evidence,
    )


def check_p12(health: HttpResult) -> CheckResult:
    evidence = {
        "status": health.status,
        "error": health.error,
        "body": health.body if health.body is not None else truncate(health.body_text, 500),
    }
    if health.error is not None:
        return _result("P12", FAIL, "GET /api/health 请求失败：%s。" % health.error, evidence)
    if health.status != 200 or not isinstance(health.body, dict):
        return _result(
            "P12", FAIL, "GET /api/health 返回 HTTP %s 或者不是 JSON 对象。" % health.status, evidence
        )
    mode = health.body.get("llm_mode")
    if mode == "live":
        return _result("P12", PASS, "llm_mode = live。", evidence)
    return _result(
        "P12",
        FAIL,
        "llm_mode = %r，但三个环境变量都已注入；配置必须只从环境变量读。" % (mode,),
        evidence,
    )


def check_p13(chat: list[dict]) -> CheckResult:
    if not chat:
        return _no_traffic("P13")
    rejected = [
        {
            "scenario": r.get("scenario"),
            "note": r.get("response_note"),
            "seq": r.get("seq"),
        }
        for r in chat
        if r.get("response_status") == 400
    ]
    # 有没有真的走到“带着我们发出去过的 assistant 消息再来一轮”。
    echoed = 0
    thinking_off = 0
    for request in chat:
        body = _body_of(request)
        if not thinking_enabled(body):
            thinking_off += 1
        messages = body.get("messages")
        if not isinstance(messages, list):
            continue
        for message in messages:
            if (
                isinstance(message, dict)
                and message.get("role") == "assistant"
                and isinstance(message.get("tool_calls"), list)
                and message.get("tool_calls")
                and isinstance(message.get("reasoning_content"), str)
                and message["reasoning_content"]
            ):
                echoed += 1
                break
    evidence = {
        "requests_rejected_with_400": rejected,
        "requests_echoing_reasoning": echoed,
        "requests_with_thinking_disabled": thinking_off,
    }
    if rejected:
        return _result(
            "P13",
            FAIL,
            "有 %d 次请求被假模型以 400 拒绝：%s。"
            "带 tools 的请求必须把收到的 assistant 消息整条追加进 messages，不要自己挑字段重组。"
            % (len(rejected), rejected[0]["note"]),
            evidence,
        )
    if echoed:
        return _result("P13", PASS, "%d 次多轮请求都原样回传了 reasoning_content。" % echoed, evidence)
    if thinking_off:
        return _result(
            "P13",
            SKIP,
            "服务关掉了思考模式（thinking.type=disabled），没有 reasoning_content 需要回传；"
            "规范允许这样做，但请在 README 里写明理由。",
            evidence,
        )
    return _result(
        "P13", SKIP, "没有观察到带工具的多轮请求，本项无从检查。", evidence
    )


def check_p14(runs: list[ScenarioRun]) -> CheckResult:
    by_name = {run.scenario: run for run in runs}
    slow = by_name.get("slow")
    normal = by_name.get("normal")
    evidence = {
        "normal_answered": bool(normal and normal.answered),
        "slow_answered": bool(slow and slow.answered),
        "slow_statuses": [a.result.status for a in slow.attempts] if slow else [],
        "slow_answer_types": [
            (a.result.body or {}).get("answer_type") if isinstance(a.result.body, dict) else None
            for a in (slow.attempts if slow else [])
        ],
    }
    if slow is None or normal is None:
        return _result("P14", SKIP, "没有同时跑 normal 与 slow 两个场景，本项无从对比。", evidence)
    if not normal.answered:
        return _result(
            "P14",
            SKIP,
            "normal 场景本身就没有拿到回答，无法判断保持连接是不是额外的问题，先修前面的检查。",
            evidence,
        )
    if slow.answered:
        return _result(
            "P14",
            PASS,
            "正文前的空行和 SSE 的 `: keep-alive` 注释都被正确跳过了，slow 场景照常给出回答。",
            evidence,
        )
    return _result(
        "P14",
        FAIL,
        "normal 场景能正常回答，slow 场景却答不出来：你的 HTTP 或 SSE 解析没有跳过"
        "正文前的空行和 `: keep-alive` 注释。服务繁忙时真实接口就是这样保持连接的。",
        evidence,
    )


def failing_scenarios(runs: list[ScenarioRun], chat_path: str) -> set[str]:
    """哪些场景算“模型不可用”，`/api/chat` 必须给结构化 refusal。"""
    present = {run.scenario for run in runs}
    failing = set(ALWAYS_FAILING_SCENARIOS) & present
    for run in runs:
        requests = _chat_requests(run.llm_requests, chat_path)
        if run.scenario == "thinking_starved":
            # 只有候选人真的把 max_tokens 设小了，这个场景才会咬人。
            for request in requests:
                value = int_or_none(_body_of(request).get("max_tokens"))
                if value is not None and value < THINKING_STARVED_THRESHOLD:
                    failing.add("thinking_starved")
                    break
        if run.scenario == "json_empty":
            # 只有候选人真的用了 JSON 模式，这个场景才会咬人（D10）。
            for request in requests:
                if wants_json_object(_body_of(request)):
                    failing.add("json_empty")
                    break
    return failing


def declared_tool_names(chat: list[dict]) -> set[str]:
    """服务真的向模型声明过的工具名。编造的 `data_evidence.tool` 靠它识破。"""
    names: set[str] = set()
    for request in chat:
        tools = _body_of(request).get("tools")
        if not isinstance(tools, list):
            continue
        for tool in tools:
            if not isinstance(tool, dict):
                continue
            fn = tool.get("function")
            if isinstance(fn, dict) and isinstance(fn.get("name"), str) and fn["name"]:
                names.add(fn["name"])
    return names


def evaluate_preflight(
    env: dict,
    health: HttpResult,
    startup_requests: list[dict],
    runs: list[ScenarioRun],
    marker: str,
    max_chat_seconds: float,
    chat_path: str,
    failure_marker: str = "",
) -> list[CheckResult]:
    all_requests: list[dict] = list(startup_requests)
    for run in runs:
        all_requests.extend(run.llm_requests)
    chat = _chat_requests(all_requests, chat_path)
    failing = failing_scenarios(runs, chat_path)
    declared = declared_tool_names(chat)
    return [
        check_p1(chat, runs, env["LLM_BASE_URL"], chat_path),
        check_p2(chat, env["LLM_MODEL"]),
        check_p3(chat),
        check_p4(chat),
        check_p5(chat),
        check_p6(all_requests, chat_path),
        check_p7(chat),
        check_p8(runs),
        check_p9(runs, failing, failure_marker, declared),
        check_p10(runs, marker, chat),
        check_p11(runs, max_chat_seconds),
        check_p12(health),
        check_p13(chat),
        check_p14(runs),
    ]


# -- 输出 -------------------------------------------------------------------------------


def render_check_table(checks: list[CheckResult]) -> str:
    rows = [("编号", "检查项", "结果", "说明")]
    for check in checks:
        rows.append((check.id, check.title, _STATUS_CN[check.status], check.reason))
    widths = [max(display_width(row[i]) for row in rows) for i in range(3)]
    lines = []
    for index, row in enumerate(rows):
        line = "  ".join(pad(row[i], widths[i]) for i in range(3)) + "  " + row[3]
        lines.append(line)
        if index == 0:
            lines.append("-" * min(120, display_width(line)))
    return "\n".join(lines)


def _answer_digest(result: HttpResult) -> str:
    if result.error is not None:
        return "（请求失败：%s）" % result.error
    if not isinstance(result.body, dict):
        return "（响应不是 JSON 对象）"
    answer = result.body.get("answer")
    if not isinstance(answer, str):
        return "（answer 不是字符串）"
    compact = " ".join(answer.split())
    return truncate(compact, 60) if compact else "（空串）"


SCENARIO_NOTES = (
    ("normal", "模型一切正常；带 tools 时先返回两个工具调用，再返回一个，最后才给正文。"),
    ("thinking_starved", "只有当你把 max_tokens 设得小于 %d 时才会咬人，此时正文为空、finish_reason 为 length。" % THINKING_STARVED_THRESHOLD),
    ("empty_content", "没有 tool_calls 而正文是空串，finish_reason 仍然是 stop，这种要按错误处理。"),
    ("json_empty", "只有当你用了 response_format=json_object 时才会咬人，文档说 JSON 模式偶尔会返回空内容。"),
    ("bad_tool_args", "工具调用的 arguments 是被截断的非法 JSON，必须处理解析失败。"),
    ("content_filter", "finish_reason 为 content_filter，正文是半截话、不是空串。"),
    ("insufficient_resource", "finish_reason 为 insufficient_system_resource，正文为空。"),
    ("aborted", "finish_reason 为 aborted，正文写到一半被掐断、不是空串，只看正文空不空的实现会漏掉它。"),
    ("http_401", "HTTP 401 认证失败。"),
    ("http_402", "HTTP 402 余额不足。"),
    ("http_422", "HTTP 422 参数错误。"),
    ("http_429", "HTTP 429 限速。"),
    ("http_500", "HTTP 500 服务器错误。"),
    ("http_503", "HTTP 503 服务器繁忙。"),
    ("slow", "服务繁忙时的保持连接：非流式在正文前发空行，流式发 `: keep-alive` 注释。"),
    ("hang", "收下连接却一直不回，你的单次模型调用必须有超时。"),
)


def split_sentences(text: str) -> list[str]:
    """按中文句号把一段说明拆成一句一行。

    `preflight_report.md` 要求一句一行；检查项的说明经常是两三句话，
    放在表格单元格里没关系（表格一行就是一行），放在正文里就得拆开。
    """
    parts: list[str] = []
    current = ""
    for ch in text:
        current += ch
        if ch == "。":
            if current.strip():
                parts.append(current.strip())
            current = ""
    if current.strip():
        parts.append(current.strip())
    return parts


def render_report_md(result: PreflightResult) -> str:
    total = len(result.checks)
    failed = result.failed
    skipped = result.skipped
    lines: list[str] = []
    lines.append("# 大模型接入预检报告")
    lines.append("")
    lines.append("生成时间：%s。" % now_iso())
    lines.append("被测服务：%s。" % result.service_url)
    lines.append("假模型地址：%s。" % result.env["LLM_BASE_URL"])
    lines.append("注入的模型名：%s。" % result.env["LLM_MODEL"])
    lines.append("工具版本：llm_gateway.py %s。" % VERSION)
    if failed:
        lines.append(
            "总体结论：**未通过**，%d 项检查里有 %d 项失败（%s）。"
            % (total, len(failed), "、".join(c.id for c in failed))
        )
    else:
        lines.append("总体结论：**没有失败项**，%d 项检查里 %d 项通过。" % (total, total - len(skipped)))
    if skipped:
        lines.append(
            "有 %d 项因为没有素材而未检查（%s），请看下面的逐项说明确认这是不是你想要的。"
            % (len(skipped), "、".join(c.id for c in skipped))
        )
    lines.append("")
    lines.append("## 检查结果一览")
    lines.append("")
    lines.append("| 编号 | 检查项 | 结果 | 说明 |")
    lines.append("|---|---|---|---|")
    for check in result.checks:
        lines.append(
            "| %s | %s | %s | %s |"
            % (check.id, check.title, _STATUS_CN[check.status], check.reason.replace("|", "\\|"))
        )
    lines.append("")
    lines.append("## 逐项证据")
    lines.append("")
    for check in result.checks:
        lines.append("### %s %s" % (check.id, check.title))
        lines.append("")
        lines.append("结果：%s。" % _STATUS_CN[check.status])
        reason_lines = split_sentences(check.reason)
        lines.append("说明：%s" % (reason_lines[0] if reason_lines else ""))
        lines.extend(reason_lines[1:])
        if check.evidence is not None:
            lines.append("")
            lines.append("```json")
            lines.append(json.dumps(check.evidence, ensure_ascii=False, indent=2, default=str))
            lines.append("```")
        lines.append("")
    lines.append("## 各场景明细")
    lines.append("")
    lines.append("| 场景 | 问题 | HTTP | 耗时（秒） | answer_type | answer 摘要 | 模型请求数 |")
    lines.append("|---|---|---|---|---|---|---|")
    chat_path = result.env.get("__chat_path", "")
    for run in result.scenario_runs:
        chat_count = len(_chat_requests(run.llm_requests, chat_path))
        for attempt in run.attempts:
            body = attempt.result.body if isinstance(attempt.result.body, dict) else {}
            lines.append(
                "| %s | %s | %s | %.2f | %s | %s | %d |"
                % (
                    run.scenario,
                    attempt.question.replace("|", "\\|"),
                    attempt.result.status if attempt.result.status is not None else "—",
                    attempt.result.elapsed,
                    body.get("answer_type", "—"),
                    _answer_digest(attempt.result).replace("|", "\\|"),
                    chat_count,
                )
            )
    lines.append("")
    lines.append("## 场景说明")
    lines.append("")
    for name, note in SCENARIO_NOTES:
        lines.append("%s：%s" % (name, note))
    lines.append("")
    lines.append("## 注意")
    lines.append("")
    lines.append("本机没有 DeepSeek Key，假模型的全部行为都来自官方文档，没有对照过真实接口。")
    lines.append("不回传 reasoning_content 时的 400、response_format 取值不合法时的 422，都是按文档推定的。")
    lines.append("P5 的 max_tokens ≥ 2048 和 P11 的 180 秒总预算是这份作业的规定，不是 DeepSeek 服务端的限制。")
    lines.append("走 OpenAI 兼容路线的，请把这份报告贴进 `LLM_SETUP.md` 第 7 节（自测结果）。")
    lines.append("")
    return "\n".join(lines)


def build_report_json(result: PreflightResult) -> dict:
    chat_path = result.env.get("__chat_path", "")
    env = {k: v for k, v in result.env.items() if not k.startswith("__")}
    return {
        "tool": "llm_gateway.py",
        "version": VERSION,
        "generated_at": now_iso(),
        "service_url": result.service_url,
        "env": env,
        "reasoning_marker": result.marker,
        "passed": result.passed,
        "failed_checks": [c.id for c in result.failed],
        "skipped_checks": [c.id for c in result.skipped],
        "checks": [c.to_json() for c in result.checks],
        "health": {
            "status": result.health.status,
            "error": result.health.error,
            "body": result.health.body,
        },
        "scenarios": [
            {
                "scenario": run.scenario,
                "llm_requests": len(_chat_requests(run.llm_requests, chat_path)),
                "attempts": [
                    {
                        "question": attempt.question,
                        "session_id": attempt.session_id,
                        "status": attempt.result.status,
                        "elapsed_seconds": round(attempt.result.elapsed, 3),
                        "error": attempt.result.error,
                        "body": attempt.result.body
                        if attempt.result.body is not None
                        else truncate(attempt.result.body_text, 1000),
                    }
                    for attempt in run.attempts
                ],
            }
            for run in result.scenario_runs
        ],
        "startup_requests": len(result.startup_requests),
    }


# -- 驱动 -------------------------------------------------------------------------------


def run_preflight(
    service_url: str,
    port: int = 0,
    host: str = "127.0.0.1",
    model: str = DEFAULT_FAKE_MODEL,
    api_key: str = DEFAULT_FAKE_API_KEY,
    prefix: str = DEFAULT_PREFIX,
    no_wait: bool = False,
    out_dir: str = ".",
    scenarios: Iterable[str] = SCENARIOS,
    slow_delay: float = DEFAULT_SLOW_DELAY,
    keepalives: int = DEFAULT_KEEPALIVES,
    hang_cap: float = DEFAULT_HANG_CAP,
    max_chat_seconds: float = DEFAULT_MAX_CHAT_SECONDS,
    chat_timeout: Optional[float] = None,
    health_timeout: float = 30.0,
    questions: Iterable[str] = DEFAULT_QUESTIONS,
    ready_hook: Optional[Callable[[dict], None]] = None,
    out_stream: Any = None,
    write_reports: bool = True,
) -> PreflightResult:
    """起假模型 → 打印环境变量 → 驱动 /api/chat → 逐项检查 → 写报告。"""
    out = out_stream if out_stream is not None else sys.stdout
    scenarios = list(scenarios)
    for scenario in scenarios:
        if scenario not in SCENARIOS:
            raise ValueError("未知场景：%s" % scenario)
    questions = list(questions)
    if chat_timeout is None:
        chat_timeout = max_chat_seconds + 10.0
    service_url = service_url.rstrip("/")

    def say(text: str = "") -> None:
        print(text, file=out)

    state = FakeLLMState(
        scenario="normal",
        model=model,
        api_key=api_key,
        prefix=prefix,
        slow_delay=slow_delay,
        keepalives=keepalives,
        hang_cap=hang_cap,
    )
    server, thread = start_fake_server(port=port, host=host, state=state)
    base_url = server_origin(server, host) + state.prefix
    env = {
        "LLM_BASE_URL": base_url,
        "LLM_API_KEY": api_key,
        "LLM_MODEL": model,
        "__chat_path": state.chat_path,
    }

    try:
        say("=" * 78)
        say("预检假模型已启动：%s" % base_url)
        say("它只提供 POST %s%s，其余任何路径都会返回 404 并被记下来。" % (base_url, "/chat/completions"))
        say("")
        say("请用下面三个环境变量重启你的服务（模型名是故意取的怪名字，写死模型名会被查出来）：")
        say("")
        for key in ("LLM_BASE_URL", "LLM_API_KEY", "LLM_MODEL"):
            say("  export %s=%s" % (key, env[key]))
        say("")
        say("或者直接在启动命令前加上：")
        say(
            "  %s <你的启动命令>"
            % " ".join("%s=%s" % (k, env[k]) for k in ("LLM_BASE_URL", "LLM_API_KEY", "LLM_MODEL"))
        )
        say("=" * 78)

        if ready_hook is not None:
            ready_hook({k: v for k, v in env.items() if not k.startswith("__")})

        if not no_wait:
            say("")
            say("重启完成后按回车继续……")
            try:
                input()
            except (EOFError, KeyboardInterrupt):
                say("（没有读到回车，直接继续）")

        say("")
        say("开始检查 %s ……" % service_url)

        health = http_json(service_url + "/api/health", timeout=health_timeout)
        startup_requests = state.take_requests()

        runs: list[ScenarioRun] = []
        for scenario in scenarios:
            state.set_scenario(scenario)
            run = ScenarioRun(scenario=scenario)
            for index, question in enumerate(questions):
                session_id = "preflight-%s-%d-%s" % (scenario, index, uuid.uuid4().hex[:8])
                result = http_json(
                    service_url + "/api/chat",
                    payload={"session_id": session_id, "question": question},
                    timeout=chat_timeout,
                )
                run.attempts.append(
                    ChatAttempt(
                        scenario=scenario,
                        question=question,
                        session_id=session_id,
                        result=result,
                    )
                )
                say(
                    "  [%s] %s → HTTP %s，%.2f 秒"
                    % (
                        scenario,
                        truncate(question, 20),
                        result.status if result.status is not None else "失败",
                        result.elapsed,
                    )
                )
            run.llm_requests = state.take_requests()
            runs.append(run)

        checks = evaluate_preflight(
            env,
            health,
            startup_requests,
            runs,
            state.marker,
            max_chat_seconds,
            state.chat_path,
            failure_marker=state.failure_marker,
        )
        result = PreflightResult(
            service_url=service_url,
            env=env,
            checks=checks,
            scenario_runs=runs,
            startup_requests=startup_requests,
            health=health,
            marker=state.marker,
        )

        say("")
        say(render_check_table(checks))
        say("")
        if result.passed:
            say("预检通过：在 OpenAI 兼容这条路线上，我们能原样接上你的服务。")
        else:
            say("预检未通过：%s 需要修。" % "、".join(check.id for check in result.failed))
        if result.skipped:
            say("未检查：%s（没有素材，见报告里的逐项说明）。" % "、".join(c.id for c in result.skipped))

        if write_reports:
            os.makedirs(out_dir, exist_ok=True)
            md_path = os.path.join(out_dir, "preflight_report.md")
            json_path = os.path.join(out_dir, "preflight_report.json")
            with open(md_path, "w", encoding="utf-8") as handle:
                handle.write(render_report_md(result))
            with open(json_path, "w", encoding="utf-8") as handle:
                json.dump(
                    build_report_json(result), handle, ensure_ascii=False, indent=2, default=str
                )
                handle.write("\n")
            result.report_md_path = md_path
            result.report_json_path = json_path
            say("报告已写入 %s 与 %s。" % (md_path, json_path))
            say("请把报告贴进 LLM_SETUP.md 第 7 节（自测结果）。")
        return result
    finally:
        stop_server(server, thread)


# ======================================================================================
# 命令行
# ======================================================================================


def _serve_until_interrupt(stop: Callable[[], None]) -> int:
    """前台等待 Ctrl-C（SIGINT）或 SIGTERM，两种都要把服务关干净。"""
    previous = None
    try:
        previous = signal.signal(signal.SIGTERM, _raise_keyboard_interrupt)
    except (ValueError, OSError):  # pragma: no cover - 非主线程时装不上
        previous = None
    try:
        while True:
            time.sleep(0.5)
    except KeyboardInterrupt:
        print("\n正在停止……")
    finally:
        if previous is not None:
            try:
                signal.signal(signal.SIGTERM, previous)
            except (ValueError, OSError):  # pragma: no cover
                pass
        stop()
    return 0


def _raise_keyboard_interrupt(signum: int, frame: Any) -> None:
    raise KeyboardInterrupt()


def cmd_fake(args: argparse.Namespace) -> int:
    state = FakeLLMState(
        scenario=args.scenario,
        model=args.model,
        api_key=args.api_key,
        prefix=args.prefix,
        slow_delay=args.slow_delay,
        keepalives=args.keepalives,
        hang_cap=args.hang_cap,
        verbose=args.verbose,
    )
    server, thread = start_fake_server(port=args.port, host=args.host, state=state)
    origin = server_origin(server, args.host)
    base = origin + state.prefix
    print("假 DeepSeek 服务已启动。")
    print("LLM_BASE_URL=%s" % base)
    print("LLM_API_KEY=%s" % args.api_key)
    print("LLM_MODEL=%s" % args.model)
    print("唯一可用的接口：POST %s/chat/completions（其余路径一律 404 并记录）。" % base)
    print("当前场景：%s" % args.scenario)
    print("可用场景：%s" % "、".join(SCENARIOS))
    print("思考标记：%s" % state.marker)
    print("切换场景：curl -sX POST %s%s/scenario -d '{\"scenario\":\"http_429\"}'" % (origin, CONTROL_PREFIX))
    print("查看收到的请求：curl -s %s%s/requests" % (origin, CONTROL_PREFIX))
    print("清空记录：curl -sX POST %s%s/reset" % (origin, CONTROL_PREFIX))
    print("按 Ctrl-C 停止。")
    return _serve_until_interrupt(lambda: stop_server(server, thread))


def cmd_preflight(args: argparse.Namespace) -> int:
    result = run_preflight(
        service_url=args.service_url,
        port=args.port,
        host=args.host,
        model=args.model,
        api_key=args.api_key,
        prefix=args.prefix,
        no_wait=args.no_wait,
        out_dir=args.out,
        scenarios=args.scenarios.split(",") if args.scenarios else SCENARIOS,
        slow_delay=args.slow_delay,
        keepalives=args.keepalives,
        hang_cap=args.hang_cap,
        max_chat_seconds=args.max_chat_seconds,
        chat_timeout=args.chat_timeout,
    )
    return 0 if result.passed else 1


def cmd_proxy(args: argparse.Namespace) -> int:
    server, thread = start_proxy_server(
        upstream=args.upstream,
        port=args.port,
        host=args.host,
        log_path=args.log,
        prefix=args.prefix,
        inject_thinking=args.inject_thinking,
        timeout=args.timeout,
        verbose=args.verbose,
    )
    base = proxy_base_url(server, args.host)
    print("代理已启动：%s  →  %s" % (base, args.upstream))
    print("把你的服务指向这一行，不要自己改写地址：")
    print("LLM_BASE_URL=%s" % base)
    print("流量日志：%s" % (args.log or "（未开启）"))
    if args.inject_thinking is not None:
        print("会为没有设置 thinking 的 chat 请求注入 thinking={\"type\":\"%s\"}。" % args.inject_thinking)
    print("按 Ctrl-C 停止。")
    return _serve_until_interrupt(lambda: stop_proxy_server(server, thread))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="llm_gateway.py",
        description="假 DeepSeek 服务、接入预检与流量代理（只依赖标准库）。",
    )
    parser.add_argument("--version", action="version", version="llm_gateway.py " + VERSION)
    subparsers = parser.add_subparsers(dest="command", required=True)

    fake = subparsers.add_parser("fake", help="起一个按 DeepSeek 文档行为模拟的假模型服务")
    fake.add_argument("--port", type=int, default=0, help="监听端口，默认 0 表示自动挑一个空闲端口")
    fake.add_argument("--host", default="127.0.0.1")
    fake.add_argument("--scenario", default="normal", choices=SCENARIOS)
    fake.add_argument("--model", default=DEFAULT_FAKE_MODEL)
    fake.add_argument("--api-key", default=DEFAULT_FAKE_API_KEY)
    fake.add_argument("--prefix", default=DEFAULT_PREFIX, help="路径前缀，默认 %s" % DEFAULT_PREFIX)
    fake.add_argument("--slow-delay", type=float, default=DEFAULT_SLOW_DELAY)
    fake.add_argument("--keepalives", type=int, default=DEFAULT_KEEPALIVES)
    fake.add_argument("--hang-cap", type=float, default=DEFAULT_HANG_CAP)
    fake.add_argument("--verbose", action="store_true")
    fake.set_defaults(func=cmd_fake)

    pre = subparsers.add_parser("preflight", help="驱动你的 /api/chat，逐项检查接入方式")
    pre.add_argument("--service-url", required=True, help="你的后端地址，例如 http://localhost:8000")
    pre.add_argument("--port", type=int, default=0, help="假模型端口，默认自动挑一个空闲端口")
    pre.add_argument("--host", default="127.0.0.1")
    pre.add_argument("--model", default=DEFAULT_FAKE_MODEL, help="注入的 LLM_MODEL")
    pre.add_argument("--api-key", default=DEFAULT_FAKE_API_KEY, help="注入的 LLM_API_KEY")
    pre.add_argument("--prefix", default=DEFAULT_PREFIX)
    pre.add_argument("--no-wait", action="store_true", help="不等回车，适合脚本里跑")
    pre.add_argument("--out", default=".", help="报告输出目录")
    pre.add_argument("--scenarios", default="", help="逗号分隔的场景子集，默认全部")
    pre.add_argument("--slow-delay", type=float, default=DEFAULT_SLOW_DELAY)
    pre.add_argument("--keepalives", type=int, default=DEFAULT_KEEPALIVES)
    pre.add_argument("--hang-cap", type=float, default=DEFAULT_HANG_CAP)
    pre.add_argument("--max-chat-seconds", type=float, default=DEFAULT_MAX_CHAT_SECONDS)
    pre.add_argument("--chat-timeout", type=float, default=DEFAULT_CHAT_TIMEOUT)
    pre.set_defaults(func=cmd_preflight)

    proxy = subparsers.add_parser("proxy", help="带路径前缀的反向代理，把全部流量记成 JSONL")
    proxy.add_argument("--upstream", required=True, help="真实模型地址，例如 https://api.deepseek.com")
    proxy.add_argument("--port", type=int, default=0, help="监听端口，默认自动挑一个空闲端口")
    proxy.add_argument("--host", default="127.0.0.1")
    proxy.add_argument("--log", default=DEFAULT_PROXY_LOG)
    proxy.add_argument("--prefix", default=DEFAULT_PREFIX)
    proxy.add_argument(
        "--inject-thinking",
        default=None,
        choices=["enabled", "disabled"],
        help="给没有设置 thinking 的 chat 请求注入该开关，默认不注入",
    )
    proxy.add_argument("--timeout", type=float, default=300.0)
    proxy.add_argument("--verbose", action="store_true")
    proxy.set_defaults(func=cmd_proxy)

    return parser


def main(argv: Optional[list[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
