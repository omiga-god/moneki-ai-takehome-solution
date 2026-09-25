# 演示记录

无 Key，`llm_mode=mock`。2026-09-26 向已重启的 `http://127.0.0.1:8000` 发送。

- `session_id`：`demo-h01`
- 问题：S03 六月第二周（6 月 8 日到 6 月 14 日）的营业额为什么比别的周低这么多？
- `trace_id`：`t-20260901-0001`
- `answer_type`：`hybrid`
- 耗时：`total_ms` 9.2。trace 步骤为 `plan`、`search`、`answer_mock`、`response`。

回答要点：

- 2026-06-08 至 2026-06-14，S03 Juicy Bao Bao 净营业额 3630.00 元，有效订单 102，客单价 35.59，销量 165，退款 0.00。
- 其中 6 月 8 日至 11 日四天营业额为 0。对照区间 2026-06-01 至 2026-06-07 的净营业额是 6117.00 元。
- 引用 KB-020。quote 写明浦东新区市场监管所 2026-06-05 检查后厨排烟，S03 自 2026-06-08 起停业 4 天，6 月 8 日至 11 日不营业。

`data_evidence` 含 `query_metrics`（6 月 8 日至 14 日，`store_id=S03`，`net_revenue=3630.0`）和 `daily_metrics`（8 日至 11 日每天 `net_revenue=0.0`）。同一窗口用 `GET /api/metrics/summary?start=2026-06-08&end=2026-06-14&store_id=S03` 可复算净营业额。KB-020 的原文在 `knowledge_base/` 对应通知里，quote 去掉空白后能在正文中找到。

`GET /api/trace/t-20260901-0001` 返回该次 `session_id`、问题和上述步骤。这次是 mock，没有外呼模型，所以 `llm_calls` 为空。
