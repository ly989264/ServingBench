# Mooncake（glm5.3）：按检测结果决定怎么做

现场装的是哪个 wheel，以检测脚本 `mooncake=` 一行的 `state` 和 `nearest` 为准。环境清单里写的是 `mooncake-transfer-engine 0.3.13`，发行名不带 `-npu`。

| 检测结果 | 补丁 04 | 要做的事 |
|---|---|---|
| OBS `v0.3.14.rc1` 目录里的 wheel（cp312，aarch64；源码提交 78566d45） | 原样适用 | 直接用 dsv4.1 已经做好的替换库（`t51-dsv41-kit/patches/mooncake/lib/`，已去调试信息、已按这个 wheel 的文件名重链接，实验室做过加载和 put 测试），不用重编 |
| PyPI `mooncake-transfer-engine-npu` 0.3.13 或 0.3.13.post1 | 原样适用（`git apply --check` 通过） | 用对应 tag 重编，选项照该 tag 的 `.github/workflows/release-npu.yaml`：`USE_ASCEND_DIRECT=ON`、`USE_ETCD=ON`、`STORE_USE_ETCD=ON`、`BUILD_UNIT_TESTS=OFF`、`CMAKE_BUILD_TYPE=Release`；之后同 dsv4.1：先去调试信息再按 RECORD 重链接，做加载和 put 测试，逐项比对 |
| PyPI `mooncake-transfer-engine-npu` 0.3.11.post1（`d4ad49796` 的 Dockerfile 装的版本）、0.3.12、0.3.12.post1、0.3.14 | 不适用，源码部分打不上（`client_service.h`、`client_service.cpp`） | 先把补丁 04 移植到对应 tag，再按上一行重编 |
| PyPI 非 npu 的 `mooncake-transfer-engine` 0.3.13 | 原样适用 | 这个 wheel 按 `release-non-cuda.yaml` 构建，不带 Ascend 传输，在 A3 上不应出现；出现时先告诉统筹 |
| `unknown_build` 或别的版本 | — | 先告诉统筹 |

已核对的事实（2026-11-19）：

- OBS `v0.3.14.rc1` 目录只有 cp312 的 wheel：把 cp312 换成 cp39、cp310、cp311、cp313，HEAD 请求都返回 403。
- 补丁 04 对各 tag 的 `git apply --check`：v0.3.13（e5598b09）、v0.3.13.post1（71973589）、v0.3.14-rc1（78566d45）通过；v0.3.11.post1、v0.3.12、v0.3.12.post1 有 3 个源码块打不上，v0.3.14 有 2 个。
- glog：npu wheel 0.3.11.post1 到 0.3.14 的 cp312 aarch64 版都自带 `libglog-bb9621e6.so.0.4.0`，和 OBS wheel 是同一个文件名；PyPI 非 npu 的 0.3.13 自带 `libglog-b842b135.so.0.0.0`。
