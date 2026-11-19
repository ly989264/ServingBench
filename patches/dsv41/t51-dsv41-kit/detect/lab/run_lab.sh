#!/bin/bash
# Lab test of t51_dsv41_check.py in throwaway Docker containers (docker run --rm, removed at the end).
# Usage: run_lab.sh LAB_DIR WORK_DIR [case ...]
#   LAB_DIR  scratch dir: kit/t51_dsv41_check.py (single-file build), sites/, out/
#   WORK_DIR source trees: vllm-0320 vllm-0320p vllm-mod asc-f9c asc-f9c-02part asc-mod proxy/
set -u
LAB=$1; WORK=$2; shift 2
HERE=$(cd "$(dirname "$0")" && pwd)
CASES=${*:-"c1 c2 c3 c4 c4b c5 c6 c7 c8 c9"}
mkdir -p "$LAB/out" "$LAB/sites"
OBS_WHL=${OBS_WHL:-}

mk_cann() {  # $1 dir  $2 with_dev(0/1)
  rm -rf "$1"; mkdir -p "$1/ascend-toolkit/latest/aarch64-linux"
  printf 'package_name=Ascend-cann-toolkit\nversion=9.2.1\ninnerversion=V100R001C26\narch=aarch64\n' > "$1/ascend-toolkit/latest/aarch64-linux/ascend_toolkit_install.info"
  if [ "$2" = 1 ]; then
    mkdir -p "$1/ascend-toolkit/latest/include/acl" "$1/ascend-toolkit/latest/include/hccl" "$1/ascend-toolkit/latest/lib64"
    touch "$1/ascend-toolkit/latest/include/acl/acl.h" "$1/ascend-toolkit/latest/include/hccl/hccl.h" "$1/ascend-toolkit/latest/lib64/libascendcl.so"
  fi
}
mk_cann "$LAB/cann_nodev" 0
mk_cann "$LAB/cann_dev" 1

site() {  # $1 name, rest: make_site args
  local n=$1; shift
  rm -rf "$LAB/sites/$n"; python3 "$HERE/make_site.py" "$LAB/sites/$n" "$@"
}

D_KV='{"kv_connector":"MultiConnector","kv_role":"kv_consumer","kv_load_failure_policy":"recompute","kv_connector_extra_config":{"connectors":[{"kv_connector":"MooncakeHybridConnector","kv_role":"kv_consumer","engine_id":"1"},{"kv_connector":"AscendStoreConnector","kv_role":"kv_both","kv_connector_extra_config":{"backend":"mooncake","load_async":true}}]}}'
P_ARGS="--run-dp-template /launch/run_dp_template.sh --launch-online-dp /launch/launch_online_dp.py --start-script /launch/start_p.sh --mooncake-json /launch/mooncake.json --start-mooncake /launch/start_mooncake.sh"
D_ARGS="--run-dp-template /launch/run_dp_template.sh --launch-online-dp /launch/launch_online_dp.py --start-script /launch/start_d.sh --mooncake-json /launch/mooncake.json --start-mooncake /launch/start_mooncake.sh"

run_case() {  # name image pyver hostname "docker args" "entry..." "check args" [python] [exec user]
  local name=$1 image=$2 pyver=$3 host=$4 dargs=$5 entry=$6 cargs=$7 py=${8:-python3} user=${9:-root}
  local c=t51lab-$name
  docker rm -fv "$c" >/dev/null 2>&1
  rm -rf "$LAB/out/$name"; mkdir -p "$LAB/out/$name"; chmod 777 "$LAB/out/$name"
  # shellcheck disable=SC2086
  docker run --rm -d --name "$c" --hostname "$host" --tmpfs /logs \
    -v "$LAB/kit:/kit:ro" -v "$LAB/out/$name:/t51out" -v "$HERE/bin:/opt/t51bin:ro" \
    -v "$LAB/sites/$name:/usr/local/lib/python$pyver/site-packages:ro" \
    $dargs "$image" $entry >/dev/null || { echo "$name: docker run failed"; return; }
  sleep 4
  # shellcheck disable=SC2086
  docker exec -u "$user" "$c" $py /kit/t51_dsv41_check.py $cargs --out-dir /t51out > "$LAB/out/$name/summary.txt" 2> "$LAB/out/$name/stderr.txt"
  echo "rc=$?" >> "$LAB/out/$name/stderr.txt"
  docker exec "$c" sh -c 'ps -eo pid,ppid,comm,args 2>/dev/null || true' > "$LAB/out/$name/ps.txt" 2>&1
  docker rm -fv "$c" >/dev/null 2>&1
  echo "== $name ($(wc -l < "$LAB/out/$name/summary.txt") lines)"; cat "$LAB/out/$name/summary.txt"; grep -v '^rc=0' "$LAB/out/$name/stderr.txt" | head -5
}

MC_P=/usr/local/lib/python3.12/site-packages/mooncake
for case in $CASES; do
  case $case in
  c1)  # public code, P, launched by start_p.sh (vLLM not PID 1), stdout to file, no build tools, real Mooncake wheel if given
    if [ -n "$OBS_WHL" ]; then site c1 --mooncake-wheel "$OBS_WHL"; else site c1 --fake-mooncake; fi
    MAP=/usr/local/lib/python3.12/site-packages/mooncake_transfer_engine.libs/libmooncake_store-2a608938.so:/usr/local/lib/python3.12/site-packages/mooncake_transfer_engine.libs/libglog-bb9621e6.so.0.4.0
    run_case c1 python:3.12-slim 3.12 t51-lab-pnode7 \
      "-v $WORK/vllm-0320:/vllm-workspace/vllm:ro -v $WORK/asc-f9c:/vllm-workspace/vllm-ascend:ro -v $HERE/launch_p:/launch:ro -v $LAB/cann_nodev:/usr/local/Ascend:ro -e T51_FAKE_MAP=$MAP" \
      "bash /launch/start_p.sh" "$P_ARGS --startup-log /logs/vllm_9000.log" ;;
  c2)  # colleague patch 01 on vLLM + the parts of 02 that apply to f9c; D; vLLM is PID 1; stdout goes to docker
    site c2 --fake-mooncake
    run_case c2 python:3.12-slim 3.12 t51-lab-dnode2 \
      "-v $WORK/vllm-0320p:/vllm-workspace/vllm:ro -v $WORK/asc-f9c-02part:/vllm-workspace/vllm-ascend:ro -v $HERE/launch_d:/launch:ro -v $LAB/cann_nodev:/usr/local/Ascend:ro -e VLLM_USE_V2_MODEL_RUNNER=1 -e MOONCAKE_CONFIG_PATH=/launch/mooncake.json -e T51_FAKE_PD_DIAG=1 -e GLOG_logtostderr=1 -e HCCL_IF_IP=10.1.2.5 --entrypoint /opt/t51bin/vllm" \
      "serve /models/dsv41 --host 0.0.0.0 --port 9000 --data-parallel-size 16 --data-parallel-rank 8 --data-parallel-address 10.1.2.3 --data-parallel-rpc-port 12890 --tensor-parallel-size 2 --enable-expert-parallel --async-scheduling --kv-transfer-config $D_KV --speculative-config {\"method\":\"dspark\",\"num_speculative_tokens\":5,\"enforce_eager\":false}" \
      "$D_ARGS" ;;
  c3)  # other local edits, D x8 via start_d.sh with tee, build tools and CANN dev files present, Mooncake with timing string
    site c3 --fake-mooncake --mooncake-patched
    MAP=/usr/local/lib/python3.15/site-packages/mooncake_transfer_engine.libs/libmooncake_store-2a608938.so:/usr/local/lib/python3.15/site-packages/mooncake_transfer_engine.libs/libglog-bb9621e6.so.0.4.0
    run_case c3 python:3 3.15 t51-lab-dnode3 \
      "-v $WORK/vllm-mod:/vllm-workspace/vllm:ro -v $WORK/asc-mod:/vllm-workspace/vllm-ascend:ro -v $HERE/launch_d:/launch:ro -v $LAB/cann_dev:/usr/local/Ascend:ro -e T51_FAKE_MAP=$MAP" \
      "bash /launch/start_d.sh" "$D_ARGS --startup-log /logs/vllm_9003.log" ;;
  c4)  # proxy host, Python 3.9, main proxy file
    site c4 --no-vllm --proxy-deps
    run_case c4 python:3.9-slim 3.9 t51-lab-proxy1 \
      "-v $WORK/proxy:/proxy:ro -v $HERE/proxy:/proxyrun:ro" \
      "python3 /proxyrun/load_balance_proxy_server_example.py --port 8000 --workers 1 --prefiller-hosts 10.1.2.3 --prefiller-ports 9000 --decoder-hosts 10.1.2.5 --decoder-ports 9000" \
      "--proxy-file /proxy/load_balance_proxy_server_example.py" ;;
  c4b)  # proxy host, edited proxy file
    site c4b --no-vllm --proxy-deps
    run_case c4b python:3.9-slim 3.9 t51-lab-proxy2 \
      "-v $WORK/proxy_edit:/proxy:ro -v $HERE/proxy:/proxyrun:ro" \
      "python3 /proxyrun/load_balance_proxy_server_example.py --port 8000 --prefiller-hosts 10.1.2.3" \
      "--proxy-file /proxy/load_balance_proxy_server_example.py" ;;
  c5)  # like c1 on Python 3.9
    site c5 --fake-mooncake
    run_case c5 python:3.9-slim 3.9 t51-lab-pnode8 \
      "-v $WORK/vllm-0320:/vllm-workspace/vllm:ro -v $WORK/asc-f9c:/vllm-workspace/vllm-ascend:ro -v $HERE/launch_p:/launch:ro -v $LAB/cann_nodev:/usr/local/Ascend:ro" \
      "bash /launch/start_p.sh" "$P_ARGS --startup-log /logs/vllm_9000.log" ;;
  c6)  # self-check must block: the hostname shows up in a printed value
    site c6 --fake-mooncake
    rm -rf "$LAB/launch_block"; cp -R "$HERE/launch_p" "$LAB/launch_block"
    sed -i.bak 's/"protocol": "ascend"/"protocol": "t51-lab-pnode9"/' "$LAB/launch_block/mooncake.json"
    run_case c6 python:3.12-slim 3.12 t51-lab-pnode9 \
      "-v $WORK/vllm-0320:/vllm-workspace/vllm:ro -v $WORK/asc-f9c:/vllm-workspace/vllm-ascend:ro -v $LAB/launch_block:/launch:ro -v $LAB/cann_nodev:/usr/local/Ascend:ro" \
      "bash /launch/start_p.sh" "$P_ARGS" ;;
  c7)  # detail file must also block: an install path that contains the hostname; plus run as a non-root user
    site c7 --fake-mooncake
    run_case c7 python:3.12-slim 3.12 t51-lab-pnode10 \
      "-v $WORK/vllm-0320:/opt/t51-lab-pnode10/vllm:ro -v $WORK/asc-f9c:/vllm-workspace/vllm-ascend:ro -v $HERE/launch_p:/launch:ro -e PYTHONPATH=/opt/t51-lab-pnode10/vllm" \
      "bash /launch/start_p.sh" "$P_ARGS" ;;
  c8)  # like c1, but the check runs as an unprivileged user: no access to the vLLM processes' environ and maps
    site c8 --fake-mooncake
    run_case c8 python:3.12-slim 3.12 t51-lab-pnode11 \
      "-v $WORK/vllm-0320:/vllm-workspace/vllm:ro -v $WORK/asc-f9c:/vllm-workspace/vllm-ascend:ro -v $HERE/launch_p:/launch:ro -v $LAB/cann_nodev:/usr/local/Ascend:ro -e T51_FAKE_MAP=$MC_P/store.so" \
      "bash /launch/start_p.sh" "$P_ARGS" python3 nobody ;;
  c9)  # after the kit: vLLM with patch 01, vllm-ascend with the T51 patch, rebuilt Mooncake library
    site c9 --fake-mooncake --mooncake-patched
    run_case c9 python:3.12-slim 3.12 t51-lab-pnode12 \
      "-v $WORK/vllm-0320p:/vllm-workspace/vllm:ro -v $WORK/asc-port:/vllm-workspace/vllm-ascend:ro -v $HERE/launch_p:/launch:ro -v $LAB/cann_nodev:/usr/local/Ascend:ro -e T51_FAKE_MAP=$MC_P/store.so" \
      "bash /launch/start_p.sh" "$P_ARGS" ;;
  esac
done
