# TokenSQL Agent 交接规则

范围：<REMOTE_PROJECT_DIR> 全项目。更新：2026-10-07。

1. 先读 plan.md、progress.md、docs/agent_handoff.md，再读 README 和 docs/workflow.md；2026-10-07 新需求取代旧 32B/全人工审核计划。
2. 所有项目文件、测试、数据库与部署均在 SSH user@<REMOTE_HOST> 的远端完成；禁止在本机建立项目副本或写任务文件。不要把登录/API 凭据写进文档、源码或日志。
3. 主 Agent 统筹、分工、汇总及独立验收；写代码/review 的 Subagent 使用 gpt-5.6-sol、high reasoning，并收到相关计划与约束。
4. 新主链路：Excel + 上传旁 Prompt/Context → 结构解析 → Qwen 整表理解 → 行抽取 → 校验/标准化 → 高置信度自动入库，仅异常 review。
5. 用户上下文必须进入模型请求且可追溯，不是 audit-only；普通导入不应要求手写 JSON parser profile。
6. 扩大 Qwen 的语义/目录查询和创建建议权限；服务层校验后可自动写库，不开放任意 SQL/DDL，不把所有行强制送人工批准。
7. 优先复用 local-coding-agent 的 Strata 标准接口，实际 URL/model ID 见交接说明并现场核实；不使用旧 32B。27B Flash 是待核实备选，不默认部署。
8. TokenSQL 请求/工具/会话与 coding agent 隔离；不得重启、升级、替换共享模型服务或影响其他用户进程。
9. 可信内网、低安全等级 MVP，避免重型安全/权限设计；保留金额/单位/关系校验、事务、幂等、来源及价格历史。
10. 允许修改本项目任务相关文件；保留原 Excel、他人数据、历史快照和无关修改。不要擅自初始化 Git 或复用其他业务数据库。
11. 持续维护 progress.md；区分用户报告、配置证据、mock、真实模型调用与浏览器/部署实测，不把历史 187 tests 当新功能验收。
12. 本轮只是文档交接；下一位 Agent 按新版 plan.md 实施、跑回归和真实样表闭环，并清楚报告未完成项。
13. 公开文档已去敏；远端执行如需真实服务器/路径上下文，先读取被 Git 忽略的 .private-docs/ 中对应原始交接文档。不要把私有副本、真实报价样表、凭据或环境细节强制加入提交。
