# 交给后续 AI 的实施说明

## 0. 目标和边界

目标是完成 Moneki.ai 的四关作业：经营数据看板、修复官方 RAG starter、数据库与文档混合问答、可调试追踪与评测回归。官方[题目 README](https://github.com/MorrisPRC/moneki-ai-takehome/blob/main/README.md)、[API 契约](https://github.com/MorrisPRC/moneki-ai-takehome/blob/main/docs/API_CONTRACT.md)和[评测说明](https://github.com/MorrisPRC/moneki-ai-takehome/blob/main/eval/README.md)高于本指南；发现差异时按官方文件执行并在 README 记录取舍。

当前目录只是骨架。**先导入官方仓库，再阅读实际文件。**不要把本指南当成已验证的缺陷清单，也不要让模型凭描述补造数据库或 35 份文档。

## 1. 第一轮：建立可复现基线

1. 执行 `pwsh -File scripts/bootstrap-upstream.ps1`，或者用已下载仓库的本地目录执行 `pwsh -File scripts/bootstrap-upstream.ps1 -SourceDir '...'`。
2. 核对 `data/pos.db`、`knowledge_base/`、`starter/kbqa/`、`eval/public_questions.jsonl` 与 `docs/API_CONTRACT.md` 存在。读官方 `UPSTREAM_README.md`、`starter/HANDOVER.md`、`eval/README.md`；交接文档中的质量声称只能当待验证假设。
3. 在 `starter/` 建 Python 3.12 虚拟环境，安装 `requirements.txt`，按其 Makefile 或等价 Python 命令执行 `rebuild`、`run`、`test`。Windows 若无 `make`，先看 Makefile 的实际 target 再转换，不能猜命令。
4. 在项目根目录运行官方公开评测：`python eval/run_eval.py --base-url http://localhost:8000 --questions eval/public_questions.jsonl`。保存原始 `report.md`、`report.json` 和当时 Git commit。将真实初始分数、命令、Key 状态写入 `EVAL_REPORT.md`。
5. 检查 `KB-001` 的现行口径及相关版本文件，列出清洗规则、营收/退款/订单/销量/客单价公式和日期边界。只写读到的规则，并用小样本 SQL 验证。

**这一阶段的完成标准：** 后端可启动；无 Key 时健康、指标、检索可用；取得原始评测报告；掌握实际表结构和有效口径。若因网络或环境受阻，记录原始报错，不填假分数。

## 2. 第二轮：清洗与指标 API

保留 `starter/` 为后端。重点检查 `kbqa/cleaning.py`、`tools.py`、`server.py`，但具体根因以实验为准。对每个口径规则先写一个会失败的针对性测试，再修复。必须覆盖重复行、缺失/非法字段、外键、退款、日期格式、金额和数量、边界日期；最终准确范围以 `KB-001` 为准。

按契约实现：

- `GET /api/health`：`status`、`llm_mode`、真正入索引的 `kb_docs` 与 `kb_chunks`、清洗后保留的 `valid_sales_rows`。
- `GET /api/metrics/summary`：`start`、`end` 必填且闭区间；`store_id`、`product_id` 可选；返回 `net_revenue`、`refund_amount`、`orders`、`aov`、`qty`。区间内无数据时除 `aov=null` 外数值为 0。
- `GET /api/metrics/daily`：区间每一天都出现，零营业日值为 0 且 `aov=null`。
- 看板的数据质量信息可以继续使用 starter 的 `/api/data_quality`，但字段先检查实现，必要时补 API；前端不硬编码清洗数量。

日期不存在、`start > end`、未知门店与商品如何响应，应在 README 明确且保持稳定。金额计算使用精确十进制思路或 SQLite 整数分处理，避免浮点累计误差。所有 SQL 参数化。营业数字只从重建后的真实数据得出。

**验收：**公开题库 `metrics`、`health` 逐项检查；另写边界和空区间测试；替换一份小型输入后重建，输出跟着变。不要以现有 starter 测试全绿代替官方评测。

## 3. 第三轮：修检索，不跳过调查过程

沿实际调用路径检查 `loader → chunker → tokenizer → index → retriever → service`，重点确认：

- `.md`、`.txt`、`.html` 真正进入索引；GBK、HTML 可见正文、标题、表格和文档编号解析正确。
- 文档数量只算 `KB-xxx` 文件；缓存由知识库内容变化失效，重建后新增、修改、删除文档均生效。
- 中英混合、商品别名、日期与政策版本检索可用。先按适用时间/有效性筛选，再截断到 `top_k`；`/api/retrieve` 在片段充足时恰好返回 `top_k` 条。
- 排序和问答实际使用同一套检索路径，不能为公开题特判或预存答案。

每发现一个缺陷，`DEBUG_LOG.md` 写：现象（题号/日志）、最初假设与被排除假设、验证实验和关键输出、文件行号级根因、修复 commit、修前红修后绿的测试证据。每修一层重新跑受影响类别，观察是否暴露下一层问题。

**验收：** `retrieval` 类得分提升；更换知识库后 `health` 和检索结果同步变化；所有回归测试保留。

## 4. 第四轮：混合问答

按问题判定 `data`、`doc`、`hybrid`、`refusal`、`clarify`。数据库事实调用真实只读工具；文档事实先检索并选择当时有效的版本；回答由经过验证的工具结果与原文证据组成。建议把模型用于意图/参数提取与语言组织，把金额、数量、差值计算和最终数字渲染放在确定性代码里。

`POST /api/chat` 请求是 `{session_id, question}`，响应必须始终为 HTTP 200 合法 JSON，包含 `answer`、`answer_type`、`citations`、`data_evidence`、`trace_id`。拒答和异常也需要可查询 trace。`GET /api/trace/{trace_id}` 要保存检索片段及过滤原因、SQL/工具与结果、完整模型请求、原始模型输出、耗时和异常。

严格要求：

- 数据库数字同时出现在回答和 `data_evidence.result`；证据中的工具名、参数、结果能复现答案。
- `citations.quote` 必须来自对应 `doc_id` 可见原文的一段连续文字，规范化后不超过 400 字符；一轮最多引用 4 份文档。
- 当前规定与历史规定按提问时间选择。数据库与文档数字冲突时遵守 `KB-001`，不能让文档估算值覆盖真实 POS 查询。
- 无数据、证据不足、越权修改、泄露系统提示、文档注入时拒答。数据库连接只读，工具白名单，禁止任意 SQL 写操作。
- 同一个 `session_id` 支持“那 7 月呢”等追问；不同 session 完全隔离，需设容量与过期策略。
- 系统“今天”固定为 `2026-09-01`。超出实际销售数据范围的问题，要区分“无数据”和“有数据但指标为零”。
- `answer` 最长 1200 字符；不能用大量数字、超大结果或整篇文档刷分。逐条对照官方契约第 5 节上限。

**验收：**独立测试纯数据、纯文档、混合、版本、追问隔离、拒答、安全题；每次回答的 `trace_id` 可取；测试数据库文件哈希或指标前后不变。

## 5. 第五轮：大模型接入

从环境变量读 `LLM_BASE_URL`、`LLM_API_KEY`、`LLM_MODEL`。没有 Key 仍能启动，健康、指标、检索保持正常，聊天使用结构化降级。Key 不写入仓库、前端、日志或 trace。若走 OpenAI 兼容 Chat Completions，请把 `LLM_BASE_URL` 原样作为 base URL，路径不擅自增删 `/v1`。

对 DeepSeek 接入特别验证：思考内容不显示给用户；带工具调用时保留收到的 assistant 消息（含 `reasoning_content`）进入下一轮；解析字符串形式工具参数；处理多个工具调用、空回答、截断、错误状态码、超时与 180 秒总预算。不能依赖 `temperature=0` 保证数字正确。对照官方契约第 7.3 节逐项检查。

运行 `python eval/llm_gateway.py preflight --service-url http://localhost:8000`，按命令提示重启服务并完成预检。将实际输出放入 `LLM_SETUP.md`；若未运行就明确标为待完成。最终有 Key 时运行公开题库，记录模型与配置但不记录 Key。

## 6. 第六轮：前端看板与调试面板

完善 `frontend/src/App.vue`，将骨架拆为适当组件。至少包含：日期和门店筛选、营收趋势图、Top 10 商品表、数据质量、聊天输入与会话、证据/引用、trace 调试面板。日趋势调用 `/api/metrics/daily`；Top 10 如无现成稳定接口，给后端增加可选接口并从真实 SQL 排序；不要在前端用样例数冒充结果。

交互状态覆盖加载、空数据、接口错误、无 Key 降级、长时模型调用。证据条目可展开查看参数与结果；引用显示 `doc_id` 和原文；调试面板显示步骤耗时、检索得分、过滤原因、SQL、完整提示词与模型原始输出（注意在面板中脱敏 Key）。开发代理默认把 `/api` 转到 `localhost:8000`；生产部署时把代理改为实际后端地址。

## 7. 最终交付检查

1. 从干净环境按 README 不超过三步启动；提供一条重建命令，替换 `data/` 与 `knowledge_base/` 后无需手改代码。
2. 运行官方全量公开题库，`EVAL_REPORT.md` 同时附 starter 初始与最终输出、命令、commit、模型、是否有 Key；诚实标注未通过题。
3. `DEBUG_LOG.md` 中的每条修复有真实 commit 与红绿测试；`AI_USAGE.md` 给出真实 AI 提示词、误判及人工判断；`DEMO.md` 给出一道混合题的实际问答和证据。
4. 根 README 包含架构图、选型、口径与歧义取舍、启动与重建。确保 `.env`、真实 Key、模型流量日志、数据库派生缓存未入库。
5. 分多次有意义地提交。不要把整个作业压成一个 `finish` commit。

## 可以直接交给另一个 AI 的首条提示词

> 这是 Moneki.ai 实操作业的骨架。请先读本目录 `AGENTS.md`、`IMPLEMENTATION_GUIDE.md`，导入并读官方 `UPSTREAM_README.md`、`docs/API_CONTRACT.md`、`knowledge_base` 中当前有效的 `KB-001`、`starter/HANDOVER.md` 和 `eval/README.md`。先让原 starter 跑起来并保存公开评测基线，不要立即重写或猜 bug。之后按指南阶段实现，每个修复先给失败复现和红测试，再修复并复测，分次 commit。所有数值、引用和分数必须来自实际数据与命令输出，不能硬编码。完成后填齐根目录必交文件并报告未通过项。
