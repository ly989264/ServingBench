# 面板二源码

面板二（请求时延拆解）的部署包源码、更新脚本、诊断工具和测试，取自开发仓库的当前文件：本仓库的第一个提交是 v3（开发仓库 main 1815e9a），第二个提交换成 v3.1-extra（分支 v3.1-extra 61cc794），2026-11-22 的同步换成 v3.4-extra（分支 t64 8ea0d90，就是部署包 `b883692a` 的来源提交），`git diff` 相邻两次即可看到其间的全部改动。v3.2-extra（0d846b7，包 `d77528d4`）、v3.3-extra（t56 9002ae3，包 `d071644c`）没有单独的源码提交，只有 Release 里的部署包。开发仓库的 git 历史中有现场值（用户目录、内网地址、本机路径），不能进入公开仓库，因此没有带历史。

总说明见 [../docs/20-面板二.md](../docs/20-面板二.md)；最详细的原始说明是 [../docs/面板系统说明书.zh-CN.md](../docs/面板系统说明书.zh-CN.md)（v3.1-extra）。

## 目录

| 目录 | 内容 |
|---|---|
| `vllm-pd-grafana-panel2/` | 部署包源码（`bin/`、`scripts/`、`node/`、`clickhouse/`、`grafana/`、`segments.json`、`profiles/glm53`、`profiles/dsv41`） |
| `update/` | 更新脚本的源文件。交付时 `tests/t41/pack_dist.sh` 写入 tar 的 sha256；已填路径版（`*_filled.py`，只改 `USER_DIR` 一行）不进 git |
| `diag-tool/` | v3.4-extra 交付的只读诊断工具 `pd_panel2_diag.py`（单文件，在监控机上直接跑，输出几十行），`使用说明.zh-CN.md` |
| `diag-kit/` | 独立诊断工具。README 与 `diag.conf.example` 中的节点地址和用户目录已换成占位符，与交付件不同 |
| `docs/v3/`、`docs/v3.1-extra/` | 手册与说明书的源文件（占位符版；`docs/v3.1-extra/` 中的面板手册取自分支 v3.1-extra d58f47e，proxy 手册和《执行顺序总表》取自 t64 8ea0d90，即现场改文件路径的 ce5ddf80、85f1b78e）。v3.2-extra 以后的手册不在开发仓库里，交付原件在 `../manuals/`。`fill_guide.py` 需要环境变量 `USER_DIR`；`fill_guide_b.py` 从已填路径的更新脚本读取用户目录 |
| `tests/` | 见下表 |

## 打包

只用一条路径：`tests/t41/build_pack.sh <输出目录> [提交]` 从一个提交 `git archive` 出部署包，`tests/t41/pack_dist.sh` 组装交付目录（tar、sha256、更新脚本、手册）。`build_pack.sh` 已在本仓库布局下运行过；`pack_dist.sh` 没有：它是 v3 交付时写的，其中 `git show <提交>:update/…` 的路径按开发仓库根目录写（本仓库为 `panel2/update/…`），并依赖不进 git 的已填路径文件，下次出交付目录前要先改（`docs/50-已知问题与待办.md` K72）。v3.1-extra 的交付目录不是用它组装的。demo 目录的 `tools/build_panel2_pack.sh` 已废弃，不在本仓库。

2026-11-19 核对：用本仓库的两个提交分别出包，包内文件与在用的两个 tar 逐文件相同，只有 `MANIFEST.txt` 的构建时间和来源提交一行不同——v3 提交对应 glm53-1 的 3e98c2b4（原件 built 2026-11-16 from 765a0fc），v3.1-extra 提交对应 test-glm53 的 e3885b93（原件 built 2026-11-19 from 7ef24eb）。2026-11-22 核对：用同步 v3.4-extra 以后的本仓库 `tests/t41/build_pack.sh <输出目录> HEAD` 出包，包内文件与 `b883692a` 逐文件相同，只有 `MANIFEST.txt` 第 3 行（构建时间和来源提交）不同。

## 测试

2026-11-22 在本仓库布局下运行的结果（macOS，`python3` 为 3.9，标明 3.12 的用 Python 3.12）：

| 测试 | 需要 | 结果 |
|---|---|---|
| `tests/t41/test_segments_mock.py` | 无 | 13 项通过 |
| `tests/t51/test_update_rollback.py` | 无 | 0 FAIL |
| `tests/t51/test_native_put.py` | 无 | 不通过：`add_task()` 的参数在 v3.3-extra 变了，开发仓库原样运行结果相同；v3.3-extra 起由下面的 `test_native_put_v33.py` 代替 |
| `tests/t56/test_update_rollback_t56.py` | 无 | 0 FAIL |
| `tests/t58/test_native_put_v33.py <部署包目录>` | 无 | 0 FAIL |
| `tests/t58/test_diag_level_v33.py <部署包目录> <T58 的 pd_diagnostics.py>` | Python 3.10 以上 | 0 FAIL（3.12；3.9 下补丁文件的类型注解报错） |
| `tests/t64/test_proc_tree_19.py` | 无 | 通过 |
| `tests/v3/test_known_issues.py` | pytest | 81 项通过、4 项不通过：这是 v3 的测试，未随 v3.1-extra 更新（`VERSION` 与 `safety.py` 接口变了），开发仓库原样运行结果相同；在 v3 提交上为 84 过 1 不过 |
| `tests/t41/e2e_v3.py`、`tests/t51/e2e_v31.py`、`tests/t51/e2e_ratio73.py`、`tests/t56/e2e_t56.py`、`tests/t58/e2e_t58.py`、`tests/t64/e2e_t64.py`（`run_e2e_*.sh`） | Docker（一次性容器 Vector 0.61.0、ClickHouse 26.8.36.3，`--rm`） | 本次未运行；交付时 t64 四种粒度组合 0 FAIL（`../manuals/面板二/交付说明_面板包b883692a.zh-CN.md`） |

需要实验室或真实日志、因此没有带入的测试：`tests/t32`、`tests/t33`（多容器的内网形态实验室）、`tests/t51/test_filter_candidate.py`、`tests/t51/test_filter_device.py`（需要两份真实日志，日志不进仓库；第二个提交里带进过，第三个提交删除）、`tests/t51/lab_native_sources.py`、`tests/t53`（proxy 手册与执行顺序总表的实验室走查）、`tests/t51s`、`tests/t53g`（手册的实验室走查）、`tests/t56/setup_lab.sh`、`walk_t56.sh`、`tests/t58/walk_t58*.sh`、`lab_dist.py`、`fill_manual.py`、`tests/t64/walk_t64.sh`（依赖 `tests/t33` 的实验室）、`tests/t64/make_dist_t64.sh`（从开发侧任务目录取文件、写已填路径版），开发仓库根目录的 `dist-*/`（交付目录，包在 Release，说明在 `../manuals/`）、`proxy-patch/`（proxy 补丁的源目录，交付的包解开在 `../patches/glm53/t51-glm53-proxy-kit/`）、`docs/t53/`（T53 旧版手册的分段源文件，已由 `../drive/t53-drive/` 的一页手册代替），以及所有 `results/` 目录。`tools/test_arrange_boards.py` 要一个真 Grafana，本次未运行。
