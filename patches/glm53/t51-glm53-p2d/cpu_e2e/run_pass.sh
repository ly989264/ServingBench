#!/usr/bin/env bash
# Run every case before and after the patch in one-shot containers. RUNNER_POINTS=1 (default) puts the vllm-ascend
# runner's measurement points on the CPU runner in both trees (a no-op on the unpatched one). Usage:
#   WORK=<dir> IMAGE=<image> PY=<python in image> PYPATH_EXTRA=<extra PYTHONPATH> PASS=<name> [TREES=<dirs in $WORK/site>]
#   [PY311_ENV=<env of setup_pass2.sh, mounted read-only at /opt/py311>] bash run_pass.sh [case ...]
# Pass 2: PY311_ENV=$WORK/py311env PY=/opt/py311/bin/python3.11 PYPATH_EXTRA= PASS=py311
# Cases: sync (--no-async-scheduling), async (--async-scheduling), site (simulated site patches, async on, the length
# check raises ValueError), sitev (the same, the length check raises VLLMValidationError),
# pd (P --no-async-scheduling, D --async-scheduling, ExampleConnector, proxy). Results: $WORK/results/$PASS/<tree>/<case>/
set -uo pipefail
: "${WORK:?}" "${PASS:?}"
E2E="$(cd "$(dirname "$0")" && pwd)"
PANEL=${PANEL:?set PANEL to the panel-2 pack directory, panel2/vllm-pd-grafana-panel2 in this repository}
IMAGE=${IMAGE:-t51g-vllmtest:v1}
PY=${PY:-python}
cases=${*:-sync async site pd}
free() { df -k / | tail -1 | awk '{printf "%.2f", $4*1024/1e9}'; }
for tree in ${TREES:-vllm030cpu vllm030cpu_p}; do
  for c in $cases; do
    # start a case only with at least 30.5 GB free (the host must keep 30 GB); wait up to an hour for others to free space
    waited=0
    while f=$(free); awk -v f="$f" 'BEGIN { exit !(f < 30.5) }'; do
      [ $waited -ge 3600 ] && { echo "STOP: free $f GB after waiting ${waited}s"; exit 2; }
      [ $waited -eq 0 ] && echo "waiting: free $f GB"; sleep 30; waited=$((waited + 30))
    done
    reject=value
    case $c in
      sync)  extra="--no-async-scheduling"; site=0; mode=single ;;
      async) extra="--async-scheduling"; site=0; mode=single ;;
      site)  extra="--async-scheduling"; site=1; mode=single; reject=value ;;
      sitev) extra="--async-scheduling"; site=1; mode=single; reject=validation ;;
      pd)    extra=""; site=0; mode=pd ;;
    esac
    out=/w/results/$PASS/$tree
    mkdir -p "$WORK/results/$PASS/$tree"
    echo "== $PASS $tree $c (free $f GB)"
    docker run --rm --network none --shm-size 2g -v "$WORK:/w" -v "$E2E:/e2e:ro" -v "$PANEL:/panel:ro" \
      ${PY311_ENV:+-v "$PY311_ENV:/opt/py311:ro"} \
      -e VLLM_TREE="${PYPATH_EXTRA:+$PYPATH_EXTRA:}/w/site/$tree" -e MODEL=/w/model -e EXTRA_ARGS="$extra" \
      -e SITE_PATCHES=$site -e P2D_SITE_REJECT=$reject -e RUNNER_POINTS="${RUNNER_POINTS:-1}" -e PANEL=/panel -e OUT="$out" -e PY="$PY" "$IMAGE" timeout 1200 sh /e2e/run_case.sh "$c" "$mode"
  done
done
