# 面板一（P、D 看板）差异分析

`diff_panel1.py` 读出内网的两张看板，逐个 panel 和内置的基线比较，把不同的地方写成一个很短的文件 `panel1-diff.txt`。

**内置的基线：** 我们这边记录的两张看板，与本目录 `dashboards/vllm-pd-prefill.json`、`vllm-pd-decode.json` 相同（摘要 base 436fc949）。它的来历：
- 按 11 月 11 日发回的差异校准；
- 做完四处改动（见 `UPDATE_PANEL1.zh-CN.md`）；
- 11 月 12 日并入现场 P/821 的标题「Prefill tokens 计算量」，其余现场改动按用户决定丢弃；
- 之后所有折线图改为每行一张，D 看板折线图的图例从右侧移到下方；
- 再按用户 11 月 12 日的决定改了标题和分布图：「实时总览」、KV Cache 使用率（HBM）、命中率的名字、输入长度和实算 tokens 分布改为百分比，三张长度分布图的查询加上 le 过滤；
- 时延分位数图加 Max（桶上限），图例名改成「实例 P99」的写法，D 的这 6 张用表格图例、高 14；P 看板加 Mooncake get/put 失败的 5 张图，「KV Cache 与 Mooncake Client」拆成两组。

现有面板一换成这份基线的步骤见 `REPLACE_PANEL1.zh-CN.md`。所以：

- 执行完 `update_panel1.py` 之后跑本脚本，结果应为 `P 0处`、`D 0处`，表示两边完全一致。
- 以后看板有了新改动，跑一次本脚本，把 `panel1-diff.txt` 发回来，我们就能把这边同步成和内网一样。

- **只读：** 脚本只向 Grafana 发读取请求，不改任何看板，也不写 Grafana 的文件。
- **环境：** 只需要 Python 3.6 及以上，用标准库，不用装别的。

## 给 agent 的任务

在能访问 Grafana 的机器上执行（一般就是监控机）。某一步不符合预期就停下，报告步骤号和完整输出。

### 步骤 1：放好脚本

把本目录的 `diff_panel1.py` 拷到监控机上任意一个目录，进入该目录。

```bash
python3 diff_panel1.py --help | head -1
```

预期：第一行以 `usage: diff_panel1.py` 开头。

### 步骤 2：运行

先输入 Grafana 管理员密码（不显示，不进 shell 历史）。地址不是 `http://localhost:3000` 的，换成实际地址。

```bash
read -rs -p 'Grafana 密码：' GRAFANA_PASSWORD; echo; export GRAFANA_PASSWORD
python3 diff_panel1.py --url http://localhost:3000 --user admin
```

预期输出四行，形如：

```
P 0处 | 预置 vllm-pd-prefill.json | da7961e5
D 0处 | 预置 vllm-pd-decode.json | 11629d0e
结果文件 /…/panel1-diff.txt：3 行，72 字符；套回基线自检通过
与基线：无差异
```

退出码：0 表示无差异，1 表示有差异，2 表示出错（读不到看板、文件不对等）。

- 前两行：P、D 各有多少处不同，分布在哪些行；最后 8 位是校验摘要。上面是两边一致时的样子。
- 有差异时形如 `P 29处 总览17 API Server7 KV Cache 与 Mooncake Client5 | 非预置 | f1014d67`。
- 最后一行：结果文件的位置、行数、字符数。「套回基线自检通过」表示脚本已经验证过，这个文件能把基线精确还原成你们的看板。
- 两张看板都和基线相同时，结果文件只有 3 行。

不符合时：

- **`HTTP 401`：** 密码或用户名不对。用服务账号 token 也可以：`GRAFANA_TOKEN=<token> python3 diff_panel1.py --url http://localhost:3000`。
- **`HTTP 404`：** 看板的 uid 不是 `vllm-pd-prefill`、`vllm-pd-decode`。在浏览器打开看板，地址栏 `/d/` 后面那一段就是 uid，加参数 `--uids <P 的 uid> <D 的 uid>` 再运行。
- **`连不上 Grafana`：** 地址或端口不对。
- **`自检失败`：** 把完整输出发回来，不用发结果文件。

### 步骤 3：检查后发回

只发回一个文件：`panel1-diff.txt`，就在运行脚本的目录里。请作为附件原样发送，不要把内容粘贴进聊天窗口：粘贴时空格、换行可能被改动，我们这边套用时会按校验摘要拒收。

**发回前请自己看一遍。** 文件里是你们看板上与基线不同的原值，可能包括查询表达式、数据源 uid、链接，可能带出内网地址或主机名。脚本不做脱敏，不能外发的内容请先确认。

## 没有 API 访问时

两张看板如果由部署包的预置文件加载（v3 部署包就是这样），`grafana/dashboards/` 下的两个 JSON 文件就是看板本身，可以直接读：

```bash
python3 diff_panel1.py --files grafana/dashboards/vllm-pd-prefill.json grafana/dashboards/vllm-pd-decode.json
```

也可以在 Grafana 页面上导出：看板右上角「Export」→「Export as JSON」，两张看板各存一个文件，用上面的命令读。

导出时：
- 格式选「Classic」，不要选「V1 Resource」「V2 Resource」。资源格式的 JSON 顶层是 `apiVersion`、`spec`，脚本会报错，并提示改用 Classic 格式或 API。
- **不要**勾选「Export the dashboard to use in another instance」或「Export for sharing externally」（随页面不同，显示其中一个）。勾上会改写数据源。

结果一样写到 `panel1-diff.txt`。用文件输入时，第一、二行的末尾是 `文件输入`，不是 `预置 ...`。

## 结果文件长什么样

不用读懂它，原样发回即可。下面的说明供核对时参考。

```
# base 9cdbbecc fmt 1
# P 29处 总览17 API Server7 KV Cache 与 Mooncake Client5 | 非预置 | f1014d67
# D 63处 总览（备份）27 已删除19 NPU 与主机10 看板4 API Server2 另1行 | 非预置 | 703809f3
P/200 = @99
D/dash refresh = "1m"
D/49 -
D/19-36,85-97,900-911,913-921,950 g.y += -4
P/801 ti = "请求吞吐（完成）"
P/810 fc.d.u = "percent"
```

- **开头 3 行：** 基线编号；P、D 各一行摘要，写明看板是由哪个预置文件加载的，或不是预置的；行末是这张看板的 8 位校验摘要，我们套用后用它核对，不一致就拒收。
- **其余每行一处不同：** 格式是「看板/panel id 字段 = 你们的值」。只写你们那边的值，相同的不写。
- **几种写法：**
  - `-` 表示你们那边没有这个字段或这个 panel；
  - `+= -4` 表示数值统一加减，比如一排 panel 都上移了 4 格；
  - `= @97` 表示只有你们有的 panel，以我们的 97 号为底稿，后面几行写出它的不同之处；
  - `dash` 表示看板级字段，如变量、时间范围、刷新间隔。
- **合并：**
  - 多个 panel 的同一处改得一样，只写一行，panel id 写成 `85-97` 或 `37,200`；
  - `*` 表示全部 panel，`*/*` 表示两张看板的全部 panel。
- **字段名用了缩写：** 例如 `ti` 是标题，`t.0.e` 是第 1 个查询的表达式，`g.y` 是纵坐标，`fc.d.u` 是单位。对照表在脚本开头的 `ABBR` 里。
- **值是完整的 JSON，** 不截断。

**比较范围：**
- 每个 panel 按 id 比较：所在行、行的折叠状态、位置大小、标题、类型、查询的全部字段、fieldConfig、options，以及 panel 上的其他字段。
- 看板级字段也都比较，包括变量、时间范围、刷新间隔等。
- 只有一边有的 panel 也会写出。

**不比较的：**
- 三个看板级字段 `id`、`version`、`iteration`。每次保存或换一个 Grafana 实例，它们都会变。
- 按集群派生的配置值：看板 uid、标题末尾的「（<集群名>）」、datasource 变量的 `regex` 和 `current`。所以用 `bin/panel1_cluster.sh` 装的新集群看板，加 `--uids vllm-pd-prefill-<集群> vllm-pd-decode-<集群>` 跑本脚本，结果也是 0 处。
- panel 在 JSON 数组里的排列顺序、JSON 键的顺序。Grafana 按位置摆放 panel，与这两种顺序无关。

## 漂移检查与并入

全量部署前先跑一次，看退出码：0 表示可以直接部署；1 表示现场有改动，要先决定这些改动要不要进基线。

**1. 列出现场改动**（在内网执行）：

```bash
python3 diff_panel1.py --url http://localhost:3000 --user admin --decisions-out decisions.txt
```

`decisions.txt` 里，现场的每处改动占一行，默认「保留」：

```
# base 436fc949 P 8e6bc10b D 11629d0e
保留 P/821/title 「输入 tokens 构成」标题：基线 Prefill tokens 计算量 → 现场 输入 tokens 构成
保留 P/9001/新增 现场加了 timeseries「…」（行「…」）
保留 D/93/删除 现场删了 stat「…」
```

一处改动是下面之一：
- 一个看板级字段；
- 一个变量（按名字）；
- 加的一个 panel，或删的一个 panel；
- 某个 panel 的一个字段，如标题、位置、fieldConfig、所在的行；
- 某个 panel 的一个查询（按 refId）。

用户把不要的那些行的第一列改成「丢弃」。第二列和后面的说明不要改。

**2. 生成新基线**（在我们这边执行，输入是同一时刻的现场看板）：

```bash
python3 diff_panel1.py --files P.json D.json --merge --decisions decisions.txt --out-dir new-baseline
```

- 把「保留」的改动按 panel id、变量名、refId 套到基线上，写出两个 JSON；
- 自检：新基线与现场相比只剩「丢弃」的那些，与旧基线相比只多「保留」的那些；
- 现场看板在做决策之后又变过的（摘要对不上）、决策文件少行或多行的、新增的行选了丢弃而行里的 panel 选了保留的，都拒绝，退出码 2。

新基线提交到 `dashboards/`，再重新嵌入本脚本、重新生成探测程序。

## 测试情况

本机测试结果，测试代码不在本仓库里：

- **环境：** Grafana 12.4.2 一次性容器。加载方式同 v3 部署包：预置文件，不允许界面保存。另外还测了用 API 保存的看板。
- **Python 版本：** 文件输入在 Python 3.6 下跑，并设了 `LANG=C`、`LC_ALL=C`，即终端只支持 ASCII 的情况；API 输入用 3.9。

| 项目 | 结果 |
|---|---|
| 基线对基线（文件输入、API 输入） | 0 处差异，结果文件 3 行 |
| 执行 `update_panel1.py` 之后，在改动后的看板上跑（API 输入） | 0 处差异 |
| 人为改 16 处，覆盖上面各类字段（文件输入） | 全部报出；结果文件 39 行、1356 字符；套回基线后与改动后的看板逐字段相同（用另写的比较程序核对） |
| 批量改动：两张看板全部 panel 的数据源 uid、没有 pluginVersion 的 panel 补上、P 从「时延」起整体下移 2 格 | 347 个字段不同，结果文件除开头 3 行外只有 3 行，共 313 字符 |
| 上面两组一起 | 42 行、1510 字符，全部报出，套回后逐字段相同 |
| 标题里有 U+2028、U+2029、U+0085 和连续空格 | 正常运行，套回后逐字段相同 |
| 结果文件被改动后再套用（合并连续空格；改掉一个值） | 都按校验摘要拒收，不写出文件 |
| 资源格式的导出 | 报错，提示改用 Classic 格式或 API |
| `--dump-baseline`、`--apply` 的目标文件已存在 | 报错，不覆盖 |
| API 输入，看板由预置文件加载 | 与文件输入的结果逐行相同；开头写明两个预置文件名 |
| API 输入，看板是用 API 保存的 | 与文件输入的结果相同，只多出副本自己的 uid 和标题两行；写明「非预置」 |

11 月 12 日（新基线、配置类、退出码、并入）：

| 项目 | 结果 |
|---|---|
| 新基线对新基线；旧基线 9cdbbecc 对新基线 | 0 处，退出码 0；1 处（P/821 标题），退出码 1 |
| 新集群看板（uid、标题后缀、datasource 的 regex 和 current 都改了） | 0 处，退出码 0；再改标题正文，退出码 1 |
| 人为改 7 处（两处标题、一个查询、加 panel、删 panel、变量字段、看板字段），`--decisions-out` | 7 条，一处一条 |
| `--merge`，51 种保留组合（保留 0、1、3、6、7 条的全部组合） | 全部通过自检；全保留等于现场，全丢弃等于旧基线 |
| 决策文件少一行、第一列写错、对不上现场摘要；新增的行丢弃而行里的 panel 保留 | 都拒绝，退出码 2 |
| 实验室并入演练（Grafana 12.4.2，文件预置，allowUiUpdates true，界面保存改两处标题、加一个 panel、删一个 panel）：探测 → 决策（保留两条、丢弃两条）→ 并入 → 备份 → 写入 → 验证 → 恢复 | 见 `REPLACE_PANEL1.zh-CN.md` 的测试记录 |
