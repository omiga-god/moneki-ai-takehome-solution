# LLM 接入说明

## 1. 用了什么

DeepSeek 官方 Chat Completions，OpenAI 兼容 JSON。客户端是 `starter/kbqa/llm.py` 里的 `httpx`，没有使用厂商 SDK。只 POST `{LLM_BASE_URL}/chat/completions`，不补 `/v1`。

## 2. 配置从哪里读

`starter/kbqa/config.py` 的 `load_settings()` 读取：

- `LLM_BASE_URL`：去掉末尾斜杠，原样使用
- `LLM_API_KEY`
- `LLM_MODEL`

三个都非空时 `/api/health` 的 `llm_mode` 为 `live`，否则为 `mock`。代码里没有默认 Key，也没有写死模型名。

## 3. 怎么换成评审环境

设置下面三个变量后重启服务。索引和清洗库不用重建。

```powershell
$env:LLM_BASE_URL = "https://api.deepseek.com"
$env:LLM_API_KEY = "<评审 Key>"
$env:LLM_MODEL = "deepseek-flash"
cd starter
.\.venv\Scripts\python.exe -m uvicorn kbqa.server:app --host 127.0.0.1 --port 8000
```

Key 只留在环境变量里，不要写入文件或提交。

## 4. 怎么看到发给模型的请求

每次 `/api/chat` 都有 `trace_id`。`GET /api/trace/{trace_id}` 的 `llm_calls` 记录端点、模型名、消息条数、工具个数和提示词预览。预检时假模型收到的请求由 `eval/llm_gateway.py` 统计，完整报告在本地 `starter/var/preflight/preflight_report.md`（不入库）。

## 5. 没有 Key 时会怎样

`/api/health`、`/api/metrics/*`、`/api/retrieve` 照常返回。`/api/chat` 走本地规划器和检索，返回 200，不调用外网。2026-09-26 的无 Key 公开评测是 100.00/100.00。

## 6. 依赖与安装

模型调用只用 `requirements.txt` 里的 `httpx`。没有额外模型包。无 Key 启动不下载模型。

## 7. 自测结果

没有真实 DeepSeek Key。预检用官方假模型，命令是仓库根目录：

```powershell
.\starter\.venv\Scripts\python.exe starter\var\run_preflight.py
```

这个脚本调用 `eval/llm_gateway.py` 的 `run_preflight`：假模型先起来，再按它打印的三个变量重启 `127.0.0.1:8000`，然后驱动 `/api/chat`。`--no-wait` 由脚本里的 `ready_hook` 代替人工回车。

报告时间 2026-09-26 02:04:58 +08:00。假模型地址 `http://127.0.0.1:49165/ds-gw`，注入模型名 `preflight-model-7f3a`。工具版本 `llm_gateway.py 2.0.0`。结论：**14 项检查全部通过**。

| 编号 | 检查项 | 结果 |
|---|---|---|
| P1 | 请求发到注入的 LLM_BASE_URL，含路径前缀 | 通过，60 次 POST `/ds-gw/chat/completions` |
| P2 | model 等于注入的 LLM_MODEL | 通过 |
| P3 | Key 以 Authorization: Bearer 发送 | 通过 |
| P4 | 只用文档列出的顶层参数 | 通过 |
| P5 | max_tokens 不小于 2048 | 通过 |
| P6 | 不访问其它路径 | 通过 |
| P7 | 工具结果以 role=tool 和 tool_call_id 回传 | 通过，44 个工具调用 |
| P8 | /api/chat 全部 HTTP 200 且 JSON 完整 | 通过，32 次 |
| P9 | 模型不可用时结构化拒答，answer 非空 | 通过 |
| P10 | 思考内容不出现在对外字段 | 通过 |
| P11 | 180 秒内返回 | 通过，最慢 120.22 秒 |
| P12 | health 的 llm_mode 为 live | 通过 |
| P13 | 多轮回传 reasoning_content | 通过，18 次 |
| P14 | 空行和 SSE keep-alive 不影响解析 | 通过 |

终端最后一行是 `PREFLIGHT PASS`。没有失败项。

## 8. 已知限制

真实 DeepSeek 的全量公开评测没有跑。预检 Key 是假模型自己的测试 Key，不是可用的生产 Key。
