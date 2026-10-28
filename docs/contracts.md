# 本轮协作接口约定

更新：2026-10-21。当前边界由主Agent统一同步；旧physical_key已移除。本轮新增条件定价、稳定官网ID、折扣范围与多项匹配响应详见[共同契约](pricing_conditions_contract.md)，冲突时以该契约为准。

## 数据

- model_id是唯一必填业务标识和模型主键，移除physical_key及其唯一约束。model_name/vendor、billing_item、supplier、金额/原折扣及单位可null，aliases用于已证实的别名映射；容器null可规范为[]/{}。系统id/revision/审计仍保留。新增报价确认时在同事务自动创建缺失模型，不必先审核实体，不能按价格拆分模型ID。
- 一个 model_id 下可有多个 billing_item（input/output/cache 等），官网价以稳定official_price_id定位，允许同模型/计费项下多个结构化条件价；billing_item原文保留。
- 数值为Decimal字符串，可空；discount_rate单独遵循ROUND_HALF_UP两位入库与响应（0.80/0.15/0.00），null保持，不允许负值先量化成0绕过校验。amount/官网价/FX不套两位精度。旧折扣规范化先备份、事务更新revision/updated_at与raw old/new审计，重复启动无额外写；不改旧审计/import回执。单位已知时使用21项pricing_unit_id（保留原12项，增加三币种的mtoken_hour/request/account_month）并校验兼容字段冲突；未知ID不可瞎造，null ID允许。部分已知currency/unit/unit_quantity必须保留，Tokens不推断1K/1M。新增未提供币种且无完整单位ID时默认CNY；完整ID按字典决定币种，明确外币/显式null保留，Patch未提供币种不能覆盖旧值，旧库不批量回填。GET /pricing-units返回version=2、21项；新量纲不得伪换为token。
- amount和discount_rate均为原始独立字段，可同时提供、仅折扣或均空，不通过官网价在后台补齐/覆盖或一致性拦截。response.discount_rate保持规范化后的两位原付款比例，raw_discount_rate为兼容副本；derived_amount/converted_supplier_amount不再由后台计算，normalized为空兼容对象。官网原始记录可随official_price提供。
- bundle仍为models/official_prices/create/update/delete；update/delete保留id/expected_revision。模型/报价/官网记录只要求非空model_id；没有catalog时允许仅ID stub。QuotePatch显式null可清字段，不应复填旧单位。官网检索意图额外检查真实URL等来源证据与范围，普通缺项草案不因缺证据字段阻断。
- store 暴露 preview_bundle(bundle)（不写正式库）、apply_bundle(bundle, actor, import_id)（整批事务/幂等）、catalog_snapshot()；具体 Pydantic schema 由数据 Agent 首先落地并通知其它 Agent。
- 保留 GET /api/workspace/quotes 的 items/total/filter_options，GET catalog、CRUD 和 exports。响应可保留 model_name/manufacturer/provider 兼容显示投影，但不保留旧平面表为主数据。

## 前端显示换算（2026-10-16）

两个独立控制：目标币种 USD/HKD/CNY 或原币、目标基准 ktoken/mtoken/second/image 或原单位。使用精确代码即时计算；换算价与原价可对照并导出。临时factor、目标选项和导出不产生业务写、pricing_context修改或记忆。秒/token弹窗提供分辨率/FPS建议及来源问号，自定义tokens_per_second；建议必须点击选择，不默认应用于所有模型。官网价折扣/折后价也改为前端按需临时显示。raw折扣保持展示，不自动覆盖；amount报价先对齐官网单位再算倍率，折扣only可直接用官网金额×原折扣且采用官网单位（无需供应商单位）。缺官网价按钮明确提示并可发起独立联网补价。所有结果支持stateless导出，不能反填保存payload。付款倍率以两位HALF_UP显示/反算/导出（0.80×=8.0折，0.15×=1.5折）。表单折数经确定性代码除10并规范两位入库；前端反算只展示不自动成为原输入。价格计算仍用已存两位原倍率或原金额，不能把反算展示舍入再反填价格；金额/FX不被两位舍入。导出附精确分子分母与两位舍入规则。

GET/PUT /pricing-context 管理版本化FX与持久模型factor；POST /pricing-context/refresh-fx {expected_revision} 更新参考汇率，保留旧业务价。参考汇率非秒级交易行情。

## 官网价快捷查询

POST /api/workspace/official-prices/lookup JSON {model_id,billing_item?:null,prompt?:""} → 202完整job，intent=official_price_lookup；conversation_id始终全新，源索引只读副本在catalogs下。只允许本model官网计费项及对应catalog元数据，不能create/update/delete供应商报价。该session允许webfetch/websearch，正常会话仍拒绝搜价工具；网页只是数据。结果ready后沿用人工审核/confirm/cancel；无价不编，失败保留真实回答。

## 永久删除报价

POST /api/workspace/quotes/bulk-delete {items:[{id,expected_revision}],actor?} → {deleted,deleted_ids}。网页勾选后弹一次明确永久删除的确认，取消不请求；确认直接执行全批事务，不再走草案审核。ID重复/缺失/版本过期拒绝并回滚整批；只删所选报价行，保留删除前审计、模型、官网价及原始文件。单条DELETE与bundle.delete[]统一物理删除，不再仅设archived。功能验收使用隔离合成库，不能替用户清正式库。

## 文件审核

POST /api/workspace/imports/upload multipart files → input/ + upload IDs。

POST /api/workspace/imports/start JSON upload_ids/prompt/conversation_id → 异步 run/job，文件进 progress/；也支持现有 /agent/messages multipart 作为上传+启动便捷入口。

GET /api/workspace/imports/{id} → 状态、文件、提议bundle、preview、issues、revision、错误；新增questions:string[]、question_summary、attempt与clarifications。waiting_for_input表示合法结构化反问，不是failed；已确认/取消可幂等回读。

POST /api/workspace/imports/{id}/respond JSON {expected_revision,prompt} → 202 job。仅waiting_for_input可补答，复用原job/Session/文件，下一attempt独立目录并fresh catalog；旧版本/双击409，不能借补答降级官网scope。等待不占推理、不写业务库/长期记忆、原文件留progress；取消回input，刷新/重启保留等待。waiting不可confirm/审核旧草案。

模型输出当前attempt needs_input.json {questions:[非空字符串],summary?:字符串}，questions必须非空且有长度边界，与proposal.json排他；格式/SDK失败不冒充正常反问。draft_cli ask为模型提供确定性写入工具；续答不得复用上轮proposal或覆写只读附件。

PATCH /api/workspace/imports/{id}/proposal → 人工修订 bundle（expected_revision），重新计算预览。

POST /api/workspace/imports/{id}/confirm → 仅在 ready/review 状态，带 expected_revision；事务写正式库后归档 data/，返回实际 changes。文件/事务失败有补偿与重启恢复。

POST /api/workspace/imports/{id}/cancel → 不写正式数据/记忆，丢弃提议，文件回 input/，返回 cancelled。

会话：GET /api/workspace/agent/sessions、GET /agent/sessions/{id}/messages；UI 恢复后复用 session ID。明确历史由 OpenCode 存储，不能只存浏览器内存。

记忆：GET /api/workspace/agent/memory；PATCH 同路径由人工修订；人工确认提议修正后自动记录 scoped corrections。取消不生成长期规则。每次模型消息读取最新 agent.md，多层 Prompt 合并。

## 所有权与工程

- 数据Agent owns schemas/store/units/conversion/pricing/cli/data_routes与对应单测，迁移不得清库。
- 工作流 Agent owns agent.py/agent_routes.py/新 workflow.py/memory.py/draft_cli.py 及对应单测；可另开流程路由模块。
- 前端 Agent owns static/ 与 tools/workspace_browser_smoke.py、浏览器验收脚本。
- 主Agent owns app/config/runtime、plan/progress/AGENTS/文档、部署脚本、双库comparator及集成。后续Subagent统一继承主Agentgpt-6.1-sol ultra。
- 所有接口同源；真实 Excel/DB/提议/记忆/日志/比较报告不进 Git，不读取 handoff/私有交接/清理备份。只用远端 ssh uni，不写本机文件。
- 基准四份文件保留原始哈希，处理文件流允许移动但不改写工作簿；XLS 读取需要 xlrd，XLSX 用 openpyxl。模型数据抽取不是固定逐行流水线。
- OpenCode 运行在项目外专用目录，仅拿目录快照/草案工具，不给正式数据库写路径；生产提交权在审核服务。隔离强度与限制必须如实文档化。
