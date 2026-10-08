# TokenSQL

## 新导入链路已实现（2026-10-07）

新主链路已实现并通过回归测试（240 项 pytest；语义端到端使用 mock 网关，真实模型闭环与浏览器/容器实测状态见 [progress.md](progress.md)）。开发规范与交接见 [plan.md](plan.md)、[progress.md](progress.md)、[AGENTS.md](AGENTS.md) 和 [交接说明](docs/agent_handoff.md)。

已实现链路：Excel 上传旁提供自由文本 Prompt/Context（UI 文本框 + 4 个结构化默认输入 `manufacturer`/`provider_name`/`currency`/`unit_basis`；CLI `--prompt`/`--prompt-file`；API `prompt_text`/`context_defaults`）→ 确定性结构解析 → Qwen 整表理解（先于逐行）→ 分批逐行语义抽取（默认 10 行/批）→ Pydantic/规则校验（币种白名单、单位归一化含百万/千 token 换算、计费 metric 解析、evidence 单元格坐标核验、置信度门槛默认 0.85 同时作用于整行与 `model_name`/`components` 字段、外币需 verified 汇率、冲突与缺字段进 review）→ 高置信度且规则通过的行由服务层自动 get_or_create 目录（manufacturer/provider/model/version/alias（off、unverified）/offering/plan）、自动发布 PriceSnapshot（`verified`，actor `qwen-auto:<model>`，`validation_method=auto_rules`）并自动标准化（CNY 直接；外币使用 verified FX 汇率）；仅低置信度或规则异常行进入 review。

模型走 OpenAI 兼容接口 `POST /v1/chat/completions`（`json_schema` strict + `reasoning_effort`，默认 `low`），默认 base URL `http://127.0.0.1:8080/v1`（仅 loopback，除非 `TOKENSQL_QWEN_ALLOW_REMOTE=true`），启用时 model 必填（当前实测 `qwen3.8-flash-next-iq3_s`）；全部模型调用发生在数据库写事务之外。Prompt/Context 进入两次模型请求与 `ImportBatch.import_context`（`prompt_sha256`、`context_scope`、`context_version`、`semantic.table_understanding` 等）并纳入幂等键；默认值只填缺、不覆盖表内明示值，冲突进 review。旧确定性路径保留：不提供 prompt 时行为与旧版一致。

以下安装/CLI/人工审核说明描述基础版代码；2026-09-23 的模型服务调查是历史事实，不是今日模型服务状态，也不再是模型选择要求。

## 现有基础版说明（历史实现）

TokenSQL 是一个可追溯的 Token 报价管理 MVP。它先用 Python 确定性读取 Excel，

版本控制仓库：[Allargandalf/TokenSQL](https://github.com/Allargandalf/TokenSQL)；用途是保存基线与 review 后续 Qwen 改动，不发布 release/tag。

仓库只包含代码、文档、测试代码和配置模板；三份真实报价 Excel（test/*.xlsx）保留在远端且不上传。公开克隆缺少这些原件，需自行放入对应样表才能运行真实样表相关测试；不要把历史187通过误认为无样表公开克隆也已验证。

建议 Qwen 从 main 创建工作分支；review 使用 git diff <基线提交SHA> 比较基线与当前内容，并检查新增/未跟踪文件，不只看它的自述。
把不确定项送入人工审核；本地 Qwen 仅可作为可选的语义候选判断器，不能决定数据库
主键、执行 SQL 或绕过校验直接写库。

## 当前远端环境结论

2026-09-23 的只读调查结果如下：

- 服务器为 Ubuntu 22.04、Python 3.10，用户目录已有 `uv`；项目使用独立
  `.venv`，不复用模型或其他业务环境。
- 主机没有可用的 PostgreSQL 服务。当前 MVP 使用可配置的 SQLite 文件，生产目标为
  独立 PostgreSQL 16；不得复用主机上的其他业务数据库或数据卷。
- 调查开始时存在的 Qwen2.5-1.5B vLLM 服务，在最小结构化输出探测中触发既有
  vLLM/xgrammar 接口不兼容后自行退出。当时 18080 endpoint 已不可用；不是 2026-10-07 Strata 服务状态。
- Qwen2.5-VL-32B-Instruct 权重与专用环境存在，但服务未启动，结构化输出与
  Function Calling 均未验收。本项目没有停止、重启、升级或修改任何 Qwen 服务。
- 外网总体可达，但部分官方定价站点会返回 403、地区页或动态内容；正式价格只接受
  可验证的官方页面/API，抓取失败必须进入人工复核。

更完整的证据见 `docs/environment_report.md`，数据库关系见
`docs/database_design.md`。

## 安装

在远程项目目录执行：

```bash
uv sync --locked --all-extras
cp .env.example .env
mkdir -p var
```

`.env.example` 只有占位值。不要提交 `.env`，也不要把密码、API key 或真实生产
连接串写入 README、Compose 文件或命令历史。

本地开发建议覆盖为持久化 SQLite 文件：

```bash
export TOKENSQL_DATABASE_URL='sqlite+pysqlite:///./var/tokensql.db'
uv run tokensql db-upgrade
```

配置 PostgreSQL 时使用独立数据库与凭据，例如环境变量中的
`postgresql+psycopg://<user>:<password>@<host>:5432/<database>`。迁移始终由
Alembic 执行，不使用 ORM 自动建表替代迁移。
`db-upgrade` 默认先查找当前工作目录中的 `alembic.ini` 与同级 `alembic/`，
再回退到源码树；从其他目录运行已安装包时，应显式传入
`--alembic-config /path/to/alembic.ini` 或设置
`TOKENSQL_ALEMBIC_CONFIG`。

## Catalog bootstrap

全新迁移库没有可供导入引用的 source/catalog ID。先审核严格 JSON 文档，再原子、
幂等地写入基础目录：

```bash
uv run tokensql bootstrap-catalog \
  --file examples/bootstrap-catalog.json \
  --database-url "$TOKENSQL_DATABASE_URL"
```

文档以局部 `slug` 建立引用，支持 data source、manufacturer/model/version、
provider/offering/plan 和 billing metric；未知字段、未知引用或既有记录冲突会使
整个事务回滚。相同文档再次执行只会返回 `reused`，不会复制记录。命令输出的 `ids.data_sources` 与 `ids.providers` 可供后续 `import-xlsx`
的 `--source-id` 和 `--provider-id` 使用；示例文件仅是结构模板，生产值必须单独审核。
语义导入链路会按已校验的抽取结果自动 get_or_create 模型/供应商等目录，但
`data_source.id` 仍必须来自已审核的 bootstrap 输出。

## CLI

查看所有命令：

```bash
uv run tokensql --help
```

只预览 Excel，不写数据库：

```bash
uv run tokensql profile-xlsx 'test/token报价 (1).xlsx' --json
uv run tokensql profile-xlsx 'test/token折扣.xlsx' --discount-scale 10
```

导入必须引用已经存在且审核过的 `data_source.id`。`--provider-id` 与 `--profile` 现为可选：
提供自由文本 Prompt 且 Qwen 启用时走语义链路，目录由服务层自动创建/复用，此时可省略
`--provider-id`；不提供 Prompt 时仍要求 `--provider-id`，并可给出人工确认过的 JSON
parser profile。CLI 不会猜测 source ID。

语义导入示例（需 `TOKENSQL_QWEN_ENABLED=true` 且已设置 `TOKENSQL_QWEN_MODEL`）：

```bash
uv run tokensql import-xlsx 'test/token折扣.xlsx' \
  --source-id <existing-source-id> \
  --prompt '该表模型厂商为 XX；报价供应商 YY；缺省币种 CNY；价格按每百万 Token' \
  --parser-version cli-v1
```

`--prompt-file <file>` 从 UTF-8 文本文件读取 Prompt，与 `--prompt` 互斥。旧确定性路径示例
（无 Prompt 时行为与旧版一致）：

```json
{
  "header_row": 1,
  "discount_scale": 10,
  "price_headers": ["折扣", "蒸馏折扣"]
}
```

```bash
uv run tokensql import-xlsx 'test/token折扣.xlsx' \
  --source-id <existing-source-id> \
  --provider-id <reviewed-provider-id> \
  --profile <reviewed-profile.json> \
  --parser-version <version>
```

如果导入服务、API 或 UI 模块尚未安装，CLI 会以非零状态退出并给出明确错误，不会
伪装操作成功。

一次官方价格刷新必须显式提供 primary official `data_source.id`、审核过的适配器和
精确官方域名 allowlist：

```bash
export TOKENSQL_DATABASE_URL='sqlite+pysqlite:///./var/tokensql.db'
export TOKENSQL_OFFICIAL_ADAPTER_CONFIG=/secure/path/official-adapter.json
uv run tokensql official-refresh \
  --data-source-id <primary-official-source-id> \
  --allowed-domain <official-pricing-host> \
  --adapter tokensql.official.configured:create_adapter \
  --limit 20 \
  --timeout 10
```

内置适配器只解析运维已审核映射的官方 JSON，不调用 Qwen；
`examples/official-adapter.example.json` 使用 `.invalid` 域名和占位字段，仅用于说明
配置结构，不能冒充真实厂商报价。命令只执行一次并退出，不会启动后台线程。每轮先
处理尚无已验证官方价的 offering，再按最旧验证/采集时间刷新已有官方价；`--limit`
对整轮队列生效且每个 offering 只处理一次。候选只进入 staging/review；403、429、地区限制、
页面结构变化和解析错误会留下审核与失败审计，并以非零状态汇报失败。只允许官方
页面/API，禁止使用搜索摘要。真实厂商 URL、JSON pointer、catalog metric code 和
适用条件必须由运维审核配置；详见 `docs/official_adapter.md`，外部调度见
`docs/deployment.md`。

## API 与 UI

预期的本地启动命令：

```bash
uv run tokensql serve-api --host 127.0.0.1 --port 8000
uv run tokensql run-ui --host 127.0.0.1 --port 8501
```

API 提供 `GET /health`（只报告 Qwen enabled/configured，不探测模型），交互文档位于
`http://127.0.0.1:8000/docs`。语义导入相关端点：

- `POST /imports` 的请求 JSON 支持 `prompt_text` 与 `context_defaults`；`prompt_text`
  非空且 Qwen 启用时走语义服务，否则走旧确定性路径。模型不可用时返回 503。
- `GET /imports/{batch_id}/context` 回读批次上下文（`prompt_text`、`prompt_sha256`、
  `context_defaults`、`semantic.table_understanding` 等），批次不存在返回 404。
- `POST /staged-quotes/{id}/re-extract` 对单条待审核行重跑语义抽取，通过则自动发布；
  需 Qwen 启用（否则 409），行不存在 404，已批准或请求体非法分别 409/422，批次缺少
  已存整表理解时 503。

Streamlit 是可选依赖，UI 只是管理入口；导入页在上传旁提供 Prompt 文本框和 4 个结构化
默认输入（默认模型厂商/报价供应商/缺省币种/计价单位），自动合并进导入请求，并提供
"本批次上下文回显"读取 `GET /imports/{id}/context`。所有写入仍应经过服务层校验、
事务和审计。

这些命令会持续运行服务；验收测试不会调用它们。容器部署见
`docs/deployment.md`。

## 测试

```bash
uv run --no-sync pytest -q tests/test_cli.py tests/test_cli_official.py tests/test_configured_official_adapter.py
uv run --no-sync pytest -q
```

CLI 测试使用 Typer `CliRunner` 和 mock，不会启动 Uvicorn、Streamlit、Docker 或
Qwen。语义链路测试使用 httpx mock 网关，不调用真实模型服务；真实模型闭环状态见
progress.md。真实样表测试只读取 `test/` 下的文件。

## 数据安全边界

- 原始工作簿、哈希、来源、parser 版本、原始价格/货币/单位和审核状态必须可追溯。
  Excel 原件以 SHA-256 内容寻址并保存在 `source_artifact.raw_content`；上传
  临时文件删除后仍可由数据库中的原始文件名、哈希和二进制内容复核。
- 公式不会执行；未知币种、条件价格、空表头、低置信度和不合法模型响应必须待审。
- Qwen 是主要语义抽取者，但没有任何 SQL、数据库 ID 或 DDL 权限：prompt 明确禁止
  输出 SQL/主键，模型只见单元格坐标与文本；目录创建、价格发布和标准化全部由
  服务层在事务内校验后执行，模型建议（catalog_suggestions）只是提案。
- 自动发布有独立审计：actor 记 `qwen-auto:<model>`，审计事件与 staged payload 带
  `validation_method=auto_rules`、模型名、request id、置信度和 prompt 哈希，与人工
  发布路径可区分；原件追加写入、历史不覆盖和幂等约束不变。
- 旧确定性 Excel 路径保留：逐行目录精确/模糊匹配、可选 Qwen 候选匹配，只写
  staging，绝不直接发布价格。
- 官方价格采集与 Excel 导入相互独立，且不得用无来源的搜索摘要冒充官方价格。
  官方 JSON 价格使用确定性映射，价格语义歧义必须人工确认。
- 历史价格采用追加写入；不得覆盖已有快照。只有计费维度一致且验证通过的数据可比较。
- SQLite 适合单机 MVP 和测试；并发/生产部署使用独立 PostgreSQL 16，并执行备份、
  恢复和最小权限验收。
- 默认 Qwen 关闭，base URL 默认为 `http://127.0.0.1:8080/v1` 的 loopback 地址；
  非 loopback 必须显式设置 `TOKENSQL_QWEN_ALLOW_REMOTE=true`，否则配置校验拒绝启动。
  健康检查只报告 enabled/configured，绝不探测或唤醒模型服务。
- 启用时必须显式设置 model；语义抽取前会先经 `GET /v1/models` 预检确认模型已加载。
  全部模型调用在数据库写事务之外，失败不会留下半写批次。

## Qwen 配置

全部变量与默认值（见 `src/tokensql/config.py`）：

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `TOKENSQL_QWEN_ENABLED` | `false` | 语义导入总开关；关闭时走旧确定性路径 |
| `TOKENSQL_QWEN_BASE_URL` | `http://127.0.0.1:8080/v1` | OpenAI 兼容网关；默认仅 loopback |
| `TOKENSQL_QWEN_MODEL` | 无（启用时必填） | 当前实测 `qwen3.8-flash-next-iq3_s` |
| `TOKENSQL_QWEN_API_KEY` | 无 | 可选 Bearer token，勿写入仓库/日志 |
| `TOKENSQL_QWEN_ALLOW_REMOTE` | `false` | 非 loopback base URL 必须显式开启 |
| `TOKENSQL_QWEN_TIMEOUT_SECONDS` | `30`（上限 300） | 每次模型请求超时 |
| `TOKENSQL_QWEN_MAX_IN_FLIGHT` | `1`（1–8） | 进程级按 base URL 共享的并发上限 |
| `TOKENSQL_QWEN_MAX_TOKENS` | `8192`（256–32768） | 单次补全最大 token 数 |
| `TOKENSQL_QWEN_REASONING_EFFORT` | `low` | 可选 `none`/`low`/`medium`/`high` |
| `TOKENSQL_QWEN_AUTO_PUBLISH_MIN_CONFIDENCE` | `0.85` | 自动发布门槛，作用于整行与 `model_name`/`components` 字段置信度 |
| `TOKENSQL_QWEN_MIN_CONFIDENCE` | `0.75` | 旧确定性路径的候选匹配门槛 |
| `TOKENSQL_QWEN_EXTRACTION_VERSION` | `qwen-extract-v3` | 逐行抽取/prompt 版本，进入幂等键与审计 |
| `TOKENSQL_QWEN_TABLE_PROMPT_VERSION` | `qwen-table-v2` | 整表理解 prompt 版本，记入批次上下文 |
