# glm53-test 面板二更新到 v3.4-extra 操作手册

<!-- 占位符版开始 -->
> **本手册有两版。** 这一版是占位符版：`<用户目录>` 是监控机上装面板的账号的个人目录（`/data/` 下），`<总目录>` 是面板的总目录；其余内容与交付的已填路径版（512aadea，只放私有 Caching 仓库）逐字相同。手册里的 `vllm_pd_update_pack2_v34extra_filled.py` 是把占位符版更新脚本 `面板二v3.4-extra/vllm_pd_update_pack2_v34extra.py` 的 `USER_DIR` 一行改成 `<用户目录>` 以后的副本。
<!-- 占位符版结束 -->

在 glm53-test 的监控机上，用装面板的账号，从上到下做 8 步。不停集群，不重启 vLLM，面板一不动。实验室照这份手册的命令走过一遍，第 2 步到第 7 步共约 1 分钟，第 8 步要先等 3 分钟。

**这次改了什么：**
- test-glm53 的面板二看板：
  - P、D 两张分布表在合计正上方各加一行 else；
  - 均值占比改成「这一段的耗时之和 ÷ 合计之和」；
  - P 合计算上 P/D LB proxy；
  - P 表加标题行「后台任务」，D 表加标题行「第一步」「第二步起 · 每步」；
  - 段名去掉括号，「等 put 完成」改名为 wait_for_put；
  - 「有值 / 请求」列默认隐藏，一条命令就能显示出来。
- 只有 D 记录的请求原来不进表，现在进表。
- glm53-1 的 mock 面板二删掉，同时删掉它在 12 台机器上留下的 `~/vllm-timing` 和空的 `~/vllm-pd-diag`。
- test-glm53 的面板二看板挪到顶层文件夹 GLM-5.3-test。

**几个词：**
- **else：** 每个请求的本侧合计减去表里各一级段（空的按 0 算）。小于 0 时记 0。这个请求在本侧没有 `core_summary[request]` 记录时，else 为空。
- **均值占比：** 这一段在窗口内所有请求上的耗时之和（空的按 0 算），除以这些请求的合计之和。各一级段的均值占比加上 else 的，不一定是 1：时间段里有请求缺 `core_summary[request]` 记录时，相加偏小；有请求的 else 小于 0、记为 0 时，相加偏大。
- **标题行：** 只是分组的名字，没有数字，格子都是空白的。

**路径：** 下面的命令开头都 `. ~/vllm-pd-test-glm53/vars.sh`。它定义了 `UD=<用户目录>`、`ROOT=<总目录>`、`C=test-glm53`、`D=<总目录>/vllm-pd-grafana-panel2/test-glm53`、`T45=<用户目录>/vllm-pd-boards-clusters`，第 3 步以后还有 `ENV2`。这次的文件都放在 `<用户目录>/vllm-pd-panel2-update-v3.4-extra`。

| 步 | 做什么 | 能不能退回 |
|---|---|---|
| 1 | 放文件，核对 sha256 | — |
| 2 | 只读核对（删 mock、挪文件夹之前看一眼） | 只读 |
| 3 | `vars.sh` 里的 `ENV2` 改指 test-glm53 自己的 `deploy.env` | 删掉加的那一行 |
| 4 | 删 glm53-1 的 mock 面板二，删节点上留下的目录 | 不能退回 |
| 5 | 换整理命令的两个文件，把看板挪到顶层 GLM-5.3-test | `undo` |
| 6 | 面板二更新到 v3.4-extra | `rollback` |
| 7 | 再整理一次看板（应当没有要改的） | — |
| 8 | 核对 | — |

第 5 步放在第 6 步前面：更新导入看板的时候，看板已经在最终的位置。

---

## 第 1 步：放文件，核对 sha256

**目的：** 把这次的文件放到监控机上，确认拷得完整。

把交付目录里的这些文件拷到监控机的 `<用户目录>/vllm-pd-panel2-update-v3.4-extra/`，按下面的位置放：

| 监控机上的位置（`<用户目录>/vllm-pd-panel2-update-v3.4-extra/` 下） | 交付目录里的文件 |
|---|---|
| `vllm-pd-grafana-panel2.tar.gz`、`vllm-pd-grafana-panel2.tar.gz.sha256` | `面板二v3.4-extra/` 下的同名文件 |
| `vllm_pd_update_pack2_v34extra_filled.py` | `已填路径-只放私有Caching/` 下的同名文件 |
| `01e_现场只读核对_删mock与挪文件夹.sh`、`01f_删节点上的vllm-timing.sh` | `现场命令/` 下的同名文件 |
| `整理命令/arrange_boards.py`、`整理命令/boards_layout.txt` | `整理命令/` 下的同名文件 |
| `诊断工具/pd_panel2_diag.py` | `诊断工具/` 下的同名文件（可选，第 8 步用） |

```bash
. ~/vllm-pd-test-glm53/vars.sh && N=$UD/vllm-pd-panel2-update-v3.4-extra && cd "$N" && sha256sum -c vllm-pd-grafana-panel2.tar.gz.sha256 && sha256sum vllm_pd_update_pack2_v34extra_filled.py 01e_*.sh 01f_*.sh 整理命令/* | awk '{print substr($1, 1, 8), $2}'
```

应当看到：

```
vllm-pd-grafana-panel2.tar.gz: OK
dd76d133 vllm_pd_update_pack2_v34extra_filled.py
276929ef 01e_现场只读核对_删mock与挪文件夹.sh
7d475a07 01f_删节点上的vllm-timing.sh
0d3d4ec7 整理命令/arrange_boards.py
da33f3ac 整理命令/boards_layout.txt
```

---

## 第 2 步：只读核对

**目的：** 删 glm53-1 的 mock 面板二、挪文件夹之前，确认现场和 11-21 带回的样子一致。只读文件和列目录，不改任何东西。

```bash
. ~/vllm-pd-test-glm53/vars.sh && cd $UD/vllm-pd-panel2-update-v3.4-extra && bash 01e_现场只读核对_删mock与挪文件夹.sh 2>&1 | tail -n 3
```

应当看到：

```
== 结论
删 glm53-1 的 mock 面板二：和讨论稿附录 E 的预期一致，可以执行
挪文件夹：和预期一致
```

不是这两行时停下，把整个输出带回来。

---

## 第 3 步：`ENV2` 改指 test-glm53 自己的 `deploy.env`

**目的：** `ENV2` 现在指向 glm53-1 面板二的 `deploy.env`，整理看板的命令靠它取 Grafana 账号。第 4 步删了这个文件以后，这些命令就跑不了了。test-glm53 的 Grafana 设置和 glm53-1 的相同，第 2 步已经核过。

```bash
echo 'ENV2=$D/deploy.env' >> ~/vllm-pd-test-glm53/vars.sh && . ~/vllm-pd-test-glm53/vars.sh && echo "ENV2=$ENV2"
```

应当看到：

```
ENV2=<总目录>/vllm-pd-grafana-panel2/test-glm53/deploy.env
```

---

## 第 4 步：删 glm53-1 的 mock 面板二

**目的：** 删掉 glm53-1 的 mock 面板二：监控机上的看板、数据源、容器、数据和注册表，以及 12 台机器上的 mock 和它写的文件。然后删掉 mock 在 12 台机器上留下的 `~/vllm-timing` 和空的 `~/vllm-pd-diag`。vLLM、面板一、共用的 Grafana 都不动。**这一步不能退回。**

分三条，按顺序执行。

① 记下 glm53-1 的机器清单和 SSH 设置（② 会删掉 glm53-1 的 `deploy.env`）。只读本机文件：

```bash
. ~/vllm-pd-test-glm53/vars.sh && cd $UD/vllm-pd-panel2-update-v3.4-extra && bash 01f_删节点上的vllm-timing.sh save
```

应当看到：

```
已记下 12 台机器和 SSH 设置：<家目录>/vllm-pd-test-glm53/glm53-1-nodes.sh
```

② 删 glm53-1 的面板二：

```bash
. ~/vllm-pd-test-glm53/vars.sh && cd "$ROOT/vllm-pd-grafana-panel2/glm53-1" && bin/panel2_cluster.sh remove glm53-1 2>&1 | tee ~/vllm-pd-test-glm53/remove-glm53-1.log | tail -n 4
```

应当看到（最后一行是这个就行，前面几行实验室和现场会有出入）：

```
已从注册表删除集群 glm53-1 的面板二
已删除目录 <总目录>/vllm-pd-grafana-panel2/glm53-1
已删除空目录 <总目录>/vllm-timing/glm53-1
RESULT: OK（集群 glm53-1 的面板二已卸载）
```

③ 删 12 台机器上留下的 `~/vllm-timing` 和空的 `~/vllm-pd-diag`。脚本先逐台只读判断：`~/vllm-timing` 里只剩标记文件、`~/vllm-pd-diag` 是空目录，才删。只要有一台不是这样，或者连不上，就一台也不删。

```bash
. ~/vllm-pd-test-glm53/vars.sh && cd $UD/vllm-pd-panel2-update-v3.4-extra && bash 01f_删节点上的vllm-timing.sh delete
```

应当看到（12 台各一行，最后一行是结论）：

```
<机器>  ~/vllm-timing 和 ~/vllm-pd-diag 已删除
……
结论：12 台机器上都没有 ~/vllm-timing 和 ~/vllm-pd-diag 了
```

最后一行是「一台也没有删。把这几行带回来」时，停下，把输出带回来。第 5 步可以照常往下做。

---

## 第 5 步：把看板挪到顶层 GLM-5.3-test

**目的：** test-glm53 的面板二看板从 GLM-5.3/test-glm53 挪到顶层文件夹 GLM-5.3-test。换整理命令的两个文件，先 `plan` 看要改什么，再 `apply`。

① 换文件，原来的两个文件留一份 `.before-t64`：

```bash
. ~/vllm-pd-test-glm53/vars.sh && cp -p "$T45/arrange_boards.py" "$T45/arrange_boards.py.before-t64" && cp -p "$T45/boards_layout.txt" "$T45/boards_layout.txt.before-t64" && cp $UD/vllm-pd-panel2-update-v3.4-extra/整理命令/arrange_boards.py $UD/vllm-pd-panel2-update-v3.4-extra/整理命令/boards_layout.txt "$T45/" && python3 "$T45/arrange_boards.py" plan --env-from "$ENV2"
```

应当看到（只看不改）：

```
== 对照表 boards_layout.txt：10 个集群；已登记的 9 个：…；还没登记的：test
改 模型文件夹 test-glm53：test-glm53 → GLM-5.3-test（注册表；<总目录>/vllm-pd-grafana-panel2/test-glm53/deploy.env）
建 文件夹 GLM-5.3-test（顶层）
挪 看板 vllm-pd-request-test-glm53「面板二：请求时延拆解（P / D）（test-glm53）」：GLM-5.3/test-glm53 → GLM-5.3-test（顶层）
删 空文件夹 GLM-5.3/test-glm53（uid <…>）
RESULT: 要改 4 项（plan 只看不改；执行 apply 才改）
```

② 执行：

```bash
. ~/vllm-pd-test-glm53/vars.sh && python3 "$T45/arrange_boards.py" apply --env-from "$ENV2" 2>&1 | tee ~/vllm-pd-test-glm53/arrange-t64.log | tail -n 4
```

应当看到：

```
OK   集群 test-glm53：注册表 是 GLM-5.3-test
OK   集群 test-glm53：<总目录>/vllm-pd-grafana-panel2/test-glm53/deploy.env 推出的文件夹 是 GLM-5.3-test
OK   再看一遍：没有要改的
RESULT: OK（改了 4 项；回退：python3 arrange_boards.py undo <家目录>/vllm-pd-arrange-backup/<时间> --env-from <同一个 deploy.env>）
```

---

## 第 6 步：面板二更新到 v3.4-extra

**目的：** 换 test-glm53 的面板二看板和段定义。数据不删；过滤器约 2 秒内自己重读新的段定义，不用重启 vLLM。先 `check`（只读），再 `run`。

① 只读核对：

```bash
. ~/vllm-pd-test-glm53/vars.sh && cd $UD/vllm-pd-panel2-update-v3.4-extra && python3 vllm_pd_update_pack2_v34extra_filled.py check --cluster test-glm53 2>&1 | sed -n '/==== 摘要/,$p'
```

应当看到（尖括号是每次不同的部分）：

```
==== 摘要（只发这一段）====
vllm_pd_update_pack2_v34extra.py check，面板二 v3.4-extra，集群 test-glm53，成功
check：通过（面板二 v3.3-extra，vllm 模式；WARN 1 项）
迁移预览：RESULT: OK
INFO 段定义换成新包的 profiles/glm53/segments.json（config_id pd-diag-v3.3-extra-glm53-2026-11-20a → pd-diag-v3.3-extra-glm53-2026-11-20a）
INFO segments.json 会更新（pd-diag-v3.3-extra-glm53-2026-11-20a@ace07cfe → pd-diag-v3.3-extra-glm53-2026-11-20a@0d6699a1），过滤器自己重读，看板读的 config_id 不变
INFO 过滤器程序和日志配置（pd_diag_logfilter.py、pd_diag_logging.json）和现在的相同，不用重启 vLLM
升级安全检查（升级前）：RESULT: WARN（1 项）
WARN 版本和文件：1 个文件和 SHA256SUMS 不同，升级会换成包里的：segments.json
INFO 节点：<机器> 找到 vLLM 容器 <容器>（容器里有 vllm 进程）；不对时填 deploy.env 的 NODE_VLLM_CONTAINERS
……
INFO 最近 15 分钟 <n> 个请求
升级前比对：RESULT: 不变 2 项，有变化 3 项，比不了 0 项
INFO 看板：有变化：panel「请求数」的 targets[0].rawSql
INFO 表和视图：有变化：视图 phase_line_cur 的定义；视图 request_json 的定义
INFO 采集器：有变化：central.yaml 第 22 行（包里：exclude: ["/pull/*/mooncake/**", "/pull/*/d*/mooncake/**"]）
日志：<家目录>/vllm-pd-update-logs/pack2-v34extra-test-glm53-check-<时间>.log
```

- 「1 个文件和 SHA256SUMS 不同，升级会换成包里的：segments.json」是这次要换的段定义，不用处理。
- 升级前比对里「看板」「表和视图」两项是这次要改的东西。「采集器」一项 v3.3-extra 的 check 也报，`run` 会跳过它（内容不变），不用处理。

② 更新：

```bash
. ~/vllm-pd-test-glm53/vars.sh && cd $UD/vllm-pd-panel2-update-v3.4-extra && python3 vllm_pd_update_pack2_v34extra_filled.py run --cluster test-glm53 2>&1 | sed -n '/==== 摘要/,$p'
```

应当看到：

```
==== 摘要（只发这一段）====
vllm_pd_update_pack2_v34extra.py run，面板二 v3.4-extra，集群 test-glm53，成功
预检：通过（更新前面板二是 v3.3-extra，vllm 模式）
备份：<家目录>/vllm-pd-update-backup/<时间>
迁移：deploy.env 里没有要写的
数据：保留，不删（vllm_pd 的 7 张表，共 <n> 行）
已更新：表和视图：视图 request_json 的定义
跳过（内容不变）：采集器
跳过（内容不变）：数据源
已更新：看板：panel「请求数」的 targets[0].rawSql
跳过（内容不变）：Prometheus（不写文件、不重新加载）
节点文件：segments.json 会更新（…@ace07cfe → …@0d6699a1），过滤器自己重读，看板读的 config_id 不变；过滤器程序和日志配置（…）和现在的相同，不用重启 vLLM
段定义：profiles/glm53/segments.json；proxy 采集 <开或关>；Mooncake 计时：节点目录里有 mooncake/ 的文件就关联，不用开关（拉取程序 status 的 mooncake 一行）
「有值 / 请求」列：隐藏（bin/has_value.sh show | hide 改）
不变的项：全部通过（9 项）
几分钟后看看板；可选的核对（不影响升级）：cd <总目录>/vllm-pd-grafana-panel2/test-glm53 && bin/panel2_cluster.sh check test-glm53
回到 v3.3-extra：python3 <用户目录>/vllm-pd-panel2-update-v3.4-extra/vllm_pd_update_pack2_v34extra_filled.py rollback <家目录>/vllm-pd-update-backup/<时间> --cluster test-glm53
日志：<家目录>/vllm-pd-update-logs/pack2-v34extra-test-glm53-run-<时间>.log
```

「回到 v3.3-extra：」一行是退回时用的命令，留着。

---

## 第 7 步：再整理一次看板

**目的：** 确认更新以后看板仍在 GLM-5.3-test，应当没有要改的。

```bash
. ~/vllm-pd-test-glm53/vars.sh && python3 "$T45/arrange_boards.py" apply --env-from "$ENV2" 2>&1 | tail -n 1
```

应当看到：

```
RESULT: OK（没有要改的：看板都在对照表的位置）
```

---

## 第 8 步：核对

**目的：** 等 3 分钟，让新段定义下的数据进库，再核对一次，打开看板看一眼。

```bash
. ~/vllm-pd-test-glm53/vars.sh && cd "$D" && cat VERSION && bin/panel2_cluster.sh check "$C" 2>&1 | grep -E "过滤器版本|RESULT"
```

应当看到：

```
v3.4-extra
OK   过滤器版本：<n> 个实例 <m> 个进程都是 pd-diag-v3.3-extra-glm53-2026-11-20a@0d6699a1
RESULT: OK
```

打开 Grafana 顶层文件夹 GLM-5.3-test 里的「面板二：请求时延拆解（P / D）（test-glm53）」，看两张分布表：

- P 表：P 合计正上方是 else，P 合计下面是标题行「后台任务」；
- D 表：有标题行「第一步」「第二步起 · 每步」，D 合计正上方是 else；
- 段名里没有括号，没有「有值 / 请求」列。

**需要「有值 / 请求」列时**（可选，只改看板，节点和数据都不动）：

```bash
. ~/vllm-pd-test-glm53/vars.sh && cd "$D" && bin/has_value.sh show
```

应当看到 `RESULT: OK（「有值 / 请求」列已显示）`。再隐藏用 `bin/has_value.sh hide`。这个设置写在 `deploy.env` 里，以后更新面板二会保留。

**诊断工具**（可选，不用带回输出）：用法见交付目录 `诊断工具/使用说明.zh-CN.md`。

---

## 退回（需要时才做）

- **第 6 步：** 原样执行 `run` 摘要里「回到 v3.3-extra：」后面的命令，退回 v3.3-extra。命令的样子：

  ```
  python3 <用户目录>/vllm-pd-panel2-update-v3.4-extra/vllm_pd_update_pack2_v34extra_filled.py rollback <家目录>/vllm-pd-update-backup/<时间> --cluster test-glm53
  ```

  摘要第二行应当是「恢复 …：和更新前一致，校验全部通过」。退回以后看板仍在 GLM-5.3-test：第 5 步在第 6 步之前做，第 6 步备份时注册表里已经是 GLM-5.3-test。
- **第 5 步：** `apply` 最后一行写着退回命令（`python3 arrange_boards.py undo ~/vllm-pd-arrange-backup/<时间> --env-from <同一个 deploy.env>`），把 `arrange_boards.py` 换成 `"$T45/arrange_boards.py"`，`--env-from` 后面写 `"$ENV2"`。要两步都退回时，先退回第 6 步，再退回第 5 步。
- **第 3 步：** 删掉 `~/vllm-pd-test-glm53/vars.sh` 的最后一行。第 4 步做过以后不要退回这一步（glm53-1 的 `deploy.env` 已经没有了）。
- **第 4 步：** 不能退回。
