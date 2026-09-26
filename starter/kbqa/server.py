"""FastAPI 层：只做参数校验和 JSON 序列化，逻辑都在 service.py。"""

from __future__ import annotations

import json
import re
import threading
from datetime import date
from pathlib import Path
from typing import Any, Optional

from fastapi import FastAPI, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .service import Service
from .trace import Trace

app = FastAPI(title="经营看板 + 问答服务", version="0.9.3")
_service: Optional[Service] = None
_service_lock = threading.Lock()


def service() -> Service:
    global _service
    if _service is None:
        with _service_lock:
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
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
                raise ValueError("invalid ISO date")
            date.fromisoformat(value)
        except (TypeError, ValueError):
            return JSONResponse(
                status_code=400,
                content={"error": "日期格式必须是 YYYY-MM-DD，收到 %r" % value},
            )
    if len(values) == 2 and (values[0] > values[1] or (date.fromisoformat(values[1]) - date.fromisoformat(values[0])).days > 3660):
        return JSONResponse(status_code=400, content={"error": "起始日期不能晚于结束日期，单次范围不能超过十年"})
    return None


@app.exception_handler(RequestValidationError)
async def validation_error(request: Request, exc: RequestValidationError):
    if request.url.path != "/api/chat":
        return await request_validation_exception_handler(request, exc)
    current = service()
    record = Trace(current.traces.new_id(current.settings.today.isoformat()), "[invalid request]")
    record.error("request_validation", exc)
    current.traces.save(record)
    return JSONResponse(status_code=200, content={"answer": "请求格式不正确，请发送包含 question 的 JSON 对象。", "answer_type": "refusal", "citations": [], "data_evidence": [], "trace_id": record.trace_id})


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


@app.get("/api/metrics/top_products")
def metrics_top_products(
    start: str = Query(...),
    end: str = Query(...),
    store_id: Optional[str] = None,
    limit: int = Query(default=10, ge=1, le=100),
):
    bad = _bad_date(start, end)
    return bad or service().tools.top_products(start, end, store_id, limit)


@app.get("/api/stores")
def stores() -> dict:
    return {"stores": service().tools.stores()}


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


# npm run build 后可由同一个本地后端提供看板，无需另起前端服务器。
_frontend = Path(__file__).resolve().parents[2] / "frontend" / "dist"
if (_frontend / "index.html").is_file():
    app.mount("/", StaticFiles(directory=_frontend, html=True), name="dashboard")
