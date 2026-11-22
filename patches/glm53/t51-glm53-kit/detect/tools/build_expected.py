#!/usr/bin/env python3
"""Build t51_expected.json for the glm5.3 check (run on the lab machine, not in the intranet).

Baselines: vLLM v0.30.0 and vllm-ascend d4ad49796, both hashed file by file and function by function
(every .py file, because glm5.3 has local edits that may be anywhere). The export list (files the T51
patches change) also embeds the public source text, for --export-diff. "Kit installed" states are
optional (--vllm-kit / --ascend-kit) and are added once the ports exist.

Every hash uses the same functions as the check script, imported from it.
"""

import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import t51_glm53_check as chk  # noqa: E402

VLLM_EXPORT = [
    "vllm/entrypoints/openai/chat_completion/api_router.py",
    "vllm/entrypoints/openai/chat_completion/serving.py",
    "vllm/entrypoints/openai/completion/api_router.py",
    "vllm/entrypoints/openai/completion/serving.py",
    "vllm/inputs/engine.py",
    "vllm/inputs/llm.py",
    "vllm/renderers/base.py",
    "vllm/renderers/online_renderer.py",
    "vllm/v1/core/kv_cache_manager.py",
    "vllm/v1/core/sched/output.py",
    "vllm/v1/core/sched/scheduler.py",
    "vllm/v1/engine/__init__.py",
    "vllm/v1/engine/async_llm.py",
    "vllm/v1/engine/core.py",
    "vllm/v1/engine/core_client.py",
    "vllm/v1/engine/input_processor.py",
    "vllm/v1/engine/output_processor.py",
    "vllm/v1/metrics/stats.py",
    "vllm/v1/request.py",
    "vllm/v1/worker/gpu/kv_connector.py",
    "vllm/v1/worker/gpu/model_runner.py",
    "vllm/v1/worker/kv_connector_model_runner_mixin.py",
    "vllm/v1/worker/worker_base.py",
    # interfaces the patches rely on: connector base class, the multi-connector fan-out, connector registry, logging
    "vllm/distributed/kv_transfer/kv_connector/v1/base.py",
    "vllm/distributed/kv_transfer/kv_connector/v1/multi_connector.py",
    "vllm/distributed/kv_transfer/kv_connector/factory.py",
    "vllm/logger.py",
]
ASCEND_EXPORT = [
    # files the T51 vllm-ascend port changes (diagnostics.py is new and is picked up as a new file)
    "vllm_ascend/distributed/kv_transfer/kv_pool/ascend_store/backend/mooncake_backend.py",
    "vllm_ascend/distributed/kv_transfer/kv_pool/ascend_store/backend/mooncake_layerwise.py",
    "vllm_ascend/distributed/kv_transfer/kv_pool/ascend_store/kv_transfer.py",
    "vllm_ascend/distributed/kv_transfer/kv_pool/ascend_store/metadata.py",
    "vllm_ascend/distributed/kv_transfer/kv_pool/ascend_store/pool_scheduler.py",
    "vllm_ascend/distributed/kv_transfer/kv_pool/ascend_store/pool_worker.py",
    "vllm_ascend/worker/model_runner_v1.py",
    "vllm_ascend/worker/v2/model_runner.py",
    "vllm_ascend/core/recompute_scheduler.py",
    # the P/D connector instrumentation (replayed on site: judged by structure, see --p2d-replay)
    "vllm_ascend/distributed/kv_transfer/kv_p2p/mooncake_connector.py",
    # on the call chains of the measuring points
    "vllm_ascend/distributed/kv_transfer/kv_pool/ascend_store/ascend_store_connector.py",
    "vllm_ascend/distributed/kv_transfer/kv_pool/ascend_store/attention_fence.py",
    "vllm_ascend/worker/worker.py",
    # interfaces: connector registration, the multi-connector, runner selection, the store backend base class
    "vllm_ascend/distributed/kv_transfer/__init__.py",
    "vllm_ascend/distributed/kv_transfer/ascend_multi_connector.py",
    "vllm_ascend/patch/platform/patch_use_v2_model_runner.py",
    "vllm_ascend/distributed/kv_transfer/kv_pool/ascend_store/backend/base.py",
]


def read_list(path, default):
    if not path:
        return list(default)
    with open(path) as f:
        return [line.strip() for line in f if line.strip() and not line.startswith("#")]


def tree(root, top, all_funcs):
    files, _ = chk.hash_tree(os.path.join(root, top), top)
    funcs = {}
    if all_funcs:
        for rel in sorted(files):
            data = chk.read_bytes(os.path.join(root, rel))
            h = chk.func_hashes(data) if data is not None else None
            if h is not None:
                funcs[rel] = h
    return files, funcs


def kit_alt(base_files, kit_root, top):
    k_files, _ = chk.hash_tree(os.path.join(kit_root, top), top)
    alt = {rel: h for rel, h in k_files.items() if base_files.get(rel) != h}
    funcs = {}
    for rel in sorted(alt):
        data = chk.read_bytes(os.path.join(kit_root, rel))
        h = chk.func_hashes(data) if data is not None else None
        if h is not None:
            funcs[rel] = h
    return {"files": alt, "funcs": funcs}


def originals(root, rels):
    out = {}
    for rel in rels:
        data = chk.read_bytes(os.path.join(root, rel))
        if data is None:
            raise SystemExit("export file missing in public tree: %s" % rel)
        out[rel] = data.decode("utf-8")
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--vllm-base", required=True, help="tree root containing vllm/ (v0.30.0)")
    ap.add_argument("--ascend-base", required=True, help="tree root containing vllm_ascend/ (d4ad49796)")
    ap.add_argument("--vllm-kit", action="append", default=[],
                    help="[LABEL=]tree: v0.30.0 with a vLLM port applied (baseline v0.30.0+LABEL; repeat for T51, T56)")
    ap.add_argument("--ascend-kit", action="append", default=[],
                    help="[LABEL=]tree: d4ad49796 with a vllm-ascend port applied (baseline d4ad49796+LABEL)")
    ap.add_argument("--vllm-export-list", help="file with one path per line (default: built-in list)")
    ap.add_argument("--ascend-export-list", help="file with one path per line (default: built-in list)")
    ap.add_argument("--proxy", required=True)
    ap.add_argument("--record", action="append", default=[], help="LABEL=record.json from wheel_record.py")
    ap.add_argument("--p2d-replay", action="append", default=[],
                    help="[LABEL=]replay_p2d.py, embedded byte for byte (structural check of the replayed connector; repeat per version)")
    ap.add_argument("--p2d-extra", help="t51-glm53-p2d extra_checks.py, embedded byte for byte")
    ap.add_argument("--p2d-patched", action="append", default=[],
                    help="[LABEL=]the p2d replacement mooncake_connector.py: its extra_line() is the expected value on site")
    ap.add_argument("--mc-replacement", action="append", default=[], help="LABEL=libmooncake_store.so of a delivered replacement (its sha16 and size: store=replacement(LABEL)); several allowed (v1, v2)")
    ap.add_argument("--mc-site-sha16", help="sha16 of the site's libmooncake_store.so as the field check saw it")
    ap.add_argument("--mc-site-size", type=int)
    ap.add_argument("--mc-siblings", help="directory with the lab build's libtransfer_engine.so, libmooncake_common.so, libasio.so, ascend_transport.so")
    ap.add_argument("--out", required=True)
    a = ap.parse_args()

    v_export = read_list(a.vllm_export_list, VLLM_EXPORT)
    a_export = read_list(a.ascend_export_list, ASCEND_EXPORT)
    v_files, v_funcs = tree(a.vllm_base, "vllm", True)
    a_files, a_funcs = tree(a.ascend_base, "vllm_ascend", True)
    vllm = {"base_label": "v0.30.0", "base": {"files": v_files, "funcs": v_funcs}, "alts": {}, "focus": v_export}
    ascend = {"base_label": "d4ad49796", "base": {"files": a_files, "funcs": a_funcs}, "alts": {}, "focus": a_export}
    kit_text = {"vllm": {}, "vllm_ascend": {}}

    def labelled(items, default):
        out = []
        for item in items:
            label, _, path = item.partition("=") if "=" in item else (default, "", item)
            out.append((label, path))
        return out

    for pkg, ref, base_files, items, top in (("vllm", vllm, v_files, a.vllm_kit, "vllm"),
                                              ("vllm_ascend", ascend, a_files, a.ascend_kit, "vllm_ascend")):
        kits = labelled(items, "port")
        for label, path in kits:
            key = "%s+%s" % (ref["base_label"], label)
            ref["alts"][key] = kit_alt(base_files, path, top)
            texts = originals(path, sorted(ref["alts"][key]["files"]))
            for rel, text in texts.items():
                if len(kits) == 1:
                    kit_text[pkg][rel] = text
                else:
                    kit_text[pkg].setdefault(rel, {})[label] = text

    pdata = chk.read_bytes(a.proxy)
    ex_dir = os.path.join(a.ascend_base, "examples", "external_online_dp")
    mooncake = {}
    for item in a.record:
        label, path = item.split("=", 1)
        with open(path) as f:
            rec = json.load(f)
        keep = {k: v[:16] for k, v in rec["record"].items() if ".so" in k or k.endswith(".py") or k.endswith("mooncake_master")}
        mooncake[label] = {"files": keep, "wheel_url": rec["url"], "wheel_bytes": rec["size"]}
    out = {
        "schema": 1,
        "env": "glm5.3",
        "vllm": vllm,
        "vllm_ascend": ascend,
        "proxy": {"sha": chk.sha16(pdata), "funcs": chk.func_hashes(pdata)},
        "examples": {
            "launch_online_dp.py": chk.file_sha16(os.path.join(ex_dir, "launch_online_dp.py")),
            "run_dp_template.sh": chk.file_sha16(os.path.join(ex_dir, "run_dp_template.sh")),
        },
        "mooncake": mooncake,
        "export": {"vllm": originals(a.vllm_base, v_export), "vllm_ascend": originals(a.ascend_base, a_export)},
        # full text of every file the ports change or add, for the strict comparison after patching
        "port_text": kit_text,
    }
    if a.p2d_replay and a.p2d_extra and a.p2d_patched:
        import base64, hashlib
        xraw = open(a.p2d_extra, "rb").read()
        ns = {"__name__": "t51_embedded_extra_checks", "__file__": "extra_checks.py"}
        exec(compile(xraw.decode("utf-8"), "extra_checks.py", "exec"), ns)
        replays = dict(labelled(a.p2d_replay, "t51"))
        patcheds = dict(labelled(a.p2d_patched, "t51"))
        if set(replays) != set(patcheds):
            raise SystemExit("--p2d-replay and --p2d-patched must name the same versions: %s / %s" % (sorted(replays), sorted(patcheds)))
        versions = {}
        for label in sorted(replays):
            raw = open(replays[label], "rb").read()
            patched = open(patcheds[label], encoding="utf-8").read()
            versions[label] = {"replay_b64": base64.b64encode(raw).decode(), "replay_sha256": hashlib.sha256(raw).hexdigest(),
                               "extra_b64": base64.b64encode(xraw).decode(), "extra_sha256": hashlib.sha256(xraw).hexdigest(),
                               "extra_expected": ns["extra_line"](patched), "patched_sha16": chk.sha16(patched.encode())}
        if len(versions) == 1:
            out["p2d"] = next(iter(versions.values()))
        else:
            out["p2d_versions"] = versions
    if a.mc_replacement:
        out["mooncake_replacements"] = []
        for item in a.mc_replacement:
            label, path = item.split("=", 1) if "=" in item else ("", item)
            out["mooncake_replacements"].append({"label": label, "sha16": chk.file_sha16(path), "size": os.path.getsize(path)})
    if a.mc_site_sha16:
        out["mooncake_site_original"] = {"sha16": a.mc_site_sha16, "size": a.mc_site_size}
    if a.mc_siblings:
        sib = {}
        for name in ("libtransfer_engine.so", "libmooncake_common.so", "libasio.so", "ascend_transport.so"):
            exp = chk.dynsym_exports(os.path.join(a.mc_siblings, name))
            sib[name] = [len(exp), chk.sha16("\n".join(sorted(exp)).encode())]
        out["mooncake_lab_siblings"] = sib
    with open(a.out, "w") as f:
        json.dump(out, f, sort_keys=True, separators=(",", ":"))
    print(
        "vllm files %d funcs-files %d export %d; ascend files %d funcs-files %d export %d; kit alts %s/%s; mooncake refs %d; p2d %s; mc_replacement %s; siblings %d"
        % (len(v_files), len(v_funcs), len(v_export), len(a_files), len(a_funcs), len(a_export),
           list(vllm["alts"]), list(ascend["alts"]), len(mooncake), ",".join("%s:%s" % (k, v["replay_sha256"][:8]) for k, v in sorted((out.get("p2d_versions") or ({"t51": out["p2d"]} if out.get("p2d") else {})).items())) or "-",
           ",".join("%s:%s" % (r["label"], r["sha16"]) for r in out.get("mooncake_replacements") or []) or "-", len(out.get("mooncake_lab_siblings") or {}))
    )


if __name__ == "__main__":
    main()
