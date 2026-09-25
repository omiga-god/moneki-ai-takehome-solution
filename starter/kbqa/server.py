"""FastAPI 层：只做参数校验和 JSON 序列化，逻辑都在 service.py。"""

from __future__ import annotations

import json
from datetime import date
from typing import Any, Optional

from fastapi import FastAPI, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from .service import Service

app = FastAPI(title="经营看板 + 问答服务", version="0.9.3")
_service: Optional[Service] = None


def service() -> Service:
    global _service
    if _service is None:
        _service = Service()
    return _service


def _as_text(value: Any) -> str:
    """把请求里的标量原样变成字符串。

    契约 §5 要求 `/api/chat` 无论如何都返回 200，所以这里对类型宽容：
    数字、布尔的 `session_id` 或 `question` 一律当字符串收下，`null` 当没填。
    """
    if value is None:
        return ""
    if isinstance(value, (str, int, float, bool)):
        return str(value).strip()
    return json.dumps(value, ensure_ascii=False)


class ChatRequest(BaseModel):
    session_id: Optional[Any] = None
    question: Optional[Any] = None


class RetrieveRequest(BaseModel):
    query: Optional[Any] = None
    #: 只要求是正整数；超过索引片段总数时由服务按总数封顶（契约 §4）。
    top_k: int = Field(default=5, ge=1)


def _bad_date(*values: str) -> Optional[JSONResponse]:
    for value in values:
        try:
            date.fromisoformat(value)
        except (TypeError, ValueError):
            return JSONResponse(
                status_code=400,
                content={"error": "日期格式必须是 YYYY-MM-DD，收到 %r" % value},
            )
    return None


@app.get("/api/health")
def health() -> dict:
    return service().health()


@app.get("/api/metrics/summary")
def metrics_summary(
    start: str = Query(...),
    end: str = Query(...),
    store_id: Optional[str] = None,
    product_id: Optional[str] = None,
):
    bad = _bad_date(start, end)
    return bad or service().metrics_summary(start, end, store_id, product_id)


@app.get("/api/metrics/daily")
def metrics_daily(
    start: str = Query(...),
    end: str = Query(...),
    store_id: Optional[str] = None,
    product_id: Optional[str] = None,
):
    bad = _bad_date(start, end)
    return bad or service().metrics_daily(start, end, store_id, product_id)


@app.post("/api/retrieve")
def retrieve(request: RetrieveRequest) -> dict:
    return service().retrieve(_as_text(request.query), request.top_k)


@app.post("/api/chat")
def chat(request: ChatRequest) -> dict:
    session_id = _as_text(request.session_id) or None
    return service().chat(session_id, _as_text(request.question))


@app.get("/api/trace/{trace_id}")
def trace(trace_id: str):
    payload = service().get_trace(trace_id)
    if payload is None:
        return JSONResponse(status_code=404, content={"error": "没有这个 trace_id：%s" % trace_id})
    return payload


@app.get("/api/data_quality")
def data_quality() -> dict:
    """第一关的“数据质量”面板：清洗掉了多少行、各因为什么。"""
    current = service()
    return {
        "cleaning_report": current.tools.cleaning_report(),
        "data_period": current.data_period,
        "kb_warnings": current.index.warnings,
    }
