#!/bin/bash
# Read-only personal-data scan of the Mooncake incremental package: text files, the replacement library's printable
# strings, and the precheck's embedded data (base64 + gzip, decoded without running the script). The scanner and the
# decoder are the patch kit's (copied unchanged). Local names (this account, its home directory) come from the
# environment at run time, so they are never written into the package or this script.
# Usage: scan_pkg.sh TAR OUTDIR
set -u
TAR=$1; OUT=$2; HERE=$(cd "$(dirname "$0")" && pwd); PY=${PY:-/opt/homebrew/bin/python3.12}
rm -rf "$OUT"; mkdir -p "$OUT/x" "$OUT/strings" "$OUT/embedded" "$OUT/text"
tar -xf "$TAR" -C "$OUT/x"
echo "== SHA256SUMS"; (cd "$OUT/x" && shasum -a 256 -c SHA256SUMS | grep -vc ': OK$' | sed 's/^/not OK: /'; echo "files listed: $(wc -l < SHA256SUMS)")
echo "== .git / .DS_Store / __pycache__ / AppleDouble"; find "$OUT/x" \( -name .git -o -name .DS_Store -o -name __pycache__ -o -name '._*' \) | wc -l
echo "== text files (all files except the library, which is scanned below by its strings)"
mkdir -p "$OUT/text"; (cd "$OUT/x" && find . -type f ! -path ./lib/libmooncake_store.so -exec sh -c 'mkdir -p "$1/$(dirname "$2")" && cp "$2" "$1/$2"' _ "$OUT/text" {} \;)
"$PY" -I "$HERE/scan_pii.py" "$OUT/text"
echo "== printable strings of lib/libmooncake_store.so (length >= 6)"
# strings longer than 4096 characters are cut into 4096-character pieces overlapping by 256 (the scanner's patterns are
# short; one run of 2.1 MB is patchelf's filler, all "X", which the e-mail pattern would scan quadratically)
strings -a -n 6 "$OUT/x/lib/libmooncake_store.so" | "$PY" -I -c '
import sys
n = long = filler = 0
for line in sys.stdin:
    line = line.rstrip("\n"); n += 1
    if len(line) <= 4096:
        print(line); continue
    long += 1; filler += set(line) == {"X"}
    for i in range(0, len(line), 4096 - 256):
        print(line[i:i + 4096])
print("strings %d, longer than 4096: %d (of them all X: %d)" % (n, long, filler), file=sys.stderr)
' > "$OUT/strings/libmooncake_store.strings.txt"
"$PY" -I "$HERE/scan_pii.py" "$OUT/strings"
echo "== precheck embedded data"; "$PY" -I "$HERE/decode_embedded.py" "$OUT/x/precheck/dist/t51_mc_precheck.py" "$OUT/embedded"; "$PY" -I "$HERE/scan_pii.py" "$OUT/embedded"
echo "== local names (this account, home directory, work directories)"
k=0
for n in "$(id -un)" "$(basename "$HOME")" Desktop scratchpad /private/tmp claude-501; do
  c=$(grep -rIl -F -- "$n" "$OUT/x" "$OUT/strings" "$OUT/embedded" 2>/dev/null | wc -l); b=$(grep -l -a -F -- "$n" "$OUT/x/lib/libmooncake_store.so" | wc -l)
  echo "name #$((++k)): files with it $c, in the binary $b"
done
