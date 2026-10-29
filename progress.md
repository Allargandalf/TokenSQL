# TokenSQL 已完成事项与验收记录

更新：2026-10-29（Asia/Hong_Kong）。

本文件记录已做工作和当次证据；当前待办统一见[plan.md](plan.md)。下方旧版本的行数、PID、测试数量及业务约定仅代表对应日期，不是今天的运行状态，也不覆盖最新需求。验收范围限制保留用于说明“当时验证到了哪里”，不在这里重复维护待办列表。

## 已完成：v2.4 最终验收与部署（2026-10-29）

- 主Agent在最终代码上全套执行：583 passed，仅1条已有Starlette/httpx弃用提示；git diff --check通过。独立数据/工作流/前端终审无剩余Blocker或Major。
- 主Agent独立执行六套浏览器验证：v2.4档案、专属会话、表格对比、差异入库全链路及两套v2.3回归全部通过；最后的供应商别名规范化另经70项相关测试和差异浏览器复验。四套新浏览器为真实API/SQLite/Workflow结合脚本化SDK，真实Qwen验收单列下文。
- 本项目服务已重启至2.4.0，PID3261433，127.0.0.1:8765健康、独立OpenCode ready。正式页面只读浏览器检查：0页面错误、0写请求，供应商档案入口可见；已实际查看截图。共享模型部署保持原状，18875/18876测试实例已关闭。
- 正式模型、官网价、供应商报价、供应商、导入回执、档案与来源基准均0；三份原Excel与.gitkeep文件哈希和开工时一致；SQLite quick_check=ok。本次没有清库或重新导入用户数据。
- plan只保留原有七项后续任务，所有本轮完成工程转入progress；README、接口/供应商路由/工作台文档、AGENTS与开发规则同步。源码、项目元数据和锁文件版本统一2.4.0。交付标识：main及注释标签v2.4「v2.4 按供应商路由」，不创建Release。

主Agent最终证据：/tmp/tokensql-v24-root-50v31_x8/final-pytest.log、production-verification.json、production.jpg；六套浏览器证据：/tmp/tokensql-v24-root-browser/results.json及各子目录。真实Qwen证据：/tmp/tokensql-v24-real-model-final/result.json。

## 已完成：v2.4 P4/P5 差异解析、审核与真实模型测量（2026-10-29）

- 原表比较接入OpenCode：无语义变化直接复用；变化区域携带表头/单位/邻行解释，重读结果和复用条目合并为完整本期候选，再与最新数据库对账。普通供应商上传也采用稳定来源范围的增量对账。
- 审核页并排显示原表，区分五类变化并可查看单元格依据；显示沿用/重新理解条数及处理耗时，更多请求/token统计折叠查看。提供整表重新理解、本期/长期偏好选择和官网源价相对当前目录的前后变化。
- 完整来源覆盖参与基准建立与复用；多sheet漏读不能因缓存获得完整替换覆盖。基准采用确认版本检查，晚到的旧确认不回退新基准；确认后精简重复快照，只保留该系列最近确认的完整对比。等待与committed恢复保留所需内容，取消不更新基准。
- 主Agent先执行全套569项通过；新增终审用例仍在最终回归中。前端四套v2.4浏览器及两套v2.3回归通过，真实API/SQLite/Workflow、模型接口脚本化，页面/API错误均0；主Agent已查看桌面及窄屏实际截图。

真实模型测量使用现有qwen38-flash-next-local/qwen3.8-flash-next-iq3_s，独立18875/18876实例、16模型合成XLSX及隔离库；未导入用户原件。首次确认16条后，后续所有试验草案均取消，正式用户库不参与。

- 首次读取：23.80秒，1次SDK语义请求、5个LLM步骤、16条解析。
- 相同文件：0.71秒，16条复用、0次模型请求，数据库对账无增改删。
- 仅字体/行高变化：0.83秒，16条复用、0次模型请求，数据库对账无增改删。
- 改1条价格并倒序：15.70秒，复用15条、重新理解1条、1次请求/3个LLM步骤，变化上下文4839字符。
- 同一新版强制整表重读：16.91秒，重读16条、1次请求/3个LLM步骤。与差异路径的16条模型/计费项/精确金额/单位结果逐项相同。
- 单位由USD/百万token改为USD/千token：18.73秒，16条全部重新理解；原数值不被擅自换算。

该16行样例小幅改价仅约7.2%墙钟提速，不宣称普遍大幅加速。差异路径实际非缓存输入token29792、输出1281、缓存读取56483；整表路径分别32025、1667、64732。持续会话末次上下文由首次约25099 token增长至单位变更时51120 token。顺序单次测量受会话历史、缓存及共享推理负载影响；新一轮会话保留档案/摘要，可用于缩短后续上下文。测试没有做并发压力验证。

真实首轮曾发现Qwen将说明附加到source_ref后，使小改价退回整表；现已补充规范来源格式和有界注释地址解析，保留原证据且不改业务note。再次全流程验证了15+1的真实复用路径。首次证据：/tmp/tokensql-v24-real-model-root（标记不作为最终提速验收）；最终证据：/tmp/tokensql-v24-real-model-final/result.json及各阶段job.json。可复验脚本tools/workspace_v24_model_benchmark.py会验证真实复用条数与无变化对账，测试进程退出后已关闭独立服务。

## 已完成：v2.4 P3 报价来源基准与表格比较（2026-10-29）

- 按供应商/报价单系列保存最新已确认来源快照、候选解释、归档引用与解释版本；基准写入有revision检查和幂等回执，重放旧确认不会覆盖新基准，原文件继续使用data归档。
- 结构读取支持XLSX/XLS/CSV/TSV。内容对齐区分新增、修改、删除、位置和展示变化；数字文本/百分比也参与正确比较。合并区域、多行模型、表头/单位/脚注、公式依赖与计算缓存变化进入对应上下文。
- 基准保留原始解释，复用时采用最新模型目录；来源范围移动通过内容匹配更新证据位置，不建立固定单元格入库配方。缺乏可定位来源时重新理解完整范围。
- 数据Subagent的比较、档案和旧库初始化回归106项通过；覆盖文本金额调序、公式引用行调序、命名引用/表格范围、缓存值、来源范围移动及基准重放。集成联调中的补充修复和最终验收另记。
- 只读实测当前input：港研院18工作表/423非空行/3636单元格约0.340秒，客易云3/56/504约0.113秒，乙太XLS1/39/229约0.008秒；自身比较均无语义变化。这是结构读取耗时，不是真实Qwen推理提速数据，未入库。

## 已完成：v2.4 P2 供应商专属会话（2026-10-29）

- supplier_id与默认OpenCode会话持久关联，首次打开创建供应商标题会话，后续选择直接续接。开启新一轮加载最新档案和近期结果摘要；历史待审核结果保留独立入口。
- 供应商选择、报价列表供应商筛选、来源系列及默认Prompt接入聊天区；未发送的文字和附件按供应商分别保存。切换、刷新和重启恢复同一等待任务、Session及原文件，运行中切换不把另一供应商的轮询结果写到当前聊天。
- 记录SDK语义请求次数、提交文字量、耗时和SDK可得token；单次最终回复token与整个任务多步token分别标记，历史中的其他请求不混入统计。
- 工作流阶段97项相关测试通过，补充的新一轮/多步统计等30项回归通过；浏览器真实API/SQLite/Workflow加脚本化SDK完成等待刷新、A/B往返、独立附件、同任务补答、运行中切换、确认及新一轮。0页面/API错误。证据：/tmp/tokensql-v24-sessions-evidence。SDK为测试脚本，不是真实Qwen性能结果。
- 主Agent全套P1检查曾发现4项旧库初始化失败回滚回归；已将档案DDL放入既有初始化事务并修复，相关84项回归通过，原断言保持。

## 已完成：v2.4 P1 供应商档案（2026-10-29）

- 供应商/报价单系列档案已落地：模型名称映射、规则、默认Prompt、确认来源、revision及可编辑页面；当前任务仅加载对应有效档案。全局偏好单独保存。
- 已确认补答和稳定语义修订在成功提交后更新档案；本期说明可单独使用，默认Prompt由用户勾选保存。金额、折扣和日期修订留在本期证据，不变成永久价格规则。确认前冻结档案更新内容，重启恢复幂等重放。
- 已有可归属供应商的已确认纠错迁入档案。有效解释指纹按业务含义计算，证据和更新时间变化不触发重复理解；同一语义规则的后续修正替换旧解释。
- 数据Subagent 44项档案/对账测试通过；工作流Subagent 89项相关回归通过。前端真实FastAPI/SQLite浏览器覆盖档案编辑、系列隔离、供应商隔离、刷新、版本冲突与全局偏好，page errors为0；v2.3两套浏览器回归通过。这些是阶段性验证，最终全套验收另记。
- 主Agent已实际查看桌面档案截图，核对正式业务表仍空，input三份原件保留。证据：/tmp/tokensql-v24-profile-evidence；正式数据基线摘要：/tmp/tokensql-v24-root-50v31_x8/before.json。未调用真实模型、未提交Git。

## 已完成：供应商档案与报价差异读取计划（2026-10-29）

- [x] 只读核对供应商对账、会话与纠错记忆实现：当前纠错按来源文件名追加，Prompt加载全部启用规则；现有会话持久化、同任务补答和供应商对账可复用。
- [x] 根据用户要求调整plan优先级：供应商档案统一承载业务记忆、供应商默认对话自动路由、新旧表格差异读取、变化区域理解及性能验收。工作重点为第二次及后续报价。
- [x] 按最新补充移除表格位置映射方案；计划采用需求、方向和改动项表述。原有七项待办全部保留并后移。
- [x] 主Agent统筹，gpt-6.1-sol ultra Subagent参与只读设计复核。本轮仅整理plan/progress文档，功能尚待实施；代码、数据库和服务保持原状，未运行测试或调用模型，未提交/推送Git。

## 已完成：新分支交付（2026-10-28）

- [x] 新实现与测试已提交ec709d1并推送origin/ui-supplier-comparison；main及v2.3继续指向开发前基线834b991，不把新实现混入基线。
- [x] git ls-remote --heads核验远程仅main、ui-supplier-comparison两个分支；本地同样仅这两个分支。未创建Release，旧提交仍可从主分支历史及已有标签追溯。
- [x] plan已移除本轮完成工程和交付步骤，仅保留用户新文件真实导入/金标比对、真实模型并发压测及可选后续。新界面操作见docs/supplier_updates.md。
- [x] 本次最终文档提交仅记录已完成推送，不改验收过的代码；服务仍运行于新分支，正常启停不清数据。

## 已完成：模型比价、供应商更新与等待布局工程（2026-10-28）

- [x] P1：供应商报价在Store/API/CLI/审核各入口强制关联供应商；可整批补齐，缺失进入等待并续用原文件/Session；官网和模型维护不要求供应商。稳定供应商ID、明确别名、来源范围/条目/渠道字段及API/Agent契约已同步。
- [x] P2：整模规则显示全部计费项，分项例外优先；输入/输出/缓存与忙闲、市场、上下文、TTL、模态及含税条件分别表达。加入tax_status及保守未知匹配。旧库按用户要求已清空，取消存量回填，不借测试重导旧文件。
- [x] P3：模型概览仅显示model_id，选择场景进入供应商排行；全量候选先精确计算和排序再分页，支持并列最低及同视图导出。主表仅供应商价和折扣，官网细则收进详情；金额已知但缺官网仍可比较，估算和待补条件不伪装严格最低。
- [x] P4：等待问题进入单一聊天滚动区、紧凑状态条和常驻输入；左右/上下可拖拽、键盘调整、恢复默认及窄屏切换。保留续答/刷新/取消语义；修复取消与晚到SDK回复竞态，取消任务不会被复活。
- [x] P5：供应商新版报价支持增量与明确范围完整替换，五组变更预览、歧义人工映射、原位增改删及跨任务文件/版本检查。报价ID保留，整批事务、回执及并发目录重审；普通更新不做每期整库备份。
- [x] 终审修复：更新报价禁止变更既有模型元数据，仍可自动创建缺失模型；关键价格依据冲突不生成因缺失删除。模型不能自行升级删除/回滚授权。审核编辑使用proposal_bundle源条目而非对账投影，重新预审不丢更新；新建报价不会继承上次来源范围或定价依据。
- [x] 主Agent独立最终全套：506 passed，1条既有Starlette/httpx弃用提示；git diff --check通过。单独含税8项、独立API4项和各数据/工作流新增用例已包含在全套计数，不重复累计。
- [x] 浏览器合成接口验收8组：652候选、跨页升降序/并列最低/导出、条件隔离、分项覆盖/冲突、估算、日期与新建表单重置、等待/拖拽/窄屏；page_errors=[]。实际查看1440/1280概览、排行与等待截图。
- [x] 浏览器实际API/SQLite/Workflow验收：上传合成XLSX，两次经审核表单修改金额及定价依据、PATCH重新预审、各一次确认；增量A改C保留，完整替换A改/B增/C删；A原ID保留revision递增，手工及其他供应商保留，29位Decimal精度不丢失，预审不写业务。SDK调用仅脚本化2次，不是真实Qwen；page_errors/api_errors均[]。

- [x] 独立终审三处最后修复无Blocker/Major；主Agent重新执行全套506项及两套浏览器验收，均通过。
- [x] 已恢复本项目服务：127.0.0.1:8765，启动PID278232；首页/health/Agent状态/quotes均HTTP200，独立OpenCode available=true、status=ready。模型/官网价/报价/供应商/导入回执仍0；启动按原配置刷新ECB参考汇率，新增1条pricing_context系统审计，不是报价回灌。未发起真实模型推理。

复验命令：`.venv/bin/python -B -m pytest -q --tb=line`；`.venv/bin/python -B tools/workspace_v23_browser_smoke.py --evidence-dir /tmp/tokensql-v23-frontend-evidence`；`.venv/bin/python -B tools/workspace_v23_api_browser_smoke.py --evidence-dir /tmp/tokensql-v23-frontend-evidence`。证据为上述远程目录的result.json、api-result.json和截图，不提交Git。

验收范围：全部测试写入只在隔离合成库；启动前正式模型/官网价/报价/供应商/审计/导入回执核验均0。未重新导入旧Excel、未调用真实模型解析业务文件、未做真实金标双库或coding并发压测；这些须等待新文件，列在plan，不以506测试冒充已完成。

## 已完成：v2.3基线推送、旧分支清理与空库重置（2026-10-28）

- [x] 当前项目保存为834b991，main及注释标签v2.3（v2.3 UI新设计）已推GitHub；未创建Release。其他三个分支的提交均包含于main后，删除本地/远程agent-workspace、pricing-review-workflow、qwen-semantic-import。
- [x] 创建新开发分支ui-supplier-comparison；新功能与基线分开保存。
- [x] 执行sh scripts/clean.sh --yes --no-restart：停止本项目运行实例、清空数据库与独立OpenCode会话/草案/纠错记忆。项目外备份：/home/user/luxiaofeng/TokenSQL-maintenance-backups/20261028T013907Z-reset-baaa06047a4d；不读回、不重新导入。input/progress/data中的原Excel保留。
- [x] 只读核验模型、官网价、报价、供应商、审计、导入回执均0；定价上下文及其初始历史各1条，为默认配置，不是业务价格。
- [x] 根据用户新要求取消存量语义整理，更新plan及项目规则。含税状态成为明确条件，unknown不视为已知等价；测试/运行验收使用隔离合成数据，等待用户新文件。

## 已完成：v2.3 UI新设计基线（2026-10-28）

- 保存当前已完成的条件定价、清理脚本及待实施UI方案为v2.3基线；新UI实现将放在独立分支，不把待实施计划冒充已实现。
- 保存前回归：442 passed；Git公开候选检查未包含真实Excel、数据库、运行记录或凭据。
- 按用户新要求，后续清空数据库并等待重新上传，取消存量价格语义整理；代码开发、验收及新分支推送由本轮继续执行。

## 已完成：比价界面与报价更新的只读诊断、计划整理（2026-10-26）

- [x] 主Agent只读检查现有代码、业务库和港研院原表，两个gpt-6.1-sol ultra Subagent分别审查界面布局与供应商更新机制。
- [x] 重写plan.md，只保留尚未实施的界面、数据规则、比价排序、等待补充布局和新版报价对账计划；本轮没有开始开发这些功能。
- [x] 原plan中已完成的条件定价、v2.2等待补充、倍率精度、单一模型标识/批删、可空导入、单位显示及服务脚本内容，与下方已有验收记录合并，不再重复保留整份旧计划。未完成双库验收等事项迁入plan；清理脚本的既有进度完整保留。

只读核查结果：

1. 当前业务库为146模型、871官网价、195供应商报价；报价供应商均非空（乙太35、Nebula Data160）。但当前schema、CLI、Agent及审核仍允许新报价供应商为空，因此“供应商必填”尚未实现。
2. 195条报价自身billing_item/billing_metric均为空；160条明确discount_scope=model，另35条unknown。主表直接读取报价自身字段，没有为整模规则显示“全部计费项”，所以全部出现“计费项未明确”，不能据此断定官网细则未入库。
3. Claude输入/输出/缓存的具体价格在官网表里，例claude-opus-4-6对应原表claude计费细则!E3:I3及各列E1:I1；整模折扣来自汇总表!E3表头、B8/E8。当前5项已入库且有metric，缓存写入TTL300/3600也已结构化。该部分是显示与供应商规则/官网记录分工的问题，不是五项都没识别。
4. 存量官网价仍有706/871条billing_metric为空（原始billing_item长标签仍在），只有165条已有规范metric；部分旧条目确实没有完成结构化。国内版-deepseek计费细则!E6:H7明确忙闲时、输入/输出/缓存命中；现输入/输出有market/time_band，而cache hit长标签尚未归一。Gemini的模态/上下文也还在长标签中。源表不是没有这些信息。
5. DeepSeek供应商135/205两条规则的国内/国际标记还在原conditions.版本，pricing_conditions为空；之后的比价/对账需要同时处理供应商侧限制，不能只修官网字段。这次只记录事实，没有回填或合并记录。
6. 当前模型名与ID重复、供应商/渠道/标签挤在一列，官网详细条件放进表格单元格；默认不启用价格计算，多项官网价又不能任选一个，进一步造成“有折扣但供应商原金额未提供”的阅读负担。排序目前只加载当前页，没有完整候选集最低价能力。
7. 用户截图中的等待补充布局与代码吻合：等待面板在聊天滚动区之前独占空间，固定最大高度并另有滚动；聊天和附件还各自滚动，主区固定vh且没有拖拽分隔条。这次是代码/截图结构诊断，不声称已修复或重新跑过浏览器。
8. 已有Bundle支持create/update/delete、revision与事务，但create始终INSERT；当前确认幂等是review:<job_id>，文件SHA仅用于完整性核验。同文件作为新任务上传仍可能重复新增，且没有“新版报价与旧供应商条目对账、明确范围内缺失删除”的完整机制。

代码定位（对应本次只读快照）：static/app.js模型双行及供应商展示约297–303、计费项317、列表加载401–410；static/index.html等待/聊天布局95–103；static/app.css等待面板512、聊天288、主区133–136；store.py新增678–688、Bundle事务746–832；workflow.py任务创建320–330、确认回执504–506。后续代码修改后应按符号查找，不将行号当稳定接口。

表格核查仅使用远程openpyxl只读；港研院原件SHA256仍为f60505588194d1327001a1a5fa36faca2be7eaeaa5f80bc2eae629fe8bea77aa。未运行迁移、导入、真实Qwen请求、测试写入、清理、服务启停、Git提交/推送；本轮仅修改plan.md和progress.md，既有未提交代码保持。

---

## 已完成：一键清理脚本（2026-10-21）

用户要求新增scripts下的脚本，供其以后清理当前数据库和专属OpenCode记录；本轮只开发和隔离测试，不实际清理正式库或重启正式服务，不自动提交/推送。

- [x] scripts/clean.sh与reset模块：默认一次RESET确认，可--yes/--dry-run/--no-restart；只支持项目默认workspace/专属agent。
- [x] 清理采用原资料移动至项目外0700私有备份，再创建空库/默认记忆；保留Excel/代码/.env/tests，不回灌备份。原来运行才重启自有服务；包含长期纠错记忆，旧progress任务不可续答。
- [x] 新增42项独立合成验收，主Agent最终全量370 passed（1条既有Starlette/httpx弃用warning）；Shell语法、差异/源文件空白检查通过。覆盖确认取消/EOF/中断和dry-run零写、锁inode/并发拒绝、路径/资源/symlink和错误实例、不同端口/孤儿worker、stop/move/init/restart故障数据保全、重复不覆盖。
- [x] 真实独立runtime+OpenCode SDK验收两次通过：合成1报价/模型/SDK会话/纠错记忆被清，旧字节进0700备份，6个Excel/代码/env文件哈希不变；running按原参数重启、off不启动、锁inode保留，重复备份唯一。未调用真实模型推理；18965/18966 QA已停，不冒充真实业务Excel验收。
- [x] README/workspace使用文档同步，独立终审无Blocker/Major。正式仅运行dry-run，前后15张表逻辑摘要、14个原文件/持久状态哈希及PID668956一致；未执行正式清理，未停/重启正式服务，未提交/推送。

最新QA证据：/tmp/tokensql-clean-real-391me9ig/result.json；正式只读预览证据：/tmp/tokensql-clean-real-nla8dr_8/production-dry-run-evidence.json。用户在服务器执行sh scripts/clean.sh后输入RESET；--yes免确认、--dry-run只预览、--no-restart保持停服。清理前请结束当前导入，且期间不要绕过service.sh直接启动runtime。所有清理资料只移动至项目外TokenSQL-maintenance-backups/<UTC时间>-reset-<随机ID>/，不删除/自动回灌；失败可据manifest人工恢复。此前其他任务的条件定价规则/契约/plan/进度及隔离开发均保留，本节不代替其验收。

## 已完成：条件定价、整模折扣与官网价关联（2026-10-21）

用户已授权实施前轮诊断建议：主界面折扣只显示折数；修复整模折扣与多项官网价关联及错误缺价提示，统一计费项、适用条件、折扣范围、导入契约和审核UI。主Agent Astra统筹；代码/审查Subagent明确使用gpt-6.1-sol ultra。

只读诊断基线：当前146模型、871官网价、195供应商报价，195报价billing_item均为空；港研院confirmed任务已录入871官网价（Claude12模型60价完整）及160整模折扣。旧匹配仅model_id+billing_item，故有官网价仍显示缺价。官网871均revision1、无覆盖审计；忙闲/国内国际/模态差异保留在原计费项文本中。倍率和折数并非两份存储，主表重复显示造成误解。

- [x] 更新计划与共同契约、规则；三路开发任务及边界已传递。
- [x] 无活动导入窗口完成项目外一致性备份：/home/user/luxiaofeng/TokenSQL-maintenance-backups/20261021-110834-pricing-conditions-baseline/。数据库quick_check=ok，146模型/871官网/195报价/1315审计、11个生命周期文件及专属会话/规则完整备份。开工备份后曾恢复旧服务，PID668956；最终部署状态见下文。
- [x] 后端稳定官网价格ID、结构化计费项/条件、明确折扣范围、候选匹配状态、无损幂等事务迁移。
- [x] 前端仅折数、多项官网价及条件展开、正确缺价/未知范围提示、审核与编辑控件、保真显示导出。
- [x] OpenCode契约及提示词、旧草案兼容、等待补充与人工确认链回归。
- [x] 真实数据私有副本迁移核对、真实表代表场景验收、浏览器及真实Qwen隔离验收、独立终审。
- [x] 同步使用文档，备份后迁移部署正式服务并逐项核对保留数据。

代码在远程隔离副本/home/user/luxiaofeng/TokenSQL-worktrees/pricing-conditions-20261021开发，避免开发中页面与正式后端不同步。主Agent在正式项目同步计划/进度/规则；最终仅将30个本轮代码/测试/工具文件补丁转移到正式项目，逐字节核对与验收副本一致后上线。不开新Release，不自动推送或移动已发布v2.2；不清库、不重导覆盖已有业务，不读handoff/旧清理备份，不操作共享Strata/Qwen/coding服务。当前授权备份用于本次隔离迁移验收，不回灌旧清理资料。

共同接口详见docs/pricing_conditions_contract.md。原始金额/折扣/币种/单位保留；派生价格仍由前端确定性计算且不回写。新单位只补充原值表达，不建立账号商品模块，不把token-hour/request/account-month伪换成token。

最终交付（已正式部署）：隔离开发副本完整回归400 passed；正式目录连同用户另有未提交的清理模块测试共442 passed，仅1条既有Starlette/httpx弃用warning。三路交叉审查无剩余Blocker/Major；主Agent真实库副本、正式只读浏览器均通过。未推送/提交Git或移动v2.2标签，不创建Release。用户新增scripts/clean.sh、reset.py及其测试和文档原样保留，不纳入本轮覆盖。

- 后端：stable official_price_id，metric与typed条件，同模多条件价、unknown/model/items，含全量渠道+标签隔离的effective_discount_rules。默认整模/具体例外不叠乘；缺条件及冲突明确停算。21单位保留旧12项，新复合量纲不伪换token。旧标签仅确定字面升级，其余原文保留。
- 审查修复：未知scope单一受限官网仍不能猜适用条件；raw只装未结构化价格限制，来源证据不污染条件；显式空条件与字段缺省区分；opaque标签标点不能被双库比较吞掉；只补schema默认字段不能伪造人工纠错；页面转换组件失败不能把0.70误标成0.70折。
- 主Agent迁移副本：/tmp/tokensql-conditions-copy-by_j2i7o/evidence.json。146/871/195原始金额/币种/单位/来源保持，原1315审计前缀、3条导入回执、context历史不改；增加1066条语义迁移审计，重复初始化完全幂等。DeepSeek12个忙闲/国内国际条目、Gemini4个文本/图片条目均保留，未确定旧长标签不猜测结构化。
- 真实库副本浏览器：/tmp/tokensql-root-real-catalog-5slgrpx2/result.json。Claude75七折关联5项，折后输入3.5、输出17.5、写缓存5m 4.375、写缓存1h 7、读缓存0.35（USD/百万token）；CSV和XLSX各导出5项且完整数据库快照不变，37号未知范围显示needs_scope而非缺价。
- 新合成真API浏览器：/tmp/tokensql-conditions-real-evidence/result.json，12报价覆盖完整/null/部分单位、组件加载失败、范围包含、分项覆盖/冲突、官网ID/revision及21单位。旧mock、nullable、simplify、waiting四类浏览器回归全通过，含跨页批删/409整批回滚及等待刷新/重启/取消。
- 真实本地Strata/Qwen + 真实Excel限定Claude样例：/tmp/tokensql-conditions-real-qwen-mzpd_02x/real-qwen-confirmed-evidence.json。新会话识别1模型、5官网价、1条0.70整模折扣，TTL300/3600正确；主Agent按原表单元格逐一核对后仅QA库确认，归档SHA相同。首轮来源证据误放raw，提示词修订后独立新会话复验正确；重启保留ready。8875/8876 QA已关闭，共享模型未改。本例不等于四份真实表完整双库金标验收。
- 正式部署备份：/home/user/luxiaofeng/TokenSQL-maintenance-backups/20261021-034605-conditions-deploy/（目录时间为UTC），0700、项目外，含一致性库/专属OpenCode/生命周期文件/被替换代码及本轮补丁。无活动任务后只重启TokenSQL，PID775046，8765/8766健康。
- post-deploy-evidence.json核对146模型/871官网/195报价，审计1315→2381；全部原始价格/单位/来源、旧审计/回执/context、11文件SHA、workflow.json/sessions.json/memory.json及6个历史任务status/revision一致。production-browser-evidence.json证实正式页面源7.0折/5项计算与needs_scope正确、JS错误0、写请求0、全库快照不变。

plan、共同契约、README/工作台说明、AGENTS/.cursor规则已同步。完整四表双库业务比对、真实复杂表所有条目逐项纠错及coding并发压测仍属后续，不将本轮代表例验收冒充全量完成。已有未结构化长标签继续展示原文；只有来源证据足够才补metric/条件。

## v2.2：等待补充、两位付款倍率与测试报价清理（2026-10-20）

本轮工程、验收、正式部署与公开范围检查已完成。用户要求的发布范围是当前pricing-review-workflow分支及附注v2.2标签，不改main/旧标签，不创建GitHub Release。此快照记录部署与验收；实际发布引用以git ls-remote origin refs/heads/pricing-review-workflow refs/tags/v2.2及其解引用核对，不把dry-run当实际推送。

- [x] 后端discount_rate全入口Decimal HALF_UP两位入库；前端确定性反算、显示与CSV/XLSX导出同口径，amount/FX精度不变。
- [x] 备份后永久删除用户授权的35条测试报价，剩0；35模型、供应商/渠道、8个生命周期文件、Session与记忆保留。删除审计35条，备份可人工恢复，不自动恢复。
- [x] 结构化needs_input.json与waiting_for_input、questions/question_summary；同job/Session/原文件respond，多轮attempt独立目录、新catalog、revision锁、取消和重启恢复。
- [x] 前端问题卡与直接补答、多轮、刷新恢复、历史接口超时/503回退、取消；恢复窗口锁定提交/附件/官网入口，等待时拒绝新附件，双击/旧revision不重复推理。
- [x] 等待不占推理、不写正式业务或长期记忆；真实SDK/格式错误仍failed；questions/proposal排他；默认只问关键歧义或明确偏好，不机械反问所有可空项。
- [x] 主Agent与独立终审完整回归328 passed（1条既有Starlette/httpx弃用warning），无剩余Major/Blocker；git diff --check、脚本语法、4类浏览器回归通过。
- [x] 真实Strata/Qwen先反问→服务重启→同会话补答→草案→浏览器人工确认全链通过。确认前业务行0；确认后仅隔离QA库增加1条合成报价/1模型，原折扣0.155按代码规范为0.16，文件归档SHA不变。未处理四份真实业务表，不冒充双库业务验收。
- [x] 2.2.0/uv.lock/共同规则与使用文档同步；远程构建wheel、按锁定hash装依赖的新环境导入和OpenAPI检查通过，打包代码逐字节匹配正式部署。
- [x] 公开范围独立复核80候选路径，无实际凭据/Excel/DB/会话/日志/备份；业务目录仅.gitkeep。仅代码/文档发布，不创建Release。

精度/清理备份：/home/user/luxiaofeng/TokenSQL-maintenance-backups/20261020-102944-discount2-clear35/。先规范30条旧倍率（revision+1、迁移审计30条、amount不变），再按已备份ID集合一次硬删35，post-deploy-clear-evidence.json留证。旧ready草案仅精度预览revision3→4，未自动确认；其base_catalog可能因已授权删除而失效，确认前应复查预览，不能自动重灌旧报价。

最终部署备份：/home/user/luxiaofeng/TokenSQL-maintenance-backups/20261020-105822-v2.2-waiting/，0700、项目外，含SQLite一致性备份、workspace/专属OpenCode/生命周期文件；manifest.json与post-deploy-evidence.json留证。正式PID1408755，8765版本2.2.0、respond接口存在，8766 ready。全表逻辑摘要、137条审计、35模型/0官网/0报价、8文件SHA、workflow/Session注册/记忆及历史均与本次备份一致。旧ready仍revision4，confirmed/failed历史不变；普通会话webfetch/websearch仍deny。只重启TokenSQL自有服务，共享模型服务未操作。

验收证据（全在远端，不入Git）：

- 主Agent最终等待浏览器：/tmp/tokensql-v22-root-waiting-latest/result.json；独立终审：/tmp/tokensql-v22-final-review-waiting/result.json。属于真实API/文件/浏览器+Mock SDK验收，不冒充真实模型。
- 真实Qwen：/tmp/tokensql-v22-wait-real-9novdc59/real-waiting-confirmed-evidence.json，含先问、重启、补答、确认及桌面/移动截图；QA8865/8866已关闭。
- wheel安装验证：/tmp/tokensql-v22-final-build-z4vv_gjh/evidence.json。先验证离线构建；隔离新venv按uv.lock导出hash下载缺失依赖，再离线安装wheel，不改正式依赖环境。

仍待后续：四份真实Excel的独立Codex金标/OpenCode双库业务验收、与coding同时段压测等历史范围未由本轮合成验收替代。全程仅ssh uni远端工作，不读handoff/旧清理备份业务内容，不写本机项目文件。下文为历史过程，以本节最新约定为准。

## 已完成：付款倍率两位小数入库（2026-10-20）

用户明确更正：不是只显示舍入，付款倍率必须保留两位并入库；八折0.80，1.5折0.15，前端反算同口径。此节替代上轮“raw折扣保留任意精度”的约定，其他业务规则不变。

- [x] 后端全入口Decimal HALF_UP两位规范，null/0/负数/大数边界；amount/FX不受影响。
- [x] 旧记录备份后事务规范、revision与old/new审计，重复启动幂等，失败整批回滚。
- [x] 前端BigInt两位显示/反算/导出，辅助折数一位；不把派生反算写成原始值。
- [x] OpenCode提示词与旧ready草案两位策略兼容，preview/revision刷新，不自动确认，不将政策规范当人工纠错记忆。
- [x] 完整回归、浏览器与独立终审，备份和远程部署，记录真实变化与原金额不变。

开工只读基线：正式35报价、折扣均非null；30条需数值规范（原存15/16位小数）、5条已两位，最大付款比例差0.0044444444444445，正值舍入为0的数量0。ready草案revision3，另1 confirmed/2 failed，均保留；不得删除报价/文件或自动确认。三个Subagent继承主Agentgpt-6.1-sol ultra，所有工作只远端ssh uni，不读handoff/旧备份，不动共享服务，不push。

## 上轮完成：单一模型标识与简化人工操作（2026-10-20）

- [x] 删除physical_key业务字段/数据库列/界面输入，以唯一model_id及可选名称、别名维护身份；兼容旧草案与带数据迁移。
- [x] 手工报价可直接输入新模型ID，确认报价时同事务自动建缺失模型，不要求先审模型实体。
- [x] 折扣按折数显示一位（0.15→1.5折、0.1555→1.6折），前端反算同样HALF_UP；输入折数经确定性代码除10保存付款比例，未改折扣时保留旧raw高精度，不舍入存储值或价格计算。
- [x] 新增记录币种默认CNY，明确外币保留、显式null仍未知；计费尺度不设猜测默认，编辑旧记录不覆盖原币。
- [x] 新增所选报价批量永久删除，一次确认即事务执行；单条/草案删除统一真正删行，保留审计和模型/官网/文件，测试不删除正式记录。
- [x] 主Agent备份部署、完整回归/浏览器验收；另只评估waiting_for_input最小范围和必要性，本轮未实现。

约束：三个新Subagent统一继承主Agentgpt-6.1-sol ultra；所有项目工作仅远端ssh uni。开工正式状态：35模型/35报价、官网0、审计72、已确认导入1；有1个ready草案，另有confirmed/failed历史，均保留，不自动审核/重跑。现有dirty工作树保留，不commit/push/建Release，不读取handoff或旧备份，不操作共享服务。

本轮交付证据：

- 主Agent最终完整回归223 passed（1条既有Starlette/httpx2弃用warning），独立终审同样223通过；git diff --check与服务脚本语法通过，终审无剩余Blocker/Major。带数据迁移保留外键/索引/触发器/序列、审计/幂等回执；模型重建/别名重建/删除审计注入故障、重叠双连接批删均事务回滚验证。
- 审查抓到并修复旧physical/model_name自动别名残留：迁移按明确payload.aliases重建索引，不让删字段后仍有隐式身份限制。模型名称可重名；ID和明确alias双向冲突仍拦截。正式原32个自动别名索引→0（用户当时没有明确aliases）；模型/报价不因此合并、拆分或改ID。
- 冻结Chromium mock完整交互主Agent/独立审查均PASS；旧显示断言0.6/0.48更新为6.0/4.8折，不删除旧功能回归。真实API可空/显示导出复验4条、审计8→8，真XLSX折数列与原始值均正确；桌面移动截图主Agent查看，无页面级横溢。
- 新 tools/workspace_simplify_real_smoke.py 三次独立PASS（UI8893、审查8894、主Agent8895；均自动退出）：新ID直接新增自动建模/CNY默认、外币及显式null不覆盖、raw0.1555仅改备注仍0.1555、界面1.6折、跨页109条选2条永久删、取消零请求、stale409整批不删、模型/官网不变。主Agent证据/tmp/tokensql-simplify-root-final-evidence/result.json，独立终审/tmp/tokensql-simplify-review-evidence/result.json。
- 主Agent另做105条真实API+Chromium隔离验收：跨页选2，取消0请求，一次确认→103；SQL确认2行物理消失且恰2条deleted审计，模型105/官网1和input/progress/data文件哈希保留；第二批人为改revision后409零删除。证据/tmp/tokensql-bulk-root-sssjtwhk/result.json。首跑脚本分页等待误匹配总页数，精确等待2/2后通过，非产品分页选择故障。
- 真实Strata/Qwen合成Excel：SYNTHETIC-CNY-ONE原折扣0.1555/缺省CNY且无单位，USD-TWO原价1.25/usd_mtoken，HKD-THREE原价3.50/hkd_second。3条草案无需先建模型/补厂商；确认前正式QA模型/报价0，QA显式调用确认接口后同事务自动建3模型，原值/原币不变、无physical列、原件归档SHA一致。8865/8866 QA已停止；证据/tmp/tokensql-simplified-qwen-okl6yp07/real-qwen-confirmed-evidence.json及浏览器截图。未处理四份真实业务表或向生产写合成数据。
- 正式无running/queued/preparing/cancelling/confirming/committed任务后停自有实例；备份于/home/user/luxiaofeng/TokenSQL-maintenance-backups/20261020-101221-model-id-bulk-delete/，含正式SQLite一致性备份、专属OpenCode状态/规则/记忆和生命周期原文件，quick_check=ok。在该真实数据库的私有副本先验证迁移，再部署；manifest.json/copy-migration-evidence.json/post-deploy-evidence.json留证。
- 正式PID1281183，8765健康、8766可用、全局webfetch/websearch仍deny。35模型/35报价、ID/revision/raw金额折扣币种/原审计72前缀/导入回执保持；8个生命周期文件哈希、Session索引、记忆和历史正文保持。物理列与当前API字段删除。ready草案68318a64284a43bcb664e0f5be54ed73仅预览重算、revision2→3，无兼容警告，不自动确认。生产删除报价0，未运行删除API；共享服务及旧备份/交接资料未操作。
- 规则/plan/contracts/README/workspace已同步。本轮仍在pricing-review-workflow，不commit/push/改main/tag/创建Release；四份真实业务Excel独立Codex金标/OpenCode双库验收仍为历史后续项，不用合成结果代替。

等待补充评估：中等流程改造，无需改业务数据库，但需agent结构化questions、持久waiting_for_input、同job/Session续答API与revision锁、原文件留progress、刷新/取消/重启恢复和前端/集成测试。现有同job重跑会覆写只读上传，须采用每轮attempt目录或安全复用原件并隔离旧proposal，避免错确认。强制先反问再草案时有必要；缺项null+人工审核可继续用现状，不必阻断本轮。建议只对模型身份无法确定/证据冲突等关键歧义等待，不为每个可空值机械反问。此状态不能保证Qwen提问质量，仍需真实引擎验收。

升级限制：本次升级前已确认没有旧confirming/committed。若旧schema业务已提交但尚未持久化workflow回执，清理字段会改变规范hash，不能直接跨该崩溃窗口升级重放；应先完成或人工恢复旧任务。既有postcommit重试回归保留并通过，本轮未改旧importhash或增加回执恢复接口。

## 上轮完成：可空原始数据、前端折扣与官网快捷补价（2026-10-16）

- [x] 修复失败job立即展示真实assistant回复（无需切换会话），覆盖history/模型/验证失败异常。
- [x] 仅model_id业务必填；其他标量可空/部分单位保留/纯折扣独立保存；带数据SQLite兼容迁移，不猜vendor或Tokens倍率。
- [x] 官网折扣↔折后价改为前端显示及导出，不补写/覆盖raw amount/discount_rate；缺价提示+新官方检索会话入口。
- [x] OpenCode独立官网检索可读官网索引并联网，结果仍审核；供应商原始报价不能被检索任务顺带改写。
- [x] 三个Subagent统一继承主Agentgpt-6.1-sol ultra，主Agent更新规则/契约并独立验收。仅远程工作，保留现有资料；部署前数据库备份，无活动任务时只重启TokenSQL自有服务。

本轮验收与交付：

- 完整回归186 passed（1条既有Starlette/httpx2弃用warning），主Agent与独立终审分别重跑；git diff --check、启动脚本语法通过，终审无新增Blocker/Major。带数据旧结构迁移保留ID/外键/索引/触发器/审计/幂等记录及自增序列；注入迁移失败后逻辑快照相同，可重试。
- Chromium API桩及独立真实API验收均通过。四条合成报价覆盖纯折扣、CNY/Tokens部分单位、仅ID及金额+折扣并存；前端显示折后价16/反算倍率0.6、CSV/真XLSX导出、桌面/移动布局通过。报价/目录/context/记忆快照不变，审计8→8。证据：/tmp/tokensql-nullable-real-evidence/，主Agent复看截图。
- 真实Strata/Qwen独立QA：官网不存在指定旧模型时不替换为新模型，价格留空并说明；指定存在模型从官网提取原币原价9.0并仅在QA确认；启用独立子进程搜索工具注册后，真实websearch与webfetch均completed，另一次发现搜索旧缓存与实时官网不同，采用实时官网并披露差异。普通会话仍webfetch/websearch双deny，全局双deny；仅官网查询会话显式allow，不更改共享provider配置。
- 真实合成Excel三条：mimo-v2.5-pro折扣0.8 + CNY/Tokens且无千/百万尺度；hy4-preview折扣0；Banana Pro仅ID。Qwen无需追问厂商/单位即可生成3条草案；QA确认后原金额/vendor均null、原折扣0.8/0/null保真，归档文件SHA256与上传一致，input/progress无残留文件。验收脚本首次误以为归档文件直属data/导致断言，改为核对实际data/<job_id>/文件路径后通过，非产品故障。全部引擎证据仅远端/tmp/tokensql-nullable-ZsPMav/，8855/8856验收服务已停止。
- 正式部署前无活动任务。项目外私有备份：/home/user/luxiaofeng/TokenSQL-maintenance-backups/20261016-164557-nullable-display-pricing/，含正式SQLite一致性备份、专属OpenCode会话/规则/记忆、workspace及input/progress/data；quick_check=ok，manifest.json与post-deploy-evidence.json留证。旧清理备份/交接内容未读取。
- 仅TokenSQL正式自有服务重启至PID1080832，8765健康、8766就绪；生产OpenAPI三个Create schema仅model_id必填。正式模型/官网/报价仍0；8个生命周期文件（含.gitkeep）哈希不变、两条历史failed任务/Session注册/记忆保持；既有审计前缀保留。新沙箱隐藏维护备份目录，不给OpenCode读取备份。共享Strata/Qwen/coding服务未操作。
- plan/契约/README/使用说明及AGENTS/.cursor规则已同步；新后续Subagent继承主Agentgpt-6.1-sol ultra，旧5.6记录仅为历史。业务四表Codex独立金标与OpenCode双库验收仍待后续，不以合成验收替代；本轮不自动处理/重跑用户旧失败任务、不推送Git或创建Release。已保存旧失败记录当时未捕获的回复不能凭空补回，刷新页面后可新建/重试处理。


## 已完成：单脚本启停封装（2026-10-16）

- [x] 新增项目根service.sh，统一start/stop/restart/status/logs及无参数中文菜单；后台运行、健康检查、进程身份与并发锁保护。兼容sh/bash/直接可执行及任意当前目录。
- [x] 无中断接管正式PID154276；start/start/status真实通过，PID身份不变，health/catalog/quotes/imports/memory五API摘要与开工前一致。真实foreign LISTEN冲突拒绝且监听者保留；正常停止只TERM，不清理业务数据。
- [x] gpt-5.6-sol high编码/测试与独立审核，主Agent修复并实测。独立终审无阻断项；完整回归133 passed（既有弃用warning 1条），sh/bash语法和diff检查通过。README/workspace/plan同步。

验收：/tmp/tokensql-service-final-z9hy4q、8795隔离实例完成start/status/重复start/stop/重复stop/start/logs/restart/stop；1条合成报价、三个生命周期目录Excel及agent.md/memory.json/workflow.json共6文件，重启前后五API及文件SHA256一致。owned/目录8797/8798真实独立OpenCode启动/健康/TERM子进程清理通过，两端口均关闭。审查Agent另以28935/28936验证跨SSH后台存活（PPID1/SID=PID、stdio重定向、未继承锁FD）。

修复实测发现的exec前后argv记录竞态、瞬时/proc身份稳定、CRLF参数污染、自然退出记录清理与TIME_WAIT端口误报（service和runtime均修复）。非阻断硬化项：可信单用户场景下仍使用PID+starttime/argv/token复核，不提供pidfd级别对抗PID复用竞态保证；活PID身份不一致会安全拒绝，需人工核查运行记录。本轮未调用模型处理业务文件，四份真实Excel双库验收仍不受此服务验收替代。

## 已完成：12项单位字典与前端临时换算（2026-10-16）

- [x] 更新 plan/AGENTS/.cursor/contracts：3币种×4基准，金额+pricing_unit_id；正式折扣以官网单位为准。按用户指定新建3个 gpt-5.6-sol high 编码/审查任务。
- [x] 12项字典/严格兼容边界/官网单位折扣及单测；同币种不依赖FX，缺跨量纲依据不伪算，正式issues与display_issues分离。
- [x] 独立统一币种/单位按钮，可组合/分别恢复；临时model+billing倍率与问号建议；BigInt有理数计算和保真CSV/真XLSX导出，附精确分子/分母。
- [x] 官方视频生成公式验证；建议按宽×高×FPS/1024由代码算，明确Seedance范围、视频输入及最低用量限制，不套视频理解算法。
- [x] 独立回归/Chromium桌面移动/服务部署验收。真实隔离API三行synthetic样例，XLSX复读核价；显示/导出前后报价/版本/context/memory不变，审计10→10。模型全条目转换导出修复并跨500分页验证504行。
- [x] refresh-fx已挂载；ECB真实XML解析、离线失效/无变化/并发上下文冲突测试5项通过。8765健康、字典12项、8766 Qwen ready；正式业务模型/官网/供应商报价0，参考汇率context rev2，不改原价。

旧 CNY-only 设计已作废。显示临时参数不得写正式库/全局参数/长期记忆。真实四表业务双库仍待完成；真实模型已在隔离QA通过两次空草案、同Session回忆、服务重启后12条历史恢复，以及合成Excel两模型四报价预审→取消（业务行0→0）。不将这些证据冒充四份真实业务文件验收。

### 当时的验收记录（2026-10-16）

- 主Agent最终完整回归：127 passed，1条既有Starlette/httpx2弃用warning；bash启动脚本语法与git diff --check通过。
- 已移除旧priceView下拉与服务端normalized价格展示；默认仅原价，双轴启用后只追加一条前端换算价。真实API三行复验：HKD单轴不再额外出现CNY/百万token；重做组合换算及导出，审计仍10→10。
- 真实Chromium（API桩）覆盖双轴控制、缺参数/过期、临时factor作用域、极小科学计数/大整数/循环小数、504行单模型分页导出；真实隔离API三行synthetic并下载真XLSX核价。桌面/移动截图由主Agent再次查看，文件仅在远端 /tmp/tokensql-units-ui-evidence/。
- 真实Strata/Qwen：空草案同Session正确回忆，工作台/OpenCode重启后API恢复12条历史。合成Excel独立两次：预审2模型4报价→取消，报价0→0；隔离测试模式确认→4报价、validation_errors=[]。证据 /tmp/tokensql-live-9jEmWJ/smoke-units-{preview,confirmed}.json。不是四份业务基准验收。
- 双库comparator适配canonical-only单位ID；保留版本关键标点，gpt.4/gpt4不再盲目合并，需可信alias或显式map。37项comparator合成回归通过。
- gpt-5.6-sol high独立终审：0阻断项；复跑截获式Chromium（含504行分页）与6项HTTP smoke mock均通过。当时正常取消/确认路径已通过；异常终态计数复查的增强事项已迁至plan.md。
- 服务8765/8766就绪，生产模型/官网/供应商报价均0；input四原件仍在，已重读SHA256。真实业务Excel从未被测试脚本导入正式库。旧test空占位已合并为tests/data，私有目录仅.gitkeep可进Git。
- 真业务四表独立Codex金标+OpenCode库比对、与coding同时段压力测试仍未完成。代码留在pricing-review-workflow；本轮不push、不创建Release、不更改main或旧tag。

## 已完成基础工程：定价实体、审核导入与独立记忆（2026-10-16）

本轮按 plan.md 与 docs/contracts.md 实施。用户已确认：同一模型 ID 下允许多个官网计费项；Excel 明确标注官网价的允许 OpenCode 提议并经人工审核入库，不必 Codex 独占。未明示官网价不猜测；支持用户主动发起独立OpenCode官网联网补价，经人工审核后录入，或由Codex维护。

- [x] 核对此前 main 与 v2.1_cleaned 的远端引用均为 d636b03，上一计划全部完成。
- [x] 建 pricing-review-workflow 分支，保留用户 input/ 四份原始 Excel；新计划/共同契约已下发三个 Subagent。
- [x] 规范化模型/计费项/供应商标签、原币代码定价、CRUD/原价导出基础；最新单位字典/前端显示扩展见上节。
- [x] 上传→处理→人工审核→确认归档/取消回输入，事务与文件补偿（合成/Mock验证，待真实文件）。
- [x] 外部OpenCode目录、bubblewrap防误写、Session真实回忆/跨服务重启历史恢复、agent.md分层记忆；网络共享的限制如实写入docs。
- [x] 前端审核/撤销/删除/文件交互/会话和纠错记忆（Chromium/API桩；真实API显示导出与真实引擎合成导入分别验收，尚未做四份业务表的全链路人工验收）。
- [x] 读取四份input原件的工作簿结构并核验SHA256；建立官网链接索引（原件不改写）。
- [x] scripts/setup.sh/start.sh、.env.example、SSH命令、官网链接索引、倍率依据、独立交叉审查及分层证据；真实四表双库仍未宣称完成。

只重启本工作台；旧 PID1718828 已确认身份并停止。正式旧平面库仍为 0 行；不清除用户新文件/备份、不动共享服务，不在本机写项目文件。远端缺少 artifact-tool，本轮只读原 Excel，使用 openpyxl/xlrd 读取，全部价格计算用确定性代码，不改写工作簿。

## 已完成：v2.1_cleaned 单一工作台与空白数据起点

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

## v2.1_cleaned 验收证据（历史）

- 最小运行依赖 7 项；完整开发/浏览器 lock 共 35 个包。全新 Python 3.10 venv 从锁文件安装成功，无旧栈依赖。
- 完整相关 suite：25 passed，1 条既有 TestClient 弃用 warning；新增 4 项清洁初始化/退役 CLI/版本/入口回归。新测试已修 Python 3.10 TOML fallback 和遗漏旧入口保护。
- 构建并在新环境安装 wheel，从项目外验证包导入、2.1.0、当前解释器、两个 console scripts、空库 API、HTML/CSS/JS。wheel 只含包入口与 workspace，不含旧代码。
- 独立执行浏览器 smoke：真实点击发送、工具摘要、失败后的实际变更刷新、宽窄布局与零页面错误通过；API 为浏览器桩，不写正式库。JS 语法和 git diff 检查通过。
- 正式库 workspace_quote/workspace_audit/workspace_request 均为 0；inbox 空，OpenCode 会话列表为空，test/ 仅 .gitkeep，项目内无 Excel。新服务 8765 health 正常、独立 8766 Agent ready；共享服务未调整。
- Subagent 终审无 Blocker/Major；最小安装说明、旧入口保护、无效测试 patch 等 Minor 已收尾。本轮未调用真实模型、未导入新业务样例。

## v2.1_cleaned 执行边界（历史）

全部项目操作在远端；未读取 handoff/私有交接内容，未回灌备份业务资料，不更改共享 Strata/Qwen/coding 服务。清理仅针对 TokenSQL，旧代码保留在 v2.0，私有资料可由项目外备份恢复，不自动恢复或回灌。

旧清理阶段曾等待新文件；当前用户已提供四份input原件，不再列为缺文件。正式库未自动导入，测试证据区分临时样例与真实文件，不创建 GitHub Release。
