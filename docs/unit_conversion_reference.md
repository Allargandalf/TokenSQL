# 视频秒与 token 建议倍率

来源核对日期：2026-10-16。

## 适用范围

- 本文只提供火山方舟 Seedance 视频生成的估算依据，不是视频的通用物理等价，
  也不能替代其他厂商、模型或计费项的官网规则。
- 前端可把建议值填入临时的 `tokens_per_second`，供 `/s` 与 token 视图估算。
  用户仍可改写；模型、分辨率、宽高比、FPS 和来源必须随建议一起显示。
- 建议倍率与用户自定义倍率只影响当前显示和显示视图导出，不写报价数据库、
  `pricing_context`、审核草案或 Agent 记忆。

## 官方依据

- [火山方舟模型价格：视频生成模型](https://www.volcengine.com/docs/82379/1099320)
  给出的估算公式是：
  `token = (输入视频时长 + 输出视频时长) × 输出宽 × 输出高 × 输出 FPS ÷ 1024`。
  页面同时声明结果是估算值，准确用量以 API 返回的
  `usage.completion_tokens` 为准。
- [创建视频生成任务 API](https://docs.volcengine.com/docs/ark/create-video-generation-task-api?lang=zh)
  给出各 Seedance 模型支持的分辨率、宽高比和精确像素值。Seedance 2.5
  支持 480p、720p、1080p；Seedance 2.0 支持 480p、720p、1080p、4k；
  fast/mini 只支持 480p、720p。该接口没有 2K 输出档。
- [火山方舟模型列表](https://docs.volcengine.com/docs/ark/model-list?lang=zh)
  标明 Seedance 输出帧率为 24 fps。
- [查询视频生成任务 API](https://docs.volcengine.com/docs/ark/list-video-generation-tasks-api?lang=zh)
  把 `completion_tokens` 定义为生成视频消耗的 token，可用于计费对账；
  视频生成模型不统计输入 token，因此 `total_tokens = completion_tokens`。
  这不否定公式中的“输入视频时长”：参考视频时长仍参与生成视频
  completion token 的估算。

## 24 fps 建议值

方向统一为 `tokens_per_second`，这里的一秒是公式中的
“输入视频秒数 + 输出视频秒数”之一秒。

- Seedance 2.5，480p，16:9 `854×480` 或 9:16 `480×854`：
  `9607.5 tokens/s`。
- Seedance 2.0，480p，16:9 `864×496` 或 9:16 `496×864`：
  `10044 tokens/s`。不能与 2.5 的 480p 建议混用。
- Seedance 2.0 或 2.5，720p，16:9 `1280×720` 或 9:16
  `720×1280`：`21600 tokens/s`。
- Seedance 2.0 或 2.5，1080p，16:9 `1920×1080` 或 9:16
  `1080×1920`：`48600 tokens/s`。Seedance 2.0 fast/mini 不支持
  1080p。
- 仅 Seedance 2.0，4k，16:9 `3840×2160` 或 9:16
  `2160×3840`：`194400 tokens/s`。

横竖规格只是宽高互换，像素积相同，所以建议倍率相同。4:3、3:4、1:1、
21:9 的官方像素积可能不同，必须使用创建 API 的对应精确宽高重新计算，
不能只按“480p/720p”等标签套用上述 16:9 值。

## 确定性计算

以下脚本使用十进制运算生成上述建议，不经过二进制浮点数：

```python
from decimal import Decimal, localcontext

SPECS = {
    "seedance-2.5/480p/16:9": (854, 480, 24),
    "seedance-2.0/480p/16:9": (864, 496, 24),
    "seedance-2.x/720p/16:9": (1280, 720, 24),
    "seedance-2.x/1080p/16:9": (1920, 1080, 24),
    "seedance-2.0/4k/16:9": (3840, 2160, 24),
}

with localcontext() as context:
    context.prec = 60
    for name, (width, height, fps) in SPECS.items():
        tokens_per_second = (
            Decimal(width) * Decimal(height) * Decimal(fps) / Decimal(1024)
        )
        print(name, format(tokens_per_second, "f"))
```

## 误差与禁止外推

- 官方公式本身是估算。账单与对账必须采用调用成功后返回的
  `usage.completion_tokens`；Seedance 2.0 系列在实际用量低于最低 token
  用量时，API 会返回并按最低量计费。价格页还说明 Seedance 2.0 系列和
  Seedance 2.5 在包含输入视频时存在最低 token 用量。
- 公式没有独立的音频参数。创建 API 虽允许 Seedance 2.0/2.5 生成同步音频，
  但现有价格公式不足以证明“音频免费”或推导另一倍率；界面不得作这种声明。
- 30 fps 可以用同一公式作为用户明确选择的自定义假设，但不是当前官方
  Seedance 输出规格，因此不提供官方建议默认值。
- 2K 不是当前官方 Seedance 视频输出档，也没有唯一像素定义；不提供默认值。
  只有用户提供明确宽、高、FPS 和适用模型来源时，才可临时计算并标为未验证支持。
- 不把 Gemini 等视频理解的输入 token 规则用于 Seedance 视频生成输出，
  也不把本文倍率用于跨模型真实计费。
