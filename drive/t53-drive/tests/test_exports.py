#!/usr/bin/env python3
"""Where the two export lines go (c_probe.py exports) and how c_act.py exports inserts them, on crafted start
scripts: python3 -I tests/test_exports.py"""
import os
import subprocess
import sys
import tempfile

LIB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "lib")
T = tempfile.mkdtemp()
E1 = "export VLLM_LOGGING_CONFIG_PATH=/data/pd-diag/conf/pd_diag_logging.json"
E2 = "export PYTHONPATH=/data/pd-diag/conf${PYTHONPATH:+:$PYTHONPATH}"
bad = 0


def probe(text):
    p = os.path.join(T, "s%d.sh" % abs(hash(text)))
    open(p, "w").write(text)
    r = subprocess.run([sys.executable, "-I", "-S", "-B", os.path.join(LIB, "c_probe.py"), "exports", p], stdout=subprocess.PIPE)
    return p, dict(l.split("=", 1) for l in r.stdout.decode().splitlines() if "=" in l)


def case(label, text, want):
    global bad
    _, k = probe(text)
    got = {x: k.get(x) for x in want}
    good = got == want
    bad += not good
    print("%s %-44s %s" % ("PASS" if good else "FAIL", label, " ".join("%s=%s" % kv for kv in sorted(k.items()) if kv[0] != "script_sha16")))


case("plain: before the launch line", "#!/bin/bash\ncd /launch\npython3 launch_online_dp.py --x\n", {"exports": "absent", "insert_before": "3"})
case("comment mentioning vllm serve is skipped", "#!/bin/bash\n# vllm serve is started below\nbash run_dp_template.sh 0\n", {"insert_before": "3"})
case("two launch lines: before the first", "#!/bin/bash\nvllm serve /m --port 1\nvllm serve /m --port 2\n", {"insert_before": "2"})
case("no launch line", "#!/bin/bash\necho hi\n", {"exports": "bad no_launch_line"})
case("already there", "#!/bin/bash\n%s\n%s\npython3 launch_online_dp.py\n" % (E1, E2), {"exports": "ok", "at_line": "2"})
case("only one of the two", "#!/bin/bash\n%s\npython3 launch_online_dp.py\n" % E1, {"exports": "bad different_or_after_launch lines=2"})
case("after the launch line", "#!/bin/bash\npython3 launch_online_dp.py\n%s\n%s\n" % (E1, E2), {"exports": "bad different_or_after_launch lines=3,4"})
case("other value", "#!/bin/bash\nexport VLLM_LOGGING_CONFIG_PATH=/x.json\n%s\nvllm serve m\n" % E2, {"exports": "bad different_or_after_launch lines=2,3"})
print("RESULT: %s" % ("OK" if not bad else "FAIL %d" % bad))
sys.exit(1 if bad else 0)
