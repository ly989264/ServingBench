# glm5.3 Mooncake 增量包（操作说明 A 第 11 步）

补丁包（t51-glm53-patch-kit.tar，sha256 7adc1c10…）操作说明 A 第 9 步、第 11 步写的「待补」，以本包为准：第 11 步见 guides/操作说明A_第11步_替换Mooncake库.zh-CN.md，回滚见第 11.9 节。

补丁包操作说明 A 第 11 步：把 P、D 容器里的 `libmooncake_store.so` 换成打了补丁 04 的替换库，让 P 侧「后台 wait_for_save → put」下的 3 个子段（submit、wait_for_transfer、finalize）有值。补丁包第 1–10 步做完以后才做。

| 文件 | 说明 |
|---|---|
| `guides/操作说明A_第11步_替换Mooncake库.zh-CN.md` | 第 11 步的完整操作：放进容器、换库前的核对、备份原库、换库、重启、核对、回滚 |
| `lib/libmooncake_store.so` | 替换库：Mooncake `78566d4` 加补丁 04，15402248 字节，sha256 `3131784385846064327e858d6dcdb11a6c7674c2162964d52f9ed0e866b52bb8` |
| `precheck/dist/t51_mc_precheck.py` | 换库前的只读核对 glm53mc-5，sha256 `4475c662c9ae669b50fa60bd097b386f2b4a3f6b5987eb78b0a0686cafd24cd4` |
| `precheck/使用说明.zh-CN.md` | 核对脚本每一行怎么读、判决规则、实验室结果 |
| `precheck/lab/lab_T0-T10.txt`、`precheck/lab/run_lab.sh` | 核对脚本实验室用例 T0–T10 的完整输出和用例脚本 |
| `reference/04-mooncake-0.3.13-native-timing.patch` | 补丁 04 原文，只供查看，现场不用 |
| `SHA256SUMS` | 以上全部文件的 sha256 |

换库以后的核对用补丁包里的检测脚本（`detect/t51_glm53_check.py`，sha256 `f9e6073f…`），本包里没有。
