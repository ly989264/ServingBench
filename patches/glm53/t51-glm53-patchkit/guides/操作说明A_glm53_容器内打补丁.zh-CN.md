# 操作说明 A：在运行中的 glm5.3 容器里打补丁

**状态：** 第 11 步（Mooncake，可后补）等另一部分材料，标「待补」。其余步骤的命令已在实验室容器里按原文走过（第 10 节）。

**适用范围：** glm5.3 测试集群（面板二里的集群 `test-glm53`）的 P、D 容器。不新建镜像，不新建容器，改动写在容器的可写层里。

<!-- 占位符版开始 -->
> **本文有两版。** 这一版是占位符版：路径写成尖括号里的说明，不含任何内网信息。已填路径版把监控机上的个人路径填好，只放在私有的 Caching 仓库。
<!-- 占位符版结束 -->

## 0. 先读这几条

- **打三件，顺序固定；Mooncake 可后补。** 每个容器里按 vLLM、vllm-ascend 主体、p2d 重放的顺序打（第 4.1、4.2、4.3 步）。每一件都先核对现场文件的 sha256，再备份，再动文件，动完再核对一次。Mooncake 库的替换是第 11 步，可以以后单独做，前 1–10 步不依赖它。
- **几件补丁必须一起装、一起回滚。** vllm-ascend 主体和 p2d 用到的下面这些名字只有 vLLM 补丁里有；只装 vllm-ascend 或 p2d、不装 vLLM 补丁时，Worker 启动就会 ImportError，vLLM 起不来：
  - `vllm.v1.metrics.pd_diagnostics` 模块；
  - `vllm.v1.worker.worker_base` 的 `pd_execution`、`pd_sample`、`pd_claim_finish`；
  - `SchedulerOutput.pd_execution_id`。
- **和《新增 test-glm53 集群操作说明》第 7 步合成一次重启。** 第 7 步给 vLLM 加两个挂载、两个环境变量，接上面板二的过滤器：
  - 要加挂载、得重建容器的，先重建容器、加好环境变量，再打本说明的补丁。重建容器会丢掉已经打的补丁。
  - 不用重建的，两件事做完以后一起重启一次（本说明第 7 步）。
  - **两个环境变量必须在打完补丁后的第一次重启时就生效。** 其中 `VLLM_LOGGING_CONFIG_PATH` 指向面板二的 `pd_diag_logging.json`，补丁写的 `[PD_DIAG]` 记录才只进过滤器。现场现在没有设（第 2 次检测 `logcfg=unset`）。漏设时，原始记录全部写进 vLLM 日志：一台 D 机器每天约 1.4–13 TB，几小时到几天就能写满盘（第 6 步结论四）。第 7.2 节在重启后核对。
- **不设 `MC_LOG_DIR`。** 打补丁和重启时都不要加这个环境变量（原因见第 11 步）。
- **现场的本地补丁不用动，但打补丁前先核对它们没变（第 3 步）。** 这一条的结论，依据的是小核对 glm53f-3b 带回的这两个补丁文件的版本（下面的 sha256 前 16 位就是那次报的）和替换方式，以及 vLLM 一侧按这种替换方式做的模拟（vLLM 说明第 13 节）。vllm-ascend 的 `patch/platform/` 下有两个现场加的文件，两处替换都是包装，调用原函数，补丁的埋点都保留：
  - `patch_kv_load_failure_reason.py`（P、D 都有，sha256 前 16 位 `d7c764bb425993c0`）包装 `OpenAIServingCompletion.completion_stream_generator`：把输出原样逐个转交、记下 KV 加载失败的标记，再调用原函数，改写原函数产出的输出块。补丁在这个函数里的三处记录都在原函数里；模拟的 8 个场景，记录和不替换时逐条相同。改写输出块只改变发给客户端的内容。多出的这一层转发没有测耗时。
  - `patch_prefill_length_validation.py`（只在 P 上，`c36392a6338bc2f1`）包装 `AsyncLLM._add_request`：先做长度校验，再调用原函数。关键字参数原样传下去时，记录和不替换时逐条相同；校验的耗时算在 P 的 add_request 段里，没有测；校验不通过的请求，`engine_submission` 记录的状态是 `rejected`，没有 add_request 段。n>1 的请求，每个子请求都经过 `_add_request`，和原版 v0.30.0 相同。
  - 现场还改过 `patch/__init__.py`、`patch/platform/__init__.py`，第 3 步同样核对它们的 sha256 前 16 位，预期值是第 2 步检测脚本 `site_patches` 一行报出的值。
  - **停止条件：** 第 3 步核对这四个文件的值，任何一个对不上，说明现场的本地补丁变了，停下，把输出报回来。
  - 第 3 步另有一条只读的 `grep`，看这两个补丁文件是不是调用原函数、把参数原样传下去。它不是停止条件：`_add_request` 的包装没有把 `**kwargs` 原样传下去时，补丁照样打，服务不受影响，只是 P 的 add_request 段为空，把那条命令的输出带回来。
- **p2d 的停止条件（第 4.3 步）：** 重放的定位结果和小核对 f-3b 不同（除 C25 以外还有别的编号用了备选锚点，或 C25 回到 `primary`），或者 `p2d_extra` 那一行不是 `p2d_extra X1=1 X2=1/1 X3=2 X4=4/1 X5=1 X6=ok`，就停下。
- **备份必须拷回监控机（第 3 步）。** 现场的 `mooncake_connector.py` 是本地修改版，补丁包里没有这一份，丢了无法重建。第 3 步的备份拷到监控机、核对通过以后，才做第 4 步；回滚（第 9 步）用监控机上的这一份。
- **分批上线：** 先一台 D 机器的容器，再其余 D 机器，P 最后（第 7 步）。每批重启前记下各实例的命令行，重启后要逐项相同；每批做完、看到第 7 步写的结果，再做下一批。
- **核对命令的退出码。** 文中 `LC_ALL=C sha256sum -c … | grep -v ': OK$'` 这类命令，没有输出就是全部通过，这时整条管道的退出码是 1（`grep` 没有选出任何行）。所以不要把它们放进带 `set -e` 的脚本，按输出判断。
- **`docker restart` 以后补丁还在；`docker rm` 后重建容器、或者换镜像，补丁就没了，** 要重新做一遍。
- 所有容器里的命令都以 root 执行：先在那台机器上 `docker exec -it <容器名> bash` 进入容器。
- 任何一步的结果和「执行后马上会看到」不一样时，停下，把那一步的输出带回来，不要继续。

### 0.1 和 glm5.3 现场的对应

现场的事实取自第 1、2 次环境检测和小核对 f-1、f-3b；验证依据取自 vLLM、vllm-ascend、p2d、Mooncake 各部分的交付说明（本包 `patches/*/README.zh-CN.md`、`t51-glm53-kit` 的 Mooncake 说明）。vLLM 一侧的调用路径复核在本包的 vLLM 补丁里（vLLM 说明第 13 节）；vllm-ascend 一侧的调用图审查还在做，结果到了以后补进这张表。

| 现场 | 包里怎么验证 | 没有验证的 |
|---|---|---|
| **vLLM v0.30.0 原版**（检测 `vllm=… state=v0.30.0 changed=0`，装在 `site-packages`） | 补丁以干净的 v0.30.0 为基线：23 个要改的文件对照 `SHA256SUMS.before` 0 处不符，`git apply` 通过，打完和替换件相同（vLLM 说明 9.1）；15 个合成场景的记录和 v0.32.0 加 01 逐条对照（9.3）；调用路径复核：原版经过、移植后不再经过的调用 0 处，n>1 的子请求改回经过 `_add_request`（第 13 节）；现场补丁只含 `vllm/` 下的 25 个运行时文件（t51-glm53-vllm `40152b2`），另加三行启动日志 `PD_DIAG_STARTUP`，报出运行时实际用的调度器和 runner（第 14 节，第 8 步核对）；第 3 步在现场再核对一次，不符就停；实验室在干净 v0.30.0 上照本文原文走过（第 10 节）。V2 runner 的两个文件（`v1/worker/gpu/kv_connector.py`、`gpu/model_runner.py`）在现场用不到（检测 `runner=V1(env)`），因为和 dsv4.1 的补丁共用而保留，照样核对（本包 README） | 没有在昇腾、真实模型、多进程 ZMQ、DP 协调器上跑（vLLM 说明 11.1） |
| **vllm-ascend `d4ad49796`，现场改了 3 个文件、加了 55 个**（检测 `ascend=… state=modified changed=3 added=55`；改的是 `kv_p2p/mooncake_connector.py`、`patch/__init__.py`、`patch/platform/__init__.py`，加的是 `_cann_ops_custom/` 52 个、`patch/` 2 个、根目录 1 个） | 本补丁改的 11 个文件现场都没动：第 3 步按 `SHA256SUMS.before`（9 个）、`NEW_FILES.txt` 核对，不符就停。现场改过的 `mooncake_connector.py` 不直接替换，第 4.3 步用 `replay_p2d.py` 在现场那一份上重放：小核对 f-1、f-3b 在现场文件上测得 28 处全部定位（只有 C25 用备选锚点）、X1–X6 和原版相同。现场改过的两个 `__init__.py` 第 3 步核对 sha256 前 16 位（预期值取第 2 步检测的 `site_patches`）。实验室在干净 `d4ad49796` 和模拟的现场修改上都走过 | `_cann_ops_custom/` 的 52 个新文件没有逐个看；在卡上跑（vllm-ascend 说明第 8 节） |
| **Mooncake `78566d4`，本地构建**（检测 `mooncake_build git=78566d4 wheel_tag=cp311-cp311-manylinux_2_38_aarch64`，glog 0.6.0） | 第 11 步（可后补）【待补】。替换件按现场的选项编：同一镜像 `quay.io/ascend/cann:9.2.0-a3-openeuler24.03-py3.11`，去调试信息后重链接成现场的 NEEDED、RUNPATH、SONAME；核对件和现场原库的指纹除导出名哈希外都相同；实验室加载和 put 测试通过（`t51-glm53-kit` `c91bfdc`） | 替换前核对的判据待定（导出名哈希不同）；没有在现场替换过 |
| **Python 3.11.10** | 实验室走查用 `python:3.11.10-slim` 镜像，替换和重放以后的 25 + 11 + 1 个文件都能 `py_compile`（第 10 节）；vLLM 补丁另在 3.11.2 下对 25 个运行时文件做过语法解析，0 处错误（vLLM 说明 11.3） | 完整的单元测试是在 3.12 上跑的（vLLM 3.12.3，vllm-ascend `python:3.12-slim`），没有在 3.11 上跑 |
| **CANN 9.2.0** | Mooncake 替换件在 CANN 9.2.0 的镜像里编、测（同上）；C15 设备计时的前提在现场容器里确认过（下一行）。补丁的 Python 代码不直接调 CANN 接口 | 整条链路没有在卡上跑 |
| **torch_npu 2.10.0.post4** | 用户在现场 glm53 容器里跑过设备事件探针 `probe/npu_event_probe.py`（torch 2.10.0a0、torch_npu 2.10.0.post4，aclgraph replay）：设备事件 2.010 ms，host 计时加同步 2.024 ms（vllm-ascend 说明第 8 节） | C15 的整条链路（推迟写出、读取挂钩）没有在卡上跑；探针只跑过图模式，P 的 eager 下没有测过 |
| **P：TP16、DP8（每个容器 1 个实例）、eager、同步调度、MTP k=1** | vLLM 合成场景 E1（同步调度）、E7（P 同步加载，`stages.submit` 的范围）；vllm-ascend 3.1（同步加载记 `load`）、3.2（MTP 下 forward、sampling、draft 的口径，读码）；面板二端到端 glm53 一套（P 同步加载、只取 rank 0）和过滤器的 TP2 拆进程测试 | 多进程的 TP16、DP8 没有真实运行，TP>1 只在单进程里模拟（p2d、vLLM 说明）；eager 下的设备事件没有在卡上测过（《面板系统说明书》已知问题第 32 条） |
| **D：TP1、每个容器 16 个实例、图模式、async scheduling、MTP k=5、`AsyncRecomputeScheduler`** | vLLM 合成场景 E2（D 异步取远端 KV、k=5）、E5、E6（async scheduling，两步重叠，k=5）；vllm-ascend 第 11 节：`RecomputeScheduler` 改成经 01 的包装，调度器记录比对 363 项通过、2 项按约定预期不同，联合检查 D 走真实的 `AsyncRecomputeScheduler.schedule()`，130/130；forward、sampling 只用设备计时，取不到留空（C15）；面板二的实验室 D 容器起 8 个实例，过滤器按端口分目录 | 图模式下的整条链路没有在卡上跑；`DyntraLBRecomputeScheduler`（要 DP>1）没有跑；每个容器 16 个实例没有在实验室起过 |
| **现场两个本地补丁**（`patch/platform/patch_kv_load_failure_reason.py`，P、D；`patch_prefill_length_validation.py`，只在 P） | 小核对 f-3b 报出两处替换的目标和写法：`_add_request` 的包装 `wraps_orig=1`；`completion_stream_generator` 的包装以 `@functools.wraps(original)` 开头，内部把输出原样转交给原函数。vLLM 一侧据此判定两处都是包装（vLLM 说明第 13 节），按这种写法模拟（`review/field-patches`，HTTP 场景 F1–F8）：两个包装单独装、一起装，记录都和不替换时逐条相同；`_add_request` 的包装在 n=1、2、4 时分别被调用 1、2、4 次，和原版 v0.30.0 相同。第 3 步核对两个文件的 sha256 前 16 位（不符就停），另有一条只读的 `grep` | 两个文件的全文我们看不到；调用原函数的那几行在小核对截断的 diff 里，没有直接看到，由第 3 步的 `grep` 补上；包装多出的耗时没有测 |

### 0.2 文中的占位符

<!-- 占位符表开始 -->
**文中的占位符：**

| 占位符 | 含义 | 在哪里查 |
|---|---|---|
| `<用户目录>` | 监控机上放部署包和更新脚本的目录 | 包二 v3 更新脚本「现场路径」一段的 `USER_DIR` |
| `<总目录>` | 面板目录的上级目录 | 同一个脚本的 `ROOT` |
<!-- 占位符表结束 -->
| `<SSH 账号>`、`<机器地址>` | 从监控机登录 glm5.3 机器用的账号和机器 | 和新增集群说明的清单相同 |
| `<机器>` | 这台机器的简称，只用在监控机上备份目录的名字里；同一台机器每次写法相同 | 自己定，例如清单里的机器序号 |
| `<容器名>` | 这台机器上跑 vLLM 的容器 | 在那台机器上 `docker ps` |
| `<备份目录>` | 第 3 步在容器里建的备份目录 | 第 3 步的输出 |
| `<时间戳>` | 备份目录名末尾的 `<年月日>-<时分秒>`，例如 `/tmp/t51-glm53-backup-20261120-103000` 的 `20261120-103000` | 第 3 步的输出 |
| `<节点目录>` | 面板二的过滤器在这台机器上写文件的目录（宿主机路径），下面有 `logs/<端口>/` | test-glm53 的 `deploy.env` 的 `NODE_LOG_DIR` 去掉末尾的 `/logs` |
| `<vLLM 日志文件>` | 这台机器上各个 vLLM 实例的标准输出、标准错误重定向到的文件（宿主机路径），几个文件用空格隔开；D 机器有 16 个 | P 上就是新增集群说明第 9 步填进 `MOONCAKE_LOG_FILES` 的文件；D 上是同一类文件 |
| `<启动文件目录>` | 容器里 `run_dp_template.sh`、`launch_online_dp.py`、`start_p.sh`（D 上 `start_d0.sh`）、`mooncake.json`、`start_mooncake.sh` 所在的目录；不在同一目录的，按各自的路径写，缺哪个就不传哪个 | 那台机器上拉起 vLLM 的方式；第 1 次环境检测 |
| `<容器里的 vLLM 日志文件>` | `<vLLM 日志文件>` 在容器里看到的路径（vLLM 标准输出所写的文件）；容器里看不到就不传 `--startup-log` | 同上 |
| `<这一批的实例>` | 面板一里这一批实例的名字，用竖线连起来，例如 `D0\|D1\|D2` | 面板一的实例名按清单的顺序排：清单里 D 一侧第 n 项（从 0 数）是 `Dn` |
| 端口 | 正文直接写了现场的端口：D 的容器是 `$(seq 9100 9115)`（16 个实例，9100–9115），P 的容器是 `9900`。现场的端口不同时，按同样的格式改：连续的写成 `$(seq <第一个> <最后一个>)`，不连续的用空格隔开逐个列出 | 和新增集群说明的清单相同 |

**对同机其他服务的影响（总的）：** 第 1–6 步只往容器里写文件，正在运行的 vLLM 不受影响，还在用内存里的旧代码。第 7 步重启这个容器里的 vLLM 实例，重启本身和平时一样；但重启以后多了持续写入：面板二的过滤器往 `<节点目录>` 写；做了第 11 步以后，换了 Mooncake 的实例还往 vLLM 日志写 `[BatchPutTiming]` 行。每天的量见第 6 步。替换完以后尽快重启：重启之前，运行中的进程如果第一次用到某个被换掉的模块，会读到新文件，新旧代码混在一个进程里。

## 1. 把补丁包放进容器并核对

**机器：** 先在监控机上，再在 glm5.3 的那台机器上，最后在容器里。

补丁包 `t51-glm53-patch-kit.tar` 和它的 `t51-glm53-patch-kit.tar.sha256` 放在监控机的 `<用户目录>/vllm-pd-patch-glm53/` 下。在监控机上先核对，再拷到那台机器：

```bash
cd <用户目录>/vllm-pd-patch-glm53 && LC_ALL=C sha256sum -c t51-glm53-patch-kit.tar.sha256
```

**执行后马上会看到：** `t51-glm53-patch-kit.tar: OK`。

```bash
scp <用户目录>/vllm-pd-patch-glm53/t51-glm53-patch-kit.tar <SSH 账号>@<机器地址>:/tmp/
```

在那台机器上，拷进容器：

```bash
docker cp /tmp/t51-glm53-patch-kit.tar <容器名>:/tmp/
```

在容器里解开、核对：

```bash
mkdir -p /tmp/t51-kit && tar -xf /tmp/t51-glm53-patch-kit.tar -C /tmp/t51-kit && cd /tmp/t51-kit && LC_ALL=C sha256sum -c SHA256SUMS | grep -v ': OK$'
```

**执行后马上会看到：** 最后一条命令没有任何输出。有输出说明补丁包在拷贝中损坏，重新拷贝。

再看容器的 `/tmp` 在什么文件系统上：

```bash
df -hT /tmp | tail -n 1
```

**执行后马上会看到：** 一行，第二列是文件系统类型，一般是 `overlay`（容器的可写层）。是 `tmpfs` 时，`/tmp` 下的文件在容器重启以后就没了：第 3 步会自动把备份放到 `/root` 下；解开的 `/tmp/t51-kit` 重启后也不在了，回滚时按第 9 步重做。把这一行带回来。

那台机器上的 `/tmp/t51-glm53-patch-kit.tar` 留着，不要删：容器的 `/tmp` 可能被清空，回滚（第 9 步）时要用。

**对同机其他服务的影响：** 只多了一个约 4 MB 的文件和解开的目录。

## 2. 打补丁前的检测

**机器：** 容器里，接着第 1 步。检测脚本在解开的补丁包里（`/tmp/t51-kit/detect/t51_glm53_check.py`，sha256 `f9e6073f…`，第 1 步的核对已经确认过它）。每个要打补丁的 P、D 容器各跑一次。脚本只读，几秒内结束，只往 `/tmp` 写一个明细文件；各行的含义和它对环境的影响见补丁包里的 `detect/使用说明.zh-CN.md`。

P 的容器：

```bash
python3 -I -S -B /tmp/t51-kit/detect/t51_glm53_check.py --run-dp-template <启动文件目录>/run_dp_template.sh --launch-online-dp <启动文件目录>/launch_online_dp.py --start-script <启动文件目录>/start_p.sh --mooncake-json <启动文件目录>/mooncake.json --start-mooncake <启动文件目录>/start_mooncake.sh --startup-log <容器里的 vLLM 日志文件>
```

D 的容器（`--start-script` 是 `start_d0.sh`）：

```bash
python3 -I -S -B /tmp/t51-kit/detect/t51_glm53_check.py --run-dp-template <启动文件目录>/run_dp_template.sh --launch-online-dp <启动文件目录>/launch_online_dp.py --start-script <启动文件目录>/start_d0.sh --mooncake-json <启动文件目录>/mooncake.json --start-mooncake <启动文件目录>/start_mooncake.sh --startup-log <容器里的 vLLM 日志文件>
```

- `-I -S -B` 三个参数缺一个，脚本只打印一行 `REFUSED`，什么都不读。
- 5 个启动文件里缺哪个就不传哪个；没有 vLLM 日志文件就不传 `--startup-log`。
- 现场的启动文件里没有 `vllm serve` 这一行，文件和运行中的进程不一致时，脚本以运行中的进程为准。

**执行后马上会看到：** 不超过 20 行，退出码 0。其中这几行要看：

- `vllm=… state=v0.30.0 …` 和 `ascend=… state=modified …`，两行末尾都是 `from=site cands=1`（第 3 步拿它和找到的目录对照）。
- `site_patches patch/__init__.py=<16 位> patch/platform/__init__.py=<16 位> patch/platform/patch_kv_load_failure_reason.py=d7c764bb425993c0 patch/platform/patch_prefill_length_validation.py=c36392a6338bc2f1`；D 上最后一项是 `absent`。后两个和这里不同，就停下，把输出带回来。前两个记下来，是第 3 步的预期值。
- `inst[run …]`：每组配置相同的实例一行。
- `run` 行里是 `logcfg=unset`：面板二的日志配置要等第 7 步重启才生效。
- `verdict vllm=unpatched ascend=unpatched(site_local=<n>) p2d=unpatched mooncake=not_replaced`：还没打补丁。同样是 `unpatched`，在第 2 步是预期值，在第 8 步是失败。`site_local=n` 是现场本地改动的文件数（不算错）；`strict` 行末是 `p2d=not_replayed`。
- `mooncake=not_replaced` 表示是现场原装的 Mooncake 库（小核对 f-1 带回的那一份）。出现 `mooncake=BAD(other)`，说明现场的 Mooncake 库既不是原装的那一份，也不是我们交付的替换库：把这一行带回来；第 1–10 步不受影响，照常做，第 11 步要等我们看过再决定做不做。
- 最后一行 `fingerprint=… baseline_token=<一串>`：把 `baseline_token=` 后面的整串记下来（每个容器各一串），第 8 步要用。这一串是这个容器里各组实例的 kind、conn、spec、eager、async、load_async、tp、dp 和调度器。

只打印一行 `BLOCKED` 时（脚本的自查认出输出里有 IP、主机名之类），把这一行带回来，不往下做。

**带回来的东西：** 终端打印的全部行；指纹（`fingerprint=` 后面的 16 位）相同的容器只带一份，但 `baseline_token` 每个容器都记下。带回以后删掉明细文件（路径照抄 `detail=` 那一行）：

```bash
rm -f <detail= 那一行的路径>
```

**对同机其他服务的影响：** 只读；按 `nice 19`、IO 空闲级运行，读取量上限 1 GiB，耗时上限 300 秒（使用说明「对环境的影响」一节）。

## 3. 找到安装目录，核对，备份

**机器：** 容器里；最后在那台机器上和监控机上拷备份。

找到 vLLM、vllm-ascend 装在哪里（只查找位置，不导入 vLLM）：

```bash
VLLM_PKG=$(python3 -c "import importlib.util as u; print(u.find_spec('vllm').submodule_search_locations[0])")
ASCEND_PKG=$(python3 -c "import importlib.util as u; print(u.find_spec('vllm_ascend').submodule_search_locations[0])")
VLLM_ROOT=$(dirname "$VLLM_PKG"); ASCEND_ROOT=$(dirname "$ASCEND_PKG"); F="$ASCEND_PKG/distributed/kv_transfer/kv_p2p/mooncake_connector.py"
echo "PYTHONPATH=[${PYTHONPATH:-}]"; echo "$VLLM_ROOT"; echo "$ASCEND_ROOT"; echo "$F"
```

**执行后马上会看到：** 四行。

- 第一行是 `PYTHONPATH=[]`：这个 shell 没有设 `PYTHONPATH`，上面找到的就是装在 `site-packages` 里的那一份。
- 后三行：现场两个包都装在 `site-packages` 里，所以第二、三行是同一个 `site-packages` 目录，第四行是它下面的 `vllm_ascend/distributed/kv_transfer/kv_p2p/mooncake_connector.py`。
- 再和第 2 步检测结果的 vllm、ascend 两行对照：两行都应当是 `from=site cands=1`，即运行中的 vLLM 也从 `site-packages` 导入，只找到这一份。新增集群说明第 7 步会把面板二的 `conf` 目录加进 vLLM 进程的 `PYTHONPATH`，那个目录里只有面板二的三个节点文件，没有 `vllm`、`vllm_ascend`，不改变这个结果。
- 第一行方括号里不为空，或者第 2 步不是 `from=site cands=1`，说明这里找到的目录可能不是运行中的 vLLM 用的那一份：停下，把这四行和第 2 步那两行带回来。

核对要改的文件就是公开版本，要新加的文件还不存在，现场的本地补丁没有变：

```bash
K=/tmp/t51-kit/patches
(cd "$VLLM_ROOT" && LC_ALL=C sha256sum -c $K/vllm/SHA256SUMS.before | grep -v ': OK$'; for f in $(cat $K/vllm/NEW_FILES.txt); do [ -e "$f" ] && echo "已存在：$f"; done)
(cd "$ASCEND_ROOT" && LC_ALL=C sha256sum -c $K/vllm-ascend/SHA256SUMS.before | grep -v ': OK$'; for f in $(cat $K/vllm-ascend/NEW_FILES.txt); do [ -e "$f" ] && echo "已存在：$f"; done)
sha256sum "$F" | cut -c1-16
(cd "$ASCEND_PKG/patch" && for f in __init__.py platform/__init__.py platform/patch_kv_load_failure_reason.py platform/patch_prefill_length_validation.py; do [ -e "$f" ] && echo "$f $(sha256sum "$f" | cut -c1-16)" || echo "$f 没有"; done)
```

**执行后马上会看到：** 前两条没有任何输出；第三条是 `2c4acb3eeb1ced7d`；最后四行是文件名和 sha256 前 16 位：

| 文件 | P | D |
|---|---|---|
| `__init__.py` | 第 2 步 `site_patches` 的 `patch/__init__.py` | 同左（D 自己那一次的值） |
| `platform/__init__.py` | 第 2 步 `site_patches` 的 `patch/platform/__init__.py` | 同左（D 自己那一次的值） |
| `platform/patch_kv_load_failure_reason.py` | `d7c764bb425993c0` | `d7c764bb425993c0` |
| `platform/patch_prefill_length_validation.py` | `c36392a6338bc2f1` | 这一行是 `platform/patch_prefill_length_validation.py 没有` |

- vLLM 的 23 个文件、vllm-ascend 的 9 个文件和公开的 v0.30.0、`d4ad49796` 相同，两个包要新加的文件都还不存在。23 个里有 V2 runner 的两个文件（`v1/worker/gpu/kv_connector.py`、`v1/worker/gpu/model_runner.py`）：现场用 V1 runner（检测 `runner=V1(env)`），这两个文件不会被执行；补丁和 dsv4.1 共用，所以保留，照样核对。现场这两个文件没改过，不会误停。
- `mooncake_connector.py` 是现场的本地修改版，P、D 相同。现场小核对测得的就是这个值，p2d 的重放（第 4.3 步）是照这一份核对过的。
- 出现 `FAILED`、「已存在」，或者后五行和上面的值不同，说明文件已经被改过、已经装过补丁，或者现场的本地补丁变了：停下，把输出带回来。

再看现场两个本地补丁是不是调用原函数（只读，P 上跑；D 上只有第一个文件）：

```bash
grep -nE "original|_orig|kwargs" "$ASCEND_PKG"/patch/platform/patch_kv_load_failure_reason.py "$ASCEND_PKG"/patch/platform/patch_prefill_length_validation.py
```

**执行后马上会看到：** 两个文件里都有调用原函数（`original`、`_orig` 一类名字）的行；`patch_prefill_length_validation.py` 里 `_add_request` 的包装把 `**kwargs` 原样传给原函数。没有把 `**kwargs` 传下去时，照样往下做（服务不受影响，P 的 add_request 段会为空），把这条命令的输出带回来。

备份：

```bash
BKBASE=/tmp; [ "$(stat -f -c %T /tmp)" = tmpfs ] && BKBASE=/root
BK=$BKBASE/t51-glm53-backup-$(date +%Y%m%d-%H%M%S); mkdir -p "$BK"; echo "$BK"
(cd "$VLLM_ROOT" && tar -cf "$BK/vllm.tar" $(awk '{print $2}' $K/vllm/SHA256SUMS.before))
(cd "$ASCEND_ROOT" && tar -cf "$BK/vllm-ascend.tar" $(awk '{print $2}' $K/vllm-ascend/SHA256SUMS.before) vllm_ascend/distributed/kv_transfer/kv_p2p/mooncake_connector.py)
(cd "$BK" && sha256sum vllm.tar vllm-ascend.tar > SHA256SUMS && tar -tf vllm.tar | wc -l && tar -tf vllm-ascend.tar | wc -l && awk '{print substr($1, 1, 16), $2}' SHA256SUMS)
```

**执行后马上会看到：** 备份目录名 `/tmp/t51-glm53-backup-<时间戳>`（`/tmp` 是 tmpfs 时在 `/root` 下），然后两个数字 `23` 和 `10`（vllm-ascend 的 9 个文件加现场的 `mooncake_connector.py`），最后两行是两个 tar 的 sha256 前 16 位。把备份目录名记下来。

**把备份拷到那台机器上，再拷回监控机（必做）。** 在那台机器上：

```bash
docker cp <容器名>:<备份目录> /tmp/
```

在监控机上：

```bash
mkdir -p <用户目录>/vllm-pd-patch-glm53/backup && scp -r <SSH 账号>@<机器地址>:/tmp/t51-glm53-backup-<时间戳> <用户目录>/vllm-pd-patch-glm53/backup/<机器>-<时间戳>
```

```bash
cd <用户目录>/vllm-pd-patch-glm53/backup/<机器>-<时间戳> && LC_ALL=C sha256sum -c SHA256SUMS && tar -tf vllm.tar | wc -l && tar -tf vllm-ascend.tar | wc -l
```

**执行后马上会看到：** `vllm.tar: OK`、`vllm-ascend.tar: OK`，然后 `23`、`10`：监控机上的备份和容器里的逐字节相同。有 `FAILED` 或者数字不对时重新拷；核对通过以前不做第 4 步。

**对同机其他服务的影响：** 两个备份文件合计约 2 MB（`vllm.tar` 约 0.9 MB，`vllm-ascend.tar` 约 1.1 MB），容器里、那台机器的 `/tmp` 下、监控机上各一份。

## 4. 替换源文件

**机器：** 容器里，接着第 3 步，要用第 3 步设好的 `VLLM_PKG`、`ASCEND_PKG`、`VLLM_ROOT`、`ASCEND_ROOT`、`F`、`K`、`BK`。先确认它们都在：

```bash
: "${VLLM_PKG:?}" "${ASCEND_PKG:?}" "${VLLM_ROOT:?}" "${ASCEND_ROOT:?}" "${F:?}" "${K:?}" "${BK:?}" && echo "变量都在：$BK"
```

**执行后马上会看到：** `变量都在：<备份目录>`。报 `parameter null or not set` 时，说明换过 shell：按第 3 步第一段重新找目录，再设 `K=/tmp/t51-kit/patches` 和 `BK=<备份目录>`（不要重做备份），然后再执行这一条。

### 4.1 vLLM

```bash
cp -R $K/vllm/files/vllm/. "$VLLM_PKG"/ && (cd "$VLLM_ROOT" && LC_ALL=C sha256sum -c $K/vllm/SHA256SUMS.after | grep -v ': OK$')
```

**执行后马上会看到：** 没有任何输出，表示 25 个文件都和交付的一致（23 个改过的，加 `NEW_FILES.txt` 里的 2 个新文件）。有 `FAILED` 时停下，按第 9 步回滚。

### 4.2 vllm-ascend 主体

```bash
cp -R $K/vllm-ascend/files/vllm_ascend/. "$ASCEND_PKG"/ && (cd "$ASCEND_ROOT" && LC_ALL=C sha256sum -c $K/vllm-ascend/SHA256SUMS.after | grep -v ': OK$')
```

**执行后马上会看到：** 没有任何输出，表示 11 个文件都和交付的一致（9 个改过的，加 2 个新文件）。有 `FAILED` 时停下，按第 9 步回滚。

这 11 个文件里没有 `mooncake_connector.py`，它在下一步按现场那一份重放。

### 4.3 p2d：在现场的 `mooncake_connector.py` 上重放

现场的 `mooncake_connector.py` 被本地改过，不能直接换成交付的替换件；用 `replay_p2d.py` 在现场那一份上重放 p2d 的 28 处改动。P、D 都要做：D 写读 KV 的全部记录，P 只多写一条 producer link。`replay_p2d.py` 的 sha256 是 `02304c8f76429780635870d17bac73558a2ecfafaadd744428c0ec93f457d4b3`，和现场小核对用的是同一版；第 1 步的核对已经确认过它。

**只检查、不写文件：**

```bash
python3 -I $K/p2d/replay_p2d.py "$F" --check; echo "exit=$?"
```

**预计输出**（现场小核对 glm53f-1 的结果）：

```
replay_p2d: input=<$F> sha256=2c4acb3eeb1ced7d… same_as_d4ad49796=False
replay_p2d: note N01 functions whose line count differs from d4ad49796: <n>: …
replay_p2d: facts ok (31 stop facts equal the original)
replay_p2d: C01 primary equal 'from vllm.v1.request import RequestStatus': import pd_diagnostics
…（C02–C28 每处一行）
replay_p2d: C25 alternative_2 prefix 'return delay_free_blocks, ': producer offer
…
replay_p2d: check ok: 28 of 28 changes located, result checks passed, nothing written
exit=0
```

- `N01` 一行列出行数和原版不同的函数和类，只是提示，不影响结果。里面应包括小核对报出的 9 个函数，其中新增的 2 个 `get_kv_load_failure_reason` 原行数显示为 `-`；各个类本身也会列出，所以 `<n>` 会大于 9。
- 28 行定位结果里，只有 C25 是 `alternative_2`，其余都是 `primary`。C25 用备选锚点在预期之内：producer offer 插在 `request_finished` 函数体最外层、唯一那处 `return delay_free_blocks, ...` 之前，语义和主锚点相同。
- 定位结果和上面不一样时（别的编号也用了备选锚点，或者 C25 回到 `primary`），说明现场文件和小核对时不同，停下，把输出带回来。

**再跑补充核对（X1–X6）：**

```bash
python3 -I -c "import sys; sys.path.insert(0, sys.argv[2]); import extra_checks; print(extra_checks.extra_line(open(sys.argv[1]).read()))" "$F" $K/p2d
```

**执行后马上会看到：** `p2d_extra X1=1 X2=1/1 X3=2 X4=4/1 X5=1 X6=ok`，没有一项带 `!`。

现场小核对 f-3b 在 P、D 上测得的正是这两行（`p2d_replay … alt=C25:2 … result=ok` 和上面这行）：X1–X6 和原版相同，现场的改动不影响 p2d 的记录。打补丁时这两行和 f-3b 不一样，说明现场文件变了，停下，把两条命令的完整输出带回来。`extra_checks.py` 只读源码文本，不导入 vLLM，不写文件。

**写出打好补丁的文件：**

```bash
python3 -I $K/p2d/replay_p2d.py "$F" --out /tmp/mooncake_connector.p2d.py; echo "exit=$?"
```

**预计输出：** 和上一条前面的行相同，最后一行换成：

```
replay_p2d: written out=/tmp/mooncake_connector.p2d.py sha256=<S> changes=28/28 py_compile=ok same_as_replacement=False
exit=0
```

记下 `<S>`。所有 P、D 容器的输入文件相同，`<S>` 也必须相同；和前面做过的容器不同就停下。`same_as_replacement=False` 是正常的：交付的替换件对应的是公开的原文件，不是现场这一份。

**替换并核对：**

```bash
cp /tmp/mooncake_connector.p2d.py "$F" && sha256sum "$F" | cut -c1-16
```

**执行后马上会看到：** `<S>` 的前 16 位。

打好补丁的模块能不能导入，在第 1 批的那一个容器里、实例停掉以后核对（第 7.1 节）。

`replay_p2d.py` 的退出码不为 0 时，现场文件没有被动过（脚本从不修改输入，只写 `--out`）：

| 退出码 | 含义 | 怎么办 |
|---|---|---|
| 0 | 检查通过，或写出成功 | 继续 |
| 1 | 参数错，读不了输入，或输入不是合法的 Python | 核对路径和文件 |
| 2 | 某处改动（输出里的 `Cxx`）的主锚点和备选锚点都没有恰好命中一行 | 不写文件，把完整输出带回来 |
| 3 | 结构判据不满足（输出里有 `STOP Fxx`） | 不写文件，把完整输出带回来 |
| 4 | 重放结果的检查没通过 | 不写文件，把完整输出带回来 |
| 5 | 写出的文件 `py_compile` 不通过 | 不要用 `/tmp` 下的文件，把输出带回来 |
| 6 | 输入已经打过补丁 | 不要重复打，核对是不是已经替换过 |

不要手工修改 `replay_p2d.py` 或现场文件去绕过检查。

**上线以后怎么确认 p2d 的记录写出来了：** 见第 7.3 节。接上过滤器以后看不到 consumer link、task 的原文，按那里的间接证据看（`request_generation` 在过滤器写的文件里，consumer link、task 看 `cand_maps` 和 D 的 start_load_kv/submit）。

**带回来的东西**（P、D 各一份）：这一节两条 `replay_p2d.py` 命令和 `p2d_extra` 那条命令的完整输出，以及 `<S>`。

## 5. Mooncake：改为第 11 步（可后补）

Mooncake 库的替换不在这里做，挪到第 11 步，可以以后单独做；前 1–10 步不依赖它。不做时只有 P 后台 wait_for_save/put 下的 3 段为空（第 11 步）。

## 6. 日志去向和日志量

- `[PD_DIAG]` 记录由面板二的过滤器写进 `<节点目录>/logs/<端口>/`，前提是新增集群说明第 7 步的挂载和两个环境变量已经加好。
- Mooncake 的 `[BatchPutTiming]` 行写进各实例的 vLLM 日志文件，第 11 步换了 Mooncake 以后才有（不设 `MC_LOG_DIR`，见第 11 步）。

### 对节点的影响：日志量

**结论**

**一、落盘的地方有两处，规则不同。**

| 写到哪里 | 写什么 | 谁来删 |
|---|---|---|
| 过滤器目录（`NODE_LOG_DIR`） | 请求级记录，加上每请求每 5 秒一行累计值 | 过滤器自己删超过 `NODE_LOG_KEEP_HOURS`（缺省 12 小时）的文件 |
| vLLM 自己的日志文件 | Mooncake 的 `[BatchPutTiming]`，经标准错误写入（不设 `MC_LOG_DIR`） | 过滤器不删，现场要自己轮转 |

**二、过滤器目录：在现场的空闲空间下可控。**

**依据。** 第 2 次检测的 `free_gib`：
- P 容器 3445.5 GiB，D 容器 3217.2 GiB；
- 这是 vLLM 标准输出所在的那块盘。过滤器目录是否在同一块盘上，待现场确认。

**每台机器的占用。** D 机器按 16 个实例算，P 机器按 1 个实例算。下表每行取 6.1 表里那一档的最重组合（步间隔 20 ms）。

| 档位 | 12 小时占用 | 占可用空间 |
|---|---|---|
| D，每实例 1 请求/秒，B=256 | 99.5 GB，再加 16 GiB 上限 | 约 3% |
| D，每实例 10 请求/秒，B=256 | 351 GB，再加 16 GiB | 约 11% |
| D，每实例 50 请求/秒，B=256（表中最重的一档） | 1471 GB，再加 16 GiB | 约 43%；建议把 `NODE_LOG_KEEP_HOURS` 调到 6 小时，占用减半 |
| P，每实例 50 请求/秒，每步 16 个请求（最重的一档） | 41 GB，再加 1 GiB | 约 1% |

「上限」来自过滤器的删除规则：每个实例 4 个角色文件，各自写满 256 MiB 才换；正在写的不删，换下的要等最后一次写入超过保留时长才删（`pd_diag_logfilter.py` 的 `_clean_old`）。

统筹定：面板的默认值仍是 12 小时（`NODE_LOG_KEEP_HOURS` 留空）。每个 D 实例超过每秒 10 个请求、批大小 256 时，建议把 `NODE_LOG_KEEP_HOURS` 改成 6 小时，改法见《面板系统说明书》3.6.5。

**三、vLLM 日志要现场自己轮转。**

`[BatchPutTiming]` 让 vLLM 日志每天增长：

| 每实例每秒请求数 | P 机器，g=1 | P 机器，g=2 | D 机器（16 实例，D 保存时），g=1 | D 机器，g=2 |
|---|---|---|---|---|
| 1 | 0.43 GB | 0.86 GB | 0.43 GB | 0.86 GB |
| 10 | 4.3 GB | 8.6 GB | 4.3 GB | 8.6 GB |
| 50 | 21.6 GB | 43.1 GB | 21.6 GB | 43.1 GB |

- g 是每个请求保存的 KV cache 组数，没有核实，所以 1 组、2 组各列一列。
- D 不往缓存池保存时，D 上没有这一项。
- 例：P 每秒 50 个请求、g=2 时，一个月不轮转约 1.3 TB。

**四、必须和补丁同一次重启：设好面板二的日志配置。**
- **为什么。** 补丁 01、02、p2d 的 `[PD_DIAG]` 记录都用 vLLM 的 logger 在 INFO 级写出，没有开关。只有 `VLLM_LOGGING_CONFIG_PATH` 指向面板二的 `pd_diag_logging.json` 时，它们才只进过滤器：这份配置在 vLLM 的标准输出 handler 上挂了 `DropPdDiag`。
- **现场现状。** 现在是 `logcfg=unset`（第 2 次检测的 run 行）。
- **漏设的后果。** 6.3 的原始量全部进 vLLM 日志：
  - 示例 A，D 机器每天约 1.4 TB，3.2 TiB 的空闲约 2.4 天写满；
  - 示例 B，D 机器每天约 13 TB，约 6 小时写满。
- **重启后的核对。** 检测脚本 run 行的 `logcfg=` 必须是已设置。

**五、原始量那一层（不落盘）是一项开销，作为已知问题。**
- 它是 EngineCore 和 Worker 里的 JSON 序列化：每条记录先 `json.dumps` 再交给 logger。
- 量见 6.3，例如 D 每秒 20 步、B=256 时约 3.6 MB/s。
- CPU 开销本身没有测。

**六、上线时的两个数和阈值（第 7.4、7.6 节用）。**

两个数（每台机器每天，按 6.4 的两个示例点，参数都待现场确认）：

| | 节点目录每天增长 | vLLM 日志每天增长（`[BatchPutTiming]`） |
|---|---|---|
| 示例 A | D 机器约 92 GB，P 机器约 16 GB | P 机器约 4.3 GB；D 机器 0（D 不保存） |
| 示例 B | D 机器约 702 GB，P 机器约 64 GB | P 机器约 17.3 GB；D 机器 0（D 不保存） |

- 节点目录每天增长是 6.4 里 12 小时的量的两倍。盘上只留 `NODE_LOG_KEEP_HOURS`（缺省 12 小时）的量，再加每实例最多 1 GiB。
- vLLM 日志这一列要等第 11 步换了 Mooncake 以后才有，没换时是 0。vLLM 日志由现场自己轮转：这一路的增长按现场保留 vLLM 日志的天数，算进 vLLM 日志所在文件系统的空间。

阈值（每批量 30 分钟，换算成每天以后比较，见第 7.4 节；超过就回滚这一批，`[BatchPutTiming]` 在第 11 步以前的那一条除外）：

| 项 | 阈值 | 取法 |
|---|---|---|
| 节点目录 | 每天增长 × `NODE_LOG_KEEP_HOURS` ÷ 24，再加这台机器的实例数 × 1 GiB（D 机器 16 GiB，P 机器 1 GiB），不超过第 7.1 节 `df` 里节点目录所在文件系统可用空间（`Avail`）的一半 | 结论二：6.1 表里最重的一档（每实例每秒 50 个请求、B=256）在现场的空闲空间下约占 43%，一半以内覆盖表里的全部档位 |
| vLLM 日志里的 `[PD_DIAG]` 行 | 0 行；不是 0 行就立刻停这一批，不用等满 30 分钟 | 结论四：日志配置生效时 `[PD_DIAG]` 只进过滤器；vLLM 日志里出现一行，就说明这个实例的日志配置没有生效。没设日志配置时，一台 D 机器约 6 小时就会写满盘 |
| vLLM 日志里的 `[BatchPutTiming]` | 第 11 步以前：应接近 0（Mooncake 没换时不写这种行）；明显大于 0 时带回来，不回滚，这说明现场的 Mooncake 可能已经不是我们核对过的那份，第 11 步先等我们看过这些行再决定做不做。第 11 步以后：一台机器每天不超过 43.1 GB（P 机器；D 机器只在往缓存池保存时有，不保存时应当是 0 行） | 结论三表里最大的一格（每实例每秒 50 个请求、g=2），P、D 机器都是 43.1 GB。D 只有往缓存池保存时才写这种行。第 7.4 节的命令量的是这些行的实际字节数；按行数估算，30 分钟换算成一天是「30 分钟内新增的 `[BatchPutTiming]` 行数 × 312 B × 48」 |

- 量 30 分钟：一个请求在 D 上停留 26–64 秒（6.4 的两个示例），累计值每 5 秒写一组，30 分钟里各类行都写过许多轮；时间再短，请求量的起伏影响大。
- 选平时有正常请求量的时段量。

#### 6.1 过滤器目录，每实例 12 小时（GB）

**D**：不命中缓存池、不保存；命中时每请求多 3.2 KB，保存时多 3.6 KB。

| 每实例每秒请求数 | 步间隔 | B=16 | B=64 | B=256 |
|---|---|---|---|---|
| 1 | 20 / 50 / 100 ms | 2.03 | 2.87 | 6.22 |
| 10 | 20 ms | 17.77 | 18.61 | 21.96 |
| 10 | 50 ms | 11.74 | 18.61 | 21.96 |
| 10 | 100 ms | 8.69 | 18.61 | 21.96 |
| 50 | 20 ms | 42.34 | 88.58 | 91.92 |
| 50 | 50 ms | 33.20 | 52.31 | 91.92 |
| 50 | 100 ms | 30.16 | 40.13 | 80.02 |

**P（TP16）**：命中缓存池；不命中时每请求少约 3.3 KB。

| 每实例每秒请求数 | 每步 1 个请求 | 每步 4 个请求 | 每步 16 个请求 |
|---|---|---|---|
| 1 | 0.79 | 0.80 | 0.83 |
| 10 | 7.89 | 7.96 | 8.26 |
| 50 | 39.44 | 39.80 | 41.28 |

**算法。**
- **请求在 D 上停留多久。** 按 Little 定律，停留时间 = B ÷ r（B 是一步里在跑的请求数，r 是每实例每秒请求数）；在 D 上走的步数 = 停留时间 ÷ 步间隔。
- **每个请求写的固定几行。**
  - 3 条 `api_summary`、`core_summary[request]`；
  - p2d 的 `request_generation`；
  - `api_summary[request]` 的 detokenize 区间、`core_summary[request]` 的 hash 区间，每步各一项，最多各 64 项。
- **累计值行。** 停留期间每 5 秒写一组，每组 engine、worker 各一行。
- **自检行**：每个实例每分钟 4 行。

**和 MTP 的关系。** 每步记录的字节数和 MTP 无关，实测每个请求项都是 333 B：t51-glm53-vllm 的 E5（async）和 E6（async + spec5）两个场景一样。MTP 只改变每步出几个 token，表里的数不随 MTP 变。

#### 6.2 vLLM 日志：`[BatchPutTiming]`

**行长。** 每行 312 B。实验室实测 290 B（keys=4、bytes=16384），按现场位数补齐：keys 3 位、bytes 10 位、四段各 5 位、total 6 位、线程号 7 位。

**行数。** 每个请求、每个 TP rank、每个 KV cache 组各一行：`kv_transfer.py` 的保存循环里，每组调一次 `put`。

**数据。** 结论三那张表。

#### 6.3 原始 `[PD_DIAG]`（GB/天；只在没设日志配置时进 vLLM 日志）

**D 每实例**：每步部分加每请求部分。

| 每实例每秒请求数 | 步间隔 | B=16 | B=64 | B=256 |
|---|---|---|---|---|
| 1 | 20 ms | 77 | 217 | 777 |
| 1 | 50 ms | 33 | 89 | 313 |
| 1 | 100 ms | 19 | 46 | 158 |
| 10 | 20 ms | 112 | 252 | 812 |
| 10 | 50 ms | 57 | 125 | 349 |
| 10 | 100 ms | 36 | 82 | 194 |
| 50 | 20 ms | 180 | 410 | 970 |
| 50 | 50 ms | 118 | 210 | 507 |
| 50 | 100 ms | 97 | 143 | 328 |

**P 每实例（TP16）**：16 个 rank 各写自己的 worker 和缓存池记录。

| 每秒步数 | 每步 1 个请求 | 每步 4 个请求 | 每步 16 个请求 |
|---|---|---|---|
| 2 | 34 | 101 | 395 |
| 5 | 85 | 252 | 987 |
| 10 | 171 | 503 | 1975 |

#### 6.4 两个示例点

全部参数待现场确认；两个示例都按 g=1、D 不保存。

**示例 A。**
- 假设：
  - D：每实例每秒 1 个请求，B=64，步间隔 50 ms，MTP k=5；每个请求在 D 上 1280 步、停留 64 秒。
  - P：每秒 10 步，每步 1 个请求（每秒 10 个请求），命中缓存池。
- 过滤器目录 12 小时：D 机器 45.9 GB，再加 16 GiB；P 机器 7.9 GB，再加 1 GiB。
- vLLM 日志每天：P 机器 4.3 GB。

**示例 B。**
- 假设：
  - D：每实例每秒 10 个请求，B=256，步间隔 20 ms，MTP k=5；每个请求 1280 步、停留 26 秒。
  - P：每秒 10 步，每步 4 个请求（每秒 40 个请求），命中缓存池。
- 过滤器目录 12 小时：D 机器 351 GB，再加 16 GiB；P 机器 31.8 GB，再加 1 GiB。
- vLLM 日志每天：P 机器 17.3 GB。

#### 6.5 和 glm5.3 现场的对应

| 项 | 本节怎么取 | 依据 |
|---|---|---|
| P 的形态 | TP16，每容器 1 个实例；16 个 rank 都写 worker 和缓存池记录，过滤器只留 rank 0 | 统筹给的 glm5.3 形态；第 2 次检测 |
| D 的形态 | TP1，每容器 16 个实例，MTP k=5，async 调度（`AsyncRecomputeScheduler`） | 同上；记录由这个调度器的真实代码产生 |
| 补丁版本 | vLLM v0.30.0 + t51-glm53-vllm `ea04aa1`（补丁 `88685fda`）；vllm-ascend `d4ad49796` + `ef5bd17` 的 11 个文件（SHA256SUMS.after `4c2d978a`）；p2d `a9e9695` | 本包的 vLLM 最终版 `40152b2` 比 `ea04aa1` 只多了 n>1 子请求走 `_add_request` 和三行启动日志 `PD_DIAG_STARTUP`；后者不是 `[PD_DIAG]` 记录，不改记录格式，也不改这一节的量 |
| 过滤器规则 | 包二 v3.1-extra 的 glm53 `segments.json`（config_id `pd-diag-v3.1-extra-glm53-2026-11-19b`）；哪些记录落盘，以过滤器自己解析出的规则为准 | 实测 |
| 日志配置 | 现场现在 `logcfg=unset`；本节前三层的数，都以补丁那次重启同时设好面板二日志配置为前提 | 第 2 次检测 |
| `MC_LOG_DIR` | 不设，`[BatchPutTiming]` 进 vLLM 日志 | f-1：16 个 Worker 都没设 |
| 空闲空间 | P 3445.5 GiB，D 3217.2 GiB；是 vLLM 标准输出所在的盘 | 第 2 次检测 |
| 请求速率、批大小、步间隔、g | 未知，按档位列表 | 待现场确认 |

#### 6.6 出处

- **记录字节数：** `t51-glm53-kit` `volume/`。
  - 测量方法：在上面那几份真实代码上，一步排 1、64、256 个请求测出，D 另跑 70 步的长请求；`docker run --rm --network none`。
  - `api_summary`：取自 t51-glm53-vllm 的 `verify/pd_equiv.py`（未改）。
  - 落盘行：把记录喂给真实的过滤器量出。
  - 字段宽度：同 t51-glm53-p2d 的测量。
- **p2d：** t51-glm53-p2d `results/12_record_bytes.json`。
- **`[BatchPutTiming]`：** `t51-glm53-kit` `mooncake/replace/lab/lab_test.txt`。
- **计算：** `volume/harness/compute_tables2.py`，结果在 `volume/results/tables2.md`。
- **本节：** `t51-glm53-kit` 提交 `b2f2617` 的 `notes/guideA_glm53_logvolume.zh-CN.md`，搬入时只改了标题层级和节号引用；结论六是本说明加的。

## 7. 重启，分批上线

**分三批：** 第 1 批是一台 D 机器的容器（这台机器上的全部 D 实例），第 2 批是其余 D 机器的容器，第 3 批是 P 机器的容器。每批的容器都做完第 1–6 步再重启；这一批的检查都通过了，再做下一批。

**怎么重启：** 现场容器的 1 号进程是 `bash`，vLLM 由 `launch_online_dp.py` 拉起。按平时的方式停掉这个容器里的 vLLM 实例（第 1 批的那一个容器，停掉以后先做第 7.1 节的导入核对），再用原来的启动文件拉起（D 是 `start_d0.sh`，和 `run_dp_template.sh` 同一目录）。参数和停之前相同；环境变量只多新增集群说明第 7 步的两行 `export`，这两行这次必须生效（第 0 节）。不要加 `MC_LOG_DIR`。

### 7.1 每批重启以前

**记下每个实例的命令行**（容器里，停实例之前）：

```bash
BK=<备份目录>
for d in /proc/[0-9]*; do a=$(tr '\0' ' ' < "$d/cmdline" 2>/dev/null); case "$a" in *"vllm serve"*|*vllm.entrypoints.openai.api_server*) echo "$a";; esac; done | LC_ALL=C sort | uniq -c > "$BK/cmdline.before"; cat "$BK/cmdline.before"
```

**执行后马上会看到：** 每种命令行一行，前面的数是用这一行的进程个数（同一个实例的子进程沿用父进程的命令行时，这个数大于 1）。D 的容器里每个实例一种（端口各不相同），P 的容器里一种。把输出带回来。

**每批都要记下两个文件系统的可用空间**（在这一批的每台机器上，宿主机里）：

```bash
df -hT <节点目录> <vLLM 日志文件> | uniq
```

**执行后马上会看到：** 表头一行，然后节点目录、vLLM 日志所在的文件系统各一行（两者在同一个文件系统上时只有一行）。把这几行带回来；第 7.4 节判断盘够不够时用其中的 `Avail`。

**记下停实例的时刻**（第 7.5 节比较 TTFT、TPOT 用），然后按平时的方式停掉这个容器里的 vLLM 实例。

**第 1 批的那一个容器：实例停掉以后、重新拉起以前，核对打好补丁的模块能导入。** 其余容器、其余批次都不做这一条。

```bash
python3 -B -c "import time, resource; t = time.time(); import vllm_ascend.distributed.kv_transfer.kv_p2p.mooncake_connector as m; print(hasattr(m.KVCacheRecvingThread, '_transfer_batch'), '%.1f s' % (time.time() - t), '%d MiB' % (resource.getrusage(resource.RUSAGE_SELF).ru_maxrss // 1024))"
```

**执行后马上会看到：** 一行 `True <用时> s <峰值内存> MiB`。`True` 表示打好补丁的模块能导入，它用到的第 4.1 步装的 vLLM 诊断模块也在。

- **这条命令在机器上做什么：** 起一个 Python 进程，导入 torch、torch_npu、vLLM 和 vllm_ascend 里这个模块用到的部分（实验室里约 4900 个模块），导入完就退出。不加载模型，不开端口；`-B` 让它不写 `.pyc`。
- **用时和内存：** 至少 5 秒、至少 500 MiB。实验室（CPU 版 torch，torch_npu、torchair、Mooncake、acl 换成替身）是 6–8 秒、约 500 MiB（第 10 节）；现场还要加上加载 torch_npu 和 CANN 的部分，没有测过。把这一行的用时和峰值内存带回来。
- **为什么放在实例停掉以后。** 导入时 vLLM、vllm-ascend 的 Python 代码不碰设备（vllm-ascend 一侧读代码确认，CPU 容器里实测过）；torch_npu 和 Mooncake 这两个二进制扩展在导入时做什么没有验证。实例停掉以后 NPU 已经空出来，就算导入时碰了设备，也不会和服务争。
- **其余容器不做：** 它们的文件和第 1 批逐个相同（第 4 步各处的 sha256 核对，第 4.3 步的 `<S>`），导入的问题会在第 7.2 节的日志检查里看到。

然后用原来的启动文件拉起。

### 7.2 每批重启以后，在那台机器的容器里看

每个实例都起来了。D 的容器：

```bash
for p in $(seq 9100 9115); do printf "%s " "$p"; curl -s -o /dev/null -w '%{http_code}\n' localhost:$p/health; done
```

P 的容器：

```bash
for p in 9900; do printf "%s " "$p"; curl -s -o /dev/null -w '%{http_code}\n' localhost:$p/health; done
```

**执行后马上会看到：** 每个端口一行，都是 `200`（D 16 行，P 1 行）。

命令行和重启前相同：

```bash
BK=<备份目录>
for d in /proc/[0-9]*; do a=$(tr '\0' ' ' < "$d/cmdline" 2>/dev/null); case "$a" in *"vllm serve"*|*vllm.entrypoints.openai.api_server*) echo "$a";; esac; done | LC_ALL=C sort | uniq -c > "$BK/cmdline.after"; diff "$BK/cmdline.before" "$BK/cmdline.after" && echo "命令行和重启前相同"
```

**执行后马上会看到：** `命令行和重启前相同`。有 `diff` 的输出时，说明拉起来的参数和停之前不一样：停下，不做这一批后面的检查，把 `cmdline.before`、`cmdline.after` 两个文件带回来。

面板二的日志配置在每个 vLLM 进程上都生效了：

```bash
for d in /proc/[0-9]*; do a=$(tr '\0' ' ' < "$d/cmdline" 2>/dev/null); case "$a" in *"vllm serve"*|*vllm.entrypoints.openai.api_server*) tr '\0' '\n' < "$d/environ" | grep -q '^VLLM_LOGGING_CONFIG_PATH=.*/pd_diag_logging\.json$' && echo "已设" || echo "没有设：pid ${d#/proc/}";; esac; done | sort | uniq -c
```

**执行后马上会看到：** 只有一行 `<进程个数> 已设`。有「没有设」的行，说明这个进程没有拿到新增集群说明第 7 步的 `export`，补丁的原始记录会全部写进它的 vLLM 日志（第 0 节）：这一批算失败，马上按第 7.6 节处理。

vLLM 日志里没有本套补丁引起的错误：

```bash
grep -nE -A12 "ImportError|Traceback" <vLLM 日志文件> | grep -E "ImportError|Traceback|pd_diagnostics|pd_device_timing|worker_base|ascend_store|mooncake_connector|recompute_scheduler" | head -20
```

**执行后马上会看到：** 没有输出。有输出时看它是不是本套补丁引起的：`ImportError`，或者 `Traceback` 后面几行里有 `pd_diagnostics`、`pd_device_timing`、`worker_base`、`ascend_store`、`mooncake_connector`、`recompute_scheduler` 这些本套补丁改过的模块，算这一批失败。

### 7.3 每批重启 5 分钟以后，在监控机上看

**哪几类记录到了面板二。** 接上过滤器以后，`[PD_DIAG]` 记录的原文由过滤器接管，不再写进 vLLM 日志；过滤器只把看板要用的部分写进 `<节点目录>/logs/<端口>/`：有的记录照原样写出，有的只在过滤器里累加、配对，看不到原文。所以 consumer link、task、`worker_summary` 这几类记录按下表的间接证据看：

| 记录 | 哪一侧 | 在哪里看 | 应当看到 |
|---|---|---|---|
| `pd_transfer_summary[scope=request_generation]` | D | 节点上 `<节点目录>/logs/<端口>/` 下的文件（原样写出），`grep -c '"scope":"request_generation"'` | 大于 0 |
| `pd_transfer_summary` 的 consumer link 和 task | D | 过滤器只拿它们把首次候选那一步归到请求上，不写出原文；看下面那条查询的 `cand_maps`，和看板上 D 的 start_load_kv/submit | `cand_maps` 大于 0；D 的 start_load_kv/submit 有值 |
| `worker_summary`（带 `device_duration_ns`） | P、D | 过滤器按请求累加，不写出原文；看下面那条查询的留空次数 `forward_empty`、`sampling_empty`，和看板上的 forward、sampling | forward、sampling 有值；留空比例见下面（D 的批是判据） |
| `core_summary`、`api_summary` | P、D | 看板上的 schedule、req_resolve、tokenizer、add_request | 有值 |

过滤器自检行的几个计数（只读，进程启动以来累计）：

```bash
cd <总目录>/vllm-pd-grafana-panel2/test-glm53 && bash -c '. bin/lib.sh && load_env >/dev/null && curl -s -u "vllm_reader:$CH_READER_PASSWORD" "http://localhost:$CLICKHOUSE_HTTP_PORT/" --data-binary @-' <<'SQL'
SELECT instance, sum(m) AS cand_maps, sum(f) AS forward_empty, sum(sa) AS sampling_empty, sum(e) AS no_execution_id FROM (SELECT instance, argMax(cand_maps, ts) AS m, argMax(acc_fallback['forward'], ts) AS f, argMax(acc_fallback['sampling'], ts) AS sa, argMax(exec_missing, ts) AS e FROM vllm_pd.filter_selfcheck WHERE ts > now() - INTERVAL 10 MINUTE GROUP BY instance, pid) GROUP BY instance ORDER BY instance FORMAT PrettyCompactMonoBlock
SQL
```

**执行后马上会看到：** 每个实例一行。这一批的 D 实例 `cand_maps` 大于 0（D 从 P 读过 KV 的请求越多，这个数越大）；`no_execution_id` 是 0。留空次数 `forward_empty`、`sampling_empty` 是这一段没有设备时间、看板上留空的步数（《面板系统说明书》3.6.5 的 C15），和 `no_execution_id` 放在一起看，按下面的留空比例判。

**留空比例（判据）。** 图模式按批大小分桶捕获图，服务中第一次遇到某个桶的那一步在捕获、没有设备时间，所以留空次数不会一直是 0。下面的查询执行两次，每个实例单独看。两条查询里的 `<这一批的实例>` 和第 7.5 节填的相同（第 0.2 节，例如 `D0|D1|D2`）。

**第一次：重启 10 分钟以后。** 只记录、不判。它打印这一刻的时刻 `t10_utc`（UTC），每个实例的进程 `pids`、`pid_list`，以及到这一刻为止的步数和留空次数。整份输出带回来；`t10_utc` 第二次要用，每个实例的 `pid_list` 也照抄下来，第二次拿来对照。

```bash
cd <总目录>/vllm-pd-grafana-panel2/test-glm53 && bash -c '. bin/lib.sh && load_env >/dev/null && curl -s -u "vllm_reader:$CH_READER_PASSWORD" "http://localhost:$CLICKHOUSE_HTTP_PORT/" --data-binary @-' <<'SQL'
WITH now64(3, 'UTC') AS t10, cur AS (SELECT DISTINCT instance, pid FROM vllm_pd.filter_selfcheck WHERE ts > now() - INTERVAL 10 MINUTE), done AS (SELECT DISTINCT instance, request_id FROM vllm_pd.phase_line_cur WHERE proc = 'api' AND api_end_ts < now() - INTERVAL 1 MINUTE), ew AS (SELECT instance, uniqExact(request_id) AS ne FROM vllm_pd.phase_line_cur WHERE proc = 'api' AND api_end_ts >= t10 AND api_end_ts < now() - INTERVAL 1 MINUTE GROUP BY instance), emp AS (SELECT instance, arraySort(groupUniqArray(pid)) AS pl, length(pl) AS np, sum(f) AS fe, sum(sa) AS se FROM (SELECT instance, pid, argMax(acc_fallback['forward'], ts) AS f, argMax(acc_fallback['sampling'], ts) AS sa FROM vllm_pd.filter_selfcheck WHERE ts > now() - INTERVAL 10 MINUTE GROUP BY instance, pid) GROUP BY instance) SELECT i.instance AS instance, match(i.instance, '^(<这一批的实例>)$') AS this_batch, toString(t10) AS t10_utc, dateDiff('minute', t10, now()) AS window_min, ew.ne AS ended_win, emp.np AS pids, emp.pl AS pid_list, emp.fe AS forward_empty, emp.se AS sampling_empty, x.a_tot AS alloc_total, x.f_tot AS forward_total, x.s_tot AS sampling_total, x.a_win AS alloc_win, x.f_win AS forward_win, x.s_win AS sampling_win, if(x.a_win = 0, NULL, round(100 * (1 - x.f_win / x.a_win), 2)) AS forward_empty_pct, if(x.a_win = 0, NULL, round(100 * (1 - x.s_win / x.a_win), 2)) AS sampling_empty_pct FROM (SELECT DISTINCT instance FROM done) AS i LEFT JOIN (SELECT instance, sum(al) AS a_tot, sum(fw) AS f_tot, sum(sm) AS s_tot, sum(al - al0) AS a_win, sum(fw - fw0) AS f_win, sum(sm - sm0) AS s_win FROM (SELECT l.instance AS instance, l.request_id AS request_id, l.part AS part, toUInt32OrZero(splitByChar('-', l.part)[2]) AS pid, ifNull(max(l.acc__forward__steps), 0) AS fw, ifNull(maxIf(l.acc__forward__steps, l.ts < t10), 0) AS fw0, ifNull(max(l.acc__sampling__steps), 0) AS sm, ifNull(maxIf(l.acc__sampling__steps, l.ts < t10), 0) AS sm0, ifNull(max(l.acc__alloc__steps), 0) AS al, ifNull(maxIf(l.acc__alloc__steps, l.ts < t10), 0) AS al0 FROM vllm_pd.phase_line_cur AS l WHERE l.part != '' AND (l.instance, l.request_id) IN (SELECT instance, request_id FROM done) GROUP BY l.instance, l.request_id, l.part) WHERE (instance, pid) IN (SELECT instance, pid FROM cur) GROUP BY instance) AS x ON i.instance = x.instance LEFT JOIN emp ON i.instance = emp.instance LEFT JOIN ew ON i.instance = ew.instance ORDER BY this_batch DESC, instance SETTINGS join_use_nulls = 0 FORMAT PrettyCompactMonoBlock
SQL
```

**第二次：和第 7.4 节第二次量磁盘的时候一起**（重启约 35 分钟以后）。把 `<第一次的 t10_utc>` 换成第一次打印的那个时刻（照抄，带毫秒），按这一次的结果判。

```bash
cd <总目录>/vllm-pd-grafana-panel2/test-glm53 && bash -c '. bin/lib.sh && load_env >/dev/null && curl -s -u "vllm_reader:$CH_READER_PASSWORD" "http://localhost:$CLICKHOUSE_HTTP_PORT/" --data-binary @-' <<'SQL'
WITH toDateTime64('<第一次的 t10_utc>', 3, 'UTC') AS t10, cur AS (SELECT DISTINCT instance, pid FROM vllm_pd.filter_selfcheck WHERE ts > now() - INTERVAL 10 MINUTE), done AS (SELECT DISTINCT instance, request_id FROM vllm_pd.phase_line_cur WHERE proc = 'api' AND api_end_ts < now() - INTERVAL 1 MINUTE), ew AS (SELECT instance, uniqExact(request_id) AS ne FROM vllm_pd.phase_line_cur WHERE proc = 'api' AND api_end_ts >= t10 AND api_end_ts < now() - INTERVAL 1 MINUTE GROUP BY instance), emp AS (SELECT instance, arraySort(groupUniqArray(pid)) AS pl, length(pl) AS np, sum(f) AS fe, sum(sa) AS se FROM (SELECT instance, pid, argMax(acc_fallback['forward'], ts) AS f, argMax(acc_fallback['sampling'], ts) AS sa FROM vllm_pd.filter_selfcheck WHERE ts > now() - INTERVAL 10 MINUTE GROUP BY instance, pid) GROUP BY instance) SELECT i.instance AS instance, match(i.instance, '^(<这一批的实例>)$') AS this_batch, toString(t10) AS t10_utc, dateDiff('minute', t10, now()) AS window_min, ew.ne AS ended_win, emp.np AS pids, emp.pl AS pid_list, emp.fe AS forward_empty, emp.se AS sampling_empty, x.a_tot AS alloc_total, x.f_tot AS forward_total, x.s_tot AS sampling_total, x.a_win AS alloc_win, x.f_win AS forward_win, x.s_win AS sampling_win, if(x.a_win = 0, NULL, round(100 * (1 - x.f_win / x.a_win), 2)) AS forward_empty_pct, if(x.a_win = 0, NULL, round(100 * (1 - x.s_win / x.a_win), 2)) AS sampling_empty_pct FROM (SELECT DISTINCT instance FROM done) AS i LEFT JOIN (SELECT instance, sum(al) AS a_tot, sum(fw) AS f_tot, sum(sm) AS s_tot, sum(al - al0) AS a_win, sum(fw - fw0) AS f_win, sum(sm - sm0) AS s_win FROM (SELECT l.instance AS instance, l.request_id AS request_id, l.part AS part, toUInt32OrZero(splitByChar('-', l.part)[2]) AS pid, ifNull(max(l.acc__forward__steps), 0) AS fw, ifNull(maxIf(l.acc__forward__steps, l.ts < t10), 0) AS fw0, ifNull(max(l.acc__sampling__steps), 0) AS sm, ifNull(maxIf(l.acc__sampling__steps, l.ts < t10), 0) AS sm0, ifNull(max(l.acc__alloc__steps), 0) AS al, ifNull(maxIf(l.acc__alloc__steps, l.ts < t10), 0) AS al0 FROM vllm_pd.phase_line_cur AS l WHERE l.part != '' AND (l.instance, l.request_id) IN (SELECT instance, request_id FROM done) GROUP BY l.instance, l.request_id, l.part) WHERE (instance, pid) IN (SELECT instance, pid FROM cur) GROUP BY instance) AS x ON i.instance = x.instance LEFT JOIN emp ON i.instance = emp.instance LEFT JOIN ew ON i.instance = ew.instance ORDER BY this_batch DESC, instance SETTINGS join_use_nulls = 0 FORMAT PrettyCompactMonoBlock
SQL
```

**执行后马上会看到：** 每个实例一行。

- `this_batch`：`1` 是这一批的实例（实例名在 `<这一批的实例>` 里），`0` 是之前批次已经打过补丁的实例。输出里两种都有，`this_batch` 是 1 的排在前面。
- `window_min`：`t10_utc` 到现在的分钟数。第二次执行时应当在 25 左右（重启后 10 到 35 分钟）。
- `ended_win`：这个实例在 `t10_utc` 以后、现在 1 分钟以前结束的请求数（API server 一侧，按请求号去重）。第一次执行时是 0。
- `pid_list`、`pids`：这个实例最近 10 分钟写过自检行的进程号（排好序、去重）和它们的个数（就是 `pid_list` 的长度）。两次执行应当相同。
- `forward_empty`、`sampling_empty`：留空次数，即没有设备时间的步数（过滤器自检行，进程启动以来累计）。
- 每一行是一个有请求结束了 1 分钟以上的实例；这个实例的引擎、Worker 进程没有写进库的（例如它们的过滤器停了），后面几列是 0。两条查询末尾都有 `SETTINGS join_use_nulls = 0`：拼不上的列一律取 0（`pid_list` 取 `[]`），所以拼进来的列都不加 `ifNull`；查询里的 `ifNull` 只用在按步累加的步数上，那几列在没有这一段的行上本身为空。
- `alloc_total`、`forward_total`、`sampling_total`：这个实例正在运行的进程写进库的、已经结束 1 分钟以上的请求，按步累加的步数之和（一步里有几个请求就算几次）。alloc 是调度器那一侧的步数，每一步都有；forward、sampling 只数有设备时间的步。
- `alloc_win`、`forward_win`、`sampling_win`：同样的步数，只算 `t10_utc` 以后的部分。第一次执行时是 0。
- `forward_empty_pct` = 1 − `forward_win` ÷ `alloc_win`，`sampling_empty_pct` 同理：`t10_utc` 以后留空的步所占的比例（按请求的步数加权），也就是看板上 D 第二步起每步平均被放大的那个 f。第一次执行时为空（`ᴺᵁᴸᴸ`）。比例可能是零点几的负数，按 0 看。
- 留空次数的单位是步，alloc、forward 的单位是步 × 请求，所以留空次数只看增没增加，不能和比例相除。例如面板包端到端测试里 D 的 forward 留空次数 162 是记录数，即 162 步；同一个实例的 alloc 是 624，是步 × 请求数。

**怎么判：** 用第二次的结果，forward、sampling 各自判，每个实例单独判。**只判这一批的实例（`this_batch` 是 1 的行）；其他批次的实例也会出现在输出里，只作参考，它们有问题时按它们自己那一批处理。**「留空次数增加了」指第二次的 `forward_empty`（或 `sampling_empty`）比第一次大。先看这三条：

- **`window_min` 明显大于 35：** `t10_utc` 抄错了，或者抄成了上一批的。重抄这一批第一次的 `t10_utc`，再执行第二次查询；这一次不判。

- **两次的 `pid_list` 不同（`pids` 不同时它一定不同）：** 两次之间这个实例的进程重启过，留空次数从 0 重新累计，两次不可比。重启发生在第二次执行 10 分钟以前时，旧进程已经不在第二次的列表里、新进程在，列表照样不同。这一项按失败处理（D：这一批失败；P：带回来），把两次输出带回来。
- **第二次的比例为空（`alloc_win` 是 0）：** 看同一行的 `ended_win` 和另外两个 `*_win`。
  - `ended_win` 大于 0，`forward_win` 或 `sampling_win` 大于 0：窗口里有请求结束，Worker 一侧有步骤，调度器一侧却一步也没有记下，调度器侧断了，可能出在埋点、过滤器、入库三处之一。这一批失败（D、P 都算；P 的豁免只针对 eager 下没测过的设备计时，alloc 是调度器一侧的 host 计时，不在豁免里），带回来。
  - `ended_win` 大于 0，三个 `*_win` 都是 0：可能是整条链断了，也可能是窗口里没有新的步骤（例如请求在窗口里结束，但它的步骤全在 `t10_utc` 之前就做完了）。不直接判失败，带回来。
  - `ended_win` 是 0：窗口里没有请求结束，这一项没判成，不算通过，带回来。

两条都不是，再按下表判：

| 比例 | 留空次数 | 结果 |
|---|---|---|
| 超过 1%  | 增加了 | D：这一批失败（第 7.6 节）。P：带回来，不算失败 |
| 超过 1% | 没有增加 | 不判失败：两侧步数对不上，原因待查，带回来 |
| 不超过 1% | 增加了 | 通过。记下增加了多少，带回来：可能是后来遇到新的图模式桶留下的空 |
| 不超过 1% | 没有增加 | 通过 |

- **为什么要两个条件。** 比例超过 1%、留空次数却没有增加时，多出来的不是留空，而是调度器和 Worker 两侧的步数本身对不上（《面板系统说明书》已知问题第 35 条）。代价是：真实留空很少、偏差又是正的时候，两者叠加可能误判失败；带回来的两次输出能分辨出来。
- **D 的批失败时**，说明设备计时没有每步都取到，看板上 D 第二步起的每步平均已经偏大，倍数是 1/(1−f)（《面板系统说明书》已知问题第 34 条）。把两次输出带回来。
- **P（第 3 批）只记录、不判：** P 是 eager，eager 下的设备事件没有在卡上测过（《面板系统说明书》已知问题第 32 条），不能拿同一个阈值判 P 失败；否则 P 一回滚，其余能正常出数的 P 段也跟着没了。P 的两次输出必须带回来。第一种情况（比例超过 1%、留空次数增加）时，看板上 P 的 forward、sampling 两段会相应变空：留空的步不计入，一个请求的步都留空时这两段整段为空。
- **第 1 批另要：** 每个 D 实例两次输出里的 forward、alloc 步数（`*_total`、`*_win`）和 `forward_empty`，sampling 的同样几列，都带回来。用这些数算现场调度器和 Worker 两侧步数的偏差：Δ留空次数为 0 时能直接算出偏差；不为 0 时只能给出区间。

再照新增集群说明 10.1、10.2 看：10.1 `过滤器版本` 一行里这一批的实例都是 `pd-diag-v3.1-extra-glm53-2026-11-19c@<8 位>`，`RESULT: OK`。10.2 里应当有值的段：

| 做完第几批 | 有值的段（只有经过已打补丁实例的请求才有值） | 仍为空的段 |
|---|---|---|
| 第 1、2 批（D） | D 的 req_resolve、tokenizer、add_request、schedule 和 queue_time、alloc、other，start_load_kv 和 submit、wait_for_transfer，forward、sampling、detokenizer；D 第二步起的 schedule、alloc、other、forward、sampling、detokenizer | P 的全部段（P 还没打补丁，不写 `[PD_DIAG]`）；P、D 的 lb_proxy（proxy 采集打开时有值） |
| 第 3 批（P） | 上面这些，加上 P 的 req_resolve、tokenizer、add_request、schedule 和 queue_time、hash、lookup、alloc、other，wait_for_put，forward、sampling、detokenizer，后台 wait_for_save 和它的 5 个子段；P 的 start_load_kv 和 submit 只在命中缓存池的请求上有值；做了第 11 步的，后台 wait_for_save/put 下的 3 段 | P 的 api_gateway、P 的 start_load_kv/wait_for_transfer（按设计为空）；没做第 11 步时，put 下的 3 段 |

第 3 批做完以后（新增集群说明第 9 步的两个采集来源都打开）：不做第 11 步时，47 段里 42 段有值；做了第 11 步以后 45 段。D 的两批不受第 11 步影响。

`_add_request` 的包装没有把 `**kwargs` 传下去（第 3 步那条 `grep`）时，P 的 add_request 段为空，其余照上表。

### 7.4 每批：磁盘增长（在这一批的每台机器上，宿主机里）

每批重启 5 分钟以后（做第 7.3 节的同时），在这一批的每台机器上记下起点，30 分钟以后再量一次，换算成每天，和第 6 步结论六的阈值比较。

起点：

```bash
N=<节点目录>/logs
{ date +%s; du -sb "$N" | cut -f1; for f in <vLLM 日志文件>; do echo "$f $(stat -c %s "$f")"; done; } > ~/t51-glm53-disk.t0 && echo "起点已记下：$(wc -l < ~/t51-glm53-disk.t0) 行"
```

**执行后马上会看到：** `起点已记下：<n> 行`，n 是 vLLM 日志文件的个数加 2（D 机器 18，P 机器 3）。

30 分钟以后：

```bash
N=<节点目录>/logs; S0=~/t51-glm53-disk.t0; t0=$(sed -n 1p "$S0"); n0=$(sed -n 2p "$S0"); t1=$(date +%s); n1=$(du -sb "$N" | cut -f1); b=0; p=0
while read -r f s0; do b=$((b + $(tail -c +$((s0 + 1)) "$f" | grep -a '\[BatchPutTiming\]' | wc -c))); p=$((p + $(tail -c +$((s0 + 1)) "$f" | grep -a -c '\[PD_DIAG\]'))); done < <(sed -n '3,$p' "$S0")
awk -v t=$((t1 - t0)) -v n=$((n1 - n0)) -v b=$b -v p=$p 'BEGIN { printf "%d 分钟：节点目录每天 %.1f GB；vLLM 日志 [BatchPutTiming] 每天 %.2f GB，[PD_DIAG] %d 行\n", t / 60, n / t * 86400 / 1e9, b / t * 86400 / 1e9, p }'
```

**执行后马上会看到：** 一行，例如 `30 分钟：节点目录每天 <x> GB；vLLM 日志 [BatchPutTiming] 每天 <y> GB，[PD_DIAG] 0 行`。

- 节点目录一项是这台机器上全部实例（D 机器 16 个，P 机器 1 个）合计、过滤器写的全部文件。
- vLLM 日志一项只数这 30 分钟里新写的部分：`[BatchPutTiming]` 是换了 Mooncake 以后才有的行（没做第 11 步时应接近 0；做了以后 D 只在往缓存池保存时有）；`[PD_DIAG]` 应当是 0 行。
- 这 30 分钟里有 vLLM 日志文件被现场轮转了的，vLLM 日志一项会偏小：重新记起点再量。
- 记下起点以后，随时可以先执行一次第二条命令看 `[PD_DIAG]`：不是 0 行就不等满 30 分钟，马上按第 7.6 节回滚这一批（没设日志配置时，一台 D 机器约 6 小时就会写满盘）。
- 判断见第 7.6 节。把这一行和第 7.1 节的 `df` 带回来。

### 7.5 面板一：TTFT、TPOT 中位数（观测项，不判失败）

本套补丁的开销没有实测过，用这一项兜底。每批换完以后，比较这一批实例重启前后的 TTFT、TPOT 中位数：

- 在面板一 Decode 看板「TPOT P50 / P90 / P99 / Max」面板的菜单里点 Explore（这样用的是这个集群面板一的数据源），把查询换成下面的语句，查询类型选 Instant。
- 「重启前」把时间范围的结束时刻设成第 7.1 节记下的停实例时刻；「重启后」设成重启以后 40 分钟，即取重启后第 10–40 分钟，避开刚起来的那几分钟。
- D 的批（第 1、2 批）比 TTFT 和 TPOT，`<这一批的实例>` 填这一批的 D 实例：

```text
histogram_quantile(0.5, sum by (le) (increase(vllm:time_to_first_token_seconds_bucket{role="decode", pd_instance=~"<这一批的实例>"}[30m])))
```

```text
histogram_quantile(0.5, sum by (le) (increase(vllm:request_time_per_output_token_seconds_bucket{role="decode", pd_instance=~"<这一批的实例>"}[30m])))
```

- P 的批（第 3 批）只比 TTFT：P 每个请求只出 1 个 token，vLLM 把它的 TPOT 记成 0（v0.30.0 `vllm/v1/metrics/stats.py:558`）。

```text
histogram_quantile(0.5, sum by (le) (increase(vllm:time_to_first_token_seconds_bucket{role="prefill", pd_instance=~"P.*"}[30m])))
```

**带回来的东西：** 每批重启前、重启后的 TTFT（D 的批还有 TPOT），单位是秒。重启后比重启前大 10% 以上时，报给我们，由我们决定要不要回滚。两个时段的请求量和请求长短可能不同，这一项只作参考，不算这一批失败。

### 7.6 怎么判失败，失败了怎么办

**先停下、带回的：** 第 7.2 节命令行和重启前不同。

**这一批算失败，** 只要有下面任何一条：

- 有实例的 `/health` 不是 `200`，或者起不来；
- 有 vLLM 进程没有设 `VLLM_LOGGING_CONFIG_PATH`（第 7.2 节）；
- vLLM 日志里有第 7.2 节说的 `ImportError` 或本套补丁模块的 `Traceback`；
- `check` 不是 `RESULT: OK`，或者这一批的实例不是 `…-19c` 那一版；
- 重启 10 分钟以后、这一批的实例收到过请求，D 的 forward、sampling、start_load_kv/wait_for_transfer 仍然全为空（第 3 批看 P 的 forward、sampling、schedule）；
- 这一批的 D 实例 `cand_maps` 是 0，或者 `no_execution_id` 大于 0。`no_execution_id` 一出现就算失败：execution_id 在 vLLM 补丁的 core.py `_pd_schedule` 里兜底（`patches/vllm/files/vllm/v1/engine/core.py:662-682`）：调度器没设就新建一个，所以和调度器是哪个类无关；过滤器只对 core_summary、worker_summary 两类累加记录计数，这两类都必然带 id。所以出现一次就说明链路断了；
- 第 8 步的检测：出现 `BASELINE_DIFF`（重启前后各组实例的 kind、conn、spec、eager、async、load_async、tp、dp、调度器有变化），`verdict` 的 vllm、ascend、p2d 三项有一项不是 `ok` 或 `ok(…)`（如 `unpatched`、`absent`、`-`（这一项没有跑）、`BAD(…)`；mooncake 一项例外，见第 8 步），`run` 行是 `logcfg=unset`，或者 `site_patches` 和第 2 步不同；启动日志 `PD_DIAG_STARTUP` 的 `scheduler`、`model_runner` 不是第 8 步写的那几种（或者 `BalanceScheduler` 带 `balance=True`），或者 D 上 `tp_rank=0` 的 `device_timing` 是 `off`（`?` 带回来，交给第 7.3 节的留空比例判；P 上只带回来）；
- 留空比例（第 7.3 节第二次查询；只判这一批的实例，即 `this_batch` 是 1 的行，其他批次的实例也会出现在输出里，只作参考，它们有问题时按它们自己那一批处理）：D 的 `forward_empty_pct` 超过 1% 而且 `forward_empty` 比第一次大，或者 sampling 的这两项同样如此；D 两次的 `pid_list` 不同；第二次比例为空、`ended_win` 大于 0、`forward_win` 或 `sampling_win` 大于 0（调度器侧断了，D、P 都算失败）。比例为空的其余情况不算通过，带回来（P 只记录，见第 7.3 节）；
- 磁盘项（每批，这一批的每台机器，第 7.4 节，阈值见第 6 步结论六）：
  - 节点目录每天增长 × `NODE_LOG_KEEP_HOURS` ÷ 24，再加这台机器的实例数 × 1 GiB（D 机器 16 GiB，P 机器 1 GiB），超过第 7.1 节 `df` 里节点目录所在文件系统 `Avail` 的一半；
  - vLLM 日志里有 `[PD_DIAG]` 行（一出现就处理，不等满 30 分钟）；
  - 节点目录和 vLLM 日志在同一个文件系统上时（第 7.1 节的 `df` 只出一行），把 `[BatchPutTiming]` 每天的增长 × 现场 vLLM 日志保留的天数也加进上面那一条的预算，和同一个 `Avail` 的一半比。这一条第 11 步以后才有意义，第 11 步以前 `[BatchPutTiming]` 应接近 0；
  - 做了第 11 步以后，`[BatchPutTiming]` 每台机器每天超过 43.1 GB（D 不往缓存池保存时应当是 0 行）。第 11 步以前它应接近 0，明显大于 0 时带回来，不算失败：这些行是 Mooncake 写的，回滚本套补丁去不掉。出现这种情况，说明现场的 Mooncake 可能已经不是我们核对过的那份，做第 11 步以前先把这些行带回来给我们看，再决定做不做。

**失败了：** 停下，不做下一批。这一批的每个容器按第 9 步回滚，再按本节的方式重启一次；已经成功的批不动。把这一批的检查输出、vLLM 启动日志和自检行那条查询的输出带回来。

**每批带回来的东西：**

- 第 7.1 节的命令行（`cmdline.before`）和第 7.2 节的比较结果、日志配置那一行；
- 第 7.3 节自检行查询的输出和 `check` 的结果；
- 第 7.5 节的 TTFT、TPOT；
- 每批另加：第 7.1 节的 `df`（每台机器），第 7.4 节的那一行（每台机器），第 7.3 节自检行查询的输出和留空比例查询的两次输出（D、P 每个实例都要）；
- 第 1 批另加：第 7.1 节导入核对的那一行。

**对同机其他服务的影响：** 重启本身和平时重启 vLLM 一样，同一台机器上别的容器不动。但重启以后多了持续写入：面板二的过滤器往 `<节点目录>` 写；做了第 11 步以后，换了 Mooncake 的实例还往 vLLM 日志写 `[BatchPutTiming]` 行。每天的量见第 6 步结论六。第 1、2 批做完、P 还没打补丁时，看板上只有 D 一侧的段，这是预期的。

## 8. 用检测脚本核对

**机器：** 容器里，第 7 步重启以后、第 7.2 节的检查通过以后。每个打过补丁的容器跑一次。命令和第 2 步相同，最后加 `--baseline <这个容器第 2 步记下的 baseline_token>`。

P 的容器：

```bash
python3 -I -S -B /tmp/t51-kit/detect/t51_glm53_check.py --run-dp-template <启动文件目录>/run_dp_template.sh --launch-online-dp <启动文件目录>/launch_online_dp.py --start-script <启动文件目录>/start_p.sh --mooncake-json <启动文件目录>/mooncake.json --start-mooncake <启动文件目录>/start_mooncake.sh --startup-log <容器里的 vLLM 日志文件> --baseline <第 2 步的 baseline_token>
```

D 的容器：

```bash
python3 -I -S -B /tmp/t51-kit/detect/t51_glm53_check.py --run-dp-template <启动文件目录>/run_dp_template.sh --launch-online-dp <启动文件目录>/launch_online_dp.py --start-script <启动文件目录>/start_d0.sh --mooncake-json <启动文件目录>/mooncake.json --start-mooncake <启动文件目录>/start_mooncake.sh --startup-log <容器里的 vLLM 日志文件> --baseline <第 2 步的 baseline_token>
```

**执行后马上会看到：**

- `verdict vllm=ok ascend=ok(site_local=<n>) p2d=ok mooncake=not_replaced`。`site_local=n` 是移植以外现场本地改动的文件数，不算错。第 11 步换了 Mooncake 库以后，最后一项是 `mooncake=ok`（第 11 步）。
- vLLM 一项预期是 `vllm=ok`：现场两次检测都确认 vLLM 是干净的 v0.30.0（`ced6857a`），没有本地改动。如果出现 `vllm=ok(site_local=n)`，说明现场 vLLM 有本地改动，带回来。
- `mooncake=BAD(other)`：现场的 Mooncake 库既不是原装的那一份，也不是交付的替换库（第 1–10 步没有动它）。不算这一批失败，把这一行带回来，第 11 步先不做。
- `vllm=` 行是 `state=v0.30.0+port`，行末 `port=25/25`；`ascend=` 行仍是 `state=modified`（现场有本地改动），行末 `port=11/11`。
- `strict` 行末是 `p2d=ok(changes=28/28,missing=none,struct=ok,extra=same,replacement_file=0)`。`replacement_file=0` 是正常的：现场原文件不是公开版本，重放出来的文件和替换件不同。
- `site_patches` 一行和第 2 步相同。
- `run` 行里是 `logcfg=set`：面板二的日志配置生效了（第 0 节）。
- 单独一行 `baseline=same`：重启前后各组实例的 kind、conn、spec、eager、async、load_async、tp、dp 和调度器都没变。

**这一批算失败（第 7.6 节）的：** 出现 `BASELINE_DIFF 端口:项:旧值->新值 …`（重启以后配置变了）；`verdict` 的 vllm、ascend、p2d 三项有一项不是 `ok` 或 `ok(…)`，例如 `unpatched`、`absent`、`-`（这一项没有跑）、`BAD(…)`（mooncake 一项例外：`mooncake=BAD(other)` 不算，见上）；`run` 行是 `logcfg=unset`；`site_patches` 和第 2 步不同。把全部输出带回来。

**启动日志里的三行 `PD_DIAG_STARTUP`**（在那台机器上，宿主机里）。现场的调度器和 runner 都是子类，补丁的埋点能不能执行到，取决于运行时实际构造出来的是哪个类；vLLM 补丁在启动时把它们打进 vLLM 日志（vLLM 说明第 14 节）。`model_runner` 那一行在启动时就有，`device_timing` 那一行要在第一个真实请求执行以后才有：

```bash
grep -h 'PD_DIAG_STARTUP' <vLLM 日志文件> | sed 's/.*PD_DIAG_STARTUP //' | LC_ALL=C sort | uniq -c
```

**执行后马上会看到：** 每种行一行，前面是出现的次数（vLLM 日志不在每次启动时清空的，次数会包括以前的启动，只看内容）。预期值是 vllm-ascend 一侧实测的行（t51-glm53-ascend `9dad4b6`，说明第 12.5 节）：

- **EngineCore，每个实例一行：**
  - P：`scheduler=vllm_ascend.patch.platform.patch_balance_schedule.BalanceScheduler balance=False recompute=False dyntra_lb=False batch_job=False profiling_chunk=False`
  - D：`scheduler=vllm_ascend.core.recompute_scheduler.AsyncRecomputeScheduler balance=False recompute=True dyntra_lb=False batch_job=False profiling_chunk=False`
- **Worker，每个 rank 两行：**
  - 启动时（`init_device` 之后）：`worker model_runner=vllm_ascend.worker.model_runner_v1.NPUModelRunner tp_rank=<n>`
  - 第一个真实请求执行完以后：`worker device_timing=<on|off> tp_rank=<n>`
- **`device_timing` 只在 `tp_rank=0` 上是 `on`，这是设计如此**（vllm-ascend 补丁只在 TP rank 0 记设备时间）：
  - P（TP16）：每个实例 1 行 `device_timing=on tp_rank=0`，15 行 `device_timing=off tp_rank=1`…`tp_rank=15`。**`tp_rank` 不为 0 时是 `off`，属正常。**
  - D（TP1）：每个实例 1 行 `device_timing=on tp_rank=0`。

**怎么判：**

- `scheduler` 的类、`model_runner` 的类不是上面这几种，或者 `BalanceScheduler` 带 `balance=True`：这一批失败（第 7.6 节），把这几行带回来。
- `tp_rank` 不为 0 的 `device_timing=off`：正常，设备计时按设计只在 rank 0 上开。
- D，`tp_rank=0` 是 `off`：这一批失败，把这几行带回来。这和第 7.3 节留空比例的结论一致：设备计时没开，forward、sampling 会全部留空，10 分钟以后一样会判失败；启动行在第一个请求以后就能看出来，不用等 10 分钟。
- D，`tp_rank=0` 是 `?`：带回来，不算失败，交给第 7.3 节的留空比例判。`?` 只表示第一个有 token 的步没走到 `begin_execute`，不等于设备计时没开。
- P，`tp_rank=0` 是 `off` 或 `?`：带回来，不算失败（P 只记录，第 7.3 节）。这时看板上 P 的 forward、sampling 是空的。

- 这几行不带 `[PD_DIAG]` 标记，面板二的过滤器不读，所以要在 vLLM 自己的日志里 grep。
- 一行都没有：vLLM 补丁没有生效，或者日志不在这个文件里；对照第 8 步检测脚本的 `vllm=` 行，带回来。
- `device_timing` 那一行还没出现：这个实例还没有执行过真实请求，等有请求以后再看。

带回以后照第 2 步删掉明细文件。

**对同机其他服务的影响：** 同第 2 步，只读。

## 9. 回滚

**机器：** 监控机、那台机器，然后容器里；最后按第 7 步重启。

**顺序（只重启一次）：** 要连面板二一起退回时，先做面板二那一边（新增集群说明附录 B.7，或附录 A），再做本步，最后重启一次。

**用监控机上的备份。** 先把第 3 步拷到监控机的那一份送回容器。在监控机上：

```bash
ssh <SSH 账号>@<机器地址> mkdir -p /tmp/<机器>-<时间戳> && scp <用户目录>/vllm-pd-patch-glm53/backup/<机器>-<时间戳>/* <SSH 账号>@<机器地址>:/tmp/<机器>-<时间戳>/
```

在那台机器上：

```bash
docker cp /tmp/<机器>-<时间戳>/. <容器名>:/tmp/<机器>-<时间戳>
```

在容器里核对送回来的备份：

```bash
cd /tmp/<机器>-<时间戳> && LC_ALL=C sha256sum -c SHA256SUMS && tar -tf vllm.tar | wc -l && tar -tf vllm-ascend.tar | wc -l
```

**执行后马上会看到：** `vllm.tar: OK`、`vllm-ascend.tar: OK`，`23`、`10`。

容器里的 `/tmp/t51-kit` 不在了，先重做第 1 步里容器内的那一条（用那台机器上留着的补丁包）。再按第 3 步第一段重新设好 `VLLM_PKG`、`ASCEND_PKG`、`VLLM_ROOT`、`ASCEND_ROOT`、`F`。

vLLM、vllm-ascend 主体和 p2d 一起回滚：

```bash
BK=/tmp/<机器>-<时间戳>; K=/tmp/t51-kit/patches
tar -xf "$BK/vllm.tar" -C "$VLLM_ROOT" && (cd "$VLLM_ROOT" && rm -f $(cat $K/vllm/NEW_FILES.txt) && LC_ALL=C sha256sum -c $K/vllm/SHA256SUMS.before | grep -v ': OK$')
tar -xf "$BK/vllm-ascend.tar" -C "$ASCEND_ROOT" && (cd "$ASCEND_ROOT" && rm -f $(cat $K/vllm-ascend/NEW_FILES.txt) && LC_ALL=C sha256sum -c $K/vllm-ascend/SHA256SUMS.before | grep -v ': OK$')
sha256sum "$F" | cut -c1-16
```

**执行后马上会看到：** 前两条没有任何输出，表示两个包的文件已恢复成公开版本、新加的文件已删掉；最后一行是 `2c4acb3eeb1ced7d`，表示 `mooncake_connector.py` 已恢复成现场原来那一份。

再删掉新模块的编译缓存（重启以后 Python 为新加的 4 个模块写的 `.pyc`）：

```bash
for f in $(sed "s#^#$VLLM_ROOT/#" $K/vllm/NEW_FILES.txt) $(sed "s#^#$ASCEND_ROOT/#" $K/vllm-ascend/NEW_FILES.txt); do rm -f "$(dirname "$f")/__pycache__/$(basename "$f" .py)".*.pyc; ls "$(dirname "$f")/__pycache__/$(basename "$f" .py)".*.pyc 2>/dev/null; done; echo "新模块的 .pyc 已删"
```

**执行后马上会看到：** 只有 `新模块的 .pyc 已删` 一行。

- 4 个新文件所在的目录在 v0.30.0、`d4ad49796` 里本来就有（实验室按公开源码核对过），回滚不删目录。
- 被替换过的文件恢复以后，Python 按源文件的修改时间和大小发现原来的 `.pyc` 对不上，会重新编译，不用删。

做过第 11 步的：按第 11 步的回滚恢复 Mooncake 库【待补】。

然后按第 7 步重启，重启前后照第 7.1、7.2 节核对命令行（`BK` 用这里的目录）。重启以后，`[PD_DIAG]` 记录不再写出，面板二上这个容器的实例没有新数据。

## 10. 实验室走查记录

在 `python:3.11.10-slim` 容器（`docker run --rm`；容器里 `python3 -V` 是 `Python 3.11.10`，和现场相同）里，把公开的 vLLM v0.30.0、vllm-ascend `d4ad49796`（本机 git 仓库 `git archive` 出来的干净源码）装成 `site-packages` 里的样子，用交付的补丁包，按本文原文执行第 1、3、4、9 步的命令，以及第 7 步里能在容器里做的几条。走查脚本是 `guides/lab/walk_A.sh`，两次的完整输出是 `guides/lab/walk_A-clean.txt`、`walk_A-site.txt`。

**两种情形：**

- `clean`：`mooncake_connector.py` 是公开的 `d4ad49796` 原文件。
- `site`：先用 p2d 的 `tools/simulate_site_edits.py`（模式 C：在现场改过的几个函数里，改动锚点所在的行，行为不变）造一份带本地修改的文件。

**结果：**

- 第 1 步：两处核对都通过；`/tmp` 是 `overlay`。另起一个带 `--tmpfs /tmp` 的 `--rm` 容器，单跑第 1 步那条 `df` 和第 3 步备份的第一行：类型是 `tmpfs`，备份放到 `/root`。
- 第 3 步：`PYTHONPATH=[]`，检测脚本两行都是 `from=site cands=1`；vLLM、vllm-ascend 的核对没有输出；备份是 `23` 和 `10`；拷到「那台机器」、再拷到「监控机」以后核对是 `vllm.tar: OK`、`vllm-ascend.tar: OK`、`23`、`10`。
- 第 4 步：在没有这些变量的新 shell 里，变量兜底那条报 `VLLM_PKG: parameter null or not set`，退出码不为 0；在第 3 步的 shell 里打印 `变量都在：…`。4.1、4.2 没有输出。
- 第 4.3 步：
  - `clean` 的 28 处都是 `primary`，写出的文件就是交付的替换件（`same_as_replacement=True`，sha256 `f953b27c…`），说明重放和补丁的结果相同。
  - `site` 的 28 处都定位到，写出的文件 sha256 `ef9e4e43…`。
  - 两种情形的 `p2d_extra` 都是 `X1=1 X2=1/1 X3=2 X4=4/1 X5=1 X6=ok`。
- 导入核对（第 7.1 节）：走查的镜像里没有 torch，在替身实例停掉以后改成 AST 核对，结果 `True`。照原文的导入另在 `t51g-vllmtest:v1`（Python 3.12.3，CPU 版 torch 2.13.0；torch_npu、torchair、mooncake、acl、scipy 换成替身）里，对这个包的 vLLM、vllm-ascend 替换件和 p2d 替换件跑了 3 次：都是 `True`，5.9–8.1 s，499–500 MiB，导入 4935 个模块（`guides/lab/import_measure.sh`、`import_measure.txt`）。
- 替换和重放以后的 25 + 11 + 1 个文件，在 Python 3.11.10 上都能 `py_compile`。
- 第 7.1、7.2 节：容器里起 3 个替身 `vllm serve` 进程（端口 9100–9102，带 `VLLM_LOGGING_CONFIG_PATH`）。记下命令行、按原参数重起以后打印 `命令行和重启前相同`，日志配置那条打印 `3 已设`。把其中一个的端口改成 9103 重起，`diff` 列出这一行；再起一个不带这个环境变量的，日志配置那条多出一行 `没有设：pid <n>`。
- 第 7.1、7.4 节：在容器里造一个节点目录和两个 vLLM 日志文件。记下起点以后，往节点目录写 3000000 字节，往日志写 100 行 312 字节的 `[BatchPutTiming]` 和 50 行别的内容；5 秒后量出的一行和按写进去的字节数算的相同（节点目录每天 51.8 GB，`[BatchPutTiming]` 每天 0.54 GB，`[PD_DIAG] 0 行`）。再往日志里写一行 `[PD_DIAG]`，那一项变成 `1 行`。
- 第 9 步：从「监控机」送回的备份核对是 `OK`、`23`、`10`；回滚的三条没有输出，`mooncake_connector.py` 回到第 3 步的值；走查里 `py_compile` 给 4 个新模块写过 `.pyc`，删完以后剩 0 个。走完以后两个包的 3851 个文件和走之前逐个相同（只比普通文件，不计 `__pycache__` 目录）。检测脚本在回滚以后认出 `state=v0.30.0`、`state=d4ad49796`（`site` 情形的 vllm-ascend 是那份模拟的本地修改，`state=modified changed=1`）。

**因为实验室环境，结果和现场不同的几处：**

| 步 | 实验室 | 现场应当是 |
|---|---|---|
| 1、3、9 | 没有监控机、那台机器和 `ssh`、`scp`：「监控机」「那台机器」是本机的两个目录，`scp`、`ssh … mkdir` 换成本机的 `cp`、`mkdir`；监控机上的两处核对在 `--rm` 容器里跑；`docker cp` 照原文在本机执行 | 照原文 |
| 3 | `mooncake_connector.py` 的 sha256 前 16 位是 `4e476a50afb5a7f1`（clean）、`717de022aaaf9c0b`（site） | `2c4acb3eeb1ced7d` |
| 3 | 公开源码里没有现场的两个本地补丁文件：两行都是「没有」，`grep` 报找不到文件；两个 `__init__.py` 是公开版的 `637a36ebad386a30`、`d1a45b2767e43b70` | P 上 `d7c764bb425993c0`、`c36392a6338bc2f1`，D 上只有第一个，`grep` 列出调用原函数的行；两个 `__init__.py` 是现场改过的值（待补） |
| 4.3 | clean 的 28 处都是 `primary`（现场 C25 是 `alternative_2`）；site 有 10 处用了备选锚点，按现场的停止条件应当停下，实验室有意走完，为的是验证重放本身 | 只有 C25 是 `alternative_2` |
| 4.3 | 写出文件的 sha256 是 `f953b27c…`、`ef9e4e43…` | 现场的 `<S>`，P、D 相同 |
| 7.1 | 导入核对：走查的镜像里没有 torch，改用 AST 核对 `KVCacheRecvingThread._transfer_batch`；照原文的导入在 `t51g-vllmtest:v1` 里跑，torch_npu 等是替身 | 照原文导入，打印 `True <用时> s <峰值内存> MiB` |
| 7.1、7.2 | 没有 vLLM：命令行和日志配置的两条用替身进程验证；`/health`、日志的 `grep` 和重启本身没有走 | 照原文 |
| 7.4 | 节点目录和 vLLM 日志是造的，窗口是 5 秒，宿主机上的命令在容器里执行 | 照原文，在 D 机器的宿主机上，窗口 30 分钟 |
| 9 | 回滚以后检测脚本报 `pyc=23/27,match:0`：走查里 `py_compile` 给替换过的模块写的 `.pyc` 还在，和恢复后的源文件对不上，Python 导入时会重新编译 | 重启后的 vLLM 给替换过的模块写的 `.pyc` 同样留着，同样会重新编译 |
| 2、8 | 用补丁包里的检测脚本 glm53-6（`detect/t51_glm53_check.py`），没有运行中的 vLLM、没有启动文件，只传 `--out-dir`、`--max-seconds`：打补丁前 `verdict vllm=unpatched ascend=unpatched(site_local=0) p2d=unpatched mooncake=-`；打完 `verdict vllm=ok ascend=ok(site_local=0) p2d=ok mooncake=-`，`vllm=` 行 `state=v0.30.0+port`、`port=25/25`，`strict` 行 `vllm=29/29(port:25)`、`p2d=ok(changes=28/28,missing=none,struct=ok,extra=same,replacement_file=1)`（clean）、`replacement_file=0`（site）；回滚后回到 `unpatched`。实验室没有 Mooncake（`mooncake=-`），没有 `inst[run]`、`baseline_token`、`logcfg` | 照原文：`mooncake=not_replaced`，`site_local=` 是现场本地改动的文件数，第 8 步另有 `baseline=same`、`logcfg=set` |
| 7、11 | 重启、面板二、面板一、Mooncake 依赖现场，没有走（第 7 步只走了上面几行） | 照原文 |

## 11. （可后补）替换 Mooncake 库

**前 1–10 步不依赖这一步。** vLLM、vllm-ascend 主体、p2d 三件按第 1–9 步上线；Mooncake 库可以以后单独换。

**不做这一步时：** P 侧「后台 wait_for_save → put」下面的 3 个子段为空：submit、wait_for_transfer、finalize。它们的父段 put 仍有值，P 合计、TTFT 拆解不受影响，D 侧也不受影响。第 3 批做完以后，47 段里 42 段有值；做了这一步以后 45 段（第 7.3 节）。

**做这一步的前提：** Mooncake 的现场核对（替换前的只读核对 `t51_mc_precheck.py`）结果是 `mc_pre result=ok_replace`，或者统筹另行通知。第 7.4 节在第 11 步以前就量到 `[BatchPutTiming]` 行的，说明现场的 Mooncake 可能已经不是核对过的那份：先把这些行带回来给我们看，再决定做不做这一步。

**替换：**【待补：单独的 Mooncake 增量包交付替换件、替换前的只读核对（precheck）、SHA256SUMS、这一步的完整操作和回滚方法；这个补丁包的 `patches/mooncake/` 只有说明】。增量包到了以后，换库前的只读核对用增量包带的 precheck。换库并按第 7 步重启以后，用这个补丁包里的同一份检测脚本核对（命令同第 8 步）：预期 verdict 最后一项 `mooncake=ok`，`mooncake=` 一行 `store=replacement`、`timing=2`（交付的替换库 `libmooncake_store.so`，sha256 `31317843…`）；出现 `mooncake=BAD(other)`（`store=other`），说明换上的既不是原库也不是交付的替换库，把这一行带回来。做法同第 3、4 步：先核对现场原文件的 sha256，备份并拷回监控机，再替换，再核对。重启照第 7 步，命令行、日志配置照第 7.1、7.2 节核对。

**为什么不设 `MC_LOG_DIR`（第 0 节）。** Mooncake 的传输引擎只有在设了它时才初始化 glog，把日志写进那个目录；不设时写标准错误。现场的 Worker 都没有设，换了 Mooncake 以后，`[BatchPutTiming]` 行因而写进各实例的 vLLM 日志文件。面板二的 `MOONCAKE_LOG_FILES` 填的就是这些文件：每个 P 实例 vLLM 日志文件的宿主机路径（新增集群说明第 9 步）。

**换了以后看什么：**

- P 的 vLLM 日志里有 `[BatchPutTiming]` 行（在那台 P 机器上，宿主机里）：

```bash
grep -c '\[BatchPutTiming\]' <vLLM 日志文件>
```

  **执行后马上会看到：** 每个文件一个数（只有一个文件时只有一个数），都大于 0。

- 看板上后台 wait_for_save/put 下的 3 段有值，47 段里 45 段有值（第 7.3 节）。
- 磁盘：在换了的机器上照第 7.4 节再量 30 分钟，`[BatchPutTiming]` 每台机器每天不超过 43.1 GB（P 机器；D 机器只在往缓存池保存时有），超过就回滚这一步。vLLM 日志由现场自己轮转，这一路的增长按现场保留 vLLM 日志的天数，算进 vLLM 日志所在文件系统的空间（第 6 步结论三）。

**回滚：**【待补：随 Mooncake 增量包交付】。
