# vLLM P/D 服务侧监控部署包（Prometheus + Grafana，两个看板）

装好后有两个看板：「面板一·P」和「面板一·D」。不需要 docker compose，不打 vllm-ascend 补丁也能装。

示例按这个集群写：P 是 8 台机器、8 个实例（`P0`–`P7`），每个实例 TP16、有自己的 API server；D 是 4 台机器、每台 16 个实例，共 64 个（`D0`–`D63`），每个实例 TP1、有自己的 API server。全集群一个 proxy，看板不用它，只用它配置里的 P、D 地址列表。

镜像版本：Prometheus v3.11.2，Grafana 12.4.2。

## 安装步骤

从头到尾照做。所有命令都在**监控机**（跑 Prometheus 和 Grafana 的那台）上执行，步骤 1–4 除外。监控机要能访问集群各节点的指标端口。

下面的命令按 aarch64 写。x86_64 机器把 `linux/arm64` 换成 `linux/amd64`。

### 步骤 1：在能上网的机器上拉 Prometheus 镜像

监控机自己能上网的话，直接在监控机上做步骤 1、2，然后跳到步骤 6。

```bash
docker pull --platform linux/arm64 prom/prometheus:v3.11.2
```

预期：最后一行是 `docker.io/prom/prometheus:v3.11.2`。

不符合时：连不上 Docker Hub 就换一台能上网的机器，或者配好镜像代理后再执行。

### 步骤 2：在能上网的机器上拉 Grafana 镜像

```bash
docker pull --platform linux/arm64 grafana/grafana:12.4.2
```

预期：最后一行是 `docker.io/grafana/grafana:12.4.2`。

不符合时：同步骤 1。

### 步骤 3：把两个镜像存成一个文件

```bash
docker save prom/prometheus:v3.11.2 grafana/grafana:12.4.2 -o vllm-pd-monitor-images.tar
```

预期：没有输出，当前目录多了 `vllm-pd-monitor-images.tar`，大小约 340 MB（本机 Docker 实测，其他 Docker 配置下可能更大）。

不符合时：报 `content digest ... not found` 时，在 `docker save` 后面加 `--platform linux/arm64`，再执行一次。

### 步骤 4：把镜像文件和部署包拷到监控机

把 `<用户>@<监控机>` 换成实际值。可以用 U 盘或别的方式，只要两个文件到了监控机的同一个目录。

```bash
scp vllm-pd-monitor-images.tar vllm-pd-grafana-panel1.tar.gz <用户>@<监控机>:~/
```

预期：两个文件都传完 100%。

不符合时：检查到监控机的网络和磁盘空间。

### 步骤 5：在监控机上导入镜像

下面所有步骤都在监控机上，在这两个文件所在的目录开始。

```bash
docker load -i vllm-pd-monitor-images.tar
```

预期：有两行 `Loaded image`，分别是 `prom/prometheus:v3.11.2` 和 `grafana/grafana:12.4.2`。

不符合时：报 `no space left on device` 就清理磁盘；报文件损坏就重新拷一次。

### 步骤 6：确认两个镜像都在

```bash
docker image ls --format '{{.Repository}}:{{.Tag}}' | grep -E '^(prom/prometheus:v3\.11\.2|grafana/grafana:12\.4\.2)$'
```

预期：正好两行。

不符合时：缺哪个就回到对应的拉取或导入步骤。

### 步骤 7：解开部署包

```bash
tar xzf vllm-pd-grafana-panel1.tar.gz
```

预期：没有输出，当前目录多了 `vllm-pd-grafana-panel1/`。

不符合时：文件损坏，重新拷一次。

### 步骤 8：进入目录

后面所有命令都在这个目录里执行。

```bash
cd vllm-pd-grafana-panel1
```

预期：没有输出。

### 步骤 9：校验文件

```bash
sha256sum -c SHA256SUMS --quiet
```

预期：没有输出。

不符合时：有 `FAILED` 就是文件被改过或没传完，重新解包。没有 `sha256sum` 命令时用 `shasum -a 256 -c SHA256SUMS --quiet`。

### 步骤 10：确认 Python 版本

```bash
python3 --version
```

预期：3.8 或更新。不用装第三方库。

不符合时：装一个 Python 3.8 以上的版本。

### 步骤 11：复制集群清单

```bash
cp prometheus/inventory.example.json prometheus/inventory.json
```

预期：没有输出。

### 步骤 12：改清单

用编辑器打开 `prometheus/inventory.json`，按下表改。**实例名按列表顺序算：第 1 个地址是 `P0` / `D0`，第 2 个是 `P1` / `D1`，以此类推，所以列表要按启动脚本里的 `--data-parallel-rank` 从小到大排。** 机器数和每台几个实例不用写，生成脚本从地址里算。

| 清单里的内容 | 示例值 | 换成 |
|---|---|---|
| `prefill.api_servers` | 8 个 `IP:PORT` | proxy 配置里 P 的列表，照抄 |
| `prefill.procs_per_instance` | `16` | P 每个实例的进程数，= TP × PP × prefill-CP（EP 和 DCP 不算） |
| `decode.api_servers` | 64 个 `IP:PORT` | proxy 配置里 D 的列表，照抄 |
| `decode.procs_per_instance` | `1` | D 每个实例的进程数，算法同上 |
| `mooncake_master.address` | `10.0.3.1:9003` | Mooncake master 的 IP 和 metrics 端口 |
| `npu_exporter_port`、`node_exporter_port` | `8082`、`9100` | 两个 exporter 的端口，默认值不用改 |
| `vllm_ascend_patched` | `false` | 没打补丁就保持 `false`。没打补丁时缺哪些 panel 见「不打补丁时缺什么」 |

一个 API server 带多个实例的集群：按附录 B 改完再继续。

预期：步骤 14 输出的实例名和你的集群一致。

不符合时：回到这一步。清单写错时，步骤 13 会报一行错，说明哪个字段不对。

### 步骤 13：生成 Prometheus 配置

```bash
python3 prometheus/gen_prometheus_yml.py prometheus/inventory.json > prometheus/prometheus.yml
```

预期：没有输出。

不符合时：看报错那一行，它会说明哪个字段不对，对照附录 A 改清单，再执行一次。

### 步骤 14：看实例名对不对

```bash
python3 prometheus/gen_prometheus_yml.py prometheus/inventory.json --instances | cut -d' ' -f1 | tr '\n' ' '
```

预期：一行，`P0` 到 `P7`，然后是 `D0` 到 `D63`。这些名字就是看板「实例」下拉框里的内容。

不符合时：实例数不对，检查两个 `api_servers` 列表有没有抄全。

### 步骤 15：看抓取目标数

```bash
python3 prometheus/gen_prometheus_yml.py prometheus/inventory.json --list | wc -l
```

预期：`vllm_ascend_patched` 是 `false` 时 `97`：72 个 API server、1 个 master、12 个 npu-exporter、12 个 node_exporter。

不符合时：你的集群和示例规模不同时，目标数 = API server 个数 + 1 + 2 × 机器数，对得上就没问题。否则回步骤 12 检查。

### 步骤 16：检查配置文件语法

```bash
docker run --rm -v "$PWD/prometheus:/p:ro" --entrypoint promtool prom/prometheus:v3.11.2 check config /p/prometheus.yml
```

预期：有一行 `SUCCESS: /p/prometheus.yml is valid prometheus config file syntax`。

不符合时：报 `permission denied`，见「排查」。其他报错说明配置不是步骤 13 生成的，重新做步骤 13。

### 步骤 17：确认每个 API server 都能访问，而且列表顺序对

这条命令访问清单里每个 API server，看它报的 `engine` 值是不是它的实例序号。

```bash
python3 prometheus/gen_prometheus_yml.py prometheus/inventory.json --api-servers | while read name addr; do echo "$name $addr $(curl -s --max-time 5 http://$addr/metrics | grep -o 'engine="[0-9]*"' | sort -u | tr '\n' ' ')"; done | awk '{n=$1; gsub(/[^0-9]/,"",n); e=$3; gsub(/[^0-9]/,"",e); if (NF!=3 || n!=e) {print "不一致:", $0; bad++}} END {print NR-bad "/" NR " 个 API server 顺序正确"}'
```

预期：只有一行 `72/72 个 API server 顺序正确`。

不符合时：

- `不一致: P3 10.0.1.4:9000` 后面没有 `engine`：这个地址访问不到。确认实例已经启动，防火墙放行了监控机。
- `不一致: P3 10.0.1.4:9000 engine="5"`：这个地址应该排在第 6 个（序号 5）。调整清单列表的顺序，回到步骤 13。
- 所有地址都报 `engine="0"`：这条校验不起作用，手动确认列表是按 `--data-parallel-rank` 排的，然后继续。

### 步骤 18：确认能访问 Mooncake master

```bash
curl -s --max-time 5 http://10.0.3.1:9003/metrics | grep -c '^master_'
```

预期：大于 0 的数。

不符合时：端口是 master 配置里的 `metrics_port`，默认 9003。

### 步骤 19：确认能访问 npu-exporter

每台机器上都要有 npu-exporter（默认端口 8082）。这里抽查一台，步骤 26 会查全部。

```bash
curl -s --max-time 5 http://10.0.1.1:8082/metrics | grep -c '^npu_chip_info'
```

预期：大于 0 的数。

不符合时：这台机器没装 npu-exporter 或端口不同。没装也能继续，「NPU 与主机」分组和总览里 NPU 的 panel 会没有数据，步骤 26 里这些目标显示 down。

### 步骤 20：确认能访问 node_exporter

每台机器上都要有 node_exporter（默认端口 9100）。

```bash
curl -s --max-time 5 http://10.0.1.1:9100/metrics | grep -c '^node_cpu'
```

预期：大于 0 的数。

不符合时：同步骤 19。

### 步骤 21：建容器网络

```bash
docker network create vllm-pd-monitor
```

预期：输出一串 ID。

不符合时：报 `already exists` 说明之前建过，继续下一步。

### 步骤 22：建 Prometheus 数据卷

```bash
docker volume create vllm-pd-prometheus-data
```

预期：输出 `vllm-pd-prometheus-data`。

### 步骤 23：建 Grafana 数据卷

```bash
docker volume create vllm-pd-grafana-data
```

预期：输出 `vllm-pd-grafana-data`。

### 步骤 24：启动 Prometheus

```bash
docker run -d --name vllm-pd-prometheus --network vllm-pd-monitor --restart unless-stopped -p 9090:9090 -v "$PWD/prometheus/prometheus.yml:/etc/prometheus/prometheus.yml:ro" -v vllm-pd-prometheus-data:/prometheus prom/prometheus:v3.11.2 --config.file=/etc/prometheus/prometheus.yml --storage.tsdb.path=/prometheus --web.enable-lifecycle
```

预期：输出一串容器 ID。

不符合时：端口被占时把 `-p 9090:9090` 左边的数改掉，后面命令里的 `localhost:9090` 也跟着改。报名字已被占用：见「停止、重启和卸载」里的卸载命令，先删旧容器。

### 步骤 25：确认 Prometheus 起来了

```bash
sleep 10 && curl -s http://localhost:9090/-/ready
```

预期：`Prometheus Server is Ready.`

不符合时：执行 `docker logs vllm-pd-prometheus` 看最后几行。常见的是 `permission denied`，见「排查」。

### 步骤 26：确认所有目标都抓到了

```bash
sleep 30 && curl -s 'http://localhost:9090/api/v1/targets?state=active' | grep -o '"health":"[a-z]*"' | sort | uniq -c
```

预期：只有一行 `97 "health":"up"`（目标数和步骤 15 相同）。

不符合时：有 `down` 时，打开 `http://<监控机>:9090/targets` 看哪个目标、什么错误。`connection refused` 是对方端口没开，`context deadline exceeded` 是网络不通或防火墙。

### 步骤 27：启动 Grafana

先输入你定的管理员密码（不显示，不进 shell 历史；后面的步骤也从 `GRAFANA_PASSWORD` 取，换了终端就重新输入一次），再启动：

```bash
read -rs -p 'Grafana 管理员密码：' GRAFANA_PASSWORD; echo; export GRAFANA_PASSWORD GRAFANA_USER=admin
GF_SECURITY_ADMIN_PASSWORD="$GRAFANA_PASSWORD" docker run -d --name vllm-pd-grafana --network vllm-pd-monitor --restart unless-stopped -p 3000:3000 -e GF_SECURITY_ADMIN_USER=admin -e GF_SECURITY_ADMIN_PASSWORD -e GF_USERS_ALLOW_SIGN_UP=false -e GF_ANALYTICS_REPORTING_ENABLED=false -e GF_ANALYTICS_CHECK_FOR_UPDATES=false -e GF_ANALYTICS_CHECK_FOR_PLUGIN_UPDATES=false -e GF_NEWS_NEWS_FEED_ENABLED=false -e GF_PLUGINS_PREINSTALL_DISABLED=true -e PROMETHEUS_URL=http://vllm-pd-prometheus:9090 -e PROMETHEUS_SCRAPE_INTERVAL=15s -v "$PWD/grafana/provisioning:/etc/grafana/provisioning:ro" -v "$PWD/grafana/dashboards:/etc/grafana/dashboards:ro" -v vllm-pd-grafana-data:/var/lib/grafana grafana/grafana:12.4.2
```

预期：输出一串容器 ID。

不符合时：端口被占时把 `-p 3000:3000` 左边的数改掉，后面命令和浏览器地址里的 `3000` 也跟着改。清单里改过 `scrape_interval` 时，`PROMETHEUS_SCRAPE_INTERVAL` 要写成一样的值。

### 步骤 28：确认 Grafana 起来了

第一次启动要建数据库，等久一点。

```bash
sleep 45 && curl -s http://localhost:3000/api/health
```

预期：输出里有 `"database": "ok"` 和 `"version": "12.4.2"`。

不符合时：再等一分钟重试。还不行就执行 `docker logs vllm-pd-grafana`，看最后的 `level=error` 行。

### 步骤 29：确认两个看板加载了

密码取步骤 27 输入的 `GRAFANA_PASSWORD`。

```bash
curl -s -u "admin:$GRAFANA_PASSWORD" 'http://localhost:3000/api/search?type=dash-db' | grep -o '"uid":"vllm-pd-[a-z]*"'
```

预期：两行，`"uid":"vllm-pd-prefill"` 和 `"uid":"vllm-pd-decode"`。

不符合时：等 30 秒重试。还没有就执行 `docker logs vllm-pd-grafana 2>&1 | grep provisioning`，常见原因是 `grafana/` 目录没挂进去：确认步骤 27 是在 `vllm-pd-grafana-panel1/` 目录里执行的。

### 步骤 30：检查每个 panel 的数据

先等 3 分钟攒数据。**集群上要有请求在跑**，否则时延、吞吐类的 panel 没有数据。密码取步骤 27 输入的 `GRAFANA_PASSWORD`。

```bash
sleep 180 && GRAFANA_USER=admin python3 scripts/verify_panel1.py --prometheus http://localhost:9090 --inventory prometheus/inventory.json --grafana http://localhost:3000 --minutes 30 | tail -n 1
```

预期：`RESULT: OK`。

不符合时：去掉 `| tail -n 1` 再跑一次，看完整输出里哪些 panel 是 `NO DATA`。下面这些没开对应功能时为空，属于正常：

- P 的「AscendStore 加载耗时 p50 / p99」「AscendStore 加载 keys/s」「延迟释放」两张：要用 AscendStoreConnector。
- D 的「MTP 接受率」：要开 MTP。
- 「NPU 与主机」分组：要有 npu-exporter 和 node_exporter。

其他 panel 为空，看「排查」。

### 步骤 31：打开看板

浏览器打开下面两个地址（`<监控机>` 换成监控机地址），用 `admin` 和步骤 27 的密码登录：

- 面板一·P：`http://<监控机>:3000/d/vllm-pd-prefill`
- 面板一·D：`http://<监控机>:3000/d/vllm-pd-decode`

也可以从左侧菜单 Dashboards 进文件夹「vLLM P/D」。

预期：

- 顶部只有「模型」「实例」两个下拉框和时间范围。
- P 的实例下拉框是 `P0` 到 `P7`，D 的是 `D0` 到 `D63`。
- 选一个实例，时延和调度分组的图只剩这个实例一条线；选 All 回到全部。D 选 All 时每张图 64 条线，图例在图下方，可以滚动。
- 曲线有数据。没打补丁时，部分 panel 显示 No data，名单见下一节。

不符合时：看「排查」。

安装到这里结束。

---

## 不打补丁时缺什么

`vllm_ascend_patched` 是 `false` 时，部署包不抓 Mooncake client 和 Transfer Engine 的每进程端口，所以 `/targets` 上没有一片 down 的目标，但读这些指标的 panel 显示 No data。其余 panel 不受影响。

下面的名单用不提供这些端点的模拟程序实测得到，编号是 `PANELS.md` 的编号。

**面板一·P：91 个 panel，14 个没有数据，77 个不受影响。**

| 编号 | 分组 | panel |
|---|---|---|
| 16 | 总览 | KV 传输耗时 p99 |
| 56 | KV Cache 与 Mooncake Client | KV 池写带宽 |
| 57 | KV Cache 与 Mooncake Client | KV 池读带宽 |
| 58 | KV Cache 与 Mooncake Client | SSD 层读写带宽 |
| 59 | KV Cache 与 Mooncake Client | KV 池写入量 |
| 60 | KV Cache 与 Mooncake Client | KV 池读出量 |
| 61 | KV Cache 与 Mooncake Client | SSD 层读写量 |
| 62 | KV Cache 与 Mooncake Client | KV 池写时延 p99（batch_put） |
| 63 | KV Cache 与 Mooncake Client | KV 池读时延 p99（batch_get） |
| 64 | KV Cache 与 Mooncake Client | SSD 层读写时延 p99 |
| 65 | KV Cache 与 Mooncake Client | Client→Master RPC 时延 p99 |
| 66 | KV Cache 与 Mooncake Client | Client RPC QPS |
| 67 | KV Cache 与 Mooncake Client | Client 操作速率（读 / 写） |
| 78 | Mooncake Master | Master 心跳 |

**面板一·D：89 个 panel，16 个没有数据，73 个不受影响。**

| 编号 | 分组 | panel |
|---|---|---|
| 16 | 总览 | KV 传输耗时 p99 |
| 62 | P→D KV 传输与 Mooncake Client | TE 读吞吐 |
| 63 | P→D KV 传输与 Mooncake Client | TE 写吞吐 |
| 64 | P→D KV 传输与 Mooncake Client | 传输失败 |
| 65 | P→D KV 传输与 Mooncake Client | TE 读时延 p99 |
| 66 | P→D KV 传输与 Mooncake Client | TE 写时延 p99 |
| 67 | P→D KV 传输与 Mooncake Client | 传输超时 |
| 68 | P→D KV 传输与 Mooncake Client | 单次传输耗时 |
| 69 | P→D KV 传输与 Mooncake Client | 单次传输大小分布 |
| 70 | P→D KV 传输与 Mooncake Client | TE 传输次数 |
| 71 | P→D KV 传输与 Mooncake Client | Client 读吞吐 |
| 72 | P→D KV 传输与 Mooncake Client | Client 写吞吐 |
| 73 | P→D KV 传输与 Mooncake Client | Client RPC QPS |
| 74 | P→D KV 传输与 Mooncake Client | batch_get 时延 p99 |
| 75 | P→D KV 传输与 Mooncake Client | batch_put 时延 p99 |
| 76 | P→D KV 传输与 Mooncake Client | Client→Master RPC 时延 p99 |

**只开 `MC_TE_METRIC=1` 不打补丁，行不行？** 不推荐。Transfer Engine 的指标端口只从环境变量 `MC_TE_METRIC_HTTP_PORT` 读（Mooncake `metrics_exporter.cpp:57-62`）。同一台机器上的 worker 进程继承同一份环境，只有第一个进程能绑定成功，其余进程绑定失败、只打一行 ERROR（`metrics_exporter.cpp:155-166`），它们的流量不会被导出，所以每台机器只能看到一个进程的数据。这一段按源码推断，没有实测。嵌入式 store client 的指标端口在 vllm-ascend 里根本没有打开，不打补丁就没有。

---

## 打补丁（可选）：让上面这些 panel 也有数据

补丁只改 vllm-ascend 的 3 个文件，要重启 vLLM 实例。针对 `releases/v0.32.0rc`（`3b1da3fe`）。步骤 B1–B6 在每台 P、D 机器上做；B7 起回到监控机。

### 步骤 B1：把补丁拷到机器上，并找到 vllm-ascend 的安装位置（每台 P、D 机器）

补丁文件是部署包里的 `patches/vllm-ascend-3b1da3fe-mooncake-metrics-ports.patch`。在装了 vllm-ascend 的 Python 环境里执行：

```bash
python -c "import vllm_ascend, os; print(os.path.dirname(os.path.dirname(vllm_ascend.__file__)))"
```

预期：输出一个目录，例如 `.../site-packages`。下面的 `<目录>` 就是它。

不符合时：`No module named vllm_ascend` 说明不是这个 Python 环境。

### 步骤 B2：试打补丁（每台 P、D 机器）

```bash
patch -p1 --dry-run -d <目录> < vllm-ascend-3b1da3fe-mooncake-metrics-ports.patch
```

预期：3 行 `checking file ...`（或 `patching file ...`），没有 `FAILED`。

不符合时：有 `FAILED` 说明 vllm-ascend 不是 `3b1da3fe` 这个版本。不要强打，保持不打补丁。

### 步骤 B3：打补丁（每台 P、D 机器）

```bash
patch -p1 -d <目录> < vllm-ascend-3b1da3fe-mooncake-metrics-ports.patch
```

预期：3 行 `patching file ...`。回滚时加 `-R` 再执行一次。

### 步骤 B4：判断 Mooncake 是共享库还是静态链接（每台 P、D 机器）

```bash
ldd "$(python -c 'import importlib.util as u; print(u.find_spec("mooncake._store").origin)')" | grep transfer_engine
```

预期：有输出是共享库，这是普通情况，继续。

不符合时：没有输出是静态链接。这时如果 KV 池用了 SSD 预取、fabric memory 或 `ASCEND_GLOBAL_RESOURCE_CONFIG`，还要多设一个基准端口并在清单写 `store_owns_te`，见附录 E。其他情况不用管。

### 步骤 B5：在启动脚本里加环境变量（每台 P、D 机器）

加到 vLLM 实例的启动环境。`MC_TE_METRIC_HTTP_PORT` 不要手动设。

```bash
export MC_TE_METRIC=1 VLLM_ASCEND_MC_TE_METRIC_HTTP_PORT_BASE=20000 VLLM_ASCEND_MOONCAKE_CLIENT_HTTP_PORT_BASE=22000
```

预期：没有输出。两个基准值要和清单 `metrics_port_bases` 的 `transfer_engine`、`mooncake_client` 相同。端口要求见附录 D。

### 步骤 B6：重启 vLLM 实例

按你们平时的方式重启 P、D。

预期：每个进程的日志里有一行 `Transfer Engine metrics started (http=0.0.0.0:<端口>)`，用了 KV 池的进程还有一行 `Mooncake store client metrics on port <端口>`，端口各不相同。

不符合时：日志和对应的处理见「排查」。

### 步骤 B7：确认 TE 端口有数（在监控机上）

`P0` 的 rank 0 进程在第一台 P 机器上，端口是 20000 + 0。第 n 个实例的进程全局 rank 从 n × `procs_per_instance` 开始，例如 `P1` 在第二台机器的 20016–20031，`D63` 在第四台 D 机器的 20063。

```bash
curl -s --max-time 5 http://10.0.1.1:20000/metrics | grep -c '^mooncake_te_'
```

预期：大于 0 的数。

不符合时：端口不通或没有数据，回到 B5、B6 检查。

### 步骤 B8：确认 client 端口有数（在监控机上）

```bash
curl -s --max-time 5 http://10.0.1.1:22000/metrics | grep -c '^mooncake_transfer_'
```

预期：大于 0 的数。

不符合时：P 没用 Mooncake KV 池时这个端口不存在：清单的 `prefill` 里加 `"mooncake_client_port_base": null`，并删掉 `scheduler_store_clients`。D 没用 KV 池时，在 `decode` 里加 `"mooncake_client_port_base": null`，步骤 B11 的目标数相应减少。

### 步骤 B9：改开关（在监控机上）

把 `prometheus/inventory.json` 里的 `vllm_ascend_patched` 改成 `true`。

预期：下面步骤 B11 的目标数变大。

### 步骤 B10：重新生成配置（在监控机上）

```bash
python3 prometheus/gen_prometheus_yml.py prometheus/inventory.json > prometheus/prometheus.yml
```

预期：没有输出。

不符合时：看报错那一行，对照附录 A。

### 步骤 B11：看新的目标数（在监控机上）

```bash
python3 prometheus/gen_prometheus_yml.py prometheus/inventory.json --list | wc -l
```

预期：`497`。示例清单的算法：97 个基础目标 + 200 个 TE（P 128 个 worker + P 8 个调度侧 + D 64 个 worker）+ 200 个 client。

### 步骤 B12：让 Prometheus 重新加载（在监控机上）

```bash
curl -s -X POST http://localhost:9090/-/reload
```

预期：没有输出。

不符合时：看 `docker logs vllm-pd-prometheus` 的最后几行。

### 步骤 B13：确认目标都抓到了（在监控机上）

```bash
sleep 45 && curl -s 'http://localhost:9090/api/v1/targets?state=active' | grep -o '"health":"[a-z]*"' | sort | uniq -c
```

预期：只有一行 `497 "health":"up"`。

不符合时：看 `http://<监控机>:9090/targets`。某些端口一直 down，看附录 D 的端口表，确认该进程真的存在。

### 步骤 B14：再查一次每个 panel（在监控机上）

等 3 分钟攒数据。密码取步骤 27 输入的 `GRAFANA_PASSWORD`；换了终端就先执行 `read -rs GRAFANA_PASSWORD && export GRAFANA_PASSWORD`。

```bash
sleep 180 && GRAFANA_USER=admin python3 scripts/verify_panel1.py --prometheus http://localhost:9090 --inventory prometheus/inventory.json --grafana http://localhost:3000 --minutes 3 | tail -n 1
```

预期：`RESULT: OK`。

不符合时：SSD 的 panel 要开 SSD，D 的 Client panel 要 D 用 KV 池。其他为空的看「排查」。

---

## 停止、重启和卸载

都在 `vllm-pd-grafana-panel1/` 目录里执行。容器带 `--restart unless-stopped`，机器重启后会自动起来。

| 要做什么 | 命令 |
|---|---|
| 停止 | `docker stop vllm-pd-grafana vllm-pd-prometheus` |
| 再启动 | `docker start vllm-pd-prometheus vllm-pd-grafana` |
| 重启 Grafana | `docker restart vllm-pd-grafana` |
| 看日志 | `docker logs --tail 50 vllm-pd-grafana` |
| 改清单后生效 | 做步骤 13，然后 `curl -s -X POST http://localhost:9090/-/reload` |
| 删容器，保留数据 | `docker rm -f vllm-pd-grafana vllm-pd-prometheus`，要再装时从步骤 24 开始 |
| 全部删掉，包括数据 | `docker rm -f vllm-pd-grafana vllm-pd-prometheus`，然后 `docker volume rm vllm-pd-prometheus-data vllm-pd-grafana-data`，然后 `docker network rm vllm-pd-monitor` |

看板由 provisioning 管理，界面上改了不能保存。要改就改 `grafana/dashboards/` 里的 JSON，Grafana 30 秒内自动加载。

---

## 同一台监控机上再加一个集群

每个集群一套 Prometheus、一个数据源、两张看板；Grafana 和容器网络共用。上面步骤 1–31 装的那一套保持原样。

```bash
export GRAFANA_URL=http://localhost:3000 GRAFANA_USER=admin DOCKER_NETWORK=vllm-pd-monitor
read -rs GRAFANA_PASSWORD && export GRAFANA_PASSWORD
bin/panel1_cluster.sh adopt . --cluster <现有集群名> --model-group <模型>     # 只做一次：登记上面装的那一套，不改它
bin/panel1_cluster.sh add <集群名> --model-group <模型> --inventory <清单>      # 新集群
bin/panel1_cluster.sh add <集群名> --model-group <模型> --inventory <清单> --root <总目录>   # 指定总目录，记进注册表，以后的 add 不写也用它
bin/panel1_cluster.sh add <集群名> --model-group <模型> --inventory <清单> --dir <目录>      # 只这个集群装到指定目录，优先于总目录
bin/panel1_cluster.sh list
bin/panel1_cluster.sh check <集群名>
bin/panel1_cluster.sh remove <集群名>
```

- **add：** 依次做下面这些事，重跑同一条命令就是更新（`prometheus.yml` 里面板二写的标记段原样保留）：
  - 目录 `<总目录>/vllm-pd-grafana-panel1/<集群名>`，总目录依次取 `--root`、注册表里记的、`$HOME`，给了 `--dir` 就用 `--dir`；
  - 端口段自动分配，`--port-base` 可以指定，Prometheus 用段内第 3 个端口；
  - 容器 `vllm-pd-prometheus-<集群名>` 和数据卷 `vllm-pd-prometheus-data-<集群名>`；
  - 经 Grafana API 建数据源 `Prometheus（<集群名>）`，在文件夹 `<模型>` 里导入两张标题带「（<集群名>）」的看板；
  - 最后跑 `scripts/verify_panel1.py --cluster <集群名>`。
- **看板的 datasource 变量只匹配本集群的数据源。** 数据源不在时看板报找不到数据源，不会显示别的集群的数据。
- **防误用：** 注册表 `~/.vllm-pd-clusters.json` 记每个集群的容器、端口、目录、UID 和机器。与别的集群重复，或端口被占，`add` 在建任何东西之前就停下。
- **remove：** 删两张看板、数据源、容器、数据卷、目录和注册表条目，Grafana 文件夹保留。按上面步骤 1–31 装的那一套，看板是文件预置的，`remove` 拒绝删。
- **账号：** 要 Grafana 组织管理员（Admin），因为建数据源要这个权限。
- **共用的值：** 没有 export 时，从面板二的 `deploy.env`（`--env-from`）或已登记集群的 `deploy.env` 取。`DOCKER_NETWORK` 也取不到时，用 Grafana 容器所在的网络。

改看板之前备份、出错时恢复（只针对用文件预置的那一套）：

```bash
bin/panel1_backup.sh <面板一目录> -o <备份目录> --prometheus-container vllm-pd-prometheus
bin/panel1_restore.sh <备份目录> --grafana-url http://localhost:3000 --prometheus-url http://localhost:9090
```

- **恢复：** 文件写回原处，inode 不变；写完核对每个文件都与备份逐字节相同。
- **重新加载：** 让 Grafana 重新读取预置，这一步要 Grafana 服务器管理员账号。`prometheus.yml` 有变化时，再让 Prometheus 热加载。

---

## 排查

| 现象 | 原因和处理 |
|---|---|
| `docker logs` 里有 `permission denied`（读 `prometheus.yml` 或 `grafana/`） | 文件权限：执行 `chmod -R a+rX .`。开了 SELinux 时，在 `docker run` 的 `-v ...:ro` 后面改成 `:ro,z` |
| 「模型」下拉框是空的 | vLLM 目标没抓到，或者实例加了 `--disable-log-stats`。先看步骤 26 |
| 实例下拉框少实例，或某个 P、D 没有线 | 每个实例的 API server 都要抓。先看 `http://<监控机>:9090/targets` 里它是不是 up；清单列表漏了它就补上，做一遍「改清单后生效」 |
| 实例名和真实序号对不上 | 清单列表没按 `--data-parallel-rank` 排。重做步骤 17，`verify_panel1.py` 也会报 `reports engine ... expected ...` |
| 只有 P 或只有 D 没数据 | 检查清单里对应那一侧的 `api_servers` |
| 时延、吞吐 panel 没有数据 | 集群上没有请求。发一些请求，等 1 分钟 |
| 总览的「AICore 利用率」等没有数据 | npu-exporter 目标 down |
| 曲线断断续续 | `PROMETHEUS_SCRAPE_INTERVAL` 和清单 `scrape_interval` 不一致 |
| 时间对不上 | 各节点时钟没同步 |
| 选实例后 Client、TE 的线和 vLLM 的线对不上 | `procs_per_instance` 写错，或列表顺序不对（步骤 17） |
| 进程日志 `Failed to start Transfer Engine metrics HTTP server … bind failed` | 端口被占了，换基准端口（附录 D） |
| 进程日志 `gives no valid metrics port` | 基准值 + slot 超出 65535，或者不是数字 |
| 进程日志 `rank is unknown; metrics port not opened` | 补丁算不出这个进程的 slot，这个进程没开指标端口 |
| 一行 Transfer Engine 日志都没有 | Mooncake 可能没用 `WITH_METRICS` 编译 |
| 标题旁有一个小图标 | Prometheus 的 info 提示，不是错误。`mooncake_transfer_read_bytes` 等上游指标名不以 `_total` 结尾，`rate()` 时提示「可能不是 counter」。时延 panel 上如果提示 `needed to be fixed for monotonicity`，是直方图的桶计数没有完全同步，结果已被 Prometheus 修正。都不影响看图 |
| D 的图例只显示一部分 | 64 个实例放不下，滚动图例能看到全部；也可以用「实例」下拉框只选几个 |

---

## 附录 A：清单字段

| 字段 | 含义 |
|---|---|
| `vllm_ascend_patched` | 必填，`true` 或 `false`。`false` 时不生成 client 和 TE 的抓取目标 |
| `prefill.api_servers`、`decode.api_servers` | API server 的 `IP:PORT` 列表，按 `--data-parallel-rank` 排。第 n 项（从 0 数）就是实例 `P<n>` / `D<n>` |
| `prefill.procs_per_instance`、`decode.procs_per_instance` | 每个实例的进程数，= TP × PP × prefill-CP。EP 和 DCP 不增加进程 |
| `prefill.scheduler_store_clients`、`decode.scheduler_store_clients` | 这一侧用了 AscendStoreConnector，每个实例有一个调度侧 store client 时写 `true`。没有就不写 |
| `prefill.mooncake_client_port_base`、`transfer_engine_port_base`、`store_transfer_engine_port_base`（`decode` 同） | 只对这一侧覆盖基准端口。写 `null` 表示这一侧没有这类端口。D 不用 KV 池时给 `decode` 写 `"mooncake_client_port_base": null` |
| `prefill.store_owns_te`、`decode.store_owns_te` | 静态链接时才写，见附录 E。`"workers"`：只因 SSD 预取。`"all"`：fabric memory 或 `ASCEND_GLOBAL_RESOURCE_CONFIG` |
| `scrape_interval`、`scrape_timeout` | 抓取间隔和超时，默认 `15s`、`10s`。改了 `scrape_interval` 要同步改步骤 27 的 `PROMETHEUS_SCRAPE_INTERVAL` |
| `retention` | Prometheus 数据保存时长，默认 `15d` |
| `metrics_port_bases.transfer_engine` | TE 指标基准端口，和 `VLLM_ASCEND_MC_TE_METRIC_HTTP_PORT_BASE` 相同。只在打补丁时用 |
| `metrics_port_bases.mooncake_client` | store client 指标基准端口，和 `VLLM_ASCEND_MOONCAKE_CLIENT_HTTP_PORT_BASE` 相同。全集群都没用 KV 池时填 `null`。只在打补丁时用 |
| `metrics_port_bases.store_transfer_engine` | KV 池自建 TE 的基准端口，和 `VLLM_ASCEND_MC_STORE_TE_METRIC_HTTP_PORT_BASE` 相同。只对写了 `store_owns_te` 的那一侧生效 |
| `scheduler_slot_offset` | 调度侧 client 的端口偏移，默认 1000，和补丁的 `SCHEDULER_SLOT_OFFSET` 相同 |
| `npu_exporter_port`、`node_exporter_port` | 两个 exporter 的端口 |
| `mooncake_master.address`、`.node` | master 的 `host:metrics_port`，和显示用的节点名 |

**生成脚本怎么算：**

| 算什么 | 算法 |
|---|---|
| 实例名 | 列表第 n 项是 `P<n>` / `D<n>` |
| 机器 | 列表里不同的 IP，按第一次出现的顺序，节点名 `p-node-0`、`d-node-0`… |
| 实例的 worker 全局 rank | n × `procs_per_instance` 到 n × `procs_per_instance` + `procs_per_instance` − 1。例如 `P1` 是 16–31，`D63` 是 63 |
| 进程的指标端口 | 基准端口 + 全局 rank，在这个实例的 IP 上 |
| 调度侧 client 的端口 | 基准端口 + 1000 + n |

全局 rank 的算法在 vllm-ascend 3b1da3fe 的 `get_global_rank`（`vllm_ascend/distributed/parallel_state.py`）：`data_parallel_index × (TP × PP × prefill-CP) + rank_in_group % (TP × PP × prefill-CP)`。MoE 下 world group 跨所有 DP，这个公式同样成立；EP 叠加在 TP、DP 上，不增加 rank。

---

## 附录 B：其他组合

**只要每个实例有自己的 API server，就只需要改两个列表和 `procs_per_instance`。**

- 8 台机器、每台 2 个 P，TP8：`prefill.api_servers` 写 16 项（每台两个端口），`procs_per_instance` 写 `8`。实例名是 `P0` 到 `P15`，`P3` 是第二台机器的第二个端口，rank 24–31。
- 2 台机器、每台 32 个 D，TP1：`decode.api_servers` 写 64 项，实例名还是 `D0` 到 `D63`，rank 0–31 在第一台。
- 步骤 14、15、17 的预期数字相应变化：目标数 = API server 个数 + 1 + 2 × 机器数。

**一个 API server 带多个实例（内部负载均衡）。** 启动时只有 `--data-parallel-size N`，没有 `--data-parallel-rank`。这时用另一种写法：去掉 `prefill`、`decode`，写 `api_servers`，每个 API server 列出它的节点和这些节点上进程的全局 rank：

```json
"api_servers": [
  {"name": "P-API-0", "role": "prefill", "address": "10.0.1.1:9000", "procs_per_dp": 16, "nodes": [
    {"node": "p-node-0", "host": "10.0.1.1", "ranks": "0-15", "dp_indexes": "0"},
    {"node": "p-node-1", "host": "10.0.1.2", "ranks": "16-31", "dp_indexes": "1"}
  ]}
]
```

这种写法里，实例名按 DP 序号（rank ÷ `procs_per_dp`）依次编号，由 `metric_relabel_configs` 把 `(api_server, engine)` 换成 `pd_instance`，所以要求 `engine` 是全局的 DP 序号。先用下面的命令确认（把地址换成你的）：

```text
curl -s http://10.0.1.1:9000/metrics | grep -o 'engine="[0-9]*"' | sort -u
```

预期是 `engine="0"` 到 `engine="N-1"`。这种写法下步骤 17 没有输出内容可比，输出 `0/0`。

---

## 附录 D：端口规则（打补丁时）

| 进程 | slot | TE 端口 | store client 端口 | KV 池自建 TE 端口 |
|---|---|---|---|---|
| worker，全局 rank r | r | 20000 + r | 22000 + r | 24000 + r |
| 调度侧 store client，DP 序号 d | 1000 + d | 21000 + d（`store_owns_te: "all"` 时没有） | 23000 + d | 25000 + d（只在 `store_owns_te: "all"` 时有） |

- 相邻两个基准值之间至少隔「1000 + 最大 DP 序号 + 1」个端口。
- worker 的全局 rank 必须小于 `scheduler_slot_offset`（默认 1000），否则 worker 和调度侧端口相撞，生成脚本直接报错。全局 rank 超过 999 时（例如 DP64 × TP16），把偏移调大到超过最大 rank，补丁里的 `SCHEDULER_SLOT_OFFSET` 和清单的 `scheduler_slot_offset` 一起改，相邻基准值也要拉开。
- 同一侧的实例全局 rank 不同，同一台机器上放多个实例不会撞端口。
- P 和 D 在同一台机器上时，它们的 rank 可能相同，端口会撞，生成脚本会报错。给其中一侧换一组基准值（例如 26000 / 28000 / 30000），在清单这一侧写 `transfer_engine_port_base`、`mooncake_client_port_base`，启动环境也要一致。这台机器的 npu-exporter 和 node_exporter 只抓一次，归到清单里先出现的那一侧。
- 基准端口要避开 vllm-ascend 的 `kv_port` 握手端口段和机器上已用的端口。

---

## 附录 E：补丁做了什么

**为什么要打：**

- Transfer Engine 的指标端口只从环境变量 `MC_TE_METRIC_HTTP_PORT` 读，同一节点上的 worker 继承同一份环境，只有第一个进程能绑定。
- 嵌入式 store client 的指标端口在 vllm-ascend 里根本没打开。

**补丁的做法：**

- Transfer Engine：在全局 TE 创建之前，把 `MC_TE_METRIC_HTTP_PORT` 设成「TE 基准端口 + slot」。改动在 `utils/mooncake_transfer_engine.py`。P/D 连接器（`kv_p2p/mooncake/base_worker.py`）和 KV 池（`mooncake_backend.py`）在取全局 TE 之前，先登记本进程的 parallel_config。
- store client：给 `store.setup()` 的两条路径（`_setup_store_config_dict`、`_setup_store_kwargs`）加上 `enable_client_http_server=True` 和 `client_http_port=基准端口 + slot`。
- slot：worker 是全局 rank；调度侧 client 所在进程没有 world group，用 1000 + DP 序号。
- 不设基准端口时，什么都不改。算不出 slot 或端口不合法时，不开端口，只打一行 warning。

**一个进程里可能有两个 Transfer Engine。** 通常每个进程只有一个全局 TE。下面三种情况，KV 池会在 `store.setup()` 里自己再建一个（行号是 3b1da3fe 原文件的 `mooncake_backend.py`）：

- 设了 `ASCEND_GLOBAL_RESOURCE_CONFIG`（:400、:543）；
- 开了 SSD 预取，走 config-dict 路径（:455、:482）。只有 worker 走这条路径，调度侧用 `contribute_memory=False`（:555-557），仍用全局 TE；
- 开了 fabric memory（`ASCEND_ENABLE_USE_FABRIC_MEM=1`）。

两个 TE 的指标会不会互相挤掉，取决于 Mooncake 怎么编译（步骤 B4 的判断）：

- **共享库**：进程里只有一份指标，两个 TE 记到同一组计数器（`transfer_engine_metrics.cpp:23-26`，`metrics_exporter.cpp:119`），不会漏数据。不要设 store TE 基准端口，清单不要写 `store_owns_te`。
- **静态链接**：两个模块各有一份指标，第二个绑定失败（`metrics_exporter.cpp:155-166`），它的流量不在 TE 的 panel 里。这时设 `VLLM_ASCEND_MC_STORE_TE_METRIC_HTTP_PORT_BASE=24000`，在清单给这一侧写 `store_owns_te`：
  - `"workers"`：只因 SSD 预取。worker 多一个 store TE 端口；调度侧仍在 21000 + d。
  - `"all"`：fabric memory 或 `ASCEND_GLOBAL_RESOURCE_CONFIG`。调度侧也自建 TE，有 25000 + d，没有 21000 + d。
  - 只用 AscendStoreConnector、没有 P/D 连接器的那一侧，再写 `"transfer_engine_port_base": null`。
- 静态链接时，TE 的 panel 只保证覆盖连接器一侧；KV 池走共享 TE 时，它的传输要看 client 指标，`_store.so` 那份指标单例从没启动 exporter（`metrics_exporter.cpp:118-119`）。

以上按源码推断，没有在 A3 上验证。

**补丁验证到什么程度：** 针对 `3b1da3fe9ac5a40ec6a200f31a20d33f67e2f02c` 生成，在这个提交的原始文件上 `git apply --check` 通过，改动 3 个文件。端口计算和调用顺序用替身和语法树检查过（20 项）。**没有在昇腾、真实的 vLLM 或 Mooncake 上运行过。**

---

## 附录 F：看板怎么响应实例筛选

- 带实例名的 panel：时延、调度、KV Cache、传输、Client，选 All 显示全部，选一个只剩它自己。
- 不响应实例筛选的：总览（看整体）；HTTP 和准入拒绝（按 API server 显示）；NPU 和主机（按节点显示）；Mooncake Master 分组里除「Master 心跳」外的 panel（集群级）。

每个 panel 的指标、PromQL 和出处见 demo 目录的 `PANELS.md`。

**容量。** 不打补丁时目标数是 API server 个数 + 1 + 2 × 机器数，很轻。打补丁后每个进程多一个 client 和一个 TE 目标，15 秒抓一次。大集群上先看 Prometheus 的 `scrape_duration_seconds` 和内存，再决定要不要调大 `scrape_interval`。

---

## 附录 G：本包是怎么测试的

用部署包原样部署，抓取目标换成模拟导出程序。

| 项目 | 内容 |
|---|---|
| 版本 | Prometheus v3.11.2、Grafana 12.4.2（arm64，本机 Docker）。`storage.tsdb.retention.time` 写在配置文件里生效；设了 `GF_PLUGINS_PREINSTALL_DISABLED=true` 后启动时不再从网上装插件 |
| 拓扑 | P 8 个 API server；D 64 个 API server，分布在 4 台机器，每台 16 个。每个 API server 只报自己的 `engine` |
| 怎么跑 | 解开 tar 包，逐条执行本文的命令，只替换地址、容器名、端口、密码。不打补丁跑步骤 1–31；再跑步骤 B7–B14 |
| 不打补丁 | 97 个目标全部 up；上面名单之外的 panel 都有数据；名单在 Prometheus、Grafana 和页面上一致 |
| 打补丁 | 497 个目标全部 up；所有 panel 都有数据 |
| 选单个实例 | 用每个带 `$pd_instance` 的查询判定：All 时 P 有 8 条线、D 有 64 条线，选 `P3`、`D3` 后只剩它自己 |
| 列表顺序 | 故意调换清单里两个地址，`verify_panel1.py` 会报 `reports engine ... expected ...` 并返回失败 |
| 其他 | Grafana 日志 0 条 error，默认页面没有红色告警。生成脚本的单元测试覆盖了 8 台各 2 个 P（`P0`–`P15`）、多种 D 分布、列表顺序和报错 |

**没有验证的：**

- 没有连真实的 vLLM、Mooncake、npu-exporter，也没有在昇腾机器上运行。数据都是模拟的，指标名和 label 按上游源码写。
- 步骤 B1–B6（打补丁、改启动环境、重启）没有执行。补丁只在 `3b1da3fe` 的原始文件上试过 `patch --dry-run`，端口计算和调用顺序用替身和语法树检查过。
- `engine` 等于 `data_parallel_index`、全局 rank 的算法，都只读过源码（vLLM v0.32.0、vllm-ascend `3b1da3fe`），没有在真实集群上运行验证。步骤 17 就是用来在现场确认的。
- 附录 B 里 D 的端口规律、`store_owns_te` 的各种情况，只有单元测试。
