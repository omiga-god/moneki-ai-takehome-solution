# `llm_gateway.py` 使用说明

这是一个只依赖 Python 标准库的单文件工具，服务于 `docs/API_CONTRACT.md` 第 7 节推荐的那条路线：OpenAI 兼容的 Chat Completions 加三个环境变量（7.1）。
评测时我们会照着你的 `LLM_SETUP.md`，在不改你代码的前提下把服务换到 DeepSeek 的 `deepseek-flash`；走这条路线的服务，可以先用这个工具自测能不能被原样接上。

`preflight` 和 `fake` 只在本机起一个假模型服务：不需要 Key，不花钱，也不联网。
`proxy` 不一样：它把请求转发到你指定的真实上游，会联网，用的是上游的 Key，产生的也是上游的费用。

**走 OpenAI 兼容协议的，请跑一遍预检，并把输出贴进 `LLM_SETUP.md` 第 7 节。**
走其他协议的（包括 DeepSeek 自己的 Anthropic 格式接口）跑不了预检，请按契约 7.4 在 `LLM_SETUP.md` 第 7 节给出等价证据。

运行环境：Python 3.10 或更高版本，不需要安装任何第三方包。

```bash
python3 eval/llm_gateway.py --help
```

三个子命令：`preflight`（接入预检）、`fake`（假模型服务）、`proxy`（流量代理）。

---

## 一、`preflight`：交之前的接入预检

预检只检查 OpenAI 兼容的 Chat Completions 路线，下面 14 项的规则也都是这条路线上的规则（契约 7.1 与 7.3）。

它会在本机起一个假的 DeepSeek 服务，打印三个环境变量，等你用这三个变量重启服务，然后驱动你的 `POST /api/chat`，逐项检查你的接入方式。

```bash
python3 eval/llm_gateway.py preflight --service-url http://localhost:8000
```

它会先打印类似这样的一段：

```
预检假模型已启动：http://127.0.0.1:53120/ds-gw

  export LLM_BASE_URL=http://127.0.0.1:53120/ds-gw
  export LLM_API_KEY=preflight-key-3b9c1f
  export LLM_MODEL=preflight-model-7f3a
```

请原样用这三个变量重启你的服务，然后回车继续。
端口每次都不一样，所以不要把上一次的值记在配置文件里。
模型名是故意取的怪名字，写死模型名的实现会被查出来。
地址带一个 `/ds-gw` 前缀，自己补 `/v1` 或者只取域名的实现会接不上。

跑完之后它会打印一张 PASS/FAIL 表，并在当前目录写下 `preflight_report.md` 和 `preflight_report.json`。
全部通过时退出码是 0，有任何一项失败时退出码是 1。

常用参数：

| 参数 | 含义 |
|---|---|
| `--service-url` | 你的后端地址，必填 |
| `--no-wait` | 不等回车，适合写进脚本 |
| `--out DIR` | 报告写到哪个目录，默认当前目录 |
| `--scenarios a,b,c` | 只跑其中几个场景，默认全部 16 个 |
| `--port N` | 指定假模型端口，默认自动挑一个空闲端口 |
| `--max-chat-seconds` | `/api/chat` 的时限，默认 180 秒 |

### 14 项检查分别是什么意思

| 编号 | 检查什么 | 红了通常是因为 | 怎么修 |
|---|---|---|---|
| P1 | 服务确实把请求发到了注入的 `LLM_BASE_URL`（含 `/ds-gw` 前缀） | 没有用新环境变量重启；地址从配置文件读；仍然停在 mock 模式 | 地址只从 `LLM_BASE_URL` 读，原样拼上 `/chat/completions` |
| P2 | 请求里的 `model` 等于注入的 `LLM_MODEL` | 模型名写死在代码里 | 模型名只从 `LLM_MODEL` 读 |
| P3 | 注入的 Key 以 `Authorization: Bearer` 发送 | 没带这个头；用了别的认证方式；Key 从别处读 | 从 `LLM_API_KEY` 读，按 `Authorization: Bearer <key>` 发 |
| P4 | 只用了 DeepSeek 文档列出的顶层参数 | 用了 `parallel_tool_calls`、`seed`、`n` 之类文档里没有的参数 | 去掉它们；报告里会列出每一个多余的参数名。文档《Rate Limit》一页的 `user_id` 是允许的 |
| P5 | `max_tokens` 不设，或不小于 2048 | 沿用了别处调好的小额度 | 思考也占输出额度，设小了额度会被思考吃光，回来的是 `finish_reason: "length"` 加一个空正文；不设或者给到 2048 以上。2048 是这份作业的规定，不是 DeepSeek 服务端的下限，官方接口允许更小的值 |
| P6 | 没有访问 `{prefix}/chat/completions` 之外的任何路径 | 启动时列模型或查余额；自己补了 `/v1`；截掉了路径；在这条路线上又去调了 `/beta` 之类别的地址 | 在 OpenAI 兼容这条路线上，只允许 `{prefix}/chat/completions` 这一个接口；假服务会把所有越界的路径记下来。Anthropic 格式接口、Responses API 是另外的路线，本身不算错，只是预检检查的不是它们；选了它们就不必跑预检，按 `LLM_SETUP.md` 第 7 节给等价证据 |
| P7 | 工具定义规范，且每一个工具调用都以 `role: "tool"` + `tool_call_id` 回传 | 工具结果用 `role: "user"` 发回去；一条 assistant 消息带了两个工具调用却只回传了一个 | 一次可能返回多个 `tool_calls`，每一个都要有对应的 `role: "tool"` 消息。无参数的函数可以直接省略 `parameters`，预检不会因此判红 |
| P8 | 每个场景下 `/api/chat` 都返回 HTTP 200 与字段完整的合法 JSON | 模型出错时直接 500；漏字段；字段类型不对 | 无论内部发生什么，这个接口都要 200 + 合法 JSON，把错误体现在 `answer_type: "refusal"` 里 |
| P9 | 模型不可用时给出结构化 `refusal`，`answer` 从不是空串，且没有把失败的模型输出当成回答 | 拒答时把 `answer` 写成空串；把 `aborted` / `content_filter` 的半截正文当成了正常回答；配一条从没执行过的证据（`SELECT 0` 这种常量查询，或者一个你从未向模型声明过的工具名） | 拒答要写清原因，不得出现编造的数字；失败场景的模型输出里埋了一个一次性标记，它出现在 `answer`、`citations` 或 `data_evidence` 里就会判红 |
| P10 | 思考内容没有漏进 `answer` / `citations` / `data_evidence` | 把所有返回字段拼起来当回答；流式时把所有 delta 都拼进了正文 | 只读 `message.content` 和 `delta.content`，`reasoning_content` 不要给用户看 |
| P11 | `/api/chat` 在时限内返回，包括模型长时间不响应的场景 | 单次模型调用没有超时；整体没有兜底 | `/api/chat` 有 180 秒的总预算，到点必须返回；单次模型调用的超时取“120 秒”和“剩余预算”中较小的那个，预算用尽就返回结构化的 `refusal`。180 秒和 120 秒都是这份作业的评测政策，不是 DeepSeek 服务端的超时 |
| P12 | 注入环境变量后 `/api/health` 报告 `llm_mode: "live"` | 配置不是只从环境变量读；判断逻辑写死 | 有 `LLM_API_KEY` 就是 `live`，没有才是 `mock` |
| P13 | 多轮工具调用之间 `reasoning_content` 原样回传 | 自己挑字段重组 assistant 消息，把 `reasoning_content` 丢了 | 把收到的 assistant 消息整条追加进 `messages`，不要重组 |
| P14 | 保持连接的空行与 SSE 注释没有把服务弄坏 | 自己解析 HTTP 或 SSE，却不肯跳过正文前的空行和 `: keep-alive` 注释 | 解析前先跳过它们；用成熟的 HTTP 客户端一般不会踩到 |

表里还可能出现“未检查”。
那不算失败，但说明这一项没有素材可查，请照着报告里的逐项说明确认那是不是你想要的结果。
例如你完全不用工具调用，P7 和 P13 就会是未检查；你关掉了思考模式，P13 也会是未检查，这是规范允许的，不扣分。
服务压根没起来的时候，几乎所有检查都会是未检查，因为预检不会凭空说你超时或者说你做对了。
先把 P1、P8、P12 修好再看别的。

### 假服务会模拟的 16 个场景

预检会把每个场景都跑一遍，每个场景都用全新的 `session_id`。

| 场景 | 假服务会怎么做 | 它在考你什么 |
|---|---|---|
| `normal` | 先返回两个工具调用，再返回一个，最后才给正文 | 多轮工具调用，以及一条消息里多个工具调用 |
| `thinking_starved` | 只有当你把 `max_tokens` 设得小于 1024 时才咬人：正文为空、`finish_reason: "length"` | 思考占输出额度 |
| `empty_content` | 正文是空串、没有 `tool_calls`，`finish_reason` 仍然是 `stop` | 没有 `tool_calls` 而正文为空要当错误处理；带 `tool_calls` 的空正文是正常的，别一起误判 |
| `json_empty` | 只有当你用了 `response_format: json_object` 时才咬人：正文为空 | 文档说 JSON 模式偶尔会返回空内容 |
| `bad_tool_args` | 工具调用的 `arguments` 是被截断的非法 JSON | `arguments` 是字符串，解析会失败 |
| `content_filter` | `finish_reason: "content_filter"`，正文是半截话、不是空串 | 正常结束只有 `stop` 和 `tool_calls`；只看正文空不空会漏掉它 |
| `insufficient_resource` | `finish_reason: "insufficient_system_resource"`，正文为空 | 同上 |
| `aborted` | `finish_reason: "aborted"`，正文写到一半被掐断，不是空串 | 只判断“正文空不空”的实现会把这半句话当成正常回答发给用户，必须看 `finish_reason` |
| `http_401` | HTTP 401 认证失败 | 错误码不能让 `/api/chat` 崩 |
| `http_402` | HTTP 402 余额不足 | 同上 |
| `http_422` | HTTP 422 参数错误 | 同上 |
| `http_429` | HTTP 429 限速 | 同上 |
| `http_500` | HTTP 500 服务器错误 | 同上 |
| `http_503` | HTTP 503 服务器繁忙 | 同上 |
| `slow` | 非流式在正文前发空行，流式发 `: keep-alive` 注释 | 服务繁忙时的保持连接机制 |
| `hang` | 收下连接却一直不回 | 单次模型调用必须有超时 |

除此之外，假服务在所有场景下都会：

- 默认开启思考模式，每条 assistant 消息都带非空的 `reasoning_content`，里面有一个唯一标记；这个标记出现在你的 `answer` 里就说明思考内容漏了（P10）。
  你可以用 `thinking: {"type": "disabled"}` 关掉它。
- 只提供 `POST {prefix}/chat/completions`，其余任何路径都返回 404 并记录下来（P6）。
- 要求 `Authorization: Bearer <注入的 Key>`，否则 401（P3）。
- 带 `tools` 的请求，如果此前那条 assistant 消息的 `reasoning_content` 没有原样回传，就返回 400（P13）。
- 接受 `response_format` 的 `text` 与 `json_object`，其它取值返回 422。

---

## 二、`fake`：自己手动调试假模型服务

预检是自动跑一遍。
如果你想自己慢慢调，可以把假服务单独起起来：

```bash
python3 eval/llm_gateway.py fake --port 0
```

它会打印 `LLM_BASE_URL` / `LLM_API_KEY` / `LLM_MODEL` 和一个控制面地址。
`--port 0` 表示自动挑一个空闲端口；也可以写一个固定端口。

控制面（不算作你的流量，不影响任何检查）：

```bash
# 切换场景
curl -sX POST http://127.0.0.1:<端口>/__control/scenario -d '{"scenario":"http_429"}'
# 看假服务收到了什么（Authorization 只记长度，不记值）
curl -s http://127.0.0.1:<端口>/__control/requests
# 清空记录
curl -sX POST http://127.0.0.1:<端口>/__control/reset
```

按 `Ctrl-C` 停止。

---

## 三、`proxy`：把你发给模型的每一条请求记下来

```bash
python3 eval/llm_gateway.py proxy --upstream https://api.deepseek.com --log llm_traffic.jsonl
# 然后用它打印的 LLM_BASE_URL 启动你的服务
```

它会把 `{前缀}/<其余路径>` 转发到 `{upstream}/<其余路径>`，流式响应逐字节原样透传，同时每一次往返写一行 JSON：时间戳、路径、请求体、状态码、响应体（流式的话是重新拼装出来的 `content`、`reasoning_content`、`tool_calls`、分片数、首个正文 token 的时间）、耗时、token 用量。

它会真的联网访问上游，请求用的是上游的 Key，费用也记在上游的账上。

转发和记日志不挑协议：任何 HTTP 请求都会原样转发，非流式响应的正文会整段记进日志。
但把流式响应重新拼装成 `content` / `reasoning_content` / `tool_calls` 这一步只认 OpenAI Chat Completions 的分片格式；别的协议的流式响应，日志里只有分片数、耗时和请求体，拼不出正文。

`Authorization` 会照常转发给上游，但日志里只记它的长度，不记值，所以这份日志可以放心贴给我们。

`--inject-thinking enabled|disabled` 可以给本来没有设置 `thinking` 的请求补上这个开关，请求自己设了就不动它。
默认不注入。

评测时我们会让你的服务经过这个代理，或者你在 `LLM_SETUP.md` 里给出的等价办法，所以你发给模型的每一条提示词我们都看得到。

---

## 四、哪些行为来自文档、但没有对照过真实接口

我们手上没有 DeepSeek Key，所以假服务的全部行为都是照着官方文档实现的，没有跟真实接口对照过。
下面几条尤其要注意，如果你在真实接口上看到不一样的行为，请写进 `LLM_SETUP.md` 第 8 节（已知限制）告诉我们，这不会扣分。

- 不回传 `reasoning_content` 时返回 400：文档的 Thinking Mode 一节明确要求带 `tools` 的请求必须原样回传，假服务按这个要求返回 400，但 400 的正文结构是推定的。
- `response_format.type` 取 `text` / `json_object` 之外的值时返回 422：文档只说“只能是 `text` 或 `json_object`”，没有给不合法取值时的状态码，422 是我们按“参数错误”推定的。
- 所有错误正文的 JSON 结构：文档的错误码页只给了状态码和中文名称，没有给正文结构；假服务按 OpenAI 兼容接口常见的 `{"error": {"message": ..., "type": ..., "code": ...}}` 构造。
- `thinking_starved` 的 1024 这个阈值：文档只说思考占输出额度、思考模式下 `max_tokens` 默认 64K，没有给“多小算小”的界限；1024 是我们为了给 2048 这条底线留余量而定的。
- 保持连接时空行和注释的条数与间隔：文档说等待期间会持续返回空行或 `: keep-alive`，没有给频率；假服务发的条数和间隔都是我们定的。
- `usage` 里的 token 数：假服务按字符数粗算，不是真实的分词结果。
  流式时 `usage` 的位置按现行文档来：挂在最后一个带 `finish_reason` 的分片上，没有单独的空 `choices` 块。
- 失败场景正文里的那个一次性标记：那是预检自己加的钩子，真实接口不会有。

另外要分清两类规则。
`max_tokens ≥ 2048`、`/api/chat` 180 秒总预算、单次模型调用取 min(120 秒, 剩余预算)，这三条是这份作业的评测政策，为的是让思考模式加多轮工具调用能跑完；DeepSeek 官方接口本身允许更小的输出额度，也没有这样的超时规定。
其余各条（思考默认开启、`reasoning_content` 回传、`finish_reason` 取值、错误码、保持连接、流式顺序）才是在复现官方文档描述的服务端行为。

已经跟文档对齐、可以当真的部分：接口是 `POST {LLM_BASE_URL}/chat/completions` 且地址不带 `/v1`；思考模式默认开启，思考过程在 `reasoning_content`；`tool_calls[].function.arguments` 是 JSON 字符串；一条 assistant 消息可以带多个工具调用且此时 `content` 可为空串；`finish_reason` 的六种取值；错误码 400/401/402/422/429/500/503；流式先 `delta.reasoning_content` 后 `delta.content`。

---

## 五、常见问题

问：预检说一次请求都没收到（P1 红）。

先确认你真的用它打印的三个环境变量重启了服务，而不是只改了配置文件。
再确认没有 `LLM_API_KEY` 时你的服务会进 mock 模式。
如果它仍然停在 mock，就永远不会发请求。

问：预检卡在“重启完成后按回车继续”。

写进脚本时加 `--no-wait`。

问：我走的不是 OpenAI 兼容协议。

可以，契约允许任何厂商、协议和 SDK，只要满足 7.2 的四条。
这时预检跑不了，也不需要跑；请在 `LLM_SETUP.md` 第 7 节贴一次完整的请求与响应样例（Key 打码），并说明你怎么处理空回答、超时和报错。
如果你走的是 HTTP，`proxy` 照样能帮你把请求记下来，只是流式响应在日志里拼不出正文。

问：我不用工具调用，走的是 SQL。

可以。
P7 和 P13 会显示“未检查”，不算失败。

问：我想关掉思考模式。

可以，规范允许，请在 README 里写明理由。
这时 P10 和 P13 会因为没有思考内容可查而显示“未检查”。

问：端口被占用了。

`--port 0` 会自动挑一个空闲端口，预检默认就是这样。

问：这个工具自己坏了怎么办？

它有一套自测：

```bash
cd 候选人作业包/eval/tests
python3 -m unittest test_llm_gateway -v
```

全部通过大约需要半分钟，不联网、不留下任何后台进程。
