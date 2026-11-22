# glm5.3 环境检测脚本使用说明

脚本：`t51_glm53_check.py`（glm53-8，单个文件，期望哈希、比对和导出用的公开原文件、T51、T56、T58 三版打完补丁后的样子、Mooncake 替换库 v1 和 v2 的指纹都已内嵌）。glm53-8 和 glm53-7 的读取、输出格式、对环境的影响都相同，多出三处，见「glm53-8 多出的三处」。只读：不导入 vLLM、vllm-ascend、torch、torch_npu、Mooncake，不启动 NPU，不改任何文件；只往 `/tmp` 写明细文件和（加 `--export-diff` 时）导出文件。只用 Python 标准库，Python 3.9 起可用。对环境的影响逐条列在最后一节。

## 在哪里跑

| 位置 | 次数 | 参数 |
|---|---|---|
| 每个要打补丁的 P 容器 | 每个容器一次 | 5 个启动文件，另加 `--startup-log` |
| 每个要打补丁的 D 容器 | 每个容器一次 | 同上，`--start-script` 传 `start_d0.sh`（现场 D 的启动文件名） |
| proxy 所在的机器或容器 | 一次 | 只传 `--proxy-file` |

在容器里用 root 跑（`docker exec` 默认就是 root），并且用运行 vLLM 的那个 `python3`。不是 root 时读不到 vLLM 进程的环境变量和已加载的库，相关项会显示 `?` 或 `noperm`。

**必须带 `-I -S -B` 三个参数运行**（见下面的命令）：`-I` 不读 PYTHONPATH 等环境变量、不把脚本所在目录加入搜索路径；`-S` 不执行 site-packages 里 `.pth` 文件中的代码；`-B` 不写 `.pyc`。少一个，脚本就只打印一行 `REFUSED`，什么都不读、不写，退出码为 3。

## 命令

先把脚本拷进容器，例如 `docker cp t51_glm53_check.py <容器名>:/tmp/`，然后：

```bash
docker exec <容器名> python3 -I -S -B /tmp/t51_glm53_check.py --run-dp-template <路径>/run_dp_template.sh --launch-online-dp <路径>/launch_online_dp.py --start-script <路径>/start_p.sh --mooncake-json <路径>/mooncake.json --start-mooncake <路径>/start_mooncake.sh --startup-log <vLLM 日志文件>
```

- `--start-script` 在 P 上传 `start_p.sh`，在 D 上传 `start_d.sh`，要传本机上实际存在的那个文件。第一行的 `role=` 按运行中 vLLM 的 `kv_role` 判断；没有运行中的 vLLM 时才按 `--role` 或文件名判断。传进来的文件名或 `--role` 和运行中的不一致时，会多出 `role_conflict=`，说明传错了文件。
- `--startup-log` 可选，传 vLLM 标准输出所写的那个日志文件，用来确认 runner 是 V1 还是 V2。没有这个文件就不传。
- 5 个启动文件里缺哪个就不传哪个，脚本照常跑，最后一行之前的 `files` 行会列出缺的项。

proxy 上：

```bash
python3 -I -S -B t51_glm53_check.py --proxy-file <路径>/load_balance_proxy_server_example.py
```

用跑 proxy 的那个 `python3`，这样 fastapi、httpx、uvicorn 的版本才对得上。

## 导出本地修改（`--export-diff`）

glm5.3 的 vllm-ascend 构建时有本地修改（版本串带 `.d20261018`）。要把我们的补丁移植到现场的真实代码上，请在一台 P 容器、一台 D 容器上各加 `--export-diff` 再跑一次：

```bash
docker exec <容器名> python3 -I -S -B /tmp/t51_glm53_check.py <和上面相同的参数> --export-diff
```

- 它把比对范围内的文件（vLLM 27 个、vllm-ascend 16 个：补丁会改的文件、测量点调用链上的文件、补丁依赖的接口所在的文件），和脚本里内嵌的公开原文件（vLLM `v0.30.0`、vllm-ascend `d4ad49796`）逐个比较，写成一个 unified diff 文件；同一目录下公开版本没有的 `.py` 文件整份列出。其他改过的文件只在文件头列出文件名，不导出内容。
- 写出之前，把其中的 IP、主机名、口令类变量的值、`/home/<用户名>`、`/data/<目录>`、工号样式的字符串（一个字母加 8 位数字）都换成占位符，再按 IP、主机名、口令、`/home` 自查一遍；自查不过就不写，只打印 `export=BLOCKED`。
- 终端多一行，例如：

  ```
  export=/tmp/t51_glm53_diff_<指纹>.patch diffed=4 new=1 missing=0 lines=52 redacted=5
  ```

  `diffed` 是有改动的文件数，`new` 是整份列出的新文件数，`missing` 是现场没有的文件数，`redacted` 是被换成占位符的处数。
- **这个 `.patch` 文件请带回来。** 带出之前请先看一遍，确认没有不该带出的内容。脚本只认得出 IP、工号样式的字符串、本机的主机名，以及启动文件和配置里出现过的地址和口令；代码里写死的其他机器的主机名（不是 IP 的那种）它认不出，要靠人工看。

## glm53-8 多出的三处

- **多认一版补丁 T58：** `verdict` 里是 `ok(t58)`；`vllm=` 行的 `state=` 在完全打对时为 `v0.30.0+t58`，`port=` 列出三版各自的数。
- **`run` 行末尾多一项 `diag_level=<级别>(<来源>)`：** 从运行中 vLLM（API 进程）的环境读 `PD_DIAG_LEVEL`，规则和补丁里相同。
  - 不设或空串是 `3(default)`；`1`、`2`、`3` 是 `(env)`；其余值按 3，是 `3(invalid)`。
  - 级别也算进 `baseline_token`，升级前后级别意外变了，就报 `BASELINE_DIFF …:diag_level:3->2`。
  - glm53-7 打出的旧 `baseline_token` 里没有级别，按 3 比。
- **Mooncake 认两版替换库：** v1（增量包 `6d70978c`）、v2（增量包 v2），`verdict` 里是 `mooncake=ok(v1)`、`mooncake=ok(v2)`；`mooncake=` 那一行是 `store=replacement(v1)` 或 `store=replacement(v2)`。没换是 `mooncake=not_replaced`。v2 的库里没有 `[BatchPutTiming]` 这类日志串，所以同一行的 `timing=` 是 `0`（v1 是 `2`）；v2 的计时写在节点目录的 `logs/mooncake/` 下，检测脚本不读那些文件。

## 几行新增内容的含义

- `vllm=`、`ascend=` 行末尾：`from=` 是运行中的 vLLM 导入这个包时会用的那一份是从哪里找到的。脚本按 vLLM 进程的命令行、工作目录、PYTHONPATH、site-packages 和其中 `.pth` 文件（只读其中的路径和可编辑安装的映射，不执行）重建它的搜索路径，取第一个：`script_dir`、`pythonpath`、`user_site`、`site`、`pth`、`editable`。`cands=` 是沿途找到的同名包的个数，大于 1 时明细里列出每一份；`dist_at=` 是版本串所在的安装信息是否就描述这一份代码（`same`／`other`／`none`）；`rec_diff=改动数/多出数` 是和安装时记录的文件相比，事后改动和多出的 `.py` 文件数（可编辑安装没有记录，为 `-`）；`so=` 是运行中的 Worker 实际加载的这个包的 `.so` 是否来自同一份代码（`same`／`other`／`none`）；`pyc=a/b,match:c,after_start:d` 是比对范围内 b 个源文件里有 a 个带本 Python 版本的 `.pyc`，其中 c 个的头部（版本号、源文件时间和大小）和源文件对得上，d 个是 vLLM 启动以后写的（只读 16 字节的头部）；`tree_ver=` 是这一份代码自己的版本文件里写的版本。
- `ascend_tree`：改动、多出、缺少的文件按顶层目录计数，只在有多出或缺少时出现。
- **打完补丁后的核对。** 脚本内嵌了两版补丁打完后的样子，版本名 `t51`、`t56`：
  - `t51`：vLLM `v0.30.0` 加 t51-glm53-vllm `40152b2`（补丁 `7e2ec359`，`SHA256SUMS.after` 为 `5a708485`，25 个文件都在 `vllm/` 下：改 23 个、新增 2 个）；vllm-ascend `d4ad49796` 加 t51-glm53-ascend `dc92d32` 的 11 个文件（补丁 `024a8b3e`，`SHA256SUMS.after` 为 `4c2d978a`；改 9 个、新增 2 个）。
  - `t56`：vLLM `v0.30.0` 加 t51-glm53-vllm `796927a`（补丁 `7959cbb7`，`SHA256SUMS.after` 为 `9a8a5507`，同样 25 个文件，比 `t51` 多改 4 个：`scheduler.py`、`request.py`、`pd_diagnostics.py`、`worker_base.py`）；vllm-ascend `d4ad49796` 加 t51-glm53-ascend `cf72b07` 的 11 个文件（补丁 `d0fbb82b`，`SHA256SUMS.after` 为 `0b46fa15`，比 `t51` 多改 2 个：`model_runner_v1.py`、`pd_device_timing.py`）。
  - Mooncake 替换库：`libmooncake_store.so`，15,402,248 字节，sha256 `3131784385846064…`（78566d4 加补丁 04 `0625b51e`，见 `mooncake/replace/README.zh-CN.md`）。现场原库按小核对 f-1：17,610,953 字节，`3bf82f8b0c5ab715`。

  `vllm=` 行的 `state=` 在完全打对时为 `v0.30.0+t51` 或 `v0.30.0+t56`；行末的 `port=t51:a/b,t56:c/d` 是两版各自涉及的 b、d 个文件里，有 a、c 个和那一版逐字节相同。打完哪一版，那一版就是 `25/25`、`11/11`；另一版的数小一些，是两版之间不同的那几个文件，不是错。现场在公开代码之外另有本地改动（vllm-ascend 的 `patch/` 下几个文件、P/D connector），所以打完以后 `ascend=` 仍是 `state=modified`，看 `port=11/11` 和最后的 `verdict` 行。
- **`verdict` 行（打完补丁后看这一行）。** 每个组件一项：
  - `vllm=`、`ascend=`：`ok(<版本>)` 表示那一版移植的文件全部逐字节相同，两版都对得上时取新的一版；`ascend=` 括号里另有 `site_local=n`，是移植之外还有 n 个现场本地改动的文件（不算错）；`unpatched` 表示还没打；`BAD(<版本>:port=a/b,other=n)` 表示有移植文件不对（按对得最多的那一版报），看 `strict` 行的 `_at=` 找位置。
  - **三项的版本要相同。** 例如 `vllm=ok(t56) ascend=ok(t51)` 是只升了一半：每一项单看都是 `ok`，合起来不对。
  - `p2d=`：P/D connector 在现场是重放出来的，字节取决于现场原文件，所以按结构认，见下一条。
  - `mooncake=`：`ok` 表示 `libmooncake_store.so` 是交付的替换库；`not_replaced` 表示还是现场原来那个（f-1 带回的 sha16）；`BAD(other)` 表示两者都不是。
- **`strict` 行末的 `p2d=`。** 对 `mooncake_connector.py` 做 4 项核对：
  1. `pd_diagnostics` 的导入正好 1 处；
  2. 内嵌的 `replay_p2d.py` 里每一处改动的代码，都按顺序出现在它所属的函数里：`t51` 是 `02304c8f` 的 C01–C28（`changes=28/28`），`t56` 是 `9b12baa6` 的 C01–C29（`changes=29/29`）；缺的列在 `missing=`；
  3. 重放工具自己的结果检查全部通过（`struct=ok`）；
  4. 内嵌的 `extra_checks.py`（`2759c06f`，两版共用）在这个文件上给出的那一行，和在那一版 p2d 替换件（`t51` 是 `f953b27c`，`t56` 是 `256b335e`）上给出的相同（`extra=same`）。

  两版各核一遍，取四项都过的新的一版，写成 `p2d=ok(<版本>,changes=…)`，`verdict` 行是 `p2d=ok(<版本>)`；哪一版都不全时，按找到的改动最多的那一版报 `BAD(<版本>:missing:…)`；没打为 `p2d=not_replayed`。`replacement_file=1` 表示这个文件和替换件逐字节相同：现场原文件就是公开版本时会这样，不是错。
- **`site_patches` 行。** 现场 vllm-ascend 本地加的几个文件，各报 sha16，写成一行：`patch/__init__.py`、`patch/platform/__init__.py`、`patch/platform/patch_kv_load_failure_reason.py`、`patch/platform/patch_prefill_length_validation.py`（不存在为 `absent`）。打补丁前后都应和 f-3b 带回的值相同（后两个是 `d7c764bb425993c0`、`c36392a6338bc2f1`）；对不上就停。
- **`mooncake=` 行的 `store=`、`timing=`。** `store=replacement|site_original|other` 同上。`timing=` 是库里补丁 04 的计时行字符串有几种（`[BatchPutTiming]`、`[BatchUpsertTiming]`），换了库以后为 2，原库为 0。换库后 `changed_since_install=1` 是预期的（安装记录里还是原库的哈希），不是漂移。`mooncake_build` 行末的 `siblings=` 是四个兄弟库的导出名个数和指纹，带 `!lab` 的和实验室构建的不同。
- **配置基线（`baseline_token`）。** 有运行中的实例时，最后一行（`fingerprint=`）末尾多一个 `baseline_token=…`：把每组实例的 kind、conn、spec、eager、async、load_async、tp、dp、调度器编成一串。打补丁前那次检测记下它；打补丁、重启以后那次加 `--baseline <这串>`（步骤号见打补丁手册），这时最后一行不再打印 token，改为单独一行：`baseline=same` 表示配置没变，`BASELINE_DIFF 端口:项:旧值->新值 …` 逐项列出变了的地方。这一项不增加任何读取。
- **启动文件和运行中的进程（`files` 行的 `file_vs_run=`）。** 结论：运行中的进程是准的。
  - 现场的 `start_p.sh`、`start_d0.sh` 里没有 `vllm serve` 这一行（f-3b：`serve_lines=0`），参数都来自模板 `run_dp_template.sh`，所以只拿模板和运行中的进程比。
  - 模板的 serve 行里，有的选项经变量传入，变量在文件的别处设定（D 的 `--async-scheduling` 就是这样，只在模板里出现一次，不在 serve 行里）。凡是排在最后一个变量之前、可能被变量里的同名选项覆盖的值，文件一侧记为未知，不算不一致；`--kv-transfer-config`、`--speculative-config` 不在 serve 行上时，同样记为未知。
- `strict`：比对范围内的文件和脚本内嵌的公开原文件逐字节比较。`vllm=26/27` 表示 27 个文件里 26 个完全相同；`vllm_at=` 列出不同的文件和第一处不同在现场文件里的行号范围，`(+n)` 是同一文件里另外还有几处。每一处不同的精确位置（公开版本的行号和现场的行号）在明细文件的 `strict` 里，内容在 `--export-diff` 的导出文件里。
  打完补丁以后，移植改到的文件和移植后的版本比，移植新增的文件也算进来：`vllm=29/29(port:25)` 表示 29 个文件都对得上，其中 25 个是移植后的版本。和两者都不同的文件，按差得少的那一个给位置，和移植后的版本比的在文件名后标 `~port`。移植新增的文件现场没有时（没打补丁）不计数。
- `glog`：Mooncake 的 glog 日志会写到哪里，以及据以判断的事实。`lib=` 是 Worker 加载的 glog 库；`env=` 是 Worker 环境里设了哪些 `GLOG_*`；`init_refs=` 是 Worker 加载的、链接了 glog 的库里，哪些引用了 `InitGoogleLogging`（只读这些库的动态段，`scanned=` 是读过的库数）；`log_files=` 是 vLLM 启动以后 glog 可能用的目录（`GLOG_log_dir`、`TEST_TMPDIR`、`TMPDIR`、`TMP`、`/tmp`）里新出现的 glog 日志文件数；`worker_fd2=` 是各 Worker 的标准错误指向什么；`same_as_log=` 是其中多少个和 `--startup-log` 传的是同一个文件（按 inode 比）；`dest=` 是结论：`stderr(not_initialized)` 表示没有人初始化 glog、也没有 glog 日志文件，glog 只写标准错误；`stderr(env)` 表示设了 `GLOG_logtostderr`；其余情况给出原因。
- `mooncake_build`：`git=` 是编进 Mooncake 库里的源码提交号，`wheel_tag=` 是安装包的平台标记，`store_debug=` 是库里是否带调试信息，`glog_from=` 是加载的 glog 来自安装包自带（`bundled`）还是系统（`system`），`store_needed=` 是 `libmooncake_store` 依赖的库名。
- `log` 行的 `glog_files=`：vLLM 启动以后，`/tmp`（设了 `GLOG_log_dir` 时还有那个目录）里新出现的 glog 日志文件数。
- `inst[...]` 行末尾的调度器三项：
  - `sched=类名(依据)`：这个实例实际用的调度器类，依据是命令行的 `--scheduler-cls`（`cli`）、vLLM 的默认（`default`，开了 `--async-scheduling` 时为 `AsyncScheduler`），或 vllm-ascend `d4ad49796` 按 `--additional-config` 选出的（`recompute_scheduler_enable`、`dyntra_lb`、`profiling_chunk`、`batch_job_sched`、`short_request_first`）。选择顺序照 `platform.py` 的 `_check_ascend_config`、`_setup_worker_and_scheduler`：后面的覆盖前面的；`recompute_scheduler_enable` 只在 D 上生效，P 上记为 `+recompute_ignored_on_P`。命令行没写 `--async-scheduling`/`--no-async-scheduling` 时，vLLM 启动后才决定是否异步，这里写成 `Scheduler|AsyncScheduler(default(async_unknown))`。
  - `sched_path=`：这个类的 `schedule()` 是否走到 vLLM 的 `Scheduler.schedule()`（补丁 01 在那里记录每一步）。`vllm_schedule` 表示走到；`rewritten:类@文件:行` 表示某个类自己重写了 `schedule()` 且不调用 `super().schedule()`，这时补丁 01 的调度器字段全部缺失（首次候选、`schedule`/`other`、`pd_execution_id` 等）。判断方法：按文本解析 vLLM 和 vllm-ascend 的调度器源文件（`vllm/v1/core/sched/` 两个、`vllm_ascend/core/` 五个），按 Python 的继承顺序找第一个定义 `schedule()` 的类；不导入。
  - `sched_keys=`：`additional_config` 里开着的调度相关项，没有为 `none`。
- `files` 行的 `file/run=`：启动文件和运行中的进程第一处不一致的值，格式为「项:文件里的值/运行中的值」；`serve_lines=` 是模板里 `vllm serve` 命令的条数，不是 1 条时才出现。

## 执行后会看到什么

终端打印不超过 20 行 `键=值`，几秒内结束，退出码为 0。倒数第二行是明细文件的路径，最后一行是指纹，例如：

```
detail=/tmp/t51_check_5a5e6e00b3c7004d.json
fingerprint=5a5e6e00b3c7004d code=7d293d0d config=cc332d1b
```

## 带回哪些

- **带回终端打印的全部行**（不超过 20 行）。
- **指纹相同的容器只带回一份。** 指纹由代码状态（vLLM、vllm-ascend、Mooncake、proxy 的文件哈希，加上 Python、torch、CANN 等版本）和配置（实例形态、connector、投机解码等，不含每台机器不同的 DP rank 和端口）算出，同一个镜像、同一份配置起的容器指纹相同。
- 明细文件留在容器里，不用带出。我们看了带回的行有疑问时，会请你查明细里的某一项。

## 输出里不会有的内容

不输出 IP、主机名、口令和 `/home/<用户名>`；路径只给相对安装目录的部分；改过的文件只给文件名和函数名，不给代码。脚本在打印之前按 IPv4、IPv6、本机主机名、启动文件和配置里出现的地址、口令类变量的值、`/home/` 路径逐项自查，终端输出和明细文件都查。

**只要命中一项，就什么都不写**，只打印一行，退出码为 2：

```
t51_check=1 BLOCKED self_check=host (nothing written)
```

`self_check=` 后面是命中的类别（`ipv4`、`ipv6`、`host`、`secret`、`home`；带 `detail:` 前缀的表示命中的是明细文件）。遇到这种情况，把这一行带回即可，我们按类别改脚本。

## 各行的含义（简表）

| 行首 | 内容 |
|---|---|
| `t51_check=` | 角色、CPU 架构、Python 版本、是否和 vLLM 用同一个 Python、glibc |
| `torch=` | torch、torch_npu、transformers、CANN 的版本 |
| `vllm=` | 版本串、安装方式、提交号、状态（`v0.30.0` 原版、`modified` 有改动）、改动文件数、是否已有诊断补丁、启动后才改的文件数 |
| `vllm_changes=` | 只在有改动时出现：和已知版本相同的文件数，以及改动所在的「文件~最接近的版本:函数」 |
| `ascend=`、`ascend_changes=` | vllm-ascend，同上；状态 `d4ad49796` 原版、`modified` 有改动。改动报到「文件~最接近的版本:函数」 |
| `mooncake=` | 版本（装的是 `mooncake-transfer-engine-npu` 时带 `(npu)`）、和已发布的哪个 wheel 一致（OBS `v0.3.14.rc1` 构建、PyPI `mooncake-transfer-engine` 0.3.13、PyPI `mooncake-transfer-engine-npu` 0.3.11.post1–0.3.14）、安装后被替换的库文件数、库里有没有原生计时、glog 版本、Worker 实际加载的 Mooncake 库数 |
| `build_tools=` | 容器里有哪些编译工具、缺哪些头文件、CANN 开发文件是否在 |
| `inst[...]` | 每组相同配置的实例一行：P 或 D、端口、TP、DP、DP rank、Worker 数、connector、`load_async`、失败策略、投机解码和 k、`enforce_eager`、`--async-scheduling`、`prefer_alloc_in_same_node` |
| `run` | 1 号进程是什么、由哪些启动脚本拉起、标准输出和标准错误的去向、`VLLM_LOGGING_CONFIG_PATH`、`GLOG_*`、日志分区剩余空间、runner（`d4ad49796` 不设 `VLLM_USE_V2_MODEL_RUNNER` 时用 V1，只有用 V2 时启动日志里有一行） |
| `log` | 启动日志里的 runner 行、`[PD_DIAG]` 和 `[BatchPutTiming]` 的行数 |
| `files` | 启动文件和公开示例是否一致、重定向方式、`mooncake.json` 的键数和 `protocol`、文件里的值和运行中的值是否一致 |
| `proxy` | proxy 文件是否就是 main 上那一份、改动的函数、fastapi/httpx/uvicorn 版本、`--workers` |
| `site_patches` | 现场 vllm-ascend 本地 patch 文件的 sha16，见上 |
| `verdict` | 打完补丁后的结论：vllm、ascend、p2d、mooncake 各一项，见上 |
| `export=` | 只在加了 `--export-diff` 时出现，见上一节 |
| `errors=` | 只在某一部分检测出错时出现，格式为「部分:异常类型@行号」，其余部分照常输出 |

## 对环境的影响

**会读的：**
- 运行中 vLLM 进程的 `/proc/<pid>/` 下的 `cmdline`、`stat`、`cwd` 和 `fd/0–2` 的链接目标，以及：
  - `maps`：最多 2 个 API server，每个只取 1 个 Worker；
  - `environ`：每个 API server 各一次，第一个两次（取 `MOONCAKE_CONFIG_PATH` 和日志相关的变量），每个 Worker 各一次（只看有哪些 `GLOG_*` 变量）。
  
  读 `maps`、`environ` 时内核要短暂访问目标进程的内存映射，每次读取的前后都计了时，次数和总耗时在明细的 `cost.proc_reads` 里。实际证据：11-19 现场第 1 次检测（上一版脚本）已经读过 `maps`（输出里有 `loaded=13 store_lib=…`）和各 API server 的 `environ`，没有出问题；读各 Worker 的 `environ` 是本版新加的。只读这些文本和链接，不读 `/proc/<pid>/mem`，不 ptrace，不向任何进程发信号。
- 传进来的启动文件、`mooncake.json`、vLLM 日志文件（只读前 64 MiB）、proxy 文件。
- vLLM、vllm-ascend 的 `.py` 文件（逐个哈希，比对范围内的逐字节比较）和 `__pycache__` 里 `.pyc` 的前 16 字节。
- site-packages 里的安装信息（`*.dist-info` 下的文本）和 `.pth` 文件的文本；`.pth` 里的 `import` 行不执行。
- Mooncake 包里的 `.so`：按 1 MiB 一块读，单个文件超过 1 GiB 不读，每个文件只读一遍（哈希和查找版本串一起做），`libmooncake_store` 最先读；Worker 已加载的库只读 ELF 头部和动态段。
- CANN 的版本文件、编译工具是否存在（只看文件是否存在）。
- 不读模型权重和任何 `*.safetensors`、`*.bin`；不遍历模型目录。

**会写的：** 只有 `/tmp` 下的两个新文件（`/tmp` 可能是内存文件系统，文件带回以后请删掉，命令见本节末尾）：明细 `t51_check_<指纹>.json`，和加 `--export-diff` 时的导出 `t51_glm53_diff_<指纹>.patch`。文件以「只新建、不覆盖、不跟随符号链接」的方式创建，权限 0600；同名文件已存在时换一个名字。`--out-dir` 只接受 `/tmp` 或它下面的目录，否则只打印 `REFUSED`。

文件带回以后，删掉这两个文件和拷进去的脚本（路径照抄终端里 `detail=`、`export=` 两行）：

```bash
docker exec <容器名> rm -f <detail= 那一行的路径> <export= 那一行的路径> /tmp/t51_glm53_check.py
```

**不做的：**
- 不导入 vLLM、vllm-ascend、torch、torch_npu、Mooncake，也不执行 site-packages 里的任何代码（`-S`）；包的位置是按路径查文件找到的，不调用 `importlib` 的查找函数。
- 不写 `.pyc`：`-B`，脚本开头还设了 `sys.dont_write_bytecode = True`。
- 不联网，不起子进程，不改环境变量、文件权限和时间戳。
- 不改任何配置，不碰 NPU。

**占用：**
- 开头把自己设为 `nice 19`，IO 设为空闲级（`ioprio_set`），只影响脚本自己。IO 空闲级只在 BFQ、CFQ 两种磁盘调度器下起作用；第一行的 `io=idle` 只表示这次调用成功，不保证不影响 IO。
- 读取总量上限默认 1 GiB（`--max-read-mib` 可改）。累计读到上限后，剩下的 Mooncake 二进制不再哈希，记为跳过，不当作「不同」。最后一行给出 `read=`（读了多少 MiB）、`elapsed=`（总耗时）、`slow=`（最慢的三段及秒数）、`so_skipped=`（跳过了几个二进制），各段耗时在明细的 `cost` 里。
- 不调用 `posix_fadvise` 之类的接口去清页缓存：这些 `.so` 正被 Worker 映射，清掉反而会影响运行中的服务。
- 总耗时上限默认 300 秒（`--max-seconds` 可改）；到了上限就停下，输出已经得到的结果，并多打印一行 `timeout=…`。导出另有 60 秒。

**实验室验证：** 用 `strace -f` 跟踪一次完整的检测（P 容器、有本地改动、带 `--export-diff`），结果在 `lab/results/g12_strace_report.txt`：除 `/tmp` 下这两个文件外没有任何写操作，没有网络调用，没有子进程，没有向其他进程发信号。
