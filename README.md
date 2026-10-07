# TokenSQL

## 最新接手入口（2026-10-07）

当前开发规范见 [plan.md](plan.md)、[progress.md](progress.md)、[AGENTS.md](AGENTS.md) 和 [交接说明](docs/agent_handoff.md)。用户要求改为 Excel + 上传旁 Prompt/Context → Qwen 整表/逐行语义抽取 → 规则校验/标准化 → 高置信度自动入库，仅异常 review；优先复用 Strata 标准模型接口，不用旧 32B。

本次仅修订文档；以下安装/CLI/人工审核说明描述现有基础版代码，不代表新流程已实现。2026-09-23 的模型服务调查是历史事实，不是今日 Strata 服务状态，也不再是模型选择要求。实施时需同步更新这些说明，不要求普通用户先手工 bootstrap/JSON profile/逐行双重批准。

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

导入必须引用已经存在且审核过的 `data_source.id` 与 `provider.id`，并提供人工
确认过的 JSON parser profile。CLI 不会猜测 source/provider ID。示例 profile：

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

API 应提供 `GET /health`，交互文档预期位于
`http://127.0.0.1:8000/docs`。Streamlit 是可选依赖，UI 只是管理入口；所有写入
仍应经过服务层校验、事务和审计。

这些命令会持续运行服务；验收测试不会调用它们。容器部署见
`docs/deployment.md`。

## 测试

```bash
uv run --no-sync pytest -q tests/test_cli.py tests/test_cli_official.py tests/test_configured_official_adapter.py
uv run --no-sync pytest -q
```

CLI 测试使用 Typer `CliRunner` 和 mock，不会启动 Uvicorn、Streamlit、Docker 或
Qwen。真实样表测试只读取 `test/` 下的文件。

## 数据安全边界

- 原始工作簿、哈希、来源、parser 版本、原始价格/货币/单位和审核状态必须可追溯。
  Excel 原件以 SHA-256 内容寻址并保存在 `source_artifact.raw_content`；上传
  临时文件删除后仍可由数据库中的原始文件名、哈希和二进制内容复核。
- 公式不会执行；未知币种、条件价格、空表头、低置信度和不合法模型响应必须待审。
- Qwen 只能在 Python 提供的候选集合中做语义判断；不得生成主键、连接信息或 SQL。
- Excel 导入逐行执行目录精确匹配、限定符感知的高置信模糊匹配，再按配置可选调用 Qwen；歧义、低置信、未知或无效响应均创建模型审核项，匹配过程只写 staging，绝不直接发布价格。
- 官方价格采集与 Excel 导入相互独立，且不得用无来源的搜索摘要冒充官方价格。
  当前 Qwen 仅用于模型候选匹配，没有价格抽取接口；官方 JSON 价格使用确定性映射，
  价格语义歧义必须人工确认。
- 历史价格采用追加写入；不得覆盖已有快照。只有计费维度一致且验证通过的数据可比较。
- SQLite 适合单机 MVP 和测试；并发/生产部署使用独立 PostgreSQL 16，并执行备份、
  恢复和最小权限验收。
- 默认 Qwen 关闭，base URL 固定为 `http://127.0.0.1:18080/v1` 的 loopback
  安全占位；健康检查只报告 enabled/configured，绝不会探测或唤醒 Qwen。
- 启用时必须显式设置 model；非 loopback 地址还必须显式设置
  `TOKENSQL_QWEN_ALLOW_REMOTE=true`。超时与最低置信度分别由
  `TOKENSQL_QWEN_TIMEOUT_SECONDS`、`TOKENSQL_QWEN_MIN_CONFIDENCE` 控制。
  启用前仍须获得授权并单独验证 schema、并发与故障恢复。
