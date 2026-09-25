# 后续 AI 开发规则

先读 `IMPLEMENTATION_GUIDE.md`、官方 `README.md`、`docs/API_CONTRACT.md`、`knowledge_base/` 中当前有效的 `KB-001`、`starter/HANDOVER.md` 和 `eval/README.md`。官方原始文件若尚未导入，先执行 `scripts/bootstrap-upstream.ps1`；不要凭印象编造文件内容。

本项目是面试作业。遵守 API 契约，保留缺陷调查证据和逐步提交历史。每次修复先记录可复现失败、添加会红的回归测试，再修复并复测。`DEBUG_LOG.md`、`EVAL_REPORT.md`、`LLM_SETUP.md`、`AI_USAGE.md` 只写实际发生的过程，不编造分数、测试或 commit。

清洗和知识库索引必须通过重建命令随 `data/`、`knowledge_base/` 的替换而变化；不得把题库答案、数字或文档内容写死。无 Key 启动、查询只读、引用逐字、session 隔离和 trace 可取是硬要求。完成每个阶段后给出命令、关键输出、未解决项。
