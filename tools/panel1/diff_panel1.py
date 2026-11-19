#!/usr/bin/env python3
"""Compare the panel-1 dashboards in your Grafana (面板一·P, 面板一·D) with the copy we rebuilt from
grafana_panels.txt, and write the differences to one short file that turns our copy into yours exactly.

Read-only: nothing in Grafana is changed. Python 3.6+, standard library only.

  python3 diff_panel1.py --url http://localhost:3000          # read both dashboards through the API
  python3 diff_panel1.py --files P.json D.json                 # or from exported JSON files
  python3 diff_panel1.py --apply panel1-diff.txt --out-dir D   # (our side) rebuild your dashboards from the file
  python3 diff_panel1.py --files P.json D.json --decisions-out decisions.txt
                                         # also write one line per site change, each marked 保留 (keep) or 丢弃 (drop)
  python3 diff_panel1.py --files P.json D.json --merge --decisions decisions.txt --out-dir NEW
                                         # (our side) the baseline plus the kept changes: the next baseline

Exit status: 0 = no differences, 1 = differences, 2 = error.
Per-cluster values are configuration, not differences: the dashboard uid, a "（<cluster>）" suffix on the title,
and the regex and current value of the datasource variable.

Result file format, one line per difference:
  <board>/<panel ids> <field path> = <your value as compact JSON>
  <board>/<panel ids> <field path> -          the field does not exist on your side
  <board>/<panel ids> <field path> += <n>     your number is ours plus n (same shift on many panels)
  <board>/<id> = @<id2>                        a panel only you have: start from our panel id2, then the lines below
  <board>/<ids> -                              panels only we have
  <board>/dash <field path> = ...              dashboard-level fields (variables, time, refresh, ...)
<board> is P or D, or * for both; <panel ids> is a list like 19-21,25, or * for every panel that is not a row.
Field paths use the short names in ABBR below; r is the row a panel sits in.
"""
import argparse
import base64
import copy
import getpass
import hashlib
import io
import json
import os
import re
import sys
import urllib.error
import urllib.request
import zlib

FORMAT = 1
UIDS = (("P", "vllm-pd-prefill"), ("D", "vllm-pd-decode"))
OUT_NAMES = {"P": "vllm-pd-prefill.json", "D": "vllm-pd-decode.json"}
# Fields left out of the comparison: Grafana changes them on every save or they differ per Grafana instance.
IGNORED_DASHBOARD_FIELDS = ("id", "version", "iteration")
ROW_KEY = "_row"  # pseudo field: id of the row a panel belongs to (None above the first row)

ABBR = {
    ROW_KEY: "r", "targets": "t", "expr": "e", "legendFormat": "lf", "refId": "ref", "datasource": "ds",
    "fieldConfig": "fc", "defaults": "d", "overrides": "ov", "custom": "c", "options": "o", "gridPos": "g",
    "title": "ti", "type": "ty", "thresholds": "th", "steps": "st", "color": "co", "mappings": "mp",
    "properties": "pr", "matcher": "m", "reduceOptions": "ro", "legend": "lg", "tooltip": "tt", "unit": "u",
    "decimals": "dc", "interval": "iv", "instant": "in", "range": "rn", "editorMode": "em", "format": "fm",
    "showPoints": "sp", "lineWidth": "lw", "fillOpacity": "fo", "drawStyle": "dr", "graphMode": "gm",
    "textMode": "tm", "colorMode": "cm", "collapsed": "cl", "templating": "tp", "list": "ls", "current": "cu",
    "query": "q", "value": "v", "fixedColor": "fx", "mode": "md", "displayMode": "dm", "placement": "pl",
    "calcs": "ca", "hideFrom": "hf", "lineStyle": "lst", "scheme": "sc", "showLegend": "sl", "maxHeight": "mh",
    "lineInterpolation": "lip", "gradientMode": "grm", "spanNulls": "sn", "axisSoftMin": "asm",
    "axisPlacement": "ap", "stacking": "sk", "thresholdsStyle": "ths", "justifyMode": "jm", "wideLayout": "wl",
    "showPercentChange": "spc", "text": "tx", "titleSize": "tsz", "valueSize": "vsz", "orientation": "or",
    "sort": "so", "values": "vs", "fields": "fl", "transparent": "tr", "content": "ct", "panels": "pn",
    "maxDataPoints": "mdp", "pluginVersion": "pv", "description": "de", "yAxis": "ya",
    "cellOptions": "cop", "filterValues": "fv", "rowsFrame": "rf", "cellGap": "cg", "exponent": "ex",
    "reverse": "rv", "fill": "fi", "dash": "da", "calculate": "clc", "exemplars": "exm",
    "cellValues": "cv", "showColorScale": "scs", "yHistogram": "yh", "axisLabel": "al", "noValue": "nv",
    "displayName": "dn", "pieType": "pt", "displayLabels": "dl", "minVizHeight": "mvh", "minVizWidth": "mvw",
    "showUnfilled": "su", "valueMode": "vm", "namePlacement": "np", "sizing": "sz", "barWidth": "bw",
    "xTickLabelRotation": "xr", "showValue": "sv", "mergeValues": "mv", "alignValue": "av", "rowHeight": "rh",
}
SHORT = {}
for _k, _v in ABBR.items():
    assert _v not in SHORT, _v
    SHORT[_v] = _k
BARE = re.compile(r"^[A-Za-z0-9_$@-]+$")
DEL = object()


class Fail(Exception):
    pass


# ---------------------------------------------------------------- the baseline we rebuilt
BASELINE_BLOB = """
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
t/8/VqtbFw==
"""


def baseline():
    return json.loads(zlib.decompress(base64.b64decode("".join(BASELINE_BLOB.split()))).decode("utf-8"))


# ---------------------------------------------------------------- normal form
def classic(data, label):
    """The dashboard inside an export or API response; refuse the resource format (apiVersion / spec)."""
    d = data.get("dashboard", data) if isinstance(data, dict) and "panels" not in data else data
    if not isinstance(d, dict) or "apiVersion" in d or "spec" in d or not isinstance(d.get("panels"), list):
        raise Fail("%s 不是经典格式的看板 JSON（顶层没有 panels 列表，或者有 apiVersion、spec）。"
                   "导出时请选「Classic」格式，或改用 --url 从 Grafana API 读" % label)
    return d


def yx(p):
    g = p.get("gridPos") or {}
    return (g.get("y", 0), g.get("x", 0))


CLUSTER_SUFFIX = re.compile(r"（[a-z0-9-]+）$")


def strip_config(top):
    """Drop the per-cluster values from the dashboard-level fields (see the module docstring)."""
    top.pop("uid", None)
    if isinstance(top.get("title"), str):
        top["title"] = CLUSTER_SUFFIX.sub("", top["title"])
    for v in (top.get("templating") or {}).get("list") or []:
        if isinstance(v, dict) and v.get("type") == "datasource":
            v.pop("regex", None)
            v.pop("current", None)


def put_config(d, ref):
    """An output board with the per-cluster values of the raw board `ref`, keys in the order of `ref`."""
    d = dict(d, uid=ref.get("uid"))
    m = CLUSTER_SUFFIX.search(ref.get("title") or "")
    if m and isinstance(d.get("title"), str):
        d["title"] += m.group(0)
    refvars = {v.get("name"): v for v in (ref.get("templating") or {}).get("list") or [] if isinstance(v, dict)}
    for v in (d.get("templating") or {}).get("list") or []:
        r = refvars.get(v.get("name")) if isinstance(v, dict) and v.get("type") == "datasource" else None
        for k in ("regex", "current"):
            if r is not None and k in r:
                v[k] = copy.deepcopy(r[k])
    order = [k for k in ref if k in d] + [k for k in d if k not in ref]
    return {k: d[k] for k in order}


def normalize(dash):
    """{'dash': fields other than panels, 'panels': {id: panel}}; row contents become the pseudo field _row."""
    dash = dash.get("dashboard", dash) if "panels" not in dash else dash
    top = {k: copy.deepcopy(v) for k, v in dash.items() if k != "panels" and k not in IGNORED_DASHBOARD_FIELDS}
    strip_config(top)
    panels, row = {}, None

    def put(p, row_id):
        if "id" not in p:
            raise Fail("a panel has no id: %s" % p.get("title"))
        if p["id"] in panels:
            raise Fail("panel id %s appears twice" % p["id"])
        q = {k: copy.deepcopy(v) for k, v in p.items() if k not in ("id", "panels")}
        if p.get("type") == "row":
            if "panels" in p:
                q["panels"] = []
        else:
            q[ROW_KEY] = row_id
        panels[p["id"]] = q

    for p in sorted(dash.get("panels", []), key=yx):
        if p.get("type") == "row":
            row = p["id"]
            put(p, None)
            for c in p.get("panels") or []:
                put(c, row)
        else:
            put(p, row)
    return {"dash": top, "panels": panels}


def denormalize(norm):
    d = copy.deepcopy(norm["dash"])
    out, kids = [], {}
    for pid, p in norm["panels"].items():
        q = dict(copy.deepcopy(p), id=pid)
        if q.get("type") != "row":
            kids.setdefault(q.pop(ROW_KEY, None), []).append(q)
    rows = sorted(((pid, p) for pid, p in norm["panels"].items() if p.get("type") == "row"), key=lambda x: yx(x[1]))
    out.extend(sorted(kids.get(None, []), key=yx))
    for pid, p in rows:
        r = dict(copy.deepcopy(p), id=pid)
        mine = sorted(kids.get(pid, []), key=yx)
        if r.get("collapsed"):
            r["panels"] = mine
            out.append(r)
        else:
            out.append(r)
            out.extend(mine)
    d["panels"] = out
    return d


def dump(x):
    return json.dumps(x, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def digest(norms):
    return hashlib.sha256(dump(norms).encode("utf-8")).hexdigest()[:8]


# ---------------------------------------------------------------- diff
def diff(a, b, path=()):
    """Leaf operations turning a into b: (path, value) or (path, DEL)."""
    if isinstance(a, dict) and isinstance(b, dict):
        ops = []
        for k in sorted(a, key=str):
            if k not in b:
                ops.append((path + (k,), DEL))
        for k in sorted(b, key=str):  # sorted: the result must not depend on the key order of the source
            if k not in a:
                ops.append((path + (k,), b[k]))
            else:
                ops.extend(diff(a[k], b[k], path + (k,)))
        return ops
    if isinstance(a, list) and isinstance(b, list) and len(a) == len(b):
        ops = []
        for i, (x, y) in enumerate(zip(a, b)):
            ops.extend(diff(x, y, path + (i,)))
        return ops
    if dump(a) == dump(b) and type(a) == type(b):
        return []
    return [(path, b)]


def expand(obj, pattern):
    """Concrete paths for a pattern whose '*' segments match list indices; only through existing containers."""
    found = []

    def walk(o, i, acc):
        if i == len(pattern) - 1:
            seg = pattern[i]
            if seg == "*":
                if isinstance(o, list):
                    found.extend(acc + (n,) for n in range(len(o)))
            elif isinstance(o, dict) or (isinstance(o, list) and isinstance(seg, int) and seg < len(o)):
                found.append(acc + (seg,))
            return
        seg = pattern[i]
        if seg == "*":
            if isinstance(o, list):
                for n, x in enumerate(o):
                    walk(x, i + 1, acc + (n,))
        elif isinstance(o, dict) and seg in o:
            walk(o[seg], i + 1, acc + (seg,))
        elif isinstance(o, list) and isinstance(seg, int) and seg < len(o):
            walk(o[seg], i + 1, acc + (seg,))

    if pattern:
        walk(obj, 0, ())
    return found


def get_at(obj, path):
    for seg in path:
        obj = obj[seg]
    return obj


def has_at(obj, path):
    try:
        get_at(obj, path)
        return True
    except (KeyError, IndexError, TypeError):
        return False


def set_at(obj, path, value):
    for seg in path[:-1]:
        obj = obj[seg]
    last = path[-1]
    if value is DEL:
        if isinstance(obj, dict):
            obj.pop(last, None)
        else:
            raise Fail("cannot delete a list element by index")
    elif isinstance(obj, list) and last == len(obj):
        obj.append(copy.deepcopy(value))
    else:
        obj[last] = copy.deepcopy(value)


# ---------------------------------------------------------------- text form
def seg_text(seg):
    if isinstance(seg, int):
        return str(seg)
    if seg == "*":
        return "*"
    if seg in ABBR:
        return ABBR[seg]
    if seg in SHORT or not BARE.match(seg) or seg.isdigit():
        return json.dumps(seg, ensure_ascii=False)
    return seg


def path_text(path):
    return ".".join(seg_text(s) for s in path)


def ids_text(ids):
    ids = sorted(ids)
    out, i = [], 0
    while i < len(ids):
        j = i
        while j + 1 < len(ids) and ids[j + 1] == ids[j] + 1:
            j += 1
        out.append(str(ids[i]) if i == j else "%d-%d" % (ids[i], ids[j]))
        i = j + 1
    return ",".join(out)


def val_text(v):
    return json.dumps(v, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def parse_path(text):
    segs, i = [], 0
    while i < len(text):
        if text[i] == '"':
            m = re.match(r'"(?:[^"\\]|\\.)*"', text[i:])
            segs.append(("key", json.loads(m.group(0))))
            i += m.end()
        else:
            m = re.match(r"[^.]+", text[i:])
            tok = m.group(0)
            segs.append(("tok", tok))
            i += m.end()
        if i < len(text):
            if text[i] != ".":
                raise Fail("bad path: %s" % text)
            i += 1
    return segs


def resolve(obj, segs):
    """Turn parsed segments into a pattern, choosing list index or dict key by the container met on the way."""
    out = []
    cur = obj
    for kind, tok in segs:
        if kind == "key":
            seg = tok
        elif tok == "*":
            seg = "*"
        elif isinstance(cur, list) and tok.isdigit():
            seg = int(tok)
        else:
            seg = SHORT.get(tok, tok)
        out.append(seg)
        if cur is not None and seg != "*":
            try:
                cur = cur[seg]
            except (KeyError, IndexError, TypeError):
                cur = None
        elif seg == "*":
            cur = cur[0] if isinstance(cur, list) and cur else None
    return tuple(out)


# ---------------------------------------------------------------- building the result
def ops_size(ops):
    """Characters the operations would take in the result file."""
    return sum(len(path_text(p)) + (2 if v is DEL else 4 + len(val_text(v))) for p, v in ops)


def row_of(norm, pid):
    p = norm["panels"].get(pid)
    if p is None:
        return None
    row = pid if p.get("type") == "row" else p.get(ROW_KEY)
    return norm["panels"][row].get("title", "") if row in norm["panels"] else "(顶部)"


def make_lines(base, user):
    """Lines turning the normal forms in `base` into `user` ({'P': norm, 'D': norm}).
    Returns (lines, stats) where stats[side] = {row title: number of differing fields}."""
    lines, stats = [], {"P": {}, "D": {}}
    work = copy.deepcopy(base)
    panel_ops = []  # (side, pid, path, value)

    def count(side, title):
        stats[side][title] = stats[side].get(title, 0) + 1

    for side in ("P", "D"):
        b, u = work[side], user[side]
        for path, v in diff(b["dash"], u["dash"]):
            lines.append("%s/dash %s %s" % (side, path_text(path), "-" if v is DEL else "= " + val_text(v)))
            set_at(b["dash"], path, v)
            count(side, "看板")
        gone = sorted(set(b["panels"]) - set(u["panels"]))
        if gone:
            lines.append("%s/%s -" % (side, ids_text(gone)))
            for pid in gone:
                count(side, row_of(base[side], pid))
                del b["panels"][pid]
        for pid in sorted(set(u["panels"]) - set(b["panels"])):
            target = u["panels"][pid]
            same = [q for q, p in base[side]["panels"].items() if p.get("type") == target.get("type")]
            if same:
                src = min(same, key=lambda q: (ops_size(diff(base[side]["panels"][q], target)), q))
                lines.append("%s/%s = @%s" % (side, pid, src))
                b["panels"][pid] = copy.deepcopy(base[side]["panels"][src])
            else:
                lines.append("%s/%s = %s" % (side, pid, val_text(target)))
                b["panels"][pid] = copy.deepcopy(target)
            count(side, row_of(user[side], pid))
        for pid in sorted(u["panels"]):
            for path, v in diff(b["panels"][pid], u["panels"][pid]):
                panel_ops.append((side, pid, path, v))
                count(side, row_of(user[side], pid))
    lines.extend(group_ops(work, panel_ops))
    return lines, stats


def group_ops(work, ops):
    """Write each operation once; operations that are the same on several panels share one line."""
    remaining = set(range(len(ops)))
    out = []

    def keys_of(i):
        side, pid, path, v = ops[i]
        wild = tuple("*" if isinstance(s, int) else s for s in path)
        ks = []
        for pat in {path, wild}:
            ks.append((side, pat, "=", "-" if v is DEL else val_text(v)))
            if v is not DEL and isinstance(v, (int, float)) and not isinstance(v, bool):
                old = get_at(work[side]["panels"][pid], path) if has_at(work[side]["panels"][pid], path) else None
                if isinstance(old, (int, float)) and not isinstance(old, bool):
                    delta = v - old
                    if isinstance(delta, float) and delta == int(delta) and isinstance(v, int):
                        delta = int(delta)
                    if old + delta == v and type(old + delta) == type(v):  # exact, no float rounding
                        ks.append((side, pat, "+=", val_text(delta)))
        return ks

    while True:
        cands = {}
        for i in remaining:
            for k in keys_of(i):
                cands.setdefault(k, []).append(i)
        best = None
        for k, members in cands.items():
            if len(members) < 2:
                continue
            side, pat, op, val = k
            by_pid = {}
            for i in members:
                by_pid.setdefault(ops[i][1], set()).add(ops[i][2])
            good = []
            for pid, paths in by_pid.items():
                exp = expand(work[side]["panels"][pid], pat)
                if op == "=" and val == "-":
                    exp = [p for p in exp if has_at(work[side]["panels"][pid], p)]
                if set(exp) == paths:
                    good.append(pid)
            covered = [i for i in members if ops[i][1] in good]
            if len(covered) >= 2 and (best is None or len(covered) > len(best[1])
                                      or (len(covered) == len(best[1]) and len(dump(k)) < len(dump(best[0])))):
                best = (k, covered, good)
        if best is None:
            break
        (side, pat, op, val), covered, good = best
        all_ids = [pid for pid, p in work[side]["panels"].items() if p.get("type") != "row"
                   and (expand(p, pat) if not (op == "=" and val == "-") else
                        [q for q in expand(p, pat) if has_at(p, q)])]
        scope = "*" if sorted(good) == sorted(all_ids) and all(work[side]["panels"][q].get("type") != "row" for q in good) \
            else ids_text(good)
        out.append((side, scope, pat, op, val))
        remaining -= set(covered)
    for i in sorted(remaining, key=lambda i: (ops[i][0], ops[i][1], dump([str(s) for s in ops[i][2]]))):
        side, pid, path, v = ops[i]
        out.append((side, str(pid), path, "=", "-" if v is DEL else val_text(v)))
    # the same * line on both boards becomes one line
    lines, used = [], set()
    for n, (side, scope, pat, op, val) in enumerate(out):
        if n in used:
            continue
        twin = next((m for m, o in enumerate(out) if m != n and m not in used and o[0] != side and o[1:] == (scope, pat, op, val)
                     and scope == "*"), None)
        board = side
        if twin is not None:
            used.add(twin)
            board = "*"
        used.add(n)
        tail = "-" if (op == "=" and val == "-") else "%s %s" % (op, val)
        lines.append("%s/%s %s %s" % (board, scope, path_text(pat), tail))
    return lines


def board_digest(norm):
    """8 hex digits over one board's normal form; --apply checks the restored board against it."""
    return hashlib.sha256(dump(norm).encode("utf-8")).hexdigest()[:8]


def summary(side, stats, meta, check):
    """One header line per board: number of differing fields, the rows they are in, how Grafana loads the board."""
    rows = sorted(stats[side].items(), key=lambda kv: -kv[1])
    where = ("预置 %s" % meta.get("provisionedExternalId")) if meta.get("provisioned") else \
        ("非预置" if meta else "文件输入")
    shown = "".join("%s%d " % kv for kv in rows[:5]) + ("另%d行 " % (len(rows) - 5) if len(rows) > 5 else "")
    return "# %s %d处 %s| %s | %s" % (side, sum(stats[side].values()), shown, where, check)


# ---------------------------------------------------------------- applying a result file
def apply_lines(base, text):
    work = copy.deepcopy(base)
    panel_lines = []
    for raw in text.split("\n"):  # not splitlines(): U+2028, U+2029, U+0085 may sit inside values
        ln = raw.rstrip("\n")
        if not ln.strip() or ln.startswith("#"):
            continue
        m = re.match(r"^([PD*])/(\S+)(?: (.*))?$", ln)
        if not m:
            raise Fail("cannot read line: %s" % ln)
        boards = ("P", "D") if m.group(1) == "*" else (m.group(1),)
        scope, rest = m.group(2), m.group(3) or ""
        if scope == "dash":
            for side in boards:
                op_apply(work[side]["dash"], rest, ln)
            continue
        if rest == "-":
            for side in boards:
                for pid in parse_ids(scope, None):
                    work[side]["panels"].pop(pid, None)
            continue
        if rest.startswith("= "):  # a panel only the user has: "= @<baseline id>" or "= <full JSON>"
            v = rest[2:]
            for side in boards:
                if v.startswith("@"):
                    work[side]["panels"][int(scope)] = copy.deepcopy(base[side]["panels"][int(v[1:])])
                else:
                    work[side]["panels"][int(scope)] = json.loads(v)
            continue
        panel_lines.append((boards, scope, rest, ln))
    for boards, scope, rest, ln in panel_lines:
        for side in boards:
            panels = work[side]["panels"]
            for pid in parse_ids(scope, panels):
                op_apply(panels[pid], rest, ln)
    return work


def parse_ids(scope, panels):
    if scope == "*":
        return [pid for pid, p in panels.items() if p.get("type") != "row"]
    ids = []
    for part in scope.split(","):
        a, _, b = part.partition("-")
        ids.extend(range(int(a), int(b or a) + 1))
    return ids


def op_apply(obj, rest, ln):
    m = re.match(r'^((?:"(?:[^"\\]|\\.)*"|[^ "])+) (?:(-)$|(=|\+=) (.*)$)', rest)
    if not m:
        raise Fail("cannot read line: %s" % ln)
    path_s, op, val = m.group(1), m.group(2) or m.group(3), m.group(4)
    pattern = resolve(obj, parse_path(path_s))
    if op == "-":
        for p in expand(obj, pattern):
            if has_at(obj, p):
                set_at(obj, p, DEL)
        return
    value = json.loads(val)
    for p in expand(obj, pattern):
        if op == "+=":
            cur = get_at(obj, p)
            set_at(obj, p, cur + value)
        else:
            set_at(obj, p, value)


# ---------------------------------------------------------------- reading the user's dashboards
def auth_header(args):
    token = args.token or os.environ.get("GRAFANA_TOKEN")
    if token:
        return "Bearer " + token
    user = args.user or os.environ.get("GRAFANA_USER")
    if not user:
        return None
    password = os.environ.get("GRAFANA_PASSWORD")
    if password is None:
        password = getpass.getpass("Grafana password for %s: " % user)
    return "Basic " + base64.b64encode(("%s:%s" % (user, password)).encode("utf-8")).decode("ascii")


def fetch(url, uid, auth):
    req = urllib.request.Request("%s/api/dashboards/uid/%s" % (url.rstrip("/"), uid))
    req.add_header("Accept", "application/json")
    if auth:
        req.add_header("Authorization", auth)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raise Fail("读取看板 %s 失败：HTTP %s %s" % (uid, e.code, e.read().decode("utf-8", "replace")[:300]))
    except urllib.error.URLError as e:
        raise Fail("连不上 Grafana %s：%s" % (url, e.reason))


def utf8_stdio():
    """Python 3.6 under a C/POSIX locale writes ASCII to the terminal; the messages here are Chinese."""
    for name in ("stdout", "stderr"):
        stream = getattr(sys, name)
        if hasattr(stream, "buffer") and (stream.encoding or "").lower().replace("-", "") != "utf8":
            setattr(sys, name, io.TextIOWrapper(stream.buffer, encoding="utf-8", line_buffering=True))


def refuse_existing(paths):
    for path in paths:
        if os.path.exists(path):
            raise Fail("%s 已存在，不覆盖。请换一个空目录" % path)


# ---------------------------------------------------------------- decisions: site changes into the next baseline
NONE = object()
KEEP, DROP = ("保留", "keep"), ("丢弃", "drop")


def brief(v, n=50):
    t = "（无）" if v is NONE else (v if isinstance(v, str) else dump(v))
    return t if len(t) <= n else t[:n - 1] + "…"


def var_list(dash):
    return (dash.get("templating") or {}).get("list") or []


def unique_refs(targets):
    refs = [t.get("refId") for t in targets or [] if isinstance(t, dict)]
    return refs if len(refs) == len(set(refs)) and all(isinstance(r, str) and " " not in r for r in refs) else None


def same(a, c):
    return (a is NONE) == (c is NONE) and (a is NONE or (dump(a) == dump(c) and type(a) == type(c)))


def units(base, user):
    """One decision unit per change the site made: [(key, text)]. Units: a dashboard field, a variable (by name),
    an added or removed panel, a field of a panel, a query of a panel (by refId)."""
    out = []
    for side in ("P", "D"):
        b, u = base[side], user[side]
        for k in sorted(set(b["dash"]) | set(u["dash"]), key=str):
            x, y = b["dash"].get(k, NONE), u["dash"].get(k, NONE)
            if k != "templating" and not same(x, y):
                out.append(("%s/dash/%s" % (side, k), "看板字段 %s：基线 %s → 现场 %s" % (k, brief(x), brief(y))))
        bv = {v.get("name"): v for v in var_list(b["dash"])}
        uv = {v.get("name"): v for v in var_list(u["dash"])}
        for n in sorted(set(bv) | set(uv), key=str):
            if n not in uv:
                out.append(("%s/var/%s" % (side, n), "现场删了变量 %s" % n))
            elif n not in bv:
                out.append(("%s/var/%s" % (side, n), "现场加了变量 %s：%s" % (n, brief(uv[n].get("query")))))
            elif dump(bv[n]) != dump(uv[n]):
                fields = sorted(set(str(p[0]) for p, _ in diff(bv[n], uv[n])))
                out.append(("%s/var/%s" % (side, n), "变量 %s 的 %s 不同" % (n, "、".join(fields))))
        if [n for n in bv if n in uv] != [n for n in uv if n in bv]:
            out.append(("%s/var/顺序" % side, "变量的顺序不同"))
        bp, up = b["panels"], u["panels"]
        for pid in sorted(set(bp) - set(up)):
            out.append(("%s/%s/删除" % (side, pid), "现场删了 %s「%s」" % (bp[pid].get("type"), brief(bp[pid].get("title"), 30))))
        for pid in sorted(set(up) - set(bp)):
            p = up[pid]
            row = up.get(p.get(ROW_KEY), {}).get("title") if p.get(ROW_KEY) is not None else None
            out.append(("%s/%s/新增" % (side, pid), "现场加了 %s「%s」%s" % (p.get("type"), brief(p.get("title"), 30),
                                                                     "（行「%s」）" % brief(row, 16) if row else "")))
        for pid in sorted(set(bp) & set(up)):
            x, y = bp[pid], up[pid]
            for k in sorted(set(x) | set(y), key=str):
                a, c = x.get(k, NONE), y.get(k, NONE)
                if same(a, c):
                    continue
                if k == "targets" and a is not NONE and c is not NONE and unique_refs(a) is not None and unique_refs(c) is not None:
                    ta = {t["refId"]: t for t in a}
                    tc = {t["refId"]: t for t in c}
                    for r in sorted(set(ta) | set(tc)):
                        if dump(ta.get(r)) != dump(tc.get(r)):
                            out.append(("%s/%s/targets/%s" % (side, pid, r), "「%s」查询 %s：基线 %s → 现场 %s" % (
                                brief(y.get("title"), 20), r, brief((ta.get(r) or {}).get("expr", NONE) if r in ta else NONE),
                                brief((tc.get(r) or {}).get("expr", NONE) if r in tc else NONE))))
                    if [t for t in unique_refs(a) if t in tc] != [t for t in unique_refs(c) if t in ta]:
                        out.append(("%s/%s/targets/顺序" % (side, pid), "「%s」查询顺序不同" % brief(y.get("title"), 20)))
                    continue
                name = {ROW_KEY: "所在的行", "title": "标题", "type": "类型", "gridPos": "位置"}.get(k, k)
                out.append(("%s/%s/%s" % (side, pid, k), "「%s」%s：基线 %s → 现场 %s" % (
                    brief(x.get("title"), 20), name, brief(a), brief(c))))
    return out


def write_decisions(path, bdig, user, us):
    head = ["# 面板一并入决策 fmt 1。每行第一列写「保留」或「丢弃」（默认保留），第二列和后面的说明不要改。",
            "# 保留 = 这处现场改动进新基线；丢弃 = 新基线不要它，以后全量部署时会被覆盖掉。",
            "# base %s P %s D %s" % (bdig, board_digest(user["P"]), board_digest(user["D"]))]
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(head + ["保留 %s %s" % (k, t) for k, t in us]) + "\n")


def read_decisions(path, bdig, user, us):
    keys = [k for k, _ in us]
    keep, seen = set(), set()
    with open(path, encoding="utf-8") as f:
        text = f.read()
    m = re.search(r"^# base (\w+) P (\w+) D (\w+)", text, re.M)
    if not m:
        raise Fail("决策文件开头缺少「# base … P … D …」一行")
    if m.group(1) != bdig:
        raise Fail("决策文件是对基线 %s 做的，本脚本内置的是 %s" % (m.group(1), bdig))
    for side, want in (("P", m.group(2)), ("D", m.group(3))):
        if board_digest(user[side]) != want:
            raise Fail("现场 %s 看板的摘要是 %s，决策文件记的是 %s：看板在做决策之后又变了，请重新生成决策文件"
                       % (side, board_digest(user[side]), want))
    for n, ln in enumerate(text.split("\n"), 1):
        if not ln.strip() or ln.startswith("#"):
            continue
        parts = ln.split(None, 2)
        if len(parts) < 2 or parts[0] not in KEEP + DROP:
            raise Fail("决策文件第 %d 行读不懂（第一列要是「保留」或「丢弃」）：%s" % (n, ln))
        if parts[1] not in keys:
            raise Fail("决策文件第 %d 行的 %s 不是现场的改动" % (n, parts[1]))
        if parts[1] in seen:
            raise Fail("决策文件里 %s 出现了两次" % parts[1])
        seen.add(parts[1])
        if parts[0] in KEEP:
            keep.add(parts[1])
    missing = [k for k in keys if k not in seen]
    if missing:
        raise Fail("决策文件少了 %d 条：%s" % (len(missing), "，".join(missing[:5])))
    return keep


def merge(base, user, us, keep):
    """The baseline with the kept units applied, by panel id, variable name and refId."""
    new = copy.deepcopy(base)
    for key, _ in us:
        if key not in keep:
            continue
        side, rest = key.split("/", 1)
        b, u, n = base[side], user[side], new[side]
        a, _, c = rest.partition("/")
        if a == "dash":
            if c in u["dash"]:
                n["dash"][c] = copy.deepcopy(u["dash"][c])
            else:
                n["dash"].pop(c, None)
        elif a == "var" and c == "顺序":
            order = [v.get("name") for v in var_list(u["dash"])]
            n["dash"]["templating"]["list"].sort(key=lambda v: order.index(v.get("name")) if v.get("name") in order else len(order))
        elif a == "var":
            lst = n["dash"].setdefault("templating", {}).setdefault("list", [])
            uv = [v for v in var_list(u["dash"]) if v.get("name") == c]
            at = next((i for i, v in enumerate(lst) if v.get("name") == c), None)
            if not uv:
                del lst[at]
            elif at is not None:
                lst[at] = copy.deepcopy(uv[0])
            else:
                pos = next(i for i, v in enumerate(var_list(u["dash"])) if v.get("name") == c)
                lst.insert(min(pos, len(lst)), copy.deepcopy(uv[0]))
        else:
            pid = int(a)
            if c == "删除":
                n["panels"].pop(pid, None)
            elif c == "新增":
                n["panels"][pid] = copy.deepcopy(u["panels"][pid])
            elif c.startswith("targets/"):
                ref = c.split("/", 1)[1]
                tu = u["panels"][pid].get("targets") or []
                tn = n["panels"][pid].setdefault("targets", [])
                if ref == "顺序":
                    order = [t["refId"] for t in tu]
                    tn.sort(key=lambda t: order.index(t["refId"]) if t["refId"] in order else len(order))
                    continue
                site = next((t for t in tu if t.get("refId") == ref), None)
                at = next((i for i, t in enumerate(tn) if t.get("refId") == ref), None)
                if site is None:
                    del tn[at]
                elif at is not None:
                    tn[at] = copy.deepcopy(site)
                else:
                    pos = next(i for i, t in enumerate(tu) if t.get("refId") == ref)
                    tn.insert(min(pos, len(tn)), copy.deepcopy(site))
            elif c in u["panels"][pid]:
                n["panels"][pid][c] = copy.deepcopy(u["panels"][pid][c])
            else:
                n["panels"][pid].pop(c, None)
    for side in ("P", "D"):
        ps = new[side]["panels"]
        lost = sorted(pid for pid, p in ps.items() if p.get("type") != "row" and p.get(ROW_KEY) is not None
                      and ps.get(p[ROW_KEY], {}).get("type") != "row")
        if lost:
            raise Fail("%s 看板的 panel %s 所在的行在新基线里没有了：行和行里的 panel 要一起保留或一起丢弃"
                       % (side, ids_text(lost)))
    # self-check: what is left against the site is exactly what was dropped, and the baseline moved by what was kept
    left = set(k for k, _ in units(new, user))
    moved = set(k for k, _ in units(base, new))
    dropped = set(k for k, _ in us) - keep
    if left != dropped or moved != keep:
        raise Fail("合并自检失败：剩下 %s，应为 %s；新基线改了 %s，应为 %s" % (
            sorted(left ^ dropped)[:5], "丢弃的那些", sorted(moved ^ keep)[:5], "保留的那些"))
    return new


def main():
    utf8_stdio()
    ap = argparse.ArgumentParser(description="面板一·P、面板一·D 与基线的差异（只读）。")
    ap.add_argument("--url", default=os.environ.get("GRAFANA_URL"), help="Grafana 地址（或环境变量 GRAFANA_URL）")
    ap.add_argument("--token", help="服务账号 token（或环境变量 GRAFANA_TOKEN）")
    ap.add_argument("--user", help="用户名（或 GRAFANA_USER）；密码取 GRAFANA_PASSWORD，没有就提示输入")
    ap.add_argument("--uids", nargs=2, default=[u for _, u in UIDS], metavar=("P_UID", "D_UID"))
    ap.add_argument("--files", nargs=2, metavar=("P_JSON", "D_JSON"), help="改读导出的两个 JSON 文件")
    ap.add_argument("-o", "--output", default="panel1-diff.txt", help="结果文件（默认 ./panel1-diff.txt）")
    ap.add_argument("--apply", metavar="DIFF_TXT", help="（我们这边用）把结果文件套到基线上")
    ap.add_argument("--out-dir", default=".", help="--apply、--merge 时两个 JSON 写到哪里")
    ap.add_argument("--decisions-out", metavar="FILE", help="另写一个决策文件：现场的每处改动一行，默认「保留」")
    ap.add_argument("--merge", action="store_true", help="（我们这边用）基线加上决策文件里保留的改动，写成新基线")
    ap.add_argument("--decisions", metavar="FILE", help="--merge 用的决策文件")
    ap.add_argument("--dump-baseline", metavar="DIR", help="把内置基线写成两个 JSON 文件")
    args = ap.parse_args()
    try:
        base_raw = baseline()
        base = {side: normalize(base_raw[side]) for side in ("P", "D")}
        bdig = digest(base)
        if args.dump_baseline:
            refuse_existing(os.path.join(args.dump_baseline, OUT_NAMES[side]) for side in ("P", "D"))
            for side in ("P", "D"):
                with open(os.path.join(args.dump_baseline, OUT_NAMES[side]), "w", encoding="utf-8") as f:
                    json.dump(base_raw[side], f, ensure_ascii=False, indent=2)
                    f.write("\n")
            print("基线已写到 %s（base %s）" % (args.dump_baseline, bdig))
            return 0
        if args.apply:
            with open(args.apply, encoding="utf-8") as f:
                text = f.read()
            m = re.search(r"^# base (\w+)", text, re.M)
            if m and m.group(1) != bdig:
                raise Fail("结果文件是对基线 %s 做的，本脚本内置的是 %s" % (m.group(1), bdig))
            checks = dict(re.findall(r"^# ([PD]) .*\| ([0-9a-f]{8})$", text, re.M))
            if sorted(checks) != ["D", "P"]:
                raise Fail("结果文件开头缺少 P、D 的校验摘要，文件可能在传递中被改动过")
            rebuilt = apply_lines(base, text)
            for side in ("P", "D"):
                got = board_digest(rebuilt[side])
                if got != checks[side]:
                    raise Fail("套用后 %s 的校验摘要是 %s，结果文件记的是 %s：文件在传递中被改动过，请原样（作为附件）重新发送"
                               % (side, got, checks[side]))
            refuse_existing(os.path.join(args.out_dir, OUT_NAMES[side]) for side in ("P", "D"))
            for side in ("P", "D"):
                path = os.path.join(args.out_dir, OUT_NAMES[side])
                with open(path, "w", encoding="utf-8") as f:
                    json.dump(put_config(denormalize(rebuilt[side]), base_raw[side]), f, ensure_ascii=False, indent=2)
                    f.write("\n")
                print("写出 %s" % path)
            return 0
        if args.merge and not args.decisions:
            ap.error("--merge 要和 --decisions 一起用")
        metas = {}
        if args.files:
            user_raw = {}
            for side, path in zip(("P", "D"), args.files):
                with open(path, encoding="utf-8") as f:
                    data = json.load(f)
                user_raw[side] = classic(data, path)
                if isinstance(data.get("meta"), dict):
                    metas[side] = data["meta"]
        elif args.url:
            auth = auth_header(args)
            user_raw = {}
            for (side, _), uid in zip(UIDS, args.uids):
                res = fetch(args.url, uid, auth)
                user_raw[side], metas[side] = classic(res, "看板 %s" % uid), res.get("meta", {})
        else:
            ap.error("需要 --url（或环境变量 GRAFANA_URL），或 --files")
        user = {side: normalize(user_raw[side]) for side in ("P", "D")}
        if args.merge:
            us = units(base, user)
            keep = read_decisions(args.decisions, bdig, user, us)
            new = merge(base, user, us, keep)
            os.makedirs(args.out_dir, exist_ok=True)
            refuse_existing(os.path.join(args.out_dir, OUT_NAMES[side]) for side in ("P", "D"))
            for side in ("P", "D"):
                path = os.path.join(args.out_dir, OUT_NAMES[side])
                with open(path, "w", encoding="utf-8") as f:
                    json.dump(put_config(denormalize(new[side]), base_raw[side]), f, ensure_ascii=False, indent=2)
                    f.write("\n")
                print("写出 %s" % path)
            print("保留 %d 处、丢弃 %d 处；新基线 base %s（P %s，D %s），合并自检通过" % (
                len(keep), len(us) - len(keep), digest(new), board_digest(new["P"]), board_digest(new["D"])))
            return 0
        lines, stats = make_lines(base, user)
        head = ["# base %s fmt %d" % (bdig, FORMAT)] + [summary(side, stats, metas.get(side) or {}, board_digest(user[side]))
                                                         for side in ("P", "D")]
        text = "\n".join(head + lines) + "\n"
        # self-check: the file must turn the baseline into exactly what was read
        back = apply_lines(base, text)
        bad = [side for side in ("P", "D") if dump(back[side]) != dump(user[side])
               or dump(normalize(denormalize(back[side]))) != dump(user[side])]
        if bad:
            raise Fail("自检失败：结果文件套回基线后与 %s 不一致。请把这条输出发给我们" % "、".join(bad))
        with open(args.output, "w", encoding="utf-8") as f:
            f.write(text)
        for line in head[1:]:
            print(line[2:])
        print("结果文件 %s：%d 行，%d 字符；套回基线自检通过" % (os.path.abspath(args.output), text.count("\n"), len(text)))
        if args.decisions_out:
            refuse_existing([args.decisions_out])
            us = units(base, user)
            write_decisions(args.decisions_out, bdig, user, us)
            print("决策文件 %s：%d 条" % (os.path.abspath(args.decisions_out), len(us)))
        total = sum(sum(st.values()) for st in stats.values())
        print("与基线：%s" % ("无差异" if total == 0 else "%d 处差异（退出码 1）" % total))
        return 1 if total else 0
    except (Fail, OSError, ValueError) as e:
        print("出错：%s" % e)
        return 2


if __name__ == "__main__":
    sys.exit(main())
