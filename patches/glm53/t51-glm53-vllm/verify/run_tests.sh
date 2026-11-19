#!/bin/bash
# usage: run2.sh <variant>   v030 | v030port | v032 | v032p
set -u
V=$1; OUT=/out/$V; mkdir -p $OUT
export PATH=/opt/venv/bin:$PATH
mkdir -p /w/$V && cd /w/$V
case $V in
  v030|v030port) tar -xf /src/$V.tar; VER=0.30.0 ;;
  v032|v032p) tar -xf /src/$V.tar; VER=0.32.0 ;;
esac
SETUPTOOLS_SCM_PRETEND_VERSION=$VER VLLM_TARGET_DEVICE=empty pip install --no-deps --no-build-isolation -e . > $OUT/install.txt 2>&1; echo "install rc=$?" >> $OUT/install.txt
# transformers stays at the image version for every tree
pip list 2>/dev/null | grep -E "^(vllm|transformers|tokenizers|torch|huggingface.hub|fastapi|pydantic) " > $OUT/pip.txt
case $V in
  v030|v032) T="tests/v1/core/test_scheduler.py tests/v1/engine/test_output_processor.py tests/v1/metrics/test_stats.py" ;;
  v030port) T="tests/v1/worker/test_pd_finish_claim.py tests/v1/metrics/test_pd_diagnostics.py tests/v1/metrics/test_pd_compact_core.py tests/v1/metrics/test_pd_compact_batch.py tests/tools/test_pd_compact.py tests/v1/engine/test_async_llm_output_handler.py tests/v1/core/test_scheduler.py tests/v1/engine/test_output_processor.py tests/v1/metrics/test_stats.py" ;;
  *) T="tests/v1/metrics/test_pd_diagnostics.py tests/v1/metrics/test_pd_compact_core.py tests/v1/metrics/test_pd_compact_batch.py tests/tools/test_pd_compact.py tests/v1/engine/test_async_llm_output_handler.py tests/v1/core/test_scheduler.py tests/v1/engine/test_output_processor.py tests/v1/metrics/test_stats.py" ;;
esac
for t in $T; do
  n=$(echo $t | tr / _)
  HF_HUB_OFFLINE=1 T51_SUBST_TOKENIZER=facebook/opt-125m VLLM_TARGET_DEVICE=cpu PYTHONPATH=/src/plug timeout 1500 python -m pytest -p t51_cpu_env -p no:cacheprovider -q -rA --timeout=300 --junitxml=$OUT/$n.xml $t > $OUT/$n.log 2>&1; echo "rc=$?" >> $OUT/$n.log
done
echo done > $OUT/DONE
