# TokenSQL 开发进度

最后更新：2026-10-07（Asia/Hong_Kong）

## 总体状态

- 当前阶段：新需求文档修订/Agent 交接完成，等待新 Agent 实施 Excel + Prompt → Qwen 整表/逐行理解 → 高置信度自动入库。旧基础版历史验收为 187 tests（2026-09-24），新能力尚未实现。
- 项目目录：`<REMOTE_PROJECT_DIR>`。
- 已确认初始内容：`plan.md` 与 `test/` 下 3 个 Excel 报价样例。
- Git：2026-10-07 已按用户授权在远端初始化 main；公开个人仓库为 https://github.com/Allargandalf/TokenSQL，仅纳入代码/文档/测试/配置模板，原始报价 Excel 留在远端。实际推送验收见下方 Git 记录。
- 所有调查、实现与验证仅在远程服务器执行。

## 当前执行约束（2026-10-07 修订）

- 所有项目工作仅在远程服务器；允许修改任务相关项目文件，保留原 Excel、无关文件及历史价格。
- 不停止、重启或修改共享 coding-agent/Qwen/Strata 服务，不影响其他用户进程。
- Qwen 扩大为整表理解、行价格语义抽取、目录查询及创建建议；校验通过的结果可由服务层自动入库，模型不执行任意 SQL/DDL。
- 不再全量强制人工 confirm/approve；低置信度或规则无法通过才进入 review。明确的用户默认币种/厂商/单位可以填缺，不一律待审。
- 原始文件、Prompt/Context、价格历史、字段证据、来源、汇率及自动/人工验证方式必须可追溯。
- 可信内网低安全等级 MVP，保持必要的数据校验和事务/幂等；不增设无关重型安全/权限前置。
- 主 Agent 统筹；写代码/review 的 Subagent 使用 gpt-5.6-sol + high；随任务下发计划与限制。

## 2026-10-07 新需求与交接状态

本轮范围：整理对话补充，修订文档并交接；未修改代码、启动服务、部署模型或实施新导入流程。

- [x] 将用户最新流程与模型选择改为 plan.md 当前规范；旧计划归档，不再要求使用 32B。
- [x] 确认现有 vendor_context 只是 audit-only，现有 Qwen 只挑模型候选；不能把这些旧能力当成新 Prompt/整表理解已完成。
- [x] 创建 AGENTS.md 及 docs/agent_handoff.md，供下一位主 Agent 阅读并下发给 Subagent。
- [x] 更新 README、workflow、环境/数据库设计文档入口，区分历史能力与新目标。
- [ ] 上传控件旁自由文本 Prompt/Context，并贯通 UI/API/CLI/ImportContext/持久化。
- [ ] Strata 接口适配、实际模型名核实、真实结构化响应与 coding/报价分析并发实测。
- [ ] Qwen 整表理解、逐行价格语义抽取、新目录建议与字段证据。
- [ ] 规则通过的高置信度自动发布/标准化；仅异常 review，改造旧审核/verified 状态机。
- [ ] 三份真实样表+用户 Context 的 API/浏览器/数据库闭环、超时/重复导入/Prompt 改变回归。
- [ ] 真实官方网页/Qwen 价格语义抽取；PostgreSQL/容器部署按实际需要单独验收。

模型方向：优先复用 local-coding-agent 的 Strata 标准接口；本轮元数据确认 http://127.0.0.1:8080/v1 与实际 ID qwen3.8-flash-next-iq3_s（4 slots/vision）。用户报告多模态与联网已实测；本轮未发推理请求，bare HTTP 不自动联网，客户端扩展字段/schema 兼容缺口见交接说明。不再要求旧 Qwen-32B 实机验收；27B Flash 仅作为待确认的独立部署备选。

### 本轮验证（2026-10-07）

- 主 Agent 在远端执行 uv run --no-sync pytest -q：187 passed, 1 warning（10.25 秒）；警告仍为 Starlette/httpx 弃用提示。
- 本轮无源码/测试/迁移修改；本次回归是旧基础版基线，不是新功能验收。
- 文档检查：10 份 Markdown、17 个本地链接全部通过；核心 plan/progress/AGENTS/交接/旧计划归档逐字复核一致，敏感信息模式无命中，无 .orig/.rej 残留。
- gpt-5.6-sol + high Subagent 只读 review 通过；已按建议为 deployment 加旧基础版入口提示，并将 README 的“当前旧 endpoint 不可用”改为历史措辞。
- src/tests/alembic 中 .py 文件集合 SHA-256 修订前后完全一致；所有任务文件修改仅发生在远端。模型只读元数据核查不等于真实生成/联网/并发验收。

以下 2026-09-23/24 勾选与命令结果均是历史基础版记录，不代表上述待办已完成。


## 2026-10-07 Git 版本控制

- 用户明确授权“没有的话帮我创建”；原项目及父目录均不是 Git 仓库，现已运行 git init --initial-branch=main。
- 保留并补充 .gitignore：排除虚拟环境、缓存、实际 .env、运行数据库及 WAL/SHM/journal、凭据/日志，以及 test/*.xlsx；.env.example、源码、测试代码、文档纳入首次基线。
- 已按用户要求只读获取本机 Allargandalf 作者信息，并仅设置远端本项目 Git 作者；未修改本机或服务器全局 Git 设置。
- 用户明确要求新公开个人仓库，只公开代码和文档，不创建 release/tag；已创建 Allargandalf/TokenSQL，计划从远端 main 推送首次基线，作为后续 Qwen 改动 review 的比较点。
- 三份原 Excel 仅从 Git 暂存区移除，服务器原件未删除，也未进入首次提交历史。公开克隆需自行提供报价样表，真实样表测试只在保留原件的远端执行。
- GitHub 凭据只用于本轮认证，不写入仓库/remote URL/文档；本机不写项目文件。未修改业务源码、共享服务或现有数据库。
- 公开推送自动检查要求去敏：完整交接/环境原文保留在被忽略的 .private-docs/；公开文档替换内网地址/真实账户路径，环境报告只保留通用开发结论。未发布过含这些环境信息的提交，不创建 release/tag。
- 初始化后的提交/忽略规则/工作区状态由主 Agent 检查；这不是新智能导入功能验收。
## 历史调查里程碑（截至 2026-09-24）

- [x] 调查 OS、CPU、内存、磁盘、GPU、Python 与依赖环境。
- [x] 调查现有数据库服务与可用权限。
- [x] 调查 Qwen-32B 版本、推理框架、API、结构化输出与工具调用能力。
- [x] 验证访问官方 API 文档和定价页面的网络能力。
- [x] 分析 3 个真实 Excel 样例的 sheet、表头、合并单元格和报价格式。
- [x] 评审 PostgreSQL + SQLAlchemy 与 SQLite fallback，完成数据库关系设计。

## 历史基础版里程碑（截至 2026-09-24）

- [x] 数据库模型与迁移脚本。
- [x] Excel 确定性解析、可选 Qwen 模型匹配与严格校验；Qwen 价格语义抽取仍未完成。
- [x] 模型精确/模糊/语义匹配和人工审核闭环。
- [x] 独立官方 JSON 采集、来源审计与单次/外部定时刷新（fixture/mock 验收；真实厂商待验收）。
- [x] 人民币/百万 Token 标准化、汇率追溯和同维度比较。
- [x] FastAPI 与 Streamlit 管理流程（API/client 测试通过；浏览器待验收）。
- [x] 自动化测试、真实样表 API 端到端测试与部署文档。

## 验收记录

以下保留历史阶段记录；当前计划以 plan.md 新版和上方 2026-10-07 待办为准。勾选只表示当时注明的范围通过，不代表新流程或完整生产验收。

### 2026-09-23 第一阶段调查完成

- 历史服务器资源调查已完成；具体硬件、磁盘和共享环境记录仅保留在远端被忽略的 .private-docs/，公开版不披露主机细节。
- Python：系统 Python 3.10.12，无系统 pip/conda；用户已有 `uv 0.12.18`。后续使用项目独立虚拟环境，不改共享环境。
- 数据库：未安装/运行 PostgreSQL；存在其他业务 MySQL 容器，明确不复用、不修改。实现保持 PostgreSQL 目标并用 SQLite 做首版和测试。
- Qwen 探测前：实际在线的是 `Qwen2.5-1.5B-Instruct`（vLLM 0.6.6，`127.0.0.1:18080`），不是 32B，且未启用自动工具调用。
- 32B：`Qwen2.5-VL-32B-Instruct` 权重和 vLLM 0.8.5.post1 环境存在，但服务未启动；结构化输出和 Function Calling 尚未实测。
- Qwen 探测事件：最小 JSON 结构化输出请求触发既有 vLLM/xgrammar 接口不兼容，服务自退出；日志无 OOM、GPU XID 或外部 kill 证据。未停止、重启、升级或修改服务，且不会擅自恢复。
- 外网：总体可访问官方站点，但部分域名受 Cloudflare/地区限制；采集器须按厂商适配并在失败时进入人工复核。
- 样表：两张折扣表各 26 行；报价表 121 行、20 个合并区域，混合折扣、秒、Token、百万 Token、次、张、万字符和音色等口径。
- 样表风险：9 条按秒报价缺少币种；E 列无表头且为机械 `/0.79` 数组公式；供应商、方案、生效时间和币种上下文普遍缺失，当时按旧规则待审；新流程允许明确的用户 Context 填缺并经规则校验。
- 数据设计：采用供应商/模型/版本/别名/Offering/方案/快照/计费组件结构；导入先进入 staging，历史追加，不覆盖旧价格；只有比较键完全一致且验证通过的价格可比较。

### 第一阶段决策

- 使用 Python 3.10、SQLAlchemy 2.x、Alembic、Pydantic v2、FastAPI；SQLite 用于远端单机 MVP/测试，保留 PostgreSQL 兼容。
- Excel 先确定性解析，再对歧义字段调用可选 Qwen；所有模型输出经 JSON Schema/Pydantic 校验，低置信度或未知项进入人工审核。
- 不依赖 Qwen Function Calling；Python 负责网络、校验、主键、事务和数据库写入。
- 官方价格采集与 Excel 导入相互独立，仅接受官方页面/API 作为正式来源。

### 已启动实现

- [x] SQLAlchemy 数据模型、Alembic 初始迁移和数据库约束。
- [x] Excel 原始结构解析、公式/缓存保留、单位分类和真实样表测试。

### 2026-09-23 Excel 解析验收

- 远端独立 `.venv` 已通过 `uv sync --all-extras --no-install-project` 创建，未改系统 Python 或共享服务。
- `uv run --no-sync pytest -q tests/test_excel_importer.py`：`12 passed`。

### 2026-09-23 核心模块验收

- 数据库：20 张 ORM 表与 Alembic 迁移一致；SQLite 内存 upgrade/downgrade、元数据零差异、PostgreSQL/SQLite 离线 DDL 均通过。
- 历史保护：价格快照、价格组件、标准化结果和审计事件具备 ORM/数据库双层只追加保护。
- 模型匹配：精确→模糊→可选 Qwen；越界候选、限定冲突、低置信、非法响应和 SQL/主键字段全部转人工审核。
- 价格：Decimal 标准化、FX 来源校验、严格 comparison key、折扣基价约束和跨维度拒比通过。
- 官方采集：HTTPS 官方域名白名单、SSRF/跨域重定向/超时/大小/内容类型保护通过；测试未实际联网。
- `uv run --no-sync pytest -q`：`66 passed`。

### 2026-09-23 数据服务验收

- Excel 导入保存文件/行/单元格/公式/缓存/合并来源，按 source + SHA-256 + parser/profile 上下文幂等。
- 三份真实样表按 26/26/121 行进入 staging；未知币种、条件价、公式列、缺供应商和歧义项进入 review，未写正式价格。
- Catalog/Review/Pricing 服务支持多供应商、多方案、多组件、verified alias、审核审计、只追加发布、历史查询、严格比较和 CSV 导出。
- CLI、README、部署文档、无密钥环境模板、非 root Dockerfile 与可选 PostgreSQL/Streamlit Compose 已完成；未启动任何容器。

### 2026-09-24 集成验收与未完成项

- 主 Agent 已独立执行全量测试：162 passed；后续并行修复报告 172 passed，待全部收尾后独立复验。
- 已接入 Excel exact → fuzzy → 可选 Qwen 匹配；原始 XLSX 二进制保存在 SourceArtifact.raw_content，SHA-256 可复核。
- 已新增普通/官方价格发布、FX 创建、持久化标准化 API 与 UI；比较严格要求 verified 持久化标准化记录。
- 发布幂等已加入数据库唯一索引；Qwen HTTP 客户端禁用环境代理。SQLite 迁移与 ORM metadata 一致性已测试。
- 终审仍发现真实 Excel candidates 到可发布 components 的人工确认连接缺失，正在修复，尚不能标记完整端到端验收通过。
- 官方采集目前为 HTTP/审计/调度框架与 one-shot CLI；尚无内置厂商适配器，真实官方采集未验收。
- PostgreSQL 实例、Qwen-32B 实机调用及浏览器 UI 联调尚未验收；没有启动或重启共享服务。
- 最终验收必须覆盖真实样表导入、明确人工确认、审核、发布、标准化、比较和导出，并记录未满足标准。

### 2026-09-24 最终基础版验收（主 Agent 独立执行）

- uv run --no-sync pytest -q：187 passed, 1 warning（10.11 秒）；唯一警告是 Starlette TestClient/httpx 弃用提示。
- uv run --no-sync python -m compileall -q src tests alembic：通过。
- test_initial_migration_matches_metadata_and_downgrades_cleanly：1 passed；SQLite 实际 upgrade/downgrade 与 metadata zero diff 通过。
- uv lock --check：通过；default、ui、postgres 三种 docker compose config --quiet 均通过。未构建或启动容器。
- 真实报价样表 API 全流程通过：121 行导入 → 读取原候选 → 明确人工确认 → 独立审核 → 发布 → 持久化标准化 → 比较 → CSV；测试目录与人工决定均为隔离测试数据。
- CLI 从全新迁移库/bootstrap 以明确 source/provider 导入真实 26 行折扣样表两次，仅产生一个批次。原始上传临时文件清理后，数据库原件字节与 SHA-256 仍可恢复验证。
- 人工 exact/normalized 别名审核后，下一次 Catalog/Excel 匹配可复用正确 provider/version；冲突拒绝并审计。regex 不作为字面别名启用。
- 内置 configured 官方 JSON 适配器与工厂可用，严格 URL/映射/JSON/金额校验；mock 验证采集证据、待审、失败审计及已有官方价格刷新，新发布快照保留 supersedes 历史。
- 发布幂等由数据库唯一索引保护，组件校验失败可完整回滚并继续使用会话；比较不再回退到临时计算，公开标准化仅允许已实现的 v1。
- 文档入口：README.md、docs/workflow.md、docs/deployment.md、docs/official_adapter.md；旧设计目标与实际验收范围已作区分。
- 项目未发现 .orig/.rej 残留；最后一批备份保留在 <REMOTE_BACKUP_DIR>
- 结束检查无 Qwen/vLLM 进程、18080/8001 无监听，TokenSQL Compose 无容器；未执行共享服务恢复、重启、升级或修改。

### 旧基础版未完成项（历史记录，模型要求已被新版取代）

- Qwen-32B 实机结构化输出/Function Calling 尚未验收；既有小模型服务的兼容性故障见环境报告，修复或启用共享服务需服务负责人授权。
- Qwen 目前只参与模型匹配；Excel/官方页面中的歧义价格语义抽取尚未实现，基础版采用确定性解析与明确人工确认，不应宣称该项完成。
- 官方 JSON 适配器需配置真实、经审核的厂商 URL 与字段映射；本轮仅 fixture/mock 验证，真实厂商抓取未执行。
- PostgreSQL 实例、容器构建/运行、浏览器交互和生产备份恢复尚未验收；当前为远端源码及 SQLite 隔离测试基础版。
- API 审计 actor/verified 是人工操作声明，不是登录认证；使用范围限于受信任 loopback 或带鉴权代理。HTTP DNS 校验与实际连接仍存在需部署层防护的 DNS rebinding 时间窗口。
- 因上述范围限制，本记录表示基础版自动化验收通过，不表示 plan.md 全部生产/实机标准已完成。
