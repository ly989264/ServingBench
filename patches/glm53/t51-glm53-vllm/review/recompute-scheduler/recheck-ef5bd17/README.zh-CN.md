# 复核：RecomputeScheduler.schedule 改为按 MRO 直接调用（ascend `ef5bd17`）

- **被复核的改动。** t51-glm53-ascend 提交 `ef5bd17` 只改了 `vllm_ascend/core/recompute_scheduler.py` 的 `schedule()`（`:181–188`）：在方法解析顺序里找到 vLLM 自己的 `Scheduler`，直接调用它的 `schedule`，不再用 `super()`。改后文件 sha256 为 `ff719569…`。和上次核对的 `63036ac` 版（`24c12522…`，`super()` 写法）相比，只差这 7 行（`results/patch_check.txt`）。
- **vLLM 侧。** 用本仓库 `ea04aa1` 的补丁 `88685fda…`。在干净的 v0.30.0 上 `git apply --check` 通过，`SHA256SUMS.after` 全部一致（`results/vllm_patch_check.txt`）。
- **做法。** 沿用上次的驱动（`ascend_equiv.py`），场景同样是 E1–E8，在 `docker run --rm` 容器里跑，每个变体各开一个进程，共 7 个变体（`run_ascend_recheck.sh`）。驱动比上次多两处：
  - 增加第三个计数钩子，挂在 vLLM 自己的 `Scheduler.schedule` 上；
  - 把 `63036ac` 版作为反向对照（表中记为「旧移植」）一起跑。

## 结论

上次核对的必须改项已经解决：基类为 `BalanceScheduler` 时，`BalanceScheduler.schedule` 的调用次数回到原版的 0 次。调度决定和记录都没有变化。

## 结果

**计数钩子**（`results/hooks_and_mro.txt`；每格为 E1–E7 / E8 的次数）：

| 变体 | `RecomputeScheduler.schedule` | `BalanceScheduler.schedule` | vLLM `Scheduler.schedule` |
|---|---|---|---|
| 原版，基类 vLLM `Scheduler` | 39 / 41 | 0 / 0 | 0 / 0 |
| `ef5bd17`，基类 vLLM `Scheduler` | 39 / 41 | 0 / 0 | 39 / 41 |
| 旧移植 `63036ac`，基类 vLLM `Scheduler` | 39 / 41 | 0 / 0 | 39 / 41 |
| 原版，基类 `BalanceScheduler` | 39 / 41 | 0 / 0 | 0 / 0 |
| **`ef5bd17`，基类 `BalanceScheduler`** | 39 / 41 | **0 / 0** | 39 / 41 |
| 旧移植 `63036ac`，基类 `BalanceScheduler` | 39 / 41 | 39 / 41 | 39 / 41 |
| 不用 Recompute（vLLM `Scheduler`） | 0 / 0 | 0 / 0 | 39 / 71 |

两种基类下的方法解析顺序都与上次相同。`RuntimeError` 那条分支在所有场景里都没有走到。

**其他各项：**

| 项 | 结果 |
|---|---|
| 调度决定和发给客户端的输出（`results/decisions_compare.txt`） | 两种基类下，8 个场景每一步都相同，分别做了三组比较：原版对 `ef5bd17`、旧移植对 `ef5bd17`、两种基类之间。和 vLLM `Scheduler` 比，只有 E8 不同，这是按设计的（上次已说明：vLLM 抢占后恢复，Recompute 把请求退回 P） |
| 记录（`results/records_*.txt`） | `ef5bd17` 和旧移植比，两种基类下 8 个场景都是 0 处差别。和 vLLM `Scheduler` 比，E1–E7 逐条相同；E8 的差别与上次核对的结果相同（遮去编号，按每行前 150 个字符比较） |
| 按步核对（`results/check_steps.*.txt`） | 两种基类下都是 0 处失败 |

## 这次改动没有消除的一处（与上次核对时相同）

vLLM 自己的 `Scheduler.schedule` 现在每一步都会经过一次（E1–E7 共 39 次，E8 共 41 次），原版是 0 次。只要调用 01 写在 vLLM `Scheduler.schedule` 上的包装，就会这样；`super()` 写法的旧移植也一样。

- d4ad49796 里只有 ms_service_profiler 挂了这个名字：`vllm_ascend/profiling_config.py:46`，`vllm.v1.core.sched.scheduler:Scheduler.schedule`，span 名为 `batchFrameworkProcessing`。
- ms_service_metric 没有挂这个名字。`observability/config/base_metrics.yaml` 里挂的是 `RecomputeScheduler.schedule` 和 `BalanceScheduler.schedule`，这两个名字现在都和原版一样。
- profiler 的钩子最后落在哪个类上，取决于外部包 ms_service_profiler 解析这个名字的时机，在本仓库和 d4ad49796 里都确定不了：
  - 名字换成 `BalanceScheduler` 之前就解析，钩子落在 vLLM 自己的类上，D 上每一步多一个 `batchFrameworkProcessing` span；
  - 换名之后才解析，钩子落在 `BalanceScheduler.schedule` 上，就不会多出这个 span。
- 这只在 D 上启用了 profiler 时才有影响，与 47 段和调度行为无关。要彻底避免，`RecomputeScheduler` 得自己带一份 01 包装的副本。
- **统筹定（11-19）**：保持现状，列为已知问题，不复制 01 的包装，以免两份代码分叉。补丁后核对用的检测脚本会报出 D 上是否装了、是否启用了这两个 ms_service 工具。触发条件和影响范围写在 `patches/vllm/README.zh-CN.md` 第 12.2 节。

## 没有复核的

- 没有在 NPU 上运行，也没有装真实的 ms_service_metric 和 ms_service_profiler；
- vllm-ascend 自己的单元测试和联合核对没有重跑（对方报告两种基类下各 130/130）。
