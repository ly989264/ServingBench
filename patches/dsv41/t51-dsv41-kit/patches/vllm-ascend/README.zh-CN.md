# vllm-ascend 补丁（产物 3）

| 项 | 内容 |
|---|---|
| 补丁 | `t51-vllm-ascend-f9c8e0bdb-pd-diag.patch`（21 个文件，含 2 份文档和 8 个测试文件） |
| 基线 | vllm-ascend `f9c8e0bdb` |
| 替换件 | `files/vllm_ascend/...`：打完补丁后的 12 个源文件（其中 `diagnostics.py` 是新文件） |
| 校验表 | `SHA256SUMS.before`（`f9c8e0bdb` 原版；新文件另列在 `NEW_FILES.txt`）、`SHA256SUMS.after` |
| 移植工具 | `tools/port_pool_worker.py`：把补丁 02 对 `pool_worker.py` 的改动重放到 `f9c8e0bdb` 的文件上 |
| 测试辅助 | `ut_harness/`：本机跑单元测试用的两个 pytest 插件，不进补丁 |

## 怎么做的

1. 补丁 02 用三方合并打到 `f9c8e0bdb` 上：17 个文件自动合并，`kv_transfer.py`（只有 import 冲突）和 `pool_worker.py` 冲突。`kv_transfer.py` 去掉 RC1 才有的 `MambaStateSharding` import；`pool_worker.py` 从 `f9c8e0bdb` 原文件出发，用 `tools/port_pool_worker.py` 重放补丁 02 的改动。
2. 核对：19 个文件里，「`f9c8e0bdb` → 移植版」的增删行和「RC1 → RC1+02」的增删行逐行相同（`diff -w`，不计缩进）。保存路径上 RC1 和 `f9c8e0bdb` 都是 `add_save_batch` 加批次结束标记，两版只差等待保存完成的函数（RC1 `wait_for_previous_save`，`f9c8e0bdb` `wait_for_requests_saved`）；每个请求入队时取快照，批次结束标记不产生记录，和 RC1+02 相同。
3. 新增两处：
   - `KVCacheStoreSendingThread.wait_for_requests_saved` 把等待时间写进当前 `worker_summary` 的 `stages.wait_for_put`（`CONTRACT.zh-CN.md` 第 1 节），测试 `test_wait_for_put_standalone.py`；
   - `KVPoolScheduler.build_connector_meta` 末尾为这一步发起异步缓存池加载的每个请求写一条 `pd_transfer_summary[scope=link]`、`transfer_role="pool_load"` 的映射记录（约定 3.3 节），测试 `test_pool_load_link_standalone.py`。
4. 不含补丁 03。

## 验证

| 项 | 结果 |
|---|---|
| `git apply --check` | 在干净的 `f9c8e0bdb` 上通过；打完后整棵树和移植树逐字节相同 |
| 语法 | 全部 `vllm_ascend/*.py` 用 Python 3.12 解析通过 |
| 新增测试（只用标准库，`docker run --rm` 的 `python:3.12-slim` 里跑） | `test_wait_for_put_standalone.py` 4 个、`test_pool_load_link_standalone.py` 6 个，全部通过 |
| 补丁 02 自带的独立测试 `test_compact_diagnostics_standalone.py` | 移植版、RC1+02 各 7 个全部通过 |
| `tests/ut/distributed/ascend_store`，`docker run --rm` 的 `python:3.12-slim`，torch 和 vLLM 用测试自带的桩，`pd_diagnostics` 用真实模块（加映射记录之后跑的） | `f9c8e0bdb` 原版 145 个通过；RC1+02 154 个通过；移植版 162 个通过；三者都没有失败。另有 19 个测试文件因为桩里缺 `torch.nn`、`vllm.v1.worker.gpu` 等收集不了，三棵树相同 |

vLLM 补丁和本补丁必须一起装：`worker/v2/model_runner.py`、`worker/v1/model_runner.py` 在模块顶层 `from vllm.v1.worker.worker_base import pd_execution, pd_sample`，这两个名字只有 vLLM 补丁里有，只装本补丁时 Worker 启动就 ImportError。

## 没有验证的

- 加映射记录之后，用真实 vLLM 的完整测试（含调用 `build_connector_meta` 的 `test_pool_scheduler.py`）没有在容器里重跑：容器里装不了 torch 和 vLLM 的依赖（这台机器的网络会截断大文件下载）。映射记录的方法用标准库测试覆盖，整段包在 `try/except` 里。

- 在昇腾卡上运行；测量点是否落在预期位置。
- 图模式下 forward 的口径（C8）。
- 层式保存（`use_layerwise`）路径：dsv4.1 的配置里没有打开，没有专门测。
