#!/bin/bash
export PATH=/opt/venv/bin:$PATH
pip install -q pyflakes >/dev/null 2>&1
FILES="vllm/entrypoints/openai/chat_completion/api_router.py vllm/entrypoints/openai/chat_completion/serving.py vllm/entrypoints/openai/completion/api_router.py vllm/entrypoints/openai/completion/serving.py vllm/entrypoints/serve/utils/pd_diagnostics.py vllm/inputs/engine.py vllm/inputs/llm.py vllm/renderers/base.py vllm/renderers/online_renderer.py vllm/v1/core/kv_cache_manager.py vllm/v1/core/sched/output.py vllm/v1/core/sched/scheduler.py vllm/v1/engine/__init__.py vllm/v1/engine/async_llm.py vllm/v1/engine/core.py vllm/v1/engine/core_client.py vllm/v1/engine/input_processor.py vllm/v1/engine/output_processor.py vllm/v1/metrics/pd_diagnostics.py vllm/v1/metrics/stats.py vllm/v1/request.py vllm/v1/worker/gpu/kv_connector.py vllm/v1/worker/gpu/model_runner.py vllm/v1/worker/kv_connector_model_runner_mixin.py vllm/v1/worker/worker_base.py"
for V in v030 v030port v032 v032p; do
  mkdir -p /w/$V && tar -xf /src/$V.tar -C /w/$V
  (cd /w/$V && python -m pyflakes $FILES 2>&1 | sed 's/:[0-9]*:[0-9]*:/:/' | sort > /out/flakes.$V.txt)
done
for p in "v030 v030port" "v032 v032p"; do set -- $p; echo "== new warnings $1 -> $2"; comm -13 /out/flakes.$1.txt /out/flakes.$2.txt; echo "== gone"; comm -23 /out/flakes.$1.txt /out/flakes.$2.txt; done
