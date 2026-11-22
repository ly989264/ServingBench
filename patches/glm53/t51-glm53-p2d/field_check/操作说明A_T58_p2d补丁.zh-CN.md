# 操作说明 A（T58）：把现场的 p2d 补丁换成 T58 版

T58 版在 T56 版上只加了一样东西：**粒度**。D 的接收线程启动时调用一次 vLLM 补丁里的 `pd_diagnostics.diag_level()`，记下这个进程的级别：

| 级别 | p2d 写什么 |
|---|---|
| 不设、`3`、`2` | 和 T56 逐字段相同 |
| `1` | `request_generation` 不写这 4 个字段：`task_max_queue_wait_ns`、`task_max_meta_fetch_ns`、`task_max_prepare_ns`、`transfer_envelope_ns`；接收线程也不再把它们并进请求汇总 |

各级都照写的：
- `kv_load_envelope_ns`、`kv_load_timing_scope`；
- `task` 记录和 `link` 记录，一个字段不变。`task` 记录里每个任务自己的排队、取对端信息、组读表和传输的计时都在，候选映射要用。

取级别的规则：
- vLLM 补丁里没有 `diag_level()`（例如还是 T56 的 vLLM 补丁）、返回的不是 1、2、3、或者出错，都按 3。
- 改级别要重启进程。

**两条路径，按现场选一条：**
- **A. glm53-test：** 现场现在是 T56 版，从 T56 升到 T58，见第 2–6 步。
- **B. glm53-1：** 现场还是原文件，直接打 T58，见第 7 节。

两条路径写出的文件逐字节相同。模拟的现场修改 A、B、C 三组上都测过（`results/16_upgrade_paths.txt`）。

## 用到的文件

| 文件 | sha256 | 用途 |
|---|---|---|
| `replay_p2d.py`（T58 版） | `e62795bd63b146eb6813865d039d574d1fff067e1654f56f02a4458ca5e4a9e7` | 重放 29 处改动（C01–C29），锚点和 T56 版相同 |
| `t56/replay_p2d.py`（T56 版，字节不变） | `9b12baa6b38c8ff7d697c450ad80a9e0b48889f384dcdbddc33e04c2ee5e553e` | 只用来证明备份确是现在这份文件的原文件 |
| `t51/replay_p2d.py`（T51 版，字节不变） | `02304c8f76429780635870d17bac73558a2ecfafaadd744428c0ec93f457d4b3` | 现场文件是 T51 版时，第 2 步换用它 |

## 为什么先还原、再完整重放

T58 改的是补丁已经插进去的代码（接收线程的初始化、`_pd_finish_task`、`_transfer_batch`），不是在 T56 版上再加一处。所以做法和 T56 升级时一样：
1. 回到 T51 时备份的现场原文件；
2. 用现场现在那一版的脚本，证明这份备份正是现在这个文件的原文件；
3. 用 T58 的脚本在备份上完整重放。

T58 版脚本遇到已经打过 p2d 的文件会退出，退出码 6，并说明文件是哪一版。

## 1. 前提

- 在 D 容器里执行，`python3` 是容器自带的（现场为 3.11.10）。
- `T58` 是放上述文件的目录。
- `ORIG` 是 T51 打补丁时备份的现场原文件，T51 版操作说明 A 第 4 步备份为 `"$F.p2d_orig"`。T56 升级时没有改它。

```
F=$(python3 -c "import os, vllm_ascend; print(os.path.join(os.path.dirname(vllm_ascend.__file__), 'distributed/kv_transfer/kv_p2p/mooncake_connector.py'))")
ORIG="$F.p2d_orig"            # 按打补丁手册的备份位置改
sha256sum "$T58/replay_p2d.py" "$T58/t56/replay_p2d.py"   # 和上表一致
```

## 2. 核对现在的文件和备份（路径 A）

```
sha256sum "$F"                       # 应为 T56 升级时记下的 <S56>
sha256sum "$ORIG"                    # 前 16 位应为 2c4acb3eeb1ced7d（现场原文件）
grep -c 'kv_load_envelope_ns' "$F"   # 不为 0：是 T56 版
grep -c '_pd_diag_level' "$F"        # 应为 0：还不是 T58 版
```

任何一项不一致，就停下，把四行输出带回来。

## 3. 证明备份就是现在这份文件的原文件

```
python3 -I "$T58/t56/replay_p2d.py" "$ORIG" --out /tmp/p2d_t56_check.py; echo "exit=$?"
sha256sum /tmp/p2d_t56_check.py "$F"   # 两个 sha256 必须相同
```

- 应当看到：`exit=0`，最后一行 `changes=29/29 py_compile=ok`，两个 sha256 相同。
- 不相同，说明 T56 以后现场文件又被改过，或者备份不是那一份。停下，把输出带回来。
- 现场文件若是 T51 版（第 2 步 `kv_load_envelope_ns` 为 0），这一步换成 `t51/replay_p2d.py`，最后一行是 `changes=28/28`。

## 4. 只检查、不写文件

```
python3 -I "$T58/replay_p2d.py" "$ORIG" --check; echo "exit=$?"
```

预计输出和 T56 升级时的第 4 步相同：
- `facts ok (32 stop facts equal the original)`；
- 29 行定位结果：C25 是 `alternative_2`，其余都是 `primary`；
- 最后一行 `check ok: 29 of 29 changes located, result checks passed, nothing written`，`exit=0`。

和这不同（别的编号用了备选锚点，或出现 `STOP`），就停下，把输出带回来。

## 5. 写出 T58 版文件

```
python3 -I "$T58/replay_p2d.py" "$ORIG" --out /tmp/mooncake_connector.p2d58.py; echo "exit=$?"
```

- 应当看到：最后一行 `written out=/tmp/mooncake_connector.p2d58.py sha256=<S58> changes=29/29 py_compile=ok same_as_replacement=False`，`exit=0`。
- 记下 `<S58>`。各 D 容器的原文件相同，`<S58>` 也必须相同，不同就停下。

## 6. 替换、核对

```
cp -p "$F" "$F.p2d_t56"                       # 留下 T56 版，用来回滚
cp /tmp/mooncake_connector.p2d58.py "$F"
sha256sum "$F"                                # 应等于 <S58>
python3 -c "import vllm_ascend.distributed.kv_transfer.kv_p2p.mooncake_connector as m; print(hasattr(m, '_pd_diag_level'))"   # 应打印 True
```

然后按升级手册的顺序停机、拉起（T58 要整集群一起重启）。

**回滚。**
- 回到 T56：`cp -p "$F.p2d_t56" "$F"`，再重启；
- 回到原文件：`cp -p "$ORIG" "$F"`，再重启。

**上线后看第一批记录。**
- 不设 `PD_DIAG_LEVEL`（默认 3）或设为 2：D 日志里 p2d 的记录和 T56 逐字段相同，判法照 T56。
- 设为 1：`"scope":"request_generation"` 的记录里没有上面那 4 个字段，`kv_load_envelope_ns` 照常有值；`"scope":"task"` 的记录不变。
- 进程实际用的级别看 vLLM 补丁写的启动行 `PD_DIAG_STARTUP diag level=<n> source=<...>`，p2d 自己不另写启动行。

## 7. 路径 B：从原文件直接打 T58（glm53-1）

文件还没打过 p2d 时，第 2、3 步换成：
- `sha256sum "$F"`，前 16 位应为 `2c4acb3eeb1ced7d`；
- 备份：`cp -p "$F" "$F.p2d_orig"`；
- `ORIG="$F.p2d_orig"`。

然后做第 4–6 步。第 6 步的回滚只有「回到原文件」一条，不用 `"$F.p2d_t56"`。

## 8. 退出码

| 退出码 | 含义 | 怎么办 |
|---|---|---|
| 0 | `--check` 通过，或写出成功 | 继续下一步 |
| 1 | 参数错，读不了输入，或输入不是合法的 Python | 核对路径和文件 |
| 2 | 某处改动（输出里有编号 `Cxx`）的锚点没有恰好命中一行 | 不写文件，把完整输出带回来 |
| 3 | 结构判据不满足（输出里有 `STOP Fxx`） | 不写文件，把完整输出带回来 |
| 4 | 重放结果的检查没通过 | 不写文件，把完整输出带回来 |
| 5 | 写出的文件 `py_compile` 不通过 | 不要用 `/tmp` 下的文件，把输出带回来 |
| 6 | 输入已经打过 p2d，输出说明是哪一版 | T51 版、T56 版：输入给错了，应当给 `"$ORIG"`，并用那一版的脚本做第 3 步的证明。T58 版：已经升级过，不要重复打 |

退出码不为 0 时，现场文件没有被动过：脚本从不修改输入。

**带回来的东西：**
- 路径 A：第 2 步的四行输出、第 3 步的两个 sha256；
- 两条路径：第 4、5 步的完整输出，各 D 的 `<S58>`。
