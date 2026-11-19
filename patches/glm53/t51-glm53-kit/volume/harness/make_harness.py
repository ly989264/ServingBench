"""Build t51_volume.py from the vllm-ascend joint check (t51-glm53-ascend joint/t51_joint_check.py): its setup and
parts 1-2 unchanged (lines up to part 3's run), plus run_chain's helpers re-used by a scenario that schedules n
requests per step. The joint check file itself is not modified."""
import sys
src_path, out_path = sys.argv[1], sys.argv[2]
lines = open(src_path, encoding="utf-8").read().split("\n")
cut = next(i for i, l in enumerate(lines) if l.startswith("try:") and "run_chain" in lines[i + 1])
head = "\n".join(lines[:cut])
start = next(i for i, l in enumerate(lines) if l.startswith("def run_chain():"))
end = next(i for i in range(start, len(lines)) if lines[i].startswith("    return p_records, d_records"))
body = lines[start:end + 1]
k = next(i for i, l in enumerate(body) if l.startswith("    runner = Runner()"))
helpers = body[:k]
helpers[0] = "def run_volume_chain(n, d_save, decode_steps=2):"
scenario = open(sys.argv[3], encoding="utf-8").read().rstrip("\n").split("\n")
driver = open(sys.argv[4], encoding="utf-8").read()
open(out_path, "w", encoding="utf-8").write(head + "\n\n# ==== volume measurement (T51 glm5.3 guide A) ====\n" + "\n".join(helpers + scenario) + "\n\n" + driver)
print("cut at line", cut + 1, "run_chain", start + 1, end + 1, "helpers", len(helpers))
