# LLM 接入说明

## 1. 用了什么

面向 DeepSeek 官方 Chat Completions，OpenAI 兼容 JSON。客户端是 `starter/kbqa/llm.py`，使用 `httpx==0.28.1`，没有厂商 SDK。只 POST `{LLM_BASE_URL}/chat/completions`，不补 `/v1`。本次没有真实 Key，实测使用官方假模型。

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

每次 `/api/chat` 都有 `trace_id`。`GET /api/trace/{trace_id}` 的 `llm_calls[].request` 保留完整请求体（所有轮次的 messages、tools、model、max_tokens），`response` 和 `raw_response` 保留完整响应；不记录 Authorization 头。无需开启额外开关。前端调试面板显示请求/输出，隐藏思考字段；服务端 trace 保留思考用于协议回传核查。trace 只在内存中保留最近 200 次，重启清空。

也可按官方 `eval/README_llm_gateway.md` 启动 proxy，将三个变量中的 BASE_URL 改成它输出的含路径地址后重启。不要自行增加 `/v1`。本地网关直连，远程地址保留系统/环境代理支持。

## 5. 没有 Key 时会怎样

`/api/health`、`/api/metrics/*`、`/api/retrieve` 照常返回。`/api/chat` 走本地规划器和检索，返回 200，不调用外网。2026-09-26 的无 Key 公开评测是 100.00/100.00。

## 6. 依赖与安装

模型调用只用 `requirements.txt` 里的 `httpx`。没有额外模型包。无 Key 启动不下载模型。

## 7. 自测结果

没有真实 DeepSeek Key。预检用官方假模型，命令是仓库根目录：

```powershell
.\starter\.venv\Scripts\python.exe scripts/verify.py preflight
```

这个已入库脚本调用 `eval/llm_gateway.py` 的 `run_preflight`：假模型先起来，再用注入变量启动隔离缓存、空闲端口的后端，然后驱动 `/api/chat`。不占用 8000、不停止用户的服务。报告写入 `starter/var/audit-preflight/`。Windows 和 Linux 使用同一脚本。

接手后复测报告时间 2026-09-26 16:21:48 +08:00。假模型地址 `http://127.0.0.1:54906/ds-gw`，注入模型名 `preflight-model-7f3a`。工具版本 `llm_gateway.py 2.0.0`。结论：**14 项检查全部通过**。后续最终提交报告另见 `verification/preflight.md`。

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
| P11 | 180 秒内返回 | 通过，最慢 120.28 秒 |
| P12 | health 的 llm_mode 为 live | 通过 |
| P13 | 多轮回传 reasoning_content | 通过，18 次 |
| P14 | 空行和 SSE keep-alive 不影响解析 | 通过 |

终端显示“预检通过：在 OpenAI 兼容这条路线上，我们能原样接上你的服务。”，脚本退出码为 0。没有失败项。

处理策略：max_tokens=4096；默认保留思考；assistant 消息连 reasoning_content 原样回传；工具参数 JSON 无法解析时反馈给模型，连续失败则拒答；异常 finish_reason、HTTP 错误、空最终正文均记录并结构化拒答。单次请求取 120 秒和剩余预算的较小值，以 asyncio 总时限覆盖持续 keep-alive；默认总预算 150 秒，给 180 秒接口期限留余量。仅短暂故障且剩余时间充足时重试一次。默认不流式输出，前端显示“思考中”。

## 8. 已知限制

真实 DeepSeek 的全量公开评测没有跑。预检 Key 是假模型自己的测试 Key，不是可用的生产 Key。

live 的模型工具探索后，最终回答仍由可信查询与原文抽取器核验生成，未经语义验证的模型散文不会直接展示。因此它不是无限制的通用 SQL/文档代理；未支持的领域或没有对应目标/制度时会说明证据不足。预检验证协议兼容性，不能证明真实模型质量或隐藏题成绩。
