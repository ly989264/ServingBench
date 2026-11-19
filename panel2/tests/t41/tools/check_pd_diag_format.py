#!/usr/bin/env python3
"""Format check for the [PD_DIAG] lines (version 2) in redirected vLLM logs (panel 2).

Usage:
  python3 check_pd_diag_format.py <file or folder> [...]            one summary per file, then a total
  python3 check_pd_diag_format.py --max-lines 200000 logs/          only the first 200000 lines of each file
  python3 check_pd_diag_format.py --show-examples logs/P0.log       also print the first lines of each problem
  python3 check_pd_diag_format.py --json report.json logs/           also write the summary as JSON

- Folders are read recursively; .gz files are read compressed (recognised by content). Files are read line by line, so
  their size does not matter. Python 3.6 or later, standard library only, no network.
- A line counts as an observation when it contains "[PD_DIAG] "; whatever comes before it (vLLM's
  "(EngineCore pid=N) INFO MM-DD hh:mm:ss [file:line]" or a custom prefix) is accepted. Other lines are only counted;
  Mooncake's "[BatchPutTiming]" / "[BatchUpsertTiming]" lines are counted separately.
- "Known format" is version 2: one compact JSON object after "[PD_DIAG] ", with the common fields in COMMON on every
  line, in one of 7 kinds: api_summary with scope http, engine_submission or request; core_summary with scope request or
  execution; worker_summary with phase execute or sample. KINDS lists, for each kind, the fields and stages panel 2
  reads; a stage is an object with duration_ns (null when it did not run), start_ns, end_ns and status.
- By default no log content is printed: only event names, field names, counts and line numbers. --show-examples
  prints the first lines of each problem (cut to 300 characters).

Errors (the line is not in the known format): E1 not JSON after [PD_DIAG]; E2 unknown event, or unknown scope / phase;
E3 a common field is missing; E4 a common field has the wrong type, or version is not 2; E5 a stage panel 2 reads is
missing, or its duration_ns is neither an integer nor null; E6 a field panel 2 reads is missing; E7 a field panel 2
reads has the wrong type; E8 negative duration; E9 end before start; E10 a stage with one call whose duration_ns differs
from end_ns - start_ns.
Warnings (format is right, worth a look): W1 a request-level record without its ID (observation_id, request_id); W2 a
stage whose status is not ok or skipped; W3 a worker_summary without tp_rank; W4 bytes that are not UTF-8; W5 a
Mooncake timing line without keys=.

Exit code: 0 every [PD_DIAG] line is in the known format (warnings allowed; --strict makes them count); 1 errors
found; 2 bad arguments or a file that cannot be read; 3 no [PD_DIAG] line at all.
"""
import argparse
import collections
import gzip
import io
import json
import os
import re
import sys

MARK = "[PD_DIAG] "
VERSION = 2
COMMON = (("event", str), ("version", int), ("host", str), ("pid", int), ("native_tid", int), ("run_id", str),
          ("timestamp_ns", int), ("record_id", str), ("status", str), ("stages", dict))
NUM = "number"      # an int or a float, not a bool
KIND_FIELD = {"api_summary": "scope", "core_summary": "scope", "worker_summary": "phase"}

# The 7 kinds, and what panel 2 reads from each: top-level fields with their types, the stages, and nested fields.
# A value may be null (a request that ended early); a missing key is an error.
KINDS = {
    ("api_summary", "http"): {
        "fields": {"observation_id": str, "start_ns": int, "end_ns": int, "http_status": int},
        "stages": ("req_resolve", "render", "tokenize"), "id": "observation_id"},
    ("api_summary", "engine_submission"): {
        "fields": {"observation_id": str}, "stages": ("add_request",), "id": "observation_id"},
    ("api_summary", "request"): {
        "fields": {"observation_id": str, "request_id": str, "official": dict, "terminal": dict},
        "nested": {"official": {"e2e_latency": NUM, "time_to_first_token": NUM, "mean_time_per_output_token": NUM,
                                "num_prompt_tokens": int, "num_cached_tokens": int, "num_generation_tokens": int,
                                "status": str},
                   "terminal": {"detokenize_duration_ns": int}},
        "stages": (), "id": "request_id"},
    ("core_summary", "request"): {
        "fields": {"request_id": str, "members": list}, "stages": ("queue", "hash"),
        "stage_fields": {"queue": {"initial_wait_ns": int}},
        "members": {"request_id": str, "executions": int}, "id": "request_id"},
    ("core_summary", "execution"): {
        "fields": {"execution_id": str, "request_ids": list}, "stages": ("lookup", "alloc", "schedule", "other")},
    ("worker_summary", "execute"): {
        # A step with no forward (no_forward true, request_ids empty) has no forward stage.
        "fields": {"execution_id": str, "request_ids": list}, "stages": ("forward",), "stages_unless": "no_forward"},
    ("worker_summary", "sample"): {
        "fields": {"execution_id": str, "request_ids": list}, "stages": ("sampling",)},
}
STAGE_STATUS = ("ok", "skipped")

PROBLEMS = collections.OrderedDict([
    ("E1", ("错误", "[PD_DIAG] 后面不是一个完整的 JSON 对象")),
    ("E2", ("错误", "未知事件，或 scope / phase 不在 7 类记录里")),
    ("E3", ("错误", "缺公共字段")),
    ("E4", ("错误", "公共字段类型不对，或 version 不是 2")),
    ("E5", ("错误", "缺面板二读的 stage，或 stage 的 duration_ns 既不是整数也不是 null")),
    ("E6", ("错误", "缺面板二读的字段")),
    ("E7", ("错误", "面板二读的字段类型不对")),
    ("E8", ("错误", "时长为负")),
    ("E9", ("错误", "结束早于开始")),
    ("E10", ("错误", "只调用一次的 stage，duration_ns 不等于 end_ns − start_ns")),
    ("W1", ("提醒", "请求级记录没有它的 ID（observation_id / request_id）")),
    ("W2", ("提醒", "stage 的 status 不是 ok 或 skipped")),
    ("W3", ("提醒", "worker_summary 没有 tp_rank，认不出 TP rank")),
    ("W4", ("提醒", "行里有不是 UTF-8 的字节")),
    ("W5", ("提醒", "Mooncake 计时行没有 keys=")),
])
PROC = re.compile(r"\(([A-Za-z][A-Za-z0-9_]*) pid=\d+\)")


def is_int(v):
    return isinstance(v, int) and not isinstance(v, bool)


def type_ok(v, t):
    if v is None:
        return True
    if t is int:
        return is_int(v)
    if t == NUM:
        return (is_int(v) or isinstance(v, float))
    return isinstance(v, t)


def open_text(path):
    raw = open(path, "rb")
    head = raw.read(2)
    raw.seek(0)
    if head == b"\x1f\x8b":
        raw = gzip.GzipFile(fileobj=raw)
    return raw


def files_of(paths):
    for p in paths:
        if os.path.isdir(p):
            for root, dirs, names in os.walk(p):
                dirs.sort()
                for n in sorted(names):
                    yield os.path.join(root, n)
        else:
            yield p


class FileReport(object):
    def __init__(self, path):
        self.path = path
        self.lines = self.pd = self.mooncake = self.other = 0
        self.events = collections.Counter()
        self.procs = collections.Counter()
        self.problems = collections.OrderedDict()   # code -> [lines, [line numbers], [examples], {detail: times}, times]
        self.truncated = False
        self.err_lines = self.warn_lines = 0        # lines with at least one error / warning
        self._last = {}                             # code (and "E", "W") -> last line number counted

    def add(self, code, lineno, raw, detail, opts):
        """One problem on one line. Counts are of lines (a line missing 7 fields is one line); the details count
        every occurrence (times)."""
        p = self.problems.setdefault(code, [0, [], [], collections.Counter(), 0])
        p[4] += 1
        if detail:
            p[3][detail] += 1
        if self._last.get(code) != lineno:
            self._last[code] = lineno
            p[0] += 1
            if len(p[1]) < opts.limit:
                p[1].append(lineno)
            if opts.show_examples and len(p[2]) < opts.limit:
                p[2].append((lineno, raw[:300]))
        level = code[0]
        if self._last.get(level) != lineno:
            self._last[level] = lineno
            if level == "E":
                self.err_lines += 1
            else:
                self.warn_lines += 1


def check_times(o, add, where, one_call):
    s, e, d = o.get("start_ns"), o.get("end_ns"), o.get("duration_ns")
    if is_int(d) and d < 0:
        add("E8", where + "duration_ns")
    if is_int(s) and is_int(e):
        if e < s:
            add("E9", where + "end_ns<start_ns")
        if one_call and is_int(d) and d != e - s:
            add("E10", where + "duration_ns")


def check_fields(o, spec, add, where):
    for f, t in spec.items():
        if f not in o:
            add("E6", where + f)
        elif not type_ok(o[f], t):
            add("E7", where + f)


def check_record(o, add):
    """Checks one record; returns the name it is counted under (api_summary[http], ...)."""
    for f, t in COMMON:
        if f not in o:
            add("E3", f)
        elif not type_ok(o[f], t) or o[f] is None:
            add("E4", f)
    if is_int(o.get("version")) and o["version"] != VERSION:
        add("E4", "version=%s" % o["version"])
    ev = o.get("event")
    if not isinstance(ev, str):
        return None
    kf = KIND_FIELD.get(ev)
    if kf is None:
        add("E2", ev)
        return ev
    name = "%s[%s]" % (ev, o.get(kf))
    spec = KINDS.get((ev, o.get(kf)))
    if spec is None:
        add("E2", "%s %s=%s" % (ev, kf, o.get(kf)))
        return name
    check_fields(o, spec["fields"], add, name + ".")
    for f, sub in spec.get("nested", {}).items():
        if isinstance(o.get(f), dict):
            check_fields(o[f], sub, add, "%s.%s." % (name, f))
    if isinstance(o.get("members"), list) and "members" in spec:
        for m in o["members"]:
            if isinstance(m, dict):
                check_fields(m, spec["members"], add, name + ".members[].")
            else:
                add("E7", name + ".members[]")
    stages = o.get("stages") if isinstance(o.get("stages"), dict) else {}
    need = spec["stages"] if not (spec.get("stages_unless") and o.get(spec["stages_unless"]) is True) else ()
    for st in need:
        if st not in stages:
            add("E5", "%s.stages.%s" % (name, st))
    for st, v in stages.items():
        where = "%s.stages.%s." % (name, st)
        if not isinstance(v, dict):
            if st in spec["stages"]:
                add("E5", where[:-1])
            continue
        if st in spec["stages"]:
            if "duration_ns" not in v or not type_ok(v["duration_ns"], int):
                add("E5", where + "duration_ns")
            check_fields(v, spec.get("stage_fields", {}).get(st, {}), add, where)
        if v.get("status") is not None and v.get("status") not in STAGE_STATUS:
            add("W2", "%s:%s" % (where + "status", v.get("status")))
        check_times(v, add, where, v.get("calls") == 1)
        if is_int(v.get("initial_wait_ns")) and v["initial_wait_ns"] < 0:
            add("E8", where + "initial_wait_ns")
    if ev == "api_summary" and o.get(kf) == "http":
        check_times(o, add, name + ".", False)
    if ev == "api_summary" and isinstance(o.get("terminal"), dict):
        d = o["terminal"].get("detokenize_duration_ns")
        if is_int(d) and d < 0:
            add("E8", name + ".terminal.detokenize_duration_ns")
    if spec.get("id") and not o.get(spec["id"]):
        add("W1", "%s.%s" % (name, spec["id"]))
    if ev == "worker_summary" and not is_int(o.get("tp_rank")):
        add("W3", name)
    return name


def scan(path, opts):
    rep = FileReport(path)
    try:
        fh = open_text(path)
    except (IOError, OSError) as e:
        return rep, str(e)
    try:
        for lineno, b in enumerate(fh, 1):
            if opts.max_lines and lineno > opts.max_lines:
                rep.truncated = True
                break
            rep.lines += 1
            try:
                line = b.decode("utf-8")
            except UnicodeDecodeError:
                line = b.decode("utf-8", "replace")
                rep.add("W4", lineno, line, None, opts)
            i = line.find(MARK)
            if i < 0:
                if "[BatchPutTiming]" in line or "[BatchUpsertTiming]" in line:
                    rep.mooncake += 1
                    if "keys=" not in line:
                        rep.add("W5", lineno, line, None, opts)
                else:
                    rep.other += 1
                continue
            rep.pd += 1
            m = PROC.search(line, 0, i)
            rep.procs[m.group(1) if m else "（没有进程名前缀）"] += 1

            def add(code, detail, _n=lineno, _l=line):
                rep.add(code, _n, _l.rstrip("\n"), detail, opts)
            try:
                o = json.loads(line[i + len(MARK):])
            except ValueError:
                add("E1", None)
                continue
            if not isinstance(o, dict):
                add("E1", None)
                continue
            ev = check_record(o, add)
            rep.events[ev if isinstance(ev, str) else "（没有 event）"] += 1
    except (IOError, OSError, EOFError, gzip.zlib.error if hasattr(gzip, "zlib") else IOError) as e:
        return rep, "读到第 %d 行出错：%s" % (rep.lines, e)
    finally:
        fh.close()
    return rep, None


def show(rep, opts, out):
    out.write("\n== %s\n" % rep.path)
    out.write("   行数 %d（%s）：[PD_DIAG] %d，Mooncake 计时 %d，其他 %d\n" % (
        rep.lines, "只读了前 %d 行" % opts.max_lines if rep.truncated else "读完", rep.pd, rep.mooncake, rep.other))
    if rep.procs:
        out.write("   进程：%s\n" % "，".join("%s %d" % kv for kv in rep.procs.most_common()))
    if rep.events:
        out.write("   记录（%d 类）：%s\n" % (len(rep.events), "，".join("%s %d" % kv for kv in sorted(rep.events.items(), key=lambda x: (-x[1], x[0])))))
    if not rep.problems:
        out.write("   问题：没有\n")
    else:
        out.write("   有错误的行 %d，有提醒的行 %d（同一行有几处问题只算一行）\n" % (rep.err_lines, rep.warn_lines))
        old = sum(n for d, n in rep.problems.get("E4", [0, [], [], collections.Counter(), 0])[3].items() if d.startswith("version=") and d != "version=%d" % VERSION)
        if old:
            out.write("   %d 处 version 不是 %d：这是别的版本的补丁写的日志，面板二这一版只认 version %d\n" % (old, VERSION, VERSION))
    for code in PROBLEMS:
        if code not in rep.problems:
            continue
        n, linenos, examples, details, times = rep.problems[code]
        level, text = PROBLEMS[code]
        out.write("   %s %s %s：%d 行，行号 %s%s\n" % (level, code, text, n, ", ".join(str(x) for x in linenos), " …" if n > len(linenos) else ""))
        if details:
            out.write("        涉及（按字段计，单位：次）：%s\n" % "，".join("%s ×%d 次" % kv for kv in details.most_common(10)))
        for lineno, raw in examples:
            out.write("        例 %d：%s\n" % (lineno, raw))


def main(argv=None):
    ap = argparse.ArgumentParser(description="[PD_DIAG] 日志格式校验（version 2，面板二）")
    ap.add_argument("paths", nargs="+", help="日志文件或目录（递归；.gz 可以）")
    ap.add_argument("--max-lines", type=int, default=0, help="每个文件只读前 N 行（默认读完）")
    ap.add_argument("--limit", type=int, default=5, help="每类问题最多列几个行号（默认 5）")
    ap.add_argument("--show-examples", action="store_true", help="打印每类问题的前几行原文（会输出日志内容）")
    ap.add_argument("--strict", action="store_true", help="提醒也算不符合（退出码 1）")
    ap.add_argument("--json", help="同时把汇总写成 JSON 文件")
    opts = ap.parse_args(argv)
    out = sys.stdout
    if hasattr(out, "buffer"):
        out = io.TextIOWrapper(out.buffer, encoding="utf-8", errors="replace", line_buffering=True)
    if opts.show_examples:
        out.write("注意：--show-examples 会把日志原文打印出来。\n")
    reports, unreadable = [], []
    for path in files_of(opts.paths):
        if not os.path.exists(path):
            unreadable.append((path, "不存在"))
            continue
        rep, err = scan(path, opts)
        if err:
            unreadable.append((path, err))
        reports.append(rep)
        show(rep, opts, out)
    total_pd = sum(r.pd for r in reports)
    tot = collections.Counter()
    for r in reports:
        for code, p in r.problems.items():
            tot[code] += p[0]
    errors = sum(r.err_lines for r in reports)
    warns = sum(r.warn_lines for r in reports)
    out.write("\n== 合计：%d 个文件，%d 行，[PD_DIAG] %d 行，记录 %d 类；有错误的行 %d，有提醒的行 %d（按行去重）\n" % (
        len(reports), sum(r.lines for r in reports), total_pd, len(set(e for r in reports for e in r.events)), errors, warns))
    for code in PROBLEMS:
        if tot.get(code):
            out.write("   %s %s %s：%d 行\n" % (PROBLEMS[code][0], code, PROBLEMS[code][1], tot[code]))
    for path, err in unreadable:
        out.write("   读不了：%s（%s）\n" % (path, err))
    if unreadable:
        code, result = 2, "FAIL（有文件读不了）"
    elif total_pd == 0:
        code, result = 3, "NO_PD_DIAG"
        out.write("   没有找到 [PD_DIAG] 行。可能的原因：这批日志是在装好面板二之后写的（面板的 logging 配置会把这些行"
                  "从 stdout 去掉，改写进节点上过滤器的文件），或者给的不是 vLLM 的 stdout 日志。\n")
    elif errors or (opts.strict and warns):
        code, result = 1, "FAIL"
    else:
        code, result = 0, "OK" if not warns else "OK（有提醒）"
    out.write("RESULT: %s\n" % result)
    if opts.json:
        doc = {"result": result, "exit_code": code, "files": [{
            "path": r.path, "lines": r.lines, "pd_diag": r.pd, "mooncake": r.mooncake, "other": r.other, "truncated": r.truncated,
            "processes": dict(r.procs), "events": dict(r.events),
            "error_lines": r.err_lines, "warning_lines": r.warn_lines,
            "problems": {c: {"level": PROBLEMS[c][0], "what": PROBLEMS[c][1], "lines_count": p[0], "times": p[4], "lines": p[1],
                             "details_times": dict(p[3])}
                         for c, p in r.problems.items()}} for r in reports], "unreadable": [list(x) for x in unreadable]}
        with io.open(opts.json, "w", encoding="utf-8") as f:
            f.write(json.dumps(doc, ensure_ascii=False, indent=1))
    return code


if __name__ == "__main__":
    sys.exit(main())
