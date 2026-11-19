#!/bin/bash
# Lab cases for t51_mc_precheck.py glm53mc-5, in quay.io/ascend/cann:9.2.0-a3-openeuler24.03-py3.11 (docker run --rm).
# Mounts: /lay   the site-like layout (make_layout.sh: unpatched check build as the site's original, msgpack 9.0.0)
#         /t     pc_t1.py (test build: the site values are the lab file's), pc_dist.py (delivery build: site f-1
#                values), pc_p1.py / pc_p13.py (test builds whose site values are the path-length builds' files),
#                pc_repl.py (test build whose site values are the replacement's), pc_ro8.py, pc_pfx2.py (same for ro8.so, pfx2.so),
#                strace_check.py
#         /p     p1.so, p13.so: the unpatched check build compiled at a source path 1 and 13 characters longer,
#                stripped and relinked like the site; repl.so: the replacement; ro8.so: the lab file with the .rodata
#                size in its section header raised by 8 (nothing else changed); pfx2.so: the lab file with 5 of its 65
#                path strings' prefix /build/src/ changed to /build/srX/ (same length, nothing else changed)
#         /obs   mooncake/libmooncake_store.so from the OBS wheel (same commit, other options)
#         /out   strace log
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
echo "== T0: no vLLM process (test build: site values = the lab file)"; python3 -I -S -B /t/pc_t1.py; echo "exit=$?"
bash -c 'exec -a vllm-sim python3 -c "import time, mooncake.store; time.sleep(120)"' & SIM=$!; sleep 4
echo "== T1: vLLM-like process (test build)"; python3 -I -S -B /t/pc_t1.py; echo "exit=$?"
echo "== T2: delivery build (site f-1 values) on the lab layout"; python3 -I -S -B /t/pc_dist.py; echo "exit=$?"
echo "== T3: second copy in a .libs directory"; mkdir -p $SITE/mooncake_transfer_engine.libs && cp $SITE/mooncake/libmooncake_store.so $SITE/mooncake_transfer_engine.libs/libmooncake_store-0123abcd.so; python3 -I -S -B /t/pc_t1.py | tail -1; rm -rf $SITE/mooncake_transfer_engine.libs
echo "== T4: without -I -S -B"; python3 /t/pc_t1.py
echo "== strace (T1 situation, delivery build)"; strace -f -qq -yy -s 256 -o /out/strace3.log -e trace=%file,%network,%process,%signal,%ipc,close,read,write,pwrite64,writev,pwritev,pwritev2,ftruncate,fallocate,mmap,ptrace,process_vm_readv,process_vm_writev,ioprio_set,setpriority,sync,fsync,fdatasync,syncfs python3 -I -S -B /t/pc_dist.py > /dev/null; python3 /t/strace_check.py /out/strace3.log /nonexistent_out_dir | grep -E "violations|opened for writing|network|signals|process:|bytes read"
kill $SIM 2>/dev/null; sleep 1
echo "== T5: a different build in place of the store library (OBS wheel 78566d4, other options), stripped"
cp /obs/mooncake/libmooncake_store.so /tmp/obs.so && strip --strip-unneeded /tmp/obs.so && put /tmp/obs.so && rm /tmp/obs.so
python3 -I -S -B /t/pc_t1.py; echo "exit=$?"
echo "== T6: the check build compiled at a source path 1 character longer (test build: site values = that file)"
put /p/p1.so; python3 -I -S -B /t/pc_p1.py | grep -E "^mc_build|^mc_exports|^consumers|^mc_pre"; echo "exit=$?"
echo "== T7: the check build compiled at a source path 13 characters longer (test build: site values = that file)"
put /p/p13.so; python3 -I -S -B /t/pc_p13.py | grep -E "^mc_build|^mc_exports|^consumers|^mc_pre"; echo "exit=$?"
echo "== T8: the replacement already in place (test build: site values = the replacement's)"
put /p/repl.so; python3 -I -S -B /t/pc_repl.py | grep -E "^mc_build|^mc_exports|^mc_diff|^consumers|^mc_pre"; echo "exit=$?"
echo "== T9: the lab file with only its .rodata size raised by 8 bytes in the section header (test build: site values = that file)"
put /p/ro8.so; python3 -I -S -B /t/pc_ro8.py | grep -E "^mc_build|^mc_pre"; echo "exit=$?"
echo "== T10: the lab file with two different path prefixes of the same length (test build: site values = that file)"
put /p/pfx2.so; python3 -I -S -B /t/pc_pfx2.py | grep -E "^mc_build|^mc_pre"; echo "exit=$?"
put $ORIG
