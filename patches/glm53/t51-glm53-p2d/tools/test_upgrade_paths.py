"""The two site paths to T58, on the simulated site files of tools/simulate_site_edits.py (modes A, B, C).
Standard library only:
`python3 -I test_upgrade_paths.py <replay_p2d.py (T58)> <t56/replay_p2d.py> <simulate_site_edits.py> <original.py> <outdir>`.

For each mode the edited file stands for the site's original (the backup kept when T51 was applied).
- From the original (glm53-1): the T58 script replays the backup directly.
- From T56 (glm53-test): the T56 script's replay of the backup stands for the file on site now. Upgrade as in
  field_check/操作说明A_T58, section A: the site file has the T56 marker and not the T58 one; the T58 script refuses
  it (exit 6, names T56); the T56 script's replay of the backup equals the site file byte for byte (the proof);
  the T58 script's --check on the backup passes; its replay of the backup is the upgraded file.
The upgraded file must equal the direct one byte for byte. <outdir>/direct_<mode>.py and edited_<mode>.py are kept
for the follow-up test run.
"""

import os
import subprocess
import sys

replay58, replay56, simulate, original_path, outdir = sys.argv[1:6]
os.makedirs(outdir, exist_ok=True)
failures = []


def expect(cond, label, detail=""):
    print(("ok   " if cond else "FAIL ") + label)
    if not cond:
        failures.append(label)
        if detail:
            print("     " + detail.replace("\n", "\n     "))


def replay(script, path, out, *extra):
    if os.path.exists(out):
        os.remove(out)
    proc = subprocess.run([sys.executable, "-I", script, path, "--out", out, *extra], capture_output=True, text=True)
    return proc.returncode, proc.stdout + proc.stderr, (open(out).read() if os.path.exists(out) else None)


for mode in ("A", "B", "C"):
    edited = os.path.join(outdir, f"edited_{mode}.py")
    subprocess.run([sys.executable, "-I", simulate, original_path, edited, mode], check=True)
    direct_path, site56_path = os.path.join(outdir, f"direct_{mode}.py"), os.path.join(outdir, f"site56_{mode}.py")
    code, text, direct = replay(replay58, edited, direct_path)
    expect(code == 0 and direct is not None and "changes=29/29" in text, f"{mode} from the original: T58 replay 29/29", text)
    alts = sorted(line.split()[1] + "=" + line.split()[2] for line in text.splitlines()
                  if line.startswith("replay_p2d: C") and " primary " not in line)
    print(f"     {mode}: anchors not primary: {', '.join(alts) or 'none'}")
    code, text, site56 = replay(replay56, edited, site56_path)
    expect(code == 0 and site56 is not None, f"{mode} from T56: the T56 replay of the backup (the file on site now)", text)
    if site56 is None or direct is None:
        continue
    expect("kv_load_envelope_ns" in site56 and "_pd_diag_level" not in site56,
           f"{mode} step 1: the site file has the T56 marker and not the T58 one")
    code, text, out = replay(replay58, site56_path, os.path.join(outdir, f"refused_{mode}.py"))
    expect(code == 6 and out is None and "T56 p2d diagnostics" in text and "restore the original" in text,
           f"{mode} step 1: the T58 script refuses the T56 file (exit 6, names T56)", text)
    code, text, proof = replay(replay56, edited, os.path.join(outdir, f"proof_{mode}.py"))
    expect(code == 0 and proof == site56, f"{mode} step 2: the T56 script's replay of the backup == the site file", text)
    code, text, out = replay(replay58, edited, os.path.join(outdir, f"check_{mode}.py"), "--check")
    expect(code == 0 and out is None and "check ok: 29 of 29" in text, f"{mode} step 4: --check on the backup, nothing written", text)
    code, text, upgraded = replay(replay58, edited, os.path.join(outdir, f"upgraded_{mode}.py"))
    expect(code == 0 and upgraded == direct, f"{mode} step 5: the upgraded file == the direct T58 replay, byte for byte", text)

print(f"{len(failures)} failed")
sys.exit(1 if failures else 0)
