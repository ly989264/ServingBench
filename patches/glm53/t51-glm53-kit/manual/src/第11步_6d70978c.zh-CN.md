# 操作说明 A 第 11 步：替换 Mooncake 库（glm5.3）

**适用范围：** glm5.3 测试集群（面板二里的集群 `test-glm53`）的 P、D 容器，补丁包操作说明 A 第 1–10 步已经做完的。只换每个容器里的一个文件 `libmooncake_store.so`，不新建镜像，不新建容器。

**本包里有什么：** 替换库 `lib/libmooncake_store.so`、换库前的只读核对 `precheck/dist/t51_mc_precheck.py`（glm53mc-5）和它的使用说明、校验表 `SHA256SUMS`、本说明。换库以后的核对用补丁包里的检测脚本（`/tmp/t51-kit/detect/t51_glm53_check.py`，sha256 `f9e6073f…`），不在本包里。

## 11.0 先读这几条

- **前提：补丁包第 1–10 步已经做完。** 第 8 步检测脚本报的是 `verdict vllm=ok ascend=ok(site_local=<n>) p2d=ok mooncake=not_replaced`，即现场原装的 Mooncake 库（小核对 f-1 带回的那一份）。第 8 步报 `mooncake=BAD(other)` 的，不做这一步，把那一行带回来。第 7.4 节在这一步以前就量到 `[BatchPutTiming]` 行的，也先不做，把这些行带回来。
- **换了以后多了什么：** P 侧「后台 wait_for_save → put」下面的 3 个子段 submit、wait_for_transfer、finalize 有值，47 段里 45 段有值（补丁包第 7.3 节）。各实例的 vLLM 日志里多出 `[BatchPutTiming]` 行，由现场自己轮转（补丁包第 6 步结论三）。
- **只换一个文件。** `<Mooncake 目录>/libmooncake_store.so` 换成本包的 `lib/libmooncake_store.so`。store 模块、`libtransfer_engine.so`、`libmooncake_common.so`、`libasio.so`、`ascend_transport.so` 和自带的系统库都不动。
- **每个容器先跑换库前的核对（第 11.2 节），是最后一道关。** 最后一行是 `mc_pre result=ok_replace` 才往下做；其余结果都停下，不换任何一个容器，把输出带回来。
- **原库必须备份、拷回监控机（第 11.3 节）。** 现场的原库是现场自己构建的，本包里没有这一份，丢了无法重建。备份拷到监控机、核对通过以后，才做第 11.4 节；回滚（第 11.9 节）用监控机上的这一份。
- **备份不能放在 `<Mooncake 目录>` 或任何 `mooncake*.libs` 目录里**，否则换库前的核对和检测脚本会把它当成第二份 store 库。
- **换库用改名，不覆盖。** 新文件先复制到同一目录下的临时名，再 `mv` 改名。正在运行的 vLLM 继续用它已经映射的原库，不受影响；直接覆盖原文件可能让正在运行的进程崩溃。
- **换完要重启这个容器里的 vLLM 才生效，尽快重启。** 重启以前，运行中的进程如果新起一个子进程，子进程会加载新库，新旧两份库混在一起。
- **每个容器只做一次第 11.1–11.4 节，第 11.5 节起才分批。** 分批和补丁包第 7 步相同：第 1 批是一台 D 机器的容器（这台机器上的全部 D 实例），第 2 批是其余 D 机器的容器，第 3 批是 P 机器的容器。一个容器在它的实例第一次重启以前做完第 11.1–11.4 节；同一个容器的实例要是分在不同批次重启，后面的批次不再重做第 11.1–11.4 节，只做第 11.5 节起的各节。这一批的检查都通过了，再做下一批。
- **换库到重启之间可能隔几个小时。** 在这段时间里，这个容器里还没重启的实例继续用原库，已经重启的实例用新库。这样做的前提：补丁 04 只给 `Client` 加了三个非虚的私有成员函数声明，没有加数据成员、static 成员和 thread_local，没有虚函数，没有动虚表，所以两种构建同时在不同进程里运行时，布局和进程间传递的数据格式都相同（核实结果见交付说明）。
- **不设 `MC_LOG_DIR`。** 重启时也不要加。Mooncake 的传输引擎只有在设了它时才初始化 glog，把日志写进那个目录；不设时写标准错误，`[BatchPutTiming]` 行因而写进各实例的 vLLM 日志文件，面板二的 `MOONCAKE_LOG_FILES` 填的就是这些文件（补丁包第 11 步）。
- 所有容器里的命令都以 root 执行：先在那台机器上 `docker exec -it <容器名> bash` 进入容器。
- 任何一步的结果和「执行后马上会看到」不一样时，停下，把那一步的输出带回来，不要继续。

**文中的占位符：** `<用户目录>`、`<SSH 账号>`、`<机器地址>`、`<机器>`、`<容器名>`、`<vLLM 日志文件>`、`<启动文件目录>`、`<容器里的 vLLM 日志文件>` 和端口同补丁包操作说明 A 第 0.2 节。本步另有：

| 占位符 | 含义 | 在哪里查 |
|---|---|---|
| `<Mooncake 目录>` | 容器里 Mooncake 装在哪里，即 `site-packages/mooncake` | 第 11.3 节第一条命令的输出 |
| `<MC 备份目录>` | 第 11.3 节在容器里建的原库备份目录 | 第 11.3 节的输出 |
| `<MC 时间戳>` | `<MC 备份目录>` 名字末尾的 `<年月日>-<时分秒>`，例如 `/tmp/t51-glm53-mc-backup-20261121-103000` 的 `20261121-103000` | 第 11.3 节的输出 |
| `<原库 sha256>` | 第 11.3 节记下的原库的 sha256（64 位） | 第 11.3 节的输出 |

## 11.1 把增量包放进容器并核对

**机器：** 先在监控机上，再在 glm5.3 的那台机器上，最后在容器里。

增量包 `t51-glm53-mooncake-kit.tar` 和它的 `t51-glm53-mooncake-kit.tar.sha256` 放在监控机的 `<用户目录>/vllm-pd-patch-glm53/` 下。在监控机上先核对，再拷到那台机器：

```bash
cd <用户目录>/vllm-pd-patch-glm53 && LC_ALL=C sha256sum -c t51-glm53-mooncake-kit.tar.sha256
```

**执行后马上会看到：** `t51-glm53-mooncake-kit.tar: OK`。

```bash
scp <用户目录>/vllm-pd-patch-glm53/t51-glm53-mooncake-kit.tar <SSH 账号>@<机器地址>:/tmp/
```

在那台机器上，拷进容器：

```bash
docker cp /tmp/t51-glm53-mooncake-kit.tar <容器名>:/tmp/
```

在容器里解开、核对，再确认补丁包的检测脚本还在：

```bash
mkdir -p /tmp/t51-mc && tar -xf /tmp/t51-glm53-mooncake-kit.tar -C /tmp/t51-mc && cd /tmp/t51-mc && LC_ALL=C sha256sum -c SHA256SUMS | grep -v ': OK$'; sha256sum /tmp/t51-kit/detect/t51_glm53_check.py | cut -c1-16
```

**执行后马上会看到：** 只有一行 `f9e6073f692dd4ea`。前面没有任何输出，表示增量包完好；有输出说明在拷贝中损坏，重新拷贝。最后一行报 `No such file or directory` 时，容器的 `/tmp` 被清空过：先按补丁包第 1 步重做容器里的那一条（用那台机器上留着的补丁包），再执行这一条。

那台机器上的 `/tmp/t51-glm53-mooncake-kit.tar` 留着，不要删：容器的 `/tmp` 可能被清空，回滚以后要重换时还要用。

**对同机其他服务的影响：** 只多了一个约 15 MB 的文件和解开的目录。

## 11.2 换库前的核对

**机器：** 容器里，接着第 11.1 节，在容器里的 vLLM 正在运行时跑。每个要换库的容器都跑一次。脚本只读，几秒内结束，不写任何文件；各行的含义和判决规则见本包的 `precheck/使用说明.zh-CN.md`。

```bash
python3 -I -S -B /tmp/t51-mc/precheck/dist/t51_mc_precheck.py; echo "exit=$?"
```

**执行后马上会看到：** 12 行，然后 `exit=0`。其中这几行要看：

- 第一行 `t51_mc_precheck=glm53mc-5 build=78566d4-rwdi-ut-nouring-mp9.0.0/repl-p04i-0625b51e pid=<n> loaded=<n>`：`pid=` 是找到的 vLLM 进程号，不是 `none`。
- `mc_orig size=17610953 sha16=3bf82f8b0c5ab715 f1=same …`：现场原库和小核对 f-1 带回的是同一份，`second_store=none`。
- `mc_build sections_other=same … result=same_build`：现场原库和实验室的核对件是同一次构建。`actual=` 和 `predicted=` 相同，`L'=`、`N'=` 是现场构建路径的长度和条数，脚本不打印路径本身。
- `mc_exports … differing_families=none replacement_vs_site=-0/+3(expected -0/+3)`：替换库只比现场原库多 3 个导出名，不少任何一个。
- `consumers_ok n_elf=<n> removed_names=0`：包里没有文件引用替换库不再导出的名字。
- 最后一行 `mc_pre result=ok_replace`。

**停下的情况：** 最后一行不是 `mc_pre result=ok_replace`（`STOP_…`，退出码 2），或者只打印两行 `mc_pre result=STOP_missing`，或者打印一行 `REFUSED`：这个容器不换，其余容器也先不换，把整段输出带回来。

**带回来的东西：** 终端打印的全部行，每个容器一份。

**对同机其他服务的影响：** 只读；自己降到 nice 19，60 秒超时，实验室实测读取约 54 MB（`libmooncake_store.so` 读两遍）；不导入 Mooncake，不起子进程，不联网。

## 11.3 找到 Mooncake 目录，备份原库

**机器：** 容器里；最后在那台机器上和监控机上拷备份。

找到 Mooncake 装在哪里（只查找位置，不导入 Mooncake），再看运行中的进程映射的是不是这一份：

```bash
MC_PKG=$(python3 -I -B -c "import importlib.util as u; print(list(u.find_spec('mooncake').submodule_search_locations)[0])"); MC_LIB="$MC_PKG/libmooncake_store.so"
echo "$MC_LIB"; ls -l "$MC_LIB" | awk '{print $1, $5}'
for d in /proc/[0-9]*; do grep -h 'libmooncake_store' "$d/maps" 2>/dev/null | awk '{print $6, $7}' | LC_ALL=C sort -u; done | LC_ALL=C sort | uniq -c
```

**执行后马上会看到：** 三部分。

- 第一行是 `<Mooncake 目录>/libmooncake_store.so`，`<Mooncake 目录>` 在 `site-packages` 下。
- 第二行是这个文件的权限和大小，大小是 `17610953`。
- 最后是映射了 store 库的进程个数和路径，只有一行：`<进程个数> <和第一行相同的路径>`。
- 最后一部分的路径和第一行不同、不止一行，或者行末带 `(deleted)`，说明运行中的 vLLM 用的不是这里找到的那一份，或者库已经被换过：停下，把这几行带回来。本容器已经做过第 11.4 节的，这一条不适用，不要重跑第 11.3 节。

记下运行中的 vLLM 的库搜索路径（第 11.5 节的导入核对要在和 vLLM 相同的 `LD_LIBRARY_PATH` 下做；容器里另开的 shell 不一定有 NPU 驱动库的路径）：

```bash
: > /tmp/t51-mc/vllm.ld_library_path; for d in /proc/[0-9]*; do a=$(tr '\0' ' ' < "$d/cmdline" 2>/dev/null); case "$a" in *"vllm serve"*|*vllm.entrypoints.openai.api_server*) tr '\0' '\n' < "$d/environ" | grep '^LD_LIBRARY_PATH=' > /tmp/t51-mc/vllm.ld_library_path; break;; esac; done; echo "vLLM 的 LD_LIBRARY_PATH 已记下：$(wc -c < /tmp/t51-mc/vllm.ld_library_path) 字节"
```

**执行后马上会看到：** `vLLM 的 LD_LIBRARY_PATH 已记下：<n> 字节`，n 大于 0。是 0 时，说明 vLLM 进程没有设 `LD_LIBRARY_PATH`，第 11.5 节的导入核对就用当前 shell 的环境，照常往下做。

备份：

```bash
BKBASE=/tmp; [ "$(stat -f -c %T /tmp)" = tmpfs ] && BKBASE=/root
BKMC=$BKBASE/t51-glm53-mc-backup-$(date +%Y%m%d-%H%M%S); mkdir -p "$BKMC"; echo "$BKMC"
cp -p "$MC_LIB" "$BKMC/libmooncake_store.so" && (cd "$BKMC" && sha256sum libmooncake_store.so > SHA256SUMS && cat SHA256SUMS)
```

**执行后马上会看到：** 备份目录名 `/tmp/t51-glm53-mc-backup-<MC 时间戳>`（`/tmp` 是 tmpfs 时在 `/root` 下），然后一行 `<64 位> libmooncake_store.so`，前 16 位是 `3bf82f8b0c5ab715`。把备份目录名和这 64 位（`<原库 sha256>`）记下来，回滚时要用。前 16 位不是这个值时停下，把输出带回来。

**把备份拷到那台机器上，再拷回监控机（必做）。** 在那台机器上：

```bash
docker cp <容器名>:<MC 备份目录> /tmp/
```

在监控机上：

```bash
mkdir -p <用户目录>/vllm-pd-patch-glm53/backup && scp -r <SSH 账号>@<机器地址>:/tmp/t51-glm53-mc-backup-<MC 时间戳> <用户目录>/vllm-pd-patch-glm53/backup/<机器>-mc-<MC 时间戳>
```

```bash
cd <用户目录>/vllm-pd-patch-glm53/backup/<机器>-mc-<MC 时间戳> && LC_ALL=C sha256sum -c SHA256SUMS
```

**执行后马上会看到：** `libmooncake_store.so: OK`：监控机上的备份和容器里的逐字节相同。有 `FAILED` 时重新拷；核对通过以前不做第 11.4 节。

**对同机其他服务的影响：** 备份文件约 17 MB，容器里、那台机器的 `/tmp` 下、监控机上各一份。

## 11.4 换库

**机器：** 容器里，接着第 11.3 节，要用第 11.3 节设好的 `MC_PKG`、`MC_LIB`、`BKMC`。先确认它们都在：

```bash
: "${MC_PKG:?}" "${MC_LIB:?}" "${BKMC:?}" && echo "变量都在：$BKMC"
```

**执行后马上会看到：** `变量都在：<MC 备份目录>`。报 `parameter null or not set` 时，说明换过 shell：按第 11.3 节第一段重新设好 `MC_PKG`、`MC_LIB`，再设 `BKMC=<MC 备份目录>`（不要重做备份），然后再执行这一条。

换库：

```bash
cp /tmp/t51-mc/lib/libmooncake_store.so "$MC_PKG/.t51_new_store.so" && chmod --reference="$MC_LIB" "$MC_PKG/.t51_new_store.so" && chown --reference="$MC_LIB" "$MC_PKG/.t51_new_store.so" && mv -f "$MC_PKG/.t51_new_store.so" "$MC_LIB" && sha256sum "$MC_LIB" | cut -c1-16 && ls -l "$MC_LIB" | awk '{print $1, $5}'
```

**执行后马上会看到：** `3131784385846064`，然后一行权限和大小：权限和第 11.3 节相同，大小是 `15402248`。sha256 不是这个值时停下，按第 11.9 节回滚。

这条命令中途报错、没有打印 `3131784385846064` 时（例如 `mv` 没成功），先删掉临时文件 `rm -f "$MC_PKG/.t51_new_store.so"`，再执行一次。

再看运行中的进程：

```bash
for d in /proc/[0-9]*; do grep -h 'libmooncake_store' "$d/maps" 2>/dev/null | awk '{print $6, $7}' | LC_ALL=C sort -u; done | LC_ALL=C sort | uniq -c
```

**执行后马上会看到：** 一行 `<进程个数> <Mooncake 目录>/libmooncake_store.so (deleted)`，进程个数和第 11.3 节相同。`(deleted)` 表示运行中的进程还在用它们已经映射的原库，原库的文件名已经让给了新库；重启以后它们才加载新库。

- 进程个数比第 11.3 节少，但路径对、行末都是 `(deleted)`：有进程在这段时间里退出了，把两次的个数记下来，继续往下做。
- 出现行末没有 `(deleted)` 的行：已经有进程加载了新库（换库以后新起的进程），停下，把这几行带回来。

**对同机其他服务的影响：** 运行中的 vLLM 不受影响，还在用原库。原库的磁盘空间要等这些进程退出以后才释放（约 17 MB）。

## 11.5 重启，分批上线

**怎么重启：** 照补丁包第 7 步的方式：按平时的方式停掉这个容器里的 vLLM 实例，再用原来的启动文件拉起，参数、环境变量和停之前相同。不要加 `MC_LOG_DIR`。重启前后照补丁包第 7.1、7.2 节核对命令行和日志配置，`BK` 用 `<MC 备份目录>`（`BK=<MC 备份目录>`）。

**第 1 批的那一个容器：实例停掉以后、重新拉起以前，核对新库能加载。** 其余容器、其余批次都不做这一条。

```bash
(L=$(cat /tmp/t51-mc/vllm.ld_library_path); [ -n "$L" ] && export "$L"; python3 -B -c "import time; t = time.time(); import mooncake.store as s; print(s.__file__, '%.1f s' % (time.time() - t))")
```

**执行后马上会看到：** 一行 `<Mooncake 目录>/store.cpython-311-aarch64-linux-gnu.so <用时> s`。

- 这条命令起一个 Python 进程，用第 11.3 节记下的 vLLM 的 `LD_LIBRARY_PATH` 导入 Mooncake 的 store 模块，导入完就退出：store 模块加载新的 `libmooncake_store.so`，新库缺名字时这里就报 `undefined symbol`。不连 Mooncake 的 master，不开端口；`-B` 让它不写 `.pyc`。
- 放在实例停掉以后做：Mooncake 的二进制扩展在导入时做什么没有在卡上验证，实例停掉以后 NPU 已经空出来。
- 报任何错误时，不要用新库拉起：按第 11.9 节把这个容器回滚到原库，再拉起，把这一行和 `/tmp/t51-mc/vllm.ld_library_path` 的字节数带回来。
  - `undefined symbol` 是新库和现场的库对不上；
  - `libascend_hal.so: cannot open shared object file` 这一类找不到 CANN、驱动库的错误，多半是导入时的环境和 vLLM 不同（例如容器的 `/tmp` 被清空过，记下的 `LD_LIBRARY_PATH` 没了），不一定是新库的问题，由我们看过再决定。

然后用原来的启动文件拉起。

**重启以后，在那台机器的容器里看：** 先照补丁包第 7.2 节看每个实例的 `/health`、命令行、日志配置。再看每个映射了 store 库的进程属于哪个实例、用的是哪一份：

```bash
port_of() { local p=$1 q; while [ -n "$p" ] && [ "$p" -gt 1 ]; do q=$(tr '\0' ' ' < /proc/$p/cmdline 2>/dev/null | grep -oE -- '--port[ =][0-9]+' | grep -oE '[0-9]+$' | head -1); [ -n "$q" ] && { echo "$q"; return; }; p=$(awk '/^PPid:/{print $2}' /proc/$p/status 2>/dev/null); done; echo '?'; }
for d in /proc/[0-9]*; do m=$(grep -h 'libmooncake_store' "$d/maps" 2>/dev/null | awk '{print $6, $7}' | LC_ALL=C sort -u); [ -n "$m" ] || continue; case "$m" in *"(deleted)"*) u=old;; *) u=new;; esac; echo "port=$(port_of ${d#/proc/}) $u"; done | LC_ALL=C sort | uniq -c
```

**执行后马上会看到：** 每个端口一行或两行：`<进程个数> port=<端口> new` 或 `<进程个数> port=<端口> old`。`new` 是行末没有 `(deleted)`、用新库的进程，`old` 是还在用原库的进程。每个进程按它自己或上级进程命令行里的 `--port` 对到实例：Worker 这类子进程的命令行可能被改成进程名，所以沿着父进程往上找，找到第一个带 `--port` 的为止。

- 这一批的实例（这次重启的端口）都只有 `new`：重启以后加载的是新库，正常。
- 本容器还有没重启的实例时，它们的端口是 `old`，这是预期的：核对这些端口都是还没轮到的实例。
- 这一批的实例里还有 `old`，或者出现 `port=?` 的 `old`（对不上实例的进程还在用原库）：把这几行带回来。

**vLLM 日志里没有 Mooncake 库的加载错误**（在那台机器上，宿主机里）：

```bash
grep -nE "undefined symbol|libmooncake_store" <vLLM 日志文件> | head -20
```

**执行后马上会看到：** 没有输出。有输出时这一批算失败（第 11.8 节）。

## 11.6 用检测脚本核对

**机器：** 容器里，第 11.5 节的检查通过以后。每个换了库的容器跑一次。命令和补丁包第 8 步相同（P 的容器 `--start-script` 是 `start_p.sh`，D 的容器是 `start_d0.sh`），最后加 `--baseline <这个容器第 2 步记下的 baseline_token>`。

P 的容器：

```bash
python3 -I -S -B /tmp/t51-kit/detect/t51_glm53_check.py --run-dp-template <启动文件目录>/run_dp_template.sh --launch-online-dp <启动文件目录>/launch_online_dp.py --start-script <启动文件目录>/start_p.sh --mooncake-json <启动文件目录>/mooncake.json --start-mooncake <启动文件目录>/start_mooncake.sh --startup-log <容器里的 vLLM 日志文件> --baseline <第 2 步的 baseline_token>
```

D 的容器：

```bash
python3 -I -S -B /tmp/t51-kit/detect/t51_glm53_check.py --run-dp-template <启动文件目录>/run_dp_template.sh --launch-online-dp <启动文件目录>/launch_online_dp.py --start-script <启动文件目录>/start_d0.sh --mooncake-json <启动文件目录>/mooncake.json --start-mooncake <启动文件目录>/start_mooncake.sh --startup-log <容器里的 vLLM 日志文件> --baseline <第 2 步的 baseline_token>
```

**执行后马上会看到：** 不超过 20 行，退出码 0。Mooncake 看这两处：

- `verdict vllm=ok ascend=ok(site_local=<n>) p2d=ok mooncake=ok`：最后一项从 `mooncake=not_replaced` 变成 `mooncake=ok`。
- `mooncake=` 一行里是 `changed_since_install=1 timing=2 store=replacement`（第 8 步时是 `changed_since_install=0 timing=0 store=site_original`；现场第 1、2 次检测的 `mooncake=` 一行都是 `changed_since_install=0 timing=0`）：
  - `store=replacement`：`libmooncake_store.so` 是交付的替换库（sha256 `31317843…`）；
  - `timing=2`：库里有 `[BatchPutTiming]`、`[BatchUpsertTiming]` 两种计时行；
  - `changed_since_install=1`：安装记录里还是原库的哈希，属预期，不是漂移。
- 其余各行和第 8 步相同：`site_patches` 一行和第 2 步相同，`run` 行是 `logcfg=set`，单独一行 `baseline=same`。

**这一批算失败（第 11.8 节）的：** `mooncake=BAD(other)`（`store=other`：换上的既不是原库也不是交付的替换库）；`verdict` 的 vllm、ascend、p2d 三项有一项不是 `ok` 或 `ok(…)`；出现 `BASELINE_DIFF`；`run` 行是 `logcfg=unset`；`site_patches` 和第 2 步不同。还是 `mooncake=not_replaced`（`store=site_original`）时，说明库没有换上：停下，把第 11.4 节的输出和这一段带回来。把全部输出带回来。

带回以后照补丁包第 2 步删掉明细文件。

**对同机其他服务的影响：** 同补丁包第 2 步，只读。

## 11.7 换了以后看什么

**`[BatchPutTiming]` 行**（在那台机器上，宿主机里；有请求以后）：

```bash
for f in <vLLM 日志文件>; do echo "$f $(grep -c '\[BatchPutTiming\]' "$f") $(grep '\[BatchPutTiming\]' "$f" | grep -c 'complete=false')"; done
```

**执行后马上会看到：** 每个文件一行：文件名、`[BatchPutTiming]` 行数、其中 `complete=false` 的行数。

- P 机器（第 3 批）：行数大于 0，`complete=false` 的是 0。
- D 机器（第 1、2 批）：D 只在往缓存池保存时写这种行，不保存时两个数都是 0。
- `complete=false` 不是 0：不算失败，服务和看板读的这 3 段（submit、wait_for_transfer、finalize）不受影响；但必须带回来。这种行只有原签名的旧接口会写，而核对时没有找到它的调用方（第 11.2 节 `consumers_ok`），出现了就说明核对的调用方扫描漏了东西：可能是包目录以外的文件在用它，或者有代码按名字（`dlsym`）取它，或者现场的包不是核对时扫的那一份。

**看板**（第 3 批做完以后）：照新增集群说明 10.2 看，P 侧后台 wait_for_save/put 下的 3 段 submit、wait_for_transfer、finalize 有值，47 段里 45 段有值（补丁包第 7.3 节）。重启 10 分钟以后、上一条命令数出的 `[BatchPutTiming]` 行数大于 0，这 3 段仍为空的：不回滚（服务不受影响），把上面那条命令的输出和新增集群说明第 9 步填的 `MOONCAKE_LOG_FILES` 带回来。

**磁盘**（每批，这一批的每台机器）：照补丁包第 7.4 节，重启 5 分钟以后记起点，30 分钟以后再量一次，按补丁包第 6 步结论六的阈值判（第 11.8 节）：

- `[BatchPutTiming]` 每台机器每天不超过 43.1 GB（P 机器；D 机器不往缓存池保存时应当是 0 行）。
- 节点目录和 vLLM 日志在同一个文件系统上时（补丁包第 7.1 节的 `df` 只出一行），把 `[BatchPutTiming]` 每天的增长 × 现场 vLLM 日志保留的天数也加进节点目录那一条的预算，和同一个 `Avail` 的一半比（补丁包第 7.6 节磁盘项第三条）。
- vLLM 日志由现场自己轮转：这一路的增长按现场保留 vLLM 日志的天数，算进 vLLM 日志所在文件系统的空间（补丁包第 6 步结论三）。

## 11.8 怎么判失败，失败了怎么办

**先停下、带回的：** 第 11.2 节不是 `ok_replace`；第 11.3 节原库的前 16 位不是 `3bf82f8b0c5ab715`，或者映射的路径不对；第 11.6 节仍是 `mooncake=not_replaced`。

**这一批算失败，** 只要有下面任何一条：

- 第 1 批导入核对报错（第 11.5 节）；
- 补丁包第 7.6 节里重启本身的几条：有实例的 `/health` 不是 `200`，或者起不来；有 vLLM 进程没有设 `VLLM_LOGGING_CONFIG_PATH`；
- vLLM 日志里有 `undefined symbol` 或 `libmooncake_store` 的错误（第 11.5 节）；
- 第 11.6 节：`mooncake=BAD(other)`，vllm、ascend、p2d 三项有一项不是 `ok` 或 `ok(…)`，出现 `BASELINE_DIFF`，`run` 行是 `logcfg=unset`，`site_patches` 和第 2 步不同；
- 磁盘项（第 11.7 节）：`[BatchPutTiming]` 每台机器每天超过 43.1 GB，或者同一个文件系统上的预算超过 `Avail` 的一半。

**失败了：** 停下，不做下一批。这一批的每个容器按第 11.9 节回滚 Mooncake 库，再按第 11.5 节的方式重启一次；补丁包第 1–10 步打的补丁不动，已经成功的批不动。把这一批的检查输出、vLLM 启动日志带回来。

**每批带回来的东西：**

- 第 11.2 节的全部输出（每个容器）；
- 第 11.3 节的备份目录名和 `<原库 sha256>`（每个容器）；
- 第 11.5 节重启后按端口数的那几行，补丁包第 7.2 节的比较结果和日志配置那一行；
- 第 11.6 节检测脚本的全部输出（每个容器）；
- 第 11.7 节 `[BatchPutTiming]` 那条命令的输出和补丁包第 7.4 节的那一行（每台机器）；
- 第 1 批另加：第 11.5 节导入核对的那一行。

**对同机其他服务的影响：** 重启本身和平时重启 vLLM 一样，同一台机器上别的容器不动。换了库的实例往 vLLM 日志写 `[BatchPutTiming]` 行，每天的量见补丁包第 6 步结论三、结论六。

## 11.9 回滚

**机器：** 监控机、那台机器，然后容器里；最后按第 11.5 节重启。和补丁包第 9 步一起回滚时，两边的文件都恢复以后只重启一次。

**用监控机上的备份。** 先把第 11.3 节拷到监控机的那一份送回容器。在监控机上：

```bash
ssh <SSH 账号>@<机器地址> mkdir -p /tmp/<机器>-mc-<MC 时间戳> && scp <用户目录>/vllm-pd-patch-glm53/backup/<机器>-mc-<MC 时间戳>/* <SSH 账号>@<机器地址>:/tmp/<机器>-mc-<MC 时间戳>/
```

在那台机器上：

```bash
docker cp /tmp/<机器>-mc-<MC 时间戳>/. <容器名>:/tmp/<机器>-mc-<MC 时间戳>
```

在容器里核对送回来的备份：

```bash
cd /tmp/<机器>-mc-<MC 时间戳> && LC_ALL=C sha256sum -c SHA256SUMS && cut -c1-64 SHA256SUMS
```

**执行后马上会看到：** `libmooncake_store.so: OK`，然后是 64 位的 sha256，和第 11.3 节记下的 `<原库 sha256>` 相同。

按第 11.3 节第一段那条命令的第一行重新设好 `MC_PKG`、`MC_LIB`。恢复原库：

```bash
BKMC=/tmp/<机器>-mc-<MC 时间戳>
cp "$BKMC/libmooncake_store.so" "$MC_PKG/.t51_orig_store.so" && chmod --reference="$MC_LIB" "$MC_PKG/.t51_orig_store.so" && chown --reference="$MC_LIB" "$MC_PKG/.t51_orig_store.so" && mv -f "$MC_PKG/.t51_orig_store.so" "$MC_LIB" && sha256sum "$MC_LIB" | cut -c1-16 && ls -l "$MC_LIB" | awk '{print $1, $5}'
```

**执行后马上会看到：** `3bf82f8b0c5ab715`，然后一行权限和大小，大小是 `17610953`：原库已经恢复。

然后按第 11.5 节重启（重启前后照补丁包第 7.1、7.2 节核对命令行，`BK` 用这里的 `BKMC`；不做导入核对）。重启以后照第 11.5 节按端口看映射：回滚以后重启过的端口，行末都没有 `(deleted)`，这时它们用的是原库（第 11.9 节恢复的那一份），那条命令把它们记作 `new`。再照第 11.6 节跑检测脚本：

- `verdict` 最后一项是 `mooncake=not_replaced`；
- `mooncake=` 一行是 `timing=0 store=site_original`，`changed_since_install` 和第 8 步时相同（现场第 1、2 次检测都是 0）。

回滚以后，vLLM 日志里不再有新的 `[BatchPutTiming]` 行，看板上 P 侧后台 wait_for_save/put 下的 3 段重新为空，其余各段不受影响。

## 11.10 实验室走查记录

在 `quay.io/ascend/cann:9.2.0-a3-openeuler24.03-py3.11` 容器（`docker run --rm`，没有 NPU；容器里是 Python 3.11.17）里，把现场样式的 Mooncake 包装进 `site-packages`：未打补丁的核对件代替现场原库，store 模块和兄弟库按现场的文件名放好，另加一份带 `RECORD` 的安装信息。用交付的增量包，按本文原文执行第 11.1–11.7、11.9 节的命令。走查脚本和完整输出在 t51-glm53-kit 的 `mooncake/increment/lab/`（`walk_11.sh`、`walk_11.txt`），不在本包里。

**替身 vLLM：** 一个命令行为 `vllm serve sim --port 9100` 的 Python 进程。它带 CANN 的环境变量（和现场启动脚本给 vLLM 的一样），导入 `mooncake.store`，经本机的 `mooncake_master`（`MC_FORCE_TCP=1`）put 4 个键，然后一直运行；它的标准输出、标准错误写进一个文件，充当 vLLM 日志文件。「重启」是停掉它再起一个；实验室的 master 跟着重启。同一个容器里另起一个端口 9101 的替身，整个走查里不重启，充当还没轮到的实例。

**结果：**

- 第 11.1 节：监控机上 `t51-glm53-mooncake-kit.tar: OK`；容器里核对没有输出，最后一行 `f9e6073f692dd4ea`。
- 第 11.2 节：
  - 交付的核对脚本内嵌的是现场 f-1 的值，在实验室的替身原库上 `f1=DIFFERS`，`mc_pre result=STOP_changed`，`exit=2`，按规则停下；
  - 改用把替身原库的大小和 sha16 当作现场值的测试版，12 行，`mc_build … result=same_build`，`replacement_vs_site=-0/+3(expected -0/+3)`，`consumers_ok n_elf=12 removed_names=0`，`mc_pre result=ok_replace`，`exit=0`，接着往下走。
- 第 11.3 节：路径在 `site-packages/mooncake` 下，大小 `15401808`；映射那一行是 `2 <同一路径>`（两个替身）；`vLLM 的 LD_LIBRARY_PATH 已记下：1042 字节`；备份到「那台机器」、再到「监控机」以后核对是 `libmooncake_store.so: OK`。
- 第 11.4 节：在没有这些变量的新 shell 里，变量兜底那条报 `MC_PKG: parameter null or not set`；换库以后是 `3131784385846064` 和 `15402248`；映射那一行变成 `2 <同一路径> (deleted)`，两个替身照常运行。
- 第 11.5 节：
  - 替身停掉以后，在没有 vLLM 的 `LD_LIBRARY_PATH` 的 shell 里直接导入，报 `ImportError: libascend_hal.so: cannot open shared object file`；按本文的命令导入，是 `<Mooncake 目录>/store.cpython-311-aarch64-linux-gnu.so <用时> s`（0.1 s）；
  - 重新起来的替身 `put_batch rc 0`；按端口数的那条命令是 `1 port=9100 new` 和 `1 port=9101 old`：重启过的 9100 用新库，没重启的 9101 还用原库，正是本容器还有实例没轮到时的预期状态；`undefined symbol` 那条 `grep` 没有输出。
- 第 11.6 节：`mooncake=` 一行是 `changed_since_install=1 timing=2 store=replacement`，`verdict … mooncake=ok`。
- 第 11.7 节：`[BatchPutTiming]` 1 行，`complete=false` 0 行。这一行四段和 `total_us` 都有，行末是 `timing_scope=normal_full status=ok complete=true failed_keys=0`。
- 第 11.9 节：送回的备份核对 `OK`，sha256 和第 11.3 节相同；恢复以后是 `c3bb55903917909b`（实验室的替身原库）、`15401808`；重启以后按端口数是 `1 port=9100 new`（这时用的是恢复的原库）、`1 port=9101 old`，检测脚本回到 `changed_since_install=0 timing=0`，重新起来的替身 put 不再写 `[BatchPutTiming]`，行数仍是 1。

**因为实验室环境，结果和现场不同的几处：**

| 节 | 实验室 | 现场应当是 |
|---|---|---|
| 11.1、11.3、11.9 | 没有监控机、那台机器和 `ssh`、`scp`：「监控机」「那台机器」是本机的两个目录，`scp`、`ssh … mkdir` 换成 `cp`、`mkdir`，监控机上的命令在 `--rm` 容器里跑；`docker cp` 照原文在本机执行 | 照原文 |
| 11.2 | 替身原库不是现场那一份，交付的核对脚本报 `STOP_changed`；往下走用的是测试版 | 交付的核对脚本报 `ok_replace` |
| 11.3、11.9 | 原库的 sha256 是 `c3bb55903917909b…`，大小 `15401808` | `3bf82f8b0c5ab715…`，`17610953` |
| 11.5 | 没有 vLLM、没有 NPU：重启是停起一个替身进程；`/health`、命令行和日志配置的核对（补丁包第 7.2 节）没有走；导入核对没有碰设备 | 照原文；导入时 Mooncake 的 Ascend 传输在卡上做什么没有验证 |
| 11.6 | 没有 vLLM 的源码、启动文件，检测脚本只带 `--out-dir`、`--max-seconds` 跑，只看 `mooncake=`、`verdict` 两行：`vllm=absent ascend=absent p2d=-`；替身原库不是现场那一份，换库前和回滚以后是 `store=other`、`mooncake=BAD(other)`；`loaded=-`（替身不被认作 vLLM） | 照原文：换库以后 `verdict vllm=ok ascend=ok(site_local=<n>) p2d=ok mooncake=ok`，回滚以后 `store=site_original`、`mooncake=not_replaced`；`loaded=` 是 Worker 映射的 Mooncake 库数 |
| 11.7 | put 走 TCP（`MC_FORCE_TCP=1`），只有替身那 1 次 put；看板、面板二的拉取没有走（拉取脚本对这类行的解析另在面板包里测过） | put 走 Ascend 传输；P 的 3 段有值 |
| 11.5、11.9 | 实验室的 master 跟替身一起重启：不重启时，master 还记着被停掉的替身的内存段，新替身的 put 会发到那个已经不在的地址上，报 `TRANSFER_FAIL`（原库、替换库都一样） | 照原文，只重启 vLLM |
