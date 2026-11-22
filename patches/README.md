# 补丁一侧：glm5.3 的诊断补丁与检测脚本

面板二的数据来自这些补丁：它们在节点上的 vLLM、vllm-ascend、Mooncake 里写出 `[PD_DIAG]` 记录和 `[BatchPutTiming]` 行，格式由《记录格式约定》（`contracts/记录格式约定-glm5.3-v1.3.3.zh-CN.md`）规定，面板二按约定解析。补丁不属于面板二的部署包，在节点容器和 proxy 机器上另行安装；先看 `manuals/执行顺序总表.zh-CN.md`（唯一入口），手册在 `manuals/补丁/`。

规则（`docs/40-规则.md` P4）：补丁必须在检测到的目标环境上验证（调用路径比对、目标版本内部 API 存在性检查），不得带入其他版本的残留。

## 来源

各目录是开发侧同名 git 仓库的当前文件（2026-11-22 同步；`t51-glm53-vllm` 取 t58 分支，其余取 main），**不带 git 历史**（与 `panel2/` 的做法相同）。

| 目录 | 来源提交 | 内容 |
|---|---|---|
| `glm53/t51-glm53-vllm/` | t58 分支 97d5de8（2026-11-20；上次同步为 main 42c730a）。T56 版在 `patches/vllm/t56/`、`verify/t56/`，T58 版（`PD_DIAG_LEVEL`）在 `patches/vllm/t58/`、`verify/t58/` | 同事补丁 01（vLLM v0.32.0 诊断埋点）在 vLLM v0.30.0 上的等价移植；调用路径比对、启动检查、现场本地补丁影响的模拟 |
| `glm53/t51-glm53-ascend/` | 0818adb（2026-11-20；上次 9dad4b6）。T56 版在 `patches/vllm-ascend/t56/`，T58 版为 `t58-vllm-ascend-d4ad49796-pd-diag.patch` | 同事补丁 02 移植到 vllm-ascend `d4ad49796`（`kv_p2p/mooncake_connector.py` 除外），加「等 put 完成」计时和 `pool_load` 映射记录；联合检查 |
| `glm53/t51-glm53-p2d/` | 87807b1（2026-11-20；上次 7936bb0）。T51、T56 版在 `patches/vllm-ascend-p2d/t51/`、`t56/`，T58 版为 `t58-…-p2d-diag.patch`；升级用的 `field_check/span_diff.py` 与两份《操作说明 A》 | `MooncakeConnectorV1`（`kv_p2p/mooncake_connector.py`）的 D 读 KV 诊断记录；在 CPU 版 vLLM 0.30 上的端到端脚本 |
| `glm53/t51-glm53-kit/` | ac6ae43（2026-11-20；上次 2c407ee）。新增 T56、T58 补丁包的组装目录 `t56kit/`、`t58kit/`，检测脚本 `detect/dist7/`（glm53-7）、`dist8/`，Mooncake 计时写自己的文件（`mooncake/patch/04b-…`、`replace/build_v2/`、`increment_v2/`、`precheck/dist6/`） | 环境检测脚本（`detect/`）、现场核对（`fieldcheck/`）、Mooncake 替换库的构建与换库前核对（`mooncake/`）、日志量估算（`volume/`）、打补丁手册的源文件（`manual/`）、《记录格式约定》原文（`CONTRACT.zh-CN.md`） |
| `glm53/t51-glm53-proxy-kit/` | 面板二开发仓库的 `dist-proxy-patch/t51-glm53-proxy-kit.tar.gz`（c08056fe，现场改文件路径 px-3；上次为 v3.1-extra d58f47e 的 75fd49e8）解开后的文件；这份补丁的源码只以这个包的形式保存 | vLLM 代理示例 `load_balance_proxy_server_example.py` 的 `[PD_DIAG]` 补丁（`main/` 与 `v0.30/` 两版，各带离线测试）、只读检查 `t51_proxy_check.py`、启停脚本 |
| `glm53/t51-glm53-patchkit/` | 207d7a4（2026-11-19） | 打补丁包 `t51-glm53-patch-kit.tar`（d21cc319）的组装目录：上面几个仓库的补丁文件、检测脚本、《操作说明 A》；`tools/make_tar.sh` 出包 |
| `dsv41/t51-dsv41-kit/` | 305a553（2026-11-19） | **已暂停**（用户决定 T51 只做 glm5.3）。dsv4.1 版的检测脚本、补丁与说明，只放文本源码 |


## 已交付的包（GitHub Release）

| tag | 资产 | 用在 | 组装 |
|---|---|---|---|
| `glm53-patch-kit-d21cc319` | `t51-glm53-patch-kit.tar`（5.7 MB） | glm5.3 测试集群（test-glm53 的两台机器）的 P、D 容器；《操作说明 A》第 1–10 步 | `glm53/t51-glm53-patchkit/tools/make_tar.sh` |
| `glm53-proxy-kit-75fd49e8` | `t51-glm53-proxy-kit.tar.gz`（111 KB） | test-glm53 的 proxy 机器；《glm5.3 测试集群 proxy 打补丁操作手册》 | 面板二开发仓库 v3.1-extra `dist-proxy-patch/` |
| `glm53-t56-patch-kit-0d2bc9a8` | `t56-glm53-patch-kit.tar` | glm5.3 测试集群 T56 升级（已被 T58 取代）；《glm53-test：v3.2-extra 升级操作手册》 | `glm53/t51-glm53-kit/t56kit/tools/make_kit.sh` |
| `glm53-t58-patch-kit-4f59ea87` | `t58-glm53-patch-kit.tar`（7.7 MB） | glm5.3 测试集群（在用）；《glm53-test：v3.3-extra 升级操作手册》 | `glm53/t51-glm53-kit/t58kit/tools/make_kit.sh` |
| `glm53-mooncake-kit-v2-e3e338d5` | `t58-glm53-mooncake-kit.tar`（16 MB，含替换库 v2） | 同上（T58：Mooncake 计时写自己的文件） | `glm53/t51-glm53-kit/mooncake/increment_v2/` |
| `glm53-proxy-kit-c08056fe` | `t51-glm53-proxy-kit.tar.gz` | test-glm53 的 proxy 机器（在用，替换 75fd49e8）；proxy 手册 ce5ddf80 | 面板二开发仓库 `dist-proxy-patch/` |
| `glm53-mooncake-kit-6d70978c` | `t51-glm53-mooncake-kit.tar`（15 MB，含替换用的 `libmooncake_store.so`） | 同上；《操作说明 A》第 11 步（可延后） | `glm53/t51-glm53-kit/mooncake/increment/` |

sha256 与交付说明见 `releases/<tag>/`。

## 没有带入的内容

| 内容 | 原因 |
|---|---|
| `t51-glm53-p2d/fork/`（T58 p2d 改动导出成 vllm-ascend fork 的提交补丁） | 补丁头里有提交人的邮箱；同样的提交在公开 fork 的分支 `v0.30-pd-diag` 上，改动本身就是 `patches/vllm-ascend-p2d/t58-…-p2d-diag.patch` |
| Mooncake 替换库 v2 的二进制（`mooncake/replace/lib_v2/` 只带了 `SHA256SUMS`） | 二进制；glm5.3 用的那一份在 Mooncake 增量包 v2 里（Release `glm53-mooncake-kit-v2-e3e338d5`） |
| 各仓库的 `results/`、`ut_results/` 目录，以及 `lab/` 下的运行输出（`*.txt`） | 测试与走查的输出，不是源码；其中有开发机路径 |
| `t51-glm53-kit/field/` | 现场带回的检测输出和对它的分析 |
| `t51-glm53-patchkit/guides/已填路径/` | 已填路径版 |
| Mooncake 替换库的二进制（`*.so.xz`） | 二进制；glm5.3 用的那一份在 Mooncake 增量包里（Release） |
| `t51-dsv41-kit/patches/vllm/01-vllm-v0.32.0-diagnostics.patch` | 同事的原始补丁 01，其中有个人目录；dsv4.1 交付包本来也不含它 |
| 同事的原始补丁包（pd-diag-patches-20261116，针对 vllm-ascend v0.32.0rc1） | 只作参考，不在任何开发仓库 |

## 与来源不同的 7 处

| 文件 | 改动 |
|---|---|
| `glm53/t51-glm53-kit/manual/lab/walk_patch.sh` 第 17 行 | `P2D_SIM` 的默认值原指向开发机的本机路径，改为必须显式设置 |
| `glm53/t51-glm53-patchkit/guides/lab/walk_A.sh` 第 16 行 | 同上 |
| `glm53/t51-glm53-p2d/cpu_e2e/run_pass.sh` 第 13 行 | `PANEL` 的默认值原指向开发机的本机路径，改为必须显式设置（本仓库为 `panel2/vllm-pd-grafana-panel2`） |
| `glm53/t51-glm53-kit/fieldcheck/lab/make_01_patches.py` 第 21 行 | 模拟现场改动的示例字符串中，形如工号的目录名换成 `example` |
| `glm53/t51-glm53-kit/t56kit/tools/make_kit.sh` 第 14–17 行、`t58kit/tools/make_kit.sh` 第 16–21 行 | 各源仓库和上一版包的默认路径原指向开发机的本机路径，改为必须显式设置（`VLLM_REPO`、`ASC_REPO`、`P2D_REPO`、`T51_TAR`、`T56_TAR`） |
| `glm53/t51-glm53-kit/mooncake/replace/build_v2/run_post.sh` 第 6 行 | 构建用的临时目录和脚本目录原写死为开发机路径，改为必须显式设置 `SP`、`K` |

已交付的包未作任何改动。
