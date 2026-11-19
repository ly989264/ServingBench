#!/bin/sh
# Inside a container: git apply --check on a clean d4ad49796 tree, apply, compare with the replacement file,
# then replay the patch on a copy with simulated local edits outside the hunks.
# Mounts: /in/d4ad49796.tar (git archive of d4ad49796, prefix vllm-ascend/), /p (patches/vllm-ascend-p2d),
#         /out (replayed files for the follow-up test run).
set -e
F=vllm_ascend/distributed/kv_transfer/kv_p2p/mooncake_connector.py
PATCH=/p/t51-vllm-ascend-d4ad49796-p2d-diag.patch
mkdir -p /t && tar -xf /in/d4ad49796.tar -C /t && cd /t/vllm-ascend
git init -q && git add -A && git -c user.name=t -c user.email=t@example.invalid commit -qm d4ad49796
echo "== before: $(sha256sum $F | cut -c1-64)"
git apply --check "$PATCH" && echo "== git apply --check: OK"
git apply "$PATCH"
echo "== after:  $(sha256sum $F | cut -c1-64)"
cmp "$F" "/p/files/$F" && echo "== applied file == replacement file"
git diff --stat | tail -1
git checkout -q -- "$F"

edit() {
  python3 -I /tools/simulate_site_edits.py "$F" "$F" "$1"
}
for mode in A B C; do
  git checkout -q -- "$F"
  edit $mode
  echo "== replay, scenario $mode: $(git diff --shortstat)"
  if git apply --check "$PATCH" 2>/tmp/ga.err; then echo "   git apply --check: OK"; else echo "   git apply --check: FAILED ($(head -1 /tmp/ga.err))"; fi
  out=$(patch -p1 --dry-run < "$PATCH" 2>&1 || true)
  failed=$(printf '%s\n' "$out" | grep -c '^Hunk #.*FAILED' || true)
  echo "   patch -p1 (fuzz 2): $(printf '%s\n' "$out" | grep -c '^Hunk #.* succeeded' || true) hunks with offset/fuzz, $failed failed"
  printf '%s\n' "$out" | grep -E 'FAILED' | sed 's/^/     /' || true
  cp "$F" /tmp/edited_$mode.py
  cp "$F" /out/edited_$mode.py
  if python3 -I /p/replay_p2d.py /tmp/edited_$mode.py --out /out/replayed_$mode.py; then
    if [ "$mode" = A ]; then
      git apply "$PATCH" && cmp "$F" /out/replayed_$mode.py && echo "   replay result == git apply result on the same edited file"
    fi
  fi
done
