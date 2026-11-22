# 在用手册索引

**glm5.3 测试集群 test-glm53 的 T51 上线（面板、打补丁、proxy、Mooncake）从 [执行顺序总表.zh-CN.md](执行顺序总表.zh-CN.md) 开始**（85f1b78e，替换 a47f9dfa：proxy 改为现场改文件的做法）：它按行排出三份手册各步的先后、每行做完用哪一步核对；各步的命令都在对应的手册里。T51 以后的升级各有一份手册，见下表；现在 test-glm53 在 v3.3-extra，下一步是 v3.4-extra，然后是 glm53-1 的线上升级驱动。

只列现在仍适用的手册，全部为占位符版（`<用户目录>`、`<ROOT>` 等由执行者换成现场值）。已填路径版不进本仓库：面板二的已填版由 `panel2/docs/v3/fill_guide.py`（需要环境变量 `USER_DIR`）或 `panel2/docs/v3.1-extra/fill_guide_b.py`（从已填路径的更新脚本读取用户目录）生成；面板一的说明书没有生成脚本，把 `<用户目录>` 全部替换即可。

面板二手册的源文件在 `panel2/docs/`（`tests/t41/pack_dist.sh` 按这些路径出交付目录），打补丁手册的源文件在 `patches/glm53/t51-glm53-kit/manual/`；T51 的《执行顺序总表》和三份线性手册另在本目录放了交付原件。

| 要做的事 | 手册 | 格式 | 状态 |
|---|---|---|---|
| 新增、删除、改一套面板一集群 | [面板一/面板一加集群说明书.zh-CN.md](面板一/面板一加集群说明书.zh-CN.md)（第 1–3 节加集群，第 4 节删除，第 5 节改机器） | 旧式说明书 | 在用。原交付只有已填路径版，本文件为由它还原的占位符版 |
| 面板一探测、与基线比对和合并 | [../tools/panel1/PROBE_PANEL1.zh-CN.md](../tools/panel1/PROBE_PANEL1.zh-CN.md)、[../tools/panel1/DIFF_PANEL1.zh-CN.md](../tools/panel1/DIFF_PANEL1.zh-CN.md) | 工具说明 | 在用 |
| 删除面板二集群并在同一批机器上重装 | [面板二/面板二删除test安装test-glm53操作手册.zh-CN.md](面板二/面板二删除test安装test-glm53操作手册.zh-CN.md)（30c67f67，交付原件，替换 c9b9d854；源文件在 `panel2/docs/v3.1-extra/`）；交付说明 [面板二/交付说明_面板手册.zh-CN.md](面板二/交付说明_面板手册.zh-CN.md) | 线性手册（步骤 1..22，每步四块） | T51 上线时执行；本仓库按执行完以后的状态描述内网（见 `docs/02-内网现状.md`） |
| 新增一个 vllm 模式的面板二集群 | [../panel2/docs/v3.1-extra/新增test-glm53集群操作说明.zh-CN.md](../panel2/docs/v3.1-extra/新增test-glm53集群操作说明.zh-CN.md)；包的交付说明 [面板二/交付说明_面板包e3885b93.zh-CN.md](面板二/交付说明_面板包e3885b93.zh-CN.md) | 旧式说明书 | 在用 |
| 面板二从 v2.1 升级到 v3（glm53-1 已执行；回滚步骤在其中） | [../panel2/docs/v3/内网升级总说明.zh-CN.md](../panel2/docs/v3/内网升级总说明.zh-CN.md) | 旧式说明书 | 已执行，作为 glm53-1 当前版本与回滚的依据 |
| 面板二日常操作、机器重启以后、排障 | [../docs/面板系统说明书.zh-CN.md](../docs/面板系统说明书.zh-CN.md) 第 9、12、15 节 | 说明书 | 在用 |
| glm5.3 测试集群的 P、D 容器打补丁 | [补丁/glm5.3测试集群打补丁操作手册.zh-CN.md](补丁/glm5.3测试集群打补丁操作手册.zh-CN.md)（5cb9fe7f，交付原件，替换 48c88aa7、857bbec4：附录 B 补记测试集群 Mooncake 的 `.comment` 例外；底层为打补丁包内的《操作说明 A》）；交付说明 [补丁/交付说明_打补丁手册.zh-CN.md](补丁/交付说明_打补丁手册.zh-CN.md) | 线性手册 | T51 上线时执行；包见 `patches/README.md` |
| glm5.3 测试集群的 proxy 打补丁 | [补丁/glm5.3测试集群proxy打补丁操作手册.zh-CN.md](补丁/glm5.3测试集群proxy打补丁操作手册.zh-CN.md)（ce5ddf80，交付原件，替换 3925bbc7：现场改文件的做法；源文件在 `panel2/docs/v3.1-extra/`）；交付说明 [补丁/交付说明_proxy手册与执行顺序总表.zh-CN.md](补丁/交付说明_proxy手册与执行顺序总表.zh-CN.md) | 线性手册 | T51 上线时执行；包见 `patches/README.md` |
| test-glm53 从 v3.1-extra 升到 v3.2-extra（T56：面板二、vLLM / vllm-ascend / p2d 补丁，整集群停一次） | [glm53-test升级/glm53-test_v3.2-extra升级操作手册.zh-CN.md](glm53-test升级/glm53-test_v3.2-extra升级操作手册.zh-CN.md)（cdd33713，交付原件）；执行顺序 [glm53-test升级/执行顺序_v3.2-extra.zh-CN.md](glm53-test升级/执行顺序_v3.2-extra.zh-CN.md)；补丁包说明 [glm53-test升级/T56补丁包说明.zh-CN.md](glm53-test升级/T56补丁包说明.zh-CN.md)；交付说明 [glm53-test升级/交付说明_v3.2-extra升级手册.zh-CN.md](glm53-test升级/交付说明_v3.2-extra升级手册.zh-CN.md)、[面板二/交付说明_面板包d77528d4.zh-CN.md](面板二/交付说明_面板包d77528d4.zh-CN.md) | 线性手册 | 已执行（T56） |
| test-glm53 从 v3.2-extra 升到 v3.3-extra（T58：Mooncake 计时写自己的文件、`PD_DIAG_LEVEL`、Mooncake 替换库 v2，整集群停一次，回滚顺序固定） | [glm53-test升级/glm53-test_v3.3-extra升级操作手册.zh-CN.md](glm53-test升级/glm53-test_v3.3-extra升级操作手册.zh-CN.md)（f9abc406，43 步，交付原件）；交付说明 [glm53-test升级/交付说明_v3.3-extra升级手册.zh-CN.md](glm53-test升级/交付说明_v3.3-extra升级手册.zh-CN.md)、[面板二/交付说明_面板包d071644c.zh-CN.md](面板二/交付说明_面板包d071644c.zh-CN.md)；第八部分的查询 [glm53-test升级/第一步开销分析_v3.3-extra.sql](glm53-test升级/第一步开销分析_v3.3-extra.sql) | 线性手册 | 已执行（T58，用户 2026-11-21 报做完） |
| test-glm53 面板二从 v3.3-extra 更新到 v3.4-extra，同时删 glm53-1 的 mock 面板二、把看板挪到顶层 `GLM-5.3-test`（不停集群） | [面板二/glm53-test面板二更新到v3.4-extra操作手册.zh-CN.md](面板二/glm53-test面板二更新到v3.4-extra操作手册.zh-CN.md)（8 步；交付的只有已填路径版 512aadea，本文件是由它还原的占位符版）；现场命令 [面板二/v3.4-extra现场命令/](面板二/v3.4-extra现场命令/)；交付说明 [面板二/交付说明_面板包b883692a.zh-CN.md](面板二/交付说明_面板包b883692a.zh-CN.md) | 线性手册 | 已交付，未执行 |
| glm53-1（线上 12 台）面板二接入与整集群打补丁：监控机上一个驱动脚本，先在测试集群演练 | [glm53-1线上/glm53-1面板二接入与整集群打补丁_一页手册.zh-CN.md](glm53-1线上/glm53-1面板二接入与整集群打补丁_一页手册.zh-CN.md)（e5e7e69c，交付原件）；交付说明 [glm53-1线上/交付说明_线上升级驱动.zh-CN.md](glm53-1线上/交付说明_线上升级驱动.zh-CN.md)；脚本 `../drive/t53-drive/` | 一页手册 + 脚本 | 已交付，未执行；在 v3.4-extra 之后 |
| 面板二独立诊断 | [../panel2/diag-kit/README.zh-CN.md](../panel2/diag-kit/README.zh-CN.md)；v3.4-extra 的只读诊断工具 [../panel2/diag-tool/使用说明.zh-CN.md](../panel2/diag-tool/使用说明.zh-CN.md) | 工具说明 | 在用 |
| 看板两层文件夹整理 | [../tools/README.md](../tools/README.md)（`arrange_boards.py`）；原《动现有看板操作单》已执行完，未带入 | — | 每次 add / update / rollback 之后执行 `apply` |

缺口（没有线性手册，需要时按线性格式另写）：

| 要做的事 | 现状 |
|---|---|
| 面板一部署包升级到新版本（8 套逐套重跑 `panel1_cluster.sh add`，再执行 `arrange_boards.py apply`） | 只有《面板一加集群说明书》第 5 节的要点，部分覆盖 |
| 本地实验室镜像按交付命令同步 | 没有手册，只有历次同步记录（未带入） |

不在本目录的说明：《操作说明 A》在打补丁包内（源文件 `patches/glm53/t51-glm53-patchkit/guides/`），打补丁包与 Mooncake 增量包的交付说明在 `releases/glm53-patch-kit-d21cc319/`、`releases/glm53-mooncake-kit-6d70978c/`。不在本仓库的手册：已执行完的一次性操作单（面板一二新装、停用旧面板一、动现有看板）；适用对象已删除的《操作说明 B》《test 集群操作总说明》。
