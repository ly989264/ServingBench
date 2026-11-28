# ServingBench：vLLM P/D 集群的监控面板

本仓库是内网 vLLM P/D 分离集群监控面板的维护仓库，记录内网环境的现状，并存放三套面板的源码：面板一（服务侧 Prefill / Decode 看板，Prometheus）、旧面板二（请求时延拆解，节点写文件、监控机拉取）、新面板二（请求时延拆解，采集进程 + 入库进程）。全部面板装在内网一台监控机上，共用一个 Grafana。

仓库里的改动不会自动到达内网：由用户出包，在内网执行。本仓库公开，现场的地址、目录、账号都不写进来（见 [`AGENTS.md`](AGENTS.md)）。

## 现状（2026-11-28）

| 面板 | 目录 | 集群 | 版本 | 状态 |
|---|---|---|---|---|
| 面板一 | [`panel1/`](panel1/) | glm53-0、glm53-1、glm53-2、dsv41-flash-1 … 5（8 个） | v1.2 | 线上在用 |
| 旧面板二 | [`panel2-v3/`](panel2-v3/) | glm53-1 | v3.4-extra-t53 | 线上在用，冻结 |
| 新面板二 | [`panel2-v4/`](panel2-v4/) | test-glm53（线下测试集群） | v4.0-extra | 已部署、待验收；本仓库内容的本地全链路运行（ClickHouse → 入库进程 → 看板）待验证 |
| 新面板二的采集接口（接口一） | [`interface/`](interface/) | test-glm53 的采集进程 | pd_diag_v1 | 随新面板二 |

内网的端口、目录、容器、看板和数据源清单见 [`site/内网现状.md`](site/内网现状.md)；每个目录改动后影响内网哪里，见 [`site/改动影响表.md`](site/改动影响表.md)。

## 负责人

| 目录 | 负责人 | 其他人 |
|---|---|---|
| `site/` | 用户 | 只读 |
| `interface/` | 用户 | 只读 |
| `panel1/` | 用户 | 新建同类面板的维护者只读，照此在新目录另建 |
| `panel2-v3/` | 用户（冻结） | 只读 |
| `panel2-v4/` | 新面板二的维护者 | 用户审阅、出包、在内网执行 |

## 阅读顺序

- **新面板二的维护者：** 本文 → [`site/内网现状.md`](site/内网现状.md) → [`site/改动影响表.md`](site/改动影响表.md) → [`interface/README.md`](interface/README.md) → [`panel2-v4/README.md`](panel2-v4/README.md) → [`panel2-v3/README.md`](panel2-v3/README.md)（只为了解线上现状）。
- **新建同类面板的维护者：** 本文 → [`site/内网现状.md`](site/内网现状.md) → [`site/改动影响表.md`](site/改动影响表.md) → [`panel1/README.md`](panel1/README.md)（最后一节「照此新建一类面板」）。

## 目录

| 目录 | 内容 |
|---|---|
| `site/` | 内网现状（文档与 YAML）、改动影响表、只读检测脚本 `check_site_state.py` 及其说明 |
| `interface/` | 接口一：采集进程对入库进程提供的 HTTP 接口规范、JSON Schema、样例、模拟采集进程、接口校验脚本 |
| `panel1/` | 面板一 v1.2 部署包源码、升级脚本、加集群和整理文件夹的工具、测试 |
| `panel2-v3/` | 旧面板二部署包（线上 glm53-1 的版本） |
| `panel2-v4/` | 新面板二的监控机侧：入库进程、ClickHouse 建表、看板生成、告警规则、接口二 |

历史版本（旧部署包、补丁、现场手册）不在当前目录中，见 git 历史和 GitHub Release。集群侧的补丁与本仓库无关，不放在这里。
