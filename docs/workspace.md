# 工作台使用与部署

## 服务器启动

只在服务器项目目录运行：

```bash
cd /home/user/luxiaofeng/TokenSQL
scripts/setup.sh
sh service.sh start
sh service.sh status
sh service.sh logs
sh service.sh stop
sh service.sh restart
```

也可只执行 `sh service.sh`，按中文菜单选择开启、关闭、重启、状态或日志。脚本自动切换到项目目录，可从任意目录执行 `sh /home/user/luxiaofeng/TokenSQL/service.sh start`。

默认复用本服务器已验证的OpenCode可执行文件和provider配置路径；环境变量或远端私有.env中的 TOKENSQL_OPENCODE_BIN / TOKENSQL_OPENCODE_CONFIG 可覆盖默认值。复制.env.example时自行填写真实路径，凭据不进Git。需要uv、Python>=3.10、Linux bubblewrap；不自动修改系统或共享推理服务。

统一脚本后台启动并等待健康检查；重复start不会重复启动，也不会重新配置已运行实例。修改运行参数应执行restart。若提示另有启停操作正在进行，请稍后重试。默认仅监听127.0.0.1:8765（网页）、8766（独立OpenCode）。PID身份记录/锁和日志位于var/workspace/service.json、service.lock、service.log；支持识别本项目之前手动启动的同配置runtime。端口被其他进程占用则报错，不杀占用进程。stop严格复核进程身份后发送SIGTERM，超时不强杀；runtime负责关闭自己启动的OpenCode，不关闭共享Qwen/Strata/coding服务。退出SSH或关闭转发窗口不停止后台服务。

启停操作不删除quotes.sqlite3及其SQLite边车文件、input/progress/data、草案、会话或长期记忆。已提交数据持久保留；正在生成的任务可能中断并需重试，建议等待当前导入结束再stop/restart。业务schema升级前还须完成或人工恢复所有confirming/committed任务，不能跨旧schema已提交但未保存回执的崩溃窗口升级重放。ready草案升级可仅刷新预览并提高revision，需重新打开人工审核。浏览器临时显示换算参数不持久化。

原scripts/start.sh仍保留前台开发启动：需自行传入--opencode-bin/--provider-config或设置私有.env，按Ctrl+C停止。统一脚本的runtime参数放在`--`后，例如`sh service.sh start -- --no-auto-refresh-fx`；隔离实例必须在stop/status等命令传入同样的端口/目录参数，不与默认实例混用。

参考汇率按需在UI刷新；--auto-refresh-fx 开启每小时拉取最新可用ECB日参考价，--no-auto-refresh-fx关闭。设置 .env 中 TOKENSQL_FX_AUTO_REFRESH=true 同效。来源每日而非秒级市场行情，网络失败保留旧数据，超期明确提示，不改原始报价。

## 持久数据位置

以下均在服务器，正常start/stop/restart不删除：

- 正式数据库：`/home/user/luxiaofeng/TokenSQL/var/workspace/quotes.sqlite3`。
- Excel生命周期：项目内`input/`（待处理）、`progress/`（处理/待审核）、`data/`（已确认原件归档）。
- Codex测试库与私有比对资料：项目内`tests/data/`。
- 独立OpenCode会话、草案与长期记忆：`/home/user/luxiaofeng/TokenSQL-opencode/`。

`data/`只是Excel归档，不是数据库。不要手动删除SQLite的`-wal`/`-shm`文件；备份应使用SQLite备份功能，或正常停服后复制数据库及相关资料。

## 本机连接（用户自行运行）

```powershell
ssh -N -L 8765:127.0.0.1:8765 uni
```

保持该窗口打开，在本机浏览器访问 http://127.0.0.1:8765 。无需把8766暴露给浏览器。若出现 channel open failed: Connection refused，先在服务器检查网页服务/health是否启动；这表示转发目标没有监听，不是浏览器问题。若本机8765占用，可把命令左侧改为18765，浏览器相应访问18765。

## 数据与显示单位

model_id是唯一必填业务标识，model_name为可选显示名；物理标识已移除，明确aliases可辅助归一化。供应商、计费项、金额、原折扣和单位可空，系统ID/revision/审计约束仍保留。手动新增报价可输入新ID或选择已有ID，确认时同事务自动创建缺失模型，不必先审核目录。纯折扣不需官网价或单位；未知厂商不猜，Tokens无尺度不强选千/百万。

新增币种默认人民币CNY，不默认计费尺度。明确USD/HKD保持原币，显式空币种可保存未知；编辑已有外币/空币种不自动覆盖。选择完整12项单位时同步其对应币种，旧数据不因默认值批量改写。

计价单位已知时固定USD/HKD/CNY × /ktoken、/mtoken、/s、/张，共12项，完整ID与拆分字段冲突仍拒绝。一个物理模型保留唯一ID，不按输入/输出/缓存、地区或分辨率拆模型。

amount和discount_rate独立保存，可均空、仅折扣或同时存在，不通过官网价在后端补算，也不因二者不一致而拒绝保存。付款倍率是实付比例：0.80=八折，0.15=1.5折，1.20=溢价20%，不是减免比例。

最新规则：付款倍率通过Decimal/BigInt确定性代码ROUND_HALF_UP保留两位并入库，前端反算及CSV/XLSX同样两位；0.1555→0.16、0.005→0.01、0→0.00，null保持。辅助折数仍一位。所有新增/修改入口统一规范，不能从旧originalRatio绕过；负数/NaN/Inf仍拒绝，先校验再舍入。amount/官网金额/FX精度不受此规则影响。

旧报价升级前先备份；每个存储值变化的折扣事务更新payload/revision/updated_at并审计迁移前后值，重复启动不重复改，原文件/金额/模型/币种及既有审计/导入回执不改。旧高精度不再是正式折扣保留策略，可从备份及迁移审计查原值。

点击“折扣/折后价”切换到计算显示：有原折扣时以官网价×原折扣显示折后价（无需供应商单位，结果使用官网单位）；有原金额时先对齐官网单位再反算倍率。缺官网价、币种、单位或跨量纲参数明确提示，不假设两个空币种相同。官网0价不反算倍率，原始金额0和折扣0正常保留。计算只是临时视图；反算倍率按两位显示并附精确分子分母，不能自动写成原始折扣或反填金额。原折扣独立按上述两位入库策略规范。

前端“统一币种”和“统一单位”独立组合；秒/token窗口允许选择按分辨率/FPS的建议或自定义。建议有模型范围与估算标记，详情见 unit_conversion_reference.md。显示值及其导出不修改原始报价、原始折扣、全局参数或agent.md。原价导出与换算显示导出须明确区别，文件保留换算依据。

## 文件与审核

上传进入input/；启动任务后移至progress/。OpenCode只创建候选，独立审核面板支持修改、确认、取消。取消回input且不写正式业务数据/记忆；确认后事务入库并归档data/。重复确认幂等；原始报价、官网或目录在审核期间变化会409，必须复查新预览；仅显示用FX/倍率参数变化不再使原始草案过期。

支持拖拽/粘贴文件、供应商报价增删改、筛选与选中/模型条目导出。原件不改写。tests/集中代码和Codex测试库，tests/data/是私有验收资料，不进Git。

勾选报价后点击“删除所选”，只弹一次明确条数的永久删除确认，取消不发删除请求；确认直接事务删除所选报价，保留未选报价、模型、官网价和原始文件。存在缺失或版本冲突时整批不删，刷新复核后再选。单条删除及审核中的delete[]同样是真正删行，不再只是归档；删除前快照保留于审计。此功能不是服务启动清库，不能恢复的报价需从备份人工找回。

## 官网价快捷补充

在缺官网价的报价行点击“联网补官网价（新会话）”，会创建独立OpenCode会话，读取只读的docs/official_price_sources.md索引，再检索并抓取主来源。该会话只准补对应模型及计费范围的官网价/目录元数据，不能顺带改供应商报价；续聊也继承这个范围，不能附加Excel降级成普通导入。查询完成仍弹出审核界面，确认后才正式写入，取消不入库。

联网工具默认webfetch/websearch均deny，仅官网查询session显式allow。自定义Qwen provider需注册搜索工具：子进程设置OPENCODE_ENABLE_EXA=true，但这不是授权，普通会话仍deny。此行为按[OpenCode工具文档](https://docs.opencode.ai/docs/tools/#websearch)实现；若搜索服务不可达，优先按官网索引webfetch或代码取主源并如实说明，不编造结果。证据包含HTTPS来源URL、检索日期、地区/档位及具体价格位置；格式校验不是网页真实性证明，最终仍人工核对。

有明确单位ID时无需重复填写currency/unit/unit_quantity兼容字段，防止尺度字段误写；例如cny_mtoken已完整定义人民币/百万token。Write工具被路径规则限制时，OpenCode可在本run目录内通过已有bash工具生成candidate.json后交draft_cli，不扩大文件权限。

解析失败时，本轮已收到的真实assistant回答先显示，任务失败原因另行显示。历史请求超时不覆盖真实回答，也不能拿以前turn的回答冒充本次；失败草案可取消并退回文件。

## OpenCode独立环境与历史

默认项目外 ../TokenSQL-opencode/ 保存agent.md、memory.json、Session索引、runs草案和独立XDG配置/状态。SDK使用稳定directory和Session ID；UI选择会话后从API恢复历史，不仅存在浏览器内存。每次请求加载最新基础规则及带作用域的已确认人工纠错。取消草案不会生成规则；显示倍率也不会自动变成规则。

默认提示规则可在网页“纠错记忆”中添加global作用域并启用，保存后每次请求加载；项目外agent.md由memory.json及基础规则生成，直接手改会被覆盖。

模型可执行草案工具，正式写库权在宿主审核流程。bubblewrap把宿主文件系统设为只读、隐藏正式库/金标数据，只放开专用运行输出和OpenCode状态目录。网络为共享推理端点保留：这是可信个人环境的防误写隔离，不是对抗恶意代码的完整安全边界。--unsafe-no-sandbox 是显式降低隔离的选项，不默认启用。

## 等待补充（v2.2）

导入遇到关键歧义，或你明确要求先反问时，OpenCode可先提出结构化问题，状态显示“等待补充”而不是失败。等待不占后台推理，原文件留progress，不写报价或长期规则。直接在原聊天框答复再发送，会沿用同一个任务、会话和原文件继续；无需重新上传，可以多轮补答。解析明确后仍先展示草案，人工确认才入库。

刷新页面、切回会话或重启服务后，等待状态与问题保留。点取消将原件退回input；等待时不能确认旧草案。新增附件需先取消当前等待或切新会话，不能悄悄把新文件漏掉。若页面提示版本冲突，请刷新当前任务再补答，不连点重复提交。

默认只对关键歧义反问，不为每个可空字段机械停住。若希望这类文件总是先检查缺项，可在“纠错记忆”添加global或供应商/文件作用域的规则，例如：

> 生成入库草案前，先检查原表缺失或歧义；将需要我解释的内容一次性列出，使用等待补充流程。收到我的答复或明确允许留空后再生成草案。不得猜测，模型ID仍必须明确。

模型输出当前attempt的needs_input.json（questions非空字符串数组，可选summary），或proposal.json，二者排他；draft_cli ask校验并生成提问文件。续答采用独立attempt目录和最新catalog，不覆写只读附件，也不误取上轮草案。真正SDK/网络/格式错误仍为failed并显示真实回复，不能用等待态掩盖故障。官网补价续答仍保留原模型范围和联网权限，不允许降级导入供应商文件。

## 验证

```bash
.venv/bin/python -m pytest -q
.venv/bin/python -m tokensql.workspace.compare --baseline tests/data/codex.sqlite3 --actual var/workspace/quotes.sqlite3 --output tests/data/comparison-private.json
```

双库比较只读，不会初始化或改写待比对库；未知实体、同物理模型拆ID严格失败。真实文件验收与合成/浏览器API桩分别记录progress.md，不能互相冒充。

真实模型合成烟测使用 tools/workspace_smoke.py --base-url http://127.0.0.1:8787；默认审核候选后取消并复查空库，不是自动发布。--confirm仅对health声明isolated_test=true且初始报价空的测试实例开放，生产默认关闭该声明。请仅连接路径全部隔离、单独端口的QA，不对8765或用户业务文件运行。QA启动时显式 TOKENSQL_TEST_MODE=true，并将WORKSPACE_DIR/AGENT_DIR/INPUT_DIR/PROGRESS_DIR/DATA_DIR全部指向专用临时目录。脚本在内存生成标明SYNTHETIC的Excel，与input四份基准不同。
