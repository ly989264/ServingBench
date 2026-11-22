# 操作说明 A（T56）：把现场的 p2d 补丁升级到 T56 版

适用：glm5.3 的 **D** 容器。T56 只改 D 的接收线程（等 KV 的墙钟包络和三个最大值），P 上 p2d 写的 producer link 不变。按 T56 定稿，glm53-test 这次 P 不重启，P 上的文件保持 T51 版。

T51 版的操作说明 A（`操作说明A_在现场重放p2d补丁.zh-CN.md`）不改。这一份只写 T51 → T56 的升级，以及一次打 T51 加 T56 的做法（第 7 节）。

## 用到的文件

| 文件 | sha256 | 用途 |
|---|---|---|
| `replay_p2d.py`（T56 版） | `9b12baa6b38c8ff7d697c450ad80a9e0b48889f384dcdbddc33e04c2ee5e553e` | 重放 29 处改动（C01–C29） |
| `t51/replay_p2d.py`（T51 版，字节不变） | `02304c8f76429780635870d17bac73558a2ecfafaadd744428c0ec93f457d4b3` | 只用来证明备份确是现在这份文件的原文件 |
| `field_check/span_diff.py` | `40ebf068e0df5f08257681c8bfba65ac6910e6424d5711db5702478abdd33cbe` | 只读：现场那 8 行差异落在哪一段 |

## 为什么先还原、再完整重放

**现状。** T51 打完以后，现场文件已经导入了 `pd_diagnostics`。T56 版 `replay_p2d.py` 遇到这样的文件会退出（退出码 6），说明它是 T51 版。

**做法。** T56 不只是多一处修改，还改了 T51 已经插进去的补丁代码（`add_request` 的汇总块、`_pd_finish_task`）。所以做法是：
1. 回到 T51 时备份的现场原文件；
2. 先用 T51 的脚本证明这份备份正是现在这个文件的原文件；
3. 再用 T56 的脚本在备份上完整重放。

**好处。**
- 用的是这个现场已经核过的同一套重放（f-1、f-3b：28/28，只有 C25 用备选锚点）；
- 结果和一次打 T51 加 T56 逐字节相同；
- 备份不在或对不上，第 2 步就会停下，不会写出错误的文件。

## 0. 前提

- vLLM 侧的诊断补丁已经在同一个容器里（T51 时装的）。
- 以下命令都在 D 容器里执行，`python3` 是容器自带的（现场为 3.11.10）。
- `T56` 是放上述文件的目录。
- `ORIG` 是 T51 打补丁时备份的现场原文件：T51 版操作说明 A 第 4 步备份为 `"$F.p2d_orig"`。打补丁手册里的备份位置以手册为准。

```
F=$(python3 -c "import os, vllm_ascend; print(os.path.join(os.path.dirname(vllm_ascend.__file__), 'distributed/kv_transfer/kv_p2p/mooncake_connector.py'))")
ORIG="$F.p2d_orig"            # 按打补丁手册的备份位置改
sha256sum "$T56/replay_p2d.py" "$T56/t51/replay_p2d.py" "$T56/field_check/span_diff.py"   # 和上表一致
```

## 1. 核对现在的文件和备份

```
sha256sum "$F"       # 应为 T51 第 3 步记下的 <S>（T51 版写出的文件）
sha256sum "$ORIG"    # 前 16 位应为 2c4acb3eeb1ced7d（小核对 glm53f-1 测得的现场原文件）
grep -c 'kv_load_envelope_ns' "$F"   # 应为 0：还是 T51 版
```

任何一项不一致，就停下，把三行输出带回来。

## 2. 证明备份就是现在这份文件的原文件

```
python3 -I "$T56/t51/replay_p2d.py" "$ORIG" --out /tmp/p2d_t51_check.py; echo "exit=$?"
sha256sum /tmp/p2d_t51_check.py "$F"   # 两个 sha256 必须相同
```

- 应当看到：`exit=0`，最后一行 `changes=28/28 py_compile=ok`，两个 sha256 相同。
- 不相同，就说明 T51 以后现场文件又被改过，或者备份不是那一份。停下，把输出带回来，不要往下做。

## 3. 现场核对：那 8 行差异落在哪一段（只读）

```
python3 -I -S -B "$T56/field_check/span_diff.py" "$F"; echo "exit=$?"
```

- 只读文件，不导入 vLLM、vllm-ascend，不联网、不写任何东西，和小核对一样是零影响。
- 输出一行，形如：

  ```
  p2d_span_diff spans=instrumented func_lines=<n> ref_lines=269 added=<A> removed=<R> before=+a/-r meta_fetch=+a/-r between=+a/-r prepare=+a/-r after=+a/-r added_at=<段>:<sha16>,…
  ```

  `func_lines` 是这个函数在文件里的行数，含空行。小核对 f-1 测得现场原文件是 292 行，T51 在这个函数里插了 9 行，所以对 T51 版文件应为 301；对 `"$ORIG"` 应为 292。

  各字段的意思：
  - `spans=instrumented`：取对端信息、组读表两段的边界，取的是现场文件里 T51 插进去的计时语句本身；
  - `added`、`removed`：现场在 `_transfer_kv_cache_all_groups` 里相对 `d4ad49796` 加了、删了几行（不算空行）；
  - `meta_fetch=+a/-r`、`prepare=+a/-r`：落在「取对端信息」「组读表」两段里的各有几行；`before`、`between`、`after` 是两段之外的；
  - `added_at`：每一行所在的段和这一行的 sha256 前 16 位，不打印代码本身。
- 把这一行原样带回来，`exit` 应为 0。
- 加 `--show` 会把这几行代码也打出来。要不要带回代码，由用户定。
- 读的是第 1 步核过的 T51 版文件，也可以对 `"$ORIG"` 读：那时是 `spans=anchors`，其余各项应当相同。

## 4. 只检查、不写文件

```
python3 -I "$T56/replay_p2d.py" "$ORIG" --check; echo "exit=$?"
```

预计输出（照 T51 时的结果，多了 C29 和 F32）：
- `facts ok (32 stop facts equal the original)`；
- 29 行定位结果：C25 是 `alternative_2`，其余都是 `primary`。新增的一行是：

  ```
  replay_p2d: C29 primary equal 'self.task_tracker.update_done_task_count(request_id)': request marked done
  ```

  它排在 C14 和 C15 之间：重放按文件中的顺序做，C15–C28 的编号仍和 T51 版相同。
- 最后一行 `check ok: 29 of 29 changes located, result checks passed, nothing written`，`exit=0`。

和这不同（别的编号用了备选锚点，或出现 `STOP`），就停下，把输出带回来。

## 5. 写出 T56 版文件

```
python3 -I "$T56/replay_p2d.py" "$ORIG" --out /tmp/mooncake_connector.p2d56.py; echo "exit=$?"
```

- 应当看到：最后一行 `written out=/tmp/mooncake_connector.p2d56.py sha256=<S56> changes=29/29 py_compile=ok same_as_replacement=False`，`exit=0`。
- 记下 `<S56>`。各 D 容器的原文件相同，`<S56>` 也必须相同，不同就停下。

## 6. 替换、核对、重启

```
cp -p "$F" "$F.p2d_t51"                       # 留下 T51 版，用来回滚
cp /tmp/mooncake_connector.p2d56.py "$F"
sha256sum "$F"                                # 应等于 <S56>
python3 -c "import vllm_ascend.distributed.kv_transfer.kv_p2p.mooncake_connector as m; print(hasattr(m, '_pd_mark_request_done'))"   # 应打印 True
```

然后按打补丁手册分批重启 D 实例。

**回滚。**
- 回到 T51：`cp -p "$F.p2d_t51" "$F"`，再重启；
- 回到原文件：`cp -p "$ORIG" "$F"`，再重启。

**上线后看第一批记录。** D 日志里 `"scope":"request_generation"` 的记录多了下面几个字段：
- `kv_load_envelope_ns`；
- `kv_load_timing_scope`，值为 `"first_task_queued_to_request_marked_done"`；
- `task_max_queue_wait_ns`、`task_max_meta_fetch_ns`、`task_max_prepare_ns`。

`"scope":"task"` 的记录多了 `request_done_ns`：每个请求只有一条有值，其余为 `null`。

可以核的两条关系：
- `kv_load_envelope_ns` ≥ `transfer_envelope_ns`；
- `kv_load_envelope_ns` 不小于三个最大值中的任何一个。

## 7. 一次打 T51 加 T56（以后的 glm53-1）

文件还没打过 p2d 时，第 1、2 步换成：
- `sha256sum "$F"`，前 16 位应为 `2c4acb3eeb1ced7d`；
- 备份：`cp -p "$F" "$F.p2d_orig"`；
- 然后 `ORIG="$F.p2d_orig"`，做第 3–6 步。第 3 步那时是 `spans=anchors`。

## 8. 退出码

| 退出码 | 含义 | 怎么办 |
|---|---|---|
| 0 | `--check` 通过，或写出成功 | 继续下一步 |
| 1 | 参数错，读不了输入，或输入不是合法的 Python | 核对路径和文件 |
| 2 | 某处改动（输出里有编号 `Cxx`）的锚点没有恰好命中一行 | 不写文件，把完整输出带回来 |
| 3 | 结构判据不满足（输出里有 `STOP Fxx`） | 不写文件，把完整输出带回来 |
| 4 | 重放结果的检查没通过 | 不写文件，把完整输出带回来 |
| 5 | 写出的文件 `py_compile` 不通过 | 不要用 `/tmp` 下的文件，把输出带回来 |
| 6 | 输入已经打过 p2d。输出会说是 T51 版还是 T56 版 | T51 版：输入给错了，应当给 `"$ORIG"`，见第 2 步。T56 版：已经升级过，不要重复打 |

`span_diff.py` 的退出码：0 打印成功；1 读不了输入；2 找不到这个函数或某个阶段的边界，输出 `p2d_span_diff missing: …`。

退出码不为 0 时，现场文件没有被动过：两个脚本都从不修改输入。

**带回来的东西：**
- 第 1 步的三行输出；
- 第 2 步的两个 sha256；
- 第 3 步那一行；
- 第 4、5 步的完整输出；
- 各 D 的 `<S56>`。
