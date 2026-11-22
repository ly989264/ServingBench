#!/usr/bin/env python3
"""Reads segments.json, the one place where the latency segments are defined.

A segment's column is its path joined by "__", for example schedule__lookup_ms. A segment under "tpot"
gets _rest before _ms, for example schedule__alloc_rest_ms. Any depth works.

A log line may write a segment directly, or only its children, depending on the level vLLM is configured for.
A value written directly wins; without one, the value is the sum of the children; with neither, it is empty.
"""
import json
import os
import pathlib
import re

PACK = pathlib.Path(__file__).resolve().parent.parent
PROCS = ("api", "engine", "worker", "save", "proxy")


class Node:
    def __init__(self, raw, role, phase, parent=None, where=""):
        self.id = raw.get("id", "")
        where = "%s/%s" % (where, self.id)
        if not re.fullmatch(r"[a-z][a-z0-9]*(_[a-z0-9]+)*", self.id):
            raise SystemExit("segments.json：%s 的 id 只能用小写字母、数字和单个下划线" % where)
        self.role, self.phase, self.parent = role, phase, parent
        self.depth = 1 if parent is None else parent.depth + 1
        self.path = [self.id] if parent is None else parent.path + [self.id]
        self.proc = raw.get("proc") or (parent.proc if parent else None)
        self.optional = bool(raw.get("optional"))
        self.label = raw.get("label", self.id)
        self.short = raw.get("short", self.id)
        # Segments measured on the proxy live on the proxy's line. "column" names that field when the path alone would not be unique across
        # P and D. "before_proxy" marks time that passed before the proxy got the request, which the proxy's own totals do not include.
        self.own_column = raw.get("column")
        self.before_proxy = bool(raw.get("before_proxy"))
        # "parallel": overlaps other segments in time (EngineCore and Worker wrappers around the steps); listed below the
        # side's total, never added to it, drawn on the timeline beside the segments it overlaps.
        self.parallel = bool(raw.get("parallel"))
        # T56: "compare": a comparison row (vLLM's own figure, or the receive thread's wall clock), placed right after the
        # segments it compares with, never a segment: no share of the side's total, not in the bars, the sum checks or the
        # timeline. "outside_total": a segment that overlaps others in time (host time beside device time, the overhead
        # A/B's host variant): shown, no share, not added into the side's first-level sums or bars.
        self.compare = bool(raw.get("compare"))
        self.outside_total = bool(raw.get("outside_total")) or self.compare
        # T58: "from_level": the lowest PD_DIAG_LEVEL (1, 2, 3) at which the patches record this segment; a request whose
        # side ran below it has no value here by design ("未采"), which the dashboard, the T55 column and coverage tell
        # apart from a missing value. Default 1 (recorded at every level); never below the parent's.
        self.from_level = raw.get("from_level", parent.from_level if parent is not None else 1)
        if self.from_level not in (1, 2, 3) or (parent is not None and self.from_level < parent.from_level):
            raise SystemExit("segments.json：%s 的 from_level 只能是 1、2、3，且不小于上一层的" % where)
        # Where the value comes from: a list of {"event": ..., "where": ..., "field": ...} (a field of a request-level record),
        # {"acc": ..., "part": ...} (the filter's per-request sums), or {"event": ..., "ranges": ..., "total": ..., "part":
        # "first" | "rest"} (the range of a request-level record's per-call list whose execution_id is the request's first
        # step, and the total minus it); null means the log has nothing for the segment, and it stays empty.
        # Under "proxy" (v3.1-extra) only {"proxy": <field>}: a field of the proxy's line for the request (vllm_pd.proxy_line).
        self.src = raw.get("src")
        for s in self.src or []:
            if phase == "proxy":
                if set(s) != {"proxy"} or not re.fullmatch(r"[a-z][a-z0-9_]*_ms", str(s["proxy"])):
                    raise SystemExit("segments.json：%s 在 proxy 下，src 只能写 {\"proxy\": \"<proxy 行的 *_ms 字段>\"}" % where)
                continue
            if ("event" in s) == ("acc" in s):
                raise SystemExit("segments.json：%s 的 src 每一项要写 event 或 acc 之一" % where)
            if "acc" in s and s.get("part", "total") not in ("total", "first", "rest"):
                raise SystemExit("segments.json：%s 的 part 只能是 total、first、rest" % where)
            if "ranges" in s and (s.get("part") not in ("first", "rest") or "total" not in s):
                raise SystemExit("segments.json：%s 的 ranges 要写 total，part 只能是 first、rest" % where)
            if s.get("unit", "ns") not in ("ns", "s") or ("unit" in s and "event" not in s):
                raise SystemExit("segments.json：%s 的 unit 只能写在 event 来源上，取 ns（缺省）或 s" % where)
        self.children = []
        seen = set()
        for c in raw.get("children", []):
            child = Node(c, role, phase, self, where)
            if child.id in seen:
                raise SystemExit("segments.json：%s 下有两个 %s" % (where, child.id))
            seen.add(child.id)
            self.children.append(child)
        if self.compare and (self.children or not self.src or parent is not None):
            raise SystemExit("segments.json：%s 是对照行（compare），只能是第一层、没有子段、要写 src" % where)
        if phase == "proxy":
            self.proc = "proxy"
        if self.proc not in PROCS:
            raise SystemExit("segments.json：%s 要写 proc（%s 之一）" % (where, "、".join(PROCS)))

    @property
    def level(self):
        return self.depth

    @property
    def column(self):
        if self.own_column:
            return self.own_column
        return "__".join(self.path) + ("_rest" if self.phase == "tpot" else "") + "_ms"

    def procs(self):
        """The processes whose lines can hold this segment or something below it."""
        out = {self.proc}
        for c in self.children:
            out |= c.procs()
        return out

    def columns_below(self):
        """Every column of this node and its descendants."""
        out = [self.column]
        for c in self.children:
            out += c.columns_below()
        return out

    def walk(self):
        yield self
        for c in self.children:
            yield from c.walk()


def range_name(src):
    parts = src["ranges"].split(".")
    return parts[-2] if len(parts) > 1 else parts[0]


class Tree:
    def __init__(self, path=None):
        path = path or os.environ.get("SEGMENTS_FILE") or PACK / "segments.json"
        with open(path, encoding="utf-8") as f:
            raw = json.load(f)
        self.version = raw.get("version", 1)
        if self.version != 2:
            raise SystemExit("segments.json 要 version 2")
        self.raw = raw
        self.acc_ids = []
        # Sums the filter keeps per request: accumulated per-step spans, and durations made from two paired point events.
        for a in raw.get("accumulate", []) + (raw.get("candidate") or {}).get("accumulate", []):
            if a["id"] not in self.acc_ids:
                self.acc_ids.append(a["id"])
        self.request_fields = raw.get("request", {})
        self.sides = {}
        for role in ("prefill", "decode"):
            s = raw["sides"][role]
            self.sides[role] = {
                "name": s["name"],
                "total_label": s["total_label"],
                "proxy": [Node(x, role, "proxy", None, role + "/proxy") for x in s.get("proxy", [])],
                "ttft": [Node(x, role, "ttft", None, role + "/ttft") for x in s.get("ttft", [])],
                "tpot": [Node(x, role, "tpot", None, role + "/tpot") for x in s.get("tpot", [])],
                "background": [Node(x, role, "background", None, role + "/background") for x in s.get("background", [])],
            }

    def nodes(self, role, *phases):
        out = []
        for ph in phases:
            for top in self.sides[role][ph]:
                out += list(top.walk())
        return out

    def phase_line_columns(self):
        """All segment columns of the phase_line table, without duplicates, in definition order."""
        out = []
        for role in ("prefill", "decode"):
            for n in self.nodes(role, "ttft", "tpot", "background"):
                if n.column not in out:
                    out.append(n.column)
        return out

    def event_columns(self):
        """The segment columns Vector fills directly from a record's field (the others come from the filter's sums, from
        per-call ranges, or stay empty)."""
        out = []
        for role in ("prefill", "decode"):
            for n in self.nodes(role, "ttft", "tpot", "background"):
                if any("event" in s and "ranges" not in s for s in n.src or []) and n.column not in out:
                    out.append(n.column)
        return out

    def ranges(self):
        """{name: src}: the per-call range lists a segment reads (name: the path element before "ranges", detokenize)."""
        out = {}
        for role in ("prefill", "decode"):
            for n in self.nodes(role, "ttft", "tpot", "background"):
                for s in n.src or []:
                    if "ranges" in s:
                        out.setdefault(range_name(s), s)
        return out

    def latest_columns(self):
        """Columns read from core_summary[request]: a request may have more than one; the one written last counts."""
        out = []
        for role in ("prefill", "decode"):
            for n in self.nodes(role, "ttft", "tpot", "background"):
                if any(s.get("event") == "core_summary" for s in n.src or []) and n.column not in out:
                    out.append(n.column)
        for k, spec in self.request_fields.items():
            if spec.get("event") == "core_summary" and k not in out:
                out.append(k)
        return out

    def proxy_sourced(self):
        """[(column, field of the proxy line)] for the proxy segments that read the proxy's line (v3.1-extra); [] when none
        does, and then nothing about the proxy differs from v3."""
        out = []
        for role in ("prefill", "decode"):
            for n in self.nodes(role, "proxy"):
                for s in n.src or []:
                    if (n.column, s["proxy"]) not in out:
                        out.append((n.column, s["proxy"]))
        return out

    def summary_columns(self):
        out = []
        for role in ("prefill", "decode"):
            for n in self.nodes(role, "proxy"):
                if n.column not in out:
                    out.append(n.column)
        return out

    def proc_columns(self, role):
        """{proc: [(column, node)]}: the columns a process of this side writes."""
        out = {}
        for n in self.nodes(role, "ttft", "tpot", "background"):
            out.setdefault(n.proc, []).append((n.column, n))
        return out
