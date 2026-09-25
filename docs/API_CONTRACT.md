# API 契约（必须遵守）

我们用同一个脚本评测所有人的作品，所以后端必须暴露下面这组接口。
技术栈、内部实现、前端长什么样都由你决定，但**路径、字段名、字段类型不能改**。
你可以在此之外增加任何接口和字段。

- 所有接口返回 `application/json`，UTF-8。
- 服务地址由你在 README 里写明（默认按 `http://localhost:8000` 评测）。
- 系统的“今天”固定为 2026-09-01，所有“现在”“最近”“目前”都以这一天为准。
- 数据口径以知识库 `KB-001` 为准。
- `doc_id` 指知识库文件名开头的编号，例如 `KB-013`，与文件格式无关。

## 1. `GET /api/health`

```json
{
  "status": "ok",
  "llm_mode": "live",
  "kb_docs": 35,
  "kb_chunks": 412,
  "valid_sales_rows": 17890
}
```

- `llm_mode`：`live`（配置了真实模型）或 `mock`（未配置 Key 时的降级模式）。
- `kb_docs`：实际进入索引的文档数，不是目录里的文件数。
  `knowledge_base/` 里可能有不是文档的文件（没有 `KB-xxx` 编号的说明文件之类），它们不算文档。
- `valid_sales_rows`：按 KB-001 清洗后保留的明细行数（销售行 + 退款行）。

## 2. `GET /api/metrics/summary`

查询参数：`start`、`end`（`YYYY-MM-DD`，闭区间，必填），`store_id`、`product_id`（可选）。

```json
{
  "start": "2026-06-01",
  "end": "2026-06-30",
  "store_id": null,
  "product_id": null,
  "net_revenue": 123456.00,
  "refund_amount": 789.00,
  "orders": 3456,
  "aov": 35.72,
  "qty": 5012
}
```

字段含义与 KB-001 一致：净营业额、退款金额、有效订单数、客单价、销量。
区间内没有数据时，数值字段返回 `0`，`aov` 返回 `null`，不要报错。

## 3. `GET /api/metrics/daily`

查询参数同上。

```json
{
  "days": [
    {"date": "2026-06-01", "net_revenue": 4012.00, "orders": 118, "aov": 34.00}
  ]
}
```

区间内每一天都要有一条记录，没有营业额的日期也要出现，数值为 `0`，`aov` 为 `null`。

## 4. `POST /api/retrieve`

只做检索，不调用大模型。
我们用它单独评测检索质量。

请求：

```json
{"query": "外卖订单多久内可以退款", "top_k": 5}
```

响应：

```json
{
  "results": [
    {"doc_id": "KB-013", "chunk_id": "KB-013#2", "score": 12.84, "text": "……"}
  ]
}
```

- 按相关性从高到低排序，恰好 `top_k` 条；只有当索引里的片段总数不足 `top_k` 时才允许更少。
  先取前 `top_k` 再做过滤、结果只剩两三条的实现，不符合这一条。
- 这里返回的必须和问答链路实际使用的检索是同一套实现。

## 5. `POST /api/chat`

请求：

```json
{"session_id": "abc-123", "question": "618 当天 S02 的牛肉poke 卖了多少份，达到目标了吗？"}
```

同一个 `session_id` 的多次请求视为同一段对话，要支持追问。

响应：

```json
{
  "answer": "618 当天 S02 牛肉poke 实际售出 118 份，活动目标为 120 份，差 2 份未达标。",
  "answer_type": "hybrid",
  "citations": [
    {"doc_id": "KB-023", "quote": "目标销量 120 份"}
  ],
  "data_evidence": [
    {
      "tool": "query_metrics",
      "params": {"start": "2026-06-18", "end": "2026-06-18", "store_id": "S02", "product_id": "P06"},
      "result": {"qty": 118}
    }
  ],
  "trace_id": "t-20260901-0001"
}
```

上面示例里的数字只用来说明字段的形状，是虚构的，不是这份数据的真实答案。

字段规则：

- `answer`：给运营看的中文回答。
  涉及金额、数量、比例时必须写出精确的阿拉伯数字，不要只写“约 13 万”。
- `answer_type`：五选一。
  - `data`：答案来自数据库。
  - `doc`：答案来自知识库。
  - `hybrid`：两者都用了。
  - `refusal`：无法回答或不应执行，此时不得出现编造的数字或原因。
  - `clarify`：问题有歧义，向用户反问。
- `citations`：回答中凡是来自知识库的事实，都要列出对应的 `doc_id` 和原文片段 `quote`。
  `quote` 必须是该文档里真实存在的连续文字，并且要短：只引用支撑这条事实的那一两句，一条 `quote` 不超过 400 个字符。
  逐字比较和长度都按同一套规范化计算：先做 NFKC，再去掉所有空白，以及 `*`、`` ` ``、`|`、`#`、`>` 这几个 Markdown 符号。
  所以不必照抄文档里的换行、表格竖线和加粗记号。
  把整段、整篇文档贴进来不算引用，评测脚本不会从这样的 `quote` 里认任何事实。
  没有用到知识库时返回空数组。
- `data_evidence`：回答中凡是来自数据库的数字，都要列出对应的查询。
  可以是工具调用（`tool` + `params`），也可以是 SQL（`sql`），二选一，`result` 必填。
  没有用到数据库时返回空数组。
- `trace_id`：本次回答的追踪编号，用于下面的接口。
  评测时每答完一题都会去取一次 `/api/trace/{trace_id}`，取不到按这一题不合格处理。

硬上限（评测脚本逐题检查，超出即判这一题不合格；这些上限用来拦截刷分，正常回答离它们很远）：

| 项 | 上限 |
|---|---|
| `answer` 长度 | 1200 个字符 |
| `answer` 里不同数字的个数 | 20 个 |
| 一次回答引用的不同文档 | 4 份 |
| 单条 `data_evidence.result` 序列化后的大小 | 4096 字节 |
| 全部 `result` 里的数字总数 | 60 个 |
| `data_evidence.sql` | 只能是一条只读查询（`SELECT` 或 `WITH` 开头），并且确实查了表（有 `FROM`） |

涨跌类问题只给一个结论：同一个回答里，“上涨 / 上升 / 涨了 / 升了”和“下跌 / 下降 / 跌了 / 降了”这两组词同时出现，按没有结论处理。
只看这两组词：用“高于 / 低于 / 多了 / 少了”比较两个时段、或者顺带提到别的指标“减少”“降低”，都不受影响。

无论内部发生什么错误，这个接口都必须返回 HTTP 200 和合法的 JSON，把错误体现在 `answer_type: "refusal"` 与 `answer` 的说明里，同时在你的日志和 trace 里留下真实的错误原因。

## 6. `GET /api/trace/{trace_id}`

返回这次回答的完整处理过程，字段自定，但至少要能看到：

- 改写后的检索查询（如果有改写）。
- 检索到的每个片段的 `doc_id`、`chunk_id`、分数，以及哪些被过滤掉、为什么。
- 执行的工具调用或 SQL，以及结果。
- 发给大模型的最终提示词和模型原始输出。
- 每一步的耗时，以及出现的错误。

第四关的调试面板就是把这个接口可视化。

## 7. 大模型接入

上一轮作业里，有相当一部分作品的 AI 功能离开作者自己的环境就跑不起来，我们无法验收。
这一轮开发时用哪家模型、哪种协议、哪个 SDK，你自己定。
但评审时我们统一把你的服务切到 DeepSeek 的 `deepseek-flash` 上、用我们自己的 Key 来跑。
你要让这一步不改代码就能完成，并为此交一份接口说明。
我们照着说明切不过去，第三关就只能按“没有 Key 的降级模式”给分。

### 7.1 推荐路线：OpenAI 兼容 + 三个环境变量

这条路线最省事，也最不容易出问题。
评审时我们用的就是它：`LLM_BASE_URL=https://api.deepseek.com`、`LLM_MODEL=deepseek-flash`、我们自己的 Key。
我们自己验证这套题时用的也是 DeepSeek 官方 API（`https://api.deepseek.com`，模型 `deepseek-flash`）：不需要本地算力，什么电脑都能跑，按官网定价估算，把整套题库完整跑上几遍花费在十几元人民币以内。

| 变量 | 含义 |
|---|---|
| `LLM_BASE_URL` | 模型服务地址，例如 `https://api.deepseek.com` |
| `LLM_API_KEY` | 模型 Key |
| `LLM_MODEL` | 模型名，例如 `deepseek-flash` |

走这条路线的注意：

- 只调 `POST {LLM_BASE_URL}/chat/completions`，地址原样拼接：不要自己补 `/v1`，不要截掉路径，不要只取域名。
  用 OpenAI SDK 的话，把 `LLM_BASE_URL` 原样作为 `base_url` 传进去即可。
  评测时我们可能让你的服务经过一个带路径前缀的代理（见 7.5），自己改写地址的实现会接不上。
- 7.3 的可移植性规则适用，并且可以用 `eval/llm_gateway.py preflight` 自测，不花钱、不需要 Key。
- DeepSeek 的行为以官网文档为准：<https://api-docs.deepseek.com/>，7.3 表格里标注的“文档”均指它。

### 7.2 开发时换别的也行，但要满足四条

开发时用别家厂商、厂商自有协议、官方 SDK、本地模型、自建网关，都可以。
条件只有下面四条，缺一条我们就切不过去，第三关就没法按真实模型评分：

1. **可替换**：切到 DeepSeek `deepseek-flash`、换上我们的 Key 时，不需要改你的代码，改配置或环境变量就够。
   DeepSeek 同时提供 OpenAI 兼容接口和 Anthropic 格式接口（见其官网文档），两条都可以。
   具体改哪几个值、改完要不要重启或重建，写进 `LLM_SETUP.md`。
2. **可观察**：我们必须能看到你发给模型的完整请求，包括提示词、工具定义和每一轮的消息。
   走 HTTP 的，把地址指向 `eval/llm_gateway.py proxy` 就行；用 SDK 或本地模型的，请提供一个开关，把请求原文写进日志或 trace，并在说明里写清怎么打开、日志在哪。
   看不到请求，我们就无法判断数字是查出来的还是让模型现编的。
3. **可离线启动**：没有配置任何 Key 时，服务仍要能启动，`/api/health`、`/api/metrics/*`、`/api/retrieve` 正常工作，`/api/chat` 进入降级模式或返回结构化的 `refusal`，不允许 HTTP 500。
4. **Key 不入库**：仓库里出现真实 Key 按红线处理。
   你自己开发用的 Key 自备；我们不提供。

不要在启动时校验 Key 的格式，也不要在启动时调用查余额、列模型之类的接口。
我们换上自己的配置时，这类检查最容易出错。

### 7.3 必须处理好的行为（走 OpenAI 兼容协议时适用）

评审时对面是 DeepSeek，所以请求参数以 DeepSeek 文档为准；你开发时用的厂商支持、而 DeepSeek 不支持的参数（例如 `seed`、`parallel_tool_calls`），切过去之前要去掉。
用别的协议时，下面每一条都有对应的等价问题（思考字段、超时、错误码、工具参数解析），请在 `LLM_SETUP.md` 第 7 节说明你怎么处理的。

请求参数以文档的 Chat Completions 接口为准，另外允许文档《Rate Limit》一页里的 `user_id`。
文档里没有的参数（例如 `parallel_tool_calls`、`seed`、`n`）不要用。

| 规则 | 依据 |
|---|---|
| `deepseek-flash` 默认开启思考模式：回答在 `message.content`，思考过程在 `message.reasoning_content` | 文档《Thinking Mode》；思考过程不要展示给用户，也不要当成回答 |
| 带 `tools` 的请求，必须把此前每一条 assistant 消息连同 `reasoning_content` 原样回传，否则接口返回 400 | 文档《Thinking Mode》“Tool Calls”一节；最稳妥的做法是把收到的 assistant 消息整条追加进 `messages`，不要自己挑字段重组 |
| 思考也占输出额度：`max_tokens` 不设，或不小于 2048（这是本作业的要求，不是 DeepSeek 的服务端下限）；`finish_reason` 为 `length`，或者没有 `tool_calls` 而 `content` 又为空时，按错误处理 | 文档：思考模式下 `max_tokens` 默认 64K；设小了额度会被思考吃光，回答为空。注意带 `tool_calls` 的消息 `content` 本来就可能是空串，那是正常的 |
| `finish_reason` 不只有 `stop`：正常结束是 `stop` 和 `tool_calls`；`length`、`content_filter`、`insufficient_system_resource`、`aborted` 这四种都按错误处理 | 文档《Create Chat Completion》；重试或返回结构化的 `refusal`，真实原因写进 trace |
| 工具调用：`tool_calls[].function.arguments` 是 JSON 字符串，要自己解析并处理解析失败；结果用 `role: "tool"` 加 `tool_call_id` 回传；一次可能返回多个工具调用；此时 `content` 可能是空串 | 文档《Tool Calls》 |
| `response_format` 只支持 `text` 和 `json_object`，且 JSON 模式“偶尔会返回空内容” | 文档《JSON Output》；要结构化输出，优先用工具调用 |
| 思考模式下 `temperature`、`presence_penalty`、`frequency_penalty` 不生效 | 文档《Thinking Mode》；不要指望靠 `temperature=0` 得到确定性，回答里的数字必须由你的代码从工具结果渲染 |
| 服务繁忙时连接会保持：非流式响应体前面会有空行，流式响应里会有 `: keep-alive` 注释行 | 文档《Rate Limit》；自己解析 HTTP 或 SSE 的实现要能跳过它们 |
| 错误码：400、401、402（余额不足）、422、429（并发超限）、500、503 | 文档《Error Codes》；任何一种都不能让 `/api/chat` 返回 500 或挂起，要返回结构化的 `refusal` 并把真实原因写进 trace |
| `/api/chat` 有 180 秒的总预算，到点必须返回（本作业的要求，不是 DeepSeek 的限制）；单次模型调用的超时取“120 秒”和“剩余预算”中较小的那个，不要设成十几秒 | 思考模式加两三轮工具调用，一次问答可能要几十秒，超时设短了会把正常的慢回答当成失败；但模型彻底不响应时，也不能让用户无限等下去。预算用尽就返回结构化的 `refusal` |
| 流式输出时先到的是 `delta.reasoning_content`，之后才是 `delta.content` | 文档《Thinking Mode》的流式示例；前端要有“思考中”的状态，不要把思考内容拼进回答 |

是否关闭思考模式（`thinking: {"type": "disabled"}`）、用多大的 `reasoning_effort`，由你决定并在 README 里说明理由：关掉更快更省，开着规划更稳。
关闭思考之后就没有 `reasoning_content`，预检里与它有关的两项会显示“未检查”而不是“通过”，这不扣分。

### 7.4 必交：`LLM_SETUP.md` 接口说明

这份说明是我们把你的服务接上自己模型的唯一依据，**写不清楚我们就接不上**。
它本身也是评分项。
请按下面的骨架写，每一节都要有实际内容，不要写“见代码”。

```markdown
# LLM 接入说明

## 1. 用了什么
厂商、模型名、协议（OpenAI 兼容 Chat Completions / Anthropic Messages / 厂商原生 / 本地模型…）、SDK 及版本。

## 2. 配置从哪里读
每一项配置的名字、默认值、读取位置（环境变量 / 配置文件 / 命令行参数）。

## 3. 怎么换成你们的
一步一步写：我们要改哪几个值，才能换成自己的厂商、Key 和模型。
改完之后需要重启、还是要重新执行重建命令？

## 4. 怎么看到发给模型的请求
打开日志或代理的具体命令，日志文件在哪，长什么样（贴一小段脱敏样例）。

## 5. 没有 Key 时会怎样
服务能不能启动，四个接口分别返回什么，降级策略是什么。

## 6. 依赖与安装
额外依赖、模型文件下载体积、首次启动耗时。

## 7. 自测结果
走 OpenAI 兼容协议的：贴 `eval/llm_gateway.py preflight` 的输出。
不走的：贴一次完整的请求与响应样例（Key 打码），并说明你怎么验证空回答、超时、报错这三种异常。

## 8. 已知限制
你清楚但没解决的问题。写出来不扣分，藏起来被我们撞见才扣。
```

### 7.5 交之前自测：接入预检

作业包里提供了一个只依赖标准库的工具 `eval/llm_gateway.py`，常用的是下面两种用法；它还有第三个子命令 `fake`，可以单独起假模型服务供你写测试用，详见 `eval/README_llm_gateway.md`。

预检模式：它会起一个按 DeepSeek 文档行为模拟的假模型服务，然后驱动你的 `/api/chat`，检查你的服务是不是按规范接入的。
不花钱，不需要 Key。
走 OpenAI 兼容协议的，请务必跑一遍，并把输出贴进 `LLM_SETUP.md`；用别的协议的跑不了它，请按 7.4 第 7 节给出等价证据。

```bash
python3 eval/llm_gateway.py preflight --service-url http://localhost:8000
# 按它打印的提示，用它给出的 LLM_BASE_URL / LLM_API_KEY / LLM_MODEL 重启你的服务，然后回车继续
```

它会检查：请求是否真的原样发到了 `LLM_BASE_URL`；`model` 是否等于 `LLM_MODEL`；Key 是否通过 `Authorization: Bearer` 发送；工具调用多轮之间 `reasoning_content` 是否回传；有没有用文档之外的参数；以及当模型返回空回答、超长截断、畸形的工具参数、各种错误码、保持连接的空行、长时间无响应时，你的 `/api/chat` 是否仍然返回 HTTP 200 和合法的 JSON，思考内容有没有漏进 `answer`。
走兼容协议的，预检全部通过，才说明我们切得过去。
请把预检输出贴进 `LLM_SETUP.md` 第 7 节。

代理模式：它转发到真实的 DeepSeek，并把你的服务发出的每一次请求和收到的每一次响应记到 JSONL 里，调试时很有用。

```bash
python3 eval/llm_gateway.py proxy --upstream https://api.deepseek.com --log llm_traffic.jsonl
# 然后用它打印的地址作为 LLM_BASE_URL 启动你的服务
```

评测时我们会让你的服务经过这个代理（或你在 `LLM_SETUP.md` 里给出的等价办法），所以你发给模型的每一条提示词我们都看得到。

### 7.6 向量模型（可选）

检索不强制使用向量模型。
如果你要用向量检索，两条路任选其一：

- 本地向量模型：能自动下载、只用 CPU 就能跑，并在 README 写明首次下载的大小。
- 需要 Key 的向量服务：我们评审时不一定有这家的 Key，所以它的地址、Key、模型名只从环境变量读，Key 不入库；没有配置时服务照常启动、自动退回不依赖向量的检索。
  配置与关闭的方法写进 `LLM_SETUP.md`。
  7.2 里“切到 DeepSeek `deepseek-flash`”那一条只针对问答用的大模型，不针对向量服务。

无论哪条：

- 知识库里有中文、英文两种文档，选模型时请确认它支持跨语言。
- 索引必须在重建命令里重新生成。
- 向量模型不可用时，检索必须能退回到不依赖向量的方式，`/api/retrieve` 不允许 500。

## 8. 重建命令

评审时我们会替换 `data/` 和 `knowledge_base/` 两个目录（同结构，数字不同，文档有增有改），然后重建。
所以你必须提供一条命令，从这两个目录重新生成清洗后的数据和检索索引，并写在 README 里，例如：

```bash
make rebuild
```

这意味着：

- 不要把任何答案、数字、文档内容写死在代码里。
- 不要依赖本机绝对路径。
- 索引必须能感知知识库的变化。
