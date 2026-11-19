# 新增 test-glm53 集群：用面板二 v3.1-extra 对接 glm5.3 的测试集群

这份说明在监控机上新增一个只有面板二的集群 `test-glm53`，对接 glm5.3 的真实测试集群：

- **新集群直接用面板二 v3.1-extra 装**，段定义用 glm5.3 的那一份（`SEGMENTS_PROFILE=glm53`），vllm 模式（节点上跑真实的 vLLM）。
- **监控机上已有的集群都不动**，留在 v3：glm53-1、test，以及只有面板一的几个集群。新集群和它们并存，端口、目录、容器、数据源、看板各用各的。
- **看板放在 Grafana 的 `GLM-5.3/test-glm53`**，按看板分层的约定（模型文件夹 `MODEL_GROUP` 就是子文件夹名，加完以后执行一次整理看板命令 `arrange_boards.py`）。
- 新集群没有面板一，告警用它自己的一个 Prometheus，和 test 的做法相同。
- 这份说明只管监控机上的面板二：新增看板、接上节点、打开采集开关、看效果。glm5.3 集群打补丁（vLLM、vllm-ascend、Mooncake、proxy）由操作说明 A 负责，本文在用到的地方引用它。

<!-- 占位符版开始 -->
> **本文有两版。** 这一版是占位符版：路径写成尖括号里的说明，不含任何内网信息。已填路径版把监控机上的个人路径填好，只放在私有的 Caching 仓库。
<!-- 占位符版结束 -->
>
> **预计输出的来源。** 各步的「预计输出」取自实验室 t51 用交付的面板包（sha256 `e3885b93…`）照本文原文做的全新安装走查，10.5 的输出取自附录 B 的换包走查（同一个 test-glm53，10.5 的命令按这一版的写法）；附录 B 的取自从上一版（`52b8824b…`）换包的走查：B.1–B.3 用的就是 `e3885b93…`，B.4 以后用的包和它只差拉取程序 `scripts/native_put.py`（这一处另做了落地核对，见附录 B 开头）。走查前的实验室照内网的形状装好：glm53-1（v3，mock 模式，有面板一）、test（v3，vllm 模式，自己的 Prometheus），两个集群的看板已经按看板分层放在 `GLM-5.3/glm53-1`。test-glm53 的节点上跑的是 vLLM 替身，替身没有本套补丁，所以测试一步的段数和现场不同，那一处另外注明。尖括号 `<…>` 是每次不同的部分。

## 第一页

### 怎么用这份说明

- **已经用上一版说明和上一版面板包（sha256 `52b8824b…`）装好 test-glm53 的，不用重做第 1–9 步，按附录 B 换成这一版。**
- 用更早的面板包（sha256 `76dec56e…`）装好的：【待定】，见附录 B0，先不要照做。
- 先读完这一页，再从第 1 步开始按顺序做。每一步写明在哪台机器上做、命令、预计输出、执行后马上会看到什么，以及对同机其他服务的影响。
- 看到预计输出再做下一步；不一样时停下，把这一步的全部输出发回。
- 除第 7 步在 glm5.3 的 P、D 机器上做以外，都在监控机上做，用当初装面板的那个账号（注册表 `~/.vllm-pd-clusters.json` 在这个账号的家目录里）。
- 第 1 步把常用路径写进一个变量文件，以后每一步都以 `. ~/vllm-pd-test-glm53/vars.sh` 开头。

### 要先准备好的

| 事项 | 怎么确认 |
|---|---|
| glm5.3 测试集群的 P、D 机器地址，每个实例 `vllm serve --port` 的端口，每侧实例的 TP | 现场；第 4 步写进清单 |
| 监控机能用 SSH 免密登录这些机器（和 test 的机器一样） | `ssh <SSH 账号>@<P 机器地址> true` 不问密码 |
| 这些机器上放节点文件和日志的目录（宿主机路径），vLLM 容器里看到的路径，容器里跑 vLLM 的用户的 uid | 部署包 `README.md`「模型推理环境」一节「先向现场确认的事」那张表 |
| glm5.3 的 vLLM、vllm-ascend 已经按操作说明 A 打了补丁（写 `[PD_DIAG]` 记录） | 操作说明 A 第 8 步的检测；没打时面板二收不到记录，各段为空 |
| 这个集群的 Mooncake master 地址（第 4 步写清单时用） | 现场 |

<!-- 占位符表开始 -->
### 文中的占位符

| 占位符 | 含义 | 在哪里查 |
|---|---|---|
| `<用户目录>` | 放部署包和更新脚本的目录 | 包二 v3 更新脚本「现场路径」一段的 `USER_DIR` |
| `<总目录>` | 面板目录的上级目录 | 同一个脚本的 `ROOT` |
| `<更新脚本>` | 更新脚本的文件名（附录 B 用） | 路径已填好的是 `vllm_pd_update_pack2_v31extra_filled.py`；用 `vllm_pd_update_pack2_v31extra.py` 时先改好它「现场路径」一段 |
<!-- 占位符表结束 -->
| `<SSH 账号>` | 监控机登录 glm5.3 机器用的账号 | 现场；test 用的是哪个，见下面「现场的值怎么查」第 2 条 |
| `<节点目录>` | glm5.3 机器上放节点文件和日志的目录（宿主机路径） | 现场；它下面会有 `conf`、`logs` 两个子目录 |
| `<容器里的节点目录>` | 同一个目录在 vLLM 容器里的路径，和宿主机相同时不用填 | 现场 |
| `<容器用户 uid>` | vLLM 容器里跑 vLLM 的用户的 uid | 在 glm5.3 机器上 `docker exec <容器名> id -u` |
| `<模型名>` | 看板「模型」下拉框里显示的名字 | glm5.3 的 `vllm serve --served-model-name`；不知道时填 `glm-5.3` |
| `<P 机器地址>`、`<D 机器地址>`、`<端口>`、`<P 实例的 TP>`、`<D 实例的 TP>`、`<Mooncake master 的地址>:<端口>` | 清单里的机器和实例 | 第 4 步 |
| `<proxy 机器地址>`、`<proxy 日志目录>`、`<P 实例日志文件>` | 第 9 步打开两个采集来源时填的值 | 操作说明 A 第 5、6 步和环境检测结果，见第 9 步 |

### 现场的值怎么查（都在监控机上，只读）

1. **已经装了哪些集群、各占哪个端口段：**

   ```bash
   cd <总目录>/vllm-pd-grafana-panel2/test && bin/panel2_cluster.sh list
   ```

   **执行后看到：** 每个集群一行，列出模型文件夹、端口基数、看板 UID 和目录。test-glm53 还不在里面。

2. **test 用的 SSH 账号和节点参数**（新集群的机器不同，值可能不同，这里只作参考）：

   ```bash
   grep -E '^(SSH_USER|NODE_CONF_DIR|NODE_LOG_DIR|NODE_CONF_DIR_IN_CONTAINER|NODE_LOG_DIR_IN_CONTAINER|NODE_CONTAINER_UID)=' <总目录>/vllm-pd-grafana-panel2/test/deploy.env
   ```

3. **新集群的端口基数**：第 3 步用命令取，不用手算。

实验室里第 1 条的输出：

```
集群             模型文件夹        端口基数      面板二看板 UID                  面板一看板 UID                  目录
glm53-1        glm53-1      18100     vllm-pd-request-glm53-1    vllm-pd-prefill-glm53-1 vllm-pd-decode-glm53-1 <总目录>/vllm-pd-grafana-panel2/glm53-1 <总目录>/vllm-pd-grafana-panel1/glm53-1
test           glm53-1      18110     vllm-pd-request-test       -                          <总目录>/vllm-pd-grafana-panel2/test
```

### 要做的几步

| 步 | 做什么 | 在哪 | 实验室用时 |
|---|---|---|---|
| 1 | 设变量 | 监控机 | 几秒 |
| 2 | 放面板包，核对、解开 | 监控机 | 约 20 秒（不含拷文件） |
| 3 | 取端口基数 | 监控机 | 几秒 |
| 4 | 写清单 `inventory.json` | 监控机 | 按机器数 |
| 5 | 起 test-glm53 自己的 Prometheus | 监控机 | 约 10 秒 |
| 6 | 装 test-glm53 的面板二（`add`） | 监控机 | 约 1 分钟 |
| 7 | 接上面板二：给 glm5.3 的 vLLM 加挂载和环境变量，重启 | glm5.3 的 P、D 机器 | 按现场的启动方式 |
| 8 | 看板放进 `GLM-5.3/test-glm53` | 监控机 | 约 10 秒 |
| 9 | 打开 proxy、Mooncake 两个采集来源 | 监控机 | 几秒 |
| 10 | 测试：`check`、`coverage.sh`、`quality`、自检行的两个计数 | 监控机 | 约 15 分钟（含等待） |
| 附录 A | 删掉 test-glm53（需要时） | 监控机，然后 glm5.3 的机器 | 约 1 分钟 |
| 附录 B | 已经用上一版装好的 test-glm53 换成这一版（只在已经装过时做） | 监控机，然后 glm5.3 的机器 | 约 1 分钟（不含 vLLM 重启和测试） |

### 对同机其他服务的影响

| 机器 | 服务 | 影响 |
|---|---|---|
| 监控机 | glm53-1、test 和其他已有集群 | 不碰：目录、容器、注册表里它们的条目、数据都不变，容器不重启。实验室用 `arrange_boards.py state` 前后比过，见第 10 步 |
| 监控机 | Grafana | 不重启。新加一个数据源「vLLM ClickHouse（test-glm53）」和一个看板；第 8 步把看板挪进 `GLM-5.3/test-glm53` |
| 监控机 | Docker | 新加 3 个容器：`vllm-pd-prometheus-test-glm53`、`vllm-pd-clickhouse-test-glm53`、`vllm-pd-vector-test-glm53`（还有一个拉取程序进程），各自的数据卷；占用端口基数起的 3 个端口 |
| 监控机 | 磁盘 | 面板包解到 `<用户目录>/vllm-pd-panel2-v3.1-extra/pack/`，约 390 MB；集群目录 `<总目录>/vllm-pd-grafana-panel2/test-glm53`；ClickHouse 数据和拉取镜像按请求量增长 |
| 监控机 | 注册表 `~/.vllm-pd-clusters.json` | 多一个 test-glm53 的条目，别的条目不变 |
| glm5.3 的机器 | vLLM | 第 7 步加两个挂载、两个环境变量，重启一次；之后 `[PD_DIAG]` 记录改写进 `<节点目录>/logs` |
| glm5.3 的机器 | 拉取 | 拉取程序每 10 秒经 SSH 和 rsync 读一次 `<节点目录>/logs` |

## 第 1 步：设变量

**机器：** 监控机。

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
ENV_TEST=$ROOT/vllm-pd-grafana-panel2/test/deploy.env
ENV2=$ROOT/vllm-pd-grafana-panel2/glm53-1/deploy.env
GLM_INV=$ROOT/vllm-pd-grafana-panel1/glm53-1/prometheus/inventory.json
T45=$UD/vllm-pd-boards-clusters
SV=~/vllm-pd-test-glm53
B=$(cat ~/vllm-pd-test-glm53/port_base 2>/dev/null || true)
EOF
. ~/vllm-pd-test-glm53/vars.sh && ls -d "$OFF" "$ENV_TEST" "$ENV2" "$GLM_INV" "$T45/arrange_boards.py" "$T45/boards_layout.txt" "$T45/make_inventories.py" "$T45/clusters.txt" ~/.vllm-pd-clusters.json && echo 变量已设
```

**预计输出：** 列出 9 个路径，最后一行 `变量已设`（实验室）。

- `OFF` 是面板二一直用的离线包（镜像），已经在监控机上，不用再拷。
- `T45` 是看板分层时放整理看板命令和 `make_inventories.py` 的目录；`GLM_INV` 是 glm53-1 面板一的清单，`make_inventories.py` 从它取缺省值。
- `ENV2` 只用来给整理看板命令读 Grafana 的地址和账号，不改它。

**执行后马上会看到：** 家目录下多了 `~/vllm-pd-test-glm53/vars.sh`，别的都不变。

**先记下现状，第 10 步拿来对照：**

```bash
. ~/vllm-pd-test-glm53/vars.sh && python3 "$T45/arrange_boards.py" state --env-from "$ENV2" > "$SV/state-before" && grep -c '' "$SV/state-before"
```

**预计输出：** 一个行数。`state-before` 里每个已登记集群的模型文件夹、各文件的 sha256、看板的版本和位置、容器的启动时间各一行。

**对同机其他服务的影响：** 只读。

## 第 2 步：放面板包，核对、解开

**机器：** 监控机。

把交付的面板包 `vllm-pd-grafana-panel2.tar.gz` 和 `vllm-pd-grafana-panel2.tar.gz.sha256`（ServingBench `包二-v3.1-extra/2-面板二v3.1-extra/`）拷进 `<用户目录>/vllm-pd-panel2-v3.1-extra/`：

```bash
. ~/vllm-pd-test-glm53/vars.sh && mkdir -p "$NEW" && ls "$NEW"
```

拷完以后：

```bash
. ~/vllm-pd-test-glm53/vars.sh && cd "$NEW" && sha256sum -c vllm-pd-grafana-panel2.tar.gz.sha256 && rm -rf pack && mkdir pack && tar xzf vllm-pd-grafana-panel2.tar.gz -C pack && tar xzf "$OFF" -C pack/vllm-pd-grafana-panel2 && cd pack/vllm-pd-grafana-panel2 && sha256sum -c SHA256SUMS --quiet && (cd offline && sha256sum -c SHA256SUMS --quiet) && cat VERSION && ls profiles
```

**预计输出**（实验室）：

```
vllm-pd-grafana-panel2.tar.gz: OK
tar: Ignoring unknown extended header keyword 'LIBARCHIVE.xattr.com.apple.provenance'
…（同样的一行共 7 行）
v3.1-extra
dsv41
glm53
```

解离线包时打印的几行 `tar: Ignoring unknown extended header keyword 'LIBARCHIVE.xattr.com.apple.provenance'` 来自离线包本身（它是在 Mac 上打的），不影响解包和校验。

**执行后马上会看到：** `<用户目录>/vllm-pd-panel2-v3.1-extra/pack/vllm-pd-grafana-panel2/` 是解开的面板包。它是单独的目录，不碰 v3 的任何目录。

**对同机其他服务的影响：** 磁盘多约 390 MB。

## 第 3 步：取端口基数

**机器：** 监控机。

```bash
. ~/vllm-pd-test-glm53/vars.sh && cd "$PK" && python3 scripts/names.py alloc --cluster "$C" | tee "$SV/port_base"
```

**预计输出：** 一个数，是注册表里还没被占、端口也空着的下一个端口段的起点。实验室是 `18120`（实验室只装了 glm53-1、test 两个集群）；内网已经用到 18180，应当是 18190，以命令输出为准。这个数存进 `~/vllm-pd-test-glm53/port_base`，变量文件里的 `B` 就是它。

新集群占 `B`（ClickHouse）、`B+1`（拉取程序）、`B+2`（它自己的 Prometheus）三个端口。

**对同机其他服务的影响：** 只读，不登记。

## 第 4 步：写清单 `inventory.json`

**机器：** 监控机。

清单列出 glm5.3 测试集群的每个 P、D 实例。用看板分层时的 `make_inventories.py` 生成，做法和加面板一的集群时相同：在清单文件 `clusters.txt` 里加一套 test-glm53 的行，再生成这一套的 `inventory.json`。

**4.1 在 `clusters.txt` 里加一套 test-glm53。** 先备份，再在末尾加 3 行模板：

```bash
. ~/vllm-pd-test-glm53/vars.sh && cp -p "$T45/clusters.txt" "$SV/clusters.txt.before" && printf '\n# test-glm53：glm5.3 的测试集群（面板二）\n集群 test-glm53 master=<Mooncake master 的地址>:<端口>\nP <P 机器地址> 端口=<端口> TP=<P 实例的 TP>\nD <D 机器地址> 端口=<端口> TP=<D 实例的 TP>\n' >> "$T45/clusters.txt" && tail -n 4 "$T45/clusters.txt"
```

再用编辑器把这几行换成现场的值。写法和 `clusters.txt` 开头的注释相同：

- 一台机器一行，`P` 或 `D` 打头；同一侧的机器按 DP rank 从小到大写，第 n 个 API server（从 0 数）就是实例 P<n>、D<n>；
- `端口=` 写这台机器上每个实例 `vllm serve --port` 的值：`端口=9000`、`端口=9000-9015` 或 `端口=9000,9002`；
- `TP=` 写这台机器上每个实例的 TP（glm5.3 现在是 P 每实例 TP16、D 每实例 TP1，以现场为准）；
- 换完以后这一套的行里不能再有 `<` 或 `>`。别的几套的行不要动。

**4.2 生成，补上模型名，核对：**

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

```bash
. ~/vllm-pd-test-glm53/vars.sh && cd "$PK" && python3 scripts/inventory_tool.py "$SV/inventory.json" summary
```

**预计输出**（实验室：P 1 台机器 2 个实例、D 1 台机器 8 个实例，TP 都是 2）：

```
集群 test-glm53：P 1 台 2 个实例（P0–P1），D 1 台 8 个实例（D0–D7），master <Mooncake master 的地址>:<端口>；抓取目标 15 个；两个列表的写法 → <家目录>/vllm-pd-test-glm53/inv/test-glm53/inventory.json
RESULT: OK（集群 test-glm53，2 台机器；清单文件里其他集群的行没有读）
写好 <家目录>/vllm-pd-test-glm53/inventory.json
proxy: （清单里没有，这一版不采 proxy）
P: 2 个实例（P0–P1），1 台机器
D: 8 个实例（D0–D7），1 台机器
机器总数: 2
```

- `make_inventories.py` 写出的清单另有面板一用的几项（`mooncake_master`、exporter 端口等），面板二不读，留着没关系。
- 面板二只读 `prefill`、`decode` 两侧的 `api_servers`（`机器地址:端口`）和 `procs_per_instance`（TP），以及 `model`。`model` 只决定看板「模型」下拉框里的名字。

**4.3 同一侧各机器的 TP 不同时：** `make_inventories.py` 会写成面板一的 `api_servers` 写法，面板二读不了，4.2 的第二条命令会停下。这时直接写 `$SV/inventory.json`，格式如下，写完再执行 4.2 的第三条命令核对：

```json
{
  "model": "<模型名>",
  "prefill": {"procs_per_instance": <P 实例的 TP>, "api_servers": ["<P 机器地址>:<端口>", "<P 机器地址>:<端口>"]},
  "decode": {"procs_per_instance": <D 实例的 TP>, "api_servers": ["<D 机器地址>:<端口>", "<D 机器地址>:<端口>"]}
}
```

**执行后马上会看到：** `$T45/clusters.txt` 多了 test-glm53 一套；`~/vllm-pd-test-glm53/` 下多了 `inv/test-glm53/inventory.json` 和 `inventory.json`。**对同机其他服务的影响：** 无，面板一的清单不动。

## 第 5 步：起 test-glm53 自己的 Prometheus

**机器：** 监控机。新集群没有面板一，面板二的告警（拉取程序、采集器的故障）写进它自己的 Prometheus，和 test 的做法相同。网络和镜像取自 glm53-1 面板一的 Prometheus 容器。

```bash
. ~/vllm-pd-test-glm53/vars.sh && NET=$(docker inspect -f '{{range $k, $v := .NetworkSettings.Networks}}{{$k}} {{end}}' vllm-pd-prometheus-glm53-1 | awk '{print $1}') && IMG=$(docker inspect -f '{{.Config.Image}}' vllm-pd-prometheus-glm53-1) && echo "网络 $NET，镜像 $IMG，端口 $((B+2))" && mkdir -p "$PDT" && printf 'global:\n  scrape_interval: 15s\n  evaluation_interval: 15s\nscrape_configs:\n' > "$PDT/prometheus.yml" && chmod 644 "$PDT/prometheus.yml" && docker run -d --name vllm-pd-prometheus-$C --network "$NET" --restart unless-stopped -p $((B+2)):9090 -v "$PDT/prometheus.yml:/etc/prometheus/prometheus.yml:ro" -v vllm-pd-prometheus-data-$C:/prometheus "$IMG" --config.file=/etc/prometheus/prometheus.yml --storage.tsdb.path=/prometheus --web.enable-lifecycle >/dev/null && for i in $(seq 1 30); do curl -s localhost:$((B+2))/-/ready && break; sleep 2; done
```

**预计输出**（实验室）：

```
网络 <网络名>，镜像 prom/prometheus:v3.11.2，端口 <B+2>
Prometheus Server is Ready.
```

报端口被占用时，`ss -ltnp | grep $((B+2))` 看是谁占着，发回输出。

**执行后马上会看到：** 新容器 `vllm-pd-prometheus-test-glm53`（内存约百兆）、数据卷 `vllm-pd-prometheus-data-test-glm53`、文件 `<总目录>/vllm-pd-prometheus-test-glm53/prometheus.yml`。它不接入 Grafana，只用来告警，告警在 `http://<监控机地址>:<B+2>/alerts` 看。

**对同机其他服务的影响：** 占用端口 `B+2`。glm53-1、test 的 Prometheus 不受影响。

## 第 6 步：装 test-glm53 的面板二

**机器：** 监控机。

**先看 test 的磁盘门槛。** `add` 的预检按 `deploy.env` 里三项磁盘门槛检查剩余空间，新集群的缺省值是 `PULL_MIN_DISK_GB=200`、`CLICKHOUSE_MIN_DISK_GB=1000`、`NODE_MIN_DISK_GB=50`。test 装的时候改过的，新集群沿用同样的值：

```bash
. ~/vllm-pd-test-glm53/vars.sh && grep -E '^(PULL_MIN_DISK_GB|CLICKHOUSE_MIN_DISK_GB|NODE_MIN_DISK_GB)=' "$ENV_TEST"
```

**执行后看到：** 最多三行。列出来的每一项，在下面 `add` 命令的末尾加一个同样的 `--set`（比如 `--set CLICKHOUSE_MIN_DISK_GB=<test 的值>`）；没列出来的不加。实验室的输出：

```
PULL_MIN_DISK_GB=5
CLICKHOUSE_MIN_DISK_GB=20
NODE_MIN_DISK_GB=5
```

**再装。** 把节点参数换成现场的值（`<容器里的节点目录>` 和宿主机相同时，去掉那两个 `--set`）：

```bash
. ~/vllm-pd-test-glm53/vars.sh && cd "$PK" && bin/panel2_cluster.sh add "$C" --model-group "$C" --mode vllm --port-base "$B" --inventory "$SV/inventory.json" --env-from "$ENV_TEST" --set PROMETHEUS_YML="$PDT/prometheus.yml" --set SEGMENTS_PROFILE=glm53 --set SSH_USER=<SSH 账号> --set NODE_CONF_DIR=<节点目录>/conf --set NODE_LOG_DIR=<节点目录>/logs --set NODE_CONF_DIR_IN_CONTAINER=<容器里的节点目录>/conf --set NODE_LOG_DIR_IN_CONTAINER=<容器里的节点目录>/logs --set NODE_CONTAINER_UID=<容器用户 uid> 2>&1 | tee "$SV/add.log"
```

- `--model-group test-glm53`：模型文件夹就是看板分层里的子文件夹名（第 8 步把它放到 `GLM-5.3` 下面）。
- `--env-from` 取 test 的 `deploy.env` 里几项共用的值：Grafana 的地址和账号、docker 网络、监控机地址。别的都不取。
- `SEGMENTS_PROFILE=glm53` 写进新集群的 `deploy.env`，`add` 把包里 `profiles/glm53/segments.json` 拷成集群目录的 `segments.json`。
- ClickHouse 的三个密码随机生成，写在新集群的 `deploy.env` 里（权限 600）。

**预计输出**（实验室，摘出开头、各步的结论和结尾；中间每台机器一段的节点参数在第 7 步用）：

```
共用值来自 <总目录>/vllm-pd-grafana-panel2/test/deploy.env
已写 <总目录>/vllm-pd-grafana-panel2/test-glm53/deploy.env：集群 test-glm53，模型文件夹 test-glm53，端口基数 <B>（ClickHouse <B>、拉取程序 <B+1>、Prometheus <B+2>）
段定义：profiles/glm53/segments.json（deploy.env 的 SEGMENTS_PROFILE）
==== 集群 test-glm53：查注册表、端口和清单
RESULT: OK（集群 test-glm53：注册表里没有重复，端口空闲或属于本集群）
已登记集群 test-glm53 的面板二（<家目录>/.vllm-pd-clusters.json）
==== 步骤 3：导入镜像（已有就跳过）
==== 步骤 6：预检监控机
RESULT: OK（24 项全过）
==== 步骤 7：预检所有节点
RESULT: OK（2 台全过）
==== 步骤 9：启动 ClickHouse … RESULT: OK
==== 步骤 10：启动采集器 … RESULT: OK
==== 步骤 11：给 Grafana 装 ClickHouse 插件（已装就跳过） … RESULT: OK
==== 步骤 12：导入数据源和看板 … RESULT: OK
==== 步骤 13：让本集群的 Prometheus 抓采集链路的指标 … RESULT: OK
==== 步骤 14：下发三个节点文件
RESULT: OK（2 台全过）
==== 步骤 15：节点自检
RESULT: OK（2 台全过，其中 2 台有 WARN）
==== 步骤 17：启动拉取程序 … RESULT: OK
==== 每台集群机器要加的目录、挂载和环境变量（bin/nodes.sh paths）
<P 机器地址>
  <节点目录>/conf/  pd_diag_logging.json  pd_diag_logfilter.py  segments.json
  <节点目录>/logs/<端口>/  api.<启动时间>.<pid>.<序号>.log  engine.….log  worker.….log    （P0，vllm serve --port <端口>）
  …（每个实例一行）
  这台机器一组：启动脚本里、vllm serve 之前加这两行，所有 rank 共用
    export VLLM_LOGGING_CONFIG_PATH=<容器里的节点目录>/conf/pd_diag_logging.json
    export PYTHONPATH=<容器里的节点目录>/conf${PYTHONPATH:+:$PYTHONPATH}
  容器加挂载：-v <节点目录>/conf:<容器里的节点目录>/conf:ro -v <节点目录>/logs:<容器里的节点目录>/logs（容器里已经能在同一路径看到这两个宿主机目录时，不用加）
…（每台机器一段）
共 <n> 台机器、<n> 个实例（P <n> 个，D <n> 个）；写进了 generated/node_paths.txt
RESULT: OK（集群 test-glm53 的监控机部分装好了。还没接上的 vLLM 实例按上面的输出加挂载和环境变量、重启 vLLM；已经接好的不用改。然后执行 bin/panel2_cluster.sh check test-glm53）
```

**执行后马上会看到：**

- `bin/panel2_cluster.sh list` 多了一行 test-glm53；
- Grafana 里多了一个顶层文件夹 `test-glm53`，里面是看板「面板二：请求时延拆解（P / D）（test-glm53）」，还没有数据（第 7 步重启 vLLM 以后才有）；
- glm5.3 的机器上 `<节点目录>/conf` 里有了三个节点文件。

**对同机其他服务的影响：** 见第一页的表。

**`add` 中途停下时：** 照停下那一步的 FAIL 行处理，然后重跑同一条命令：做过的步骤内容不变就跳过，`deploy.env`、清单都沿用。查注册表一步报端口段被占时，回到第 3 步重取。

## 第 7 步：接上面板二：给 glm5.3 的 vLLM 加挂载和环境变量，重启

**机器：** glm5.3 测试集群的每台 P、D 机器。

这一步只把面板二的过滤器接到 vLLM 上，不打补丁；补丁按操作说明 A 打。还没打补丁的，可以把这一步和操作说明 A 第 7 步的重启合在一次。

照第 6 步输出里每台机器那一段做（部署包 `README.md`「模型推理环境」一节的步骤 V2–V4 有完整的说明）：

1. vLLM 容器能看到 `<节点目录>/conf`、`<节点目录>/logs` 两个目录：输出里「容器加挂载」一行；容器里已经能在同一路径看到它们的，不用加；都不是的，要重建容器。
2. 在容器里执行 `vllm serve` 的那个脚本里、`vllm serve` 之前加两行（输出里「这台机器一组」下面那两行 `export`）。一个脚本起多个 DP rank 时也只加一次。
3. 按你们平时的方式重启 vLLM。

**执行后马上会看到：** vLLM 照常启动；`[PD_DIAG]` 行不再出现在 vLLM 的标准输出里，改写进 `<节点目录>/logs/<端口>/` 下的文件。几分钟后看板上有数据。

**对同机其他服务的影响：** 和平时重启 vLLM 相同。

## 第 8 步：看板放进 `GLM-5.3/test-glm53`

**机器：** 监控机。

部署包的 `add` 只认顶层文件夹，所以看板先落在顶层的 `test-glm53` 里。在看板分层的对照表里加一行，再执行一次整理看板命令：

```bash
. ~/vllm-pd-test-glm53/vars.sh && cp -p "$T45/boards_layout.txt" "$SV/boards_layout.before" && printf 'GLM-5.3        test-glm53     test-glm53\n' >> "$T45/boards_layout.txt" && tail -n 3 "$T45/boards_layout.txt" && python3 "$T45/arrange_boards.py" plan --env-from "$ENV2"
```

`plan` 只看不改。核对它只列出 test-glm53 的 3 项（建文件夹 `GLM-5.3/test-glm53`、挪看板、删空文件夹 `test-glm53`，最后一行 `RESULT: 要改 3 项`）以后，执行：

```bash
. ~/vllm-pd-test-glm53/vars.sh && python3 "$T45/arrange_boards.py" apply --env-from "$ENV2" 2>&1 | tee "$SV/arrange-apply.log"
```

**预计输出**（实验室）：

```
== 对照表 boards_layout.txt：10 个集群；已登记的 3 个：glm53-1、test、test-glm53；还没登记的：glm53-0、glm53-2、dsv41-flash-1、dsv41-flash-2、dsv41-flash-3、dsv41-flash-4、dsv41-flash-5
== 备份到 <家目录>/vllm-pd-arrange-backup/<时间>（注册表、要改的 deploy.env；manifest.json 记下每一项改动）
已建 文件夹 GLM-5.3/test-glm53
已挪 看板 vllm-pd-request-test-glm53「面板二：请求时延拆解（P / D）（test-glm53）」：test-glm53 → GLM-5.3/test-glm53
已删 空文件夹 test-glm53（uid <…>）
== 核对
OK   再看一遍：没有要改的
RESULT: OK（改了 3 项；回退：python3 arrange_boards.py undo <家目录>/vllm-pd-arrange-backup/<时间> --env-from <同一个 deploy.env>）
```

**执行后马上会看到：** 看板在 `GLM-5.3/test-glm53` 里，UID 和链接不变；顶层的 `test-glm53` 文件夹空了，被删掉。

**对同机其他服务的影响：** 只挪 test-glm53 的看板。别的集群的看板已经在各自的位置，`apply` 不动它们（`plan` 里没有它们的行）。

## 第 9 步：打开 proxy、Mooncake 两个采集来源

**机器：** 监控机。两个来源各管几段，不打开时那几段为空，其余照常：

| 来源 | 管哪几段 | 前提 |
|---|---|---|
| proxy | P、D 的 lb_proxy | proxy 按操作说明 A 第 5.2 节换了文件，设了 `PD_PROXY_DIAG=1` 和 `PD_PROXY_DIAG_DIR` |
| Mooncake | P 后台 wait_for_save/put 下的 submit、wait_for_transfer、finalize | P 的 Mooncake 按操作说明 A 第 5.1 节换成了打补丁 04 的库 |

**命令：** 在 test-glm53 的 `deploy.env` 末尾加 6 行（只打开一个时，只加那一个的几行），然后重启拉取程序：

- `<proxy 机器地址>`：proxy 所在机器，写法和清单里的机器相同；`<proxy 日志目录>`：操作说明 A 里给 proxy 设的 `PD_PROXY_DIAG_DIR` 在宿主机上的路径。
- `<P 实例日志文件>`（`MOONCAKE_LOG_FILES`）：每个 P 实例 vLLM 日志文件的宿主机路径，也就是这个实例的标准错误重定向到的那个文件。写法是 `<P 机器地址>=/宿主机/路径/文件名`；一台机器上有几个 P 实例时，路径之间用分号分开，几台机器之间用逗号。`SSH_USER` 要能读这些文件。
- **不用设任何 GLOG 变量。** 依据是环境检测结果：现场没有设 `GLOG_*`，glog 版本是 0.6.0；没有初始化 glog 时，各级别的日志只写到标准错误，不写日志文件（glog v0.6.0 `src/logging.cc:1830–1846`）；现场每个实例的标准错误都重定向到了文件。
- `MOONCAKE_TID_FALLBACK` 填 `0`：glog 0.6.0 日志行里的线程号就是 gettid，能和保存任务记录的 `native_tid` 对上。
- 第二次环境检测会输出 `glog_files=`。它不为 0 时，说明有别的库初始化了 glog，这一项的写法要另外定，把这一行检测结果带回来。

```bash
. ~/vllm-pd-test-glm53/vars.sh && printf '\nPROXY_COLLECT=1\nPROXY_MACHINE=%s\nPROXY_LOG_DIR=%s\nMOONCAKE_LOG_COLLECT=1\nMOONCAKE_LOG_FILES=%s\nMOONCAKE_TID_FALLBACK=0\n' '<proxy 机器地址>' '<proxy 日志目录>' '<P 实例日志文件>' >> "$D/deploy.env"
```

```bash
. ~/vllm-pd-test-glm53/vars.sh && cd "$D" && bin/puller.sh restart && bin/puller.sh status
```

**预计输出：** `bin/puller.sh status` 每台机器一行 OK，比原来多 proxy 机器一行和 `mooncake` 一行，最后 `RESULT: OK`。实验室的输出：

```
RESULT: OK（集群 test-glm53：注册表里没有重复，端口空闲或属于本集群）
拉取程序已启动，每 10 秒拉一轮，镜像到 <PULL_DIR>
RESULT: OK
OK   <P 机器地址>   <P 机器地址>   上次成功 1.9 秒前
OK   <D 机器地址>   <D 机器地址>   上次成功 1.9 秒前
OK   <proxy 机器地址>   <proxy 机器地址>   上次成功 1.9 秒前
OK   mooncake   _native   上次成功 1.9 秒前
镜像占用 <大小>
RESULT: OK
```

**执行后马上会看到：** 拉取程序重启，几秒没有新数据。两个 proxy 段从下一个经 proxy 的请求起有值；Mooncake 计时行从这时起才取，开关打开以前写的行不取。

**对同机其他服务的影响：** 拉取程序每 10 秒经 SSH 读一次 `<proxy 日志目录>` 和各来源里新增的 Mooncake 计时行。`logs:` 写法每轮执行一次 `docker logs --since`，日志很大时在节点上的开销没有测过。

**关掉：** 把 `$D/deploy.env` 里 `PROXY_COLLECT`、`MOONCAKE_LOG_COLLECT` 的 `1` 改成 `0`，再执行 `bin/puller.sh restart`。

## 第 10 步：测试

**机器：** 监控机。第 7 步重启以后等至少 3 分钟再跑 10.1–10.3，至少 10 分钟再跑 10.4。

### 10.1 升级安全检查

```bash
. ~/vllm-pd-test-glm53/vars.sh && cd "$D" && bin/panel2_cluster.sh check "$C"
```

**预计输出**（实验室）：

```
OK   版本和文件：面板二目录是 v3.1-extra
OK   版本和文件：文件都和 SHA256SUMS 相同（segments.json 只差 log_dir 和 keep_hours；它是 profiles/glm53/segments.json）
OK   容器：vllm-pd-clickhouse-test-glm53 在运行
OK   容器：vllm-pd-vector-test-glm53 在运行
OK   容器：采集器配置 Vector 自己检查过
OK   库：表和视图都在，视图读的是 pd-diag-v3.1-extra-glm53-2026-11-19c
OK   Grafana：能登录，看板和数据源都在
OK   节点：<机器> 三个节点文件的 sha256 和监控机相同（segments.json <8 位>）
OK   过滤器版本：<n> 个实例 <n> 个进程都是 pd-diag-v3.1-extra-glm53-2026-11-19c@<8 位>
OK   链路通：拉取 <n> 台，最久的 <n> 秒前成功
OK   链路通：Prometheus 抓 vllm-timing-puller-test-glm53 正常
OK   链路通：Prometheus 抓 vllm-timing-central-test-glm53 正常
OK   链路通：最近 <n> 秒这一版的自检行 <n> 行、汇总行 <n> 行
OK   日志量和空闲空间：…（每台节点一行，镜像盘一行）
RESULT: OK
```

### 10.2 各段有值的请求数

```bash
. ~/vllm-pd-test-glm53/vars.sh && cd "$D" && bin/coverage.sh 200 30
```

**预计输出：** 每段一行，然后是 P 缓存池各段里值来自共用任务的请求数（C9，只在命令行报），最后两行汇总。glm5.3 的段定义里 P 的 start_load_kv/wait_for_transfer 没有来源（P 同步加载），和 P 的 api 网关一起单列在「没有来源、按设计为空」里。P 的 start_load_kv/submit 是同步加载的时长，含同步 get，只在命中缓存池的请求上有值（C11，《面板系统说明书》3.6.5）。

都打上补丁、两个采集来源都打开时（出处：端到端测试 `tests/t51/e2e_v31.py` 的 glm53 一套，以 rc1 的日志为底造出 glm5.3 形状的记录；实验室的替身得不出）：

```
47 段里 45 段有值；没有来源、按设计为空的 2 段：P api_gateway、P start_load_kv/wait_for_transfer
RESULT: OK
```

少了哪一件，哪几段就为空，最后一行变成 `RESULT: FAIL（有来源但这些请求里都为空的 … 段：…）`：

| 少了 | 为空的段 |
|---|---|
| proxy 采集（第 9 步没开，或 proxy 没打补丁） | P、D 的 lb_proxy |
| Mooncake 采集（第 9 步没开，或 Mooncake 没打补丁 04） | P 后台 wait_for_save/put 下的 submit、wait_for_transfer、finalize |
| vllm-ascend 上本套补丁的「等 put 完成」 | P 的 wait_for_put |
| 这批请求里没有 P 命中缓存池的（P 的加载记录都是 `all_hit`、`no_task`、`skipped`、`missing_load_spec`） | P 的 start_load_kv 和 submit |
| 这批请求里 P 没有保存 KV 的 | P 后台 wait_for_save 和它的子段 |

实验室的输出（替身没有本套补丁；第 9 步打开了两个来源，proxy 段有值；替身不写 Mooncake 计时行，put 下 3 段仍为空；替身只写 host 时间，只取设备时间的 6 段——P、D 的 forward、sampling，D 第二步起的 forward、sampling——也为空，C15）：

```
段定义 pd-diag-v3.1-extra-glm53-2026-11-19c：最近 30 分钟里 200 个请求（最多取 200 个）
…（每段一行，共 47 行；然后是 P 缓存池各段里值来自共用任务的请求数）
47 段里 35 段有值；没有来源、按设计为空的 2 段：P api_gateway、P start_load_kv/wait_for_transfer
RESULT: FAIL（有来源但这些请求里都为空的 10 段：P wait_for_put、P forward、P sampling、P 后台 wait_for_save/put/submit、P 后台 wait_for_save/put/wait_for_transfer、P 后台 wait_for_save/put/finalize、D forward、D sampling、D 第二步起 forward、D 第二步起 sampling）
```

### 10.3 已有集群没有变

```bash
. ~/vllm-pd-test-glm53/vars.sh && python3 "$T45/arrange_boards.py" state --env-from "$ENV2" --except "$C" > "$SV/state-after" && diff "$SV/state-before" "$SV/state-after"; echo "diff 结束"
```

**预计输出**（实验室）：只多出 test-glm53 自己的 3 个容器各一行，没有别的差别：

```
<行号>a<行号>
> 容器 vllm-pd-clickhouse-test-glm53 启动于 <时间>
<行号>a<行号>
> 容器 vllm-pd-prometheus-test-glm53 启动于 <时间>
<行号>a<行号>
> 容器 vllm-pd-vector-test-glm53 启动于 <时间>
diff 结束
```

`state` 列出监控机上全部正在运行的容器，`--except` 只去掉这个集群自己的文件和看板，所以新集群的 3 个容器会出现在差别里。除此之外，glm53-1、test 等已有集群的模型文件夹、文件、看板（版本和位置）、容器的启动时间都和第 1 步记下的相同；出现别的差别时停下，发回输出。

### 10.4 数据准确性检查

```bash
. ~/vllm-pd-test-glm53/vars.sh && cd "$D" && bin/panel2_cluster.sh quality "$C"
```

**预计输出：** `RESULT: OK`，只报告，不拦任何命令。实验室的替身有两条 FAIL（「三类进程都有行」的比例、「P 末到 D 首的间隔」为负），test 在 v3 下同样，是替身造成的，所以这一处不是实验室得出的。

### 10.5 过滤器自检行的两个计数

```bash
. ~/vllm-pd-test-glm53/vars.sh && cd "$D" && bash -c '. bin/lib.sh && load_env >/dev/null && curl -s -u "vllm_reader:$CH_READER_PASSWORD" "http://localhost:$CLICKHOUSE_HTTP_PORT/" --data-binary @-' <<'SQL'
SELECT instance, sum(f) AS forward_empty, sum(s) AS sampling_empty, sum(e) AS no_execution_id FROM (SELECT instance, argMax(acc_fallback['forward'], ts) AS f, argMax(acc_fallback['sampling'], ts) AS s, argMax(exec_missing, ts) AS e FROM vllm_pd.filter_selfcheck WHERE ts > now() - INTERVAL 10 MINUTE GROUP BY node, instance, pid) GROUP BY instance ORDER BY instance FORMAT PrettyCompactMonoBlock
SQL
```

集群目录的 `deploy.env` 里不写 ClickHouse 的端口（按 `PORT_BASE` 派生），所以命令先用 `bin/lib.sh` 的 `load_env` 读出端口和只读账号的密码，再查询。

**预计输出：** 每个实例一行，三列都是进程启动以来的条数：

- `forward_empty`、`sampling_empty`：留空次数，即 forward、sampling 没有设备时间、看板上这一步留空的记录条数（C15，过滤器自检行的 `acc_fallback`）。打上 vLLM、vllm-ascend 的新补丁以后应当接近 0；明显大于 0 时，把这一行发回。
- `no_execution_id`：步骤记录没有 `execution_id` 的条数，应当是 0；不是 0 时，vLLM 移植版的 `SchedulerOutput.pd_execution_id` 可能漏了，这些请求第一步的几段为空（《面板系统说明书》3.6.5）。

实验室的输出（这里的值来自附录 B 的换包走查，换包以后替身运行约 10 分钟；全新安装以后格式相同、数值不同；替身不写设备时间，前两列等于这个实例的全部 forward、sampling 记录数；`no_execution_id` 是 0）：

```
    ┌─instance─┬─forward_empty─┬─sampling_empty─┬─no_execution_id─┐
 1. │ D0       │          1108 │           1107 │               0 │
 2. │ D1       │          1108 │           1108 │               0 │
 3. │ D2       │          1108 │           1108 │               0 │
…（每个实例一行）
    └──────────┴───────────────┴────────────────┴─────────────────┘
```

**10.1–10.5 执行后马上会看到：** 只多了 `state-after`。**对同机其他服务的影响：** 只读。

## 附录 A：删掉 test-glm53（需要时才做）

**机器：** 监控机，然后 glm5.3 的机器。

1. **卸面板二**（删容器、数据源、看板、数据和集群目录，注销注册表里的条目）：

   ```bash
   . ~/vllm-pd-test-glm53/vars.sh && cd "$D" && bin/panel2_cluster.sh remove "$C" 2>&1 | tee "$SV/remove.log"
   ```

   **预计输出**（实验室，摘出结论）：

   ```
   OK    Grafana 看板：已删除
   OK    Grafana 数据源：已删除
   OK    容器 vllm-pd-vector-test-glm53 已删除
   OK    容器 vllm-pd-clickhouse-test-glm53 已删除
   …
   OK    数据卷 vllm-pd-clickhouse-test-glm53-data 已删除
   OK    拉取镜像 <总目录>/vllm-timing/test-glm53/pull 已删除
   OK    <机器>：<节点目录>/logs 里过滤器写的旧文件（删了 0 个；留下正在写的 <n> 个：vLLM 还开着它们，摘掉过滤器、重启 vLLM 以后再手删）已删除
   …
   已删除目录 <总目录>/vllm-pd-grafana-panel2/test-glm53
   RESULT: OK（集群 test-glm53 的面板二已卸载）
   ```

2. **删它自己的 Prometheus**（`remove` 不管第 5 步手工起的这个）：

   ```bash
   . ~/vllm-pd-test-glm53/vars.sh && docker rm -f vllm-pd-prometheus-$C && docker volume rm vllm-pd-prometheus-data-$C && rm -rf "$PDT" && echo 已删除
   ```

   **预计输出：** 两行容器名、卷名，最后一行 `已删除`（实验室）。

3. **删空的看板文件夹：** 整理看板命令会删掉已经注销的集群留下的空子文件夹：

   ```bash
   . ~/vllm-pd-test-glm53/vars.sh && python3 "$T45/arrange_boards.py" apply --env-from "$ENV2" 2>&1 | tail -n 4
   ```

   **预计输出**（实验室）：

   ```
   已删 空文件夹 GLM-5.3/test-glm53（uid <…>）
   == 核对
   OK   再看一遍：没有要改的
   RESULT: OK（改了 1 项；回退：…）
   ```

   以后不再加回来的，再把对照表和清单文件恢复成第 4、8 步之前的样子：`cp -p "$SV/boards_layout.before" "$T45/boards_layout.txt" && cp -p "$SV/clusters.txt.before" "$T45/clusters.txt"`。

4. **glm5.3 的机器上：** 去掉第 7 步加的两行 `export`（挂载可以留着），按平时的方式重启 vLLM；`<节点目录>` 里的文件这时可以手删（`remove` 的输出里写着还开着的文件数）。

**执行后马上会看到：** `bin/panel2_cluster.sh list` 里没有 test-glm53；Grafana 里没有它的数据源、看板和文件夹；端口 `B`–`B+2` 空出来。再执行一次 10.3 的命令，这时 `diff` 只有一行 `diff 结束`：test-glm53 的容器也没有了，已有集群和第 1 步记下的完全相同（实验室）。

## 附录 B：已经用上一版装好的 test-glm53 换成这一版（换包）

**什么时候做：** test-glm53 已经按上一版说明、用上一版面板包（sha256 `52b8824b…`）装好。不用删掉重装，也不用重做第 1–9 步，按 B.1–B.6 做。只动 test-glm53，glm53-1、test 不动。

**换了以后有什么不同：**

- 段定义换成这一版的 `profiles/glm53/segments.json`：forward、sampling 只取设备时间，没有设备时间的那一步留空，不再用 host 侧耗时兜底（C15）；过滤器自检行的 `acc_fallback` 记的是留空次数。各段的口径见《面板系统说明书》3.6.5。
- `config_id` 从 `pd-diag-v3.1-extra-glm53-2026-11-19b` 换成 `pd-diag-v3.1-extra-glm53-2026-11-19c`，两版口径的数据不混在同一个看板里。
- 监控机上的拉取程序 `scripts/native_put.py` 认得 glog 0.6 带年份的 Mooncake 计时行（《面板系统说明书》已知问题第 36 条）。实验室在换包以后另做了落地核对：集群目录里的这个文件和新包里的相同；注入一个 P 请求的保存任务和两行计时行（一行带年份、一行不带），拉取程序两行都解析、都配上，单请求 JSON 里 put 的 submit、wait_for_transfer、finalize 和注入的值相同。
- 更新脚本先删掉 test-glm53 在 ClickHouse 里的请求数据，看板从空开始，只显示换包以后的请求。
- 节点上的过滤器程序换了（`pd_diag_logfilter.py`），glm5.3 的 vLLM 重启以后才用上（B.5）。
- `deploy.env` 里已有的设置（两个采集开关、`MOONCAKE_LOG_FILES` 等）不变。

### B.1 放新包和更新脚本，核对 sha256

**机器：** 监控机。

**命令：** `vllm-pd-grafana-panel2.tar.gz` 和 `.sha256` 放进第一个目录，`<更新脚本>` 放进第二个，再核对。目录已经有的，里面的同名文件换成这一版的。

```bash
mkdir -p <用户目录>/vllm-pd-panel2-update-v3.1-extra <用户目录>/vllm-pd-update-v3.1-extra
```

```bash
cd <用户目录>/vllm-pd-panel2-update-v3.1-extra && sha256sum -c vllm-pd-grafana-panel2.tar.gz.sha256
```

**预计输出：** `vllm-pd-grafana-panel2.tar.gz: OK`（实验室）。

**执行后马上会看到：** 只多了（或换了）这三个文件，别的都没动。

**对同机其他服务的影响：** 没有。

### B.2 核对（只读）

**机器：** 监控机。

```bash
cd <用户目录>/vllm-pd-update-v3.1-extra && python3 <更新脚本> check --cluster test-glm53 --profile glm53
```

**预计输出**（实验室，摘要一段）：

```
vllm_pd_update_pack2_v31extra.py check，面板二 v3.1-extra，集群 test-glm53，成功
check：通过（面板二 v3.1-extra，vllm 模式；WARN 2 项）
迁移预览：RESULT: WARN（1 项）
WARN 要重启 vLLM：过滤器文件变了（pd_diag_logfilter.py），升级后各节点的 vLLM 重启了才用上新的
INFO 段定义换成新包的 profiles/glm53/segments.json（config_id pd-diag-v3.1-extra-glm53-2026-11-19b → pd-diag-v3.1-extra-glm53-2026-11-19c）
INFO segments.json 会更新（pd-diag-v3.1-extra-glm53-2026-11-19b@<8 位> → pd-diag-v3.1-extra-glm53-2026-11-19c@<8 位>），过滤器自己重读；config_id 变了：…
升级安全检查（升级前）：RESULT: WARN（1 项）
WARN 版本和文件：1 个文件和 SHA256SUMS 不同，升级会换成包里的：segments.json
INFO 节点：<机器> 找到 vLLM 容器 <容器名>（容器里有 vllm 进程）；不对时填 deploy.env 的 NODE_VLLM_CONTAINERS
INFO 最近 15 分钟 <n> 个请求
升级前比对：RESULT: 不变 2 项，有变化 3 项，比不了 0 项
…
```

两个 WARN 都是预期的：一个是 B.5 要重启 vLLM；另一个是集群目录里的 `segments.json` 是 glm53 的段定义，和包根目录那份不同，`run` 会换成这一版的 glm53 段定义。

**对同机其他服务的影响：** 只读。

### B.3 换包

**机器：** 监控机。

```bash
cd <用户目录>/vllm-pd-update-v3.1-extra && python3 <更新脚本> run --cluster test-glm53 --profile glm53
```

**预计输出**（实验室，摘要一段）：

```
vllm_pd_update_pack2_v31extra.py run，面板二 v3.1-extra，集群 test-glm53，成功
预检：通过（更新前面板二是 v3.1-extra，vllm 模式；迁移预览 WARN 1 项见上面和日志）
WARN 要重启 vLLM：过滤器文件变了（pd_diag_logfilter.py），升级后各节点的 vLLM 重启了才用上新的
备份：<家目录>/vllm-pd-update-backup/<时间>
迁移：deploy.env 里没有要写的
删数据：已删除 vllm_pd 的 4 张表、3 个视图，共 <行数> 行（…）；保留 filter_file、filter_other、filter_selfcheck
已更新：表和视图：…
已更新：采集器：…
跳过（内容不变）：数据源
已更新：看板：…
跳过（内容不变）：Prometheus（不写文件、不重新加载）
节点文件：segments.json 会更新（pd-diag-v3.1-extra-glm53-2026-11-19b@<8 位> → pd-diag-v3.1-extra-glm53-2026-11-19c@<8 位>），过滤器自己重读；config_id 变了：…
看板：config_id pd-diag-v3.1-extra-glm53-2026-11-19b → pd-diag-v3.1-extra-glm53-2026-11-19c，从空库开始，只显示升级以后的请求
段定义：profiles/glm53/segments.json；proxy 采集 开，Mooncake 计时采集 开（打开见操作说明 B 第 4 步）
不变的项：全部通过（9 项）
…
回到这次 run 之前（v3.1-extra）：python3 <更新脚本的路径> rollback <家目录>/vllm-pd-update-backup/<时间> --cluster test-glm53
```

把「备份」一行的目录记下来，B.7 换回上一版时用。

**执行后马上会看到：** 看板空了，之后只有新请求。vLLM 还没重启，节点上还是上一版的过滤器程序。实验室换包后约 2 分钟、重启以前，47 段里 31 段有值，为空的 14 段是：

- P 的 schedule/queue_time、schedule/hash、wait_for_put、forward、sampling，P 后台 wait_for_save/put 下的 3 段；
- D 的 schedule/queue_time、start_load_kv/wait_for_transfer、forward、sampling，D 第二步起的 forward、sampling。

重启以后（B.5）恢复到 B.6 的样子。

**对同机其他服务的影响：** 只动 test-glm53：它的采集器重启一次，ClickHouse 不重启；别的集群不动。

### B.4 看板放回 `GLM-5.3/test-glm53`

**机器：** 监控机。`run` 把看板重新导入到模型文件夹 `test-glm53` 下，用整理看板命令放回去：

```bash
. ~/vllm-pd-test-glm53/vars.sh && python3 "$T45/arrange_boards.py" plan --env-from "$ENV2"
```

```bash
. ~/vllm-pd-test-glm53/vars.sh && python3 "$T45/arrange_boards.py" apply --env-from "$ENV2" 2>&1 | tail -n 6
```

**预计输出**（实验室）：`plan` 列出 2 项，`apply` 改 2 项：

```
挪 看板 vllm-pd-request-test-glm53「面板二：请求时延拆解（P / D）（test-glm53）」：test-glm53 → GLM-5.3/test-glm53
删 空文件夹 test-glm53（uid <…>）
RESULT: 要改 2 项（plan 只看不改；执行 apply 才改）
…
OK   再看一遍：没有要改的
RESULT: OK（改了 2 项；回退：…）
```

**对同机其他服务的影响：** 只挪 test-glm53 的看板。

### B.5 重启 glm5.3 的 vLLM

**机器：** glm5.3 的 P、D 机器。节点上的过滤器程序换了，vLLM 重启以后才用上；按平时的方式重启 vLLM，只重启一次。还没按操作说明 A 打补丁的，和打补丁放在同一次重启里。挂载和环境变量是第 7 步加的，不用再改。

**执行后马上会看到：** 重启后几分钟，B.3 里为空的段恢复有值。只取设备时间的 6 段（P、D 的 forward、sampling，D 第二步起的 forward、sampling）要 vLLM、vllm-ascend 的补丁写出设备时间才有值。

### B.6 测试

**机器：** 监控机。重启以后等至少 3 分钟，照第 10 步做 10.1–10.5。

**预计输出**（实验室，替身没有本套补丁）：10.1 `RESULT: OK`，视图和 30 个过滤器进程都是 `pd-diag-v3.1-extra-glm53-2026-11-19c`；10.2 和第 10 步相同（47 段里 35 段有值）；10.3 只多 test-glm53 自己的 3 个容器；10.5 每个实例的 `forward_empty`、`sampling_empty` 是这个实例的全部 forward、sampling 记录数（替身不写设备时间，每一步都留空），`no_execution_id` 是 0。打上新补丁的现场，前两列应当接近 0；留空比例按操作说明 A 第 7.3 节判。

### B.7 换回上一版（需要时才做）

**机器：** 监控机，然后 glm5.3 的机器。

**命令：** 用 B.3 记下的备份目录：

```bash
cd <用户目录>/vllm-pd-update-v3.1-extra && python3 <更新脚本> rollback <B.3 的备份目录> --cluster test-glm53
```

**预计输出**（实验室，摘要一段）：

```
vllm_pd_update_pack2_v31extra.py rollback <备份目录>，面板二 v3.1-extra，集群 test-glm53，成功
恢复 <备份目录>：和更新前一致，校验全部通过
OK   拉取程序先停下：已停止
INFO 镜像目录里 v3.1-extra 多出来的 目录已删除：_proxy
INFO 删数据：已删除 vllm_pd 的 4 张表、3 个视图，共 <行数> 行（…）；保留 filter_file、filter_other、filter_selfcheck
OK   节点文件：更新前的三个文件已下发（apply_segments.sh 里的 bin/nodes.sh push），segments.json 是 pd-diag-v3.1-extra-glm53-2026-11-19b@<8 位>
INFO 看板读回 v3.1-extra 的 config_id pd-diag-v3.1-extra-glm53-2026-11-19b，从空库开始：回滚以后的请求才显示
OK   拉取程序用回 v3.1-extra 启动：已启动
WARN 过滤器文件回滚前后不同（pd_diag_logfilter.py）：各节点的 vLLM 要重启才用回旧的
```

`_proxy` 镜像目录删掉以后，拉取程序重启时重新拉，proxy 段照常有值。然后：

1. 照 B.4 再执行一次整理看板命令的 `apply`（实验室：`已删 空文件夹 test-glm53`，`RESULT: OK（改了 1 项；…）`）。
2. glm5.3 的 vLLM 重启一次，节点上用回上一版的过滤器程序。vLLM、vllm-ascend 的补丁要不要回滚，按操作说明 A 第 9 步另行决定，和面板二的回滚无关。

**执行后马上会看到：** 看板空了，之后只有回滚以后的请求。实验室里回滚约 1 分钟后、重启以前，47 段里 39 段有值，为空的是 P 的 schedule/lookup、wait_for_put，P 后台 wait_for_save/put 下的 3 段，D 的 start_load_kv/wait_for_transfer；重启以后 41 段有值，和换包前相同。

**回滚以后的检查**（实验室，重启以后）：10.1 `RESULT: OK`，视图和 30 个过滤器进程都是 `pd-diag-v3.1-extra-glm53-2026-11-19b`；10.2 是 47 段里 41 段有值；集群目录的文件和换包前逐个相同，只有看板的版本号变大（实验室 2 → 7，中间多做了一次改节点日志保留时长）。

**对同机其他服务的影响：** 和 B.3 相同，只动 test-glm53。

## 附录 B0：用更早的 76dec56e 装好的 test-glm53（【待定】）

**【待定】这一节是上一版说明的原文：它把 `76dec56e…` 换成 `52b8824b…`，不是换成这一版。** 内网是否还有用 `76dec56e…` 装的 test-glm53，等确认：有的话，再走一遍从 `76dec56e…` 换到这一版并改写这一节；没有的话，删掉这一节。在那以前不要照这一节做。

**什么时候做：** test-glm53 已经按上一版说明、用上一版面板包（sha256 `76dec56e…`）装好。不用删掉重装，也不用重做第 1–9 步，按 B0.1–B0.6 做。只动 test-glm53，glm53-1、test 不动。

**换了以后有什么不同：**

- 段定义换成这一版的 `profiles/glm53/segments.json`：P 后台 wait_for_save 加上排队时间（C12）；D 第一步的 alloc、other 不再含首次候选那一步（C2 细化）；forward、sampling 优先取设备计时（C14）；步骤记录缺 `execution_id` 时第一步留空。各段的口径见《面板系统说明书》3.6.5。
- `config_id` 从 `pd-diag-v3.1-extra-glm53-2026-11-19` 换成 `pd-diag-v3.1-extra-glm53-2026-11-19b`，两版口径的数据不混在同一个看板里。
- 更新脚本先删掉 test-glm53 在 ClickHouse 里的请求数据，看板从空开始，只显示换包以后的请求。
- 节点上的过滤器程序换了（`pd_diag_logfilter.py`），glm5.3 的 vLLM 重启以后才用上。**换包以后、vLLM 重启以前，新口径的几段对新请求为空，重启后恢复**（B0.3 后面列出实验室里为空的段）。forward、sampling 的设备计时还要 vLLM、vllm-ascend 的新补丁（操作说明 A），两件事一起做，只重启一次。
- `deploy.env` 里已有的设置（两个采集开关、`MOONCAKE_LOG_FILES` 等）不变。

### B0.1 放新包和更新脚本，核对 sha256

**机器：** 监控机。

**命令：** `vllm-pd-grafana-panel2.tar.gz` 和 `.sha256` 放进第一个目录，`<更新脚本>` 放进第二个，再核对。目录已经有的，里面的同名文件换成这一版的。

```bash
mkdir -p <用户目录>/vllm-pd-panel2-update-v3.1-extra <用户目录>/vllm-pd-update-v3.1-extra
```

```bash
cd <用户目录>/vllm-pd-panel2-update-v3.1-extra && sha256sum -c vllm-pd-grafana-panel2.tar.gz.sha256
```

**预计输出：** `vllm-pd-grafana-panel2.tar.gz: OK`（实验室）。

**执行后马上会看到：** 只多了（或换了）这三个文件，别的都没动。

**对同机其他服务的影响：** 没有。

### B0.2 核对（只读）

**机器：** 监控机。

```bash
cd <用户目录>/vllm-pd-update-v3.1-extra && python3 <更新脚本> check --cluster test-glm53 --profile glm53
```

**预计输出**（实验室，摘要一段）：

```
vllm_pd_update_pack2_v31extra.py check，面板二 v3.1-extra，集群 test-glm53，成功
check：通过（面板二 v3.1-extra，vllm 模式；WARN 2 项）
迁移预览：RESULT: WARN（1 项）
WARN 要重启 vLLM：过滤器文件变了（pd_diag_logfilter.py），升级后各节点的 vLLM 重启了才用上新的
INFO 段定义换成新包的 profiles/glm53/segments.json（config_id pd-diag-v3.1-extra-glm53-2026-11-19 → pd-diag-v3.1-extra-glm53-2026-11-19b）
INFO segments.json 会更新（pd-diag-v3.1-extra-glm53-2026-11-19@<8 位> → pd-diag-v3.1-extra-glm53-2026-11-19b@<8 位>），过滤器自己重读；config_id 变了：…
升级安全检查（升级前）：RESULT: WARN（1 项）
WARN 版本和文件：1 个文件和 SHA256SUMS 不同，升级会换成包里的：segments.json
INFO 节点：<机器> 找到 vLLM 容器 <容器名>（容器里有 vllm 进程）；不对时填 deploy.env 的 NODE_VLLM_CONTAINERS
INFO 最近 15 分钟 <n> 个请求
升级前比对：RESULT: 不变 2 项，有变化 3 项，比不了 0 项
…
```

两个 WARN 都是预期的：一个是 B0.5 要重启 vLLM；另一个是集群目录里的 `segments.json` 是 glm53 的段定义，和包根目录那份不同，`run` 会换成这一版的 glm53 段定义。

**对同机其他服务的影响：** 只读。

### B0.3 换包

**机器：** 监控机。

```bash
cd <用户目录>/vllm-pd-update-v3.1-extra && python3 <更新脚本> run --cluster test-glm53 --profile glm53
```

**预计输出**（实验室，摘要一段）：

```
vllm_pd_update_pack2_v31extra.py run，面板二 v3.1-extra，集群 test-glm53，成功
预检：通过（更新前面板二是 v3.1-extra，vllm 模式；迁移预览 WARN 1 项见上面和日志）
WARN 要重启 vLLM：过滤器文件变了（pd_diag_logfilter.py），升级后各节点的 vLLM 重启了才用上新的
备份：<家目录>/vllm-pd-update-backup/<时间>
迁移：deploy.env 里没有要写的
删数据：已删除 vllm_pd 的 4 张表、3 个视图，共 <行数> 行（…）；保留 filter_file、filter_other、filter_selfcheck
已更新：表和视图：…
已更新：采集器：…
跳过（内容不变）：数据源
已更新：看板：…
跳过（内容不变）：Prometheus（不写文件、不重新加载）
节点文件：segments.json 会更新（… → pd-diag-v3.1-extra-glm53-2026-11-19b@<8 位>），过滤器自己重读；config_id 变了：…
看板：config_id pd-diag-v3.1-extra-glm53-2026-11-19 → pd-diag-v3.1-extra-glm53-2026-11-19b，从空库开始，只显示升级以后的请求
段定义：profiles/glm53/segments.json；proxy 采集 开，Mooncake 计时采集 开（打开见操作说明 B 第 4 步）
不变的项：全部通过（9 项）
…
回到这次 run 之前（v3.1-extra）：python3 <更新脚本的路径> rollback <家目录>/vllm-pd-update-backup/<时间> --cluster test-glm53
```

把「备份」一行的目录记下来，B0.7 换回上一版时用。

**执行后马上会看到：** 看板空了，之后只有新请求。vLLM 还没重启，节点上还是上一版的过滤器程序，它读不了这一版的段定义，照旧按上一版写按步累加的行；这些行的 `config_id` 是上一版的，看板不读。所以重启以前，新请求的这些段为空（实验室，换包后约 2 分钟：47 段里 17 段有值）：

- P、D 的 schedule 和它下面的各段（queue_time、hash、lookup、alloc、other）、forward、sampling、P 的 wait_for_put；
- D 的 start_load_kv 和它的两个子段、D 的 detokenizer、D 第二步起的各段；
- P 后台 wait_for_save/put 下的 3 段。

API server 的几段（req_resolve、tokenizer、add_request）、lb_proxy、P 的 detokenizer、P 的 start_load_kv/submit、P 后台 wait_for_save 和它的 5 个子段照常有值。

**对同机其他服务的影响：** 只动 test-glm53：它的采集器重启一次，ClickHouse 不重启；别的集群不动。

### B0.4 看板放回 `GLM-5.3/test-glm53`

**机器：** 监控机。`run` 把看板重新导入到模型文件夹 `test-glm53` 下，用整理看板命令放回去：

```bash
. ~/vllm-pd-test-glm53/vars.sh && python3 "$T45/arrange_boards.py" plan --env-from "$ENV2"
```

```bash
. ~/vllm-pd-test-glm53/vars.sh && python3 "$T45/arrange_boards.py" apply --env-from "$ENV2" 2>&1 | tail -n 6
```

**预计输出**（实验室）：`plan` 列出 2 项，`apply` 改 2 项：

```
挪 看板 vllm-pd-request-test-glm53「面板二：请求时延拆解（P / D）（test-glm53）」：test-glm53 → GLM-5.3/test-glm53
删 空文件夹 test-glm53（uid <…>）
RESULT: 要改 2 项（plan 只看不改；执行 apply 才改）
…
OK   再看一遍：没有要改的
RESULT: OK（改了 2 项；回退：…）
```

**对同机其他服务的影响：** 只挪 test-glm53 的看板。

### B0.5 重启 glm5.3 的 vLLM

**机器：** glm5.3 的 P、D 机器。按操作说明 A 换上 vLLM、vllm-ascend 的新补丁，按平时的方式重启 vLLM，只重启一次。挂载和环境变量是第 7 步加的，不用再改。

**执行后马上会看到：** 重启后几分钟，B0.3 里为空的段恢复有值。

### B0.6 测试

**机器：** 监控机。重启以后等至少 3 分钟，照第 10 步做 10.1–10.5。

**预计输出**（实验室，替身没有本套补丁）：10.1 `RESULT: OK`，视图和 30 个过滤器进程都是 `pd-diag-v3.1-extra-glm53-2026-11-19b`；10.2 和第 10 步相同（47 段里 41 段有值）；10.3 只多 test-glm53 自己的 3 个容器；10.5：10.5 每个实例 `forward_host`、`sampling_host` 是这个实例的全部 forward、sampling 记录数（替身不写设备时间，全部回退到 host 侧耗时），`no_execution_id` 是 0。打上新补丁的现场，前两列应当接近 0。

### B0.7 换回上一版（需要时才做）

**机器：** 监控机，然后 glm5.3 的机器。

**命令：** 用 B0.3 记下的备份目录：

```bash
cd <用户目录>/vllm-pd-update-v3.1-extra && python3 <更新脚本> rollback <B0.3 的备份目录> --cluster test-glm53
```

**预计输出**（实验室，摘要一段）：

```
vllm_pd_update_pack2_v31extra.py rollback <备份目录>，面板二 v3.1-extra，集群 test-glm53，成功
恢复 <备份目录>：和更新前一致，校验全部通过
OK   拉取程序先停下：已停止
INFO 镜像目录里 v3.1-extra 多出来的 目录已删除：_proxy
INFO 删数据：已删除 vllm_pd 的 4 张表、3 个视图，共 <行数> 行（…）；保留 filter_file、filter_other、filter_selfcheck
OK   节点文件：更新前的三个文件已下发（apply_segments.sh 里的 bin/nodes.sh push），segments.json 是 pd-diag-v3.1-extra-glm53-2026-11-19@<8 位>
INFO 看板读回 v3.1-extra 的 config_id pd-diag-v3.1-extra-glm53-2026-11-19，从空库开始：回滚以后的请求才显示
OK   拉取程序用回 v3.1-extra 启动：已启动
WARN 过滤器文件回滚前后不同（pd_diag_logfilter.py）：各节点的 vLLM 要重启才用回旧的
```

`_proxy` 镜像目录删掉以后，拉取程序重启时重新拉，proxy 段照常有值。然后：

1. 照 B0.4 再执行一次整理看板命令的 `apply`（实验室：`已删 空文件夹 test-glm53`，`RESULT: OK（改了 1 项；…）`）。
2. glm5.3 的 vLLM 按操作说明 A 换回上一版补丁，重启一次。

**执行后马上会看到：** 看板空了，之后只有回滚以后的请求。和换包时不同，回滚以后不等重启就大多有值：节点上运行的是这一版的过滤器程序，它读得了上一版的段定义，下发以后马上按上一版写。实验室里回滚约 1 分钟后，47 段里 37 段有值（空的是 P、D 的 queue_time、P 的 hash、D 的 start_load_kv/wait_for_transfer，以及替身不写的 4 段）；重启以后 41 段有值，和换包前相同。

**回滚以后的检查**（实验室，重启以后）：10.1 `RESULT: OK`，视图和 30 个过滤器进程都是 `pd-diag-v3.1-extra-glm53-2026-11-19`；10.2 是 47 段里 41 段有值；集群目录的文件和换包前逐个相同，只有看板的版本号变大（实验室 2 → 6）。

**对同机其他服务的影响：** 和 B0.3 相同，只动 test-glm53。

## 附录 C：参考

- 《面板系统说明书》v3.1-extra 版：3.3（段定义按集群选）、3.6.4（dsv4.1 段定义的来源和口径，C1–C10）、3.6.5（glm5.3 和 dsv4.1 的差别）、13.2（`deploy.env` 的键）。
- 部署包 `README.md`：「按集群装」（`add` 的参数和各步骤）、「模型推理环境」（节点上的 V1–V6）。
- 操作说明 B 第 4 步：proxy、Mooncake 两个采集来源的写法。
- 看板分层的《动现有看板操作单》：`arrange_boards.py` 的几个子命令。
