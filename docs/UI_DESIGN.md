# 经营工作台视觉设计

## 技能来源

- 项目内技能：`.agents/skills/ui-ux-pro-max/SKILL.md`。
- 来源：https://github.com/nextlevelbuilder/ui-ux-pro-max-skill
- 下载日期：2026-09-26；同日参考仓库版本：`dcc40ff5133ef78276117db0cc34e7b83cc8aeba`。
- 保留上游 MIT LICENSE。完整参考克隆位于被忽略的 `work/ui-ux-pro-max-source`，运行技能所需脚本、数据、参考文档随项目保存。

## 设计取舍

运行 skill 的 `restaurant analytics dashboard minimal --design-system`、收窄后的 `analytics dashboard --design-system`，以及 `responsive dashboard forms --stack vue`。

采用适合经营分析的 Data-Dense Dashboard 建议：核心指标卡、筛选、数据表格、轻量图表、清晰的焦点与加载反馈。两次系统检索的 Enterprise Gateway 营销模板均不符合工作台场景，未采用；导航和内容布局按本项目已有功能设计。

界面使用蓝色强调、蓝灰背景、白色面板和绿色数据质量提示。采用本地系统字体栈，避免依赖外部字体下载；金额增加千分位和两位小数。折线连接真实日数据，不做曲线平滑。未添加虚构增长率、门店或经营数据。

桌面侧栏链接到概览、助手、质量和追踪，当前链接通过 hash 状态标记。手机侧栏变为顶部导航，指标两列、主要内容单列。沿用全部现有 API 和问答证据功能，增加示例问题、空输入禁用、刷新按钮和数据保留率展示。

## 验证

- `npm --prefix frontend run build`：Vue TypeScript 检查与生产构建通过；原有 ECharts 主包仍有大于 500 kB 的构建提示。
- 浏览器真实服务：全部门店与 S02 筛选切换，指标更新；7 月营业额问答返回答案和查询证据；追踪步骤可见；新对话恢复空状态。
- 1440 像素桌面与 375 像素手机截图检查；375、768、1024 像素下 DOM 检查未发现整页横向溢出。
- 添加可见键盘焦点、跳过导航链接、表单名称、消息日志语义与 reduced-motion 样式；未声称完成全部 WCAG 审计。
