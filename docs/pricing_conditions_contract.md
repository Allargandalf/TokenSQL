# 条件定价与整模折扣共同契约

更新：2026-10-28。主Agent Astra统筹；Subagent gpt-6.1-sol ultra。本文优先于旧model_id+billing_item唯一官网价和固定12单位约定。实现分工：backend负责schemas/store/conditions/units/data_routes；frontend负责static/浏览器；workflow负责agent/workflow/memory/draft/lookup；主Agent负责文档/compare/整合与正式迁移。

## 原始数据与身份

- model_id保持唯一模型身份。计费项、条件、渠道差异不能制造新的模型ID。
- amount与discount_rate独立原始输入；倍率HALF_UP两位，金额/FX不缩精度。所有派生价格、折扣反算、显示与导出转换只能由确定性代码执行，不覆盖原值。
- billing_item保留旧原文标签；billing_metric为可空规范化字符串，推荐input/output/cache_read/cache_write/cache_storage/image/video/audio/request/subscription。不得把泛称cache猜成read或write；自定义维度仍可保留。
- 官方价格新增official_price_id（稳定opaque string）；同模型同metric可有多个条件价。更新使用ID+expected_revision；旧model+label调用只有唯一候选时兼容，多候选报告冲突。不同条件不可静默覆盖，旧长标签不可因metric相同而合并。
- 官方和供应商均有pricing_conditions结构化条件；conditions原文dict保留。范围/标签/备注并存，各自职责明确；渠道标签不能充当计算用自由文本匹配键。

## pricing_conditions

可选字段：market、platform、input_modality、output_modality、input_tokens、output_tokens、mode、time_band、cache_ttl_seconds、resolution、with_audio、reference_video、customer_tier、tax_status、legacy_text、raw。

input_tokens/output_tokens分别使用范围对象：min、max（可空整数），min_inclusive、max_inclusive。保留上下界及端点含义，不把输入长度和输出长度混成一个上下文档位。其他字段按后端schema约束，非法范围/类型在审核前明确报告。

market保存来源明确的国内/国际或市场语义，platform保留原厂/云平台区别；不得仅凭CNY/USD猜地区。mode区分普通/批量等，time_band区分忙闲等，时段未给具体小时/时区不能自动推断当前适用价。缓存写入TTL与按小时缓存存储不是同一计费项。tax_status可为included/excluded/unknown；未给或unknown不视为已证明同税基，不能默默混排。

legacy_text/raw只保留尚未结构化且影响适用价格的条件原文，不把它们当已证明可互换的条件。表头/单元格/币种单位来源证据存source_ref、conditions或note，不放pricing_conditions.raw。已完整结构化时raw={}；显式pricing_conditions={}允许无额外限制。只有旧输入完全缺省pricing_conditions且有conditions时才兼容镜像raw，不把显式空条件重新污染。仅可用确定的字面规则做兼容升级；未知条件保留并提示，不丢失原条目。

## 折扣范围

quote.discount_scope为unknown/model/items，默认unknown；discount_items为条目选择器数组，每项可含billing_metric、billing_item、official_price_id、pricing_conditions。

- unknown表示原文未明确适用范围。billing_item=null不能自动解释成全部。
- model表示有明确证据的整模默认折扣。保存一条供应商规则，按每条官方记录自身的计费项/条件展开前端展示；不能合并成单一官网价格或重复生成多条原始折扣。
- items表示明确的分项范围，选择器可进一步限定条件。具体计费项的原始金额按对应条件比较。
- 分项明确例外可覆盖对应整模默认，不能自动叠乘。候选发生互不包含的条件冲突时提示选择/补条件，不任取第一条、最低价或按文字数量猜优先级。
- 旧scope升级必须有明确原文证据并留审计。当前港研院source与conditions折扣口径均可核对；其余未知不批量猜。历史amount/currency/unit/discount及source_ref均保持。

## 报价响应与前端

兼容official_price单对象，仅在一条明确匹配时提供。新增official_prices为明确匹配条目数组；official_match包含status、reason、candidate_ids、matched_ids。

effective_discount_rules是服务端仅语义投影：每项携带official_price_id、有效quote_id/discount_rate/discount_scope、reason、candidate_quote_ids，不在后端算价。同model+供应商+渠道+相同tags集合的全量active规则参与（不受前端分页影响）；匿名供应商不跨报价借规则。明确分项优先整模默认；同优先级倍率冲突则返回null并说明，不叠乘。原报价discount_rate不改。

status为matched/multiple/ambiguous/needs_scope/missing。具体项unknown范围即使仅一个官网候选，缺少该候选的价格决定条件仍需补充；明确model/items范围可枚举各条件价。只有真正没有可用官网价时提示缺价及联网补价；有官网候选但条件/范围未明确时说明已有N项、需要选择计费项/条件或补范围。候选ID可在catalog定位，不误报模型未识别官网价。

主表只显示model_id、一个供应商价和一个折扣（如7.0折），不出现“源折扣”或重复倍率。供应商原金额即实际支付价；仅折扣时按匹配官网价前端计算支付价，默认可比较，无需先打开计算开关。原文/计算依据放详情，官网多项细则收进独立详情。金额/折扣/换算证据的导出仍保留规范化原始值及来源，并可输出多项派生展示。

官网目录、报价编辑、审核共用计费项/条件/范围编辑逻辑；简单报价无需填满复杂字段，可折叠高级条件。稳定ID是系统标识，不要求普通用户手工填写。编辑和导出不能把派生值混成原始输入。

## 单位扩展

保留旧12单位ID，添加USD/HKD/CNY各自的mtoken_hour、request、account_month，共21项，字典version=2。mtoken_hour对应token_hour、数量1000000、token_time量纲；request及account_month数量1。它们支持原值存储及有明确意义的同量纲币种换算，不支持token/秒/张的伪等价。新增完整订阅商品目录不是本轮范围，不能为录入账号月费伪造物理模型。

## 模型导入与兼容

OpenCode继续文件+Prompt理解整表，不增加固定逐行Excel解析流水线。原厂价格与官方价格同义；按表头和行/列条件组织多条官网价，保留工作表/单元格证据。整模折扣明确scope，价格决定因素记录到pricing_conditions，不仅塞进标签或备注。

模型只准备草案，人工确认落库；关键价格/范围歧义才waiting_for_input，供应商报价缺供应商必须补充后才可提交，模型/官网价不要求供应商；其他可空元数据仍可null。范围缺失影响计算而非原文保存。已确认任务、历史审计、import回执和会话正文不重写；旧ready若需策略升级，只同步bundle/original规范、重预览和revision，不自动确认、不沉淀为人工纠错。

## 迁移与验收

2026-10-28用户要求清空正式库，取消存量语义整理；不读取备份、不重导旧Excel。新工程在隔离合成库验收，正式库等待用户重新上传。后续普通报价更新原位事务增改删，不新增每期整库/Excel备份。

重点场景：Claude5项官网+一条七折；DeepSeek忙闲国内国际；Gemini文本/图片不同输出；豆包输入/输出双档；unknown和重复候选；分项例外；新复合单位；CSV/XLSX零写；旧会话/等待/取消/幂等与多轮审核。主Agent独立核对隔离合成数据及浏览器；真实文件验收等待用户重新上传，合成、Mock、真实浏览器和真实模型证据分开记录。
