# 独立核对：RecomputeScheduler 的 01 埋点移植

- **被核对的移植**：t51-glm53-ascend 提交 `b802142`，`prep/recompute-scheduler/`。补丁打的是 vllm-ascend `d4ad49796` 的 `vllm_ascend/core/recompute_scheduler.py`，改动前 `488627ff…`，改动后 `24c12522…`。
- **核对依据**：本仓库的 vLLM v0.30.0 移植版 `65c8e87`，补丁 `b22adf23`。
- **做法**：核对时没有沿用对方的测试，用的是本仓库自己的驱动和比对工具（`pd_equiv.py`、`compare.py`、`check_steps.py`），场景是 E1–E7 加 E8。每种调度器各开一个进程运行，测试在 `docker run --rm` 容器里跑。

> **11-19 复核。** 对方按第 1 条改成在方法解析顺序里直接调用 vLLM 的 `Scheduler.schedule`（`ef5bd17`）。复核结果：`BalanceScheduler.schedule` 的调用次数回到 0 次，调度决定和记录没有变化。详见 `recheck-ef5bd17/README.zh-CN.md`。

## 结论

| 类 | 项 | 结论 |
|---|---|---|
| 必须改，或由检测确认后降为建议改 | `RecomputeScheduler.schedule()` 里用 `super().schedule()`，基类是 `BalanceScheduler` 时每一步会多走一次 `BalanceScheduler.schedule` | 见下文第 1 条 |
| 建议改 | S8 那条已知差别的说明写得不全 | 见下文第 2 条 |
| 无需改 | P1–P5 的位置、字段、语义；`pd_execution_id` 的时机；调用链；调度行为；记录 | 见「核对结果」 |

### 1. 每一步多走一次 `BalanceScheduler.schedule`（必须改，或由检测确认后降为建议改）

- **现象。** 移植后，`RecomputeScheduler.schedule()` 写成 `return super().schedule(...)`。基类是 `BalanceScheduler` 时（按对方的分析，这是正常启动下的情况），每一步会依次经过 `RecomputeScheduler.schedule`、`BalanceScheduler.schedule`、01 的包装。原版的 `RecomputeScheduler.schedule` 用的是自己的整份正文，从来不经过 `BalanceScheduler.schedule`。
- **影响。** 调度结果不受影响：`BalanceScheduler.schedule` 在 balance 关闭时只是转发一次。但 vllm-ascend 给这两个名字都挂了钩子：
  - `observability/config/base_metrics.yaml:6`、`:9`：ms_service_metric 的 `scheduler_scheduler_hooker` 同时挂在 `RecomputeScheduler.schedule` 和 `BalanceScheduler.schedule` 上；
  - `profiling_config.py`：ms_service_profiler 也同时挂了这两个名字。

  装了并开启这两个工具时，D 上每一步的调度器指标会记两次，profiler 每一步也会多出一个 span。
- **证据**（`results/hooks_and_mro.txt`）。在这两个名字上各挂一个只计数的钩子：
  - 原版：E1–E7 里 `RecomputeScheduler.schedule` 39 次、`BalanceScheduler.schedule` 0 次；E8 里 41 次、0 次。
  - 移植版（基类为 `BalanceScheduler`）：E1–E7 是 39 次、39 次；E8 是 41 次、41 次。
  - 基类为 vLLM 自己的 `Scheduler` 时，两版都是 0 次。
- **最小修法（建议由对方实现）。** `schedule()` 直接调 vLLM 自己的 `Scheduler.schedule`，也就是 01 的包装，跳过 `BalanceScheduler`。balance 关闭时，`BalanceScheduler.schedule` 本来就只做转发，跳过它不改变任何行为，而且和原版一样不经过它。
  ```python
  def schedule(self, throttle_prefills: bool = False) -> SchedulerOutput:
      # Patch 01's wrapper on vLLM's own Scheduler. Not super(): with BalanceScheduler as
      # the base it would also run BalanceScheduler.schedule and the hooks on it,
      # which this class never ran.
      for cls in type(self).__mro__:
          if cls.__module__ == "vllm.v1.core.sched.scheduler" and cls.__qualname__ == "Scheduler":
              return cls.schedule(self, throttle_prefills)
      raise RuntimeError("vLLM's Scheduler is not a base of this scheduler")
  ```
  这样改以后，`schedule` 仍然定义在这个类里，vllm-ascend 测试里的 `is not Scheduler.schedule` 断言和 profiler 的挂载名都不受影响。基类是 balance 打开的 `BalanceScheduler` 时，原来会整个跳过本类正文的那条分支（配置上已被 `platform.py:1036–1044` 排除）也不再相关。

  剩下一种情况改了也消除不了：ms_service_profiler 按 `vllm.v1.core.sched.scheduler:Scheduler.schedule` 这个名字挂钩子，如果挂的时候名字还没换成 `BalanceScheduler`，钩子就落在 vLLM 自己的类上，修改后的路径会经过它，D 上每一步多一个 `batchFrameworkProcessing` span。这只影响 profiler，而且只在它被打开时发生。
- **要现场确认。** D 的镜像里是否装了 `ms_service_metric`、`ms_service_profiler`，是否启用了它们（vllm-ascend 经 `setup.py:515` 的入口注册 metric provider）。确认都没有启用时，这一条可以降为建议改。

### 2. S8 已知差别的说明（建议改）

被 Recompute 在 `schedule()` 里退回 P 的请求，它的 `core_summary[scope=request]` 与正常结束的请求比，差两个键，而不是一个：

- 多一个 `role: "unknown"`。这个差别统筹已定为保持现状。
- 少一个 `no_forward`。正常结束的请求是在 EngineCore 这一步的上下文里写出的，带着这个键；被退回的请求是在调度器的上下文里写出的，不带。

`no_forward` 缺失的情况，01 本身也有：在 vLLM 上，被 abort 和 shutdown 结束的请求同样没有这个键（本仓库合成调用的 E4）。所以只有 `role` 是新出现的。建议对方在 `SCHEDULER_BYPASS.zh-CN.md` 第 6 节和约定里把这一点写全。

## 核对结果（无需改的部分）

| 项 | 结果 |
|---|---|
| 补丁 | 在 `d4ad49796` 上 `git apply --check` 通过；改动前后的 sha256 都与对方的校验表一致；打完的文件和 `files/` 里的逐字节相同（`results/patch_check.txt`） |
| P1–P5 | 按语法树比较：`_schedule()` 里加的语句和 vLLM 移植版 `Scheduler._schedule()` 里的逐组相同，包括 P1、P2 整个 try/finally、P3、P5；每组前后相邻的语句也相同。P4 只差类名（`RecomputeSchedulerOutput`），`pd_execution_id=` 参数相同。`_schedule()` 里只有一处 `return`（在末尾）、一处 `peek_request()`、一处 connector 查找、一处构造输出，所以 5 处已经全覆盖 |
| `pd_execution_id` | 由 01 的包装在调用 `_schedule()` 之前生成；构造输出时写入（`:993`），之后才调用 `_build_kv_connector_meta`（`:1023`） |
| 调用链 | 两种基类下的方法解析顺序都与对方描述一致，`_schedule` 都解析到本类；`AsyncRecomputeScheduler` 的方法解析顺序是 Async、AsyncScheduler、本类、（BalanceScheduler、）vLLM `Scheduler`；正文里没有调用 `schedule`，不会递归；profiler 挂的仍是本类自己的函数 |
| 调度行为 | 原版和移植版在两种基类下，8 个场景、每一步的调度决定（各请求 token 数、块号、已算 token 数、spec token、结束、抢占、退回 P、同步加载、公共前缀块）和发给客户端的输出都相同（`results/decisions_compare.txt`）。这 8 个场景包括 async（E5、E6）和退回 P 的 recompute 路径（E8，41 步）。两种基类之间也相同 |
| 记录 | E1–E7：两种基类下与 vLLM 的 `Scheduler` 逐条相同（遮去编号和时间以后）。E8 按设计不同：vLLM 抢占后恢复，共 71 步；Recompute 把请求退回 P，共 41 步。这个场景里移植版自身的记录是完整的（`results/records_vllm_vs_*.txt`） |
| 按步核对 | `check_steps.py` 在移植版的两种基类下都是 0 处失败（`results/check_steps.*.txt`）：步骤号由调度器设置、每步唯一、execute 和 sample 记录每步各一条并且配对。原版不通过这项核对，作为反向对照 |

没有核对的：DyntraLB 的两个变体（现场不用）没有实际运行，只确认了它们的 mixin 不定义 `schedule` 和 `_schedule`；vllm-ascend 自己的调度器单元测试没有重跑（对方报告 68/68）；没有在 NPU 上运行。
