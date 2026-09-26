# 大模型接入预检报告

生成时间：2026-09-26T16:30:06.503+08:00。
被测服务：http://127.0.0.1:65244。
假模型地址：http://127.0.0.1:65245/ds-gw。
注入的模型名：preflight-model-7f3a。
工具版本：llm_gateway.py 2.0.0。
总体结论：**没有失败项**，14 项检查里 14 项通过。

## 检查结果一览

| 编号 | 检查项 | 结果 | 说明 |
|---|---|---|---|
| P1 | 服务确实把请求发到了注入的 LLM_BASE_URL（含路径前缀） | 通过 | 共观察到 60 次 POST /ds-gw/chat/completions。 |
| P2 | 请求里的 model 等于注入的 LLM_MODEL | 通过 | 全部请求都用了 preflight-model-7f3a。 |
| P3 | 注入的 Key 以 Authorization: Bearer 发送 | 通过 | 全部请求都带了正确的 Bearer Key。 |
| P4 | 只用了 DeepSeek 文档列出的顶层参数 | 通过 | 只出现了 DeepSeek 文档列出的顶层参数。 |
| P5 | max_tokens 不设，或不小于 2048 | 通过 | max_tokens 都不小于 2048。 |
| P6 | 没有访问 {prefix}/chat/completions 之外的任何路径 | 通过 | 只访问了 POST /ds-gw/chat/completions，没有碰任何别的路径。 |
| P7 | 工具定义规范，且每一个工具调用都以 role=tool + tool_call_id 回传 | 通过 | 工具定义规范，44 个工具调用的结果都正确回传了。 |
| P8 | 每个场景下 /api/chat 都返回 HTTP 200 与字段完整的合法 JSON | 通过 | 32 次问答全部返回 200 和字段完整的 JSON。 |
| P9 | 模型不可用时给出结构化 refusal，answer 从不是空串 | 通过 | 模型不可用的场景下都给了结构化 refusal 或有据可查的回答，answer 从不是空串。 |
| P10 | 思考内容没有漏进 answer / citations / data_evidence | 通过 | 32 次回答里，思考标记都没有出现在任何对外字段里。 |
| P11 | /api/chat 在时限内返回（含长时间无响应的场景） | 通过 | 最慢的一次是 120.20 秒，都在 180 秒以内。 |
| P12 | 注入环境变量后 /api/health 报告 llm_mode = live | 通过 | llm_mode = live。 |
| P13 | 多轮工具调用之间 reasoning_content 原样回传（没有触发 400） | 通过 | 18 次多轮请求都原样回传了 reasoning_content。 |
| P14 | 保持连接的空行与 SSE 注释没有把服务弄坏 | 通过 | 正文前的空行和 SSE 的 `: keep-alive` 注释都被正确跳过了，slow 场景照常给出回答。 |

## 逐项证据

### P1 服务确实把请求发到了注入的 LLM_BASE_URL（含路径前缀）

结果：通过。
说明：共观察到 60 次 POST /ds-gw/chat/completions。

```json
{
  "expected_base_url": "http://127.0.0.1:65245/ds-gw",
  "expected_path": "/ds-gw/chat/completions",
  "chat_completions_requests": 60,
  "per_scenario": {
    "normal": 6,
    "thinking_starved": 6,
    "empty_content": 4,
    "json_empty": 6,
    "bad_tool_args": 4,
    "content_filter": 2,
    "insufficient_resource": 4,
    "aborted": 2,
    "http_401": 2,
    "http_402": 2,
    "http_422": 2,
    "http_429": 4,
    "http_500": 4,
    "http_503": 4,
    "slow": 6,
    "hang": 2
  }
}
```

### P2 请求里的 model 等于注入的 LLM_MODEL

结果：通过。
说明：全部请求都用了 preflight-model-7f3a。

```json
{
  "expected": "preflight-model-7f3a",
  "seen": {
    "preflight-model-7f3a": 60
  }
}
```

### P3 注入的 Key 以 Authorization: Bearer 发送

结果：通过。
说明：全部请求都带了正确的 Bearer Key。

```json
{
  "requests": 60,
  "missing_authorization_in": [],
  "non_bearer_schemes": [],
  "key_mismatch_in": []
}
```

### P4 只用了 DeepSeek 文档列出的顶层参数

结果：通过。
说明：只出现了 DeepSeek 文档列出的顶层参数。

```json
{
  "documented": [
    "frequency_penalty",
    "logprobs",
    "max_tokens",
    "messages",
    "model",
    "presence_penalty",
    "reasoning_effort",
    "response_format",
    "stop",
    "stream",
    "stream_options",
    "temperature",
    "thinking",
    "tool_choice",
    "tools",
    "top_logprobs",
    "top_p",
    "user_id"
  ],
  "extra_params": {}
}
```

### P5 max_tokens 不设，或不小于 2048

结果：通过。
说明：max_tokens 都不小于 2048。

```json
{
  "min_required": 2048,
  "values_seen": [
    "4096"
  ],
  "offenders": []
}
```

### P6 没有访问 {prefix}/chat/completions 之外的任何路径

结果：通过。
说明：只访问了 POST /ds-gw/chat/completions，没有碰任何别的路径。

```json
{
  "allowed": "POST /ds-gw/chat/completions",
  "requests_seen": 60,
  "other_paths": []
}
```

### P7 工具定义规范，且每一个工具调用都以 role=tool + tool_call_id 回传

结果：通过。
说明：工具定义规范，44 个工具调用的结果都正确回传了。

```json
{
  "declared_tools": true,
  "announced_tool_calls": 44,
  "returned_tool_results": 44,
  "problems": []
}
```

### P8 每个场景下 /api/chat 都返回 HTTP 200 与字段完整的合法 JSON

结果：通过。
说明：32 次问答全部返回 200 和字段完整的 JSON。

```json
{
  "attempts": 32,
  "failures": []
}
```

### P9 模型不可用时给出结构化 refusal，answer 从不是空串

结果：通过。
说明：模型不可用的场景下都给了结构化 refusal 或有据可查的回答，answer 从不是空串。

```json
{
  "failing_scenarios": [
    "aborted",
    "bad_tool_args",
    "content_filter",
    "empty_content",
    "hang",
    "http_401",
    "http_402",
    "http_422",
    "http_429",
    "http_500",
    "http_503",
    "insufficient_resource"
  ],
  "checked": 24,
  "skipped_invalid_responses": 0,
  "failure_marker": "FAILOUT-fe45c2885561",
  "declared_tools": [
    "by_store",
    "by_store_category",
    "compare_periods",
    "daily_metrics",
    "payment_mix",
    "query_metrics",
    "run_sql",
    "search_kb",
    "top_products",
    "unit_price_check"
  ],
  "problems": []
}
```

### P10 思考内容没有漏进 answer / citations / data_evidence

结果：通过。
说明：32 次回答里，思考标记都没有出现在任何对外字段里。

```json
{
  "marker": "RSN-b41f3a2d1ce0",
  "inspected_answers": 32,
  "responses_carrying_the_marker": 40,
  "requests_with_thinking_disabled": 0,
  "leaks": []
}
```

### P11 /api/chat 在时限内返回（含长时间无响应的场景）

结果：通过。
说明：最慢的一次是 120.20 秒，都在 180 秒以内。

```json
{
  "limit_seconds": 180.0,
  "timed_attempts": 32,
  "slowest": [
    {
      "scenario": "hang",
      "seconds": 120.2
    },
    {
      "scenario": "hang",
      "seconds": 120.17
    },
    {
      "scenario": "slow",
      "seconds": 18.66
    },
    {
      "scenario": "slow",
      "seconds": 18.56
    },
    {
      "scenario": "http_500",
      "seconds": 1.38
    }
  ],
  "offenders": [],
  "unreachable_attempts": []
}
```

### P12 注入环境变量后 /api/health 报告 llm_mode = live

结果：通过。
说明：llm_mode = live。

```json
{
  "status": 200,
  "error": null,
  "body": {
    "status": "ok",
    "llm_mode": "live",
    "kb_docs": 35,
    "kb_chunks": 131,
    "valid_sales_rows": 18290,
    "today": "2026-09-01",
    "data_period": {
      "start": "2026-05-01",
      "end": "2026-08-31"
    },
    "cleaning_report": {
      "raw_rows": 18628,
      "removed": {
        "1_unparseable_date": 8,
        "2_empty_amount": 150,
        "3_qty_le_zero": 30,
        "4_store_not_in_stores": 10,
        "5_product_not_in_products": 40,
        "6_duplicate_row": 100,
        "note_unparseable_amount": 0
      },
      "kept_rows": 18290,
      "kept_sales_rows": 18196,
      "kept_refund_rows": 94
    },
    "index_key": "42b07fe808f7",
    "kb_warnings": [
      "非 UTF-8，已按 GBK 解码：KB-062_旧OA导出_营业时间调整通知.txt",
      "跳过没有 KB 编号的文件：README.md"
    ]
  }
}
```

### P13 多轮工具调用之间 reasoning_content 原样回传（没有触发 400）

结果：通过。
说明：18 次多轮请求都原样回传了 reasoning_content。

```json
{
  "requests_rejected_with_400": [],
  "requests_echoing_reasoning": 18,
  "requests_with_thinking_disabled": 0
}
```

### P14 保持连接的空行与 SSE 注释没有把服务弄坏

结果：通过。
说明：正文前的空行和 SSE 的 `: keep-alive` 注释都被正确跳过了，slow 场景照常给出回答。

```json
{
  "normal_answered": true,
  "slow_answered": true,
  "slow_statuses": [
    200,
    200
  ],
  "slow_answer_types": [
    "doc",
    "data"
  ]
}
```

## 各场景明细

| 场景 | 问题 | HTTP | 耗时（秒） | answer_type | answer 摘要 | 模型请求数 |
|---|---|---|---|---|---|---|
| normal | 你们的退款规则是怎么规定的？ | 200 | 0.59 | doc | KB-001《指标口径手册 v3》（2026-05-01 起生效）：- **退款行**：通过上述剔除规则后 `amoun…<已截断 58 字符> | 6 |
| normal | 最近一段时间的整体经营情况怎么样？ | 200 | 0.55 | data | 2026-05-01 至 2026-08-31（全部门店）：净营业额 646929.00 元，有效订单数 17926 单…<已截断 39 字符> | 6 |
| thinking_starved | 你们的退款规则是怎么规定的？ | 200 | 0.56 | doc | KB-001《指标口径手册 v3》（2026-05-01 起生效）：- **退款行**：通过上述剔除规则后 `amoun…<已截断 58 字符> | 6 |
| thinking_starved | 最近一段时间的整体经营情况怎么样？ | 200 | 0.53 | data | 2026-05-01 至 2026-08-31（全部门店）：净营业额 646929.00 元，有效订单数 17926 单…<已截断 39 字符> | 6 |
| empty_content | 你们的退款规则是怎么规定的？ | 200 | 1.36 | refusal | 模型服务这次没有正常返回（返回了空回答），为了不给出没有依据的数字，这个问题先不回答。可以稍后重试；失败的真实原因记在 …<已截断 8 字符> | 4 |
| empty_content | 最近一段时间的整体经营情况怎么样？ | 200 | 1.36 | refusal | 模型服务这次没有正常返回（返回了空回答），为了不给出没有依据的数字，这个问题先不回答。可以稍后重试；失败的真实原因记在 …<已截断 8 字符> | 4 |
| json_empty | 你们的退款规则是怎么规定的？ | 200 | 0.56 | doc | KB-001《指标口径手册 v3》（2026-05-01 起生效）：- **退款行**：通过上述剔除规则后 `amoun…<已截断 58 字符> | 6 |
| json_empty | 最近一段时间的整体经营情况怎么样？ | 200 | 0.56 | data | 2026-05-01 至 2026-08-31（全部门店）：净营业额 646929.00 元，有效订单数 17926 单…<已截断 39 字符> | 6 |
| bad_tool_args | 你们的退款规则是怎么规定的？ | 200 | 0.36 | refusal | 模型服务这次没有正常返回（工具参数无法解析），为了不给出没有依据的数字，这个问题先不回答。可以稍后重试；失败的真实原因记…<已截断 10 字符> | 4 |
| bad_tool_args | 最近一段时间的整体经营情况怎么样？ | 200 | 0.36 | refusal | 模型服务这次没有正常返回（工具参数无法解析），为了不给出没有依据的数字，这个问题先不回答。可以稍后重试；失败的真实原因记…<已截断 10 字符> | 4 |
| content_filter | 你们的退款规则是怎么规定的？ | 200 | 0.19 | refusal | 模型服务这次没有正常返回（被内容过滤拦截），为了不给出没有依据的数字，这个问题先不回答。可以稍后重试；失败的真实原因记在…<已截断 9 字符> | 2 |
| content_filter | 最近一段时间的整体经营情况怎么样？ | 200 | 0.17 | refusal | 模型服务这次没有正常返回（被内容过滤拦截），为了不给出没有依据的数字，这个问题先不回答。可以稍后重试；失败的真实原因记在…<已截断 9 字符> | 2 |
| insufficient_resource | 你们的退款规则是怎么规定的？ | 200 | 1.34 | refusal | 模型服务这次没有正常返回（服务端资源不足），为了不给出没有依据的数字，这个问题先不回答。可以稍后重试；失败的真实原因记在…<已截断 9 字符> | 4 |
| insufficient_resource | 最近一段时间的整体经营情况怎么样？ | 200 | 1.34 | refusal | 模型服务这次没有正常返回（服务端资源不足），为了不给出没有依据的数字，这个问题先不回答。可以稍后重试；失败的真实原因记在…<已截断 9 字符> | 4 |
| aborted | 你们的退款规则是怎么规定的？ | 200 | 0.19 | refusal | 模型服务这次没有正常返回（请求被中止），为了不给出没有依据的数字，这个问题先不回答。可以稍后重试；失败的真实原因记在 t…<已截断 7 字符> | 2 |
| aborted | 最近一段时间的整体经营情况怎么样？ | 200 | 0.20 | refusal | 模型服务这次没有正常返回（请求被中止），为了不给出没有依据的数字，这个问题先不回答。可以稍后重试；失败的真实原因记在 t…<已截断 7 字符> | 2 |
| http_401 | 你们的退款规则是怎么规定的？ | 200 | 0.17 | refusal | 模型服务这次没有正常返回（接口返回错误码 401），为了不给出没有依据的数字，这个问题先不回答。可以稍后重试；失败的真实…<已截断 13 字符> | 2 |
| http_401 | 最近一段时间的整体经营情况怎么样？ | 200 | 0.17 | refusal | 模型服务这次没有正常返回（接口返回错误码 401），为了不给出没有依据的数字，这个问题先不回答。可以稍后重试；失败的真实…<已截断 13 字符> | 2 |
| http_402 | 你们的退款规则是怎么规定的？ | 200 | 0.20 | refusal | 模型服务这次没有正常返回（接口返回错误码 402），为了不给出没有依据的数字，这个问题先不回答。可以稍后重试；失败的真实…<已截断 13 字符> | 2 |
| http_402 | 最近一段时间的整体经营情况怎么样？ | 200 | 0.19 | refusal | 模型服务这次没有正常返回（接口返回错误码 402），为了不给出没有依据的数字，这个问题先不回答。可以稍后重试；失败的真实…<已截断 13 字符> | 2 |
| http_422 | 你们的退款规则是怎么规定的？ | 200 | 0.19 | refusal | 模型服务这次没有正常返回（接口返回错误码 422），为了不给出没有依据的数字，这个问题先不回答。可以稍后重试；失败的真实…<已截断 13 字符> | 2 |
| http_422 | 最近一段时间的整体经营情况怎么样？ | 200 | 0.17 | refusal | 模型服务这次没有正常返回（接口返回错误码 422），为了不给出没有依据的数字，这个问题先不回答。可以稍后重试；失败的真实…<已截断 13 字符> | 2 |
| http_429 | 你们的退款规则是怎么规定的？ | 200 | 1.36 | refusal | 模型服务这次没有正常返回（接口返回错误码 429），为了不给出没有依据的数字，这个问题先不回答。可以稍后重试；失败的真实…<已截断 13 字符> | 4 |
| http_429 | 最近一段时间的整体经营情况怎么样？ | 200 | 1.36 | refusal | 模型服务这次没有正常返回（接口返回错误码 429），为了不给出没有依据的数字，这个问题先不回答。可以稍后重试；失败的真实…<已截断 13 字符> | 4 |
| http_500 | 你们的退款规则是怎么规定的？ | 200 | 1.34 | refusal | 模型服务这次没有正常返回（接口返回错误码 500），为了不给出没有依据的数字，这个问题先不回答。可以稍后重试；失败的真实…<已截断 13 字符> | 4 |
| http_500 | 最近一段时间的整体经营情况怎么样？ | 200 | 1.38 | refusal | 模型服务这次没有正常返回（接口返回错误码 500），为了不给出没有依据的数字，这个问题先不回答。可以稍后重试；失败的真实…<已截断 13 字符> | 4 |
| http_503 | 你们的退款规则是怎么规定的？ | 200 | 1.33 | refusal | 模型服务这次没有正常返回（接口返回错误码 503），为了不给出没有依据的数字，这个问题先不回答。可以稍后重试；失败的真实…<已截断 13 字符> | 4 |
| http_503 | 最近一段时间的整体经营情况怎么样？ | 200 | 1.36 | refusal | 模型服务这次没有正常返回（接口返回错误码 503），为了不给出没有依据的数字，这个问题先不回答。可以稍后重试；失败的真实…<已截断 13 字符> | 4 |
| slow | 你们的退款规则是怎么规定的？ | 200 | 18.66 | doc | KB-001《指标口径手册 v3》（2026-05-01 起生效）：- **退款行**：通过上述剔除规则后 `amoun…<已截断 58 字符> | 6 |
| slow | 最近一段时间的整体经营情况怎么样？ | 200 | 18.56 | data | 2026-05-01 至 2026-08-31（全部门店）：净营业额 646929.00 元，有效订单数 17926 单…<已截断 39 字符> | 6 |
| hang | 你们的退款规则是怎么规定的？ | 200 | 120.17 | refusal | 模型服务这次没有正常返回（调用超时），为了不给出没有依据的数字，这个问题先不回答。可以稍后重试；失败的真实原因记在 tr…<已截断 6 字符> | 2 |
| hang | 最近一段时间的整体经营情况怎么样？ | 200 | 120.20 | refusal | 模型服务这次没有正常返回（调用超时），为了不给出没有依据的数字，这个问题先不回答。可以稍后重试；失败的真实原因记在 tr…<已截断 6 字符> | 2 |

## 场景说明

normal：模型一切正常；带 tools 时先返回两个工具调用，再返回一个，最后才给正文。
thinking_starved：只有当你把 max_tokens 设得小于 1024 时才会咬人，此时正文为空、finish_reason 为 length。
empty_content：没有 tool_calls 而正文是空串，finish_reason 仍然是 stop，这种要按错误处理。
json_empty：只有当你用了 response_format=json_object 时才会咬人，文档说 JSON 模式偶尔会返回空内容。
bad_tool_args：工具调用的 arguments 是被截断的非法 JSON，必须处理解析失败。
content_filter：finish_reason 为 content_filter，正文是半截话、不是空串。
insufficient_resource：finish_reason 为 insufficient_system_resource，正文为空。
aborted：finish_reason 为 aborted，正文写到一半被掐断、不是空串，只看正文空不空的实现会漏掉它。
http_401：HTTP 401 认证失败。
http_402：HTTP 402 余额不足。
http_422：HTTP 422 参数错误。
http_429：HTTP 429 限速。
http_500：HTTP 500 服务器错误。
http_503：HTTP 503 服务器繁忙。
slow：服务繁忙时的保持连接：非流式在正文前发空行，流式发 `: keep-alive` 注释。
hang：收下连接却一直不回，你的单次模型调用必须有超时。

## 注意

本机没有 DeepSeek Key，假模型的全部行为都来自官方文档，没有对照过真实接口。
不回传 reasoning_content 时的 400、response_format 取值不合法时的 422，都是按文档推定的。
P5 的 max_tokens ≥ 2048 和 P11 的 180 秒总预算是这份作业的规定，不是 DeepSeek 服务端的限制。
走 OpenAI 兼容路线的，请把这份报告贴进 `LLM_SETUP.md` 第 7 节（自测结果）。
