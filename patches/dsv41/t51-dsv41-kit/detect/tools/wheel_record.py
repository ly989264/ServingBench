#!/usr/bin/env python3
"""Fetch a wheel's .dist-info/RECORD with HTTP range requests (central directory + one member).

Usage: wheel_record.py URL OUT.json
Writes {"url", "size", "record": {path: sha256_hex}, "sizes": {path: bytes}}.
Nothing from the wheel is executed; only the zip directory and RECORD text are read.
"""

import base64
import json
import struct
import sys
import urllib.request
import zlib


def fetch(url, start, end=None):
    rng = "bytes=%d-%s" % (start, "" if end is None else end)
    req = urllib.request.Request(url, headers={"Range": rng, "User-Agent": "wheel-record/1"})
    for attempt in range(8):
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                data = r.read()
                total = r.headers.get("Content-Range", "").rsplit("/", 1)[-1]
                return data, int(total) if total.isdigit() else None
        except Exception:
            if attempt == 7:
                raise
    raise RuntimeError("unreachable")


def main():
    url, out = sys.argv[1:3]
    _, size = fetch(url, 0, 0)
    tail_len = min(size, 1 << 20)
    tail, _ = fetch(url, size - tail_len, size - 1)
    eocd = tail.rfind(b"PK\x05\x06")
    if eocd < 0:
        raise SystemExit("no end of central directory")
    _, _, _, _, count, cd_size, cd_off, _ = struct.unpack("<IHHHHIIH", tail[eocd : eocd + 22])
    if cd_off < size - tail_len:
        cd, _ = fetch(url, cd_off, cd_off + cd_size - 1)
    else:
        cd = tail[cd_off - (size - tail_len) : cd_off - (size - tail_len) + cd_size]
    entries = {}
    p = 0
    for _ in range(count):
        sig, _, _, _, method, _, _, crc, csize, usize, nlen, xlen, clen, _, _, _, off = struct.unpack(
            "<IHHHHHHIIIHHHHHII", cd[p : p + 46]
        )
        name = cd[p + 46 : p + 46 + nlen].decode("utf-8")
        entries[name] = (method, csize, usize, off, crc)
        p += 46 + nlen + xlen + clen
    rec_name = [n for n in entries if n.endswith(".dist-info/RECORD")][0]
    method, csize, usize, off, crc = entries[rec_name]
    head, _ = fetch(url, off, off + 29)
    nlen, xlen = struct.unpack("<HH", head[26:30])
    data, _ = fetch(url, off + 30 + nlen + xlen, off + 30 + nlen + xlen + csize - 1)
    text = zlib.decompress(data, -15) if method == 8 else data
    if zlib.crc32(text) & 0xFFFFFFFF != crc:
        raise SystemExit("RECORD crc mismatch")
    record, sizes = {}, {}
    for line in text.decode("utf-8").splitlines():
        parts = line.rsplit(",", 2)
        if len(parts) == 3 and parts[1].startswith("sha256="):
            b64 = parts[1][len("sha256=") :]
            record[parts[0]] = base64.urlsafe_b64decode(b64 + "=" * (-len(b64) % 4)).hex()
            sizes[parts[0]] = int(parts[2]) if parts[2].isdigit() else None
    with open(out, "w") as f:
        json.dump({"url": url, "size": size, "record": record, "sizes": sizes}, f, indent=1, sort_keys=True)
    print(out, size, len(record), "files")


if __name__ == "__main__":
    main()
