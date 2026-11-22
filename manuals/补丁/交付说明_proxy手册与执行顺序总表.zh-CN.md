# 《glm5.3 测试集群 proxy 打补丁操作手册》和《执行顺序总表》交付说明

## 第三轮：现场自己改 proxy 文件的那条路（本地提交，未推送）

**顾问审过一轮，无阻塞，4 条已知问题。** 第 2、3 条写进 proxy 手册附录 B 第 16、17 条（只改文字，命令不改）；第 1 条是审查材料 D4、D5 比终版补丁包旧一版，已对 `c08056fe` 重出（审查材料不进交付）；第 4 条（附录 D 第 2 处 `NameError` 的说法）核过是对的，不用改。冻结。

用户原话：

- 「proxy需要提供你的修改等patch，因为内网的proxy虽然也是这个load_balancexxxx的文件，但是好像也和你的original one不完全一致。所以我需要在内网自己修改代码。」
- 「是的 我会改proxy 所以sha256sum肯定不同。你需要在前几个步骤 向我要我改后的新的文件或者新文件的sha256sum。这个要搞」

### 交付件

| 文件 | sha256（前 16 位） | 和第二轮（ServingBench 42e7831）比 |
|---|---|---|
| `执行顺序总表.zh-CN.md` | `85f1b78ec2e92c28` | 原 `a47f9dfa`。第 2 行加上第 3A、3B 步；「proxy 第 3 步报 STOP 时」分成「只报 `file_not_a_known_version`」和「别的原因」两种；「为什么是这个顺序」改一句。第 8 行没改 |
| `glm5.3测试集群proxy打补丁操作手册.zh-CN.md` | `ce5ddf8055e447ea` | 原 `3925bbc7`，改动见下面「手册改了什么」；顾问审以后只加了附录 B 第 16、17 条（审的是 `cfc7f3a2`） |
| `t51-glm53-proxy-kit.tar.gz`（`.sha256` 是全长哈希） | `c08056fed8d83f39` | 原 `75fd49e8`。核对脚本升到 px-3；两份 `tests/run_tests.py` 加 `--base`、`--patched`（`--base none`：没有原文件时跳过和原文件的对比）；其余 10 个文件不变，`SHA256SUMS` 仍是 13 行 |
| 面板手册 `30c67f67`、已填路径版 `66789d42`、打补丁手册 `48c88aa7` | — | 没改，不重出 |

另有两份只给我们自己用、不进交接仓库的文件：`tests/t51s/verify_site_file.sh`（核用户带回的文件，见下面）和 `tests/t51s/` 下的实验室走查脚本。

### 核对脚本 px-3

- 新参数 `--expect-sha256 <64 位>`：proxy 文件只按这个值判。相同时多打一行 `file expect_sha16=<前 16 位> match=yes`，不再报和版本有关的 `STOP`；不同时是 `match=no`，`RESULT: STOP file_not_the_expected_one`。值不是 64 位十六进制时，第二行就是 `RESULT: STOP bad_expect_sha256`，什么都不读。
- 不带这个参数时和 px-2 相同（只有第一行的版本号不同）：实验室已知版本那条路第 3 步的输出逐行和第二轮相同。
- 零影响：四个分支都在 strace 下跑过（`tests/t51s/results/Z03-strace-px3-*.txt`：`match`、`mismatch`、`bad`、`--proxy-file` 加 `match`）。写打开 0、网络 0、子进程 0（`execve` 1 次是 python 自己）、发信号 0（这次加查了 `kill`、`tkill`、`tgkill`）、删改名建目录 0，`write` 只有标准输出一次。读的是 proxy 的 `/proc/<pid>/` 下 `cmdline`、`status`、`environ`、`cgroup` 和 proxy 文件本身。

### 手册改了什么

- **第一页：** 新加「两条路，第 3 步定走哪条」；总顺序第 4 条注明含第 3A、3B 步；补丁包表里核对脚本写 px-3、补丁和测试的用途；现场值表拆出 `<版本>`（改过时取 `diff vs=` 的值），新加 `<新文件>`、`<新文件 sha256>`（两条路各取什么，已知版本时 64 位写全）。
- **第 3 步：** 新加「现场文件改过时」一段：最后一行恰好是 `RESULT: STOP file_not_a_known_version`（只有这一个原因）、两个 `dev_ino` 相同、`version=other`、下一行是 `diff vs=…` 时做第 3A 步，附实验室这三行。「不对时」改成「`STOP` 后面不只这一个原因」；原因表里这一行改成指向第 3A 步，不再要 `--show-diff`。实验室输出换成这次 px-3 的。
- **新第 3A 步：** 从容器里拷出现场文件到补丁包目录下的 `site/orig.py`、`site/new.py`（已有 `site` 时停下，不覆盖）；`patch --dry-run`；`patch --no-backup-if-mismatch`（打不上的写进 `new.py.rej`）；按附录 D 手工合并。「不对时」含没有 `patch` 的情况。
- **新第 3B 步：** 用 `/proc/<进程号>/exe -I -S -B` 编译（proxy 正在用的解释器，文件从标准输入读，容器里不写文件）；两段 `diff`（原文件对上游、新文件对补丁包的打好补丁的文件，应当只有现场自己的改动，第二段多出来的就是合并错的地方）；`sha256sum` 两个文件。带回：首选两个文件；按你的补充，另两种各一行：只带新文件（不需要原文件的测试照跑，和原文件的对比写明没跑，靠第 8–11 步），只带 sha256（测试不跑，只靠第 8–11 步）。带出前自查地址、口令那一句保留。然后停下，等我们回复确认的 sha256。
- **第 6、8 步：** 两条路共用一条命令，`<新文件>`、`<新文件 sha256>` 从现场值表取；都加 `--expect-sha256`。「应当看到」是 `match=yes` 和 `RESULT: OK`，两条路各附实验室输出。
- **回滚 1：** 最后的核对改成 `--expect-sha256 "$(docker exec <容器> sha256sum /root/t51-proxy-bk/orig.py | cut -c1-64)"`，按第 5 步备份的原文件判。原来的命令在现场文件改过时恢复成功也会报 `STOP file_not_a_known_version`。两条路都用这一条。
- **带回清单：** 第 3 步只带全部输出；加第 3A、3B 两行。
- **第一页影响表**加一行：补丁包目录下的 `site/`。
- **附录 A** 加三条（为什么让用户自己打、为什么首选两个文件、为什么用 `/proc/<进程号>/exe`）；**附录 B** 第 4 条补上 px-3 的 strace，加第 14–17 条（编译只查语法；只带回 sha256 时第 8 步核不了合并对不对；做过第 3A 步却在第 6 步填了补丁包的文件和 sha；busybox 等非 GNU 的 `patch`）；**附录 C** 改一条、加四条；**新附录 D**：补丁逐处说明。

### 附录 D：补丁逐处说明

- 两版补丁加的行、删的行逐行相同（`grep '^+'`、`grep '^-'` 比过）。分处不同：v0.30 版 20 处，main 版 22 处（main 把开头的 import 分成两处，`stream_service_response` 第一处也分成两处）。表里每一行是同一处改动，两列分别是它在两版里排第几处，和 `patch` 打印的 `Hunk #<n>` 一致（实验室第 3A 步打印的 `Hunk #12 FAILED` 就是表里 v0.30 第 12 处）。
- 每一行写了在哪个函数、哪两行之间，加了什么，哪几行不能漏。
- 「不设 `PD_PROXY_DIAG` 时行为不变」的依据写成一段：补丁加的代码只有四种（导入和定义、缺省 `None` 的参数和往下传、`if pd_rec is not None:` 下的语句、最外层原样再抛出的 `except asyncio.CancelledError`），以及测试第 1–3 轮比的就是这一点。
- 手工合并最容易出错的两处单独写了：第 2 处那一大块必须在 `send_request_to_service` 之前（参数注解在定义时求值，文件没有 `from __future__ import annotations`）；第 12 处的 `start(req_data)` 必须在 `assign_instances` 之前，现场在那里也改 `req_data` 时放在现场的行之后。

### 用户带回文件时我们怎么核（统筹这边直接用）

在 `pd-v3-wt-v31extra` 仓库（v3.1-extra 分支）的根目录里：

```bash
tests/t51s/verify_site_file.sh <补丁包 tar> <带回的 orig.py> <带回的 new.py> <结果目录>
```

只带回新文件时，第二个参数写 `-`：

```bash
tests/t51s/verify_site_file.sh <补丁包 tar> - <带回的 new.py> <结果目录>
```

要 docker 和镜像 `t51g-vllmtest:v1`（Python 3.12.3、fastapi 0.136.3、httpx 0.28.1、uvicorn 0.54.0）。不进现场，四段输出：

1. 两个文件的 sha256 和行数；
2. 用户的改动（原文件到新文件）和补丁的加、删行逐行比：多出来的行、补丁里没加上的行，各列出来；`SAME_AS_PATCH=yes` 表示只改了补丁那些行；
3. 新文件编译；
4. 补丁包测试，`--base orig.py --patched new.py`。

最后一行 `RESULT: OK tests passed; changes beyond the patch: none`（或者 `see section 2`），再一行 `new file sha256=<64 位>`。只带回新文件时，第 2 段打 `no original file: skipped`，测试里两行 `SKIP off-unset/off-zero: same as unpatched (no original file)`，最后一行是 `RESULT: OK tests passed, without the comparison with the original (no original file: not run)`。不通过时是 `RESULT: STOP compile_rc=… tests_rc=… PASS=… FAIL=…`，`FAIL` 行列在上面。

回复用户时，带上结论和这个 sha256，例如「可以：`<新文件 sha256>` = `<64 位>`，从第 4 步做起」。第 2 段有多出来的行时，一起告诉用户是哪几行。只带新文件时，回复写明「和原文件的对比没跑，由第 8–11 步在现场核对」；只带回 sha256 时没法跑，回复写明「没有测过，只靠第 8–11 步在现场核对」。

用终版补丁包跑过的四次（`tests/t51s/results/final/`）：

- `V01-two-files.txt`：实验室走查时从节点上带回的两个文件（现场改过的原文件、手工合并后的新文件），`SAME_AS_PATCH=yes`，测试 118 项全过，`RESULT: OK`，`new file sha256=cc30f368…94bf5d`；
- `V03-new-file-only.txt`：只给新文件，98 项全过，两行 `SKIP`，`RESULT: OK … without the comparison with the original`；
- `V02-missed-start.txt`：故意漏掉第 12 处的 `pd_rec.start(req_data)` 两行，第 2 段列出这两行（`patch lines not added: 2`），40 过、22 失败，`RESULT: STOP`；
- `V04-new-only-missed-start.txt`：同一份漏两行的文件，只给新文件，20 过、22 失败，`RESULT: STOP`。

### 测试（`tests/t51s/results/`）

- 补丁包测试，缺省的两个文件：main、v0.30 各 118 项全过，0 失败（`final/T-main-default.out`、`final/T-v0.30-default.out`，用终版 tar 解开的文件跑）。
- `--base`、`--patched` 换成实验室那对现场文件、`--base none`、漏两行的那份：见上一节四次。

### 实验室走查（t51k，每个容器 `--rm`，走完拆掉）

从面板手册第 16 步做完的状态起（`tests/t53/setup_px.sh`），proxy 在单独的容器里，起法和第二轮相同（`docker exec -d … sh -c '… >> /var/log/proxy.log'`，2 个 worker）。

- **A，已知版本那条路**（v0.30 原文件 `85b89829`）：第 1–3、5–8 步和回滚 1 照手册执行（第 4、9–11 步没改，这次没重走）。第 3 步 `RESULT: OK`；第 6 步三行 `version=patched-v0.30`、`match=yes`、`RESULT: OK`；第 8 步 `RESULT: OK`；回滚 1 恢复成 `85b89829`，`match=yes`、`RESULT: OK`。
- **B，现场改过的那条路**（v0.30 上改了两处：`handle_completions_impl` 里加两行，在补丁第 12 处的上下文中间；`setup_logging` 的日志格式改一行，补丁不碰。sha16 `687f3130`，1398 行）：第 1–11 步（含 3A、3B）和回滚 1 照手册执行，结果和手册里写的实验室输出相同：
  - 第 3 步 `version=other`、`diff vs=v0.30 similarity=0.999 hunks=2 added=3 removed=1 funcs=handle_completions_impl,setup_logging`、`RESULT: STOP file_not_a_known_version`；
  - 第 3A 步：副本 sha16 和行数与第 3 步相同；试打和真打都是 `Hunk #12 FAILED`、其余 19 处打上，`1 out of 20 hunks FAILED`，留下 `new.py.rej`；按附录 D 第 12 处手工合并；
  - 第 3B 步：`编译通过 3.12.3`；两段 `diff` 都只有那两处现场改动；`site/new.py` 的 sha256 `cc30f368…94bf5d`；我们这边的核对 `RESULT: OK`，`SAME_AS_PATCH=yes`，118 项全过；
  - 第 4 步拉取程序 `RESULT: OK`；第 6 步 `version=other`、`match=yes`、`RESULT: OK`（1789 行）；第 7 步 `proxies running=1`、两个环境变量；第 8 步 `match=yes`、`RESULT: OK`；
  - 第 9 步 5 个请求都是 200，两个 worker 各一个文件，`pd_proxy_request` 5 行；第 10 步节点日志镜像里的 proxy 子目录 0、proxy 来源 2 个文件、解析失败 0，`proxy_line` 五个数都是 5；第 11 步 `P lb_proxy`、`D lb_proxy` 各 5；
  - 回滚 1：恢复成 `687f3130`，`version=other`、`match=yes`、`RESULT: OK`。
- **补丁包版本：** 走查用的是 `b167d281`。之后按你的第 4 点补充，两份 `tests/run_tests.py` 加了 `--base none`，出了终版 `c08056fe`。两版只差这两个测试文件，现场的步骤用不到它们；终版用上一节的四次和缺省测试重新跑过。
- **和现场不同的地方：** 实验室的节点镜像没有 `patch`，第 3A 步以前拷进了一个 GNU patch 2.7.6（Debian bullseye）；第 3A 步的手工合并由 `tests/t51s/hand_merge.py` 代替（就是附录 D 第 12 处的三处）；「带回」是从节点上 `docker cp` 出两个文件，在第 3B 步和第 4 步之间跑 `verify_site_file.sh`，确认的 sha256 填进第 4 步以后的命令。
- B 第一次走到第 3B 步后面我们的核对时失败：`verify_site_file.sh` 给 `docker -v` 传了相对路径（docker 返回 125），不是手册的问题。改成绝对路径以后，重新搭实验室，B 从第 1 步重走。第一次的记录留在 `tests/t51s/results/site-try1-verify-relpath/`，第 1–3B 步的输出和第二次相同。

### 个人信息扫描

扫描对象：proxy 手册、总表、终版补丁包解开的全部文件。核对脚本里内嵌的两份原文件和第二轮相同，没有重新解码。

- `scan_pii.py` 的各类都是 0：`/home`、`/Users`、个人 `/data` 目录、桌面目录、GitHub 账号、邮箱、工号样式、IPv4、主机名。
- 另按名字查了本机账号、实验室地址段、实验室口令、个人目录名、临时目录、实验室前缀，也是 0。
- 手册里实验室那份「现场改动」的两行是我们自己写的英文注释（`site change 1`、`site change 2`），不是现场内容。

### 和统筹那条消息不完全一样的地方

1. **回滚 1 也改了。** 消息里说回滚 1–3 不受影响；但现场文件改过时，原来的回滚 1 恢复成功也会报 `STOP file_not_a_known_version`，又要用户在 STOP 的原因里挑。改成按备份的 sha256 判，两条路一条命令。回滚 2、3 没改。
2. **已知版本那条路的第 6、8 步命令也变了**（加了 `--expect-sha256`），两条路共用一条，不写成两支。A 重走了这两步（你的第 2 点）。
3. **第 3B 步首选带回两个文件**（原文件和新文件）：我们的测试要拿原文件作对照。另两种按你的第 4 点各写一行。
4. **第 3 步不再要 `--show-diff`：** 现场改过时走第 3A、3B 步，不再由我们移植。脚本里这个参数还在。

没有改：打补丁手册、面板手册、vLLM、vllm-ascend、Mooncake 部分；proxy 补丁本身的逻辑和记录格式（两份打好补丁的文件、两份补丁 sha256 不变）；交接仓库 ServingBench。

### 已知问题和没有测过的部分

见 proxy 手册附录 B（第 14–17 条是新加的，第 16、17 条来自顾问审）和附录 C（第 3 条改写，加了四条：main 版的这条路、没有 `patch` 那一支、只带回 sha256 那一支、venv 的 Python）。

## 第二轮交付件（742af5f，已推送 ServingBench 42e7831）

以下到文末是第二轮的说明，原样保留；第二轮的「终版」说的是那一轮。


| 文件 | sha256（前 16 位） | 和顾问审过的 8762c7f 比 |
|---|---|---|
| `执行顺序总表.zh-CN.md`（用户的唯一入口） | `a47f9dfac5be8673` | 新加 |
| `glm5.3测试集群proxy打补丁操作手册.zh-CN.md`（没有个人路径，只有一版） | `3925bbc704dd02d1` | 原 `56d2f0dc`；两条阻塞、10 条已知问题 |
| `t51-glm53-proxy-kit.tar.gz`（`.sha256` 是全长哈希） | `75fd49e8244f63ca` | 原 `a5d4f2b2`；核对脚本升到 px-2，其余文件不变 |
| `面板二删除test安装test-glm53操作手册.zh-CN.md` | `30c67f673be6ddad` | 没改，不重出 |
| `已填路径-只放私有Caching/面板二删除test安装test-glm53操作手册_已填路径.zh-CN.md` | `66789d427c6a3a7a` | 没改，不重出 |

本地提交 c21db91 里的第二轮版本作废，不要推：

- 面板手册 `41fc8ec6`：它把 proxy 的几行放回第 17 步；
- proxy 手册 `c9a6bb64`；
- 总表 `aa747037`。

现在的版本都按顾问审过的 8762c7f 和面板手册 30c67f67 第一页的总顺序：面板第 16 步 → proxy 手册 → 面板第 17–22 步。

## 顾问两条阻塞的改法和验证

**阻塞 1：proxy 由容器 1 号进程带起时，第 7 步会停掉容器。**

- **核对脚本 px-2：** 沿 ppid 走到容器里的根（父进程是 0 或看不到），根是 NSpid 1 时报 `STOP proxy_started_by_container_init`。在 `proc parent=` 一行后多打一行 `proc ancestor_root=<进程号> pid1=yes|no root=<命令行，地址遮掉>`。
- **手册：**
  - 第 3 步原因表加这一行；
  - 第 3 步、第 8 步的实验室输出后面，各加一句说明 px-2 多出的这一行；
  - 第 7 步「不对时」加「容器已退出：`docker start <容器>`，带回 `docker ps -a` 那一行」。
- **验证（`tests/t53/results/px2/`）：**
  - `A01`：容器的 1 号进程是 `sh -c`，由它带起 python（真的 proxy，v0.30）。结果 `proc ancestor_root=1 pid1=yes`，`RESULT: STOP proxy_started_by_container_init`。
  - `A02`：1 号进程是 `sleep`，用 `docker exec -d … sh -c` 起。结果 `proc ancestor_root=15 pid1=no`，`RESULT: OK`。
  - `A04`：照手册第 5、7 步备份、停、用 `start_proxy.sh on` 再起。结果 `proc ancestor_root=<proxy 自己> pid1=no`，`env PD_PROXY_DIAG=1 …`，`RESULT: OK`。
- **改法带出的一点：** proxy 直接跑在宿主机上时，往上走到的是宿主机的 1 号进程，第 3 步也报这一条。手册原因表里写明了：同样停下带回。

**阻塞 2：`start_proxy.sh` 要 bash 4.4 以上。** 第 5 步照顾问给的命令加了预检，写进「应当看到」；不通过就停下，不做第 6、7 步，带回。脚本没改。`A03` 是实验室同一镜像里的输出：`bash 5.2.21(1)-release：start_proxy.sh 可用`。bash 低于 4.4 的那一支没有实测，写进了附录 C。

**已知问题：** 顾问的 10 条照抄进 proxy 手册附录 B 第 4–13 条，行号改成了步骤号。其中：

- 第 1 条（strace 只覆盖 STOP 分支）：这次 OK 分支也在 strace 下跑过，照实写了，见下一节。
- 第 7 条（打补丁手册总顺序陈旧）：写明冻结版 98e65095 已经改成 6 段。

**改完以后跑过的：**

- 本地测试：main、v0.30 两版各 118 项，全部通过，0 失败（`T-summary.txt`）。环境是容器 t51g-vllmtest:v1：Python 3.12.3、fastapi 0.136.3、httpx 0.28.1、uvicorn 0.54.0。
- 重新打包，`SHA256SUMS` 13 行全部 `OK`。
- 个人信息扫描各类都是 0，扫的是总表、proxy 手册、补丁包解开的全部文件和核对脚本内嵌的两份原文件。
- 实验室没有重走。

## 核对脚本零影响：两个分支都在 strace 下跑过

- **STOP 分支：** 第一轮的 `tests/t53/results/proxy-walk/Z01-strace-check.txt`，px-1，2 行的桩文件，`version=other`。
- **OK 分支：** 这次的 `tests/t53/results/px2/Z02-strace-ok-branch.txt`，px-2，`RESULT: OK`：
  - 被检进程的命令行里带 proxy 文件名，工作目录里是 v0.30 原文件，用 `docker exec -d … sh -c` 起；
  - 走了 `site_versions`、环境读取、日志目录这几支；
  - 写打开 0；网络 0；clone、fork 0；execve 只有它自己 1 次；unlink、rename、mkdir 0；`write` 只写标准输出 1 次；
  - 读的是这个进程的 `/proc/<pid>/` 下 `cmdline`、`status`、`cgroup`、`environ` 和 proxy 文件。
- **订正：** 第一轮记录里列在「new .pyc」下的 3 个 `encodings` 的 `.pyc` 是镜像里原有的，不是脚本写的。第一轮说明里说它们是假 proxy 写的，不对。

## 执行顺序总表

- **起点：** 面板手册第 1–15 步做完；打补丁手册没开始；P 的 vLLM 当时没在跑，要先按平时的方式拉起，再跑第 3 步。
- **表的内容：** 14 行，每行写手册和步骤、机器、重启什么、用哪一步核对、不通过时去哪一步回滚或停下带回。
- **写明的几件事：**
  - vLLM 和 vllm-ascend（加 p2d）是同一个补丁包，一起换、一起重启；
  - 第 2 批对 test-glm53 是空的；
  - 第 19 步在最后一批以前是 `RESULT: FAIL`，以及哪些 FAIL 项不算失败；
  - 回滚时 Mooncake 和三件的牵连；
  - 段数按做到哪里：40 → 42 → 45，段数不是判据；
  - proxy 第 3 步报 STOP 时跳过 proxy，P、D 两个 lb_proxy 段为空：40 段，做了 Mooncake 是 43 段；
  - 第 17 步的 Mooncake 来源和换不换库的关系：不换库照做，put 下 3 段为空；
  - 全部做完以后 45 段或 42 段，预期为空的段。
- **表里只引用已走查手册的步骤号，不写新命令。** 只有两处是手册里没有的提示：
  - 面板第 18 步在第 17 步重启拉取程序 3 分钟以后做，这是第一轮走查里看到的；
  - 第 19 步不做 Mooncake 时 `RESULT: FAIL` 列出 put 下 3 段是预期的，这是按 coverage 的判法。
- **W1 的核对：** W1 对总表里打补丁那几行提的五条都照改了：第 2 批为空；第 19 步最后一批以前的情况；回滚的牵连和两句提示；`mooncake=BAD(other)`；第 8 步的停止条件。
- **行号：** 行号和打补丁手册最终版 48c88aa7（已填路径版 3c2c390e）第一页引用的一致：第 1–21 步对应第 1、3–6 行，第 27–34 步对应第 11–12 行。总表引用的就是这一版。

## 第一轮的内容（8762c7f，顾问审过的那一版）

面板包 tar（`e3885b93…`）、vLLM、vllm-ascend、Mooncake 的补丁都没有改。来源：仓库 pd-v3-wt-v31extra（分支 v3.1-extra）`docs/v3.1-extra/`，走查记录在 `tests/t53/`。

### 补丁包里有什么

| 文件 | 说明 |
|---|---|
| `t51_proxy_check.py` | 只读核对脚本，版本 px-1 |
| `main/` | 给 vllm-ascend main 那一版（原文件 `ffd9a54456dc6ad1`，1440 行）：打好补丁的文件 `205aa8d49ad7f4b1`（1831 行）、补丁、原文件、本地测试。就是产物 5 |
| `v0.30/` | 给 v0.30 那一版（原文件 `85b8982948530758`，1396 行）：打好补丁的文件 `68e07c0abd1fbf4c`（1787 行）、补丁、原文件、本地测试。这次移植的 |
| `stop_proxy.sh`、`start_proxy.sh` | 停 proxy；按备份下来的参数、环境、工作目录、输出文件原样起 proxy，加 `on` 时多两个环境变量 |
| `SHA256SUMS` | 13 个文件 |

### 现场是哪一版：要准备两版

- vllm-ascend 的 `load_balance_proxy_server_example.py`：现在的 main（536f084cc，2026-11-19）和产物 5 的原文件相同（`ffd9a544`），从 6658faefc（2026-10-30）起没变过，v0.32.0rc1 也是这一版。
- glm5.3 现场的 vllm-ascend 是 d4ad49796。它和 v0.30.0 里的这个文件是 `85b89829`（08c511a03，2026-10-10），和产物 5 的原文件不同。
- 用户说现场用的是「最新 main」的文件，但现场容器里的 vllm-ascend 是 d4ad49796，两种都有可能。补丁包两版都带，第 3 步的核对脚本认出是哪一版，第 6 步按它选文件。两版以外的版本，第 3 步报 `STOP`，带回差异以后再移植。
- 核对脚本里记了这个文件最近 20 个上游版本的哈希。是更早的上游版本时，它能说出是哪一次提交。

### 移植到 v0.30 和调用路径比对

- **移植：** 用 git 把产物 5 的补丁提交 cherry-pick 到 v0.30 的原文件上，没有冲突。两版补丁里加、删的行逐行相同，只是上下文不同（v0.30 没有 `import hashlib`，也没有 main 独有的几行）。`v0.30/proxy-pd-diag.patch` 用 `git apply` 打到 v0.30 原文件上，结果和 `v0.30/load_balance_proxy_server_example.py` 逐字节相同。
- **调用路径：** 补丁改动的函数有 27 个。其中 3 个在两版之间本来就不同：`assign_instances`、`handle_completions_impl`、`stream_service_response`。逐个比较补丁后的函数体，两版只差 main 独有的几行：prefill 会话亲和（`--enable-prefill-affinity`、`X-Session-Id`）和非流式请求去掉 `stream_options`。补丁的插入点和调用都相同。
- **本地测试：** 产物 5 的测试换上 v0.30 的原文件和补丁文件，118 项全部通过，0 失败。环境是容器 t51g-vllmtest:v1，Python 3.12.3、fastapi 0.136.3、httpx 0.28.1、uvicorn 0.54.0、starlette 1.10.0。原文件本身就要求 Python 3.10 以上，补丁没有提高这个要求。

### 核对脚本：为什么不用 glm53-6 的 `--proxy-file`

detector glm53-6 的 `--proxy-file` 能认出文件是不是 main 那一版，也能列出改过的函数。但它不合这次的要求：

- 它往 `/tmp` 写明细文件；
- 不打印文件的 sha16 和行数；
- 不报 proxy 怎么起的、输出写到哪里。

所以另写了一个最小的 `t51_proxy_check.py`：

- **怎么跑：** 必须带 `-I -S -B`，少一个就打印 `REFUSED`，什么都不读。手册里用 `docker exec -i <容器> python3 -I -S -B - < t51_proxy_check.py` 从标准输入读进容器，不往容器里拷文件。
- **报什么：**
  - 文件：路径、sha16、行数、版本（`main`、`v0.30`、`patched-*`、更早的上游版本或 `other`）。是 `other` 时，报和最接近那一版的差异块数、增删行数、涉及的函数；加 `--show-diff` 时另打印差异全文。
  - 启动：参数（地址换成 `<host>`、`<ip>`）、父进程、工作目录、标准输出和标准错误去哪、是不是容器的第一个进程。
  - 环境和依赖：`PD_PROXY_DIAG*` 环境变量，proxy 那个 Python 的 fastapi、httpx、uvicorn 版本。
  - 日志目录：proxy 看不看得到 `/data/pd-diag/logs`、能不能写，以及 `dev_ino`。手册第 3 步拿它和宿主机上的 `stat` 比，确认容器里和宿主机上是同一个目录。
- **结论行：** 最后一行是 `RESULT: OK` 或 `RESULT: STOP <原因>`。
- **零影响：** STOP 分支第一轮用 strace 验证过（`tests/t53/results/proxy-walk/Z01-strace-check.txt`），OK 分支这次也用 strace 验证过（见上面一节）。第一轮的记录：
  - 没有以写或建的方式打开任何文件；
  - 没有 socket、connect；
  - 没有 clone、fork；只有它自己那一次 execve；
  - 没有 unlink、rename、mkdir；
  - `write` 只写标准输出。
  - 只 import 标准库，没有 subprocess、socket。

### 要查清的两件事

**1. 同一份 proxy 行会不会收两遍？** 不会进表两遍，但不排除时会触发解析失败。

- 节点日志的拉取是递归的：`rsync -r`，`--include=*/ --include=*.log --exclude=*`。
- proxy 写进 `/data/pd-diag/logs/proxy/` 时，节点日志的拉取会把它镜像一份到 `<镜像目录>/<机器>/proxy/`。
- 采集器读节点文件时，要求文件名是 `api|engine|worker|pool.*.log`，所以这一份每行都解析失败：进 dead-letter，触发 Prometheus 的解析失败告警（5 分钟超过 20 行）。
- 这一份不会进 `proxy_line`。那张表只读 `<镜像目录>/_proxy/`，节点文件的来源已经排除了 `_proxy`。
- 实验室里专门试过一次：去掉排除以后，镜像里多出 `<P 机器>/proxy/`（2 个文件），dead-letter 抽样 10 行，都是这些 proxy 行；`proxy_line` 仍是 5 行、5 个请求。记录在 `N01-negative-no-exclude.txt`。

**2. 怎么配：** 手册里写死一种，test-glm53 的 `deploy.env` 末尾加：

```
# proxy 打补丁手册第 4 步
PROXY_COLLECT=1
PROXY_MACHINE=<proxy 机器地址>
PROXY_LOG_DIR=/data/pd-diag/logs/proxy
PULL_RSYNC_FLAGS="--append --exclude=/proxy/"
```

- `PROXY_MACHINE` 写 proxy 所在的那台 glm5.3 机器，写法和清单、`bin/puller.sh status` 里相同。
- `PROXY_LOG_DIR` 是宿主机路径。第 3 步核对过：容器里在同一路径，是同一个目录。
- `--exclude=/proxy/` 加在 `PULL_RSYNC_FLAGS` 里，因为拉取程序把它放在 include 规则前面，先匹配先生效。它只排除节点日志目录顶层名叫 `proxy` 的子目录；proxy 来源拉的是 `/data/pd-diag/logs/proxy` 本身，不受影响。`--append` 是原值。
- 这几行要在重启 proxy 之前加，所以放在本手册第 4 步，不在面板手册第 17 步。

**和节点上别的程序不冲突（读代码）：**

- 节点过滤器只清理自己实例目录里、自己命名规则的文件；
- 删除集群时的清理只删深度 2 的 `api/engine/worker/pool.*.log` 和空目录；
- proxy 只清理 `proxy-<端口>/` 里自己的文件。

### 实验室走查

**实验室 t51k：** 每个容器都 `--rm`；起之前看过 docker stats；走完马上拆掉，容器、卷、网络都是 0；t14p2-* 没碰。

**怎么搭的：**

- glm53-1：v3，mock 模式。
- test-glm53：装在第二对机器上，用面板包 e3885b93，照面板手册第 11–16 步，vLLM 是替身。
- 现场那样的 proxy：单独一个容器 t51k-pxr。
  - 镜像 t51g-vllmtest:v1；挂 P 机器的 `/data`；带 `t33.machine` 标签，P 机器的 `docker ps` 看得到它。
  - 由 `docker exec -d … sh -c 'cd /opt/proxy && python3 load_balance_proxy_server_example.py --port 8000 --workers 2 … >> /var/log/proxy.log 2>&1'` 起。
  - 文件是 v0.30 那一版的原文件。
- 节点机器上没有 Python（和宿主机没有 Python 的情况一样），所以核对脚本在容器里跑。

**怎么走：** `tests/t53/walk_px.py` 从手册里取出每一步的 bash 块，原文执行，只把 `<…>` 换成实验室的值。

- proxy 机器上的命令在 P 机器（t51k-n-p2）上以 root 执行，监控机的命令在 t51k-mon 上执行。
- 第 3 步的值（进程号、文件路径、端口、版本）从第 3 步的输出里取。

**结果，全部和手册里「应当看到」一致：**

- **第 1 步：** P 机器上 `proxy 在容器 /t51k-pxr`；D 机器上只有最后一行。
- **第 2 步：** 13 行 `OK`。
- **第 3 步：**
  - `version=v0.30`、`RESULT: OK`；
  - 宿主机和容器的 `dev_ino` 相同；
  - 参数里的地址都换成了 `<host>`、`<ip>`。
- **第 4 步：** 加了 5 行；`status` 里 P 机器两行（节点和 proxy），都是 `OK`。
- **第 5–7 步：**
  - 备份的原文件 sha16 和第 3 步相同；
  - 换成 `patched-v0.30` 以后，停（2 个 worker 一起退出），再用 `start_proxy.sh on` 起来。
- **第 8 步：** 和第 3 步比，只差这几项：
  - 进程号；
  - `parent_pid=0`（由 `docker exec -d` 起）；
  - `env PD_PROXY_DIAG=1 PD_PROXY_DIAG_DIR=/data/pd-diag/logs/proxy`；
  - 文件版本。
  - 参数、端口、worker 数、输出文件都相同。
- **第 9 步：** 5 个请求都是 200；`proxy-8000/` 里 2 个文件（每个 worker 一个），共 5 行 `pd_proxy_request`。
- **第 10 步：**
  - 节点日志镜像里的 proxy 子目录 0 个；
  - proxy 来源镜像 2 个文件；
  - dead-letter 0 行；
  - `proxy_line` 的 rows、ok、with_p、with_d、requests 都是 5。
- **第 11 步：** `P lb_proxy 5 / 100`、`D lb_proxy 5 / 100`。100 个请求里只有经过 proxy 的这 5 个有值；其余是替身自己回放的。
- **面板手册新第 17 步：** 只加 Mooncake 3 行，`status` 4 行都是 `OK`（P、D、proxy、mooncake）。
- **面板手册第 18 步：**
  - 紧接着做时有一条 FAIL：「Prometheus 抓 vllm-timing-puller-test-glm53 不到」。这是因为拉取程序刚重启，手册要求等至少 3 分钟才做第 18 步，我没有等。
  - 等 75 秒以后重做，`RESULT: OK`。
  - coverage 是 47 段里 35 段有值，和面板手册里实验室的那 10 段一致。
- **回滚 1–3：**
  - proxy 回到原文件、原环境（`PD_PROXY_DIAG*=unset`、`version=v0.30`）；
  - `deploy.env` 删掉那 5 行，`status` 里没有 proxy 那一行；
  - `/data/pd-diag/logs/proxy` 已删。
  - 回滚后的核对输出和第 3 步相同，只差进程号和父进程。

**没有在实验室里走的：**

- `main` 那一版。产物 5 当初的本地测试 114 项通过。
- proxy 不在容器里的那一支。

### 面板手册的改动（只改文字，22 步编号不变）

W1 引用的第 3、15、16、18 步没动。改了这几处：

- **第一页总顺序：** 改成 打补丁手册 → 第 16 步 → proxy 打补丁手册 → 第 17–22 步。
- **第一页的值表：** 去掉 `<proxy 机器地址>`、`<proxy 日志目录>`，第 17 步只用 `<P 实例日志文件>`。
- **第 16 步末尾：** 加一句「先做 proxy 打补丁手册，再做第 17 步」。
- **第 17 步：**
  - 标题改成「打开 Mooncake 采集来源」；
  - 命令只加 Mooncake 的 3 行；
  - 说明 proxy 的 4 项已在 proxy 手册第 4 步加过；
  - 「不对时」加一句：没有 proxy 那一行时，先做 proxy 手册。
- 已填路径版用同一个生成脚本重做，和占位符版只差两个路径值。和上一版已填路径版 `f2dfd93c` 比，差的就是上面这 19 行。

### 个人信息扫描

扫描对象：两份手册的占位符版、补丁包解开的全部文件、核对脚本里内嵌数据解码出来的两份原文件，以及本说明。

- `scan_pii.py` 的各类都是 0：`/home`、`/Users`、个人 `/data` 目录、桌面目录、GitHub 账号、邮箱、工号样式、IPv4、主机名。
- 另按名字查了本机账号、实验室地址段、实验室口令、个人目录名、临时目录，也是 0。
- proxy 手册以后放进公开的 GitHub 仓库，里面没有个人路径，所以只有一版。

## 已知问题

见 proxy 手册（`3925bbc7`）附录 B，共 13 条：第 1–3 条是第一轮的，第 4–13 条是顾问的 10 条。

## 没有测过的部分

见 proxy 手册附录 C。这次加了两条：

- 第 5 步 bash 预检不通过的那一支（bash 低于 4.4）没有实测；
- px-2 的两种起法只在单独的容器里补测过，没有在实验室里重走手册。
