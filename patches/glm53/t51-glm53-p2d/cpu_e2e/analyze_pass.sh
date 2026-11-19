#!/usr/bin/env bash
# Run every check on one pass: [AFTER=<patched tree, default vllm030cpu_p>] bash analyze_pass.sh <results/PASS dir> <glm53 segments.json>
# Writes <dir>/analysis/<case>.{compare,records,segments[,site]}.json and prints a one-line summary per case.
set -uo pipefail
R=$1; SEG=$2; E="$(cd "$(dirname "$0")" && pwd)"; AFTER=${AFTER:-vllm030cpu_p}  # the patched tree
mkdir -p "$R/analysis"
for c in $(ls "$R/$AFTER"); do
  a=$R/analysis/$c
  python3 -I -B "$E/compare_cases.py" "$R/vllm030cpu/$c/client.json" "$R/$AFTER/$c/client.json" > "$a.compare.json"; cmp_rc=$?
  python3 -I -B "$E/check_records.py" "$R/$AFTER/$c" > "$a.records.json"
  python3 -I -B "$E/check_records.py" "$R/vllm030cpu/$c" > "$a.records_before.json"
  if [ -d "$R/$AFTER/$c/filter/P" ]; then  # P/D: prefill side from P's filter output, decode side from D's
    python3 -I -B "$E/segments_with_values.py" "$SEG" "$R/$AFTER/$c" P prefill > "$a.segments_P.json"
    python3 -I -B "$E/segments_with_values.py" "$SEG" "$R/$AFTER/$c" D decode > "$a.segments_D.json"
    python3 -I -B -c "import json,sys; p,d=(json.load(open(f)) for f in sys.argv[1:3]); p['segments']+=d['segments']; p['instance']='P+D'; p['side']=None; json.dump(p,sys.stdout,ensure_ascii=False,indent=1)" "$a.segments_P.json" "$a.segments_D.json" > "$a.segments.json"
  else
    python3 -I -B "$E/segments_with_values.py" "$SEG" "$R/$AFTER/$c" > "$a.segments.json"
  fi
  site=""
  if [ "$c" = site ] || [ "$c" = sitev ]; then
    python3 -I -B "$E/check_site_patch.py" "$R/vllm030cpu/$c" > "$a.site_before.json"; b=$?
    python3 -I -B "$E/check_site_patch.py" "$R/$AFTER/$c" > "$a.site.json"; p=$?
    site=" site_wrappers before=$([ $b = 0 ] && echo ok || echo FAIL) after=$([ $p = 0 ] && echo ok || echo FAIL)"
  fi
  python3 -I -B - "$a" "$cmp_rc" "$site" <<'PY'
import json, sys
a, cmp_rc, site = sys.argv[1], int(sys.argv[2]), sys.argv[3]
rec = json.load(open(a + ".records.json"))
before = json.load(open(a + ".records_before.json"))
seg = json.load(open(a + ".segments.json"))
parts = []
for name, s in rec["servers"].items():
    ch = s["checks"]
    parts.append(f"{name}: records={s['records']} common={'ok' if ch['common']['ok'] else 'FAIL'} "
                 f"chain={'ok' if ch['chain']['ok'] else 'FAIL'}({ch['chain']['engine_steps']} steps) "
                 f"queue={'ok' if ch['queue']['ok'] else 'FAIL'}")
nb = sum(s["records"] for s in before["servers"].values())
with_values = sum(1 for x in seg["segments"] if x["status"] == "有值")
print(f"{a.rsplit('/', 1)[1]}: client before/after {'same' if cmp_rc == 0 else 'DIFFERENT'}; records before patch {nb}; "
      + "; ".join(parts) + f"; segments with values {with_values}/{len(seg['segments'])}" + site)
PY
done
