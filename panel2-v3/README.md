线上 glm53-1 在用。冻结：本目录不接受改动，缺陷报给用户。

# 旧面板二（v3.4-extra-t53）

本目录是线上 glm53-1 面板二的部署包内容，版本 `v3.4-extra-t53`，与现场集群目录 `/data/grafana/vllm-pd-grafana-panel2/glm53-1/` 的 `VERSION` 相同。内容取自 T53 两步包 pkg26 中的面板包（现场由 pkg25 加快捷时间范围改法 `t53qrange.py` 装成，看板内容与 pkg26 相同）：与上一版 v3.4-extra（部署包 `b883692a`）相比有三处不同：

1. `VERSION` 为 `v3.4-extra-t53`。
2. `scripts/generate.py` 在生成的看板里写入快捷时间范围：第 1 项「今天 00:00–次日 00:00」，其后为 Grafana 12.4.2 自带的 36 项。
3. 新增 `deploy.env` 键 `BOARD_OMIT_SEGMENTS`（只从看板去掉指定的对照行，采集和入库不变），`GRAFANA_BOARD_TITLE` 填了就照用、不加后缀；对应 `deploy.env.example` 的两处说明和 `scripts/generate.py` 的 `board_tree()`。

`MANIFEST.txt`、`SHA256SUMS` 是 pkg26 面板包的原件。`offline/`（ClickHouse、Vector 镜像，ClickHouse 插件，Python 3.12.16 的离线安装文件）没有放进仓库，由 `bin/fetch_offline.sh` 在能上网的机器上下载；它们的版本：ClickHouse 26.8.36.3，Vector 0.61.0-debian，grafana-clickhouse-datasource 4.23.4，Python 3.12.16。

部署包自身的说明在 [`vllm-pd-grafana-panel2/README.md`](vllm-pd-grafana-panel2/README.md)（标题写的是 v2.1，正文到 v3.4-extra 一直沿用）；诊断工具的说明在 `vllm-pd-grafana-panel2/docs/诊断工具说明.zh-CN.md`。

## 1. 数据通路

```
glm53-1 每台 P、D 机器                                   监控机
  vLLM（打了诊断补丁）                                     拉取程序 puller.py（bash while 循环维护）
    └ 节点过滤器 pd_diag_logfilter.py（随 vLLM 进程加载）       └ SSH + rsync，默认每 10 秒拉一次 → /data/clickhouse-glm53-1/pull
        按 segments.json 写诊断文件到 /data/pd-diag/logs           Vector 容器读取、解析 → ClickHouse（库 vllm_pd）
                                                                       ← Grafana 看板「vLLM 请求侧拆解面板」
```

- vLLM 的诊断补丁写出 `[PD_DIAG]` 记录；节点过滤器（`node/pd_diag_logfilter.py`、`node/pd_diag_logging.json`，与 `segments.json` 一起放在节点的 `/data/pd-diag/conf`）在 vLLM 进程里把记录按段定义写成文件。
- 监控机上的拉取程序经 SSH 和 rsync 把各节点的文件拉到监控机，Vector 解析后写进 ClickHouse 的 `phase_line`、`diag_event`、`proxy_line` 等表；看板读视图 `phase_line_cur`。
- 告警借用面板一的 Prometheus `vllm-pd-prometheus-glm53-1`：规则写在它的 `prometheus.yml` 的标记段里。
- 看板上的段严格等于用户的 L1/L2/L3 名单（`profiles/glm53/segments.json`），只显示日志里有的内容。

## 2. 在 glm53-1 上的部署事实

| 项 | 值 |
|---|---|
| 集群目录 | `/data/grafana/vllm-pd-grafana-panel2/glm53-1/`；`deploy.env` 只写了集群名、端口基数、数据目录、网络、节点目录等，容器名、看板 UID、端口由 `scripts/names.py` 的默认规则推出 |
| ClickHouse | 容器 `vllm-pd-clickhouse-glm53-1`，端口 18100->8123，数据目录 `/data/clickhouse-glm53-1` |
| Vector | 容器 `vllm-pd-vector-glm53-1`，无主机端口，与 ClickHouse 共用数据目录（`vector/`、`pull/`） |
| 拉取程序 | `scripts/puller.py`，pid 文件 `/data/grafana/vllm-timing/glm53-1/puller/puller.pid`；指标端口 18101 |
| 看板 | UID `vllm-pd-request-glm53-1`，标题「vLLM 请求侧拆解面板」，位于 `GLM-5.3/glm53-1` |
| 数据源 | 名称「vLLM ClickHouse（glm53-1）」，UID `vllm-pd-clickhouse-glm53-1` |
| 节点 | 12 台 P、D 机器，容器 `vllm-ascend-glm53-v30`；节点目录 `/data/pd-diag/conf`、`/data/pd-diag/logs`；`PD_DIAG_LEVEL` 未设置 |
| 数据量（11-28） | `phase_line` 约 295 万行、`diag_event` 约 146 万行，TTL 30 天和 15 天 |

完整清单见 [`../site/内网现状.md`](../site/内网现状.md) 第 3.2 节。

## 3. 与新方案（`panel2-v4/`）的差别

| 项 | 旧方案（本目录，glm53-1） | 新方案（`panel2-v4/`，test-glm53） |
|---|---|---|
| 节点上 | vLLM 进程里的过滤器写诊断文件到节点磁盘 | 节点不写诊断文件；记录留在 vLLM 实例内存里 |
| 数据到监控机 | 监控机拉取程序经 SSH + rsync 拉文件，Vector 解析 | proxy 机器上的采集进程（端口 9300）合成每个请求一份文档，监控机上的入库进程经 HTTP（接口一，见 `interface/`）读取 |
| ClickHouse 表 | `phase_line`、`diag_event`、`proxy_line` 等，看板读视图 `phase_line_cur` | `request_doc`、`status_doc`、`segment_def`、`db_interface` |
| 段定义 | 包内 `segments.json`，下发到节点 | 经接口一的段定义接口到达，看板不自行加段 |
| 告警 | 借用面板一的 Prometheus | 自己的只告警 Prometheus（端口 18192） |
| 状态 | 线上在用 | 已部署、待验收 |

## 4. 修改

本目录冻结。发现缺陷时报给用户，不在这里改；新功能做在 `panel2-v4/`。
