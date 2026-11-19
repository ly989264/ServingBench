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
