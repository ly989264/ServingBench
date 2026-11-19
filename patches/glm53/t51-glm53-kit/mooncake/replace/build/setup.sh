#!/bin/bash
# inside quay.io/ascend/cann:9.2.0-a3-openeuler24.03-py3.11: development packages for building Mooncake's store library
# repositories: the TUNA mirror of openEuler 24.03 LTS SP4 (the default metalink is ~100x slower from the lab)
set -u
mkdir -p /etc/yum.repos.d/orig && mv /etc/yum.repos.d/*.repo /etc/yum.repos.d/orig/ 2>/dev/null
B=https://mirrors.tuna.tsinghua.edu.cn/openeuler/openEuler-24.03-LTS-SP4
cat > /etc/yum.repos.d/tuna.repo <<R
[OS]
name=OS
baseurl=$B/OS/aarch64/
enabled=1
gpgcheck=0
[everything]
name=everything
baseurl=$B/everything/aarch64/
enabled=1
gpgcheck=0
[EPOL]
name=EPOL
baseurl=$B/EPOL/main/aarch64/
enabled=1
gpgcheck=0
R
PKGS="gcc-c++ make ninja-build git pkgconf patchelf rdma-core-devel glog-devel gflags-devel jsoncpp-devel libunwind-devel numactl-devel boost-devel openssl-devel yaml-cpp-devel libcurl-devel liburing-devel msgpack-devel zstd-devel asio-devel xxhash-devel"
dnf -y --setopt=install_weak_deps=False --setopt=retries=20 --setopt=timeout=120 --skip-broken install $PKGS > /out/dnf.log 2>&1
echo "dnf rc=$?"
for p in $PKGS; do rpm -q $p >/dev/null 2>&1 && echo "have $p $(rpm -q --qf '%{VERSION}' $p)" || echo "MISSING $p"; done
