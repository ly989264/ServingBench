# vLLM 补丁（产物 2）

| 项 | 内容 |
|---|---|
| 补丁 | 同事补丁包 `pd-diag-patches-20261116` 里的 01（`01-vllm-v0.32.0-diagnostics.patch`），原样使用。下面的替换件就是 `v0.32.0` 打上这份补丁得到的结果。补丁原文件不随交付包交付（同事那边本来就有）；需要核对时，和内网那份比 SHA-256：`7cda4008c1fafe45ab809a26e9367f8ec24b6f79d779a9e02c33460efed470eb`（和补丁包的 `SHA256SUMS` 一致） |
| 基线 | vLLM `v0.32.0`（`5df63a72`） |
| 替换件 | `files/vllm/...`：打完补丁后的 25 个源文件（其中 2 个是新文件） |
| 校验表 | `SHA256SUMS.before`：其中 23 个原有文件在原版 `v0.32.0` 里的 SHA-256，2 个新文件另列在 `NEW_FILES.txt`；`SHA256SUMS.after`：替换后应有的 SHA-256 |

用法：

- 检测结果 `vllm=... state=v0.32.0` 时，按操作说明 A 替换这 25 个文件。
- `state=v0.32.0+01` 时，补丁 01 已经装好，什么都不做。
- `state=modified` 时先不动，按 `vllm_changes=` 给出的文件和函数另定。

验证：补丁包自带的检查加上我们在 `v0.32.0` 上重做的 `git apply --check` 都通过，替换件由打完补丁的源码树导出。补丁 01 的代码只读过，没有在本机跑 vLLM。补丁 01 必须和 vllm-ascend 补丁一起装：vllm-ascend 一侧引用 `vllm.v1.metrics.pd_diagnostics`、`scheduler_output.pd_execution_id`、`vllm.v1.worker.worker_base.pd_execution`。
