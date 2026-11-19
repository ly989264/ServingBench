# glm5.3 操作说明 A：已定、要写进去的事项

由统筹转来、已经定下的，写操作说明 A 时逐条落实。

1. **不设 `MC_LOG_DIR`。** Mooncake 78566d4 的传输引擎只有在设了 `MC_LOG_DIR` 时才初始化 glog，并把日志写进那个目录（`config.cpp:473–505`）；不设时写标准错误。现场 16 个 Worker 都没有设（小核对 f-1 的 `glog_env` 一行），`[BatchPutTiming]` 因而写进各实例的 vLLM 日志文件，`MOONCAKE_LOG_FILES` 填各实例的 vLLM 日志文件。打补丁和重启时都不要加 `MC_LOG_DIR`。
2. **替换 `libmooncake_store.so` 之前的只读核对。** 先跑 `mooncake/precheck/t51_mc_precheck.py`（`python3 -I -S -B`）：现场 `libtransfer_engine.so`、`libmooncake_common.so`、`libasio.so`、`ascend_transport.so` 和现场原有的 `libmooncake_store.so` 的导出指纹，必须和实验室同一次构建的对应库一致。最后一行是 `mc_pre result=ok_replace` 才替换；任何一行 `DIFFERS` 就不替换，把输出带回来。（11-19：核对件与现场原库导出名数相同、导出名哈希不同，按这条判据现场会 STOP；判据改法已报统筹，待定。替换的文件和 sha256 见 `mooncake/replace/README.zh-CN.md`。）
3. **对节点的影响：日志量。** 按 glm5.3 的形态（P 每实例 TP16、D 每实例 TP1）分 P、D，以每实例每秒请求数、D 批大小为参数列表；依据写明来自哪个测试或样本；按现场 `NODE_LOG_KEEP_HOURS` 缺省 12 小时换算节点磁盘要留多少。
4. **D 的启动文件** 是 `start_d0.sh`（和 `run_dp_template.sh` 同一目录）。说明里路径只写占位符。
