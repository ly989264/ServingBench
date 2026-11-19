# ServingBench：vLLM P/D 监控面板

本仓库是两套内网监控面板的维护资料：现在的实现、部署结构和相关信息，供接手的同事和他的 Claude Code 读取。

- **面板一**：服务侧看板。每台 P/D 机器上的 exporter → 每个集群一个 Prometheus → 共用 Grafana 上的 Prefill、Decode 两张看板。在用 8 套。
- **面板二**：请求时延拆解。补丁在 vLLM 中写 `[PD_DIAG]` 日志 → 节点上的过滤器按段定义过滤 → 监控机经 SSH 拉取 → Vector → ClickHouse → Grafana。按用户的 L1/L2/L3 名单分 47 段。在用 2 套（glm53-1 为 mock 模式，test-glm53 为 vllm 模式）。

所有地址、主机名和用户目录都是占位符或示例值（10.1.x.x）；实验室地址（10.98.x.x 等）会注明。内网与开发侧本地实验室镜像的一致目前由用户本人保障，本仓库不提供程序上的证明。

## 阅读顺序

1. [CLAUDE.md](CLAUDE.md)：不能改的规则和事实索引（一屏）
2. [docs/00-术语表.md](docs/00-术语表.md)
3. [docs/01-全景.md](docs/01-全景.md)：机器、组件、数据流
4. [docs/02-内网现状.md](docs/02-内网现状.md)（机器可读版 [state/内网现状.yaml](state/内网现状.yaml)）
5. [docs/10-面板一.md](docs/10-面板一.md)、[docs/20-面板二.md](docs/20-面板二.md)
6. [docs/40-规则.md](docs/40-规则.md)
7. [docs/30-变更与发布.md](docs/30-变更与发布.md)
8. [docs/50-已知问题与待办.md](docs/50-已知问题与待办.md)
9. [docs/60-文件地图.md](docs/60-文件地图.md)
10. 需要细节时：[docs/面板系统说明书.zh-CN.md](docs/面板系统说明书.zh-CN.md)（面板二 v3.1-extra 的完整说明书，2071 行）、[manuals/README.md](manuals/README.md)（在用手册）

## 目录

| 目录 | 内容 |
|---|---|
| `docs/` | 上面的说明文档；`面板系统说明书.zh-CN.md` 为交付原件 |
| `state/` | 内网现状的 yaml |
| `contracts/` | 《记录格式约定》glm5.3 版 1.3.3（补丁与面板二之间的接口）和 `[PD_DIAG]` 样例 |
| `panel1/` | 面板一部署包源码（`pack/`）、打包脚本（`tools/build_pack.sh`）、离线测试 |
| `panel2/` | 面板二部署包源码、更新脚本、诊断工具、手册源文件、离线测试 |
| `patches/` | 补丁一侧：glm5.3 的 vLLM、vllm-ascend、MooncakeConnectorV1 诊断补丁，环境检测与现场核对脚本，Mooncake 替换库的构建；dsv4.1 版已暂停 |
| `tools/` | `arrange_boards.py`、`make_inventories.py`、`panels_snapshot.py`、面板一的 `probe/diff/diag_panel1.py` |
| `manuals/` | 在用手册索引和不在其他目录的手册（占位符版） |
| `releases/` | 每个在用部署包的 sha256；包本身在 GitHub Release |
| `local/` | 只在本地、不进 git：现场值、已填路径版手册与脚本、Release 资产的本地副本 |

## 说明

- **面板二源码和 `patches/` 都没有带开发仓库的 git 历史**：历史中有现场值（用户目录、内网地址、本机路径），只导入了当前文件；`patches/` 另去掉了测试输出、现场带回的输出和二进制，见 [patches/README.md](patches/README.md)。本仓库的前两个提交分别是 v3 和 v3.1-extra 的源码，二者的差异可直接 `git diff`。面板一的开发目录本来没有 git。
- **大文件放 GitHub Release，不用 Git LFS**：免费账户的 LFS 额度为存储 1 GiB、流量每月 1 GiB，面板二离线包一个就有 398 MB；Release 资产单个上限 2 GiB。tag、资产和发布命令见 [releases/README.md](releases/README.md)。
- 不在本仓库：本地实验室镜像本身和带密钥的实验室镜像、历史包与旧源码、已填路径版、任何现场值；T51 的 proxy 补丁尚未交付。清单见 [docs/50-已知问题与待办.md](docs/50-已知问题与待办.md)。
