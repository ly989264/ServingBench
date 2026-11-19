#!/bin/bash
# Lab test of the small field check on a simulated site, traced with strace -f (docker run --rm only).
# Usage: run_fc_lab.sh SCRATCH WORK REPO DIST NPU_WHEEL STRACE_DIR
#   WORK holds asc-d4a and asc-d4a-mod; REPO is a vllm-ascend clone (for a later public patch/__init__.py)
set -eu
SCR=$1; WORK=$2; REPO=$3; DIST=$4; WHEEL=$5; STRACE=$6
HERE=$(cd "$(dirname "$0")" && pwd); DET=$(cd "$HERE/../../detect/lab" && pwd)
T=$SCR/fc_lab; rm -rf "$T"; mkdir -p "$T/kit" "$T/strace" "$T/launch"; chmod 777 "$T/strace"
# simulated site edits: patch/__init__.py from a later public commit, a monkey patch in a new patch file,
# one edited function in the P/D connector, one new root file
cp -R "$WORK/asc-d4a" "$T/tree"
P=vllm_ascend/patch/__init__.py; C=$(git -C "$REPO" log -1 --format=%h origin/main -- $P)
git -C "$REPO" show "$C:$P" > "$T/tree/$P"; echo "simulated: $P from public $C"
cp "$WORK/asc-d4a-mod/vllm_ascend/distributed/kv_transfer/kv_p2p/mooncake_connector.py" "$T/tree/vllm_ascend/distributed/kv_transfer/kv_p2p/"
printf 'import vllm_ascend.distributed.kv_transfer.kv_p2p.mooncake_connector as mc\n\n\ndef _new_start(self, metadata):\n    return None\n\n\nmc.MooncakeConnectorWorker.start_load_kv = _new_start\n' > "$T/tree/vllm_ascend/patch/platform/patch_site_extra.py"
echo 'BUILD = "lab"' > "$T/tree/vllm_ascend/_build_info.py"
# whole-class replacement in a new patch file: module attribute, setattr, vars()[...] and a rebound import
printf '%s\n' 'import vllm.v1.core.sched.scheduler as _m' 'from vllm_ascend.distributed.kv_transfer.kv_pool.ascend_store import pool_worker' 'from vllm.v1.core.sched.async_scheduler import AsyncScheduler' '' '' 'class _X:' '    pass' '' '' '_m.Scheduler = _X' 'setattr(pool_worker, "KVPoolWorker", _X)' 'vars(_m)["ChunkedTokenDatabase"] = _X' 'AsyncScheduler = _X' > "$T/tree/vllm_ascend/patch/platform/patch_site_class.py"
python3 "$HERE/make_01_patches.py" "$WORK/vllm-0300" "$T/tree/vllm_ascend/patch/platform"
python3 "$DET/make_site.py" "$T/site" --mooncake-wheel "$WHEEL" --ascend-local "$T/tree" >/dev/null
cp "$DIST" "$T/kit/"; cp -R "$DET/launch_d/." "$T/launch/"
docker run --rm --cap-add SYS_PTRACE --tmpfs /logs -v "$T/kit:/kit:ro" -v "$T/site:/usr/local/lib/python3.12/site-packages:ro" \
  -v "$DET/bin:/opt/t51bin:ro" -v "$T/launch:/launch:ro" -v "$STRACE:/opt/strace:ro" -v "$T/strace:/straceout" python:3.12-slim \
  bash -c 'bash /launch/start_d.sh >/dev/null 2>&1 & sleep 4
    env LD_LIBRARY_PATH=/opt/strace/lib /opt/strace/strace -f -qq -yy -E LD_LIBRARY_PATH -s 256 -o /straceout/strace.log \
      -e trace=%file,%network,%process,%signal,%ipc,close,read,write,pwrite64,writev,ftruncate,fallocate,mmap,ptrace,process_vm_readv,setpriority \
      python3 -I -S -B /kit/t51_glm53_fieldcheck.py --role D --run-dp-template /launch/run_dp_template.sh --start-script /launch/start_d.sh
    echo "rc=$?"; python3 /kit/t51_glm53_fieldcheck.py | head -1
    echo "=== with --show-diff"
    python3 -I -S -B /kit/t51_glm53_fieldcheck.py --role D --run-dp-template /launch/run_dp_template.sh --start-script /launch/start_d.sh --show-diff | sed -n "/^diff /,\$p"' > "$T/output.txt" 2>&1
cat "$T/output.txt"; echo "lines (without --show-diff): $(sed -n '1,/^rc=/p' "$T/output.txt" | grep -vc '^rc=\|REFUSED\|^simulated')"
python3 "$DET/strace_check.py" "$T/strace/strace.log" /nonexistent > "$T/strace_report.txt" || true
grep -E 'process|network|signals|opened for writing|violations' "$T/strace_report.txt"
