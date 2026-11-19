#!/bin/sh
# Inside the test container. Starts vLLM (or P + D + proxy), runs client.py, stops everything, keeps the logs.
# $1 case name, $2 mode: single | pd. Env: VLLM_TREE (vLLM source tree with the CPU build), MODEL (model dir),
# EXTRA_ARGS (extra vllm serve arguments), SITE_PATCHES=1 to load the simulated site patches, RUNNER_POINTS=1 to put
# the vllm-ascend runner's measurement points on the CPU runner (see site_patches/sitecustomize.py),
# PANEL (vllm-pd-grafana-panel2 dir of v3.1-extra), OUT (output root).
set -u
case_name=$1; mode=$2; out=$OUT/$case_name; mkdir -p "$out"
export PYTHONPATH="/e2e/site_patches:$PANEL/node:$VLLM_TREE"
export P2D_SITE_PATCHES=${SITE_PATCHES:-0}
export P2D_RUNNER_POINTS=${RUNNER_POINTS:-0}
[ "$P2D_SITE_PATCHES" = 1 ] && export P2D_SITE_MAX_PROMPT=${P2D_SITE_MAX_PROMPT:-64}  # above every normal prompt (5 and 16 tokens)
export VLLM_CONFIGURE_LOGGING=1 VLLM_LOGGING_CONFIG_PATH=/e2e/logging_raw_and_filter.json
export PD_DIAG_MAPPING=$PANEL/profiles/glm53/segments.json
export VLLM_CPU_KVCACHE_SPACE=${VLLM_CPU_KVCACHE_SPACE:-1} VLLM_CPU_OMP_THREADS_BIND=${VLLM_CPU_OMP_THREADS_BIND:-auto}
export HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1
serve() {  # $1 port, $2 instance name, $3.. extra args
  port=$1; inst=$2; shift 2
  PD_DIAG_INSTANCE=$inst PD_DIAG_LOG_DIR=$out/filter ${PY:-python} -m vllm.entrypoints.cli.main serve "$MODEL" \
    --port "$port" --served-model-name tiny --dtype float32 --max-model-len 512 --max-num-seqs 8 \
    --enforce-eager ${EXTRA_ARGS:-} "$@" > "$out/$inst.log" 2>&1 &
  echo $!
}
wait_ready() {  # $1 port
  i=0; while [ $i -lt 600 ]; do
    ${PY:-python} - "$1" <<'PY' && return 0
import sys, urllib.request
try:
    urllib.request.urlopen(f"http://127.0.0.1:{sys.argv[1]}/health", timeout=2); sys.exit(0)
except Exception:
    sys.exit(1)
PY
    i=$((i + 1)); sleep 1; done; return 1
}
pids=""
if [ "$mode" = single ]; then
  pids=$(serve 8000 single)
  if wait_ready 8000; then ${PY:-python} /e2e/client.py http://127.0.0.1:8000 tiny "$out/client.json" > "$out/client.txt" 2>&1; else echo "server not ready" > "$out/client.txt"; fi
else
  kvp='{"kv_connector":"ExampleConnector","kv_role":"kv_producer","kv_connector_extra_config":{"shared_storage_path":"/tmp/kv"}}'
  kvd='{"kv_connector":"ExampleConnector","kv_role":"kv_consumer","kv_connector_extra_config":{"shared_storage_path":"/tmp/kv"}}'
  # as on glm5.3: P without async scheduling, D with it
  pids="$(serve 8100 P --no-async-scheduling --kv-transfer-config "$kvp") $(serve 8200 D --async-scheduling --kv-transfer-config "$kvd")"
  ${PY:-python} /e2e/proxy.py 8300 http://127.0.0.1:8100 http://127.0.0.1:8200 > "$out/proxy.log" 2>&1 & pids="$pids $!"
  if wait_ready 8100 && wait_ready 8200; then ${PY:-python} /e2e/client.py http://127.0.0.1:8300 tiny "$out/client.json" > "$out/client.txt" 2>&1; else echo "servers not ready" > "$out/client.txt"; fi
fi
sleep 8  # let the filter flush its per-request lines (flush_seconds 5)
for p in $pids; do kill -INT "$p" 2>/dev/null; done
sleep 5
for p in $pids; do kill -KILL "$p" 2>/dev/null; done
cat "$out/client.txt"
