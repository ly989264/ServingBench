"""t53drive container actions (they write). Run inside the vLLM container as root:
    python3 -I -S -B - <action> [args] < c_act.py
Standard library only, no child processes. Every output line is "key=value" or "S <text>"; the last line is
"act=ok" or "act=fail <reason>".

  backup <kit patches> <backup dir> <start script> [<libmooncake_store.so>]
        stages ②: the 23 vLLM files, the 9 vllm-ascend files and mooncake_connector.py, the start script, the
        Mooncake library; SHA256SUMS over all of them. An existing backup that checks out is kept, never overwritten.
  verify <backup dir>   the backup's SHA256SUMS
  apply <kit patches> <p2d result> <expected p2d sha256>     stage ④: the T58 files and the p2d result
  exports <start script> <line> <anchor sha8>               stage ④: the two exports before that line
  mcswap <new lib> <libmooncake_store.so> <expected sha256>  stage ④: rename into place, mode and owner kept
  pxswap <proxy file> <new file> <original sha256> <new sha256>
  restore <kit patches> <backup dir> <start script> [<libmooncake_store.so>]   rollback
  pxrestore <proxy file> <backup copy> <original sha256>
"""
import hashlib
import io
import json
import os
import shutil
import sys
import sysconfig
import tarfile

LIB = sysconfig.get_paths()["purelib"]
CONN = "vllm_ascend/distributed/kv_transfer/kv_p2p/mooncake_connector.py"
EXPORTS = ("export VLLM_LOGGING_CONFIG_PATH=/data/pd-diag/conf/pd_diag_logging.json\n",
           "export PYTHONPATH=/data/pd-diag/conf${PYTHONPATH:+:$PYTHONPATH}\n")


def sha(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def fail(msg):
    print("act=fail " + msg)
    sys.exit(1)


def names(listfile):
    return [l.split(None, 1)[1].strip().lstrip("*") for l in open(listfile) if l.strip()]


def check_sums(root, listfile):
    bad = []
    for line in open(listfile):
        if not line.strip():
            continue
        h, n = line.split(None, 1)
        n = n.strip().lstrip("*")
        p = os.path.join(root, n)
        if not os.path.isfile(p) or sha(p) != h:
            bad.append(n)
    return bad


def write_sums(bk):
    files = sorted(f for f in os.listdir(bk) if f != "SHA256SUMS" and os.path.isfile(os.path.join(bk, f)))
    with open(os.path.join(bk, "SHA256SUMS"), "w") as f:
        for n in files:
            f.write("%s  %s\n" % (sha(os.path.join(bk, n)), n))


def verify(bk):
    p = os.path.join(bk, "SHA256SUMS")
    if not os.path.isfile(p):
        return ["SHA256SUMS missing"]
    return check_sums(bk, p)


def make_tar(path, root, members):
    with tarfile.open(path, "w", format=tarfile.GNU_FORMAT) as t:
        for n in members:
            t.add(os.path.join(root, n), arcname=n, recursive=False)


def a_backup(K, bk, start, mclib=None):
    if os.path.isfile(os.path.join(bk, "SHA256SUMS")):
        bad = verify(bk)
        if bad:
            fail("existing backup does not check out: " + ",".join(bad[:3]))
        meta = json.load(open(os.path.join(bk, "meta.json")))
        if meta.get("start") != start or (mclib and meta.get("mclib") != mclib):
            fail("existing backup is for another start script or library")
        print("backup=kept")
    else:
        os.makedirs(bk, exist_ok=True)
        v = names(K + "/vllm/SHA256SUMS.before")
        a = names(K + "/vllm-ascend/SHA256SUMS.before") + [CONN]
        make_tar(os.path.join(bk, "vllm.tar"), LIB, v)
        make_tar(os.path.join(bk, "vllm-ascend.tar"), LIB, a)
        shutil.copyfile(start, os.path.join(bk, "start-script"))
        st = os.stat(start)
        meta = {"start": start, "start_mode": st.st_mode & 0o7777, "start_uid": st.st_uid, "start_gid": st.st_gid,
                "start_sha256": sha(start), "purelib": LIB}
        if mclib:
            shutil.copy2(mclib, os.path.join(bk, "libmooncake_store.so"))
            ms = os.stat(mclib)
            meta.update(mclib=mclib, mc_mode=ms.st_mode & 0o7777, mc_uid=ms.st_uid, mc_gid=ms.st_gid, mc_sha256=sha(mclib))
        json.dump(meta, open(os.path.join(bk, "meta.json"), "w"), indent=1, sort_keys=True)
        write_sums(bk)
        print("backup=made")
    with tarfile.open(os.path.join(bk, "vllm.tar")) as t:
        nv = len(t.getnames())
    with tarfile.open(os.path.join(bk, "vllm-ascend.tar")) as t:
        na = len(t.getnames())
    print("bk_vllm=%d" % nv)
    print("bk_ascend=%d" % na)
    print("bk_sums=%s" % sha(os.path.join(bk, "SHA256SUMS"))[:16])
    print("act=ok")


def copy_tree(src, dst):
    n = 0
    for root, _, files in os.walk(src):
        for f in files:
            s = os.path.join(root, f)
            d = os.path.join(dst, os.path.relpath(s, src))
            os.makedirs(os.path.dirname(d), exist_ok=True)
            shutil.copy(s, d)
            n += 1
    return n


def a_apply(K, result, want):
    if sha(result) != want:
        fail("p2d result is not the one of stage ②")
    nv = copy_tree(K + "/vllm/files/vllm", os.path.join(LIB, "vllm"))
    na = copy_tree(K + "/vllm-ascend/files/vllm_ascend", os.path.join(LIB, "vllm_ascend"))
    bad = check_sums(LIB, K + "/vllm/SHA256SUMS.after") + check_sums(LIB, K + "/vllm-ascend/SHA256SUMS.after")
    if bad:
        fail("after sums differ: " + ",".join(bad[:3]))
    shutil.copyfile(result, os.path.join(LIB, CONN))
    if sha(os.path.join(LIB, CONN)) != want:
        fail("p2d result not in place")
    print("copied=%d/%d" % (nv, na))
    print("connector_sha16=%s" % want[:16])
    print("act=ok")


def a_exports(path, line, anchor):
    line = int(line)
    with open(path, encoding="utf-8") as f:
        lines = f.read().split("\n")
    present = [l.strip() for l in lines if "VLLM_LOGGING_CONFIG_PATH" in l or "pd-diag/conf" in l]
    if present == [e.strip() for e in EXPORTS]:
        print("exports=already")
        print("act=ok")
        return
    if present:
        fail("the start script has other export lines")
    if hashlib.sha256(lines[line - 1].encode()).hexdigest()[:8] != anchor:
        fail("line %d of the start script changed since stage ①" % line)
    new = lines[:line - 1] + [e.rstrip("\n") for e in EXPORTS] + lines[line - 1:]
    with open(path, "w", encoding="utf-8") as f:   # in place: inode, owner and mode stay
        f.write("\n".join(new))
    print("exports=inserted_before_%d" % line)
    print("act=ok")


def a_mcswap(new, lib, want):
    if os.path.exists(lib) and sha(lib) == want:
        print("mc=already")
        print("act=ok")
        return
    d = os.path.dirname(lib)
    tmp = os.path.join(d, ".t58_new_store.so")
    shutil.copyfile(new, tmp)
    st = os.stat(lib)
    os.chmod(tmp, st.st_mode & 0o7777)
    os.chown(tmp, st.st_uid, st.st_gid)
    os.replace(tmp, lib)
    if sha(lib) != want:
        fail("library in place is not the replacement")
    print("mc=swapped size=%d" % os.stat(lib).st_size)
    print("act=ok")


def a_pxswap(path, new, orig, want):
    cur = sha(path)
    if cur == want:
        print("px=already")
        print("act=ok")
        return
    if cur != orig:
        fail("proxy file is neither the original nor the new one")
    if sha(new) != want:
        fail("new proxy file in the container is not the expected one")
    data = open(new, "rb").read()
    with open(path, "r+b") as f:   # in place: owner and mode stay
        f.write(data)
        f.truncate()
    if sha(path) != want:
        fail("proxy file not in place")
    print("px=swapped")
    print("act=ok")


def a_pxrestore(path, bkfile, orig):
    if sha(bkfile) != orig:
        fail("proxy backup is not the original")
    data = open(bkfile, "rb").read()
    with open(path, "r+b") as f:
        f.write(data)
        f.truncate()
    if sha(path) != orig:
        fail("proxy file not restored")
    print("px=restored")
    print("act=ok")


def a_restore(K, bk, start, mclib=None):
    bad = verify(bk)
    if bad:
        fail("backup does not check out: " + ",".join(bad[:3]))
    meta = json.load(open(os.path.join(bk, "meta.json")))
    if meta["start"] != start:
        fail("backup is for another start script")
    for tname in ("vllm.tar", "vllm-ascend.tar"):
        with tarfile.open(os.path.join(bk, tname)) as t:
            for m in t.getmembers():
                if m.name.startswith("/") or ".." in m.name.split("/"):
                    fail("unexpected name in " + tname)
            t.extractall(LIB)
    removed = 0
    for listfile in (K + "/vllm/NEW_FILES.txt", K + "/vllm-ascend/NEW_FILES.txt"):
        for n in open(listfile).read().split():
            p = os.path.join(LIB, n)
            if os.path.exists(p):
                os.remove(p)
                removed += 1
            pc = os.path.join(os.path.dirname(p), "__pycache__")
            if os.path.isdir(pc):
                stem = os.path.basename(n)[:-3] + "."
                for f in os.listdir(pc):
                    if f.startswith(stem) and f.endswith(".pyc"):
                        os.remove(os.path.join(pc, f))
    left = check_sums(LIB, K + "/vllm/SHA256SUMS.before") + check_sums(LIB, K + "/vllm-ascend/SHA256SUMS.before")
    if left:
        fail("files differ from the backup after restore: " + ",".join(left[:3]))
    data = open(os.path.join(bk, "start-script"), "rb").read()
    with open(start, "r+b") as f:
        f.write(data)
        f.truncate()
    if sha(start) != meta["start_sha256"]:
        fail("start script not restored")
    if mclib and meta.get("mclib"):
        if sha(mclib) != meta["mc_sha256"]:
            tmp = os.path.join(os.path.dirname(mclib), ".t58_orig_store.so")
            shutil.copyfile(os.path.join(bk, "libmooncake_store.so"), tmp)
            os.chmod(tmp, meta["mc_mode"])
            os.chown(tmp, meta["mc_uid"], meta["mc_gid"])
            os.replace(tmp, mclib)
        if sha(mclib) != meta["mc_sha256"]:
            fail("Mooncake library not restored")
        print("mc_sha16=%s" % meta["mc_sha256"][:16])
    print("new_files_removed=%d" % removed)
    print("connector_sha16=%s" % sha(os.path.join(LIB, CONN))[:16])
    print("act=ok")


def main():
    if sys.flags.isolated != 1 or sys.flags.no_site != 1 or sys.flags.dont_write_bytecode != 1:
        print("act=fail run with python3 -I -S -B")
        sys.exit(3)
    if os.getuid() != 0:
        fail("not root")
    a, args = sys.argv[1], sys.argv[2:]
    try:
        if a == "backup":
            a_backup(*args)
        elif a == "verify":
            bad = verify(args[0])
            print("verify=%s" % (",".join(bad[:3]) or "ok"))
            print("act=%s" % ("ok" if not bad else "fail backup does not check out"))
        elif a == "apply":
            a_apply(*args)
        elif a == "exports":
            a_exports(*args)
        elif a == "mcswap":
            a_mcswap(*args)
        elif a == "pxswap":
            a_pxswap(*args)
        elif a == "pxrestore":
            a_pxrestore(*args)
        elif a == "restore":
            a_restore(*args)
        else:
            fail("unknown action")
    except (OSError, ValueError, KeyError, tarfile.TarError) as e:
        fail("%s: %s" % (type(e).__name__, str(e)[:120]))


main()
