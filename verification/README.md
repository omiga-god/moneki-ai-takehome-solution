# 验证材料

- `public-eval.md`：官方脚本原始 Markdown，2026-09-26 16:30:47；后端 `45239dd`，mock，55/55、100/100。
- `preflight.md`：官方网关原始 Markdown，2026-09-26 16:30:06；后端 `fdf0506`，假模型，14/14。

报告中的本地端口和文件路径由实际运行产生。复现时端口不同不影响结果。完整 JSON 和日志重新运行 `scripts/verify.py eval` / `preflight` 即可生成到 `starter/var/audit-*/`；CI 也上传为 Actions artifacts。

这些报告没有真实模型 Key，不是 DeepSeek 真实模型的质量评测。语义修复后的最新代码还由 CI 执行完整回归；版本对应关系见根目录 EVAL_REPORT。
