#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""题库评测脚本：同一份脚本评测所有人的作品。

用法：

    python3 eval/run_eval.py --base-url http://localhost:8000 \
        --questions eval/public_questions.jsonl

只依赖标准库，Python 3.10 及以上。
不联网（只访问 --base-url 指向的那一个服务），不写数据库，不改知识库。

它做四件事：

1. 按题库文件逐题请求你的服务（**严格一题一题地发，不并发**）。
2. 按题目里写明的检查项逐条核对回答。
3. 计分：一个 turn 的全部检查项都通过，这个 turn 才算通过；
   一题得分 = 分值 × 通过的 turn 数 ÷ 总 turn 数。
4. 写出 ``report.json`` 与 ``report.md``：总分、分类别得分、逐题明细、
   每一条没通过的检查（期望 vs 实际）、`/api/health` 快照、每题耗时。

服务出任何状况（超时、HTTP 500、返回的不是 JSON、字段缺失或类型不对），
都只会让对应的检查变红并写明原因，脚本本身不会崩。
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import statistics
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass, field
from html import unescape

__all__ = [
    "normalize_text", "normalize_doc", "extract_numbers", "KnowledgeBase",
    "Client", "Evaluator", "load_questions", "render_markdown", "main",
]

DEFAULT_TIMEOUT = 180.0          # 契约 7.3：/api/chat 整体在 180 秒内返回
FACT_QUOTE_MAX = 400             # 一条 quote 最长多少字（去空白后）；超了就不算引用
MAX_BODY_BYTES = 2 * 1024 * 1024  # 单次响应体上限：超过按失败处理，不把内存吃光
MAX_JSON_DEPTH = 64              # JSON 嵌套深度上限：再深就不解析（防递归炸弹）
MAX_ABS_NUMBER = 1e15            # 可比较的数值范围：再大按"不是有效数字"处理
MAX_ANSWER_CHARS = 1200          # answer 长度上限（契约第 5 节：给运营看的回答）
MAX_ANSWER_NUMBERS = 20          # answer 里最多允许多少个不同的数字（防数字轰炸）
MAX_CITED_DOCS = 4               # 一轮回答最多引用多少份不同文档
MAX_EVIDENCE_RESULT_BYTES = 4096  # 单条 data_evidence.result 序列化后的上限
MAX_EVIDENCE_NUMBERS = 60        # 全部 result 里的数字总数上限
ANSWER_TYPES = ("data", "doc", "hybrid", "refusal", "clarify")
CATEGORY_ORDER = [
    "metrics", "retrieval", "data", "doc", "version",
    "hybrid", "multi_turn", "refusal", "safety", "health",
]
CATEGORY_LABEL = {
    "metrics": "指标接口", "retrieval": "检索质量", "data": "纯数据问题",
    "doc": "纯文档问题", "version": "版本与时效", "hybrid": "数据 + 文档",
    "multi_turn": "多轮追问", "refusal": "拒答", "safety": "安全",
    "health": "健康检查",
}
MONEY_FIELDS = ("net_revenue", "refund_amount", "aov")


# ======================================================================
# 一、文本规范化与数字提取
# ======================================================================

_MD_NOISE = str.maketrans({"*": None, "`": None, "|": None, "#": None,
                           ">": None, "​": None})


def nfkc(text: str) -> str:
    """全角转半角、兼容字符归一（NFKC）。"""
    return unicodedata.normalize("NFKC", text)


def normalize_text(text: str) -> str:
    """用于 ``text_any`` / ``text_all`` / ``text_none`` 的子串比较。

    全角半角归一 + 去掉全部空白 + 去掉 Markdown 记号 + 忽略英文大小写。
    """
    return "".join(nfkc(text).translate(_MD_NOISE).split()).casefold()


def normalize_doc(text: str) -> str:
    """用于 ``quotes_verbatim`` 的逐字比较（对文档与 quote 用同一套规则）。"""
    return "".join(nfkc(text).translate(_MD_NOISE).split())


# --- 数字提取 ---------------------------------------------------------
# 先把"不是答案数字"的东西整段遮掉（日期、时间、编号、电话、年份），
# 再从剩下的文本里取数。顺序很重要：编号和日期要先于裸数字被吃掉。
_MASK_PATTERNS = [
    r"KB-\d{3}",                                        # 文档编号
    r"\bORD\d+",                                        # 订单号
    r"\b[A-Za-z][A-Za-z0-9]{0,5}-\d[\dA-Za-z-]*",       # t-2026...-0001、HU-8842
    r"\b[A-Za-z]{1,3}\d{2,}\b",                         # S02、P06、A0117
    r"\d{3,4}-\d{4}-\d{4}",                             # 电话
    r"\d{4}[-/]\d{1,2}[-/]\d{1,2}",                     # 2026-06-18、2026/6/5
    r"\d{1,2}[-/]\d{1,2}[-/]\d{4}",                     # 18-06-2026
    r"\d{4}\s*年(?:\s*\d{1,2}\s*月)?(?:\s*\d{1,2}\s*[日号])?",
    r"(?<![\d个半])\d{1,2}\s*月(?:\s*\d{1,2}\s*[日号])?",
    r"(?<![\d个半])\d{1,2}\s*[日号](?![\d])",
    r"\d{1,2}:\d{2}(?::\d{2})?",                        # 23:00
]
_MASK_RE = re.compile("|".join("(?:%s)" % p for p in _MASK_PATTERNS))
_THOUSANDS_RE = re.compile(r"\d{1,3}(?:,\d{3})+(?:\.\d+)?")
_PERCENT_RE = re.compile(r"(-?\d+(?:\.\d+)?)\s*%")
_SCALE_RE = re.compile(r"(-?\d+(?:\.\d+)?)\s*([万亿])")
_SCALES = {"万": 10000, "亿": 100000000}
_NUMBER_RE = re.compile(r"-?\d+(?:\.\d+)?")


def _usable(value: float) -> bool:
    return math.isfinite(value) and abs(value) <= MAX_ABS_NUMBER


def extract_numbers(text: str) -> list[float]:
    """把一段回答里"当作答案的数字"取出来。

    - 去千分位逗号、`¥`、`元`（`¥13,524.00` / `13524元` 都得到 13524.0）。
    - 全角数字先转半角。
    - `100%` 与 `100` 都得到 100.0。
    - `万` / `亿` 换算：`16.2414万` 得到 162414，`1万元` 得到 10000。
      （契约要求写精确数字，但写成"万"的精确数字同样认；反过来，编出来的
      "平均工资 1 万元"也就跑不掉了。）
    - 日期（`2026-06-18`、`6 月 18 日`）、时间（`23:00`）、文档编号（`KB-013`）、
      门店与商品编号（`S02`、`P06`）、订单号、电话**不算答案数字**。
    - 非有限数与绝对值超过 1e15 的数一律丢掉，不参与任何比较。
    """
    if not isinstance(text, str):
        return []
    s = nfkc(text)
    s = _MASK_RE.sub(" ", s)
    s = _THOUSANDS_RE.sub(lambda m: m.group(0).replace(",", ""), s)
    out: list[float] = []

    def take(raw: str, factor: float = 1.0) -> None:
        try:
            value = float(raw) * factor
        except (ValueError, OverflowError):                  # pragma: no cover
            return
        if _usable(value):
            out.append(value)

    for m in _PERCENT_RE.finditer(s):
        take(m.group(1))
    s = _PERCENT_RE.sub(" ", s)
    for m in _SCALE_RE.finditer(s):
        take(m.group(1), _SCALES[m.group(2)])
    s = _SCALE_RE.sub(" ", s)
    for m in _NUMBER_RE.finditer(s):
        take(m.group(0))
    return out


def as_number(value) -> tuple[float | None, str | None]:
    """把响应里的字段转成可比较的数：不是数字、超范围、非有限都不接受。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None, "不是数字"
    try:
        num = float(value)
    except (OverflowError, ValueError):
        return None, "数值超出可比较范围（绝对值大于 1e15 或不是有限数）"
    if not _usable(num):
        return None, "数值超出可比较范围（绝对值大于 1e15 或不是有限数）"
    return num, None


def numbers_in_object(obj) -> list[float]:
    """从 ``data_evidence`` 的 ``result`` 里取数（同样忽略日期与编号）。"""
    try:
        blob = json.dumps(obj, ensure_ascii=False, default=str)
    except (TypeError, ValueError):                          # pragma: no cover
        blob = str(obj)
    return extract_numbers(blob)


def close_enough(actual: float, expected: float, tol: float) -> bool:
    return abs(actual - expected) <= tol + 1e-9


# ======================================================================
# 二、知识库（逐字校验 quote 用）
# ======================================================================

_DOC_ID_RE = re.compile(r"^(KB-\d{3})_")
_SCRIPT_RE = re.compile(r"<(script|style)\b.*?</\1>", re.I | re.S)
_TAG_RE = re.compile(r"<[^>]+>")


def decode_bytes(raw: bytes) -> str:
    """先试 UTF-8，失败再试 GB18030（KB-062 是 GBK 导出的旧 OA 文件）。"""
    for enc in ("utf-8", "gb18030"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def html_to_text(text: str) -> str:
    text = _SCRIPT_RE.sub(" ", text)
    text = _TAG_RE.sub(" ", text)
    return unescape(text)


class KnowledgeBase:
    """把 ``--kb`` 目录里的文档读成"可见正文"，供 quote 逐字校验。"""

    def __init__(self, root: str | None):
        self.root = root
        self.docs: dict[str, str] = {}
        self.errors: list[str] = []
        if root and os.path.isdir(root):
            self._load(root)
        elif root:
            self.errors.append("知识库目录不存在：%s" % root)

    def _load(self, root: str) -> None:
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames.sort()
            for name in sorted(filenames):
                m = _DOC_ID_RE.match(name)
                if not m:
                    continue
                path = os.path.join(dirpath, name)
                try:
                    with open(path, "rb") as fh:
                        text = decode_bytes(fh.read())
                except OSError as exc:                       # pragma: no cover
                    self.errors.append("%s 读不出来：%s" % (name, exc))
                    continue
                if name.lower().endswith((".html", ".htm")):
                    text = html_to_text(text)
                self.docs[m.group(1)] = normalize_doc(text)

    @property
    def loaded(self) -> bool:
        return bool(self.docs)

    def check_quote(self, doc_id: str, quote: str) -> str | None:
        """返回 None 表示通过，否则返回人话的失败原因。"""
        if not self.loaded:
            return None                      # 没有知识库目录时跳过这项检查
        if doc_id not in self.docs:
            return "知识库里没有 %s 这份文档" % doc_id
        q = normalize_doc(quote or "")
        if not q:
            return "%s 的 quote 是空的" % doc_id
        if q not in self.docs[doc_id]:
            return "%s 的 quote 不是原文里的连续文字：%s" % (doc_id, shorten(quote, 60))
        return None


def shorten(value, limit: int = 120) -> str:
    text = value if isinstance(value, str) else json.dumps(
        value, ensure_ascii=False, default=str)
    text = " ".join(text.split())
    return text if len(text) <= limit else text[:limit] + "…"


# ======================================================================
# 三、HTTP 客户端
# ======================================================================

@dataclass
class Resp:
    ok: bool
    status: int | None
    data: object
    error: str | None
    seconds: float
    body: str = ""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """评测不跟随跳转：候选人的服务必须自己在这个地址上答完。"""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class _TooBig(Exception):
    pass


class _PastDeadline(Exception):
    pass


def _reject_constant(name):
    raise ValueError("JSON 里出现了 %s，不是合法的 JSON 数值" % name)


def json_depth(text: str, limit: int = MAX_JSON_DEPTH) -> int:
    """扫一遍括号算嵌套深度（跳过字符串内部），超过 limit 就提前返回。

    一万层嵌套的数组会让 ``json.loads`` 直接 RecursionError 把进程带走，
    所以先用一次线性扫描挡住它。
    """
    depth = max_depth = 0
    in_string = escape = False
    for ch in text:
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch in "[{":
            depth += 1
            max_depth = max(max_depth, depth)
            if max_depth > limit:
                return max_depth
        elif ch in "]}":
            depth -= 1
    return max_depth


class Client:
    def __init__(self, base_url: str, timeout: float = DEFAULT_TIMEOUT):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        # 不跟随重定向；不经过任何代理（评测对象就在这个地址上）。
        self.opener = urllib.request.build_opener(
            _NoRedirect, urllib.request.ProxyHandler({}))

    def get(self, path: str, params: dict | None = None) -> Resp:
        url = self.base_url + path
        if params:
            clean = {k: v for k, v in params.items() if v is not None}
            url += "?" + urllib.parse.urlencode(clean)
        return self._send(urllib.request.Request(url, method="GET"))

    def post(self, path: str, payload: dict) -> Resp:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            self.base_url + path, data=body, method="POST",
            headers={"Content-Type": "application/json; charset=utf-8"})
        return self._send(req)

    def _read_capped(self, fh, started: float) -> bytes:
        """边读边看两件事：整体截止时间，以及响应体大小上限。

        socket 超时只管"一次读多久没动静"；每 80 毫秒挤一小段的服务能把
        `--timeout` 拖到无限长，所以这里用**从发请求算起**的绝对截止时间。
        """
        chunks, total = [], 0
        while True:
            if time.monotonic() - started > self.timeout:
                raise _PastDeadline()
            chunk = fh.read(65536)
            if not chunk:
                return b"".join(chunks)
            total += len(chunk)
            if total > MAX_BODY_BYTES:
                raise _TooBig()
            chunks.append(chunk)

    def _send(self, req: urllib.request.Request) -> Resp:
        started = time.monotonic()
        try:
            with self.opener.open(req, timeout=self.timeout) as fh:
                status = fh.status
                raw = self._read_capped(fh, started)
        except urllib.error.HTTPError as exc:
            seconds = time.monotonic() - started
            try:
                raw = exc.read(MAX_BODY_BYTES + 1)
            except Exception:                                # pragma: no cover
                raw = b""
            body = raw.decode("utf-8", errors="replace")
            if 300 <= exc.code < 400:
                return Resp(False, exc.code, None,
                            "服务返回了 %d 重定向（评测不跟随跳转，请在原地址上作答）"
                            % exc.code, seconds, body)
            return Resp(False, exc.code, None,
                        "HTTP %s：%s" % (exc.code, shorten(body, 200) or exc.reason),
                        seconds, body)
        except _TooBig:
            seconds = time.monotonic() - started
            return Resp(False, None, None,
                        "响应体超过 %d MB，已中断" % (MAX_BODY_BYTES // (1024 * 1024)),
                        seconds, "")
        except _PastDeadline:
            seconds = time.monotonic() - started
            return Resp(False, None, None,
                        "请求超过 %.1f 秒还没有读完（按整体截止时间计）" % self.timeout,
                        seconds, "")
        except Exception as exc:
            seconds = time.monotonic() - started
            name = type(exc).__name__
            reason = str(exc) or name
            if "timed out" in reason or name in ("timeout", "TimeoutError"):
                reason = "请求超过 %.1f 秒没有返回" % self.timeout
            return Resp(False, None, None, "请求失败：%s" % reason, seconds, "")
        seconds = time.monotonic() - started
        try:
            body = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            return Resp(False, status, None,
                        "返回的字节不是合法 UTF-8（契约要求 UTF-8）：%s" % exc,
                        seconds, "")
        depth = json_depth(body)
        if depth > MAX_JSON_DEPTH:
            return Resp(False, status, None,
                        "JSON 嵌套超过 %d 层，不解析" % MAX_JSON_DEPTH, seconds, body)
        try:
            data = json.loads(body, parse_constant=_reject_constant)
        except RecursionError:                               # pragma: no cover
            return Resp(False, status, None, "JSON 嵌套太深，解析不了", seconds, body)
        except ValueError as exc:
            return Resp(False, status, None,
                        "返回的不是合法 JSON：%s" % (shorten(body, 160) or exc),
                        seconds, body)
        if not isinstance(data, dict):
            return Resp(False, status, data,
                        "返回的 JSON 顶层不是对象：%s" % shorten(body, 120), seconds, body)
        return Resp(True, status, data, None, seconds, body)


# ======================================================================
# 四、检查项
# ======================================================================

@dataclass
class Check:
    name: str
    passed: bool
    expected: object = None
    actual: object = None
    reason: str = ""

    def to_json(self) -> dict:
        out = {"name": self.name, "passed": self.passed}
        if not self.passed:
            out["expected"] = self.expected
            out["actual"] = self.actual
            out["reason"] = self.reason
        return out


def ok(name: str) -> Check:
    return Check(name, True)


def bad(name: str, expected, actual, reason: str) -> Check:
    return Check(name, False, expected, actual, reason)


def spec_values(specs) -> list[dict]:
    """把 ``[{"value":1,"tol":0}]`` 规整成统一形状，容忍只写数字的简写。"""
    out = []
    for item in specs or []:
        if isinstance(item, dict):
            out.append({"value": item.get("value"), "tol": float(item.get("tol", 0) or 0)})
        else:
            out.append({"value": item, "tol": 0.0})
    return out


def find_number(specs_item: dict, pool: list[float]) -> bool:
    value = specs_item["value"]
    if value is None:                                        # pragma: no cover
        return False
    return any(close_enough(n, float(value), specs_item["tol"]) for n in pool)


# ======================================================================
# 五、评测
# ======================================================================

@dataclass
class TurnResult:
    question: str
    checks: list[Check] = field(default_factory=list)
    seconds: float = 0.0
    answer: str = ""
    answer_type: object = None
    citations: list = field(default_factory=list)
    trace_id: object = None

    @property
    def passed(self) -> bool:
        return all(c.passed for c in self.checks)


@dataclass
class QuestionResult:
    qid: str
    category: str
    points: float
    turns: list[TurnResult] = field(default_factory=list)
    seconds: float = 0.0

    @property
    def earned(self) -> float:
        if not self.turns:
            return 0.0
        passed = sum(1 for t in self.turns if t.passed)
        return self.points * passed / len(self.turns)

    @property
    def passed(self) -> bool:
        return bool(self.turns) and all(t.passed for t in self.turns)


class Evaluator:
    def __init__(self, client: Client, questions: list[dict],
                 kb: KnowledgeBase, only: str | None = None):
        self.client = client
        self.questions = [q for q in questions
                          if not only or q.get("category") == only]
        self.kb = kb
        self.nonce = uuid.uuid4().hex[:8]
        self.health: dict | None = None
        self.health_error: str | None = None
        # 每一个 post.range 各取一份基准：题目写的区间和脚本自己猜的区间
        # 必须是同一个，否则"跑完还是不是原来的数"根本无从比起。
        self.baselines: dict[tuple, dict] = {}
        self.baseline_errors: dict[tuple, str] = {}
        self.results: list[QuestionResult] = []

    # -- 跑前准备 ------------------------------------------------------
    def snapshot_health(self) -> None:
        resp = self.client.get("/api/health")
        if resp.ok:
            self.health = resp.data
        else:
            self.health_error = resp.error

    def _range_for(self, post: dict) -> tuple | None:
        rng = post.get("range")
        if isinstance(rng, (list, tuple)) and len(rng) == 2:
            return tuple(rng)
        return self._guess_range()

    def _baseline_ranges(self) -> list[tuple]:
        out: list[tuple] = []
        for q in self.questions:
            post = q.get("post") or {}
            if not post.get("metrics_unchanged"):
                continue
            rng = self._range_for(post)
            if rng and rng not in out:
                out.append(rng)
        return out

    def _guess_range(self) -> tuple[str, str] | None:
        starts, ends = [], []
        for q in self.questions:
            params = (q.get("request") or {}).get("params") or {}
            if params.get("start") and params.get("end"):
                starts.append(params["start"])
                ends.append(params["end"])
            post = q.get("post") or {}
            if post.get("range"):
                starts.append(post["range"][0])
                ends.append(post["range"][1])
        if not starts:
            return None
        return min(starts), max(ends)

    def snapshot_baseline(self) -> None:
        for rng in self._baseline_ranges():
            resp = self.client.get("/api/metrics/summary",
                                   {"start": rng[0], "end": rng[1]})
            if resp.ok:
                self.baselines[rng] = resp.data
            else:
                self.baseline_errors[rng] = resp.error or "取不到基准"

    # -- 主循环 --------------------------------------------------------
    def run(self) -> list[QuestionResult]:
        self.snapshot_health()
        self.snapshot_baseline()
        for q in self.questions:
            category = q.get("category", "")
            runner = {"metrics": self.run_metrics, "retrieval": self.run_retrieval,
                      "health": self.run_health}.get(category, self.run_chat)
            try:
                result = runner(q)
            except Exception as exc:                 # 一题炸了不能带走整场评测
                result = QuestionResult(q.get("id", "?"), category,
                                        float(q.get("points", 1) or 0))
                turns = q.get("turns")
                first = turns[0] if isinstance(turns, list) and turns else {}
                label = (first.get("question") if isinstance(first, dict) else None)
                turn = TurnResult(question=shorten(
                    label or q.get("query") or category or q.get("id", "?"), 120))
                turn.checks.append(bad(
                    "internal_error", "这一题能跑完",
                    "%s: %s" % (type(exc).__name__, exc),
                    "评测这一题时出了异常，已记为失败并继续跑后面的题：%s: %s"
                    % (type(exc).__name__, shorten(str(exc), 200))))
                result.turns.append(turn)
            self.results.append(result)
        return self.results

    # -- metrics -------------------------------------------------------
    def run_metrics(self, q: dict) -> QuestionResult:
        res = QuestionResult(q.get("id", "?"), q.get("category", ""),
                             float(q.get("points", 1)))
        req = q.get("request") or {}
        path = req.get("path", "")
        params = req.get("params") or {}
        turn = TurnResult(question="%s %s" % (req.get("method", "GET"), path))
        resp = self.client.get(path, params)
        turn.seconds = resp.seconds
        if not resp.ok:
            turn.checks.append(bad("request", "HTTP 200 + JSON", resp.status, resp.error))
            res.turns.append(turn)
            res.seconds = resp.seconds
            return res
        data = resp.data
        turn.checks.append(ok("request"))
        for name, spec in sorted((q.get("expect") or {}).items()):
            turn.checks.append(self._check_field(data, name, spec))
        if q.get("expect_days"):
            turn.checks.append(self._check_days(data, q["expect_days"]))
        turn.answer = shorten(data, 400)
        res.turns.append(turn)
        res.seconds = resp.seconds
        return res

    @staticmethod
    def _check_field(data: dict, name: str, spec) -> Check:
        label = "expect.%s" % name
        if not isinstance(data, dict) or name not in data:
            return bad(label, spec, None, "响应里没有 %s 字段" % name)
        actual = data.get(name)
        if isinstance(spec, dict) and "value" in spec:
            value, tol = spec.get("value"), float(spec.get("tol", 0) or 0)
        else:
            value, tol = spec, 0.0
        if value is None:
            if actual is None:
                return ok(label)
            return bad(label, None, actual, "%s 应为 null" % name)
        if isinstance(value, str):
            if actual == value:
                return ok(label)
            return bad(label, value, actual, "%s 对不上" % name)
        number, problem = as_number(actual)
        if problem:
            return bad(label, value, shorten(actual, 80), "%s %s" % (name, problem))
        if close_enough(number, float(value), tol):
            return ok(label)
        return bad(label, value, actual,
                   "%s 差了 %.2f（容差 %s）" % (name, number - float(value), tol))

    @staticmethod
    def _check_days(data: dict, expect_days: list) -> Check:
        label = "expect_days"
        days = data.get("days") if isinstance(data, dict) else None
        if not isinstance(days, list):
            return bad(label, "days 列表", shorten(data, 120), "响应里没有 days 列表")
        got = {}
        for item in days:
            if isinstance(item, dict) and isinstance(item.get("date"), str):
                got[item["date"]] = item
        problems = []
        for want in expect_days:
            date = want.get("date")
            item = got.get(date)
            if item is None:
                problems.append("缺少 %s 这一天" % date)
                continue
            for name, spec in sorted(want.items()):
                if name == "date":
                    continue
                c = Evaluator._check_field(item, name, spec)
                if not c.passed:
                    problems.append("%s：%s（期望 %s，实际 %s）"
                                    % (date, c.reason, c.expected, c.actual))
        if len(days) != len(expect_days):
            problems.append("应返回 %d 天，实际 %d 条" % (len(expect_days), len(days)))
        if problems:
            return bad(label, "%d 天逐日核对" % len(expect_days),
                       shorten("；".join(problems), 300), problems[0])
        return ok(label)

    # -- retrieval -----------------------------------------------------
    def run_retrieval(self, q: dict) -> QuestionResult:
        res = QuestionResult(q.get("id", "?"), q.get("category", ""),
                             float(q.get("points", 1)))
        turn = TurnResult(question=q.get("query", ""))
        payload = {"query": q.get("query", ""), "top_k": q.get("top_k", 5)}
        resp = self.client.post("/api/retrieve", payload)
        turn.seconds = resp.seconds
        if not resp.ok:
            turn.checks.append(bad("request", "HTTP 200 + JSON", resp.status, resp.error))
            res.turns.append(turn)
            res.seconds = resp.seconds
            return res
        results = resp.data.get("results")
        if not isinstance(results, list):
            turn.checks.append(bad("results_shape", "results 为数组",
                                   shorten(resp.data, 120), "响应里没有 results 数组"))
            res.turns.append(turn)
            res.seconds = resp.seconds
            return res
        # 契约 §4：每一条都要有 doc_id、chunk_id、score、text，一条都不能省。
        doc_ids, problems = [], []
        for i, item in enumerate(results, 1):
            if not isinstance(item, dict):
                problems.append("第 %d 条不是对象" % i)
                continue
            doc_id = item.get("doc_id")
            if not isinstance(doc_id, str) or not doc_id.strip():
                problems.append("第 %d 条没有 doc_id" % i)
            else:
                doc_ids.append(doc_id)
            if not isinstance(item.get("chunk_id"), str) or not item["chunk_id"].strip():
                problems.append("第 %d 条没有 chunk_id" % i)
            score, score_problem = as_number(item.get("score"))
            if score_problem:
                problems.append("第 %d 条的 score %s" % (i, score_problem))
            text = item.get("text")
            if not isinstance(text, str) or not text.strip():
                problems.append("第 %d 条没有 text" % i)
        top_k = q.get("top_k", 5)
        if len(results) > top_k:
            problems.append("返回了 %d 条，超过 top_k=%d" % (len(results), top_k))
        turn.checks.append(
            ok("results_shape") if not problems else
            bad("results_shape",
                "最多 %d 条，每条都要有 doc_id / chunk_id / score / text" % top_k,
                shorten(results, 200), "；".join(problems[:4])))
        expected_count = q.get("results_count")
        if expected_count is not None:
            turn.checks.append(
                ok("results_count") if len(results) == expected_count else
                bad("results_count", expected_count, len(results),
                    "返回了 %d 条，应为 top_k 条；先取 top-k 再过滤的实现会这样"
                    % len(results)))
        turn.answer = json.dumps(doc_ids, ensure_ascii=False)
        gold_any = q.get("gold_any") or []
        if gold_any:
            hit = [d for d in gold_any if d in doc_ids]
            turn.checks.append(ok("gold_any") if hit else
                               bad("gold_any", gold_any, doc_ids,
                                   "top-%d 里一个金标文档都没有" % top_k))
        gold_all = q.get("gold_all") or []
        if gold_all:
            missing = [d for d in gold_all if d not in doc_ids]
            turn.checks.append(ok("gold_all") if not missing else
                               bad("gold_all", gold_all, doc_ids,
                                   "top-%d 里缺少 %s" % (top_k, "、".join(missing))))
        res.turns.append(turn)
        res.seconds = resp.seconds
        return res

    # -- health --------------------------------------------------------
    def run_health(self, q: dict) -> QuestionResult:
        res = QuestionResult(q.get("id", "?"), q.get("category", ""),
                             float(q.get("points", 1)))
        turn = TurnResult(question="GET /api/health")
        resp = self.client.get("/api/health")
        turn.seconds = resp.seconds
        if not resp.ok:
            turn.checks.append(bad("request", "HTTP 200 + JSON", resp.status, resp.error))
        else:
            turn.checks.append(ok("request"))
            for name, spec in sorted((q.get("expect") or {}).items()):
                turn.checks.append(self._check_field(resp.data, name, spec))
            turn.answer = shorten(resp.data, 400)
        res.turns.append(turn)
        res.seconds = resp.seconds
        return res

    # -- chat ----------------------------------------------------------
    def run_chat(self, q: dict) -> QuestionResult:
        res = QuestionResult(q.get("id", "?"), q.get("category", ""),
                             float(q.get("points", 1)))
        # session_id 是一串随机值：题号、顺序、任何可猜的模式都不能出现在里面，
        # 否则服务可以按题号去公开题库里抄检查项（Codex 的作弊服务就是这么做的）。
        session = uuid.uuid4().hex
        total = 0.0
        for turn_spec in q.get("turns") or []:
            question = turn_spec.get("question", "")
            resp = self.client.post("/api/chat",
                                    {"session_id": session, "question": question})
            turn = TurnResult(question=question, seconds=resp.seconds)
            total += resp.seconds
            if not resp.ok:
                turn.checks.append(bad("response", "HTTP 200 + 合法 JSON",
                                       resp.status, resp.error))
            else:
                self._check_chat(turn, turn_spec.get("checks") or {}, resp.data,
                                 question)
                trace_check, trace_seconds = self._check_trace(resp.data)
                total += trace_seconds
                turn.seconds += trace_seconds
                turn.checks.append(trace_check)
            res.turns.append(turn)
        post = q.get("post") or {}
        if post.get("metrics_unchanged"):
            check, seconds = self._check_metrics_unchanged(post)
            total += seconds
            if res.turns:
                res.turns[-1].checks.append(check)
        res.seconds = total
        return res

    def _check_chat(self, turn: TurnResult, checks: dict, data: dict,
                    question: str = "") -> None:
        answer = data.get("answer")
        answer_type = data.get("answer_type")
        citations = data.get("citations")
        evidence = data.get("data_evidence")
        turn.answer = answer if isinstance(answer, str) else shorten(answer, 200)
        turn.answer_type = answer_type
        turn.trace_id = data.get("trace_id")

        # --- 形状（契约第 5 节）---
        problems = []
        if not isinstance(answer, str) or not answer.strip():
            problems.append("answer 必须是非空字符串")
        if not isinstance(answer_type, str) or answer_type not in ANSWER_TYPES:
            problems.append("answer_type 必须是 %s 之一，实际 %r"
                            % ("/".join(ANSWER_TYPES), answer_type))
        cite_pairs: list[tuple[str, str]] = []
        if not isinstance(citations, list):
            problems.append("citations 必须是数组（没用到知识库就给空数组）")
        else:
            for item in citations:
                if not isinstance(item, dict):
                    problems.append("citations 里出现了非对象元素")
                    continue
                doc_id, quote = item.get("doc_id"), item.get("quote")
                if not isinstance(doc_id, str):
                    problems.append("citations 里有一条没有 doc_id")
                    continue
                cite_pairs.append((doc_id, quote if isinstance(quote, str) else ""))
                if not isinstance(quote, str):
                    problems.append("%s 的 quote 不是字符串" % doc_id)
        if not isinstance(evidence, list):
            problems.append("data_evidence 必须是数组（没用到数据库就给空数组）")
        turn.citations = [d for d, _ in cite_pairs]
        turn.checks.append(ok("schema") if not problems else
                           bad("schema", "契约第 5 节的字段形状",
                               shorten(data, 200), "；".join(problems)))

        answer_text = answer if isinstance(answer, str) else ""
        norm_answer = normalize_text(answer_text)
        pool = extract_numbers(answer_text)
        # 引用要"摘出来的那一句"才算：整篇文档贴进 quote 的那一条不算引用，
        # 但 cite_none 照旧按**全部**引用算——引了不该引的，长短都算引了。
        doc_ids_all = [d for d, _ in cite_pairs]
        doc_ids = [d for d, quote in cite_pairs
                   if quote and len(normalize_doc(quote)) <= FACT_QUOTE_MAX]
        dumped = sorted({d for d, quote in cite_pairs
                         if quote and len(normalize_doc(quote)) > FACT_QUOTE_MAX})

        # --- 回答卫生：长度、数字轰炸（每一轮都查）---
        if len(answer_text) > MAX_ANSWER_CHARS:
            turn.checks.append(bad("answer_length", "不超过 %d 字" % MAX_ANSWER_CHARS,
                                   len(answer_text),
                                   "回答 %d 字，超过上限；请给运营一段能读的话，"
                                   "不要把文档或数据倒进来" % len(answer_text)))
        else:
            turn.checks.append(ok("answer_length"))
        distinct = sorted({round(n, 6) for n in pool})
        if len(distinct) > MAX_ANSWER_NUMBERS:
            turn.checks.append(bad("number_flood",
                                   "不超过 %d 个不同的数字" % MAX_ANSWER_NUMBERS,
                                   len(distinct),
                                   "回答里出现了 %d 个不同的数字；穷举一堆数字"
                                   "不是回答" % len(distinct)))
        else:
            turn.checks.append(ok("number_flood"))
        turn.checks.append(self._check_citation_hygiene(cite_pairs, dumped))
        if isinstance(evidence, list) and evidence:
            turn.checks.append(self._check_evidence_hygiene(evidence))

        if "answer_type_in" in checks:
            allow = checks["answer_type_in"]
            turn.checks.append(ok("answer_type_in") if answer_type in allow else
                               bad("answer_type_in", allow, answer_type,
                                   "answer_type 应是 %s 之一" % "/".join(allow)))

        if "numbers_all" in checks:
            want = spec_values(checks["numbers_all"])
            missing = [s["value"] for s in want if not find_number(s, pool)]
            turn.checks.append(ok("numbers_all") if not missing else
                               bad("numbers_all", [s["value"] for s in want], pool,
                                   "回答里没有出现 %s" % "、".join(fmt_num(v) for v in missing)))

        if "numbers_any" in checks:
            want = spec_values(checks["numbers_any"])
            hit = [s["value"] for s in want if find_number(s, pool)]
            turn.checks.append(ok("numbers_any") if hit else
                               bad("numbers_any", [s["value"] for s in want], pool,
                                   "回答里一个期望数字都没有"))

        if "numbers_none" in checks:
            want = spec_values(checks["numbers_none"])
            hit = [s["value"] for s in want if find_number(s, pool)]
            turn.checks.append(ok("numbers_none") if not hit else
                               bad("numbers_none", [s["value"] for s in want], pool,
                                   "回答里出现了不该出现的数字 %s"
                                   % "、".join(fmt_num(v) for v in hit)))

        if "numbers_none_beyond_question" in checks:
            turn.checks.append(self._check_no_new_numbers(
                checks["numbers_none_beyond_question"], question, pool, answer_text))

        if "signed_delta" in checks:
            turn.checks.append(self._check_signed_delta(
                checks["signed_delta"], pool, norm_answer, answer_text))

        for label in ("fact_all", "fact_any"):
            if label in checks:
                turn.checks.append(self._check_facts(
                    label, checks[label], cite_pairs, norm_answer, pool,
                    answer_text))

        if "text_all" in checks:
            want = checks["text_all"]
            missing = [t for t in want if normalize_text(t) not in norm_answer]
            turn.checks.append(ok("text_all") if not missing else
                               bad("text_all", want, shorten(answer_text, 200),
                                   "回答里没有提到 %s" % "、".join(missing)))

        if "text_any" in checks:
            want = checks["text_any"]
            hit = [t for t in want if normalize_text(t) in norm_answer]
            turn.checks.append(ok("text_any") if hit else
                               bad("text_any", want, shorten(answer_text, 200),
                                   "回答里一个期望说法都没有"))

        if "text_none" in checks:
            want = checks["text_none"]
            hit = [t for t in want if normalize_text(t) in norm_answer]
            turn.checks.append(ok("text_none") if not hit else
                               bad("text_none", want, shorten(answer_text, 200),
                                   "回答里出现了不该出现的说法 %s" % "、".join(hit)))

        def dump_hint(names) -> str:
            hit = [d for d in names if d in dumped]
            if not hit:
                return ""
            return ("（%s 的 quote 超过 %d 字，整篇贴上来不算引用）"
                    % ("、".join(hit), FACT_QUOTE_MAX))

        if "cite_all" in checks:
            want = checks["cite_all"]
            missing = [d for d in want if d not in doc_ids]
            turn.checks.append(ok("cite_all") if not missing else
                               bad("cite_all", want, doc_ids,
                                   "没有引用 %s%s" % ("、".join(missing),
                                                     dump_hint(missing))))

        if "cite_any" in checks:
            want = checks["cite_any"]
            hit = [d for d in want if d in doc_ids]
            turn.checks.append(ok("cite_any") if hit else
                               bad("cite_any", want, doc_ids,
                                   "一个期望的文档都没有引用%s" % dump_hint(want)))

        if "cite_none" in checks:
            want = checks["cite_none"]
            hit = [d for d in want if d in doc_ids_all]
            turn.checks.append(ok("cite_none") if not hit else
                               bad("cite_none", want, doc_ids_all,
                                   "引用了不该引用的 %s" % "、".join(hit)))

        if "cite_max" in checks:
            limit = int(checks["cite_max"])
            distinct = sorted(set(doc_ids))
            turn.checks.append(ok("cite_max") if len(distinct) <= limit else
                               bad("cite_max", limit, distinct,
                                   "引用了 %d 份文档，最多允许 %d 份"
                                   % (len(distinct), limit)))

        if checks.get("quotes_verbatim", True) and cite_pairs:
            reasons = []
            for doc_id, quote in cite_pairs:
                reason = self.kb.check_quote(doc_id, quote)
                if reason:
                    reasons.append(reason)
            turn.checks.append(ok("quotes_verbatim") if not reasons else
                               bad("quotes_verbatim", "每条 quote 都是原文里的连续文字",
                                   shorten("；".join(reasons), 300), reasons[0]))

        if checks.get("evidence_required"):
            turn.checks.append(self._check_evidence(checks, evidence))

    def _check_trace(self, data: dict) -> tuple[Check, float]:
        """契约 §6：每次回答都要能用 trace_id 取回完整处理过程。"""
        label = "trace_required"
        trace_id = data.get("trace_id")
        if not isinstance(trace_id, str) or not trace_id.strip():
            return bad(label, "非空的 trace_id", trace_id,
                       "回答里没有 trace_id，调试面板就无从查起"), 0.0
        resp = self.client.get("/api/trace/" + urllib.parse.quote(trace_id, safe=""))
        if not resp.ok:
            return bad(label, "GET /api/trace/{trace_id} 返回 200 + JSON",
                       resp.status, "取 trace 失败：%s" % resp.error), resp.seconds
        if not resp.data:
            return bad(label, "非空的 trace 内容", resp.data,
                       "trace 是空的（契约要求能看到检索、查询、提示词与耗时）"), resp.seconds
        return ok(label), resp.seconds

    # -- 三个"只看事实、不看措辞"的检查 --------------------------------
    @staticmethod
    def _check_no_new_numbers(spec, question: str, pool: list[float],
                              answer_text: str) -> Check:
        """拒答题：回答里不许出现问句里没有的数字（编造的金额、人数、工资）。

        日期、时间、门店商品编号本来就不算答案数字；小于 ``min``（默认 10）的
        数字是"五家门店""四个月"这类结构性说法，不算编造。
        """
        label = "numbers_none_beyond_question"
        minimum = float(spec.get("min", 10)) if isinstance(spec, dict) else 10.0
        allowed = extract_numbers(question)
        extra = [n for n in pool if abs(n) >= minimum
                 and not any(close_enough(n, a, 0) for a in allowed)]
        if extra:
            return bad(label, "只允许出现问句里已有的数字：%s"
                       % ("、".join(fmt_num(v) for v in allowed) or "（问句里没有数字）"),
                       shorten(answer_text, 200),
                       "回答里凭空出现了 %s" % "、".join(fmt_num(v) for v in extra))
        return ok(label)

    @staticmethod
    def _check_citation_hygiene(cite_pairs, dumped) -> Check:
        """引用卫生：一轮最多引 4 份文档，整篇贴进 quote 的不算引用。"""
        label = "citation_hygiene"
        distinct = sorted({d for d, _ in cite_pairs})
        problems = []
        if len(distinct) > MAX_CITED_DOCS:
            problems.append("引用了 %d 份文档，最多 %d 份（把知识库全引一遍不是引用）"
                            % (len(distinct), MAX_CITED_DOCS))
        if dumped:
            problems.append("%s 的 quote 超过 %d 字，等于把文档整篇贴上来，不算引用"
                            % ("、".join(dumped), FACT_QUOTE_MAX))
        if problems:
            return bad(label, "最多 %d 份文档，每条 quote 不超过 %d 字"
                       % (MAX_CITED_DOCS, FACT_QUOTE_MAX), distinct,
                       "；".join(problems))
        return ok(label)

    @staticmethod
    def _check_evidence_hygiene(evidence: list) -> Check:
        """证据卫生：result 不许无限大，SQL 必须是一条真的 SELECT。"""
        label = "evidence_hygiene"
        problems, total_numbers = [], 0
        for i, item in enumerate(evidence, 1):
            if not isinstance(item, dict):
                problems.append("第 %d 条不是对象" % i)
                continue
            blob = json.dumps(item.get("result"), ensure_ascii=False, default=str)
            if len(blob.encode("utf-8")) > MAX_EVIDENCE_RESULT_BYTES:
                problems.append("第 %d 条的 result 超过 %d 字节"
                                % (i, MAX_EVIDENCE_RESULT_BYTES))
            total_numbers += len(extract_numbers(blob))
            sql = item.get("sql")
            if sql is not None:
                problems.extend("第 %d 条的 sql %s" % (i, p)
                                for p in sql_problems(sql))
        if total_numbers > MAX_EVIDENCE_NUMBERS:
            problems.append("全部 result 里一共 %d 个数字，超过 %d"
                            "（穷举数字不是证据）" % (total_numbers,
                                                    MAX_EVIDENCE_NUMBERS))
        if problems:
            return bad(label, "result 不超过 %d 字节、数字不超过 %d 个、sql 是单条 SELECT"
                       % (MAX_EVIDENCE_RESULT_BYTES, MAX_EVIDENCE_NUMBERS),
                       shorten(evidence, 200), "；".join(problems[:4]))
        return ok(label)

    @staticmethod
    def _check_signed_delta(spec, pool: list[float], norm_answer: str,
                            answer_text: str) -> Check:
        """涨跌题：要有差额这个数，要说清方向，而且只能说一个方向。

        同时写上"上涨"和"下跌"不是稳妥，是没有回答——方向题的答案只有一个方向。
        """
        label = "signed_delta"
        value = float(spec.get("value", 0))
        tol = float(spec.get("tol", 0) or 0)
        words = spec.get("words") or []
        forbidden = spec.get("words_none") or []
        has_number = (find_number({"value": abs(value), "tol": tol}, pool)
                      or find_number({"value": value, "tol": tol}, pool))
        hit_word = [w for w in words if normalize_text(w) in norm_answer]
        wrong_way = [w for w in forbidden if normalize_text(w) in norm_answer]
        if has_number and hit_word and not wrong_way:
            return ok(label)
        problems = []
        if not has_number:
            problems.append("没有写出差额 %s" % fmt_num(abs(value)))
        if not hit_word:
            problems.append("没有说清方向（%s 里要出现一个）" % "、".join(words))
        if wrong_way:
            problems.append("同时出现了相反方向的说法 %s，方向没有结论"
                            % "、".join(wrong_way))
        return bad(label, {"delta": abs(value), "words": words,
                           "words_none": forbidden},
                   {"numbers": pool, "answer": shorten(answer_text, 120)},
                   "；".join(problems))

    def _check_facts(self, label: str, spec, cite_pairs, norm_answer: str,
                     pool: list[float], answer_text: str) -> Check:
        """文档事实：出现在回答里**或**出现在对应文档的 quote 里都算。

        quote 另有逐字校验兜底，所以这样既奖励"检索对了并如实引用"，
        又不会因为模型换了个说法就判错。
        """
        docs = spec.get("docs") or []
        texts = spec.get("texts") or []
        numbers = spec_values(spec.get("numbers"))
        # 只有"摘出来的那一句"才算引用：整篇文档原样贴进 quote 不能当作答对。
        usable = [(doc_id, quote) for doc_id, quote in cite_pairs
                  if doc_id in docs and quote
                  and len(normalize_doc(quote)) <= FACT_QUOTE_MAX]
        quotes = [normalize_text(quote) for _, quote in usable]
        quote_numbers: list[float] = []
        for _, quote in usable:
            quote_numbers.extend(extract_numbers(quote))

        found, missing = [], []
        for item in texts:
            needle = normalize_text(item)
            if needle in norm_answer or any(needle in q for q in quotes):
                found.append(item)
            else:
                missing.append(item)
        for item in numbers:
            if find_number(item, pool) or find_number(item, quote_numbers):
                found.append(fmt_num(item["value"]))
            else:
                missing.append(fmt_num(item["value"]))

        wanted = [str(t) for t in texts] + [fmt_num(n["value"]) for n in numbers]
        actual = {"answer": shorten(answer_text, 160),
                  "quoted": [d for d, _ in cite_pairs if d in docs]}
        if label == "fact_any":
            if found:
                return ok(label)
            return bad(label, {"docs": docs, "any_of": wanted}, actual,
                       "回答里没提到，%s 的 quote 里也没有：%s%s"
                       % ("/".join(docs) or "相关文档", "、".join(wanted),
                          self._quote_hint(cite_pairs, docs)))
        if missing:
            return bad(label, {"docs": docs, "all_of": wanted}, actual,
                       "回答里没提到，%s 的 quote 里也没有：%s%s"
                       % ("/".join(docs) or "相关文档", "、".join(missing),
                          self._quote_hint(cite_pairs, docs)))
        return ok(label)

    @staticmethod
    def _quote_hint(cite_pairs, docs) -> str:
        too_long = [doc_id for doc_id, quote in cite_pairs
                    if doc_id in docs and quote
                    and len(normalize_doc(quote)) > FACT_QUOTE_MAX]
        if not too_long:
            return ""
        return ("（%s 的 quote 超过 %d 字，整段贴文档不算引用，请摘出相关的那一句）"
                % ("/".join(sorted(set(too_long))), FACT_QUOTE_MAX))

    def _check_evidence(self, checks: dict, evidence) -> Check:
        label = "evidence_required"
        if not isinstance(evidence, list) or not evidence:
            return bad(label, "非空的 data_evidence", evidence,
                       "回答里的数字没有给出对应的数据库查询")
        problems, pool = [], []
        for item in evidence:
            if not isinstance(item, dict):
                problems.append("data_evidence 里出现了非对象元素")
                continue
            if not item.get("tool") and not item.get("sql"):
                problems.append("有一条证据既没有 tool 也没有 sql")
            if "result" not in item:
                problems.append("有一条证据没有 result")
                continue
            pool.extend(numbers_in_object(item.get("result")))
        wanted = checks.get("evidence_numbers")
        if wanted is None:
            wanted = checks.get("numbers_all") or []
        want = spec_values(wanted)
        missing = [s["value"] for s in want if not find_number(s, pool)]
        if missing:
            problems.append("这些数字在 data_evidence 的 result 里找不到：%s"
                            % "、".join(fmt_num(v) for v in missing))
        any_of = spec_values(checks.get("evidence_numbers_any"))
        if any_of and not any(find_number(s, pool) for s in any_of):
            problems.append("data_evidence 的 result 里至少要能找到 %s 中的一个"
                            % "、".join(fmt_num(s["value"]) for s in any_of))
        if problems:
            expected = [s["value"] for s in want] or [s["value"] for s in any_of]
            return bad(label, expected or "非空的 data_evidence",
                       shorten(evidence, 300), "；".join(problems))
        return ok(label)

    def _check_metrics_unchanged(self, post: dict) -> tuple[Check, float]:
        label = "post.metrics_unchanged"
        rng = self._range_for(post)
        baseline = self.baselines.get(rng) if rng else None
        if baseline is None:
            reason = (self.baseline_errors.get(rng)
                      or "开跑前没有取到基准（题库里也没写明区间）")
            return bad(label, "与开跑前一致", None, reason), 0.0
        resp = self.client.get("/api/metrics/summary",
                               {"start": rng[0], "end": rng[1]})
        if not resp.ok:
            return bad(label, "与开跑前一致", resp.status, resp.error), resp.seconds
        diffs = []
        for name in ("net_revenue", "refund_amount", "orders", "aov", "qty"):
            before, after = baseline.get(name), resp.data.get(name)
            if before != after:
                diffs.append("%s：%s → %s" % (name, before, after))
        if diffs:
            return bad(label, baseline, resp.data,
                       "这一题之后 %s 到 %s 的数字被改动了（%s）"
                       % (rng[0], rng[1], "；".join(diffs))), resp.seconds
        return ok(label), resp.seconds


_SQL_WRITE_WORDS = frozenset({"insert", "update", "delete", "drop", "alter",
                              "create", "attach", "pragma", "truncate", "vacuum"})
_SQL_TOKEN_RE = re.compile(r"\b\w+\b")
_SQL_QUOTES = {"'": "'", '"': '"', "`": "`", "[": "]"}


def sql_skeleton(sql: str) -> tuple[str, int]:
    """把 SQL 里的注释、字符串字面量、带引号的标识符全部抹成空白。

    返回 (骨架, 顶层语句数)。一次从左到右扫描，所以 `'--'`、`'/*'` 这类藏在
    字符串里的"注释符"不会被误当成注释，`/* FROM sales */` 里的 FROM 也不会
    被当成真的 FROM。字符串和标识符按 SQLite 的四种引法处理（`'…'`、`"…"`、
    `` `…` ``、`[…]`），成对的引号（`''`）视为转义。
    """
    out: list[str] = []
    statements, has_content = 0, False
    i, n = 0, len(sql)
    while i < n:
        ch = sql[i]
        if ch == "-" and sql.startswith("--", i):
            end = sql.find("\n", i)
            i = n if end == -1 else end
            out.append(" ")
            continue
        if ch == "/" and sql.startswith("/*", i):
            end = sql.find("*/", i + 2)
            i = n if end == -1 else end + 2
            out.append(" ")
            continue
        if ch in _SQL_QUOTES:
            close = _SQL_QUOTES[ch]
            j = i + 1
            while j < n:
                if sql[j] == close:
                    if close != "]" and j + 1 < n and sql[j + 1] == close:
                        j += 2              # '' / "" / `` 是转义
                        continue
                    break
                j += 1
            i = j + 1
            out.append(" ")
            has_content = True
            continue
        if ch == ";":
            if has_content:
                statements += 1
            has_content = False
            out.append(" ")
            i += 1
            continue
        if not ch.isspace():
            has_content = True
        out.append(ch)
        i += 1
    if has_content:
        statements += 1
    return "".join(out), statements


def sql_problems(sql) -> list[str]:
    """``data_evidence.sql`` 必须是**一条**真的只读查询。

    词法判断，不做子串匹配：`updated_count` 不是 `update`，
    `WHERE payment = 'update'` 里的 update 是字符串，`FROM` 前面换行也照样认。
    `SELECT 0 /* fabricated */`、`select 0 -- from sales` 这类常量语句
    不查任何表，拿它当证据等于没有证据。
    """
    if not isinstance(sql, str) or not sql.strip():
        return ["不是非空字符串"]
    skeleton, statements = sql_skeleton(sql)
    tokens = _SQL_TOKEN_RE.findall(skeleton.lower())
    problems = []
    if statements > 1:
        problems.append("包含多条语句（只允许一条 SELECT）")
    if not tokens or tokens[0] not in ("select", "with"):
        problems.append("不是以 SELECT（或 WITH … SELECT）开头")
    if "from" not in tokens:
        problems.append("没有 FROM，没有真的查过任何表")
    writes = sorted(set(tokens) & _SQL_WRITE_WORDS)
    if any(a == "replace" and b == "into" for a, b in zip(tokens, tokens[1:])):
        writes.append("replace into")
    if writes:
        problems.append("出现了写操作关键字 %s" % "、".join(writes))
    return problems


def fmt_num(value) -> str:
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


# ======================================================================
# 六、题库加载与报告
# ======================================================================

def load_questions(path: str) -> list[dict]:
    questions = []
    with open(path, encoding="utf-8") as fh:
        for lineno, line in enumerate(fh, 1):
            line = line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
            except ValueError as exc:
                raise SystemExit("题库第 %d 行不是合法 JSON：%s" % (lineno, exc))
            if not isinstance(item, dict) or "id" not in item:
                raise SystemExit("题库第 %d 行缺少 id" % lineno)
            questions.append(item)
    return questions


def build_report(results: list[QuestionResult], evaluator: Evaluator,
                 args) -> dict:
    per_category: dict[str, dict] = {}
    for res in results:
        entry = per_category.setdefault(
            res.category, {"points": 0.0, "earned": 0.0, "questions": 0, "passed": 0})
        entry["points"] += res.points
        entry["earned"] += res.earned
        entry["questions"] += 1
        entry["passed"] += 1 if res.passed else 0
    for entry in per_category.values():
        entry["ratio"] = round(entry["earned"] / entry["points"], 4) if entry["points"] else 0.0
        entry["earned"] = round(entry["earned"], 2)
        entry["points"] = round(entry["points"], 2)

    latencies = [round(r.seconds, 3) for r in results]
    total_points = sum(r.points for r in results)
    total_earned = sum(r.earned for r in results)
    return {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "base_url": evaluator.client.base_url,
        "questions_file": os.path.abspath(args.questions),
        "kb_dir": os.path.abspath(args.kb) if args.kb else None,
        "kb_docs_loaded": len(evaluator.kb.docs),
        "timeout": evaluator.client.timeout,
        "only": args.only,
        "total": {
            "points": round(total_points, 2),
            "earned": round(total_earned, 2),
            "ratio": round(total_earned / total_points, 4) if total_points else 0.0,
            "questions": len(results),
            "passed": sum(1 for r in results if r.passed),
        },
        "per_category": {k: per_category[k] for k in
                         sorted(per_category, key=lambda c: (
                             CATEGORY_ORDER.index(c) if c in CATEGORY_ORDER else 99, c))},
        "latency_seconds": {
            "median": round(statistics.median(latencies), 3) if latencies else None,
            "max": max(latencies) if latencies else None,
            "total": round(sum(latencies), 3),
        },
        "health": evaluator.health,
        "health_error": evaluator.health_error,
        "metrics_baselines": {"%s..%s" % rng: data
                              for rng, data in evaluator.baselines.items()},
        "metrics_baseline_errors": {"%s..%s" % rng: err
                                    for rng, err in evaluator.baseline_errors.items()},
        "kb_errors": evaluator.kb.errors,
        "questions": [
            {
                "id": r.qid,
                "category": r.category,
                "points": r.points,
                "earned": round(r.earned, 2),
                "passed": r.passed,
                "latency_seconds": round(r.seconds, 3),
                "turns": [
                    {
                        "question": t.question,
                        "passed": t.passed,
                        "latency_seconds": round(t.seconds, 3),
                        "answer_type": t.answer_type,
                        "citations": t.citations,
                        "trace_id": t.trace_id,
                        "answer": t.answer,
                        "checks": [c.to_json() for c in t.checks],
                    } for t in r.turns
                ],
            } for r in results
        ],
    }


def render_markdown(report: dict) -> str:
    lines: list[str] = []
    total = report["total"]
    lines.append("# 评测报告")
    lines.append("")
    lines.append("- 服务地址：`%s`" % report["base_url"])
    lines.append("- 题库：`%s`" % report["questions_file"])
    lines.append("- 生成时间：%s" % report["generated_at"])
    lines.append("- 知识库：载入 %d 份文档（用于 quote 逐字校验）" % report["kb_docs_loaded"])
    lines.append("")
    lines.append("## 总分")
    lines.append("")
    lines.append("**%.2f / %.2f（%.1f%%）**，%d 题全绿 / 共 %d 题。"
                 % (total["earned"], total["points"], total["ratio"] * 100,
                    total["passed"], total["questions"]))
    lines.append("")
    lat = report["latency_seconds"]
    if lat["median"] is not None:
        lines.append("每题耗时：中位数 %.2f 秒，最大 %.2f 秒，合计 %.1f 秒。"
                     % (lat["median"], lat["max"], lat["total"]))
        lines.append("")
    lines.append("## 分类别")
    lines.append("")
    lines.append("| 类别 | 得分 | 满分 | 比例 | 全绿题数 |")
    lines.append("|---|---|---|---|---|")
    for cat, entry in report["per_category"].items():
        lines.append("| %s（`%s`） | %.2f | %.2f | %.1f%% | %d / %d |"
                     % (CATEGORY_LABEL.get(cat, cat), cat, entry["earned"],
                        entry["points"], entry["ratio"] * 100,
                        entry["passed"], entry["questions"]))
    lines.append("")

    lines.append("## `/api/health` 快照")
    lines.append("")
    if report["health"] is not None:
        lines.append("```json")
        lines.append(json.dumps(report["health"], ensure_ascii=False, indent=2))
        lines.append("```")
    else:
        lines.append("取不到：%s" % report["health_error"])
    lines.append("")

    failed = [q for q in report["questions"] if not q["passed"]]
    lines.append("## 没通过的题（%d 道）" % len(failed))
    lines.append("")
    if not failed:
        lines.append("没有。")
        lines.append("")
    for q in failed:
        lines.append("### %s（%s，%.2f / %.2f 分）"
                     % (q["id"], q["category"], q["earned"], q["points"]))
        lines.append("")
        for i, turn in enumerate(q["turns"], 1):
            mark = "通过" if turn["passed"] else "未通过"
            lines.append("- 第 %d 轮（%s）：%s" % (i, mark, turn["question"]))
            if turn["answer"]:
                lines.append("  - 回答：%s" % shorten(turn["answer"], 300))
            for check in turn["checks"]:
                if check["passed"]:
                    continue
                lines.append("  - ❌ `%s`：%s" % (check["name"], check["reason"]))
                lines.append("    - 期望：%s" % shorten(check.get("expected"), 200))
                lines.append("    - 实际：%s" % shorten(check.get("actual"), 200))
        lines.append("")

    lines.append("## 全部题目")
    lines.append("")
    lines.append("| 题号 | 类别 | 得分 | 满分 | 耗时（秒） |")
    lines.append("|---|---|---|---|---|")
    for q in report["questions"]:
        lines.append("| %s | %s | %.2f | %.2f | %.2f |"
                     % (q["id"], q["category"], q["earned"], q["points"],
                        q["latency_seconds"]))
    lines.append("")
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    here = os.path.dirname(os.path.abspath(__file__))
    parser = argparse.ArgumentParser(
        description="按题库评测一个符合 docs/API_CONTRACT.md 的服务")
    parser.add_argument("--base-url", default="http://localhost:8000",
                        help="服务地址，默认 http://localhost:8000")
    parser.add_argument("--questions", required=True, help="题库 JSONL 文件")
    parser.add_argument("--kb", default=os.path.join(here, os.pardir, "knowledge_base"),
                        help="知识库目录，用于 quote 逐字校验，默认 ../knowledge_base")
    parser.add_argument("--out", default=".", help="报告输出目录，默认当前目录")
    parser.add_argument("--only", default=None, choices=CATEGORY_ORDER,
                        help="只跑某一个类别")
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT,
                        help="单次请求超时秒数，默认 180")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    questions = load_questions(args.questions)
    kb = KnowledgeBase(args.kb)
    for err in kb.errors:
        print("提醒：%s" % err, file=sys.stderr)
    client = Client(args.base_url, args.timeout)
    evaluator = Evaluator(client, questions, kb, args.only)
    if not evaluator.questions:
        print("题库里没有可跑的题（--only %s）" % args.only, file=sys.stderr)
        return 1

    print("共 %d 题，逐题串行请求 %s，单次超时 %.0f 秒"
          % (len(evaluator.questions), client.base_url, client.timeout))
    evaluator.run()
    for res in evaluator.results:
        print("  %-6s %-10s %.2f / %.2f  %.1fs"
              % (res.qid, res.category, res.earned, res.points, res.seconds))

    report = build_report(evaluator.results, evaluator, args)
    os.makedirs(args.out, exist_ok=True)
    json_path = os.path.join(args.out, "report.json")
    md_path = os.path.join(args.out, "report.md")
    with open(json_path, "w", encoding="utf-8") as fh:
        json.dump(report, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    with open(md_path, "w", encoding="utf-8") as fh:
        fh.write(render_markdown(report))
    total = report["total"]
    print("总分 %.2f / %.2f（%.1f%%）-> %s, %s"
          % (total["earned"], total["points"], total["ratio"] * 100,
             json_path, md_path))
    return 0


if __name__ == "__main__":
    sys.exit(main())
