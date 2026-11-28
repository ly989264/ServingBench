# 接口一：新面板二的采集接口（pd_diag/v1）

接口一是集群侧的采集进程对监控机上的入库进程提供的只读 HTTP 接口，也是新面板二中用户与新面板二的维护者之间的分界：分界以上（集群侧的过滤器、采集进程、proxy）由用户负责，分界以下（入库进程、ClickHouse、看板，即 `../panel2-v4/`）由新面板二的维护者负责。现场由 test-glm53 的采集进程提供（proxy 机器，端口 9300）。

## 1. 分工

| 目录 | 谁改 | 其他人 |
|---|---|---|
| `interface/`（本目录） | 用户 | 新面板二的维护者只读 |
| `../panel2-v4/` | 新面板二的维护者 | 用户审阅、出包、在内网执行 |

接口二（数据库接口 `pd_diag_db/v1`，ClickHouse 对看板）的两端都在 `../panel2-v4/` 中，规范在 `../panel2-v4/interface/pd_diag_db_v1/`，由新面板二的维护者负责。

## 2. 本目录的文件

| 文件 | 内容 |
|---|---|
| `pd_diag_v1/接口一_采集接口规范.zh-CN.md` | 接口一的规范：接入参数、三个端点、序号与游标、文档结构、版本规则、调用方的要求 |
| `pd_diag_v1/*.schema.json` | 4 份 JSON Schema：请求文档 `request.v2`、状态文档 `status.v1`、段定义接口的响应 `contract.v1`、状态接口的响应 `status_api.v1` |
| `pd_diag_v1/samples/` | 13 份样例：请求文档 8 份（`complete`、`no_proxy`、`incomplete`、`evicted`，粒度 1、2、3，含空字段），状态文档 3 份（`instance`、`proxy`、`fetcher`），段定义接口和状态接口的响应各 1 份 |
| `pd_diag_v1/SHA256SUMS` | 以上 18 个文件的 SHA-256 |
| `mock_fetcher.py` | 模拟采集进程：按样例提供接口一的三个端点，单文件、只用 Python 标准库 |
| `fetcher_check.py` | 采集进程的检查脚本：调用状态接口，逐个来源、写出队列和计数给出结论，最后一行 `RESULT: OK` 或 `RESULT: FAIL`。对真实的采集进程和模拟采集进程都适用 |

规范、Schema 和样例与现场采集进程所用的为同一版本（`SHA256SUMS` 逐项相同）。

## 3. 接口的三个端点

入库进程只调用这三个端点（`../panel2-v4/ingest/pd_ingest.py` 开头的说明）：

| 端点 | 入库进程何时调用 |
|---|---|
| `GET /pd_diag/v1/status` | 启动时，以及连续失败期间每 5 秒：核对接口主版本和集群名；每分钟一次：看 `config_version` 是否变化 |
| `GET /pd_diag/v1/contract` | 启动时，以及出现新的 `config_version` 时：把段定义写进 `vllm_pd.segment_def`，为新段在 `request_doc` 中加列 |
| `GET /pd_diag/v1/docs?after=<游标>&max_bytes=<字节数>` | 持续：游标是库中 `request_doc`、`status_doc` 两表 `max(seq)` 的大者；返回满了接着取，不满等 5 秒 |

详细定义见规范第 2、3、6 节。

## 4. 变更规则

- 接口的变更以规范第 5 节为准：文档新增字段、状态文档新增计数、端点新增可选参数不升主版本，入库进程不需要升级；删除字段，改变字段的含义、类型或单位，改变游标或去重规则，改变端点路径，都要升主版本。本目录不另定规则。
- 段的新增、删除和改名只经段定义接口（`GET /pd_diag/v1/contract`）到达：`config_version` 改变，入库进程写进 `segment_def`，看板按 `segment_def` 重新生成。看板不自行加段，也不由其他数据推断段。
- 需要接口一提供新的字段或段时，向用户提出；用户改采集进程和本目录，并在 test-glm53 上更新后通知维护者。

## 5. 改动怎样到达内网

仓库里的改动不会自动到达内网。

1. 新面板二的维护者在仓库里修改 `../panel2-v4/`，在本地按第 6 节跑通。
2. 用户审阅后出包，在 test-glm53 上执行。
3. test-glm53 上通过后，用户再用于线上集群。

## 6. 本地开发

不需要内网和集群：模拟采集进程代替采集进程，本机起一个 ClickHouse，入库进程和看板生成器用 `../panel2-v4/` 中的原样代码。

需要：Python 3.9 或以上；Docker（用于 ClickHouse 镜像 `clickhouse/clickhouse-server:26.8.36.3`）；`curl`。

以下命令在仓库根目录执行，用到的端口为 9300（模拟采集进程）、18123（ClickHouse）、18191（入库进程的状态端口），被占用时一并改掉。

**第 1 步：启动模拟采集进程（单独一个终端）。**

```bash
python3 interface/mock_fetcher.py --port 9300 --cluster lab
```

写出队列先有 12 份样例文档，之后每秒新增 1 份请求文档（样例的副本，换新的 `request_id`）、每 60 秒新增一组 3 份状态文档。`--rate 0` 只提供 12 份样例。

**第 2 步：检查模拟采集进程。**

```bash
python3 interface/fetcher_check.py --url http://127.0.0.1:9300
```

**第 3 步：准备工作目录和三份配置文件。** 工作目录相当于监控机上的集群目录，目录名即集群名。

```bash
W=$HOME/pd-local/lab && mkdir -p "$HOME/pd-local" && cp -R panel2-v4 "$W"
```

```bash
A=$(python3 -c 'import secrets;print(secrets.token_hex(12))'); WR=$(python3 -c 'import secrets;print(secrets.token_hex(12))'); RD=$(python3 -c 'import secrets;print(secrets.token_hex(12))')
```

```bash
printf '{"interface": "pd_diag/v1", "fetcher_url": "http://127.0.0.1:9300", "poll_seconds": 5, "max_bytes": 4194304}\n' > "$W/ingest.json"
printf 'CLICKHOUSE_CONTAINER=pd-local-clickhouse\nCLICKHOUSE_ADDR=127.0.0.1\nCLICKHOUSE_HTTP_PORT=18123\nCH_ADMIN_PASSWORD=%s\nCH_WRITER_PASSWORD=%s\nCH_READER_PASSWORD=%s\nSUMMARY_TTL_DAYS=30\nINGEST_STATUS_PORT=18191\n' "$A" "$WR" "$RD" > "$W/clickhouse.env"
printf 'DB_INTERFACE=pd_diag_db/v1\nCLICKHOUSE_ADDR=127.0.0.1\nCLICKHOUSE_HTTP_PORT=18123\nCH_READER_PASSWORD=%s\nGRAFANA_DS_UID=pd-local-clickhouse\nGRAFANA_DS_NAME=pd-local-clickhouse\nGRAFANA_BOARD_UID=pd-local-request\nGRAFANA_BOARD_TITLE=面板二本地开发\nMODEL_GROUP=local\nSTALE_RED_SECONDS=60\n' "$RD" > "$W/grafana.env"
```

`CLICKHOUSE_ADDR` 写成 IPv4 地址时，入库进程直接连这个地址的 `CLICKHOUSE_HTTP_PORT`。三个口令只在本机使用，每次随机生成。

**第 4 步：启动 ClickHouse，建账号和表。**

```bash
docker run --rm -d --name pd-local-clickhouse -p 18123:8123 -e CLICKHOUSE_PASSWORD="$A" -e CLICKHOUSE_DEFAULT_ACCESS_MANAGEMENT=1 -v "$W/clickhouse/config.d/vllm_pd.xml:/etc/clickhouse-server/config.d/vllm_pd.xml:ro" clickhouse/clickhouse-server:26.8.36.3
```

```bash
for i in $(seq 1 60); do curl -s -u "default:$A" 'http://127.0.0.1:18123/?query=SELECT+1' && break; sleep 2; done
```

```bash
docker exec pd-local-clickhouse clickhouse-client --password "$A" -q 'CREATE DATABASE IF NOT EXISTS vllm_pd' && sed -e "s/{CH_WRITER_PASSWORD}/$WR/" -e "s/{CH_READER_PASSWORD}/$RD/" "$W/clickhouse/users.sql" | docker exec -i pd-local-clickhouse clickhouse-client --password "$A" --multiquery
```

```bash
python3 -I "$W/ingest/pd_ingest.py" init --dir "$W"
```

**第 5 步：运行入库进程（单独一个终端，同一组变量）。**

```bash
python3 -I "$W/ingest/pd_ingest.py" run --dir "$W"
```

**第 6 步：确认文档已入库。**

```bash
curl -s -u "vllm_reader:$RD" --data-binary "SELECT count() FROM vllm_pd.request_doc FINAL" http://127.0.0.1:18123/; curl -s http://127.0.0.1:18191/status
```

**第 7 步：生成看板。**

```bash
cd "$W" && GRAFANA_ENV="$W/grafana.env" python3 -I scripts/generate.py --out "$W/generated/vllm-pd-request.json"
```

导入 Grafana 用 `bin/grafana_board.sh`，需要一个装有 ClickHouse 数据源插件（`grafana-clickhouse-datasource`）的 Grafana，并在 `grafana.env` 中补 `GRAFANA_URL`、`GRAFANA_USER`、`GRAFANA_PASSWORD`。

**第 8 步：结束。** 在两个终端中按 Ctrl-C 结束模拟采集进程和入库进程；`docker stop pd-local-clickhouse`（带 `--rm` 启动，停止后容器和数据一并删除）；删除工作目录 `$HOME/pd-local/lab`。

### 6.1 实测结果（2026-11-28）

| 步骤 | 结果 |
|---|---|
| 第 1、2 步 | `fetcher_check.py` 对模拟采集进程输出 `RESULT: OK`；三个端点的输出（状态接口、段定义接口、62 份请求文档和 12 份状态文档）按 4 份 JSON Schema 校验全部通过 |
| 第 7 步（不连库） | `python3 -I scripts/generate.py --contract interface/pd_diag_v1/samples/contract.json --out <文件>` 按样例的段定义生成看板：P 27 段、D 32 段，看板 29 个 panel |
| 第 3–7 步（连库） | 尚未实测 |
