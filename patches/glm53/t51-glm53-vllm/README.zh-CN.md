# T51 glm5.3：vLLM v0.30.0 诊断补丁

同事补丁 01（vLLM v0.32.0 诊断埋点）在 vLLM v0.30.0 上的等价移植，写出的 `[PD_DIAG]` 记录与 v0.32.0 加 01 相同。

| 目录 | 内容 |
|---|---|
| `patches/vllm/` | 现场补丁（只含 `vllm/`）、替换件、校验表、说明（`README.zh-CN.md`） |
| `patches/vllm/lab/` | 只给实验室用的 lab 补丁：01 的工具、文档和全部测试，不进现场 |
| `verify/` | 容器里的验证脚本 |
| `verify/results/` | 验证结果 |

本仓库只在本地提交，不推送。
