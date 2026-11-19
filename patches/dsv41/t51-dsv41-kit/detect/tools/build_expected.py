#!/usr/bin/env python3
"""Build t51_expected.json from public source trees (run on the lab machine, not in the intranet).

Every hash uses the same functions as the check script, imported from it.
"""

import argparse
import json
import os
import re
import sys
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import t51_dsv41_check as chk  # noqa: E402

ASCEND_EXTRA_FOCUS = [
    "vllm_ascend/distributed/kv_transfer/__init__.py",
    "vllm_ascend/distributed/kv_transfer/ascend_multi_connector.py",
    "vllm_ascend/distributed/kv_transfer/kv_pool/ascend_store/ascend_store_connector.py",
    "vllm_ascend/distributed/kv_transfer/kv_p2p/sfa_pd_rd2h/dspark_context.py",
    "vllm_ascend/mrv2_utils.py",
    "vllm_ascend/patch/platform/patch_use_v2_model_runner.py",
    "vllm_ascend/worker/v2/kv_connector.py",
    "vllm_ascend/worker/dspark_utils.py",
    "vllm_ascend/worker/v1/spec_decode/dspark_proposer.py",
]


def patch_files(patch_path, top):
    files = []
    with open(patch_path, encoding="utf-8", errors="replace") as f:
        for line in f:
            m = re.match(r"^diff --git a/(\S+) b/(\S+)", line)
            if m and m.group(2).startswith(top + "/") and m.group(2).endswith(".py"):
                files.append(m.group(2))
    return files


def tree_files(root, top):
    out, _ = chk.hash_tree(os.path.join(root, top), top)
    return out


def funcs_for(root, rels):
    out = {}
    for rel in rels:
        data = chk.read_bytes(os.path.join(root, rel))
        if data is None:
            continue
        h = chk.func_hashes(data)
        if h is None:
            raise SystemExit("cannot parse %s/%s with this python; use python>=3.10" % (root, rel))
        out[rel] = h
    return out


def wheel_files(path):
    out = {}
    with zipfile.ZipFile(path) as z:
        for info in z.infolist():
            name = info.filename
            if name.endswith("/") or ".dist-info/" in name:
                continue
            if ".so" in name or name.endswith(".py") or name.endswith("mooncake_master"):
                out[name] = chk.sha16(z.read(info))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--vllm-base", required=True)
    ap.add_argument("--vllm-patched", required=True)
    ap.add_argument("--patch01", required=True)
    ap.add_argument("--ascend-base", required=True)
    ap.add_argument("--ascend-rc1p", required=True)
    ap.add_argument("--patch02", required=True)
    ap.add_argument("--ascend-kit", help="f9c tree with the T51 kit patch applied (optional)")
    ap.add_argument("--proxy", required=True)
    ap.add_argument("--wheel", action="append", default=[], help="LABEL=path.whl")
    ap.add_argument("--record", action="append", default=[], help="LABEL=record.json from wheel_record.py")
    ap.add_argument("--out", required=True)
    a = ap.parse_args()

    v_focus = patch_files(a.patch01, "vllm")
    v_base = tree_files(a.vllm_base, "vllm")
    v_pat = tree_files(a.vllm_patched, "vllm")
    v_alt = {rel: h for rel, h in v_pat.items() if v_base.get(rel) != h}
    vllm = {
        "base_label": "v0.32.0",
        "base": {"files": v_base, "funcs": funcs_for(a.vllm_base, [f for f in v_focus if f in v_base])},
        "alts": {"v0.32.0+01": {"files": v_alt, "funcs": funcs_for(a.vllm_patched, sorted(v_alt))}},
        "focus": v_focus,
    }

    a_touched = patch_files(a.patch02, "vllm_ascend")
    a_focus = sorted(set(a_touched) | set(ASCEND_EXTRA_FOCUS))
    a_base = tree_files(a.ascend_base, "vllm_ascend")
    rc1p = {}
    for rel in a_touched:
        h = chk.file_sha16(os.path.join(a.ascend_rc1p, rel))
        if h is not None:
            rc1p[rel] = h
    alts = {"rc1+02": {"files": rc1p, "funcs": funcs_for(a.ascend_rc1p, sorted(rc1p))}}
    if a.ascend_kit:
        k_all = tree_files(a.ascend_kit, "vllm_ascend")
        k_alt = {rel: h for rel, h in k_all.items() if a_base.get(rel) != h}
        alts["f9c+t51kit"] = {"files": k_alt, "funcs": funcs_for(a.ascend_kit, sorted(k_alt))}
    ascend = {
        "base_label": "f9c8e0bdb",
        "base": {"files": a_base, "funcs": funcs_for(a.ascend_base, [f for f in a_focus if f in a_base])},
        "alts": alts,
        "focus": a_focus,
    }

    pdata = chk.read_bytes(a.proxy)
    proxy = {"sha": chk.sha16(pdata), "funcs": chk.func_hashes(pdata)}
    ex_dir = os.path.join(a.ascend_base, "examples", "external_online_dp")
    examples = {
        "launch_online_dp.py": chk.file_sha16(os.path.join(ex_dir, "launch_online_dp.py")),
        "run_dp_template.sh": chk.file_sha16(os.path.join(ex_dir, "run_dp_template.sh")),
    }
    mooncake = {}
    for item in a.wheel:
        label, path = item.split("=", 1)
        mooncake[label] = {"files": wheel_files(path), "wheel_sha16": chk.file_sha16(path)}

    for item in a.record:
        label, path = item.split("=", 1)
        with open(path) as f:
            rec = json.load(f)
        keep = {k: v[:16] for k, v in rec["record"].items() if ".so" in k or k.endswith(".py") or k.endswith("mooncake_master")}
        mooncake[label] = {"files": keep, "wheel_url": rec["url"], "wheel_bytes": rec["size"]}

    out = {
        "schema": 1,
        "vllm": vllm,
        "vllm_ascend": ascend,
        "proxy": proxy,
        "examples": examples,
        "mooncake": mooncake,
    }
    with open(a.out, "w") as f:
        json.dump(out, f, sort_keys=True, separators=(",", ":"))
    print("vllm files %d (alt %d, focus %d); ascend files %d (rc1+02 %d, focus %d); mooncake %s" % (
        len(v_base), len(v_alt), len(v_focus), len(a_base), len(rc1p), len(a_focus),
        {k: len(v["files"]) for k, v in mooncake.items()}))


if __name__ == "__main__":
    main()
