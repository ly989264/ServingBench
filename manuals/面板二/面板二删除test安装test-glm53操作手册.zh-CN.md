# 面板二：删除 test、安装 test-glm53 操作手册

## 第一页

**这份手册做什么。** 在监控机上删掉面板二的集群 `test`（连同它的数据），再给 glm5.3 的测试集群全新安装一个面板二集群 `test-glm53`。两者用的是同两台机器。做完以后：

- `bin/panel2_cluster.sh list` 里没有 test，有 test-glm53；test 的容器、数据、看板、数据源、Prometheus 都已删除，端口 18110–18112 空出来；
- test-glm53 的看板在 Grafana 的 `GLM-5.3/test-glm53` 里，有数据；
- glm53-1 和其他集群不变。

**总顺序：**

1. 本手册第一部分（第 1–15 步）：记下 test 的几项值，删 test，装 test-glm53 的监控机部分，做到 `add` 的 `RESULT: OK`；
2. 打补丁手册：给 glm5.3 的 vLLM 打补丁，加挂载和两个环境变量，重启；
3. 本手册第 16 步：看板归位；
4. 《glm5.3 测试集群 proxy 打补丁操作手册》：给 proxy 打补丁、只重启 proxy，并打开 test-glm53 的 proxy 采集来源；
5. 本手册第 17–22 步：打开 Mooncake 采集来源，测试。

**怎么用。** 每一步写明在哪台机器上做、命令、应当看到什么、不对时怎么办。看到的和「应当看到」不一样时停下，不做下一步，把这一步标「带回：」的输出带回来（文末有汇总清单）。除第 3、9 步在 glm5.3 的两台机器上做以外，都在监控机上做，用当初装面板的那个账号（注册表 `~/.vllm-pd-clusters.json` 在这个账号的家目录里）。第 1 步把常用路径写进变量文件，以后每一步的命令都以 `. ~/vllm-pd-test-glm53/vars.sh` 开头。

<!-- 占位符版开始 -->
> **本手册有两版。** 这一版是占位符版：路径写成尖括号里的说明，不含任何内网信息。已填路径版把监控机上的个人路径填好，只放在私有的 Caching 仓库。
<!-- 占位符版结束 -->

<!-- 占位符表开始 -->
**要先准备好的现场值：**

| 值 | 怎么查 |
|---|---|
| `<用户目录>` | 包二 v3 更新脚本「现场路径」一段的 `USER_DIR` |
| `<总目录>` | 同一个脚本的 `ROOT` |
<!-- 占位符表结束 -->
| `<vLLM 容器名>` | 在 glm5.3 的每台机器上 `docker ps --format '{{.Names}}\t{{.Image}}'`，镜像是 vLLM 的那一个 |
| 每个实例 `vllm serve --port` 的端口、每侧实例的 TP、Mooncake master 的地址 | 现场；第 13 步写进清单 |
| `<模型名>` | glm5.3 的 `vllm serve --served-model-name`；不知道时填 `glm-5.3` |
| `<P 实例日志文件>` | 打补丁手册；第 17 步用 |
| SSH 账号、节点目录、三项磁盘门槛 | 不用事先查：第 2 步从 test 的 `deploy.env` 里记下 |
| `<家目录>` | 监控机上执行本手册的那个账号的家目录（`echo ~`）；只出现在「应当看到」的输出里，不用填进命令 |

**对其他服务的影响：**

| 对象 | 影响 |
|---|---|
| test | 删掉，数据不留，删了不能恢复 |
| glm53-1 和其他集群 | 不动；第 4、10、20 步各核对一次 |
| Grafana | 不重启；ClickHouse 插件保留（还有别的集群在用）；只删 test 的看板和数据源，文件夹不删 |
| docker 网络 obs-net | 不动 |
| glm5.3 的两台机器 | 第 9 步清空 `<节点目录>/logs` 里 test 留下的文件；vLLM 由打补丁手册重启，本手册不重启 |
| 监控机的端口 | test 的 18110–18112 空出来；test-glm53 占第 12 步取到的三个端口 |
| 监控机的磁盘 | 面板包解开约 390 MB；test-glm53 的数据按第 2 步记下的三项门槛预检 |

## 第一部分：删除 test，装 test-glm53 的监控机部分

### 1. 设变量

**在哪台机器：** 监控机。

**命令：**

```bash
mkdir -p ~/vllm-pd-test-glm53 && chmod 700 ~/vllm-pd-test-glm53 && cat > ~/vllm-pd-test-glm53/vars.sh <<'EOF'
UD=<用户目录>
ROOT=<总目录>
C=test-glm53
NEW=$UD/vllm-pd-panel2-v3.1-extra
PK=$NEW/pack/vllm-pd-grafana-panel2
OFF=$UD/vllm-pd-panel2-update-1112/vllm-pd-grafana-panel2-offline-aarch64.tar.gz
D=$ROOT/vllm-pd-grafana-panel2/test-glm53
PDT=$ROOT/vllm-pd-prometheus-test-glm53
T=$ROOT/vllm-pd-grafana-panel2/test
ENV2=$ROOT/vllm-pd-grafana-panel2/glm53-1/deploy.env
GLM_INV=$ROOT/vllm-pd-grafana-panel1/glm53-1/prometheus/inventory.json
T45=$UD/vllm-pd-boards-clusters
SV=~/vllm-pd-test-glm53
B=$(cat ~/vllm-pd-test-glm53/port_base 2>/dev/null || true)
EOF
. ~/vllm-pd-test-glm53/vars.sh && ls -d "$OFF" "$T/deploy.env" "$ENV2" "$GLM_INV" "$T45/arrange_boards.py" "$T45/boards_layout.txt" "$T45/make_inventories.py" "$T45/clusters.txt" ~/.vllm-pd-clusters.json && echo 变量已设
```

**应当看到：** 列出 9 个路径，最后一行 `变量已设`。

**不对时：** 哪个路径报 `No such file or directory`，停下。带回：这条命令的全部输出。

### 2. 记下 test 的几项值（只读）

**在哪台机器：** 监控机。

test 删掉以后它的 `deploy.env` 就没有了。第 15 步要沿用其中的 SSH 账号、节点目录和三项磁盘门槛，这里先记下来：

```bash
. ~/vllm-pd-test-glm53/vars.sh && grep -E '^(NODE_MODE|PORT_BASE|SSH_USER|NODE_CONF_DIR|NODE_LOG_DIR|NODE_CONF_DIR_IN_CONTAINER|NODE_LOG_DIR_IN_CONTAINER|NODE_CONTAINER_UID|PULL_MIN_DISK_GB|CLICKHOUSE_MIN_DISK_GB|NODE_MIN_DISK_GB)=' "$T/deploy.env" | tee "$SV/test-values.txt" && python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); print("机器", " ".join(sorted({a.rsplit(":", 1)[0] for a in d["prefill"]["api_servers"] + d["decode"]["api_servers"]})))' "$T/inventory.json" | tee -a "$SV/test-values.txt"
```

**应当看到：** 11 行设置，然后一行 `机器 <P 机器地址> <D 机器地址>`（test 的两台机器，也就是 glm5.3 测试集群的两台机器）。同样的内容写进了 `~/vllm-pd-test-glm53/test-values.txt`。实验室的输出：

```
PORT_BASE=18110
SSH_USER=appuser
PULL_MIN_DISK_GB=16
CLICKHOUSE_MIN_DISK_GB=85
NODE_MODE=vllm
NODE_CONF_DIR=/data/pd-diag/conf
NODE_LOG_DIR=/data/pd-diag/logs
NODE_CONF_DIR_IN_CONTAINER=/data/pd-diag/conf
NODE_LOG_DIR_IN_CONTAINER=/data/pd-diag/logs
NODE_CONTAINER_UID=
NODE_MIN_DISK_GB=50
机器 <P 机器地址> <D 机器地址>
```

- `NODE_MODE=vllm`：test 是 vllm 模式，节点上没有 mock，写日志的是 vLLM 里的过滤器。
- `NODE_CONF_DIR`、`NODE_LOG_DIR` 的上一级就是 test 的节点目录（内网是 `/data/pd-diag`），第 3 步核对它。
- 三项门槛 `PULL_MIN_DISK_GB`、`CLICKHOUSE_MIN_DISK_GB`、`NODE_MIN_DISK_GB` 第 15 步原样传给 test-glm53。

**不对时：** 少了哪一行，停下。带回：这条命令的全部输出。

再比对 6 个共用值。第 15 步的 `--env-from` 从 glm53-1 的 `deploy.env` 取这 6 项，这里核对它们和 test 的相同（只打印「相同」「不同」，不打印值）：

```bash
. ~/vllm-pd-test-glm53/vars.sh && for k in GRAFANA_URL GRAFANA_USER GRAFANA_PASSWORD GRAFANA_CONTAINER DOCKER_NETWORK MONITOR_HOST; do a=$(grep "^$k=" "$T/deploy.env" | tail -n 1 | cut -d= -f2-); b=$(grep "^$k=" "$ENV2" | tail -n 1 | cut -d= -f2-); if [ -z "$b" ]; then r="glm53-1 里没有"; elif [ "$a" = "$b" ]; then r=相同; else r=不同; fi; echo "$k $r"; done | tee -a "$SV/test-values.txt"
```

**应当看到：** 6 行，每行是键名和 `相同`；同样的 6 行追加进 `test-values.txt`：

```
GRAFANA_URL 相同
GRAFANA_USER 相同
GRAFANA_PASSWORD 相同
GRAFANA_CONTAINER 相同
DOCKER_NETWORK 相同
MONITOR_HOST 相同
```

**不对时：** 有一行是 `不同` 或 `glm53-1 里没有`：停下，不做第 3 步以后的任何一步（第 6 步删掉 test 以后就没法再比了）。带回：这 6 行（里面没有密码）。

### 3. 核对节点目录（glm5.3 的两台机器，只读）

**在哪台机器：** glm5.3 的 P 机器和 D 机器，各做一次。

```bash
docker inspect <vLLM 容器名> --format '{{range .Mounts}}{{.Source}} -> {{.Destination}}{{println}}{{end}}' && docker exec <vLLM 容器名> ls -d /data/pd-diag/conf /data/pd-diag/logs
```

**应当看到：** 先是这个容器的挂载，每行 `宿主机路径 -> 容器里的路径`，其中有一行盖住 `/data/pd-diag`（`/data/pd-diag/… -> /data/pd-diag/…`，或者它的上级目录，例如 `/data -> /data`）；最后两行是 `/data/pd-diag/conf`、`/data/pd-diag/logs`。

**结论：**

- **两台机器都能看到：** test-glm53 沿用 `/data/pd-diag` 作节点目录，容器里的路径也是 `/data/pd-diag`。第 9 步清空里面 test 留下的文件，打补丁手册不用重建容器。
- **有一台看不到**（`ls` 报 `No such file or directory`，或者挂载里没有盖住 `/data/pd-diag` 的那一行）：停下，不往下做。带回：两台机器上这条命令的输出。由我们定新的节点目录，挂载放进打补丁手册里做。

**带回：** 两台机器上这条命令的输出。

### 4. 记下删除前的状态（只读）

**在哪台机器：** 监控机。

```bash
. ~/vllm-pd-test-glm53/vars.sh && cd "$T" && bin/panel2_cluster.sh list && python3 "$T45/arrange_boards.py" state --env-from "$ENV2" --except test > "$SV/state-before" && grep -c '' "$SV/state-before"
```

**应当看到：** 每个已登记集群一行（含 test），最后一个行数。`state-before` 记下了除 test 自己的文件和看板以外，各集群的模型文件夹、各文件的 sha256、看板的版本和位置、监控机上各容器的启动时间。

**不对时：** 报错时停下。带回：这条命令的全部输出。

### 5. 停 test 的 mock 和拉取程序

**在哪台机器：** 监控机。

```bash
. ~/vllm-pd-test-glm53/vars.sh && cd "$T" && bin/nodes.sh stop && bin/puller.sh stop
```

**应当看到：** 实验室的输出：

```
OK   <P 机器地址> 没有 mock 进程在运行
OK   <D 机器地址> 没有 mock 进程在运行
RESULT: OK（2 台全过）
拉取程序已停止
```

**不对时：** 出现 `FAIL`，停下。带回：这条命令的全部输出。

### 6. 删 test

**在哪台机器：** 监控机。

```bash
. ~/vllm-pd-test-glm53/vars.sh && cd "$T" && bin/panel2_cluster.sh remove test 2>&1 | tee "$SV/remove-test.log"
```

`remove` 依次做 `plan`（列出要删什么）、`remove`（停拉取程序，关 SSH 连接复用，停节点上的 mock 并删它的程序目录，删看板、数据源、Prometheus 里面板二的那一段，删采集器和 ClickHouse 容器）、`purge-data`（删 ClickHouse 的数据卷、镜像目录、节点上 test 的旧日志）、`check`（核对删干净了），然后注销注册表里的 test，删掉集群目录 `<总目录>/vllm-pd-grafana-panel2/test`。不带 `--backup`：数据不留。

**应当看到：** 四段：`plan` 只列出要删的东西；`remove`、`purge-data`、`check` 三段末尾各一行 `RESULT: OK`；最后一行 `RESULT: OK（集群 test 的面板二已卸载）`。实验室的输出（摘出各段的结论）：

```
==== uninstall_old.sh plan <总目录>/vllm-pd-grafana-panel2/test
      Grafana 的 ClickHouse 插件 grafana-clickhouse-datasource：新版也用它；删除要重启 Grafana（remove 加 --remove-plugin）
==== uninstall_old.sh remove <总目录>/vllm-pd-grafana-panel2/test
RESULT: OK
==== uninstall_old.sh purge-data <总目录>/vllm-pd-grafana-panel2/test
OK    <P 机器地址>：/data/pd-diag/logs 里过滤器写的旧文件（删了 0 个；留下正在写的 10 个：vLLM 还开着它们，摘掉过滤器、重启 vLLM 以后再手删）已删除
OK    <D 机器地址>：/data/pd-diag/logs 里过滤器写的旧文件（删了 0 个；留下正在写的 40 个：vLLM 还开着它们，摘掉过滤器、重启 vLLM 以后再手删）已删除
RESULT: OK
==== uninstall_old.sh check <总目录>/vllm-pd-grafana-panel2/test
INFO  ClickHouse 插件：保留（新版要用）
RESULT: OK
已删除目录 <总目录>/vllm-pd-grafana-panel2/test
已删除空目录 <总目录>/vllm-timing/test
RESULT: OK（集群 test 的面板二已卸载）
```

- `ClickHouse 插件：保留`：监控机上还有别的面板二集群，插件不删，Grafana 不重启。
- 节点上 vLLM 正在写的文件 `remove` 不删（`留下正在写的 <n> 个`），第 9 步清掉。
- `check` 一段里两行 `INFO … $HOME/vllm-timing 还在`：那是节点上 SSH 账号家目录里面板自带的 Python，留着，test-glm53 接着用。

**不对时：** 中途停在某一段的 `FAIL`：照那一行处理以后，重跑同一条命令。带回：`~/vllm-pd-test-glm53/remove-test.log`。

### 7. 删 test 自己的 Prometheus

**在哪台机器：** 监控机。

test 没有面板一，装的时候另起了一个 Prometheus。`remove` 只从它的 `prometheus.yml` 里去掉面板二的那一段，容器、数据卷和配置目录都留着。先看一眼是不是这个容器：

```bash
. ~/vllm-pd-test-glm53/vars.sh && docker inspect vllm-pd-prometheus-test --format '{{.Name}} {{range .Mounts}}{{.Source}} {{end}}'
```

**应当看到：** 一行 `/vllm-pd-prometheus-test`，后面是它的数据卷路径（`…/vllm-pd-prometheus-data-test/_data`）和 `<总目录>/vllm-pd-prometheus-test/prometheus.yml`。

**不对时：** 报 `No such object`：没有这个容器，跳过这一步的删除，在第 10 步照常核对。挂载不是这两项：停下，带回这一行。

再删：

```bash
. ~/vllm-pd-test-glm53/vars.sh && docker rm -f vllm-pd-prometheus-test && docker volume rm vllm-pd-prometheus-data-test && rm -rf "$ROOT/vllm-pd-prometheus-test" && echo 已删除
```

**应当看到：** `vllm-pd-prometheus-test`、`vllm-pd-prometheus-data-test`、`已删除` 三行。

**不对时：** 哪一项报错，停下。带回：这条命令的全部输出。

### 8. 核对 Grafana 和注册表（只读）

**在哪台机器：** 监控机。

```bash
. ~/vllm-pd-test-glm53/vars.sh && cd "$ROOT/vllm-pd-grafana-panel2/glm53-1" && (set -a; . ./deploy.env; curl -s -u "$GRAFANA_USER:$GRAFANA_PASSWORD" "$GRAFANA_URL/api/search" | python3 -c 'import json,sys; [print(d["type"], "|", d.get("folderTitle", "-"), "|", d["uid"], "|", d["title"]) for d in json.load(sys.stdin)]'; curl -s -u "$GRAFANA_USER:$GRAFANA_PASSWORD" "$GRAFANA_URL/api/datasources" | python3 -c 'import json,sys; [print("datasource |", d["uid"]) for d in json.load(sys.stdin)]') | grep -ic test; bin/panel2_cluster.sh list
```

**应当看到：** 第一行是 `0`（Grafana 里没有名字或 UID 带 test 的看板、文件夹、数据源），后面是 `list` 的输出，没有 test 那一行。

- test 的看板在 glm53-1 的模型文件夹里（test 的 `MODEL_GROUP` 是 glm53-1），没有自己的文件夹，所以不用删文件夹。文件夹里 glm53-1 的看板都还在。

**不对时：** 第一行不是 `0`：先把命令里的 `grep -ic test` 换成 `grep -i test` 再执行一次，看命中的是不是 test 的东西（这是子串匹配，标题里带 latest 这类的项也会命中）；命中的都不是 test 的东西，照常往下做。命中了 test 的东西，或者 `list` 里还有 test：停下。带回：这条命令的全部输出。

### 9. 清空节点目录里 test 的旧文件（glm5.3 的两台机器）

**在哪台机器：** glm5.3 的 P 机器和 D 机器，各做一次，以 root 执行（这些文件属于容器里跑 vLLM 的用户，SSH 账号删不掉）。

```bash
find /data/pd-diag/logs -mindepth 1 -delete; find /data/pd-diag/logs -type f | wc -l; ls /data/pd-diag/conf
```

**应当看到：** 一行 `0`，然后 `conf` 里的 `__pycache__` 和三个文件 `pd_diag_logfilter.py`、`pd_diag_logging.json`、`segments.json`（test 下发的，第 15 步换成 test-glm53 的）。

- 现场第 2 次检测时两台机器上的 vLLM 是 `logcfg=unset`（没有设 `VLLM_LOGGING_CONFIG_PATH`），清空以后不会再有新文件。如果打补丁手册第 2 步（打补丁前检测）的 run 行是 `logcfg=set`，现在的 vLLM 里挂着 test 留下的旧过滤器，重启以前它还会往 `logs` 里写，见附录 B 第 3 条。

**不对时：** 报 `Permission denied`：不是 root。带回：这条命令的输出。

### 10. 删除以后的核对（只读）

**在哪台机器：** 监控机。三条命令。

```bash
. ~/vllm-pd-test-glm53/vars.sh && cd "$ROOT/vllm-pd-grafana-panel2/glm53-1" && bin/panel2_cluster.sh list && python3 "$T45/arrange_boards.py" state --env-from "$ENV2" --except test > "$SV/state-after-rm" && diff "$SV/state-before" "$SV/state-after-rm"; echo "diff 结束"
```

**应当看到：** `list` 里没有 test；`diff` 只有 test 自己的 3 个容器各一行，然后 `diff 结束`：

```
<行号>d<行号>
< 容器 vllm-pd-clickhouse-test 启动于 <时间>
<行号>d<行号>
< 容器 vllm-pd-prometheus-test 启动于 <时间>
<行号>d<行号>
< 容器 vllm-pd-vector-test 启动于 <时间>
diff 结束
```

```bash
. ~/vllm-pd-test-glm53/vars.sh && docker ps -a --format '{{.Names}} {{.Ports}}' | grep -E -- '-test |:1811[0-9]->'; docker network ls --format '{{.Name}}' | grep -x obs-net; ls -d "$T" 2>&1 | tail -n 1
```

**应当看到：** 没有容器行（没有名字以 `-test` 结尾、也没有占 18110–18119 的容器）；一行 `obs-net`；最后一行 `ls: cannot access '<总目录>/vllm-pd-grafana-panel2/test': No such file or directory`。

```bash
. ~/vllm-pd-test-glm53/vars.sh && cd "$ROOT/vllm-pd-grafana-panel2/glm53-1" && bin/panel2_cluster.sh check glm53-1 | tail -n 3
```

**应当看到：** 最后一行 `RESULT: OK`。

**不对时：** `diff` 里有 test 的 3 个容器以外的行（别的集群的文件、看板或容器变了），或者 glm53-1 的 `check` 不是 `RESULT: OK`：停下，不装 test-glm53。带回：三条命令的输出。

**带回：** 三条命令的输出。

### 11. 放面板包，核对、解开

**在哪台机器：** 监控机。

把交付的面板包 `vllm-pd-grafana-panel2.tar.gz` 和 `vllm-pd-grafana-panel2.tar.gz.sha256`（ServingBench `包二-v3.1-extra/2-面板二v3.1-extra/`）拷进 `<用户目录>/vllm-pd-panel2-v3.1-extra/`：

```bash
. ~/vllm-pd-test-glm53/vars.sh && mkdir -p "$NEW" && ls "$NEW"
```

拷完以后：

```bash
. ~/vllm-pd-test-glm53/vars.sh && cd "$NEW" && sha256sum -c vllm-pd-grafana-panel2.tar.gz.sha256 && rm -rf pack && mkdir pack && tar xzf vllm-pd-grafana-panel2.tar.gz -C pack && tar xzf "$OFF" -C pack/vllm-pd-grafana-panel2 && cd pack/vllm-pd-grafana-panel2 && sha256sum -c SHA256SUMS --quiet && (cd offline && sha256sum -c SHA256SUMS --quiet) && cat VERSION && ls profiles
```

**应当看到：**

```
vllm-pd-grafana-panel2.tar.gz: OK
tar: Ignoring unknown extended header keyword 'LIBARCHIVE.xattr.com.apple.provenance'
…（同样的一行共 7 行）
v3.1-extra
dsv41
glm53
```

`tar: Ignoring unknown extended header keyword …` 几行来自离线包本身，不影响解包和校验。

**不对时：** 出现 `FAILED`：包损坏，重新拷。带回：这条命令的全部输出。

### 12. 取端口基数

**在哪台机器：** 监控机。

```bash
. ~/vllm-pd-test-glm53/vars.sh && cd "$PK" && python3 scripts/names.py alloc --cluster "$C" | tee "$SV/port_base"
```

**应当看到：** 一个数：注册表里还没被占、端口也空着的下一个端口段的起点。实验室是 `18110`，就是 test 空出来的端口段；内网以命令输出为准。新集群占它和后面两个端口（ClickHouse、拉取程序、它自己的 Prometheus）。

**不对时：** 报错时停下。带回：这条命令的输出。

### 13. 写清单 `inventory.json`

**在哪台机器：** 监控机。三条命令。

第一条：备份清单文件，在末尾加 test-glm53 的 3 行模板：

```bash
. ~/vllm-pd-test-glm53/vars.sh && cp -p "$T45/clusters.txt" "$SV/clusters.txt.before" && printf '\n# test-glm53：glm5.3 的测试集群（面板二）\n集群 test-glm53 master=<Mooncake master 的地址>:<端口>\nP <P 机器地址> 端口=<端口> TP=<P 实例的 TP>\nD <D 机器地址> 端口=<端口> TP=<D 实例的 TP>\n' >> "$T45/clusters.txt" && tail -n 4 "$T45/clusters.txt"
```

再用编辑器把这几行换成现场的值：机器地址就是第 2 步记下的两台；`端口=` 写这台机器上每个实例 `vllm serve --port` 的值（`端口=9000`、`端口=9000-9015` 或 `端口=9000,9002`）；`TP=` 写每个实例的 TP。换完以后这几行里不能再有 `<` 或 `>`；别的几套的行不要动。

第二条：生成，补上模型名：

```bash
. ~/vllm-pd-test-glm53/vars.sh && python3 "$T45/make_inventories.py" build "$T45/clusters.txt" --defaults "$GLM_INV" --out "$SV/inv" --cluster "$C"
```

```bash
. ~/vllm-pd-test-glm53/vars.sh && python3 - "$SV/inv/$C/inventory.json" "$SV/inventory.json" '<模型名>' <<'EOF'
import json, sys
d = json.load(open(sys.argv[1]))
if "prefill" not in d or "decode" not in d:
    raise SystemExit("这份清单是 api_servers 写法（同一侧的 TP 不同），面板二读不了：按 4.3 手写")
d["model"] = sys.argv[3]
json.dump(d, open(sys.argv[2], "w"), indent=1)
print("写好 " + sys.argv[2])
EOF
```

第三条：核对：

```bash
. ~/vllm-pd-test-glm53/vars.sh && cd "$PK" && python3 scripts/inventory_tool.py "$SV/inventory.json" summary
```

**应当看到**（实验室：P 1 台机器 2 个实例、D 1 台机器 8 个实例，TP 都是 2）：

```
集群 test-glm53：P 1 台 2 个实例（P0–P1），D 1 台 8 个实例（D0–D7），master <Mooncake master 的地址>:<端口>；抓取目标 15 个；两个列表的写法 → <家目录>/vllm-pd-test-glm53/inv/test-glm53/inventory.json
RESULT: OK（集群 test-glm53，2 台机器；清单文件里其他集群的行没有读）
写好 <家目录>/vllm-pd-test-glm53/inventory.json
```

**不对时：** 第二条报「api_servers 写法」（同一侧各机器的 TP 不同）：停下，带回清单文件里 test-glm53 那几行。其余报错同样停下。带回：三条命令的输出。

### 14. 起 test-glm53 自己的 Prometheus

**在哪台机器：** 监控机。这一步要在第 12 步之后做：命令里的 `B` 是第 12 步写出来的端口基数。新集群没有面板一，面板二的告警写进它自己的 Prometheus。网络和镜像取自 glm53-1 面板一的 Prometheus 容器。

```bash
. ~/vllm-pd-test-glm53/vars.sh && NET=$(docker inspect -f '{{range $k, $v := .NetworkSettings.Networks}}{{$k}} {{end}}' vllm-pd-prometheus-glm53-1 | awk '{print $1}') && IMG=$(docker inspect -f '{{.Config.Image}}' vllm-pd-prometheus-glm53-1) && echo "网络 $NET，镜像 $IMG，端口 $((B+2))" && mkdir -p "$PDT" && printf 'global:\n  scrape_interval: 15s\n  evaluation_interval: 15s\nscrape_configs:\n' > "$PDT/prometheus.yml" && chmod 644 "$PDT/prometheus.yml" && docker run -d --name vllm-pd-prometheus-$C --network "$NET" --restart unless-stopped -p $((B+2)):9090 -v "$PDT/prometheus.yml:/etc/prometheus/prometheus.yml:ro" -v vllm-pd-prometheus-data-$C:/prometheus "$IMG" --config.file=/etc/prometheus/prometheus.yml --storage.tsdb.path=/prometheus --web.enable-lifecycle >/dev/null && for i in $(seq 1 30); do curl -s localhost:$((B+2))/-/ready && break; sleep 2; done
```

**应当看到：**

```
网络 <网络名>，镜像 prom/prometheus:v3.11.2，端口 <B+2>
Prometheus Server is Ready.
```

**不对时：** 报端口被占用：`ss -ltnp | grep $((B+2))` 看是谁占着。带回：两条命令的输出。

### 15. 装 test-glm53 的监控机部分

**在哪台机器：** 监控机。

把 `<…>` 换成第 2 步 `test-values.txt` 里的值（节点目录按第 3 步的结论，沿用时就是那几项原样；第 2 步里值为空的项，写成 `--set 键=`，例如 `--set NODE_CONTAINER_UID=`）：

```bash
. ~/vllm-pd-test-glm53/vars.sh && cd "$PK" && bin/panel2_cluster.sh add "$C" --model-group "$C" --mode vllm --port-base "$B" --inventory "$SV/inventory.json" --env-from "$ENV2" --set PROMETHEUS_YML="$PDT/prometheus.yml" --set SEGMENTS_PROFILE=glm53 --set SSH_USER=<第 2 步的 SSH_USER> --set NODE_CONF_DIR=<第 2 步的 NODE_CONF_DIR> --set NODE_LOG_DIR=<第 2 步的 NODE_LOG_DIR> --set NODE_CONF_DIR_IN_CONTAINER=<第 2 步的 NODE_CONF_DIR_IN_CONTAINER> --set NODE_LOG_DIR_IN_CONTAINER=<第 2 步的 NODE_LOG_DIR_IN_CONTAINER> --set NODE_CONTAINER_UID=<第 2 步的 NODE_CONTAINER_UID> --set PULL_MIN_DISK_GB=<第 2 步的 PULL_MIN_DISK_GB> --set CLICKHOUSE_MIN_DISK_GB=<第 2 步的 CLICKHOUSE_MIN_DISK_GB> --set NODE_MIN_DISK_GB=<第 2 步的 NODE_MIN_DISK_GB> 2>&1 | tee "$SV/add.log"
```

- `--env-from "$ENV2"`：从 glm53-1 的 `deploy.env` 取共用的值（Grafana 的地址和账号、docker 网络、监控机地址），别的都不取。test 已经删了，所以不再从 test 取；这几项在两个集群里相同。
- 三项磁盘门槛 `--env-from` 不取，用 `--set` 传 test 的值。
- 第 2 步里为空的项写成空值（`--set 键=`），和 test 的做法一致，实测可用：实验室里 test 按内网 test 的值装（`NODE_CONTAINER_UID` 为空），第 15 步照这条命令写 `--set NODE_CONTAINER_UID=`；另把 `NODE_CONTAINER_UID`、`NODE_CONF_DIR_IN_CONTAINER`、`NODE_LOG_DIR_IN_CONTAINER` 三项都写成空值补测过一次。两次 `add` 各步都过、节点自检过，`deploy.env` 里写成空值的项为空，节点文件的属主和权限和 test 相同。`NODE_CONTAINER_UID` 为空时，节点自检不查容器里的用户能不能读三个节点文件、写日志目录，其余照常；`NODE_CONF_DIR_IN_CONTAINER`、`NODE_LOG_DIR_IN_CONTAINER` 为空时，容器里的路径按宿主机路径算。

**应当看到：** 实验室的输出（摘出开头、各步的结论和结尾）：

```
共用值来自 <总目录>/vllm-pd-grafana-panel2/glm53-1/deploy.env
已写 <总目录>/vllm-pd-grafana-panel2/test-glm53/deploy.env：集群 test-glm53，模型文件夹 test-glm53，端口基数 18110（ClickHouse 18110、拉取程序 18111、Prometheus 18112）
段定义：profiles/glm53/segments.json（deploy.env 的 SEGMENTS_PROFILE）
==== 集群 test-glm53：查注册表、端口和清单
已登记集群 test-glm53 的面板二（<家目录>/.vllm-pd-clusters.json）
==== 步骤 3：导入镜像（已有就跳过）
==== 步骤 6：预检监控机
RESULT: OK（24 项全过）
==== 步骤 7：预检所有节点
RESULT: OK（2 台全过）
==== 步骤 9：启动 ClickHouse
RESULT: OK
==== 步骤 10：启动采集器
RESULT: OK
==== 步骤 11：给 Grafana 装 ClickHouse 插件（已装就跳过）
RESULT: OK
==== 步骤 12：导入数据源和看板
RESULT: OK
==== 步骤 13：让本集群的 Prometheus 抓采集链路的指标
RESULT: OK
==== 步骤 14：下发三个节点文件
RESULT: OK（2 台全过）
==== 步骤 15：节点自检
RESULT: OK（2 台全过）
==== 步骤 17：启动拉取程序
RESULT: OK
==== 每台集群机器要加的目录、挂载和环境变量（bin/nodes.sh paths）
<P 机器地址>
  /data/pd-diag/conf/  pd_diag_logging.json  pd_diag_logfilter.py  segments.json
  /data/pd-diag/logs/7100/  api.<启动时间>.<pid>.<序号>.log  engine.<启动时间>.<pid>.<序号>.log  worker.<启动时间>.<pid>.<序号>.log    （P0，vllm serve --port 7100）
  …（每个实例一行）
  这台机器一组：启动脚本里、vllm serve 之前加这两行，所有 rank 共用
    export VLLM_LOGGING_CONFIG_PATH=/data/pd-diag/conf/pd_diag_logging.json
    export PYTHONPATH=/data/pd-diag/conf${PYTHONPATH:+:$PYTHONPATH}
  容器加挂载：-v /data/pd-diag/conf:/data/pd-diag/conf:ro -v /data/pd-diag/logs:/data/pd-diag/logs（容器里已经能在同一路径看到这两个宿主机目录时，不用加）
…（每台机器一段）
共 2 台机器、10 个实例（P 2 个，D 8 个）；写进了 generated/node_paths.txt
RESULT: OK（集群 test-glm53 的监控机部分装好了。还没接上的 vLLM 实例按上面的输出加挂载和环境变量、重启 vLLM；已经接好的不用改。然后执行 bin/panel2_cluster.sh check test-glm53）
```

中间每台机器一段「要加的目录、挂载和环境变量」，打补丁手册照它做。

**不对时：** 停在某一步的 `FAIL`：照那一行处理以后，重跑同一条命令，做过的步骤内容不变就跳过。查注册表一步报端口段被占时，回到第 12 步重取。带回：`~/vllm-pd-test-glm53/add.log`。

**第一部分到这里结束。接着做打补丁手册，做完再做第二部分。**

## 第二部分：打补丁手册做完以后

前提：打补丁手册已经在两台机器上给 vLLM 打了补丁、设了两个环境变量、重启过。

### 16. 看板放进 `GLM-5.3/test-glm53`

**在哪台机器：** 监控机。

`add` 只认顶层文件夹，看板先落在顶层的 `test-glm53` 里。在看板分层的对照表里加一行，先看要改什么：

```bash
. ~/vllm-pd-test-glm53/vars.sh && cp -p "$T45/boards_layout.txt" "$SV/boards_layout.before" && printf 'GLM-5.3        test-glm53     test-glm53\n' >> "$T45/boards_layout.txt" && tail -n 3 "$T45/boards_layout.txt" && python3 "$T45/arrange_boards.py" plan --env-from "$ENV2"
```

**应当看到：** 只列出 test-glm53 的 3 项（建文件夹 `GLM-5.3/test-glm53`、挪看板、删空文件夹 `test-glm53`），最后一行 `RESULT: 要改 3 项（plan 只看不改；执行 apply 才改）`。

再改：

```bash
. ~/vllm-pd-test-glm53/vars.sh && python3 "$T45/arrange_boards.py" apply --env-from "$ENV2" 2>&1 | tee "$SV/arrange-apply.log"
```

**应当看到：**

```
== 对照表 boards_layout.txt：10 个集群；已登记的 2 个：glm53-1、test-glm53；还没登记的：glm53-0、test、glm53-2、dsv41-flash-1、dsv41-flash-2、dsv41-flash-3、dsv41-flash-4、dsv41-flash-5
== 备份到 <家目录>/vllm-pd-arrange-backup/<时间>（注册表、要改的 deploy.env；manifest.json 记下每一项改动）
已建 文件夹 GLM-5.3/test-glm53
已挪 看板 vllm-pd-request-test-glm53「面板二：请求时延拆解（P / D）（test-glm53）」：test-glm53 → GLM-5.3/test-glm53
已删 空文件夹 test-glm53（uid <…>）
== 核对
OK   再看一遍：没有要改的
RESULT: OK（改了 3 项；回退：python3 arrange_boards.py undo <家目录>/vllm-pd-arrange-backup/<时间> --env-from <同一个 deploy.env>）
```

**不对时：** `plan` 列出了 test-glm53 以外的项：不执行 `apply`，停下。带回：两条命令的输出。

**第 16 步做完以后，先做《glm5.3 测试集群 proxy 打补丁操作手册》，再做第 17 步。**

### 17. 打开 Mooncake 采集来源

**在哪台机器：** 监控机。

proxy 采集来源已经在《glm5.3 测试集群 proxy 打补丁操作手册》第 4 步打开（`PROXY_COLLECT`、`PROXY_MACHINE`、`PROXY_LOG_DIR`、`PULL_RSYNC_FLAGS` 4 项，proxy 的记录写在 proxy 所在机器的 `/data/pd-diag/logs/proxy`），这里不再加。在 test-glm53 的 `deploy.env` 末尾加 Mooncake 的 3 行，再重启拉取程序。值从打补丁手册取：

- `<P 实例日志文件>`：每个 P 实例 vLLM 日志文件（标准错误重定向到的那个文件）的宿主机路径，写成 `<P 机器地址>=/宿主机/路径/文件名`；一台机器几个实例时路径之间用分号，几台机器之间用逗号。

```bash
. ~/vllm-pd-test-glm53/vars.sh && printf '\nMOONCAKE_LOG_COLLECT=1\nMOONCAKE_LOG_FILES=%s\nMOONCAKE_TID_FALLBACK=0\n' '<P 实例日志文件>' >> "$D/deploy.env"
```

```bash
. ~/vllm-pd-test-glm53/vars.sh && cd "$D" && bin/puller.sh restart && bin/puller.sh status
```

**应当看到：** 每台机器一行 `OK … 上次成功 <n> 秒前`，另有 proxy 机器一行（proxy 打补丁手册第 4 步加的）和 `mooncake` 一行，最后 `RESULT: OK`：

```
RESULT:   OK（集群   test-glm53：注册表里没有重复，端口空闲或属于本集群）
拉取程序已启动，每   10   秒拉一轮，镜像到   <总目录>/vllm-timing/test-glm53/pull
RESULT:   OK
OK   <P 机器地址>   <P 机器地址>   上次成功   1.9   秒前
OK   <D 机器地址>   <D 机器地址>   上次成功   1.9   秒前
OK   <proxy 机器地址>   <proxy 机器地址>   上次成功   1.9   秒前
OK   mooncake   _native   上次成功   1.9   秒前
镜像占用   7.5M
RESULT:   OK
```

**不对时：** 某一行不是 `OK`：带回 `bin/puller.sh status` 的输出。没有 proxy 机器那一行：proxy 打补丁手册还没做，先做完那本手册再回到这一步。

### 18. 升级安全检查

**在哪台机器：** 监控机。重启 vLLM 以后至少 3 分钟再做第 18–20 步，至少 10 分钟再做第 21 步。

```bash
. ~/vllm-pd-test-glm53/vars.sh && cd "$D" && bin/panel2_cluster.sh check "$C"
```

**应当看到：** 每项 `OK`，其中 `库：表和视图都在，视图读的是 pd-diag-v3.1-extra-glm53-2026-11-19c`、`过滤器版本：<n> 个实例 <n> 个进程都是 pd-diag-v3.1-extra-glm53-2026-11-19c@<8 位>`，最后一行 `RESULT: OK`。

**不对时：** 有 `FAIL`：带回全部输出。

**带回：** 全部输出。

### 19. 各段有值的请求数

**在哪台机器：** 监控机。

```bash
. ~/vllm-pd-test-glm53/vars.sh && cd "$D" && bin/coverage.sh 200 30
```

**应当看到：** 每段一行，最后两行汇总。现场都打上补丁、两个采集来源都打开时：

```
47 段里 45 段有值；没有来源、按设计为空的 2 段：P api_gateway、P start_load_kv/wait_for_transfer
RESULT: OK
```

实验室的替身没有本套补丁，只写 host 时间，最后两行是：

```
47 段里 35 段有值；没有来源、按设计为空的 2 段：P api_gateway、P start_load_kv/wait_for_transfer
RESULT: FAIL（有来源但这些请求里都为空的 10 段：P wait_for_put、P forward、P sampling、P 后台 wait_for_save/put/submit、P 后台 wait_for_save/put/wait_for_transfer、P 后台 wait_for_save/put/finalize、D forward、D sampling、D 第二步起 forward、D 第二步起 sampling）
```

**不对时：** 最后一行是 `RESULT: FAIL`，列出了有来源却为空的段：带回全部输出。

**带回：** 全部输出。

### 20. 已有集群没有变

**在哪台机器：** 监控机。和第 10 步记下的状态比：

```bash
. ~/vllm-pd-test-glm53/vars.sh && python3 "$T45/arrange_boards.py" state --env-from "$ENV2" --except "$C" > "$SV/state-after" && diff "$SV/state-after-rm" "$SV/state-after"; echo "diff 结束"
```

**应当看到：** 只多出 test-glm53 自己的 3 个容器各一行：

```
<行号>a<行号>
> 容器 vllm-pd-clickhouse-test-glm53 启动于 <时间>
<行号>a<行号>
> 容器 vllm-pd-prometheus-test-glm53 启动于 <时间>
<行号>a<行号>
> 容器 vllm-pd-vector-test-glm53 启动于 <时间>
diff 结束
```

**不对时：** 有别的差别：带回全部输出。

### 21. 数据准确性检查

**在哪台机器：** 监控机。

```bash
. ~/vllm-pd-test-glm53/vars.sh && cd "$D" && bin/panel2_cluster.sh quality "$C"
```

**应当看到：** `RESULT: OK`。只报告，不拦任何命令。

**不对时：** 有 `FAIL`：带回全部输出（实验室的替身有两条 FAIL，见附录 B）。

**带回：** 全部输出。

### 22. 过滤器自检行的两个计数

**在哪台机器：** 监控机。

```bash
. ~/vllm-pd-test-glm53/vars.sh && cd "$D" && bash -c '. bin/lib.sh && load_env >/dev/null && curl -s -u "vllm_reader:$CH_READER_PASSWORD" "http://localhost:$CLICKHOUSE_HTTP_PORT/" --data-binary @-' <<'SQL'
SELECT instance, sum(f) AS forward_empty, sum(s) AS sampling_empty, sum(e) AS no_execution_id FROM (SELECT instance, argMax(acc_fallback['forward'], ts) AS f, argMax(acc_fallback['sampling'], ts) AS s, argMax(exec_missing, ts) AS e FROM vllm_pd.filter_selfcheck WHERE ts > now() - INTERVAL 10 MINUTE GROUP BY node, instance, pid) GROUP BY instance ORDER BY instance FORMAT PrettyCompactMonoBlock
SQL
```

**应当看到：** 每个实例一行，三列都是进程启动以来的条数。`forward_empty`、`sampling_empty`（没有设备时间、看板上留空的步数）打上补丁以后应当接近 0；`no_execution_id` 应当是 0。实验室的输出（替身不写设备时间，前两列等于全部 forward、sampling 记录数）：

```
    ┌─instance─┬─forward_empty─┬─sampling_empty─┬─no_execution_id─┐
 1. │ D0       │          1236 │           1236 │               0 │
 2. │ D1       │          1236 │           1236 │               0 │
 3. │ D2       │          1236 │           1236 │               0 │
…（每个实例一行）
    └──────────┴───────────────┴────────────────┴─────────────────┘
```

**不对时：** `no_execution_id` 不是 0，或者前两列明显大于 0：带回这条命令的输出。

**带回：** 这条命令的输出。

## 带回清单

| 步骤 | 带回什么 |
|---|---|
| 1 | 出错时这条命令的输出 |
| 2 | 出错时这一步的输出（比对共用值那条只有键名和「相同」「不同」，没有密码） |
| 3 | 两台机器上这条命令的输出（都要带回） |
| 4 | 出错时这条命令的输出 |
| 5 | 出现 `FAIL` 时这条命令的输出 |
| 6 | 出错时 `~/vllm-pd-test-glm53/remove-test.log` |
| 7 | 挂载不对或删除报错时的输出 |
| 8 | 不对时这条命令的输出 |
| 9 | 不对时这条命令的输出 |
| 10 | 三条命令的输出（都要带回） |
| 11–14 | 出错时这一步的输出 |
| 15 | 出错时 `~/vllm-pd-test-glm53/add.log` |
| 16 | 不对时两条命令的输出 |
| 17 | 不对时 `bin/puller.sh status` 的输出 |
| 18、19、21、22 | 全部输出（都要带回） |
| 20 | 有别的差别时的输出 |

## 附录 A：原因和背景

- **为什么先删 test、再装 test-glm53。** 两者用同两台机器、同一个节点目录。先删 test，test-glm53 装好以后节点目录里只有它自己的文件；`remove` 在节点上删 mock 程序目录和旧日志时，也不会碰到 test-glm53 的东西。
- **为什么第 2 步要先记下 test 的值。** 原来的新增说明从 test 的 `deploy.env` 取 SSH 账号、节点目录、三项磁盘门槛，并用 `--env-from` 取共用值。test 删掉以后这个文件就没有了：共用值改从 glm53-1 的 `deploy.env` 取（两者相同），其余用 `--set` 传第 2 步记下的值。
- **为什么第 2 步要比对 6 个共用值。** `--env-from` 只取 6 项（`scripts/names.py` 的 `SHARED_KEYS`：Grafana 的地址、账号、密码、容器，docker 网络，监控机地址）；取不到值时 `add` 只打一行「注意」，用模板里的值继续，不会停下。test 删掉以后就比不了了，所以删以前比。
- **磁盘门槛。** test 的三项门槛比新集群的缺省值小（内网 test 是 `PULL_MIN_DISK_GB=16`、`CLICKHOUSE_MIN_DISK_GB=85`、`NODE_MIN_DISK_GB=50`），内网的盘可能就是按它定的；沿用它，`add` 的预检才不会因为门槛过不去。
- **为什么不删 Grafana 文件夹。** test 的看板在 glm53-1 的模型文件夹里，那个文件夹里还有 glm53-1 的看板。`remove` 只删 test 的看板和数据源。
- **为什么要单独删 test 的 Prometheus。** 它是装 test 时手工起的，不归 `remove` 管；`remove` 只从它的配置里去掉面板二的那一段。
- **为什么第 9 步要以 root 删。** `logs` 里的文件属于容器里跑 vLLM 的用户，目录带粘滞位，SSH 账号删不掉别人的文件；`remove` 也只删得掉旧序号的文件，正在写的那一个留着。
- **为什么沿用 `/data/pd-diag`。** vLLM 容器已经挂着它时，打补丁手册只改两个环境变量，不用重建容器。

## 附录 B：已知问题

1. **第 8 步的 `grep -ic test` 是子串匹配：** 标题里带 latest 这类的项也会命中。不是 0 时，先看命中的是不是 test 的东西（第 8 步「不对时」）。
2. **实验室的替身没有本套补丁，**只写 host 时间：第 19 步的段数比现场少（替身不写的 10 段为空：只取设备时间的 6 段，`P wait_for_put`，P 后台 `wait_for_save/put` 下的 3 段）；第 21 步有两条 FAIL（「三类进程都有行」的比例、「P 末到 D 首的间隔」为负），是替身造成的；第 22 步前两列是全部记录数。
3. 如果打补丁手册第 2 步（打补丁前检测）的 run 行是 `logcfg=set`，说明现在的 vLLM 里挂着 test 留下的旧过滤器。分批重启期间，还没重启的那几批会继续往 /data/pd-diag/logs 写旧格式的记录，test-glm53 会把它们收进库，`check` 的过滤器版本在最后一批做完以前不会全部一致。是 `logcfg=unset` 时，没有这个问题。
4. **实验室里 test 是 vllm 模式**（和实验室镜像里内网 test 的 `NODE_MODE=vllm` 相同），节点上没有 mock：第 5 步的 `bin/nodes.sh stop` 在实验室里没有可停的进程。内网 test 如果是 mock 模式，它停的是 mock。
5. **第 22 步的自检计数查询用的是 `GROUP BY node, instance, pid`。** 补丁包操作说明 A 第 7.3 节里同一条查询已经改成 `GROUP BY instance, pid`，以补丁包那份为准；只在一个 pid 带两个 node 标签时，两份的结果才会不同。

## 附录 C：没有测过的部分

- **内网的真实环境：** vLLM 容器的挂载写法（第 3 步）、真实的 SSH 账号权限、真实的磁盘。实验室里替身容器把节点的整个 `/data` 挂进去。
- **第 3 步看不到 `/data/pd-diag` 的那一支：** 实验室里两台机器都能看到，另选节点目录的做法没有走。
- **mock 模式的 test：** 实验室的 test 是 vllm 模式（见附录 B 第 4 条）。
- **打补丁手册本身：** 实验室里用重启替身代替。
- 分批重启的中间状态没在实验室实测过，实验室是一次全部重启的。
- **第 2 步记下的其余几项为空：** `SSH_USER`、`NODE_CONF_DIR`、`NODE_LOG_DIR` 和三项门槛为空的情况没有走（实验室镜像里内网 test 这几项都有值）。`NODE_CONTAINER_UID` 为空（内网 test 的写法）和 `NODE_CONTAINER_UID`、`NODE_CONF_DIR_IN_CONTAINER`、`NODE_LOG_DIR_IN_CONTAINER` 三项都为空，都走过，见交付说明。
