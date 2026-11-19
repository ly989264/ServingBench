# 面板一源码

面板一（服务侧 Prefill、Decode 两张看板，每集群一个 Prometheus）的部署包源码、打包脚本和离线测试。总说明见 [../docs/10-面板一.md](../docs/10-面板一.md)。

## 来源

`pack/vllm-pd-grafana-panel1/` 是在用部署包（tar sha256 前 8 位 60ce7f30，built 2026-11-12T15:06:34Z，8 套面板一共用）解开后的文件，与开发目录中的源码逐文件相同。开发目录没有 git，旧版只有目录副本，旧版不带入。

看板的真源是 `pack/vllm-pd-grafana-panel1/grafana/dashboards/` 中的两张 JSON：用户内网看板的导出加各轮修改合并后的基线（T26 起内网不再手改面板一）。

## 出包

在 `panel1/` 下执行：

```bash
NO_EXPORT=1 tools/build_pack.sh
```

输出 `panel1/dist/vllm-pd-grafana-panel1.tar.gz` 及其 `.sha256`（`dist/` 不进 git）。与开发目录中的原脚本相比改了两处：不带 `NO_EXPORT=1` 时直接报错退出（原脚本会调用 demo 页面的导出脚本覆盖合并后的看板，本仓库不带该脚本）；补建 git 不保存的两个空目录 `grafana/provisioning/alerting`、`grafana/provisioning/plugins`。2026-11-19 在本仓库布局下重新出包，包内 `SHA256SUMS` 与在用 tar 的完全相同；tar 本身因 gzip 时间戳不能逐字节复现，交付以 Release 中的原件为准。

## 测试

2026-11-19 在本仓库根目录运行的结果，均不需要实验室：

| 测试 | 命令 | 结果 |
|---|---|---|
| Prometheus 目标生成 | `python3 -I panel1/tests/test_gen_targets.py panel1/pack/vllm-pd-grafana-panel1/prometheus/gen_prometheus_yml.py panel1/pack/vllm-pd-grafana-panel1/prometheus/inventory.example.json` | OK |
| 基线比对与合并 | `python3 -I panel1/tests/test_diff_merge.py tools/panel1/diff_panel1.py` | 13 项，0 不通过 |
| add 保留面板二配置块 | `python3 -I panel1/tests/test_keep_blocks.py panel1/pack/vllm-pd-grafana-panel1` | 7 项，0 不通过 |
| 清单生成 | `python3 -I panel1/tests/test_make_inventories.py tools/make_inventories.py panel1/pack/vllm-pd-grafana-panel1 <空目录>` | OK，0 项不通过 |

需要实验室（Grafana、mock exporter 或 vllm-ascend 源码）、因此没有带入的测试：`test_arrange_boards.py`（需要 Grafana）、`run_drill.py`、`run_test.sh`、`test_patch_static.py`（需要 vllm-ascend 源码），以及 demo 目录 `tests/` 下各次实验室走查脚本和结果。
