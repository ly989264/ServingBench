# 《glm5.3 测试集群 proxy 打补丁操作手册》和《执行顺序总表》交付说明

## 交付件（终版）

| 文件 | sha256（前 16 位） | 和顾问审过的 8762c7f 比 |
|---|---|---|
| `执行顺序总表.zh-CN.md`（用户的唯一入口） | `a47f9dfac5be8673` | 新加 |
| `glm5.3测试集群proxy打补丁操作手册.zh-CN.md`（没有个人路径，只有一版） | `3925bbc704dd02d1` | 原 `56d2f0dc`；两条阻塞、10 条已知问题 |
| `t51-glm53-proxy-kit.tar.gz`（`.sha256` 是全长哈希） | `75fd49e8244f63ca` | 原 `a5d4f2b2`；核对脚本升到 px-2，其余文件不变 |
| `面板二删除test安装test-glm53操作手册.zh-CN.md` | `30c67f673be6ddad` | 没改，不重出 |
| `已填路径-只放私有Caching/面板二删除test安装test-glm53操作手册_已填路径.zh-CN.md` | `66789d427c6a3a7a` | 没改，不重出 |

本地提交 c21db91 里的第二轮版本作废，不要推：

- 面板手册 `41fc8ec6`：它把 proxy 的几行放回第 17 步；
- proxy 手册 `c9a6bb64`；
- 总表 `aa747037`。

现在的版本都按顾问审过的 8762c7f 和面板手册 30c67f67 第一页的总顺序：面板第 16 步 → proxy 手册 → 面板第 17–22 步。

## 顾问两条阻塞的改法和验证

**阻塞 1：proxy 由容器 1 号进程带起时，第 7 步会停掉容器。**

- **核对脚本 px-2：** 沿 ppid 走到容器里的根（父进程是 0 或看不到），根是 NSpid 1 时报 `STOP proxy_started_by_container_init`。在 `proc parent=` 一行后多打一行 `proc ancestor_root=<进程号> pid1=yes|no root=<命令行，地址遮掉>`。
- **手册：**
  - 第 3 步原因表加这一行；
  - 第 3 步、第 8 步的实验室输出后面，各加一句说明 px-2 多出的这一行；
  - 第 7 步「不对时」加「容器已退出：`docker start <容器>`，带回 `docker ps -a` 那一行」。
- **验证（`tests/t53/results/px2/`）：**
  - `A01`：容器的 1 号进程是 `sh -c`，由它带起 python（真的 proxy，v0.30）。结果 `proc ancestor_root=1 pid1=yes`，`RESULT: STOP proxy_started_by_container_init`。
  - `A02`：1 号进程是 `sleep`，用 `docker exec -d … sh -c` 起。结果 `proc ancestor_root=15 pid1=no`，`RESULT: OK`。
  - `A04`：照手册第 5、7 步备份、停、用 `start_proxy.sh on` 再起。结果 `proc ancestor_root=<proxy 自己> pid1=no`，`env PD_PROXY_DIAG=1 …`，`RESULT: OK`。
- **改法带出的一点：** proxy 直接跑在宿主机上时，往上走到的是宿主机的 1 号进程，第 3 步也报这一条。手册原因表里写明了：同样停下带回。

**阻塞 2：`start_proxy.sh` 要 bash 4.4 以上。** 第 5 步照顾问给的命令加了预检，写进「应当看到」；不通过就停下，不做第 6、7 步，带回。脚本没改。`A03` 是实验室同一镜像里的输出：`bash 5.2.21(1)-release：start_proxy.sh 可用`。bash 低于 4.4 的那一支没有实测，写进了附录 C。

**已知问题：** 顾问的 10 条照抄进 proxy 手册附录 B 第 4–13 条，行号改成了步骤号。其中：

- 第 1 条（strace 只覆盖 STOP 分支）：这次 OK 分支也在 strace 下跑过，照实写了，见下一节。
- 第 7 条（打补丁手册总顺序陈旧）：写明冻结版 98e65095 已经改成 6 段。

**改完以后跑过的：**

- 本地测试：main、v0.30 两版各 118 项，全部通过，0 失败（`T-summary.txt`）。环境是容器 t51g-vllmtest:v1：Python 3.12.3、fastapi 0.136.3、httpx 0.28.1、uvicorn 0.54.0。
- 重新打包，`SHA256SUMS` 13 行全部 `OK`。
- 个人信息扫描各类都是 0，扫的是总表、proxy 手册、补丁包解开的全部文件和核对脚本内嵌的两份原文件。
- 实验室没有重走。

## 核对脚本零影响：两个分支都在 strace 下跑过

- **STOP 分支：** 第一轮的 `tests/t53/results/proxy-walk/Z01-strace-check.txt`，px-1，2 行的桩文件，`version=other`。
- **OK 分支：** 这次的 `tests/t53/results/px2/Z02-strace-ok-branch.txt`，px-2，`RESULT: OK`：
  - 被检进程的命令行里带 proxy 文件名，工作目录里是 v0.30 原文件，用 `docker exec -d … sh -c` 起；
  - 走了 `site_versions`、环境读取、日志目录这几支；
  - 写打开 0；网络 0；clone、fork 0；execve 只有它自己 1 次；unlink、rename、mkdir 0；`write` 只写标准输出 1 次；
  - 读的是这个进程的 `/proc/<pid>/` 下 `cmdline`、`status`、`cgroup`、`environ` 和 proxy 文件。
- **订正：** 第一轮记录里列在「new .pyc」下的 3 个 `encodings` 的 `.pyc` 是镜像里原有的，不是脚本写的。第一轮说明里说它们是假 proxy 写的，不对。

## 执行顺序总表

- **起点：** 面板手册第 1–15 步做完；打补丁手册没开始；P 的 vLLM 当时没在跑，要先按平时的方式拉起，再跑第 3 步。
- **表的内容：** 14 行，每行写手册和步骤、机器、重启什么、用哪一步核对、不通过时去哪一步回滚或停下带回。
- **写明的几件事：**
  - vLLM 和 vllm-ascend（加 p2d）是同一个补丁包，一起换、一起重启；
  - 第 2 批对 test-glm53 是空的；
  - 第 19 步在最后一批以前是 `RESULT: FAIL`，以及哪些 FAIL 项不算失败；
  - 回滚时 Mooncake 和三件的牵连；
  - 段数按做到哪里：40 → 42 → 45，段数不是判据；
  - proxy 第 3 步报 STOP 时跳过 proxy，P、D 两个 lb_proxy 段为空：40 段，做了 Mooncake 是 43 段；
  - 第 17 步的 Mooncake 来源和换不换库的关系：不换库照做，put 下 3 段为空；
  - 全部做完以后 45 段或 42 段，预期为空的段。
- **表里只引用已走查手册的步骤号，不写新命令。** 只有两处是手册里没有的提示：
  - 面板第 18 步在第 17 步重启拉取程序 3 分钟以后做，这是第一轮走查里看到的；
  - 第 19 步不做 Mooncake 时 `RESULT: FAIL` 列出 put 下 3 段是预期的，这是按 coverage 的判法。
- **W1 的核对：** W1 对总表里打补丁那几行提的五条都照改了：第 2 批为空；第 19 步最后一批以前的情况；回滚的牵连和两句提示；`mooncake=BAD(other)`；第 8 步的停止条件。
- **行号：** 行号和打补丁手册最终版 48c88aa7（已填路径版 3c2c390e）第一页引用的一致：第 1–21 步对应第 1、3–6 行，第 27–34 步对应第 11–12 行。总表引用的就是这一版。

## 第一轮的内容（8762c7f，顾问审过的那一版）

面板包 tar（`e3885b93…`）、vLLM、vllm-ascend、Mooncake 的补丁都没有改。来源：仓库 pd-v3-wt-v31extra（分支 v3.1-extra）`docs/v3.1-extra/`，走查记录在 `tests/t53/`。

### 补丁包里有什么

| 文件 | 说明 |
|---|---|
| `t51_proxy_check.py` | 只读核对脚本，版本 px-1 |
| `main/` | 给 vllm-ascend main 那一版（原文件 `ffd9a54456dc6ad1`，1440 行）：打好补丁的文件 `205aa8d49ad7f4b1`（1831 行）、补丁、原文件、本地测试。就是产物 5 |
| `v0.30/` | 给 v0.30 那一版（原文件 `85b8982948530758`，1396 行）：打好补丁的文件 `68e07c0abd1fbf4c`（1787 行）、补丁、原文件、本地测试。这次移植的 |
| `stop_proxy.sh`、`start_proxy.sh` | 停 proxy；按备份下来的参数、环境、工作目录、输出文件原样起 proxy，加 `on` 时多两个环境变量 |
| `SHA256SUMS` | 13 个文件 |

### 现场是哪一版：要准备两版

- vllm-ascend 的 `load_balance_proxy_server_example.py`：现在的 main（536f084cc，2026-11-19）和产物 5 的原文件相同（`ffd9a544`），从 6658faefc（2026-10-30）起没变过，v0.32.0rc1 也是这一版。
- glm5.3 现场的 vllm-ascend 是 d4ad49796。它和 v0.30.0 里的这个文件是 `85b89829`（08c511a03，2026-10-10），和产物 5 的原文件不同。
- 用户说现场用的是「最新 main」的文件，但现场容器里的 vllm-ascend 是 d4ad49796，两种都有可能。补丁包两版都带，第 3 步的核对脚本认出是哪一版，第 6 步按它选文件。两版以外的版本，第 3 步报 `STOP`，带回差异以后再移植。
- 核对脚本里记了这个文件最近 20 个上游版本的哈希。是更早的上游版本时，它能说出是哪一次提交。

### 移植到 v0.30 和调用路径比对

- **移植：** 用 git 把产物 5 的补丁提交 cherry-pick 到 v0.30 的原文件上，没有冲突。两版补丁里加、删的行逐行相同，只是上下文不同（v0.30 没有 `import hashlib`，也没有 main 独有的几行）。`v0.30/proxy-pd-diag.patch` 用 `git apply` 打到 v0.30 原文件上，结果和 `v0.30/load_balance_proxy_server_example.py` 逐字节相同。
- **调用路径：** 补丁改动的函数有 27 个。其中 3 个在两版之间本来就不同：`assign_instances`、`handle_completions_impl`、`stream_service_response`。逐个比较补丁后的函数体，两版只差 main 独有的几行：prefill 会话亲和（`--enable-prefill-affinity`、`X-Session-Id`）和非流式请求去掉 `stream_options`。补丁的插入点和调用都相同。
- **本地测试：** 产物 5 的测试换上 v0.30 的原文件和补丁文件，118 项全部通过，0 失败。环境是容器 t51g-vllmtest:v1，Python 3.12.3、fastapi 0.136.3、httpx 0.28.1、uvicorn 0.54.0、starlette 1.10.0。原文件本身就要求 Python 3.10 以上，补丁没有提高这个要求。

### 核对脚本：为什么不用 glm53-6 的 `--proxy-file`

detector glm53-6 的 `--proxy-file` 能认出文件是不是 main 那一版，也能列出改过的函数。但它不合这次的要求：

- 它往 `/tmp` 写明细文件；
- 不打印文件的 sha16 和行数；
- 不报 proxy 怎么起的、输出写到哪里。

所以另写了一个最小的 `t51_proxy_check.py`：

- **怎么跑：** 必须带 `-I -S -B`，少一个就打印 `REFUSED`，什么都不读。手册里用 `docker exec -i <容器> python3 -I -S -B - < t51_proxy_check.py` 从标准输入读进容器，不往容器里拷文件。
- **报什么：**
  - 文件：路径、sha16、行数、版本（`main`、`v0.30`、`patched-*`、更早的上游版本或 `other`）。是 `other` 时，报和最接近那一版的差异块数、增删行数、涉及的函数；加 `--show-diff` 时另打印差异全文。
  - 启动：参数（地址换成 `<host>`、`<ip>`）、父进程、工作目录、标准输出和标准错误去哪、是不是容器的第一个进程。
  - 环境和依赖：`PD_PROXY_DIAG*` 环境变量，proxy 那个 Python 的 fastapi、httpx、uvicorn 版本。
  - 日志目录：proxy 看不看得到 `/data/pd-diag/logs`、能不能写，以及 `dev_ino`。手册第 3 步拿它和宿主机上的 `stat` 比，确认容器里和宿主机上是同一个目录。
- **结论行：** 最后一行是 `RESULT: OK` 或 `RESULT: STOP <原因>`。
- **零影响：** STOP 分支第一轮用 strace 验证过（`tests/t53/results/proxy-walk/Z01-strace-check.txt`），OK 分支这次也用 strace 验证过（见上面一节）。第一轮的记录：
  - 没有以写或建的方式打开任何文件；
  - 没有 socket、connect；
  - 没有 clone、fork；只有它自己那一次 execve；
  - 没有 unlink、rename、mkdir；
  - `write` 只写标准输出。
  - 只 import 标准库，没有 subprocess、socket。

### 要查清的两件事

**1. 同一份 proxy 行会不会收两遍？** 不会进表两遍，但不排除时会触发解析失败。

- 节点日志的拉取是递归的：`rsync -r`，`--include=*/ --include=*.log --exclude=*`。
- proxy 写进 `/data/pd-diag/logs/proxy/` 时，节点日志的拉取会把它镜像一份到 `<镜像目录>/<机器>/proxy/`。
- 采集器读节点文件时，要求文件名是 `api|engine|worker|pool.*.log`，所以这一份每行都解析失败：进 dead-letter，触发 Prometheus 的解析失败告警（5 分钟超过 20 行）。
- 这一份不会进 `proxy_line`。那张表只读 `<镜像目录>/_proxy/`，节点文件的来源已经排除了 `_proxy`。
- 实验室里专门试过一次：去掉排除以后，镜像里多出 `<P 机器>/proxy/`（2 个文件），dead-letter 抽样 10 行，都是这些 proxy 行；`proxy_line` 仍是 5 行、5 个请求。记录在 `N01-negative-no-exclude.txt`。

**2. 怎么配：** 手册里写死一种，test-glm53 的 `deploy.env` 末尾加：

```
# proxy 打补丁手册第 4 步
PROXY_COLLECT=1
PROXY_MACHINE=<proxy 机器地址>
PROXY_LOG_DIR=/data/pd-diag/logs/proxy
PULL_RSYNC_FLAGS="--append --exclude=/proxy/"
```

- `PROXY_MACHINE` 写 proxy 所在的那台 glm5.3 机器，写法和清单、`bin/puller.sh status` 里相同。
- `PROXY_LOG_DIR` 是宿主机路径。第 3 步核对过：容器里在同一路径，是同一个目录。
- `--exclude=/proxy/` 加在 `PULL_RSYNC_FLAGS` 里，因为拉取程序把它放在 include 规则前面，先匹配先生效。它只排除节点日志目录顶层名叫 `proxy` 的子目录；proxy 来源拉的是 `/data/pd-diag/logs/proxy` 本身，不受影响。`--append` 是原值。
- 这几行要在重启 proxy 之前加，所以放在本手册第 4 步，不在面板手册第 17 步。

**和节点上别的程序不冲突（读代码）：**

- 节点过滤器只清理自己实例目录里、自己命名规则的文件；
- 删除集群时的清理只删深度 2 的 `api/engine/worker/pool.*.log` 和空目录；
- proxy 只清理 `proxy-<端口>/` 里自己的文件。

### 实验室走查

**实验室 t51k：** 每个容器都 `--rm`；起之前看过 docker stats；走完马上拆掉，容器、卷、网络都是 0；t14p2-* 没碰。

**怎么搭的：**

- glm53-1：v3，mock 模式。
- test-glm53：装在第二对机器上，用面板包 e3885b93，照面板手册第 11–16 步，vLLM 是替身。
- 现场那样的 proxy：单独一个容器 t51k-pxr。
  - 镜像 t51g-vllmtest:v1；挂 P 机器的 `/data`；带 `t33.machine` 标签，P 机器的 `docker ps` 看得到它。
  - 由 `docker exec -d … sh -c 'cd /opt/proxy && python3 load_balance_proxy_server_example.py --port 8000 --workers 2 … >> /var/log/proxy.log 2>&1'` 起。
  - 文件是 v0.30 那一版的原文件。
- 节点机器上没有 Python（和宿主机没有 Python 的情况一样），所以核对脚本在容器里跑。

**怎么走：** `tests/t53/walk_px.py` 从手册里取出每一步的 bash 块，原文执行，只把 `<…>` 换成实验室的值。

- proxy 机器上的命令在 P 机器（t51k-n-p2）上以 root 执行，监控机的命令在 t51k-mon 上执行。
- 第 3 步的值（进程号、文件路径、端口、版本）从第 3 步的输出里取。

**结果，全部和手册里「应当看到」一致：**

- **第 1 步：** P 机器上 `proxy 在容器 /t51k-pxr`；D 机器上只有最后一行。
- **第 2 步：** 13 行 `OK`。
- **第 3 步：**
  - `version=v0.30`、`RESULT: OK`；
  - 宿主机和容器的 `dev_ino` 相同；
  - 参数里的地址都换成了 `<host>`、`<ip>`。
- **第 4 步：** 加了 5 行；`status` 里 P 机器两行（节点和 proxy），都是 `OK`。
- **第 5–7 步：**
  - 备份的原文件 sha16 和第 3 步相同；
  - 换成 `patched-v0.30` 以后，停（2 个 worker 一起退出），再用 `start_proxy.sh on` 起来。
- **第 8 步：** 和第 3 步比，只差这几项：
  - 进程号；
  - `parent_pid=0`（由 `docker exec -d` 起）；
  - `env PD_PROXY_DIAG=1 PD_PROXY_DIAG_DIR=/data/pd-diag/logs/proxy`；
  - 文件版本。
  - 参数、端口、worker 数、输出文件都相同。
- **第 9 步：** 5 个请求都是 200；`proxy-8000/` 里 2 个文件（每个 worker 一个），共 5 行 `pd_proxy_request`。
- **第 10 步：**
  - 节点日志镜像里的 proxy 子目录 0 个；
  - proxy 来源镜像 2 个文件；
  - dead-letter 0 行；
  - `proxy_line` 的 rows、ok、with_p、with_d、requests 都是 5。
- **第 11 步：** `P lb_proxy 5 / 100`、`D lb_proxy 5 / 100`。100 个请求里只有经过 proxy 的这 5 个有值；其余是替身自己回放的。
- **面板手册新第 17 步：** 只加 Mooncake 3 行，`status` 4 行都是 `OK`（P、D、proxy、mooncake）。
- **面板手册第 18 步：**
  - 紧接着做时有一条 FAIL：「Prometheus 抓 vllm-timing-puller-test-glm53 不到」。这是因为拉取程序刚重启，手册要求等至少 3 分钟才做第 18 步，我没有等。
  - 等 75 秒以后重做，`RESULT: OK`。
  - coverage 是 47 段里 35 段有值，和面板手册里实验室的那 10 段一致。
- **回滚 1–3：**
  - proxy 回到原文件、原环境（`PD_PROXY_DIAG*=unset`、`version=v0.30`）；
  - `deploy.env` 删掉那 5 行，`status` 里没有 proxy 那一行；
  - `/data/pd-diag/logs/proxy` 已删。
  - 回滚后的核对输出和第 3 步相同，只差进程号和父进程。

**没有在实验室里走的：**

- `main` 那一版。产物 5 当初的本地测试 114 项通过。
- proxy 不在容器里的那一支。

### 面板手册的改动（只改文字，22 步编号不变）

W1 引用的第 3、15、16、18 步没动。改了这几处：

- **第一页总顺序：** 改成 打补丁手册 → 第 16 步 → proxy 打补丁手册 → 第 17–22 步。
- **第一页的值表：** 去掉 `<proxy 机器地址>`、`<proxy 日志目录>`，第 17 步只用 `<P 实例日志文件>`。
- **第 16 步末尾：** 加一句「先做 proxy 打补丁手册，再做第 17 步」。
- **第 17 步：**
  - 标题改成「打开 Mooncake 采集来源」；
  - 命令只加 Mooncake 的 3 行；
  - 说明 proxy 的 4 项已在 proxy 手册第 4 步加过；
  - 「不对时」加一句：没有 proxy 那一行时，先做 proxy 手册。
- 已填路径版用同一个生成脚本重做，和占位符版只差两个路径值。和上一版已填路径版 `f2dfd93c` 比，差的就是上面这 19 行。

### 个人信息扫描

扫描对象：两份手册的占位符版、补丁包解开的全部文件、核对脚本里内嵌数据解码出来的两份原文件，以及本说明。

- `scan_pii.py` 的各类都是 0：`/home`、`/Users`、个人 `/data` 目录、桌面目录、GitHub 账号、邮箱、工号样式、IPv4、主机名。
- 另按名字查了本机账号、实验室地址段、实验室口令、个人目录名、临时目录，也是 0。
- proxy 手册以后放进公开的 GitHub 仓库，里面没有个人路径，所以只有一版。

## 已知问题

见 proxy 手册（`3925bbc7`）附录 B，共 13 条：第 1–3 条是第一轮的，第 4–13 条是顾问的 10 条。

## 没有测过的部分

见 proxy 手册附录 C。这次加了两条：

- 第 5 步 bash 预检不通过的那一支（bash 低于 4.4）没有实测；
- px-2 的两种起法只在单独的容器里补测过，没有在实验室里重走手册。
