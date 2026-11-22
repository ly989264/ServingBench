#!/bin/bash
# Lab test of t51_glm53_check.py in throwaway Docker containers (docker run --rm, removed at the end).
# Usage: run_lab.sh LAB_DIR WORK_DIR NPU_WHEEL [case ...]
#   env OBS_WHEEL: the OBS v0.3.14.rc1 wheel (g9, g9b); env STRACE_DIR: a directory with strace and lib/ (g12)
#   LAB_DIR   scratch dir: kit/t51_glm53_check.py (single-file build), sites/, out/
#   WORK_DIR  source trees: vllm-0300 vllm-0300-mod asc-d4a asc-d4a-mod proxy/ proxy_edit/,
#             and the final patched trees (g13-g17): vllm-fin (v0.30.0 + t51-glm53-vllm db27fd1), asc-fin-p
#             (d4ad49796 + ef5bd17's 11 files + the connector replayed from the public original), asc-fin-d (the same
#             with site edits outside the port and the connector replayed from a site-edited one), asc-fin-bad
#             (asc-fin-d with one line added to a ported file and one replay change undone)
#             glm53-7 (T56): vllm-t56 (v0.30.0 + t51-glm53-vllm 796927a's T56 patch), asc-t56-p (asc-fin-p with the
#             2 files t51-glm53-ascend cf72b07 changes and the connector replayed by t51-glm53-p2d d7d26f3's T56
#             replay_p2d.py from the public original), asc-t56-d (asc-fin-d the same way, connector replayed from the
#             site-edited one)
#   env MC_REPLACEMENT: the delivered libmooncake_store.so (g13 puts it in place of the wheel's)
#   glm53-8 (T58): vllm-t58 (v0.30.0 + t51-glm53-vllm's T58 files), asc-t58-p / asc-t58-d (asc-t56-p/-d with the 2 files
#             t51-glm53-ascend's T58 changes and the connector replayed by t51-glm53-p2d's T58 replay_p2d.py from the
#             public / the site-edited original); env MC_REPLACEMENT_V2: Mooncake replacement v2 (patch 04b);
#             env BASELINE_G7: a glm53-7 baseline_token of the g22 situation (T56 lab), BASELINE_G24: g24's glm53-8 token
#   NPU_WHEEL a real mooncake-transfer-engine-npu wheel (cp312, aarch64), installed into the fake site
set -u
LAB=$1; WORK=$2; NPU_WHEEL=$3; shift 3
HERE=$(cd "$(dirname "$0")" && pwd)
CASES=${*:-"g1 g2 g3 g4 g4b g5 g6 g7 g8 g9 g9b g10 g11 g12 g13 g14 g15 g16 g17 g18 g19 g20 g21 g22 g23 g24 g25 g26 g27 g28 g29 g30 g31 g32"}
PYFLAGS="-I -S -B"   # how the check must be run (it refuses otherwise); g11 runs it without
WRAP=""              # g12 runs it under strace
mkdir -p "$LAB/out" "$LAB/sites"

mk_cann() {  # $1 dir  $2 with_dev(0/1)
  rm -rf "$1"; mkdir -p "$1/ascend-toolkit/latest/aarch64-linux"
  printf 'package_name=Ascend-cann-toolkit\nversion=25.2.1\ninnerversion=V100R001C25\narch=aarch64\n' > "$1/ascend-toolkit/latest/aarch64-linux/ascend_toolkit_install.info"
  if [ "$2" = 1 ]; then
    mkdir -p "$1/ascend-toolkit/latest/include/acl" "$1/ascend-toolkit/latest/include/hccl" "$1/ascend-toolkit/latest/lib64"
    touch "$1/ascend-toolkit/latest/include/acl/acl.h" "$1/ascend-toolkit/latest/include/hccl/hccl.h" "$1/ascend-toolkit/latest/lib64/libascendcl.so"
  fi
}
mk_cann "$LAB/cann_nodev" 0
mk_cann "$LAB/cann_dev" 1
# decoy: a vllm_ascend package next to the check script must never be picked up
mkdir -p "$LAB/kit/vllm_ascend" && : > "$LAB/kit/vllm_ascend/__init__.py"

site() {  # $1 name, rest: make_site args
  local n=$1; shift
  rm -rf "$LAB/sites/$n"; python3 "$HERE/make_site.py" "$LAB/sites/$n" "$@"
}

D_KV='{"kv_connector":"MultiConnector","kv_role":"kv_consumer","kv_load_failure_policy":"fail","kv_connector_extra_config":{"connectors":[{"kv_connector":"MooncakeConnectorV1","kv_role":"kv_consumer","kv_port":"30100","engine_id":"1"},{"kv_connector":"AscendStoreConnector","kv_role":"kv_both","kv_connector_extra_config":{"backend":"mooncake","load_async":true}}]}}'
P_ARGS="--run-dp-template /launch/run_dp_template.sh --launch-online-dp /launch/launch_online_dp.py --start-script /launch/start_p.sh --mooncake-json /launch/mooncake.json --start-mooncake /launch/start_mooncake.sh"
D_ARGS="--run-dp-template /launch/run_dp_template.sh --launch-online-dp /launch/launch_online_dp.py --start-script /launch/start_d.sh --mooncake-json /launch/mooncake.json --start-mooncake /launch/start_mooncake.sh"

run_case() {  # name image pyver hostname "docker args" "entry..." "check args" [python] [exec user]
  local name=$1 image=$2 pyver=$3 host=$4 dargs=$5 entry=$6 cargs=$7 py=${8:-python3} user=${9:-root}
  local c=t51glm-$name
  docker rm -fv "$c" >/dev/null 2>&1
  rm -rf "$LAB/out/$name"; mkdir -p "$LAB/out/$name"; chmod 777 "$LAB/out/$name"
  # shellcheck disable=SC2086
  docker run --rm -d --name "$c" --hostname "$host" --tmpfs /logs \
    -v "$LAB/kit:/kit:ro" -v "$LAB/out/$name:/tmp/t51out" -v "$HERE/bin:/opt/t51bin:ro" \
    -v "$LAB/sites/$name:/usr/local/lib/python$pyver/site-packages:ro" \
    $dargs "$image" $entry >/dev/null || { echo "$name: docker run failed"; return; }
  sleep 5
  # shellcheck disable=SC2086
  docker exec -u "$user" "$c" $WRAP $py $PYFLAGS /kit/t51_glm53_check.py $cargs --out-dir /tmp/t51out > "$LAB/out/$name/summary.txt" 2> "$LAB/out/$name/stderr.txt"
  echo "rc=$?" >> "$LAB/out/$name/stderr.txt"
  docker rm -fv "$c" >/dev/null 2>&1
  echo "== $name ($(wc -l < "$LAB/out/$name/summary.txt") lines)"; cat "$LAB/out/$name/summary.txt"; grep -v '^rc=0' "$LAB/out/$name/stderr.txt" | head -5
  rm -rf "$LAB/sites/$name"   # each fake site is several hundred MB; only the current one is kept
}

NPU_LIB=mooncake_transfer_engine_npu.libs
for case in $CASES; do
  case $case in
  g1)  # public code, P (1 instance, TP16) via start_p.sh, stdout to a file, no build tools, real npu wheel
    site g1 --mooncake-wheel "$NPU_WHEEL"
    MAP=$(cd "$LAB/sites/g1" && ls mooncake/*.so 2>/dev/null | head -2 | sed "s#^#/usr/local/lib/python3.12/site-packages/#" | paste -sd: -)
    run_case g1 python:3.12-slim 3.12 t51-glm-pnode1 \
      "-v $WORK/vllm-0300:/vllm-workspace/vllm:ro -v $WORK/asc-d4a:/vllm-workspace/vllm-ascend:ro -v $HERE/launch_p:/launch:ro -v $LAB/cann_nodev:/usr/local/Ascend:ro -e T51_FAKE_MAP=$MAP" \
      "bash /launch/start_p.sh" "$P_ARGS --startup-log /logs/vllm_9000.log" ;;
  g2)  # public code, D: 16 instances via start_d.sh with tee
    site g2 --mooncake-wheel "$NPU_WHEEL"
    run_case g2 python:3.12-slim 3.12 t51-glm-dnode1 \
      "-v $WORK/vllm-0300:/vllm-workspace/vllm:ro -v $WORK/asc-d4a:/vllm-workspace/vllm-ascend:ro -v $HERE/launch_d:/launch:ro -v $LAB/cann_nodev:/usr/local/Ascend:ro" \
      "bash /launch/start_d.sh" "$D_ARGS --startup-log /logs/vllm_9003.log" ;;
  g3)  # d4ad49796 + local edits (and v0.30.0 + local edits), export of the diff; build tools present
    site g3 --mooncake-wheel "$NPU_WHEEL"
    run_case g3 python:3 3.15 t51-glm-pnode3 \
      "-v $WORK/vllm-0300-mod:/vllm-workspace/vllm:ro -v $WORK/asc-d4a-mod:/vllm-workspace/vllm-ascend:ro -v $HERE/launch_p:/launch:ro -v $LAB/cann_dev:/usr/local/Ascend:ro" \
      "bash /launch/start_p.sh" "$P_ARGS --export-diff" ;;
  g4)  # proxy host, Python 3.9, main proxy file
    site g4 --no-vllm --proxy-deps
    run_case g4 python:3.9-slim 3.9 t51-glm-proxy1 \
      "-v $WORK/proxy:/proxy:ro -v $HERE/proxy:/proxyrun:ro" \
      "python3 /proxyrun/load_balance_proxy_server_example.py --port 8000 --workers 1 --prefiller-hosts 10.1.2.3" \
      "--proxy-file /proxy/load_balance_proxy_server_example.py" ;;
  g4b)  # proxy host, edited proxy file
    site g4b --no-vllm --proxy-deps
    run_case g4b python:3.9-slim 3.9 t51-glm-proxy2 \
      "-v $WORK/proxy_edit:/proxy:ro -v $HERE/proxy:/proxyrun:ro" \
      "python3 /proxyrun/load_balance_proxy_server_example.py --port 8000 --prefiller-hosts 10.1.2.3" \
      "--proxy-file /proxy/load_balance_proxy_server_example.py" ;;
  g5)  # local edits on Python 3.9 with the export (3.9 cannot parse some v0.30.0 files: function detail degrades, export still works)
    site g5 --mooncake-wheel "$NPU_WHEEL"
    run_case g5 python:3.9-slim 3.9 t51-glm-pnode5 \
      "-v $WORK/vllm-0300-mod:/vllm-workspace/vllm:ro -v $WORK/asc-d4a-mod:/vllm-workspace/vllm-ascend:ro -v $HERE/launch_p:/launch:ro -v $LAB/cann_nodev:/usr/local/Ascend:ro" \
      "bash /launch/start_p.sh" "$P_ARGS --export-diff" ;;
  g6)  # self-check must block: the hostname shows up in a printed value
    site g6 --mooncake-wheel "$NPU_WHEEL"
    rm -rf "$LAB/launch_block"; cp -R "$HERE/launch_p" "$LAB/launch_block"
    sed -i.bak 's/"protocol": "ascend"/"protocol": "t51-glm-pnode6"/' "$LAB/launch_block/mooncake.json"
    run_case g6 python:3.12-slim 3.12 t51-glm-pnode6 \
      "-v $WORK/vllm-0300:/vllm-workspace/vllm:ro -v $WORK/asc-d4a:/vllm-workspace/vllm-ascend:ro -v $LAB/launch_block:/launch:ro -v $LAB/cann_nodev:/usr/local/Ascend:ro" \
      "bash /launch/start_p.sh" "$P_ARGS" ;;
  g7)  # unprivileged user: no access to the vLLM processes' environ and maps
    site g7 --mooncake-wheel "$NPU_WHEEL"
    run_case g7 python:3.12-slim 3.12 t51-glm-pnode7 \
      "-v $WORK/vllm-0300:/vllm-workspace/vllm:ro -v $WORK/asc-d4a:/vllm-workspace/vllm-ascend:ro -v $HERE/launch_p:/launch:ro -v $LAB/cann_nodev:/usr/local/Ascend:ro" \
      "bash /launch/start_p.sh" "$P_ARGS" python3 nobody ;;
  g8)  # D, vLLM is PID 1, stdout goes to docker
    site g8 --mooncake-wheel "$NPU_WHEEL"
    run_case g8 python:3.12-slim 3.12 t51-glm-dnode2 \
      "-v $WORK/vllm-0300:/vllm-workspace/vllm:ro -v $WORK/asc-d4a:/vllm-workspace/vllm-ascend:ro -v $HERE/launch_d:/launch:ro -v $LAB/cann_nodev:/usr/local/Ascend:ro -e MOONCAKE_CONFIG_PATH=/launch/mooncake.json -e MC_LOG_DIR=/tmp -e VLLM_DISABLE_REQUEST_ID_RANDOMIZATION=1 -e HCCL_IF_IP=10.1.2.5 -e VLLM_USE_V2_MODEL_RUNNER=1 --entrypoint /opt/t51bin/vllm" \
      "serve /models/glm53 --host 0.0.0.0 --port 9000 --data-parallel-size 64 --data-parallel-rank 16 --data-parallel-address 10.1.2.6 --data-parallel-rpc-port 12890 --tensor-parallel-size 1 --enable-expert-parallel --kv-transfer-config $D_KV --speculative-config {\"method\":\"deepseek_mtp\",\"num_speculative_tokens\":5,\"enforce_eager\":true} --additional-config {\"scheduler_config\":{\"recompute_scheduler_enable\":true}}" \
      "$D_ARGS" ;;
  g9|g9b)  # first site run: vllm_ascend imported from another tree on PYTHONPATH (dist-info says d4ad49796),
       # the worker maps a vllm_ascend .so from a third place, OBS Mooncake wheel, a glog log file in /tmp.
       # g9: D's template and start script given on a P machine; g9b: a start_d.sh path that does not exist
    site $case --mooncake-wheel "$OBS_WHEEL"
    rm -rf "$LAB/other_$case" "$LAB/other2_$case" "$LAB/entry_$case"; mkdir -p "$LAB/other_$case" "$LAB/other2_$case/vllm_ascend" "$LAB/entry_$case"
    cp -R "$WORK/asc-f9c/vllm_ascend" "$LAB/other_$case/vllm_ascend"
    echo "__version__ = version = '0.32.0rc1.dev9+gf9c8e0bdb'" > "$LAB/other_$case/vllm_ascend/_version.py"
    SO_DIR=other2; [ "$case" = g9b ] && SO_DIR=other   # g9: .so from a third tree (so=other); g9b: from the scanned tree (so=same)
    head -c 8192 /dev/urandom > "$LAB/${SO_DIR}_$case/vllm_ascend/vllm_ascend_C.cpython-312-aarch64-linux-gnu.so"
    SP_IN=/usr/local/lib/python3.12/site-packages
    MAP=$(cd "$LAB/sites/$case" && ls mooncake_transfer_engine.libs/libmooncake_store*.so mooncake_transfer_engine.libs/libglog*.so* 2>/dev/null | sed "s#^#$SP_IN/#" | paste -sd: -)
    MAP="$MAP:/$SO_DIR/vllm_ascend/vllm_ascend_C.cpython-312-aarch64-linux-gnu.so"
    PYP=/other; [ "$case" = g9b ] && PYP=../other   # g9b: relative entry, resolved against the vLLM process cwd (/launch)
    printf '%s\n' '#!/bin/bash' '(sleep 2; touch /tmp/python3.lab.root.log.INFO.20261119-101500.42) &' 'exec bash /launch/start_p.sh' > "$LAB/entry_$case/run.sh"
    if [ "$case" = g9 ]; then
      ARGS="--run-dp-template /launchd/run_dp_template.sh --launch-online-dp /launch/launch_online_dp.py --start-script /launchd/start_d.sh --mooncake-json /launch/mooncake.json --start-mooncake /launch/start_mooncake.sh --startup-log /logs/vllm_9000.log"
    else
      ARGS="--run-dp-template /launch/run_dp_template.sh --launch-online-dp /launch/launch_online_dp.py --start-script /launch/start_d.sh --mooncake-json /launch/mooncake.json --start-mooncake /launch/start_mooncake.sh --startup-log /logs/vllm_9000.log"
    fi
    run_case $case python:3.12-slim 3.12 t51-glm-pnode9 \
      "-v $WORK/vllm-0300:/vllm-workspace/vllm:ro -v $WORK/asc-d4a:/vllm-workspace/vllm-ascend:ro -v $LAB/other_$case:/other:ro -v $LAB/other2_$case:/other2:ro -v $LAB/entry_$case:/entry:ro -v $HERE/launch_p:/launch:ro -v $HERE/launch_d:/launchd:ro -v $LAB/cann_nodev:/usr/local/Ascend:ro -e PYTHONPATH=$PYP -e T51_FAKE_MAP=$MAP" \
      "bash /entry/run.sh" "$ARGS" ;;
  g10)  # D with vllm_ascend installed as a local (non-editable) copy, one file edited and one added after install
    site g10 --mooncake-wheel "$NPU_WHEEL" --ascend-local "$WORK/asc-d4a" --ascend-tamper
    # .pyc for the pool and P/D connector directories, written by a Python with the same cache tag (cpython-312)
    "${PY312:-/opt/homebrew/bin/python3.12}" -m compileall -q "$LAB/sites/g10/vllm_ascend/distributed/kv_transfer/kv_pool/ascend_store" "$LAB/sites/g10/vllm_ascend/distributed/kv_transfer/kv_p2p" >/dev/null
    run_case g10 python:3.12-slim 3.12 t51-glm-dnode10 \
      "-v $WORK/vllm-0300:/vllm-workspace/vllm:ro -v $HERE/launch_d:/launch:ro -v $LAB/cann_nodev:/usr/local/Ascend:ro" \
      "bash /launch/start_d.sh" "$D_ARGS --startup-log /logs/vllm_9003.log" ;;
  g11)  # run without -I -S -B: must refuse, read nothing, write nothing
    site g11 --mooncake-wheel "$NPU_WHEEL"
    PYFLAGS=""
    run_case g11 python:3.12-slim 3.12 t51-glm-pnode11 \
      "-v $WORK/vllm-0300:/vllm-workspace/vllm:ro -v $WORK/asc-d4a:/vllm-workspace/vllm-ascend:ro -v $HERE/launch_p:/launch:ro -v $LAB/cann_nodev:/usr/local/Ascend:ro" \
      "bash /launch/start_p.sh" "$P_ARGS"
    PYFLAGS="-I -S -B"
    echo "g11 files written: $(ls -A "$LAB/out/g11" | grep -v -e summary.txt -e stderr.txt | wc -l | tr -d ' ')" ;;
  g12)  # zero impact: the g3 situation (P, local edits, --export-diff) traced with strace -f
    site g12 --mooncake-wheel "$NPU_WHEEL"
    rm -rf "$LAB/strace_g12"; mkdir -p "$LAB/strace_g12"; chmod 777 "$LAB/strace_g12"
    TRACE="%file,%network,%process,%signal,%ipc,close,read,write,pwrite64,writev,pwritev,pwritev2,ftruncate,fallocate,mmap,ptrace,process_vm_readv,process_vm_writev,ioprio_set,setpriority,sync,fsync,fdatasync,syncfs"
    WRAP="env LD_LIBRARY_PATH=/opt/strace/lib /opt/strace/strace -f -qq -yy -E LD_LIBRARY_PATH -s 256 -o /straceout/strace.log -e trace=$TRACE"
    run_case g12 python:3.12-slim 3.12 t51-glm-pnode12 \
      "--cap-add SYS_PTRACE -v $STRACE_DIR:/opt/strace:ro -v $LAB/strace_g12:/straceout -v $WORK/vllm-0300-mod:/vllm-workspace/vllm:ro -v $WORK/asc-d4a-mod:/vllm-workspace/vllm-ascend:ro -v $HERE/launch_p:/launch:ro -v $LAB/cann_dev:/usr/local/Ascend:ro" \
      "bash /launch/start_p.sh" "$P_ARGS --startup-log /logs/vllm_9000.log --export-diff"
    WRAP=""
    python3 "$HERE/strace_check.py" "$LAB/strace_g12/strace.log" /tmp/t51out > "$LAB/out/g12/strace_report.txt"; cat "$LAB/out/g12/strace_report.txt" ;;
  g13)  # after guide A on a public-code P: both ports exact, connector replayed, Mooncake store replaced
    site g13 --mooncake-wheel "$NPU_WHEEL"
    [ -n "${MC_REPLACEMENT:-}" ] && cp "$MC_REPLACEMENT" "$LAB/sites/g13/mooncake/libmooncake_store.so"
    run_case g13 python:3.12-slim 3.12 t51-glm-pnode13 \
      "-v $WORK/vllm-fin:/vllm-workspace/vllm:ro -v $WORK/asc-fin-p:/vllm-workspace/vllm-ascend:ro -v $HERE/launch_p:/launch:ro -v $LAB/cann_nodev:/usr/local/Ascend:ro" \
      "bash /launch/start_p.sh" "$P_ARGS --startup-log /logs/vllm_9000.log" ;;
  g14)  # after guide A on a D with site edits outside the port; connector replayed from a site-edited file
    site g14 --mooncake-wheel "$NPU_WHEEL"
    run_case g14 python:3.12-slim 3.12 t51-glm-dnode14 \
      "-v $WORK/vllm-fin:/vllm-workspace/vllm:ro -v $WORK/asc-fin-d:/vllm-workspace/vllm-ascend:ro -v $HERE/launch_d:/launch:ro -v $LAB/cann_nodev:/usr/local/Ascend:ro" \
      "bash /launch/start_d.sh" "$D_ARGS --startup-log /logs/vllm_9003.log --export-diff" ;;
  g15)  # patching gone wrong: a ported file edited afterwards, one replay change undone
    site g15 --mooncake-wheel "$NPU_WHEEL"
    run_case g15 python:3.12-slim 3.12 t51-glm-dnode15 \
      "-v $WORK/vllm-fin:/vllm-workspace/vllm:ro -v $WORK/asc-fin-bad:/vllm-workspace/vllm-ascend:ro -v $HERE/launch_d:/launch:ro -v $LAB/cann_nodev:/usr/local/Ascend:ro" \
      "bash /launch/start_d.sh" "$D_ARGS --startup-log /logs/vllm_9003.log" ;;
  g16|g17|g18)  # g14 again with --baseline: g16 its own token (same), g17 the P token of g13 (other instances),
               # g18 g14's token with async changed 1 -> 0 and the connector renamed (field differences)
    src=g14; [ "$case" = g17 ] && src=g13
    TOKEN=$(sed -n "s/.* baseline_token=\\([A-Za-z0-9_-]*\\).*/\\1/p" "$LAB/out/$src/summary.txt" | head -1)
    if [ "$case" = g18 ]; then
      TOKEN=$(python3 -c 'import base64,gzip,json,sys; t=sys.argv[1]; r=json.loads(gzip.decompress(base64.urlsafe_b64decode(t+"="*(-len(t)%4)))); r[0][5]="0"; r[0][2]="Other"; print(base64.urlsafe_b64encode(gzip.compress(json.dumps(r,separators=(",",":")).encode(),9,mtime=0)).decode().rstrip("="))' "$TOKEN")
    fi
    site "$case" --mooncake-wheel "$NPU_WHEEL"
    run_case "$case" python:3.12-slim 3.12 t51-glm-dnode14 \
      "-v $WORK/vllm-fin:/vllm-workspace/vllm:ro -v $WORK/asc-fin-d:/vllm-workspace/vllm-ascend:ro -v $HERE/launch_d:/launch:ro -v $LAB/cann_nodev:/usr/local/Ascend:ro" \
      "bash /launch/start_d.sh" "$D_ARGS --startup-log /logs/vllm_9003.log --baseline ${TOKEN:-none}" ;;
  g19)  # glm53-7: a D after the T56 upgrade (both ports and the connector at T56)
    site g19 --mooncake-wheel "$NPU_WHEEL"
    run_case g19 python:3.12-slim 3.12 t51-glm-dnode19 \
      "-v $WORK/vllm-t56:/vllm-workspace/vllm:ro -v $WORK/asc-t56-p:/vllm-workspace/vllm-ascend:ro -v $HERE/launch_d:/launch:ro -v $LAB/cann_nodev:/usr/local/Ascend:ro" \
      "bash /launch/start_d.sh" "$D_ARGS --startup-log /logs/vllm_9003.log" ;;
  g20)  # glm53-7: an upgrade half done (vLLM at T56, vllm-ascend still at T51)
    site g20 --mooncake-wheel "$NPU_WHEEL"
    run_case g20 python:3.12-slim 3.12 t51-glm-dnode20 \
      "-v $WORK/vllm-t56:/vllm-workspace/vllm:ro -v $WORK/asc-fin-p:/vllm-workspace/vllm-ascend:ro -v $HERE/launch_d:/launch:ro -v $LAB/cann_nodev:/usr/local/Ascend:ro" \
      "bash /launch/start_d.sh" "$D_ARGS --startup-log /logs/vllm_9003.log" ;;
  g21)  # glm53-7: the site-like D after the T56 upgrade (site edits outside the port, connector replayed by the T56
        # replay from the site-edited original), with --export-diff
    site g21 --mooncake-wheel "$NPU_WHEEL"
    run_case g21 python:3.12-slim 3.12 t51-glm-dnode21 \
      "-v $WORK/vllm-t56:/vllm-workspace/vllm:ro -v $WORK/asc-t56-d:/vllm-workspace/vllm-ascend:ro -v $HERE/launch_d:/launch:ro -v $LAB/cann_nodev:/usr/local/Ascend:ro" \
      "bash /launch/start_d.sh" "$D_ARGS --startup-log /logs/vllm_9003.log --export-diff" ;;
  g22)  # glm53-7: g19 on a container whose Mooncake store was already replaced (the 11-20 test-cluster exception)
    site g22 --mooncake-wheel "$NPU_WHEEL"
    [ -n "${MC_REPLACEMENT:-}" ] && cp "$MC_REPLACEMENT" "$LAB/sites/g22/mooncake/libmooncake_store.so"
    run_case g22 python:3.12-slim 3.12 t51-glm-dnode22 \
      "-v $WORK/vllm-t56:/vllm-workspace/vllm:ro -v $WORK/asc-t56-p:/vllm-workspace/vllm-ascend:ro -v $HERE/launch_d:/launch:ro -v $LAB/cann_nodev:/usr/local/Ascend:ro" \
      "bash /launch/start_d.sh" "$D_ARGS --startup-log /logs/vllm_9003.log" ;;
  g23)  # glm53-7: the glm53-1 path, a P that goes from the site's original files straight to T56 (no T51 in between):
        # P launch (1 instance, TP16) on the site-edited tree whose connector the T56 replay wrote from the site original;
        # the before state of this path is g1 (P) and g2 (D), the D after state is g21
    site g23 --mooncake-wheel "$NPU_WHEEL"
    MAP=$(cd "$LAB/sites/g23" && ls mooncake/*.so 2>/dev/null | head -2 | sed "s#^#/usr/local/lib/python3.12/site-packages/#" | paste -sd: -)
    run_case g23 python:3.12-slim 3.12 t51-glm-pnode23 \
      "-v $WORK/vllm-t56:/vllm-workspace/vllm:ro -v $WORK/asc-t56-d:/vllm-workspace/vllm-ascend:ro -v $HERE/launch_p:/launch:ro -v $LAB/cann_nodev:/usr/local/Ascend:ro -e T51_FAKE_MAP=$MAP" \
      "bash /launch/start_p.sh" "$P_ARGS --startup-log /logs/vllm_9000.log" ;;
  g24)  # glm53-8: glm53-test after T58, a D with all three at T58 and Mooncake v2 in place
    site g24 --mooncake-wheel "$NPU_WHEEL"
    [ -n "${MC_REPLACEMENT_V2:-}" ] && cp "$MC_REPLACEMENT_V2" "$LAB/sites/g24/mooncake/libmooncake_store.so"
    run_case g24 python:3.12-slim 3.12 t51-glm-dnode24 \
      "-v $WORK/vllm-t58:/vllm-workspace/vllm:ro -v $WORK/asc-t58-p:/vllm-workspace/vllm-ascend:ro -v $HERE/launch_d:/launch:ro -v $LAB/cann_nodev:/usr/local/Ascend:ro" \
      "bash /launch/start_d.sh" "$D_ARGS --startup-log /logs/vllm_9003.log" ;;
  g25)  # glm53-8: glm53-test before T58 (the g22 situation: T56 and Mooncake v1) seen by glm53-8
    site g25 --mooncake-wheel "$NPU_WHEEL"
    [ -n "${MC_REPLACEMENT:-}" ] && cp "$MC_REPLACEMENT" "$LAB/sites/g25/mooncake/libmooncake_store.so"
    run_case g25 python:3.12-slim 3.12 t51-glm-dnode25 \
      "-v $WORK/vllm-t56:/vllm-workspace/vllm:ro -v $WORK/asc-t56-p:/vllm-workspace/vllm-ascend:ro -v $HERE/launch_d:/launch:ro -v $LAB/cann_nodev:/usr/local/Ascend:ro" \
      "bash /launch/start_d.sh" "$D_ARGS --startup-log /logs/vllm_9003.log" ;;
  g26)  # glm53-8: an upgrade half done (vLLM at T58, vllm-ascend still at T56)
    site g26 --mooncake-wheel "$NPU_WHEEL"
    run_case g26 python:3.12-slim 3.12 t51-glm-dnode26 \
      "-v $WORK/vllm-t58:/vllm-workspace/vllm:ro -v $WORK/asc-t56-p:/vllm-workspace/vllm-ascend:ro -v $HERE/launch_d:/launch:ro -v $LAB/cann_nodev:/usr/local/Ascend:ro" \
      "bash /launch/start_d.sh" "$D_ARGS --startup-log /logs/vllm_9003.log" ;;
  g27)  # glm53-8: the site-like D after T58 (site edits outside the port, connector replayed by the T58 replay from the
        # site-edited original), with --export-diff
    site g27 --mooncake-wheel "$NPU_WHEEL"
    run_case g27 python:3.12-slim 3.12 t51-glm-dnode27 \
      "-v $WORK/vllm-t58:/vllm-workspace/vllm:ro -v $WORK/asc-t58-d:/vllm-workspace/vllm-ascend:ro -v $HERE/launch_d:/launch:ro -v $LAB/cann_nodev:/usr/local/Ascend:ro" \
      "bash /launch/start_d.sh" "$D_ARGS --startup-log /logs/vllm_9003.log --export-diff" ;;
  g28)  # glm53-8: the glm53-1 path, a P that goes from the site's original files straight to T58, Mooncake v2 in place
    site g28 --mooncake-wheel "$NPU_WHEEL"
    [ -n "${MC_REPLACEMENT_V2:-}" ] && cp "$MC_REPLACEMENT_V2" "$LAB/sites/g28/mooncake/libmooncake_store.so"
    MAP=$(cd "$LAB/sites/g28" && ls mooncake/*.so 2>/dev/null | head -2 | sed "s#^#/usr/local/lib/python3.12/site-packages/#" | paste -sd: -)
    run_case g28 python:3.12-slim 3.12 t51-glm-pnode28 \
      "-v $WORK/vllm-t58:/vllm-workspace/vllm:ro -v $WORK/asc-t58-d:/vllm-workspace/vllm-ascend:ro -v $HERE/launch_p:/launch:ro -v $LAB/cann_nodev:/usr/local/Ascend:ro -e T51_FAKE_MAP=$MAP" \
      "bash /launch/start_p.sh" "$P_ARGS --startup-log /logs/vllm_9000.log" ;;
  g29)  # glm53-8: g24 with PD_DIAG_LEVEL=2 in the launch environment: diag_level=2(env), and it enters the baseline
    site g29 --mooncake-wheel "$NPU_WHEEL"
    run_case g29 python:3.12-slim 3.12 t51-glm-dnode29 \
      "-v $WORK/vllm-t58:/vllm-workspace/vllm:ro -v $WORK/asc-t58-p:/vllm-workspace/vllm-ascend:ro -v $HERE/launch_d:/launch:ro -v $LAB/cann_nodev:/usr/local/Ascend:ro -e PD_DIAG_LEVEL=2" \
      "bash /launch/start_d.sh" "$D_ARGS --startup-log /logs/vllm_9003.log" ;;
  g30)  # glm53-8: g24 with PD_DIAG_LEVEL=abc: diag_level=3(invalid)
    site g30 --mooncake-wheel "$NPU_WHEEL"
    run_case g30 python:3.12-slim 3.12 t51-glm-dnode30 \
      "-v $WORK/vllm-t58:/vllm-workspace/vllm:ro -v $WORK/asc-t58-p:/vllm-workspace/vllm-ascend:ro -v $HERE/launch_d:/launch:ro -v $LAB/cann_nodev:/usr/local/Ascend:ro -e PD_DIAG_LEVEL=abc" \
      "bash /launch/start_d.sh" "$D_ARGS --startup-log /logs/vllm_9003.log" ;;
  g31)  # glm53-8: the g24 situation checked against a glm53-7 token taken before the upgrade (no diag_level in it, read
        # as 3): baseline=same
    site g31 --mooncake-wheel "$NPU_WHEEL"
    [ -n "${MC_REPLACEMENT_V2:-}" ] && cp "$MC_REPLACEMENT_V2" "$LAB/sites/g31/mooncake/libmooncake_store.so"
    run_case g31 python:3.12-slim 3.12 t51-glm-dnode24 \
      "-v $WORK/vllm-t58:/vllm-workspace/vllm:ro -v $WORK/asc-t58-p:/vllm-workspace/vllm-ascend:ro -v $HERE/launch_d:/launch:ro -v $LAB/cann_nodev:/usr/local/Ascend:ro" \
      "bash /launch/start_d.sh" "$D_ARGS --startup-log /logs/vllm_9003.log --baseline ${BASELINE_G7:-none}" ;;
  g32)  # glm53-8: the g29 situation (PD_DIAG_LEVEL=2) checked against g24's token (level 3): BASELINE_DIFF on diag_level
    site g32 --mooncake-wheel "$NPU_WHEEL"
    run_case g32 python:3.12-slim 3.12 t51-glm-dnode29 \
      "-v $WORK/vllm-t58:/vllm-workspace/vllm:ro -v $WORK/asc-t58-p:/vllm-workspace/vllm-ascend:ro -v $HERE/launch_d:/launch:ro -v $LAB/cann_nodev:/usr/local/Ascend:ro -e PD_DIAG_LEVEL=2" \
      "bash /launch/start_d.sh" "$D_ARGS --startup-log /logs/vllm_9003.log --baseline ${BASELINE_G24:-none}" ;;
  esac
done
