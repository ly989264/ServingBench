# 给 agent 的指引

本仓库维护内网 vLLM P/D 集群的监控面板：面板一（`panel1/`）、旧面板二（`panel2-v3/`）、新面板二（`panel2-v4/`）及其采集接口（`interface/`），并记录内网现状（`site/`）。仓库公开。

| 目录 | 对应内网 | 能否修改 |
|---|---|---|
| `interface/` | test-glm53 采集进程与入库进程之间的接口一 | 不修改，只由用户改 |
| `panel1/` | 8 个线上集群的面板一 | 不修改；新建同类面板复制到新目录 |
| `panel2-v3/` | 线上 glm53-1 的面板二 | 冻结，不修改 |
| `panel2-v4/` | test-glm53 的面板二（已部署、待验收） | 新面板二的维护者可改 |
| `site/` | 无（现状记录） | 只由用户按现场检测结果更新 |

## 不能改的规则

1. 不修改 `panel1/`、`panel2-v3/`、`interface/`；发现缺陷报给用户。
2. 面板二的段严格等于用户的 L1/L2/L3 名单；段的增减只经接口一到达，看板不自行加段。
3. 只显示数据里有的内容：不推断、不分摊、不补算，没有的段留空。
4. 段名单、显示项和范围交给用户决定；实现上的取舍写进对应目录的 `README.md`。
5. 对已登记集群做 add、update、rollback 之后，每次都执行 `panel1/tools/arrange_boards.py apply`。
6. 升级脚本只按升级安全设闸，不按测试数据或模拟数据的准确性设闸。
7. 看板：图例放在图下方；一个实例下拉框，名称 P0..Pn、D0..Dm；P 看板不设 D 过滤，D 看板不设 P 过滤；不放解释性文字。
8. ClickHouse 内存上限 `CLICKHOUSE_MAX_SERVER_MEMORY_MB` 不低于 1500。
9. 现场值（内网地址、主机名、个人目录、工号、口令）不写进仓库；新写的示例地址只用 10.1.x.x（`panel1/`、`panel2-v3/` 里交付件原有的实验室地址保持原样）。提交前检查改动里没有这些内容，也没有本机路径和邮箱。
10. 不执行 `docker system prune`，不重启 docker：监控机上运行着推理容器。

## 阅读顺序

- 新面板二的维护者：`README.md` → `site/内网现状.md` → `site/改动影响表.md` → `interface/README.md` → `panel2-v4/README.md` → `panel2-v3/README.md`（只为了解线上现状）。
- 新建同类面板的维护者：`README.md` → `site/内网现状.md` → `site/改动影响表.md` → `panel1/README.md`。

## 指向

- 改任何文件前先查 `site/改动影响表.md`。
- 接口变更流程见 `interface/README.md`。
- 现场真实值到现场查，查法见 `site/内网现状.md` 附录 A。

仓库里的改动不会自动到达内网，由用户出包并在现场执行。
