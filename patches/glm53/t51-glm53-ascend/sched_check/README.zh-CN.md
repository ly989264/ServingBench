# RecomputeScheduler 的调度器记录比对

补丁包里第 11 个源文件 `vllm_ascend/core/recompute_scheduler.py`（`../patches/vllm-ascend/per-file/11-core-recompute_scheduler.patch`，说明在补丁包 README 第 11 节）用这里的两个脚本验证。为什么要改、哪些调度器绕过 vLLM 的 `Scheduler.schedule()`，见 `../docs/SCHEDULER_BYPASS.zh-CN.md`。

## 文件

| 文件 | 内容 |
|---|---|
| `run_sched_fieldset.sh` + `t51_sched_fieldset.py` | 同一组请求经 vLLM 的 `Scheduler` 和经 RecomputeScheduler（原版、移植版）走真实的 EngineCore，比对写出的记录和调度决定 |
| `run_recompute_ut.sh` + `t51_scipy_stub.py` + `t51_balance_base.py` | vllm-ascend 自己的 `tests/ut/core`，补丁前、补丁后各跑一次，两种基类各一遍 |
| `vendor/pd_equiv.py` | vLLM 移植版（t51-glm53-vllm）自己的验证驱动，原样复制，sha256 `bf89fef612463f274fbf75dd927d7743e2096cfbe88f7feeaeb952fa643254c0` |

## 运行

镜像：vLLM worker 的测试镜像 `t51g-vllmtest:v1`（Python 3.12、CPU 版 torch、vLLM 的依赖，镜像里不装 vLLM）。两个脚本挂载相同：

```
docker run --rm --network none --entrypoint sh \
  -v <vLLM v0.30.0 + 移植版补丁的树>:/vllm:ro \
  -v <vllm-ascend d4ad49796 树，recompute_scheduler.py 是原文件>:/ascend:ro \
  -v <补丁包 patches/vllm-ascend>:/pkg:ro -v <本目录>:/check:ro \
  -v <facebook/opt-125m 的 HF 缓存（只要配置和分词器）>:/root/.cache/huggingface:ro \
  -v <输出目录>:/out t51g-vllmtest:v1 /check/run_sched_fieldset.sh     # 或 /check/run_recompute_ut.sh
```

两个脚本都先按补丁包的 `SHA256SUMS.before` 核对原文件，`git apply` `per-file/11`，再按 `SHA256SUMS.after` 核对，并和 `files/` 逐字节比较。

## `t51_sched_fieldset.py`

**调度器，6 种：**

- vLLM 的 `Scheduler`；
- `BalanceScheduler`，balance 关（vllm-ascend 进程里默认的同步调度器类，P 用的就是它）；
- 原版和移植版 RecomputeScheduler，各自以 vLLM 的 `Scheduler`、以 `BalanceScheduler` 为基类，共 4 种。vllm-ascend 进程里实际是哪一种基类，取决于导入顺序。

同步场景用 `RecomputeScheduler`，异步场景用 `AsyncRecomputeScheduler`。

**场景，12 个：**

| 场景 | 内容 |
|---|---|
| S1 | 同步，前缀缓存，分块 prefill |
| S2 | D：异步远端 KV |
| S3 | D：异步远端 KV + MTP 5 |
| S4 | batch queue = 2 |
| S5 | 中止 + shutdown |
| S6 | 异步调度 |
| S7 | 同步加载 KV |
| S8 | KV 块不够：vLLM 抢占后恢复，Recompute 把请求退回 P |
| S9 | connector 查找抛异常 |
| S10 | 现场 D 的形态：异步调度 + batch queue 2 + 异步远端 KV |
| S11 | S10 再加 MTP 5 |
| S12 | 现场 P 的形态：同步调度 + MTP 1 + 同步加载 KV |

S10、S11 的执行器在 `pd_equiv` 的执行器上只改一处：每个远端 KV 只报告收到一次。两步同时在途时，原执行器会报两次，vLLM 自己的 `Scheduler` 也会因此断言失败。

**核对：**

| 组 | 内容 |
|---|---|
| A 字段集合 | 每一种记录（`core_summary` 的 execution、request，`worker_summary` 的 execute、sample）写出的键路径集合，移植版（两种基类）和 `BalanceScheduler` 都要等于 vLLM 的 `Scheduler`；原版是反向对照，必须不同 |
| B 调度行为 | 同一基类上，移植版和原版每一步的调度决定都相同。决定包括：各请求的 token 数、新请求和续跑请求的块号与已算 token 数、spec token、结束和抢占的请求、退回 P 的请求、`has_sync_kv_loads`、公共前缀块数。发给客户端的输出也要相同；两种基类之间同样要相同 |
| C 取值 | 和 vLLM 的 `Scheduler` 决定相同的场景里，屏蔽 `record_id`、`execution_id`、`engine_id`、`output_batch_id`、`*_ns` 以后，记录逐条相同 |
| E 钩子 | 在 `BalanceScheduler.schedule` 和每个 `RecomputeScheduler.schedule` 上挂只计数的钩子，模拟 ms_service_metric / ms_service_profiler 挂钩的位置：移植版两种基类下 `BalanceScheduler.schedule` 都是 0 次（原版的行为），`RecomputeScheduler.schedule` 每步 1 次；`BalanceScheduler`（balance 关）每步 1 次，证明钩子本身有效 |
| D 步号、排队、出错 | `schedule()` 返回时每一步都有 `SchedulerOutput.pd_execution_id`，且每步不同，这一步的 execution 记录带同一个 id、同样的 `request_ids`；每条请求记录的 `stages.queue.status` 是 `ok`；S9 里 execution 记录 `status=error`，`stages.lookup` 的 `status=error`、`error_type=RuntimeError` |

**结果**（`../ut_results/recompute-scheduler/sched-fieldset.txt`、`.json`；vLLM ea04aa1）：396 项通过，2 项按约定预期不同，0 项不过，连跑 3 次逐项相同。

| 组 | 通过 | 按约定预期不同 | 不过 |
|---|---|---|---|
| setup | 5 | 0 | 0 |
| 运行无报错 | 72 | 0 | 0 |
| A 字段集合（含原因钉死、S5 参照各项） | 61 | 2 | 0 |
| B 调度行为 | 72 | 0 | 0 |
| C 取值 | 34 | 0 | 0 |
| D 步号、排队、出错 | 92 | 0 | 0 |
| E 钩子 | 60 | 0 | 0 |

**按约定预期不同的 2 项：** 同一件事在两种基类上各算一次。S8 里被 RecomputeScheduler 在 `schedule()` 内退回 P 的那个请求，请求记录和正常结束的请求相比，多一个 `role: "unknown"`，少一个 `no_forward`。

- 统筹定为 (a)：保持现状，写进约定。
- 判为「预期」的条件很窄：字段集合只差 `.role`；带 `role` 的请求记录正好是被退回 P 的那几个，值都是 `"unknown"`；和同一次运行里正常结束的请求比，键正好多 `role`、少 `no_forward`。任何一条不满足就判不过。
- 另有一项把这个原因单独钉死。S5 里另核了一项参照：vLLM 的 `Scheduler` 里被 abort、shutdown 结束的请求，本来也没有 `no_forward`。
- 记录在 `../ut_results/recompute-scheduler/records-S8_kv_blocks_exhausted.json`。

**反向对照：** 上一版移植（63036ac，用 `super().schedule()`）跑同一组测试，E 组 `*_on_balance` 的 11 项和 setup 的 1 项不过（`sched-fieldset-negative-super-63036ac.txt`）。

C 组只有 S8 没比：Recompute 的决定按设计和 vLLM 不同。其余 11 个场景的决定都和 vLLM 相同，包括现场 D 形态的 S10、S11 和现场 P 形态的 S12。

## `run_recompute_ut.sh`

跑 vllm-ascend 的 `tests/ut/core`，共 298 个，补丁前后各跑一次，两种基类各一遍。结果在 `../ut_results/recompute-scheduler/ut-{original,ported}-{vllm,balance}.txt`。

| 基类 | 补丁前 | 补丁后 | 逐个比对 |
|---|---|---|---|
| `BalanceScheduler`（`t51_balance_base.py`：vllm-ascend 的 `tests/ut/conftest.py` 已经按自己的导入顺序把名字换掉了，插件只核对 RecomputeScheduler 的基类确实是它） | 295 通过、3 不过 | 同左 | 相同 |
| vLLM 的 `Scheduler`（`t51_vllm_base.py`：把名字临时换回 vLLM 的类，重新导入 `recompute_scheduler`，再恢复名字） | 292 通过、6 不过 | 同左 | 相同 |

两边都不过的：

- `test_profiling_chunk.py` 里要用 `scipy.optimize.nnls` 的 3 个；
- vLLM 基类下另有 `test_recompute_scheduler.py` 的 3 个。它们按名字 `patch.object(Scheduler, ...)`，而这个环境里那个名字是 `BalanceScheduler`，不在 RecomputeScheduler 的继承链上。

注意：在 vllm-ascend 自己的测试环境里不加插件时，RecomputeScheduler 的基类已经是 `BalanceScheduler`。

环境：

- 镜像里没有 scipy，`t51_scipy_stub.py` 给了一个空的桩。`tests/ut/conftest.py` 的 `adapt_patch()` 会经 `attention/indexer.py` 导入 scipy。
- `VLLM_VERSION=0.30.0`：容器里 vLLM 没有安装，版本号读出来是 `dev`，有的测试会因此报错。
