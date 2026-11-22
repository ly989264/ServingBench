# T51 glm5.3 记录格式约定

适用范围：glm5.3，即 vLLM v0.30.0 + vllm-ascend `d4ad49796`（现场另有本地改动）+ Mooncake。补丁的组成：

| 组件 | 内容 |
|---|---|
| vLLM | 补丁 01 移植到 v0.30.0 |
| vllm-ascend：缓存池 | 补丁 02 的缓存池部分移植到 `d4ad49796`，另加「等 put 完成」的计时（第 1 节）和缓存池异步加载的映射记录（3.3 节） |
| vllm-ascend：V1 runner | 补丁 02 在 runner 里的 forward、sampling 阶段，移到 `d4ad49796` 默认使用的 V1 runner（第 1 节） |
| vllm-ascend：P/D 传输 | `MooncakeConnectorV1`（`kv_p2p/mooncake_connector.py`）新加打点，记录格式和补丁 02 里 `MooncakeHybridConnector` 的完全相同（第 4 节） |
| Mooncake | 补丁 04；1.5.0 起是 04b：计时记录改写进文件（8.4 节） |

读者：面板二（过滤器、`segments.json`、采集）和上面三处移植。本约定只规定节点进程写出什么、哪些字段可用，不规定过滤器和看板怎么取。要改约定，先告诉统筹。

版本：glm5.3 版 1.5.1（2026-11-20），由 dsv4.1 版 2.2 改写；1.4.0 加 T56 的 D 侧内容（第 7 节），1.5.0 加 T58 的粒度开关和 Mooncake 的记录文件（第 8 节）。修订见文末。

## 0. 总则

- **行格式不变。** 和补丁 01 相同：logger `vllm.v1.metrics.pd_diagnostics`，INFO 级，消息为 `[PD_DIAG] <JSON>`，JSON 的 `version` 为 2。每条记录都带公共字段 `event`、`version`、`host`、`pid`、`native_tid`、`run_id`、`timestamp_ns`、`record_id`（`pd_diagnostics.py` 的 `_write`）。补丁 01 移植到 v0.30.0 后，这些都不变。
- **时钟。** 所有 `*_ns`（`*_wall_ns` 除外）都取本进程的 `time.monotonic_ns()`。同一台机器上的进程之间可以直接比较，跨机器不能比较。`*_wall_ns` 取 `time.time_ns()`，只出现在 `native_calls[]` 里（`pool_task` 和 `pd_transfer_summary[task]` 两种记录都有），用来和 Mooncake 的原生日志行对时间。
- **阶段字典的形状。** 记录里 `stages.<名字>` 的值都由 `Batch.add_stage` 生成，键固定为 `calls`、`duration_ns`、`start_ns`、`end_ns`、`status`、`exceptions`、`measured_calls`、`skipped_calls`、`unmeasured_calls`，另加各阶段自己的属性。同一条记录里同名阶段被调用多次时，`duration_ns` 是各次之和，`start_ns` 取最早，`end_ns` 取最晚。
- **`execution_id`。** 格式为 `execution-<run_id>-<EngineCore 的 pid>-<序号>`，由 EngineCore 每次 `schedule()` 生成（补丁 01，`vllm/v1/core/sched/scheduler.py`），经 `scheduler_output.pd_execution_id` 传给各 connector 的 `build_connector_meta` 和 Worker。这个字符串在整个集群里唯一，按它关联不需要另带机器、实例或 dp_rank。
- **新增只做加法。** 不新增 `event` 种类，不改已有字段的名字、类型和含义。和补丁 02 相比新增的只有：`worker_summary` 里的一个阶段（第 1 节）；沿用 `pd_transfer_summary[scope=link]` 的一种新 `transfer_role` 取值 `"pool_load"`（3.3 节）；`MooncakeConnectorV1` 写出和 hybrid 相同的几种记录（第 4 节）。v3 的段只引用 `pd_transfer_summary` 的 `scope="request_generation"`，读这些日志时行为不变。 1.4.0（T56）在已有的 `core_summary[request]`、`pd_transfer_summary[task]`、`pd_transfer_summary[request_generation]`、`worker_summary` 里加阶段和字段，同样只做加法，见第 7 节。
- **已有阶段的边界不动（1.4.0）。** `stages.forward`、`stages.sampling` 的主机计时和设备计时只包住目标模型调用、sampler 调用；以后加的段各是新阶段，不移动现有计时的起止点。
- **T56 的内容只在 D 写（1.4.0）。** 新代码装到 P 上时，写出的记录和本约定 1.3.3 逐字段相同。「是不是 D」只用 `pd_diagnostics.is_pd_decode(vllm_config)` 这一个函数判断（vllm-ascend 的 utils 条件），规则见 7.1。
- **不再新增字段。** 从本版起，不再新增任何字段，除非写明它的核对用途；新增之前先报统筹。本套补丁已经加的字段，都在所在的表里写了核对用途。
- **两种 connector，同一种格式。** dsv4.1 的 D 用 `MooncakeHybridConnector` 从 P 读 KV，glm5.3 的 D 用 `MooncakeConnectorV1`。两者写出的 `pd_transfer_summary` 记录字段逐一相同，记录里没有区分 connector 种类的字段，过滤器不需要区分。

glm5.3 的配置里和记录有关的几项：`MultiConnector` = `MooncakeConnectorV1` + `AscendStoreConnector`；`AscendStoreConnector` 在 P 上 `load_async=false`、在 D 上 `load_async=true`，两边都是 `kv_both`、未开层式；`kv_load_failure_policy=fail`；P 每实例 TP16，D 每实例 TP1。

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
| `released_requests` | 这一步要等的请求数，即调度器本步交来的「已释放请求」个数，可以为 0。核对用途：区分「`wait_for_put` 接近 0 是因为没有要等的请求」和「确实等得快」 |
| `timing_scope` | 固定为 `"released_request_save_fence"`。核对用途：标明计时口径，沿用补丁 01 自己的写法 |

**什么时候写。** 只要这一步执行了 `handle_preemptions`，并且缓存池的保存线程是非层式的 `KVCacheStoreSendingThread`，就写这个阶段。没有要等的请求时照样写，`released_requests` 为 0，`duration_ns` 只含函数进出本身，接近 0。以下情况不写：dummy run（这时没有 `worker_summary`）；实例没有缓存池保存线程；层式保存（层式保存在每步最后一层已经同步，不进这个函数）。glm5.3 的 P、D 都是 `kv_both`、未开层式，按这个条件 P、D 都会写。

**测量点。** `vllm_ascend/distributed/kv_transfer/kv_pool/ascend_store/kv_transfer.py` 的 `KVCacheStoreSendingThread.wait_for_requests_saved`（`d4ad49796` 第 947 行）。调用链：

1. runner 调 `handle_preemptions`：默认的 V1 runner 在 `worker/model_runner_v1.py:2282`；设了 `VLLM_USE_V2_MODEL_RUNNER` 时是 V2 runner，在 `worker/v2/model_runner.py:616`；
2. vLLM 的 `MultiConnector.handle_preemptions`（v0.30.0 `multi_connector.py:370`）分给每个子 connector；
3. `ascend_store_connector.py:290` 的 `handle_preemptions`；
4. `pool_worker.py:3116` 的 `handle_released_saves`，它在第 3131 行调用 `wait_for_requests_saved`。

等待用 1 ms 间隔轮询（`SAVE_REQUEST_POLL_INTERVAL_S = 0.001`，`kv_transfer.py` 第 893 行），所以有等待时，时长按 1 ms 左右取整，偏大最多约 1 ms。原有那行 DEBUG 日志不改。

**归属。** 等待发生在这一步 forward 之前，记给这一步的请求，即 `request_ids` 里的每个请求。按 C3 只用 `tp_rank` 为 0 的记录。

**`worker_summary` 由谁生成。** 补丁 01 的 `WorkerWrapperBase.execute_model` 只设 `diag.scope`，不建记录。`worker_summary` 由 runner 的 `execute_model`、`sample_tokens` 上的两个装饰器生成：`pd_execution` 生成 `phase="execute"` 的那条，`pd_sample` 生成 `phase="sample"` 的那条，两个装饰器都定义在补丁 01 的 `vllm/v1/worker/worker_base.py`。补丁 01 只给 vLLM 的 V2 GPU runner 加了这两个装饰器；`d4ad49796` 默认用的 V1 runner（`worker/model_runner_v1.py` 第 2229 行的 `execute_model`、第 2766 行的 `sample_tokens`）由 vllm-ascend 补丁加上。

**V1 runner 的 forward、sampling。** `stages.forward`、`stages.sampling` 由 runner 里的 `diag.span("runner.forward", ...)`、`diag.span("runner.sampling", ...)` 写入。补丁 02 在 `f9c8e0bdb` 上把这两处加在 `worker/v1/model_runner.py` 和 `worker/v2/model_runner.py`；`d4ad49796` 默认用的 V1 runner 是 `worker/model_runner_v1.py`，这两处要加在它里面，参数和补丁 02 相同（forward：`completion="host_return"`；sampling：`algorithm` 为 `"sampler"` 或 `"rejection"`，`completion="host_return"`）。glm5.3 默认时 `runner` 为 `vllm_ascend.worker.model_runner_v1.NPUModelRunner`。

**设备计时（统筹定，C14）。** glm5.3 的 D 是图模式加异步调度，host 侧只量到发命令的耗时，所以 forward、sampling 另记设备上的时长：

- **新字段。** `stages.forward`、`stages.sampling` 各加一个 `device_duration_ns`：这一步在设备上执行 forward（或 sampling）的时长，取自前后两个设备事件，两个事件在同一条计算流上。原有的 host 侧 `duration_ns`、`start_ns`、`end_ns` 不变。这是 C14 唯一新增的字段。核对用途：在 P 上比较 host 值和设备值，检验设备计时本身是否准确。
- **哪些记录有。** 只在 V1 runner、`tp_rank` 为 0 的 `worker_summary` 里写；事件不加同步、延后读取，读取前先 `query()`，读不到时不写这个字段（不写 0）。以后如果改了 `keep_tp_rank`，其他 rank 的记录没有设备值，只能回退到 host 值，两种口径会混在一起，用时要注意。
- **推迟写出。** `tp_rank` 为 0 上带设备值的 `worker_summary[phase=execute]`、`[phase=sample]` 推迟到读到设备值以后再写，最多推迟 4 步，写出顺序严格按步骤先后；到期还读不到设备值，这条记录照常写出，只是不带 `device_duration_ns`，也不写 0。下一步开始时和进程退出时都有兜底写出。这两条记录的 `timestamp_ns`、`native_tid` 是写出时的时刻和线程，不是这一步执行时的；各阶段的 `start_ns`、`end_ns` 不变。推迟写出用的是 vLLM `worker_base` 里一个默认关闭的开关，具体接口由 vLLM 和 vllm-ascend 两处移植对齐。不另写补充记录，同一条记录里既有 host 值也有设备值。
- **段的取法。** 段定义优先取 `device_duration_ns`，没有时回退到 `duration_ns`；面板按段回退，并在过滤器的自检行里数出回退了多少步。forward 的定义因此从「目标模型调用开始 → host 返回」改为「设备上执行这一步的时长」，sampling 同理。

**1.4.0：** 同一套设备计时另加 4 个 stage（prepare_inputs、lm_head、draft、bookkeeping），以及只记录的几个字段，都只在 D，见 7.4、7.5。

**参考取法（给 `segments.json`，由另一个 worker 定）。** 和 v3 的 `forward` 一样走累加：`event` 为 `worker_summary`，`where` 为 `{"phase": "execute"}`，`key` 为 `request_ids`，`value` 为 `stages.wait_for_put.duration_ns`，P 侧 `part` 取 `total`。

## 2. 缓存池加载任务 `pool_task[load]`（glm5.3 P 的 submit 用，C11）

**选择条件。** `event="pool_task"`，`scope="task"`，`operation="load"`。

**会用到的字段。**

| 字段 | 含义 |
|---|---|
| `duration_ns` | 任务执行时长：开始处理 → 处理结束（`end_ns − start_ns`） |
| `start_ns`、`end_ns` | 开始处理、处理结束的时刻 |
| `queue_enter_ns` | 任务进入加载线程队列的时刻；同步调用的任务为 `null` |
| `counters.queue_wait_ns` | 在队列里等待的时长：各次出队开始处理的时刻减去入队时刻，求和；同步调用的任务没有这个字段 |
| `counters.queue_entries` | 出队次数，通常为 1；同步调用的任务没有这个字段 |
| `status` | `succeeded`、`backend_failure`、`unknown_result`、`all_hit`、`no_task`、`skipped`、`missing_load_spec`、`exception`、`not_processed` 等 |
| `request_id`、`execution_id` | 任务只含一个请求时为该请求的号和步骤号；含多个请求时 `request_id` 为 `null`，`execution_id` 在所有请求同属一步时保留，否则为 `null` |
| `members[]` | 每个请求一项：`request_id`、`execution_id`，有时带 `observation_id`、`request_generation`、`store_job_id` |
| `member_count`、`members_dropped` | 保留的请求数、超过 128 个后丢掉的次数 |
| `tp_rank` | 写这条记录的 Worker |
| `associated_stages.backend_get` | 实际调用 Mooncake get 的时长，只作关联参考 |

C1（`counters.queue_wait_ns + duration_ns`）是 dsv4.1 P 的 wait_for_transfer 用的；glm5.3 的 P 没有这个后台阶段，D 的段不用缓存池加载，glm5.3 不用 C1。

### 2.1 P：同步加载（`load_async=false`）

glm5.3 的 P 不起加载线程，加载在 Worker 主线程里、`pool_worker.py` 的 `start_load_kv`（`d4ad49796` 第 1251 行）中逐个请求同步完成。格式和补丁 02 的同步分支相同（02 在这里用 `capture(request, "load", tp_rank, queued=False)`）：

- 这一步缓存池 connector 元数据里的每个请求写一条 `operation="load"`，`member_count` 为 1，`request_id`、`execution_id` 为该请求和这一步；
- 没有真正加载的请求也写，用 `status` 区分：`skipped`（`can_load` 为假）、`missing_load_spec`、`no_task`（没有要取的 key）；
- `queue_enter_ns` 为 `null`，没有 `counters.queue_wait_ns` 和 `counters.queue_entries`；
- `duration_ns` 包含 Mooncake get 本身（`tp_mismatch` 分支里的加载也在内）；
- **P 不写 `operation="load_submit"` 的记录**，这种记录只在 `load_async=true` 时有。

**取法（统筹定，C11）。**

- P 的 start_load_kv/submit 取 `pool_task[operation=load]` 的 `duration_ns`，含义是「同步加载，含实际传输」。
- 只取终态为 `succeeded`、`backend_failure`、`unknown_result`、`exception` 的记录；`all_hit`、`no_task`、`skipped`、`missing_load_spec` 不取，没命中缓存池的请求这一段为空。
- 每个请求单独一条，`members` 只有一项，`request_id` 不为空，C9 用不上。
- 和 C3 一样只取 `tp_rank` 为 0 的记录。
- `exception` 的时长只算到异常抛出为止。Mooncake 的 get 抛异常时，`MooncakeBackend.get` 自己接住并返回 `None`，这种情况记为 `backend_failure`，不是 `exception`；`exception` 只出现在 backend 之外出错的时候，例如准备地址那一步。C11 取的四种终态不受影响。
- P 的 start_load_kv/wait_for_transfer 为空：没有后台阶段，P 没有任何「等加载完成」的记录。
- 不取 Worker 整步的 `worker_summary[phase=execute].stages.submit`。有同步加载时，vLLM 在 forward 之前调用 `start_load_kv`（v0.30.0 `kv_connector_model_runner_mixin.py:86`，`has_sync_kv_loads` 为真），这个阶段包含这一步所有请求的同步加载，按 `request_ids` 会计到这一步的所有请求上，没命中缓存池的请求也算在内。第一批真实数据出来后核对一项：同一步里各请求的值加起来，应当约等于这一步的 `stages.submit`。

加载在调度这个请求的同一步里做完，请求本身就在这一步的 `request_ids` 里，P 不写 3.3 节的映射记录。

### 2.2 D：异步加载（`load_async=true`）

和 dsv4.1 相同：加载在后台线程 `KVCacheStoreRecvingThread` 里执行，每个加载任务完成时写一条 `operation="load"`；提交另有一条 `operation="load_submit"`、`status="submission_returned"` 的记录。D 只有在缓存池被选中加载时才有这些记录（见 3.4 节），D 的段不用它们，和 v3 相同。

**已知情况，供过滤器参考。** 一个异步加载任务可能含多个请求（`member_count > 1`），这时值怎么分给各请求，按 C1 由过滤器定。

## 3. 首次候选那一步（C2 用）

**这一步是什么。** 请求第一次被调度器选中时，如果它的 KV 要异步取来，请求转入 `WAITING_FOR_REMOTE_KVS`，这一步对它只发起读取、不做 forward，请求本身不在这一步的 `request_ids` 里，所以 v3 归不到请求上。glm5.3 有两种情况，都在 D 上：

- D 从 P 读 KV（`MooncakeConnectorV1`），这是主要情况；
- D 命中缓存池、异步加载（`AscendStoreConnector`，`load_async=true`）。

P 是同步加载（2.1 节），没有这一步。

**统筹定的做法（C2）：** 过滤器在 EngineCore 进程里，用同一进程里带「请求号 R + 步骤号 X」的映射记录，把步骤 X 的调度记录计给请求 R。两种映射记录形状相同。

### 3.1 关联用的记录和规则

| 记录 | 选择条件 | 用到的字段 | 谁写 |
|---|---|---|---|
| 映射记录 | `event="pd_transfer_summary"`，`scope="link"`，`transfer_role` 为 `"consumer"`（D 从 P 读）或 `"pool_load"`（D 从缓存池异步加载） | `request_id`、`execution_id` | EngineCore |
| 这一步的调度汇总 | `event="core_summary"`，`scope="execution"` | `execution_id`；`stages.lookup`、`stages.alloc`、`stages.other` 的 `duration_ns`；`request_ids`；`no_forward` | EngineCore |
| 请求的调度汇总 | `event="core_summary"`，`scope="request"` | `request_id`；`stages.queue.ranges[0].execution_id` | EngineCore |
| 这一步的 Worker 记录 | `event="worker_summary"`，`phase="execute"` | `execution_id`；`stages.submit.duration_ns`（connector 的 `start_load_kv` 调用 → 返回，只入队）；`tp_rank` | Worker |

映射记录在 `build_connector_meta` 里写，这个函数在同一次 `schedule()` 的末尾被调用，`execution_id` 取 `scheduler_output.pd_execution_id`，也就是这一步本身。`stages.queue.ranges[0].execution_id` 是请求第一次被选中那一步的步骤号（补丁 01 `vllm/v1/request.py`，只在第一次被选中时追加一项）。两者是同一步，所以：

```
pd_transfer_summary[scope=link].execution_id
  == core_summary[scope=execution].execution_id
  == core_summary[scope=request].stages.queue.ranges[0].execution_id
  == worker_summary[phase=execute].execution_id
```

### 3.2 D：`MooncakeConnectorV1` 的映射记录（consumer）

**写在哪里。** `kv_p2p/mooncake_connector.py` 的 `MooncakeConnectorScheduler.build_connector_meta`（`d4ad49796` 第 1978 行），由 EngineCore 写出。在循环 `self._reqs_need_recv` 里每次 `meta.add_new_req(...)` 之后：

1. 给这个请求的 `ReqMeta`（第 121 行，新加属性 `pd_execution_id`，默认 `None`）赋值 `scheduler_output.pd_execution_id`；Worker 侧第 4 节要用它；
2. 写一条映射记录，字段和 hybrid 补丁 02 的相同：

| 字段 | 值 |
|---|---|
| `event`、`scope`、`transfer_role` | `"pd_transfer_summary"`、`"link"`、`"consumer"` |
| `request_id` | D 上的请求号 R |
| `execution_id` | 这一步的步骤号 X |
| `engine_id` | `MooncakeConnectorScheduler.engine_id`（glm5.3 的 D 为 `"1"`） |
| `dp_rank` | `parallel_config.data_parallel_rank` |
| `links` | `[{"kind": "producer", "request_id": <ReqMeta.remote_request_id>, "engine_id": <ReqMeta.remote_engine_id>}]` |
| `status`、`complete`、`incomplete` | `"linked"`、`true`、`false` |

写法和 hybrid 相同：`diag.Batch("pd_transfer_summary", scope="link", request_id=..., execution_id=..., engine_id=..., dp_rank=..., transfer_role="consumer")`，`add_link("producer", ...)`，`finish(status="linked", complete=True, incomplete=False)`。EngineCore 进程里没有 `tp_rank`，这条记录也不带 `tp_rank`。

### 3.3 D：缓存池异步加载的映射记录（pool_load，新增）

**写在哪里。** `vllm_ascend/distributed/kv_transfer/kv_pool/ascend_store/pool_scheduler.py`，`KVPoolScheduler.build_connector_meta` 的末尾（新方法 `_emit_load_links`），由 EngineCore 写出。

**写哪些请求。** 这一步 connector 元数据里 `load_spec.can_load` 为真的每个请求，即 Worker 这一步会对它发起缓存池加载的请求，一个请求一条。

**什么时候不写。** `load_async=false`（同步加载）和 `use_layerwise=true`（层式加载）时都不写：这两种情况下加载在调度这个请求的同一步里完成，请求本身就在那一步的 `request_ids` 里，不需要映射。**glm5.3 的 P 是 `load_async=false`，P 不写这种记录。** D 是 `load_async=true`，缓存池被选中加载时写。

| 字段 | 值 |
|---|---|
| `event`、`scope`、`transfer_role` | `"pd_transfer_summary"`、`"link"`、`"pool_load"` |
| `request_id` | 请求号 R |
| `execution_id` | 这一步的步骤号 X（`scheduler_output.pd_execution_id`） |
| `engine_id` | `kv_transfer_config.engine_id` |
| `dp_rank` | `parallel_config.data_parallel_rank` |
| `load_async` | `true`。核对用途：记下映射记录成立的条件（只有异步加载才写） |
| `vllm_cached_tokens`、`kvpool_cached_tokens` | 本地已有的 token 数、缓存池命中的 token 数。核对用途：核对 C1、C11 的值和命中规模是否相称 |
| `links` | `[]` |
| `status`、`complete`、`incomplete` | `"linked"`、`true`、`false` |

和 3.2 节的 consumer 映射记录相比：事件名、`scope`、`request_id`、`execution_id`、`engine_id`、`dp_rank`、`status`、`complete` 的含义相同；`transfer_role` 不同；`links` 为空（没有对端）；多出 `load_async` 和两个 token 数。两种记录都由 `diag.Batch` 生成，所以同样带着 `schedule()` 的上下文字段（如 `kv_role`、`batch_shared`、`request_ids=[]`）。

写记录的代码包在 `try/except` 里，出错时什么都不写，不影响调度。

### 3.4 已知情况，供过滤器参考

- **D 上一般只有 consumer。** vLLM 的 `MultiConnector` 对每个请求按顺序问各子 connector，用第一个报出可取 token 的那个；glm5.3 里 `MooncakeConnectorV1` 排在前面，带远端预填参数的请求都由它取。只有它报 0 个时，才可能轮到缓存池，这时才有 pool_load 映射记录。
- D 上通常同时有别的请求在 decode，这一步的 Worker 记录 `request_ids` 不为空，里面是那些 decode 请求，它们这一步的 forward、sampling 照常归给它们。`stages.submit` 是这一步所有新发起读取的合计；同一步里有几个请求同时开始读时，它们共用这一个值，怎么分由 C2 定。
- `stages.queue.ranges` 可能为空：请求没有经过等待队列，或者记录不完整（`stages.queue.status` 不是 `ok`）。映射记录不受影响。
- glm5.3 的 `kv_load_failure_policy` 是 `fail`：读取失败时请求以失败结束，不在本地重算。这时出错的读任务 `status="failed"`，同一请求后面的读任务不再读取、`status="skipped"`（4.2 节），`request_generation` 记录 `status="failed"`；首次候选那一步的映射记录已经写出，不受影响。
- 请求在进入调度之前就被拒绝（例如 prompt 加 `max_tokens` 超过 `max_model_len`）时，`MooncakeConnectorScheduler.request_finished`（第 2031–2040 行）会补一个空的读取，让 P 释放块。这种请求在下一步也会有 consumer 映射记录，Worker 侧对应的读任务 `status="skipped"`；它没有别的调度记录，关联不上任何段。
- 一个请求只有在发起异步读取的那一步有映射记录；后面等读取完成、真正被调度的步骤里，请求在 `request_ids` 里，不需要映射。
- **D 用 RecomputeScheduler 时，被退回 P 的请求。** glm5.3 的 D 开了 `recompute_scheduler_enable`，用的是 `RecomputeScheduler`（异步时为它的 Async 变体），它重写了 `schedule()`。KV 块不够时，它在 `schedule()` 里把请求退回 P。这种请求的 `core_summary[scope=request]` 和正常结束的请求有两处不同：多一个 `role: "unknown"`，少一个 `no_forward`。原因是这条记录在调度器的上下文里写出；补丁 01 里被 abort 或 shutdown 结束的请求本来也没有 `no_forward`。过滤器和采集器都不读这两个键，段不受影响。

## 4. `MooncakeConnectorV1` 的读取记录（新增，格式同 hybrid）

`MooncakeConnectorV1` 在 D 上写三种记录：3.2 节的 consumer 映射记录（EngineCore）、下面的读任务记录和请求汇总记录（Worker 的读取线程）；在 P 上写一种：4.4 节的 producer 记录。字段、取值和写法逐一照补丁 02 的 `mooncake_hybrid_connector.py`，下面写明在 V1 代码里对应的位置。glm5.3 的 D 每实例 TP1，所以这些记录的 `tp_rank` 都是 0。

### 4.1 读任务的身份（Worker 主线程，入队时）

**写在哪里。** `MooncakeConnectorWorker.start_load_kv`（`d4ad49796` 第 3824 行）里每次调用 `self.kv_recv_thread.add_request(...)` 时，把步骤号和序号作为可选参数传进去：`pd_execution_id=meta.pd_execution_id`、`pd_offset=<序号>`。`KVCacheRecvingThread.add_request`（第 568 行）有这两个参数时用参数，没有时退回 `diag.current()` 里的值；两种写法写出的值相同。然后生成身份，建 `diag.Batch("pd_transfer_summary", scope="task", **identity)`，挂到这次的 `trans_info["_pd_diag"]` 上。

**步骤号缺失时。** 读 `scheduler_output.pd_execution_id`、`meta.pd_execution_id` 都用 `getattr(..., None)`：属性不存在时 `execution_id` 记 `null`，不报错。这个属性由 vLLM 的移植（补丁 01）保证存在。

| 字段 | 值 |
|---|---|
| `request_id` | D 上的请求号 |
| `remote_request_id` | P 上的请求号 |
| `remote_engine_id` | 这次读取的对端 engine_id（`_get_remote_host_info_by_port` 返回的那个） |
| `engine_id` | 本端 `local_engine_id` |
| `connector_id` | 读取线程建立时生成一次（`diag.new_id("pd_connector")`） |
| `task_id` | 每个读任务一个（`diag.new_id("pd_transfer")`） |
| `execution_id` | 发起这次读取的步骤号 X，来自上面的 `diag.scope` |
| `tp_rank` | 读取线程所在 Worker 的 `tp_rank` |
| `dp_rank` | `parallel_config.data_parallel_rank` |
| `direction` | `"read"` |
| `offset` | 这个请求在这次 `start_load_kv` 里第几次 `add_request`，从 0 起，按两层循环的顺序（外层 `shard_idx`，内层 `remote_tp_offset`）展平，含义和 hybrid 的 `i` 相同。一个请求的读任务数 = shard 数 × 每个 shard 的对端数 |
| `request_generation` | 同一请求的一轮读取共用一个（`diag.new_id("pd_request")`），在这个请求没有未完成的汇总时，由第一次 `add_request` 生成 |
| `links` | `[{"kind": "producer", "request_id": <remote_request_id>, "engine_id": <remote_engine_id>}]` |

`meta.do_virtual` 为真的请求（第 3833 行）不调 `add_request`，不写读任务记录，也不写请求汇总记录；调度端照常写 3.2 节的 consumer 映射记录。所以这类请求只有映射记录，D 的 wait_for_transfer 这一段为空。只有请求的 `kv_transfer_params` 带 `do_virtual=true` 时才会走这条路径。

入队时记 `queued_ns`；读取线程的 `_submit_request`（第 651 行）记 `submitted_ns`。

### 4.2 读任务记录 `pd_transfer_summary[task]`（读取线程，任务结束时）

**写在哪里。** `KVCacheRecvingThread._handle_request`（第 720 行）。整个函数体包在 `with diag.scope(**identity), diag.collect(batch):` 里；开始处理时 `batch.update(...)`；函数最后（发完给 P 的完成信号 `_send_done_recv_signal` 之后）`batch.finish(...)`，所以 `processing_duration_ns` 和 hybrid 一样，覆盖从开始处理到发完完成信号的整段。

**开始处理时更新的字段。** `start_ns`；`queued_ns`；`submitted_ns`；`queue_wait_ns`（`start_ns − queued_ns`）；`final_task`（`all_task_done`）。

**`stages.wait_for_transfer`。** 每次原生读取调用一次 `add_stage("wait_for_transfer", start_ns=..., end_ns=..., status=..., exceptions=..., completion="native_read_return")`。V1 每个任务只有一处原生读取（第 1042 行 `self.engine.batch_transfer_sync_read`），所以至多一次。没有发起原生读取时，结束前补一次 `add_stage("wait_for_transfer", calls=0, status=...)`：`transfer_status` 为 `skipped` 或 `no_work` 时传 `status="skipped"`，否则传 `"unmeasured"`。记录里看到的是 `Batch.add_stage` 处理后的结果：前者 `status="skipped"`；后者 `status="missing"`、`unmeasured_calls=1`，不会出现 `"unmeasured"`。这是 `add_stage` 对没有时长、又不是 skipped 的调用的处理，和 hybrid 相同。

**`associated_stages`（只作关联参考）。** 键和 hybrid 相同，每项为 `calls`、`duration_ns`、`exceptions`、`start_ns`、`end_ns`、`status`：

| 键 | V1 里包住的代码 |
|---|---|
| `meta_fetch` | `_transfer_kv_cache_all_groups` 里从检查 `has_remote_metadata` 到从缓存表取出对端地址（第 807–823 行） |
| `metadata_request` | 其中的 `self._get_remote_metadata(...)`，只在没有缓存时发生 |
| `prepare` | 从 `req_start_time = time.perf_counter()`（第 827 行）到原生读取之前，即拼 `src_list`、`dst_list`、`length_list` |
| `reformat` | `_handle_request` 里全部读取结束后调用的 `_reformat_pending_kv_caches`，记在执行它的那个任务上 |

**结束时的字段。**

| 字段 | 值 |
|---|---|
| `status` | 见下表 |
| `error_type` | 异常的类名；没有异常时为 `null` |
| `end_ns`、`processing_duration_ns` | 结束时刻、`end_ns − start_ns` |
| `timing_scope` | `"task_processing_envelope"` |
| `transfer_envelope_ns` | 第一次原生读取开始 → 最后一次结束；没有原生读取时为 `null` |
| `transfer_timing_scope` | `"native_read_envelope_not_call_sum"` |
| `submitted_bytes`、`succeeded_bytes`、`failed_bytes` | 按原生调用的提交长度合计（不是线上字节） |
| `transfer_status` | 初值 `not_submitted`；`skipped`、`no_work`、`succeeded`、`failed`、`cancelled` |
| `tracker_completed` | `_mark_request_task_done` 的返回值，即这个请求的全部读任务是否已完成 |
| `native_calls`、`native_calls_limit`、`native_call_count`、`native_calls_dropped` | 原生调用明细（见下）、64、调用次数、超过 64 后丢掉的次数 |
| `associated_stages` | 上表 |
| `complete`、`incomplete` | `native_calls_dropped` 为 0 时 `true`、`false`，否则反过来 |

**V1 下的恒定项。** V1 的每个读任务只调一次 `batch_transfer_sync_read`，所以 `native_calls` 最多 1 条，`native_calls_dropped` 恒为 0，`complete` 恒为 `true`、`incomplete` 恒为 `false`。字段照 hybrid 保留。

`status` 的取值和 V1 代码里的对应：

| `status` | 什么时候 | `transfer_status` |
|---|---|---|
| `succeeded` | 原生读取返回值不小于 0，并且没有异常（含 `reformat`） | `succeeded` |
| `skipped` | 本地没有要读的块（第 804 行提前返回） | `skipped` |
| `skipped` | 有块但拼出来的 `src_list` 为空（第 1031 行提前返回），不发起原生读取 | `no_work` |
| `failed` | 读取中抛出异常，包括返回值小于 0 时第 1049 行抛出的 `RuntimeError` | `failed`（已发起原生读取时）或 `not_submitted` |
| `failed` | `_reformat_pending_kv_caches`（第 750 行）抛出异常，在其后的 `except` 里捕获 | 保持读取本身的值 |
| `skipped` | 同一请求前面的任务已失败，这个任务不再读取（第 727 行 `_is_failed_recv_request`）；`error_type` 为 `null`。前面那个任务记 `failed`，所以请求汇总仍为 `failed` | `not_submitted` |
| `cancelled` | `CancelledError` | 按读取进度 |
| `interrupted` | 初值；上面各情况都会覆盖它，正常不会出现 | 按读取进度 |

**`native_calls[]` 每项**（和 hybrid 相同）：`call_id`（`diag.new_id("pd_read")`）、`operation`（`"batch_transfer_sync_read"`）、`native_tid`（`threading.get_native_id()`）、`session_id`、`status`、`error_type`、`native_return`、`start_ns`、`end_ns`、`duration_ns`、`start_wall_ns`、`end_wall_ns`、`wall_utc_offset_seconds`、`transfer_scope_duration_ns`（从 `req_start_time` 到原生调用返回）、`transfer_scope`（`"original_prepare_and_native_read"`）、`submitted_bytes`、`succeeded_bytes`、`failed_bytes`、`submitted_segments`、`result_scope`（`"native_batch"`）、`per_segment_status`（`"unavailable"`）、`byte_semantics`（`"submitted_lengths_by_batch_status_not_wire_bytes"`）。

原有的日志行（第 1053 行的 `KV cache transfer for request ... took ... ms`）和返回值、异常行为都不改。

**1.4.0：** 使请求完成的那条 task 另有 `request_done_ns`（7.3）。

### 4.3 请求汇总记录 `pd_transfer_summary[request_generation]`

**什么时候写。** 同一请求同一个 `request_generation` 的读任务全部结束（未完成数为 0），并且带 `all_task_done` 的最后一个任务已经入队时，由结束最后一个任务的读取线程用 `diag.emit` 写一条。

| 字段 | 值 |
|---|---|
| `event`、`scope` | `"pd_transfer_summary"`、`"request_generation"` |
| `request_id`、`tp_rank`、`dp_rank`、`request_generation` | 同 4.1 节 |
| `connector_id`、`engine_id` | 同 4.1 节 |
| `remote_request_id`、`task_id`、`execution_id`、`observation_id`、`remote_engine_id`、`offset` | 都为 `null`：一个请求的读取可能跨几个步骤、几个对端，不归给其中某一个 |
| `pending_tasks`、`final_task_queued` | 0、`true` |
| `submitted_tasks`、`succeeded_tasks`、`failed_tasks`、`skipped_tasks` | 各状态的任务数；`cancelled`、`interrupted` 计入 `failed_tasks` |
| `submitted_bytes`、`succeeded_bytes`、`failed_bytes` | 各任务之和 |
| `tracker_completed`、`native_calls_dropped` | 各任务的或、之和 |
| `status` | 有失败的任务为 `failed`；全部 `skipped` 为 `skipped`；否则 `succeeded` |
| `transfer_envelope_ns` | 所有任务里最早的原生读取开始 → 最晚的结束；没有原生读取时为 `null` |
| `timing_scope` | `"native_read_envelope_not_task_sum_or_pure_wait"` |
| `complete`、`incomplete` | `native_calls_dropped` 为 0 时 `true`、`false`，否则反过来 |

v3 用这条记录的 `transfer_envelope_ns` 做 D 的 wait_for_transfer，和 dsv4.1 相同。这条记录的 `execution_id` 固定为 `null`，不能用来关联步骤；按步骤关联用 3.2 节的映射记录或 4.2 节的读任务记录。

**1.4.0：** 另有 `kv_load_envelope_ns`、`kv_load_timing_scope`、`task_max_queue_wait_ns`、`task_max_meta_fetch_ns`、`task_max_prepare_ns`（7.3）。

### 4.4 P：producer 记录

**写在哪里。** P 上 `MooncakeConnectorScheduler.request_finished`（第 2007 行）里，算出 `delay_free_blocks`（第 2049 行）之后、返回 `kv_transfer_params`（第 2054 行）之前，由 EngineCore 写一条。只有走到这里（`do_remote_decode` 为真、`FINISHED_LENGTH_CAPPED`）的请求才写。

| 字段 | 值 |
|---|---|
| `event`、`scope`、`transfer_role` | `"pd_transfer_summary"`、`"link"`、`"producer"` |
| `request_id`、`engine_id`、`dp_rank` | P 上的请求号、P 的 engine_id（glm5.3 为 `"0"`）、`data_parallel_rank` |
| `delayed_free` | `delay_free_blocks` |
| `links` | `[{"kind": "producer", "request_id": <本请求号>, "engine_id": <P 的 engine_id>}]` |
| `status`、`complete`、`incomplete` | `"offered"`、`true`、`false` |

它不是在首次候选那一步写的，即使带了 `execution_id`（来自写记录时的上下文），也不是 C2 要的那一步，C2 不用。

## 5. Mooncake 原生计时行 `[BatchPutTiming]`（C4 用）

**行格式。** 补丁 04 在每次 `Client::BatchPut`（`client_service.cpp`）结束时，用 glog 的 `LOG(INFO)` 写一行。消息部分是空格分隔的键值对，顺序固定：

```
[BatchPutTiming] keys=<n> bytes=<n> phase1_start_batch_put_us=<n> phase2_submit_transfers_us=<n> phase3_wait_transfers_us=<n> phase4_finalize_batch_put_us=<n> total_us=<n> timing_scope=<s> status=<ok|error> complete=true failed_keys=<n>
```

配置校验失败、还没开始时，写的是另一种短行，没有四个阶段，也没有 `total_us`：

```
[BatchPutTiming] keys=<n> bytes=<n> status=error complete=false timing_scope=prefer_same_node_not_started phase_status=not_started
```

`BatchUpsert` 写的是 `[BatchUpsertTiming]`，格式相同。glm5.3 的保存路径和 dsv4.1 一样调用 `batch_put_from_multi_buffers`（`d4ad49796` `backend/mooncake_backend.py` 第 443 行），对应 `BatchPut`。

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
- **线程号是否等于 `native_tid`。** glog 0.6 及以前，前缀里的线程号是 Linux 的 `gettid()`，和 Python 的 `threading.get_native_id()` 相同；glog 0.7 起改为 `std::this_thread::get_id()`，和 `native_tid` 不同（glog 源码 `src/logging.cc`，v0.6.0 用 `GetTID()`，v0.7.0 用 `std::this_thread::get_id()`）。现场装的是哪个 wheel，以检测脚本的 `mooncake=` 行为准。各 wheel 自带的 glog（由 wheel 的 RECORD 读出）：

| wheel | 自带 glog | 线程号与 `native_tid` |
|---|---|---|
| 华为 OBS `v0.3.14.rc1` 目录里的 `mooncake_transfer_engine`（版本串 0.3.13，dsv4.1 所用） | `libglog-bb9621e6.so.0.4.0`（aarch64） | 相同，实验室实测（装上这个 wheel、走 TCP 做 `put_batch`） |
| PyPI `mooncake-transfer-engine-npu` 0.3.11.post1、0.3.12、0.3.12.post1、0.3.13、0.3.13.post1、0.3.14（cp312，aarch64；`d4ad49796` 的 Dockerfile 装 0.3.11.post1） | `libglog-bb9621e6.so.0.4.0`，和 OBS wheel 是同一个文件名 | 按 glog 0.4.0 应相同，未实测 |
| PyPI `mooncake-transfer-engine` 0.3.13（非 npu） | `libglog-b842b135.so.0.0.0`（aarch64），从文件名看不出 glog 版本 | 未核对 |

如果不相等，只能按「同一文件里时间落在调用区间内、`keys` 相等」关联。

## 6. 和补丁 02 原有格式的差异

目标是零差异，下面逐条列出。

| # | 差异 | 影响 |
|---|---|---|
| 1 | `worker_summary[phase=execute]` 新增 `stages.wait_for_put`（第 1 节） | 只加字段。v3 读到的 `stages.forward`、`stages.sampling` 等不变 |
| 2 | EngineCore 新写 `pd_transfer_summary[scope=link]`、`transfer_role="pool_load"` 的记录（3.3 节）；glm5.3 只有 D 可能写 | 新的行，事件名和 `scope` 沿用已有的；v3 不引用 `scope="link"`，不受影响 |
| 3 | `MooncakeConnectorV1` 写出 consumer 映射记录、读任务记录、请求汇总记录和 producer 记录（3.2 节、第 4 节）。补丁 02 只给 `MooncakeHybridConnector` 打了点，这些记录的格式照它 | 对 v3 来说和 dsv4.1 的 D 写出的记录相同 |
| 4 | `pool_task` 记录的顶层、`members[]`、`completions[]` 都不带 `store_job_id`；dsv4.1 从同一请求的第二个保存任务起会带。原因是 `d4ad49796` 没有保存任务号 | 字段缺省。v3 不用 `store_job_id` 关联，不受影响 |
| 5 | 整个 key 保存失败时记 `backend_failure` 或 `unknown_result`，不出现 `failed` | `status` 的取值少一种；按状态筛选时不要依赖 `failed` |
| 6 | 线程正常停止时不写 `not_processed`，只在线程异常退出时写 | `not_processed` 只表示线程异常退出 |
| 7 | `stages.forward`、`stages.sampling` 新增 `device_duration_ns`（只在 V1 runner、`tp_rank` 为 0）；这些记录推迟写出，最多 4 步，`timestamp_ns`、`native_tid` 是写出时的（第 1 节，C14） | 只加字段；v3 读到的 `duration_ns` 不变。按 `timestamp_ns` 排序时，这些记录会晚于它们所属的那一步 |
| 8 | 1.4.0（T56）：`core_summary[request]` 加 `stages.kv_ready_wait`；`worker_summary` 加 `prepare_inputs`、`lm_head`、`draft`、`bookkeeping` 4 个 stage 和只记录的 `step_device_span_ns`、`cudagraph_runtime_mode`；都只在 D（第 7 节） | 只加阶段和字段；已有阶段的边界不动 |
| 9 | 1.4.0（T56）：`MooncakeConnectorV1` 的 `request_generation` 加 `kv_load_envelope_ns`、`kv_load_timing_scope`、`task_max_queue_wait_ns`、`task_max_meta_fetch_ns`、`task_max_prepare_ns`，task 加 `request_done_ns`；hybrid 没有这 6 个字段（7.3） | 是「两种 connector 同一种格式」的例外；对 hybrid 的记录不要依赖它们 |

另外两处不是格式差异，只是写的位置不同：forward、sampling 两个阶段写在 `d4ad49796` 的 V1 runner `worker/model_runner_v1.py` 里（第 1 节）；P 的缓存池加载是同步的，`pool_task[load]` 由 Worker 主线程写，没有 `load_submit`（2.1 节），这是补丁 02 本来就有的同步分支。

`d4ad49796` 的保存路径和 `f9c8e0bdb` 相同：`add_save_batch`（`kv_transfer.py` 第 934 行）一次把一批请求放进保存队列，后面跟一个批次结束标记；等待保存完成用 `wait_for_requests_saved`（只等被释放的请求）。`pool_task[save]` 的记录除上表第 4–6 项外和补丁 02 相同：每个请求在入队时取快照，出队时开始计时；批次结束标记不产生记录。以后再发现差异，补进上表，并先告诉统筹。

## 7. T56：D 侧新增（1.4.0）

设计依据：`docs/tasks/T56/10_记录格式约定草案.zh-CN.md`（1.4.0 设计定稿，实施开始时改定，又按顾问的精简把关和统筹的 `32_精简落地决定` 和之后的回复改一次，`d805b26f`）。本节只写节点进程写出什么；段怎么取由 `segments.json` 定（t41）。本节新增的每一项都只在 D 上写，规则见 7.1。

### 7.1 只在 D 写；「是不是 D」只用一条规则

- **P 上和 1.3.3 逐字段相同。** 新代码装到 P 上时，写出的记录和 T51 版逐字段相同：本节的阶段、字段都不写，也不写成 `skipped`、`null` 或 0；启动行也和 T51 版逐字相同（7.4，不是 D 的那一档）。用记录等价测试核。
- **判断函数：** `vllm.v1.metrics.pd_diagnostics.is_pd_decode(vllm_config)`，在 vLLM 补丁的 `pd_diagnostics.py` 里，三份补丁只调这一个函数，不各自判断。
  - **规则：** 调用时从 `sys.modules` 取 `vllm_ascend.utils`，返回它的 `is_pd_decode_recompute_scheduler_enabled(vllm_config)`（`d4ad49796` 的 `utils.py` 第 1217–1245 行：顶层 `kv_role == "kv_consumer"`，并且 `recompute_scheduler_enable` 为真）。模块没加载、没有这个函数或出异常时返回 `False`。只用这一个条件，不看调度器类。
  - 这和「D 跑 Recompute 一族」是同一个条件：`d4ad49796` 的 `platform.py` 第 1115–1145 行只在这个条件成立时把调度器换成 Recompute 一族（D 是 `AsyncRecomputeScheduler`）。
  - 不在模块导入时 import vllm_ascend。
  - 不只看 `recompute_scheduler_enable`：它是配置开关，不是角色；在 `kv_producer` 上配了它时，EngineCore 里的配置单例仍是真。共用条件另外要求顶层是 `kv_consumer`。
  - 只在初始化时调一次并保存：调度器初始化（vLLM 补丁），DeviceTiming 建立（vllm-ascend 补丁）。p2d 的新字段只在 D 的接收线程写，不需要判断。
  - vllm-ascend 补丁另要求 `_SPANS` 里已有 7.4 的 4 个 `runner.*`，缺一个就按假处理，整体回到 T51 的行为（补丁 01、03 版本不配套时不会半开）。
- **启动行是硬核对：** 取不到配置时函数返回 `False`，D 会悄悄退回 T51 的行为，不报错。所以第一批 D 重启以后，下面两条必须同时满足，不满足就停下：
  - EngineCore 启动行 `PD_DIAG_STARTUP scheduler=…` 是 Recompute 一族（D 为 `vllm_ascend.core.recompute_scheduler.AsyncRecomputeScheduler`）；
  - `tp_rank` 0 的 Worker 启动行 `runner_stages=` 列出 6 个 stage（7.4）。
  - EngineCore 一侧的结果启动行上看不出，由首批数据里 `kv_ready_wait` 有值来核（7.2）。
- **已有阶段的边界不动。** `stages.forward`、`stages.sampling` 的主机计时和设备计时仍只包住目标模型调用、sampler 调用；新加的段各是新阶段。

### 7.2 kv_ready_wait：`core_summary[scope=request].stages.kv_ready_wait`

由 EngineCore 写，在请求结束时那条 `core_summary[request]` 里（`Request.pd_queue_transition("finished")`），不新增日志行。两个条件都满足才写，否则没有这个阶段：这个实例是 D（7.1）；这个请求开过至少一轮（调度器得知 KV 到齐、读取失败、在 `WAITING_FOR_REMOTE_KVS` 里被中止，三者有其一）。

| 字段 | 单位、时钟 | 含义 | 核对用途 |
|---|---|---|---|
| `calls` | 次 | 发生过几轮「KV 到齐 → 开始运行」，通常为 1 | 区分重试 |
| `duration_ns` | ns | 第一轮的 `end_ns − start_ns`；没有终点时为 `null` | 段的取值 |
| `start_ns` | ns，EngineCore 的 `monotonic_ns` | 第一轮的起点：`_update_from_kv_xfer_finished` 收到 `finished_recving`、请求还在 `WAITING_FOR_REMOTE_KVS` 时（vLLM `scheduler.py` 第 3171–3176 行） | 「调度器得知 KV 到齐」，比字节到齐晚一步 |
| `end_ns` | 同上 | 第一轮的终点：运行它的那一次 `schedule()` 的开始，即补丁 01 包装里的 `start_ns`（第 589 行） | 等式：等于同一步 `core_summary[execution].stages.schedule.start_ns`；数值由单测核。现场按步的标识核：`ranges[0].scheduled_execution_id` 等于这个请求 EngineCore 一侧部分和的 `first_exec`（ClickHouse 里没有每步的 `schedule.start_ns`） |
| `status` | — | `ok`、`missing`、`error`、`cancelled`、`invalid` | 区分没有值的原因 |
| `ranges` | 列表，最多 64 项 | 每轮一项：`episode`、`start_ns`、`promoted_ns`、`end_ns`、`duration_ns`、`ready_execution_id`、`scheduled_execution_id`、`status`、`load_failed` | `promoted_ns` 区分「等下一次 `schedule()`」和「提升后又多等几步」；两个 `execution_id` 和步骤记录对上 |
| `dropped_ranges` | 项 | 超过 64 项丢掉的轮数 | 同 `queue` |
| `complete`、`end_observed` | 布尔 | 第一轮起点终点都有；第一轮有终点 | 同 `queue` |
| `timing_scope` | — | `"kv_ready_seen_to_scheduled"` | 标明口径 |

- 字段名、`status` 取值、按 `episode` 分项照已有的 `queue` 阶段；以第一轮为准，和 `queued_time` 不算抢占一致。
- D 的 `AsyncRecomputeScheduler` 沿 MRO 调到补丁 01 的 `schedule()` 包装，起点所在的方法都是继承的，vllm-ascend 不用改。
- 段的说明：kv_ready_wait从调度器得知 KV 到齐算起，比字节到齐晚一步；字节到齐在接收线程（墙钟）里（7.3）。

### 7.3 等 KV 的墙钟包络和 3 个 L2 的最大值：`pd_transfer_summary`

由 D Worker 的接收线程写，在已有的两种记录里。

| 记录 | 字段 | 单位、时钟 | 含义 | 为空 | 核对用途 |
|---|---|---|---|---|---|
| `request_generation` | `kv_load_envelope_ns` | ns，差值，D Worker 的 `monotonic_ns` | 请求被标记完成的时刻减去本请求所有任务中最早的 `queued_ns` | 没有标记完成时为 `null` | 段的取值：接收线程（墙钟） |
| `request_generation` | `kv_load_timing_scope` | 字符串常量 | `"first_task_queued_to_request_marked_done"` | 不为空 | 标明口径；现有 `timing_scope` 说明的是 `transfer_envelope_ns` |
| `request_generation` | `task_max_queue_wait_ns` | ns，差值 | 本请求（本 rank）各任务 `queue_wait_ns` 的最大值 | 没有一个任务开始处理时为 `null` | 段「receive_queue」的取值 |
| `request_generation` | `task_max_meta_fetch_ns` | 同上 | 各任务 `associated_stages.meta_fetch.duration_ns` 的最大值（已含 `metadata_request`） | 没有一个任务走到取对端信息时为 `null` | 段「取对端信息」的取值 |
| `request_generation` | `task_max_prepare_ns` | 同上 | 各任务 `associated_stages.prepare.duration_ns` 的最大值 | 没有一个任务走到组读表时为 `null` | 段「组读表」的取值 |
| `task` | `request_done_ns` | ns，原始时刻 | 请求被标记完成的时刻：p2d 第 924 行 `task_tracker.update_done_task_count` 之后。只在使请求完成的那条 task（`tracker_completed=true`）上有值 | 其余 task 为 `null`（`pd_transfer_summary` 的惯例：形状固定，不适用的字段写 `null`） | 让包络能从 task 记录复算 |

- 终点取标记完成，不取各任务 `end_ns` 的最大值：标记完成以后还要给 P 发结束信号、等 ACK，那段不属于等 KV。
- 三个最大值在请求汇总（`pd_request_tasks[request_id]`）里和任务计数、字节数、包络同一处并，在同一把锁里更新。
- 关系（同一请求、同一 D rank）：`kv_load_envelope_ns` ≥ `transfer_envelope_ns`；≥ 三个最大值各自；≤ 最晚的 `end_ns` − 最早的 `queued_ns`。
- 取对端信息只取 `meta_fetch`：`meta_fetch` 第 1172–1190 行已包含第 1179 行的 `metadata_request`。
- **这是「两种 connector 同一种格式」（第 0 节）的例外：** 这 6 个字段 hybrid 没有，是 V1 独有的。dsv4.1（hybrid）挂起，不受影响；过滤器、面板对 hybrid 的记录不要依赖它们。
- 重放：`replay_p2d.py` 出新版，新增一处修改，锚点是 `self.task_tracker.update_done_task_count(request_id)`；其余改的是补丁自己的代码。

### 7.4 prepare_inputs、lm_head、draft、bookkeeping：`worker_summary` 的 4 个 stage

| 面板上 | stage 名 | 记录 | 起止（`model_runner_v1.py`，T51 版） | span 事件名 |
|---|---|---|---|---|
| prepare_inputs | `prepare_inputs` | `worker_summary[phase=execute]` | 第 2290 行 `prepare input` 块开始 → 第 2533 行 `update_cos_sin` 之后 | `runner.prepare_inputs` |
| lm_head | `lm_head` | 同上 | 只包第 2639 行目标模型那一次 `compute_logits` | `runner.lm_head` |
| draft | `draft` | `worker_summary[phase=sample]` | 闭包 `propose_draft_token_ids`（第 2834 行）的函数体 | `runner.draft` |
| bookkeeping | `bookkeeping` | 同上 | 第 2878–2894 行，包住 `_bookkeeping_sync` | `runner.bookkeeping` |

- **字段：** 和 forward、sampling 相同，由 `Batch.add_stage` 生成；读到设备值时加 `device_duration_ns`，规则照第 1 节 C14（只在 V1 runner、`tp_rank` 0；读不到不写，不写 0；推迟写出最多 4 步）。6 段在同一条计算流上，设备区间互不重叠，可以相加。核对用途：段的取值；主机时长留作分析。
- **span：** vLLM 补丁在 `_SPANS` 里加 `"runner.prepare_inputs": "prepare_inputs"`、`"runner.lm_head": "lm_head"`、`"runner.draft": "draft"`、`"runner.bookkeeping": "bookkeeping"`。
- **助手：** 4 处都用 `with pd_device_timing.stage(self, "<名>"):`，打开时等价于 `with diag.span("runner.<名>"), self.measure("<名>")`；关掉时直接 `yield`，记录里没有这个 stage，主机时长也不写。不另加 `forward_context.capturing` 检查（服务期间不捕图）；T51 已有的捕图规则照旧（`npu.is_current_stream_capturing` 为真时不记设备事件）。forward、sampling 两处不动。
- **`pd_device_timing.py`：** `_PHASE_OF_STAGE` 加 4 项；每个 slot 从 2 对事件变成 6 对，SLOTS 仍为 4；已有规则照样适用于新 stage。
- **开关 `VLLM_PD_DEVICE_STAGES`**（DeviceTiming 建立时读一次，改了要重启；只在 `is_pd_decode(vllm_config)` 为真时起作用）：
  - 不设：4 段全开；
  - 4 个 stage 名中的若干个，用逗号分隔：只开列出的，没列的整段不写；
  - `none`：4 段全关，回到 T51（每步 2 对设备事件）；
  - forward、sampling 写进来也忽略；不认识的名字忽略、不报错。
- **启动行：** `PD_DIAG_STARTUP worker device_timing=<on|off> tp_rank=<n>` 按三档写：
  - D 上至少一段开着：末尾加 ` runner_stages=<开着的 stage，含 forward、sampling> source=<default|env>`，默认是 `runner_stages=forward,sampling,prepare_inputs,lm_head,draft,bookkeeping source=default`；
  - D 上设成 `none`：末尾加 ` runner_stages=forward,sampling source=env`；
  - 不是 D：不加，这一行和 T51 版逐字相同。

### 7.5 只记录、不上看板的字段

| 记录 | 字段 | 单位 | 含义 | 什么时候有 | 核对用途 |
|---|---|---|---|---|---|
| `worker_summary[sample]` | `step_device_span_ns` | ns，设备时长 | 本步 `prepare_inputs` 的 start 事件到 `draft` 的 end 事件 | 设备计时开着，并且 `prepare_inputs`、`draft` 都开着；读不到不写 | 核「6 段之和 ≤ 本步设备包络」；判断第一步多出的约 48 ms 是否落在段之间的缝里 |
| `worker_summary[execute]` | `cudagraph_runtime_mode` | 字符串 `"FULL"`、`"PIECEWISE"`、`"NONE"` | 本步目标模型实际用的图模式，取 runner 已经算好的 `CUDAGraphStat.runtime_mode` | D 的 `tp_rank` 0，4 个新 stage 至少开着一个时每步写；`none` 时不写 | 看第一步、第二步起各走什么模式（第一步每请求 1 个 token，不满足 uniform decode） |

- 只记录的字段，用不上看板的累加规则写进按请求的部分和，供核对查询用。记录格式不变。规则由 `segments.json` 定（t41），共 5 条：`step_device_span_ns` 1 条，分第一步、第二步起；`cudagraph_runtime_mode` 3 条，用 `where` 分 `FULL`、`PIECEWISE`、`NONE` 计步数；bookkeeping `stages.bookkeeping` 1 条，分第一步、第二步起。看板和 coverage 都不显示这些规则。
- bookkeeping `stages.bookkeeping` 照记（7.4），看板不显示；上面那第 5 条规则让它的现场数据进 ClickHouse，要上看板时只改段定义（10_ 第 8 节冲突 20）。
- 节点上不留每步的原值：过滤器是节点上唯一写 `[PD_DIAG]` 的地方，没有规则的每步记录不写出。按请求的部分和是这几个字段唯一的取数来源。
- `cudagraph_runtime_mode` 只代表目标模型：草稿 `_propose` 自己 dispatch（`llm_base_proposer.py` 第 1090–1111 行），只沿用目标模型的 `uniform_decode`。草稿的图模式不另记。
- P 上不管开关怎么设都不写（7.1）。

### 7.6 开销判据（glm53-test 升级手册里的两步）

- 没有开销自测字段，也没有 `VLLM_PD_DIAG_TIMING_COST`。
- 同一组请求、同样并发，升级前（T51 版）、升级后（4 段全开）各跑 3 遍，取 TPOT 中位数。升级后中位数 − 升级前中位数 > max(2% × 升级前中位数, 波动) 时，把 `VLLM_PD_DEVICE_STAGES` 改成 `lm_head,draft`。波动是同一版本 3 遍 TPOT 的最大减最小，两个版本各算一次，取大的。
- 2% 是阈值，不是预测：按 D 每步约 60 ms、新增设备事件记录每次几十微秒估的，没有量过。
- 改成 `lm_head,draft` 以后，关掉的段整段不写（含主机时长）：prepare_inputs、bookkeeping没有数据，看板上prepare_inputs为空，`step_device_span_ns` 也不写。只改环境变量，重启那批 D，不重出补丁包。

### 7.7 兼容

- 记录的 `version` 仍为 2；只加阶段和字段，不新增 `event` 种类。
- 旧过滤器（v3.1-extra）读新记录：新阶段、新字段不读，已有段的口径不变；原样写出的 `core_summary[request]`、`request_generation` 在 D 上每请求多约 0.8–0.9 KB（按文本长度估）。
- 新过滤器读旧记录或 P 上的记录：新段为空，不报错。P 节点也会收到新的 `segments.json`，新段的列为空，P 侧原来的 27 段不变。
- 过滤器代码不变；升级顺序先补丁、后面板。
- 服务期间不捕图：vllm-ascend `acl_graph.py` 第 184 行在捕图前调 `validate_cudagraph_capturing_enabled()`，开关只在启动时的捕图函数里打开。T51 打补丁手册（操作说明 A 第 7.3 节）里「服务中第一次遇到某个桶的那一步在捕获」这句解释不对；留空比例这个判据本身不受影响。

## 8. T58：粒度开关和 Mooncake 计时文件（1.5.0）

设计定稿：`docs/tasks/T58/T58_设计.zh-CN.md`（`f08bfac5`），用户定「T58，按照推荐」。记录的 `version` 仍是 2；只在低级别少写字段，没有字段改含义，也没有新的必需字段。

### 8.1 `PD_DIAG_LEVEL`

| 环境变量的值 | 生效的级别 | `source` |
|---|---|---|
| 不设 | 3 | `default` |
| 空串 | 3 | `default` |
| `3` | 3 | `env` |
| `2` | 2 | `env` |
| `1` | 1 | `env` |
| 其余，如 `0`、`4`、`abc`、` 2 `（带空格）、`2x` | 3 | `invalid` |

- vLLM、vllm-ascend 主体、p2d 都调 vLLM 补丁的 `pd_diagnostics.diag_level()`：进程里第一次用时读、缓存，改了要重启。Mooncake 在 C++ 里按同一张表解析（`pd_timing_level.h`）。两边用这张表的输入测，结果逐行相同。
- proxy 不读它，仍只由 `PD_PROXY_DIAG` 开关。
- **粒度是上限，组件自己的开关只能往下收：**
  - `VLLM_PD_DEVICE_STAGES` 的意思和默认列表不变；
  - `MC_PD_TIMING=0` 时 Mooncake 不写；
  - 粒度不是 3 时 Mooncake 也不写。

### 8.2 每一级不写的字段

| 级别 | 不写（也不为它计时） | 组件 |
|---|---|---|
| 2 级及以下 | Mooncake 计时记录（8.4） | Mooncake |
| 2 级及以下 | `pool_task` 的 `native_calls`：写成空列表（键保留，喂它的计数器照记），任何 `pool_task` 都一样。用到的是 `save` 上的；第 2 节读 `pool_task[load]` 不读它 | vllm-ascend 缓存池 |
| 2 级及以下 | `worker_summary` 的 `stages.bookkeeping` 段值。那一对设备事件照记，`step_device_span_ns` 的含义不变（用户定的做法乙） | vllm-ascend runner |
| 1 级 | `pool_task` 的 `stages`：只有 `build_key`、`lookup`、`prepare`、`sync`、`put` 这 5 个名字会进 `stages`，1 级不计时也不写，所以任何 `pool_task` 的 `stages` 都是空的。用到的是 `save` 上的；`load` 上没有任何一段读 `stages`（第 2 节用的是时长、计数、状态和 `members`） | vllm-ascend 缓存池 |
| 1 级 | D `worker_summary[execute]` 的 `stages.submit`（`kv.load_submit` 这个 span 在 D、1 级时不开，`start_load_kv` 照常调用） | vLLM（`kv_connector_model_runner_mixin.py`、V2 `gpu/kv_connector.py`） |
| 1 级 | D `pd_transfer_summary[request_generation]` 的 `task_max_queue_wait_ns`、`task_max_meta_fetch_ns`、`task_max_prepare_ns`、`transfer_envelope_ns` | p2d |

**任何一级都照写：**
- schedule 的子段：queue、hash、lookup、alloc、other，这是唯一的例外；
- `kv_load_envelope_ns`、`kv_ready_wait`；
- forward、sampling、prepare_inputs、lm_head、draft 的设备时长，`step_device_span_ns`、`cudagraph_runtime_mode`；
- 候选映射用的记录，即 `pd_transfer_summary` 的 link、task，和 `pool_task[load_submit]`；
- 其余 L1 段的来源。

各段从第几级起记，见设计乙第 2 节的两张表（P 27 段、D 29 段），t41 的 `segments.json` `from_level` 以它为准。

### 8.3 记录里的粒度、启动行

- **`diag_level`**（整数 1、2）：写进 `api_summary[scope=request]` 和 `core_summary[scope=request]`，**只在 1、2 级写**。3 级不写，缺省就是 3，所以 3 级的记录和 T56 逐字段相同，P 和 T51 逐字段相同。
- **过滤器自检行**（t41）每次都带 `diag_level`；`pd_file_open` 不带。
- **新启动行：** `PD_DIAG_STARTUP diag level=<n> source=<default|env|invalid>`，每个 EngineCore 进程、每个 Worker 进程各一行，API 不写。已有的启动行一个字不动。

### 8.4 Mooncake 计时记录（补丁 04b，替代第 5 节的 glog 行）

- **文件：** `/data/pd-diag/logs/mooncake/mc.<启动时刻>.<pid>.<seq>.log`，每个进程一个文件（`MC_PD_TIMING_DIR` 可改目录，供测试用）。
  - 写满 256 MiB 换 `seq`，从不改名，只追加。
  - 这个名字格式的文件超过 12 小时就删，换文件时、每 10 分钟各清一次，也删别的进程留下的；`ENOENT` 忽略。
- **行的格式：** `<本地 ISO 时间> (mooncake pid=<pid>) INFO [client_service.cpp:0] [PD_DIAG] {json}`。
  - 第一行 `pd_file_open`：`source`、`pid`、`file`、`seq`、`build`、`wall_ns`、`timestamp_ns`。
  - 每分钟一行 `mc_timing_stats`：`written`、`dropped`、`queue_max`。
- **计时记录：** 事件名 `mc_batch_put`、`mc_batch_upsert`，`version` 1。
  - 和 `pool_task[save].native_calls` 关联的字段，名字和格式和那边一致：`submitted_keys`、`submitted_bytes`、`native_tid`（`gettid()`）、`wall_ns`（调用里、finalize 以后读的 UTC 纳秒，落在调用的 `start_wall_ns`、`end_wall_ns` 之间）、`pid`（`getpid()`，就是调用 Mooncake 的 vLLM 进程）。
  - 其余字段：`phase1_start_batch_put_us` 到 `phase4_finalize_batch_put_us`、`total_us`、`timing_scope`（`normal_full`、`prefer_same_node_full`、`prefer_same_node_not_started`）、`status`、`complete`、`failed_keys`、`phase_status`（只有 not_started 有）。含义和第 5 节相同。
  - 原签名包装的记录照旧不带 `phase1…`、`total_us`，`complete=false`。
- **写入：**
  - 调用线程只拼行、放进 16384 行的有界队列，满了就丢、计数；
  - 每个进程一个写线程，`pthread_create` 以后 `pthread_detach`，每 200 ms 写一次，不 `fsync`；
  - 没有带析构的静态对象；退出时最后不到 200 ms 的行丢；
  - fork 以后子进程开自己的写线程和文件。
- glog 和标准错误里不再有 `[BatchPutTiming]`、`[BatchUpsertTiming]`；`MC_PD_TIMING=0` 时不退回标准错误。

## 修订记录

| 版本 | 日期 | 改动 |
|---|---|---|
| glm5.3 版 1 | 11-19 | 初版，由 dsv4.1 版 2.2 改写：测量点和行号换成 `d4ad49796` 和 v0.30.0；第 1 节写明默认 V1 runner 的调用链和 forward、sampling 的位置；第 2 节分出 P 的同步加载（不写 `load_submit`，P 的 wait_for_transfer 为空）；第 3 节写明 P 不写 pool_load 映射记录、D 的 consumer 映射记录由 `MooncakeConnectorV1` 写；新增第 4 节（`MooncakeConnectorV1` 的记录，格式同 hybrid）；第 5 节补上各 Mooncake wheel 自带的 glog；第 6 节相应更新 |
| glm5.3 版 1.1 | 11-19 | 第 2 节写入统筹定的 C11（P 的 submit 取 `pool_task[load]`，只取四种终态，取 rank 0，wait_for_transfer 为空，不取整步 `stages.submit`），C1 在 glm5.3 不用；第 4 节按 P/D 传输移植的设计改两处：同一请求前面的任务失败后不再读取的任务记 `skipped`（原为 `failed`），`do_virtual` 请求只有映射记录；`offset` 写明展平和任务数；3.4 节相应更新 |
| glm5.3 版 1.2 | 11-19 | 统筹定（字段取舍为 (c)）：本套补丁加的 5 个字段各写核对用途，第 0 节加「不再新增字段」的规则；第 1 节更正 `worker_summary` 由 runner 上的 `pd_execution`、`pd_sample` 装饰器生成，V1 runner 的装饰器由 vllm-ascend 补丁加；第 2.1 节 C11 补 `exception` 与 `backend_failure` 的区别；第 6 节补 3 项差异（`store_job_id`、无 `failed`、`not_processed`）。记录格式没有变 |
| glm5.3 版 1.3 | 11-19 | 统筹定的 C14：第 1 节加设备计时（`device_duration_ns` 及其核对用途、只在 V1 runner 的 rank 0、读不到不写、推迟写出最多 4 步、`timestamp_ns` 和 `native_tid` 为写出时、`keep_tp_rank` 的口径提醒、段优先取设备值）；第 6 节加第 7 项 |
| glm5.3 版 1.3.1 | 11-19 | 第 1 节 C14 一段补两句（统筹确认）：到期读不到设备值时照常写出、不带该字段也不写 0；面板按段回退到 host 值，并在过滤器自检行里数出回退步数。记录格式没有变 |
| glm5.3 版 1.3.2 | 11-19 | 按 P/D 传输移植的核对和顾问审查改第 4 节五处：4.1 步骤号和序号改为 `add_request` 的可选参数（没有时退回 `diag.current()`，值相同）；步骤号属性缺失时记 `null`；4.2 没有原生读取时记录里是 `status="missing"`、`unmeasured_calls=1`；V1 下 `native_calls` 至多 1 条、`native_calls_dropped` 恒为 0；`do_virtual` 只在 `kv_transfer_params` 带 `do_virtual=true` 时出现。记录格式没有变 |
| glm5.3 版 1.3.3 | 11-19 | 3.4 节加一条：D 的 RecomputeScheduler 在 `schedule()` 里把请求退回 P 时，该请求的 `core_summary[request]` 多 `role: "unknown"`、少 `no_forward`，过滤器和采集器都不读这两个键。记录格式没有变 |
| glm5.3 版 1.4.0 | 11-20 | T56，D 侧（设计定稿 `docs/tasks/T56/10_记录格式约定草案.zh-CN.md` `d805b26f`，实施开始时改定，后按精简把关、`32_精简落地决定` 和统筹之后的回复又改一次）：第 0 节加「已有阶段的边界不动」「T56 的内容只在 D 写」；新增第 7 节：「是不是 D」的判断函数（只用 vllm-ascend 的 utils 条件，取不到返回 `False`；启动行硬核对）、kv_ready_wait `kv_ready_wait`、包络和 F3 的三个最大值、`request_done_ns`、4 个 runner stage 和开关 `VLLM_PD_DEVICE_STAGES`、只记录的字段（用不上看板的累加规则写进按请求的部分和，span 1 条、图模式 3 条、bookkeeping 1 条）、开销判据（升级前后各 3 遍比 TPOT 中位数）、兼容；按顾问的精简把关不设开销自测字段、`VLLM_PD_DIAG_TIMING_COST` 和助手里的 capturing 检查；第 6 节加第 8、9 条。记录的 `version` 仍为 2 |
| glm5.3 版 1.5.0 | 11-20 | T58（设计 `docs/tasks/T58/T58_设计.zh-CN.md` `f08bfac5`，用户定「T58，按照推荐」）：新加第 8 节，粒度开关 `PD_DIAG_LEVEL`、各级不写的字段、记录里的 `diag_level`、新启动行，以及 Mooncake 计时记录改写进文件（补丁 04b，替代第 5 节的 glog 行）。记录 `version` 仍是 2 |
| glm5.3 版 1.5.1 | 11-20 | 8.2 节两处更正，按实施时的核对：D 的 `stages.submit` 由 vLLM 补丁写（`kv.load_submit`），组件一栏原写成 vllm-ascend；1 级不写的 `pool_task` 的 5 个 stage、2 级及以下不写的 `native_calls` 都不只限于 `save`，任何 `pool_task` 都一样（`load` 上没有段读它们；`native_calls` 写成空列表，键保留）。各级写不写的规则不变，记录格式没有变 |
