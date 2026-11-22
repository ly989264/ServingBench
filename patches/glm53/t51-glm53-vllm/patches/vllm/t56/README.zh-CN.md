# T56 vLLM 补丁（glm5.3，记录格式约定 1.4.0）

依据：统筹定稿 `31_统筹定稿`，W1 冻结的 `10_记录格式约定草案`（glm5.3 版 1.4.0）第 0 节第 7、8 条、第 2 节、第 5 节，提议 `01_vllm提议`。在 T51 版（现场补丁 `7e2ec359`）之上改，基线仍是干净的 vLLM `v0.30.0`（`ced6857a`）。

## 结论

- **加了什么：**
  - `core_summary[scope=request].stages.kv_ready_wait`（等调度），只在 D 上、只在走过异步加载的请求上写；
  - `pd_diagnostics.is_pd_decode(vllm_config)`，三份补丁共用的「是不是 D」：只用 vllm-ascend 自己的条件 `is_pd_decode_recompute_scheduler_enabled(vllm_config)`（顶层 `kv_role` 为 `kv_consumer`、不是 producer、recompute 开着），从 `sys.modules` 取，取不到或出异常为假（统筹 11-20 定）；
  - `_SPANS` 加 4 项（`runner.prepare_inputs`、`runner.lm_head`、`runner.draft`、`runner.bookkeeping`），给 03 的 4 个 runner 段用；
  - Worker 启动行在 vllm-ascend 给出的 `runner_stages` 非空时加 ` runner_stages=… source=…`（只有不是 D 时为空）。
- **改了哪些文件：** 25 个运行时文件里的 4 个：`scheduler.py`、`request.py`、`pd_diagnostics.py`、`worker_base.py`。vllm-ascend、Mooncake 不改；D 的 `AsyncRecomputeScheduler` 只重写 `_schedule()`，起点、提升、失败、中止、结束这几处都是继承 vLLM 的方法，`schedule()` 沿方法解析顺序调 vLLM 的包装，所以测点全在 vLLM 补丁里。
- **P 上不变：** 新代码在 P 上不写任何新内容。同一组合成调用分别驱动 T51 版和 T56 版，记录逐字段相同，其中包括在 `kv_producer` 上配了 recompute 的 P（第 4 节）。
- **等式：** `kv_ready_wait.end_ns` 等于同一步 `core_summary[scope=execution].stages.schedule.start_ns`，单测和 D 的合成调用逐轮核过。
- **开销：** 在实验室直接调调度器测（64 个并发请求，全部走异步远端 KV）。P 和 D 上，`schedule()`、`update_from_output()` 每次调用的主机时长是 T51 版的 0.99–1.00 倍，差别小于重复之间的波动，测不出开销（第 5 节）。
- `pd_diagnostics.py` 从这一版起不再与补丁 01 逐字节相同（多了 `is_pd_decode()`、`import sys` 和 `_SPANS` 的 4 项）。

## 1. 产物

| 文件 | 内容 | SHA-256 |
|---|---|---|
| `t56-vllm-v0.30.0-pd-diag.patch` | 现场补丁：干净 v0.30.0 上的完整补丁（`git diff --full-index v0.30.0 <T56> -- vllm/`），25 个文件，和 T51 版是同一组文件，+2897/−359 行 | `7959cbb716ff1018b176c973e62cade1968897281dcb0f9019d55db79618e61c` |
| `t56-vllm-from-t51.patch` | 已装 T51 版的环境升级用：只含变了的 4 个文件，+266/−3 行 | `5d7578685c525b307ea926bdd63718f04d4eddfe8dc4faf2057be5c5d4a65db9` |
| `files/vllm/...` | 装好以后的 25 个运行时文件，替换用 | — |
| `SHA256SUMS.after` | 25 个文件装好以后应有的 SHA-256；和 T51 版的 `SHA256SUMS.after` 比，只有这 4 个文件的值变了 | `9a8a55078c6293655ea5647e91d750fcdb67b542cd55c51656da8069047b53bc` |
| `SHA256SUMS.t51-changed` | 增量升级前，这 4 个文件在 T51 版里的 SHA-256（和 `../SHA256SUMS.after` 里的同名行相同） | — |
| `lab/t56-vllm-v0.30.0-pd-diag.lab-tests.patch` | 只给实验室用：T51 lab 补丁的 19 个文件，加 T56 的单测，共 20 个文件 | `bed9d3e376082f9b404e97f5ac0aca2de42c9cc6a6a45c4d5437a49a971ba28d` |

`SHA256SUMS.before`、`NEW_FILES.txt` 和 T51 版相同（`../`）。两条路径都核过（`verify/results/t56/apply_check.txt`）：
- 增量补丁打在 T51 版的 25 个文件上：打之前 4 个文件和 `SHA256SUMS.t51-changed` 相符，打完 25 个文件和 `SHA256SUMS.after` 全部相符；
- 完整补丁打在干净 v0.30.0 上，结果同样 25 个文件全部相符，和 `files/` 逐字节相同。

## 2. 改动

| 文件 | 改了什么 |
|---|---|
| `vllm/v1/metrics/pd_diagnostics.py` | `is_pd_decode(vllm_config)`：调用时从 `sys.modules` 取 `vllm_ascend.utils`，返回它的 `is_pd_decode_recompute_scheduler_enabled(vllm_config)`；模块没加载、没有这个函数或出异常时返回 `False`；导入时不 import vllm_ascend；docstring 写明它判的是配置。`_SPANS` 加 4 项；`import sys` |
| `vllm/v1/core/sched/scheduler.py` | `__init__` 的最后调一次 `is_pd_decode(vllm_config)`，存成 `self._pd_decode`，以后不再调；下面 6 处都以它为前提，为假时一轮也不开：<br>1. `_update_from_kv_xfer_finished`：请求还在 `WAITING_FOR_REMOTE_KVS`、第一次收到 `finished_recving` 时开一轮（起点）；<br>2. `_try_promote_blocked_waiting_request`：记 `promoted_ns`；<br>3. `schedule()` 包装：`_schedule()` 返回后，本次放进 running 的请求以本次的 `start_ns` 关掉（终点）；<br>4. `update_from_output`：connector 报读取失败的请求标 `load_failed`；<br>5. `finish_requests`：结束时还开着的一轮按结束原因关掉；还在等 KV、一轮也没开的，记一轮没有起点的；<br>6. `shutdown`：同 5，按中止处理。<br>类上另有 `_pd_decode = False`、`_pd_kv_ready_open = ()` 两个默认值，给不经 `__init__` 构造的调度器（vLLM 自带的测试里有） |
| `vllm/v1/request.py` | `pd_kv_ready`（只在 D 上有值）、`pd_kv_ready_transition()`；请求结束那条记录在 `queue`、`hash` 之后写 `kv_ready_wait` |
| `vllm/v1/worker/worker_base.py` | `_pd_startup_runner_stages()`：读 vllm-ascend `DeviceTiming` 的 `runner_stages`（元组）和 `stages_source`（`"default"` 或 `"env"`），名字由 03 定。`runner_stages` 非空时，在 `device_timing=` 那一行末尾加 ` runner_stages=<逗号连接> source=<stages_source>`；为空、没有这个属性或读取出错时不加，这一行和 T51 版逐字相同。03 只在不是 D 时给空元组；D 上至少列出 forward、sampling，4 段都关时也是。所以三档分得开：D 默认 `runner_stages=forward,sampling,prepare_inputs,lm_head,draft,bookkeeping source=default`；D 设 `VLLM_PD_DEVICE_STAGES=none` 时 `runner_stages=forward,sampling source=env`；不是 D 时没有后缀。P 上 `device_timing=` 仍可能是 `on` |

**「是不是 D」在 EngineCore 里取得到的前提。** vllm-ascend 的条件读进程内的 AscendConfig 单例。EngineCore 进程里这个单例由 vllm-ascend 自己先建好：它把 `EngineCoreProc.run_engine_core` 换成先调 `init_ascend_config(vllm_config)` 再启动的版本（`d4ad49796` 的 `patch/platform/patch_engine_core.py` 第 45–47 行），所以构造调度器时已经就绪。D 的调度器以 BalanceScheduler 为基类时，`BalanceScheduler.__init__` 在 `Scheduler.__init__` 之后用同一个 `vllm_config` 再建一次，取值不变。实验室驱动照这个顺序做：先用带 `recompute_scheduler_enable` 的配置建好单例，再构造调度器；只要配置里没有这个开关，balance 基类上的 D 就会被判成不是 D（实测过），现场 D 的配置里有这个开关。判断错时不报错，D 的 `core_summary[request]` 里不会有 `kv_ready_wait`。打补丁手册可以把「D 的请求记录里有 `kv_ready_wait`」列为核对项。

## 3. 口径

**两个时刻，都是 EngineCore 进程的 `time.monotonic_ns()`：**
- 起点：调度器第一次得知这个请求的 KV 已到齐（`_update_from_kv_xfer_finished` 收到 `finished_recving`，请求还在 `WAITING_FOR_REMOTE_KVS`）。不是字节到齐，中间隔着一步。
- 终点：把它放进 running 的那一次 `schedule()` 的开始，即包装里的 `start_ns`，等于同一步 `stages.schedule.start_ns`。

**`ranges` 里每一轮：** `episode`、`start_ns`、`promoted_ns`、`end_ns`、`duration_ns`、`ready_execution_id`（交回 `finished_recving` 的那一步）、`scheduled_execution_id`（运行它的那一步）、`status`、`load_failed`。最多 64 项。
- 提升和运行在同一次 `schedule()` 里时，`promoted_ns` 晚于 `end_ns`（提升发生在这次调用里面）；提升以后又等了几步时，`promoted_ns` 早于 `end_ns`。

**`status` 怎么来：**

| 情况 | 这一轮的 `status` | 起点 |
|---|---|---|
| 正常运行 | `ok` | 有 |
| 时刻倒退，差值为负 | `invalid`，`duration_ns` 为 `null` | 有 |
| 还开着时又收到一次到齐（被抢占、恢复后又走异步加载） | 旧的一轮 `missing`，新开一轮 | 有 |
| 读取失败，`policy=fail`（现场 D）：请求以 `FINISHED_ERROR` 结束 | `error`，`load_failed=true` | 没有 |
| 读取失败，`policy=recompute`，之后本地重算 | `ok`，`load_failed=true` | 有 |
| 读取失败，`policy=recompute`，connector 又发起一次异步加载 | 第一轮在下一次到齐时 `missing`，第二轮照常 | 有 |
| 在等待中被中止（等 KV 时、到齐后、提升后） | `cancelled` | 到齐以后有，之前没有 |
| shutdown 时还在等 | `cancelled` | 同上 |
| 请求以其他原因结束时还开着 | `missing` | 有 |

stage 顶层的 `start_ns`、`end_ns`、`duration_ns`、`status` 取第一轮；`calls` 是轮数；`complete` 为第一轮起点终点都有，`end_observed` 为第一轮有终点；`timing_scope` 为 `"kv_ready_seen_to_scheduled"`。阶段字典由 `Batch.add_stage` 生成，键和其他阶段相同；`add_stage` 会把没有时长的调用记成 `missing`，所以写完以后把 `status` 改回第一轮自己的值（`error`、`cancelled` 的一轮都没有终点）。

**和提议 01 第 4.1 节不同的三处（实施时细化，记录格式不变）：**
1. 提升以后、运行以前被中止：记 `cancelled`（提议写的是请求结束时一律 `missing`）。按 `finish_requests` 的结束原因取，比 `missing` 准。
2. shutdown 时还在等 KV、调度器还没得知到齐：也记一轮 `cancelled`，起点为 `null`（提议第 4 节的表就是这样写的，第 4.1 节漏了这一行）。
3. `policy=recompute` 读取失败后 connector 又发起异步加载：失败那一轮在下一次到齐时以 `missing` 关掉。现场 D 是 `policy=fail`，不走这条。

## 4. 验证

两套镜像各跑一遍 `verify/t56/run.sh`：Python 3.12.3（torch 2.13.0+cpu，transformers 5.19.0），以及贴近现场的 Python 3.11.10（torch 2.10.0+cpu，transformers 5.14.1，tokenizers 0.22.2）。同一个脚本、同样的输入，先驱动 T51 版的树（`d3156e5e29`，即现场补丁 `7e2ec359`），再驱动 T56 版的树（`87152f9876`）。结果在 `verify/results/t56/r312/`、`r311/`，汇总见各自的 `analysis.txt`（`verify/t56/analyze.sh` 生成）。两套镜像的结论相同，下表的数字两边一致。

| 项 | 做法 | 结果 |
|---|---|---|
| P 和 T51 逐字段相同 | `pd_equiv.py` 的 15 个场景（E1–E8、F1–F8，P 形态，含异步加载的 E2），比事件种类、字段集合和遮去时间、编号后的取值 | 两边各 223 条记录，差别 0 处 |
| 调度决定不变 | `ascend_equiv.py` 6 种调度器（vLLM、P 的 BalanceScheduler、原版和移植版 RecomputeScheduler 各在两种基类上），每步的调度决定和记录 | 每种 0 处差别（335 或 245 条记录） |
| D 的记录 | `d_equiv.py`：移植版 `RecomputeScheduler` / `AsyncRecomputeScheduler`，vLLM 和 balance 两种基类，顶层 `kv_consumer`，vllm-ascend 真实的 `ascend_config` 和 `utils` 判是不是 D。场景：D1 同步调度加异步远端 KV、MTP 5；D2 异步调度、batch queue 2、异步远端 KV、MTP 5（现场 D 的组合）；D3 等 KV 时中止；D4 shutdown 时一个已得知到齐、一个还没有 | 两种基类上 9 个请求都有 `kv_ready_wait`；去掉这个阶段以后，和 T51 版的 84 条记录、每步调度决定逐字段相同。逐轮核：`ok` 的轮 `end_ns` 等于 `scheduled_execution_id` 那一步的 `schedule.start_ns`、`duration_ns` 等于差值、`ready_execution_id` 那一步在前、`promoted_ns` 落在起点和那次调度结束之间，0 处不符。D3 中止的 `cancelled`、没有起点；D4 一个 `cancelled` 有起点、一个没有 |
| 配了 recompute 的 P | `d_equiv.py` 的 P1：`kv_producer`，配置里开着 `recompute_scheduler_enable`，vllm-ascend 的单例里也是开着；调度器是 vLLM 的或 BalanceScheduler，照样走异步加载 | 判为不是 D，没有 `kv_ready_wait`，和 T51 逐字段相同 |
| `is_pd_decode` 对 vllm-ascend 真实的条件 | `pd_decode_check.py`，真实的 `d4ad49796` `utils.py` 和 `init_ascend_config` | 7 种配置全部与 vllm-ascend 自己的结果相同：`kv_consumer` 加 recompute 为真；配了 recompute 的 `kv_producer`、`kv_both`、没开 recompute、没有 `kv_transfer_config`、vllm_ascend 没加载都为假 |
| 启动行 | `startup_check.py` 9 个场景（EngineCore 5 种调度器配置、Worker 4 种），T51 版的 vllm-ascend 计时文件 | 两棵树打出的行逐字相同（vllm-ascend 这边还没有 `runner_stages` 时不加后缀） |
| 单测 | 01 的 pd 测试 9 个文件、调度器和 KV connector 的上游测试 12 个文件；新加 `test_pd_kv_ready_wait.py` 39 例、`test_pd_compat.py` 2 例 | T51 和 T56 共有的 607 个用例：3.11 上结果逐个相同；3.12 上 1 例不同，见下。新加的 41 例全部通过。其余失败的 99 例和 1 个收集错误在 T51 上同样失败（环境：离线拿不到多模态模型的配置等，以及 01 自带的已知问题，同 T51 的说明） |
| 所有调度器都当成 D 跑上游测试 | 插件 `t56_d_plugin.py` 让 `is_pd_decode()` 恒为真，跑上面 12 个调度器和 KV connector 测试文件 | 403 个用例和不当成 D 时结果逐个相同；钩子确实跑到了（如 `test_scheduler.py` 里得知到齐 25 次、开始运行时关闭 18 次） |
| 打补丁 | `apply_check.txt` | 增量补丁和完整补丁都打得上，25 个文件全部对上 `SHA256SUMS.after` |
| 静态检查 | `callgraph.txt`、`api_audit_from_t51.txt`、`pyflakes_py311.txt` | 相对 T51 没有少掉任何调用点，新增的调用都是 `pd_diagnostics`、本补丁新加的函数或内置函数；API 审查只报 3 个 vLLM 里没有的名字，都在意料之中：vllm-ascend 的 `is_pd_decode_recompute_scheduler_enabled`，03 的 `runner_stages`、`stages_source`，都用 `getattr` 读；pyflakes 0 条，25 个文件在 Python 3.11.10 上编译通过 |
| 个人信息扫描 | 补丁新加的行、`verify/t56/`、本说明 | IP、主机名、个人目录 0 处 |

**单测的那 1 例。** 3.12 上 `tests/v1/engine/test_output_processor.py::test_stop_string[5-True]` 在 T56 版上失败一次（累计 logprob 不等），T51 版上通过；3.11 上两边都通过。这个测试的 logprob 测试数据用的是不设种子的 `torch.rand`（`tests/v1/engine/utils.py` 第 58 行），和 T51 说明里记的 `test_logprobs_processor` 是同一类偶发失败；本补丁不改 output processor。在 3.12 镜像里单独重跑这个测试（4 组参数）各 12 次：T51 版 48 次、T56 版 48 次全部通过（`r312/flaky_test_stop_string.txt`）。

**单测怎么覆盖统筹点名的三种情况（相等都是 `==`，不是 `<=`）：**
1. 走过异步加载的请求，`end_ns` 等于它被排上那一步的 `stages.schedule.start_ns`：同步正常路径、异步调度两步重叠、提升后又等一步、抢占后再读一次（两轮各自相等）、`policy=recompute` 两种、运行中 shutdown，共 7 例；
2. 没走过异步加载的请求不写：D 上没有远端可读、同步加载，2 例；
3. P 上什么都不写：`kv_producer` 配了 recompute、`kv_producer`、`kv_consumer` 没开 recompute、`kv_both` 开了 recompute、取不到 vllm_ascend，各在同步和异步调度下，10 例；每例断言 stages 只有 `queue`、`hash`，请求和调度器上都没有状态。

这个等式在现场取不到 `schedule.start_ns`，统筹定只在实验室单测里核，现场不查。

### 4.1 和 vllm-ascend 补丁（03）的联测

03 跑的，我核过它的结果文件（t51-glm53-ascend 提交 `51d00ab`，`ut_results/t56/with-vllm-01/`）。
- 树：v0.30.0 加 T51 现场补丁，再打本目录的增量补丁（打前 4 个文件对上 `SHA256SUMS.t51-changed`，打后 25 个文件对上 `SHA256SUMS.after`）；vllm-ascend `d4ad49796` 加 03 的 `cf72b07`（合并补丁 `d0fbb82b`），加 p2d `a9e9695`。
- 启动行的接缝（`tools/t56_joint_startup.py`，12 项全过）：本补丁真实的 `_pd_startup_log_timing` 加 03 真实的 `DeviceTiming`，整行比对。D 不设开关时 6 段 `source=default`；D 设 `none` 时 `forward,sampling source=env`；子集时只列开着的；不是 D 时没有后缀，和 T51 逐字相同；`tp_rank` 1 为 `device_timing=off`，没有后缀。另核了 4 个 `runner.*` 的映射、`is_pd_decode` 的四种情形，以及 `DeviceTiming.for_runner` 自己的接线：`kv_consumer` 的 runner 得到 6 段，`kv_producer` 的得到 0 段。
- 记录（`tools/t56_stage_records.py`，9 项全过）：经过本补丁真实的 `_SPANS`，4 段确实写进 `worker_summary`。
- 联合检查（两种基类各 135 项全过）：驱动里的 runner 不带 `vllm_config`，`is_pd_decode` 判为否，两份补丁的 T56 部分都整体不开，启动行与 T51 逐字相同。判不出来时整体退回 T51 行为，不会只开一半。

## 5. 开销

- 只在 D 上、只对走过异步加载的请求有额外工作：得知到齐、提升、开始运行时各取一次 `monotonic_ns` 并写几个属性，失败或中止时再一次；每个请求最多存 64 轮；请求结束时多写一个阶段，不新增日志行。
- P 上每次 `schedule()` 多一次对空元组的真值判断，`update_from_output` 在有读取失败时多一次布尔判断，`finish_requests` 每个结束的请求多一次布尔判断。
**实测。** 脚本 `verify/t56/bench_sched.py`（由 `verify/t56/bench.sh` 在容器里调用），结果在 `verify/results/t56/bench311/`，汇总见其中的 `summary.txt`。
- 层级：直接调 `Scheduler.schedule()` 和 `update_from_output()`，测每次调用的主机时长。不经过 EngineCore，没有模型。
- 场景：
  - 负载：64 个并发请求，每个都走异步远端 KV（mock connector，48 个 prompt token 里 32 个从远端读），输出 16 个 token，结束一个补一个，前缀缓存关闭。
  - 计时：预热 100 步后计 600 步取中位数，重复 5 次，每次重复共 2688 个请求。
  - P：vLLM 的 `Scheduler`。
  - D：类名、模块名与现场相同的 `Scheduler` 子类；测点都在 vLLM 的方法里，所以走到的是同一份新代码。vllm-ascend 的判定由替身给出：D 为真，P 为假。
  - 核对：计时跑完以后另跑 60 步，不计时，用来核对记录。T56 的 D 有 192 条请求记录，全部带 `kv_ready_wait`，状态都是 `ok`；T56 的 P 和 T51 的两种角色都没有这个阶段。
- 环境和基线：镜像 `t51g-vllmtest:py311`（Python 3.11.10），实验室 CPU，在 perflock 持锁下运行。基线是 T51 版的树（`d3156e5e29`），同一个脚本、同样的输入。

| 角色 | 调用 | T51 | T56 | T56 是 T51 的 |
|---|---|---|---|---|
| P | `schedule()` | 557.9 µs | 551.0 µs | 0.99 倍 |
| P | `update_from_output()` | 168.2 µs | 168.2 µs | 1.00 倍 |
| D | `schedule()` | 557.9 µs | 553.9 µs | 0.99 倍 |
| D | `update_from_output()` | 168.1 µs | 166.7 µs | 0.99 倍 |

表中是 5 次重复中位数的中位数。

- 结论：四项都在 0.99–1.00 倍之间。同一棵树 5 次重复之间，最大值是最小值的 1.008–1.063 倍，所以 T56 和 T51 的差别小于重复之间的波动。这个分辨率下测不出 T56 的开销，T56 略低的几项也不说明变快了。
- 这是实验室 CPU 上的主机时长，不代表现场鲲鹏上的绝对值；现场没有测。

