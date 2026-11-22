# 面板一、面板二维护仓库

面板一：服务侧 Prefill / Decode 看板，每集群一个 Prometheus。面板二：请求时延拆解（L1/L2/L3 段），节点过滤 → 监控机拉取 → Vector → ClickHouse → Grafana。两者共用一个 Grafana。

## 不能改的规则（用户的决定，原因见 docs/40-规则.md）

- 面板二的段严格等于用户的 L1/L2/L3 名单，不增不减；增删只由用户决定。包根目录与 dsv4.1 段定义 47 段；glm5.3 段定义 v3.2-extra 起 56 段和 3 行对照（T56 用户加的 D 侧 9 段），v3.4-extra 的 else 行和标题行由生成脚本加，不是段。
- 面板二只显示日志里有的内容，不推断、不补算。
- 任何 add、update、rollback 之后执行一次 `tools/arrange_boards.py apply`：两个部署包都只认顶层文件夹，会把看板挪回去。
- 面板一出包只用 `NO_EXPORT=1 tools/build_pack.sh`（在 `panel1/` 下）；看板的真源是包内 `grafana/dashboards/` 的合并基线。
- 补丁（`patches/`）必须在检测到的目标环境上验证，不得带入其他版本的残留。
- 升级只按升级安全设闸（版本、配置、容器、看板、数据在流入）；mock 和测试数据的准确性不设闸。
- 现场值（内网地址、主机名、用户目录、口令）不进 git，只放 `local/`；示例地址只用 10.1.x.x。本仓库公开，提交前检查全部改动（含历史）没有上述内容，也没有本机路径、邮箱、SSH 密钥。

## 去哪里找事实

| 问题 | 位置 |
|---|---|
| 叫法 | `docs/00-术语表.md` |
| 内网有哪些机器、数据怎么走 | `docs/01-全景.md` |
| 每个集群的现状（包版本、端口、目录、模式），已交付未执行的变更 | `docs/02-内网现状.md`（第 11 节）、`state/内网现状.yaml`（`pending_changes`） |
| 面板一 / 面板二的组成、配置、加集群、出包 | `docs/10-面板一.md`、`docs/20-面板二.md`；面板二最详细的是 `docs/面板系统说明书.zh-CN.md` |
| 补丁写出的记录格式（面板二按它解析） | `contracts/` |
| 补丁源码、检测脚本、已交付的打补丁包 | `patches/README.md` |
| glm53-1 线上升级驱动（面板二接入 + 整集群打补丁，一个脚本） | `drive/t53-drive/`；手册 `manuals/glm53-1线上/` |
| 变更怎么做、怎么发布 | `docs/30-变更与发布.md`、`releases/README.md` |
| 已知问题、待办、只在本地的文件 | `docs/50-已知问题与待办.md` |
| 某个概念在哪个文件实现 | `docs/60-文件地图.md` |
| 内网操作手册 | `manuals/README.md`；test-glm53 的 T51 上线从 `manuals/执行顺序总表.zh-CN.md` 开始，之后的升级在 `manuals/glm53-test升级/`、`manuals/面板二/` |
