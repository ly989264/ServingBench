# MooncakeConnectorV1 的 D 读 KV 埋点（glm5.3，vllm-ascend `d4ad49796`）

给 `vllm_ascend/distributed/kv_transfer/kv_p2p/mooncake_connector.py`（`MooncakeConnectorV1`）加上 D 从 P 读 KV 的诊断记录，共三种：`pd_transfer_summary` 的 `link`（`transfer_role="consumer"`）、`task`、`request_generation`。格式和同事补丁 02 给 `MooncakeHybridConnector` 写的相同。P 侧另照 02 写一条 `link`（`transfer_role="producer"`）。

glm5.3 的两段 D 侧数据由此有了来源：D 的 start_load_kv/submit 经 C2 关联；D 的 start_load_kv/wait_for_transfer 取 C6 的 envelope。

**T58 版。** 本目录现在是 T58 版：在 T56 版上加了粒度（约定 1.5.0 第 8 节），见第 3 节末尾「T58：粒度」。
- 级别取 vLLM 补丁的 `pd_diagnostics.diag_level()`，接收线程建好时读一次；取不到、不是 1/2/3 都按 3。
- 不设、3 级、2 级：写出的记录和 T56 版逐字段相同。1 级：`request_generation` 不写 4 个字段，见下。
- 锚点、判据、改动编号都和 T56 版相同：T58 只改补丁自己插进去的代码。

T56 版在 T51 版上加了等 KV 的墙钟包络和三个阶段的最大值：

- **新增的字段：**
  - `request_generation` 加 `kv_load_envelope_ns`、`kv_load_timing_scope`，以及 `task_max_queue_wait_ns`、`task_max_meta_fetch_ns`、`task_max_prepare_ns`；
  - `task` 加 `request_done_ns`。
- **和 hybrid 的关系：** 这 6 个字段 hybrid 没有，是约定 glm5.3 版 1.4.0 写明的例外（「V1 只写 hybrid 已有字段」）。其余字段都是 hybrid 版已有的，不新增取值，见第 4 节。
- **记录格式：** 以 `t51-glm53-kit/CONTRACT.zh-CN.md` 为准：T51 部分照 1.3.1 第 3.2 节和第 4 节，T56 部分照 1.4.0（`docs/tasks/T56/10_记录格式约定草案.zh-CN.md`），T58 部分照 1.5.0 第 8 节（t51-glm53-kit `23afecf`）。和约定写法不同的地方见第 4.3 节。
- **T51、T56 版的文件：** 补丁和 `replay_p2d.py` 分别放在 `t51/`、`t56/` 下，字节不变。

升级已经打过 T56（或 T51）版的现场文件，按 `field_check/操作说明A_T58_p2d补丁.zh-CN.md`：
1. 用 T51 时备份的原文件还原；
2. 用现场那一版的脚本（`t56/` 或 `t51/` 下的 `replay_p2d.py`）证明备份确是现在这份文件的原文件；
3. 用本目录的 `replay_p2d.py` 完整重放。

还没打过 p2d 的现场文件（glm53-1），直接在原文件上重放本目录的脚本，同一份说明的第 7 节。两条路径写出的文件逐字节相同。

只改这一个文件。

## 1. 文件

| 文件 | 内容 |
|---|---|
| `t58-vllm-ascend-d4ad49796-p2d-diag.patch` | 只改 `mooncake_connector.py` 的补丁：19 个片段，新增 471 行，删除 3 行（这 3 行都是在原调用外包一层，原逻辑不变）；基线 vllm-ascend `d4ad49796` |
| `files/vllm_ascend/distributed/kv_transfer/kv_p2p/mooncake_connector.py` | 替换件，即 `d4ad49796` 打完补丁后的文件 |
| `replay_p2d.py` | 在现场文件上重放 29 处改动（C01–C29，按文件中的顺序，C29 在 C14 和 C15 之间），只用标准库，`python3 -I` 可直接运行。重放前先检查 32 条结构判据，重放后再做检查，写到 `/tmp`，然后 `py_compile` 并报 sha256。输入已经打过 p2d 时退出码 6，并说明是 T51、T56 还是 T58 版。用法、判据、锚点清单见仓库的 `field_check/README.zh-CN.md`（第 5 节） |
| `t51/` | T51 版的补丁 `t51-vllm-ascend-d4ad49796-p2d-diag.patch` 和 `replay_p2d.py`（`02304c8f`），字节不变。现场是 T51 版时，升级只用 `t51/replay_p2d.py` 证明备份就是原文件 |
| `t56/` | T56 版的补丁 `t56-vllm-ascend-d4ad49796-p2d-diag.patch`（`7eeb231f`）和 `replay_p2d.py`（`9b12baa6`），字节不变。现场是 T56 版时（glm53-test），升级只用 `t56/replay_p2d.py` 证明备份就是原文件 |
| `tests/test_mooncake_connector_pd_diag.py` | V1 的埋点测试，69 个（T51 的 35 个，T56 的 11 个，含 16 个对端的多线程走查，T58 的 23 个），放到 `tests/ut/distributed/kv_transfer/kv_p2p/` 下运行 |
| `SHA256SUMS.before` | `d4ad49796` 原文件的 sha256，用来判断现场文件有没有本地修改 |
| `SHA256SUMS` | 本目录全部交付文件的 sha256 |

| 文件 | sha256 |
|---|---|
| `mooncake_connector.py`，`d4ad49796` 原文件 | `4e476a50afb5a7f13c274c071efc909ee823577383843b04e952bbdad21d1453` |
| 替换件（即打完补丁后），T58 版 | `01ce3b2a7dd5cdb10b630daa43c805d72f7d518407494d5199559ee78072c1de` |
| 替换件，T56 版（`t56/` 的补丁打出的） | `256b335e06f9c5b6b2cc686dac1432adecf1c67aa4396e652c60355dc2d71d76` |
| 替换件，T51 版（`t51/` 的补丁打出的） | `f953b27c3e53c125e6d931bb35d698b5e39662485b972e056447ef9b09a61909` |

## 2. 依赖

- **vLLM 侧的 `vllm.v1.metrics.pd_diagnostics`**（补丁 01 新增的模块，glm5.3 上由别的 worker 移植到 v0.30.0）。connector 在模块顶层 `from vllm.v1.metrics import pd_diagnostics as diag`，vLLM 侧没有这个模块时，导入就失败、实例起不来，这一点和补丁 02 对 hybrid 的要求相同。用到的接口都是 02 的 hybrid 代码原来就用的：`new_id`、`current`、`scope`、`collect`、`emit`，以及 `Batch` 的构造函数和 `update`、`add_stage`、`add_link`、`finish`。测试在两棵 vLLM 树上都跑过：t51-glm53-vllm `d3e84e2` 的整棵 v0.30.0 移植树（`pd_diagnostics.py` 为 blob `5d465d1e`），以及只加补丁 01 中 `pd_diagnostics.py` 的 v0.30.0。
- **`SchedulerOutput.pd_execution_id`**（补丁 01 在 `scheduler.py` 里加的步骤号），C2 关联要靠它。用 `getattr` 读取，属性缺失时 `execution_id` 记为 `null`，调度照常进行。统筹已定保留这个写法：诊断代码在任何情况下都不能让调度出错。由 vLLM 侧保证这个属性每步都有、每步唯一。
- **Worker 当前的诊断上下文。** `task` 记录除了本文件写的字段，还带上 `diag.current()` 里已有的字段。这些字段由 vLLM 侧 runner 的移植设置，机制和 hybrid 相同。
- **不依赖 `store/diagnostics.py`。** 补丁 02 的 hybrid 部分也不用它。
- vllm-ascend 的其他文件不需要跟着改。`ReqMeta` 新增的 `pd_execution_id` 只在同一实例的 EngineCore 和 Worker 之间传递，P、D 之间的协议（`kv_transfer_params`）不变，所以 P、D 可以分开更新。

## 3. 计时点和对应的段

行号指 `d4ad49796` 的原文件。

| # | 位置 | 进程 · 线程 | 记什么 | 写进哪里 | 对应的段 |
|---|---|---|---|---|---|
| 1 | `MooncakeConnectorScheduler.build_connector_meta`，循环里 `add_new_req` 之后（:1989–1995） | D 的 EngineCore · 主循环 | `ReqMeta.pd_execution_id = scheduler_output.pd_execution_id`，并为这一步每个要从 P 读 KV 的请求写一条映射 | `link`/`consumer`：`request_id` R、`execution_id` X、`engine_id`、`dp_rank`、`links=[producer: P 请求号, P engine_id]` | D 的 start_load_kv/submit：按 C2，用 X 把首次候选那一步计给 R；submit 的时长取 vLLM 侧 `worker_summary[execute].stages.submit`（不在本文件） |
| 2 | `MooncakeConnectorWorker.start_load_kv` 调 `kv_recv_thread.add_request`（:3905） | D 的 Worker · 主线程 | 传入 X 和这个读任务的序号 `pd_offset` | `task.execution_id`、`task.offset` | 核对 C2：`task.execution_id` 等于 `link.execution_id` |
| 3 | `KVCacheRecvingThread.add_request`（:606 之后） | D 的 Worker · 主线程 | 生成读任务身份，入队时刻 | `task.queued_ns`，以及身份字段 | — |
| 4 | `_submit_request`（:651） | 接收线程 | 分派给按对端的处理队列的时刻 | `task.submitted_ns` | — |
| 5 | `_handle_request`（:720） | 线程池里的处理线程 | 开始处理的时刻 | `task.start_ns`、`task.queue_wait_ns`（开始处理减入队） | — |
| 6 | 远端元数据（:807–823），未缓存时向 P 请求（:814） | 同上 | 两段时长，含 ZMQ 收发的重试等待 | `task.associated_stages.meta_fetch`、`metadata_request` | 不进段 |
| 7 | 生成读取描述符（:826–1030） | 同上 | 时长 | `task.associated_stages.prepare` | 不进段 |
| 8 | `batch_transfer_sync_read`（:1042），改为经 `_transfer_batch` 调用 | 同上，Mooncake 在这个线程里同步执行 | 这次 READ 的起止、返回值、字节数 | `task.stages.wait_for_transfer`、`task.native_calls[]`；请求级的最早开始和最晚结束 | D 的 start_load_kv/wait_for_transfer（C6）：`request_generation.transfer_envelope_ns` |
| 9 | 一个请求的全部读任务结束后做 reformat（:750） | 最后结束的那个任务的线程 | 时长 | `task.associated_stages.reformat` | 不进段 |
| 10 | `_handle_request` 结束（完成信号发出之后） | 同上 | 结束时刻、状态 | `task` 记录写出：`end_ns`、`processing_duration_ns`、`status` | — |
| 11 | 同一请求最后一个读任务结束 | 同上 | 请求级汇总 | `request_generation`：`transfer_envelope_ns`、任务数和字节数、`status`；`execution_id` 固定为 `null` | 同 8 |
| 12 | `MooncakeConnectorScheduler.request_finished`，`FINISHED_LENGTH_CAPPED` 且 `do_remote_decode` 的分支（:2052 之后） | P 的 EngineCore | 交给 D 的请求 | `link`/`producer`：`status="offered"`、`delayed_free` | 不进段；用于对照 P、D 两边的请求号 |
| 13（T56） | `_handle_request` 里 `self.task_tracker.update_done_task_count(request_id)` 之后（:761，C29） | 使请求完成的那个任务的线程 | 请求被标记完成的时刻，即 `get_finished` 从这一刻起可以把请求报给调度器 | 这个任务的 `task.request_done_ns`；请求汇总 | D 的等 KV（墙钟）：`request_generation.kv_load_envelope_ns` |
| 14（T56） | `_pd_finish_task` 把任务并进请求汇总处（补丁自己的代码） | 每个任务的线程 | 本任务的 `queued_ns`、`queue_wait_ns`、`associated_stages.meta_fetch/prepare.duration_ns` | 汇总：最早的 `queued_ns`；三项各自的最大值 | `request_generation.task_max_queue_wait_ns`、`task_max_meta_fetch_ns`、`task_max_prepare_ns`：D start_load_kv 下的「接收排队」「取对端信息」「组读表」（T56 定稿 F3） |

**envelope 的口径（C6）。** V1 每个读任务只调用一次 READ。一个请求在一个 TP rank 上有「shard 数 × 每个 shard 的对端数」个读任务，`offset` 按这两层循环的顺序编号。`transfer_envelope_ns` 取这个 rank 上、这个请求所有 READ 中最早的开始到最晚的结束，不是各次之和。READ 之间的间隔算在里面，比如前一个任务的完成信号、任务之间的等待；第一次 READ 之前和最后一次 READ 之后的时间不算，比如取元数据、拼描述符、reformat、最后的完成信号。用受控时钟的测试逐个验证了这些数值是否精确（第 6 节）：

- 多个 remote_tp_offset、多个 shard、两者组合；
- 中途有一次 READ 返回 -1；
- 中途有一次 READ 抛异常。

**T56：等 KV 的墙钟包络和三个最大值（约定 1.4.0）。**
- **起点和终点：**
  - `kv_load_envelope_ns` = 请求被标记完成的时刻（第 13 行）− 本请求所有任务中最早的 `queued_ns`（第 3 行）。
  - 同一进程同一时钟（D Worker 的 `monotonic_ns`）上的差值，不跨进程。
  - 终点在给 P 发完成信号之前，所以不含那一次 ZMQ 往返；各任务的 `end_ns` 含这一段。
  - `kv_load_timing_scope` 固定为 `"first_task_queued_to_request_marked_done"`。
- **`request_done_ns`：**
  - 只在使请求完成的那条 task（`tracker_completed=true`）上有值，其余 task 写 `null`，照约定的惯例。
  - 有了它，包络可以从 task 记录复算：最早的 `queued_ns` 加上这个时刻。
- **三个最大值：**
  - 本请求所有任务里 `queue_wait_ns`、`associated_stages.meta_fetch.duration_ns`、`associated_stages.prepare.duration_ns` 各自的最大值；
  - 没有任何任务有这一项时（例如全前缀命中，不取对端信息、不组读表）为 `null`。
  - `meta_fetch` 已经包含 `metadata_request`（:807–823 里嵌着 :814），不另外加。
- **同一把锁：** 最早的 `queued_ns`、`request_done_ns` 和三个最大值，都在 `_pd_finish_task` 里并进汇总，和任务计数、字节数在同一个 `with self.request_task_counts_lock:` 块里。`request_generation` 在最后一个任务并完以后才写出，所以写出时这几项已经齐了。
- **请求级的原始时刻不输出**，只输出差值，和 `transfer_envelope_ns` 的做法相同。
- **三条关系**（每个请求都成立，测试逐条核对）：
  - `kv_load_envelope_ns` ≥ `transfer_envelope_ns`；
  - 不小于任何一个任务的 `queue_wait_ns` + 取对端信息 + 组读表 + `wait_for_transfer` 之和；
  - ≤ 最晚的 `end_ns` − 最早的 `queued_ns`。
  - 三个最大值都 ≤ 包络。
- **失败和跳过：**
  - 读失败、跳过、reformat 失败时，请求照常被标记完成（:761 不分成败），所以包络照样有值；
  - 本连接器没有标记完成这个请求时（例如最后一个任务没有入队），没有 `request_generation`，或包络为 `null`，不推算。
- **只在 D 上有：** 这几项只由 D 的接收线程写；P 不写 task 和 request_generation。

**T58：粒度（约定 1.5.0 第 8 节）。**
- **级别从哪来：** `KVCacheRecvingThread.__init__` 调一次 `_pd_diag_level()`，存为 `self.pd_diag_level`。它调 vLLM 补丁的 `pd_diagnostics.diag_level()`（文件顶层已经导入的 `diag` 模块）；没有这个函数（例如 T56 的 vLLM 补丁）、返回值不是整数 1、2、3（`True`、`"2"`、`2.0` 都不算）、或者调用出错，都按 3。改级别要重启进程。
- **1 级不写的：** `request_generation` 的 `task_max_queue_wait_ns`、`task_max_meta_fetch_ns`、`task_max_prepare_ns`、`transfer_envelope_ns`。接收线程也不再把它们并进请求汇总：不算三个最大值（第 14 行），不合并请求级的 READ 起止（第 8 行的「请求级的最早开始和最晚结束」）。
- **1 级照写的：** `kv_load_envelope_ns`、`kv_load_timing_scope`，以及 `task`、`link` 记录的全部字段。每个任务自己的 `queue_wait_ns`、`associated_stages`、`wait_for_transfer`、task 级的 `transfer_envelope_ns` 都在 task 记录里，所以这些计时在 1 级照记，候选映射要用。
- **2、3 级：** 和 T56 版逐字段相同，字段顺序也相同。p2d 没有 L3 的字段，所以 2 级和 3 级写的一样。
- **记录大小（1 级）：** `request_generation` 从约 1260 字节变为约 1151–1154 字节，每个 D 请求少 109–116 字节；task 和 link 不变（`results/12_record_bytes_level1.json`，条件同下面的「记录大小」）。

**读取失败（`kv_load_failure_policy=fail`）。**
- 出错的读任务记 `status="failed"`。READ 返回值小于 0 时，`error_type` 是 V1 随后抛出的 `RuntimeError`；READ 本身抛异常时是那个异常的类名。
- 同一请求后面的读任务不再读，记 `status="skipped"`、`transfer_status="not_submitted"`、`error_type=null`。
- `request_generation` 记 `failed`。envelope 截止到最后一次实际执行的 READ，也就是失败的那一次。
- `fail` 策略本身在 vLLM 调度器里按 connector 上报的无效块处理，不影响这里的记录；connector 的上报行为不变。
- 从 connector 到调度器的整条路径有测试，调用的是 vLLM v0.30 真实的 `Scheduler._handle_invalid_blocks` 和 `Scheduler.finish_requests`，调度器状态用桩：
  - READ 失败后，connector 报出这个请求已接收，块号进入无效块集合；
  - `fail` 策略下，这些块让请求以 `FINISHED_ERROR` 结束，结束原因是 `FinishReason.ERROR`；
  - `recompute` 策略下，请求被标记为重算，不报错。
  - 两种策略下 connector 写出的记录相同。

**诊断代码出错时不影响业务。**
- `_pd_finish_task` 在 `_handle_request` 的 `finally` 里调用，函数体整段包在 `try/except Exception` 里，取请求汇总用 `.get()`，所以它不会盖住正在传播的异常。
- 调度端的两条 link 记录（consumer、producer）各自包在 `try/except Exception` 里。
- `CancelledError` 按类名判断，和补丁 01 相同。
- 收尾时还没关闭的附属阶段，是否记成异常，由调用处根据任务状态传入。

**记录大小。** 用接近现场的字段长度量了 D 每个请求写出的 `[PD_DIAG]` 消息字节数，包括 consumer link、各读任务的 task 和 request_generation（`results/12_record_bytes.json`）：

| 每个 D 请求（T56 版） | 这一步调度了 0 个请求 | 64 个 | 256 个 |
|---|---|---|---|
| 1 个读任务 | 5049 | 8700 | 19648 |
| 2 个读任务 | 7987 | 15280 | 37168 |

T51 版在同样条件下是 4840、8491、19439 和 7748、15042、36930。T56 每个请求多出 209 或 239 字节：`request_generation` 多了包络和三个最大值，每条 task 多了 `request_done_ns`。

- **测量条件。**
  - 字段长度：请求号 54 字符，取 vLLM v0.30 在 D 上的形式；host 20 字符；pid、tid 各 7 位；一次 READ 2 ms，块长 1 MiB。
  - 写记录时的诊断上下文照 t51-glm53-vllm `d3e84e2` 设置。
- **哪一项随批大小变。** link 约 867 字节，request_generation 约 1260 字节，都是固定的（T51 版为约 1089 字节）。task 记录照 hybrid 的做法，带上 `start_load_kv` 那一步 Worker 上下文里的字段，其中 `request_ids` 是这一步调度到的全部请求，所以这一步每多调度一个请求，每条 task 记录约多 57 字节。
- **统计范围。** 算上 vLLM 日志前缀（`INFO 月-日 时:分:秒 [pd_diagnostics.py:214] `）和换行，每条记录约多 45 字节；多进程启动时进程名前缀没有计入。

**重试。**
- connector 不重试 READ：一次 READ 失败就是最终结果。
- 代码里只有 ZMQ 收发会重试（`ensure_zmq_send`、`ensure_zmq_recv`，最多 3 次，每次间隔 0.1 s），用在两个地方：向 P 取元数据，等待时间计入 `associated_stages.metadata_request` 和 `meta_fetch`；发完成信号，计入 `processing_duration_ns`。两处都不进 envelope。
- hybrid 没有记录重试次数的字段，这里也不加；原有的 WARNING 日志照常输出。
- proxy 把请求重发给 D 时，vLLM v0.30 会给每个到达的请求号追加 8 位随机字符（`input_processor.assign_request_id`）。所以重试在 D 上是一个新请求，有自己的 link、task 和 `request_generation`，不会和上一次混在一起。例外是设了 `VLLM_DISABLE_REQUEST_ID_RANDOMIZATION`。

**TP 大于 1。**
- 每个 TP rank 的接收线程各自写 `task` 和 `request_generation`，记录带 `tp_rank`，各 rank 的 `request_generation` 号不同。
- 按 C3，过滤器只用 `tp_rank` 为 0 的记录，和 hybrid 相同。
- EngineCore 写的 `link` 没有 `tp_rank`。
- glm5.3 的 D 每实例 TP1，所以 D 上这些记录的 `tp_rank` 都是 0。

**`do_virtual` 请求。** D 请求的 `kv_transfer_params` 里带 `do_virtual: true` 时，Worker 不读 KV，直接把请求标为已接收。这条路径由 vllm-ascend PR #13669 引入，用于虚拟执行。P 的 `request_finished` 和 vllm-ascend 自带的 proxy 示例都不会设置这个字段，只有现场的 proxy 或客户端自己加上时才会出现。这类请求照常有 consumer link，没有 `task` 和 `request_generation`，所以它的 D wait_for_transfer 为空。

**进入调度前就被拒绝的请求。** 比如 prompt 加 `max_tokens` 超过 `max_model_len`，`request_finished` 会补一个空的读取，让 P 释放块。这种请求下一步有 consumer link，读任务记 `skipped`。

**P 侧为什么只写这一条。** 补丁 02 对 hybrid 的 producer 只在 `request_finished` 写这条 offer，P 的发送线程 `KVCacheSendingThread` 不埋点。这里照做，两套环境写出的记录种类就一致。v3 和 C2 都不用这条记录。

## 4. 和 hybrid 版的差别

### 4.1 记录格式：没有差别

| 核对 | 做法 | 结果 |
|---|---|---|
| 配对场景逐字段对比 | 在 RC1（`24487f4f`）加补丁 02 的 hybrid 和 `d4ad49796` 加本补丁的 V1 上跑同样的 5 个场景：consumer link、producer offer，以及读取成功、全前缀命中、READ 返回 -1 三种情况下的 `task` 和 `request_generation`。两边各 8 条记录，逐字段比较 | 398 个字段路径和类型完全相同，其中 241 个按值比较的字段也完全相同。不按值比较的有：id 类（记录号、任务号等）、`*_ns` 时间、host/pid/tid，以及取决于测试块布局的字节数和段数，这些只比类型 |
| 字段子集 | 收集 V1 在全部 35 个测试加上配对场景里写出的所有记录（122 条），与 hybrid 在 14 个场景里写出的记录（29 条）比较。hybrid 的场景覆盖成功、READ 失败、READ 抛异常、READ 被取消、全前缀命中、描述符为空、元数据请求成功和失败、reformat 成功和失败、两个任务的请求 | V1 的每个字段路径都在 hybrid 里出现；`status`、`transfer_status`、各阶段的 `status` 等 27 类枚举字段，V1 的每个取值也都在 hybrid 里出现。多出的路径和取值共 0 处 |

对比脚本在仓库的 `tools/compare/`，原始记录和结果在 `results/08_*`、`results/10_*`。

### 4.2 行为上的差别

以下各条都是 V1 自身的代码路径，hybrid 里没有对应的情况；字段和取值都沿用 hybrid 已有的。

| # | 情况 | V1 怎么记 | hybrid |
|---|---|---|---|
| 1 | 同一请求的某个读任务失败后，后面的读任务不再读（V1 原有的 `failed_recv_requests` 逻辑） | 后面的任务记 `status="skipped"`、`transfer_status="not_submitted"`、`native_calls=[]`、`stages.wait_for_transfer` 的 `calls=0`、`status="missing"`、`transfer_envelope_ns=null`；`request_generation` 记 `failed`，`failed_tasks` 和 `skipped_tasks` 分别计数 | 没有这条路径，每个任务都会读 |
| 2 | reformat | 一个请求的全部读任务结束后统一做一次，由最后结束的任务执行，所以 `associated_stages.reformat` 只出现在这个任务上；只要这个请求暂存了 reformat 信息就计时，最后没有实际重排时时长接近 0。reformat 失败时，这个任务记 `failed`，`error_type` 为异常类名 | 每个任务读完后各自 reformat，只在需要 NZ 或 cat 时计时 |
| 3 | 读取描述符为空（`src_list` 为空，V1 原代码直接返回） | `transfer_status="no_work"`、`status="skipped"`，没有 `native_calls`，`stages.wait_for_transfer` 的 `calls=0`、`status="skipped"` | 空列表也调用一次 READ，记一条 `no_work` 的 `native_calls` |
| 4 | `do_virtual` 请求（V1 独有） | 只写 consumer link | 没有这类请求 |
| 5 | `offset` | 新增可选参数 `pd_offset`，取 (shard_idx, remote_tp_offset) 展平后的序号 0..N−1；`final_task` 为真的是最后一个 | `start_load_kv` 循环变量 `i`，含义相同 |
| 6 | 原有的 INFO 日志「KV cache transfer for request … took … ms」 | 保留，现场原有日志不变 | 02 删掉了这一行 |
| 7 | 怎样判断「被取消」 | 按异常的类名是不是 `CancelledError` 判断，和补丁 01 相同（统筹定）。`asyncio.CancelledError` 两边都记 `cancelled`；类名也叫 `CancelledError`、但不是 asyncio 那个类的异常，V1 记 `cancelled` | 用 `isinstance(exc, asyncio.CancelledError)`，后一种情况记 `failed` |

`stages.wait_for_transfer` 在没有 READ 时：代码以 `status="unmeasured"` 调用 `add_stage`，`Batch.add_stage` 把它写成 `status="missing"`（`unmeasured_calls=1`），hybrid 相同。所以记录里看到的是 `missing`，不会出现 `unmeasured`。

### 4.3 和约定写法不同、写出的记录相同的地方（统筹已定：都保留，约定一侧改写法）

| # | 约定 1.3.1 的写法 | 现在的实现 | 原因 |
|---|---|---|---|
| 1 | 4.1 节：`with diag.scope(execution_id=meta.pd_execution_id):` 包住 `add_request` | 给 `add_request` 加可选参数 `pd_execution_id`，不传时退回 `diag.current()` 的 `execution_id`，所以用 scope 的写法照样有效（有测试） | 不用整体缩进那 18 行调用，便于重放；`offset` 本来就要加可选参数 |
| 2 | 3.2 节：直接取 `scheduler_output.pd_execution_id` | `getattr(scheduler_output, "pd_execution_id", None)`；`start_load_kv` 里也用 `getattr(meta, ...)` | vLLM 侧缺这个属性时调度不出错，`execution_id` 记 `null`。vLLM 侧保证有这个属性时，两种写法行为相同 |
| 3 | 4.2 节：`_handle_request` 的整个函数体包在 `with` 里 | `_handle_request` 持有 `with diag.scope(...), diag.collect(...)`，原函数体原样移到新方法 `_handle_request_body` | 原函数体不改缩进，便于重放；`finish` 仍在完成信号发出之后，`processing_duration_ns` 口径不变 |
| 4 | — | 队列项没有 `_pd_diag` 时（例如测试直接构造的字典），按原逻辑传输、不写记录；hybrid 会 `KeyError` | upstream 的 kv_p2p 测试因此不用改就能通过 |

### 4.4 同事补丁 02 自带的测试

同事补丁 02 自带的 hybrid 测试在它自己的目标版本（RC1 加 02）上跑不过：pytest 报 8 failed、16 passed（失败数含子用例）。

- 其中 3 个是 `test_group_transfer_and_completion` 的三个子用例：它们断言 `task["stages"]["submit"]`，但代码并不写这个阶段，报 `KeyError`。
- 另外 5 个是调度端用例（2 个测试，加 1 个测试的 3 个子用例）：它们的桩没有 `vllm_config`，而 02 在 `request_finished` 里要读它，报 `AttributeError`。

两类都是测试本身的问题，生产代码的 `__init__` 会设置 `vllm_config`。上面的对比直接运行 02 的代码，不依赖这些测试（`results/07_*`）。

## 5. 在现场打补丁

现场的 vllm-ascend 带本地修改。第 2 次检测显示，P、D 上这个文件的改动相同，在 `<module>`、`add_request`、`_transfer_kv_cache_all_groups` 等 13 个函数里，第一处改动在第 99–106 行，后面还有 14 处。所以先判断现场文件是不是 `d4ad49796` 的原样。以下在打补丁的容器里执行，先备份原文件。

1. 比对原文件：在 vllm-ascend 的根目录（或已安装包里 `vllm_ascend` 的上一级目录）执行 `sha256sum -c /path/to/SHA256SUMS.before`。
   - **一致**：直接用替换件覆盖，或者 `git apply /path/to/t58-vllm-ascend-d4ad49796-p2d-diag.patch`，两者结果逐字节相同。
   - **不一致**：接第 2 步。
2. 重放：`python3 -I replay_p2d.py <现场的 mooncake_connector.py> --check`，通过后去掉 `--check` 再执行一次，结果写到 `/tmp/mooncake_connector.p2d.py`（可用 `--out` 改），不改输入文件。现场文件已经打过 T51 或 T56 版时，先按 `field_check/操作说明A_T58_p2d补丁.zh-CN.md` 还原并证明，再在原文件上重放。脚本做的事、输出、退出码、32 条结构判据和锚点清单，都在仓库的 `field_check/README.zh-CN.md`。要点：
   - **结构判据先行。** 先在现场文件上检查插入的代码依赖的结构，有一条和原版不同就不写文件（退出码 3）。函数行数不同只打印，不停。
   - **锚点定位。** 29 处改动按锚点行定位，不看行号。每处先试主锚点，再依次试备选锚点；一条锚点必须恰好命中一行。都没命中时不写文件，并报出编号（退出码 2）。
   - **不依赖现场改名。** 插入的代码从现场代码里读出异常变量名和 reformat 的参数名；READ 调用用正则改写，现场改过的参数原样保留。
   - **结果检查。** 重放后检查插入的记录在预期的循环和分支里，并检查有没有多出未定义的名字（退出码 4）。写出后 `py_compile`（退出码 5），并报出 sha256。已打过补丁的输入拒绝处理（退出码 6）。
3. 退出码不为 0 时不要手工改：把 `replay_p2d.py` 的输出和对应的现场代码段带回来，调整锚点或补丁后再重放。
4. 有单元测试环境时，用写出的文件跑 `tests/test_mooncake_connector_pd_diag.py`。
5. 用写出的文件替换现场文件，重启 P、D 实例后生效。D 侧的计时点都在这个文件里；P 侧只多一条 producer link。

## 6. 验证

都在 `docker run --rm --network none` 的容器里执行，完整输出在仓库的 `results/`，复现脚本是 `tools/run_all.sh`。

测试环境：
- 镜像里有 CPU 版 torch、msgspec、pyzmq、pytest。
- vLLM 用 t51-glm53-vllm `40152b2`（T51 最终的 vLLM 补丁）的整棵 v0.30.0 树，另外用只加补丁 01 中 `pd_diagnostics.py`（`d3e84e2` 版）的 v0.30.0 对照一次。T51 版的结果是在 `d3e84e2` 树上跑的。
- hybrid 一侧（`results/07_*`，以及 08、10 里 hybrid 的记录）沿用 T51 时的结果：T56、T58 不改 hybrid 的代码。
- 这棵 vLLM 树的 `pd_diagnostics` 还没有 `diag_level()`（vLLM 侧的 T58 补丁在做），所以不设级别时按 3；1、2、3 级用 `tools/ut_harness/p2d_level.py` 把 `diag_level()` 换成返回固定值的函数来测。
- `torch_npu` 用 vllm-ascend UT 自带的 conftest 桩，`scipy` 用桩模块。

| 项 | 结果 |
|---|---|
| 在干净的 `d4ad49796` 上 `git apply --check` | 通过；打完后的文件和替换件逐字节相同（`results/05_*`） |
| upstream `tests/ut/distributed/kv_transfer/kv_p2p`，打补丁前 | 194 个通过，14 个跳过（`results/01_*`） |
| 同一目录，打补丁后，加上本补丁的测试 | 263 个通过（194 个加 69 个），14 个跳过，没有失败；只加 `pd_diagnostics.py` 的 v0.30.0 上结果相同（`results/02_*`） |
| 本补丁的测试连续跑 30 遍 | 30 遍都是 69 个全部通过，含 16 个对端的多线程走查（3 级、1 级各一个）（`results/04_*`） |
| 本补丁的测试在没打补丁的文件上跑 | 68 个失败，1 个通过；通过的那个测的是不写记录的路径（`results/03_*`） |
| 和原文件的行为对照 | 同一进程里同时加载原文件和打补丁后的文件，跑 10 个读取场景和调度端场景，11 项全部一致（`results/09_*`）。逐项比较的内容：READ 调用参数、发给 P 的完成信号、完成集合、无效块、队列和计数、失败标记、reformat 状态、connector 日志、抛出的异常、入队的内容（去掉诊断项）、调度端的元数据和 `request_finished` 返回值。做过变异自检：在原文件副本里改一行日志、删一行业务代码，有 4 个场景报出不一致 |
| 和 hybrid 逐字段对比 | 配对 8 条记录 0 差异；字段子集 0 处多出。T56 的 6 个字段单列为约定 1.4.0 允许的 V1 独有字段，不计入差异（第 4.1 节，`results/08_*`、`10_*`） |
| 重放：模拟修改 A（42 行，不碰补丁片段） | `git apply --check` 通过；重放结果和 `git apply` 结果逐字节相同 |
| 重放：模拟修改 B（A 再加两处，改在上下文行上） | `git apply` 打不上；`patch -p1` 有 2 个片段失败；重放 29 处全部定位成功；重放后的文件 263 个测试通过（`results/05_*`、`06_*`） |
| 重放：模拟修改 C（B 再改动多处锚点行本身，涉及 `<module>`、`add_request`、`_transfer_kv_cache_all_groups`、`_handle_request`、`build_connector_meta`、`request_finished`、`_submit_request`，都不改变行为） | `git apply` 打不上；`patch -p1` 有 9 个片段失败；重放 29 处全部定位成功，其中 10 处用了备选锚点，C29 用主锚点。现场改过的语句都保留下来，例如改过参数名的 READ 调用。和「改过的原文件」逐项对照全部一致；重放后的文件 274 个测试通过（194 个加 69 个，再加 11 个对照）（`results/05_*`、`06_*`） |
| 重放脚本的判据和备选锚点（`tools/test_replay_guards.py`，Python 3.11.10 和 3.9 各一遍） | 每遍 47 项全部通过，两遍共 94 项：<br>21 条备选锚点逐条强制使用都能定位，其中有 C29 的那一条；<br>4 种不影响埋点的改动照常写出，其中有 `update_done_task_count` 写成关键字参数；<br>13 种会让记录出错的改动在写文件前停下，其中有 F32 的两种；<br>锚点缺失时退出码 2；<br>重复打补丁时退出码 6，并分得出 T51、T56、T58 版；T56 版的文件加 `--check` 也是 6；<br>升级的证明步骤：T51、T56 版脚本在原文件上重放出的文件分别和 T51、T56 替换件逐字节相同。<br>所有改动都走最后一条备选锚点时，重放出的文件 274 个测试通过（`results/11_*`） |
| `--check` 只读 | 在根文件系统只读、没有临时目录的容器里，`--check` 和把脚本当模块调用都能通过 |
| 记录字节数 | 见第 3 节「记录大小」（`results/12_*`） |
| 补充核对 X1–X6（`field_check/extra_checks.py`） | 原文件、T58 替换件、模拟修改 C 都是原值，各种模拟的现场改动报在对应的项上（`results/13_*`） |
| 现场 8 行差异的核对工具（`field_check/span_diff.py`，Python 3.11.10 和 3.9） | 全部通过：原文件、T51、T56 和 T58 替换件 0 处差异；在两段内、两段外加删的行都报在对应区间；未打补丁的文件和它的重放结果报出的相同，C17 的注释被删时也相同（`results/14_*`） |

| T58：各级的记录和 T56 逐字段对比（`tools/compare/compare_levels.py`） | 同一份测试文件分别在 T56 替换件和 T58 替换件上跑，比较 43 个测试写出的全部记录（182 对，其中 18 个测试用受控时钟，逐值比较）：不设级别、3 级、2 级都是 0 差异；1 级除了每条 `request_generation` 少那 4 个字段（39 条，共 156 处）以外 0 差异；字段顺序相同。T56 对 T56 自己两遍也是 0 差异；把 3 级的结果当 1 级来核报 195 处差异，说明比较是有效的。1 级下 T51、T56 的测试有 22 个读到那 4 个字段而失败，失败原因全是这 4 个字段的 `KeyError`（`results/15_*`） |
| T58：现场的两条路径（`tools/test_upgrade_paths.py`，Python 3.11.10 和 3.9） | 模拟修改 A、B、C 各当一份现场原文件：从原文件直接重放 T58，29/29；从 T56 升级：T56 脚本重放出的文件当作现场现在的文件，T58 脚本拒绝它（退出码 6，说是 T56），T56 脚本对备份的重放和它逐字节相同，`--check` 29/29，写出的文件和直接重放的逐字节相同。C 用了 10 处备选锚点，和 T56 时相同。C 的结果上 kv_p2p 不设级别 274 个通过；1 级 252 个通过、22 个失败，失败原因同上（`results/16_*`） |

本补丁的 69 个测试（`tests/test_mooncake_connector_pd_diag.py`）覆盖下面各项，T56 的 11 个、T58 的 23 个见本节末尾：

- **映射记录。**
  - 一步两个请求各写一条 consumer link，R、X、engine_id、dp_rank、links 都正确，`schedule()` 的上下文字段保留；
  - `ReqMeta.pd_execution_id` 等于 X；缺 `pd_execution_id` 时记 `null`、不报错；
  - 读任务的 `execution_id` 等于 link 的 X。
- **P 侧 offer。** 字段正确；不交给 D 的请求不写。
- **envelope，受控时钟，数值精确。**
  - 1 个 shard、3 个对端：每次 READ 的起止、每个任务的 envelope、`wait_for_transfer`、`processing_duration_ns`、`queue_wait_ns` 都是预期的精确值；请求的 envelope 是 7020，也就是最晚结束减最早开始，不等于各次之和 6000。
  - 2 个 shard；2 个 shard 各 2 个对端，offset 依次为 0..3。
  - 中途 READ 返回 -1；中途 READ 抛异常。
  - 另有真实时钟下依次执行和并行执行的对照。
- **读取失败。**
  - 失败任务记 `failed`、`RuntimeError`、`native_return=-1`，失败字节等于提交字节；
  - 后续任务记 `skipped`，不调用 READ；
  - 业务行为不变：请求照常完成，块号进入 `get_block_ids_with_load_errors`，完成信号照常发出；
  - 元数据请求失败时记 `not_submitted`；reformat 失败；描述符为空记 `no_work`。
- **fail 策略在调度器里的结果。** connector 上报的无效块，经 vLLM v0.30 真实的 `_handle_invalid_blocks` 和 `finish_requests` 处理后：fail 策略下请求以 `FINISHED_ERROR` 结束，recompute 策略下请求被标记为重算、不报错。
- **重试。**
  - 取元数据时发送重试一次：`metadata_request` 正好 100 ms，不进 envelope；
  - 重试用完：任务失败、记 `not_submitted`，没有 READ；
  - 完成信号的接收重试一次：计入 `processing_duration_ns`，不进 envelope；
  - 失败的 READ 不重复调用；
  - proxy 重试到 D 的请求是新请求，有自己的 link、task 和 `request_generation`。
- **诊断代码出错不影响业务。**
  - 请求汇总已被弹出时，`_pd_finish_task` 不抛异常；
  - 写记录出错时，正在传播的业务异常不被替换；
  - link 记录出错时，调度结果不变；
  - READ 被取消（`asyncio.CancelledError`，以及类名同为 `CancelledError` 的异常）时记 `cancelled`；
  - 没关闭的阶段只在任务失败时记成异常。
- **TP 为 2。** 两个 rank 各写 2 条 `task` 和 1 条 `request_generation`，`tp_rank`、`task_id`、`connector_id`、`request_generation` 各自独立，`execution_id` 相同。过滤器只用 rank 0。
- **不传新参数时与原代码一致。** `add_request` 入队的内容和原代码完全相同，`offset` 为 `null`；用 `diag.scope` 提供 X 时照样记上。
- **其他路径。**
  - 全前缀命中记 `skipped`；
  - 完成信号回得慢时，`request_generation` 等两个任务都结束才写；
  - `do_virtual` 请求只写 link；
  - 没有 `_pd_diag` 的队列项照常传输、不写记录。
- **T56：包络和三个最大值**（受控时钟，数值精确；每个测试都核对第 3 节的三条关系和三个最大值）：
  - 两个任务入队相差 300 ns、开始处理前等 1000 ns：包络 4810，`transfer_envelope_ns` 3510，`request_done_ns` 只在完成请求的那条 task 上有值；
  - 任务按和入队相反的顺序完成：起点仍取最早入队的那个，终点取实际完成请求的那个；
  - 完成信号很慢：包络不含这段，task 的 `end_ns` 含；
  - 有 reformat：包络含 reformat，标记完成在 reformat 之后；
  - 中途读失败、后面的任务跳过：请求 `failed`，包络到跳过的最后一个任务标记完成为止；
  - 最后一个任务的 READ 抛异常：包络照样有值；
  - 全前缀命中：没有 READ，`task_max_meta_fetch_ns`、`task_max_prepare_ns` 为 `null`，包络只有排队；
  - 最后一个任务没有入队：不写 `request_generation`，`request_done_ns` 为 `null`；
  - 标记完成时诊断出错：传输、完成集合、完成信号都照常，只是包络为 `null`；
  - 一个对端要取元数据（重试一次，0.1 s）：`task_max_meta_fetch_ns` 取 meta_fetch 本身，不加 metadata_request；`task_max_prepare_ns` 取两个任务中较大的那个；
  - **多对端走查**（真实线程）：
    - 16 个 P 端口由 32 线程的执行器并行处理，完成信号经回环地址上真实的 ZMQ 收发 ACK，第二个请求排在第一个后面；
    - 两个请求各 16 条 task；包络不小于最慢的一次读、小于所有读之和；
    - 完成请求的那条 task，`end_ns` 比 `request_done_ns` 晚出至少一次 ACK 的等待；
    - 每个端口上，第二个请求的任务在第一个请求的任务结束以后才开始，`queue_wait_ns` 不小于前者的处理时长。
- **T58：粒度**（23 个，四个受控时钟场景：两个任务、两个 shard 三个对端、中途读失败、逆序完成）：
  - `diag_level()` 返回 1、2、3 时就是这一级；没有这个函数、返回 0、4、`"2"`、`" 2 "`、`True`、`2.0`、`"abc"`，或者调用出错，都按 3（12 个）；
  - 一个接收线程只调一次 `diag_level()`，处理两个任务也不再调；
  - 3 级、2 级写出的记录和不设级别时逐字段相同，字段顺序也相同（4 个场景）；
  - 1 级只少 `request_generation` 那 4 个字段，其余字段的值和顺序不变，`kv_load_envelope_ns` 相同；task、link 记录完全相同（4 个场景）；
  - 1 级下请求还没完成时，汇总里三个最大值和请求级 READ 起止都没有并进去，最早的 `queued_ns` 照常并进；3 级下都并进去了；
  - 16 个对端的多线程走查在 1 级下：两个请求都写出 `request_generation`，没有那 4 个字段，包络等于完成时刻减最早入队，不小于最慢的一次读、小于所有读之和；每条 task 的排队、取对端信息、组读表、传输包络都在。

## 7. 已知问题和没有验证的

- **`_pd_reformat_stage` 多拿一次锁。** 为了判断这个请求有没有暂存 reformat 信息，在最后一个任务结束时多拿一次 `pending_reformat_lock`。只在每个请求的最后一个任务上发生一次，不改。
- **V1 下 `native_calls` 至多 1 条。** 所以 `native_calls_dropped` 恒为 0，`incomplete` 恒为 false，`complete` 恒为 true。约定那边会写明。
- **task 记录的大小随批大小增长**（第 3 节「记录大小」）。原因是 task 记录照 hybrid 的做法，带上 Worker 上下文里的 `request_ids`。
- **硬件和真实传输。** 没有在昇腾卡和真实的 Mooncake TransferEngine 上运行。READ 用的是假引擎，没有测真实的读取时长和埋点开销。
- **联合检查。** 和 t51-glm53-ascend 的 10 个文件合在 `d4ad49796` 上一起跑，由 t51-glm53-ascend 做，等 vLLM 的 A2 交来后进行。本补丁已经和 t51-glm53-vllm `d3e84e2` 的 vLLM 移植树一起跑过（第 6 节）。A2 交来后，如果 `pd_diagnostics.py` 有变化，再跑一次。
- **TP 大于 1。** 只在一个进程里用两个接收线程对象模拟，不是多进程。glm5.3 的 D 是 TP1。
- **其他读取路径。** PP、DCP、PCP 大于 1、Mamba 组和 SFA replicate-k 的读取路径没有专门测，只读了代码。这些路径都经过同一个 `add_request` 和 `_transfer_batch`。
- **现场文件。** 现场文件的实际内容还没拿到，重放只在三组模拟修改上测过；现场核对见 `field_check/`。现场 `_transfer_kv_cache_all_groups` 比原版多的 8 行落在哪一段，要在现场用 `field_check/span_diff.py` 核（操作说明 A（T56）第 3 步）。
- **T56 包络的真实数值。** 包络、三个最大值在受控时钟下逐个核对过精确值，16 个对端的多线程走查（真实线程、真实 ZMQ 回 ACK、假的读引擎）核对过三条关系和同一对端的串行；昇腾上的真实数值没有测。
- **包络和等调度之间。** 包络的终点（请求被标记完成）到 Worker 下一次 `get_finished` 把它取走、再到调度器得知，这一段两份补丁都不量：包络在 Worker 进程里，「等调度」从调度器得知 KV 到齐算起（T56 定稿第二节第 1 条）。
- **T58 的级别只用替身测过。** 测试用的 vLLM 树还没有 `diag_level()`，1、2、3 级是把它换成返回固定值的函数测的；vLLM 侧的 T58 补丁交来以后，要用真的 `PD_DIAG_LEVEL` 再跑一遍 `results/15_*`。不设级别（没有这个函数）时按 3，这条在真的树上已经测过。
- **1 级省下的开销。** p2d 在 1 级只省请求一级的汇总：每个任务结束时取一次最大值、每次 READ 后合并一次起止，都在已有的锁里，微秒级，没有测。每个任务自己的计时照记，因为 task 记录各级都要写。
- **fail 策略在调度器里的那一步。** 测试调用的是 vLLM v0.30 真实的 `_handle_invalid_blocks` 和 `finish_requests`，调度器状态用桩。`update_from_output` 里把两者连起来的两行在测试里照写，没有调用 `update_from_output` 本身。
