#!/usr/bin/env python3
"""Fetch one member of a remote wheel with HTTP range requests. Usage: wheel_member.py URL MEMBER OUT"""
import struct
import sys
import zlib

sys.path.insert(0, __import__("os").path.dirname(__file__))
from wheel_record import fetch  # noqa: E402

url, member, out = sys.argv[1:4]
_, size = fetch(url, 0, 0)
tail, _ = fetch(url, size - (1 << 20), size - 1)
eocd = tail.rfind(b"PK\x05\x06")
_, _, _, _, count, cd_size, cd_off, _ = struct.unpack("<IHHHHIIH", tail[eocd : eocd + 22])
cd, _ = fetch(url, cd_off, cd_off + cd_size - 1)
p = 0
for _ in range(count):
    f = struct.unpack("<IHHHHHHIIIHHHHHII", cd[p : p + 46])
    method, crc, csize, nlen, xlen, clen, off = f[4], f[7], f[8], f[10], f[11], f[12], f[16]
    name = cd[p + 46 : p + 46 + nlen].decode()
    p += 46 + nlen + xlen + clen
    if name == member:
        head, _ = fetch(url, off, off + 29)
        ln, lx = struct.unpack("<HH", head[26:30])
        data, _ = fetch(url, off + 30 + ln + lx, off + 30 + ln + lx + csize - 1)
        data = zlib.decompress(data, -15) if method == 8 else data
        assert zlib.crc32(data) & 0xFFFFFFFF == crc
        open(out, "wb").write(data)
        print(out, len(data))
        break
else:
    raise SystemExit("member not found")
