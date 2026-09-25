"""Chat Completions 客户端，OpenAI 格式，目标是 DeepSeek 官方 API。

只 POST {LLM_BASE_URL}/chat/completions，地址原样拼接，不补 /v1。
配置只从 LLM_BASE_URL、LLM_API_KEY、LLM_MODEL 三个环境变量读。
空正文、异常 finish_reason、错误码、超时都抛成带原因的异常。
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any, Optional

import httpx

#: 契约 §7.3：思考也占输出额度，`max_tokens` 不设或不小于 2048。
MAX_TOKENS = 4096
#: D11：正常结束只有这两种。
GOOD_FINISH = ("stop", "tool_calls")
#: 这几类是暂时性的，值得重试一次。
RETRYABLE_STATUS = (429, 500, 503)
RETRYABLE_KINDS = ("empty_content", "insufficient_system_resource", "transport")


@dataclass
class LLMError(Exception):
    kind: str
    detail: str
    status: Optional[int] = None

    def __str__(self) -> str:  # pragma: no cover - 只用于日志
        return "%s: %s" % (self.kind, self.detail)

    @property
    def retryable(self) -> bool:
        return self.status in RETRYABLE_STATUS or self.kind in RETRYABLE_KINDS


@dataclass
class LLMReply:
    message: dict
    """assistant 消息原样，回传时整条塞回 messages（含 reasoning_content）。"""
    finish_reason: str
    content: str
    tool_calls: list[dict]
    elapsed: float


class LLMClient:
    def __init__(self, base_url: str, api_key: str, model: str, timeout: float = 120.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.timeout = timeout

    @property
    def endpoint(self) -> str:
        return self.base_url + "/chat/completions"

    def _body(self, messages: list[dict], tools: Optional[list[dict]]) -> dict:
        body: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "max_tokens": MAX_TOKENS,
        }
        if tools:
            body["tools"] = tools
            body["tool_choice"] = "auto"
        return body

    def chat(
        self,
        messages: list[dict],
        tools: Optional[list[dict]] = None,
        timeout: Optional[float] = None,
        on_call: Optional[Any] = None,
    ) -> LLMReply:
        started = time.perf_counter()
        body = self._body(messages, tools)
        record: dict[str, Any] = {
            "endpoint": self.endpoint,
            "model": self.model,
            "messages": len(messages),
            "tools": len(tools or []),
            # 契约 §6：trace 里要看得到发给模型的最终提示词。
            "prompt": _preview(json.dumps(messages, ensure_ascii=False)),
        }
        try:
            response = httpx.post(
                self.endpoint,
                json=body,
                headers={
                    "Authorization": "Bearer %s" % self.api_key,
                    "Content-Type": "application/json",
                },
                timeout=httpx.Timeout(timeout or self.timeout, connect=15.0),
            )
        except httpx.TimeoutException as exc:
            record.update(error="timeout", detail=str(exc))
            self._note(on_call, record, started)
            raise LLMError("timeout", "等待模型响应超时：%s" % exc) from exc
        except httpx.HTTPError as exc:
            record.update(error="transport", detail=str(exc))
            self._note(on_call, record, started)
            raise LLMError("transport", "调用模型失败：%s" % exc) from exc

        record["status"] = response.status_code
        if response.status_code != 200:
            # D13：400/401/402/422/429/500/503 都在这里变成结构化错误。
            detail = _error_detail(response)
            record.update(error="http_%d" % response.status_code, detail=detail)
            self._note(on_call, record, started)
            raise LLMError("http_error", detail, status=response.status_code)

        # D14：服务繁忙时正文前面会有空行，json 解析要能跳过。
        try:
            payload = json.loads(response.text.strip() or "{}")
        except ValueError as exc:
            record.update(error="bad_json", detail=response.text[:200])
            self._note(on_call, record, started)
            raise LLMError("bad_json", "模型返回的不是合法 JSON：%s" % response.text[:200]) from exc

        choices = payload.get("choices") or []
        if not choices:
            record.update(error="no_choice")
            self._note(on_call, record, started)
            raise LLMError("no_choice", "模型响应里没有 choices")
        choice = choices[0]
        message = choice.get("message") or {}
        finish = choice.get("finish_reason") or ""
        content = message.get("content") or ""
        tool_calls = message.get("tool_calls") or []
        record.update(
            finish_reason=finish,
            content_chars=len(content),
            tool_calls=[call.get("function", {}).get("name") for call in tool_calls],
            has_reasoning=bool(message.get("reasoning_content")),
            usage=payload.get("usage"),
            # 契约 §6：模型原始输出也要留痕。思考过程只留在 trace 里，不进任何对外字段。
            raw_content=_preview(content),
            raw_reasoning=_preview(message.get("reasoning_content") or ""),
        )
        self._note(on_call, record, started)

        if finish not in GOOD_FINISH:
            # D11：length / content_filter / insufficient_system_resource / aborted 一律按错误处理。
            raise LLMError(finish or "unknown_finish", "模型异常结束：finish_reason=%s" % finish)
        if not content.strip() and not tool_calls:
            raise LLMError("empty_content", "模型返回了空正文（finish_reason=%s）" % finish)
        return LLMReply(
            message=message,
            finish_reason=finish,
            content=content,
            tool_calls=tool_calls,
            elapsed=time.perf_counter() - started,
        )

    def chat_with_retry(
        self,
        messages: list[dict],
        tools: Optional[list[dict]] = None,
        budget: Optional[float] = None,
        on_call: Optional[Any] = None,
    ) -> LLMReply:
        """暂时性故障重试一次，且只在时间预算够的时候重试。"""
        per_call = min(self.timeout, budget) if budget else self.timeout
        try:
            return self.chat(messages, tools, timeout=per_call, on_call=on_call)
        except LLMError as first:
            remaining = (budget - per_call) if budget else self.timeout
            if not first.retryable or remaining < 5:
                raise
            time.sleep(min(1.0, max(0.0, remaining / 60)))
            return self.chat(messages, tools, timeout=min(self.timeout, remaining), on_call=on_call)

    @staticmethod
    def _note(on_call, record: dict, started: float) -> None:
        if on_call is not None:
            record["took_ms"] = round((time.perf_counter() - started) * 1000, 1)
            on_call(record)


def _preview(text: str, limit: int = 4000) -> str:
    text = text or ""
    return text if len(text) <= limit else text[:limit] + "…（截断，共 %d 字）" % len(text)


def _error_detail(response: httpx.Response) -> str:
    try:
        payload = response.json()
    except ValueError:
        return "HTTP %d：%s" % (response.status_code, response.text[:200])
    error = payload.get("error") if isinstance(payload, dict) else None
    if isinstance(error, dict):
        return "HTTP %d：%s（code=%s）" % (
            response.status_code,
            error.get("message", ""),
            error.get("code", ""),
        )
    return "HTTP %d：%s" % (response.status_code, json.dumps(payload, ensure_ascii=False)[:200])
