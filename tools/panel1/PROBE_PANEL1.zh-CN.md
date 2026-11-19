# 面板一现场探测：probe_panel1.py

`probe_panel1.py` 把现场的面板一（Grafana 的两张看板、Prometheus 的配置和指标）与 v3 基线逐项对比，报出差异；同时把重新生成面板一需要的现场配置导出到本机。它只读，不改 Grafana 和 Prometheus 里的任何东西。

输出三份：

| 输出 | 内容 | 去向 |
|---|---|---|
| `panel1-probe.txt` | 短摘要，同时打印到屏幕：一屏以内（默认最多 40 行，每行不超过 100 个字符），只列不同点，每条一个编号；IP 已打码 | **只发回这一份** |
| `panel1-export/panel1-detail.txt` | 全部明细，差异按同样的编号列出；另有目录检查的文件清单 | 只留在本机 |
| `panel1-export/panel1-probe.json` | 全部比对明细（结构化）；IP 已打码 | 只留在本机 |
| `panel1-export/` 其余文件 | 本机导出：现场配置原文和结构化内容，不打码，供之后的更新工具读取；`export.json` 的 `items` 按编号列出全部差异 | **只留在本机，不要外传**（含内网地址、主机名和完整看板） |

## 运行

前提：
- Python 3.6 及以上，只用标准库，不用装包；
- 在 Grafana 和 Prometheus 所在的监控机上运行；
- Grafana 账号有 Viewer 权限即可，也可以用服务账号 token（`GRAFANA_TOKEN`）。

```bash
export GRAFANA_URL=http://localhost:3000
export GRAFANA_USER=admin
read -s -p 'Grafana 密码：' GRAFANA_PASSWORD; echo; export GRAFANA_PASSWORD
export PROMETHEUS_URL=http://localhost:9090
python3 probe_panel1.py --grafana-dir /etc/grafana --prometheus-dir /etc/prometheus \
    --provisioning-dir /etc/grafana/provisioning --prometheus-yml /etc/prometheus/prometheus.yml --inventory inventory.json
```

容器部署、目录没有挂到本机的，两个目录参数写成 `容器名:容器内路径`，例如 `--grafana-dir vllm-pd-grafana:/etc/grafana --prometheus-dir vllm-pd-prometheus:/etc/prometheus`。

- 地址换成实际地址。没设 `GRAFANA_PASSWORD` 时，脚本会提示输入密码。Prometheus 开了认证的，另设 `PROMETHEUS_USER` 和 `PROMETHEUS_PASSWORD`，或者设 `PROMETHEUS_TOKEN`。
- 三个路径参数都可以不加，不加的那一项在导出里记为「没有」，其余照常：
  - `--provisioning-dir`：Grafana 的 provisioning 目录，含义和 `diag_panel1.py` 的同名参数相同。容器部署的，填挂进容器的那个主机目录。
  - `--prometheus-yml`：本机上的 prometheus.yml。容器部署的，填挂进容器的那个主机文件。`rule_files` 里写的容器内路径，会按它所在的目录换算成本机路径。
  - `--inventory`：原来给 `gen_prometheus_yml.py` 用的 inventory.json。没有就不加。
- `--grafana-dir`、`--prometheus-dir`（可选）：列出目录下全部文件（相对路径、大小、sha256 前 8 位），按文件名与 v3 包对应的文件比较，分为一致、不同、现场多出、现场缺少。
  - 认得的文件比内容：provider 和数据源的 yaml 逐行比（密码、secret、token 一类的值只写「有值」）；看板 json 按 uid 与基线看板比，并与 Grafana 里加载的版本比；`grafana.ini` 只看 paths、server、security、users、auth*、dashboards、panels、feature_toggles 段里设了值的项；`prometheus.yml` 的 job、目标、label 和 rule_files 与 Prometheus 已加载的配置比。
  - `grafana.db`、Prometheus 数据（`wal`、`chunks_head`、块目录）只记存在和大小，不读、不复制。
  - `容器名:路径` 用 `docker cp` 读到内存，不落盘，不在容器里执行任何命令；运行的账号要能用 docker。
- `--max-lines N` 改短摘要的行数上限（默认 40）。差异超过上限时，最后一行写「其余 N 项见本机明细」并按部分计数。
- `-o 前缀` 改短摘要的文件名，`--export-dir` 改导出目录。导出目录已存在且不为空时，新导出会写到加了时间后缀的目录里，不覆盖旧的。`--no-mask` 让短摘要、明细和 panel1-probe.json 里的 IP 显示原值。

**发回：** 只发 `panel1-probe.txt`，其余都留在本机。IP 默认打码，同一个地址始终对应同一个代号（host-01、host-02……），`10-0-1-1` 这种连字符写法也算。主机名、模型名、文件路径不打码，发回前请自行检查。所有输出里都不含密码和 token。

## 短摘要怎么读

- 第一行：时间、基线的看板摘要、IP 是否打码；
- 第二行：差异总数（不同、新增、缺少、读不到各几项）；和基线一致的项合成一个计数，按部分列出；
- 之后每行一项差异：`编号 部分 种类 对象：说明`，例如 `D03 看板 不同 P/801 查询 A：expr 不同，基线「…」，现场「…」`。部分是 读不到、Grafana、看板、Prometheus、Grafana目录、Prometheus目录；种类是 不同、新增、缺少、读不到；
- 最后一行：本机明细的位置。按编号在 `panel1-detail.txt`（或 `export.json` 的 `items`）里查完整内容。
- 编号按发现的顺序给出（D01、D02……）。现场变了，编号也会变；按编号回复时，以同一次运行的短摘要和本机明细为准。

## 本机明细 panel1-detail.txt

- 第一、二行：时间和基线；访问不到的部分和原因；
- 接下来四组：和基线一致、有差异、基线没有的现场新增、现场缺少，差异前面是短摘要里的编号；
- 「现场 label」：现场指标实际带的 label 和取值；
- 「本机导出」：导出目录名、cluster 和 model_group 这两个 label 名有没有被占用、prometheus.yml 和 inventory.json 收没收到、Prometheus 能不能热加载、指标名个数、看板里已用到 cluster 的查询、看板文件在本机的位置。

| 部分 | 比较的内容 | 基线 |
|---|---|---|
| Grafana | 版本 | 12.4.2 |
| 看板 P、D | 是否存在、版本号、加载方式（文件预置或 API）、allowUiUpdates（由 Grafana 的 provisioned 标志推断）、文件夹；panel 的增减、标题、类型、查询、所在行、其他字段；变量（选中的值不比） | `diff_panel1.py` 内置的两张看板（摘要 9129a17b），即 v3 包加 11-11 校准、`update_panel1.py` 的四处改动，再加 11-12 并入的 P/821 标题。按集群派生的值（看板 uid、标题末尾的「（集群名）」、datasource 变量的 regex 和 current）不算差异。现场如果还是四处改动之前的状态，只写一行说明，改动不逐条列出 |
| 其他相关看板 | uid 以 vllm-pd 开头、标题含「面板一」或「vLLM 服务侧监控面板」、或在「vLLM P/D」文件夹里的看板 | 没有 |
| 数据源 | 名字、uid、类型 | Prometheus，uid prometheus |
| provider 文件 | 加 `--provisioning-dir` 时才比 | v3 包的 `vllm-pd.yaml` |
| Prometheus | 版本；global 和其他配置段；每个 job 的抓取参数、服务发现方式、label 键、role 取值、relabel 规则；加载的规则 | `gen_prometheus_yml.py` 对示例 inventory 的输出经 Prometheus 3.11.2 加载后的配置。目标数量随 inventory 变化，只报数量 |
| 抓取目标 | 每个 job 有几个 up、几个 down，down 的报错 | 全部 up |

「现场 label」一节列出：
- vLLM、NPU、Mooncake、主机四类指标各自的序列数、label 名，以及缺 model_name、pd_instance、role 的序列数；
- model_name、pd_instance、role、job、node、api_server、a_order、engine 的取值和每个值的序列数；
- 名字像集群的 label（cluster、region、zone、env 等）和 external_labels，单独列一行。

退出码：0 表示各部分都读到了；1 表示有部分读不到，其余部分照常输出；2 表示用法错误。

## 本机导出 panel1-export/（格式第 1 版）

目录权限是 700，文件权限是 600。

| 文件 | 内容 |
|---|---|
| `export.json` | 结构化的全部内容，字段见下表 |
| `prometheus.yml` | `--prometheus-yml` 指定的文件原文。password、bearer_token、credentials、client_secret、api_key、token 这几种字段的值换成 `<secret>`；文件里没有这类字段时，副本与原文逐字节相同 |
| `prometheus.effective.yml` | `/api/v1/status/config` 返回的生效配置原文。密码由 Prometheus 显示为 `<secret>` |
| `rules/<序号>-<序号>-<文件名>` | `rule_files` 指向的规则文件，原样复制 |
| `inventory.json` | `--inventory` 指定的文件，原样复制 |
| `grafana/dashboards/<uid>.json` | 从 Grafana API 读到的两张看板 |
| `grafana/provisioning/…` | `--provisioning-dir` 下的看板 provider 文件，原样复制。数据源的预置文件不复制，因为里面可能有密码 |
| `panel1-diff.txt` | 与 `diff_panel1.py` 结果文件格式相同的差异，不打码；`diff_panel1.py --apply` 可以据此还原现场的两张看板 |

`export.json` 的字段如下。之后 `gen_prometheus_yml.py --from-export` 和 `update_panel1.py clusters` 按这些字段读取。约定：字段只增不改；改名或改含义时，`version` 加 1。取不到的值记为 `null`。

| 字段 | 内容 |
|---|---|
| `format`、`version` | 固定为 `"panel1-export"`、`1` |
| `warning`、`generated` | 不外传的提示；生成时间（UTC） |
| `baseline` | `pack`（v3 包摘要）、`dashboards`（基线看板摘要）、`diff_panel1_sha256` |
| `unreachable` | 读不到的部分：`[{part, reason}]` |
| `files` | 目录里每个文件的相对路径及其 sha256（不含 `export.json` 自身） |
| `prometheus.url`、`prometheus.version` | 地址（不含账号）和版本 |
| `prometheus.flags` | `/api/v1/status/flags` 的原样内容 |
| `prometheus.reload` | `web_enable_lifecycle`（true / false / null）、`config_file`（Prometheus 内的路径）、`how`（重新加载的办法） |
| `prometheus.prometheus_yml` | `source`（本机路径）、`copy`（副本的文件名）、`source_sha256`（原文件的 sha256，可用来判断现场文件之后有没有被改过）、`redacted`（换成 `<secret>` 的处数） |
| `prometheus.config` | `global`、`rule_files`、`sections`（其他配置段，如 storage、runtime、otlp）、`scrape_configs`（每个 job 的生效配置，顺序与现场相同，密码类字段为 `"<secret>"`）。按这些字段写出的 prometheus.yml 能被 Prometheus 直接加载 |
| `prometheus.credentials` | 设了认证的位置：`{path, inline: true}`（值在配置里，没有导出）或 `{path, file}`（引用的文件） |
| `prometheus.rule_files` | `[{pattern, files: [{path, copy}], note}]`；本机找不到时 `note` 写明找过哪些位置 |
| `prometheus.rules` | `/api/v1/rules` 返回的规则组：`[{name, file, interval, rules: [{type, name, query, duration, labels, annotations}]}]` |
| `prometheus.targets` | 按 job 列出全部抓取目标：`{job: [{instance, labels, discovered, scrape_url, health, last_error}]}`。`labels` 是 relabel 之后的标签，`discovered` 是 relabel 之前的标签（含 `__address__`、`__scheme__`、`__metrics_path__`） |
| `prometheus.metric_names`、`prometheus.label_names` | 现场全部指标名和 label 名 |
| `prometheus.reserved_labels` | `{cluster: {used, values: {值: 序列数}}, model_group: {…}}` |
| `prometheus.inventory` | `{source, found, copy}`；没给参数或文件不存在时 `found` 为 false |
| `grafana.url`、`grafana.version` | 地址和版本 |
| `grafana.datasources` | `[{name, uid, type, url, access, isDefault, basicAuth, basicAuthUser, user, jsonData, readOnly, secureJsonFields}]`，其中 `secureJsonFields` 只记设了哪些加密字段的名字，不记值；`datasources_source` 记读取途径（api 或 frontend/settings） |
| `grafana.provisioning` | 加了 `--provisioning-dir` 时才有：`{dir, files: [{path, copy, providers: [{name, type, folder, path, allowUiUpdates, updateIntervalSeconds, disableDeletion, foldersFromFilesStructure}]}]}` |
| `grafana.dashboards` | `[{side, uid, title, version, folder, folderUid, provisioned, provisionedExternalId, managedBy, managerId, sourcePath, host_file, copy, compared_with, user_changes, queries_with_cluster}]`。`host_file` 是看板在本机的文件，找不到时为 null；`compared_with` 为 `"baseline"`，或者 `"before_changes"`（现场更接近四处改动之前的状态时） |
| `…user_changes` | 现场相对于 `compared_with` 的改动：`panels_added`（完整的 panel JSON，`_row` 是所在行的 id）、`panels_removed`、`titles_changed`、`types_changed`、`rows_changed`、`queries_changed`（`[{panel, ref, what, base, site}]`，`base`、`site` 是完整的 target）、`other_fields`（`{panel id: [字段路径]}`）、`dashboard_fields`、`variables_added`（完整定义）、`variables_removed`、`variables_changed`（`[{name, fields, site}]`） |
| `…queries_with_cluster` | 已经用到 cluster 或 model_group 的查询 `[{panel, ref, expr}]` 和变量 `[{variable, query}]` |
| `grafana.diff_file` | `"panel1-diff.txt"` |

## 测试记录（2026-11-12）

测试环境：
- Grafana 12.4.2 和 Prometheus v3.11.2，都在一次性容器里，用完删除；
- 模拟指标：8 个 P 实例、64 个 D 实例、12 台机器，共 497 个抓取目标，用 v3 测试时的模拟导出程序；
- 看板由 v3 包的预置配置加载。

第一版（82 项检查）的测试记录见 ServingBench 提交 08c7d62 里的本文件。本版加了短摘要和目录检查，按一次性脚本处理，只测下面四项和 Python 3.6 基线：

| 测试 | 做法 | 结果 |
|---|---|---|
| 基线 0 差异 | 基线环境，两个目录参数用本机路径 | 短摘要报「差异 0 项」，退出码 0；目录检查认出 provider、数据源两个 yaml，两张看板文件与基线和 Grafana 里加载的都相同，prometheus.yml 与已加载的配置相同 |
| 人为改动 | 沿用上一版的改动（改 P/801 查询、删 D/801、job vllm-prefill 加 cluster、allowUiUpdates 改 true、加带密码的 job、加规则文件），再改数据源 yaml（httpMethod 改 GET，加 basicAuth 密码），并在看板目录多放一个文件 | 共 16 项差异，每处各占短摘要一行（19 行）；每个编号在 `panel1-detail.txt` 和 `export.json` 的 `items` 里都查得到。数据源 yaml 那行写「httpMethod POST→GET；多了 basicAuth…」，密码字段只出现名字 |
| `容器名:路径` | `--grafana-dir t26p-grafana:/etc/grafana --prometheus-dir t26p-prom:/etc/prometheus/cfg`，基线环境 | 退出码 0，差异 0 项；`docker cp` 读到了挂进容器的 provisioning 和看板目录，以及镜像自带的 grafana.ini（没有设置项） |
| 不泄漏 | 每次运行都查短摘要和屏幕输出 | 所有运行的短摘要都不超过 40 行、每行不超过 100 个字符；短摘要、屏幕输出和本机导出里都没有 Grafana 密码、配置里的抓取密码和数据源密码；打码时短摘要里没有 IPv4 地址 |
| Python 3.6 | `python:3.6-slim`，`LANG=C`，基线环境 | 退出码 0，短摘要正文与 Python 3.9 的相同 |

没有测：编号在现场变化后是否稳定；差异多到超过上限时的合并；往返；访问不到的各种情况（沿用上一版，代码没改）。

往返测试只覆盖「按导出写回原样」。「按导出反推 inventory，再用 `gen_prometheus_yml.py` 生成同一份 prometheus.yml」属于更新那一步的 `--from-export`，这一版没有做，也没有测。

没有测过：
- Prometheus 开认证的情况；
- 开了 `--web.enable-lifecycle` 的 Prometheus：只测了没开的情况；
- 认证或 TLS 引用的文件（`password_file`、`ca_file` 等）：只测了写在配置里的密码；
- Grafana 12 以前的版本：取不到 k8s 注解时，分不清「非预置」和「文件预置且 allowUiUpdates=true」，输出会照实写明；
- Git Sync 管理的看板；
- 交互输入密码。

## 已知限制

- 「现场 label」对全部序列做 `count by` 查询，共约 30 个。
- provider 文件沿用 `diag_panel1.py` 的逐行读取，要求每个 provider 的第一项是 `name`。
- prometheus.yml 副本里密码的替换按字段名逐行匹配，覆盖块写法和 `{…}` 写法。其他写法（比如多行字符串里的密码）不会被替换；所以这份导出只留在本机。
- 比对代码和内置看板原样取自 `diff_panel1.py`（sha256 前 16 位 db31ca6a04b5ddbc），JSON 里记有这个值。`diff_panel1.py` 的基线以后如果更新，`probe_panel1.py` 要一并重新生成。
