#!/usr/bin/env bash
# Pass 1 (Python 3.12.3 and torch 2.13.0+cpu of t51g-vllmtest:v1): install the vLLM v0.30.0 CPU wheel and the
# transformers 5.14.1 set into mounted dirs, make a patched copy (hard links, then the vLLM patch of one t51-glm53-vllm
# commit), generate the tiny model. Usage:
#   WORK=<dir> VLLM_REPO=<t51-glm53-vllm checkout> VLLM_COMMIT=db27fd1 bash setup_pass1.sh
# The patch and its SHA256SUMS.after are taken from the commit (git show), not from the working tree.
# transformers 5.14.1 needs tokenizers >=0.22.0,<=0.23.0 and huggingface-hub >=1.5.0,<2.0 (the image has 0.23.3 and
# 2.4.0; vLLM v0.30.0 needs huggingface_hub >=1.31.0): the newest matching tokenizers and huggingface-hub go into the
# same dir. Every step checks the free space first and stops below 30.5 GB.
set -euo pipefail
: "${WORK:?}" "${VLLM_REPO:?}" "${VLLM_COMMIT:?}"
E2E="$(cd "$(dirname "$0")" && pwd)"
WHEEL=vllm-0.30.0+cpu-cp38-abi3-manylinux_2_39_aarch64.whl
WHEEL_SHA256=0423f802029362ca31940262c580ed3f5af9d10bd123106a47f0fde6296f3c47
IMAGE=${IMAGE:-t51g-vllmtest:v1}
free() { df -k / | tail -1 | awk '{printf "%.2f", $4*1024/1e9}'; }
guard() { f=$(free); echo "free: $f GB ($1)"; awk -v f="$f" 'BEGIN { exit !(f < 30.5) }' && { echo "STOP: below 30.5 GB"; exit 2; }; true; }
guard start
mkdir -p "$WORK/dl" "$WORK/site"
[ -f "$WORK/dl/$WHEEL" ] || curl -sSL -C - -o "$WORK/dl/$WHEEL" "https://github.com/vllm-project/vllm/releases/download/v0.30.0/vllm-0.30.0%2Bcpu-cp38-abi3-manylinux_2_39_aarch64.whl"
echo "$WHEEL_SHA256  $WORK/dl/$WHEEL" | shasum -a 256 -c -
guard downloaded
PIP="/opt/venv/bin/pip install -q --no-deps --no-cache-dir"
MIRROR="-i https://pypi.tuna.tsinghua.edu.cn/simple"
docker run --rm -v "$WORK:/w" "$IMAGE" sh -c "
  set -e
  [ -d /w/site/vllm030cpu/vllm ] || $PIP --target /w/site/vllm030cpu /w/dl/$WHEEL
  for req in transformers==5.14.1 'tokenizers>=0.22.0,<=0.23.0' 'huggingface-hub>=1.31.0,<2.0'; do
    $PIP --target /w/site/tf5141 \"\$req\" || $PIP $MIRROR --target /w/site/tf5141 \"\$req\"
  done
"
guard installed
git -C "$VLLM_REPO" show "$VLLM_COMMIT:patches/vllm/t51-vllm-v0.30.0-pd-diag.patch" > "$WORK/$VLLM_COMMIT.patch"
git -C "$VLLM_REPO" show "$VLLM_COMMIT:patches/vllm/SHA256SUMS.after" > "$WORK/$VLLM_COMMIT.SHA256SUMS.after"
echo "patch $VLLM_COMMIT sha256 $(shasum -a 256 "$WORK/$VLLM_COMMIT.patch" | cut -c1-64)"
rm -rf "$WORK/site/vllm030cpu_p"
cp -al "$WORK/site/vllm030cpu" "$WORK/site/vllm030cpu_p"
# git apply writes each patched file anew, so the hard-linked originals stay untouched (checked below)
(cd "$WORK/site/vllm030cpu_p" && git apply --include='vllm/*' "$WORK/$VLLM_COMMIT.patch")
(cd "$WORK/site/vllm030cpu_p" && shasum -a 256 -c "$WORK/$VLLM_COMMIT.SHA256SUMS.after" | grep -v ': OK$' && exit 1 || true)
echo "patched files: $(wc -l < "$WORK/$VLLM_COMMIT.SHA256SUMS.after") match SHA256SUMS.after of $VLLM_COMMIT"
n=$({ grep -rl pd_diagnostics "$WORK/site/vllm030cpu/vllm" || true; } | wc -l | tr -d ' ')
[ "$n" = 0 ] || { echo "original tree modified ($n files mention pd_diagnostics)"; exit 1; }
docker run --rm --network none -v "$WORK:/w" -e PYTHONPATH=/w/site/tf5141:/w/site/vllm030cpu "$IMAGE" python -c \
  "import sys, torch, transformers, tokenizers, huggingface_hub, vllm; print('versions', sys.version.split()[0], torch.__version__, transformers.__version__, tokenizers.__version__, huggingface_hub.__version__, vllm.__version__)"
[ -f "$WORK/model/config.json" ] || docker run --rm --network none -v "$WORK:/w" -v "$E2E:/e2e:ro" -e PYTHONPATH=/w/site/tf5141 "$IMAGE" \
  python /e2e/make_tiny_model.py /w/model
du -sh "$WORK/site/vllm030cpu" "$WORK/site/vllm030cpu_p" "$WORK/site/tf5141" "$WORK/model" "$WORK/dl"
guard done
