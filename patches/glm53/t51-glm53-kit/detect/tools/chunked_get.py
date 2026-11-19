#!/usr/bin/env python3
"""Download a file in 1 MiB HTTP range chunks (this lab network truncates long responses).
Usage: chunked_get.py URL OUT [SHA256]"""
import hashlib
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
from wheel_record import fetch  # noqa: E402

url, out = sys.argv[1:3]
want = sys.argv[3] if len(sys.argv) > 3 else None
_, size = fetch(url, 0, 0)
step = 1 << 20
with open(out + ".part", "wb") as f:
    for start in range(0, size, step):
        end = min(size, start + step) - 1
        for _ in range(5):
            data, _ = fetch(url, start, end)
            if len(data) == end - start + 1:
                break
        else:
            raise SystemExit("short chunk at %d" % start)
        f.write(data)
digest = hashlib.sha256(open(out + ".part", "rb").read()).hexdigest()
if want and digest != want:
    raise SystemExit("sha256 mismatch %s" % digest)
os.replace(out + ".part", out)
print(out, size, digest[:16])
