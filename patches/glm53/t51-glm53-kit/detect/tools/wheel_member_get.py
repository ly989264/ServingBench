#!/usr/bin/env python3
"""Fetch one whole wheel member in 4 MiB range chunks, inflate to a file, check its CRC.
Usage: wheel_member_get.py URL MEMBER OUT"""
import os
import struct
import sys
import zlib

sys.path.insert(0, os.path.dirname(__file__))
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
    method, crc, csize, usize, nlen, xlen, clen, off = f[4], f[7], f[8], f[9], f[10], f[11], f[12], f[16]
    name = cd[p + 46 : p + 46 + nlen].decode()
    p += 46 + nlen + xlen + clen
    if name != member:
        continue
    head, _ = fetch(url, off, off + 29)
    ln, lx = struct.unpack("<HH", head[26:30])
    start = off + 30 + ln + lx
    d = zlib.decompressobj(-15) if method == 8 else None
    got, check, step = 0, 0, 4 << 20
    with open(out + ".part", "wb") as fo:
        for s in range(start, start + csize, step):
            e = min(s + step, start + csize) - 1
            for _ in range(6):
                chunk, _ = fetch(url, s, e)
                if len(chunk) == e - s + 1:
                    break
            else:
                raise SystemExit("short chunk at %d" % s)
            data = d.decompress(chunk) if d else chunk
            check = zlib.crc32(data, check)
            got += len(data)
            fo.write(data)
        if d:
            rest = d.flush()
            check = zlib.crc32(rest, check)
            got += len(rest)
            fo.write(rest)
    if (check & 0xFFFFFFFF) != crc or got != usize:
        raise SystemExit("crc/size mismatch")
    os.replace(out + ".part", out)
    print(out, got)
    break
else:
    raise SystemExit("member not found")
