# glm5.3 补丁包（T56：D 侧新增的计时）

在已经打完 T51 的 glm5.3 测试集群上，把所有容器的 vLLM、vllm-ascend、p2d 三件补丁升级到 T56，补上 D 侧等 KV、第一步、第二步起的几段计时。整个集群停一次、一起拉起；新内容只在 D 上写，P 写出的东西和 T51 一样。怎么做见《glm53-test：v3.2-extra 升级操作手册》，手册放在包外面（它要写这个包的 sha256）。

新加的记录内容和取值规则见记录格式约定 1.4.0。概括起来：

- 「是不是 D」只用 vllm-ascend 自己的条件；
- 新内容只在 D 上写，装到 P 上和 T51 版逐字段相同；
- 记录的 `version` 仍是 2；已有阶段的边界不动。

| 目录 | 内容 | 来源 |
|---|---|---|
| `patches/vllm/` | vLLM v0.30.0 的 T56 补丁：<br>- 完整补丁 `t56-vllm-v0.30.0-pd-diag.patch`（干净 v0.30.0 上用）<br>- 增量补丁 `t56-vllm-from-t51.patch`（已装 T51 的环境用，只含变了的 4 个文件）<br>- 替换件 `files/vllm/`（25 个）<br>- `SHA256SUMS.after`（25 个）、`SHA256SUMS.t51-changed`（增量补丁要改的 4 个文件在 T51 下的值）<br>- `SHA256SUMS.t51`（T51 版 25 个文件的值，升级前核对用） | `t51-glm53-vllm` 提交 `796927a` 的 `patches/vllm/t56/`，不含 `lab/`（实验室测试补丁）；`SHA256SUMS.t51` 取自 T51 补丁包（`d21cc319…`）的 `patches/vllm/SHA256SUMS.after` |
| `patches/vllm-ascend/` | vllm-ascend `d4ad49796` 的主体补丁 T56 版：<br>- 替换件 `files/vllm_ascend/`（11 个）<br>- `SHA256SUMS.before`（9 个）、`SHA256SUMS.after`（11 个）、`NEW_FILES.txt`（2 个）<br>- 合并补丁、`per-file/` 单文件补丁、`replay/` 按锚点重放工具、`tools/apply_check.sh`<br>- `SHA256SUMS.t51`（T51 版 11 个文件的值，升级前核对用）<br>- `SHA256SUMS.d4ad49796-unchanged`（`platform.py`、`utils.py` 在公开 d4ad49796 上的值：「是不是 D」用的条件就在这两个文件里，升级前核对现场没有本地改动） | `t51-glm53-ascend` 提交 `3380187` 的 `patches/vllm-ascend/`（和 `cf72b07` 只差合并补丁改名为 `t56-…`，内容相同），不含 `optional/`（hybrid connector，glm5.3 不用）；`tools/apply_check.sh` 取自同一提交的 `tools/`；`SHA256SUMS.t51` 取自 T51 补丁包的 `patches/vllm-ascend/SHA256SUMS.after` |
| `patches/p2d/` | MooncakeConnectorV1 的 D 读 KV 埋点 T56 版：<br>- `replay_p2d.py`（29 处改动）<br>- 补丁<br>- 替换件（对应公开原文件，只作参照）<br>- 校验表<br>- `t51/`：T51 版的 `replay_p2d.py` 和补丁，字节不变，升级时证明现场文件是 T51 重放出来的<br>- `field_check/`：`extra_checks.py`、只读的 `span_diff.py`、锚点和判据表、升级步骤说明 | `t51-glm53-p2d` 提交 `d7d26f3` 的 `patches/vllm-ascend-p2d/` 和 `field_check/` |
| `patches/mooncake/` | 只有说明：T56 不碰 Mooncake | — |
| `detect/` | 环境检测脚本 glm53-7 `t51_glm53_check.py`（只读）、使用说明、内嵌基线的来源清单 `PORT_BASELINE.txt`。脚本内嵌 T51、T56 两版打完补丁后的样子，`verdict` 里的 `ok` 带版本名 | `t51-glm53-kit` 提交 `11b5ddd` 的 `detect/dist7/`（sha256 `588e6d94…`） |

关键文件的 sha256：

| 文件 | sha256 |
|---|---|
| `patches/vllm/t56-vllm-v0.30.0-pd-diag.patch` | `7959cbb716ff1018…` |
| `patches/vllm/t56-vllm-from-t51.patch` | `5d7578685c525b30…` |
| `patches/vllm/SHA256SUMS.after` | `9a8a55078c629365…` |
| `patches/vllm-ascend/t56-vllm-ascend-d4ad49796-pd-diag.patch` | `d0fbb82bce0fe453…` |
| `patches/vllm-ascend/SHA256SUMS.after` | `0b46fa152612d152…` |
| `patches/p2d/replay_p2d.py` | `9b12baa6b38c8ff7…` |
| `patches/p2d/files/.../mooncake_connector.py` | `256b335e06f9c5b6…` |
| `patches/p2d/t56-vllm-ascend-d4ad49796-p2d-diag.patch` | `7eeb231f180abd16…` |
| `detect/t51_glm53_check.py` | `588e6d94d278a876…` |

**几点说明：**

- **P 也换成 T56：** glm53-test 整个集群停一次，P、D 所有容器换成同一版。新代码在 P 上写出的记录和 T51 版逐字段相同，这一点在实验室用记录等价测试核过。
- **Mooncake 不动：** 已经换过库（`mooncake=ok`）和没换（`mooncake=not_replaced`）都是正常状态。

交付的 tar 用 `t56kit/tools/make_kit.sh` 打，所有内容都取自上面几个提交，不取工作区。根目录的 `SHA256SUMS` 覆盖 tar 里的每个文件。
