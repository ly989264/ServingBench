线上 8 个集群在用。本目录由用户维护；新建同类面板请复制到新目录，不在本目录修改。

# 面板一：服务侧 Prefill / Decode 看板

面板一是每个集群两张 Grafana 看板（Prefill、Decode），数据来自该集群自己的一个 Prometheus 容器。Prometheus 抓取各 vLLM 实例 API server 的 `/metrics`、各机器的 npu-exporter 与 node_exporter、Mooncake master。线上 8 个集群（glm53-0、glm53-1、glm53-2、dsv41-flash-1 … 5）都是部署包 v1.2。现场的端口、目录和看板清单见 [`../site/内网现状.md`](../site/内网现状.md) 第 3.1 节。

## 1. 组成

| 路径 | 内容 |
|---|---|
| `pack/vllm-pd-grafana-panel1/` | 部署包 v1.2 的源码。`grafana/dashboards/` 里的两份 JSON 是看板的真源（合并基线）；`bin/panel1_cluster.sh` 为一个集群建 Prometheus、数据源和两张看板；`prometheus/gen_prometheus_yml.py` 由清单生成 `prometheus.yml`；`scripts/` 为命名规则（`names.py`）、看板生成、核对；`patches/` 为可选的 vllm-ascend 指标补丁（现场没有打）；`README.md` 为单集群的逐步安装说明 |
| `tools/build_pack.sh` | 出包：`NO_EXPORT=1 tools/build_pack.sh`（在 `panel1/` 下执行），输出 `dist/vllm-pd-grafana-panel1.tar.gz` 和 `.sha256` |
| `tools/panel1_upgrade.py` | 把已装的 v1.0、v1.1 看板升级为 v1.2：`check`（只读）、`apply`（先备份）、`undo` |
| `tools/make_inventories.py`、`tools/clusters.txt` | 由机器表 `clusters.txt` 生成每个集群的 `inventory.json`；`clusters.txt` 是空白模板，现场填好的版本不进仓库 |
| `tools/arrange_boards.py`、`tools/boards_layout.txt` | 按对照表把各集群的看板放进两层文件夹（模型组 / 集群）：`plan`、`apply`、`undo`、`state`、`folder` |
| `tools/diff_panel1.py` | 只读比较现场看板与内嵌基线，`--decisions-out` 列出差异，`--merge` 合并成新基线 |
| `tests/` | `test_make_inventories.py`、`test_gen_targets.py`、`test_keep_blocks.py`、`test_diff_merge.py`、`test_arrange_boards.py`；参数见各文件开头的说明（`test_arrange_boards.py` 需要一个 Grafana 12.4.2） |

**出包核对。** 由本目录出的包，包内 `SHA256SUMS` 与线上用的 v1.2 交付件（tar 的 sha256 前 8 位 `55847494`）逐字节相同；tar 本身因压缩时间不同而 sha256 不同。

## 2. 部署（新集群）

在监控机上，由部署包和该集群的清单装一套：

```bash
python3 tools/make_inventories.py build clusters.txt --defaults <glm53-1 的 inventory.json> --out inventories --cluster <集群>
python3 tools/make_inventories.py probe inventories/<集群>/inventory.json
bin/panel1_cluster.sh add <集群> --model-group <子文件夹> --inventory inventories/<集群>/inventory.json --env-from <glm53-1 面板二的 deploy.env>
python3 tools/arrange_boards.py apply --env-from <glm53-1 面板二的 deploy.env>
```

- `tools/` 下的命令在 `panel1/` 下执行，`bin/` 下的命令在部署包目录 `pack/vllm-pd-grafana-panel1/`（现场为解包后的目录）下执行。
- `--model-group` 取 `python3 tools/arrange_boards.py folder <集群>` 的输出；先在 `boards_layout.txt` 加这个集群的一行。
- `add` 建 `/data/grafana/vllm-pd-grafana-panel1/<集群>/`、容器 `vllm-pd-prometheus-<集群>`、数据卷、数据源和两张看板，写注册表，最后运行 `verify_panel1.py --cluster`。端口基数自动从 18100 起按 10 分配（`scripts/names.py alloc`），也可用 `--port-base` 指定；Prometheus 的主机端口为端口基数 + 2。
- Grafana 账号从 glm53-1 面板二的 `deploy.env` 读（`--env-from`）。
- 部署包只认顶层文件夹，所以 `add`、升级、回退之后都要执行一次 `arrange_boards.py apply`。

## 3. 升级

线上看板由 v1.0 升到 v1.2 用 `panel1_upgrade.py`：

```bash
python3 tools/panel1_upgrade.py check --env-from <glm53-1 面板二的 deploy.env>
python3 tools/panel1_upgrade.py apply --env-from <glm53-1 面板二的 deploy.env>
python3 tools/panel1_upgrade.py check --env-from <glm53-1 面板二的 deploy.env> | tail -1
```

- `check` 每个集群一行，列出看板版本（v1.0 / v1.1 / v1.2 / 被改过），以及 P、D、Mooncake 的在线数与应有数；被改过的看板不动。
- `apply` 先把看板备份到 `~/vllm-pd-panel1-v1.2-backup/<时间>/`，再改「服务实例在线」（panel 807）和各数据 panel 的说明；最后一行给出回退命令。
- 改看板的内容（不是只升级版本）时，在 `pack/…/grafana/dashboards/` 里改，出包，由用户在现场整套部署。部署前先用 `diff_panel1.py` 查现场是否偏离基线。

## 4. 加集群

与第 2 节相同：在 `clusters.txt` 填机器行、在 `boards_layout.txt` 加一行，`make_inventories.py build --cluster`，`panel1_cluster.sh add`，`arrange_boards.py apply`。同一参数再跑一次 `add` 即更新该集群。`panel1_cluster.sh list` 列出已登记的集群，`check <集群>` 只读核对。

## 5. 回退

| 回退什么 | 命令 |
|---|---|
| `panel1_upgrade.py apply` 改过的看板 | `python3 tools/panel1_upgrade.py undo <备份目录> --env-from <deploy.env>` |
| `arrange_boards.py apply` 移动过的看板和文件夹 | `python3 tools/arrange_boards.py undo <备份目录> --env-from <deploy.env>`（备份在 `~/vllm-pd-arrange-backup/<时间>/`） |
| 一个集群的整套面板一 | `bin/panel1_cluster.sh remove <集群>`：删两张看板、数据源、容器、数据卷、集群目录和注册表条目，Grafana 文件夹保留 |

## 6. 已知问题

- `diff_panel1.py` 的内嵌基线早于 v1.2：用它比较 v1.2 的看板会报约 190 处差异。比较线上看板前，要先用 v1.2 的 `grafana/dashboards/` 重新生成内嵌基线；生成脚本不在本仓库。
- `test_keep_blocks.py` 在本机运行时通过前几项后不再输出（60 秒超时），未查原因。

## 7. 照此新建一类面板

新建同类面板（例如 pypto 的看板）时，把本目录复制到仓库里的新目录，在新目录里改。部署方式可以沿用：每个集群一个 Prometheus、经 Grafana API 导入看板、由清单生成抓取配置。下面几项必须按此取值，否则会影响线上面板一：

| 项 | 取值 | 原因 |
|---|---|---|
| 端口 | 从 18200 起，每个集群占 10 个；在监控机上先确认空闲 | 18100 至 18190 已被 9 个集群占用；`names.py alloc` 从 18100 起分配，新面板不要用它分配 |
| 看板 UID、数据源 UID、容器名、数据卷名 | 不用 `vllm-pd-` 前缀，也不用 `prometheus-<集群>` 的形式；用新面板自己的前缀并带集群名 | UID 相同时导入会覆盖已有的看板或数据源 |
| 看板标题 | 不以「vLLM 服务侧监控面板」开头，也不与「vLLM 请求侧拆解面板」「面板二：请求时延拆解（P / D）」相同 | `panel1_upgrade.py` 按标题查找面板一看板，会把同前缀的看板一并升级；部署包按标题找看板 |
| 注册表 `/root/.vllm-pd-clusters.json` | 不往里写；确需登记时沿用同样的字段，并先报给用户 | 面板一、面板二的脚本和 `arrange_boards.py`（按注册表里的 UID 找看板）都读它，字段不合规时会报错或把看板放错文件夹 |
| Grafana 文件夹 | 用自己的文件夹，不写进 `boards_layout.txt` | `arrange_boards.py apply` 会整理对照表里的文件夹 |
| docker 网络 | 可以加入 `vllm-pd-monitor`，不删除、不重建 | Grafana 经这个网络访问全部数据源 |

其余共用资源（Grafana 账号的来源、provisioning 目录、监控机上的推理容器）见 [`../site/改动影响表.md`](../site/改动影响表.md) 表二。
