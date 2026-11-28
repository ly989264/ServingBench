# 面板二（新方案，v4.0-extra）的监控机侧

状态：test-glm53 已部署（v4.0-extra，2026-11-28），待验收；线上集群尚未使用。本目录由新面板二的维护者修改；改动经用户出包，先在 test-glm53 上执行，通过后再用于线上（见 `../interface/README.md`）。

本目录是面板二装在监控机上的部分：入库进程、ClickHouse 的建表与账号、看板生成、告警规则和各项命令。集群侧的过滤器、采集进程和 proxy 补丁不在本仓库。入库进程只经接口一（`../interface/`）读取数据。

## 1. 组成与数据路径

诊断记录的传输路径：

```
vLLM 实例（P、D）的过滤器 → 实例缓冲区 ─┐
                                        ├→ 采集进程 → 写出队列 → 入库进程 → ClickHouse → Grafana 看板
proxy 的诊断记录 → proxy 缓冲区 ────────┘     （proxy 机器）              （监控机）
```

| 部件 | 运行位置 | 所在目录 | 作用 |
|---|---|---|---|
| 过滤器与实例缓冲区 | 每个 vLLM 实例 | 集群侧，不在本仓库 | 从 vLLM 日志中取出诊断记录，留在实例内存中的缓冲区里 |
| 采集进程 `pd_fetcher.py` | proxy 机器上的 proxy 容器 | 集群侧，不在本仓库 | 读取各实例和 proxy 的记录，把每个请求的记录合并成一份请求文档，每分钟写一份状态文档，按序号写进写出队列；经接口一（`pd_diag/v1`，端口 9300）提供。本地开发用 `../interface/mock_fetcher.py` 代替 |
| 写出队列 | proxy 机器 `/data/pd-diag/fetcher/spool/` | — | 采集进程写出的文档，合计至多 2 GiB（缺省）；入库进程停止期间文档留在这里 |
| 入库进程 `pd_ingest.py` | 监控机，每个集群一个 | `ingest/` | 经接口一按序号读取文档，写进 ClickHouse；状态端口供 Prometheus 抓取 |
| ClickHouse | 监控机，每个集群一个容器 | `clickhouse/` | 库 `vllm_pd`，表结构即接口二（`pd_diag_db/v1`） |
| 看板生成器 `generate.py` | 监控机 | `scripts/` | 按库中的段定义 `segment_def` 生成看板，导入现有 Grafana |
| 告警规则 | 本集群的 Prometheus | `prometheus/` | 入库进程状态端口的 4 条规则（第 5 节） |

两个接口，入库进程与看板只按规范工作：

| 接口 | 提供方 → 调用方 | 规范所在目录 | 谁改 |
|---|---|---|---|
| 接口一 `pd_diag/v1`：采集接口 | 采集进程 → 入库进程 | `../interface/pd_diag_v1/` | 用户；本目录只读 |
| 接口二 `pd_diag_db/v1`：数据库接口 | ClickHouse 侧 → 看板与诊断工具 | `interface/pd_diag_db_v1/` | 本目录的维护者 |

段的增减只经接口一的段定义接口（`GET /pd_diag/v1/contract`）到达：入库进程把段定义写进 `segment_def`，看板生成器按 `segment_def` 生成看板。看板不自行加段，也不由其他数据推断段。

## 2. 目录

| 目录或文件 | 内容 |
|---|---|
| `bin/` | 各项操作的命令（第 4 节），在集群目录中执行 |
| `ingest/pd_ingest.py` | 入库进程 |
| `clickhouse/` | 建表语句 `schema.sql`、账号 `users.sql`、ClickHouse 的配置片段 `config.d/` |
| `grafana/` | 看板的样式与会话面板定义 |
| `prometheus/pd-ingest-rules.yml` | 告警规则 |
| `scripts/` | 看板生成器、检查和诊断脚本（Python 3 标准库） |
| `interface/pd_diag_db_v1/` | 接口二的规范、建表语句副本和 `SHA256SUMS` |
| `docs/诊断工具说明.zh-CN.md` | 诊断工具的说明 |
| `VERSION` | 版本号 |

本目录不含集群侧的文件：节点文件（过滤器、日志配置）、`segments.json` 与各模型组的段定义、真实日志样本、日志格式检查工具 `tools/check_pd_diag_format.py`。诊断工具中用到它们的检查项（`bin/diag.sh log` 的格式校验和与已知样本的比较）报「不适用」。

## 3. 配置文件

每个集群在监控机上有一个集群目录（`<总目录>/vllm-pd-grafana-panel2/<部署名>/`），其中三份文件由 `bin/panel2_cluster.sh add` 一次写出，容器名、端口和 UID 都写成具体值，运行时不再由部署名推导。

### 3.1 `ingest.json`（入库进程）

| 字段 | 缺省 | 含义 |
|---|---|---|
| `interface` | `pd_diag/v1` | 入库进程依赖的接口一的名称和主版本，随软件发布写好 |
| `fetcher_url` | 必填 | 采集进程的地址 `http://<proxy 机器>:<listen_port>`，例如 `http://10.1.0.20:9300` |
| `poll_seconds` | 5 | 单次读取未达到上限时的等待时长（秒） |
| `max_bytes` | 4194304 | 单次读取的字节数上限 |

### 3.2 `clickhouse.env`（ClickHouse 侧）

| 项 | 含义 |
|---|---|
| `CLICKHOUSE_CONTAINER`、`CLICKHOUSE_IMAGE`、`CLICKHOUSE_ADDR`、`CLICKHOUSE_HTTP_PORT` | 容器名、镜像、地址（容器名或 IPv4 地址）和映射到本机的 HTTP 端口 |
| `CLICKHOUSE_DATA_DIR`、`CLICKHOUSE_MIN_DISK_GB` | 数据目录（空则为 Docker 卷）、启动前要求的最小可用空间 |
| `DOCKER_NETWORK` | 与 Grafana 共用的容器网络 |
| `CH_ADMIN_PASSWORD`、`CH_WRITER_PASSWORD`、`CH_READER_PASSWORD` | 管理账号、入库进程的写入账号 `vllm_writer`、看板的只读账号 `vllm_reader` 的密码，安装时随机生成 |
| `SUMMARY_TTL_DAYS` | 文档保留天数，缺省 30 |
| `INGEST_STATUS_PORT` | 入库进程的状态端口（`/metrics`、`/status`） |
| `MONITOR_HOST` | 监控机地址，Prometheus 按它抓取状态端口 |
| `PROMETHEUS_URL`、`PROMETHEUS_CONTAINER`、`PROMETHEUS_YML`、`PROMETHEUS_MARK` | 本集群的 Prometheus，以及在 `prometheus.yml` 中标记本集群抓取任务的标记 |
| `INGEST_JOB`、`RULES_FILE`、`RULES_GROUP` | 抓取任务名、告警规则文件（Prometheus 容器内的路径）、规则组名 |
| `ALERT_STALE_SECONDS` | 「最近文档超过阈值」的阈值，缺省 120 秒 |

### 3.3 `grafana.env`（面板端）

| 项 | 含义 |
|---|---|
| `DB_INTERFACE` | 看板依赖的接口二的名称和主版本 `pd_diag_db/v1`，随软件发布写好 |
| `CLICKHOUSE_ADDR`、`CLICKHOUSE_HTTP_PORT`、`CH_READER_PASSWORD` | Grafana 连接 ClickHouse 的地址、端口和只读账号的密码 |
| `GRAFANA_URL`、`GRAFANA_USER`、`GRAFANA_PASSWORD`、`GRAFANA_CONTAINER` | 现有 Grafana 的地址、管理账号和容器名 |
| `GRAFANA_DS_UID`、`GRAFANA_DS_NAME`、`GRAFANA_BOARD_UID`、`GRAFANA_BOARD_TITLE` | 数据源和看板的 UID、名称 |
| `MODEL_GROUP` | 模型组，决定看板分在哪个文件夹 |
| `PANEL1_P_UID`、`PANEL1_D_UID` | 面板一 P、D 看板的 UID，供看板间跳转 |
| `STALE_RED_SECONDS` | 状态面板「最近文档距今秒数」显示为红色的阈值，缺省 60 秒 |
| `BOARD_HAS_VALUE` | 为 1 时分布表显示「有值 / 请求」列 |

## 4. 命令

在集群目录中执行。

| 命令 | 作用 |
|---|---|
| `bin/panel2_cluster.sh add <部署名> --model-group <组> [--fetcher-url …]` | 新装一个集群：写三份配置文件，启动 ClickHouse 并建表，装 Grafana 插件，加抓取任务和告警规则，启动入库进程，取得段定义后导入看板。某一步失败时改好后重跑同一条命令，已完成的步骤不重复执行 |
| `bin/panel2_cluster.sh remove <部署名> [--backup <目录>]` | 删除该集群及其数据和目录 |
| `bin/panel2_cluster.sh list`、`check <部署名>`、`quality <部署名>` | 已登记的集群；升级安全检查（只读）；数据准确性检查（不作为升级的条件） |
| `bin/clickhouse_up.sh` | 启动 ClickHouse，创建账号、表、版本表和视图；可重复执行 |
| `bin/ingest.sh start｜stop｜restart｜status` | 入库进程；退出后 5 秒自动重启，日志在 `logs/ingest.log` |
| `bin/grafana_plugin.sh`、`bin/grafana_board.sh` | 离线安装 ClickHouse 插件；按 `segment_def` 生成并导入数据源和看板 |
| `bin/prometheus_job.sh` | 向本集群的 Prometheus 加入抓取任务和 4 条告警规则；内容不变时不写入、不重新加载 |
| `bin/check_cpu.sh` | 监控机预检：CPU 特性与 ClickHouse 试运行、内存、Grafana 与 Prometheus 容器 |
| `bin/verify.sh`、`bin/postdeploy_check.sh`、`bin/coverage.sh` | 整条链路的检查；部署后文档的检查；各段在最近请求中有值的比例 |
| `bin/diag.sh <子命令>` | 诊断工具（只读，`pair` 除外），说明见 `docs/诊断工具说明.zh-CN.md` |
| `bin/clear_data.sh --yes [--all]` | 清空请求文档（`--all` 连同状态文档），保留表、段定义和看板 |
| `bin/monitor_down.sh [--purge]` | 从监控机移除本集群的面板二，缺省保留数据 |
| `bin/migrate_deploy_env.sh split｜retire-old｜drop-old-tables` | 由上一版迁移：拆分 `deploy.env`；停止上一版的拉取程序并删除 Vector 容器；删除上一版的 9 个表和视图 |
| `bin/has_value.sh show｜hide｜state` | 分布表「有值 / 请求」列的显示设置 |
| `bin/make_pack.sh <输出文件.tar.gz>` | 由本目录出部署包：复制一份（不含三份配置文件和 `generated/`、`run/`、`logs/`），在副本中写出 `SHA256SUMS` 后打包；本目录不变。安装后 `panel2_cluster.sh check` 按这份 `SHA256SUMS` 核对集群目录的文件 |

采集进程一侧的检查用 `../interface/fetcher_check.py --url http://<proxy 机器>:9300`（只读，只调用状态接口）。

## 5. 告警规则

由入库进程的状态端口提供，`bin/prometheus_job.sh` 加入本集群的 Prometheus：规则文件经 `docker cp` 写到 Prometheus 容器内 `RULES_FILE` 的路径（容器的数据目录 `/prometheus`），再在 `prometheus.yml` 中加 `rule_files` 和抓取任务的标记段并重新加载。

| 规则 | 触发条件 | 通常的原因 |
|---|---|---|
| 入库进程停止 | 状态端口无法抓取 | 入库进程停止；期间文档留在写出队列中 |
| 最近文档超过阈值 | 最近一份文档距今超过 `ALERT_STALE_SECONDS` | 采集进程停止。状态文档也计入，采集进程每分钟写一份，所以集群没有流量时不触发 |
| 无法连接采集进程 | 入库进程连续连接失败 | 网络中断或采集进程停止 |
| 写出队列丢弃文档 | `fetcher` 状态文档的 `log_dropped` 增加 | 入库进程停止的时间超过写出队列可容纳的时长 |

## 6. 文档的写出与计数

- 一个请求一份请求文档，`closed` 表示写出的原因：`complete`（各方记录齐全）、`no_proxy`（没有 proxy 记录，端到端时延从实例起算）、`incomplete`（记录不全且空闲超时）、`evicted`（尚未写出的请求超过上限）。
- 被 P 的前端拒绝的请求没有带请求号的记录，不产生文档，计入采集进程的 `no_api_dropped`。
- 写出后到达的迟到记录使这份文档重新写出，`doc_version` 加 1；查询用 `FINAL` 取最大的版本。
- 判为 `complete`、`no_proxy` 的第一份 `doc_version` 为 1，判为 `incomplete`、`evicted` 的第一份为 0。采集进程重启后由迟到的记录建出的不完整文档版本为 0，不会取代此前写出的完整文档。
- 没有 proxy 的记录是正常情况：proxy 的段为空，`client_request_id` 为空，端到端时延从实例起算；检查脚本和告警规则不把它当作异常。
- 状态文档每分钟一份，`scope` 为 `instance`（每个实例）、`proxy`（每个 proxy worker）或 `fetcher`；计数从进程启动起累计。
- 应为 0 的计数：实例缓冲区未读出即被覆盖的 `ring_overwritten_lines`、proxy 的 `buffer_overwritten`、`late_dropped`、`log_dropped`，以及入库进程的序号跳号 `ingest_gap`。

## 7. 由上一版迁移

由上一版（拉取程序与 Vector）迁移、回退的现场步骤由用户执行，不在本仓库。监控机上的三条迁移命令：

| 命令 | 作用 |
|---|---|
| `bin/migrate_deploy_env.sh split <集群目录> [--fetcher-url …]` | 由 `deploy.env` 写出三份新配置文件，`deploy.env` 另存为 `deploy.env.before-t62`，原有脚本和 `inventory.json` 打包为 `pack-before-t62.tar.gz`；不启动、不停止任何进程 |
| `bin/migrate_deploy_env.sh retire-old <集群目录>` | 停止上一版的拉取程序，删除 Vector 容器和上一版的告警规则文件；ClickHouse、旧表、看板和数据源保留，随后由 `panel2_cluster.sh add` 替换看板、数据源和抓取任务 |
| `bin/migrate_deploy_env.sh drop-old-tables <集群目录> --yes` | 删除上一版的 9 个表和视图及其数据，不可恢复 |

执行 `drop-old-tables` 之前回退时旧表仍在，历史数据可查询。

## 8. 接口的核对

- 入库进程启动时调用接口一的状态接口，接口主版本不支持或集群名与库中已有数据不同时不读取文档，原因写在 `/status` 的 `blocked` 和日志中。
- 遇到不认识的文档 `schema` 时，之前的文档照常写入，游标停在这份文档，原因同上。
- 看板生成器先读版本表 `db_interface`，与 `grafana.env` 的 `DB_INTERFACE` 不符时不生成看板、不改动已有看板。
- 接口一的变更规则见 `../interface/pd_diag_v1/接口一_采集接口规范.zh-CN.md` 第 5 节。

## 9. 已知问题与限制

- **监控机重启后入库进程不会自动启动。** `bin/ingest.sh start` 用 `nohup` 起一个循环，入库进程退出后 5 秒由循环重启；但监控机重启或这个循环本身被结束后，没有任何机制再启动它，需要在集群目录执行 `bin/ingest.sh start`。期间文档留在写出队列中，启动后从库中的最大序号接着读取。
- **集群没有流量时，状态面板「最近文档距今秒数」变红。** 这个数字取请求文档表 `request_doc` 中最新一份文档的时间，超过 `STALE_RED_SECONDS`（缺省 60 秒）显示为红色。没有流量时没有新的请求文档，数字持续增长并变红，并不表示采集或入库中断。数据链路是否在工作，以状态文档表 `status_doc` 中 `scope = 'fetcher'` 的最新时间为准（采集进程每分钟写一份）。告警「最近文档超过阈值」同时计入状态文档，不受影响。
- **`migrate_deploy_env.sh split` 删除集群目录中的 `inventory.json`。** 原文件打包在 `pack-before-t62.tar.gz` 中。此后仍读取集群目录中 `inventory.json` 的脚本（例如 `scripts/uninstall_old.py`）会因找不到文件而失败；需要时用 `tar -xzf pack-before-t62.tar.gz -C <集群目录> inventory.json` 取回。
- **修改固定列没有迁移路径。** `request_doc`、`status_doc` 的固定列（段列以外的列）由 `clickhouse/schema.sql` 的 `CREATE TABLE IF NOT EXISTS` 建立；`pd_ingest.py init` 和 `bin/clickhouse_up.sh` 对已有的表只调整保留天数，不改列。入库进程只为新段加列。新增、删除、改名固定列或改变列的类型后，已有集群的表不会随之改变，需要另写 `ALTER TABLE` 语句并在升级时执行；删除、改名和改类型还要按接口二规范第 4 节把版本表的 `major` 加 1，与看板在同一次升级中完成。
- 接口一不做身份认证、不限制来源地址。
- 一台机器只登记给一个集群，这一点不由任何机制保证，由集群清单的填写者保证。
- 采集进程只在 proxy 容器中手动启动，没有自动重启；停止时由告警「无法连接采集进程」和「最近文档超过阈值」发现。

## 10. ClickHouse 放在另一台机器上

ClickHouse 缺省与 Grafana 在同一台监控机上。监控机的 CPU 或内存不满足预检时，可放在另一台机器上：

1. 把本目录和该架构的离线镜像拷到那台机器，`docker load` 镜像。
2. 集群目录的 `clickhouse.env` 和 `grafana.env` 中，`CLICKHOUSE_ADDR` 都写那台机器的 IPv4 地址，`CLICKHOUSE_HTTP_PORT` 写它映射出的端口。
3. 在那台机器上执行 `docker network create <DOCKER_NETWORK>` 和 `bin/clickhouse_up.sh`。
4. 其余步骤仍在监控机上执行；监控机须能访问那台机器的 `CLICKHOUSE_HTTP_PORT`，入库进程仍在监控机上运行。
