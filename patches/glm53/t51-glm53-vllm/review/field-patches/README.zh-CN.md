# 现场两个本地补丁对 01 埋点的影响

现场小核对 glm53f-1 报出 vllm-ascend `patch/platform/` 下新增了两个文件，内容我们看不到：

| 文件 | 部署位置 | f-1 报出的内容 |
|---|---|---|
| `patch_kv_load_failure_reason.py` | P、D | `completion_stream_generator`（引用、新定义并赋值）；导入了 `serving` 和 `scheduler` |
| `patch_prefill_length_validation.py` | 只在 P | 引用 `_add_request`；新定义 `add_request` 并赋值；导入了 `async_llm` |

模拟方法（`field_copy_sim.py`）：在 v0.30.0 加本仓库移植版上，用 `docker run --rm` 跑 HTTP 场景 F1–F8，把替换函数分别做成两种：

- 整份复制：原样复制 v0.30.0 原函数，编译进同一个模块；
- 包一层：先做一步占位的校验，再调用移植版自己的函数。

另加一种「复制，但 `_add_request` 的 `owner` 有默认值」，用来看补上默认值以后的情况。各模式的结果在 `results/summary.txt`。

## 结论

| 情况 | 结果 |
|---|---|
| P 的 `add_request` 是整份复制 | 每个请求都报 `TypeError: AsyncLLM._add_request() missing 1 required keyword-only argument: 'owner'`（`results/add_copy_typeerror.txt`）。原因是 01 把 `_add_request` 的 `owner` 改成了必须传的关键字参数。**P 无法服务**。`ea04aa1` 起 `owner` 有默认值，见下一行 |
| 同上，`owner` 有默认值 `None` | 8 个场景都正常返回；只有 P 的 add_request 段为空（不写 `api_summary[engine_submission]`），其余段都在 |
| `add_request` 包一层、调原函数 | 与不替换时完全相同 |
| `completion_stream_generator` 是整份复制 | 47 段都不受影响。丢掉的是 `/v1/completions` 流式请求的 `api_summary[http].first_generated`；流式生成中途出错时，status 记成 completed 而不是 error。P 上走不到这个函数（proxy 发给 P 的是非流式请求） |

## f-3b 的结果（11-19）

f-3b 报出了两处替换的具体目标（检测脚本 `3594cfef`，带 `--show-diff`）：

| 文件 | 部署位置 | 替换的目标 | 检测结果 |
|---|---|---|---|
| `patch_prefill_length_validation.py`（sha16 `c36392a6338bc2f1`） | 只在 P | `AsyncLLM._add_request`（f-1 那一行把它和 `add_request` 混在了一起） | 签名 `(self, request, *args, **kwargs)`；4 行，原函数 21 行；`wraps_orig=1` |
| `patch_kv_load_failure_reason.py`（sha16 `d7c764bb425993c0`） | P、D | `OpenAIServingCompletion.completion_stream_generator` | 加了 `**kwargs`；69 行，原函数 221 行；`wraps_orig=0`；diff 开头是 `@functools.wraps(original)` |

**判断：两处都是包装，属于 (a)。** 01 的埋点保留，vLLM 补丁不需要反向重放。

- `_add_request` 是包装：`wraps_orig=1` 表示检测脚本认出了它调用事先存下的原函数，存下的就是移植版自己的 `_add_request`。
- `completion_stream_generator` 的 `wraps_orig=0` 是漏报。检测脚本只认「先把原函数赋给一个名字再调用」的写法，闭包参数 `original` 认不出来。判断它是包装的依据：
  - `@functools.wraps(original)`；
  - 内部生成器 `record_load_failures` 把 `(prompt_idx, res)` 原样逐个交出，只记下 `pending`。这种写法只有在把它交给另一个消费者、也就是原函数时才有用；
  - 69 行对 221 行；与 v0.30.0 相同的 17 行只有签名（函数头、4 个参数、右括号）、一个 `continue`、一处 `CompletionStreamResponse(...)` 的构造。原函数逐个输出的处理（文本增量、logprobs、usage）一行都不在。
- 两处的调用行都在被截断的 diff 里，没有直接看到。操作说明 A 的打补丁前核对里加一行 grep 确认：

  ```bash
  grep -nE "original|_orig|kwargs" vllm_ascend/patch/platform/patch_kv_load_failure_reason.py vllm_ascend/patch/platform/patch_prefill_length_validation.py
  ```

  它同时能看出 `_add_request` 的包装是否把关键字参数原样传下去。

**按 f-3b 的描述写成包装后的模拟**（`field_wrap_sim.py`，最终补丁，HTTP 场景 F1–F8，结果在 `results/wrap_summary.txt`）：

| 模式 | 结果 |
|---|---|
| `_add_request` 包装，关键字参数原样传下去 | 8 个场景的记录与不替换时逐条相同。包装在 F1、F2、F4、F5、F6、F8 各被调用 1 次，在 F3（n=2）被调用 2 次；F7 在分词前就被拒绝，不经过它 |
| 同上，但不传关键字参数 | 请求照常返回；`owner` 丢失，所有请求的 add_request 段为空，也没有 `add_request_send` 链接 |
| 同上，长度上限设成 5 个 token | F1、F5 被包装拒绝，返回 400；`engine_submission` 状态为 `rejected`，`error_type` 为 `VLLMValidationError`，没有 add_request 段 |
| `completion_stream_generator` 包装（`record_load_failures` 加改写输出块） | 8 个场景的记录与不替换时逐条相同 |
| 同上，强制每个结束的输出都走改写路径 | 记录逐条相同；F2 改写了 1 个输出块 |
| 两个包装同时装上（P） | 8 个场景的记录与不替换时逐条相同 |

**模拟中发现、已经改掉的一处。** 在 `ea04aa1` 上，F3 的包装被调用 0 次。原因是移植版照 01（也就是 v0.32.0 上游）的写法，n>1 的子请求不经过 `_add_request`，而原版 v0.30.0 的每个子请求都经过它。这样现场的长度校验对 n>1 的子请求不生效。最终补丁把子请求改回经过 `_add_request`。

按现场写法替换 `_add_request` 后，一个请求调用替换函数的次数（`fanout_count.py`，结果在 `results/fanout_counts.txt`）：

| | n=1 | n=2 | n=4 |
|---|---|---|---|
| 原版 v0.30.0 | 1 | 2 | 4 |
| `ea04aa1` | 1 | 0 | 0 |
| 最终补丁 | 1 | 2 | 4 |

整份复制的 6 种模式在最终补丁上重跑，结果与第一节的结论相同（`results/summary-final.txt`）：
- 整份复制 `add_request`：add_request 段为空，其余照常；
- 整份复制 `completion_stream_generator`：F2 缺 `first_generated`；
- 包一层：与不替换时相同。

`completion_stream_generator` 的口径写在 `patches/vllm/README.zh-CN.md` 第 13 节末尾。

