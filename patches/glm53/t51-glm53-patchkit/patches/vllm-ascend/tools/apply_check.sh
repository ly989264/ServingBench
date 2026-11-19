# Applies the vllm-ascend package to a clean d4ad49796 checkout and checks it against files/ and the checksum lists.
# Runs in python:3 (has git). Mounts: /base (git checkout of vllm-ascend d4ad49796, ro), /pkg (patches/vllm-ascend, ro).
set -e
git config --global --add safe.directory '*'
cp -r /base /w && cd /w
echo "base $(git rev-parse --short=9 HEAD), dirty files: $(git status --porcelain | wc -l)"
COMBINED=/pkg/t51-vllm-ascend-d4ad49796-pd-diag.patch
git apply --check "$COMBINED" && echo "combined patch ($(grep -c '^diff --git' "$COMBINED") files): git apply --check OK"
n=0
for p in /pkg/per-file/*.patch; do git apply --check "$p"; n=$((n + 1)); done
echo "per-file patches ($n): each git apply --check OK on clean base"
for p in /pkg/optional/*.patch; do git apply --check "$p"; done && echo "optional: git apply --check OK"
sha256sum --quiet -c /pkg/SHA256SUMS.before && echo "SHA256SUMS.before OK on clean base: $(wc -l < /pkg/SHA256SUMS.before)"
while read -r f; do [ ! -e "$f" ] || { echo "new file already in base: $f"; exit 1; }; done < /pkg/NEW_FILES.txt
echo "NEW_FILES.txt: $(wc -l < /pkg/NEW_FILES.txt) files, none present in base"
git apply "$COMBINED"
sha256sum --quiet -c /pkg/SHA256SUMS.after && echo "SHA256SUMS.after OK: $(wc -l < /pkg/SHA256SUMS.after)"
for f in $(cut -d' ' -f3 /pkg/SHA256SUMS.after); do cmp "$f" "/pkg/files/$f"; done
echo "applied tree == files/ ($(find /pkg/files -type f | wc -l) files)"
before=$(cut -d' ' -f3 /pkg/SHA256SUMS.before | sort); new=$(sort /pkg/NEW_FILES.txt); after=$(cut -d' ' -f3 /pkg/SHA256SUMS.after | sort)
[ "$(printf '%s\n%s\n' "$before" "$new" | sort)" = "$after" ] && echo "SHA256SUMS.after = SHA256SUMS.before + NEW_FILES.txt"
# each per-file patch alone, then all source per-file patches together, gives the same files
for p in /pkg/per-file/[0-9][0-9]-*.patch; do
  case $p in */90-tests.patch) continue ;; esac
  rm -rf /w1 && cp -r /base /w1 && cd /w1 && git apply "$p"
done
rm -rf /w2 && cp -r /base /w2 && cd /w2
for p in /pkg/per-file/[0-8][0-9]-*.patch; do git apply "$p"; done
for f in $(cut -d' ' -f3 /pkg/SHA256SUMS.after); do cmp "$f" "/pkg/files/$f"; done
echo "per-file source patches applied in order == files/"
