#!/usr/bin/env python3
"""T58: the filter's _diag_level() (node/pd_diag_logfilter.py of v3.3-extra) against the patch's own rule, contract 1.5.1
section 8.1: exactly "1", "2" or "3" is that level; unset, empty or anything else (" 2 ", "2x", "0", ...) is 3.
Each value is tried twice: with the patch's pd_diagnostics loaded (the filter must take .level of its (level, source)
pair) and without it (the filter reads PD_DIAG_LEVEL itself). Prints one line per check and "N FAIL" at the end.
Usage: test_diag_level_v33.py <panel pack dir> <the patch's vllm/v1/metrics/pd_diagnostics.py>"""
import importlib.util
import os
import sys

MOD = "vllm.v1.metrics.pd_diagnostics"
FAILS = []


def say(ok, what):
    print("%s  %s" % ("OK  " if ok else "FAIL", what))
    if not ok:
        FAILS.append(what)


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


filt = load("pd_diag_logfilter", os.path.join(sys.argv[1], "node", "pd_diag_logfilter.py"))
patch = load("pd_diagnostics_under_test", sys.argv[2])

VALUES = [None, "", "1", "2", "3", "0", "4", "abc", "2x", " 2 ", " 2", "2 ", "\t1", "3\n", "02", "+2"]

for raw in VALUES:
    want = patch.parse_diag_level(raw).level
    if raw is None:
        os.environ.pop("PD_DIAG_LEVEL", None)
    else:
        os.environ["PD_DIAG_LEVEL"] = raw
    sys.modules.pop(MOD, None)
    got_env = filt._diag_level()
    patch._DIAG_LEVEL = None
    sys.modules[MOD] = patch
    got_mod = filt._diag_level()
    sys.modules.pop(MOD, None)
    say(got_env == want, "PD_DIAG_LEVEL=%r: patch %d, filter without the module %d" % (raw, want, got_env))
    say(got_mod == want, "PD_DIAG_LEVEL=%r: patch %d, filter with the module loaded %d" % (raw, want, got_mod))

# The module's cached value wins over a later change of the environment (the patch reads once per process).
os.environ["PD_DIAG_LEVEL"] = "1"
patch._DIAG_LEVEL = None
sys.modules[MOD] = patch
first = filt._diag_level()
os.environ["PD_DIAG_LEVEL"] = "2"
second = filt._diag_level()
sys.modules.pop(MOD, None)
say(first == 1 and second == 1, "module loaded: the level read first (1) stays after PD_DIAG_LEVEL changes (got %d, %d)" % (first, second))


class Broken:
    @staticmethod
    def diag_level():
        raise RuntimeError("x")


os.environ["PD_DIAG_LEVEL"] = "2"
sys.modules[MOD] = Broken
got = filt._diag_level()
sys.modules.pop(MOD, None)
say(got == 2, "module whose diag_level() raises: falls back to PD_DIAG_LEVEL (2), got %d" % got)

print("%d FAIL" % len(FAILS))
sys.exit(1 if FAILS else 0)
