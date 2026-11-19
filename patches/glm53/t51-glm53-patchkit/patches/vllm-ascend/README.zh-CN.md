# vllm-ascend 补丁（glm5.3，基线 `d4ad49796`）

| 项 | 内容 |
|---|---|
| 合并补丁 | `t51-vllm-ascend-d4ad49796-pd-diag.patch`：11 个源文件，加 9 个测试文件（改 3 个、新增 6 个） |
| 单文件补丁 | `per-file/01`–`11` 每个源文件一份，`per-file/90-tests.patch` 是全部测试。每一份都能在干净的 `d4ad49796` 上单独 `git apply` |
| 基线 | vllm-ascend `d4ad49796`（`releases/v0.30.0` 分支），公开仓库 |
| 替换件 | `files/vllm_ascend/...`：打完补丁后的 11 个源文件，其中 `diagnostics.py`、`worker/pd_device_timing.py` 是新文件 |
| 校验表 | `SHA256SUMS.before`：9 个被改文件在 `d4ad49796` 上的原值；`SHA256SUMS.after`：11 个文件打完后的值；新文件另列在 `NEW_FILES.txt`（2 个，没变） |
| 可选件 | `optional/02-mooncake-hybrid-connector.patch`：补丁 02 的 `MooncakeHybridConnector` 部分及其测试。glm5.3 用的是 `MooncakeConnectorV1`，**默认不打** |
| 重放工具 | `replay/`：按文本锚点重放缓存池 4 个文件和 `core/recompute_scheduler.py` 的改动，供现场文件带本地修改、`git apply` 打不上时使用（第 9 节） |
| 不含 | `p2p/mooncake_connector.py`（另一个 worker 负责）；补丁 02 的两份文档；补丁 03 |

缩写同 T51 方案：`store/` 指 `vllm_ascend/distributed/kv_transfer/kv_pool/ascend_store/`，`worker/` 指 `vllm_ascend/worker/`，`p2p/` 指 `vllm_ascend/distributed/kv_transfer/kv_p2p/`。行号都是打完补丁后的行号。

## 0. 和 dsv4.1 版、和 vLLM 补丁的关系

- **依据的约定。** glm5.3 记录格式约定「glm5.3 版 1」（`t51-glm53-kit` 的 `CONTRACT.zh-CN.md`，提交 `2218b50`），它由 dsv4.1 版 2.2 改写。本补丁负责其中第 1 节（等 put 完成、V1 runner 的 forward 和 sampling，含 C14 的设备事件计时）、第 2 节（缓存池加载任务，含 2.1 节 P 的同步加载）、3.3 节（`pool_load`）和保存任务记录；第 4 节（`MooncakeConnectorV1`）不在本补丁里。
- **目标。** 写出的记录和约定、和 dsv4.1 版（vllm-ascend `f9c8e0bdb` 上的移植）相同。两版跑同一组场景逐字段比较的结果见第 4 节。
- **`store/diagnostics.py` 和补丁 02、dsv4.1 版逐字节相同**（sha256 `bdfa9b6f…`）。记录的形状大部分由这个文件决定。
- **必须和 vLLM 补丁一起装。** 本补丁用到的下列名字都来自补丁 01（`pd_claim_finish` 来自 vLLM 移植版加的 A2 开关，见第 10 节），glm5.3 的 vLLM v0.30.0 移植版必须保留同样的名字和用法，否则 Worker 启动时 ImportError：
  - `vllm.v1.metrics.pd_diagnostics`：`current`、`current_batch`、`scope`、`batch`、`Batch`（`add_stage`、`finish`）、`emit`、`span`、`new_id`、`_LOG`（测试用）；
  - `vllm.v1.worker.worker_base`：`pd_execution`、`pd_sample`；`pd_claim_finish`（只有它缺失时，设备事件计时自动关闭，其余照常）；
  - `SchedulerOutput.pd_execution_id`。
  测试用的 `pd_diagnostics.py` 取自补丁 01 的新文件，git blob 与补丁里的 `5d465d1e` 相同。

## 1. 每个文件怎么处理

`d4ad49796` 比补丁 02 的基线 RC1 早，结构差别大：层式代码没有单独的 `store/layerwise/` 目录，层式线程类在 `store/kv_transfer.py` 里，层式 worker 逻辑并在 `store/pool_worker.py` 的 `KVPoolWorker` 里（`use_layerwise` 分支）；V1 runner 在 `worker/model_runner_v1.py`。所以补丁 02 的 `layerwise/kv_transfer.py`、`layerwise/pool_worker.py` 两个文件的改动分别并入这两个文件。

| # | 文件 | 对应补丁 02 / dsv4.1 的哪部分 | 冲突原因 | 处理 |
|---|---|---|---|---|
| 1 | `store/diagnostics.py` | 02 新文件 | 无 | 原样 |
| 2 | `store/metadata.py` | 02 `metadata.py` | 上下文不同 | 02 的全部改动：`prepare_value`、`prepare_value_layer` 计 `prepare`；`process_tokens`、`process_token_key_strings_with_block_ids` 计 `build_key`；`ReqMeta.pd_execution_id`；`AscendConnectorMetadata(pd_execution_id=…)`，`add_request` 时补到请求上 |
| 3 | `store/kv_transfer.py` | 02 `kv_transfer.py` + 02 `layerwise/kv_transfer.py` + dsv4.1 的等 put 完成 | ① 基类没有 `_request_submit_lock`、`_stopping`、`stop()`；② 保存线程没有保存任务号和已保存水位，`_handle_stored_request` 不返回成败；③ 层式线程类在本文件里，等待写成内联的循环，没有 `_wait_for_event` | 基类：入队时取快照、出队时开始计时，逻辑和 dsv4.1 逐行相同，用一把新锁 `_diagnostic_lock` 代替 `_request_submit_lock`；致命错误后不再取快照（和 dsv4.1 拒收新提交的效果相同）；`batch_copy`、`lookup` 计时同 02。保存：`stale_job`、`exception` 两种结果同 02；`failed` 无从判断，不写（第 4 节第 3 条）；完成记录（`completions[]`）在计数减一之后写。等 put 完成：`wait_for_requests_saved`（:1047）写法和 dsv4.1 逐行相同。层式：`LayerBatchBuilder.build_shared`/`build_addrs` 计 `prepare`，`build_cached_process_tokens` 计 `build_key`，每处 `sync_save_events[...].synchronize()` 计 `sync`，按 key 的 put/get 走 `pool_diag.put`/`get`，等保存完成、等注意力起算的循环计 `wait`（只进 `associated_stages`），整层拷贝失败记 `failed` |
| 4 | `store/pool_worker.py` | 02 `pool_worker.py` + 02 `layerwise/pool_worker.py` | 上下文不同；没有 PP 本地空组检查、没有 memcache 失败分支、`_load_kv_tp_mismatch` 不返回成败 | `start_load_kv`（:1264）每个请求一个任务：`load_async` 为真记 `load_submit`，为假记 `load`；`wait_for_save`（:3192）记 `save_submit`；TP 不一致时的 get/put 计时；层式各处（`_filter_pool_existing_keys`、put/get 会话准备、共享数据准备、最后一层的保存等待）同 02 |
| 5 | `store/pool_scheduler.py` | 02 + dsv4.1 的 `pool_load` | 上下文不同 | `AscendConnectorMetadata(pd_execution_id=…)`；`_emit_load_links`（:1118）和 dsv4.1 逐行相同，在 `build_connector_meta` 末尾调用（:1115） |
| 6 | `store/backend/mooncake_backend.py` | 02 `mooncake_backend.py` | 文件 653 行（RC1 927 行），没有按配置字典初始化的那条路径 | 计时同 02：整 key 的 put 包 `mooncake_put`（:454，约定第 4 节靠它配 `[BatchPutTiming]`），get 包 `mooncake_get`（:503），层式批量调用、`batch_get_session_end` 包对应的 operation。另有两处取舍见第 5 节 |
| 7 | `store/backend/mooncake_layerwise.py` | 02 `mooncake_layerwise.py` | 函数的类型注解从 `LayerwiseKVPoolWorker` 改成了 `KVPoolWorker`，循环体相同 | 同 02：`_prepare_group_sessions` 每个请求一个 `session_prepare` 任务 |
| 8 | `worker/model_runner_v1.py` | 02 `worker/v1/model_runner.py`；C14 | 路径不同 | 同 02：`execute_model` 加 `@pd_execution`，`sample_tokens` 加 `@pd_sample`，`_sample` 里两种 sampler 包 `runner.sampling`，`_model_forward` 里两处 `run_model()` 包 `runner.forward`。C14：在这几个 span 里面紧贴目标调用加 `pd_device_timing.measure`，另在 `execute_model`、`sample_tokens` 加读取和写出的挂钩（第 10 节） |
| 10 | `worker/pd_device_timing.py` | C14 新文件 | 无 | 设备事件计时（第 10 节）。只用标准库，torch 和 vLLM 在运行时导入 |
| 11 | `core/recompute_scheduler.py` | vLLM 补丁 01 在 `Scheduler.schedule()` 里的调度器记录 | 这个类有自己的一份 `schedule()` 正文，不调 vLLM 的，01 的调度器记录在 D 上全部缺失 | 正文改名为 `_schedule()`，`schedule()` 经 01 的包装；01 在正文里的 5 处原样加在同样的位置（第 11 节）。调度逻辑不变 |
| 9 | `worker/v2/model_runner.py` | 02 V2 部分 | `sample_tokens` 上没有 `@runtime_guard_sample_tokens`；`rejection_sampler` 少一个 `invalid_drafts` 参数（上游差别） | 同 02：`@pd_sample`（:349）、`@pd_execution`（:597）、`sample()` 里两种 sampler 包 `runner.sampling`（:1108、:1114）。这个 `sample()` 只在 `lmhead_tp_enable()` 时生效，否则走 vLLM 父类；V2 的 forward 在 vLLM v0.30.0 的 `v1/worker/gpu/model_runner.py`，属于 vLLM 补丁 |
| — | `p2p/mooncake_hybrid_connector.py` | 02 | 能原样打上 | 放在 `optional/`，默认不打 |
| — | 测试 | 02 的 `test_backend.py`、`test_mooncake_pipeline.py`、`test_pool_worker.py`；dsv4.1 新加的两个；新加 `test_pool_load_scope.py`、`test_pool_measurement_points.py`、`tests/ut/worker/test_pd_diag_runner_points.py`（第 6 节） | 3 个测试文件冲突 | 02 的测试用例逐个移到 `d4ad49796` 的测试文件里，只改 import 路径和夹具差别；不搬 `test_preferred_segment_uses_registered_name_after_auto_port_setup`（它测的是没搬的 `local_seg` 改动，第 5 节）；02 的空尾层用例补上 `LoadSpec` 必填的 `can_load=True`（RC1 的 `LoadSpec` 同样必填）。`test_compact_diagnostics_standalone.py` 主要测 hybrid connector，随 `optional/` 走 |

## 2. 测量点对应哪一段

| 段（名单） | 记录 | 测量点 | glm5.3 P（`load_async=false`） | glm5.3 D（`load_async=true`） |
|---|---|---|---|---|
| forward（L1） | `worker_summary[phase=execute].stages.forward`：host `duration_ns`，V1 另有 `device_duration_ns`（C14） | V1 `_model_forward` 里两处 `run_model()`；V2 在 vLLM，只有 host 值 | 有 | 有 |
| sampling（L1） | `worker_summary[phase=sample].stages.sampling`：同上 | V1 `_sample`（只在 `sample_tokens` 里调用）；V2 `sample()` 或 vLLM，只有 host 值 | 有 | 有 |
| 等 put 完成（L1） | `worker_summary[phase=execute].stages.wait_for_put`（约定第 1 节） | `store/kv_transfer.py:1047`；调用链 V1 `model_runner_v1.py:2285` → `store/ascend_store_connector.py:290` → `store/pool_worker.py:3174`、`:3189` → `:1047` | 有 | 有（glm5.3 的 P、D 都是 `kv_both`，约定第 1 节） |
| start_load_kv/submit（P，L2） | 见第 3.1 节 | `store/pool_worker.py:1264` | `pool_task[operation=load]`，含实际传输 | 不适用（D 的 submit 来自 P→D connector） |
| start_load_kv/wait_for_transfer（P，L2） | — | — | 无此段（同步加载） | — |
| 后台 wait_for_save（P，L1） | `pool_task[operation=save].duration_ns` | 入队 `store/kv_transfer.py` `add_save_batch`，处理 `_handle_request`、`_handle_stored_request`（:1188） | 有 | — |
| └ build_key / lookup / prepare / sync / put（L2） | `pool_task[save].stages.<名字>` | `metadata.py:600`、`:651`（build_key），`kv_transfer.py:884`（lookup），`metadata.py:484`（prepare），`kv_transfer.py:1351`（sync），`:1352`（put） | 有 | — |
| put 下 3 段（L3） | `pool_task[save].native_calls[operation=mooncake_put]` 配补丁 04 的 `[BatchPutTiming]` | `store/backend/mooncake_backend.py:454` | 有（要打补丁 04） | — |
| 保存提交 | `pool_task[operation=save_submit]` | `store/pool_worker.py:3192` | 有 | 有 |
| 首次候选映射（C2） | `pd_transfer_summary[scope=link, transfer_role=pool_load]` | `store/pool_scheduler.py:1118` | **不写**（同步加载） | 只在 D 直接命中缓存池时写 |
| D 的 start_load_kv、wait_for_transfer | — | `p2p/mooncake_connector.py` | — | 另一个 worker |

## 3. glm5.3 上的口径

### 3.1 P 同步加载时，加载记录是 `load` 不是 `load_submit`

按补丁 02 的原设计（`pool_worker.py:1264`），`start_load_kv` 对每个要加载的请求开一个任务，operation 在 `load_async` 为真时是 `load_submit`，为假时是 `load`。所以：

| | dsv4.1 的 P、glm5.3 的 D（`load_async=true`） | glm5.3 的 P（`load_async=false`） |
|---|---|---|
| `start_load_kv` 里写的记录 | `pool_task[operation=load_submit]`，`status=submission_returned`，只含入队 | `pool_task[operation=load]`，`status` 为 `succeeded`、`backend_failure` 等，**含实际的 get** |
| `associated_stages.backend_get` | 不在 `load_submit` 里，在后台的 `load` 里 | 在这条 `load` 里，`duration_ns` 小于等于任务的 `duration_ns` |
| `queue_enter_ns`、`counters.queue_wait_ns`、`counters.queue_entries` | 后台的 `load` 有 | `queue_enter_ns` 为 `null`，后两个没有 |
| 后台加载线程的记录 | 每个任务一条 `pool_task[load]` | 无 |
| `pool_load` 映射记录 | 写 | 不写 |
| 请求在不在这一步的 `request_ids` 里 | 不在（首次候选空步） | 在（同一步加载并执行） |

**统筹已定的取法（C11）。** P 的 start_load_kv/submit 取 `pool_task[operation=load]` 的 `duration_ns`，只取终态为 `succeeded`、`backend_failure`、`unknown_result`、`exception` 的记录，每个请求一条，取 rank 0；P 的 wait_for_transfer 为空。本补丁按补丁 02 的原设计写记录，不为 C11 加记录。

**终态怎么来（`d4ad49796` 的真实 `MooncakeBackend.get` 加假的 Mooncake 对象实测）：**

| 情况 | `status` | 依据 |
|---|---|---|
| 请求没有 `load_spec` | `missing_load_spec` | `pool_worker.py` 循环开头 |
| `can_load` 为假 | `skipped` | 同上 |
| 没有要取的 key | `no_task` | `if not key_list` |
| get 全部成功 | `succeeded` | 外层 get 的计数 |
| 有 key 失败（Mooncake 返回负数） | `backend_failure` | `get_failed_keys` |
| Mooncake 的 get 抛异常 | `backend_failure` | `MooncakeBackend.get` 自己捕获异常、返回 `None`；内层 `mooncake_get_exceptions` 为 1，外层 `get_unknown_result_keys` 为 key 数，失败优先 |
| store 还没初始化（不调 Mooncake，返回 `None`） | `unknown_result` | `get_unknown_result_keys` |
| 异常在 backend 之外抛出（如准备地址时） | `exception`，异常照常向上抛 | `processing` 捕获后重新抛出 |

所以 `exception` 只在 backend 之外出错时出现；Mooncake 本身出错记为 `backend_failure`。

**测试。**
- `test_pool_load_scope.py` 3 个用例：get 固定耗时 5 ms 的假存储，同步时 `load` 的时长包含 get；异步时 `load_submit` 不含 get，后台 `load` 含 get；`pool_load` 只在异步时写。
- `test_pool_measurement_points.py` 的 `TestSyncLoadRecords` 4 个用例，对应 C11 要证明的三点：
  1. 每个请求一个任务、它自己的 get 在里面：3 个请求，每次 get 固定 10 ms。每个任务的 `[start_ns, end_ns]` 包住自己那次 Mooncake 调用，任务之间不重叠。get 拿到的地址就是 `prepare_value` 算出的 KV cache 地址（基址 + 块号 × 块长），也就是说取到设备上就是这次 get 本身，同步分支里没有另一次拷贝。`tp_mismatch` 分支的加载同样在任务里。
  2. 上表各种终态逐个验证。
  3. 合成的一步：照 vLLM 补丁 01 的写法，在 `diag.batch("worker_summary")` 里用 `diag.span("kv.load_submit")` 包住 `start_load_kv`，5 个请求（3 个命中、1 个没有 `load_spec`、1 个 `can_load` 为假）。每个任务都落在整步 `stages.submit` 的时间窗口里，任务时长之和不超过 submit。两者之差是循环里取快照、写记录和函数入口的开销，小于一次 10 ms 的 get，所以没有 get 落在任务外面。容器里一次实测（假存储，非真实环境）：submit 32.117 ms，5 个任务之和 31.626 ms，C11 选中的 3 个之和 31.595 ms，任务外 0.491 ms（`ut_results/measurement-points.txt`）。
- 和 C11 有关的已知情况：P 上 `MultiConnector` 里 `MooncakeConnectorV1.start_load_kv` 也在 vLLM 的 `kv.load_submit` 里（约定 2.1 节），所以真实环境里 submit 与任务时长之和的差还包含它，这个测试没有覆盖。

### 3.2 投机解码（deepseek_mtp）下 forward、sampling、draft 怎么记（读码）

- **forward 每步一次。** `_model_forward` 只在 `execute_model` 里调用一次（`model_runner_v1.py:2598`），另一个调用方是 `_dummy_run`（:4278）。D 的 verify（1 + k 个位置）就在这一次 forward 里。P（k=1）和 D（k=5）都一样。
- **sampling 在 sample 阶段。** `_sample` 只在 `sample_tokens` 里调用（:2816），所以 `stages.sampling` 落在 `worker_summary[phase=sample]`，这条记录由 `@pd_sample` 带上同一个 `execution_id`。
- **draft 不在 forward、sampling 里。** `sample_tokens` 在 `_sample` 返回后才调 `propose_draft_token_ids`（:2873、:2901、:2905，经内部函数 :2831 调 :1898）。`deepseek_mtp` 在 vLLM 里归为 `mtp`，vllm-ascend 选 `AscendEagleProposer` 或 `AscendMultiKVCacheGroupMTPProposer`（`vllm_ascend/spec_decode/__init__.py:54–60`），draft 模型由 proposer 自己调用（`vllm_ascend/spec_decode/llm_base_proposer.py:1491`、`:1743`），不经过被包住的 `run_model()`。draft 时间在 sample 阶段的记录里，但不在任何阶段里，落在「未归属」。P 的请求只出 1 个 token，draft 照样执行。
- **保存提交在 MTP 下落在 sample 阶段。** V1 在投机解码时把 connector 的收尾推迟（:2572 `defer_finalize`），在 `sample_tokens` 里调 `finalize_kv_connector()`（:2911）。所以 `pool_task[save_submit]` 和 vLLM 的 `kv.save_wait` 写在 sample 阶段的上下文里，`execution_id` 仍是这一步的。
- V2 同理：forward 在 vLLM `gpu/model_runner.py`，draft 由 `AscendMTPSpeculator`（`worker/v2/spec_decode/mtp/speculator.py:25`）自己调模型。
- 静态核对：`tools/check_runner_points.py`，12 项全部通过（`ut_results/runner-points.txt`）。

## 4. 记录格式和 dsv4.1 版逐字段对比

**做法。** `ut_harness/t51_record_shapes.py` 在两棵树上跑同一组 9 个场景：dsv4.1 版（`f9c8e0bdb` + dsv4.1 补丁）和本补丁（`d4ad49796` + 本补丁）。被测的是真实代码，存储、token 数据库、NPU 事件是假件，其中 2 个场景走真实的 `MooncakeBackend.put`/`get`。`tools/compare_record_shapes.py` 按路径逐个字段比较；时钟、id、pid 这类每次都变的字段只比是否存在和类型。结果在 `ut_results/record-shapes-compare.txt`。

| 场景 | 记录 | 差异 |
|---|---|---|
| save（整 key 保存，一个请求） | `pool_task[save]` | 0 |
| save_mooncake_backend（经真实 `MooncakeBackend.put`） | `pool_task[save]`，含 `native_calls[mooncake_put]` | 0 |
| load_async_mooncake_backend | `pool_task[load_submit]`、`pool_task[load]` | 0 |
| save_submit | `pool_task[save_submit]` | 0 |
| load_async | `pool_task[load_submit]`、`pool_task[load]` | 0 |
| load_sync | `pool_task[load]` | 0 |
| wait_for_put | `worker_summary[execute]` | 0 |
| pool_load_link | `pd_transfer_summary[link, pool_load]` | 0 |
| save_twice（同一请求、同一段 token 保存两次） | 两条 `pool_task[save]` | 第二条 35 处，原因见下表第 1、2 条 |

**逐条差异：**

| # | 差异 | 原因 | 影响 |
|---|---|---|---|
| 1 | `store_job_id`：dsv4.1 从同一请求的第二个保存任务起，在记录顶层（单成员时）、`members[]`、`completions[]` 带本地任务号（-2、-3…；第一个是 -1，`diagnostics.py` 把 -1 当作没有）；glm5.3 从不带 | `d4ad49796` 没有保存任务号 | 约定 2 节已写「有时带 `store_job_id`」；v3 的段不用它 |
| 2 | 同一段 token 第二次保存：dsv4.1 记 `status=no_task`（已保存水位跳过），glm5.3 照常查池并 put | 两版的业务逻辑不同，`d4ad49796` 没有已保存水位 | 记录格式相同，取值随业务逻辑 |
| 3 | 整 key 保存失败时，dsv4.1 记 `status=failed`；glm5.3 记 `backend_failure`（put 有失败 key）或 `unknown_result` | `d4ad49796` 的 `_handle_stored_request` 不返回成败，只能由计数推出状态 | 取值都在约定第 2 节的状态集合里；成功时两版都是 `succeeded` |
| 4 | 线程停止时，dsv4.1 为队列里没处理的提交写 `status=not_processed`；glm5.3 只在线程因异常退出时写 | `d4ad49796` 的线程没有 `stop()` | 只影响进程退出时的少量记录 |
| 5 | 层式路径的等待：dsv4.1 由 `_wait_for_event` 计 `wait`；glm5.3 由内联循环外的 `stage("wait")` 计，名字相同 | 函数结构不同 | 只在 `associated_stages` 里，不是展示段；glm5.3 没开层式时不出现 |

## 5. 两处取舍（不是计时，但影响记录或行为；顾问已同意，按交付现状定稿）

1. **补 `MooncakeBackend.put` 的返回值（Q1）。** d4a 原来的 put 没有返回值，补上以后只给计时用，调用方不读，业务行为不变。依据：
   - `d4ad49796` 的 `put` 返回 `None`；
   - 外层的 put 计时（`diagnostics.py` 的 `put` → `backend("put", …)`，产生 `stages.put` 和 `put_*` 计数）拿到 `None` 时，`BackendCall.result(None)` 直接返回，`result_status` 停在初始的 `unknown_result`，`backend()` 收尾时记 `put_unknown_result_keys`，于是保存成功的任务也记成 `status=unknown_result`。两版容器实测对比：`ut_results/record-shapes-compare-without-put-return.txt`，9 处差异；补上以后为 0；
   - 补上的函数签名返回类型（`-> list[bool] | bool`）和两条 return 语句（`return [int(value) >= 0 for value in res]`、异常分支的 `return False`）与 `f9c8e0bdb` 逐字相同，另加了一行注释；
   - `d4ad49796` 里 put 的 3 个调用方（`kv_transfer.py` 两处、`pool_worker.py` 一处）都不读返回值。
   不改共用的 `diagnostics.py`。
2. **不搬补丁 02 的 `local_seg` 改动（Q2），而且必须不搬。** 02 在 Mooncake 初始化后把 `self.local_seg` 改成 `store.get_hostname()`。理由：
   - 它修的是分配行为，不是计时。`local_seg` 只用来在开了 `preferred_segment` 时设置分配目标（`d4ad49796` `mooncake_backend.py:350–351`）。共享 TE 那条初始化路径本来就以 `ip:rpc_port` 注册（`:265–267`）；只有 fabric 或独立 TE 路径用的是裸主机名，02 的改动改的正是这条路径上的分配目标；
   - 不搬不影响计时。补丁 04 里 `[BatchPutTiming]` 的 `timing_scope` 由 `prefer_alloc_in_same_node` 决定，和 `preferred_segment` 是两个独立的开关（`mooncake_backend.py:350–352` 分别设置）；
   - 搬了反而会改变现网的分配行为，测出来的就不是现网的样子。诊断补丁不改分配行为。
   02 里测这一处的用例（`test_preferred_segment_uses_registered_name_after_auto_port_setup`）也不搬。dsv4.1 版搬了这处。

另有两处防御性写法，打了 vLLM 补丁时不起作用：`pool_scheduler.py` 取 `pd_execution_id` 用 `getattr(..., None)`；层式保存等待取 `tp_rank` 用 `getattr`。原因是 `d4ad49796` 自带的测试用 `SimpleNamespace` 构造对象，缺这两个属性时会让 3 个原本通过的测试失败。

## 6. 每个测量点由哪个测试证明

| 测量点（段） | 测试 | 证明的内容 |
|---|---|---|
| forward（V1） | `tests/ut/worker/test_pd_diag_runner_points.py` `test_v1_forward_covers_only_the_target_model_call` | 从源码编译出 `_model_forward`，假模型固定耗时 20 ms：`stages.forward` 包住模型调用，不包 `_update_full_graph_params_if_needed`（`enable_enpu` 两种取值都测） |
| sampling（V1、V2） | 同文件 `test_v1_sampling_covers_only_the_sampler_call`、`test_v2_lmhead_sampling_covers_only_the_sampler_call` | `stages.sampling` 包住 sampler 或 rejection sampler，`algorithm` 取值对；不包 draft 概率、更新 token、算 logits |
| forward、sampling、等 put 的位置 | 同文件 `RunnerPlacementTest` 3 个 | `execute_model`/`sample_tokens` 的装饰器；`handle_preemptions` 在 `execute_model` 里；`_sample` 只在 `sample_tokens` 里调用；MTP 的 draft 不在任何 span 里；import 的 vLLM 名字 |
| 等 put 完成 | `test_wait_for_put_standalone.py` 4 个；`test_pool_measurement_points.py` `TestWaitForPutChain` 2 个 | 函数本身的计时和状态；经真实的 `AscendStoreConnector.handle_preemptions` → `KVPoolWorker.handle_released_saves` → `wait_for_requests_saved`，等待 20 ms 写进这一步的 `worker_summary[execute]`，`released_requests` 对；没有要等的请求时照样写 |
| P 的 start_load_kv/submit（同步加载，C11） | `TestSyncLoadRecords` 4 个；`test_pool_load_scope.py` 3 个 | 第 3.1 节 |
| 后台 wait_for_save 和 build_key / lookup / prepare / sync / put | `TestSaveRecords.test_each_save_stage_measures_only_its_own_call` | 真实的 `ChunkedTokenDatabase` 计时包装、保存线程和 `MooncakeBackend`，5 个阶段各有固定耗时：每段不少于自己的耗时，五段的时间窗口按 build_key → lookup → prepare → sync → put 先后排开、互不包含，lookup 和 put 各自包住对应的 Mooncake 调用；任务时长包住全部；`queue_wait_ns` 不少于入队后等待的 30 ms |
| put 下 3 段的配对字段 | `TestSaveRecords.test_mooncake_put_native_window_matches_the_native_call` | `native_calls[mooncake_put]` 的单调时钟和墙钟窗口都包住 Mooncake 调用；`native_tid` 等于调用线程；`submitted_keys`、`submitted_bytes` 对 |
| `pool_load` 映射（D） | `test_pool_load_link_standalone.py` 6 个；`test_pool_load_scope.py` | 只在异步、非层式时写；字段 |
| D 的异步加载（`load_submit`、`load`） | `test_pool_load_scope.py` | 第 3.1 节 |

**反向对照。** 把上表新加的测试放到没打补丁的 `d4ad49796` 上（只拷入 `diagnostics.py` 让 import 能过），runner 的 6 个、缓存池的 11 个全部不通过（`ut_results/measurement-points.txt`），说明这些测试确实依赖补丁里的测量点。

**时间断言的写法。** 「不包含别的阶段」用时间窗口的先后和包含关系判断，不用耗时上界，不受机器负载影响；耗时只断言下界（不少于假调用固定的耗时）。`test_pool_measurement_points.py` 连跑 20 次全部通过。

## 7. 验证

都在 `docker run --rm --network none` 的容器里做，结果文件在 `../../ut_results/`。

| 项 | 结果 |
|---|---|
| `git apply --check`（`python:3` 容器，从公开仓库对象克隆、检出 `d4ad49796`，工作区干净；脚本 `../../tools/apply_check.sh`） | 合并补丁（20 个文件）通过；`per-file/` 的 12 份每份单独通过；`optional/` 通过；`SHA256SUMS.before` 9 个在干净树上对上；`NEW_FILES.txt` 的 2 个在干净树上不存在；打完后 `SHA256SUMS.after` 11 个对上，和 `files/` 逐字节相同；`per-file/01`–`11` 依次打上的结果也和 `files/` 相同（`apply-check.txt`） |
| `tests/ut/distributed/ascend_store` 全部（`python:3.12-slim`，torch、vLLM 用测试自带的桩加 `ut_harness/t51_glm_stubs.py` 补的桩，`pd_diagnostics` 用真实模块） | 干净的 `d4ad49796`：545 通过、73 失败、1 个收集错误；打补丁后：578 通过、73 失败、1 个收集错误。失败和收集错误的清单逐条相同，都是桩环境造成的（如桩 torch 没有 `arange`、`bfloat16`）。多出的 33 个通过就是本补丁在这个目录里带的测试，见下一行 |
| 本补丁带的测试（C14 之前的 39 个，全部通过；C14 另加的见第 10 节） | 补丁 02 的：`test_backend.py` 9 个、`test_mooncake_pipeline.py` 1 个、`test_pool_worker.py` 2 个新加 + 1 个改写；dsv4.1 新加的：`test_wait_for_put_standalone.py` 4 个、`test_pool_load_link_standalone.py` 6 个；新加：`test_pool_load_scope.py` 3 个、`test_pool_measurement_points.py` 8 个、`tests/ut/worker/test_pd_diag_runner_points.py` 6 个（后者只用标准库，用 `python -m unittest` 跑） |
| 独立测试直接用 `python -m unittest` 跑（`standalone.txt`） | `test_wait_for_put_standalone.py` 4/4、`test_pool_load_link_standalone.py` 6/6；`test_compact_diagnostics_standalone.py` 在加了 `optional/` 的树上 7/7（不加时它的 5 个 hybrid 用例无从运行） |
| 记录格式对比 | 第 4 节 |
| runner 测量点 | `test_pd_diag_runner_points.py` 9/9 通过（含 C14 的 3 个），`test_pd_device_timing.py` 16/16 通过；`tools/check_runner_points.py` 12/12 通过，同一脚本在干净树上有 7 项不过 |
| 重放工具 | 在干净副本上运行 `replay/replay.sh`，再打 `per-file/06`–`10`，11 个文件和 `files/` 逐字节相同 |
| RecomputeScheduler（第 11 节） | 调度器记录比对 363 项通过、2 项按约定预期不同、0 项不过；vllm-ascend 自己的 `tests/ut/core` 在两种基类下补丁前后逐个相同；联合检查（vLLM 65c8e87 + 11 个文件 + p2d a9e9695）D 走 `AsyncRecomputeScheduler`，两种基类各 130/130；pyflakes：这 11 个文件补丁后没有新增警告（`pyflakes.txt`） |

## 8. 没有验证的

- **C14 设备事件计时的整条链路没有在卡上跑过。** 前提已在卡上确认：用户在 glm53 容器里跑 `probe/npu_event_probe.py`（torch 2.10.0a0、torch_npu 2.10.0.post4，aclgraph replay），设备事件 2.010 ms、host 计时加同步 2.024 ms、只算 host 发射 0.050 ms，20 轮里 host 返回时设备都还在算，判为 CLOSE（原始输出 `../../probe/field/2026-11-19_用户带回_第1次.txt`）。这只验证了事件计时本身；runner 里的挂钩、推迟写出、async 下的读取没有在卡上跑过。
- **和 vLLM 移植版一起跑：** 联合检查（`../../joint/`）用的是 t51-glm53-vllm 定稿 `65c8e87` 的补丁（带 A2）。这只说明两边的接口和记录链对得上，没有替代在卡上跑。

- 在昇腾卡上运行；测量点是否落在预期位置；真实 Mooncake 下的返回值和计数。
- vllm-ascend 自己的单元测试只用了补丁 01 的 `pd_diagnostics.py`，没有装整个 vLLM 移植版；和移植版合起来跑的只有联合检查和 `sched_check`。
- RecomputeScheduler：
  - `DyntraLBRecomputeScheduler` 要 DP > 1，比对场景里没跑，只靠 vllm-ascend 自己的测试；
  - Recompute 为推测解码补齐首个 decode 请求的那条分支没有走到，补丁没碰这条分支；
  - 开了 ms_service_profiler 时，每步可能多一层 `Scheduler.schedule` 的 span，没有在装了它的环境里看过。
- 用真实 torch 和 vLLM 的完整测试：容器里装不了。桩环境下 73 个失败的测试在干净树上同样失败，它们覆盖的代码（如 dspark 前缀缓存、层式缓存布局）这次没有被测到。
- 层式路径（`use_layerwise`）：glm5.3 的配置里没有打开（以检测结果为准），只跑了测试里已有的层式用例。
- V2 runner：glm5.3 默认 V1；V2 只测了 lmhead TP 时它自己的 `sample()`，V2 的 forward 和普通 sampling 在 vLLM 里，属于 vLLM 补丁。
- 设备耗时（C14）只在 V1 runner、`tp_rank 0` 上有；V2 runner 和其他 rank 只有 host 值。
- 投机解码下 forward、sampling、draft 的口径由读码和上面的静态、编译测试确认，没有真实日志。
- 现场的本地修改：补丁以公开的 `d4ad49796` 为基线，现场文件的 diff 回来后要重做一遍（第 8 节）。

## 9. 现场文件有本地修改时怎么重做

检测脚本会导出现场文件和 `d4ad49796` 的 diff。拿到后：

1. 用 `SHA256SUMS.before` 判断哪些文件和公开版本一致；一致的直接用 `files/` 里的替换件。
2. 不一致的文件，先在「`d4ad49796` + 现场 diff」重建出的文件上逐个 `git apply --3way per-file/NN-*.patch`。
3. 打不上时，缓存池的 4 个文件（`metadata.py`、`pool_scheduler.py`、`kv_transfer.py`、`pool_worker.py`）和 `core/recompute_scheduler.py` 用 `replay/replay.sh <树根>` 按文本锚点重放：每处改动都写明锚点文本和应出现的次数，对不上就在那一处停下并报出锚点，便于逐处手工处理。backend 和 runner 的改动小，按 `per-file/06`–`09` 手工处理。
4. 重做后重跑第 6、7 节的测试和记录对比。

## 10. 设备事件计时（C14）

**范围。** 只改 V1 runner（`worker/model_runner_v1.py`，现场用的就是它）；V2 runner 保持 host 计时。`stages.forward`、`stages.sampling` 原有的 `duration_ns`（host 侧）和其他字段都不动，各加一个 `device_duration_ns`，这是 C14 允许新增的唯一字段，核对用途：在 P 上用 host 值和设备值是否接近，验证设备计时本身准不准。只在 `tp_rank == 0` 上做，其他 rank 和以前一样。

**怎么量。** `pd_device_timing.measure` 放在现有 `runner.forward`、`runner.sampling` 两个 span 的里面，紧贴 `run_model()` 和 sampler 调用：调用前后各在当前计算流上记录一个 NPU 事件（`torch.npu.Event(enable_timing=True)`）。事件放在 4 个槽位的环形池里（K = 4），每个槽位一对 forward、一对 sampling，第一次用时创建，之后复用。图捕获期间、dummy run、没有打开的 `worker_summary` 时不记录。同一步里同一阶段被调用两次时，这一阶段不给设备值（host 值是两次之和，一对事件量不出来）。

**延后读取和写出。** 这一步的 `worker_summary[execute]`、`[sample]` 用 vLLM 的 `pd_claim_finish()`（A2，`docs/A2_INTERFACE.zh-CN.md`）先不写出，读到设备值后由本模块写出，严格按步骤先后。读取只用 `query()`，两个事件都完成才调 `elapsed_time()`：

| 情况 | 读取和写出的时机 | 为什么这时事件已完成 |
|---|---|---|
| P（不开 async） | `sample_tokens` 返回前（`end_sample(read_now=True)`）。execute 那条立刻写出，sample 那条在 `sample_tokens` 返回时写出 | `_bookkeeping_sync` 已经把采样结果拷回 host |
| D（async） | 异步输出的 `get_output()`：先调 `super().get_output()` 拿到返回值，再读取、写出；这一段包在 `try/except` 里，不抛异常、不改返回值。线程：mp 执行器是 Worker 的异步输出线程，uni 执行器是消费 `AsyncOutputFuture.result()` 的线程 | 拷回流在构造时 `wait_stream` 了计算流，拷回完成就说明这一步的事件都已完成 |
| 兜底 | 每步 `execute_model` 开始时；进程退出时（`atexit`） | — |

事件池和待写记录由本模块自己的一把锁保护（读在异步输出线程，写在主线程）。

**读不到、出错。** 读不到（4 步以后仍未完成）、事件调用出错、读取出错时，这一步照常写出，只是不带 `device_duration_ns`（不写成 0）。被测的调用本身抛出的异常原样向外抛。

**出异常的那一步立即写出，不推迟。** `execute_model` 或 `sample_tokens` 抛异常时，vLLM 的包装器原样用 `status`（`error` 或 `cancelled`）和 `error_type` 立即写出这条记录，claim 随之作废（A2 接口的约定）；这一步不会有异步输出，所以不能等。出错那一步的记录因此可能排在前面还压着的步骤之前写出。计时这边之后对它的 `release()` 不起作用，不会重复写出，后面的步骤照常。模块里任何意外错误都会让它自己关闭并写出手里的记录；vLLM 侧没有 `pd_claim_finish` 时它从一开始就不启用。

**副作用。** 被推迟的记录，公共字段 `timestamp_ns`、`native_tid` 是写出时刻和写出线程（D 上是异步输出线程），各阶段的 `start_ns`、`end_ns` 不变。进程硬崩溃时，最多丢 4 步还没写出的 worker 记录。

**测试。**

| 测试 | 内容 |
|---|---|
| `tests/ut/worker/test_pd_device_timing.py`（16 个，标准库） | 事件用桩，设备时间由测试推进、完成与否由测试决定：同步一步的两个值；事件晚完成（在 `get_output` 或下一步读到）；一直不完成（4 步后不带设备值写出，后面的步骤照常有值）；读取出错；记录事件出错不影响被测调用；被测调用的异常原样抛出；async 三步重叠各归各步；写出顺序按步骤；没有事件的步骤也占位；同一阶段两次不给值；非 rank 0、图捕获、拿不到 claim 时什么都不做；退出时写出；`get_output` 里诊断代码抛异常时返回值不变、原有异常照样抛出；读线程和主线程并发 300 步，每条记录写一次、按步骤先后、值归对；出异常时 vLLM 已写出的记录不会被再写一次，后面的步骤照常 |
| `tests/ut/worker/test_pd_diag_runner_points.py` 新加 3 个 | 从源码编译出 V1 的 `_model_forward`、`_sample`：两个事件都在计算流上，紧贴模型调用和 sampler 调用，不包 `_update_full_graph_params_if_needed`、draft 概率、更新 token；挂钩的位置（`end_sample` 是两个 `return` 前的最后一句）；V2 里没有设备计时 |
| 联合检查第 4 部分（25 项） | 用 vLLM 侧的 A2 参考实现、真实的 `pd_device_timing` 和按 V1 位置调用挂钩的最小 runner：同步两步的值和顺序；host 字段不变、只多 `device_duration_ns`；async 三步在消费前一条都不写出，消费线程里读到后各归各步、按顺序写出，`get_output` 返回值不变；`get_output` 里诊断抛异常时返回值不变，这一步之后照样写出；一直不完成的一步不带设备值写出，后面的步骤照常；`execute_model`、`sample_tokens` 分别抛异常时：异常原样抛给调用方，出错的记录在抛出时已经写出、状态 `error`、`error_type` 原样，只写一次、不带设备值，同一组步骤的其他记录各写一次，后一步照常有设备值；`tp_rank` 1 不做 |
| 反向对照 | C14 之前的 runner（带上新模块）：runner 的 4 个设备事件用例出错、挂钩用例不过；没有 A2 的 vLLM：联合检查第 4 部分 4 项不过，记录照常立刻写出 |

时间断言：设备值来自桩上推进的设备时间，是确定值；真实时间只断言下界和先后顺序。

## 11. D 的调度器：RecomputeScheduler

**为什么要改。** 用户核对过现场进程命令行：D 开了 `recompute_scheduler_enable` 和 `--async-scheduling`，用的是 `AsyncRecomputeScheduler`。它经 `scheduler_cls` 选中，继承 `RecomputeScheduler` 在 `core/recompute_scheduler.py:180`（原文件行号）自己的一份 `schedule()` 正文，从不调用 vLLM 的 `Scheduler.schedule()`。

不改的话，01 写在调度器里的记录在 D 上全部缺失：

- `core_summary[execution]` 的 `stages.schedule`、`stages.other`、`members[]`；
- 请求上的 `pd_execution_id`、排队段的首次候选；
- `SchedulerOutput.pd_execution_id`，只能由 EngineCore 兜底补一个。

P 和没开 recompute 的 D 跑的是 `BalanceScheduler`（balance 默认关），它调用 `super().schedule()`，不受影响。哪些调度器绕过、各埋点在哪一行，见 `../../docs/SCHEDULER_BYPASS.zh-CN.md`。

**怎么改（行号是新文件的行号）。**

| 处 | 位置 | 内容 |
|---|---|---|
| 导入 | :41 | `from vllm.v1.metrics import pd_diagnostics as diag` |
| 入口 | `schedule()` :181–188，`_schedule()` :190 起 | 原正文改名为 `_schedule()`。`schedule()` 仍定义在这个类里：在方法解析顺序里找到 vLLM 自己的 `Scheduler`（`__module__ == "vllm.v1.core.sched.scheduler"`、`__qualname__ == "Scheduler"`），直接调它的 `schedule()`，由 01 的包装打开记录，再调回 `self._schedule()` |
| P1 成为候选 | :491 | `request.pd_queue_transition("candidate")` |
| P2 外部查找 | :557–583 | 包住 `connector.get_num_new_matched_tokens(...)` |
| P3 等待→调度 | :856 | `request.pd_execution_id = diag.current().get("execution_id")` |
| P4 输出带步号 | :998 | `RecomputeSchedulerOutput(pd_execution_id=...)` |
| P5 成员 | :1044–1078 | `members[]` 和 `diag.current_batch().update(...)` |

**入口为什么这样写：**

- **不用 `super()`：** vllm-ascend 进程里 `vllm.v1.core.sched.scheduler.Scheduler` 这个名字会被换成 `BalanceScheduler`（`patch_balance_schedule.py:897`），`RecomputeScheduler` 的基类可能就是它（看导入顺序）。这时 `super()` 会让每步多走一次 `BalanceScheduler.schedule`；原版不经过它，而 ms_service_metric 和 ms_service_profiler 在两个名字上都挂了钩子。
- **不按名字导入：** 那个名字可能已经换掉了。
- **`schedule` 仍定义在本类：** vllm-ascend 测试里的 `RecomputeScheduler.schedule is not Scheduler.schedule`，和 profiler 按 `RecomputeScheduler.schedule` 挂的钩子，都不受影响。

P1–P5 的代码和 vLLM 移植版 `_schedule()` 里对应的 5 处相同，只按本文件 120 列的格式换了行。新增 73 行，另有 3 行只改了缩进，原有的行一行没删。没有新加段或字段。

**测试**（`../../sched_check/`，结果在 `../../ut_results/recompute-scheduler/`）：

- **调度器记录比对：** `t51_sched_fieldset.py`，驱动用 vLLM 移植版自己的 `pd_equiv.py`（真实的 EngineCore、KVCacheManager、Request）。
  - 调度器 6 种：vLLM 的 `Scheduler`；`BalanceScheduler`（balance 关）；原版和移植版 RecomputeScheduler，各在两种基类上。同步场景用 `RecomputeScheduler`，异步场景用 `AsyncRecomputeScheduler`。
  - 场景 11 个，含现场 D 的形态：异步调度、batch queue 2、异步远端 KV，带和不带 MTP 5。
  - 核对：
    - 每种记录的字段集合和 vLLM 的相同；
    - 每步都有 `pd_execution_id`，而且每步不同；
    - 移植前后每一步的调度决定和发给客户端的输出完全相同；
    - 决定和 vLLM 相同的场景，屏蔽 id 和时间以后记录逐条相同；
    - 计数钩子：`BalanceScheduler.schedule` 在移植版两种基类下都是 0 次，`RecomputeScheduler.schedule` 每步 1 次；
    - 原版作反向对照，字段确实缺失。
  - 结果：363 项通过，2 项按约定预期不同（见下），0 项不过；连跑 3 次相同。
  - 上一版（用 `super()`）跑同一组测试，计数钩子的 11 项都不过。
- **vllm-ascend 自己的 `tests/ut/core`（298 个）：** 两种基类下补丁前后逐个结果相同。
  - `BalanceScheduler` 为基类（vllm-ascend 测试环境自己的导入顺序就是这样）：295 个通过；3 个在两边都不过，它们要用 `scipy.optimize.nnls`，而镜像里的 scipy 是桩。
  - vLLM `Scheduler` 为基类：292 个通过、6 个不过，两边相同。多出的 3 个是 `test_recompute_scheduler.py` 里按名字 patch `Scheduler` 的用例：这个环境里那个名字是 `BalanceScheduler`，不在 RecomputeScheduler 的继承链上，是测试环境本身的问题。
- **p2d a9e9695 的测试：** 它自己的 35 个测试加 `kv_p2p` 目录，在合并树（本包 11 个文件 + p2d）上两种基类各跑一次。结果都是 229 通过、14 跳过，35 个全过，和 p2d 自己的参照结果相同（`../../ut_results/p2d-a9e9695-on-merged/`）。
- **联合检查：** vLLM 65c8e87 + 本包 11 个文件 + p2d a9e9695，两种基类各 130/130。D 的两步（第 3 部分的 D、第 5 部分）由真实的 `AsyncRecomputeScheduler.schedule()` 产生 `SchedulerOutput`，步号不再手工填写。用原版 `recompute_scheduler.py` 时，D 的两道 execution_id 门都不过。

**按约定预期不同（统筹定为 (a)，写进约定）：**

- **触发条件：** 只在 RecomputeScheduler 于 `schedule()` 内把请求退回 P 的那一次出现。
- **差别：** 这个请求的 `core_summary[scope=request]` 记录，和正常结束的请求相比，多一个 `role: "unknown"`，少一个 `no_forward`。
- **原因：** 记录在 01 调度器包装的 diag scope 里写出，`Batch` 以 `current()` 为底，继承了那里的 `role`；正常结束的请求在 EngineCore 这一步的 scope 里写出，带的是那里的 `no_forward`。
- **`no_forward` 不是新问题：** vLLM 的 `Scheduler` 里被 abort、shutdown 结束的请求也没有它。
- **影响：** 过滤器、采集器、生成脚本都不读记录里的 `role`。
- **详见：** `../../docs/SCHEDULER_BYPASS.zh-CN.md` 第 6 节。

**已知问题：** ms_service_profiler 按 `vllm.v1.core.sched.scheduler:Scheduler.schedule` 挂钩子。如果挂钩时这个名字还是 vLLM 自己的类，移植版的路径会经过它，D 上每步多一个 `batchFrameworkProcessing` span。只在 profiler 打开时发生。
