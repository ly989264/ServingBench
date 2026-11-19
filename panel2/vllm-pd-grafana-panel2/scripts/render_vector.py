#!/usr/bin/env python3
"""Writes the collector's Vector config from segments.json and the environment (bin/lib.sh loads deploy.env).

Usage: render_vector.py <output> <data dir> <conf dir>      (render_vector.py --print writes to stdout with placeholders)
render_vector.py --run-script prints the command the collector container runs (bin/central_up.sh, scripts/compare.py).

What the collector does with a line of the mirror /pull/<machine>/<instance>/<role>.<start>.<pid>.<seq>.log:
  - parses the prefix (time with UTC offset, process, pid) and the JSON after [PD_DIAG];
  - request-level records go unchanged to the event table vllm_pd.diag_event (kept EVENT_TTL_DAYS);
  - the records a segment reads (segments.json "src" and "request") become sparse rows of vllm_pd.phase_line
    (kept SUMMARY_TTL_DAYS). The API server's three records of a request (api_summary with scope engine_submission,
    request and http) are merged into one row per (observation, request): api_summary[http] has no request id and
    is split into one copy per engine submission in its links, each with that submission's external_req_id;
  - the filter's per-request sums (pd_seg_partial) become phase_line rows too, one per part, cumulative;
  - every row gets the config_id of this segments.json (records of the line format it reads) or of the node's filter
    (its sums), which the dashboard's queries select on;
  - the filter's self-check lines go to vllm_pd.filter_selfcheck, file-open lines to vllm_pd.filter_file.
v3.1-extra, only when segments.json reads them or deploy.env collects them (otherwise the output is v3's):
  - /pull/_native/<machine>/<instance>/native.*.log: the puller's pd_native_put records (Mooncake's timing paired with the
    pool's save records, scripts/native_put.py), read like the node files (role native, process save);
  - /pull/_proxy/<machine>/<instance>/proxy.*.log: the proxy's line per request (pd_proxy_request) into vllm_pd.proxy_line,
    its self-check lines into vllm_pd.proxy_selfcheck; /pull/_proxy and /pull/_mooncake are kept out of the node files' source.
Generated; edit segments.json and run bin/apply_segments.sh instead of editing the output.
"""
import json
import os
import pathlib
import re
import sys

PACK = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PACK / "scripts"))
from segtree import Tree, range_name  # noqa: E402

API_EVENT = "api_summary"
PREFIXES = r"^(?:chatcmpl-|cmpl-|resp[-_])"
PROC_OF_ROLE = {"api": "api", "engine": "engine", "worker": "worker", "pool": "save"}
NATIVE_EVENT = "pd_native_put"
# v3.1-extra: the proxy's files (proxy patch), /pull/_proxy/<machine>/<instance>/proxy.<start>.<pid>.<seq>.log, same line format.
PROXY_PARSE = r'''
file_path = string!(.file)
m = parse_regex!(file_path, r'^/pull/_proxy/(?P<node>[^/]+)/(?:d[0-9]+/)?(?P<dir>[^/]+)/proxy\.[0-9]{8}T[0-9]{6}\.[0-9]+\.[0-9]{4}\.log$')
h = parse_regex!(string!(.message), r'^(?P<ts>[0-9T:.+-]+) \((?P<proc>[^ ]+) pid=(?P<pid>[0-9]+)\) (?P<level>[A-Z]+) \[[^\]]*\] \[PD_DIAG\] (?P<json>\{.*\})$')
ev = object!(parse_json!(h.json))
t = parse_timestamp!(h.ts, "%Y-%m-%dT%H:%M:%S%.3f%:z")
. = {"ev": ev, "node": m.node, "instance": m.dir, "pid": to_int!(h.pid), "event": string!(ev.event)}
.ts = format_timestamp!(t, "%Y-%m-%d %H:%M:%S%.3f", timezone: "UTC")
.model = "@@MODEL@@"
'''
PROXY_ROW = r'''
e = .ev
r = {"ts": .ts, "model": .model, "node": .node, "instance": .instance, "pid": .pid}
for_each(["request_id", "client_request_id", "api", "status", "p_addr", "d_addr"]) -> |_i, k| {
  v = get(e, [k]) ?? null
  if is_string(v) { r = set!(r, [k], v) }
}
for_each(["stream", "include_usage"]) -> |_i, k| {
  v = get(e, [k]) ?? null
  if is_boolean(v) {
    b = 0
    if v == true { b = 1 }
    r = set!(r, [k], b)
  }
}
for_each(["http_status", "n", "retries", "recompute_retries", "p_http_retries", "d_http_retries", "retry_ms", "api_gateway_ms",
          "proxy_to_p_ms", "p_total_ms", "proxy_to_d_ms", "d_first_ms", "d_total_ms", "ttft_ms", "e2e_ms"]) -> |_i, k| {
  v = get(e, [k]) ?? null
  if is_integer(v) || is_float(v) { r = set!(r, [k], v) }
}
if !is_string(r.request_id) || r.request_id == "" { abort }
. = r
'''
PROXY_SELFCHECK_ROW = r'''
e = .ev
. = {"ts": .ts, "node": .node, "instance": .instance, "pid": .pid, "queued": e.queued, "queue_full": e.queue_full,
     "written": e.written, "errors": e.errors, "queue_len": e.queue_len, "last_error": e.last_error}
if !is_string(.last_error) { .last_error = "" }
'''
# What the collector container runs with sh -c (bin/central_up.sh starts it; scripts/compare.py checks it in docker inspect).
# The small loop removes dead-letter files older than 7 days, once at start and then hourly, so they cannot grow without limit.
RUN_SCRIPT = ('while true; do find /var/lib/vector -maxdepth 1 -name "dead-letter-*.jsonl" -mtime +7 -delete 2>/dev/null; '
              'sleep 3600; done & exec vector --quiet --config /etc/vector/central.yaml')


def vrl_str(s):
    return json.dumps(s, ensure_ascii=False)


def field_expr(field, obj=".ev"):
    """VRL statements that put a source field into v: a.b.c is a nested path, x-y a difference; v is null when a part is missing."""
    if "-" in field:
        a, b = field.split("-", 1)
        return "v = null\nif is_integer(%s) && is_integer(%s) { v = int!(%s) - int!(%s) }" % (
            path(a, obj), path(b, obj), path(a, obj), path(b, obj))
    return "v = %s" % path(field, obj)


def path(field, obj):
    return obj + "".join("." + vrl_str(p) if not re.fullmatch(r"[a-z_][a-z0-9_]*", p) else "." + p for p in field.split("."))


def collect(tree, raw):
    """{(side, event): [item]}: what each record writes into phase_line. An item is a dict with col, field, where, scale,
    how (sum: segment durations, added up; max: request fields) and member (read the field from the members[] entry
    whose request_id is the record's own)."""
    out = {}
    for side in ("prefill", "decode"):
        for phase in ("ttft", "tpot", "background"):
            for node in tree.nodes(side, phase):
                for src in node.src or []:
                    if "event" not in src or "ranges" in src:
                        continue
                    out.setdefault((side, src["event"]), []).append(dict(
                        col=node.column, field=src.get("field", "duration_ns"), where=src.get("where") or {}, scale=1e-6, how="sum"))
        for name, spec in raw.get("request", {}).items():
            scale = 1000.0 if spec.get("unit") == "s" else (1e-6 if spec["field"].endswith("_ns") else None)
            out.setdefault((side, spec["event"]), []).append(dict(
                col=name, field=spec["field"], where=spec.get("where") or {}, scale=scale, how="max", member=spec.get("member")))
    return out


def cond_of(where):
    return " && ".join("%s == %s" % (path(k, ".ev"), vrl_str(v)) for k, v in sorted(where.items()))


def mapping_vrl(items):
    """VRL that sets the phase_line columns of one record. Two fields of one record that go into the same segment add up
    (req_resolve and render into req_resolve)."""
    lines = []
    for it in items:
        col, field, scale = it["col"], it["field"], it["scale"]
        op = ""
        if scale is not None:
            op = " / %.1f" % (1 / scale) if scale < 1 else " * %.1f" % scale   # VRL has no exponent literals
        if it.get("member"):
            val = ("v = null\nmm = get(.ev, [%s]) ?? null\nif is_array(mm) {\n"
                   "  ms = filter(array!(mm)) -> |_i, m| { is_object(m) && m.request_id == rid }\n"
                   "  if length(ms) > 0 { v = get(ms[0], [%s]) ?? null }\n}" % (vrl_str(it["member"]), vrl_str(field)))
        else:
            val = field_expr(field)
        if scale is None:
            set_ = "  .%s = v\n" % col
        elif it["how"] == "sum":
            set_ = "  f, err = to_float(v)\n  if err == null {\n    if is_float(.%s) { .%s = float!(.%s) + f%s } else { .%s = f%s }\n  }\n" % (
                col, col, col, op, col, op)
        else:
            set_ = "  f, err = to_float(v)\n  if err == null { .%s = f%s }\n" % (col, op)
        body = "%s\nif v != null {\n%s}" % (val, set_)
        if "-" in field and scale is not None and not it.get("member"):
            a, b = (path(x, ".ev") for x in field.split("-", 1))
            body = "if is_integer(%s) && is_integer(%s) {\n  .%s = to_float(int!(%s) - int!(%s))%s\n}" % (a, b, col, a, b, op)
        cond = cond_of(it["where"])
        if cond:
            body = "if %s {\n%s\n}" % (cond, "\n".join("  " + x for x in body.splitlines()))
        lines.append(body)
    return "\n".join(lines)


def ranges_vrl(tree, side):
    """The per-call ranges a segment reads (detokenize): two arrays, execution_id and duration_ns, and the total. Which
    range is the request's first step is decided in ClickHouse, with the first execution_id from the filter's sums."""
    out = []
    seen = set()
    for n in tree.nodes(side, "ttft", "tpot", "background"):
        for src in n.src or []:
            if "ranges" not in src or range_name(src) in seen:
                continue
            name = range_name(src)
            seen.add(name)
            cond = " && ".join(x for x in (".event == %s" % vrl_str(src["event"]), cond_of(src.get("where") or {})) if x)
            rs = vrl_str(src["ranges"].split("."))
            tot = vrl_str(src["total"].split("."))
            out.append(
                "if %s {\n"
                "  rs = get(.ev, %s) ?? null\n"
                "  if is_array(rs) {\n"
                "    ok = filter(array!(rs)) -> |_i, r| { is_object(r) && is_string(r.execution_id) && is_integer(r.duration_ns) }\n"
                "    .rg__%s__exec = map_values(ok) -> |r| { string(r.execution_id) ?? \"\" }\n"
                "    .rg__%s__ns = map_values(ok) -> |r| { int(r.duration_ns) ?? 0 }\n"
                "  }\n"
                "  t = get(.ev, %s) ?? null\n"
                "  if is_integer(t) { .rg__%s__total_ns = t }\n"
                "}" % (cond, rs, name, name, tot, name))
    return "\n".join(out)


def strategies(tree, raw, cols):
    """Reduce merge strategies: durations add up, everything else keeps the largest or the first value."""
    out = {"ts": "discard", "x": "discard", "attempt_id": "discard", "internal": "discard", "instance": "discard",
           "node": "discard", "side": "discard", "proc": "discard", "status": "retain", "finish_reason": "discard",
           "http_status": "max", "observation_id": "discard", "task_id": "discard", "events": "array", "file": "discard",
           "api_start_ts": "discard", "api_end_ts": "discard", "scope": "retain", "config_id": "discard",
           "core_mono_ns": "max"}
    for name in tree.ranges():
        out["rg__%s__exec" % name] = "retain"
        out["rg__%s__ns" % name] = "retain"
        out["rg__%s__total_ns" % name] = "max"
    for (_, _), items in cols.items():
        for it in items:
            col = it["col"]
            if col in ("status", "finish_reason"):
                out[col] = "retain" if col == "status" else "discard"
            else:
                out[col] = "sum" if it["how"] == "sum" else "max"
    return out


SHARED_PICK = r'''members_c9 = []
if includes(%s, .event) && !is_string(.internal) {
  mm = array(.ev.members) ?? []
  members_c9 = filter(mm) -> |_i, m| { is_object(m) && is_string(m.request_id) && m.request_id != "" }
}
'''
SHARED_SPLIT = r'''if length(members_c9) > 0 {
  base = .
  rows_c9, err_c9 = map_values(members_c9) -> |m| {
    i = replace(string(m.request_id) ?? "", r'@@PREFIXES@@', "")
    x_ = i
    a_ = ""
    r, err = parse_regex(i, r'^(?P<x>.+)-(?P<att>[0-9a-f]{8})$')
    if err == null { x_ = string(r.x) ?? i; a_ = string(r.att) ?? "" }
    merge(base, {"internal": m.request_id, "x": x_, "attempt_id": a_})
  }
  if err_c9 == null { . = rows_c9 }
}
'''


def build(env, data_dir, conf_dir, placeholders=False):
    tree = Tree()
    raw = tree.raw
    cols = collect(tree, raw)
    acc_ids = list(tree.acc_ids)      # the accumulate rules, and in v3.1-extra the candidate-step rules
    proxy_cols = tree.proxy_sourced()
    native = NATIVE_EVENT in {ev for (_, ev) in cols} or env.get("MOONCAKE_LOG_COLLECT") == "1"
    extra_dirs = bool(proxy_cols) or native or env.get("PROXY_COLLECT") == "1"
    proc_of = dict(PROC_OF_ROLE, native="save") if native else PROC_OF_ROLE
    passthrough = {x if isinstance(x, str) else x["event"] for x in raw.get("passthrough", [])} | {ev for (_, ev) in cols}
    addr = env.get("CLICKHOUSE_ADDR", "")
    port = "8123" if addr == env.get("CLICKHOUSE_CONTAINER") else env.get("CLICKHOUSE_HTTP_PORT", "8123")
    ch = "@@CH_ENDPOINT@@" if placeholders else "http://%s:%s" % (addr, port)
    pw = "@@CH_WRITER_PASSWORD@@" if placeholders else env.get("CH_WRITER_PASSWORD", "")
    model = env.get("MODEL_NAME") or "unknown"
    cfg = raw.get("config_id", "")
    record_version = raw.get("line", {}).get("record_version")

    events_vrl = []
    for side in ("prefill", "decode"):
        blocks = []
        for (s, ev), items in sorted(cols.items()):
            if s != side:
                continue
            blocks.append("if .event == %s {\n%s\n}" % (vrl_str(ev), "\n".join("  " + x for x in mapping_vrl(items).splitlines())))
        rv = ranges_vrl(tree, side)
        if rv:
            blocks.append(rv)
        events_vrl.append("if .side == %s {\n%s\n}" % (vrl_str(side), "\n".join("  " + x for x in "\n".join(blocks).splitlines())))
    mapped = sorted({ev for (_, ev) in cols})

    partial_vrl = ["cv = string(.ev.config_version) ?? \"\"\nparts = split(cv, \"@\")\n.config_id = string(parts[0]) ?? \"\"",
                   "if is_string(.ev.first_exec) { .first_exec = .ev.first_exec }"]
    for a in acc_ids:
        partial_vrl.append(
            "s = .ev.segs.%s\nif is_object(s) {\n  .acc__%s__ns = s.ns\n  .acc__%s__steps = s.steps\n  .acc__%s__first_ns = s.first_ns\n}" % (a, a, a, a))

    parse = r'''
file_path = string!(.file)
m = parse_regex!(file_path, r'^/pull/@@NATIVE_DIR@@(?P<node>[^/]+)/(?:d[0-9]+/)?(?P<dir>[^/]+)/(?P<role>api|engine|worker|pool@@NATIVE_ROLE@@)\.[0-9]{8}T[0-9]{6}\.[0-9]+\.[0-9]{4}\.log$')
h = parse_regex!(string!(.message), r'^(?P<ts>[0-9T:.+-]+) \((?P<proc>[^ ]+) pid=(?P<pid>[0-9]+)\) (?P<level>[A-Z]+) \[[^\]]*\] \[PD_DIAG\] (?P<json>\{.*\})$')
ev = object!(parse_json!(h.json))
t = parse_timestamp!(h.ts, "%Y-%m-%dT%H:%M:%S%.3f%:z")
inst = string!(m.dir)
# The instance folder is the name (P0, D17) or the port; a port is looked up as <machine>:<port> in the inventory.
if !match(inst, r'^[PD][0-9]+$') {
  rec = get_enrichment_table_record("inventory", {"addr": string!(m.node) + ":" + inst}) ?? {}
  if is_string(rec.name) { inst = string!(rec.name) }
}
. = {"ev": ev, "raw": h.json, "file": file_path, "node": m.node, "instance": inst, "role": m.role, "proc_name": h.proc,
     "pid": to_int!(h.pid), "event": string!(ev.event)}
.ts = format_timestamp!(t, "%Y-%m-%d %H:%M:%S%.3f", timezone: "UTC")
.side = ""
if starts_with(inst, "P") { .side = "prefill" } else if starts_with(inst, "D") { .side = "decode" }
if .side == "" { abort }
.model = "@@MODEL@@"
'''.replace("@@MODEL@@", model).replace("@@NATIVE_DIR@@", "(?:_native/)?" if native else "").replace(
        "@@NATIVE_ROLE@@", "|native" if native else "")

    ids = r'''
# The request key X: <prefix>-<X>-<8 hex> inside vLLM (request_id), <prefix>-<X> at the API (external_req_id);
# VLLM_DISABLE_REQUEST_ID_RANDOMIZATION leaves no suffix. api_summary[engine_submission] names its request in links.
.internal = null
.x = null
.attempt_id = null
rid = null
if is_string(.ev.request_id) {
  rid = string!(.ev.request_id)
} else if .event == "@@API@@" && is_array(.ev.links) {
  ls = filter(array!(.ev.links)) -> |_i, l| { is_object(l) && (l.kind == "request_id" || l.kind == "engine_request") && is_string(l.request_id) }
  if length(ls) > 0 { rid = string(ls[0].request_id) ?? null }
}
if is_string(rid) {
  .internal = rid
  i = replace(string!(rid), r'@@PREFIXES@@', "")
  mm, err = parse_regex(i, r'^(?P<x>.+)-(?P<att>[0-9a-f]{8})$')
  if err == null { .x = mm.x; .attempt_id = mm.att } else { .x = i; .attempt_id = "" }
} else if is_string(.ev.external_req_id) {
  .x = replace(string!(.ev.external_req_id), r'@@PREFIXES@@', "")
} else if is_string(.ev.api_request_id) {
  .x = replace(string!(.ev.api_request_id), r'@@PREFIXES@@', "")
}
.observation_id = null
if is_string(.ev.observation_id) { .observation_id = .ev.observation_id }
.task_id = null
'''.replace("@@API@@", API_EVENT).replace("@@PREFIXES@@", PREFIXES)

    to_rows = ("\n".join(events_vrl))
    yaml = []
    w = yaml.append
    w("# Generated by scripts/render_vector.py from segments.json. Do not edit; run bin/apply_segments.sh.")
    w("data_dir: %s" % vrl_str(data_dir))
    w("acknowledgements:\n  enabled: true")
    w("enrichment_tables:\n  inventory:\n    type: file\n    file:\n      path: %s\n      encoding:\n        type: csv\n    schema:\n      addr: string\n      name: string"
      % vrl_str(conf_dir + "/inventory.csv"))
    w("sources:\n  vector_metrics:\n    type: internal_metrics\n    scrape_interval_secs: 5\n  timing:\n    type: file\n"
      "    include: [\"/pull/**/*.log\"]\n%s    read_from: beginning\n    ignore_older_secs: 604800\n    max_line_bytes: 1048576\n"
      "    fingerprint:\n      strategy: checksum\n      lines: 1\n    oldest_first: true" % (
          "    exclude: [\"/pull/_proxy/**\", \"/pull/_mooncake/**\"]\n" if extra_dirs else ""))
    if proxy_cols:
        w("  proxy_files:\n    type: file\n    include: [\"/pull/_proxy/**/*.log\"]\n    read_from: beginning\n    ignore_older_secs: 604800\n"
          "    max_line_bytes: 1048576\n    fingerprint:\n      strategy: checksum\n      lines: 1\n    oldest_first: true")
    w("transforms:")

    def remap(name, inputs, source, extra=""):
        w("  %s:\n    type: remap\n    inputs: %s\n%s    source: |\n%s" % (
            name, json.dumps(inputs), extra, "\n".join("      " + x for x in source.strip("\n").splitlines())))

    remap("parse", ["timing"], parse, "    drop_on_error: true\n    reroute_dropped: true\n")
    w("  dead_letter_sample:\n    type: throttle\n    inputs: %s\n    threshold: 10\n    window_secs: 60" % (
        "[parse.dropped, proxy_parse.dropped]" if proxy_cols else "[parse.dropped]"))
    if proxy_cols:
        remap("proxy_parse", ["proxy_files"], PROXY_PARSE.replace("@@MODEL@@", model), "    drop_on_error: true\n    reroute_dropped: true\n")
        w("  proxy_kind:\n    type: route\n    inputs: [proxy_parse]\n    route:\n"
          "      request: '.event == \"pd_proxy_request\"'\n      selfcheck: '.event == \"pd_proxy_selfcheck\"'")
        remap("proxy_row", ["proxy_kind.request"], PROXY_ROW, "    drop_on_abort: true\n")
        remap("proxy_selfcheck_row", ["proxy_kind.selfcheck"], PROXY_SELFCHECK_ROW)
        # The proxy file's first line (pd_file_open) names the file for the collector's fingerprint only; it is not a
        # filter's file and stays out of vllm_pd.filter_file, which the checks read as the nodes' instances.
    w("  by_kind:\n    type: route\n    inputs: [parse]\n    route:\n"
      "      file_open: '.event == \"pd_file_open\"'\n      selfcheck: '.event == \"pd_filter_selfcheck\"'\n"
      "      partial: '.event == \"pd_seg_partial\"'\n      event: '%s'" % (
          "includes(%s, .event)" % json.dumps(sorted(passthrough))))
    # The event table: request-level events as they are.
    remap("event_row", ["by_kind.event"], ids + r'''
e = .ev
kind = e.scope
if !is_string(kind) { kind = e.phase }
name = .event
if is_string(kind) { name = string!(.event) + "[" + string!(kind) + "]" }
if !is_integer(e.duration_ns) && is_integer(e.start_ns) && is_integer(e.end_ns) { e.duration_ns = int!(e.end_ns) - int!(e.start_ns) }
. = {"ts": .ts, "node": .node, "instance": .instance, "side": .side, "role": .role, "proc_name": .proc_name, "pid": .pid,
     "event": name, "x": .x, "request_id": .internal, "observation_id": .observation_id, "task_id": .task_id,
     "execution_id": e.execution_id, "record_id": e.record_id, "duration_ns": e.duration_ns, "start_ns": e.start_ns,
     "end_ns": e.end_ns, "status": e.status, "raw": .raw}
if !is_string(.status) { .status = null }
if !is_integer(.duration_ns) { .duration_ns = null }
if !is_integer(.start_ns) { .start_ns = null }
if !is_integer(.end_ns) { .end_ns = null }
if !is_string(.execution_id) { .execution_id = null }
''')
    # Sparse phase_line rows from the events a segment reads.
    phase_src = ids + r'''
if !includes(%s, .event) { abort }
.proc = get(value: %s, path: [string!(.role)]) ?? "api"
.scope = .ev.scope
%s
if .ev.version == %s { .config_id = %s }
if .event == "core_summary" && is_integer(.ev.timestamp_ns) { .core_mono_ns = .ev.timestamp_ns }
# The API server's own start and end on the wall clock: api_summary[http] is written at the end, start = end - (end_ns - start_ns).
subs = []
if .event == %s && .scope == "http" {
  if is_integer(.ev.start_ns) && is_integer(.ev.end_ns) {
    te = parse_timestamp!(.ts, "%%Y-%%m-%%d %%H:%%M:%%S%%.3f")
    .api_end_ts = .ts
    .api_start_ts = format_timestamp!(from_unix_timestamp!(to_unix_timestamp(te, unit: "milliseconds") - to_int((int!(.ev.end_ns) - int!(.ev.start_ns)) / 1000000), unit: "milliseconds"), "%%Y-%%m-%%d %%H:%%M:%%S%%.3f", timezone: "UTC")
  }
  ls = array(.ev.links) ?? []
  subs = filter(ls) -> |_i, l| { is_object(l) && l.kind == "engine_submission" && is_string(l.external_req_id) }
}
del(.ev)
del(.raw)
# Fields a record does not have are left out rather than null, so that the merge keeps the first real value.
. = compact(., null: true, string: false, object: false, array: false, nullish: false)
# api_summary[http] has no request id: one copy per engine submission in its links, each with that request's key.
if length(subs) > 0 {
  base = .
  . = map_values(subs) -> |l| { merge(base, {"x": replace(string(l.external_req_id) ?? "", r'%s', "")}) }
}
''' % (json.dumps(mapped), json.dumps(proc_of), to_rows, json.dumps(record_version), vrl_str(cfg), vrl_str(API_EVENT), PREFIXES)
    shared = raw.get("shared_members") or []
    if shared:
        # v3.1-extra, C9: a record of these events that names no single request (a cache-pool task shared by several)
        # counts in full for every request of its members[], as a shared step does; one row per member.
        phase_src = phase_src.replace("del(.ev)\ndel(.raw)\n", SHARED_PICK % json.dumps(shared) + "del(.ev)\ndel(.raw)\n", 1) + SHARED_SPLIT.replace(
            "@@PREFIXES@@", PREFIXES)
    remap("phase_src", ["by_kind.event"], phase_src, "    drop_on_abort: true\n")
    w("  phase_split:\n    type: route\n    inputs: [phase_src]\n    route:\n"
      "      api: '.event == %s && is_string(.observation_id) && is_string(.x)'\n"
      "      direct: '!(.event == %s && is_string(.observation_id) && is_string(.x))'" % (vrl_str(API_EVENT), vrl_str(API_EVENT)))
    strat = strategies(tree, raw, cols)
    w("  api_merge:\n    type: reduce\n    inputs: [phase_split.api]\n    group_by: [file, observation_id, x]\n"
      "    ends_when: '.scope == \"http\"'\n    expire_after_ms: 900000\n    flush_period_ms: 1000\n"
      "    merge_strategies:\n%s" % "\n".join("      %s: %s" % (k, v) for k, v in sorted(strat.items())))
    remap("phase_row", ["phase_split.direct", "api_merge"], r'''
if !is_string(.x) || .x == "" { abort }
.request_id = .x
.part = ""
if !is_string(.attempt_id) { .attempt_id = "" }
.role = .side
del(.side)
del(.x)
del(.internal)
del(.event)
del(.file)
del(.observation_id)
del(.task_id)
del(.proc_name)
del(.pid)
del(.events)
del(.scope)
''', "    drop_on_abort: true\n")
    remap("partial_row", ["by_kind.partial"], ids + "\n" + "\n".join(partial_vrl) + r'''
if !is_string(.x) || .x == "" { abort }
. = merge(., {"request_id": .x, "part": string!(.ev.part), "proc": get(value: %s, path: [string!(.role)]) ?? "worker",
              "first_mono_ns": .ev.first_mono_ns, "seq": .ev.seq, "role": .side})
del(.side)
del(.x)
del(.internal)
del(.ev)
del(.raw)
del(.event)
del(.file)
del(.observation_id)
del(.task_id)
del(.proc_name)
del(.pid)
''' % json.dumps(proc_of), "    drop_on_abort: true\n")
    remap("selfcheck_row", ["by_kind.selfcheck"], r'''
e = .ev
. = {"ts": .ts, "node": .node, "instance": .instance, "pid": e.pid, "config_version": e.config_version,
     "seen": e.seen, "written": e.written, "dropped": e.dropped, "no_event": e.no_event, "rank_dropped": e.rank_dropped,
     "accumulated": e.accumulated, "empty_key_dropped": e.empty_key_dropped, "errors": e.errors,
     "partials_written": e.partials_written, "evicted": e.evicted, "forced": e.forced, "config_reloads": e.config_reloads,
     "config_errors": e.config_errors, "inflight": e.inflight, "python": e.python, "files": e.files, "role": .role,
     "instance_source": e.instance_source, "dp_rank_argv": e.dp_rank_argv, "version_dropped": e.version_dropped}
''' + ("""if is_integer(e.cand_maps) { .cand_maps = e.cand_maps; .cand_applied = e.cand_applied; .cand_unpaired = e.cand_unpaired; .cand_evicted_full = e.cand_evicted_full }
""" if raw.get("candidate") else "") + ("""if is_object(e.acc_fallback) { .acc_fallback = e.acc_fallback }
""" if any(isinstance(a.get("value"), list) for a in raw.get("accumulate", [])) else "") + ("""if is_integer(e.exec_missing) { .exec_missing = e.exec_missing }
""" if raw.get("missing_execution_id") else ""))
    # Anything that is neither a request-level event nor the filter's own lines: on a node whose filter works there is none.
    remap("other_row", ["by_kind._unmatched"], r'''
. = {"ts": .ts, "node": .node, "instance": .instance, "role": .role, "pid": .pid, "event": .event}
''')
    remap("file_row", ["by_kind.file_open"], r'''
. = {"ts": .ts, "node": .node, "instance": .instance, "role": .role, "pid": .pid, "file": .ev.file,
     "config_version": .ev.config_version}
''')

    def sink(name, inputs, table):
        w("  %s:\n    type: clickhouse\n    inputs: %s\n    endpoint: %s\n    database: vllm_pd\n    table: %s\n"
          "    auth:\n      strategy: basic\n      user: vllm_writer\n      password: %s\n    format: json_each_row\n"
          "    skip_unknown_fields: true\n    date_time_best_effort: true\n    compression: gzip\n"
          "    query_settings:\n      async_insert_settings:\n        enabled: true\n        wait_for_processing: true\n"
          "    batch:\n      timeout_secs: 5\n      max_events: 20000\n"
          "    buffer:\n      type: disk\n      max_size: 268435488\n      when_full: block\n"
          "    acknowledgements:\n      enabled: true" % (name, json.dumps(inputs), vrl_str(ch), table, vrl_str(pw)))

    w("sinks:")
    sink("ch_event", ["event_row"], "diag_event")
    sink("ch_phase", ["phase_row", "partial_row"], "phase_line")
    sink("ch_selfcheck", ["selfcheck_row"], "filter_selfcheck")
    sink("ch_file", ["file_row"], "filter_file")
    sink("ch_other", ["other_row"], "filter_other")
    if proxy_cols:
        sink("ch_proxy", ["proxy_row"], "proxy_line")
        sink("ch_proxy_selfcheck", ["proxy_selfcheck_row"], "proxy_selfcheck")
    w("  dead_letter:\n    type: file\n    inputs: [dead_letter_sample]\n    path: %s\n    encoding:\n      codec: json"
      % vrl_str(data_dir + "/dead-letter-%Y-%m-%d.jsonl"))
    w("  metrics:\n    type: prometheus_exporter\n    inputs: [vector_metrics]\n    address: \"0.0.0.0:9598\"")
    return "\n".join(yaml) + "\n"


def main():
    if len(sys.argv) >= 2 and sys.argv[1] == "--run-script":
        print(RUN_SCRIPT)
        return
    if len(sys.argv) >= 2 and sys.argv[1] == "--print":
        sys.stdout.write(build(os.environ, "/var/lib/vector", "/etc/vector", placeholders=True))
        return
    dst, data_dir, conf_dir = sys.argv[1:4]
    text = build(os.environ, data_dir, conf_dir)
    with open(dst, "w", encoding="utf-8") as f:
        f.write(text)


if __name__ == "__main__":
    main()
