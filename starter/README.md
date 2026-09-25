# 经营看板 + 问答服务

运营内部用的问答服务：一条线查销售数据库，一条线查公司知识库。
Python 3.12，只用 `fastapi` / `uvicorn` / `httpx`，测试用 `pytest`。

## 跑起来

```bash
make setup      # uv venv --python 3.12 + 装依赖
make rebuild    # 重建清洗表与检索索引
make run        # 起服务，默认 http://127.0.0.1:8000
make test       # 跑测试
```

换一套数据或知识库：

```bash
make rebuild DATA_DIR=/path/to/data KB_DIR=/path/to/knowledge_base
```

`DATA_DIR`、`KB_DIR`、`VAR_DIR` 也可以直接作为环境变量传给 `make run`。

## 目录

| 文件 | 干什么的 |
|---|---|
| `kbqa/config.py` | 环境变量与路径；“今天”固定 2026-09-01 |
| `kbqa/cleaning.py` | 把原始 `sales` 导进 `var/clean.db` |
| `kbqa/tools.py` | 指标查询：汇总、按天、支付方式、商品排行、门店、品类、对比、单价 |
| `kbqa/loader.py` | 读知识库文件，认出 doc_id、标题、生效日期 |
| `kbqa/chunker.py` | 切块 |
| `kbqa/tokenizer.py` | 分词 |
| `kbqa/index.py` | BM25 索引 + 磁盘缓存 |
| `kbqa/retriever.py` | 检索与元数据过滤 |
| `kbqa/docfacts.py` / `units.py` | 从文档里挑句子、出引用 |
| `kbqa/planner.py` / `entities.py` / `timeparse.py` | 意图、实体、时间 |
| `kbqa/answerer.py` / `hybrid.py` / `render.py` | 组装回答 |
| `kbqa/llm.py` / `live.py` / `toolspec.py` | 模型客户端与工具回路 |
| `kbqa/service.py` / `server.py` | 编排与 HTTP 层 |

## 接口

| 方法 | 路径 |
|---|---|
| GET | `/api/health` |
| GET | `/api/metrics/summary` |
| GET | `/api/metrics/daily` |
| POST | `/api/retrieve` |
| POST | `/api/chat` |
| GET | `/api/trace/{trace_id}` |
| GET | `/api/data_quality` |

## 两种模式

配了 `LLM_BASE_URL`、`LLM_API_KEY`、`LLM_MODEL` 就走模型，没配就走本地模板回答。
没有 Key 时服务照常启动，`/api/chat` 不会 500。

交接说明见 `HANDOVER.md`。
