#!/usr/bin/env python3
"""Fetch and inflate only the first part of one wheel member (HTTP range). Usage: URL MEMBER OUT COMPRESSED_BYTES"""
import os
import struct
import sys
import zlib

sys.path.insert(0, os.path.dirname(__file__))
from wheel_record import fetch  # noqa: E402

url, member, out, want = sys.argv[1], sys.argv[2], sys.argv[3], int(sys.argv[4])
_, size = fetch(url, 0, 0)
tail, _ = fetch(url, size - (1 << 20), size - 1)
eocd = tail.rfind(b"PK\x05\x06")
_, _, _, _, count, cd_size, cd_off, _ = struct.unpack("<IHHHHIIH", tail[eocd : eocd + 22])
cd, _ = fetch(url, cd_off, cd_off + cd_size - 1)
p = 0
for _ in range(count):
    f = struct.unpack("<IHHHHHHIIIHHHHHII", cd[p : p + 46])
    method, csize, nlen, xlen, clen, off = f[4], f[8], f[10], f[11], f[12], f[16]
    name = cd[p + 46 : p + 46 + nlen].decode()
    p += 46 + nlen + xlen + clen
    if name == member:
        head, _ = fetch(url, off, off + 29)
        ln, lx = struct.unpack("<HH", head[26:30])
        start = off + 30 + ln + lx
        d = zlib.decompressobj(-15)
        data = b""
        step = 1 << 20
        for s in range(start, start + min(want, csize), step):
            chunk, _ = fetch(url, s, min(s + step, start + csize) - 1)
            data += d.decompress(chunk)
        open(out, "wb").write(data)
        print(out, len(data), "bytes inflated from", min(want, csize))
        break
