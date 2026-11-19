# 验证脚本

都在 `docker run --rm` 容器里运行。`$WORK` 是任意工作目录，里面放：

- `v030.tar`、`v030port.tar`、`v032.tar`、`v032p.tar`：对应源码树的 `git archive`（v0.30.0、v0.30.0 加本补丁、v0.32.0、v0.32.0 加 01）；
- `01-vllm-v0.32.0-diagnostics.patch`、`t51-vllm-v0.30.0-pd-diag.patch`；
- 本目录的脚本；`plug/t51_cpu_env.py`；`equiv/` 下放 `pd_equiv.py`、`compare.py`、`run_equiv.sh`。

镜像：`docker build -t t51g-vllmtest:v1 .`（`Dockerfile`）。HF 缓存预先下载 `facebook/opt-125m`、`llava-hf/llava-1.5-7b-hf`、`distilbert/distilgpt2` 的配置和分词器文件，挂到 `/root/.cache/huggingface`。

| 脚本 | 作用 | 结果文件 |
|---|---|---|
| `verify_apply.sh` | 校验表；现场补丁只含 `vllm/`、`git apply --check`；lab 补丁打在其上；两份补丁打完与测试所用的树一致；替换件一致 | `results/apply_check.txt` |
| `scan_site.py PATCH FILES_DIR` | 扫个人目录、运行时用 `--name` 给出的名字（例如同事的个人 Mooncake 构建目录，名字不存进仓库）、邮箱、工号样式的串、IP、主机名，列出 `github.com/<账号>` | `results/site_patch_scan.txt` |
| `check_runtime_refs.py PATCH FILES_DIR` | 运行时代码有没有 import 或按路径引用 `tools/`、`tests/`、`docs/` | `results/site_patch_scan.txt` |
| `run_tests.sh <树>` | 以 `VLLM_TARGET_DEVICE=empty` 安装后跑 01 的测试 | `results/unit_tests_summary.txt` |
| `junit_compare.py <结果根目录>` | 四棵树逐个用例对照 | `results/unit_tests_compare.txt` |
| `run_equiv.sh <树>` + `pd_equiv.py` | 同一组合成调用驱动一套安装，导出全部记录 | — |
| `compare.py A B --values [--unordered-links]` | 比较两份导出 | `results/records_compare.txt`、`records_compare_ordered.txt` |
| `check_steps.py DUMP` | 按步核对：步骤号由调度器设置且唯一、core 记录和 execute、sample 记录一一配对、async 下两步重叠、同步加载时 submit 的范围 | `results/check_steps.v032p.txt`、`check_steps.v030port.txt` |
| `run_upstream_sched.sh <树>` | 用同版本原版、没有改过的 `test_scheduler.py` 测补丁后的安装，核对调度行为是否改变 | `results/upstream_scheduler_tests.txt` |
| `compile_py311.sh` | 用 Python 3.11 解析 25 个运行时文件和 A2 测试文件 | `results/python311_syntax.txt` |
| `mtp_executions.py` | 逐请求比 executions 与 forward 步数 | `results/executions_vs_forward.txt` |
| `placement.py` | 01 与本补丁的增删行按所在函数比较 | `results/placement.txt` |
| `pyflakes_check.sh` | 25 个运行时文件的 pyflakes | `results/pyflakes.txt` |
| t51-glm53-ascend `tools/check_signatures.py`（`b3d1ce1`）`BASE PATCHED FILE...` | 按语法树比较 23 个改动文件里函数、方法、dataclass 的签名与 v0.30.0 是否兼容 | `results/signatures/check_signatures_report.txt` |
| `check_structs.py BASE PATCHED FILE...` | 同样按语法树比较，覆盖上一个工具不管的 msgspec 结构：原有字段顺序不变，新增字段在末尾并有默认值 | `results/signatures/check_structs_report.txt` |
| `callgraph_diff.py BASE PATCHED FILE...` | 调用图比对，两个方向：v0.30.0 里每个函数对每个被调对象的调用点，移植后（包括补丁新加的辅助函数）是否变少、是否新增；以及向原版函数多传的关键字参数 | `results/callgraph.txt`、`results/callgraph_new_calls_all.txt` |
| `api_audit.py V030 PATCHED V032 DIFF` | 01 新写的代码用到的导入、属性名、`getattr` 字面量是否在 v0.30.0 存在；定义和字段标注在 v0.30.0 与 v0.32.0 之间是否不同 | `results/api_audit.txt` |
| `startup_check.py SCENARIO`（`run_startup_check.sh`） | 三行 `PD_DIAG_STARTUP` 在真实的 EngineCore、Worker 路径上的合成调用 | `results/startup/` |
| `Dockerfile.site-py311` + `run_site_env.sh` | 贴近现场的镜像（Python 3.11.10、transformers 5.14.1、torch 2.10.0+cpu）和其中的全套测试 | `results/site_env/` |

例：

```bash
docker run --rm -v $WORK:/src:ro -v $WORK/out:/out -v $WORK/hf:/root/.cache/huggingface --entrypoint bash t51g-vllmtest:v1 /src/run_tests.sh v030port
```
