# glm5.3 Mooncake 增量包 v2（T58）

把 P、D 容器里的 `libmooncake_store.so` 换成替换库 v2。v2 是 Mooncake `78566d4` 加补丁 04 和补丁 04b 编出的：
- 计时本身和 v1 相同：P 侧「后台 wait_for_save → put」下 3 个子段的值照旧；
- 计时记录不再写进 vLLM 日志（glog、标准错误），改写进节点目录 `/data/pd-diag/logs/mooncake/` 下的文件，格式见记录格式约定 1.5.0 第 8.4 节；
- `PD_DIAG_LEVEL` 恰好是 `1` 或 `2` 时不写；`MC_PD_TIMING=0` 时也不写；不设、`3` 和其余值照写。

**两种起点，换库以前都先跑核对：**

| 现在的库 | 核对结果 | 怎么换 | 回滚 |
|---|---|---|---|
| 替换库 v1（glm53-test；15402248 字节，sha256 `31317843…`，增量包 `6d70978c`） | `ok_upgrade` | 先把现在的 v1 备份出来，再换成 v2 | 用这份备份换回 v1，不从增量包 v1 重装 |
| 现场原库（glm53-1；17610953 字节，sha16 `3bf82f8b0c5ab715`） | `ok_replace` | 先备份原库，再换成 v2 | 用备份换回原库 |

核对结果是别的值（任何 `STOP_…`）就不换，带回输出。唯一的例外仍是原库只差 `.comment` 的那种 `STOP_build_differs`（glm53-test 换 v1 时就是这种情况）：照 T53 第五部分的例外一步做两条只读命令，再由用户决定；从 v1 升到 v2 不经过这一步，`ok_upgrade` 不看构建是否相同（v1 本身是我们编的）。怎么做见各自的手册（glm53-test 的 T58 升级手册，T53 第五部分），手册放在包外面。

| 文件 | 说明 |
|---|---|
| `lib/libmooncake_store.so` | 替换库 v2：15402392 字节，sha256 `37a3aaf8acb53c6a3cdb9251b9e6d3e1edc0c13789c8d4138d111b1d71cc4a6f` |
| `precheck/dist/t51_mc_precheck.py` | 换库前的只读核对 glm53mc-6，sha256 `559782ea1dfb29c1b2ec2a2016b3423f0a6fe9e8869732dd1970a231e9262327` |
| `precheck/使用说明.zh-CN.md` | 核对脚本每一行怎么读、判决规则（含 `ok_upgrade`）、实验室结果 |
| `precheck/lab/lab_v6.txt`、`precheck/lab/run_lab_v6.sh` | glm53mc-6 实验室用例的完整输出和用例脚本 |
| `reference/04-mooncake-0.3.13-native-timing.patch` | 补丁 04 原文（v1 用的），只供查看 |
| `reference/04b-mooncake-timing-files.patch` | 补丁 04b 原文（在 04 之上），只供查看，现场不用 |
| `lab/*.txt` | v2 的实验室结果：导出名核对、类布局、解析表、原签名包装、写文件的各项（开关、粒度、退出、fork、目录建不了、切分、清理、队列满） |
| `SHA256SUMS` | 以上全部文件的 sha256 |

**v2 和 v1 的对外接口相同：**
- 导出的名字逐个相同（和未打补丁的核对件比：去掉 0 个、新增 3 个，和 v1 一样）；
- 头文件和 v1 逐字相同，`mooncake::Client` 的类布局和原版逐行相同；
- NEEDED、RUNPATH、SONAME 和 v1 相同。

换库以后的核对用 T58 补丁包里的检测脚本 glm53-8：v2 报 `mooncake=ok(v2)`，v1 报 `mooncake=ok(v1)`。
