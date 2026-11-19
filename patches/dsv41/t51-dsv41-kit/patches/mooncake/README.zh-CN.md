# Mooncake：补丁 04 和替换件（产物 4）

状态：方式 1 的库文件已在实验室编出（第 6 节）；交付方式最终以检测结果为准（第 5 节）。

## 1. 补丁

| 文件 | 内容 |
|---|---|
| `04-mooncake-0.3.13-native-timing.patch` | 同事的补丁 04，原样。SHA-256 `bd807d9b…86daa30c`，和补丁包 `SHA256SUMS` 一致 |
| `lib/mooncake/libmooncake_store.so.xz`、`lib/mooncake_transfer_engine.libs/libmooncake_store-2a608938.so.xz` | 交付的替换文件：打了补丁 04、去掉调试信息、按 OBS wheel 改好依赖名的两份库，xz 压缩（第 6 节） |
| `lib/SHA256SUMS.before` | OBS 原文件的 SHA-256（取自 wheel 的 RECORD，路径相对 `site-packages`）；替换前核对用，不一致就停下 |
| `lib/SHA256SUMS.after` | 替换后应有的 SHA-256（解压后的库） |
| `lib/SHA256SUMS.xz` | 两个 `.xz` 文件本身的 SHA-256 |

补丁 04 只改两个源文件：`mooncake-store/include/client_service.h`、`mooncake-store/src/client_service.cpp`，另改 README、`docs/`、`mooncake-store/tests/`。`git apply --check` 在 `v0.3.13`（`e5598b09`）和补丁基线 `78566d45`（标签 `v0.3.14-rc1`）上都通过。

## 2. 现场的 Mooncake 很可能就是补丁 04 的基线

- vllm-ascend `f9c8e0bdb` 的 Dockerfile（`Dockerfile`、`Dockerfile.a3` 等，第 107–142 行附近）不从 PyPI 装 Mooncake，而是从华为 OBS 的 `.../Mooncake/v0.3.14.rc1/` 目录下载 `mooncake_transfer_engine-0.3.13-cp312-cp312-manylinux_2_35_<arch>.whl`。目录名是 `v0.3.14.rc1`，版本串是 0.3.13，和补丁 04 的基线 `78566d45`（`v0.3.14-rc1`）对得上。
- 这个 wheel 和 PyPI 上的 0.3.13 不是同一种构建：PyPI 的是 `manylinux_2_28`、不含昇腾传输；OBS 的是 `manylinux_2_35`，带 `ascend_transport.so`。
- 现场是否就是这个 wheel，以检测脚本 `mooncake=` 行为准：`nearest=obs-v0.3.14rc1-cp312-aarch64`，且 `so_diff=0`。

OBS wheel 的已知构成，从 wheel 的 RECORD 和其中 `ascend_transport.so` 读出，没有在实际环境里核对：

| 项 | 值 |
|---|---|
| 编译器 | GCC 11.4.0（`Ubuntu 11.4.0-1ubuntu1~22.04.3`），即 Ubuntu 22.04 |
| 链接的 CANN 库 | `libascendcl.so`、`libmetadef.so`、`libllm_datadist.so` |
| 自带的第三方库 | glog（文件名后缀 0.4.0）、gflags 2.2.2、jsoncpp 1.9.5、yaml-cpp 0.7.0、zstd 1.4.8、liburing 2.1.0、xxhash 0.8.1 |
| client_service 所在的库 | `libmooncake_store`，wheel 里有两份：`mooncake/libmooncake_store.so` 和 auditwheel 改名后的 `mooncake_transfer_engine.libs/libmooncake_store-2a608938.so` |
| etcd | wheel 里没有 etcd 的封装库，推测构建时没开 `USE_ETCD`（Mooncake 官方 NPU 发布流程是开的） |

## 3. 要替换哪个文件

补丁 04 改的代码只编进 `libmooncake_store`。头文件的改动是新增一个私有成员函数、改两个成员函数的参数，不改数据成员、不改虚函数，所以 `store.so`、`engine.so` 等其他库不用重编。第 6 节的符号对比印证了这一点：其他库需要的符号新库里都有。

wheel 里有两份 `libmooncake_store`，按 `readelf` 读出（第 6 节）：

| 文件 | SONAME | RPATH | 谁用 |
|---|---|---|---|
| `mooncake/libmooncake_store.so` | `libmooncake_store.so` | `$ORIGIN/../mooncake_transfer_engine.libs` | Python 模块 `mooncake/store.so` 的 NEEDED 写的是 `libmooncake_store.so`，RPATH 先查 `$ORIGIN`，所以 Python 加载的是这一份 |
| `mooncake_transfer_engine.libs/libmooncake_store-2a608938.so` | `libmooncake_store-2a608938.so` | `$ORIGIN` | auditwheel 改名后的副本；`store.so` 不直接用它 |

两份都交付、都替换，Worker 实际加载的是哪一份以检测脚本的 `store_lib=` 为准。

## 4. 编译依赖（`v0.3.13` 和 `78566d45` 相同）

来源：`.github/workflows/release-npu.yaml`、`dependencies.sh`、`scripts/ascend/dependencies_ascend_installation.sh`、`mooncake-common/FindYLT.cmake`。两个版本的系统包清单、yalantinglibs 版本、Go 版本逐行相同。

| 类别 | 内容 |
|---|---|
| 系统 | Ubuntu 22.04（和 OBS wheel 一致，glibc 2.35），与现场相同的 CPU 架构（A3 为 aarch64） |
| 工具链 | `build-essential`（GCC 11）、`cmake`（≥3.16）、`ninja-build`、`git`、`pkg-config`、`patchelf`；C++20 |
| 系统开发包 | `libibverbs-dev`、`libgoogle-glog-dev`、`libjsoncpp-dev`、`libunwind-dev`、`libnuma-dev`、`libpython3-dev`、`libboost-all-dev`、`libssl-dev`、`libgrpc-dev`、`libgrpc++-dev`、`libprotobuf-dev`、`protobuf-compiler-grpc`、`libyaml-cpp-dev`、`libcurl4-openssl-dev`、`libhiredis-dev`、`liburing-dev`、`libjemalloc-dev`、`libmsgpack-dev`、`libzmq3-dev`、`libzstd-dev`、`libasio-dev`、`libxxhash-dev`；昇腾脚本另加 `mpich`、`libmpich-dev` |
| CANN | toolkit 的头文件和库（`acl`、`hccl`、`metadef`、`llm_datadist`），版本应和现场一致；构建前 `source /usr/local/Ascend/cann/set_env.sh`（或 `ascend-toolkit/set_env.sh`） |
| Python | 和现场一致的 Python 3.12 开发头文件；`extern/pybind11` 子模块 |
| 编译时联网 | `FindYLT.cmake` 在配置阶段从 GitHub 下载 yalantinglibs `7801bc9a…` 的源码包（固定 SHA-256）；`extern/pybind11` 子模块；开 `USE_ETCD` 时还要 Go 1.25.9；官方 NPU 流程另从 gitcode 编译 HIXL |
| CMake 选项（参考官方 NPU 发布流程） | `-DUSE_ASCEND_DIRECT=ON -DBUILD_UNIT_TESTS=OFF -DCMAKE_BUILD_TYPE=Release -DPython3_EXECUTABLE=<python3.12>`；是否加 `-DUSE_ETCD=ON -DSTORE_USE_ETCD=ON` 按第 2 节的推测不加 |

内网容器一般不能联网，所以在容器里编译（方式 2）要事先带进去：Mooncake 源码（已打补丁 04）、yalantinglibs 源码包、pybind11 源码，以及缺的系统开发包。

## 5. 两种交付方式，各自要什么

| | 方式 1：我们编好库文件，你只替换 | 方式 2：在你的容器里编译 |
|---|---|---|
| 条件 | 检测给出的架构、Python、glibc、CANN 版本能在公开镜像里找到对应的 | 检测的 `build_tools=` 有 gcc、g++、make、cmake，`headers_missing=` 里的包能装上，`cann_dev=` 有头文件 |
| 我们这边要做的 | 用对应镜像在实验室编译，交付库文件和 SHA-256 | 交付打好补丁的源码包、依赖源码包和编译脚本 |
| 需要的镜像 | vllm-ascend Dockerfile 的基础镜像 `quay.io/ascend/cann:9.2.1-a3-ubuntu22.04-py3.12`，arm64 压缩后约 4.8 GB，拉下来后 17.2 GB；拉镜像加编译实际多用了约 19 GB Mac 磁盘（第 6 节） | 无 |
| 风险 | 实验室的 CANN、依赖库版本和 OBS 构建不完全一样时，新库和现场其他库不兼容 | 内网容器缺包、不能联网 |

这个镜像已经按用户授权拉下来，并用它编出了方式 1 的库文件（第 6 节）。

## 6. 实验室编译结果（方式 1）

**环境。** `quay.io/ascend/cann:9.2.1-a3-ubuntu22.04-py3.12`（arm64；Ubuntu 22.04.5，glibc 2.35，GCC 11.4.0，Python 3.12.16，CANN 9.2.1）。编译容器用 `--rm`，脚本 `build/build_store_in_cann.sh`，改名脚本 `build/relink_like_obs_wheel.sh`。

**源码和依赖。** Mooncake `78566d45`（`git clone --depth 1 --branch v0.3.14-rc1`，`core.abbrev=8`）加补丁 04；`extern/pybind11` 取子模块固定的 `58c382a8`；yalantinglibs `7801bc9a`（SHA-256 `2a4b93c2…dfc1e`，和 `FindYLT.cmake` 固定的一致）；googletest 1.17.0（SHA-256 `65fab701…0b27c`，和 `FindGTest.cmake` 固定的一致）。这些源码包都事先在 Mac 上下好，编译时不联网（系统开发包除外，用 apt 从 Ubuntu 源装）。

**编译选项要和 OBS 一致。** 第一次按官方 NPU 发布流程用 `CMAKE_BUILD_TYPE=Release`，编出的库和 OBS 的导出符号差了 1801 / 855 个，几乎都是 yalantinglibs 序列化（`struct_pack`）的模板实例。原因是 `mooncake-common/common.cmake` 把 Release 的选项换成了只有 `-O3`，去掉了 `-DNDEBUG`；OBS 的库里没有断言字符串，是带 `NDEBUG` 编的。序列化代码负责客户端和 `mooncake_master` 之间的 RPC，必须一致，所以改用 Mooncake 自己的缺省 `RelWithDebInfo`（`-O2 -g -DNDEBUG`），并保持缺省的 `BUILD_UNIT_TESTS=ON`（OBS 的库里带着只在这个选项下才编进去的 NVMe KV 测试桩），只编 `mooncake_store` 这一个目标。

**产物（带调试信息、用于对比的版本）。** 编出的 `libmooncake_store.so`（442.5 MB）用 `patchelf` 改成和 OBS 两份副本相同的依赖名、RPATH、SONAME，得到两个文件（不进 git，放在 kit 的 `.work/mooncake-replace/`，校验和见 `build/REPLACEMENT_SHA256SUMS`）。`patchelf` 会把文件加大，改完以后分别是 471.5 MB 和 474.1 MB：

| 文件 | 大小 | SHA-256 |
|---|---|---|
| `mooncake/libmooncake_store.so` | 471.5 MB | `41ee4bbb…6cde510b` |
| `mooncake_transfer_engine.libs/libmooncake_store-2a608938.so` | 474.1 MB | `e8fa82c6…8fa27a70` |

交付用的是下面「去掉调试信息」一段里的版本。

**和 OBS 原文件的对比**（`build/lab_compare_with_obs.txt`、`build/lab_relink.txt`）：

| 项 | OBS 原文件 | 新文件 |
|---|---|---|
| 版本串 | `0.3.13 (git: 78566d45)` | 相同 |
| 编译器 | GCC 11.4.0（`Ubuntu 11.4.0-1ubuntu1~22.04.3`） | 相同 |
| `NDEBUG`（断言字符串数） | 0 | 0 |
| NEEDED（18 项，含顺序） | `libzstd-a1561916.so.1.4.8`、`libtransfer_engine-9fe35722.so`、`libxxhash-83d27ce3.so.0.8.1`、`libnuma.so.1`、`liburing-f0ec91e5.so.2.1.0`、`libascendcl.so`、`libmooncake_common-342bf32e.so`、`libyaml-cpp-1f8382c1.so.0.7.0`、`libasio-ade03112.so`、`libibverbs.so.1`、`libgflags-8661e189.so.2.2.2`、`libjsoncpp-b4b91bd8.so.1.9.5`、`ascend_transport.so`、`libglog-bb9621e6.so.0.4.0`、`libstdc++.so.6`、`libgcc_s.so.1`、`libc.so.6`、`ld-linux-aarch64.so.1` | 改名后逐项相同；改名前是 Ubuntu 22.04 的同版本库（glog 包为 `0.5.0+really0.4.0`，gflags 2.2.2，jsoncpp 1.9.5，yaml-cpp 0.7.0，liburing 2.1） |
| RPATH、SONAME | 见第 3 节 | 两份分别相同 |
| 导入的符号 | 724 个 | 724 个，没有新增 |
| 导出的符号 | 12719 个 | 12721 个。只在 OBS 里的 2 个、只在新文件里的 4 个，正好是补丁 04 的改动：`Client::CollectResults`、`Client::BatchWriteWhenPreferSameNode` 换了参数，新增 `Client::LogBatchWriteTiming`、`DurationUsBetween` |
| `store.so` 需要从这个库取的符号 | 46 个 | 46 个都在 |
| `[BatchPutTiming]` 字符串 | 0 | 2 |

**装进 OBS wheel 以后的加载和读写测试**（`build/load_test_with_obs_wheel.sh`、`build/put_test_with_obs_wheel.sh`，结果 `build/lab_load_test.txt`、`build/lab_put_test.txt`）。在同一个 CANN 镜像的 `--rm` 容器里装上 OBS 的 aarch64 wheel（整包按 range 分块下载，zip 校验和 RECORD 里 101 个文件的 SHA-256 全部对上），补装 vllm-ascend 镜像本来就有的 `libibverbs1`、`libnuma1`，先用原文件、再换上新文件各做一遍：

| 项 | 原文件 | 换上新文件 |
|---|---|---|
| `ldd`：`store.so` 解析到哪一份 | `mooncake/libmooncake_store.so` | 相同 |
| `ldd`：解析不到的库 | 两份副本单独看时 `ascend_transport.so` 解析不到（运行时经 `store.so` 的 RPATH 找到） | 相同 |
| wheel 里还有哪些库引用补丁 04 改了参数的两个函数 | 没有 | — |
| `import mooncake.store` | 成功 | 成功 |
| 起 wheel 自带的 `mooncake_master`，客户端走 TCP（`MC_FORCE_TCP=1`，否则要装昇腾传输、需要 NPU）：`setup` → `put_batch`（4 个 key，经 `Client::BatchPut`）→ `get` | 全部成功，读回内容一致 | 全部成功，读回内容一致；`mooncake_master` 仍是 wheel 原来的 |
| `[BatchPutTiming]` 行 | 0 | 1 行：`keys=4 bytes=16384 … timing_scope=normal_full status=ok complete=true failed_keys=0`，字段顺序和 `CONTRACT.zh-CN.md` 第 4 节一致 |
| glog 前缀里的线程号 | — | 等于调用 `put_batch` 的 Python 线程的 `threading.get_native_id()`（实测 540 和 540） |

这说明：新库能被 wheel 里其他文件正常加载，和未换的 `mooncake_master` 之间的 RPC 能用，补丁 04 的计时行按约定的格式写到标准错误，线程号可以和 `pool_task.native_calls[].native_tid` 直接对上。

**实验室磁盘用量。** 拉镜像和编译以后：Mac 剩余 64 GB（开始前 83 GB）；Docker 镜像合计 104.1 GB，其中这个 CANN 镜像 17.2 GB；Docker.raw 实际占用 125 GB（开始前 109 GB）；编译目录和下载件在 scratchpad 里约 6.1 GB。镜像按统筹的要求留着。

**没有验证的。** 在 NPU 上、走昇腾传输（`ascend` 协议、`prefer_alloc_in_same_node` 打开时的 `prefer_same_node_full` 路径）的读写；vllm-ascend 的 `batch_put_from_multi_buffers` 路径（实验室走的是 `put_batch`，两者都调 `Client::BatchPut`）。

## 7. 交付的版本：去掉调试信息（统筹 11-19 定）

**顺序：先 strip、再 patchelf。** 对已经用 `patchelf` 改过的文件再跑 `strip --strip-debug`，动态表对比全部相同，但库加载不了（`ELF load command address/offset not page-aligned`），是读写测试发现的。改为先对编译产物 `strip --strip-debug`（442.5 MB → 21.2 MB），再用 `build/relink_like_obs_wheel.sh` 改名。脚本 `build/strip_and_compare.sh`，结果 `build/lab_strip_compare.txt`。

**和带调试信息的版本逐项对比（两份分别比）：** 动态段（NEEDED、RPATH、SONAME，20 项）、导出的动态符号（12721 个，含类型）、导入的动态符号（724 个）、版本依赖（46 项）全部相同；没有剩下 `.debug_*` 节；各 LOAD 段的文件偏移和地址按对齐取模一致；`[BatchPutTiming]` 字符串仍在。

**大小和校验和：**

| 文件 | 去掉前 | 去掉后 | xz 压缩后 | SHA-256（去掉后） | SHA-256（.xz） |
|---|---|---|---|---|---|
| `mooncake/libmooncake_store.so` | 471.5 MB | 50.2 MB | 3.0 MB | `c3a916cd…2360d408` | `d3d50dda…fadb1544` |
| `mooncake_transfer_engine.libs/libmooncake_store-2a608938.so` | 474.1 MB | 52.8 MB | 3.0 MB | `073f2426…bcf0ba4e` | `f30266e0…a6acef31` |

完整的 SHA-256 见 `lib/SHA256SUMS.after`、`lib/SHA256SUMS.xz`。去掉后仍有 50 MB 左右，是 `patchelf` 为新的动态段加的对齐填充，填充是零，所以压缩后只有 3 MB。

**读写测试重跑（去掉调试信息的版本，`build/lab_load_test_stripped.txt`、`build/lab_put_test_stripped.txt`）：** 和第 6 节同样的做法。`ldd` 结果和原文件相同；`import mooncake.store` 成功；起 wheel 自带的 `mooncake_master`，`setup` → `put_batch` → `get` 全部成功，读回内容一致；写出 1 行 `[BatchPutTiming]`（`keys=4 bytes=16384 … status=ok`）；glog 线程号等于 `native_tid`（实测 541 和 541）。

**操作说明 A 第 5.1 步和第 9 步** 按这两个文件写：替换前用 `lib/SHA256SUMS.before` 核对现场文件就是 OBS 原文件，不一致就停下；两份各自备份；先解压成同目录下的临时文件再 `mv` 换名，不覆盖正在被映射的旧文件。实验室按原文走过一遍（装真实的 OBS wheel）：替换前检测为 `state=obs-v0.3.14rc1-cp312-aarch64`，替换后为 `state=modified_obs-v0.3.14rc1-cp312-aarch64 … timing=1`，回滚后回到原状态（`guides/lab/walk_A.txt`）。
