#!/usr/bin/env bash
# A patched copy of the vLLM v0.30.0 CPU tree for one t51-glm53-vllm commit:
#   WORK=<dir> VLLM_REPO=<t51-glm53-vllm checkout> bash make_patched_tree.sh <commit>   -> $WORK/site/vllm030cpu_<commit>
# The patch and SHA256SUMS.after come from the commit (git show); hard links first, then git apply (which writes each
# patched file anew, so the original tree stays untouched); every patched file is checked against SHA256SUMS.after.
set -euo pipefail
: "${WORK:?}" "${VLLM_REPO:?}"
C=${1:?commit}
git -C "$VLLM_REPO" show "$C:patches/vllm/t51-vllm-v0.30.0-pd-diag.patch" > "$WORK/$C.patch"
git -C "$VLLM_REPO" show "$C:patches/vllm/SHA256SUMS.after" > "$WORK/$C.SHA256SUMS.after"
echo "patch $C sha256 $(shasum -a 256 "$WORK/$C.patch" | cut -c1-64)"
rm -rf "$WORK/site/vllm030cpu_$C"
cp -al "$WORK/site/vllm030cpu" "$WORK/site/vllm030cpu_$C"
(cd "$WORK/site/vllm030cpu_$C" && git apply --include='vllm/*' "$WORK/$C.patch")
(cd "$WORK/site/vllm030cpu_$C" && shasum -a 256 -c "$WORK/$C.SHA256SUMS.after" | grep -v ': OK$' && exit 1 || true)
echo "patched files: $(wc -l < "$WORK/$C.SHA256SUMS.after" | tr -d ' ') match SHA256SUMS.after of $C"
n=$({ grep -rl pd_diagnostics "$WORK/site/vllm030cpu/vllm" || true; } | wc -l | tr -d ' ')
[ "$n" = 0 ] || { echo "original tree modified ($n files mention pd_diagnostics)"; exit 1; }
