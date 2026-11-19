# glm5.3 日志量测量

给操作说明 A「对节点的影响：日志量」用，结论和表在 `../notes/guideA_glm53_logvolume.zh-CN.md`。

## 文件

| 文件 | 作用 |
|---|---|
| `harness/make_harness.py` | 从 t51-glm53-ascend 的 `joint/t51_joint_check.py` 生成 `t51_volume.py`：联合检查的准备部分和第 1、2 部分原样保留，再接上 `scenario.txt`（一步排 n 个请求的 P、D 场景）和 `driver.txt`（真实调度器、字段宽度统一、按记录种类量字节）。联合检查的文件本身不改 |
| `harness/run_volume.sh` | 容器里的入口，同联合检查的 `run_joint_check.sh` |
| `harness/run_equiv_port.sh` | 在同一棵 vLLM 树上运行 t51-glm53-vllm 的 `verify/pd_equiv.py`（未改），得到 `api_summary`、`core_summary[request]` |
| `harness/norm.py` | 字段宽度统一为现场的位数（同 t51-glm53-p2d 的测量） |
| `harness/analyze.py` | 每种记录按 n=64、256 拟合「基数 + 每请求增量」，用 n=1 核对，写 `results/fit.json` |
| `harness/filter_sim.py` | 把记录喂给包二 v3.1-extra 的过滤器（glm53 的 `segments.json`），量写出的行，写 `results/filter_lines.txt` |
| `harness/compute_tables.py` | 用上面量出的字节数算表，写 `results/tables.md` |

## 运行

```bash
python3 -I harness/make_harness.py <t51-glm53-ascend>/joint/t51_joint_check.py harness/t51_volume.py harness/scenario.txt harness/driver.txt
docker run --rm --network none --entrypoint sh -e VOL_N=1,64,256 \
  -v <vLLM 树>:/vllm:ro -v <vllm-ascend 树>:/ascend:ro -v <vLLM 树>/vllm/v1/metrics/pd_diagnostics.py:/reference/pd_diagnostics.py:ro \
  -v <本目录>/harness:/joint:ro -v <opt-125m 的 HF 缓存>:/root/.cache/huggingface:ro -v <输出目录>:/out t51g-vllmtest:v1 /joint/run_volume.sh
```

- 再跑一次长请求：`-e VOL_N=1 -e VOL_DECODE=69 -e VOL_FILE=volume-long.json`。
- 在同一棵 vLLM 树上跑一次 `run_equiv_port.sh`。
- 最后依次运行 `analyze.py`、`filter_sim.py`、`compute_tables.py`，参数见各文件开头。

树的组成：
- vLLM：v0.30.0 + t51-glm53-vllm `ea04aa1` 的 `patches/vllm/files/`（SHA256SUMS.after 25/25）；
- vllm-ascend：`d4ad49796` + t51-glm53-ascend 的 `patches/vllm-ascend/files/`（11/11），再加 t51-glm53-p2d `a9e9695` 的 `mooncake_connector.py`（`f953b27c`）。

原始记录文件约 30 MB，不进 git，只留拟合和表。
