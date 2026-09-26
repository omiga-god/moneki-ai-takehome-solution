# 公开评测报告

本文件记录真实运行结果。提交的原始报告见 `verification/`；本地完整 JSON 在 `starter/var/audit-eval/`，可通过自动脚本重现。

## 原 starter 基线

- 运行时间：2026-09-26 00:42:04（北京时间）
- 命令：`.\starter\.venv\Scripts\python.exe eval\run_eval.py --base-url http://127.0.0.1:8000 --questions eval\public_questions.jsonl`
- 代码 commit：`a43b8bd`（官方作业输入已导入，starter 未修复）
- 模型与关键配置：未配置模型，starter 本地 mock 模式
- 是否有 Key：否
- 总分：**17.00 / 100.00（17.0%）**，11 / 55 题全绿
- 耗时：中位数 0.02 秒，最大 0.11 秒，合计 1.5 秒

| 类别 | 得分 | 满分 | 比例 |
|---|---:|---:|---:|
| metrics | 1.00 | 6.00 | 16.7% |
| retrieval | 6.00 | 15.00 | 40.0% |
| data | 0.00 | 12.00 | 0.0% |
| doc | 0.00 | 16.00 | 0.0% |
| version | 0.00 | 6.00 | 0.0% |
| hybrid | 0.00 | 18.00 | 0.0% |
| multi_turn | 1.00 | 9.00 | 11.1% |
| refusal | 6.00 | 8.00 | 75.0% |
| safety | 3.00 | 9.00 | 33.3% |
| health | 0.00 | 1.00 | 0.0% |

### 基线观察

- `/api/health` 报告 `kb_docs=36`，公开评测期望 35；`valid_sales_rows=18628`，期望 18290。
- 清洗报告所有剔除数量均为 0，且数据期间显示 `start=""`、`end="N/A"`，与实际数据和 KB-001 的清洗规则不符。
- M01–M04、M06 的营业额、退款、订单、销量或日期边界错误；M05 是唯一通过的指标题。
- 检索通过 6 / 15。多道失败题返回固定的 KB-001、KB-002、KB-003 等片段，说明需调查分词、文档加载、排序与缓存，而不是为题目硬编码答案。
- 纯数据题全部受错误清洗和指标计算影响；纯文档、版本和混合题全部未通过。
- 多轮上下文没有稳定保存；越界时间与破坏性请求的拒答判断不完整。

以上仅是评测现象。具体根因、验证实验、修复 commit 和红绿测试应在 `DEBUG_LOG.md` 中逐项补充。

## 清洗与指标阶段（不是全量复测）

- 运行时间：2026-09-26 01:13:03（报告内时间）
- 代码 commit：`75e466c`
- 命令：先在 `starter/` 执行 `.\.venv\Scripts\python.exe -m kbqa.rebuild`，再启动 `uvicorn kbqa.server:app --host 127.0.0.1 --port 8000`，然后在仓库根目录分别执行 `--only metrics` 和 `--only health`
- 模型与 Key：未配置，`llm_mode=mock`
- metrics：**6.00 / 6.00**，M01–M06 全绿
- health：**1.00 / 1.00**，N01 通过。快照为 `kb_docs=35`、`kb_chunks=111`、`valid_sales_rows=18290`、`data_period=2026-05-01..2026-08-31`
- 未重跑其余类别，因此不能把这两项加进基线总分当作新的 100 分结果

原始报告在 `starter/var/eval-metrics/` 与 `starter/var/eval-health/`，该目录不入库。

## 最终版本

- 运行时间：2026-09-26 16:38:42（北京时间）
- 后端代码 commit：`d15e655`
- 命令：根目录 `.\starter\.venv\Scripts\python.exe scripts/verify.py eval`
- 模型与关键配置：没有真实 Key，`llm_mode=mock`，固定今天 2026-09-01；脚本创建全新临时清洗库/索引，官方完整 55 题串行执行。
- 总分：**100.00 / 100.00，55/55 全绿**。
- 耗时：中位数 0.03 秒、最大 0.13 秒，合计 1.7 秒。
- 原始报告：[verification/public-eval.md](verification/public-eval.md)。

| 类别 | 得分 | 全绿题数 |
|---|---:|---:|
| metrics | 6 / 6 | 6 / 6 |
| retrieval | 15 / 15 | 15 / 15 |
| data | 12 / 12 | 6 / 6 |
| doc | 16 / 16 | 8 / 8 |
| version | 6 / 6 | 3 / 3 |
| hybrid | 18 / 18 | 6 / 6 |
| multi_turn | 9 / 9 | 3 / 3 |
| refusal | 8 / 8 | 4 / 4 |
| safety | 9 / 9 | 3 / 3 |
| health | 1 / 1 | 1 / 1 |

## 其他验证

- `python -m pytest starter/tests -q`：**70 passed**。包含临时数据清洗、真实检索与缓存、会话隔离、SQL 写保护、异常 trace、模型证据、总超时、异常请求、日期边界、中文紧邻编号、目标适用时间、价格冲突和 LF/CRLF 事实句一致性。存在一条 Starlette 的 httpx 测试客户端弃用提醒，不影响结果。
- `scripts/verify.py preflight`：后端 `fdf0506`，2026-09-26 16:30:06，**14/14 通过**；60 次模型请求、44 次工具结果回传、32 次问答，最长 120.20 秒。原始报告：[verification/preflight.md](verification/preflight.md)。之后的目标时间和价格语义修复不改变 HTTP/工具协议；最终 GitHub Actions 会再次全量执行。
- 前端 `npm run build`：TypeScript 检查和 Vite 构建成功。按需引入 ECharts 后主包约 573 KB（gzip 200 KB），仍有大于 500 KB 的性能提醒。
- npm audit（官方 registry）：0 个已知漏洞。ECharts 已升级 6.1.0；Python 环境 pip-audit 在升级 pip 后同样无已知漏洞，`pip check` 正常。
- 浏览器已验证看板、门店筛选、混合问答、查询证据和追踪。紧邻中文的 S02 问句现按正确门店查询；发现的问题及红绿测试见 DEBUG_LOG。
- 干净安装：将 `45239dd` 的 `git archive` 解压到新的 `work/clean-review/`，新建 Python 3.12 venv 并按 requirements 安装，重新 `npm ci`。结果仍为 68 passed、公开题 100/100、前端构建成功，未借用原目录的索引、清洗库或 node_modules。
- 当前跟踪文件及 Git 历史的常见 API Key/私钥模式扫描未发现命中。该模式扫描不是全面的密钥或安全审计。

## 结果的边界

首次 Linux CI 在 `4e354bc` 上只有 95/100（C07、H03 失败），暴露了 Windows 换行导致事实切块不同的问题，现已由 `d15e655` 修复；没有把 Windows 满分当作跨平台满分。云端最终结果可在 [GitHub Actions](https://github.com/omiga-god/moneki-ai-takehome-solution/actions/workflows/verify.yml) 查到，包括完整报告 artifact。

公开题没有失败项。**真实 DeepSeek 全量题库未跑，隐藏题未知**；假模型 preflight 只证明协议行为。系统是本地评审用途，不具备公网身份鉴权和部署防护。不能用公开题满分或依赖扫描为零声称绝对没有漏洞。
