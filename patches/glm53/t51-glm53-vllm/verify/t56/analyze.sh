#!/bin/bash
# usage: analyze.sh RUN_DIR   (output of run.sh); T56 checks summary
R=$1; H=$(dirname $0)
echo "#### environment"; cat $R/env.txt
echo "#### installs"; grep -h "rc=" $R/t51.install.txt $R/t56.install.txt
echo "#### driver exit codes (rc != 0 listed)"; grep -l "rc=[1-9]" $R/*.log $R/*.txt 2>/dev/null | grep -v "tests_\|\.xml" | sed 's#.*/##'
echo "#### driver errors in dumps"; grep -l driver_error $R/*.jsonl 2>/dev/null | sed 's#.*/##'; echo "(none listed = none)"
echo "#### 1. pd_equiv, 15 scenarios: T51 vs T56"; python3 $H/equiv/compare.py $R/t51.equiv.jsonl $R/t56.equiv.jsonl --values | tail -3
for V in vllm balanceP original_vllm ported_vllm original_balance ported_balance; do
  echo "#### 2. scheduler variant $V: T51 vs T56"; python3 $H/equiv/compare.py $R/t51.sched.$V.jsonl $R/t56.sched.$V.jsonl --values | tail -1
done
for B in vllm balance; do
  echo "#### 3. D driver, base $B: T51 vs T56 without kv_ready_wait"
  python3 $H/strip.py $R/t56.d.$B.jsonl $R/t56.d.$B.stripped.jsonl
  python3 $H/equiv/compare.py $R/t51.d.$B.jsonl $R/t56.d.$B.stripped.jsonl --values | grep -E "^==|difference lines|  "
  echo "-- decisions (T56 tree):"; cat $R/t56.d.$B.jsonl.decode.json; echo
  echo "-- kv_ready_wait checks (T56 tree):"; python3 $H/t56_check.py $R/t56.d.$B.jsonl
  echo "-- kv_ready_wait in the T51 tree's dump (must be 0):"; grep -c kv_ready_wait $R/t51.d.$B.jsonl
done
echo "#### 4. startup lines: T51 vs T56"
for f in $R/t51.startup.*.txt; do n=${f##*/t51.}; a=$(grep -E "^LINE|^lines|^built|^after|^timing" $f); b=$(grep -E "^LINE|^lines|^built|^after|^timing" $R/t56.$n); [ "$a" = "$b" ] && echo "  same: $n ($(grep -c '^LINE' $f) lines)" || { echo "  DIFF: $n"; diff <(echo "$a") <(echo "$b"); }; done
echo "#### 5. is_pd_decode against vllm-ascend's real condition"; grep -E "is_pd_decode=|no vllm_ascend|rc=" $R/t56.pd_decode_check.txt
echo "#### 6. tests"; python3 $H/t56_junit.py $R
echo "-- D-class plugin hook counts:"; for f in $R/t56.*.D.counts; do echo "  ${f##*/t56.}: $(cat $f)"; done
