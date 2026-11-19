#!/usr/bin/env python3
"""Embed t51_expected.json into a single-file copy of the check script."""

import base64
import gzip
import hashlib
import sys

src, expected, out = sys.argv[1:4]
with open(src) as f:
    text = f.read()
with open(expected, "rb") as f:
    blob = base64.b64encode(gzip.compress(f.read(), mtime=0)).decode("ascii")
marker = "EXPECTED_B64 = None  # filled in by build.py; otherwise t51_expected.json next to this file is used"
assert text.count(marker) == 1
chunks = [blob[i : i + 100] for i in range(0, len(blob), 100)]
literal = "EXPECTED_B64 = (\n" + "\n".join('    "%s"' % c for c in chunks) + "\n)"
text = text.replace(marker, literal)
with open(out, "w") as f:
    f.write(text)
print(out, hashlib.sha256(text.encode()).hexdigest())
