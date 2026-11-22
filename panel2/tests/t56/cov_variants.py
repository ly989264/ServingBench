"""coverage.py of the kept e2e pack on t56e-ch in four states: all sources, no proxy rows, neither, no Mooncake rows.

After `E2E_KEEP=1 tests/t56/e2e_t56.py ... glm53`: python3 -I tests/t56/cov_variants.py <e2e work>/pack. Changes the kept database.
"""
import contextlib, importlib.util, io, os, subprocess, sys
PACK = sys.argv[1]
CH = "t56e-ch"
def q(sql):
    r = subprocess.run(["docker", "exec", "-i", CH, "clickhouse-client", "--user", "vllm_writer", "--password", "x", "-q", sql],
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
    if r.returncode:
        raise SystemExit("query failed: %s\n%s" % (sql[:200], r.stderr[:400]))
    return r.stdout
os.environ["SEGMENTS_FILE"] = os.path.join(PACK, "segments.json")
spec = importlib.util.spec_from_file_location("cov", os.path.join(PACK, "scripts", "coverage.py"))
cov = importlib.util.module_from_spec(spec); spec.loader.exec_module(cov)
cov.ch = q
def run(name):
    buf = io.StringIO(); sys.argv = ["coverage.py", "200", "100000"]
    with contextlib.redirect_stdout(buf):
        rc = cov.main()
    out = buf.getvalue().splitlines()
    print("== %s (rc=%s)" % (name, rc)); print("\n".join(out[-2:]))
NAT = ["wait_for_save__put__submit_ms", "wait_for_save__put__wait_for_transfer_ms", "wait_for_save__put__finalize_ms"]
run("proxy 开、Mooncake 有")
q("CREATE TABLE vllm_pd.proxy_bak AS vllm_pd.proxy_line")
q("INSERT INTO vllm_pd.proxy_bak SELECT * FROM vllm_pd.proxy_line")
print("proxy_line rows saved:", q("SELECT count() FROM vllm_pd.proxy_bak").strip())
q("TRUNCATE TABLE vllm_pd.proxy_line")
run("proxy 关、Mooncake 有")
q("ALTER TABLE vllm_pd.phase_line UPDATE %s WHERE %s SETTINGS mutations_sync = 2" % (
    ", ".join("%s = NULL" % c for c in NAT), " OR ".join("isNotNull(%s)" % c for c in NAT)))
print("native rows left:", q("SELECT count() FROM vllm_pd.phase_line WHERE %s" % " OR ".join("isNotNull(%s)" % c for c in NAT)).strip())
run("proxy 关、Mooncake 没有")
q("INSERT INTO vllm_pd.proxy_line SELECT * FROM vllm_pd.proxy_bak")
print("proxy_line rows back:", q("SELECT count() FROM vllm_pd.proxy_line").strip())
run("proxy 开、Mooncake 没有")
