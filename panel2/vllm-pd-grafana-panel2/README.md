# vLLM P/D 请求时延看板部署包（面板二 v2.1）

装好后，Grafana 里多一个看板「面板二：请求时延拆解（P / D）」：所选时间段 P、D 两侧各段的分布，最慢的 20 个请求，单个请求的拆解、P / D 时间线和逐个事件。任何一个历史请求都能查到：各段的合计、首步值和步数保留 `SUMMARY_TTL_DAYS`（默认 30 天），请求级原始事件保留 `EVENT_TTL_DAYS`（默认 15 天）。

一台监控机可以装几个集群：每个集群一套 ClickHouse、采集器和拉取程序，Grafana 共用，看板按模型放在文件夹里。装一个集群用一条命令 `bin/panel2_cluster.sh add`（「装一个集群（一条命令）」一节），步骤 3–18 是它依次做的事，出错时对照着看；步骤 19 的数据准确性检查另行执行。

数据来自 vLLM 补丁写出的 `[PD_DIAG]` 日志行，`version` 为 2 的格式：三种记录 `api_summary`、`core_summary`、`worker_summary`（「附录 C」）。这一版只认这种格式，更早格式的行由过滤器丢掉并计数。同一个包用于两种环境，差别只在 `deploy.env` 的 `NODE_MODE` 和本文的一节：

| | 内网（`NODE_MODE=mock`） | 模型推理环境（`NODE_MODE=vllm`） |
|---|---|---|
| 节点上写日志的 | mock 写入程序，按当前时间生成请求，一个实例一个进程 | 打了补丁、写 version 2 记录的 vLLM |
| 节点上的三个文件 | mock 写入程序加载 | vLLM 通过 `VLLM_LOGGING_CONFIG_PATH`、`PYTHONPATH` 加载 |
| 写到盘上的 | 同一个过滤器写出的同一种文件 | 同左 |
| 本文对应的部分 | 第一到第四部分 | 第一到第四部分，其中步骤 16 换成「模型推理环境」一节 |

```
每台 P、D 机器                                           监控机（面板一的 Grafana 机器）
  vLLM 或 mock 写入程序                                    拉取程序 → 镜像目录 → 采集器（Vector，容器）→ ClickHouse ← Grafana（已有）
    └ 三个节点文件（监控机下发）：过滤器        ←── SSH + rsync ───     ↑                        ↑
        └ 日志目录/<实例>/<进程>.*.log          默认每 10 秒           Prometheus（已有，发现采集链路的问题）
```

**节点上做什么：** 节点上只有一个目录放三个文件（logging 配置 `pd_diag_logging.json`、过滤模块 `pd_diag_logfilter.py`、映射规则 `segments.json`），由监控机下发。它们在 vLLM 的每个进程里做三件事：请求级记录原样写盘；按步的记录不写盘，在进程里按请求累加成可相加的部分行；只留 TP rank 0。节点不装采集程序，不开新端口，不存密码；拉日志用节点已有的 SSH 和 rsync。

**监控机上做什么：** 配置都在这里：集群清单 `inventory.json`、部署参数 `deploy.env`、映射规则 `segments.json`。采集器的解析规则和 ClickHouse 的表、视图都由 `segments.json` 生成；日志格式变了，改 `segments.json` 再执行 `bin/apply_segments.sh`（见「映射规则」）。

示例清单 `inventory.example.json` 按测试环境写：P 1 台机器、2 个实例（端口 7100、7101，P0、P1），TP8；D 1 台机器、8 个实例（端口 7100–7107，D0–D7），TP2。实例名由 `inventory.json` 里列表的顺序算出来，机器数、实例数和端口都按现场改，比如 P 8 台、每台一个 9000，D 8 台、64 个实例。这一版不采 proxy 的日志：proxy 补丁是第二阶段的事。

## 开始之前

| 机器 | 要有什么 | 不需要什么 |
|---|---|---|
| 监控机（面板一的 Grafana 机器） | 面板一已部署好（Grafana 12.4.2、Prometheus 3.11.2）；docker；ssh 客户端；rsync 3.x；磁盘见「磁盘」一节（按全集群 100 请求/秒估计约 830 GB） | 不需要 compose，不需要上网，不需要自己装 Python |
| 每台 P、D 机器 | 监控机能免密 SSH 登录；rsync 3.x；bash、tar；日志目录所在盘按「磁盘」一节留空间。内网时 Python 3.9 及以上，没有就用包里带的 | 不需要 root，不需要上网，不开新端口 |

**SSH：** 在监控机上，要能不输密码登录每台 P、D 机器。用哪个用户都可以，写进 `deploy.env` 的 `SSH_USER`。还没配时：

```
ssh-keygen -t ed25519            # 已经有密钥就跳过，一路回车
ssh-copy-id <用户>@<地址>         # 每台机器一次，这一步要输入密码
```

**Python：** 监控机和节点上的脚本要 Python 3.9 及以上。先用 `deploy.env` 里 `PYTHON` 写的，再找 `PATH` 里的 `python3`、`python3.x`。都没有时，自动用离线包里带的 Python 3.12，解到 `TIMING_HOME` 下，不影响机器上别的东西。过滤模块在 vLLM 的 Python 里运行，同样要 3.9 及以上（vLLM 0.32 本身要 3.10 以上）。

**对 P、D 机器上其他东西的影响：**

| 项目 | 内网（mock） | 模型推理环境（vLLM） |
|---|---|---|
| 目录 | `NODE_CONF_DIR`（三个文件）、`NODE_LOG_DIR`（日志）、`~/vllm-timing-node` 和 `TIMING_HOME`（mock 和它的状态） | `NODE_CONF_DIR`、`NODE_LOG_DIR`，各挂进 vLLM 容器 |
| 进程 | 每个实例一个 mock 进程（`nice 19`）；拉取时短暂多一个 rsync 和 sshd | 不新增进程，过滤器在 vLLM 自己的进程里 |
| CPU 和内存 | 见下面「资源实测」 | 过滤器每行的开销见「过滤器的开销」 |
| 磁盘 | 过滤器按 `segments.json` 的 `line.keep_hours`（默认 12 小时）删旧文件 | 同左 |
| 端口 | 不开新端口 | 同左 |
| 停止和清理 | `bin/nodes.sh clean` | vLLM 去掉两个环境变量后重启；`bin/nodes.sh clean` 只删 `~/vllm-timing-node` |

**对监控机的影响：**

| 项目 | 说明 |
|---|---|
| Grafana | 第一个集群装插件时重启一次（测试里页面中断 1–2 秒，Mac 上的容器，上一版实测），以后的集群不再重启。面板一的看板 JSON 不动，新看板放在 `MODEL_GROUP` 文件夹里 |
| Prometheus | 在 `prometheus.yml` 里加一段带标记的抓取任务和规则（第一次会备份成 `prometheus.yml.before-panel2`），热加载，不重启 |
| 新增的容器 | ClickHouse 和采集器 |
| 新增的进程 | 每个集群一个拉取程序（`bin/puller.sh`），以及它每轮调用的 rsync 和 ssh。端口 `PULLER_METRICS_PORT`（端口基数 +1），给 Prometheus 看状态 |
| CPU 和内存 | 见下面「资源实测」 |
| 镜像目录 | 节点上删掉的文件，镜像里也跟着删，所以占用等于各节点保留的日志之和，见「磁盘」 |

**资源实测：** 在实验室里测：Mac 上的 Docker 容器，aarch64；拓扑缩小为 2 个 P 实例、5 个 D 实例，每台机器一个容器（另有一台 x86_64 机器在 Rosetta 转译下运行，不列入下表）。机器和监控机容器每 5 秒采一次、测 180 秒，mock 进程按 `/proc` 的 CPU 时间测 120 秒。CPU 以一核为 100%。

| 位置 | 进程 | 全集群 7.4 请求/秒（`MOCK_RATE=8`） | 全集群约 94 请求/秒（`MOCK_RATE=100`，见表下） |
|---|---|---|---|
| P 机器 | 一个 P 实例的 mock 进程（每实例 3.7 请求/秒） | CPU 1%，内存 28 MiB | 没有单独测 |
| D 机器 | 一个 D 实例的 mock 进程（每实例 1.5 请求/秒） | CPU 4.5%，内存 25 MiB | 没有单独测 |
| P 机器 | 整台机器的容器：1 个 P 实例，加拉取时的 rsync、sshd | CPU 中位数 0.8–1.2%，最高 2.1%；内存 66–81 MiB | CPU 中位数 7.7–8.3%，最高 13%；内存 78–91 MiB |
| D 机器 | 整台机器的容器：2 个 D 实例，加拉取时的 rsync、sshd | CPU 中位数 9.2–9.6%，最高 12%；内存 78–85 MiB | CPU 中位数 12–18%，最高 34%；内存 138–140 MiB |
| 监控机 | ClickHouse | CPU 7.5%；内存 1292 MiB，最高 1420 MiB | CPU 21%；内存 1453 MiB，最高 1648 MiB |
| 监控机 | 采集器 | CPU 8.4%；内存 399 MiB，最高 411 MiB | CPU 83%；内存 580 MiB，最高 677 MiB |
| 监控机 | 拉取程序（含它调用的 rsync）和 ssh 主连接 | CPU 0.6%；内存 52 MiB | CPU 2.4%；内存 55 MiB |

「约 94 请求/秒」一列把 `MOCK_RATE` 调到 100，P 侧实测 94 请求/秒。实验室只有 5 个 D 实例，每个约 19 请求/秒（完整拓扑每个 D 实例 1.56 请求/秒），并发和每步时间都比完整拓扑大得多，测量的 3 分钟里多数 D 请求还没有结束。所以这一列只说明监控机在接近 100 请求/秒的写入量下的用量，不是完整拓扑的稳态。

表头的两个 `MOCK_RATE` 是测量时的设置，不是推荐值。按这一版的保护线（每个 D 实例不超过 1.5 请求/秒，见步骤 5 的 `MOCK_RATE`），实验室 5 个 D 实例时，`MOCK_RATE=8` 每个 D 1.6 请求/秒，`MOCK_RATE=100` 每个 D 20 请求/秒，升级安全检查都会报 FAIL。

【估计】完整拓扑下每个 D 实例 1.56 请求/秒，和「7.4 请求/秒」一列的每实例 1.5 相当；一台 D 机器 16 个实例，mock 约 16 × 4.5% ≈ 0.7 核、16 × 25 MiB ≈ 0.4 GiB，另有每台机器一个约 23 MiB 的父进程和拉取时的 rsync。每个 P 实例 12.5 请求/秒，按「7.4 请求/秒」一列每请求/秒约 0.27% 算，约 3.4%。

**从写日志到看板能查到：** 实验室实测：在一台机器的日志目录里新写一个文件，带一行请求级事件，计时到 ClickHouse 查得到这一行，测 20 次。全集群 7.4 请求/秒时中位数 10.6 秒、p90 14.7 秒、最长 15.7 秒；约 94 请求/秒时中位数 10.2 秒、p90 14.7 秒、最长 16.4 秒。时间主要是拉取间隔 `PULL_INTERVAL_S`（默认 10 秒）加采集器写入 ClickHouse 的攒批（最多 5 秒）。按步累加的段由过滤器每 `line.flush_seconds`（默认 5 秒）写一次累计行，在途请求的这些段再晚最多 5 秒。

## 第一部分：把文件拷到监控机

### 步骤 1：看监控机的架构，拷文件

**先在监控机上：**

```bash
uname -m
```

预期：`aarch64` 或 `x86_64`。

**然后把这 4 个文件拷到监控机的主目录。** 用 scp 或 U 盘都可以，下面的 `<架构>` 换成上面看到的值：

```
vllm-pd-grafana-panel2.tar.gz
vllm-pd-grafana-panel2.tar.gz.sha256
vllm-pd-grafana-panel2-offline-<架构>.tar.gz
vllm-pd-grafana-panel2-offline-<架构>.tar.gz.sha256
```

只要这一份离线包。P、D 和监控机架构不同也一样，里面两种架构的 Python 都有。

**这台监控机装过 tar `a87f4d74` 或 `59604f30` 的面板二时，先把旧目录改名，再做步骤 2。** 卸载要用旧目录里的 `deploy.env` 和 `inventory.json`，改名以后不会和新集群的目录混淆。装着这一版之前的那一版（目录 `~/vllm-pd-grafana-panel2`）时不改名，按「已经装好的那一套：重装成一个集群」做。装的是 tar `a87f4d74`（ServingBench 提交 `085a299` 那一版）时：

```bash
mv ~/vllm-pd-grafana-panel2 ~/vllm-pd-grafana-panel2-a87f4d74
```

装的是更早的 tar `59604f30` 时：

```bash
mv ~/vllm-pd-grafana-panel2 ~/vllm-pd-grafana-panel2-59604f30
```

步骤 2 做完后，按「卸载上一版（a87f4d74）」或「卸载旧版（59604f30）」一节卸掉，再按「装一个集群（一条命令）」继续。没有装过的，跳过这一段和那两节。

### 步骤 2：解开并校验

**在监控机上。**

```bash
cd ~ && sha256sum -c vllm-pd-grafana-panel2.tar.gz.sha256 vllm-pd-grafana-panel2-offline-$(uname -m).tar.gz.sha256 && mkdir -p vllm-pd-grafana-panel2-pack && tar xzf vllm-pd-grafana-panel2.tar.gz -C vllm-pd-grafana-panel2-pack && tar xzf vllm-pd-grafana-panel2-offline-$(uname -m).tar.gz -C vllm-pd-grafana-panel2-pack/vllm-pd-grafana-panel2 && cd vllm-pd-grafana-panel2-pack/vllm-pd-grafana-panel2 && sha256sum -c SHA256SUMS --quiet && (cd offline && sha256sum -c SHA256SUMS --quiet) && echo 校验通过
```

预期：前两行以 `OK` 结尾，最后一行是 `校验通过`。

部署包解在 `~/vllm-pd-grafana-panel2-pack/vllm-pd-grafana-panel2`。这个目录只放部署包，不在这里部署：每个集群由 `bin/panel2_cluster.sh add` 拷一份到 `<总目录>/vllm-pd-grafana-panel2/<集群名>`（总目录见「装一个集群（一条命令）」）。分开放是为了不盖到已经装好的 `~/vllm-pd-grafana-panel2`。

不符合时：有 `FAILED` 就是文件没传完，重新拷一次。没有 `sha256sum` 命令时，换成 `shasum -a 256`。

## 卸载上一版（a87f4d74）

只有这台监控机装过 tar `a87f4d74` 时才做，在步骤 2 之后、装新集群之前。都在监控机上、部署包目录里执行：

```bash
cd ~/vllm-pd-grafana-panel2-pack/vllm-pd-grafana-panel2
```

用的是同一个卸载脚本 `bin/uninstall_old.sh`，它从目录里的文件认出是哪一版（`a87f4d74` 有 `mock/pd032_mock.py`），按旧目录里的 `deploy.env` 和 `inventory.json` 找东西。`a87f4d74` 装过的东西和下一节「旧版装过的东西」那张表相同，只有节点上不同：mock 的日志在 `NODE_LOG_DIR`（默认 `~/vllm-pd-diag/logs`），三个节点文件在 `NODE_CONF_DIR`（默认 `~/vllm-pd-diag/conf`）。

**数据可以留着。** 这一版和 `a87f4d74` 的库名、表名相同；新版的步骤 9 给已有的表补上新增的列，旧数据照常能查。要从空库开始，做步骤 A3。

**`NODE_MODE=vllm`、vLLM 已经按 `a87f4d74` 加了三个环境变量的：** 先在启动脚本里去掉这三个（`VLLM_LOGGING_CONFIG_PATH`、`PYTHONPATH` 里的那一项、`PD_DIAG_INSTANCE`），重启 vLLM，再做 A2；装好新版后按步骤 V4 加两行。内网（`NODE_MODE=mock`）没有这一步。

### 步骤 A1：看上一版装了什么

```bash
bin/uninstall_old.sh plan ~/vllm-pd-grafana-panel2-a87f4d74
```

预期：分「监控机」「Grafana、Prometheus」「数据」「节点」「保留，不动」五块，最后一行是 `面板一现在的状态记在 …`。这一步只查不改，记下面板一现在的看板（含版本号）、数据源和 Prometheus 目标，A4 拿它比较。

### 步骤 A2：卸载（数据留着）

```bash
bin/uninstall_old.sh remove ~/vllm-pd-grafana-panel2-a87f4d74
```

预期：每一项一行 `OK`，最后两行是 `RESULT: OK` 和 `数据还在：…`。依次做：停拉取程序和 SSH 连接复用；每台节点停 mock、删 `~/vllm-timing-node`，内网再删 `NODE_CONF_DIR`；删 Grafana 里面板二的看板和数据源；去掉 `prometheus.yml` 里面板二的两段、删规则文件、热加载 Prometheus；先停再删采集器和 ClickHouse 容器。面板一不动。

不符合时：和 U2 相同。

### 步骤 A3：删数据（可选，不可恢复）

**这一步删除的东西不能恢复**：ClickHouse 里面板二的全部数据（库 `vllm_pd`）、采集器的状态、监控机上的拉取镜像、每台节点 `NODE_LOG_DIR` 里过滤器和 mock 写的文件（只删 `<实例目录>/api|engine|worker|pool.*.log`，然后删空了的目录）。要留旧数据就跳过这一步；要先打包，加 `--backup <目录>`，用法和 U3 相同。

```bash
bin/uninstall_old.sh purge-data ~/vllm-pd-grafana-panel2-a87f4d74
```

预期：每一项一行 `OK`，最后一行是 `RESULT: OK`。

### 步骤 A4：检查清单

```bash
bin/uninstall_old.sh check ~/vllm-pd-grafana-panel2-a87f4d74
```

预期：最后一行是 `RESULT: OK`。逐项和 U4 那张表相同；节点一项查的是：没有 `mock/pd032_mock.py` 进程，没有 `~/vllm-timing-node`，内网没有 `NODE_CONF_DIR`；`NODE_LOG_DIR` 里还有文件时写 `INFO`（没有做 A3 时是这样）。旧数据没删时「旧数据」一行也是 `INFO`。面板一的看板、数据源、Prometheus 目标和 A1 记下的相同。

### 步骤 A5：删旧目录

A4 通过后：

```bash
rm -rf ~/vllm-pd-grafana-panel2-a87f4d74
```

然后按「装一个集群（一条命令）」装新版。

## 卸载旧版（59604f30）

只有这台监控机装过上一版面板二（tar `59604f30`）时才做，在步骤 2 之后、装新集群之前。都在监控机上、部署包目录里执行：

```bash
cd ~/vllm-pd-grafana-panel2-pack/vllm-pd-grafana-panel2
```

**为什么不能直接装在旧版上面：** 两版的容器名、ClickHouse 库名 `vllm_pd`、数据卷名和 Grafana 看板的 UID 都相同，表结构不同；旧版的拉取程序和节点上的 mock 不停的话，还会继续按旧格式写入。新版的 `bin/clickhouse_up.sh` 看到库里还有旧版的表会停下，不会把两种表混在一起。

**卸载脚本 `bin/uninstall_old.sh` 只读旧目录里的 `deploy.env` 和 `inventory.json`**，按旧版当时的设置（容器名、端口、目录、Grafana 和 Prometheus 的地址、SSH 用户、节点地址）找东西，分四步：`plan` 只查不改，`remove` 卸载但数据留着，`purge-data` 删数据，`check` 是卸载完的检查清单。

### 旧版装过的东西，哪一步删

| 在哪里 | 旧版装的东西 | 怎么处理 |
|---|---|---|
| 监控机 | 容器 `vllm-pd-vector`（采集器）、`vllm-pd-clickhouse`（以旧 `deploy.env` 的 `CENTRAL_CONTAINER`、`CLICKHOUSE_CONTAINER` 为准） | U2 删除：先停采集器、等它写完，再 `docker rm -fv`。`-v` 只删匿名卷，数据卷留到 U3 |
| 监控机 | 端口：ClickHouse HTTP `8123`（`CLICKHOUSE_HTTP_PORT`）、拉取程序状态 `9598`（`PULLER_METRICS_PORT`） | U2 之后空出来 |
| 监控机 | 进程：拉取程序（`bin/puller.sh` 起的循环和 `puller.py`）、它调用的 rsync，以及 SSH 连接复用的主连接 | U2 停止 |
| 监控机 | 目录：临时目录下的 `vllm-ssh-*`（SSH 连接复用）；`TIMING_HOME/puller`（拉取程序的进程文件、输出和状态） | 前者 U2 删除，后者 U3 删除 |
| 监控机 | 数据：数据卷 `vllm-pd-clickhouse-data`、`vllm-pd-clickhouse-logs`、`vllm-pd-vector-data`；`CLICKHOUSE_DATA_DIR` 有值时是它下面的 `data`、`logs`、`vector`。库 `vllm_pd` 的表和视图、用户 `vllm_writer`、`vllm_reader` 都在里面 | U3 删除，**不可恢复**；`--backup` 先打包 ClickHouse 的数据 |
| 监控机 | 拉取镜像：`PULL_DIR`（默认 `CLICKHOUSE_DATA_DIR/pull` 或 `TIMING_HOME/pull`） | U3 删除，**不可恢复**；只删带旧版标记文件 `.made-by-vllm-timing` 的目录 |
| 监控机 | 旧版目录（改名后的 `~/vllm-pd-grafana-panel2-59604f30`），里面有旧 `deploy.env`（含密码）、离线文件 | U5 删除 |
| 监控机 | `TIMING_HOME/python`：机器上没有合适的 Python 时解出来的 | 保留：新版用同一个 |
| 监控机 | 镜像 `clickhouse/clickhouse-server:26.8.36.3`、`timberio/vector:0.61.0-debian` | 保留：新版用同样的两个。删不删由现场决定，见本节末尾 |
| Grafana（面板一的实例） | 看板「面板二」（UID `vllm-pd-request`）、数据源「vLLM ClickHouse」（UID `vllm-pd-clickhouse`） | U2 删除，只删这两个 |
| Grafana | ClickHouse 插件 `grafana-clickhouse-datasource` | 保留：新版要用，删除要重启 Grafana，面板一的页面会中断一下。完全不用面板二时见本节末尾 |
| Grafana | 目录「vLLM P/D」 | 保留：面板一的看板也在里面 |
| Prometheus（面板一的实例） | `prometheus.yml` 里两段带「面板二」标记的内容：抓取任务 `vllm-timing-puller`、`vllm-timing-central` 和 `rule_files` | U2 删除，只删标记之间的部分，然后热加载，不重启 |
| Prometheus | 容器里的 `/prometheus/vllm-timing-rules.yml`（6 条告警） | U2 删除 |
| Prometheus | `prometheus.yml.before-panel2`：第一次装面板二之前的备份 | 保留：新版沿用，它是没有面板二时的原样 |
| 面板一的其他部分 | docker 网络 `vllm-pd-monitor`，面板一的容器、配置、看板、数据源 | 不动 |
| 每台节点（P、D、proxy） | mock 进程 | U2 停止 |
| 每台节点 | `~/vllm-timing-node`（mock 程序） | U2 删除 |
| 每台节点 | `TIMING_HOME`（默认 `~/vllm-timing`）：mock 日志、自带的 Python | U3 删除，**不可恢复**；只删带旧版标记文件 `.vllm-timing-home` 的目录 |
| SSH | 监控机上的密钥、节点 `~/.ssh/authorized_keys` 里监控机的公钥、监控机 `~/.ssh/known_hosts` 里各节点的记录 | 保留：新版用同样的 SSH 登录。完全不用时见本节末尾 |

### 步骤 U1：看旧版装了什么

```bash
bin/uninstall_old.sh plan ~/vllm-pd-grafana-panel2-59604f30
```

预期：分「监控机」「Grafana、Prometheus」「数据」「节点」「保留，不动」五块，逐项写有没有，最后一行是 `面板一现在的状态记在 …`。这一步只查不改；它把面板一现在的看板（含版本号）、数据源和 Prometheus 目标记下来，U4 拿它比较。

不符合时：某台节点写 `SSH 连不上`，先修好 SSH（旧版用的是旧 `deploy.env` 的 `SSH_USER`、`SSH_OPTS`），否则那台机器上的 mock 停不掉。Grafana 一项写 `查不了` 时，检查旧 `deploy.env` 的 `GRAFANA_URL`、`GRAFANA_PASSWORD`。

### 步骤 U2：卸载（数据留着）

```bash
bin/uninstall_old.sh remove ~/vllm-pd-grafana-panel2-59604f30
```

预期：每一项一行 `OK`，最后两行是 `RESULT: OK` 和 `数据还在：…`。依次做：停拉取程序和 SSH 连接复用；每台节点停 mock、删 `~/vllm-timing-node`；删 Grafana 里面板二的看板和数据源；去掉 `prometheus.yml` 里面板二的两段、删规则文件、热加载 Prometheus；先停再删采集器和 ClickHouse 容器。

不符合时：`Prometheus 重新加载配置失败` 时，面板一的 Prometheus 没有开 `--web.enable-lifecycle`，在没人看看板的时候执行 `docker restart <PROMETHEUS_CONTAINER 的值>`。其他 `FAIL` 行写了原因，修好后再执行一次，已经删掉的会显示「本来就没有」。

### 步骤 U3：删数据（不可恢复）

**这一步删除的东西不能恢复**：ClickHouse 里旧版的全部数据（库 `vllm_pd`）、采集器的状态、监控机上的拉取镜像、每台节点上的 mock 日志。装新版之前这一步必须做：新版用同一个库名和同样的数据卷或目录。

**要保留旧数据的**，加 `--backup` 和一个空间够的目录，脚本先把 ClickHouse 的数据打包成 `.tar.gz`（附 `.sha256`）放进去，打包成功才删除：

```bash
bin/uninstall_old.sh purge-data ~/vllm-pd-grafana-panel2-59604f30 --backup /data/vllm-pd-backup-59604f30
```

包里是 ClickHouse 数据目录的原样。要再看旧数据时，把 `vllm-pd-clickhouse-data.tar.gz`（`CLICKHOUSE_DATA_DIR` 有值时是 `clickhouse-data.tar.gz`）解到一个空目录，挂到同一个镜像 `clickhouse/clickhouse-server:26.8.36.3` 的容器的 `/var/lib/clickhouse` 上启动，库 `vllm_pd` 和旧版的表都在里面。拉取镜像和节点上的日志是 mock 写的，不打包。

不需要保留时：

```bash
bin/uninstall_old.sh purge-data ~/vllm-pd-grafana-panel2-59604f30
```

预期：每一项一行 `OK`（数据卷或目录、拉取镜像、拉取程序的状态目录、每台节点的 `TIMING_HOME`），最后一行是 `RESULT: OK`。

不符合时：`容器 … 还在：先执行 remove` 就回到 U2。`不是旧版建的目录，没有删` 说明那个目录里没有旧版的标记文件，可能放了别的东西，自己确认后手工处理。

### 步骤 U4：检查清单

```bash
bin/uninstall_old.sh check ~/vllm-pd-grafana-panel2-59604f30
```

预期：最后一行是 `RESULT: OK`。逐项确认：

| 项 | 怎么查 |
|---|---|
| 没有旧版的容器 | `docker ps -a` 里没有这两个名字 |
| 没有旧版的进程 | 拉取程序的进程文件都不在，也没有运行旧版 `scripts/puller.py` 的进程；没有 SSH 连接复用的主连接和 `vllm-ssh-*` 目录 |
| 端口空出来了 | `CLICKHOUSE_HTTP_PORT`、`PULLER_METRICS_PORT` 没有程序在监听 |
| 旧数据 | 已删除；没有做 U3 时这一行是 `INFO`，写出还在的位置 |
| Grafana | 面板二的看板和数据源返回 404；插件写「保留」 |
| Prometheus | `prometheus.yml` 里没有面板二的标记，容器里没有规则文件，抓取任务里没有 `vllm-timing-*`，告警规则里没有 `vllm-timing` |
| 面板一照常工作 | 和 U1 记下的比：面板一的看板个数、版本号不变，数据源不变，Prometheus 抓面板一的目标一个不少，之前是 up 的现在也是 up；Grafana 和 Prometheus 的健康检查正常 |
| 每台节点 | 没有 mock 进程，没有 `~/vllm-timing-node`；做过 U3 时也没有 `TIMING_HOME` |

不符合时：按 `FAIL` 那一行处理后，再执行一次 U2 或 U3，最后再执行一次 U4。

### 步骤 U5：删旧目录

U4 通过后：

```bash
rm -rf ~/vllm-pd-grafana-panel2-59604f30
```

旧目录里有旧 `deploy.env` 里的密码，留着没有用处。然后按「装一个集群（一条命令）」装新版。

### 完全不再用面板二时

上面保留的几样新版也要用。不再装新版、要把面板二的痕迹全部清掉时，在 U2 加 `--remove-plugin`（删 ClickHouse 插件，Grafana 重启一次，面板一的页面中断几秒），再按需执行：

| 要删的 | 命令 |
|---|---|
| 两个镜像 | `docker rmi clickhouse/clickhouse-server:26.8.36.3 timberio/vector:0.61.0-debian`（旧 `deploy.env` 里 `CLICKHOUSE_IMAGE`、`CENTRAL_IMAGE` 改过时写改过的） |
| 自带的 Python | `rm -rf ~/vllm-timing`（`TIMING_HOME` 改过时写改过的路径） |
| 监控机 `known_hosts` 里各节点的记录 | 每个节点地址执行一次 `ssh-keygen -R <地址>` |
| 节点 `authorized_keys` 里监控机的公钥 | 在每台节点上，把 `~/.ssh/authorized_keys` 里和监控机 `~/.ssh/id_ed25519.pub` 相同的那一行删掉。这台监控机还用这把密钥登录别的机器时不要删 |

镜像要不要删由现场决定：删了以后再装面板二要重新从离线包导入（步骤 3）。

## 装一个集群（一条命令）

一台监控机可以装几个集群的面板二。每个集群一套 ClickHouse、采集器和拉取程序，指标写进本集群自己的 Prometheus（面板一的那一套）；Grafana 共用一个，看板按模型放在文件夹里。在部署包目录里执行下面一条命令，就做完第二到第四部分的步骤 3–18；步骤 19 的数据准确性检查不在其中，要看时另行执行：

```bash
cd ~/vllm-pd-grafana-panel2-pack/vllm-pd-grafana-panel2
bin/panel2_cluster.sh add <集群名> --model-group <模型文件夹> --inventory <清单>
```

| 参数 | 写什么 |
|---|---|
| `<集群名>` | 小写字母、数字和 `-`，比如 `glm53-1`。这台监控机上不能重复 |
| `--model-group` | Grafana 里的文件夹名，比如 `GLM-5.3`。同一模型的几个集群放进同一个文件夹 |
| `--inventory` | 这个集群的 `inventory.json`，写法见步骤 4。面板一的清单可以直接用，面板一多出来的字段这里不读 |
| `--mode`（可选） | 内网写 `mock`（默认），模型推理环境写 `vllm` |
| `--port-base`（可选） | 端口基数。不写时自动分配：从 18100 起每个集群 10 个端口，取第一段没有登记、在这台机器上也空闲的。ClickHouse 用基数，拉取程序用基数 +1，本集群面板一的 Prometheus 用基数 +2 |
| `--root`（可选） | 总目录：本集群在监控机上的目录都放在它下面（见下面的「目录」）。第一次装时写一次，比如 `--root /data/grafana`，记进注册表；以后的 `add` 不写就用注册表里的；注册表里也没有时是 `$HOME`。`$HOME` 所在的盘小时，第一次就写到大盘上 |
| `--dir`（可选） | 只给这个集群指定面板二目录，优先于总目录 |
| `--env-from`（可选） | 共用的 `GRAFANA_URL`、`GRAFANA_USER`、`GRAFANA_PASSWORD`、`GRAFANA_CONTAINER`、`DOCKER_NETWORK`、`MONITOR_HOST` 从哪一份 `deploy.env` 读。不写时读最早登记的那个集群的；`DOCKER_NETWORK` 仍为空时，取 Grafana 容器所在的 docker 网络 |
| `--set 参数=值`（可选，可写多次） | 写进 `deploy.env` 的其他参数，比如 `--set SSH_USER=ops --set CLICKHOUSE_MAX_SERVER_MEMORY_MB=1500` |
| `--force`（可选） | 内容不变的步骤也照做（见下面的「内容不变的步骤跳过」）。只在怀疑现场和比对结果不一致、要强制重做时用 |
| `--ignore-check`（可选） | 内网最后一步的升级安全检查（步骤 18）有 `FAIL` 时只报告，不停下：`FAIL` 行和 `步骤 18 有 FAIL，按 --ignore-check 继续` 留在屏幕上，命令照常做完 |

它依次做：

1. 建目录 `<总目录>/vllm-pd-grafana-panel2/<集群名>`（给了 `--dir` 时是它），把部署包拷进去；
2. 按 `deploy.env.example` 写 `deploy.env`：三个 ClickHouse 密码随机生成，总目录写成 `VLLM_PD_ROOT`（以后注册表里的总目录改了，这个集群也不挪），文件权限 600；
3. 查注册表、端口和清单（见「同一台监控机装几个集群」），登记这个集群；
4. 在这个目录里执行步骤 3、6、7、9–17；内网时再执行步骤 18（升级安全检查），最多等 2 分钟过滤器的自检行；
5. 打印每台集群机器上的目录、挂载和环境变量（`bin/nodes.sh paths` 的输出）。

预期：最后一行是 `RESULT: OK（集群 … 装好了：看板在 Grafana 的「…」文件夹里）`。内网时它上面一行是 `数据准不准另看：bin/panel2_cluster.sh quality <集群名>（不影响升级）`，即步骤 19。

**内容不变的步骤跳过。** 下面五样（看板和数据源都在步骤 12），命令先把这一次要写的内容和现场正在用的比较：相同就不做，输出 `跳过（内容不变）：…`；不同时照做，输出 `已更新：…`。

| 步骤 | 比较的内容 | 内容不变时不做的事 |
|---|---|---|
| 9 | 表和视图：每张表的列名和类型、TTL 子句、视图定义 | 不执行 DDL（加列、改 TTL、重建视图） |
| 10 | 采集器：`central.yaml`，容器的镜像和启动参数 | 不重建采集器容器 |
| 12 | 看板 JSON、数据源 | 不重新导入看板，不改写数据源 |
| 13 | Prometheus：`prometheus.yml` 里面板二那一段和告警规则文件 | 不写文件，不重新加载 Prometheus |

所以内容没有变化时重跑 `add`，不会重新加载 Prometheus（它和面板一共用），也不会重建采集器；面板二只有步骤 17 重启拉取程序的几秒没有新数据。要全部照做时加 `--force`。

**出错时。** 哪一步没过，命令就停在那一步，并给出步骤号。按下表找到那一步的说明处理，改好后**重跑同一条命令**：已经写好的 `deploy.env` 沿用，已经建好的容器、看板和抓取任务再检查一遍，内容不变的跳过，不会重复建。要改参数时改集群目录里的 `deploy.env`，再重跑。

| 停下时提示的 | 内容 | 在集群目录里单独执行 |
|---|---|---|
| 查注册表、端口和清单 | 名字、端口、目录或集群机器和别的集群重复，或者端口被别的程序占用。FAIL 行写了是哪一项、和哪个集群重复 | `bin/check_cpu.sh`（最先做这一项） |
| 步骤 3 | 导入镜像，已有就跳过 | `docker load -i offline/clickhouse-image.tar` 等 |
| 步骤 6 | 预检监控机 | `bin/check_cpu.sh` |
| 步骤 7 | 预检所有节点 | `bin/nodes.sh check` |
| 步骤 9 | 启动 ClickHouse；表和视图内容不变时不执行 DDL | `bin/clickhouse_up.sh` |
| 步骤 10 | 启动采集器；配置、镜像和启动参数不变时不重建 | `bin/central_up.sh` |
| 步骤 11 | 给 Grafana 装 ClickHouse 插件；已经装了就跳过，不重启 Grafana | `bin/grafana_plugin.sh` |
| 步骤 12 | 导入数据源和看板；内容不变时跳过 | `bin/grafana_board.sh` |
| 步骤 13 | 本集群的 Prometheus 加抓取任务和告警规则；那一段和规则文件不变时不写、不重新加载 | `bin/prometheus_job.sh` |
| 步骤 14、15 | 下发三个节点文件、节点自检 | `bin/nodes.sh push`、`bin/nodes.sh selfcheck` |
| 步骤 16 | 启动 mock，只在内网 | `bin/nodes.sh start` |
| 步骤 17 | 启动拉取程序 | `bin/puller.sh start` |
| 步骤 18 | 升级安全检查，只在内网；模型推理环境在 vLLM 加好挂载和环境变量以后单独执行 | `bin/panel2_cluster.sh check <集群名>` |

**模型推理环境（`--mode vllm`）。** 命令做到步骤 17，打印每台机器要加的挂载和环境变量后结束。按「模型推理环境」一节的步骤 V4 给每个实例加上、重启 vLLM 以后，执行 `bin/panel2_cluster.sh check <集群名>`（步骤 18）；数据准不准用 `bin/panel2_cluster.sh quality <集群名>` 另看（步骤 19）。

**其他命令。** 在部署包目录或任一集群目录里执行：

| 命令 | 做什么 |
|---|---|
| `bin/panel2_cluster.sh list` | 列出这台监控机上登记的集群：模型文件夹、端口基数、看板 UID、目录 |
| `bin/panel2_cluster.sh check <集群名> [--wait <秒>]` | 升级安全检查（步骤 18）：九项，只读，不改任何东西。`--wait` 时过滤器和链路通两项没过的，每 10 秒再查一次，最多等这么多秒 |
| `bin/panel2_cluster.sh quality <集群名>` | 数据准确性检查（步骤 19）：执行 `bin/verify.sh` 和 `bin/postdeploy_check.sh`，只报告，不影响 `add`、`check` 和更新脚本 |
| `bin/panel2_cluster.sh remove <集群名> [--backup <目录>]` | 卸载这个集群：依次执行 `bin/uninstall_old.sh` 的 plan、remove、purge-data、check，从注册表删除，再删集群目录。**数据一起删除，不可恢复**；要留数据加 `--backup <目录>`，先把 ClickHouse 的数据打包到那里。别的集群不受影响；最后一个集群卸载时才删 Grafana 的 ClickHouse 插件 |

### 已经装好的那一套：重装成一个集群

这一版之前装好的面板二（目录 `~/vllm-pd-grafana-panel2`），按下面的顺序换成按集群装的这一版：先换面板一，再卸掉旧的面板二，最后用一条命令装回来。重装以后容器、数据源和看板都换成带集群名的名字，看板进模型文件夹，功能和原来相同。旧的 ClickHouse 数据不带过来。

1. **先按面板一的教程把面板一全量替换完。** 面板一的替换步骤原样保留 `prometheus.yml` 里面板二的标记段；去掉了也没关系：第 2 步删掉旧的那段，第 3 步加上新的。面板一登记集群时用的集群名，第 3 步用同一个。

2. **用新包卸掉旧的面板二。** 在部署包目录里依次执行：

   ```bash
   cd ~/vllm-pd-grafana-panel2-pack/vllm-pd-grafana-panel2
   bin/uninstall_old.sh plan ~/vllm-pd-grafana-panel2
   bin/uninstall_old.sh remove ~/vllm-pd-grafana-panel2
   bin/uninstall_old.sh purge-data ~/vllm-pd-grafana-panel2
   bin/uninstall_old.sh check ~/vllm-pd-grafana-panel2
   ```

   每一步做什么、预期是什么，和「卸载上一版（a87f4d74）」一节的 A1–A4 相同。purge-data 删除旧的 ClickHouse 数据、拉取镜像和节点上 mock 写的日志，不可恢复；要留一份就加 `--backup <目录>`。预期：check 的最后一行是 `RESULT: OK`。

3. **用一条命令装回来。** 清单用原来的。共用的 Grafana 账号、docker 网络、监控机地址从原来的 `deploy.env` 读（`--env-from`）。Prometheus 用面板一现有的那一套：它的容器名、地址、`prometheus.yml` 路径和两个看板的 UID 用 `--set` 写进去，值照抄原来的 `deploy.env`。原来改过的其他参数（比如 `SSH_USER`、`SSH_OPTS`、`NODE_CONF_DIR`、`NODE_LOG_DIR`、`MOCK_RATE`、三个 `*_MIN_DISK_GB`）也用 `--set` 写上。模型推理环境再加 `--mode vllm`。

   ```bash
   bin/panel2_cluster.sh add <集群名> --model-group <模型文件夹> --inventory ~/vllm-pd-grafana-panel2/inventory.json \
     --env-from ~/vllm-pd-grafana-panel2/deploy.env \
     --set PROMETHEUS_CONTAINER=<原值> --set PROMETHEUS_URL=<原值> --set PROMETHEUS_YML=<原值> \
     --set PANEL1_P_UID=<原值> --set PANEL1_D_UID=<原值>
   ```

   预期：最后一行是 `RESULT: OK（集群 … 装好了：看板在 Grafana 的「…」文件夹里）`。这时：
   - `prometheus.yml` 里是新的标记段 `# BEGIN vllm-timing (面板二 <集群名>)`；
   - 抓取任务是 `vllm-timing-puller-<集群名>`、`vllm-timing-central-<集群名>`，告警规则组是 `vllm-timing-<集群名>`；旧的两个任务和规则组 `vllm-timing` 都没有了；
   - 看板在模型文件夹里，标题带集群名。

4. **删旧目录。** 原来的 `deploy.env` 里有密码，留着没有用处：

   ```bash
   rm -rf ~/vllm-pd-grafana-panel2
   ```

### 已经装好的那一套：登记（不重装）

不想重装时，也可以把它原样登记成一个集群，容器、名字都不变：

```bash
bin/panel2_cluster.sh adopt ~/vllm-pd-grafana-panel2 --cluster <集群名> --model-group <模型文件夹>
```

它做三件事：

1. 在那份 `deploy.env` 里写上 `CLUSTER`、`MODEL_GROUP`，并把原来留空的名字按原来的值写明，所以容器、端口、目录、数据源和看板的 UID、抓取任务名都不变。改前的文件存为 `deploy.env.before-adopt`。
2. 把那个目录里的脚本换成这一版。原来的打包成 `pack-before-adopt.tar.gz`；`deploy.env`、`inventory.json`、`offline` 不动。
3. 查注册表和端口，登记。

容器不动，数据照常流入。看板要挪进模型文件夹时，在那个目录里执行 `bin/grafana_board.sh`：看板的 UID 不变，只换文件夹。

### 同一台监控机装几个集群

**名字怎么来。** `deploy.env` 里名字、端口、目录、UID 这些项填了就用填的；留空时按集群名和端口基数派生，规则写在 `scripts/names.py`。集群名为 `c`、端口基数为 `B` 时：

| 项 | 留空时的值 |
|---|---|
| 总目录 `ROOT` | `deploy.env` 的 `VLLM_PD_ROOT`；`add` 时写的是 `--root`、注册表里的或 `$HOME` |
| 集群目录 | `ROOT/vllm-pd-grafana-panel2/c` |
| ClickHouse 容器、数据卷 | `vllm-pd-clickhouse-c`；`vllm-pd-clickhouse-c-data`、`vllm-pd-clickhouse-c-logs`。填了 `CLICKHOUSE_DATA_DIR` 时不用数据卷，每个集群的目录要不同 |
| 采集器容器、数据卷 | `vllm-pd-vector-c`；`vllm-pd-vector-c-data` |
| 主机端口 | ClickHouse `B`，拉取程序 `B+1`，本集群面板一的 Prometheus `B+2`。容器里的端口不变 |
| 监控机上的 `TIMING_HOME` | `ROOT/vllm-timing/c`：拉取程序的进程文件和状态；没有填 `CLICKHOUSE_DATA_DIR` 时拉取镜像也在它下面的 `pull`。节点上的 `TIMING_HOME` 不变 |
| 本集群面板一的 Prometheus | 容器 `vllm-pd-prometheus-c`，配置 `ROOT/vllm-pd-grafana-panel1/c/prometheus/prometheus.yml`，地址 `http://localhost:B+2` |
| Grafana 数据源 | UID `vllm-pd-clickhouse-c`，名字 `vLLM ClickHouse（c）` |
| Grafana 看板 | UID `vllm-pd-request-c`，标题「面板二：请求时延拆解（P / D）（c）」，放在 `MODEL_GROUP` 文件夹里；顶部跳到本集群面板一的 `vllm-pd-prefill-c`、`vllm-pd-decode-c` |
| Prometheus 里的抓取任务和规则 | 任务 `vllm-timing-puller-c`、`vllm-timing-central-c`；规则文件 `/prometheus/vllm-timing-rules-c.yml`，规则组 `vllm-timing-c`，每条规则带标签 `cluster: c`，告警名不变 |
| SSH 连接复用目录 | 临时目录下的 `vllm-ssh-c-*` |

**注册表。** `~/.vllm-pd-clusters.json` 记着总目录（`add --root` 写的），以及每个已装集群的上述名字、端口、目录、数据卷和集群机器，不记密码。每个会建容器、占端口或写 Grafana 的步骤开头都查一次：和别的集群重复，或者端口被别的程序占着，就逐项报出并停下，什么都不改。本集群自己的容器和拉取程序占着的端口不算，所以任何一步都能重跑。复制一份已有集群的 `deploy.env` 去建新集群，会在这一检查被拦住。一台集群机器只能属于一个集群。

**清单检查。** 一台机器上 vLLM API server 的端口和清单的 `node_exporter_port`（默认 9100）或 `npu_exporter_port`（默认 8082）相同时报错。这两个 exporter 装在集群机器上，本包只检查端口，不安装它们。

**共用的东西。** Grafana：各集群按 UID 增删自己的数据源和看板。Grafana 的 ClickHouse 插件：最后一个集群卸载时才删。docker 网络：不删。所有集群的容器要在 Grafana 所在的同一个 docker 网络里，Grafana 按容器名访问各集群的 ClickHouse。

**资源按集群数增加。** 磁盘：每个集群各自按 `CLICKHOUSE_MIN_DISK_GB`、`PULL_MIN_DISK_GB` 预检，N 个集群就要 N 倍。内存：ClickHouse 不设上限时按整台机器的内存定自己的上限，几个集群在同一台机器上会互相挤占。给每个集群设 `CLICKHOUSE_MAX_SERVER_MEMORY_MB`（写进 ClickHouse 的 `max_server_memory_usage`），不依赖 docker 的 cgroup 内存限制；改了以后在集群目录里执行 `bin/clickhouse_up.sh`，ClickHouse 重新读配置，不重启；整项去掉（改回留空）时旧的上限还在，要再执行 `docker restart <ClickHouse 容器名>` 才生效。不要小于 1500：实验室里每个集群几万行数据时，ClickHouse 自己常驻约 0.5–0.9 GiB，看板 Top-20 一条查询还要约 480 MiB，设成 900 时这条查询被 ClickHouse 停掉（`memory limit exceeded`）。docker 支持 cgroup 内存限制时，也可以再用 `CLICKHOUSE_DOCKER_ARGS="--memory 1200m"`、`CENTRAL_DOCKER_ARGS="--memory 300m"` 给容器整体限内存；docker 不支持时，步骤 9 或 10 报 `cgroup` 相关的错，去掉这两项、在集群目录里重跑即可。

## 第二部分：配置和预检

下面是 `bin/panel2_cluster.sh add` 依次做的步骤：出错时对照着看；不用这条命令、手工部署时按这里一步步做。命令都在**监控机**上、集群目录 `<总目录>/vllm-pd-grafana-panel2/<集群名>` 里执行。节点上不用手工做任何事（模型推理环境里，给 vLLM 加挂载和环境变量除外，见那一节）。

### 步骤 3：导入镜像

```bash
cd <集群目录> && docker load -i offline/clickhouse-image.tar && docker load -i offline/vector-image.tar
```

预期：有 `Loaded image: clickhouse/clickhouse-server:26.8.36.3` 和 `Loaded image: timberio/vector:0.61.0-debian` 两行。

不符合时：`no space left on device` 就清理磁盘；文件损坏就重新拷一次。

### 步骤 4：写集群清单

```bash
cp inventory.example.json inventory.json
```

用编辑器打开 `inventory.json`，改成现场的地址：

- `model`：模型名，看板的「模型」下拉框里显示它；
- `prefill.api_servers`：P 的 `IP:端口` 列表，第 0 个就是 P0，依次类推；端口是这个实例 `vllm serve --port` 的值；`procs_per_instance` 写 TP 数；
- `decode.api_servers`：D 的 `IP:端口` 列表，第 0 个就是 D0。D 的端口每台机器从 7100 排起（`10.0.2.1:7100`…`7107`、`10.0.2.2:7100`…），或者跨机器连续排（7100–7163），都按各实例实际的 `--port` 写。

面板一的 `inventory.json` 可以直接复制过来用。里面有 `proxy` 一项也没关系，这一版不去拉它。步骤 6 的预检会检查这个文件。

### 步骤 5：写部署参数

```bash
cp deploy.env.example deploy.env
```

用编辑器打开 `deploy.env`，改这几项（文件里标了「必改」）。用 `bin/panel2_cluster.sh add` 时，前四行已经写好，共用的几项从已装集群读：

| 参数 | 写什么 |
|---|---|
| `CLUSTER`、`MODEL_GROUP` | 集群名和模型文件夹，见「装一个集群（一条命令）」 |
| `PORT_BASE` | 端口基数。`add` 自动分配；手工部署时写一段没被占用的，比如 18100 |
| `CH_ADMIN_PASSWORD`、`CH_WRITER_PASSWORD`、`CH_READER_PASSWORD` | 三个新密码，自己定，只用字母和数字 |
| `MONITOR_HOST` | 监控机的 IP。Prometheus 用它访问拉取程序的状态，不能写 localhost |
| `SSH_USER` | 登录节点用的用户。和当前用户相同时留空 |
| `GRAFANA_PASSWORD` | 面板一 Grafana 的管理员密码 |
| `PROMETHEUS_YML` | 本集群面板一 `prometheus.yml` 的绝对路径；面板一按集群命令装的，留空即可 |
| `NODE_MODE` | 内网写 `mock`，模型推理环境写 `vllm` |

这几项按现场情况改，不确定就先不改，预检会说明该写什么：

| 参数 | 什么时候改 |
|---|---|
| `NODE_CONF_DIR`、`NODE_LOG_DIR` | 节点上放三个文件和日志的目录。默认在 SSH 用户的家目录下。模型推理环境里写挂进 vLLM 容器的那两个目录在宿主机上的路径 |
| `NODE_CONF_DIR_IN_CONTAINER`、`NODE_LOG_DIR_IN_CONTAINER` | 只用于模型推理环境：vLLM 容器里看到 `NODE_CONF_DIR`、`NODE_LOG_DIR` 的路径。留空时等于宿主机上的路径，即容器按同一路径看到这两个目录（比如 `-v /data:/data`）；挂在别的路径时填容器里的路径。`NODE_CONF_DIR_IN_CONTAINER` 的默认值在 v2.0 是 `/opt/vllm-pd-diag`，这一版改为等于 `NODE_CONF_DIR`。`NODE_LOG_DIR_IN_CONTAINER` 是新项：`add` 生成集群目录里的 `segments.json` 时把 `line.log_dir` 换成它，所以升级、回滚不会再把容器里的日志目录改回包里的值 |
| `NODE_VLLM_CONTAINERS` | 只用于模型推理环境：每台机器上 vLLM 的容器名，写成 `地址=容器名`，一台有几个容器时用分号分开，各台之间用逗号，比如 `10.0.3.11=vllm-p`。留空时节点自检和 `bin/diag.sh` 在每台上自己找（命令含 `vllm serve` 的容器），找到的名字写在输出里；核对后不对时再填 |
| `NODE_VLLM_STDOUT_FILES` | 只用于模型推理环境：启动脚本把 vLLM 的标准输出 `tee` 进文件、`docker logs` 看不到时，写 `地址=文件路径`。留空时只看 `docker logs` |
| `EVENT_TTL_DAYS`、`SUMMARY_TTL_DAYS` | 事件表和汇总表的保留天数，默认 15 和 30。磁盘见「磁盘」 |
| `GRAFANA_CONTAINER`、`GRAFANA_URL`、`DOCKER_NETWORK` | 共用的 Grafana 改过容器名、端口或网络名 |
| `PROMETHEUS_CONTAINER`、`PROMETHEUS_URL`、`PANEL1_P_UID`、`PANEL1_D_UID` | 本集群面板一的 Prometheus 和两个看板没有按派生的名字装。看板 UID 只影响页面顶部的跳转 |
| `CLICKHOUSE_DATA_DIR` | ClickHouse 和采集器的数据要放在大盘上，写绝对路径，比如 `/data/vllm-clickhouse`。镜像目录默认也放在这下面 |
| `SSH_OPTS` | SSH 要额外的参数，比如 `-p 2222 -i /root/.ssh/mykey` |
| `RSYNC_PATH` | 节点上的 rsync 不在 `PATH` 里，比如 `/opt/rsync/bin/rsync`。每台节点上都要有这个路径 |
| `MOCK_RATE` | 内网 mock 的全集群请求率（请求/秒）。默认留空，等于清单里 D 实例的总数，即每个 D 实例每秒 1 个请求。每个 D 实例的负担是 `MOCK_RATE` ÷ D 实例总数，超过 1.5 时升级安全检查（「节点上的数据量」一项）和 `bin/nodes.sh start` 报 `FAIL`，并写出建议值（D 实例总数）：一个 D 实例稳定处理的上限约 1.7–2.3 请求/秒，超过就持续积压。例如 8 个 D 实例时，留空即 8；填 100 时每个 D 实例 12.5 请求/秒，报 `FAIL` |

### 步骤 6：预检监控机

```bash
bin/check_cpu.sh
```

预期：每项一行，最后一行是 `RESULT: OK（…项全过）`。检查架构、CPU 特性、页大小、docker、Python、rsync、ssh、密码、清单、ClickHouse 镜像和试运行、采集器镜像、数据盘、镜像目录、内存、端口、面板一的容器和看板、`prometheus.yml`。

不符合时：`FAIL` 那一行写了怎么办，改完重跑。要把结果抄给别人时，只抄 `FAIL` 那几行和最后一行。

**CPU 这一项：** 用官方版还是兼容版，只看这台监控机的实际 CPU 特性。官方 aarch64 版要求 `asimd aes sha2 atomics lrcpc asimddp`，官方 x86_64 版要求 `avx2` 等（x86-64-v3）。缺任何一项，`FAIL` 那一行会给出要写进 `CLICKHOUSE_COMPAT_BINARY` 的路径，写好后重跑。兼容版来自离线包：aarch64 是 `26.11.1.1522`，x86_64 是 `26.11.1.1574`，镜像是 `26.8.36.3`。官方版和兼容版之间切换前，先用 `bin/monitor_down.sh --purge` 清掉数据。

### 步骤 7：预检所有节点

```bash
bin/nodes.sh check
```

预期：每台机器一行，最后一行是 `RESULT: OK（…台全过）`。比如：

```
OK   10.0.2.1 d-node-0 aarch64 Python 3.9.9 日志盘剩余 500 GB 时钟 +0.1 秒 实例 D0 D1 … D15
```

检查 SSH、架构、Python、日志目录能不能写和剩余空间、rsync、时钟差。`没有 Python 3.9 及以上，自检和 mock 用包里带的` 不算失败。`WARN` 的行也不算失败，可以继续。

| 这一行写的 | 处理 |
|---|---|
| `SSH 连不上` | 在监控机上执行这一行写的 `ssh-copy-id`，或者检查网络、`SSH_USER`、`SSH_OPTS` |
| `日志目录 … 建不了或写不了` | 换 `NODE_LOG_DIR`，或者让 SSH 用户能写这个目录 |
| `没有 rsync`、`rsync 不是 3.x` | 在那台机器上安装（`yum install rsync` 或 `apt install rsync`）；已经装在别处时写 `RSYNC_PATH` |
| `日志目录所在盘要 … GB` | 清理磁盘，或者改小 `segments.json` 的 `line.keep_hours` 后改小 `NODE_MIN_DISK_GB` |
| `时钟差超过 5 分钟` | 先校时 |
| `WARN … 时钟差超过 1 秒` | 不算失败。P / D 时间线上 P 末到 D 首的间隔会差这么多，P / D 配对不受影响。建议用 chrony 或 ntp 校时 |

### 步骤 8：生成每台机器的路径清单

```bash
bin/nodes.sh paths
```

预期：每台机器一段，列出三个文件的目录、每个实例的日志目录和这个实例会有的文件；最后一行是 `共 … 台机器、… 个实例（P … 个，D … 个）；写进了 generated/node_paths.txt`。每个实例的日志目录按它的 `vllm serve --port` 命名，后面括号里写实例名（P0、D17）。P、D 的实例都有 api、engine、worker 三类文件。`NODE_MODE=vllm` 时，每台机器还有一行「这台机器一组：启动脚本加 …」和一行「容器加挂载 …」，模型推理环境一节要用（步骤 V2）。

## 第三部分：启动

### 步骤 9：启动 ClickHouse

```bash
bin/clickhouse_up.sh
```

预期：最后三行是 ClickHouse 版本、`表: diag_event filter_file filter_other filter_selfcheck phase_line request_events request_json`、`RESULT: OK`。

不符合时：

- 报端口被占用：改 `deploy.env` 里的 `CLICKHOUSE_HTTP_PORT`，重新执行；
- 容器不断重启、日志里有 `Illegal instruction`：按步骤 6 改用兼容版；
- 其他情况执行 `docker logs --tail 30 <CLICKHOUSE_CONTAINER 的值>` 看最后的报错。

### 步骤 10：启动采集器

```bash
bin/central_up.sh
```

预期：显示 `采集器已启动`，最后一行是 `RESULT: OK`。采集器的规则由 `segments.json` 生成，启动前用 Vector 自己检查过一遍；容器名是 `CENTRAL_CONTAINER`（留空时 `vllm-pd-vector-<集群名>`），机器重启后自己起来。

### 步骤 11：给 Grafana 装 ClickHouse 插件

会重启一次 Grafana，页面中断一小段时间，重启后面板一的看板都在。Prometheus 不受影响。

```bash
bin/grafana_plugin.sh
```

预期：最后两行是 `ClickHouse 插件版本: 4.23.4` 和 `RESULT: OK`。

不符合时：报 `容器 ... 没在运行` 时检查 `GRAFANA_CONTAINER`；报 `登录失败` 时检查 `GRAFANA_PASSWORD`。

### 步骤 12：导入数据源和看板

```bash
bin/grafana_board.sh
```

预期：有 `Grafana 能连上 ClickHouse` 和 `看板已导入`，最后一行是 `RESULT: OK`。

不符合时：报 `Grafana 连不上 ClickHouse` 时，确认 ClickHouse 和 Grafana 在同一个容器网络（`DOCKER_NETWORK`）里，密码和 `deploy.env` 一致。

### 步骤 13：让 Prometheus 抓采集链路的指标

```bash
bin/prometheus_job.sh
```

预期：有 `抓取目标: 1 个拉取程序、1 个采集器，告警规则: 6 条`，最后一行是 `RESULT: OK`。这时拉取程序还没启动，它的目标显示 down 是正常的。

不符合时：报 `已经有 rule_files` 时，手工把 `/prometheus/<RULES_FILE>`（留空时 `vllm-timing-rules-<集群名>.yml`）加进现有的 `rule_files`，再执行一次。以后用面板一的脚本重新生成或整个替换了 `prometheus.yml`，要再执行一次这一步，它把带标记的那一段重新加回去。

### 步骤 14：下发三个节点文件

```bash
bin/nodes.sh push
```

预期：每台机器一行 `三个文件已下发到 …（segments.json …）`，最后一行是 `RESULT: OK（…台全过）`。文件先写到同一目录下的临时名字，再改名替换，正在运行的过滤器不会读到写了一半的文件；下发后逐个比对 sha256。这一版先在节点上算三个文件的 sha256，只替换内容不同的文件，内容相同的文件不动。

### 步骤 15：节点自检

```bash
bin/nodes.sh selfcheck
```

预期：每台机器一行 `… 自检 … 项全过`，最后一行是 `RESULT: OK`。在每台机器上，用和 vLLM 一样的环境变量起一个 Python 进程：加载 logging 配置、从 `PYTHONPATH` 导入过滤模块、把包里带的一份 P、D 全量样本灌进过滤器（写到临时目录），检查请求级事件一条不缺、各请求的累加和样本的真值一致、自检行出错数为 0、每类进程各有自己的文件；再改一份映射文件的副本，确认过滤器不重启就重读；最后确认日志目录能写。机器上除了日志目录里一个立即删掉的测试文件，不留下任何东西。

不符合时：那一行写了没过的项。`import pd_diag_logfilter 失败` 多半是 Python 低于 3.9；`日志目录 … 写不了` 改 `NODE_LOG_DIR` 或目录权限。

### 步骤 16：在所有节点上启动 mock 写入程序（只在内网）

模型推理环境跳过这一步，按「模型推理环境」一节做。

```bash
bin/nodes.sh start
```

预期：每台机器一行 `mock 已启动：…，mock：… 上 … 个实例（…），全集群 … 请求/秒，日志 …`，最后一行是 `RESULT: OK（…台全过）`。这条命令先下发一次三个文件，再通过 SSH 把 mock 拷到每台机器的 `~/vllm-timing-node` 并启动。只想动某几台时，在命令后面写地址。

启动前，监控机先经 SSH 停掉这台机器上已有的 mock，确认进程数为 0；停不干净时那一行 `FAIL`，不启动。每个 D 实例的负担（`MOCK_RATE` ÷ D 实例总数）超过 1.5 请求/秒时也 `FAIL`，见步骤 5。

mock 写入程序做的事：每台机器上，每个实例一个进程，按当前时间生成请求（所有机器用同一个随机种子，各自只写自己实例的部分）；P 每个请求单独一批，D 按到达的请求组批，每步写出 vLLM 那样的全量逐事件流；这些行经过下发的同一份 logging 配置、过滤器和 `segments.json` 写盘，所以写到盘上的和 vLLM 加过滤器写的是同一种文件。按步的行在内存里生成、被过滤器丢掉，不落盘。

### 步骤 17：启动拉取程序

```bash
bin/puller.sh start
```

预期：显示 `拉取程序已启动`，最后一行是 `RESULT: OK`。它留在后台运行，崩溃后自己再起来。

## 第四部分：确认链路通

### 步骤 18：升级安全检查

```bash
bin/panel2_cluster.sh check <集群名>
```

内网时，`add` 的最后一步就是它，并且最多等 2 分钟过滤器的自检行：过滤器和链路通两项没过时，每 10 秒再查一次。单独执行时不等，要等时加 `--wait <秒>`。模型推理环境里 `add` 不做这一步：给 vLLM 加好挂载和环境变量、重启以后单独执行（步骤 V6）。

这一步只查能不能安全地装上和升级，不查数据准不准（那是步骤 19）。它是只读的：ClickHouse 的查询都带 `--readonly=1`，节点上只执行读的命令，什么都不改。代码在 `scripts/safety.py`。

预期：第一行是 `升级安全检查：集群 …（…，… 模式，面板二 v2.1），只读`，最后一行是 `RESULT: OK`。中间每一行以四种标记之一开头：

| 标记 | 意思 | 拦不拦 |
|---|---|---|
| `OK` | 这一项满足 | — |
| `INFO` | 只是告知，比如最近 15 分钟的请求数、自动找到的 vLLM 容器名 | 不拦 |
| `WARN` | 要留意，但不影响装和升级，比如没能核对真实的 vLLM 容器 | 不拦：最后一行是 `RESULT: WARN（n 项）`，退出码仍为 0，`add` 照常做完 |
| `FAIL` | 这一项不满足 | 拦：最后一行是 `RESULT: FAIL（n 项）`，退出码为 1，`add` 停在步骤 18；带 `--ignore-check` 时只报告，不停下 |

九项：

| 项 | 查什么 | `FAIL` 时 |
|---|---|---|
| 版本和文件 | 集群目录的 `VERSION` 是 v2.1；目录里的文件和 `SHA256SUMS` 相同（`segments.json` 只允许 `line.log_dir` 不同；`deploy.env`、`inventory.json`、`generated/`、`offline/` 不比） | 有文件被改过或缺了：重跑 `add`，换回包里的 |
| 容器 | ClickHouse 和采集器的容器在运行；采集器的配置通过 Vector 自己的检查 | `bin/clickhouse_up.sh` 或 `bin/central_up.sh` |
| 库 | 库 `vllm_pd` 里五张表（`phase_line`、`diag_event`、`filter_selfcheck`、`filter_file`、`filter_other`）和视图 `phase_line_cur` 都在，视图读的是这一版的 `config_id` | `bin/clickhouse_up.sh` |
| Grafana | 用 `deploy.env` 的账号能登录，本集群的看板和数据源都在 | 登录不了时查 `GRAFANA_PASSWORD`；看板或数据源不在时 `bin/grafana_board.sh` |
| 节点 | 每台能 SSH 登录；三个节点文件和监控机要下发的相同（sha256）；内网每台的 mock 进程数等于这台的实例数；模型推理环境里没有 mock 进程，并核对正在运行的 vLLM 容器 | 文件不同：`bin/nodes.sh push`；mock 进程少了：`bin/nodes.sh start`；多了：旧进程没停干净，先 `bin/nodes.sh stop` 再 `start`。`FAIL` 行写了要执行的命令 |
| 过滤器 | 最近 3 分钟各进程的自检行：写的版本（`config_id@` 加集群目录里 `segments.json` 的 sha1 前 8 位）是期望的那一个，没有两个以上版本同时在写 | 内网没有自检行时看 `bin/nodes.sh status`；版本不对时 `bin/nodes.sh push`，内网再 `bin/nodes.sh start`；模型推理环境见下 |
| 链路通 | 拉取程序在运行，每台最近 2 分钟内拉取成功过；Prometheus 抓得到拉取程序和采集器；最近 135 秒（自检间隔 60 秒、写出间隔 5 秒、拉取间隔 10 秒，再加 60 秒）ClickHouse 里有这一版的自检行或汇总行 | `bin/puller.sh start`；某台拉不到时看 `bin/puller.sh status` 那一行的报错 |
| 节点上的数据量 | 只在内网：每个 D 实例的负担 `MOCK_RATE` ÷ D 实例总数不超过 1.5 请求/秒 | 改小 `MOCK_RATE`（或留空），执行 `bin/nodes.sh start` |
| 日志量和空闲空间 | 按拉取程序最近 30 分钟传输的字节数和 `line.keep_hours` 估每台节点要保留的日志量，和这台日志盘的空闲空间比；各台之和和监控机镜像盘的空闲空间比 | 调小 `MOCK_RATE` 或 `segments.json` 的 `line.keep_hours`，或者腾出磁盘。拉取程序没在运行时估不出，记 `WARN` |

链路通一项的 135 秒按 `segments.json` 的 `line.selfcheck_seconds`、`line.flush_seconds` 和 `deploy.env` 的 `PULL_INTERVAL_S` 算，改了这几项就跟着变。

**模型推理环境：按实例判有没有记录。** 过滤器在一个进程写出第一条记录时才打开文件，自检行也只写进已经打开的文件。所以 vLLM 重启以后、第一个请求以前，这个进程什么都不写，连自检行也没有。过滤器、版本、链路通三项因此按实例判：

- 这次 vLLM 启动以后还没有记录、过滤器已经创建（进程环境里有 `PD_DIAG_INSTANCE_SOURCE`）的实例，记 `WARN`「这次启动以后还没有记录，有请求后再查」，不是 `FAIL`；
- 这次启动以后没有记录、又读不了进程环境变量的实例，分不清过滤器有没有创建，也记 `WARN`；
- 这次启动以后没有记录、过滤器也没有创建的实例，记 `FAIL`，用 `bin/diag.sh vllm` 看是哪一种原因；
- 有过记录的实例照上表判。`add` 下发节点文件以后 2 分钟内版本还是旧的，记 `INFO`（过滤器 2 秒内重读 `segments.json`，下一次自检最多 60 秒）；超过 2 分钟仍是旧的，记 `FAIL`。

vLLM 重启以后还没有请求时检查（比如在空闲时段重启后马上检查），这三项因此是 `WARN`。发几个请求以后再执行一次 `check`，这些实例就按上表判。

### 步骤 19：数据准确性检查（可选，quality）

```bash
bin/panel2_cluster.sh quality <集群名>
```

要看数据准不准时执行，先让集群攒几分钟数据（内网 `add` 以后至少 2 分钟）。它依次执行 `bin/verify.sh`（链路和看板）和 `bin/postdeploy_check.sh`（部署后核验），只报告：不进 `add`、`check` 和更新脚本，结果不影响它们的 `RESULT`。

预期：最后一行是 `RESULT: OK（集群 … 的数据检查）`。有项没过时是 `RESULT: FAIL（集群 … 的数据检查有项没过，见上面；不影响升级）`。

**链路和看板（`bin/verify.sh`）。** 逐项列出：Prometheus 抓到拉取程序和采集器、解析失败的行、6 条告警规则、最近 15 分钟的请求数、汇总行和事件行、每个实例都有数据、各机器最新日志的时间差、抽查 20 个请求的 JSON、P、D 两侧三类进程（api、engine、worker）都有行的比例、29 个 panel 都有数据。

| 现象 | 处理 |
|---|---|
| 拉取程序指标抓不到 | 先看 `bin/puller.sh status`。它在运行时，检查 Prometheus 容器能不能访问 `MONITOR_HOST` 的 `PULLER_METRICS_PORT` 端口 |
| 某台机器超过 2 分钟没有拉取成功 | `bin/puller.sh status` 那一行有 rsync 的报错。修好后自动补上 |
| 没有数据 | 依次看 `bin/nodes.sh status`、`bin/puller.sh status`、`docker logs --tail 20 vllm-pd-vector-<集群名>`，或者执行 `bin/diag.sh chain` |
| 某些实例没有数据 | 这些实例所在的机器 mock 没起来，或者地址写错了 |

**部署后核验（`bin/postdeploy_check.sh`）。** 逐项列出：

```
OK    清单上的 … 个实例都有过滤器写的文件
OK    每个实例目录里只有一个 DP rank
OK    每个实例的身份都来自 vllm serve 的 --port 或手工设的 PD_DIAG_INSTANCE
OK    采集器没有解析失败的行（行前缀和 JSON 都是新格式）
OK    最近 10 分钟只有请求级事件和过滤器自己的行
OK    每个实例的进程都有文件：api、engine、worker 三类
OK    … 个进程的自检行出错数都是 0
OK    节点上的 segments.json 和监控机一致（…）
OK    按 X 配对：P 侧 … 个请求里 …% 在 D 侧也有，D 侧 … 个里 …% 在 P 侧也有
OK    P 末到 D 首的间隔：最小 … ms，p1 … ms，p50 … ms，p99 … ms；为负的 0 个
OK    … 个累计段都没有变小
OK    … 台机器都在拉取
```

部署后核验在模型推理环境的第一天最有用，每一项 `FAIL` 的处理见步骤 V6。内网 mock 的时钟偏移演练（`MOCK_CLOCK_OFFSET_S`）会让「为负的」不为 0，这是预期的。

### 步骤 20：打开看板

**在浏览器里。** 打开 `GRAFANA_URL`（比如 `http://<监控机>:3000`），进入 `MODEL_GROUP` 文件夹里的「面板二：请求时延拆解（P / D）（<集群名>）」。右上角选时间范围；页面不自动刷新。看板只用本集群的数据源：数据源被删掉时它报找不到数据源，不会显示别的集群的数据。

- 「实例」下拉框里是 P0–P7 和 D0–D63。选 P3 时，Top-20 只看经过 P3 的请求，P 侧分布只看 P3，D 侧分布不受影响；选 D 实例时反过来。
- 「排序依据」决定 Top-100 取哪些请求：D 官方 E2E、P 官方 TTFT、P 侧合计、D 侧合计、TPOT。
- 点 Top-20 表里的 `request_id`，页面跳到这个请求的拆解、P / D 时间线和逐个事件。

## 模型推理环境（`NODE_MODE=vllm`）

节点上跑的是打了补丁、写 version 2 记录的 vLLM。和内网相比，监控机上的步骤相同，节点上多一件事：给每台机器上的 vLLM 挂两个目录、加两个环境变量，然后重启（原容器没有挂这两个目录、也不能按同一路径看到它们的，要重建容器）。一台机器一组变量：用一个启动脚本（比如 `launch_online_dp.sh`）起多个 DP rank 时，所有 rank 共用这一组，过滤器按每个 `vllm serve` 自己的 `--port` 分实例。

启用后，`[PD_DIAG]` 行从重定向出来的 stdout 日志里去掉，改写进过滤器的文件，所以参考脚本 `pd_metrics_report.py` 不能再读 serving.log 拿这些行。vLLM 的其他日志行不变。

### 先向现场确认的事

| 事项 | 为什么要知道 | 写到哪里 |
|---|---|---|
| vLLM 的日志目录是否已经挂出到宿主机，挂在哪里，容器里是什么路径 | 过滤器把 `[PD_DIAG]` 行写到容器里的 `line.log_dir`，即 `NODE_LOG_DIR_IN_CONTAINER`（留空时等于 `NODE_LOG_DIR`）；监控机从宿主机的那一端拉 | `NODE_LOG_DIR`、`NODE_LOG_DIR_IN_CONTAINER` |
| 放三个文件的目录：宿主机上的路径（SSH 用户能写），容器里是什么路径 | 监控机下发到宿主机上的这个目录；过滤器在容器里从同一个目录读 | `NODE_CONF_DIR`、`NODE_CONF_DIR_IN_CONTAINER` |
| 容器里跑 vLLM 的用户的 uid（`docker exec <容器> id -u`） | 这个用户要能读三个文件、写日志目录 | `NODE_CONTAINER_UID` |
| 启动脚本能不能改，容器能不能重建 | 第一次启用要加环境变量，容器看不到这两个目录时还要加挂载；没挂过这两个目录、也不能按同一路径看到它们的容器只能重建 | — |
| vLLM 的镜像名 | 自检会在这个镜像的一次性容器里再查一遍挂载和权限 | `NODE_SELFCHECK_IMAGE` |
| 每台机器上 vLLM 的容器名 | 节点自检和 `bin/diag.sh` 要对正在运行的 vLLM 容器核对；不填时自己找，找到的名字写在输出里 | `NODE_VLLM_CONTAINERS`（可不填） |
| 容器里有没有设 `PYTHONPATH`（比如在 `~/.bashrc` 里） | 步骤 V4 的 `export PYTHONPATH=…` 是追加；要放在读完 `~/.bashrc` 之后 | 启动脚本 |
| vLLM 的启动方式：`tee serving.log` 还是 `docker logs` | 不影响过滤器：它自己写文件，`[PD_DIAG]` 行不再出现在 vLLM 的标准输出里。`bin/diag.sh vllm` 要读标准输出，`docker logs` 看不到时要告诉它文件在哪里 | `NODE_VLLM_STDOUT_FILES`（只在 `tee` 进文件时填） |
| 每个 P、D 实例的 `vllm serve --port`（启动脚本起多个 DP rank 时，每个 rank 一个端口） | 过滤器按它给实例目录命名，采集器按「机器地址:端口」在清单里查出 P0、D17 | `inventory.json` 的 `api_servers` |

### 步骤 V1：改部署参数

在 `deploy.env` 里：`NODE_MODE=vllm`，按上表填 `NODE_CONF_DIR`、`NODE_LOG_DIR`、`NODE_CONTAINER_UID`、`NODE_SELFCHECK_IMAGE`。容器里看到这两个目录的路径是 `NODE_CONF_DIR_IN_CONTAINER`、`NODE_LOG_DIR_IN_CONTAINER`，留空时等于 `NODE_CONF_DIR`、`NODE_LOG_DIR`：容器按同一路径看到宿主机目录时不用填，挂在别的路径时填容器里的路径。`NODE_VLLM_CONTAINERS`、`NODE_VLLM_STDOUT_FILES` 一般留空，见步骤 5。

```bash
grep -E '^(NODE_MODE|NODE_CONF_DIR|NODE_LOG_DIR|NODE_CONTAINER_UID|NODE_SELFCHECK_IMAGE)=' deploy.env
```

预期：五行，`NODE_MODE=vllm`。

### 步骤 V2：生成每个实例的启动参数

```bash
bin/nodes.sh paths
```

预期：每台机器一段：每个实例一行日志目录（按端口命名，括号里是实例名和 `vllm serve --port`）；一行「这台机器一组：启动脚本里、vllm serve 之前加这两行，所有 rank 共用」，下面是两行 `export`，路径是 `NODE_CONF_DIR_IN_CONTAINER`；一行「容器加挂载：-v <NODE_CONF_DIR>:<NODE_CONF_DIR_IN_CONTAINER>:ro -v <NODE_LOG_DIR>:<NODE_LOG_DIR_IN_CONTAINER>」。同样的内容写在 `generated/node_paths.txt`。以测试环境的 D 机器为例（8 个实例，端口 7100–7107；`NODE_CONF_DIR=/data/pd-diag/conf`、`NODE_LOG_DIR=/data/pd-diag/logs`，两个容器路径留空，即容器里路径相同），下面列出其中的目录、`export` 和挂载几行：

```
10.0.0.21
  /data/pd-diag/conf/  pd_diag_logging.json  pd_diag_logfilter.py  segments.json
  /data/pd-diag/logs/7100/  api.<启动时间>.<pid>.<序号>.log  engine.<启动时间>.<pid>.<序号>.log  worker.<启动时间>.<pid>.<序号>.log    （D0，vllm serve --port 7100）
  …
  /data/pd-diag/logs/7107/  …    （D7，vllm serve --port 7107）
  这台机器一组：启动脚本里、vllm serve 之前加这两行，所有 rank 共用
    export VLLM_LOGGING_CONFIG_PATH=/data/pd-diag/conf/pd_diag_logging.json
    export PYTHONPATH=/data/pd-diag/conf${PYTHONPATH:+:$PYTHONPATH}
  容器加挂载：-v /data/pd-diag/conf:/data/pd-diag/conf:ro -v /data/pd-diag/logs:/data/pd-diag/logs
```

这一版的输出另外写明三点：容器里已经能在同一路径看到这两个宿主机目录时，不用加这两条挂载；容器内路径和宿主机不同时，单独列出对应关系；两行 `export` 要放在哪里（见步骤 V4）。

**实例怎么认：** 不用设 `PD_DIAG_INSTANCE`。过滤器从每个 `vllm serve` 自己的命令行取 `--port`（`--port 7103` 或 `--port=7103`）作为实例目录名，它启动的 EngineCore、Worker 继承同一个值；采集器按 `<机器地址>:<端口>` 在清单里查出 P0、D17。所以清单里每个实例的端口要和它的 `--port` 一致，端口怎么排（每台 7100–7107，或者 7100–7163 跨机器连续）都可以。命令行上没有 `--port`（没写，或者只写在 `--config` 文件里）时，过滤器按 vLLM 默认的 8000 算，部署后核验会报「身份是缺省值」。

**一个脚本起多个 rank 时，不要设 `PD_DIAG_INSTANCE`，或者设成 `auto`。** 设了名字或端口时以它为准，同一组变量下的所有 rank 会落进同一个目录，部署后核验会报「实例目录里有不止一个 DP rank」。一定要手工设的话，设在每个 rank 自己那一段里，值用这个 rank 的端口变量，例如：

```bash
for i in $(seq 0 7); do
  PORT=$((7100 + i))
  PD_DIAG_INSTANCE=$PORT vllm serve <模型> --port $PORT --data-parallel-rank $i ... &
done
```

### 步骤 V3：下发和自检

```bash
bin/nodes.sh push && bin/nodes.sh selfcheck
```

预期：两段输出都以 `RESULT: OK` 结尾。填了 `NODE_CONTAINER_UID` 时，自检多两行「容器用户 uid … 能读三个文件」「… 能写日志目录」；填了 `NODE_SELFCHECK_IMAGE` 时，再多一行「镜像 … 里：挂进去的三个文件能读、模块能 import、日志目录 … 能写」（这一项要节点上有 docker 命令）。

这一版的自检还对正在运行的 vLLM 容器核对：用 `docker inspect`、`docker exec` 看三个文件在同一个目录里、日志目录能写。容器名取 `NODE_VLLM_CONTAINERS`，没填时自己找，找到的名字写在输出里，要核对一下。找不到容器，或者 SSH 用户不能执行 docker 时，记 `WARN`「没有核对真实容器」，不算失败。

### 步骤 V4：给每个实例加挂载和环境变量，重启或重建

**在每台 P、D 机器上，改 vLLM 的启动方式。** 两处：

1. 容器加两个挂载（按步骤 V2 的「容器加挂载」）。以 `docker run` 启动的为例：

   ```
   docker run ... \
     -v <NODE_CONF_DIR>:<NODE_CONF_DIR_IN_CONTAINER>:ro \
     -v <NODE_LOG_DIR>:<NODE_LOG_DIR_IN_CONTAINER> \
     <原来的镜像和参数>
   ```

   容器已经挂了这两个目录的，或者已经能在同一路径看到它们的（比如 `-v /data:/data`），跳过这一步。都不是的，容器要重建。挂目录而不是挂单个文件：下发是「写临时文件再改名」，单个文件的挂载看不到改名后的新文件。

2. 在 `vllm serve` 之前加两行（按步骤 V2 的「这台机器一组」，路径已经替换好）。一个脚本起多个 DP rank 时，也只加这一次，所有 rank 共用：

   ```bash
   export VLLM_LOGGING_CONFIG_PATH=<NODE_CONF_DIR_IN_CONTAINER>/pd_diag_logging.json
   export PYTHONPATH=<NODE_CONF_DIR_IN_CONTAINER>${PYTHONPATH:+:$PYTHONPATH}
   ```

   **写在哪里：** 这两行要写在容器里实际执行 `vllm serve` 的那个脚本里（比如 `launch_online_dp.sh`），在 `vllm serve` 之前；也可以用 `docker run -e` 或 `docker exec -e` 把这两个变量传进容器。写在宿主机的 shell 里或者别的脚本里不生效，vLLM 进程看不到这两个变量。用 `-e` 时，`PYTHONPATH` 的值在宿主机上展开，要写成完整的值。

   **指向哪里：** 两个变量都指向 `NODE_CONF_DIR_IN_CONTAINER`。它必须是 `bin/nodes.sh push` 写入的那个目录（宿主机上的 `NODE_CONF_DIR`）在容器里看到的路径，三个文件在这个目录里放在一起。

   `PYTHONPATH` 是追加，不是覆盖：容器里已经设了的（比如写在 `~/.bashrc` 里），原来的目录都还在后面；原来为空时也不会多出一个空项。这两行要在脚本读完 `~/.bashrc` 之后，否则会被它覆盖。改完重启 vLLM，再用 `bin/diag.sh vllm` 确认每台的结论。

几点注意：

- `VLLM_LOGGING_CONFIG_PATH` 让 vLLM 用这份 logging 配置整份替换自己的默认配置。vLLM 自己的日志仍然打到标准输出，格式和原来一样；只有 `[PD_DIAG]` 行改为写进日志目录。配置里日志级别固定为 INFO，原来用 `VLLM_LOGGING_LEVEL` 调过级别的，改 `pd_diag_logging.json` 里的 `level`。
- 环境变量要设在 vLLM 进程上，子进程（EngineCore、Worker）会继承。
- 只有 TP rank 0 的 Worker 写盘；其他 rank 的行在进程里丢掉。
- 第一次启用要重启 vLLM。以后改 `segments.json` 不用重启：每个进程每 2 秒看一次它的修改时间，变了就重读；写过记录的进程 2 秒内重读，还没写过记录的进程在它的第一条记录之后 2 秒内重读。`pd_diag_logfilter.py` 和 `pd_diag_logging.json` 本身改了，要重启 vLLM。改过以后都用 `bin/diag.sh vllm` 确认。

### 步骤 V5：启动拉取程序

还没有启动时：

```bash
bin/puller.sh start
```

### 步骤 V6：升级安全检查和数据准确性检查

先让每个实例处理几个请求，等 2 分钟，然后执行升级安全检查（步骤 18）：

```bash
bin/panel2_cluster.sh check <集群名>
```

预期：最后一行是 `RESULT: OK`，或者只有 `WARN` 的 `RESULT: WARN（n 项）`。还没有处理过请求的实例，过滤器、版本、链路通三项记 `WARN`「这次启动以后还没有记录，有请求后再查」，不是 `FAIL`（规则见步骤 18）。要看每台 vLLM 的变量、挂载和过滤器是否生效，执行 `bin/diag.sh vllm`。

再看数据准不准（步骤 19，只报告，不拦任何命令）：

```bash
bin/panel2_cluster.sh quality <集群名>
```

其中部署后核验（`bin/postdeploy_check.sh`）每一项 `FAIL` 的意思和处理：

| `FAIL` 的项 | 常见原因 | 处理 |
|---|---|---|
| 清单上的这些端口没有过滤器写的文件 | 环境变量没加到这个实例上；挂载路径不对；这个实例的 `vllm serve --port` 和清单里的端口不一致 | 对照 `generated/node_paths.txt`；在容器里看 `ls <NODE_CONF_DIR_IN_CONTAINER> <NODE_LOG_DIR_IN_CONTAINER>`；或者执行 `bin/diag.sh vllm` |
| 这些实例名不在清单里 | 实例目录名（`--port`）不是清单里这台机器的端口；或 `PD_DIAG_INSTANCE` 写了别的值 | 改清单或启动脚本 |
| 实例目录里有不止一个 DP rank | 几个 `vllm serve` 共用了一个 `PD_DIAG_INSTANCE` 值，或用了同一个 `--port` | 去掉 `PD_DIAG_INSTANCE`（或设成 `auto`），重启 vLLM。这一项只看还在运行的进程（最近 3 分钟写过自检行的），重启后约 3 分钟再查；3 分钟后仍报，就是新进程还在共用目录 |
| 这些实例的身份是缺省值 | 命令行上没有 `--port`：没写（vLLM 用 8000），或者只写在 `--config` 文件里 | 在 `vllm serve` 的命令行上写 `--port`，重启 vLLM |
| 采集器有解析失败的行 | 行前缀不是新格式（logging 配置没有生效）；或者真实日志的格式和补丁源码不一样 | 输出里有一行样子。前者查环境变量；后者把这一行发给维护者，改 `segments.json` 或解析规则 |
| 有白名单以外的事件 | 过滤器没生效，或者真实 vLLM 写了映射没想到的事件 | 看自检行；确实需要的事件加进 `segments.json` 的 `passthrough`，执行 `bin/apply_segments.sh` |
| 缺进程的文件 | EngineCore 或 Worker 子进程没有用到 logging 配置；或者这类进程还没有写过要留的行 | 先多发几个请求再查；仍然没有时，确认环境变量设在 vLLM 主进程上 |
| 自检行出错数不为 0 | 过滤器处理某类行出错（只计数，不影响 vLLM） | 把那台机器的日志目录和 `segments.json` 发给维护者 |
| 节点上的 `segments.json` 版本和监控机不同 | 下发之后又改了监控机上的 `segments.json` | `bin/nodes.sh push` |
| 按 X 配对的比例不到 95% | 清单里漏了 P 或 D；proxy 发给 P、D 的 `X-Request-Id` 不同；见「已知限制」 | 核对清单和 proxy |
| P 末到 D 首的间隔有负值 | D 机器的钟比 P 慢；或者配对错了 | 先校时（chrony/ntp），再看已知限制 |

### 从内网切到模型推理环境

已经在内网用 mock 部署过的，按下面的顺序切换。每一步都在监控机的集群目录里执行。

1. **备份：** 先拷一份 `deploy.env` 和 `inventory.json`（比如 `cp -p deploy.env deploy.env.mock-backup`），切回 mock 时用。
2. **停 mock、删节点上 mock 的文件（仍是 mock 模式）：**

   ```bash
   bin/nodes.sh clean
   ```

   预期：每台一行 `OK`，最后一行 `RESULT: OK`。它从监控机停掉每台的 mock（进程为 0 才删），再限速删本包放在节点上的文件和 mock 的日志。
3. **改成 vllm 模式重装监控机部分：** `bin/panel2_cluster.sh add <集群> --model-group <组> --inventory <清单> --mode vllm --set NODE_CONF_DIR=… --set NODE_LOG_DIR=…`（步骤 V1）。内容不变的步骤会跳过；vllm 模式下 add 不启动 mock。
4. **清空旧环境的全部数据（这时已是 vllm 模式）：**

   ```bash
   bin/clear_data.sh --yes --all
   ```

   预期：最后一行 `RESULT: OK`。`--all` 连过滤器的三张表一起清，旧环境的文件记录不留到新环境。vllm 模式下它不起 mock、不清镜像、不动节点；拉取程序和采集器清完以后照常启动。
5. **接 vLLM：** 按步骤 V2–V4 给每个实例加两行 `export`、重启 vLLM，再做 V5、V6；最后执行 `bin/diag.sh vllm`，每台的结论应为 OK。

## 看板上有什么

| 区域 | panel |
|---|---|
| 所选时间段概况 | 请求数；官方 E2E：P / D；官方 TTFT：P / D；TPOT；P 合计 / D 合计；P 侧轮次、D 侧总步数（p50、p90、p99、均值、最大）；P 侧、D 侧分段时延分布（p50、p90、p99、均值、均值占比） |
| 最慢请求 | 按「排序依据」取最慢的 100 个请求，含每侧最大的段和它占这一侧合计的比例，分页显示，点表头可以重新排；最慢的 20 个请求的 P、D 两侧分段条形图；P、D 两侧的子段明细：P 侧一张，D 侧到第一个 token 一张、第二步起一张；日志里没有记录的段不列 |
| 单请求拆解 | 官方 E2E：P / D；官方 TTFT：P / D；TPOT；tokens；路由与结果；P / D 时间线；P、D 两侧分段明细 |
| 单请求事件 | 这个请求两侧的请求级记录，按时间排列（保留 `EVENT_TTL_DAYS` 天） |

段和 L1 / L2 / L3 一致：P 侧 api 网关、P/D LB proxy、req_resolve、tokenizer、add_request、schedule（queue_time、hash、lookup、alloc、other）、等 put 完成、start_load_kv（submit、wait_for_transfer）、forward、sampling、detokenizer，后台 wait_for_save（build_key、lookup、prepare、sync、put：submit、wait_for_transfer、finalize）；D 侧 P/D LB proxy、req_resolve、tokenizer、add_request、schedule（queue_time、alloc、other）、start_load_kv（submit、wait_for_transfer）、第一步的 forward、sampling、detokenizer，第二步起的 schedule（alloc、other）、forward、sampling、detokenizer。每段取自哪条记录的哪个字段：

- **官方 E2E、官方 TTFT、TPOT、token 数、结束原因**：`api_summary`（`scope` 为 `request`）的 `official`，vLLM 自己统计的值，各侧从本侧收到请求算起。P、D 两侧分列，不相加：客户端视角的 E2E 和 TTFT 要 proxy 补丁之后才有。
- **P 合计、D 合计**：`api_summary`（`scope` 为 `http`）的 `end_ns − start_ns`，各侧 API server 从收到请求到最后一个字节。
- **req_resolve**：`api_summary[http]` 的 `stages.req_resolve` 加 `stages.render`；**tokenizer**：`stages.tokenize`；**add_request**：`api_summary[engine_submission]` 的 `stages.add_request`。add_request 的起点比 tokenize 的结束早几十微秒，照日志的值显示，不扣。
- **detokenizer**：P 是 `api_summary[request]` 的 `terminal.detokenize_duration_ns`。D 的第一步是 `stages.detokenize.ranges[]` 里 `execution_id` 等于这个请求第一步的那一条（第一步由节点上的过滤器按这个请求最早出现的 `core_summary[execution]` 记下），第二步起是合计减第一步。ranges 有条数上限：第一步那一条不在里面时，D 的这两段都为空，部署后核验报这样的请求有几个。
- **schedule 下的 queue_time、hash**：`core_summary`（`scope` 为 `request`）的 `stages.queue.initial_wait_ns`、`stages.hash.duration_ns`。一个请求有几条时只取 `timestamp_ns` 最晚的一条，部署后核验报这样的请求有几个。
- **lookup、alloc、other**：每一步 `core_summary`（`scope` 为 `execution`）的 `stages.lookup` / `alloc` / `other`，在节点上按 `request_ids` 累加；值为 null（这一步没有做）的不加。D 的「第一步」是请求的第一步，「第二步起」是其余各步。
- **forward、sampling**：每一步 `worker_summary`（`phase` 为 `execute` / `sample`）的 `stages.forward` / `stages.sampling`，TP rank 0，按 `request_ids` 累加。同一批的每个请求都记整步的时长。都是 host 侧耗时（`completion=host_return`），补丁不加设备同步。
- **P 侧轮次、D 侧总步数**：`core_summary[request]` 的 `members[]` 里这个请求那一项的 `executions`（有几条时取最晚的一条），各次尝试加在一起。P 侧是分块 prefill 的块数，被抢占后重做的步也算；D 侧是第一步加上第二步起的步数。
- **排序依据**：总时间（D 官方 E2E）、P 官方 TTFT、P 侧执行最慢（P 侧合计）、D 侧执行最慢（D 侧合计）、TPOT。它决定取哪些请求；表头点击只把已经取出的请求重新排。
- **时间段里的请求超过 20 万个时**，轮次、步数、分段时延分布和单请求拆解里的「窗口 p50」按请求抽样算：每 n 个请求取一个（按请求键的哈希），n 是请求数除以 20 万向上取整，所以查询的内存有上限。抽样的分位数和全量的差在抽样误差以内。
- **分段时延分布**只算这一侧已经结束的请求（有 P 合计或 D 合计）：还在进行的请求只有一部分段，不计入。D 侧「第二步起」各行的 p50、p90、p99、均值是每步的值（每个请求的值除以它第二步起的步数），行名带「每步」；同一行的均值占比和下面的 D 合计是所有步之和。
- **为空的段**：api 网关、P/D LB proxy（要 proxy 补丁）；等 put 完成、start_load_kv 及其子段、wait_for_save 及其子段（这一版日志里没有 KV 传输、KV 池的记录）。看板上为空，不计入分位数。

看板只读 `config_id` 等于这份 `segments.json` 的行（视图 `vllm_pd.phase_line_cur`）。更早版本写的行留在表里，到保留天数自然删掉，不在这一版的看板上出现。

P / D 时间线按 P、D 各自机器的墙钟放：两台机器的钟有偏差时，P 末到 D 首的间隔照实画，可能为负。

## 单个请求的 JSON 和事件

一个请求的全部拆解是一个 JSON 文档（结构 `vllm_pd.request.v2`），一个请求的全部请求级记录是一张表：

```
read -rs CH_PW     # 输入 deploy.env 里的 CH_ADMIN_PASSWORD，屏幕上不显示，也不进 shell 历史
docker exec vllm-pd-clickhouse-<集群名> clickhouse-client --password "$CH_PW" -q "SELECT doc FROM vllm_pd.request_json(id = '<request_id>')"
docker exec vllm-pd-clickhouse-<集群名> clickhouse-client --password "$CH_PW" -q "SELECT ts, side, instance, role, event, duration_ns FROM vllm_pd.request_events(id = '<request_id>') ORDER BY ts"
```

`request_id` 是 proxy 发给 P、D 的 `X-Request-Id`，也就是 vLLM 里 `chatcmpl-<X>-<8 位十六进制>` 中间的 X（`external_req_id` 去掉 `chatcmpl-` / `cmpl-` 前缀）。JSON 的字段说明见 `docs/panel2/REQUEST_JSON.zh-CN.md`。来自 proxy 的值（`session_id`、`route.retries`、`latency_ms.e2e`、`latency_ms.ttft` 等）为 null，列在 `missing` 里；P、D 两侧的官方 E2E、TTFT 在 `latency_ms.official`。

## 映射规则：日志变了怎么改

`segments.json`（version 2）是唯一的映射文件，监控机和节点用同一份：

| 部分 | 内容 | 谁读 |
|---|---|---|
| `line` | `[PD_DIAG]` 标记、只认的记录版本（`record_version`）、日志目录（容器里的路径）、轮转大小、保留小时数、部分行写出间隔、自检间隔 | 过滤器 |
| `roles`、`passthrough` | 进程分类（按进程名；`worker_summary` 出现在 EngineCore 里时按事件名归 worker）、只留的 TP rank、原样写盘的请求级记录（事件名，或事件名加顶层字段条件，如 `core_summary` 的 `scope` 为 `request`） | 过滤器、采集器 |
| `accumulate` | 按请求累加的按步记录：事件名、条件（`scope` / `phase`）、键（`request_ids`）、取值的路径（如 `stages.forward.duration_ns`） | 过滤器 |
| `sides` | 看板和 JSON 的分段树；每段的 `src` 写它来自哪条记录（事件名加条件）的哪个字段，或者哪个累加的合计、首步、其余步，或者按第一步从 ranges 里取的值；`null` 表示这一版日志没有 | 采集器、ClickHouse 视图、看板 |
| `request` | 每个请求的官方 E2E、TTFT、TPOT、token 数、状态、步数来自哪条记录的哪个字段（`member` 表示取 `members[]` 里这个请求那一项） | 采集器 |

只改这个文件就够的：事件或字段改名，条件增减，新增一个「直接取字段」「两个字段相减」「按 `request_ids` 逐步累加」形状的段，换 TP rank。要改代码的：新的归属规则（比如按 token 数分摊）、新的进程类型。改 `config_id` 时，看板只显示新 `config_id` 写的行。

改完：

```
bin/vector_test.sh && bin/apply_segments.sh
```

`bin/apply_segments.sh` 加列、重建视图、重新导入看板、按新规则重启采集器，并把 `segments.json` 下发到所有节点；节点上的过滤器两秒内重读，vLLM 不用重启。新列对旧请求是空的。

## 过滤器的开销

这一版（version 2 记录）的过滤器开销没有测。按步的记录每行 0.9–2.3 KB（样例），过滤器要整行解析才能累加；更早日志格式的测量不适用。

## 磁盘

下面是更早日志格式的估计，version 2 没有重测（这一版的请求级记录每个请求每侧约 33–40 KB，样例实测，比更早的格式大），只能当量级。每个请求的大小来自 mock 在本机 ClickHouse 里的实测（300 秒、821 个请求·侧）：事件表压缩后约 3.0 KB，汇总表约 0.97 KB，节点上的原始文件约 44 KB，其中 D 侧约占 68%、P 侧约占 32%。真实 vLLM 的行比 mock 多几个字段，请求越长部分行越多。磁盘用量随请求率线性变化；一天按 86 400 秒算。

**节点：按每个实例每秒的请求数算。** 一台机器的日志量是它上面各实例之和。每个 D 实例每秒的请求数 = 全集群请求率 ÷ D 实例总数，P 实例同理；内网 mock 默认（`MOCK_RATE` 留空）每个 D 实例每秒 1 个请求。

| 一个实例 | 每个请求的原始文件【由上面的实测推算】 | 每秒 1 个请求时每天【推算】 | 保留 12 小时【推算】 |
|---|---|---|---|
| D 实例 | 44 KB × 68% ≈ 30 KB | 30 KB × 86 400 ≈ 2.6 GB | 约 1.3 GB |
| P 实例 | 44 KB × 32% ≈ 14 KB | 14 KB × 86 400 ≈ 1.2 GB | 约 0.6 GB |

每秒 r 个请求时乘以 r。例如【推算】：

- 一台机器 8 个 D 实例、每个每秒 1 个请求（8 个 D 时 `MOCK_RATE` 留空就是这样）：每天约 8 × 2.6 ≈ 21 GB，保留 12 小时约 10 GB。
- 完整拓扑：全集群 100 请求/秒，64 个 D 实例分在 4 台机器，每个 D 实例 100 ÷ 64 ≈ 1.56 请求/秒，一台 D 机器（16 个实例）每天约 16 × 1.56 × 2.6 ≈ 65 GB，保留 12 小时约 32 GB；8 个 P 实例各在一台机器，每个 12.5 请求/秒，一台 P 机器每天约 12.5 × 1.2 ≈ 15 GB，保留 12 小时约 8 GB。

**实验室的一次实测（11-13 复现 mock 积压时）。** 8 个 D 实例在一台机器上、全集群 100 请求/秒（每个 D 实例 12.5 请求/秒）时，这台机器每分钟写约 0.6 GB 日志，折合每天约 0.6 × 1 440 ≈ 860 GB；按实例和请求率折算，每个 D 实例每秒 1 个请求每天约 860 ÷ 8 ÷ 12.5 ≈ 8.6 GB，是上表推算值 2.6 GB 的 3.3 倍。这时每个 D 实例的请求率远超它能稳定处理的约 1.7–2.3 请求/秒，D 侧一直积压，不是稳态，所以这个数不能按请求率换算到正常负载。它说明的是 `MOCK_RATE` 设得过大时节点的日志盘写满得有多快：按这个速率，保留 12 小时约 860 ÷ 2 = 430 GB。

**监控机：按全集群请求率算。**

| 放在哪 | 内容 | 全集群每秒 1 个请求时每天【推算】 | 全集群 100 请求/秒时每天 | 保留 | 100 请求/秒时合计【估计】 |
|---|---|---|---|---|---|
| ClickHouse | 事件表 `diag_event` | 3.0 KB × 86 400 ≈ 0.26 GB | 约 26 GB | `EVENT_TTL_DAYS` 默认 15 天 | 约 390 GB |
| ClickHouse | 汇总表 `phase_line` 等 | 0.97 KB × 86 400 ≈ 0.084 GB | 约 8.4 GB | `SUMMARY_TTL_DAYS` 默认 30 天 | 约 250 GB |
| 镜像目录 `PULL_DIR` | 各节点日志的副本，等于各节点之和 | 44 KB × 86 400 ≈ 3.8 GB | 约 380 GB | 跟节点一样，默认 12 小时 | 约 190 GB |
| 其他 | 镜像导入后约 3 GB，采集器缓冲最多约 1 GB | — | — | — | 约 4 GB |

监控机合计约 830 GB（100 请求/秒）；请求率是 R 时，前三行乘以 R ÷ 100。`CLICKHOUSE_MIN_DISK_GB`（预检用）默认 1000。保留天数改小，表的大小按比例变小；改了 `deploy.env` 的 TTL 后执行 `bin/apply_segments.sh` 生效。

**现场的实际用量由升级安全检查估。** 「日志量和空闲空间」一项（步骤 18）按拉取程序最近 30 分钟实际传输的字节数和 `line.keep_hours`，估每台节点和监控机镜像盘要保留的日志量，放不下就 `FAIL`，不依赖上面的估计。

## 已知限制

- **只有一份真实日志样本。** 单机 TP1、DP1、4 个请求、`kv_role` 为 null，不是 P/D 分离。P/D 分离下才有的记录（KV 传输、KV 池）样本里没有，这一版对应的段为空；拿到真实 P/D 日志后再加。
- **forward、sampling 是 host 侧耗时。** 补丁记的是 host 上调用返回的时间（`completion=host_return`），不加设备同步。NPU 异步下发时，设备执行时间会落到后面第一个同步点。这一点只有补丁方能改。
- **同一批的请求都记整步。** lookup、alloc、other、forward、sampling 是整步的值，同一步里的每个请求各记一份，不按 token 数分摊。
- **一个 HTTP 请求有几个 prompt 时**，按 `api_summary[http]` 的 `links` 每个请求一行；req_resolve、tokenizer、P 合计、D 合计是整个 HTTP 请求的值，每个请求都记一份。
- **D 的 detokenizer 第一步可能为空。** detokenize 的 ranges 有条数上限，第一步那一条不在里面时，第一步和第二步起都为空，不估算；部署后核验报个数。
- **一个请求有几条 `core_summary[request]` 时**只取最晚的一条，部署后核验报个数。
- **add_request 和 tokenizer 交叠几十微秒**（样例 60–83 µs），照日志显示，P 合计里「未归属」的部分相应少这几十微秒。
- **vLLM 停服时最后不超过 5 秒的累计会丢。** vLLM 停止时强制结束 EngineCore 和 Worker，过滤器的退出处理来不及运行：在途请求最后一次写出（每 5 秒一次）之后的累加、最后一分钟的自检行丢失。
- **P、D 按 X 配对。** 没有 proxy 补丁，P、D 两侧只靠 `X-Request-Id`（即 X）配对：
  - 客户端重复使用同一个 X，不同请求会拼成一个；
  - 重试时 proxy 换了 X，一次请求会变成两个，各缺一侧；
  - 两台机器的钟有偏差时，P / D 时间线上 P 末到 D 首的间隔是错的，可能为负。
  部署后核验报配对比例和间隔分布。
- **只有 TP rank 0。** 要别的 rank，改 `segments.json` 的 `roles.keep_tp_rank`。
- **采集器被强行结束后，事件表里可能有重复的行。** 本包的命令都先停采集器、等它写完再删容器；机器掉电或采集器被 OOM 杀掉时，重启后磁盘缓冲里最后一批行会再写一次，「单请求事件」里同一条记录（`record_id` 相同）出现两次。汇总表和看板上的各段、分位数不受影响：它们按请求取最大值、最新的累计行和最晚的记录。
- **`[PD_DIAG]` 行不再进 stdout。** 启用后，`[PD_DIAG]` 行从重定向出来的 stdout 日志里去掉，改写进过滤器的文件，读 serving.log 的脚本拿不到这些行。
- **磁盘和过滤器开销没有按这一版重测**，见「磁盘」「过滤器的开销」。
- **从 v1.0 回滚以后**，v1.0 的看板不看 `config_id`：v2.0 运行期间写入的请求，在 v1.0 看板上那段时间里只有 v2.0 也有的段有值，其余为空。

## 改拓扑和速率

| 要改什么 | 怎么做 |
|---|---|
| 加减 P、D 实例或机器 | 改 `inventory.json`，执行 `bin/nodes.sh paths`、`bin/nodes.sh push`，内网再执行 `bin/nodes.sh start`；然后 `bin/puller.sh restart`、`bin/central_up.sh` |
| mock 的请求率 | 改 `deploy.env` 的 `MOCK_RATE`，执行 `bin/nodes.sh start`。每个 D 实例不超过 1.5 请求/秒（步骤 5）；留空时跟着清单里的 D 实例总数走 |
| 保留天数 | 改 `EVENT_TTL_DAYS`、`SUMMARY_TTL_DAYS`，执行 `bin/apply_segments.sh` |
| 节点日志保留多久 | 改 `segments.json` 的 `line.keep_hours`，执行 `bin/apply_segments.sh` |

## 停止和卸载

都在监控机上、集群目录里执行，只动这一个集群。`bin/nodes.sh` 后面不写地址时是本集群的所有机器，写地址时只动这几台。整个卸载一个集群用 `bin/panel2_cluster.sh remove <集群名>`。

| 要做什么 | 命令 |
|---|---|
| 看每台机器的 mock、节点文件和日志。内网时按 mock 进程数判：不等于这台的实例数就 `FAIL` | `bin/nodes.sh status` |
| 看每台机器拉取得怎么样 | `bin/puller.sh status` |
| 停 mock，文件留着。监控机经 SSH 先正常结束，10 秒内没有结束的强制结束，确认进程数为 0 才算停好；强制结束的，最后几秒的数据会少 | `bin/nodes.sh stop` |
| 再启动 mock | `bin/nodes.sh start` |
| 内网：停 mock，删除本包放在节点上的全部文件（含 mock 的日志和三个文件） | `bin/nodes.sh clean` |
| 模型推理环境：只删 `~/vllm-timing-node`；三个文件和日志目录属于 vLLM，先去掉 vLLM 的两个环境变量并重启，再手工删 | `bin/nodes.sh clean` |
| 停拉取程序 | `bin/puller.sh stop` |
| 清空看板的请求数据，表、看板和过滤器的文件记录留着（见下面「清空数据」） | `bin/clear_data.sh --yes` |
| 换环境时清空全部数据（五张表） | `bin/clear_data.sh --yes --all` |
| 卸载面板二，数据留着 | `bin/monitor_down.sh` |
| 卸载面板二，连数据一起删；没有别的集群时连插件一起删（Grafana 重启一次） | `bin/monitor_down.sh --purge` |

**清空数据（`bin/clear_data.sh`）。**

| 用法 | 清的表 | 另外清的 | 保留 |
|---|---|---|---|
| `bin/clear_data.sh --yes`（默认） | `phase_line`、`diag_event`（请求数据） | 采集器的解析失败样本（`dead-letter`） | `filter_file`、`filter_selfcheck`、`filter_other` |
| `bin/clear_data.sh --yes --all` | 五张表都清 | 同上 | — |

- **默认只清请求数据。** 过滤器的文件记录留着，清完以后检查不会误报「没有过滤器写的文件」，也不用重启 mock 或 vLLM。`--all` 只在换环境时用，清完要重启 mock 或 vLLM。
- **内网（mock）。** 先由监控机停各节点的 mock，删掉 mock 写在节点上的日志，再停拉取程序，等采集器的缓冲写完，在采集器停着的时候清表和镜像目录，最后启动采集器、拉取程序和 mock。镜像目录逐个文件删、限速，打印进度。不先删节点上的日志，拉取程序一启动就会把旧文件原样拉回镜像。清镜像是这个模式的默认，`--with-mirror` 仍然接受，效果相同。
- **模型推理环境（vllm）。** 停拉取程序，等缓冲写完，在采集器停着的时候清表，再启动采集器和拉取程序；不清镜像，不动节点，不动 vLLM。带 `--with-mirror` 时写明原因后退出，什么都不改：节点上的文件属于 vLLM，按 `line.keep_hours` 保留，镜像删掉以后会被拉取程序原样拉回。
- **模型推理环境里不删节点上的日志。** vLLM 一直开着这些文件在写，删掉以后它继续写进已经删除的文件，新数据到不了看板，要等这个文件写满 256 MiB 换新文件，或者重启 vLLM，才恢复。`bin/clear_data.sh` 在这个模式下不动节点，`bin/nodes.sh clean` 在这个模式下也不删日志。

卸载不影响面板一和别的集群：看板 JSON 不动，本集群的 `prometheus.yml` 只去掉本包加的那一段；注册表里还有别的集群时，插件保留，Grafana 不重启。

机器重启后：监控机上的 ClickHouse 和采集器容器会自己起来，拉取程序不会，重新执行 `bin/puller.sh start`（没有运行时有告警 `VllmTimingPullerDown`）。内网节点上的 mock 也不会，重新执行 `bin/nodes.sh start`。

## 排查

| 现象 | 原因和处理 |
|---|---|
| 看板下拉框是空的，或者 panel 报 `IN ()` | 所选时间范围内没有数据。放大时间范围；数据断在哪一段，`bin/diag.sh chain` 逐段报出 |
| Prometheus 告警 `VllmTimingParseFailed` | 有日志行解析不了。告警的 `cluster` 标签是出问题的集群；`docker exec vllm-pd-vector-<集群名> sh -c 'tail -n 3 /var/lib/vector/dead-letter-*'` 里有原始行 |
| Prometheus 告警 `VllmTimingBufferFilling` | 采集器写 ClickHouse 堵住了。检查 ClickHouse 是否在运行、磁盘是否满。恢复后自动补写 |
| Prometheus 告警 `VllmTimingCentralDown` | 采集器访问不到。`docker ps -a` 看 `vllm-pd-vector-<集群名>`，`docker logs --tail 20 vllm-pd-vector-<集群名>` |
| Prometheus 告警 `VllmTimingPullerDown` | 拉取程序没有运行。执行 `bin/puller.sh start`。日志还在节点上，恢复后补上（节点上保留 `line.keep_hours` 小时） |
| Prometheus 告警 `VllmTimingPullStale` | 这台机器超过 2 分钟没有拉取成功。`bin/puller.sh status` 那一行有报错 |
| Prometheus 告警 `VllmTimingNoLogLines` | 这台机器 10 分钟没有新日志：mock 或 vLLM 没在写，或者 `NODE_LOG_DIR` 不对。进程写过第一行之后，过滤器每分钟写一行自检，没有请求时也有新行 |
| `bin/puller.sh status` 报 `Permission denied` | SSH 用户读不了日志目录。容器里的 vLLM 用户写的文件，要让 SSH 用户能读 |
| `central_up.sh` 报采集器配置有错 | 改过 `segments.json` 时，先执行 `bin/vector_test.sh` |
| 某个段全是「—」 | 这个段在 `segments.json` 里 `src` 为 `null`（这一版没有），或者事件、字段名和真实日志对不上 |
| 镜像目录占得比预期多 | 镜像跟着节点上的文件走。改小 `line.keep_hours` |

采集链路的告警在本集群 Prometheus 的页面 `PROMETHEUS_URL/alerts`（留空时 `http://<监控机>:<端口基数+2>/alerts`）看。

### 诊断工具（`bin/diag.sh`）

看板空了、数据断了，或者切到模型推理环境以后，在集群目录里执行 `bin/diag.sh <子命令>`，它报出哪一段不对、怎么修。除 `pair` 以外都只读：ClickHouse 的查询带 `--readonly=1`，节点上只执行读的命令；`pair` 经 proxy 发 3 个请求。报告写在 `~/vllm-pd-diag-reports/<集群名>/`，不写进集群目录。容器名和标准输出文件取 `deploy.env` 的 `NODE_VLLM_CONTAINERS`、`NODE_VLLM_STDOUT_FILES`，没填时自己找。

| 子命令 | 查什么 |
|---|---|
| `all` | 依次执行 `nodes`、`vllm`（模型推理环境）、`chain`、`monitor`，`FAIL`、`WARN` 排在前面。出问题时先执行它 |
| `nodes` | 每台节点：时钟差、内存、日志盘空闲和按拉取字节数估的 12 小时日志量、mock 进程数、三个节点文件和监控机是否相同 |
| `vllm` | 每个 vLLM 容器：挂载、主进程和子进程的环境变量、变量指向的目录里有没有三个文件、过滤器创建了没有、要不要重启；最后一行是结论 |
| `chain` | 数据经过的每一段：节点日志的最新一行、拉取程序、监控机上的镜像、ClickHouse 里每个实例的最新一行、采集器的积压和解析失败 |
| `monitor` | 监控机：各盘的空闲空间、容器状态、拉取程序、Prometheus 的目标、Grafana |
| `history "<起>" "<止>"` | 一段时间里每台拉到的字节数、Prometheus 各抓取任务的 `up`、解析失败，用来查数据什么时候断的 |
| `log <文件>` | 一份 vLLM 日志的格式，面板二读的记录类型在不在 |
| `pair <proxy 地址:端口>` | 经 proxy 发 3 个请求，看同一个请求在 P、D 两侧能不能配上 |

参数、输出的读法和每种结论的处理，见 `docs/诊断工具说明.zh-CN.md`。

## 附录 A：ClickHouse 放在别的机器上

默认和 Grafana 放在同一台机器。要放在别的机器：

1. 把部署包和该架构的离线包拷到那台机器，解开，`docker load`；
2. `deploy.env` 里：`CLICKHOUSE_ADDR` 写那台机器的 IP；
3. 在那台机器上执行 `docker network create <DOCKER_NETWORK>`、`bin/clickhouse_up.sh`；
4. 其余步骤仍在监控机上做，采集器仍在监控机，监控机要能访问那台机器的 `CLICKHOUSE_HTTP_PORT` 端口。

## 附录 B：离线包里有什么

| 文件 | 说明 |
|---|---|
| `clickhouse-image.tar` | ClickHouse 26.8.36.3 镜像，这个架构的 |
| `clickhouse-aarch64v80compat` 或 `clickhouse-amd64compat` | 兼容版 ClickHouse，官方版跑不了时才用 |
| `grafana-clickhouse-datasource-4.23.4.linux_*.zip` | Grafana 插件，这个架构的 |
| `vector-image.tar` | 采集器（Vector 0.61.0）镜像，这个架构的 |
| `python-3.12.16-*-unknown-linux-gnu.tar.gz` | 机器上没有 Python 3.9 时用，两种架构都有 |
| `SHA256SUMS`、`VERSIONS.txt` | 校验和、版本和来源 |

## 附录 C：日志的样子

节点上，每个实例一个目录，每个进程、每类角色一个文件：

```
<NODE_LOG_DIR>/<实例>/<角色>.<进程启动时间>.<pid>.<序号>.log      角色：api、engine、worker
```

写满 `line.rotate_mib`（默认 256 MiB）就换一个新文件名，旧文件不改名；超过 `line.keep_hours` 的删掉。每个文件第一行是 `pd_file_open`，写明文件名，所以不会有两个文件第一行相同。

每一行：

```
2026-11-11T09:30:01.123+08:00 (EngineCore_DP0 pid=4711) INFO [pd_diagnostics.py:156] [PD_DIAG] {"request_id":"chatcmpl-<X>-1a2b3c4d",...,"scope":"request",...,"version":2,...,"event":"core_summary"}
```

时间是带时区偏移的 ISO 8601，到毫秒。`[PD_DIAG]` 之后是补丁写的 JSON，原样不动。补丁写七类记录，节点上只有请求级的四类写盘：

| 记录 | 每个请求几条 | 写盘 | 面板二用到的 |
|---|---|---|---|
| `api_summary`，`scope` 为 `http` | 每个 HTTP 请求一条，最后写 | 是 | `stages.req_resolve`、`render`、`tokenize`，`start_ns`、`end_ns`，`http_status`、`status`，`links` 里的 `engine_submission` |
| `api_summary`，`scope` 为 `engine_submission` | 每个引擎请求一条 | 是 | `stages.add_request`，`links` 里的 `request_id` |
| `api_summary`，`scope` 为 `request` | 每个引擎请求一条 | 是 | `official.*`，`terminal.detokenize_duration_ns`，`stages.detokenize.ranges[]` |
| `core_summary`，`scope` 为 `request` | 每个引擎请求一条 | 是 | `stages.queue.initial_wait_ns`、`stages.hash.duration_ns`，`members[].executions` |
| `core_summary`，`scope` 为 `execution` | 每步一条 | 否，按请求累加 | `stages.lookup`、`alloc`、`other`，`request_ids`，`execution_id` |
| `worker_summary`，`phase` 为 `execute` | 每步每个 TP rank 一条 | 否，rank 0 按请求累加 | `stages.forward`，`request_ids`，`tp_rank` |
| `worker_summary`，`phase` 为 `sample` | 每步每个 TP rank 一条 | 否，rank 0 按请求累加 | `stages.sampling`，`request_ids`，`tp_rank` |

过滤器自己写三种行：`pd_seg_partial`（一个请求在这个进程里的累加，`segs` 里每段的合计、步数、首步值，`first_exec` 是这个请求的第一步；同一个 `part` 的行是累计值，取最后一行；不同 `part` 相加）、`pd_filter_selfcheck`（每分钟一行，处理的行数、丢弃数、版本不对丢掉的行数、累加数、出错数、在途请求数）、`pd_file_open`。

包里 `samples/` 下有一份 P0、D0 的全量逐事件样本、它的真值和清单，节点自检用它。
