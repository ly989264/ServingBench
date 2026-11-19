# glm5.3 测试集群打补丁操作手册

> 本手册取代补丁包（`t51-glm53-patch-kit.tar`，sha256 `d21cc319…`）里的操作说明 A 和 Mooncake 增量包（`t51-glm53-mooncake-kit.tar`，sha256 `6d70978c…`）里的第 11 步说明。两个包的内容不变，本手册放在包外面。

<!-- 占位符版开始 -->
> **本手册有两版。** 这一版是占位符版：路径写成尖括号里的说明，不含任何内网信息。已填路径版把监控机上的个人路径填好，只放在私有的 Caching 仓库。
<!-- 占位符版结束 -->

## 第一页

**做什么。** 在 glm5.3 测试集群（面板二里的集群 `test-glm53`）的 P、D 容器里，打 vLLM、vllm-ascend、p2d 三件补丁，接上面板二，重启一次，分三批上线。最后一部分可选：把 Mooncake 的 `libmooncake_store.so` 换成带计时的替换库。不新建镜像，不新建容器，改动写在容器的可写层里。

**做完是什么样子。**
- 每个容器的检测脚本报 `verdict vllm=ok ascend=ok(site_local=<n>) p2d=ok mooncake=not_replaced`；做了可选的 Mooncake 部分以后，最后一项是 `mooncake=ok`。
- 面板二 `test-glm53` 的看板：47 段里 42 段有值；做了 Mooncake 部分以后 45 段。

**总顺序：** 面板手册第 1–15 步 → 本手册 → 面板手册第 16–22 步。面板手册指《面板二：删除 test、安装 test-glm53 操作手册》，步骤号以它为准。

**要先准备好的现场值：**

<!-- 占位符表开始 -->
| 占位符 | 含义 | 在哪里查 |
|---|---|---|
| `<用户目录>` | 监控机上放部署包和更新脚本的目录 | 包二 v3 更新脚本「现场路径」一段的 `USER_DIR` |
| `<总目录>` | 面板目录的上级目录 | 同一个脚本的 `ROOT` |
<!-- 占位符表结束 -->
| `<SSH 账号>`、`<机器地址>` | 从监控机登录 glm5.3 机器用的账号和机器 | 面板手册第 2 步从 test 的 `deploy.env` 里记下的值 |
| `<机器>` | 这台机器的简称，只用在监控机上备份目录的名字里；同一台机器每次写法相同 | 自己定，例如清单里的机器序号 |
| `<容器名>` | 这台机器上跑 vLLM 的容器 | 在那台机器上 `docker ps` |
| `<节点目录>` | 面板二的过滤器在这台机器上写文件的目录（宿主机路径），下面有 `logs/<端口>/` | 沿用 test 的 `/data/pd-diag`，容器里的路径相同（面板手册第 3 步核对过） |
| `<启动脚本>` | 第 3 步检测时 `--start-script` 传的那个文件：P 是 P 那次检测用的启动文件（`start_p.sh`），D 是 D 那次检测用的启动文件（`start_d0.sh`）；一台机器一个，所有 rank 共用 | 第 3 步的命令 |
| `<vLLM 日志文件>` | 这台机器上各个 vLLM 实例的标准输出、标准错误重定向到的文件（宿主机路径），几个文件用空格隔开；D 机器有 16 个 | P 上就是面板手册里填进 `MOONCAKE_LOG_FILES` 的文件；D 上是同一类文件 |
| `<启动文件目录>` | 容器里 `run_dp_template.sh`、`launch_online_dp.py`、`start_p.sh`（D 上 `start_d0.sh`）、`mooncake.json`、`start_mooncake.sh` 所在的目录；不在同一目录的，按各自的路径写，缺哪个就不传哪个 | 那台机器上拉起 vLLM 的方式 |
| `<容器里的 vLLM 日志文件>` | `<vLLM 日志文件>` 在容器里看到的路径；容器里看不到就不传 `--startup-log` | 同上 |
| `<这一批的实例>` | 面板一里这一批实例的名字，用竖线连起来，例如 `D0\|D1\|D2` | 清单里 D 一侧第 n 项（从 0 数）是 `Dn` |
| 端口 | D 的容器是 `$(seq 9100 9115)`（16 个实例），P 的容器是 `9900`。现场不同时按同样的格式改：连续的写 `$(seq <第一个> <最后一个>)`，不连续的用空格隔开 | 面板手册第 13 步写进清单的端口 |

做的过程中还要记下几个值（第 3 步的 `baseline_token` 和 `site_patches`、第 6 步的 `<备份目录>`、`<时间戳>`、第 8 步的 `<S>`），在那一步里写明。

**对服务的影响：**

| 步骤 | 对运行中的 vLLM |
|---|---|
| 1–9 | 不影响：只往容器里写文件、在 `<启动脚本>` 里加两行 `export`，运行中的 vLLM 还在用内存里的旧代码 |
| 10–12 | 每批重启一次这一批容器里的 vLLM 实例；重启本身和平时一样 |
| 13–21 | 只读核对。重启以后多了持续写入：过滤器往 `<节点目录>` 写；做了 Mooncake 部分以后，还往 vLLM 日志写 `[BatchPutTiming]` 行 |
| 22–26 | 回滚，只在失败时做：恢复文件以后再重启一次 |
| 27–35（可选，Mooncake） | 换库不影响运行中的进程；之后分批重启 |

## 通用约定

- 所有容器里的命令都以 root 执行：先在那台机器上 `docker exec -it <容器名> bash` 进入容器。
- 任何一步的结果和「应当看到」不一样时，停下，把那一步的输出带回来，不要继续。
- `LC_ALL=C sha256sum -c … | grep -v ': OK$'` 这类核对命令，没有输出就是全部通过，这时整条管道的退出码是 1。不要把它们放进带 `set -e` 的脚本，按输出判断。
- `docker restart` 以后补丁还在；`docker rm` 后重建容器、或者换镜像，补丁就没了，要重新做一遍。
- 不设 `MC_LOG_DIR`：打补丁、重启时都不要加这个环境变量。
- 三件补丁必须一起装、一起回滚：vllm-ascend 主体和 p2d 用到的名字只有 vLLM 补丁里有，只装其中一件时 Worker 启动就会 `ImportError`。

## 正文

### 一、放进容器，检测，核对，备份（每个 P、D 容器各做一次）

#### 第 1 步　在监控机上核对补丁包，拷到那台机器

**机器：** 监控机。补丁包 `t51-glm53-patch-kit.tar` 和它的 `t51-glm53-patch-kit.tar.sha256` 放在 `<用户目录>/vllm-pd-patch-glm53/` 下。

**命令：**

{{B:sha256sum -c t51-glm53-patch-kit.tar.sha256}}

{{B:scp <用户目录>/vllm-pd-patch-glm53/t51-glm53-patch-kit.tar}}

**应当看到：** 第一条打印 `t51-glm53-patch-kit.tar: OK`。

**不对时：** 第一条不是 `OK`：补丁包损坏，重新取一份，不往下做。

#### 第 2 步　拷进容器，解开，核对

**机器：** 先在那台机器上执行第一条，再进容器执行后两条。

**命令：**

{{B:docker cp /tmp/t51-glm53-patch-kit.tar}}

{{B:mkdir -p /tmp/t51-kit && tar -xf /tmp/t51-glm53-patch-kit.tar}}

{{B:df -hT /tmp | tail -n 1}}

**应当看到：** 第二条没有任何输出。第三条一行，第二列是文件系统类型，一般是 `overlay`；是 `tmpfs` 时，`/tmp` 下的文件在容器重启以后就没了，第 6 步会自动把备份放到 `/root` 下。

**不对时：** 第二条有输出：补丁包在拷贝中损坏，重新拷贝。

**带回：** `df` 那一行。那台机器上的 `/tmp/t51-glm53-patch-kit.tar` 留着，回滚时要用。

#### 第 3 步　打补丁前的检测

**机器：** 容器里。检测脚本 `/tmp/t51-kit/detect/t51_glm53_check.py`（sha256 `f9e6073f…`）只读，几秒内结束，只往 `/tmp` 写一个明细文件；按 `nice 19`、IO 空闲级运行，读取量上限 1 GiB，耗时上限 300 秒。5 个启动文件里缺哪个就不传哪个；没有 vLLM 日志文件就不传 `--startup-log`。

**命令：** P 的容器：

{{B:--start-script <启动文件目录>/start_p.sh --mooncake-json <启动文件目录>/mooncake.json --start-mooncake <启动文件目录>/start_mooncake.sh --startup-log <容器里的 vLLM 日志文件>
}}

D 的容器：

{{B:--start-script <启动文件目录>/start_d0.sh --mooncake-json <启动文件目录>/mooncake.json --start-mooncake <启动文件目录>/start_mooncake.sh --startup-log <容器里的 vLLM 日志文件>
}}

带回以后删掉明细文件（路径照抄 `detail=` 那一行）：

{{B:rm -f <detail= 那一行的路径>}}

**应当看到：** 不超过 20 行，退出码 0。其中：

- `vllm=… state=v0.30.0 …` 和 `ascend=… state=modified …`，两行末尾都是 `from=site cands=1`。
- `site_patches patch/__init__.py=<16 位> patch/platform/__init__.py=<16 位> patch/platform/patch_kv_load_failure_reason.py=d7c764bb425993c0 patch/platform/patch_prefill_length_validation.py=c36392a6338bc2f1`；D 上最后一项是 `absent`。前两个记下来，是第 5 步的预期值。
- `inst[run …]`：每组配置相同的实例一行。
- `run` 行里是 `logcfg=unset`：面板二的日志配置要等重启才生效。
- `verdict vllm=unpatched ascend=unpatched(site_local=<n>) p2d=unpatched mooncake=not_replaced`；`strict` 行末是 `p2d=not_replayed`。同样是 `unpatched`，在这一步是预期值，在第 13 步是失败。
- 最后一行 `fingerprint=… baseline_token=<一串>`：把 `baseline_token=` 后面的整串记下来，每个容器各一串，第 13 步要用。

**不对时：**

| 看到 | 怎么办 |
|---|---|
| `site_patches` 的后两个和上面不同 | 停下，带回输出 |
| `mooncake=BAD(other)` | 带回这一行；第 1–26 步照常做，可选的 Mooncake 部分先不做 |
| 只打印一行 `BLOCKED`（自查认出输出里有 IP、主机名之类） | 带回这一行，不往下做 |
| 只打印一行 `REFUSED` | `-I -S -B` 少了，照原文重新执行 |

**带回：** 终端打印的全部行（指纹 `fingerprint=` 相同的容器只带一份），每个容器的 `baseline_token`。

#### 第 4 步　找到安装目录

**机器：** 容器里，接着第 3 步。只查找位置，不导入 vLLM。

**命令：**

{{B:VLLM_PKG=$(python3 -c}}

**应当看到：** 四行。第一行是 `PYTHONPATH=[]`；第二、三行是同一个 `site-packages` 目录；第四行是它下面的 `vllm_ascend/distributed/kv_transfer/kv_p2p/mooncake_connector.py`。第 3 步的 `vllm=`、`ascend=` 两行都是 `from=site cands=1`。

**不对时：** 第一行方括号里不为空，或者第 3 步不是 `from=site cands=1`：停下，带回这四行和第 3 步那两行。

#### 第 5 步　核对要改的文件和现场的本地补丁

**机器：** 容器里，接着第 4 步。

**命令：**

{{B:K=/tmp/t51-kit/patches
(cd "$VLLM_ROOT" && LC_ALL=C sha256sum -c $K/vllm/SHA256SUMS.before}}

再看现场两个本地补丁是不是调用原函数（只读；D 上只有第一个文件）：

{{B:grep -nE "original|_orig|kwargs"}}

**应当看到：** 第一段的前两条没有任何输出；第三条是 `2c4acb3eeb1ced7d`；最后四行是文件名和 sha256 前 16 位：

| 文件 | P | D |
|---|---|---|
| `__init__.py` | 第 3 步 `site_patches` 的 `patch/__init__.py` | 同左（D 自己那一次的值） |
| `platform/__init__.py` | 第 3 步 `site_patches` 的 `patch/platform/__init__.py` | 同左（D 自己那一次的值） |
| `platform/patch_kv_load_failure_reason.py` | `d7c764bb425993c0` | `d7c764bb425993c0` |
| `platform/patch_prefill_length_validation.py` | `c36392a6338bc2f1` | `platform/patch_prefill_length_validation.py 没有` |

`grep` 列出调用原函数（`original`、`_orig` 一类名字）的行；`patch_prefill_length_validation.py` 里 `_add_request` 的包装把 `**kwargs` 原样传给原函数。

**不对时：**

| 看到 | 怎么办 |
|---|---|
| `FAILED`、「已存在」，或者后五行和上表不同 | 停下，带回输出：文件已经被改过、已经装过补丁，或者现场的本地补丁变了 |
| `grep` 显示 `**kwargs` 没有传下去 | 照常往下做（服务不受影响，P 的 add_request 段会为空），带回这条命令的输出 |

#### 第 6 步　备份，拷回监控机（必做）

**机器：** 容器里执行第一条；然后在那台机器上执行第二条；最后在监控机上执行后两条。

**命令：**

{{B:BKBASE=/tmp; [ "$(stat -f -c %T /tmp)" = tmpfs ] && BKBASE=/root
BK=$BKBASE/t51-glm53-backup-}}

{{B:docker cp <容器名>:<备份目录> /tmp/}}

{{B:mkdir -p <用户目录>/vllm-pd-patch-glm53/backup && scp -r <SSH 账号>@<机器地址>:/tmp/t51-glm53-backup-}}

{{B:backup/<机器>-<时间戳> && LC_ALL=C sha256sum -c SHA256SUMS && tar -tf vllm.tar}}

**应当看到：** 第一条打印备份目录名 `/tmp/t51-glm53-backup-<时间戳>`（`/tmp` 是 tmpfs 时在 `/root` 下），然后 `23`、`10` 和两个 tar 的 sha256 前 16 位。最后一条打印 `vllm.tar: OK`、`vllm-ascend.tar: OK`、`23`、`10`。把备份目录名记作 `<备份目录>`，末尾的 `<年月日>-<时分秒>` 记作 `<时间戳>`。

**不对时：** 数字不是 `23`、`10`，或者监控机上有 `FAILED`：重新拷；监控机上核对通过以前不做第 7 步。现场的 `mooncake_connector.py` 是本地修改版，补丁包里没有这一份，丢了无法重建。

**带回：** `<备份目录>`。备份合计约 2 MB，容器里、那台机器的 `/tmp` 下、监控机上各一份。

### 二、替换源文件（每个容器）

#### 第 7 步　替换 vLLM 和 vllm-ascend 主体

**机器：** 容器里，接着第 6 步。

**命令：** 先确认第 4、6 步设的变量都在：

{{B:"${VLLM_PKG:?}" "${ASCEND_PKG:?}"}}

{{B:cp -R $K/vllm/files/vllm/. "$VLLM_PKG"/}}

{{B:cp -R $K/vllm-ascend/files/vllm_ascend/. "$ASCEND_PKG"/}}

**应当看到：** 第一条打印 `变量都在：<备份目录>`；后两条没有任何输出（25 个、11 个文件都和交付的一致）。

**不对时：**

| 看到 | 怎么办 |
|---|---|
| 第一条报 `parameter null or not set` | 换过 shell：按第 4 步重新找目录，再设 `K=/tmp/t51-kit/patches` 和 `BK=<备份目录>`（不要重做备份），然后再执行 |
| 后两条有 `FAILED` | 停下，按第 22–26 步回滚 |

#### 第 8 步　在现场的 `mooncake_connector.py` 上重放 p2d

**机器：** 容器里，接着第 7 步。P、D 都要做。`replay_p2d.py` 的 sha256 是 `02304c8f76429780635870d17bac73558a2ecfafaadd744428c0ec93f457d4b3`。

**命令：** 只检查、不写文件：

{{B:"$F" --check; echo "exit=$?"}}

补充核对 X1–X6：

{{B:extra_checks.extra_line}}

写出打好补丁的文件：

{{B:--out /tmp/mooncake_connector.p2d.py; echo "exit=$?"}}

替换并核对：

{{B:cp /tmp/mooncake_connector.p2d.py "$F"}}

**应当看到：**

- 第一条最后两行是 `replay_p2d: check ok: 28 of 28 changes located, result checks passed, nothing written` 和 `exit=0`；28 行定位结果里只有 C25 是 `alternative_2`，其余都是 `primary`；`N01` 一行只是提示。
- 第二条：`p2d_extra X1=1 X2=1/1 X3=2 X4=4/1 X5=1 X6=ok`，没有一项带 `!`。
- 第三条最后两行：`replay_p2d: written out=/tmp/mooncake_connector.p2d.py sha256=<S> changes=28/28 py_compile=ok same_as_replacement=False` 和 `exit=0`。记下 `<S>`：所有 P、D 容器的 `<S>` 必须相同。`same_as_replacement=False` 是正常的。
- 第四条：`<S>` 的前 16 位。

**不对时：**

| 看到 | 怎么办 |
|---|---|
| 别的编号也用了备选锚点，或者 C25 回到 `primary` | 停下，带回输出：现场文件和小核对时不同 |
| `p2d_extra` 不是上面那一行 | 停下，带回两条命令的完整输出 |
| `<S>` 和前面做过的容器不同 | 停下，带回 |
| 退出码 1 | 参数错，读不了输入，或输入不是合法的 Python：核对路径和文件 |
| 退出码 2、3、4 | 不写文件，带回完整输出（2：某处改动的主锚点和备选锚点都没有恰好命中一行；3：结构判据不满足，`STOP Fxx`；4：重放结果的检查没通过） |
| 退出码 5 | 写出的文件 `py_compile` 不通过：不要用 `/tmp` 下的文件，带回输出 |
| 退出码 6 | 输入已经打过补丁：不要重复打，核对是不是已经替换过 |

退出码不为 0 时现场文件没有被动过。不要手工修改 `replay_p2d.py` 或现场文件去绕过检查。

**带回：** 三条 `replay_p2d.py`、`p2d_extra` 命令的完整输出和 `<S>`（P、D 各一份）。

### 三、接上面板二（每个容器）

#### 第 9 步　在启动脚本里加两行 export

{{T41}}

### 四、重启，分三批上线

**分三批：** 第 1 批是一台 D 机器的容器（这台机器上的全部 D 实例），第 2 批是其余 D 机器的容器，第 3 批是 P 机器的容器。测试集群只有一台 D 机器时，第 2 批是空的，第 1 批通过以后直接做第 3 批。每批的容器都做完第 1–9 步再重启。每批做第 10–21 步，这一批的检查都通过了，再做下一批。第 1、2 批做完、P 还没打补丁时，看板上只有 D 一侧的段，这是预期的。

#### 第 10 步　重启以前：记下命令行和磁盘

**机器：** 第一条在容器里（停实例之前）；第二条在这一批的每台机器上，宿主机里。`BK` 填 `<备份目录>`。

**命令：**

{{B:cmdline.before"; cat "$BK/cmdline.before"}}

{{B:df -hT <节点目录> <vLLM 日志文件> | uniq}}

**应当看到：** 第一条每种命令行一行，前面的数是用这一行的进程个数；D 的容器里每个实例一种（端口各不相同），P 的容器里一种。第二条：表头一行，然后节点目录、vLLM 日志所在的文件系统各一行（两者在同一个文件系统上时只有一行）。

**带回：** 两条的输出（`df` 每台机器一份）。再记下停实例的时刻（第 20 步用）。

#### 第 11 步　停实例，第 1 批的那一个容器做导入核对，再拉起

**机器：** 容器里。

**怎么停、怎么起：** 现场容器的 1 号进程是 `bash`，vLLM 由 `launch_online_dp.py` 拉起。按平时的方式停掉这个容器里的 vLLM 实例，再用原来的启动文件拉起（D 是 `start_d0.sh`，和 `run_dp_template.sh` 同一目录）。参数和停之前相同；环境变量只多第 9 步的两行 `export`，这两行这次必须生效。不要加 `MC_LOG_DIR`。

**命令：** 只在第 1 批的那一个容器里、实例停掉以后、重新拉起以前执行（其余容器、其余批次都不做）：

{{B:resource.getrusage}}

**应当看到：** 一行 `True <用时> s <峰值内存> MiB`。用时至少 5 秒、内存至少 500 MiB（实验室 6–8 秒、约 500 MiB；现场还要加载 torch_npu 和 CANN，没有测过）。这条命令导入 torch、torch_npu、vLLM 和这个模块用到的部分，导入完就退出，不加载模型，不开端口。

**不对时：** 不是 `True`，或者报错：不要拉起，按第 22–26 步回滚这个容器，带回这一行。

**带回：** 这一行（第 1 批）。

#### 第 12 步　重启以后，在容器里核对

**机器：** `/health`、命令行、日志配置三项在容器里；最后一条在那台机器上，宿主机里。

**命令：** 每个实例都起来了（D 的容器一条、P 的容器一条）：

{{B:for p in $(seq 9100 9115); do}}

{{B:for p in 9900; do}}

命令行和重启前相同（`BK` 填 `<备份目录>`）：

{{B:cmdline.after"; diff}}

面板二的日志配置在每个 vLLM 进程上都生效了：

{{B:VLLM_LOGGING_CONFIG_PATH=.*/pd_diag_logging}}

vLLM 日志里没有本套补丁引起的错误：

{{B:grep -nE -A12 "ImportError|Traceback" <vLLM 日志文件>}}

**应当看到：** 每个端口一行，都是 `200`（D 16 行，P 1 行）；`命令行和重启前相同`；只有一行 `<进程个数> 已设`；最后一条没有输出。vLLM 照常启动；`[PD_DIAG]` 行不再出现在 vLLM 的标准输出里，改写进 `<节点目录>/logs/<端口>/` 下的文件。

**不对时：** 有 `diff` 的输出：停下，不做这一批后面的检查，带回 `cmdline.before`、`cmdline.after`。有「没有设」的行：第 9 步的两行 `export` 没有生效（不在第一次启动 vLLM 或启动器的命令之前、写在了宿主机的 shell 里，或者没有重启），这一批失败；停下，带回 `<启动脚本>` 里这两行附近的内容和这条核对的输出。其余几条不对，这一批算失败（见「判失败的条件」）。

**带回：** 比较结果那一行和日志配置那一行。

#### 第 13 步　用检测脚本核对

**机器：** 容器里，第 12 步通过以后。每个打过补丁的容器跑一次。

**命令：** P 的容器：

{{B:start_p.sh --mooncake-json <启动文件目录>/mooncake.json --start-mooncake <启动文件目录>/start_mooncake.sh --startup-log <容器里的 vLLM 日志文件> --baseline}}

D 的容器：

{{B:start_d0.sh --mooncake-json <启动文件目录>/mooncake.json --start-mooncake <启动文件目录>/start_mooncake.sh --startup-log <容器里的 vLLM 日志文件> --baseline}}

带回以后照第 3 步删掉明细文件。

**应当看到：**

- `verdict vllm=ok ascend=ok(site_local=<n>) p2d=ok mooncake=not_replaced`。`vllm=ok` 不带 `site_local`；出现 `vllm=ok(site_local=n)` 时带回来。
- `vllm=` 行 `state=v0.30.0+port`，行末 `port=25/25`；`ascend=` 行仍是 `state=modified`，行末 `port=11/11`。
- `strict` 行末是 `p2d=ok(changes=28/28,missing=none,struct=ok,extra=same,replacement_file=0)`。
- `site_patches` 一行和第 3 步相同；`run` 行里是 `logcfg=set`；单独一行 `baseline=same`。
- `mooncake=BAD(other)` 不算这一批失败，带回这一行，可选的 Mooncake 部分先不做。

**不对时：** 见「判失败的条件」第 13 步那几行。带回全部输出。

#### 第 14 步　启动日志里的三行 `PD_DIAG_STARTUP`

**机器：** 那台机器上，宿主机里。`device_timing` 那一行要在第一个真实请求执行以后才有。

**命令：**

{{B:grep -h 'PD_DIAG_STARTUP' <vLLM 日志文件>}}

**应当看到：** 每种行一行，前面是出现的次数（只看内容）：

- EngineCore，每个实例一行。P：`scheduler=vllm_ascend.patch.platform.patch_balance_schedule.BalanceScheduler balance=False recompute=False dyntra_lb=False batch_job=False profiling_chunk=False`；D：`scheduler=vllm_ascend.core.recompute_scheduler.AsyncRecomputeScheduler balance=False recompute=True dyntra_lb=False batch_job=False profiling_chunk=False`。
- Worker，每个 rank 两行：`worker model_runner=vllm_ascend.worker.model_runner_v1.NPUModelRunner tp_rank=<n>`，第一个真实请求执行完以后 `worker device_timing=<on|off> tp_rank=<n>`。
- `device_timing` 只在 `tp_rank=0` 上是 `on`。P（TP16）每个实例 1 行 `on tp_rank=0`、15 行 `off`（`tp_rank=1`…`15`），属正常；D（TP1）每个实例 1 行 `on tp_rank=0`。

**不对时：**

| 看到 | 怎么办 |
|---|---|
| `scheduler`、`model_runner` 的类不是上面这几种，或者 `BalanceScheduler` 带 `balance=True` | 这一批失败，带回这几行 |
| D，`tp_rank=0` 是 `off` | 这一批失败，带回这几行 |
| D，`tp_rank=0` 是 `?` | 带回，不算失败，交给第 16、18 步的留空比例判 |
| P，`tp_rank=0` 是 `off` 或 `?` | 带回，不算失败；这时看板上 P 的 forward、sampling 是空的 |
| 一行都没有 | 对照第 13 步的 `vllm=` 行，带回 |
| `device_timing` 那一行还没出现 | 还没执行过真实请求，等有请求以后再看 |

#### 第 15 步　监控机：过滤器自检（重启 5 分钟以后）

**机器：** 监控机。

**命令：**

{{B:SELECT instance, sum(m) AS cand_maps}}

**应当看到：** 每个实例一行。这一批的 D 实例 `cand_maps` 大于 0；`no_execution_id` 是 0。`forward_empty`、`sampling_empty` 是没有设备时间、看板上留空的步数，按第 16、18 步判。

**不对时：** 这一批的 D 实例 `cand_maps` 是 0，或者 `no_execution_id` 大于 0：这一批失败。

**带回：** 这条查询的输出。

#### 第 16 步　监控机：留空比例第一次（重启 10 分钟以后）

**机器：** 监控机。`<这一批的实例>` 照第一页的写法填。只记录、不判。

**命令：**

{{B:WITH now64(3, 'UTC') AS t10}}

**应当看到：** 每个实例一行，列含义见第 18 步。第一次执行时 `ended_win`、`alloc_win`、`forward_win`、`sampling_win` 是 0，两个比例为空（`ᴺᵁᴸᴸ`）。

**带回：** 整份输出。`t10_utc` 照抄下来（带毫秒），第 18 步要用；每个实例的 `pid_list` 也照抄，第 18 步对照。

#### 第 17 步　磁盘起点（重启 5 分钟以后，和第 15 步同时）

**机器：** 这一批的每台机器上，宿主机里。

**命令：**

{{B:{ date +%s; du -sb "$N"}}

**应当看到：** `起点已记下：<n> 行`，n 是 vLLM 日志文件的个数加 2（D 机器 18，P 机器 3）。

记下起点以后，随时可以先执行一次第 18 步的磁盘命令看 `[PD_DIAG]`：不是 0 行就不等满 30 分钟，马上按第 22–26 步回滚这一批。

#### 第 18 步　监控机和机器：留空比例第二次、磁盘第二次（重启约 35 分钟以后）

**机器：** 留空比例在监控机上；磁盘在这一批的每台机器上，宿主机里。

**命令：** 留空比例，把 `<第一次的 t10_utc>` 换成第 16 步打印的那个时刻：

{{B:WITH toDateTime64('<第一次的 t10_utc>', 3, 'UTC') AS t10}}

磁盘：

{{B:N=<节点目录>/logs; S0=~/t51-glm53-disk.t0}}

**应当看到：** 留空比例每个实例一行：

- `this_batch`：`1` 是这一批的实例，`0` 是之前批次的实例，排在后面，只作参考。
- `window_min`：`t10_utc` 到现在的分钟数，应当在 25 左右。
- `ended_win`：`t10_utc` 以后、现在 1 分钟以前结束的请求数。
- `pid_list`、`pids`：最近 10 分钟写过自检行的进程号和个数，和第 16 步相同。
- `forward_empty`、`sampling_empty`：留空次数（进程启动以来累计）。
- `alloc_total`、`forward_total`、`sampling_total`：已经结束 1 分钟以上的请求按步累加的步数；`alloc_win`、`forward_win`、`sampling_win`：同样的步数，只算 `t10_utc` 以后。
- `forward_empty_pct` = 1 − `forward_win` ÷ `alloc_win`，`sampling_empty_pct` 同理。比例可能是零点几的负数，按 0 看。留空次数只看增没增加，不能和比例相除。

磁盘一行，例如 `30 分钟：节点目录每天 <x> GB；vLLM 日志 [BatchPutTiming] 每天 <y> GB，[PD_DIAG] 0 行`。这 30 分钟里有 vLLM 日志文件被轮转了的，vLLM 日志一项会偏小：重新记起点再量。

**怎么判（只判 `this_batch` 是 1 的行，forward、sampling 各自判，每个实例单独判）：** 先看下面三条，再看留空比例表。

| 看到 | 结果 |
|---|---|
| `window_min` 明显大于 35 | `t10_utc` 抄错了，或者抄成了上一批的：重抄这一批第 16 步的 `t10_utc`，再执行；这一次不判 |
| 两次的 `pid_list` 不同 | 进程重启过，两次不可比：D 这一批失败；P 带回。带回两次输出 |
| 比例为空，`ended_win` 大于 0，`forward_win` 或 `sampling_win` 大于 0 | 调度器侧断了：这一批失败（D、P 都算），带回 |
| 比例为空，`ended_win` 大于 0，三个 `*_win` 都是 0 | 不直接判失败，带回 |
| 比例为空，`ended_win` 是 0 | 没判成，不算通过，带回 |

| 比例 | 留空次数（第 18 步比第 16 步） | 结果 |
|---|---|---|
| 超过 1% | 增加了 | D：这一批失败。P：带回，不算失败 |
| 超过 1% | 没有增加 | 不判失败：两侧步数对不上，带回 |
| 不超过 1% | 增加了 | 通过。记下增加了多少，带回 |
| 不超过 1% | 没有增加 | 通过 |

磁盘按「判失败的条件」里的磁盘项判。

**带回：** 留空比例两次的输出（D、P 每个实例都要；第 1 批另要每个 D 实例的 `*_total`、`*_win` 和 `forward_empty`、`sampling_empty`），磁盘那一行（每台机器）。

#### 第 19 步　面板二：升级安全检查和看板上的段

{{PANEL}}

看板上应当有值的段：

| 做完第几批 | 有值的段（只有经过已打补丁实例的请求才有值） | 仍为空的段 |
|---|---|---|
| 第 1、2 批（D） | D 的 req_resolve、tokenizer、add_request、schedule 和 queue_time、alloc、other，start_load_kv 和 submit、wait_for_transfer，forward、sampling、detokenizer；D 第二步起的 schedule、alloc、other、forward、sampling、detokenizer | P 的全部段（P 还没打补丁）；P、D 的 lb_proxy（proxy 采集打开时有值） |
| 第 3 批（P） | 上面这些，加上 P 的 req_resolve、tokenizer、add_request、schedule 和 queue_time、hash、lookup、alloc、other，wait_for_put，forward、sampling、detokenizer，后台 wait_for_save 和它的 5 个子段；P 的 start_load_kv 和 submit 只在命中缓存池的请求上有值；做了 Mooncake 部分的，后台 wait_for_save/put 下的 3 段 | P 的 api_gateway、P 的 start_load_kv/wait_for_transfer（按设计为空）；没做 Mooncake 部分时，put 下的 3 段 |

第 3 批做完以后：不做 Mooncake 部分时，47 段里 42 段有值；做了以后 45 段。第 5 步的 `grep` 显示 `**kwargs` 没有传下去时，P 的 add_request 段为空。

**不对时：** 这一批的实例在 `过滤器版本` 里不是 `…-19c` 那一版；最后一批做完以后，`check` 不是 `RESULT: OK`，或者这一批的实例不是 `…-19c` 那一版：这一批失败，带回全部输出。最后一批以前，只涉及还没重启批次（P）实例的 `FAIL` 项（没在写、旧版本两种）：不算这一批失败，带回全部输出。重启 10 分钟以后、这一批的实例收到过请求，D 的 forward、sampling、start_load_kv/wait_for_transfer 仍然全为空（第 3 批看 P 的 forward、sampling、schedule）：这一批失败。

#### 第 20 步　面板一：TTFT、TPOT 中位数（观测项，不判失败）

**机器：** 面板一 Decode 看板「TPOT P50 / P90 / P99 / Max」面板的菜单里点 Explore，把查询换成下面的语句，查询类型选 Instant。「重启前」把时间范围的结束时刻设成第 10 步记下的停实例时刻；「重启后」设成重启以后 40 分钟。

**命令：** D 的批（第 1、2 批）比 TTFT 和 TPOT：

{{B:time_to_first_token_seconds_bucket{role="decode"}}

{{B:request_time_per_output_token_seconds_bucket{role="decode"}}

P 的批（第 3 批）只比 TTFT（P 每个请求只出 1 个 token，TPOT 记成 0）：

{{B:time_to_first_token_seconds_bucket{role="prefill"}}

**应当看到：** 每条一个数，单位是秒。

**不对时：** 重启后比重启前大 10% 以上：报给我们，由我们决定要不要回滚。这一项只作参考，不算这一批失败。

**带回：** 每批重启前、重启后的 TTFT（D 的批还有 TPOT）。

#### 第 21 步　这一批的结论

按「判失败的条件」逐条看这一批。都通过：做下一批（回到第 10 步）。有一条失败：停下，不做下一批；这一批的每个容器按第 22–26 步回滚，再按第 11 步的方式重启一次；已经成功的批不动；带回这一批的检查输出、vLLM 启动日志和第 15 步那条查询的输出。

### 五、回滚（只在失败时做，每个要回滚的容器）

要连面板二一起退回时，先找我们：面板手册里没有退回面板二的步骤。做过 Mooncake 部分的，同时做第 35 步，也只重启一次。

#### 第 22 步　把监控机上的备份送回容器

**机器：** 监控机执行第一条，那台机器执行第二条，容器里执行第三条。

**命令：**

{{B:ssh <SSH 账号>@<机器地址> mkdir -p /tmp/<机器>-<时间戳>}}

{{B:docker cp /tmp/<机器>-<时间戳>/. <容器名>:/tmp/<机器>-<时间戳>}}

{{B:cd /tmp/<机器>-<时间戳> && LC_ALL=C sha256sum -c SHA256SUMS}}

**应当看到：** 最后一条打印 `vllm.tar: OK`、`vllm-ascend.tar: OK`，`23`、`10`。

#### 第 23 步　重做补丁包解开和找目录

**机器：** 容器里。容器里的 `/tmp/t51-kit` 不在了时，先用那台机器上留着的补丁包重做第 2 步的前两条（拷进容器、解开核对）；再执行第 4 步的命令，重新设好 `VLLM_PKG`、`ASCEND_PKG`、`VLLM_ROOT`、`ASCEND_ROOT`、`F`。

**应当看到：** 和第 2、4 步相同。

#### 第 24 步　恢复三件

**机器：** 容器里。

**命令：**

{{B:BK=/tmp/<机器>-<时间戳>; K=/tmp/t51-kit/patches}}

**应当看到：** 前两条没有任何输出；最后一行是 `2c4acb3eeb1ced7d`。

**不对时：** 停下，带回输出。

#### 第 25 步　删掉新模块的编译缓存

**机器：** 容器里。

**命令：**

{{B:新模块的 .pyc 已删}}

**应当看到：** 只有 `新模块的 .pyc 已删` 一行。

#### 第 26 步　重启

按第 11 步的方式重启（不做导入核对），重启前后照第 10、12 步核对命令行（`BK` 用第 24 步的目录）。重启以后，`[PD_DIAG]` 记录不再写出，面板二上这个容器的实例没有新数据。

### 六、可选：替换 Mooncake 库

前 26 步不依赖这一部分，可以以后单独做。不做时，P 侧「后台 wait_for_save → put」下的 3 个子段 submit、wait_for_transfer、finalize 为空，父段 put 仍有值。做完以后这 3 段有值，47 段里 45 段有值；各实例的 vLLM 日志多出 `[BatchPutTiming]` 行。

**前提：** 第 1–21 步三批都已通过；第 13 步报 `mooncake=not_replaced`（不是 `BAD(other)`）；第 18 步在这一部分以前就量到的 `[BatchPutTiming]` 接近 0。不满足时先不做，带回。

**用的文件：** Mooncake 增量包 `t51-glm53-mooncake-kit.tar`（sha256 `6d70978c…`），放在监控机的 `<用户目录>/vllm-pd-patch-glm53/` 下，连同它的 `.sha256`。里面有替换库 `lib/libmooncake_store.so`（15402248 字节，sha256 `3131784385846064327e858d6dcdb11a6c7674c2162964d52f9ed0e866b52bb8`）和换库前的只读核对 `precheck/dist/t51_mc_precheck.py`（glm53mc-5）。

**怎么分批：** 每个容器只做一次第 27–30 步，第 31 步起才分批；分批和第四部分相同。换库到这个容器最后一个实例重启之间可能隔几个小时，这段时间里没重启的实例继续用原库。这样做的前提：补丁 04 只给 `Client` 加了三个非虚的私有成员函数声明，没有加数据成员、static 成员和 thread_local，没有虚函数，没有动虚表，所以两种构建同时在不同进程里运行时，布局和进程间传递的数据格式都相同。

**几条约定：** 只换 `<Mooncake 目录>/libmooncake_store.so` 一个文件，store 模块、兄弟库和自带的系统库都不动。换库用改名，不覆盖。备份不能放在 `<Mooncake 目录>` 或任何 `mooncake*.libs` 目录里。不设 `MC_LOG_DIR`。

这一部分另有几个占位符：`<Mooncake 目录>`（第 29 步第一条命令的输出，即 `site-packages/mooncake`）、`<MC 备份目录>`、`<MC 时间戳>`（第 29 步的输出）、`<原库 sha256>`（第 29 步记下的 64 位）。

#### 第 27 步　放进容器并核对

**机器：** 监控机执行前两条，那台机器执行第三条，容器里执行最后一条。

**命令：**

{{B:sha256sum -c t51-glm53-mooncake-kit.tar.sha256}}

{{B:scp <用户目录>/vllm-pd-patch-glm53/t51-glm53-mooncake-kit.tar}}

{{B:docker cp /tmp/t51-glm53-mooncake-kit.tar}}

{{B:mkdir -p /tmp/t51-mc && tar -xf /tmp/t51-glm53-mooncake-kit.tar}}

**应当看到：** 第一条 `t51-glm53-mooncake-kit.tar: OK`；最后一条只有一行 `f9e6073f692dd4ea`。

**不对时：** 最后一条前面有输出：增量包在拷贝中损坏，重新拷贝。最后一行报 `No such file or directory`：容器的 `/tmp` 被清空过，先用那台机器上留着的补丁包重做第 2 步的前两条（拷进容器、解开核对），再执行。

#### 第 28 步　换库前的核对（最后一道关）

**机器：** 容器里，在 vLLM 正在运行时跑。每个要换库的容器都跑一次。只读，几秒内结束，不导入 Mooncake，不写文件，不起子进程，不联网。

**命令：**

{{B:t51_mc_precheck.py; echo "exit=$?"}}

**应当看到：** 12 行，然后 `exit=0`。其中：

- 第一行 `t51_mc_precheck=glm53mc-5 build=78566d4-rwdi-ut-nouring-mp9.0.0/repl-p04i-0625b51e pid=<n> loaded=<n>`，`pid=` 不是 `none`。
- `mc_orig size=17610953 sha16=3bf82f8b0c5ab715 f1=same …`，`second_store=none`。
- `mc_build sections_other=same … result=same_build`，`actual=` 和 `predicted=` 相同，`prefixes=1`。
- `mc_exports … differing_families=none replacement_vs_site=-0/+3(expected -0/+3)`。
- `consumers_ok n_elf=<n> removed_names=0`。
- 最后一行 `mc_pre result=ok_replace`。

**不对时：** 最后一行不是 `mc_pre result=ok_replace`（`STOP_…`，退出码 2），或者只打印两行 `mc_pre result=STOP_missing`，或者打印一行 `REFUSED`：这个容器不换，其余容器也先不换，带回整段输出。各行含义和判决规则见增量包的 `precheck/使用说明.zh-CN.md`。

**带回：** 全部输出，每个容器一份。

#### 第 29 步　找到 Mooncake 目录，备份原库，拷回监控机（必做）

**机器：** 前三条在容器里；第四条在那台机器上；最后两条在监控机上。

**命令：**

{{B:MC_PKG=$(python3 -I -B -c}}

{{B:: > /tmp/t51-mc/vllm.ld_library_path}}

{{B:BKMC=$BKBASE/t51-glm53-mc-backup-}}

{{B:docker cp <容器名>:<MC 备份目录> /tmp/}}

{{B:scp -r <SSH 账号>@<机器地址>:/tmp/t51-glm53-mc-backup-}}

{{B:backup/<机器>-mc-<MC 时间戳> && LC_ALL=C sha256sum -c SHA256SUMS}}

**应当看到：**

- 第一条：`<Mooncake 目录>/libmooncake_store.so`；权限和大小，大小是 `17610953`；最后只有一行 `<进程个数> <和第一行相同的路径>`。
- 第二条：`vLLM 的 LD_LIBRARY_PATH 已记下：<n> 字节`，n 大于 0（是 0 时第 31 步的导入核对用当前 shell 的环境，照常往下做）。
- 第三条：备份目录名 `/tmp/t51-glm53-mc-backup-<MC 时间戳>`（`/tmp` 是 tmpfs 时在 `/root` 下），然后一行 `<64 位> libmooncake_store.so`，前 16 位是 `3bf82f8b0c5ab715`。记下备份目录名（`<MC 备份目录>`）和这 64 位（`<原库 sha256>`）。
- 最后一条：`libmooncake_store.so: OK`。

**不对时：**

| 看到 | 怎么办 |
|---|---|
| 第一条最后一部分的路径和第一行不同、不止一行，或者行末带 `(deleted)` | 停下，带回这几行。本容器已经做过第 30 步的，这一条不适用，不要重跑第 29 步 |
| 前 16 位不是 `3bf82f8b0c5ab715` | 停下，带回 |
| 监控机上 `FAILED` | 重新拷；核对通过以前不做第 30 步 |

#### 第 30 步　换库

**机器：** 容器里，接着第 29 步。

**命令：** 先确认变量都在：

{{B:"${MC_PKG:?}" "${MC_LIB:?}" "${BKMC:?}"}}

{{B:.t51_new_store.so" && chmod --reference}}

再看运行中的进程：

{{B:for d in /proc/[0-9]*; do grep -h 'libmooncake_store' "$d/maps" 2>/dev/null | awk '{print $6, $7}' | LC_ALL=C sort -u; done | LC_ALL=C sort | uniq -c
}}

**应当看到：** 第一条 `变量都在：<MC 备份目录>`；第二条 `3131784385846064`，然后权限（和第 29 步相同）和大小 `15402248`；第三条一行 `<进程个数> <Mooncake 目录>/libmooncake_store.so (deleted)`，进程个数和第 29 步相同。运行中的 vLLM 继续用它已经映射的原库，重启以后才加载新库。

**不对时：**

| 看到 | 怎么办 |
|---|---|
| 第一条报 `parameter null or not set` | 换过 shell：按第 29 步第一条重新设好 `MC_PKG`、`MC_LIB`，再设 `BKMC=<MC 备份目录>`（不要重做备份） |
| 第二条中途报错、没有打印 `3131784385846064`（例如 `mv` 没成功） | 先 `rm -f "$MC_PKG/.t51_new_store.so"`，再执行一次 |
| sha256 不是 `3131784385846064` | 停下，按第 35 步回滚 |
| 进程个数比第 29 步少，路径对、行末都是 `(deleted)` | 有进程退出了：记下两次的个数，继续 |
| 出现行末没有 `(deleted)` 的行 | 已经有进程加载了新库：停下，带回 |

#### 第 31 步　分批重启，第 1 批的那一个容器做导入核对

**机器：** 容器里。重启照第 11 步的方式；重启前后照第 10、12 步核对命令行和日志配置，`BK` 用 `<MC 备份目录>`。

**命令：** 只在第 1 批的那一个容器里、实例停掉以后、重新拉起以前执行：

{{B:vllm.ld_library_path); [ -n "$L" ] && export "$L"}}

**应当看到：** 一行 `<Mooncake 目录>/store.cpython-311-aarch64-linux-gnu.so <用时> s`。

**不对时：** 报任何错误时，不要用新库拉起：按第 35 步把这个容器回滚到原库，再拉起，带回这一行和 `/tmp/t51-mc/vllm.ld_library_path` 的字节数。`undefined symbol` 是新库和现场的库对不上；`libascend_hal.so: cannot open shared object file` 一类找不到 CANN、驱动库的错误，多半是导入时的环境和 vLLM 不同，由我们看过再决定。

**带回：** 这一行（第 1 批）。

#### 第 32 步　重启以后，按实例看用的是哪一份库

**机器：** 第一条在容器里；第二条在那台机器上，宿主机里。

**命令：**

{{B:port_of()}}

{{B:grep -nE "undefined symbol|libmooncake_store" <vLLM 日志文件>}}

**应当看到：** 第一条每个端口一行或两行：`<进程个数> port=<端口> new` 或 `<进程个数> port=<端口> old`（`new` 用新库，`old` 还在用原库；每个进程沿着父进程往上，按第一个带 `--port` 的命令行对到实例）。这一批的端口只有 `new`；本容器还没轮到的实例是 `old`，属预期。第二条没有输出。

**不对时：**

| 看到 | 怎么办 |
|---|---|
| 这一批的端口还有 `old`，或者出现 `port=?` 的 `old` | 带回这几行 |
| 第二条有输出 | 这一批失败 |

#### 第 33 步　用检测脚本核对

**机器：** 容器里，第 32 步通过以后。命令和第 13 步相同（P 用 P 的那一条，D 用 D 的那一条，最后加 `--baseline <这个容器第 3 步记下的 baseline_token>`）。

**应当看到：**

- `verdict vllm=ok ascend=ok(site_local=<n>) p2d=ok mooncake=ok`。
- `mooncake=` 一行里是 `changed_since_install=1 timing=2 store=replacement`（第 13 步时是 `changed_since_install=0 timing=0 store=site_original`；现场第 1、2 次检测的 `mooncake=` 一行都是 `changed_since_install=0 timing=0`）。`changed_since_install=1` 是安装记录里还是原库的哈希，属预期。
- 其余各行和第 13 步相同：`site_patches` 一行和第 3 步相同，`run` 行是 `logcfg=set`，单独一行 `baseline=same`。

**不对时：** 还是 `mooncake=not_replaced`（`store=site_original`）：库没有换上，停下，带回第 30 步的输出和这一段。其余见「判失败的条件」。

#### 第 34 步　`[BatchPutTiming]` 行、看板和磁盘

**机器：** 那台机器上，宿主机里；有请求以后。

**命令：**

{{B:grep -c 'complete=false'}}

**应当看到：** 每个文件一行：文件名、`[BatchPutTiming]` 行数、其中 `complete=false` 的行数。P 机器（第 3 批）行数大于 0，`complete=false` 是 0；D 机器只在往缓存池保存时写这种行，不保存时两个数都是 0。第 3 批做完以后，看板上 P 侧后台 wait_for_save/put 下的 3 段 submit、wait_for_transfer、finalize 有值，47 段里 45 段有值。

再照第 17、18 步量 30 分钟磁盘，按「判失败的条件」的磁盘项判。

**不对时：**

| 看到 | 怎么办 |
|---|---|
| `complete=false` 不是 0 | 不算失败，服务和看板读的这 3 段不受影响；但必须带回：它说明第 28 步的调用方扫描漏了东西（包目录以外的文件在用、有代码按名字取，或者现场的包不是扫过的那一份） |
| 重启 10 分钟以后、`[BatchPutTiming]` 行数大于 0，这 3 段仍为空 | 不回滚，带回这条命令的输出和面板手册里填的 `MOONCAKE_LOG_FILES` |

#### 第 35 步　Mooncake 的回滚（只在失败时做）

**机器：** 监控机执行第一条，那台机器执行第二条，容器里执行后两条；最后按第 11 步重启。和第 22–26 步一起回滚时，两边的文件都恢复以后只重启一次。

**命令：**

{{B:ssh <SSH 账号>@<机器地址> mkdir -p /tmp/<机器>-mc-<MC 时间戳>}}

{{B:docker cp /tmp/<机器>-mc-<MC 时间戳>/.}}

{{B:cut -c1-64 SHA256SUMS}}

先用第 29 步第一条命令的第一行重新设好 `MC_PKG`、`MC_LIB`，再恢复原库：

{{B:.t51_orig_store.so}}

**应当看到：** 第三条 `libmooncake_store.so: OK`，然后 64 位的 sha256，和 `<原库 sha256>` 相同；最后一条 `3bf82f8b0c5ab715`，然后权限和大小 `17610953`。重启以后，第 32 步第一条：回滚以后重启过的端口行末都没有 `(deleted)`（那条命令把它们记作 `new`，这时用的是恢复的原库）。第 33 步的检测脚本：`verdict` 最后一项是 `mooncake=not_replaced`；`mooncake=` 一行是 `timing=0 store=site_original`，`changed_since_install` 和第 13 步时相同（现场第 1、2 次检测都是 0）。vLLM 日志里不再有新的 `[BatchPutTiming]` 行，看板上那 3 段重新为空。

## 判失败的条件

每批对这一批的实例判。**先停下、带回（不算失败）**的：第 12 步命令行和重启前不同。下表任何一条出现，这一批算失败：停下，不做下一批，按第 21 步处理。

| 步骤 | 失败条件 |
|---|---|
| 11 | 第 1 批导入核对不是 `True`，或者报错 |
| 12 | 有实例的 `/health` 不是 `200`，或者起不来 |
| 12 | 有 vLLM 进程没有设 `VLLM_LOGGING_CONFIG_PATH` |
| 12 | vLLM 日志里有 `ImportError`，或者 `Traceback` 后面几行里有 `pd_diagnostics`、`pd_device_timing`、`worker_base`、`ascend_store`、`mooncake_connector`、`recompute_scheduler` |
| 13 | 出现 `BASELINE_DIFF 端口:项:旧值->新值 …`（重启前后各组实例的 kind、conn、spec、eager、async、load_async、tp、dp、调度器有变化） |
| 13 | `verdict` 的 vllm、ascend、p2d 三项有一项不是 `ok` 或 `ok(…)`，例如 `unpatched`、`absent`、`-`（这一项没有跑）、`BAD(…)`（mooncake 一项例外：`mooncake=BAD(other)` 不算） |
| 13 | `run` 行是 `logcfg=unset` |
| 13 | `site_patches` 和第 3 步不同 |
| 14 | `PD_DIAG_STARTUP` 的 `scheduler`、`model_runner` 不是第 14 步写的那几种，或者 `BalanceScheduler` 带 `balance=True` |
| 14 | D 上 `tp_rank=0` 的 `device_timing` 是 `off`（`?` 带回，交给留空比例判；P 上只带回） |
| 15 | 这一批的 D 实例 `cand_maps` 是 0，或者 `no_execution_id` 大于 0（`no_execution_id` 一出现就算失败） |
| 18 | 只判 `this_batch` 是 1 的行：D 的 `forward_empty_pct` 超过 1% 而且 `forward_empty` 比第 16 步大，或者 sampling 的这两项同样如此 |
| 18 | D 两次的 `pid_list` 不同 |
| 18 | 第二次比例为空、`ended_win` 大于 0、`forward_win` 或 `sampling_win` 大于 0（调度器侧断了，D、P 都算失败）。比例为空的其余情况不算通过，带回（P 只记录） |
| 19 | 重启 10 分钟以后、这一批的实例收到过请求，D 的 forward、sampling、start_load_kv/wait_for_transfer 仍然全为空（第 3 批看 P 的 forward、sampling、schedule） |
| 19 | {{PANELFAIL}} |
| 18（磁盘） | 节点目录每天增长 × `NODE_LOG_KEEP_HOURS` ÷ 24，再加这台机器的实例数 × 1 GiB（D 机器 16 GiB，P 机器 1 GiB），超过第 10 步 `df` 里节点目录所在文件系统 `Avail` 的一半 |
| 17、18（磁盘） | vLLM 日志里有 `[PD_DIAG]` 行（一出现就处理，不等满 30 分钟） |
| 18（磁盘） | 节点目录和 vLLM 日志在同一个文件系统上时（第 10 步的 `df` 只出一行），把 `[BatchPutTiming]` 每天的增长 × 现场 vLLM 日志保留的天数也加进上面那一条的预算，和同一个 `Avail` 的一半比（做了 Mooncake 部分以后才有意义） |
| 34（磁盘） | 做了 Mooncake 部分以后，`[BatchPutTiming]` 每台机器每天超过 43.1 GB（D 不往缓存池保存时应当是 0 行） |
| 31 | Mooncake 部分第 1 批的导入核对报错 |
| 32 | vLLM 日志里有 `undefined symbol` 或 `libmooncake_store` 的错误 |
| 33 | `mooncake=BAD(other)`；vllm、ascend、p2d 三项有一项不是 `ok` 或 `ok(…)`；出现 `BASELINE_DIFF`；`run` 行是 `logcfg=unset`；`site_patches` 和第 3 步不同 |

**不算失败、带回的：** 第 3 步、第 13 步的 `mooncake=BAD(other)`；第 13 步的 `vllm=ok(site_local=n)`；第 14 步 D 的 `?`、P 的 `off` 或 `?`；第 18 步 P 的留空比例；第 20 步重启后 TTFT、TPOT 大 10% 以上；Mooncake 部分以前第 18 步量到的 `[BatchPutTiming]` 明显大于 0（这些行回滚本套补丁去不掉，说明现场的 Mooncake 可能已经不是核对过的那份，Mooncake 部分先不做）；第 34 步 `complete=false` 不是 0。

## 带回清单

| 步骤 | 带回 | 每份 |
|---|---|---|
| 2 | `df -hT /tmp` 那一行 | 每个容器 |
| 3 | 检测脚本的全部输出；`baseline_token` | 指纹相同的容器只带一份输出；`baseline_token` 每个容器 |
| 5 | 不对时的输出；`grep` 显示 `**kwargs` 没传下去时，那条命令的输出 | 每个容器 |
| 6 | `<备份目录>` | 每个容器 |
| 8 | 三条命令的完整输出和 `<S>` | P、D 各一份 |
| 10 | `cmdline.before`、`df` | 每个容器；`df` 每台机器 |
| 11 | 导入核对那一行 | 第 1 批 |
| 12 | 命令行比较结果、日志配置那一行 | 每个容器 |
| 13 | 检测脚本的全部输出 | 每个容器 |
| 14 | 不对时的几行 | 每台机器 |
| 15 | 自检查询的输出 | 每批 |
| 16、18 | 留空比例两次的输出；第 1 批另要每个 D 实例的 `*_total`、`*_win`、`forward_empty`、`sampling_empty` | 每批，D、P 每个实例 |
| 18 | 磁盘那一行 | 每台机器 |
| 19 | {{PANELBRING}} | 每批 |
| 20 | 重启前、重启后的 TTFT（D 的批还有 TPOT） | 每批 |
| 21 | 失败时：这一批的检查输出、vLLM 启动日志、第 15 步的输出 | 失败的批 |
| 28 | 换库前核对的全部输出 | 每个容器 |
| 29 | `<MC 备份目录>`、`<原库 sha256>` | 每个容器 |
| 31 | 导入核对那一行 | 第 1 批 |
| 32 | 按端口数的那几行 | 每个容器 |
| 33 | 检测脚本的全部输出 | 每个容器 |
| 34 | `[BatchPutTiming]` 那条命令的输出；第 18 步的磁盘那一行 | 每台机器 |

## 附录 A　原因和背景

{{APPENDIX_A}}

## 附录 B　已知问题

{{APPENDIX_B}}

## 附录 C　没在 NPU 上、在现场测过的部分

{{APPENDIX_C}}

## 附录 D　文件和 sha256

{{APPENDIX_D}}
