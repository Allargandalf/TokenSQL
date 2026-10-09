# 报价工作台

当前入口是报价表和 OpenCode 聊天。少量 Excel/CSV 可以由 Agent 一次读取，按用户 Prompt 查询、创建或修订报价；不强制执行旧结构解析、整表理解、逐行语义抽取流程。金额、单位、日期、修改版本及事务由轻量 store 检查，所有写入都有审计记录。

## 远端启动

在项目目录安装依赖：`uv sync --locked --all-extras`。配置现有 OpenCode **真实 executable** 和 provider JSON 的路径，避免使用会覆盖 XDG/配置环境的共享 wrapper：

```bash
export TOKENSQL_OPENCODE_BIN=/path/to/opencode-runtime/node_modules/.bin/opencode
export TOKENSQL_OPENCODE_CONFIG=/path/to/local-coding-agent/opencode.json
.venv/bin/python -m tokensql.workspace.runtime \
  --opencode-bin "$TOKENSQL_OPENCODE_BIN" \
  --provider-config "$TOKENSQL_OPENCODE_CONFIG"
```

默认启动 HTTP `127.0.0.1:8765` 和独立 OpenCode `127.0.0.1:8766`。runtime 只读取指定 provider，不复制共享 plugins/MCP/session/permissions；OpenCode 配置与状态在工作台自身目录内。端口已占用会报错，不停止已有服务。Ctrl+C 或 SIGTERM 会关闭本次启动的 HTTP 和 OpenCode 子进程。

客户端建立 SSH 转发：

```bash
ssh -N -L 8765:127.0.0.1:8765 user@<REMOTE_HOST>
```

打开 `http://127.0.0.1:8765`。无需把数据库、附件或项目文件复制到本机。

可用 `--port`、`--opencode-port`、`--workspace-dir` 换端口或独立数据目录。使用已配置的独立 OpenCode 时，加 `--external-opencode`，并设置 `TOKENSQL_OPENCODE_URL`；不要复用 coding 会话。

## 使用

- 左侧工作表：搜索和下拉筛选；双击或选中行后 Enter 打开编辑；新增按钮创建报价。更新带 revision，遇到其他操作已修改时提示重新打开，避免静默覆盖。
- 导出：当前筛选、勾选条目或某模型全部报价；支持 CSV/XLSX。分页选择会保留已勾选项，改变筛选时清空隐藏选择。
- 导入：点导入或直接把文件拖到右侧，也可使用浏览器提供的文件剪贴板；文件 chip 可移除。右侧写 Prompt 后发送，Agent 真正调用工具维护数据库，完成后刷新表格。
- 默认单文件最多 20 MiB、单次最多 20 个文件、合计最多 80 MiB；可通过工作台环境配置调整。
- Prompt 示例：「供应商 A，厂商按模型归属填写；默认 CNY；B/C 列是每百万 token 输入/输出价格。查询已有报价后导入附件，不重复创建；无法确定的字段请先说明。」
- 现有报价也可直接聊：「把模型 X 的供应商 A 输入价改为 2.5 CNY/百万 token，其他报价不变。」

后台任务不堵住表格请求。界面展示助手回复、紧凑工具活动和数据库审计统计的真实修改数量；不依赖模型自报成功。金额类型与折扣类型分别保留原单位或折扣率，不把秒/张/次强行换成 token。

## 数据与旧库

默认工作台目录 `var/workspace/`：`quotes.sqlite3` 是新报价库，`inbox/` 保留上传附件，`opencode/` 保存独立 Agent 状态。原 Excel、旧价格库与验收库保持原状，新库初始为空。

需要复制旧 verified 组件时显式运行：

```bash
.venv/bin/python -m tokensql.workspace.cli --db var/workspace/quotes.sqlite3 \
  import-legacy /path/to/legacy.sqlite3
```

该命令只读旧 SQLite，复制所有 verified 快照的组件（包括历史），不是只导入每模型最新价。重复执行相同来源使用幂等键，不覆盖旧库。报价 CLI 的 `schema/list/apply/export` 可直接供 Agent 或操作人员使用；`apply` 中 create/update/delete 为同一事务，失败整批回滚，delete 为归档。

## 验证与边界

运行 `.venv/bin/python -m pytest -q` 验证数据接口与旧功能。真实模型小测可运行 `.venv/bin/python tools/workspace_smoke.py --server-url http://127.0.0.1:8766`；每次生成两行 XLSX 和临时报价库，预期四条 input/output 报价，不写业务库。

浏览器验收依赖可选 browser extra，浏览器安装命令：`PLAYWRIGHT_BROWSERS_PATH=./var/browser .venv/bin/python -m playwright install chromium`。真实执行结果见 progress.md，不把模拟模型测试当成实机证据。

只读浏览器回归：`PLAYWRIGHT_BROWSERS_PATH=./var/browser .venv/bin/python tools/workspace_browser_smoke.py --url http://127.0.0.1:8765`。它实际加载网页与点击发送，但拦截报价/Agent API 模拟失败后已写库的响应，验证页面错误、变更刷新与宽窄屏布局，不会写业务数据库。

Agent 可以处理复杂表格，但价格含义仍由用户 Prompt 与实际文件决定，金额不能凭模型自报置信度变成业务正确。官方联网采集、独立 PostgreSQL/容器部署不属于本次网页交付。

OpenCode 服务接口与会话能力依据 [官方 server 文档](https://docs.opencode.ai/docs/server/) 和 [SDK 文档](https://docs.opencode.ai/docs/sdk/)，本项目实际接入版本为 1.18.34。
