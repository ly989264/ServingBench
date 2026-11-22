# 现场核对：在现场的 mooncake_connector.py 上重放 p2d 埋点

本目录给现场核对和操作说明 A 用。

**版本。** 现在是 T58 版：`replay_p2d.py` 的 sha256 为 `e62795bd63b146eb6813865d039d574d1fff067e1654f56f02a4458ca5e4a9e7`，29 处改动，32 条判据。
- T58 只改补丁自己插进去的代码（粒度，见 `patches/vllm-ascend-p2d/README.zh-CN.md` 第 3 节末尾），锚点、判据、改动编号都和 T56 版相同；重放后的检查多两条（第 3 节）。
- 升级 T56 版（或 T51 版）的现场文件，以及从原文件直接打，见 `操作说明A_T58_p2d补丁.zh-CN.md`。T56 版脚本（`9b12baa6`）放在 `patches/vllm-ascend-p2d/t56/`，只用于升级时的证明步骤。

T56 版相对 T51 版：
- 新增 C29：请求被标记完成的时刻，包络的终点。在文件中位于 C14 和 C15 之间，C15–C28 的编号和 T51 版相同。
- 新增 F32：见第 2 节。
- 第 6、7 节是 T51 版脚本（`02304c8f`）在现场跑出的结论，照原样保留。
- 当时的升级说明是 `操作说明A_T56_升级p2d补丁.zh-CN.md`（T51 → T56，保留作记录）；现场 8 行差异落在哪一段，见第 8 节。

下面两份 JSON 都从 `patches/vllm-ascend-p2d/replay_p2d.py` 导出，不是手抄：

| 文件 | 内容 |
|---|---|
| `anchors.json` | 29 处改动（C01–C29）的主锚点和备选锚点：所在函数、匹配方式、锚点文本、插入位置 |
| `criteria.json` | 重放前在现场文件上检查的 32 条结构判据（F01–F32，都是「停」），1 条只打印的说明项（N01），以及重放后的检查 |
| `span_diff.py` | 只读：现场在 `_transfer_kv_cache_all_groups` 里加、删的行落在哪一段（第 8 节） |
| `README.zh-CN.md` | 本说明 |

导出命令：`python3 -I replay_p2d.py --export-anchors anchors.json --export-criteria criteria.json`。两份 JSON 里都有 `script_sha256`，和现场用的 `replay_p2d.py` 对得上，才说明清单和脚本一致。

## 1. 在现场怎么跑

在容器里、打补丁那一步执行，只用标准库，Python 3.9 及以上（现场是 3.11.10），不改输入文件：

```
python3 -I replay_p2d.py <现场的 mooncake_connector.py> --check
python3 -I replay_p2d.py <现场的 mooncake_connector.py> --out /tmp/mooncake_connector.p2d.py
```

`--check` 做全部检查但不写文件。不加 `--check` 时，检查都通过才写到 `--out`（默认 `/tmp/mooncake_connector.p2d.py`），写出后用 `py_compile` 编译，最后报出新文件的 sha256。

输出每行以 `replay_p2d:` 开头：

| 行 | 含义 |
|---|---|
| `input=... sha256=... same_as_d4ad49796=True/False` | 输入文件的 sha256，以及它是不是公开的 `d4ad49796` 原文件 |
| `STOP Fxx <fact>: expected ..., found ...` | 一条「停」判据不满足，后面跟原因 |
| `note N01 functions whose line count differs ...` | 行数和原版不同的函数，格式为 `函数(原行数->现行数)`。只打印，不停；可以用来看现场改了哪些函数 |
| `facts ok (32 stop facts equal the original)` | 32 条判据都满足 |
| `Cxx primary` 或 `Cxx alternative_N`，后跟匹配方式、锚点和名字 | 每处改动用的是主锚点还是第几条备选 |
| `Cxx left_out ...` | 只会出现在 C22：现场代码里没有「描述符为空就返回」这一段时跳过 |
| `check ok: 29 of 29 changes located ...` | `--check` 全部通过 |
| `written out=... sha256=... changes=29/29 py_compile=ok same_as_replacement=...` | 写出成功；`same_as_replacement=True` 表示结果和 `d4ad49796` 的替换件逐字节相同（输入就是原文件时才会这样） |

退出码：

| 退出码 | 含义 | 怎么办 |
|---|---|---|
| 0 | 写出成功，或 `--check` 通过 | 用写出的文件替换现场文件（先备份），重启实例 |
| 1 | 参数错误，读不了输入，或输入不是合法的 Python | 核对路径 |
| 2 | 某处改动（输出里有编号 Cxx）的主锚点和备选锚点都没有恰好命中一行 | 不写文件。把输出和这一处的现场代码段带回来调整锚点 |
| 3 | 结构判据不满足（输出里有 `STOP Fxx`） | 不写文件。重放后的埋点可能记错或出错，把输出带回来 |
| 4 | 重放结果的检查没通过（见第 3 节） | 不写文件。把输出带回来 |
| 5 | 写出的文件 `py_compile` 不通过 | 不用这个文件，把输出带回来 |
| 6 | 输入已经打过补丁（已经导入 `pd_diagnostics`）；输出说明是 T51、T56 还是 T58 版 | 不要重复打。T51、T56 版：按操作说明 A（T58）先还原，用那一版的脚本证明，再在原文件上重放。T58 版：已经打过 |

## 2. 结构判据：哪些情况要停

现场改过的函数，我们看不到内容。所以重放前，脚本先在现场文件上用 Python 的 `ast` 统计插入的代码所依赖的结构：调用次数、所在循环、返回语句、变量名、属性名，再和 `d4ad49796` 原文件比。32 条判据都是「停」：任何一条不等于原版的值，就说明重放后的埋点可能记错数，或者在运行时出错，脚本什么都不写，退出码 3。

函数的行数（N01）只打印、不停：现场改过的函数行数不同是正常的，行数本身说明不了埋点对不对。真正要看的是下表这些结构有没有变。

| 编号 | 统计项 | 原版的值 | 为什么 |
|---|---|---|---|
| F01 | `file.read_calls` | `1` | 整个文件只有一处原生 READ（batch_transfer_sync_read）。多出来的 READ 不经过 _transfer_batch，不进 envelope |
| F02 | `transfer.read_calls` | `1` | _transfer_kv_cache_all_groups 里只有一处 READ；C23 只改写这一处 |
| F03 | `transfer.read_in_loop` | `false` | READ 不在循环里。在循环里时一个任务会有多次 READ，约定写的「V1 每个任务至多一次」不成立 |
| F04 | `transfer.read_single_line_assign` | `true` | READ 是一行内的 ret = ... 赋值；C23 用正则改写这一行 |
| F05 | `transfer.returns` | `2` | _transfer_kv_cache_all_groups 只有两处提前返回（全前缀命中、描述符为空），各有对应的标记。多出的返回路径会把没读的任务记成 succeeded |
| F06 | `transfer.has_req_start_time` | `true` | READ 之前有 req_start_time；_transfer_batch 用它算 transfer_scope_duration_ns |
| F07 | `transfer.first_param` | `"req_meta"` | 插入的代码用参数名 req_meta |
| F08 | `receiver.queue_put_calls` | `1` | 接收线程只有一处入队，在 add_request 里。别处入队的任务没有 _pd_diag，不写记录，envelope 会漏掉它们 |
| F09 | `receiver.queue_put_in` | `["KVCacheRecvingThread.add_request"]` | 同上 |
| F10 | `file.add_request_calls` | `1` | 只有一处调用 kv_recv_thread.add_request；别处调用不带 pd_execution_id、pd_offset |
| F11 | `file.add_request_in` | `["MooncakeConnectorWorker.start_load_kv"]` | 同上 |
| F12 | `start_load_kv.add_request_loop_depth` | `[3]` | 调用在 for req_id, meta → for shard_idx → for remote_tp_offset 三层循环里；offset 按后两层展平 |
| F13 | `start_load_kv.meta_loop` | `true` | 外层循环是 for req_id, meta in metadata.requests.items()；插入的代码用 meta |
| F14 | `file.add_new_req_calls` | `1` | 只有一处 add_new_req；别处加入的请求没有 consumer link（C2）和 pd_execution_id |
| F15 | `file.add_new_req_in` | `["MooncakeConnectorScheduler.build_connector_meta"]` | 同上 |
| F16 | `build_connector_meta.add_new_req_loop` | `["self._reqs_need_recv.items()"]` | add_new_req 在遍历 _reqs_need_recv 的循环里；consumer link 插在同一循环里 |
| F17 | `build_connector_meta.param` | `"scheduler_output"` | 插入的代码用参数名 scheduler_output |
| F18 | `handle_request.calls.self._transfer_kv_cache_all_groups` | `1` | _handle_request 里各调用的次数和原版相同，status 的对应关系才成立 |
| F19 | `handle_request.calls.self._mark_request_task_done` | `1` | 同上；tracker_completed 取它的返回值 |
| F20 | `handle_request.calls.self._reformat_pending_kv_caches` | `1` | 同上；reformat 计时包住这一处 |
| F21 | `handle_request.calls.self._send_done_recv_signal` | `1` | 同上；task 记录在完成信号之后写出 |
| F22 | `handle_request.calls.self._mark_failed_recv_request` | `3` | 同上；失败、失败后跳过两条路径 |
| F23 | `handle_request.calls.self.task_tracker.update_done_task_count` | `1` | 同上 |
| F24 | `handle_request.calls.self.request_queue.task_done` | `1` | 同上 |
| F25 | `handle_request.except_handlers` | `["Exception as e", "Exception as e"]` | 两处 except Exception as <名字>：传输失败、reformat 失败；插入的代码从这两行取异常变量名 |
| F26 | `handle_request.param` | `"req_meta"` | 插入的代码用参数名 req_meta |
| F27 | `receiver_init.missing_attrs` | `[]` | 插入的代码用到接收线程的这些属性：request_task_counts_lock、pending_reformat_lock、pending_reformat、local_engine_id、tp_rank、vllm_config、engine、request_queue |
| F28 | `scheduler_init.missing_attrs` | `[]` | 插入的代码用到调度端的 engine_id、vllm_config、_reqs_need_recv |
| F29 | `request_finished.delay_free_returns` | `1` | request_finished 只有一处返回 kv_transfer_params（return delay_free_blocks, ...），producer offer 插在它之前 |
| F30 | `request_finished.returns_at_body_level` | `true` | 这一处返回在函数体最外层，offer 不会落进某个分支 |
| F31 | `reqmeta.is_dataclass` | `true` | ReqMeta 仍是 dataclass；pd_execution_id 作为最后一个带默认值的字段加入 |
| F32（T56） | `handle_request.update_done_after_last_task` | `true` | `task_tracker.update_done_task_count` 是一行语句，在 `if <_mark_request_task_done 的结果>:` 的块里，即请求最后一个任务完成时才调用；C29 紧跟其后记下 `request_done_ns`，包络的终点就是这一刻 |

几条说明：
- F25 只要求两处都是 `except Exception as <名字>`，名字可以和原版不同：插入的代码从这一行读出现场用的变量名。
- READ 调用的参数可以改：C23 用正则把 `self.engine.batch_transfer_sync_read(<现场的参数>)` 改写成 `self._transfer_batch(req_meta, <现场的参数>, req_start_time)`，参数原样保留。前提是 F04：这次调用是写在一行内的 `ret = ...` 赋值。
- 「停」不等于现场改错了，只说明本补丁对那部分代码的假设不再成立。要看到现场那段代码，才能决定怎么改补丁。

## 3. 重放后的检查（不通过时退出码 4）

- the result parses (ast.parse) and compiles (py_compile)
- pd_diagnostics is imported once; the seven module-level helpers exist (T56 adds _pd_mark_request_done, T58 _pd_diag_level)
- (T58) KVCacheRecvingThread.__init__ sets self.pd_diag_level
- _transfer_kv_cache_all_groups has no direct batch_transfer_sync_read and exactly one _transfer_batch
- _handle_request_body, _pd_reformat_stage, _pd_finish_task, _transfer_batch exist in KVCacheRecvingThread
- (T56) in _handle_request_body, `_pd_mark_request_done(req_meta)` is the statement right after update_done_task_count
- the consumer link is created in the same loop as add_new_req
- the producer offer is a body-level statement of request_finished before the delay_free_blocks return
- pd_offset = 0 is right before the loops that call add_request, and pd_offset += 1 is in the block of that call
- no function uses a name that it did not use before and that is not defined

最后一条：同一个函数里，重放后用到的名字如果重放前没有用过、文件里也找不到定义，就判为失败。这种情况运行时会出 NameError，比如现场把插入代码要用的变量改了名。

## 4. 锚点清单

完整清单在 `anchors.json`。

**匹配方式。** `equal`：去掉首尾空白后整行相等；`prefix`：去掉首尾空白后以锚点开头；`regex`：在原始行（含缩进）上用 Python `re.search`；`scope_end`：没有锚点文本，插在函数或类的最后一个非空行之后。

**定位规则。**
- 改动按编号顺序依次做。
- 每处在所属函数的范围里查找，函数范围按「改到这一步为止」的文件算；先试主锚点，再按顺序试备选锚点。
- 一条锚点必须恰好命中一行才算命中，不取「第一处」。
- 有 `walk` 的，从命中的那一行再向前或向后，找到第一行符合 walk 条件的行，以那一行为准。

**两处依赖。**
- C08 把 `_handle_request` 的函数体移进新建的 `_handle_request_body`，所以 C09–C14 在 `_handle_request_body` 里查找。表里写的是这些行在没打补丁的文件中所在的函数，即 `_handle_request`。
- C14 的主锚点是 C13 写出的那一行。

所以单独拿 `anchors.json` 在没打补丁的文件上逐条 grep，C14 必然找不到。核对以 `replay_p2d.py --check` 的结果为准。

| 编号 | 改动 | 没打补丁时所在函数 | 必须 | 锚点（1 为主锚点，其余为备选，按顺序试） |
|---|---|---|---|---|
| C01 | import pd_diagnostics | `<module>` | 是 | 1. equal `from vllm.v1.request import RequestStatus`；2. prefix `from vllm.v1.kv_cache_interface import (` |
| C02 | module helpers | `<module>` | 是 | 1. equal `KV_CACHE_BUFFER_ALIGNMENT = 2 * 1024 * 1024`；2. prefix `MAX_REQUESTS_PER_PEER_HANDLER =`；3. prefix `class RemotePortInfo(` |
| C03 | ReqMeta.pd_execution_id | `ReqMeta` | 是 | 1. scope_end |
| C04 | receiver state | `KVCacheRecvingThread.__init__` | 是 | 1. equal `self.request_task_counts_lock = threading.Lock()`；2. prefix `super().__init__(` |
| C05 | add_request parameters | `KVCacheRecvingThread.add_request` | 是 | 1. equal `remote_block_ids_replicate_k: BlockIds | None = None,`；2. prefix `def add_request(` |
| C06 | add_request task identity | `KVCacheRecvingThread.add_request` | 是 | 1. equal `logger.debug("Adding request %s to the queue.Trans info:%s", request_id, trans_info)`；2. equal `self.request_queue.put(trans_info)` |
| C07 | _submit_request submitted_ns | `KVCacheRecvingThread._submit_request` | 是 | 1. equal `peer_key = (request_data["remote_host"], request_data["remote_handshake_port"])`；2. prefix `def _submit_request(` |
| C08 | _handle_request wrapper | `KVCacheRecvingThread` | 是 | 1. equal `def _handle_request(self, req_meta: dict[str, Any]):`；2. prefix `def _handle_request(self` |
| C09 | skip after failure | `KVCacheRecvingThread._handle_request` | 是 | 1. equal `logger.warning("Skipping KV cache transfer for request. remote_request_id=%s. ", remote_request_id)`；2. equal `if transfer_failed:` |
| C10 | transfer outcome | `KVCacheRecvingThread._handle_request` | 是 | 1. equal `self._transfer_kv_cache_all_groups(req_meta)` |
| C11 | transfer exception | `KVCacheRecvingThread._handle_request` | 是 | 1. equal `self._transfer_kv_cache_all_groups(req_meta)` |
| C12 | tracker_completed | `KVCacheRecvingThread._handle_request` | 是 | 1. equal `all_tasks_done = self._mark_request_task_done(request_id, all_task_done)`；2. prefix `all_tasks_done = self._mark_request_task_done(` |
| C13 | reformat stage | `KVCacheRecvingThread._handle_request` | 是 | 1. prefix `self._reformat_pending_kv_caches(` |
| C14 | reformat failure | `KVCacheRecvingThread._handle_request` | 是 | 1. regex `^\s*with self\._pd_reformat_stage\(req_meta, \w+\):\s*$` |
| C29（T56） | request marked done | `KVCacheRecvingThread._handle_request` | 是 | 1. equal `self.task_tracker.update_done_task_count(request_id)`；2. prefix `self.task_tracker.update_done_task_count(` |
| C15 | receiver methods | `KVCacheRecvingThread` | 是 | 1. prefix `def _send_done_signal_to_free_remote_port(`；2. prefix `def _transfer_kv_cache_all_groups(`；3. prefix `def _send_done_recv_signal(` |
| C16 | full prefix hit | `KVCacheRecvingThread._transfer_kv_cache_all_groups` | 是 | 1. equal `if num_local_blocks == 0 and not has_replicate_k_blocks:`；2. prefix `if num_local_blocks == 0` |
| C17 | meta_fetch begin | `KVCacheRecvingThread._transfer_kv_cache_all_groups` | 是 | 1. equal `# Check if we have the remote metadata cached.`；2. prefix `has_remote_metadata = (` |
| C18 | metadata_request | `KVCacheRecvingThread._transfer_kv_cache_all_groups` | 是 | 1. prefix `self._get_remote_metadata(` |
| C19 | meta_fetch end | `KVCacheRecvingThread._transfer_kv_cache_all_groups` | 是 | 1. equal `remote_layer_name_to_idx = build_layer_name_to_metadata_idx(remote_kv_group2layeridx)`；2. prefix `session_id = `；3. prefix `req_start_time = time.perf_counter()` |
| C20 | prepare begin | `KVCacheRecvingThread._transfer_kv_cache_all_groups` | 是 | 1. prefix `req_start_time = time.perf_counter()` |
| C21 | prepare end | `KVCacheRecvingThread._transfer_kv_cache_all_groups` | 是 | 1. equal `if not src_list:`；2. regex `self\.engine\.batch_transfer_sync_read\(` |
| C22 | no work | `KVCacheRecvingThread._transfer_kv_cache_all_groups` | 否（缺这段代码时跳过） | 1. equal `if not src_list:` |
| C23 | observed READ | `KVCacheRecvingThread._transfer_kv_cache_all_groups` | 是 | 1. regex `self\.engine\.batch_transfer_sync_read\(.*\)\s*$` |
| C24 | consumer link | `MooncakeConnectorScheduler.build_connector_meta` | 是 | 1. equal `# Clear the list once workers start the transfers`；2. equal `self._reqs_need_recv.clear()` |
| C25 | producer offer | `MooncakeConnectorScheduler.request_finished` | 是 | 1. equal `self._reqs_need_send[request.request_id] = time.time()`；2. prefix `return delay_free_blocks, ` |
| C26 | pull offset counter | `MooncakeConnectorWorker.start_load_kv` | 是 | 1. prefix `for shard_idx, remote_ports in enumerate(` |
| C27 | add_request kwargs | `MooncakeConnectorWorker.start_load_kv` | 是 | 1. equal `remote_block_ids_replicate_k=remote_block_ids_replicate_k_for_port,`；2. prefix `self.kv_recv_thread.add_request(` |
| C28 | pull offset increment | `MooncakeConnectorWorker.start_load_kv` | 是 | 1. prefix `self.kv_recv_thread.add_request(` |

## 5. 已经在本地测过的

- 在 `d4ad49796` 原文件上，32 条判据都等于原版的值；重放结果和替换件逐字节相同。
- 每条备选锚点都单独强制使用过一次，在原文件上都能定位，重放后的检查都通过；所有改动同时走最后一条备选锚点时也通过，生成的文件跑完整测试，232 个通过。
- 现场可能有、但不影响埋点的改动，脚本都照常写出：
  - 异常变量改名后，插入的代码用新名字；
  - READ 参数改动原样保留；
  - 锚点所在的方法加了装饰器时，新方法插在装饰器之上。
- 会让埋点出错的改动，都在写文件之前停下（退出码 3）：
  - 多一处 READ、READ 放进循环、多一处提前返回；
  - 多一处入队、多一处 `add_new_req`；
  - `add_request` 被移出循环；
  - `meta` 改名；
  - 对同一请求传输两次（重试）；
  - 裸 `except`；
  - 锁的属性改名；
  - 多一处返回 `kv_transfer_params`。
- 其他情况：锚点没了时退出码 2，并报出编号；重复打补丁时退出码 6，并说明是 T51、T56 还是 T58 版。
- T58 新增的：T56 版脚本在原文件上重放出的文件和 T56 替换件逐字节相同（升级时的证明步骤）；T56 版的文件，加不加 `--check` 都是退出码 6，并说明要先还原、用 `t56/replay_p2d.py` 证明。
- T56 新增的：
  - `update_done_task_count` 的参数写成关键字时，C29 走备选锚点，标记紧跟在调用之后；
  - 这一调用被移出 `if <done>:`，或拆成两行时，F32 停下；
  - T51 版脚本在原文件上重放出的文件和 T51 替换件逐字节相同，这是升级时的证明步骤。

以上在 Python 3.11 和 3.9 下各跑一遍（`tools/test_replay_guards.py`）。现场文件本身还没有拿到，没在它上面跑过。

## 6. 现场文件 glm53f-1 的核对结论

**小核对结果。**
- P、D 的 `mooncake_connector.py` 相同，sha256 前 16 位为 `2c4acb3eeb1ced7d`，公开仓库里找不到相同的版本。
- 两边都是 `p2d_replay script=02304c8f facts=ok(31) changes=28/28 alt=C25:2 left_out=none result=ok`。
- 有 9 个函数和原版不同：
  - 改动的 7 个：`_transfer_kv_cache_all_groups` 284→292、`add_request` 40→42、`add_new_req` 26→27、`MooncakeConnectorScheduler.__init__` 42→48、`request_finished` 63→73、`update_state_after_alloc` 28→39（循环 0→1）、`start_load_kv` 116→117；
  - 新增的 2 个：`get_kv_load_failure_reason`，两处。
- 现场另有本地补丁 `patch_kv_load_failure_reason.py`。

**C25 用备选 2，在预期之内。**
- 主锚点 `self._reqs_need_send[request.request_id] = time.time()` 在现场没有恰好命中一行：这一行要么被改了，要么出现了不止一次。
- 备选 2 把 producer offer 插在函数体最外层、唯一那处 `return delay_free_blocks, ...` 之前。原版里主锚点所在的 if 块后面紧接着就是这个 return，所以两者位置相同。
- F29、F30 和重放后的检查保证了这一点。`delayed_free` 取的是返回时的最终值。

**判据能保证的。**
- READ 和 envelope（F01–F07）；
- 读任务身份、execution_id、offset（F08–F13）；
- consumer link 和 `add_new_req` 在同一循环里（F14–F17）；
- `_handle_request` 的失败状态对应关系（F18–F26）。现场没有改 `_handle_request`。
- 插入代码用到的名字和属性都在（F27、F28 和重放后的名字检查）。

**判据覆盖不到的风险。** 按风险从高到低排；最后两列是原版的值和上线后在数据里的表现：

| 编号 | 现场改动 | 可能的影响 | 原版的值 | 数据里的表现 |
|---|---|---|---|---|
| R1 | `update_state_after_alloc` +11 行、多一个循环 | 同一请求再次进入 `_reqs_need_recv`：两条 consumer link、两条 `request_generation`，C2、C6 取哪一轮说不清。或者某些请求不进入：没有 link、task、`request_generation`，D wait_for_transfer 为空 | `params["do_remote_prefill"] = False` 1 处，写 `_reqs_need_recv` 1 处，循环 0 个 | 同一 `request_id` 的 consumer link 多于一条；有请求在 D 有 API 记录却没有 link |
| R2 | `_transfer_kv_cache_all_groups` +8 行 | READ 返回值小于 0 时不再抛异常、也没有新的 return：task 的 `status` 记成 `succeeded`，`transfer_status` 和 `native_calls[].status` 为 `failed`；envelope 仍然对 | `if ret < 0:` 下 `raise RuntimeError("Mooncake transfer failed ...")` 1 处 | task 记录 `status=succeeded` 且 `transfer_status=failed` |
| R3 | `start_load_kv` +1 行 | 多一处 `add_not_transfer_request`（不读就标为完成）：只有 link，D wait_for_transfer 为空 | `add_not_transfer_request` 调用 2 处 | 有 link 却没有 `request_generation` |
| R4 | `request_finished` +10 行 | 新增别的 return 也把 kv_transfer_params 交给 D（第一个返回值不是 `delay_free_blocks`）：这些 P 请求没有 producer offer。offer 不进任何段 | return 4 处，其中第二个返回值不为 `None` 的 1 处；写 `_reqs_need_send` 1 处 | D 上有 link 的 P 请求，在 P 上没有 producer link |
| R5 | `add_request` +2 行 | trans_info 的 `"request_id"` 不再取自参数：请求汇总取不到，不写（不报错） | trans_info 的 `request_id`、`all_task_done` 直接取自参数 | 有 task 却没有 `request_generation` |

新增的 `get_kv_load_failure_reason` 和 `patch_kv_load_failure_reason.py` 看起来是把失败原因报给客户端，不经过写记录的代码。本补丁只加属性和方法，不改、不删原有属性，业务状态不变，这一点有和原文件的行为对照测试作依据。现场这两段代码看不到，没法逐行确认。

R1–R5 的「原版的值」是在 `d4ad49796` 原文件上用 `ast` 算出的。现场的核对由 `extra_checks.py` 完成（X1–X6，第 7 节），已并进小核对 f-3。

**f-3b 的结果：X1–X6 与原版相同。** P、D 两边都是：
- `p2d_extra X1=1 X2=1/1 X3=2 X4=4/1 X5=1 X6=ok`，没有一项带 `!`；
- `p2d_replay script=02304c8f facts=ok(31) changes=28/28 alt=C25:2 result=ok`，和 f-1 相同。

所以 R1–R5 设想的五种改法，现场代码里都没有。在代码层能核对的范围内（F01–F31、重放后的检查、X1–X6），现场改动不影响 p2d 记录。上线后的第一批数据，只看 D 上 consumer link、task、request_generation 三种记录有没有出现。

统筹已定，本次不做 `首批数据核对.zh-CN.md` 里的四条核对，面板也不加这一节。那份文件留作备查：以后现场文件变了、`p2d_extra` 带 `!` 时再用。

## 7. 补充核对 `extra_checks.py`（X1–X6）

纯函数，只用标准库 `ast`，不读写文件，不导入业务包，嵌进小核对 f-3 使用：
- `extra_checks(source: str) -> dict`：返回 X1–X6 的值；
- `extra_line(source: str) -> str`：返回一行，和原版不同的项后面加 `!`；
- 原版的值是 `EXPECTED`，对应的一行是 `p2d_extra X1=1 X2=1/1 X3=2 X4=4/1 X5=1 X6=ok`。

各项的含义见文件开头的表。和原版不同时不停，只把这一行带回来。实验室用例在 `tools/test_extra_checks.py`，Python 3.11 和 3.9 下 13 个全部通过（`results/13_extra_checks.txt`）：原文件、替换件、模拟修改 C 都和原版相同；R1–R5 的模拟改法各自在对应的项上报出 `!`。

## 8. 现场 8 行差异落在哪一段（`span_diff.py`）

现场 `_transfer_kv_cache_all_groups` 比 `d4ad49796` 多 8 行（小核对 f-1：284→292），内容没有带回。
- 这几行落在「取对端信息」（meta_fetch）或「组读表」（prepare）里，那一段的时长就包含它们；
- 落在两段之外，就不影响这两段。

`span_diff.py` 在现场把这一点核出来。

- **怎么跑：** `python3 -I -S -B span_diff.py <现场的 mooncake_connector.py>`。
  - 只读，只用标准库，不导入 vLLM、vllm-ascend，不联网，不写文件；
  - 对原文件和打过 p2d（T51、T56 或 T58）的文件都能用。
- **做法：**
  - 把这个函数的非空行（去掉首尾空白）和 `d4ad49796` 原版逐行比较。原版每一行的 sha256 前 16 位嵌在脚本里。
  - 打过补丁的文件，先认出补丁插进去的行并去掉，再把改写过的 READ 读回原来的调用。
  - 然后把每一行加、删的行归到 5 个区间：`before`、`meta_fetch`、`between`、`prepare`、`after`。
- **区间的边界：**
  - 打过补丁的文件，取补丁插进去的 4 个计时语句本身（`spans=instrumented`）；
  - 没打补丁的文件，取 C17、C19、C20、C21 的主锚点（`spans=anchors`）。
- **输出：** 一行 `p2d_span_diff spans=... func_lines=... ref_lines=269 added=... removed=... before=+a/-r meta_fetch=+a/-r between=+a/-r prepare=+a/-r after=+a/-r added_at=<区间>:<sha16>,...`。
  - 不打印代码；加 `--show` 才打印这几行代码。
  - 退出码：0 成功，1 读不了输入，2 找不到函数或边界。
- **本地测过的**（`tools/test_span_diff.py`，Python 3.11.10 和 3.9 各一遍）：
  - 原文件、T51、T56、T58 替换件都是 0 处差异，打过补丁的文件找到的边界和锚点给出的相同；
  - 在 meta_fetch、prepare（8 行）、两段之前、两段之间、两段之后各加行，以及在 prepare 里删一行，都报在对应的区间；同一份改动在未打补丁的文件和它的重放结果（T58 版脚本）上，报出的完全相同；
  - 找不到函数时退出码 2。
- **用法：** 写进 glm53-test 的升级手册（T56 定稿第三节第 4 条），操作说明 A（T56）第 3 步。
