#!/bin/bash
# T56: the six e2e_t56 runs one after another (they share the t56e-* container names).
# Usage: run_e2e_all.sh <out dir> <rc1 log dir with P.log and D.log> <docs.json of e2e_v31 glm53> <19c segments.json>
set -u
O=$1; R=$2; REF=$3; SEG19=$4
cd "$(dirname "$0")/../.." || exit 1
mkdir -p "$O"
for run in glm53 sum sum-oldrec oldrec 19c shift; do
  case $run in
    glm53) env E2E_REF_DOCS=$REF timeout 900 python3 -I tests/t56/e2e_t56.py vllm-pd-grafana-panel2 "$O/w-$run" "$R/P.log" "$R/D.log" glm53 ;;
    sum) env E2E_SEGMENTS=tests/t56/profile-glm53-sum/segments.json E2E_REF_DOCS=$REF timeout 900 python3 -I tests/t56/e2e_t56.py vllm-pd-grafana-panel2 "$O/w-$run" "$R/P.log" "$R/D.log" glm53-sum ;;
    sum-oldrec) env E2E_SEGMENTS=tests/t56/profile-glm53-sum/segments.json E2E_T56_RECORDS=0 E2E_REF_DOCS=$REF timeout 900 python3 -I tests/t56/e2e_t56.py vllm-pd-grafana-panel2 "$O/w-$run" "$R/P.log" "$R/D.log" glm53-sum ;;
    oldrec) env E2E_T56_RECORDS=0 E2E_REF_DOCS=$REF timeout 900 python3 -I tests/t56/e2e_t56.py vllm-pd-grafana-panel2 "$O/w-$run" "$R/P.log" "$R/D.log" glm53 ;;
    19c) env E2E_SEGMENTS=$SEG19 E2E_REF_DOCS=$REF timeout 900 python3 -I tests/t56/e2e_t56.py vllm-pd-grafana-panel2 "$O/w-$run" "$R/P.log" "$R/D.log" glm53-19c ;;
    shift) env E2E_REF_DOCS=$REF timeout 900 python3 -I tests/t56/e2e_t56.py vllm-pd-grafana-panel2 "$O/w-$run" "$R/P.log" "$R/D.log" glm53 shift ;;
  esac > "$O/e2e-$run.txt" 2>&1
  echo "$run rc=$? $(tail -1 "$O/e2e-$run.txt")"
done
