# LLM 接入说明

> 这是官方契约 7.4 的填写骨架。当前尚无完成的 LLM 接入，也未运行预检。

## 1. 用了什么

待完成：厂商、模型、协议、SDK 和版本。

## 2. 配置从哪里读

计划使用环境变量 `LLM_BASE_URL`、`LLM_API_KEY`、`LLM_MODEL`；实现后补默认值、读取位置和实际行为。

## 3. 怎么换成你们的

待完成：切换到 `https://api.deepseek.com`、`deepseek-flash` 与评审 Key 的准确步骤，说明是否重启、是否重建。

## 4. 怎么看到发给模型的请求

待完成：代理或日志命令、位置、脱敏样例。必须包含提示词、工具定义与每轮消息。

## 5. 没有 Key 时会怎样

待完成：`health`、`metrics`、`retrieve`、`chat` 的实际无 Key 行为。

## 6. 依赖与安装

待完成：依赖版本、下载大小、首次启动耗时。

## 7. 自测结果

待完成：粘贴 `python eval/llm_gateway.py preflight --service-url http://localhost:8000` 的真实输出。未通过的项目保留原样并解释。

## 8. 已知限制

待完成：实测后列出。
