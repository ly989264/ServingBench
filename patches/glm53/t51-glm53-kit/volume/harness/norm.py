"""Site-like widths for record sizes (same rules as the volume harness; t51-glm53-p2d's assumptions)."""
import json, re, uuid
HOST20 = "d-node-0123456789abc"
RUN32 = "0123456789abcdef0123456789abcdef"
ID_RE = re.compile(r"^([a-z_]+(?:-[a-z_]+)*)-([0-9a-f]{32})-(\d+)-(\d+)$")
REQ_RE = re.compile(r"^(chat)?cmpl-req-[A-Za-z0-9_]+(-[0-9a-f]+)?$")
CLOCK0 = 1_728_000_000_000_000
SITE_REQ = "chatcmpl-%s-%08x" % (uuid.UUID(int=7), 7)

def normalize(obj, key=None):
    if isinstance(obj, dict):
        return {k: normalize(v, k) for k, v in obj.items()}
    if isinstance(obj, list):
        return [normalize(v, key) for v in obj]
    if isinstance(obj, bool) or obj is None:
        return obj
    if key == "host" and isinstance(obj, str):
        return HOST20
    if key == "pid" and isinstance(obj, int):
        return 1234567
    if key == "native_tid" and isinstance(obj, int):
        return 2345678
    if key == "run_id" and isinstance(obj, str):
        return RUN32
    if isinstance(obj, str):
        m = ID_RE.match(obj)
        if m:
            return "%s-%s-1234567-%07d" % (m.group(1), RUN32, 1000000 + int(m.group(4)) % 1000000)
        if REQ_RE.match(obj):
            return SITE_REQ
        return obj
    if isinstance(obj, int) and key and key.endswith("_ns") and obj >= 10 ** 13:
        return CLOCK0 + obj % 10 ** 14
    return obj

def msg_bytes(rec):
    return len(("[PD_DIAG] " + json.dumps(normalize(rec), separators=(",", ":"))).encode())
