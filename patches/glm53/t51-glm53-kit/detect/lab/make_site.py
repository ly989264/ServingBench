#!/usr/bin/env python3
"""Build a fake site-packages for one lab case (dist-info metadata, editable .pth files, Mooncake files).

Usage: make_site.py OUT_DIR [--mooncake-wheel PATH | --fake-mooncake] [--proxy-deps] [--no-vllm]
"""

import argparse
import base64
import hashlib
import json
import os
import zipfile


def dist(site, name, version, editable_url=None, top=None):
    d = os.path.join(site, "%s-%s.dist-info" % (name.replace("-", "_"), version))
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "METADATA"), "w") as f:
        f.write("Metadata-Version: 2.1\nName: %s\nVersion: %s\n" % (name, version))
    if editable_url:
        with open(os.path.join(d, "direct_url.json"), "w") as f:
            json.dump({"url": editable_url, "dir_info": {"editable": True}}, f)
        with open(os.path.join(site, "__editable__.%s-%s.pth" % (name.replace("-", "_"), version)), "w") as f:
            f.write(editable_url[len("file://") :] + "\n")
    if top:
        with open(os.path.join(d, "top_level.txt"), "w") as f:
            f.write(top + "\n")


def install_local(site, tree, tamper):
    """pip install /path (not editable): files copied into the site, RECORD with sha256, direct_url without editable."""
    import shutil
    version = "0.30.0rc2.dev157+gd4ad49796.d20261018"
    shutil.copytree(os.path.join(tree, "vllm_ascend"), os.path.join(site, "vllm_ascend"), ignore=shutil.ignore_patterns("__pycache__"))
    with open(os.path.join(site, "vllm_ascend", "_version.py"), "w") as f:
        f.write("__version__ = version = %r\n" % version)
    dist(site, "vllm-ascend", version, top="vllm_ascend")
    d = os.path.join(site, "vllm_ascend-%s.dist-info" % version)
    with open(os.path.join(d, "direct_url.json"), "w") as f:
        json.dump({"url": "file:///build/vllm-ascend", "dir_info": {}}, f)
    rows = []
    for dirpath, _dirs, names in os.walk(os.path.join(site, "vllm_ascend")):
        for fn in names:
            full = os.path.join(dirpath, fn)
            data = open(full, "rb").read()
            b64 = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode()
            rows.append("%s,sha256=%s,%d" % (os.path.relpath(full, site), b64, len(data)))
    with open(os.path.join(d, "RECORD"), "w") as f:
        f.write("\n".join(sorted(rows)) + "\n")
    if tamper:
        with open(os.path.join(site, "vllm_ascend", "utils.py"), "a") as f:
            f.write("\n# edited after install\n")
        with open(os.path.join(site, "vllm_ascend", "site_extra.py"), "w") as f:
            f.write("X = 1\n")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("out")
    ap.add_argument("--mooncake-wheel")
    ap.add_argument("--fake-mooncake", action="store_true")
    ap.add_argument("--mooncake-patched", action="store_true", help="with --fake-mooncake: replace the store library after install")
    ap.add_argument("--proxy-deps", action="store_true")
    ap.add_argument("--no-vllm", action="store_true")
    ap.add_argument("--profile", default="glm53", choices=("glm53",))
    ap.add_argument("--ascend-local", help="install this tree's vllm_ascend/ into the site as a non-editable local install, with RECORD")
    ap.add_argument("--ascend-tamper", action="store_true", help="with --ascend-local: change one file and add one file after install")
    a = ap.parse_args()
    site = a.out
    os.makedirs(site, exist_ok=True)
    if not a.no_vllm:
        dist(site, "vllm", "0.30.0+empty", "file:///vllm-workspace/vllm")
        if a.ascend_local:
            install_local(site, a.ascend_local, a.ascend_tamper)
        else:
            dist(site, "vllm-ascend", "0.30.0rc2.dev157+gd4ad49796.d20261018", "file:///vllm-workspace/vllm-ascend")
        dist(site, "torch", "2.10.0a0+gitunknown")
        dist(site, "torch-npu", "2.10.0.post4+git5dd8ef3")
        dist(site, "transformers", "5.14.1")
    if a.proxy_deps:
        dist(site, "fastapi", "0.121.0")
        dist(site, "httpx", "0.28.1")
        dist(site, "uvicorn", "0.38.0")
    if a.mooncake_wheel:
        # a real wheel, laid out as pip installs it (its own dist-info, METADATA and RECORD included)
        with zipfile.ZipFile(a.mooncake_wheel) as z:
            for info in z.infolist():
                if info.filename.endswith("/"):
                    continue
                target = os.path.join(site, info.filename)
                os.makedirs(os.path.dirname(target), exist_ok=True)
                with open(target, "wb") as f:
                    f.write(z.read(info))
    elif a.fake_mooncake:
        # same layout as the OBS v0.3.14.rc1 wheel used by the vllm-ascend image; contents are fake
        d = os.path.join(site, "mooncake_transfer_engine-0.3.13.dist-info")
        dist(site, "mooncake-transfer-engine", "0.3.13")
        pkg = os.path.join(site, "mooncake")
        libs = os.path.join(site, "mooncake_transfer_engine.libs")
        os.makedirs(pkg, exist_ok=True)
        os.makedirs(libs, exist_ok=True)
        files = {
            "mooncake/__init__.py": b"",
            "mooncake/store.so": b"\x7fELF fake store libmooncake_store-2a608938.so" + b"\0" * 4096,
            "mooncake/engine.so": b"\x7fELF fake engine" + b"\0" * 4096,
            "mooncake/libmooncake_store.so": b"\x7fELF fake libglog-bb9621e6.so.0" + b"\0" * 4096,
            "mooncake_transfer_engine.libs/libmooncake_store-2a608938.so": b"\x7fELF fake libglog-bb9621e6.so.0" + b"\0" * 4096,
            "mooncake_transfer_engine.libs/libglog-bb9621e6.so.0.4.0": b"\x7fELF glog" + b"\0" * 1024,
        }
        record = []
        for rel, data in files.items():
            with open(os.path.join(site, rel), "wb") as f:
                f.write(data)
            digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode()
            record.append("%s,sha256=%s,%d" % (rel, digest, len(data)))
        with open(os.path.join(d, "RECORD"), "w") as f:
            f.write("\n".join(record) + "\n")
        if a.mooncake_patched:
            # a rebuilt library with the native timing replaces the installed one after pip install
            with open(os.path.join(libs, "libmooncake_store-2a608938.so"), "wb") as f:
                f.write(b"\x7fELF rebuilt libglog-bb9621e6.so.0 [BatchPutTiming] keys=" + b"\0" * 4096)

if __name__ == "__main__":
    main()
