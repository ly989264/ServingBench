# 评估：在 V1 runner 里给 forward、sampling 加设备事件计时

范围：glm5.3，vllm-ascend `d4ad49796` 的 V1 runner `worker/model_runner_v1.py`（行号是打完本套补丁后的行号），vLLM v0.30.0。只评估，不改补丁。改不改、改了以后 forward 的定义怎么变，由用户定。行数都是估计，没有写过代码。

## 结论

1. **能做，但不能只在 runner 里加两个事件。** 现在 forward 记在 `worker_summary[phase=execute]`，sampling 记在 `[phase=sample]`。这两条记录都在 `execute_model`、`sample_tokens` 返回时就写出（vLLM 补丁 01 的 `pd_execution`、`pd_sample`）。这时设备通常还没算完，P 的 eager 也一样，所以设备时长来不及写进这两条记录。要把 `device_duration_ns` 写进同一条记录，就得把这两条记录的写出推迟到设备值读到以后，或者推迟到读不到、改用 host 值的那一刻。这是这件事里最主要的改动。
2. **读取的时机：**
   - P（eager，不开 async）：`sample_tokens` 里把采样结果拷回 host 时已经同步过，之后读；
   - D（图模式加 async）：在异步输出被消费时读，也就是 `get_output()` 等到拷回事件完成以后。这时 forward、sampling 的结束事件一定已经完成，读取不阻塞。
3. **规模【估算】：**
   - vllm-ascend：`model_runner_v1.py` 加约 90–140 行，可能另起一个小文件放事件池，这样移植的文件清单会多一项；
   - vLLM：0 行，或 20–40 行，取决于推迟写出放在哪一侧（第 3 节）；
   - 测试：约 150–250 行；
   - 另需约定加字段、过滤器支持「优先设备值、回退 host 值」，这两项不在本仓库。
4. **先决条件：** 在空闲卡上跑 `npu_event_probe.py`，确认 torch_npu 2.10 下带计时的事件能给 aclgraph replay 计时，且和同步值接近。

## 1. 现在的测量点和记录写出的时刻

| 段 | 记录 | span 的位置 | 记录什么时候写出 |
|---|---|---|---|
| forward | `worker_summary[execute].stages.forward` | `_model_forward` 里 `run_model()` 的两处（:3253、:3256） | `execute_model`（:2230）返回时，`pd_execution` 退出 `diag.batch` |
| sampling | `worker_summary[sample].stages.sampling` | `_sample` 里 sampler、rejection sampler（:2983、:2992） | `sample_tokens`（:2767）返回时，`pd_sample` 退出 `diag.batch` |

两种模式下，这两个时刻设备都可能还没跑完：

- **D（图模式，async）：** `run_model()` 只是发起 aclgraph replay；`sample_tokens` 返回的是 `AsyncGPUModelRunnerOutput`（:2960），拷回在另一个线程里等待。
- **P（eager，非 async）：** `execute_model` 返回时 forward 的算子可能还在队列里；到 `sample_tokens` 里 `_bookkeeping_sync`（:2884，内部在 :3060 起把采样结果拷回 host）才同步。

## 2. 四个条件逐条

### 2.1 两个事件在同一条流上

- 在 span 的同一位置、同一线程上，对 `torch.npu.current_stream()` 记录：`run_model()` 前后各一次，sampler 调用前后各一次。D 的 forward 是 aclgraph replay，事件在 replay 外面记录，不进图，`npu_event_probe.py` 测的就是这种用法。
- 多流的情况：forward 内部如果用了别的流（通信、重叠计算），只要它们在输出被使用之前汇合回主流，主流上的结束事件就会把它们包含进来；HCCL 等待的时间也算在设备时长里。没有汇合回主流的工作不会被计入。这一点要在卡上，按现场的图模式配置核对。
- **捕获期间不能记录。** `capture_model`（:6615）和 `_dummy_run`（:3932）都会调用 `_model_forward`，捕获时记录事件会被录进图里。门槛设两条：`diag.current_batch()` 不为空（只在真实的 `execute_model` 里有，dummy run 和捕获都没有这条记录）；`torch.npu.is_current_stream_capturing()` 为假（:3226 已经在用这个判断）。

### 2.2 延后读取：没完成就跳过，下一步或消费异步输出时再读

读取只用 `query()` 判断，两个结束事件都完成了才调 `elapsed_time()`，从不调 `synchronize()`。读取点有三个，按顺序：

| 读取点 | 位置 | 适用 | 为什么这时已经完成 |
|---|---|---|---|
| ① `sample_tokens` 里 `_bookkeeping_sync` 之后 | `model_runner_v1.py` :2884 之后、`sample_tokens` 返回前 | P（非 async） | 采样结果已经拷回 host（:3065 的 `_to_list`），主流上这之前的工作都已完成 |
| ② 异步输出被消费时 | vLLM `AsyncGPUModelRunnerOutput.get_output()`（`v1/worker/gpu_model_runner.py:345`），在 `async_copy_ready_event.synchronize()`（:351）之后 | D（async） | 拷回流在构造时 `wait_stream(default_stream)`（同文件 :319–321），拷回完成就说明主流上 forward、sampling 及其后的工作都已完成 |
| ③ 下一步 `execute_model` 开始时 | `model_runner_v1.py` :2230 入口 | 兜底 | 处理 ①② 没读到、事后才完成的事件 |

**D 的异步输出在哪个线程被消费**，取决于执行器，要看检测结果：

- **mp（`MultiprocExecutor`）：** Worker 进程里的 `async_output_busy_loop` 线程（vLLM `v1/executor/multiproc_executor.py:1011`）。它从队列取出输出（:1026），在 `enqueue_output` 里调 `get_output()`（:989）。线程开始时已经设好设备（:1023）。
- **uni（`UniProcExecutor`）：** 消费 `AsyncOutputFuture.result()` 的那个线程（`uniproc_executor.py:32`，`get_output()` 在 :44），和 EngineCore 在同一进程。

做法是在 vllm-ascend 里写一个 `AsyncGPUModelRunnerOutput` 的子类，重写 `get_output()`：先调父类，再按这一步的 `execution_id` 读事件、补进记录、写出记录。:2960 构造时改用这个子类。这段代码运行在上面说的另一个线程里，所以事件池要加锁；`Batch` 本身已经带 `RLock`。

### 2.3 事件按 execution_id 放进环形池，复用，不每步新建

- **池的结构：** K 个槽位，每个槽位 4 个事件（forward 起止、sampling 起止），第一次用时创建，之后一直复用。槽位里另存 `execution_id`、待写出的两条记录和状态。
- **槽位数 K：** 取 async 的深度加余量，例如 4，留给统筹定。
- **槽位怎么取：** 按步序号对 K 取模。取到的槽位如果上一轮的读数还没完成，就不覆盖它，这一步改为不测设备值（回退 host），保证从不为了腾槽位而等待。
- **哪些 rank 记录：** P 是 TP16。过滤器只用 rank 0 的记录（C3），可以只在 `tp_rank == 0` 上记录事件来省开销；也可以每个 rank 都记录，保持和 host 值一致。要统筹定。

### 2.4 读不到时回退到 host 值

- `device_duration_ns` 只在两个事件都完成、`elapsed_time()` 成功时写进对应的 stage；其他情况都不写这个字段，段定义就用 host 的 `duration_ns`。不写的情况有：没完成、超过 K 步仍没读到、dummy run 或捕获、`elapsed_time()` 抛异常。
- 现有的 `duration_ns`、`start_ns`、`end_ns` 都不动。
- 超过 K 步仍没读到的记录，在兜底读取点直接用 host 值写出。
- 进程退出时还没写出的记录，统一用 host 值写出：在 runner 的 shutdown 路径里做，再加 `atexit` 兜底。

## 3. 推迟写出放在哪一侧

| 方案 | 做法 | 改动 | 优缺点 |
|---|---|---|---|
| A1：只改 vllm-ascend | runner 在 `execute_model`、`sample_tokens` 里接管当前 batch 的写出：在 `diag.batch` 退出之前，把这个 batch 实例的 `finish` 换成「放进事件池的槽位」，到读取点再调原来的 `finish` | vLLM 0 行；`pd_diagnostics.py` 不动，和补丁 01 保持逐字节相同 | 依赖 `diag.batch` 通过实例属性调用 `finish` 这个实现细节，vLLM 侧以后改了这里就会失效；要有测试锁住 |
| A2：改 vLLM 的 `worker_base.py` | `pd_execution`、`pd_sample` 加一个「由 runner 负责写出」的开关，runner 拿到 batch，在读取点写出 | vLLM 移植版加 20–40 行【估算】，要 t51-glm53-vllm 配合；`pd_diagnostics.py` 仍可不动 | 清楚、不依赖实现细节；会影响所有用这两个装饰器的 runner，要保证开关默认关 |
| B：不推迟，设备值写进后面某一步的记录 | 在下一步的记录里带上前一步的 `execution_id` 和设备值 | 要新加字段，过滤器要按前一步重新归属 | 不推荐：字段更多，过滤器改动大 |

建议 A2；A1 可以作为不动 vLLM 时的备选。

**两种推迟方式都会带来的变化：**

- 每步的 `worker_summary` 写出得更晚：D 晚到 `get_output()` 那一刻，通常比下一步的发射还晚；P 晚到 `sample_tokens` 末尾。
- 日志里两条记录的先后顺序可能和步骤顺序不一致。
- 过滤器按 `execution_id`、`request_ids` 累加，按读码不依赖写出顺序。但请求结束后最后一步的记录会晚一点到，过滤器每 5 秒写盘的累加要能接住它，这一点要请过滤器的负责人确认。

## 4. 改动清单【估算】

| 位置 | 改什么 | 规模 |
|---|---|---|
| vllm-ascend `worker/model_runner_v1.py`（或另起一个小文件放事件池） | 事件池、两处记录（:3253/:3256 一组，:2983/:2992 一组）、三个读取点、`AsyncGPUModelRunnerOutput` 子类、捕获和 dummy run 门槛、退出时写出 | 90–140 行；另起文件的话，移植的文件清单多一项，检测脚本的基线要跟着改 |
| vLLM `v1/worker/worker_base.py`（A2） | 推迟写出的开关 | 20–40 行 |
| `pd_diagnostics.py` | 不改 | 0 |
| 测试 | 用假事件测：同流记录、没完成时跳过、环形池复用和不覆盖、超时回退、在另一个线程里读、捕获期间不记录、退出时写出 | 150–250 行 |
| 约定 | `stages.forward`、`stages.sampling` 加 `device_duration_ns`；写明写出时刻推迟和回退规则 | 要统筹和用户批准（新字段） |
| 过滤器和 `segments.json` | 「优先 `device_duration_ns`，没有时用 `duration_ns`」。v3 同一段两个 src 是相加，不是取其一，可能要改过滤器 | 另一个 worker |
| dsv4.1 | 默认 V2 runner，forward 在 vLLM 的 `gpu/model_runner.py`，要另外评估 | 不在这次范围 |

## 5. 要先确认或要定的

1. **卡上探测**：`npu_event_probe.py` 在空闲卡上的输出。若 NOT CLOSE 或报错，这个方案先停。
2. **forward 的定义**：从「host 发射」改为「设备执行」，由用户定。
3. **新字段** `device_duration_ns`，按「不再新增字段」的规则，要报批，并写明核对用途。
4. **推迟写出放在哪一侧**（A1 或 A2）；若选 A2，要 vLLM worker 配合。
5. **只在 rank 0 记录还是每个 rank 都记录。**
6. **开销**：每步 4 次事件记录和若干次 `query()` 的 host 开销没测过。建议在 test 集群上开、关各跑一次，比较 TPOT 和吞吐。
