# TokenSQL

文件 + Prompt → OpenCode 直接维护报价数据库。

`v2.1_cleaned` 只保留报价工作台：左侧筛选、编辑和导出；右侧聊天、上传文件和执行维护。没有旧 parser、逐行语义导入、审核发布或第二套数据库。

## 启动

在远端项目目录执行：

```bash
uv sync --locked
.venv/bin/tokensql \
  --opencode-bin /path/to/opencode-runtime/node_modules/.bin/opencode \
  --provider-config /path/to/local-coding-agent/opencode.json
```

使用 OpenCode 真实 executable，不使用会覆盖共享环境变量的 wrapper。程序启动网页 `127.0.0.1:8765` 和独立 OpenCode `127.0.0.1:8766`，不更改共享模型服务。

本机执行 SSH 转发并保持终端开启：

```bash
ssh -N -L 8765:127.0.0.1:8765 user@<REMOTE_HOST>
```

打开 <http://127.0.0.1:8765>。服务必须先在远端运行；SSH 不会自动启动网页。

## 使用

- 把 Excel/CSV 拖到右侧，填写供应商、币种、单位等 Prompt，再发送。
- OpenCode 用工具读取附件、查询已有报价，经同一事务接口维护数据库；不是固定抽取流水线。
- 左侧可搜索、筛选、双击编辑，导出当前筛选、选中条目或某模型全部报价，支持 CSV/XLSX。
- 保留 Decimal 金额、修改版本与审计；有歧义时通过聊天说明，不凭空编造价格。

默认数据目录为 `var/workspace/`，首次启动自动创建空库，不自带报价、上传附件或会话。`test/` 留给用户新样例；样例、数据库和运行文件不提交 Git。

## 验证

```bash
uv sync --locked --extra test
.venv/bin/python -B -m pytest -q -p no:cacheprovider
```

测试仅在临时目录生成数据，不依赖旧 Excel。浏览器与真实模型小测见 [工作台说明](docs/workspace.md)。

## 项目入口

- `src/tokensql/workspace/`：唯一应用、数据 store、CLI、OpenCode 集成和前端。
- `tests/`：仅工作台回归；`tools/`：可选验收工具。
- [plan.md](plan.md)：范围与验收；[progress.md](progress.md)：当前进度。
- `tokensql` 启动网页；`tokensql-quotes --db <path> schema/list/apply/export` 操作报价。

旧代码与历史文档可从 Git 标签 `v2.0` 找回，不属于当前运行路径。
