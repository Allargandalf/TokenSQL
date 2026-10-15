# 工作台使用与维护

唯一流程：文件 + 用户 Prompt → 独立 OpenCode 会话用工具读取文件、查询和维护报价 → 事务校验与审计 → 页面刷新。

## 运行配置

执行 README 的启动命令前先 `uv sync --locked`。可选配置见 `.env.example`；不要提交真实 `.env`、provider JSON 或密钥。

- `--workspace-dir`：独立数据目录，默认 `var/workspace/`。
- `--port` / `--opencode-port`：网页和独立 OpenCode 的端口，默认 8765/8766。
- `--opencode-bin`：现有 OpenCode 真实 executable 路径。
- `--provider-config`：只读复用指定 provider 的 JSON 路径；不复制共享插件/MCP/会话。
- 也可导出 `TOKENSQL_OPENCODE_BIN` / `TOKENSQL_OPENCODE_CONFIG` 代替上述路径参数；它们不是自动从 `.env` 读取的参数。
- 使用已经独立部署的 OpenCode 时，加 `--external-opencode`，设置 `TOKENSQL_OPENCODE_URL`。

runtime 只管理自己启动的子进程；端口冲突会报错，不会杀掉已有服务。Ctrl+C / SIGTERM 同时关闭网页和本次 OpenCode。当前为前台程序，没有自动开机常驻配置。

## 文件和 Prompt

右侧可选择、拖放文件，或粘贴浏览器剪贴板中的文件。默认单文件 20 MiB、单次 20 个、合计 80 MiB。大模型通过 Python/openpyxl 等工具读取附件，不要求用户配置列映射或解析 profile。

示例：

> 导入附件报价。报价供应商为 A，默认 CNY；B/C 列分别是每百万 token 输入/输出价。厂商按模型归属填写。先查询已有报价，不重复创建；无法确定的字段先向我说明。

界面显示助手回复、工具摘要和审计统计的真实修改数量；后台执行不堵住表格。失败后若此前已有写入，也显示实际变更并刷新，不伪装成成功。

左侧支持搜索和下拉筛选、双击或选中后 Enter 编辑、Ctrl+K 搜索，以及当前筛选/勾选条目/模型全部报价导出。编辑使用 revision 防止静默覆盖其他修改。分页保留选择，改变筛选会清空隐藏选择。

## 存储和 CLI

`var/workspace/quotes.sqlite3` 是唯一业务库。`inbox/` 存上传附件，`opencode/` 存独立会话状态。首次启动只建表，不自动扫描或导入 `test/`；用户重新提供文件后才按 Prompt 入库。

```bash
.venv/bin/tokensql-quotes --db var/workspace/quotes.sqlite3 schema
.venv/bin/tokensql-quotes --db var/workspace/quotes.sqlite3 list
.venv/bin/tokensql-quotes --db var/workspace/quotes.sqlite3 apply changes.json --actor manual
.venv/bin/tokensql-quotes --db var/workspace/quotes.sqlite3 export quotes.xlsx
```

`apply` 接受 create/update/delete 数组，整批事务失败就回滚；delete 是归档，修改前后值保留审计。没有旧库迁移、staging/review/publish 或自动建目录接口。币种与秒/张/次/token 等原计价单位保留，不强制换成 token。

## 验收

```bash
uv sync --locked --extra test --extra browser
.venv/bin/python -B -m pytest -q -p no:cacheprovider
PLAYWRIGHT_BROWSERS_PATH=/path/to/browser-cache .venv/bin/python tools/workspace_browser_smoke.py --url http://127.0.0.1:8765
.venv/bin/python tools/workspace_smoke.py --server-url http://127.0.0.1:8766
```

浏览器需要可选 browser extra 和 Chromium，可用 `PLAYWRIGHT_BROWSERS_PATH=/path/to/browser-cache .venv/bin/python -m playwright install chromium` 安装。

浏览器 smoke 拦截 API，只检查发送、失败变更刷新和布局，不写业务库；模型 smoke 在临时目录生成两行 XLSX 和独立库，会真实调用模型，不把生成样例放进 `test/` 或业务库。两者不等同于真实业务文件验收。

## 清空与历史

清理数据库时先停止本工作台，将库和 WAL/SHM、附件与会话一起备份，随后首次启动生成新空库；不要触碰共享模型服务。`v2.1_cleaned` 已按用户确认清走旧资料并在项目外留备份。

旧系统源码/历史文档只存在于 Git 历史（如 `v2.0`），不应重新接入当前应用；禁止读取 handoff 或私有交接副本。
