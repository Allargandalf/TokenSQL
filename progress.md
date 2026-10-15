# TokenSQL 开发进度

最后更新：2026-10-15（Asia/Hong_Kong）。

## v2.1_cleaned：单一工作台与空白数据起点

- 用户确认唯一主流程为“文件 + Prompt → OpenCode 直接维护数据库”，并要求删除旧代码、清空数据库/test Excel、提交主分支和标签。
- 用户另行明确同意旧上传、会话、日志、旧部署与私有交接资料一并清走，先备份到项目外。
- 基线：`v2.0`（工作台代码 `0c84ec7`，发布记录 `07083fb`）；旧实现可由 Git 历史恢复。本节替代历史大段进度，不保留旧实施要求。
- [x] 完成保留代码依赖审查，移除旧源码/旧测试及工作台 import-legacy 兼容入口。
- [x] 移走旧 var/test/私有交接与部署文档；资料位于项目外受限权限备份，不纳入 Git。
- [x] 精简依赖与入口，版本统一为 2.1.0；补直接上传依赖，移除 SQLAlchemy/Alembic/Streamlit 等旧栈。
- [x] 重写当前 README/plan/进度/工作台说明/Agent 规则，不再携带旧流水线指令。
- [x] 全新环境、相关回归、wheel 和浏览器验收。
- [x] 正式空库与无样例/附件/会话状态核验。
- [x] 发布范围确认：main + 附注标签 v2.1_cleaned，无 Release/强推；发布后核验远端引用一致。

## 验收证据

- 最小运行依赖 7 项；完整开发/浏览器 lock 共 35 个包。全新 Python 3.10 venv 从锁文件安装成功，无旧栈依赖。
- 完整相关 suite：25 passed，1 条既有 TestClient 弃用 warning；新增 4 项清洁初始化/退役 CLI/版本/入口回归。新测试已修 Python 3.10 TOML fallback 和遗漏旧入口保护。
- 构建并在新环境安装 wheel，从项目外验证包导入、2.1.0、当前解释器、两个 console scripts、空库 API、HTML/CSS/JS。wheel 只含包入口与 workspace，不含旧代码。
- 独立执行浏览器 smoke：真实点击发送、工具摘要、失败后的实际变更刷新、宽窄布局与零页面错误通过；API 为浏览器桩，不写正式库。JS 语法和 git diff 检查通过。
- 正式库 workspace_quote/workspace_audit/workspace_request 均为 0；inbox 空，OpenCode 会话列表为空，test/ 仅 .gitkeep，项目内无 Excel。新服务 8765 health 正常、独立 8766 Agent ready；共享服务未调整。
- Subagent 终审无 Blocker/Major；最小安装说明、旧入口保护、无效测试 patch 等 Minor 已收尾。本轮未调用真实模型、未导入新业务样例。

## 约束与下一步

全部项目操作在远端；未读取 handoff/私有交接内容，未回灌备份业务资料，不更改共享 Strata/Qwen/coding 服务。清理仅针对 TokenSQL，旧代码保留在 v2.0，私有资料可由项目外备份恢复，不自动恢复或回灌。

等待用户提供新文件和业务 Prompt；在此之前正式报价库保持空白。测试只使用临时目录造数据，不等同于新业务文件验收，不创建 GitHub Release。
