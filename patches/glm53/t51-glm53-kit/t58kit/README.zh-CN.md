# glm5.3 补丁包（T58：粒度开关 `PD_DIAG_LEVEL`）

在 T56 的基础上，加一个环境变量 `PD_DIAG_LEVEL`，控制 vLLM、vllm-ascend、p2d 记到第几级：
- 不设、`3`：全记，和 T56 逐字段相同；
- `2`：不记 L3；
- `1`：只记 L1，schedule 的子段照记。
- 其余值按 3。

记录内容和规则见记录格式约定 1.5.0 第 8 节。记录的 `version` 仍是 2。

**两条升级路径：**
- **glm53-test：** 从 T56 升到 T58。核对用 `SHA256SUMS.t56`。
- **glm53-1：** 从现场原样直接打 T58。vLLM 的核对用 `patches/vllm/SHA256SUMS.before`、`NEW_FILES.txt`，这两个文件取自 T51 补丁包 `d21cc319…`，和那边逐字节相同。

两条路径都是整个集群停一次，P、D 都换。怎么做见各自的手册，手册放在包外面（它要写这个包的 sha256）。

| 目录 | 内容 | 来源 |
|---|---|---|
| `patches/vllm/` | vLLM v0.30.0 的 T58 补丁：<br>- 完整补丁 `t58-vllm-v0.30.0-pd-diag.patch`（干净 v0.30.0 上用）<br>- 增量补丁 `t58-vllm-from-t56.patch`（已装 T56 的环境用，只含变了的 7 个文件）<br>- 替换件 `files/vllm/`（25 个，和 T51、T56 是同一组文件）<br>- `SHA256SUMS.after`（25 个）、`SHA256SUMS.t56-changed`（增量补丁要改的 7 个文件在 T56 下的值）<br>- `SHA256SUMS.t56`（T56 版 25 个文件的值，glm53-test 升级前核对用）<br>- `SHA256SUMS.before`、`NEW_FILES.txt`（现场原样的文件清单，glm53-1 从原样直接打 T58 时核对用） | `t51-glm53-vllm` 提交 `97d5de8` 的 `patches/vllm/t58/`，不含 `lab/`；`SHA256SUMS.t56` 取自 T56 补丁包（`0d2bc9a8…`）的 `patches/vllm/SHA256SUMS.after`；`SHA256SUMS.before`、`NEW_FILES.txt` 取自 T51 补丁包（`d21cc319…`），逐字节相同 |
| `patches/vllm-ascend/` | vllm-ascend `d4ad49796` 的主体补丁 T58 版：<br>- 替换件 `files/vllm_ascend/`（11 个；和 T56 比改了 `store/diagnostics.py`、`worker/pd_device_timing.py` 2 个）<br>- `SHA256SUMS.before`（9 个）、`SHA256SUMS.after`（11 个）、`NEW_FILES.txt`（2 个）<br>- 合并补丁、`per-file/` 单文件补丁、`replay/` 按锚点重放工具、`tools/apply_check.sh`；`t56/` 是 T56 版的合并补丁和 `SHA256SUMS.after`<br>- `SHA256SUMS.t56`（T56 版 11 个文件的值，升级前核对用）<br>- `SHA256SUMS.d4ad49796-unchanged`（`platform.py`、`utils.py` 在公开 d4ad49796 上的值） | `t51-glm53-ascend` 提交 `0818adb` 的 `patches/vllm-ascend/`，不含 `optional/`；`tools/apply_check.sh` 取自同一提交；`SHA256SUMS.t56` 取自 T56 补丁包的 `patches/vllm-ascend/SHA256SUMS.after` |
| `patches/p2d/` | MooncakeConnectorV1 的 D 读 KV 埋点 T58 版：<br>- `replay_p2d.py`（29 处改动，锚点、判据、编号和 T56 版相同）<br>- 补丁、替换件（对应公开原文件，只作参照）、校验表、测试<br>- `t56/`、`t51/`：T56、T51 版的 `replay_p2d.py` 和补丁，字节不变，升级时证明现场文件是哪一版重放出来的<br>- `field_check/`：`extra_checks.py`、只读的 `span_diff.py`、锚点和判据表、升级步骤说明 | `t51-glm53-p2d` 提交 `87807b1` 的 `patches/vllm-ascend-p2d/` 和 `field_check/` |
| `patches/mooncake/` | 只有说明：Mooncake 的替换库 v2 在 Mooncake 增量包 v2（`t58-glm53-mooncake-kit.tar`）里 | — |
| `detect/` | 环境检测脚本 glm53-8 `t51_glm53_check.py`（只读）、使用说明、内嵌基线的来源清单 `PORT_BASELINE.txt`。内嵌 T51、T56、T58 三版打完补丁后的样子和 Mooncake 替换库 v1、v2 的指纹；`run` 行末多 `diag_level=` | `t51-glm53-kit` 的 `detect/dist8/`（sha256 `b7b11e29…`） |

关键文件的 sha256：

| 文件 | sha256 |
|---|---|
| `patches/vllm/t58-vllm-v0.30.0-pd-diag.patch` | `cb5b63585b396fff…` |
| `patches/vllm/t58-vllm-from-t56.patch` | `9aec9b72ee4aed52…` |
| `patches/vllm/SHA256SUMS.after` | `999d4f72278da0d6…` |
| `patches/vllm-ascend/t58-vllm-ascend-d4ad49796-pd-diag.patch` | `36d37dd605661842…` |
| `patches/vllm-ascend/SHA256SUMS.after` | `5467154cd8f3b7f6…` |
| `patches/p2d/replay_p2d.py` | `e62795bd63b146eb…` |
| `patches/p2d/files/.../mooncake_connector.py` | `01ce3b2a7dd5cdb1…` |
| `patches/p2d/t58-vllm-ascend-d4ad49796-p2d-diag.patch` | `94646c95dde8f753…` |
| `detect/t51_glm53_check.py` | `b7b11e295a73ba91…` |

**几点说明：**
- **3 级就是 T56：** 默认不设时，P 上写出的东西和 T51 逐字段相同，D 上的和 T56 逐字段相同。启动行只多一行 `PD_DIAG_STARTUP diag level=3 source=default`。
- **Mooncake 不在这个包里：** 见 `patches/mooncake/README.zh-CN.md` 和 Mooncake 增量包 v2。

交付的 tar 用 `t58kit/tools/make_kit.sh` 打，所有内容都取自上面几个提交，不取工作区。根目录的 `SHA256SUMS` 覆盖 tar 里的每个文件。
