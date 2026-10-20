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


## 当前补充：单脚本启停封装（2026-10-16）

- [x] 新增项目根service.sh，统一start/stop/restart/status/logs及无参数中文菜单；后台运行、健康检查、进程身份与并发锁保护。兼容sh/bash/直接可执行及任意当前目录。
- [x] 无中断接管正式PID154276；start/start/status真实通过，PID身份不变，health/catalog/quotes/imports/memory五API摘要与开工前一致。真实foreign LISTEN冲突拒绝且监听者保留；正常停止只TERM，不清理业务数据。
- [x] gpt-5.6-sol high编码/测试与独立审核，主Agent修复并实测。独立终审无阻断项；完整回归133 passed（既有弃用warning 1条），sh/bash语法和diff检查通过。README/workspace/plan同步。

验收：/tmp/tokensql-service-final-z9hy4q、8795隔离实例完成start/status/重复start/stop/重复stop/start/logs/restart/stop；1条合成报价、三个生命周期目录Excel及agent.md/memory.json/workflow.json共6文件，重启前后五API及文件SHA256一致。owned/目录8797/8798真实独立OpenCode启动/健康/TERM子进程清理通过，两端口均关闭。审查Agent另以28935/28936验证跨SSH后台存活（PPID1/SID=PID、stdio重定向、未继承锁FD）。

修复实测发现的exec前后argv记录竞态、瞬时/proc身份稳定、CRLF参数污染、自然退出记录清理与TIME_WAIT端口误报（service和runtime均修复）。非阻断硬化项：可信单用户场景下仍使用PID+starttime/argv/token复核，不提供pidfd级别对抗PID复用竞态保证；活PID身份不一致会安全拒绝，需人工核查运行记录。本轮未调用模型处理业务文件，四份真实Excel双库验收仍不受此服务验收替代。

## 当前最新需求：12项单位字典与前端临时换算（2026-10-16）

- [x] 更新 plan/AGENTS/.cursor/contracts：3币种×4基准，金额+pricing_unit_id；正式折扣以官网单位为准。按用户指定新建3个 gpt-5.6-sol high 编码/审查任务。
- [x] 12项字典/严格兼容边界/官网单位折扣及单测；同币种不依赖FX，缺跨量纲依据不伪算，正式issues与display_issues分离。
- [x] 独立统一币种/单位按钮，可组合/分别恢复；临时model+billing倍率与问号建议；BigInt有理数计算和保真CSV/真XLSX导出，附精确分子/分母。
- [x] 官方视频生成公式验证；建议按宽×高×FPS/1024由代码算，明确Seedance范围、视频输入及最低用量限制，不套视频理解算法。
- [x] 独立回归/Chromium桌面移动/服务部署验收。真实隔离API三行synthetic样例，XLSX复读核价；显示/导出前后报价/版本/context/memory不变，审计10→10。模型全条目转换导出修复并跨500分页验证504行。
- [x] refresh-fx已挂载；ECB真实XML解析、离线失效/无变化/并发上下文冲突测试5项通过。8765健康、字典12项、8766 Qwen ready；正式业务模型/官网/供应商报价0，参考汇率context rev2，不改原价。

旧 CNY-only 设计已作废。显示临时参数不得写正式库/全局参数/长期记忆。真实四表业务双库仍待完成；真实模型已在隔离QA通过两次空草案、同Session回忆、服务重启后12条历史恢复，以及合成Excel两模型四报价预审→取消（业务行0→0）。不将这些证据冒充四份真实业务文件验收。

## 本轮验收记录

- 主Agent最终完整回归：127 passed，1条既有Starlette/httpx2弃用warning；bash启动脚本语法与git diff --check通过。
- 已移除旧priceView下拉与服务端normalized价格展示；默认仅原价，双轴启用后只追加一条前端换算价。真实API三行复验：HKD单轴不再额外出现CNY/百万token；重做组合换算及导出，审计仍10→10。
- 真实Chromium（API桩）覆盖双轴控制、缺参数/过期、临时factor作用域、极小科学计数/大整数/循环小数、504行单模型分页导出；真实隔离API三行synthetic并下载真XLSX核价。桌面/移动截图由主Agent再次查看，文件仅在远端 /tmp/tokensql-units-ui-evidence/。
- 真实Strata/Qwen：空草案同Session正确回忆，工作台/OpenCode重启后API恢复12条历史。合成Excel独立两次：预审2模型4报价→取消，报价0→0；隔离测试模式确认→4报价、validation_errors=[]。证据 /tmp/tokensql-live-9jEmWJ/smoke-units-{preview,confirmed}.json。不是四份业务基准验收。
- 双库comparator适配canonical-only单位ID；保留版本关键标点，gpt.4/gpt4不再盲目合并，需可信alias或显式map。37项comparator合成回归通过。
- gpt-5.6-sol high独立终审：0阻断项；复跑截获式Chromium（含504行分页）与6项HTTP smoke mock均通过。可选后续增强：smoke遇异常自行终态confirmed/failed时额外复查DB总数，以强化事故证据，不影响正常取消/确认路径。
- 服务8765/8766就绪，生产模型/官网/供应商报价均0；input四原件仍在，已重读SHA256。真实业务Excel从未被测试脚本导入正式库。旧test空占位已合并为tests/data，私有目录仅.gitkeep可进Git。
- 真业务四表独立Codex金标+OpenCode库比对、与coding同时段压力测试仍未完成。代码留在pricing-review-workflow；本轮不push、不创建Release、不更改main或旧tag。

# TokenSQL 开发进度

最后更新：2026-10-16（Asia/Hong_Kong）。

## 当前：定价实体、审核导入、双库与独立记忆（进行中）

本轮按 plan.md 与 docs/contracts.md 实施。用户已确认：同一模型 ID 下允许多个官网计费项；Excel 明确标注官网价的允许 OpenCode 提议并经人工审核入库，不必 Codex 独占。未明示官网价不猜测；支持用户主动发起独立OpenCode官网联网补价，经人工审核后录入，或由Codex维护。

- [x] 核对此前 main 与 v2.1_cleaned 的远端引用均为 d636b03，上一计划全部完成。
- [x] 建 pricing-review-workflow 分支，保留用户 input/ 四份原始 Excel；新计划/共同契约已下发三个 Subagent。
- [x] 规范化模型/计费项/供应商标签、原币代码定价、CRUD/原价导出基础；最新单位字典/前端显示扩展见上节。
- [x] 上传→处理→人工审核→确认归档/取消回输入，事务与文件补偿（合成/Mock验证，待真实文件）。
- [x] 外部OpenCode目录、bubblewrap防误写、Session真实回忆/跨服务重启历史恢复、agent.md分层记忆；网络共享的限制如实写入docs。
- [x] 前端审核/撤销/删除/文件交互/会话和纠错记忆（Chromium/API桩；真实API显示导出与真实引擎合成导入分别验收，尚未做四份业务表的全链路人工验收）。
- [x] 读取四份input原件的工作簿结构并核验SHA256；建立官网链接索引（原件不改写）。
- [ ] 四份业务基准的Codex独立测试库与Excel明示官网价金标。
- [ ] OpenCode 真实文件导入真实库与双库差异/实体拆分检查。
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

## v2.1_cleaned 约束与下一步（历史）

全部项目操作在远端；未读取 handoff/私有交接内容，未回灌备份业务资料，不更改共享 Strata/Qwen/coding 服务。清理仅针对 TokenSQL，旧代码保留在 v2.0，私有资料可由项目外备份恢复，不自动恢复或回灌。

旧清理阶段曾等待新文件；当前用户已提供四份input原件，不再列为缺文件。正式库未自动导入，测试证据区分临时样例与真实文件，不创建 GitHub Release。
