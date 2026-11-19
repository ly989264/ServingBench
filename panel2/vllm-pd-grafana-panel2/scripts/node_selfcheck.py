#!/usr/bin/env python3
"""Self-check of the node files, run on a P or D machine (bin/nodes.sh selfcheck sends it there with the sample).

  VLLM_LOGGING_CONFIG_PATH=<conf>/pd_diag_logging.json PYTHONPATH=<conf> PD_DIAG_INSTANCE=selfcheck \
      python3 node_selfcheck.py --sample <full stream .log.gz> --truth <truth.jsonl> --log-dir <NODE_LOG_DIR> [--uid N] [--docker-image IMG --conf-in-container DIR]

In a Python process set up the way vLLM sets itself up:
  1. the logging config named by VLLM_LOGGING_CONFIG_PATH loads, and pd_diag_logfilter comes from PYTHONPATH;
  2. the sample (full record stream of one P and one D instance) goes through the filter, into a temporary folder:
     every request-level record is written once, nothing else is, the per-request sums equal the sample's truth,
     the self-check line reports 0 errors, and each role (api, engine, worker, and pool when the sample has cache-pool
     records the filter keeps) has its own file;
  3. a copy of segments.json is changed on disk and the filter picks it up without a restart;
  4. the real log folder exists and this user can write to it; with --uid, the given container user can read the
     three files and write the log folder;
  5. with --docker-image, the same checks of 4 run inside a throw-away container of that image (docker run --rm).
Writes nothing outside a temporary folder, except a test file in the log folder that it removes at once.
One line per item, RESULT: OK or RESULT: FAIL at the end. Python 3.9 or later.
"""
import argparse
import gzip
import json
import logging
import logging.config
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import time

problems = []


def ok(msg):
    print("OK   " + msg)


def bad(msg):
    problems.append(msg)
    print("FAIL " + msg)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sample", required=True)
    ap.add_argument("--truth", required=True)
    ap.add_argument("--log-dir", required=True)
    ap.add_argument("--uid", type=int)
    ap.add_argument("--docker-image")
    ap.add_argument("--conf-in-container", default="/opt/vllm-pd-diag")
    ap.add_argument("--log-in-container", default="")
    a = ap.parse_args()

    if sys.version_info < (3, 9):
        bad("Python %s：过滤模块要 3.9 及以上" % sys.version.split()[0])
        return finish()
    cfg_path = os.environ.get("VLLM_LOGGING_CONFIG_PATH", "")
    conf = os.environ.get("PYTHONPATH", "").split(os.pathsep)[0]
    if not cfg_path or not os.path.isfile(cfg_path):
        bad("VLLM_LOGGING_CONFIG_PATH 没有指向一个文件：%r" % cfg_path)
        return finish()
    try:
        import pd_diag_logfilter as F
    except Exception as exc:  # noqa: BLE001
        bad("PYTHONPATH=%s 里 import pd_diag_logfilter 失败：%s" % (conf, exc))
        return finish()
    if os.path.dirname(os.path.abspath(F.__file__)) != os.path.abspath(conf):
        bad("pd_diag_logfilter 来自 %s，不是 PYTHONPATH 的 %s" % (F.__file__, conf))
    else:
        ok("配置 %s，模块 %s，Python %s" % (cfg_path, F.__file__, sys.version.split()[0]))
    mapping = os.path.join(conf, "segments.json")
    try:
        seg = json.load(open(mapping, encoding="utf-8"))
        assert seg.get("version") == 2
    except Exception as exc:  # noqa: BLE001
        bad("%s 读不了或不是 version 2：%s" % (mapping, exc))
        return finish()

    tmp = tempfile.mkdtemp(prefix="pd-diag-selfcheck-")
    try:
        run_sample(a, F, cfg_path, mapping, seg, tmp)
    finally:
        logging.shutdown()
        shutil.rmtree(tmp, ignore_errors=True)
    check_dirs(a, conf)
    if a.docker_image:
        check_docker(a, conf)
    return finish()


def run_sample(a, F, cfg_path, mapping, seg, tmp):
    m2 = os.path.join(tmp, "segments.json")
    shutil.copy(mapping, m2)
    os.environ["PD_DIAG_MAPPING"] = m2
    os.environ["PD_DIAG_LOG_DIR"] = os.path.join(tmp, "out")
    os.environ["PD_DIAG_INSTANCE"] = "selfcheck"
    cfg = json.load(open(cfg_path, encoding="utf-8"))
    cfg.get("handlers", {}).get("vllm", {})["stream"] = open(os.devnull, "w")
    logging.config.dictConfig(cfg)
    handler = next((h for h in logging.getLogger("vllm").handlers if isinstance(h, F.PdDiagHandler)), None)
    if handler is None:
        bad("logging 配置里没有 pd_diag_logfilter.PdDiagHandler")
        return
    log = logging.getLogger("vllm.v1.metrics.pd_diagnostics")
    current = {"name": "x"}
    base = logging.getLogRecordFactory()

    def make(*x, **k):
        r = base(*x, **k)
        r.vllm_process_name = current["name"]
        return r
    logging.setLogRecordFactory(make)

    # Which records the filter must write: its own rules (an accumulate rule wins; then a passthrough rule), TP rank 0.
    rules = F._Config(mapping)
    want = 0
    want_pool = False
    n = 0
    t0 = time.time()
    with gzip.open(a.sample, "rt", encoding="utf-8") as f:
        for line in f:
            i = line.find("[PD_DIAG] ")
            if i < 0:
                continue
            current["name"] = line[1:line.index(" pid=")]
            payload = line[i + 10:].rstrip("\n")
            r = json.loads(payload)
            ev = r["event"]
            acc = any(F._where_ok(r, spec[3]) for spec in rules.acc.get(ev, ()))
            if not acc and r.get("tp_rank") in (None, 0) and any(F._where_ok(r, w) for w in rules.pass_rules.get(ev, ())):
                want += 1
                want_pool = want_pool or ev.startswith(POOL_EVENT)
            log.info("[PD_DIAG] %s", payload)
            n += 1
    with handler._mu:
        handler._flush_partials(force_all=True, reason="selfcheck")
        handler._selfcheck()
        for fh in handler._files.values():
            fh.fh.flush()
    took = time.time() - t0
    out = os.path.join(tmp, "out", "selfcheck")
    files = sorted(os.listdir(out)) if os.path.isdir(out) else []
    roles = sorted({x.split(".")[0] for x in files})
    got = 0
    sums = {}
    checks = []
    for name in files:
        for line in open(os.path.join(out, name), encoding="utf-8"):
            r = json.loads(line[line.index("[PD_DIAG] ") + 10:])
            ev = r["event"]
            if ev == "pd_seg_partial":
                # The engine and the worker sum different segments of the same request; within one, the last line is the latest.
                sums.setdefault(r["request_id"], {}).update(r["segs"])
            elif ev == "pd_filter_selfcheck":
                checks.append(r)
            elif ev != "pd_file_open":
                got += 1
    truth = {}
    for line in open(a.truth, encoding="utf-8"):
        t = json.loads(line)
        if t["segs"]:       # a request with no per-step sums (it ended before its first step) has nothing to compare
            truth[t["request_id"]] = t["segs"]
    # Only the sums the truth has: a segment definition of v3.1-extra (profiles/) adds sums of its own (the candidate
    # step, wait for put) that the sample's truth does not know; the end-to-end tests check those.
    # C15 (profiles/glm53): a rule that reads the device time only (a list of paths, all *.device_duration_ns) must stay
    # empty on the sample, whose records have the host time only: a value there would be a silent fallback to the host
    # time, which C15 rules out. Those sums are checked for being empty instead of against the truth (host time).
    device_only = sorted(x["id"] for x in json.load(open(mapping, encoding="utf-8")).get("accumulate", [])
                         if isinstance(x.get("value"), list) and x["value"]
                         and all(isinstance(v, str) and v.endswith(".device_duration_ns") for v in x["value"]))

    def cmp(d):
        return {k: v for k, v in d.items() if k not in device_only}
    diff = sum(1 for rid, segs in truth.items()
               if cmp({k: v for k, v in (sums.get(rid) or {}).items() if k in segs}) != cmp(segs))
    errors = max((c.get("errors", 0) for c in checks), default=None)
    line = "样本 %d 行，用时 %.1f 秒：请求级记录写出 %d / 应有 %d，累计 %d 个请求，与真值不同 %d 个，自检行出错数 %s，文件 %s" % (
        n, took, got, want, len(sums), diff, errors, " ".join(roles))
    if got == want and diff == 0 and errors == 0 and roles == expected_roles(want_pool):
        ok(line)
    else:
        bad(line)
    if device_only:
        with_value = [rid for rid, segs in sums.items() if any(segs.get(k) for k in device_only)]
        dline = "只取设备时间的 %s：样本里只有 host 时间，应为空，%d 个请求%s" % (
            "、".join(device_only), len(sums), "全为空" if not with_value else "里 %d 个有值（%s…）" % (len(with_value), with_value[0]))
        if with_value:
            bad(dline)
        else:
            ok(dline)
    # Hot reload of the mapping.
    before = handler._cfg.version
    raw = json.load(open(m2, encoding="utf-8"))
    raw["config_id"] = str(raw.get("config_id")) + "-selfcheck"
    with open(m2 + ".tmp", "w", encoding="utf-8") as f:
        json.dump(raw, f)
    os.replace(m2 + ".tmp", m2)
    os.utime(m2, ns=(time.time_ns(), time.time_ns() + 10_000_000_000))
    with handler._mu:
        handler._next_stat = 0
        handler._tick()
    after = handler._cfg.version
    if after != before:
        ok("改映射文件后不重启即生效：%s → %s" % (before, after))
    else:
        bad("改了映射文件，过滤器没有重读（%s）" % before)


POOL_EVENT = "pool_task"          # cache-pool records of patch 02; the filter writes them to a file of role pool


def expected_roles(want_pool):
    """The role files the sample must give: api, engine and worker always; pool when the filter keeps cache-pool
    records of the sample (segments.json of v3 reads them)."""
    return sorted(("api", "engine", "worker") + (("pool",) if want_pool else ()))


def can(uid, path, want_write):
    st = os.stat(path)
    mode = st.st_mode
    if uid == 0:
        return True
    if st.st_uid == uid:
        bits = (stat.S_IRUSR | (stat.S_IWUSR if want_write else 0))
    else:
        bits = (stat.S_IROTH | (stat.S_IWOTH if want_write else 0))
    if os.path.isdir(path):
        bits |= stat.S_IXUSR if st.st_uid == uid else stat.S_IXOTH
    return (mode & bits) == bits


def check_dirs(a, conf):
    d = a.log_dir
    try:
        os.makedirs(d, exist_ok=True)
        probe = os.path.join(d, ".pd-diag-write-test-%d" % os.getpid())
        with open(probe, "w") as f:
            f.write("x")
        os.unlink(probe)
        ok("日志目录 %s 能写（用户 %d）" % (d, os.getuid()))
    except OSError as exc:
        bad("日志目录 %s 写不了：%s" % (d, exc))
        return
    if a.uid is not None:
        files = [os.path.join(conf, x) for x in ("pd_diag_logging.json", "pd_diag_logfilter.py", "segments.json")]
        unread = [x for x in files + [conf] if not can(a.uid, x, False)]
        if unread:
            bad("容器用户 uid %d 读不了：%s（chmod 0644 文件、0755 目录）" % (a.uid, " ".join(unread)))
        else:
            ok("容器用户 uid %d 能读三个文件" % a.uid)
        if can(a.uid, d, True):
            ok("容器用户 uid %d 能写日志目录 %s" % (a.uid, d))
        else:
            bad("容器用户 uid %d 写不了日志目录 %s（属主 uid %d，权限 %o）" % (a.uid, d, os.stat(d).st_uid, os.stat(d).st_mode & 0o777))


def check_docker(a, conf):
    user = ["--user", str(a.uid)] if a.uid is not None else []
    log_in = a.log_in_container or seg_log_dir(conf)
    cmd = ["docker", "run", "--rm", "--network", "none"] + user + [
        "-v", "%s:%s:ro" % (conf, a.conf_in_container), "-v", "%s:%s" % (a.log_dir, log_in),
        "-e", "PYTHONPATH=" + a.conf_in_container, "--entrypoint", "python3", a.docker_image, "-c",
        "import json, os, sys, pd_diag_logfilter as F; json.load(open('%s/pd_diag_logging.json')); "
        "p = os.path.join('%s', '.pd-diag-write-test'); open(p, 'w').write('x'); os.unlink(p); "
        "print('container ok', sys.version.split()[0], F.__file__)" % (a.conf_in_container, log_in)]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    except Exception as exc:  # noqa: BLE001
        bad("docker run 失败：%s" % exc)
        return
    if out.returncode == 0 and "container ok" in out.stdout:
        ok("镜像 %s 里：挂进去的三个文件能读、模块能 import、日志目录 %s 能写（%s）" % (a.docker_image, log_in, out.stdout.strip()))
    else:
        bad("镜像 %s 里检查失败：%s" % (a.docker_image, (out.stderr or out.stdout).strip()[-300:]))


def seg_log_dir(conf):
    try:
        return json.load(open(os.path.join(conf, "segments.json"), encoding="utf-8"))["line"]["log_dir"]
    except Exception:  # noqa: BLE001
        return "/var/log/pd-diag"


def finish():
    print("RESULT: %s" % ("OK" if not problems else "FAIL（%d 项）" % len(problems)))
    return 0 if not problems else 1


if __name__ == "__main__":
    sys.exit(main())
