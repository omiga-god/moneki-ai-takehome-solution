# 调试记录

基线是 commit `b251002` 的未修改 starter，公开题库 17.00/100。本轮只改清洗、指标和 health 计数，没有改检索排序，也没有改前端。修复提交是 `75e466c`。

## 缺陷 001：销售明细没有按 KB-001 规范化和按序剔除

- 现象：基线 `/api/health` 的 `valid_sales_rows` 是 18628，公开题 N01 期望 18290。`data_period` 为 `start=""`、`end="N/A"`。清洗报告里六类剔除数量都是 0。`sales` 原表有 18628 行。
- 假设：先以为只是日期右边界少算了一天。对过了：即使区间改成闭区间，空金额、`qty <= 0`、未知门店/商品和完全重复行仍会留在表里，`N/A` 和空日期仍会参与 `MIN/MAX`。KB-002 已不是现行口径；KB-001 写明自 2026-05-01 起取代 v2，空金额不回填，退款行要留下。
- 验证：修复前在未改实现的工作区执行 `starter\.venv\Scripts\python.exe -m pytest tests/test_kb001_cleaning.py -q --tb=line`。12 失败，1 通过。和清洗直接相关的失败是：
  - `test_normalize_store_and_product_before_foreign_key`：`" s01 "` / `"p06"` 原样留下，`S99`、`P99` 也留下。
  - `test_date_formats_and_day_first_dmy`：`removed["1_unparseable_date"]` 为 0，期望 3。
  - `test_empty_and_bad_amount_dropped_yen_amount_kept`：空金额剔除数为 0，期望 3。
  - `test_non_positive_and_non_integer_qty_dropped`：数量剔除数为 0，期望 4。
  - `test_duplicate_removed_but_multiline_order_kept`：重复行剔除数为 0，期望 1。
  - `test_removal_stops_at_the_first_failing_rule`：坏日期没有先被计为日期错误。
  - `test_data_period_and_valid_rows_ignore_unusable_source_rows`：`kept_sales_rows` 为 4，期望 1。
  另外用 KB-001 的六步规则对 `data/pos.db` 做了一次独立计数，得到剔除 8 / 150 / 30 / 10 / 40 / 100，保留 18290，日期范围 2026-05-01 至 2026-08-31。这次计数没有写进测试，避免把公开题答案写死。
- 根因：`starter/kbqa/cleaning.py` 原来的 `clean_rows`（修复前约第 77–102 行）把每行都插入清洗表。金额解析失败时改成 0 而不是剔除；日期原样保存；`store_id` / `product_id` 不规范化、不查维表；完全相同的行不合并。因此 `""` 会成为 `MIN(date)`，`N/A` 会成为 `MAX(date)`。
- 修复：commit `75e466c`。先按 KB-001 第 2 节规范化，再按第 3 节的顺序剔除，同一行只记第一条原因。`DD-MM-YYYY` 按日在前解析。带 `¥` 的金额去掉前缀后保留。金额为 0 的行既不是销售行也不是退款行。
- 回归测试：上述测试修前失败。`75e466c` 之后 `pytest tests/test_kb001_cleaning.py` 为 13 passed。`python -m kbqa.rebuild` 打印 `kept_rows=18290`（销售 18196，退款 94），与独立计数一致。

## 缺陷 002：指标仍按旧口径，并且日期区间右开

- 现象：基线公开题只有 M05 通过。M01–M04、M06 的营业额、退款、订单、销量或结束日不对。M05 问的是 2026-09，表里没有这个月，所以两种口径都会得到 0 和 `aov=null`。
- 假设：曾以为客单价函数用了银行家舍入。核对后 `round2` 已经是 `ROUND_HALF_UP`。错在送进去的净额、订单数和数量，以及 `date < end` 把结束日排除了。KB-001 第 4 节要求净额包含负数退款、退款金额取绝对值、订单只数销售行里的不同 `order_id`、销量是销售数量减退款数量、客单价是净额除以有效订单数。
- 验证：同一轮修前 pytest 里：
  - `test_refund_net_orders_qty_and_aov` 实际净额 40.0。样例里 6 月 1 日有一笔 40 元销售，6 月 2 日有退款和另外两笔销售；旧查询既丢掉退款，又因右开区间丢掉 6 月 2 日。第一次写测试时期望误写成 24.95，这是样例加法写错，不是系统输出。正确合计是 `40 - 15 + 0.02 + 0.03 = 25.05`。改正期望后没有再单独跑旧代码，但旧实现返回的仍是 40.0，对 25.05 依然失败。
  - `test_summary_range_is_closed_on_both_ends` 实际净额 5.0，结束日的 7 元被丢掉，期望 12.0。
  - `test_daily_includes_every_day_and_the_end_date` 结束日净额 0.0，期望 7.0。
  - `test_rebuilt_metrics_follow_the_source_rows` 单日查询得到 0.0，因为 `start == end` 在右开区间里是空集。
  - `test_empty_range_returns_zeros_and_null_aov` 修前就是通过的，对应 M05。
- 根因：`starter/kbqa/tools.py` 修复前的 `_where`（约第 52–54 行）是 `date >= start AND date < end`。`query_metrics`（约第 93–109 行）只汇总 `is_refund = 0`，退款金额写成常数 0，订单用 `COUNT(*)`，销量只加销售行数量。这是被 KB-001 取代的 v2 算法：负金额不进净额，客单价用明细行数做分母。
- 修复：同一 commit `75e466c`。汇总改为闭区间；净额是全部保留行的金额和；退款金额是负金额之和的绝对值；订单是 `amount > 0` 的不同 `order_id`；销量是销售数量减退款数量。日指标用同一套净额和订单数，区间内没有行的日期补 0，`aov` 为 null。
- 回归测试：修前上述指标测试失败，空区间测试通过。修后 13 passed。公开题 `--only metrics` 为 6.00/6.00，M01–M06 全绿。测试用的是临时小库，没有把公开题期望数字写进代码。

## 缺陷 003：health 的文档数、有效行数和数据期间不反映真实索引与清洗结果

- 现象：基线 N01 失败。`kb_docs` 为 36，期望 35；`valid_sales_rows` 为 18628，期望 18290。`data_period` 为 `""` 到 `"N/A"`。知识库目录有 36 个文件，其中 `README.md` 没有 `KB-` 编号；另有 `KB-022`、`KB-062` 两个 `.txt` 和 `KB-061` 一个 `.html`。
- 假设：曾以为把文件数减 1、去掉 README 就够了。不够。契约要求 `kb_docs` 是实际进入索引的文档数。修复前装载器只接收 `.md` / `.markdown`，txt 和 html 根本不会进索引；若只数当时的索引，会少于 35。若继续按目录文件数上报，又会把没有编号的文件算进去。`valid_sales_rows` 和 `data_period` 则是缺陷 001 的下游：清洗表等于原表，字符串最小/最大日期就是空串和 `N/A`。
- 验证：`test_health_counts_indexed_docs_and_cleaned_sales` 在临时目录放了 README、无编号 txt，以及 md/txt/html 各一份 KB 文档。修前 `kb_docs` 为 5，期望 3。`test_data_period_and_valid_rows_ignore_unusable_source_rows` 同时失败，见缺陷 001。
- 根因：`starter/kbqa/service.py` 修复前约第 70 行用目录文件数充当 `kb_docs`。`starter/kbqa/loader.py` 的 `SUPPORTED_SUFFIXES` 不含 `.txt` 和 `.html`。`starter/kbqa/tools.py` 的 `valid_sales_rows` 是 `COUNT(*)`，`data_period` 对未规范化的 `date` 做 `MIN/MAX`。索引缓存键原来也不包含装载规则版本，旧缓存不会因为开始接收 txt/html 而失效。
- 修复：同一 commit `75e466c`。`kb_docs` 改为 `len(index.docs_meta)`。装载器接收 `.txt` 和 `.html`，没有 `KB-` 编号的文件仍然跳过。缓存键加入 `LOADER_VERSION`，重建时旧索引失效。`valid_sales_rows` 只数金额不为 0 的保留行。`data_period` 仍取清洗后 ISO 日期的最小和最大日期，因此会随数据变化。没有改检索打分。
- 回归测试：修前 health 测试失败。修后 `pytest tests` 为 30 passed。`python -m kbqa.rebuild` 打印 35 篇文档、111 个片段，并告警跳过 `README.md`。公开题 `--only health` 为 1.00/1.00。当时 health 快照为 `kb_docs=35`、`valid_sales_rows=18290`、`data_period.start=2026-05-01`、`data_period.end=2026-08-31`。

## 本轮命令和结果

在 `starter/` 下：

```text
.\.venv\Scripts\python.exe -m pytest tests/test_kb001_cleaning.py -q --tb=line
```

修复前：12 failed, 1 passed。修复后：13 passed。

```text
.\.venv\Scripts\python.exe -m pytest tests -q
.\.venv\Scripts\python.exe -m kbqa.rebuild
```

全量测试 30 passed。重建保留 18290 行，索引 35 篇文档、111 个片段。

在仓库根目录，服务为 `http://127.0.0.1:8000`：

```text
.\starter\.venv\Scripts\python.exe eval\run_eval.py --base-url http://127.0.0.1:8000 --questions eval\public_questions.jsonl --only metrics --out starter\var\eval-metrics
.\starter\.venv\Scripts\python.exe eval\run_eval.py --base-url http://127.0.0.1:8000 --questions eval\public_questions.jsonl --only health --out starter\var\eval-health
```

metrics 6.00/6.00，health 1.00/1.00。没有重跑其余公开题，总分仍不能从这两项外推。

## 还未处理

检索排序、问答、多轮、拒答和前端都没有改。全量公开题库还没有在这次修复后重跑。
