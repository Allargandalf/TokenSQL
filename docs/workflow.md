# 管理流程与验收边界

## 当前流程：Qwen 语义导入（已实现）

新版规范见 [plan.md](../plan.md)，实施交接见 [agent_handoff.md](agent_handoff.md)。以下按已实现代码描述。

1. 上传与 Prompt。UI 导入页在文件上传旁提供自由文本 Prompt 文本框和 4 个结构化默认输入（默认模型厂商 `manufacturer`、报价供应商 `provider_name`、缺省币种 `currency`、计价单位 `unit_basis`），自动合并进导入请求；API `POST /imports` 请求 JSON 直接接受 `prompt_text`/`context_defaults`；CLI 用 `--prompt`/`--prompt-file`。`POST /imports/profile` 预览解析不写库，UI 要求先预览再"确认导入"。
2. 导入执行顺序：确定性结构解析 → `GET /v1/models` 预检 → Qwen 整表理解（先于逐行，产出列角色、厂商/供应商区分、币种/单位基准等 sheet 级默认）→ 分批逐行语义抽取（默认 10 行/批）。全部模型调用在数据库写事务之外。
3. 校验与标准化。Pydantic strict schema 加规则校验：币种白名单、单位归一化（含百万/千 token 到单 token 的换算）、计费 metric 解析、evidence 必须是该行单元格坐标或 `user_context`、置信度门槛（默认 0.85）同时作用于整行与 `model_name`/`components` 字段、外币必须有 verified FX 汇率、冲突与缺字段进 review。默认值只填缺，不覆盖表内明示值；与默认值冲突进 review。
4. 自动发布。高置信度且全部规则通过的行：服务层在同一事务内自动 get_or_create 目录（manufacturer/provider/model/version/alias（off、unverified）/offering/plan），自动发布 PriceSnapshot（`verified`，actor `qwen-auto:<model>`，审计与 staged payload 记 `validation_method=auto_rules`、模型名、request id、置信度、prompt 哈希），并自动标准化（CNY 直接；外币用 verified FX 汇率）。仅异常行创建 review 项；正常行不要求逐行人工 confirm/approve。
5. re-extract。`POST /staged-quotes/{id}/re-extract`（body 需 `actor`）对单条待审核行用批次已存的整表理解重跑抽取：通过则走同一自动发布路径，否则更新审核项；已批准行返回 409，Qwen 关闭返回 409，行不存在 404，请求体非法 422，批次缺少整表理解 503。
6. 上下文回读与幂等。`GET /imports/{batch_id}/context` 回读 `prompt_text`、`prompt_sha256`、`context_defaults`、`semantic.table_understanding` 等；`ImportBatch.import_context` 记 `context_scope`/`context_version`。幂等键纳入 `prompt_sha256`、`context_defaults` 与 `interpretation_version`：相同文件+相同上下文重放不重复调模型；修改 Prompt 会产生新批次。

模型不可用时：预检或整表理解失败会使批次记 `failed` 并返回 503（无半写数据）；单批行抽取失败只把该行送 review。运维细节见 [deployment.md](deployment.md)。

## 旧基础版实际流程（截至 2026-09-24，历史）

以下描述旧基础版的实际代码与历史验收，用于理解仍保留的确定性路径与人工审核接口；无 Prompt 的导入仍按本节行为。新链路不再要求普通导入手工 JSON profile 或逐行双重批准。

所有命令在远端项目目录执行。先按 README 创建独立数据库、运行迁移并审核 bootstrap 目录；示例目录是测试模板，不能作为真实供应商身份或报价证据。

### Excel 到正式价格

1. 通过 API/UI 上传 XLSX，或运行带 --source-id、--provider-id 和 --profile 的 import-xlsx。导入只写 staging，模型依次进行精确、模糊和可选 Qwen 候选匹配。
2. GET /staged-quotes?import_batch_id=... 查看候选；GET /staged-quotes/{id} 查看原始候选、来源 SHA-256 与审核项。Excel 原件在 source_artifact.raw_content，数据库备份须包含这一列。
3. POST /staged-quotes/{id}/confirm 明确提交 actor、来源、供应商、模型/版本、offering、方案、观察/生效时间、候选索引及计费组件。字段格式以 /docs 中 schema 为准。系统保留原 candidates，保存人工确认依据并创建独立待审核项。
4. 逐项检查后调用 POST /reviews/{id}/approve；不确定或不接受的数据使用 reject。确认与批准是两个独立步骤。创建 exact/normalized 别名规则后，后续导入可复用同供应商/版本的 verified 别名。
5. POST /prices/publish 提供 staged_quote_id、source_artifact_id、pricing_plan_id、actor、verified=true 和稳定 idempotency_key。所有审核必须已批准，未知目录或不一致关联会失败。
6. 外币先通过 POST /fx-rates 保存已人工验证的汇率和来源日期；再通过 POST /price-normalizations 保存标准化结果。公开接口目前仅允许实际实现的 tokensql-normalization-v1。
7. POST /prices/compare 引用两侧组件及持久化标准化记录。GET /prices/history 和 GET /prices/export.csv 查询历史并导出。不同计费维度或适用条件不能直接比较。

Streamlit 的人工确认表单接受上述严格 JSON；候选详情用于逐项核对。actor 是审计声明，不能替代身份认证；MVP 应限定在受信任本机或由带鉴权的反向代理访问。

### 官方价格

配置内置官方 JSON 适配器并显式提供官方来源和域名白名单，运行 official-refresh。任务优先补缺，并刷新已有官方价格；采集结果始终进入待审。通过 POST /official-prices/publish 显式验证发布，追加新快照并保留旧来源及历史。外部调度配置见 deployment.md。

### 历史验证的含义

自动化测试使用真实 XLSX 原件、隔离测试目录和模拟人工决定；测试目录中的供应商/模型与确认操作不是实际业务批准。官方抓取使用合成响应和 mock transport，不证明某一厂商当前页面可用。

SQLite 迁移、持久化、事务回滚、API 全流程及 HTTP 客户端已测试。PostgreSQL 实例、真实厂商采集、Qwen-32B 实机结构化能力、浏览器交互和容器镜像构建尚未验收。就历史基线而言，Qwen 当时仅参与模型匹配，价格语义抽取尚未实现；该限制已被上文 Qwen 语义导入链路取代，其回归测试使用 mock 网关，真实模型闭环与剩余项见 progress.md。
