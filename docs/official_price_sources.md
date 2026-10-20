# 官网价格参考索引

维护日期：2026-10-16。此索引供Codex及用户主动开启的OpenCode官网价独立会话读取；不运行后台定时官网价抓取。快捷查询优先下列主来源，未列模型商可联网寻找其官方文档，不按模型前缀强认vendor。记录适用模型/计费项/地区档位、原币原价、URL与查询日期；找不到则保留null与说明，禁止拿供应商报价充当官网。表格明确官网价或联网核实结果均可成为草案，审核确认后录入。

- Anthropic / Claude：[官方平台定价](https://platform.claude.com/docs/en/about-claude/pricing)。
- Google / Gemini：[Gemini API 定价](https://ai.google.dev/gemini-api/docs/pricing)；Vertex等渠道须另核对区域与SKU，不能当同一份官网计费表。
- 阿里云百炼 / Qwen：[模型价格](https://help.aliyun.com/zh/model-studio/model-pricing)，注意国内/国际、上下文档位、输入/输出/缓存。
- 火山方舟 / Doubao、Seedance：[价格说明](https://docs.volcengine.com/docs/ark/model-pricing?lang=zh&redirect=1)。视频倍率边界见 unit_conversion_reference.md。
- DeepSeek：[官方价格](https://api-docs.deepseek.com/zh-cn/quick_start/pricing/)。
- 智谱 / GLM：[定价入口](https://bigmodel.cn/pricing)，页面为动态内容，访问成功不代表已核对具体价格。
- Kimi：[推理价格](https://platform.kimi.com/docs/pricing/chat)；旧 chat-k3 路径现重定向到此页。
- xAI / Grok：[API Pricing](https://docs.x.ai/developers/pricing)。
- OpenAI：旧 openai.com/api/pricing 已重定向至商业订阅价，不能据此录入API价格。应在 [API文档](https://developers.openai.com/api/docs/pricing) 核对，未核实具体SKU前不得用订阅套餐价充当模型价。

其他模型商待业务范围明确时补官方地址，不使用供应商报价页面冒充模型商官网。

## 参考汇率（与官网价格流水线分离）

[ECB参考汇率](https://www.ecb.europa.eu/stats/policy_and_exchange_rates/euro_reference_exchange_rates/html/index.en.html)及[XML](https://www.ecb.europa.eu/stats/eurofxref/eurofxref-daily.xml)。代码从共同EUR基准相除获得CNY/USD及CNY/HKD。来源按工作日发布，不是秒级交易报价，展示观测日/获取时间/失效时间；手动或可选定时刷新只更新汇率版本，不改原始模型价格。
