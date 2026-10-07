# TokenSQL 下一位 Agent 交接说明

更新：2026-10-07（Asia/Hong_Kong）。本轮是用户要求的文档修订与 Agent 交接，不是新流程实现。当前用户消息决定授权范围；旧计划/历史文档不自动授权改共享服务或扩大任务。

## 一、对话中需要补充的事实与偏好

- 只在远端工作：`<REMOTE_HOST>`、SSH 用户 `user`、根工作区 `<REMOTE_WORKSPACE_DIR>`；TokenSQL 位于 `<REMOTE_PROJECT_DIR>`。本机只是连接入口，不创建/编辑本机项目文件。
- 用户曾提供 SSH 登录密码；不在交接文档重录。新会话使用已有连接/凭据；确实缺少访问凭据时向用户安全索取，不猜测。
- 用户提到 VSCode 设置了“强制 UI 显示”；这是已有编辑器偏好，不是本任务授权改本机 settings 或把实现转到本机。
- 由主 Agent 统筹任务，给所有 Subagent 传递计划、现状及限制；写代码/review 使用 `gpt-5.6-sol` + `high` reasoning。执行中维护 progress.md，主 Agent 按 plan.md 独立验收。
- 项目低安全等级、可信内网，以实用闭环优先；不用过度工程化的安全门槛/复杂权限或全量双重人工批准，但不能丢原件、破坏历史或影响他人进程。
- 用户明确不想继续用旧 32B。优先复用 local-coding-agent 中已暴露的 Strata Qwen 接口；备选“Qwen3.8 Flash 27B”只有在必要时确认真实名称并评估隔离部署。
- 用户报告该模型多模态+联网已实测成功；这不等于 TokenSQL 的 JSON 抽取、价格解释、自动入库和并发已实测。

## 二、新产品要求（取代旧流程限制）

Excel 上传旁直接放自由文本 Prompt/Context，例如“模型厂商 XX；报价商 YY；本表缺省 CNY；每百万 Token；B 输入/C 输出；报价生效日……”。

Excel + 用户上下文 → Python 结构解析 → Qwen 整表理解 → 逐行语义抽取 → 规则校验/标准化 → 高置信度且校验通过自动入库；低置信度或规则异常才 review。

Qwen 不再只是固定候选挑选器：允许解释列/备注/条件/折扣、推断名称、提出新目录项/别名/方案、通过应用工具查目录和有来源的官方资料；服务层校验后自动创建/关联/发布。仍由应用管理主键、事务与关系，不需要给模型任意 SQL/DDL 权限或数据库密码。

用户默认币种等是合法业务上下文，可用于补缺；文件明确值与默认值冲突时不静默覆盖，明确“纠正/覆盖”可执行但保留原值/依据。review 不是每一行必经步骤。

## 三、2026-10-07 Strata 接口只读核查

位置：`<REMOTE_WORKSPACE_DIR>/local-coding-agent`；运行实现位于 `strata-next/`，不要因为另一个 `strata/` 目录存在就误认运行版本。

- API base URL：`http://127.0.0.1:8080/v1`。
- `GET /v1/models` 返回唯一已加载 ID：`qwen3.8-flash-next-iq3_s`。这是当前真实本地 ID，不应改写成猜测的官方模型名/参数规模。
- 监听端口可能因启动器避让端口冲突改变，应通过 env 配置，并用 health/model 元数据预检，不把 8080 当永久地址；不让健康检查加载/重启模型。
- 主调用：`POST /v1/chat/completions`；另支持 `POST /v1/responses`、`POST /v1/messages`。
- 元数据报告引擎 0.1.39、context 262144、4 个 serving/requested slots、text/image 输入及 vision enabled。这是核查时运行状态，不是无限并发/TokenSQL 多模态验收承诺。
- 配置/接口未发现用户所称的独立“Flash 27B”可直接调用 ID；不要把正在运行的 Flash-next 模型自动当成 27B。
- 本轮只请求健康/模型/状态元数据并读源码/配置，未发送模型生成请求，也未做 coding+报价抽取并发压测。

证据入口（均为远端路径，不含凭据）：

- `../local-coding-agent/strata-next/strata-iq3_s.json`（监听/model/slots 配置，关键行 38、44、54–55；有敏感字段时只查看白名单项）。
- `../local-coding-agent/opencode.json`（coding 客户端 provider/model 配置，关键行 18、22；不要整份打印含密钥配置）。
- `../local-coding-agent/strata-next/docs/DETAILS.md`：标准路由约 475 行，cache 629–644，stateless Responses 705–729。
- `../local-coding-agent/strata-next/serve/responses.py:307`：拒绝 previous_response_id/conversation，要求每次发送完整 input。
- `../local-coding-agent/strata-next/serve/server.py`：请求/缓存约 1080 行，非流响应 usage/timings 2648–2699，最终模型实际 ID 可不同于请求 model；应主动查 models 而不是用错误名字“请求成功”证明身份。
- `../local-coding-agent/strata-next/docs/DETAILS.md`：MCP 与客户端 tools 762–800，结构化输出 1093–1110；`serve/structured.py` 校验回退 35–51、121–146。
- `../local-coding-agent/strata-next/docs/BATCHING.md`：slot/cache 状态与排队/长 prompt admission/batch 差异（文件名以现场为准）。

### 并发结论与工具边界

可复用同一个模型服务做编码和报价语义分析，没有业务上必须另部署模型的冲突。该结论是接口/源码支持的判断，不是本轮负载实测结果。

- HTTP 请求以 messages/input 独立处理；Responses 是 stateless，不支持 conversation/previous_response_id 存储式串接。
- TokenSQL 自己维护每个批次的 table context/行上下文/工具结果/request_id，不复用 OpenCode/coding agent 的 session 或 shell/工作目录状态。
- 推理层有按完全一致 token/image prefix 匹配的 cache/slot；cache 复用不等于把两个任务对话混在一起。conversation parking 当前关闭（预算 0）。
- 真正风险是共享 4 slots/GPU/队列、长 prompt admission、batch/solo 吞吐差异及超时；当前服务无每客户端配额/优先级。TokenSQL 初始 max_in_flight=1、有界队列/背压（2 需测试后决定）；多进程 worker 共用限流，不能每进程都独立打满。
- 测量排队+prefill+decode 的 P95 后设 timeout（可先保守 120 秒），给 JSON 输出足够 max_tokens；显式选择 reasoning_effort 的 none/low 等档位并按语义质量评估，不让默认 high 思考吃掉旧候选选择的短输出预算。不重配服务以追求 token 级完全一致；失败保持待处理，生成重试受批次/发布幂等保护。
- 标准 Chat/Responses 支持传 tools/返回 tool calls，不表示服务自动执行联网工具；业务客户端通常要执行并回传结果。Strata 服务端 MCP 要显式 strata_mcp:true 且配置 mcp_servers，当前配置无 mcp_servers；OpenCode 的 websearch 来自客户端工具。Responses 文档明确 hosted web_search/file_search 不支持。
- Strata 网页或 coding agent 已经测试的联网/MCP能力不等于所有普通 HTTP 请求自动拥有它；后续需核实并接入独立的网页/官方来源工具，多模态也按真实输入格式测试。
- JSON response format 是“checked, not constrained”，不是强制 token 级约束。当前 Strata venv 未安装 jsonschema，服务端只检查单个 JSON object，不保证完整业务 schema；无隐藏自动重试，失败可能为 502 structured_output_failed。TokenSQL 必须自己严格校验、处理拒绝/非法 JSON。
- 当前 TokenSQL 的 _Usage/_ChatCompletion 使用 extra=forbid，不认识 Strata 的 usage.prompt_tokens_details.cached_tokens（恒有）与顶层 timings（有计时则带）；静态检查可知旧 client 会拒绝合法网关响应。实施时在 transport envelope 兼容已知扩展/忽略非业务扩展，业务 content JSON 继续严格校验。
- 不动现有服务；只有复用明显不可用或资源争用时，再评估真实的27B备选及独立端口/GPU资源。

## 四、现有代码能做什么，不能做什么

基础版已存在（2026-09-24 历史 187 tests，通过范围见 progress.md）：

- 20 张 ORM 表、Alembic、SQLite 迁移/回滚；源 XLSX 二进制、SHA-256、坐标/公式/缓存/合并来源可保留。
- Excel 确定性解析，精确/模糊/可选 Qwen 模型候选匹配；staging、人工 confirm/review、正式发布、只追加历史。
- Decimal 标准化、汇率来源、严格同维度比较/CSV、发布数据库幂等及事务回滚。
- FastAPI、Streamlit 客户端管理入口；bootstrap-catalog、CLI 与 configured 官方 JSON 适配器/独立刷新框架。

关键落点：

- `src/tokensql/ui/app.py`、`ui/client.py`：上传和表单；现状偏 JSON profile/vendor_context，没有所需的普通 Prompt 文本框闭环。
- `src/tokensql/api/app.py`、`api/schemas.py`、`cli.py`：上传/请求 schema、CLI 参数，需贯通自由上下文。
- `services/import_service.py` 的 ImportContext.vendor_context 明确为 audit-only；不能“字段已有”就声称提示词已进模型。
- `llm/client.py`、`llm/schemas.py`：当前仅 candidate_index/confidence/reason/abstain 模型选择；严格旧响应模型、旧 18080/32B 默认要改为真实接口/响应能力。
- `services/catalog_service.py`、`staged_quote_service.py`、`review_service.py`、`pricing_service.py`：新目录建议、自动确认/发布和异常审核，避免旧状态机强制所有待批。
- `db/models.py`、`alembic/`：上下文/整表解释/模型版本/字段证据/验证方式与幂等变更，按实际字段选择迁移，不假设设计文档中的列都已存在。
- `pricing/normalization.py`、`comparison.py`：保持原价/FX/同维度约束，自动验证不伪造人工批准；不能绕开已实现的算法版本检查。
- `official/`、`services/official_service.py`：当前 configured JSON/mock 框架，不是 Qwen 官方网页语义抽取。

尚未完成：Prompt 自由文本实际参与推理、整表理解、逐行价格语义抽取、校验后自动发布、真实 Strata 抽取/并发实测、真实厂商采集、浏览器交互验收。PostgreSQL 实例/容器运行仍无历史实机验收；SQLite 足够当前 MVP，不因此阻塞新流程。

Git 状态补充（2026-10-07 用户授权创建）：项目已在远端初始化 main，个人公开仓库为 https://github.com/Allargandalf/TokenSQL；仅代码/文档/测试代码/配置模板纳入基线，三份 test/*.xlsx 原件留在远端且被忽略，不公开，不创建 release/tag。作者信息只读复用本机 Allargandalf 配置，仅设置远端项目 Git 配置，未修改本机或全局配置；实际提交/推送记录见 progress.md。Qwen 接手先看 git status，从 main 基线创建工作分支，按小任务提交；主 Agent 可用 git diff <基线SHA> review，不擅自 reset/覆盖历史。历史补丁备份在 `<REMOTE_BACKUP_DIR>` 等远端临时目录；未删除原始 Excel。旧进程/端口调查不是实时状态。

## 五、下一位主 Agent 起步

1. 读取 AGENTS.md、plan.md、progress.md、本交接与 workflow；确认只在远端工作，运行现有测试建立当前基线。
2. 独立小规模核查实际模型/结构化 JSON/tool 行为与共享并发；不要启动旧32B或重启现有 Strata。
3. 按 UI/上下文贯通、Qwen整表+行抽取、目录/自动发布状态机分工，gpt-5.6-sol high 写代码/review；主 Agent 自己集成与验收。
4. 不再重建已有基础模块；重点解决现有候选选择和全人工发布无法满足新链路的问题。
5. 使用 test/ 的三份原件（历史识别为26/26/121行，重新按当前解析核对），结合明确的用户 Context 验证正常行自动入库与异常行 review。
6. 用 plan.md 新验收标准记录 API/浏览器/数据库、模型实测、回归命令和未完成项；不要把合成目录、mock模型或用户报告当业务批准/实机验收。
