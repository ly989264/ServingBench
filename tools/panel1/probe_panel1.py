#!/usr/bin/env python3
"""Read-only probe of panel 1 (面板一·P, 面板一·D) on site: compare Grafana and Prometheus with the v3 baseline.

  python3 probe_panel1.py                 # addresses and logins from the environment variables below
  python3 probe_panel1.py --grafana-dir /etc/grafana --prometheus-dir vllm-pd-prometheus:/etc/prometheus

Environment: GRAFANA_URL, GRAFANA_USER + GRAFANA_PASSWORD or GRAFANA_TOKEN;
             PROMETHEUS_URL, optional PROMETHEUS_USER + PROMETHEUS_PASSWORD or PROMETHEUS_TOKEN.
A password that is not in the environment is asked for. Passwords and tokens are never written anywhere.

Writes panel1-probe.txt, the only file to send back: one screen (--max-lines, default 40, at most 100 characters
a line) with one line per difference (stable id, part, kind, object and what differs) and the consistent parts as
one count; IP addresses are replaced by host-01, host-02, ... unless --no-mask is given. The same site gives the
same ids. Everything else goes to panel1-export/ (format "panel1-export", version 1; fields in
PROBE_PANEL1.zh-CN.md), which stays on this machine: panel1-detail.txt (every item by id), panel1-probe.json,
export.json (the site configuration as it is, unmasked, for the update tools) and copies of the files.
  --grafana-dir DIR, --prometheus-dir DIR   list and compare the files of these directories with the v3 pack;
                          DIR is a path on this machine or container:path (read with docker cp, nothing written)
  --prometheus-yml FILE   copy prometheus.yml (password-like values replaced by <secret>) and its rule files
  --inventory FILE        copy the old inventory.json
A part that cannot be read is named with the reason; the other parts are still reported. Exit code 0 when every
part was read, 1 when some part could not be read, 2 on a usage error.

Baseline: the two dashboards built into diff_panel1.py (the v3 pack, the 11-11 calibration and the four
update_panel1.py changes; the state before those four changes is recognised too), the v3 pack's Grafana provider
and datasource, and the Prometheus configuration gen_prometheus_yml.py writes, as Prometheus 3.11.2 loads it.
Python 3.6+, standard library only. Nothing is written to Grafana, Prometheus or their containers.
"""
import argparse
import base64
import copy
import datetime
import fnmatch
import getpass
import glob
import hashlib
import io
import json
import os
import posixpath
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import zlib

# ================================================================ comparison code of diff_panel1.py (unchanged)
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


# ================================================================ end of the diff_panel1.py code

PROBE_FORMAT = 1
DIFF_SCRIPT_SHA = "db31ca6a04b5ddbc"     # sha256 prefix of the diff_panel1.py the code and baseline above come from
PACK_SHA = "7ceaff80"                # vllm-pd-grafana-panel1.tar.gz of the v3 pack
# fields that make the state before the four update_panel1.py changes out of the baseline (diff_panel1.py lines)
BEFORE_CHANGES_LINES = 'P/101-103,111-117,900-911,913-921,950 -\nD/101-106,111,900-911,913-921,950 -\nP/32-37,52-72,78-83,87-95,97,99 g.w = 8\nD/41-49,64-66,68-82,85-93,95-97 g.w = 8\nP/801-806,808-811,813-818 t.*.in -\nD/801-806,808-811,813-818 t.*.in -\nP/801-806,808-811,813-818 t.*.rn = true\nD/801-806,808-811,813-818 t.*.rn = true\nD/41-49,64-66,68-73,76-78,80-81 o.lg.pl = "right"\nD/19-36 r = 18\nD/19-36 g.y += -546\nP/801-806,808-811,813-818 o.gm = "area"\nD/801-806,808-811,813-818 o.gm = "area"\nP/33,36,53,56,59,62,65,68,71,79,82,88,91,94,99 g.x = 8\nP/34,37,54,57,60,63,66,69,72,80,83,89,92,95 g.x = 16\nD/42,45,48,65,69,72,75,78,81,86,89,92,96 g.x = 8\nD/43,46,49,66,70,73,76,79,82,87,90,93,97 g.x = 16\nP/61-72 r = 51\nP/19-27 r = 18\nP/19-27 g.y += -535\nP/37-44 g.y += -36\nP/45-52 g.y += -45\nD/49-56 g.y += -84\nD/57-64 g.y += -95\nP/73-78 g.y += -190\nD/37-41 g.y += -18\nP/83,85-87 g.y += -253\nD/82-85 g.y += -216\nP/55-57 g.y = 108\nP/58-60 g.y = 117\nP/61-63 g.y = 126\nP/64-66 g.y = 135\nP/67-69 g.y = 144\nP/70-72 g.y = 153\nP/90-92 g.y = 226\nP/93-95 g.y = 235\nD/44-46 g.y = 108\nD/68-70 g.y = 171\nD/71-73 g.y = 182\nD/74-76 g.y = 193\nD/77-79 g.y = 204\nD/88-90 g.y = 258\nD/91-93 g.y = 269\nD/95-97 g.y = 307\nP/33-34 g.y = 56\nP/35-36 g.y = 65\nP/53-54 g.y = 99\nD/42-43 g.y = 97\nP/79-80 g.y = 168\nP/81-82 g.y = 177\nP/88-89 g.y = 217\nP/97,99 g.y = 245\nD/47-48 g.y = 119\nD/65-66 g.y = 159\nD/80-81 g.y = 215\nD/86-87 g.y = 247\nP/45-46 fc.d.u = "short"\nD/1 ti = "总览"\nD/57 t.0.e = "sum by (le) (increase(vllm:request_generation_tokens_bucket{role=\\"decode\\", model_name=\\"$model_name\\", pd_instance=~\\"$pd_instance\\"}[$__range]))"\nD/65 ti = "本地命中率"\nD/66 ti = "外部命中率"\nD/67 g.y = 170\nD/84 o.co.min = -0.5\nD/84 o.co.sc = "Greens"\nD/84 o.fv.le = -1\nD/94 g.y = 280\nD/810 ti = "KV Cache 使用率"\nD/811 t.0.lf = "本地"\nD/811 t.1.lf = "外部"\nD/811 ti = "KV Cache 命中率"\nP/1 ti = "总览"\nP/45 t.0.e = "sum by (le) (increase(vllm:request_prompt_tokens_bucket{role=\\"prefill\\", model_name=\\"$model_name\\", pd_instance=~\\"$pd_instance\\"}[$__range]))"\nP/46 t.0.e = "sum by (le) (increase(vllm:request_prefill_kv_computed_tokens_bucket{role=\\"prefill\\", model_name=\\"$model_name\\", pd_instance=~\\"$pd_instance\\"}[$__range]))"\nP/51 ti = "KV Cache 与 Mooncake Client"\nP/53 ti = "本地命中率"\nP/54 ti = "外部命中率"\nP/86 o.co.min = -0.5\nP/86 o.co.sc = "Greens"\nP/86 o.fv.le = -1\nP/96 g.y = 244\nP/810 ti = "KV Cache 使用率"\nP/811 t.0.lf = "本地"\nP/811 t.1.lf = "外部"\nP/811 ti = "KV Cache 命中率"\nP/821 ti = "输入 tokens 构成"\n'
# Prometheus 3.11.2's /api/v1/status/config for the gen_prometheus_yml.py output (example inventory, patched)
PROM_BASELINE = json.loads(zlib.decompress(base64.b64decode("eNrtmM2O0zAQx1+lypmWdmEFQuLIEYEEiANaWY4zacw6nsh22i2oV+AAEgckxAnxChwQDwSLeAtmnGSbhS6CFRJU5JTU8+Hxf362Uj9O5gZTaZJro8cJLKSpZdBohbYB3CKOJ7N9n1wYJXAUnBReOVmBKCE4rTyZc2k8kLkZEVaWIChQZ00irwoogdPUIb/KedoMW2ZoLZYiFyAK7QPOnSz7s7QuQZeAdYixU5+syfIQUx9XUSJaJQ9hrIwGG+JYrsFkjVmapVz5bh3KSO+p7K2TKbQLcGGLkwgobKHSnjNYmRpKiGXlgNzRkjG4umcrQqj2eqM/F5SeJpXqsPOoHAZUGAW77ZACCqj9XcoynUwnl1nAHI3BpXCQaQcq+M1cBVp0UTYfZFn1LP2+gVey0nbe65rkjGPq3Tg275e73K5HVDIUPH6xW2BsYufNivwpJjp92PQguVWBvdlMyRLNSKIp5/tunKWb8fhpRU/ctwl9cCaHZKCWUss2Qgt6GLBERFcxw2pkCkYcwqopVgp0GTiOt5gBP6uM1KBYq+JPhwbixPzSBGWgOl8HuTYmOeDUJ/iX0pOaA/4D/v86/pH5U3CfJjnAQPFA8f90iHOCMRxV6Hb6CB9I3t3z+Pd4reoB1wHXXcF1YUw5bm0DrQOtf+kzgfQXnkQ450fDBuUO7oHlgeUdY3lzLnN+rLgt7XXkjxd5iSZKbUC3Gn19+nzE7AvpFdiM+0Xdya7HUkfHrz98+vjiy6u3x89eJlsvRXqpjt+/+/zkzajzEY3P2Tni39HzlcJrDFjF5WEwzUsjakST9xOLKMraBF3RRqotCewVOuix34vY2IWXVgf9KJK98aXGWm9a3OlHgPmK6793Enij3T33dSju1Hmuj6C5UnW1ZSBikXOcK3q5ss8GgtnJeWMIPkub9dQB88gDBy21zXBJhikngkBaNWVRRJOTdkqWrNex74RNe4wklyaz2WQvWX8DlcHmFA==")).decode("utf-8"))
BASE_GRAFANA_VERSION = "12.4.2"
BASE_DATASOURCE = {"name": "Prometheus", "uid": "prometheus", "type": "prometheus"}
BASE_PROVIDER = {"name": "vllm-pd", "folder": "vLLM P/D", "type": "file", "disableDeletion": "true",
                 "allowUiUpdates": "false", "updateIntervalSeconds": "30", "path": "/etc/grafana/dashboards",
                 "foldersFromFilesStructure": "false"}
BASE_FOLDER = "vLLM P/D"
K8S_VERSIONS = ("v1beta1", "v0alpha1", "v2beta1")
OVERVIEW_STATS = (801, 802, 803, 804, 805, 806, 808, 809, 810, 811, 813, 814, 815, 816, 817, 818)
KEY_LABELS = ("model_name", "pd_instance", "role", "job", "node", "api_server", "a_order", "engine")
MISSING_CHECK = ("model_name", "pd_instance", "role")
CLUSTER_LIKE = re.compile(r"(?i)clu|集群|region|zone|^az$|idc|^dc$|datacenter|env|tenant|deploy|namespace|^site$|"
                          r"^cell$|^group$|^pool$|^service$|^svc$|^app$|^project$|^biz")
GROUPS = (("vLLM", "vllm:.+"), ("NPU", "(machine_|container_)?npu_.+"),
          ("Mooncake", "mooncake_.+|master_.+|segment_.+|transfer_engine_.+"), ("主机", "node_.+"))
# job fields that are compared elsewhere or carry credentials
JOB_SKIP = ("job_name", "static_configs", "relabel_configs", "metric_relabel_configs")
SECRET_KEYS = re.compile(r"(?i)password|secret|token|credential|authorization|basic_auth|oauth2|api_key|bearer")


# ---------------------------------------------------------------- output helpers
def utf8_stdio():
    """Python 3.6 under a C/POSIX locale writes ASCII to the terminal; the messages here are Chinese."""
    for name in ("stdout", "stderr"):
        stream = getattr(sys, name)
        if hasattr(stream, "buffer") and (stream.encoding or "").lower().replace("-", "") != "utf8":
            setattr(sys, name, io.TextIOWrapper(stream.buffer, encoding="utf-8", line_buffering=True))


OCTET = r"(?:25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)"
IPV4 = re.compile(r"(?<!\d)(?<!\d\.)(%s)([.\-_])(%s)\2(%s)\2(%s)(?!\d)(?!\.\d)" % (OCTET, OCTET, OCTET, OCTET))
IPV6 = re.compile(r"(?<![\w:])((?:[0-9A-Fa-f]{0,4}:){2,7}[0-9A-Fa-f]{0,4})(?![\w:])")


class Masker:
    """The same IP address always becomes the same host-NN; dashed forms (h-10-0-1-1) count as the address."""

    def __init__(self, on):
        self.on = on
        self.names = {}

    def name(self, ip, v6=False):
        if ip not in self.names:
            self.names[ip] = ("host6-%02d" if v6 else "host-%02d") % (len(self.names) + 1)
        return self.names[ip]

    def text(self, s):
        if not self.on or not isinstance(s, str):
            return s
        s = IPV4.sub(lambda m: self.name(".".join(m.group(i) for i in (1, 3, 4, 5))), s)

        def v6(m):
            t = m.group(1)
            if "::" in t or t.count(":") >= 5:
                if re.match(r"^[\d:]+$", t) and "::" not in t:
                    return t
                return self.name(t.lower(), True)
            return t
        return IPV6.sub(v6, s)

    def walk(self, x):
        if isinstance(x, str):
            return self.text(x)
        if isinstance(x, list):
            return [self.walk(v) for v in x]
        if isinstance(x, dict):
            return {self.walk(k): self.walk(v) for k, v in x.items()}
        return x


def clean_url(u):
    """The URL without user:password@, query or fragment."""
    p = urllib.parse.urlsplit(u)
    host = p.hostname or ""
    if p.port:
        host += ":%d" % p.port
    return urllib.parse.urlunsplit((p.scheme, host, p.path.rstrip("/"), "", ""))


def short(s, n=70):
    s = " ".join(str(s).split())
    return s if len(s) <= n else s[:n - 1] + "…"


def compress_values(values, limit=20):
    """['P0', 'P1', 'P2', 'D0'] -> 'P0–P2、D0'; at most `limit` items, then the count of the rest."""
    parsed, plain = [], []
    for v in values:
        m = re.match(r"^(.*?)(\d+)$", v)
        if m:
            parsed.append((m.group(1), m.group(2)))
        else:
            plain.append(v)
    padded = set((pre, len(d)) for pre, d in parsed if len(d) > 1 and d.startswith("0"))
    runs = {}
    for pre, d in parsed:  # zero-padded numbers (00..63) form runs of their own width
        width = len(d) if (pre, len(d)) in padded else 0
        runs.setdefault((pre, width), []).append(int(d))
    items = []
    for pre, width in sorted(runs):
        nums = sorted(set(runs[(pre, width)]))
        fmt = lambda n: pre + str(n).zfill(width)
        i = 0
        while i < len(nums):
            j = i
            while j + 1 < len(nums) and nums[j + 1] == nums[j] + 1:
                j += 1
            items.append(fmt(nums[i]) if i == j else "%s–%s" % (fmt(nums[i]), fmt(nums[j])))
            i = j + 1
    items += sorted(plain)
    if len(items) > limit:
        return "、".join(items[:limit]) + "…另 %d 项见 JSON" % (len(items) - limit)
    return "、".join(items)


# ---------------------------------------------------------------- HTTP
class Unreachable(Exception):
    pass


class Client:
    def __init__(self, what, url, auth):
        self.what, self.url, self.auth = what, url.rstrip("/"), auth

    def get(self, path, params=None, auth=True, timeout=60):
        """(status, parsed JSON or None). Connection problems raise Unreachable."""
        full = self.url + path + ("?" + urllib.parse.urlencode(params, doseq=True) if params else "")
        req = urllib.request.Request(full)
        req.add_header("Accept", "application/json")
        if auth and self.auth:
            req.add_header("Authorization", self.auth)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                body = resp.read().decode("utf-8", "replace")
                try:
                    return resp.status, json.loads(body)
                except ValueError:
                    return resp.status, None
        except urllib.error.HTTPError as e:
            try:
                data = json.loads(e.read().decode("utf-8", "replace"))
            except ValueError:
                data = None
            return e.code, data
        except (urllib.error.URLError, OSError) as e:
            raise Unreachable("连不上 %s %s：%s" % (self.what, clean_url(self.url), getattr(e, "reason", e)))


def make_auth(token, user, password_env, label):
    if token:
        return "Bearer " + token
    if not user:
        return None
    password = os.environ.get(password_env)
    if password is None:
        password = getpass.getpass("%s password for %s: " % (label, user))
    return "Basic " + base64.b64encode(("%s:%s" % (user, password)).encode("utf-8")).decode("ascii")


def why(code, data):
    msg = (data or {}).get("message") or (data or {}).get("error") or ""
    if code == 401:
        return "账号或密码不对（HTTP 401）"
    if code == 403:
        return "账号没有权限（HTTP 403%s）" % ("：" + short(msg, 60) if msg else "")
    return "HTTP %s%s" % (code, "：" + short(msg, 60) if msg else "")


# ---------------------------------------------------------------- report
class Report:
    GROUPS = ("和基线一致", "有差异", "基线没有的现场新增", "现场缺少")

    def __init__(self):
        self.items = {g: [] for g in self.GROUPS}
        self.unreachable = []
        self.facts = []          # lines of the 现场 label section
        self.notes = []          # lines of the 本机导出 section
        self.export = {}         # export.json of panel1-export/ (not masked)
        self.files = {}          # other files of panel1-export/: relative path -> text or bytes
        self.data = {}
        self.dirs = []           # (side, spec, files, data) of the directory checks
        self.ctx = {}            # what the directory checks need from the API parts
        self.part = "Grafana"    # the part the next add() belongs to
        self.entries = []        # (group, part, text) in order; differences are numbered in render_short()

    def add(self, group, text):
        self.items[group].append(text)
        self.entries.append((group, self.part, text))

    def miss(self, part, reason):
        self.unreachable.append({"part": part, "reason": reason})
        self.entries.append(("读不到", "读不到", "%s：%s" % (part, reason)))


# ---------------------------------------------------------------- Grafana
def all_panels(d):
    for p in d.get("panels", []):
        yield p
        for q in p.get("panels") or []:
            yield q


def state_of(d):
    """Whether the dashboard has the four changes of update_panel1.py (as diag_panel1.py reports it)."""
    ps = {p.get("id"): p for p in all_panels(d)}
    rows = [i for i in (900, 950) if ps.get(i, {}).get("type") == "row"]
    merged = [i for i in range(101, 107) if i in ps and ps[i].get("title", "").endswith("p50 / p90 / p99")]
    sparks = sum(1 for i in OVERVIEW_STATS if i in ps and ps[i].get("type") == "stat"
                 and ps[i].get("options", {}).get("graphMode", "area") != "none")
    hm = next((p for p in ps.values() if p.get("type") == "heatmap" and p.get("title", "").startswith("AICore")), {})
    scheme = hm.get("options", {}).get("color", {}).get("scheme")
    tps = 111 in ps
    done = len(rows) == 2 and merged and sparks == 0 and scheme == "RdYlGn" and tps
    none = not rows and not merged and sparks > 0 and scheme != "RdYlGn" and not tps
    return "改动后" if done else ("改动前" if none else "部分改动")


def parse_providers(text):
    """Dashboard providers from a provisioning YAML (as diag_panel1.py reads them)."""
    provs, cur = [], None
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].rstrip()
        m = re.match(r"^\s*-\s*name:\s*(.*)$", line)
        if m:
            cur = {"name": m.group(1).strip().strip("'\"")}
            provs.append(cur)
            continue
        m = re.match(r"^\s*(type|folder|path|allowUiUpdates|updateIntervalSeconds|disableDeletion|foldersFromFilesStructure):\s*(.*)$", line)
        if m and cur is not None:
            cur[m.group(1)] = m.group(2).strip().strip("'\"")
    return provs


def k8s_annotations(g, uid, namespace):
    for v in K8S_VERSIONS:
        code, data = g.get("/apis/dashboard.grafana.app/%s/namespaces/%s/dashboards/%s" % (v, namespace, uid))
        if code == 200 and data:
            ann = (data.get("metadata") or {}).get("annotations") or {}
            return {k.replace("grafana.app/", ""): val for k, val in ann.items()
                    if any(s in k for s in ("managedBy", "managerId", "sourcePath", "sourceChecksum"))}
    return None


def load_mode(meta, ann):
    """(mode text, allowUiUpdates as Grafana shows it or None) from meta.provisioned and the k8s annotations."""
    managed = (ann or {}).get("managedBy")
    if meta.get("provisioned"):
        return "文件预置", "false"
    if managed == "classic-file-provisioning":
        return "文件预置", "true"
    if managed:
        return "由 %s 管理" % managed, None
    if ann is None:
        return "非预置，或文件预置且 allowUiUpdates=true（取不到 k8s 注解，分不清）", None
    return "API 或界面保存（非预置）", None


def full_path(path):
    return ".".join(str(x) for x in path)


def targets_of(p):
    return {t.get("refId", "#%d" % i): t for i, t in enumerate(p.get("targets") or [])}


def compare_board(b, u):
    """Panel-level differences between two normal forms."""
    out = {"added": [], "removed": [], "title": [], "type": [], "query": [], "moved": [], "other": {}, "dash": []}

    def row_title(norm, pid):
        r = norm["panels"][pid].get(ROW_KEY)
        return norm["panels"][r].get("title", "") if r in norm["panels"] else "顶部"

    for pid in sorted(set(u["panels"]) - set(b["panels"])):
        p = u["panels"][pid]
        out["added"].append({"id": pid, "type": p.get("type"), "title": p.get("title", ""),
                             "row": row_title(u, pid) if p.get("type") != "row" else None})
    for pid in sorted(set(b["panels"]) - set(u["panels"])):
        p = b["panels"][pid]
        out["removed"].append({"id": pid, "type": p.get("type"), "title": p.get("title", "")})
    for pid in sorted(set(b["panels"]) & set(u["panels"])):
        bp, up = b["panels"][pid], u["panels"][pid]
        if bp.get("title") != up.get("title"):
            out["title"].append({"id": pid, "base": bp.get("title"), "site": up.get("title")})
        if bp.get("type") != up.get("type"):
            out["type"].append({"id": pid, "base": bp.get("type"), "site": up.get("type")})
        if bp.get(ROW_KEY) != up.get(ROW_KEY):
            out["moved"].append({"id": pid, "base_row": bp.get(ROW_KEY), "site_row": up.get(ROW_KEY)})
        bt, ut = targets_of(bp), targets_of(up)
        for ref in sorted(set(bt) | set(ut)):
            if ref not in ut:
                out["query"].append({"id": pid, "ref": ref, "what": "现场没有这个查询", "base": bt[ref].get("expr")})
            elif ref not in bt:
                out["query"].append({"id": pid, "ref": ref, "what": "现场多出的查询", "site": ut[ref].get("expr")})
            else:
                fields = sorted(set(full_path(path) for path, _ in diff(bt[ref], ut[ref])))
                if fields:
                    e = {"id": pid, "ref": ref, "what": "、".join(fields)}
                    if bt[ref].get("expr") != ut[ref].get("expr"):
                        e["base"], e["site"] = bt[ref].get("expr"), ut[ref].get("expr")
                    out["query"].append(e)
        rest = lambda p: {k: v for k, v in p.items() if k not in ("title", "type", "targets", ROW_KEY)}
        paths = [full_path(path) for path, _ in diff(rest(bp), rest(up))]
        if paths:
            out["other"][pid] = paths
    bd = {k: v for k, v in b["dash"].items() if k != "templating"}
    ud = {k: v for k, v in u["dash"].items() if k != "templating"}
    out["dash"] = [full_path(path) for path, _ in diff(bd, ud)]
    return out


def expr_change(a, b):
    """The changed middle of two queries with a little context: 基线「…x…」，现场「…y…」."""
    a, b = a or "", b or ""
    k = 0
    while k < min(len(a), len(b)) and a[k] == b[k]:
        k += 1
    m = 0
    while m < min(len(a), len(b)) - k and a[-1 - m] == b[-1 - m]:
        m += 1
    ctx = max(0, k - 12)
    part = lambda s: ("…" if ctx else "") + short(s[ctx:len(s) - m], 40) + ("…" if m else "")
    return "基线「%s」，现场「%s」" % (part(a), part(b))


def variables(d):
    return {v.get("name"): v for v in ((d.get("templating") or {}).get("list") or [])}


def compare_variables(bd, ud):
    """Variable definitions by name; the selected value and the option list are what a viewer last saw, not compared."""
    out = {"added": [], "removed": [], "changed": []}
    bv, uv = variables(bd), variables(ud)
    strip = lambda v: {k: x for k, x in v.items() if k not in ("current", "options")}
    for n in sorted(set(uv) - set(bv), key=str):
        out["added"].append({"name": n, "type": uv[n].get("type"), "query": uv[n].get("query")})
    for n in sorted(set(bv) - set(uv), key=str):
        out["removed"].append({"name": n, "type": bv[n].get("type")})
    for n in sorted(set(bv) & set(uv), key=str):
        fields = sorted(set(full_path(p) for p, _ in diff(strip(bv[n]), strip(uv[n]))))
        if fields:
            out["changed"].append({"name": n, "fields": fields, "base": strip(bv[n]), "site": strip(uv[n])})
    return out


def count_ops(b, u):
    n = len(diff(b["dash"], u["dash"]))
    n += len(set(b["panels"]) ^ set(u["panels"]))
    for pid in set(b["panels"]) & set(u["panels"]):
        n += len(diff(b["panels"][pid], u["panels"][pid]))
    return n


CLUSTER_WORD = re.compile(r"\b(cluster|model_group)\b")


def panel_json(norm, pid):
    p = {k: v for k, v in norm["panels"][pid].items() if k != ROW_KEY}
    return dict(p, id=pid)


def export_board(rep, side, uid, raw0, meta, ann, ref, ref_name, u0):
    """The board as read (unmasked) and what the site changed against the baseline, for the full update."""
    copy_path = "grafana/dashboards/%s.json" % uid
    rep.files[copy_path] = json.dumps(raw0, ensure_ascii=False, indent=2) + "\n"
    cmp0 = compare_board(ref, u0)
    var0 = compare_variables(ref["dash"], u0["dash"])
    tq = {(q["id"], q["ref"]): q for q in cmp0["query"]}
    with_cluster = []
    for pid, p in sorted(u0["panels"].items()):
        for t in p.get("targets") or []:
            if CLUSTER_WORD.search(t.get("expr") or ""):
                with_cluster.append({"panel": pid, "ref": t.get("refId"), "expr": t.get("expr")})
    for v in (u0["dash"].get("templating") or {}).get("list") or []:
        if CLUSTER_WORD.search(json.dumps(v.get("query"), ensure_ascii=False)) or v.get("name") in ("cluster", "model_group"):
            with_cluster.append({"variable": v.get("name"), "query": v.get("query")})
    rep.export["grafana"]["dashboards"].append({
        "side": side, "uid": uid, "title": raw0.get("title"), "version": raw0.get("version"),
        "folder": meta.get("folderTitle"), "folderUid": meta.get("folderUid"), "provisioned": meta.get("provisioned"),
        "provisionedExternalId": meta.get("provisionedExternalId") or None,
        "managedBy": (ann or {}).get("managedBy"), "managerId": (ann or {}).get("managerId"),
        "sourcePath": (ann or {}).get("sourcePath"), "host_file": None, "copy": copy_path,
        "compared_with": ref_name,
        "user_changes": {
            "panels_added": [dict(panel_json(u0, a["id"]), _row=u0["panels"][a["id"]].get(ROW_KEY)) for a in cmp0["added"]],
            "panels_removed": [{"id": r["id"], "type": r["type"], "title": r["title"]} for r in cmp0["removed"]],
            "titles_changed": cmp0["title"], "types_changed": cmp0["type"], "rows_changed": cmp0["moved"],
            "queries_changed": [{"panel": q["id"], "ref": q["ref"], "what": q["what"],
                                 "base": next((t for t in ref["panels"].get(q["id"], {}).get("targets") or [] if t.get("refId") == q["ref"]), None),
                                 "site": next((t for t in u0["panels"][q["id"]].get("targets") or [] if t.get("refId") == q["ref"]), None)
                                 if q["id"] in u0["panels"] else None} for q in tq.values()],
            "other_fields": {str(k): v for k, v in cmp0["other"].items()},
            "dashboard_fields": cmp0["dash"],
            "variables_added": [variables(u0["dash"])[v["name"]] for v in var0["added"]],
            "variables_removed": [v["name"] for v in var0["removed"]],
            "variables_changed": [{"name": v["name"], "fields": v["fields"], "site": variables(u0["dash"])[v["name"]]}
                                  for v in var0["changed"]]},
        "queries_with_cluster": with_cluster})


def probe_grafana(args, rep, mask):
    g = Client("Grafana", args.grafana_url, make_auth(args.grafana_token, args.grafana_user, "GRAFANA_PASSWORD", "Grafana"))
    G = rep.data.setdefault("grafana", {"url": mask.text(clean_url(args.grafana_url))})
    code, health = g.get("/api/health", auth=False)
    health = health or {}
    G["health"] = {k: health.get(k) for k in ("version", "commit", "database")}
    ver = health.get("version")
    if ver == BASE_GRAFANA_VERSION:
        rep.add("和基线一致", "Grafana %s" % ver)
    else:
        rep.add("有差异", "Grafana 版本：基线 %s，现场 %s" % (BASE_GRAFANA_VERSION, ver or "取不到（HTTP %s）" % code))
    if g.auth:
        code, data = g.get("/api/user")
        if code != 200:
            rep.miss("Grafana 看板、数据源", why(code, data))
            return
    else:
        code, data = g.get("/api/search", {"limit": 1})
        if code in (401, 403):
            rep.miss("Grafana 看板、数据源", "没给账号（GRAFANA_USER 或 GRAFANA_TOKEN），Grafana 不许匿名读取（HTTP %s）" % code)
            return
    code, fs = g.get("/api/frontend/settings")
    fs = fs if code == 200 and isinstance(fs, dict) else {}
    namespace = fs.get("namespace") or "default"

    # dashboards that belong to panel 1: the two uids, copies of them, the v3 folder
    code, found = g.get("/api/search", {"type": "dash-db", "limit": 5000})
    related = []
    if code == 200 and isinstance(found, list):
        for h in found:
            t = h.get("title") or ""
            if (h.get("uid") or "").startswith("vllm-pd") or "面板一" in t or t.startswith("vLLM 服务侧监控面板") \
                    or h.get("folderTitle") == BASE_FOLDER:
                related.append({"uid": h.get("uid"), "title": t, "folder": h.get("folderTitle") or "General"})
    else:
        rep.miss("Grafana 看板列表", why(code, found))
    G["related_boards"] = related
    for h in related:
        if h["uid"] not in args.uids:
            rep.add("基线没有的现场新增", "看板 %s「%s」（文件夹 %s）" % (h["uid"], short(h["title"], 40), h["folder"]))

    base_raw = baseline()
    base = {s: normalize(base_raw[s]) for s in ("P", "D")}
    pre = apply_lines(base, BEFORE_CHANGES_LINES)
    G["baseline"] = {"base": digest(base), "P": board_digest(base["P"]), "D": board_digest(base["D"]),
                     "before_changes": {"P": board_digest(pre["P"]), "D": board_digest(pre["D"])}}
    user, user0, metas, boards = {}, {}, {}, {}
    XG = rep.export.setdefault("grafana", {"url": clean_url(args.grafana_url), "version": ver, "dashboards": []})
    for side, uid in zip(("P", "D"), args.uids):
        code, res = g.get("/api/dashboards/uid/%s" % uid)
        B = boards.setdefault(side, {"uid": uid})
        if code == 404:
            rep.add("现场缺少", "%s 看板 %s（Grafana 里没有这个 uid）" % (side, uid))
            B["error"] = "not found"
            continue
        if code != 200 or not res:
            rep.miss("%s 看板 %s" % (side, uid), why(code, res))
            B["error"] = why(code, res)
            continue
        meta = res.get("meta") or {}
        try:
            raw = classic(res, "看板 %s" % uid)
        except Fail as e:
            rep.miss("%s 看板 %s" % (side, uid), str(e))
            B["error"] = str(e)
            continue
        raw0, raw = raw, mask.walk(raw)
        user0[side] = normalize(raw0)
        rep.ctx.setdefault("live", {})[side] = user0[side]
        ann = k8s_annotations(g, uid, namespace)
        mode, allow = load_mode(meta, ann)
        B.update({"title": raw.get("title"), "version": raw.get("version"), "updated": meta.get("updated"),
                  "updatedBy": meta.get("updatedBy"), "folder": meta.get("folderTitle"), "provisioned": meta.get("provisioned"),
                  "provisionedExternalId": meta.get("provisionedExternalId"), "annotations": ann, "mode": mode,
                  "allowUiUpdates_shown": allow, "changes_state": state_of(raw)})
        metas[side] = meta
        user[side] = normalize(raw)
        # how the board is loaded
        prov_bad = []
        if mode != "文件预置":
            prov_bad.append("加载方式 基线 文件预置，现场 %s" % mode)
        else:
            if allow != BASE_PROVIDER["allowUiUpdates"]:
                prov_bad.append("allowUiUpdates 基线 false，现场 %s（由 provisioned=%s 推断）" % (allow, meta.get("provisioned")))
            mid = (ann or {}).get("managerId")
            if mid and mid != BASE_PROVIDER["name"]:
                prov_bad.append("provider 基线 %s，现场 %s" % (BASE_PROVIDER["name"], mid))
        if (meta.get("folderTitle") or "General") != BASE_FOLDER:
            prov_bad.append("文件夹 基线 %s，现场 %s" % (BASE_FOLDER, meta.get("folderTitle") or "General"))
        rep.part = "Grafana"
        head = "%s 看板 %s v%s（update_panel1.py 四处改动：%s）" % (side, uid, raw.get("version"), B["changes_state"])
        if prov_bad:
            rep.add("有差异", "%s：%s" % (head, "；".join(prov_bad)))
        else:
            rep.add("和基线一致", "%s：文件预置，provider %s，allowUiUpdates=false，文件夹 %s" % (
                head, (ann or {}).get("managerId") or BASE_PROVIDER["name"], BASE_FOLDER))

        rep.part = "看板"
        # content: against the baseline, or against the state before the four changes when that is closer
        n_post, n_pre = count_ops(base[side], user[side]), count_ops(pre[side], user[side])
        ref, ref_name = (pre[side], "update_panel1.py 改动前的状态") if n_pre < n_post else (base[side], "基线")
        B["fields_differing"] = {"baseline": n_post, "before_changes": n_pre}
        B["compared_with"] = ref_name
        cmp_ = compare_board(ref, user[side])
        var = compare_variables(ref["dash"], user[side]["dash"])
        B["panels"], B["variables"] = cmp_, var
        export_board(rep, side, uid, raw0, meta, ann, ref, "before_changes" if ref is pre else "baseline", user0[side])
        B["queries_with_cluster"] = mask.walk(rep.export["grafana"]["dashboards"][-1]["queries_with_cluster"])
        n_panels = sum(1 for p in user[side]["panels"].values() if p.get("type") != "row")
        if n_pre == 0:
            rep.add("有差异", "%s 看板内容等于 11-11 校准的改动前状态：update_panel1.py 的四处改动没有生效（与基线差 %d 个字段）" % (side, n_post))
        elif ref is pre:
            rep.add("有差异", "%s 看板最接近 update_panel1.py 改动前的状态（差 %d 个字段，与基线差 %d 个），下面按改动前的状态比" % (side, n_pre, n_post))
        if n_post == 0:
            rep.add("和基线一致", "%s 看板内容：%d 个 panel、标题、查询、%d 个变量、其他字段都与基线相同" % (
                side, n_panels, len(variables(user[side]["dash"]))))
            continue
        if n_pre == 0:
            continue
        for a in cmp_["added"]:
            rep.add("基线没有的现场新增", "%s/%s %s「%s」%s" % (side, a["id"], a["type"], short(a["title"], 40),
                                                       "（行「%s」）" % short(a["row"], 16) if a["row"] else ""))
        for r in cmp_["removed"]:
            rep.add("现场缺少", "%s/%s %s「%s」" % (side, r["id"], r["type"], short(r["title"], 40)))
        for t in cmp_["title"]:
            rep.add("有差异", "%s/%s 标题：基线「%s」，现场「%s」" % (side, t["id"], short(t["base"], 30), short(t["site"], 30)))
        for t in cmp_["type"]:
            rep.add("有差异", "%s/%s 类型：基线 %s，现场 %s" % (side, t["id"], t["base"], t["site"]))
        for q in cmp_["query"]:
            if q["what"] == "现场没有这个查询":
                rep.add("现场缺少", "%s/%s 查询 %s" % (side, q["id"], q["ref"]))
            elif q["what"] == "现场多出的查询":
                rep.add("基线没有的现场新增", "%s/%s 查询 %s：%s" % (side, q["id"], q["ref"], short(q.get("site"), 60)))
            else:
                rep.add("有差异", "%s/%s 查询 %s：%s 不同%s" % (side, q["id"], q["ref"], q["what"],
                                                       "，" + expr_change(q["base"], q["site"]) if "site" in q else ""))
        if cmp_["moved"]:
            rep.add("有差异", "%s/%s 所在的行不同" % (side, ids_text([m["id"] for m in cmp_["moved"]])))
        if cmp_["other"]:
            n = sum(len(v) for v in cmp_["other"].values())
            rep.add("有差异", "%s 其他字段 %d 处，在 panel %s（逐项见 JSON）" % (side, n, ids_text(list(cmp_["other"]))))
        if cmp_["dash"]:
            rep.add("有差异", "%s 看板级字段：%s" % (side, short("、".join(cmp_["dash"]), 80)))
        for v in var["added"]:
            rep.add("基线没有的现场新增", "%s 变量 %s（%s）" % (side, v["name"], v["type"]))
        for v in var["removed"]:
            rep.add("现场缺少", "%s 变量 %s" % (side, v["name"]))
        for v in var["changed"]:
            rep.add("有差异", "%s 变量 %s：%s 不同" % (side, v["name"], "、".join(v["fields"])))
    rep.part = "Grafana"
    G["boards"] = boards
    if len(user) == 2:
        # the same file diff_panel1.py writes: diff_panel1.py --apply rebuilds the dashboards read here
        lines, stats = make_lines(base, user)
        head = ["# base %s fmt %d" % (digest(base), FORMAT)] + [summary(s, stats, metas.get(s) or {}, board_digest(user[s]))
                                                             for s in ("P", "D")]
        G["diff_text"] = "\n".join(head + lines) + "\n"
        lines0, stats0 = make_lines(base, user0)
        head0 = ["# base %s fmt %d" % (digest(base), FORMAT)] + [summary(s, stats0, metas.get(s) or {}, board_digest(user0[s]))
                                                              for s in ("P", "D")]
        rep.files["panel1-diff.txt"] = "\n".join(head0 + lines0) + "\n"
        XG["diff_file"] = "panel1-diff.txt"

    # datasources
    code, dss = g.get("/api/datasources")
    if code == 200 and isinstance(dss, list):
        src = "api"
    else:
        src = "frontend/settings"
        dss = [dict(v, name=k) for k, v in (fs.get("datasources") or {}).items()
               if v.get("type") not in ("grafana", "datasource", "dashboard", "mixed")]
        if not dss:
            rep.miss("Grafana 数据源", why(code, None) if code != 200 else "列表为空")
    XG["datasources"] = [{"name": ds.get("name"), "uid": ds.get("uid"), "type": ds.get("type"), "url": ds.get("url"),
                          "access": ds.get("access"), "isDefault": ds.get("isDefault"), "basicAuth": ds.get("basicAuth"),
                          "basicAuthUser": ds.get("basicAuthUser") or None, "user": ds.get("user") or None,
                          "jsonData": ds.get("jsonData") or {}, "readOnly": ds.get("readOnly"),
                          "secureJsonFields": sorted(k for k, v in (ds.get("secureJsonFields") or {}).items() if v)}
                         for ds in dss]
    XG["datasources_source"] = src
    out = []
    for ds in dss:
        jd = ds.get("jsonData") or {}
        out.append({"name": ds.get("name"), "uid": ds.get("uid"), "type": ds.get("type"), "url": mask.text(ds.get("url")),
                    "isDefault": ds.get("isDefault"), "access": ds.get("access"),
                    "timeInterval": jd.get("timeInterval"), "httpMethod": jd.get("httpMethod")})
    G["datasources"] = {"source": src, "list": out}
    hit = next((d for d in out if d["uid"] == BASE_DATASOURCE["uid"]), None)
    if hit and all(hit[k] == v for k, v in BASE_DATASOURCE.items()):
        rep.add("和基线一致", "数据源 %s（uid %s，%s，timeInterval %s）" % (hit["name"], hit["uid"], hit["type"], hit["timeInterval"]))
    elif hit:
        rep.add("有差异", "数据源 uid %s：基线 %s/%s，现场 %s/%s" % (hit["uid"], BASE_DATASOURCE["name"], BASE_DATASOURCE["type"],
                                                               hit["name"], hit["type"]))
    elif out:
        rep.add("现场缺少", "数据源 uid prometheus（看板的 datasource 变量默认用它）")
    for d in out:
        if d is not hit:
            rep.add("基线没有的现场新增", "数据源 %s（uid %s，%s）" % (d["name"], d["uid"], d["type"]))

    if args.provisioning_dir:
        provisioning_files(args.provisioning_dir, rep, G, mask)


def provisioning_files(dirname, rep, G, mask):
    files = sorted(set(glob.glob(os.path.join(dirname, "dashboards", "*.y*ml")) + glob.glob(os.path.join(dirname, "*.y*ml"))))
    if not os.path.isdir(dirname):
        rep.miss("预置配置文件", "%s 不是目录（要在 Grafana 所在的机器上运行）" % mask.text(dirname))
        return
    if not files:
        rep.miss("预置配置文件", "%s 下没有 dashboards/*.yaml" % mask.text(dirname))
        return
    provs = []
    XP = rep.export["grafana"].setdefault("provisioning", {"dir": os.path.abspath(dirname), "files": []})
    for f in files:
        try:
            with open(f, "rb") as fh:
                data = fh.read()
            text = data.decode("utf-8")
            parsed = parse_providers(text)
            copy_path = "grafana/provisioning/" + os.path.relpath(f, dirname).replace(os.sep, "/")
            rep.files[copy_path] = data
            for p in parsed:
                provs.append(dict(p, file=os.path.basename(f)))
            XP["files"].append({"path": os.path.abspath(f), "copy": copy_path, "providers": parsed})
        except (OSError, UnicodeDecodeError) as e:
            rep.miss("预置配置文件 %s" % os.path.basename(f), str(e))
    # the files the dashboards come from, on this machine: under the provider path, or the dashboards folder next to
    # the provisioning folder (a container path is not visible on the host)
    want = {b["uid"]: b for b in rep.export["grafana"]["dashboards"]}
    for p in provs:
        cands = [p.get("path") or "", os.path.join(os.path.dirname(os.path.abspath(dirname)), "dashboards")]
        where = next((c for c in cands if c and os.path.isdir(c)), None)
        if where is None:
            continue
        for jf in sorted(glob.glob(os.path.join(where, "**", "*.json"), recursive=True)):
            try:
                with open(jf, encoding="utf-8") as fh:
                    d = json.load(fh)
            except (OSError, ValueError):
                continue
            d = d.get("dashboard", d) if isinstance(d, dict) else {}
            if d.get("uid") in want and not want[d["uid"]]["host_file"]:
                want[d["uid"]]["host_file"] = os.path.abspath(jf)
    G["providers"] = mask.walk(provs)
    mine = [p for p in provs if p.get("name") == BASE_PROVIDER["name"]]
    for p in provs:
        if p.get("name") != BASE_PROVIDER["name"]:
            rep.add("基线没有的现场新增", "看板 provider %s（%s，path=%s）" % (p.get("name"), p["file"], mask.text(p.get("path"))))
    if not mine:
        rep.add("现场缺少", "看板 provider %s（预置文件里没有）" % BASE_PROVIDER["name"])
        return
    p = mine[0]
    bad = ["%s 基线 %s，现场 %s" % (k, v, p.get(k, "（未写）")) for k, v in BASE_PROVIDER.items()
           if k != "name" and p.get(k, {"allowUiUpdates": "false", "disableDeletion": "false",
                                         "updateIntervalSeconds": "10", "foldersFromFilesStructure": "false"}.get(k)) != v]
    if bad:
        rep.add("有差异", "预置文件 %s 的 provider %s：%s" % (p["file"], p["name"], "；".join(bad)))
    else:
        rep.add("和基线一致", "预置文件 %s 的 provider %s：%s" % (p["file"], p["name"], "，".join(
            "%s=%s" % (k, v) for k, v in BASE_PROVIDER.items() if k in ("allowUiUpdates", "updateIntervalSeconds", "path"))))


# ---------------------------------------------------------------- Prometheus configuration (YAML)
def _strip_comment(s):
    q = None
    for i, ch in enumerate(s):
        if q:
            if ch == q:
                q = None
        elif ch in "'\"":
            q = ch
        elif ch == "#" and (i == 0 or s[i - 1] in " \t"):
            return s[:i]
    return s


def _scalar(s):
    s = s.strip()
    if s[:1] == "'" and s[-1:] == "'" and len(s) > 1:
        return s[1:-1].replace("''", "'")
    if s[:1] == '"' and s[-1:] == '"' and len(s) > 1:
        try:
            return json.loads(s)
        except ValueError:
            return s[1:-1]
    if s in ("true", "True"):
        return True
    if s in ("false", "False"):
        return False
    if s in ("null", "~", ""):
        return None
    if re.match(r"^-?\d+$", s):
        return int(s)
    if re.match(r"^-?\d+\.\d+$", s):
        return float(s)
    return s


def _flow(s, i):
    """Value of a flow collection or scalar starting at s[i]: (value, next index)."""
    while i < len(s) and s[i] == " ":
        i += 1
    if s[i] in "[{":
        close = "]" if s[i] == "[" else "}"
        out = [] if close == "]" else {}
        i += 1
        while True:
            while i < len(s) and s[i] in " ,":
                i += 1
            if i >= len(s):
                raise ValueError("unclosed flow collection")
            if s[i] == close:
                return out, i + 1
            if close == "]":
                v, i = _flow(s, i)
                out.append(v)
            else:
                k, i = _flow_token(s, i, ":")
                i += 1
                v, i = _flow(s, i)
                out[_scalar(k)] = v
    tok, i = _flow_token(s, i, ",]}")
    return _scalar(tok), i


def _flow_token(s, i, stops):
    j = i
    if s[i] in "'\"":
        q = s[i]
        j = i + 1
        while j < len(s):
            if s[j] == q and not (q == "'" and s[j + 1:j + 2] == "'"):
                break
            j += 2 if (q == '"' and s[j] == "\\") or (q == "'" and s[j:j + 2] == "''") else 1
        return s[i:j + 1], j + 1
    while j < len(s) and s[j] not in stops:
        j += 1
    return s[i:j].strip(), j


def _value(rest):
    """A value in block context: a flow collection, or a scalar that may contain , ] }."""
    return _flow(rest, 0)[0] if rest[:1] in "[{" else _scalar(rest)


def _key_value(s):
    """'key: value' -> (key, value text); None when the line is not a mapping entry."""
    q = None
    for i, ch in enumerate(s):
        if q:
            if ch == q:
                q = None
        elif ch in "'\"" and i == 0:
            q = ch
        elif ch == ":" and (i + 1 == len(s) or s[i + 1] == " "):
            return _scalar(s[:i]), s[i + 1:].strip()
        elif ch in "[{" and i == 0:
            return None
    return None


def parse_yaml(text):
    """Block mappings and sequences, flow collections, quoted and plain scalars, literal blocks:
    enough for the configuration Prometheus prints at /api/v1/status/config."""
    lines = []
    for raw in text.split("\n"):
        s = _strip_comment(raw.rstrip("\r")).rstrip()
        if s.strip() and s.strip() != "---":
            lines.append([len(s) - len(s.lstrip(" ")), s.strip(), raw])

    def block(i, ind):
        if lines[i][1] == "-" or lines[i][1].startswith("- "):
            return seq(i, ind)
        if _key_value(lines[i][1]) is None:
            return _value(lines[i][1]), i + 1
        return mapping(i, ind)

    def seq(i, ind):
        out = []
        while i < len(lines) and lines[i][0] == ind and (lines[i][1] == "-" or lines[i][1].startswith("- ")):
            rest = lines[i][1][1:].lstrip()
            if not rest:
                i += 1
                if i < len(lines) and lines[i][0] > ind:
                    v, i = block(i, lines[i][0])
                else:
                    v = None
            elif _key_value(rest) is not None:
                lines[i] = [ind + len(lines[i][1]) - len(rest), rest, lines[i][2]]
                v, i = mapping(i, lines[i][0])
            else:
                v = _value(rest)
                i += 1
            out.append(v)
        return out, i

    def mapping(i, ind):
        out = {}
        while i < len(lines) and lines[i][0] == ind and not (lines[i][1] == "-" or lines[i][1].startswith("- ")):
            kv = _key_value(lines[i][1])
            if kv is None:
                raise ValueError("cannot read line: %s" % lines[i][1])
            k, rest = kv
            i += 1
            if rest in ("|", "|-", "|+", ">", ">-", ">+"):
                body = []
                while i < len(lines) and lines[i][0] > ind:
                    body.append(lines[i][1])
                    i += 1
                out[k] = ("\n" if rest[0] == "|" else " ").join(body)
            elif rest:
                out[k] = _value(rest)
            elif i < len(lines) and (lines[i][0] > ind or (lines[i][0] == ind and (lines[i][1] == "-" or lines[i][1].startswith("- ")))):
                out[k], i = block(i, lines[i][0])
            else:
                out[k] = None
        return out, i

    if not lines:
        return {}
    v, _ = block(0, lines[0][0])
    return v


def sd_kinds(job):
    return sorted(k for k in job if k.endswith("_sd_configs") or k == "static_configs")


def static_labels(job):
    keys, values, n = set(), {}, 0
    for grp in job.get("static_configs") or []:
        n += len(grp.get("targets") or [])
        for k, v in (grp.get("labels") or {}).items():
            keys.add(k)
            values.setdefault(k, set()).add(str(v))
    return keys, values, n


def relabel_text(r):
    parts = []
    if r.get("source_labels"):
        parts.append("[%s]" % ",".join(r["source_labels"]))
    if r.get("regex") not in (None, "(.*)"):
        parts.append("regex=%s" % r.get("regex"))
    act = r.get("action", "replace")
    if act != "replace":
        parts.append(act)
    if r.get("target_label"):
        parts.append("→%s" % r["target_label"])
    if r.get("replacement") not in (None, "$1"):
        parts.append("=%s" % r.get("replacement"))
    return " ".join(parts)


def clean_job(job):
    """Job fields without credentials (only that a credential is set)."""
    out = {}
    for k, v in job.items():
        if SECRET_KEYS.search(k):
            out[k] = "（已设置）"
        elif isinstance(v, dict):
            out[k] = clean_job(v)
        else:
            out[k] = v
    return out


# ---------------------------------------------------------------- Prometheus
def probe_prometheus(args, rep, mask):
    rep.part = "Prometheus"
    p = Client("Prometheus", args.prometheus_url,
               make_auth(args.prometheus_token, args.prometheus_user, "PROMETHEUS_PASSWORD", "Prometheus"))
    R = rep.data.setdefault("prometheus", {"url": mask.text(clean_url(args.prometheus_url))})
    code, bi = p.get("/api/v1/status/buildinfo")
    if code in (401, 403):
        rep.miss("Prometheus", why(code, bi))
        return
    ver = ((bi or {}).get("data") or {}).get("version")
    R["version"] = ver
    if ver == PROM_BASELINE["version"]:
        rep.add("和基线一致", "Prometheus %s" % ver)
    else:
        rep.add("有差异", "Prometheus 版本：基线 %s，现场 %s" % (PROM_BASELINE["version"], ver or "取不到（%s）" % why(code, bi)))

    X = rep.export.setdefault("prometheus", {"url": clean_url(args.prometheus_url), "version": ver})
    code, fl = p.get("/api/v1/status/flags")
    flags = (fl or {}).get("data") if code == 200 else None
    X["flags"] = flags
    if flags is None:
        rep.miss("Prometheus 启动参数", why(code, fl))
        X["reload"] = {"web_enable_lifecycle": None, "how": "取不到启动参数"}
    else:
        on = str(flags.get("web.enable-lifecycle", "")).lower() == "true"
        X["reload"] = {"web_enable_lifecycle": on, "config_file": flags.get("config.file"),
                       "how": "POST /-/reload" if on else "向 Prometheus 进程发 SIGHUP（容器部署：docker kill -s HUP <容器>），或重启"}

    # configuration as loaded
    code, cfgr = p.get("/api/v1/status/config")
    cfg = None
    if code == 200 and cfgr:
        text = ((cfgr.get("data") or {}).get("yaml")) or ""
        rep.files["prometheus.effective.yml"] = text
        try:
            cfg = parse_yaml(text)
        except (ValueError, IndexError) as e:
            rep.miss("Prometheus 配置", "解析不了 /api/v1/status/config 的 YAML：%s" % e)
    else:
        rep.miss("Prometheus 配置", why(code, cfgr))
    rep.ctx["prom_cfg"], rep.ctx["prom_flags"] = cfg, flags
    if cfg is not None:
        refs = []
        X["config"] = {"global": scrub(cfg.get("global") or {}, ("global",), refs),
                       "rule_files": cfg.get("rule_files") or [],
                       "sections": {k: scrub(v, (k,), refs) for k, v in cfg.items() if k not in ("global", "scrape_configs", "rule_files")},
                       "scrape_configs": [scrub(j, ("scrape_configs", j.get("job_name")), refs) for j in cfg.get("scrape_configs") or []]}
        X["credentials"] = refs
    export_files(args, rep, X, cfg)
    code, tr = p.get("/api/v1/targets", {"state": "active"})
    active = ((tr or {}).get("data") or {}).get("activeTargets") if code == 200 else None
    if active is None:
        rep.miss("Prometheus 抓取目标", why(code, tr))
    X["targets"] = {}
    for t in active or []:
        X["targets"].setdefault(t.get("scrapePool") or (t.get("labels") or {}).get("job"), []).append(
            {"instance": (t.get("labels") or {}).get("instance"), "labels": t.get("labels") or {},
             "discovered": t.get("discoveredLabels") or {}, "scrape_url": t.get("scrapeUrl"),
             "health": t.get("health"), "last_error": t.get("lastError") or ""})
    pools = {}
    for t in active or []:
        pool = t.get("scrapePool") or (t.get("labels") or {}).get("job")
        e = pools.setdefault(pool, {"up": 0, "down": 0, "unknown": 0, "errors": {}, "label_keys": set()})
        h = t.get("health", "unknown")
        e[h if h in ("up", "down") else "unknown"] += 1
        if h == "down":
            err = short(mask.text(re.sub(r"https?://\S+", "<target>", t.get("lastError") or "")), 80)
            e["errors"][err] = e["errors"].get(err, 0) + 1
        e["label_keys"] |= set(k for k in (t.get("labels") or {}) if not k.startswith("__") and k not in ("job", "instance"))
    if cfg is not None:
        config_compare(cfg, pools, rep, R, mask)
    R["targets"] = {k: {"up": v["up"], "down": v["down"], "unknown": v["unknown"], "errors": v["errors"],
                        "label_keys": sorted(v["label_keys"])} for k, v in sorted(pools.items(), key=lambda kv: str(kv[0]))}
    code, rules = p.get("/api/v1/rules")
    groups = ((rules or {}).get("data") or {}).get("groups") if code == 200 else None
    if groups is None:
        rep.miss("Prometheus 规则", why(code, rules))
    else:
        X["rules"] = [{"name": g.get("name"), "file": g.get("file"), "interval": g.get("interval"),
                       "rules": [{k: r.get(k) for k in ("type", "name", "query", "duration", "labels", "annotations") if k in r}
                                 for r in g.get("rules") or []]} for g in groups]
        R["rule_groups"] = [{"name": g.get("name"), "file": mask.text(g.get("file")), "rules": len(g.get("rules") or [])}
                            for g in groups]
        if groups:
            rep.add("基线没有的现场新增", "规则组 %d 个、规则 %d 条（%s）" % (len(groups), sum(len(g.get("rules") or []) for g in groups),
                                                          short("、".join(g.get("name", "") for g in groups), 50)))
        else:
            rep.add("和基线一致", "没有加载规则（rule_files 为空）")
    series_labels(p, rep, R, cfg, mask)


SECRET_FIELD = re.compile(r"(?i)^(password|bearer_token|credentials|client_secret|secret|secrets|token|api_key|"
                          r".+_password|.+_token|.+_secret)$")


def scrub(x, path, refs):
    """A copy without passwords and tokens; refs gets where each one is set (inline, or the file it is read from)."""
    if isinstance(x, dict):
        out = {}
        for k, v in x.items():
            p = path + (k,)
            if isinstance(k, str) and k.endswith("_file") and v:
                if SECRET_KEYS.search(k) or k in ("key_file", "cert_file", "ca_file"):
                    refs.append({"path": ".".join(str(s) for s in p), "file": v})
                out[k] = v
            elif isinstance(k, str) and SECRET_FIELD.match(k) and v not in (None, "", []):
                refs.append({"path": ".".join(str(s) for s in p), "inline": True})
                out[k] = "<secret>"
            else:
                out[k] = scrub(v, p, refs)
        return out
    if isinstance(x, list):
        return [scrub(v, path + (i,), refs) for i, v in enumerate(x)]
    return x


def read_copy(path):
    with open(path, "rb") as f:
        return f.read()


SECRET_YAML_BLOCK = re.compile(r"(?m)^(\s*(?:-\s+)?(?:password|bearer_token|credentials|client_secret|api_key|token)\s*:[ \t]*)(?!<secret>)(\S[^\n#]*?)([ \t]*(?:#.*)?)$")
SECRET_YAML_FLOW = re.compile(r"([{,]\s*(?:password|bearer_token|credentials|client_secret|api_key|token)\s*:\s*)(?!<secret>)(\"[^\"]*\"|'[^']*'|[^,}\s]+)")


def redact_yaml(text):
    """prometheus.yml with the values of password-like fields replaced by <secret>: (text, number replaced)."""
    text, n1 = SECRET_YAML_BLOCK.subn(lambda m: m.group(1) + "<secret>" + m.group(3), text)
    text, n2 = SECRET_YAML_FLOW.subn(lambda m: m.group(1) + "<secret>", text)
    return text, n1 + n2


def export_files(args, rep, X, cfg):
    """prometheus.yml as it is on disk, the rule files it names, the old inventory.json: copied as they are."""
    py = args.prometheus_yml
    X["prometheus_yml"] = {"source": os.path.abspath(py) if py else None, "copy": None, "source_sha256": None, "redacted": 0}
    if py:
        try:
            data = read_copy(py)
            text, n = redact_yaml(data.decode("utf-8"))
            rep.files["prometheus.yml"] = text if n else data
            X["prometheus_yml"].update(copy="prometheus.yml", source_sha256=hashlib.sha256(data).hexdigest(), redacted=n)
        except (OSError, UnicodeDecodeError) as e:
            rep.miss("prometheus.yml 原文", "读不到 %s：%s" % (py, getattr(e, "strerror", None) or e))
    X["rule_files"] = []
    cfg_dir_in_prom = os.path.dirname((X.get("flags") or {}).get("config.file") or "")
    for n, pat in enumerate((cfg or {}).get("rule_files") or []):
        cands = []
        if os.path.isabs(pat):
            cands.append(pat)
            if py and cfg_dir_in_prom and pat.startswith(cfg_dir_in_prom.rstrip("/") + "/"):
                # a path inside the Prometheus container: the same place next to prometheus.yml on this machine
                cands.append(os.path.join(os.path.dirname(os.path.abspath(py)), os.path.relpath(pat, cfg_dir_in_prom)))
        elif py:
            cands.append(os.path.join(os.path.dirname(os.path.abspath(py)), pat))
        found = []
        for c in cands:
            found = sorted(glob.glob(c))
            if found:
                break
        entry = {"pattern": pat, "files": []}
        for k, f in enumerate(found):
            copy_path = "rules/%02d-%02d-%s" % (n, k, os.path.basename(f))
            try:
                rep.files[copy_path] = read_copy(f)
                entry["files"].append({"path": os.path.abspath(f), "copy": copy_path})
            except OSError as e:
                entry["files"].append({"path": os.path.abspath(f), "error": e.strerror or str(e)})
        if not found:
            entry["note"] = "本机找不到（%s）" % ("、".join(cands) if cands else "没给 --prometheus-yml，相对路径无从解析")
        X["rule_files"].append(entry)
    inv = args.inventory
    X["inventory"] = {"source": os.path.abspath(inv) if inv else None, "found": False, "copy": None}
    if inv:
        try:
            rep.files["inventory.json"] = read_copy(inv)
            X["inventory"].update(found=True, copy="inventory.json")
        except OSError:
            pass


def config_compare(cfg, pools, rep, R, mask):
    base = PROM_BASELINE
    glob_site = clean_job(cfg.get("global") or {})
    R["global"] = mask.walk(glob_site)
    bad = []
    for k in sorted(set(base["global"]) | set(glob_site)):
        if k == "external_labels":
            continue
        if base["global"].get(k) != glob_site.get(k):
            bad.append("%s 基线 %s，现场 %s" % (k, base["global"].get(k, "（无）"), glob_site.get(k, "（无）")))
    if bad:
        rep.add("有差异", "global：%s" % "；".join(bad))
    else:
        rep.add("和基线一致", "global：scrape_interval %s，scrape_timeout %s，evaluation_interval %s" % (
            glob_site.get("scrape_interval"), glob_site.get("scrape_timeout"), glob_site.get("evaluation_interval")))
    ext = glob_site.get("external_labels") or {}
    if ext:
        rep.add("基线没有的现场新增", "external_labels：%s" % ", ".join("%s=%s" % (k, mask.text(str(v))) for k, v in ext.items()))
    rf = cfg.get("rule_files") or []
    R["rule_files"] = mask.walk(rf)
    if rf:
        rep.add("基线没有的现场新增", "rule_files：%s" % short("、".join(mask.text(str(x)) for x in rf), 80))
    for k in sorted(set(cfg) | set(base["top"])):
        if k in ("global", "scrape_configs", "rule_files"):
            continue
        site = clean_job(cfg[k]) if isinstance(cfg.get(k), dict) else cfg.get(k)
        if k not in base["top"]:
            rep.add("基线没有的现场新增", "配置段 %s" % k)
        elif k not in cfg:
            rep.add("现场缺少", "配置段 %s" % k)
        elif site != base["top"][k]:
            ops = diff(base["top"][k], site) if isinstance(site, dict) else [((), site)]
            rep.add("有差异", "配置段 %s：%s" % (k, "；".join("%s 基线 %s，现场 %s" % (
                full_path(path) or k, json.dumps(at(base["top"][k], path), ensure_ascii=False),
                "（无）" if v is DEL else short(json.dumps(mask.walk(v), ensure_ascii=False), 30)) for path, v in ops[:4])))
    jobs = {}
    for job in cfg.get("scrape_configs") or []:
        jobs[job.get("job_name")] = job
    R["jobs"] = {}
    for name in sorted(set(jobs) | set(base["jobs"]), key=str):
        site, b = jobs.get(name), base["jobs"].get(name)
        if site is None:
            note = base["optional"].get(name)
            rep.add("现场缺少", "job %s%s" % (name, "（%s）" % note if note else ""))
            continue
        keys, values, n_static = static_labels(site)
        pool = pools.get(name) or {}
        tkeys = pool.get("label_keys") or set()
        all_keys = keys | tkeys
        n_targets = (pool.get("up", 0) + pool.get("down", 0) + pool.get("unknown", 0)) if pool else None
        J = {"sd": sd_kinds(site), "static_targets": n_static, "active_targets": n_targets,
             "static_label_keys": sorted(keys), "target_label_keys": sorted(tkeys),
             "label_values": {k: sorted(mask.walk(list(v))) for k, v in values.items()},
             "relabel_configs": [relabel_text(r) for r in site.get("relabel_configs") or []],
             "metric_relabel_configs": [relabel_text(r) for r in site.get("metric_relabel_configs") or []],
             "fields": mask.walk(clean_job({k: v for k, v in site.items() if k not in JOB_SKIP and not k.endswith("_sd_configs")}))}
        J["relabel_configs"] = mask.walk(J["relabel_configs"])
        J["metric_relabel_configs"] = mask.walk(J["metric_relabel_configs"])
        R["jobs"][name] = J
        health, issues = "", 0
        if pool:
            health = "%d 个目标全部 up" % pool["up"] if pool["down"] == 0 and pool["unknown"] == 0 else ""
            if pool["down"] or pool["unknown"]:
                issues += 1
                errs = "；".join("%s ×%d" % (e, c) for e, c in sorted(pool["errors"].items(), key=lambda x: -x[1])[:2])
                rep.add("有差异", "job %s 抓取目标：%d up，%d down，%d 未知%s" % (name, pool["up"], pool["down"], pool["unknown"],
                                                                    "（%s）" % errs if errs else ""))
        elif n_static:
            issues += 1
            rep.add("有差异", "job %s：配置里有 %d 个目标，Prometheus 没有在抓" % (name, n_static))
        if b is None:
            rep.add("基线没有的现场新增", "job %s：%s %s 个目标，label %s" % (
                name, "+".join(J["sd"]) or "无服务发现", n_targets if n_targets is not None else n_static,
                " ".join(sorted(all_keys)) or "无"))
            continue
        bad = []
        bf = b["fields"]
        sf = J["fields"]
        for k in sorted(set(bf) | set(sf)):
            if bf.get(k) != sf.get(k):
                bad.append("%s 基线 %s，现场 %s" % (k, short(json.dumps(bf.get(k), ensure_ascii=False), 30) if k in bf else "（无）",
                                                short(json.dumps(sf.get(k), ensure_ascii=False), 30) if k in sf else "（无）"))
        if J["sd"] != ["static_configs"]:
            bad.append("服务发现 基线 static_configs，现场 %s" % ("+".join(J["sd"]) or "无"))
        roles = set(values.get("role", set()))
        if "role" in keys and sorted(roles) != b["roles"]:
            bad.append("role 基线 %s，现场 %s" % ("/".join(b["roles"]), "/".join(sorted(roles))))
        for kind in ("relabel_configs", "metric_relabel_configs"):
            if J[kind]:
                issues += 1
                rep.add("基线没有的现场新增", "job %s 的 %s %d 条：%s" % (name, kind, len(J[kind]), short("；".join(J[kind]), 70)))
        for k in sorted(all_keys - set(b["label_keys"])):
            issues += 1
            rep.add("基线没有的现场新增", "job %s 的 label %s%s" % (name, k, "（%s）" % compress_values(sorted(mask.walk(list(values[k]))), 6)
                                                            if k in values else "（目标上的 label，配置里不是静态写的）"))
        miss = sorted(set(b["label_keys"]) - all_keys)
        if miss:
            rep.add("现场缺少", "job %s 的 label %s" % (name, " ".join(miss)))
        if bad:
            rep.add("有差异", "job %s：%s" % (name, "；".join(bad)))
        elif not issues:
            rep.add("和基线一致", "job %s：抓取参数、label %s 一致%s" % (
                name, " ".join(k for k in b["label_keys"] if k in all_keys), "，" + health if health else ""))


def at(obj, path):
    try:
        return get_at(obj, path)
    except (KeyError, IndexError, TypeError):
        return "（无）"


def query(p, expr):
    code, data = p.get("/api/v1/query", {"query": expr})
    if code != 200 or not data or data.get("status") != "success":
        raise Unreachable(why(code, data))
    return data["data"]["result"]


def series_labels(p, rep, R, cfg, mask):
    """Which labels the panel-1 metrics carry on site, the values of the key labels, labels that look like a cluster."""
    L = R.setdefault("labels", {})
    try:
        code, names = p.get("/api/v1/labels")
        if code != 200:
            raise Unreachable(why(code, names))
        all_names = sorted(names.get("data") or [])
        L["all_label_names"] = all_names
        X = rep.export["prometheus"]
        X["label_names"] = all_names
        code, mn = p.get("/api/v1/label/__name__/values")
        if code != 200:
            raise Unreachable(why(code, mn))
        X["metric_names"] = sorted(mn.get("data") or [])
        L["metric_names"] = X["metric_names"]
        reserved = {}
        for n in ("cluster", "model_group"):
            vals = {}
            if n in all_names:
                vals = {r["metric"].get(n, ""): int(float(r["value"][1])) for r in query(p, 'count by (%s) ({%s!=""})' % (n, n))}
            reserved[n] = {"used": n in all_names, "values": vals}
        X["reserved_labels"] = reserved
        L["reserved_labels"] = mask.walk(reserved)
        rep.notes.append("label 名 %s" % "；".join(
            "%s：%s" % (n, "已被占用，%d 个值：%s" % (len(r["values"]), compress_values(sorted(mask.walk(list(r["values"]))), 6))
                       if r["used"] else "没有被占用") for n, r in reserved.items()))
        groups = {}
        for gname, regex in GROUPS:
            sel = '{__name__=~"%s"}' % regex
            res = query(p, "count(%s)" % sel)
            n = int(float(res[0]["value"][1])) if res else 0
            code, ln = p.get("/api/v1/labels", {"match[]": sel})
            code2, mn = p.get("/api/v1/label/__name__/values", {"match[]": sel})
            G = {"series": n, "metric_names": len((mn or {}).get("data") or []), "label_names": sorted((ln or {}).get("data") or [])}
            if n:
                G["series_without"] = {}
                for lab in MISSING_CHECK:
                    r = query(p, 'count({__name__=~"%s", %s=""})' % (regex, lab))
                    G["series_without"][lab] = int(float(r[0]["value"][1])) if r else 0
            groups[gname] = G
        L["groups"] = groups
        values = {}
        for lab in KEY_LABELS:
            if lab not in all_names:
                values[lab] = {}
                continue
            res = query(p, 'count by (%s) ({%s!=""})' % (lab, lab))
            values[lab] = {mask.text(r["metric"].get(lab, "")): int(float(r["value"][1])) for r in res}
        L["values"] = values
        if values.get("model_name"):
            res = query(p, 'count by (model_name) ({__name__=~"%s", model_name!=""})' % GROUPS[0][1])
            L["model_name_in_vllm"] = {mask.text(r["metric"].get("model_name", "")): int(float(r["value"][1])) for r in res}
        cluster = {}
        for n in all_names:
            if CLUSTER_LIKE.search(n) and n not in KEY_LABELS:
                res = query(p, 'count by (%s) ({%s!=""})' % (n, n))
                cluster[n] = {mask.text(r["metric"].get(n, "")): int(float(r["value"][1])) for r in res}
        ext = ((cfg or {}).get("global") or {}).get("external_labels") or {}
        L["cluster_like"] = cluster
        L["external_labels"] = mask.walk(ext)
    except Unreachable as e:
        rep.miss("Prometheus 指标 label", str(e))
        return
    for gname, G in groups.items():
        if not G["series"]:
            rep.facts.append("%s 指标：没有序列" % gname)
            continue
        wo = "，".join("缺 %s %d" % (k, v) for k, v in G["series_without"].items() if v)
        rep.facts.append("%s 指标 %d 个、序列 %d 条；label：%s%s" % (
            gname, G["metric_names"], G["series"], " ".join(x for x in G["label_names"] if x != "__name__"),
            "；序列中" + wo if wo else ""))
    for lab in KEY_LABELS:
        vals = values.get(lab) or {}
        if not vals:
            rep.facts.append("%s：没有这个 label" % lab)
            continue
        fmt = lambda vs: "、".join("%s（%d）" % (k, v) for k, v in sorted(vs.items())) if len(vs) <= 8 else compress_values(sorted(vs))
        if lab == "model_name" and "model_name_in_vllm" in L:
            inv = L["model_name_in_vllm"]
            other = {k: v - inv.get(k, 0) for k, v in vals.items() if v - inv.get(k, 0) > 0}
            rep.facts.append("model_name %d 个值：vLLM 指标里 %s%s" % (len(vals), fmt(inv) or "无",
                                                                 "；其他指标里 %s" % fmt(other) if other else ""))
            continue
        rep.facts.append("%s %d 个值：%s" % (lab, len(vals), fmt(vals)))
    if cluster or ext:
        for n, vals in cluster.items():
            rep.facts.append("类似集群的 label %s %d 个值：%s" % (n, len(vals), compress_values(sorted(vals), 10)))
        if ext:
            rep.facts.append("类似集群的 external_labels：%s" % ", ".join("%s=%s" % (k, v) for k, v in mask.walk(ext).items()))
    else:
        rep.facts.append("类似集群的 label：没有（查了全部 %d 个 label 名和 external_labels）" % len(all_names))


# ---------------------------------------------------------------- directories (--grafana-dir, --prometheus-dir)
# v3 pack files: side -> basename -> {"path", "sha256", "text" (YAML only), "required"}
PACK_FILES = json.loads("{\"grafana\": {\"vllm-pd.yaml\": {\"path\": \"grafana/provisioning/dashboards/vllm-pd.yaml\", \"sha256\": \"fc76f05393cb7432cd0a25143d62f7cc1f7aab46ff004bd58e6ff1976ea49dfb\", \"required\": true, \"text\": \"# Load every dashboard JSON under /etc/grafana/dashboards (mounted from ./grafana/dashboards).\\napiVersion: 1\\nproviders:\\n  - name: vllm-pd\\n    folder: vLLM P/D\\n    type: file\\n    disableDeletion: true\\n    allowUiUpdates: false\\n    updateIntervalSeconds: 30\\n    options:\\n      path: /etc/grafana/dashboards\\n      foldersFromFilesStructure: false\\n\"}, \"prometheus.yaml\": {\"path\": \"grafana/provisioning/datasources/prometheus.yaml\", \"sha256\": \"04874b0863b6372312066750f5d444e74498efa3bc5df458ef8a47e97713c853\", \"required\": true, \"text\": \"# Grafana datasource provisioning for dashboard 1.\\n# The dashboard's \\\"datasource\\\" variable defaults to uid \\\"prometheus\\\"; keep the uid if you rename the datasource.\\napiVersion: 1\\ndatasources:\\n  - name: Prometheus\\n    uid: prometheus\\n    type: prometheus\\n    access: proxy\\n    url: ${PROMETHEUS_URL}\\n    isDefault: true\\n    editable: false\\n    jsonData:\\n      # Must equal the Prometheus scrape_interval (inventory \\\"scrape_interval\\\"); $__rate_interval is derived from it.\\n      timeInterval: ${PROMETHEUS_SCRAPE_INTERVAL}\\n      httpMethod: POST\\n      prometheusType: Prometheus\\n\"}}, \"prometheus\": {\"gen_prometheus_yml.py\": {\"path\": \"prometheus/gen_prometheus_yml.py\", \"sha256\": \"fdec299d0426cf2a1ca4850cc37a511007d220a4074a40ce0fe3dbef79137a50\", \"required\": false}, \"prometheus.example.yml\": {\"path\": \"prometheus/prometheus.example.yml\", \"sha256\": \"2af051f4aacfc4da9eee05f19ba04d61f085f7e9ca614157e3ce7d70c3c9fd75\", \"required\": false}, \"inventory.example.json\": {\"path\": \"prometheus/inventory.example.json\", \"sha256\": \"c786b4df2903e7c23684943e9cb5b785b5d6a716d71f9201234b8437c312618d\", \"required\": false}}}")
# database and TSDB entries: only their size is recorded, nothing is read
DATA_NAME = re.compile(r"^(grafana\.db(-journal|-wal|-shm)?|wal|chunks_head|lock|queries\.active|[0-9A-HJKMNP-TV-Z]{26})$")
SECRETISH = re.compile(r"(?i)password|secret|token|api_key|private_key|credential|cookie")
INI_SECTIONS = ("paths", "server", "security", "users", "auth", "auth.anonymous", "auth.basic", "auth.proxy",
                "dashboards", "panels", "feature_toggles")
READ_LIMIT = 32 << 20


def read_dir(spec):
    """(files {rel: (size, sha256 or None, bytes or None)}, data {rel: size}, errors {rel: reason}).
    spec is a path on this machine, or container:path read with docker cp into memory."""
    files, data, errors = {}, {}, {}

    def data_key(rel):
        parts = rel.split("/")
        k = next((i for i, c in enumerate(parts) if DATA_NAME.match(c)), None)
        return None if k is None else "/".join(parts[:k + 1])

    def put(rel, size, read):
        k = data_key(rel)
        if k:
            data[k] = data.get(k, 0) + size
            return
        body = read() if size <= READ_LIMIT else None
        files[rel] = (size, hashlib.sha256(body).hexdigest() if body is not None else None, body)

    if os.path.isdir(spec):
        for dp, dns, fns in os.walk(spec):
            for f in fns:
                full = os.path.join(dp, f)
                rel = os.path.relpath(full, spec).replace(os.sep, "/")
                try:
                    size = os.path.getsize(full)
                    put(rel, size, lambda: open(full, "rb").read())
                except OSError as e:
                    errors[rel] = e.strerror or str(e)
        return files, data, errors
    m = re.match(r"^([A-Za-z0-9][A-Za-z0-9_.-]*):(/.*)$", spec)
    if not m:
        raise Unreachable("%s 不是本机目录；容器部署写成 容器名:容器内路径" % spec)
    try:
        proc = subprocess.Popen(["docker", "cp", spec, "-"], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except OSError as e:
        raise Unreachable("运行不了 docker：%s" % (e.strerror or e))
    try:
        with tarfile.open(fileobj=proc.stdout, mode="r|") as tf:
            for t in tf:
                if not t.isfile():
                    continue
                parts = t.name.split("/")
                rel = "/".join(parts[1:]) or parts[0]
                put(rel, t.size, lambda: tf.extractfile(t).read())
    except tarfile.TarError:
        pass
    err = proc.stderr.read().decode("utf-8", "replace").strip()
    if proc.wait() != 0:
        raise Unreachable("docker cp %s 失败：%s" % (spec, short(err, 80)))
    return files, data, errors


def size_text(n):
    return "%d B" % n if n < 1024 else "%.1f KB" % (n / 1024.0) if n < 1 << 20 else "%.1f MB" % (n / 1048576.0) \
        if n < 1 << 30 else "%.1f GB" % (n / 1073741824.0)


def yaml_lines(text):
    """Non-empty, non-comment lines; values of password-like keys replaced by 有值 / 空."""
    out = []
    for ln in text.splitlines():
        ln = _strip_comment(ln).strip()
        if not ln:
            continue
        m = re.match(r"^(-\s*)?([\w.]+)\s*:\s*(.*)$", ln)
        if m and SECRETISH.search(m.group(2)) and m.group(3):
            ln = "%s%s: %s" % (m.group(1) or "", m.group(2), "有值")
        out.append(ln)
    return out


def ini_settings(text):
    sec, out = "", []
    for ln in text.splitlines():
        ln = ln.strip()
        if not ln or ln[0] in ";#":
            continue
        m = re.match(r"^\[(.+)\]$", ln)
        if m:
            sec = m.group(1).strip()
            continue
        if "=" in ln:
            k, v = [x.strip() for x in ln.split("=", 1)]
            out.append((sec, k, ("有值" if v else "空") if SECRETISH.search(k) else v))
    return out


def job_shape(cfg):
    return {j.get("job_name"): sorted(json.dumps(g, sort_keys=True, default=str) for g in j.get("static_configs") or [])
            for j in (cfg or {}).get("scrape_configs") or []}


def check_dir(side, spec, rep, args, ctx):
    """List the files of a Grafana or Prometheus directory and compare them with the v3 pack."""
    rep.part = "Grafana目录" if side == "grafana" else "Prometheus目录"
    files, data, errors = read_dir(spec)
    pack = PACK_FILES[side]
    seen = set()
    cfg_name = os.path.basename((ctx.get("prom_flags") or {}).get("config.file") or "") or "prometheus.yml"
    for rel in sorted(files):
        size, sha, body = files[rel]
        name = rel.split("/")[-1]
        text = body.decode("utf-8", "replace") if body is not None else ""
        tag = "文件 %s" % rel
        if side == "grafana" and name.endswith(".json"):
            try:
                d = json.loads(text)
                d = d.get("dashboard", d)
            except (ValueError, AttributeError):
                d = {}
            if isinstance(d, dict) and d.get("uid") in args.uids:
                s = ("P", "D")[list(args.uids).index(d["uid"])]
                seen.add(s)
                try:
                    n = normalize(d)
                except Exception as e:  # noqa: BLE001 - any malformed board is reported, not raised
                    rep.add("有差异", "%s：%s 看板读不了（%s）" % (tag, s, short(e, 40)))
                    continue
                live = (ctx.get("live") or {}).get(s)
                lv = "" if live is None else "；与 Grafana 里加载的%s" % ("相同" if dump(live) == dump(n) else "不同")
                k = count_ops(ctx["base"][s], n)
                if k == 0:
                    rep.add("和基线一致", "%s：%s 看板与基线相同%s" % (tag, s, lv))
                else:
                    rep.add("有差异", "%s：%s 看板与基线差 %d 个字段%s" % (tag, s, k, lv))
                continue
        if name in pack and not (side == "prometheus" and name == cfg_name):
            seen.add(name)
            want = pack[name]
            if sha == want["sha256"]:
                rep.add("和基线一致", "%s：与 v3 包 %s 相同" % (tag, want["path"]))
            elif "text" in want:
                a, b = yaml_lines(want["text"]), yaml_lines(text)
                gone = [x for x in a if x not in b]
                new = [x for x in b if x not in a]
                if not gone and not new:
                    rep.add("和基线一致", "%s：与 v3 包 %s 内容相同（注释或空白不同）" % (tag, want["path"]))
                else:
                    kv = lambda ls: dict((m.group(1), m.group(2).strip()) for m in
                                         (re.match(r"^(?:-\s*)?([\w.]+)\s*:\s*(.*)$", x) for x in ls) if m)
                    g, n = kv(gone), kv(new)
                    parts = ["%s %s→%s" % (k, g[k] or "（空）", n[k] or "（空）") for k in n if k in g]
                    if [k for k in n if k not in g]:
                        parts.append("多了 " + "、".join(k for k in n if k not in g))
                    if [k for k in g if k not in n]:
                        parts.append("少了 " + "、".join(k for k in g if k not in n))
                    rep.add("有差异", "%s：%s（与 v3 包比）" % (tag, "；".join(parts) or "%d 行不同" % (len(gone) + len(new))))
            else:
                rep.add("有差异", "%s：与 v3 包 %s 不同（sha256 %s）" % (tag, want["path"], (sha or "?")[:8]))
            continue
        if side == "prometheus" and name == cfg_name:
            seen.add(name)
            try:
                fc = parse_yaml(text)
            except (ValueError, IndexError) as e:
                rep.add("有差异", "%s：解析不了（%s）" % (tag, short(e, 40)))
                continue
            loaded = ctx.get("prom_cfg")
            a = job_shape(fc)
            b = job_shape(loaded) if loaded is not None else {j: None for j in PROM_BASELINE["jobs"]}
            what = "Prometheus 已加载的配置" if loaded is not None else "基线"
            bad = ["job %s 只在文件里" % j for j in sorted(set(a) - set(b), key=str)]
            bad += ["job %s 文件里没有" % j for j in sorted(set(b) - set(a), key=str) if loaded is not None or j not in PROM_BASELINE["optional"]]
            if loaded is not None:
                bad += ["job %s 的目标或 label 不同" % j for j in sorted(set(a) & set(b), key=str) if a[j] != b[j]]
                if [str(x) for x in fc.get("rule_files") or []] != [str(x) for x in loaded.get("rule_files") or []]:
                    bad.append("rule_files 不同")
            if bad:
                rep.add("有差异", "%s：与%s不同：%s" % (tag, what, short("；".join(bad), 60)))
            else:
                rep.add("和基线一致", "%s：job 和目标与%s相同" % (tag, what))
            continue
        if side == "grafana" and name.endswith(".ini"):
            sets = [(s, k, v) for s, k, v in ini_settings(text) if s in INI_SECTIONS]
            if not sets:
                rep.add("和基线一致", "%s：面板一相关的段没有设置（v3 包用默认值）" % tag)
            for s, k, v in sets:
                rep.add("有差异", "%s：[%s] %s = %s（v3 包不改 grafana.ini）" % (tag, s, k, short(v, 40)))
            continue
        if side == "grafana" and name == "ldap.toml":
            continue  # shipped with the Grafana image
        rep.add("基线没有的现场新增", "%s（%s，sha256 %s）" % (tag, size_text(size), (sha or "未读")[:8]))
    for name, want in sorted(pack.items()):
        if want.get("required") and name not in seen:
            rep.add("现场缺少", "文件 %s（v3 包的 %s）" % (name, want["path"]))
    if side == "grafana":
        for s, uid in zip(("P", "D"), args.uids):
            if s not in seen:
                rep.add("现场缺少", "%s 看板文件（uid %s）" % (s, uid))
    elif cfg_name not in seen:
        rep.add("现场缺少", "文件 %s（Prometheus 的配置文件）" % cfg_name)
    if errors:
        rep.miss("%s 目录 %d 个文件" % (side, len(errors)), "读不到（%s）" % short(next(iter(errors.values())), 40))
    rep.notes.append("%s 目录 %s：%d 个文件%s" % (side, spec, len(files), "；数据只记大小：" + "、".join(
        "%s %s" % (k, size_text(v)) for k, v in sorted(data.items())[:6]) if data else ""))
    rep.export.setdefault("dirs", {})[side] = {
        "source": spec, "files": [{"path": r, "size": v[0], "sha256": v[1]} for r, v in sorted(files.items())],
        "data": [{"path": k, "size": v} for k, v in sorted(data.items())], "unreadable": errors}
    rep.dirs.append((side, spec, files, data))


# ---------------------------------------------------------------- panel1-export/
EXPORT_FORMAT = "panel1-export"
EXPORT_VERSION = 1


def write_export(args, rep, bdig):
    """panel1-export/: export.json plus the files it names, unmasked, readable only by the owner."""
    d = args.export_dir
    if os.path.isdir(d) and os.listdir(d):
        d = "%s-%s" % (d.rstrip("/"), datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d-%H%M%S"))
    os.makedirs(d, exist_ok=True)
    os.chmod(d, 0o700)
    sums = {}
    for rel, body in sorted(rep.files.items()):
        path = os.path.join(d, *rel.split("/"))
        sub = os.path.dirname(path)
        if not os.path.isdir(sub):
            os.makedirs(sub)
            for x in range(rel.count("/")):
                os.chmod(os.path.join(d, *rel.split("/")[:x + 1]), 0o700)
        data = body.encode("utf-8") if isinstance(body, str) else body
        with open(path, "wb") as f:
            f.write(data)
        os.chmod(path, 0o600)
        sums[rel] = hashlib.sha256(data).hexdigest()
    X = {"format": EXPORT_FORMAT, "version": EXPORT_VERSION,
         "warning": "含内网地址、主机名和完整看板，只留在本机，不要外传",
         "generated": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
         "baseline": {"pack": PACK_SHA, "dashboards": bdig, "diff_panel1_sha256": DIFF_SCRIPT_SHA},
         "unreachable": rep.unreachable, "files": sums, "items": rep.numbered, "dirs": rep.export.get("dirs"),
         "prometheus": rep.export.get("prometheus"), "grafana": rep.export.get("grafana")}
    path = os.path.join(d, "export.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(X, f, ensure_ascii=False, indent=1, sort_keys=True)
        f.write("\n")
    os.chmod(path, 0o600)
    P, G = rep.export.get("prometheus") or {}, rep.export.get("grafana") or {}
    py, inv = P.get("prometheus_yml") or {}, P.get("inventory") or {}
    rel = os.path.basename(d.rstrip("/"))
    rep.notes.insert(0, "%s/：%d 个文件，含内网地址，只留在本机，不要外传" % (rel, len(sums) + 1))
    if P:
        rep.notes.append("prometheus.yml 原文：%s" % (("已收入" + ("，%d 处密码类字段的值换成了 <secret>" % py["redacted"] if py.get("redacted") else ""))
                                                    if py.get("copy") else "没有（没给 --prometheus-yml 或读不到）"))
        if P.get("credentials"):
            rep.notes.append("配置里有认证 %d 处（密码、token 不导出，只记位置和引用的文件）" % len(P["credentials"]))
        rf = P.get("rule_files") or []
        if rf:
            rep.notes.append("rule_files %d 个：本机找到 %d 个文件%s" % (len(rf), sum(len(e["files"]) for e in rf),
                                                             "，%d 个找不到" % sum(1 for e in rf if not e["files"]) if any(not e["files"] for e in rf) else ""))
        rep.notes.append("原来的 inventory.json：%s" % ("已收入" if inv.get("found") else "没有"))
        r = P.get("reload") or {}
        rep.notes.append("热加载：%s" % ("--web.enable-lifecycle 已开，可用 POST /-/reload" if r.get("web_enable_lifecycle")
                                       else "--web.enable-lifecycle 没开，改配置后要发 SIGHUP 或重启" if r.get("web_enable_lifecycle") is False
                                       else "取不到启动参数"))
        if "metric_names" in P:
            rep.notes.append("指标名 %d 个（全部列表在导出里）" % len(P["metric_names"]))
    if G.get("dashboards"):
        wc = [(b["side"], q) for b in G["dashboards"] for q in b["queries_with_cluster"]]
        rep.notes.append("看板里已经用到 cluster 或 model_group 的查询和变量：%s" % (
            "；".join("%s/%s" % (s, q.get("panel", q.get("variable"))) for s, q in wc[:10]) + ("…共 %d 处" % len(wc) if len(wc) > 10 else "")
            if wc else "没有"))
        rep.notes.append("看板文件：%s" % "；".join("%s %s" % (b["uid"], "本机 " + os.path.basename(b["host_file"]) if b["host_file"]
                                                       else (b["provisionedExternalId"] or b["sourcePath"] or "找不到本机文件"))
                                               for b in G["dashboards"]))
    return d


# ---------------------------------------------------------------- main
def number(rep):
    """Number the differences D01, D02, ... in the order they were found (读不到 first)."""
    order = [e for e in rep.entries if e[0] == "读不到"] + [e for e in rep.entries if e[0] not in ("读不到", "和基线一致")]
    nums = {id(e): "D%02d" % (i + 1) for i, e in enumerate(order)}
    rep.numbered = [{"no": nums[id(e)], "part": e[1], "kind": KIND[e[0]], "text": e[2]} for e in order]
    rep.no_of = {}
    for e in rep.entries:
        rep.no_of.setdefault(e[0], []).append(nums.get(id(e), ""))


KIND = {"有差异": "不同", "基线没有的现场新增": "新增", "现场缺少": "缺少", "读不到": "读不到"}


def render(rep, bdig, mask):
    """panel1-detail.txt: every item, differences with their numbers."""
    now = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    out = ["面板一探测明细 %s；基线 v3 面板一（看板 %s，包 %s）；%s" % (now, bdig, PACK_SHA, "IP 已打码" if mask.on else "IP 未打码")]
    out.append("访问不到：%s" % ("；".join("%s %s（%s）" % (n, m["part"], m["reason"]) for n, m in
                                     zip(rep.no_of.get("读不到", []), rep.unreachable)) or "无"))
    for g in Report.GROUPS:
        out.append("【%s】%d 项" % (g, len(rep.items[g])))
        nos = rep.no_of.get(g, [])
        out.extend("  " + ("%s %s" % (nos[i], x) if i < len(nos) and nos[i] else x) for i, x in
                   enumerate(mask.text(y) for y in rep.items[g]))
    out.append("【现场 label】")
    out.extend("  " + mask.text(x) for x in rep.facts)
    out.append("【本机导出】")
    out.extend("  " + mask.text(x) for x in rep.notes)
    for side, spec, files, data in rep.dirs:
        out.append("【%s 目录 %s】%d 个文件" % (side, mask.text(spec), len(files)))
        out.extend("  %s  %s  %s" % (mask.text(r), size_text(v[0]), (v[1] or "未读")[:8]) for r, v in sorted(files.items()))
        out.extend("  %s  %s  （数据，只记大小）" % (mask.text(k), size_text(v)) for k, v in sorted(data.items()))
    return "\n".join(out) + "\n"


def render_short(rep, bdig, mask, export_rel, max_lines, width=100):
    """panel1-probe.txt: one line per difference, the consistent items as one count; at most max_lines lines."""
    clip = lambda t: t if len(t) <= width else t[:width - 1] + "…"
    parts = lambda es: "、".join("%s %d" % (p, n) for p, n in
                                sorted(((p, sum(1 for e in es if e[1] == p)) for p in set(e[1] for e in es)),
                                       key=lambda x: list(PART_ORDER).index(x[0])))
    same = [e for e in rep.entries if e[0] == "和基线一致"]
    diffs = [e for e in rep.entries if e[0] != "和基线一致"]
    kinds = "、".join("%s %d" % (k, sum(1 for d in rep.numbered if d["kind"] == k)) for k in ("不同", "新增", "缺少", "读不到")
                     if any(d["kind"] == k for d in rep.numbered))
    now = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    head = [clip("面板一探测 %s；基线 v3（看板 %s，包 %s）；%s" % (now, bdig, PACK_SHA, "IP 已打码" if mask.on else "IP 未打码")),
            clip("差异 %d 项%s；一致 %d 项（%s）" % (len(diffs), "（%s）" % kinds if kinds else "", len(same), parts(same) or "无"))]
    foot = [clip("明细按编号查：%s/panel1-detail.txt（只留本机，不外传）" % export_rel)]
    lines = [clip("%s %s %s %s" % (d["no"], d["part"], d["kind"], mask.text(d["text"]))) for d in rep.numbered]
    room = max(1, max_lines - len(head) - len(foot))
    if len(lines) > room:
        rest = rep.numbered[room - 1:]
        lines = lines[:room - 1] + [clip("其余 %d 项见本机明细（%s）" % (len(rest), "、".join(
            "%s %d" % (p, sum(1 for d in rest if d["part"] == p)) for p in PART_ORDER if any(d["part"] == p for d in rest))))]
    return "\n".join(head + lines + foot) + "\n"


PART_ORDER = ("读不到", "Grafana", "看板", "Prometheus", "Grafana目录", "Prometheus目录")


def main():
    utf8_stdio()
    ap = argparse.ArgumentParser(description="面板一现场探测（只读）：Grafana、Prometheus 与 v3 基线逐项对比。")
    ap.add_argument("--grafana-url", default=os.environ.get("GRAFANA_URL"), help="Grafana 地址（或 GRAFANA_URL）")
    ap.add_argument("--grafana-user", default=os.environ.get("GRAFANA_USER"), help="用户名（或 GRAFANA_USER），密码取 GRAFANA_PASSWORD，没有就提示输入")
    ap.add_argument("--grafana-token", default=os.environ.get("GRAFANA_TOKEN"), help="服务账号 token（或 GRAFANA_TOKEN）")
    ap.add_argument("--prometheus-url", default=os.environ.get("PROMETHEUS_URL"), help="Prometheus 地址（或 PROMETHEUS_URL）")
    ap.add_argument("--prometheus-user", default=os.environ.get("PROMETHEUS_USER"), help="有认证时的用户名（或 PROMETHEUS_USER）")
    ap.add_argument("--prometheus-token", default=os.environ.get("PROMETHEUS_TOKEN"), help="或 bearer token（PROMETHEUS_TOKEN）")
    ap.add_argument("--uids", nargs=2, default=[u for _, u in UIDS], metavar=("P_UID", "D_UID"))
    ap.add_argument("--provisioning-dir", help="Grafana 的 provisioning 目录，读看板 provider 的配置（可选）")
    ap.add_argument("--prometheus-yml", help="本机上的 prometheus.yml，原样收进导出（可选）")
    ap.add_argument("--inventory", help="原来的 inventory.json，原样收进导出（可选，找不到就记「没有」）")
    ap.add_argument("--grafana-dir", help="Grafana 的目录（如 /etc/grafana，或 容器名:/etc/grafana），列出文件并与 v3 包比较（可选）")
    ap.add_argument("--prometheus-dir", help="Prometheus 配置目录（如 /etc/prometheus，或 容器名:/etc/prometheus），同上（可选）")
    ap.add_argument("--max-lines", type=int, default=40, help="短摘要最多几行（默认 40）")
    ap.add_argument("--export-dir", default="panel1-export", help="本机导出目录（默认 ./panel1-export，已存在且非空时加时间后缀）")
    ap.add_argument("--no-mask", action="store_true", help="IP 显示原值")
    ap.add_argument("-o", "--output", default="panel1-probe", help="短摘要文件名前缀（默认 panel1-probe → panel1-probe.txt）")
    args = ap.parse_args()
    if not args.grafana_url and not args.prometheus_url:
        ap.error("需要 Grafana 地址（--grafana-url 或 GRAFANA_URL）和 Prometheus 地址（--prometheus-url 或 PROMETHEUS_URL）")
    mask = Masker(not args.no_mask)
    rep = Report()
    base_raw = baseline()
    base = {s: normalize(base_raw[s]) for s in ("P", "D")}
    bdig = digest(base)
    rep.ctx["base"] = base
    for what, url, fn in (("Grafana", args.grafana_url, probe_grafana), ("Prometheus", args.prometheus_url, probe_prometheus)):
        if not url:
            rep.miss(what, "没给地址（--%s-url 或 %s_URL）" % (what.lower(), what.upper()))
            continue
        try:
            fn(args, rep, mask)
        except Unreachable as e:
            rep.miss(what, str(e))
    for side, spec in (("grafana", args.grafana_dir), ("prometheus", args.prometheus_dir)):
        if spec:
            try:
                check_dir(side, spec, rep, args, rep.ctx)
            except Unreachable as e:
                rep.part = "读不到"
                rep.miss("%s 目录" % side, str(e))
    number(rep)
    rep.data.update({"probe_format": PROBE_FORMAT, "generated": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                     "masked": mask.on, "baseline": {"pack": PACK_SHA, "diff_panel1_sha256": DIFF_SCRIPT_SHA, "dashboards": bdig,
                                                     "grafana_version": BASE_GRAFANA_VERSION,
                                                     "prometheus_version": PROM_BASELINE["version"]},
                     "summary": {g: [mask.text(x) for x in rep.items[g]] for g in Report.GROUPS},
                     "labels_text": [mask.text(x) for x in rep.facts], "unreachable": rep.unreachable})
    export_dir = write_export(args, rep, bdig)
    data = mask.walk(rep.data)
    data["differences"] = [dict(d, text=mask.text(d["text"])) for d in rep.numbered]
    for name, body in (("panel1-detail.txt", render(rep, bdig, mask)),
                       ("panel1-probe.json", json.dumps(data, ensure_ascii=False, indent=1, sort_keys=True) + "\n")):
        path = os.path.join(export_dir, name)
        with open(path, "w", encoding="utf-8") as f:
            f.write(body)
        os.chmod(path, 0o600)
    text = render_short(rep, bdig, mask, os.path.basename(export_dir.rstrip("/")), args.max_lines)
    with open(args.output + ".txt", "w", encoding="utf-8") as f:
        f.write(text)
    sys.stdout.write(text)
    print("写出 %s（%d 行）：只发回这一份" % (os.path.abspath(args.output + ".txt"), text.count("\n")))
    print("写出本机导出 %s（明细 panel1-detail.txt、panel1-probe.json、export.json）：含内网地址，只留在本机，不要外传" % os.path.abspath(export_dir))
    return 1 if rep.unreachable else 0


if __name__ == "__main__":
    sys.exit(main())
