# 操作说明 A：在运行中的 dsv4.1 容器里打补丁（草稿）

**状态：草稿。** 标「待检测」的步骤要等环境检测结果回来才能定稿；其余步骤的命令已在实验室容器里按原文走过一遍（第 9 节）。

适用范围：test 集群的 dsv4.1 P、D 容器。不新建镜像，不新建容器；改动写在容器的可写层里。

## 0. 先读这几条

- **vLLM 补丁和 vllm-ascend 补丁必须一起装、一起回滚。** 只装一个时，Worker 启动时就会 ImportError，vLLM 起不来：vllm-ascend 的 `worker/v2/model_runner.py`（V1 是 `worker/v1/model_runner.py`）在模块顶层 `from vllm.v1.worker.worker_base import pd_execution, pd_sample`，这两个名字只有 vLLM 补丁里有。
- **每个 P 容器、每个 D 容器都要做第 1–7 步。** Mooncake（5.1）只在 P 容器上换；proxy（5.2）只在 proxy 所在的机器或容器上做。
- **和操作说明 B 一起做，重启只做一次。** 顺序：B 第 1、2 步（监控机升级、下发新节点文件）→ 本说明第 1–6 步 → 本说明第 7 步重启（vLLM 和 proxy 一起）→ B 第 3、4 步。新补丁和 B 的新节点文件在这一次重启里同时生效。
- **不含 proxy 时**（只打 vLLM、vllm-ascend、Mooncake 三个补丁，proxy 不动）：步骤编号不变，跳过下面这些内容，其余照做：
  - 第 5.2 节整节（不换 proxy 文件，不设 `PD_PROXY_DIAG`、`PD_PROXY_DIAG_DIR`，也不用在 proxy 上跑 `--proxy-file` 检测）；
  - 第 7 步里重启 proxy 的部分（表格下面「proxy 按它平时的启动方式重启……」那一段），只重启 vLLM；
  - 第 9 步里 proxy 的回滚：回滚顺序第 2 条里的「去掉 proxy 的环境变量」，以及「proxy：换回第 5.2 步备份的原文件……」那一段；
  - 检测核对里 proxy 那一行：第 9 步重启后的检测不看 `proxy file=main`。

  第 8 步第 2 条「经 proxy 发几个请求」照常经现有的 proxy 发；proxy 没换，只是不写 proxy 行，面板二的两个 P/D LB proxy 段为空。
- **`docker restart` 以后补丁还在；`docker rm` 后重建容器、或者换镜像，补丁就没了，要重新做一遍。**
- 所有命令都在容器里以 root 执行：先在宿主机上 `docker exec -it <容器名> bash` 进入容器。
- 任何一步的结果和「执行后看到」不一样时，停下，把那一步的输出带回来，不要继续。

## 1. 把交付包放进容器并核对

在宿主机上：

```bash
docker cp t51-dsv41-kit.tar <容器名>:/tmp/
```

在容器里：

```bash
mkdir -p /tmp/t51-kit && tar -xf /tmp/t51-dsv41-kit.tar -C /tmp/t51-kit && cd /tmp/t51-kit && sha256sum -c SHA256SUMS | grep -v ': OK$'
```

**执行后看到：** 没有任何输出。有输出说明交付包在拷贝中损坏，重新拷贝。

宿主机上的 `t51-dsv41-kit.tar` 留着，不要删：容器的 `/tmp` 可能被清空（例如镜像把 `/tmp` 挂成 tmpfs），回滚（第 9 步）时要用。

## 2. 跑一次检测，确认可以打补丁

按 `detect/使用说明.zh-CN.md` 跑检测脚本。能往下做的条件：

| 行 | 要求 |
|---|---|
| `vllm=` | `state=v0.32.0`（原版）。`state=v0.32.0+01` 表示补丁 01 已在，第 4.1 步跳过 |
| `ascend=` | `state=f9c8e0bdb`（原版）。`state=f9c+t51kit` 表示已装本套补丁，第 4.2 步跳过 |
| 其他状态 | `modified` 等：停下，把检测输出带回来，按改动另定 |

**执行后看到：** 不超过 20 行 `键=值`，最后一行是 `fingerprint=…`，退出码为 0。装补丁之前，通常是 `vllm=` 行 `state=v0.32.0`、`ascend=` 行 `state=f9c8e0bdb`。只打印一行 `BLOCKED` 时，按检测脚本使用说明处理。

## 3. 找到安装目录，备份要改的文件

```bash
VLLM_PKG=$(python3 -c "import importlib.util as u; print(u.find_spec('vllm').submodule_search_locations[0])")
ASCEND_PKG=$(python3 -c "import importlib.util as u; print(u.find_spec('vllm_ascend').submodule_search_locations[0])")
VLLM_ROOT=$(dirname "$VLLM_PKG"); ASCEND_ROOT=$(dirname "$ASCEND_PKG")
echo "$VLLM_ROOT"; echo "$ASCEND_ROOT"
```

**执行后看到：** 两行目录，例如 `/vllm-workspace/vllm` 和 `/vllm-workspace/vllm-ascend`。`find_spec` 只查找位置，不导入 vLLM。

先核对原文件就是公开版本：

```bash
K=/tmp/t51-kit/patches
(cd "$VLLM_ROOT" && sha256sum -c $K/vllm/SHA256SUMS.before | grep -v ': OK$'; for f in $(cat $K/vllm/NEW_FILES.txt); do [ -e "$f" ] && echo "已存在：$f"; done)
(cd "$ASCEND_ROOT" && sha256sum -c $K/vllm-ascend/SHA256SUMS.before | grep -v ': OK$'; for f in $(cat $K/vllm-ascend/NEW_FILES.txt); do [ -e "$f" ] && echo "已存在：$f"; done)
```

**执行后看到：** 没有任何输出。出现 `FAILED` 或「已存在」，说明文件已经被改过或已装过补丁，停下（第 2 步的检测本应已经发现）。

再备份：

```bash
BK=/tmp/t51-backup-$(date +%Y%m%d-%H%M%S); mkdir -p "$BK"; echo "$BK"
(cd "$VLLM_ROOT" && tar -cf "$BK/vllm.tar" $(awk '{print $2}' $K/vllm/SHA256SUMS.before))
(cd "$ASCEND_ROOT" && tar -cf "$BK/vllm-ascend.tar" $(awk '{print $2}' $K/vllm-ascend/SHA256SUMS.before))
tar -tf "$BK/vllm.tar" | wc -l; tar -tf "$BK/vllm-ascend.tar" | wc -l
```

**执行后看到：** 备份目录名，然后两个数字 `23` 和 `11`。两个备份文件各约 1 MB（`vllm.tar`、`vllm-ascend.tar`）。把备份目录名记下来，回滚要用。备份也在容器的可写层里，建议在宿主机上另存一份：`docker cp <容器名>:<备份目录> .`。

## 4. 替换源文件

### 4.1 vLLM（补丁 01）

```bash
cp -R $K/vllm/files/vllm/. "$VLLM_PKG"/ && (cd "$VLLM_ROOT" && sha256sum -c $K/vllm/SHA256SUMS.after | grep -v ': OK$')
```

**执行后看到：** 没有任何输出，表示 25 个文件都和交付的一致。有 `FAILED` 时停下，按第 9 步回滚。

### 4.2 vllm-ascend（本套补丁）

```bash
cp -R $K/vllm-ascend/files/vllm_ascend/. "$ASCEND_PKG"/ && (cd "$ASCEND_ROOT" && sha256sum -c $K/vllm-ascend/SHA256SUMS.after | grep -v ': OK$')
```

**执行后看到：** 没有任何输出，表示 12 个文件都和交付的一致。有 `FAILED` 时停下，按第 9 步回滚。

4.1、4.2 做完以后，正在运行的 vLLM 还在用内存里的旧代码，重启（第 7 步）后才生效。

## 5. Mooncake 和 proxy

### 5.1 Mooncake（只在 P 容器上）

**前提：** 检测结果的 `mooncake=` 行是 `state=obs-v0.3.14rc1-cp312-aarch64`（`so_diff=0`），即现场装的就是 vllm-ascend 镜像用的那个 OBS wheel，而且没被改过。不是这样时，停下，带回检测输出另定。

交付包里的两个文件（`patches/mooncake/lib/`，xz 压缩，各约 3 MB）是打了补丁 04、去掉调试信息的 `libmooncake_store`，分别替换 wheel 里的两份同名文件。现场实际加载哪一份以检测的 `store_lib=` 为准；两份都替换，各自备份。

先核对现场的两个文件就是 OBS 原文件：

```bash
MC_SITE=$(python3 -c "import importlib.util as u, os; print(os.path.dirname(u.find_spec('mooncake').submodule_search_locations[0]))")
M=/tmp/t51-kit/patches/mooncake/lib
echo "$MC_SITE"; (cd "$MC_SITE" && sha256sum -c $M/SHA256SUMS.before | grep -v ': OK$')
```

**执行后看到：** 一行目录（`site-packages` 的路径），然后没有别的输出。出现 `FAILED`，说明现场的库和 OBS 原文件不一致，停下，不要替换。

备份之前先看空间。备份文件 `mooncake.tar` 约 900 MB（两份原文件 445 MB、448 MB），写在 `/tmp` 下；两份新库解压后约 50 MB、53 MB，写进 `site-packages`。容器里的这些目录就是运行中 vLLM 的根文件系统，写满会让 vLLM 和过滤器写不了日志：

```bash
df -h /tmp "$MC_SITE"
```

**执行后看到：** 两行（两处在同一个文件系统上时，`Mounted on` 一栏相同）。看 `Avail` 一栏：两处合计至少留 1.2 GB——在同一个文件系统上时，这一个文件系统的 `Avail` 不少于 1.2 GB；不在同一个文件系统上时，`/tmp` 那一行不少于 1.0 GB、`site-packages` 那一行不少于 0.2 GB。不够就停下，不要备份和替换。

两份各自备份（放进第 3 步的备份目录）：

```bash
(cd "${MC_SITE:?}" && tar -cf "${BK:?先按第 3 步设好 BK}/mooncake.tar" $(awk '{print $2}' $M/SHA256SUMS.before)) && tar -tf "$BK/mooncake.tar"
```

**执行后看到：** 两行，`mooncake/libmooncake_store.so` 和 `mooncake_transfer_engine.libs/libmooncake_store-2a608938.so`。

`mooncake.tar` 约 900 MB。建议先拷到宿主机上，确认宿主机上那份完好，再决定要不要删掉容器里的那份：在容器里 `sha256sum "$BK/mooncake.tar"`；在宿主机上 `docker cp <容器名>:<第 3 步的备份目录>/mooncake.tar <宿主机目录>/`，再对拷出来的文件跑一次 `sha256sum`，两个值相同就是完好的。之后需要腾出空间时，再在容器里删掉 `"$BK/mooncake.tar"`；删了的话，回滚前要先拷回来（第 9 步）。

替换。正在运行的 vLLM 还映射着旧文件，所以不能直接覆盖原文件：先解压成同目录下的临时文件，再用 `mv` 换名（换名后旧进程继续用旧文件，重启后才加载新文件）：

```bash
: "${MC_SITE:?}"; for f in $(awk '{print $2}' $M/SHA256SUMS.after); do python3 -c "import lzma, sys; open(sys.argv[2], 'wb').write(lzma.open(sys.argv[1]).read())" "$M/$f.xz" "$MC_SITE/$f.t51new" && chmod --reference="$MC_SITE/$f" "$MC_SITE/$f.t51new" && mv "$MC_SITE/$f.t51new" "$MC_SITE/$f"; done
(cd "$MC_SITE" && sha256sum -c $M/SHA256SUMS.after | grep -v ': OK$')
```

**执行后看到：** 没有任何输出，表示两个文件都和交付的一致。有 `FAILED` 时停下，按第 9 步回滚。

### 5.2 proxy（只在 proxy 所在的机器或容器上）

proxy 的补丁文件和校验和由产物 5 提供（`load_balance_proxy_server_example.py`，基线是 vllm-ascend main `e2f6cb1b4` 上的原文件）。在 proxy 所在的地方：

1. 用检测脚本的 `--proxy-file` 确认现有文件是 main 上那一份（`proxy file=main`）；
2. 备份现有文件，换成产物 5 的文件，按产物 5 的 `SHA256SUMS` 核对；
3. 在启动 proxy 的那个环境里加两个环境变量（写在启动 proxy 的脚本里 `python3 load_balance_proxy_server_example.py` 之前，或者用 `docker run -e` / `docker exec -e` 传给 proxy 进程）：

   ```bash
   export PD_PROXY_DIAG=1
   export PD_PROXY_DIAG_DIR=<proxy 日志目录>
   ```

   `PD_PROXY_DIAG_DIR` 不设时为 `/var/log/pd-diag-proxy`。**这个目录在宿主机上的路径，就是操作说明 B 第 4 步要填的 `<proxy 日志目录>`（`PROXY_LOG_DIR`）。** proxy 在容器里跑时，这个目录要挂到宿主机上，填宿主机那一侧的路径。

**执行后看到：** 第 1 项检测输出 `proxy file=main`；第 2 项按产物 5 的 `SHA256SUMS` 核对时没有 `FAILED`；第 3 项设完环境变量，在重启 proxy（第 7 步）之前看不到变化，`PD_PROXY_DIAG_DIR` 下还没有 `proxy-<端口>/` 目录。

**打开以后要知道的一点：** 请求号改由 proxy 生成，vLLM 日志和面板二里看到的是 proxy 生成的号；客户端带来的号记在 proxy 的那一行里（`client_request_id`）。按客户端请求号查 vLLM 日志的人，要先在 proxy 行里查到对应关系。客户端收到的响应 `id` 也会变：原来客户端带了 `X-Request-Id` 时，响应 `id` 是 `chatcmpl-<客户端的号>`，打开开关后是 `chatcmpl-<proxy 生成的号>`；靠响应 `id` 对账的客户端会受影响。关掉开关（不设 `PD_PROXY_DIAG`）时，proxy 的行为和原文件相同。

## 6. 日志设置【待检测】

- `[PD_DIAG]` 记录：test 集群在 v3 时已经按 v3 说明书步骤 V2、V4 设过 `VLLM_LOGGING_CONFIG_PATH` 和 `PYTHONPATH` 的，这里不用再设；没设过的，按操作说明 B 做。
- Mooncake 的 `[BatchPutTiming]` 行由 glog 写到 Worker 进程的标准错误，不经过 Python logging。要让 glog 一定写标准错误、而且不丢 INFO 级，在 `vllm serve` 之前加 `export GLOG_logtostderr=1`，并确认没有设 `GLOG_minloglevel`（或设为 0）。检测结果 `run` 行的 `glog_env=` 里已经有 `logtostderr=1` 的，不用再加。
- **vLLM 标准错误的去处，就是操作说明 B 第 4 步要填的 `<vLLM 标准错误的来源>`（`MOONCAKE_LOG_FILES`）。** 按检测结果 `run` 行的 `worker_stderr=` 选写法：

  | `worker_stderr=` | 含义 | `MOONCAKE_LOG_FILES` 的写法 |
  |---|---|---|
  | `file` | 标准错误写进了一个文件 | 这个文件在宿主机上能看到（挂载的目录）时，写宿主机路径 `<P 机器地址>=/宿主机/路径/*.log`；只在容器里时，写 `<P 机器地址>=exec:<容器名>:/容器里的路径` |
  | `pipe>tee` 等 `pipe>` 加程序名 | 经管道交给容器里的 `tee` 一类程序，再写文件 | 按 `tee` 写的那个文件，同上一行 |
  | `pipe>outside` | 交给了容器外，一般就是 `docker logs` | `<P 机器地址>=logs:<容器名>` |
  | `tty`、`null`、`socket` | 没有落盘 | 停下，带回检测输出另定 |

  文件的具体路径，在容器里用下面这条命令查（只读，列出每个 Worker 进程的标准错误指向哪里）：

  ```bash
  python3 -c "import os; [print(p, os.readlink('/proc/%s/fd/2' % p)) for p in os.listdir('/proc') if p.isdigit() and (open('/proc/%s/comm' % p).read().startswith('VLLM::Worker') or open('/proc/%s/cmdline' % p, 'rb').read().startswith(b'VLLM::Worker'))]"
  ```

  一台 P 机器上有几个实例、标准错误写进不同文件时，用通配符或分号把它们都写上（写法见操作说明 B 第 4 步）。

## 7. 重启 vLLM 和 proxy（只做一次）【待检测】

在操作说明 B 第 1、2 步（下发新节点文件）做完以后再重启：这一次重启同时用上本说明打的补丁、换的 proxy 文件和 B 下发的新节点文件，不要分两次重启。

按检测结果 `run` 行的 `pid1=` 和 `launcher=` 选一种：

| 情况 | 做法 |
|---|---|
| `pid1=vllm`，或 1 号进程就是 `start_p.sh` / `start_d.sh` | 在宿主机上 `docker restart <容器名>` |
| 1 号进程是 `sleep` 一类占位进程，vLLM 是进容器后手工拉起的 | 按平时的方式停掉 vLLM，再执行一次 `start_p.sh` / `start_d.sh` |

**执行后看到：** vLLM 正常启动，启动日志里有 runner 那一行（例如 `VLLM_USE_V2_MODEL_RUNNER is unset; using Model Runner V2 by default.`）；在容器里 `curl -s -o /dev/null -w '%{http_code}\n' localhost:<端口>/health` 返回 `200`。

proxy 按它平时的启动方式重启，重启后 `PD_PROXY_DIAG_DIR` 下出现 `proxy-<端口>/` 目录，里面的文件第一行是 `pd_file_open`。

## 8. 确认生效

1. 再跑一次检测，**用本交付包里的检测脚本**（`detect/t51_dsv41_check.py`，SHA-256 以 `877eb55e` 开头或更新的版本）：`vllm=` 行 `state=v0.32.0+01`，`ascend=` 行 `state=f9c+t51kit`；P 容器换过 Mooncake 后 `mooncake=` 行 `timing=1`。装补丁之前用过的那一版（`78165c38`）不认识本套补丁的最终版本，会把装好的 vllm-ascend 报成 `state=modified`。
2. 经 proxy 发几个请求，然后看 `[PD_DIAG]` 记录（具体在哪个文件看，取决于第 6 步：没开 v3 过滤器时在 vLLM 的标准输出日志里，开了时用 v3 的 `bin/diag.sh vllm`）。没开过滤器时可以这样查：

   ```bash
   grep -o '"event":"[a-z_]*"' <vLLM 日志文件> | sort | uniq -c
   grep -c '"wait_for_put"' <vLLM 日志文件>
   ```

   **执行后看到：** 第一条列出 `api_summary`、`core_summary`、`worker_summary`、`pool_task`、`pd_transfer_summary` 五种；第二条在 P 上大于 0。
3. P 上 `[BatchPutTiming]` 的行数大于 0（在第 6 步定下的文件里查）。

## 9. 回滚

**顺序（这样只需重启一次）：**
1. 先做操作说明 B 的回滚：监控机退回 v3，下发 v3 的节点文件；
2. 再做本步：恢复备份的文件，去掉 proxy 的环境变量；
3. 最后按第 7 步重启一次。

如果先重启、再做 B 的回滚，节点上还跑着新的过滤器，就要重启两次。

容器里的 `/tmp/t51-kit` 不在了，先重做第 1 步（用宿主机上留着的交付包）；第 3 步的备份目录不在了，先把宿主机上另存的那一份拷回容器：`docker cp <备份目录> <容器名>:/tmp/`。容器里的 `mooncake.tar` 已经删了（第 5.1 步），先从宿主机拷回来：`docker cp <宿主机目录>/mooncake.tar <容器名>:<第 3 步的备份目录>/`。

vLLM 和 vllm-ascend 一起回滚：

```bash
BK=<第 3 步记下的备份目录>; K=/tmp/t51-kit/patches
tar -xf "$BK/vllm.tar" -C "$VLLM_ROOT" && (cd "$VLLM_ROOT" && rm -f $(cat $K/vllm/NEW_FILES.txt) && sha256sum -c $K/vllm/SHA256SUMS.before | grep -v ': OK$')
tar -xf "$BK/vllm-ascend.tar" -C "$ASCEND_ROOT" && (cd "$ASCEND_ROOT" && rm -f $(cat $K/vllm-ascend/NEW_FILES.txt) && sha256sum -c $K/vllm-ascend/SHA256SUMS.before | grep -v ': OK$')
```

**执行后看到：** 没有任何输出，表示文件已恢复成公开版本。`VLLM_ROOT`、`ASCEND_ROOT` 要先按第 3 步第一段重新设好。

Mooncake（只在 P 容器上，`MC_SITE`、`M` 按第 5.1 步第一段重新设好）。和替换时一样，先解到 `site-packages` 下的临时目录（同一个文件系统，`mv` 才是换名而不是重写），再用 `mv` 换名：

```bash
R=$(mktemp -d -p "${MC_SITE:?}") && tar -xf "${BK:?}/mooncake.tar" -C "$R" && for f in $(awk '{print $2}' $M/SHA256SUMS.before); do mv "$R/$f" "$MC_SITE/$f"; done && rm -rf "$R" && (cd "$MC_SITE" && sha256sum -c $M/SHA256SUMS.before | grep -v ': OK$')
```

**执行后看到：** 没有任何输出，表示两个文件已恢复成 OBS 原文件。

proxy：换回第 5.2 步备份的原文件，去掉 `PD_PROXY_DIAG` 和 `PD_PROXY_DIAG_DIR`（只去掉 `PD_PROXY_DIAG` 也等于关闭）。

然后按第 7 步重启。重启后再跑一次检测：`vllm=`、`ascend=` 两行回到 `state=v0.32.0`、`state=f9c8e0bdb`；P 容器的 `mooncake=` 行回到 `state=obs-v0.3.14rc1-cp312-aarch64`；proxy 上用 `--proxy-file` 跑的那一行回到 `proxy file=main`（和第 5.2 步第 1 条对应）。

## 10. 实验室走查记录

在 `python:3.12-slim` 容器里，用公开源码造的 `/vllm-workspace/vllm`、`/vllm-workspace/vllm-ascend`，按第 1、3、4、9 步原文执行，结果见 `guides/lab/walk_A.txt`。第 5–8 步依赖现场，没有走。
