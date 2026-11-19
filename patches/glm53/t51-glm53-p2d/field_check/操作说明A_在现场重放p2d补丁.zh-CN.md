# 操作说明 A：在现场重放 p2d 补丁（MooncakeConnectorV1 的 D 读 KV 埋点）

适用：glm5.3 的 P 和 D 容器。两边的 `mooncake_connector.py` 都是现场的本地修改版，小核对 glm53f-1 测得 sha256 前 16 位为 `2c4acb3eeb1ced7d`，P、D 相同。P、D 都要做这一步：D 写读 KV 的全部记录，P 只多写一条 producer link。

用到的文件只有一个：`replay_p2d.py`，sha256 必须为 `02304c8f76429780635870d17bac73558a2ecfafaadd744428c0ec93f457d4b3`，和小核对用的是同一版。

## 0. 前提

- vLLM 侧的诊断补丁（t51-glm53-vllm 的 v0.30.0 移植）已经装进同一个容器。`mooncake_connector.py` 打完补丁后会导入 `vllm.v1.metrics.pd_diagnostics`，没有它，实例启动时就会 ImportError。核对：
  ```
  python3 -c "import vllm.v1.metrics.pd_diagnostics as d; print(d.__file__)"
  ```
- 以下命令都在容器里执行；`python3` 是容器自带的（现场为 3.11.10）。

## 1. 找到文件、核对版本

```
F=$(python3 -c "import os, vllm_ascend; print(os.path.join(os.path.dirname(vllm_ascend.__file__), 'distributed/kv_transfer/kv_p2p/mooncake_connector.py'))")
sha256sum "$F"                 # 前 16 位应为 2c4acb3eeb1ced7d
sha256sum /path/to/replay_p2d.py   # 应为 02304c8f76429780635870d17bac73558a2ecfafaadd744428c0ec93f457d4b3
```

两项有任何一项不一致，就停下，把 sha256 带回来。现场文件变了，小核对的结论就不再适用，要重新核对。

**小核对 f-3 的两行。** f-3 已经在现场文件上跑过 `p2d_replay`（即 `replay_p2d.py --check`）和 `p2d_extra`（`field_check/extra_checks.py`，sha256 `2759c06f4bead2b072fecfe36b221bfa6a74b86f981982c7e0b1c1f7c864b95a`）：
- `p2d_replay` 应为 `result=ok`，`alt=C25:2`。
- `p2d_extra` 应为 `p2d_extra X1=1 X2=1/1 X3=2 X4=4/1 X5=1 X6=ok`，没有一项带 `!`。

f-3b 在 P、D 上测得的正是这两行：X1–X6 与原版相同，现场改动不影响 p2d 记录（`field_check/README.zh-CN.md` 第 6 节）。打补丁时如果这两行和 f-3b 不一样，说明现场文件变了，先停下，把输出带回来。

## 2. 只检查、不写文件

```
python3 -I /path/to/replay_p2d.py "$F" --check; echo "exit=$?"
```

预计输出（按小核对 glm53f-1 的结果）：

```
replay_p2d: input=<$F> sha256=2c4acb3eeb1ced7d… same_as_d4ad49796=False
replay_p2d: note N01 functions whose line count differs from d4ad49796: <n>: …
replay_p2d: facts ok (31 stop facts equal the original)
replay_p2d: C01 primary equal 'from vllm.v1.request import RequestStatus': import pd_diagnostics
replay_p2d: C02 primary equal 'KV_CACHE_BUFFER_ALIGNMENT = 2 * 1024 * 1024': module helpers
replay_p2d: C03 primary scope_end None: ReqMeta.pd_execution_id
replay_p2d: C04 primary equal 'self.request_task_counts_lock = threading.Lock()': receiver state
replay_p2d: C05 primary equal 'remote_block_ids_replicate_k: BlockIds | None = None,': add_request parameters
replay_p2d: C06 primary equal 'logger.debug("Adding request %s to the queue.Trans info:%s", request_id, trans_info)': add_request task identity
replay_p2d: C07 primary equal 'peer_key = (request_data["remote_host"], request_data["remote_handshake_port"])': _submit_request submitted_ns
replay_p2d: C08 primary equal 'def _handle_request(self, req_meta: dict[str, Any]):': _handle_request wrapper
replay_p2d: C09 primary equal 'logger.warning("Skipping KV cache transfer for request. remote_request_id=%s. ", remote_request_id)': skip after failure
replay_p2d: C10 primary equal 'self._transfer_kv_cache_all_groups(req_meta)': transfer outcome
replay_p2d: C11 primary equal 'self._transfer_kv_cache_all_groups(req_meta)': transfer exception
replay_p2d: C12 primary equal 'all_tasks_done = self._mark_request_task_done(request_id, all_task_done)': tracker_completed
replay_p2d: C13 primary prefix 'self._reformat_pending_kv_caches(': reformat stage
replay_p2d: C14 primary regex '^\\s*with self\\._pd_reformat_stage\\(req_meta, \\w+\\):\\s*$': reformat failure
replay_p2d: C15 primary prefix 'def _send_done_signal_to_free_remote_port(': receiver methods
replay_p2d: C16 primary equal 'if num_local_blocks == 0 and not has_replicate_k_blocks:': full prefix hit
replay_p2d: C17 primary equal '# Check if we have the remote metadata cached.': meta_fetch begin
replay_p2d: C18 primary prefix 'self._get_remote_metadata(': metadata_request
replay_p2d: C19 primary equal 'remote_layer_name_to_idx = build_layer_name_to_metadata_idx(remote_kv_group2layeridx)': meta_fetch end
replay_p2d: C20 primary prefix 'req_start_time = time.perf_counter()': prepare begin
replay_p2d: C21 primary equal 'if not src_list:': prepare end
replay_p2d: C22 primary equal 'if not src_list:': no work
replay_p2d: C23 primary regex 'self\\.engine\\.batch_transfer_sync_read\\(.*\\)\\s*$': observed READ
replay_p2d: C24 primary equal '# Clear the list once workers start the transfers': consumer link
replay_p2d: C25 alternative_2 prefix 'return delay_free_blocks, ': producer offer
replay_p2d: C26 primary prefix 'for shard_idx, remote_ports in enumerate(': pull offset counter
replay_p2d: C27 primary equal 'remote_block_ids_replicate_k=remote_block_ids_replicate_k_for_port,': add_request kwargs
replay_p2d: C28 primary prefix 'self.kv_recv_thread.add_request(': pull offset increment
replay_p2d: check ok: 28 of 28 changes located, result checks passed, nothing written
exit=0
```

- `N01` 一行列出行数和原版不同的函数和类，只是提示，不影响结果。里面应包括小核对报出的 9 个函数，其中新增的 2 个 `get_kv_load_failure_reason`，原行数显示为 `-`。各个类本身也会列出，所以 `<n>` 会大于 9。
- 28 行定位结果里，只有 C25 是 `alternative_2`，其余都是 `primary`。C25 用备选锚点在预期之内：producer offer 插在 `request_finished` 函数体最外层、唯一那处 `return delay_free_blocks, ...` 之前，语义和主锚点相同。
- 定位结果和上面不一样时（别的编号也用了备选锚点，或者 C25 又回到 `primary`），说明现场文件和小核对时不同。先停下，把输出带回来。

## 3. 写出打好补丁的文件

```
python3 -I /path/to/replay_p2d.py "$F" --out /tmp/mooncake_connector.p2d.py; echo "exit=$?"
```

预计输出：和第 2 步前面的行相同，最后一行换成

```
replay_p2d: written out=/tmp/mooncake_connector.p2d.py sha256=<S> changes=28/28 py_compile=ok same_as_replacement=False
exit=0
```

记下 `<S>`。P、D 的输入文件相同，`<S>` 也必须相同，不同就停下。`same_as_replacement=False` 是正常的：替换件对应的是公开的原文件，不是现场这份。

## 4. 替换、核对、重启

```
cp -p "$F" "$F.p2d_orig"                      # 备份现场原文件
cp /tmp/mooncake_connector.p2d.py "$F"
sha256sum "$F"                                # 应等于 <S>
python3 -c "import vllm_ascend.distributed.kv_transfer.kv_p2p.mooncake_connector as m; print(hasattr(m.KVCacheRecvingThread, '_transfer_batch'))"   # 应打印 True
```

然后按现场原来的方式重启 P、D 实例。

**回滚。**
```
cp -p "$F.p2d_orig" "$F"
```
然后重启。

## 5. 上线后看第一批记录

在 D 的日志里，找 `[PD_DIAG]` 开头、`"event":"pd_transfer_summary"` 的行。下面三种都出现，就说明 p2d 记录正常写出：

- consumer link：`"scope":"link"` 且 `"transfer_role":"consumer"`；
- task：`"scope":"task"`；
- request_generation：`"scope":"request_generation"`。

三种里少了哪一种，就把 D 的启动日志和最早的一批 `[PD_DIAG]` 行带回来。

## 6. 退出码

| 退出码 | 含义 | 怎么办 |
|---|---|---|
| 0 | `--check` 通过，或写出成功 | 继续下一步 |
| 1 | 参数错，读不了输入，或输入不是合法的 Python | 核对路径和文件 |
| 2 | 某处改动（输出里有编号 `Cxx`）的主锚点和备选锚点都没有恰好命中一行 | 不写文件。把完整输出带回来 |
| 3 | 结构判据不满足（输出里有 `STOP Fxx`），重放后的埋点可能记错数或出错 | 不写文件。把完整输出带回来 |
| 4 | 重放结果的检查没通过 | 不写文件。把完整输出带回来 |
| 5 | 写出的文件 `py_compile` 不通过 | 不要用 `/tmp` 下的文件，把输出带回来 |
| 6 | 输入已经打过补丁 | 不要重复打。先用备份还原，或核对是不是已经替换过 |

退出码不为 0 时，现场文件没有被动过：脚本从不修改输入，只写 `--out`。不要手工修改 `replay_p2d.py` 或现场文件去绕过检查。

**带回来的东西。** 第 2、3 步的完整输出，`sha256sum "$F"` 的结果，以及 P、D 各自的 `<S>`。
