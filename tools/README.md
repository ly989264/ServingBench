# 工具

| 文件 | 用途 | 来源 |
|---|---|---|
| `arrange_boards.py`、`boards_layout.txt` | Grafana 两层文件夹整理（plan / apply / undo / state / folder）。两个部署包都只认顶层文件夹，任何 add、update、rollback 之后执行一次 `apply` | 交付件《看板分层与加集群》，2026-11-17；现在是 v3.4-extra 交付的版本（0d3d4ec7、da33f3ac，T64：对照表第一列写 `-` 表示放在顶层，test-glm53 放在顶层 `GLM-5.3-test`） |
| `test_arrange_boards.py` | `arrange_boards.py` 的单测（假 Grafana） | T64 |
| `make_inventories.py`、`clusters.txt` | 从机器表 `clusters.txt` 生成面板一的 `inventory.json`。`clusters.txt` 是空白模板，现场填好的版本不进 git | 同上 |
| `SHA256SUMS` | 上面四个文件的校验和（原交付件中另两行是已填路径的说明书，已去掉） | 同上 |

`boards_layout.txt` 是 v3.4-extra 交付的版本（10 行，最后一行 `- test-glm53 GLM-5.3-test`）。内网现在的那份是 T45 的 9 行加 T51 面板手册第 16 步追加的 `GLM-5.3 test-glm53 test-glm53`；v3.4-extra 更新第 5 步换成仓库这份（见 `docs/50-已知问题与待办.md` K69）。
| `panels_snapshot.py` | 单个集群的只读配置快照（格式 2）；只支持带面板一的集群 | 交付件，2026-11-13 |
| `panel1/probe_panel1.py`、`PROBE_PANEL1.zh-CN.md` | 只读探测内网面板一（看板、Prometheus 配置、清单），输出可交给 `diff_panel1.py` | T26，2026-11-12 |
| `panel1/diff_panel1.py`、`DIFF_PANEL1.zh-CN.md` | 现场看板与内嵌基线比对、`--decisions-out` 列出差异、`--merge` 合并、`--apply` 恢复 | T23 / T26 |
| `panel1/diag_panel1.py` | 判断看板被谁改回（10 秒内读 20 次，区分文件加载与其他写入方） | T23，2026-11-11 |

基线变了（`panel1/pack/vllm-pd-grafana-panel1/grafana/dashboards/`）时，`diff_panel1.py` 的内嵌基线和 `probe_panel1.py` 都要重新生成；生成脚本在开发目录（demo 目录 `tests/t23-panel1/embed_baseline.py`、`tests/t26-probe/build_probe.py`），未带入本仓库，见 `docs/50-已知问题与待办.md`。
