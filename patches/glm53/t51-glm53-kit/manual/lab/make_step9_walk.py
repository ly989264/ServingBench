"""Write the step 9 walk script from the manual's own step 9 blocks (the grep check and the two export lines), for a
simulated start script that starts vLLM through a launcher (no literal "vllm serve" line).
    python3 -I make_step9_walk.py <manual.md> <out.sh>"""
import re, sys
t = open(sys.argv[1], encoding="utf-8").read()
s9 = t[t.index("#### 第 9 步"):t.index("### 四、")]
blocks = re.findall(r"^```([a-z]*)\n(.*?)^```\n", s9, re.S | re.M)
grep_cmd = next(b for lang, b in blocks if lang == "bash" and b.startswith("grep -n")).strip()
exports = next(b for lang, b in blocks if b.startswith("export VLLM_LOGGING_CONFIG_PATH"))
g = lambda f: grep_cmd.replace("<启动脚本>", f)
open(sys.argv[2], "w").write("""set -u
S=/tmp/start_d0.sh
printf '#!/bin/bash\\nset -e\\nexport HCCL_X=1\\npython3 /opt/launch_online_dp.py --dp-size 16 --template /opt/run_dp_template.sh\\n' > $S
echo "### before (no lines)"; %s; echo "grep rc=$?"
# add the two lines before the first command that starts vLLM or the launcher
python3 - "$S" <<'PY'
import sys
p = sys.argv[1]; ls = open(p).read().split("\\n")
i = next(n for n, l in enumerate(ls) if "launch_online_dp" in l)
ls[i:i] = %r.rstrip("\\n").split("\\n")
open(p, "w").write("\\n".join(ls))
PY
echo "### after"; %s; echo "grep rc=$?"; echo "### the script"; cat -n $S
echo "### a child started by the launcher line inherits both variables"; (set +u; . <(sed -n '1,/launch_online_dp/{/launch_online_dp/d;p}' $S); env | grep -E '^(VLLM_LOGGING_CONFIG_PATH|PYTHONPATH)=' | sort)
echo "### a script left by test with the same two lines: grep lists them, nothing to add"; cp $S /tmp/s2.sh; %s
echo "### a script with another path"; sed 's#/data/pd-diag/conf/pd_diag_logging.json#/data/other/pd_diag_logging.json#' $S > /tmp/s3.sh; %s
echo "### a script left with the PYTHONPATH line quoted: the second pattern still matches"; printf '#!/bin/bash\nexport VLLM_LOGGING_CONFIG_PATH="/data/pd-diag/conf/pd_diag_logging.json"\nexport PYTHONPATH="/data/pd-diag/conf:$PYTHONPATH"\npython3 /opt/launch_online_dp.py\n' > /tmp/s4.sh; %s
""" % (g("$S"), exports, g("$S"), g("/tmp/s2.sh"), g("/tmp/s3.sh"), g("/tmp/s4.sh")))
print("ok")
