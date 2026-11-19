#!/bin/bash
# Walk guide A step 11 (glm5.3, Mooncake) with the guide's own bash blocks, in a throwaway
# quay.io/ascend/cann:9.2.0-a3-openeuler24.03-py3.11 container (docker run --rm, no NPU).
# Usage: walk_11.sh SCRATCH MC_TAR LAYOUT_SITE DETECTOR TEST_PRECHECK
#   MC_TAR       the delivered t51-glm53-mooncake-kit.tar
#   LAYOUT_SITE  .../lay/site: mooncake/ laid out like the site's wheel, with the unpatched check build as the
#                site's original libmooncake_store.so (make_layout.sh); installed into the container's site-packages
#   DETECTOR     the patch kit's detect/t51_glm53_check.py (glm53-6, f9e6073f), placed at /tmp/t51-kit/detect/
#   TEST_PRECHECK the precheck built with the stand-in's size and sha16 as the site values: the delivered one stops
#                with STOP_changed on the stand-in, as it must; this one lets the walk go on
# The monitor is $W/mon and the machine's host is $W/host on this Mac: scp/ssh become cp/mkdir (in a --rm container
# for the monitor's sha256sum), docker cp is real. A stand-in vLLM process (python3 named "vllm serve") loads
# mooncake.store, puts 4 keys through a local mooncake_master (MC_FORCE_TCP=1) and stays up; its stderr is the
# "vLLM log file" on the host.
set -u
SCRATCH=$1; TAR=$2; LAY=$3; DET=$4; TPC=$5
IMG=quay.io/ascend/cann:9.2.0-a3-openeuler24.03-py3.11
HERE=$(cd "$(dirname "$0")" && pwd)
GUIDE="${GUIDE:?set GUIDE to the manual}"
W="$SCRATCH/walk11"; rm -rf "$W"; mkdir -p "$W/mon/vllm-pd-patch-glm53" "$W/host/tmp" "$W/host/logs" "$W/blocks"
C=t51-mcwalk; MACH=lab-d0; TS=
cp "$TAR" "$W/mon/vllm-pd-patch-glm53/"; (cd "$W/mon/vllm-pd-patch-glm53" && shasum -a 256 t51-glm53-mooncake-kit.tar > t51-glm53-mooncake-kit.tar.sha256)
LOG="$W/host/logs/vllm-9100.log"

/opt/homebrew/bin/python3.12 -I - "$GUIDE" "$W/blocks" <<'PY'
import re, sys, os
text = open(sys.argv[1], encoding="utf-8").read()
blocks = re.findall(r"```bash\n(.*?)```", text, re.S)
for i, b in enumerate(blocks):
    open(os.path.join(sys.argv[2], "%02d.sh" % i), "w").write(b)
print(len(blocks), "bash blocks in the guide")
PY
pick() { grep -lF -- "$1" "$W"/blocks/*.sh | head -1; }
MONSHA=$(pick 'sha256sum -c t51-glm53-mooncake-kit.tar.sha256'); SCPT=$(pick 'scp <用户目录>/vllm-pd-patch-glm53/t51-glm53-mooncake-kit.tar')
DCPT=$(pick 'docker cp /tmp/t51-glm53-mooncake-kit.tar'); UNPACK=$(pick 'tar -xf /tmp/t51-glm53-mooncake-kit.tar')
PRE=$(pick 't51_mc_precheck.py; echo'); FIND=$(pick 'MC_PKG=$(python3'); BACKUP=$(pick 't51-glm53-mc-backup-$(date')
HOSTCP=$(pick 'docker cp <容器名>:<MC 备份目录>'); MONCP=$(pick 'scp -r <SSH 账号>@<机器地址>:/tmp/t51-glm53-mc-backup-')
MONVER=$(pick 'backup/<机器>-mc-<MC 时间戳> && LC_ALL=C sha256sum -c SHA256SUMS'); GUARD=$(pick '"${MC_PKG:?}"')
REPL=$(pick '.t51_new_store.so'); LDCAP=$(pick 'vllm.ld_library_path; for d in'); MAPS=$(grep -LF -e 'MC_PKG=' -e 'port_of()' $(grep -lF "grep -h 'libmooncake_store'" "$W"/blocks/*.sh) | head -1); PORTMAP=$(pick 'port_of()')
IMP=$(pick 'import mooncake.store as s'); LOGG=$(pick 'undefined symbol|libmooncake_store'); DETP=$(pick 'start_p.sh --mooncake-json')
BPT=$(pick "grep -c 'complete=false'"); RSCP=$(pick 'ssh <SSH 账号>@<机器地址> mkdir -p /tmp/<机器>-mc-<MC 时间戳>')
RDCP=$(pick 'docker cp /tmp/<机器>-mc-<MC 时间戳>/.'); RVER=$(pick 'cut -c1-64 SHA256SUMS'); RB=$(pick '.t51_orig_store.so')
for b in MONSHA SCPT DCPT UNPACK PRE FIND LDCAP PORTMAP BACKUP HOSTCP MONCP MONVER GUARD REPL MAPS IMP LOGG DETP BPT RSCP RDCP RVER RB; do
  [ -n "${!b}" ] || { echo "block $b not found"; exit 1; }
done
lbl() { echo "### $(basename "$1") ${2:-}"; }
mon() {  # monitor-side commands: <用户目录> is /mon, the machine's /tmp is /host/tmp
  docker run --rm -v "$W/mon:/mon" -v "$W/host:/host" "$IMG" bash -c "$1" 2>&1
}
sub_mon() { sed -e 's#<用户目录>#/mon#g' -e "s#<机器>#$MACH#g" -e "s#<MC 时间戳>#$TS#g" \
  -e 's#scp -r <SSH 账号>@<机器地址>:/tmp/#cp -r /host/tmp/#' -e 's#ssh <SSH 账号>@<机器地址> mkdir -p /tmp/#mkdir -p /host/tmp/#' \
  -e 's#<SSH 账号>@<机器地址>:/tmp/#/host/tmp/#g' -e 's#^scp #cp #' -e 's#&& scp #\&\& cp #' "$1"; }
in_c() { docker exec "$C" bash -c "$1" 2>&1; }
host() { bash -c "$1" 2>&1; }   # the machine's host: this Mac, with the stand-in log file

docker rm -f "$C" >/dev/null 2>&1
docker run -d --rm --name "$C" -v "$LAY:/lay/site:ro" -v "$W/host/logs:/hostlogs" "$IMG" sleep 3600 >/dev/null || exit 1
in_c 'mkdir -p /etc/yum.repos.d/orig && mv /etc/yum.repos.d/*.repo /etc/yum.repos.d/orig/ 2>/dev/null
B=https://mirrors.tuna.tsinghua.edu.cn/openeuler/openEuler-24.03-LTS-SP4
printf "[OS]\nname=OS\nbaseurl=%s/OS/aarch64/\nenabled=1\ngpgcheck=0\n[everything]\nname=everything\nbaseurl=%s/everything/aarch64/\nenabled=1\ngpgcheck=0\n" $B $B > /etc/yum.repos.d/tuna.repo
dnf -y -q --setopt=install_weak_deps=False --setopt=retries=20 install libibverbs numactl-libs libcurl procps-ng >/dev/null 2>&1; echo "runtime packages rc=$?"
SITE=$(python3 -c "import sysconfig; print(sysconfig.get_paths()[\"platlib\"])"); cp -a /lay/site/mooncake "$SITE/mooncake"
# what pip leaves next to the package: dist-info with a RECORD, so the detector can tell files changed since install
D="$SITE/mooncake_transfer_engine_npu-0.3.13.dist-info"; mkdir -p "$D"
printf "Metadata-Version: 2.1\nName: mooncake-transfer-engine-npu\nVersion: 0.3.13\n" > "$D/METADATA"; echo mooncake > "$D/top_level.txt"
(cd "$SITE" && for f in mooncake/*; do [ -f "$f" ] && python3 -c "import base64,hashlib,sys,os; p=sys.argv[1]; print(\"%s,sha256=%s,%d\" % (p, base64.urlsafe_b64encode(hashlib.sha256(open(p,\"rb\").read()).digest()).rstrip(b\"=\").decode(), os.path.getsize(p)))" "$f"; done > "$D/RECORD")
echo "site-packages: $SITE; mooncake files $(ls "$SITE/mooncake" | wc -l); RECORD $(wc -l < "$D/RECORD") lines"
mkdir -p /tmp/t51-kit/detect'
docker cp "$DET" "$C:/tmp/t51-kit/detect/t51_glm53_check.py" >/dev/null
cat > "$W/sim.py" <<'PY'
import sys, time
from mooncake.store import MooncakeDistributedStore
s = MooncakeDistributedStore()
rc = s.setup("127.0.0.1", "http://127.0.0.1:8080/metadata", 64 << 20, 64 << 20, "tcp", "", "127.0.0.1:50051")
keys = ["t51-walk-%d-%d" % (int(time.time()), i) for i in range(4)]
print("sim setup rc", rc, "put_batch rc", s.put_batch(keys, [bytes([65 + i]) * 4096 for i in range(4)]), flush=True)
time.sleep(3000)
PY
docker cp "$W/sim.py" "$C:/tmp/sim.py" >/dev/null
SIMSTART='cd /tmp && set +u && source /usr/local/Ascend/cann/set_env.sh >/dev/null 2>&1; (MC_FORCE_TCP=1 setsid bash -c "exec -a \"vllm serve sim --port 9100\" python3 /tmp/sim.py" >> /hostlogs/vllm-9100.log 2>&1 &) ; for i in $(seq 1 60); do grep -q "sim setup rc" /hostlogs/vllm-9100.log 2>/dev/null && [ $(grep -c "sim setup rc" /hostlogs/vllm-9100.log) -ge ${1:-1} ] && break; sleep 1; done; grep "sim setup rc" /hostlogs/vllm-9100.log | tail -1'
SIMSTOP='pkill -f "[v]llm serve sim --port 9100"; sleep 2; pgrep -f "[v]llm serve sim --port 9100" >/dev/null && echo "sim still running" || echo "sim stopped"'
# a second stand-in instance in the same container (port 9101) that is not restarted in this walk: between the
# replacement and its own restart it keeps the original library (the window of guide 11.0 / 11.5)
SIM2='cd /tmp && set +u && source /usr/local/Ascend/cann/set_env.sh >/dev/null 2>&1; (MC_FORCE_TCP=1 setsid bash -c "exec -a \"vllm serve sim --port 9101\" python3 /tmp/sim.py" >> /hostlogs/vllm-9101.log 2>&1 &) ; for i in $(seq 1 60); do grep -q "sim setup rc" /hostlogs/vllm-9101.log 2>/dev/null && break; sleep 1; done; grep "sim setup rc" /hostlogs/vllm-9101.log | tail -1'
# the lab's master is restarted with the stand-in (it would keep the killed stand-in's segment; not a site step)
MASTER='pkill -x mooncake_master; sleep 1; set +u; source /usr/local/Ascend/cann/set_env.sh >/dev/null 2>&1; SITE=$(python3 -c "import sysconfig; print(sysconfig.get_paths()[\"platlib\"])"); (setsid "$SITE/mooncake/mooncake_master" --port 50051 --enable_http_metadata_server=true --http_metadata_server_port 8080 > /tmp/master.log 2>&1 &); sleep 3; pgrep -x mooncake_master >/dev/null && echo "mooncake_master up" || echo "mooncake_master NOT up"'
in_c "$MASTER"
echo "### stand-in vLLM (original library), port 9100"; in_c "$(echo "$SIMSTART" | sed 's/\${1:-1}/1/')"
echo "### second stand-in instance, port 9101 (not restarted in this walk)"; in_c "$SIM2"
DET_IN="python3 -I -S -B /tmp/t51-kit/detect/t51_glm53_check.py --out-dir /tmp --max-seconds 120 2>&1 | grep -E '^mooncake=|^verdict |REFUSED|BLOCKED' | cut -c1-240"

echo "===== 11.1 (monitor, machine, container)"
lbl "$MONSHA"; mon "$(sub_mon "$MONSHA")"
lbl "$SCPT"; mon "$(sub_mon "$SCPT")" && ls -l "$W/host/tmp" | tail -n +2 | awk '{print $5, $NF}'
lbl "$DCPT"; host "$(sed -e "s#<容器名>#$C#" -e "s#/tmp/t51-glm53-mooncake-kit.tar#$W/host/tmp/t51-glm53-mooncake-kit.tar#" "$DCPT")"
lbl "$UNPACK"; in_c "$(cat "$UNPACK")"
echo "===== 11.2"
lbl "$PRE" '(delivered precheck, site f-1 values: on the stand-in it must stop with STOP_changed)'; in_c "$(cat "$PRE")"
docker cp "$TPC" "$C:/tmp/pc_test.py" >/dev/null
lbl "$PRE" '(test build: site values = the stand-in)'; in_c "$(sed 's#/tmp/t51-mc/precheck/dist/t51_mc_precheck.py#/tmp/pc_test.py#' "$PRE")"
echo "===== 11.3"
lbl "$FIND"; lbl "$LDCAP"; lbl "$BACKUP"; OUT=$(in_c "$(cat "$FIND" "$LDCAP" "$BACKUP"); declare -p MC_PKG MC_LIB BKMC > /tmp/walkenv"); echo "$OUT"
BKDIR=$(echo "$OUT" | grep -m1 -E '^/(tmp|root)/t51-glm53-mc-backup-'); TS=${BKDIR##*-backup-}; echo "### MC backup dir $BKDIR, timestamp $TS"
lbl "$HOSTCP"; host "$(sed -e "s#<容器名>#$C#" -e "s#<MC 备份目录>#$BKDIR#" -e "s# /tmp/\$# $W/host/tmp/#" "$HOSTCP")"
lbl "$MONCP"; mon "$(sub_mon "$MONCP")"; lbl "$MONVER"; mon "$(sub_mon "$MONVER")"
echo "===== 11.4"
lbl "$GUARD" '(in a new shell without the variables: must refuse)'; in_c "$(cat "$GUARD")"; echo "rc=$?"
lbl "$GUARD" '(in the shell of 11.3)'; in_c ". /tmp/walkenv; $(cat "$GUARD")"
lbl "$REPL"; in_c ". /tmp/walkenv; $(cat "$REPL")"
lbl "$MAPS" '(after the rename, before the restart)'; in_c "$(cat "$MAPS")"
echo "===== 11.5 (stop, import check, start)"
echo "### stop the stand-in vLLM"; in_c "$SIMSTOP"
echo "### the same import in a shell without vLLM's LD_LIBRARY_PATH (for comparison; not a guide block)"; in_c 'python3 -B -c "import mooncake.store" 2>&1 | tail -1'
lbl "$IMP"; in_c "$(cat "$IMP")"
echo "### start the stand-in vLLM again (lab master restarted first)"; in_c "$MASTER"; in_c "$(echo "$SIMSTART" | sed 's/\${1:-1}/2/')"
lbl "$PORTMAP" '(after the restart)'; in_c "$(cat "$PORTMAP")"
lbl "$LOGG"; host "$(sed "s#<vLLM 日志文件>#$LOG#" "$LOGG")"; echo "(end of output)"
echo "===== 11.6 (the detector of the patch kit; no vLLM tree and no start files in the lab: --out-dir/--max-seconds only)"
in_c "$DET_IN"
echo "===== 11.7"
lbl "$BPT"; host "$(sed "s#<vLLM 日志文件>#$LOG#" "$BPT")" | sed "s#$W/#<W>/#g"
echo "===== 11.9 rollback"
lbl "$RSCP"; mon "$(sub_mon "$RSCP")"; ls "$W/host/tmp/$MACH-mc-$TS"
lbl "$RDCP"; host "$(sed -e "s#<容器名>#$C#" -e "s#<机器>#$MACH#g" -e "s#<MC 时间戳>#$TS#g" -e "s#docker cp /tmp/#docker cp $W/host/tmp/#" "$RDCP")"
lbl "$RVER"; in_c "$(sed -e "s#<机器>#$MACH#g" -e "s#<MC 时间戳>#$TS#g" "$RVER")"
lbl "$FIND" '(first line only)'; lbl "$RB"; in_c "$(head -1 "$FIND"); $(sed -e "s#<机器>#$MACH#g" -e "s#<MC 时间戳>#$TS#g" "$RB")"
echo "### restart the stand-in vLLM (lab master restarted first)"; in_c "$SIMSTOP"; in_c "$MASTER"; in_c "$(echo "$SIMSTART" | sed 's/\${1:-1}/3/')"
lbl "$PORTMAP" '(after the rollback restart)'; in_c "$(cat "$PORTMAP")"
echo "### detector after the rollback"; in_c "$DET_IN"
lbl "$BPT" '(after the rollback: the count stays, no new lines)'; host "$(sed "s#<vLLM 日志文件>#$LOG#" "$BPT")" | sed "s#$W/#<W>/#g"
echo "### the [BatchPutTiming] lines in the stand-in's log"; grep -h 'BatchPutTiming' "$LOG"
docker rm -f "$C" >/dev/null 2>&1; echo "walk done"
