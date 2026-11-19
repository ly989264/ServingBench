#!/usr/bin/env python3
"""T51 (pack two v3.1-extra): the update script's rollback stops when the v3.1-extra puller does not stop.

  python3 -I tests/t51/test_update_rollback.py

The rollback is run against a throw-away folder with the steps that touch the machine replaced; only the puller check
(pgrep) runs for real, against a stand-in process whose command line names <panel 2 folder>/scripts/puller.py.
  1. bin/puller.sh stop fails: Stop, nothing deleted or restored.
  2. bin/puller.sh stop succeeds but a puller of this cluster is still running: Stop, nothing deleted or restored.
  3. The puller is gone: the rollback goes on (deletes the extra mirror folders, then restores).
Prints OK/FAIL per check; exit 1 on any FAIL.
"""
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, "..", "..", "update", "vllm_pd_update_pack2_v31extra.py")
FAILS = []


def check(name, ok, detail=""):
    print(("OK   " if ok else "FAIL ") + name + ("  -- " + detail if detail and not ok else ""))
    if not ok:
        FAILS.append(name)


class Restored(Exception):
    pass


def load():
    spec = importlib.util.spec_from_file_location("upd", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def case(mod, work, stop_code):
    """Runs rollback once; returns (exception, calls made, extra mirror folders still there)."""
    pull = os.path.join(work, "pull")
    for d in ("_proxy", "_mooncake"):
        os.makedirs(os.path.join(pull, d), exist_ok=True)
    bdir = os.path.join(work, "backup")
    os.makedirs(bdir, exist_ok=True)
    json.dump({"cluster": mod.CLUSTER, "pack": "包二 " + mod.PACK_VERSION, "made": "test", "version_before": "v3",
               "folders": {"panel2": {"tar": "panel2.tar", "path": mod.NEW2}}}, open(os.path.join(bdir, "manifest.json"), "w"))
    calls = []

    def sh(cmd, cwd=None, **kw):
        calls.append(cmd)
        return (stop_code, "") if cmd.startswith("bin/puller.sh stop") else (0, "")

    def restore_folder(*a):
        calls.append("restore_folder")
        raise Restored()

    mod.sh = sh
    mod.restore_folder = restore_folder
    mod.drop_data = lambda *a: calls.append("drop_data") or ""
    mod.filter_shas = lambda d: {}
    env = {"NODE_MODE": "vllm", "PULL_DIR": pull, "TIMING_HOME": os.path.join(work, "home")}
    try:
        mod.rollback(env, bdir)
        exc = None
    except Exception as e:      # noqa: BLE001 - the test looks at which one
        exc = e
    left = [d for d in ("_proxy", "_mooncake") if os.path.isdir(os.path.join(pull, d))]
    return exc, calls, left


def main():
    mod = load()
    with tempfile.TemporaryDirectory() as work:
        mod.NEW2 = os.path.join(work, "vllm-pd-grafana-panel2", "test")
        os.makedirs(os.path.join(mod.NEW2, "scripts"))
        fake = os.path.join(mod.NEW2, "scripts", "puller.py")
        open(fake, "w").write("import time\ntime.sleep(60)\n")

        exc, calls, left = case(mod, work, stop_code=1)
        check("stop fails: the rollback stops with Stop", isinstance(exc, mod.Stop), repr(exc))
        check("stop fails: nothing deleted or restored",
              left == ["_proxy", "_mooncake"] and "restore_folder" not in calls and "drop_data" not in calls, str(calls))

        proc = subprocess.Popen([sys.executable, fake])
        try:
            time.sleep(0.5)
            found = mod.puller_left()
            check("a running puller of this cluster is found by its command line", str(proc.pid) in found, str(found))
            exc, calls, left = case(mod, work, stop_code=0)
            check("stop returns 0 but the puller still runs: the rollback stops with Stop",
                  isinstance(exc, mod.Stop) and str(proc.pid) in str(exc), repr(exc))
            check("puller still runs: nothing deleted or restored",
                  left == ["_proxy", "_mooncake"] and "restore_folder" not in calls and "drop_data" not in calls, str(calls))
        finally:
            proc.kill()
            proc.wait()

        check("after the puller has stopped none is found", mod.puller_left() == [], str(mod.puller_left()))
        exc, calls, left = case(mod, work, stop_code=0)
        check("puller stopped: the rollback goes on to delete the extra folders and restore",
              isinstance(exc, Restored) and left == [], repr(exc) + str(left))
    print("%d FAIL" % len(FAILS))
    sys.exit(1 if FAILS else 0)


if __name__ == "__main__":
    main()
