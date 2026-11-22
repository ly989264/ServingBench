# glm5.3 测试集群打补丁操作手册

> 本手册取代补丁包（`t51-glm53-patch-kit.tar`，sha256 `d21cc319…`）里的操作说明 A 和 Mooncake 增量包（`t51-glm53-mooncake-kit.tar`，sha256 `6d70978c…`）里的第 11 步说明。两个包的内容不变，本手册放在包外面。

<!-- 占位符版开始 -->
> **本手册有两版。** 这一版是占位符版：路径写成尖括号里的说明，不含任何内网信息。已填路径版把监控机上的个人路径填好，只放在私有的 Caching 仓库。
<!-- 占位符版结束 -->

## 第一页

**做什么。** 在 glm5.3 测试集群（面板二里的集群 `test-glm53`）的 P、D 容器里，打 vLLM、vllm-ascend、p2d 三件补丁，接上面板二，重启一次，分三批上线。最后一部分可选：把 Mooncake 的 `libmooncake_store.so` 换成带计时的替换库。不新建镜像，不新建容器，改动写在容器的可写层里。

**做完是什么样子。**
- 每个容器的检测脚本报 `verdict vllm=ok ascend=ok(site_local=<n>) p2d=ok mooncake=not_replaced`；做了可选的 Mooncake 部分以后，最后一项是 `mooncake=ok`。
- 面板二 `test-glm53` 的看板：第 3 批做完时 47 段里 40 段有值；做完 proxy 手册以后 42 段；再做 Mooncake 部分以后 45 段。段数不是判据。

**总顺序：** 见同一目录的《执行顺序总表》，那一页是唯一入口，行号以它为准。本手册第 1–21 步对应总表第 1、3–6 行；可选的 Mooncake 部分第 27–34 步对应第 11–12 行，要在面板手册第 17 步打开 Mooncake 采集来源以后再做；回滚的第 22–26 步、第 35 步见总表的回滚一节。

面板手册指《面板二：删除 test、安装 test-glm53 操作手册》，步骤号以它为准。

**要先准备好的现场值：**

<!-- 占位符表开始 -->
| 占位符 | 含义 | 在哪里查 |
|---|---|---|
| `<用户目录>` | 监控机上放部署包和更新脚本的目录 | 包二 v3 更新脚本「现场路径」一段的 `USER_DIR` |
| `<总目录>` | 面板目录的上级目录 | 同一个脚本的 `ROOT` |
<!-- 占位符表结束 -->
| `<SSH 账号>`、`<机器地址>` | 从监控机登录 glm5.3 机器用的账号和机器 | 面板手册第 2 步从 test 的 `deploy.env` 里记下的值 |
| `<机器>` | 这台机器的简称，只用在监控机上备份目录的名字里；同一台机器每次写法相同 | 自己定，例如清单里的机器序号 |
| `<容器名>` | 这台机器上跑 vLLM 的容器 | 在那台机器上 `docker ps` |
| `<节点目录>` | 面板二的过滤器在这台机器上写文件的目录（宿主机路径），下面有 `logs/<端口>/` | 沿用 test 的 `/data/pd-diag`，容器里的路径相同（面板手册第 3 步核对过） |
| `<启动脚本>` | 第 3 步检测时 `--start-script` 传的那个文件：P 是 P 那次检测用的启动文件（`start_p.sh`），D 是 D 那次检测用的启动文件（`start_d0.sh`）；一台机器一个，所有 rank 共用 | 第 3 步的命令 |
| `<vLLM 日志文件>` | 这台机器上各个 vLLM 实例的标准输出、标准错误重定向到的文件（宿主机路径），几个文件用空格隔开；D 机器有 16 个 | P 上就是面板手册里填进 `MOONCAKE_LOG_FILES` 的文件；D 上是同一类文件 |
| `<启动文件目录>` | 容器里 `run_dp_template.sh`、`launch_online_dp.py`、`start_p.sh`（D 上 `start_d0.sh`）、`mooncake.json`、`start_mooncake.sh` 所在的目录；不在同一目录的，按各自的路径写，缺哪个就不传哪个 | 那台机器上拉起 vLLM 的方式 |
| `<容器里的 vLLM 日志文件>` | `<vLLM 日志文件>` 在容器里看到的路径；容器里看不到就不传 `--startup-log` | 同上 |
| `<这一批的实例>` | 面板一里这一批实例的名字，用竖线连起来，例如 `D0\|D1\|D2` | 清单里 D 一侧第 n 项（从 0 数）是 `Dn` |
| 端口 | D 的容器是 `$(seq 9100 9115)`（16 个实例），P 的容器是 `9900`。现场不同时按同样的格式改：连续的写 `$(seq <第一个> <最后一个>)`，不连续的用空格隔开 | 面板手册第 13 步写进清单的端口 |

做的过程中还要记下几个值（第 3 步的 `baseline_token` 和 `site_patches`、第 6 步的 `<备份目录>`、`<时间戳>`、第 8 步的 `<S>`），在那一步里写明。

**对服务的影响：**

| 步骤 | 对运行中的 vLLM |
|---|---|
| 1–9 | 不影响：只往容器里写文件、在 `<启动脚本>` 里加两行 `export`，运行中的 vLLM 还在用内存里的旧代码 |
| 10–12 | 每批重启一次这一批容器里的 vLLM 实例；重启本身和平时一样 |
| 13–21 | 只读核对。重启以后多了持续写入：过滤器往 `<节点目录>` 写；做了 Mooncake 部分以后，还往 vLLM 日志写 `[BatchPutTiming]` 行 |
| 22–26 | 回滚，只在失败时做：恢复文件以后再重启一次 |
| 27–35（可选，Mooncake） | 换库不影响运行中的进程；之后分批重启 |

## 通用约定

- 所有容器里的命令都以 root 执行：先在那台机器上 `docker exec -it <容器名> bash` 进入容器。
- 任何一步的结果和「应当看到」不一样时，停下，把那一步的输出带回来，不要继续。
- `LC_ALL=C sha256sum -c … | grep -v ': OK$'` 这类核对命令，没有输出就是全部通过，这时整条管道的退出码是 1。不要把它们放进带 `set -e` 的脚本，按输出判断。
- `docker restart` 以后补丁还在；`docker rm` 后重建容器、或者换镜像，补丁就没了，要重新做一遍。
- 不设 `MC_LOG_DIR`：打补丁、重启时都不要加这个环境变量。
- 三件补丁必须一起装、一起回滚：vllm-ascend 主体和 p2d 用到的名字只有 vLLM 补丁里有，只装其中一件时 Worker 启动就会 `ImportError`。

## 正文

### 一、放进容器，检测，核对，备份（每个 P、D 容器各做一次）

#### 第 1 步　在监控机上核对补丁包，拷到那台机器

**机器：** 监控机。补丁包 `t51-glm53-patch-kit.tar` 和它的 `t51-glm53-patch-kit.tar.sha256` 放在 `<用户目录>/vllm-pd-patch-glm53/` 下。

**命令：**

```bash
cd <用户目录>/vllm-pd-patch-glm53 && LC_ALL=C sha256sum -c t51-glm53-patch-kit.tar.sha256
```

```bash
scp <用户目录>/vllm-pd-patch-glm53/t51-glm53-patch-kit.tar <SSH 账号>@<机器地址>:/tmp/
```

**应当看到：** 第一条打印 `t51-glm53-patch-kit.tar: OK`。

**不对时：** 第一条不是 `OK`：补丁包损坏，重新取一份，不往下做。

#### 第 2 步　拷进容器，解开，核对

**机器：** 先在那台机器上执行第一条，再进容器执行后两条。

**命令：**

```bash
docker cp /tmp/t51-glm53-patch-kit.tar <容器名>:/tmp/
```

```bash
mkdir -p /tmp/t51-kit && tar -xf /tmp/t51-glm53-patch-kit.tar -C /tmp/t51-kit && cd /tmp/t51-kit && LC_ALL=C sha256sum -c SHA256SUMS | grep -v ': OK$'
```

```bash
df -hT /tmp | tail -n 1
```

**应当看到：** 第二条没有任何输出。第三条一行，第二列是文件系统类型，一般是 `overlay`；是 `tmpfs` 时，`/tmp` 下的文件在容器重启以后就没了，第 6 步会自动把备份放到 `/root` 下。

**不对时：** 第二条有输出：补丁包在拷贝中损坏，重新拷贝。

**带回：** `df` 那一行。那台机器上的 `/tmp/t51-glm53-patch-kit.tar` 留着，回滚时要用。

#### 第 3 步　打补丁前的检测

**机器：** 容器里。检测脚本 `/tmp/t51-kit/detect/t51_glm53_check.py`（sha256 `f9e6073f…`）只读，几秒内结束，只往 `/tmp` 写一个明细文件；按 `nice 19`、IO 空闲级运行，读取量上限 1 GiB，耗时上限 300 秒。5 个启动文件里缺哪个就不传哪个；没有 vLLM 日志文件就不传 `--startup-log`。

**命令：** P 的容器：

```bash
python3 -I -S -B /tmp/t51-kit/detect/t51_glm53_check.py --run-dp-template <启动文件目录>/run_dp_template.sh --launch-online-dp <启动文件目录>/launch_online_dp.py --start-script <启动文件目录>/start_p.sh --mooncake-json <启动文件目录>/mooncake.json --start-mooncake <启动文件目录>/start_mooncake.sh --startup-log <容器里的 vLLM 日志文件>
```

D 的容器：

```bash
python3 -I -S -B /tmp/t51-kit/detect/t51_glm53_check.py --run-dp-template <启动文件目录>/run_dp_template.sh --launch-online-dp <启动文件目录>/launch_online_dp.py --start-script <启动文件目录>/start_d0.sh --mooncake-json <启动文件目录>/mooncake.json --start-mooncake <启动文件目录>/start_mooncake.sh --startup-log <容器里的 vLLM 日志文件>
```

带回以后删掉明细文件（路径照抄 `detail=` 那一行）：

```bash
rm -f <detail= 那一行的路径>
```

**应当看到：** 不超过 20 行，退出码 0。其中：

- `vllm=… state=v0.30.0 …` 和 `ascend=… state=modified …`，两行末尾都是 `from=site cands=1`。
- `site_patches patch/__init__.py=<16 位> patch/platform/__init__.py=<16 位> patch/platform/patch_kv_load_failure_reason.py=d7c764bb425993c0 patch/platform/patch_prefill_length_validation.py=c36392a6338bc2f1`；D 上最后一项是 `absent`。前两个记下来，是第 5 步的预期值。
- `inst[run …]`：每组配置相同的实例一行。
- `run` 行里是 `logcfg=unset`：面板二的日志配置要等重启才生效。
- `verdict vllm=unpatched ascend=unpatched(site_local=<n>) p2d=unpatched mooncake=not_replaced`；`strict` 行末是 `p2d=not_replayed`。同样是 `unpatched`，在这一步是预期值，在第 13 步是失败。
- 最后一行 `fingerprint=… baseline_token=<一串>`：把 `baseline_token=` 后面的整串记下来，每个容器各一串，第 13 步要用。

**不对时：**

| 看到 | 怎么办 |
|---|---|
| `site_patches` 的后两个和上面不同 | 停下，带回输出 |
| `mooncake=BAD(other)` | 带回这一行；第 1–26 步照常做，可选的 Mooncake 部分先不做 |
| 只打印一行 `BLOCKED`（自查认出输出里有 IP、主机名之类） | 带回这一行，不往下做 |
| 只打印一行 `REFUSED` | `-I -S -B` 少了，照原文重新执行 |

**带回：** 终端打印的全部行（指纹 `fingerprint=` 相同的容器只带一份），每个容器的 `baseline_token`。

#### 第 4 步　找到安装目录

**机器：** 容器里，接着第 3 步。只查找位置，不导入 vLLM。

**命令：**

```bash
VLLM_PKG=$(python3 -c "import importlib.util as u; print(u.find_spec('vllm').submodule_search_locations[0])")
ASCEND_PKG=$(python3 -c "import importlib.util as u; print(u.find_spec('vllm_ascend').submodule_search_locations[0])")
VLLM_ROOT=$(dirname "$VLLM_PKG"); ASCEND_ROOT=$(dirname "$ASCEND_PKG"); F="$ASCEND_PKG/distributed/kv_transfer/kv_p2p/mooncake_connector.py"
echo "PYTHONPATH=[${PYTHONPATH:-}]"; echo "$VLLM_ROOT"; echo "$ASCEND_ROOT"; echo "$F"
```

**应当看到：** 四行。第一行是 `PYTHONPATH=[]`；第二、三行是同一个 `site-packages` 目录；第四行是它下面的 `vllm_ascend/distributed/kv_transfer/kv_p2p/mooncake_connector.py`。第 3 步的 `vllm=`、`ascend=` 两行都是 `from=site cands=1`。

**不对时：** 第一行方括号里不为空，或者第 3 步不是 `from=site cands=1`：停下，带回这四行和第 3 步那两行。

#### 第 5 步　核对要改的文件和现场的本地补丁

**机器：** 容器里，接着第 4 步。

**命令：**

```bash
K=/tmp/t51-kit/patches
(cd "$VLLM_ROOT" && LC_ALL=C sha256sum -c $K/vllm/SHA256SUMS.before | grep -v ': OK$'; for f in $(cat $K/vllm/NEW_FILES.txt); do [ -e "$f" ] && echo "已存在：$f"; done)
(cd "$ASCEND_ROOT" && LC_ALL=C sha256sum -c $K/vllm-ascend/SHA256SUMS.before | grep -v ': OK$'; for f in $(cat $K/vllm-ascend/NEW_FILES.txt); do [ -e "$f" ] && echo "已存在：$f"; done)
sha256sum "$F" | cut -c1-16
(cd "$ASCEND_PKG/patch" && for f in __init__.py platform/__init__.py platform/patch_kv_load_failure_reason.py platform/patch_prefill_length_validation.py; do [ -e "$f" ] && echo "$f $(sha256sum "$f" | cut -c1-16)" || echo "$f 没有"; done)
```

再看现场两个本地补丁是不是调用原函数（只读；D 上只有第一个文件）：

```bash
grep -nE "original|_orig|kwargs" "$ASCEND_PKG"/patch/platform/patch_kv_load_failure_reason.py "$ASCEND_PKG"/patch/platform/patch_prefill_length_validation.py
```

**应当看到：** 第一段的前两条没有任何输出；第三条是 `2c4acb3eeb1ced7d`；最后四行是文件名和 sha256 前 16 位：

| 文件 | P | D |
|---|---|---|
| `__init__.py` | 第 3 步 `site_patches` 的 `patch/__init__.py` | 同左（D 自己那一次的值） |
| `platform/__init__.py` | 第 3 步 `site_patches` 的 `patch/platform/__init__.py` | 同左（D 自己那一次的值） |
| `platform/patch_kv_load_failure_reason.py` | `d7c764bb425993c0` | `d7c764bb425993c0` |
| `platform/patch_prefill_length_validation.py` | `c36392a6338bc2f1` | `platform/patch_prefill_length_validation.py 没有` |

`grep` 列出调用原函数（`original`、`_orig` 一类名字）的行；`patch_prefill_length_validation.py` 里 `_add_request` 的包装把 `**kwargs` 原样传给原函数。

**不对时：**

| 看到 | 怎么办 |
|---|---|
| `FAILED`、「已存在」，或者后五行和上表不同 | 停下，带回输出：文件已经被改过、已经装过补丁，或者现场的本地补丁变了 |
| `grep` 显示 `**kwargs` 没有传下去 | 照常往下做（服务不受影响，P 的 add_request 段会为空），带回这条命令的输出 |

#### 第 6 步　备份，拷回监控机（必做）

**机器：** 容器里执行第一条；然后在那台机器上执行第二条；最后在监控机上执行后两条。

**命令：**

```bash
BKBASE=/tmp; [ "$(stat -f -c %T /tmp)" = tmpfs ] && BKBASE=/root
BK=$BKBASE/t51-glm53-backup-$(date +%Y%m%d-%H%M%S); mkdir -p "$BK"; echo "$BK"
(cd "$VLLM_ROOT" && tar -cf "$BK/vllm.tar" $(awk '{print $2}' $K/vllm/SHA256SUMS.before))
(cd "$ASCEND_ROOT" && tar -cf "$BK/vllm-ascend.tar" $(awk '{print $2}' $K/vllm-ascend/SHA256SUMS.before) vllm_ascend/distributed/kv_transfer/kv_p2p/mooncake_connector.py)
(cd "$BK" && sha256sum vllm.tar vllm-ascend.tar > SHA256SUMS && tar -tf vllm.tar | wc -l && tar -tf vllm-ascend.tar | wc -l && awk '{print substr($1, 1, 16), $2}' SHA256SUMS)
```

```bash
docker cp <容器名>:<备份目录> /tmp/
```

```bash
mkdir -p <用户目录>/vllm-pd-patch-glm53/backup && scp -r <SSH 账号>@<机器地址>:/tmp/t51-glm53-backup-<时间戳> <用户目录>/vllm-pd-patch-glm53/backup/<机器>-<时间戳>
```

```bash
cd <用户目录>/vllm-pd-patch-glm53/backup/<机器>-<时间戳> && LC_ALL=C sha256sum -c SHA256SUMS && tar -tf vllm.tar | wc -l && tar -tf vllm-ascend.tar | wc -l
```

**应当看到：** 第一条打印备份目录名 `/tmp/t51-glm53-backup-<时间戳>`（`/tmp` 是 tmpfs 时在 `/root` 下），然后 `23`、`10` 和两个 tar 的 sha256 前 16 位。最后一条打印 `vllm.tar: OK`、`vllm-ascend.tar: OK`、`23`、`10`。把备份目录名记作 `<备份目录>`，末尾的 `<年月日>-<时分秒>` 记作 `<时间戳>`。

**不对时：** 数字不是 `23`、`10`，或者监控机上有 `FAILED`：重新拷；监控机上核对通过以前不做第 7 步。现场的 `mooncake_connector.py` 是本地修改版，补丁包里没有这一份，丢了无法重建。

**带回：** `<备份目录>`。备份合计约 2 MB，容器里、那台机器的 `/tmp` 下、监控机上各一份。

### 二、替换源文件（每个容器）

#### 第 7 步　替换 vLLM 和 vllm-ascend 主体

**机器：** 容器里，接着第 6 步。

**命令：** 先确认第 4、6 步设的变量都在：

```bash
: "${VLLM_PKG:?}" "${ASCEND_PKG:?}" "${VLLM_ROOT:?}" "${ASCEND_ROOT:?}" "${F:?}" "${K:?}" "${BK:?}" && echo "变量都在：$BK"
```

```bash
cp -R $K/vllm/files/vllm/. "$VLLM_PKG"/ && (cd "$VLLM_ROOT" && LC_ALL=C sha256sum -c $K/vllm/SHA256SUMS.after | grep -v ': OK$')
```

```bash
cp -R $K/vllm-ascend/files/vllm_ascend/. "$ASCEND_PKG"/ && (cd "$ASCEND_ROOT" && LC_ALL=C sha256sum -c $K/vllm-ascend/SHA256SUMS.after | grep -v ': OK$')
```

**应当看到：** 第一条打印 `变量都在：<备份目录>`；后两条没有任何输出（25 个、11 个文件都和交付的一致）。

**不对时：**

| 看到 | 怎么办 |
|---|---|
| 第一条报 `parameter null or not set` | 换过 shell：按第 4 步重新找目录，再设 `K=/tmp/t51-kit/patches` 和 `BK=<备份目录>`（不要重做备份），然后再执行 |
| 后两条有 `FAILED` | 停下，按第 22–26 步回滚 |

#### 第 8 步　在现场的 `mooncake_connector.py` 上重放 p2d

**机器：** 容器里，接着第 7 步。P、D 都要做。`replay_p2d.py` 的 sha256 是 `02304c8f76429780635870d17bac73558a2ecfafaadd744428c0ec93f457d4b3`。

**命令：** 只检查、不写文件：

```bash
python3 -I $K/p2d/replay_p2d.py "$F" --check; echo "exit=$?"
```

补充核对 X1–X6：

```bash
python3 -I -c "import sys; sys.path.insert(0, sys.argv[2]); import extra_checks; print(extra_checks.extra_line(open(sys.argv[1]).read()))" "$F" $K/p2d
```

写出打好补丁的文件：

```bash
python3 -I $K/p2d/replay_p2d.py "$F" --out /tmp/mooncake_connector.p2d.py; echo "exit=$?"
```

替换并核对：

```bash
cp /tmp/mooncake_connector.p2d.py "$F" && sha256sum "$F" | cut -c1-16
```

**应当看到：**

- 第一条最后两行是 `replay_p2d: check ok: 28 of 28 changes located, result checks passed, nothing written` 和 `exit=0`；28 行定位结果里只有 C25 是 `alternative_2`，其余都是 `primary`；`N01` 一行只是提示。
- 第二条：`p2d_extra X1=1 X2=1/1 X3=2 X4=4/1 X5=1 X6=ok`，没有一项带 `!`。
- 第三条最后两行：`replay_p2d: written out=/tmp/mooncake_connector.p2d.py sha256=<S> changes=28/28 py_compile=ok same_as_replacement=False` 和 `exit=0`。记下 `<S>`：所有 P、D 容器的 `<S>` 必须相同。`same_as_replacement=False` 是正常的。
- 第四条：`<S>` 的前 16 位。

**不对时：**

| 看到 | 怎么办 |
|---|---|
| 别的编号也用了备选锚点，或者 C25 回到 `primary` | 停下，带回输出：现场文件和小核对时不同 |
| `p2d_extra` 不是上面那一行 | 停下，带回两条命令的完整输出 |
| `<S>` 和前面做过的容器不同 | 停下，带回 |
| 退出码 1 | 参数错，读不了输入，或输入不是合法的 Python：核对路径和文件 |
| 退出码 2、3、4 | 不写文件，带回完整输出（2：某处改动的主锚点和备选锚点都没有恰好命中一行；3：结构判据不满足，`STOP Fxx`；4：重放结果的检查没通过） |
| 退出码 5 | 写出的文件 `py_compile` 不通过：不要用 `/tmp` 下的文件，带回输出 |
| 退出码 6 | 输入已经打过补丁：不要重复打，核对是不是已经替换过 |

退出码不为 0 时现场文件没有被动过。不要手工修改 `replay_p2d.py` 或现场文件去绕过检查。

**带回：** 三条 `replay_p2d.py`、`p2d_extra` 命令的完整输出和 `<S>`（P、D 各一份）。

### 三、接上面板二（每个容器）

#### 第 9 步　在启动脚本里加两行 export

**前提：** 面板手册第 1–15 步已经做完：第 15 步 `add` 是 `RESULT: OK`，两台机器的 `/data/pd-diag/conf` 里已经是 test-glm53 的三个节点文件；第 3 步核对过两台机器的 vLLM 容器都能在同一路径看到 `/data/pd-diag/conf` 和 `/data/pd-diag/logs`。所以节点目录沿用 test 的 `/data/pd-diag`，不加挂载，不重建容器。面板手册第 3 步有一台看不到时，那一步就已经停下：找我们，不要自己定目录。

**机器：** 容器里。两个环境变量写进 `<启动脚本>`，即第 3 步检测时 `--start-script` 传的那个文件（P、D 各用自己那次检测的启动文件）；一台机器一组，所有 rank 共用；写在宿主机的 shell 或别的脚本里不生效。

**放在哪：** 现场的启动文件里没有字面的 `vllm serve` 一行，vLLM 由启动器（`launch_online_dp.py`）拉起。两行放在脚本里第一次启动 vLLM 或启动器的命令之前；经启动器拉起的 `vllm serve` 子进程会继承这两个变量。示意（只有结构，不是现场脚本的内容）：

```text
#!/bin/bash
…                                   ← 脚本原有的设置，不动
export VLLM_LOGGING_CONFIG_PATH=…   ← 新加的两行（原文见下面）
export PYTHONPATH=…
<启动器或 vLLM 的命令> …            ← 脚本里第一次启动 vLLM 或启动器的那一行
```

**命令：** 先看脚本里是不是已经有了（test 留下的）：

```bash
grep -n -e 'VLLM_LOGGING_CONFIG_PATH' -e 'PYTHONPATH=.*pd-diag/conf' <启动脚本>
```

没有时，在 `<启动脚本>` 里第一次启动 vLLM 或启动器的命令之前加这两行；已经有这两行时不用重复加：

```
export VLLM_LOGGING_CONFIG_PATH=/data/pd-diag/conf/pd_diag_logging.json
export PYTHONPATH=/data/pd-diag/conf${PYTHONPATH:+:$PYTHONPATH}
```

加完以后再执行一次第一条命令。

**应当看到：** 加之前，第一条没有输出（还没有），或者正好列出上面这两行（test 留下的，不用再加）。加之后，第一条正好列出这两行，行号在第一次启动 vLLM 或启动器的那一行之前。这两行到第 11 步重启时才生效。`PYTHONPATH` 里多出的 `/data/pd-diag/conf` 只有面板二的三个节点文件，没有 `vllm`、`vllm_ascend`。

**怎么确认生效：** 重启以后，第 12 步读 `/proc/<pid>/environ` 的那条核对，每个 vLLM 进程都有 `VLLM_LOGGING_CONFIG_PATH`；第 13 步检测的 `run` 行是 `logcfg=set`。

**不对时：**

| 看到 | 怎么办 |
|---|---|
| 脚本里有这两个变量，但写法和上面不同（例如路径不是 `/data/pd-diag/conf`），或者写在第一次启动 vLLM 或启动器的命令之后 | 停下，不要改，带回这条命令的输出 |
| 重启以后第 12 步有「没有设」的行，或者第 13 步是 `logcfg=unset` | 两行放错了位置（不在第一次启动 vLLM 或启动器的命令之前、写在了宿主机的 shell 或别的脚本里），或者没有重启：这一批失败，按第 12 步的「不对时」带回 |

**带回：** 加之前、加之后两次 `grep` 的输出。

### 四、重启，分三批上线

**分三批：** 第 1 批是一台 D 机器的容器（这台机器上的全部 D 实例），第 2 批是其余 D 机器的容器，第 3 批是 P 机器的容器。测试集群只有一台 D 机器时，第 2 批是空的，第 1 批通过以后直接做第 3 批。每批的容器都做完第 1–9 步再重启。每批做第 10–21 步，这一批的检查都通过了，再做下一批。第 1、2 批做完、P 还没打补丁时，看板上只有 D 一侧的段，这是预期的。

#### 第 10 步　重启以前：记下命令行和磁盘

**机器：** 第一条在容器里（停实例之前）；第二条在这一批的每台机器上，宿主机里。`BK` 填 `<备份目录>`。

**命令：**

```bash
BK=<备份目录>
for d in /proc/[0-9]*; do a=$(tr '\0' ' ' < "$d/cmdline" 2>/dev/null); case "$a" in *"vllm serve"*|*vllm.entrypoints.openai.api_server*) echo "$a";; esac; done | LC_ALL=C sort | uniq -c > "$BK/cmdline.before"; cat "$BK/cmdline.before"
```

```bash
df -hT <节点目录> <vLLM 日志文件> | uniq
```

**应当看到：** 第一条每种命令行一行，前面的数是用这一行的进程个数；D 的容器里每个实例一种（端口各不相同），P 的容器里一种。第二条：表头一行，然后节点目录、vLLM 日志所在的文件系统各一行（两者在同一个文件系统上时只有一行）。

**带回：** 两条的输出（`df` 每台机器一份）。再记下停实例的时刻（第 20 步用）。

#### 第 11 步　停实例，第 1 批的那一个容器做导入核对，再拉起

**机器：** 容器里。

**怎么停、怎么起：** 现场容器的 1 号进程是 `bash`，vLLM 由 `launch_online_dp.py` 拉起。按平时的方式停掉这个容器里的 vLLM 实例，再用原来的启动文件拉起（D 是 `start_d0.sh`，和 `run_dp_template.sh` 同一目录）。参数和停之前相同；环境变量只多第 9 步的两行 `export`，这两行这次必须生效。不要加 `MC_LOG_DIR`。

**命令：** 只在第 1 批的那一个容器里、实例停掉以后、重新拉起以前执行（其余容器、其余批次都不做）：

```bash
python3 -B -c "import time, resource; t = time.time(); import vllm_ascend.distributed.kv_transfer.kv_p2p.mooncake_connector as m; print(hasattr(m.KVCacheRecvingThread, '_transfer_batch'), '%.1f s' % (time.time() - t), '%d MiB' % (resource.getrusage(resource.RUSAGE_SELF).ru_maxrss // 1024))"
```

**应当看到：** 一行 `True <用时> s <峰值内存> MiB`。用时至少 5 秒、内存至少 500 MiB（实验室 6–8 秒、约 500 MiB；现场还要加载 torch_npu 和 CANN，没有测过）。这条命令导入 torch、torch_npu、vLLM 和这个模块用到的部分，导入完就退出，不加载模型，不开端口。

**不对时：** 不是 `True`，或者报错：不要拉起，按第 22–26 步回滚这个容器，带回这一行。

**带回：** 这一行（第 1 批）。

#### 第 12 步　重启以后，在容器里核对

**机器：** `/health`、命令行、日志配置三项在容器里；最后一条在那台机器上，宿主机里。

**命令：** 每个实例都起来了（D 的容器一条、P 的容器一条）：

```bash
for p in $(seq 9100 9115); do printf "%s " "$p"; curl -s -o /dev/null -w '%{http_code}\n' localhost:$p/health; done
```

```bash
for p in 9900; do printf "%s " "$p"; curl -s -o /dev/null -w '%{http_code}\n' localhost:$p/health; done
```

命令行和重启前相同（`BK` 填 `<备份目录>`）：

```bash
BK=<备份目录>
for d in /proc/[0-9]*; do a=$(tr '\0' ' ' < "$d/cmdline" 2>/dev/null); case "$a" in *"vllm serve"*|*vllm.entrypoints.openai.api_server*) echo "$a";; esac; done | LC_ALL=C sort | uniq -c > "$BK/cmdline.after"; diff "$BK/cmdline.before" "$BK/cmdline.after" && echo "命令行和重启前相同"
```

面板二的日志配置在每个 vLLM 进程上都生效了：

```bash
for d in /proc/[0-9]*; do a=$(tr '\0' ' ' < "$d/cmdline" 2>/dev/null); case "$a" in *"vllm serve"*|*vllm.entrypoints.openai.api_server*) tr '\0' '\n' < "$d/environ" | grep -q '^VLLM_LOGGING_CONFIG_PATH=.*/pd_diag_logging\.json$' && echo "已设" || echo "没有设：pid ${d#/proc/}";; esac; done | sort | uniq -c
```

vLLM 日志里没有本套补丁引起的错误：

```bash
grep -nE -A12 "ImportError|Traceback" <vLLM 日志文件> | grep -E "ImportError|Traceback|pd_diagnostics|pd_device_timing|worker_base|ascend_store|mooncake_connector|recompute_scheduler" | head -20
```

**应当看到：** 每个端口一行，都是 `200`（D 16 行，P 1 行）；`命令行和重启前相同`；只有一行 `<进程个数> 已设`；最后一条没有输出。vLLM 照常启动；`[PD_DIAG]` 行不再出现在 vLLM 的标准输出里，改写进 `<节点目录>/logs/<端口>/` 下的文件。

**不对时：** 有 `diff` 的输出：停下，不做这一批后面的检查，带回 `cmdline.before`、`cmdline.after`。有「没有设」的行：第 9 步的两行 `export` 没有生效（不在第一次启动 vLLM 或启动器的命令之前、写在了宿主机的 shell 里，或者没有重启），这一批失败；停下，带回 `<启动脚本>` 里这两行附近的内容和这条核对的输出。其余几条不对，这一批算失败（见「判失败的条件」）。

**带回：** 比较结果那一行和日志配置那一行。

#### 第 13 步　用检测脚本核对

**机器：** 容器里，第 12 步通过以后。每个打过补丁的容器跑一次。

**命令：** P 的容器：

```bash
python3 -I -S -B /tmp/t51-kit/detect/t51_glm53_check.py --run-dp-template <启动文件目录>/run_dp_template.sh --launch-online-dp <启动文件目录>/launch_online_dp.py --start-script <启动文件目录>/start_p.sh --mooncake-json <启动文件目录>/mooncake.json --start-mooncake <启动文件目录>/start_mooncake.sh --startup-log <容器里的 vLLM 日志文件> --baseline <第 3 步的 baseline_token>
```

D 的容器：

```bash
python3 -I -S -B /tmp/t51-kit/detect/t51_glm53_check.py --run-dp-template <启动文件目录>/run_dp_template.sh --launch-online-dp <启动文件目录>/launch_online_dp.py --start-script <启动文件目录>/start_d0.sh --mooncake-json <启动文件目录>/mooncake.json --start-mooncake <启动文件目录>/start_mooncake.sh --startup-log <容器里的 vLLM 日志文件> --baseline <第 3 步的 baseline_token>
```

带回以后照第 3 步删掉明细文件。

**应当看到：**

- `verdict vllm=ok ascend=ok(site_local=<n>) p2d=ok mooncake=not_replaced`。`vllm=ok` 不带 `site_local`；出现 `vllm=ok(site_local=n)` 时带回来。
- `vllm=` 行 `state=v0.30.0+port`，行末 `port=25/25`；`ascend=` 行仍是 `state=modified`，行末 `port=11/11`。
- `strict` 行末是 `p2d=ok(changes=28/28,missing=none,struct=ok,extra=same,replacement_file=0)`。
- `site_patches` 一行和第 3 步相同；`run` 行里是 `logcfg=set`；单独一行 `baseline=same`。
- `mooncake=BAD(other)` 不算这一批失败，带回这一行，可选的 Mooncake 部分先不做。

**不对时：** 见「判失败的条件」第 13 步那几行。带回全部输出。

#### 第 14 步　启动日志里的三行 `PD_DIAG_STARTUP`

**机器：** 那台机器上，宿主机里。`device_timing` 那一行要在第一个真实请求执行以后才有。

**命令：**

```bash
grep -h 'PD_DIAG_STARTUP' <vLLM 日志文件> | sed 's/.*PD_DIAG_STARTUP //' | LC_ALL=C sort | uniq -c
```

**应当看到：** 每种行一行，前面是出现的次数（只看内容）：

- EngineCore，每个实例一行。P：`scheduler=vllm_ascend.patch.platform.patch_balance_schedule.BalanceScheduler balance=False recompute=False dyntra_lb=False batch_job=False profiling_chunk=False`；D：`scheduler=vllm_ascend.core.recompute_scheduler.AsyncRecomputeScheduler balance=False recompute=True dyntra_lb=False batch_job=False profiling_chunk=False`。
- Worker，每个 rank 两行：`worker model_runner=vllm_ascend.worker.model_runner_v1.NPUModelRunner tp_rank=<n>`，第一个真实请求执行完以后 `worker device_timing=<on|off> tp_rank=<n>`。
- `device_timing` 只在 `tp_rank=0` 上是 `on`。P（TP16）每个实例 1 行 `on tp_rank=0`、15 行 `off`（`tp_rank=1`…`15`），属正常；D（TP1）每个实例 1 行 `on tp_rank=0`。

**不对时：**

| 看到 | 怎么办 |
|---|---|
| `scheduler`、`model_runner` 的类不是上面这几种，或者 `BalanceScheduler` 带 `balance=True` | 这一批失败，带回这几行 |
| D，`tp_rank=0` 是 `off` | 这一批失败，带回这几行 |
| D，`tp_rank=0` 是 `?` | 带回，不算失败，交给第 16、18 步的留空比例判 |
| P，`tp_rank=0` 是 `off` 或 `?` | 带回，不算失败；这时看板上 P 的 forward、sampling 是空的 |
| 一行都没有 | 对照第 13 步的 `vllm=` 行，带回 |
| `device_timing` 那一行还没出现 | 还没执行过真实请求，等有请求以后再看 |

#### 第 15 步　监控机：过滤器自检（重启 5 分钟以后）

**机器：** 监控机。

**命令：**

```bash
cd <总目录>/vllm-pd-grafana-panel2/test-glm53 && bash -c '. bin/lib.sh && load_env >/dev/null && curl -s -u "vllm_reader:$CH_READER_PASSWORD" "http://localhost:$CLICKHOUSE_HTTP_PORT/" --data-binary @-' <<'SQL'
SELECT instance, sum(m) AS cand_maps, sum(f) AS forward_empty, sum(sa) AS sampling_empty, sum(e) AS no_execution_id FROM (SELECT instance, argMax(cand_maps, ts) AS m, argMax(acc_fallback['forward'], ts) AS f, argMax(acc_fallback['sampling'], ts) AS sa, argMax(exec_missing, ts) AS e FROM vllm_pd.filter_selfcheck WHERE ts > now() - INTERVAL 10 MINUTE GROUP BY instance, pid) GROUP BY instance ORDER BY instance FORMAT PrettyCompactMonoBlock
SQL
```

**应当看到：** 每个实例一行。这一批的 D 实例 `cand_maps` 大于 0；`no_execution_id` 是 0。`forward_empty`、`sampling_empty` 是没有设备时间、看板上留空的步数，按第 16、18 步判。

**不对时：** 这一批的 D 实例 `cand_maps` 是 0，或者 `no_execution_id` 大于 0：这一批失败。

**带回：** 这条查询的输出。

#### 第 16 步　监控机：留空比例第一次（重启 10 分钟以后）

**机器：** 监控机。`<这一批的实例>` 照第一页的写法填。只记录、不判。

**命令：**

```bash
cd <总目录>/vllm-pd-grafana-panel2/test-glm53 && bash -c '. bin/lib.sh && load_env >/dev/null && curl -s -u "vllm_reader:$CH_READER_PASSWORD" "http://localhost:$CLICKHOUSE_HTTP_PORT/" --data-binary @-' <<'SQL'
WITH now64(3, 'UTC') AS t10, cur AS (SELECT DISTINCT instance, pid FROM vllm_pd.filter_selfcheck WHERE ts > now() - INTERVAL 10 MINUTE), done AS (SELECT DISTINCT instance, request_id FROM vllm_pd.phase_line_cur WHERE proc = 'api' AND api_end_ts < now() - INTERVAL 1 MINUTE), ew AS (SELECT instance, uniqExact(request_id) AS ne FROM vllm_pd.phase_line_cur WHERE proc = 'api' AND api_end_ts >= t10 AND api_end_ts < now() - INTERVAL 1 MINUTE GROUP BY instance), emp AS (SELECT instance, arraySort(groupUniqArray(pid)) AS pl, length(pl) AS np, sum(f) AS fe, sum(sa) AS se FROM (SELECT instance, pid, argMax(acc_fallback['forward'], ts) AS f, argMax(acc_fallback['sampling'], ts) AS sa FROM vllm_pd.filter_selfcheck WHERE ts > now() - INTERVAL 10 MINUTE GROUP BY instance, pid) GROUP BY instance) SELECT i.instance AS instance, match(i.instance, '^(<这一批的实例>)$') AS this_batch, toString(t10) AS t10_utc, dateDiff('minute', t10, now()) AS window_min, ew.ne AS ended_win, emp.np AS pids, emp.pl AS pid_list, emp.fe AS forward_empty, emp.se AS sampling_empty, x.a_tot AS alloc_total, x.f_tot AS forward_total, x.s_tot AS sampling_total, x.a_win AS alloc_win, x.f_win AS forward_win, x.s_win AS sampling_win, if(x.a_win = 0, NULL, round(100 * (1 - x.f_win / x.a_win), 2)) AS forward_empty_pct, if(x.a_win = 0, NULL, round(100 * (1 - x.s_win / x.a_win), 2)) AS sampling_empty_pct FROM (SELECT DISTINCT instance FROM done) AS i LEFT JOIN (SELECT instance, sum(al) AS a_tot, sum(fw) AS f_tot, sum(sm) AS s_tot, sum(al - al0) AS a_win, sum(fw - fw0) AS f_win, sum(sm - sm0) AS s_win FROM (SELECT l.instance AS instance, l.request_id AS request_id, l.part AS part, toUInt32OrZero(splitByChar('-', l.part)[2]) AS pid, ifNull(max(l.acc__forward__steps), 0) AS fw, ifNull(maxIf(l.acc__forward__steps, l.ts < t10), 0) AS fw0, ifNull(max(l.acc__sampling__steps), 0) AS sm, ifNull(maxIf(l.acc__sampling__steps, l.ts < t10), 0) AS sm0, ifNull(max(l.acc__alloc__steps), 0) AS al, ifNull(maxIf(l.acc__alloc__steps, l.ts < t10), 0) AS al0 FROM vllm_pd.phase_line_cur AS l WHERE l.part != '' AND (l.instance, l.request_id) IN (SELECT instance, request_id FROM done) GROUP BY l.instance, l.request_id, l.part) WHERE (instance, pid) IN (SELECT instance, pid FROM cur) GROUP BY instance) AS x ON i.instance = x.instance LEFT JOIN emp ON i.instance = emp.instance LEFT JOIN ew ON i.instance = ew.instance ORDER BY this_batch DESC, instance SETTINGS join_use_nulls = 0 FORMAT PrettyCompactMonoBlock
SQL
```

**应当看到：** 每个实例一行，列含义见第 18 步。第一次执行时 `ended_win`、`alloc_win`、`forward_win`、`sampling_win` 是 0，两个比例为空（`ᴺᵁᴸᴸ`）。

**带回：** 整份输出。`t10_utc` 照抄下来（带毫秒），第 18 步要用；每个实例的 `pid_list` 也照抄，第 18 步对照。

#### 第 17 步　磁盘起点（重启 5 分钟以后，和第 15 步同时）

**机器：** 这一批的每台机器上，宿主机里。

**命令：**

```bash
N=<节点目录>/logs
{ date +%s; du -sb "$N" | cut -f1; for f in <vLLM 日志文件>; do echo "$f $(stat -c %s "$f")"; done; } > ~/t51-glm53-disk.t0 && echo "起点已记下：$(wc -l < ~/t51-glm53-disk.t0) 行"
```

**应当看到：** `起点已记下：<n> 行`，n 是 vLLM 日志文件的个数加 2（D 机器 18，P 机器 3）。

记下起点以后，随时可以先执行一次第 18 步的磁盘命令看 `[PD_DIAG]`：不是 0 行就不等满 30 分钟，马上按第 22–26 步回滚这一批。

#### 第 18 步　监控机和机器：留空比例第二次、磁盘第二次（重启约 35 分钟以后）

**机器：** 留空比例在监控机上；磁盘在这一批的每台机器上，宿主机里。

**命令：** 留空比例，把 `<第一次的 t10_utc>` 换成第 16 步打印的那个时刻：

```bash
cd <总目录>/vllm-pd-grafana-panel2/test-glm53 && bash -c '. bin/lib.sh && load_env >/dev/null && curl -s -u "vllm_reader:$CH_READER_PASSWORD" "http://localhost:$CLICKHOUSE_HTTP_PORT/" --data-binary @-' <<'SQL'
WITH toDateTime64('<第一次的 t10_utc>', 3, 'UTC') AS t10, cur AS (SELECT DISTINCT instance, pid FROM vllm_pd.filter_selfcheck WHERE ts > now() - INTERVAL 10 MINUTE), done AS (SELECT DISTINCT instance, request_id FROM vllm_pd.phase_line_cur WHERE proc = 'api' AND api_end_ts < now() - INTERVAL 1 MINUTE), ew AS (SELECT instance, uniqExact(request_id) AS ne FROM vllm_pd.phase_line_cur WHERE proc = 'api' AND api_end_ts >= t10 AND api_end_ts < now() - INTERVAL 1 MINUTE GROUP BY instance), emp AS (SELECT instance, arraySort(groupUniqArray(pid)) AS pl, length(pl) AS np, sum(f) AS fe, sum(sa) AS se FROM (SELECT instance, pid, argMax(acc_fallback['forward'], ts) AS f, argMax(acc_fallback['sampling'], ts) AS sa FROM vllm_pd.filter_selfcheck WHERE ts > now() - INTERVAL 10 MINUTE GROUP BY instance, pid) GROUP BY instance) SELECT i.instance AS instance, match(i.instance, '^(<这一批的实例>)$') AS this_batch, toString(t10) AS t10_utc, dateDiff('minute', t10, now()) AS window_min, ew.ne AS ended_win, emp.np AS pids, emp.pl AS pid_list, emp.fe AS forward_empty, emp.se AS sampling_empty, x.a_tot AS alloc_total, x.f_tot AS forward_total, x.s_tot AS sampling_total, x.a_win AS alloc_win, x.f_win AS forward_win, x.s_win AS sampling_win, if(x.a_win = 0, NULL, round(100 * (1 - x.f_win / x.a_win), 2)) AS forward_empty_pct, if(x.a_win = 0, NULL, round(100 * (1 - x.s_win / x.a_win), 2)) AS sampling_empty_pct FROM (SELECT DISTINCT instance FROM done) AS i LEFT JOIN (SELECT instance, sum(al) AS a_tot, sum(fw) AS f_tot, sum(sm) AS s_tot, sum(al - al0) AS a_win, sum(fw - fw0) AS f_win, sum(sm - sm0) AS s_win FROM (SELECT l.instance AS instance, l.request_id AS request_id, l.part AS part, toUInt32OrZero(splitByChar('-', l.part)[2]) AS pid, ifNull(max(l.acc__forward__steps), 0) AS fw, ifNull(maxIf(l.acc__forward__steps, l.ts < t10), 0) AS fw0, ifNull(max(l.acc__sampling__steps), 0) AS sm, ifNull(maxIf(l.acc__sampling__steps, l.ts < t10), 0) AS sm0, ifNull(max(l.acc__alloc__steps), 0) AS al, ifNull(maxIf(l.acc__alloc__steps, l.ts < t10), 0) AS al0 FROM vllm_pd.phase_line_cur AS l WHERE l.part != '' AND (l.instance, l.request_id) IN (SELECT instance, request_id FROM done) GROUP BY l.instance, l.request_id, l.part) WHERE (instance, pid) IN (SELECT instance, pid FROM cur) GROUP BY instance) AS x ON i.instance = x.instance LEFT JOIN emp ON i.instance = emp.instance LEFT JOIN ew ON i.instance = ew.instance ORDER BY this_batch DESC, instance SETTINGS join_use_nulls = 0 FORMAT PrettyCompactMonoBlock
SQL
```

磁盘：

```bash
N=<节点目录>/logs; S0=~/t51-glm53-disk.t0; t0=$(sed -n 1p "$S0"); n0=$(sed -n 2p "$S0"); t1=$(date +%s); n1=$(du -sb "$N" | cut -f1); b=0; p=0
while read -r f s0; do b=$((b + $(tail -c +$((s0 + 1)) "$f" | grep -a '\[BatchPutTiming\]' | wc -c))); p=$((p + $(tail -c +$((s0 + 1)) "$f" | grep -a -c '\[PD_DIAG\]'))); done < <(sed -n '3,$p' "$S0")
awk -v t=$((t1 - t0)) -v n=$((n1 - n0)) -v b=$b -v p=$p 'BEGIN { printf "%d 分钟：节点目录每天 %.1f GB；vLLM 日志 [BatchPutTiming] 每天 %.2f GB，[PD_DIAG] %d 行\n", t / 60, n / t * 86400 / 1e9, b / t * 86400 / 1e9, p }'
```

**应当看到：** 留空比例每个实例一行：

- `this_batch`：`1` 是这一批的实例，`0` 是之前批次的实例，排在后面，只作参考。
- `window_min`：`t10_utc` 到现在的分钟数，应当在 25 左右。
- `ended_win`：`t10_utc` 以后、现在 1 分钟以前结束的请求数。
- `pid_list`、`pids`：最近 10 分钟写过自检行的进程号和个数，和第 16 步相同。
- `forward_empty`、`sampling_empty`：留空次数（进程启动以来累计）。
- `alloc_total`、`forward_total`、`sampling_total`：已经结束 1 分钟以上的请求按步累加的步数；`alloc_win`、`forward_win`、`sampling_win`：同样的步数，只算 `t10_utc` 以后。
- `forward_empty_pct` = 1 − `forward_win` ÷ `alloc_win`，`sampling_empty_pct` 同理。比例可能是零点几的负数，按 0 看。留空次数只看增没增加，不能和比例相除。

磁盘一行，例如 `30 分钟：节点目录每天 <x> GB；vLLM 日志 [BatchPutTiming] 每天 <y> GB，[PD_DIAG] 0 行`。这 30 分钟里有 vLLM 日志文件被轮转了的，vLLM 日志一项会偏小：重新记起点再量。

**怎么判（只判 `this_batch` 是 1 的行，forward、sampling 各自判，每个实例单独判）：** 先看下面三条，再看留空比例表。

| 看到 | 结果 |
|---|---|
| `window_min` 明显大于 35 | `t10_utc` 抄错了，或者抄成了上一批的：重抄这一批第 16 步的 `t10_utc`，再执行；这一次不判 |
| 两次的 `pid_list` 不同 | 进程重启过，两次不可比：D 这一批失败；P 带回。带回两次输出 |
| 比例为空，`ended_win` 大于 0，`forward_win` 或 `sampling_win` 大于 0 | 调度器侧断了：这一批失败（D、P 都算），带回 |
| 比例为空，`ended_win` 大于 0，三个 `*_win` 都是 0 | 不直接判失败，带回 |
| 比例为空，`ended_win` 是 0 | 没判成，不算通过，带回 |

| 比例 | 留空次数（第 18 步比第 16 步） | 结果 |
|---|---|---|
| 超过 1% | 增加了 | D：这一批失败。P：带回，不算失败 |
| 超过 1% | 没有增加 | 不判失败：两侧步数对不上，带回 |
| 不超过 1% | 增加了 | 通过。记下增加了多少，带回 |
| 不超过 1% | 没有增加 | 通过 |

磁盘按「判失败的条件」里的磁盘项判。

**带回：** 留空比例两次的输出（D、P 每个实例都要；第 1 批另要每个 D 实例的 `*_total`、`*_win` 和 `forward_empty`、`sampling_empty`），磁盘那一行（每台机器）。

#### 第 19 步　面板二：升级安全检查和看板上的段

**机器：** 监控机。重启以后至少 3 分钟再做；`vars.sh` 是面板手册第 1 步写好的。每批都做。

**命令：**

```bash
. ~/vllm-pd-test-glm53/vars.sh && cd "$D" && bin/panel2_cluster.sh check "$C"
```

**应当看到：**

- **每批：** `过滤器版本` 一行里，这一批的实例都是 `pd-diag-v3.1-extra-glm53-2026-11-19c@<8 位>`。
- **最后一批以前：** 还没重启的批次（P）的实例不写记录，`check` 会有只涉及这些实例的 `FAIL` 项，最后一行是 `RESULT: FAIL`。只涉及还没重启批次（P）实例的 `FAIL` 项，不算这一批失败，带回全部输出。有两种：
  - **没在写：** `FAIL 过滤器：<P 的实例> 这次启动以后没有记录，过滤器也没有创建 → bin/diag.sh vllm 看结论`，和同样的 `FAIL 版本：<P 的实例> …`。P 的 vLLM 没打补丁、也没设环境变量时是这样；未打补丁的 vLLM 上旧过滤器是静默的，不写记录，也不写自检行。
  - **旧版本：** P 带着更早一版补丁、正在写记录时，`FAIL 过滤器版本：两个以上版本同时在写：…`。对照那一行括号里的进程个数：`…-19c` 的是这一批的进程，多出来的那个版本的进程个数和 P 的进程数相同。
  - 这一批的实例在 `过滤器版本` 里都是 `…-19c`，而且没有出现在任何 `FAIL` 行里；不点名实例的 `FAIL` 项（库、容器、拉取等）照常算这一批失败。
- **最后一批做完以后：** 每项 `OK`，其中 `库：表和视图都在，视图读的是 pd-diag-v3.1-extra-glm53-2026-11-19c`、`过滤器版本：<n> 个实例 <n> 个进程都是 pd-diag-v3.1-extra-glm53-2026-11-19c@<8 位>`，最后一行 `RESULT: OK`。

看板上应当有值的段：

| 做完第几批 | 有值的段（只有经过已打补丁实例的请求才有值） | 仍为空的段 |
|---|---|---|
| 第 1、2 批（D） | D 的 req_resolve、tokenizer、add_request、schedule 和 queue_time、alloc、other，start_load_kv 和 submit、wait_for_transfer，forward、sampling、detokenizer；D 第二步起的 schedule、alloc、other、forward、sampling、detokenizer | P 的全部段（P 还没打补丁）；P、D 的 lb_proxy（proxy 采集打开时有值） |
| 第 3 批（P） | 上面这些，加上 P 的 req_resolve、tokenizer、add_request、schedule 和 queue_time、hash、lookup、alloc、other，wait_for_put，forward、sampling、detokenizer，后台 wait_for_save 和它的 5 个子段；P 的 start_load_kv 和 submit 只在命中缓存池的请求上有值；做了 Mooncake 部分的，后台 wait_for_save/put 下的 3 段 | P 的 api_gateway、P 的 start_load_kv/wait_for_transfer（按设计为空）；没做 Mooncake 部分时，put 下的 3 段；P、D 的 lb_proxy（做完 proxy 手册以后有值） |

第 3 批做完时 47 段里 40 段有值；做完 proxy 手册以后 42 段；再做 Mooncake 部分以后 45 段。段数不是判据。第 5 步的 `grep` 显示 `**kwargs` 没有传下去时，P 的 add_request 段为空。

**不对时：** 这一批的实例在 `过滤器版本` 里不是 `…-19c` 那一版；最后一批做完以后，`check` 不是 `RESULT: OK`，或者这一批的实例不是 `…-19c` 那一版：这一批失败，带回全部输出。最后一批以前，只涉及还没重启批次（P）实例的 `FAIL` 项（没在写、旧版本两种）：不算这一批失败，带回全部输出。重启 10 分钟以后、这一批的实例收到过请求，D 的 forward、sampling、start_load_kv/wait_for_transfer 仍然全为空（第 3 批看 P 的 forward、sampling、schedule）：这一批失败。

#### 第 20 步　面板一：TTFT、TPOT 中位数（观测项，不判失败）

**机器：** 面板一 Decode 看板「TPOT P50 / P90 / P99 / Max」面板的菜单里点 Explore，把查询换成下面的语句，查询类型选 Instant。「重启前」把时间范围的结束时刻设成第 10 步记下的停实例时刻；「重启后」设成重启以后 40 分钟。

**命令：** D 的批（第 1、2 批）比 TTFT 和 TPOT：

```text
histogram_quantile(0.5, sum by (le) (increase(vllm:time_to_first_token_seconds_bucket{role="decode", pd_instance=~"<这一批的实例>"}[30m])))
```

```text
histogram_quantile(0.5, sum by (le) (increase(vllm:request_time_per_output_token_seconds_bucket{role="decode", pd_instance=~"<这一批的实例>"}[30m])))
```

P 的批（第 3 批）只比 TTFT（P 每个请求只出 1 个 token，TPOT 记成 0）：

```text
histogram_quantile(0.5, sum by (le) (increase(vllm:time_to_first_token_seconds_bucket{role="prefill", pd_instance=~"P.*"}[30m])))
```

**应当看到：** 每条一个数，单位是秒。

**不对时：** 重启后比重启前大 10% 以上：报给我们，由我们决定要不要回滚。这一项只作参考，不算这一批失败。

**带回：** 每批重启前、重启后的 TTFT（D 的批还有 TPOT）。

#### 第 21 步　这一批的结论

按「判失败的条件」逐条看这一批。都通过：做下一批（回到第 10 步）。有一条失败：停下，不做下一批；这一批的每个容器按第 22–26 步回滚，再按第 11 步的方式重启一次；已经成功的批不动；带回这一批的检查输出、vLLM 启动日志和第 15 步那条查询的输出。

### 五、回滚（只在失败时做，每个要回滚的容器）

要连面板二一起退回时，先找我们：面板手册里没有退回面板二的步骤。做过 Mooncake 部分的，同时做第 35 步，也只重启一次。

#### 第 22 步　把监控机上的备份送回容器

**机器：** 监控机执行第一条，那台机器执行第二条，容器里执行第三条。

**命令：**

```bash
ssh <SSH 账号>@<机器地址> mkdir -p /tmp/<机器>-<时间戳> && scp <用户目录>/vllm-pd-patch-glm53/backup/<机器>-<时间戳>/* <SSH 账号>@<机器地址>:/tmp/<机器>-<时间戳>/
```

```bash
docker cp /tmp/<机器>-<时间戳>/. <容器名>:/tmp/<机器>-<时间戳>
```

```bash
cd /tmp/<机器>-<时间戳> && LC_ALL=C sha256sum -c SHA256SUMS && tar -tf vllm.tar | wc -l && tar -tf vllm-ascend.tar | wc -l
```

**应当看到：** 最后一条打印 `vllm.tar: OK`、`vllm-ascend.tar: OK`，`23`、`10`。

#### 第 23 步　重做补丁包解开和找目录

**机器：** 容器里。容器里的 `/tmp/t51-kit` 不在了时，先用那台机器上留着的补丁包重做第 2 步的前两条（拷进容器、解开核对）；再执行第 4 步的命令，重新设好 `VLLM_PKG`、`ASCEND_PKG`、`VLLM_ROOT`、`ASCEND_ROOT`、`F`。

**应当看到：** 和第 2、4 步相同。

#### 第 24 步　恢复三件

**机器：** 容器里。

**命令：**

```bash
BK=/tmp/<机器>-<时间戳>; K=/tmp/t51-kit/patches
tar -xf "$BK/vllm.tar" -C "$VLLM_ROOT" && (cd "$VLLM_ROOT" && rm -f $(cat $K/vllm/NEW_FILES.txt) && LC_ALL=C sha256sum -c $K/vllm/SHA256SUMS.before | grep -v ': OK$')
tar -xf "$BK/vllm-ascend.tar" -C "$ASCEND_ROOT" && (cd "$ASCEND_ROOT" && rm -f $(cat $K/vllm-ascend/NEW_FILES.txt) && LC_ALL=C sha256sum -c $K/vllm-ascend/SHA256SUMS.before | grep -v ': OK$')
sha256sum "$F" | cut -c1-16
```

**应当看到：** 前两条没有任何输出；最后一行是 `2c4acb3eeb1ced7d`。

**不对时：** 停下，带回输出。

#### 第 25 步　删掉新模块的编译缓存

**机器：** 容器里。

**命令：**

```bash
for f in $(sed "s#^#$VLLM_ROOT/#" $K/vllm/NEW_FILES.txt) $(sed "s#^#$ASCEND_ROOT/#" $K/vllm-ascend/NEW_FILES.txt); do rm -f "$(dirname "$f")/__pycache__/$(basename "$f" .py)".*.pyc; ls "$(dirname "$f")/__pycache__/$(basename "$f" .py)".*.pyc 2>/dev/null; done; echo "新模块的 .pyc 已删"
```

**应当看到：** 只有 `新模块的 .pyc 已删` 一行。

#### 第 26 步　重启

按第 11 步的方式重启（不做导入核对），重启前后照第 10、12 步核对命令行（`BK` 用第 24 步的目录）。重启以后，`[PD_DIAG]` 记录不再写出，面板二上这个容器的实例没有新数据。

### 六、可选：替换 Mooncake 库

前 26 步不依赖这一部分，可以以后单独做。不做时，P 侧「后台 wait_for_save → put」下的 3 个子段 submit、wait_for_transfer、finalize 为空，父段 put 仍有值。做完以后这 3 段有值；做完 proxy 手册以后，47 段里 45 段有值；各实例的 vLLM 日志多出 `[BatchPutTiming]` 行。

**前提：** 第 1–21 步三批都已通过；第 13 步报 `mooncake=not_replaced`（不是 `BAD(other)`）；第 18 步在这一部分以前就量到的 `[BatchPutTiming]` 接近 0。不满足时先不做，带回。

**用的文件：** Mooncake 增量包 `t51-glm53-mooncake-kit.tar`（sha256 `6d70978c…`），放在监控机的 `<用户目录>/vllm-pd-patch-glm53/` 下，连同它的 `.sha256`。里面有替换库 `lib/libmooncake_store.so`（15402248 字节，sha256 `3131784385846064327e858d6dcdb11a6c7674c2162964d52f9ed0e866b52bb8`）和换库前的只读核对 `precheck/dist/t51_mc_precheck.py`（glm53mc-5）。

**怎么分批：** 每个容器只做一次第 27–30 步，第 31 步起才分批；分批和第四部分相同。换库到这个容器最后一个实例重启之间可能隔几个小时，这段时间里没重启的实例继续用原库。这样做的前提：补丁 04 只给 `Client` 加了三个非虚的私有成员函数声明，没有加数据成员、static 成员和 thread_local，没有虚函数，没有动虚表，所以两种构建同时在不同进程里运行时，布局和进程间传递的数据格式都相同。

**几条约定：** 只换 `<Mooncake 目录>/libmooncake_store.so` 一个文件，store 模块、兄弟库和自带的系统库都不动。换库用改名，不覆盖。备份不能放在 `<Mooncake 目录>` 或任何 `mooncake*.libs` 目录里。不设 `MC_LOG_DIR`。

这一部分另有几个占位符：`<Mooncake 目录>`（第 29 步第一条命令的输出，即 `site-packages/mooncake`）、`<MC 备份目录>`、`<MC 时间戳>`（第 29 步的输出）、`<原库 sha256>`（第 29 步记下的 64 位）。

#### 第 27 步　放进容器并核对

**机器：** 监控机执行前两条，那台机器执行第三条，容器里执行最后一条。

**命令：**

```bash
cd <用户目录>/vllm-pd-patch-glm53 && LC_ALL=C sha256sum -c t51-glm53-mooncake-kit.tar.sha256
```

```bash
scp <用户目录>/vllm-pd-patch-glm53/t51-glm53-mooncake-kit.tar <SSH 账号>@<机器地址>:/tmp/
```

```bash
docker cp /tmp/t51-glm53-mooncake-kit.tar <容器名>:/tmp/
```

```bash
mkdir -p /tmp/t51-mc && tar -xf /tmp/t51-glm53-mooncake-kit.tar -C /tmp/t51-mc && cd /tmp/t51-mc && LC_ALL=C sha256sum -c SHA256SUMS | grep -v ': OK$'; sha256sum /tmp/t51-kit/detect/t51_glm53_check.py | cut -c1-16
```

**应当看到：** 第一条 `t51-glm53-mooncake-kit.tar: OK`；最后一条只有一行 `f9e6073f692dd4ea`。

**不对时：** 最后一条前面有输出：增量包在拷贝中损坏，重新拷贝。最后一行报 `No such file or directory`：容器的 `/tmp` 被清空过，先用那台机器上留着的补丁包重做第 2 步的前两条（拷进容器、解开核对），再执行。

#### 第 28 步　换库前的核对（最后一道关）

**机器：** 容器里，在 vLLM 正在运行时跑。每个要换库的容器都跑一次。只读，几秒内结束，不导入 Mooncake，不写文件，不起子进程，不联网。

**命令：**

```bash
python3 -I -S -B /tmp/t51-mc/precheck/dist/t51_mc_precheck.py; echo "exit=$?"
```

**应当看到：** 12 行，然后 `exit=0`。其中：

- 第一行 `t51_mc_precheck=glm53mc-5 build=78566d4-rwdi-ut-nouring-mp9.0.0/repl-p04i-0625b51e pid=<n> loaded=<n>`，`pid=` 不是 `none`。
- `mc_orig size=17610953 sha16=3bf82f8b0c5ab715 f1=same …`，`second_store=none`。
- `mc_build sections_other=same … result=same_build`，`actual=` 和 `predicted=` 相同，`prefixes=1`。
- `mc_exports … differing_families=none replacement_vs_site=-0/+3(expected -0/+3)`。
- `consumers_ok n_elf=<n> removed_names=0`。
- 最后一行 `mc_pre result=ok_replace`。

**不对时：** 最后一行不是 `mc_pre result=ok_replace`（`STOP_…`，退出码 2），或者只打印两行 `mc_pre result=STOP_missing`，或者打印一行 `REFUSED`：这个容器不换，其余容器也先不换，带回整段输出。各行含义和判决规则见增量包的 `precheck/使用说明.zh-CN.md`。

**带回：** 全部输出，每个容器一份。

#### 第 29 步　找到 Mooncake 目录，备份原库，拷回监控机（必做）

**机器：** 前三条在容器里；第四条在那台机器上；最后两条在监控机上。

**命令：**

```bash
MC_PKG=$(python3 -I -B -c "import importlib.util as u; print(list(u.find_spec('mooncake').submodule_search_locations)[0])"); MC_LIB="$MC_PKG/libmooncake_store.so"
echo "$MC_LIB"; ls -l "$MC_LIB" | awk '{print $1, $5}'
for d in /proc/[0-9]*; do grep -h 'libmooncake_store' "$d/maps" 2>/dev/null | awk '{print $6, $7}' | LC_ALL=C sort -u; done | LC_ALL=C sort | uniq -c
```

```bash
: > /tmp/t51-mc/vllm.ld_library_path; for d in /proc/[0-9]*; do a=$(tr '\0' ' ' < "$d/cmdline" 2>/dev/null); case "$a" in *"vllm serve"*|*vllm.entrypoints.openai.api_server*) tr '\0' '\n' < "$d/environ" | grep '^LD_LIBRARY_PATH=' > /tmp/t51-mc/vllm.ld_library_path; break;; esac; done; echo "vLLM 的 LD_LIBRARY_PATH 已记下：$(wc -c < /tmp/t51-mc/vllm.ld_library_path) 字节"
```

```bash
BKBASE=/tmp; [ "$(stat -f -c %T /tmp)" = tmpfs ] && BKBASE=/root
BKMC=$BKBASE/t51-glm53-mc-backup-$(date +%Y%m%d-%H%M%S); mkdir -p "$BKMC"; echo "$BKMC"
cp -p "$MC_LIB" "$BKMC/libmooncake_store.so" && (cd "$BKMC" && sha256sum libmooncake_store.so > SHA256SUMS && cat SHA256SUMS)
```

```bash
docker cp <容器名>:<MC 备份目录> /tmp/
```

```bash
mkdir -p <用户目录>/vllm-pd-patch-glm53/backup && scp -r <SSH 账号>@<机器地址>:/tmp/t51-glm53-mc-backup-<MC 时间戳> <用户目录>/vllm-pd-patch-glm53/backup/<机器>-mc-<MC 时间戳>
```

```bash
cd <用户目录>/vllm-pd-patch-glm53/backup/<机器>-mc-<MC 时间戳> && LC_ALL=C sha256sum -c SHA256SUMS
```

**应当看到：**

- 第一条：`<Mooncake 目录>/libmooncake_store.so`；权限和大小，大小是 `17610953`；最后只有一行 `<进程个数> <和第一行相同的路径>`。
- 第二条：`vLLM 的 LD_LIBRARY_PATH 已记下：<n> 字节`，n 大于 0（是 0 时第 31 步的导入核对用当前 shell 的环境，照常往下做）。
- 第三条：备份目录名 `/tmp/t51-glm53-mc-backup-<MC 时间戳>`（`/tmp` 是 tmpfs 时在 `/root` 下），然后一行 `<64 位> libmooncake_store.so`，前 16 位是 `3bf82f8b0c5ab715`。记下备份目录名（`<MC 备份目录>`）和这 64 位（`<原库 sha256>`）。
- 最后一条：`libmooncake_store.so: OK`。

**不对时：**

| 看到 | 怎么办 |
|---|---|
| 第一条最后一部分的路径和第一行不同、不止一行，或者行末带 `(deleted)` | 停下，带回这几行。本容器已经做过第 30 步的，这一条不适用，不要重跑第 29 步 |
| 前 16 位不是 `3bf82f8b0c5ab715` | 停下，带回 |
| 监控机上 `FAILED` | 重新拷；核对通过以前不做第 30 步 |

#### 第 30 步　换库

**机器：** 容器里，接着第 29 步。

**命令：** 先确认变量都在：

```bash
: "${MC_PKG:?}" "${MC_LIB:?}" "${BKMC:?}" && echo "变量都在：$BKMC"
```

```bash
cp /tmp/t51-mc/lib/libmooncake_store.so "$MC_PKG/.t51_new_store.so" && chmod --reference="$MC_LIB" "$MC_PKG/.t51_new_store.so" && chown --reference="$MC_LIB" "$MC_PKG/.t51_new_store.so" && mv -f "$MC_PKG/.t51_new_store.so" "$MC_LIB" && sha256sum "$MC_LIB" | cut -c1-16 && ls -l "$MC_LIB" | awk '{print $1, $5}'
```

再看运行中的进程：

```bash
for d in /proc/[0-9]*; do grep -h 'libmooncake_store' "$d/maps" 2>/dev/null | awk '{print $6, $7}' | LC_ALL=C sort -u; done | LC_ALL=C sort | uniq -c
```

**应当看到：** 第一条 `变量都在：<MC 备份目录>`；第二条 `3131784385846064`，然后权限（和第 29 步相同）和大小 `15402248`；第三条一行 `<进程个数> <Mooncake 目录>/libmooncake_store.so (deleted)`，进程个数和第 29 步相同。运行中的 vLLM 继续用它已经映射的原库，重启以后才加载新库。

**不对时：**

| 看到 | 怎么办 |
|---|---|
| 第一条报 `parameter null or not set` | 换过 shell：按第 29 步第一条重新设好 `MC_PKG`、`MC_LIB`，再设 `BKMC=<MC 备份目录>`（不要重做备份） |
| 第二条中途报错、没有打印 `3131784385846064`（例如 `mv` 没成功） | 先 `rm -f "$MC_PKG/.t51_new_store.so"`，再执行一次 |
| sha256 不是 `3131784385846064` | 停下，按第 35 步回滚 |
| 进程个数比第 29 步少，路径对、行末都是 `(deleted)` | 有进程退出了：记下两次的个数，继续 |
| 出现行末没有 `(deleted)` 的行 | 已经有进程加载了新库：停下，带回 |

#### 第 31 步　分批重启，第 1 批的那一个容器做导入核对

**机器：** 容器里。重启照第 11 步的方式；重启前后照第 10、12 步核对命令行和日志配置，`BK` 用 `<MC 备份目录>`。

**命令：** 只在第 1 批的那一个容器里、实例停掉以后、重新拉起以前执行：

```bash
(L=$(cat /tmp/t51-mc/vllm.ld_library_path); [ -n "$L" ] && export "$L"; python3 -B -c "import time; t = time.time(); import mooncake.store as s; print(s.__file__, '%.1f s' % (time.time() - t))")
```

**应当看到：** 一行 `<Mooncake 目录>/store.cpython-311-aarch64-linux-gnu.so <用时> s`。

**不对时：** 报任何错误时，不要用新库拉起：按第 35 步把这个容器回滚到原库，再拉起，带回这一行和 `/tmp/t51-mc/vllm.ld_library_path` 的字节数。`undefined symbol` 是新库和现场的库对不上；`libascend_hal.so: cannot open shared object file` 一类找不到 CANN、驱动库的错误，多半是导入时的环境和 vLLM 不同，由我们看过再决定。

**带回：** 这一行（第 1 批）。

#### 第 32 步　重启以后，按实例看用的是哪一份库

**机器：** 第一条在容器里；第二条在那台机器上，宿主机里。

**命令：**

```bash
port_of() { local p=$1 q; while [ -n "$p" ] && [ "$p" -gt 1 ]; do q=$(tr '\0' ' ' < /proc/$p/cmdline 2>/dev/null | grep -oE -- '--port[ =][0-9]+' | grep -oE '[0-9]+$' | head -1); [ -n "$q" ] && { echo "$q"; return; }; p=$(awk '/^PPid:/{print $2}' /proc/$p/status 2>/dev/null); done; echo '?'; }
for d in /proc/[0-9]*; do m=$(grep -h 'libmooncake_store' "$d/maps" 2>/dev/null | awk '{print $6, $7}' | LC_ALL=C sort -u); [ -n "$m" ] || continue; case "$m" in *"(deleted)"*) u=old;; *) u=new;; esac; echo "port=$(port_of ${d#/proc/}) $u"; done | LC_ALL=C sort | uniq -c
```

```bash
grep -nE "undefined symbol|libmooncake_store" <vLLM 日志文件> | head -20
```

**应当看到：** 第一条每个端口一行或两行：`<进程个数> port=<端口> new` 或 `<进程个数> port=<端口> old`（`new` 用新库，`old` 还在用原库；每个进程沿着父进程往上，按第一个带 `--port` 的命令行对到实例）。这一批的端口只有 `new`；本容器还没轮到的实例是 `old`，属预期。第二条没有输出。

**不对时：**

| 看到 | 怎么办 |
|---|---|
| 这一批的端口还有 `old`，或者出现 `port=?` 的 `old` | 带回这几行 |
| 第二条有输出 | 这一批失败 |

#### 第 33 步　用检测脚本核对

**机器：** 容器里，第 32 步通过以后。命令和第 13 步相同（P 用 P 的那一条，D 用 D 的那一条，最后加 `--baseline <这个容器第 3 步记下的 baseline_token>`）。

**应当看到：**

- `verdict vllm=ok ascend=ok(site_local=<n>) p2d=ok mooncake=ok`。
- `mooncake=` 一行里是 `changed_since_install=1 timing=2 store=replacement`（第 13 步时是 `changed_since_install=0 timing=0 store=site_original`；现场第 1、2 次检测的 `mooncake=` 一行都是 `changed_since_install=0 timing=0`）。`changed_since_install=1` 是安装记录里还是原库的哈希，属预期。
- 其余各行和第 13 步相同：`site_patches` 一行和第 3 步相同，`run` 行是 `logcfg=set`，单独一行 `baseline=same`。

**不对时：** 还是 `mooncake=not_replaced`（`store=site_original`）：库没有换上，停下，带回第 30 步的输出和这一段。其余见「判失败的条件」。

#### 第 34 步　`[BatchPutTiming]` 行、看板和磁盘

**机器：** 那台机器上，宿主机里；有请求以后。

**命令：**

```bash
for f in <vLLM 日志文件>; do echo "$f $(grep -c '\[BatchPutTiming\]' "$f") $(grep '\[BatchPutTiming\]' "$f" | grep -c 'complete=false')"; done
```

**应当看到：** 每个文件一行：文件名、`[BatchPutTiming]` 行数、其中 `complete=false` 的行数。P 机器（第 3 批）行数大于 0，`complete=false` 是 0；D 机器只在往缓存池保存时写这种行，不保存时两个数都是 0。第 3 批做完以后，看板上 P 侧后台 wait_for_save/put 下的 3 段 submit、wait_for_transfer、finalize 有值；做完 proxy 手册以后，47 段里 45 段有值。

再照第 17、18 步量 30 分钟磁盘，按「判失败的条件」的磁盘项判。

**不对时：**

| 看到 | 怎么办 |
|---|---|
| `complete=false` 不是 0 | 不算失败，服务和看板读的这 3 段不受影响；但必须带回：它说明第 28 步的调用方扫描漏了东西（包目录以外的文件在用、有代码按名字取，或者现场的包不是扫过的那一份） |
| 重启 10 分钟以后、`[BatchPutTiming]` 行数大于 0，这 3 段仍为空 | 不回滚，带回这条命令的输出和面板手册里填的 `MOONCAKE_LOG_FILES` |

#### 第 35 步　Mooncake 的回滚（只在失败时做）

**机器：** 监控机执行第一条，那台机器执行第二条，容器里执行后两条；最后按第 11 步重启。和第 22–26 步一起回滚时，两边的文件都恢复以后只重启一次。

**命令：**

```bash
ssh <SSH 账号>@<机器地址> mkdir -p /tmp/<机器>-mc-<MC 时间戳> && scp <用户目录>/vllm-pd-patch-glm53/backup/<机器>-mc-<MC 时间戳>/* <SSH 账号>@<机器地址>:/tmp/<机器>-mc-<MC 时间戳>/
```

```bash
docker cp /tmp/<机器>-mc-<MC 时间戳>/. <容器名>:/tmp/<机器>-mc-<MC 时间戳>
```

```bash
cd /tmp/<机器>-mc-<MC 时间戳> && LC_ALL=C sha256sum -c SHA256SUMS && cut -c1-64 SHA256SUMS
```

先用第 29 步第一条命令的第一行重新设好 `MC_PKG`、`MC_LIB`，再恢复原库：

```bash
BKMC=/tmp/<机器>-mc-<MC 时间戳>
cp "$BKMC/libmooncake_store.so" "$MC_PKG/.t51_orig_store.so" && chmod --reference="$MC_LIB" "$MC_PKG/.t51_orig_store.so" && chown --reference="$MC_LIB" "$MC_PKG/.t51_orig_store.so" && mv -f "$MC_PKG/.t51_orig_store.so" "$MC_LIB" && sha256sum "$MC_LIB" | cut -c1-16 && ls -l "$MC_LIB" | awk '{print $1, $5}'
```

**应当看到：** 第三条 `libmooncake_store.so: OK`，然后 64 位的 sha256，和 `<原库 sha256>` 相同；最后一条 `3bf82f8b0c5ab715`，然后权限和大小 `17610953`。重启以后，第 32 步第一条：回滚以后重启过的端口行末都没有 `(deleted)`（那条命令把它们记作 `new`，这时用的是恢复的原库）。第 33 步的检测脚本：`verdict` 最后一项是 `mooncake=not_replaced`；`mooncake=` 一行是 `timing=0 store=site_original`，`changed_since_install` 和第 13 步时相同（现场第 1、2 次检测都是 0）。vLLM 日志里不再有新的 `[BatchPutTiming]` 行，看板上那 3 段重新为空。

## 判失败的条件

每批对这一批的实例判。**先停下、带回（不算失败）**的：第 12 步命令行和重启前不同。下表任何一条出现，这一批算失败：停下，不做下一批，按第 21 步处理。

| 步骤 | 失败条件 |
|---|---|
| 11 | 第 1 批导入核对不是 `True`，或者报错 |
| 12 | 有实例的 `/health` 不是 `200`，或者起不来 |
| 12 | 有 vLLM 进程没有设 `VLLM_LOGGING_CONFIG_PATH` |
| 12 | vLLM 日志里有 `ImportError`，或者 `Traceback` 后面几行里有 `pd_diagnostics`、`pd_device_timing`、`worker_base`、`ascend_store`、`mooncake_connector`、`recompute_scheduler` |
| 13 | 出现 `BASELINE_DIFF 端口:项:旧值->新值 …`（重启前后各组实例的 kind、conn、spec、eager、async、load_async、tp、dp、调度器有变化） |
| 13 | `verdict` 的 vllm、ascend、p2d 三项有一项不是 `ok` 或 `ok(…)`，例如 `unpatched`、`absent`、`-`（这一项没有跑）、`BAD(…)`（mooncake 一项例外：`mooncake=BAD(other)` 不算） |
| 13 | `run` 行是 `logcfg=unset` |
| 13 | `site_patches` 和第 3 步不同 |
| 14 | `PD_DIAG_STARTUP` 的 `scheduler`、`model_runner` 不是第 14 步写的那几种，或者 `BalanceScheduler` 带 `balance=True` |
| 14 | D 上 `tp_rank=0` 的 `device_timing` 是 `off`（`?` 带回，交给留空比例判；P 上只带回） |
| 15 | 这一批的 D 实例 `cand_maps` 是 0，或者 `no_execution_id` 大于 0（`no_execution_id` 一出现就算失败） |
| 18 | 只判 `this_batch` 是 1 的行：D 的 `forward_empty_pct` 超过 1% 而且 `forward_empty` 比第 16 步大，或者 sampling 的这两项同样如此 |
| 18 | D 两次的 `pid_list` 不同 |
| 18 | 第二次比例为空、`ended_win` 大于 0、`forward_win` 或 `sampling_win` 大于 0（调度器侧断了，D、P 都算失败）。比例为空的其余情况不算通过，带回（P 只记录） |
| 19 | 重启 10 分钟以后、这一批的实例收到过请求，D 的 forward、sampling、start_load_kv/wait_for_transfer 仍然全为空（第 3 批看 P 的 forward、sampling、schedule） |
| 19 | 每批：这一批的实例在 `过滤器版本` 里不是 `…-19c` 那一版，或者出现在 `check` 的 `FAIL` 行里，或者有不点名实例的 `FAIL` 项（最后一批以前，只涉及还没重启批次（P）实例的 `FAIL` 项，包括没在写和旧版本两种，不算这一批失败，带回）。最后一批做完以后：`check` 不是 `RESULT: OK`，或者这一批的实例不是 `…-19c` 那一版 |
| 18（磁盘） | 节点目录每天增长 × `NODE_LOG_KEEP_HOURS` ÷ 24，再加这台机器的实例数 × 1 GiB（D 机器 16 GiB，P 机器 1 GiB），超过第 10 步 `df` 里节点目录所在文件系统 `Avail` 的一半 |
| 17、18（磁盘） | vLLM 日志里有 `[PD_DIAG]` 行（一出现就处理，不等满 30 分钟） |
| 18（磁盘） | 节点目录和 vLLM 日志在同一个文件系统上时（第 10 步的 `df` 只出一行），把 `[BatchPutTiming]` 每天的增长 × 现场 vLLM 日志保留的天数也加进上面那一条的预算，和同一个 `Avail` 的一半比（做了 Mooncake 部分以后才有意义） |
| 34（磁盘） | 做了 Mooncake 部分以后，`[BatchPutTiming]` 每台机器每天超过 43.1 GB（D 不往缓存池保存时应当是 0 行） |
| 31 | Mooncake 部分第 1 批的导入核对报错 |
| 32 | vLLM 日志里有 `undefined symbol` 或 `libmooncake_store` 的错误 |
| 33 | `mooncake=BAD(other)`；vllm、ascend、p2d 三项有一项不是 `ok` 或 `ok(…)`；出现 `BASELINE_DIFF`；`run` 行是 `logcfg=unset`；`site_patches` 和第 3 步不同 |

**不算失败、带回的：** 第 3 步、第 13 步的 `mooncake=BAD(other)`；第 13 步的 `vllm=ok(site_local=n)`；第 14 步 D 的 `?`、P 的 `off` 或 `?`；第 18 步 P 的留空比例；第 20 步重启后 TTFT、TPOT 大 10% 以上；Mooncake 部分以前第 18 步量到的 `[BatchPutTiming]` 明显大于 0（这些行回滚本套补丁去不掉，说明现场的 Mooncake 可能已经不是核对过的那份，Mooncake 部分先不做）；第 34 步 `complete=false` 不是 0。

## 带回清单

| 步骤 | 带回 | 每份 |
|---|---|---|
| 2 | `df -hT /tmp` 那一行 | 每个容器 |
| 3 | 检测脚本的全部输出；`baseline_token` | 指纹相同的容器只带一份输出；`baseline_token` 每个容器 |
| 5 | 不对时的输出；`grep` 显示 `**kwargs` 没传下去时，那条命令的输出 | 每个容器 |
| 6 | `<备份目录>` | 每个容器 |
| 8 | 三条命令的完整输出和 `<S>` | P、D 各一份 |
| 10 | `cmdline.before`、`df` | 每个容器；`df` 每台机器 |
| 11 | 导入核对那一行 | 第 1 批 |
| 12 | 命令行比较结果、日志配置那一行 | 每个容器 |
| 13 | 检测脚本的全部输出 | 每个容器 |
| 14 | 不对时的几行 | 每台机器 |
| 15 | 自检查询的输出 | 每批 |
| 16、18 | 留空比例两次的输出；第 1 批另要每个 D 实例的 `*_total`、`*_win`、`forward_empty`、`sampling_empty` | 每批，D、P 每个实例 |
| 18 | 磁盘那一行 | 每台机器 |
| 19 | `check` 的全部输出 | 每批 |
| 20 | 重启前、重启后的 TTFT（D 的批还有 TPOT） | 每批 |
| 21 | 失败时：这一批的检查输出、vLLM 启动日志、第 15 步的输出 | 失败的批 |
| 28 | 换库前核对的全部输出 | 每个容器 |
| 29 | `<MC 备份目录>`、`<原库 sha256>` | 每个容器 |
| 31 | 导入核对那一行 | 第 1 批 |
| 32 | 按端口数的那几行 | 每个容器 |
| 33 | 检测脚本的全部输出 | 每个容器 |
| 34 | `[BatchPutTiming]` 那条命令的输出；第 18 步的磁盘那一行 | 每台机器 |

## 附录 A　原因和背景

### A.1 为什么这样做

- **打三件，顺序固定；Mooncake 可后补。** 每个容器里按 vLLM、vllm-ascend 主体、p2d 重放的顺序打（第 7、8 步）。每一件都先核对现场文件的 sha256，再备份，再动文件，动完再核对一次。Mooncake 库的替换是Mooncake 部分（第 27–35 步），可以以后单独做，前 1–10 步不依赖它。
- **几件补丁必须一起装、一起回滚。** vllm-ascend 主体和 p2d 用到的下面这些名字只有 vLLM 补丁里有；只装 vllm-ascend 或 p2d、不装 vLLM 补丁时，Worker 启动就会 ImportError，vLLM 起不来：
  - `vllm.v1.metrics.pd_diagnostics` 模块；
  - `vllm.v1.worker.worker_base` 的 `pd_execution`、`pd_sample`、`pd_claim_finish`；
  - `SchedulerOutput.pd_execution_id`。
- **和接上面板二（第 9 步）合成一次重启。** 第 9 步在启动脚本里加两个环境变量，接上面板二的过滤器（节点目录沿用 test 的 `/data/pd-diag`，不加挂载）：
  - 要加挂载、得重建容器的，先重建容器、加好环境变量，再打本说明的补丁。重建容器会丢掉已经打的补丁。
  - 不用重建的，两件事做完以后一起重启一次（第 10–21 步）。
  - **两个环境变量必须在打完补丁后的第一次重启时就生效。** 其中 `VLLM_LOGGING_CONFIG_PATH` 指向面板二的 `pd_diag_logging.json`，补丁写的 `[PD_DIAG]` 记录才只进过滤器。现场现在没有设（第 2 次检测 `logcfg=unset`）。漏设时，原始记录全部写进 vLLM 日志：一台 D 机器每天约 1.4–13 TB，几小时到几天就能写满盘（A.3 结论四）。第 12 步在重启后核对。
- **不设 `MC_LOG_DIR`。** 打补丁和重启时都不要加这个环境变量（原因见 A.5）。
- **现场的本地补丁不用动，但打补丁前先核对它们没变（第 5、6 步）。** 这一条的结论，依据的是小核对 glm53f-3b 带回的这两个补丁文件的版本（下面的 sha256 前 16 位就是那次报的）和替换方式，以及 vLLM 一侧按这种替换方式做的模拟（vLLM 说明第 13 节）。vllm-ascend 的 `patch/platform/` 下有两个现场加的文件，两处替换都是包装，调用原函数，补丁的埋点都保留：
  - `patch_kv_load_failure_reason.py`（P、D 都有，sha256 前 16 位 `d7c764bb425993c0`）包装 `OpenAIServingCompletion.completion_stream_generator`：把输出原样逐个转交、记下 KV 加载失败的标记，再调用原函数，改写原函数产出的输出块。补丁在这个函数里的三处记录都在原函数里；模拟的 8 个场景，记录和不替换时逐条相同。改写输出块只改变发给客户端的内容。多出的这一层转发没有测耗时。
  - `patch_prefill_length_validation.py`（只在 P 上，`c36392a6338bc2f1`）包装 `AsyncLLM._add_request`：先做长度校验，再调用原函数。关键字参数原样传下去时，记录和不替换时逐条相同；校验的耗时算在 P 的 add_request 段里，没有测；校验不通过的请求，`engine_submission` 记录的状态是 `rejected`，没有 add_request 段。n>1 的请求，每个子请求都经过 `_add_request`，和原版 v0.30.0 相同。
  - 现场还改过 `patch/__init__.py`、`patch/platform/__init__.py`，第 5、6 步同样核对它们的 sha256 前 16 位，预期值是第 3 步检测脚本 `site_patches` 一行报出的值。
  - **停止条件：** 第 5、6 步核对这四个文件的值，任何一个对不上，说明现场的本地补丁变了，停下，把输出报回来。
  - 第 5、6 步另有一条只读的 `grep`，看这两个补丁文件是不是调用原函数、把参数原样传下去。它不是停止条件：`_add_request` 的包装没有把 `**kwargs` 原样传下去时，补丁照样打，服务不受影响，只是 P 的 add_request 段为空，把那条命令的输出带回来。
- **p2d 的停止条件（第 8 步）：** 重放的定位结果和小核对 f-3b 不同（除 C25 以外还有别的编号用了备选锚点，或 C25 回到 `primary`），或者 `p2d_extra` 那一行不是 `p2d_extra X1=1 X2=1/1 X3=2 X4=4/1 X5=1 X6=ok`，就停下。
- **备份必须拷回监控机（第 5、6 步）。** 现场的 `mooncake_connector.py` 是本地修改版，补丁包里没有这一份，丢了无法重建。第 5、6 步的备份拷到监控机、核对通过以后，才做第 7、8 步；回滚（第 22–26 步）用监控机上的这一份。
- **测试集群只有一台 D 机器。** 第 1 批就是全部 D，D 这一侧其实没有分批的保护，只剩 P 和 D 之间是隔开的。这是已知的，不是遗漏。
- **分批上线：** 先一台 D 机器的容器，再其余 D 机器，P 最后（第 10–21 步）。每批重启前记下各实例的命令行，重启后要逐项相同；每批做完、看到第 10–21 步写的结果，再做下一批。
- **核对命令的退出码。** 文中 `LC_ALL=C sha256sum -c … | grep -v ': OK$'` 这类命令，没有输出就是全部通过，这时整条管道的退出码是 1（`grep` 没有选出任何行）。所以不要把它们放进带 `set -e` 的脚本，按输出判断。
- **`docker restart` 以后补丁还在；`docker rm` 后重建容器、或者换镜像，补丁就没了，** 要重新做一遍。
- 所有容器里的命令都以 root 执行：先在那台机器上 `docker exec -it <容器名> bash` 进入容器。
- 任何一步的结果和「应当看到」不一样时，停下，把那一步的输出带回来，不要继续。

### A.2 和 glm5.3 现场的对应

现场的事实取自第 1、2 次环境检测和小核对 f-1、f-3b；验证依据取自 vLLM、vllm-ascend、p2d、Mooncake 各部分的交付说明（本包 `patches/*/README.zh-CN.md`、`t51-glm53-kit` 的 Mooncake 说明）。vLLM 一侧的调用路径复核在本包的 vLLM 补丁里（vLLM 说明第 13 节）；vllm-ascend 一侧的调用图审查还在做，结果到了以后补进这张表。

| 现场 | 包里怎么验证 | 没有验证的 |
|---|---|---|
| **vLLM v0.30.0 原版**（检测 `vllm=… state=v0.30.0 changed=0`，装在 `site-packages`） | 补丁以干净的 v0.30.0 为基线：23 个要改的文件对照 `SHA256SUMS.before` 0 处不符，`git apply` 通过，打完和替换件相同（vLLM 说明 9.1）；15 个合成场景的记录和 v0.32.0 加 01 逐条对照（9.3）；调用路径复核：原版经过、移植后不再经过的调用 0 处，n>1 的子请求改回经过 `_add_request`（第 13 节）；现场补丁只含 `vllm/` 下的 25 个运行时文件（t51-glm53-vllm `40152b2`），另加三行启动日志 `PD_DIAG_STARTUP`，报出运行时实际用的调度器和 runner（第 14 节，第 13、14 步核对）；第 5、6 步在现场再核对一次，不符就停；实验室在干净 v0.30.0 上照本文原文走过（本手册交付说明里的走查记录）。V2 runner 的两个文件（`v1/worker/gpu/kv_connector.py`、`gpu/model_runner.py`）在现场用不到（检测 `runner=V1(env)`），因为和 dsv4.1 的补丁共用而保留，照样核对（本包 README） | 没有在昇腾、真实模型、多进程 ZMQ、DP 协调器上跑（vLLM 说明 11.1） |
| **vllm-ascend `d4ad49796`，现场改了 3 个文件、加了 55 个**（检测 `ascend=… state=modified changed=3 added=55`；改的是 `kv_p2p/mooncake_connector.py`、`patch/__init__.py`、`patch/platform/__init__.py`，加的是 `_cann_ops_custom/` 52 个、`patch/` 2 个、根目录 1 个） | 本补丁改的 11 个文件现场都没动：第 5、6 步按 `SHA256SUMS.before`（9 个）、`NEW_FILES.txt` 核对，不符就停。现场改过的 `mooncake_connector.py` 不直接替换，第 8 步用 `replay_p2d.py` 在现场那一份上重放：小核对 f-1、f-3b 在现场文件上测得 28 处全部定位（只有 C25 用备选锚点）、X1–X6 和原版相同。现场改过的两个 `__init__.py` 第 5、6 步核对 sha256 前 16 位（预期值取第 3 步检测的 `site_patches`）。实验室在干净 `d4ad49796` 和模拟的现场修改上都走过 | `_cann_ops_custom/` 的 52 个新文件没有逐个看；在卡上跑（vllm-ascend 说明第 8 节） |
| **Mooncake `78566d4`，本地构建**（检测 `mooncake_build git=78566d4 wheel_tag=cp311-cp311-manylinux_2_38_aarch64`，glog 0.6.0） | Mooncake 部分（第 27–35 步，可选）。替换件按现场的选项编：同一镜像 `quay.io/ascend/cann:9.2.0-a3-openeuler24.03-py3.11`，msgpack-c cpp-9.0.0 头文件，去调试信息后重链接成现场的 NEEDED、RUNPATH、SONAME；对齐以后核对件的导出名（10974 个，`8d83f4262568bdd3`）和现场原库相同。换库前的只读核对 glm53mc-5 是最后一道关；实验室加载、put、原签名反例测试和第 27–35 步的走查都通过（增量包交付说明） | 没有在现场替换过；替换库没有在卡上、没有走 Ascend 传输跑过；glm53mc-5 还没在现场跑过（附录 C） |
| **Python 3.11.10** | 实验室走查用 `python:3.11.10-slim` 镜像，替换和重放以后的 25 + 11 + 1 个文件都能 `py_compile`（本手册交付说明里的走查记录）；vLLM 补丁另在 3.11.2 下对 25 个运行时文件做过语法解析，0 处错误（vLLM 说明 11.3） | 完整的单元测试是在 3.12 上跑的（vLLM 3.12.3，vllm-ascend `python:3.12-slim`），没有在 3.11 上跑 |
| **CANN 9.2.0** | Mooncake 替换件在 CANN 9.2.0 的镜像里编、测（同上）；C15 设备计时的前提在现场容器里确认过（下一行）。补丁的 Python 代码不直接调 CANN 接口 | 整条链路没有在卡上跑 |
| **torch_npu 2.10.0.post4** | 用户在现场 glm53 容器里跑过设备事件探针 `probe/npu_event_probe.py`（torch 2.10.0a0、torch_npu 2.10.0.post4，aclgraph replay）：设备事件 2.010 ms，host 计时加同步 2.024 ms（vllm-ascend 说明第 8 节） | C15 的整条链路（推迟写出、读取挂钩）没有在卡上跑；探针只跑过图模式，P 的 eager 下没有测过 |
| **P：TP16、DP8（每个容器 1 个实例）、eager、同步调度、MTP k=1** | vLLM 合成场景 E1（同步调度）、E7（P 同步加载，`stages.submit` 的范围）；vllm-ascend 3.1（同步加载记 `load`）、3.2（MTP 下 forward、sampling、draft 的口径，读码）；面板二端到端 glm53 一套（P 同步加载、只取 rank 0）和过滤器的 TP2 拆进程测试 | 多进程的 TP16、DP8 没有真实运行，TP>1 只在单进程里模拟（p2d、vLLM 说明）；eager 下的设备事件没有在卡上测过（《面板系统说明书》已知问题第 32 条） |
| **D：TP1、每个容器 16 个实例、图模式、async scheduling、MTP k=5、`AsyncRecomputeScheduler`** | vLLM 合成场景 E2（D 异步取远端 KV、k=5）、E5、E6（async scheduling，两步重叠，k=5）；vllm-ascend 第 11 节：`RecomputeScheduler` 改成经 01 的包装，调度器记录比对 363 项通过、2 项按约定预期不同，联合检查 D 走真实的 `AsyncRecomputeScheduler.schedule()`，130/130；forward、sampling 只用设备计时，取不到留空（C15）；面板二的实验室 D 容器起 8 个实例，过滤器按端口分目录 | 图模式下的整条链路没有在卡上跑；`DyntraLBRecomputeScheduler`（要 DP>1）没有跑；每个容器 16 个实例没有在实验室起过 |
| **现场两个本地补丁**（`patch/platform/patch_kv_load_failure_reason.py`，P、D；`patch_prefill_length_validation.py`，只在 P） | 小核对 f-3b 报出两处替换的目标和写法：`_add_request` 的包装 `wraps_orig=1`；`completion_stream_generator` 的包装以 `@functools.wraps(original)` 开头，内部把输出原样转交给原函数。vLLM 一侧据此判定两处都是包装（vLLM 说明第 13 节），按这种写法模拟（`review/field-patches`，HTTP 场景 F1–F8）：两个包装单独装、一起装，记录都和不替换时逐条相同；`_add_request` 的包装在 n=1、2、4 时分别被调用 1、2、4 次，和原版 v0.30.0 相同。第 5、6 步核对两个文件的 sha256 前 16 位（不符就停），另有一条只读的 `grep` | 两个文件的全文我们看不到；调用原函数的那几行在小核对截断的 diff 里，没有直接看到，由第 5、6 步的 `grep` 补上；包装多出的耗时没有测 |

### A.3 对节点的影响：日志量

**结论**

**一、落盘的地方有两处，规则不同。**

| 写到哪里 | 写什么 | 谁来删 |
|---|---|---|
| 过滤器目录（`NODE_LOG_DIR`） | 请求级记录，加上每请求每 5 秒一行累计值 | 过滤器自己删超过 `NODE_LOG_KEEP_HOURS`（缺省 12 小时）的文件 |
| vLLM 自己的日志文件 | Mooncake 的 `[BatchPutTiming]`，经标准错误写入（不设 `MC_LOG_DIR`） | 过滤器不删，现场要自己轮转 |

**二、过滤器目录：在现场的空闲空间下可控。**

**依据。** 第 2 次检测的 `free_gib`：
- P 容器 3445.5 GiB，D 容器 3217.2 GiB；
- 这是 vLLM 标准输出所在的那块盘。过滤器目录是否在同一块盘上，待现场确认。

**每台机器的占用。** D 机器按 16 个实例算，P 机器按 1 个实例算。下表每行取 A.3.1 表里那一档的最重组合（步间隔 20 ms）。

| 档位 | 12 小时占用 | 占可用空间 |
|---|---|---|
| D，每实例 1 请求/秒，B=256 | 99.5 GB，再加 16 GiB 上限 | 约 3% |
| D，每实例 10 请求/秒，B=256 | 351 GB，再加 16 GiB | 约 11% |
| D，每实例 50 请求/秒，B=256（表中最重的一档） | 1471 GB，再加 16 GiB | 约 43%；建议把 `NODE_LOG_KEEP_HOURS` 调到 6 小时，占用减半 |
| P，每实例 50 请求/秒，每步 16 个请求（最重的一档） | 41 GB，再加 1 GiB | 约 1% |

「上限」来自过滤器的删除规则：每个实例 4 个角色文件，各自写满 256 MiB 才换；正在写的不删，换下的要等最后一次写入超过保留时长才删（`pd_diag_logfilter.py` 的 `_clean_old`）。

统筹定：面板的默认值仍是 12 小时（`NODE_LOG_KEEP_HOURS` 留空）。每个 D 实例超过每秒 10 个请求、批大小 256 时，建议把 `NODE_LOG_KEEP_HOURS` 改成 6 小时，改法见《面板系统说明书》3.6.5。

**三、vLLM 日志要现场自己轮转。**

`[BatchPutTiming]` 让 vLLM 日志每天增长：

| 每实例每秒请求数 | P 机器，g=1 | P 机器，g=2 | D 机器（16 实例，D 保存时），g=1 | D 机器，g=2 |
|---|---|---|---|---|
| 1 | 0.43 GB | 0.86 GB | 0.43 GB | 0.86 GB |
| 10 | 4.3 GB | 8.6 GB | 4.3 GB | 8.6 GB |
| 50 | 21.6 GB | 43.1 GB | 21.6 GB | 43.1 GB |

- g 是每个请求保存的 KV cache 组数，没有核实，所以 1 组、2 组各列一列。
- D 不往缓存池保存时，D 上没有这一项。
- 例：P 每秒 50 个请求、g=2 时，一个月不轮转约 1.3 TB。

**四、必须和补丁同一次重启：设好面板二的日志配置。**
- **为什么。** 补丁 01、02、p2d 的 `[PD_DIAG]` 记录都用 vLLM 的 logger 在 INFO 级写出，没有开关。只有 `VLLM_LOGGING_CONFIG_PATH` 指向面板二的 `pd_diag_logging.json` 时，它们才只进过滤器：这份配置在 vLLM 的标准输出 handler 上挂了 `DropPdDiag`。
- **现场现状。** 现在是 `logcfg=unset`（第 2 次检测的 run 行）。
- **漏设的后果。** A.3.3 的原始量全部进 vLLM 日志：
  - 示例 A，D 机器每天约 1.4 TB，3.2 TiB 的空闲约 2.4 天写满；
  - 示例 B，D 机器每天约 13 TB，约 6 小时写满。
- **重启后的核对。** 检测脚本 run 行的 `logcfg=` 必须是已设置。

**五、原始量那一层（不落盘）是一项开销，作为已知问题。**
- 它是 EngineCore 和 Worker 里的 JSON 序列化：每条记录先 `json.dumps` 再交给 logger。
- 量见 A.3.3，例如 D 每秒 20 步、B=256 时约 3.6 MB/s。
- CPU 开销本身没有测。

**六、上线时的两个数和阈值（第 17、18 步和「判失败的条件」用）。**

两个数（每台机器每天，按 A.3.4 的两个示例点，参数都待现场确认）：

| | 节点目录每天增长 | vLLM 日志每天增长（`[BatchPutTiming]`） |
|---|---|---|
| 示例 A | D 机器约 92 GB，P 机器约 16 GB | P 机器约 4.3 GB；D 机器 0（D 不保存） |
| 示例 B | D 机器约 702 GB，P 机器约 64 GB | P 机器约 17.3 GB；D 机器 0（D 不保存） |

- 节点目录每天增长是 A.3.4 里 12 小时的量的两倍。盘上只留 `NODE_LOG_KEEP_HOURS`（缺省 12 小时）的量，再加每实例最多 1 GiB。
- vLLM 日志这一列要等Mooncake 部分（第 27–35 步）换了 Mooncake 以后才有，没换时是 0。vLLM 日志由现场自己轮转：这一路的增长按现场保留 vLLM 日志的天数，算进 vLLM 日志所在文件系统的空间。

阈值（每批量 30 分钟，换算成每天以后比较，见第 17、18 步；超过就回滚这一批，`[BatchPutTiming]` 在Mooncake 部分（第 27–35 步）以前的那一条除外）：

| 项 | 阈值 | 取法 |
|---|---|---|
| 节点目录 | 每天增长 × `NODE_LOG_KEEP_HOURS` ÷ 24，再加这台机器的实例数 × 1 GiB（D 机器 16 GiB，P 机器 1 GiB），不超过第 10 步 `df` 里节点目录所在文件系统可用空间（`Avail`）的一半 | 结论二：A.3.1 表里最重的一档（每实例每秒 50 个请求、B=256）在现场的空闲空间下约占 43%，一半以内覆盖表里的全部档位 |
| vLLM 日志里的 `[PD_DIAG]` 行 | 0 行；不是 0 行就立刻停这一批，不用等满 30 分钟 | 结论四：日志配置生效时 `[PD_DIAG]` 只进过滤器；vLLM 日志里出现一行，就说明这个实例的日志配置没有生效。没设日志配置时，一台 D 机器约 6 小时就会写满盘 |
| vLLM 日志里的 `[BatchPutTiming]` | Mooncake 部分（第 27–35 步）以前：应接近 0（Mooncake 没换时不写这种行）；明显大于 0 时带回来，不回滚，这说明现场的 Mooncake 可能已经不是我们核对过的那份，Mooncake 部分（第 27–35 步）先等我们看过这些行再决定做不做。Mooncake 部分（第 27–35 步）以后：一台机器每天不超过 43.1 GB（P 机器；D 机器只在往缓存池保存时有，不保存时应当是 0 行） | 结论三表里最大的一格（每实例每秒 50 个请求、g=2），P、D 机器都是 43.1 GB。D 只有往缓存池保存时才写这种行。第 17、18 步的命令量的是这些行的实际字节数；按行数估算，30 分钟换算成一天是「30 分钟内新增的 `[BatchPutTiming]` 行数 × 312 B × 48」 |

- 量 30 分钟：一个请求在 D 上停留 26–64 秒（A.3.4 的两个示例），累计值每 5 秒写一组，30 分钟里各类行都写过许多轮；时间再短，请求量的起伏影响大。
- 选平时有正常请求量的时段量。

#### A.3.1 过滤器目录，每实例 12 小时（GB）

**D**：不命中缓存池、不保存；命中时每请求多 3.2 KB，保存时多 3.6 KB。

| 每实例每秒请求数 | 步间隔 | B=16 | B=64 | B=256 |
|---|---|---|---|---|
| 1 | 20 / 50 / 100 ms | 2.03 | 2.87 | 6.22 |
| 10 | 20 ms | 17.77 | 18.61 | 21.96 |
| 10 | 50 ms | 11.74 | 18.61 | 21.96 |
| 10 | 100 ms | 8.69 | 18.61 | 21.96 |
| 50 | 20 ms | 42.34 | 88.58 | 91.92 |
| 50 | 50 ms | 33.20 | 52.31 | 91.92 |
| 50 | 100 ms | 30.16 | 40.13 | 80.02 |

**P（TP16）**：命中缓存池；不命中时每请求少约 3.3 KB。

| 每实例每秒请求数 | 每步 1 个请求 | 每步 4 个请求 | 每步 16 个请求 |
|---|---|---|---|
| 1 | 0.79 | 0.80 | 0.83 |
| 10 | 7.89 | 7.96 | 8.26 |
| 50 | 39.44 | 39.80 | 41.28 |

**算法。**
- **请求在 D 上停留多久。** 按 Little 定律，停留时间 = B ÷ r（B 是一步里在跑的请求数，r 是每实例每秒请求数）；在 D 上走的步数 = 停留时间 ÷ 步间隔。
- **每个请求写的固定几行。**
  - 3 条 `api_summary`、`core_summary[request]`；
  - p2d 的 `request_generation`；
  - `api_summary[request]` 的 detokenize 区间、`core_summary[request]` 的 hash 区间，每步各一项，最多各 64 项。
- **累计值行。** 停留期间每 5 秒写一组，每组 engine、worker 各一行。
- **自检行**：每个实例每分钟 4 行。

**和 MTP 的关系。** 每步记录的字节数和 MTP 无关，实测每个请求项都是 333 B：t51-glm53-vllm 的 E5（async）和 E6（async + spec5）两个场景一样。MTP 只改变每步出几个 token，表里的数不随 MTP 变。

#### A.3.2 vLLM 日志：`[BatchPutTiming]`

**行长。** 每行 312 B。实验室实测 290 B（keys=4、bytes=16384），按现场位数补齐：keys 3 位、bytes 10 位、四段各 5 位、total 6 位、线程号 7 位。

**行数。** 每个请求、每个 TP rank、每个 KV cache 组各一行：`kv_transfer.py` 的保存循环里，每组调一次 `put`。

**数据。** 结论三那张表。

#### A.3.3 原始 `[PD_DIAG]`（GB/天；只在没设日志配置时进 vLLM 日志）

**D 每实例**：每步部分加每请求部分。

| 每实例每秒请求数 | 步间隔 | B=16 | B=64 | B=256 |
|---|---|---|---|---|
| 1 | 20 ms | 77 | 217 | 777 |
| 1 | 50 ms | 33 | 89 | 313 |
| 1 | 100 ms | 19 | 46 | 158 |
| 10 | 20 ms | 112 | 252 | 812 |
| 10 | 50 ms | 57 | 125 | 349 |
| 10 | 100 ms | 36 | 82 | 194 |
| 50 | 20 ms | 180 | 410 | 970 |
| 50 | 50 ms | 118 | 210 | 507 |
| 50 | 100 ms | 97 | 143 | 328 |

**P 每实例（TP16）**：16 个 rank 各写自己的 worker 和缓存池记录。

| 每秒步数 | 每步 1 个请求 | 每步 4 个请求 | 每步 16 个请求 |
|---|---|---|---|
| 2 | 34 | 101 | 395 |
| 5 | 85 | 252 | 987 |
| 10 | 171 | 503 | 1975 |

#### A.3.4 两个示例点

全部参数待现场确认；两个示例都按 g=1、D 不保存。

**示例 A。**
- 假设：
  - D：每实例每秒 1 个请求，B=64，步间隔 50 ms，MTP k=5；每个请求在 D 上 1280 步、停留 64 秒。
  - P：每秒 10 步，每步 1 个请求（每秒 10 个请求），命中缓存池。
- 过滤器目录 12 小时：D 机器 45.9 GB，再加 16 GiB；P 机器 7.9 GB，再加 1 GiB。
- vLLM 日志每天：P 机器 4.3 GB。

**示例 B。**
- 假设：
  - D：每实例每秒 10 个请求，B=256，步间隔 20 ms，MTP k=5；每个请求 1280 步、停留 26 秒。
  - P：每秒 10 步，每步 4 个请求（每秒 40 个请求），命中缓存池。
- 过滤器目录 12 小时：D 机器 351 GB，再加 16 GiB；P 机器 31.8 GB，再加 1 GiB。
- vLLM 日志每天：P 机器 17.3 GB。

#### A.3.5 和 glm5.3 现场的对应

| 项 | 本节怎么取 | 依据 |
|---|---|---|
| P 的形态 | TP16，每容器 1 个实例；16 个 rank 都写 worker 和缓存池记录，过滤器只留 rank 0 | 统筹给的 glm5.3 形态；第 2 次检测 |
| D 的形态 | TP1，每容器 16 个实例，MTP k=5，async 调度（`AsyncRecomputeScheduler`） | 同上；记录由这个调度器的真实代码产生 |
| 补丁版本 | vLLM v0.30.0 + t51-glm53-vllm `ea04aa1`（补丁 `88685fda`）；vllm-ascend `d4ad49796` + `ef5bd17` 的 11 个文件（SHA256SUMS.after `4c2d978a`）；p2d `a9e9695` | 本包的 vLLM 最终版 `40152b2` 比 `ea04aa1` 只多了 n>1 子请求走 `_add_request` 和三行启动日志 `PD_DIAG_STARTUP`；后者不是 `[PD_DIAG]` 记录，不改记录格式，也不改这一节的量 |
| 过滤器规则 | 包二 v3.1-extra 的 glm53 `segments.json`（config_id `pd-diag-v3.1-extra-glm53-2026-11-19b`）；哪些记录落盘，以过滤器自己解析出的规则为准 | 实测 |
| 日志配置 | 现场现在 `logcfg=unset`；本节前三层的数，都以补丁那次重启同时设好面板二日志配置为前提 | 第 2 次检测 |
| `MC_LOG_DIR` | 不设，`[BatchPutTiming]` 进 vLLM 日志 | f-1：16 个 Worker 都没设 |
| 空闲空间 | P 3445.5 GiB，D 3217.2 GiB；是 vLLM 标准输出所在的盘 | 第 2 次检测 |
| 请求速率、批大小、步间隔、g | 未知，按档位列表 | 待现场确认 |

#### A.3.6 出处

- **记录字节数：** `t51-glm53-kit` `volume/`。
  - 测量方法：在上面那几份真实代码上，一步排 1、64、256 个请求测出，D 另跑 70 步的长请求；`docker run --rm --network none`。
  - `api_summary`：取自 t51-glm53-vllm 的 `verify/pd_equiv.py`（未改）。
  - 落盘行：把记录喂给真实的过滤器量出。
  - 字段宽度：同 t51-glm53-p2d 的测量。
- **p2d：** t51-glm53-p2d `results/12_record_bytes.json`。
- **`[BatchPutTiming]`：** `t51-glm53-kit` `mooncake/replace/lab/lab_test.txt`。
- **计算：** `volume/harness/compute_tables2.py`，结果在 `volume/results/tables2.md`。
- **本节：** `t51-glm53-kit` 提交 `b2f2617` 的 `notes/guideA_glm53_logvolume.zh-CN.md`，搬入时只改了标题层级和节号引用；结论六是本说明加的。

### A.4 Mooncake 部分

- **前提：第 1–21 步三批都已通过。** 第 13 步检测脚本报的是 `verdict vllm=ok ascend=ok(site_local=<n>) p2d=ok mooncake=not_replaced`，即现场原装的 Mooncake 库（小核对 f-1 带回的那一份）。第 13 步报 `mooncake=BAD(other)` 的，不做这一步，把那一行带回来。第 18 步在这一步以前就量到 `[BatchPutTiming]` 行的，也先不做，把这些行带回来。
- **换了以后多了什么：** P 侧「后台 wait_for_save → put」下面的 3 个子段 submit、wait_for_transfer、finalize 有值；做完 proxy 手册以后，47 段里 45 段有值（第 19 步）。各实例的 vLLM 日志里多出 `[BatchPutTiming]` 行，由现场自己轮转（A.3 结论三）。
- **只换一个文件。** `<Mooncake 目录>/libmooncake_store.so` 换成增量包的 `lib/libmooncake_store.so`。store 模块、`libtransfer_engine.so`、`libmooncake_common.so`、`libasio.so`、`ascend_transport.so` 和自带的系统库都不动。
- **每个容器先跑换库前的核对（第 28 步），是最后一道关。** 最后一行是 `mc_pre result=ok_replace` 才往下做；其余结果都停下，不换任何一个容器，把输出带回来。
- **原库必须备份、拷回监控机（第 29 步）。** 现场的原库是现场自己构建的，增量包里没有这一份，丢了无法重建。备份拷到监控机、核对通过以后，才做第 30 步；回滚（第 35 步）用监控机上的这一份。
- **备份不能放在 `<Mooncake 目录>` 或任何 `mooncake*.libs` 目录里**，否则换库前的核对和检测脚本会把它当成第二份 store 库。
- **换库用改名，不覆盖。** 新文件先复制到同一目录下的临时名，再 `mv` 改名。正在运行的 vLLM 继续用它已经映射的原库，不受影响；直接覆盖原文件可能让正在运行的进程崩溃。
- **换完要重启这个容器里的 vLLM 才生效，尽快重启。** 重启以前，运行中的进程如果新起一个子进程，子进程会加载新库，新旧两份库混在一起。
- **每个容器只做一次第 27–30 步，第 31 步起才分批。** 分批和第四部分相同：第 1 批是一台 D 机器的容器（这台机器上的全部 D 实例），第 2 批是其余 D 机器的容器，第 3 批是 P 机器的容器。一个容器在它的实例第一次重启以前做完第 27–30 步；同一个容器的实例要是分在不同批次重启，后面的批次不再重做第 27–30 步，只做第 31 步起的各节。这一批的检查都通过了，再做下一批。
- **换库到重启之间可能隔几个小时。** 在这段时间里，这个容器里还没重启的实例继续用原库，已经重启的实例用新库。这样做的前提：补丁 04 只给 `Client` 加了三个非虚的私有成员函数声明，没有加数据成员、static 成员和 thread_local，没有虚函数，没有动虚表，所以两种构建同时在不同进程里运行时，布局和进程间传递的数据格式都相同（核实结果见交付说明）。
- **不设 `MC_LOG_DIR`。** 重启时也不要加。Mooncake 的传输引擎只有在设了它时才初始化 glog，把日志写进那个目录；不设时写标准错误，`[BatchPutTiming]` 行因而写进各实例的 vLLM 日志文件，面板二的 `MOONCAKE_LOG_FILES` 填的就是这些文件。
- 所有容器里的命令都以 root 执行：先在那台机器上 `docker exec -it <容器名> bash` 进入容器。
- 任何一步的结果和「应当看到」不一样时，停下，把那一步的输出带回来，不要继续。

### A.5 面板二上看什么，留空比例和 `no_execution_id` 判据的依据

**哪几类记录到了面板二。** 接上过滤器以后，`[PD_DIAG]` 记录的原文由过滤器接管，不再写进 vLLM 日志；过滤器只把看板要用的部分写进 `<节点目录>/logs/<端口>/`：有的记录照原样写出，有的只在过滤器里累加、配对，看不到原文。所以 consumer link、task、`worker_summary` 这几类记录按下表的间接证据看：

| 记录 | 哪一侧 | 在哪里看 | 应当看到 |
|---|---|---|---|
| `pd_transfer_summary[scope=request_generation]` | D | 节点上 `<节点目录>/logs/<端口>/` 下的文件（原样写出），`grep -c '"scope":"request_generation"'` | 大于 0 |
| `pd_transfer_summary` 的 consumer link 和 task | D | 过滤器只拿它们把首次候选那一步归到请求上，不写出原文；看第 15 步那条查询的 `cand_maps`，和看板上 D 的 start_load_kv/submit | `cand_maps` 大于 0；D 的 start_load_kv/submit 有值 |
| `worker_summary`（带 `device_duration_ns`） | P、D | 过滤器按请求累加，不写出原文；看第 15 步那条查询的留空次数 `forward_empty`、`sampling_empty`，和看板上的 forward、sampling | forward、sampling 有值；留空比例见下面（D 的批是判据） |
| `core_summary`、`api_summary` | P、D | 看板上的 schedule、req_resolve、tokenizer、add_request | 有值 |

- **为什么要两个条件。** 比例超过 1%、留空次数却没有增加时，多出来的不是留空，而是调度器和 Worker 两侧的步数本身对不上（《面板系统说明书》已知问题第 35 条）。代价是：真实留空很少、偏差又是正的时候，两者叠加可能误判失败；带回来的两次输出能分辨出来。
- **D 的批失败时**，说明设备计时没有每步都取到，看板上 D 第二步起的每步平均已经偏大，倍数是 1/(1−f)（《面板系统说明书》已知问题第 34 条）。把两次输出带回来。
- **P（第 3 批）只记录、不判：** P 是 eager，eager 下的设备事件没有在卡上测过（《面板系统说明书》已知问题第 32 条），不能拿同一个阈值判 P 失败；否则 P 一回滚，其余能正常出数的 P 段也跟着没了。P 的两次输出必须带回来。第一种情况（比例超过 1%、留空次数增加）时，看板上 P 的 forward、sampling 两段会相应变空：留空的步不计入，一个请求的步都留空时这两段整段为空。
- `no_execution_id` 一出现就算失败：execution_id 在 vLLM 补丁的 core.py `_pd_schedule` 里兜底（`patches/vllm/files/vllm/v1/engine/core.py:662-682`）：调度器没设就新建一个，所以和调度器是哪个类无关；过滤器只对 core_summary、worker_summary 两类累加记录计数，这两类都必然带 id。所以出现一次就说明链路断了。

## 附录 B　已知问题

1. **第 18 步的 `window_min` 只写了上限。** 第二次查询跑得太早时，窗口很短、步数很少，比例会很噪，输出里看不出是数据太少：第二次查询的 `window_min` 明显小于 20 时数据太少，等满 25 分钟左右再跑。这条规则没有被合成用例覆盖。（补丁包交付说明已知问题第 1 条）
2. **面板包新增 test-glm53 说明 10.5 的自检计数查询，用的还是 `GROUP BY node, instance, pid`。** 本手册第 15 步的同一条查询已经改成 `GROUP BY instance, pid`，以本手册为准。只在一个 pid 带两个 node 标签时，两份的结果才会不同。（补丁包交付说明已知问题第 2 条）
3. **Mooncake 部分：换库前核对的实验室记录里，T6–T10 的 `exit=` 是 `grep` 的退出码，不是核对脚本的。** 交付的脚本只要有 `STOP` 就返回 2，第 28 步写的「退出码 2」没有错。（增量包交付说明已知问题第 1 条）
4. **第 19 步的 `check` 每批只判这一批的实例，和补丁包操作说明 A 第 7.6 节不同。** 7.6 写的是「`check` 不是 `RESULT: OK`，或者这一批的实例不是 `…-19c` 那一版」就算这一批失败。分批时，最后一批以前还有实例没重启：
   - 未打补丁的 vLLM 上，旧过滤器是静默的，不写记录，也不写自检行：`_selfcheck` 只往已经打开的文件里写，文件要等第一条 `[PD_DIAG]` 来了才打开。
   - `check`（面板包 `scripts/safety.py`）在 vllm 模式下读清单里每台机器的 vLLM 进程，按实例判这次启动以后有没有记录（`scripts/criteria.py` 第 220–241 行 `instance_record_state`）。还没重启的 P 没有记录，环境里也没有 `PD_DIAG_INSTANCE_SOURCE`，是 `no_records_no_filter`：「过滤器」「版本」两项各报一条 `FAIL`（`criteria.py` 第 244–266 行 `judge_process_states`，`safety.py` 第 411、423 行）。「链路通」一项只在最近没有这一版的任何行时才按实例判（`safety.py` 第 456–467 行），这一批的 D 在写时不会因 P 报 `FAIL`。
   - 这样的实例不进「过滤器版本」的比较（`criteria.py` 第 356–357 行），所以静默的 P 不会报「两个以上版本同时在写」；只有 P 带着更早一版补丁、正在写记录时，才会报（`criteria.py` 第 370 行）。
   - 两种情况下整条 `check` 都是 `RESULT: FAIL`，照 7.6 判会误判这一批失败。所以本手册每批只判这一批的实例是 `…-19c`、不出现在 `FAIL` 行里，只涉及还没重启批次（P）实例的 `FAIL` 项不算失败、带回；全部一致和 `RESULT: OK` 只在最后一批要求。
   - 测试集群只有 1 台 P、1 台 D，第 1 批（D）做完、第 3 批（P）还没做时就是这种状态。实验室是一次全部重启的，前两批的 `check` 输出没有实测。（面板手册的 t41 指出，顾问核过）
5. **回滚（第 22–26 步）不撤第 9 步加的两行 `export`。** 回滚以后，vLLM 仍然加载面板二的日志配置，只是不写记录。要彻底退回打补丁前，得手动删掉 `<启动脚本>` 里这两行，再重启。
6. **Mooncake 部分：第 32 步按端口把进程对到实例的规则有一处缝。** 映射了 store 库、命令行或上级进程里有自己的 `--port`、但不是这一批实例的进程，停在原库上时两条规则都不会触发。ABI 没变，这样的进程只是不写 `[BatchPutTiming]`，不影响服务。（增量包交付说明已知问题第 2 条）
7. **11-20 测试集群例外：Mooncake 第 28 步的 `STOP_build_differs`，用户决定照换。** 只限 glm5.3 测试集群（`test-glm53`）、只做这一次。手册正文不改：第 28 步的规则仍是 STOP 不换，生产集群（glm53-1）换不换，到时由用户单独定。
   - **现场看到的：** P、D 两个容器第 28 步都是 `STOP`，原因只有 `build_differs`；`sections_other` 只有 `.comment:51/102`，即两份库只差编译器标识串的长度，其余各节大小相同。
   - **依据（都是只读命令，在运行中的容器里跑，零影响）：**
     - 读串：现场原库的 `.comment` 只有一条编译器标识，是 openEuler 的 gcc 12.3.1 -110，和实验室编 Mooncake 用的发行相同。差的只是 glibc 静态小函数随带的标识：实验室多一条 glibc-devel 构建时的 gcc 标识。
     - 逐函数长度：`.dynsym` 里 9906 个函数的 `st_size` 全部相同。
     - 字节比对：P、D 都是 `same_after_normalization`（`masked_words=352`，`path_prefixes=65`，`.text` 没有一块不同，所有节规范化以后都相同）。
   - **顾问对这次例外定的五个条件**，都满足了才换：
     1. 证据齐全：上面三项，加上第 28 步的输出只差 `.comment` 一项（`actual=` 等于 `predicted=`、`prefixes=1`、`differing_families=none`、`replacement_vs_site=-0/+3`、`removed_names=0`）；
     2. 只限测试集群这一次，在交付说明和本附录记一条（就是这一条）；
     3. 先把回滚条件钉死：第 29 步监控机上 `OK`、前 16 位 `3bf82f8b0c5ab715`，备份目录名和 64 位 sha 记下以后才做第 30 步；
     4. 分批照原样，不并批：第 1 批只换 D，第 31–34 步都过了再做 P；
     5. 由用户决定，并告诉用户残余风险。
   - **残余风险：** 比对证明的是现场原库和实验室核对件相同，不证明替换库在现场一定没问题。兜底靠分批、第 31 步的导入核对、第 29 步的备份和第 35 步的回滚。
   - **做法：** 第 27–35 步原样做，只跳过第 28 步；D 整批通过以后再做 P 的第 27–30 步。带回件里附读串、逐函数长度、字节比对三段输出和第 28 步的输出，作为这次例外的依据。

## 附录 C　没在 NPU 上、在现场测过的部分

- **整条链路没有在卡上跑。** vLLM 补丁没有在昇腾、真实模型、多进程 ZMQ、DP 协调器上跑；vllm-ascend 一侧没有在卡上跑，`_cann_ops_custom/` 的 52 个新文件没有逐个看。
- **P、D 的形态。** 多进程的 TP16、DP8 没有真实运行，TP>1 只在单进程里模拟；每个容器 16 个实例没有在实验室起过；`DyntraLBRecomputeScheduler`（要 DP>1）没有跑。
- **设备计时。** C15 的整条链路（推迟写出、读取挂钩）没有在卡上跑；设备事件探针只在现场跑过图模式，P 的 eager 下没有测过。
- **Python 3.11。** 完整的单元测试是在 3.12 上跑的；在 3.11.10 上只做过 `py_compile` 和语法解析。
- **第 11 步的导入核对。** 实验室用 CPU 版 torch，torch_npu、torchair、Mooncake、acl 换成替身；现场要加载 torch_npu 和 CANN 的部分没有测过。
- **现场的两个本地补丁。** 两个文件的全文我们看不到；调用原函数的那几行由第 5 步的 `grep` 补上；包装多出的耗时没有测。
- **开销。** 本套补丁的开销没有实测，第 20 步的 TTFT、TPOT 用来兜底。
- **Mooncake 部分。**
  - 替换库没有在卡上、没有走 Ascend 传输跑过：put 只走过 TCP（`MC_FORCE_TCP=1`）；P 在真实保存路径上写的 `[BatchPutTiming]` 没有实测；计时代码的开销没有测。
  - 换库前的核对 glm53mc-5 还没在现场跑过：`.rodata` 的精确预测第一次用在现场原库上。
  - 原签名包装那条路径从没在现场执行过；现场如果真有调用方（第 34 步 `complete=false` 不是 0），那就是这段代码第一次运行。
  - 第 31 步导入时 Mooncake 的 Ascend 传输在卡上做什么，没有验证。
  - 现场 D 是否往缓存池保存没有核实。

## 附录 D　文件和 sha256

| 文件 | sha256 |
|---|---|
| 补丁包 `t51-glm53-patch-kit.tar`（t51-glm53-patchkit `207d7a4`；根目录 `SHA256SUMS` 列出 86 个文件） | `d21cc3196e8c3430ff851c9810cf8ed6ddc6b619a41c3a510c0af9f211c24815` |
| 补丁包里的检测脚本 `detect/t51_glm53_check.py`（glm53-6） | `f9e6073f692dd4ea8b08b0c7aa26400ac0603495e89b5641e8c4bd20f67ef4ed` |
| `patches/vllm/t51-vllm-v0.30.0-pd-diag.patch`（t51-glm53-vllm `40152b2`） | `7e2ec359f5781ccd987fb9904f2581e15671b8b43c49f8a4dd615cf9e9aafe4a` |
| `patches/vllm/SHA256SUMS.after` | `5a7084855419155fecae1198225e93571c807fb15968f377b76b65e55aabb594` |
| `patches/vllm-ascend/t51-vllm-ascend-d4ad49796-pd-diag.patch` | `024a8b3e1cbbf36823fb6f60e9a204ca350f45524cec40d0856faa0a0f334e99` |
| `patches/vllm-ascend/SHA256SUMS.after` | `4c2d978a0017a2288fe99fd1401ba0215cd36490491e28db659d4436e5138e33` |
| `patches/p2d/replay_p2d.py` | `02304c8f76429780635870d17bac73558a2ecfafaadd744428c0ec93f457d4b3` |
| `patches/p2d/extra_checks.py` | `2759c06f4bead2b072fecfe36b221bfa6a74b86f981982c7e0b1c1f7c864b95a` |
| Mooncake 增量包 `t51-glm53-mooncake-kit.tar`（t51-glm53-kit `99a1f23`；根目录 `SHA256SUMS` 列出 8 个文件） | `6d70978ce93b99281d4e8e5ad781146b522782ebccf15930cdb68f2fc1b9c306` |
| 增量包里的替换库 `lib/libmooncake_store.so`（15402248 字节） | `3131784385846064327e858d6dcdb11a6c7674c2162964d52f9ed0e866b52bb8` |
| 增量包里的换库前核对 `precheck/dist/t51_mc_precheck.py`（glm53mc-5） | `4475c662c9ae669b50fa60bd097b386f2b4a3f6b5987eb78b0a0686cafd24cd4` |
| 补丁 04 `reference/04-mooncake-0.3.13-native-timing.patch` | `0625b51e7f71a62f4cb6e279182fa709d1537800c43bcf7d977459e43c01921f` |
| 现场原有的 `libmooncake_store.so`（小核对 f-1） | 17610953 字节，前 16 位 `3bf82f8b0c5ab715` |
| 现场的 `mooncake_connector.py`（打补丁前） | 前 16 位 `2c4acb3eeb1ced7d` |
