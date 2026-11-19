# 在用手册索引

只列现在仍适用的手册，全部为占位符版（`<用户目录>`、`<ROOT>` 等由执行者换成现场值）。已填路径版不进本仓库：面板二的已填版由 `panel2/docs/v3/fill_guide.py`（需要环境变量 `USER_DIR`）或 `panel2/docs/v3.1-extra/fill_guide_b.py`（从已填路径的更新脚本读取用户目录）生成；面板一的说明书没有生成脚本，把 `<用户目录>` 全部替换即可。

手册的源文件留在原处（`panel2/docs/` 为开发源文件，`tests/t41/pack_dist.sh` 按这些路径出交付目录），本索引不复制。

| 要做的事 | 手册 | 格式 | 状态 |
|---|---|---|---|
| 新增、删除、改一套面板一集群 | [面板一/面板一加集群说明书.zh-CN.md](面板一/面板一加集群说明书.zh-CN.md)（第 1–3 节加集群，第 4 节删除，第 5 节改机器） | 旧式说明书 | 在用。原交付只有已填路径版，本文件为由它还原的占位符版 |
| 面板一探测、与基线比对和合并 | [../tools/panel1/PROBE_PANEL1.zh-CN.md](../tools/panel1/PROBE_PANEL1.zh-CN.md)、[../tools/panel1/DIFF_PANEL1.zh-CN.md](../tools/panel1/DIFF_PANEL1.zh-CN.md) | 工具说明 | 在用 |
| 删除面板二集群并在同一批机器上重装 | [../panel2/docs/v3.1-extra/面板二删除test安装test-glm53操作手册.zh-CN.md](../panel2/docs/v3.1-extra/面板二删除test安装test-glm53操作手册.zh-CN.md)；交付说明 [面板二/交付说明_面板手册.zh-CN.md](面板二/交付说明_面板手册.zh-CN.md) | 线性手册（步骤 1..22，每步四块） | T51 上线时执行；本仓库按执行完以后的状态描述内网（见 `docs/02-内网现状.md`） |
| 新增一个 vllm 模式的面板二集群 | [../panel2/docs/v3.1-extra/新增test-glm53集群操作说明.zh-CN.md](../panel2/docs/v3.1-extra/新增test-glm53集群操作说明.zh-CN.md)；包的交付说明 [面板二/交付说明_面板包e3885b93.zh-CN.md](面板二/交付说明_面板包e3885b93.zh-CN.md) | 旧式说明书 | 在用 |
| 面板二从 v2.1 升级到 v3（glm53-1 已执行；回滚步骤在其中） | [../panel2/docs/v3/内网升级总说明.zh-CN.md](../panel2/docs/v3/内网升级总说明.zh-CN.md) | 旧式说明书 | 已执行，作为 glm53-1 当前版本与回滚的依据 |
| 面板二日常操作、机器重启以后、排障 | [../docs/面板系统说明书.zh-CN.md](../docs/面板系统说明书.zh-CN.md) 第 9、12、15 节 | 说明书 | 在用 |
| 面板二独立诊断 | [../panel2/diag-kit/README.zh-CN.md](../panel2/diag-kit/README.zh-CN.md) | 工具说明 | 在用 |
| 看板两层文件夹整理 | [../tools/README.md](../tools/README.md)（`arrange_boards.py`）；原《动现有看板操作单》已执行完，未带入 | — | 每次 add / update / rollback 之后执行 `apply` |

缺口（没有线性手册，需要时按线性格式另写）：

| 要做的事 | 现状 |
|---|---|
| 面板一部署包升级到新版本（8 套逐套重跑 `panel1_cluster.sh add`，再执行 `arrange_boards.py apply`） | 只有《面板一加集群说明书》第 5 节的要点，部分覆盖 |
| 本地实验室镜像按交付命令同步 | 没有手册，只有历次同步记录（未带入） |
| 线上 glm53-1 接入真实 vLLM（打补丁、面板二改 vllm 模式） | 没有手册 |

不在本仓库的手册：glm5.3 集群打补丁手册与《操作说明 A》、Mooncake 增量包说明（补丁一侧，留在私有交付仓库）；已执行完的一次性操作单（面板一二新装、停用旧面板一、动现有看板）；适用对象已删除的《操作说明 B》《test 集群操作总说明》。
