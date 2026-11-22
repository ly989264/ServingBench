"""T56 startup output vs T58 startup output for one scenario: the T58 lines are the T56 lines plus one
'PD_DIAG_STARTUP diag ...' line right after the scheduler line (EngineCore) or the model_runner line (Worker).
usage: python3 startup_cmp.py T56.txt T58.txt "level=<n> source=<s>"   (prints one result line)"""
import sys

A, B, WANT = sys.argv[1], sys.argv[2], "PD_DIAG_STARTUP diag " + sys.argv[3]


def lines(path, prefix):
    return [l.rstrip("\n")[len(prefix):] for l in open(path) if l.startswith(prefix)]


a, b = lines(A, "LINE "), lines(B, "LINE ")
diag = [i for i, l in enumerate(b) if l.startswith("PD_DIAG_STARTUP diag ")]
rest = [l for l in b if not l.startswith("PD_DIAG_STARTUP diag ")]
problems = []
if rest != a:
    problems.append(f"other lines differ: T56 {a} T58 {rest}")
if len(diag) != 1:
    problems.append(f"{len(diag)} diag lines")
else:
    i = diag[0]
    if b[i] != WANT:
        problems.append(f"diag line {b[i]!r} != {WANT!r}")
    prev = b[i - 1] if i > 0 else ""
    if not (prev.startswith("PD_DIAG_STARTUP scheduler=") or prev.startswith("PD_DIAG_STARTUP worker model_runner=")):
        problems.append(f"diag line after {prev!r}")
for p in ("built", "timing", "rc="):
    if lines(A, p) != lines(B, p):
        problems.append(f"{p} differs")
name = B.rsplit("/", 1)[-1]
print(f"  {'OK  ' if not problems else 'DIFF'} {name}: {len(a)} T56 lines, {len(b)} T58 lines"
      + ("" if not problems else " | " + "; ".join(problems)))
