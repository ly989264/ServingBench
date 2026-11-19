# vLLM 移植版和 vllm-ascend 移植版的联合检查

在 `docker run --rm --network none` 里，把打了补丁的 vLLM v0.30.0 树和 vllm-ascend `d4ad49796` 树放进同一个 Python 环境，检查三件事：

1. **import**：补丁 02 依赖的 vLLM 模块，和 vllm-ascend 改过的每个模块都能导入；另从源码核对 `model_runner_v1.NPUModelRunner.execute_model` 先调 `handle_preemptions`、再进 KV connector 的上下文、forward 在上下文里，并且带 `pd_execution` 装饰器。
2. **名字和签名**：vllm-ascend 移植版在 `vllm.v1.metrics.pd_diagnostics` 上用到的每个名字（按 AST 找出）都在，签名和补丁 01 的原文件相同；`Batch.add_stage`、`Batch.finish` 的签名；logger 名；`runner.forward`、`runner.sampling`、`kv.load_submit`、`kv.save_wait` 对应的阶段名；`worker_base` 的 `pd_execution`、`pd_sample`、`pd_execution_context`；`SchedulerOutput.pd_execution_id`。
3. **调用链**：用真实的 vLLM 部件（`pd_execution`/`pd_sample`、KV connector mixin、`MultiConnector`）和真实的 vllm-ascend 部件（`AscendMultiConnector`、`AscendStoreConnector`、`KVPoolWorker`、真正启动的保存和加载线程、`MooncakeBackend`、`KVPoolScheduler._emit_load_links`），走 P（`load_async=false`，两步）和 D（`load_async=true`）两种场景，按 glm5.3 约定逐项检查写出的记录。torch_npu、NPU 事件、Mooncake 的 store 对象用桩；P→D connector 用一个什么都不做的桩（`MooncakeConnectorV1` 是另一个 worker 的范围）。runner 是一个最小的类，按 `model_runner_v1.execute_model` 的顺序调用这些部件，这个顺序由第 1 部分从源码核对。

4. **设备事件计时（C14）**：用 vLLM 侧的 A2 开关（`pd_claim_finish`）和真实的 `vllm_ascend/worker/pd_device_timing.py`，配一个按 `model_runner_v1` 的位置调用挂钩的最小 runner，NPU 事件用按脚本推进的桩：同步两步、async 三步重叠后由另一个线程消费、`get_output` 里诊断代码抛异常、事件一直不完成、`tp_rank` 1，检查 `device_duration_ns` 的值和归属、host 字段不变、写出顺序、`get_output` 的返回值。挂钩在 V1 runner 里的位置由 vllm-ascend 的 `tests/ut/worker/test_pd_diag_runner_points.py` 测。

5. **D 从 P 读 KV（`MooncakeConnectorV1`，t51-glm53-p2d 的补丁）**：vllm-ascend 树上同时打了 p2d 的补丁时运行。D 的首次候选那一步：EngineCore 用真实的 vLLM `SchedulerOutput`（带 `pd_execution_id`）调 `MooncakeConnectorScheduler.build_connector_meta`，写 consumer 映射记录；Worker 在真实的 `pd_execution`、KV connector mixin、`AscendMultiConnector` 里调 `MooncakeConnector.start_load_kv`，读任务入队，再按接收线程的做法逐个处理（传输引擎是每次 READ 固定 3 ms 的桩，P 不在）。核对：映射记录、两个读任务、这一步的 `worker_summary[execute]` 三处的 `execution_id` 都等于 `SchedulerOutput.pd_execution_id`（C2 的关联链）；读任务的 `tp_rank`、P 侧请求号、READ 的 native call、`stages.wait_for_transfer`；`request_generation` 的 envelope 包住两次 READ、`execution_id` 按设计为空。

**D 的调度器。** 第 3 部分的 D 那一步和第 5 部分那一步的 `SchedulerOutput`，由 vllm-ascend 的 `AsyncRecomputeScheduler.schedule()` 产生，步号不再手工填写。现场 D 开了 `recompute_scheduler_enable` 和 `--async-scheduling`，用的就是这个类。调度器用 vLLM 自己的测试辅助函数 `tests/v1/core/utils.create_scheduler` 构造，读 `facebook/opt-125m` 的配置，离线，设备指定为 CPU。

- 第 3 部分 D：`AscendConnectorMetadata` 的步号按 `KVPoolScheduler.build_connector_meta` 的写法，从 `SchedulerOutput` 上用 getattr 取；
- 第 5 部分：调真实的 `MooncakeConnectorScheduler.build_connector_meta(output)`；
- 两部分都另核对：调度器自己写的 `core_summary[execution]` 记录，`execution_id` 等于 `SchedulerOutput.pd_execution_id`，并写了 `stages.schedule` 和这一步请求的 `members[]`。

**execution_id 硬判定。** 第 3 部分（P、D 各一道）和第 5 部分开头各有一道门：只要有一条 `worker_summary` 或 `core_summary[execution]` 记录的 `execution_id` 为 null，这一部分就判失败，其余检查不再跑。原因：过滤器遇到 null 会退回用 `record_id`，算出来的是错的数，不是缺的数，看板上分不出来。`JOINT_NULL_EXECUTION_IDS=1` 让各步不带 `pd_execution_id`，作为这道门的反向对照。

6. **PD_DIAG_STARTUP（vLLM 移植版 40152b2 起）**：调 vLLM 自己的 `_pd_startup_log_scheduler`（EngineCore 建好调度器后调用它）、`WorkerWrapperBase.init_device` 和 `WorkerWrapperBase.execute_model`。P 用 `BalanceScheduler`（同步、默认开关），D 用 `AsyncRecomputeScheduler`（打开 recompute），Worker 用 V1 的 `NPUModelRunner` 类，`tp_rank` 分别为 0 和 1。逐字核对每个模拟进程打出的行，每行只打一次（打印行之前，先把「每个进程只打一次」的标志复位）。

## 运行

镜像：vLLM worker 的测试镜像（Python 3.12、CPU 版 torch、vLLM 的依赖，镜像里不装 vLLM）。

```
docker run --rm --network none --entrypoint sh \
  -v <vLLM 树>:/vllm:ro -v <vllm-ascend 树>:/ascend:ro \
  -v <补丁 01 的 pd_diagnostics.py>:/reference/pd_diagnostics.py:ro \
  -v <本目录>:/joint:ro -v <facebook/opt-125m 的 HF 缓存（只要配置和分词器）>:/root/.cache/huggingface:ro \
  -v <输出目录>:/out <镜像> /joint/run_joint_check.sh
```

输出：`/out/joint-check.json`（每一项的结果），`joint-records-P.json`、`joint-records-D.json`（写出的记录）。有任何一项不过时退出码为 1。

## 现状

vllm-ascend 用补丁包的 11 个源文件，加 p2d 最终版 `a9e9695`（`mooncake_connector.py` 和它的替换件 `f953b27c…` 逐字节相同）；vLLM 用移植版定稿 `65c8e87`（带 A2）。

- **结果：** 两种基类各 130 项全过，结果在 `../ut_results/joint-final-65c8e87-11files-p2d-a9e9695/`。
  - `JOINT_RECOMPUTE_BASE=vllm`：先导入 `recompute_scheduler`，基类是 vLLM 的 `Scheduler`；
  - `JOINT_RECOMPUTE_BASE=balance`：先加载 `patch_balance_schedule`，基类是 `BalanceScheduler`。
  - 按正常的导入顺序，导入 `vllm_ascend.worker.v2.model_runner` 时那个名字就被换掉了（日志里的 INFO 行）。
- **反向对照（同一目录，基类 balance）：**
  - `JOINT_NULL_EXECUTION_IDS=1`：三道门全部不过；
  - 用原版 `recompute_scheduler.py`：D 的两道门不过。
- **早先几次，留作对照：**
  - 63036ac：130/130。那时基类没有指定，按导入顺序实际是 `BalanceScheduler`；
  - 10 个文件 + 65c8e87：125/125；
  - 9ff2d86：122/122；
  - 不带 A2 的 d3e84e2：第 4 部分 7 项不过；
  - 临时树。
