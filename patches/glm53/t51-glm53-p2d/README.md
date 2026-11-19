# t51-glm53-p2d

glm5.3 面板二：给 vllm-ascend `d4ad49796` 的 `MooncakeConnectorV1`（`kv_p2p/mooncake_connector.py`）加 D 读 KV 的诊断记录，格式与补丁 02 的 hybrid 版相同。只在本地提交，不推送。

| 目录 | 内容 |
|---|---|
| `patches/vllm-ascend-p2d/` | 交付物：补丁、替换件、重放脚本 `replay_p2d.py`、测试、校验和、说明（`README.zh-CN.md`） |
| `field_check/` | 现场核对：由 `replay_p2d.py` 导出的锚点清单 `anchors.json`、结构判据 `criteria.json`，以及现场操作说明 |
| `tools/` | 验证用脚本：`run_all.sh`（全部复现）、`run_v1_tests.sh`、`apply_check.sh` 与 `simulate_site_edits.py`（重放测试）、`test_replay_guards.py`（判据和备选锚点）、`regen_replay_texts.py`（从替换件生成重放脚本里的代码块）、`compare/`（和 hybrid 逐字段对比、字段子集检查、和原文件的行为对照、记录字节数）、`ut_harness/`（缺包时的桩插件） |
| `results/` | 验证输出，编号与 `run_all.sh` 的步骤对应 |
