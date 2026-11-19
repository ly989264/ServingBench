# vLLM v0.30.0 诊断补丁（同事补丁 01 的移植，含 A2 开关）

## 结论

- **补丁**：`t51-vllm-v0.30.0-pd-diag.patch`，在干净的 vLLM `v0.30.0`（`ced6857a`，现场 P、D 都是这个版本，检测结果 `changed=0`）上 `git apply --check` 通过。现场补丁只含 `vllm/` 下的 25 个运行时源文件（修改 23 个、新增 2 个），替换件在 `files/vllm/`；两个新文件与 01 逐字节相同。补丁 01 从同事分支带来的工具、文档和全部测试（共 20 个文件）都不进现场补丁：其中 `tools/deploy_pd_metrics_d.sh` 含个人目录，整个去掉；其余 19 个放在实验室用的 lab 补丁里，只供实验室跑测试（第 1 节）。
- **做法**：01 能直接打上的部分原样使用（2 个新文件、7 个修改文件，以及 docs、tools 和新增的测试，后三类只在 lab 补丁里）。16 个冲突源文件和 3 个冲突测试文件按 v0.30.0 的代码逐处重做，测量点放在 01 所在的同一个函数、同一个调用周围，只去掉 v0.32.0 才有的上下文。补丁 01 以外只加了三类改动：`worker_base.py` 里统筹定的 A2 开关（C14），默认关闭（第 6 节）；5 处向后兼容的调整，让按原版 v0.30.0 写法调用的代码、替换原版函数的现场补丁都不会出错，也不会被绕过（第 13 节）；两行启动日志 `PD_DIAG_STARTUP`，在运行时报出实际构造的调度器和 model runner（第 14 节）。和 glm5.3 现场逐项的对应见第 15 节。
- **记录等价性**：同一组合成调用（15 个场景）分别驱动 v0.32.0 加 01 和 v0.30.0 加本补丁，两边各写出 223 条 `[PD_DIAG]` 记录，事件种类、scope、字段集合，以及遮去时间、编号后的取值都相同。差别只有 1 处，涉及 3 条记录：在不执行 forward 的那一步，v0.30.0 本身不调用 `wait_for_save`（第 9.3 节）。场景覆盖 async scheduling（两步重叠）、投机解码 k=5、同步加载，按步核对的 12 项不变式在两边全部通过。
- **单元测试**：01 新增的 136 个用例在两边结果逐个相同：121 个通过、2 个跳过、13 个失败。失败的 13 个在 v0.32.0 加 01 上以同样的报错失败，属于 01 自身。A2 的 11 个用例全部通过。各版本原版的调度器测试在补丁前后逐个结果相同。`test_preempt_during_execution` 的变化来自 01 自己加的断言，不是运行时行为的改变（第 9.2 节第 2 条）。
- **MTP k=5 下「engine 的 executions 等于 worker 的 forward 步数」**：按读码成立（第 10 节）；合成调用里同步调度和 async scheduling 都逐请求相等。前提是调度器没有换成 vllm-ascend 的几个重写类（第 11 节第 2 条）。

## 1. 产物

| 文件 | 内容 |
|---|---|
| `t51-vllm-v0.30.0-pd-diag.patch` | **现场补丁**：对 v0.30.0 的补丁（`git diff --full-index --binary v0.30.0 <移植> -- vllm/`），只含 `vllm/` 下的 25 个文件，+2634/−359 行 |
| `lab/t51-vllm-v0.30.0-pd-diag.lab-tests.patch` | **只给实验室用，不进现场**：01 的工具、文档和全部测试，19 个文件，打在现场补丁之上。打完两份补丁的树与测试所用的树逐字节相同（第 9.1 节） |
| `files/vllm/...` | 打完补丁后的 25 个运行时源文件，在容器里直接替换用 |
| `SHA256SUMS.before` | 23 个修改文件在原版 v0.30.0 里的 SHA-256，文件清单和 dsv4.1 那份逐行相同 |
| `SHA256SUMS.after` | 替换后 25 个文件应有的 SHA-256 |
| `NEW_FILES.txt` | 2 个新文件：`vllm/v1/metrics/pd_diagnostics.py`、`vllm/entrypoints/serve/utils/pd_diagnostics.py` |
| `../../verify/` | 验证脚本和结果（第 9 节） |

**现场补丁的范围（11-19 统筹定）。** 现场补丁只含运行时的 `vllm/`。补丁 01 从同事分支带来的 20 个文件不进现场补丁：

| 文件 | 去向 |
|---|---|
| `tools/deploy_pd_metrics_d.sh` | 整个去掉。这是同事自己的部署脚本，含个人目录和个人 fork，现场用不到 |
| `tools/deploy_pd_compact_dashboard.py`、`tools/grafana_pd_compact_dashboard.json`、`tools/grafana_rows_api.py`、`tools/pd_compact_fixture.py`、`tools/pd_grafana_json.py`、`tools/pd_metrics_report.py`、`tools/pd_request_json.py` | lab 补丁。同事自己的看板和报表工具，面板二不用；`tests/tools/test_pd_compact.py` 依赖它们 |
| `docs/design/metrics.md`、`docs/design/pd_compact_contract.md` | lab 补丁。同事的文档；前者仍有一句提到 `tools/deploy_pd_metrics_d.sh` 这个名字 |
| `tests/tools/test_pd_compact.py`、`tests/v1/core/test_scheduler.py`、`tests/v1/engine/test_async_llm_output_handler.py`、`tests/v1/engine/test_output_processor.py`、`tests/v1/metrics/test_pd_compact_batch.py`、`tests/v1/metrics/test_pd_compact_core.py`、`tests/v1/metrics/test_pd_compat.py`、`tests/v1/metrics/test_pd_diagnostics.py`、`tests/v1/metrics/test_stats.py`、`tests/v1/worker/test_pd_finish_claim.py` | lab 补丁。测试内容不变，照常在实验室跑 |

`vllm/` 里的运行时代码不依赖这些文件（`verify/check_runtime_refs.py`，结果在 `verify/results/site_patch_scan.txt`）。25 个运行时文件全文和补丁新加的行里，都没有对 `tools/`、`tests/`、`docs/` 的 import，没有拼这些路径的字符串，也没有 `__file__`、`subprocess`、`Path(...)`、`os.path`、`importlib`、`exec(` 这类按路径加载的写法。全文里出现这几个词的 24 行，都是 OpenAI 接口的 `request.tools` 字段和文档网址。

**现场补丁的扫描**（`verify/scan_site.py`，扫补丁的每一行，包括新加、删去和上下文行，以及 `files/` 的全文，结果在 `verify/results/site_patch_scan.txt`）：

| 项 | 结果 |
|---|---|
| `/data/`、`/home/`、`/root/` 下的个人目录 | 0 |
| 同事的个人 Mooncake 构建目录（已删除） | 0 |
| 邮箱 | 0；正则命中的 6 处都是装饰器 `@contextlib.contextmanager` |
| 字母加 8 位数字这类工号样式的串 | 0 |
| IP（含保留地址） | 0 |
| 主机名 | 0 |
| `github.com/<账号>` | 补丁里 0 处。`files/` 里 9 处，都是 v0.30.0 原有代码里的，干净树里同样数目：`vllm-project` 4 处（`bart-plugin` 2 处，`vllm/pull/35243`、`vllm/pull/22471`），`ray-project` 2 处，`QwenLM`、`huggingface`、`openai` 各 1 处 |

lab 补丁也扫过：同样没有个人目录、同事的个人 Mooncake 构建目录（已删除）和 GitHub 账号。命中的都是误报：
- pytest 装饰器 35 处，被当成邮箱；
- 测试数据里 glog 的日期前缀 `I20261112` 3 处；
- 文档里的 OpenCV 版本号 `4.11.0.86` 1 处；
- `127.0.0.1` 6 处。

用法和 dsv4.1 相同：检测结果为原版 v0.30.0 时替换这 25 个文件；已经是补丁后的状态时不动；文件被改过时先不动。现场装的不是可编辑安装（`src=local`），替换的是安装目录里的同名文件。本补丁必须和 glm5.3 的 vllm-ascend 补丁一起装，接口见第 5 节。

## 2. 基线和移植方法

- 基线：`v0.30.0` 标签（`ced6857a`）。补丁 01 的 SHA-256 为 `7cda4008c1fafe45ab809a26e9367f8ec24b6f79d779a9e02c33460efed470eb`，它的基线是 `v0.32.0`（`5df63a72`）。
- 01 在 v0.30.0 上打不上的是 16 个源文件和 3 个测试文件。对这 19 个文件，先以 v0.32.0 为共同祖先，把「v0.32.0 → v0.32.0 加 01」的改动三方合并到 v0.30.0 上，再逐块人工核对。冲突块按 v0.30.0 的代码重写；没有冲突的块也逐处核对所在函数和前后调用。
- 有两项自动检查，结果在 `verify/results/`：
  - **位置比对**（`placement.txt`）：把 01 和本补丁增删的每一行按所在的函数或类比较。7 个干净文件和 16 个冲突源文件里的 11 个逐行相同；另外 5 个（`chat_completion/serving.py`、`inputs/engine.py`、`v1/engine/async_llm.py`、`v1/engine/core.py`、`v1/worker/gpu/model_runner.py`）的差别全部是 v0.32.0 独有的上下文，或第 4 节说明的改写。`worker_base.py` 多出的行全部是 A2。
  - **静态检查**（`pyflakes.txt`）：25 个运行时文件在补丁后 0 条警告。这项检查找出过一处移植错误，已改正（第 4 节第 1 行）。

## 3. 测量点和段的对应

行号是补丁后 v0.30.0 的行号；括号里是方案第 1.2、1.3 节 B 列的原版行号。

| 段 | 记录和字段 | 测量点 |
|---|---|---|
| req_resolve | `api_summary[http].stages.req_resolve` | 中间件（新文件 `entrypoints/serve/utils/pd_diagnostics.py`）记入口，`chat_completion/api_router.py:56`、`completion/api_router.py:49` 的 `typed_handler` 记终点（B：`api_router.py:54`） |
| render（v3 计入 req_resolve） | `stages.render` | `renderers/base.py:1194`、`:1218`、`:1249`、`:1297`（`render_cmpl`、`render_chat` 及其异步版本）；Harmony：`online_renderer.py:547`（B：`base.py:512`） |
| tokenizer | `stages.tokenize`；`pd_tokenization` 边界 | `base.py:654`（同步）、`:712`（异步）；分词返回的时刻在 `base.py:594` 取 |
| add_request | `api_summary[engine_submission].stages.add_request` | `v1/engine/async_llm.py:600` 的 `_send_engine_request`（`:660` 记阶段）；ZMQ 发送完成由 `v1/engine/core_client.py:1193` 观察（B：`async_llm.py:361`、`:507`，`core_client.py:1197`） |
| schedule/queue_time | `core_summary[request].stages.queue` | 入队 `v1/core/sched/scheduler.py:2642`，首次候选 `:950`；汇总在 `v1/request.py:402`（B：`:2496`、`:870`） |
| schedule/hash | `stages.hash` | `v1/request.py:306` 的 `update_block_hashes`（B：`:278`） |
| schedule/lookup | `core_summary[execution].stages.lookup` | `v1/core/kv_cache_manager.py:317`、`:376`；connector 查找 `scheduler.py:1016–1044`（B：`kv_cache_manager.py:265`，`scheduler.py:937`） |
| schedule/alloc | `stages.alloc` | `kv_cache_manager.py:424` 的 `allocate_slots`（B：`:370`） |
| schedule/other | `stages.schedule`、`stages.other` | `scheduler.py:563` 的 `schedule` 外层，内层逻辑移到 `:640` 的 `_schedule`（B：`:561`） |
| 步骤号与每步汇总 | `execution_id`、`members[]`、`no_forward` | 第 7 节 |
| start_load_kv/submit | `worker_summary.stages.submit` | V1 runner：`v1/worker/kv_connector_model_runner_mixin.py:97`（有同步加载时在 forward 之前）、`:106`（其余在 forward 之后）（B：`:86–93`）；V2 runner：`v1/worker/gpu/kv_connector.py:92`（B：`:82`） |
| （Worker 侧保存等待，面板不用） | `worker_summary.stages.wait_for_save` | mixin `:66`、`:112`（B：`:95`）；V2 `gpu/kv_connector.py:123`（B：`:111`） |
| forward、sampling | `stages.forward`、`stages.sampling` | V2 runner：`v1/worker/gpu/model_runner.py:1919`、`:1948`、`:1959`、`:1969`、`:1557`、`:1565`。glm5.3 默认的 V1 runner 在 vllm-ascend 一侧 |
| detokenizer | `api_summary[request].stages.detokenize` | `v1/engine/output_processor.py:769`；请求终态 `:894` |
| 官方口径汇总 | `api_summary[request].official` | `v1/metrics/stats.py:587` |

**forward、sampling 的口径。** vLLM 这边（V2 runner 的计时）和 01 一样只计 host 侧耗时（C8）。glm5.3 的 D 是图模式（`enforce_eager=false`）加 async scheduling，host 侧量到的只是发射命令的耗时，所以按 C14，V1 runner 的 forward、sampling 另记设备事件时长 `device_duration_ns`。这个字段由 vllm-ascend 一侧写入，本补丁只提供推迟写出的开关（第 6 节）；原有的 host 侧 `duration_ns` 不变。

## 4. 16 个冲突源文件

「差别」一列只写和 01 原版不一样的地方；没写的，测量点、字段和取值都与 01 相同。

| 文件 | 01 改了什么 | v0.30.0 上怎么做 | 和 01 原版的差别 |
|---|---|---|---|
| `entrypoints/openai/chat_completion/serving.py` | 请求号关联、首个生成内容时刻、生成错误 | 冲突只在导入行，非流式路径的 `choice_index` 另作处理 | v0.32.0 的非流式循环是 `for i, output in enumerate(...)`（上游 #58939 引入），v0.30.0 是 `for output in ...`，没有 `i`。三方合并的初稿原样用了 `i`，合成调用时报 `NameError`，pyflakes 也查出这一处。现改为 `len(choices) - 1`：每轮只追加一个 choice、循环里没有 `continue`，取值与 01 的 `i` 相同 |
| `inputs/engine.py` | `pd_tokenization` 两个 TypedDict 字段 | v0.30.0 的 `NotRequired` 来自 `typing_extensions`，只补导入 `Any` | 无 |
| `renderers/base.py` | tokenize、render 计时，分词结束边界 | 自动合并无冲突，9 处改动所在的函数逐一与 01 相同 | 无 |
| `renderers/online_renderer.py` | Harmony 渲染和复用 token 时的 render、tokenize 记录 | 同上 | 无 |
| `v1/core/kv_cache_manager.py` | `_pd_range` 装饰 6 个方法（lookup、alloc 等） | 6 个方法在 v0.30.0 同名同签名 | 无 |
| `v1/core/sched/scheduler.py` | `schedule` 拆成外层计时加 `_schedule`；queue、lookup、members、身份字段 | 唯一的冲突是 v0.32.0 才有的 `skip_reading_prefix_cache` 两行，已去掉；其余 9 处逐一核对，变量都在作用域内 | 无 |
| `v1/engine/__init__.py` | `EngineCoreRequest.pd_identity`、`pd_tokenization`；`EngineCoreOutput.pd_execution_id`、`output_batch_id`；`EngineCoreOutputs.output_batch_id` | 追加在 v0.30.0 各结构的末尾 | 字段在 msgspec 按位序列化里的位置与 v0.32.0 不同，因为 v0.32.0 在前面多了 `kv_hints`、`prompt_token_id_logprobs`、`weight_version`。各进程运行同一版本，不受影响；补丁前后的进程不能混跑，这一点与 01 相同 |
| `v1/engine/async_llm.py` | `_api_submission`、`_send_engine_request`、`_record_pd_failure`，add_request 整段包进提交记录 | 三个新函数与 01 逐字相同；add_request 和流式输入保留 v0.30.0 自己的业务流程，在相同的位置加记录 | 不改业务流程。n>1 时，v0.30.0 在函数开头做准入检查，并逐个子请求「登记后立即发送」；v0.32.0 是先全部登记再统一发送，出错时还会主动 abort。本补丁沿用 v0.30.0 的做法，不加 abort，只在出错时补写诊断终态。所以 n>1 时 `api_summary[engine_submission].links` 的顺序与 01 不同，各项内容相同（第 9.3 节）。v0.30.0 的流式输入总会发送最后一个信号，这一点也沿用 |
| `v1/engine/core.py` | `_pd_schedule`、`_pd_execution_scope`，重写 `step`、`step_with_batch_queue`，输出带步骤号 | 用 01 的版本替换这两个函数，去掉 v0.32.0 才有的 `_attach_weight_version`，保留 v0.30.0 的 `is_ec_consumer` 判断（v0.32.0 改名为 `is_mm_encoder_only`，含义取反） | 无 |
| `v1/engine/input_processor.py` | 跨线程池传递诊断上下文，请求带 `pd_identity`、`pd_tokenization` | 去掉 v0.32.0 才有的 `kv_hints` | 无 |
| `v1/engine/output_processor.py` | 去分词计时、请求终态 | 去掉 v0.32.0 才有的 `_trim_surplus_tokens`、`num_prev_tokens`、`remote_prefill_cached_tokens` | 无。结束请求的路径两版数量相同，逐一核对过 |
| `v1/metrics/stats.py` | `RequestStateStats.pd_batch`；官方口径写入请求汇总 | 去掉 v0.32.0 才有的水印字段上下文 | 无 |
| `v1/request.py` | hash、queue 计数，请求汇总 | 去掉 `kv_hints` 上下文 | 无 |
| `v1/worker/gpu/kv_connector.py`（V2） | submit、wait_for_save 计时，传输完成关联 | v0.30.0 的 `post_forward` 还有 `wait_for_save` 参数（v0.32.0 已删），计时放在这个条件里面 | 不执行 forward 的那一步，v0.30.0 本身传 `wait_for_save=False`、不调用 `wait_for_save`，所以没有 `stages.wait_for_save` |
| `v1/worker/gpu/model_runner.py`（V2） | forward、sampling 计时，`@pd_execution`、`@pd_sample` | rejection sampler 的调用去掉 v0.32.0 才有的 `invalid_drafts` 参数 | 无。glm5.3 默认用 V1 runner，这个文件只在显式启用 V2 时才生效 |
| `v1/worker/kv_connector_model_runner_mixin.py`（V1） | submit、wait_for_save 计时，延迟保存的上下文 | 同 V2，保留 `wait_for_save` 条件 | 同 V2 |

**7 个干净文件**（`chat_completion/api_router.py`、`completion/api_router.py`、`completion/serving.py`、`inputs/llm.py`、`v1/core/sched/output.py`、`v1/engine/core_client.py`、`v1/worker/worker_base.py`）原样打上，并逐一核对了前后调用：`parallel_config.data_parallel_index`（v0.30.0 `config/parallel.py:960` 赋值）、`WorkerWrapperBase.execute_model`、`AsyncMPClient._send_input` 在 v0.30.0 都存在，语义相同。`worker_base.py` 在此之上另加 A2（第 6 节）。

## 5. 对 vllm-ascend 暴露的接口

glm5.3 的 vllm-ascend 补丁（t51-glm53-ascend、t51-glm53-p2d）直接 import 下面这些名字。名字和签名都与补丁 01 相同，任何一个缺失，Worker 一启动就会 ImportError。

| 位置 | 名字和签名 | 和 01 的关系 |
|---|---|---|
| `vllm.v1.metrics.pd_diagnostics` | `new_id(kind) -> str`、`current() -> dict`、`current_batch() -> Batch \| None`、`scope(**fields)`（上下文管理器）、`collect(value)`、`batch(event, **identity)`（上下文管理器）、`emit(event, **fields)`、`span(event, **fields)`（上下文管理器）、`clock_domain()`；`Batch(event, **identity)` 的 `update(**fields)`、`add_stage(name, *, start_ns=None, end_ns=None, duration_ns=None, calls=1, status="ok", **attributes)`、`add_link(kind, **identity)`、`capture(event, **fields)`、`finish(status="ok", **fields)` | 文件与 01 逐字节相同 |
| `vllm.v1.worker.worker_base` | `pd_execution(method)`、`pd_sample(method)`、`pd_execution_context(owner, scheduler_output) -> dict` | 语法树与 01 相同，只有一处不同：两个装饰器里原来调 `diag.batch("worker_summary", phase=...)` 的地方，改为调本模块的 `_pd_worker_batch(phase, context)`。没有 runner 调 `pd_claim_finish()` 时，写出的记录与 01 逐字段相同（第 6 节测试 1） |
| `vllm.v1.worker.worker_base`（A2，新增） | `PdFinishClaim.set_stage_fields(stage, **fields) -> bool`、`PdFinishClaim.release() -> None`、`pd_claim_finish() -> PdFinishClaim \| None`、`pd_release_claims() -> None` | 第 6 节 |
| `vllm.v1.core.sched.output.SchedulerOutput` | `pd_execution_id: str \| None = None`（`output.py:263`） | 与 01 相同；赋值见第 7 节 |

vllm-ascend 侧另外依赖本补丁的两个行为：`ParallelConfig.data_parallel_index` 是 Worker 记录 `dp_rank` 的来源；`_pd_executing` 和 `_pd_sampling` 防重入，vllm-ascend 的 runner 覆盖了 `execute_model` 再调回 vLLM 的同名方法时，只生成一条记录。

## 6. A2 开关（C14：runner 接管一步 `worker_summary` 的写出）

**来源和范围。** 统筹定的 C14（用户选方案 A）。接口由 t51-glm53-ascend 的 `docs/A2_INTERFACE.zh-CN.md` 规定，代码与它的参考实现 `joint/provisional-vllm/worker_base_a2_spec.py` 相同：对 01 的 `worker_base.py` 做 4 处文本替换，计数核对后打上，增加 120 行、修改 3 行。`pd_diagnostics.py` 不改，不新增事件种类，被包住方法的返回值和异常都不变。

| 名字 | 行为 |
|---|---|
| `pd_claim_finish()` | 在被 `pd_execution` 或 `pd_sample` 包住的方法里、同一线程上调用。效果是：方法正常返回时，包装器不写这条记录，改由返回的 claim 写出。当前没有打开的 `worker_summary` 时返回 `None`（dummy run、图捕获、包装器之外）。同一条记录重复调用，返回同一个 claim |
| 方法抛异常 | 包装器立刻写出，`status`（`error` 或 `cancelled`）和 `error_type` 与原来相同，不交给 claim。此后这个 claim 的 `release()` 什么都不做，`set_stage_fields()` 返回 `False` |
| `PdFinishClaim.set_stage_fields(stage, **fields)` | 往还没写出的记录的 `stages[stage]` 里加字段，用 `Batch` 自己的锁。阶段不存在或记录已写出时返回 `False` |
| `PdFinishClaim.release()` | 可重复调用，任何线程都能调。方法还没返回时调用，相当于取消推迟，记录在方法返回时照常写出；方法已返回后调用，立刻写出，状态用包装器当时定下的值（正常返回为 `ok`，`error_type` 为 `null`） |
| `pd_release_claims()` | 写出所有「方法已返回、还没 release」的记录；模块导入时用 `atexit` 注册 |

**默认关闭。** vLLM 自己的代码不调用 `pd_claim_finish()`。只要 vllm-ascend 的 runner 也不调用，记录的写出时刻和内容都与 01 相同。

**测试**（`tests/v1/worker/test_pd_finish_claim.py`，11 个用例，全部通过）：

1. 开关关闭时与原版逐条相同：把 `_pd_worker_batch` 临时换回 01 的 `diag.batch(...)` 跑一遍，再用 A2 跑一遍，正常返回、`ValueError`、`KeyboardInterrupt` 三种情况下，execute、sample 两条记录逐字段相同（时间、编号除外）；
2. claim 正常 release：返回后、release 前不写出；`set_stage_fields` 加的字段进入记录；release 两次只写出一次；写出以后 `set_stage_fields` 返回 `False`；
3. 同一条记录重复调用 `pd_claim_finish()`，返回同一个对象；
4. 方法返回前 release：在返回时写出，只写一次；
5. 抛异常时立即写出：`ValueError` 对应 `status=error`，`KeyboardInterrupt` 对应 `status=cancelled`，`error_type` 正确；之后 release 不再写出；
6. 从另一个线程 release：写出一次；
7. 没有打开的记录（包装器之外、dummy run）时返回 `None`；
8. `pd_release_claims()` 写出被扣住的记录，再调一次不重复写出；
9. 进程退出：在子进程里扣住一条记录、不 release，正常退出时由 `atexit` 写出，而且写在方法返回之后。

01 自己的 `test_pd_compact_batch.py` 会把 `pd_execution`、`pd_sample` 从源文件里单独编译出来测试。A2 以后这两个装饰器还依赖 `_pd_worker_batch` 及其辅助对象，所以这个测试改为把它们一起编译；断言没有改动，11 个用例照常通过。

**兜底写出的边界。** `atexit` 只在解释器正常退出时运行；进程被 `SIGKILL` 杀掉或调用 `os._exit` 时不会运行，扣住的记录会丢失。glm5.3 约定由 vllm-ascend 的 runner 自己负责兜底：最多推迟 4 步，下一步开始时补写（契约 glm5.3 版 1.3.1 第 1 节）。

## 7. 步骤号 `pd_execution_id`：在哪里赋值，async scheduling 下是否每步都有

- **赋值。** vLLM 自己的 `Scheduler.schedule()` 外层（`scheduler.py:564`）每次调用生成一个 `execution-<run_id>-<EngineCore 的 pid>-<序号>`，放进诊断上下文；内层 `_schedule()` 构造 `SchedulerOutput` 时写入 `pd_execution_id`（`:1513`），然后才调用 connector 的 `build_connector_meta`（`:1542`），所以 `MooncakeConnectorV1`、`AscendStoreConnector` 在建元数据时已经能读到它。EngineCore 的 `_pd_schedule`（`core.py:631`）只在调度器没设时补一个，正常路径下不会走到。
- **async scheduling。** v0.30.0 开了 async scheduling 时 `max_concurrent_batches` 为 2（`config/vllm.py:589–599`），EngineCore 走 `step_with_batch_queue`（`core.py:244–246`、`:746`）。调度器是 `AsyncScheduler`，它不覆盖 `schedule()`（`async_scheduler.py`），所以每一步都经过上面的赋值。batch queue 里每一项是（future、`SchedulerOutput`、执行 future、这一步的 `core_summary`）四元组，取出时用的就是这一步自己的 `SchedulerOutput` 给输出带步骤号、写这一步的汇总，后面一步已经提交也不会串号。
- **测试**（`verify/check_steps.py`，两边各跑一遍，结果在 `results/check_steps.*.txt`）。在 7 个引擎场景里逐步断言下面 8 项：`Scheduler.schedule()` 返回时 `pd_execution_id` 已设好（在 EngineCore 补号之前取）；每步唯一；`execute_model` 按调度顺序收到每一步的号；每步正好一条 `core_summary[execution]`；每步正好一条 `worker_summary[execute]`，号相同；每个执行了模型的步（调度 token 数大于 0）正好一条 `worker_summary[sample]`，号和 `request_ids` 与这一步的 execute 记录相同，没有调度 token 的步没有 sample 记录（EngineCore 对这种步不调 `sample_tokens`）；每个带 token 的输出带着调度过它的那一步的号；同一步的 core 记录和 worker 记录 `request_ids` 相同。async 场景另加一项「两步重叠」：后一步开始调度时，前一步的汇总还没写出。E5（async）、E6（async 加 k=5）里这种情况各出现 6 次。两边全部通过。

## 8. 两点读码确认

- **D 的执行器和记录所在的进程。** D 由 `launch_online_dp.py --data-parallel-size` 启动，`world_size_across_dp > 1`，v0.30.0 默认选 `mp`（`config/parallel.py:966`），用的是 `MultiprocExecutor`，不是进程内的 UniProc。所以 D 上的 Worker 记录写在 Worker 进程里，和 EngineCore 分开，情况与 P 相同。两边靠 `execution_id` 关联：这个号由 EngineCore 生成，随 `SchedulerOutput` 广播给每个 Worker，字符串本身全集群唯一，与在哪个进程里写无关。
- **`KVOutputAggregator`。** `MultiprocExecutor` 收集各 rank 的输出时经过 `KVOutputAggregator`（`multiproc_executor.py:397–409`），它只合并 `KVConnectorOutput`。01 的 Worker 计时都由各进程直接写日志，没有给 `KVConnectorOutput` 加字段，所以不受聚合器影响。

## 9. 验证

全部在 `docker run --rm` 容器里做，没有在本机 Python 环境里运行。

### 9.1 打补丁（`verify/results/apply_check.txt`）

| 检查 | 结果 |
|---|---|
| 23 个文件对照 `SHA256SUMS.before`（干净 v0.30.0） | 0 处不符 |
| 01 原版打在干净 v0.30.0 上 | 19 个文件打不上（16 个源文件 + 3 个测试文件），与方案第 5.1 节相同 |
| 现场补丁的文件 | 25 个，`vllm/` 以外 0 个 |
| 现场补丁 `git apply --check` | 通过（rc=0），随后 `git apply` 成功 |
| lab 补丁的文件 | 19 个，`vllm/` 下 0 个；打在现场补丁之上 `git apply --check` 通过 |
| 打完两份补丁的源码树 vs 测试所用的源码树 | 相同（`diff -r` 无输出；逐文件比对 7176 项，内容 0 处不同） |
| 25 个文件对照 `SHA256SUMS.after` | 0 处不符 |
| 干净 v0.30.0 加 `files/` 替换 | `vllm/` 与打补丁的结果相同 |

### 9.2 单元测试（`verify/results/unit_tests_summary.txt`、`unit_tests_compare.txt`）

环境：python 3.12.3、torch 2.13.0+cpu、transformers 5.19.0、tokenizers 0.23.3、pydantic 2.14.3、fastapi 0.136.3，aarch64。各源码树都以 `VLLM_TARGET_DEVICE=empty pip install -e . --no-deps --no-build-isolation` 安装；运行时设 `VLLM_TARGET_DEVICE=cpu`，因为 `empty` 平台下 `VllmConfig` 推断不出设备。环境上的三处处理：

- glm5.3 用的是 transformers 5.14.1，但它要求 tokenizers ≤ 0.23.0，这台机器多次下载都不完整，所以各源码树都用 5.19.0。受影响的只有多模态（LLaVA）用例。
- 仓库的全局清理 fixture 会调用加速卡显存接口。插件 `verify/t51_cpu_env.py` 给用例加上仓库自带的 `skip_global_cleanup` 标记，关掉这一步。
- 需要 HF 授权的 `meta-llama/Llama-3.2-1B` 换成 `facebook/opt-125m`。

对照组是干净 v0.30.0、干净 v0.32.0、v0.32.0 加 01。

| 文件 | v0.30.0 + 本补丁 | v0.32.0 + 01 |
|---|---|---|
| `tests/v1/worker/test_pd_finish_claim.py`（A2，新增） | 11 通过 | — |
| `tests/v1/metrics/test_pd_diagnostics.py` | 59 通过，2 跳过 | 59 通过，2 跳过 |
| `tests/v1/metrics/test_pd_compact_core.py` | 23 通过 | 23 通过 |
| `tests/v1/metrics/test_pd_compact_batch.py` | 11 通过 | 11 通过 |
| `tests/tools/test_pd_compact.py` | 27 通过 | 27 通过 |
| `tests/v1/engine/test_async_llm_output_handler.py` | 8 失败 | 8 失败 |
| `tests/v1/core/test_scheduler.py` | 128 通过，42 失败 | 208 通过，3 失败 |
| `tests/v1/engine/test_output_processor.py` | 30 通过，8 失败 | 45 通过，8 失败 |
| `tests/v1/metrics/test_stats.py` | 12 通过，2 失败 | 15 通过，2 失败 |

逐个用例对照的结论：

1. **01 新增的 136 个用例，两边结果逐个相同**：121 个通过；2 个跳过（只在 DP 下才通知协调器的参数组合）；13 个失败，在 v0.32.0 加 01 上报错相同，属于 01 自身：
   - `test_output_batch_boundaries` 8 个、`test_request_summary_requires_generated_token_for_first_token` 2 个、`test_resumable_stream_close_emits_one_terminal` 1 个。这 3 组断言的是单独写出的 `request_summary`、`frontend_request_terminal`、`api_output_received` 记录；01 的代码里 `request_summary` 只并入请求汇总，后两种事件没有任何代码写出。
   - `test_execution_members_report_current_prefill_chunk` 1 个。
   - `test_independent_dense_replica_diagnostics_keep_global_rank` 1 个，它调用了 01 里不存在的 `_pd_execution_span`。
2. **`test_preempt_during_execution` 的结果变化来自 01 改过的测试，运行时行为没有变。**
   - 补丁后的测试文件里，这个用例从通过变成失败（`assert 0 == 1`，第 1342 行）。在 v0.32.0 上打 01 也一样。
   - 原因是 01 改了这个上游用例：在原有断言（`running` 里只剩第一个请求、第二个请求为 `PREEMPTED`）之后，加了三行，要求日志里有一条带 `"transition":"preempted"` 的 `[PD_DIAG]` 记录。01 的代码里没有任何地方写这种记录：`Request.pd_queue_transition` 只处理 `queued`、`candidate`、`finished` 三种。所以失败在 01 自己加的断言上，原有断言都通过。这是 01 内部测试和代码不一致，本补丁照原样移植这个测试，没有加这种记录。
   - 为证明运行时行为没有变，把各版本原版的、没有改过的 `tests/v1/core/test_scheduler.py` 分别在干净安装和补丁后安装上跑（`verify/run_upstream_sched.sh`，结果在 `results/upstream_scheduler_tests.txt`）。v0.30.0 的 167 个用例、v0.32.0 的 208 个用例，补丁前后逐个结果相同，0 处差别。`test_preempt_during_execution` 在两边都通过，抢占时机和调度顺序都没有变。
3. **两边都失败的上游用例是环境原因**：39 个调度器用例要导入 LLaVA，在 transformers 5.19.0 下失败；7 个 `test_stop_token` 用例要求 meta-llama。干净 v0.30.0 和补丁后失败的用例集合相同。

测试文件的移植：

| 文件 | 处理 |
|---|---|
| `tests/v1/core/test_scheduler.py` | 自动合并无冲突；补导入 `EngineCore`（v0.32.0 的这个文件本来就导入它，01 新增的一个用例要用） |
| `tests/v1/engine/test_output_processor.py` | 冲突在导入和 v0.32.0 才有的采样掩码用例，后者不移植；补导入 `STREAM_FINISHED`、`EngineCoreOutput` |
| `tests/v1/metrics/test_stats.py` | 冲突在导入，补 `json`、`pytest`、`EngineCoreOutput` |
| `tests/v1/metrics/test_pd_compact_core.py`（01 的新文件） | EngineCore 替身用的是 v0.32.0 的属性名 `is_mm_encoder_only = False`，改为 v0.30.0 的同义属性 `is_ec_consumer = True` |
| `tests/v1/metrics/test_pd_compact_batch.py`（01 的新文件） | 把 A2 的辅助对象和两个装饰器一起编译（第 6 节） |

### 9.3 记录等价性（`verify/results/records_compare.txt`）

`verify/pd_equiv.py` 在两套安装上执行同一组合成调用，收集全部 `[PD_DIAG]` 记录。

- **真实执行的代码**：OpenAI 路由和 serving 类、渲染器和分词、InputProcessor、AsyncLLM、`AsyncMPClient._send_input`、msgpack 编解码、EngineCore 的 `step` / `step_with_batch_queue`、Scheduler / AsyncScheduler、KVCacheManager、Request、OutputProcessor、IterationStats、`WorkerWrapperBase`、`pd_execution` / `pd_sample`、V1 KV connector mixin。
- **换成假的**：模型（采样出的 token）、ZMQ 套接字、进程边界、分布式组。runner 的 forward、sampling 计时是在假 runner 里按 vllm-ascend V1 runner 的位置补上的。

场景：

| 场景 | 内容 |
|---|---|
| E1 | 前缀缓存、分块 prefill、同步调度 |
| E2 | D 侧异步取远端 KV（`WAITING_FOR_REMOTE_KVS`），投机解码 k=5 |
| E3 | batch queue |
| E4 | 运行中 abort，然后 shutdown |
| E5 | async scheduling：AsyncScheduler 加 batch queue 2；模型输出由另一个线程延迟 5 ms 完成，两步重叠 |
| E6 | async scheduling 加 k=5。配置只允许 async scheduling 搭配设备侧 draft，所以 draft 方法用 `ngram_gpu`；在调度器这边都是 k=5 个占位 token |
| E7 | P 侧同步加载（`has_sync_kv_loads`），`start_load_kv` 人为耗时 20 ms |
| F1–F8 | HTTP：completions 非流式、流式、n=2、token id 输入；chat 非流式、流式带 usage；超长被拒；带 `kv_transfer_params` |

**比较方法。** 按场景和（event，scope 或 phase）比较记录条数和展开后的字段路径集合；遮去时间、进程号、线程号、运行号和请求号的随机后缀以后，再比较全部取值。由 Python 集合生成的列表（如 `finished_request_ids`）按内容排序后再比；两边都设 `PYTHONHASHSEED=0`。

**结果。** 两边各 223 条记录，15 个场景里各类记录的条数都相同。剩下的差别逐条如下：

| # | 差别 | 为什么可以接受 |
|---|---|---|
| 1 | 3 条 `worker_summary[phase=execute]`（E2 两条、E7 一条，都是 `no_forward=true`、`request_ids` 为空的那一步）：01 有 `stages.wait_for_save`，本补丁没有 | v0.30.0 的 `kv_connector_no_forward` 本身就传 `wait_for_save=False`，不调用 `wait_for_save`。01 的约定是「实际执行了对应动作才有阶段记录」，没有调用就没有这个阶段，而不是写一个空值。面板二各 profile 都不读这个字段：P 的后台 wait_for_save 段取自 `pool_task[save]`；glm53 profile 读 Worker 记录的只有 submit、forward、sampling、wait_for_put。同一步的 `stages.submit` 两边都有 |
| 2 | F3（n=2）的 `api_summary[engine_submission].links` 顺序不同，按内容排序后相同（`records_compare_ordered.txt` 是不排序的对照） | 见第 4 节 `async_llm.py` 一行：v0.30.0 逐个子请求登记后发送，v0.32.0 先全部登记再发送，链接按调用顺序追加。过滤器和采集按 `kind` 取链接，不依赖顺序 |

**同步加载时 `stages.submit` 的范围**（E7，结果在 `check_steps.*.txt`）。有同步加载的那一步，`stages.submit` 在 forward 之前结束，并且包含了 20 ms 的人为加载耗时：v0.32.0 加 01 上是 21.3 ms，本补丁是 20.6 ms。其余各步的 submit 都在 forward 之后开始。两边的包围范围相同，都只包 `start_load_kv` 这一次调用（mixin `:97` 和 `:106`）。glm5.3 的 P 不取这个整步值（契约 glm5.3 版 1.1 的 C11），这里只是核对口径。

### 9.4 静态检查

`verify/results/pyflakes.txt`：25 个运行时文件在补丁后 0 条警告，01 在 v0.32.0 上也是 0 条。

## 10. MTP k=5 下「engine 的 executions 等于 worker 的 forward 步数」

按读码成立。engine 侧取 `core_summary[request].members[0].executions`；worker 侧沿用 T50 的取法：`tp_rank` 为 0、`phase=execute`、`stages.forward.duration_ns` 是整数、`request_ids` 含这个请求的记录条数。

1. **engine 每调度一次，一个请求最多计一次。** `scheduler.py:1564` 在每次 `_schedule()` 的末尾，给 `num_scheduled_tokens` 里的每个请求加 1。投机解码时一个请求一步调度 1+k 个 token，但在 `num_scheduled_tokens` 里只占一项。async scheduling 下也是每次调度加一次（第 7 节）。
2. **每次调度对应一次 `execute_model`，生成一条 Worker 记录。** `core.py:682` 和 `:746` 里，每个调度结果只调一次 `model_executor.execute_model`。`worker_base.py` 的 `pd_execution` 为每次非 dummy 调用生成一条 `worker_summary[phase=execute]`，并且有防重入；它的 `request_ids` 直接取自同一个 `scheduler_output.num_scheduled_tokens`，和 engine 计数用的是同一个集合。A2 推迟的只是写出时刻，不改变条数。
3. **draft 模型不产生 execute 记录，也不进 forward 阶段。** glm5.3 的 `deepseek_mtp` 在 vllm-ascend `d4ad49796` 里属于 `mtp` 方法，proposer 是 `AscendEagleProposer` 或 `AscendMultiKVCacheGroupMTPProposer`（`spec_decode/__init__.py:41–70`）。draft 在 `sample_tokens`（`worker/model_runner_v1.py:2763`）里经 `propose_draft_token_ids`（`:1896`、`:2827`）执行，k 次 draft 都在 `spec_decode/llm_base_proposer.py:1491` 和 `:1743` 直接调 `self.model`，不经过 runner 的 `_model_forward`（`:3191`）。目标模型在 `:3241–3249` 调 `run_model()`，那里是包住 forward 的位置。`spec_decode/` 目录里没有任何地方调用 `_model_forward`。`sample_tokens` 的记录是 `phase=sample`，不计入 forward 步数。verify 和目标模型打分在同一次目标 forward 里完成，算 1+k 个位置。
4. **两边都不计的情况。** D 首次候选那一步，请求处于 `WAITING_FOR_REMOTE_KVS`，不在 `num_scheduled_tokens` 里，engine 不计；vllm-ascend runner 在没有 token 时走 `kv_connector_no_forward`（`model_runner_v1.py:2336`、`:2351`），这一步的记录没有 forward 阶段，worker 也不计。DP 空闲 rank 走 `_dummy_run`，既不经过 `pd_execution`，也不经过 `_pd_schedule`。
5. **合成调用的结果**（`verify/results/executions_vs_forward.txt`，Worker 侧是替身）。E2（同步调度）和 E6（async）都是 k=5、每请求 8 个输出 token：E2 的 executions 为 3 和 4，E6 为 6 和 4，都与 forward 步数逐请求相等。E1、E3、E5、E7 也都相等，两套安装结果相同。

## 11. 没有验证的部分和需要注意的地方

1. **没有在昇腾上运行。** 没有真实模型、NPU、多进程 ZMQ、DP 协调器和 PP。V2 runner 的 forward、sampling 计时只验证了能导入、位置与 01 相同，没有执行。glm5.3 的 V1 runner 在 vllm-ascend 一侧，合成调用里用替身；A2 的实际使用方也在那边。
2. **vllm-ascend 的调度器子类会绕过 01 的调度器埋点。** `d4ad49796` 里 `RecomputeScheduler`（`core/recompute_scheduler.py:180`）、`ProfilingChunkScheduler`（`core/scheduler_profiling_chunk.py:265`）、`DyntraLBScheduler`（`core/dyntra_lb_scheduler.py:423`）都完整重写了 `schedule()`，不调用 `super().schedule()`。`ShortRequestFirstAsyncScheduler` 自己不定义 `schedule()`，不在此列（t51-glm53-ascend `docs/SCHEDULER_BYPASS.zh-CN.md`）。

   01 把 queue 的「首次候选」、`schedule` 和 `other` 阶段、`members`/`executions`，以及调度器赋的 `pd_execution_id` 都放在 vLLM 的 `schedule()`/`_schedule()` 里。用了上面任何一个类，这些字段就会缺失：EngineCore 会补一个步骤号，lookup、alloc 仍然有。glm5.3 现场的情况：
   - D 用 `AsyncRecomputeScheduler`，vllm-ascend 一侧已经移植（t51-glm53-ascend `ef5bd17`，本仓库 `review/recompute-scheduler/`）；
   - P 用 `BalanceScheduler`，balance 关着，走 01 的包装；
   - ProfilingChunk、DyntraLB 现场没有打开。

   第 14 节的启动日志会在运行时报出实际构造的类和这几个开关。
3. **测试环境。** 全部测试在两套环境里各跑了一遍，第 15 节第 2 条：
   - 原来的镜像：Python 3.12.3、transformers 5.19.0；
   - 贴近现场的镜像：Python 3.11.10、transformers 5.14.1、torch 2.10.0 CPU 版。

   现场的 torch 是 2.10.0a0 加 torch_npu 2.10.0.post4，NPU 版装不到 CPU 容器里。
4. **A2 兜底的边界**（第 6 节）：进程被强杀时，扣住的记录会丢失。
5. **开销没有测。**

## 12. 已知问题

### 12.1 01 自带的测试问题（原样保留）

以下都是补丁 01 自己的测试和代码不一致：在 v0.32.0 加 01 上以同样的方式失败。本补丁照原样移植这些测试，不删断言、不改代码，也不为它们新增记录或字段。

1. **`tests/v1/core/test_scheduler.py::test_preempt_during_execution`**
   - **用例测什么**：一个请求在执行中被抢占的场景。第二个请求在第一个请求的输出还没回来时就被调度（`max_concurrent_batches > 1` 时会这样）；再次调度第一个请求时，KV 缓存不够，第二个请求被抢占；随后第二个请求那一步的输出回来，要求它照样被记到这个请求上。
   - **01 改了什么**：给这个上游用例加了三行断言。位置：01 补丁文件第 1440–1448 行；v0.32.0 加 01 后的测试文件第 1589–1597 行；本补丁测试文件第 1336–1344 行。新增的断言要求日志里正好有一条带 `"transition":"preempted"` 的 `[PD_DIAG]` 记录（`assert len(preemptions) == 1`），而且这条记录的 `execution_id` 是引发抢占的那一步的步骤号。
   - **为什么失败**：01 的代码里没有任何地方写这种记录。`Request.pd_queue_transition` 只处理 `queued`、`candidate`、`finished` 三种，调度器的抢占路径也不调用它。所以 `preemptions` 是空的，`assert 0 == 1` 失败在 01 新加的这一行上；这一行之前的上游原有断言都通过（`running` 里只剩第一个请求，第二个请求为 `PREEMPTED`）。
   - **运行时行为没有变**：用各版本原版、没有改过的 `test_scheduler.py`，分别在干净安装和补丁后的安装上运行，v0.30.0 的 167 个用例、v0.32.0 的 208 个用例逐个结果相同，这个用例在两边都通过（`verify/results/upstream_scheduler_tests.txt`）。
   - **处理**：统筹定，不加 `"preempted"` 记录（01 没有这种记录，加了就是新字段，超出 47 段的范围），保留 01 的断言。
2. **01 新增的另外 13 个失败用例**（第 9.2 节第 1 条）：
   - `test_output_batch_boundaries` 8 个、`test_request_summary_requires_generated_token_for_first_token` 2 个、`test_resumable_stream_close_emits_one_terminal` 1 个：这三组断言的是单独写出的 `api_output_received`、`request_summary`、`frontend_request_terminal` 记录，01 并不单独写这些记录；
   - `test_execution_members_report_current_prefill_chunk` 1 个；
   - `test_independent_dense_replica_diagnostics_keep_global_rank` 1 个：调用了 01 里不存在的 `_pd_execution_span`。

   这 13 个在本补丁和 v0.32.0 加 01 上的报错逐个相同，处理方式与第 1 条相同。

### 12.2 与 vllm-ascend 调度器配合时的已知问题

3. **启用 ms_service_profiler 时，每一步可能多出一个 `batchFrameworkProcessing` span**
   - **现象**：vllm-ascend 的 `RecomputeScheduler`（t51-glm53-ascend `ef5bd17`）每一步都调用一次 vLLM 自己的 `Scheduler.schedule`，也就是 01 的包装，由它写出调度器记录。原版的 `RecomputeScheduler.schedule` 用的是自己的整份正文，不经过它。实测 E1–E7 共 39 次、E8 共 41 次，原版都是 0 次（`review/recompute-scheduler/recheck-ef5bd17/`）。
   - **触发条件**，三条同时满足：
     1. 实例的调度器是 `RecomputeScheduler` 或 `AsyncRecomputeScheduler`（现场是 D）；
     2. 这个实例装了并启用了 ms_service_profiler；
     3. profiler 解析 `vllm.v1.core.sched.scheduler:Scheduler.schedule`（vllm-ascend `d4ad49796` 的 `vllm_ascend/profiling_config.py:46`）时，balance 补丁还没有把这个名字换成 `BalanceScheduler`，钩子因此落在 vLLM 自己的类上。换名之后才解析的话，钩子落在 `BalanceScheduler.schedule` 上，不会多出 span。
   - **影响范围**：只影响 profiler 的数据，这个实例每一步多一个 `batchFrameworkProcessing` span。47 段、`[PD_DIAG]` 记录和调度决定都不受影响。ms_service_metric 也不受影响：它挂的是 `RecomputeScheduler.schedule` 和 `BalanceScheduler.schedule`，这两个名字的调用次数和原版相同。
   - **处理**：统筹定，保持现状。不在 `RecomputeScheduler` 里复制一份 01 的包装，以免两份代码分叉、以后不一致。补丁后核对用的检测脚本会报出 D 上是否装了、是否启用了 ms_service_metric 和 ms_service_profiler。

## 13. 向后兼容（11-19 统筹定）

**要求。** 诊断代码不能让原有的调用方出错，包括现场按旧方式写的代码，例如整份复制原函数的本地补丁。原版 v0.30.0 经过的调用路径也要保留：现场补丁替换了某个方法时，原来经过它的调用移植后仍要经过它。

**做法。** 把 01 改过的所有函数、方法的签名，按语法树和原版 v0.30.0 逐个对比。凡是新增必填参数、改变参数顺序或返回值形状、改变内部数据形状、会让旧写法出错的，都改成兼容。再从调用图上比对原版和移植版，找出原来经过、移植后不再经过的调用。下表是改了的 5 处：

| 位置 | 01 原样 | 现在 | 原因 |
|---|---|---|---|
| `AsyncLLM._add_request` | `*, owner: diag.Batch \| None`，必须传 | `*, owner: diag.Batch \| None = None` | 复制来的旧 `add_request` 按旧方式调用 `_add_request(request, prompt, parent, index, queue)` 时，会报 `TypeError`，所有请求都失败（实测见 `review/field-patches/`）。不传 owner 时，诊断部分安静地退化：不写 `api_summary[engine_submission]`，也不写 `add_request_send` 链接，不报错，请求照常发出；add_request 段为空，不会补 0 |
| `SchedulerOutput.pd_execution_id` | 插在第 10 个字段（`free_encoder_mm_hashes` 之后） | 移到最后一个字段 | 按位置构造 `SchedulerOutput` 时，后面的字段会错位。v0.30.0 原有的 23 个字段现在顺序不变 |
| `WorkerWrapperBase.sample_tokens` | `(self, grammar_output)` | `(self, grammar_output, *args, **kwargs)`，原样转发 | 01 之前，这个方法经 `__getattr__` 委托，接受任何参数 |
| `EngineCore.batch_queue` 的元素 | 01 改成四元组，`shutdown` 和 `step_with_batch_queue` 的出错路径都按四元组解包 | 只对四元组调用 `finish`，三元组跳过 | 旧代码放进去的仍是三元组，按四元组解包会在退出时出错 |
| n>1 的子请求（`AsyncLLM.add_request` 的 fan-out） | 照 v0.32.0 上游的写法，每个子请求直接调用 `output_processor.add_request` 和 `_send_engine_request`，不经过 `_add_request` | 每个子请求改回调用 `self._add_request(child, prompt_text, parent_request, idx, queue, owner=submission, accounted=accounted)`；`_add_request` 增加仅关键字参数 `accounted=None`；兜底边界和 `_record_pd_failure` 只在 `parent_req is None` 时执行 | 原版 v0.30.0 的每个子请求都经过 `_add_request`。现场 P 上的 `patch_prefill_length_validation.py` 替换的正是 `_add_request`，不改的话 n>1 的子请求会绕过现场的长度校验。改后 n>1 的记录与改前逐条相同 |

**核对过、本来就兼容的：**

- 新参数都加在末尾，并且有默认值：`Request.__init__` 的 `pd_identity`、`RequestState.__init__` 的 `pd_identity`。
- msgspec 结构的新字段都加在末尾，并且有默认值：`EngineCoreRequest` 的 `pd_identity`、`pd_tokenization`，`EngineCoreOutput` 的 `pd_execution_id`、`output_batch_id`，`EngineCoreOutputs` 的 `output_batch_id`；`RequestStateStats.pd_batch` 也在末尾。
- 加了装饰器的方法，包装层接受 `*args, **kwargs`，返回原方法的结果：`KVCacheManager` 的 6 个方法，V2 runner 的 `execute_model`、`sample_tokens`。
- `Scheduler.schedule()` 返回的是同一个对象。
- `InputProcessor.process_inputs_async` 的调用方式不变。
- `AsyncMPClient._send_input` 返回同一个 future。
- `_tokenize_prompt` 和 `process_for_engine` 返回的字典多一个 `pd_tokenization` 键，类型定义里声明为 `NotRequired`，使用方都按 `.get` 读取。vLLM v0.30.0 和 vllm-ascend `d4ad49796` 里都没有把这些字典用 `**` 展开传参的地方。

**工具复核。** 用 t51-glm53-ascend 的 `tools/check_signatures.py`（`b3d1ce1`）按语法树比较 v0.30.0 和打完补丁的树，范围是 `SHA256SUMS.before` 里的 23 个文件，结果 0 处不兼容（`verify/results/signatures/check_signatures_report.txt`）。工具标出要人工看的有 9 处，逐个读过：

- `KVCacheManager` 的 6 个方法加了 `_pd_range`，V2 runner 的 `execute_model`、`sample_tokens` 加了 `pd_execution`、`pd_sample`。这些包装都用 `functools.wraps`，接受 `*args, **kwargs` 原样转发，返回原方法的结果，异常原样抛出。
- `InputProcessor.__init__` 被标为返回值形状变化，是误报：那个 `return` 在 `__init__` 里新定义的 `process_inputs_async` 闭包中，`__init__` 本身没有返回值。这个闭包按原来的参数调用 `process_inputs`。和原来有一处细微差别：原来在构造时绑定 `self.process_inputs`，现在在调用时才查找，所以构造之后再替换 `process_inputs`，现在也会生效。
- 另外，工具把 `WorkerWrapperBase.sample_tokens` 列为新方法：v0.30.0 没有这个方法，调用经 `__getattr__` 交给 worker。现在的方法原样转发全部参数，调用效果与原来相同。

msgspec 结构不在这个工具的检查范围内，另用 `verify/check_structs.py` 比较，0 处不兼容（`check_structs_report.txt`）。最终补丁上两个工具重跑的结果相同，只多出 `_add_request` 的 `accounted`（有默认值）。

**调用路径复核。** 签名检查查不出「原来经过某个方法、移植后不再经过」这类变化，所以另用 `verify/callgraph_diff.py` 在调用图上比对，结果在 `verify/results/callgraph.txt`：

- 方法：对 v0.30.0 里的每个函数，统计它对每个被调对象的调用点个数。移植后的计数包括函数自己，以及补丁新加、由它调用的辅助函数（例如 `_send_engine_request`、`_pd_schedule`、`_enqueue_output`、`_start_load_kv`），因为 01 把原来的代码挪进了这些辅助函数。计数少于原版的列出来逐个读。
- 结果：`ea04aa1` 上只有 1 处，就是上表最后一行的 fan-out（`AsyncLLM.add_request` 调 `self._add_request`，原版 2 处，移植后 1 处）。最终补丁上 0 处。
- 补充核对：补丁删去的 v0.30.0 行里，含调用的有 124 行，其中 25 行没有原样出现在新加的行里，逐行读过：2 行是 `except` 子句（只是多了 `as` 名字），不是调用；其余 23 行都还在调用：有的只是改了格式或改成赋值后再返回，有的加了关键字参数，有的挪进了新加的辅助函数，有的外面多包了一层 `diag.scope` 或 `diag.span`。
- 计数只看调用点个数，看不出一个调用是否被放进了新的条件分支里；上面的逐行核对补上了这一点。

同一个工具还列出移植后的调用里，向原版就有的函数多传了原版定义不接受的关键字参数的地方。去掉在 v0.30.0 自己身上也会报出的 3 处（工具把 `super().__init__` 解析成本类造成的），只剩 `_add_request` 的 4 个调用点：n=1 的路径、fan-out、流式输入的两处，多传了 `owner`，fan-out 还多传了 `accounted`。所以现场若替换 `_add_request`，替换函数必须接受多出来的关键字参数。现场 `patch_prefill_length_validation.py` 的签名是 `(self, request, *args, **kwargs)`，满足这个条件；它是否把关键字参数原样传给原函数，由操作说明 A 打补丁前核对里的那一行 grep 确认。

**改了哪些文件。** 运行时的 4 个文件（第 5 处只改 `async_llm.py`）：`vllm/v1/engine/async_llm.py`、`vllm/v1/core/sched/output.py`、`vllm/v1/worker/worker_base.py`、`vllm/v1/engine/core.py`。另外新增测试 `tests/v1/metrics/test_pd_compat.py`（在 lab 补丁里）。`SHA256SUMS.before`、`NEW_FILES.txt` 不变。

**测试**（`docker run --rm`）：

- `test_pd_compat.py` 6 项全部通过：
  - `owner` 有默认值；
  - 不传 `owner` 时请求照常发出，不写记录；
  - `SchedulerOutput` 前 23 个字段的顺序与 v0.30.0 相同；
  - `sample_tokens` 转发多余的参数；
  - `shutdown` 能处理三元组；
  - n=2 的请求，两个子请求都经过（被替换的）`_add_request`，`owner` 和 `accounted` 都传到，两条 `add_request_send` 链接都写出。
- 现场包装被调用的次数（`review/field-patches/fanout_count.py`，按现场的写法替换 `_add_request`）：原版 v0.30.0 上 n=1、2、4 分别是 1、2、4 次；`ea04aa1` 上是 1、0、0 次；最终补丁上是 1、2、4 次（`review/field-patches/results/fanout_counts.txt`）。
- 现场补丁模拟（`review/field-patches/`），8 个 HTTP 场景：
  - P 的 `add_request` 为整份复制时：7 个返回 200，F7 是有意构造的超长请求，在所有模式（包括不替换）下都返回 400。只有 add_request 段为空（7 条 `engine_submission` 不写）；另外，原来挂在 submission 记录上的 `request_id`、`engine_request` 链接改挂到 `api_summary[http]` 上。其余记录的字段都相同。
  - `completion_stream_generator` 为整份复制时：只有 F2（`/v1/completions` 流式）缺 `first_generated`。
  - 两者同时复制：两种情况合在一起。
  - 包一层（`add_request`、`completion_stream_generator` 各一种）以及不替换时：都是 102 条记录，与改动前逐条相同。
- 01 的测试：363 个用例（01 新增的 136 个、3 个被 01 修改的测试文件里原有的 216 个、A2 的 11 个）与 `65c8e87` 逐个结果相同。最终补丁与 `ea04aa1` 在同一个容器里重跑，这 363 个也逐个结果相同（`verify/results/unit_tests_final_vs_ea04aa1.txt`）。
- 记录等价：最终补丁与 `ea04aa1` 相比，15 个场景 0 处差别，包括 n=2 的 F3；与 v0.32.0 加 01 相比，两边各 223 条，仍是第 9.3 节那 3 条不执行 forward 的记录。
- `check_steps` 0 处失败；pyflakes 0 条警告；Python 3.11 下语法 0 处错误；打补丁和校验表的核对全部通过。

**现场的两个本地补丁（f-3b，11-19）。** 两处替换都是包装，01 的埋点保留，不需要反向重放。依据和模拟结果见 `review/field-patches/README.zh-CN.md`。

- P 上的 `patch_prefill_length_validation.py` 替换的是 `AsyncLLM._add_request`：先做长度校验，再调用移植版自己的 `_add_request`。`_add_request` 里的兜底边界、`_send_engine_request`、发送失败时的 `_record_pd_failure` 都照常执行。add_request 段的起止不变：起点是分词结束，终点是 ZMQ 发送结束。校验在这两点之间运行，所以耗时算在这一段里，这符合这一段的定义；耗时多少没有测。校验不通过时，`engine_submission` 记录照常写出，状态为 `rejected`（模拟里用的是 `VLLMValidationError`），没有 add_request 段。这与 01 在 `check_admission` 处拒绝请求时一样。
- P、D 上的 `patch_kv_load_failure_reason.py` 替换的是 `OpenAIServingCompletion.completion_stream_generator`。

`completion_stream_generator` 的口径：

- 现场函数是包装。它先用 `record_load_failures` 包住 `result_generator`：逐个原样交出输出，只记下以 KV 加载失败标记开头的 `stop_reason`。然后调用原函数，再改写原函数产出的输出块。
- 01 在这个函数里的埋点只有三处：`first_generated`（`pd_api.generated`）和两处 `pd_api.generation_error`。这三处都在原函数里，由原函数写出，看到的输出与不包装时相同。
- 包装对输出块的改写（`finish_reason`、`stop_reason`）只改变发给客户端的内容，不改变任何记录。`api_summary[http]` 的状态仍按原函数的结果写，KV 加载失败要从传输记录里看。
- 包装在输出到达原函数之前多了一层逐项转发，没有等待；这层转发的耗时没有测。

## 14. 启动日志 `PD_DIAG_STARTUP`（11-19 统筹定）

**目的。** 现场的调度器和 runner 都是子类，子类会覆盖方法，01 的埋点能不能执行到，取决于运行时实际构造出来的是哪个类。下面三行普通日志在运行时报出这些类，操作说明 A 第 8 步用它们判断。这三行不是 `[PD_DIAG]` 记录，不新增记录类型，也不改 `[PD_DIAG]`。

**格式。** INFO 级，logger 分别是 `vllm.v1.engine.core` 和 `vllm.v1.worker.worker_base`。消息以固定前缀 `PD_DIAG_STARTUP ` 开头；这个前缀不含 `[PD_DIAG] `，也不含 `[BatchPutTiming]`，用它 grep 不会和别的日志互相命中。

```text
PD_DIAG_STARTUP scheduler=<模块>.<类名> balance=<True|False|?> recompute=<…> dyntra_lb=<…> batch_job=<…> profiling_chunk=<…>
PD_DIAG_STARTUP worker model_runner=<模块>.<类名> tp_rank=<n|?>
PD_DIAG_STARTUP worker device_timing=<on|off|?> tp_rank=<n|?>
```

| 项 | 取值 |
|---|---|
| `scheduler` | EngineCore 建好调度器后，`type(self.scheduler)` 的 `__module__` 加 `__qualname__` |
| 五个开关 | vllm-ascend `get_ascend_config().scheduler_config` 的 `enable_balance_scheduling`、`recompute_scheduler_enable`、`dyntra_lb_config.enabled`、`batch_job_sched_config.enabled`、`profiling_chunk_config.enabled`（`d4ad49796` `ascend_config.py:1603–1618`）。只在 `vllm_ascend.ascend_config` 已经被加载时才读，不新导入任何模块；读不到或者不是布尔值时写 `?` |
| `model_runner` | Worker 的 `model_runner` 的 `__module__` 加 `__qualname__` |
| `tp_rank` | `get_tp_group().rank_in_group`。第一行在 `init_device` 之后直接取，取不到写 `?`；第二行取这一步 `pd_execution_context` 里的值 |
| `device_timing` | vllm-ascend 挂在 runner 上的 `_pd_device_timing.enabled`（t51-glm53-ascend 的 `pd_device_timing.py`），用 `getattr` 取。`True` 写 `on`，`False` 写 `off`，取不到写 `?` |

`d4ad49796` 里有两个都叫 `NPUModelRunner` 的类，分别在 `worker/model_runner_v1.py` 和 `worker/v2/model_runner.py`，`_310p/` 下面还有子类，所以类名一律带完整的模块路径。

**什么时候打。**
- **EngineCore 那一行：** 在 `EngineCore.__init__` 里，调度器建好之后立即打。
- **Worker 的 `model_runner` 那一行：** 在 `WorkerWrapperBase.init_device` 里，`worker.init_device()` 返回之后打。runner 就是在这里建好的，所以重启后马上就能 grep 到，不用等请求。
- **Worker 的 `device_timing` 那一行：** 在 `WorkerWrapperBase.execute_model` 第一次正常返回之后打，所以要先进来一个真实请求。
  - vllm-ascend 的 DeviceTiming 要到第一次执行时才创建（`pd_device_timing.py` 的 `_timing`），在这之前读只能得到 `?`。
  - `WorkerWrapperBase.execute_model` 只在 EngineCore 调度出的步骤里调用（执行器的 `execute_model` RPC）。dummy run（`execute_dummy_batch`）和图捕获都直接走 runner，不经过这里，所以不算。
  - vllm-ascend 的 V1 runner 在每次 `execute_model` 的开头就决定 DeviceTiming（`model_runner_v1.py` 的 `begin_execute`，在不执行 forward 的提前返回之前），所以第一步之后就能打出 `on` 或 `off`。
  - 对不带这个钩子的 runner，一步既没有调度 token、DeviceTiming 也还没决定时，这一步不算，要等第一个执行了 forward 的步骤，那时写 `?`。
  - 执行出错的那一步也不算。

三行都是每个进程只打一次（统筹定）。函数整体包在 `try/except Exception` 里，任何异常都吞掉，不影响启动和执行。

**允许的值**（统筹、顾问定，操作说明 A 第 8 步按这张表判断）：

| 行 | 允许 |
|---|---|
| `scheduler` | `vllm.v1.core.sched.scheduler.Scheduler`；`vllm_ascend.patch.platform.patch_balance_schedule.BalanceScheduler`，并且 `balance=False`；`vllm_ascend.core.recompute_scheduler.RecomputeScheduler` 一族，包括 `AsyncRecomputeScheduler` |
| `worker model_runner` | 只允许 `vllm_ascend.worker.model_runner_v1.NPUModelRunner` |

**验证**（`docker run --rm`，贴近现场的镜像，第 15.2 节）：

- 单元测试 5 项（`tests/v1/metrics/test_pd_compat.py`），全部通过：
  - 没有 vllm-ascend 时，五个开关都写 `?`；
  - 读到 vllm-ascend 的配置时照实写；配置未初始化、读取报错时写 `?`，不报错；
  - `model_runner` 那一行在 `init_device` 之后打一次；
  - `device_timing` 那一行：执行出错的一步、不执行 forward 且未决定的一步都不算，之后正常返回的一步打一次；
  - TP 组未初始化时 `tp_rank` 写 `?`；读 Worker 对象的属性时报错，异常被吞掉；
  - 每个进程只打一次。
- 合成调用（`verify/startup_check.py`，结果在 `verify/results/startup/`）。每个场景开一个进程，用真实的 `EngineCore.__init__`（执行器换成替身）和真实的 `WorkerWrapperBase.init_device`、`execute_model`（单 rank 的 gloo TP 组，runner 在每次 `execute_model` 开头调用 vllm-ascend 真实的 `pd_device_timing.begin_execute`，和 `model_runner_v1.py` 一样）。vllm-ascend 的调度器按现场的模块名加载，配置用 vllm-ascend 真实的 `AscendConfig`，由调度器自己的 `__init__` 初始化，和现场相同。EngineCore 构造了两次，每个场景都只打出一行。Worker 先调 `init_device`，再执行 3 步，第一步不执行 forward；每个场景打出两行，`model_runner` 那一行在 `init_device` 之后、执行之前就已打出。

| 场景 | 打出的那一行（去掉前缀） |
|---|---|
| vLLM 自己的 `Scheduler`，没有 vllm-ascend | `scheduler=vllm.v1.core.sched.scheduler.Scheduler balance=? recompute=? dyntra_lb=? batch_job=? profiling_chunk=?` |
| P：balance 补丁换名后的 `BalanceScheduler` | `scheduler=vllm_ascend.patch.platform.patch_balance_schedule.BalanceScheduler balance=False recompute=False dyntra_lb=False batch_job=False profiling_chunk=False` |
| 同上，配置里打开 balance（应被操作说明拒绝） | `… BalanceScheduler balance=True recompute=False …` |
| D 同步：移植后的 `RecomputeScheduler` | `scheduler=vllm_ascend.core.recompute_scheduler.RecomputeScheduler balance=False recompute=True dyntra_lb=False batch_job=False profiling_chunk=False` |
| D：`AsyncRecomputeScheduler` | `scheduler=vllm_ascend.core.recompute_scheduler.AsyncRecomputeScheduler balance=False recompute=True …` |
| Worker，V1 runner，没有 NPU | `worker model_runner=vllm_ascend.worker.model_runner_v1.NPUModelRunner tp_rank=0`，第一步之后 `worker device_timing=off tp_rank=0` |
| 同上，用替身 NPU 事件 | `worker model_runner=…model_runner_v1.NPUModelRunner tp_rank=0`，第一步之后 `worker device_timing=on tp_rank=0` |
| Worker，V2 runner（应被操作说明拒绝） | `worker model_runner=vllm_ascend.worker.v2.model_runner.NPUModelRunner tp_rank=0`，第一步之后 `worker device_timing=off tp_rank=0` |
| Worker，runner 不带 vllm-ascend 的计时钩子 | `worker model_runner=… tp_rank=0`；第一步不执行 forward，不算；第二步之后 `worker device_timing=? tp_rank=0` |

## 15. 和 glm5.3 现场的对应

现场事实的来源：
- t51-glm53-kit `field/` 下第 2 次检测（P、D 各一份），以及现场小核对 f-1、f-3b；
- 调度器配置来自 t51-glm53-ascend 的 `docs/SCHEDULER_BYPASS.zh-CN.md`，那里记的是用户核对过的进程命令行。

### 15.1 逐项

| 项 | 现场 | 本仓库怎么验证的 | 没有验证的和原因 |
|---|---|---|---|
| vLLM | v0.30.0 原版。P、D 都是 `vllm=0.30.0+empty state=v0.30.0 changed=0`，检测的 27 个文件 27/27 与 v0.30.0 相同 | 补丁在干净的 v0.30.0（`ced6857a`）上 `git apply --check` 通过。`SHA256SUMS.before` 的 23 个文件与干净的 v0.30.0 一致。测试镜像里装好后，版本串同为 `0.30.0+empty` | — |
| Python | 3.11.10 | 测试镜像用 `python:3.11.10-slim`，全部测试重跑；25 个运行时文件和 A2 测试文件在 3.11.10 下语法解析 0 处错误 | — |
| transformers | 5.14.1 | 测试镜像装的就是 5.14.1，tokenizers 按它的依赖解析为 0.22.2，全部测试重跑 | — |
| torch | 2.10.0a0 加 torch_npu 2.10.0.post4（NPU 版） | 测试镜像用 torch 2.10.0+cpu | NPU 版只能在昇腾上装。补丁本身不调用 torch 的设备接口 |
| 平台 | aarch64，glibc 2.38（openEuler 24.03），CANN 9.2.0 | 测试镜像是 aarch64（Debian 12） | 补丁是纯 Python，不含编译产物。没有 CANN |
| vllm-ascend | `d4ad49796` 加本地改动：改了 3 个文件，多出 54、55 个文件 | 对 vllm-ascend 暴露的接口见第 5 节。现场两个本地补丁都是包装，见第 13 节 | vllm-ascend 一侧的移植和核对由 t51-glm53-ascend 负责 |
| runner | V1（`runner=V1(env)`）：vllm-ascend `worker/model_runner_v1.py` 的 `NPUModelRunner`，继承 vLLM 的 V1 `GPUModelRunner` | 经过本补丁的部分有两处：每个 Worker 的 `WorkerWrapperBase`，以及 V1 runner 的 connector 路径 `KVConnectorModelRunnerMixin`。`worker_summary` 由 vllm-ascend 在 V1 runner 上加的装饰器写出 | 01 对 V2 runner 的改动（`vllm/v1/worker/gpu/model_runner.py`、`gpu/kv_connector.py`）在 v0.30.0 上成立，合成调用跑过，但现场不执行。统筹定保留，与 01 一致；现场用 V1 runner，不执行这部分。runner 的类由第 14 节的 Worker 启动日志在运行时确认 |
| P 的调度器 | `BalanceScheduler`：没有调度配置，balance 关着，`schedule()` 调 `super()`，走 01 的包装 | 用 vllm-ascend 真实的 `BalanceScheduler` 跑 E1–E8。调度决定、输出和记录都与 vLLM 的 `Scheduler` 相同，`check_steps` 0 处失败。第 14 节的启动日志场景 | — |
| D 的调度器 | `AsyncRecomputeScheduler`：开了 `recompute_scheduler_enable` 和 `--async-scheduling` | 用 t51-glm53-ascend `ef5bd17` 的移植，两种基类各跑 E1–E8。调度决定与原版相同，`BalanceScheduler.schedule` 0 次，两套测试环境各跑一遍。第 14 节的启动日志场景 | NPU 上没有运行 |
| async scheduling | P 关（`async=0`），D 开（`async=1`） | E5、E6：`max_concurrent_batches=2`，走 `step_with_batch_queue`。`check_steps` 核对两步重叠 | — |
| MTP | P `deepseek_mtp` k=1，D k=5 | E2、E6 用 k=5；第 10 节读码 | k=1 没有单独跑，走的是同一条路径。现场的 draft 模型（vllm-ascend 的 proposer）不能在 CPU 上跑 |
| 图模式 | P eager，D 图模式（`eager=0`） | 本补丁的代码都在 host 侧，和是否用图模式无关 | 图模式下的设备计时是 vllm-ascend 的 C14，不在本仓库 |
| TP/DP | P：tp=16、dp=8，每个实例 16 个 Worker。D：tp=1、dp=64，每台机器 16 个实例。两边 pp=1 | 测试用 tp=1、dp=1。rank 的来源 `get_tp_group().rank_in_group`、`parallel_config.data_parallel_index` 在 v0.30.0 里存在（第 15.3 节） | 多 rank 没有跑：CPU 容器里没有多卡 |
| 执行器 | `MultiprocExecutor`（第 8 节读码） | 合成调用用进程内的替身执行器 | 多进程 ZMQ 没有跑 |
| 启动方式 | `launch_online_dp.py`：每个 DP rank 一个 `vllm serve` 进程，各有自己的 API server | F1–F8 每次起一个 API server | 多实例没有跑。各实例相互独立，记录按进程写 |
| KV connector | `MultiConnector` = `MooncakeConnectorV1` + `AscendStoreConnector`。P `load_async=0`（同步加载），D `load_async=1` | vLLM 一侧：E7 核对同步加载时 submit 的范围。两个 connector 本身在 vllm-ascend | Mooncake、AscendStore 不能在 CPU 上跑 |
| 现场本地补丁 | P：`patch_prefill_length_validation.py` 替换 `AsyncLLM._add_request`，是包装。P、D：`patch_kv_load_failure_reason.py` 替换 `completion_stream_generator`，也是包装 | 第 13 节；F1–F8 的模拟。n>1 子请求的调用路径已改回 v0.30.0，按现场写法替换 `_add_request` 后，n=1、2、4 时被调用的次数与原版相同（1、2、4） | 两处的调用行在 f-3b 的 diff 里被截断了，由操作说明 A 打补丁前核对的那一行 grep 确认 |
| 日志 | `[PD_DIAG]` 以 INFO 级写进 vLLM 的日志文件（`stderr=file`）；`PD_DIAG_STARTUP` 写在同一处 | `[PD_DIAG]` 的行格式与 01 相同 | 日志量见 t51-glm53-kit |

### 15.2 两套测试环境

| | 原来的镜像：Python 3.12.3、transformers 5.19.0、torch 2.13.0+cpu | 贴近现场的镜像：Python 3.11.10、transformers 5.14.1、torch 2.10.0+cpu |
|---|---|---|
| 兼容测试 `test_pd_compat.py` | 11/11 | 11/11 |
| 01 的 363 个用例 | 与 `db27fd1` 逐个结果相同 | 与 `db27fd1` 在同一个镜像里逐个结果相同，只有 `test_logprobs_processor` 的两种参数组合不同，属于间歇失败，见下 |
| 原版 `test_scheduler.py`（167 个） | 补丁前后逐个相同（第 9 节） | 干净的 v0.30.0 与最终补丁逐个相同：128 个通过，39 个失败。失败的都是环境问题，干净的 v0.30.0 也一样 |
| 记录等价（15 个场景） | 与 `db27fd1` 相比 0 处差别；与 v0.32.0 加 01 相比，仍是第 9.3 节那 3 条 | 与 `db27fd1` 相比 0 处差别；与原来镜像里的最终补丁相比 0 处差别；与 v0.32.0 加 01（在原来镜像里生成）相比，仍是那 3 条 |
| `check_steps` | 0 处失败 | 0 处失败 |
| F1–F8 现场补丁模拟（13 种模式） | 与 `db27fd1` 相同 | 与 `db27fd1` 相同，与原来的镜像也相同 |
| 按现场写法替换 `_add_request` 后被调用的次数，n=1、2、4 | 1、2、4 | 1、2、4（干净的 v0.30.0 也是 1、2、4） |
| 调度器：P 的 `BalanceScheduler`，D 的 Recompute 两种基类 | 见第 15.1 节 | 结论相同 |

- **本轮与 `c9ca251` 相比**（Worker 那一行拆成两行、去掉部署脚本；`verify/results/site_env/final_vs_c9ca251_both_envs.txt`）：两套镜像里单元测试除改写的启动日志用例外逐个相同，贴近现场的镜像里那个间歇失败的用例这次通过；记录 15 个场景 0 处差别；13 种现场模拟、调度器各变体、调用次数也都相同。
- **现场补丁只留 `vllm/` 之后**（`verify/results/site_env/split_vs_e29f1f9_both_envs.txt`）：用现场补丁加 lab 补丁在干净的 v0.30.0 上重建测试用的树，两套镜像全套重跑。结果与 `e29f1f9` 逐项相同：374 个用例 0 处不同，记录 15 个场景 0 处差别，13 种现场模拟、调度器各变体和调用次数也相同。
- **镜像怎么搭的**（`verify/Dockerfile.site-py311`）：
  - 包从清华 PyPI 镜像装，Debian 包从清华 Debian 镜像装；
  - torch 从 PyTorch 的 CPU 源装，因为 PyPI 上 aarch64 的 torch 会带上 CUDA 库；
  - 用约束文件钉住 torch，防止被依赖带着升级；
  - venv 自带的 setuptools 65.5.0 读不了 v0.30.0 `pyproject.toml` 的 license 字段，按 v0.30.0 `[build-system]` 的要求另装 `setuptools>=77.0.3,<81`。
- **v0.32.0 加 01 只在原来的镜像里跑。** 它要求 transformers ≥ 5.16.1，装不进贴近现场的镜像，所以它的记录只在原来的镜像里生成。跨镜像比较的结果与同一镜像里比较的结果相同。
- **间歇失败。** `tests/v1/engine/test_output_processor.py::test_logprobs_processor` 的 `[None-5-*]` 两种参数组合，在贴近现场的镜像里两次整套运行中各失败一个：`db27fd1` 那次是 `FINAL_ONLY`，最终补丁那次是 `DELTA`，都是累计 logprob 对不上。判断是测试本身的随机性，依据如下（`verify/results/site_env/logprobs_flaky.txt`）：
  - 测试向量由未设种子的 `torch.rand` 生成；
  - logprob 在 `vllm/v1/engine/logprobs.py` 里计算，补丁没有改这个文件；
  - 单独跑这个测试，两棵树、两套镜像各跑 12 次，8 种组合全部通过；
  - 整个测试文件在贴近现场的镜像里两棵树各跑 6 次，这个测试全部通过。

### 15.3 从 v0.32 带过来的东西：审查结果

- **顾问的专审（统筹转达）。** 导入和 API 层都干净。
- **本仓库的审查。** 最终补丁的结果见 `verify/results/callgraph.txt`、`callgraph_new_calls_all.txt`、`api_audit.txt`。脚本 `verify/callgraph_diff.py`、`verify/api_audit.py` 都只按语法树分析，不导入代码。

1. **原来调用、移植后不再调用的。** 对 v0.30.0 的每个函数，统计它对每个被调对象的调用点个数。移植后的计数包括函数自己，以及补丁新加、由它调用的辅助函数。
   - `ea04aa1` 上有 1 处：n>1 的 fan-out，已经改回 v0.30.0 的写法。最终补丁上 0 处。
   - 计数看不出调用是否被挪进了新的条件分支里，所以另外逐行读了补丁删去的、含调用的 124 行。其中 26 行没有原样出现在新加的行里：2 行是 `except` 子句，不是调用；其余 24 行都还在调用，只是改了格式、改成赋值后再返回、加了关键字参数、挪进了补丁新加的辅助函数，或者外面多包了一层 `diag.scope`、`diag.span`。
2. **原来不调用、移植后新调用的。** 共 354 个调用点：diagnostics 159 个，标准库 116 个，补丁自己的函数 29 个，其余 50 个逐个读过。这 48 个是：
   - 对字典的读取和写入（提示词字典、`owner.fields`、`output_processor` 的两张表等）；
   - 装饰器包装层的内部调用；
   - `_send_engine_request` 两个分支里各一处 `add_request_async`，每个请求仍只发送一次；
   - `process_inputs_async` 的闭包；
   - `threading`、`time` 的计时函数；
   - `Request.is_finished()`，只读状态；
   - 启动日志里的 `get_ascend_config`、`get_tp_group`、`logger.info`；
   - `pd_execution_context` 里的 `get_tp_group()`、`get_pp_group()`。

   没有一个改变行为。

   **更正：** 之前说 `get_tp_group()`、`get_pp_group()` 只在 V2 runner 里调用，这是错的。`pd_execution_context` 在 `WorkerWrapperBase.execute_model` 里，现场每个 Worker 每一步都会执行。在现场这是安全的：vllm-ascend 的 `NPUWorker` 继承 vLLM 的 `WorkerBase`，`parallel_config` 在 `WorkerBase.__init__` 里设好；TP、PP 组在 `init_device` 里建好（`worker.py:1272`），早于任何一步执行。
3. **01 新加的代码用到的内部 API。** 这里只看真正新写的行，不看只是挪动或改了缩进的 v0.30.0 原有行。
   - **导入：** 25 个 vLLM 导入全部能解析。
   - **属性名：** 114 个在 v0.30.0 里有定义，74 个由补丁定义，15 个属于标准库或第三方。另有 2 个是 vllm-ascend 的名字，`get_ascend_config` 和 `_pd_device_timing`，都是启动日志用的。它们在 `d4ad49796` 和 t51-glm53-ascend 的补丁里存在，只在 vllm-ascend 已经加载以后才读，读的时候包在 `getattr` 或 `try` 里。
   - **`getattr`、`hasattr` 的字面量：** 8 个由补丁定义，3 个在 v0.30.0 里有定义，1 个是上面的 `_pd_device_timing`。没有哪个会因为名字不存在而悄悄取到默认值。
   - **同名类在 v0.32.0 有、v0.30.0 没有的属性：** 4 个候选，逐个读过。报出它们的是 v0.32.0 里另外几个同名的类，例如 `Engram`、`RealtimeConnection`。代码实际读的对象是 `PdFinishClaim`、`atexit`、`Request`、`Scheduler`，在 v0.30.0 里都有这个属性。
   - **定义在两个版本之间不同的函数：** 15 个。它们要么只是同名，比如对列表调用的 `.append`；要么是 v0.30.0 自己原有的调用，参数也和 v0.30.0 相同，例如 `execute_model`、`sample_tokens`、`schedule`、`process_inputs`、`start_load_kv`。
   - **字段的类型标注：** 新代码读到的字段，两个版本里 0 处不同。
   - 在 Python 3.11.10 的标准库下重跑，结果相同。
4. **签名工具和 msgspec 结构：** 最终补丁 0 处不兼容。

