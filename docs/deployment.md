# TokenSQL 部署说明

## 2026-10-09 网页工作台

当前默认入口是同源的报价表 + OpenCode 聊天网页，宿主机运行独立 HTTP/OpenCode 服务，通过 SSH 转发访问，SQLite 足以完成当前少量报价维护。配置、启动、附件目录及备份见 [工作台部署](workspace.md)。无需 Docker/PostgreSQL 或旧 parser profile 即可使用。

以下章节描述旧 API/Streamlit 与数据库部署历史，不是新工作台的前置条件；本轮实际启动与验收以 progress.md 最新记录为准。

> 2026-10-07 接手提示：Qwen 语义导入链路已实现，第 6 节已按实现更新环境变量与运维说明，规范见 [新版计划](../plan.md) 与 [交接说明](agent_handoff.md)。第 6 节保留的 18080/32B 是历史调查占位，不是当前模型选择；当前默认 base URL 为 http://127.0.0.1:8080/v1（loopback），实测 model ID 为 qwen3.8-flash-next-iq3_s，启用前仍须现场核实共享网关可用，不部署旧 32B、不重启共享服务。普通导入不再以第 4 节的 JSON profile/全人工批准作为产品要求；无 Prompt 时仍走旧确定性路径。

本文描述可执行的部署路径，不代表已经部署。本次交付没有构建镜像、拉取镜像、创建
容器、启动 API/UI/PostgreSQL，也没有重启 Qwen。

## 1. 准备配置

```bash
cp .env.example .env
```

逐项替换 `.env` 中的尖括号占位符。至少确认：

- `TOKENSQL_DATABASE_URL` 指向本项目独立的 SQLite 文件或 PostgreSQL 16。
- `TOKENSQL_QWEN_ENABLED=false` 保持默认，直到共享模型网关通过第 6 节的现场验收；
  启用时必须同时设置 `TOKENSQL_QWEN_MODEL`。
- 使用 PostgreSQL profile 时，设置新的强密码；不要复用其他业务凭据。
- 端口默认只发布到 `127.0.0.1`。需要反向代理时，应在代理层配置 TLS、鉴权和访问
  控制，而不是直接改成公网监听。

提交前可检查 Compose 展开结果，但输出可能包含本地配置值，不应粘贴到公开日志：

```bash
docker compose config
```

Compose 资源名默认由项目名隔离。共享主机或多套部署应显式设置唯一名称，镜像标签也可覆盖：

```bash
export COMPOSE_PROJECT_NAME=tokensql-dev
export TOKENSQL_IMAGE=tokensql:local
```

## 2. SQLite MVP

宿主机运行：

```bash
uv sync --locked --all-extras
mkdir -p var
export TOKENSQL_DATABASE_URL='sqlite+pysqlite:///./var/tokensql.db'
uv run tokensql db-upgrade
uv run tokensql bootstrap-catalog \
  --file examples/bootstrap-catalog.json \
  --database-url "$TOKENSQL_DATABASE_URL"
uv run tokensql serve-api --host 127.0.0.1 --port 8000
```

另一个终端可启动可选 UI：

```bash
uv run tokensql run-ui --host 127.0.0.1 --port 8501
```

容器运行时，API 默认使用项目隔离的 `tokensql-data` 命名卷中的
`/data/tokensql.db`。Dockerfile 使用 `uv.lock` 和 `uv sync --locked` 构建；
锁文件与项目元数据不一致时构建会失败，而不是重新解析依赖：

```bash
docker compose build api
docker compose run --rm api db-upgrade --alembic-config /app/alembic.ini
docker compose run --rm --volume ./examples:/bootstrap:ro api \
  bootstrap-catalog --file /bootstrap/bootstrap-catalog.json
docker compose up -d api
```

验证：

```bash
curl --fail http://127.0.0.1:8000/health
docker compose ps
```

启用 UI profile：

```bash
docker compose --profile ui up -d ui
curl --fail http://127.0.0.1:8501/_stcore/health
```

## 3. PostgreSQL 16 目标部署

PostgreSQL 使用 Compose 项目隔离的 `tokensql-postgres` volume 和 `tokensql` network（实际资源名带项目前缀），
不挂载或加入任何现有业务数据库的卷/网络。

在 `.env` 中设置：

```dotenv
POSTGRES_DB=tokensql
POSTGRES_USER=tokensql
POSTGRES_PASSWORD=<set-a-strong-password>
TOKENSQL_DATABASE_URL=postgresql+psycopg://tokensql:<same-password>@postgres:5432/tokensql
```

然后按顺序执行：

```bash
docker compose --profile postgres up -d postgres
docker compose --profile postgres ps
docker compose --profile postgres run --rm api db-upgrade --alembic-config /app/alembic.ini
docker compose --profile postgres run --rm --volume ./examples:/bootstrap:ro api \
  bootstrap-catalog --file /bootstrap/bootstrap-catalog.json
docker compose --profile postgres up -d api
```

不要在数据库健康前迁移。上线前还需实测备份、恢复、连接上限、磁盘告警和最小权限。
Compose 中映射的 PostgreSQL 端口同样只绑定 loopback；API 通过内部网络使用
`postgres:5432`。

## 4. Excel 预览与导入

预览不会连接数据库：

```bash
docker compose run --rm api profile-xlsx /imports/<file.xlsx> --json
```

如需容器访问工作簿，应由部署者添加只读 bind mount；不要把样表或供应商原始文件
复制进镜像。导入前必须：

1. 审核 bootstrap JSON 并运行 `bootstrap-catalog`，从输出的
   `ids.data_sources` 与 `ids.providers` 取得 source/provider ID；不要手工猜测主键。
   语义链路会自动 get_or_create 模型/供应商目录，但 `data_source.id` 仍必须来自
   已审核输出。
2. 语义导入（推荐）：确认 `TOKENSQL_QWEN_ENABLED=true` 且 model 已配置，随导入提供
   自由文本 Prompt（CLI `--prompt`/`--prompt-file`，API `prompt_text`/`context_defaults`），
   无需手工 JSON parser profile；`--provider-id` 可省略。
3. 旧确定性路径：为工作簿准备版本化的 JSON parser profile，并显式提供
   `--provider-id`。
4. 先运行 `profile-xlsx` 检查合并单元格、公式、缺失币种和 review 数量。
5. 运行 `import-xlsx --source-id ... [--prompt ... | --provider-id ... --profile ...]`。

## 5. 官方价格单次刷新与外部调度

TokenSQL 默认不会运行官方采集，也不会创建后台线程、cron、systemd timer 或
Kubernetes CronJob。运维系统只能显式调用一次性命令：

```bash
export TOKENSQL_OFFICIAL_ADAPTER_CONFIG=/secure/path/official-adapter.json
uv run tokensql official-refresh \
  --data-source-id <primary-official-source-id> \
  --allowed-domain <exact-official-host> \
  --adapter tokensql.official.configured:create_adapter \
  --limit 20 \
  --timeout 10
```

内置 `configured` 适配器严格解析运维已审核的官方 JSON 映射，不调用 Qwen。
`examples/official-adapter.example.json` 只有 `.invalid` 域名和占位值，不能直接用于
生产或被描述成真实报价。真实厂商 URL、offering selector、JSON pointer、catalog
metric code、单位和条件都必须逐项审核；配置规范见 `docs/official_adapter.md`。

`TOKENSQL_DATABASE_URL` 和 `TOKENSQL_OFFICIAL_ADAPTER_CONFIG` 必须由受保护的运行
环境或 secret 注入；不要把含密码的 URL 写进命令参数、crontab、镜像、仓库或公开
日志。`--allowed-domain` 只接受主机名，可重复指定；HTTP 客户端仍强制 HTTPS、公共
地址、同主机重定向、超时、响应大小和内容类型限制。不要创建“搜索引擎适配器”，也
禁止用搜索摘要补齐或猜测价格。

命令输出不含原始页面、数据库 URL 或凭据，只给出批次/制品 ID、候选数量和状态。
每轮先处理缺少已验证官方价的 offering，再按最旧验证/采集时间刷新已有官方价；
`--limit` 对全局队列生效，每个 offering 每轮只处理一次。任一 offering 为
`failed`/`skipped` 时进程返回非零；成功抓取仍只产生待审核候选，不会自动发布价格。

周期执行应由部署者在项目外配置，并把上述命令作为唯一任务体：

- cron：使用权限受限的 wrapper 加载受保护环境文件后调用一次命令；设置独占锁，保留
  非零退出码并告警。不要把 secret 直接写入 crontab。
- systemd：使用 `Type=oneshot` service + timer，数据库 URL 与适配器配置路径通过
  权限为 `0600` 的 `EnvironmentFile` 注入；配置合理的运行超时与失败告警。
- Kubernetes：使用 `CronJob`/一次性 `Job`，数据库 URL 来自 Secret，审核后的适配器
  配置以只读 ConfigMap/Secret 挂载，并设置 `concurrencyPolicy: Forbid`、资源限制和
  失败历史保留。

这些外部调度定义属于部署环境，不由应用写入或安装。上线前用录制 fixture、mock HTTP
客户端和测试数据库验收适配器，不在 CI 或验收测试中访问真实官网。

## 6. Qwen 语义导入

语义导入链路已实现。默认配置（`src/tokensql/config.py`）：

```dotenv
TOKENSQL_QWEN_ENABLED=false
TOKENSQL_QWEN_BASE_URL=http://127.0.0.1:8080/v1
TOKENSQL_QWEN_MODEL=<model-id-required-when-enabled>
TOKENSQL_QWEN_TIMEOUT_SECONDS=30
TOKENSQL_QWEN_ALLOW_REMOTE=false
TOKENSQL_QWEN_MAX_IN_FLIGHT=1
TOKENSQL_QWEN_MAX_TOKENS=8192
TOKENSQL_QWEN_REASONING_EFFORT=low
TOKENSQL_QWEN_AUTO_PUBLISH_MIN_CONFIDENCE=0.85
TOKENSQL_QWEN_MIN_CONFIDENCE=0.75
TOKENSQL_QWEN_EXTRACTION_VERSION=qwen-extract-v3
TOKENSQL_QWEN_TABLE_PROMPT_VERSION=qwen-table-v2
```

默认 base URL 是 loopback 的共享 OpenAI 兼容网关；启用前必须现场核实该网关可用、
model ID 已加载，且不得重启、升级或替换共享模型服务。容器内的 `127.0.0.1` 指向
容器本身，跨主机使用时必须显式配置可达地址并同时设置
`TOKENSQL_QWEN_ALLOW_REMOTE=true`，否则配置校验拒绝启动；key 只经环境/secret 注入，
不写入镜像或 Compose 源文件。API 健康检查只报告 enabled/configured，不发送模型请求。

并发与超时：

- `TOKENSQL_QWEN_MAX_IN_FLIGHT`（默认 1，1–8）是进程级、按 base URL 共享的信号量
  上限：同一进程内多个请求各自建客户端，但共用一个在途预算，取最大配置值，用于
  保护共享网关不被 TokenSQL 挤占。
- `TOKENSQL_QWEN_TIMEOUT_SECONDS` 默认 30 秒、上限 300 秒，作用于预检与每次
  chat completion。
- `TOKENSQL_QWEN_REASONING_EFFORT` 默认 `low`（可选 `none`/`low`/`medium`/`high`），
  `TOKENSQL_QWEN_MAX_TOKENS` 默认 8192。
- `TOKENSQL_QWEN_AUTO_PUBLISH_MIN_CONFIDENCE` 默认 0.85，同时作用于整行置信度与
  `model_name`/`components` 字段置信度；低于门槛进 review。旧确定性路径的候选匹配
  门槛仍是 `TOKENSQL_QWEN_MIN_CONFIDENCE`（默认 0.75）。
- 全部模型调用发生在数据库写事务之外；导入先跑模型阶段，再在单事务内落库。

模型服务不可用时的运维行为：预检（`GET /v1/models`）或整表理解失败会抛出
`SemanticImportError`，API 返回 503，批次以 `failed` 状态与错误信息留痕，写事务
回滚、不产生半写数据；单个行批次的超时/坏响应只把该行送 review，批次转
`partial`。重试语义：相同文件+相同上下文（含 prompt 哈希）重放时，成功/部分完成
的既有批次直接返回而不重跑模型；`failed` 批次则会被复活并重跑导入（幂等键保持
唯一，错误信息更新）。`POST /staged-quotes/{id}/re-extract`
可对单条待审核行在网关恢复后重跑抽取。

历史说明：2026-09-23 调查中的 18080 端点与 32B 权重是旧代码占位与历史事实，已不是
当前模型选择；旧 `QwenClient` 的候选索引匹配仍保留给无 Prompt 的确定性路径。
Function Calling 不是安全边界，数据库写入始终由 Python 服务层完成。

## 7. 回滚与运维

- 应用回滚：停止新应用容器并恢复上一镜像标签；不要回滚或删除价格历史。
- 数据库迁移：生产环境降级前先验证 downgrade 脚本和备份，不直接删除 volume。
- SQLite：停写后复制数据库及 WAL/SHM；生产切换 PostgreSQL 时做记录数和哈希核对。
- PostgreSQL：使用受控备份目录和加密存储，定期执行恢复演练。
- 日志不得记录数据库密码、API key、完整供应商文件或未脱敏的模型原始响应。

## 8. 验收命令

以下命令不启动服务器或容器：

```bash
uv run --no-sync python -m py_compile src/tokensql/cli.py
uv run --no-sync pytest -q tests/test_cli.py tests/test_cli_official.py tests/test_configured_official_adapter.py
uv run --no-sync pytest -q
```

需要部署授权后，再单独执行 Compose 构建、迁移、健康检查和 UI/API 冒烟测试。
