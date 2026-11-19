#!/bin/bash
# usage: run_equiv.sh <variant>   (v030port | v032p)
set -u
V=$1; OUT=/out/equiv; mkdir -p $OUT
export PATH=/opt/venv/bin:$PATH
mkdir -p /w/$V && cd /w/$V
if [ $V = v030port ]; then tar -xf /src/v030port.tar; VER=0.30.0; else tar -xf /src/v032p.tar; VER=0.32.0; fi
SETUPTOOLS_SCM_PRETEND_VERSION=$VER VLLM_TARGET_DEVICE=empty pip install --no-deps --no-build-isolation -e . > $OUT/$V.install.txt 2>&1
[ $V = v030port ] && pip install -q transformers==5.14.1 >> $OUT/$V.install.txt 2>&1
pip list 2>/dev/null | grep -E "^(vllm|transformers|torch) " >> $OUT/$V.install.txt
PYTHONHASHSEED=0 HF_HUB_OFFLINE=1 VLLM_TARGET_DEVICE=cpu timeout 600 python /src/equiv/pd_equiv.py $OUT/$V.jsonl > $OUT/$V.log 2>&1; echo "rc=$?" >> $OUT/$V.log
