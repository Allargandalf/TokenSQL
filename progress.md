# TokenSQL 开发进度

最后更新：2026-10-09（Asia/Hong_Kong）

## 2026-10-09 OpenCode 报价工作台（已实现与验收）

- 用户已改为少量 Excel 由 OpenCode 通过文件 + Prompt 直接维护数据库；旧多阶段语义导入保留兼容，不是本轮主流程。
- 已从 main@6248325 创建 agent-workspace；初始工作树干净。主 Agent 分工数据 store、前端与 OpenCode 集成，独立核验。
- 所有实现/测试/服务只在远端，不读取 handoff，不更改共享 Strata 或 coding 服务。
- 已实测 OpenCode 1.18.34 独立服务支持 health、session、prompt_async、message、event；xlsx 可由 Agent 的 Python/openpyxl 工具直接读取。
- [x] 报价 store、事务修改记录、筛选/编辑/CSV 与 XLSX 导出。
- [x] 左侧数据表与右侧真实 OpenCode 聊天，拖放/粘贴附件。
- [x] 独立后台 Agent 会话直接写报价库，完成后表格刷新。
- [x] 数据接口回归、真实 OpenCode 文件写库、浏览器交互验收。
- [x] 启动服务与 SSH 转发使用说明。

验收证据（严格区分）：

- 主 Agent 独立执行完整回归：274 passed，1 条既有 Starlette/httpx 弃用 warning；JS 语法、git diff 检查通过。
- 真实 OpenCode 1.18.34 + Strata：生成的两行 XLSX 写入四条正确报价；后续真实浏览器只读聊天 POST 202/completed、实际 0 修改。
- 真实网页全链路：选择生成 XLSX + 明确 Prompt → POST 202 → OpenCode 写临时库 → 表格自动刷新为 4 条，耗时 23.4 秒，仅一次请求无重试。主 Agent 只读 SQLite 独立核对四组模型/方向/金额、CNY、供应商、百万 token 单位、B2/C2/B3/C3 来源及四条 added 审计，全部精确匹配。正式新库前后仍为空。
- 浏览器交互：分页/跨页勾选、筛选后清除隐藏选择、金额编辑、折扣切换、Enter 编辑、当前/选中 XLSX 与模型 CSV 导出、文件 chip、桌面 2:1 和窄屏上下布局均通过。主 Agent 亲自检查远端截图。
- 可重复只读浏览器回归 `tools/workspace_browser_smoke.py`：实际点击发送、tool 摘要、failed 后真实 changes 显示与刷新、页面错误监听通过，不写业务库。真实请求验收无 pageerror/console error。
- 证据位于 ignored `var/workspace-evidence/`：`opencode-smoke-2026-10-09.json`、`browser-2026-10-09.json` 与截图。仅生成样表，不代表全部真实业务 Excel 都已验证。

终审修复：虚拟环境 Python symlink 误解析、上传失败遗留 busy、80 MiB 合计容量限制、模型 HTTP 200 error 响应、SIGTERM 子进程清理、Enter 误提交、聊天发送未定义变量、failed 已写库不刷新、审计汇总前提前公布 completed。追加慢审计和 shutdown 取消回归。Subagent 最终复核未见剩余 Blocker/Major。

交付：HTTP 8765 + 独立 OpenCode 8766 均仅监听 loopback，最新服务 health/agent ready 实测通过；自己启动的临时 8785 已关闭。新报价库 `var/workspace/quotes.sqlite3` 初始为空，原 Excel/旧价格库/验收库保留。代码提交在 `agent-workspace`；本轮不推送、不创建 release。启动与 SSH 转发见 `docs/workspace.md`。

边界：全部实现、依赖、浏览器、测试和服务在远端，无本机项目文件写入；不读取 handoff，不调整共享服务。尚未全量验收真实业务表、与 coding 任务同时段压力、官方价采集或 PostgreSQL/容器；不将这些未做项当成本轮网页已交付功能。

以下记录保留历史；最新验收以本节最终追加结果为准。

## 总体状态（以下为历史记录）

- 当前阶段：2026-10-07 Qwen 语义导入已在分支 `qwen-semantic-import` 实现并通过 mock 回归（250 tests）；真实 Strata 元数据与两次小规模真实抽取已实测成功；三份真实样表 + 用户上下文的 API/浏览器/数据库实机闭环验收尚未执行。
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

以下勾选为 2026-10-07 实施轮（分支 qwen-semantic-import）状态，证据级别见下一节说明。

- [x] 上传控件旁自由文本 Prompt/Context，并贯通 UI/API/CLI/ImportContext/持久化（mock 测试通过；浏览器实测待做）。
- [x] Strata 接口适配、实际模型名核实、真实结构化响应小规模实测（元数据 + 2 次小规模真实 JSON 抽取成功；与 coding 任务并发压测待做）。
- [x] Qwen 整表理解、逐行价格语义抽取、新目录建议与字段证据（mock 测试通过；真实样表全量闭环待做）。
- [x] 规则通过的高置信度自动发布/标准化；仅异常 review，改造旧审核/verified 状态机（mock 测试通过）。
- [x] 三份真实样表+用户 Context 的 API/浏览器/数据库闭环、超时/重复导入/Prompt 改变回归（实机完成 2026-10-08；浏览器仅 AppTest，人工交互未做）。
- [ ] 真实官方网页/Qwen 价格语义抽取；PostgreSQL/容器部署按实际需要单独验收。

模型方向：优先复用 local-coding-agent 的 Strata 标准接口；本轮元数据确认 http://127.0.0.1:8080/v1 与实际 ID qwen3.8-flash-next-iq3_s（4 slots/vision）。用户报告多模态与联网已实测；本轮未发推理请求，bare HTTP 不自动联网，客户端扩展字段/schema 兼容缺口见交接说明。不再要求旧 Qwen-32B 实机验收；27B Flash 仅作为待确认的独立部署备选。

## 2026-10-07 Qwen 语义导入实施轮（进行中，未 commit）

分支 `qwen-semantic-import`（基线 main@1827e82）。主 Agent 统筹；实现/review 由 Subagent 分工（本环境无法固定 Subagent 为 gpt-5.6-sol，已用可用 general Subagent 执行并全部由主 Agent 独立复验）。

已完成（代码在工作区，未提交）：

- LLM 层：`llm/semantic.py`（SemanticQwenClient：verify_model 预检、整表理解先于逐行、分批行抽取、json_schema strict + reasoning_effort、进程级按 base URL 共享 max_in_flight=1、trust_env=False、错误分类 timeout/http_status/transport/invalid_json/schema_invalid/empty）；`llm/extraction_schemas.py`（TableUnderstanding/RowExtraction 严格 schema，金额一律字符串）；`llm/client.py` envelope 兼容 Strata 扩展（cached_tokens/timings/reasoning_content，未知 envelope 字段 ignore，业务 JSON 仍严格）；默认 base URL 改 `http://127.0.0.1:8080/v1`、默认 model `qwen3.8-flash-next-iq3_s`。
- 上下文贯通：ImportContext 新增 prompt_text/context_defaults/interpretation_version；normalize_prompt + prompt_sha256 进入幂等键与 ImportBatch.import_context（context_scope/context_version）；provider_id 仅无 prompt 时必填；API ImportRequest 透传 + `GET /imports/{id}/context`；CLI `--prompt/--prompt-file`、`--profile/--provider-id` 可选、qwen_enabled+prompt 走语义服务；UI 上传旁 Prompt 文本框 + 4 个结构化默认 + 上下文回显（build_import_request 纯函数）。
- 语义管线 `services/semantic_import_service.py`：模型调用全部在 DB 写事务外；校验门槛（默认 0.85，同时作用于 row 与 model_name/components 字段置信度）、币种白名单与默认填缺不覆盖明示值、单位归一化（百万/千 token 换算）、metric 解析、evidence 坐标（含 sheet 前缀）、conflicts/缺字段/FX 缺失进 review；高置信+规则通过：服务层 get_or_create 目录（alias 不 verified）+ 自动发布（actor=qwen-auto:<model>、validation_method=auto_rules、审计齐全）+ 自动标准化（CNY 直接、外币需 verified FX 且快照观察日对齐 FX 日期）；`reextract_staged_quote` 重抽取异常行；`POST /staged-quotes/{id}/re-extract`（qwen 关闭 409）。
- 独立 review 修复：provider/manufacturer 默认值不再覆盖明示值且冲突进 review；千 token 换算；低置信行不写目录；envelope extra=ignore；模型阶段移出写事务；normalization 失败容错；prompt 注入防护措辞；UI 空串占位不阻塞侧栏合并。

验证级别（严格区分）：

- mock：`uv run --no-sync pytest -q` = 252 passed（基线 187 + 新增 65）；compileall 通过。全部模型交互为 httpx.MockTransport。
- 实机（真实 Strata + 真实样表 + 验收 SQLite `var/acceptance.db`，2026-10-08）：
  - 三份真实 Excel + 中文用户 Context 全链路完成。修正 prompt 后：token折扣.xlsx 5 条自动发布；token折扣(2).xlsx 22/26 自动发布（v3 prompt，API 路径 batch 11）；token报价 (1).xlsx 121 行全部 review（按秒视频报价无对应 billing metric、折扣行未指明作用 metric、低置信度——均为真实歧义，review 是正确结果）。
  - 自动发布溯源实测：snapshot verified、`publication_idempotency_key=auto:...`、raw_payload 含 `_auto_extraction`（actor=`qwen-auto:qwen3.8-flash-next-iq3_s`、validation_method=auto_rules、prompt_sha256）；组件 raw 6.215 CNY/1,000,000 token；normalization CNY/百万 token 正确；audit_event 记录 `auto_publish_price`/`auto_resolve_catalog_record`（actor qwen-auto）。目录自动创建：5 manufacturers/15 models/1 provider。
  - 幂等：同文件+同 prompt+同版本重放复用批次（reused_existing=true，0 模型调用）；改 prompt 或改 extraction_version 生成新批次。
  - 并发探针：两个 SemanticQwenClient 并发 understand_table，max_in_flight=1 串行化（2.8s/6.4s，均成功）。
  - API 实机（127.0.0.1:8010 + 验收库）：/health ok；POST /imports（prompt 路由语义服务）；GET /imports/{id}/context 返回完整 prompt/sha/defaults/interpretation_version；POST /staged-quotes/414/re-extract 真实重抽取成功（仍 needs_review: manufacturer_missing）。
  - UI：Streamlit AppTest 对运行中 API 加载无异常，Prompt 文本框与上传控件存在。浏览器人工交互未做（无 chromium）。
  - 实机发现并修复：models 列表 status 为对象 `{"value":"loaded"}` 导致预检误拒（已修+回归测试）；column_roles.notes 被严格 schema 误拒（已加可选 notes+测试）；prompt v2/v3 澄清"用户上下文澄清表头语义不算冲突"、枚举条件键（新增 variant/resolution）、要求 discount 指明 dimension/direction。
  - 实机确认正确的 review 行为：表头"折扣"实为折后价的语义冲突由模型主动上报（旧 prompt 下 26/26 review）；offering 身份跨批次冲突 `catalog_conflict` 进 review；按秒报价 metric_unresolved 进 review。
- 未做：真实官方价采集、PostgreSQL/容器、浏览器人工交互、与 coding 任务同时段压测。

终审（2026-10-08，只读 Subagent 复核 `git diff 9a0f2eb..f99977f`）：无 Blocker；2 Major 已修复并回归（253 passed）：

- Major 1 失败批次幂等遮蔽：复用查询现排除 `failed` 批次，重放时复活原批次（状态 processing、清 error_message、幂等键保持唯一），`_record_failure` 对再次失败更新错误信息；新增 retry-after-failure 回归测试。deployment 文档重试语义同步。
- Major 2 prompt 版本文档滞后：README/deployment/.env.example 的默认版本同步为 `qwen-extract-v3`/`qwen-table-v2`。
- Minor 已修：re-extract 缺 stored understanding 改 ValueError→409（原 503）；`_idempotency_key` 对 context_defaults/vendor_context 做 `_json_safe`。
- 已知未修（记录）：config 版本旋钮与 semantic.py 常量脱钩（table 版本不进幂等键）；re-extract 模型调用仍在 DB 事务内（最长阻塞=timeout）；re-extract 旧批次回退版本字符串为历史值；normalization 失败仅审计不建 ReviewItem；`_workbook_summary` 二级截断；verify_model 绕过信号量；UI prompt 与 qwen 关闭时 provider_id 必填放宽按 prompt 判定。均为 MVP 可接受，后续迭代处理。

下一步（恢复时按序）：1) 隔离验收库 alembic upgrade + 最小 bootstrap（source+metrics）；2) 三份 test/*.xlsx + 明确中文 Context 跑 SemanticExcelImportService 实机闭环并记录各 Sheet 自动/review/失败数与价格证据；3) 起 API/UI 实测（无 chromium，用 streamlit AppTest + 运行中服务 curl，浏览器人工交互如实标注未做）；4) 并发探针（2 路 TokenSQL 请求 + 网关排队）；5) 全绿后按小任务 commit 到 qwen-semantic-import 并请 review Subagent 复核 git diff 1827e82。

## 2026-10-07 交接轮验证（历史）

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
- 已从远端成功推送公开个人仓库 Allargandalf/TokenSQL 的 main；初始代码基线为 1827e82be9cac4f644bb35105ba47d89d052dab9，作为后续 Qwen 改动 review 的比较点。
- 三份原 Excel 仅从 Git 暂存区移除，服务器原件未删除，也未进入首次提交历史。公开克隆需自行提供报价样表，真实样表测试只在保留原件的远端执行。
- 用户已完成远端 GitHub 设备网页登录；认证资料仅位于不跟踪的 .git 内，目录权限700/凭据文件600，项目级官方GH credential helper用于推送。未修改本机C盘或Git配置，不在仓库/remote URL/文档中记录令牌；未修改业务源码、共享服务或现有数据库。
- 公开推送自动检查要求去敏：完整交接/环境原文保留在被忽略的 .private-docs/；公开文档替换内网地址/真实账户路径，环境报告只保留通用开发结论。未发布过含这些环境信息的提交，不创建 release/tag。
- 主 Agent 验证：GitHub为public、默认main，首次远端SHA与本地基线一致；API查询release数量0，本地无tag，三份Excel原件仍在服务器且不跟踪。
- 只读Subagent公开范围review通过；源码/tests/alembic相对初始基线无变化。这是版本控制交付，不是新智能导入功能验收。
- 后续建议Qwen从main创建工作分支；review用 git diff 1827e82 -- src tests alembic 查看代码变化，同时检查新增/未跟踪文件。此次收尾仅追加进度文档提交，不改变代码基线。
- 用户最新约束：禁止再阅读handoff；已通知Subagent停止相关读取，收尾只更新本进度文档并核对Git元数据。

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
