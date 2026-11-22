#!/bin/bash
# T58: the e2e at four PD_DIAG_LEVEL combinations (P, D), one after another (they share the t58e-* container names).
# Usage: run_e2e_t58.sh <out dir> <rc1 log dir with P.log and D.log> <docs.json of e2e_v31 glm53>
set -u
O=$1; R=$2; REF=$3
cd "$(dirname "$0")/../.." || exit 1
mkdir -p "$O"
for lv in 33 22 11 23; do
  env E2E_LEVEL_P=${lv:0:1} E2E_LEVEL_D=${lv:1:1} E2E_REF_DOCS=$REF timeout 900 python3 -I tests/t58/e2e_t58.py vllm-pd-grafana-panel2 "$O/w-L$lv" "$R/P.log" "$R/D.log" glm53 > "$O/e2e-L$lv.txt" 2>&1
  echo "L$lv rc=$? $(tail -1 "$O/e2e-L$lv.txt")"
done
