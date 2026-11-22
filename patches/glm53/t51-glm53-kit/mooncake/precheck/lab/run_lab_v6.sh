#!/bin/bash
# Lab cases for t51_mc_precheck.py glm53mc-6, in quay.io/ascend/cann:9.2.0-a3-openeuler24.03-py3.11 (docker run --rm).
# Mounts: /lay   the site-like layout (make_layout.sh: unpatched check build as the site's original, msgpack 9.0.0)
#         /t     pc6_t1.py (test build: the site values are the lab file's), pc6_dist.py (the delivery build: site f-1
#                values), strace_check.py
#         /v     v1.so (replacement v1, 6d70978c), v2.so (replacement v2)
#         /out   strace log
# The "vLLM process" is a process whose command line has vllm in it and which has imported mooncake.store; it is
# (re)started after each library change, so that it loads the library in place.
set -u
mkdir -p /etc/yum.repos.d/orig && mv /etc/yum.repos.d/*.repo /etc/yum.repos.d/orig/ 2>/dev/null
B=https://mirrors.tuna.tsinghua.edu.cn/openeuler/openEuler-24.03-LTS-SP4
printf '[OS]\nname=OS\nbaseurl=%s/OS/aarch64/\nenabled=1\ngpgcheck=0\n[everything]\nname=everything\nbaseurl=%s/everything/aarch64/\nenabled=1\ngpgcheck=0\n' $B $B > /etc/yum.repos.d/tuna.repo
dnf -y -q --setopt=install_weak_deps=False --setopt=retries=20 install libibverbs numactl-libs libcurl strace binutils >/dev/null 2>&1; echo "runtime libs + strace rc=$?"
set +u; source /usr/local/Ascend/cann/set_env.sh >/dev/null 2>&1; set -u
SITE=$(python3 -c "import sysconfig; print(sysconfig.get_paths()['platlib'])")
cp -a /lay/site/mooncake $SITE/mooncake
ORIG=/tmp/orig_store.so; cp $SITE/mooncake/libmooncake_store.so $ORIG
put() { cp "$1" $SITE/mooncake/.t && mv -f $SITE/mooncake/.t $SITE/mooncake/libmooncake_store.so; }
SIM=
sim() { [ -n "$SIM" ] && { kill $SIM 2>/dev/null; wait $SIM 2>/dev/null; }; bash -c 'exec -a vllm-sim python3 -c "import time, mooncake.store; time.sleep(300)"' & SIM=$!; sleep 4; }
echo "== U0: original in place, vLLM process (test build: site values = the lab file): the original's side unchanged"
sim; python3 -I -S -B /t/pc6_t1.py; echo "exit=$?"
echo "== U1: v1 in place, vLLM process (delivery build): the upgrade v1 -> v2"
put /v/v1.so; sim; python3 -I -S -B /t/pc6_dist.py; echo "exit=$?"
echo "== strace (U1 situation, delivery build)"; strace -f -qq -yy -s 256 -o /out/strace6.log -e trace=%file,%network,%process,%signal,%ipc,close,read,write,pwrite64,writev,pwritev,pwritev2,ftruncate,fallocate,mmap,ptrace,process_vm_readv,process_vm_writev,ioprio_set,setpriority,sync,fsync,fdatasync,syncfs python3 -I -S -B /t/pc6_dist.py > /dev/null; python3 /t/strace_check.py /out/strace6.log /nonexistent_out_dir | grep -E "violations|opened for writing|network|signals|process:|bytes read"
echo "== U2: v1 in place, a second copy in a .libs directory (delivery build)"
mkdir -p $SITE/mooncake_transfer_engine.libs && cp $SITE/mooncake/libmooncake_store.so $SITE/mooncake_transfer_engine.libs/libmooncake_store-0123abcd.so; python3 -I -S -B /t/pc6_dist.py | grep -E "^mc_orig|^mc_pre"; rm -rf $SITE/mooncake_transfer_engine.libs
echo "== U3: v1 in place, without -I -S -B"; python3 /t/pc6_dist.py; echo "exit=$?"
echo "== U4: v2 already in place (delivery build)"
put /v/v2.so; sim; python3 -I -S -B /t/pc6_dist.py | grep -E "^mc_orig|^mc_build|^mc_exports|^mc_pre"; echo "exit=$?"
echo "== U5: original in place, no vLLM process (delivery build: site f-1 values, so the lab file is a changed library)"
kill $SIM 2>/dev/null; wait $SIM 2>/dev/null; SIM=
put $ORIG; python3 -I -S -B /t/pc6_dist.py | grep -E "^mc_orig|^mc_pre"; echo "exit=$?"
