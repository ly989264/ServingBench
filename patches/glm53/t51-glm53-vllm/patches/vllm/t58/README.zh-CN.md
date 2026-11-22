# T58 vLLM 补丁（glm5.3，记录格式约定 1.5.0）

依据：设计 `docs/tasks/T58/T58_设计.zh-CN.md`（`f08bfac5`）乙第 1、2、6 节，记录格式约定 1.5.0 第 8 节（t51-glm53-kit `23afecf`），以及 W1 对 D 在 1 级去掉 `submit` 的决定。在 T56 版（`../t56/`，现场补丁 `7959cbb7`）之上改，基线仍是干净的 vLLM `v0.30.0`（`ced6857a`）。

## 结论

- **加了什么：**
  - `pd_diagnostics.diag_level()`：读 `PD_DIAG_LEVEL`，进程里第一次用时读一次并缓存。vllm-ascend 主体和 p2d 也调它，解析只有这一处。
  - `diag_level` 字段（整数）：写进 `api_summary[scope=request]` 和 `core_summary[scope=request]`，**只在 1、2 级写**。
  - 新启动行 `PD_DIAG_STARTUP diag level=<n> source=<default|env|invalid>`：每个 EngineCore 进程一行、每个 Worker 进程一行，API 进程不写。
  - **D 在 1 级不写 `worker_summary[execute].stages.submit`**（约定 8.2）：KV 读取照常提交，只是不计时。P 各级不变，2、3 级不变。
- **改了哪些文件：** 25 个运行时文件里的 7 个，共 +95/−11 行：
  - `pd_diagnostics.py`：新函数；
  - `core.py`、`worker_base.py`：写启动行；`worker_base.py` 另在 `init_device` 之后为本进程定下 submit 记不记；
  - `output_processor.py`、`request.py`：写字段；
  - `kv_connector_model_runner_mixin.py`、`gpu/kv_connector.py`：三处 `kv.load_submit` 改用同一个函数，函数按上面的决定开或不开这个 span。
- **3 级（不设）和 T56 逐字段相同：** 记录、调度决定、已有的启动行都没有变化，只多那一行新启动行。P 和 T51 逐字段相同。
- **2 级：** 和 T56 相比，只在那两条汇总里多 `diag_level`。
- **1 级：** 和 2 级一样，另外 D 的 `worker_summary` 少 `stages.submit`。schedule 的子段任何一级都照记。

## 1. 产物

| 文件 | 内容 | SHA-256 |
|---|---|---|
| `t58-vllm-v0.30.0-pd-diag.patch` | 现场补丁：干净 v0.30.0 上的完整补丁（`git diff --full-index v0.30.0 <T58> -- vllm/`），25 个文件，和 T51、T56 是同一组文件，+2981/−359 行 | `cb5b63585b396fff91f1c47fc5e0bf33abdafede7828acef15825f6db5f4e236` |
| `t58-vllm-from-t56.patch` | 已装 T56 版的环境升级用：只含变了的 7 个文件，+95/−11 行 | `9aec9b72ee4aed520cbfa9c5328bf4368a1f18785f3b2a5f5d9fd50eb3f4076f` |
| `files/vllm/...` | 装好以后的 25 个运行时文件，替换用 | — |
| `SHA256SUMS.after` | 25 个文件装好以后应有的 SHA-256。和 T56 版比，只有这 7 个文件的值变了 | `999d4f72278da0d692f0c84c11b007d2580581b9d052f8193ae7bfe518f5ca33` |
| `SHA256SUMS.t56-changed` | 增量升级前，这 7 个文件在 T56 版里的 SHA-256（和 `../t56/SHA256SUMS.after` 里的同名行相同） | — |
| `lab/t58-vllm-v0.30.0-pd-diag.lab-tests.patch` | 只给实验室用：T56 lab 补丁的 20 个文件，加 T58 的单测，共 21 个文件 | `2f4a320cbcd05eb1b79ee7fbfec99c60c9681efbe2ac388dbe786e2d6901e04f` |

`SHA256SUMS.before`、`NEW_FILES.txt` 和 T51 版相同（`../`）。两条路径都核过，记录在 `verify/results/t58/apply_check.txt`：
- 增量补丁打在 T56 版的 25 个文件上：打之前 7 个文件和 `SHA256SUMS.t56-changed` 相符，打完 25 个文件和 `SHA256SUMS.after` 全部相符，和 `files/` 逐字节相同；
- 完整补丁打在干净 v0.30.0 上：25 个文件全部相符。

## 2. 改动

| 文件 | 改动 |
|---|---|
| `v1/metrics/pd_diagnostics.py` | 下面七项 |
| `v1/engine/core.py` | `EngineCore.__init__` 在已有的调度器启动行之后调 `diag.log_diag_level(logger, "engine_core")` |
| `v1/worker/worker_base.py` | `WorkerWrapperBase.init_device` 在已有的 `model_runner` 启动行之后，调 `diag.log_diag_level(logger, "worker")` 和 `diag.decide_kv_load_submit(self.vllm_config)` |
| `v1/engine/output_processor.py` | `_finish_pd_request` 写 `api_summary[scope=request]` 时带上 `**diag.diag_level_fields()` |
| `v1/request.py` | `pd_queue_transition("finished")` 写 `core_summary[scope=request]` 时带上 `**diag.diag_level_fields()` |
| `v1/worker/kv_connector_model_runner_mixin.py` | 两处 `diag.span("kv.load_submit", …)` 改为 `diag.kv_load_submit_span(getattr(kv_connector, "_vllm_config", None))` |
| `v1/worker/gpu/kv_connector.py`（V2） | 一处，同上，用 `self.vllm_config` |

`pd_diagnostics.py` 加的七项：
- `DiagLevel(level, source)`：NamedTuple。
- `parse_diag_level(raw)`：解析，规则见第 3 节。
- `diag_level()`：返回 `DiagLevel`。调用方取 `.level`，不要拿返回值直接和整数比较。
- `diag_level_fields()`：3 级返回 `{}`，1、2 级返回 `{"diag_level": n}`。
- `log_diag_level(log, role)`：写启动行。每个进程里每个角色只写一次，不抛异常。
- `decide_kv_load_submit(vllm_config)`：为本进程定下 `kv.load_submit` 记不记，规则见第 3 节。
- `kv_load_submit_span(vllm_config=None)`：按这个决定，返回原来的 span 或者空操作；本进程还没定时，先用传入的 config 定一次。

同一个进程里既有 EngineCore 又有 Worker 时（进程内执行器），两行启动行都写。

## 3. 口径

**解析**（约定 8.1，`parse_diag_level`）：

| `PD_DIAG_LEVEL` | 级别 | `source` |
|---|---|---|
| 不设、空串 | 3 | `default` |
| `3`、`2`、`1` | 同值 | `env` |
| 其余，如 `0`、`4`、`abc`、` 2 `（带空格）、`2x` | 3 | `invalid` |

只认 `"1"`、`"2"`、`"3"` 这三个字符串，不去空格。进程里第一次调用时读，以后都返回同一个值，改了要重启。

**字段**（约定 8.3）：
- `diag_level` 是生效的级别，不是环境变量的原文，整数。
- 写在两条汇总记录的末尾，位于 `completion` 之后、`stages` 之前；3 级不写。
- 其余记录（`core_summary[scope=execution]`、`worker_summary` 等）任何一级都不带这个字段。

**submit**（约定 8.2）：
- 在 D 上、1 级时，`kv.load_submit` 不开，所以 `worker_summary[execute]` 没有 `stages.submit`；`start_load_kv` 照常调用，异常照常抛出。
- 是不是 D 用 `is_pd_decode(vllm_config)`，与调度器、vllm-ascend 用的是同一个判断。
- 每个 Worker 进程在 `init_device` 之后用自己的 `vllm_config` 判一次，结果整个进程沿用。vllm-ascend 的 Worker 在构造时就初始化了它的 config（`d4ad49796` `worker/worker.py:153`），所以这时已经判得出。判的时候出任何错都保留 submit。
- 其余情况都照记：P 的各级、D 的 2 级和 3 级。
  - P 的 `worker_summary` 本来就有 `stages.submit`：只要有 KV connector 就有，等价驱动的 E2、E7、P1 三个场景都带。这一版不动它。

**启动行：**
- EngineCore 进程：紧跟在 `PD_DIAG_STARTUP scheduler=…` 之后，logger 为 `vllm.v1.engine.core`。
- Worker 进程：紧跟在 `PD_DIAG_STARTUP worker model_runner=…` 之后，logger 为 `vllm.v1.worker.worker_base`。
- INFO 级。已有的三种启动行一字未改。

## 4. 验证

用贴近现场的 Python 3.11.10 镜像（torch 2.10.0+cpu，transformers 5.14.1，tokenizers 0.22.2）跑 `verify/t58/run.sh`。同一个脚本、同样的输入，分别驱动三棵树：T51（`d3156e5e29`）、T56（`87152f9876`）、T58（`756ee5943d`）。结果在 `verify/results/t58/r311/`，汇总见其中的 `analysis.txt`（由 `verify/t58/analyze.sh` 生成）。

**D 驱动的一处调整（`verify/t58/d_equiv.py`）：** 这个驱动在一个进程里依次跑 D1–D4 和 P1 五个场景，Worker 也不经过 `init_device` 构造。现场每个实例是自己的进程，Worker 在 `init_device` 之后定下 submit 记不记。所以驱动在每个场景建 config 时，用这个 config 调一次 `decide_kv_load_submit`，和它已经为每个场景重新初始化 vllm-ascend 的 config 是同一种做法。旧的两棵树上没有这个函数，就不调。

| 项 | 做法 | 结果 |
|---|---|---|
| 3 级和 T56 逐字段相同 | 不设 `PD_DIAG_LEVEL`：`pd_equiv.py` 15 个场景、`ascend_equiv.py` 6 种调度器、`d_equiv.py` 两种基类上的 D 驱动（D1–D4，加配了 recompute 的 P1） | 223 条、335 或 245 条、各 84 条记录，差别都是 0 处；`kv_ready_wait` 逐轮核 0 处不符；没有一条记录带 `diag_level`；`stages.submit` 和 T56 一样多 |
| P 和 T51 逐字段相同 | T51 对 T58，`pd_equiv.py` 15 个场景和 6 种调度器 | 差别 0 处 |
| 2 级只多 `diag_level` | `PD_DIAG_LEVEL=2` 跑上面三个驱动，去掉 `diag_level` 以后和 T56 比 | 两条汇总全都带它，值是 2，例如 15 个场景里 `core_summary[request]` 24 条、`api_summary[request]` 8 条；别的记录都不带；去掉以后差别 0 处 |
| 1 级另少 D 的 submit | `PD_DIAG_LEVEL=1`，同上。D 驱动另从 T56 的 D1–D4 里去掉 `stages.submit` 再比 | `diag_level` 同上，值是 1。D 驱动两种基类上，T56 的 D1–D4 共 24 条带 submit，T58 是 0 条；P1 两边都是 5 条。这样去掉以后差别 0 处。`pd_equiv.py`、`ascend_equiv.py` 里没有 D，`stages.submit` 和 T56 一样多 |
| 启动行 | `startup_check.py` 9 个场景（EngineCore 5 种、Worker 4 种），分别在不设、`1`、`2` 时跑；另有 2 个场景在 `3`、空串、`0`、`4`、`abc`、` 2 `、`2x` 时跑 | 41 组：已有的行和 T56 逐字相同，只多一行 `diag`，位置紧跟在调度器行或 `model_runner` 行之后，内容符合对照表；EngineCore 构造两次也只写一行 |
| 新单测 | `test_pd_diag_level.py` 59 例，下面七组 | 全部通过 |
| 单测 | 01 的 pd 测试 9 个文件、调度器和 KV connector 的上游测试 12 个文件、`test_pd_kv_ready_wait.py` | T56 和 T58 共有的 648 个用例逐个结果相同。其余失败的 99 例和 1 个收集错误在 T56 上同样失败，原因见 T51、T56 的说明。`test_pd_compat.py` 改了一例的期望值：这个用例列出 `init_device` 之后的全部启动行，现在多了新的一行 |
| 所有调度器都当成 D | 插件让 `is_pd_decode()` 恒为真，跑 12 个调度器和 KV connector 测试文件 | 403 个用例和 T56 逐个相同；钩子调用次数也逐个相同 |
| 打补丁 | `apply_check.txt` | 两条路径都打得上，25 个文件全部对上 `SHA256SUMS.after` |
| 静态检查 | `pyflakes_py311.txt` | 25 个文件和新单测 0 条警告（T56 也是 0）；在 Python 3.11.10 上 25 个文件都能编译 |
| 个人信息扫描 | 增量补丁新加的行、`verify/t58/`、本说明 | IP、主机名、个人目录 0 处。lab 补丁里的 7 处 `127.0.0.1` 来自补丁 01 自带的工具，和 T56 相同 |

新单测 `test_pd_diag_level.py` 的 59 例分七组：
1. 对照表 10 个输入；
2. 只读一次；
3. 字段只在 1、2 级出现，用真实的调度器和真实的 `_finish_pd_request` 测；
4. 启动行，通过真实的 `EngineCore.__init__` 和 `init_device` 测，不抛异常；
5. submit 在真实的 mixin 里：D 1 级没有；D 2、3、不设、非法值，以及 P 1、2、不设都有；同步、异步两种加载下 `start_load_kv` 都只调一次；
6. submit 的决定：整个进程沿用，判断出错时保留，V2 的 connector 同样规则；
7. `init_device` 之后用 Worker 自己的 config 判，connector 没有 config 时照样生效。

## 5. 开销

- **3 级：**
  - 每个请求结束时，两条汇总各多一次 `diag_level_fields()`：读一次缓存、建一个空字典。
  - 每个 KV 步多一次 `kv_load_submit_span()`：读一个模块变量。
  - 每个进程多写一行启动行，多判一次 submit。
  - 没有新的计时，也没有新的日志行。
- **1、2 级：** 两条汇总各多一个整数字段。D 在 1 级每个 KV 步少一次 span：少取两次时刻，少写一个 stage。
- **没有另测数值：** T56 的测法测的是调度器每次调用的主机时长，而本版在调度循环里没有加任何代码。
