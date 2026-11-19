# T51 dsv4.1 记录格式约定

适用范围：dsv4.1（vLLM v0.32.0 + vllm-ascend `f9c8e0bdb` + Mooncake）。补丁的组成：vLLM 用同事的补丁 01，原样；vllm-ascend 用补丁 02 移植到 `f9c8e0bdb` 的版本，另加「等 put 完成」的计时和 P 的缓存池加载映射记录；Mooncake 用补丁 04，原样。

读者：面板二 v3.1-extra（过滤器、`segments.json`、采集）。本约定只规定节点进程写出什么、哪些字段可用，不规定过滤器和看板怎么取。要改约定，先告诉统筹。

版本：2.2（2026-11-19）。修订见文末。

## 0. 总则

- **行格式不变。** 和补丁 01 相同：logger `vllm.v1.metrics.pd_diagnostics`，INFO 级，消息为 `[PD_DIAG] <JSON>`，JSON 的 `version` 为 2。每条记录都带公共字段 `event`、`version`、`host`、`pid`、`native_tid`、`run_id`、`timestamp_ns`、`record_id`（`pd_diagnostics.py` 的 `_write`）。
- **时钟。** 所有 `*_ns`（`*_wall_ns` 除外）都取本进程的 `time.monotonic_ns()`。同一台机器上的进程之间可以直接比较，跨机器不能比较。`*_wall_ns` 取 `time.time_ns()`，只出现在 `pool_task.native_calls[]` 里，用来和 Mooncake 的原生日志行对时间。
- **阶段字典的形状。** 记录里 `stages.<名字>` 的值都由 `Batch.add_stage` 生成，键固定为 `calls`、`duration_ns`、`start_ns`、`end_ns`、`status`、`exceptions`、`measured_calls`、`skipped_calls`、`unmeasured_calls`，另加各阶段自己的属性。同一条记录里同名阶段被调用多次时，`duration_ns` 是各次之和，`start_ns` 取最早，`end_ns` 取最晚。
- **`execution_id`。** 格式为 `execution-<run_id>-<EngineCore 的 pid>-<序号>`，由 EngineCore 每次 `schedule()` 生成（补丁 01，`scheduler.py:594`），经 `scheduler_output.pd_execution_id` 传给 Worker。这个字符串在整个集群里唯一，按它关联不需要另带机器、实例或 dp_rank。
- **新增只做加法。** 不新增 `event` 种类，不改已有字段的名字、类型和含义。新增的只有两处：`worker_summary` 里的一个阶段（第 1 节），和沿用 `pd_transfer_summary[scope=link]` 的一种新 `transfer_role` 取值 `"pool_load"`（3.3 节）。v3 的段只引用 `pd_transfer_summary` 的 `scope="request_generation"`，读这些日志时行为不变。

## 1. 等 put 完成（新增）

**记在哪里。** Worker 每一步的记录 `worker_summary`（`phase="execute"`）里新加一个阶段 `stages.wait_for_put`。这条记录就是现在承载 `stages.forward` 的那一条，身份字段沿用记录本身已有的：

| 字段 | 含义 |
|---|---|
| `execution_id` | 这一步的步骤号 |
| `tp_rank`、`pp_rank`、`dp_rank` | 写这条记录的 Worker |
| `request_ids` | 这一步有调度 token 的请求（`scheduler_output.num_scheduled_tokens` 的键） |
| `runner` | 实际用的 runner 类路径 |

**`stages.wait_for_put` 的内容。**

| 键 | 值 |
|---|---|
| `calls` | 1 |
| `duration_ns` | 等待时长：进入 `wait_for_requests_saved` → 返回 |
| `start_ns`、`end_ns` | 进入、返回的时刻 |
| `status` | `ok`；等待中抛出异常时为 `error` |
| `exceptions` | 0；有异常时为 1 |
| `measured_calls`、`skipped_calls`、`unmeasured_calls` | 1、0、0 |
| `released_requests` | 这一步要等的请求数，即调度器本步交来的「已释放请求」个数，可以为 0 |
| `timing_scope` | 固定为 `"released_request_save_fence"` |

**什么时候写。** 只要这一步执行了 `handle_preemptions`，并且缓存池的保存线程是非层式的 `KVCacheStoreSendingThread`，就写这个阶段。没有要等的请求时照样写，`released_requests` 为 0，`duration_ns` 只含函数进出本身，接近 0。以下情况不写：dummy run（这时没有 `worker_summary`）；实例没有缓存池保存线程；层式保存（层式保存在每步最后一层已经同步，不进这个函数）。

**测量点。** `vllm_ascend/distributed/kv_transfer/kv_pool/ascend_store/kv_transfer.py` 的 `KVCacheStoreSendingThread.wait_for_requests_saved`（`f9c8e0bdb` 第 576 行），调用链是 `worker/v2/model_runner.py:729`（V1 runner 是 `worker/v1/model_runner.py:2402`）→ `ascend_store_connector.py:341` → `pool_worker.py:1052`。等待用 1 ms 间隔轮询（`SAVE_REQUEST_POLL_INTERVAL_S = 0.001`，同文件第 453 行），所以有等待时，时长按 1 ms 左右取整，偏大最多约 1 ms。原有那行 DEBUG 日志不改。

**归属。** 等待发生在这一步 forward 之前，记给这一步的请求，即 `request_ids` 里的每个请求。按 C3 只用 `tp_rank` 为 0 的记录。

**参考取法（给 `segments.json`，由另一个 worker 定）。** 和 v3 的 `forward` 一样走累加：`event` 为 `worker_summary`，`where` 为 `{"phase": "execute"}`，`key` 为 `request_ids`，`value` 为 `stages.wait_for_put.duration_ns`，P 侧 `part` 取 `total`。

## 2. 缓存池加载任务 `pool_task[load]`（C1 用）

dsv4.1 的 P、D 都是 `load_async=true`，加载在后台线程 `KVCacheStoreRecvingThread` 里执行，每个加载任务完成时写一条 `pool_task`。格式和同事的补丁 02 完全相同。

**选择条件。** `event="pool_task"`，`scope="task"`，`operation="load"`。

**会用到的字段。**

| 字段 | 含义 |
|---|---|
| `duration_ns` | 任务执行时长：加载线程开始处理 → 处理结束（`end_ns − start_ns`） |
| `start_ns`、`end_ns` | 开始处理、处理结束的时刻 |
| `queue_enter_ns` | 任务进入加载线程队列的时刻；同步调用的任务为 `null` |
| `counters.queue_wait_ns` | 在队列里等待的时长：各次出队开始处理的时刻减去入队时刻，求和 |
| `counters.queue_entries` | 出队次数，通常为 1 |
| `status` | `succeeded`、`backend_failure`、`unknown_result`、`all_hit`、`no_task`、`exception`、`not_processed` 等 |
| `request_id`、`execution_id` | 任务只含一个请求时为该请求的号和步骤号；含多个请求时 `request_id` 为 `null`，`execution_id` 在所有请求同属一步时保留，否则为 `null` |
| `members[]` | 每个请求一项：`request_id`、`execution_id`，有时带 `observation_id`、`request_generation`、`store_job_id` |
| `member_count`、`members_dropped` | 保留的请求数、超过 128 个后丢掉的次数 |
| `tp_rank` | 写这条记录的 Worker |
| `associated_stages.backend_get` | 实际调用 Mooncake get 的时长，只作关联参考 |

**C1 的值。** `counters.queue_wait_ns + duration_ns`；没有 `counters.queue_wait_ns` 时只取 `duration_ns`。

**已知情况，供过滤器参考。** 一个加载任务可能含多个请求（`member_count > 1`），这时值怎么分给各请求，按 C1 由过滤器定。加载提交另有一条 `operation="load_submit"`、`status="submission_returned"` 的记录，v3 已经用它做 P 的 submit 段，格式不变。D 侧直接命中缓存池时，D 上也会有 `pool_task[load]`；D 的段不用它，和 v3 相同。

## 3. 首次候选那一步（C2 用）

**这一步是什么。** 请求第一次被调度器选中时，如果它的 KV 要异步取来，请求转入 `WAITING_FOR_REMOTE_KVS`，这一步对它只发起读取、不做 forward，请求本身不在这一步的 `request_ids` 里，所以 v3 归不到请求上。dsv4.1 有两种情况：

- D：从 P 读 KV（`MooncakeHybridConnector`）；
- P：命中缓存池、异步加载（`AscendStoreConnector`，`load_async=true`）。

**统筹定的做法（C2）：** 过滤器在 EngineCore 进程里，用同一进程里带「请求号 R + 步骤号 X」的映射记录，把步骤 X 的调度记录计给请求 R。D 的映射记录补丁 02 已经有（3.2 节）；P 的映射记录是本套补丁新加的（3.3 节）。两种映射记录形状相同。

### 3.1 关联用的记录和规则

| 记录 | 选择条件 | 用到的字段 | 谁写 |
|---|---|---|---|
| 映射记录 | `event="pd_transfer_summary"`，`scope="link"`，`transfer_role` 为 `"consumer"`（D）或 `"pool_load"`（P） | `request_id`、`execution_id` | EngineCore |
| 这一步的调度汇总 | `event="core_summary"`，`scope="execution"` | `execution_id`；`stages.lookup`、`stages.alloc`、`stages.other` 的 `duration_ns`；`request_ids`；`no_forward` | EngineCore |
| 请求的调度汇总 | `event="core_summary"`，`scope="request"` | `request_id`；`stages.queue.ranges[0].execution_id` | EngineCore |
| 这一步的 Worker 记录 | `event="worker_summary"`，`phase="execute"` | `execution_id`；`stages.submit.duration_ns`（connector 的 `start_load_kv` 调用 → 返回，只入队）；`tp_rank` | Worker |

映射记录在 `build_connector_meta` 里写，这个函数在同一次 `schedule()` 的末尾被调用，`execution_id` 取 `scheduler_output.pd_execution_id`，也就是这一步本身。`stages.queue.ranges[0].execution_id` 是请求第一次被选中那一步的步骤号（补丁 01 `request.py:434`，只在第一次被选中时追加一项）。两者是同一步，所以：

```
pd_transfer_summary[scope=link].execution_id
  == core_summary[scope=execution].execution_id
  == core_summary[scope=request].stages.queue.ranges[0].execution_id
  == worker_summary[phase=execute].execution_id
```

### 3.2 D：补丁 02 原样已有的记录（核对过）

`MooncakeHybridConnector` 的补丁 02 部分在 `f9c8e0bdb` 上原样打上（这部分在 `f9c8e0bdb` 和 RC1 之间没有改动），按读码，下面两种记录照常写出。

**映射记录 `pd_transfer_summary[link]`（consumer）。** EngineCore 在 `MooncakeConnectorScheduler.build_connector_meta` 里，为这一步每个要从 P 读 KV 的请求写一条（`mooncake_hybrid_connector.py`，`req_meta.pd_execution_id = scheduler_output.pd_execution_id` 之后）：

| 字段 | 值 |
|---|---|
| `event`、`scope`、`transfer_role` | `"pd_transfer_summary"`、`"link"`、`"consumer"` |
| `request_id` | D 上的请求号 R |
| `execution_id` | 这一步的步骤号 X |
| `engine_id` | `kv_transfer_config.engine_id`（dsv4.1 的 D 为 `"1"`） |
| `dp_rank` | `parallel_config.data_parallel_rank` |
| `links` | `[{"kind": "producer", "request_id": <P 上的请求号>, "engine_id": <P 的 engine_id>}]` |
| `status`、`complete` | `"linked"`、`true` |

EngineCore 进程里没有 `tp_rank`，这条记录也不带 `tp_rank`。另外，P 上同一个 connector 在请求结束时写一条 `transfer_role="producer"` 的 link 记录（`request_finished`）。它不是在首次候选那一步写的，即使带了 `execution_id`（来自写记录时的上下文），也不是 C2 要的那一步，C2 不用。

**读任务记录 `pd_transfer_summary[task]`。** D 的每个 TP rank 的读取线程，每个读任务结束时写一条，带 `request_id`、`execution_id`（同一个 X，由 `diag.scope(execution_id=meta.pd_execution_id)` 带入）、`tp_rank`、`dp_rank`、`remote_request_id`、`remote_engine_id`、`request_generation`、`task_id`、`direction="read"`。每个 rank 都写，按 C3 只用 `tp_rank` 为 0 的那条，可以用来核对 3.1 节的关联。

同一个请求所有读任务结束后，读取线程还写一条 `pd_transfer_summary[request_generation]`（v3 用它的 `transfer_envelope_ns` 做 D 的 wait_for_transfer），这条记录的 `execution_id` 按补丁 02 的设计固定为 `null`，不能用来关联步骤。

### 3.3 P：缓存池异步加载的映射记录（新增）

**写在哪里。** `vllm_ascend/distributed/kv_transfer/kv_pool/ascend_store/pool_scheduler.py`，`KVPoolScheduler.build_connector_meta` 的末尾（新方法 `_emit_load_links`），由 EngineCore 写出。

**写哪些请求。** 这一步 connector 元数据里 `load_spec.can_load` 为真的每个请求，即 Worker 这一步会对它发起缓存池加载的请求，一个请求一条。

**什么时候不写。** `load_async=false`（同步加载）和 `use_layerwise=true`（层式加载）时都不写：这两种情况下加载在调度这个请求的同一步里完成，请求本身就在那一步的 `request_ids` 里，不需要映射。dsv4.1 的 P、D 都是 `load_async=true`、未开层式，所以都会写。D 直接命中缓存池时，D 的 EngineCore 也会写这种记录。

| 字段 | 值 |
|---|---|
| `event`、`scope`、`transfer_role` | `"pd_transfer_summary"`、`"link"`、`"pool_load"` |
| `request_id` | 请求号 R |
| `execution_id` | 这一步的步骤号 X（`scheduler_output.pd_execution_id`） |
| `engine_id` | `kv_transfer_config.engine_id`（dsv4.1 的 P 为 `"0"`） |
| `dp_rank` | `parallel_config.data_parallel_rank` |
| `load_async` | `true` |
| `vllm_cached_tokens`、`kvpool_cached_tokens` | 本地已有的 token 数、缓存池命中的 token 数 |
| `links` | `[]` |
| `status`、`complete`、`incomplete` | `"linked"`、`true`、`false` |

和 3.2 节的 consumer 映射记录相比：事件名、`scope`、`request_id`、`execution_id`、`engine_id`、`dp_rank`、`status`、`complete` 的含义相同；`transfer_role` 不同；`links` 为空（没有对端）；多出 `load_async` 和两个 token 数。两种记录都由 `diag.Batch` 生成，所以同样带着 `schedule()` 的上下文字段（如 `kv_role`、`batch_shared`、`request_ids=[]`）。

写记录的代码包在 `try/except` 里，出错时什么都不写，不影响调度。

### 3.4 已知情况，供过滤器参考

- D 上通常同时有别的请求在 decode，这一步的 Worker 记录 `request_ids` 不为空，里面是那些 decode 请求，它们这一步的 forward、sampling 照常归给它们。`stages.submit` 是这一步所有新发起读取的合计；同一步里有几个请求同时开始读时，它们共用这一个值，怎么分由 C2 定。P 的缓存池加载同理。
- `stages.queue.ranges` 可能为空：请求没有经过等待队列，或者记录不完整（`stages.queue.status` 不是 `ok`）。映射记录不受影响。
- dsv4.1 的 `kv_load_failure_policy` 是 `recompute`。读取失败时，D 会在后面的步骤里本地重算，首次候选那一步的关联不受影响。
- 一个请求只有在发起异步加载的那一步有映射记录；后面等加载完成、真正被调度的步骤里，请求在 `request_ids` 里，不需要映射。

## 4. Mooncake 原生计时行 `[BatchPutTiming]`（C4 用）

**行格式。** 补丁 04 在每次 `Client::BatchPut`（`client_service.cpp`）结束时，用 glog 的 `LOG(INFO)` 写一行。消息部分是空格分隔的键值对，顺序固定：

```
[BatchPutTiming] keys=<n> bytes=<n> phase1_start_batch_put_us=<n> phase2_submit_transfers_us=<n> phase3_wait_transfers_us=<n> phase4_finalize_batch_put_us=<n> total_us=<n> timing_scope=<s> status=<ok|error> complete=true failed_keys=<n>
```

配置校验失败、还没开始时，写的是另一种短行，没有四个阶段，也没有 `total_us`：

```
[BatchPutTiming] keys=<n> bytes=<n> status=error complete=false timing_scope=prefer_same_node_not_started phase_status=not_started
```

`BatchUpsert` 写的是 `[BatchUpsertTiming]`，格式相同。dsv4.1 的保存路径调用的是 `batch_put_from_multi_buffers`，对应 `BatchPut`。

glog 在消息前加前缀 `Lmmdd hh:mm:ss.uuuuuu <线程号> <文件>:<行号>] `：`L` 是级别（这里是 `I`），时间是本地时间、精确到微秒、不带年份，`<线程号>` 见下文「待检测」。

**`timing_scope` 的取值和 C4 的对应。** 以代码为准。补丁 04 自带的 README 表格里写的 `phase1_only`、`prefer_same_node_remaining` 在代码里不出现。

| `timing_scope` | 什么时候 | submit / wait_for_transfer / finalize |
|---|---|---|
| `normal_full` | `prefer_alloc_in_same_node` 关 | `phase2_submit_transfers_us` / `phase3_wait_transfers_us` / `phase4_finalize_batch_put_us` |
| `prefer_same_node_full` | `prefer_alloc_in_same_node` 开 | 同上；按 C4，合计只含这三段，不含 phase1 |
| `prefer_same_node_not_started` | 配置校验失败 | 三段都没有，段为空 |

单位是微秒，取整到整数微秒。

**和保存任务记录的关联。** 一次 `batch_put_from_multi_buffers` 调用对应保存任务 `pool_task[save]`（`event="pool_task"`，`scope="task"`，`operation="save"`）里 `native_calls[]` 的一项，这一项 `operation="mooncake_put"`（补丁 02，`backend/mooncake_backend.py` 的 `put`）。关联用的字段：

| `native_calls[]` 的字段 | 对应 `[BatchPutTiming]` 行的 |
|---|---|
| `native_tid` | glog 前缀里的线程号（前提见「待检测」） |
| `start_wall_ns`、`end_wall_ns`，加 `wall_utc_offset_seconds` 换成本地时间 | glog 前缀里的时间：这一行在调用返回之前写出，所以时间落在 `[start_wall_ns, end_wall_ns]` 之内 |
| `submitted_keys` | `keys` |
| `submitted_bytes` | `bytes`，应当相等，没在实际环境里核对过 |
| `status` | `status`，供核对 |

保存任务记录本身的 `tp_rank` 和 `members[]`（`request_id`、`execution_id`、`store_job_id`）不变，关联到行以后，行的三段就归到这个保存任务的请求上。`native_calls[]` 每个任务最多保留 64 项，超过时 `native_calls_dropped` 大于 0，这些调用关联不上。

**待检测。**
- **这些行落到哪个文件。** glog 不经过 Python logging，v3 的进程内过滤器拿不到。Mooncake 的 Python 绑定不调用 `InitGoogleLogging`（v0.3.13 源码里只有 master 和独立客户端调用），在这种情况下 glog 把各级日志写到进程的标准错误；但同一进程里别的库可能先初始化了 glog，改变去向。实际去向，以及标准错误被启动脚本重定向到哪里，等环境检测结果（检测项「运行方式」）。
- **线程号是否等于 `native_tid`。** glog 0.6 及以前，前缀里的线程号是 Linux 的 `gettid()`，和 Python 的 `threading.get_native_id()` 相同；glog 0.7 起改为 `std::this_thread::get_id()`，和 `native_tid` 不同（glog 源码 `src/logging.cc`，v0.6.0 用 `GetTID()`，v0.7.0 用 `std::this_thread::get_id()`）。vllm-ascend `f9c8e0bdb` 的 Dockerfile 装的 Mooncake 是华为 OBS 上 `v0.3.14.rc1` 目录里的 wheel（版本串仍是 0.3.13），这个 wheel 自带 `mooncake_transfer_engine.libs/libglog-bb9621e6.so.0.4.0`，即 glog 0.4.0，线程号与 `native_tid` 相同（由 wheel 的 RECORD 读出；实验室里装上这个 wheel、走 TCP 做 `put_batch`，glog 前缀里的线程号等于调用线程的 `threading.get_native_id()`，见 `patches/mooncake/README.zh-CN.md` 第 6 节）。现场是否就是这个 wheel，以检测脚本的 `mooncake=` 行为准（`nearest`、`glog` 两项）。如果不相等，只能按「同一文件里时间落在调用区间内、`keys` 相等」关联。

## 5. 和同事补丁 02 原有格式的差异

目标是零差异，下面逐条列出。

| # | 差异 | 影响 |
|---|---|---|
| 1 | `worker_summary[phase=execute]` 新增 `stages.wait_for_put`（第 1 节） | 只加字段。v3 读到的 `stages.forward`、`stages.sampling` 等不变 |
| 2 | EngineCore 新写 `pd_transfer_summary[scope=link]`、`transfer_role="pool_load"` 的记录（3.3 节） | 新的行，事件名和 `scope` 沿用已有的；v3 不引用 `scope="link"`，不受影响 |

`f9c8e0bdb` 和补丁的目标版本 RC1 在保存路径上都用 `add_save_batch`：一次把一批请求放进保存队列，后面跟一个批次结束标记（RC1 `kv_transfer.py:560`）。两版只差等待保存完成的函数：RC1 是 `pool_worker.py` 的 `wait_for_previous_save`（等上一整批），`f9c8e0bdb` 是 `kv_transfer.py` 的 `wait_for_requests_saved`（只等被释放的请求）。`pool_task[save]` 的记录和 RC1 相同：每个请求在入队时取快照，出队时开始计时；批次结束标记不产生记录。这一点在移植完成后核对，如有任何字段差异，补进上表，并先告诉统筹。

## 修订记录

| 版本 | 日期 | 改动 |
|---|---|---|
| 1 | 11-18 | 初版 |
| 1.1 | 11-19 | 第 1 节：没有要等的请求时 `duration_ns` 接近 0，不是精确的 0。第 4 节：补上 vllm-ascend 官方镜像所用 Mooncake wheel 自带 glog 0.4.0 的事实。字段和记录格式都没有变 |
| 2 | 11-19 | 按统筹定的 C2 做法：第 3 节改写，新增 P 的缓存池异步加载映射记录（3.3 节，`transfer_role="pool_load"`）；写明 D 的 consumer 映射记录和读任务记录在本套补丁下照常写出（3.2 节）；第 0、5 节相应更新 |
| 2.1 | 11-19 | 第 4 节：glog 线程号等于 `native_tid` 一项，补上实验室实测（OBS wheel 自带的 glog 0.4.0）。记录格式没有变 |
| 2.2 | 11-19 | 第 5 节：按第二批顾问审更正对 RC1 保存路径的描述（RC1 同样是批次加结束标记，两版只差等待保存完成的函数）。结论和记录格式都没有变 |
