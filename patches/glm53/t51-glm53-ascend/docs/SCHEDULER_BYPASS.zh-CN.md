# vllm-ascend `d4ad49796` 里绕过 vLLM `Scheduler.schedule()` 的调度器

范围：vllm-ascend `d4ad49796`（releases/v0.30.0），vLLM v0.30.0 加移植版补丁（t51-glm53-vllm d3e84e2 / 9ff2d86 / 定稿 65c8e87；这几版的 `scheduler.py`、`kv_cache_manager.py`、`request.py`、`engine/core.py`、`sched/output.py` 逐字节相同）。vllm-ascend 的行号都是 `d4ad49796` 原文件的行号；vLLM 的行号是打完移植版补丁后 `vllm/v1/core/sched/scheduler.py` 的行号。现场到底启用了哪个调度器，以进程命令行和 `additional_config` 里的调度配置为准，本文不判断现场。

## 1. 结论：按族列，逐条核对 t51-p2-metrics-plan 的结论

核对方法：用 AST 扫了 `vllm_ascend/` 下所有类，找出类体里定义了 `schedule()` 的类，看它的函数体里有没有 `super().schedule(...)`；再看各个 Async、DyntraLB 变体的类体里有没有 `schedule()`（都没有，所以按 MRO 用的是父类的那一个）。启用条件按 `platform.py` 读。

### 1.1 重写 `schedule()` 又不调 `super()` 的三族（绕过 01 的调度器埋点）

| 族 | t51-p2-metrics-plan 的结论 | 核对结果 | 启用条件（`platform.py`） |
|---|---|---|---|
| Recompute：`RecomputeScheduler`（:85），以及 `AsyncRecomputeScheduler`（:1045）、`DyntraLBRecomputeScheduler`（:1049）、`AsyncDyntraLBRecomputeScheduler`（:1053） | `recompute_scheduler.py:180` | **一致**。`schedule()` 在 :180–1011，函数体里没有 `super().schedule`；3 个变体的类体里都没有 `schedule()`，用的都是 :180 这个 | `recompute_scheduler_enable`：只在 PD 分离的 D（`kv_role='kv_consumer'`）上生效；P 上告警后关掉，PD 混合时报错（:1116–1145）。用哪个类见 :1578–1589：开了 `--async-scheduling` 就是 `AsyncRecomputeScheduler`，DyntraLB 同时开时是 `*DyntraLBRecompute*` |
| DyntraLB：`DyntraLBScheduler`（:420），以及 `AsyncDyntraLBScheduler`（:1203） | `dyntra_lb_scheduler.py:423` | **一致**。`schedule()` 在 :423–1200，不调 `super`；Async 变体类体里没有 `schedule()`。`DyntraLBPolicyMixin` 重写的 `_update_after_schedule`（:403）、`_free_request`（:411）都调用了 `super()` | `dyntra_lb_config.enabled`：只在 D 上、`data_parallel_size > 1`、单节点、不与 balance 或 profiling chunk 同开（:1086–1113） |
| ProfilingChunk：`ProfilingChunkScheduler`（:52），以及 `ProfilingChunkAsyncScheduler`（:1257） | `scheduler_profiling_chunk.py:265` | **一致**。`schedule()` 在 :265–1254，不调 `super`；Async 变体类体里没有 `schedule()`。`patch_profiling_chunk.py:176` 给 `cls.schedule` 再包一层，包的仍是这个类自己的 `schedule()`，不改变结论 | `profiling_chunk_config.enabled`（:1385–1390）；async 时要求 V2 runner（:1078） |

### 1.2 走 vLLM `Scheduler.schedule()` 的（01 的字段照常有）

| 类 | t51-p2-metrics-plan 的结论 | 核对结果 |
|---|---|---|
| `ShortRequestFirstAsyncScheduler`（`short_request_first_scheduler.py:370`） | 自己没有定义 `schedule()`，经 `AsyncScheduler` 继承，改的是请求队列 | **一致**。类体里只有 `__init__`（:373，调用 `super().__init__` 后安装等待队列，:380）。同步时没有单独的类，等待队列由 `BalanceScheduler.__init__` 安装（`patch_balance_schedule.py:127–133`）。启用条件：只在 P 或 PD 混合上，D 上报错（`platform.py`:1051–1072） |
| `BatchJobAwareScheduler`（`batch_job_aware_scheduler.py:622`） | 定义了 `schedule()`，但调用了 `super().schedule()` | **一致**。`schedule()` 在 :643，只先清 block-reserve 缓存，:653 `return super().schedule(*args, **kwargs)`；它重写的 `_free_request` 也调用了 `super()`。`BatchJobAwareAsyncScheduler`（:664）类体里没有 `schedule()`。启用：`platform.py`:1397–1405 |

### 1.3 补充：`BalanceScheduler`（和三族的结论不冲突；P 用的就是它）

`patch_balance_schedule.py:897` 把 `vllm.v1.core.sched.scheduler.Scheduler` 这个名字换成 `BalanceScheduler`。这件事有两层，互不相干，第 3 节分开写：

- 哪个类在跑：没设 `scheduler_cls` 的 P 和没开 recompute 的 D 跑 `BalanceScheduler`。balance 关着，它调 `super()`，01 的埋点生效。开了 recompute 的 D 跑 RecomputeScheduler，不经过这个替换。
- RecomputeScheduler 的基类是谁：取决于导入顺序。

### 1.4 `schedule()` 以外的 01 埋点

`__init__` 的 `_pd_engine_id`、`add_request` 的 identity 和 `queued`、`_free_request` 的 `finished`、`shutdown`：上面所有类都没有用不调 `super()` 的方法覆盖它们，照常生效（逐个类用 AST 核对过）。`ShortRequestFirstRequestQueue.add_request` 是请求队列的方法，不是 Scheduler 的。

## 2. 01 在 vLLM `Scheduler.schedule()` 里的埋点，在各个类里对应的行

01 把 vLLM 原来的 `schedule()` 改名为 `_schedule()`，另加一个 `schedule()` 包住它（第 W 行）。第 P1–P5 行是 01 在 `_schedule()` 正文里加的 5 处。下表右边 4 列是各类自己那份正文里，**同一位置**的行（这些类都没有这 5 处，「对应」是指埋点应该加在哪一行旁边）。

| 埋点 | 01 写什么 | vLLM 移植版 | Recompute | ProfilingChunk | DyntraLB | Balance（开） |
|---|---|---|---|---|---|---|
| W 包装 | 新建 `execution_id`；`core_summary[scope=execution]` 的 identity（含 `kv_role`、`role`）；`stages.schedule`、`stages.other`；没有发生的 `lookup`/`alloc` 写 `calls=0, status=skipped`；`num_scheduled_tokens`、`total_num_scheduled_tokens`；自己新建的记录或出错时 `finish` | `schedule()` 563–638，正文 `_schedule()` 640–1603 | 不经过 | 不经过 | 不经过 | 不经过 |
| P1 成为候选 | `peek_request()` 之后 `request.pd_queue_transition("candidate")`（排队段的结束点） | 949 | 480 | 638 | 676 | 408 |
| P2 外部查找 | 包住 `connector.get_num_new_matched_tokens(...)`：`stages.lookup` 加一段，状态、`error_type`；计入 `other` 扣除项 | 1021（计时 1016–1044） | 546 | 699 | 756 | 485 |
| P3 等待→调度 | 发 `SCHEDULED` 事件之前 `request.pd_execution_id = 当前 execution_id` | 1349（事件 1352） | 822 | 1026 | 1010 | 679 |
| P4 输出带步号 | `SchedulerOutput(pd_execution_id=...)` | 1512 | 961 | 1207 | 1174 | 817 |
| P5 成员 | `_update_after_schedule` 之前：每个被调度请求的 `pd_execution_id`、`pd_executions`、`pd_decode_steps`；`members[]`；`request_ids`、`no_forward`、`finished_request_ids`、`preempted_request_ids` | 1560–1599（`update` 1593，调用 1602） | 1008 | 1253 | 1196 | 839 |
| KVCacheManager 计时（不在 `schedule()` 里） | `get_computed_blocks`、`allocate_slots` 的 `lookup`/`alloc` 段，写进当前打开的记录 | 1084；808、1263 | 586；341、740 | 739；460、944 | 747、797；559、928 | 481；296、614 |

KVCacheManager 那一行的计时在管理器里，绕过时照样执行，写进 EngineCore 自己打开的 `core_summary`；W 里的 `pd_stage_ns.clear()` 不执行，只影响 `other` 的扣除，而 `other` 本来就不写。

## 3. BalanceScheduler：两层，互不相干

`patch/platform/__init__.py:44` 无条件导入 `patch_balance_schedule`，后者在 :897 把 `vllm.v1.core.sched.scheduler.Scheduler` 这个名字换成 `BalanceScheduler`。

### 3.1 哪个类在跑

`patch_balance_schedule.py:894–897` 的注释写明：这个替换只在 `scheduler_cls` 没设时生效（vLLM 按这个名字构造调度器）；recompute、dynamic-batch、profiling 三种调度器都设了 `scheduler_cls`，绕开了这个替换。所以现场：

| 实例 | 跑的类 | 01 的调度器埋点 |
|---|---|---|
| P（没有调度配置，不开 async） | `BalanceScheduler` | 生效。`enable_balance_scheduling` 默认 `False`（`ascend_config.py:1614`，不读环境变量），balance 关时 `schedule()` 在 :164–165 调 `super().schedule()`，走 01 的包装。PD 分离下打开会报错（`platform.py`:1036–1044） |
| 没开 recompute 的 D | 同上 | 同上 |
| 开了 recompute 的 D（现场就是，`--async-scheduling`） | `AsyncRecomputeScheduler`，经 `scheduler_cls` 选中，不经过这个替换 | 原版不生效（:180 自己的正文），所以要移植（第 5 节） |

测试里 `BalanceScheduler`（balance 关）在全部场景下写出的记录，字段集合和取值都和 vLLM 的 `Scheduler` 相同。

### 3.2 RecomputeScheduler 的基类是谁

和上一节不是一回事。`recompute_scheduler.py` 用 `from vllm.v1.core.sched.scheduler import Scheduler` 定义基类，取的是导入那一刻这个名字指向的类：

- 替换之后才导入，基类就是 `BalanceScheduler`；
- 替换之前导入，基类就是 vLLM 的 `Scheduler`。

正常启动时，`pre_register_and_update`（`platform.py`:307–311）先调 `adapt_patch(is_global_patch=True)`，之后 `check_and_update_config` 才在 :1027 导入 `recompute_scheduler`。联合检查里按正常的导入顺序，导入 `vllm_ascend.worker.v2.model_runner` 时这个名字就被换掉了，在 `recompute_scheduler` 之前。所以现场多半是 `BalanceScheduler`；EngineCore 子进程的导入顺序没法静态确定。

移植版的 `schedule()` 在方法解析顺序里找到 vLLM 自己的 `Scheduler`，直接调它的 `schedule()`，也就是 01 的包装。不用 `super()`：基类是 `BalanceScheduler` 时，`super()` 会让每一步多走一次 `BalanceScheduler.schedule`。原版从不经过它，而 ms_service_metric（`observability/config/base_metrics.yaml`:6、:9）和 ms_service_profiler（`profiling_config.py`）在两个名字上都挂了钩子。这个问题是 t51-glm53-vllm 独立核对时发现的，按它的参考写法改了。两种基类都测了（第 5 节）。

## 4. 绕过时少了什么（测试里的反向对照）

同一组请求经过原版 `RecomputeScheduler` 时，与 vLLM 的 `Scheduler` 相比（场景 S2：D，异步远端 KV）：

- **`core_summary[execution]` 缺的字段**：`stages.schedule`、`stages.other`、`members[]`（12 个子字段）、`num_scheduled_tokens`、`total_num_scheduled_tokens`、`finished_request_ids`、`preempted_request_ids`、`kv_role`、`role`。
- **`stages.lookup`**：只剩本地前缀缓存的查找（每步 2 次，vLLM 是 4 次），connector 的查找没记；没有查找的步骤里，整个 `lookup` 段都没写，而不是写成 `calls=0, status=skipped`。
- **步号**：`schedule()` 返回时 `SchedulerOutput.pd_execution_id` 是空的，由 EngineCore 的兜底补一个（`engine/core.py` 的 `_pd_schedule`）。所以 worker 侧的记录照样有步号，但请求上的 `pd_execution_id` 没设，请求记录里的 `last_execution_id` 是空的，`members[].executions`、`decode_steps` 是 0。
- **请求记录的排队段**：`stages.queue.status` 是 `missing`，`ranges[]` 是空的（P1 没执行）。

## 5. RecomputeScheduler 的移植（补丁包第 11 个源文件）

用户核对过现场进程命令行：D 开了 `recompute_scheduler_enable` 和 `--async-scheduling`，用的是 `AsyncRecomputeScheduler`，走 :180 的 `schedule()`；P 没有调度配置。

- **位置：** `../patches/vllm-ascend/per-file/11-core-recompute_scheduler.patch`；说明见补丁包 README 第 11 节，测试见 `../sched_check/README.zh-CN.md`。
- **范围：** DyntraLB、ProfilingChunk 两族只做了上面的对照表，没有移植。

**改法：**

- 正文改名为 `_schedule()`。
- `RecomputeScheduler.schedule()` 仍在这个类里定义，在方法解析顺序里找到 vLLM 自己的 `Scheduler`（`__module__ == "vllm.v1.core.sched.scheduler"`、`__qualname__ == "Scheduler"`），直接调它的 `schedule()`。不按名字导入，因为那个名字可能已经是 `BalanceScheduler`。
- P1–P5 加在第 2 节表里 Recompute 那一列的位置，代码和 01 相同。调度逻辑一行没改。

**测试**（`t51g-vllmtest:v1`，`docker run --rm --network none`，vLLM 65c8e87）：

- **调度器记录比对：**
  - 规模：12 个场景，含现场 D 的形态（异步调度 + 异步远端 KV，带和不带 MTP 5）和现场 P 的形态（同步、MTP 1、同步加载）；6 种调度器，含两种基类。
  - 结果：396 项通过，2 项按约定预期不同（第 6 节），0 项不过；连跑 3 次相同。
  - 计数钩子：其中一组在 `BalanceScheduler.schedule` 和 `RecomputeScheduler.schedule` 上挂了计数钩子。移植版两种基类下 `BalanceScheduler.schedule` 都是 0 次，`RecomputeScheduler.schedule` 每步 1 次；原版也是 0 次。同一组测试换成上一版（用 `super()`），`*_on_balance` 的 11 项都不过。
- **vllm-ascend 自己的 `tests/ut/core`：** 两种基类下补丁前后逐个结果相同。
- **联合检查：** vLLM 65c8e87、11 个文件、p2d a9e9695 一起上，两种基类各 130/130，D 走真实的 `AsyncRecomputeScheduler`。用原版文件时，D 的 execution_id 门不过。

## 6. 已定：被 RecomputeScheduler 在 `schedule()` 里结束的请求，请求记录的键（约定 (a)）

- **触发条件：** 只在 RecomputeScheduler 于 `schedule()` 内把请求退回 P 的那一次出现（以 `FINISHED_ABORTED` 结束）。细粒度 TP 下改为中止的那条路径，按读码也在 `schedule()` 里结束请求，键的差别应相同，没有测。vLLM 的 `Scheduler` 不会在 `schedule()` 里结束请求。
- **差别：** 这个请求的 `core_summary[scope=request]` 记录，和正常结束的请求相比差两个键：
  - 多一个 `role`，值固定为 `"unknown"`；
  - 少一个 `no_forward`。
- **原因：** 请求记录用 `Batch` 写出，`Batch` 以写出时的 `current()` 为底（`pd_diagnostics.py:220` 起）。
  - 正常结束的请求在 EngineCore 这一步的 scope 里写出，带上了那里的 `no_forward`；
  - 被退回的请求在 01 调度器包装的 scope 里写出，带上了那里的 `role`，没有 `no_forward`。
  - vLLM 的 `Scheduler` 里，被 abort 和 shutdown 结束的请求同样没有 `no_forward`（测试 S5 核过）。所以只有 `role` 是新出现的。
- **决定：** 统筹定为保持现状，写进约定（由 t51-p2-metrics-plan 写）。
  - 理由：过滤器的 role 取自进程名（`pd_diag_logfilter.py`:209–210），采集器的 role 来自文件元数据，生成脚本也不读记录里的 `role`；顾问核过。
  - 测试里这 2 项（两种基类各一项）记为「按约定预期不同」，另有一项把上面的原因钉死。
- **已知问题，改不掉：** ms_service_profiler 按 `vllm.v1.core.sched.scheduler:Scheduler.schedule` 这个名字挂钩子。如果挂钩时这个名字还没换成 `BalanceScheduler`，钩子就落在 vLLM 自己的类上，移植版的路径会经过它，D 上每步多一个 `batchFrameworkProcessing` span。只影响 profiler，而且只在它打开时发生。
