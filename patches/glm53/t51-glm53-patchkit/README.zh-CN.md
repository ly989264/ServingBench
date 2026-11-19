# glm5.3 补丁包（T51）

给 glm5.3 测试集群的 P、D 容器打 `[PD_DIAG]` 埋点补丁。怎么打见 `guides/操作说明A_glm53_容器内打补丁.zh-CN.md`。

| 目录 | 内容 | 来源 |
|---|---|---|
| `patches/vllm/` | vLLM v0.30.0 的诊断补丁：替换件 `files/vllm/`（25 个），`SHA256SUMS.before`（23 个原文件）、`SHA256SUMS.after`（25 个），`NEW_FILES.txt`（2 个新文件），补丁本身 | `t51-glm53-vllm` 提交 `40152b2`，`README.zh-CN.md` 取自 `42c730a`（只把同事个人构建目录的名字改成泛称，补丁和替换件不变）。现场补丁只含 `vllm/` 下的 25 个运行时文件（改 23 个、新增 2 个）；README 里提到的 `lab/` 补丁（补丁 01 的工具、文档和测试，只给实验室跑测试）不放进补丁包 |
| `patches/vllm-ascend/` | vllm-ascend `d4ad49796` 的主体补丁：替换件 `files/vllm_ascend/`（11 个），`SHA256SUMS.before`（9 个）、`SHA256SUMS.after`（11 个），`NEW_FILES.txt`（2 个），合并补丁，`per-file/` 单文件补丁，`replay/` 按锚点重放工具，`tools/apply_check.sh` | `t51-glm53-ascend` 提交 `ef5bd17`；`README.zh-CN.md` 取自 `b1f725f`（之后只加了复跑结果）；不含 `optional/`（hybrid connector，glm5.3 不用） |
| `patches/p2d/` | MooncakeConnectorV1 的 D 读 KV 埋点：`replay_p2d.py`（在现场的 `mooncake_connector.py` 上重放），补丁，替换件（对应公开原文件，只作参照），校验表；`extra_checks.py`（X1–X6 补充核对，只读源码文本） | `t51-glm53-p2d` 提交 `a9e9695`；`extra_checks.py` 取自 `5595f58` 的 `field_check/`（sha256 `2759c06f…`），操作说明 4.3 那一段以 `5595f58` 为准（sha256 `ea72c3e1…`）。和 `a9e9695` 相比改了一处：`SHA256SUMS.before` 改名为 `SHA256SUMS.d4ad49796`（里面是公开原文件的值，现场的文件是本地修改版，sha256 前 16 位 `2c4acb3eeb1ced7d`），`README.zh-CN.md` 里两处引用和本目录的 `SHA256SUMS` 跟着改 |
| `patches/mooncake/` | 只有说明：替换库待现场核对后交付（操作说明 A 第 11 步，可后补；不做时 47 段里 42 段有值） | — |
| `detect/` | 环境检测脚本 glm53-6 `t51_glm53_check.py`（只读，操作说明 A 第 2、8 步）、使用说明和内嵌基线的来源清单 `PORT_BASELINE.txt`。脚本内嵌打完补丁后的基线：vLLM v0.30.0 加本包 `patches/vllm/`（t51-glm53-vllm `40152b2`，补丁 `7e2ec359`，SHA256SUMS.after `5a708485`，25 个文件）；vllm-ascend `d4ad49796` 加本包 `patches/vllm-ascend/`（补丁 `024a8b3e`，SHA256SUMS.after `4c2d978a`，11 个文件）；P/D connector 是现场重放的，按结构认（内嵌 `replay_p2d.py` `02304c8f`、`extra_checks.py` `2759c06f`）；Mooncake 的现场原库 sha16 `3bf82f8b0c5ab715`、交付的替换库 `31317843…`（替换库不在这个包里） | `t51-glm53-kit` 提交 `56a0d64`（`detect/dist/`，sha256 `f9e6073f…`，和它的 `dist/SHA256SUMS` 一致）；使用说明、`expected/PORT_BASELINE.txt` 取自同一提交 |
| `guides/` | 操作说明 A（占位符版），实验室走查 | 本仓库 |

**V2 runner 的两个文件。** vLLM 补丁改了 V2 runner 的 `vllm/v1/worker/gpu/kv_connector.py`、`vllm/v1/worker/gpu/model_runner.py`。glm5.3 现场用的是 V1 runner（检测结果 `runner=V1(env)`），这两个文件在现场不会被执行。补丁里保留它们，是因为这套改动和 dsv4.1 的补丁共用，两边保持同一份。打补丁前照样按 `SHA256SUMS.before` 核对这两个文件：现场这两个文件和 v0.30.0 相同，不会误停。

关键文件的 sha256：

| 文件 | sha256 |
|---|---|
| `patches/vllm/t51-vllm-v0.30.0-pd-diag.patch` | `7e2ec359f5781ccd…` |
| `patches/vllm/SHA256SUMS.after` | `5a7084855419155f…` |
| `patches/vllm-ascend/t51-vllm-ascend-d4ad49796-pd-diag.patch` | `024a8b3e1cbbf368…` |
| `patches/vllm-ascend/SHA256SUMS.after` | `4c2d978a0017a228…` |
| `patches/p2d/t51-vllm-ascend-d4ad49796-p2d-diag.patch` | `ab0c771776373f42…` |
| `patches/p2d/files/.../mooncake_connector.py` | `f953b27c3e53c125…` |
| `patches/p2d/replay_p2d.py` | `02304c8f76429780…` |
| `patches/p2d/extra_checks.py` | `2759c06f4bead2b0…` |

交付的 tar 用 `tools/make_tar.sh` 打，根目录的 `SHA256SUMS` 覆盖 tar 里的每个文件。已填路径版的操作说明只放私有的 Caching 仓库，不进 tar。
