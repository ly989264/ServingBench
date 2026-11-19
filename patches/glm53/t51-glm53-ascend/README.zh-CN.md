# T51 glm5.3：vllm-ascend 补丁（基线 `d4ad49796`）

把同事补丁 02 移植到 glm5.3 的 vllm-ascend `d4ad49796`（`p2p/mooncake_connector.py` 除外），加上 dsv4.1 版新加的「等 put 完成」计时和 `pool_load` 映射记录。写出的记录和 dsv4.1 版的约定 v2.2 相同。

| 目录 | 内容 |
|---|---|
| `patches/vllm-ascend/` | 补丁、单文件补丁、替换件、校验表、可选件、重放工具；说明见其中的 `README.zh-CN.md` |
| `tools/` | `compare_record_shapes.py`（两棵树的记录逐字段比较）、`check_runner_points.py`（runner 测量点静态核对） |
| `ut_harness/` | 容器里跑单元测试和记录场景的脚本、pytest 插件 |
| `ut_results/` | 测试和对比的原始结果 |

本仓库只在本地提交，不推送。
