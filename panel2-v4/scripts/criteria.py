#!/usr/bin/env python3
"""Judgement functions shared by the upgrade-safety check (bin/panel2_cluster.sh check), the post-deployment check and
diag.py, so that every tool gives the same answer on the same facts.

The functions here do not collect anything: callers gather the facts their own way (docker, ClickHouse, Prometheus)
and pass them in. Nothing here writes files or changes any service.

Every judgement returns a Verdict:
    Verdict(level, text, data)
    level   "OK" | "WARN" | "FAIL" | "INFO"
    text    one summary line in Chinese (the caller prefixes the item)
    data    dict of the numbers behind the line, for reports and tests

Python 3.9 and later, standard library only.
"""
import hashlib
import json
import os
from dataclasses import dataclass, field

LEVELS = ("OK", "INFO", "WARN", "FAIL")
# Within this many seconds after the fetcher switched to another segments.json, an instance still reporting the
# previous version is INFO: the filter rereads segments.json every 2 s and self-checks once a minute.
SWITCH_WINDOW_S = 120


@dataclass
class Verdict:
    level: str
    text: str
    data: dict = field(default_factory=dict)


def worst(verdicts):
    """The highest level among verdicts (FAIL > WARN > INFO > OK); "OK" for an empty list."""
    return max((v.level for v in verdicts), key=LEVELS.index, default="OK")


def file_sha256(path):
    """sha256 hex of a file's bytes."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# ==================== segments.json version ====================
def segments_version(path):
    """The version string a filter reports for this segments.json: "<config_id>@<sha1 of the file's bytes, 8 hex>"."""
    raw = open(path, "rb").read()
    cid = json.loads(raw.decode("utf-8")).get("config_id", "?")
    return "%s@%s" % (cid, hashlib.sha1(raw).hexdigest()[:8])


def expected_version(cluster_dir):
    """The version of the cluster folder's segments.json; None (not applicable) when the folder has none: segments.json
    belongs to the cluster side, and the monitoring side gets the segment definitions from interface one."""
    path = os.path.join(cluster_dir, "segments.json")
    if not os.path.isfile(path):
        return None
    return segments_version(path)


def segments_key_diff(installed, packed, ignore=("line.log_dir",)):
    """Key-level differences between two parsed segments.json, as [(dotted key, installed value, pack value)], keys in
    `ignore` left out. Lists compare as whole values."""
    out = []

    def walk(a, b, prefix):
        if isinstance(a, dict) and isinstance(b, dict):
            for k in sorted(set(a) | set(b)):
                walk(a.get(k), b.get(k), prefix + "." + k if prefix else k)
        elif a != b and prefix not in ignore:
            out.append((prefix, a, b))

    walk(installed, packed, "")
    return out


def judge_config_versions(versions, fetcher, defined, since_change=None):
    """The segment definition versions in use, from the status documents.
    versions: {instance: config_version of its latest status document, "" when the fetcher has seen no record of it};
    fetcher: config_version of the fetcher's latest status document ("" when there is none); defined: the
    config_versions in segment_def; since_change: seconds since the fetcher's last change of segments.json, None when
    unknown. An instance on another version is FAIL, INFO within SWITCH_WINDOW_S after the fetcher's change.
    Returns a list of Verdicts."""
    data = {"versions": dict(versions), "fetcher": fetcher, "defined": sorted(defined), "since_change": since_change}
    if not fetcher:
        return [Verdict("FAIL", "没有采集进程的状态文档，无法核对段定义版本", data)]
    out = []
    if fetcher not in defined:
        out.append(Verdict("FAIL", "采集进程的段定义版本 %s 不在 segment_def 中（入库进程尚未取到这一版段定义）" % fetcher, data))
    quiet = sorted(i for i, v in versions.items() if not v)
    if quiet:
        out.append(Verdict("WARN", "%s 启动以后采集进程还没有读到记录，无法核对版本；有请求后再查" % " ".join(quiet), data))
    other = {}
    for i, v in sorted(versions.items()):
        if v and v != fetcher:
            other.setdefault(v, []).append(i)
    if other:
        text = "；".join("%s 为 %s" % (" ".join(ins), v) for v, ins in sorted(other.items()))
        if since_change is not None and since_change <= SWITCH_WINDOW_S:
            out.append(Verdict("INFO", "采集进程更换段定义后 %d 秒：%s，期望 %s；过滤器每分钟自检一次，2 分钟后再查" % (
                since_change, text, fetcher), data))
        else:
            out.append(Verdict("FAIL", "实例的段定义版本与采集进程不同：%s，期望 %s → 这些实例的过滤器读取的 segments.json 应与采集进程的相同" % (
                text, fetcher), data))
    same = [i for i, v in versions.items() if v == fetcher]
    if same and not other and fetcher in defined:
        out.append(Verdict("OK", "%d 个实例的段定义版本都是 %s，segment_def 中有这一版" % (len(same), fetcher), data))
    return out


# ==================== helpers ====================
def age_level(age, limit):
    """OK up to limit, WARN up to 3 x limit, FAIL above; FAIL when age is None (nothing there)."""
    if age is None:
        return "FAIL"
    return "OK" if age <= limit else ("WARN" if age <= 3 * limit else "FAIL")


def human(n):
    """Bytes as text: 1.4 GB."""
    n = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024 or unit == "TB":
            return "%.1f %s" % (n, unit) if unit != "B" else "%d B" % n
        n /= 1024
