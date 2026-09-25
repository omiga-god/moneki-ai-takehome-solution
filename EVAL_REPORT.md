# 公开评测报告

本文件记录可复现的公开题库结果。原始机器输出保存在本地 `report.md` 与 `report.json`；最终提交前应再次运行并更新最终版本部分。

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

- 运行时间：2026-09-26 01:56:18（报告内时间）
- 命令：`.\starter\.venv\Scripts\python.exe eval\run_eval.py --base-url http://127.0.0.1:8000 --questions eval\public_questions.jsonl --out starter\var\eval-final`
- 代码：当时工作区已包含后来提交的 `14343a8`（问答挑句、拒答和看板）。检索修复是 `e8ed102`，记录提交是 `91bb80e`。评测时服务已重启并加载这些改动。
- 模型与关键配置：未设置 `LLM_API_KEY`，`/api/health` 的 `llm_mode` 为 `mock`
- 是否有 Key：否
- 总分：**100.00 / 100.00（100.0%）**，55 / 55 题全绿
- 耗时：中位数 0.03 秒，最大 0.14 秒，合计 1.6 秒

| 类别 | 得分 | 满分 |
|---|---:|---:|
| metrics | 6.00 | 6.00 |
| retrieval | 15.00 | 15.00 |
| data | 12.00 | 12.00 |
| doc | 16.00 | 16.00 |
| version | 6.00 | 6.00 |
| hybrid | 18.00 | 18.00 |
| multi_turn | 9.00 | 9.00 |
| refusal | 8.00 | 8.00 |
| safety | 9.00 | 9.00 |
| health | 1.00 | 1.00 |

健康检查快照：`kb_docs=35`，`kb_chunks=131`，`valid_sales_rows=18290`，`data_period=2026-05-01..2026-08-31`。

未通过题：无。

这之前还有一次同代码路径上的全量跑分是 52.00/100（`starter/var/eval-chat/`），失败原因记在 `DEBUG_LOG.md` 缺陷 005 和 006。52 分不是最终分数，也不能把各类别单独相加后当成新的总分。

原始 `report.json` 与 `report.md` 在 `starter/var/eval-final/`，该目录不入库。
