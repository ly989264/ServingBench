# 面板二源码

面板二（请求时延拆解）的部署包源码、更新脚本、诊断工具和测试，取自开发仓库的当前文件：本仓库的第一个提交是 v3（开发仓库 main 1815e9a），第二个提交换成 v3.1-extra（分支 v3.1-extra 61cc794），`git diff` 两个提交即可看到 v3 到 v3.1-extra 的全部改动。开发仓库的 git 历史中有现场值（用户目录、内网地址、本机路径），不能进入公开仓库，因此没有带历史。

总说明见 [../docs/20-面板二.md](../docs/20-面板二.md)；最详细的原始说明是 [../docs/面板系统说明书.zh-CN.md](../docs/面板系统说明书.zh-CN.md)（v3.1-extra）。

## 目录

| 目录 | 内容 |
|---|---|
| `vllm-pd-grafana-panel2/` | 部署包源码（`bin/`、`scripts/`、`node/`、`clickhouse/`、`grafana/`、`segments.json`、`profiles/glm53`、`profiles/dsv41`） |
| `update/` | 更新脚本的源文件。交付时 `tests/t41/pack_dist.sh` 写入 tar 的 sha256；已填路径版（`*_filled.py`，只改 `USER_DIR` 一行）不进 git |
| `diag-kit/` | 独立诊断工具。README 与 `diag.conf.example` 中的节点地址和用户目录已换成占位符，与交付件不同 |
| `docs/v3/`、`docs/v3.1-extra/` | 手册与说明书的源文件（占位符版）。`fill_guide.py` 需要环境变量 `USER_DIR`；`fill_guide_b.py` 从已填路径的更新脚本读取用户目录 |
| `tests/` | 见下表 |

## 打包

只用一条路径：`tests/t41/build_pack.sh <输出目录> [提交]` 从一个提交 `git archive` 出部署包，`tests/t41/pack_dist.sh` 组装交付目录（tar、sha256、更新脚本、手册）。`build_pack.sh` 已在本仓库布局下运行过；`pack_dist.sh` 没有：它是 v3 交付时写的，其中 `git show <提交>:update/…` 的路径按开发仓库根目录写（本仓库为 `panel2/update/…`），并依赖不进 git 的已填路径文件，下次出交付目录前要先改（`docs/50-已知问题与待办.md` K72）。v3.1-extra 的交付目录不是用它组装的。demo 目录的 `tools/build_panel2_pack.sh` 已废弃，不在本仓库。

2026-11-19 核对：用本仓库的两个提交分别出包，包内文件与在用的两个 tar 逐文件相同，只有 `MANIFEST.txt` 的构建时间和来源提交一行不同——v3 提交对应 glm53-1 的 3e98c2b4（原件 built 2026-11-16 from 765a0fc），v3.1-extra 提交对应 test-glm53 的 e3885b93（原件 built 2026-11-19 from 7ef24eb）。

## 测试

2026-11-19 在本仓库布局下运行的结果：

| 测试 | 需要 | 结果 |
|---|---|---|
| `tests/t41/test_segments_mock.py` | 无 | 13 项通过 |
| `tests/t51/test_update_rollback.py` | 无 | 0 FAIL |
| `tests/t51/test_native_put.py` | 无 | 0 FAIL |
| `tests/v3/test_known_issues.py` | pytest | 81 项通过、4 项不通过：这是 v3 的测试，未随 v3.1-extra 更新（`VERSION` 与 `safety.py` 接口变了），开发仓库原样运行结果相同；在 v3 提交上为 84 过 1 不过 |
| `tests/t41/e2e_v3.py`、`tests/t51/e2e_v31.py`、`tests/t51/e2e_ratio73.py` | Docker（一次性容器 Vector 0.61.0、ClickHouse 26.8.36.3，`--rm`） | 本次未运行 |

需要实验室或真实日志、因此没有带入的测试：`tests/t32`、`tests/t33`（多容器的内网形态实验室）、`tests/t51/test_filter_candidate.py`、`tests/t51/test_filter_device.py`（需要两份真实日志，日志不进仓库；第二个提交里带进过，第三个提交删除）、`tests/t51/lab_native_sources.py`，以及所有 `results/` 目录。
