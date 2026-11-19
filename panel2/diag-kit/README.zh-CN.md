# 面板二诊断工具（已填路径）

本工具诊断内网监控机上已安装的面板二 v2.0 集群 `glm53-1`，mock 模式和 vllm 模式都适用。它回答「环境哪里不对」，不判断数据准不准，也不拦升级。

**只读：** 除 `pair` 以外，各子命令只读：
- 不写集群目录（包括 `generated/`），不调用 `verify.sh`；
- 不重启、不停止、不修改任何服务。

报告只写在工具自己的 `reports/` 目录。`pair` 会经 proxy 发 3 个短请求，见第 8 节。

## 概要

| 情况 | 命令（在监控机上，工具目录里执行） | 看哪一行 |
| --- | --- | --- |
| 看板空了、数据断了、升级前自查 | `./diag.sh all` | 最后一行 `RESULT`；`FAIL` 行 `→` 后面是修法 |
| vllm 模式下 `[PD_DIAG]` 仍在标准输出，或日志目录没有文件 | `./diag.sh vllm` | 每个容器的「结论」一行 |
| 看板上请求只有 P 或只有 D | `./diag.sh pair <proxy 地址:端口>` | `OK` / `FAIL` 一行 |
| 事后查某一段时间断没断 | `./diag.sh history "2026-11-13 10:30" "2026-11-13 12:00"` | 各台、各 job 的 `WARN` |
| 拿到一份新的 vLLM 日志，要知道格式变没变 | `./diag.sh log <日志文件>` | `FAIL` 和「和已知样本不同」几行 |

下文的地址和路径：
- 节点：`<P 节点地址>` 上是 P0、P1，`<D 节点地址>` 上是 D0–D7；
- 集群目录：`/data/grafana/vllm-pd-grafana-panel2/glm53-1`；
- 工具目录：`<用户目录>/vllm-pd-panel2-diag`。

样例输出来自实验室，地址 `10.99.1.1`（P）、`10.99.2.1`（D）、`10.99.0.10`（proxy）都是实验室地址。

## 1. 放置和准备

### 1.1 拷贝和校验

把 Caching 仓库 ServingBench 分支的 `面板二诊断工具/` 整个目录拷到监控机的 `<用户目录>/vllm-pd-panel2-diag/`，然后校验：

```bash
cd <用户目录>/vllm-pd-panel2-diag && grep -E '^[0-9a-f]{64}  ' MANIFEST.txt | sha256sum -c && chmod +x diag.sh diag.py
```

过：每一行都是 `: OK`。

### 1.2 工具自己的配置 `diag.conf`

工具新增的配置项写在工具目录的 `diag.conf` 里，不写进集群的 `deploy.env`。命令行上的 `--cluster-dir`、`--containers`、`--stdout-file` 优先于 `diag.conf`。

```bash
cd <用户目录>/vllm-pd-panel2-diag && cp -n diag.conf.example diag.conf && vi diag.conf
```

| 配置项 | 填什么 | 不填时 |
| --- | --- | --- |
| `CLUSTER_DIR` | 集群目录，已填 `/data/grafana/vllm-pd-grafana-panel2/glm53-1` | 用这个默认值 |
| `VLLM_CONTAINERS` | 每台节点上 vLLM 的容器名，格式 `<P 节点地址>=<P 的容器名>,<D 节点地址>=<D 的容器名>`；一台有几个容器时，容器名之间用分号 | `vllm` 写 `WARN`「没有核对真实容器」，容器里的各项不查 |
| `VLLM_STDOUT_FILES` | 启动脚本把 vLLM 的标准输出 `tee` 进文件、`docker logs` 看不到时，填 `地址=文件路径` | 标准输出只看 `docker logs` |
| `UPDATE_SCRIPTS` | 更新脚本的路径，可以用通配符，已填 `<用户目录>/vllm-pd-update-v*/vllm_pd_update*filled.py` | `monitor` 不列更新脚本 |

容器名在节点上这样查：

```bash
docker ps --format '{{.Names}} {{.Image}}'
```

### 1.3 运行用户和权限

在监控机上，用平时执行 `bin/panel2_cluster.sh` 的那个用户执行。工具需要的权限：

- **监控机：**
  - 读集群目录里的 `deploy.env`、`inventory.json`、`segments.json`；
  - 能执行 `docker exec` 进 ClickHouse 容器（`vllm-pd-clickhouse-glm53-1`），查询时加 `--readonly=1`；
  - 能访问本机的 Prometheus、Grafana 和拉取程序的端口。
- **节点：**
  - SSH 用户、端口、密钥和 `bin/nodes.sh` 相同，取自 `deploy.env`；
  - 查容器时，SSH 用户要能执行 `docker`：在 docker 组里，或能免密 `sudo docker`；
  - 查内存耗尽和磁盘满时，要能读系统日志：root 或 `adm` 组。

Python 用监控机上已有的 3.6 及以上版本，不需要装别的包。`diag.sh` 会自己找 Python，并且不生成 `__pycache__`。

## 2. 子命令一览

| 子命令 | 查什么 | 什么时候用 | 实验室用时 |
| --- | --- | --- | --- |
| `all` | 依次执行 `nodes`、`vllm`（vllm 模式，或给了容器名时）、`chain`、`monitor`；屏幕上最多 40 行，`FAIL`、`WARN` 优先 | 第一步；升级前自查 | 3.4 秒 |
| `nodes` | 每台节点：<br>· 开机时间、时钟差、内存；<br>· 日志盘空闲，按拉取字节数估 12 小时日志量；<br>· mock 进程数，以及主循环卡没卡；<br>· 三个节点文件的 sha256 和监控机比；<br>· 系统日志里的内存耗尽和磁盘满；<br>· 两台的 `/data` 是不是同一块盘 | mock 不写、节点重启过、怀疑磁盘满 | 0.5 秒 |
| `vllm` | 每个 vLLM 容器：<br>· 挂载；<br>· 主进程和子进程的环境变量；<br>· 变量指向的目录里有没有三个文件；<br>· 过滤器创建了没有；<br>· 标准输出里这次启动以后还有没有 `[PD_DIAG]`；<br>· 启动时的 logging 报错；<br>· 节点文件下发以后有没有重启。<br>最后给一行「结论」，见第 5 节 | 切到 vllm 模式以后；看板在 vllm 模式下没有数据 | 2.5 秒（2 台） |
| `chain` | 数据经过的每一段：<br>· 节点日志的最新一行；<br>· 拉取程序；<br>· 监控机上的镜像；<br>· ClickHouse 每个实例的最新一行；<br>· 采集器的缓冲积压、解析失败；<br>· 过滤器出错数；<br>· 过滤器丢掉的空键记录数（取键字段为空的记录，各进程从启动累计） | 看板有一部分实例空 | 0.9 秒 |
| `monitor` | 监控机：<br>· 镜像盘、ClickHouse 和 Prometheus 的数据盘；<br>· 各容器的状态、重启次数、内存耗尽；<br>· 拉取程序；<br>· Prometheus 的目标、Grafana 的健康状态；<br>· 内存；<br>· 更新脚本 | 看板整个打不开、某个面板没有数 | 0.2 秒 |
| `history "<起>" "<止>"` | 一段时间里：<br>· 每台拉到的字节数什么时候降到 0；<br>· 每个 Prometheus job 什么时候 `up` 为 0、抓取耗时；<br>· 解析失败。<br>时间是监控机的本地时间，6 小时以内每 60 秒一个点，更长每 300 秒一个点 | 事后查「昨晚几点断的」 | 0.1 秒（40 分钟） |
| `log <文件> [--save-profile]` | 一份 vLLM 日志：<br>· 格式校验；<br>· 面板二读的 9 种记录在不在：原有的 7 种缺一种就是 FAIL；新补丁的 `pool_task[task]` 只在 P 侧有，`pd_transfer_summary[request_generation]` 只在 D 侧有，两种都没有时是 WARN（这份日志不是补丁 01、02 写的）；<br>· 共用缓存池任务（多个请求共用、只带 `request_ids` 的 `pool_task`）的条数；<br>· 和已知样本（`profiles/` 里 11-12 的 `server.log`、11-16 rc1 的 `P.log` 和 `D.log` 的统计，取记录种类最接近的一份）比，多了或少了哪些记录类型、进程类型、字段 | 同事给了新日志；vLLM 或补丁升级以后 | 随文件大小 |
| `pair <proxy 地址:端口> [--model M] [--wait 秒]` | 经 proxy 发 3 个请求，看同一个请求在 P、D 两侧能不能配上 | 看板上请求只有 P 或只有 D | 最多 3 分钟（`--wait` 默认 180 秒） |

## 3. 命令写法

以下命令都在监控机上执行。

```bash
cd <用户目录>/vllm-pd-panel2-diag && ./diag.sh all
```

```bash
cd <用户目录>/vllm-pd-panel2-diag && ./diag.sh nodes
```

容器名没有写进 `diag.conf` 时，在命令行上给：

```bash
cd <用户目录>/vllm-pd-panel2-diag && ./diag.sh --containers <P 节点地址>=<P 的容器名>,<D 节点地址>=<D 的容器名> vllm
```

标准输出在文件里时：

```bash
cd <用户目录>/vllm-pd-panel2-diag && ./diag.sh --stdout-file <D 节点地址>=<D 的标准输出文件> vllm
```

```bash
cd <用户目录>/vllm-pd-panel2-diag && ./diag.sh chain
```

```bash
cd <用户目录>/vllm-pd-panel2-diag && ./diag.sh monitor
```

```bash
cd <用户目录>/vllm-pd-panel2-diag && ./diag.sh history "2026-11-13 10:30" "2026-11-13 12:00"
```

`log` 的日志文件先拷到监控机，比如 `<用户目录>/logs/`：

```bash
cd <用户目录>/vllm-pd-panel2-diag && ./diag.sh log <用户目录>/logs/prefill_v32.log --save-profile
```

`--save-profile` 把这份日志的统计存成 `reports/profiles/prefill_v32.log.json`。文件里只有计数和字段名，不含日志内容，可以发回。

```bash
cd <用户目录>/vllm-pd-panel2-diag && ./diag.sh pair <proxy 地址:端口>
```

模型名默认取 `inventory.json` 里的 `model`；proxy 要求别的名字时加 `--model <模型名>`。

集群不在默认目录时，在子命令前加 `--cluster-dir <集群目录>`。

## 4. 摘要怎么看

### 4.1 格式

- **第一行：** 集群、集群目录、模式，以及「只读」或「会发 3 个请求」。
- **每段的标题：** 以 `==` 开头。
- **每一行：** 级别加内容。

| 级别 | 意思 |
| --- | --- |
| `OK` | 这一项正常 |
| `WARN` | 可能有问题，或者没法核对，比如没有权限、没有给容器名；不影响别的项 |
| `FAIL` | 这一项不对；`→` 后面是修法 |
| `INFO` | 只是记录，比如路径、版本、请求数 |

- **最后两行：**
  - `报告：<路径>`：完整报告，权限 0600，里面有每条远程命令的原始输出，诊断时把它发回；
  - `RESULT: OK`、`RESULT: WARN（n 项）`或 `RESULT: FAIL（n 项）`。
- **退出码：** 有 `FAIL` 时为 1，否则为 0。
- **报告里有什么：**
  - 有：进程命令行、挂载、文件的 sha256，以及 7 个变量的值（`VLLM_LOGGING_CONFIG_PATH`、`PYTHONPATH`、`VLLM_CONFIGURE_LOGGING`、`PD_DIAG_INSTANCE`、`PD_DIAG_INSTANCE_SOURCE`、`PD_DIAG_MAPPING`、`PD_DIAG_LOG_DIR`）；
  - 没有：密码、别的环境变量、请求内容。

### 4.2 样例：mock 模式的 `all`

实验室真实输出，mock 模式，2 台节点：

```text
diag.sh all：集群 glm53-1（/data/grafana/vllm-pd-grafana-panel2/glm53-1，mock 模式），只读
== nodes（2 台）
OK   10.99.1.1 开机 2026-11-12 14:09:49；时钟 +0.4 秒；内存 available 11.5 GB
OK   10.99.1.1 日志盘 /data/pd-diag/logs：空闲 310.4 GB（/dev/vda1[/docker/volumes/t33-data-p/_data] ext4   /data，已用 28%）；按最近 30 分钟的写入估 12 小时日志 443.8 MB
OK   10.99.1.1 mock 进程 2 个，应为 2 个（P0 P1）
OK   10.99.1.1 三个节点文件的 sha256 和监控机相同（segments.json b385648f）
OK   10.99.2.1 开机 2026-11-12 14:09:49；时钟 +0.2 秒；内存 available 11.5 GB
OK   10.99.2.1 日志盘 /data/pd-diag/logs：空闲 310.4 GB（/dev/vda1[/docker/volumes/t33-data-d/_data] ext4   /data，已用 28%）；按最近 30 分钟的写入估 12 小时日志 1.1 GB
OK   10.99.2.1 mock 进程 8 个，应为 8 个（D0 D1 D2 D3 D4 D5 D6 D7）
OK   10.99.2.1 三个节点文件的 sha256 和监控机相同（segments.json b385648f）
INFO /data 各台来源不同（10.99.1.1 /dev/vda1[/docker/volumes/t33-data-p/_data]，10.99.2.1 /dev/vda1[/docker/volumes/t33-data-d/_data]）
== chain（节点日志 → 镜像 → 采集器 → ClickHouse）
OK   10.99.1.1 节点上各实例最新一行落后：最多 1 秒
OK   10.99.2.1 节点上各实例最新一行落后：最多 2 秒
OK   拉取：2 台，最久的 1 秒前成功
OK   镜像 10.99.1.1：最新文件 2 秒前改过
OK   镜像 10.99.2.1：最新文件 1 秒前改过
OK   ClickHouse：10 个实例，最新一行最多落后 12 秒（vllm_pd.phase_line_cur）
OK   采集器缓冲：5 个输出共 0 条积压
OK   解析失败：最近 10 分钟 0 行；dead-letter 0 行
OK   过滤器：10 个进程的出错数都是 0
INFO 最近 15 分钟 2337 个请求
== monitor（监控机）
OK   镜像盘 /data/grafana/vllm-timing/glm53-1/pull：空闲 310.4 GB；各节点 12 小时日志估 1.6 GB
OK   ClickHouse 数据（/var/lib/docker/volumes/vllm-pd-clickhouse-glm53-1-data/_data）：已用 31%，空闲 310.4 GB
OK   Prometheus 数据（/var/lib/docker/volumes/vllm-pd-prometheus-data-glm53-1/_data）：已用 31%，空闲 310.4 GB
OK   容器 vllm-pd-clickhouse-glm53-1 在运行，启动于 2026-11-13 05:57:09 UTC，重启 0 次
OK   容器 vllm-pd-vector-glm53-1 在运行，启动于 2026-11-13 05:57:12 UTC，重启 0 次
OK   容器 vllm-pd-prometheus-glm53-1 在运行，启动于 2026-11-13 05:54:59 UTC，重启 0 次
OK   容器 obs-grafana 在运行，启动于 2026-11-13 05:57:16 UTC，重启 0 次
OK   拉取程序在运行
WARN Prometheus：17 个目标（7 个 job），15 个 down：mooncake-master 10.99.1.1:9003（Get "http://10.99.1.1:9003/metrics": dial tcp 10.9）；（后略）
OK   Grafana 正常
INFO 面板二 v2.0，节点模式 mock，过滤器版本应为 pd-diag-v2-2026-11-13@eff1c34a
报告：/root/diag-tool/reports/diag-all-20261113-060220.txt
RESULT: WARN（1 项）
```

唯一的 `WARN` 是面板一的 exporter：实验室里没有这些 exporter，在内网应为 `OK`。

### 4.3 样例：vllm 模式的 `vllm`，一切正常

实验室真实输出。P 是 2 个 `vllm serve`、18 个子进程；D 是 8 个 `vllm serve`、24 个子进程：

```text
diag.sh vllm：集群 glm53-1（/data/grafana/vllm-pd-grafana-panel2/glm53-1，vllm 模式），只读
== vllm（vllm 模式）
INFO 容器里外同路径：三个文件 /data/pd-diag/conf，日志 /data/pd-diag/logs
INFO 10.99.2.1 容器 t33-vllm-d 在运行，启动于 2026-11-13T06:51:54，重启 0 次
OK   10.99.2.1 t33-vllm-d：宿主机的 /data/pd-diag/conf 不是直接挂进来的（挂载类型 bind、volume），但容器里 /data/pd-diag/conf 的三个文件和下发的相同
INFO 10.99.2.1 t33-vllm-d：宿主机的 /data/pd-diag/logs 不是直接挂进来的（挂载类型 bind、volume）；过滤器写的文件有没有出现在宿主机，看「日志目录 3 分钟内都有新写入」一行
INFO 10.99.2.1 t33-vllm-d：vLLM 进程 32 个（主进程 8 个，子进程 24 个）
OK   10.99.2.1 t33-vllm-d：主进程和 24 个子进程都有 VLLM_LOGGING_CONFIG_PATH
OK   10.99.2.1 t33-vllm-d：VLLM_LOGGING_CONFIG_PATH=/data/pd-diag/conf/pd_diag_logging.json，目录里三个文件都在，和监控机相同
OK   10.99.2.1 t33-vllm-d：子进程带 PD_DIAG_INSTANCE_SOURCE=argv，主进程在起子进程之前已经加载了过滤器
OK   10.99.2.1 t33-vllm-d：标准输出里的 34779 行 [PD_DIAG] 都是 vLLM 这次启动（11-13 06:51:54）以前的（最后一行 2026-11-13 06:24:50 UTC），启动以后没有
OK   10.99.2.1 t33-vllm-d 结论：日志配置生效，过滤器在写，标准输出里没有新的 [PD_DIAG]（34779 行是这次启动以前的）
OK   10.99.2.1 8 个实例的日志目录 3 分钟内都有新写入
OK   过滤器版本：10 个实例 52 个进程都是 pd-diag-v2-2026-11-13@10dc8087
报告：/root/diag-tool/reports/diag-vllm-20261113-065222.txt
RESULT: OK
```

P 那台的各行同 D，样例里省略了。

「不是直接挂进来的」：
- 实验室的节点本身是容器，`/data` 来自 docker 卷，不是宿主机目录的直接映射，所以工具改用证据判断：
  - 容器里三个文件的 sha256 和下发的相同；
  - 过滤器写的文件出现在宿主机的日志目录里。
- 内网若是 `-v /data:/data` 直接映射，这两行是 `OK … 在容器里同样是 …`。

「34779 行是这次启动以前的」：
- 这些是修好以前写进标准输出的旧行，`docker logs` 会一直保留，不算问题；
- 判「以后」用的时间是 vLLM 主进程的启动时间加 10 秒，因为 `docker logs` 给重启前最后几行打的时间会晚 1 秒左右。

## 5. `vllm` 的结论

每个容器最后一行是「结论」。结论按下表从上到下取第一条成立的：

| 级别 | 结论（原文开头） | 意思 | 怎么修 |
| --- | --- | --- | --- |
| `FAIL` | vLLM 没有重启，新下发的节点文件没生效 | vLLM 主进程的启动时间早于节点文件的下发时间（`segments.json` 的 ctime） | 重启 vLLM |
| `FAIL` | 日志配置被关掉（`VLLM_CONFIGURE_LOGGING=0`） | 进程里有 `VLLM_CONFIGURE_LOGGING=0`，没有 `VLLM_LOGGING_CONFIG_PATH` | 去掉这个变量，加上两行 `export`，重启 vLLM |
| `FAIL` | 变量没有进到 vLLM 进程 | 主进程没有 `VLLM_LOGGING_CONFIG_PATH`，这次启动以后过滤器也没写过文件；多半是 `export` 写在了宿主机上，或者不在起 vLLM 的那个 shell 里 | 两行 `export` 加在容器里执行 `vllm serve` 的启动脚本里，重启 vLLM |
| `FAIL` | 子进程没有继承变量 | 主进程有变量，子进程（EngineCore、Worker）没有 | 变量要在 `vllm serve` 启动前就设在它的环境里，不要在进程里改环境变量后再起子进程；重启 vLLM |
| `FAIL` | 变量指向的目录里没有三个文件 | `VLLM_LOGGING_CONFIG_PATH` 所在目录里缺 `pd_diag_logging.json`、`pd_diag_logfilter.py`、`segments.json` 之一 | `export` 改成容器里的 `/data/pd-diag/conf/…`，重启 vLLM |
| `FAIL` | 日志配置加载了、过滤器已经创建，但 `[PD_DIAG]` 仍在标准输出 | 过滤器在，记录却没有经过它：发这些记录的 logger 不在 `vllm` 之下 | 查补丁 `pd_diagnostics.py` 用的 logger 名，把它加进 `pd_diag_logging.json` 的 `loggers` |
| `OK` | 日志配置生效，过滤器在写，标准输出里没有新的 `[PD_DIAG]` | 正常 | — |
| `WARN` | 日志配置加载了、过滤器已经创建，这次启动以后还没有 `[PD_DIAG]` 记录 | vLLM 重启以后还没有请求。过滤器要等第一条记录才建文件，之后每 60 秒写一行自检 | 有请求以后再跑一次 `diag.sh vllm` |
| `WARN` | vLLM 启动不到 70 秒 | 刚启动，分不清 | 1 分钟后再跑一次 |
| `FAIL` | 变量和文件都对，但这次启动以后日志目录里没有新文件 | 过滤器没有起来 | 看 vLLM 的启动日志里有没有 logging 的报错 |
| `WARN` | 组合没见过，原样写下 | 以上都不成立；后面跟着五个原始信号（S1 环境变量、S2 启动与下发时间、S3 文件、S4 启动后写过的文件、S5 标准输出） | 把报告发回 |

结论之外还有两种 `FAIL` 行，出现时 vLLM 多半没有起来，结论一行不出现：

- **「vLLM 启动时的报错：…」：** 原样摘自容器的标准输出，比如路径不存在：`RuntimeError: Could not load logging config. File does not exist: …`。
- **「容器里没有找到 vLLM 进程」：** 容器在，但里面没有命令行含 `vllm` 的进程。

### 5.1 样例（实验室真实输出，D 那台）

变量写在宿主机上：

```text
FAIL 10.99.2.1 t33-vllm-d：vLLM 这次启动（11-13 06:52:23）10 秒以后，标准输出里有 1723 行 [PD_DIAG] → 看结论一行
FAIL 10.99.2.1 t33-vllm-d 结论：变量没有进到 vLLM 进程：export 在容器外，或不在起 vLLM 的那个 shell 里 → 加在容器里执行 vllm serve 的启动脚本里，重启 vLLM
```

只有主进程有变量：

```text
FAIL 10.99.2.1 t33-vllm-d 结论：子进程没有继承变量：24 个子进程没有 VLLM_LOGGING_CONFIG_PATH（主进程有）→ 变量要在 vllm serve 启动前设在它的环境里，不要在进程里改环境变量后再起子进程；重启 vLLM
```

`VLLM_CONFIGURE_LOGGING=0`：

```text
FAIL 10.99.2.1 t33-vllm-d 结论：日志配置被关掉（VLLM_CONFIGURE_LOGGING=0）→ 去掉这个变量，加上两行 export，重启 vLLM
```

`VLLM_CONFIGURE_LOGGING=0` 又设了路径，vLLM 起不来：

```text
FAIL 10.99.2.1 t33-vllm-d：vLLM 启动时的报错：RuntimeError: VLLM_CONFIGURE_LOGGING evaluated to false, but VLLM_LOGGING_CONFIG_PATH was given. VLLM_LOGGING_CONFIG_PATH implies VLLM_CONFIGURE_LOGGING. …
FAIL 10.99.2.1 t33-vllm-d：容器里没有找到 vLLM 进程（命令行含 vllm 的）
```

路径指向的目录里没有文件，vLLM 起不来：

```text
FAIL 10.99.2.1 t33-vllm-d：vLLM 启动时的报错：RuntimeError: Could not load logging config. File does not exist: /data/vllm-pd-diag-data/pd_diag_logging.json
FAIL 10.99.2.1 t33-vllm-d：容器里没有找到 vLLM 进程（命令行含 vllm 的）
```

记录的 logger 不在 `vllm` 之下：

```text
FAIL 10.99.2.1 t33-vllm-d 结论：日志配置加载了、过滤器已经创建（子进程带 PD_DIAG_INSTANCE_SOURCE），日志目录里却没有新文件，但 [PD_DIAG] 仍在标准输出：发这些记录的 logger 不在 vllm 之下，走不到过滤器 → 看补丁 pd_diagnostics.py 用的 logger 名，把它加进 pd_diag_logging.json 的 loggers
```

重新下发了节点文件、没有重启 vLLM：

```text
FAIL 10.99.2.1 t33-vllm-d 结论：vLLM 没有重启，新下发的节点文件没生效（vLLM 11-13 06:57:46 启动，节点文件 11-13 07:05:39 下发）→ 重启 vLLM
```

重启以后还没有请求：

```text
WARN 10.99.2.1 t33-vllm-d 结论：日志配置加载了、过滤器已经创建（子进程带 PD_DIAG_INSTANCE_SOURCE），这次启动以后还没有 [PD_DIAG] 记录（日志目录里没有新文件，标准输出里也没有新行）：多半是还没有请求 → 有请求以后再跑一次 diag.sh vllm
WARN 10.99.2.1 这些实例的日志目录（/data/pd-diag/logs/<端口>）没有新文件：D0 3 分钟没有新写入，D1 3 分钟没有新写入，…；vLLM 这次启动以后还没有记录（见结论一行），过滤器要等第一条记录才建文件
```

这种情况下 `chain` 会报 D 那台「节点上各实例最新一行落后」`FAIL`，因为确实没有数据在流动；以 `vllm` 的结论为准。

## 6. 其他子命令的 `FAIL` 和修法

`→` 后面已经写了修法的，照做即可。下表列常见的几种：

| 子命令 | `FAIL` 行（开头） | 原因 | 怎么修 |
| --- | --- | --- | --- |
| `nodes` | `<地址> SSH 连不上` | 网络、密钥、SSH 用户 | 在监控机上用 `deploy.env` 里的用户 `ssh <地址>` 试 |
| `nodes` | `mock 进程 n 个，应为 m 个` | 少了：mock 停了，停着的不会自己起来；多了：旧进程没停干净 | 少了：`cd /data/grafana/vllm-pd-grafana-panel2/glm53-1 && bin/nodes.sh start`；多了：按 `→` 后的命令先停旧进程，再 `bin/nodes.sh start` |
| `nodes` | `vllm 模式下还有 n 个 mock 进程` | 从 mock 切到 vllm 时没有停干净 | 在那台节点上执行 `pkill -9 -f '[p]d032_mock.py'` |
| `nodes` | `mock 的主循环卡住` | `MOCK_RATE` 太大，每个 D 超过约 1.7 请求/秒会积压 | 调小 `MOCK_RATE`，重启 mock |
| `nodes` | `节点文件和监控机不同或缺` | 下发以后节点上的文件被改过或删了 | `cd /data/grafana/vllm-pd-grafana-panel2/glm53-1 && bin/nodes.sh push`；vllm 模式下之后要重启 vLLM |
| `nodes` | `内存耗尽杀过进程`、`系统日志里有磁盘满` | 系统日志里的原文 | 按原文处理；磁盘满时先看 `nodes` 的日志盘一行 |
| `chain` | `拉取程序没有运行` | 监控机重启以后拉取程序不会自己起来 | `cd /data/grafana/vllm-pd-grafana-panel2/glm53-1 && bin/puller.sh start` |
| `chain` | `拉取：这些机器超过 2 分钟没有拉取成功` | SSH 不通，或者节点上日志目录读不了 | 看括号里的报错 |
| `chain` | `节点上各实例最新一行落后` | 节点上没有在写 | mock 模式看 `nodes`，vllm 模式看 `vllm` |
| `chain` | `镜像 … 最新文件 n 秒前改过`、`ClickHouse：这些实例最近 60 分钟没有行` | 上一段没有数据过来 | 往上一段找第一行 `FAIL` |
| `chain` | `解析失败` | 日志格式和包二不符 | 把报告发回；同时用 `log` 查一份节点上的日志 |
| `chain` | `过滤器出错` | 过滤器在处理记录时出错 | 把报告发回 |
| `monitor` | `容器 … 不存在`、`容器 … <状态>`（不在运行）、`被内存耗尽杀过` | 监控机上的服务停了 | `cd /data/grafana/vllm-pd-grafana-panel2/glm53-1 && bin/panel2_cluster.sh check glm53-1` 看是哪个，再按包二说明处理 |
| `monitor` | `Prometheus 访问不了` | Prometheus 容器停了或端口变了 | 同上 |
| `log` | `包二读的 9 种记录：原有的 7 种缺 …` | 这份日志里没有面板二要的记录 | 确认补丁版本；把统计文件发回 |
| `log` | `包二读的 9 种记录：新的 2 种（…）都没有` | 日志不是补丁 01、02 写的，v3 新加的 8 个段会是空的 | 确认节点上换了补丁 01、02 |
| `pair` | `诊断请求只在 P 侧有` | proxy 没有把 id 传给 D | 现场改 proxy：同一个请求发给 P、D 时带相同的 `X-Request-Id`（或请求体里相同的 `request_id`） |
| `pair` | `两侧都没有诊断请求的键，但 … P 新增、D 新增` | proxy 对两侧都没有传 id | 同上 |
| `pair` | `发请求以后只有 P 侧新增请求` | 请求没到 D，或 D 侧过滤器没生效 | 对 D 那台跑 `diag.sh vllm` |
| `pair` | `n 秒内两侧都没有新增请求` | 请求没到 vLLM，或过滤器没生效 | 先跑 `diag.sh chain`、`diag.sh vllm` |

`pair` 的样例（实验室真实输出）：proxy 正常转发时：

```text
OK   3 个请求返回 200，请求键 diag-20261113064816-1 … diag-20261113064816-3（头部和请求体都带）
OK   3 个请求两侧都有：P、D 能配上
INFO 这几个请求会出现在看板上，请求键以 diag- 开头，是诊断发的，可以忽略
```

proxy 头部和请求体里的 id 都不传时：

```text
OK   3 个请求返回 200，请求键 diag-20261113064854-1 … diag-20261113064854-3（头部和请求体都带）
FAIL 两侧都没有诊断请求的键，但发请求以后 P 新增 61 个、D 新增 60 个请求：proxy 对两侧都没有传 id（头部和请求体都没有）→ 要现场改 proxy
```

## 7. 现场要提供的信息

| 编号 | 要提供什么 | 写到哪里 | 没有时 |
| --- | --- | --- | --- |
| U1 | 每台节点上 vLLM 的容器名 | `diag.conf` 的 `VLLM_CONTAINERS`，或命令行 `--containers` | `vllm` 写 `WARN`「没有核对真实容器」；节点上发现有 vLLM 进程的容器时，会把容器名列出来 |
| U2 | SSH 用户能不能执行 `docker`（docker 组，或免密 `sudo docker`） | 不用写，工具自己判断 | 容器里的各项写 `WARN`「SSH 用户不能执行 docker，没有核对」 |
| U3 | `docker exec` 进容器的默认用户能不能读 vLLM 进程的 `/proc/<pid>/environ`（默认用户是 root 时可以） | 不用写 | 读不了的进程写 `WARN`「读不了进程环境变量」 |
| U4 | 两台节点的 `/data` 是不是同一块共享盘 | 不用写，`nodes` 用 `findmnt` 判断 | 来源相同时写 `INFO`「看起来是同一块盘」 |
| U5 | vLLM 的标准输出在 `docker logs` 里，还是被启动脚本 `tee` 进了文件 | 在文件里时写 `diag.conf` 的 `VLLM_STDOUT_FILES` | `docker logs` 为空时写 `WARN`，请给文件路径 |
| U6 | SSH 用户能不能读系统日志（root 或 `adm` 组） | 不用写 | `nodes` 写 `WARN`「读不了系统日志」，附一条可以用有权限的用户手工执行的命令 |
| U7 | proxy 的地址、端口和模型名 | `pair` 的命令行 | 不能跑 `pair` |
| U8 | vLLM 的启动脚本（去掉密码） | 发回即可 | 不影响诊断，只用来核对进程数应为多少 |
| U9 | 真实 P 侧日志的统计 | 执行 `diag.sh log <日志> --save-profile`，只发回 `reports/profiles/` 下生成的统计文件 | `log` 只能和 11-12 的 `server.log` 统计比 |

## 8. 只读范围和 `pair` 的例外

- **集群目录：** 只读。读 `deploy.env` 时，在一个干净的子 shell 里执行 `bin/lib.sh` 的 `load_env`，并设 `PYTHONDONTWRITEBYTECODE=1`，不在 `scripts/` 下生成 `__pycache__`。
- **ClickHouse：** 每条查询都带 `--readonly=1`。
- **节点：** SSH 上去只执行读的命令：`ls`、`stat`、`df`、`findmnt`、`sha256sum`、`journalctl`、`docker inspect`、`docker top`、`docker logs`。
- **vLLM 容器：** `docker exec` 进容器只做两件事：
  - 读 `/proc/<pid>` 下的命令行和上面 7 个变量；
  - 用 vLLM 进程的 uid 执行 `test -w` 判断日志目录能不能写。`test -w` 只判断，不写文件。
- **写的地方：** 只有工具目录下的 `reports/`。
- **`pair`：**
  - 经 proxy 发 3 个 `max_tokens` 为 8 的请求，请求键是 `diag-<时间>-1` 到 `-3`；
  - 这几个请求会进看板，以 `diag-` 开头的请求是诊断发的，可以忽略；
  - 除此以外 `pair` 也只读。

实验室里核对过：执行全部子命令前后，集群目录下 70 个文件的 sha256 逐个相同。

## 9. 已知限制

- **vLLM 加载 logging 配置的方式：**
  - 工具对它的判断按 vLLM 的 `vllm/logger.py`：
    - `VLLM_CONFIGURE_LOGGING=0` 又设了路径时，启动报错；
    - 路径不存在时，启动报错；
    - 路径存在时，整份 `dictConfig`。
  - 实验室用的是按这个行为写的替身，不是真实 vLLM；
  - vllm-ascend 若在这一层另有处理，「启动时的报错」一行的原文可能不同。
- **「logger 不在 vllm 之下」是推断：** 依据是过滤器已经创建（子进程带 `PD_DIAG_INSTANCE_SOURCE`），而 `[PD_DIAG]` 仍在标准输出。工具不读补丁的源码，logger 名要现场看。
- **重启以后、第一个请求以前：** 过滤器一行也不写，自检行也不写。这时：
  - `vllm` 给 `WARN`；
  - `chain` 因为没有数据而给 `FAIL`。
- **读不到进程环境变量时：** 容器的默认用户不是 root、读不了 vLLM 进程的 `environ` 时，变量各项写 `WARN`，结论只能靠文件和标准输出判断。
- **`history` 只看字节数和 `up`：** 拉到的字节数为 0 超过 10 分钟才算断，记为 `WARN`；短于 10 分钟的记 `INFO`。
- **只诊断，不修复：** `→` 后面的修法要人工执行。

## 10. 实验室验证

| 项目 | 做法 | 结果 |
| --- | --- | --- |
| 拓扑 | 与内网相同：2 台节点，一台 P0、P1，一台 D0–D7；`/data/pd-diag/conf`、`/data/pd-diag/logs` | — |
| vLLM 替身 | 每台一个容器，映射 `/data`，用 `VLLM_LOGGING_CONFIG_PATH` 加载过滤器，有主进程和子进程（EngineCore、Worker） | — |
| `vllm` 的各种情况 | 逐个造出：<br>· 变量在宿主机；<br>· 只有主进程有变量；<br>· `VLLM_CONFIGURE_LOGGING=0`；<br>· 设了它又设了路径；<br>· 路径指向的目录不存在；<br>· logger 不在 `vllm` 之下；<br>· 标准输出里有重启以前的旧行；<br>· 下发后没重启；<br>· 重启后没有请求 | 每种都得到第 5 节里对应的那一行（样例见 5.1）。<br>没有造出的有三种：<br>· 「目录里没有三个文件」（缺 `pd_diag_logging.json` 时 vLLM 起不来，结果同「目录不存在」）；<br>· 「启动不到 70 秒」；<br>· 「过滤器没有起来」 |
| `pair` | proxy 替身三种：转发头部、只转发请求体、都不转发 | 前两种 `OK`，第三种 `FAIL`（对应的那一行） |
| mock 模式 | `all`、`nodes`、`chain`、`monitor`、`history`、`vllm` 各跑一次 | 都是 `OK`，只有三类 `WARN`：<br>· 面板一的 exporter（实验室没有）；<br>· `vllm` 没有给容器名；<br>· 那时节点上还没有装 docker |
| `log` | 11-12 的 `server.log`，以及一份带新记录类型的样本 | 前者 `OK`，后者列出多出的记录类型和进程类型 |
| 只读 | 集群目录每个文件的 sha256，跑之前、跑之后各记一次 | mock 和 vllm 两轮都逐个相同（70 个文件） |
