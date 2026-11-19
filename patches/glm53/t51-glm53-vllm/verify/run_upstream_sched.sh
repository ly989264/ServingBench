#!/bin/bash
# Run the unmodified upstream tests/v1/core/test_scheduler.py of the same version
# against a patched install: does the patch change any scheduler behaviour?
# usage: run_upstream_sched.sh v030port|v032p
set -u
V=$1; OUT=/out/upstream_$V; mkdir -p $OUT
export PATH=/opt/venv/bin:$PATH
mkdir -p /w/$V && cd /w/$V && tar -xf /src/$V.tar
case $V in v030port) VER=0.30.0; U=/src/upstream_test_scheduler_v030.py ;; v032p) VER=0.32.0; U=/src/upstream_test_scheduler_v032.py ;; esac
SETUPTOOLS_SCM_PRETEND_VERSION=$VER VLLM_TARGET_DEVICE=empty pip install --no-deps --no-build-isolation -e . > $OUT/install.txt 2>&1
cp $U tests/v1/core/test_scheduler_upstream.py
HF_HUB_OFFLINE=1 VLLM_TARGET_DEVICE=cpu PYTHONPATH=/src/plug timeout 1500 python -m pytest -p t51_cpu_env -p no:cacheprovider -q -rA --timeout=300 --junitxml=$OUT/upstream.xml tests/v1/core/test_scheduler_upstream.py > $OUT/upstream.log 2>&1; echo "rc=$?" >> $OUT/upstream.log
