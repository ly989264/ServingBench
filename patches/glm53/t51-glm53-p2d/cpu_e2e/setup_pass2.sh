#!/usr/bin/env bash
# Pass 2 (Python 3.11.10): a CPython 3.11.10 from python-build-standalone (20241016, aarch64, install_only) mounted at
# /opt/py311 in t51g-vllmtest:v1 (Ubuntu 24.04, glibc 2.39), with torch 2.13.0+cpu and the torchvision / torchaudio /
# torchcodec +cpu builds of pass 1's image (cp311), the dependencies of the
# vLLM v0.30.0 CPU wheel under cp311 and the transformers 5.14.1 set installed into it. vLLM itself is not in the env:
# the vllm030cpu / vllm030cpu_p trees of pass 1 are reused (the wheel is cp38-abi3), so run setup_pass1.sh first.
#   WORK=<dir> bash setup_pass2.sh        -> env in $WORK/py311env (mount it at /opt/py311); downloaded wheels in
#   $WORK/wheelhouse311 (can be deleted afterwards)
# Every step checks the free space first and stops below 30.5 GB.
set -euo pipefail
: "${WORK:?}"
IMAGE=${IMAGE:-t51g-vllmtest:v1}
ENV=$WORK/py311env
PBS=cpython-3.11.10+20241016-aarch64-unknown-linux-gnu-install_only.tar.gz
WHEEL=vllm-0.30.0+cpu-cp38-abi3-manylinux_2_39_aarch64.whl
free() { df -k / | tail -1 | awk '{printf "%.2f", $4*1024/1e9}'; }
guard() { f=$(free); echo "free: $f GB ($1)"; awk -v f="$f" 'BEGIN { exit !(f < 30.5) }' && { echo "STOP: below 30.5 GB"; exit 2; }; true; }
guard start
mkdir -p "$WORK/dl/pbs"
if [ ! -f "$WORK/dl/pbs/$PBS" ]; then
  (cd "$WORK/dl/pbs" && gh release download 20241016 -R astral-sh/python-build-standalone -p "$PBS" -p SHA256SUMS --clobber)
fi
(cd "$WORK/dl/pbs" && grep " $PBS\$\|  $PBS\$" SHA256SUMS | shasum -a 256 -c -)
[ -f "$WORK/dl/$WHEEL" ] || { echo "run setup_pass1.sh first (vLLM wheel and trees)"; exit 1; }
rm -rf "$ENV"; mkdir -p "$ENV"
tar -xzf "$WORK/dl/pbs/$PBS" -C "$ENV" --strip-components=1
guard python
RUN() { docker run --rm -v "$WORK:/w" -v "$ENV:/opt/py311" "$IMAGE" sh -c "$1"; }
PIP="/opt/py311/bin/python3.11 -m pip install -q --no-cache-dir --disable-pip-version-check"
MIRROR="-i https://pypi.tuna.tsinghua.edu.cn/simple"
RUN "/opt/py311/bin/python3.11 -c 'import sys, ssl, sqlite3, ctypes, lzma, bz2; print(sys.version)'"
# Downloads are fetched with pip download (hash-checked, kept only when they match) and retried: plain downloads from
# PyPI and the PyTorch index arrived corrupted now and then here. The install then runs offline from the wheelhouse.
WH=/w/wheelhouse311
PINS="transformers==5.14.1 'tokenizers>=0.22.0,<=0.23.0' 'huggingface-hub>=1.31.0,<2.0'"
DL="/opt/py311/bin/python3.11 -m pip download -q --disable-pip-version-check -d $WH"
# the torch family as in pass 1's image (t51g-vllmtest:v1 has these +cpu builds from the PyTorch CPU index); the PyPI
# torchvision 0.28.0 does not load with torch 2.13.0+cpu ("operator torchvision::nms does not exist")
TORCH="torch==2.13.0+cpu torchvision==0.28.0+cpu torchaudio==2.11.1+cpu torchcodec==0.17.0+cpu"
RUN "for i in 1 2 3 4 5 6; do $DL --index-url https://download.pytorch.org/whl/cpu torch==2.13.0+cpu && $DL --no-deps --index-url https://download.pytorch.org/whl/cpu $TORCH && exit 0; echo retry \$i; done; exit 1"
guard torch-downloaded
# vLLM's own requirements under cp311, resolved by pip from the wheel (torch from the wheelhouse); PyPI and the tuna
# mirror alternate between attempts
RUN "for i in 1 2 3 4 5 6; do if [ \$((i % 2)) = 1 ]; then idx=; else idx='$MIRROR'; fi; $DL \$idx --find-links $WH /w/dl/$WHEEL $PINS && exit 0; echo retry \$i; done; exit 1"
guard deps-downloaded
RUN "$PIP --no-index --find-links $WH $TORCH /w/dl/$WHEEL $PINS"
RUN "/opt/py311/bin/python3.11 -m pip uninstall -q -y vllm && /opt/py311/bin/python3.11 -m pip check; /opt/py311/bin/python3.11 -m pip freeze > /opt/py311/.pip-freeze.txt"
guard deps
docker run --rm --network none -v "$WORK:/w" -v "$ENV:/opt/py311:ro" -e PYTHONPATH=/w/site/vllm030cpu "$IMAGE" \
  /opt/py311/bin/python3.11 -c "import sys, torch, torchvision, torchaudio, transformers, tokenizers, huggingface_hub, vllm; print('versions', sys.version.split()[0], torch.__version__, torchvision.__version__, torchaudio.__version__, transformers.__version__, tokenizers.__version__, huggingface_hub.__version__, vllm.__version__, vllm.__file__)"
du -sh "$ENV"
guard done
