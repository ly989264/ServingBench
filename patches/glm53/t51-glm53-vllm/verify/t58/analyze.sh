#!/bin/bash
# usage: analyze.sh RUN_DIR T56_CHECK_DIR   (RUN_DIR = output of run.sh); T58 checks summary
R=$1; H=$(dirname $0); C=$2/equiv/compare.py
echo "#### environment"; cat $R/env.txt
echo "#### installs"; grep -h "rc=" $R/t51.install.txt $R/t56.install.txt $R/t58.install.txt
echo "#### driver exit codes (rc != 0 listed)"; grep -l "rc=[1-9]" $R/*.log $R/*.txt 2>/dev/null | grep -v "tests_\|\.xml" | sed 's#.*/##'
echo "#### driver errors in dumps"; grep -l driver_error $R/*.jsonl 2>/dev/null | sed 's#.*/##'; echo "(none listed = none)"
echo "#### 1. level 3 (PD_DIAG_LEVEL unset): T56 vs T58, and P against T51"
echo "-- pd_equiv, 15 scenarios"; python3 $C $R/t56.equiv.jsonl $R/t58.equiv.jsonl --values | tail -3
echo "-- pd_equiv, T51 vs T58"; python3 $C $R/t51.equiv.jsonl $R/t58.equiv.jsonl --values | tail -3
for V in vllm balanceP original_vllm ported_vllm original_balance ported_balance; do
  echo "-- scheduler variant $V: T56 vs T58 / T51 vs T58"
  python3 $C $R/t56.sched.$V.jsonl $R/t58.sched.$V.jsonl --values | tail -1
  python3 $C $R/t51.sched.$V.jsonl $R/t58.sched.$V.jsonl --values | tail -1
done
for B in vllm balance; do
  echo "-- D driver, base $B: T56 vs T58"; python3 $C $R/t56.d.$B.jsonl $R/t58.d.$B.jsonl --values | grep -E "^==|difference lines|  "
  echo "   kv_ready_wait checks (T58):"; python3 $2/t56_check.py $R/t58.d.$B.jsonl | sed 's/^/   /'
done
echo "-- diag_level in level-3 dumps (must be on no record)"
for f in $R/t58.equiv.jsonl $R/t58.sched.*.jsonl $R/t58.d.vllm.jsonl $R/t58.d.balance.jsonl; do
  printf '   %s: ' "${f##*/}"; python3 $H/strip_level.py $f /tmp/strip.jsonl 3
done
for L in 1 2; do
  echo "#### 2. PD_DIAG_LEVEL=$L: T58 minus diag_level vs T56"
  for k in equiv sched.vllm sched.balanceP sched.original_vllm sched.ported_vllm sched.original_balance sched.ported_balance d.vllm d.balance; do
    printf -- '-- %s: ' "$k"; python3 $H/strip_level.py $R/t58.L$L.$k.jsonl $R/t58.L$L.$k.stripped.jsonl $L
    BASE=$R/t56.$k.jsonl
    if [ $L = 1 ] && [ ${k%%.*} = d ]; then  # D at level 1: worker_summary stages.submit left out on D1-D4 only
      printf '   T56 '; python3 $H/strip_submit.py $R/t56.$k.jsonl $R/t56.$k.nosubmit_d.jsonl
      printf '   T58 level 1 '; python3 $H/strip_submit.py $R/t58.L$L.$k.jsonl x --count-only
      BASE=$R/t56.$k.nosubmit_d.jsonl
      echo "   compared with T56 minus stages.submit of D1-D4:"
    fi
    python3 $C $BASE $R/t58.L$L.$k.stripped.jsonl --values | tail -1 | sed 's/^/   /'
  done
done
echo "-- worker_summary stages.submit, levels 2 and 3 (must equal T56):"
for B in vllm balance; do for X in "t56.d.$B" "t58.d.$B" "t58.L2.d.$B"; do printf '   %s ' "$X"; python3 $H/strip_submit.py $R/$X.jsonl x --count-only; done; done
for X in t56.equiv t58.equiv t58.L1.equiv t58.L2.equiv; do printf '   %s ' "$X"; python3 $H/strip_submit.py $R/$X.jsonl x --count-only; done
echo "#### 3. startup lines: T56 vs T58"
for f in $R/t56.startup.*.txt; do n=${f##*/t56.}
  python3 $H/startup_cmp.py $f $R/t58.$n "level=3 source=default"
  for L in 1 2; do python3 $H/startup_cmp.py $f $R/t58.L$L.$n "level=$L source=env"; done
done
for SC in engine_recompute_async worker_npu_events; do
  python3 $H/startup_cmp.py $R/t56.startup.$SC.txt $R/t58.L3env.startup.$SC.txt "level=3 source=env"
  python3 $H/startup_cmp.py $R/t56.startup.$SC.txt $R/t58.Lempty.startup.$SC.txt "level=3 source=default"
  for X in 0 4 abc 2 2x; do python3 $H/startup_cmp.py $R/t56.startup.$SC.txt $R/t58.Linv$X.startup.$SC.txt "level=3 source=invalid"; done
done
echo "#### 4. tests"; python3 $H/t58_junit.py $R
echo "-- new test files on T58:"; for f in $R/t58.tests_v1_metrics_test_pd_diag_level.py.log $R/t58.tests_v1_metrics_test_pd_kv_ready_wait.py.log; do echo "   ${f##*/t58.}: $(grep -E '^[0-9]+ (passed|failed)|passed|failed' $f | tail -1)"; done
echo "-- D-class plugin hook counts (T56 / T58):"; for f in $R/t56.*.D.counts; do n=${f##*/t56.}; echo "   $n: $(cat $f) / $(cat $R/t58.$n 2>/dev/null)"; done
