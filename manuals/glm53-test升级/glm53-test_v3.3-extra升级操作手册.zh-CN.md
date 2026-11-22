# glm53-test：v3.3-extra 升级操作手册（T58）

> 把 glm5.3 测试集群（面板二里的集群 `test-glm53`）从 v3.2-extra 升级到 v3.3-extra：面板二换成 v3.3-extra，补丁从 T56 升到 T58，Mooncake 的替换库从 v1 换成 v2。补丁包 `t58-glm53-patch-kit.tar`（sha256 `4f59ea877db09e46…`）、Mooncake 增量包 `t58-glm53-mooncake-kit.tar`（sha256 `e3e338d5cd5a95d4…`）和面板包都不变，本手册放在包外面。命令从《glm5.3 测试集群打补丁操作手册》（`48c88aa7…`）、T56 升级手册的命令来源和本手册的命令来源原样照抄。

<!-- 占位符版开始 -->
> **本手册有两版。** 这一版是占位符版：路径写成尖括号里的说明，不含任何内网信息。已填路径版把监控机上的个人路径填好，只放在私有的 Caching 仓库。
<!-- 占位符版结束 -->

## 第一页

**做什么。** 这次：

1. **第一部分：** 升级前，用一组参考请求跑 3 遍，记下 TPOT（开销判据的基线）。
2. **第二部分：** 换面板二（v3.3-extra）。集群照常运行，不重启 vLLM。
3. **第三部分：** 集群照常运行时，在 P、D 两个容器里放补丁包和 Mooncake 增量包、检测、核对、备份（T56 版的文件和现在的 Mooncake 库 v1），把 p2d 的结果先写到 `/tmp`。
4. **第四部分：** 整个集群停一次，两个容器都换成 T58 的文件、把 Mooncake 换成 v2，一个 D 容器做导入核对，然后一起拉起。不分批。
5. **第五部分：** 拉起以后核对：检测脚本、启动行（不对就停）、Mooncake 的记录文件、过滤器自检、留空比例、磁盘、面板的安全检查。
6. **第六部分：** 用一条只读查询做首批数据核对。
7. **第七部分：** 升级后，同一组参考请求再跑 3 遍，比 TPOT。
8. **第八部分：** 第一步开销分析（只读查询，必做）。

**前提：** T56 升级手册（v3.2-extra）已经做完，检测报 `ok(t56)`。Mooncake 换过 v1（`mooncake=ok(v1)`）的，第三、四部分把它换成 v2；没换过的（`mooncake=not_replaced`），Mooncake 那几步跳过，P 后台 put 下 3 段照旧为空。proxy 做没做都可以，T58 不动 proxy。

**为什么先换面板、再换补丁：** T58 的过滤器多认一个字段 `diag_level`，过滤器程序变了，要重启 vLLM 才生效。所以先换面板二（新的段定义旧过滤器也读得了），再停机换补丁，新过滤器随拉起一起加载。

**粒度：** T58 加了一个环境变量 `PD_DIAG_LEVEL`，控制记到第几级。本手册不设它，就是 3 级：P 写出的东西和 T51 逐字段相同，D 的和 T56 逐字段相同，判据和 T56 相同。多出的只有：启动行多一行 `diag level=3 source=default`；检测脚本报 `ok(t58)`、`diag_level=3(default)`；Mooncake 的计时记录不再写进 vLLM 日志，改写进节点目录下 `logs/mooncake/` 里自己的文件。不新建镜像，不新建容器，改动写在容器的可写层里。

**做完是什么样子。**
- P、D 两个容器的检测脚本都报 `verdict vllm=ok(t58) ascend=ok(t58,site_local=<n>) p2d=ok(t58) mooncake=ok(v2)`（没换过 Mooncake 的是 `mooncake=not_replaced`），`run` 行末是 `diag_level=3(default)`。
- 启动行：EngineCore 和 Worker 各多一行 `diag level=3 source=default`；D 的 `tp_rank=0` Worker 启动行照旧列出 6 个 stage。
- P 机器的节点目录下 `logs/mooncake/` 里有 `mc.*.log` 文件，vLLM 日志里不再新增 `[BatchPutTiming]` 行。
- 面板二 `test-glm53` 的看板在 v3.3-extra 下，L1、L2、L3 三级可以折叠；3 级下段和数值和 v3.2-extra 相同。
- 首批核对的查询 `RESULT: PASS`；TPOT 判据 `OK`。

**停机：** 第四部分从第 18 步停集群到第 23 步拉起。停以前的第 7–17 步只读，或者只往 `/tmp` 和备份目录里写，不影响服务。第二部分换面板不碰 vLLM。

**要先准备好的现场值：**

<!-- 占位符表开始 -->
| 占位符 | 含义 | 在哪里查 |
|---|---|---|
| `<用户目录>` | 监控机上放部署包和更新脚本的目录 | 包二 v3 更新脚本「现场路径」一段的 `USER_DIR` |
| `<总目录>` | 面板目录的上级目录 | 同一个脚本的 `ROOT` |
| `<SSH 账号>`、`<机器地址>` | 从监控机登录 glm5.3 机器用的账号和机器 | T51 打补丁时用的值 |
| `<机器>` | 这台机器的简称，只用在监控机上备份目录的名字里；同一台机器每次写法相同 | 自己定，和 T51 时相同即可 |
| `<容器名>` | 这台机器上跑 vLLM 的容器 | 在那台机器上 `docker ps` |
| `<节点目录>` | 面板二的过滤器在这台机器上写文件的目录（宿主机路径） | `/data/pd-diag`，和 T51 时相同 |
| `<vLLM 日志文件>` | 这台机器上各个 vLLM 实例的标准输出、标准错误重定向到的文件（宿主机路径），几个文件用空格隔开；D 机器有 16 个 | 和 T51 时相同 |
| `<启动文件目录>` | 容器里 `run_dp_template.sh`、`launch_online_dp.py`、`start_p.sh`（D 上 `start_d0.sh`）、`mooncake.json`、`start_mooncake.sh` 所在的目录 | 和 T51 时相同 |
| `<容器里的 vLLM 日志文件>` | `<vLLM 日志文件>` 在容器里看到的路径；容器里看不到就不传 `--startup-log` | 同上 |
| `<T51 的备份目录>` | T51 打补丁时第 6 步在容器里做的备份目录，`/tmp/t51-glm53-backup-<时间戳>`（`/tmp` 是 tmpfs 时在 `/root` 下）。里面有现场的 p2d 原文件，第 12 步要用 | T51 时记下的 `<备份目录>`，T56 升级时也用过；容器里没有了，见第 12 步「不对时」 |
| `<proxy 地址>` | 第一、七部分的参考请求发到的 proxy，`<IP 或主机名>:<端口>` | 平时发请求用的 proxy 地址 |
| `<模型目录>`、`<服务名>` | P 容器里的模型目录；请求里的模型名（vLLM 启动时的 `--served-model-name`，没设就是模型目录） | P 的启动文件 |
| 端口 | D 的容器是 `$(seq 9100 9115)`（16 个实例），P 的容器是 `9900`。现场不同时按同样的格式改 | 第 9 步检测的 `inst[run …]` 行的 `ports=` |
<!-- 占位符表结束 -->

做的过程中还要记下几个值（第 9 步的 `baseline_token` 和 Mooncake 的状态、第 13 步的 `<备份目录>`、`<时间戳>`、第 15 步的 `<MC 备份目录>`、`<MC 时间戳>`、第 16 步的 `<S58>`、第 1 步的 3 个 TPOT），在那一步里写明，记进下面的记录表。

## 通用约定

- 所有容器里的命令都以 root 执行：先在那台机器上 `docker exec -it <容器名> bash` 进入容器。
- 任何一步的结果和「应当看到」不一样时，停下，把那一步的输出带回来，不要继续。
- `LC_ALL=C sha256sum -c … | grep -v ': OK$'` 这类核对命令，没有输出就是全部通过，这时整条管道的退出码是 1。不要把它们放进带 `set -e` 的脚本，按输出判断。
- `docker restart` 以后补丁还在；`docker rm` 后重建容器、或者换镜像，补丁就没了。
- 不设 `MC_LOG_DIR`；不设 `VLLM_PD_DEVICE_STAGES`（除非第七部分的判据要求）；不设 `PD_DIAG_LEVEL`、`MC_PD_TIMING`（不设就是 3 级、Mooncake 照写）。
- 三件补丁必须一起换、一起回滚：vllm-ascend 主体和 p2d 用到的名字只有 vLLM 补丁里有。Mooncake 的库可以单独回滚：v1、v2 对外的接口相同。
- **窗口：** 监控机一个窗口；P、D 两台机器各开一个宿主机窗口和一个容器窗口。容器窗口从第 8 步开到第 23 步拉起以前都不要关：第 10、11、13 步设的变量只在这个 shell 里。中途断了，按第 19 步「不对时」第一行重新设。
- **同一条命令，每个容器各执行一次。** 写「P 的容器」的在 P 的容器窗口执行；写「D 的容器」的在 D 的容器窗口执行；没有分 P、D 的，两个窗口都执行。宿主机窗口、监控机上的命令同样按「每台机器一次」执行。

**记录表：**

| `<机器>` | 角色 | `<容器名>` | `baseline_token`、`mooncake=`（第 9 步） | `<备份目录>`、`<时间戳>`（第 13 步） | `<MC 备份目录>`、`<MC 时间戳>`（第 15 步） | `<S58>` 的前 16 位（第 16 步） |
|---|---|---|---|---|---|---|
| | P | | | | | |
| | D | | | | | |

| 第 1 步升级前的 Median TPOT（3 遍） | 第 36 步升级后的 Median TPOT（3 遍） |
|---|---|
| | |

## 第一部分　升级前：参考请求 3 遍

#### 第 1 步　参考请求跑 3 遍，记下 TPOT

**机器：** 甲在 P 的容器窗口里执行，集群照常运行（T56 版）。乙用你自己平时那套工具。

**用哪一种：** 优先用甲。甲用不了（例如报 `vllm: command not found`，或者发不到 proxy），改用乙。升级前后必须用同一种、同一组参数。

**甲：** vLLM 自带的压测，发到 proxy。同一条命令跑 3 遍，每遍之间不用等：

```bash
vllm bench serve --backend openai --base-url http://<proxy 地址> --endpoint /v1/completions --model <模型目录> --served-model-name <服务名> --dataset-name random --random-input-len 1024 --random-output-len 128 --ignore-eos --num-prompts 64 --max-concurrency 8 --seed 0 2>&1 | tee -a /tmp/t58-tpot-<升级前或升级后>.txt | grep -E 'Successful requests|Failed requests|Median TPOT'
```

- `--model` 填 P 容器里的模型目录：它同时用来加载分词器，生成随机请求要用。`--served-model-name` 填请求里的模型名，就是服务启动时的 `--served-model-name`；服务没设这个名字时，这一项可以不写。服务启动带 `--trust-remote-code` 时，这条命令末尾也要加 `--trust-remote-code`。
- `<升级前或升级后>` 这次填 `升级前`：3 遍的完整输出都追加在 `/tmp/t58-tpot-升级前.txt` 里。
- 参数：随机数据，输入 1024 个 token、输出 128 个（`--ignore-eos`：不提前结束），64 个请求，最多同时 8 个，随机种子 0。
- 这条命令在 P 容器里只当客户端用，往 proxy 发请求；参数名按 vLLM v0.30.0 的源码核过。

**乙：** 用你自己的工具，发同一组请求、同样的并发，跑 3 遍，每遍记下它给出的 TPOT 中位数。

**应当看到：** 甲每遍三行：`Successful requests: 64`、`Failed requests: 0`、`Median TPOT (ms): <数>`。把 3 遍的 `Median TPOT` 记进记录表。实验室（CPU、2 层小模型，直接发给 vLLM）一遍约 15 秒；现场一遍要多久没有量过。

**不对时：**
- 有失败的请求（`Failed requests` 不是 0）：这一遍不算，查清原因再跑。
- `vllm bench serve` 报 NPU 相关的错误：改用乙，用你自己的工具。

**带回：** 3 遍各自的那三行，以及用的是甲还是乙。

## 第二部分　换面板二（v3.3-extra，集群照常运行）

这一部分把 test-glm53 的面板二从 v3.2-extra 升到 v3.3-extra，需要时回滚到 v3.2-extra。只动 test-glm53 的面板二，glm53-1 和其他集群不动。

四步都在换补丁以前做，集群照常运行，vLLM 不重启。T58 拉起以后的那次核对是第五部分第 32 步。

**v3.3-extra 多了什么：**
- 看板多一个选择「层级」：分布表、单请求页、Top-20 子段明细表显示到第几层（L1、L2、L3），默认第 3 层，就是全部。
- 比请求那一侧的粒度更深的段，看板写「未采（粒度 N 级）」，不再显示成空或 `0 / n`。
- Mooncake 的计时改从 P 机器自己的节点日志目录里的文件读（T58 补丁的 Mooncake 库写的），不用再在 `deploy.env` 里写 `MOONCAKE_LOG_FILES`。
- 过滤器每分钟的自检行带上这个进程的粒度。

**几个词：**
- **粒度：** 环境变量 `PD_DIAG_LEVEL`，补丁记到第几级，1、2、3，不设就是 3。
- **层级：** 看板上的选择，显示到第几层。它和粒度互不相干：3 级记、看板只显示到 L1 也行。
- **未采：** 这一段比请求那一侧的粒度深，补丁本来就不记，不是缺数据。

**放在手册的哪里（T58 的顺序）：**
- 第 2–5 步在整集群停机以前做。换面板不停 vLLM，旧的过滤器照常工作，也读得了新的段定义；第 5 步核对的是这时的样子，数据还是 T56 补丁写的。
- 然后整集群停机，换 T58 补丁和 Mooncake 库，一起拉起（手册的补丁部分）。
- 拉起以后在手册第五部分核对。

**为什么先换面板：** 过滤器程序变了（自检行多了粒度），要重启 vLLM 才用上。T58 本来就要停一次、一起重启，所以不用为面板多停一次。T56 那种「先补丁、后面板、不重启」不再适用。

**预计输出的来源：** 实验室 t56p 照这一部分原文走了一遍：从 v3.2-extra 升级，替身照 T58 那次拉起（P 3 级，D 1 级），核对，回滚。尖括号 `<…>` 是每次不同的部分。实验室和现场不一样的地方，写在第 5 步和最后一节。

### 文中的占位符

| 占位符 | 含义 | 在哪里查 |
|---|---|---|
| `<用户目录>` | 放部署包和更新脚本的目录 | 更新脚本「现场路径」一段的 `USER_DIR` |
| `<总目录>` | 面板目录的上级目录 | 更新脚本的 `ROOT` |
| `<更新脚本>` | 更新脚本的文件名 | 路径已填好的是 `vllm_pd_update_pack2_v33extra_filled.py`；用 `vllm_pd_update_pack2_v33extra.py` 时，先改好它「现场路径」一段 |

### 要做的几步

| 步 | 什么时候 | 在哪里 | 做什么 | 实验室用时 |
|---|---|---|---|---|
| 2 | 停机以前 | 监控机 | 放文件，核对 sha256 | 几秒（不含拷文件） |
| 3 | 停机以前 | 监控机 | 更新脚本：先 `check`，再 `run` | `check`、`run` 加看板分层合计约 45 秒 |
| 4 | 停机以前 | 监控机 | 看板分层：`arrange_boards.py apply` | 几秒 |
| 5 | `run` 以后 3 分钟，停机以前 | 监控机 | 核对：升级安全检查、coverage（T56 的数据） | 约 4 分钟（含等待） |
| 6 | 需要时 | 监控机 | 回滚到 v3.2-extra | 回滚加看板分层合计约 28 秒 |

### 对同机其他服务的影响

只动 test-glm53 自己的面板二。

| 机器 | 服务 | 影响 |
|---|---|---|
| 监控机 | glm53-1 的面板二和面板一 | 不碰 |
| 监控机 | Grafana | 不重启，只重新导入 test-glm53 的看板 |
| 监控机 | test-glm53 的 ClickHouse | 不重启，数据不删：自检表加一列 `diag_level`，两个视图（`phase_line_cur`、`request_json`）按新段定义重建 |
| 监控机 | test-glm53 的采集器、拉取程序 | 各重启一次，几秒没有新数据 |
| 监控机 | 磁盘 | 新包和离线包解到 `<用户目录>/vllm-pd-panel2-update-v3.3-extra/pack/`，约 390 MB，升级后不自动删；备份写到执行账号家目录的 `vllm-pd-update-backup/<时间>/` |
| test-glm53 的节点 | vLLM | 第 3 步不重启。过滤器程序变了，T58 那次整集群重启时用上；重启以前旧过滤器照常工作，约 2 秒内自己重读新的 segments.json |
| test-glm53 的 P 机器 | 节点日志目录 | 3 级时 Mooncake 的计时文件写在 `/data/pd-diag/logs/mooncake/`，保留 12 小时。最重一档每台 P 机器每天约 63 GB，12 小时约 31.5 GB；拉取程序把它和节点日志一起拉回，监控机的镜像盘也多这么多 |

#### 第 2 步　放文件，核对 sha256

**做什么、为什么：** 把新的面板包和更新脚本放到监控机上，核对包没有拷坏、就是交付的那一份。

**机器：** 监控机。

**命令：** 先建两个目录。`vllm-pd-grafana-panel2.tar.gz` 和 `.sha256` 放进第一个目录，`<更新脚本>` 放进第二个，再核对。

```bash
mkdir -p <用户目录>/vllm-pd-panel2-update-v3.3-extra <用户目录>/vllm-pd-update-v3.3-extra
```

```bash
cd <用户目录>/vllm-pd-panel2-update-v3.3-extra && sha256sum -c vllm-pd-grafana-panel2.tar.gz.sha256
```

**应当看到：** `vllm-pd-grafana-panel2.tar.gz: OK`。

**不对时：** `FAILED`：重新拷一次面板包和 `.sha256`。

**马上会看到：** 只多了两个目录和三个文件，别的都没动。离线包仍用 `<用户目录>/vllm-pd-panel2-update-1112/` 里那一份，不用再拷。

#### 第 3 步　对 test-glm53 执行更新脚本

**做什么、为什么：** 先只读核对能不能升，再升级。升级时脚本先备份，再给表加列、换段定义和看板、重启采集器和拉取程序；数据不删。

**机器：** 监控机。

**命令：**

```bash
cd <用户目录>/vllm-pd-update-v3.3-extra && python3 <更新脚本> check --cluster test-glm53
```

```bash
cd <用户目录>/vllm-pd-update-v3.3-extra && python3 <更新脚本> run --cluster test-glm53
```

**`check` 应当看到**（摘要一段）：

```
vllm_pd_update_pack2_v33extra.py check，面板二 v3.3-extra，集群 test-glm53，成功
check：通过（面板二 v3.2-extra，vllm 模式；WARN 2 项）
迁移预览：RESULT: WARN（1 项）
WARN 要重启 vLLM：过滤器文件变了（pd_diag_logfilter.py）。照手册在 T58 补丁那次整集群停机里一起重启；重启以前旧过滤器照常工作、读得了新的 segments.json，只是自检行里还没有 diag_level
INFO 段定义换成新包的 profiles/glm53/segments.json（config_id pd-diag-v3.2-extra-glm53-2026-11-20a → pd-diag-v3.3-extra-glm53-2026-11-20a）
INFO segments.json 会更新（pd-diag-v3.2-extra-glm53-2026-11-20a@04d0c3ea → pd-diag-v3.3-extra-glm53-2026-11-20a@ace07cfe），过滤器自己重读；config_id 变了：pd-diag-v3.2-extra-glm53-2026-11-20a → pd-diag-v3.3-extra-glm53-2026-11-20a
升级安全检查（升级前）：RESULT: WARN（1 项）
WARN 版本和文件：1 个文件和 SHA256SUMS 不同，升级会换成包里的：segments.json
INFO 节点：<机器> 找到 vLLM 容器 <容器名>（容器里有 vllm 进程）；不对时填 deploy.env 的 NODE_VLLM_CONTAINERS
INFO 节点：<机器> 找到 vLLM 容器 <容器名>（容器里有 vllm 进程）；不对时填 deploy.env 的 NODE_VLLM_CONTAINERS
INFO 粒度：自检表还没有 diag_level 列（v3.3-extra 以前的面板；升级以后才有）
INFO 最近 15 分钟 86 个请求
升级前比对：RESULT: 不变 2 项，有变化 3 项，比不了 0 项
INFO 看板：有变化：panel「请求数」的 targets[0].rawSql
```

- 两条 WARN 都是预期的：段定义要换成新包里的（T56 换包时也有这一条）；过滤器文件变了，要在 T58 那次整集群停机里重启 vLLM。

**`run` 应当看到**（摘要一段）：

```
vllm_pd_update_pack2_v33extra.py run，面板二 v3.3-extra，集群 test-glm53，成功
预检：通过（更新前面板二是 v3.2-extra，vllm 模式；迁移预览 WARN 1 项见上面和日志）
WARN 要重启 vLLM：过滤器文件变了（pd_diag_logfilter.py）。照手册在 T58 补丁那次整集群停机里一起重启；重启以前旧过滤器照常工作、读得了新的 segments.json，只是自检行里还没有 diag_level
备份：<家目录>/vllm-pd-update-backup/<时间>
迁移：deploy.env 里没有要写的
数据：保留，不删（vllm_pd 的 7 张表，共 14523 行）
已更新：表和视图：视图 phase_line_cur 的定义；视图 request_json 的定义；加列 filter_selfcheck.diag_level
已更新：采集器：central.yaml 第 22 行（包里：exclude: ["/pull/*/mooncake/**", "/pull/*/d*/mooncake/**", "…）
跳过（内容不变）：数据源
已更新：看板：panel「P 侧轮次」的 fieldConfig.overrides[1].properties[0].value
跳过（内容不变）：Prometheus（不写文件、不重新加载）
节点文件：segments.json 会更新（pd-diag-v3.2-extra-glm53-2026-11-20a@04d0c3ea → pd-diag-v3.3-extra-glm53-2026-11-20a@ace07cfe），过滤器自己重读；config_id 变了：pd-diag-v3.2-extra-glm53-2026-11-20a → pd-diag-v3.3-extra-glm53-2026-11-20a；要重启 vLLM：过滤器文件变了（pd_diag_logfilter.py）。照手册在 T58 补丁那次整集群停机里一起重启；重启以前旧过滤器照常工作、读得了新的 segments.json，只是自检行里还没有 diag_level
看板：config_id pd-diag-v3.2-extra-glm53-2026-11-20a → pd-diag-v3.3-extra-glm53-2026-11-20a，只显示升级以后的请求；升级以前的行留在库里，rollback 以后重新显示
段定义：profiles/glm53/segments.json；proxy 采集 关；Mooncake 计时：节点目录里有 mooncake/ 的文件就关联，不用开关（拉取程序 status 的 mooncake 一行）
不变的项：全部通过（9 项）
几分钟后看看板；可选的核对（不影响升级）：cd <总目录>/vllm-pd-grafana-panel2/test-glm53 && bin/panel2_cluster.sh check test-glm53
回到 v3.2-extra：python3 <用户目录>/vllm-pd-update-v3.3-extra/vllm_pd_update_pack2_v33extra.py rollback <家目录>/vllm-pd-update-backup/<时间> --cluster test-glm53
日志：<家目录>/vllm-pd-update-logs/pack2-v33extra-test-glm53-run-<时间>.log
```

- **deploy.env 原样保留：** 更新脚本只在缺少迁移项时追加行，这次没有要追加的。用户加过的 proxy 4 行照旧在用。Mooncake 那 3 行（`MOONCAKE_LOG_COLLECT`、`MOONCAKE_LOG_FILES`、`MOONCAKE_TID_FALLBACK`）v3.3-extra 不再读，原样留着无害，摘要里有一行说明；回滚到 v3.2-extra 时它们又会用上。
- **记下「回到 v3.2-extra」那一行**，回滚时用它。

**不对时：** `run` 中途停下：照摘要里的那一行处理，处理后重跑同一条 `run`。

**马上会看到：**
- test-glm53 的看板空了，从这时起只显示新请求；
- 节点上 `conf` 目录里的三个节点文件换成新的；vLLM 没有重启，自检行里还没有粒度，要等 T58 那次重启；
- P 后台 put 下 3 段从这时起为空，到 T58 换上 Mooncake 新库、拉起以后才有值：v3.3-extra 只从节点目录里的 Mooncake 文件取这 3 段，不再取 glog 行。

#### 第 4 步　看板分层

**做什么、为什么：** 换看板以后，把 test-glm53 的看板放回它在看板分层里的子文件夹。

**机器：** 监控机。

**命令：** 和面板手册第 16 步相同。

```bash
. ~/vllm-pd-test-glm53/vars.sh && python3 "$T45/arrange_boards.py" apply --env-from "$ENV2"
```

**应当看到：** 最后一行是 `RESULT: OK（改了 <n> 项；回退：python3 arrange_boards.py undo <备份目录> --env-from <同一个 deploy.env>）`，前一行是 `OK   再看一遍：没有要改的`。

**马上会看到：** test-glm53 的看板回到它的子文件夹里，其他集群的看板不动。

#### 第 5 步　换面板以后核对（拉起以前）

**做什么、为什么：** 确认面板二已经是 v3.3-extra，各节点上的旧过滤器已经读到新的段定义，数据照常进来。这时还是 T56 的补丁和旧的过滤器：自检行里没有粒度，看板上各段都按 3 级显示。

**机器：** 监控机。

**命令：** `run` 以后等 3 分钟，集群照常有请求就行，再执行：

```bash
cd <总目录>/vllm-pd-grafana-panel2/test-glm53 && cat VERSION && bin/panel2_cluster.sh check test-glm53
```

```bash
cd <总目录>/vllm-pd-grafana-panel2/test-glm53 && bin/coverage.sh
```

**`check` 应当看到：** 第一行 `v3.3-extra`，最后一行 `RESULT: OK`。其中：
- `视图读的是 pd-diag-v3.3-extra-glm53-2026-11-20a`；
- `过滤器版本：<n> 个实例 <n> 个进程都是 pd-diag-v3.3-extra-glm53-2026-11-20a@ace07cfe`：旧过滤器已经重读了新的 segments.json；
- `INFO 粒度：<n> 个实例的自检行没有粒度（过滤器还是 v3.3-extra 以前的，重启 vLLM 以后才有）`：这时就应该是这样，不是错。

实验室的输出（摘出这几行）：

```
v3.3-extra
OK   库：表和视图都在，视图读的是 pd-diag-v3.3-extra-glm53-2026-11-20a
OK   过滤器版本：10 个实例 30 个进程都是 pd-diag-v3.3-extra-glm53-2026-11-20a@ace07cfe
INFO 粒度：10 个实例的自检行没有粒度（过滤器还是 v3.3-extra 以前的，重启 vLLM 以后才有）
RESULT: OK
```

**`coverage.sh` 应当看到：** 每段一行，3 行对照，最后两行汇总。没有「按粒度不记」那一行：T56 的记录里没有粒度，都按 3 级算。P 后台 put 下 3 段为空，T58 拉起以后才有值。段数看 proxy 做没做：

| proxy | 有值的段 | 最后一行 |
|---|---|---|
| 做了 | 51 段 | `RESULT: FAIL`，只列 P 后台 put 下 3 段 |
| 没做 | 49 段 | `RESULT: FAIL`，只列 P 后台 put 下 3 段和 2 个 lb_proxy |

- **为什么是 FAIL：** coverage 不看采集来源在不在，没采到的段也算「有来源但为空」。最后一行列出的段正好是表里这一格的段时，不算失败。

实验室的输出（摘出开头、put 下 3 段和最后两行）：

```
段定义 pd-diag-v3.3-extra-glm53-2026-11-20a：最近 30 分钟里 94 个请求（最多取 200 个）
  P 后台 wait_for_save/put                             43 / 94
  P 后台 wait_for_save/put/submit                       0 / 94
  P 后台 wait_for_save/put/wait_for_transfer            0 / 94
  P 后台 wait_for_save/put/finalize                     0 / 94
56 段里 42 段有值；没有来源、按设计为空的 2 段：P api_gateway、P start_load_kv/wait_for_transfer
RESULT: FAIL（有来源但这些请求里都为空的 12 段：P lb_proxy、P wait_for_put、P forward、P sampling、P 后台 wait_for_save/put/submit、P 后台 wait_for_save/put/wait_for_transfer、P 后台 wait_for_save/put/finalize、D lb_proxy、D forward、D sampling、D 第二步起 forward、D 第二步起 sampling）
```

- 实验室里另外为空的 9 段是替身造成的：替身的样本里 forward、sampling 只有主机时长，没有 wait_for_put；实验室没开 proxy 采集。现场不会这样。

**看板上看到的：**
- 看板顶上多一个选择「层级」，默认 3，各表和换面板以前一样全部显示；
- 只显示换面板以后的请求；
- 哪一段都没有「未采」。实验室 D 的分布表 33 行，没有一行是「未采」；
- P 后台 put 下 3 段为空。

**不对时：**
- `过滤器版本` 一行不是 `OK`：照那一行箭头后面的处理（一般是 2 分钟后再查）；还不对，带回全部输出。
- coverage 最后一行列出了表里那一格以外的段：带回全部输出。

**马上会看到：** 只读，什么都不改。

#### 第 6 步　回滚到 v3.2-extra（需要时才做）

**做什么、为什么：** 把面板二恢复成升级前备份的样子，数据不删。什么时候做：
- 第二部分换面板就不对的（T58 补丁还没换，vLLM 没有重启过）：马上做，不用停 vLLM。摘要里那条「要重启才用回旧的」`WARN` 这时不用管：vLLM 用的一直是旧的过滤器。
- T58 拉起以后要回滚的：只在第九部分开头的「回滚顺序」里做（第 5 条：三件补丁已恢复、Mooncake 已换回 v1 并移走 `logs/mooncake/`、第 42 步确认监控机的镜像里已经没有 `mc.*.log` 以后，拉起以前）。旧的过滤器要重启 vLLM 才用回，放在那次停机里，整个集群只停一次。

**机器：** 监控机。

**命令：** 用第 3 步摘要里「回到 v3.2-extra」那一行，再做一次看板分层。

```bash
python3 <用户目录>/vllm-pd-update-v3.3-extra/<更新脚本> rollback <家目录>/vllm-pd-update-backup/<时间> --cluster test-glm53
```

```bash
. ~/vllm-pd-test-glm53/vars.sh && python3 "$T45/arrange_boards.py" apply --env-from "$ENV2"
```

**应当看到：** 摘要第一行以「成功」结尾，第二行「和更新前一致，校验全部通过」；看板分层最后一行 `RESULT: OK`。实验室的输出：

```
vllm_pd_update_pack2_v33extra.py rollback <家目录>/vllm-pd-update-backup/<时间>，面板二 v3.3-extra，集群 test-glm53，成功
恢复 <家目录>/vllm-pd-update-backup/<时间>：和更新前一致，校验全部通过
OK   拉取程序先停下：已停止
INFO 数据：保留，不删（vllm_pd 的 7 张表，共 87485 行）
OK   节点文件：更新前的三个文件已下发（apply_segments.sh 里的 bin/nodes.sh push），segments.json 是 pd-diag-v3.2-extra-glm53-2026-11-20a@04d0c3ea
INFO 看板读回 v3.2-extra 的 config_id pd-diag-v3.2-extra-glm53-2026-11-20a：换版以前的行重新显示；更新以后写的行留在库里，不显示
OK   拉取程序用回 v3.2-extra 启动：已启动
WARN 过滤器文件回滚前后不同（pd_diag_logfilter.py）：各节点的 vLLM 要重启才用回旧的
日志：<家目录>/vllm-pd-update-logs/pack2-v33extra-test-glm53-rollback-<时间>.log
RESULT: OK（改了 1 项；回退：python3 arrange_boards.py undo <家目录>/vllm-pd-arrange-backup/<时间> --env-from <同一个 deploy.env>）
```

- 回滚以后，看板回到 v3.2-extra 的 config_id，换版以前的行重新显示；v3.3-extra 写的行留在库里，不显示。
- deploy.env 回到更新前的样子，用户加的几行都在。
- **P 机器上的 `/data/pd-diag/logs/mooncake/`：做这一步以前已经移走，不删。** 照回滚顺序，第 41 步的最后一条已经把这个目录移出节点日志目录（`mv /data/pd-diag/logs/mooncake /data/pd-diag/mooncake-rolled-back-<时间>`），第 42 步确认了监控机的镜像里已经没有 `mc.*.log`，然后才做这一步。第二部分换面板就回滚的，还没换过 v2，没有这个目录。
  - 为什么移走：v3.2-extra 的采集器不排除这个目录，留着时里面的文件每一行都报一个解析错误；实验室里留了一个 3 行的文件，回滚以后 dead-letter 多了 3 行，都来自它。回到旧库以后，也没有人再清理这些文件。
  - 为什么不删：回滚说明出了问题，这些计时行可能正好是查原因要用的。先留着，等看过、确认不需要了，再由用户删。大约多大：最重一档 12 小时约 31.5 GB。同一个文件系统里 `mv` 只是改名，瞬间完成，不占新的空间；目标在节点日志目录以外，拉取程序和采集器都碰不到。
  - 应当看到：`/data/pd-diag/logs/` 下不再有 `mooncake` 目录；拉取程序 `status` 正常，dead-letter 不再增长。

**不对时：** 带回全部输出。

## 第三部分　停集群以前：放进容器，检测，核对，备份（集群照常运行）

这一段的命令只读，或者只往 `/tmp`、备份目录里写，不影响运行中的 vLLM。有一个容器不对，就停在这里，集群照常运行。

#### 第 7 步　在监控机上核对补丁包，拷到两台机器

**机器：** 监控机。补丁包 `t58-glm53-patch-kit.tar`、Mooncake 增量包 `t58-glm53-mooncake-kit.tar` 和它们的 `.sha256` 放在 `<用户目录>/vllm-pd-patch-glm53/` 下。核对的两条执行一次；拷贝的两条每台机器执行一次，换 `<机器地址>`。Mooncake 状态是 `not_replaced` 的集群（第 9 步才知道；T56 升级时记下过）不用拷增量包。

**命令：**

```bash
cd <用户目录>/vllm-pd-patch-glm53 && LC_ALL=C sha256sum -c t58-glm53-patch-kit.tar.sha256
```

```bash
cd <用户目录>/vllm-pd-patch-glm53 && LC_ALL=C sha256sum -c t58-glm53-mooncake-kit.tar.sha256
```

```bash
scp <用户目录>/vllm-pd-patch-glm53/t58-glm53-patch-kit.tar <SSH 账号>@<机器地址>:/tmp/
```

```bash
scp <用户目录>/vllm-pd-patch-glm53/t58-glm53-mooncake-kit.tar <SSH 账号>@<机器地址>:/tmp/
```

**应当看到：** 前两条打印 `t58-glm53-patch-kit.tar: OK`、`t58-glm53-mooncake-kit.tar: OK`。拷贝几次都没有报错。

**不对时：** 不是 `OK`：包损坏，重新取一份，不往下做。

#### 第 8 步　拷进容器，解开，核对

**机器：** 每台机器：先在宿主机窗口执行前两条，再在容器窗口执行后三条。

**命令：**

```bash
docker cp /tmp/t58-glm53-patch-kit.tar <容器名>:/tmp/
```

```bash
docker cp /tmp/t58-glm53-mooncake-kit.tar <容器名>:/tmp/
```

```bash
mkdir -p /tmp/t58-kit && tar -xf /tmp/t58-glm53-patch-kit.tar -C /tmp/t58-kit && cd /tmp/t58-kit && LC_ALL=C sha256sum -c SHA256SUMS | grep -v ': OK$'; sha256sum /tmp/t58-kit/detect/t51_glm53_check.py | cut -c1-16
```

```bash
mkdir -p /tmp/t58-mc && tar -xf /tmp/t58-glm53-mooncake-kit.tar -C /tmp/t58-mc && cd /tmp/t58-mc && LC_ALL=C sha256sum -c SHA256SUMS | grep -v ': OK$'; sha256sum /tmp/t58-mc/lib/libmooncake_store.so | cut -c1-16
```

```bash
df -hT /tmp | tail -n 1
```

**应当看到：** 第三条只有一行 `b7b11e295a73ba91`（检测脚本 glm53-8 的 sha256 前 16 位）。第四条只有一行 `37a3aaf8acb53c6a`（替换库 v2 的 sha256 前 16 位）。第五条一行，第二列是文件系统类型，一般是 `overlay`；是 `tmpfs` 时，`/tmp` 下的文件在容器重启以后就没了，第 13、15 步会自动把备份放到 `/root` 下。

**不对时：** 第三、四条在那一行之前有输出：包在拷贝中损坏，重新拷贝。

**带回：** `df` 那一行（每个容器）。每台机器上的 `/tmp/t58-glm53-patch-kit.tar` 留着，回滚时要用。

#### 第 9 步　升级前的检测

**机器：** 容器窗口，集群照常运行（检测要读运行中的实例）。检测脚本 `/tmp/t58-kit/detect/t51_glm53_check.py`（glm53-8，sha256 `b7b11e295a73ba91…`）只读，几秒内结束，只往 `/tmp` 写一个明细文件；按 `nice 19`、IO 空闲级运行，读取量上限 1 GiB，耗时上限 300 秒。和 T56 用的 glm53-7 相比，它多认一版补丁（T58）和一版 Mooncake 替换库（v2），`run` 行末多一项 `diag_level=`。

**命令：** P 的容器：

```bash
python3 -I -S -B /tmp/t58-kit/detect/t51_glm53_check.py --run-dp-template <启动文件目录>/run_dp_template.sh --launch-online-dp <启动文件目录>/launch_online_dp.py --start-script <启动文件目录>/start_p.sh --mooncake-json <启动文件目录>/mooncake.json --start-mooncake <启动文件目录>/start_mooncake.sh --startup-log <容器里的 vLLM 日志文件>
```

D 的容器：

```bash
python3 -I -S -B /tmp/t58-kit/detect/t51_glm53_check.py --run-dp-template <启动文件目录>/run_dp_template.sh --launch-online-dp <启动文件目录>/launch_online_dp.py --start-script <启动文件目录>/start_d0.sh --mooncake-json <启动文件目录>/mooncake.json --start-mooncake <启动文件目录>/start_mooncake.sh --startup-log <容器里的 vLLM 日志文件>
```

带回以后删掉明细文件（路径照抄 `detail=` 那一行）：

```bash
rm -f <detail= 那一行的路径>
```

**应当看到：** 不超过 20 行，退出码 0。其中：

- `vllm=` 行 `state=v0.30.0+t56`，行末 `port=t51:21/25,t56:25/25,t58:18/25`；`ascend=` 行仍是 `state=modified`，行末 `port=t51:9/11,t56:11/11,t58:9/11`。另一版的数小一些，是两版之间不同的那几个文件，不是错。两行末尾都是 `from=site cands=1`。
- `site_patches` 一行和 T51 时相同：`patch/platform/patch_kv_load_failure_reason.py=d7c764bb425993c0 patch/platform/patch_prefill_length_validation.py=c36392a6338bc2f1`；D 上最后一项是 `absent`。
- **运行模式：** D 的 `inst[run …] kind=D` 一行是 `tp=1 dp=64 … eager=0 async=1`，16 个实例；P 的一行是 1 个实例。
- `run` 行里是 `logcfg=set`（T51 加的两行 `export` 还在），行末 `diag_level=3(default)`（启动脚本里没有设 `PD_DIAG_LEVEL`）。
- `verdict vllm=ok(t56) ascend=ok(t56,site_local=<n>) p2d=ok(t56) mooncake=<…>`。
- **Mooncake 两种状态：** 换过 v1 的是 `mooncake=ok(v1)`，`mooncake=` 那一行是 `store=replacement(v1) changed_since_install=1`；没换的是 `mooncake=not_replaced`。记进记录表：`ok(v1)` 的容器做第 14、15、21 步；`not_replaced` 的容器跳过这三步，第 25 步仍是 `not_replaced`。
- 最后一行 `fingerprint=… baseline_token=<一串>`：把 `baseline_token=` 后面的整串记进记录表，第 25 步要用。

**不对时：**

| 看到 | 怎么办 |
|---|---|
| `verdict` 的前三项不是 `ok(t56…)` | 停下，带回输出：T56 的状态和升级完时不同 |
| `run` 行末不是 `diag_level=3(default)` | 停下，带回这一行：启动脚本里设了 `PD_DIAG_LEVEL`，先去掉 |
| `inst[run …]` 的 `eager=`、`async=`、`tp=`、`dp=` 和上面不同 | 停下，带回这一行：运行模式和设计依据的不一样 |
| `site_patches` 的后两个和上面不同 | 停下，带回输出 |
| `mooncake=BAD(other)` | 带回这一行，照常往下做，Mooncake 那三步跳过 |
| 只打印一行 `BLOCKED`（自查认出输出里有 IP、主机名之类） | 带回这一行，不往下做 |
| 只打印一行 `REFUSED` | `-I -S -B` 少了，照原文重新执行 |

**带回：** 终端打印的全部行（每个容器），每个容器的 `baseline_token` 和 `mooncake=` 的值。

#### 第 10 步　找到安装目录

**机器：** 容器窗口，接着第 9 步。只查找位置，不导入 vLLM。

**命令：**

```bash
VLLM_PKG=$(python3 -c "import importlib.util as u; print(u.find_spec('vllm').submodule_search_locations[0])")
ASCEND_PKG=$(python3 -c "import importlib.util as u; print(u.find_spec('vllm_ascend').submodule_search_locations[0])")
VLLM_ROOT=$(dirname "$VLLM_PKG"); ASCEND_ROOT=$(dirname "$ASCEND_PKG"); F="$ASCEND_PKG/distributed/kv_transfer/kv_p2p/mooncake_connector.py"
echo "PYTHONPATH=[${PYTHONPATH:-}]"; echo "$VLLM_ROOT"; echo "$ASCEND_ROOT"; echo "$F"
```

**应当看到：** 四行。第一行是 `PYTHONPATH=[/data/pd-diag/conf]`（T51 加的 `export`）或者 `PYTHONPATH=[]`（容器窗口不是从启动脚本里开的）；第二、三行是同一个 `site-packages` 目录；第四行是它下面的 `vllm_ascend/distributed/kv_transfer/kv_p2p/mooncake_connector.py`。

**不对时：** 第二、三行不是同一个目录，或者第 9 步不是 `from=site cands=1`：停下，带回这四行和第 9 步那两行。

#### 第 11 步　核对要改的文件和现场的本地补丁

**机器：** 容器窗口，接着第 10 步。只读。

**命令：**

```bash
K=/tmp/t58-kit/patches
(cd "$VLLM_ROOT" && LC_ALL=C sha256sum -c $K/vllm/SHA256SUMS.t56 | grep -v ': OK$')
(cd "$ASCEND_ROOT" && LC_ALL=C sha256sum -c $K/vllm-ascend/SHA256SUMS.t56 | grep -v ': OK$'; LC_ALL=C sha256sum -c $K/vllm-ascend/SHA256SUMS.d4ad49796-unchanged | grep -v ': OK$')
(cd "$ASCEND_PKG/patch" && for f in __init__.py platform/__init__.py platform/patch_kv_load_failure_reason.py platform/patch_prefill_length_validation.py; do [ -e "$f" ] && echo "$f $(sha256sum "$f" | cut -c1-16)" || echo "$f 没有"; done)
```

**应当看到：** 前两段没有任何输出；最后四行是文件名和 sha256 前 16 位，和 T51 时相同（D 上最后一个文件 `没有`）。

- 第一段核的是 vLLM 的 25 个文件，和 T56 升级完以后的值逐个相同（`SHA256SUMS.t56`）。
- 第二段核的是 vllm-ascend 的 11 个文件，同样和 T56 升级完以后相同；另外核 `platform.py`、`utils.py` 和公开的 d4ad49796 相同（`SHA256SUMS.d4ad49796-unchanged`）。「是不是 D」用的是 `utils.py` 里的条件，这个条件又和 `platform.py` 换调度器的条件一样，所以这两个文件不能有本地改动。

**不对时：**

| 看到 | 怎么办 |
|---|---|
| 第一段或第二段的 `SHA256SUMS.t56` 有 `FAILED` | 停下，带回输出：文件和 T56 升级完时不同 |
| D 的容器：`platform.py` 或 `utils.py` 是 `FAILED` | 停下，带回输出：D 上「是不是 D」的判断依赖这两个文件 |
| P 的容器：`platform.py` 或 `utils.py` 是 `FAILED` | 带回输出，照常往下做：P 是 `kv_producer`，判断结果总是「不是 D」，不受这两个文件影响 |
| 后四行和 T51 时不同 | 停下，带回输出 |

#### 第 12 步　p2d：取出现场原文件，证明现场文件是 T56 重放出来的

**机器：** 容器窗口，接着第 11 步。只读，只往 `/tmp` 写。P、D 都要做。`<T51 的备份目录>` 填 T51 时记下的备份目录（T56 升级时第 6 步用的也是它）。

**命令：** 取出现场原文件并核对：

```bash
ORIG=/tmp/mooncake_connector.orig.py
tar -xOf <T51 的备份目录>/vllm-ascend.tar vllm_ascend/distributed/kv_transfer/kv_p2p/mooncake_connector.py > "$ORIG"
sha256sum "$ORIG" | cut -c1-16; sha256sum "$F" | cut -c1-16; grep -c '_pd_diag_level' "$F"
```

用 T56 版的 `replay_p2d.py` 在原文件上重放，结果应当就是现场当前的文件：

```bash
python3 -I $K/p2d/t56/replay_p2d.py "$ORIG" --out /tmp/p2d_t56_check.py; echo "exit=$?"; sha256sum /tmp/p2d_t56_check.py "$F" | cut -c1-16
```

**应当看到：**

- 第一条三行：`2c4acb3eeb1ced7d`（现场原文件）；T56 升级时记下的 `<S56>` 的前 16 位（现场当前的文件）；`0`（还没有 T58 的改动）。
- 第二条：`exit=0`，然后两行 sha256 前 16 位，两行相同。

**不对时：**

| 看到 | 怎么办 |
|---|---|
| 第一条报 `No such file or directory` | 容器里的 T51 备份没了：从监控机的 T51 备份 `<用户目录>/vllm-pd-patch-glm53/backup/<机器>-<T51 的时间戳>/` 拷回容器（照第 38 步的两条命令，换成 T51 的目录名），再执行 |
| 第一行不是 `2c4acb3eeb1ced7d` | 停下，带回：取出的不是现场原文件 |
| 第三行不是 `0` | 停下，带回：现场文件已经有 T58 的改动 |
| 第二条两行不同 | 停下，带回：现场文件不是 T56 重放出来的那一份 |

**带回：** 两条命令的完整输出（P、D 各一份）。

#### 第 13 步　备份 T56 版，拷回监控机（必做）

**机器：** 容器窗口执行第一条；然后在宿主机窗口执行第二条；最后在监控机上执行后两条（每台机器一次）。

**命令：**

```bash
BKBASE=/tmp; [ "$(stat -f -c %T /tmp)" = tmpfs ] && BKBASE=/root
BK=$BKBASE/t58-glm53-backup-$(date +%Y%m%d-%H%M%S); mkdir -p "$BK"; echo "$BK"
(cd "$VLLM_ROOT" && tar -cf "$BK/vllm.tar" $(awk '{print $2}' $K/vllm/SHA256SUMS.t56))
(cd "$ASCEND_ROOT" && tar -cf "$BK/vllm-ascend.tar" $(awk '{print $2}' $K/vllm-ascend/SHA256SUMS.t56) vllm_ascend/distributed/kv_transfer/kv_p2p/mooncake_connector.py)
(cd "$BK" && sha256sum vllm.tar vllm-ascend.tar > SHA256SUMS && tar -tf vllm.tar | wc -l && tar -tf vllm-ascend.tar | wc -l && awk '{print substr($1, 1, 16), $2}' SHA256SUMS)
```

```bash
docker cp <容器名>:<备份目录> /tmp/
```

```bash
mkdir -p <用户目录>/vllm-pd-patch-glm53/backup && scp -r <SSH 账号>@<机器地址>:/tmp/t58-glm53-backup-<时间戳> <用户目录>/vllm-pd-patch-glm53/backup/t58-<机器>-<时间戳>
```

```bash
cd <用户目录>/vllm-pd-patch-glm53/backup/t58-<机器>-<时间戳> && LC_ALL=C sha256sum -c SHA256SUMS && tar -tf vllm.tar | wc -l && tar -tf vllm-ascend.tar | wc -l
```

**应当看到：** 第一条打印备份目录名 `/tmp/t58-glm53-backup-<时间戳>`（`/tmp` 是 tmpfs 时在 `/root` 下），然后 `25`、`12` 和两个 tar 的 sha256 前 16 位。最后一条打印 `vllm.tar: OK`、`vllm-ascend.tar: OK`、`25`、`12`。把备份目录名记作 `<备份目录>`，末尾的 `<年月日>-<时分秒>` 记作 `<时间戳>`，记进记录表。

**不对时：** 数字不是 `25`、`12`，或者监控机上有 `FAILED`：重新拷；两台机器在监控机上都核对通过以前，不停集群。

**带回：** `<备份目录>`（每个容器）。备份是 T56 版的 25 个、11 个文件和 T56 重放出来的 `mooncake_connector.py`，回滚到 T56 用。

#### 第 14 步　Mooncake：换库前的核对（只在 `mooncake=ok(v1)` 的容器里）

**机器：** 容器窗口，vLLM 照常运行（核对要读运行中的 vLLM 加载了哪些库）。核对脚本 glm53mc-6（sha256 `559782ea1dfb29c1…`）只读，不导入 Mooncake，不写文件，自己降到 nice 19，60 秒超时。第 9 步是 `mooncake=not_replaced` 的容器跳过第 14、15 步和第 21 步。

**命令：** 核对：

```bash
python3 -I -S -B /tmp/t58-mc/precheck/dist/t51_mc_precheck.py; echo "exit=$?"
```

找到库，记下 vLLM 的 `LD_LIBRARY_PATH`（第 21 步的导入核对要用）：

```bash
MC_PKG=$(python3 -I -B -c "import importlib.util as u; print(list(u.find_spec('mooncake').submodule_search_locations)[0])"); MC_LIB="$MC_PKG/libmooncake_store.so"
echo "$MC_LIB"; ls -l "$MC_LIB" | awk '{print $1, $5}'; sha256sum "$MC_LIB" | cut -c1-16
```

```bash
: > /tmp/t58-mc/vllm.ld_library_path; for d in /proc/[0-9]*; do a=$(tr '\0' ' ' < "$d/cmdline" 2>/dev/null); case "$a" in *"vllm serve"*|*vllm.entrypoints.openai.api_server*) tr '\0' '\n' < "$d/environ" | grep '^LD_LIBRARY_PATH=' > /tmp/t58-mc/vllm.ld_library_path; break;; esac; done; echo "vLLM 的 LD_LIBRARY_PATH 已记下：$(wc -c < /tmp/t58-mc/vllm.ld_library_path) 字节"
```

**应当看到：**
- 第一条不超过 20 行，然后 `exit=0`。要看的几行：
  - 第一行 `t51_mc_precheck=glm53mc-6 build=glm53mc-6 pid=<vLLM 的进程号> loaded=<n>`；
  - `mc_orig` 一行里是 `f1=replacement_v1`，`size=15402248`；
  - `mc_build` 一行最后是 `result=skipped_upgrade`；
  - `mc_exports` 一行里是 `replacement_vs_site=-0/+0(expected -0/+0)`；
  - `consumers_ok`；
  - 最后一行 `mc_pre result=ok_upgrade`。
- 第二条三行：`<Mooncake 目录>/libmooncake_store.so`；权限和大小 `15402248`；`3131784385846064`（v1 的 sha256 前 16 位）。
- 第三条一行 `vLLM 的 LD_LIBRARY_PATH 已记下：<n> 字节`。n 是 0 也照常往下做：vLLM 进程没有设它，导入核对就用当前 shell 的环境。

**不对时：**

| 看到 | 怎么办 |
|---|---|
| 最后一行是 `STOP_…` | 这个容器不换 Mooncake：跳过第 15、21 步，带回全部输出；补丁照常升级。是 P 的容器时，新面板上 P 后台 put 下 3 段为空（第 32 步表里「P 上没有 Mooncake 文件」那一列） |
| `STOP_incomplete` | vLLM 没在运行：等它起来再跑一次 |
| 只打印一行 `REFUSED` | `-I -S -B` 少了，照原文重新执行 |
| 第二条的前 16 位不是 `3131784385846064` | 停下，带回：现在的库不是 v1 |

**带回：** 三条的全部输出（每个容器）。

#### 第 15 步　Mooncake：备份现在的 v1，拷回监控机（换 v2 以前必做）

**机器：** 容器窗口执行第一条；然后在宿主机窗口执行第二条；最后在监控机上执行后两条（每台机器一次）。回滚到 v1 用的就是这份备份，不从 v1 的增量包重装。

**命令：**

```bash
BKBASE=/tmp; [ "$(stat -f -c %T /tmp)" = tmpfs ] && BKBASE=/root
BKMC=$BKBASE/t58-glm53-mc-backup-$(date +%Y%m%d-%H%M%S); mkdir -p "$BKMC"; echo "$BKMC"
cp -p "$MC_LIB" "$BKMC/libmooncake_store.so" && (cd "$BKMC" && sha256sum libmooncake_store.so > SHA256SUMS && cut -c1-16 SHA256SUMS)
```

```bash
docker cp <容器名>:<MC 备份目录> /tmp/
```

```bash
mkdir -p <用户目录>/vllm-pd-patch-glm53/backup && scp -r <SSH 账号>@<机器地址>:/tmp/t58-glm53-mc-backup-<MC 时间戳> <用户目录>/vllm-pd-patch-glm53/backup/t58-<机器>-mc-<MC 时间戳>
```

```bash
cd <用户目录>/vllm-pd-patch-glm53/backup/t58-<机器>-mc-<MC 时间戳> && LC_ALL=C sha256sum -c SHA256SUMS
```

**应当看到：** 第一条打印 `/tmp/t58-glm53-mc-backup-<MC 时间戳>`（`/tmp` 是 tmpfs 时在 `/root` 下），然后一行 `3131784385846064  libmooncake_store.so`。最后一条打印 `libmooncake_store.so: OK`。把这个目录记作 `<MC 备份目录>`，末尾的 `<年月日>-<时分秒>` 记作 `<MC 时间戳>`，记进记录表。

**不对时：** 前 16 位不是 `3131784385846064`，或者监控机上有 `FAILED`：重新拷；核对通过以前不停集群。

#### 第 16 步　p2d：用 T58 版重放，写到 `/tmp`

**机器：** 容器窗口，接着第 13 步。P、D 都要做。`replay_p2d.py`（T58）的 sha256 是 `e62795bd63b146eb6813865d039d574d1fff067e1654f56f02a4458ca5e4a9e7`。这一步不动现场文件：结果只写到 `/tmp`，停集群以后第 20 步才换上。

**命令：** 只检查、不写文件：

```bash
python3 -I $K/p2d/replay_p2d.py "$ORIG" --check; echo "exit=$?"
```

把重放的结果写到 `/tmp`：

```bash
python3 -I $K/p2d/replay_p2d.py "$ORIG" --out /tmp/mooncake_connector.p2d58.py; echo "exit=$?"
```

**应当看到：**

- 第一条最后两行是 `replay_p2d: check ok: 29 of 29 changes located, result checks passed, nothing written` 和 `exit=0`；29 行定位结果里只有 C25 是 `alternative_2`，其余都是 `primary`，和 T56 时相同（T58 的锚点、判据、改动编号都和 T56 版相同，只改补丁自己插进去的代码）。
- 第二条最后两行：`replay_p2d: written out=/tmp/mooncake_connector.p2d58.py sha256=<S58> changes=29/29 py_compile=ok same_as_replacement=False` 和 `exit=0`。把 `<S58>` 的前 16 位记进记录表：P、D 的 `<S58>` 必须相同。

**不对时：**

| 看到 | 怎么办 |
|---|---|
| 别的编号也用了备选锚点，或者 C25 回到 `primary` | 停下，带回输出：现场文件和小核对时不同 |
| P、D 的 `<S58>` 不同 | 停下，带回 |
| 退出码 1 | 参数错，读不了输入，或输入不是合法的 Python：核对路径和文件 |
| 退出码 2、3、4 | 不写文件，带回完整输出 |
| 退出码 5 | 写出的文件 `py_compile` 不通过：不要用 `/tmp` 下的文件，带回输出 |
| 退出码 6 | 输入不对：给的不是第 12 步取出的原文件 `$ORIG`。看最后一行的说明 |

退出码不为 0 时现场文件没有被动过。不要手工修改 `replay_p2d.py` 或现场文件去绕过检查。

**带回：** 两条命令的完整输出和 `<S58>`（P、D 各一份）。

#### 第 17 步　停集群以前：记下命令行和磁盘

**机器：** 第一条在容器窗口（集群还在运行）；第二条在宿主机窗口。`BK` 填 `<备份目录>`。

**命令：**

```bash
BK=<备份目录>
for d in /proc/[0-9]*; do a=$(tr '\0' ' ' < "$d/cmdline" 2>/dev/null); case "$a" in *"vllm serve"*|*vllm.entrypoints.openai.api_server*) echo "$a";; esac; done | LC_ALL=C sort | uniq -c > "$BK/cmdline.before"; cat "$BK/cmdline.before"
```

```bash
df -hT <节点目录> <vLLM 日志文件> | uniq
```

**应当看到：** 第一条每种命令行一行，前面的数是用这一行的进程个数；D 的容器里每个实例一种（端口各不相同），P 的容器里一种。第二条：表头一行，然后节点目录、vLLM 日志所在的文件系统各一行（两者在同一个文件系统上时只有一行）。

**带回：** 两条的输出（每个容器、每台机器）。

## 第四部分　停整个集群，换文件，拉起（停机）

**顺序：** 第 18 步停整个集群。然后先在 D 的容器里做完第 19、20、21 步和第 22 步的导入核对；导入核对通过以后，P 的容器再做第 19、20、21 步。两个容器都做完，第 23 步一起拉起。

#### 第 18 步　停整个集群

**机器：** 按平时停集群的方式停；停完以后在每个容器窗口里核对。

**怎么停：** 现场容器的 1 号进程是 `bash`，vLLM 由 `launch_online_dp.py` 拉起。按平时的方式停掉两个容器里的全部 vLLM 实例。停的是容器里的 vLLM，不要 `docker rm` 容器。记下停的时刻（第 33 步用）。

**命令：** 容器里还有没有 vLLM 进程（每个容器窗口）：

```bash
for d in /proc/[0-9]*; do a=$(tr '\0' ' ' < "$d/cmdline" 2>/dev/null); case "$a" in *"vllm serve"*|*vllm.entrypoints.openai.api_server*) tr '\0' '\n' < "$d/environ" | grep -q '^VLLM_LOGGING_CONFIG_PATH=.*/pd_diag_logging\.json$' && echo "已设" || echo "没有设：pid ${d#/proc/}";; esac; done | sort | uniq -c
```

**应当看到：** 没有输出：这个容器里已经没有 vLLM 进程。这条命令要在容器窗口里直接执行；用 `bash -c '…'` 包起来执行时，执行它的那个 shell 的命令行里也有 `vllm serve`，会被当成 vLLM 进程列出来。

**不对时：**

| 看到 | 怎么办 |
|---|---|
| 有 `已设` 或 `没有设` 的行 | 还有 vLLM 进程没停：按平时的方式停掉，再执行一次 |
| 容器窗口断开了 | 容器起来以后重新进入容器窗口，按第 19 步「不对时」第一行重新设变量；`/tmp` 是 `tmpfs` 的，先重做第 8 步的前四条和第 12 步的第一条、第 16 步的第二条（写到 `/tmp` 的文件没了），`<S58>` 要和记录表相同 |

#### 第 19 步　换上 vLLM 和 vllm-ascend 的 T58 文件

**机器：** 容器窗口。

**命令：** 先确认前面几步设的变量都在：

```bash
: "${VLLM_PKG:?}" "${ASCEND_PKG:?}" "${VLLM_ROOT:?}" "${ASCEND_ROOT:?}" "${F:?}" "${K:?}" "${BK:?}" && echo "变量都在：$BK"
```

```bash
cp -R $K/vllm/files/vllm/. "$VLLM_PKG"/ && (cd "$VLLM_ROOT" && LC_ALL=C sha256sum -c $K/vllm/SHA256SUMS.after | grep -v ': OK$')
```

```bash
cp -R $K/vllm-ascend/files/vllm_ascend/. "$ASCEND_PKG"/ && (cd "$ASCEND_ROOT" && LC_ALL=C sha256sum -c $K/vllm-ascend/SHA256SUMS.after | grep -v ': OK$')
```

**应当看到：** 第一条打印 `变量都在：<备份目录>`；后两条没有任何输出（25 个、11 个文件都和 T58 的 `SHA256SUMS.after` 一致）。T58 和 T56 改的是同一组文件（vLLM 25 个、vllm-ascend 11 个，文件名逐个相同），不新增、不删文件，所以不用清编译缓存：改过的 `.py` 时间变了，Python 会自己重新编译。

**不对时：**

| 看到 | 怎么办 |
|---|---|
| 第一条报 `parameter null or not set` | 换过 shell：按第 10 步重新找目录，再设 `K=/tmp/t58-kit/patches` 和 `BK=<备份目录>`（不要重做备份），然后再执行 |
| 后两条有 `FAILED` | 停下，已经换过文件的容器按第九部分回滚 |

#### 第 20 步　换上 p2d 的结果

**机器：** 容器窗口，接着第 19 步。

**命令：**

```bash
cp -p "$F" "$F.p2d_t56" && cp /tmp/mooncake_connector.p2d58.py "$F" && sha256sum "$F" | cut -c1-16
```

**应当看到：** `<S58>` 的前 16 位，和记录表里这个容器的相同。T56 版留在 `"$F.p2d_t56"`，回滚时第九部分用的是备份，不用它。

**不对时：** 报 `No such file or directory`：`/tmp` 下的文件没了，重做第 16 步的第二条再执行。前 16 位和记录表不同：停下，带回。

#### 第 21 步　换上 Mooncake v2（只在第 14 步是 `ok_upgrade` 的容器里）

**机器：** 容器窗口，接着第 20 步。集群停着，没有进程在用这个库。

**命令：** 先确认第 14、15 步设的变量都在：

```bash
: "${MC_PKG:?}" "${MC_LIB:?}" "${BKMC:?}" && echo "变量都在：$BKMC"
```

换库（先拷成同目录下的临时文件，再改名换上，权限和属主照原库）：

```bash
cp /tmp/t58-mc/lib/libmooncake_store.so "$MC_PKG/.t58_new_store.so" && chmod --reference="$MC_LIB" "$MC_PKG/.t58_new_store.so" && chown --reference="$MC_LIB" "$MC_PKG/.t58_new_store.so" && mv -f "$MC_PKG/.t58_new_store.so" "$MC_LIB" && sha256sum "$MC_LIB" | cut -c1-16 && ls -l "$MC_LIB" | awk '{print $1, $5}'
```

导入核对（用 vLLM 的 `LD_LIBRARY_PATH`）：

```bash
(L=$(cat /tmp/t58-mc/vllm.ld_library_path); [ -n "$L" ] && export "$L"; python3 -B -c "import time; t = time.time(); import mooncake.store as s; print(s.__file__, '%.1f s' % (time.time() - t))")
```

**应当看到：**
- 第一条打印 `变量都在：<MC 备份目录>`。
- 第二条：`37a3aaf8acb53c6a`，然后一行权限和大小：权限和第 14 步相同，大小是 `15402392`。
- 第三条一行 `<Mooncake 目录>/store.cpython-311-aarch64-linux-gnu.so <用时> s`。

**不对时：**

| 看到 | 怎么办 |
|---|---|
| 第一条报 `parameter null or not set` | 换过 shell：照第 14 步第二条重新设 `MC_PKG`、`MC_LIB`，再设 `BKMC=<MC 备份目录>`（不要重做备份），然后再执行 |
| 第二条的前 16 位不是上面的值 | 停下，按第 41 步换回 v1 |
| 第三条报 `undefined symbol` 或别的错 | 按第 41 步换回 v1，补丁照常往下做，带回这一行。是 P 的容器时，新面板上 P 后台 put 下 3 段为空（第 32 步表里「P 上没有 Mooncake 文件」那一列） |

#### 第 22 步　导入核对（只在 D 的容器里）

**机器：** 容器窗口。只在 D 的容器里、第 19、20、21 步做完以后执行一次。

**命令：**

```bash
python3 -B -c "import time, resource; t = time.time(); import vllm_ascend.distributed.kv_transfer.kv_p2p.mooncake_connector as m; print(hasattr(m.KVCacheRecvingThread, '_transfer_batch'), '%.1f s' % (time.time() - t), '%d MiB' % (resource.getrusage(resource.RUSAGE_SELF).ru_maxrss // 1024))"
```

**应当看到：** 一行 `True <用时> s <峰值内存> MiB`。这条命令导入 torch、torch_npu、vLLM 和这个模块用到的部分，导入完就退出，不加载模型，不开端口。

**不对时：** 不是 `True`，或者报错：P 的容器不再换文件；D 的容器按第九部分回滚（P 没有换过文件，不用回滚），再拉起整个集群，带回这一行。

**带回：** 这一行。

#### 第 23 步　拉起整个集群

**机器：** 按平时拉起集群的方式；拉起完成以后在每个容器窗口里核对。

**怎么起：** 两个容器都做完第 19、20、21 步以后，用原来的启动文件拉起全部实例（P、D 各用自己的启动文件，和第 9 步检测时传的相同）。参数和停之前相同；不加任何新的环境变量（T51 的两行 `export` 已经在启动脚本里；不加 `PD_DIAG_LEVEL`）。记下拉起的时刻（第 33 步用）。

**命令：** 每个实例都起来了：

```bash
for p in $(seq 9100 9115); do printf "%s " "$p"; curl -s -o /dev/null -w '%{http_code}\n' localhost:$p/health; done
```

```bash
for p in 9900; do printf "%s " "$p"; curl -s -o /dev/null -w '%{http_code}\n' localhost:$p/health; done
```

**应当看到：** 每个端口一行，都是 `200`（D 的容器 16 行，P 的容器 1 行）。

**不对时：** 有实例的 `/health` 不是 `200`，或者起不来：这次升级失败，按第 34 步处理。

## 第五部分　拉起以后的核对（全部实例）

#### 第 24 步　拉起以后，在容器里核对

**机器：** 命令行、日志配置两项在容器里；最后一条在那台机器上，宿主机里。

**命令：** 命令行和停机前相同（`BK` 填 `<备份目录>`）：

```bash
BK=<备份目录>
for d in /proc/[0-9]*; do a=$(tr '\0' ' ' < "$d/cmdline" 2>/dev/null); case "$a" in *"vllm serve"*|*vllm.entrypoints.openai.api_server*) echo "$a";; esac; done | LC_ALL=C sort | uniq -c > "$BK/cmdline.after"; diff "$BK/cmdline.before" "$BK/cmdline.after" && echo "命令行和重启前相同"
```

面板二的日志配置在每个 vLLM 进程上都生效了：

```bash
for d in /proc/[0-9]*; do a=$(tr '\0' ' ' < "$d/cmdline" 2>/dev/null); case "$a" in *"vllm serve"*|*vllm.entrypoints.openai.api_server*) tr '\0' '\n' < "$d/environ" | grep -q '^VLLM_LOGGING_CONFIG_PATH=.*/pd_diag_logging\.json$' && echo "已设" || echo "没有设：pid ${d#/proc/}";; esac; done | sort | uniq -c
```

vLLM 日志里没有本套补丁引起的错误：

```bash
grep -nE -A12 "ImportError|Traceback" <vLLM 日志文件> | grep -E "ImportError|Traceback|pd_diagnostics|pd_device_timing|worker_base|ascend_store|mooncake_connector|recompute_scheduler" | head -20
```

**应当看到：** `命令行和重启前相同`；只有一行 `<进程个数> 已设`；最后一条没有输出。

**不对时：** 有 `diff` 的输出、有「没有设」的行、或者最后一条有输出：这次升级失败，按第 34 步处理，带回输出。

#### 第 25 步　用检测脚本核对

**机器：** 容器窗口，第 24 步通过以后。

**命令：** P 的容器：

```bash
python3 -I -S -B /tmp/t58-kit/detect/t51_glm53_check.py --run-dp-template <启动文件目录>/run_dp_template.sh --launch-online-dp <启动文件目录>/launch_online_dp.py --start-script <启动文件目录>/start_p.sh --mooncake-json <启动文件目录>/mooncake.json --start-mooncake <启动文件目录>/start_mooncake.sh --startup-log <容器里的 vLLM 日志文件> --baseline <升级前检测的 baseline_token>
```

D 的容器：

```bash
python3 -I -S -B /tmp/t58-kit/detect/t51_glm53_check.py --run-dp-template <启动文件目录>/run_dp_template.sh --launch-online-dp <启动文件目录>/launch_online_dp.py --start-script <启动文件目录>/start_d0.sh --mooncake-json <启动文件目录>/mooncake.json --start-mooncake <启动文件目录>/start_mooncake.sh --startup-log <容器里的 vLLM 日志文件> --baseline <升级前检测的 baseline_token>
```

带回以后照第 9 步删掉明细文件。

**应当看到：**

- `verdict vllm=ok(t58) ascend=ok(t58,site_local=<n>) p2d=ok(t58) mooncake=ok(v2)`，P、D 都是（第 9 步是 `not_replaced` 的容器仍是 `mooncake=not_replaced`）。**前三项都要是 `t58`。**
- `vllm=` 行 `state=v0.30.0+t58`，行末 `port=t51:17/25,t56:18/25,t58:25/25`；`ascend=` 行行末 `port=t51:8/11,t56:9/11,t58:11/11`。
- `mooncake=` 那一行是 `store=replacement(v2) changed_since_install=1`。
- `strict` 行末是 `p2d=ok(t58,changes=29/29,missing=none,struct=ok,extra=same,replacement_file=0)`。
- `site_patches` 一行和第 9 步相同；`run` 行里是 `logcfg=set`，行末 `diag_level=3(default)`；单独一行 `baseline=same`。

**不对时：**

| 看到 | 怎么办 |
|---|---|
| 三项里有 `ok(t56)`，例如 `vllm=ok(t58) ascend=ok(t56)` | 只升了一半：这次升级失败，按第 34 步处理 |
| 任何一项是 `BAD(…)` 或 `unpatched` | 这次升级失败，带回全部输出 |
| 换了 v2 的容器 `mooncake=` 不是 `ok(v2)`；没换的容器和第 9 步不同 | 停下，带回 |
| `run` 行末不是 `diag_level=3(default)` | 停下，带回：启动环境里有 `PD_DIAG_LEVEL` |
| 有 `BASELINE_DIFF` | 配置变了：停下，带回这一行 |

#### 第 26 步　启动行（不对就停）

**机器：** 那台机器上，宿主机里。`device_timing` 那一行要在第一个真实请求执行以后才有：先发几个请求。

**命令：**

```bash
grep -h 'PD_DIAG_STARTUP' <vLLM 日志文件> | sed 's/.*PD_DIAG_STARTUP //' | LC_ALL=C sort | uniq -c
```

**应当看到：** 每种行一行，前面是出现的次数。glm53-test 是 P 1 台（1 个实例，TP16）、D 1 台（16 个实例，TP1）。和 T56 相比只多一种行 `diag level=3 source=default`：每个 EngineCore 进程在调度器那一行后面写一次，每个 Worker 在 model runner 那一行后面写一次。所以 P 是 `17`（1 个 EngineCore 加 16 个 Worker），D 是 `32`（16 个实例，每个 EngineCore、Worker 各一次；D 是 TP1，Worker 在 EngineCore 的进程里，同一个进程写两行）：

- **D：**
  - EngineCore 每个实例一行：`scheduler=vllm_ascend.core.recompute_scheduler.AsyncRecomputeScheduler balance=False recompute=True dyntra_lb=False batch_job=False profiling_chunk=False`；
  - Worker 每个实例两行：`worker model_runner=vllm_ascend.worker.model_runner_v1.NPUModelRunner tp_rank=0`，以及 `worker device_timing=on tp_rank=0 runner_stages=forward,sampling,prepare_inputs,lm_head,draft,bookkeeping source=default`。
  - **数一下：** 带 `runner_stages=` 的 `tp_rank=0` 那一行前面是 `16`，`scheduler=…AsyncRecomputeScheduler` 那一行前面也是 `16`（这台机器的 D 实例数）。
- **P：**
  - EngineCore：`scheduler=vllm_ascend.patch.platform.patch_balance_schedule.BalanceScheduler balance=False recompute=False dyntra_lb=False batch_job=False profiling_chunk=False`；
  - Worker 每个 rank 两行，和 T51 时逐字相同：`worker device_timing=on tp_rank=0` 1 行、`off` 15 行（`tp_rank=1`…`15`），**没有** `runner_stages=`。
  - **数一下：** `scheduler=…BalanceScheduler` 1 行，`device_timing=on tp_rank=0` 1 行，`off` 15 行。
- **P、D 都有：** `diag level=3 source=default`，P 前面是 `17`，D 前面是 `32`。日志是追加写的时候，每拉起一次多一份：第 36 步判成 `SLOWER`、改了环境变量再拉起一次以后，这里是 `34`、`64`。
- **新的一行：** `diag level=3 source=default`。`source=env` 表示启动环境里设了 `PD_DIAG_LEVEL=3`，也对；`level=1`、`level=2` 或 `source=invalid` 表示设了别的值，停下，从启动脚本里去掉它，再整体拉起一次。
- **日志是追加写的时候，** 文件里同时有升级前（T51、T56 时期）的旧行：P、D 上各种行的次数会多出升级前的那几份，`diag level=` 的行只有这次拉起以后才有。**旧行不算**：只看带 `runner_stages=` 的行数够不够、`diag level=3` 的行，和这次拉起以后新出现的行。

**这一步不对就停（不能只记下来）：** D 机器上带 `runner_stages=` 的 `tp_rank=0` 行数少于 D 实例数（16），或者列的不是那 6 个 stage，或者 D 的 EngineCore 不是上面那个调度器，就停下，按第九部分回滚，带回这几行。原因：「是不是 D」取不到配置时会悄悄退回 T51 的行为，不报错，这两行是唯一当场看得出的地方。

| 看到 | 是什么 |
|---|---|
| D 机器上带 `runner_stages=` 的行数少于 D 实例数（16） | 有的 D 被新代码判成了「不是 D」，或者三件补丁的版本不配套。只有不带 `runner_stages=` 的旧行、带它的行数够 16 的，不是失败 |
| D 的那一行是 `runner_stages=forward,sampling source=env` | 有人设了 `VLLM_PD_DEVICE_STAGES=none`：从 D 的启动脚本里去掉它，照第 18 步停整个集群、第 23 步拉起，再看这一步 |
| P 的那一行有 `runner_stages=` | P 被判成了 D：停下，带回 |
| `device_timing` 那一行还没出现 | 还没执行过真实请求，等有请求以后再看 |
| 没有 `diag level=` 的行 | 新代码没有生效：停下，带回这几行和第 25 步的输出 |

#### 第 27 步　Mooncake 的记录写进了自己的文件（P 的机器，拉起并有请求以后）

**机器：** P 的机器上，宿主机里（`<节点目录>` 在容器里也是同一个目录）。D 不往缓存池保存时没有 Mooncake 文件，D 的机器只做最后两条。第 21 步没做的机器（`not_replaced`）只做最后两条。

**命令：**

```bash
ls -l <节点目录>/logs/mooncake/ | tail -5; cat <节点目录>/logs/mooncake/mc.*.log | grep -c '"event":"mc_batch_put"'; head -1 $(ls -t <节点目录>/logs/mooncake/mc.*.log | head -1) | grep -o '"event":"pd_file_open".*"build":"[^"]*"'
```

```bash
grep -nE "undefined symbol|libmooncake_store" <vLLM 日志文件> | head -20
```

```bash
for f in <vLLM 日志文件>; do echo "$f $(grep -c '\[BatchPutTiming\]' "$f") $(grep -c 'mooncake timing records dropped' "$f")"; done
```

**应当看到：**
- 第一条：几行 `mc.<启动时刻>.<pid>.<序号>.log`（每个用 Mooncake 保存的 vLLM 进程一个文件，序号从 `0000` 起），然后一个数（计时记录的条数，有请求以后大于 0），最后一行 `"event":"pd_file_open"… "build":"timing-files-1"`。
- 第二条没有输出。
- 第三条每个 vLLM 日志文件一行：文件名、`[BatchPutTiming]` 行数、`mooncake timing records dropped` 行数。两个数在拉起以后都不再增长。日志是追加写的，第一个数含升级前 v1 写的旧行，看一次分不清：隔几分钟（期间有请求）再执行一次第三条，两次的数相同才是不再增长。也可以以第 31 步磁盘那一行为准：它按起点以后的增量算，`[BatchPutTiming]` 一项应当是 `0.00`。

**不对时：**

| 看到 | 怎么办 |
|---|---|
| `logs/mooncake/` 不存在或没有文件，有请求已经几分钟 | 带回第一条的输出和第 25 步的输出；补丁不回滚，P 后台 put 下 3 段为空 |
| 第二条有输出 | 这次升级失败（第 34 步） |
| 第三条的第二个数大于 0 | 目录建不了或文件打不开，记录被丢掉：带回这一行和第一条的输出，查节点目录的权限和空间 |
| 第三条执行两次，第一个数变大了 | 换上的还是 v1：带回第 21 步和第 25 步的输出 |

**带回：** 三条的输出。

#### 第 28 步　监控机：过滤器自检（拉起 5 分钟以后）

**机器：** 监控机。

**命令：**

```bash
cd <总目录>/vllm-pd-grafana-panel2/test-glm53 && bash -c '. bin/lib.sh && load_env >/dev/null && curl -s -u "vllm_reader:$CH_READER_PASSWORD" "http://localhost:$CLICKHOUSE_HTTP_PORT/" --data-binary @-' <<'SQL'
SELECT instance, sum(m) AS cand_maps, sum(f) AS forward_empty, sum(sa) AS sampling_empty, sum(e) AS no_execution_id FROM (SELECT instance, argMax(cand_maps, ts) AS m, argMax(acc_fallback['forward'], ts) AS f, argMax(acc_fallback['sampling'], ts) AS sa, argMax(exec_missing, ts) AS e FROM vllm_pd.filter_selfcheck WHERE ts > now() - INTERVAL 10 MINUTE GROUP BY instance, pid) GROUP BY instance ORDER BY instance FORMAT PrettyCompactMonoBlock
SQL
```

**应当看到：** 每个实例一行。D 的实例 `cand_maps` 大于 0；`no_execution_id` 是 0。`forward_empty`、`sampling_empty` 是没有设备时间、看板上留空的步数，按第 29、31 步判。

**不对时：** D 的实例 `cand_maps` 是 0，或者 `no_execution_id` 大于 0：这次升级失败。

**带回：** 这条查询的输出。

#### 第 29 步　监控机：留空比例第一次（拉起 10 分钟以后）

**机器：** 监控机。只记录、不判。整个集群一起拉起，所以所有实例都算这一次（命令里已经填成全部实例）。

**命令：**

```bash
cd <总目录>/vllm-pd-grafana-panel2/test-glm53 && bash -c '. bin/lib.sh && load_env >/dev/null && curl -s -u "vllm_reader:$CH_READER_PASSWORD" "http://localhost:$CLICKHOUSE_HTTP_PORT/" --data-binary @-' <<'SQL'
WITH now64(3, 'UTC') AS t10, cur AS (SELECT DISTINCT instance, pid FROM vllm_pd.filter_selfcheck WHERE ts > now() - INTERVAL 10 MINUTE), done AS (SELECT DISTINCT instance, request_id FROM vllm_pd.phase_line_cur WHERE proc = 'api' AND api_end_ts < now() - INTERVAL 1 MINUTE), ew AS (SELECT instance, uniqExact(request_id) AS ne FROM vllm_pd.phase_line_cur WHERE proc = 'api' AND api_end_ts >= t10 AND api_end_ts < now() - INTERVAL 1 MINUTE GROUP BY instance), emp AS (SELECT instance, arraySort(groupUniqArray(pid)) AS pl, length(pl) AS np, sum(f) AS fe, sum(sa) AS se FROM (SELECT instance, pid, argMax(acc_fallback['forward'], ts) AS f, argMax(acc_fallback['sampling'], ts) AS sa FROM vllm_pd.filter_selfcheck WHERE ts > now() - INTERVAL 10 MINUTE GROUP BY instance, pid) GROUP BY instance) SELECT i.instance AS instance, match(i.instance, '^(.+)$') AS this_batch, toString(t10) AS t10_utc, dateDiff('minute', t10, now()) AS window_min, ew.ne AS ended_win, emp.np AS pids, emp.pl AS pid_list, emp.fe AS forward_empty, emp.se AS sampling_empty, x.a_tot AS alloc_total, x.f_tot AS forward_total, x.s_tot AS sampling_total, x.a_win AS alloc_win, x.f_win AS forward_win, x.s_win AS sampling_win, if(x.a_win = 0, NULL, round(100 * (1 - x.f_win / x.a_win), 2)) AS forward_empty_pct, if(x.a_win = 0, NULL, round(100 * (1 - x.s_win / x.a_win), 2)) AS sampling_empty_pct FROM (SELECT DISTINCT instance FROM done) AS i LEFT JOIN (SELECT instance, sum(al) AS a_tot, sum(fw) AS f_tot, sum(sm) AS s_tot, sum(al - al0) AS a_win, sum(fw - fw0) AS f_win, sum(sm - sm0) AS s_win FROM (SELECT l.instance AS instance, l.request_id AS request_id, l.part AS part, toUInt32OrZero(splitByChar('-', l.part)[2]) AS pid, ifNull(max(l.acc__forward__steps), 0) AS fw, ifNull(maxIf(l.acc__forward__steps, l.ts < t10), 0) AS fw0, ifNull(max(l.acc__sampling__steps), 0) AS sm, ifNull(maxIf(l.acc__sampling__steps, l.ts < t10), 0) AS sm0, ifNull(max(l.acc__alloc__steps), 0) AS al, ifNull(maxIf(l.acc__alloc__steps, l.ts < t10), 0) AS al0 FROM vllm_pd.phase_line_cur AS l WHERE l.part != '' AND (l.instance, l.request_id) IN (SELECT instance, request_id FROM done) GROUP BY l.instance, l.request_id, l.part) WHERE (instance, pid) IN (SELECT instance, pid FROM cur) GROUP BY instance) AS x ON i.instance = x.instance LEFT JOIN emp ON i.instance = emp.instance LEFT JOIN ew ON i.instance = ew.instance ORDER BY this_batch DESC, instance SETTINGS join_use_nulls = 0 FORMAT PrettyCompactMonoBlock
SQL
```

**应当看到：** 每个实例一行，列含义见第 31 步。第一次执行时 `ended_win`、`alloc_win`、`forward_win`、`sampling_win` 是 0，两个比例为空（`ᴺᵁᴸᴸ`）。

**带回：** 整份输出。`t10_utc` 照抄下来（带毫秒），第 31 步要用；每个实例的 `pid_list` 也照抄。

#### 第 30 步　磁盘起点（拉起 5 分钟以后，和第 28 步同时）

**机器：** 每台机器上，宿主机里。

**命令：**

```bash
N=<节点目录>/logs
{ date +%s; du -sb "$N" | cut -f1; for f in <vLLM 日志文件>; do echo "$f $(stat -c %s "$f")"; done; } > ~/t51-glm53-disk.t0 && echo "起点已记下：$(wc -l < ~/t51-glm53-disk.t0) 行"
```

**应当看到：** `起点已记下：<n> 行`，n 是 vLLM 日志文件的个数加 2（D 机器 18，P 机器 3）。

#### 第 31 步　监控机和机器：留空比例第二次、磁盘第二次（拉起约 35 分钟以后）

**机器：** 留空比例在监控机上；磁盘在每台机器上，宿主机里。

**命令：** 留空比例，把 `<第一次的 t10_utc>` 换成第 29 步打印的那个时刻：

```bash
cd <总目录>/vllm-pd-grafana-panel2/test-glm53 && bash -c '. bin/lib.sh && load_env >/dev/null && curl -s -u "vllm_reader:$CH_READER_PASSWORD" "http://localhost:$CLICKHOUSE_HTTP_PORT/" --data-binary @-' <<'SQL'
WITH toDateTime64('<第一次的 t10_utc>', 3, 'UTC') AS t10, cur AS (SELECT DISTINCT instance, pid FROM vllm_pd.filter_selfcheck WHERE ts > now() - INTERVAL 10 MINUTE), done AS (SELECT DISTINCT instance, request_id FROM vllm_pd.phase_line_cur WHERE proc = 'api' AND api_end_ts < now() - INTERVAL 1 MINUTE), ew AS (SELECT instance, uniqExact(request_id) AS ne FROM vllm_pd.phase_line_cur WHERE proc = 'api' AND api_end_ts >= t10 AND api_end_ts < now() - INTERVAL 1 MINUTE GROUP BY instance), emp AS (SELECT instance, arraySort(groupUniqArray(pid)) AS pl, length(pl) AS np, sum(f) AS fe, sum(sa) AS se FROM (SELECT instance, pid, argMax(acc_fallback['forward'], ts) AS f, argMax(acc_fallback['sampling'], ts) AS sa FROM vllm_pd.filter_selfcheck WHERE ts > now() - INTERVAL 10 MINUTE GROUP BY instance, pid) GROUP BY instance) SELECT i.instance AS instance, match(i.instance, '^(.+)$') AS this_batch, toString(t10) AS t10_utc, dateDiff('minute', t10, now()) AS window_min, ew.ne AS ended_win, emp.np AS pids, emp.pl AS pid_list, emp.fe AS forward_empty, emp.se AS sampling_empty, x.a_tot AS alloc_total, x.f_tot AS forward_total, x.s_tot AS sampling_total, x.a_win AS alloc_win, x.f_win AS forward_win, x.s_win AS sampling_win, if(x.a_win = 0, NULL, round(100 * (1 - x.f_win / x.a_win), 2)) AS forward_empty_pct, if(x.a_win = 0, NULL, round(100 * (1 - x.s_win / x.a_win), 2)) AS sampling_empty_pct FROM (SELECT DISTINCT instance FROM done) AS i LEFT JOIN (SELECT instance, sum(al) AS a_tot, sum(fw) AS f_tot, sum(sm) AS s_tot, sum(al - al0) AS a_win, sum(fw - fw0) AS f_win, sum(sm - sm0) AS s_win FROM (SELECT l.instance AS instance, l.request_id AS request_id, l.part AS part, toUInt32OrZero(splitByChar('-', l.part)[2]) AS pid, ifNull(max(l.acc__forward__steps), 0) AS fw, ifNull(maxIf(l.acc__forward__steps, l.ts < t10), 0) AS fw0, ifNull(max(l.acc__sampling__steps), 0) AS sm, ifNull(maxIf(l.acc__sampling__steps, l.ts < t10), 0) AS sm0, ifNull(max(l.acc__alloc__steps), 0) AS al, ifNull(maxIf(l.acc__alloc__steps, l.ts < t10), 0) AS al0 FROM vllm_pd.phase_line_cur AS l WHERE l.part != '' AND (l.instance, l.request_id) IN (SELECT instance, request_id FROM done) GROUP BY l.instance, l.request_id, l.part) WHERE (instance, pid) IN (SELECT instance, pid FROM cur) GROUP BY instance) AS x ON i.instance = x.instance LEFT JOIN emp ON i.instance = emp.instance LEFT JOIN ew ON i.instance = ew.instance ORDER BY this_batch DESC, instance SETTINGS join_use_nulls = 0 FORMAT PrettyCompactMonoBlock
SQL
```

磁盘：

```bash
N=<节点目录>/logs; S0=~/t51-glm53-disk.t0; t0=$(sed -n 1p "$S0"); n0=$(sed -n 2p "$S0"); t1=$(date +%s); n1=$(du -sb "$N" | cut -f1); b=0; p=0
while read -r f s0; do b=$((b + $(tail -c +$((s0 + 1)) "$f" | grep -a '\[BatchPutTiming\]' | wc -c))); p=$((p + $(tail -c +$((s0 + 1)) "$f" | grep -a -c '\[PD_DIAG\]'))); done < <(sed -n '3,$p' "$S0")
awk -v t=$((t1 - t0)) -v n=$((n1 - n0)) -v b=$b -v p=$p 'BEGIN { printf "%d 分钟：节点目录每天 %.1f GB；vLLM 日志 [BatchPutTiming] 每天 %.2f GB，[PD_DIAG] %d 行\n", t / 60, n / t * 86400 / 1e9, b / t * 86400 / 1e9, p }'
```

**应当看到、怎么判：** 和 T51 时第 18 步相同，只是不分批：所有实例都是 `this_batch` 为 1，每个实例单独判。

留空比例每个实例一行：

- `this_batch`：整个集群一起拉起，所有实例都是 `1`。
- `window_min`：`t10_utc` 到现在的分钟数，应当在 25 左右。
- `ended_win`：`t10_utc` 以后、现在 1 分钟以前结束的请求数。
- `pid_list`、`pids`：最近 10 分钟写过自检行的进程号和个数，和第 29 步相同。
- `forward_empty`、`sampling_empty`：留空次数（进程启动以来累计）。
- `alloc_total`、`forward_total`、`sampling_total`：已经结束 1 分钟以上的请求按步累加的步数；`alloc_win`、`forward_win`、`sampling_win`：同样的步数，只算 `t10_utc` 以后。
- `forward_empty_pct` = 1 − `forward_win` ÷ `alloc_win`，`sampling_empty_pct` 同理。比例可能是零点几的负数，按 0 看。留空次数只看增没增加，不能和比例相除。
- 这里只判 forward、sampling；新加的几段有没有留空，在第 35 步那条查询里看。
- **留空不是因为捕图。** 服务期间不捕图：所有图在启动时捕完，服务中再要捕图会直接报错（vllm-ascend `acl_graph.py` 第 184 行）。T51 手册第 18 步那句「服务中第一次遇到某个桶的那一步在捕获」不对；留空另有原因：设备事件在 4 步之内没读到、同一步量了两次、这一步本来就没跑这个 stage。这个比例也不按第一步、第二步起分开看：设备事件记在计算流上，和这一步走哪种图模式无关。

磁盘一行，例如 `30 分钟：节点目录每天 <x> GB；vLLM 日志 [BatchPutTiming] 每天 <y> GB，[PD_DIAG] 0 行`。这 30 分钟里有 vLLM 日志文件被轮转了的，vLLM 日志一项会偏小：重新记起点再量。
- **Mooncake v2 以后：** 计时记录写在节点目录的 `logs/mooncake/` 下，算在「节点目录每天」里（P 机器比 T56 时多出这一项；按一行约 456 字节估，最重时每天约 63 GB，只留 12 小时）。`[BatchPutTiming]` 一项应当是 `0.00`：大于 0 说明换上的还是 v1，带回第 27 步的输出。

**怎么判（forward、sampling 各自判，每个实例单独判）：** 先看下面三条，再看留空比例表。

| 看到 | 结果 |
|---|---|
| `window_min` 明显大于 35 | `t10_utc` 抄错了：重抄第 29 步的 `t10_utc`，再执行；这一次不判 |
| 两次的 `pid_list` 不同 | 进程重启过，两次不可比：D 的实例算升级失败；P 的带回。带回两次输出 |
| 比例为空，`ended_win` 大于 0，`forward_win` 或 `sampling_win` 大于 0 | 调度器侧断了：升级失败（D、P 都算），带回 |
| 比例为空，`ended_win` 大于 0，三个 `*_win` 都是 0 | 不直接判失败，带回 |
| 比例为空，`ended_win` 是 0 | 没判成，不算通过，带回 |

| 比例 | 留空次数（第 31 步比第 29 步） | 结果 |
|---|---|---|
| 超过 1% | 增加了 | D：升级失败。P：带回，不算失败 |
| 超过 1% | 没有增加 | 不判失败：两侧步数对不上，带回 |
| 不超过 1% | 增加了 | 通过。记下增加了多少，带回 |
| 不超过 1% | 没有增加 | 通过 |

磁盘按「判失败的条件」里的磁盘项判。

**带回：** 留空比例两次的输出，磁盘那一行（每台机器）。

#### 第 32 步　面板二（v3.3-extra）：拉起以后的核对

**机器：** 监控机。拉起 3 分钟以后，发一批请求（第 36 步的参考请求就可以，也可以先用平时的请求），再执行。命令和第 5 步相同。

**命令：**

```bash
cd <总目录>/vllm-pd-grafana-panel2/test-glm53 && cat VERSION && bin/panel2_cluster.sh check test-glm53
```

```bash
cd <总目录>/vllm-pd-grafana-panel2/test-glm53 && bin/coverage.sh
```

**应当看到：** `check` 第一行 `v3.3-extra`，最后一行 `RESULT: OK`。其中：
- `OK   库：表和视图都在，视图读的是 pd-diag-v3.3-extra-glm53-2026-11-20a`；
- `OK   过滤器版本：<n> 个实例 <n> 个进程都是 pd-diag-v3.3-extra-glm53-2026-11-20a@ace07cfe`；
- `INFO 粒度：P 3 级 1 个实例；D 3 级 16 个实例`：和启动脚本一致（这次不设 `PD_DIAG_LEVEL`，就是 3 级）。和第 5 步不同的只有这一行：拉起以前是「自检行没有粒度」。

`coverage.sh` 每段一行，3 行对照，最后两行汇总。这次是 3 级，没有「按粒度不记」那一行；段数看 proxy 做没做、P 上有没有 Mooncake 文件：

| proxy 和 Mooncake 文件都有 | 没做 proxy | P 上没有 Mooncake 文件（`not_replaced`，或者仍是 v1） | 两样都没有 |
|---|---|---|---|
| 54 段，`RESULT: OK` | 52 段，`RESULT: FAIL` 只列 2 个 lb_proxy | 51 段，`RESULT: FAIL` 只列 P 后台 put 下 3 段 | 49 段，`RESULT: FAIL` 只列这 5 段 |

- coverage 不看采集来源在不在，没采到的段也算「有来源但为空」。最后一行列出的段正好是表里这一格的段时，不算失败。
- coverage 看最近 30 分钟：时间窗里还有拉起以前的请求时，P 后台 put 下 3 段写成 `k / n`，n 里含拉起以前、v2 还没写文件时的请求。等 30 分钟以后再看一次，就只剩拉起以后的请求。
- 拉取程序 `status` 里 `mooncake` 一行是 `OK`，不说明有文件：没有文件时也是 `OK`。判据是 P 后台 put 下 3 段有值。

**不对时：**

| 看到 | 怎么办 |
|---|---|
| `check` 不是 `RESULT: OK` | 这次升级失败（「判失败的条件」），带回全部输出 |
| 粒度那一行不是 3 级，或者有一条 `WARN 粒度：<实例> 的几个进程粒度不同` | 停下，带回这条命令的输出和那个实例的启动脚本里 `PD_DIAG_LEVEL` 那一行 |
| 粒度那一行还是「自检行没有粒度」 | 过滤器还是旧的，那些实例的 vLLM 没有在换面板以后重启：带回 |
| coverage 最后一行列出了表里那一格以外的段 | 带回全部输出 |

#### 第 33 步　面板一：TTFT、TPOT 中位数（观测项，不判失败）

**机器：** 面板一 Decode 看板「TPOT P50 / P90 / P99 / Max」面板的菜单里点 Explore，把查询换成下面的语句，查询类型选 Instant。「停机前」把时间范围的结束时刻设成第 18 步记下的停的时刻；「拉起后」设成拉起以后 40 分钟。

**命令：**

```text
histogram_quantile(0.5, sum by (le) (increase(vllm:time_to_first_token_seconds_bucket{role="decode", pd_instance=~".+"}[30m])))
```

```text
histogram_quantile(0.5, sum by (le) (increase(vllm:request_time_per_output_token_seconds_bucket{role="decode", pd_instance=~".+"}[30m])))
```

```text
histogram_quantile(0.5, sum by (le) (increase(vllm:time_to_first_token_seconds_bucket{role="prefill", pd_instance=~"P.*"}[30m])))
```

**应当看到：** 每条一个数，单位是秒。只作参考；开销的判据看第七部分。

**带回：** 停机前、拉起后的 D 的 TTFT、TPOT，P 的 TTFT。

#### 第 34 步　升级的结论

按「判失败的条件」逐条看。都通过：做第六部分。有一条失败：停下；照第九部分开头的「回滚顺序」回滚到 T56（两个容器都做，换过 Mooncake 的同时换回 v1，整个集群只停一次）；面板回不回滚到 v3.2-extra，要在那个顺序里拉起（第 43 步）以前定；带回检查输出、vLLM 启动日志和第 28 步那条查询的输出。

## 第六部分　首批数据核对（换面板以后，一条只读查询）

#### 第 35 步　首批数据核对

**机器：** 监控机。只读。

**什么时候做：**
- T58 拉起以后（第 23 步），先等 1–2 分钟，让拉起那一刻正在处理的请求结束；
- 再让平常的流量或第 1 步那组请求跑几分钟，有一些从 P 读过 KV 的请求结束了，再执行；
- 拉起以后 15 天以内都可以查（`diag_event` 保留 15 天，其余表 30 天）。

**为什么只看拉起以后开始的请求：** 新的过滤器随拉起才加载；拉起那一刻在途的请求，开头几步的记录在停机里断了，部分和不全。

**命令：** `<D 实例>` 填一个 D 实例的名字，例如 `D0`。`<开始时刻>` 填第 23 步拉起完成的时刻加 2 分钟，UTC，写成 `年-月-日 时:分:秒.毫秒`，例如 `2026-11-21 02:30:00.000`。

```bash
cd <总目录>/vllm-pd-grafana-panel2/test-glm53 && bash -c '. bin/lib.sh && load_env >/dev/null && curl -s -u "vllm_reader:$CH_READER_PASSWORD" "http://localhost:$CLICKHOUSE_HTTP_PORT/" --data-binary @-' <<'SQL'
WITH '<D 实例>' AS inst, toDateTime64('<开始时刻>', 3, 'UTC') AS t0,
r AS (
 SELECT request_id, min(ts) AS t_first,
  max(acc__prepare_inputs__first_ns) AS pi1, max(acc__prepare_inputs__ns) AS pi, max(acc__forward__first_ns) AS fw1, max(acc__forward__ns) AS fw,
  max(acc__lm_head__first_ns) AS lm1, max(acc__lm_head__ns) AS lm, max(acc__sampling__first_ns) AS sa1, max(acc__sampling__ns) AS sa,
  max(acc__bookkeeping__first_ns) AS bk1, max(acc__bookkeeping__ns) AS bk, max(acc__draft__first_ns) AS dr1, max(acc__draft__ns) AS dr,
  max(acc__step_device_span__first_ns) AS sp1, max(acc__step_device_span__ns) AS sp,
  max(acc__graph_full__first_ns) AS gf1, max(acc__graph_piecewise__first_ns) AS gp1, max(acc__graph_none__first_ns) AS gn1,
  max(acc__graph_full__steps) AS gfs, max(acc__graph_piecewise__steps) AS gps, max(acc__graph_none__steps) AS gns,
  max(start_load_kv_ms) AS env, max(start_load_kv__receive_queue_ms) AS rq, max(start_load_kv__prepare_read_ms) AS rp,
  max(start_load_kv__wait_for_transfer_ms) AS wft, max(kv_ready_wait_ms) AS krw, max(schedule__queue_time_ms) AS qt,
  max(kv_wait_vllm_ms) AS kvv, max(first_step_vllm_ms) AS fsv,
  argMinIf(first_exec, first_mono_ns, proc = 'engine' AND part != '' AND first_exec != '') AS fexec
 FROM vllm_pd.phase_line_cur WHERE role = 'decode' AND instance = inst AND ts >= t0 - INTERVAL 1 HOUR
 GROUP BY request_id HAVING t_first >= t0),
k AS (
 SELECT x, any(JSONExtractString(raw, 'stages', 'kv_ready_wait', 'ranges', 1, 'scheduled_execution_id')) AS sexec
 FROM vllm_pd.diag_event WHERE event = 'core_summary[request]' AND side = 'decode' AND instance = inst AND ts >= t0 - INTERVAL 1 HOUR AND JSONHas(raw, 'stages', 'kv_ready_wait')
 GROUP BY x),
j AS (SELECT r.*, k.sexec AS sexec, k.x != '' AS has_krw, r.rq IS NOT NULL AS from_p,
  coalesce(pi1, 0) + coalesce(fw1, 0) + coalesce(lm1, 0) + coalesce(sa1, 0) + coalesce(bk1, 0) + coalesce(dr1, 0) AS six1,
  coalesce(pi, 0) + coalesce(fw, 0) + coalesce(lm, 0) + coalesce(sa, 0) + coalesce(bk, 0) + coalesce(dr, 0) AS six
  FROM r LEFT JOIN k ON k.x = r.request_id),
fb AS (
 SELECT sum(f1 - f0) AS miss, sum(a1 - a0) AS recs FROM (
  SELECT pid,
   argMax(acc_fallback['forward'] + acc_fallback['sampling'] + acc_fallback['prepare_inputs'] + acc_fallback['lm_head'] + acc_fallback['draft'] + acc_fallback['bookkeeping'], ts) AS f1,
   argMin(acc_fallback['forward'] + acc_fallback['sampling'] + acc_fallback['prepare_inputs'] + acc_fallback['lm_head'] + acc_fallback['draft'] + acc_fallback['bookkeeping'], ts) AS f0,
   argMax(accumulated, ts) AS a1, argMin(accumulated, ts) AS a0
  FROM vllm_pd.filter_selfcheck WHERE instance = inst AND role = 'worker' AND ts >= t0 GROUP BY pid)),
c AS (
 SELECT 1 AS o, 'span_first' AS item, countIf(sp1 > 0) AS n, countIf(sp1 > 0 AND six1 > sp1) AS bad, '' AS note FROM j
 UNION ALL SELECT 2, 'span_rest', countIf(sp > sp1), countIf(sp > sp1 AND six - six1 > sp - sp1), '' FROM j
 UNION ALL SELECT 3, 'first_step_le_vllm', countIf(fsv IS NOT NULL AND fw1 IS NOT NULL), countIf(fsv IS NOT NULL AND fw1 IS NOT NULL AND six1 / 1e6 > fsv), '' FROM j
 UNION ALL SELECT 4, 'envelope', countIf(from_p AND env IS NOT NULL), countIf(from_p AND env IS NOT NULL AND env < greatest(coalesce(wft, 0), coalesce(rq, 0), coalesce(rp, 0))), 'no envelope (not marked done): ' || toString(countIf(from_p AND env IS NULL)) FROM j
 UNION ALL SELECT 5, 'kv_wait_vllm_ge_sum', countIf(from_p AND kvv IS NOT NULL AND qt IS NOT NULL AND env IS NOT NULL AND krw IS NOT NULL),
  countIf(from_p AND kvv IS NOT NULL AND qt IS NOT NULL AND env IS NOT NULL AND krw IS NOT NULL AND kvv < qt + env + krw), '' FROM j
 UNION ALL SELECT 6, 'kv_ready_wait_present', countIf(from_p), countIf(from_p AND NOT has_krw), '' FROM j
 UNION ALL SELECT 7, 'kv_ready_wait_same_step', countIf(has_krw), countIf(has_krw AND sexec != fexec), '' FROM j
 UNION ALL SELECT 8, 'device_missing', toUInt64(recs), toUInt64(miss), 'per 100 records: ' || toString(round(100 * miss / greatest(recs, 1), 2)) FROM fb
 UNION ALL SELECT 9, 'graph_rest_none', toUInt64(sum(coalesce(gfs, 0) + coalesce(gps, 0) + coalesce(gns, 0)) - countIf(gf1 > 0 OR gp1 > 0 OR gn1 > 0)), toUInt64(sum(coalesce(gns, 0)) - countIf(gn1 > 0)),
  'first step: FULL ' || toString(countIf(gf1 > 0)) || ', PIECEWISE ' || toString(countIf(gp1 > 0)) || ', NONE ' || toString(countIf(gn1 > 0)) FROM j),
 res AS (SELECT o, item, n, bad, if(n = 0, 'NO_DATA', if(o IN (8, 9), if(bad * 100 <= n, 'PASS', 'FAIL'), if(bad = 0, 'PASS', 'FAIL'))) AS result, note FROM c)
SELECT item, n, bad, result, note FROM (
 SELECT o, item, n, bad, result, note FROM res
 UNION ALL SELECT 99, 'RESULT', toUInt64(0), toUInt64(countIf(result = 'FAIL')), if(countIf(result = 'FAIL') > 0, 'FAIL', if(countIf(result = 'NO_DATA') > 0, 'NO_DATA', 'PASS')), '' FROM res)
ORDER BY o
FORMAT PrettyCompactMonoBlock
SQL
```

**应当看到：** 10 行。前 9 行每项一行，`n` 是参与这一项的请求数（或步数、记录数），`bad` 是不满足的个数，`result` 是 `PASS`、`FAIL` 或 `NO_DATA`；最后一行 `RESULT`，是 `PASS`。

| 项 | 核的是什么 | `PASS` 的条件 |
|---|---|---|
| `span_first` | 第一步 6 段设备时长（`prepare_inputs`、`forward`、`lm_head`、`sampling`、`bookkeeping`、`draft`）之和 ≤ 这一步的设备包络 `step_device_span_ns` | `bad` 为 0 |
| `span_rest` | 同上，第二步起的总和 | `bad` 为 0 |
| `first_step_le_vllm` | 第一步 6 段之和 ≤ 对照行 `prefill_time (vLLM)` | `bad` 为 0 |
| `envelope` | 从 P 读过 KV 的请求：`start_load_kv`（墙钟包络）≥ `wait_for_transfer`、`receive_queue`、`prepare_read` | `bad` 为 0 |
| `kv_wait_vllm_ge_sum` | 从 P 读过 KV、几段都有值的请求：对照行 `queued_time (vLLM)` ≥ `queue_time` + `start_load_kv` + `kv_ready_wait`。直连的请求不核：两边起点不同 | `bad` 为 0 |
| `kv_ready_wait_present` | 从 P 读过 KV 的 D 请求都有 `kv_ready_wait` | `bad` 为 0 |
| `kv_ready_wait_same_step` | `kv_ready_wait` 结束在这个请求第一次被调度的那一步（按步的标识核）。结束时刻等于那一步调度开始的时刻，这一条由实验室单测核，现场不查 | `bad` 为 0 |
| `device_missing` | 拉起以后，自检行里设备值缺失的次数（6 段合计）和同期记录数 | 缺失 ≤ 记录数的 1% |
| `graph_rest_none` | 第二步起走 `NONE`（不用图）的步数；`note` 里列出第一步走 FULL、PIECEWISE、NONE 的请求数 | 第二步起 `NONE` ≤ 1% |

- 两个 1%（`device_missing`、`graph_rest_none`）是阈值，不是预测，和 T51 留空比例的阈值一致。
- `graph_rest_none` 只判第二步起；第一步各走什么模式只列出来，不判。
- `cudagraph_runtime_mode` 只代表目标模型：MTP 草稿模型自己选图模式，不在这一项里。

**不对时：**

| 看到 | 怎么办 |
|---|---|
| 某一项 `NO_DATA` | 请求还不够（例如还没有从 P 读过 KV 的请求结束）：再等几分钟，重跑同一条 |
| `kv_ready_wait_present` 是 `FAIL` | D 的 EngineCore 没被判成 D：新加的段不写，服务不受影响。不回滚，带回这一行和第 26 步的启动行 |
| 其余某一项 `FAIL` | 服务不受影响，不回滚：带回整份输出，由我们看 |
| 报 `UNKNOWN_IDENTIFIER` 之类的错 | 面板还不是 v3.3-extra（第二部分没做完），或者表里还没有这些列：先核第五部分最后一步 |

**带回：** 整份输出，`<D 实例>` 和 `<开始时刻>`。

## 第七部分　升级后：参考请求 3 遍，比 TPOT

#### 第 36 步　参考请求再跑 3 遍，按判据比

**机器：** 和第 1 步相同。第 35 步做完以后再做。

**命令：** 和第 1 步同一种工具、同一组参数，再跑 3 遍。甲的命令照抄第 1 步，`<升级前或升级后>` 填 `升级后`。然后把升级前、升级后各 3 个 Median TPOT 填进下面这条命令（在任何一个有 `python3` 的窗口里执行）：

```bash
python3 -I -c "import statistics as s, sys; a = [float(x) for x in sys.argv[1:4]]; b = [float(x) for x in sys.argv[4:7]]; ma, mb = s.median(a), s.median(b); w = max(max(a) - min(a), max(b) - min(b)); lim = max(0.02 * ma, w); print('before=%.2f after=%.2f diff=%.2f limit=%.2f %s' % (ma, mb, mb - ma, lim, 'SLOWER' if mb - ma > lim else 'OK'))" <前1> <前2> <前3> <后1> <后2> <后3>
```

**怎么判：** 输出一行 `before=<升级前的中位数> after=<升级后的中位数> diff=<差> limit=<界> OK` 或者 `… SLOWER`。

- 中位数是 3 遍各自的 Median TPOT 再取中位数。
- `limit` = max(2% × 升级前的中位数, 波动)；波动是同一版本 3 遍的最大减最小，升级前、升级后各算一次，取大的。
- `diff` 大于 `limit` 时是 `SLOWER`。
- 2% 是阈值，不是预测：它是按 D 每步约 60 ms、新加的设备事件记录每次几十微秒估的，没有量过。

| 结果 | 怎么办 |
|---|---|
| `OK` | 保留现在的设置，升级完成 |
| `SLOWER` | 在 D 的启动脚本里，T51 加的两行 `export` 下面加一行 `export VLLM_PD_DEVICE_STAGES=lm_head,draft`（P 不加）。然后照第 18 步停整个集群，照第 23 步拉起；拉起以后照第 24、26 步核对：D 的 `tp_rank=0` 那一行应当是 `runner_stages=forward,sampling,lm_head,draft source=env`，P 的行不变。之后看板上 `prepare_inputs` 为空（「有值 / 请求」一列显示 `0 / n`），`bookkeeping` 和 `step_device_span_ns` 也不写。改了启动脚本以后，再做检测时 `baseline_token` 会和第 9 步的不同，这是预期的，下次升级重新记基线。只改环境变量，不重出补丁包。把这一行和 3+3 个数带回 |

**带回：** 升级后 3 遍的那三行、这条命令的输出，以及用的是甲还是乙。

## 第八部分　第一步开销分析（必做，只读）

#### 第 37 步　第一步开销分析

**用途：** 看 D 上第一步比第二步起多出的时间（约 48 ms）落在哪里。这次只给分析能力：只读，不上看板，不加 L1/L2/L3；找到原因以后，在 T57 里解决。只用面板二已有的列，和 5 条不上看板的规则（`step_device_span`，`graph_full`、`graph_piecewise`、`graph_none`，`bookkeeping`）。

**什么时候做：** 第 35 步通过以后，跑过一批请求再做。
- 最近 `<分钟>` 分钟里，要有这个 D 实例结束的请求。
- 只算换面板以后开始的请求，所以时间窗不要跨过换面板那一刻。

**机器：** 监控机。

**命令：** `<D 实例>` 填一个 D 实例的名字，写法和看板上的实例名相同，例如 `D0`；`<分钟>` 填时间窗的分钟数，例如 `30`。两处都在命令里改。查询是 t41 的 `第一步开销分析.sql`，原样放在命令里，用的是和第 35 步相同的只读访问，不用另外拷文件。

```bash
cd <总目录>/vllm-pd-grafana-panel2/test-glm53 && bash -c '. bin/lib.sh && load_env >/dev/null && curl -s -u "vllm_reader:$CH_READER_PASSWORD" "http://localhost:$CLICKHOUSE_HTTP_PORT/" --data-binary @-' <<'SQL'
-- 第一步开销分析（只读，不上看板）：一个 D 实例、一个时间窗里的请求，第一步一行、第二步起一行（每步）。
-- 6 段：prepare_inputs、forward、lm_head、sampling、draft、bookkeeping；最后一列第一步是 prefill_time，第二步起是每步的
-- decode_time（decode_time ÷ 第二步起的步数），都是 vLLM 口径。
-- 填两处：<D 实例>（例如 D0）、<分钟>。只用面板二已有的列和 5 条不上看板的规则（step_device_span、graph_full、
-- graph_piecewise、graph_none、bookkeeping），段定义 glm53。
-- 时长单位 ms；6 段、span 都是设备时长，只在 D 的 tp_rank 0 有。第一步取各请求第一步的值，第二步起取各请求
-- （总和 − 第一步）÷ 第二步起的步数，再取中位数。某一段这个请求的第一步没有值（规则的 first_ns 为 0）时，这个请求
-- 不进这一段的中位数。6 段之和、span、空隙只算可比的请求：第一步是 6 段和 span 都有值的请求，第二步起是 6 段和 span
-- 第二步起的步数都相同的请求（有的步缺设备值时，各段的平均不是同一批步，相减没有意义）；「可比请求数」是这批请求的个数。
-- T58（v3.3-extra）：按每个请求 D 侧的粒度（core_summary[request] 的 diag_level，没有按 3）。低于 3 级时不写
-- bookkeeping 这一段的值，那对设备事件照记（用户选的做法乙），所以 step_device_span 各级含义相同；这时「6 段之和」
-- 只有 5 段（不含 bookkeeping），「空隙」里含 bookkeeping 的时间，可比请求也不看 bookkeeping 的步数。最后一列
-- 「D 侧粒度」列出这批请求的粒度；不止一种时，缩短时间窗，不跨换粒度的那次重启。
WITH per AS (
    SELECT request_id,
        max(acc__prepare_inputs__ns) AS pi_t, nullIf(max(acc__prepare_inputs__first_ns), 0) AS pi_f, max(acc__prepare_inputs__steps) AS pi_n,
        max(acc__forward__ns) AS fw_t, nullIf(max(acc__forward__first_ns), 0) AS fw_f, max(acc__forward__steps) AS fw_n,
        max(acc__lm_head__ns) AS lh_t, nullIf(max(acc__lm_head__first_ns), 0) AS lh_f, max(acc__lm_head__steps) AS lh_n,
        max(acc__sampling__ns) AS sp_t, nullIf(max(acc__sampling__first_ns), 0) AS sp_f, max(acc__sampling__steps) AS sp_n,
        max(acc__draft__ns) AS dr_t, nullIf(max(acc__draft__first_ns), 0) AS dr_f, max(acc__draft__steps) AS dr_n,
        max(acc__bookkeeping__ns) AS bk_t, nullIf(max(acc__bookkeeping__first_ns), 0) AS bk_f, max(acc__bookkeeping__steps) AS bk_n,
        max(acc__step_device_span__ns) AS sd_t, nullIf(max(acc__step_device_span__first_ns), 0) AS sd_f, max(acc__step_device_span__steps) AS sd_n,
        max(acc__graph_full__steps) AS gf_n, max(acc__graph_full__first_ns) AS gf_f,
        max(acc__graph_piecewise__steps) AS gp_n, max(acc__graph_piecewise__first_ns) AS gp_f,
        max(acc__graph_none__steps) AS gn_n, max(acc__graph_none__first_ns) AS gn_f,
        maxIf(first_step_vllm_ms, proc = 'api') AS prefill_ms, maxIf(rest_vllm_rest_ms, proc = 'api') AS decode_ms
    FROM vllm_pd.phase_line_cur
    WHERE role = 'decode' AND instance = '<D 实例>' AND ts > now() - INTERVAL <分钟> MINUTE /*请求*/
    GROUP BY request_id
),
lvl AS (
    SELECT x AS request_id, toUInt8(ifNull(min(JSONExtract(raw, 'diag_level', 'Nullable(UInt8)')), 3)) AS dlv
    FROM vllm_pd.diag_event
    WHERE event = 'core_summary[request]' AND side = 'decode' AND instance = '<D 实例>' AND ts > now() - INTERVAL <分钟> MINUTE - INTERVAL 10 MINUTE
    GROUP BY x
),
r AS (
    SELECT *,
        -- 第二步起每步的值：（总和 − 第一步）÷（步数 − 第一步那一步）
        if(pi_n - (pi_f > 0) > 0, (pi_t - ifNull(pi_f, 0)) / (pi_n - (pi_f > 0)), NULL) AS pi_r,
        if(fw_n - (fw_f > 0) > 0, (fw_t - ifNull(fw_f, 0)) / (fw_n - (fw_f > 0)), NULL) AS fw_r,
        if(lh_n - (lh_f > 0) > 0, (lh_t - ifNull(lh_f, 0)) / (lh_n - (lh_f > 0)), NULL) AS lh_r,
        if(sp_n - (sp_f > 0) > 0, (sp_t - ifNull(sp_f, 0)) / (sp_n - (sp_f > 0)), NULL) AS sp_r,
        if(dr_n - (dr_f > 0) > 0, (dr_t - ifNull(dr_f, 0)) / (dr_n - (dr_f > 0)), NULL) AS dr_r,
        if(bk_n - (bk_f > 0) > 0, (bk_t - ifNull(bk_f, 0)) / (bk_n - (bk_f > 0)), NULL) AS bk_r,
        if(sd_n - (sd_f > 0) > 0, (sd_t - ifNull(sd_f, 0)) / (sd_n - (sd_f > 0)), NULL) AS sd_r,
        if(lv >= 3, pi_f + fw_f + lh_f + sp_f + dr_f + bk_f, pi_f + fw_f + lh_f + sp_f + dr_f) AS six_f,
        if(lv >= 3, pi_r + fw_r + lh_r + sp_r + dr_r + bk_r, pi_r + fw_r + lh_r + sp_r + dr_r) AS six_r,
        pi_n - (pi_f > 0) AS pi_k,
        ifNull(pi_k > 0 AND pi_k = fw_n - (fw_f > 0) AND pi_k = lh_n - (lh_f > 0) AND pi_k = sp_n - (sp_f > 0)
            AND pi_k = dr_n - (dr_f > 0) AND (lv < 3 OR pi_k = bk_n - (bk_f > 0)) AND pi_k = sd_n - (sd_f > 0), 0) AS rest_ok,
        ifNull(gf_n, 0) + ifNull(gp_n, 0) + ifNull(gn_n, 0) AS steps_all
    FROM (SELECT per.*, if(lvl.dlv = 0, toUInt8(3), lvl.dlv) AS lv FROM per LEFT JOIN lvl ON lvl.request_id = per.request_id)
)
SELECT * FROM (
SELECT '第一步' AS "步",
    count() AS "请求数",
    round(quantileExact(0.5)(pi_f) / 1e6, 3) AS "prepare_inputs",
    round(quantileExact(0.5)(fw_f) / 1e6, 3) AS "forward",
    round(quantileExact(0.5)(lh_f) / 1e6, 3) AS "lm_head",
    round(quantileExact(0.5)(sp_f) / 1e6, 3) AS "sampling",
    round(quantileExact(0.5)(dr_f) / 1e6, 3) AS "draft",
    round(quantileExact(0.5)(bk_f) / 1e6, 3) AS "bookkeeping",
    countIf(isNotNull(six_f) AND isNotNull(sd_f)) AS "可比请求数",
    round(quantileExactIf(0.5)(six_f, isNotNull(sd_f)) / 1e6, 3) AS "6 段之和",
    round(quantileExactIf(0.5)(sd_f, isNotNull(six_f)) / 1e6, 3) AS "step_device_span",
    round(quantileExactIf(0.5)(sd_f - six_f, isNotNull(sd_f) AND isNotNull(six_f)) / 1e6, 3) AS "空隙（step_device_span − 6 段之和）",
    countIf(gf_f > 0) AS "FULL 步数",
    countIf(gp_f > 0) AS "PIECEWISE 步数",
    countIf(gn_f > 0) AS "NONE 步数",
    round(quantileExact(0.5)(prefill_ms), 3) AS "prefill_time / decode_time (vLLM)",
    arrayStringConcat(arrayMap(x -> toString(x), arraySort(groupUniqArray(lv))), '、') AS "D 侧粒度"
FROM r
UNION ALL
SELECT '第二步起（每步）' AS "步",
    count() AS "请求数",
    round(quantileExact(0.5)(pi_r) / 1e6, 3),
    round(quantileExact(0.5)(fw_r) / 1e6, 3),
    round(quantileExact(0.5)(lh_r) / 1e6, 3),
    round(quantileExact(0.5)(sp_r) / 1e6, 3),
    round(quantileExact(0.5)(dr_r) / 1e6, 3),
    round(quantileExact(0.5)(bk_r) / 1e6, 3),
    countIf(rest_ok),
    round(quantileExactIf(0.5)(six_r, rest_ok) / 1e6, 3),
    round(quantileExactIf(0.5)(sd_r, rest_ok) / 1e6, 3),
    round(quantileExactIf(0.5)(sd_r - six_r, rest_ok) / 1e6, 3),
    toUInt64(sum(ifNull(gf_n, 0) - (gf_f > 0))),
    toUInt64(sum(ifNull(gp_n, 0) - (gp_f > 0))),
    toUInt64(sum(ifNull(gn_n, 0) - (gn_f > 0))),
    round(quantileExactIf(0.5)(decode_ms / (steps_all - 1), steps_all > 1), 3),
    arrayStringConcat(arrayMap(x -> toString(x), arraySort(groupUniqArray(lv))), '、')
FROM r
)
ORDER BY "步" != '第一步'
FORMAT Vertical
SQL
```

**输出：** 两行。`第一步` 一行，`第二步起（每步）` 一行。时长单位 ms，都是各请求的中位数。

| 列 | 第一步 | 第二步起（每步） |
|---|---|---|
| 请求数 | 时间窗里这个 D 实例的请求数 | 同左 |
| prepare_inputs、forward、lm_head、sampling、draft、bookkeeping | 各请求第一步的设备时长 | 各请求第二步起每步的平均设备时长 |
| 可比请求数 | 6 段和 step_device_span 第一步都有值的请求数 | 6 段和 step_device_span 第二步起的步数都相同的请求数 |
| 6 段之和、step_device_span、空隙（step_device_span − 6 段之和） | 只算可比的请求 | 只算可比的请求，每步 |
| FULL、PIECEWISE、NONE 步数 | 第一步走各图模式的请求数 | 第二步起各图模式的步数之和 |
| prefill_time / decode_time (vLLM) | prefill_time | decode_time ÷ 第二步起的步数 |
| D 侧粒度 | 这批请求的粒度（这次不设 `PD_DIAG_LEVEL`，是 `3`） | 同左 |

几点说明：
- 6 段和 step_device_span 都是设备时长，只在 D 的 `tp_rank` 0 有。
- bookkeeping：同步模式下含从设备拷回主机，异步模式下不含。glm53-test 的 D 是异步模式。
- 粒度低于 3 时不写 bookkeeping 的段值（那对设备事件照记），「6 段之和」只有 5 段，「空隙」里含 bookkeeping 的时间。`D 侧粒度` 不止一种时，缩短时间窗，不跨换粒度的那次重启。这次是 3 级，6 段都在。
- 某一段在某个请求的第一步没有设备值时，这个请求不进这一段第一步的中位数。
- 可比请求数为 0 时，6 段之和、step_device_span、空隙为空。原因是有的步缺设备值，各段的平均不是同一批步，相减没有意义；这时先看首批核对里设备值缺失计数那一项。

**应当看到：**
- 两行都有。请求数 > 0，可比请求数 > 0：首批核对通过时，设备值缺失接近 0。
- 6 段和 step_device_span 都是正数，空隙 ≥ 0。
- 两行的 FULL、PIECEWISE、NONE 步数加起来不为 0；第二步起几乎都是 FULL。
- prefill_time (vLLM) 比第二步起每步的 decode_time 大，大约多出 48 ms（现场讨论材料里的数，这一步就是要找它）。
- 两行都没有时：时间窗里没有这个实例的请求，或者面板还没换成 v3.3-extra。先查 `cat VERSION` 和首批核对，再发回输出。

**怎么读：** 先用第一步一行减第二步起一行，找出第一步多出来的那一部分，再按下表判断。

| 看到的 | 说明 |
|---|---|
| 第一步的 prepare_inputs 明显大于第二步起 | 多出的时间在输入准备 |
| 第一步的 NONE 或 PIECEWISE 步数多，第二步起几乎都是 FULL | 第一步没走完整的图，forward 也会跟着变大 |
| 空隙（step_device_span − 6 段之和）明显大 | 时间在设备段之间：设备上有段以外的工作，或者在等主机 |
| 都不是 | 时间不在设备时间线上（例如主机侧的调度、通信），要另定下一步 |

「明显」没有定死的阈值。做法是：把各段「第一步减第二步起每步」的差，和 vLLM 口径第一步多出的时间（prefill_time 减第二步起每步的 decode_time）对着看，哪一项能填上大部分，就是哪一项。

**把这一步的全部输出发回。** 判断出来的原因在 T57 里解决。

**带回：** 整份输出，`<D 实例>` 和 `<分钟>`。

## 第九部分　整体回滚（只在失败时做，两个容器都做）

回滚把三件补丁恢复到 T56 版，换过 Mooncake 的容器同时用第 15 步的备份换回 v1。

**回滚顺序（固定，不要调换）：** 第 34 步、第 6 步和别处说的「按第九部分回滚」，都照这一条顺序做。

1. 停整个集群（照第 18 步）。
2. 第 38–40 步：恢复三件补丁。
3. 第 41 步：换回 v1，并把 `logs/mooncake/` 移出节点日志目录（那一步的最后一条）。
4. 第 42 步：等拉取程序跑完一轮，在监控机上确认镜像里已经没有 `mc.*.log`。
5. 要回滚面板的，这时做第 6 步；不回滚面板的跳过。
6. 第 43 步：整体拉起，核对。

- **面板回不回滚，要在第 43 步拉起以前定。** 拉起以后再回滚面板，旧的过滤器要再重启一次 vLLM 才用回，要多停一次集群。
- **第 3、4 条必须在第 5 条以前：** 监控机的镜像里还有 `mc.*.log` 时换回 v3.2-extra，它的采集器会把这些文件从头读，每一行进一条 dead-letter；P 跑过几个小时以后，这些文件是 GB 级。
- **面板留在 v3.3-extra 时：** 能用，3 级下补丁写的记录和 T56 相同；但 P 后台 put 下 3 段为空（第 32 步表里「P 上没有 Mooncake 文件」那一列）：v1 把这 3 段的计时写进 vLLM 日志，v3.3-extra 不再读它。要这 3 段，面板也回到 v3.2-extra（第 5 条）。

#### 第 38 步　把监控机上的备份送回容器

**机器：** 监控机执行第一条；那台机器上执行第二条；容器里执行第三条。

**命令：**

```bash
ssh <SSH 账号>@<机器地址> mkdir -p /tmp/t58-<机器>-<时间戳> && scp <用户目录>/vllm-pd-patch-glm53/backup/t58-<机器>-<时间戳>/* <SSH 账号>@<机器地址>:/tmp/t58-<机器>-<时间戳>/
```

```bash
docker cp /tmp/t58-<机器>-<时间戳>/. <容器名>:/tmp/t58-<机器>-<时间戳>
```

```bash
cd /tmp/t58-<机器>-<时间戳> && LC_ALL=C sha256sum -c SHA256SUMS && tar -tf vllm.tar | wc -l && tar -tf vllm-ascend.tar | wc -l
```

**应当看到：** 最后一条打印 `vllm.tar: OK`、`vllm-ascend.tar: OK`、`25`、`12`。

#### 第 39 步　重做补丁包解开和找目录

**机器：** 容器里。容器窗口还开着、变量都在的，跳过这一步。

**命令：**

```bash
mkdir -p /tmp/t58-kit && tar -xf /tmp/t58-glm53-patch-kit.tar -C /tmp/t58-kit && cd /tmp/t58-kit && LC_ALL=C sha256sum -c SHA256SUMS | grep -v ': OK$'; sha256sum /tmp/t58-kit/detect/t51_glm53_check.py | cut -c1-16
```

```bash
VLLM_PKG=$(python3 -c "import importlib.util as u; print(u.find_spec('vllm').submodule_search_locations[0])")
ASCEND_PKG=$(python3 -c "import importlib.util as u; print(u.find_spec('vllm_ascend').submodule_search_locations[0])")
VLLM_ROOT=$(dirname "$VLLM_PKG"); ASCEND_ROOT=$(dirname "$ASCEND_PKG"); F="$ASCEND_PKG/distributed/kv_transfer/kv_p2p/mooncake_connector.py"
echo "PYTHONPATH=[${PYTHONPATH:-}]"; echo "$VLLM_ROOT"; echo "$ASCEND_ROOT"; echo "$F"
```

**应当看到：** 和第 8、10 步相同。

#### 第 40 步　恢复三件

**机器：** 容器里。

**命令：**

```bash
BK=/tmp/t58-<机器>-<时间戳>; K=/tmp/t58-kit/patches
tar -xf "$BK/vllm.tar" -C "$VLLM_ROOT" && (cd "$VLLM_ROOT" && LC_ALL=C sha256sum -c $K/vllm/SHA256SUMS.t56 | grep -v ': OK$')
tar -xf "$BK/vllm-ascend.tar" -C "$ASCEND_ROOT" && (cd "$ASCEND_ROOT" && LC_ALL=C sha256sum -c $K/vllm-ascend/SHA256SUMS.t56 | grep -v ': OK$')
sha256sum "$F" | cut -c1-16
```

**应当看到：** 前两行没有输出（25 个、11 个文件都回到 T56 版）；最后一行是 T56 时记下的 `<S56>` 的前 16 位（T56 重放出来的 `mooncake_connector.py`）。

**不对时：** 有 `FAILED`，或者最后一行不对：停下，带回。

#### 第 41 步　Mooncake 换回 v1（只在换过 v2 的容器里）

**机器：** 监控机执行第一条；那台机器上执行第二条；容器里执行第三、四条；最后一条在 P 的机器上，宿主机里。容器窗口还开着、`MC_PKG`、`MC_LIB` 都在的，前两条可以跳过，第四条里的 `BKMC` 直接用第 15 步的 `<MC 备份目录>`。

**命令：**

```bash
ssh <SSH 账号>@<机器地址> mkdir -p /tmp/t58-<机器>-mc-<MC 时间戳> && scp <用户目录>/vllm-pd-patch-glm53/backup/t58-<机器>-mc-<MC 时间戳>/* <SSH 账号>@<机器地址>:/tmp/t58-<机器>-mc-<MC 时间戳>/
```

```bash
docker cp /tmp/t58-<机器>-mc-<MC 时间戳>/. <容器名>:/tmp/t58-<机器>-mc-<MC 时间戳>
```

```bash
cd /tmp/t58-<机器>-mc-<MC 时间戳> && LC_ALL=C sha256sum -c SHA256SUMS && cut -c1-16 SHA256SUMS
```

```bash
BKMC=/tmp/t58-<机器>-mc-<MC 时间戳>
cp "$BKMC/libmooncake_store.so" "$MC_PKG/.t58_v1_store.so" && chmod --reference="$MC_LIB" "$MC_PKG/.t58_v1_store.so" && chown --reference="$MC_LIB" "$MC_PKG/.t58_v1_store.so" && mv -f "$MC_PKG/.t58_v1_store.so" "$MC_LIB" && sha256sum "$MC_LIB" | cut -c1-16 && ls -l "$MC_LIB" | awk '{print $1, $5}'
```

最后在 P 的机器上（宿主机），把 v2 写的文件移出节点日志目录。要回滚面板的，这一条必须在第 6 步以前做（第九部分开头的回滚顺序第 3 条）。移走不删：回滚说明出了问题，这些计时可能正好是查原因要用的，看过、确认不需要了再由你删；同一个文件系统里 `mv` 只是改名，不占新的空间。留在原处的话，v3.2-extra 的采集器会把里面每一行都报成解析错误，换回 v1 以后也没有人再清理它们：

```bash
[ -d <节点目录>/logs/mooncake ] && mv <节点目录>/logs/mooncake <节点目录>/mooncake-rolled-back-$(date +%Y%m%d-%H%M%S); ls -d <节点目录>/logs/mooncake <节点目录>/mooncake-rolled-back-* 2>&1
```

**应当看到：** 第三条 `libmooncake_store.so: OK` 和 `3131784385846064`；第四条 `3131784385846064` 和一行权限、大小 `15402248`。最后一条：`ls` 报 `<节点目录>/logs/mooncake` 不存在，然后一行 `<节点目录>/mooncake-rolled-back-<时间>`。

**不对时：** 第四条报 `MC_PKG` 为空：照第 14 步第二条重新设好再执行。前 16 位不对：停下，带回。最后一条两行都不存在：这台机器上 v2 没有写过文件（例如 D 不往缓存池保存），不用管。

#### 第 42 步　监控机：确认镜像里已经没有 Mooncake 文件

**机器：** 监控机。第 41 步把 P 机器的 `logs/mooncake/` 移走以后，等拉取程序跑完一轮（默认 10 秒一轮）再执行：拉取程序的 `rsync --delete` 会把镜像里的 `mooncake/` 一起删掉。

**命令：** 只读：从 deploy.env 算出镜像目录 `PULL_DIR`，再数里面的 `mc.*.log`，不改任何东西。外面套一层 `bash -c`：`bin/lib.sh` 会打开 `set -u`，出错时直接 `exit`；在当前终端里直接 source，这个设置会留在终端里，出错时还会把终端关掉。

```bash
cd <总目录>/vllm-pd-grafana-panel2/test-glm53 && bash -c '. bin/lib.sh && load_env >/dev/null && { [ -d "$PULL_DIR" ] || { echo "没有镜像目录 $PULL_DIR"; exit 1; }; } && echo "镜像 $PULL_DIR 里的 mc.*.log：$(find "$PULL_DIR" -path "*/mooncake/*" -name "mc.*.log" | wc -l | tr -d " ") 个"'
```

**应当看到：** `镜像 <镜像目录> 里的 mc.*.log：0 个`。

**不对时：**
- 不是 0 个：等 30 秒再执行一次。还不是 0 个时，**不要回滚面板**（不做第 6 步），执行 `cd <总目录>/vllm-pd-grafana-panel2/test-glm53 && bin/puller.sh status`，看 P 机器那一行是不是 `OK`、上次成功是不是在 `mv` 以后，带回两条命令的输出。
- `没有镜像目录 …`：带回这条输出和 deploy.env。

#### 第 43 步　整体拉起，核对

照第 23 步拉起整个集群，再照第 24、25 步核对。这时检测应当报 `verdict vllm=ok(t56) ascend=ok(t56,site_local=<n>) p2d=ok(t56) mooncake=ok(v1)`（没换过 Mooncake 的是 `not_replaced`），`baseline=same`；这次拉起以后没有新的 `diag level=` 行（日志是追加写的时候，T58 拉起时写的那几行还在文件里，第 26 步的 `grep` 照样数得到；以检测的 `ok(t56)` 为准）；Mooncake 的计时又写回 vLLM 日志的 `[BatchPutTiming]` 行（面板留在 v3.3-extra 时不读这些行，P 后台 put 下 3 段为空）。

## 判失败的条件

对全部实例判。下表任何一条出现，这次升级算失败：停下，整个集群按第 34 步处理（第九部分回滚到 T56，换过 Mooncake 的同时换回 v1）。**停下、带回、不回滚**的另列在表后。

| 步骤 | 失败条件 |
|---|---|
| 22 | 导入核对不是 `True`，或者报错 |
| 23 | 有实例的 `/health` 不是 `200`，或者起不来 |
| 24 | 命令行和停机前不同；有 vLLM 进程没有设 `VLLM_LOGGING_CONFIG_PATH` |
| 24 | vLLM 日志里有 `ImportError`，或者 `Traceback` 后面几行里有 `pd_diagnostics`、`pd_device_timing`、`worker_base`、`ascend_store`、`mooncake_connector`、`recompute_scheduler` |
| 25 | `verdict` 的 vllm、ascend、p2d 三项有一项不是 `ok(t58…)`，包括只升了一半（例如 `ascend=ok(t56…)`） |
| 25 | 出现 `BASELINE_DIFF`；`run` 行是 `logcfg=unset`，或者行末不是 `diag_level=3(default)`；`site_patches` 和第 9 步不同 |
| 26 | D 的 EngineCore 不是 `AsyncRecomputeScheduler`；D 机器上带 `runner_stages=` 的 `tp_rank=0` 行数少于 D 实例数，或者列的不是那 6 个 stage（`forward,sampling,prepare_inputs,lm_head,draft,bookkeeping`；日志追加时升级前的旧行不算）；P 的 Worker 那一行有 `runner_stages=`；`model_runner` 不是 `NPUModelRunner`；`BalanceScheduler` 带 `balance=True` |
| 26 | D 上 `tp_rank=0` 的 `device_timing` 是 `off` |
| 26 | 这次拉起以后没有 `diag level=` 的行，或者不是 `level=3` |
| 27 | vLLM 日志里有 `undefined symbol` 或 `libmooncake_store` 的报错 |
| 28 | 有 D 实例 `cand_maps` 是 0，或者 `no_execution_id` 大于 0 |
| 31 | D 的 `forward_empty_pct` 超过 1% 而且 `forward_empty` 比第 29 步大，或者 sampling 的这两项同样如此 |
| 31 | D 两次的 `pid_list` 不同 |
| 31 | 第二次比例为空、`ended_win` 大于 0、`forward_win` 或 `sampling_win` 大于 0 |
| 31（磁盘） | 节点目录每天增长 × `NODE_LOG_KEEP_HOURS` ÷ 24，再加这台机器的实例数 × 1 GiB（D 机器 16 GiB，P 机器 1 GiB），超过第 17 步 `df` 里节点目录所在文件系统 `Avail` 的一半 |
| 30、31（磁盘） | vLLM 日志里有 `[PD_DIAG]` 行（一出现就处理，不等满 30 分钟） |
| 32 | 面板的 `check` 不是 `RESULT: OK` |

**停下、带回、不回滚：**
- 第 9 步前三项不是 `ok(t56…)`、运行模式不对（这时补丁还没改）；
- 第 11、12、16 步不对（同上）；
- 第二部分换面板不对（按那一部分的回滚一节处理面板，补丁不动，不往下做）；
- 第 14 步是 `STOP_…`（这个容器不换 Mooncake，补丁照常升级）；
- 第 21 步导入核对报错（这个容器换回 v1，补丁照常升级）；
- 第 27 步没有文件、或者有记录被丢掉（补丁不回滚，带回）；
- 第 35 步有 `FAIL`：这些是数据核对，服务不受影响；
- 第 36 步是 `SLOWER`：按那一步改环境变量。

**不算失败、带回的：**
- 第 9、25 步的 `mooncake=BAD(other)`（两步相同时，Mooncake 那几步跳过）；
- 第 31 步 P 的留空比例；
- 第 33 步拉起后 TTFT、TPOT 大 10% 以上（开销的判据看第 36 步）。

## 带回清单

| 步骤 | 带回 | 每份 |
|---|---|---|
| 1 | 3 遍各自的三行，用的是甲还是乙 | 一份 |
| 第二部分 | 那一部分各步写的带回 | 一份 |
| 8 | `df -hT /tmp` 那一行 | 每个容器 |
| 9 | 检测脚本的全部输出；`baseline_token`、`mooncake=` | 每个容器 |
| 11 | 不对时的输出 | 每个容器 |
| 12 | 两条命令的完整输出 | P、D 各一份 |
| 13 | `<备份目录>` | 每个容器 |
| 14 | 核对脚本和后两条的全部输出 | 换 Mooncake 的每个容器 |
| 15 | `<MC 备份目录>` | 换 Mooncake 的每个容器 |
| 16 | 两条命令的完整输出和 `<S58>` | P、D 各一份；`<S58>` 每个容器 |
| 17 | `cmdline.before`、`df` | 每个容器；`df` 每台机器 |
| 21 | 三条的输出 | 换 Mooncake 的每个容器 |
| 22 | 导入核对那一行 | 一份 |
| 24 | 命令行比较结果、日志配置那一行 | 每个容器 |
| 25 | 检测脚本的全部输出 | 每个容器 |
| 26 | 启动行（`PD_DIAG_STARTUP` 那几行） | 每台机器 |
| 27 | 三条的输出 | 每台机器 |
| 28 | 自检查询的输出 | 一份 |
| 29、31 | 留空比例两次的输出 | 一份，含 D、P 每个实例 |
| 31 | 磁盘那一行 | 每台机器 |
| 32 | 面板 `check` 的全部输出 | 一份 |
| 33 | 停机前、拉起后的 D 的 TTFT、TPOT，P 的 TTFT | 一份 |
| 35 | 整份输出，`<D 实例>`、`<开始时刻>` | 一份 |
| 36 | 3 遍各自的三行、判据那一行 | 一份 |
| 37 | 那一步写的带回 | 一份 |
| 34 | 失败时：检查输出、vLLM 启动日志、第 28 步的输出 | 失败时 |

## 附录 A　原因和背景

**A.1 这次加了什么（记录格式约定 1.5.1 第 8 节）。** 一个环境变量 `PD_DIAG_LEVEL` 控制记到第几级，vLLM、vllm-ascend、p2d、Mooncake 读的是同一个变量，同一张解析表：

| 值 | 级别 |
|---|---|
| 不设、空 | 3（`source=default`） |
| 恰好是 `1`、`2`、`3` | 那一级（`source=env`） |
| 其余任何值（`0`、`4`、`abc`、` 2 `、`2x`） | 3（`source=invalid`） |

- 3 级：全记，和 T56 逐字段相同。本手册不设它，就是 3 级。
- 2 级：不记 L3 的那些段；1 级：只记 L1，schedule 的子段照记。各级不记哪些字段，见约定 1.5.1 第 8.2 节。
- 1、2 级下，请求一级的汇总记录里多一个 `diag_level`；3 级不写（没有这个字段就是 3 级）。
- EngineCore、Worker 启动时各写一行 `PD_DIAG_STARTUP diag level=<n> source=<…>`。

**A.2 Mooncake 的计时改写进自己的文件（补丁 04b）。** 计时本身不变（同样的四段、`timing_scope`、`complete`、`failed_keys`），只是不再写进 glog 和 vLLM 日志：
- 写到节点目录下 `logs/mooncake/mc.<启动时刻>.<pid>.<序号>.log`，每个进程一个文件，写满 256 MiB 换一个序号，超过 12 小时的删掉；
- 每行一条 `[PD_DIAG]` 加 JSON 的记录，和节点文件同一种写法；多了 `pid`、`native_tid`、`wall_ns`、`submitted_keys`、`submitted_bytes`，面板二用它们和 vllm-ascend 那边同一次调用的记录对上；
- 调用线程只拼一行放进队列，另有一个后台线程每 200 毫秒写一次；队列满了就丢、计数（每分钟一行 `mc_timing_stats`）；
- `PD_DIAG_LEVEL` 恰好是 `1` 或 `2` 时不写，`MC_PD_TIMING=0` 时也不写，都不退回标准错误。
- v2 和 v1 导出的名字逐个相同，头文件逐字相同，所以换库不影响别的库和 Python 模块。

**A.3 为什么先换面板。** 新过滤器认 `diag_level`，过滤器程序变了，要重启 vLLM 才加载；新的段定义旧过滤器也读得了。所以先换面板（不重启），再停机换补丁，新过滤器随拉起一起加载。

**A.4 3 级就是 T56。** 默认不设 `PD_DIAG_LEVEL` 时，P 上写出的东西和 T51 逐字段相同，D 上的和 T56 逐字段相同（实验室的记录等价测试）。所以判据、首批核对、开销分析都和 T56 时相同。

## 附录 B　已知问题

1. **第 35 步的两个 1%：** 「设备值缺失 ≤ 记录数的 1%」「第二步起 `NONE` ≤ 1%」是阈值，不是预测，和 T56 时相同。
2. **Mooncake 文件的磁盘：** 按一行约 456 字节估，P 机器最重时每天约 63 GB，只留 12 小时（约 31.5 GB）。这是估的，第 31 步的磁盘一行是实测。
3. **进程退出时最后不到 200 毫秒的 Mooncake 记录会丢：** 写线程不 join、不刷盘，换来的是退出时不会卡住。
4. **补丁包里有 `t51`、`t56` 字样的文件：** `patches/p2d/t56/` 下是 T56 版的 `replay_p2d.py`，字节不变，只用在第 12 步证明现场文件是 T56 重放出来的；检测脚本仍叫 `t51_glm53_check.py`（版本 glm53-8），核对脚本仍叫 `t51_mc_precheck.py`（版本 glm53mc-6）。

## 附录 C　没在 NPU 上、在现场测过的部分

- **Mooncake v2 在 NPU 上。** v2 在实验室的同一镜像里编、在 CPU 上用 TCP 传输测过（写文件、开关、粒度、退出 100 次、fork、目录建不了、切分、清理、队列满）；在卡上、用现场的传输方式没有跑过。
- **1、2 级。** 本手册只用 3 级。1、2 级在实验室的 CPU vLLM 上测过记录内容；在现场没有跑过。
- **开销。** 3 级下和 T56 相同的代码路径，多出的是每个进程第一次用到时读一次级别、以后沿用，以及 Mooncake 改由后台线程写文件；对 TPOT 的影响没有在卡上量过，所以有第一、七部分。
- **整体停、整体拉起。** 实验室走查用的是替身进程，不是真实的 vLLM。

## 附录 D　文件和 sha256

| 文件 | sha256 |
|---|---|
| 补丁包 `t58-glm53-patch-kit.tar` | `4f59ea877db09e46…`（全长见 `.sha256` 文件） |
| 检测脚本 `detect/t51_glm53_check.py`（glm53-8） | `b7b11e295a73ba91…` |
| vLLM 完整补丁 `patches/vllm/t58-vllm-v0.30.0-pd-diag.patch` | `cb5b63585b396fff91f1c47fc5e0bf33abdafede7828acef15825f6db5f4e236` |
| vLLM 增量补丁 `patches/vllm/t58-vllm-from-t56.patch`（7 个文件） | `9aec9b72ee4aed520cbfa9c5328bf4368a1f18785f3b2a5f5d9fd50eb3f4076f` |
| `patches/vllm/SHA256SUMS.after` | `999d4f72278da0d692f0c84c11b007d2580581b9d052f8193ae7bfe518f5ca33` |
| vllm-ascend 合并补丁 `patches/vllm-ascend/t58-vllm-ascend-d4ad49796-pd-diag.patch` | `36d37dd6056618420dc5873db1b205f43f6d677a779c843bab590c8e9c65bf34` |
| `patches/vllm-ascend/SHA256SUMS.after` | `5467154cd8f3b7f6234503e6c89df4b2f91d5649ba08e5e4b3fa56b345cc69c0` |
| `patches/p2d/replay_p2d.py`（T58） | `e62795bd63b146eb6813865d039d574d1fff067e1654f56f02a4458ca5e4a9e7` |
| `patches/p2d/t56/replay_p2d.py`（T56，第 12 步证明用） | `9b12baa6b38c8ff7d697c450ad80a9e0b48889f384dcdbddc33e04c2ee5e553e` |
| Mooncake 增量包 v2 `t58-glm53-mooncake-kit.tar` | `e3e338d5cd5a95d4…`（全长见 `.sha256` 文件） |
| 增量包里的替换库 v2 `lib/libmooncake_store.so`（15402392 字节） | `37a3aaf8acb53c6a3cdb9251b9e6d3e1edc0c13789c8d4138d111b1d71cc4a6f` |
| 增量包里的换库前核对 `precheck/dist/t51_mc_precheck.py`（glm53mc-6） | `559782ea1dfb29c1b2ec2a2016b3423f0a6fe9e8869732dd1970a231e9262327` |
| 现在在位的替换库 v1（第 15 步备份的就是它） | `3131784385846064327e858d6dcdb11a6c7674c2162964d52f9ed0e866b52bb8` |
| 面板包 `面板二v3.3-extra/vllm-pd-grafana-panel2.tar.gz`（第二部分） | `d071644c0f3dd8180919e2c6679f7929e15f4d84351f55a3c7d48dd3bd6895ed` |
| 更新脚本 `面板二v3.3-extra/vllm_pd_update_pack2_v33extra.py`（第二部分） | `1ff92a2ef696162d0c8736099f97a09592563faf7e7a56799b00f5b06472b647` |
| 已填路径的更新脚本 `vllm_pd_update_pack2_v33extra_filled.py`（只放私有 Caching；易读版的命令用它） | `1206480c34880880fe8e59c79b0b465c729ac2030cf12263e9b29d434e0a2fee` |
| 第一步开销分析的查询 `第一步开销分析.sql`（t41，第 37 步原样放在命令里） | `8e7a6f9ba75cbcc9d9b0a6542066adb531d9dcf9764e16d37bd1e0857992a7b2` |
| 现场的 `mooncake_connector.py`（打 T51 以前的原文件，第 12 步从 T51 的备份里取出） | 前 16 位 `2c4acb3eeb1ced7d` |
