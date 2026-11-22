# glm53-test：v3.2-extra 升级操作手册

> 把 glm5.3 测试集群（面板二里的集群 `test-glm53`）从 v3.1-extra 升级到 v3.2-extra：补丁从 T51 升到 T56，面板二换成 v3.2-extra。补丁包 `t56-glm53-patch-kit.tar`（sha256 `0d2bc9a86a395046…`）和面板包都不变，本手册放在包外面。命令从《glm5.3 测试集群打补丁操作手册》（`48c88aa7…`）和本手册的命令来源原样照抄。

<!-- 占位符版开始 -->
> **本手册有两版。** 这一版是占位符版：路径写成尖括号里的说明，不含任何内网信息。已填路径版把监控机上的个人路径填好，只放在私有的 Caching 仓库。
<!-- 占位符版结束 -->

## 第一页

**做什么。** 这次：

1. **第一部分：** 升级前，用一组参考请求跑 3 遍，记下 TPOT（开销判据的基线）。
2. **第二部分：** 集群照常运行时，在 P、D 两个容器里放补丁包、检测、核对、备份，把 p2d 的结果先写到 `/tmp`。
3. **第三部分：** 整个集群停一次，两个容器都换成 T56 的文件，一个 D 容器做导入核对，然后一起拉起。不分批。
4. **第四部分：** 拉起以后核对：检测脚本、启动行（不对就停）、过滤器自检、留空比例、磁盘、旧面板的安全检查。
5. **第五部分：** 换面板二（v3.2-extra）。
6. **第六部分：** 换面板以后，用一条只读查询做首批数据核对。
7. **第七部分：** 升级后，同一组参考请求再跑 3 遍，比 TPOT。
8. **第八部分：** 第一步开销分析（只读查询，必做）。

**前提：** T51 的打补丁手册、面板手册（含第 16–22 步）已经做完。proxy、Mooncake 做没做都可以：没做 proxy 时，两个 LB proxy 段为空；T56 不动 proxy，也不动 Mooncake。

新内容只在 D 上写；P 也换成 T56，写出的东西和 T51 逐字段相同（实验室测过）。Mooncake 已经换过库和没换都是正常状态。不新建镜像，不新建容器，改动写在容器的可写层里。

**做完是什么样子。**
- P、D 两个容器的检测脚本都报 `verdict vllm=ok(t56) ascend=ok(t56,site_local=<n>) p2d=ok(t56) mooncake=<和升级前相同>`。
- D 的 `tp_rank=0` Worker 启动行列出 6 个 stage。
- 面板二 `test-glm53` 的看板在 v3.2-extra 下，D 侧多出几段：`start_load_kv` 下的 `receive_queue`、`prepare_read`，`kv_ready_wait`，第一步、第二步起的 `prepare_inputs`、`lm_head`、`draft`。
- 首批核对的查询 `RESULT: PASS`；TPOT 判据 `OK`。

**停机：** 第三部分从第 11 步停集群到第 15 步拉起。停以前的第 2–10 步只读，或者只往 `/tmp` 和备份目录里写，不影响服务。第五部分换面板不碰 vLLM。

**要先准备好的现场值：**

<!-- 占位符表开始 -->
| 占位符 | 含义 | 在哪里查 |
|---|---|---|
| `<用户目录>` | 监控机上放部署包和更新脚本的目录 | 包二 v3 更新脚本「现场路径」一段的 `USER_DIR` |
| `<总目录>` | 面板目录的上级目录 | 同一个脚本的 `ROOT` |
<!-- 占位符表结束 -->
| `<SSH 账号>`、`<机器地址>` | 从监控机登录 glm5.3 机器用的账号和机器 | T51 打补丁时用的值 |
| `<机器>` | 这台机器的简称，只用在监控机上备份目录的名字里；同一台机器每次写法相同 | 自己定，和 T51 时相同即可 |
| `<容器名>` | 这台机器上跑 vLLM 的容器 | 在那台机器上 `docker ps` |
| `<节点目录>` | 面板二的过滤器在这台机器上写文件的目录（宿主机路径） | `/data/pd-diag`，和 T51 时相同 |
| `<vLLM 日志文件>` | 这台机器上各个 vLLM 实例的标准输出、标准错误重定向到的文件（宿主机路径），几个文件用空格隔开；D 机器有 16 个 | 和 T51 时相同 |
| `<启动文件目录>` | 容器里 `run_dp_template.sh`、`launch_online_dp.py`、`start_p.sh`（D 上 `start_d0.sh`）、`mooncake.json`、`start_mooncake.sh` 所在的目录 | 和 T51 时相同 |
| `<容器里的 vLLM 日志文件>` | `<vLLM 日志文件>` 在容器里看到的路径；容器里看不到就不传 `--startup-log` | 同上 |
| `<T51 的备份目录>` | T51 打补丁时第 6 步在容器里做的备份目录，`/tmp/t51-glm53-backup-<时间戳>`（`/tmp` 是 tmpfs 时在 `/root` 下） | T51 时记下的 `<备份目录>`；容器里没有了，见第 7 步「不对时」 |
| `<proxy 地址>` | 第一、七部分的参考请求发到的 proxy，`<IP 或主机名>:<端口>` | 平时发请求用的 proxy 地址 |
| `<模型目录>`、`<服务名>` | P 容器里的模型目录；请求里的模型名（vLLM 启动时的 `--served-model-name`，没设就是模型目录） | P 的启动文件 |
| 端口 | D 的容器是 `$(seq 9100 9115)`（16 个实例），P 的容器是 `9900`。现场不同时按同样的格式改 | 第 4 步检测的 `inst[run …]` 行的 `ports=` |

做的过程中还要记下几个值（第 4 步的 `baseline_token`、第 8 步的 `<备份目录>`、`<时间戳>`、第 9 步的 `<S56>`、第 1 步的 3 个 TPOT），在那一步里写明，记进下面的记录表。

## 通用约定

- 所有容器里的命令都以 root 执行：先在那台机器上 `docker exec -it <容器名> bash` 进入容器。
- 任何一步的结果和「应当看到」不一样时，停下，把那一步的输出带回来，不要继续。
- `LC_ALL=C sha256sum -c … | grep -v ': OK$'` 这类核对命令，没有输出就是全部通过，这时整条管道的退出码是 1。不要把它们放进带 `set -e` 的脚本，按输出判断。
- `docker restart` 以后补丁还在；`docker rm` 后重建容器、或者换镜像，补丁就没了。
- 不设 `MC_LOG_DIR`；不设 `VLLM_PD_DEVICE_STAGES`（除非第七部分的判据要求）。
- 三件补丁必须一起换、一起回滚：vllm-ascend 主体和 p2d 用到的名字只有 vLLM 补丁里有。
- **窗口：** 监控机一个窗口；P、D 两台机器各开一个宿主机窗口和一个容器窗口。容器窗口从第 3 步开到第 15 步拉起以前都不要关：第 5、6、8 步设的变量只在这个 shell 里。中途断了，按第 12 步「不对时」第一行重新设。
- **同一条命令，每个容器各执行一次。** 写「P 的容器」的在 P 的容器窗口执行；写「D 的容器」的在 D 的容器窗口执行；没有分 P、D 的，两个窗口都执行。宿主机窗口、监控机上的命令同样按「每台机器一次」执行。

**记录表：**

| `<机器>` | 角色 | `<容器名>` | `baseline_token`（第 4 步） | `<备份目录>`、`<时间戳>`（第 8 步） | `<S56>` 的前 16 位（第 9 步） |
|---|---|---|---|---|---|
| | P | | | | |
| | D | | | | |

| 第 1 步升级前的 Median TPOT（3 遍） | 第 32 步升级后的 Median TPOT（3 遍） |
|---|---|
| | |

## 第一部分　升级前：参考请求 3 遍

#### 第 1 步　参考请求跑 3 遍，记下 TPOT

**机器：** 甲在 P 的容器窗口里执行，集群照常运行（T51 版）。乙用你自己平时那套工具。

**用哪一种：** 优先用甲。甲用不了（例如报 `vllm: command not found`，或者发不到 proxy），改用乙。升级前后必须用同一种、同一组参数。

**甲：** vLLM 自带的压测，发到 proxy。同一条命令跑 3 遍，每遍之间不用等：

```bash
vllm bench serve --backend openai --base-url http://<proxy 地址> --endpoint /v1/completions --model <模型目录> --served-model-name <服务名> --dataset-name random --random-input-len 1024 --random-output-len 128 --ignore-eos --num-prompts 64 --max-concurrency 8 --seed 0 2>&1 | tee -a /tmp/t56-tpot-<升级前或升级后>.txt | grep -E 'Successful requests|Failed requests|Median TPOT'
```

- `--model` 填 P 容器里的模型目录：它同时用来加载分词器，生成随机请求要用。`--served-model-name` 填请求里的模型名，就是服务启动时的 `--served-model-name`；服务没设这个名字时，这一项可以不写。服务启动带 `--trust-remote-code` 时，这条命令末尾也要加 `--trust-remote-code`。
- `<升级前或升级后>` 这次填 `升级前`：3 遍的完整输出都追加在 `/tmp/t56-tpot-升级前.txt` 里。
- 参数：随机数据，输入 1024 个 token、输出 128 个（`--ignore-eos`：不提前结束），64 个请求，最多同时 8 个，随机种子 0。
- 这条命令在 P 容器里只当客户端用，往 proxy 发请求；参数名按 vLLM v0.30.0 的源码核过。

**乙：** 用你自己的工具，发同一组请求、同样的并发，跑 3 遍，每遍记下它给出的 TPOT 中位数。

**应当看到：** 甲每遍三行：`Successful requests: 64`、`Failed requests: 0`、`Median TPOT (ms): <数>`。把 3 遍的 `Median TPOT` 记进记录表。实验室（CPU、2 层小模型，直接发给 vLLM）一遍约 15 秒；现场一遍要多久没有量过。

**不对时：**
- 有失败的请求（`Failed requests` 不是 0）：这一遍不算，查清原因再跑。
- `vllm bench serve` 报 NPU 相关的错误：改用乙，用你自己的工具。

**带回：** 3 遍各自的那三行，以及用的是甲还是乙。

## 第二部分　停集群以前：放进容器，检测，核对，备份（集群照常运行）

这一段的命令只读，或者只往 `/tmp`、备份目录里写，不影响运行中的 vLLM。有一个容器不对，就停在这里，集群照常运行。

#### 第 2 步　在监控机上核对补丁包，拷到两台机器

**机器：** 监控机。补丁包 `t56-glm53-patch-kit.tar` 和它的 `t56-glm53-patch-kit.tar.sha256` 放在 `<用户目录>/vllm-pd-patch-glm53/` 下。第一条执行一次；第二条每台机器执行一次，换 `<机器地址>`。

**命令：**

```bash
cd <用户目录>/vllm-pd-patch-glm53 && LC_ALL=C sha256sum -c t56-glm53-patch-kit.tar.sha256
```

```bash
scp <用户目录>/vllm-pd-patch-glm53/t56-glm53-patch-kit.tar <SSH 账号>@<机器地址>:/tmp/
```

**应当看到：** 第一条打印 `t56-glm53-patch-kit.tar: OK`。第二条两次都没有报错。

**不对时：** 第一条不是 `OK`：补丁包损坏，重新取一份，不往下做。

#### 第 3 步　拷进容器，解开，核对

**机器：** 每台机器：先在宿主机窗口执行第一条，再在容器窗口执行后两条。

**命令：**

```bash
docker cp /tmp/t56-glm53-patch-kit.tar <容器名>:/tmp/
```

```bash
mkdir -p /tmp/t56-kit && tar -xf /tmp/t56-glm53-patch-kit.tar -C /tmp/t56-kit && cd /tmp/t56-kit && LC_ALL=C sha256sum -c SHA256SUMS | grep -v ': OK$'; sha256sum /tmp/t56-kit/detect/t51_glm53_check.py | cut -c1-16
```

```bash
df -hT /tmp | tail -n 1
```

**应当看到：** 第二条只有一行 `588e6d94d278a876`（检测脚本的 sha256 前 16 位）。第三条一行，第二列是文件系统类型，一般是 `overlay`；是 `tmpfs` 时，`/tmp` 下的文件在容器重启以后就没了，第 8 步会自动把备份放到 `/root` 下。

**不对时：** 第二条在那一行之前有输出：补丁包在拷贝中损坏，重新拷贝。

**带回：** `df` 那一行（每个容器）。每台机器上的 `/tmp/t56-glm53-patch-kit.tar` 留着，回滚时要用。

#### 第 4 步　升级前的检测

**机器：** 容器窗口，集群照常运行（检测要读运行中的实例）。检测脚本 `/tmp/t56-kit/detect/t51_glm53_check.py`（glm53-7，sha256 `588e6d94d278a876…`）只读，几秒内结束，只往 `/tmp` 写一个明细文件；按 `nice 19`、IO 空闲级运行，读取量上限 1 GiB，耗时上限 300 秒。和 T51 用的 glm53-6 相比，它多认一版补丁（T56），`verdict` 里的 `ok` 带上版本名。

**命令：** P 的容器：

```bash
python3 -I -S -B /tmp/t56-kit/detect/t51_glm53_check.py --run-dp-template <启动文件目录>/run_dp_template.sh --launch-online-dp <启动文件目录>/launch_online_dp.py --start-script <启动文件目录>/start_p.sh --mooncake-json <启动文件目录>/mooncake.json --start-mooncake <启动文件目录>/start_mooncake.sh --startup-log <容器里的 vLLM 日志文件>
```

D 的容器：

```bash
python3 -I -S -B /tmp/t56-kit/detect/t51_glm53_check.py --run-dp-template <启动文件目录>/run_dp_template.sh --launch-online-dp <启动文件目录>/launch_online_dp.py --start-script <启动文件目录>/start_d0.sh --mooncake-json <启动文件目录>/mooncake.json --start-mooncake <启动文件目录>/start_mooncake.sh --startup-log <容器里的 vLLM 日志文件>
```

带回以后删掉明细文件（路径照抄 `detail=` 那一行）：

```bash
rm -f <detail= 那一行的路径>
```

**应当看到：** 不超过 20 行，退出码 0。其中：

- `vllm=` 行 `state=v0.30.0+t51`，行末 `port=t51:25/25,t56:21/25`；`ascend=` 行仍是 `state=modified`，行末 `port=t51:11/11,t56:9/11`。另一版的数小一些，是两版之间不同的那几个文件，不是错。两行末尾都是 `from=site cands=1`。
- `site_patches` 一行和 T51 时相同：`patch/platform/patch_kv_load_failure_reason.py=d7c764bb425993c0 patch/platform/patch_prefill_length_validation.py=c36392a6338bc2f1`；D 上最后一项是 `absent`。
- **运行模式：** D 的 `inst[run …] kind=D` 一行是 `tp=1 dp=64 … eager=0 async=1`，16 个实例；P 的一行是 1 个实例。
- `run` 行里是 `logcfg=set`（T51 加的两行 `export` 还在）。
- `verdict vllm=ok(t51) ascend=ok(t51,site_local=<n>) p2d=ok(t51) mooncake=<…>`。
- **Mooncake 两种状态都正常：** 已经按例外换过库的是 `mooncake=ok`，`mooncake=` 那一行是 `store=replacement changed_since_install=1`；没换的是 `mooncake=not_replaced`。记下是哪一种，第 17 步要相同。
- 最后一行 `fingerprint=… baseline_token=<一串>`：把 `baseline_token=` 后面的整串记进记录表，第 17 步要用。

**不对时：**

| 看到 | 怎么办 |
|---|---|
| `verdict` 的前三项不是 `ok(t51…)` | 停下，带回输出：T51 的状态和打完时不同 |
| `inst[run …]` 的 `eager=`、`async=`、`tp=`、`dp=` 和上面不同 | 停下，带回这一行：运行模式和设计依据的不一样 |
| `site_patches` 的后两个和上面不同 | 停下，带回输出 |
| `mooncake=BAD(other)` | 带回这一行，照常往下做 |
| 只打印一行 `BLOCKED`（自查认出输出里有 IP、主机名之类） | 带回这一行，不往下做 |
| 只打印一行 `REFUSED` | `-I -S -B` 少了，照原文重新执行 |

**带回：** 终端打印的全部行（每个容器），每个容器的 `baseline_token`。

#### 第 5 步　找到安装目录

**机器：** 容器窗口，接着第 4 步。只查找位置，不导入 vLLM。

**命令：**

```bash
VLLM_PKG=$(python3 -c "import importlib.util as u; print(u.find_spec('vllm').submodule_search_locations[0])")
ASCEND_PKG=$(python3 -c "import importlib.util as u; print(u.find_spec('vllm_ascend').submodule_search_locations[0])")
VLLM_ROOT=$(dirname "$VLLM_PKG"); ASCEND_ROOT=$(dirname "$ASCEND_PKG"); F="$ASCEND_PKG/distributed/kv_transfer/kv_p2p/mooncake_connector.py"
echo "PYTHONPATH=[${PYTHONPATH:-}]"; echo "$VLLM_ROOT"; echo "$ASCEND_ROOT"; echo "$F"
```

**应当看到：** 四行。第一行是 `PYTHONPATH=[/data/pd-diag/conf]`（T51 加的 `export`）或者 `PYTHONPATH=[]`（容器窗口不是从启动脚本里开的）；第二、三行是同一个 `site-packages` 目录；第四行是它下面的 `vllm_ascend/distributed/kv_transfer/kv_p2p/mooncake_connector.py`。

**不对时：** 第二、三行不是同一个目录，或者第 4 步不是 `from=site cands=1`：停下，带回这四行和第 4 步那两行。

#### 第 6 步　核对要改的文件和现场的本地补丁

**机器：** 容器窗口，接着第 5 步。只读。

**命令：**

```bash
K=/tmp/t56-kit/patches
(cd "$VLLM_ROOT" && LC_ALL=C sha256sum -c $K/vllm/SHA256SUMS.t51 | grep -v ': OK$')
(cd "$ASCEND_ROOT" && LC_ALL=C sha256sum -c $K/vllm-ascend/SHA256SUMS.t51 | grep -v ': OK$'; LC_ALL=C sha256sum -c $K/vllm-ascend/SHA256SUMS.d4ad49796-unchanged | grep -v ': OK$')
(cd "$ASCEND_PKG/patch" && for f in __init__.py platform/__init__.py platform/patch_kv_load_failure_reason.py platform/patch_prefill_length_validation.py; do [ -e "$f" ] && echo "$f $(sha256sum "$f" | cut -c1-16)" || echo "$f 没有"; done)
```

**应当看到：** 前两段没有任何输出；最后四行是文件名和 sha256 前 16 位，和 T51 时相同（D 上最后一个文件 `没有`）。

- 第一段核的是 vLLM 的 25 个文件，和 T51 打完以后的值逐个相同（`SHA256SUMS.t51`）。
- 第二段核的是 vllm-ascend 的 11 个文件，同样和 T51 打完以后相同；另外核 `platform.py`、`utils.py` 和公开的 d4ad49796 相同（`SHA256SUMS.d4ad49796-unchanged`）。「是不是 D」用的是 `utils.py` 里的条件，这个条件又和 `platform.py` 换调度器的条件一样，所以这两个文件不能有本地改动。
- `recompute_scheduler.py` 是 T51 移植的 11 个文件之一，第一段已经核了它和 T51 版逐字节相同。

**不对时：**

| 看到 | 怎么办 |
|---|---|
| 第一段或第二段的 `SHA256SUMS.t51` 有 `FAILED` | 停下，带回输出：文件和 T51 打完时不同 |
| D 的容器：`platform.py` 或 `utils.py` 是 `FAILED` | 停下，带回输出：D 上「是不是 D」的判断依赖这两个文件 |
| P 的容器：`platform.py` 或 `utils.py` 是 `FAILED` | 带回输出，照常往下做：P 是 `kv_producer`，判断结果总是「不是 D」，不受这两个文件影响 |
| 后四行和 T51 时不同 | 停下，带回输出 |

11-19 第 2 次检测时，P、D 的 vllm-ascend 都只改了 3 个文件（`mooncake_connector.py`、`patch/__init__.py`、`patch/platform/__init__.py`），`platform.py`、`utils.py` 和 d4ad49796 相同。

#### 第 7 步　p2d：取出现场原文件，证明现场文件是 T51 重放出来的，看现场那 8 行在哪一段

**机器：** 容器窗口，接着第 6 步。只读，只往 `/tmp` 写。P、D 都要做。`<T51 的备份目录>` 填 T51 时记下的备份目录。

**命令：** 取出现场原文件并核对：

```bash
ORIG=/tmp/mooncake_connector.orig.py
tar -xOf <T51 的备份目录>/vllm-ascend.tar vllm_ascend/distributed/kv_transfer/kv_p2p/mooncake_connector.py > "$ORIG"
sha256sum "$ORIG" | cut -c1-16; sha256sum "$F" | cut -c1-16; grep -c 'kv_load_envelope_ns' "$F"
```

用 T51 版的 `replay_p2d.py` 在原文件上重放，结果应当就是现场当前的文件：

```bash
python3 -I $K/p2d/t51/replay_p2d.py "$ORIG" --out /tmp/p2d_t51_check.py; echo "exit=$?"; sha256sum /tmp/p2d_t51_check.py "$F" | cut -c1-16
```

现场那 8 行改动落在哪一段（只读，不导入 vLLM）：

```bash
python3 -I -S -B $K/p2d/field_check/span_diff.py "$F"; echo "exit=$?"
```

**应当看到：**

- 第一条三行：`2c4acb3eeb1ced7d`（现场原文件）；T51 时第 8 步记下的 `<S>` 的前 16 位（T51 版写出的文件）；`0`（还没有 T56 的字段）。
- 第二条：`exit=0`，然后两行 sha256 前 16 位，两行相同。
- 第三条：一行 `p2d_span_diff spans=instrumented …`，然后 `exit=0`。原样带回这一行。`func_lines=` 按推算是 301（原文件 292 行加 T51 插入的 9 行），没有在现场量过；不是 301 也照常往下做，带回。

**不对时：**

| 看到 | 怎么办 |
|---|---|
| 第一条报 `No such file or directory` | 容器里的 T51 备份没了：从监控机的 T51 备份 `<用户目录>/vllm-pd-patch-glm53/backup/<机器>-<T51 的时间戳>/` 拷回容器（照第 34 步的两条命令，换成 T51 的目录名），再执行 |
| 第一行不是 `2c4acb3eeb1ced7d` | 停下，带回：取出的不是现场原文件 |
| 第三行不是 `0` | 停下，带回：现场文件已经有 T56 的改动 |
| 第二条两行不同 | 停下，带回：现场文件不是 T51 重放出来的那一份 |

**带回：** 三条命令的完整输出（P、D 各一份）。

#### 第 8 步　备份 T51 版，拷回监控机（必做）

**机器：** 容器窗口执行第一条；然后在宿主机窗口执行第二条；最后在监控机上执行后两条（每台机器一次）。

**命令：**

```bash
BKBASE=/tmp; [ "$(stat -f -c %T /tmp)" = tmpfs ] && BKBASE=/root
BK=$BKBASE/t56-glm53-backup-$(date +%Y%m%d-%H%M%S); mkdir -p "$BK"; echo "$BK"
(cd "$VLLM_ROOT" && tar -cf "$BK/vllm.tar" $(awk '{print $2}' $K/vllm/SHA256SUMS.t51))
(cd "$ASCEND_ROOT" && tar -cf "$BK/vllm-ascend.tar" $(awk '{print $2}' $K/vllm-ascend/SHA256SUMS.t51) vllm_ascend/distributed/kv_transfer/kv_p2p/mooncake_connector.py)
(cd "$BK" && sha256sum vllm.tar vllm-ascend.tar > SHA256SUMS && tar -tf vllm.tar | wc -l && tar -tf vllm-ascend.tar | wc -l && awk '{print substr($1, 1, 16), $2}' SHA256SUMS)
```

```bash
docker cp <容器名>:<备份目录> /tmp/
```

```bash
mkdir -p <用户目录>/vllm-pd-patch-glm53/backup && scp -r <SSH 账号>@<机器地址>:/tmp/t56-glm53-backup-<时间戳> <用户目录>/vllm-pd-patch-glm53/backup/t56-<机器>-<时间戳>
```

```bash
cd <用户目录>/vllm-pd-patch-glm53/backup/t56-<机器>-<时间戳> && LC_ALL=C sha256sum -c SHA256SUMS && tar -tf vllm.tar | wc -l && tar -tf vllm-ascend.tar | wc -l
```

**应当看到：** 第一条打印备份目录名 `/tmp/t56-glm53-backup-<时间戳>`（`/tmp` 是 tmpfs 时在 `/root` 下），然后 `25`、`12` 和两个 tar 的 sha256 前 16 位。最后一条打印 `vllm.tar: OK`、`vllm-ascend.tar: OK`、`25`、`12`。把备份目录名记作 `<备份目录>`，末尾的 `<年月日>-<时分秒>` 记作 `<时间戳>`，记进记录表。

**不对时：** 数字不是 `25`、`12`，或者监控机上有 `FAILED`：重新拷；两台机器在监控机上都核对通过以前，不停集群。

**带回：** `<备份目录>`（每个容器）。备份是 T51 版的 25 个、11 个文件和 T51 重放出来的 `mooncake_connector.py`，回滚到 T51 用。

#### 第 9 步　p2d：用 T56 版重放，写到 `/tmp`

**机器：** 容器窗口，接着第 8 步。P、D 都要做。`replay_p2d.py` 的 sha256 是 `9b12baa6b38c8ff7d697c450ad80a9e0b48889f384dcdbddc33e04c2ee5e553e`。这一步不动现场文件：结果只写到 `/tmp`，停集群以后第 13 步才换上。

**命令：** 只检查、不写文件：

```bash
python3 -I $K/p2d/replay_p2d.py "$ORIG" --check; echo "exit=$?"
```

把重放的结果写到 `/tmp`：

```bash
python3 -I $K/p2d/replay_p2d.py "$ORIG" --out /tmp/mooncake_connector.p2d56.py; echo "exit=$?"
```

**应当看到：**

- 第一条最后两行是 `replay_p2d: check ok: 29 of 29 changes located, result checks passed, nothing written` 和 `exit=0`；29 行定位结果里只有 C25 是 `alternative_2`，其余（含新加的 C29）都是 `primary`。
- 第二条最后两行：`replay_p2d: written out=/tmp/mooncake_connector.p2d56.py sha256=<S56> changes=29/29 py_compile=ok same_as_replacement=False` 和 `exit=0`。把 `<S56>` 的前 16 位记进记录表：P、D 的 `<S56>` 必须相同。

**不对时：**

| 看到 | 怎么办 |
|---|---|
| 别的编号也用了备选锚点，或者 C25 回到 `primary` | 停下，带回输出：现场文件和小核对时不同 |
| P、D 的 `<S56>` 不同 | 停下，带回 |
| 退出码 1 | 参数错，读不了输入，或输入不是合法的 Python：核对路径和文件 |
| 退出码 2、3、4 | 不写文件，带回完整输出 |
| 退出码 5 | 写出的文件 `py_compile` 不通过：不要用 `/tmp` 下的文件，带回输出 |
| 退出码 6 | 输入不对：给的是 T51 版（应当给第 7 步取出的原文件 `$ORIG`），或者已经是 T56 版（不要重复打）。看最后一行的说明 |

退出码不为 0 时现场文件没有被动过。不要手工修改 `replay_p2d.py` 或现场文件去绕过检查。

**带回：** 两条命令的完整输出和 `<S56>`（P、D 各一份）。

#### 第 10 步　停集群以前：记下命令行和磁盘

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

## 第三部分　停整个集群，换文件，拉起（停机）

**顺序：** 第 11 步停整个集群。然后先在 D 的容器里做完第 12、13 步和第 14 步的导入核对；导入核对通过以后，P 的容器再做第 12、13 步。两个容器都做完，第 15 步一起拉起。

#### 第 11 步　停整个集群

**机器：** 按平时停集群的方式停；停完以后在每个容器窗口里核对。

**怎么停：** 现场容器的 1 号进程是 `bash`，vLLM 由 `launch_online_dp.py` 拉起。按平时的方式停掉两个容器里的全部 vLLM 实例。停的是容器里的 vLLM，不要 `docker rm` 容器。记下停的时刻（第 24 步用）。

**命令：** 容器里还有没有 vLLM 进程（每个容器窗口）：

```bash
for d in /proc/[0-9]*; do a=$(tr '\0' ' ' < "$d/cmdline" 2>/dev/null); case "$a" in *"vllm serve"*|*vllm.entrypoints.openai.api_server*) tr '\0' '\n' < "$d/environ" | grep -q '^VLLM_LOGGING_CONFIG_PATH=.*/pd_diag_logging\.json$' && echo "已设" || echo "没有设：pid ${d#/proc/}";; esac; done | sort | uniq -c
```

**应当看到：** 没有输出：这个容器里已经没有 vLLM 进程。这条命令要在容器窗口里直接执行；用 `bash -c '…'` 包起来执行时，执行它的那个 shell 的命令行里也有 `vllm serve`，会被当成 vLLM 进程列出来。

**不对时：**

| 看到 | 怎么办 |
|---|---|
| 有 `已设` 或 `没有设` 的行 | 还有 vLLM 进程没停：按平时的方式停掉，再执行一次 |
| 容器窗口断开了 | 容器起来以后重新进入容器窗口，按第 12 步「不对时」第一行重新设变量；`/tmp` 是 `tmpfs` 的，先重做第 3 步的前两条和第 7 步的第一条、第 9 步的第二条（写到 `/tmp` 的文件没了），`<S56>` 要和记录表相同 |

#### 第 12 步　换上 vLLM 和 vllm-ascend 的 T56 文件

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

**应当看到：** 第一条打印 `变量都在：<备份目录>`；后两条没有任何输出（25 个、11 个文件都和 T56 的 `SHA256SUMS.after` 一致）。T56 不新增文件，也不删文件，所以不用清编译缓存：改过的 `.py` 时间变了，Python 会自己重新编译。

**不对时：**

| 看到 | 怎么办 |
|---|---|
| 第一条报 `parameter null or not set` | 换过 shell：按第 5 步重新找目录，再设 `K=/tmp/t56-kit/patches` 和 `BK=<备份目录>`（不要重做备份），然后再执行 |
| 后两条有 `FAILED` | 停下，已经换过文件的容器按第九部分回滚 |

#### 第 13 步　换上 p2d 的结果

**机器：** 容器窗口，接着第 12 步。

**命令：**

```bash
cp -p "$F" "$F.p2d_t51" && cp /tmp/mooncake_connector.p2d56.py "$F" && sha256sum "$F" | cut -c1-16
```

**应当看到：** `<S56>` 的前 16 位，和记录表里这个容器的相同。T51 版留在 `"$F.p2d_t51"`，回滚时第九部分用的是备份，不用它。

**不对时：** 报 `No such file or directory`：`/tmp` 下的文件没了，重做第 9 步的第二条再执行。前 16 位和记录表不同：停下，带回。

#### 第 14 步　导入核对（只在 D 的容器里）

**机器：** 容器窗口。只在 D 的容器里、第 12、13 步做完以后执行一次。

**命令：**

```bash
python3 -B -c "import time, resource; t = time.time(); import vllm_ascend.distributed.kv_transfer.kv_p2p.mooncake_connector as m; print(hasattr(m.KVCacheRecvingThread, '_transfer_batch'), '%.1f s' % (time.time() - t), '%d MiB' % (resource.getrusage(resource.RUSAGE_SELF).ru_maxrss // 1024))"
```

**应当看到：** 一行 `True <用时> s <峰值内存> MiB`。这条命令导入 torch、torch_npu、vLLM 和这个模块用到的部分，导入完就退出，不加载模型，不开端口。

**不对时：** 不是 `True`，或者报错：P 的容器不再换文件；D 的容器按第九部分回滚（P 没有换过文件，不用回滚），再拉起整个集群，带回这一行。

**带回：** 这一行。

#### 第 15 步　拉起整个集群

**机器：** 按平时拉起集群的方式；拉起完成以后在每个容器窗口里核对。

**怎么起：** 两个容器都做完第 12、13 步以后，用原来的启动文件拉起全部实例（P、D 各用自己的启动文件，和第 4 步检测时传的相同）。参数和停之前相同；不加任何新的环境变量（T51 的两行 `export` 已经在启动脚本里）。记下拉起的时刻（第 24 步用）。

**命令：** 每个实例都起来了：

```bash
for p in $(seq 9100 9115); do printf "%s " "$p"; curl -s -o /dev/null -w '%{http_code}\n' localhost:$p/health; done
```

```bash
for p in 9900; do printf "%s " "$p"; curl -s -o /dev/null -w '%{http_code}\n' localhost:$p/health; done
```

**应当看到：** 每个端口一行，都是 `200`（D 的容器 16 行，P 的容器 1 行）。

**不对时：** 有实例的 `/health` 不是 `200`，或者起不来：这次升级失败，按第 25 步处理。

## 第四部分　拉起以后的核对（全部实例）

#### 第 16 步　拉起以后，在容器里核对

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

**不对时：** 有 `diff` 的输出、有「没有设」的行、或者最后一条有输出：这次升级失败，按第 25 步处理，带回输出。

#### 第 17 步　用检测脚本核对

**机器：** 容器窗口，第 16 步通过以后。

**命令：** P 的容器：

```bash
python3 -I -S -B /tmp/t56-kit/detect/t51_glm53_check.py --run-dp-template <启动文件目录>/run_dp_template.sh --launch-online-dp <启动文件目录>/launch_online_dp.py --start-script <启动文件目录>/start_p.sh --mooncake-json <启动文件目录>/mooncake.json --start-mooncake <启动文件目录>/start_mooncake.sh --startup-log <容器里的 vLLM 日志文件> --baseline <升级前检测的 baseline_token>
```

D 的容器：

```bash
python3 -I -S -B /tmp/t56-kit/detect/t51_glm53_check.py --run-dp-template <启动文件目录>/run_dp_template.sh --launch-online-dp <启动文件目录>/launch_online_dp.py --start-script <启动文件目录>/start_d0.sh --mooncake-json <启动文件目录>/mooncake.json --start-mooncake <启动文件目录>/start_mooncake.sh --startup-log <容器里的 vLLM 日志文件> --baseline <升级前检测的 baseline_token>
```

带回以后照第 4 步删掉明细文件。

**应当看到：**

- `verdict vllm=ok(t56) ascend=ok(t56,site_local=<n>) p2d=ok(t56) mooncake=<和第 4 步相同>`，P、D 都是。**三项都要是 `t56`。**
- `vllm=` 行 `state=v0.30.0+t56`，行末 `port=t51:21/25,t56:25/25`；`ascend=` 行行末 `port=t51:9/11,t56:11/11`。
- `strict` 行末是 `p2d=ok(t56,changes=29/29,missing=none,struct=ok,extra=same,replacement_file=0)`。
- `site_patches` 一行和第 4 步相同；`run` 行里是 `logcfg=set`；单独一行 `baseline=same`。

**不对时：**

| 看到 | 怎么办 |
|---|---|
| 三项里有 `ok(t51)`，例如 `vllm=ok(t56) ascend=ok(t51)` | 只升了一半：这次升级失败，按第 25 步处理 |
| 任何一项是 `BAD(…)` 或 `unpatched` | 这次升级失败，带回全部输出 |
| `mooncake=` 和第 4 步不同 | 停下，带回：T56 不动 Mooncake |
| 有 `BASELINE_DIFF` | 配置变了：停下，带回这一行 |

#### 第 18 步　启动行（不对就停）

**机器：** 那台机器上，宿主机里。`device_timing` 那一行要在第一个真实请求执行以后才有：先发几个请求。

**命令：**

```bash
grep -h 'PD_DIAG_STARTUP' <vLLM 日志文件> | sed 's/.*PD_DIAG_STARTUP //' | LC_ALL=C sort | uniq -c
```

**应当看到：** 每种行一行，前面是出现的次数。glm53-test 是 P 1 台（1 个实例，TP16）、D 1 台（16 个实例，TP1）：

- **D：**
  - EngineCore 每个实例一行：`scheduler=vllm_ascend.core.recompute_scheduler.AsyncRecomputeScheduler balance=False recompute=True dyntra_lb=False batch_job=False profiling_chunk=False`；
  - Worker 每个实例两行：`worker model_runner=vllm_ascend.worker.model_runner_v1.NPUModelRunner tp_rank=0`，以及 `worker device_timing=on tp_rank=0 runner_stages=forward,sampling,prepare_inputs,lm_head,draft,bookkeeping source=default`。
  - **数一下：** 带 `runner_stages=` 的 `tp_rank=0` 那一行前面是 `16`，`scheduler=…AsyncRecomputeScheduler` 那一行前面也是 `16`（这台机器的 D 实例数）。
- **P：**
  - EngineCore：`scheduler=vllm_ascend.patch.platform.patch_balance_schedule.BalanceScheduler balance=False recompute=False dyntra_lb=False batch_job=False profiling_chunk=False`；
  - Worker 每个 rank 两行，和 T51 时逐字相同：`worker device_timing=on tp_rank=0` 1 行、`off` 15 行（`tp_rank=1`…`15`），**没有** `runner_stages=`。
  - **数一下：** `scheduler=…BalanceScheduler` 1 行，`device_timing=on tp_rank=0` 1 行，`off` 15 行。
- **日志是追加写的时候，** 文件里同时有升级前（T51 时期）的旧行：D 上会多出没有 `runner_stages=` 的 `device_timing=on tp_rank=0` 行，P、D 上各种行的次数也会多出升级前的那一份。**旧行不算**：只看带 `runner_stages=` 的行数够不够，和这次拉起以后新出现的行。

**这一步不对就停（不能只记下来）：** D 机器上带 `runner_stages=` 的 `tp_rank=0` 行数少于 D 实例数（16），或者列的不是那 6 个 stage，或者 D 的 EngineCore 不是上面那个调度器，就停下，按第九部分回滚，带回这几行。原因：「是不是 D」取不到配置时会悄悄退回 T51 的行为，不报错，这两行是唯一当场看得出的地方。

| 看到 | 是什么 |
|---|---|
| D 机器上带 `runner_stages=` 的行数少于 D 实例数（16） | 有的 D 被新代码判成了「不是 D」，或者三件补丁的版本不配套。只有不带 `runner_stages=` 的旧行、带它的行数够 16 的，不是失败 |
| D 的那一行是 `runner_stages=forward,sampling source=env` | 有人设了 `VLLM_PD_DEVICE_STAGES=none`：从 D 的启动脚本里去掉它，照第 11 步停整个集群、第 15 步拉起，再看这一步 |
| P 的那一行有 `runner_stages=` | P 被判成了 D：停下，带回 |
| `device_timing` 那一行还没出现 | 还没执行过真实请求，等有请求以后再看 |

#### 第 19 步　监控机：过滤器自检（拉起 5 分钟以后）

**机器：** 监控机。

**命令：**

```bash
cd <总目录>/vllm-pd-grafana-panel2/test-glm53 && bash -c '. bin/lib.sh && load_env >/dev/null && curl -s -u "vllm_reader:$CH_READER_PASSWORD" "http://localhost:$CLICKHOUSE_HTTP_PORT/" --data-binary @-' <<'SQL'
SELECT instance, sum(m) AS cand_maps, sum(f) AS forward_empty, sum(sa) AS sampling_empty, sum(e) AS no_execution_id FROM (SELECT instance, argMax(cand_maps, ts) AS m, argMax(acc_fallback['forward'], ts) AS f, argMax(acc_fallback['sampling'], ts) AS sa, argMax(exec_missing, ts) AS e FROM vllm_pd.filter_selfcheck WHERE ts > now() - INTERVAL 10 MINUTE GROUP BY instance, pid) GROUP BY instance ORDER BY instance FORMAT PrettyCompactMonoBlock
SQL
```

**应当看到：** 每个实例一行。D 的实例 `cand_maps` 大于 0；`no_execution_id` 是 0。`forward_empty`、`sampling_empty` 是没有设备时间、看板上留空的步数，按第 20、22 步判。

**不对时：** D 的实例 `cand_maps` 是 0，或者 `no_execution_id` 大于 0：这次升级失败。

**带回：** 这条查询的输出。

#### 第 20 步　监控机：留空比例第一次（拉起 10 分钟以后）

**机器：** 监控机。只记录、不判。整个集群一起拉起，所以所有实例都算这一次（命令里已经填成全部实例）。

**命令：**

```bash
cd <总目录>/vllm-pd-grafana-panel2/test-glm53 && bash -c '. bin/lib.sh && load_env >/dev/null && curl -s -u "vllm_reader:$CH_READER_PASSWORD" "http://localhost:$CLICKHOUSE_HTTP_PORT/" --data-binary @-' <<'SQL'
WITH now64(3, 'UTC') AS t10, cur AS (SELECT DISTINCT instance, pid FROM vllm_pd.filter_selfcheck WHERE ts > now() - INTERVAL 10 MINUTE), done AS (SELECT DISTINCT instance, request_id FROM vllm_pd.phase_line_cur WHERE proc = 'api' AND api_end_ts < now() - INTERVAL 1 MINUTE), ew AS (SELECT instance, uniqExact(request_id) AS ne FROM vllm_pd.phase_line_cur WHERE proc = 'api' AND api_end_ts >= t10 AND api_end_ts < now() - INTERVAL 1 MINUTE GROUP BY instance), emp AS (SELECT instance, arraySort(groupUniqArray(pid)) AS pl, length(pl) AS np, sum(f) AS fe, sum(sa) AS se FROM (SELECT instance, pid, argMax(acc_fallback['forward'], ts) AS f, argMax(acc_fallback['sampling'], ts) AS sa FROM vllm_pd.filter_selfcheck WHERE ts > now() - INTERVAL 10 MINUTE GROUP BY instance, pid) GROUP BY instance) SELECT i.instance AS instance, match(i.instance, '^(.+)$') AS this_batch, toString(t10) AS t10_utc, dateDiff('minute', t10, now()) AS window_min, ew.ne AS ended_win, emp.np AS pids, emp.pl AS pid_list, emp.fe AS forward_empty, emp.se AS sampling_empty, x.a_tot AS alloc_total, x.f_tot AS forward_total, x.s_tot AS sampling_total, x.a_win AS alloc_win, x.f_win AS forward_win, x.s_win AS sampling_win, if(x.a_win = 0, NULL, round(100 * (1 - x.f_win / x.a_win), 2)) AS forward_empty_pct, if(x.a_win = 0, NULL, round(100 * (1 - x.s_win / x.a_win), 2)) AS sampling_empty_pct FROM (SELECT DISTINCT instance FROM done) AS i LEFT JOIN (SELECT instance, sum(al) AS a_tot, sum(fw) AS f_tot, sum(sm) AS s_tot, sum(al - al0) AS a_win, sum(fw - fw0) AS f_win, sum(sm - sm0) AS s_win FROM (SELECT l.instance AS instance, l.request_id AS request_id, l.part AS part, toUInt32OrZero(splitByChar('-', l.part)[2]) AS pid, ifNull(max(l.acc__forward__steps), 0) AS fw, ifNull(maxIf(l.acc__forward__steps, l.ts < t10), 0) AS fw0, ifNull(max(l.acc__sampling__steps), 0) AS sm, ifNull(maxIf(l.acc__sampling__steps, l.ts < t10), 0) AS sm0, ifNull(max(l.acc__alloc__steps), 0) AS al, ifNull(maxIf(l.acc__alloc__steps, l.ts < t10), 0) AS al0 FROM vllm_pd.phase_line_cur AS l WHERE l.part != '' AND (l.instance, l.request_id) IN (SELECT instance, request_id FROM done) GROUP BY l.instance, l.request_id, l.part) WHERE (instance, pid) IN (SELECT instance, pid FROM cur) GROUP BY instance) AS x ON i.instance = x.instance LEFT JOIN emp ON i.instance = emp.instance LEFT JOIN ew ON i.instance = ew.instance ORDER BY this_batch DESC, instance SETTINGS join_use_nulls = 0 FORMAT PrettyCompactMonoBlock
SQL
```

**应当看到：** 每个实例一行，列含义见第 22 步。第一次执行时 `ended_win`、`alloc_win`、`forward_win`、`sampling_win` 是 0，两个比例为空（`ᴺᵁᴸᴸ`）。

**带回：** 整份输出。`t10_utc` 照抄下来（带毫秒），第 22 步要用；每个实例的 `pid_list` 也照抄。

#### 第 21 步　磁盘起点（拉起 5 分钟以后，和第 19 步同时）

**机器：** 每台机器上，宿主机里。

**命令：**

```bash
N=<节点目录>/logs
{ date +%s; du -sb "$N" | cut -f1; for f in <vLLM 日志文件>; do echo "$f $(stat -c %s "$f")"; done; } > ~/t51-glm53-disk.t0 && echo "起点已记下：$(wc -l < ~/t51-glm53-disk.t0) 行"
```

**应当看到：** `起点已记下：<n> 行`，n 是 vLLM 日志文件的个数加 2（D 机器 18，P 机器 3）。

#### 第 22 步　监控机和机器：留空比例第二次、磁盘第二次（拉起约 35 分钟以后）

**机器：** 留空比例在监控机上；磁盘在每台机器上，宿主机里。

**命令：** 留空比例，把 `<第一次的 t10_utc>` 换成第 20 步打印的那个时刻：

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
- `pid_list`、`pids`：最近 10 分钟写过自检行的进程号和个数，和第 20 步相同。
- `forward_empty`、`sampling_empty`：留空次数（进程启动以来累计）。
- `alloc_total`、`forward_total`、`sampling_total`：已经结束 1 分钟以上的请求按步累加的步数；`alloc_win`、`forward_win`、`sampling_win`：同样的步数，只算 `t10_utc` 以后。
- `forward_empty_pct` = 1 − `forward_win` ÷ `alloc_win`，`sampling_empty_pct` 同理。比例可能是零点几的负数，按 0 看。留空次数只看增没增加，不能和比例相除。
- 这里只判 forward、sampling；新加的几段有没有留空，在第 31 步那条查询里看。
- **留空不是因为捕图。** 服务期间不捕图：所有图在启动时捕完，服务中再要捕图会直接报错（vllm-ascend `acl_graph.py` 第 184 行）。T51 手册第 18 步那句「服务中第一次遇到某个桶的那一步在捕获」不对；留空另有原因：设备事件在 4 步之内没读到、同一步量了两次、这一步本来就没跑这个 stage。这个比例也不按第一步、第二步起分开看：设备事件记在计算流上，和这一步走哪种图模式无关。

磁盘一行，例如 `30 分钟：节点目录每天 <x> GB；vLLM 日志 [BatchPutTiming] 每天 <y> GB，[PD_DIAG] 0 行`。这 30 分钟里有 vLLM 日志文件被轮转了的，vLLM 日志一项会偏小：重新记起点再量。

**怎么判（forward、sampling 各自判，每个实例单独判）：** 先看下面三条，再看留空比例表。

| 看到 | 结果 |
|---|---|
| `window_min` 明显大于 35 | `t10_utc` 抄错了：重抄第 20 步的 `t10_utc`，再执行；这一次不判 |
| 两次的 `pid_list` 不同 | 进程重启过，两次不可比：D 的实例算升级失败；P 的带回。带回两次输出 |
| 比例为空，`ended_win` 大于 0，`forward_win` 或 `sampling_win` 大于 0 | 调度器侧断了：升级失败（D、P 都算），带回 |
| 比例为空，`ended_win` 大于 0，三个 `*_win` 都是 0 | 不直接判失败，带回 |
| 比例为空，`ended_win` 是 0 | 没判成，不算通过，带回 |

| 比例 | 留空次数（第 22 步比第 20 步） | 结果 |
|---|---|---|
| 超过 1% | 增加了 | D：升级失败。P：带回，不算失败 |
| 超过 1% | 没有增加 | 不判失败：两侧步数对不上，带回 |
| 不超过 1% | 增加了 | 通过。记下增加了多少，带回 |
| 不超过 1% | 没有增加 | 通过 |

磁盘按「判失败的条件」里的磁盘项判。

**带回：** 留空比例两次的输出，磁盘那一行（每台机器）。

#### 第 23 步　面板二（这时还是 v3.1-extra）：升级安全检查

**机器：** 监控机。拉起以后至少 3 分钟再做；`vars.sh` 是 T51 时面板手册第 1 步写好的。

**命令：**

```bash
. ~/vllm-pd-test-glm53/vars.sh && cd "$D" && bin/panel2_cluster.sh check "$C"
```

**应当看到：** 每项 `OK`，其中 `库：表和视图都在，视图读的是 pd-diag-v3.1-extra-glm53-2026-11-19c`、`过滤器版本：<n> 个实例 <n> 个进程都是 pd-diag-v3.1-extra-glm53-2026-11-19c@<8 位>`，最后一行 `RESULT: OK`。整个集群一起拉起，没有没重启的实例，所以要 `RESULT: OK`。

**不对时：** 不是 `RESULT: OK`：带回全部输出，先不换面板（第五部分）。

#### 第 24 步　面板一：TTFT、TPOT 中位数（观测项，不判失败）

**机器：** 面板一 Decode 看板「TPOT P50 / P90 / P99 / Max」面板的菜单里点 Explore，把查询换成下面的语句，查询类型选 Instant。「停机前」把时间范围的结束时刻设成第 11 步记下的停的时刻；「拉起后」设成拉起以后 40 分钟。

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

#### 第 25 步　升级的结论

按「判失败的条件」逐条看。都通过：做第五部分。有一条失败：停下；两个容器都按第九部分回滚到 T51，整个集群再拉起一次；带回检查输出、vLLM 启动日志和第 19 步那条查询的输出。

## 第五部分　换面板二（v3.2-extra）

这一部分把 test-glm53 的面板二从 v3.1-extra（段定义 19c）升到 v3.2-extra：D 侧多 9 段、3 行对照、分布表多一列「有值 / 请求」。需要时回滚到 v3.1-extra。glm53-1 和其他集群不动。

**放在手册的哪里：** 整个集群换完补丁、拉起并核对以后（第四部分做完），首批核对之前。
- 先打补丁、后换面板：打补丁和拉起以后的核对用的还是 19c 的面板，判法不变。
- 首批核对和第一步开销分析，读的都是新段定义里的列和不上看板的规则，所以在第 28 步以后做。等 1–2 分钟，只看换面板以后开始的请求：换面板那一刻在途的请求，开头几步的部分和不全。
- 升级后的 TPOT 3 遍，放在换面板之前或之后都可以，换面板不重启 vLLM。

**预计输出的来源：** 实验室 t56p 用交付的包（sha256 见交付说明）走了一遍：升级、核对、回滚。尖括号 `<…>` 是每次不同的部分。
- 实验室照内网的形状：glm53-1 是 mock 模式；test-glm53 是 vllm 模式、两台机器，替身回放带 T56 补丁字段的样本。
- 实验室和现场不一样的地方，第 29 步分开写了：
  - 替身的样本里 forward、sampling 只有主机时长，这两段在实验室为空；
  - 替身按固定样本循环回放，换面板那一刻在途的样本请求，要等这一轮放完才恢复；
  - 实验室没有开 proxy 采集和 Mooncake 计时采集，P、D 的 lb_proxy 和 P 后台 put 下 3 段为空。

### 文中的占位符

| 占位符 | 含义 | 在哪里查 |
|---|---|---|
| `<用户目录>` | 放部署包和更新脚本的目录 | 更新脚本「现场路径」一段的 `USER_DIR` |
| `<总目录>` | 面板目录的上级目录 | 更新脚本的 `ROOT` |
| `<更新脚本>` | 更新脚本的文件名 | 路径已填好的是 `vllm_pd_update_pack2_v32extra_filled.py`；用 `vllm_pd_update_pack2_v32extra.py` 时，先改好它「现场路径」一段 |

### 要做的几步

| 步 | 在哪里 | 做什么 | 实验室用时 |
|---|---|---|---|
| 26 | 监控机 | 放文件，核对 sha256 | 几秒（不含拷文件） |
| 27 | 监控机 | 更新脚本：先 `check`，再 `run` | `check` 约 20 秒，`run` 约 30 秒 |
| 28 | 监控机 | 看板分层：`arrange_boards.py apply` | 几秒 |
| 29 | 监控机 | 发一批请求以后核对：升级安全检查、coverage | 约 5 分钟（含等待） |
| 30 | 监控机 | 需要时，回滚到 v3.1-extra | 约 30 秒 |

### 升级以后看板上会看到什么

- 看板换到新的 config_id，从空开始，只显示升级以后的请求。升级以前的行留在库里，回滚以后重新显示。
- D 的「到第一个 token」多 6 段：
  - start_load_kv 下：receive_queue、prepare_read（meta_fetch + prepare）；
  - start_load_kv 之后：kv_ready_wait；
  - 第一步：prepare_inputs、lm_head、draft（设备侧，第一步）。
- D 的「第二步起」多 3 段：prepare_inputs、lm_head、draft（设备侧，第二步起）。
- 对照行：queued_time (vLLM)、prefill_time (vLLM)、decode_time (vLLM)。对照行不计入 D 合计，占比留空。
- start_load_kv 是接收线程的墙钟包络。它下面 4 个子段按各任务取最大值，互有重叠，所以子段之和不一定等于它。
- 两张分布表最后一列「有值 / 请求」：这一段有值的请求数 / 这一行可能有值的请求数。
- P 侧不变：P 节点也收到新的 segments.json，P 上新段为空，原来的 27 段不变。

### 对同机其他服务的影响

只动 test-glm53 自己的面板二。

| 机器 | 服务 | 影响 |
|---|---|---|
| 监控机 | glm53-1 的面板二和面板一 | 不碰 |
| 监控机 | Grafana | 不重启，只重新导入 test-glm53 的看板 |
| 监控机 | test-glm53 的 ClickHouse | 不重启，数据不删：表加新段的列，两个视图（`phase_line_cur`、`request_json`）按新段定义重建 |
| 监控机 | test-glm53 的采集器 | 重启一次，几秒没有新数据 |
| 监控机 | 磁盘 | 新包和离线包解到 `<用户目录>/vllm-pd-panel2-update-v3.2-extra/pack/`，约 390 MB，升级后不自动删；备份写到执行账号家目录的 `vllm-pd-update-backup/<时间>/` |
| test-glm53 的节点 | vLLM | 不重启：过滤器程序和日志配置不变，过滤器约 2 秒内自己重读新的 segments.json |

#### 第 26 步　放文件，核对 sha256

**机器：** 监控机。

**命令：** 先建两个目录。`vllm-pd-grafana-panel2.tar.gz` 和 `.sha256` 放进第一个目录，`<更新脚本>` 放进第二个，再核对。

```bash
mkdir -p <用户目录>/vllm-pd-panel2-update-v3.2-extra <用户目录>/vllm-pd-update-v3.2-extra
```

```bash
cd <用户目录>/vllm-pd-panel2-update-v3.2-extra && sha256sum -c vllm-pd-grafana-panel2.tar.gz.sha256
```

**预计输出：** `vllm-pd-grafana-panel2.tar.gz: OK`。

**马上会看到：** 只多了两个目录和三个文件，别的都没动。离线包仍用 `<用户目录>/vllm-pd-panel2-update-1112/` 里那一份，不用再拷。

#### 第 27 步　对 test-glm53 执行更新脚本

**机器：** 监控机。

**命令：** 先核对（只读），再升级。

```bash
cd <用户目录>/vllm-pd-update-v3.2-extra && python3 <更新脚本> check --cluster test-glm53
```

```bash
cd <用户目录>/vllm-pd-update-v3.2-extra && python3 <更新脚本> run --cluster test-glm53
```

**`check` 的预计输出**（摘要一段）：

```
vllm_pd_update_pack2_v32extra.py check，面板二 v3.2-extra，集群 test-glm53，成功
check：通过（面板二 v3.1-extra，vllm 模式；WARN 1 项）
迁移预览：RESULT: OK
INFO 段定义换成新包的 profiles/glm53/segments.json（config_id pd-diag-v3.1-extra-glm53-2026-11-19c → pd-diag-v3.2-extra-glm53-2026-11-20a）
INFO segments.json 会更新（pd-diag-v3.1-extra-glm53-2026-11-19c@addc903c → pd-diag-v3.2-extra-glm53-2026-11-20a@04d0c3ea），过滤器自己重读；config_id 变了：…
INFO 过滤器程序和日志配置（pd_diag_logfilter.py、pd_diag_logging.json）和现在的相同，不用重启 vLLM
升级安全检查（升级前）：RESULT: WARN（1 项）
WARN 版本和文件：1 个文件和 SHA256SUMS 不同，升级会换成包里的：segments.json
INFO 节点：<机器> 找到 vLLM 容器 <容器名>（容器里有 vllm 进程）；不对时填 deploy.env 的 NODE_VLLM_CONTAINERS
INFO 最近 15 分钟 <n> 个请求
升级前比对：RESULT: 不变 2 项，有变化 3 项，比不了 0 项
INFO 看板：有变化：…
INFO 表和视图：有变化：视图 phase_line_cur 的定义；视图 request_json 的定义
INFO 采集器：有变化：…
日志：<家目录>/vllm-pd-update-logs/pack2-v32extra-test-glm53-check-<时间>.log
```

- 「INFO 节点」每台机器一行。
- 唯一的 WARN 是预期的：段定义要换成新包里的。T51 换包时也是这一条。

**`run` 的预计输出**（摘要一段）：

```
vllm_pd_update_pack2_v32extra.py run，面板二 v3.2-extra，集群 test-glm53，成功
预检：通过（更新前面板二是 v3.1-extra，vllm 模式）
备份：<家目录>/vllm-pd-update-backup/<时间>
迁移：deploy.env 里没有要写的
数据：保留，不删（vllm_pd 的 7 张表，共 <行数> 行）
已更新：表和视图：视图 phase_line_cur 的定义；视图 request_json 的定义；加列 phase_line.start_load_kv_ms、start_load_kv__receive_queue_ms、…
已更新：采集器：central.yaml 第 <n> 行（…）
跳过（内容不变）：数据源
已更新：看板：…
跳过（内容不变）：Prometheus（不写文件、不重新加载）
节点文件：segments.json 会更新（…19c@addc903c → …20a@04d0c3ea），过滤器自己重读；config_id 变了：…；过滤器程序和日志配置（…）和现在的相同，不用重启 vLLM
看板：config_id pd-diag-v3.1-extra-glm53-2026-11-19c → pd-diag-v3.2-extra-glm53-2026-11-20a，只显示升级以后的请求；升级以前的行留在库里，rollback 以后重新显示
段定义：profiles/glm53/segments.json；proxy 采集 <开或关>，Mooncake 计时采集 <开或关>（打开见操作说明 B 第 4 步）
不变的项：全部通过（9 项）
几分钟后看看板；可选的核对（不影响升级）：cd <总目录>/vllm-pd-grafana-panel2/test-glm53 && bin/panel2_cluster.sh check test-glm53
回到 v3.1-extra：python3 <用户目录>/vllm-pd-update-v3.2-extra/<更新脚本> rollback <家目录>/vllm-pd-update-backup/<时间> --cluster test-glm53
日志：<家目录>/vllm-pd-update-logs/pack2-v32extra-test-glm53-run-<时间>.log
```

- deploy.env 原样保留：更新脚本只在缺少迁移项时追加行，这次没有要追加的。所以用户加过的 Mooncake 3 行（`MOONCAKE_LOG_COLLECT`、`MOONCAKE_LOG_FILES`、`MOONCAKE_TID_FALLBACK`）和 proxy 4 行（`PROXY_COLLECT`、`PROXY_MACHINE`、`PROXY_LOG_DIR`、`PULL_RSYNC_FLAGS`）不用重新加，摘要里 proxy、Mooncake 两个开关显示的就是 deploy.env 里的值。没做 proxy、Mooncake 的，deploy.env 里没有这几行，换面板也不用加。回滚把 deploy.env 写回更新前的样子，这几行也在。
- **记下「回到 v3.1-extra」那一行**，回滚时用它。

**马上会看到：**

- test-glm53 的看板空了，从这时起只显示新请求；
- 节点上 `conf` 目录里的 `segments.json` 换成新的，另两个节点文件不变；vLLM 没有重启；
- 实验室：升级后 1 分钟内，新旧两个 config_id 的行都在写（重读那一刻还没写完的行带旧版本），之后只有新的。

**`run` 中途停下时：** 照摘要里的那一行处理，处理后重跑同一条 `run`。

#### 第 28 步　看板分层

**机器：** 监控机。

**命令：** 和面板手册第 16 步相同。

```bash
. ~/vllm-pd-test-glm53/vars.sh && python3 "$T45/arrange_boards.py" apply --env-from "$ENV2"
```

**预计输出：** 最后一行是 `RESULT: OK（改了 <n> 项；回退：python3 arrange_boards.py undo <备份目录> --env-from <同一个 deploy.env>）`，前一行是 `OK   再看一遍：没有要改的`。

**马上会看到：** test-glm53 的看板回到它的子文件夹里，其他集群的看板不动。

#### 第 29 步　发一批请求以后核对

**机器：** 监控机。

**命令：** 换面板 2 分钟以后，发一批请求（手册里 TPOT 那一组请求就可以），再执行：

```bash
cd <总目录>/vllm-pd-grafana-panel2/test-glm53 && cat VERSION && bin/panel2_cluster.sh check test-glm53
```

```bash
cd <总目录>/vllm-pd-grafana-panel2/test-glm53 && bin/coverage.sh
```

**`check` 的预计输出：** 第一行 `v3.2-extra`，最后一行 `RESULT: OK`。

**`coverage.sh` 现场应当看到的：**

```
段定义 pd-diag-v3.2-extra-glm53-2026-11-20a：最近 30 分钟里 <n> 个请求（最多取 200 个）
  …（每段一行：有值的请求数 / 请求数）
  D start_load_kv/receive_queue      <k> / <n>（只在从 P 读 KV 的请求上有值；各任务中的最大值，…）
  D start_load_kv/prepare_read       <k> / <n>（只在从 P 读 KV 的请求上有值；取对端信息、组读表各自在各任务中的最大值之和）
  D kv_ready_wait                    <k> / <n>（只在走过异步加载的请求上有值；从调度器得知 KV 到齐算起，比字节到齐晚一步；…）
  …
对照行（不是段，不计入合计）：
  D kv_wait_vllm                     <n> / <n>（queued_time (vLLM)）
  D first_step_vllm                  <n> / <n>（prefill_time (vLLM)）
  D 第二步起 rest_vllm                <n> / <n>（decode_time (vLLM)）
56 段里 <m> 段有值；没有来源、按设计为空的 2 段：P api_gateway、P start_load_kv/wait_for_transfer
<最后一行，见下表>
```

`<m>` 和最后一行，看 proxy、Mooncake 做没做。第 27 步摘要「段定义」那一行写着 proxy 采集、Mooncake 计时采集是开还是关。

| proxy 采集 | Mooncake 计时行 | `<m>` | 最后一行 |
|---|---|---|---|
| 开 | 有 | 54 | `RESULT: OK` |
| 关 | 有 | 52 | `RESULT: FAIL（有来源但这些请求里都为空的 2 段：P lb_proxy、D lb_proxy）` |
| 开 | 没有 | 51 | `RESULT: FAIL（有来源但这些请求里都为空的 3 段：P 后台 wait_for_save/put/submit、P 后台 wait_for_save/put/wait_for_transfer、P 后台 wait_for_save/put/finalize）` |
| 关 | 没有 | 49 | `RESULT: FAIL（有来源但这些请求里都为空的 5 段：P lb_proxy、P 后台 wait_for_save/put/submit、P 后台 wait_for_save/put/wait_for_transfer、P 后台 wait_for_save/put/finalize、D lb_proxy）` |

- 「Mooncake 计时行有」：换了 Mooncake 库，并且摘要里 Mooncake 计时采集是「开」。没换库时日志里没有计时行，采集开着也按「没有」看。
- coverage 不看 deploy.env 的采集开关：没开采集的段也算「有来源但为空」，所以后三行是 `RESULT: FAIL`。**最后一行列出的段和表里这一行相同时，不算失败**，接着往下做；列出了表里没有的段，停下，把 coverage 的全部输出发回。
- 这几行出自端到端测试（rc1 的真实日志加上 T56 补丁的记录），D 全部换了新补丁：第一行是 proxy 行、Mooncake 计时行都在的结果；后三行是在同一个库里删掉 proxy 行、Mooncake 计时行（或其中一种）以后跑的 coverage。实验室的替身得不出这几行。
- **实验室的样子**（替身造成的，现场不会这样）：
  - `56 段里 42 段有值`，`RESULT: FAIL`；D 的 9 个新段和 3 行对照都有值。
  - 为空的 12 段：P、D 的 lb_proxy（实验室没开 proxy 采集）；P、D 的 forward、sampling 和 D 第二步起的这两段（替身样本只有主机时长）；P 的 wait_for_put（替身样本没有）；put 下 3 段（实验室没开 Mooncake 计时采集）。
  - 有一轮走查时，P 的 schedule/lookup 也为空：替身循环回放固定的样本，换面板那一刻在途的样本请求，要等这一轮放完才恢复。现场只影响换面板那一刻在途的请求。

**不对时：**
- 新段都为空：D 的补丁没有换上，或者节点上的 segments.json 不是新的。看 `bin/panel2_cluster.sh check` 的节点一项，把 coverage 的全部输出发回。
- 只有 D kv_ready_wait 为空：它只在走过异步加载的请求上有值；从 P 读 KV 的请求都应当有，都为空时停下，发回输出。

**马上会看到：** 只读，什么都不改。

#### 第 30 步　回滚到 v3.1-extra（需要时才做）

**机器：** 监控机。补丁的回滚按打补丁手册做，和面板的回滚互不牵连，先做哪一个都可以。

**命令：** 用第 27 步摘要里「回到 v3.1-extra」那一行，再做一次看板分层。

```bash
cd <用户目录>/vllm-pd-update-v3.2-extra && python3 <更新脚本> rollback <「回到 v3.1-extra」那一行的备份目录> --cluster test-glm53
```

```bash
. ~/vllm-pd-test-glm53/vars.sh && python3 "$T45/arrange_boards.py" apply --env-from "$ENV2"
```

**`rollback` 的预计输出**（摘要一段）：

```
vllm_pd_update_pack2_v32extra.py rollback <备份目录>，面板二 v3.2-extra，集群 test-glm53，成功
恢复 <备份目录>：和更新前一致，校验全部通过
OK   拉取程序先停下：已停止
INFO 数据：保留，不删（vllm_pd 的 7 张表，共 <行数> 行）
OK   节点文件：更新前的三个文件已下发（apply_segments.sh 里的 bin/nodes.sh push），segments.json 是 pd-diag-v3.1-extra-glm53-2026-11-19c@addc903c
INFO 看板读回 v3.1-extra 的 config_id pd-diag-v3.1-extra-glm53-2026-11-19c：换版以前的行重新显示；更新以后写的行留在库里，不显示
OK   拉取程序用回 v3.1-extra 启动：已启动
INFO vLLM 不用重启：过滤器程序和日志配置（pd_diag_logfilter.py、pd_diag_logging.json）回滚前后相同，segments.json 过滤器自己重读
日志：<家目录>/vllm-pd-update-logs/pack2-v32extra-test-glm53-rollback-<时间>.log
```

**回滚停在「拉取程序停不下来」时：** 这时还没有写回任何东西。照提示在 test-glm53 的集群目录里执行 `bin/puller.sh stop`，或者结束提示里列出的进程，再重跑同一条 `rollback`。

**马上会看到：**
- 看板回到 19c 的段，换版以前的请求重新显示；更新以后写的行留在库里，不显示；
- 节点上的 segments.json 换回 19c 的，过滤器约 2 秒内自己重读，vLLM 不用重启；
- 实验室：回滚以后半分钟内，新 config_id 的行停止增加，19c 的行接着写；`cat VERSION` 是 `v3.1-extra`，`check` 是 `RESULT: OK`。

**回滚以后再升级：** 重新做第 27、28 步。

## 第六部分　首批数据核对（换面板以后，一条只读查询）

#### 第 31 步　首批数据核对

**机器：** 监控机。只读。

**什么时候做：**
- 第五部分换面板完成以后，先等 1–2 分钟，让换面板那一刻正在处理的请求结束；
- 再让平常的流量或第 1 步那组请求跑几分钟，有一些从 P 读过 KV 的请求结束了，再执行；
- 换面板以后 15 天以内都可以查（`diag_event` 保留 15 天，其余表 30 天）。

**为什么只看换面板以后开始的请求：** 过滤器读到新的段定义时，会清掉正在处理的请求的累加值，这些请求开头几步的部分和不全。

**命令：** `<D 实例>` 填一个 D 实例的名字，例如 `D0`。`<开始时刻>` 填换面板完成的时刻加 2 分钟，UTC，写成 `年-月-日 时:分:秒.毫秒`，例如 `2026-11-21 02:30:00.000`。

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
| `kv_ready_wait_present` 是 `FAIL` | D 的 EngineCore 没被判成 D：新加的段不写，服务不受影响。不回滚，带回这一行和第 18 步的启动行 |
| 其余某一项 `FAIL` | 服务不受影响，不回滚：带回整份输出，由我们看 |
| 报 `UNKNOWN_IDENTIFIER` 之类的错 | 面板还不是 v3.2-extra（第五部分没做完），或者表里还没有这些列：先核第五部分最后一步 |

**带回：** 整份输出，`<D 实例>` 和 `<开始时刻>`。

## 第七部分　升级后：参考请求 3 遍，比 TPOT

#### 第 32 步　参考请求再跑 3 遍，按判据比

**机器：** 和第 1 步相同。第 31 步做完以后再做。

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
| `SLOWER` | 在 D 的启动脚本里，T51 加的两行 `export` 下面加一行 `export VLLM_PD_DEVICE_STAGES=lm_head,draft`（P 不加）。然后照第 11 步停整个集群，照第 15 步拉起；拉起以后照第 16、18 步核对：D 的 `tp_rank=0` 那一行应当是 `runner_stages=forward,sampling,lm_head,draft source=env`，P 的行不变。之后看板上 `prepare_inputs` 为空（「有值 / 请求」一列显示 `0 / n`），`bookkeeping` 和 `step_device_span_ns` 也不写。改了启动脚本以后，再做检测时 `baseline_token` 会和第 4 步的不同，这是预期的，下次升级重新记基线。只改环境变量，不重出补丁包。把这一行和 3+3 个数带回 |

**带回：** 升级后 3 遍的那三行、这条命令的输出，以及用的是甲还是乙。

## 第八部分　第一步开销分析（必做，只读）

#### 第 33 步　第一步开销分析

**用途：** 看 D 上第一步比第二步起多出的时间（约 48 ms）落在哪里。这次只给分析能力：只读，不上看板，不加 L1/L2/L3；找到原因以后，在 T57 里解决。只用面板二已有的列，和 5 条不上看板的规则（`step_device_span`，`graph_full`、`graph_piecewise`、`graph_none`，`bookkeeping`）。

**什么时候做：** 第 31 步通过以后，跑过一批请求再做。
- 最近 `<分钟>` 分钟里，要有这个 D 实例结束的请求。
- 只算换面板以后开始的请求，所以时间窗不要跨过换面板那一刻。

**机器：** 监控机。

**命令：** `<D 实例>` 填一个 D 实例的名字，写法和看板上的实例名相同，例如 `D0`；`<分钟>` 填时间窗的分钟数，例如 `30`。两处都在命令里改。查询是 t41 的 `第一步开销分析.sql`，原样放在命令里，用的是和第 31 步相同的只读访问，不用另外拷文件。

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
        pi_f + fw_f + lh_f + sp_f + dr_f + bk_f AS six_f,
        pi_n - (pi_f > 0) AS pi_k,
        ifNull(pi_k > 0 AND pi_k = fw_n - (fw_f > 0) AND pi_k = lh_n - (lh_f > 0) AND pi_k = sp_n - (sp_f > 0)
            AND pi_k = dr_n - (dr_f > 0) AND pi_k = bk_n - (bk_f > 0) AND pi_k = sd_n - (sd_f > 0), 0) AS rest_ok,
        ifNull(gf_n, 0) + ifNull(gp_n, 0) + ifNull(gn_n, 0) AS steps_all
    FROM per
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
    round(quantileExact(0.5)(prefill_ms), 3) AS "prefill_time / decode_time (vLLM)"
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
    round(quantileExactIf(0.5)(pi_r + fw_r + lh_r + sp_r + dr_r + bk_r, rest_ok) / 1e6, 3),
    round(quantileExactIf(0.5)(sd_r, rest_ok) / 1e6, 3),
    round(quantileExactIf(0.5)(sd_r - (pi_r + fw_r + lh_r + sp_r + dr_r + bk_r), rest_ok) / 1e6, 3),
    toUInt64(sum(ifNull(gf_n, 0) - (gf_f > 0))),
    toUInt64(sum(ifNull(gp_n, 0) - (gp_f > 0))),
    toUInt64(sum(ifNull(gn_n, 0) - (gn_f > 0))),
    round(quantileExactIf(0.5)(decode_ms / (steps_all - 1), steps_all > 1), 3)
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

几点说明：
- 6 段和 step_device_span 都是设备时长，只在 D 的 `tp_rank` 0 有。
- bookkeeping：同步模式下含从设备拷回主机，异步模式下不含。glm53-test 的 D 是异步模式。
- 某一段在某个请求的第一步没有设备值时，这个请求不进这一段第一步的中位数。
- 可比请求数为 0 时，6 段之和、step_device_span、空隙为空。原因是有的步缺设备值，各段的平均不是同一批步，相减没有意义；这时先看首批核对里设备值缺失计数那一项。

**应当看到：**
- 两行都有。请求数 > 0，可比请求数 > 0：首批核对通过时，设备值缺失接近 0。
- 6 段和 step_device_span 都是正数，空隙 ≥ 0。
- 两行的 FULL、PIECEWISE、NONE 步数加起来不为 0；第二步起几乎都是 FULL。
- prefill_time (vLLM) 比第二步起每步的 decode_time 大，大约多出 48 ms（现场讨论材料里的数，这一步就是要找它）。
- 两行都没有时：时间窗里没有这个实例的请求，或者面板还没换成 v3.2-extra。先查 `cat VERSION` 和首批核对，再发回输出。

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

先停整个集群（照第 11 步）。回滚只恢复三件补丁到 T51 版，不碰 Mooncake（换过的库留着；Mooncake 自己的回滚只按 T51 手册第 35 步）。面板的回滚单独做，见第五部分的回滚一节；补丁和面板谁先回滚都可以。

#### 第 34 步　把监控机上的备份送回容器

**机器：** 监控机执行第一条；那台机器上执行第二条；容器里执行第三条。

**命令：**

```bash
ssh <SSH 账号>@<机器地址> mkdir -p /tmp/t56-<机器>-<时间戳> && scp <用户目录>/vllm-pd-patch-glm53/backup/t56-<机器>-<时间戳>/* <SSH 账号>@<机器地址>:/tmp/t56-<机器>-<时间戳>/
```

```bash
docker cp /tmp/t56-<机器>-<时间戳>/. <容器名>:/tmp/t56-<机器>-<时间戳>
```

```bash
cd /tmp/t56-<机器>-<时间戳> && LC_ALL=C sha256sum -c SHA256SUMS && tar -tf vllm.tar | wc -l && tar -tf vllm-ascend.tar | wc -l
```

**应当看到：** 最后一条打印 `vllm.tar: OK`、`vllm-ascend.tar: OK`、`25`、`12`。

#### 第 35 步　重做补丁包解开和找目录

**机器：** 容器里。容器窗口还开着、变量都在的，跳过这一步。

**命令：**

```bash
mkdir -p /tmp/t56-kit && tar -xf /tmp/t56-glm53-patch-kit.tar -C /tmp/t56-kit && cd /tmp/t56-kit && LC_ALL=C sha256sum -c SHA256SUMS | grep -v ': OK$'; sha256sum /tmp/t56-kit/detect/t51_glm53_check.py | cut -c1-16
```

```bash
VLLM_PKG=$(python3 -c "import importlib.util as u; print(u.find_spec('vllm').submodule_search_locations[0])")
ASCEND_PKG=$(python3 -c "import importlib.util as u; print(u.find_spec('vllm_ascend').submodule_search_locations[0])")
VLLM_ROOT=$(dirname "$VLLM_PKG"); ASCEND_ROOT=$(dirname "$ASCEND_PKG"); F="$ASCEND_PKG/distributed/kv_transfer/kv_p2p/mooncake_connector.py"
echo "PYTHONPATH=[${PYTHONPATH:-}]"; echo "$VLLM_ROOT"; echo "$ASCEND_ROOT"; echo "$F"
```

**应当看到：** 和第 3、5 步相同。

#### 第 36 步　恢复三件

**机器：** 容器里。

**命令：**

```bash
BK=/tmp/t56-<机器>-<时间戳>; K=/tmp/t56-kit/patches
tar -xf "$BK/vllm.tar" -C "$VLLM_ROOT" && (cd "$VLLM_ROOT" && LC_ALL=C sha256sum -c $K/vllm/SHA256SUMS.t51 | grep -v ': OK$')
tar -xf "$BK/vllm-ascend.tar" -C "$ASCEND_ROOT" && (cd "$ASCEND_ROOT" && LC_ALL=C sha256sum -c $K/vllm-ascend/SHA256SUMS.t51 | grep -v ': OK$')
sha256sum "$F" | cut -c1-16
```

**应当看到：** 前两行没有输出（25 个、11 个文件都回到 T51 版）；最后一行是 T51 时记下的 `<S>` 的前 16 位（T51 重放出来的 `mooncake_connector.py`）。

**不对时：** 有 `FAILED`，或者最后一行不对：停下，带回。

#### 第 37 步　整体拉起，核对

照第 15 步拉起整个集群，再照第 16、17 步核对。这时检测应当报 `verdict vllm=ok(t51) ascend=ok(t51,site_local=<n>) p2d=ok(t51)`，`baseline=same`；启动行和 T51 时相同（D 的 Worker 那一行没有 `runner_stages=`）。

## 判失败的条件

对全部实例判。下表任何一条出现，这次升级算失败：停下，整个集群按第 25 步处理（第九部分回滚到 T51）。**停下、带回、不回滚**的另列在表后。

| 步骤 | 失败条件 |
|---|---|
| 14 | 导入核对不是 `True`，或者报错 |
| 15 | 有实例的 `/health` 不是 `200`，或者起不来 |
| 16 | 命令行和停机前不同；有 vLLM 进程没有设 `VLLM_LOGGING_CONFIG_PATH` |
| 16 | vLLM 日志里有 `ImportError`，或者 `Traceback` 后面几行里有 `pd_diagnostics`、`pd_device_timing`、`worker_base`、`ascend_store`、`mooncake_connector`、`recompute_scheduler` |
| 17 | `verdict` 的 vllm、ascend、p2d 三项有一项不是 `ok(t56…)`，包括只升了一半（例如 `ascend=ok(t51…)`） |
| 17 | 出现 `BASELINE_DIFF`；`run` 行是 `logcfg=unset`；`site_patches` 和第 4 步不同 |
| 18 | D 的 EngineCore 不是 `AsyncRecomputeScheduler`；D 机器上带 `runner_stages=` 的 `tp_rank=0` 行数少于 D 实例数，或者列的不是那 6 个 stage（`forward,sampling,prepare_inputs,lm_head,draft,bookkeeping`；日志追加时升级前的旧行不算）；P 的 Worker 那一行有 `runner_stages=`；`model_runner` 不是 `NPUModelRunner`；`BalanceScheduler` 带 `balance=True` |
| 18 | D 上 `tp_rank=0` 的 `device_timing` 是 `off` |
| 19 | 有 D 实例 `cand_maps` 是 0，或者 `no_execution_id` 大于 0 |
| 22 | D 的 `forward_empty_pct` 超过 1% 而且 `forward_empty` 比第 20 步大，或者 sampling 的这两项同样如此 |
| 22 | D 两次的 `pid_list` 不同 |
| 22 | 第二次比例为空、`ended_win` 大于 0、`forward_win` 或 `sampling_win` 大于 0 |
| 22（磁盘） | 节点目录每天增长 × `NODE_LOG_KEEP_HOURS` ÷ 24，再加这台机器的实例数 × 1 GiB（D 机器 16 GiB，P 机器 1 GiB），超过第 10 步 `df` 里节点目录所在文件系统 `Avail` 的一半 |
| 21、22（磁盘） | vLLM 日志里有 `[PD_DIAG]` 行（一出现就处理，不等满 30 分钟） |
| 23 | 旧面板的 `check` 不是 `RESULT: OK` |

**停下、带回、不回滚：**
- 第 4 步前三项不是 `ok(t51…)`、运行模式不对（这时什么都还没改）；
- 第 6、7、9 步不对（同上）；
- 第五部分换面板不对（按那一部分的回滚一节处理面板，补丁不动）；
- 第 31 步有 `FAIL`：这些是数据核对，服务不受影响；
- 第 32 步是 `SLOWER`：按那一步改环境变量。

**不算失败、带回的：**
- 第 4、17 步的 `mooncake=BAD(other)`（两步相同时）；
- 第 22 步 P 的留空比例；
- 第 24 步拉起后 TTFT、TPOT 大 10% 以上（开销的判据看第 32 步）。

## 带回清单

| 步骤 | 带回 | 每份 |
|---|---|---|
| 1 | 3 遍各自的三行，用的是甲还是乙 | 一份 |
| 3 | `df -hT /tmp` 那一行 | 每个容器 |
| 4 | 检测脚本的全部输出；`baseline_token` | 每个容器 |
| 6 | 不对时的输出 | 每个容器 |
| 7 | 三条命令的完整输出（含 `span_diff` 那一行） | P、D 各一份 |
| 8 | `<备份目录>` | 每个容器 |
| 9 | 两条命令的完整输出和 `<S56>` | P、D 各一份；`<S56>` 每个容器 |
| 10 | `cmdline.before`、`df` | 每个容器；`df` 每台机器 |
| 14 | 导入核对那一行 | 一份 |
| 16 | 命令行比较结果、日志配置那一行 | 每个容器 |
| 17 | 检测脚本的全部输出 | 每个容器 |
| 18 | 启动行（`PD_DIAG_STARTUP` 那几行） | 每台机器 |
| 19 | 自检查询的输出 | 一份 |
| 20、22 | 留空比例两次的输出 | 一份，含 D、P 每个实例 |
| 22 | 磁盘那一行 | 每台机器 |
| 23 | 旧面板 `check` 的全部输出 | 一份 |
| 24 | 停机前、拉起后的 D 的 TTFT、TPOT，P 的 TTFT | 一份 |
| 第五部分 | 那一部分各步写的带回 | 一份 |
| 31 | 整份输出，`<D 实例>`、`<开始时刻>` | 一份 |
| 32 | 3 遍各自的三行、判据那一行 | 一份 |
| 33 | 那一步写的带回 | 一份 |
| 25 | 失败时：检查输出、vLLM 启动日志、第 19 步的输出 | 失败时 |

## 附录 A　原因和背景

**A.1 这次加了什么（记录格式约定 1.4.0）。** 新内容只在 D 上写；P 上写出的东西和 T51 逐字段相同。记录的 `version` 仍是 2，已有阶段的边界不动。看板上的段名用英文代码名，和原来的段同一种写法；对照行是 `queued_time (vLLM)`、`prefill_time (vLLM)`、`decode_time (vLLM)`。各段在中文里是这个意思：

- `start_load_kv`：D 从交给连接器到这个请求的 KV 全部到齐的墙钟时长（`kv_load_envelope_ns`）。下面的子段按各任务的最大值取，互有重叠，加起来不等于它。
  - `receive_queue`：接收线程里，任务等着被处理的时间（各任务最大值）；
  - `prepare_read`：取对端信息加组读表（两个最大值之和，是上界：两个最大值可能来自不同的任务）；
  - `wait_for_transfer`：传输本身。
- `kv_ready_wait`：从调度器得知 KV 到齐，到这个请求被排上那一次调度开始。它比字节到齐晚一步：字节到齐在接收线程里，调度器要到下一次处理时才知道。
- 第一步、第二步起的 `prepare_inputs`（输入准备）、`lm_head`、`draft`（MTP 草稿：5 次草稿前向和草稿的 lm_head）：取设备时长，和 `forward`、`sampling` 在同一条计算流上，可以相加。
- `bookkeeping`（采样后的状态更新）照记，不上看板。同步模式下它包含从设备拷回主机；异步模式下不包含，拷回在这一段之外。现场 D 是异步。

**A.2 「是不是 D」怎么判。** 三件补丁都只用 vLLM 补丁里的一个函数，它返回 vllm-ascend 自己的条件：顶层 `kv_role` 是 `kv_consumer`，并且开着 recompute。这个条件和 vllm-ascend 启动时把调度器换成 Recompute 一族的条件相同，所以 D 跑着 `AsyncRecomputeScheduler`，就说明条件成立。取不到配置时函数返回「不是 D」，不报错，D 会悄悄退回 T51 的行为；所以第 18 步是不对就停的一步，第 31 步还要核从 P 读过 KV 的请求都有 `kv_ready_wait`。

**A.3 服务期间不捕图。** 所有图在启动时捕完；服务中要捕图会直接报错（vllm-ascend `acl_graph.py` 第 184 行）。所以新加的几段不会遇到「这一步在捕图」，留空另有原因（第 22 步）。T51 手册第 18 步那句「服务中第一次遇到某个桶的那一步在捕获」不对，T51 的判据本身不受影响。

**A.4 每步的图模式。** D 的第一步每个请求只有 1 个 token，第二步起有 6 个（MTP k=5），两种步的图模式可能不同。记录里有这一步目标模型用的图模式（`cudagraph_runtime_mode`：FULL、PIECEWISE、NONE），第 31 步分第一步、第二步起列出来。它只代表目标模型：MTP 草稿模型自己选图模式。

**A.5 开销判据。** 新加的 4 段每步多记 4 对设备事件。判据看升级前后同一组请求的 TPOT：升级后中位数 − 升级前中位数 > max(2% × 升级前中位数, 波动) 时，把 `VLLM_PD_DEVICE_STAGES` 改成 `lm_head,draft`。2% 是阈值，不是预测。

## 附录 B　已知问题

1. **第 31 步的两个 1%：** 「设备值缺失 ≤ 记录数的 1%」「第二步起 `NONE` ≤ 1%」是阈值，不是预测，和 T51 留空比例的阈值一致，不是从现场数据得出的。缺失次数按 6 段合计，和记录数（每步两条记录）比，是近似。
2. **`prepare_read` 是上界：** 取对端信息、组读表两项各取各任务的最大值再相加，两个最大值可能不在同一个任务上。平时取对端信息只在第一次连某个 P 端口时不是 0，影响小。
3. **首批核对只看换面板以后开始的请求：** 过滤器读到新的段定义时，会清掉正在处理的请求的累加值。
4. **T51 加的两行 `export` 不动：** 回滚到 T51 以后，面板二的日志配置照旧生效。
5. **补丁包里有 `t51` 字样的文件：** `patches/p2d/t51/` 下是 T51 版的 `replay_p2d.py` 和补丁，字节不变，只用在第 7 步证明现场文件是 T51 重放出来的；检测脚本仍叫 `t51_glm53_check.py`（版本 glm53-7）。

## 附录 C　没在 NPU 上、在现场测过的部分

- **新加的几段在 NPU 上的取值。** 实验室没有 NPU：设备事件、图模式、`step_device_span_ns` 都只在 CPU 上用模拟的计时核过记录格式和开关（vllm-ascend 补丁的测试），没有在卡上量过。
- **开销。** 每步多 4 对设备事件对 TPOT 的影响没有量过，所以有第一、七部分。
- **`vllm bench serve` 在现场的 P 容器里。** 只在实验室的 CPU vLLM v0.30.0 上跑过（直接发给 vLLM，没有 proxy），现场一遍的用时、它在 P 容器里会不会碰卡，都没有测过。
- **P 写出的东西和 T51 逐字段相同。** 这一点是实验室里用记录等价测试核的，这次是第一次在现场 P 上装新代码。
- **首批核对的查询。** 在实验室用面板二 v3.2-extra 的表结构和合成数据测过（正常的全部 PASS，故意做错的 5 种各抓到一次）；真实记录上的结果要看现场。
- **整体停、整体拉起。** 和 T51 的分批不同；拉起以后的判法按全部实例。实验室走查用的是替身进程，不是真实的 vLLM。

## 附录 D　文件和 sha256

| 文件 | sha256 |
|---|---|
| 补丁包 `t56-glm53-patch-kit.tar` | `0d2bc9a86a395046…`（全长见 `.sha256` 文件） |
| 检测脚本 `detect/t51_glm53_check.py`（glm53-7） | `588e6d94d278a876…` |
| vLLM 完整补丁 `patches/vllm/t56-vllm-v0.30.0-pd-diag.patch` | `7959cbb716ff1018b176c973e62cade1968897281dcb0f9019d55db79618e61c` |
| vLLM 增量补丁 `patches/vllm/t56-vllm-from-t51.patch` | `5d7578685c525b307ea926bdd63718f04d4eddfe8dc4faf2057be5c5d4a65db9` |
| `patches/vllm/SHA256SUMS.after` | `9a8a55078c6293655ea5647e91d750fcdb67b542cd55c51656da8069047b53bc` |
| vllm-ascend 合并补丁 `patches/vllm-ascend/t56-vllm-ascend-d4ad49796-pd-diag.patch` | `d0fbb82bce0fe4538de32b721cd17cf1678fdadf0fe9a7d95d60685257574869` |
| `patches/vllm-ascend/SHA256SUMS.after` | `0b46fa152612d152191d92c30ba4315b247e70e327330525408d6831e942b3c4` |
| `patches/p2d/replay_p2d.py`（T56） | `9b12baa6b38c8ff7d697c450ad80a9e0b48889f384dcdbddc33e04c2ee5e553e` |
| `patches/p2d/t51/replay_p2d.py`（T51，证明用） | `02304c8f76429780635870d17bac73558a2ecfafaadd744428c0ec93f457d4b3` |
| `patches/p2d/field_check/span_diff.py` | `40ebf068e0df5f08257681c8bfba65ac6910e6424d5711db5702478abdd33cbe` |
| 面板包 `面板二v3.2-extra/vllm-pd-grafana-panel2.tar.gz`（v3.2-extra，第五部分） | `d77528d42bed97961c74f88f77d57c428813ea9475ae8ef262d57357ef9b4b1f` |
| 更新脚本 `面板二v3.2-extra/vllm_pd_update_pack2_v32extra.py`（第五部分） | `f6bcb544f4048e1eac2178c300afa020ad483e9db41043063cfe67aae53bdc01` |
| 第一步开销分析的查询 `第一步开销分析.sql`（t41，第 33 步原样放在命令里） | `5650295c6484c5e7d64388395211c8555028fe3ee38a2557c962eb5675fa6a93` |
