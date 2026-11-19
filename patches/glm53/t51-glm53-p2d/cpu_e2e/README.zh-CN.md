# CPU 端到端验证：vLLM v0.30.0 加 T51 vLLM 补丁

本节在 CPU 上起真实的 vLLM v0.30.0 服务：HTTP、API Server、EngineCore、Worker 四层，每个场景都在打补丁前后各跑一遍。

一共两遍：

| 遍 | Python | 补丁（t51-glm53-vllm 的 `patches/vllm/t51-vllm-v0.30.0-pd-diag.patch`） | 结果目录 |
|---|---|---|---|
| 第一遍 | 3.12.3 | `db27fd1`，sha256 `f227ecae…fefdbc`；另用 `c9ca251`（sha256 `db1e11d5…ae91`）复跑了 site 场景 | `results/py312/` |
| 第二遍 | 3.11.10 | `40152b2`（最终的现场补丁），sha256 `7e2ec359…e4a`，`SHA256SUMS.after` 的 sha256 前 8 位 `5a708485`，25 个文件全在 `vllm/` 下 | `results/py311/` |

补丁文件一处未改，所有补丁都取自提交（`git show`），不取工作区。

## 0. 结论

1. **服务结果前后一致。** 两遍各跑 5 个场景，每个 11 到 14 个子场景，比较状态码、每个 choice 的文本、finish_reason 和 `[DONE]`，打补丁前后 0 处差异。客户端中途断开的流不比较文本，见 2.3 节。两遍之间相比，未打补丁的结果相同，打补丁的结果也相同（`results/cross_pass.txt`）。
2. **记录完整。** 未打补丁时 `[PD_DIAG]` 记录为 0 条。打补丁后：
   - API Server、EngineCore、Worker 三类进程的记录种类和条数，与请求数、调度步数对得上；
   - 公共字段齐全；
   - execution_id 链条完整：每个调度了 token 的步，都恰好对应一条同 id、同 `request_ids` 的 Worker 执行记录和一条采样记录。
3. **过滤器有值的段。** 记录交给面板二 v3.1-extra 过滤器（glm53 配置，config_id `pd-diag-v3.1-extra-glm53-2026-11-19c@1a453775`）后，`segments.json` 的 47 个段里两遍都有 22 段有值，正好是 vLLM 侧能在 CPU 上出值的段。其余各段没有值，每一段的原因都已查清，见 3.4 节。
4. **两个模拟的现场包装。** `_add_request` 带长度校验，n=2 时每个子请求各调用一次；`completion_stream_generator` 包结果流。两遍中，两者打补丁前后调用次数、参数和结果都相同。补丁给 `_add_request` 多传的 `owner=`、`accounted=` 原样传到原函数。
5. **启动行。** 第二遍的 `40152b2` 每个服务写 3 行 `PD_DIAG_STARTUP`：EngineCore 1 行，Worker 2 行。实际输出见 3.6 节。
6. **需要说明的两点。**
   - **Worker 记录：** 只装 vLLM 补丁时，CPU 上的 Worker 一条记录也不写，因为补丁只在 Model Runner V2 上加了测量点。现场由 vllm-ascend 的 V1 runner 加这些点：ascend 补丁 `08-worker-model_runner_v1.patch` 在 `model_runner_v1.py` 上各加一处 `@pd_execution`、`@pd_sample`。所以本次在测试框架里把相同的点套到 CPU runner 上，这是测试侧的模拟，NPU 设备计时没有模拟，见 2.2 节。只装 vLLM 补丁的那一遍保留在 `results/py312_only_vllm_patch/`。
   - **P/D：** ExampleConnector 在 CPU 上没有真正把 KV 写进 D，见 3.5 节。

## 1. 环境

| 项 | 第一遍 | 第二遍 | glm5.3 现场 |
|---|---|---|---|
| 底座 | `t51g-vllmtest:v1`：Ubuntu 24.04.5，glibc 2.39，aarch64，10 核；`docker run --rm --network none` | 同第一遍 | 昇腾 NPU 机器 |
| Python | 3.12.3，镜像自带（`/opt/venv`） | 3.11.10：python-build-standalone 20241016 的 `cpython-3.11.10+20241016-aarch64-unknown-linux-gnu-install_only.tar.gz`，sha256 与该 release 的 `SHA256SUMS` 一致，只读挂载在 `/opt/py311` | 3.11.10 |
| torch 一族 | torch 2.13.0+cpu、torchvision 0.28.0+cpu、torchaudio 2.11.1+cpu、torchcodec 0.17.0+cpu，镜像自带 | 同样的版本，cp311，取自 PyTorch CPU 索引 | torch 加 torch_npu，版本没有核对 |
| vLLM | v0.30.0 官方 CPU wheel `vllm-0.30.0+cpu-cp38-abi3-manylinux_2_39_aarch64.whl`（GitHub Release，sha256 `0423f802…6c47`），`pip --no-deps --target` 装到挂载目录 | 同一棵树：wheel 是 cp38-abi3，3.11 可以直接用 | v0.30.0，跑在 vllm-ascend `d4ad49796` 上 |
| transformers | 5.14.1，装在挂载目录，放在 PYTHONPATH 前面 | 5.14.1，装在环境里 | 5.14.1（第 1、2 次检测） |
| tokenizers、huggingface-hub | 0.22.2、1.33.0 | 0.22.2、1.33.0 | 现场版本未采集 |
| 其余依赖 | 镜像 venv 中的 160 个包（`results/py312/pip-freeze.txt`） | pip 按 vLLM wheel 在 cp311 下解析出的 146 个包（`results/py311/pip-freeze.txt`） | |

关于版本的取法：

- **tokenizers、huggingface-hub：** 现场版本未采集，按 transformers 5.14.1 的约束取范围内最新的正式版。
  - transformers 5.14.1 要求 tokenizers ≥0.22.0 且 ≤0.23.0，huggingface-hub ≥1.5.0 且 <2.0；vLLM v0.30.0 另要求 huggingface_hub ≥1.31.0。
  - PyPI 上没有 tokenizers 0.23.0 正式版（只有 0.23.0rc0），所以取 0.22.2。
  - 镜像自带的 tokenizers 0.23.3、huggingface_hub 2.4.0 不满足这个约束。
- **第二遍的其余依赖：** 和第一遍共有的 145 个包里，有 25 个版本不同，都是小版本或补丁版本，例如 openai 3.49.0 对 3.55.1。torch 一族、transformers 一组、numpy、fastapi、pydantic、uvicorn、safetensors 两遍相同。
- **第二遍的 torch 一族：** 一开始 pip 从 PyPI 取了 torchvision 0.28.0 和 torchaudio 2.11.1，这两个和 torch 2.13.0+cpu 不配套，服务起不来（`operator torchvision::nms does not exist`）。改为和第一遍镜像相同的 `+cpu` 版本后正常。

**模型：** `make_tiny_model.py` 本地生成，两遍共用：Llama 结构，2 层，hidden 256，4 个头，float32，字节级 BPE 词表 388，带 chat 模板，不下载。

**服务参数：** `--dtype float32 --max-model-len 512 --max-num-seqs 8 --enforce-eager`，按场景加 `--async-scheduling` 或 `--no-async-scheduling`。

**日志：** `VLLM_LOGGING_CONFIG_PATH=logging_raw_and_filter.json`。控制台保留原始 `[PD_DIAG]` 行，同时挂面板二的 `pd_diag_logfilter.PdDiagHandler`，`PD_DIAG_MAPPING` 指向 glm53 的 `segments.json`。

**几棵树：**

- `vllm030cpu`：wheel 原样。
- `vllm030cpu_p`（`db27fd1`，由 `setup_pass1.sh` 做出）、`vllm030cpu_c9ca251`、`vllm030cpu_40152b2`（由 `make_patched_tree.sh` 做出）：都先硬链接复制，再 `git apply`，然后与该提交的 `SHA256SUMS.after` 核对，25 个文件都一致。
- 打完补丁后，原树里提到 `pd_diagnostics` 的文件数为 0。

**搭环境的脚本：** 第一遍 `setup_pass1.sh`，第二遍 `setup_pass2.sh`，每一步之前都检查剩余空间，低于 30.5 GB 就停。

**第二遍的下载：** 本机下载 PyPI 和 PyTorch 索引的文件时，偶尔出现内容损坏（哈希不符）和断连。所以 `setup_pass2.sh` 先用 `pip download` 把文件下载到 wheelhouse：pip 校验哈希，不符的文件不保留；下载失败就重试，PyPI 和清华镜像轮流用。之后再离线安装。

## 2. 做法

### 2.1 场景

| 场景 | 服务 | 说明 |
|---|---|---|
| `sync` | 单实例，`--no-async-scheduling` | |
| `async` | 单实例，`--async-scheduling` | |
| `site` | 单实例，开异步，加两个模拟的现场包装 | 长度校验不通过时抛 `ValueError` |
| `sitev` | 同 `site` | 长度校验不通过时抛 `vllm.exceptions.VLLMValidationError` |
| `pd` | P、D 各一个实例，加代理 | ExampleConnector（P 为 kv_producer，D 为 kv_consumer，共用目录）。P `--no-async-scheduling`，D `--async-scheduling`，和现场运行中的配置一致。代理先把请求以 `max_tokens=1`、`n=1` 发给 P，再把原请求发给 D |

`client.py` 依次发出以下请求，只用标准库：

- completions 和 chat × 非流式和流式 × n=1 和 n=2，共 8 个。
  - n=1 用 temperature 0。
  - n=2 用 temperature 0.8、seed 1234：vLLM 不接受贪心采样下 n>1，第一次试跑时 n=2 都返回了 400，记录保留在 `results/py312_only_vllm_patch/`。
- 同时发 4 个 completions。
- 1 个流式 completions（`max_tokens=400`、`ignore_eos`）：收到 2 个 chunk 后客户端关闭连接，服务端随即中止该请求。之后再发 1 个普通请求。
- 只在 `site`、`sitev` 中：prompt 长 201 个 token 的 n=1 和 n=2 请求各 1 个（长度上限 64，正常 prompt 是 5 和 16 个 token），之后再发 1 个普通请求。

每个场景结束后等 8 秒，让过滤器写出逐请求的行（flush 周期 5 秒），再发 SIGINT 停服务。

### 2.2 测试框架里的两类模拟（`site_patches/sitecustomize.py`）

**现场的两个包装（`P2D_SITE_PATCHES=1`）。** 签名按现场小核对 f-3b 的结果写：

- **`AsyncLLM._add_request`：**
  - 签名是现场的 `(self, request, *args, **kwargs)`，结果里记为 `wraps_orig=1`；
  - 先按 prompt 长度校验，再用原参数调用原函数；
  - 每次调用记一行 `p2d_site_patch add_request`，含 parent、index、收到的关键字参数。
- **`OpenAIServingCompletion.completion_stream_generator`：**
  - 加 `functools.wraps(original)` 和 `**kwargs`；
  - 把结果流包一层，再调用原函数，流结束时记一行 chunk 数。
  - 现场的包装还会在包装的流里记 KV 读取失败原因，这一点没有模拟。
- 现场长度校验抛哪种异常不知道，所以 `site` 和 `sitev` 两种都跑。

**vllm-ascend V1 runner 上的测量点（`P2D_RUNNER_POINTS=1`，所有树都设）。**

- **为什么要模拟：**
  - vLLM 补丁只在 Model Runner V2（`vllm/v1/worker/gpu/model_runner.py`）上加 `@pd_execution`、`@pd_sample`；V2 需要 Triton，CPU 上 vLLM 退回 V1 runner。
  - glm5.3 现场用的是 vllm-ascend 的 V1 NPU runner（`model_runner_v1.py`）：ascend 补丁 `08-worker-model_runner_v1.patch` 在它上面加这几个点，见 t51-glm53-ascend 的 `tools/check_runner_points.py`。
- **套到 `CPUModelRunner`（V1 `GPUModelRunner` 的子类）上的点：**
  - `execute_model`：`@torch.inference_mode()` 套 `@pd_execution`；
  - `sample_tokens`：同样套 `@pd_sample`；
  - 模型调用外包 `diag.span("runner.forward", completion="host_return")`；
  - 采样外包 `diag.span("runner.sampling", algorithm=..., completion="host_return")`。
- 用的都是补丁自带的装饰器和 span。未打补丁的树里没有这些，这项设置不起作用。
- **没有模拟的：** NPU 设备计时（`pd_device_timing.measure`），CPU 上没有对应物。

### 2.3 判定（各脚本）

- **`compare_cases.py`：** 逐场景比较状态码、各 choice 的文本和 finish_reason、`[DONE]`。被中止的流只比状态码和 `[DONE]`，因为它停在第几个 token 取决于时机。
- **`check_records.py`：** 从服务日志的 `(进程名 pid=N)` 前缀区分进程，按事件和 scope/phase 计数，并检查以下几项：
  - **公共字段：** event、version=2、host、pid、native_tid、run_id、timestamp_ns、record_id 齐全；
  - **链条：** 见第 0 节；另外没有 Worker 记录引用 EngineCore 没写过的 execution_id；
  - **queue 区间：** 起点是本次运行里的某一步；
  - **面板要读的字段：** 统计各字段出现的记录数。
- **`segments_with_values.py`：** 读过滤器的输出（`pd_seg_partial` 和透传记录），按 glm53 `segments.json` 的段树判断每段是否有值。P/D 场景中，prefill 侧只看 P 的输出，decode 侧只看 D 的输出。
- **`check_site_patch.py`：** 检查两个包装的调用次数、父子关系、收到的关键字参数和被拒次数。

## 3. 结果

所有数字都出自 `results/py312/analysis/`、`results/py311/analysis/` 和各场景目录下的 `client.json`。

### 3.1 服务

| 场景 | 子场景数 | 非 200（两遍相同） | 打补丁前后差异：第一遍 / 第二遍 |
|---|---|---|---|
| sync | 11 | 无 | 0 / 0 |
| async | 11 | 无 | 0 / 0 |
| site | 14 | 两个超长请求，前后都是 500 | 0 / 0 |
| sitev | 14 | 两个超长请求，前后都是 400（`p2d_site_patch: prompt has 201 tokens, more than 64`） | 0 / 0 |
| pd（经代理） | 11 | 无 | 0 / 0 |

- 被中止的流和被拒的请求之后，后续请求都返回 200。
- **两遍之间：** 未打补丁的结果相同，打补丁的（`db27fd1` 和 `40152b2`）结果也相同，各 5 个场景都是 0 差异。
- **服务日志：** 只有强制停服时出现过 `AsyncLLM output_handler failed` / `EngineDeadError`。原因是停服超时为 0，进程管理器直接杀掉 EngineCore，打补丁前后都有，出现与否取决于时机；它们都出现在 `shutdown triggered` 之后。
- **代理日志：** 有 `ConnectionResetError`，来自客户端主动断开的那个流。

### 3.2 每个进程的记录（打补丁后）

| 场景 | 进程 | http | engine_submission | API request | core request | core execution（第一遍 / 第二遍） | Worker execute（第一遍 / 第二遍） | Worker sample = 调度了 token 的步（第一遍 / 第二遍） |
|---|---|---|---|---|---|---|---|---|
| sync | 单实例 | 14 | 14 | 18 | 18 | 136 / 137 | 136 / 137 | 125 / 126 |
| async | 单实例 | 14 | 14 | 18 | 18 | 137 / 138 | 137 / 138 | 126 / 127 |
| site | 单实例 | 17 | 17 | 19 | 19 | 152 / 152 | 152 / 152 | 140 / 140 |
| sitev | 单实例 | 17 | 17 | 19 | 19 | 152 / 152 | 152 / 152 | 140 / 140 |
| pd | D | 14 | 14 | 18 | 18 | 138 / 141 | 138 / 141 | 127 / 130 |
| pd | P | 14 | 14 | 14 | 14 | 23 / 23 | 23 / 23 | 12 / 12 |

各列的含义：http、engine_submission、API request 是 API Server 写的 `api_summary`（scope 分别为 `http`、`engine_submission`、`request`）；core request、core execution 是 EngineCore 写的 `core_summary`（scope 为 `request`、`execution`）；Worker execute、Worker sample 是 Worker 写的 `worker_summary`（phase 为 `execute`、`sample`）。

前四列两遍相同。调度步数取决于并发请求被排进同一步的时机，两遍之间差 0 到 3 步。

条数和请求数的对应：

- **http 和 engine_submission：** 每个 HTTP 请求一条。8 个基本请求加 4 个并发、1 个被中止的流、1 个之后的请求，共 14 个；site 和 sitev 再加 2 个超长请求和 1 个之后的请求。
- **API request 和 core request：** 每个引擎请求一条。n=2 的 4 个请求各拆成 2 个子请求，所以是 10 + 8 = 18。8 个子请求的 API 和 core 记录都带 `parent_request_id`，每个父请求下 2 个。被拒的请求没有进入引擎，没有这两种记录。
- **API request 的终态：** sync、async、pd 的 D 中，17 个 `length`、1 个 `cancelled`（被中止的流）；P 上 14 个都是 `length`（max_tokens=1）。
- **Worker execute：** 每一步都有一条，比调度了 token 的步多出来的是 0 token 的步；sample 只在有 token 的步写。链条检查对每个有 token 的步都找到恰好一条 execute 和一条 sample，`request_ids` 相同，没有孤立的 Worker 记录。
- **被拒请求的 engine_submission 记录（两遍相同）：**
  - 抛 `ValueError` 时 `status=error`，抛 `VLLMValidationError` 时 `status=rejected`，两种都没有 add_request 段。
  - 补丁只把 `VLLMClientError` 一类（`VLLMValidationError` 是其子类）和 `GracefulHTTPError` 记为 `rejected`，其余异常记为 `error`。
  - 面板二 v3.1-extra 的代码里没有用到 `rejected`（统筹核过），所以这一分类不影响看板。
- **面板要读的字段：**
  - `stages.req_resolve`、`render`、`tokenize`、`add_request`、`detokenize`、`queue.initial_wait_ns`、`hash`、`alloc`、`other` 在各自的记录里都齐全。
  - `stages.lookup` 只出现在调度新请求、做了前缀查找的步：sync 中第一遍 136 步里有 13 步，第二遍 137 步里有 14 步。
  - Worker 记录：`stages.forward.duration_ns` 只在有 token 的步出现，`stages.sampling.duration_ns` 每条采样记录都有。
  - pd 中 Worker execute 每条都有 `stages.submit`（start_load_kv 的调用）。

### 3.3 两个模拟的现场包装（site 和 sitev，两遍、打补丁前后都相同）

- **`_add_request`：** 调用 21 次。
  - 12 次不带父请求：10 个 n=1 请求、1 个超长 n=1 请求、1 个被拒之后的请求。
  - 5 个 n=2 父请求：4 个各有子请求 0 和 1；超长的那个在子请求 0 被拒，子请求 1 没有再发出。
  - 共被拒 2 次。
- **打补丁前后的差别**只在收到的关键字参数：
  - 打补丁前，21 次都没有关键字参数；
  - 打补丁后，`owner` 12 次、`accounted,owner` 9 次，都原样传给原函数。
- **`completion_stream_generator`：** 调用 3 次（流式 n=1、流式 n=2、被中止的流），chunk 数依次为 14、26、2。

### 3.4 面板二过滤器：哪些段有值

glm53 的 `segments.json` 有 47 个段（含父段）。两遍的 5 个场景都是 22 段有值，其余 25 段为空、只来自代理或没有来源。下表以第一遍的 sync 为例，第二遍的请求数与之相同。

P/D 场景按实例分开看：prefill 侧的段读 P 的输出（14 个请求），decode 侧读 D 的输出（18 个请求）。结果与单实例相同，只是 prefill 侧的请求数是 14。

| 段 | 有值的请求数 | 没有值的原因 |
|---|---|---|
| prefill / decode 的 req_resolve、tokenizer、add_request | 14 条（按 http 和 engine_submission 记录计） | |
| prefill / decode 的 schedule：queue_time、alloc、other；prefill 另有 hash、lookup | 18 | |
| decode 的 tpot.schedule：alloc、other（取第一步之后的部分） | 18 | |
| prefill / decode 的 detokenizer，decode 的 tpot.detokenizer | 18 | |
| prefill / decode 的 forward、sampling，decode 的 tpot.forward、tpot.sampling | 0 | glm53 配置取 NPU 设备时间 `stages.forward.device_duration_ns`、`stages.sampling.device_duration_ns`，不回退到主机时间（v3.1-extra C15）。CPU 上没有设备计时；Worker 记录里的主机时间都在 |
| prefill 的 wait_for_put；start_load_kv.submit；wait_for_save 及其下各段 | 0 | 来自 vllm-ascend 缓存池（pool_task）和 Mooncake（pd_native_put），本次不涉及 |
| decode 的 start_load_kv.submit、wait_for_transfer | 0 | submit 的值在 Worker execute 记录里，但要经传输记录（C2：consumer link / task）映射到请求上；wait_for_transfer 读 request_generation 的 `transfer_envelope_ns`。ExampleConnector 不写传输记录，现场由 p2d 补丁写（见 `patches/vllm-ascend-p2d/`） |
| lb_proxy | — | 来源是代理那一行日志，本次没有 |
| api_gateway、prefill 的 start_load_kv.wait_for_transfer | — | 配置里没有来源 |

### 3.5 P/D（两遍相同）

- **控制流程走通了。** D 的 EngineCore 有 18 次 `External Cache Hit!`，即调度器在 D 上认定这些 token 来自外部，P 存、D 取的控制流程是通的。D 的 core 请求记录里 `kv_role=kv_consumer`。
- **KV 数据没有写进 D。** D 的 Worker 每次进入 ExampleConnector 的 `start_load_kv`，都因为 `forward_context.attn_metadata is None` 告警后直接返回（11 次），一次 `Inject KV cache` 也没有出现。打补丁前后相同。所以 D 的输出文本和单实例不同，但打补丁前后一致。
- **对本次验证的影响：** 这影响的只是 KV 内容，不影响 vLLM 侧的调度、记录和链条。D 侧和传输相关的段在 CPU 上本来就没有来源，见 3.4 节。

### 3.6 启动行 `PD_DIAG_STARTUP`

- **`c9ca251`（第一遍复跑 site）：** 比 `db27fd1` 只多了 `core.py`、`worker_base.py` 两个文件的启动行，其余文件相同。
  - 和打补丁前比：14 个子场景 0 差异。
  - 两个包装的调用：与 `db27fd1` 相同。
  - 记录：条数逐类与 `db27fd1` 相同，链条完整，有值的段同为 22。
  - 启动行一共 2 行：EngineCore 1 行，Worker 1 行（`model_runner=… tp_rank=0 device_timing=?`）。
- **`40152b2`（第二遍，所有场景）：** 比 `c9ca251` 只改了 `worker_base.py`，Worker 的启动行拆成两行。每个服务进程组写 3 行；pd 中 P、D 各 3 行。
  - 实际输出（第二遍 sync，单实例）：

    ```
    (Worker pid=160) (VllmWorker-0 pid=160) INFO 11-19 08:37:44 [worker_base.py:254] PD_DIAG_STARTUP worker model_runner=vllm.v1.worker.cpu_model_runner.CPUModelRunner tp_rank=0
    (EngineCore pid=110) (EngineCore pid=110) INFO 11-19 08:37:46 [core.py:131] PD_DIAG_STARTUP scheduler=vllm.v1.core.sched.scheduler.Scheduler balance=? recompute=? dyntra_lb=? batch_job=? profiling_chunk=?
    (Worker pid=160) (VllmWorker-0 pid=160) INFO 11-19 08:37:48 [worker_base.py:278] PD_DIAG_STARTUP worker device_timing=? tp_rank=0
    ```

  - EngineCore 那一行的各个开关都是 `?`，因为没有加载 vllm-ascend；Worker 的 `device_timing=?`，因为 CPU runner 上没有 `_pd_device_timing`。
- 这几行都不带 `[PD_DIAG] ` 标记，过滤器不读。

### 3.7 只装 vLLM 补丁时（`results/py312_only_vllm_patch/`，第一遍）

这一遍只有 sync 场景，n=2 用贪心采样，所以 n=2 的 4 个请求前后都返回 400；其余 7 个子场景前后都是 200，客户端结果 0 差异。

- 打补丁后，API Server 和 EngineCore 有记录：http 14、engine_submission 10、API request 10、core request 10、core execution 83。
- Worker 记录为 0 条，因为 V1 runner 上没有测量点（见 2.2 节）。

## 4. 和 glm5.3 现场的对应

现场一列出自 t51-glm53-kit 的第 2 次检测（运行中的实例）和现场小核对 f-1、f-3b。

| 项 | 本次（CPU） | glm5.3 现场 | 本次没有覆盖的部分 |
|---|---|---|---|
| 设备 | CPU（aarch64 容器） | 昇腾 NPU | 设备计时（forward、sampling 段的值）、NPU 图、HCCL |
| Python | 第一遍 3.12.3，第二遍 3.11.10 | 3.11.10（f-1、f-3b 的 `py=3.11.10`） | |
| torch | 2.13.0+cpu（两遍相同） | torch 加 torch_npu，本次没有核对版本 | |
| transformers | 5.14.1 | 5.14.1（第 1、2 次检测） | |
| tokenizers、huggingface-hub | 0.22.2、1.33.0 | 现场版本未采集 | 按 transformers 5.14.1 的约束取范围内最新的正式版 |
| vLLM | v0.30.0 官方 CPU 构建加补丁：第一遍 `db27fd1`，第二遍为最终的现场补丁 `40152b2` | v0.30.0 加补丁，跑在 vllm-ascend `d4ad49796` 上 | vllm-ascend 自己的调度器、runner、缓存池 |
| runner | `CPUModelRunner`（V1），加模拟的测量点 | vllm-ascend `model_runner_v1.py`，由 ascend 补丁 08 加 `@pd_execution`、`@pd_sample`、两个 span 和设备计时 | runner 内部逻辑；实际测量点以 t51-glm53-ascend 的补丁为准 |
| 图模式 | P、D 都是 eager | P：`eager=1`；D：`eager=0`，即图模式 | D 的图模式路径 |
| MTP | 无 | P：`deepseek_mtp:1`；D：`deepseek_mtp:5`；两者都是 `spec_eager=1` | 投机解码路径：延迟采样、rejection 采样的 span、`pd_sample` 对 MTP 的处理 |
| 异步调度 | sync、async 两种都跑；pd 中 P 关、D 开 | P：`async=0`；D：`async=1` | |
| 并行 | TP1、DP1，一个 Worker | P：TP16、DP8，16 个 Worker；D：TP1、DP64，探测到的这台机器上有 16 个端口（DP rank 0–15） | tp_rank>0 的记录（过滤器只取 rank 0，C3）；DP 协调 |
| KV 连接器 | ExampleConnector，共享目录 | MooncakeConnectorV1 加 AscendStore，`policy=fail`；D 上 `load_async=1` | 传输记录（由 p2d 补丁写，已在 `patches/vllm-ascend-p2d/` 里单独测过）；真实的 KV 读取 |
| 现场包装 | 按 f-3b 的签名模拟，只计数、只做长度校验 | P 上有 `patch_prefill_length_validation.py` 和 `patch_kv_load_failure_reason.py`；D 上只有后者 | 两个文件的实际内容；长度校验抛的异常类型；KV 读取失败原因的记录 |
| 调度器 | vLLM 自带的 `Scheduler` | vllm-ascend 的调度器（见 t51-glm53-vllm README 第 11 节） | 启动行里 balance、recompute 等开关的实际取值 |
| 模型 | 2 层小 Llama，float32 | GLM-5.3 | 模型相关路径 |
| 过滤器 | 面板二 v3.1-extra，glm53 配置 `-19c` | 同一过滤器 | 代理那一行日志 |

## 5. 已知问题

- ExampleConnector 在 CPU 上不写入 KV（3.5 节）。这是 vLLM v0.30.0 CPU 路径自身的情况，打补丁前后相同。
- runner 测量点是按 vllm-ascend 补丁写法做的模拟（2.2 节），不是 vllm-ascend 的代码本身。
- 长度校验的异常类型和现场的 KV 读取失败包装的内容不知道（3.2、3.3 节）。
- 强制停服时偶尔出现 `EngineDeadError`，打补丁前后都有（3.1 节）。
- 本机下载 PyPI 和 PyTorch 索引的文件时，偶尔有内容损坏和断连（第 1 节）。

## 6. 复现

第一遍：

```bash
WORK=<工作目录> VLLM_REPO=<t51-glm53-vllm> VLLM_COMMIT=db27fd1 bash cpu_e2e/setup_pass1.sh
```

```bash
WORK=<工作目录> PASS=py312 PYPATH_EXTRA=/w/site/tf5141 bash cpu_e2e/run_pass.sh sync async site sitev pd
```

```bash
bash cpu_e2e/analyze_pass.sh <工作目录>/results/py312 <vllm-pd-grafana-panel2>/profiles/glm53/segments.json
```

第二遍：先跑完 `setup_pass1.sh`，再做出 `40152b2` 的树，并搭 Python 3.11.10 环境：

```bash
WORK=<工作目录> VLLM_REPO=<t51-glm53-vllm> bash cpu_e2e/make_patched_tree.sh 40152b2
```

```bash
WORK=<工作目录> bash cpu_e2e/setup_pass2.sh
```

```bash
WORK=<工作目录> PASS=py311 PY311_ENV=<工作目录>/py311env PY=/opt/py311/bin/python3.11 PYPATH_EXTRA= TREES="vllm030cpu vllm030cpu_40152b2" bash cpu_e2e/run_pass.sh sync async site sitev pd
```

```bash
AFTER=vllm030cpu_40152b2 bash cpu_e2e/analyze_pass.sh <工作目录>/results/py311 <vllm-pd-grafana-panel2>/profiles/glm53/segments.json
```

- `run_pass.sh` 每个场景开跑前都检查剩余空间，低于 30.5 GB 就等，最多等 1 小时。
- `PANEL` 默认指向 v3.1-extra 的 `vllm-pd-grafana-panel2`。
- 结果目录里的日志已用 gzip 压缩，分析脚本可以直接读 `.log.gz`。
